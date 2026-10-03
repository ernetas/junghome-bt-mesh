"""What the mesh's models are, what is wired to what, and the plans that rewire it — pure.

The modes and model ids of the configurator, the wiring read from an export (element groups, sensor publication,
threshold wiring), the export's paths and digest, and the planners of rooms, key links and scenes as plain functions
(review-4 brief 55): a `ProjectFile` and its CDB in, steps out. A refusal is a `PlanError`; no Home Assistant import.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import properties as P
from custom_components.junghome_ble.jhmesh import vendor_models as V
from custom_components.junghome_ble.jhmesh.cdb import CDB, canonical_uuid, parse_address
from custom_components.junghome_ble.jhmesh.devices import (
    ALL_SCENES,
    GATEWAY_PID,
    GROUP_RANGE,
    KEY_LOCATION,
    MINI_ACTUATOR_PIDS,
    SCENE_CLIENT,
    SOCKET_PIDS,
    THERMOSTAT_PIDS,
    Metadata,
    as_int,
    ctl_temperature_element,
    is_room,
    load_kind,
    meta_list,
    slat_element,
)
from custom_components.junghome_ble.jhmesh.export import (
    FUNCTION_KEY_MODE,
    GROUP_FUNCTIONS,
    KEY_MODE_GATEWAY,
    KEY_MODE_LIGHT,
    KEY_MODE_MOVE,
    KEY_MODE_PROPERTY,
    KEY_MODE_RTR,
    KEY_MODE_SCENE,
    KEY_MODE_SERVERS,
    KEY_MODE_SWITCH,
    RENAME_MAX_LENGTH,
    AllocationCrowded,
    ExportError,
    InvalidName,
    ProjectFile,
    cdb_element_groups,
    check_name,
    function_code,
    has_model,
    hexaddr,
    keeps_row,
    location_ids,
    meta_rows,
    raw_model,
)
from custom_components.junghome_ble.jhmesh.merge import MISSING, Change

from .plan import (
    ConfigStep,
    KeyPlan,
    Note,
    PlanError,
    _deletable,
    _drop_scene_key_row,
    bind_step,
    config_steps,
)

if TYPE_CHECKING:
    from custom_components.junghome_ble.jhmesh.cdb import Element, Node

_LOGGER = logging.getLogger(__name__)

# Service-facing mode names. `light_and_switch` is a room function (lamps and sockets together); `gateway` is only
# meaningful with the gateway's primary element as the target (network-logic.md §2.3, "key -> gateway"); `lock` is
# the app's locking function on a light or socket, KeyMode *property* (network-logic.md §2.6); `temperature` (key
# mode 4, the app's *Temperature* category: the set-point up / down) needs a room thermostat's.
MODE_LIGHT, MODE_SWITCH, MODE_MOVE, MODE_GATEWAY, MODE_LIGHT_AND_SWITCH, MODE_LOCK = (
    "light",
    "switch",
    "move",
    "gateway",
    "light_and_switch",
    "lock",
)
MODE_TEMPERATURE = "temperature"
KEY_MODES: dict[str, int] = {
    MODE_LIGHT: KEY_MODE_LIGHT,
    MODE_SWITCH: KEY_MODE_SWITCH,
    MODE_MOVE: KEY_MODE_MOVE,
    MODE_GATEWAY: KEY_MODE_GATEWAY,
    MODE_LIGHT_AND_SWITCH: KEY_MODE_LIGHT,
    MODE_LOCK: KEY_MODE_PROPERTY,
    MODE_TEMPERATURE: KEY_MODE_RTR,
}
ROOM_FUNCTIONS: dict[
    str, str
] = {  # GroupConnection.Function per mode (network-logic.md §2.1)
    MODE_LIGHT: "LIGHT",
    MODE_SWITCH: "SWITCH",
    MODE_MOVE: "BLIND",
    MODE_LIGHT_AND_SWITCH: "LIGHT_AND_SWITCH",
}
DEVICE_MODES = frozenset(
    {MODE_LIGHT, MODE_SWITCH, MODE_MOVE, MODE_GATEWAY, MODE_LOCK, MODE_TEMPERATURE}
)
ROOM_MODES = frozenset(ROOM_FUNCTIONS)
# a detector source (`ConnectionSource.Detector`) has no KeyMode nor property mode: it drives a load in the mode the
# load gives, never a gateway, a lock function or a thermostat's set-point (those live in the key's KeyMode / 0x5006)
DETECTOR_MODES = frozenset({MODE_LIGHT, MODE_SWITCH, MODE_MOVE})
# a scene link's key mode: never a `mode` of the action, which names the scene instead (`assign_key(scene=…)`)
MODE_SCENE = "scene"
# blinds and room thermostats: no such device was ever wired from here; scene links: never tried on a real device
# (review-3 F15); lock links: written from the app's code alone, no capture of the app making one yet (review-4
# brief 38)
UNTESTED_MODES = frozenset({MODE_MOVE, MODE_SCENE, MODE_LOCK, MODE_TEMPERATURE})
MODES = tuple(KEY_MODES)
# `DeviceConnection.Element` beyond the target's own element (network-logic.md §2.1), with the mode the target's kind
# gives: a tunable-white light's temperature element (LIGHT_TEMPERATURE, `Light.temperature_address`) and a blind's
# slat element (SLAT, `Blind.slat_address`), either driven by the key's Level client alone. Unverified on air.
TARGET_COLOR_TEMPERATURE, TARGET_SLAT = "color_temperature", "slat"
TARGET_ELEMENTS: dict[str, str] = {
    TARGET_COLOR_TEMPERATURE: MODE_LIGHT,
    TARGET_SLAT: MODE_MOVE,
}
LEVEL_CLIENT = "1003"
LOCK_SECONDS_MAX = 0xFFFF  # EnforceOutput's time field, u16 seconds; 0 = no limit

# Client models the key element publishes from, per key mode (network-logic.md §2.1).
KEY_MODE_CLIENTS: dict[int, tuple[str, ...]] = {
    KEY_MODE_LIGHT: ("1001", "1003", "05271015"),
    KEY_MODE_MOVE: ("1003", "05271015"),
    KEY_MODE_SWITCH: ("1001", "05271015"),
    KEY_MODE_GATEWAY: ("05271015",),
    KEY_MODE_SCENE: (SCENE_CLIENT,),  # publish only, to all nodes
    KEY_MODE_PROPERTY: ("05271015",),
    # the set-point's Generic Level; unverified on air (no room thermostat here)
    KEY_MODE_RTR: ("1003",),
}
# `RemoveConnectionForAddress` never touches these models of a key element (network-logic.md §2.3 step 4).
CLEAR_KEEP_MODELS = frozenset({"1100", "05271013", "05271011"})
USER_PROPERTY_SERVER = "05271013"
ONOFF_SERVER, ONOFF_CLIENT = "1000", "1001"
SENSOR_SERVER = "1100"

PROPERTY_KEY_MODE = 0x5003
PROPERTY_KEY_SCENE_CONFIG = (
    0x5002  # KeyModeSceneConfig: the scene a key in scene mode recalls
)
# The servers a key's own load element hosts in the app's pick for a scene link's cached publication address
# (`ConnectionSceneSelectionActivity` → `e2()`: OnOff / Level / Lightness / Scene / CTL servers, network-logic.md §2.5)
SCENE_LINK_LOAD_SERVERS = ("1000", "1002", "1300", "1203", "1303")
PROPERTY_KEY_PROPERTY_MODE, PROPERTY_KEY_VALUE_UP, PROPERTY_KEY_VALUE_DOWN = (
    0x5006,
    0x5007,
    0x5008,
)

SCENE_SETUP_SERVER, SCENE_ACTION_SETUP = "1204", "05271017"


def _subscribed(element: Element, model: str, address: int) -> bool:
    return address in element.subscriptions(raw_model(element, model)["modelId"])


def element_groups(pf: ProjectFile) -> dict[int, int]:
    """Map element address -> its element group (`element group #…` in the CDB, `meta.elementConnectionGroups`)."""
    return cdb_element_groups(pf.cdb, pf.meta)


def sensor_elements(node: Node) -> list[Element]:
    """Return the node's elements with a Sensor Server (a socket's meter, a detector's sensor, a thermostat)."""
    return [e for e in node.elements if has_model(e, SENSOR_SERVER)]


