"""The device-registry model: the devices of a mesh, their names, rooms and areas, and the entity ids of its loads.

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

It reads the hub through `protocols.HubView` only, so the hub keeps its devices up to date without this module
importing it; `entity.py`, the entity base, re-exports every name for the platforms.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import (
    CONNECTION_BLUETOOTH,
    DeviceEntryType,
    DeviceInfo,
)

from .areas import area_name_for
from .const import (
    DOMAIN,
    NODE_INFO,
)
from .jhmesh import properties as P
from .jhmesh.advert import mac_from_uuid
from .jhmesh.devices import (
    BATTERY_PIDS,
    GATEWAY_PID,
    Blind,
    Light,
    Socket,
    Thermostat,
)
from .jhmesh.properties import (
    SIG_HARDWARE_REVISION,
    SIG_MANUFACTURER_NAME,
    SIG_SOFTWARE_VERSION,
)

if TYPE_CHECKING:
    from .jhmesh.cdb import Node
    from .jhmesh.devices import Button, Device, KeyConnection, MeteredLoad
    from .protocols import HubView


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
# The translation key of each product name, `selector.product.options.<key>` of `strings.json`:
# a device's model shows the name in Home Assistant's language, PRODUCT_NAMES (English) where no translation is at
# hand (diagnostics, a hub without labels). `tests/test_translations.py` keeps the two in step.
PRODUCT_KEYS = {
    0x01: "push_button_1",
    0x02: "push_button_2",
    0x03: "socket_metering",
    0x0C: "socket",
    0x04: "switch_actuator_mini",
    0x0D: "blinds_actuator_mini",
    0x05: "wall_transmitter_1",
    0x06: "wall_transmitter_2",
    0x07: "motion_detector_1m",
    0x08: "motion_detector_2m",
    0x09: "presence_detector",
    0x0A: "room_thermostat",
    0x0B: "gateway",
    0x10: "switch_actuator_1_energy",
    0x11: "switch_actuator_2",
    0x12: "dimmer_actuator",
    0x13: "blinds_actuator",
    0x14: "dali_actuator",
    0x15: "mini_sensor_mains",
    0x16: "mini_sensor_battery",
}
# The other device models, English, by `selector.device_model.options.<key>`; a light's kind (`Light.kind`) is its key.
MODEL_NAMES = {
    "mesh_network": "Bluetooth Mesh network",
    "switch": "Switched light",
    "dimmer": "Dimmable light",
    "ctl": "Tunable-white (DALI) light",
    "blind": "Blind / shutter drive",
    "push_buttons": "Push-buttons",
    "unknown_product": "Product {pid}",
}


def model_labels(hub: HubView) -> dict[str, str]:
    """Return the selector labels the hub loaded in Home Assistant's language (`inserts.async_load_labels`)."""
    inserts = getattr(
        hub, "inserts", None
    )  # a stand-in hub resolving devices alone has none
    return inserts.labels if inserts is not None else {}


def model_name(labels: dict[str, str], key: str) -> str:
    """Return the device model `key` (`MODEL_NAMES`) in the language of `labels`, English without a translation."""
    return labels.get(f"device_model.options.{key}", MODEL_NAMES[key])


def product_name(pid: int | None, labels: dict[str, str] | None = None) -> str:
    """Return the product name for a PID, or a generic placeholder; in the language of `labels`, else English."""
    labels = labels or {}
    if (key := PRODUCT_KEYS.get(pid or -1)) is None:
        return model_name(labels, "unknown_product").format(pid=pid)
    return labels.get(f"product.options.{key}", PRODUCT_NAMES[pid or -1])


def mesh_identifier(hub: HubView) -> str:
    """Return the device identifier of the mesh (service) device."""
    return f"mesh:{hub.cdb.mesh_uuid.lower()}"


def node_identifier(node: Node) -> str:
    """Return the device identifier of a node device."""
    return f"node:{node.uuid.lower()}"


def hub_device_info(hub: HubView) -> DeviceInfo:
    """Return the device info of the mesh (service) device."""
    return DeviceInfo(
        identifiers={(DOMAIN, mesh_identifier(hub))},
        name=hub.entry.title,
        manufacturer="JUNG",
        model=model_name(model_labels(hub), "mesh_network"),
        entry_type=DeviceEntryType.SERVICE,
    )


def software_version(hub: HubView, node: Node) -> str | None:
    """Return the node's software version (SIG 0x001A, e.g. `"2.2.0.2"`) once it has been read, else None."""
    state = hub.states.get(node.unicast)
    raw = state.properties.get(SIG_SOFTWARE_VERSION) if state else None
    if raw is None:
        return None
    try:
        return P.ASCII_VERSION.decode(raw)
    except ValueError:
        return None


