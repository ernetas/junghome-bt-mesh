"""Entity base and device-registry model.

Registry layout (mirrors how the JUNG app presents the installation):
- one *service* device for the mesh network itself (scenes, link diagnostics),
- one device per physical node (product, MAC, address) — the parent of
- one device per app-level thing: each load (light / socket) and each *gang* of keys.

Device identifiers (all under the integration domain, node UUIDs lower-case):
- ``mesh:{mesh uuid}`` — the network,
- ``node:{node uuid}`` — a physical node,
- ``{node uuid}-{location:04x}`` — a load: the element's unique id (location 0001 / 0002),
- ``{node uuid}-{location:04x}-buttons`` — a gang of keys, ``location`` being the lowest key location of the
  gang. A gang is the set of a node's keys the app presents as one device: the keys whose element locations sit
  in the same app device entry (`Metadata.entry_for`, carried as `Button.gang`), so two gangs the user gave the
  same name stay two devices. Without app metadata every key of a node falls into one gang (named after the
  node), so a node has one buttons device as before.

Every platform builds its entities from the hub's device model (`build_entities`) and keeps them with the hub
(`async_setup_platform`, `TrackedPlatform`): an action that rewrote the export then has `model_update` build them
again from the new model and carry the change over to the running entities, rather than reload the entry.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import (
    CONNECTION_BLUETOOTH,
    DeviceEntryType,
    DeviceInfo,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import (
    DOMAIN,
    NODE_INFO,
    SIG_HARDWARE_REVISION,
    SIG_MANUFACTURER_NAME,
    SIG_SOFTWARE_VERSION,
    SIGNAL_CONNECTION,
    SIGNAL_UPDATE,
)
from .jhmesh import properties as P
from .jhmesh.advert import mac_from_uuid
from .jhmesh.devices import BATTERY_PIDS, Blind, Light, Socket, Thermostat

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import JungHomeHub
    from .jhmesh.cdb import Node
    from .jhmesh.devices import Button, Device, MeteredLoad

# Product ids from the gateway firmware's `btmesh_product_ids.js` (docs/cross-repo-analysis.md §1.4).
PRODUCT_NAMES = {
    0x01: "Push-button 1-gang",
    0x02: "Push-button 2-gang",
    0x03: "Socket (metering)",
    0x0C: "Socket",
    0x04: "Switch actuator 1-gang mini",
    0x0D: "Blinds actuator 1-gang mini",
    0x05: "Wall transmitter 1-gang",
    0x06: "Wall transmitter 2-gang",
    0x07: "Motion detector 1 m",
    0x08: "Motion detector 2 m",
    0x09: "Presence detector",
    0x0A: "Room thermostat",
    0x0B: "Gateway",
    0x10: "Switch actuator 1-gang 2-input energy",
    0x11: "Switch actuator 2-gang 2-input",
    0x12: "Dimmer actuator 1-gang 2-input",
    0x13: "Blinds PP2 actuator 1-gang 2-input",
    0x14: "DALI actuator 1-gang 2-input",
    0x15: "Mini sensor 2-input mains",
    0x16: "Mini sensor 2-input battery",
}
LIGHT_MODELS = {
    "switch": "Switched light",
    "dimmer": "Dimmable light",
    "ctl": "Tunable-white (DALI) light",
}


def product_name(pid: int | None) -> str:
    """Return the product name for a PID, or a generic placeholder."""
    return PRODUCT_NAMES.get(pid or -1, f"Product {pid}")


def mesh_identifier(hub: JungHomeHub) -> str:
    """Return the device identifier of the mesh (service) device."""
    return f"mesh:{hub.cdb.mesh_uuid.lower()}"


def node_identifier(node: Node) -> str:
    """Return the device identifier of a node device."""
    return f"node:{node.uuid.lower()}"


def hub_device_info(hub: JungHomeHub) -> DeviceInfo:
    """Return the device info of the mesh (service) device."""
    return DeviceInfo(
        identifiers={(DOMAIN, mesh_identifier(hub))},
        name=hub.entry.title,
        manufacturer="JUNG",
        model="Bluetooth Mesh network",
        entry_type=DeviceEntryType.SERVICE,
    )


def software_version(hub: JungHomeHub, node: Node) -> str | None:
    """Return the node's software version (SIG 0x001A, e.g. `"2.2.0.2"`) once it has been read, else None."""
    state = hub.states.get(node.unicast)
    raw = state.properties.get(SIG_SOFTWARE_VERSION) if state else None
    if raw is None:
        return None
    try:
        return P.ASCII_VERSION.decode(raw)
    except ValueError:
        return None


