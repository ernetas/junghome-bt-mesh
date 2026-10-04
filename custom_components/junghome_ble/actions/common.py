"""What every action shares: the entry's configurator and hub, the fields several actions take, running an operation.

`_run` / `_execute` run a configurator operation under the entry's lock (`coordinator.entry_lock`), on a live
link (`_wait_for_link`), have the hub follow the export (`_follow`) and report the call's plan (`_report_plan`);
`async_configure` runs an entity's operation the same way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util.hass_dict import HassKey

from custom_components.junghome_ble.const import (
    ATTR_ENTRY_ID,
    ATTR_SCENE,
    DOMAIN,
    EVENT_PLAN,
    SERVICE_LINK_WAIT,
)
from custom_components.junghome_ble.data import entry_lock, jung_data
from custom_components.junghome_ble.jhmesh.devices import (
    Blind,
    Device,
    Light,
    Socket,
    Thermostat,
)
from custom_components.junghome_ble.mesh_config import (
    MeshConfigurator,
    PlanOutcome,
    plan_history,
    run_to_end,
)
from custom_components.junghome_ble.model_update import async_follow_export

if TYPE_CHECKING:
    from custom_components.junghome_ble.coordinator import (
        JungHomeConfigEntry,
        JungHomeHub,
    )


ATTR_ROOM = "room"
ATTR_ROOM_AREA = "room_area"  # the room named like this area, instead of `room`
ATTR_SCENE_ENTITY = (
    "scene_entity"  # the scene behind this scene entity, instead of `scene`
)
# skips a check: the pre-flight comparison of the nodes with the export (every destructive action), and the
# action's own refusal where it has one (`remove_from_room`, `delete_scene`, `remove_device`)
ATTR_FORCE = "force"
ATTR_DRY_RUN = "dry_run"
# what cannot be undone asks for it explicitly: `remove_device`, `delete_scene` with `force`
ATTR_CONFIRM = "confirm"
ATTR_NAME = "name"
ATTR_NEW_NAME = "new_name"
ATTR_CONFIG_ENTRY = "config_entry_id"
SCHEDULE_ACTIONS = ("on", "off")
ATTR_DEVICE = "device"
LINK_WAIT_SLICE = (
    1.0  # seconds between looks at which hub the entry has, while waiting for a link
)
# What a key can drive and a room can hold (a blind moves: KeyMode *move*, `mesh_config.derive_mode`); the scene
# services take thermostats too, each with its own scene action record (`scene_action_for`); a threshold switches
# what has an OnOff server (lights and sockets).
LOAD_TYPES: tuple[type[Device], ...] = (Light, Socket, Blind)
# what a key can drive: a load, or a room thermostat's set-point (key mode `temperature`)
KEY_TARGET_TYPES: tuple[type[Device], ...] = (Light, Socket, Blind, Thermostat)
SCENE_LOAD_TYPES: tuple[type[Device], ...] = (Light, Socket, Blind, Thermostat)
ONOFF_LOAD_TYPES: tuple[type[Device], ...] = (Light, Socket)
# every load hosts a JH Scheduler, a room thermostat too
SCHEDULE_LOAD_TYPES: tuple[type[Device], ...] = (Light, Socket, Blind, Thermostat)

CONFIGURATORS: HassKey[dict[str, MeshConfigurator]] = HassKey(f"{DOMAIN}_mesh_config")

_ENTRY_FIELD: dict[str | vol.Marker, Any] = {vol.Optional(ATTR_CONFIG_ENTRY): cv.string}
_DRY_RUN_FIELD: dict[str | vol.Marker, Any] = {
    vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean
}
# the destructive actions without a `force` of their own: it skips the pre-flight comparison only
_FORCE_FIELD: dict[str | vol.Marker, Any] = {
    vol.Optional(ATTR_FORCE, default=False): cv.boolean
}
# a room by name or by the area named like it; a scene by name / number or by its scene entity
_ROOM_FIELDS: dict[str | vol.Marker, Any] = {
    vol.Optional(ATTR_ROOM): cv.string,
    vol.Optional(ATTR_ROOM_AREA): cv.string,
}
_SCENE_FIELDS: dict[str | vol.Marker, Any] = {
    vol.Optional(ATTR_SCENE): cv.string,
    vol.Optional(ATTR_SCENE_ENTITY): cv.entity_id,
}
_ONE_ROOM = (
    cv.has_at_least_one_key(ATTR_ROOM, ATTR_ROOM_AREA),
    cv.has_at_most_one_key(ATTR_ROOM, ATTR_ROOM_AREA),
)
_ONE_SCENE = (
    cv.has_at_least_one_key(ATTR_SCENE, ATTR_SCENE_ENTITY),
    cv.has_at_most_one_key(ATTR_SCENE, ATTR_SCENE_ENTITY),
)


_PERCENT = vol.All(vol.Coerce(int), vol.Range(min=0, max=100))
# what a load is set to: the lamps' and sockets' state, a blind's position and slats, a thermostat's set-point
# (the schedule and scene actions, `schedules.schedule_action`)
_STATE_FIELDS: dict[str | vol.Marker, Any] = {
    vol.Optional("action"): vol.In(SCHEDULE_ACTIONS),
    vol.Optional("brightness_pct"): _PERCENT,
    vol.Optional("color_temp_kelvin"): vol.All(
        vol.Coerce(int), vol.Range(min=2000, max=10000)
    ),
    vol.Optional("position"): _PERCENT,
    vol.Optional("tilt_position"): _PERCENT,
    vol.Optional("temperature"): vol.All(vol.Coerce(float), vol.Range(min=5, max=30)),
}
STATE_FIELDS = tuple(str(marker) for marker in _STATE_FIELDS)
# the actions that take their targets alone: `get_schedules`, `delete_threshold`
TARGETS_SCHEMA = vol.All(
    vol.Schema(cv.ENTITY_SERVICE_FIELDS),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
)


def _validation(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )


# ------------------------------------------------------------------ registration


@callback
def async_register_configurator(
    hass: HomeAssistant, entry: JungHomeConfigEntry
) -> None:
    """Create the entry's configurator (the services themselves are registered once, in async_setup)."""
    hass.data.setdefault(CONFIGURATORS, {})[entry.entry_id] = MeshConfigurator(
        entry.runtime_data
    )
    jung_data(hass)


