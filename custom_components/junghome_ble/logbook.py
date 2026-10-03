"""Describe the JUNG HOME bus events in the logbook.

English on purpose, unlike everything else in this integration: the logbook API has no translation hook — a
describer is a sync callback returning literal strings, runs in the server's language rather than the viewing
user's, and `strings.json` has no category hassfest would accept for it. Core's own describers (automation,
deconz, shelly, zha, …) hard-code English the same way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.logbook.const import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID, ATTR_NAME, CONF_TYPE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import (
    ATTR_KEY,
    ATTR_REASON,
    ATTR_SCENE,
    DOMAIN,
    EVENT_BUTTON_ACTION,
    EVENT_SCENE_RECALLED,
    HOLD_END_LINK_LOST,
    HOLD_END_STOPPED,
    HOLD_END_TIMEOUT,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# What happened to the key, per event type; the wording mirrors the event entity's state labels (strings.json).
BUTTON_MESSAGES = {
    "click": "clicked",
    "double_click": "double-clicked",
    "hold_start": "hold started",
    "hold_end": "hold released",
    "press_on": "pressed on / up",
    "press_off": "pressed off / down",
    "dim": "dimming",
}  # a `scene` event names the scene number instead
# Why a hold ended without its release (`const.HOLD_END_REASONS`), appended to "hold released".
HOLD_END_MESSAGES = {
    HOLD_END_TIMEOUT: "no release heard in time",
    HOLD_END_LINK_LOST: "link lost",
    HOLD_END_STOPPED: "integration stopped",
}


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Register the describers of the button-action and scene-recalled events."""
    registry = dr.async_get(hass)

    def _device_name(device_id: str | None) -> str | None:
        device = registry.async_get(device_id) if device_id else None
        return (device.name_by_user or device.name) if device else None

    @callback
    def describe_button_action(event: Event) -> dict[str, str]:
        """'Living room rocker Button A clicked': the key entity's name, or the device's while it is gone or disabled."""
        data = event.data
        entity_id = data.get(ATTR_ENTITY_ID)
        state = hass.states.get(entity_id) if entity_id else None
        if state is not None:
            name = state.name
        else:
            key = f"Button {data.get(ATTR_KEY, '?')}"
            device_name = _device_name(data.get(ATTR_DEVICE_ID))
            name = f"{device_name} {key}" if device_name else key
        event_type = str(data.get(CONF_TYPE))
        if event_type == "scene":
            message = f"recalled scene {data.get(ATTR_SCENE, '?')}"
        else:
            message = BUTTON_MESSAGES.get(event_type, event_type)
        if (reason := data.get(ATTR_REASON)) is not None:
            message += f" ({HOLD_END_MESSAGES.get(str(reason), str(reason))})"
        entry = {LOGBOOK_ENTRY_NAME: name, LOGBOOK_ENTRY_MESSAGE: message}
        if entity_id:
            entry[LOGBOOK_ENTRY_ENTITY_ID] = str(entity_id)
        return entry

    @callback
    def describe_scene_recalled(event: Event) -> dict[str, str]:
        """'All off was recalled by Living room rocker', filed under the scene entity when the export has it."""
        data = event.data
        name = data.get(ATTR_NAME) or (
            f"Scene {data[ATTR_SCENE]}" if ATTR_SCENE in data else "Scene"
        )
        message = "was recalled"
        if (device_name := _device_name(data.get(ATTR_DEVICE_ID))) is not None:
            message += f" by {device_name}"
        entry = {LOGBOOK_ENTRY_NAME: str(name), LOGBOOK_ENTRY_MESSAGE: message}
        entity_id = data.get(ATTR_ENTITY_ID)
        if entity_id:
            entry[LOGBOOK_ENTRY_ENTITY_ID] = str(entity_id)
        return entry

    async_describe_event(DOMAIN, EVENT_BUTTON_ACTION, describe_button_action)
    async_describe_event(DOMAIN, EVENT_SCENE_RECALLED, describe_scene_recalled)