def node_text(hub: JungHomeHub, node: Node, pid: int) -> str | None:
    """Return a text of the node's SIG identity (0x0010, 0x0011) once it has been read; None before, or when blank."""
    raw = hub.node_info(node.unicast).get(NODE_INFO[pid])
    return (P.SIG_PROPERTIES[pid].codec.decode(raw) or None) if raw else None


@dataclass(frozen=True)
class NodeRegistryFields:
    """What the node told about itself, as device-registry fields (None: not known, left as it is)."""

    manufacturer: str
    sw_version: str | None
    hw_version: str | None


def node_registry_fields(hub: JungHomeHub, node: Node) -> NodeRegistryFields:
    """Return the manufacturer and versions the node reported (SIG 0x0011 / 0x001A / 0x0010).

    The hardware revision is shown as the node's own text (`10000000` on air): the app stores it but shows it
    nowhere, so there is no rule to render it otherwise. The manufacturer is JUNG until the node has named itself
    (`Albrecht Jung GmbH & Co.KG` on air).
    """
    return NodeRegistryFields(
        manufacturer=node_text(hub, node, SIG_MANUFACTURER_NAME) or "JUNG",
        sw_version=software_version(hub, node),
        hw_version=node_text(hub, node, SIG_HARDWARE_REVISION),
    )


def node_device_info(hub: JungHomeHub, node: Node) -> DeviceInfo:
    """Return the device info of a node device, hanging off the mesh device.

    The model id is the JUNG product id; the software version (review-3 F1), hardware revision and manufacturer
    are the node's SIG 0x001A / 0x0010 / 0x0011 once read (`node_registry_fields`; `update_node_device` fills them
    in when they arrive later). A room thermostat's or detector's entities live on the node device itself, so it
    carries that device's app name and first room, as a load's own device does.
    """
    mac = mac_from_uuid(node.uuid)
    unit = next(
        (
            d
            for d in (*hub.devices.thermostats, *hub.devices.detectors)
            if d.node is node
        ),
        None,
    )
    fields = node_registry_fields(hub, node)
    info = DeviceInfo(
        identifiers={(DOMAIN, node_identifier(node))},
        name=unit.name if unit is not None else f"{node.name} {node.unicast:04X}",
        manufacturer=fields.manufacturer,
        model=product_name(node.pid),
        serial_number=mac,
    )
    if unit is not None and unit.rooms:
        info["suggested_area"] = unit.rooms[0]
    if node.pid is not None:
        info["model_id"] = f"0x{node.pid:04X}"
    if fields.sw_version is not None:
        info["sw_version"] = fields.sw_version
    if fields.hw_version is not None:
        info["hw_version"] = fields.hw_version
    if (via := hub.device_ids.get(mesh_identifier(hub))) is not None:
        info["via_device_id"] = via
    if mac:
        info["connections"] = {(CONNECTION_BLUETOOTH, mac)}
    return info


@callback
def update_node_device(hass: HomeAssistant, hub: JungHomeHub, node: Node) -> None:
    """Show what the node told about itself after its device was registered (`node_registry_fields`)."""
    device_id = hub.device_ids.get(node_identifier(node))
    if device_id is None:
        return
    registry = dr.async_get(hass)
    device = registry.async_get(device_id)
    if not isinstance(device, dr.DeviceEntry):  # removed meanwhile
        return
    fields = node_registry_fields(hub, node)
    known = (
        fields.sw_version or device.sw_version,
        fields.hw_version or device.hw_version,
    )
    if (device.manufacturer, device.sw_version, device.hw_version) != (
        fields.manufacturer,
        *known,
    ):
        registry.async_update_device(
            device_id,
            manufacturer=fields.manufacturer,
            sw_version=known[0],
            hw_version=known[1],
        )


def register_parent_devices(hass: HomeAssistant, hub: JungHomeHub) -> None:
    """Create the mesh + node devices up front so child devices can point at them (`via_device_id`)."""
    registry = dr.async_get(hass)
    entry_id = hub.entry.entry_id
    mesh = registry.async_get_or_create(
        config_entry_id=entry_id, **hub_device_info(hub)
    )
    hub.device_ids[mesh_identifier(hub)] = mesh.id
    for node in hub.cdb.nodes:
        if node.pid is None:
            continue
        dev = registry.async_get_or_create(
            config_entry_id=entry_id, **node_device_info(hub, node)
        )
        hub.device_ids[node_identifier(node)] = dev.id