def node_text(hub: HubView, node: Node, pid: int) -> str | None:
    """Return a text of the node's SIG identity (0x0010, 0x0011) once it has been read; None before, or when blank."""
    raw = hub.node_info(node.unicast).get(NODE_INFO[pid])
    return (P.SIG_PROPERTIES[pid].codec.decode(raw) or None) if raw else None


@dataclass(frozen=True)
class NodeRegistryFields:
    """What the node told about itself, as device-registry fields (None: not known, left as it is)."""

    manufacturer: str
    sw_version: str | None
    hw_version: str | None


def node_registry_fields(hub: HubView, node: Node) -> NodeRegistryFields:
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


def room_area_name(hub: HubView, room: str | None) -> str | None:
    """Return the area a device of `room` starts in, by name (its `suggested_area`): `areas.area_name_for`.

    The mapped area's name, else the room's; None without a room, or when the entry assigns no areas. A model
    looked at without Home Assistant (no `hass`: a stand-in hub resolving devices alone) suggests the room's name.
    """
    if (hass := getattr(hub, "hass", None)) is None:
        return room
    return area_name_for(hass, hub.entry.options, room)


def _suggest_area(info: DeviceInfo, hub: HubView, room: str | None) -> None:
    """Have the device start in the area of `room` (`room_area_name`), when that names one."""
    if (area := room_area_name(hub, room)) is not None:
        info["suggested_area"] = area


def first_room(device: Device | None) -> str | None:
    """Return the room a device's own device goes to: its first (a load may be in several), else None."""
    return device.rooms[0] if device is not None and device.rooms else None


def node_loads(hub: HubView, node: Node) -> list[Device]:
    """Return the node's loads (lights, sockets, blinds), by element address."""
    loads: list[Device] = [
        *hub.devices.lights,
        *hub.devices.sockets,
        *hub.devices.blinds,
    ]
    return sorted((d for d in loads if d.node is node), key=lambda d: d.address)


def node_unit(hub: HubView, node: Node) -> Device | None:
    """Return the room thermostat or detector whose entities live on the node device itself, if the node has one."""
    return next(
        (
            d
            for d in (*hub.devices.thermostats, *hub.devices.detectors)
            if d.node is node
        ),
        None,
    )


def node_gangs(hub: HubView, node: Node) -> list[list[Button]]:
    """Return the node's gangs of keys (`button_gang`), by their lowest key location."""
    gangs: dict[str, list[Button]] = {}
    for button in sorted(hub.devices.buttons, key=lambda b: b.location):
        if button.node is node:
            gang = button_gang(hub, button)
            gangs.setdefault(buttons_device_id(gang), gang)
    return list(gangs.values())


def connection_room(hub: HubView, connection: KeyConnection | None) -> str | None:
    """Return the room what a key drives is in: the room it switches, or the first room of the load it switches."""
    if connection is None:
        return None
    if connection.kind in ("room", "group"):
        return connection.name  # a room's name; None for a group that is no room
    if connection.kind in ("device", "lock") and connection.target is not None:
        return first_room(hub.devices.by_address.get(connection.target))
    return None  # a scene, the gateway


def gang_room(hub: HubView, gang: list[Button]) -> str | None:
    """Return the room a gang of keys goes to: that of a load on its node, else that of what its keys drive.

    A push-button or mini actuator sits next to its load; a node without a load of its own (a wall transmitter,
    an extension insert, a mini sensor) is where the room it switches is. Unverified on air for a wall transmitter:
    none has been seen.
    """
    node = gang[0].node
    for load in node_loads(hub, node):
        if load.rooms:
            return load.rooms[0]
    for key in sorted(gang, key=lambda b: b.location):
        if (room := connection_room(hub, key.connection)) is not None:
            return room
    return None


def node_room(hub: HubView, node: Node) -> str | None:
    """Return the room a node device goes to: that of its first unit (its thermostat or detector, a load, a gang).

    The gateway gets none: it serves the whole home.
    """
    if node.pid == GATEWAY_PID:
        return None
    if (unit := node_unit(hub, node)) is not None:
        return first_room(unit)
    for load in node_loads(hub, node):
        if load.rooms:
            return load.rooms[0]
    for gang in node_gangs(hub, node):
        if (room := gang_room(hub, gang)) is not None:
            return room
    return None


def _app_named(hub: HubView, device: Device) -> bool:
    """Whether the app named the load (`Metadata.name_for` at its element), rather than the fallback node label."""
    element = hub.cdb.element(device.address)
    assert element is not None  # a load is built from one of the CDB's elements
    return hub.devices.metadata.name_for(device.node.uuid, element.location) is not None


