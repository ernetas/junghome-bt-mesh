"""JUNG HOME over Bluetooth Mesh — no gateway required."""

from __future__ import annotations

import logging
import time
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .actions.common import async_register_configurator, async_unregister_configurator
from .app_follow import AppFollower
from .config_entities import device_lock_targets, retired_unique_ids
from .config_flow import (
    LOAD_ERRORS,
    SOURCE_GATEWAY,
    SOURCE_UPLOAD,
    async_migrate_unique_id,
    forget_stored_export,
    infer_source,
    mesh_proxies_without_match,
    proxy_in_range,
)
from .const import (
    CONF_CDB_PATH,
    CONF_MESH_UUID,
    CONF_METADATA_DIR,
    CONF_SOURCE,
    CONF_UNICAST,
    DOMAIN,
    ISSUE_CARRY_OVER_CONFLICT,
    ISSUE_LEARN_MORE,
    PLATFORMS,
    STORAGE_DIR,
    issue_id,
)
from .coordinator import JungHomeConfigEntry as JungHomeConfigEntry
from .coordinator import (
    JungHomeHub,
    async_known_mesh,
    forget_known_mesh,
    load_network,
    remember_known_mesh,
)
from .device_names import async_track_device_names
from .entity import current_device_identifiers, register_parent_devices
from .export_view import ExportDownloadView
from .identity import async_vault_keeper
from .jhmesh.cdb import CDB
from .jhmesh.client import MESH_PROXY_SERVICE
from .jhmesh.devices import InvalidMetadata
from .mesh_config import (
    async_remove_gateway_sync,
    cancel_upload_retry,
    gateway_sync,
    held_scenes,
    plan_journal,
    token_rejected_open,
)
from .migration import (
    async_update_gateway_issue,
    drop_retired_entities,
    enable_now_default,
)
from .model_update import (
    async_follow_export,
    async_sync_areas,
    check_our_address,
    remove_stale_devices,
)
from .node_info import async_remove_node_versions
from .onboard import async_clear_vault_issue, async_update_pending_issue
from .seq_store import (
    STORAGE_VERSION,
    async_apply_followed_key_refresh,
    async_migrate_legacy_seq_store,
)
from .services import async_setup_services

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)


# set up from the UI only: a `junghome_ble:` key in configuration.yaml gets Home Assistant's "does not support YAML"
# error and repair instead of being accepted without a word (the integration has `async_setup`, for its actions)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

INCOMING_MAX_AGE: Final = (
    3600.0  # seconds; an `.incoming-*` file older than this belongs to no flow any more
)


