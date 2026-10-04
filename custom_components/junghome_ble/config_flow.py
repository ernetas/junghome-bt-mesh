"""Config flow: point the integration at the JUNG HOME app's mesh export.

Entry points: manual (user), Bluetooth discovery of a JUNG node's Mesh Proxy advertisement, zeroconf discovery of the
JUNG HOME Gateway, reconfigure, and reauth. All but reauth and zeroconf lead to the same menu: fetch the export from the
JUNG HOME Gateway, upload the app's export file, or name a file that is already on the Home Assistant host; zeroconf goes
straight to the gateway form with the address the gateway announced. Our unicast address sits in a collapsed
*Advanced* section of every source form (review-4 U4-8): the default suits every installation with one Home
Assistant. Fetched and uploaded exports are kept under `<config>/junghome_ble/<mesh UUID>.json` (mode 0600: they hold
every mesh key).

Whatever the source, the export is validated by loading it (`CDB.load` checks the document's shape; the mesh
UUID that names the stored file is a UUID by then) and by checking that at least one proxy node of *that* network
is currently advertising (its Network ID is derived from the NetKey in the file); nodes of the export advertising
another Network ID mean a stale export (`export_keys_stale`), not a mesh out of range. A fetched or uploaded file
lives as `.incoming-<flow id>.json` until it passes; every failure path, and the flow's removal, deletes it.

Discovery offers a Mesh Proxy that also carries JUNG's manufacturer data (company id 0x0527: the manifest's matcher
needs both, and the step aborts `not_jung` without it; review-4 H I-7) and never a configured mesh: an advertisement
whose Network ID or Node Identity any configured entry's keys derive (the export's, and a followed key refresh's),
or from a node MAC of its export, is that mesh (`coordinator.KnownMesh`). An entry's unique id is its mesh UUID
(decision M10, H I-9), which no advertisement carries and no key refresh changes; a discovery flow holds the
advertised Network ID as its unique id only until the export is given. The gateway's mDNS announcement
(`_junghome._tcp`, TXT `serial`, `manufacturer=JUNG`) is offered once per gateway serial, and never for a gateway an
entry already names by that address; the address is only prefilled, and the certificate is pinned exactly as for a
typed one.

The gateway is only ever spoken to over a connection pinned to its certificate (`tls.py`). The pin comes from the
mesh when the hub is connected (the gateway node reports its own certificate fingerprint), else from the entry
(recorded at the previous fetch), else it is learned at first contact before the password, the access request or
the token leaves. A responder with another certificate is refused at the TLS handshake; the `gateway_certificate`
step then shows both fingerprints and continues only once the user vouches for the new one — unless the gateway
node vouched for the pin (`CONF_GATEWAY_PIN_SOURCE`): then the responder is not the gateway, and the flow aborts.
A pin the user vouched for is compared with the gateway node's report by the hub before the gateway is used.

Reauth renews the gateway token alone, when the gateway rejected the entry's (`MeshConfigurator.report_token_rejected`
starts it): by the gateway's password, or by approving a new access request in the app. The export, the mesh and the
running hub are left alone; the new token is used from the next request on.

Once an export is loaded (setup, discovery, reconfigure), the `areas` step maps each JUNG room to a Home Assistant
area (`areas.py`, review-4 U4-2): prefilled with the area named or aliased like the room, left empty for an area named
after it; stored in the entry's options. The reconfigure menu offers the same step on its own, which also moves the
devices still in the area the previous mapping gave them, never one the user placed.

The reconfigure menu also offers the import from the JUNG HOME Gateway integration (`migration.py`): a dry run shown
as a form, applied on confirmation. Options (`JungHomeOptionsFlow`) hold the runtime behaviour switches; a change
reloads the entry through the update listener (`__init__._async_entry_updated`), which also reloads a loaded entry
after a reconfiguration changed what the hub is built from.

The steps of a new export for an existing entry are module functions, shared with the repairs that load one
(`repairs.NewExportFlow`, review-4 U4-5): taking an upload in (`async_take_upload`), fetching from the gateway
(`async_fetch_to_store`, pinned by `async_known_pin`), checking it (`async_validate_stored`) and the reconfigure's end
(`async_replace_export`).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    AreaSelector,
    BooleanSelector,
    FileSelector,
    FileSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .areas import async_move_devices, mapped_area
from .const import (
    CONF_CDB_PATH,
    CONF_EXPORT_FILE,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_PASSWORD,
    CONF_GATEWAY_PIN_SOURCE,
    CONF_GATEWAY_SYNCED,
    CONF_GATEWAY_TOKEN,
    CONF_MESH_UUID,
    CONF_METADATA_DIR,
    CONF_ROOM_AREAS,
    CONF_SOURCE,
    CONF_UNICAST,
    DEFAULT_ALLOW_PROVISIONING,
    DEFAULT_ASSIGN_AREAS,
    DEFAULT_CLICK_DELAY,
    DEFAULT_FOLLOW_APP,
    DEFAULT_GATEWAY_CHECK,
    DEFAULT_HEARTBEATS,
    DEFAULT_PROVISIONER_IDENTITY,
    DEFAULT_SYNC_AREAS,
    DEFAULT_UNICAST,
    DOMAIN,
    GATEWAY_DEFAULT_HOST,
    GATEWAY_DOMAIN,
    GATEWAY_USER_NAME,
    ISSUE_APP_CHANGED,
    ISSUE_GATEWAY_CERTIFICATE,
    ISSUE_GATEWAY_TOKEN,
    OPTION_ALLOW_PROVISIONING,
    OPTION_ASSIGN_AREAS,
    OPTION_CLICK_DELAY,
    OPTION_FOLLOW_APP,
    OPTION_GATEWAY_CHECK,
    OPTION_HEARTBEATS,
    OPTION_PROVISIONER_IDENTITY,
    OPTION_SYNC_AREAS,
    PIN_FROM_MESH,
    PIN_FROM_USER,
    STORAGE_DIR,
)
from .coordinator import (
    KNOWN_MESHES,
    KnownMesh,
    abort_discovery_flows,
    async_apply_followed_key_refresh,
    async_known_mesh,
    async_release_network_id,
    forget_known_mesh,
    hub_data,
    issue_id,
    node_macs,
)
from .entity import device_rooms
from .gateway_api import (
    GatewayAuthError,
    GatewayBusy,
    GatewayCertificateMismatch,
    GatewayError,
    GatewayNoProject,
    GatewayNotApproved,
    GatewayUnreachable,
    JungHomeGatewayApi,
)
from .identity import async_vault_keeper
from .jhmesh.advert import JUNG_COMPANY_ID
from .jhmesh.cdb import CDB, UUID_PATTERN, InvalidExport
from .jhmesh.client import MESH_PROXY_SERVICE, classify_proxy_advert
from .jhmesh.devices import InvalidMetadata, Metadata, room_names
from .jhmesh.export import write_private
from .jhmesh.fileio import PRIVATE_MODE, backup_paths, copy_private, fsync_dir
from .jhmesh.vault import recognise
from .mesh_config import app_copy_path, export_digest, gateway_sync, pre_adopt_path
from .migration import (
    ImportAborted,
    ImportPlan,
    async_apply_import,
    build_import_plan,
)
from .model_update import remember_device_rooms
from .tls import (
    CONF_GATEWAY_FINGERPRINT,
    async_learn_fingerprint,
    async_read_mesh_fingerprint,
    format_fingerprint,
    normalize_fingerprint,
)

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

_LOGGER = logging.getLogger(__name__)

SOURCE_GATEWAY = "gateway"
SOURCE_UPLOAD = "upload"
SOURCE_PATH = "path"
STEP_REFETCH = "gateway_refetch"
STEP_FETCH = "gateway_fetch"
STEP_IMPORT = "import_gateway"
STEP_CERTIFICATE = "gateway_certificate"
STEP_REGISTER = "gateway_register"
STEP_REAUTH = "reauth_confirm"
STEP_REAUTH_DONE = "reauth_done"
STEP_AREAS = "areas"
STEP_ZEROCONF_CONFIRM = "zeroconf_confirm"
# the collapsed section of the source forms that holds our unicast address (its strings: `sections.advanced`)
SECTION_ADVANCED = "advanced"
# the gateway's mDNS TXT record `manufacturer`, when it carries one; anything else is not a JUNG HOME Gateway
GATEWAY_MANUFACTURER = "JUNG"

# Exceptions a malformed export can raise from `CDB.load` beyond `InvalidExport` (which covers the validated shape):
# an unreadable file, JSON that is not JSON, and — should a shape slip past the validation — the bare Python errors.
LOAD_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError)

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class _FormError(Exception):
    """Validation failed; `errors` is what the form shows (field or `base` → translation key)."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__(str(errors))
        self.errors = errors


class _CertificateChanged(Exception):
    """The gateway refused at the TLS handshake: the user must vouch for its certificate before anything is sent."""

    def __init__(self, mismatch: GatewayCertificateMismatch) -> None:
        super().__init__(str(mismatch))
        self.mismatch = mismatch


