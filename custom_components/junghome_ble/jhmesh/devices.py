"""Device model: turn the CDB (+ the app's device_metadata / scene_metadata caches) into controllable devices.

The derivation is a rule table, `ELEMENT_RULES`: every element of every product node is offered to the rules in
order and the first `ElementRule` whose predicate accepts it builds the device; elements no rule claims (the
gateway's primary element, a CTL dimmer's temperature element, vendor-only elements) produce nothing. A new kind
of device (blinds on a `1002` level element, a room thermostat, a detector, a mini input) is one added rule
building one `Device` subclass (placed before the load rules when its elements would pass for loads, as the
thermostat's does).

The built-in rules come from docs/android/network-logic.md and docs/ios-app-data.md:
- element location 0x0001/0x0002 = load outputs; a CTL dimmer's second element (Generic Level + CTL Temperature
  Server, also location 0x0001) is not a device of its own;
- a blind (docs/gap-analysis/control-and-state.md §2.6) is the first load element of a blinds-capable product that
  hosts a Generic Level server without a lamp's OnOff / Lightness / CTL Temperature server next to it; the node's
  last other such element is its slat element;
- elements with location >= 0x0040 are buttons / binary inputs (OnOff client), or a socket's power sensor
  (Sensor Server); on a detector product (`DETECTOR_PIDS`) the Sensor Server element is the detector itself;
- a node's meter (`meter_element`) is its Sensor Server element at a key location on any product but a detector
  or an RTR: the load on its primary element is a *metered load* (`MeteredLoad`: the metering socket 0x0003, the
  energy puck 0x0010's output), whose meter readings and energy counters the integration shows on that load;
- a push-button takes any insert, so where the app cached its InsertId (`insert_function`) that decides: a blinds
  insert is a blind whatever lamp servers sit next to it, a lamp insert never a blind, an extension no load;
- an RTR is a thermostat on every element, never a light next to it;
- the element's OnOff/Lightness server subscriptions name its rooms. Element groups (`element group #0x…` in iOS
  exports, `element group #…` decimal in Android ones) and device-type groups are not rooms.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .cdb import canonical_uuid, is_virtual
from .properties import key_lock

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .cdb import CDB, Element, Node
    from .properties import EnforcedOutput

SOCKET_PIDS = {0x0003, 0x000C}
DETECTOR_PIDS = frozenset(
    {0x0007, 0x0008, 0x0009}
)  # motion 1 m / 2 m, ceiling presence
PRESENCE_DETECTOR_PID = 0x0009
# Products running on a battery (`docs/gap-analysis/control-and-state.md` §2.9): wall transmitters 1-/2-gang and the
# battery binary-input puck. They sleep, so a Generic Battery Get only gets through right after they sent something.
BATTERY_PIDS = frozenset({0x0005, 0x0006, 0x0016})
# Nodes that drive a blind: push-buttons with a blinds insert (0x01 / 0x02), the blinds actuator mini (0x0D) and
# the blinds PP2 puck (0x13); the two minis can be nothing else (docs/cross-repo-analysis.md §1.4).
BLIND_PIDS = {0x0001, 0x0002, 0x000D, 0x0013}
BLIND_ONLY_PIDS = {0x000D, 0x0013}
PUSH_BUTTON_PIDS = frozenset({0x0001, 0x0002})
# InsertId actuator functions (`properties.ACTUATOR_FUNCTION`) that settle what a push-button drives. It takes any
# insert and its composition does not say which (a blinds insert may sit next to lamp servers), but the app caches
# the node's InsertId (`Node.insert_function`); without one the composition rules below decide.
# switch, 2-gang switch, dimming, 2-gang dimming, tunable white
LAMP_FUNCTIONS = frozenset({0, 1, 2, 3, 4})
BLIND_FUNCTION = 5
EXTENSION_FUNCTION = 6
NO_LOAD_FUNCTIONS = frozenset({6, 7})  # extension (a satellite insert), not available
INSERT_FUNCTIONS = LAMP_FUNCTIONS | {BLIND_FUNCTION} | NO_LOAD_FUNCTIONS
TWO_GANG_FUNCTIONS = frozenset({1, 3})  # 2-gang switch, 2-gang dimming: two loads
# Products that are one app device whatever their function (`CheckForMissingDevices`): RTR, sockets, gateway, mini
# sensors, wall transmitters
SINGLE_DEVICE_PIDS = frozenset(
    {0x000A, 0x0003, 0x000C, 0x000B, 0x0015, 0x0016, 0x0006, 0x0005}
)
# ButtonLayout (0x5001, `properties.BUTTON_LAYOUT`) → key element location → where that key sits (`key_position`)
KEY_POSITIONS: dict[int, dict[int, str]] = {
    0: {0x40: "top", 0x41: "bottom"},  # one key top, one bottom
    1: {0x40: "rocker"},  # one rocker
    2: {0x40: "left_top", 0x41: "left_bottom", 0x42: "right_top", 0x43: "right_bottom"},
    3: {0x40: "left_rocker", 0x42: "right_top", 0x43: "right_bottom"},
    4: {0x40: "left_top", 0x41: "left_bottom", 0x42: "right_rocker"},
    5: {0x40: "left_rocker", 0x42: "right_rocker"},  # rocker | rocker
}
# products whose ButtonLayout says which keys and rockers their key elements are: push-buttons, wall transmitters
# (a mini actuator's layout only tells whether its two inputs act as one)
KEY_LAYOUT_PIDS = frozenset({0x0001, 0x0002, 0x0005, 0x0006})
# Servers that make a Generic Level element part of a lamp rather than a blind: a dimmer's own level server sits
# next to its OnOff / Lightness servers, a CTL lamp's temperature element next to a CTL Temperature server.
LAMP_LEVEL_MODELS = {"1000", "1300", "1306"}
# a tunable-white light's Light CTL Server and the Light CTL Temperature Server on its temperature element
CTL_SERVER, CTL_TEMPERATURE_SERVER = "1303", "1306"
THERMOSTAT_PIDS = {0x000A}  # room thermostat (RTR)
BUTTON_LETTERS = {0x40: "A", 0x41: "B", 0x42: "C", 0x43: "D"}
# Mini actuators and pucks (`docs/android/firmware-products.md`): their key-location elements are binary inputs,
# which the app names E1 / E2 (`docs/gap-analysis/device-settings.md` §6.2) where a push-button has keys A..D.
MINI_ACTUATOR_PIDS = frozenset(
    {0x0004, 0x000D, 0x0010, 0x0011, 0x0012, 0x0013, 0x0014, 0x0015, 0x0016}
)
INPUT_NAMES = {0x40: "E1", 0x41: "E2"}
LOAD_LOCATIONS = {0x0001, 0x0002}
# Group addresses and names that are never rooms (`is_room`): the fixed device-type groups and the time-keeper
# group (0xFEF5..0xFEFF), the app's per-element groups and the device-type groups by name.
GROUP_RANGE = (0xC000, 0xFEFF)
# The app's central functions ("all luminaires / blinds / slats / sockets / RTRs"): device-type groups the app
# subscribes every device of the kind to at provisioning (`network-logic.md` §1.3, §4.4) — lamps and sockets on their
# first element, blinds on their position element (slats on the slat element), RTRs on their set-point element
ALL_LIGHTS, ALL_BLINDS, ALL_SLATS, ALL_SOCKETS, ALL_THERMOSTATS = (
    0xFEF5,
    0xFEF6,
    0xFEF7,
    0xFEF8,
    0xFEF9,
)
CENTRAL_SERVERS = (
    "1000",
    "1002",
    "1300",
)  # Generic OnOff, Generic Level and Light Lightness servers
DEVICE_TYPE_GROUPS = (
    0xFEF5,
    0xFEFF,
)  # fixed device-type groups + the time-keeper group, never rooms
ELEMENT_GROUP_PREFIX = "element group #"
DEVICE_TYPE_GROUP_PREFIX = "device type group"
TIME_KEEPER_GROUP = "#time_keeper_group#"


def is_room(address: int, name: str) -> bool:
    """User groups are everything but element groups, device-type groups, the time-keeper group and virtual groups.

    A virtual group (a Label UUID in `groups[]`) is parsed but cannot be addressed (`cdb` module docstring), so
    the room operations never offer it. The one definition of a room: the device model, the export and the room
    services all ask this.
    """
    return (
        address < DEVICE_TYPE_GROUPS[0]
        and not is_virtual(address)
        and not name.startswith((ELEMENT_GROUP_PREFIX, DEVICE_TYPE_GROUP_PREFIX))
        and name != TIME_KEEPER_GROUP
    )


KEY_LOCATION = 0x40  # keys, binary inputs and sensor elements start here
GATEWAY_PID = 0x000B


def element_group_address(name: str) -> int | None:
    """Element address named by an `element group #0x148` (iOS, hex) / `element group #328` (Android) group."""
    if not name.startswith(ELEMENT_GROUP_PREFIX):
        return None
    rest = name[len(ELEMENT_GROUP_PREFIX) :]
    try:
        return int(rest, 16) if rest.lower().startswith("0x") else int(rest)
    except ValueError:
        return None


def as_int(value: Any) -> int | None:
    """Return an address or code of a `meta` row as an int (an int, or a hex string); None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16)
        except ValueError:
            return None
    return None