def sensor_publication(cdb: CDB, meta: dict[str, Any] | None, node: Node) -> bool:
    """Whether the node's sensor values are published: any Sensor Server publishing to its own element group."""
    groups = cdb_element_groups(cdb, meta)
    for element in sensor_elements(node):
        pub = raw_model(element, SENSOR_SERVER).get("publish")
        group = groups.get(element.address)
        if (
            pub
            and group is not None
            and parse_address(str(pub.get("address"))) == group
        ):
            return True
    return False


def threshold_client(node: Node) -> Element | None:
    """Return the element a metering socket's thresholds switch through: the one with the OnOff Client (the meter's)."""
    return next((e for e in node.elements if has_model(e, ONOFF_CLIENT)), None)


def threshold_devices(cdb: CDB, client: Element, group: int) -> list[Element]:
    """Return the load elements a socket's thresholds switch: every OnOff server on the client's element group.

    The app keeps no other record (`ObserveThresholdDevices` derives them from the subscriptions the same way).
    """
    return [
        element
        for node in cdb.nodes
        for element in node.elements
        if element is not client
        and has_model(element, ONOFF_SERVER)
        and _subscribed(element, ONOFF_SERVER, group)
    ]


# what a load a socket's thresholds switch subscribes to the socket's element group, in the app's order
# (`CreateThreshold`, on air: the JUNG User Property Server first, then the OnOff server)
THRESHOLD_TARGET_MODELS = (USER_PROPERTY_SERVER, ONOFF_SERVER)


