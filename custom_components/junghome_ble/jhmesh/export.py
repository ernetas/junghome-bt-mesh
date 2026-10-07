"""Write the mesh CDB and the JUNG HOME project file (`ExportDto`) back to disk — roadmap step 0.4 / blocker 2.

The app imports the project file as a *full replace* and `DeleteUnusedScenes` removes any scene it does not know,
so everything HA changes on the mesh must land in the Nordic CDB JSON **and** the `meta` block
(`docs/gap-analysis/network-features.md` §8). `ProjectFile` keeps the parsed tree the `CDB` was built from
(`CDB.raw`; `Element.raw_models` are views into it), applies targeted mutations that mirror §8.3's "minimum
write-back" matrix, and serialises either flavour:

- `MeshNetwork.json` — the nRF-Mesh CDB with the iOS `{"meshNetwork": …}` wrapper, 4-hex-digit addresses;
- `JungHome.json` — `{"version", "appVersion", "platform", "meta", "network": "<Base64 CDB JSON>"}`.

The JSON layout (indent, `" : "` vs `": "`, compact inner payload) is sniffed from the loaded file so a rewrite
diffs minimally against the app's own output; new entries copy the key order of their siblings. Writes are atomic
(temp file + replace) and keep the previous contents in `fileio.BACKUP_GENERATIONS` rotating backups (`.bak` the
newest, then `.bak.1`, …); the file we loaded is never overwritten once its bytes changed, nor any other file whose
CDB `timestamp` is newer than ours (`NewerExportError`), unless forced.

Key material passes through untouched; nothing here logs it, and `repr()` / error messages never include it. The
files it writes hold every mesh key, so they are written by `fileio.atomic_write`: the temp file is created mode
0600 whatever the umask, and a backup copy is never more readable than 0600 either (a fetched / uploaded export is
stored 0600 by the config flow; a user-provided file keeps its own mode where that is stricter still).
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from . import vendor_models as V
from .cdb import (
    CDB,
    Element,
    InvalidExport,
    Node,
    canonical_uuid,
    is_virtual,
    parse_address,
)
from .devices import (
    BLIND_ONLY_PIDS,
    DEVICE_TYPE_GROUPS,
    GATEWAY_PID,
    GROUP_RANGE,
    LAMP_LEVEL_MODELS,
    SOCKET_PIDS,
    THERMOSTAT_PIDS,
    TIME_KEEPER_ADDRESS,
    TIME_KEEPER_GROUP,
    Metadata,
    as_int,
    element_group_address,
    is_room,
    load_kind,
    meta_list,
)
from .fileio import atomic_write

__all__ = [
    "ACTUATOR_2G_DIMMING",
    "ACTUATOR_2G_SWITCH",
    "ACTUATOR_BLIND",
    "ACTUATOR_DIMMING",
    "ACTUATOR_GATEWAY",
    "ACTUATOR_NOT_AVAILABLE",
    "ACTUATOR_RTR",
    "ACTUATOR_SWITCH",
    "ACTUATOR_TW_DIMMING",
    "ALLOCATION_MARGIN",
    "DEFAULT_APP_VERSION",
    "DEFAULT_GROUP_ICON",
    "DEFAULT_PLATFORM",
    "DEFAULT_SCENE_ICON",
    "DEFAULT_VERSION",
    "FUNCTION_KEY_MODE",
    "GROUP_FUNCTIONS",
    "INSERT_GENERIC",
    "INSERT_NONE",
    "KEY_MODE_GATEWAY",
    "KEY_MODE_LIGHT",
    "KEY_MODE_MOVE",
    "KEY_MODE_PROPERTY",
    "KEY_MODE_RTR",
    "KEY_MODE_SCENE",
    "KEY_MODE_SERVERS",
    "KEY_MODE_SWITCH",
    "META_KEYS",
    "PROPERTY_ROW_KEYS",
    "PUSH_BUTTON_PIDS",
    "RENAME_MAX_LENGTH",
    "ROOM_MEMBER_MODELS",
    "SCENE_INFO_FIELDS",
    "SCENE_INFO_KELVIN",
    "SHARE_KEYS",
    "Allocation",
    "AllocationCrowded",
    "DeviceIdRow",
    "DeviceRow",
    "ExportError",
    "Flavour",
    "InvalidName",
    "Layout",
    "ModelChange",
    "NewerExportError",
    "ProjectFile",
    "RoomLink",
    "RoomLinkRow",
    "SceneConfigRow",
    "SceneLinkRow",
    "Style",
    "cdb_element_groups",
    "check_name",
    "function_code",
    "group_addresses_in_use",
    "guess_actuator_function",
    "guess_insert_type",
    "has_model",
    "hexaddr",
    "keeps_row",
    "location_ids",
    "mac_from_uuid",
    "meta_rows",
    "name_length",
    "names_device",
    "now_timestamp",
    "pick_free",
    "raw_model",
    "row_node",
    "scene_infos",
    "suffixed_name",
    "timestamp_advanced",
    "write_private",
    "write_private_with_backup",
]

Flavour = Literal["cdb", "share"]

DEFAULT_GROUP_ICON = (
    "ic_group_ground_plan"  # GroupIconKt default (network-features.md §1)
)
DEFAULT_SCENE_ICON = "SceneDay"  # first SceneIcon value (network-features.md §3)
DEFAULT_VERSION, DEFAULT_APP_VERSION, DEFAULT_PLATFORM = (
    "1.1",
    "2.2.0 (822956)",
    "Home Assistant",
)
META_KEYS = (  # MetaData.java field order (network-features.md §8.1)
    "userGroups",
    "elementConnectionGroups",
    "devices",
    "scenes",
    "sceneInfo",
    "schedulerMetaInfo",
    "timer",
    "actuatorExports",
    "buttonLayoutExports",
    "keyModeSceneConfigExports",
)
# GroupConnection.Function (network-logic.md §2.1). Both apps export the enum name (`"LIGHT"`; checked against a
# real iOS 2.2.0 share export); `function_code` accepts the ordinal too, just in case.
GROUP_FUNCTIONS = {
    "LIGHT": 0,
    "LIGHT_PROPERTY_MODE": 1,
    "BLIND": 2,
    "BLIND_PROPERTY_MODE": 3,
    "SWITCH": 4,
    "SWITCH_PROPERTY_MODE": 5,
    "LIGHT_AND_SWITCH": 6,
    "LIGHT_AND_SWITCH_PROPERTY_MODE": 7,
    "RTR_PROPERTY_MODE": 8,
}
KEY_MODE_LIGHT, KEY_MODE_MOVE, KEY_MODE_SCENE, KEY_MODE_PROPERTY = 0, 1, 2, 3
KEY_MODE_RTR, KEY_MODE_SWITCH, KEY_MODE_GATEWAY = 4, 5, 6
FUNCTION_KEY_MODE = {  # domain/item/a.java:10-29
    0: KEY_MODE_LIGHT,
    6: KEY_MODE_LIGHT,
    1: KEY_MODE_PROPERTY,
    3: KEY_MODE_PROPERTY,
    5: KEY_MODE_PROPERTY,
    7: KEY_MODE_PROPERTY,
    8: KEY_MODE_PROPERTY,
    2: KEY_MODE_MOVE,
    4: KEY_MODE_SWITCH,
}
KEY_MODE_SERVERS = {  # server models on the target element per key mode (network-logic.md §2.1)
    KEY_MODE_LIGHT: ("1000", "1002"),
    KEY_MODE_MOVE: ("1002",),
    KEY_MODE_SCENE: ("1203",),
    KEY_MODE_PROPERTY: ("05271013",),
    KEY_MODE_RTR: ("1002",),
    KEY_MODE_SWITCH: ("1000",),
    KEY_MODE_GATEWAY: ("05271013",),
}
ROOM_MEMBER_MODELS = (
    "1000",
    "1002",
)  # AddGroupToDevices: OnOff / Level servers of the first element
PUSH_BUTTON_PIDS = {0x0001, 0x0002}
ACTUATOR_SWITCH, ACTUATOR_2G_SWITCH, ACTUATOR_DIMMING, ACTUATOR_2G_DIMMING = 0, 1, 2, 3
ACTUATOR_TW_DIMMING, ACTUATOR_BLIND, ACTUATOR_NOT_AVAILABLE = 4, 5, 7
ACTUATOR_RTR, ACTUATOR_GATEWAY = 8, 9
INSERT_NONE, INSERT_GENERIC = 1, 2
# the `infos` of a `meta.sceneInfo` row, in the order of `Infos`' fields (`domain/dto/Infos.java`; Gson writes them
# so and leaves a null one out), and the colour temperatures the app's export can hold (`p097i9/d.java`)
SCENE_INFO_FIELDS = (
    "blindPosition",
    "slatPosition",
    "lightness",
    "colorTemperature",
    "temperatureValue",
)
SCENE_INFO_KELVIN = (2000, 10000)
# the app's per-element caches of a node's InsertId and ButtonLayout (`MeshPropertyExport`), one row per element
PROPERTY_ROW_KEYS = ("actuatorExports", "buttonLayoutExports")

_EUI64 = re.compile(
    r"^([0-9A-F]{2})([0-9A-F]{2})([0-9A-F]{2})FF-FE([0-9A-F]{2})-([0-9A-F]{2})([0-9A-F]{2})-0000-000000000000$"
)


class ExportError(Exception):
    """A project-file operation was refused; the message never carries key material."""


# How Home Assistant picks a new room, scene or element group. "app": the lowest free number of the
# provisioner's range, the app's own rule (the parity tests, and the CLI where it mirrors the app). "top": the
# highest free one. The app never downloads the project (`docs/android/network-logic.md` §6.1): without a provisioner
# of Home Assistant's own, its database does not know what Home Assistant added, and its next allocation is the
# lowest number it believes free — the one an "app" pick just took.
Allocation = Literal["app", "top"]
# the free numbers a "top" pick must keep below it: that many more rooms (or scenes) of the app's own, and the app's
# lowest-free counter would reach it
ALLOCATION_MARGIN = 64


class AllocationCrowded(ExportError):
    """A top-down pick lies within `ALLOCATION_MARGIN` free numbers of the app's own allocations.

    The range is no longer sparse: the app, handing out the lowest free number first, would take the pick within a
    few more allocations of its own. `what` names the kind ("group address", "scene number").
    """

    def __init__(self, what: str, pick: int, below: int) -> None:
        """Say which pick was refused and how many free numbers were left below it."""
        super().__init__(
            f"only {below} free {what}(s) left below {pick:04X}: the app's next ones would reach it"
        )
        self.what, self.pick, self.below = what, pick, below


def pick_free(
    low: int, high: int, used: set[int], policy: Allocation, what: str
) -> int | None:
    """Return a number of `low..high` not in `used`: the lowest ("app") or the highest ("top"); None when none is.

    A "top" pick with fewer than `ALLOCATION_MARGIN` free numbers below it is refused (`AllocationCrowded`): the
    app allocates from the bottom, and that many more of its own would reach it. Counted as free numbers, not as a
    distance from the app's highest one: a hole left by a removed room (the app's, or one of Home Assistant's near
    the top) is taken first by whoever allocates from its side, so the count is how many the app has left.
    """
    order = range(high, low - 1, -1) if policy == "top" else range(low, high + 1)
    pick = next((n for n in order if n not in used), None)
    if pick is None or policy == "app":
        return pick
    below = sum(1 for n in range(low, pick) if n not in used)
    if below < ALLOCATION_MARGIN:
        raise AllocationCrowded(what, pick, below)
    return pick


# the longest name the app's rename sheet takes (`fragment_name_config.xml`: `app:maxLength="30"` on its name field,
# an `InputFilter.LengthFilter` — Java chars, UTF-16 code units); it renames devices, rooms and scenes alike
RENAME_MAX_LENGTH = 30

_NAME_ERRORS = {
    "blank": "a name cannot be blank",
    "not_allowed": "a name cannot hold a lone '%' (only '%%' and '%n' pass)",
    "too_long": f"a new name cannot be longer than {RENAME_MAX_LENGTH} characters",
}


class InvalidName(ValueError):
    """A name the app refuses whatever the other names are: `reason` is `blank`, `not_allowed` or `too_long`."""

    def __init__(self, reason: Literal["blank", "not_allowed", "too_long"]) -> None:
        """Say why: `blank` (the app's `Blank`), `not_allowed` (`NotAllowed`, a name Java cannot format), `too_long`.

        `too_long`: a rename past the rename sheet's `RENAME_MAX_LENGTH`, which the app cannot type.
        """
        super().__init__(_NAME_ERRORS[reason])
        self.reason = reason


# `java.util.Formatter`'s format specifier, `%[index$][flags][width][.precision][t]conversion` (ASCII digits and
# letters, as Java's `\d` and `[a-zA-Z]` are)
_FORMAT_SPECIFIER = re.compile(
    r"%([0-9]+\$)?([-#+ 0,(<]*)([0-9]+)?(\.[0-9]+)?([tT])?([a-zA-Z%])"
)


def _formats_without_arguments(name: str) -> bool:
    """Whether Java's `String.format(name)` with no arguments returns (`CheckNameInput`'s `NotAllowed` test).

    Two conversions need no argument: `%%` (a literal `%`: no flag but `-`, which needs a width; no precision) and
    `%n` (a line break: no flag, width or precision). Every other one throws — a real conversion for its missing
    argument, an unknown letter or a `%` at the end as an unknown conversion — so a name with a lone `%` is refused.
    """
    pos = 0
    while (start := name.find("%", pos)) >= 0:
        spec = _FORMAT_SPECIFIER.match(name, start)
        if spec is None:
            return False
        _, flags, width, precision, date, conversion = spec.groups()
        if date or precision:
            return False
        if conversion == "%":
            if flags not in ("", "-") or (flags and width is None):
                return False
        elif conversion != "n" or flags or width:
            return False
        pos = spec.end()
    return True


def name_length(name: str) -> int:
    """Return the length of `name` as Java counts it (UTF-16 code units: a character past U+FFFF counts twice)."""
    return len(name.encode("utf-16-le")) // 2


def check_name(name: str | None, max_length: int | None = None) -> str:
    """Return `name` when the app's `CheckNameInput` accepts it before looking for duplicates; raise `InvalidName`.

    `Blank`: None, empty or only whitespace. `NotAllowed`: `String.format(name)` throws (`_formats_without_arguments`).
    The same check runs for a device, a room and a scene name (`CheckType` DEVICE / GROUP / SCENE); only the
    duplicate check that follows differs. `max_length` (a rename: `RENAME_MAX_LENGTH`) refuses a longer name as
    `too_long` — the app's rename sheet cannot take one; its create screens set no limit.
    """
    if name is None or not name.strip():
        raise InvalidName("blank")
    if not _formats_without_arguments(name):
        raise InvalidName("not_allowed")
    if max_length is not None and name_length(name) > max_length:
        raise InvalidName("too_long")
    return name


def _rename_limit(old: str | None, new: str) -> int | None:
    """Return the length limit for renaming `old` to `new`: none when the name stays, or it could not be kept."""
    return None if old == new else RENAME_MAX_LENGTH


def suffixed_name(name: str, names: Iterable[str]) -> str:
    """Return the app's automatic name for a device renamed to a `name` another device has (`countInstance`).

    `"<name> <n>"`, n = 1 + the device names equal to `name` + those whose part before the last space is (both
    ignoring case), over every device the project has, the renamed one with its old name included: a second
    "Lamp" becomes "Lamp 3", as in the app. The app does not check that result; here n counts on while another
    device already has it, so the rename never makes a duplicate.
    """
    everyone = list(names)
    wanted = name.lower()
    number = 1 + sum(
        (n.lower() == wanted) + (n.rsplit(" ", 1)[0].lower() == wanted)
        for n in everyone
    )
    taken = {n.lower() for n in everyone}
    while f"{wanted} {number}" in taken:
        number += 1
    return f"{name} {number}"


def write_private(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically (`fileio.atomic_write`), readable by the owner only: it holds every key."""
    atomic_write(path, data, private=True)


