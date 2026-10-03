"""The key-connection actions `assign_key` and `clear_key`, and the key and the target a call names."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.helpers import config_validation as cv

from ..const import ATTR_KEY, ATTR_SCENE
from ..entity import node_identifier
from ..jhmesh.devices import GATEWAY_PID, Button, Detector
from ..mesh_config import LOCK_SECONDS_MAX, MODES, TARGET_ELEMENTS
from .common import (
    _DRY_RUN_FIELD,
    _ROOM_FIELDS,
    _SCENE_FIELDS,
    ATTR_ROOM,
    ATTR_ROOM_AREA,
    ATTR_SCENE_ENTITY,
    KEY_TARGET_TYPES,
    _answer,
    _execute,
    _hub,
    _validation,
)
from .resolve import (
    _device_of_entity,
    _devices_behind,
    _our_identifier,
    _registry_device,
    _room_of,
    _same_network,
    _scene_of,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse


ATTR_KEY_ENTITY = "key_entity"
ATTR_KEY_DEVICE = "key_device"
ATTR_TARGET_ENTITY = "target_entity"
ATTR_TARGET_DEVICE = "target_device"
ATTR_MODE = "mode"
ATTR_TARGET_ELEMENT = "target_element"
ATTR_LOCK_SECONDS = "lock_seconds"
KEY_LETTERS = (
    "A",
    "B",
    "C",
    "D",
    "E1",
    "E2",
)  # push-button keys, a mini actuator's inputs
_KEY_FIELDS: dict[str | vol.Marker, Any] = {
    vol.Optional(ATTR_KEY_ENTITY): cv.entity_id,
    vol.Optional(ATTR_KEY_DEVICE): cv.string,
    vol.Optional(ATTR_KEY): vol.All(cv.string, vol.Upper, vol.In(KEY_LETTERS)),
}


def _key_only_with_device(data: dict[str, Any]) -> dict[str, Any]:
    """Refuse `key` next to `key_entity`: the letter picks a key of a `key_device`; an event entity is one key."""
    if ATTR_KEY in data and ATTR_KEY_ENTITY in data:
        raise vol.Invalid("`key` goes with `key_device`, not with `key_entity`")
    return data


# what a key can be wired to: one load, a room, a scene
ASSIGN_KEY_TARGETS = (
    ATTR_TARGET_ENTITY,
    ATTR_TARGET_DEVICE,
    ATTR_ROOM,
    ATTR_ROOM_AREA,
    ATTR_SCENE,
    ATTR_SCENE_ENTITY,
)
ASSIGN_KEY_SCHEMA = vol.All(
    vol.Schema(
        {
            **_KEY_FIELDS,
            vol.Optional(ATTR_TARGET_ENTITY): cv.entity_id,
            vol.Optional(ATTR_TARGET_DEVICE): cv.string,
            **_ROOM_FIELDS,
            **_SCENE_FIELDS,
            vol.Optional(ATTR_MODE): vol.In(MODES),
            vol.Optional(ATTR_TARGET_ELEMENT): vol.In(tuple(TARGET_ELEMENTS)),
            vol.Optional(ATTR_LOCK_SECONDS): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=LOCK_SECONDS_MAX)
            ),
            **_DRY_RUN_FIELD,
        }
    ),
    cv.has_at_least_one_key(ATTR_KEY_ENTITY, ATTR_KEY_DEVICE),
    cv.has_at_most_one_key(ATTR_KEY_ENTITY, ATTR_KEY_DEVICE),
    cv.has_at_least_one_key(*ASSIGN_KEY_TARGETS),
    cv.has_at_most_one_key(*ASSIGN_KEY_TARGETS),
    _key_only_with_device,
)
CLEAR_KEY_SCHEMA = vol.All(
    vol.Schema({**_KEY_FIELDS, **_DRY_RUN_FIELD}),
    cv.has_at_least_one_key(ATTR_KEY_ENTITY, ATTR_KEY_DEVICE),
    cv.has_at_most_one_key(ATTR_KEY_ENTITY, ATTR_KEY_DEVICE),
    _key_only_with_device,
)


def _resolve_key(
    hass: HomeAssistant, data: dict[str, Any], *, detector: bool = False
) -> tuple[str, Button | Detector]:
    """Find the key element a call names: its event entity, or its buttons device plus the key letter.

    `detector`: a detector counts as a key too (`assign_key`, the app's `ConnectionSource.Detector`), named by one
    of its entities or its device; unverified on air.
    """
    if (entity_id := data.get(ATTR_KEY_ENTITY)) is not None:
        entry_id, device, registry_id = _device_of_entity(hass, entity_id)
        if isinstance(device, Button):
            return entry_id, device
        if detector and registry_id is not None:
            found = _detector_behind(hass, registry_id)
            if found is not None:
                return found
        raise _validation("service_not_a_key", name=entity_id)
    registry_device, entry_id = _registry_device(hass, data[ATTR_KEY_DEVICE])
    name = registry_device.name_by_user or registry_device.name or registry_device.id
    keys = [
        d
        for d in _devices_behind(_hub(hass, entry_id), registry_device)
        if isinstance(d, Button)
    ]
    if not keys and detector and (found := _detector_behind(hass, registry_device.id)):
        return found
    if not keys:
        raise _validation("service_not_a_key", name=name)
    letters = ", ".join(k.key for k in keys)
    if (letter := data.get(ATTR_KEY)) is not None:
        for key in keys:
            if key.key == letter:
                return entry_id, key
        raise _validation("service_unknown_key", name=name, letter=letter, keys=letters)
    if len(keys) > 1:
        raise _validation("service_key_required", name=name, keys=letters)
    return entry_id, keys[0]


def _detector_behind(
    hass: HomeAssistant, device_id: str
) -> tuple[str, Detector] | None:
    """Return the detector a registry device of ours stands for (a detector node's) with its entry, else None."""
    registry_device, entry_id = _registry_device(hass, device_id)
    found = [
        d
        for d in _devices_behind(_hub(hass, entry_id), registry_device)
        if isinstance(d, Detector)
    ]
    return (entry_id, found[0]) if found else None


def _resolve_target(hass: HomeAssistant, data: dict[str, Any]) -> tuple[str, int]:
    """Find the load element a key should drive: (entry id, element address); the gateway node is a target too.

    A blind's address is its position element, the one a key in *move* mode publishes to; a room thermostat's its
    set-point element (key mode `temperature`).
    """
    if (entity_id := data.get(ATTR_TARGET_ENTITY)) is not None:
        entry_id, device, _ = _device_of_entity(hass, entity_id)
        if not isinstance(device, KEY_TARGET_TYPES):
            raise _validation("service_not_a_load", name=entity_id)
        return entry_id, device.address
    registry_device, entry_id = _registry_device(hass, data[ATTR_TARGET_DEVICE])
    name = registry_device.name_by_user or registry_device.name or registry_device.id
    hub = _hub(hass, entry_id)
    ident = _our_identifier(registry_device) or ""
    if ident.startswith("node:"):
        node = next((n for n in hub.cdb.nodes if node_identifier(n) == ident), None)
        if node is not None and node.pid == GATEWAY_PID:
            return entry_id, node.unicast
    loads = [
        d
        for d in _devices_behind(hub, registry_device)
        if isinstance(d, KEY_TARGET_TYPES)
    ]
    if len(loads) != 1:
        raise _validation("service_not_a_load", name=name)
    return entry_id, loads[0].address


async def _assign_key(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Wire a key to a load, a room (or the area named like it) or a scene (or its scene entity)."""
    entry_id, key = _resolve_key(hass, call.data, detector=True)
    options: dict[str, Any] = {
        "mode": call.data.get(ATTR_MODE),
        "target_element": call.data.get(ATTR_TARGET_ELEMENT),
        "lock_seconds": call.data.get(ATTR_LOCK_SECONDS),
    }
    if ATTR_ROOM in call.data or ATTR_ROOM_AREA in call.data:
        options["room"] = _room_of(hass, call.data)
    elif ATTR_SCENE in call.data or ATTR_SCENE_ENTITY in call.data:
        owner, options["scene"] = _scene_of(hass, call.data)
        _same_network(owner, [entry_id])
    else:
        target_entry, options["element"] = _resolve_target(hass, call.data)
        _same_network(target_entry, [entry_id])
    result = await _execute(
        hass, call, entry_id, lambda c: c.assign_key(key.address, **options)
    )
    return _answer(call, [result])


async def _clear_key(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry_id, key = _resolve_key(hass, call.data)
    result = await _execute(hass, call, entry_id, lambda c: c.clear_key(key.address))
    return _answer(call, [result])