def node_device_name(hub: HubView, node: Node) -> str:
    """Return the name of a node device: what it carries, so the device list shows which one it is.

    A room thermostat's or detector's node device is that unit (its app name). A node with exactly one unit — its
    one load, or without a load its one gang of keys — that the app named is `"<unit name> - <product name>"`. Any
    other (two outputs, two gangs, nothing named) keeps `"<node name> <address>"`.
    """
    if (unit := node_unit(hub, node)) is not None:
        return unit.name
    loads = node_loads(hub, node)
    named = (
        [d.name for d in loads if _app_named(hub, d)]
        if loads
        else [g[0].group_name for g in node_gangs(hub, node) if g[0].gang]
    )
    units = len(loads) if loads else len(node_gangs(hub, node))
    if units == 1 and named:
        return f"{named[0]} - {product_name(node.pid, model_labels(hub))}"
    return f"{node.name} {node.unicast:04X}"


def node_device_info(hub: HubView, node: Node) -> DeviceInfo:
    """Return the device info of a node device, hanging off the mesh device.

    The model is the product and a push-button's insert once known (`inserts.NodeInserts.node_model`), the model id
    the JUNG product id; the software version, hardware revision and manufacturer
    are the node's SIG 0x001A / 0x0010 / 0x0011 once read (`node_registry_fields`; `update_node_device` fills them
    in when they arrive later). A room thermostat's or detector's entities live on the node device itself, so it
    carries that device's app name, as a load's own device does. Its name is `node_device_name`'s, its area that
    of its first unit (`node_room`).
    """
    mac = mac_from_uuid(node.uuid)
    fields = node_registry_fields(hub, node)
    info = DeviceInfo(
        identifiers={(DOMAIN, node_identifier(node))},
        name=node_device_name(hub, node),
        manufacturer=fields.manufacturer,
        model=hub.inserts.node_model(node),
        serial_number=mac,
    )
    _suggest_area(info, hub, node_room(hub, node))
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
def update_node_device(hass: HomeAssistant, hub: HubView, node: Node) -> None:
    """Show what the node told about itself after its device was registered (`node_registry_fields`, its insert)."""
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
    model = hub.inserts.node_model(node)
    if (device.manufacturer, device.sw_version, device.hw_version, device.model) != (
        fields.manufacturer,
        *known,
        model,
    ):
        registry.async_update_device(
            device_id,
            manufacturer=fields.manufacturer,
            sw_version=known[0],
            hw_version=known[1],
            model=model,
        )


@callback
def update_buttons_devices(hass: HomeAssistant, hub: HubView, node: Node) -> None:
    """Show the node's key layout on its buttons devices once it is known (`inserts.NodeInserts.buttons_model`)."""
    registry = dr.async_get(hass)
    model = hub.inserts.buttons_model(node)
    for gang in {
        buttons_device_id(button_gang(hub, b))
        for b in hub.devices.buttons
        if b.node is node
    }:
        device = registry.async_get_device_by_identifier(
            (DOMAIN, gang), hub.entry.entry_id
        )
        if device is not None and device.model != model:
            registry.async_update_device(device.id, model=model)


def register_parent_devices(hass: HomeAssistant, hub: HubView) -> None:
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


def light_device_info(hub: HubView, light: Light) -> DeviceInfo:
    """Return the device info of a light, hanging off its node device."""
    info = DeviceInfo(
        identifiers={(DOMAIN, light.unique_id)},
        name=light.name,
        manufacturer="JUNG",
        model=model_name(model_labels(hub), light.kind),
    )
    if (via := hub.device_ids.get(node_identifier(light.node))) is not None:
        info["via_device_id"] = via
    _suggest_area(info, hub, first_room(light))
    return info


def socket_device_info(hub: HubView, socket: Socket) -> DeviceInfo:
    """Return the device info of a socket, hanging off its node device.

    The model is the product's name: a plain socket (0x0C) has no meter, only the metering one (0x03) has.
    """
    info = DeviceInfo(
        identifiers={(DOMAIN, socket.unique_id)},
        name=socket.name,
        manufacturer="JUNG",
        model=product_name(socket.node.pid, model_labels(hub)),
    )
    if (via := hub.device_ids.get(node_identifier(socket.node))) is not None:
        info["via_device_id"] = via
    _suggest_area(info, hub, first_room(socket))
    return info


def metered_device_info(hub: HubView, load: MeteredLoad) -> DeviceInfo:
    """Return the device a metered load's meter entities show under: the socket's, or the light's (an energy puck's output)."""
    if isinstance(load, Socket):
        return socket_device_info(hub, load)
    return light_device_info(hub, load)


def blind_device_info(hub: HubView, blind: Blind) -> DeviceInfo:
    """Return the device info of a blind, hanging off its node device (the cover entity decides the device class)."""
    info = DeviceInfo(
        identifiers={(DOMAIN, blind.unique_id)},
        name=blind.name,
        manufacturer="JUNG",
        model=model_name(model_labels(hub), "blind"),
    )
    if (via := hub.device_ids.get(node_identifier(blind.node))) is not None:
        info["via_device_id"] = via
    _suggest_area(info, hub, first_room(blind))
    return info