class InvalidMetadata(ValueError):
    """An app-container metadata file is not what the app writes; `path` names it — the export is not to blame.

    A `ValueError` like `cdb.InvalidExport`, so every caller that catches load errors already catches it; the
    message starts with the metadata file's path so a "cannot load <export>" report still points at the file.
    """

    def __init__(self, path: Path, problem: str) -> None:
        """Record the file and what is wrong with it (never its content: it holds the user's device names)."""
        super().__init__(f"{path}: {problem}")
        self.path = path


def meta_list(value: Any) -> list[Any]:
    """Read a list-valued `meta` field as the apps write it: `null` (seen for `cachedGroupConnectionMetadata`) is empty.

    Anything that is not a list reads as empty too — the loaders must never crash on the shape of a cosmetic block.
    """
    return value if isinstance(value, list) else []


def _load_pairs(path: Path | None) -> list[tuple[Any, Any]]:
    """Read the Swift-encoded `[key, value, key, value, …]` list of an app-container metadata file; absent = empty."""
    if not path or not Path(path).exists():
        return []
    path = Path(path)
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError) as err:  # unreadable, not UTF-8, not JSON
        raise InvalidMetadata(path, "cannot be read as JSON") from err
    except RecursionError as err:  # nested deeper than the JSON decoder recurses
        raise InvalidMetadata(path, "is nested too deeply") from err
    if not isinstance(raw, list) or len(raw) % 2:
        raise InvalidMetadata(path, "is not a list of key/value pairs")
    return list(zip(raw[::2], raw[1::2], strict=True))


def _device_pair(path: Path, key: Any, val: Any) -> tuple[str, list[int], str]:
    """One `device_metadata.json` pair: (`nodeId`, sorted `locationIds`, `name`), or the file's own error."""
    if not isinstance(key, dict) or not isinstance(val, dict):
        raise InvalidMetadata(path, "has a device entry that is not a key/value pair")
    node_id, locations, name = (
        key.get("nodeId"),
        key.get("locationIds"),
        val.get("name"),
    )
    if (
        not isinstance(node_id, str)
        or not isinstance(name, str)
        or not isinstance(locations, list)
        or not all(isinstance(x, int) and not isinstance(x, bool) for x in locations)
    ):
        raise InvalidMetadata(
            path, "has a device entry without a nodeId, locationIds or name"
        )
    return canonical_uuid(node_id), sorted(locations), name


def _scene_pair(path: Path, key: Any, val: Any) -> tuple[int, str]:
    """One `scene_metadata.json` pair: (scene number, `name`), or the file's own error."""
    if (
        isinstance(key, bool)
        or not isinstance(key, int)
        or not isinstance(val, dict)
        or not isinstance(val.get("name"), str)
    ):
        raise InvalidMetadata(path, "has a scene entry without a number or name")
    return key, val["name"]


