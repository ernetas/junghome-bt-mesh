"""Follow a rewritten export without reloading the entry: the running hub takes the new model over.

The room, key, scene, threshold and sensor-publication actions, a device rename and the unknown-node export adoption
end here once the export changed (`actions.common._run`, `ExportWatch._reload_for_export`). A reload replaced the hub —
its states cache, the property reader, the link — and removed every entity first, which wrote `unavailable`: every
`state` trigger without `from:` fired again, and every config entity read its value over the mesh once more.
`async_follow_export` instead:

1. reads the export the way the setup does (`load_network`), with what the setup does to it before a hub exists:
   Home Assistant's provisioner identity taken back from it (`VaultKeeper.async_recover`), our address checked
   against its nodes (`check_our_address`), a key refresh the hub followed put in (`async_apply_followed_key_refresh`),
   the mesh remembered for discovery;
2. judges whether the running hub can take it over: the same mesh, keys and nodes, a node added allowed
   (`JungHomeHub.model_refusal`); then builds every platform's entities from it (`entity.TrackedPlatform`) with
   the hub showing the new model for that moment (`JungHomeHub.swap_model`), and checks each against the entity
   of the same unique id that runs (`_rebind_refusal`): the same class, the same element, the same elements
   followed;
3. applies it, awaiting nothing, so no other task ever sees a half-swapped model: the hub's model
   (`JungHomeHub.async_apply_model`); per platform the entities whose unique id is new are added and those no
   longer built leave the entity registry; every kept entity takes over the attributes its constructor took from
   the model (`_rebind`) — those it changed since are what it learnt at run time and stay — follows what else
   changed (`JungHomeEntity.async_model_rebound`: a room entity's members) and writes its state; the registry
   devices follow (`register_parent_devices`, each entity's device info, the devices the export lost pruned).

With `OPTION_SYNC_AREAS` on, the devices whose room the change moved follow it into their new room's area
(`async_sync_areas`), in place or after the reload alike; a device the user placed stays where it is.

Anything refused in 1 or 2 reloads the entry as before, and so does an error anywhere (logged at DEBUG with the reason):
the reload rebuilds all of it, so the running model is never left half-updated. Adding or removing a node with
Home Assistant (`add_device`, `remove_device`), an options change and a reconfiguration still reload. The states
cache, the property reader and the link survive an in-place apply: no entity passes through `unavailable` or
`unknown`, and nothing is read over the mesh again. Unverified on air.
"""

from __future__ import annotations

import logging
from functools import cache
from typing import TYPE_CHECKING, Any, Final, cast

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .areas import async_move_devices
from .const import (
    CONF_CDB_PATH,
    CONF_METADATA_DIR,
    CONF_UNICAST,
    DEFAULT_SYNC_AREAS,
    DOMAIN,
    ISSUE_ADDRESS_IN_USE,
    ISSUE_ADDRESS_RESERVED,
    OPTION_SYNC_AREAS,
    learn_more_url,
)
from .coordinator import (
    async_apply_followed_key_refresh,
    async_known_mesh,
    issue_id,
    load_network,
    remember_known_mesh,
)
from .data import jung_data
from .entity import (
    JungHomeEntity,
    current_device_identifiers,
    current_room_central_ids,
    device_rooms,
    entities_by_unique_id,
    register_parent_devices,
    room_central_prefix,
)
from .inserts import apply_reported
from .onboard import async_update_pending_issue

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity

    from .coordinator import JungHomeHub
    from .jhmesh.cdb import CDB
    from .jhmesh.devices import Devices

_LOGGER = logging.getLogger(__name__)

_MISSING: Final = object()
_PRIVATE_ATTR: Final = (
    "__attr_"  # where Home Assistant's cached `_attr_` properties keep their values
)


class ApplyRefused(Exception):
    """The export cannot be followed in place with confidence; the message says why (never a key)."""


