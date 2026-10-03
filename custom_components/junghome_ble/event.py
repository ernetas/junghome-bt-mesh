"""Rocker / push-button events.

Buttons linked to the gateway publish JUNG vendor events (click / hold_start / hold_end; double_click is derived).
A rocker in that mode is one element with two halves: its events carry a `side` attribute (`down` / `up`, see
`const.KEY_EVENTS`), a single key's do not. Buttons wired directly to loads or scenes are seen through the SIG
messages they send (press_on / press_off / scene / dim). Each entity also says what its key drives
(`connection_attributes`).

Every event an entity fires is also published on the Home Assistant bus as `EVENT_BUTTON_ACTION`, because a
device trigger (`device_trigger.py`) can only attach to a bus event, not to an entity (this is how HA's own button
integrations do it). A `scene` event additionally publishes `EVENT_SCENE_RECALLED`, named after the scene, so
automations and the logbook can follow scene recalls without knowing which key is wired to which scene. The hub
publishes the same event for the app's and the gateway's recalls, for our own, and — from the Scene Status the
members publish after a recall — for one Home Assistant did not hear (`coordinator._on_scene_status`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID, ATTR_NAME, CONF_TYPE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    ATTR_ENTRY_ID,
    ATTR_KEY,
    ATTR_REPORTED_BY,
    ATTR_SCENE,
    ATTR_SOURCE,
    DOMAIN,
    EVENT_BUTTON_ACTION,
    EVENT_SCENE_RECALLED,
    SIGNAL_SCENE_RECALLED,
)
from .entity import JungHomeEntity, button_gang, buttons_device_info
from .scene import scene_unique_id

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import JungHomeConfigEntry
    from .coordinator import JungHomeHub
    from .jhmesh.devices import Button

PARALLEL_UPDATES = 0  # push-based

EVENT_TYPES = [
    "click",
    "double_click",
    "hold_start",
    "hold_end",
    "press_on",
    "press_off",
    "scene",
    "dim",
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one event entity per button."""
    hub = entry.runtime_data
    add_entities(JungHomeButtonEvent(hub, button) for button in hub.devices.buttons)


def connection_attributes(button: Button) -> dict[str, Any]:
    """Return what the key drives, from the export (`devices.KeyConnection`): kind, address, name / scene if known.

    `connection_address` is the load element, the room group or the group the key publishes to. The export is
    what the app and the `assign_key` / `clear_key` actions wrote; the entry reloads after every such action.
    """
    connection = button.connection
    if connection is None:
        return {"connection": "none"}
    target = connection.address if connection.target is None else connection.target
    attrs: dict[str, Any] = {
        "connection": connection.kind,
        "connection_address": f"{target:04X}",
    }
    if connection.name:
        attrs["connection_name"] = connection.name
    if connection.scene is not None:
        attrs["connection_scene"] = (
            connection.scene
        )  # not `scene`: a scene event carries that
    return attrs


def scene_name(hub: JungHomeHub, number: int) -> str:
    """Return the app's name for scene `number`, or a generic one for a scene the export does not know."""
    for scene in hub.devices.scenes:
        if scene.number == number:
            return scene.name
    return hub.metadata.scenes.get(number, f"Scene {number}")


@callback
def fire_scene_recalled(
    hass: HomeAssistant,
    hub: JungHomeHub,
    number: int,
    source: int | None,
    *,
    device_id: str | None = None,
    reported_by: int | None = None,
) -> None:
    """Publish `EVENT_SCENE_RECALLED` for scene `number`, recalled by the element at `source`.

    `device_id` is the buttons device of the key that sent the recall, when it was a key. `source` is None for a
    recall only known from a member's Scene Status: `reported_by` is that member. The scene entity is linked
    through `entity_id` when the export defines the scene (the unique id mirrors `scene.py`), so the logbook can
    file the line under it; the entity itself hears of the recall through SIGNAL_SCENE_RECALLED.
    """
    hub.note_scene_recall(number)
    data: dict[str, Any] = {
        ATTR_SCENE: number,
        ATTR_NAME: scene_name(hub, number),
        ATTR_ENTRY_ID: hub.entry.entry_id,
    }
    if source is not None:
        data[ATTR_SOURCE] = f"{source:04X}"
    if reported_by is not None:
        data[ATTR_REPORTED_BY] = f"{reported_by:04X}"
    if device_id is not None:
        data[ATTR_DEVICE_ID] = device_id
    entity_id = er.async_get(hass).async_get_entity_id(
        "scene", DOMAIN, scene_unique_id(hub, number)
    )
    if entity_id is not None:
        data[ATTR_ENTITY_ID] = entity_id
    hass.bus.async_fire(EVENT_SCENE_RECALLED, data)
    async_dispatcher_send(
        hass, SIGNAL_SCENE_RECALLED.format(hub.entry.entry_id), number, source
    )


class JungHomeButtonEvent(JungHomeEntity, EventEntity):
    """One key of a push-button node; fires the gesture events the node reports."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = EVENT_TYPES
    _attr_translation_key = "button"

    def __init__(self, hub: JungHomeHub, button: Button) -> None:
        """Bind to `button`; a single-key gang names the device, a multi-key gang names each key."""
        gang = button_gang(
            hub, button
        )  # the keys sharing this button's device (one per app-named gang, see entity.py)
        super().__init__(
            hub, button.address, button.unique_id, buttons_device_info(hub, gang)
        )
        self.button = button
        if button.input:
            self._attr_translation_key = "input"  # a mini actuator's E1 / E2
        if len(gang) == 1:
            self._attr_name = None  # a single key: the device *is* the button
        else:
            self._attr_translation_placeholders = {"key": button.key}
        self._attr_extra_state_attributes = {
            "mesh_address": f"{button.address:04X}",
            "location": f"{button.location:04X}",
            **connection_attributes(button),
        }

    async def async_added_to_hass(self) -> None:
        """Also subscribe to the hub's button events for this address."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hub.add_event_listener(self.address, self._on_event))

    @callback
    def _on_event(self, event_type: str, attrs: dict[str, Any]) -> None:
        if event_type not in EVENT_TYPES:
            return  # an unknown vendor event code; EventEntity rejects event types it was not declared with
        self._trigger_event(event_type, attrs)
        self.async_write_ha_state()
        self._fire_bus_events(event_type, attrs)

    @callback
    def _fire_bus_events(self, event_type: str, attrs: dict[str, Any]) -> None:
        """Re-emit the event on the bus for device triggers, plus the scene event for a scene recall.

        Skipped while the entity is not in the device registry yet: a device trigger is keyed on the device id,
        so an event without one could match nothing.
        """
        device_entry = self.device_entry
        if device_entry is None:
            return
        self.hass.bus.async_fire(
            EVENT_BUTTON_ACTION,
            {
                ATTR_DEVICE_ID: device_entry.id,
                ATTR_ENTITY_ID: self.entity_id,
                ATTR_KEY: self.button.key,
                CONF_TYPE: event_type,
                **attrs,
            },
        )
        if event_type == "scene":  # the coordinator always attaches the scene number
            fire_scene_recalled(
                self.hass,
                self.hub,
                attrs[ATTR_SCENE],
                self.address,
                device_id=device_entry.id,
            )
