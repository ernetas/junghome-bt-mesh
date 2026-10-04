"""The integration's own bus events: a key's `EVENT_BUTTON_ACTION` and a scene's `EVENT_SCENE_RECALLED`.

Every event of a key is also published on the Home Assistant bus as `EVENT_BUTTON_ACTION`, because a device trigger
(`device_trigger.py`) can only attach to a bus event, not to an entity (this is how HA's own button integrations do
it). The hub publishes it (`publish_button_event`, from `JungHomeHub.fire_button`), not the entity: a key whose event
entity is disabled keeps its device triggers and logbook lines, the event then without `entity_id`.
A `scene` event additionally publishes `EVENT_SCENE_RECALLED`, named after the scene (`fire_scene_recalled`). They
lived in the event platform (`event.py`, which re-exports them); here the hub fires them without importing a
platform module or itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID, ATTR_NAME, CONF_TYPE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
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
from .device_info import button_gang, buttons_device_id
from .jhmesh.devices import Button
from .protocols import HubView

if TYPE_CHECKING:
    from .jhmesh.devices import Metadata


class BusHub(HubView, Protocol):
    """What the bus events ask of the hub besides `HubView`: the app's scene names and the recall window."""

    @property
    def metadata(self) -> Metadata:
        """The app's names (devices, scenes) the hub's device model was built with."""

    def note_scene_recall(self, number: int) -> bool:
        """Note that EVENT_SCENE_RECALLED fires for `number` now; False when it fired within the recall window."""


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


def scene_unique_id(hub: BusHub, number: int) -> str:
    """Return the unique id of scene `number`'s entity (the scene event links the entity by it, `event.py`)."""
    return f"{hub.cdb.mesh_uuid.lower()}-scene-{number}"


def scene_name(hub: BusHub, number: int) -> str:
    """Return the app's name for scene `number`, or a generic one for a scene the export does not know."""
    for scene in hub.devices.scenes:
        if scene.number == number:
            return scene.name
    return hub.metadata.scenes.get(number, f"Scene {number}")


@callback
def fire_scene_recalled(
    hass: HomeAssistant,
    hub: BusHub,
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


@callback
def publish_button_event(
    hass: HomeAssistant,
    hub: BusHub,
    addr: int,
    event_type: str,
    attrs: dict[str, Any],
) -> None:
    """Publish a key's event on the bus for device triggers and the logbook, plus the scene event for a recall.

    Called by the hub for every event it delivers (`JungHomeHub.fire_button`), after the key's event entity — if it
    is enabled — took it: once per event, whether or not that entity exists. An element that is no key, and an
    event type the entities do not declare (an unknown vendor code), publish nothing. The button event is skipped
    while the key's buttons device is not in the device registry: a device trigger is keyed on the device id, so an
    event without one could match nothing. `entity_id` is the key's event entity, left out while it is disabled or
    not registered (the logbook then names the device and key). The buttons device is looked up in the registry by
    its identifier: `hub.device_ids` only holds the parents registered up front (`register_parent_devices`).
    """
    button = hub.devices.by_address.get(addr)
    if not isinstance(button, Button) or event_type not in EVENT_TYPES:
        return
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, buttons_device_id(button_gang(hub, button))), hub.entry.entry_id
    )
    device_id = None if device is None else device.id
    if device_id is not None:
        data: dict[str, Any] = {ATTR_DEVICE_ID: device_id}
        entities = er.async_get(hass)
        entity_id = entities.async_get_entity_id("event", DOMAIN, button.unique_id)
        entry = entities.async_get(entity_id) if entity_id is not None else None
        if entry is not None and not entry.disabled:
            data[ATTR_ENTITY_ID] = entry.entity_id
        data |= {ATTR_KEY: button.key, CONF_TYPE: event_type, **attrs}
        hass.bus.async_fire(EVENT_BUTTON_ACTION, data)
    if event_type == "scene":  # the coordinator always attaches the scene number
        fire_scene_recalled(hass, hub, attrs[ATTR_SCENE], addr, device_id=device_id)