class Metadata:
    """Names the user gave things in the app.

    Sources: the iOS app container's `device_metadata.json` / `scene_metadata.json` (Swift-encoded
    `[key, value, ...]`), or the `meta` block of the app's share export (`Metadata.from_export`).
    Node ids are kept in `canonical_uuid` form, so a dashed id matches an undashed one.
    """

    def __init__(
        self, device_metadata: Path | None = None, scene_metadata: Path | None = None
    ) -> None:
        """Load the app's device and scene metadata files; either may be missing.

        A file that is present but not in the app's shape raises `InvalidMetadata` naming that file.
        """
        self.devices: dict[str, list[tuple[list[int], str]]] = {}
        # node id -> the app's cached InsertId actuator function (`Node.insert_function`, which a share export's
        # `meta` sets on its own); a load-side entry wins over a key-side one
        self.insert_functions: dict[str, int] = {}
        for key, val in _load_pairs(device_metadata):
            node_id, locations, name = _device_pair(
                Path(device_metadata or ""), key, val
            )
            self.devices.setdefault(node_id, []).append((locations, name))
            actuator = key.get("actuatorFunction")
            function = (
                actuator.get("actuatorFunctionId")
                if isinstance(actuator, dict)
                else None
            )
            if isinstance(function, bool) or not isinstance(function, int):
                continue
            if LOAD_LOCATIONS & set(locations) or node_id not in self.insert_functions:
                self.insert_functions[node_id] = function
        self.scenes: dict[int, str] = dict(
            _scene_pair(Path(scene_metadata or ""), key, val)
            for key, val in _load_pairs(scene_metadata)
        )
        # key element -> the room its room link drives (`cachedGroupConnectionMetadata`), and -> the scene a key in
        # scene mode recalls (`keyModeSceneConfigExports`, share exports only): what the key's publications alone
        # cannot tell (`KeyConnection`)
        self.room_links: dict[int, int] = {}
        self.key_scenes: dict[int, int] = {}
        for _, val in _load_pairs(device_metadata):
            self._add_room_links(val.get("cachedGroupConnectionMetadata"))

    def _add_room_links(self, rows: Any) -> None:
        for row in meta_list(rows):
            if not isinstance(row, dict):
                continue
            key, room = (
                as_int(row.get("elementAddress")),
                as_int(row.get("groupAddress")),
            )
            if key is not None and room is not None:
                self.room_links.setdefault(key, room)

    @classmethod
    def from_export(cls, meta: dict[str, Any] | None) -> Metadata:
        """Build from the `meta` block of a share export: devices[{name, deviceId{nodeId, locationIds}}], scenes[{name, number}].

        Also the room links of `devices[].cachedGroupConnectionMetadata` and the scene keys of
        `keyModeSceneConfigExports`.

        Best effort: a `null` list, a non-object entry or an entry without the fields is skipped, never an error
        (the names are cosmetic; the file loads in the app).
        """
        m = cls()
        for dev in meta_list((meta or {}).get("devices")):
            did = dev.get("deviceId") if isinstance(dev, dict) else None
            if (
                not isinstance(did, dict)
                or not did.get("nodeId")
                or not dev.get("name")
            ):
                continue
            try:
                locations = sorted(int(x) for x in meta_list(did.get("locationIds")))
            except (ValueError, TypeError):
                continue
            m.devices.setdefault(canonical_uuid(str(did["nodeId"])), []).append(
                (locations, dev["name"])
            )
        for dev in meta_list((meta or {}).get("devices")):
            if isinstance(dev, dict):
                m._add_room_links(dev.get("cachedGroupConnectionMetadata"))
        for row in meta_list((meta or {}).get("keyModeSceneConfigExports")):
            config = row.get("sceneConfig") if isinstance(row, dict) else None
            key = as_int(row.get("elementAddress")) if isinstance(row, dict) else None
            scene = as_int(config.get("sceneId")) if isinstance(config, dict) else None
            if key is not None and scene is not None:
                m.key_scenes.setdefault(key, scene)
        for sc in meta_list((meta or {}).get("scenes")):
            try:
                m.scenes[int(sc["number"])] = sc["name"]
            except (KeyError, ValueError, TypeError):
                continue
        return m

    def entry_for(self, node_uuid: str, location: int) -> tuple[list[int], str] | None:
        """Return the most specific app device covering this element location: (locations, name)."""
        best = None
        for locs, name in self.devices.get(canonical_uuid(node_uuid), []):
            if location in locs and (best is None or len(locs) < len(best[0])):
                best = (locs, name)
        return best

    def name_for(self, node_uuid: str, location: int) -> str | None:
        """Return the app's name for the device covering this element location, if any."""
        best = self.entry_for(node_uuid, location)
        return best[1] if best else None


# ----------------------------------------------------------------------------- devices


@dataclass
class Device:
    """What every derived device shares; `kind` is the flavour the platforms switch on.

    Subclasses keep their own fields positional; `kind` (a per-class default where there is one) and `rooms` are
    keyword-only so the constructors read the same for every kind.
    """

    address: int
    unique_id: str
    name: str
    node: Node
    kind: str = field(kw_only=True)
    rooms: list[str] = field(default_factory=list, kw_only=True)


@dataclass
class Light(Device):
    """A load output we can switch or dim; `kind` is 'switch', 'dimmer' or 'ctl'.

    `temperature_address` is a CTL light's temperature element (`ctl_temperature_element`), where a colour
    temperature can be set on its own; None for the other kinds and for a CTL light without one.
    `meter_address` is the node's meter (`meter_element`) when this light is the load it measures: the energy
    puck's (0x0010) output, which the app makes a `MeasureLampDevice` with the socket's consumption page
    (`docs/gap-analysis/device-settings.md` §6); None for every other light. Unverified on air: no puck was seen.
    """

    kind: str  # 'switch' | 'dimmer' | 'ctl'
    temperature_address: int | None = field(default=None, kw_only=True)
    meter_address: int | None = field(default=None, kw_only=True)


@dataclass
class Socket(Device):
    """A switched socket, with the element address of its meter (`meter_element`) when it has one (0x0003 only)."""

    meter_address: int | None
    kind: str = field(default="socket", kw_only=True)