def threshold_wiring(
    cdb: CDB, client: Element, group: int
) -> list[tuple[Element, str]]:
    """Return every (element, model) the socket's threshold wiring subscribed to the client's element group.

    The loads' OnOff servers (`threshold_devices`) and their JUNG User Property Servers (`0x0527:1013`): the app
    subscribes both, and a load HA wired before it did too has the OnOff server alone. The client's own
    `0x0527:1013` listens to its group by itself (every element's does) and is not wiring. OnOff server first,
    as the app's disable unsubscribes them.
    """
    return [
        (element, model)
        for node in cdb.nodes
        for element in node.elements
        if element is not client
        for model in (ONOFF_SERVER, USER_PROPERTY_SERVER)
        if has_model(element, model) and _subscribed(element, model, group)
    ]


def export_digest(doc: dict[str, Any]) -> str | None:
    """SHA-256 of a gateway export's `(meta, network)` content, canonical so whitespace or key order don't matter.

    None for a document that carries no `meta` at all — the `{"meshNetwork": …}` shape `fetch_project`'s
    `/project/cdb` fallback returns. It can never legitimately equal a share export's digest, so treating it as
    "no digest" (rather than hashing `None` for `meta`) keeps a real change from silently comparing equal to it.
    """
    network = doc.get("network")
    if not isinstance(network, str):
        return None
    net = json.loads(base64.b64decode(network))
    if isinstance(net, dict) and "meshNetwork" in net:
        net = net["meshNetwork"]
    canonical = json.dumps(
        {"meta": doc.get("meta"), "network": net},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def derive_mode(element: Element) -> str:
    """Key mode the app would pick for a target element (`SetDeviceConnection.getKeyMode`, network-logic.md §2.1)."""
    node = element.node
    if node.pid == GATEWAY_PID:
        return MODE_GATEWAY
    if node.pid in THERMOSTAT_PIDS:
        # `ActuatorFunctionId` Rtr -> RTR (4); unverified on air
        return MODE_TEMPERATURE
    if node.pid in SOCKET_PIDS:
        return MODE_SWITCH
    if has_model(element, "1300") or has_model(element, "1303"):
        return MODE_LIGHT  # dimming / tunable white
    if load_kind(element) == "blind":
        return MODE_MOVE
    if has_model(element, "1000"):
        return MODE_SWITCH
    raise PlanError("service_no_mode", address=hexaddr(element.address))


def _lockable(element: Element) -> bool:
    """Whether a key may lock `element` (`MODE_LOCK`): a light or socket of a product with the lock function 0x0009.

    Its element must host the LBC User Property server the key's vendor client publishes to. A blind has a lock
    function too, but its lock-out protection and wind alarm are not offered here (no blind to check them on).
    """
    return (
        load_kind(element) in ("light", "socket")
        and (element.node.pid or 0) in P.LOCKABLE
        and has_model(element, USER_PROPERTY_SERVER)
    )


def _confirms_property(params: bytes, prop: int, value: bytes) -> bool:
    """Whether an LBC Admin Property Status `[propId u16][access u8][value…]` reports `prop` == `value`."""
    return (
        len(params) >= 3 + len(value)
        and int.from_bytes(params[:2], "little") == prop
        and params[3 : 3 + len(value)] == value
    )


def _confirms_key_mode(params: bytes, key_mode: int) -> bool:
    """Whether an LBC Admin Property Status reports KeyMode == `key_mode`."""
    return _confirms_property(params, PROPERTY_KEY_MODE, bytes([key_mode]))


# what a carried-over change may hold that is never shown: a key (`netKeys[].key`, `nodes[].deviceKey`, …)
_SECRET_FIELDS = frozenset({"key", "oldKey", "deviceKey"})
# the fields of an entry that say which one it is, in this order (a room, a scene, a node, a link row)
_IDENTITY_FIELDS = (
    "address",
    "number",
    "unicastAddress",
    "elementAddress",
    "groupAddress",
    "name",
)


def held(change: Change) -> str:
    """Say what Home Assistant wrote at a conflicting change's path — what the nodes still hold — without any key.

    For the `carry_over_conflict` repair: a value the change removed is "nothing"; an entry (a room, a scene, a
    node) is named by its identifying fields only, never written out whole (a node entry carries its device key);
    a list of plain values (subscriptions) is listed; a key field is never rendered.
    """
    if change.new is MISSING:
        return "nothing (Home Assistant removed it)"
    return shown(change.new, change.path)


def app_copy_path(cdb_path: str | Path) -> Path:
    """Where the app's last upload is kept beside an export set up from a gateway (`<export>.app`).

    It is the base of the three-way merge that carries Home Assistant's changes over onto the app's next upload
    (`ExportStore._carry_over`): the app never downloads the project, so its uploads lack them.
    """
    path = Path(cdb_path)
    return path.with_name(path.name + ".app")


def pre_adopt_path(cdb_path: str | Path) -> Path:
    """Where the export is kept as it was before the last adoption of the gateway's export (`<export>.pre-adopt`).

    Review-3 W7: an adoption replaces Home Assistant's copy wholesale (with its changes carried over, but a
    merge can be wrong), and the change planned on it usually saves right after — the rotating backups of the
    saves soon pass the copy by. This one stays until the next adoption.
    """
    path = Path(cdb_path)
    return path.with_name(path.name + ".pre-adopt")


def load_project(cdb_path: str, metadata_dir: str | None) -> ProjectFile:
    """Blocking: read the export (either flavour) with the optional iOS app-container names overlaid.

    New rooms and scenes of the file are allocated from the top of the app's ranges (review-4 W4-2): the app never
    downloads the project, so it does not know them until it imports a file, and gives its own next room or scene
    the lowest number it believes free. With the provisioner identity on they go into Home Assistant's own ranges.
    """
    metadata = (
        Metadata(
            Path(metadata_dir) / "device_metadata.json",
            Path(metadata_dir) / "scene_metadata.json",
        )
        if metadata_dir
        else None
    )
    pf = ProjectFile.load(Path(cdb_path), metadata)
    pf.allocation = "top"
    return pf


def shown(value: Any, path: tuple[Any, ...]) -> str:
    """Say what an export holds at `path` without any key: an entry by its identifying fields, a list's plain values.

    A key field is never rendered (`_SECRET_FIELDS`), nor an entry written out whole (a node entry carries its
    device key); a value that is not there is "nothing".
    """
    last = path[-1] if path else None
    if value is MISSING:
        return "nothing"
    if isinstance(last, str) and last in _SECRET_FIELDS:
        return "a key (not shown)"
    if isinstance(value, dict):
        named = [
            f"{name} {value[name]}"
            for name in _IDENTITY_FIELDS
            if isinstance(value.get(name), (str, int))
        ]
        return ", ".join(named) or "an entry"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if isinstance(v, (str, int))) or "none"
    return str(value)


