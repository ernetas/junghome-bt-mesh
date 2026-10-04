"""Resolving the Home Assistant ids a call names — devices, entities, areas, scene entities — to mesh elements.

Through the registries and the device-identifier scheme documented in `device_info.py` (`{uuid}-{location:04x}` loads,
`{uuid}-{location:04x}-buttons` gangs of keys, `node:{uuid}` nodes).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ENTITY_MATCH_ALL, Platform
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.target import (
    TargetSelection,
    async_extract_referenced_entity_ids,
)

from custom_components.junghome_ble.const import ATTR_SCENE, DOMAIN
from custom_components.junghome_ble.entity import (
    button_gang,
    buttons_device_id,
    mesh_identifier,
    node_identifier,
)

from .common import (
    ATTR_CONFIG_ENTRY,
    ATTR_ROOM,
    ATTR_ROOM_AREA,
    ATTR_SCENE_ENTITY,
    SCENE_LOAD_TYPES,
    _hub,
    _validation,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

    from custom_components.junghome_ble.coordinator import JungHomeHub
    from custom_components.junghome_ble.jhmesh.devices import Device


@dataclass(frozen=True)
class Load:
    """A light / socket element behind a Home Assistant id, with its registry device (for the area)."""

    entry_id: str
    address: int
    device_id: str | None


def _entry_for_hub_services(hass: HomeAssistant, data: Mapping[str, Any]) -> str:
    """Pick the config entry a room-only service applies to: the given one, or the only loaded one."""
    if (entry_id := data.get(ATTR_CONFIG_ENTRY)) is not None:
        _hub(hass, entry_id)
        return str(entry_id)
    loaded = [
        e.entry_id
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]
    if not loaded:
        raise _validation("service_entry_not_loaded")
    if len(loaded) > 1:
        raise _validation("service_entry_ambiguous")
    return loaded[0]


def _our_entry_id(hass: HomeAssistant, device: dr.DeviceEntry) -> str | None:
    """Return the device's config entry id when it is one of ours (a device has exactly one entry since 2026.8)."""
    entry = hass.config_entries.async_get_entry(device.config_entry_id)
    if entry is not None and entry.domain == DOMAIN:
        return device.config_entry_id
    return None


def _our_identifier(device: dr.DeviceEntry) -> str | None:
    return next(
        (ident for domain, ident in device.identifiers if domain == DOMAIN), None
    )


def _registry_device(hass: HomeAssistant, device_id: str) -> tuple[dr.DeviceEntry, str]:
    """Look up a device of ours in the registry; returns it with its config entry id."""
    device = dr.async_get(hass).async_get(device_id)
    if not isinstance(device, dr.DeviceEntry):
        raise _validation("service_unknown_device", id=device_id)
    entry_id = _our_entry_id(hass, device)
    if entry_id is None or _our_identifier(device) is None:
        raise _validation("service_unknown_device", id=device_id)
    return device, entry_id


def _devices_behind(hub: JungHomeHub, device: dr.DeviceEntry) -> list[Device]:
    """List the mesh devices (loads, keys) a registry device stands for."""
    ident = _our_identifier(device) or ""
    if ident.startswith("node:"):
        return [
            d
            for d in hub.devices.by_address.values()
            if node_identifier(d.node) == ident
        ]
    if ident.endswith("-buttons"):
        return [
            b
            for b in hub.devices.buttons
            if buttons_device_id(button_gang(hub, b)) == ident
        ]
    return [d for d in hub.devices.by_address.values() if d.unique_id == ident]


def _device_of_entity(
    hass: HomeAssistant, entity_id: str
) -> tuple[str, Device | None, str | None]:
    """Find the mesh device behind one of our entities: (entry id, device, registry device id).

    The device is None for one of our entities that stands for no mesh device (a config entity, a sensor of the
    proxy): the callers say what it is not (a load, a key); `service_unknown_device` is for entities not ours.
    """
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None or entry.platform != DOMAIN or entry.config_entry_id is None:
        raise _validation("service_unknown_device", id=entity_id)
    hub = _hub(hass, entry.config_entry_id)
    device = next(
        (d for d in hub.devices.by_address.values() if d.unique_id == entry.unique_id),
        None,
    )
    return entry.config_entry_id, device, entry.device_id