# A load whose node has a meter: a socket or light with a `meter_address` (`Devices.metered`).
type MeteredLoad = Light | Socket


@dataclass
class Blind(Device):
    """A blind, roller shutter or awning drive: its position element, plus the slat element when the node has one.

    `address` is the position element, the node's first Generic Level server outside a lamp (`docs/gap-analysis/
    control-and-state.md` §2.6: the app's "blind element"); `slat_address` is the last other such element of the
    node (the app's "slat element", meaningful in operation mode 0 BLINDS only), None when there is none. Built
    from the firmware / app analysis only: no blinds node has been seen on air yet (`docs/ha-integration.md`).
    """

    slat_address: int | None
    kind: str = field(default="blind", kw_only=True)

    @property
    def level_elements(self) -> tuple[int, ...]:
        """The element addresses that answer a Generic Level Get: position first, then the slats when present."""
        if self.slat_address is None:
            return (self.address,)
        return (self.address, self.slat_address)


ConnectionKind = Literal["device", "room", "scene", "gateway", "group", "lock"]
KEY_CLIENT_MODELS = (
    "1001",
    "1003",
    "05271015",
)  # OnOff / Level / JUNG vendor clients: what a key publishes from
PROPERTY_CLIENT = "05271015"  # the LBC User Property client: the only one a key in KeyMode *property* (3) publishes from
SCENE_CLIENT = "1205"
ALL_SCENES = 0xFFFF  # a scene key's Scene Client publishes its recalls here (`network-logic.md` §4.3)


@dataclass(frozen=True)
class KeyConnection:
    """What a key drives, read from its publications the way `network-logic.md` §2.7 says the app does.

    `address` is where the key publishes. `device`: a load's element group — `target` is the load element;
    `room`: the key's own element group, which the room's loads subscribe to — `target` the room group, from the
    app's room-link record (None when the export has none); `scene`: the Scene Client publishes to 0xFFFF —
    `scene` the number when the export records it; `gateway`: the gateway's element group (key events for the
    gateway, and for Home Assistant); `group`: any other group, a room group included (not what the app wires).
    `name` is the load's, room's or scene's name when known.

    `property_mode`: a device or room link published from the LBC User Property client alone, though the key hosts an
    OnOff or Level client too — KeyMode *property* (3), whose meaning sits in the key's 0x5006 to 0x5008, not in the
    export. Once those are known (`with_key_lock`), a lock-function link (`SetLockingFunctionConnection`) is
    `lock`: `target` / `name` still name what it locks, `lock` is the lock its up half sets. Unverified on air.
    """

    kind: ConnectionKind
    address: int
    target: int | None = None
    name: str | None = None
    scene: int | None = None
    property_mode: bool = False
    lock: EnforcedOutput | None = None


@dataclass
class Button(Device):
    """One key of a push-button node (element location 0x40..0x43), or a binary input of a mini actuator.

    `gang` is the location set of the app device the key belongs to (`Metadata.entry_for`): the keys of one node
    sharing it are presented as one device. Without an app name it is empty, and all such keys of a node form one
    gang. `input`: the element is a mini actuator's binary input (`MINI_ACTUATOR_PIDS`), named E1 / E2 as in the
    app rather than by a key letter.
    """

    location: int
    key: str  # "A".."D" (element location 0x40..0x43); "E1" / "E2" for a mini actuator's inputs
    group_name: str  # the gang's app name, e.g. "Bed L button"
    key_mode: int | None = None
    gang: tuple[int, ...] = ()
    battery: bool = False  # the node runs on a battery (`BATTERY_PIDS`): it sleeps between key presses
    input: bool = (
        False  # a mini actuator's binary input (E1 / E2), not a push-button key
    )
    connection: KeyConnection | None = (
        None  # what it drives; None = nothing (`key_connection`)
    )
    kind: str = field(default="button", kw_only=True)


@dataclass
class Detector(Device):
    """The sensor element of a motion / presence detector: `1001` OnOff client + `1100` Sensor Server at a key location.

    The detector switches its load itself, like a rocker: the OnOff client publishes to `target` (the element group
    of its own relay output, `relay_address`, or a room group), which is a free "motion" signal on the air. The
    Sensor Server carries Presence Detected (0x004D) and Present Illuminance (0x0055). `presence` tells the ceiling
    presence detector (product 0x0009) from the wall motion detectors (0x0007 / 0x0008).
    """

    location: int
    relay_address: int | None
    target: int | None
    presence: bool
    kind: str = field(default="detector", kw_only=True)


@dataclass
class Thermostat(Device):
    """A room thermostat (RTR, PID 0x000A): `address` is its set-point element (the node's first Generic Level server).

    The RTR's heating demand is the state of its own Generic OnOff server (`docs/cross-repo-analysis.md` D9) and its
    room temperature comes from its Sensor server (`0x004F`); the composition of a real RTR is not captured yet, so
    both are resolved per node (first element hosting the model) and may be None when the export lists neither.
    The vendor properties (preset temperatures, modes) live on the primary element like every node's.
    """

    onoff_address: int | None  # element with the Generic OnOff server: heating demand
    sensor_address: int | None  # element with the Sensor server: room temperature
    kind: str = field(default="thermostat", kw_only=True)


# The name the app gives the scene a SIG timer recalls (`"TimerScene <index> <deviceId>"`,
# `MeshSceneRepository.java:197`); its scene lists leave such scenes out (`meshnetwork/C1873e.java:93`).
TIMER_SCENE_PREFIX = "TimerScene"


@dataclass
class SceneDef:
    """A scene number with the name the app gave it; `timer` for a scene the app made for a SIG timer."""

    number: int
    name: str
    timer: bool = False