async def async_follow_export(
    hass: HomeAssistant, entry_id: str, *, scenes: bool = False
) -> None:
    """Have the entry follow the export it points at after a change: in place when it can, else by a reload.

    `scenes`: the change stored or deleted scenes on the devices, whose actions the hub then reads again (a reload
    read them on its first link). An entry that is not loaded (a reload outside the service lock caught it in
    between) is simply set up again.
    """
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is not None and entry.state is ConfigEntryState.LOADED:
        remember_device_rooms(hass, entry)
        try:
            await _async_follow(hass, entry, entry.runtime_data, scenes=scenes)
        except ApplyRefused as err:
            _LOGGER.debug("%s: reloading to follow the export: %s", entry.title, err)
        except Exception as err:
            _LOGGER.debug(
                "%s: reloading to follow the export: applying it in place failed (%r)",
                entry.title,
                err,
                exc_info=True,
            )
        else:
            async_sync_areas(hass, entry, entry.runtime_data)
            return
    await hass.config_entries.async_reload(entry_id)


def remember_device_rooms(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Note each device's room before the entry follows a new export, for `async_sync_areas` (sync on, entry loaded).

    Kept until the next setup or in-place apply of the entry takes it: the export's new rooms are known only then.
    """
    if (
        entry.options.get(OPTION_SYNC_AREAS, DEFAULT_SYNC_AREAS)
        and entry.state is ConfigEntryState.LOADED
    ):
        jung_data(hass).rooms_before[entry.entry_id] = device_rooms(entry.runtime_data)


def async_sync_areas(hass: HomeAssistant, entry: ConfigEntry, hub: JungHomeHub) -> int:
    """Move the devices whose room changed since `remember_device_rooms` into their new room's area; how many moved.

    `OPTION_SYNC_AREAS` (off by default): a device the user placed in an area of their own stays there
    (`areas.async_move_devices`). Unverified on air.
    """
    before = jung_data(hass).rooms_before.pop(entry.entry_id, None)
    if before is None or not entry.options.get(OPTION_SYNC_AREAS, DEFAULT_SYNC_AREAS):
        return 0
    changed = {
        ident: (before[ident], room)
        for ident, room in device_rooms(hub).items()
        if ident in before and before[ident] != room
    }
    return async_move_devices(
        hass, entry.entry_id, changed, entry.options, entry.options
    )


async def _async_follow(
    hass: HomeAssistant, entry: ConfigEntry, hub: JungHomeHub, *, scenes: bool = False
) -> None:
    unicast = int(entry.data[CONF_UNICAST], 16)
    cdb, devices = await hass.async_add_executor_job(
        load_network,
        entry.data[CONF_CDB_PATH],
        entry.data.get(CONF_METADATA_DIR) or None,
    )
    if cdb.mesh_uuid.lower() != hub.cdb.mesh_uuid.lower():
        raise ApplyRefused("the export is of another mesh")
    # what the push-buttons advertised or answered of their inserts, as the setup takes it (`inserts.async_setup`):
    # without it a node whose export has no InsertId would come back with another load class and force a reload
    devices = apply_reported(cdb, devices, hub.inserts.adverts, hub.node_info)
    if await hub.vault.async_recover(cdb, unicast):
        raise ApplyRefused(
            "Home Assistant's provisioner identity was taken back from it"
        )
    try:
        check_our_address(hass, entry, cdb, unicast, hub.vault.own_uuid)
    except ConfigEntryError as err:
        raise ApplyRefused("a node of the export has our address") from err
    await async_apply_followed_key_refresh(hass, cdb, unicast)
    if (why := hub.model_refusal(cdb)) is not None:
        raise ApplyRefused(why)
    known = await async_known_mesh(hass, cdb, unicast)
    if entry.state is not ConfigEntryState.LOADED or entry.runtime_data is not hub:
        # an options change or a reconfiguration reloads without the service lock, and may have meanwhile
        raise ApplyRefused("the entry was set up again meanwhile")
    # nothing awaited from here on: no other task sees the hub between the old model and the new one
    fresh = _build(hass, hub, cdb, devices)
    remember_known_mesh(hass, entry.entry_id, known)
    _apply(hass, entry, hub, cdb, devices, fresh, scenes=scenes)


def _build(
    hass: HomeAssistant, hub: JungHomeHub, cdb: CDB, devices: Devices
) -> dict[str, dict[str, Entity]]:
    """Build every platform's entities from the new model and check each kept one can take its over; by platform.

    The hub shows the new model while its builders run and the checks ask it, and its own again afterwards
    whatever happens. Its parent devices are registered first: the entities' device info names them
    (`via_device_id`), and a reload would register them the same way.
    """
    previous = hub.swap_model(cdb, devices)
    try:
        register_parent_devices(hass, hub)
        fresh = {
            domain: entities_by_unique_id(tracked.build(hub))
            for domain, tracked in hub.platforms.items()
        }
        for domain, tracked in hub.platforms.items():
            for unique_id, entity in fresh[domain].items():
                kept = tracked.entities.get(unique_id)
                if kept is not None and (why := _rebind_refusal(kept, entity)):
                    raise ApplyRefused(f"{domain} {unique_id}: {why}")
    finally:
        hub.swap_model(*previous)
    return fresh


def _apply(
    hass: HomeAssistant,
    entry: ConfigEntry,
    hub: JungHomeHub,
    cdb: CDB,
    devices: Devices,
    fresh: dict[str, dict[str, Entity]],
    *,
    scenes: bool = False,
) -> None:
    """Make the new model the running one: the hub's, then each platform's entities, then the registries."""
    hub.async_apply_model(cdb, devices, scenes=scenes)
    entities = er.async_get(hass)
    registry = dr.async_get(hass)
    rebound: list[Entity] = []
    for domain, tracked in hub.platforms.items():
        built = fresh[domain]
        for unique_id in [u for u in tracked.entities if u not in built]:
            del tracked.entities[unique_id], tracked.built[unique_id]
            if entity_id := entities.async_get_entity_id(domain, DOMAIN, unique_id):
                _LOGGER.info("Removing %s, no longer in the mesh export", entity_id)
                entities.async_remove(entity_id)
        added: list[Entity] = []
        for unique_id, entity in built.items():
            kept = tracked.entities.get(unique_id)
            if kept is None:
                tracked.entities[unique_id] = entity
                tracked.built[unique_id] = dict(vars(entity))
                added.append(entity)
                continue
            tracked.built[unique_id] = _rebind(kept, tracked.built[unique_id], entity)
            entity_id = entities.async_get_entity_id(domain, DOMAIN, unique_id)
            registered = entities.async_get(entity_id) if entity_id else None
            _follow_device(entry, registry, entities, kept, registered)
            if (
                kept.hass is not None
                and registered is not None
                and not registered.disabled
            ):
                rebound.append(kept)
        if added:
            tracked.add(added)
    remove_stale_devices(hass, entry, hub)
    async_update_pending_issue(hass, entry, hub.vault)
    for entity in rebound:
        if isinstance(entity, JungHomeEntity):
            entity.async_model_rebound()
        entity.async_write_ha_state()


def _follow_device(
    entry: ConfigEntry,
    registry: dr.DeviceRegistry,
    entities: er.EntityRegistry,
    entity: Entity,
    registered: er.RegistryEntry | None,
) -> None:
    """Bring the entity's device and registry entry up to its device info, as adding it again would.

    The device takes the new name the export gives it (`async_get_or_create`, as Home Assistant's entity platform
    registers it); an entity whose device info names another device (a key moved to another gang) moves to it.
    """
    if (info := entity.device_info) is None:
        return
    device = registry.async_get_or_create(config_entry_id=entry.entry_id, **info)
    if registered is not None and (
        registered.device_id != device.id
        or registered.translation_key != entity.translation_key
    ):
        entities.async_update_entity(
            registered.entity_id,
            device_id=device.id,
            translation_key=entity.translation_key,
        )


# ----------------------------------------------------------------------------- rebinding an entity


@cache
def _cached_properties(cls: type) -> frozenset[str]:
    """Return the names of Home Assistant's cached properties on `cls` (`CachedProperties`), worked out once per class."""
    names: set[str] = set()
    for klass in cls.__mro__:
        names |= klass.__dict__.get("_CachedProperties__cached_properties", set())
    return frozenset(names)


def _rebind_refusal(kept: Entity, fresh: Entity) -> str | None:
    """Why `kept` cannot become what `fresh` is; None when it can.

    The class must be the same, and the entity's own check must pass (`JungHomeEntity.rebind_refusal`: its
    element, the other elements it follows).
    """
    if type(fresh) is not type(kept):
        return f"it is a {type(fresh).__name__} now"
    if isinstance(kept, JungHomeEntity):
        return kept.rebind_refusal(cast("JungHomeEntity", fresh))  # the same class
    return None


def _attribute(key: str) -> str:
    """Return the attribute a `vars()` key is set through: an `_attr_` property for its stored `__attr_` value."""
    if key.startswith(_PRIVATE_ATTR):
        return "_attr_" + key.removeprefix(_PRIVATE_ATTR)
    return key


def _rebind(kept: Entity, built: dict[str, Any], fresh: Entity) -> dict[str, Any]:
    """Carry what `fresh`'s constructor took from the new model over to `kept`; return `kept`'s new `built`.

    An attribute `kept` still holds as its constructor set it (the same object) came from the model and takes the
    new one: the device it binds, its members, attributes, device info, translation placeholders; one the new
    constructor no longer sets goes, so the class default shows again (a key that came to share its gang loses
    the `_attr_name` that made it the device's name). One the entity replaced since is what it learnt at run time
    (a value read, a timer, the link it read on) and stays — no constructor here sets one from the model. An
    `_attr_` value goes through its property, which drops the cached value; every other cached property is dropped
    too, so the name and the device info are worked out anew.
    """
    held = vars(kept)
    cached = _cached_properties(type(kept))
    after = dict(built)
    for key, value in vars(fresh).items():
        if key in cached or (
            key in built and held.get(key, _MISSING) is not built[key]
        ):
            continue
        setattr(kept, _attribute(key), value)
        after[key] = held[key]
    for key in built.keys() - vars(fresh).keys():
        if held.get(key, _MISSING) is built[key]:
            delattr(kept, _attribute(key))
            del after[key]
    for name in cached:
        held.pop(name, None)
    return after


# ----------------------------------------------------------------------------- what the setup checks too


def check_our_address(
    hass: HomeAssistant,
    entry: ConfigEntry,
    cdb: CDB,
    unicast: int,
    own: str | None = None,
) -> None:
    """Refuse an address a node of the export occupies; warn about one the app may hand out.

    The config flow checks the address once, but the export changes under it: the app provisions nodes and a
    second app user gets a provisioner range of their own, which may cover Home Assistant's default address. A
    node sending from our address makes every node drop one of us as a replay and our replies go astray, so that
    stops the setup; an address that is merely reserved keeps working until the app uses it. `own` is Home
    Assistant's provisioner UUID: the node and the range the file records for it are ours.
    """
    key = f"{unicast:04X}"
    placeholders = {"title": entry.title, "unicast": key}
    suggestion = cdb.suggest_unicast()
    placeholders["suggestion"] = "none" if suggestion is None else f"{suggestion:04X}"
    in_use = unicast in cdb.used_unicasts(own)
    reserved = not in_use and not cdb.unicast_is_free(unicast, own=own)
    for issue_key, raised in (
        (ISSUE_ADDRESS_IN_USE, in_use),
        (ISSUE_ADDRESS_RESERVED, reserved),
    ):
        if not raised:
            ir.async_delete_issue(hass, DOMAIN, issue_id(entry, issue_key))
            continue
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id(entry, issue_key),
            # taken: its repair moves Home Assistant to the suggested address (`repairs.FreeAddressFlow`)
            is_fixable=in_use,
            data={"entry_id": entry.entry_id} if in_use else None,
            severity=ir.IssueSeverity.ERROR if in_use else ir.IssueSeverity.WARNING,
            translation_key=issue_key,
            learn_more_url=learn_more_url(issue_key),
            translation_placeholders=placeholders,
        )
    if in_use:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="address_in_use",
            translation_placeholders=placeholders,
        )


def remove_stale_devices(
    hass: HomeAssistant, entry: ConfigEntry, hub: JungHomeHub
) -> None:
    """Take the devices the export no longer has off the entry, and the room entities of rooms gone or emptied."""
    registry = dr.async_get(hass)
    current = current_device_identifiers(hub)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not device.identifiers & current:
            _LOGGER.info(
                "Removing device %s, no longer in the mesh export", device.name
            )
            registry.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )
    # a room's central entities sit on the mesh device, which stays: a room deleted, or left without loads of a
    # kind, takes its own with it here
    entities = er.async_get(hass)
    prefix, rooms = room_central_prefix(hub), current_room_central_ids(hub)
    for ent in er.async_entries_for_config_entry(entities, entry.entry_id):
        if ent.unique_id.startswith(prefix) and ent.unique_id not in rooms:
            _LOGGER.info(
                "Removing %s, its room no longer has such loads", ent.entity_id
            )
            entities.async_remove(ent.entity_id)
