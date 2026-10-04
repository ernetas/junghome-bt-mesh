"""The scene actions, `create_scene` … `delete_unused_scenes`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from custom_components.junghome_ble.const import DEFAULT_UNUSED_SCENES_DRY_RUN, DOMAIN
from custom_components.junghome_ble.conversions import temperature_to_level
from custom_components.junghome_ble.entity import load_entity_id
from custom_components.junghome_ble.errors import mesh_errors
from custom_components.junghome_ble.jhmesh import vendor_models as V
from custom_components.junghome_ble.jhmesh.devices import Blind, Device, Thermostat
from custom_components.junghome_ble.mesh_config import (
    MeshConfigurator,
    scene_action_for,
)
from custom_components.junghome_ble.schedules import ActionError, schedule_action

from .common import (
    _DRY_RUN_FIELD,
    _ENTRY_FIELD,
    _FORCE_FIELD,
    _ONE_SCENE,
    _SCENE_FIELDS,
    _STATE_FIELDS,
    ATTR_CONFIG_ENTRY,
    ATTR_CONFIRM,
    ATTR_DRY_RUN,
    ATTR_FORCE,
    ATTR_NAME,
    ATTR_NEW_NAME,
    ATTR_SCENE_ENTITY,
    STATE_FIELDS,
    _answer,
    _execute,
    _hub,
    _run,
    _validation,
)
from .resolve import (
    Load,
    _entry_for_hub_services,
    _resolve_loads,
    _same_network,
    _scene_of,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse

    from custom_components.junghome_ble.coordinator import JungHomeHub


CREATE_SCENE_SCHEMA = vol.Schema(
    {vol.Required(ATTR_NAME): cv.string, **_DRY_RUN_FIELD, **_ENTRY_FIELD}
)
RENAME_SCENE_SCHEMA = vol.All(
    vol.Schema(
        {
            **_SCENE_FIELDS,
            vol.Required(ATTR_NEW_NAME): cv.string,
            **_ENTRY_FIELD,
        }
    ),
    *_ONE_SCENE,
    cv.has_at_most_one_key(ATTR_SCENE_ENTITY, ATTR_CONFIG_ENTRY),
)
# why a state does not fit a load (`schedules.ActionError.key`), in the words of `store_scene` rather than a schedule's
SCENE_STATE_ERRORS = {
    "schedule_action_not_applicable": "scene_state_not_applicable",
    "schedule_action_needs_temperature": "scene_state_needs_temperature",
    "schedule_action_needs_position": "scene_state_needs_position",
    "schedule_action_needs_action": "scene_state_needs_action",
    "schedule_action_brightness_off": "scene_state_brightness_off",
}
STORE_SCENE_SCHEMA = vol.All(
    vol.Schema(
        {
            **_SCENE_FIELDS,
            **_STATE_FIELDS,
            **cv.ENTITY_SERVICE_FIELDS,
        }
    ),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
    *_ONE_SCENE,
)
REMOVE_FROM_SCENE_SCHEMA = vol.All(
    vol.Schema({**_SCENE_FIELDS, **_FORCE_FIELD, **cv.ENTITY_SERVICE_FIELDS}),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
    *_ONE_SCENE,
)
DELETE_SCENE_SCHEMA = vol.All(
    vol.Schema(
        {
            **_SCENE_FIELDS,
            vol.Optional(ATTR_FORCE, default=False): cv.boolean,
            vol.Optional(ATTR_CONFIRM, default=False): cv.boolean,
            **_DRY_RUN_FIELD,
            **_ENTRY_FIELD,
        }
    ),
    *_ONE_SCENE,
    cv.has_at_most_one_key(ATTR_SCENE_ENTITY, ATTR_CONFIG_ENTRY),
)
ATTR_NUMBERS = "numbers"
ATTR_CONFIRM_STALE_EXPORT = "confirm_stale_export"
DELETE_UNUSED_SCENES_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DRY_RUN, default=DEFAULT_UNUSED_SCENES_DRY_RUN): cv.boolean,
        vol.Optional(ATTR_NUMBERS): vol.All(
            cv.ensure_list,
            vol.Length(min=1),
            [vol.All(vol.Coerce(int), vol.Range(min=1, max=0xFFFF))],
        ),
        vol.Optional(ATTR_CONFIRM_STALE_EXPORT, default=False): cv.boolean,
        **_ENTRY_FIELD,
    }
)


def _scene_entry(hass: HomeAssistant, data: Mapping[str, Any]) -> tuple[str, str]:
    """Return the entry and scene of a scene-only action: the scene entity's, or the entry `config_entry_id` picks."""
    owner, scene = _scene_of(hass, data)
    if owner is None:
        return _entry_for_hub_services(hass, data), scene
    _hub(hass, owner)  # loaded
    return owner, scene