@callback
def async_unregister_configurator(
    hass: HomeAssistant, entry: JungHomeConfigEntry
) -> None:
    """Drop the entry's configurator; the services stay registered and answer "not loaded"."""
    hass.data.get(CONFIGURATORS, {}).pop(entry.entry_id, None)


def _bound(
    hass: HomeAssistant,
    handler: Callable[
        [HomeAssistant, ServiceCall], Coroutine[Any, Any, ServiceResponse]
    ],
) -> Callable[[ServiceCall], Coroutine[Any, Any, ServiceResponse]]:
    async def call(service_call: ServiceCall) -> ServiceResponse:
        return await handler(hass, service_call)

    return call


# ------------------------------------------------------------------ resolving ids


def _hub(hass: HomeAssistant, entry_id: str) -> JungHomeHub:
    """Return the loaded hub of one of our entries; a typo'd or foreign id is told apart from a reloading entry."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise _validation("service_unknown_entry", id=entry_id)
    if entry.state is not ConfigEntryState.LOADED:
        raise _validation("service_entry_not_loaded")
    hub: JungHomeHub = entry.runtime_data
    return hub


def _configurator(hass: HomeAssistant, entry_id: str) -> MeshConfigurator:
    _hub(hass, entry_id)  # loaded, ours
    configurator = hass.data.get(CONFIGURATORS, {}).get(entry_id)
    if configurator is None:
        raise _validation("service_entry_not_loaded")
    return configurator


# ------------------------------------------------------------------ running an operation


_lock = entry_lock


async def _run(
    hass: HomeAssistant,
    entry_id: str,
    operation: Callable[[MeshConfigurator], Coroutine[Any, Any, bool]],
    *,
    needs_link: bool = True,
    reload: bool = False,
    scenes: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Run `operation` on the entry's configurator, then have the hub follow the export when the device model changed.

    The hub takes the new export over in place (`model_update`): no entity goes `unavailable`, the
    link stays up. `reload`: set the entry up again instead, for what changes the nodes the hub was built with
    (adding or removing a node with Home Assistant). `scenes`: the operation stored or deleted scenes on the
    devices, so their actions are read again (the export does not hold them). An operation that goes on air (`needs_link`) first waits for
    the entry's proxy link: a reload, or an ordinary reconnect, leaves the hub without one for as long as a real
    BLE connect takes.

    Answers what the call's plans did (`MeshConfigurator.plan_response`); a call that ran one is reported
    (`_report_plan`: the logbook, the diagnostics, and the error's placeholders when it stopped). `force`: the
    call's `force` field — its plans skip the pre-flight comparison of the nodes with the export
    (`PlanExecutor.preflight`).
    """
    async with _lock(hass, entry_id):
        configurator = _configurator(hass, entry_id)
        if needs_link:
            configurator = await _wait_for_link(hass, entry_id, configurator)
        # the flags of this call only: a call that fails before planning must not follow an earlier one's write
        configurator.recorded = configurator.adopted = False
        configurator.outcome = PlanOutcome()
        try:
            with configurator.forcing(force):
                changed = await operation(configurator)
        except BaseException as err:
            _report_plan(hass, entry_id, configurator, err)
            # a stopped plan raises after recording what the mesh accepted, and so does a cancelled one: the
            # device model must follow the export all the same, still under the lock — the cancellation
            # (or the error) goes on once the model followed
            if configurator.recorded:
                await _follow(hass, entry_id, reload=reload, scenes=scenes)
            raise
        _report_plan(hass, entry_id, configurator, None)
        if changed:
            await _follow(hass, entry_id, reload=reload, scenes=scenes)
        return configurator.plan_response()