def name_error(err: InvalidName, name: str) -> PlanError:
    """Return the refusal of a name the app refuses (blank, a lone `%`, a rename past the sheet's limit)."""
    if err.reason == "blank":
        return PlanError("service_name_blank")
    if err.reason == "too_long":
        return PlanError(
            "service_name_too_long", name=name, max_length=str(RENAME_MAX_LENGTH)
        )
    return PlanError("service_name_not_allowed", name=name)


# ------------------------------------------------------------------ lookups and planners


def find_element(pf: ProjectFile, address: int) -> Element:
    """Return the element at `address`; refuse an address the export has no element at."""
    element = pf.cdb.element(address)
    if element is None:
        raise PlanError("service_unknown_element", address=hexaddr(address))
    return element


def find_room(pf: ProjectFile, name: str) -> int:
    """Return the address of the room called `name` (case-insensitive); refuse a name no room has."""
    wanted = name.strip().lower()
    for addr, room in pf.user_groups().items():
        if room.lower() == wanted:
            return addr
    raise PlanError("service_no_room", room=name)


def check_room_name(name: str) -> str:
    """Validate a room name as the services accept it: non-empty, one the app can use, not one the mesh reserves.

    A name with a lone `%` is refused as the app's `CheckNameInput` refuses it (`check_name`). `element group
    #…`, `device type group…` and `#time_keeper_group#` name the internal groups the app filters out of its
    room list (`devices.is_room`); a room called that would vanish from the app.
    """
    wanted = name.strip()
    if not wanted:
        raise PlanError("service_invalid_room_name")
    try:
        check_name(wanted)
    except InvalidName as err:
        raise name_error(err, wanted) from err
    if not is_room(GROUP_RANGE[0], wanted):
        raise PlanError("service_room_name_reserved", name=wanted)
    return wanted


def add_room(pf: ProjectFile, name: str) -> int:
    """Add a room called `name` (`check_room_name`) to `pf` and return its address; refuse one it cannot add."""
    wanted = check_room_name(name)
    try:
        return pf.add_group(wanted)
    except ValueError as err:
        raise PlanError("service_room_exists", room=name) from err
    except AllocationCrowded as err:
        raise PlanError("service_room_range_crowded", free=str(err.below)) from err
    except ExportError as err:  # the provisioner's group range is used up
        raise PlanError("service_room_range_full") from err


def rooms_of(pf: ProjectFile, element: Element) -> list[int]:
    """Return the rooms `element` is in: the room addresses any of its models subscribes to."""
    rooms = pf.user_groups()
    return sorted(
        {
            a
            for m in element.raw_models
            for a in element.subscriptions(m["modelId"])
            if a in rooms
        }
    )


def room_keys(pf: ProjectFile, element: Element, group: int) -> list[int]:
    """Return the key elements whose room link to `group` drives `element`: it listens to the link's publish group."""
    listened = {
        address
        for raw in element.raw_models
        for address in element.subscriptions(raw["modelId"])
    }
    return [
        key
        for row in pf.room_connections(group)
        if (key := as_int(row.get("elementAddress"))) is not None
        and as_int(row.get("publishAddress")) in listened
    ]


