"""Service actions: rooms, key connections, scenes, schedules, thresholds (`junghome_ble.set_room` … `delete_threshold`).

The services are registered once per Home Assistant run, in `async_setup` (`async_setup_services`); they stay
registered and answer "not loaded" while no entry is loaded. A call resolves the
Home Assistant ids it was given — device ids, entity ids, areas — to mesh elements through the registries and the
device-identifier scheme documented in `device_info.py` (`{uuid}-{location:04x}` loads, `{uuid}-{location:04x}-buttons`
gangs of keys, `node:{uuid}` nodes), hands the operation to the hub's `MeshConfigurator`, and finally has the
running hub take the rewritten export over in place (`model_update`, review-4 D23: no entity goes `unavailable`, the
link stays up; adding or removing a node still reloads the entry). The schedule actions write no export: they go to
the loads' own JH Scheduler (`schedules.py`) and change no model; a socket threshold is a property plus wiring
(`thresholds.py`), and the model follows only when the wiring changed. `audit_network` only reads (`jhmesh.audit`): it answers
what the nodes' Configuration Servers hold against the export and changes nothing. `locate_node` has a node advertise
its Node Identity for a minute at most; `approve_gateway_client` lists the access requests waiting at the gateway and
approves the one named (review-4 F4-15, F4-17). The dimming actions
(`start_dim` / `stop_dim` / `step_dim`) are entity actions of the light platform (`actions/dim.py`): they send one
command to a dimmer and write nothing. Every action but the reading ones (`USER_SERVICES`) and the dimming ones is
for administrators only (review-4 W4-9). The rewiring actions answer what their plans applied, or with `dry_run` only
what they would send and change (`actions.common._execute`, `MeshConfigurator.dry_run`); every call that ran a plan is
logged in the logbook and the diagnostics (`actions.common._report_plan`); what cannot be undone needs `confirm`
(review-4 W I3, W I6, W I7, W I9).

Calls for one entry are serialised (`coordinator.entry_lock`, kept across reloads, taken by the unknown-node
refresh too) so a call never runs against a model being swapped, or a hub being torn down by a reload (an options
change, a call that could not follow in place); a call that goes on air then waits (`SERVICE_LINK_WAIT`) for the
reloaded hub's link, which connects in the background.

This module is the registration table; the handlers and their schemas live in `actions/`, one module per domain
(`actions.common` runs an operation, `actions.resolve` resolves the ids; review-4 A4-8).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import voluptuous as vol
from homeassistant.components.light.const import DOMAIN as LIGHT_DOMAIN
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.helpers.service import (
    async_register_admin_service,
    async_register_platform_entity_service,
)

from .actions.audit import (
    APPROVE_GATEWAY_CLIENT_SCHEMA,
    AUDIT_NETWORK_SCHEMA,
    EXPORT_FLAVOURS,
    EXPORT_NETWORK_SCHEMA,
    SYNC_GATEWAY_SCHEMA,
    _approve_gateway_client,
    _audit_network,
    _export_network,
    _sync_gateway,
)
from .actions.common import (
    CONFIGURATORS,
    SCHEDULE_ACTIONS,
    STATE_FIELDS,
    _bound,
    _configurator,
    _lock,
    _run,
    _validation,
    async_configure,
    async_register_configurator,
    async_unregister_configurator,
)
from .actions.devices import (
    ADD_DEVICE_SCHEMA,
    FIND_NEW_DEVICES_SCHEMA,
    LOCATE_NODE_SCHEMA,
    REMOVE_DEVICE_SCHEMA,
    RESET_PENDING_DEVICE_SCHEMA,
    _add_device,
    _find_new_devices,
    _locate_node,
    _remove_device,
    _reset_pending_device,
)
from .actions.dim import (
    ATTR_DIRECTION,
    ATTR_SPEED,
    ATTR_STEP,
    DIM_DIRECTIONS,
    async_start_dim,
    async_step_dim,
    async_stop_dim,
)
from .actions.keys import (
    ASSIGN_KEY_SCHEMA,
    CLEAR_KEY_SCHEMA,
    KEY_LETTERS,
    _assign_key,
    _clear_key,
)
from .actions.resolve import _entry_for_hub_services
from .actions.rooms import (
    ADD_TO_ROOM_SCHEMA,
    CREATE_ROOM_SCHEMA,
    DELETE_ROOM_SCHEMA,
    REMOVE_FROM_ROOM_SCHEMA,
    RENAME_ROOM_SCHEMA,
    SET_ROOM_SCHEMA,
    _add_to_room,
    _create_room,
    _delete_room,
    _remove_from_room,
    _rename_room,
    _set_room,
)
from .actions.scenes import (
    CREATE_SCENE_SCHEMA,
    DELETE_SCENE_SCHEMA,
    DELETE_UNUSED_SCENES_SCHEMA,
    REMOVE_FROM_SCENE_SCHEMA,
    RENAME_SCENE_SCHEMA,
    SCENE_STATE_ERRORS,
    STORE_SCENE_SCHEMA,
    _apply_state,
    _create_scene,
    _delete_scene,
    _delete_unused_scenes,
    _remove_from_scene,
    _rename_scene,
    _store_scene,
)
from .actions.schedules import (
    CREATE_SCHEDULE_SCHEMA,
    DELETE_SCHEDULE_SCHEMA,
    DISABLE_SCHEDULE_SCHEMA,
    ENABLE_SCHEDULE_SCHEMA,
    GET_SCHEDULES_SCHEMA,
    UPDATE_SCHEDULE_SCHEMA,
    _create_schedule,
    _delete_schedule,
    _disable_schedule,
    _enable_schedule,
    _get_schedules,
    _update_schedule,
)
from .actions.thresholds import (
    DELETE_THRESHOLD_SCHEMA,
    SET_THRESHOLD_SCHEMA,
    _delete_threshold,
    _set_threshold,
)
from .const import DIM_DEFAULT_SPEED, DOMAIN
from .thresholds import THRESHOLD_PROPERTIES

# what other modules (and the tests) import from here, now defined in `actions/`
__all__ = [
    "CONFIGURATORS",
    "EXPORT_FLAVOURS",
    "KEY_LETTERS",
    "SCENE_STATE_ERRORS",
    "SCHEDULE_ACTIONS",
    "STATE_FIELDS",
    "THRESHOLD_PROPERTIES",
    "_apply_state",
    "_configurator",
    "_entry_for_hub_services",
    "_lock",
    "_run",
    "_validation",
    "async_configure",
    "async_register_configurator",
    "async_unregister_configurator",
]

SERVICE_SET_ROOM = "set_room"
SERVICE_ADD_TO_ROOM = "add_to_room"
SERVICE_REMOVE_FROM_ROOM = "remove_from_room"
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
SERVICE_UPDATE_SCHEDULE = "update_schedule"
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
SERVICE_LOCATE_NODE = (
    "locate_node"  # admin only: it changes what a node advertises, for a minute
)
# admin only: an approved client gets the gateway's whole API, the export with every key of the mesh included
SERVICE_APPROVE_GATEWAY_CLIENT = "approve_gateway_client"
SERVICE_START_DIM = "start_dim"
SERVICE_STOP_DIM = "stop_dim"
SERVICE_STEP_DIM = "step_dim"
# the services that answer, and whether they must be asked to; every rewiring action answers what its plans applied
# (`MeshConfigurator.plan_response`) or, as a dry run, what they would send (review-4 W I3, W I6)
RESPONSES: dict[str, SupportsResponse] = {
    SERVICE_SET_ROOM: SupportsResponse.OPTIONAL,
    SERVICE_ADD_TO_ROOM: SupportsResponse.OPTIONAL,
    SERVICE_REMOVE_FROM_ROOM: SupportsResponse.OPTIONAL,
    SERVICE_CREATE_ROOM: SupportsResponse.OPTIONAL,
    SERVICE_DELETE_ROOM: SupportsResponse.OPTIONAL,
    SERVICE_ASSIGN_KEY: SupportsResponse.OPTIONAL,
    SERVICE_CLEAR_KEY: SupportsResponse.OPTIONAL,
    SERVICE_SET_THRESHOLD: SupportsResponse.OPTIONAL,
    SERVICE_DELETE_THRESHOLD: SupportsResponse.OPTIONAL,
    SERVICE_REMOVE_DEVICE: SupportsResponse.OPTIONAL,
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
    SERVICE_LOCATE_NODE: SupportsResponse.OPTIONAL,
    SERVICE_APPROVE_GATEWAY_CLIENT: SupportsResponse.OPTIONAL,
}
# Review-4 W4-9 (decision M8): every other action rewires, deletes or writes the export and the devices, and is for
# administrators only (`async_register_admin_service`); these only read. Moving a name here opens it to every user.
USER_SERVICES = frozenset(
    {SERVICE_GET_SCHEDULES, SERVICE_AUDIT_NETWORK, SERVICE_FIND_NEW_DEVICES}
)

# the dimming entity actions (`actions/dim.py`): a direction and a speed in % of the range per second, or a step in %
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
        (SERVICE_ADD_TO_ROOM, _add_to_room, ADD_TO_ROOM_SCHEMA),
        (SERVICE_REMOVE_FROM_ROOM, _remove_from_room, REMOVE_FROM_ROOM_SCHEMA),
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
        (SERVICE_UPDATE_SCHEDULE, _update_schedule, UPDATE_SCHEDULE_SCHEMA),
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
        (SERVICE_LOCATE_NODE, _locate_node, LOCATE_NODE_SCHEMA),
        (
            SERVICE_APPROVE_GATEWAY_CLIENT,
            _approve_gateway_client,
            APPROVE_GATEWAY_CLIENT_SCHEMA,
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