@dataclass
class Devices:
    """Everything `build_devices` derived from a CDB, plus the room names by group address and the app metadata."""

    lights: list[Light] = field(default_factory=list)
    sockets: list[Socket] = field(default_factory=list)
    blinds: list[Blind] = field(default_factory=list)
    buttons: list[Button] = field(default_factory=list)
    detectors: list[Detector] = field(default_factory=list)
    thermostats: list[Thermostat] = field(default_factory=list)
    scenes: list[SceneDef] = field(default_factory=list)
    rooms: dict[int, str] = field(default_factory=dict)
    by_address: dict[int, Device] = field(default_factory=dict)
    metadata: Metadata = field(default_factory=Metadata)
    # device-type group (ALL_LIGHTS / ALL_SOCKETS) -> the loads that listen to it; a group nobody listens to is absent
    central: dict[int, list[Device]] = field(default_factory=dict)
    # room address -> the lights, sockets, blinds and thermostats that listen to it (the app's area filter, for the
    # area's central control); a room without such a load is absent
    room_members: dict[int, list[Device]] = field(default_factory=dict)
    # meter element → its load (`by_meter`), and temperature element → its CTL light (`by_temperature`): both looked
    # up per status (review-4 R4-9), so kept up by `add` rather than searched
    _by_meter: dict[int, MeteredLoad] = field(default_factory=dict, repr=False)
    _by_temperature: dict[int, Light] = field(default_factory=dict, repr=False)

    def add(self, device: Device) -> None:
        """Record `device` under its element address and in the typed list of its class, if it has one."""
        self.by_address[device.address] = device
        if isinstance(device, Light):
            self.lights.append(device)
            if device.meter_address is not None:
                self._by_meter.setdefault(device.meter_address, device)
            if device.temperature_address is not None:
                self._by_temperature.setdefault(device.temperature_address, device)
        elif isinstance(device, Socket):
            self.sockets.append(device)
            if device.meter_address is not None and not isinstance(
                self._by_meter.get(device.meter_address), Socket
            ):
                self._by_meter[device.meter_address] = device  # a socket before a light
        elif isinstance(device, Blind):
            self.blinds.append(device)
        elif isinstance(device, Button):
            self.buttons.append(device)
        elif isinstance(device, Detector):
            self.detectors.append(device)
        elif isinstance(device, Thermostat):
            self.thermostats.append(device)

    def kinds(self) -> set[str]:
        """Return the kinds of device the export produced."""
        return {d.kind for d in self.by_address.values()}

    @property
    def metered(self) -> list[MeteredLoad]:
        """Return the loads with a meter element: the sockets first, then the lights (export order within each)."""
        loads: list[MeteredLoad] = [*self.sockets, *self.lights]
        return [load for load in loads if load.meter_address is not None]

    def by_meter(self, addr: int) -> MeteredLoad | None:
        """Return the load whose meter element is `addr`: the first in `metered` order, as a search of it would."""
        return self._by_meter.get(addr)

    def by_temperature(self, addr: int) -> Light | None:
        """Return the first CTL light whose temperature element is `addr` (`Light.temperature_address`)."""
        return self._by_temperature.get(addr)


# ----------------------------------------------------------------------------- rule table


@dataclass
class BuildContext:
    """What a rule's factory may draw on: the CDB, the app metadata and the derived-name helpers."""

    cdb: CDB
    meta: Metadata

    def unique_id(self, node: Node, element: Element) -> str:
        """Return the stable id of an element: node UUID and element location (metadata-independent)."""
        return f"{node.uuid.lower()}-{element.location:04x}"

    def node_label(self, node: Node) -> str:
        """Return the fallback name of a node the app did not name: product name and address."""
        return f"{node.name} {node.unicast:04X}"

    def rooms_of(
        self, element: Element, models: tuple[str, ...] = ("1000", "1300")
    ) -> list[str]:
        """Room names from the element's server subscriptions (element, device-type and virtual groups skipped).

        The OnOff / Lightness servers carry a lamp's rooms; a blind's are on its Generic Level server (`"1002"`).
        A virtual group is parsed but not routed (`cdb` module docstring), so it is never a room.
        """
        subs = {a for model in models for a in element.subscriptions(model)}
        return [
            name
            for a in sorted(subs)
            if (name := self.cdb.groups.get(a, "")) and is_room(a, name)
        ]

    @staticmethod
    def meter_address(node: Node, element: Element) -> int | None:
        """Return the address of the node's meter when `element` is the load it measures (`meter_element`), else None."""
        meter = meter_element(node)
        return (
            meter.address if meter is not None and element is node.elements[0] else None
        )

    @staticmethod
    def publish_address(element: Element, model_id: str) -> int | None:
        """Return the address the `model_id` model on this element publishes to; None when unset, unassigned or virtual.

        A virtual target (a Label UUID) is None on purpose: nothing here can address it (`cdb` module docstring),
        so a detector publishing to one has no usable motion signal. `ProjectFile.publication` still reports it.
        """
        for m in element.raw_models:
            if m["modelId"] == model_id:
                text = str((m.get("publish") or {}).get("address", "0000"))
                if len(text) != 4:
                    return None  # a virtual label UUID, or nothing
                try:
                    return int(text, 16) or None
                except ValueError:
                    return None
        return None


type ElementPredicate = Callable[[Node, Element, int], bool]
type DeviceFactory = Callable[[BuildContext, Node, Element], Device]


@dataclass(frozen=True)
class ElementRule:
    """One row of the derivation table: `matches(node, element, pid)` claims an element, `build` makes its device."""

    kind: str
    matches: ElementPredicate
    build: DeviceFactory


def _insert(function: int | None) -> int | None:
    """Return `function` when it is an insert a push-button can carry (`INSERT_FUNCTIONS`), else None."""
    return function if function in INSERT_FUNCTIONS else None


def insert_function(node: Node) -> int | None:
    """Return the push-button's known insert (`LAMP_FUNCTIONS`, `BLIND_FUNCTION`, `NO_LOAD_FUNCTIONS`), else None.

    The export's cached InsertId first (`Node.insert_function`); where it has none, or one the app writes when it
    has not read the InsertId (0xFFFF unset) or that makes no sense on a push-button, what the node itself
    reported (`Node.reported_function`: its advertisement, else an InsertId Get). None for every other product
    (their load is fixed; the composition rules know it).
    """
    return pick_insert(node.pid, node.insert_function, node.reported_function)