def write_private_with_backup(path: Path, data: bytes) -> None:
    """Atomically replace `path` with `data`, keeping the previous content as the newest backup (`fileio.backup_paths`).

    For a target that is the only on-disk record of what it held (an adopted gateway export).
    """
    atomic_write(path, data, private=True, backup=True)


class NewerExportError(ExportError):
    """The file on disk changed after we loaded ours (the app wrote it, or anything else did): re-import it first."""

    def __init__(self, path: Path, file_timestamp: str, loaded_timestamp: str) -> None:
        """Record which file, its CDB timestamp and the one we hold."""
        super().__init__(
            f"{path} was changed after we loaded it (CDB timestamp {file_timestamp}, ours {loaded_timestamp});"
            " re-import it before writing"
        )
        self.path = path
        self.file_timestamp = file_timestamp
        self.loaded_timestamp = loaded_timestamp


@dataclass(frozen=True)
class Layout:
    """How one JSON text is laid out: indent (None = compact) and the separators."""

    indent: int | None = 2
    key_sep: str = ": "  # Apple's JSONSerialization writes `" : "`, Gson-compact `":"`
    item_sep: str = ","  # only used when compact (`", "` for Python-style dumps)

    @classmethod
    def sniff(cls, text: str) -> Layout:
        """Derive the layout from the first key of a JSON text."""
        m = re.match(r'\s*\{\s*"(?:[^"\\]|\\.)*"(\s*:\s*)', text)
        key_sep = m.group(1) if m else ": "
        if "\n" in key_sep:
            key_sep = ": "
        indent_m = re.search(r'\n([ \t]+)"', text)
        if indent_m:
            return cls(len(indent_m.group(1)), key_sep, ",")
        item_m = re.search(r'(,[ \t]*)"', text)
        return cls(None, key_sep, item_m.group(1) if item_m else ",")

    def dumps(self, obj: Any) -> str:
        """Serialise like the app: UTF-8 as-is, no key sorting."""
        if self.indent is None:
            return json.dumps(
                obj, ensure_ascii=False, separators=(self.item_sep, self.key_sep)
            )
        return json.dumps(
            obj, ensure_ascii=False, indent=self.indent, separators=(",", self.key_sep)
        )


SHARE_KEYS = (  # ExportDto field order as the Android app writes it; iOS puts `network` first
    "version",
    "appVersion",
    "platform",
    "meta",
    "network",
)


@dataclass(frozen=True)
class Style:
    """Outer layout, the layout of a share export's `network` payload, whether it is wrapped, and the key order.

    `outer_keys` is the top-level key order of the loaded share export (`SHARE_KEYS` when nothing was loaded):
    Android writes `version, appVersion, platform, meta, network`, iOS `network, appVersion, version, platform,
    meta`, and a rewrite keeps whichever it read so the file stays byte-identical but for the change made.
    """

    outer: Layout = Layout()
    inner: Layout = Layout(
        None, ":", ","
    )  # Gson compact, as the Android app encodes `network`
    inner_wrapped: bool = (
        False  # `network` carries the iOS `{"meshNetwork": …}` wrapper
    )
    outer_keys: tuple[str, ...] = SHARE_KEYS

    @classmethod
    def sniff(
        cls, text: str, inner: str | None = None, keys: Iterable[str] = SHARE_KEYS
    ) -> Style:
        """Sniff the outer text and, for a share export, the decoded `network` payload and the key order."""
        if inner is None:
            return cls(Layout.sniff(text))
        return cls(
            Layout.sniff(text),
            Layout.sniff(inner),
            '"meshNetwork"' in inner[:64],
            tuple(keys),
        )


@dataclass(frozen=True)
class ModelChange:
    """One pub/sub edit a mutator made — what the next wave must also send on air (Config Model messages)."""

    element: int
    model: str
    address: int
    kind: Literal["subscribe", "unsubscribe", "publish"]


class DeviceIdRow(TypedDict, total=False):
    """A `meta.devices[]` row's `deviceId`: the node (`nodeId`, its UUID) and the element locations it covers."""

    actuatorFunctionId: int
    locationIds: list[int]
    insertType: int
    productId: int
    nodeId: str


class DeviceRow(TypedDict, total=False):
    """A `meta.devices[]` row: one app device — its name, MAC, `deviceId` and the room links of its keys.

    As read from a file, whatever the app wrote: `cachedGroupConnectionMetadata` may hold a stray `null` (`meta_rows`
    skips it), so it is a list of anything; its object rows are `RoomLinkRow`s.
    """

    name: str
    macAddress: str
    deviceId: DeviceIdRow
    cachedGroupConnectionMetadata: list[Any]


class RoomLinkRow(TypedDict, total=False):
    """A `cachedGroupConnectionMetadata` row (`KeyModeGroupConfig`): a key's room link.

    Addresses are ints as Gson writes them, or hex strings in a file that uses those; `function` is the
    `GroupFunction` name, or its ordinal (`function_code`).
    """

    elementAddress: int | str
    groupAddress: int | str
    publishAddress: int | str
    function: str | int