def certificate_issue_id(entry_id: str) -> str:
    """Return the id of the repair issue telling the user that `entry_id`'s gateway changed its certificate.

    Raised at runtime (`JungHomeHub.async_raise_certificate_issue`); a finished reconfigure removes it.
    """
    return f"{ISSUE_GATEWAY_CERTIFICATE}_{entry_id}"


def _advanced(unicast: str) -> dict[Any, Any]:
    """Return the collapsed *Advanced* section of a source form: our unicast address, which hardly anyone changes."""
    return {
        vol.Required(SECTION_ADVANCED): section(
            vol.Schema({vol.Required(CONF_UNICAST, default=unicast): TextSelector()}),
            {"collapsed": True},
        )
    }


def _unicast_input(user_input: Mapping[str, Any]) -> str:
    """Return our unicast address as a source form submitted it, inside its *Advanced* section."""
    return str(user_input[SECTION_ADVANCED][CONF_UNICAST])


def _form_errors(errors: dict[str, str]) -> dict[str, str]:
    """Show an error of our unicast address on the *Advanced* section that holds the field.

    `validate_input` names the field (the repairs sharing it have no such field and show it as their form's own);
    the frontend shows the error of a field inside a section on the section.
    """
    return {
        (SECTION_ADVANCED if key == CONF_UNICAST else key): value
        for key, value in errors.items()
    }


def _schema(defaults: Mapping[str, Any], unicast: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_CDB_PATH, default=defaults.get(CONF_CDB_PATH, "")
            ): TextSelector(),
            vol.Optional(
                CONF_METADATA_DIR, default=defaults.get(CONF_METADATA_DIR, "")
            ): TextSelector(),
            **_advanced(unicast),
        }
    )


def _upload_schema(unicast: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_EXPORT_FILE): FileSelector(
                FileSelectorConfig(accept=".json,application/json")
            ),
            **_advanced(unicast),
        }
    )


def _gateway_schema(host: str, unicast: str) -> vol.Schema:
    # the password is never suggested back: a retry is usually because it was wrong
    return vol.Schema(
        {
            vol.Required(CONF_GATEWAY_HOST, default=host): TextSelector(),
            vol.Optional(CONF_GATEWAY_PASSWORD): _PASSWORD,
            **_advanced(unicast),
        }
    )


def _reauth_schema() -> vol.Schema:
    return vol.Schema({vol.Optional(CONF_GATEWAY_PASSWORD): _PASSWORD})


def _normalize_host(raw: str) -> str:
    """Strip scheme, whitespace and trailing slash; hosts are case-insensitive."""
    host = raw.strip()
    for prefix in ("https://", "http://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix) :]
    return host.rstrip("/").lower()


def _parse_unicast(raw: str) -> int | None:
    """Parse our own address as typed: hexadecimal, 0001-7FFF; None when it is not."""
    try:
        unicast = int(raw, 16)
    except ValueError:
        return None
    return unicast if 0 < unicast < 0x8000 else None


def _hex4(unicast: str) -> str:
    """Format the address as the entry records it: four upper-case hex digits."""
    return f"{int(unicast, 16):04X}"


def _options_schema(options: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                OPTION_CLICK_DELAY,
                default=options.get(OPTION_CLICK_DELAY, DEFAULT_CLICK_DELAY),
            ): BooleanSelector(),
            vol.Required(
                OPTION_HEARTBEATS,
                default=options.get(OPTION_HEARTBEATS, DEFAULT_HEARTBEATS),
            ): BooleanSelector(),
            vol.Required(
                OPTION_ALLOW_PROVISIONING,
                default=options.get(
                    OPTION_ALLOW_PROVISIONING, DEFAULT_ALLOW_PROVISIONING
                ),
            ): BooleanSelector(),
            vol.Required(
                OPTION_PROVISIONER_IDENTITY,
                default=options.get(
                    OPTION_PROVISIONER_IDENTITY, DEFAULT_PROVISIONER_IDENTITY
                ),
            ): BooleanSelector(),
            vol.Required(
                OPTION_FOLLOW_APP,
                default=options.get(OPTION_FOLLOW_APP, DEFAULT_FOLLOW_APP),
            ): BooleanSelector(),
            vol.Required(
                OPTION_GATEWAY_CHECK,
                default=options.get(OPTION_GATEWAY_CHECK, DEFAULT_GATEWAY_CHECK),
            ): BooleanSelector(),
            vol.Required(
                OPTION_SYNC_AREAS,
                default=options.get(OPTION_SYNC_AREAS, DEFAULT_SYNC_AREAS),
            ): BooleanSelector(),
        }
    )


def _areas_schema(
    hass: HomeAssistant, rooms: list[str], options: Mapping[str, Any]
) -> vol.Schema:
    """Return the `areas` form: whether to assign areas, then one area per room, prefilled by `areas.mapped_area`.

    A suggested value rather than a default: a field the user empties must stay empty (an area named after the
    room), and a default would fill it in again.
    """
    fields: dict[Any, Any] = {
        vol.Required(
            OPTION_ASSIGN_AREAS,
            default=bool(options.get(OPTION_ASSIGN_AREAS, DEFAULT_ASSIGN_AREAS)),
        ): BooleanSelector()
    }
    for room in rooms:
        suggested = mapped_area(hass, options, room)
        fields[vol.Optional(room, description={"suggested_value": suggested})] = (
            AreaSelector()
        )
    return vol.Schema(fields)


