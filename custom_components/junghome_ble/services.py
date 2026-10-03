"""Service actions: rooms, key connections, scenes, schedules, thresholds (`junghome_ble.set_room` … `delete_threshold`).

The services are registered once per Home Assistant run, in `async_setup` (`async_setup_services`); they stay
registered and answer "not loaded" while no entry is loaded. A call resolves the
Home Assistant ids it was given — device ids, entity ids, areas — to mesh elements through the registries and the
device-identifier scheme documented in `entity.py` (`{uuid}-{location:04x}` loads, `{uuid}-{location:04x}-buttons`
gangs of keys, `node:{uuid}` nodes), hands the operation to the hub's `MeshConfigurator`, and finally reloads the
config entry so the device model follows the rewritten export. The schedule actions write no export: they go to
the loads' own JH Scheduler (`schedules.py`) and reload nothing; a socket threshold is a property plus wiring
(`thresholds.py`), and reloads only when the wiring changed. `audit_network` only reads (`jhmesh.audit`): it answers
what the nodes' Configuration Servers hold against the export and changes nothing. The dimming actions
(`start_dim` / `stop_dim` / `step_dim`) are entity actions of the light platform (`light.py`): they send one
command to a dimmer and write nothing. Every action but the reading ones (`USER_SERVICES`) and the dimming ones is
for administrators only (review-4 W4-9).

Calls for one entry are serialised (`coordinator.ENTRY_LOCKS`, kept across reloads, taken by the unknown-node
refresh's reload too) so a call never runs against a hub that is being torn down by the reload of the previous
one; a call that goes on air then waits (`SERVICE_LINK_WAIT`)
for the reloaded hub's link, which connects in the background.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import voluptuous as vol
from homeassistant.components.light.const import DOMAIN as LIGHT_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ENTITY_MATCH_ALL
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import (
    async_register_admin_service,
    async_register_platform_entity_service,
)
from homeassistant.helpers.target import (
    TargetSelection,
    async_extract_referenced_entity_ids,
)
from homeassistant.util.hass_dict import HassKey

from . import onboard
from .climate import temperature_to_level
from .const import (
    ATTR_KEY,
    ATTR_SCENE,
    DEFAULT_ALLOW_PROVISIONING,
    DEFAULT_UNUSED_SCENES_DRY_RUN,
    DIM_DEFAULT_SPEED,
    DOMAIN,
    OPTION_ALLOW_PROVISIONING,
    SERVICE_LINK_WAIT,
)
from .coordinator import ENTRY_LOCKS, entry_lock
from .entity import (
    button_gang,
    buttons_device_id,
    load_entity_id,
    mesh_identifier,
    node_identifier,
)
from .jhmesh import vendor_models as V
from .jhmesh.audit import report
from .jhmesh.devices import (
    BATTERY_PIDS,
    GATEWAY_PID,
    Blind,
    Button,
    Device,
    Light,
    Socket,
    Thermostat,
)
from .light import (
    ATTR_DIRECTION,
    ATTR_SPEED,
    ATTR_STEP,
    DIM_DIRECTIONS,
    async_start_dim,
    async_step_dim,
    async_stop_dim,
)
from .mesh_config import MODES, MeshConfigurator, run_to_end, scene_action_for
from .schedules import (
    TRIGGERS,
    ActionError,
    Scheduler,
    schedule_action,
    scheduler,
)
from .thresholds import (
    CLEARED,
    OTHER_THRESHOLD,
    THRESHOLD_PROPERTIES,
    Which,
    current_threshold,
    has_thresholds,
    planned_threshold,
    write_threshold,
)

if TYPE_CHECKING:
    from . import JungHomeConfigEntry
    from .coordinator import JungHomeHub
    from .identity import VaultKeeper

SERVICE_SET_ROOM = "set_room"
SERVICE_CREATE_ROOM = "create_room"
SERVICE_RENAME_ROOM = "rename_room"
SERVICE_DELETE_ROOM = "delete_room"
SERVICE_ASSIGN_KEY = "assign_key"
SERVICE_CLEAR_KEY = "clear_key"
SERVICE_CREATE_SCENE = "create_scene"
SERVICE_RENAME_SCENE = "rename_scene"
SERVICE_STORE_SCENE = "store_scene"
SERVICE_REMOVE_FROM_SCENE = "remove_from_scene"
SERVICE_DELETE_SCENE = "delete_scene"
SERVICE_DELETE_UNUSED_SCENES = "delete_unused_scenes"
SERVICE_SYNC_GATEWAY = "sync_gateway"
SERVICE_GET_SCHEDULES = "get_schedules"
SERVICE_CREATE_SCHEDULE = "create_schedule"
SERVICE_ENABLE_SCHEDULE = "enable_schedule"
SERVICE_DISABLE_SCHEDULE = "disable_schedule"
SERVICE_DELETE_SCHEDULE = "delete_schedule"
SERVICE_SET_THRESHOLD = "set_threshold"
SERVICE_DELETE_THRESHOLD = "delete_threshold"
SERVICE_AUDIT_NETWORK = "audit_network"
SERVICE_EXPORT_NETWORK = (
    "export_network"  # admin only: the answer holds every key of the mesh
)
SERVICE_FIND_NEW_DEVICES = "find_new_devices"
SERVICE_REMOVE_DEVICE = "remove_device"  # admin only, with OPTION_ALLOW_PROVISIONING
SERVICE_ADD_DEVICE = (
    "add_device"  # admin only, and only with OPTION_ALLOW_PROVISIONING (experimental)
)
# admin only, with OPTION_ALLOW_PROVISIONING: a device add_device provisioned but did not record (review-4 D2)
SERVICE_RESET_PENDING_DEVICE = "reset_pending_device"
SERVICE_START_DIM = "start_dim"
SERVICE_STOP_DIM = "stop_dim"
SERVICE_STEP_DIM = "step_dim"
# the services that answer, and whether they must be asked to
RESPONSES: dict[str, SupportsResponse] = {
    SERVICE_CREATE_SCENE: SupportsResponse.OPTIONAL,
    SERVICE_DELETE_SCENE: SupportsResponse.OPTIONAL,
    SERVICE_DELETE_UNUSED_SCENES: SupportsResponse.OPTIONAL,
    SERVICE_GET_SCHEDULES: SupportsResponse.ONLY,
    SERVICE_CREATE_SCHEDULE: SupportsResponse.OPTIONAL,
    SERVICE_AUDIT_NETWORK: SupportsResponse.ONLY,
    SERVICE_EXPORT_NETWORK: SupportsResponse.ONLY,
    SERVICE_FIND_NEW_DEVICES: SupportsResponse.ONLY,
    SERVICE_ADD_DEVICE: SupportsResponse.OPTIONAL,
    SERVICE_RESET_PENDING_DEVICE: SupportsResponse.OPTIONAL,
}
# Review-4 W4-9 (decision M8): every other action rewires, deletes or writes the export and the devices, and is for
# administrators only (`async_register_admin_service`); these only read. Moving a name here opens it to every user.
USER_SERVICES = frozenset(
    {SERVICE_GET_SCHEDULES, SERVICE_AUDIT_NETWORK, SERVICE_FIND_NEW_DEVICES}
)

ATTR_ROOM = "room"
ATTR_NAME = "name"
ATTR_NEW_NAME = "new_name"
ATTR_CONFIG_ENTRY = "config_entry_id"
ATTR_KEY_ENTITY = "key_entity"
ATTR_KEY_DEVICE = "key_device"
ATTR_TARGET_ENTITY = "target_entity"
ATTR_TARGET_DEVICE = "target_device"
ATTR_MODE = "mode"
ATTR_SLOT = "slot"
ATTR_TRIGGER = "trigger"
ATTR_TIME = "time"
ASTRO_FIELDS = ("not_before", "not_after", "offset")
SCHEDULE_ACTIONS = ("on", "off")
ATTR_THRESHOLD = "threshold"
ATTR_DEVICES = "devices"
ATTR_DEVICE = "device"
THRESHOLD_POWER_MAX = 1677721.4  # W: 24 bits of 0.1 W, all ones meaning "none"

KEY_LETTERS = (
    "A",
    "B",
    "C",
    "D",
    "E1",
    "E2",
)  # push-button keys, a mini actuator's inputs
LINK_WAIT_SLICE = (
    1.0  # seconds between looks at which hub the entry has, while waiting for a link
)
# What a key can drive and a room can hold (a blind moves: KeyMode *move*, `mesh_config.derive_mode`); the scene
# services take thermostats too, each with its own scene action record (`scene_action_for`); a threshold switches
# what has an OnOff server (lights and sockets).
LOAD_TYPES: tuple[type[Device], ...] = (Light, Socket, Blind)
SCENE_LOAD_TYPES: tuple[type[Device], ...] = (Light, Socket, Blind, Thermostat)
ONOFF_LOAD_TYPES: tuple[type[Device], ...] = (Light, Socket)
# every load hosts a JH Scheduler, a room thermostat too
SCHEDULE_LOAD_TYPES: tuple[type[Device], ...] = (Light, Socket, Blind, Thermostat)

CONFIGURATORS: HassKey[dict[str, MeshConfigurator]] = HassKey(f"{DOMAIN}_mesh_config")

_KEY_FIELDS: dict[vol.Marker, Any] = {
    vol.Optional(ATTR_KEY_ENTITY): cv.entity_id,
    vol.Optional(ATTR_KEY_DEVICE): cv.string,
    vol.Optional(ATTR_KEY): vol.All(cv.string, vol.Upper, vol.In(KEY_LETTERS)),
}
_ENTRY_FIELD: dict[vol.Marker, Any] = {vol.Optional(ATTR_CONFIG_ENTRY): cv.string}


def _key_only_with_device(data: dict[str, Any]) -> dict[str, Any]:
    """Refuse `key` next to `key_entity`: the letter picks a key of a `key_device`; an event entity is one key."""
    if ATTR_KEY in data and ATTR_KEY_ENTITY in data:
        raise vol.Invalid("`key` goes with `key_device`, not with `key_entity`")
    return data


SET_ROOM_SCHEMA = vol.All(
    vol.Schema({vol.Required(ATTR_ROOM): cv.string, **cv.ENTITY_SERVICE_FIELDS}),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
)
CREATE_ROOM_SCHEMA = vol.Schema({vol.Required(ATTR_NAME): cv.string, **_ENTRY_FIELD})
RENAME_ROOM_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ROOM): cv.string,
        vol.Required(ATTR_NEW_NAME): cv.string,
        **_ENTRY_FIELD,
    }
)
DELETE_ROOM_SCHEMA = vol.Schema({vol.Required(ATTR_ROOM): cv.string, **_ENTRY_FIELD})
ASSIGN_KEY_SCHEMA = vol.All(
    vol.Schema(
        {
            **_KEY_FIELDS,
            vol.Optional(ATTR_TARGET_ENTITY): cv.entity_id,
            vol.Optional(ATTR_TARGET_DEVICE): cv.string,
            vol.Optional(ATTR_ROOM): cv.string,
            vol.Optional(ATTR_SCENE): cv.string,
            vol.Optional(ATTR_MODE): vol.In(MODES),
        }
    ),
    cv.has_at_least_one_key(ATTR_KEY_ENTITY, ATTR_KEY_DEVICE),
    cv.has_at_most_one_key(ATTR_KEY_ENTITY, ATTR_KEY_DEVICE),
    cv.has_at_least_one_key(
        ATTR_TARGET_ENTITY, ATTR_TARGET_DEVICE, ATTR_ROOM, ATTR_SCENE
    ),
    cv.has_at_most_one_key(
        ATTR_TARGET_ENTITY, ATTR_TARGET_DEVICE, ATTR_ROOM, ATTR_SCENE
    ),
    _key_only_with_device,
)
CLEAR_KEY_SCHEMA = vol.All(
    vol.Schema(_KEY_FIELDS),
    cv.has_at_least_one_key(ATTR_KEY_ENTITY, ATTR_KEY_DEVICE),
    cv.has_at_most_one_key(ATTR_KEY_ENTITY, ATTR_KEY_DEVICE),
    _key_only_with_device,
)
CREATE_SCENE_SCHEMA = vol.Schema({vol.Required(ATTR_NAME): cv.string, **_ENTRY_FIELD})
RENAME_SCENE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SCENE): cv.string,
        vol.Required(ATTR_NEW_NAME): cv.string,
        **_ENTRY_FIELD,
    }
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
            vol.Required(ATTR_SCENE): cv.string,
            **_STATE_FIELDS,
            **cv.ENTITY_SERVICE_FIELDS,
        }
    ),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
)
REMOVE_FROM_SCENE_SCHEMA = vol.All(
    vol.Schema({vol.Required(ATTR_SCENE): cv.string, **cv.ENTITY_SERVICE_FIELDS}),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
)
ATTR_FORCE = "force"
DELETE_SCENE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SCENE): cv.string,
        vol.Optional(ATTR_FORCE, default=False): cv.boolean,
        **_ENTRY_FIELD,
    }
)
ATTR_DRY_RUN = "dry_run"
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
SYNC_GATEWAY_SCHEMA = vol.Schema(_ENTRY_FIELD)
ATTR_FLAVOUR = "flavour"
EXPORT_FLAVOURS = ("share", "cdb")  # the app's share file, the mesh database
EXPORT_NETWORK_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_FLAVOUR, default="share"): vol.In(EXPORT_FLAVOURS),
        **_ENTRY_FIELD,
    }
)
ATTR_ADDRESS = "address"
REMOVE_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE): cv.string,
        vol.Optional(ATTR_FORCE, default=False): cv.boolean,
    }
)
FIND_NEW_DEVICES_SCHEMA = vol.Schema({})
ADD_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ADDRESS): cv.string,
        vol.Required(ATTR_NAME): cv.string,
        **_ENTRY_FIELD,
    }
)
ATTR_UUID = "uuid"
ATTR_UNICAST = "unicast"


def _unicast(value: Any) -> int:
    """Return the primary unicast address hexadecimal text names (`0D20`, `0x0D20`)."""
    try:
        address = int(cv.string(value), 16)
    except ValueError as err:
        raise vol.Invalid("not a hexadecimal address") from err
    if not 0x0001 <= address <= 0x7FFF:
        raise vol.Invalid("not a unicast address (0001-7FFF)")
    return address


RESET_PENDING_DEVICE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(ATTR_UUID): cv.string,
            vol.Optional(ATTR_UNICAST): _unicast,
            vol.Optional(ATTR_FORCE, default=False): cv.boolean,
            **_ENTRY_FIELD,
        }
    ),
    cv.has_at_least_one_key(ATTR_UUID, ATTR_UNICAST),
)
# the dimming entity actions (`light.py`): a direction and a speed in % of the range per second, or a step in %
START_DIM_SCHEMA: dict[str | vol.Marker, Any] = {
    vol.Required(ATTR_DIRECTION): vol.In(DIM_DIRECTIONS),
    vol.Optional(ATTR_SPEED, default=DIM_DEFAULT_SPEED): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=100)
    ),
}
STOP_DIM_SCHEMA: dict[str | vol.Marker, Any] = {}
STEP_DIM_SCHEMA: dict[str | vol.Marker, Any] = {
    vol.Required(ATTR_STEP): vol.All(
        vol.Coerce(int), vol.Range(min=-100, max=100), vol.NotIn([0])
    ),
}
AUDIT_NETWORK_SCHEMA = vol.All(
    vol.Schema({vol.Optional(ATTR_DEVICE): cv.string, **_ENTRY_FIELD}),
    cv.has_at_most_one_key(ATTR_DEVICE, ATTR_CONFIG_ENTRY),
)


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
SET_THRESHOLD_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(ATTR_THRESHOLD): vol.In(THRESHOLD_PROPERTIES),
            vol.Optional("power"): vol.All(
                vol.Coerce(float), vol.Range(min=0, max=THRESHOLD_POWER_MAX)
            ),
            vol.Optional("duration"): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=0xFFFF)
            ),
            vol.Optional("enabled"): cv.boolean,
            vol.Optional(ATTR_DEVICES): cv.entity_ids,
            **cv.ENTITY_SERVICE_FIELDS,
        }
    ),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
)
DELETE_THRESHOLD_SCHEMA = GET_SCHEDULES_SCHEMA = vol.All(
    vol.Schema(cv.ENTITY_SERVICE_FIELDS),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
)
_CREATE_SCHEDULE_FIELDS: dict[str | vol.Marker, Any] = {
    vol.Required(ATTR_TRIGGER): vol.In(TRIGGERS),
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


@dataclass(frozen=True)
class Load:
    """A light / socket element behind a Home Assistant id, with its registry device (for the area)."""

    entry_id: str
    address: int
    device_id: str | None


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
    hass.data.setdefault(ENTRY_LOCKS, {})


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the services (once per Home Assistant run; handlers resolve the entry per call)."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_ROOM):
        return
    handlers: list[
        tuple[
            str,
            Callable[
                [HomeAssistant, ServiceCall], Coroutine[Any, Any, ServiceResponse]
            ],
            vol.Schema | vol.All,
        ]
    ] = [
        (SERVICE_SET_ROOM, _set_room, SET_ROOM_SCHEMA),
        (SERVICE_CREATE_ROOM, _create_room, CREATE_ROOM_SCHEMA),
        (SERVICE_RENAME_ROOM, _rename_room, RENAME_ROOM_SCHEMA),
        (SERVICE_DELETE_ROOM, _delete_room, DELETE_ROOM_SCHEMA),
        (SERVICE_ASSIGN_KEY, _assign_key, ASSIGN_KEY_SCHEMA),
        (SERVICE_CLEAR_KEY, _clear_key, CLEAR_KEY_SCHEMA),
        (SERVICE_CREATE_SCENE, _create_scene, CREATE_SCENE_SCHEMA),
        (SERVICE_RENAME_SCENE, _rename_scene, RENAME_SCENE_SCHEMA),
        (SERVICE_STORE_SCENE, _store_scene, STORE_SCENE_SCHEMA),
        (SERVICE_REMOVE_FROM_SCENE, _remove_from_scene, REMOVE_FROM_SCENE_SCHEMA),
        (SERVICE_DELETE_SCENE, _delete_scene, DELETE_SCENE_SCHEMA),
        (
            SERVICE_DELETE_UNUSED_SCENES,
            _delete_unused_scenes,
            DELETE_UNUSED_SCENES_SCHEMA,
        ),
        (SERVICE_SYNC_GATEWAY, _sync_gateway, SYNC_GATEWAY_SCHEMA),
        (SERVICE_GET_SCHEDULES, _get_schedules, GET_SCHEDULES_SCHEMA),
        (SERVICE_CREATE_SCHEDULE, _create_schedule, CREATE_SCHEDULE_SCHEMA),
        (SERVICE_ENABLE_SCHEDULE, _enable_schedule, ENABLE_SCHEDULE_SCHEMA),
        (SERVICE_DISABLE_SCHEDULE, _disable_schedule, DISABLE_SCHEDULE_SCHEMA),
        (SERVICE_DELETE_SCHEDULE, _delete_schedule, DELETE_SCHEDULE_SCHEMA),
        (SERVICE_SET_THRESHOLD, _set_threshold, SET_THRESHOLD_SCHEMA),
        (SERVICE_DELETE_THRESHOLD, _delete_threshold, DELETE_THRESHOLD_SCHEMA),
        (SERVICE_AUDIT_NETWORK, _audit_network, AUDIT_NETWORK_SCHEMA),
        (SERVICE_EXPORT_NETWORK, _export_network, EXPORT_NETWORK_SCHEMA),
        (SERVICE_FIND_NEW_DEVICES, _find_new_devices, FIND_NEW_DEVICES_SCHEMA),
        (SERVICE_REMOVE_DEVICE, _remove_device, REMOVE_DEVICE_SCHEMA),
        (SERVICE_ADD_DEVICE, _add_device, ADD_DEVICE_SCHEMA),
        (
            SERVICE_RESET_PENDING_DEVICE,
            _reset_pending_device,
            RESET_PENDING_DEVICE_SCHEMA,
        ),
    ]
    for name, handler, schema in handlers:
        if name in USER_SERVICES:
            hass.services.async_register(
                DOMAIN,
                name,
                _bound(hass, handler),
                schema=schema,
                supports_response=RESPONSES.get(name, SupportsResponse.NONE),
            )
        else:
            async_register_admin_service(
                hass,
                DOMAIN,
                name,
                _bound(hass, handler),
                schema=schema,
                supports_response=RESPONSES.get(name, SupportsResponse.NONE),
            )
    for name, func, entity_schema in (
        (SERVICE_START_DIM, async_start_dim, START_DIM_SCHEMA),
        (SERVICE_STOP_DIM, async_stop_dim, STOP_DIM_SCHEMA),
        (SERVICE_STEP_DIM, async_step_dim, STEP_DIM_SCHEMA),
    ):
        async_register_platform_entity_service(
            hass,
            DOMAIN,
            name,
            entity_domain=LIGHT_DOMAIN,
            func=func,
            schema=entity_schema,
        )


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


def _entry_for_hub_services(hass: HomeAssistant, data: dict[str, Any]) -> str:
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


def _resolve_key(hass: HomeAssistant, data: dict[str, Any]) -> tuple[str, Button]:
    """Find the key element a call names: its event entity, or its buttons device plus the key letter."""
    if (entity_id := data.get(ATTR_KEY_ENTITY)) is not None:
        entry_id, device, _ = _device_of_entity(hass, entity_id)
        if not isinstance(device, Button):
            raise _validation("service_not_a_key", name=entity_id)
        return entry_id, device
    registry_device, entry_id = _registry_device(hass, data[ATTR_KEY_DEVICE])
    name = registry_device.name_by_user or registry_device.name or registry_device.id
    keys = [
        d
        for d in _devices_behind(_hub(hass, entry_id), registry_device)
        if isinstance(d, Button)
    ]
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


def _resolve_target(hass: HomeAssistant, data: dict[str, Any]) -> tuple[str, int]:
    """Find the load element a key should drive: (entry id, element address); the gateway node is a target too.

    A blind's address is its position element, the one a key in *move* mode publishes to.
    """
    if (entity_id := data.get(ATTR_TARGET_ENTITY)) is not None:
        entry_id, device, _ = _device_of_entity(hass, entity_id)
        if not isinstance(device, LOAD_TYPES):
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
        d for d in _devices_behind(hub, registry_device) if isinstance(d, LOAD_TYPES)
    ]
    if len(loads) != 1:
        raise _validation("service_not_a_load", name=name)
    return entry_id, loads[0].address


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


# ------------------------------------------------------------------ running an operation


_lock = entry_lock


async def _run(
    hass: HomeAssistant,
    entry_id: str,
    operation: Callable[[MeshConfigurator], Coroutine[Any, Any, bool]],
    *,
    needs_link: bool = True,
) -> None:
    """Run `operation` on the entry's configurator, then reload the entry when the device model changed.

    An operation that goes on air (`needs_link`) first waits for the entry's proxy link: the previous call's
    reload, or an ordinary reconnect, leaves the hub without one for as long as a real BLE connect takes.
    """
    async with _lock(hass, entry_id):
        configurator = _configurator(hass, entry_id)
        if needs_link:
            configurator = await _wait_for_link(hass, entry_id, configurator)
        # the flags of this call only: a call that fails before planning must not reload for an earlier one's write
        configurator.recorded = configurator.adopted = False
        try:
            changed = await operation(configurator)
        except BaseException:
            # a stopped plan raises after recording what the mesh accepted, and so does a cancelled one (D12): the
            # device model must follow the export all the same (CFG-15), still under the lock — the cancellation
            # (or the error) goes on once the reload is done
            if configurator.recorded:
                await _reload(hass, entry_id)
            raise
        if changed:
            await _reload(hass, entry_id)


async def _reload(hass: HomeAssistant, entry_id: str) -> None:
    """Reload the entry after a change, to its end even when the call is cancelled meanwhile (`run_to_end`).

    A reload cut off half-way would leave the entry unloaded. While Home Assistant stops there is nothing to
    reload for: the next start sets the entry up from the export as it was written.
    """
    if hass.is_stopping:
        return
    await run_to_end(hass.config_entries.async_reload(entry_id))


async def async_configure(
    hass: HomeAssistant,
    entry_id: str,
    operation: Callable[[MeshConfigurator], Coroutine[Any, Any, bool]],
    *,
    needs_link: bool = True,
) -> None:
    """Run a configurator operation for an entity the way the actions run theirs: locked, on a live link, reloaded.

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
            # the mesh out of reach is no fault of the call's data: not a validation error (review-3 W13)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="service_not_connected"
            )
        configurator = current