def pick_insert(
    pid: int | None, exported: int | None, reported: int | None
) -> int | None:
    """Return a product's insert from the export's InsertId and the node's report, in that order (`insert_function`)."""
    if pid not in PUSH_BUTTON_PIDS:
        return None
    first = _insert(exported)
    return first if first is not None else _insert(reported)


def insert_mismatch(
    pid: int | None, exported: int | None, reported: int | None
) -> bool:
    """Whether a push-button reports another insert than the one the export cached for it: the insert was swapped."""
    first, second = _insert(exported), _insert(reported)
    return (
        pid in PUSH_BUTTON_PIDS
        and first is not None
        and second is not None
        and first != second
    )


def key_position(layout: int | None, location: int) -> str | None:
    """Return where the key at element `location` sits under ButtonLayout `layout` (`KEY_POSITIONS`); None when unknown.

    A rocker is one element (top = on / up, bottom = off / down), a key one element each: one top / bottom pair
    is `0040` + `0041`, a 2-gang's left half `0040` (+ `0041`), its right half `0042` (+ `0043`)
    (`docs/ios-app-data.md`, the app's cached layouts against the element lists). The mixed layouts (3, 4) follow
    that rule; unverified on air.
    """
    return KEY_POSITIONS.get(layout, {}).get(location) if layout is not None else None


def expected_device_count(pid: int | None, function: int | None) -> int | None:
    """Return how many app devices the node should yield, the app's `CheckForMissingDevices`; None: no expectation.

    One for a single-device product (`SINGLE_DEVICE_PIDS`) and for an extension insert, three for a 2-gang
    switch or dimmer (two loads and the keys), two for any other load function (the load and the keys or inputs).
    The app reports a function it cannot place (unset, unknown, not available) as an error of its own; here that
    is no expectation.
    """
    if pid in SINGLE_DEVICE_PIDS or function == EXTENSION_FUNCTION:
        return 1
    if function in TWO_GANG_FUNCTIONS:
        return 3
    if function in LAMP_FUNCTIONS or function == BLIND_FUNCTION:
        return 2
    return None


def _is_load(node: Node, element: Element, pid: int) -> bool:
    if pid in THERMOSTAT_PIDS:
        # an RTR's OnOff server (heating demand) belongs to its thermostat on whichever element it sits
        return False
    insert = insert_function(node)
    if insert is not None and insert not in LAMP_FUNCTIONS:
        return False  # a blinds insert (its blind claimed its element first) or no load at all
    if pid in BLIND_ONLY_PIDS and element in _blind_level_elements(node, pid):
        # a blinds-only node's slat element (its position element went to `_is_blind` already) belongs to its
        # blind, whatever else it hosts: an OnOff server there is not a lamp
        return False
    return element.location in LOAD_LOCATIONS and bool(
        {"1000", "1300"} & set(element.models)
    )


def _is_socket(node: Node, element: Element, pid: int) -> bool:
    return pid in SOCKET_PIDS and _is_load(node, element, pid)


def meter_element(node: Node) -> Element | None:
    """Return the node's meter: its first Sensor Server element, on any product but a detector or a thermostat.

    What the composition shows, not a product list: the metering socket (0x0003) has it on an element of its own
    at location 0x0040 (`docs/hidden-features.md` §1: Sensor Server, the SIG property servers with the energy
    counters, an OnOff client for its thresholds), the plain socket (0x000C) has none. The energy puck (0x0010)
    measures its output too (`docs/android/properties.md` §4: the app's `MeasureLampDevice` reads Sensor 0x0081,
    SIG 0x006A and the charts 0x5010 / 0x5011); where its Sensor Server sits is unverified on air (no puck seen),
    so it counts at any key location (>= 0x0040), as the socket's does. A Sensor Server on a load element (below
    0x0040) is no meter: it would otherwise meter the primary load of any node that has one. A detector's Sensor Server is the detector (`_is_detector`), an RTR's its room
    temperature: neither measures a load. The gateway has a Sensor *client* only.
    """
    if node.pid in DETECTOR_PIDS or node.pid in THERMOSTAT_PIDS:
        return None
    return next(
        (e for e in node.elements if "1100" in e.models and e.location >= KEY_LOCATION),
        None,
    )


def _blind_level_elements(node: Node, pid: int) -> list[Element]:
    """Return the node's Generic Level server elements that are not part of a lamp: position first, slats last.

    A push-button hosts whatever insert it got: with a known insert (`insert_function`) a blinds insert makes all
    its level elements blind ones and any other insert none; without one they count only when no lamp server
    sits next to them. A blinds-only product (mini, puck) has nothing but blind elements, whatever else they host.
    """
    insert = insert_function(node)
    if insert is not None and insert != BLIND_FUNCTION:
        return []
    blind_only = pid in BLIND_ONLY_PIDS or insert == BLIND_FUNCTION
    return [
        e
        for e in node.elements
        if e.location < KEY_LOCATION
        and "1002" in e.models
        and (blind_only or not LAMP_LEVEL_MODELS & set(e.models))
    ]


def _is_blind(node: Node, element: Element, pid: int) -> bool:
    if pid not in BLIND_PIDS or element.location not in LOAD_LOCATIONS:
        return False
    levels = _blind_level_elements(node, pid)
    return bool(levels) and element is levels[0]


def _is_button(node: Node, element: Element, pid: int) -> bool:
    return (
        element.location >= KEY_LOCATION
        and "1001" in element.models
        and "1100" not in element.models
    )


def _is_detector(node: Node, element: Element, pid: int) -> bool:
    """Claim the sensor element of a detector product: a Sensor Server at a key location (a socket's meter is not one)."""
    return (
        pid in DETECTOR_PIDS
        and element.location >= KEY_LOCATION
        and "1100" in element.models
    )


def _first_with_model(node: Node, model_id: str) -> Element | None:
    return next((e for e in node.elements if model_id in e.models), None)


def _is_thermostat(node: Node, element: Element, pid: int) -> bool:
    """Claim a room thermostat's primary element (its OnOff server lives there, or it would pass for a light)."""
    return pid in THERMOSTAT_PIDS and element is node.elements[0]


