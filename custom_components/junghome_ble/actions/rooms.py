"""The room actions: `set_room`, `add_to_room`, `remove_from_room`, `create_room`, `rename_room`, `delete_room`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from custom_components.junghome_ble.areas import area_name_for

from .common import (
    _DRY_RUN_FIELD,
    _ENTRY_FIELD,
    _FORCE_FIELD,
    _ONE_ROOM,
    _ROOM_FIELDS,
    _SKIP_PREFLIGHT_FIELD,
    ATTR_FORCE,
    ATTR_NAME,
    ATTR_NEW_NAME,
    LOAD_TYPES,
    _answer,
    _execute,
    _run,
)
from .resolve import Load, _entry_for_hub_services, _resolve_loads, _room_of

if TYPE_CHECKING:
    from custom_components.junghome_ble.mesh_config import MeshConfigurator


ATTR_CREATE = "create"
ADD_TO_ROOM_SCHEMA = vol.All(
    vol.Schema(
        {
            **_ROOM_FIELDS,
            vol.Optional(ATTR_CREATE, default=False): cv.boolean,
            **_DRY_RUN_FIELD,
            **cv.ENTITY_SERVICE_FIELDS,
        }
    ),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
    *_ONE_ROOM,
)
# `set_room` leaves the other rooms (Subscription Deletes): `force` skips their pre-flight comparison
SET_ROOM_SCHEMA = vol.All(
    vol.Schema(
        {
            **_ROOM_FIELDS,
            vol.Optional(ATTR_CREATE, default=False): cv.boolean,
            **_FORCE_FIELD,
            **_DRY_RUN_FIELD,
            **cv.ENTITY_SERVICE_FIELDS,
        }
    ),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
    *_ONE_ROOM,
)
# `force` takes a load a key drives out all the same; `skip_preflight` skips the comparison
REMOVE_FROM_ROOM_SCHEMA = vol.All(
    vol.Schema(
        {
            **_ROOM_FIELDS,
            vol.Optional(ATTR_FORCE, default=False): cv.boolean,
            **_SKIP_PREFLIGHT_FIELD,
            **_DRY_RUN_FIELD,
            **cv.ENTITY_SERVICE_FIELDS,
        }
    ),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
    *_ONE_ROOM,
)
CREATE_ROOM_SCHEMA = vol.Schema(
    {vol.Required(ATTR_NAME): cv.string, **_DRY_RUN_FIELD, **_ENTRY_FIELD}
)
RENAME_ROOM_SCHEMA = vol.All(
    vol.Schema(
        {
            **_ROOM_FIELDS,
            vol.Required(ATTR_NEW_NAME): cv.string,
            **_ENTRY_FIELD,
        }
    ),
    *_ONE_ROOM,
)
DELETE_ROOM_SCHEMA = vol.All(
    vol.Schema({**_ROOM_FIELDS, **_FORCE_FIELD, **_DRY_RUN_FIELD, **_ENTRY_FIELD}),
    *_ONE_ROOM,
)


@callback
def _suggest_area(
    hass: HomeAssistant, entry_id: str, device_id: str | None, room: str
) -> None:
    """Put a device that has no area yet into its room's area (`areas.area_name_for`, as on creation).

    The area the entry's `areas` step mapped the room to, else the one named or aliased like it, else a new one
    named after it; none when the entry assigns no areas. `room` is the export's name of the room
    (`PlanOutcome.room`), which the mapping is keyed by, not the call's spelling of it.
    """
    registry = dr.async_get(hass)
    device = registry.async_get(device_id) if device_id else None
    entry = hass.config_entries.async_get_entry(entry_id)
    if device is None or device.area_id is not None or entry is None:
        return
    if (name := area_name_for(hass, entry.options, room)) is None:
        return
    area = ar.async_get(hass).async_get_or_create(name)
    registry.async_update_device(device.id, area_id=area.id)


# ------------------------------------------------------------------ handlers


async def _set_room(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Put the loads into a room, out of every other one; answers what was applied (or would be, `dry_run`)."""
    return await _join_room(hass, call, only=True)


async def _add_to_room(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Put the loads into a room as well, keeping their other rooms (several rooms per load, as in the app)."""
    return await _join_room(hass, call, only=False)


async def _join_room(
    hass: HomeAssistant, call: ServiceCall, *, only: bool
) -> ServiceResponse:
    """`set_room` (`only`) or `add_to_room`: one plan per entry, the device areas suggested after a real run."""
    room = _room_of(hass, call.data)
    create: bool = call.data[ATTR_CREATE]
    loads = await _resolve_loads(hass, call, LOAD_TYPES)
    results: list[dict[str, Any]] = []
    for entry_id in sorted({load.entry_id for load in loads}):
        mine = [load for load in loads if load.entry_id == entry_id]

        async def operation(
            configurator: MeshConfigurator,
            mine: list[Load] = mine,
            entry_id: str = entry_id,
        ) -> bool:
            # one plan, one export rewrite (and `.bak`), one gateway upload for every load of the call
            change = configurator.set_rooms if only else configurator.add_to_rooms
            changed = await change([load.address for load in mine], room, create=create)
            if not configurator.dry:
                joined = configurator.outcome.room
                assert joined is not None  # every room change names the room it joined
                for load in mine:
                    _suggest_area(hass, entry_id, load.device_id, joined)
            return changed

        results.append(await _execute(hass, call, entry_id, operation))
    return _answer(call, results)


async def _remove_from_room(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Take the loads out of a room, keeping their other rooms; the device areas stay as they are."""
    room = _room_of(hass, call.data)
    force: bool = call.data[ATTR_FORCE]
    loads = await _resolve_loads(hass, call, LOAD_TYPES)
    results: list[dict[str, Any]] = []
    for entry_id in sorted({load.entry_id for load in loads}):
        mine = [load for load in loads if load.entry_id == entry_id]

        async def operation(
            configurator: MeshConfigurator, mine: list[Load] = mine
        ) -> bool:
            return await configurator.remove_from_rooms(
                [load.address for load in mine], room, force=force
            )

        results.append(await _execute(hass, call, entry_id, operation))
    return _answer(call, results)


async def _create_room(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Create an empty room; answers `{"room", "address"}` when a response is asked for."""
    entry_id = _entry_for_hub_services(hass, call.data)
    name: str = call.data[ATTR_NAME]
    created: dict[str, Any] = {}

    async def operation(configurator: MeshConfigurator) -> bool:
        address = await configurator.create_room(name)
        created.update(room=name, address=f"{address:04X}")
        # an empty room changes no device; the app's export adopted first may
        return configurator.adopted

    result = await _execute(hass, call, entry_id, operation, needs_link=False)
    return _answer(call, [result, created])


async def _rename_room(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry_id = _entry_for_hub_services(hass, call.data)
    room = _room_of(hass, call.data)
    await _run(
        hass,
        entry_id,
        lambda c: c.rename_room(room, call.data[ATTR_NEW_NAME]),
        needs_link=False,
    )
    return None


async def _delete_room(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry_id = _entry_for_hub_services(hass, call.data)
    room = _room_of(hass, call.data)
    result = await _execute(hass, call, entry_id, lambda c: c.delete_room(room))
    return _answer(call, [result])