async def _execute(
    hass: HomeAssistant,
    call: ServiceCall,
    entry_id: str,
    operation: Callable[[MeshConfigurator], Coroutine[Any, Any, bool]],
    *,
    needs_link: bool = True,
    reload: bool = False,
    scenes: bool = False,
) -> dict[str, Any]:
    """`_run` the operation, or with `dry_run` only plan it (`MeshConfigurator.dry_run`), under the entry's lock.

    A dry run writes and adopts nothing, and sends nothing but the pre-flight reads of a destructive plan
    (`PlanExecutor.preflight`), so it neither waits for the link nor has anything to follow; without a link it
    answers those reads as unanswered. The call's `force` skips them, as in a real run.
    """
    force = bool(call.data.get(ATTR_FORCE))
    if not call.data.get(ATTR_DRY_RUN):
        return await _run(
            hass,
            entry_id,
            operation,
            needs_link=needs_link,
            reload=reload,
            scenes=scenes,
            force=force,
        )
    async with _lock(hass, entry_id):
        configurator = _configurator(hass, entry_id)
        with configurator.forcing(force):
            return await configurator.dry_run(operation)


def _answer(call: ServiceCall, results: list[dict[str, Any]]) -> ServiceResponse:
    """Answer for a call that ran per entry: its entries' answers added up (lists joined), when asked for."""
    if not call.return_response:
        return None
    out: dict[str, Any] = {}
    for result in results:
        for key, value in result.items():
            before = out.get(key)
            out[key] = (
                value
                if before is None
                else (before or value)
                if isinstance(value, bool)
                else before + value
            )
    return out