def device_entry(pf: ProjectFile, node: Node, location: int) -> dict[str, Any] | None:
    """Return the `meta.devices[]` entry covering an element location: the most specific one, as the app resolves it."""
    best: dict[str, Any] | None = None
    best_size = 0
    for dev in meta_rows(meta_list(pf.meta.get("devices"))):
        did = dev.get("deviceId")
        if (
            not isinstance(did, dict)
            or canonical_uuid(str(did.get("nodeId", ""))) != node.uuid
        ):
            continue
        locations = location_ids(did.get("locationIds"))
        if locations is None:
            continue
        if location in locations and (best is None or len(locations) < best_size):
            best, best_size = dev, len(locations)
    return best


def unlink_room_steps(
    pf: ProjectFile, key: Element, publish: int, key_mode: int
) -> list[ConfigStep]:
    """`RemoveConnectionForAddress.GroupConnection`: the loads listening to the key's publish group stop."""
    steps: list[ConfigStep] = []
    for node in pf.cdb.nodes:
        for element in node.elements:
            if element is key:
                continue
            for model in KEY_MODE_SERVERS[key_mode]:
                if has_model(element, model) and _subscribed(element, model, publish):
                    steps += config_steps(pf, pf.unsubscribe(element, model, publish))
    return steps


def clear_steps(pf: ProjectFile, key: Element) -> list[ConfigStep]:
    """`RemoveConnectionForAddress`: drop the key's room link and every publication / subscription it has.

    A room link (`cachedGroupConnectionMetadata` row) first unsubscribes the room's loads from the key's publish
    group (`GroupConnection`); then every model of the key element except the Sensor / LBC servers loses its
    publication (`Publication Set 0x0000`) and each of its subscriptions (`Subscription Delete` per address).
    Every step is tagged with the key: a record of a stopped plan drops the link's `meta` row only once every
    tagged step was accepted, so a partly-unwired load still has a row naming the group it is stuck on.
    """
    return [replace(s, unlinks=key.address) for s in _clear_plan(pf, key)]


def _clear_plan(pf: ProjectFile, key: Element) -> list[ConfigStep]:
    steps: list[ConfigStep] = []
    own_group = element_groups(pf).get(key.address)
    for dev in meta_rows(meta_list(pf.meta.get("devices"))):
        cached = meta_list(dev.get("cachedGroupConnectionMetadata"))
        links = [
            r
            for r in meta_rows(cached)
            if as_int(r.get("elementAddress")) == key.address
        ]
        if not links:
            continue
        for row in links:
            publish = as_int(row.get("publishAddress")) or own_group
            key_mode = FUNCTION_KEY_MODE.get(
                function_code(row.get("function")) or 0, KEY_MODE_LIGHT
            )
            if publish is not None:
                steps += unlink_room_steps(pf, key, publish, key_mode)
        dev["cachedGroupConnectionMetadata"] = [r for r in cached if r not in links]
    _drop_scene_key_row(pf, key.address)
    for raw in key.raw_models:
        model = raw["modelId"]
        if model.upper() in CLEAR_KEEP_MODELS:
            continue
        if pf.publication(key, model) is not None:
            steps += config_steps(pf, pf.set_publication(key.node, key, model, None))
        for group in key.subscriptions(model):
            if not _deletable(group):
                # review-3 W12: no Subscription Delete can carry it; the key keeps it, the rest is cleared
                _LOGGER.warning(
                    "Key %04X keeps its subscription to %04X (model %s): a virtual or fixed group address "
                    "cannot be removed from here",
                    key.address,
                    group,
                    model,
                )
                continue
            steps += config_steps(pf, pf.unsubscribe(key, model, group))
    return steps


def record_room_link(
    pf: ProjectFile,
    key: Element,
    room: int,
    publish: int,
    function: str,
    metadata: Metadata,
    *,
    keep: bool = False,
) -> None:
    """Store the `KeyModeGroupConfig` the app needs to show (and later re-wire) a room link.

    It replaces the key's earlier rows, unless `keep`: the record of a stopped plan keeps them beside the new
    one, since the old link's loads may still listen to the key — its rows are what makes the next clear
    unwire them — and `PlanExecutor.record` drops them once every step tearing the old link down was accepted. A key no
    device entry covers gets one named as `metadata` (the hub's device model) names it.
    """
    entry = device_entry(pf, key.node, key.location)
    if entry is None:
        found = metadata.entry_for(key.node.uuid, key.location)
        locations, name = found or (
            [key.location],
            f"{key.node.name} {key.node.unicast:04X} buttons",
        )
        entry = pf.set_device_name(key.node, locations, name)
    template = next(
        (
            r
            for dev in meta_rows(meta_list(pf.meta.get("devices")))
            for r in meta_rows(meta_list(dev.get("cachedGroupConnectionMetadata")))
        ),
        None,
    )
    row: dict[str, Any] = {
        "elementAddress": key.address,
        "groupAddress": room,
        "publishAddress": publish,
        "function": function,
    }
    if (
        template is not None
    ):  # mirror the file's own style: int vs hex-string addresses, enum name vs ordinal
        if isinstance(template.get("elementAddress"), str):
            row = {k: hexaddr(v) if isinstance(v, int) else v for k, v in row.items()}
        if isinstance(template.get("function"), int):
            row["function"] = GROUP_FUNCTIONS[function]
        row = {k: row[k] for k in template if k in row} | row
    kept = [
        r
        for r in meta_list(entry.get("cachedGroupConnectionMetadata"))
        if (r != row if keep else keeps_row(r, "elementAddress", key.address))
    ]
    entry["cachedGroupConnectionMetadata"] = [*kept, row]


