"""The schedule actions, `get_schedules` … `delete_schedule`, on the loads' own JH Scheduler (`schedules.py`).

They write no export and change no model.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from custom_components.junghome_ble import schedules
from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.entity import load_entity_id
from custom_components.junghome_ble.jhmesh import vendor_models as V

from .common import (
    _STATE_FIELDS,
    SCHEDULE_LOAD_TYPES,
    TARGETS_SCHEMA,
    _hub,
    _run,
    _validation,
)
from .resolve import _resolve_loads

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse

    from custom_components.junghome_ble.mesh_config import MeshConfigurator


ATTR_SLOT = "slot"
ATTR_TRIGGER = "trigger"
ATTR_TIME = "time"
ASTRO_FIELDS = ("not_before", "not_after", "offset")


def _schedule_trigger_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Check the trigger's fields: a `time` for a timed schedule, a window and an offset for a sunrise / sunset one."""
    if data[ATTR_TRIGGER] == "time":
        if ATTR_TIME not in data:
            raise vol.Invalid("a `time` schedule needs `time`")
        if astro := [name for name in ASTRO_FIELDS if name in data]:
            raise vol.Invalid(f"{', '.join(astro)} go with sunrise / sunset")
    elif ATTR_TIME in data:
        raise vol.Invalid("`time` goes with the trigger `time`")
    return data


ENABLE_SCHEDULE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(ATTR_SLOT): vol.All(vol.Coerce(int), vol.Range(min=0, max=15)),
            **cv.ENTITY_SERVICE_FIELDS,
        }
    ),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
)
DISABLE_SCHEDULE_SCHEMA = DELETE_SCHEDULE_SCHEMA = ENABLE_SCHEDULE_SCHEMA
GET_SCHEDULES_SCHEMA = TARGETS_SCHEMA
_CREATE_SCHEDULE_FIELDS: dict[str | vol.Marker, Any] = {
    vol.Required(ATTR_TRIGGER): vol.In(schedules.TRIGGERS),
    vol.Optional(ATTR_TIME): cv.time,
    vol.Optional("not_before"): cv.time,
    vol.Optional("not_after"): cv.time,
    vol.Optional("offset"): vol.All(vol.Coerce(int), vol.Range(min=-128, max=127)),
    vol.Optional("weekdays"): vol.All(
        cv.ensure_list, vol.Length(min=1), [vol.In(V.DAYS)]
    ),
    vol.Optional("enabled"): cv.boolean,
    **_STATE_FIELDS,
    **cv.ENTITY_SERVICE_FIELDS,
}
CREATE_SCHEDULE_SCHEMA = vol.All(
    vol.Schema(_CREATE_SCHEDULE_FIELDS),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
    _schedule_trigger_fields,
)
# the app's edit (`Params.Update(index, …)`): a used slot rewritten with what create_schedule takes, checked alike
UPDATE_SCHEDULE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(ATTR_SLOT): vol.All(vol.Coerce(int), vol.Range(min=0, max=15)),
            **_CREATE_SCHEDULE_FIELDS,
        }
    ),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
    _schedule_trigger_fields,
)


# ------------------------------------------------------------------ schedules


@dataclass(frozen=True)
class ScheduleLoad:
    """A load whose JH Scheduler a schedule call works on, named by its entity (the key of the response)."""

    address: int
    kind: str
    name: str


async def _schedule_loads(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, list[ScheduleLoad]]:
    """Return the call's loads by entry; each must host a JH Scheduler (every load of the app's does)."""
    out: dict[str, list[ScheduleLoad]] = {}
    for load in await _resolve_loads(hass, call, SCHEDULE_LOAD_TYPES):
        hub = _hub(hass, load.entry_id)
        device = hub.devices.by_address[load.address]
        name = load_entity_id(hass, device)
        if not schedules.scheduler(hass, hub).hosts(load.address):
            raise _validation("schedule_not_supported", name=name)
        out.setdefault(load.entry_id, []).append(
            ScheduleLoad(load.address, device.kind, name)
        )
    return out


