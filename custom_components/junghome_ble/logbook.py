"""Describe the JUNG HOME bus events in the logbook.

The logbook API has no translation hook: a describer is a sync callback returning literal strings, and it runs in the
server's language rather than the viewing user's. Every line is still the integration's own text (review-4 U4-16),
rendered from the translations Home Assistant cached for the server's language when it set the integration up,
English where that language has none (`cached_text`):

- what happened to a key is the device trigger's wording (`device_automation.trigger_subtype`, "clicked"), the key
  the event entity's name (`entity.event.button.name`, "Button A"), the rest `logbook_*` messages of `exceptions`
  (the category hassfest accepts for a sentence with placeholders);
- a plan's line (`EVENT_PLAN`, review-4 W I7: "Key 0151 (…) now drives room Kitchen; 6 messages") is worded by the
  action, a `plan_*` message of `exceptions`.

An event type, hold-end reason or plan line nobody wrote a text for (a newer version's) shows as it is.
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
from homeassistant.helpers.translation import async_get_cached_translations

from .const import (
    ATTR_KEY,
    ATTR_REASON,
    ATTR_SCENE,
    DOMAIN,
    EVENT_BUTTON_ACTION,
    EVENT_PLAN,
    EVENT_SCENE_RECALLED,
)

if TYPE_CHECKING:
    from collections.abc import Callable


@callback
def cached_text(
    hass: HomeAssistant,
    category: str,
    key: str,
    placeholders: dict[str, str] | None = None,
) -> str | None:
    """Return the integration's text `<category>.<key>` in the server's language (English without), filled in.

    None when neither has it: the integration's translations are cached when Home Assistant sets it up, so only a
    key no version wrote lacks one.
    """
    path = f"component.{DOMAIN}.{category}.{key}"
    for language in (hass.config.language, "en"):
        text = async_get_cached_translations(hass, language, category, DOMAIN)
        if path in text:
            return text[path].format_map(placeholders or {})
    return None


@callback
def plan_message(hass: HomeAssistant, key: str, placeholders: dict[str, str]) -> str:
    """Return a plan's logbook line in the server's language (English without it); the key when it has no text."""
    return cached_text(hass, "exceptions", f"{key}.message", placeholders) or key


@callback
def logbook_message(
    hass: HomeAssistant, key: str, placeholders: dict[str, str] | None = None
) -> str | None:
    """Return the `logbook_<key>` message of `exceptions` in the server's language (`cached_text`)."""
    return cached_text(hass, "exceptions", f"logbook_{key}.message", placeholders)


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Register the describers of the button-action, scene-recalled and plan events."""
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
            letter = str(data.get(ATTR_KEY, "?"))
            key = (
                cached_text(hass, "entity", "event.button.name", {"key": letter})
                or letter
            )
            device_name = _device_name(data.get(ATTR_DEVICE_ID))
            name = f"{device_name} {key}" if device_name else key
        event_type = str(data.get(CONF_TYPE))
        if event_type == "scene":
            scene = str(data.get(ATTR_SCENE, "?"))
            message = logbook_message(hass, "scene_from_key", {"scene": scene}) or scene
        else:
            message = (
                cached_text(hass, "device_automation", f"trigger_subtype.{event_type}")
                or event_type
            )
        if (reason := data.get(ATTR_REASON)) is not None:
            said = logbook_message(hass, f"hold_end_{reason}") or str(reason)
            message += f" ({said})"
        entry = {LOGBOOK_ENTRY_NAME: name, LOGBOOK_ENTRY_MESSAGE: message}
        if entity_id:
            entry[LOGBOOK_ENTRY_ENTITY_ID] = str(entity_id)
        return entry

    @callback
    def describe_scene_recalled(event: Event) -> dict[str, str]:
        """'All off was recalled by Living room rocker', filed under the scene entity when the export has it."""
        data = event.data
        name = data.get(ATTR_NAME) or (
            logbook_message(hass, "scene_number", {"scene": str(data[ATTR_SCENE])})
            if ATTR_SCENE in data
            else logbook_message(hass, "scene")
        )
        if (device_name := _device_name(data.get(ATTR_DEVICE_ID))) is not None:
            message = logbook_message(hass, "recalled_by", {"device": device_name})
        else:
            message = logbook_message(hass, "recalled")
        entry = {
            LOGBOOK_ENTRY_NAME: str(name or ""),
            LOGBOOK_ENTRY_MESSAGE: message or "",
        }
        entity_id = data.get(ATTR_ENTITY_ID)
        if entity_id:
            entry[LOGBOOK_ENTRY_ENTITY_ID] = str(entity_id)
        return entry

    @callback
    def describe_plan(event: Event) -> dict[str, str]:
        """'JUNG HOME Key 0151 (…) now drives room Kitchen; 6 messages': the network's name, the action's line."""
        data = event.data
        return {
            LOGBOOK_ENTRY_NAME: str(data.get(ATTR_NAME) or DOMAIN),
            LOGBOOK_ENTRY_MESSAGE: plan_message(
                hass, str(data.get("message")), dict(data.get("placeholders") or {})
            ),
        }

    async_describe_event(DOMAIN, EVENT_PLAN, describe_plan)
    async_describe_event(DOMAIN, EVENT_BUTTON_ACTION, describe_button_action)
    async_describe_event(DOMAIN, EVENT_SCENE_RECALLED, describe_scene_recalled)