# ------------------------------------------------------------------ scenes


async def _create_scene(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Create an empty scene in the export; answers `{"scene": number, "name"}` when a response is asked for."""
    entry_id = _entry_for_hub_services(hass, call.data)
    name: str = call.data[ATTR_NAME]
    created: dict[str, Any] = {}

    async def operation(configurator: MeshConfigurator) -> bool:
        created.update(scene=await configurator.create_scene(name), name=name)
        return True  # a new scene entity

    result = await _execute(hass, call, entry_id, operation, needs_link=False)
    return _answer(call, [result, created])


async def _rename_scene(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry_id, scene = _scene_entry(hass, call.data)
    await _run(
        hass,
        entry_id,
        lambda c: c.rename_scene(scene, call.data[ATTR_NEW_NAME]),
        needs_link=False,
    )
    return None


async def _store_scene(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Store the targeted loads' *present* state into the scene (set them first, as in the app).

    Lights, sockets, blinds and thermostats; the last two are unverified on hardware. With state fields
    (`action`, `brightness_pct`, `color_temp_kelvin`, `position`, `tilt_position`, `temperature`) every load is
    first set to that state and waited for — but a blind, which takes too long to move: its JUNG scene action
    carries the `position` / `tilt_position` given (the app, too, carries a JUNG device's state in the action
    and sets only legacy devices first, network-features.md §3). A field that does not fit a load is refused
    by the schedule's rules (`schedule_action`), in the scene's words.
    """
    owner, scene = _scene_of(hass, call.data)
    loads = await _resolve_loads(hass, call)
    _same_network(owner, (load.entry_id for load in loads))
    wanted: dict[tuple[str, int], V.Action] = {}
    if any(name in call.data for name in STATE_FIELDS):
        for load in loads:  # every load checked before anything is sent
            device = _hub(hass, load.entry_id).devices.by_address[load.address]
            try:
                wanted[load.entry_id, load.address] = schedule_action(
                    device.kind, call.data
                )
            except ActionError as err:
                raise _validation(
                    SCENE_STATE_ERRORS[err.key],
                    name=load_entity_id(hass, device),
                    **err.placeholders,
                ) from err
    for entry_id in sorted({load.entry_id for load in loads}):
        mine = [load for load in loads if load.entry_id == entry_id]

        states = {a: v for (e, a), v in wanted.items() if e == entry_id}

        async def operation(
            configurator: MeshConfigurator,
            mine: list[Load] = mine,
            states: dict[int, V.Action] = states,
        ) -> bool:
            # the hub the lock handed us (a previous call's reload replaces it), and the loads' present state:
            # a freshly reloaded hub's cache is empty until its connect-time refresh, so ask the load itself
            hub = configurator.hub
            actions: list[tuple[int, V.Action | None]] = []
            for load in mine:
                device = hub.devices.by_address.get(load.address)
                if device is None:
                    raise _validation(
                        "service_unknown_device", id=f"{load.address:04X}"
                    )
                if (action := states.get(load.address)) is not None:
                    if isinstance(device, Blind):
                        actions.append((load.address, action))  # not moved: see above
                        continue
                    await _apply_state(hass, hub, device, action)
                actions.append((load.address, await _present_action(hub, device)))
            await configurator.store_scenes(scene, actions)
            return True

        await _run(hass, entry_id, operation, scenes=True)
    return None


async def _apply_state(
    hass: HomeAssistant, hub: JungHomeHub, device: Device, action: V.Action
) -> None:
    """Set a load to the state `action` describes and wait until it reports having arrived (ramps included).

    The Sets are the entities' own (acknowledged and waited for, as the app sends them; a load that answers none is
    reported unreachable); the scene then records what the load reports, which may differ from the request by the
    load's own rounding.
    """
    address = device.address
    # a load that answered none of the Set's attempts is unreachable now; a lost link is a send failure
    with mesh_errors(
        timeout_key="device_not_reachable",
        placeholders=lambda: {"entity": load_entity_id(hass, device)},
    ):
        if action.code == V.ACTION_SWITCH:
            await hub.set_onoff(address, bool(action.on))
            kind = device.kind
        elif action.code == V.ACTION_TEMPERATURE:
            await hub.set_level(
                address, temperature_to_level(action.temperature_c or 0)
            )
            kind = "level"
        elif action.code == V.ACTION_LIGHTNESS_CT:
            st = hub.states.get(address)
            kelvin = action.temperature_k or 0
            if st is not None and st.kelvin_min and st.kelvin_max:
                kelvin = max(
                    st.kelvin_min, min(st.kelvin_max, kelvin)
                )  # the light's own range
            await hub.set_ctl(address, action.lightness or 0, kelvin)
            kind = "ctl"
        else:
            await hub.set_lightness(address, action.lightness or 0)
            kind = "dimmer"
    if not await hub.async_wait_settled(address, kind):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="scene_state_not_reached",
            translation_placeholders={"name": load_entity_id(hass, device)},
        )