def plan_room_link(
    pf: ProjectFile, key: Element, room: str, mode: str | None, metadata: Metadata
) -> KeyPlan:
    """`SetGroupConnection` + `SetGroupFunction`: the room's loads of the function's kind listen to the key."""
    mode = mode or MODE_LIGHT
    if mode not in ROOM_MODES:
        raise PlanError("service_invalid_mode", mode=mode, target="room")
    room_address = find_room(pf, room)
    function = ROOM_FUNCTIONS[mode]
    code = GROUP_FUNCTIONS[function]
    key_mode = FUNCTION_KEY_MODE[code]
    publish = element_groups(pf).get(key.address)
    if publish is None:
        raise PlanError("service_no_element_group", address=hexaddr(key.address))
    steps = clear_steps(pf, key)
    for member in pf.group_members(room_address):
        if member is key or not pf._matches_function(member, code):  # noqa: SLF001
            continue
        for model in KEY_MODE_SERVERS[key_mode]:
            if has_model(member, model):
                steps += config_steps(pf, pf.subscribe(member, model, publish))
    record_room_link(pf, key, room_address, publish, function, metadata)
    if pf.flavour == "cdb":
        _LOGGER.warning(
            "%s is a raw MeshNetwork.json: the room link of key %04X is configured on the mesh but cannot be "
            "recorded in the file; the app will not show it and later room members are not wired to the key",
            pf.path,
            key.address,
        )
    # review-3 W5: loads the stopped plan did subscribe need the row, or no later clear unwires them
    prepare: Note = {
        "kind": "room_link",
        "key": key.address,
        "room": room_address,
        "publish": publish,
        "function": function,
    }
    return KeyPlan(
        steps,
        key_mode,
        publish,
        mode,
        f"room {room!r} ({room_address:04X})",
        prepare,
    )


def plan_device_link(
    pf: ProjectFile,
    key: Element,
    address: int,
    mode: str | None,
    target_element: str | None = None,
    lock_seconds: int | None = None,
) -> KeyPlan:
    """`SetDeviceConnection`: the key's clients go to the target's element group; the target itself is untouched.

    Its servers already sit on that group since provisioning; a target the app never wired completely gets the
    missing subscriptions. A socket or mini-actuator target additionally gets
    `ConfigurePublicationsForPropertyUser` (the app filters to those two kinds): the User Property servers on
    every one of its elements — a mini actuator's inputs E1 / E2 included — publish to their element groups and
    the key's clients subscribe there. Unverified on air for a mini actuator (none was wired from here).

    `target_element` picks another element of the target (`TARGET_ELEMENTS`, `pick_target_element`); `mode: lock`
    is the app's locking function (`SetLockingFunctionConnection`, `Keys._write_lock_function`). Both unverified on air.
    """
    target = find_element(pf, address)
    clients: tuple[str, ...] | None = None
    if target_element is not None:
        target, mode = pick_target_element(target, target_element, mode)
        clients = (LEVEL_CLIENT,)
    mode = mode or derive_mode(target)
    if (
        mode not in DEVICE_MODES
        or ((mode == MODE_GATEWAY) != (target.node.pid == GATEWAY_PID))
        or ((mode == MODE_TEMPERATURE) != (target.node.pid in THERMOSTAT_PIDS))
    ):
        raise PlanError(
            "service_invalid_mode", mode=mode, target=hexaddr(target.address)
        )
    if mode == MODE_LOCK and not _lockable(target):
        raise PlanError("service_lock_unsupported", target=hexaddr(target.address))
    key_mode = KEY_MODES[mode]
    groups = element_groups(pf)
    publish = groups.get(target.address)
    if publish is None:
        raise PlanError("service_no_element_group", address=hexaddr(target.address))
    steps = clear_steps(pf, key)
    for model in KEY_MODE_SERVERS[key_mode]:
        if has_model(target, model) and not _subscribed(target, model, publish):
            steps += config_steps(pf, pf.subscribe(target, model, publish))
    if target.node.pid in SOCKET_PIDS | MINI_ACTUATOR_PIDS:
        steps += property_user_steps(
            pf, key, target, clients or KEY_MODE_CLIENTS[key_mode], groups
        )
    plan = KeyPlan(
        steps,
        key_mode,
        publish,
        mode,
        f"element {target.address:04X}",
        clients=clients,
    )
    if mode == MODE_LOCK:
        plan = replace(
            plan,
            lock=P.key_lock_values(lock_seconds or 0),
            lock_target=target.address,
        )
    return plan