def sweep_incoming(
    store: Path, max_age: float, keep: frozenset[str] = frozenset()
) -> int:
    """Blocking: delete `.incoming-<flow id>.json` files older than `max_age` in `store`; returns how many.

    A fetched or uploaded export lives under that name while its config flow validates it, and every failure
    path deletes it; this catches whatever a crash or a lost flow left behind (the file holds every mesh key).
    `keep` names flow ids still in progress.
    """
    if not store.is_dir():
        return 0
    removed = 0
    cutoff = time.time() - max_age
    for path in store.glob(".incoming-*.json"):
        flow_id = path.name[len(".incoming-") : -len(".json")]
        try:
            if flow_id in keep or path.stat().st_mtime > cutoff:
                continue
            path.unlink()
        except OSError as err:
            _LOGGER.debug("could not remove %s: %s", path, err)
            continue
        removed += 1
    return removed


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Register the services and the export download once; they resolve the entry per call. Sweep stale incoming exports."""
    async_setup_services(hass)
    hass.http.register_view(ExportDownloadView())
    removed = await hass.async_add_executor_job(
        sweep_incoming, Path(hass.config.path(STORAGE_DIR)), INCOMING_MAX_AGE
    )
    if removed:
        _LOGGER.info("Removed %d stale incoming export file(s)", removed)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring an entry up to the flow's version: 1.2 names its `CONF_SOURCE`, 1.3 its unique id the mesh UUID.

    Entries from before `CONF_SOURCE` had none, and the places that branch on it disagreed about them: an old
    gateway entry never refreshed its export for an unknown node, and removing an old gateway or upload entry left
    the export we had stored (every mesh key) on disk. `needs_rebuild` compares against what the hub was built
    from, and this runs before setup, so it causes no reload.

    Up to 1.2 the unique id was the Network ID, which a key refresh changes. An entry whose
    mesh UUID cannot be read yet (its export unreadable) stays at 1.2 and is set up as it is; the next start tries
    again (`config_flow.async_migrate_unique_id`). Nothing here fails the setup. Unverified on air.
    """
    if entry.version != 1:
        return False  # a newer major version (a downgrade): not ours to read
    if entry.minor_version < 2:
        data = dict(entry.data)
        data.setdefault(CONF_SOURCE, infer_source(hass, data))
        hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
    if entry.minor_version < 3 and await async_migrate_unique_id(hass, entry):
        hass.config_entries.async_update_entry(entry, minor_version=3)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: JungHomeConfigEntry) -> bool:
    """Load the mesh export, make sure a proxy node is in range, start the link."""
    try:
        cdb, devices = await hass.async_add_executor_job(
            load_network,
            entry.data[CONF_CDB_PATH],
            entry.data.get(CONF_METADATA_DIR) or None,
        )
    except InvalidMetadata as err:  # before LOAD_ERRORS (it is a ValueError): the error names the metadata file
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="cannot_load",
            translation_placeholders={"path": str(err.path)},
        ) from err
    except LOAD_ERRORS as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="cannot_load",
            translation_placeholders={"path": entry.data[CONF_CDB_PATH]},
        ) from err

    # Home Assistant's own provisioner entry and node, should the file carry them: its address, not
    # another node's; its range, not another provisioner's
    vault = await async_vault_keeper(hass, cdb.mesh_uuid)
    await vault.async_recover(cdb, int(entry.data[CONF_UNICAST], 16))
    check_our_address(
        hass, entry, cdb, int(entry.data[CONF_UNICAST], 16), vault.own_uuid
    )

    # Before the not-ready check below, which can retry indefinitely without ever reaching
    # JungHomeHub.async_create (no proxy seen yet is common right after a restart): an entry stuck there must
    # still migrate its 0.2 per-entry store, or removing it while stuck strands that record.
    await async_migrate_legacy_seq_store(hass, entry, cdb.mesh_uuid)

    # a key refresh completed since the export was made: its new key, which the hub followed
    await async_apply_followed_key_refresh(hass, cdb, int(entry.data[CONF_UNICAST], 16))
    # what discovery recognises this mesh by, even while the entry retries below
    remember_known_mesh(
        hass,
        entry.entry_id,
        await async_known_mesh(hass, cdb, int(entry.data[CONF_UNICAST], 16)),
    )

    # Checked before the hub exists: creating its sequence-number state schedules a store write and, after a
    # crash, adds the restart margin — doing that on every not-ready retry would burn 512 sequence numbers each.
    if not proxy_in_range(hass, cdb):
        # no connectable scanner at all is a Home Assistant without Bluetooth, not a mesh out of range: say so
        # (the app's "Bluetooth is off" screen, `ObserveBluetoothState`); nodes of the export advertising another
        # Network ID are this mesh under keys the export lacks: a key refresh since the export
        # was made, which only a new export (Reconfigure) fixes
        if bluetooth.async_scanner_count(hass, connectable=True) == 0:
            reason = "bluetooth_unavailable"
        elif mesh_proxies_without_match(hass, cdb):
            reason = "export_keys_stale"
        else:
            reason = "no_proxy_visible"
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key=reason)

    hub = await JungHomeHub.async_create(
        hass, entry, cdb, devices, int(entry.data[CONF_UNICAST], 16)
    )
    entry.runtime_data = hub
    # what an action, a rename or the unknown-node adoption has the hub follow the export with
    hub.follow_export = partial(async_follow_export, hass, entry.entry_id)
    # what the nodes advertised or answered of their inserts, before any device is registered (`inserts.py`)
    await hub.inserts.async_setup()
    remove_stale_devices(hass, entry, hub)
    drop_retired_entities(hass, retired_unique_ids(hub))
    enable_now_default(
        hass,
        "switch",
        (t.unique_id for t in device_lock_targets(hub) if t.enabled_default),
    )
    register_parent_devices(hass, hub)
    # registered first: whatever `async_start` got going before a failure is stopped with the failed setup
    entry.async_on_unload(hub.async_stop)
    # what the entry last exchanged with its gateway (`configurator.store.GatewaySync`), before anything compares
    # with it
    await gateway_sync(hass, entry.entry_id).async_load(entry)
    # before the start: the adverts the start replays can already name unknown nodes, whose export refresh
    # adopts through the configurator
    async_register_configurator(hass, entry)
    entry.async_on_unload(partial(async_unregister_configurator, hass, entry))
    # a configuration plan Home Assistant stopped or crashed in the middle of: what its devices accepted goes into
    # the export, and the entry is set up again from it
    assert hub.configurator is not None  # registered just above
    if await hub.configurator.async_replay_journal():
        _reload_once_loaded(hass, entry)
    # changes made in the JUNG HOME app: the phone heard on the mesh, the periodic check (`app_follow.py`)
    follower = hub.app_follow = AppFollower(hub)
    follower.start()
    entry.async_on_unload(follower.stop)
    await hub.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # every device registered: those whose room an export change moved follow it (`OPTION_SYNC_AREAS`)
    async_sync_areas(hass, entry, hub)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    # a device the user names goes into the app's project too (device_names.py)
    entry.async_on_unload(async_track_device_names(hass, entry))
    async_update_gateway_issue(hass, entry)
    # a reload aborts the entry's reauth flows (Home Assistant's `async_reload`), and most actions reload it right
    # after the change that met the rejected token: ask again while the token repair is open
    if token_rejected_open(hass, entry):
        entry.async_start_reauth(hass)
    # a device Home Assistant provisioned but never recorded (onboard.py)
    async_update_pending_issue(hass, entry, hub.vault)
    # the vault could not be written while a device was added: the next save that lands clears it
    entry.async_on_unload(
        hub.vault.async_add_listener(
            partial(async_clear_vault_issue, hass, entry, hub.vault)
        )
    )
    return True