@callback
def _suggest_area(hass: HomeAssistant, device_id: str | None, room: str) -> None:
    """Put a device that has no area yet into the area named like its room (as `suggested_area` does on creation)."""
    registry = dr.async_get(hass)
    device = registry.async_get(device_id) if device_id else None
    if device is None or device.area_id is not None:
        return
    area = ar.async_get(hass).async_get_or_create(room)
    registry.async_update_device(device.id, area_id=area.id)


# ------------------------------------------------------------------ handlers


async def _set_room(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    room: str = call.data[ATTR_ROOM]
    loads = await _resolve_loads(hass, call, LOAD_TYPES)
    for entry_id in sorted({load.entry_id for load in loads}):
        mine = [load for load in loads if load.entry_id == entry_id]

        async def operation(
            configurator: MeshConfigurator, mine: list[Load] = mine
        ) -> bool:
            # one plan, one export rewrite (and `.bak`), one gateway upload for every load of the call
            changed = await configurator.set_rooms(
                [load.address for load in mine], room
            )
            for load in mine:
                _suggest_area(hass, load.device_id, room)
            return changed

        await _run(hass, entry_id, operation)
    return None


async def _create_room(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry_id = _entry_for_hub_services(hass, call.data)

    async def operation(configurator: MeshConfigurator) -> bool:
        await configurator.create_room(call.data[ATTR_NAME])
        return (
            configurator.adopted
        )  # an empty room changes no device; the app's export adopted first may

    await _run(hass, entry_id, operation, needs_link=False)
    return None


async def _rename_room(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry_id = _entry_for_hub_services(hass, call.data)
    await _run(
        hass,
        entry_id,
        lambda c: c.rename_room(call.data[ATTR_ROOM], call.data[ATTR_NEW_NAME]),
        needs_link=False,
    )
    return None


async def _delete_room(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry_id = _entry_for_hub_services(hass, call.data)
    await _run(hass, entry_id, lambda c: c.delete_room(call.data[ATTR_ROOM]))
    return None


async def _assign_key(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry_id, key = _resolve_key(hass, call.data)
    mode = call.data.get(ATTR_MODE)
    if (room := call.data.get(ATTR_ROOM)) is not None:
        await _run(
            hass,
            entry_id,
            lambda c: c.assign_key(key.address, room=room, mode=mode),
        )
        return None
    if (scene := call.data.get(ATTR_SCENE)) is not None:
        await _run(
            hass,
            entry_id,
            lambda c: c.assign_key(key.address, scene=scene, mode=mode),
        )
        return None
    target_entry, element = _resolve_target(hass, call.data)
    if target_entry != entry_id:
        raise _validation("service_target_other_network")
    await _run(
        hass,
        entry_id,
        lambda c: c.assign_key(key.address, element=element, mode=mode),
    )
    return None


async def _clear_key(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry_id, key = _resolve_key(hass, call.data)
    await _run(hass, entry_id, lambda c: c.clear_key(key.address))
    return None


# ------------------------------------------------------------------ scenes


async def _create_scene(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Create an empty scene in the export; answers `{"scene": number}` when a response is asked for."""
    entry_id = _entry_for_hub_services(hass, call.data)
    name: str = call.data[ATTR_NAME]
    created: dict[str, int] = {}

    async def operation(configurator: MeshConfigurator) -> bool:
        created["scene"] = await configurator.create_scene(name)
        return True  # a new scene entity

    await _run(hass, entry_id, operation, needs_link=False)
    return {"scene": created["scene"], "name": name} if call.return_response else None


async def _rename_scene(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry_id = _entry_for_hub_services(hass, call.data)
    await _run(
        hass,
        entry_id,
        lambda c: c.rename_scene(call.data[ATTR_SCENE], call.data[ATTR_NEW_NAME]),
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
    by the schedule's rules (`schedule_action`), in the scene's words (review-3 W8).
    """
    scene: str = call.data[ATTR_SCENE]
    loads = await _resolve_loads(hass, call)
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

        await _run(hass, entry_id, operation)
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
    try:
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
    except (
        TimeoutError
    ) as err:  # the load answered none of the Set's attempts: it is unreachable now
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="device_not_reachable",
            translation_placeholders={"entity": load_entity_id(hass, device)},
        ) from err
    except OSError as err:  # a lost link (ConnectionError)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="send_failed"
        ) from err
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
    scene: str = call.data[ATTR_SCENE]
    loads = await _resolve_loads(hass, call)
    for entry_id in sorted({load.entry_id for load in loads}):
        mine = [load for load in loads if load.entry_id == entry_id]

        async def operation(
            configurator: MeshConfigurator, mine: list[Load] = mine
        ) -> bool:
            await configurator.remove_from_scenes(
                scene, [load.address for load in mine]
            )
            return True

        await _run(hass, entry_id, operation)
    return None


async def _delete_scene(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Delete a scene from its members and the export; answers `{"skipped": ["0232"]}` when a response is asked for.

    `skipped`: the members `force` passed over, which still hold the scene (the `scene_held` repair names them).
    """
    entry_id = _entry_for_hub_services(hass, call.data)
    skipped: list[str] = []

    async def operation(configurator: MeshConfigurator) -> bool:
        skipped.extend(
            await configurator.delete_scene(
                call.data[ATTR_SCENE], force=call.data[ATTR_FORCE]
            )
        )
        return True

    await _run(hass, entry_id, operation)
    return (
        cast("ServiceResponse", {"skipped": skipped}) if call.return_response else None
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


async def _sync_gateway(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Hand the export on disk to the gateway (the retry after a failed automatic upload)."""
    entry_id = _entry_for_hub_services(hass, call.data)
    await _run(hass, entry_id, lambda c: c.sync_gateway(), needs_link=False)
    return None


# ------------------------------------------------------------------ audit


async def _export_network(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Answer the export the entry uses, as the app's share file (default) or the CDB flavour (review-3 N12).

    For a backup, or to hand the installation to the app ("import from file") with what Home Assistant changed.
    Admin only: the answer carries every key of the mesh (NetKey, AppKey, each node's device key).
    """
    entry_id = _entry_for_hub_services(hass, call.data)
    configurator = _configurator(hass, entry_id)
    async with _lock(hass, entry_id):
        return await configurator.async_export(call.data[ATTR_FLAVOUR])


async def _find_new_devices(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """List the JUNG devices nearby that are not in a mesh yet (advertising the provisioning service)."""
    return cast("ServiceResponse", {"devices": onboard.unprovisioned_devices(hass)})


async def _add_device(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Provision and commission a new JUNG device, record it, reload the entry (review-3 N3, experimental).

    Refused unless the entry's *Allow Home Assistant to add devices* option is on. Admin only: it hands the
    device the mesh's keys.
    """
    entry_id = _entry_for_hub_services(hass, call.data)
    if not _hub(hass, entry_id).entry.options.get(
        OPTION_ALLOW_PROVISIONING, DEFAULT_ALLOW_PROVISIONING
    ):
        raise _validation("add_device_not_allowed")
    response: dict[str, Any] = {}
    keepers: list[VaultKeeper] = []

    async def operation(configurator: MeshConfigurator) -> bool:
        # the hub the lock handed us, on a live link (commissioning goes through it): a previous call's reload
        # replaces it
        keepers.append(configurator.hub.vault)
        response.update(
            await onboard.async_add_device(
                hass,
                configurator.hub,
                configurator,
                call.data[ATTR_ADDRESS],
                call.data[ATTR_NAME],
            )
        )
        return True

    try:
        await _run(hass, entry_id, operation)
    finally:
        # a device provisioned but not recorded is pending now (the reload after a success clears it)
        _update_pending_issue(hass, entry_id, keepers)
    return response


def _update_pending_issue(
    hass: HomeAssistant, entry_id: str, keepers: list[VaultKeeper]
) -> None:
    """Bring the entry's `pending_device` repair issue up to date with the vault the call used (if it got one)."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if keepers and entry is not None:
        onboard.async_update_pending_issue(hass, entry, keepers[0])


async def _reset_pending_device(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Reset a device `add_device` provisioned but did not record, and forget it (review-4 D2, unverified on air).

    Refused unless the entry's *Allow Home Assistant to add devices* option is on. Admin only: it sends a Config
    Node Reset with a device key only the vault holds. `force` forgets a device that does not confirm its reset.
    """
    entry_id = _entry_for_hub_services(hass, call.data)
    if not _hub(hass, entry_id).entry.options.get(
        OPTION_ALLOW_PROVISIONING, DEFAULT_ALLOW_PROVISIONING
    ):
        raise _validation("add_device_not_allowed")
    response: dict[str, Any] = {}
    keepers: list[VaultKeeper] = []

    async def operation(configurator: MeshConfigurator) -> bool:
        keepers.append(configurator.hub.vault)
        response.update(
            await onboard.async_reset_pending_device(
                configurator.hub,
                uuid=call.data.get(ATTR_UUID),
                unicast=call.data.get(ATTR_UNICAST),
                force=call.data[ATTR_FORCE],
            )
        )
        return False  # the export did not change: nothing to reload

    try:
        await _run(hass, entry_id, operation)
    finally:
        _update_pending_issue(hass, entry_id, keepers)
    return response if call.return_response else None


async def _remove_device(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Reset a node and take it out of the network, then reload (review-3 N4, experimental; irreversible).

    Refused unless the entry's *Allow Home Assistant to add devices* option is on; `force` records the removal of
    a node that does not confirm its reset (one that is gone for good).
    """
    entry_id, unicast = _resolve_node(hass, call.data[ATTR_DEVICE])
    if unicast is None:
        raise _validation("remove_device_mesh")
    if not _hub(hass, entry_id).entry.options.get(
        OPTION_ALLOW_PROVISIONING, DEFAULT_ALLOW_PROVISIONING
    ):
        raise _validation("add_device_not_allowed")
    force = call.data[ATTR_FORCE]
    # locked, on a live link, and reloaded after a stop too: the reset is recorded even when the unwiring stops
    await _run(hass, entry_id, lambda c: c.remove_node(unicast, force=force))
    return None


async def _audit_network(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Compare the nodes' Configuration Servers with the export: every mains node, or the node `device` names.

    Gets only, so nothing is recorded or reloaded; the entry's lock keeps a configuration change from running in
    between, and the call waits for the link like the others. Battery nodes sleep and would answer nothing: the
    network-wide audit lists them as `skipped` (naming one as `device` asks it all the same). Answers
    `jhmesh.audit.report` — per node its node-wide states and findings — plus `skipped`.
    """
    if (device_id := call.data.get(ATTR_DEVICE)) is not None:
        entry_id, unicast = _resolve_node(hass, device_id)
    else:
        entry_id, unicast = _entry_for_hub_services(hass, call.data), None
    response: dict[str, Any] = {}

    async def operation(configurator: MeshConfigurator) -> bool:
        # the hub the lock handed us: a previous call's reload replaces it
        hub = configurator.hub
        if unicast is None:
            provisioned = sorted(
                (n for n in hub.cdb.nodes if n.pid is not None),
                key=lambda n: n.unicast,
            )
            nodes = [n for n in provisioned if n.pid not in BATTERY_PIDS]
            skipped = [n for n in provisioned if n.pid in BATTERY_PIDS]
        else:
            nodes, skipped = [n for n in hub.cdb.nodes if n.unicast == unicast], []
        try:
            results = await hub.async_audit(nodes)
        except ConnectionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="send_failed"
            ) from err
        response.update(report(results), skipped=[f"{n.unicast:04X}" for n in skipped])
        return False

    await _run(hass, entry_id, operation)
    return response


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
        if not scheduler(hass, hub).hosts(load.address):
            raise _validation("schedule_not_supported", name=name)
        out.setdefault(load.entry_id, []).append(
            ScheduleLoad(load.address, device.kind, name)
        )
    return out


async def _on_schedules(
    hass: HomeAssistant,
    loads: dict[str, list[ScheduleLoad]],
    act: Callable[[Scheduler, ScheduleLoad], Awaitable[Any]],
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
            on = scheduler(hass, configurator.hub)
            for load in mine:
                results[load.name] = await act(on, load)
            return False

        await _run(hass, entry_id, operation)
    return results


async def _get_schedules(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Read the loads' used slots: `{entity_id: {"schedules": [...]}}`, the fields `create_schedule` takes."""

    async def read(on: Scheduler, load: ScheduleLoad) -> dict[str, Any]:
        return {"schedules": [slot.as_dict() for slot in await on.read(load.address)]}

    return await _on_schedules(hass, await _schedule_loads(hass, call), read)


async def _create_schedule(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Write the schedule into each load's first free slot; answers `{entity_id: {"slot": n}}` when asked."""
    loads = await _schedule_loads(hass, call)
    actions: dict[str, V.Action] = {}
    for load in (load for mine in loads.values() for load in mine):
        try:
            actions[load.name] = schedule_action(load.kind, call.data)
        except ActionError as err:
            raise _validation(err.key, name=load.name, **err.placeholders) from err

    async def free_slot(on: Scheduler, load: ScheduleLoad) -> None:
        await on.free_slot(load.address)

    created: dict[str, int] = {}

    async def create(on: Scheduler, load: ScheduleLoad) -> dict[str, Any]:
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
        # those slots, which the response never gets to (review-3 W9) — a retry as it is would add them twice
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

    async def toggle(on: Scheduler, load: ScheduleLoad) -> None:
        await on.set_enabled(load.address, index, enabled)

    await _on_schedules(hass, await _schedule_loads(hass, call), toggle)


async def _delete_schedule(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    index: int = call.data[ATTR_SLOT]

    async def delete(on: Scheduler, load: ScheduleLoad) -> None:
        await on.delete(load.address, index)

    await _on_schedules(hass, await _schedule_loads(hass, call), delete)
    return None


# ------------------------------------------------------------------ thresholds


async def _threshold_sockets(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, list[int]]:
    """Return the call's metering sockets by entry; each must have the thresholds (the product measures)."""
    out: dict[str, list[int]] = {}
    for load in await _resolve_loads(hass, call, (Socket,)):
        hub = _hub(hass, load.entry_id)
        socket = hub.devices.by_address[load.address]
        assert isinstance(socket, Socket)
        if not has_thresholds(hub, socket):
            raise _validation(
                "threshold_not_supported", name=load_entity_id(hass, socket)
            )
        out.setdefault(load.entry_id, []).append(load.address)
    return out


def _threshold_devices(
    hass: HomeAssistant, entity_ids: list[str], entry_id: str
) -> list[int]:
    """Return the load elements behind `devices`: lights and sockets (an OnOff server) of the socket's network."""
    out: list[int] = []
    for entity_id in entity_ids:
        owner, device, _ = _device_of_entity(hass, entity_id)
        if not isinstance(device, ONOFF_LOAD_TYPES):
            raise _validation("service_not_a_load", name=entity_id)
        if owner != entry_id:
            raise _validation("threshold_other_network")
        out.append(device.address)
    return out


def _socket(hub: JungHomeHub, address: int) -> Socket:
    socket = hub.devices.by_address[address]
    assert isinstance(socket, Socket)
    return socket


async def _set_threshold(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Write a socket's switch-on or switch-off threshold; `devices` replaces the loads both thresholds switch.

    In the app's order: the threshold first, then the wiring; the wiring's checks run before either is written,
    so a refused call leaves the socket as it was. Without `devices`, a call that disables the threshold
    (`enabled: false`) while the socket's other one is not active either unwires the loads as the app's disable
    (`ToggleThreshold`) does (`MeshConfigurator.unwire_threshold`); an other threshold the socket does not tell
    about keeps them wired. Editing the level or duration of a disabled threshold is no disable: the loads stay.
    """
    which: Which = call.data[ATTR_THRESHOLD]
    needs_current = not {"power", "duration", "enabled"} <= set(call.data)
    for entry_id, sockets in (await _threshold_sockets(hass, call)).items():
        devices = (
            _threshold_devices(hass, call.data[ATTR_DEVICES], entry_id)
            if ATTR_DEVICES in call.data
            else None
        )

        async def operation(
            configurator: MeshConfigurator,
            sockets: list[int] = sockets,
            devices: list[int] | None = devices,
        ) -> bool:
            # the hub the lock handed us: the one a previous call's reload left
            hub = configurator.hub
            changed = False
            for address in sockets:
                socket = _socket(hub, address)
                if devices is not None:
                    await configurator.check_threshold_devices(address, devices)
                current = (
                    await current_threshold(hass, hub, socket, which)
                    if needs_current
                    else None
                )
                value = planned_threshold(
                    current, call.data, load_entity_id(hass, socket)
                )
                await write_threshold(hass, hub, socket, which, value)
                if devices is not None:
                    changed = (
                        await configurator.set_threshold_devices(address, devices)
                        or changed
                    )
                elif call.data.get("enabled") is False:
                    other = await current_threshold(
                        hass, hub, socket, OTHER_THRESHOLD[which]
                    )
                    if other is not None and not other.active:
                        changed = (
                            await configurator.unwire_threshold(address) or changed
                        )
            return changed

        await _run(hass, entry_id, operation)
    return None


async def _delete_threshold(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Clear both thresholds of the sockets, then unwire the loads they switched and reset the client's publication (the app's delete)."""
    for entry_id, sockets in (await _threshold_sockets(hass, call)).items():

        async def operation(
            configurator: MeshConfigurator, sockets: list[int] = sockets
        ) -> bool:
            hub = configurator.hub
            changed = False
            for address in sockets:
                for which in THRESHOLD_PROPERTIES:
                    await write_threshold(
                        hass, hub, _socket(hub, address), which, CLEARED
                    )
                changed = await configurator.unwire_threshold(address) or changed
            return changed

        await _run(hass, entry_id, operation)
    return None