def light_device_info(hub: JungHomeHub, light: Light) -> DeviceInfo:
    """Return the device info of a light, hanging off its node device."""
    info = DeviceInfo(
        identifiers={(DOMAIN, light.unique_id)},
        name=light.name,
        manufacturer="JUNG",
        model=LIGHT_MODELS[light.kind],
    )
    if (via := hub.device_ids.get(node_identifier(light.node))) is not None:
        info["via_device_id"] = via
    if light.rooms:
        info["suggested_area"] = light.rooms[0]
    return info


def socket_device_info(hub: JungHomeHub, socket: Socket) -> DeviceInfo:
    """Return the device info of a socket, hanging off its node device.

    The model is the product's name: a plain socket (0x0C) has no meter, only the metering one (0x03) has.
    """
    info = DeviceInfo(
        identifiers={(DOMAIN, socket.unique_id)},
        name=socket.name,
        manufacturer="JUNG",
        model=product_name(socket.node.pid),
    )
    if (via := hub.device_ids.get(node_identifier(socket.node))) is not None:
        info["via_device_id"] = via
    if socket.rooms:
        info["suggested_area"] = socket.rooms[0]
    return info


def metered_device_info(hub: JungHomeHub, load: MeteredLoad) -> DeviceInfo:
    """Return the device a metered load's meter entities show under: the socket's, or the light's (an energy puck's output)."""
    if isinstance(load, Socket):
        return socket_device_info(hub, load)
    return light_device_info(hub, load)


def blind_device_info(hub: JungHomeHub, blind: Blind) -> DeviceInfo:
    """Return the device info of a blind, hanging off its node device (the cover entity decides the device class)."""
    info = DeviceInfo(
        identifiers={(DOMAIN, blind.unique_id)},
        name=blind.name,
        manufacturer="JUNG",
        model="Blind / shutter drive",
    )
    if (via := hub.device_ids.get(node_identifier(blind.node))) is not None:
        info["via_device_id"] = via
    if blind.rooms:
        info["suggested_area"] = blind.rooms[0]
    return info


def button_gang(hub: JungHomeHub, button: Button) -> list[Button]:
    """Return the keys the app presents as one device together with `button`: same node, same app device entry."""
    return [
        b
        for b in hub.devices.buttons
        if b.node is button.node and b.gang == button.gang
    ]


def buttons_device_id(gang: list[Button]) -> str:
    """Return the device identifier of a gang of buttons: node UUID and lowest element location."""
    return f"{gang[0].node.uuid.lower()}-{min(b.location for b in gang):04x}-buttons"


def buttons_device_info(hub: JungHomeHub, gang: list[Button]) -> DeviceInfo:
    """Return the device info of a gang of buttons, hanging off its node device."""
    info = DeviceInfo(
        identifiers={(DOMAIN, buttons_device_id(gang))},
        name=gang[0].group_name,
        manufacturer="JUNG",
        model="Push-buttons",
    )
    if (via := hub.device_ids.get(node_identifier(gang[0].node))) is not None:
        info["via_device_id"] = via
    return info


def node_unit_device_info(hub: JungHomeHub, node: Node) -> DeviceInfo:
    """Return the device people look at for the node: a push-button's (first) keys, a socket, else the node itself.

    Node-level entities (Identify, the fault register) show there: a push-button's LED is in its keys, a socket is
    its own device. A node with nothing visible (a mini actuator in a junction box, the gateway) keeps them on
    the node device.
    """
    if node.pid in P.PUSH_BUTTONS:
        keys = [b for b in hub.devices.buttons if b.node is node]
        if keys:
            first = min(keys, key=lambda b: b.location)
            return buttons_device_info(hub, button_gang(hub, first))
    if node.pid in P.SOCKETS:
        for sock in hub.devices.sockets:
            if sock.node is node:
                return socket_device_info(hub, sock)
    return node_device_info(hub, node)


def health_nodes(hub: JungHomeHub) -> list[Node]:
    """Return the nodes that get the Health entities (Fault, Identify, Clear faults): every provisioned mains device.

    A battery node (`BATTERY_PIDS`) sleeps between key presses and would answer none of them.
    """
    return [n for n in hub.cdb.nodes if n.pid is not None and n.pid not in BATTERY_PIDS]


def current_device_identifiers(hub: JungHomeHub) -> set[tuple[str, str]]:
    """Every device identifier the current export produces (used to prune stale registry entries)."""
    ids: set[tuple[str, str]] = {(DOMAIN, mesh_identifier(hub))}
    for node in hub.cdb.nodes:
        if node.pid is not None:
            ids.add((DOMAIN, node_identifier(node)))
    ids.update((DOMAIN, light.unique_id) for light in hub.devices.lights)
    ids.update((DOMAIN, sock.unique_id) for sock in hub.devices.sockets)
    ids.update((DOMAIN, blind.unique_id) for blind in hub.devices.blinds)
    ids.update(
        (DOMAIN, buttons_device_id(button_gang(hub, b))) for b in hub.devices.buttons
    )
    return ids