@callback
def _report_plan(
    hass: HomeAssistant,
    entry_id: str,
    configurator: MeshConfigurator,
    err: BaseException | None,
) -> None:
    """Report a call that ran a plan: a logbook line (`EVENT_PLAN`), the diagnostics' history, the error's placeholders.

    A finished call is worded by its summary (`PlanOutcome.summary`, "Key 0151 (…) now drives room Kitchen; 6
    messages"), a stopped one by how far it got and its error, a cancelled one by how far it got. A stopped call's
    error gets `outcome_applied`, `outcome_total`, `outcome_recorded` and `outcome_nodes` (the response's fields, which it never
    gets to) in its placeholders. A call that sent nothing and has no summary (a rename, a dry run) is not reported.
    """
    outcome = configurator.outcome
    if outcome.total == 0 and (err is not None or outcome.summary is None):
        return
    placeholders = {
        "action": outcome.action or "",
        "applied": str(outcome.applied),
        "total": str(outcome.total),
        "messages": str(outcome.applied),
    }
    if err is None:
        key, extra = outcome.summary or ("plan_finished", {})
        placeholders |= extra
        result = "finished"
    elif isinstance(err, asyncio.CancelledError):
        key, result = "plan_cancelled", "cancelled"
    else:
        if isinstance(err, HomeAssistantError):
            response = configurator.plan_response()
            err.translation_placeholders = {
                **(err.translation_placeholders or {}),
                "outcome_applied": str(response["applied"]),
                "outcome_total": str(response["total"]),
                "outcome_recorded": str(response["recorded"]).lower(),
                "outcome_nodes": ", ".join(response["nodes"]) or "-",
            }
        key, result = "plan_stopped", "stopped"
        placeholders["error"] = str(err) or type(err).__name__
    plan_history(hass, entry_id).append(
        {
            "action": outcome.action,
            "outcome": result,
            "applied": outcome.applied,
            "total": outcome.total,
            "steps": list(outcome.steps),
            "error": getattr(err, "translation_key", None),
        }
    )
    entry = hass.config_entries.async_get_entry(entry_id)
    hass.bus.async_fire(
        EVENT_PLAN,
        {
            ATTR_ENTRY_ID: entry_id,
            ATTR_NAME: entry.title if entry is not None else DOMAIN,
            "action": outcome.action,
            "outcome": result,
            "message": key,
            "placeholders": placeholders,
        },
    )


async def _follow(
    hass: HomeAssistant, entry_id: str, *, reload: bool, scenes: bool
) -> None:
    """Have the entry follow the export after a change, to the end even when the call is cancelled meanwhile.

    In place (`model_update.async_follow_export`, which reloads when it cannot), or by a reload (`reload`): either
    cut off half-way would leave the entry behind its export, or unloaded (`run_to_end`). While Home Assistant
    stops there is nothing to follow: the next start sets the entry up from the export as it was written.
    """
    if hass.is_stopping:
        return
    await run_to_end(
        hass.config_entries.async_reload(entry_id)
        if reload
        else async_follow_export(hass, entry_id, scenes=scenes)
    )


async def async_configure(
    hass: HomeAssistant,
    entry_id: str,
    operation: Callable[[MeshConfigurator], Coroutine[Any, Any, bool]],
    *,
    needs_link: bool = True,
) -> None:
    """Run a configurator operation for an entity the way the actions run theirs: locked, on a live link, followed.

    `needs_link=False` for an operation that sends nothing (a rename): it runs without waiting for the link.
    """
    await _run(hass, entry_id, operation, needs_link=needs_link)


async def _wait_for_link(
    hass: HomeAssistant, entry_id: str, configurator: MeshConfigurator
) -> MeshConfigurator:
    """Wait (bounded) for the entry's link; return the configurator of the hub that has it.

    An options or reconfigure reload does not take the service lock, so it can replace the hub during the wait,
    and a hub torn down while disconnected never connects again: the wait goes in `LINK_WAIT_SLICE` steps and
    looks the entry's configurator up again after each, so it follows the replacement (an entry mid-reload,
    briefly not loaded, just costs a step).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SERVICE_LINK_WAIT
    while True:
        step = max(min(deadline - loop.time(), LINK_WAIT_SLICE), 0.0)
        connected = await configurator.hub.async_wait_connected(step)
        try:
            current = _configurator(hass, entry_id)
        except ServiceValidationError:
            # reloading right now; the old hub may still say "connected" while it stops, so wait out the step
            if loop.time() >= deadline:
                raise
            await asyncio.sleep(step)
            continue
        if connected and current is configurator:
            return current
        if loop.time() >= deadline:
            # the mesh out of reach is no fault of the call's data: not a validation error
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="service_not_connected"
            )
        configurator = current
