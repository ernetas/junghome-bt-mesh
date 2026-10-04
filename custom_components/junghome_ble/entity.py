"""The entity base (`JungHomeEntity`) and the platforms' bookkeeping with the hub.

The device-registry model — the registry layout, the device identifiers, the devices' names, rooms and areas — is
`device_info.py` (review-4 A4-11), re-exported here for the platforms.

Every platform builds its entities from the hub's device model (`build_entities`) and keeps them with the hub
(`async_setup_platform`, `TrackedPlatform`): an action that rewrote the export then has `model_update` build them
again from the new model and carry the change over to the running entities, rather than reload the entry.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from functools import partial
from typing import TYPE_CHECKING, Final, Self

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.util.hass_dict import HassKey

from .const import (
    DOMAIN,
    SIGNAL_CONNECTION,
    SIGNAL_UPDATE,
)
from .device_info import (
    LOAD_DOMAINS,
    MODEL_NAMES,
    PRODUCT_KEYS,
    PRODUCT_NAMES,
    ROOM_KINDS,
    NodeRegistryFields,
    blind_device_info,
    button_gang,
    buttons_device_id,
    buttons_device_info,
    connection_room,
    current_device_identifiers,
    current_room_central_ids,
    device_rooms,
    first_room,
    gang_room,
    health_nodes,
    hub_device_info,
    light_device_info,
    load_entity_id,
    mesh_identifier,
    metered_device_info,
    model_labels,
    model_name,
    node_device_info,
    node_device_name,
    node_gangs,
    node_identifier,
    node_loads,
    node_registry_fields,
    node_room,
    node_text,
    node_unit,
    node_unit_device_info,
    product_name,
    register_parent_devices,
    room_area_name,
    room_central_id,
    room_central_prefix,
    room_loads,
    socket_device_info,
    software_version,
    update_buttons_devices,
    update_node_device,
)
from .errors import mesh_errors
from .jhmesh.devices import (
    BATTERY_PIDS,
)
from .protocols import TrackedPlatform

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import JungHomeHub
    from .jhmesh.devices import Device

_LOGGER = logging.getLogger(__name__)


# `homeassistant.update_entity` asks an element for the same thing at most once per this many seconds
# (`JungHomeEntity.async_update`); a call within it keeps the cached state.
UPDATE_READ_INTERVAL: Final = 2.0


# re-exported: the device-registry model lived here before `device_info.py`
__all__ = [
    "LOAD_DOMAINS",
    "MODEL_NAMES",
    "PRODUCT_KEYS",
    "PRODUCT_NAMES",
    "ROOM_KINDS",
    "NodeRegistryFields",
    "blind_device_info",
    "button_gang",
    "buttons_device_id",
    "buttons_device_info",
    "connection_room",
    "current_device_identifiers",
    "current_room_central_ids",
    "device_rooms",
    "first_room",
    "gang_room",
    "health_nodes",
    "hub_device_info",
    "light_device_info",
    "load_entity_id",
    "mesh_identifier",
    "metered_device_info",
    "model_labels",
    "model_name",
    "node_device_info",
    "node_device_name",
    "node_gangs",
    "node_identifier",
    "node_loads",
    "node_registry_fields",
    "node_room",
    "node_text",
    "node_unit",
    "node_unit_device_info",
    "product_name",
    "register_parent_devices",
    "room_area_name",
    "room_central_id",
    "room_central_prefix",
    "room_loads",
    "socket_device_info",
    "software_version",
    "update_buttons_devices",
    "update_node_device",
]

# what `homeassistant.update_entity` reads: a name for the rate limit (`update_reads`), and the read itself
type UpdateRead = tuple[str, Callable[[], Awaitable[object]]]
# entry id -> (element, what was read) -> when `homeassistant.update_entity` last asked for it (`time.monotonic()`)
UPDATE_READS: HassKey[dict[str, dict[tuple[int, str], float]]] = HassKey(
    f"{DOMAIN}_update_reads"
)

# a platform's `build_entities`: every entity the hub's device model gives the platform, the disabled ones included
type EntityBuilder = Callable[[JungHomeHub], Iterable[Entity]]


def entities_by_unique_id(entities: Iterable[Entity]) -> dict[str, Entity]:
    """Index a builder's entities by unique id (every entity of the integration has one)."""
    out: dict[str, Entity] = {}
    for entity in entities:
        assert entity.unique_id is not None
        out[entity.unique_id] = entity
    return out