def _resolve_node(hass: HomeAssistant, device_id: str) -> tuple[str, int | None]:
    """Find the node a device of ours stands for: (entry id, its unicast); None for the mesh device (every node)."""
    registry_device, entry_id = _registry_device(hass, device_id)
    hub = _hub(hass, entry_id)
    ident = _our_identifier(registry_device)
    if ident == mesh_identifier(hub):
        return entry_id, None
    for node in hub.cdb.nodes:
        if node_identifier(node) == ident:
            return entry_id, node.unicast
    behind = _devices_behind(hub, registry_device)
    if not behind:  # a registry leftover of a load the export no longer has
        raise _validation("service_unknown_device", id=device_id)
    return entry_id, behind[0].node.unicast


async def _resolve_loads(
    hass: HomeAssistant,
    call: ServiceCall,
    types: tuple[type[Device], ...] = SCENE_LOAD_TYPES,
) -> list[Load]:
    """Every load of `types` behind the call's target (devices, entities, areas, floors, labels); explicit ids must be ours.

    Devices come from `referenced_devices` (named, or found through an area / floor / label); entities either
    explicitly (`entity_id`, which must be a load of ours) or indirectly — a label on the light entity itself,
    an entity moved into a targeted area on its own, a member of a targeted group — where anything that is not a
    load of ours is skipped, as Home Assistant's own entity services do.
    """
    selection = TargetSelection(call.data)
    if ENTITY_MATCH_ALL in selection.entity_ids:
        # HA's target helper filters only `none`; `all` would reach `_device_of_entity` as a literal id
        raise _validation("service_all_not_supported")
    selected = async_extract_referenced_entity_ids(hass, selection)
    registry = dr.async_get(hass)
    loads: dict[tuple[str, int], Load] = {}
    for device_id in sorted(selected.referenced_devices):
        device = registry.async_get(device_id)
        entry_id = (
            _our_entry_id(hass, device) if isinstance(device, dr.DeviceEntry) else None
        )
        if (
            not isinstance(device, dr.DeviceEntry)
            or entry_id is None
            or _our_identifier(device) is None
        ):
            if device_id in selection.device_ids:
                raise _validation("service_unknown_device", id=device_id)
            continue  # an area / label also holds devices of other integrations
        found = [
            d
            for d in _devices_behind(_hub(hass, entry_id), device)
            if isinstance(d, types)
        ]
        if not found and device_id in selection.device_ids:
            raise _validation(
                "service_not_a_load",
                name=device.name_by_user or device.name or device_id,
            )
        for d in found:
            loads.setdefault(
                (entry_id, d.address), Load(entry_id, d.address, device_id)
            )
    explicit = set(selection.entity_ids)
    indirect = (selected.referenced | selected.indirectly_referenced) - explicit
    for entity_id in sorted(explicit | indirect):
        try:
            owner, mesh_device, registry_id = _device_of_entity(hass, entity_id)
        except ServiceValidationError:
            if entity_id in explicit:
                raise
            continue  # a label / area / group also holds entities of other integrations
        if not isinstance(mesh_device, types):
            if entity_id in explicit:
                raise _validation("service_not_a_load", name=entity_id)
            continue
        loads.setdefault(
            (owner, mesh_device.address), Load(owner, mesh_device.address, registry_id)
        )
    if not loads:
        raise _validation("service_no_loads")
    return list(loads.values())


def _room_of(hass: HomeAssistant, data: Mapping[str, Any]) -> str:
    """Return the room a call names: `room`, or the area `room_area` — the room called like it."""
    if (area_id := data.get(ATTR_ROOM_AREA)) is None:
        return str(data[ATTR_ROOM])
    area = ar.async_get(hass).async_get_area(area_id)
    if area is None:
        raise _validation("service_unknown_area", id=area_id)
    return area.name


def _scene_of(hass: HomeAssistant, data: Mapping[str, Any]) -> tuple[str | None, str]:
    """Return the scene a call names, with its entry when a scene entity named it: `scene`, or that entity's number."""
    if (entity_id := data.get(ATTR_SCENE_ENTITY)) is None:
        return None, str(data[ATTR_SCENE])
    entry = er.async_get(hass).async_get(entity_id)
    if (
        entry is None
        or entry.platform != DOMAIN
        or entry.domain != Platform.SCENE
        or entry.config_entry_id is None
    ):
        raise _validation("service_not_a_scene", name=entity_id)
    return entry.config_entry_id, entry.unique_id.rsplit("-", 1)[-1]


def _same_network(owner: str | None, entry_ids: Iterable[str]) -> None:
    """Refuse a scene entity of one network for loads or a key of another."""
    if owner is not None and any(entry_id != owner for entry_id in entry_ids):
        raise _validation("service_target_other_network")
