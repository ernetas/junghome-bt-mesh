"""Device triggers for JUNG HOME push-buttons.

The keys are already exposed as `event` entities, but those only show up in the *entity* automation picker. Device
triggers put "Button A clicked" directly in the buttons device's automation UI, which is where users look first
for a wall switch.

A device trigger can only attach to something on the Home Assistant bus, so the hub publishes every key event as
`EVENT_BUTTON_ACTION` (`event.publish_button_event`, whether or not the key's event entity is enabled) and the
triggers here are thin wrappers that match it. `type` is the key (`a`…`d`, or `e1` / `e2` for a mini actuator's
inputs; only the keys the device has), `subtype` the event type the key reports (`event.EVENT_TYPES`). Which
subtypes a key can actually produce depends on how it is wired (`key_subtypes`): a gateway-mode key clicks and
holds, a key wired to a load or a room presses, dims and holds, a key wired to a scene recalls. Only those are
offered when the key's mode (0x5003, once read) or its connection in the export tells; all of them otherwise.
Validation accepts every subtype of a key the device has, so an automation saved before — or across a rewiring —
stays valid.

A gateway-mode rocker is one element with two halves whose gestures differ only in the event's `side`: the
`<type>_up` / `<type>_down` subtypes match one half (`SIDE_SUBTYPES`), the plain ones either — which is what
automations saved before the halves existed keep doing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.device_automation.exceptions import (
    InvalidDeviceAutomationConfig,
)
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_EVENT_DATA,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.helpers import device_registry as dr

from .bus_events import EVENT_TYPES
from .config_entities import PROPERTY_KEY_MODE, cached_value
from .const import (
    ATTR_KEY,
    DOMAIN,
    EVENT_BUTTON_ACTION,
    KEY_EVENT_SIDE_DOWN,
    KEY_EVENT_SIDE_UP,
)
from .entity import button_gang, buttons_device_id
from .jhmesh import properties as P
from .jhmesh.devices import BUTTON_LETTERS, INPUT_NAMES

if TYPE_CHECKING:
    from homeassistant.core import CALLBACK_TYPE, HomeAssistant
    from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
    from homeassistant.helpers.typing import ConfigType

    from .coordinator import JungHomeHub
    from .jhmesh.devices import Button

CONF_SUBTYPE = "subtype"
ATTR_SIDE = "side"  # the event attribute a gateway-mode rocker's gestures carry (`const.KEY_EVENTS`)

# `type`: the key letter (a mini actuator's input name), lower-case as translation keys must be. `subtype`: the event type, in the order the
# event entities declare them, which is the order the automation UI lists a key's triggers in.
TRIGGER_TYPES = tuple(
    name.lower() for name in (*BUTTON_LETTERS.values(), *INPUT_NAMES.values())
)
SIDED_TYPES = (
    "click",
    "double_click",
    "hold_start",
    "hold_end",
)  # the gestures a rocker half reports
SIDE_SUBTYPES: dict[str, tuple[str, str]] = {
    f"{event_type}_{side}": (event_type, side)
    for event_type in SIDED_TYPES
    for side in (KEY_EVENT_SIDE_UP, KEY_EVENT_SIDE_DOWN)
}
TRIGGER_SUBTYPES = (*EVENT_TYPES, *SIDE_SUBTYPES)

# What a key produces, by how it is wired: a gateway-mode key its vendor gestures, per rocker half too; a key wired to
# a load, a room or another group its On / Off and Level messages, and the holds derived from them
# (`ButtonGestures.dim_hold`); a key wired to a scene its recalls.
GATEWAY_SUBTYPES = (*SIDED_TYPES, *SIDE_SUBTYPES)
LOAD_SUBTYPES = ("press_on", "press_off", "dim", "hold_start", "hold_end")
SCENE_SUBTYPES = ("scene",)
CONNECTION_SUBTYPES: dict[str, tuple[str, ...]] = {
    "gateway": GATEWAY_SUBTYPES,
    "device": LOAD_SUBTYPES,
    "room": LOAD_SUBTYPES,
    "group": LOAD_SUBTYPES,
    "scene": SCENE_SUBTYPES,
}
# the key modes (0x5003) whose messages are known; `move` (blinds), `property` and `rtr` keys are left to the export
KEY_MODE_SUBTYPES: dict[str, tuple[str, ...]] = {
    "gateway": GATEWAY_SUBTYPES,
    "light": LOAD_SUBTYPES,
    "switch": LOAD_SUBTYPES,
    "scene": SCENE_SUBTYPES,
}

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES),
        vol.Required(CONF_SUBTYPE): vol.In(TRIGGER_SUBTYPES),
    }
)


def key_subtypes(hub: JungHomeHub, button: Button) -> tuple[str, ...]:
    """Return the trigger subtypes `button` can produce, in `TRIGGER_SUBTYPES` order (review-4 H I-3, U4-9).

    The key's mode as the node reported it (0x5003, read once its *Key mode* sensor is enabled) comes first: it is
    what the key sends now, whatever the export says. Otherwise the connection the export shows (`KeyConnection`).
    A key that tells neither — no mode read and no connection, or a mode whose messages are not known — offers all.
    """
    mode = cached_value(hub, button.address, P.PROPERTIES[PROPERTY_KEY_MODE])
    wanted = KEY_MODE_SUBTYPES.get(mode) if isinstance(mode, str) else None
    if wanted is None and button.connection is not None:
        wanted = CONNECTION_SUBTYPES.get(button.connection.kind)
    if wanted is None:
        return TRIGGER_SUBTYPES
    return tuple(subtype for subtype in TRIGGER_SUBTYPES if subtype in wanted)


def _device_buttons(
    hass: HomeAssistant, device_id: str
) -> tuple[JungHomeHub, list[Button]] | None:
    """Return the hub and the keys of the buttons device `device_id`, in key order.

    `None` when the registry entry is not a buttons device of a loaded entry: the automation UI then offers no
    triggers, and validation accepts the config as it is rather than break an automation while the mesh export is
    not loaded.
    """
    device_entry = dr.async_get(hass).async_get(device_id)
    if device_entry is None:
        return None
    identifiers = {
        identifier
        for domain, identifier in device_entry.identifiers
        if domain == DOMAIN
    }
    if not identifiers:
        return None
    # a device belongs to exactly one config entry (HA 2026.8+); `config_entries` is a deprecated shim
    entry = hass.config_entries.async_get_entry(device_entry.config_entry_id)
    if (
        entry is None
        or entry.domain != DOMAIN
        or entry.state is not ConfigEntryState.LOADED
    ):
        return None
    hub: JungHomeHub = entry.runtime_data
    keys = sorted(
        (
            button
            for button in hub.devices.buttons
            if buttons_device_id(button_gang(hub, button)) in identifiers
        ),
        key=lambda button: button.location,
    )
    if keys:
        return hub, keys
    return None


def _device_keys(hass: HomeAssistant, device_id: str) -> list[str] | None:
    """Return the trigger types (key letters) of the buttons device `device_id`, in key order (`_device_buttons`)."""
    found = _device_buttons(hass, device_id)
    if found is None:
        return None
    return [button.key.lower() for button in found[1]]


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """List the triggers a JUNG HOME device offers: every event type each key of a buttons device can produce."""
    found = _device_buttons(hass, device_id)
    if found is None:
        return []
    hub, buttons = found
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_TYPE: button.key.lower(),
            CONF_SUBTYPE: subtype,
        }
        for button in buttons
        for subtype in key_subtypes(hub, button)
    ]


async def async_validate_trigger_config(
    hass: HomeAssistant, config: ConfigType
) -> ConfigType:
    """Validate a trigger against the keys the device actually has."""
    config = TRIGGER_SCHEMA(config)
    keys = _device_keys(hass, config[CONF_DEVICE_ID])
    if keys is not None and config[CONF_TYPE] not in keys:
        raise InvalidDeviceAutomationConfig(
            f"Device {config[CONF_DEVICE_ID]} has no key {config[CONF_TYPE].upper()}"
        )
    return config


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a trigger by listening for the matching button event on the bus (a subset of its data)."""
    subtype = config[CONF_SUBTYPE]
    event_type, side = SIDE_SUBTYPES.get(subtype, (subtype, None))
    event_data = {
        CONF_DEVICE_ID: config[CONF_DEVICE_ID],
        ATTR_KEY: config[CONF_TYPE].upper(),
        CONF_TYPE: event_type,
    }
    if side is not None:
        event_data[ATTR_SIDE] = side
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: EVENT_BUTTON_ACTION,
            CONF_EVENT_DATA: event_data,
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