def room_loads(hub: JungHomeHub, kind: type[Device]) -> dict[int, list[Device]]:
    """Return each room's members of one class (`Devices.room_members`); a room without one is left out."""
    rooms: dict[int, list[Device]] = {}
    for room, loads in hub.devices.room_members.items():
        if members := [d for d in loads if isinstance(d, kind)]:
            rooms[room] = members
    return rooms


# a room's central entities (`JungHomeCentralEntity` with a room): the kind in their unique id, the class it covers
ROOM_KINDS: dict[str, type[Device]] = {
    "lights": Light,
    "sockets": Socket,
    "blinds": Blind,
    "thermostats": Thermostat,
}


def room_central_prefix(hub: JungHomeHub) -> str:
    """Return the unique-id prefix every room central entity of the mesh has."""
    return f"{hub.cdb.mesh_uuid.lower()}-room-"


def room_central_id(hub: JungHomeHub, room: int, kind: str) -> str:
    """Return the unique id of the central entity for the `kind` loads of `room`."""
    return f"{room_central_prefix(hub)}{room:04x}-{kind}"


def current_room_central_ids(hub: JungHomeHub) -> set[str]:
    """Every room central entity the current export produces (used to prune those of a deleted or emptied room).

    They sit on the mesh device, which stays, so pruning devices never reaches them.
    """
    return {
        room_central_id(hub, room, kind)
        for kind, cls in ROOM_KINDS.items()
        for room in room_loads(hub, cls)
    }


# the platforms a load's own entity is on (a light, a socket's switch, a cover, a climate)
LOAD_DOMAINS = ("light", "switch", "cover", "climate")


def load_entity_id(hass: HomeAssistant, device: Device) -> str:
    """Return the entity id of a load's own entity; its mesh address when it has none (yet)."""
    registry = er.async_get(hass)
    for domain in LOAD_DOMAINS:
        if entity_id := registry.async_get_entity_id(domain, DOMAIN, device.unique_id):
            return entity_id
    return f"{device.address:04X}"


# a platform's `build_entities`: every entity the hub's device model gives the platform, the disabled ones included
type EntityBuilder = Callable[[JungHomeHub], Iterable[Entity]]


@dataclass
class TrackedPlatform:
    """A platform's entities as its builder made them, kept with the hub to follow a new export in place (`model_update`).

    `entities` holds every entity the builder produced, by unique id — the disabled ones too, which Home Assistant
    never adds — and `built` what each one's constructor set (its `vars()` right after it ran): what the entity took
    from the device model, as opposed to what it learnt since.
    """

    build: EntityBuilder
    add: AddConfigEntryEntitiesCallback
    entities: dict[str, Entity] = field(default_factory=dict)
    built: dict[str, dict[str, Any]] = field(default_factory=dict)


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


class JungHomeEntity(Entity):
    """Push-updated entity bound to one mesh element."""

    _attr_should_poll = False
    _attr_has_entity_name = True

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

    async def _send(self, command: Awaitable[None]) -> None:
        """Run a command; report a load that did not answer it, or a link that could not carry it.

        A load's command waits for its status like the app's (`JungHomeHub._load_command`): unanswered through every
        attempt, the action fails with `device_not_reachable`, and the node is marked unreachable — its entities go
        unavailable, as the app shows "No connection" — unless it was heard from meanwhile or the proxy turns out
        to be the one that stopped answering, which the message allows for. TimeoutError is an OSError: it is told
        apart first.
        """
        try:
            await command
        except TimeoutError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="device_not_reachable",
                translation_placeholders={"entity": self.entity_id},
            ) from err
        except (ConnectionError, OSError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="send_failed"
            ) from err


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

    async def _switch(self, on: bool, lightness: int | None = None) -> None:
        """Switch every member, the dimmable ones to `lightness` first when given."""
        if self.room is None:
            await self._send(self.hub.central_command(self.address, on, lightness))
        else:
            addresses = [m.address for m in self.members]
            await self._send(self.hub.room_command(self.room, addresses, on, lightness))

    async def _level(self, group: int, addresses: list[int], level: int) -> None:
        """Set a Generic Level: once to the device-type `group` home-wide, on each of `addresses` in a room."""
        if self.room is None:
            await self._send(self.hub.central_level(group, level))
        else:
            await self._send(self.hub.room_level(addresses, level))