def button_gang(hub: HubView, button: Button) -> list[Button]:
    """Return the keys the app presents as one device together with `button`: same node, same app device entry."""
    return [
        b
        for b in hub.devices.buttons
        if b.node is button.node and b.gang == button.gang
    ]


def buttons_device_id(gang: list[Button]) -> str:
    """Return the device identifier of a gang of buttons: node UUID and lowest element location."""
    return f"{gang[0].node.uuid.lower()}-{min(b.location for b in gang):04x}-buttons"


def buttons_device_info(hub: HubView, gang: list[Button]) -> DeviceInfo:
    """Return the device info of a gang of buttons, hanging off its node device, in its room's area (`gang_room`)."""
    info = DeviceInfo(
        identifiers={(DOMAIN, buttons_device_id(gang))},
        name=gang[0].group_name,
        manufacturer="JUNG",
        model=hub.inserts.buttons_model(gang[0].node),
    )
    if (via := hub.device_ids.get(node_identifier(gang[0].node))) is not None:
        info["via_device_id"] = via
    _suggest_area(info, hub, gang_room(hub, gang))
    return info


def node_unit_device_info(hub: HubView, node: Node) -> DeviceInfo:
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


def node_device(
    hub: HubView, registry: dr.DeviceRegistry, node: Node
) -> dr.DeviceEntry | None:
    """Return the node's device registry entry; None while it is not registered."""
    device = registry.async_get(hub.device_ids.get(node_identifier(node), ""))
    return device if isinstance(device, dr.DeviceEntry) else None


def node_label(hub: HubView, registry: dr.DeviceRegistry, node: Node) -> str:
    """Return the name the node's device shows: the user's rename, else the integration's name for it."""
    device = node_device(hub, registry, node)
    names = (device.name_by_user, device.name) if device is not None else ()
    return next((name for name in names if name), f"{node.name} {node.unicast:04X}")


def node_areas(hub: HubView) -> dict[int, str]:
    """Return the area of each node with one, by unicast: its node device's, else the first among its children's.

    A light's, a socket's device: the room the app put the load in, when the node device itself has no area.
    """
    registry = dr.async_get(hub.hass)
    areas = ar.async_get(hub.hass)
    child_area: dict[str, str] = {}
    for device in dr.async_entries_for_config_entry(registry, hub.entry.entry_id):
        if device.area_id is not None and device.via_device_id is not None:
            child_area.setdefault(device.via_device_id, device.area_id)
    out: dict[int, str] = {}
    for node in hub.cdb.nodes:
        own = node_device(hub, registry, node)
        area_id = (own.area_id or child_area.get(own.id)) if own is not None else None
        area = areas.async_get_area(area_id) if area_id is not None else None
        if area is not None:
            out[node.unicast] = area.name
    return out


def health_nodes(hub: HubView) -> list[Node]:
    """Return the nodes that get the Health entities (Fault, Identify, Clear faults): every provisioned mains device.

    A battery node (`BATTERY_PIDS`) sleeps between key presses and would answer none of them.
    """
    return [n for n in hub.cdb.nodes if n.pid is not None and n.pid not in BATTERY_PIDS]


def current_device_identifiers(hub: HubView) -> set[tuple[str, str]]:
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


def device_rooms(hub: HubView) -> dict[str, str | None]:
    """Return the room each device of the current export goes to, by device identifier (the mesh device has none).

    What `areas.async_move_devices` compares: the same rooms the device infos suggest areas for.
    """
    rooms: dict[str, str | None] = {}
    for node in hub.cdb.nodes:
        if node.pid is not None:
            rooms[node_identifier(node)] = node_room(hub, node)
    for load in [*hub.devices.lights, *hub.devices.sockets, *hub.devices.blinds]:
        rooms[load.unique_id] = first_room(load)
    for button in hub.devices.buttons:
        gang = button_gang(hub, button)
        rooms[buttons_device_id(gang)] = gang_room(hub, gang)
    return rooms


def room_loads(hub: HubView, kind: type[Device]) -> dict[int, list[Device]]:
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


def room_central_prefix(hub: HubView) -> str:
    """Return the unique-id prefix every room central entity of the mesh has."""
    return f"{hub.cdb.mesh_uuid.lower()}-room-"


def room_central_id(hub: HubView, room: int, kind: str) -> str:
    """Return the unique id of the central entity for the `kind` loads of `room`."""
    return f"{room_central_prefix(hub)}{room:04x}-{kind}"


def current_room_central_ids(hub: HubView) -> set[str]:
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