def pick_target_element(
    target: Element, choice: str, mode: str | None
) -> tuple[Element, str]:
    """Return the element a `target_element` names on the target load, and the mode it takes; else refuse.

    `color_temperature`: a tunable-white light's temperature element (`ctl_temperature_element`), in the light
    mode the app derives from a tunable-white target; `slat`: a blind's slat element (`slat_element`), in move
    mode. Another mode than that one is refused. Unverified on air.
    """
    wanted = TARGET_ELEMENTS.get(choice)
    element: Element | None = None
    if choice == TARGET_COLOR_TEMPERATURE and load_kind(target) == "light":
        element = ctl_temperature_element(target.node, target)
    elif choice == TARGET_SLAT and load_kind(target) == "blind":
        element = slat_element(target.node)
    if wanted is None or element is None:
        raise PlanError(
            "service_target_element_missing",
            target=hexaddr(target.address),
            target_element=choice,
        )
    if (mode or wanted) != wanted:
        raise PlanError(
            "service_invalid_mode",
            mode=str(mode),
            target=f"{hexaddr(element.address)} ({choice})",
        )
    return element, wanted


def property_user_steps(
    pf: ProjectFile,
    key: Element,
    target: Element,
    models: Iterable[str],
    groups: dict[int, int],
) -> list[ConfigStep]:
    """`ConfigurePublicationsForPropertyUser`: the target node's User Property servers publish to their groups.

    Every such server publishes to its element's group, and the key's client `models` (those it hosts) subscribe
    to each such group.
    """
    steps: list[ConfigStep] = []
    clients = [m for m in models if has_model(key, m)]
    for element in target.node.elements:
        group = groups.get(element.address)
        if group is None or not has_model(element, USER_PROPERTY_SERVER):
            continue
        if pf.publication(element, USER_PROPERTY_SERVER) != group:
            # a model publishes with the AppKey it is bound to: bind first, as the app does (review-3 W10)
            bind = bind_step(element, USER_PROPERTY_SERVER)
            if bind is not None:
                steps.append(bind)
            steps += config_steps(
                pf,
                pf.set_publication(element.node, element, USER_PROPERTY_SERVER, group),
            )
        for model in clients:
            steps += config_steps(pf, pf.subscribe(key, model, group))
    return steps


def plan_scene_link(
    pf: ProjectFile, key: Element, scene: str | int, mode: str | None
) -> KeyPlan:
    """`SetSceneConnection` (network-logic.md §2.5): the key recalls a scene on every node. Unverified on air.

    The key's connections are cleared (`RemoveConnectionForAddress.AllConnections`) and its Scene Client
    publishes to `0xFFFF` (publish only: `assign_key` adds no subscription for it); once that is accepted the
    key gets the scene number (`Keys._write_scene_config`), the app's `keyModeSceneConfigExports` row
    (`record_scene_link`) and KeyMode 2. The target is a scene, so `mode` must be left out.
    """
    number = find_scene(pf, scene)
    if mode is not None:
        raise PlanError("service_invalid_mode", mode=mode, target=f"scene {number}")
    steps = clear_steps(pf, key)
    if pf.flavour == "cdb":
        _LOGGER.warning(
            "%s is a raw MeshNetwork.json: the scene link of key %04X is configured on the mesh but cannot be "
            "recorded in the file; the app will not show which scene the key recalls",
            pf.path,
            key.address,
        )
    own_group = scene_link_group(pf, key)

    def record(project: ProjectFile) -> None:
        record_scene_link(project, key.address, number, own_group)

    return KeyPlan(
        steps,
        KEY_MODE_SCENE,
        ALL_SCENES,
        MODE_SCENE,
        f"scene {number}",
        scene=number,
        record_scene=record,
    )


def scene_link_group(pf: ProjectFile, key: Element) -> int | None:
    """Return the element group of the key's own load, which the app caches with a scene link (never sent on air).

    The app takes the load element it maps to the key (`e2()`, network-logic.md §2.5); here, the node's first
    load element with one of those servers — on the fixture export the recorded row names that one. None for a
    node without a load (a wall transmitter, a binary-input puck): the Android export makes the field optional.
    """
    groups = element_groups(pf)
    for element in key.node.elements:
        if element.location < KEY_LOCATION and any(
            has_model(element, model) for model in SCENE_LINK_LOAD_SERVERS
        ):
            return groups.get(element.address)
    return None


def record_scene_link(
    pf: ProjectFile, key: int, number: int, publication: int | None
) -> None:
    """Store the `keyModeSceneConfigExports` row that makes the app show the key as recalling scene `number`.

    `{"sceneConfig": {transitionStepSeconds, sceneId, transitionResolution, publicationAddress?},
    "elementAddress"}` (`MeshPropertyExport.java:153-161`), no transition as the app writes it; it replaces the
    key's earlier row and mirrors an existing row's style (field order, int vs hex-string addresses).
    """
    rows = meta_list(pf.meta.get("keyModeSceneConfigExports"))
    template = next(
        (r for r in meta_rows(rows) if isinstance(r.get("sceneConfig"), dict)),
        None,
    )
    config: dict[str, Any] = {
        "transitionStepSeconds": 0,
        "sceneId": number,
        "transitionResolution": 0,
    }
    if publication is not None:
        config["publicationAddress"] = publication
    row: dict[str, Any] = {"sceneConfig": config, "elementAddress": key}
    if template is not None:
        if isinstance(template.get("elementAddress"), str):
            row["elementAddress"] = hexaddr(key)
            if publication is not None:
                config["publicationAddress"] = hexaddr(publication)
        old = template["sceneConfig"]
        row["sceneConfig"] = {k: config[k] for k in old if k in config} | config
        row = {k: row[k] for k in template if k in row} | row
    kept = [r for r in rows if keeps_row(r, "elementAddress", key)]
    pf.meta["keyModeSceneConfigExports"] = [*kept, row]