@callback
def async_setup_platform(
    hub: JungHomeHub,
    domain: str,
    build: EntityBuilder,
    add: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the entities `build` makes from the hub's model, and keep them with the hub (`TrackedPlatform`)."""
    entities = entities_by_unique_id(build(hub))
    hub.platforms[domain] = TrackedPlatform(
        build,
        add,
        entities,
        {unique_id: dict(vars(e)) for unique_id, e in entities.items()},
    )
    add(list(entities.values()))


def update_reads(hub: JungHomeHub) -> dict[tuple[int, str], float]:
    """Return the hub's record of `homeassistant.update_entity` reads, created on first use, dropped on unload."""
    reads = hub.hass.data.setdefault(UPDATE_READS, {})
    entry_id = hub.entry.entry_id
    if entry_id not in reads:
        reads[entry_id] = {}

        def forget() -> None:
            reads.pop(entry_id, None)

        hub.entry.async_on_unload(forget)
    return reads[entry_id]


class JungHomeEntity(Entity):
    """Push-updated entity bound to one mesh element."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    # the state Get `homeassistant.update_entity` asks the element with (`STATE_GETS`); None: nothing to ask
    _refresh_kind: str | None = None

    def __init__(
        self,
        hub: JungHomeHub,
        address: int,
        unique_id: str,
        device_info: DeviceInfo | None,
    ) -> None:
        """Bind the entity to `hub` and the element at `address`."""
        self.hub = hub
        self.address = address
        self._attr_unique_id = unique_id
        if device_info is not None:
            self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        """Available while the hub has a proxy link — and, with heartbeats on, while the node is heard from."""
        return self.hub.link_available and self.hub.node_alive(self.address)

    async def async_added_to_hass(self) -> None:
        """Subscribe to state updates of the element and to link state changes."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE.format(self.hub.entry.entry_id, self.address),
                self._handle_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CONNECTION.format(self.hub.entry.entry_id),
                self._handle_update,
            )
        )

    @property
    def listened(self) -> tuple[int, ...]:
        """The other elements the entity follows, subscribed to once when it is added: none by default."""
        return ()

    def rebind_refusal(self, fresh: Self) -> str | None:
        """Why the entity cannot take over the model `fresh` was built from in place; None when it can (`model_update`).

        Its subscriptions go by element address, made once: an entity whose element moved, or that would follow
        other elements now (`listened`), is set up again by a reload.
        """
        if fresh.address != self.address:
            return f"its element moved from {self.address:04X} to {fresh.address:04X}"
        if fresh.listened != self.listened:
            return "it follows other elements now"
        return None

    @callback
    def async_model_rebound(self) -> None:
        """Follow what a new model changed beyond the attributes `model_update` carried over; nothing by default."""

    @callback
    def _handle_update(self) -> None:
        """Write the state, unless what the entity shows is what Home Assistant already holds for it.

        One element's status reaches every entity bound to it (a busy one has over twenty), and most show nothing
        that changed (review-4 R I-10). The comparison is with the state machine itself, not with this entity's
        last write — a command's own write (an assumed state) counts too — and covers the state string, so a change
        of availability (`unavailable`) is always written; so is every attribute.
        """
        if self._shows_current_state():
            return
        self.async_write_ha_state()

    def _shows_current_state(self) -> bool:
        """Whether the state and attributes the entity would write are those the state machine holds now."""
        current = self.hass.states.get(self.entity_id)
        if current is None:
            return False
        rendered = self._async_calculate_state()
        return (
            current.state == rendered.state
            and current.attributes == rendered.attributes
        )

    async def async_update(self) -> None:
        """Ask the device for what the entity shows (`homeassistant.update_entity`), rate-limited; never raises.

        The entity is push-updated (`should_poll` is off), so Home Assistant calls this for the action alone: an
        automation that wants a value fresh rather than as last heard (an LED colour or a run-on time changed in the
        app, answered to the app's address only; review-4 H4-10). The answers update the state cache as every
        status does. What is read is the entity's `_update_read`; the same thing of an element is asked at most once
        per UPDATE_READ_INTERVAL, so an automation updating a whole device, or a loop, does not flood the mesh. A
        battery node sleeps and would not answer (its values are read when a key wakes it), and without a link
        there is nobody to ask: both keep the cached state and log at DEBUG — so does a read the link drops under
        it, since Home Assistant logs a failed update as an error, and an unanswered read is no error either.
        """
        read = self._update_read()
        if read is None:
            return
        what, job = read
        node = self.hub.cdb.node_by_addr(self.address)
        if node is not None and node.pid in BATTERY_PIDS:
            _LOGGER.debug(
                "%04X: not read for %s: a battery node sleeps", self.address, what
            )
            return
        if not self.hub.connected:
            _LOGGER.debug("%04X: not read for %s: no link", self.address, what)
            return
        reads = update_reads(self.hub)
        now = time.monotonic()
        last = reads.get((self.address, what))
        if last is not None and now - last < UPDATE_READ_INTERVAL:
            _LOGGER.debug(
                "%04X: %s was read %.1f s ago; not again yet",
                self.address,
                what,
                now - last,
            )
            return
        reads[self.address, what] = now
        try:
            await job()
        except (HomeAssistantError, ConnectionError, OSError) as err:
            _LOGGER.debug("%04X: %s not read: %r", self.address, what, err)

    def _update_read(self) -> UpdateRead | None:
        """Return what `async_update` reads: by default the state Get of the element's kind (`_refresh_kind`), if any."""
        kind = self._refresh_kind
        if kind is None:
            return None
        return kind, partial(self.hub.async_refresh_element, self.address, kind)

    async def _send(self, command: Awaitable[None]) -> None:
        """Run a command; report a load that did not answer it, or a link that could not carry it.

        A load's command waits for its status like the app's (`JungHomeHub._load_command`): unanswered through every
        attempt, the action fails with `device_not_reachable`, and the node is marked unreachable — its entities go
        unavailable, as the app shows "No connection" — unless it was heard from meanwhile or the proxy turns out
        to be the one that stopped answering, which the message allows for. TimeoutError is an OSError: it is told
        apart first.
        """
        with mesh_errors(
            timeout_key="device_not_reachable",
            placeholders=lambda: {"entity": self.entity_id},
        ):
            await command