def _build_thermostat(ctx: BuildContext, node: Node, element: Element) -> Thermostat:
    """Build the thermostat at its set-point element (the node's first Generic Level server, else the primary)."""
    level = _first_with_model(node, "1002") or element
    onoff, sensor = _first_with_model(node, "1000"), _first_with_model(node, "1100")
    return Thermostat(
        level.address,
        ctx.unique_id(node, element),
        ctx.meta.name_for(node.uuid, element.location) or ctx.node_label(node),
        node,
        onoff.address if onoff else None,
        sensor.address if sensor else None,
        rooms=ctx.rooms_of(onoff or element),
    )


def _load_name(ctx: BuildContext, node: Node, element: Element) -> str:
    return ctx.meta.name_for(node.uuid, element.location) or ctx.node_label(node) + (
        f" out {element.location}" if element.location == 2 else ""
    )


def _build_socket(ctx: BuildContext, node: Node, element: Element) -> Socket:
    return Socket(
        element.address,
        ctx.unique_id(node, element),
        _load_name(ctx, node, element),
        node,
        ctx.meter_address(node, element),
        rooms=ctx.rooms_of(element),
    )


def ctl_temperature_element(node: Node, element: Element) -> Element | None:
    """Return the element hosting the Light CTL Temperature Server of the CTL Server on `element`, None without one.

    The spec puts it on an element of its own after the CTL Server's (both bind a Generic Level state), on a JUNG
    DALI insert the next one (`docs/hidden-features.md`). A CTL Server met first means `element` has none: the
    temperature element after it belongs to that other light.
    """
    for e in node.elements:
        if e.address <= element.address:
            continue
        if CTL_TEMPERATURE_SERVER in e.models:
            return e
        if CTL_SERVER in e.models:
            return None
    return None


def _build_light(ctx: BuildContext, node: Node, element: Element) -> Light:
    models = set(element.models)
    kind = "ctl" if CTL_SERVER in models else "dimmer" if "1300" in models else "switch"
    temperature = ctl_temperature_element(node, element) if kind == "ctl" else None
    return Light(
        element.address,
        ctx.unique_id(node, element),
        _load_name(ctx, node, element),
        node,
        kind,
        rooms=ctx.rooms_of(element),
        temperature_address=temperature.address if temperature else None,
        meter_address=ctx.meter_address(node, element),
    )


def slat_element(node: Node) -> Element | None:
    """Return a blind node's slat element (`Blind.slat_address`): the last of several blind Level elements, else None."""
    levels = _blind_level_elements(node, node.pid or 0)
    return levels[-1] if len(levels) > 1 else None


def _build_blind(ctx: BuildContext, node: Node, element: Element) -> Blind:
    slat = slat_element(node)
    return Blind(
        element.address,
        ctx.unique_id(node, element),
        _load_name(ctx, node, element),
        node,
        slat.address if slat else None,
        rooms=ctx.rooms_of(element, ("1002",)),
    )


def _build_button(ctx: BuildContext, node: Node, element: Element) -> Button:
    is_input = node.pid in MINI_ACTUATOR_PIDS
    names = INPUT_NAMES if is_input else BUTTON_LETTERS
    key = names.get(element.location, f"{element.location:02X}")
    entry = ctx.meta.entry_for(node.uuid, element.location)
    gang, group = (
        (tuple(entry[0]), entry[1])
        if entry
        else ((), f"{ctx.node_label(node)} buttons")
    )
    return Button(
        element.address,
        ctx.unique_id(node, element),
        f"{group} {key}",
        node,
        element.location,
        key,
        group,
        gang=gang,
        battery=node.pid in BATTERY_PIDS,
        input=is_input,
    )


def _build_detector(ctx: BuildContext, node: Node, element: Element) -> Detector:
    """Build the detector, named after its app entry (else the node), with the rooms of its own relay output."""
    relay = next(
        (
            e
            for e in node.elements
            if e.location in LOAD_LOCATIONS and "1000" in e.models
        ),
        None,
    )
    return Detector(
        element.address,
        ctx.unique_id(node, element),
        ctx.meta.name_for(node.uuid, element.location) or ctx.node_label(node),
        node,
        element.location,
        relay.address if relay else None,
        ctx.publish_address(element, "1001"),
        node.pid == PRESENCE_DETECTOR_PID,
        rooms=ctx.rooms_of(relay) if relay else [],
    )


ELEMENT_RULES: list[ElementRule] = [
    # first: an RTR's own OnOff server (heating demand) sits on a load location and would otherwise be a light
    ElementRule("thermostat", _is_thermostat, _build_thermostat),
    ElementRule("socket", _is_socket, _build_socket),
    ElementRule("blind", _is_blind, _build_blind),  # before "light": see _is_blind
    ElementRule("light", _is_load, _build_light),
    ElementRule("button", _is_button, _build_button),
    ElementRule("detector", _is_detector, _build_detector),
]


def load_kind(element: Element) -> str | None:
    """'thermostat' | 'socket' | 'blind' | 'light' for a load element, as `ELEMENT_RULES` classify it; else None.

    Used to keep a room's `SetGroupFunction` wiring to the app's device classes (network-logic.md §2.4) instead
    of approximating them from the element's models, which disagrees with `ELEMENT_RULES` for a blinds-only
    product (still a blind whatever else its element hosts) and an RTR (a thermostat, not a lamp).
    """
    node, pid = element.node, element.node.pid or 0
    for rule in ELEMENT_RULES:
        if rule.kind in ("thermostat", "socket", "blind", "light") and rule.matches(
            node, element, pid
        ):
            return rule.kind
    if pid in THERMOSTAT_PIDS:
        return "thermostat"  # any RTR element, not only the primary
    blinds = pid in BLIND_ONLY_PIDS or insert_function(node) == BLIND_FUNCTION
    if blinds and element.location in LOAD_LOCATIONS:
        return "blind"  # the slat element of a blinds-only node or a blinds insert
    return None


def _element_groups(cdb: CDB) -> dict[int, int]:
    """Map element group address -> the element it is named after (`element group #0x148`)."""
    return {
        group: element
        for group, name in cdb.groups.items()
        if (element := element_group_address(name)) is not None
    }