def _reload_once_loaded(hass: HomeAssistant, entry: JungHomeConfigEntry) -> None:
    """Set the entry up again as soon as this setup finished: the hub was built from the export before the replay.

    A reload scheduled while the setup still runs would find the entry mid-setup; one setup that does not finish
    (an error, not ready) needs no second one — the next attempt reads the recorded export anyway.
    """
    pending = True

    def changed() -> None:
        nonlocal pending
        if not pending:
            return
        pending = False
        hass.loop.call_soon(
            unsubscribe
        )  # not while the entry runs through its callbacks
        if entry.state is ConfigEntryState.LOADED:
            hass.config_entries.async_schedule_reload(entry.entry_id)

    unsubscribe = entry.async_on_state_change(changed)


async def _async_entry_updated(hass: HomeAssistant, entry: JungHomeConfigEntry) -> None:
    """Reload the entry when the data or the options the running hub was built from changed; else apply the rest.

    Home Assistant wants the update listener to do the reloading (a flow that reloads next to a listener is
    deprecated): the reconfigure flow only updates a loaded entry. A new title or gateway token / fingerprint is
    not what the hub was built from (`JungHomeHub.needs_rebuild`) and changes nothing; neither does a change of
    `LIVE_OPTIONS` (the keys that wait for a double click, say), which the hub applies in place.
    """
    hub = entry.runtime_data if entry.state is ConfigEntryState.LOADED else None
    if hub is not None and not hub.needs_rebuild:
        hub.async_options_updated()
    # a rebuild begins once: the listener runs again while the reload it starts is pending
    elif hub is not None and await hub.async_begin_rebuild():
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: JungHomeConfigEntry) -> bool:
    """Unload the platforms; the hub itself is stopped by the entry's on_unload hook."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        async_unregister_configurator(hass, entry)
        if entry.disabled_by is not None:
            # the configurator's repair outlives a reload on purpose (the adopt it reports is often followed by
            # one), not an entry disabled: nothing sets it up again to keep it current
            ir.async_delete_issue(
                hass, DOMAIN, issue_id(entry, ISSUE_CARRY_OVER_CONFLICT)
            )
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete what the integration stored for the entry; a user-provided export path stays.

    A fetched or uploaded export goes with its backups and the copies the services keep beside it
    (`forget_stored_export`), and so does any orphaned `.incoming-*` file no flow claims.
    The sequence-number store is not the entry's: it belongs to the mesh (`seq_store.seq_store`, one record per
    address ever used) and is kept on purpose, so an entry re-created for the same mesh continues its counters
    instead of reusing nonces the nodes have already seen. An entry whose setup never reached
    `JungHomeHub.async_create` (SETUP_RETRY, SETUP_ERROR) never migrated its own 0.2 per-entry store into the
    mesh's, so that has to happen here too — before the export is deleted below, since a gateway/upload entry's
    CDB file is the fallback this uses to find the mesh UUID.
    """
    # a pending retry of a failed upload outlives reloads (`ExportStore._upload_or_retry`), not the entry
    cancel_upload_retry(hass, entry.entry_id)
    forget_known_mesh(hass, entry.entry_id)
    legacy: Store[dict[str, Any]] = Store(
        hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
    )
    if await legacy.async_load() is not None:
        mesh_uuid = entry.data.get(CONF_MESH_UUID)
        if not mesh_uuid:
            try:
                cdb = await hass.async_add_executor_job(
                    CDB.load, Path(entry.data[CONF_CDB_PATH])
                )
            except LOAD_ERRORS:
                cdb = None
            mesh_uuid = cdb.mesh_uuid if cdb is not None else None
        if mesh_uuid:
            await async_migrate_legacy_seq_store(hass, entry, mesh_uuid)
        else:
            _LOGGER.warning(
                "could not tell which mesh %s's sequence-number record (%s) belongs to; keeping it",
                entry.title,
                legacy.path,
            )

    await async_remove_node_versions(hass, entry.entry_id)
    await plan_journal(hass, entry.entry_id).async_remove()
    await held_scenes(hass, entry.entry_id).async_remove()
    await async_remove_gateway_sync(hass, entry.entry_id)
    # the mesh's proxies were matched to this entry: let discovery offer them again
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        if MESH_PROXY_SERVICE in info.service_data:
            bluetooth.async_rediscover_address(hass, info.address)
    store = Path(hass.config.path(STORAGE_DIR))
    # an entry never set up since the upgrade (disabled, say) was never migrated: judge its source the same way
    source = entry.data.get(CONF_SOURCE) or infer_source(hass, entry.data)
    if source in (SOURCE_GATEWAY, SOURCE_UPLOAD):
        await hass.async_add_executor_job(
            forget_stored_export, Path(entry.data[CONF_CDB_PATH])
        )
    in_progress = frozenset(
        flow["flow_id"]
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    )
    await hass.async_add_executor_job(sweep_incoming, store, 0.0, in_progress)
    # every repair issue of the entry, whoever raised it — the hub, the configurator (`carry_over_conflict`), a
    # flow: the unload cleared only the hub's, and a removed entry may never have loaded. Every issue has a *Learn
    # more* link, so `ISSUE_LEARN_MORE` names them all; each one's id is its key and the entry's id (`issue_id`)
    for key in ISSUE_LEARN_MORE:
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry, key))


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: JungHomeConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting devices that are no longer part of the exported network.

    An entry that is not loaded has no hub to ask: any device may go, the next setup adds back what the export has.
    """
    if entry.state is not ConfigEntryState.LOADED:
        return True
    return not (device.identifiers & current_device_identifiers(entry.runtime_data))
