"""Device triggers for JUNG HOME push-buttons.

The keys are already exposed as `event` entities, but those only show up in the *entity* automation picker. Device
triggers put "Button A clicked" directly in the buttons device's automation UI, which is where users look first
for a wall switch.

A device trigger can only attach to something on the Home Assistant bus, so the event platform re-emits every
event as `EVENT_BUTTON_ACTION` and the triggers here are thin wrappers that match it. `type` is the key
(`a`…`d`, or `e1` / `e2` for a mini actuator's inputs; only the keys the device has), `subtype` the event type the key reports (`event.EVENT_TYPES`). Which
subtypes a key can actually produce depends on how the app wired it (a gateway-mode key clicks, a key wired to a
load presses, a key wired to a scene recalls); all are offered, as the mode is not reliably known from the export.

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

from .const import (
    ATTR_KEY,
    DOMAIN,
    EVENT_BUTTON_ACTION,
    KEY_EVENT_SIDE_DOWN,
    KEY_EVENT_SIDE_UP,
)
from .entity import button_gang, buttons_device_id
from .event import EVENT_TYPES
from .jhmesh.devices import BUTTON_LETTERS, INPUT_NAMES

if TYPE_CHECKING:
    from homeassistant.core import CALLBACK_TYPE, HomeAssistant
    from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
    from homeassistant.helpers.typing import ConfigType

    from .coordinator import JungHomeHub

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

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES),
        vol.Required(CONF_SUBTYPE): vol.In(TRIGGER_SUBTYPES),
    }
)


def _device_keys(hass: HomeAssistant, device_id: str) -> list[str] | None:
    """Return the trigger types (key letters) of the buttons device `device_id`, in key order.

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
        return [button.key.lower() for button in keys]
    return None


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """List the triggers a JUNG HOME device offers: every event type of every key of a buttons device."""
    keys = _device_keys(hass, device_id)
    if keys is None:
        return []
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_TYPE: key,
            CONF_SUBTYPE: subtype,
        }
        for key in keys
        for subtype in TRIGGER_SUBTYPES
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