class JungHomeCentralEntity(JungHomeEntity):
    """One of the app's central functions: every device of a kind, in the whole home or in one room.

    Home-wide (`room` None) it is the device-type `group`, commanded by one group message (`UpdateDeviceTypeGroup`).
    In a room it is the app's area sheet (`CentralFunctionsActivity` with the room's address): on / off, positions,
    slats and set-points go to each member as an Unacknowledged Set of its own, while a dim level and a blind stop
    go to the room address as one message (`Dim.Group`, `OpenClose.Group`) — the room address alone would reach
    every kind of load in it. The address the entity stands for (`address`) is the device-type group or the room.
    On the mesh (service) device, not in the room's area, so an area action does not reach the loads twice; the
    state derived from the members' (`watched` are the elements it follows). Available while the link is up, or in its
    loss grace — the messages reach whoever is there.

    A room's entity starts hidden (decision M9): outside every area it landed among the unassigned entities of the
    auto-generated dashboards, one per room and kind, and was exposed to Assist next to the loads it duplicates.
    Home Assistant applies the flag when it first registers the entity only, so an installation that registered it
    before keeps it as it was; the home-wide *All …* entities stay visible.
    """

    def __init__(
        self,
        hub: JungHomeHub,
        group: int,
        members: list[Device],
        kind: str,
        room: int | None = None,
    ) -> None:
        """Bind to the device-type `group` (translation key `all_<kind>`), or to `room` (`room_<kind>`), and the members."""
        mesh = hub.cdb.mesh_uuid.lower()
        if room is None:
            unique_id, address = f"{mesh}-central-{group:04x}", group
            self._attr_translation_key = f"all_{kind}"
        else:
            unique_id, address = room_central_id(hub, room, kind), room
            self._attr_translation_key = f"room_{kind}"
            self._attr_entity_registry_visible_default = False
            self._attr_translation_placeholders = {"room": hub.devices.rooms[room]}
        super().__init__(hub, address, unique_id, hub_device_info(hub))
        self.room = room
        self.members = members
        self._attr_extra_state_attributes = {
            "mesh_address": f"{address:04X}",
            "members": [m.name for m in members],
        }

    @property
    def watched(self) -> list[int]:
        """The elements whose state the entity shows: the members'."""
        return [m.address for m in self.members]

    @property
    def available(self) -> bool:
        """Available while the hub has a proxy link, or lost one less than LINK_LOSS_GRACE ago, as the loads' entities.

        A command in the grace waits for the next link (`JungHomeHub._command`); unavailable, Home Assistant would
        skip the entity in an action and drop the command (review-4 R4-6).
        """
        return self.hub.link_available

    # the elements followed now, and the subscriptions that follow them (`_watch`)
    _watching: tuple[int, ...] = ()
    _unwatch_all: tuple[CALLBACK_TYPE, ...] = ()

    async def async_added_to_hass(self) -> None:
        """Also follow every watched element's state."""
        await super().async_added_to_hass()
        self._watch()
        self.async_on_remove(self._unwatch)

    @callback
    def _watch(self) -> None:
        """Follow the state of every element in `watched`, in place of those followed before."""
        self._unwatch()
        self._watching = tuple(self.watched)
        self._unwatch_all = tuple(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE.format(self.hub.entry.entry_id, address),
                self._handle_update,
            )
            for address in self._watching
        )

    @callback
    def _unwatch(self) -> None:
        for unsub in self._unwatch_all:
            unsub()
        self._unwatch_all = ()

    @callback
    def async_model_rebound(self) -> None:
        """Follow the members of the new model: a room's loads change with `set_room`, `delete_room`, an export."""
        if tuple(self.watched) != self._watching:
            self._watch()

    def members_on(self) -> bool | None:
        """On while any member is; None until one of them has reported."""
        known = [
            st.on
            for m in self.members
            if (st := self.hub.states.get(m.address)) and st.on is not None
        ]
        return any(known) if known else None

    async def _switch(
        self, on: bool, lightness: int | None = None, transition: float | None = None
    ) -> None:
        """Switch every member, the dimmable ones to `lightness` first when given, over `transition` s when given."""
        if self.room is None:
            await self._send(
                self.hub.central_command(self.address, on, lightness, transition)
            )
        else:
            addresses = [m.address for m in self.members]
            await self._send(
                self.hub.room_command(self.room, addresses, on, lightness, transition)
            )

    async def _level(self, group: int, addresses: list[int], level: int) -> None:
        """Set a Generic Level: once to the device-type `group` home-wide, on each of `addresses` in a room."""
        if self.room is None:
            await self._send(self.hub.central_level(group, level))
        else:
            await self._send(self.hub.room_level(addresses, level))