async def _on_schedules(
    hass: HomeAssistant,
    loads: dict[str, list[ScheduleLoad]],
    act: Callable[[schedules.Scheduler, ScheduleLoad], Awaitable[Any]],
) -> dict[str, Any]:
    """Run `act` on every load, one entry at a time under its lock, once its link is up; answer per load name.

    Nothing is recorded in the export, so nothing is reloaded; the scheduler is the one of the hub the lock hands
    over (a reload replaces it).
    """
    results: dict[str, Any] = {}
    for entry_id, mine in loads.items():

        async def operation(
            configurator: MeshConfigurator, mine: list[ScheduleLoad] = mine
        ) -> bool:
            on = schedules.scheduler(hass, configurator.hub)
            for load in mine:
                results[load.name] = await act(on, load)
            return False

        await _run(hass, entry_id, operation)
    return results


async def _get_schedules(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Read the loads' used slots: `{entity_id: {"schedules": [...]}}`, the fields `create_schedule` takes."""

    async def read(on: schedules.Scheduler, load: ScheduleLoad) -> dict[str, Any]:
        return {"schedules": [slot.as_dict() for slot in await on.read(load.address)]}

    return await _on_schedules(hass, await _schedule_loads(hass, call), read)


def _schedule_actions(
    loads: dict[str, list[ScheduleLoad]], data: Mapping[str, Any]
) -> dict[str, V.Action]:
    """Return each load's slot action from the call's fields, every load checked before anything goes on air."""
    actions: dict[str, V.Action] = {}
    for load in (load for mine in loads.values() for load in mine):
        try:
            actions[load.name] = schedules.schedule_action(load.kind, data)
        except schedules.ActionError as err:
            raise _validation(err.key, name=load.name, **err.placeholders) from err
    return actions


async def _create_schedule(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Write the schedule into each load's first free slot; answers `{entity_id: {"slot": n}}` when asked."""
    loads = await _schedule_loads(hass, call)
    actions = _schedule_actions(loads, call.data)

    async def free_slot(on: schedules.Scheduler, load: ScheduleLoad) -> None:
        await on.free_slot(load.address)

    created: dict[str, int] = {}

    async def create(on: schedules.Scheduler, load: ScheduleLoad) -> dict[str, Any]:
        slot = await on.create(load.address, call.data, actions[load.name])
        created[load.name] = slot.index
        return {"slot": slot.index}

    # every load has a free slot before any is written: a load found full (or silent) after the others were
    # written would leave their schedules behind, and the retry would add them a second time
    await _on_schedules(hass, loads, free_slot)
    try:
        results = await _on_schedules(hass, loads, create)
    except HomeAssistantError as err:
        if not created:
            raise
        # a load that fails after others took the schedule (it went silent since the check): the error names
        # those slots, which the response never gets to — a retry as it is would add them twice
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="schedule_partly_created",
            translation_placeholders={
                "error": str(err),
                "created": ", ".join(
                    f"{name} (slot {index})" for name, index in created.items()
                ),
            },
        ) from err
    return results if call.return_response else None


async def _update_schedule(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Rewrite slot `slot` of each load in place, the action checked for every load first.

    A load failing after others took the change leaves nothing to warn about, unlike a create: calling again
    writes the same slots with the same contents. Unverified on air.
    """
    index: int = call.data[ATTR_SLOT]
    loads = await _schedule_loads(hass, call)
    actions = _schedule_actions(loads, call.data)

    async def update(on: schedules.Scheduler, load: ScheduleLoad) -> None:
        await on.update(load.address, index, call.data, actions[load.name])

    await _on_schedules(hass, loads, update)
    return None


async def _enable_schedule(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    await _set_schedule_enabled(hass, call, enabled=True)
    return None


async def _disable_schedule(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    await _set_schedule_enabled(hass, call, enabled=False)
    return None


async def _set_schedule_enabled(
    hass: HomeAssistant, call: ServiceCall, *, enabled: bool
) -> None:
    index: int = call.data[ATTR_SLOT]

    async def toggle(on: schedules.Scheduler, load: ScheduleLoad) -> None:
        await on.set_enabled(load.address, index, enabled)

    await _on_schedules(hass, await _schedule_loads(hass, call), toggle)


async def _delete_schedule(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    index: int = call.data[ATTR_SLOT]

    async def delete(on: schedules.Scheduler, load: ScheduleLoad) -> None:
        await on.delete(load.address, index)

    await _on_schedules(hass, await _schedule_loads(hass, call), delete)
    return None