def key_connection(
    cdb: CDB, devices: Devices, element: Element
) -> KeyConnection | None:
    """Return what the key `element` drives, None when it publishes nowhere (no function, or a cleared key)."""
    meta = devices.metadata
    if BuildContext.publish_address(element, SCENE_CLIENT) == ALL_SCENES:
        scene = meta.key_scenes.get(element.address)
        name = next((s.name for s in devices.scenes if s.number == scene), None)
        return KeyConnection("scene", ALL_SCENES, name=name, scene=scene)
    publish = next(
        (
            a
            for model in KEY_CLIENT_MODELS
            if (a := BuildContext.publish_address(element, model)) is not None
        ),
        None,
    )
    if publish is None:
        return None
    owner = _element_groups(cdb).get(publish)
    property_mode = _publishes_properties_only(element)
    if owner == element.address:
        room = meta.room_links.get(element.address)
        return KeyConnection(
            "room",
            publish,
            room,
            devices.rooms.get(room or -1),
            property_mode=property_mode,
        )
    node = cdb.node_by_addr(owner) if owner is not None else None
    if node is not None and node.pid == GATEWAY_PID:
        return KeyConnection("gateway", publish, owner)
    if owner is not None:
        device = devices.by_address.get(owner)
        return KeyConnection(
            "device",
            publish,
            owner,
            device.name if device else None,
            property_mode=property_mode,
        )
    return KeyConnection("group", publish, name=devices.rooms.get(publish))


def _publishes_properties_only(element: Element) -> bool:
    """Whether only the key's LBC User Property client publishes, though it hosts an OnOff or Level client too.

    A key in light, switch or move mode publishes from those as well (`network-logic.md` §2.1); one in property mode
    from the vendor client alone. A gateway link does too, but the gateway's group says what that is.
    """
    clients = [
        m for m in KEY_CLIENT_MODELS if m != PROPERTY_CLIENT and m in element.models
    ]
    return (
        bool(clients)
        and BuildContext.publish_address(element, PROPERTY_CLIENT) is not None
        and all(BuildContext.publish_address(element, m) is None for m in clients)
    )


def with_key_lock(
    connection: KeyConnection | None, values: Mapping[int, bytes]
) -> KeyConnection | None:
    """Return `connection` as a `lock` link when the key's cached 0x5003 / 0x5006 / 0x5007 say it is one (`key_lock`).

    Only a device or room link in property mode qualifies; anything else, or a key whose values are not known yet,
    comes back as it is.
    """
    if (
        connection is None
        or not connection.property_mode
        or connection.kind not in ("device", "room")
    ):
        return connection
    lock = key_lock(values)
    if lock is None:
        return connection
    return replace(connection, kind="lock", lock=lock)


def _central_element(device: Device, group: int) -> int:
    """Return the element of `device` that listens to the central group: a blind's slat element for the slats."""
    if group == ALL_SLATS and isinstance(device, Blind) and device.slat_address:
        return device.slat_address
    return device.address


def _listens(cdb: CDB, address: int, group: int) -> bool:
    """Whether a server of the element at `address` subscribes to `group`."""
    element = cdb.element(address)
    return element is not None and any(
        group in element.subscriptions(m) for m in CENTRAL_SERVERS
    )


def build_devices(
    cdb: CDB, meta: Metadata | None = None, rules: list[ElementRule] | None = None
) -> Devices:
    """Derive the devices, scenes and rooms from the CDB, named from `meta` when given.

    Every element of every product node (the provisioning phones have no product id) goes to the first rule of
    `rules` (default `ELEMENT_RULES`) that claims it. A scene is named from `meta` first, else from the CDB's own
    `scenes[].name` (the app keeps both equal, so a raw `MeshNetwork.json` or the gateway's CDB still carries the
    user's names), else "Scene N" — the precedence `export.ProjectFile._sync_scenes` uses.
    """
    meta = meta or Metadata()
    table = ELEMENT_RULES if rules is None else rules
    ctx = BuildContext(cdb, meta)
    out = Devices(
        rooms={a: n for a, n in cdb.groups.items() if is_room(a, n)},
        metadata=meta,
    )
    # an excluded node (being removed) is in `cdb.excluded_nodes`, never a device
    for node in cdb.nodes:
        if node.pid is None:
            continue  # phone / other provisioners, another company's node
        if node.insert_function is None:
            # the iOS app container's InsertId, where no share export's `meta` gave one
            node.insert_function = meta.insert_functions.get(node.uuid)
        for element in node.elements:
            for rule in table:
                if rule.matches(node, element, node.pid):
                    out.add(rule.build(ctx, node, element))
                    break
    for num in sorted(cdb.scenes):
        name = meta.scenes.get(num) or cdb.scene_names.get(num) or f"Scene {num}"
        # the app's test is case-sensitive, on the name it shows (`kotlin.text.m.g0(name, "TimerScene", false)`)
        out.scenes.append(SceneDef(num, name, name.startswith(TIMER_SCENE_PREFIX)))
    central: tuple[tuple[int, list[Device]], ...] = (
        (ALL_LIGHTS, [*out.lights]),
        (ALL_BLINDS, [*out.blinds]),
        (ALL_SLATS, [b for b in out.blinds if b.slat_address is not None]),
        (ALL_SOCKETS, [*out.sockets]),
        (ALL_THERMOSTATS, [*out.thermostats]),
    )
    for group, candidates in central:
        members = [
            d for d in candidates if _listens(cdb, _central_element(d, group), group)
        ]
        if members:
            out.central[group] = members
    loads: list[Device] = [*out.lights, *out.sockets, *out.blinds, *out.thermostats]
    for room in out.rooms:
        if members := [d for d in loads if _listens(cdb, d.address, room)]:
            out.room_members[room] = members
    # after every device and scene: a connection names its target
    for button in out.buttons:
        key = cdb.element(button.address)
        assert key is not None  # a button is built from one of the CDB's elements
        button.connection = key_connection(cdb, out, key)
    return out