async def _present_action(hub: JungHomeHub, device: Device) -> V.Action | None:
    """Return the scene action of the load's present state; the load (and a blind's slats) asked when not known.

    A freshly reloaded hub's cache is empty until its connect-time refresh. Blinds and thermostats answer a
    Generic Level Get (position, slats, set-point), the lights their own kind's Get.
    """
    slat_address = device.slat_address if isinstance(device, Blind) else None

    def present() -> V.Action | None:
        state = hub.states.get(device.address)
        slat = None if slat_address is None else hub.element_state(slat_address)
        return None if state is None else scene_action_for(device.kind, state, slat)

    if (action := present()) is None:
        level = isinstance(device, (Blind, Thermostat))
        await hub.async_refresh_element(
            device.address, "level" if level else device.kind
        )
        if slat_address is not None:
            await hub.async_refresh_element(slat_address, "level")
        action = present()
    return action


async def _remove_from_scene(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    owner, scene = _scene_of(hass, call.data)
    loads = await _resolve_loads(hass, call)
    _same_network(owner, (load.entry_id for load in loads))
    for entry_id in sorted({load.entry_id for load in loads}):
        mine = [load for load in loads if load.entry_id == entry_id]

        async def operation(
            configurator: MeshConfigurator, mine: list[Load] = mine
        ) -> bool:
            await configurator.remove_from_scenes(
                scene, [load.address for load in mine]
            )
            return True

        await _run(hass, entry_id, operation, scenes=True, force=call.data[ATTR_FORCE])
    return None


async def _delete_scene(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Delete a scene from its members and the export; answers `{"skipped": ["0232"], …}` when a response is asked for.

    `skipped`: the members `force` passed over, which still hold the scene (the `scene_held` repair names them),
    beside what the keys' plan applied. `force` deletes a scene some members cannot forget, which cannot be
    undone: it needs `confirm` (a dry run does not).
    """
    force: bool = call.data[ATTR_FORCE]
    if force and not call.data[ATTR_CONFIRM] and not call.data[ATTR_DRY_RUN]:
        raise _validation("delete_scene_force_needs_confirm")
    entry_id, scene = _scene_entry(hass, call.data)
    skipped: list[str] = []

    async def operation(configurator: MeshConfigurator) -> bool:
        skipped.extend(await configurator.delete_scene(scene, force=force))
        return True

    result = await _execute(hass, call, entry_id, operation, scenes=True)
    return _answer(
        call, [result, {} if call.data[ATTR_DRY_RUN] else {"skipped": skipped}]
    )


async def _delete_unused_scenes(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Delete the scene numbers the export does not know from every node's register (the app's `DeleteUnusedScenes`).

    Answers `{"<register element>": [numbers], "unanswered": [elements]}` when a response is asked for: what was
    deleted, or with `dry_run` (the default) what would be; nothing in the export changes, so nothing reloads.
    """
    entry_id = _entry_for_hub_services(hass, call.data)
    result: dict[str, Any] = {}

    async def operation(configurator: MeshConfigurator) -> bool:
        result.update(
            await configurator.delete_unused_scenes(
                dry_run=call.data[ATTR_DRY_RUN],
                numbers=call.data.get(ATTR_NUMBERS),
                confirm_stale_export=call.data[ATTR_CONFIRM_STALE_EXPORT],
            )
        )
        return False

    await _run(hass, entry_id, operation)
    return result if call.return_response else None