class SceneConfigRow(TypedDict, total=False):
    """A `keyModeSceneConfigExports` row's `sceneConfig` (`MeshPropertyExport.java:153-161`)."""

    transitionStepSeconds: int
    sceneId: int
    transitionResolution: int
    publicationAddress: int | str


class SceneLinkRow(TypedDict, total=False):
    """A `keyModeSceneConfigExports` row: the scene a key recalls, as the app shows it."""

    sceneConfig: SceneConfigRow
    elementAddress: int | str


@dataclass(frozen=True)
class RoomLink:
    """A key's room link as the app caches it (a `cachedGroupConnectionMetadata` row of `meta.devices[]`).

    `key` is the key element, `room` the room's group, `publish` the group the key publishes to, `function` the
    `GroupFunction` code (`function_code`); each is None when the row does not carry it as a number.
    """

    key: int | None
    room: int | None
    publish: int | None
    function: int | None

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> RoomLink:
        """Read a `cachedGroupConnectionMetadata` row."""
        return cls(
            as_int(row.get("elementAddress")),
            as_int(row.get("groupAddress")),
            as_int(row.get("publishAddress")),
            function_code(row.get("function")),
        )


def hexaddr(addr: int) -> str:
    """4-hex-digit address string as the CDB writes it."""
    return f"{addr:04X}"


