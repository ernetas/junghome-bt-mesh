"""Rocker / push-button events.

Buttons linked to the gateway publish JUNG vendor events (click / hold_start / hold_end; double_click is derived).
A rocker in that mode is one element with two halves: its events carry a `side` attribute (`down` / `up`, see
`const.KEY_EVENTS`), a single key's do not. Buttons wired directly to loads or scenes are seen through the SIG
messages they send (press_on / press_off / scene / dim). Each entity also says what its key drives
(`connection_attributes`); a key in KeyMode *property* is asked for its 0x5006 / 0x5007 once per link, so a key that
locks a light or socket says `lock` (`devices.with_key_lock`, unverified on air).

Every event of a key is also published on the Home Assistant bus as `EVENT_BUTTON_ACTION`, because a device trigger
(`device_trigger.py`) can only attach to a bus event, not to an entity (this is how HA's own button integrations do
it). The hub publishes it (`publish_button_event`, from `JungHomeHub.fire_button`), not the entity: a key whose event
entity is disabled keeps its device triggers and logbook lines (review-4 H4-2), the event then without `entity_id`.
A `scene` event additionally publishes `EVENT_SCENE_RECALLED`, named after the scene, so automations and the logbook
can follow scene recalls without knowing which key is wired to which scene. The hub publishes the same event for the
app's and the gateway's recalls, for our own, and — from the Scene Status the members publish after a recall — for
one Home Assistant did not hear (`coordinator._on_scene_status`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID, ATTR_NAME, CONF_TYPE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .config_entities import property_reader
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
from .entity import (
    JungHomeEntity,
    async_setup_platform,
    button_gang,
    buttons_device_id,
    buttons_device_info,
)
from .jhmesh import properties as P
from .jhmesh.devices import Button, with_key_lock
from .scene import scene_unique_id

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import JungHomeConfigEntry
    from .coordinator import JungHomeHub

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
    """Add the entities of `build_entities`, kept with the hub to follow a new export in place (`model_update`)."""
    async_setup_platform(entry.runtime_data, "event", build_entities, add_entities)


def build_entities(hub: JungHomeHub) -> list[JungHomeButtonEvent]:
    """Return one event entity per button."""
    return [JungHomeButtonEvent(hub, button) for button in hub.devices.buttons]


def connection_attributes(
    button: Button, values: Mapping[int, bytes] | None = None
) -> dict[str, Any]:
    """Return what the key drives, from the export (`devices.KeyConnection`): kind, address, name / scene if known.

    `connection_address` is the load element, the room group or the group the key publishes to. The export is
    what the app and the `assign_key` / `clear_key` actions wrote; the entity follows every such action in place
    (`model_update`). `values` are the key's cached properties: a key in property mode whose 0x5006 / 0x5007 say it
    locks its target is `lock`, with `connection_lock_seconds` (0: until unlocked). Unverified on air.
    """
    connection = with_key_lock(button.connection, values or {})
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
    if connection.lock is not None:
        attrs["connection_lock_seconds"] = connection.lock.time_s
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


@callback
def publish_button_event(
    hass: HomeAssistant,
    hub: JungHomeHub,
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


class JungHomeButtonEvent(JungHomeEntity, EventEntity):
    """One key of a push-button node; fires the gesture events the node reports."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = EVENT_TYPES
    _attr_translation_key = "button"
    # `hub.link_count` of the link the key's property mode was last asked for on (`_maybe_read_property_mode`)
    _property_link: int | None = None

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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the key's address, location and connection; where it sits on its node once the key layout is known.

        The `position` (`inserts.NodeInserts.position`: `top`, `left_rocker`, `right_bottom`, ...) comes from the
        node's ButtonLayout — its export, its advertisement or its answer to a Get — so it can arrive after the
        entity was added; the hub then tells the entity to write its state again.
        """
        attrs = dict(self._attr_extra_state_attributes)
        state = self.hub.states.get(self.address)
        attrs.update(
            connection_attributes(
                self.button, state.properties if state is not None else None
            )
        )
        if (position := self.hub.inserts.position(self.button)) is not None:
            attrs["position"] = position
        return attrs

    async def async_added_to_hass(self) -> None:
        """Also subscribe to the hub's button events for this address; ask a key in property mode what it sets."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hub.add_event_listener(self.address, self._on_event))
        self._maybe_read_property_mode()

    @callback
    def async_model_rebound(self) -> None:
        """Ask a key the new export shows in property mode what it sets (`_maybe_read_property_mode`)."""
        self._maybe_read_property_mode()

    @callback
    def _handle_update(self) -> None:
        self._maybe_read_property_mode()
        super()._handle_update()

    @callback
    def _maybe_read_property_mode(self) -> None:
        """Queue the read of the key's 0x5006 / 0x5007 once per link, for a mains key the export shows in property mode.

        The export cannot tell a lock link from any other property-mode link (`KeyConnection.property_mode`); the
        app reads the same two properties for it (`GetConnection`). A battery key sleeps through such a read: it
        shows what its last writes told (`assign_key`'s Statuses), else its device link.
        """
        connection = self.button.connection
        if (
            connection is None
            or not connection.property_mode
            or self.button.battery
            or not self.hub.connected
            or self._property_link == self.hub.link_count
        ):
            return
        self._property_link = self.hub.link_count
        property_reader(self.hass, self.hub).schedule(
            self.address, self._read_property_mode, key=P.KEY_PROPERTY_MODE
        )

    async def _read_property_mode(self) -> None:
        """Ask for KeySetPropertyMode and the up value, unless the key answered them since the link came up."""
        reader = property_reader(self.hass, self.hub)
        for prop in (P.KEY_PROPERTY_MODE, P.KEY_VALUE_UP):
            await reader.read(
                self.address, P.PROPERTIES[prop], since=self.hub.link_since
            )

    @callback
    def _on_event(self, event_type: str, attrs: dict[str, Any]) -> None:
        """Show the event on the entity; the hub publishes it on the bus itself (`publish_button_event`)."""
        if event_type not in EVENT_TYPES:
            return  # an unknown vendor event code; EventEntity rejects event types it was not declared with
        self._trigger_event(event_type, attrs)
        self.async_write_ha_state()