def find_scene(pf: ProjectFile, scene: str | int) -> int:
    """Resolve a scene given by number ("5") or by the app's name (case-insensitive).

    The services pass both as text, and the app allows an all-digit name: text that is one scene's number and
    another scene's name cannot be told apart, so it is refused rather than guessed (a guess could delete the
    wrong scene from every member node).
    """
    text = str(scene).strip()
    by_name = next(
        (
            n
            for n, name in pf.scene_names().items()
            # a `meta.scenes[]` row left over from a deleted scene names nothing
            if n in pf.cdb.scenes and name.strip().lower() == text.lower()
        ),
        None,
    )
    # `isdigit` alone takes "²" too, which `int` refuses
    number_text = text.isascii() and text.isdigit()
    by_number = int(text) if number_text and int(text) in pf.cdb.scenes else None
    if by_name is not None and by_number is not None and by_name != by_number:
        raise PlanError("service_ambiguous_scene", scene=text)
    number = by_name if by_name is not None else by_number
    # scene 0 is not a scene the Scene Store / Scene Action Setup messages can carry (the builders refuse it)
    if number is None or number < 1 or number not in pf.cdb.scenes:
        raise PlanError("service_unknown_scene", scene=text)
    return number


def scene_load(pf: ProjectFile, address: int) -> tuple[Element, Element]:
    """Return the load element at `address` and the element its scenes are stored on.

    The app stores a device's scenes on the node's *first* Scene Setup Server — the primary element, for both
    channels of a two-channel node (network-features.md §3) — and writes the JUNG scene action to the channel
    element itself when that has a Scene Action Setup server.
    """
    element = find_element(pf, address)
    store = next(
        (e for e in element.node.elements if has_model(e, SCENE_SETUP_SERVER)),
        None,
    )
    if store is None or not (
        has_model(element, SCENE_SETUP_SERVER) or has_model(element, SCENE_ACTION_SETUP)
    ):
        raise PlanError("service_not_a_scene_element", address=hexaddr(address))
    return element, store


def scene_key_steps(
    pf: ProjectFile, nodes: Iterable[Node], number: int
) -> list[ConfigStep]:
    """`RemoveConnectionForAddress.SceneConnection`: clear the keys of `nodes` wired to recall scene `number`.

    The app runs it first when a device leaves a scene (network-features.md §3 *Remove device from scene*): a
    key of that device whose cached scene link (`keyModeSceneConfigExports`, the app's `KeyModeSceneConfig`
    cache) names the scene loses its connections, as `clear_key` clears a key. Keys of other devices keep
    their link, as in the app. Unlike the app, the row alone is not enough: the key's Scene Client must still
    publish to all nodes, the scene key's wiring — a row can outlive the link it cached (review-3 W2), and
    clearing a key wired to a load or the gateway because of it would take a working key away.
    """
    linked = {
        as_int(row.get("elementAddress"))
        for row in meta_rows(meta_list(pf.meta.get("keyModeSceneConfigExports")))
        if isinstance(config := row.get("sceneConfig"), dict)
        and as_int(config.get("sceneId")) == number
    }
    steps: list[ConfigStep] = []
    for node in {n.unicast: n for n in nodes}.values():
        for key in node.elements:
            if (
                key.address in linked
                and has_model(key, SCENE_CLIENT)
                and pf.publication(key, SCENE_CLIENT) == ALL_SCENES
            ):
                _LOGGER.info(
                    "Key %04X recalls scene %d: its connections are cleared with the scene",
                    key.address,
                    number,
                )
                steps += clear_steps(pf, key)
    return steps


def _sibling_channels(element: Element) -> list[Element]:
    """Return the node's other elements with a Scene Action Setup server: the channels sharing its register."""
    return [
        other
        for other in element.node.elements
        if other is not element and has_model(other, SCENE_ACTION_SETUP)
    ]


def _confirms_scene_action(params: bytes, scene: int, action: V.Action | None) -> bool:
    """Whether a Scene Action Setup Status reports `action` for `scene` (None = no action stored)."""
    try:
        status = V.decode_scene_action_status(params)
    except ValueError:
        return False
    return status.scenes is None and status.scene == scene and status.action == action


def _scene_register_status_name(status: int) -> str:
    return {
        0: "Success",
        M.SCENE_REGISTER_FULL: "Scene Register Full",
        M.SCENE_NOT_FOUND: "Scene Not Found",
    }.get(status, f"status 0x{status:02X}")