def now_timestamp(now: datetime | None = None) -> str:
    """CDB `timestamp` in the iOS form `2026-01-01T00:00:00Z`."""
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_timestamp(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def timestamp_advanced(candidate: str, reference: str) -> bool:
    """Tell whether CDB timestamp `candidate` is later than `reference` (unparseable ones count when different)."""
    a, b = _parse_timestamp(candidate), _parse_timestamp(reference)
    if a is None or b is None:
        return candidate != reference
    if (a.tzinfo is None) != (b.tzinfo is None):
        a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
    return a > b


def mac_from_uuid(uuid: str) -> str:
    """MAC of a JUNG node from its EUI-64 UUID (`30FB10FF-FE12-3456-0000-…` -> `30:FB:10:12:34:56`), else ''.

    The `meta.devices[].macAddress` form: a string, empty for a node that is not a JUNG device (the provisioning
    phone). `advert.mac_from_uuid` answers the same question for the scanner with a looser contract (`None`
    instead of '', and it does not insist on the trailing zero half of the EUI-64 form) — kept separate on
    purpose: this one decides what is *written* into the app's file, that one what is *matched* on air.
    """
    m = _EUI64.match(uuid.upper())
    return ":".join(m.groups()) if m else ""


def guess_actuator_function(node: Node) -> int:  # noqa: PLR0911  # one branch per device kind
    """Guess the InsertId actuatorFunctionId the app would have cached, from the composition (properties.md §2.1)."""
    if node.pid == GATEWAY_PID:
        return ACTUATOR_GATEWAY
    if node.pid in SOCKET_PIDS:
        return ACTUATOR_SWITCH
    if node.pid in THERMOSTAT_PIDS:
        return ACTUATOR_RTR
    loads = [e for e in node.elements if e.location in (0x0001, 0x0002)]
    if node.pid in BLIND_ONLY_PIDS or any(
        "1002" in e.models and not LAMP_LEVEL_MODELS & set(e.models) for e in loads
    ):
        return ACTUATOR_BLIND
    if any("1303" in e.models for e in loads):
        return ACTUATOR_TW_DIMMING
    dimmers = [e for e in loads if "1300" in e.models]
    if dimmers:
        return ACTUATOR_2G_DIMMING if len(dimmers) > 1 else ACTUATOR_DIMMING
    switches = [e for e in loads if "1000" in e.models]
    if switches:
        return ACTUATOR_2G_SWITCH if len(switches) > 1 else ACTUATOR_SWITCH
    return ACTUATOR_NOT_AVAILABLE


def guess_insert_type(node: Node) -> int:
    """2 GenericInsert for push-buttons, 1 NoInsert for sockets / mini actuators / the gateway."""
    return INSERT_GENERIC if node.pid in PUSH_BUTTON_PIDS else INSERT_NONE


def scene_infos(action: V.Action) -> dict[str, int | float]:
    """Return the `infos` of the `meta.sceneInfo` row of a load stored in a scene with `action`.

    The values the app captures from a device as it stores it (`M7/i.java`), converted as its export does
    (`SceneInfoRepositoryImpl.java`): lightness in percent, colour temperature in Kelvin (100 K steps, 2000..10000),
    a blind's and its slats' position in the JUNG percent (0 open .. 100 closed, as the cover converts a level),
    a target temperature in °C. A switch insert or a socket holds no lightness in the app; its on / off goes in
    as lightness 100 / 0, the one value the app's import turns back into on / off (`lightness > 0`). An action
    without values (none, an unknown code) gives no field.
    """
    out: dict[str, int | float] = {}
    if action.code == V.ACTION_SWITCH:
        out["lightness"] = 100 if action.on else 0
    elif action.code in (V.ACTION_LIGHTNESS, V.ACTION_LIGHTNESS_CT):
        out["lightness"] = round((action.lightness or 0) * 100 / V.LIGHTNESS_MAX)
        if action.code == V.ACTION_LIGHTNESS_CT and action.temperature_k is not None:
            low, high = SCENE_INFO_KELVIN
            out["colorTemperature"] = max(
                low, min(high, round(action.temperature_k / 100) * 100)
            )
    elif action.code == V.ACTION_BLINDS:
        out["blindPosition"] = _jung_percent(action.blind)
        out["slatPosition"] = _jung_percent(action.slat)
    elif action.code == V.ACTION_TEMPERATURE and action.temperature_c is not None:
        out["temperatureValue"] = float(action.temperature_c)
    return out


def _jung_percent(level: int | None) -> int:
    """Return the JUNG percent (0 open .. 100 closed) of a Generic Level, rounded as `cover.level_to_closedness`."""
    span = V.LEVEL_MAX - V.LEVEL_MIN
    return max(0, min(100, round(((level or 0) - V.LEVEL_MIN) * 100 / span)))


def row_node(row: Any) -> str | None:
    """Return the canonical node UUID a `meta` row's `deviceId` names; None for a row without one."""
    did = row.get("deviceId") if isinstance(row, dict) else None
    if not isinstance(did, dict) or did.get("nodeId") is None:
        return None
    return canonical_uuid(str(did["nodeId"]))


def names_device(row: Any, node: Node, location: int) -> bool:
    """Whether a `meta` row's `deviceId` is the app device of `node` that has `location` (one of its `locationIds`)."""
    return row_node(row) == node.uuid and location in (
        location_ids(row["deviceId"].get("locationIds")) or []
    )


def _ordered_like(template: Any, entry: dict[str, Any]) -> dict[str, Any]:
    """Reorder `entry`'s keys like `template` (an existing sibling) so the new line matches the app's layout."""
    if not isinstance(template, dict):
        return entry
    out = {k: entry[k] for k in template if k in entry}
    out.update({k: v for k, v in entry.items() if k not in out})
    return out


def _addr_like(template: Any, addr: int) -> int | str:
    """Addresses in `meta` are ints (Gson); mirror a hex string when the loaded file uses one."""
    return hexaddr(addr) if isinstance(template, str) else addr


def function_code(value: Any) -> int | None:
    """Return a room link's `function` as its code: from the enum name (any case), or the ordinal itself."""
    if isinstance(value, str):
        return GROUP_FUNCTIONS.get(value.upper())
    return as_int(value)


def _first(items: list[Any]) -> Any:
    return items[0] if items else None


def meta_rows(items: list[Any]) -> list[dict[str, Any]]:
    """Return the object entries of a `meta` list; a stray `null` / scalar entry is left where it is and ignored."""
    return [r for r in items if isinstance(r, dict)]


def keeps_row(row: Any, key: str, value: int) -> bool:
    """Filter for the `meta` rewrites: an object row whose `key` is `value` goes, anything else stays."""
    return not isinstance(row, dict) or as_int(row.get(key)) != value


def location_ids(value: Any) -> list[int] | None:
    """Sorted `locationIds` of a `meta` device id; None when the list is not one of numbers."""
    try:
        return sorted(int(x) for x in meta_list(value))
    except (ValueError, TypeError):
        return None


def has_model(element: Element, model: str) -> bool:
    """Whether the element hosts `model` (ids compared case-insensitively)."""
    return any(m.upper() == model.upper() for m in element.models)


def raw_model(element: Element, model: str) -> dict[str, Any]:
    """Return the element's CDB model entry for `model`; KeyError when it has none."""
    entry = element.model_entry(model)
    if entry is None:
        raise KeyError(f"element {element.address:04X} has no model {model}")
    return entry


def group_addresses_in_use(cdb: CDB, meta: dict[str, Any] | None) -> set[int]:
    """Every address an export (`cdb` and its `meta` block) uses as a group anywhere, listed in `groups[]` or not.

    The CDB `groups[]`, the `meta` rooms, element groups and room links, and every model's publication and
    subscriptions: a group the app keeps only in `meta`, or one a node still listens to after its entry was
    dropped (by hand, or by another tool), is not free — a new room or element group there would inherit its
    listeners. Rooms (`ProjectFile.free_group_address`) and a new node's element groups (`commission.plan`) are
    both allocated around this set.
    """
    meta = meta or {}
    used = set(cdb.groups)
    rows = [
        *((r, "address") for r in meta_rows(meta_list(meta.get("userGroups")))),
        *(
            (r, "groupAddress")
            for r in meta_rows(meta_list(meta.get("elementConnectionGroups")))
        ),
        *(
            (r, key)
            for dev in meta_rows(meta_list(meta.get("devices")))
            for r in meta_rows(meta_list(dev.get("cachedGroupConnectionMetadata")))
            for key in ("groupAddress", "publishAddress")
        ),
    ]
    used.update(a for r, key in rows if (a := as_int(r.get(key))) is not None)
    for node in cdb.nodes:
        for el in node.elements:
            for m in el.raw_models:
                used.update(el.subscriptions(m["modelId"]))
                if pub := m.get("publish"):
                    used.add(parse_address(str(pub.get("address", "0000"))))
    return used


def cdb_element_groups(cdb: CDB, meta: dict[str, Any] | None) -> dict[int, int]:
    """`element_groups` of a loaded export (the hub's CDB and its `meta`)."""
    out: dict[int, int] = {}
    for addr, name in cdb.groups.items():
        element = element_group_address(name)
        if element is not None:
            out.setdefault(element, addr)
    for row in meta_rows(meta_list((meta or {}).get("elementConnectionGroups"))):
        element, group = (
            as_int(row.get("elementAddress")),
            as_int(row.get("groupAddress")),
        )
        if element is not None and group is not None and group in cdb.groups:
            out.setdefault(element, group)
    return out


class ProjectFile:
    """The CDB plus the app's `meta` block, editable and serialisable in both export flavours."""

    def __init__(
        self,
        cdb: CDB,
        meta: dict[str, Any] | None = None,
        *,
        path: Path | None = None,
        flavour: Flavour = "cdb",
        header: dict[str, Any] | None = None,
        style: Style | None = None,
        digest: str | None = None,
        metadata: Metadata | None = None,
    ) -> None:
        """Wrap a CDB built by `CDB.load` / `CDB.from_network` (it must carry its parsed tree).

        Without `meta` (a raw `MeshNetwork.json`) a complete `meta` block is synthesised from the CDB and, when
        given, the app-container `metadata` (device / scene names). A loaded `meta` is kept as-is.
        """
        if cdb.raw is None:
            raise ExportError(
                "the CDB carries no parsed tree; load it with CDB.load / CDB.from_network"
            )
        self.cdb = cdb
        self.net: dict[str, Any] = cdb.raw
        self.path = Path(path) if path is not None else None
        self.flavour: Flavour = flavour
        self.header = dict(header or {})
        self.style = style or Style()
        self.loaded_timestamp = str(self.net.get("timestamp", ""))
        self.digest = digest
        # UUID of Home Assistant's own provisioner (`vault.Vault.merge_into` sets it): groups and
        # scenes are then allocated in its ranges. None (the default): in the first provisioner's, as the app does
        self.own_provisioner: str | None = None
        # where a new room or scene goes in the first provisioner's range (`Allocation`); in Home Assistant's own
        # ranges always the lowest free number — nobody else allocates there. Home Assistant sets "top"
        self.allocation: Allocation = "app"
        if meta is None:
            self.meta: dict[str, Any] = {k: [] for k in META_KEYS}
            self.complete_meta(metadata)
        else:
            self.meta = meta
            if metadata is not None:
                self.complete_meta(metadata)

    def __repr__(self) -> str:
        """Identify the file without any key material."""
        return (
            f"ProjectFile({self.path}, flavour={self.flavour!r}, mesh={self.cdb.mesh_uuid},"
            f" timestamp={self.loaded_timestamp!r}, nodes={len(self.cdb.nodes)},"
            f" groups={len(self.cdb.groups)}, scenes={len(self.cdb.scenes)})"
        )

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: Path, metadata: Metadata | None = None) -> ProjectFile:
        """Read either flavour; `metadata` (the iOS app-container names) seeds / overlays the `meta` block."""
        path = Path(path)
        return cls.loads(path.read_bytes(), metadata, path=path)

    @classmethod
    def loads(
        cls, data: bytes, metadata: Metadata | None = None, *, path: Path | None = None
    ) -> ProjectFile:
        """Parse either flavour from its bytes (`load` for a file); `path` is where it is saved to by default."""
        text = data.decode("utf-8")
        net, meta, doc, inner = CDB.parse_document(
            text
        )  # `InvalidExport` names what is wrong
        flavour: Flavour = "cdb" if inner is None else "share"
        header: dict[str, Any] = (
            {}
            if inner is None
            else {k: v for k, v in doc.items() if k not in ("meta", "network")}
        )
        return cls(
            CDB.from_network(net, meta),
            meta,
            path=path,
            flavour=flavour,
            header=header,
            style=Style.sniff(text, inner, doc if inner is not None else SHARE_KEYS),
            digest=hashlib.sha256(data).hexdigest(),
            metadata=metadata,
        )

    # ------------------------------------------------------------------ meta block
    def _meta(self, key: str) -> list[Any]:
        """Return the `meta` list under `key`, created when absent; a `null` (or otherwise non-list) value reads as empty.

        The apps may write `null` for a list (seen in an Android export); replacing it with `[]` on first use is
        what the app reads it as, and it keeps the loaded block byte-identical until something is written.
        """
        rows = self.meta.get(key)
        if not isinstance(rows, list):
            rows = self.meta[key] = []
        return rows

    def complete_meta(self, metadata: Metadata | None = None) -> None:
        """Bring `meta` in line with the CDB (rooms, element groups, scenes) and overlay app-container names."""
        self._sync_user_groups()
        self._sync_element_groups()
        self._sync_scenes(metadata)
        if metadata is None:
            return
        for uuid, entries in metadata.devices.items():
            node = self._node_by_uuid(uuid)
            if node is None:
                continue  # stale app metadata for a node no longer in the CDB
            for locations, name in entries:
                self.set_device_name(node, locations, name)

    def _sync_user_groups(self) -> None:
        groups = self._meta("userGroups")
        template = _first(meta_rows(groups))
        by_addr = {as_int(g.get("address")): g for g in meta_rows(groups)}
        for addr, name in self.cdb.groups.items():
            if not is_room(addr, name):
                continue
            entry = by_addr.get(addr)
            if entry is None:
                groups.append(
                    _ordered_like(
                        template,
                        {
                            "name": name,
                            "address": _addr_like(
                                template and template.get("address"), addr
                            ),
                            "icon": DEFAULT_GROUP_ICON,
                        },
                    )
                )
            else:
                entry["name"] = name

    def _sync_element_groups(self) -> None:
        ecg = self._meta("elementConnectionGroups")
        template = _first(meta_rows(ecg))
        known = {as_int(e.get("groupAddress")) for e in meta_rows(ecg)}
        for addr, name in self.cdb.groups.items():
            element = element_group_address(name)
            if element is None or addr in known:
                continue
            ecg.append(
                _ordered_like(
                    template,
                    {
                        "elementAddress": _addr_like(
                            template and template.get("elementAddress"), element
                        ),
                        "groupAddress": _addr_like(
                            template and template.get("groupAddress"), addr
                        ),
                    },
                )
            )

    def _sync_scenes(self, metadata: Metadata | None) -> None:
        scenes = self._meta("scenes")
        template = _first(meta_rows(scenes))
        by_num = {as_int(s.get("number")): s for s in meta_rows(scenes)}
        for num in self.cdb.scenes:
            entry = by_num.get(num)
            name = (
                (metadata.scenes.get(num) if metadata else None)
                or (entry.get("name") if entry else None)
                or self.cdb.scene_names.get(num)
                or f"Scene {num}"
            )
            if entry is None:
                scenes.append(
                    _ordered_like(
                        template,
                        {"name": name, "number": num, "icon": DEFAULT_SCENE_ICON},
                    )
                )
            else:
                entry["name"] = name

    def _device_entry(self, node: Node, locations: list[int]) -> dict[str, Any] | None:
        for dev in meta_rows(self._meta("devices")):
            did = dev.get("deviceId")
            if not isinstance(did, dict):
                continue
            if canonical_uuid(str(did.get("nodeId", ""))) != node.uuid:
                continue
            if location_ids(did.get("locationIds")) == locations:
                return dev
        return None

    def _node_by_uuid(self, uuid: str) -> Node | None:
        wanted = canonical_uuid(uuid)  # `Node.uuid` is canonical already
        return next((n for n in self.cdb.nodes if n.uuid == wanted), None)

    # ------------------------------------------------------------------ lookups
    def _node(self, node: Node | int | str) -> Node:
        if isinstance(node, Node):
            return node
        found = (
            self._node_by_uuid(node)
            if isinstance(node, str)
            else next((n for n in self.cdb.nodes if n.unicast == node), None)
        )
        if found is None:
            raise KeyError(f"no node {node!r} in the CDB")
        return found

    def _element(self, node: Node | int | str, element: Element | int) -> Element:
        if isinstance(element, Element):
            return element
        n = self._node(node)
        if not 0 <= element < len(n.elements):
            raise KeyError(f"node {n.unicast:04X} has no element index {element}")
        return n.elements[element]

    def _element_at(self, element: Element | int) -> Element:
        if isinstance(element, Element):
            return element
        found = self.cdb.element(element)
        if found is None:
            raise KeyError(f"no element {element:04X} in the CDB")
        return found

    @staticmethod
    def _model(element: Element, model: str) -> dict[str, Any]:
        return raw_model(element, model)

    @staticmethod
    def _has_model(element: Element, model: str) -> bool:
        return has_model(element, model)

    def _group_entry(self, address: int) -> dict[str, Any]:
        for g in self.net["groups"]:
            if parse_address(g["address"]) == address:
                return g  # type: ignore[no-any-return]
        raise KeyError(f"no group {address:04X} in the CDB")

    def _address_text(self, address: int) -> str:
        """Render an address as the CDB writes it: 4 hex digits, or the Label UUID of a virtual address the file knows.

        A virtual address whose label the file does not carry cannot be written (the label *is* the address on
        the wire, §3.4.2.3), and nothing here can send to one anyway.
        """
        if is_virtual(address):
            label = self.cdb.virtual_labels.get(address)
            if label is None:
                raise ValueError(f"{address:04X} is a virtual address without a label")
            return label.hex().upper()
        return hexaddr(address)

    def _scene_entry(self, number: int) -> dict[str, Any]:
        for s in self.net.get("scenes", []):
            if int(s["number"], 16) == number:
                return s  # type: ignore[no-any-return]
        raise KeyError(f"no scene {number} in the CDB")

    def _provisioner_range(self, kind: str) -> tuple[int, int] | None:
        """First allocated group / scene range of the first provisioner that has one (the app allocates there).

        Read from the CDB's validated ranges (in file order), not the raw tree: a malformed range is an
        `InvalidExport` at load time, never a `KeyError` from a service call. With `own_provisioner` set and in the
        file, its first range of the kind instead (Home Assistant allocates in a range of its own).
        """
        mine = self.cdb.own_provisioner(self.own_provisioner)
        own = None if mine is None else (mine.group if kind == "Group" else mine.scene)
        if own:
            return own[0]
        ranges = (
            self.cdb.provisioner_group_ranges
            if kind == "Group"
            else self.cdb.provisioner_scene_ranges
        )
        return ranges[0] if ranges else None

    def user_groups(self) -> dict[int, str]:
        """Rooms by address (element / device-type / time-keeper groups filtered out)."""
        return {a: n for a, n in self.cdb.groups.items() if is_room(a, n)}

    def used_group_addresses(self) -> set[int]:
        """Every address the export uses as a group anywhere, whether or not the CDB lists the group."""
        return group_addresses_in_use(self.cdb, self.meta)

    def _policy(self, kind: str, policy: Allocation | None) -> Allocation:
        """`policy`, or else: the lowest free number in Home Assistant's own range, `allocation` in the app's."""
        if policy is not None:
            return policy
        mine = self.cdb.own_provisioner(self.own_provisioner)
        if mine is not None and (mine.group if kind == "Group" else mine.scene):
            return "app"
        return self.allocation

    def free_group_address(
        self, *, policy: Allocation | None = None, avoid: Iterable[int] = ()
    ) -> int:
        """Return a free address of the provisioner's group range, by `policy` (default `allocation`).

        "app": the lowest, the app's `nextAvailableGroupAddress`; "top": the highest below the device-type groups.
        Free is not merely absent from the CDB's `groups[]`: nothing in the export may use it (`used_group_addresses`),
        nor any of `avoid`. `AllocationCrowded` when a "top" pick comes too close to the app's (`pick_free`).
        """
        low, high = self._provisioner_range("Group") or (
            GROUP_RANGE[0],
            DEVICE_TYPE_GROUPS[0] - 1,
        )
        # never the reserved device-type / time-keeper groups a provisioner's range may reach into
        low, high = max(low, GROUP_RANGE[0]), min(high, DEVICE_TYPE_GROUPS[0] - 1)
        used = self.used_group_addresses() | set(avoid)
        address = pick_free(
            low, high, used, self._policy("Group", policy), "group address"
        )
        if address is None:
            raise ExportError("no free group address in the provisioner's range")
        return address

    def key_scene_numbers(self) -> set[int]:
        """Scene numbers keys recall by the app's record (`keyModeSceneConfigExports`), listed as scenes or not.

        A removed scene leaves its key rows behind (the keys still send its number): a new scene there would be
        recalled by an old key.
        """
        out: set[int] = set()
        for row in meta_rows(self._meta("keyModeSceneConfigExports")):
            config = row.get("sceneConfig")
            if isinstance(config, dict) and (n := as_int(config.get("sceneId"))):
                out.add(n)
        return out

    def free_scene_number(
        self, *, policy: Allocation | None = None, avoid: Iterable[int] = ()
    ) -> int:
        """Return a free scene number of the provisioner's scene range, by `policy` (default `allocation`).

        "app": the lowest, the app's `createScene`; "top": the highest. Free: no scene, no key recalling it (`key_scene_numbers`), none of `avoid`. `AllocationCrowded` when a
        "top" pick comes too close to the app's (`pick_free`).
        """
        low, high = self._provisioner_range("Scene") or (1, 0xFFFF)
        low = max(low, 1)  # scene number 0 is prohibited (Mesh Model §5.1.3.1)
        used = set(self.cdb.scenes) | self.key_scene_numbers() | set(avoid)
        number = pick_free(
            low, high, used, self._policy("Scene", policy), "scene number"
        )
        if number is None:
            raise ExportError("no free scene number in the provisioner's range")
        return number

    # ------------------------------------------------------------------ pub/sub mutators (§8.3 rows 2-4)
    def set_subscriptions(
        self,
        node: Node | int | str,
        element: Element | int,
        model: str,
        groups: Iterable[int],
    ) -> list[ModelChange]:
        """Replace the `subscribe[]` list of one model; returns the adds / removes to mirror on air.

        An address that stays is written as the file wrote it: a virtual one whose Label UUID the file does not
        carry cannot be rendered anew, and must not stop an edit of another address of the list.
        """
        el = self._element(node, element)
        m = self._model(el, model)
        before = el.subscriptions(m["modelId"])
        written = {parse_address(a): a for a in m.get("subscribe", [])}
        wanted = list(dict.fromkeys(groups))
        m["subscribe"] = [written.get(g) or self._address_text(g) for g in wanted]
        return [
            ModelChange(el.address, m["modelId"], g, "unsubscribe")
            for g in before
            if g not in wanted
        ] + [
            ModelChange(el.address, m["modelId"], g, "subscribe")
            for g in wanted
            if g not in before
        ]

    def subscribe(
        self, element: Element | int, model: str, group: int
    ) -> list[ModelChange]:
        """Add one subscription (no-op when present)."""
        el = self._element_at(element)
        current = el.subscriptions(self._model(el, model)["modelId"])
        if group in current:
            return []
        return self.set_subscriptions(el.node, el, model, [*current, group])

    def unsubscribe(
        self, element: Element | int, model: str, group: int
    ) -> list[ModelChange]:
        """Drop one subscription (no-op when absent)."""
        el = self._element_at(element)
        current = el.subscriptions(self._model(el, model)["modelId"])
        if group not in current:
            return []
        return self.set_subscriptions(
            el.node, el, model, [g for g in current if g != group]
        )

    def set_publication(
        self,
        node: Node | int | str,
        element: Element | int,
        model: str,
        address: int | None,
        *,
        ttl: int = 0xFF,
        app_key_index: int = 0,
        credentials: int = 0,
        period_steps: int = 0,
        period_resolution: int = 100,
        retransmit_count: int = 0,
        retransmit_interval: int = 50,
    ) -> list[ModelChange]:
        """Set (or clear with `None`) the model's `publish` entry with the app's defaults (TTL 0xFF, no period)."""
        el = self._element(node, element)
        m = self._model(el, model)
        if address is None:
            m.pop("publish", None)
            return [ModelChange(el.address, m["modelId"], 0, "publish")]
        text = self._address_text(
            address
        )  # before `publish` is touched: a label-less virtual address raises
        m["publish"] = {
            "address": text,
            "index": app_key_index,
            "ttl": ttl,
            "credentials": credentials,
            "retransmit": {"count": retransmit_count, "interval": retransmit_interval},
            "period": {"numberOfSteps": period_steps, "resolution": period_resolution},
        }
        return [ModelChange(el.address, m["modelId"], address, "publish")]

    def publication(self, element: Element | int, model: str) -> int | None:
        """Publish address of a model, if it publishes (a Label UUID as its virtual address, 0x8xxx)."""
        pub = self._model(self._element_at(element), model).get("publish")
        return parse_address(str(pub.get("address", "0000"))) if pub else None

    # ------------------------------------------------------------------ rooms (§8.3 rows 1-2)
    def add_group(
        self,
        name: str,
        address: int | None = None,
        parent: int = 0,
        icon: str = DEFAULT_GROUP_ICON,
        avoid: Iterable[int] = (),
    ) -> int:
        """Create a room: CDB `groups[]` + `meta.userGroups[]`; the address from `free_group_address` unless given.

        `avoid`: addresses `free_group_address` must not pick either — groups nodes hold that the export does not
        show (a node provisioned but never recorded).
        """
        check_name(name)
        if any(n.lower() == name.lower() for n in self.user_groups().values()):
            raise ValueError(f"a room named {name!r} already exists")
        if address is None:
            address = self.free_group_address(avoid=avoid)
        elif not GROUP_RANGE[0] <= address < DEVICE_TYPE_GROUPS[0]:
            raise ValueError(f"{address:04X} is not a user group address")
        elif address in self.cdb.groups:
            raise ValueError(f"group {address:04X} already exists")
        groups = self.net["groups"]
        groups.append(
            _ordered_like(
                _first(groups),
                {
                    "address": hexaddr(address),
                    "name": name,
                    "parentAddress": hexaddr(parent),
                },
            )
        )
        self.cdb.groups[address] = name
        self._sync_user_groups()
        for g in meta_rows(self._meta("userGroups")):
            if as_int(g.get("address")) == address:
                g["icon"] = icon
        return address

    def ensure_time_keeper_group(self) -> bool:
        """Add the app's `#time_keeper_group#` (0xFEFF) to the CDB groups when the export lacks it; True when added.

        The app creates it locally before it points a time keeper's Time Server at it (network-logic.md §1.2, §6.2).
        """
        if TIME_KEEPER_ADDRESS in self.cdb.groups:
            return False
        groups = self.net.setdefault("groups", [])
        groups.append(
            _ordered_like(
                _first(groups),
                {
                    "address": hexaddr(TIME_KEEPER_ADDRESS),
                    "name": TIME_KEEPER_GROUP,
                    "parentAddress": "0000",
                },
            )
        )
        self.cdb.groups[TIME_KEEPER_ADDRESS] = TIME_KEEPER_GROUP
        return True

    def rename_group(self, address: int, name: str) -> None:
        """Rename a room in the CDB and in `meta.userGroups[]` (both must change, network-features.md §1).

        The app's rename sheet: `check_name` with its `RENAME_MAX_LENGTH`, unless the name stays as it is.
        """
        check_name(name, _rename_limit(self.cdb.groups.get(address), name))
        if any(
            a != address and n.lower() == name.lower()
            for a, n in self.user_groups().items()
        ):
            raise ValueError(f"a room named {name!r} already exists")
        self._group_entry(address)["name"] = name
        self.cdb.groups[address] = name
        self._sync_user_groups()

    def group_members(self, address: int) -> list[Element]:
        """Elements with any model subscribed to `address`."""
        return [
            e
            for n in self.cdb.nodes
            for e in n.elements
            if any(address in e.subscriptions(m["modelId"]) for m in e.raw_models)
        ]

    def remove_group(self, address: int) -> list[ModelChange]:
        """Delete a group: members unsubscribed, room-linked buttons unwired, CDB + `meta` entries dropped."""
        name = self._group_entry(address)["name"]
        changes: list[ModelChange] = []
        if is_room(address, name):
            for el in self.group_members(address):
                changes += self.set_room(el, address, member=False)
        for n in (
            self.cdb.nodes
        ):  # anything still pointing at the address (buttons, sensors)
            for el in n.elements:
                for m in el.raw_models:
                    if address in el.subscriptions(m["modelId"]):
                        changes += self.unsubscribe(el, m["modelId"], address)
                    if self.publication(el, m["modelId"]) == address:
                        changes += self.set_publication(n, el, m["modelId"], None)
        for dev in meta_rows(self._meta("devices")):
            cached = meta_list(dev.get("cachedGroupConnectionMetadata"))
            if cached:
                dev["cachedGroupConnectionMetadata"] = [
                    c for c in cached if keeps_row(c, "groupAddress", address)
                ]
        self.meta["userGroups"] = [
            g for g in self._meta("userGroups") if keeps_row(g, "address", address)
        ]
        self.meta["elementConnectionGroups"] = [
            e
            for e in self._meta("elementConnectionGroups")
            if keeps_row(e, "groupAddress", address)
        ]
        self.net["groups"] = [
            g for g in self.net["groups"] if parse_address(g["address"]) != address
        ]
        del self.cdb.groups[address]
        return changes

    def room_connections(self, group: int) -> list[RoomLinkRow]:
        """`cachedGroupConnectionMetadata` rows of every button linked to room `group`."""
        return [
            cast("RoomLinkRow", c)
            for dev in meta_rows(self._meta("devices"))
            for c in meta_rows(meta_list(dev.get("cachedGroupConnectionMetadata")))
            if as_int(c.get("groupAddress")) == group
        ]

    def room_links(self, group: int) -> list[RoomLink]:
        """Return the room links of every button linked to room `group` (`room_connections`, read)."""
        return [RoomLink.of(row) for row in self.room_connections(group)]

    def take_room_links(self, key: int) -> list[RoomLink]:
        """Remove the room-link rows of key element `key` and return them; a device without one is left as it is."""
        taken: list[RoomLink] = []
        for dev in meta_rows(meta_list(self.meta.get("devices"))):
            cached = meta_list(dev.get("cachedGroupConnectionMetadata"))
            links = [
                r for r in meta_rows(cached) if as_int(r.get("elementAddress")) == key
            ]
            if not links:
                continue
            taken += [RoomLink.of(r) for r in links]
            dev["cachedGroupConnectionMetadata"] = [r for r in cached if r not in links]
        return taken

    def drop_room_links(self, key: int) -> None:
        """Remove key element `key`'s room-link rows from every device, and its scene row (`drop_scene_link`)."""
        for dev in meta_rows(meta_list(self.meta.get("devices"))):
            dev["cachedGroupConnectionMetadata"] = [
                r
                for r in meta_list(dev.get("cachedGroupConnectionMetadata"))
                if keeps_row(r, "elementAddress", key)
            ]
        self.drop_scene_link(key)

    def add_room_link(
        self,
        entry: DeviceRow,
        key: int,
        room: int,
        publish: int,
        function: str,
        *,
        keep: bool = False,
    ) -> None:
        """Store the `KeyModeGroupConfig` row of a room link in the `meta.devices[]` entry `entry` covering the key.

        The row mirrors the file's own style (an existing row's field order, int vs hex-string addresses, enum name
        vs ordinal). It replaces the key's earlier rows, unless `keep`: then only an identical row goes.
        """
        template = next(
            (
                r
                for dev in meta_rows(meta_list(self.meta.get("devices")))
                for r in meta_rows(meta_list(dev.get("cachedGroupConnectionMetadata")))
            ),
            None,
        )
        row: dict[str, Any] = {
            "elementAddress": key,
            "groupAddress": room,
            "publishAddress": publish,
            "function": function,
        }
        if (
            template is not None
        ):  # mirror the file's own style: int vs hex-string addresses, enum name vs ordinal
            if isinstance(template.get("elementAddress"), str):
                row = {
                    k: hexaddr(v) if isinstance(v, int) else v for k, v in row.items()
                }
            if isinstance(template.get("function"), int):
                row["function"] = GROUP_FUNCTIONS[function]
            row = {k: row[k] for k in template if k in row} | row
        kept = [
            r
            for r in meta_list(entry.get("cachedGroupConnectionMetadata"))
            if (r != row if keep else keeps_row(r, "elementAddress", key))
        ]
        entry["cachedGroupConnectionMetadata"] = [*kept, row]

    def matches_function(self, element: Element, function: int | None) -> bool:
        """`SetGroupFunction` keeps only loads of the function's device class (network-logic.md §2.4).

        Classified by `devices.load_kind` — the same rules `build_devices` uses to tell a blinds-only product or
        an RTR from a lamp — rather than by which servers the element happens to host, which a blinds-only node
        with an OnOff server, or an RTR, would fail (both would pass for a light).
        """
        if function is None:
            return True
        kind = load_kind(element)
        if function in (0, 1):  # LIGHT*
            return kind == "light"
        if function in (4, 5):  # SWITCH*
            return kind == "socket"
        if function in (6, 7):  # LIGHT_AND_SWITCH*
            return kind in ("light", "socket")
        if function in (2, 3):  # BLIND*
            return kind == "blind"
        return kind == "thermostat"  # RTR_PROPERTY_MODE

    _matches_function = matches_function  # the name it had before `matches_function`

    def set_room(
        self, element: Element | int, group: int, *, member: bool = True
    ) -> list[ModelChange]:
        """Add (or remove) a load element to a room, as `AddGroupToDevices` / `DeleteGroupFromDevices` would.

        Membership = the element's OnOff / Level servers subscribe to the room (plus the server models of the
        key mode of every button already linked to the room); those buttons are then rewired
        (`reconnectSwitchesWithGroup`): the load's servers subscribe to each such button's publish group.
        Leaving drops the room address from *every* model that carries it (`DeleteGroupFromDevices` deletes
        "on the models that carry them", and app-provisioned loads have all their servers on the room).
        `meta` is untouched (§8.3: `cachedGroupConnectionMetadata` changes only when buttons are rewired). On
        leaving, the buttons' groups go before the room: an element that still carries the room is still a
        member, which is what makes a stopped leave complete on the next run. Returns the CDB edits, which the
        caller must mirror with Config Model Subscription Add / Delete.
        """
        el = self._element_at(element)
        if group not in self.cdb.groups or not is_room(group, self.cdb.groups[group]):
            raise KeyError(f"{group:04X} is not a room")
        links = self.room_connections(group)
        models = list(ROOM_MEMBER_MODELS)
        for link in links:
            key_mode = FUNCTION_KEY_MODE.get(
                function_code(link.get("function")) or 0, KEY_MODE_LIGHT
            )
            models += [m for m in KEY_MODE_SERVERS[key_mode] if m not in models]
        changes: list[ModelChange] = []
        if member:
            for model in models:
                if self._has_model(el, model):
                    changes += self.subscribe(el, model, group)
        for link in links:
            publish = as_int(link.get("publishAddress"))
            function = function_code(link.get("function"))
            if publish is None or (member and not self.matches_function(el, function)):
                continue
            key_mode = FUNCTION_KEY_MODE.get(function or 0, KEY_MODE_LIGHT)
            for model in KEY_MODE_SERVERS[key_mode]:
                if self._has_model(el, model):
                    changes += (
                        self.subscribe(el, model, publish)
                        if member
                        else self.unsubscribe(el, model, publish)
                    )
        if not member:
            # the room itself last: until it goes the element is still in the room, so a plan that stops
            # part-way is taken up by the next run — the buttons' groups included
            for m in el.raw_models:
                changes += self.unsubscribe(el, m["modelId"], group)
        return changes

    # ------------------------------------------------------------------ scenes (§8.3 row 5)
    def scene_names(self) -> dict[int, str]:
        """User-visible scene names: `meta.scenes[]` first, the CDB name otherwise."""
        names = dict(self.cdb.scene_names)
        for s in meta_rows(self._meta("scenes")):
            num = as_int(s.get("number"))
            if num is not None and s.get("name"):
                names[num] = s["name"]
        return names

    def add_scene(
        self,
        name: str,
        number: int | None = None,
        addresses: Iterable[int] = (),
        icon: str = DEFAULT_SCENE_ICON,
        avoid: Iterable[int] = (),
    ) -> int:
        """Create a scene: CDB `scenes[]` + `meta.scenes[]`; the number from `free_scene_number` unless given.

        `avoid`: numbers `free_scene_number` must not pick either — those a device still holds after a forced
        deletion skipped it, which the export no longer names.
        """
        check_name(name)
        if any(n.lower() == name.lower() for n in self.scene_names().values()):
            raise ValueError(f"a scene named {name!r} already exists")
        if number is None:
            number = self.free_scene_number(avoid=avoid)
        elif not 1 <= number <= 0xFFFF:
            raise ValueError(f"scene number {number} is not 1..65535")
        elif number in self.cdb.scenes:
            raise ValueError(f"scene {number} already exists")
        scenes = self.net.setdefault("scenes", [])
        addrs = list(dict.fromkeys(addresses))
        scenes.append(
            _ordered_like(
                _first(scenes),
                {
                    "name": name,
                    "number": hexaddr(number),
                    "addresses": [hexaddr(a) for a in addrs],
                },
            )
        )
        self.cdb.scenes[number] = addrs
        self.cdb.scene_names[number] = name
        self._sync_scenes(None)
        for s in meta_rows(self._meta("scenes")):
            if as_int(s.get("number")) == number:
                s["icon"] = icon
        return number

    def rename_scene(self, number: int, name: str) -> None:
        """Rename a scene in the CDB and in `meta.scenes[]`.

        The app's rename sheet: `check_name` with its `RENAME_MAX_LENGTH`, unless the name stays as it is.
        """
        check_name(name, _rename_limit(self.cdb.scene_names.get(number), name))
        if any(
            n != number and s.lower() == name.lower()
            for n, s in self.scene_names().items()
        ):
            raise ValueError(f"a scene named {name!r} already exists")
        self._scene_entry(number)["name"] = name
        self.cdb.scene_names[number] = name
        for s in meta_rows(self._meta("scenes")):
            if as_int(s.get("number")) == number:
                s["name"] = name

    def set_scene_addresses(self, number: int, addresses: Iterable[int]) -> None:
        """Record on which elements the scene is stored (`Scene.addresses`, what Scene Register Status tells)."""
        addrs = list(dict.fromkeys(addresses))
        self._scene_entry(number)["addresses"] = [hexaddr(a) for a in addrs]
        self.cdb.scenes[number] = addrs

    def remove_scene(self, number: int) -> None:
        """Delete a scene: CDB entry, `meta.scenes[]` and its `meta.sceneInfo[]` rows (network-features.md §3)."""
        self._scene_entry(number)
        self.net["scenes"] = [
            s for s in self.net["scenes"] if int(s["number"], 16) != number
        ]
        del self.cdb.scenes[number]
        self.cdb.scene_names.pop(number, None)
        self.meta["scenes"] = [
            s for s in self._meta("scenes") if keeps_row(s, "number", number)
        ]
        if "sceneInfo" in self.meta:
            self.meta["sceneInfo"] = [
                i for i in self._meta("sceneInfo") if keeps_row(i, "scene", number)
            ]

    def remove_scene_info(self, number: int, node: Node, location: int) -> None:
        """Drop the `meta.sceneInfo[]` rows of scene `number` for the app device of `node` that has `location`.

        What the app's *remove device from scene* does last (`RemoveDeviceConfigurationFromScene`,
        network-features.md §3): the row the app shows a legacy member's stored values from. A row of another
        device, another scene, or not naming a device is kept; a file without the list is left without it.
        """
        if "sceneInfo" not in self.meta:
            return
        self.meta["sceneInfo"] = [
            row
            for row in self._meta("sceneInfo")
            if keeps_row(row, "scene", number) or not names_device(row, node, location)
        ]

    def set_scene_info(
        self, number: int, node: Node, location: int, infos: Mapping[str, int | float]
    ) -> bool:
        """Record the values scene `number` holds for the app device of `node` that has `location` (`meta.sceneInfo`).

        What the app's *store device in scene* writes last (`SceneInfoRepositoryImpl.saveInfoFor`): one row per
        scene and device, replaced in place when there is one. The row's `deviceId` is a copy of the device row's
        (its key order and values as the file has them), `infos` holds the fields given (`scene_infos`) in the order
        of `Infos` or of a sibling row's, and a new row takes a sibling's key order, else `SceneInfoExport`'s
        (`scene`, `deviceId`, `infos`) with the scene number an int as Gson writes it. False, and nothing written,
        when the node has no app device with that location: the app would show the row on no device. The shapes
        come from the Android app's decompile; unverified with the app: no app has been seen importing a row Home
        Assistant wrote, and the iOS app's import of these rows is not known at all.
        """
        device = next(
            (
                d
                for d in self.device_rows(node)
                if location in (location_ids(d["deviceId"].get("locationIds")) or [])
            ),
            None,
        )
        if device is None:
            return False
        rows = self._meta("sceneInfo")
        template = _first(meta_rows(rows))
        fields = {k: infos[k] for k in SCENE_INFO_FIELDS if k in infos}
        row = _ordered_like(
            template,
            {
                "scene": number,
                "deviceId": copy.deepcopy(device["deviceId"]),
                "infos": _ordered_like(template and template.get("infos"), fields),
            },
        )
        mine = [
            i
            for i, r in enumerate(rows)
            if not keeps_row(r, "scene", number) and names_device(r, node, location)
        ]
        if not mine:
            rows.append(row)
            return True
        rows[mine[0]] = row
        for i in reversed(
            mine[1:]
        ):  # a second row of the same device: the app keeps one
            del rows[i]
        return True

    # ------------------------------------------------------------------ scene keys (`keyModeSceneConfigExports`)
    def scene_link_keys(self, number: int) -> set[int]:
        """Return the key elements the app's record shows recalling scene `number` (`keyModeSceneConfigExports`)."""
        return {
            key
            for row in meta_rows(meta_list(self.meta.get("keyModeSceneConfigExports")))
            if isinstance(config := row.get("sceneConfig"), dict)
            and as_int(config.get("sceneId")) == number
            and (key := as_int(row.get("elementAddress"))) is not None
        }

    def record_scene_link(self, key: int, number: int, publication: int | None) -> None:
        """Store the `keyModeSceneConfigExports` row that makes the app show key `key` as recalling scene `number`.

        `{"sceneConfig": {transitionStepSeconds, sceneId, transitionResolution, publicationAddress?},
        "elementAddress"}` (`MeshPropertyExport.java:153-161`), no transition as the app writes it; it replaces the
        key's earlier row and mirrors an existing row's style (field order, int vs hex-string addresses).
        """
        rows = meta_list(self.meta.get("keyModeSceneConfigExports"))
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
        self.meta["keyModeSceneConfigExports"] = [*kept, row]

    def drop_scene_link(self, key: int) -> None:
        """Remove key element `key`'s `keyModeSceneConfigExports` row.

        The row is what makes the app show a key as recalling "Scene N"; a key cleared or given another function
        no longer does, whatever its KeyMode still says. A file without such a row is left byte-identical.
        """
        rows = meta_list(self.meta.get("keyModeSceneConfigExports"))
        kept = [r for r in rows if keeps_row(r, "elementAddress", key)]
        if len(kept) != len(rows):
            self.meta["keyModeSceneConfigExports"] = kept

    # ------------------------------------------------------------------ device names (§8.3 row 8)
    def device_entry(self, node: Node, location: int) -> DeviceRow | None:
        """Return the `meta.devices[]` entry covering an element location: the most specific one, as the app resolves it."""
        best: dict[str, Any] | None = None
        best_size = 0
        for dev in meta_rows(meta_list(self.meta.get("devices"))):
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
        return cast("DeviceRow | None", best)

    @staticmethod
    def device_locations(entry: DeviceRow) -> list[int] | None:
        """Return the sorted `locationIds` of a `meta.devices[]` entry; None when they are not a list of numbers."""
        return location_ids(entry.get("deviceId", {}).get("locationIds"))

    def device_names(self) -> list[str]:
        """Every device name of `meta.devices[]`, in file order (what the app's duplicate check compares)."""
        return [
            str(d["name"]) for d in meta_rows(self._meta("devices")) if d.get("name")
        ]

    def rename_device(
        self, node: Node | int | str, locations: Iterable[int], name: str
    ) -> str:
        """Rename the app device `(nodeId, locationIds)` the way the app's `UpdateDeviceName` does; returns the name.

        Nothing changes when the device already has exactly that name. Otherwise the name must pass `check_name`
        with the rename sheet's `RENAME_MAX_LENGTH` (the name typed: the number suffix may take the result past
        it, as in the app), and a name another device has (ignoring case) gets the app's number suffix (`suffixed_name`: every rename
        screen of the app asks for it). Only `meta.devices[].name` changes (`set_device_name`); the CDB node name is
        the product's and stays. Unlike the app, the device's own old name does not count as taken, so changing
        only the case of a name keeps it instead of numbering it.
        """
        n = self._node(node)
        locs = sorted(int(x) for x in locations)
        entry = self._device_entry(n, locs)
        if entry is not None and entry.get("name") == name:
            return name
        check_name(name, RENAME_MAX_LENGTH)
        others = [
            str(d["name"])
            for d in meta_rows(self._meta("devices"))
            if d is not entry and d.get("name")
        ]
        if any(o.lower() == name.lower() for o in others):
            name = suffixed_name(name, self.device_names())
        self.set_device_name(n, locs, name)
        return name

    def set_device_name(
        self, node: Node | int | str, locations: Iterable[int], name: str
    ) -> DeviceRow:
        """Name the app device `(nodeId, locationIds)`: `meta.devices[].name` only, the CDB node is untouched.

        A device the `meta` block does not know yet gets a full entry (MAC from the EUI-64 UUID, product id from
        the CDB, actuator function / insert type guessed from the composition).
        """
        n = self._node(node)
        locs = sorted(int(x) for x in locations)
        entry = self._device_entry(n, locs)
        if entry is not None:
            entry["name"] = name
            return cast("DeviceRow", entry)
        devices = self._meta("devices")
        template = _first(meta_rows(devices))
        entry = _ordered_like(
            template,
            {
                "name": name,
                "macAddress": mac_from_uuid(n.uuid),
                "deviceId": _ordered_like(
                    template and template.get("deviceId"),
                    {
                        "actuatorFunctionId": guess_actuator_function(n),
                        "locationIds": locs,
                        "insertType": guess_insert_type(n),
                        "productId": n.pid if n.pid is not None else 0,
                        "nodeId": n.uuid,
                    },
                ),
                "cachedGroupConnectionMetadata": [],
            },
        )
        devices.append(entry)
        return cast("DeviceRow", entry)

    # ------------------------------------------------------------------ nodes
    def add_node_entry(
        self, entry: dict[str, Any], groups: Iterable[tuple[int, str]] = ()
    ) -> Node:
        """Append a node's CDB entry and the element groups its commissioning created; return the new node.

        The CDB is built anew from the tree (the entry is checked like any loaded one: `InvalidExport` names what
        is wrong), and the element groups get their `meta.elementConnectionGroups` rows as a loaded file would.
        """
        uuid = canonical_uuid(str(entry.get("UUID", "")))
        if self._node_by_uuid(uuid) is not None:
            raise ExportError(f"a node with UUID {uuid} is in the export already")
        nodes = self.net["nodes"]
        groups_json = self.net.setdefault("groups", [])
        cdb_nodes, cdb_groups = list(nodes), list(groups_json)
        nodes.append(entry)
        for address, name in groups:
            groups_json.append(
                _ordered_like(
                    _first(groups_json),
                    {
                        "address": hexaddr(address),
                        "name": name,
                        "parentAddress": "0000",
                    },
                )
            )
        try:
            self.cdb = CDB.from_network(self.net, self.meta)
        except InvalidExport:
            nodes[:], groups_json[:] = cdb_nodes, cdb_groups  # nothing of it stays
            raise
        self._sync_element_groups()
        node = self._node_by_uuid(uuid)
        assert node is not None
        return node

    def remove_node(self, node: Node, iv_index: int) -> list[ModelChange]:
        """Take a node out of the network's file as the app does; return the Config edits for the others.

        Its element groups go (`remove_group`: whoever subscribed or published to them is unwired), and so does any
        other node's publication to one of its elements; its elements leave every scene, its app device rows, room
        link rows, key scene rows and every other row the app keeps of it go (`exclude_node`). The node entry stays, marked `excluded`, and its addresses join
        `networkExclusions` under the current IV index (the Nordic library the app uses, `BaseMeshNetwork.java`):
        nodes still remember its sequence numbers, so nobody may take them before the IV index moved on twice.
        Edits addressed to the node itself are left out of the list: it has been reset. The part about the node
        alone, with no edit to any other node, is `exclude_node`.
        """
        own = {e.address for e in node.elements}
        changes: list[ModelChange] = []
        # by name, and by the app's `elementConnectionGroups` rows: what the rest of the
        # integration counts as the node's element groups (`cdb_element_groups`)
        listed = {
            g for e, g in cdb_element_groups(self.cdb, self.meta).items() if e in own
        }
        for address, name in list(self.cdb.groups.items()):
            if element_group_address(name) in own or address in listed:
                changes += self.remove_group(address)
        for other in self.cdb.nodes:
            if other is node:
                continue
            for el in other.elements:
                for m in el.raw_models:
                    if self.publication(el, m["modelId"]) in own:
                        changes += self.set_publication(other, el, m["modelId"], None)
        self.exclude_node(node, iv_index)
        return [c for c in changes if c.element not in own]

    def exclude_node(self, node: Node, iv_index: int) -> None:
        """Record `node` as reset and out of the network, leaving every other node's wiring to it as the file has it.

        The part of `remove_node` that holds once the node confirmed its reset, whatever the others still hold:
        its elements leave every scene, its app device rows, room link rows and key scene rows go, and so do the
        rows the app keeps per device or element of it (`sceneInfo`, `schedulerMetaInfo`, `timer`,
        `actuatorExports`, `buttonLayoutExports`; a list the file lacks stays absent); the entry is marked
        `excluded` and its addresses join `networkExclusions` under `iv_index`. That a re-import of such a file
        leaves the app no row of the node is unverified with the app. The record of a removal
        whose unwiring stopped part-way is this plus the edits the others accepted; the element groups and
        publications left point at a node that no longer answers, and stay listed so nothing new reuses them.
        """
        own = {e.address for e in node.elements}
        for number, addresses in list(self.cdb.scenes.items()):
            if own & set(addresses):
                self.set_scene_addresses(number, [a for a in addresses if a not in own])
        self.meta["devices"] = [
            dev
            for dev in self._meta("devices")
            if not (
                isinstance(dev, dict)
                and isinstance(dev.get("deviceId"), dict)
                and canonical_uuid(str(dev["deviceId"].get("nodeId", ""))) == node.uuid
            )
        ]
        for dev in meta_rows(self._meta("devices")):
            cached = meta_list(dev.get("cachedGroupConnectionMetadata"))
            if cached:
                dev["cachedGroupConnectionMetadata"] = [
                    c for c in cached if as_int(c.get("elementAddress")) not in own
                ]
        for key in ("sceneInfo", "schedulerMetaInfo", "timer"):
            self._drop_rows(key, lambda row: row_node(row) == node.uuid)
        for key in ("keyModeSceneConfigExports", *PROPERTY_ROW_KEYS):
            self._drop_rows(key, lambda row: as_int(row.get("elementAddress")) in own)
        entry = next(
            n
            for n in self.net["nodes"]
            if canonical_uuid(str(n.get("UUID", ""))) == node.uuid
        )
        entry["excluded"] = True
        exclusions = self.net.setdefault("networkExclusions", [])
        row = next(
            (
                x
                for x in exclusions
                if isinstance(x, dict) and x.get("ivIndex") == iv_index
            ),
            None,
        )
        if row is None:
            row = {"ivIndex": iv_index, "addresses": []}
            exclusions.append(row)
        row["addresses"] += [hexaddr(a) for a in sorted(own)]
        self.cdb = CDB.from_network(self.net, self.meta)

    def _drop_rows(self, key: str, drop: Callable[[dict[str, Any]], bool]) -> None:
        """Remove the object rows of `meta[key]` that `drop` picks; a list the file lacks is not added."""
        if key in self.meta:
            self.meta[key] = [
                r for r in self._meta(key) if not (isinstance(r, dict) and drop(r))
            ]

    def node_entry(self, unicast: int) -> dict[str, Any]:
        """Return the CDB `nodes[]` entry of the node at primary unicast `unicast`; StopIteration when there is none."""
        return next(
            n
            for n in self.net["nodes"]
            if parse_address(str(n.get("unicastAddress", "0"))) == unicast
        )

    def device_rows(self, node: Node) -> list[DeviceRow]:
        """Return the app device rows (`meta.devices`) of `node`: the app's logical devices of it."""
        return [
            cast("DeviceRow", dev)
            for dev in meta_rows(self._meta("devices"))
            if isinstance(dev.get("deviceId"), dict)
            and canonical_uuid(str(dev["deviceId"].get("nodeId", ""))) == node.uuid
        ]

    def clone_device_rows(
        self, template: Node, node: Node, name: str, function: int | None = None
    ) -> int:
        """Give `node` the app device rows `template` has (its split into devices, product and insert), named `name`.

        Several devices of one node get the template's names made unique with the new name in front. `function`:
        the actuator function the new node advertised, which its rows carry instead of the template's (the app
        reads the InsertId of every node it adds; a push-button's insert need not be its template's). Returns how
        many rows were added.
        """
        rows = self.device_rows(template)
        mac = mac_from_uuid(node.uuid)
        for i, row in enumerate(rows):
            clone = copy.deepcopy(row)
            clone["deviceId"]["nodeId"] = node.uuid
            if function is not None:
                clone["deviceId"]["actuatorFunctionId"] = function
            clone["name"] = name if len(rows) == 1 else f"{name} {i + 1}"
            if "macAddress" in clone:
                clone["macAddress"] = mac
            if "cachedGroupConnectionMetadata" in clone:
                clone["cachedGroupConnectionMetadata"] = []
            self._meta("devices").append(clone)
        return len(rows)

    def clone_property_rows(
        self,
        template: Node,
        node: Node,
        function: int | None = None,
        layout: int | None = None,
    ) -> int:
        """Give `node` the `actuatorExports` / `buttonLayoutExports` rows `template` has, on the same elements.

        The app reads a node's InsertId (`0x0002`: actuator function, insert type) and, for keys, its ButtonLayout
        (`0x5001`) when it adds it, keeps them per element and exports them (`MeshPropertyExport`). A node Home
        Assistant adds gets the rows the file holds for its template, the same product, at the same element offsets,
        with what the node advertised (`function`, `layout`) in place of the template's values. A template without such rows gives none: the app keeps none for it, or the file is
        one that leaves the list out. Returns how many rows were added. The shapes come from the Android app's
        decompile; unverified with the app: no app has been seen importing a row Home Assistant wrote.
        """
        offsets = {e.address: i for i, e in enumerate(template.elements)}
        added = 0
        for key in PROPERTY_ROW_KEYS:
            if key not in self.meta:
                continue
            rows = self._meta(key)
            for row in meta_rows(list(rows)):
                index = offsets.get(as_int(row.get("elementAddress")) or -1)
                if index is None or index >= len(node.elements):
                    continue
                clone = copy.deepcopy(row)
                clone["elementAddress"] = _addr_like(
                    row.get("elementAddress"), node.elements[index].address
                )
                actuator = clone.get("actuatorId")
                if function is not None and isinstance(actuator, dict):
                    actuator["actuatorFunctionId"] = function
                if layout is not None and "mode" in clone:
                    clone["mode"] = layout
                rows.append(clone)
                added += 1
        return added

    # ------------------------------------------------------------------ serialisation
    def touch(self, now: datetime | None = None) -> str:
        """Bump the CDB `timestamp` (the nRF-Mesh library does so on every change), never behind the loaded one.

        A clock behind the file's own writer (the app's phone, a host whose clock was off) used to stamp a change
        *older* than what it was made from, and every newer-export guard (ours, the app's) then took the stale side
        for the newer one. The stamp is the later of `now` and the loaded timestamp plus a millisecond, in the
        format's whole seconds: a clock behind the file keeps the file's own second rather than moving it on by
        one for every save.
        """
        when = (now or datetime.now(UTC)).astimezone(UTC)
        loaded = _parse_timestamp(self.loaded_timestamp)
        if loaded is not None:
            if loaded.tzinfo is None:
                loaded = loaded.replace(tzinfo=UTC)
            when = max(when, loaded + timedelta(milliseconds=1))
        stamp = now_timestamp(when)
        self.net["timestamp"] = stamp
        return stamp

    def cdb_json(self) -> str:
        """Render the `MeshNetwork.json` flavour: `{"meshNetwork": …}` in the loaded layout."""
        return self.style.outer.dumps({"meshNetwork": self.net}) + "\n"

    def share_json(self) -> str:
        """Render the `JungHome.json` flavour: `ExportDto` = header + `meta` + Base64 (no wrap) of the CDB JSON."""
        inner_obj: Any = (
            {"meshNetwork": self.net} if self.style.inner_wrapped else self.net
        )
        inner = self.style.inner.dumps(inner_obj)
        fields: dict[str, Any] = {
            "version": self.header.get("version", DEFAULT_VERSION),
            "appVersion": self.header.get("appVersion", DEFAULT_APP_VERSION),
            "platform": self.header.get("platform", DEFAULT_PLATFORM),
        }
        fields.update({k: v for k, v in self.header.items() if k not in fields})
        fields["meta"] = self.meta
        fields["network"] = base64.b64encode(inner.encode()).decode()
        order = [k for k in self.style.outer_keys if k in fields]
        doc = {k: fields[k] for k in (*order, *(k for k in fields if k not in order))}
        return self.style.outer.dumps(doc) + "\n"

    def render(self, flavour: Flavour | None = None) -> str:
        """Serialise in `flavour` (default: the loaded one)."""
        return (
            self.share_json()
            if (flavour or self.flavour) == "share"
            else self.cdb_json()
        )

    @staticmethod
    def file_timestamp(data: bytes) -> str | None:
        """CDB `timestamp` of an export of either flavour; None when the bytes are not one."""
        try:
            net, _ = CDB.parse(data.decode())
        except (ValueError, KeyError, TypeError, AttributeError):
            return None
        return str(net.get("timestamp", ""))

    def check_target(self, path: Path) -> None:
        """Raise unless `path` is absent or byte-identical to what we loaded from / wrote to it.

        The file we loaded / wrote (`self.path`, whose bytes `digest` is of) is refused on *any* change: an edit
        that leaves the CDB `timestamp` alone (the app's meta-only rename, a hand edit, a writer with a skewed
        clock) would otherwise be overwritten with what we planned against the old content. Any other file —
        nothing to compare its bytes with — is refused when its timestamp is newer than ours.
        """
        if not path.exists():
            return
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if self.digest is not None and digest == self.digest:
            return
        on_disk = self.file_timestamp(data)
        if on_disk is None:
            raise ExportError(
                f"{path} is not a JUNG HOME mesh export; refusing to overwrite it"
            )
        ours = (
            self.digest is not None
            and self.path is not None
            and path.resolve() == self.path.resolve()
        )
        if ours or timestamp_advanced(on_disk, self.loaded_timestamp):
            raise NewerExportError(path, on_disk, self.loaded_timestamp)

    def save(
        self,
        path: Path | None = None,
        flavour: Flavour | None = None,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> Path:
        """Write atomically (temp file + replace), keeping the previous content as a backup, after the newer-export guard.

        The backup is `<name>.bak`, the older generations rotated one down (`fileio.backup_paths`). The CDB `timestamp` is bumped on every save, so a no-op save changes nothing else. A symlinked target
        (an entry pointing at a synced copy of the app container) is written *through*: the real file gets the
        new content and the backups sit beside it, and the link stays a link — `Path.replace` on the link itself
        would swap it for a regular file and the linked original would never see another write.
        """
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ExportError("no path to save to")
        if not force:
            self.check_target(target)
        self.touch(now)
        data = self.render(flavour).encode()
        # private (0600) whatever the umask: the file holds every mesh key; a user-provided target already
        # stricter than that keeps its own mode, and the backup is never more readable than the target
        atomic_write(target, data, private=True, backup=True)
        self.path = target
        self.flavour = flavour or self.flavour
        self.digest = hashlib.sha256(data).hexdigest()
        self.loaded_timestamp = str(self.net["timestamp"])
        return target

    def snapshot(self) -> dict[str, Any]:
        """Deep copy of the tree and `meta` (for diffing before / after a mutation)."""
        return copy.deepcopy({"network": self.net, "meta": self.meta})