def area_choice(rooms: list[str], user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Return the options the `areas` form sets: the switch, and each room's area (None: one named after the room)."""
    return {
        OPTION_ASSIGN_AREAS: bool(
            user_input.get(OPTION_ASSIGN_AREAS, DEFAULT_ASSIGN_AREAS)
        ),
        CONF_ROOM_AREAS: {room: user_input.get(room) or None for room in rooms},
    }


def async_apply_area_choice(
    hass: HomeAssistant, entry: ConfigEntry, choice: Mapping[str, Any]
) -> int:
    """Move the devices of a loaded `entry` to the areas `choice` gives their rooms; how many moved (`areas.py`).

    Only a device without an area, or in the one the entry's options gave it, moves. Moved before the options are
    stored, against the running hub's model; an entry that is not loaded has nothing to compare (0).
    """
    if entry.state is not ConfigEntryState.LOADED:
        return 0
    rooms = {
        ident: (room, room) for ident, room in device_rooms(entry.runtime_data).items()
    }
    return async_move_devices(
        hass, entry.entry_id, rooms, entry.options, {**entry.options, **choice}
    )


NONE = "*none*"  # an empty list in the import summary


def _entity_lines(entities: list[er.RegistryEntry]) -> str:
    """Render a markdown list of entity ids for the import summary."""
    return "\n".join(f"- `{e.entity_id}`" for e in entities) or NONE


def import_placeholders(plan: ImportPlan) -> dict[str, str]:
    """Render every list of the plan as markdown for the import form and its result."""
    return {
        "count": str(len(plan.matches)),
        "matched": "\n".join(
            f"- `{m.theirs.entity_id}` → `{m.ours.entity_id}`" for m in plan.matches
        )
        or NONE,
        "customised": _entity_lines([m.ours for m in plan.customised]),
        "unmatched": _entity_lines(plan.unmatched),
        "gateway_only": _entity_lines(plan.gateway_only),
    }


def _gateway_error_key(err: GatewayError) -> str:
    if isinstance(err, GatewayAuthError):
        return "token_rejected"
    if isinstance(err, GatewayNotApproved):
        return "not_approved"
    if isinstance(err, GatewayNoProject):
        return "no_project"
    if isinstance(err, GatewayUnreachable):
        return "cannot_connect"
    if isinstance(err, GatewayBusy):
        return "gateway_busy"
    return "gateway_error"


# ---- blocking file helpers (executor)


_write_private = write_private  # the export store's writer (owner-only file)


def _copy_upload(hass: HomeAssistant, file_id: str, dest: Path) -> None:
    """Move an uploaded file (deleted by Home Assistant once the context closes) into our store."""
    with process_uploaded_file(hass, file_id) as src:
        _write_private(dest, src.read_bytes())


def _discard(path: Path) -> None:
    path.unlink(missing_ok=True)


def infer_source(hass: HomeAssistant, data: Mapping[str, Any]) -> str:
    """Where an entry from before `CONF_SOURCE` got its export, judged the way the flows would have set it.

    Ours is only a file the flows wrote: `<MESH UUID>.json` in our storage folder (`_export_path`), of the entry's
    own mesh when it records one. Anything else is the user's own path — even in that folder (a file named after
    the domain, kept there before the gateway and upload flows existed), and even when the entry still carries
    gateway credentials from before a reconfigure: removing the entry would delete it, and the gateway refresh
    would overwrite it in place. Of ours, one with gateway credentials was fetched from the gateway, else it was
    uploaded.
    """
    path = Path(data[CONF_CDB_PATH])
    mesh_uuid = str(data.get(CONF_MESH_UUID) or "")
    ours = (
        path.parent == Path(hass.config.path(STORAGE_DIR))
        and UUID_PATTERN.fullmatch(path.stem) is not None
        and path.stem == path.stem.upper()
        and (not mesh_uuid or path.stem == mesh_uuid.upper())
    )
    if not ours:
        return SOURCE_PATH
    if (
        data.get(CONF_GATEWAY_HOST)
        and data.get(CONF_GATEWAY_TOKEN)
        and data.get(CONF_GATEWAY_FINGERPRINT)
    ):
        return SOURCE_GATEWAY
    return SOURCE_UPLOAD


def forget_stored_export(path: Path) -> None:
    """Blocking: delete an export the integration stored itself, its backups and the copies the services keep."""
    for stored in (
        path,
        *backup_paths(path),
        app_copy_path(path),
        pre_adopt_path(path),
        pre_reconfigure_path(path),
    ):
        stored.unlink(missing_ok=True)


def pre_reconfigure_path(path: Path) -> Path:
    """Where a reconfigure keeps the export it replaced (`<export>.pre-reconfigure`), until the next one."""
    return path.with_name(path.name + ".pre-reconfigure")


def _replace_keeping(incoming: Path, final: Path) -> None:
    """Blocking: move `incoming` to `final`, the export there kept as `pre_reconfigure_path`, the directory fsynced.

    Review-4 S4-6: fetching the gateway's export again was the way out of a gateway and a file that had both
    changed, and it replaced the file without a copy — with the rooms, scenes and connections Home Assistant had
    made that the devices still use. The copy is owner-only like the export (it holds every mesh key).
    """
    if final.exists():
        copy_private(final, pre_reconfigure_path(final), PRIVATE_MODE)
    incoming.replace(final)
    fsync_dir(final.parent)


def proxy_in_range(hass: HomeAssistant, cdb: CDB) -> bool:
    """Whether a proxy node of `cdb`'s mesh is advertising: a Network ID, or a Node Identity the NetKey resolves.

    The hub connects to either kind (`ProxyClient.classify_service_data`), so setup must accept either: a node
    right after provisioning, or with the app's identify on, advertises Node Identity only (Mesh Profile §7.2.2.2.3).
    Mid key refresh a proxy advertises with either key (`CDB.rx_net_keys`). A proxy with Mesh Protocol 1.1 Proxy
    Privacy on advertises the private forms of both instead (`classify_proxy_advert`; unverified on air).
    """
    keys = cdb.rx_net_keys(0)
    unicasts = [n.unicast for n in cdb.nodes]
    return any(
        classify_proxy_advert(
            bytes(info.service_data.get(MESH_PROXY_SERVICE, b"")), keys, unicasts
        )
        is not None
        for info in bluetooth.async_discovered_service_info(hass, connectable=True)
    )


def mesh_proxies_without_match(hass: HomeAssistant, cdb: CDB) -> bool:
    """Whether a node of `cdb` advertises as a proxy under a Network ID none of the export's keys derive (H I-6).

    JUNG nodes advertise from their public MAC, which the export holds in the node UUID (`node_macs`): a node of
    the export advertising another Network ID is this mesh after a key refresh the export does not have — the
    export is stale, not out of range. A proxy of another mesh (a neighbour's) from an address the export lacks
    says nothing either way, and neither does a Node Identity advertisement. Unverified on air (no key refresh on
    this installation).
    """
    macs = node_macs(cdb)
    ids = {nk.network_id for nk in cdb.rx_net_keys(0)}
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        sd = info.service_data.get(MESH_PROXY_SERVICE)
        if (
            sd
            and sd[0] == 0x00
            and len(sd) >= 9
            and bytes(sd[1:9]) not in ids
            and info.address.upper() in macs
        ):
            return True
    return False


async def async_known_mesh_of(
    hass: HomeAssistant, entry: ConfigEntry
) -> KnownMesh | None:
    """Return what discovery knows `entry`'s mesh by (`coordinator.KNOWN_MESHES`); None when its export is unreadable.

    Every setup records it; an entry that never got that far (disabled, or its export failed to load) has its
    export loaded here once and kept (H4-4: discovery runs once per proxy, there are dozens). An unreadable export
    is not kept, so a file fixed on disk counts at the next discovery.
    """
    cache = hass.data.setdefault(KNOWN_MESHES, {})
    if entry.entry_id not in cache:
        try:
            cdb = await hass.async_add_executor_job(
                CDB.load, Path(entry.data[CONF_CDB_PATH])
            )
        except LOAD_ERRORS:
            return None
        cache[entry.entry_id] = await async_known_mesh(
            hass, cdb, int(entry.data[CONF_UNICAST], 16)
        )
    return cache[entry.entry_id]


async def _own_uuid(hass: HomeAssistant, cdb: CDB, unicast: int) -> str | None:
    """Home Assistant's provisioner UUID for the address check: the vault's, or its entry the export still has.

    Nothing is written here (the setup takes a recognised entry back into the vault, `VaultKeeper.async_recover`).
    """
    keeper = await async_vault_keeper(hass, cdb.mesh_uuid)
    found = recognise(cdb, unicast)
    if found is not None and (
        keeper.vault is None or cdb.own_provisioner(keeper.vault.uuid) is None
    ):
        return found.uuid
    return keeper.own_uuid


async def validate_input(
    hass: HomeAssistant, data: dict[str, Any]
) -> tuple[CDB | None, dict[str, str]]:
    """Load the export and check address + a visible proxy. Returns (cdb, errors)."""
    errors: dict[str, str] = {}
    try:
        cdb = await hass.async_add_executor_job(CDB.load, Path(data[CONF_CDB_PATH]))
    except InvalidExport:
        return None, {"base": "invalid_export"}  # JSON, but not a usable mesh export
    except LOAD_ERRORS:
        return None, {"base": "cannot_load"}
    meta = data.get(CONF_METADATA_DIR) or ""
    if meta and await hass.async_add_executor_job(Path(meta).is_dir):
        try:  # read the app-container files now: a malformed one fails the flow, not the first setup
            await hass.async_add_executor_job(
                Metadata,
                Path(meta) / "device_metadata.json",
                Path(meta) / "scene_metadata.json",
            )
        except InvalidMetadata as err:
            _LOGGER.warning("Metadata not usable: %s", err)
            errors[CONF_METADATA_DIR] = "invalid_metadata"
    elif meta:
        errors[CONF_METADATA_DIR] = "not_a_directory"
    if (unicast := _parse_unicast(data[CONF_UNICAST])) is None:
        errors[CONF_UNICAST] = "invalid_address"
    elif not cdb.unicast_is_free(unicast, own=await _own_uuid(hass, cdb, unicast)):
        errors[CONF_UNICAST] = "address_in_use"
    else:
        # a key refresh the hub followed to its end since the export was made, as the setup applies it: the
        # proxies advertise its new key
        await async_apply_followed_key_refresh(hass, cdb, unicast)
    if not errors and not proxy_in_range(hass, cdb):
        errors["base"] = (
            "export_keys_stale"
            if mesh_proxies_without_match(hass, cdb)
            else "no_proxy_visible"
        )
    return cdb, errors


def mesh_unique_id(mesh_uuid: str) -> str:
    """Return the unique id of an entry of the mesh `mesh_uuid`: the UUID in lower case with its dashes (M10, H I-9).

    The export's own form but for the case (`meshUUID`, which the app writes in upper case); the entities' unique
    ids use the same. A key refresh does not change it, unlike the Network ID the entries used before.
    """
    return mesh_uuid.lower()


async def async_migrate_unique_id(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Make `entry`'s unique id its mesh UUID (`mesh_unique_id`, entry version 1.3); False to try again next start.

    The mesh UUID is the one the entry recorded, else its export's: an export that cannot be read leaves the entry as
    it is, and its setup reports the export. Another entry of the same mesh (which should not exist: they would
    share the sequence numbers) leaves both their unique ids as they are, said once in the log. Never raises: an
    entry is set up whatever this finds. Unverified on air (the migration of the installation's entry).
    """
    mesh_uuid = await configured_mesh_uuid(hass, entry)
    if mesh_uuid is None:
        _LOGGER.info(
            "%s keeps its unique id until its export can be read: %s",
            entry.title,
            entry.data.get(CONF_CDB_PATH),
        )
        return False
    unique_id = mesh_unique_id(mesh_uuid)
    if entry.unique_id == unique_id:
        return True
    holder = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
    if holder is not None or await _mesh_uuid_taken(
        hass, mesh_uuid, exclude=entry.entry_id
    ):
        _LOGGER.warning(
            "%s keeps its unique id: another entry belongs to the same mesh. Remove one of them",
            entry.title,
        )
        return True
    hass.config_entries.async_update_entry(entry, unique_id=unique_id)
    return True


async def configured_mesh_uuid(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """Return the mesh UUID an entry belongs to.

    Recorded in its data, or — for entries created before it was recorded — taken from the export the entry
    currently uses. None when that export cannot be read.
    """
    if (recorded := entry.data.get(CONF_MESH_UUID)) is not None:
        return str(recorded)
    try:
        cdb = await hass.async_add_executor_job(
            CDB.load, Path(entry.data[CONF_CDB_PATH])
        )
    except LOAD_ERRORS:
        return None
    return cdb.mesh_uuid


# ---- a new export for an entry: the reconfigure's, and the repairs' that ask for one (`repairs.NewExportFlow`)


def incoming_path(hass: HomeAssistant, flow_id: str) -> Path:
    """Where the flow `flow_id` keeps a fetched or uploaded export until it passes (`.incoming-<flow id>.json`)."""
    return Path(hass.config.path(STORAGE_DIR, f".incoming-{flow_id}.json"))


def export_path(hass: HomeAssistant, mesh_uuid: str) -> Path:
    """Where a fetched or uploaded export of the mesh `mesh_uuid` lives: `<config>/junghome_ble/<MESH UUID>.json`."""
    # `CDB.from_network` accepts a mesh UUID only in canonical form: the file name never holds anything else
    assert UUID_PATTERN.fullmatch(mesh_uuid)
    return Path(hass.config.path(STORAGE_DIR, f"{mesh_uuid.upper()}.json"))


async def async_take_upload(hass: HomeAssistant, file_id: str, incoming: Path) -> None:
    """Move the uploaded file `file_id` to `incoming` in our store, before anything that can fail checks it.

    The upload holds every key of the mesh: out of Home Assistant's upload folder first, whatever comes next. A copy
    that failed half-way is deleted (`upload_failed`).
    """
    try:
        await hass.async_add_executor_job(_copy_upload, hass, file_id, incoming)
    except (OSError, ValueError) as err:
        await hass.async_add_executor_job(_discard, incoming)
        raise _FormError({"base": "upload_failed"}) from err


async def async_validate_stored(
    hass: HomeAssistant, incoming: Path, unicast: str
) -> CDB:
    """Validate a fetched or uploaded export in our store (`validate_input`); deleted again unless it passes."""
    try:
        cdb, errors = await validate_input(
            hass,
            {
                CONF_CDB_PATH: str(incoming),
                CONF_METADATA_DIR: "",
                CONF_UNICAST: unicast,
            },
        )
        if cdb is None or errors:
            raise _FormError(errors)
    except BaseException:
        await hass.async_add_executor_job(_discard, incoming)
        raise
    return cdb


async def async_fetch_to_store(
    hass: HomeAssistant, api: JungHomeGatewayApi, incoming: Path, unicast: str
) -> tuple[dict[str, Any], CDB]:
    """Fetch the gateway's export with the token `api` holds into `incoming` and validate it; (document, network).

    The gateway's errors pass through (the caller decides: a new access request, the certificate step, an abort);
    a file that cannot be stored (`cannot_store`) or does not validate is deleted again.
    """
    doc = await api.fetch_project()
    try:
        await hass.async_add_executor_job(
            _write_private, incoming, json.dumps(doc).encode()
        )
    except OSError as err:
        await hass.async_add_executor_job(_discard, incoming)
        raise _FormError({"base": "cannot_store"}) from err
    return doc, await async_validate_stored(hass, incoming, unicast)


def gateway_entry_data(
    api: JungHomeGatewayApi, unicast: str, pin_source: str, doc: dict[str, Any]
) -> dict[str, Any]:
    """Return the entry data of an export fetched with `api` (host, token, pin), as the gateway holds it (`doc`)."""
    return {
        CONF_SOURCE: SOURCE_GATEWAY,
        CONF_METADATA_DIR: "",
        CONF_UNICAST: unicast,
        CONF_GATEWAY_HOST: api.host,
        CONF_GATEWAY_TOKEN: api.token,
        CONF_GATEWAY_FINGERPRINT: api.fingerprint,
        CONF_GATEWAY_PIN_SOURCE: pin_source,
        CONF_GATEWAY_SYNCED: export_digest(doc),
    }


async def async_known_pin(
    hass: HomeAssistant, entry: ConfigEntry | None, host: str
) -> tuple[str, str, str] | None:
    """(fingerprint, pin source, where it came from) of the certificate `host` must present; None when none is known.

    In order of trust: what the gateway node reports over the mesh (`entry`'s hub is connected; the mesh is
    authenticated with the AppKey), then what `entry` recorded at its last fetch from this host — with the source
    it recorded, so one the mesh vouched for stays so. Nothing is sent to `host`.
    """
    if entry is None:
        return None
    if entry.state is ConfigEntryState.LOADED and (
        fingerprint := await async_read_mesh_fingerprint(entry.runtime_data)
    ):
        return fingerprint, PIN_FROM_MESH, "the gateway node over the mesh"
    if entry.data.get(CONF_GATEWAY_HOST) == host and (
        fingerprint := normalize_fingerprint(entry.data.get(CONF_GATEWAY_FINGERPRINT))
    ):
        source = str(entry.data.get(CONF_GATEWAY_PIN_SOURCE) or PIN_FROM_USER)
        return fingerprint, source, "the entry"
    return None


async def _mesh_uuid_taken(
    hass: HomeAssistant, mesh_uuid: str, *, exclude: str | None = None
) -> bool:
    """Return True when another entry already belongs to this mesh (by mesh UUID, which survives a key refresh).

    The unique id is the mesh UUID too (decision M10), but an entry whose export could not be read at its migration
    still holds the Network ID it had before: without this check, fetching or uploading the same mesh's export
    through "Add integration" instead of the existing entry's Reconfigure would create a second entry for it. Two
    entries sharing a mesh would then also share its sequence-number store (`seq_store.seq_store`, keyed on the
    mesh UUID) — each holding its own stale in-memory copy of the other addresses' records (`HAState._addresses`,
    loaded once) and overwriting them with that stale copy on every save, which can roll a sibling entry's counter
    backwards on its next load. One entry per mesh avoids the race outright; point the user at Reconfigure instead.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        if (
            entry.entry_id == exclude
        ):  # the entry being reconfigured is not "another" one
            continue
        known = await configured_mesh_uuid(hass, entry)
        if known is not None and known.lower() == mesh_uuid.lower():
            return True
    return False


async def _async_keep_incoming(
    hass: HomeAssistant, incoming: Path, data: dict[str, Any], cdb: CDB
) -> None:
    """Give a validated fetched/uploaded export its final name and point the entry data at it.

    The export it replaces, if any, is kept beside it (`pre_reconfigure_path`).
    """
    final = export_path(hass, cdb.mesh_uuid)
    await hass.async_add_executor_job(_replace_keeping, incoming, final)
    data[CONF_CDB_PATH] = str(final)


async def _async_forget_replaced_export(
    hass: HomeAssistant, entry: ConfigEntry, data: dict[str, Any]
) -> None:
    """Delete the export the integration had stored for `entry` when the reconfigure moved away from it.

    A fetched or uploaded export has just replaced whatever was at its path: the merge base kept beside it
    (`app_copy_path`, the app's upload before this one) is stale, and `ExportStore._carry_over` would
    take all the app changed since for Home Assistant's own changes. It goes too; the next save keeps the
    export now on disk as the base (`ExportStore._keep_app_copy`).
    """
    if data.get(CONF_SOURCE) in (SOURCE_GATEWAY, SOURCE_UPLOAD):
        await hass.async_add_executor_job(_discard, app_copy_path(data[CONF_CDB_PATH]))
    if entry.data.get(CONF_SOURCE) not in (SOURCE_GATEWAY, SOURCE_UPLOAD):
        return
    old = Path(entry.data[CONF_CDB_PATH])
    if old == Path(data[CONF_CDB_PATH]):
        return  # the same mesh fetched or uploaded again: replaced in place
    await hass.async_add_executor_job(forget_stored_export, old)


@callback
def async_update_and_reload(
    hass: HomeAssistant,
    entry: ConfigEntry,
    updated: dict[str, Any],
    unique_id: str | None = None,
    options: dict[str, Any] | None = None,
) -> None:
    """Store `updated` as `entry`'s data (and `options` as its options, when given); set the entry up again, once.

    Home Assistant wants the update listener to reload: a loaded entry's listener (`__init__._async_entry_updated`)
    starts eagerly from inside `async_update_entry` and reloads the entry when the data the hub is built from
    changed, so decide *before* the update whether it will. An entry that failed to set up has no listener, and an
    export fetched or uploaded again lands in the same file, so the data may be unchanged although the export (a
    key refresh) is new: those are reloaded here.
    """
    loaded = entry.state is ConfigEntryState.LOADED
    listener_reloads = loaded and (
        hub_data(entry.data) != hub_data(updated)
        or (options is not None and options != dict(entry.options))
    )
    changes: dict[str, Any] = {"data": updated}
    if unique_id is not None:
        changes["unique_id"] = unique_id
    if options is not None:
        changes["options"] = options
    hass.config_entries.async_update_entry(entry, **changes)
    if not listener_reloads:
        hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_export_refusal(
    hass: HomeAssistant, entry: ConfigEntry, cdb: CDB
) -> str | None:
    """Return why `entry` cannot take the export `cdb` (`async_replace_export`'s refusals); None when it can."""
    known = await configured_mesh_uuid(hass, entry)
    if known is not None and known.lower() != cdb.mesh_uuid.lower():
        return "network_mismatch"
    if known is None and await _mesh_uuid_taken(
        hass, cdb.mesh_uuid, exclude=entry.entry_id
    ):
        return "mesh_already_configured"
    return None


async def async_replace_export(
    hass: HomeAssistant,
    entry: ConfigEntry,
    data: dict[str, Any],
    cdb: CDB,
    incoming: Path | None,
    *,
    keep_flow: str | None = None,
    options: dict[str, Any] | None = None,
) -> str:
    """Point the existing `entry` at a new, validated export of its mesh and set it up again; the abort reason.

    `reconfigure_successful`, or the refusal: `network_mismatch` (another mesh than the entry's) and
    `mesh_already_configured` (an entry whose own mesh is unknown — a legacy entry with its export gone — could
    otherwise take over a mesh another entry owns, and two entries on one mesh share its sequence-number store).
    A fetched or uploaded file (`incoming`) is moved to its final name, or deleted whatever stops this. `keep_flow`
    is the reconfigure flow asking, which must not abort itself; `options` the entry's options from then on (the
    `areas` step's), None to keep them. With `OPTION_SYNC_AREAS` on, the devices' rooms are noted first: the setup
    from the new export moves those whose room changed (`model_update.async_sync_areas`).
    """
    try:
        network_id = cdb.net_keys[0].network_id
        data[CONF_MESH_UUID] = cdb.mesh_uuid
        if (refusal := await async_export_refusal(hass, entry, cdb)) is not None:
            return refusal
        # after a key refresh the proxies already advertise the new Network ID: drop the discovery flow it
        # started, and an ignored entry holding it (H4-4)
        await async_release_network_id(
            hass, entry, network_id.hex(), keep_flow=keep_flow
        )
        if incoming is not None:
            await _async_keep_incoming(hass, incoming, data, cdb)
            incoming = None
        # discovery recognises the mesh by the new export from its next setup on
        forget_known_mesh(hass, entry.entry_id)
        await _async_forget_replaced_export(hass, entry, data)
        if CONF_GATEWAY_SYNCED in data:
            # fetched from the gateway: both hold this export now (`configurator.store.GatewaySync`, before the reload)
            await gateway_sync(hass, entry.entry_id).async_seed(
                entry, data[CONF_GATEWAY_SYNCED]
            )
        # the gateway repairs are about the entry's old pin and token; the reload checks the new ones; and the new
        # export is what `app_changed` asked for (`app_follow.py`)
        for issue in (
            certificate_issue_id(entry.entry_id),
            issue_id(entry, ISSUE_GATEWAY_TOKEN),
            issue_id(entry, ISSUE_APP_CHANGED),
        ):
            ir.async_delete_issue(hass, DOMAIN, issue)
        remember_device_rooms(hass, entry)
        # an entry its migration could not reach (its export unreadable) takes the mesh UUID now; one whose mesh
        # another entry holds keeps its own
        unique_id = mesh_unique_id(cdb.mesh_uuid)
        holder = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
        async_update_and_reload(
            hass,
            entry,
            {**entry.data, **data},
            unique_id=unique_id if holder in (None, entry) else None,
            options=options,
        )
        return "reconfigure_successful"
    finally:
        if incoming is not None:
            await hass.async_add_executor_job(_discard, incoming)


class JungHomeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a mesh from its app export, started by the user or by Bluetooth discovery of a proxy."""

    VERSION = 1
    # 1.2: every entry names its `CONF_SOURCE`; 1.3: the unique id is the mesh UUID (`__init__.async_migrate_entry`)
    MINOR_VERSION = 3

    def __init__(self) -> None:
        """Start with no discovered network, no gateway and no file in flight."""
        self._discovered_network_id: bytes | None = None
        # the address the gateway announced over mDNS: the gateway form's default, nothing more
        self._discovered_host: str | None = None
        self._host: str | None = None
        self._token: str | None = None
        # SHA-256 of the certificate every request to `_host` is pinned to; None until `_async_pin` decided it
        self._fingerprint: str | None = None
        # where that pin came from (`CONF_GATEWAY_PIN_SOURCE`): one the gateway node vouched for is not overridden
        self._pin_source: str = PIN_FROM_USER
        # the certificate a responder presented instead, and the step (with its input) to resume once trusted
        self._observed: str | None = None
        self._resume: tuple[str, dict[str, Any] | None] = (SOURCE_GATEWAY, None)
        # what the gateway form held, kept across the approval wait
        self._unicast: str = DEFAULT_UNICAST
        self._gateway_input: dict[str, Any] | None = (
            None  # host + address, never the password
        )
        self._register_task: asyncio.Task[str] | None = None
        # errors for the gateway form, left by the progress step (which cannot show a form itself)
        self._pending_errors: dict[str, str] = {}
        # a fetched/uploaded export in our store that is not yet renamed to its final name
        self._incoming: Path | None = None
        # a validated export waiting for the `areas` step (entry data, its CDB), and what that step chose
        self._pending: tuple[dict[str, Any], CDB] | None = None
        self._area_choice: dict[str, Any] | None = None

    @callback
    def async_remove(self) -> None:
        """Delete a half-validated export when the flow goes away (finished, aborted or expired)."""
        if self._incoming is not None:
            self.hass.async_add_executor_job(_discard, self._incoming)
            self._incoming = None

    # ------------------------------------------------------------------ entry points

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle an advertising JUNG Mesh Proxy: nothing for a configured mesh, else ask for the export.

        Review-4 H I-7: the manifest matches a Mesh Proxy with JUNG's manufacturer data (0x0527) only; an
        advertisement without it that gets here anyway aborts `not_jung`. A configured mesh is recognised by its keys
        and nodes (`coordinator.KnownMesh`: the Network ID or a Node Identity of any configured entry's keys, the
        followed key refresh's included, or a node MAC of its export), never by the entry's unique id — the mesh
        UUID, which no advertisement carries (H I-9). Such a discovery aborts, and an entry waiting for a proxy
        (`SETUP_RETRY`) is retried at once, as Home Assistant does for a unique-id match. Another mesh's Network ID
        is the flow's unique id until the export is given: one card per mesh, and an ignored one stays ignored.
        Unverified on air (no card for the configured mesh).
        """
        if JUNG_COMPANY_ID not in discovery_info.manufacturer_data:
            return self.async_abort(reason="not_jung")
        sd = bytes(discovery_info.service_data.get(MESH_PROXY_SERVICE, b""))
        for entry in self._async_current_entries(include_ignore=False):
            known = await async_known_mesh_of(self.hass, entry)
            if known is not None and known.recognises(sd, discovery_info.address):
                if entry.state is ConfigEntryState.SETUP_RETRY:
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(reason="already_configured")
        if not sd or sd[0] != 0x00 or len(sd) < 9:
            return self.async_abort(reason="not_supported")
        self._discovered_network_id = bytes(sd[1:9])
        await self.async_set_unique_id(self._discovered_network_id.hex())
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {
            "name": f"Bluetooth Mesh {self._discovered_network_id.hex()}"
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the discovered network before asking for the export: a neighbour's JUNG mesh is offered too."""
        if user_input is not None:
            return await self.async_step_user()
        assert self._discovered_network_id is not None
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"network_id": self._discovered_network_id.hex()},
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle the JUNG HOME Gateway's mDNS announcement: one flow per gateway serial, none for a configured one.

        The manifest matches the service type `_junghome._tcp.local.`; a TXT `manufacturer` other than JUNG, or no
        `serial`, is not the gateway. A gateway an entry already names by this address (or the announced host name)
        aborts. Nothing is sent to the address: it only prefills the gateway form, and the certificate is pinned
        there as for a typed address (`_async_pin`). The entry's own unique id is the mesh UUID, set when the export
        is loaded (`_async_finish_checked`); this flow's `gateway-<serial>` only collapses the announcements.
        """
        properties = discovery_info.properties
        manufacturer = properties.get("manufacturer")
        serial = str(properties.get("serial") or "").strip().lower()
        if (
            manufacturer is not None
            and str(manufacturer).strip().upper() != GATEWAY_MANUFACTURER
        ) or not serial:
            return self.async_abort(reason="not_junghome_gateway")
        if discovery_info.ip_address.version != 4:
            return self.async_abort(reason="not_ipv4_address")
        await self.async_set_unique_id(f"gateway-{serial}")
        self._abort_if_unique_id_configured()
        host = discovery_info.host
        names = {host, discovery_info.hostname.rstrip(".").lower()}
        for entry in self._async_current_entries(include_ignore=False):
            if _normalize_host(str(entry.data.get(CONF_GATEWAY_HOST) or "")) in names:
                return self.async_abort(reason="already_configured")
        self._discovered_host = host
        self.context["title_placeholders"] = {
            "name": f"JUNG HOME Gateway {host}",
            "host": host,
        }
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the discovered gateway, then show the gateway form with its address. Unverified on air."""
        if user_input is not None:
            return await self.async_step_gateway()
        assert self._discovered_host is not None
        self._set_confirm_only()
        return self.async_show_form(
            step_id=STEP_ZEROCONF_CONFIRM,
            description_placeholders={"host": self._discovered_host},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose where the export comes from."""
        return self.async_show_menu(
            step_id="user", menu_options=[SOURCE_GATEWAY, SOURCE_UPLOAD, SOURCE_PATH]
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the entry's export with a new one of the same mesh and/or change our address.

        The mesh is identified by its `meshUUID`, not by the Network ID: a NetKey refresh changes the Network
        ID, and re-exporting is exactly how one recovers from a refresh. The entry's unique id is the mesh UUID
        (decision M10); discovery recognises the mesh by the new export's keys from its next setup on.
        """
        entry = self._get_reconfigure_entry()
        options = [SOURCE_GATEWAY, SOURCE_UPLOAD, SOURCE_PATH]
        if entry.data.get(CONF_GATEWAY_TOKEN) and entry.data.get(CONF_GATEWAY_HOST):
            options.insert(0, STEP_REFETCH)
        if self.hass.config_entries.async_entries(GATEWAY_DOMAIN):
            options.append(STEP_IMPORT)
        if entry.state is ConfigEntryState.LOADED and entry.runtime_data.devices.rooms:
            options.append(STEP_AREAS)
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=options,
            description_placeholders={
                "host": str(entry.data.get(CONF_GATEWAY_HOST) or "")
            },
        )

    # ------------------------------------------------------------------ sources

    async def async_step_path(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Use a file already on the Home Assistant host (plus the optional iOS metadata directory)."""
        errors: dict[str, str] = {}
        defaults: Mapping[str, Any] = {}
        unicast = self._default_unicast()
        if user_input is not None:
            defaults, unicast = user_input, _unicast_input(user_input)
            data = {
                CONF_CDB_PATH: user_input[CONF_CDB_PATH],
                CONF_METADATA_DIR: user_input.get(CONF_METADATA_DIR, ""),
                CONF_UNICAST: unicast,
            }
            try:
                cdb = await self._async_validate(data)
                return await self._async_finish(
                    {CONF_SOURCE: SOURCE_PATH, **data, CONF_UNICAST: _hex4(unicast)},
                    cdb,
                )
            except _FormError as err:
                errors = err.errors
        elif self.source == SOURCE_RECONFIGURE:
            defaults = self._get_reconfigure_entry().data
        return self.async_show_form(
            step_id=SOURCE_PATH,
            data_schema=_schema(defaults, unicast),
            errors=_form_errors(errors),
        )

    async def async_step_upload(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take the app's `JungHome.json` uploaded through the browser and keep it in our store."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                # out of Home Assistant's upload folder before anything can fail: the file holds every key
                incoming = self._incoming_path()
                try:
                    await async_take_upload(
                        self.hass, user_input[CONF_EXPORT_FILE], incoming
                    )
                finally:
                    self._incoming = incoming  # its removal deletes a copy left behind
                unicast = _unicast_input(user_input)
                if _parse_unicast(unicast) is None:
                    await self._async_discard_incoming()
                    raise _FormError({CONF_UNICAST: "invalid_address"})
                cdb = await self._async_validate_incoming(incoming, unicast)
                return await self._async_finish(
                    {
                        CONF_SOURCE: SOURCE_UPLOAD,
                        CONF_METADATA_DIR: "",
                        CONF_UNICAST: _hex4(unicast),
                    },
                    cdb,
                )
            except _FormError as err:
                errors = err.errors
        unicast = _unicast_input(user_input) if user_input else self._default_unicast()
        return self.async_show_form(
            step_id=SOURCE_UPLOAD,
            data_schema=_upload_schema(unicast),
            errors=_form_errors(errors),
        )

    async def async_step_gateway(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fetch the export from the JUNG HOME Gateway.

        A token is obtained either instantly with the gateway's network-key password or, when none is given, by
        approving the access request in the app (the `gateway_register` progress step). The token is kept in
        the entry so Reconfigure can fetch again without asking.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                return await self._async_gateway_submit(user_input)
            except _FormError as err:
                errors = err.errors
            except _CertificateChanged as err:
                return self._async_show_certificate(err.mismatch)
        elif self._pending_errors:
            errors, self._pending_errors = self._pending_errors, {}
        return self._show_gateway_form(user_input, errors)

    async def async_step_gateway_register(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait (up to the gateway's 180 s) for the access request to be approved in the app.

        Approved, a setup or reconfigure fetches the export next and a reauth stores the token; anything else goes
        back to the form that asked (the gateway form, or the reauth form), with the error or a certificate to vouch
        for first.
        """
        if self._register_task is None:
            self._register_task = self.hass.async_create_task(
                self._async_register(), eager_start=False
            )
        if not self._register_task.done():
            return self.async_show_progress(
                step_id=STEP_REGISTER,
                progress_action="waiting_for_approval",
                progress_task=self._register_task,
                description_placeholders={
                    "host": self._host or "",
                    "user_name": GATEWAY_USER_NAME,
                },
            )
        task, self._register_task = self._register_task, None
        form, form_input, done = (
            (STEP_REAUTH, {}, STEP_REAUTH_DONE)  # {}: no password, request access again
            if self.source == SOURCE_REAUTH
            else (SOURCE_GATEWAY, self._gateway_input, STEP_FETCH)
        )
        try:
            self._token = task.result()
        except GatewayCertificateMismatch as err:
            # the certificate changed between the pin and the request: ask before requesting access again
            self._prepare_certificate_step(err, (form, form_input))
            return self.async_show_progress_done(next_step_id=STEP_CERTIFICATE)
        except GatewayError as err:
            self._pending_errors = {"base": _gateway_error_key(err)}
            return self.async_show_progress_done(next_step_id=form)
        return self.async_show_progress_done(next_step_id=done)

    async def async_step_gateway_fetch(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """After approval: fetch, validate and finish; problems go back to the gateway form."""
        self._resume = (STEP_FETCH, None)
        try:
            return await self._async_fetch_and_finish()
        except _FormError as err:
            return self._show_gateway_form(None, err.errors)
        except _CertificateChanged as err:
            return self._async_show_certificate(err.mismatch)

    async def async_step_gateway_certificate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show both certificates when the gateway presents another than the pinned one; continue on confirmation.

        Nothing was sent to that responder. Confirming pins the certificate it presented and resumes the step that
        was refused (the gateway form with what it held, the re-fetch, or the fetch after approval), so the
        password, the access request or the token go out only once the user has vouched for the new certificate.
        A pin the gateway node vouched for over the mesh is not overridden (`_certificate_form`).
        """
        assert self._host is not None
        assert self._observed is not None
        if user_input is None:
            return self._certificate_form(
                self._host, self._observed, self._fingerprint or ""
            )
        self._fingerprint, self._observed = self._observed, None
        self._pin_source = PIN_FROM_USER
        step, data = self._resume
        handler = getattr(self, f"async_step_{step}")
        result: ConfigFlowResult = await handler(data)
        return result

    async def async_step_gateway_refetch(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure: fetch the export again from the gateway the entry already knows, with its stored token.

        A token the gateway no longer accepts is replaced through a new access request (app approval).
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                unicast = _unicast_input(user_input)
                if _parse_unicast(unicast) is None:
                    raise _FormError({CONF_UNICAST: "invalid_address"})
                self._unicast = _hex4(unicast)
                self._host = str(entry.data[CONF_GATEWAY_HOST])
                self._token = str(entry.data[CONF_GATEWAY_TOKEN])
                self._gateway_input = self._gateway_form_input(self._host)
                self._resume = (STEP_REFETCH, user_input)
                await self._async_pin(self._host)
                return await self._async_fetch_and_finish(reregister_on_401=True)
            except _FormError as err:
                errors = err.errors
            except _CertificateChanged as err:
                return self._async_show_certificate(err.mismatch)
        unicast = _unicast_input(user_input) if user_input else self._default_unicast()
        return self.async_show_form(
            step_id=STEP_REFETCH,
            data_schema=vol.Schema(_advanced(unicast)),
            errors=_form_errors(errors),
            description_placeholders={"host": str(entry.data[CONF_GATEWAY_HOST])},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Obtain a new token: the gateway rejected the entry's (`MeshConfigurator.report_token_rejected`).

        Started without `ConfigEntryAuthFailed`: only the gateway sync needs the token, the mesh keeps working
        meanwhile. Only the token (and a certificate the user vouched for on the way) changes.
        """
        self._host = str(entry_data[CONF_GATEWAY_HOST])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the gateway's network-key password; left empty, approve a new access request in the app instead."""
        assert self._host is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                return await self._async_reauth_submit(user_input)
            except _FormError as err:
                errors = err.errors
            except _CertificateChanged as err:
                return self._async_show_certificate(err.mismatch)
        elif self._pending_errors:
            errors, self._pending_errors = self._pending_errors, {}
        return self.async_show_form(
            step_id=STEP_REAUTH,
            data_schema=_reauth_schema(),
            errors=errors,
            description_placeholders={
                "host": self._host,
                "title": self._get_reauth_entry().title,
                "user_name": GATEWAY_USER_NAME,
            },
        )

    async def async_step_reauth_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """After the approval in the app: store the new token."""
        return self._async_reauth_finish()

    async def async_step_import_gateway(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure: take over the JUNG HOME Gateway integration's entities (see `migration.py`).

        First a dry run: the plan is shown, nothing is touched. Submitting applies it (the plan is computed again
        from the registries at that moment), leaves the gateway entry disabled and reports what moved.
        """
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            try:
                plan = await async_apply_import(self.hass, entry)
            except ImportAborted as err:
                return self.async_abort(
                    reason="import_unload_failed",
                    description_placeholders={"title": err.title},
                )
            return self.async_abort(
                reason="import_successful",
                description_placeholders=import_placeholders(plan),
            )
        plan = build_import_plan(self.hass, entry)
        if not plan.gateway_entries:
            return self.async_abort(reason="no_gateway_entry")
        if plan.empty:
            return self.async_abort(
                reason="nothing_to_import",
                description_placeholders=import_placeholders(plan),
            )
        return self.async_show_form(
            step_id=STEP_IMPORT,
            data_schema=vol.Schema({}),
            description_placeholders=import_placeholders(plan),
        )

    async def async_step_areas(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Map the export's rooms to Home Assistant areas, after the export is loaded or from the reconfigure menu.

        After a load (`_pending`), submitting finishes the setup or the reconfiguration as it would have without
        the step, with the choice as the entry's options; in a reconfiguration the running entry's devices move to
        the new choice first (`async_apply_area_choice`). Without a load it is the reconfigure menu's own option.
        """
        if self._pending is None:
            return await self._async_areas_option(user_input)
        data, cdb = self._pending
        rooms = room_names(cdb)
        entry = self._flow_entry()
        current = dict(entry.options) if entry is not None else {}
        if user_input is None:
            return self.async_show_form(
                step_id=STEP_AREAS, data_schema=_areas_schema(self.hass, rooms, current)
            )
        self._area_choice = area_choice(rooms, user_input)
        if entry is not None:
            async_apply_area_choice(self.hass, entry, self._area_choice)
        try:
            return await self._async_finish(data, cdb)
        except _FormError as err:
            return self.async_abort(reason=err.errors["base"])

    async def _async_areas_option(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Reconfigure menu: change the room to area mapping of the running entry, and move its devices to match.

        Unverified on air: moving devices on the installation's own registry has not been tried.
        """
        entry = self._get_reconfigure_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        rooms = room_names(entry.runtime_data.cdb)
        if user_input is None:
            return self.async_show_form(
                step_id=STEP_AREAS,
                data_schema=_areas_schema(self.hass, rooms, entry.options),
            )
        choice = area_choice(rooms, user_input)
        moved = async_apply_area_choice(self.hass, entry, choice)
        # the update listener reloads the entry, as for every options change
        self.hass.config_entries.async_update_entry(
            entry, options={**entry.options, **choice}
        )
        return self.async_abort(
            reason="areas_updated", description_placeholders={"count": str(moved)}
        )

    # ------------------------------------------------------------------ options

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> JungHomeOptionsFlow:
        """Return the options flow."""
        return JungHomeOptionsFlow()

    # ------------------------------------------------------------------ gateway helpers

    def _api(self) -> JungHomeGatewayApi:
        assert self._host is not None
        assert self._fingerprint is not None
        return JungHomeGatewayApi(
            async_get_clientsession(self.hass, verify_ssl=False),
            self._host,
            self._fingerprint,
            self._token,
        )

    async def _async_register(self) -> str:
        return await self._api().register()

    async def _async_pin(self, host: str) -> None:
        """Decide which certificate `host` must present, before anything is sent to it.

        In order of trust: what the gateway node reports over the mesh (the hub of the entry being reconfigured
        or reauthenticated is connected; the mesh is authenticated with the AppKey), what the entry recorded at its
        last fetch from this host, and — trust on first use — what the host presents now, learned through a
        handshake that sends nothing. A pin already decided in this flow (or confirmed in the certificate step) is
        kept.
        """
        if self._fingerprint is not None:
            return
        known = await async_known_pin(self.hass, self._flow_entry(), host)
        if known is None:
            try:
                learned = await async_learn_fingerprint(
                    async_get_clientsession(self.hass, verify_ssl=False), host
                )
            except (TimeoutError, aiohttp.ClientError) as err:
                raise _FormError({"base": "cannot_connect"}) from err
            known = (learned, PIN_FROM_USER, "first contact")
        fingerprint, self._pin_source, source = known
        _LOGGER.debug(
            "gateway %s: certificate pinned from %s (%s)",
            host,
            source,
            format_fingerprint(fingerprint),
        )
        self._fingerprint = fingerprint

    def _flow_entry(self) -> ConfigEntry | None:
        """Return the entry this flow works on: the one being reconfigured or reauthenticated; None for a new setup."""
        if self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        if self.source == SOURCE_REAUTH:
            return self._get_reauth_entry()
        return None

    def _prepare_certificate_step(
        self,
        mismatch: GatewayCertificateMismatch,
        resume: tuple[str, dict[str, Any] | None],
    ) -> None:
        self._observed = mismatch.observed
        self._resume = resume

    @callback
    def _async_show_certificate(
        self, mismatch: GatewayCertificateMismatch
    ) -> ConfigFlowResult:
        """Route to the certificate step for a mismatch met in the step recorded as `_resume`."""
        self._prepare_certificate_step(mismatch, self._resume)
        return self._certificate_form(
            mismatch.host, mismatch.observed, mismatch.expected
        )

    @callback
    def _certificate_form(
        self, host: str, observed: str, expected: str
    ) -> ConfigFlowResult:
        """Show the certificate step's form, or refuse when the gateway node itself vouched for the pin.

        The mesh is authenticated (AppKey): when the gateway node reports the pinned certificate (in this flow,
        or the entry recorded it so), a responder presenting another one is not the gateway, and one click must
        not hand it the token. A gateway whose certificate was really renewed reports the new one over the mesh:
        the refusal says to reconfigure again while Home Assistant is connected to the mesh.
        """
        placeholders = {
            "host": host,
            "observed": format_fingerprint(observed),
            "expected": format_fingerprint(expected),
        }
        if self._pin_source == PIN_FROM_MESH:
            _LOGGER.warning(
                "gateway %s presents a certificate other than the one its mesh node vouched for; refused",
                host,
            )
            return self.async_abort(
                reason="certificate_vouched_by_mesh",
                description_placeholders=placeholders,
            )
        return self.async_show_form(
            step_id=STEP_CERTIFICATE,
            data_schema=vol.Schema({}),
            description_placeholders=placeholders,
        )

    def _show_gateway_form(
        self, user_input: dict[str, Any] | None, errors: dict[str, str]
    ) -> ConfigFlowResult:
        if user_input:
            host, unicast = user_input[CONF_GATEWAY_HOST], _unicast_input(user_input)
        else:
            host = self._host or self._default_host()
            unicast = self._unicast if self._host else self._default_unicast()
        return self.async_show_form(
            step_id=SOURCE_GATEWAY,
            data_schema=_gateway_schema(host, unicast),
            errors=_form_errors(errors),
            description_placeholders={"user_name": GATEWAY_USER_NAME},
        )

    def _gateway_form_input(self, host: str) -> dict[str, Any]:
        """Return what the gateway form held (host and our address, never the password), to submit it again."""
        return {
            CONF_GATEWAY_HOST: host,
            SECTION_ADVANCED: {CONF_UNICAST: self._unicast},
        }

    async def _async_gateway_submit(
        self, user_input: dict[str, Any]
    ) -> ConfigFlowResult:
        self._pending_errors = {}
        host = _normalize_host(user_input[CONF_GATEWAY_HOST])
        if not host:
            raise _FormError({CONF_GATEWAY_HOST: "invalid_host"})
        unicast = _unicast_input(user_input)
        if _parse_unicast(unicast) is None:
            raise _FormError({CONF_UNICAST: "invalid_address"})
        self._unicast = _hex4(unicast)
        if host != self._host:
            self._host, self._token, self._fingerprint = host, None, None
        self._gateway_input = self._gateway_form_input(host)
        self._resume = (SOURCE_GATEWAY, user_input)
        password = user_input.get(CONF_GATEWAY_PASSWORD) or ""
        reregister_on_401 = False
        if self._token is None and not password and self.source == SOURCE_RECONFIGURE:
            # the same gateway the entry already knows: try its token before asking for a new approval
            entry = self._get_reconfigure_entry()
            if entry.data.get(CONF_GATEWAY_HOST) == host and entry.data.get(
                CONF_GATEWAY_TOKEN
            ):
                self._token = str(entry.data[CONF_GATEWAY_TOKEN])
                reregister_on_401 = True
        await self._async_pin(
            host
        )  # before the password, the access request or the token leaves
        if (self._token is None or password) and (
            progress := await self._async_obtain_token(password)
        ) is not None:
            return progress
        return await self._async_fetch_and_finish(reregister_on_401=reregister_on_401)

    async def _async_obtain_token(self, password: str) -> ConfigFlowResult | None:
        """Get a token from the pinned gateway: by `password` at once (None), else through the approval progress step.

        The gateway is probed first, so an unreachable address or another certificate shows before the password
        or the access request goes out. The password is neither kept nor logged.
        """
        api = self._api()
        try:
            await api.version()
        except GatewayCertificateMismatch as err:
            raise _CertificateChanged(err) from err
        except GatewayUnreachable as err:
            raise _FormError({"base": "cannot_connect"}) from err
        except GatewayError:
            pass  # it answered HTTP: reachable, whatever the firmware says about itself
        if not password:
            return await self.async_step_gateway_register()
        try:
            self._token = await api.register_by_password(password)
        except GatewayCertificateMismatch as err:
            raise _CertificateChanged(err) from err
        except GatewayAuthError as err:
            raise _FormError({CONF_GATEWAY_PASSWORD: "invalid_auth"}) from err
        except GatewayError as err:
            raise _FormError({"base": _gateway_error_key(err)}) from err
        return None

    async def _async_reauth_submit(
        self, user_input: dict[str, Any]
    ) -> ConfigFlowResult:
        """Renew the token of the gateway the entry names, pinned before anything is sent; the export is not fetched."""
        assert self._host is not None
        self._pending_errors = {}
        self._token = None
        self._resume = (STEP_REAUTH, user_input)
        await self._async_pin(self._host)
        progress = await self._async_obtain_token(
            user_input.get(CONF_GATEWAY_PASSWORD) or ""
        )
        return progress if progress is not None else self._async_reauth_finish()

    @callback
    def _async_reauth_finish(self) -> ConfigFlowResult:
        """Store the new token (and the pin it was obtained under) in the entry, clear the repair, end the flow.

        No reload: `hub_data` leaves the gateway out, so the update listener keeps the hub running, and every
        gateway request reads the token from the entry. A pin that changed here (vouched for in the certificate
        step) also clears the certificate repair: the hub compares the new pin with the gateway node's report
        before it uses the gateway (`JungHomeHub.async_gateway_distrust`).
        """
        entry = self._get_reauth_entry()
        assert self._token is not None
        ir.async_delete_issue(self.hass, DOMAIN, issue_id(entry, ISSUE_GATEWAY_TOKEN))
        if self._fingerprint != normalize_fingerprint(
            entry.data.get(CONF_GATEWAY_FINGERPRINT)
        ):
            ir.async_delete_issue(
                self.hass, DOMAIN, certificate_issue_id(entry.entry_id)
            )
        _LOGGER.info("The gateway %s accepts Home Assistant again", self._host)
        return self.async_update_and_abort(
            entry,
            data_updates={
                CONF_GATEWAY_TOKEN: self._token,
                CONF_GATEWAY_FINGERPRINT: self._fingerprint,
                CONF_GATEWAY_PIN_SOURCE: self._pin_source,
            },
        )

    async def _async_fetch_and_finish(
        self, *, reregister_on_401: bool = False
    ) -> ConfigFlowResult:
        """Fetch the export with the token in hand, store and validate it, finish the flow."""
        assert self._host is not None
        api = self._api()
        incoming = self._incoming_path()
        try:
            doc, cdb = await async_fetch_to_store(
                self.hass, api, incoming, self._unicast
            )
        except GatewayCertificateMismatch as err:
            raise _CertificateChanged(err) from err
        except GatewayAuthError as err:
            self._token = None
            if reregister_on_401:
                return await self.async_step_gateway_register()
            raise _FormError({"base": _gateway_error_key(err)}) from err
        except GatewayError as err:
            raise _FormError({"base": _gateway_error_key(err)}) from err
        finally:
            self._incoming = incoming  # its removal deletes a copy left behind
        assert self._token is not None
        return await self._async_finish(
            gateway_entry_data(api, self._unicast, self._pin_source, doc), cdb
        )

    # ------------------------------------------------------------------ shared tail

    def _default_unicast(self) -> str:
        if self.source == SOURCE_RECONFIGURE:
            return str(self._get_reconfigure_entry().data[CONF_UNICAST])
        return DEFAULT_UNICAST

    def _default_host(self) -> str:
        if self._discovered_host is not None:
            return self._discovered_host
        if self.source == SOURCE_RECONFIGURE:
            host = self._get_reconfigure_entry().data.get(CONF_GATEWAY_HOST)
            if host:
                return str(host)
        return GATEWAY_DEFAULT_HOST

    def _incoming_path(self) -> Path:
        return incoming_path(self.hass, self.flow_id)

    async def _async_validate(self, data: dict[str, Any]) -> CDB:
        cdb, errors = await validate_input(self.hass, data)
        if cdb is None or errors:
            raise _FormError(errors)
        return cdb

    async def _async_validate_incoming(self, incoming: Path, unicast: str) -> CDB:
        """Validate a file in our store; it is deleted again unless it passes (whatever went wrong)."""
        self._incoming = incoming  # deleted again unless it passes; the flow's removal sees to a cancellation
        return await async_validate_stored(self.hass, incoming, unicast)

    async def _async_discard_incoming(self) -> None:
        if self._incoming is not None:
            await self.hass.async_add_executor_job(_discard, self._incoming)
            self._incoming = None

    async def _async_keep_incoming(self, data: dict[str, Any], cdb: CDB) -> None:
        """Give a validated fetched/uploaded export of a new entry its final name (`_async_keep_incoming`)."""
        if self._incoming is None:
            return
        await _async_keep_incoming(self.hass, self._incoming, data, cdb)
        self._incoming = None

    async def _async_finish(self, data: dict[str, Any], cdb: CDB) -> ConfigFlowResult:
        """Identity checks, then create the entry or update (and reload) the one being reconfigured.

        A fetched / uploaded file that does not make it to its final name is deleted, whatever stops the flow.
        """
        try:
            return await self._async_finish_checked(data, cdb)
        except BaseException:
            await self._async_discard_incoming()
            raise

    async def _async_finish_checked(
        self, data: dict[str, Any], cdb: CDB
    ) -> ConfigFlowResult:
        network_id = cdb.net_keys[0].network_id
        data[CONF_MESH_UUID] = cdb.mesh_uuid
        if self.source == SOURCE_RECONFIGURE:
            entry = self._get_reconfigure_entry()
            # a refused export is refused at once (by `async_replace_export`), not after the rooms were mapped
            if (
                self._area_choice is None
                and room_names(cdb)
                and await async_export_refusal(self.hass, entry, cdb) is None
            ):
                return await self._async_ask_areas(data, cdb)
            incoming, self._incoming = self._incoming, None
            reason = await async_replace_export(
                self.hass,
                entry,
                data,
                cdb,
                incoming,
                keep_flow=self.flow_id,
                options=(
                    None
                    if self._area_choice is None
                    else {**entry.options, **self._area_choice}
                ),
            )
            return self.async_abort(reason=reason)
        if self._discovered_network_id and network_id != self._discovered_network_id:
            raise _FormError({"base": "network_mismatch"})
        # the mesh UUID from here on (decision M10); not `raise_on_progress`: another flow of this mesh at the same
        # step must not abort this one at its last step
        await self.async_set_unique_id(
            mesh_unique_id(cdb.mesh_uuid), raise_on_progress=False
        )
        self._abort_if_unique_id_configured()
        # an entry whose migration has not reached it yet still holds a Network ID: its recorded mesh UUID counts
        if await _mesh_uuid_taken(self.hass, cdb.mesh_uuid):
            raise _FormError({"base": "mesh_already_configured"})
        if self._area_choice is None and room_names(cdb):
            return await self._async_ask_areas(data, cdb)
        await self._async_keep_incoming(data, cdb)
        # a proxy in range leaves a discovery card of this very mesh, held by its Network ID: it goes with the entry
        for key in cdb.rx_net_keys(0):
            abort_discovery_flows(
                self.hass, key.network_id.hex(), keep_flow=self.flow_id
            )
        return self.async_create_entry(
            title=f"JUNG HOME mesh {cdb.mesh_uuid[:8]}",
            data=data,
            options=self._area_choice or {},
        )

    async def _async_ask_areas(
        self, data: dict[str, Any], cdb: CDB
    ) -> ConfigFlowResult:
        """Hold the validated export (a fetched or uploaded file stays `_incoming`) and show the `areas` step."""
        self._pending = (data, cdb)
        return await self.async_step_areas()


class JungHomeOptionsFlow(OptionsFlow):
    """Runtime behaviour switches; saving reloads the entry when something changed (`__init__._async_entry_updated`)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the one options form; saving stores it into the entry's options (the `areas` step's stay)."""
        if user_input is not None:
            return self.async_create_entry(
                data={**self.config_entry.options, **user_input}
            )
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(dict(self.config_entry.options))
        )
