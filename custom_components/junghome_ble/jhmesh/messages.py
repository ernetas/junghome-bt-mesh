"""Access-layer message builders and decoders (SIG generic/lighting/scene/sensor/time + JUNG vendor)."""

from __future__ import annotations

import math
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from . import config_messages, properties, vendor_models
from .pdu import decode_opcode, encode_opcode

JUNG_CID = vendor_models.JUNG_CID  # defined once, in vendor_models

# SIG opcodes
GEN_ONOFF_GET, GEN_ONOFF_SET, GEN_ONOFF_SET_UNACK, GEN_ONOFF_STATUS = (
    0x8201,
    0x8202,
    0x8203,
    0x8204,
)
GEN_LEVEL_GET, GEN_LEVEL_SET, GEN_LEVEL_SET_UNACK, GEN_LEVEL_STATUS = (
    0x8205,
    0x8206,
    0x8207,
    0x8208,
)
GEN_DELTA_SET, GEN_DELTA_SET_UNACK = 0x8209, 0x820A
GEN_MOVE_SET, GEN_MOVE_SET_UNACK = 0x820B, 0x820C
GEN_DTT_STATUS = 0x8210
GEN_ONPOWERUP_GET, GEN_ONPOWERUP_STATUS, GEN_ONPOWERUP_SET, GEN_ONPOWERUP_SET_UNACK = (
    0x8211,
    0x8212,
    0x8213,
    0x8214,
)
GEN_BATTERY_GET, GEN_BATTERY_STATUS = 0x8223, 0x8224
GEN_LOCATION_GLOBAL_GET, GEN_LOCATION_GLOBAL_STATUS = 0x8225, 0x40
GEN_LOCATION_GLOBAL_SET, GEN_LOCATION_GLOBAL_SET_UNACK = 0x41, 0x42
GEN_MANU_PROP_GET, GEN_ADMIN_PROP_GET, GEN_USER_PROP_GET = 0x822B, 0x822D, 0x822F
GEN_MANU_PROP_SET, GEN_MANU_PROP_SET_UNACK = 0x44, 0x45
GEN_ADMIN_PROP_SET, GEN_ADMIN_PROP_SET_UNACK = 0x48, 0x49
GEN_USER_PROP_SET, GEN_USER_PROP_SET_UNACK = 0x4C, 0x4D
(
    LIGHT_LIGHTNESS_DEFAULT_GET,
    LIGHT_LIGHTNESS_DEFAULT_STATUS,
    LIGHT_LIGHTNESS_RANGE_GET,
    LIGHT_LIGHTNESS_RANGE_STATUS,
    LIGHT_LIGHTNESS_DEFAULT_SET,
    LIGHT_LIGHTNESS_DEFAULT_SET_UNACK,
    LIGHT_LIGHTNESS_RANGE_SET,
    LIGHT_LIGHTNESS_RANGE_SET_UNACK,
) = 0x8255, 0x8256, 0x8257, 0x8258, 0x8259, 0x825A, 0x825B, 0x825C
LIGHT_CTL_TEMP_RANGE_GET, LIGHT_CTL_TEMP_RANGE_STATUS = 0x8262, 0x8263
LIGHT_CTL_DEFAULT_GET, LIGHT_CTL_DEFAULT_STATUS = 0x8267, 0x8268
LIGHT_CTL_DEFAULT_SET, LIGHT_CTL_DEFAULT_SET_UNACK = 0x8269, 0x826A
LIGHT_CTL_TEMP_RANGE_SET, LIGHT_CTL_TEMP_RANGE_SET_UNACK = 0x826B, 0x826C
RANGE_STATUS_CODES = {
    0: "success",
    1: "cannot-set-range-min",
    2: "cannot-set-range-max",
}
ONPOWERUP_NAMES = {0: "off", 1: "default", 2: "restore"}
# Generic Location Global (Mesh Model spec §3.1.2): s32 latitude / longitude scaled to ±(2^31-1), s16 altitude (m)
LOCATION_NOT_CONFIGURED = -0x80000000
ALTITUDE_NOT_CONFIGURED, ALTITUDE_TOO_HIGH = 0x7FFF, 0x7FFE
# Generic Battery Status (§3.1.5): u8 level (0x00-0x64 percent, 0xFF unknown, 0x65-0xFE prohibited), u24 discharge /
# charge minutes (0xFFFFFF unknown), flags
BATTERY_UNKNOWN_LEVEL, BATTERY_UNKNOWN_TIME = 0xFF, 0xFFFFFF
BATTERY_MAX_LEVEL = 100
BATTERY_PRESENCE = {0: "not-present", 1: "removable", 2: "non-removable", 3: "unknown"}
BATTERY_INDICATOR = {0: "critically-low", 1: "low", 2: "good", 3: "unknown"}
BATTERY_CHARGING = {0: "not-chargeable", 1: "not-charging", 2: "charging", 3: "unknown"}
BATTERY_SERVICEABILITY = {
    0: "reserved",
    1: "no-service-required",
    2: "service-required",
    3: "unknown",
}
(
    LIGHT_LIGHTNESS_GET,
    LIGHT_LIGHTNESS_SET,
    LIGHT_LIGHTNESS_SET_UNACK,
    LIGHT_LIGHTNESS_STATUS,
) = 0x824B, 0x824C, 0x824D, 0x824E
LIGHT_CTL_GET, LIGHT_CTL_SET, LIGHT_CTL_SET_UNACK, LIGHT_CTL_STATUS = (
    0x825D,
    0x825E,
    0x825F,
    0x8260,
)
# Light CTL Temperature (§6.3.2.4-6.3.2.7): the CTL Temperature Server on the element after the CTL Server's
LIGHT_CTL_TEMP_GET, LIGHT_CTL_TEMP_SET, LIGHT_CTL_TEMP_SET_UNACK = (
    0x8261,
    0x8264,
    0x8265,
)
LIGHT_CTL_TEMP_STATUS = 0x8266
SCENE_GET, SCENE_RECALL, SCENE_RECALL_UNACK, SCENE_STATUS = 0x8241, 0x8242, 0x8243, 0x5E
SCENE_REGISTER_GET, SCENE_REGISTER_STATUS = 0x8244, 0x8245
SCENE_STORE, SCENE_STORE_UNACK = (
    0x8246,
    0x8247,
)  # Scene Setup Server: store the element's state under a scene
SCENE_DELETE, SCENE_DELETE_UNACK = 0x829E, 0x829F
SCENE_REGISTER_FULL = 0x01  # Scene Register Status codes (Mesh Model spec §5.2.2.11)
SCENE_NOT_FOUND = 0x02
SENSOR_GET = 0x8231
SENSOR_STATUS = 0x52
SENSOR_DESCRIPTOR_GET, SENSOR_DESCRIPTOR_STATUS = 0x8230, 0x51
SENSOR_CADENCE_GET, SENSOR_CADENCE_STATUS = 0x8234, 0x57
SENSOR_SETTINGS_GET, SENSOR_SETTINGS_STATUS = 0x8235, 0x58
GEN_USER_PROP_STATUS, GEN_ADMIN_PROP_STATUS, GEN_MANU_PROP_STATUS = 0x4E, 0x4A, 0x46
# "Properties Get" (no parameters) → "Properties Status" (list of property ids u16 LE), per SIG server
GEN_MANU_PROPS_GET, GEN_MANU_PROPS_STATUS = 0x822A, 0x43
GEN_ADMIN_PROPS_GET, GEN_ADMIN_PROPS_STATUS = 0x822C, 0x47
GEN_USER_PROPS_GET, GEN_USER_PROPS_STATUS = 0x822E, 0x4B
SIG_PROPERTY_LIST_OPCODES: dict[
    str, tuple[int, int]
] = {  # kind → (Properties Get, Properties Status)
    "manufacturer": (GEN_MANU_PROPS_GET, GEN_MANU_PROPS_STATUS),
    "admin": (GEN_ADMIN_PROPS_GET, GEN_ADMIN_PROPS_STATUS),
    "user": (GEN_USER_PROPS_GET, GEN_USER_PROPS_STATUS),
}
TIME_SET, TIME_STATUS, TIME_GET = 0x5C, 0x5D, 0x8237
TIME_ROLE_GET, TIME_ROLE_SET, TIME_ROLE_STATUS = 0x8238, 0x8239, 0x823A
TIME_ZONE_GET, TIME_ZONE_STATUS = 0x823B, 0x823D
TAI_UTC_DELTA_GET, TAI_UTC_DELTA_STATUS = 0x823E, 0x8240
TIME_ROLES = {0: "none", 1: "authority", 2: "relay", 3: "client"}
# Health model (Mesh Profile §4.2 / Model spec §4.1): faults are per company id; 0x00 = no fault, 0x80+ vendor codes
HEALTH_CURRENT_STATUS = 0x04  # published by the Health Server: the faults present now (same layout as Fault Status)
HEALTH_FAULT_GET, HEALTH_FAULT_STATUS = 0x8031, 0x05
HEALTH_FAULT_CLEAR, HEALTH_FAULT_CLEAR_UNACK = 0x802F, 0x8030
HEALTH_FAULT_TEST, HEALTH_FAULT_TEST_UNACK = 0x8032, 0x8033
# Health Fault IDs (Mesh Profile §4.2.x, Table 4.5); 0x80..0xFF are vendor-specific — JUNG nodes register 0x81 and
# 0x80 (meaning unknown, `docs/hidden-features.md` §10)
HEALTH_FAULT_NAMES = {
    0x00: "no fault",
    0x01: "battery low warning",
    0x02: "battery low error",
    0x03: "supply voltage too low warning",
    0x04: "supply voltage too low error",
    0x05: "supply voltage too high warning",
    0x06: "supply voltage too high error",
    0x07: "power supply interrupted warning",
    0x08: "power supply interrupted error",
    0x09: "no load warning",
    0x0A: "no load error",
    0x0B: "overload warning",
    0x0C: "overload error",
    0x0D: "overheat warning",
    0x0E: "overheat error",
    0x0F: "condensation warning",
    0x10: "condensation error",
    0x11: "vibration warning",
    0x12: "vibration error",
    0x13: "configuration warning",
    0x14: "configuration error",
    0x15: "element not calibrated warning",
    0x16: "element not calibrated error",
    0x17: "memory warning",
    0x18: "memory error",
    0x19: "self-test warning",
    0x1A: "self-test error",
    0x1B: "input too low warning",
    0x1C: "input too low error",
    0x1D: "input too high warning",
    0x1E: "input too high error",
    0x1F: "input no change warning",
    0x20: "input no change error",
    0x21: "actuator blocked warning",
    0x22: "actuator blocked error",
    0x23: "housing opened warning",
    0x24: "housing opened error",
    0x25: "tamper warning",
    0x26: "tamper error",
    0x27: "device moved warning",
    0x28: "device moved error",
    0x29: "device dropped warning",
    0x2A: "device dropped error",
    0x2B: "overflow warning",
    0x2C: "overflow error",
    0x2D: "empty warning",
    0x2E: "empty error",
    0x2F: "internal bus warning",
    0x30: "internal bus error",
    0x31: "mechanism jammed warning",
    0x32: "mechanism jammed error",
}
HEALTH_FAULT_VENDOR_MIN = 0x80
HEALTH_ATTENTION_GET, HEALTH_ATTENTION_STATUS = 0x8004, 0x8007
HEALTH_ATTENTION_SET, HEALTH_ATTENTION_SET_UNACK = 0x8005, 0x8006
HEALTH_PERIOD_GET, HEALTH_PERIOD_STATUS = 0x8034, 0x8037
GEN_DTT_GET = 0x820D
GEN_DTT_SET, GEN_DTT_SET_UNACK = 0x820E, 0x820F

# Time model (Mesh Model spec §5.1.1): TAI seconds since 2000-01-01T00:00:00 TAI; TAI = UTC + 37 s since 2017-01-01
TAI_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
TAI_UTC_DELTA = 37

# The LBC vendor property opcodes, one row per server (docs/android/vendor-models.md §2.3; the Set / Status columns
# per docs/cross-repo-analysis.md §1.3): kind -> (Properties Get, Properties Status, Get, Set, Set Unack, Status).
# Every other LBC table below is derived from this one.
_LBC_OPS: dict[str, tuple[int, int, int, int, int, int]] = {
    "admin": (0x00, 0x01, 0x02, 0x03, 0x04, 0x05),
    "manufacturer": (0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B),
    "user": (0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11),
}

# JUNG vendor opcodes (docs/android/vendor-models.md)
VENDOR_NAMES = {
    0x00: "LBC Admin Properties Get",
    0x01: "LBC Admin Properties Status",
    0x06: "LBC Manufacturer Properties Get",
    0x07: "LBC Manufacturer Properties Status",
    0x0C: "LBC User Properties Get",
    0x0D: "LBC User Properties Status",
    0x02: "LBC Admin Property Get",
    0x03: "LBC Admin Property Set",
    0x04: "LBC Admin Property Set Unack",
    0x05: "LBC Admin Property Status",
    0x08: "LBC Manufacturer Property Get",
    0x09: "LBC Manufacturer Property Set",
    0x0A: "LBC Manufacturer Property Set Unack",
    0x0B: "LBC Manufacturer Property Status",
    0x0E: "LBC User Property Get",
    0x0F: "LBC User Property Set",
    0x10: "LBC User Property Set Unack",
    0x11: "LBC User Property Status",
    0x12: "JH Scheduler Get",
    0x13: "JH Scheduler Status",
    0x14: "JH Scheduler Set",
    0x16: "Scene Action Setup Get",
    0x17: "Scene Action Setup Status",
    0x18: "Scene Action Setup Set",
}

# Seeded randomly: a node treats a Set with the same source, destination and TID within 6 s as a retransmission
# (Mesh Model spec), so a counter starting at 0 in every process made a second short-lived CLI run's first Set a no-op.
_tid = secrets.randbelow(256)


def next_tid() -> int:
    """Return the next transaction identifier (8-bit, wrapping)."""
    global _tid  # noqa: PLW0603  # one TID counter for every sender in the process, as the spec's per-source TID intends
    _tid = (_tid + 1) & 0xFF
    return _tid


# ----------------------------------------------------------------------------- builders


def generic_onoff_get() -> bytes:
    """Build Generic OnOff Get."""
    return encode_opcode(GEN_ONOFF_GET)


TRANSITION_UNKNOWN = 0x3F  # transition-time steps "unknown" (§3.1.3): legal in a Status, prohibited in a Set


def _field(value: int, size: int, name: str, *, signed: bool = False) -> bytes:
    """Little-endian `value` in `size` bytes; ValueError naming the field when it does not fit.

    `int.to_bytes` raises OverflowError, which no caller of these builders catches (the HA services and config
    entities catch ValueError, as for every other malformed argument) and which does not say which field.
    """
    try:
        return value.to_bytes(size, "little", signed=signed)
    except OverflowError as e:
        lo, hi = (
            (-(1 << (8 * size - 1)), (1 << (8 * size - 1)) - 1)
            if signed
            else (0, (1 << (8 * size)) - 1)
        )
        raise ValueError(f"{name} {value} is not {lo}..{hi}") from e


def _tid_transition(
    tid: int | None, transition: int | None, delay: int
) -> bytes:  # `[tid](+transition,delay)` tail shared by every Set
    p = _field(next_tid() if tid is None else tid, 1, "tid")
    if transition is not None:
        if transition & 0x3F == TRANSITION_UNKNOWN:
            raise ValueError(
                f"transition time {transition:#04x}: steps 0x3F (unknown) is prohibited in a Set"
            )
        p += _field(transition, 1, "transition time") + _field(delay, 1, "delay")
    return p


def generic_onoff_set(
    on: bool,
    ack: bool = True,
    tid: int | None = None,
    transition: int | None = None,
    delay: int = 0,
) -> bytes:
    """Build Generic OnOff Set (Unacknowledged unless `ack`); transition/delay only when a transition is given."""
    return (
        encode_opcode(GEN_ONOFF_SET if ack else GEN_ONOFF_SET_UNACK)
        + bytes([1 if on else 0])
        + _tid_transition(tid, transition, delay)
    )


def light_lightness_get() -> bytes:
    """Build Light Lightness Get."""
    return encode_opcode(LIGHT_LIGHTNESS_GET)


def light_lightness_set(
    lightness: int, ack: bool = True, tid: int | None = None
) -> bytes:
    """Build Light Lightness Set (Unacknowledged unless `ack`)."""
    return (
        encode_opcode(LIGHT_LIGHTNESS_SET if ack else LIGHT_LIGHTNESS_SET_UNACK)
        + _field(lightness, 2, "lightness")
        + _tid_transition(tid, None, 0)
    )


def light_ctl_get() -> bytes:
    """Build Light CTL Get."""
    return encode_opcode(LIGHT_CTL_GET)


def light_ctl_set(
    lightness: int,
    temperature_k: int,
    delta_uv: int = 0,
    ack: bool = True,
    tid: int | None = None,
    transition: int | None = None,
    delay: int = 0,
) -> bytes:
    """Build Light CTL Set (Unacknowledged unless `ack`); transition/delay only when a transition is given.

    The temperature must be 800..20000 K (§6.1.3.1; a node ignores a Set outside it, so it is refused here
    with a reason rather than answered with a timeout).
    """
    _check_temperature(temperature_k)
    p = (
        _field(lightness, 2, "lightness")
        + _field(temperature_k, 2, "temperature")
        + _field(delta_uv, 2, "delta UV", signed=True)
        + _tid_transition(tid, transition, delay)
    )
    return encode_opcode(LIGHT_CTL_SET if ack else LIGHT_CTL_SET_UNACK) + p


def light_ctl_temperature_get() -> bytes:
    """Build Light CTL Temperature Get (0x8261), for the element hosting the CTL Temperature Server."""
    return encode_opcode(LIGHT_CTL_TEMP_GET)


def light_ctl_temperature_set(
    temperature_k: int,
    delta_uv: int = 0,
    ack: bool = True,
    tid: int | None = None,
    transition: int | None = None,
    delay: int = 0,
) -> bytes:
    """Build Light CTL Temperature Set (Unacknowledged unless `ack`): `[temperature u16][delta UV s16][tid](+transition,delay)`.

    The colour temperature alone, leaving the lightness where it is (a CTL Set must carry one). It goes to the
    element hosting the CTL Temperature Server, not the CTL Server's; the range is checked as in `light_ctl_set`.
    """
    _check_temperature(temperature_k)
    return (
        encode_opcode(LIGHT_CTL_TEMP_SET if ack else LIGHT_CTL_TEMP_SET_UNACK)
        + _field(temperature_k, 2, "temperature")
        + _field(delta_uv, 2, "delta UV", signed=True)
        + _tid_transition(tid, transition, delay)
    )


def scene_recall(scene: int, ack: bool = True, tid: int | None = None) -> bytes:
    """Build Scene Recall (Unacknowledged unless `ack`); scene 0 is prohibited (§5.1.3.1), as in Store / Delete."""
    return (
        encode_opcode(SCENE_RECALL if ack else SCENE_RECALL_UNACK)
        + _scene_number(scene)
        + _tid_transition(tid, None, 0)
    )


def scene_get() -> bytes:
    """Build Scene Get."""
    return encode_opcode(SCENE_GET)


def scene_register_get() -> bytes:
    """Scene Register Get: which scenes the element has stored (Scene Register Status)."""
    return encode_opcode(SCENE_REGISTER_GET)


def _scene_number(scene: int) -> bytes:
    if not 1 <= scene <= 0xFFFF:
        raise ValueError(f"scene {scene} is not 1..65535")
    return scene.to_bytes(2, "little")


def scene_store(scene: int, *, ack: bool = True) -> bytes:
    """Scene Store: the element saves its *current* state under `scene` (the app sets the state first, then stores).

    Answered by a Scene Register Status when acknowledged — JUNG firmware may publish it to the Scene Setup
    Server's (unset) publish address instead of replying, so read back with `scene_register_get`.
    """
    return encode_opcode(SCENE_STORE if ack else SCENE_STORE_UNACK) + _scene_number(
        scene
    )


def scene_delete(scene: int, *, ack: bool = True) -> bytes:
    """Scene Delete: the element forgets `scene` (same acknowledgement caveat as `scene_store`)."""
    return encode_opcode(SCENE_DELETE if ack else SCENE_DELETE_UNACK) + _scene_number(
        scene
    )


@dataclass(frozen=True)
class SceneStatus:
    """A Scene Status: status code, the current scene (0 = none) and, mid-transition, the target and remaining time.

    JUNG nodes publish it to their group after every recall they carry out (a key's, the app's or ours) and answer
    a Scene Get with it (`docs/parity/captures-inventory.json`: `5E`, 3 bytes, `pid1/2/4 el0 → group`).
    """

    status: int
    current: int
    target: int | None = None
    remaining: int | None = None  # the raw Generic Default Transition Time byte

    @property
    def ok(self) -> bool:
        """Whether the node reported Success."""
        return self.status == 0


def decode_scene_status(params: bytes) -> SceneStatus:
    """Decode Scene Status: `[status][current u16]` or `[status][current u16][target u16][remaining]` (§5.2.2.6)."""
    if len(params) < 3:
        raise ValueError(f"Scene Status needs 3 bytes, got {len(params)}")
    current = int.from_bytes(params[1:3], "little")
    if len(params) >= 6:
        return SceneStatus(
            params[0], current, int.from_bytes(params[3:5], "little"), params[5]
        )
    return SceneStatus(params[0], current)


@dataclass(frozen=True)
class SceneRegister:
    """A Scene Register Status: status code, the current scene (0 = none) and the stored scene numbers."""

    status: int
    current: int
    scenes: tuple[int, ...]

    @property
    def ok(self) -> bool:
        """Whether the node reported Success."""
        return self.status == 0


def decode_scene_register_status(params: bytes) -> SceneRegister:
    """Decode Scene Register Status: `[status][current scene u16][scene u16 …]`."""
    if len(params) < 3:
        raise ValueError(f"Scene Register Status needs 3 bytes, got {len(params)}")
    scenes = tuple(
        int.from_bytes(params[i : i + 2], "little")
        for i in range(3, len(params) - 1, 2)
    )
    return SceneRegister(params[0], int.from_bytes(params[1:3], "little"), scenes)


def sensor_get(property_id: int | None = None) -> bytes:
    """Sensor Get: all sensor values of the element, or one property."""
    return encode_opcode(SENSOR_GET) + (
        _field(property_id, 2, "property id") if property_id is not None else b""
    )


# ----------------------------------------------------------------------------- SIG builders (roadmap 0.2)


def generic_level_get() -> bytes:
    """Build Generic Level Get."""
    return encode_opcode(GEN_LEVEL_GET)


def generic_level_set(
    level: int,
    ack: bool = True,
    tid: int | None = None,
    transition: int | None = None,
    delay: int = 0,
) -> bytes:
    """Build Generic Level Set (Unacknowledged unless `ack`): `[level s16][tid](+transition,delay)`."""
    return (
        encode_opcode(GEN_LEVEL_SET if ack else GEN_LEVEL_SET_UNACK)
        + _field(level, 2, "level", signed=True)
        + _tid_transition(tid, transition, delay)
    )


def generic_delta_set(
    delta: int,
    ack: bool = True,
    tid: int | None = None,
    transition: int | None = None,
    delay: int = 0,
) -> bytes:
    """Build Generic Delta Set (Unacknowledged unless `ack`): `[delta s32][tid](+transition,delay)`."""
    return (
        encode_opcode(GEN_DELTA_SET if ack else GEN_DELTA_SET_UNACK)
        + _field(delta, 4, "delta", signed=True)
        + _tid_transition(tid, transition, delay)
    )


def generic_move_set(
    delta: int,
    ack: bool = True,
    tid: int | None = None,
    transition: int | None = None,
    delay: int = 0,
) -> bytes:
    """Build Generic Move Set (Unacknowledged unless `ack`): `[delta s16][tid](+transition,delay)`."""
    return (
        encode_opcode(GEN_MOVE_SET if ack else GEN_MOVE_SET_UNACK)
        + _field(delta, 2, "delta", signed=True)
        + _tid_transition(tid, transition, delay)
    )


def generic_onpowerup_get() -> bytes:
    """Build Generic OnPowerUp Get."""
    return encode_opcode(GEN_ONPOWERUP_GET)


def generic_onpowerup_set(mode: int, ack: bool = True) -> bytes:
    """Build Generic OnPowerUp Set (Unacknowledged unless `ack`): 0 off, 1 default (on), 2 restore."""
    if mode not in ONPOWERUP_NAMES:
        raise ValueError(f"OnPowerUp mode {mode} is not 0..2")
    return encode_opcode(GEN_ONPOWERUP_SET if ack else GEN_ONPOWERUP_SET_UNACK) + bytes(
        [mode]
    )


def generic_battery_get() -> bytes:
    """Build Generic Battery Get."""
    return encode_opcode(GEN_BATTERY_GET)


def generic_location_global_get() -> bytes:
    """Build Generic Location Global Get."""
    return encode_opcode(GEN_LOCATION_GLOBAL_GET)


def location_global_fields(
    latitude: float | None, longitude: float | None, altitude: int | None
) -> tuple[int, int, int]:
    """Encode degrees / metres into the Global Latitude, Global Longitude and Global Altitude fields (§3.1.2.1-3).

    The app uses the same rounding: `floor(lat/90*(2^31-1))`, `floor(lon/180*(2^31-1))`; `None` = not configured.
    """
    if latitude is None:
        lat = LOCATION_NOT_CONFIGURED
    elif -90 <= latitude <= 90:
        lat = math.floor(latitude / 90 * 0x7FFFFFFF)
    else:
        raise ValueError(f"latitude {latitude} is outside -90..90")
    if longitude is None:
        lon = LOCATION_NOT_CONFIGURED
    elif -180 <= longitude <= 180:
        lon = math.floor(longitude / 180 * 0x7FFFFFFF)
    else:
        raise ValueError(f"longitude {longitude} is outside -180..180")
    if altitude is None:
        alt = ALTITUDE_NOT_CONFIGURED
    elif altitude >= ALTITUDE_TOO_HIGH:
        alt = ALTITUDE_TOO_HIGH
    elif altitude >= -32768:
        alt = altitude
    else:
        raise ValueError(f"altitude {altitude} is below -32768 m")
    return lat, lon, alt


def generic_location_global_set(
    latitude: float | None,
    longitude: float | None,
    altitude: int | None = None,
    ack: bool = False,
) -> bytes:
    """Build Generic Location Global Set (Unacknowledged by default, as the app sends it for astro schedules).

    Layout: `[latitude s32][longitude s32][altitude s16]`, LE; degrees in, `None` = not configured.
    """
    lat, lon, alt = location_global_fields(latitude, longitude, altitude)
    return (
        encode_opcode(GEN_LOCATION_GLOBAL_SET if ack else GEN_LOCATION_GLOBAL_SET_UNACK)
        + lat.to_bytes(4, "little", signed=True)
        + lon.to_bytes(4, "little", signed=True)
        + alt.to_bytes(2, "little", signed=True)
    )


_PROPERTY_MODEL_OPS = {  # kind -> (Get, Set, Set Unack, Status)
    "manufacturer": (
        GEN_MANU_PROP_GET,
        GEN_MANU_PROP_SET,
        GEN_MANU_PROP_SET_UNACK,
        GEN_MANU_PROP_STATUS,
    ),
    "admin": (
        GEN_ADMIN_PROP_GET,
        GEN_ADMIN_PROP_SET,
        GEN_ADMIN_PROP_SET_UNACK,
        GEN_ADMIN_PROP_STATUS,
    ),
    "user": (
        GEN_USER_PROP_GET,
        GEN_USER_PROP_SET,
        GEN_USER_PROP_SET_UNACK,
        GEN_USER_PROP_STATUS,
    ),
}
SIG_PROP_GET = {
    ops[0]: f"Generic {kind.capitalize()} Property Get"
    for kind, ops in _PROPERTY_MODEL_OPS.items()
}
# 0x46 / 0x4A / 0x4E (Mesh Model spec §7.1: the answers to 0x822B / 0x822D / 0x822F and to the Sets)
SIG_PROP_STATUS = {
    ops[3]: f"Generic {kind.capitalize()} Property Status"
    for kind, ops in _PROPERTY_MODEL_OPS.items()
}


def generic_property_get(model_kind: str, property_id: int) -> bytes:
    """SIG Generic Manufacturer / Admin / User Property Get (0x822B / 0x822D / 0x822F): `[property id u16]`."""
    return encode_opcode(_PROPERTY_MODEL_OPS[model_kind][0]) + _field(
        property_id, 2, "property id"
    )


def generic_property_set(
    model_kind: str,
    property_id: int,
    value: bytes = b"",
    user_access: int = 3,
    ack: bool = True,
) -> bytes:
    """SIG Generic Manufacturer / Admin / User Property Set (0x44 / 0x48 / 0x4C; +1 for Unacknowledged).

    Admin: `[pid][user access][value]`; Manufacturer: `[pid][user access]` (no value); User: `[pid][value]`.
    """
    _, op, op_unack, _ = _PROPERTY_MODEL_OPS[model_kind]
    if not 0 <= user_access <= 3:
        raise ValueError(f"user access {user_access} is not 0..3")
    p = _field(property_id, 2, "property id")
    if model_kind == "admin":
        p += bytes([user_access]) + value
    elif model_kind == "manufacturer":
        p += bytes([user_access])
    else:
        p += value
    return encode_opcode(op if ack else op_unack) + p


def light_lightness_default_get() -> bytes:
    """Build Light Lightness Default Get."""
    return encode_opcode(LIGHT_LIGHTNESS_DEFAULT_GET)


def light_lightness_default_set(lightness: int, ack: bool = True) -> bytes:
    """Build Light Lightness Default Set (Unacknowledged unless `ack`): `[lightness u16]` (0 = use last)."""
    return encode_opcode(
        LIGHT_LIGHTNESS_DEFAULT_SET if ack else LIGHT_LIGHTNESS_DEFAULT_SET_UNACK
    ) + _field(lightness, 2, "lightness")


def light_lightness_range_get() -> bytes:
    """Build Light Lightness Range Get."""
    return encode_opcode(LIGHT_LIGHTNESS_RANGE_GET)


def light_lightness_range_set(minimum: int, maximum: int, ack: bool = True) -> bytes:
    """Build Light Lightness Range Set (Unacknowledged unless `ack`): `[min u16][max u16]`, 1 <= min <= max."""
    if not 1 <= minimum <= maximum <= 0xFFFF:
        raise ValueError(f"lightness range {minimum}..{maximum} is not 1 <= min <= max")
    return (
        encode_opcode(
            LIGHT_LIGHTNESS_RANGE_SET if ack else LIGHT_LIGHTNESS_RANGE_SET_UNACK
        )
        + minimum.to_bytes(2, "little")
        + maximum.to_bytes(2, "little")
    )


def light_ctl_default_get() -> bytes:
    """Build Light CTL Default Get."""
    return encode_opcode(LIGHT_CTL_DEFAULT_GET)


def light_ctl_default_set(
    lightness: int, temperature_k: int, delta_uv: int = 0, ack: bool = True
) -> bytes:
    """Build Light CTL Default Set (Unacknowledged unless `ack`): `[lightness u16][temperature u16][delta UV s16]`."""
    _check_temperature(temperature_k)
    return (
        encode_opcode(LIGHT_CTL_DEFAULT_SET if ack else LIGHT_CTL_DEFAULT_SET_UNACK)
        + _field(lightness, 2, "lightness")
        + _field(temperature_k, 2, "temperature")
        + _field(delta_uv, 2, "delta UV", signed=True)
    )


def light_ctl_temperature_range_get() -> bytes:
    """Build Light CTL Temperature Range Get (0x8262)."""
    return encode_opcode(LIGHT_CTL_TEMP_RANGE_GET)


def light_ctl_temperature_range_set(
    minimum_k: int, maximum_k: int, ack: bool = True
) -> bytes:
    """Build Light CTL Temperature Range Set (Unacknowledged unless `ack`): `[min u16][max u16]` in kelvin."""
    _check_temperature(minimum_k)
    _check_temperature(maximum_k)
    if minimum_k > maximum_k:
        raise ValueError(f"temperature range {minimum_k}..{maximum_k} K has min > max")
    return (
        encode_opcode(
            LIGHT_CTL_TEMP_RANGE_SET if ack else LIGHT_CTL_TEMP_RANGE_SET_UNACK
        )
        + minimum_k.to_bytes(2, "little")
        + maximum_k.to_bytes(2, "little")
    )


def _check_temperature(kelvin: int) -> None:
    if not 0x0320 <= kelvin <= 0x4E20:
        raise ValueError(f"colour temperature {kelvin} K is outside 800..20000")


# ----------------------------------------------------------------------------- SIG decoders (roadmap 0.2)


def _int(p: bytes, i: int, n: int, signed: bool = False) -> int:
    """LE integer of `n` bytes at offset `i`; a short PDU raises (→ `describe()` prints `?? <hex>`)."""
    if len(p) < i + n:
        raise ValueError("truncated PDU")
    return int.from_bytes(p[i : i + n], "little", signed=signed)


def _u16(p: bytes, i: int = 0) -> int:
    return _int(p, i, 2)


def _s16(p: bytes, i: int = 0) -> int:
    return _int(p, i, 2, signed=True)


def _set_tail(p: bytes, i: int) -> str:
    """` tid=N` plus ` transition=… delay=…ms` when the optional pair follows byte `i`."""
    s = f" tid={p[i]}"
    if len(p) >= i + 3:
        s += f" transition={_time(p[i + 1])} delay={p[i + 2] * 5}ms"
    return s


# A load's own step, what it rounds a Set to (1 % of the lightness / level range, 100 K): a Status within it of the
# requested value shows the Set applied.
STATE_STEP, KELVIN_STEP = 0x0290, 100
# acknowledged load Set → (size of its Status's present block, the state's fields as (offset, size, signed, step)):
# the fields sit at the same offsets in the Set and in the Status's present block, and again in its target block
# (Mesh Model spec §3.2.1.4, §3.2.2.5, §6.3.1.4, §6.3.2.4, §6.3.2.8). The CTL Temperature Status's delta UV is
# not compared: a light without one reports its own.
_SET_STATES: dict[int, tuple[int, tuple[tuple[int, int, bool, int], ...]]] = {
    GEN_ONOFF_SET: (1, ((0, 1, False, 0),)),
    GEN_LEVEL_SET: (2, ((0, 2, True, STATE_STEP),)),
    LIGHT_LIGHTNESS_SET: (2, ((0, 2, False, STATE_STEP),)),
    LIGHT_CTL_SET: (4, ((0, 2, False, STATE_STEP), (2, 2, False, KELVIN_STEP))),
    LIGHT_CTL_TEMP_SET: (4, ((0, 2, False, KELVIN_STEP),)),
}


def set_shown_by(access_pdu: bytes) -> Callable[[bytes], bool] | None:
    """For an acknowledged load Set, a test of a Status's parameters: whether they show the state the Set asks for.

    A Status answering an acknowledged Set reports the present state, and the target with the remaining time
    while a transition runs: the Set took effect when either is the requested one, within the load's own step
    (`STATE_STEP`, `KELVIN_STEP`). A Status reporting another state at rest did not come from the Set: a Get to
    the same element answered with the old state while the Set was lost on the air (review-4 D32) — or the load
    clamped the value (a lightness under its range minimum), which only the caller can tell. None for any other
    message (an Unacknowledged Set, a Get, a scene or property Set): there is no state to compare.
    """
    try:
        opcode, _cid, params = decode_opcode(access_pdu)
    except ValueError:
        return None
    spec = _SET_STATES.get(opcode)
    if spec is None or len(params) < spec[0]:
        return None
    block, fields = spec

    def values(p: bytes, at: int) -> list[int]:
        return [
            int.from_bytes(p[at + i : at + i + n], "little", signed=signed)
            for i, n, signed, _ in fields
        ]

    wanted = values(params, 0)

    def near(p: bytes, at: int) -> bool:
        return all(
            abs(value - want) <= step
            for value, want, (_, _, _, step) in zip(
                values(p, at), wanted, fields, strict=True
            )
        )

    def shown(status: bytes) -> bool:
        if len(status) < block:
            return False
        # present, or the target with its remaining time after it
        return near(status, 0) or (len(status) > 2 * block and near(status, block))

    return shown


def _describe_level_set(op: int, p: bytes) -> str:
    return f"Generic Level Set{'' if op == GEN_LEVEL_SET else ' Unack'} level={_s16(p)}{_set_tail(p, 2)}"


def _describe_delta_set(op: int, p: bytes) -> str:
    return f"Generic Delta Set{'' if op == GEN_DELTA_SET else ' Unack'} delta={_int(p, 0, 4, signed=True)}{_set_tail(p, 4)}"


def _describe_move_set(op: int, p: bytes) -> str:
    return f"Generic Move Set{'' if op == GEN_MOVE_SET else ' Unack'} delta={_s16(p)}{_set_tail(p, 2)}"


def _describe_onpowerup_set(op: int, p: bytes) -> str:
    return f"OnPowerUp Set{'' if op == GEN_ONPOWERUP_SET else ' Unack'} {p[0]} ({ONPOWERUP_NAMES.get(p[0], '?')})"


def _describe_ctl_temperature_set(op: int, p: bytes) -> str:
    return f"Light CTL Temperature Set{'' if op == LIGHT_CTL_TEMP_SET else ' Unack'} t={_u16(p)}K deltaUV={_s16(p, 2)}{_set_tail(p, 4)}"


def battery_status(p: bytes) -> dict[str, int | str | None]:
    """Decode Generic Battery Status: level %, minutes to discharge / charge (None = unknown) and the four flags.

    A level above 100 is unknown too: 0xFF says so, and 0x65-0xFE are prohibited values, not percentages.
    """
    if len(p) < 8:
        raise ValueError(f"Generic Battery Status needs 8 bytes, got {len(p)}")
    level, discharge, charge, flags = p[0], _int(p, 1, 3), _int(p, 4, 3), p[7]
    return {
        "level": level if level <= BATTERY_MAX_LEVEL else None,
        "discharge_minutes": None if discharge == BATTERY_UNKNOWN_TIME else discharge,
        "charge_minutes": None if charge == BATTERY_UNKNOWN_TIME else charge,
        "presence": BATTERY_PRESENCE[flags & 3],
        "indicator": BATTERY_INDICATOR[(flags >> 2) & 3],
        "charging": BATTERY_CHARGING[(flags >> 4) & 3],
        "serviceability": BATTERY_SERVICEABILITY[(flags >> 6) & 3],
    }


def _describe_battery_status(op: int, p: bytes) -> str:
    b = battery_status(p)
    return (
        f"Generic Battery Status level={b['level'] if b['level'] is not None else '?'}%"
        f" discharge={b['discharge_minutes'] if b['discharge_minutes'] is not None else '?'}min"
        f" charge={b['charge_minutes'] if b['charge_minutes'] is not None else '?'}min"
        f" presence={b['presence']} indicator={b['indicator']} charging={b['charging']}"
        f" serviceability={b['serviceability']}"
    )


def location_global(p: bytes) -> tuple[float | None, float | None, int | None]:
    """Decode the Global Latitude / Longitude / Altitude fields back to degrees and metres (None = not configured)."""
    lat, lon, alt = _int(p, 0, 4, signed=True), _int(p, 4, 4, signed=True), _s16(p, 8)
    return (
        None if lat == LOCATION_NOT_CONFIGURED else lat / 0x7FFFFFFF * 90,
        None if lon == LOCATION_NOT_CONFIGURED else lon / 0x7FFFFFFF * 180,
        None if alt == ALTITUDE_NOT_CONFIGURED else alt,
    )


def _describe_location(op: int, p: bytes) -> str:
    name = {
        GEN_LOCATION_GLOBAL_STATUS: "Generic Location Global Status",
        GEN_LOCATION_GLOBAL_SET: "Generic Location Global Set",
        GEN_LOCATION_GLOBAL_SET_UNACK: "Generic Location Global Set Unack",
    }[op]
    lat, lon, alt = location_global(p)
    return (
        f"{name} lat={'?' if lat is None else f'{lat:.6f}'} lon={'?' if lon is None else f'{lon:.6f}'}"
        f" alt={'?' if alt is None else f'{alt}m'}"
    )


# LBC vendor property opcodes by payload layout (docs/android/vendor-models.md §3.3): Gets carry `[pid]`, every
# Status and the Admin Sets `[pid][userAccess][value]`, the Manufacturer / User Sets `[pid][value]`.
_VENDOR_PROP_GET = frozenset(ops[2] for ops in _LBC_OPS.values())
_VENDOR_PROP_WITH_ACCESS = frozenset(
    {_LBC_OPS["admin"][3], _LBC_OPS["admin"][4]} | {ops[5] for ops in _LBC_OPS.values()}
)
_VENDOR_PROP_VALUE = frozenset(
    ops[i] for kind, ops in _LBC_OPS.items() if kind != "admin" for i in (3, 4)
)
_VENDOR_PROP_OPS = _VENDOR_PROP_GET | _VENDOR_PROP_WITH_ACCESS | _VENDOR_PROP_VALUE


def _property_spec(pid: int, *, sig: bool) -> properties.PropertySpec | None:
    """Look a property id up in the catalogue of the server the opcode addresses, and only there.

    SIG and LBC ids overlap (0x000C, 0x000D, 0x0010, 0x0011 name different things) and an id one catalogue lacks
    means something else on the other server, so falling back to the other catalogue labelled logs wrongly.
    """
    return (properties.SIG_PROPERTIES if sig else properties.PROPERTIES).get(pid)


def _property_text(
    pid: int, body: bytes | None, *, access: bool = False, sig: bool = False
) -> str:
    """`prop 0x5003 access=3 value=06 key_mode=gateway`: id, raw value and the catalogue's decoding of it.

    `body` is what follows the property id (None for a Get: the id alone, with the property's name when the
    catalogue knows it); with `access` its first byte is the userAccess field. The decoded `name=value` comes
    from `properties.describe_status`, so the CLI and the logs read the same way as the config entities; ids
    the catalogue does not know show the raw hex only. A secret property (the gateway API token 0xC001) never
    shows its bytes: `value=<redacted> gateway_api_token=<redacted>`.
    """
    spec = _property_spec(pid, sig=sig)
    # a credential stays hidden whichever catalogue names it: an LBC id sent to a SIG server is still the token
    secret = any(
        c.get(pid) is not None and c[pid].secret
        for c in (properties.PROPERTIES, properties.SIG_PROPERTIES)
    )
    text = f"prop 0x{pid:04X}"
    if body is None:
        return text + (f" {spec.name}" if spec else "")
    if access:
        text += f" access={body[0] if body else '?'}"
        body = body[1:]
    text += f" value={properties.REDACTED if secret else body.hex()}"
    if spec is not None:
        text += f" {properties.describe_status(pid, body, sig=sig)}"
    return text


def _describe_vendor_property(op: int, p: bytes) -> str:
    """Parameters of an LBC vendor property message (`_VENDOR_PROP_OPS`), which all start with the property id."""
    pid = int.from_bytes(p[:2], "little")
    if op in _VENDOR_PROP_GET:
        return _property_text(pid, None)
    return _property_text(pid, p[2:], access=op in _VENDOR_PROP_WITH_ACCESS)


def _describe_property_set(op: int, p: bytes) -> str:
    kind = next(k for k, ops in _PROPERTY_MODEL_OPS.items() if op in (ops[1], ops[2]))
    unack = "" if op == _PROPERTY_MODEL_OPS[kind][1] else " Unack"
    pid = _u16(p)
    name = f"Generic {kind.capitalize()} Property Set{unack}"
    if kind == "manufacturer":  # `[pid][user access]`, no value
        return f"{name} {_property_text(pid, None, sig=True)} access={p[2]}"
    if kind == "admin" and len(p) < 3:
        raise IndexError("Admin Property Set without the user access byte")
    return f"{name} {_property_text(pid, p[2:], access=kind == 'admin', sig=True)}"


def _describe_range_status(op: int, p: bytes) -> str:
    name = (
        "Light Lightness Range Status"
        if op == LIGHT_LIGHTNESS_RANGE_STATUS
        else "Light CTL Temperature Range Status"
    )
    unit = "" if op == LIGHT_LIGHTNESS_RANGE_STATUS else "K"
    return f"{name} status={p[0]} ({RANGE_STATUS_CODES.get(p[0], '?')}) min={_u16(p, 1)}{unit} max={_u16(p, 3)}{unit}"


def _describe_range_set(op: int, p: bytes) -> str:
    if op in (LIGHT_LIGHTNESS_RANGE_SET, LIGHT_LIGHTNESS_RANGE_SET_UNACK):
        return f"Light Lightness Range Set{'' if op == LIGHT_LIGHTNESS_RANGE_SET else ' Unack'} min={_u16(p)} max={_u16(p, 2)}"
    return f"Light CTL Temperature Range Set{'' if op == LIGHT_CTL_TEMP_RANGE_SET else ' Unack'} min={_u16(p)}K max={_u16(p, 2)}K"


def _describe_lightness_default(op: int, p: bytes) -> str:
    name = {
        LIGHT_LIGHTNESS_DEFAULT_STATUS: "Light Lightness Default Status",
        LIGHT_LIGHTNESS_DEFAULT_SET: "Light Lightness Default Set",
        LIGHT_LIGHTNESS_DEFAULT_SET_UNACK: "Light Lightness Default Set Unack",
    }[op]
    return f"{name} {_u16(p)}"


def _describe_ctl_default(op: int, p: bytes) -> str:
    name = {
        LIGHT_CTL_DEFAULT_STATUS: "Light CTL Default Status",
        LIGHT_CTL_DEFAULT_SET: "Light CTL Default Set",
        LIGHT_CTL_DEFAULT_SET_UNACK: "Light CTL Default Set Unack",
    }[op]
    return f"{name} l={_u16(p)} t={_u16(p, 2)}K deltaUV={_s16(p, 4)}"


@dataclass(frozen=True)
class SensorDescriptor:
    """One entry of a Sensor Descriptor Status (Mesh Model spec §4.1.1): what the sensor server measures and how."""

    property_id: int
    positive_tolerance: int  # 12 bits, in units of 100 / 4095 %
    negative_tolerance: int
    sampling_function: int
    measurement_period: int  # 0 = not applicable, else 1.1^(n-64) s
    update_interval: int  # same encoding

    @staticmethod
    def seconds(encoded: int) -> float | None:
        """Decode a period / interval byte; None for "not applicable"."""
        return None if encoded == 0 else round(1.1 ** (encoded - 64), 2)


def sensor_descriptors(params: bytes) -> list[SensorDescriptor]:
    """Parse a Sensor Descriptor Status; a 2-byte status names a property the element does not have (empty list)."""
    out = []
    for i in range(0, len(params) - 7, 8):
        entry = params[i : i + 8]
        tolerances = int.from_bytes(entry[2:5], "little")
        out.append(
            SensorDescriptor(
                int.from_bytes(entry[:2], "little"),
                tolerances & 0xFFF,
                tolerances >> 12,
                entry[5],
                entry[6],
                entry[7],
            )
        )
    return out


def _describe_descriptors(op: int, p: bytes) -> str:
    entries = sensor_descriptors(p)
    if not entries:
        return f"Sensor Descriptor Status {_raw(p, False)}"
    parts = []
    for d in entries:
        interval = SensorDescriptor.seconds(d.update_interval)
        parts.append(
            f"0x{d.property_id:04X} tol=+{d.positive_tolerance}/-{d.negative_tolerance} func={d.sampling_function}"
            f" interval={'n/a' if interval is None else f'{interval:g}s'}"
        )
    return "Sensor Descriptor Status " + "; ".join(parts)


def _describe_property_list(op: int, p: bytes) -> str:
    name = {
        GEN_MANU_PROPS_STATUS: "Generic Manufacturer Properties Status",
        GEN_ADMIN_PROPS_STATUS: "Generic Admin Properties Status",
        GEN_USER_PROPS_STATUS: "Generic User Properties Status",
    }[op]
    return f"{name} {_property_list_text(property_ids(p), sig=True)}"


def _property_list_text(ids: list[int], *, sig: bool) -> str:
    def one(pid: int) -> str:
        spec = _property_spec(pid, sig=sig)
        return f"{pid:04X}" + (f"={spec.name}" if spec else "")

    return f"[{', '.join(one(pid) for pid in ids)}]" if ids else "[]"


def _describe_health_fault(op: int, p: bytes) -> str:
    name = (
        "Health Current Status"
        if op == HEALTH_CURRENT_STATUS
        else "Health Fault Status"
    )
    if len(p) < 3:
        return f"{name} {_raw(p, False)}"
    status = decode_health_fault_status(p)
    faults = ", ".join(health_fault_name(f) for f in status.faults) or "none"
    return f"{name} test={status.test_id} company={status.company_id:04X} faults=[{faults}]"


def _describe_health_fault_request(op: int, p: bytes) -> str:
    name = {
        HEALTH_FAULT_GET: "Health Fault Get",
        HEALTH_FAULT_CLEAR: "Health Fault Clear",
        HEALTH_FAULT_CLEAR_UNACK: "Health Fault Clear Unack",
        HEALTH_FAULT_TEST: "Health Fault Test",
        HEALTH_FAULT_TEST_UNACK: "Health Fault Test Unack",
    }[op]
    test = f" test={p[0]}" if op in (HEALTH_FAULT_TEST, HEALTH_FAULT_TEST_UNACK) else ""
    p = p[1:] if test else p
    return f"{name}{test} company={int.from_bytes(p[:2], 'little'):04X}"


def _describe_time_zone(op: int, p: bytes) -> str:
    if len(p) < 7:
        return f"Time Zone Status {_raw(p, False)}"
    current, new = (p[0] - 64) * 15, (p[1] - 64) * 15
    return f"Time Zone Status current={current:+d}min new={new:+d}min change_tai={int.from_bytes(p[2:7], 'little')}"


def _describe_tai_delta(op: int, p: bytes) -> str:
    if len(p) < 9:
        return f"TAI-UTC Delta Status {_raw(p, False)}"
    current = (int.from_bytes(p[:2], "little") & 0x7FFF) - 255
    new = (int.from_bytes(p[2:4], "little") & 0x7FFF) - 255
    return f"TAI-UTC Delta Status current={current}s new={new}s change_tai={int.from_bytes(p[4:9], 'little')}"


def _describe_time_role(op: int, p: bytes) -> str:
    name = {TIME_ROLE_SET: "Time Role Set", TIME_ROLE_STATUS: "Time Role Status"}[op]
    if not p:
        return name
    return f"{name} {TIME_ROLES.get(p[0], str(p[0]))}"


def _describe_attention(op: int, p: bytes) -> str:
    name = {
        HEALTH_ATTENTION_SET: "Health Attention Set",
        HEALTH_ATTENTION_SET_UNACK: "Health Attention Set Unack",
        HEALTH_ATTENTION_STATUS: "Health Attention Status",
    }[op]
    return f"{name} {p[0]}s" if p else name


_SIG_GETS = {
    GEN_MANU_PROPS_GET: "Generic Manufacturer Properties Get",
    GEN_ADMIN_PROPS_GET: "Generic Admin Properties Get",
    GEN_USER_PROPS_GET: "Generic User Properties Get",
    SENSOR_DESCRIPTOR_GET: "Sensor Descriptor Get",
    GEN_DTT_GET: "Default Transition Time Get",
    TIME_ROLE_GET: "Time Role Get",
    TIME_ZONE_GET: "Time Zone Get",
    TAI_UTC_DELTA_GET: "TAI-UTC Delta Get",
    HEALTH_ATTENTION_GET: "Health Attention Get",
    HEALTH_PERIOD_GET: "Health Period Get",
    GEN_LEVEL_GET: "Generic Level Get",
    GEN_ONPOWERUP_GET: "OnPowerUp Get",
    GEN_BATTERY_GET: "Generic Battery Get",
    GEN_LOCATION_GLOBAL_GET: "Generic Location Global Get",
    LIGHT_LIGHTNESS_DEFAULT_GET: "Light Lightness Default Get",
    LIGHT_LIGHTNESS_RANGE_GET: "Light Lightness Range Get",
    LIGHT_CTL_DEFAULT_GET: "Light CTL Default Get",
    LIGHT_CTL_TEMP_RANGE_GET: "Light CTL Temperature Range Get",
    LIGHT_CTL_TEMP_GET: "Light CTL Temperature Get",
}
_SIG_DESCRIBERS: dict[int, Callable[[int, bytes], str]] = {
    GEN_LEVEL_SET: _describe_level_set,
    GEN_LEVEL_SET_UNACK: _describe_level_set,
    GEN_DELTA_SET: _describe_delta_set,
    GEN_DELTA_SET_UNACK: _describe_delta_set,
    GEN_MOVE_SET: _describe_move_set,
    GEN_MOVE_SET_UNACK: _describe_move_set,
    GEN_ONPOWERUP_SET: _describe_onpowerup_set,
    GEN_ONPOWERUP_SET_UNACK: _describe_onpowerup_set,
    LIGHT_CTL_TEMP_SET: _describe_ctl_temperature_set,
    LIGHT_CTL_TEMP_SET_UNACK: _describe_ctl_temperature_set,
    GEN_BATTERY_STATUS: _describe_battery_status,
    GEN_LOCATION_GLOBAL_STATUS: _describe_location,
    GEN_LOCATION_GLOBAL_SET: _describe_location,
    GEN_LOCATION_GLOBAL_SET_UNACK: _describe_location,
    GEN_MANU_PROP_SET: _describe_property_set,
    GEN_MANU_PROP_SET_UNACK: _describe_property_set,
    GEN_ADMIN_PROP_SET: _describe_property_set,
    GEN_ADMIN_PROP_SET_UNACK: _describe_property_set,
    GEN_USER_PROP_SET: _describe_property_set,
    GEN_USER_PROP_SET_UNACK: _describe_property_set,
    LIGHT_LIGHTNESS_RANGE_STATUS: _describe_range_status,
    LIGHT_CTL_TEMP_RANGE_STATUS: _describe_range_status,
    LIGHT_LIGHTNESS_RANGE_SET: _describe_range_set,
    LIGHT_LIGHTNESS_RANGE_SET_UNACK: _describe_range_set,
    LIGHT_CTL_TEMP_RANGE_SET: _describe_range_set,
    LIGHT_CTL_TEMP_RANGE_SET_UNACK: _describe_range_set,
    LIGHT_LIGHTNESS_DEFAULT_STATUS: _describe_lightness_default,
    LIGHT_LIGHTNESS_DEFAULT_SET: _describe_lightness_default,
    LIGHT_LIGHTNESS_DEFAULT_SET_UNACK: _describe_lightness_default,
    LIGHT_CTL_DEFAULT_STATUS: _describe_ctl_default,
    LIGHT_CTL_DEFAULT_SET: _describe_ctl_default,
    LIGHT_CTL_DEFAULT_SET_UNACK: _describe_ctl_default,
    GEN_MANU_PROPS_STATUS: _describe_property_list,
    GEN_ADMIN_PROPS_STATUS: _describe_property_list,
    GEN_USER_PROPS_STATUS: _describe_property_list,
    SENSOR_DESCRIPTOR_STATUS: _describe_descriptors,
    HEALTH_FAULT_STATUS: _describe_health_fault,
    HEALTH_CURRENT_STATUS: _describe_health_fault,
    HEALTH_FAULT_GET: _describe_health_fault_request,
    HEALTH_FAULT_CLEAR: _describe_health_fault_request,
    HEALTH_FAULT_CLEAR_UNACK: _describe_health_fault_request,
    HEALTH_FAULT_TEST: _describe_health_fault_request,
    HEALTH_FAULT_TEST_UNACK: _describe_health_fault_request,
    TIME_ZONE_STATUS: _describe_time_zone,
    TAI_UTC_DELTA_STATUS: _describe_tai_delta,
    TIME_ROLE_SET: _describe_time_role,
    TIME_ROLE_STATUS: _describe_time_role,
    HEALTH_ATTENTION_SET: _describe_attention,
    HEALTH_ATTENTION_SET_UNACK: _describe_attention,
    HEALTH_ATTENTION_STATUS: _describe_attention,
}


def _describe_sig_extra(op: int, p: bytes) -> str | None:
    """Describe the roadmap-0.2 SIG messages: table-driven so `_describe` stays one branch per legacy message."""
    if op in _SIG_GETS:
        return _SIG_GETS[op]
    fn = _SIG_DESCRIBERS.get(op)
    return fn(op, p) if fn else None


def vendor_property_get(model_kind: str, property_id: int) -> bytes:
    """model_kind: 'admin' | 'manufacturer' | 'user' — LBC vendor property Get (propertyId u16 LE)."""
    return encode_opcode(_LBC_OPS[model_kind][2], JUNG_CID) + _field(
        property_id, 2, "property id"
    )


# LBC "Properties Get" (no parameters) → "Properties Status" (property ids u16 LE), per server: `C0/C1`, `C6/C7`,
# `CC/CD 27 05`. Inferred from the gateway firmware's opcode table (docs/cross-repo-analysis.md §1.3) and answered by
# every node asked (docs/hidden-features.md §2) — the app never sends them.
VENDOR_PROPERTY_LIST_OPCODES: dict[str, tuple[int, int]] = {
    kind: (ops[0], ops[1]) for kind, ops in _LBC_OPS.items()
}
_VENDOR_LIST_STATUS_OPS = {
    status for _get, status in VENDOR_PROPERTY_LIST_OPCODES.values()
}


def vendor_properties_get(model_kind: str) -> bytes:
    """LBC vendor "Properties Get": the list of property ids a server holds."""
    return encode_opcode(VENDOR_PROPERTY_LIST_OPCODES[model_kind][0], JUNG_CID)


def generic_properties_get(model_kind: str) -> bytes:
    """SIG Generic Manufacturer / Admin / User Properties Get (no parameters)."""
    return encode_opcode(SIG_PROPERTY_LIST_OPCODES[model_kind][0])


def property_ids(params: bytes) -> list[int]:
    """Return the property ids of a (vendor or SIG) Properties Status: u16 LE each, a trailing odd byte is ignored."""
    return [
        int.from_bytes(params[i : i + 2], "little")
        for i in range(0, len(params) - 1, 2)
    ]


def health_attention_set(seconds: int, *, ack: bool = True) -> bytes:
    """Health Attention Set: make the node draw attention to itself (its LED blinks) for `seconds` (0 = stop, ≤ 255).

    Accepted by JUNG nodes (Attention Status counts the seconds down, `docs/hidden-features.md` §3); the app only
    uses attention during provisioning.
    """
    if not 0 <= seconds <= 0xFF:
        raise ValueError(f"attention {seconds} s is not 0..255")
    return encode_opcode(
        HEALTH_ATTENTION_SET if ack else HEALTH_ATTENTION_SET_UNACK
    ) + bytes([seconds])


@dataclass(frozen=True)
class HealthFaults:
    """A Health Fault / Current Status: the most recent test id, the company the faults belong to and their ids."""

    test_id: int
    company_id: int
    faults: tuple[int, ...]


def decode_health_fault_status(params: bytes) -> HealthFaults:
    """Decode Health Fault Status / Health Current Status: `[test id][company id u16][fault ids…]`."""
    if len(params) < 3:
        raise ValueError(f"Health Fault Status needs 3 bytes, got {len(params)}")
    return HealthFaults(
        params[0], int.from_bytes(params[1:3], "little"), tuple(params[3:])
    )


def health_fault_name(fault: int) -> str:
    """`0x81 (vendor)` for a vendor fault, `0x01 battery low warning` for a SIG one."""
    if fault >= HEALTH_FAULT_VENDOR_MIN:
        return f"0x{fault:02X} (vendor)"
    return f"0x{fault:02X} {HEALTH_FAULT_NAMES.get(fault, 'reserved')}"


def health_fault_get(company_id: int = JUNG_CID) -> bytes:
    """Health Fault Get: the registered faults of `company_id` (the node answers for its own company only)."""
    return encode_opcode(HEALTH_FAULT_GET) + _field(company_id, 2, "company id")


def health_fault_clear(company_id: int = JUNG_CID, *, ack: bool = True) -> bytes:
    """Health Fault Clear: forget the registered faults of `company_id`.

    `ack` picks Health Fault Clear (`0x802F`, answered with a Fault Status) over Health Fault Clear
    Unacknowledged (`0x8030`). Whether JUNG nodes answer the acknowledged form is not observed yet — the note in
    `docs/hidden-features.md` §10 that they do not was taken with the two opcodes swapped; `health_fault_get`
    reads the result back either way.
    """
    return encode_opcode(
        HEALTH_FAULT_CLEAR if ack else HEALTH_FAULT_CLEAR_UNACK
    ) + _field(company_id, 2, "company id")


def health_fault_test(
    test_id: int, company_id: int = JUNG_CID, *, ack: bool = True
) -> bytes:
    """Health Fault Test: run self-test `test_id` (company-specific) and report the registered faults.

    On JUNG nodes every test id is accepted and reports no fault; the id becomes the "most recent test" the
    later Fault Statuses carry (test 0 answers with an explicit *no fault* entry).
    """
    if not 0 <= test_id <= 0xFF:
        raise ValueError(f"test id {test_id} is not 0..255")
    return (
        encode_opcode(HEALTH_FAULT_TEST if ack else HEALTH_FAULT_TEST_UNACK)
        + bytes([test_id])
        + _field(company_id, 2, "company id")
    )


def sensor_descriptor_get(property_id: int | None = None) -> bytes:
    """Sensor Descriptor Get: every sensor of the element, or one property."""
    return encode_opcode(SENSOR_DESCRIPTOR_GET) + (
        b"" if property_id is None else _field(property_id, 2, "property id")
    )


# LBC vendor property Set opcodes per server: (acknowledged, unacknowledged) — docs/cross-repo-analysis.md §1.3.
VENDOR_PROPERTY_SET_OPCODES: dict[str, tuple[int, int]] = {
    kind: (ops[3], ops[4]) for kind, ops in _LBC_OPS.items()
}


def vendor_property_set(
    kind: Literal["admin", "manufacturer", "user"],
    property_id: int,
    value: bytes,
    *,
    ack: bool = True,
    user_access: int = 3,
) -> bytes:
    """LBC vendor property Set (Unacknowledged unless `ack`): `C3/C4`, `C9/CA`, `CF/D0` + `27 05`.

    Admin: `[propertyId u16 LE][userAccess u8][value…]` — the app sends 3 (READ_AND_WRITE) but 1 for DimMode, and
    on air for DeviceLock and the delays 0x1001 / 0x1002 / 0x1007 (`PropertySpec.set_access` holds the byte).
    Manufacturer / User: `[propertyId u16 LE][value…]` — no access byte (docs/android/vendor-models.md §3.3).
    """
    op_ack, op_unack = VENDOR_PROPERTY_SET_OPCODES[kind]
    if not 0 <= user_access <= 3:
        raise ValueError(f"user access {user_access} is not 0..3")
    head = _field(property_id, 2, "property id")
    if kind == "admin":
        head += bytes([user_access])
    return encode_opcode(op_ack if ack else op_unack, JUNG_CID) + head + value


# LBC vendor property Status opcode per server — docs/cross-repo-analysis.md §1.3.
VENDOR_PROPERTY_STATUS_OPCODES: dict[str, int] = {
    kind: ops[5] for kind, ops in _LBC_OPS.items()
}


def vendor_property_status(
    kind: Literal["admin", "manufacturer", "user"],
    property_id: int,
    value: bytes,
    *,
    user_access: int = 3,
) -> bytes:
    """LBC vendor property Status `C5 / CB / D1 27 05`: `[propertyId u16 LE][userAccess u8][value…]` for every server.

    Normally the reply to a Get or Set (docs/android/vendor-models.md §3.3), but the gateway also *sends* one as a
    write: it drives a key's status LED (0x5013) with a User Property Status to the key element instead of a Set
    (docs/cross-repo-analysis.md §1.2).
    """
    if not 0 <= user_access <= 3:
        raise ValueError(f"user access {user_access} is not 0..3")
    return (
        encode_opcode(VENDOR_PROPERTY_STATUS_OPCODES[kind], JUNG_CID)
        + _field(property_id, 2, "property id")
        + bytes([user_access])
        + value
    )


def time_set(
    when: datetime,
    zone_offset: timedelta | None = None,
    tai_utc_delta: int = TAI_UTC_DELTA,
    authority: bool = False,
    uncertainty: int = 0,
) -> bytes:
    """Time Set (Mesh Model spec §5.2.1.2), the 10-byte message the app broadcasts to 0xFFFF at start.

    `when` must be timezone-aware; `zone_offset` (15-minute resolution) defaults to its own UTC offset.
    Layout, LSB first: TAI seconds u40 | subsecond u8 (1/256 s) | uncertainty u8 (10 ms) |
    time authority 1 bit + (TAI-UTC delta + 255) 15 bits | zone offset u8 (quarter hours + 64).
    """
    own_offset = when.utcoffset()
    if own_offset is None:
        raise ValueError("time_set needs a timezone-aware datetime")
    zone = own_offset if zone_offset is None else zone_offset
    quarters, rem = divmod(int(zone.total_seconds()), 900)
    if rem or not -64 <= quarters <= 191:
        raise ValueError(
            f"zone offset {zone} is not a multiple of 15 minutes within -16:00..+47:45"
        )
    if not -255 <= tai_utc_delta <= 32512:
        raise ValueError(f"TAI-UTC delta {tai_utc_delta} out of range")
    elapsed = when.astimezone(UTC) - TAI_EPOCH
    tai_seconds = elapsed.days * 86400 + elapsed.seconds + tai_utc_delta
    if not 0 <= tai_seconds < 1 << 40:
        raise ValueError(f"{when.isoformat()} is outside the TAI range")
    subsecond = elapsed.microseconds * 256 // 1_000_000
    delta_field = (1 if authority else 0) | ((tai_utc_delta + 255) << 1)
    return (
        encode_opcode(TIME_SET)
        + tai_seconds.to_bytes(5, "little")
        + bytes([subsecond, uncertainty])
        + delta_field.to_bytes(2, "little")
        + bytes([quarters + 64])
    )


def time_role_get() -> bytes:
    """Time Role Get (Mesh Model spec §5.2.1.10), no parameters: to a node's Time Setup Server (`1201`).

    The app sends it to the node on every opening of its device page (`TimeRoleMessageBuilder`) and each node answers
    with its one-byte role, `client` on air (`823A 03`, the app settings session); a read, harmless.
    """
    return encode_opcode(TIME_ROLE_GET)


def decode_time_role_status(params: bytes) -> str:
    """Decode Time Role Status `[role u8]` to its name (`TIME_ROLES`); a prohibited role (4..255) or none raises."""
    if len(params) < 1 or params[0] not in TIME_ROLES:
        raise ValueError(f"not a Time Role Status: {params.hex() or '(empty)'}")
    return TIME_ROLES[params[0]]


# ----------------------------------------------------------------------------- decoders


def _time(b: int) -> str:
    """Transition-time byte as text; steps 0x3F is "unknown" in a Status (§3.1.3: still moving, no estimate)."""
    steps, res = b & 0x3F, b >> 6
    if steps == TRANSITION_UNKNOWN:
        return "unknown"
    return f"{steps}x{['100ms', '1s', '10s', '10min'][res]}"


def describe(access_pdu: bytes, *, devkey: bool = False) -> str:
    """Human-readable one-liner for an access PDU; never raises (malformed/truncated → `?? <hex>`).

    With `devkey` the PDU travelled under a device key (a Config message, possibly a key refresh carrying the
    new NetKey / AppKey): whatever is not decoded is shown as `<N bytes>` instead of hex, so an unknown opcode
    or a truncated payload can never dump key material into the log.
    """
    try:
        return _describe(access_pdu, devkey)
    except (
        IndexError,
        ValueError,
        OverflowError,
    ):  # OverflowError: arithmetic on a garbage field
        return f"?? {_raw(access_pdu, devkey)}"


def _raw(p: bytes, devkey: bool) -> str:
    """Undecoded bytes for a describe fallback: hex, or only their count for a device-key message."""
    return config_messages.byte_count(p) if devkey else p.hex()


def _describe(access_pdu: bytes, devkey: bool) -> str:  # noqa: C901, PLR0911, PLR0915  # flat opcode → text dispatch, one branch per message
    op, cid, p = decode_opcode(access_pdu)
    if cid is not None:
        name = (
            VENDOR_NAMES.get(op, f"vendor op {op:02X}")
            if cid == JUNG_CID
            else f"vendor {cid:04X} op {op:02X}"
        )
        if cid == JUNG_CID and op in _VENDOR_PROP_OPS and len(p) >= 2:
            return f"{name} {_describe_vendor_property(op, p)}"
        if cid == JUNG_CID and op in _VENDOR_LIST_STATUS_OPS:
            return f"{name} {_property_list_text(property_ids(p), sig=False)}"
        if cid == JUNG_CID:
            model_text = vendor_models.describe_vendor_model(op, p)
            if model_text is not None:
                return f"{name} {model_text}".rstrip()
        return f"{name} {_raw(p, devkey)}".rstrip()
    extra_sig = _describe_sig_extra(op, p)
    if extra_sig is not None:
        return extra_sig
    if op == GEN_ONOFF_STATUS:
        s = f"Generic OnOff Status present={'ON' if p[0] else 'OFF'}"
        if len(p) >= 3:
            s += f" target={'ON' if p[1] else 'OFF'} remaining={_time(p[2])}"
        return s
    if op == GEN_LEVEL_STATUS:
        s = f"Generic Level Status present={_s16(p, 0)}"
        if len(p) >= 5:
            s += f" target={_s16(p, 2)} remaining={_time(p[4])}"
        return s
    if op == LIGHT_LIGHTNESS_STATUS:
        s = f"Light Lightness Status present={_u16(p, 0)}"
        if len(p) >= 5:
            s += f" target={_u16(p, 2)} remaining={_time(p[4])}"
        return s
    if op == LIGHT_CTL_STATUS:
        s = f"Light CTL Status lightness={_u16(p, 0)} temp={_u16(p, 2)}K"
        if len(p) >= 9:
            s += f" target_l={_u16(p, 4)} target_t={_u16(p, 6)} remaining={_time(p[8])}"
        return s
    if op == LIGHT_CTL_TEMP_STATUS:
        s = f"Light CTL Temperature Status temp={_u16(p, 0)}K deltaUV={_s16(p, 2)}"
        if (
            len(p) >= 9
        ):  # §6.3.1.14: optional target temperature, target Delta UV, remaining time
            s += (
                f" target_t={_u16(p, 4)} target_uv={_s16(p, 6)} remaining={_time(p[8])}"
            )
        return s
    if op == SCENE_STATUS:
        st = decode_scene_status(p)
        s = f"Scene Status status={st.status} current={st.current}"
        if st.target is not None and st.remaining is not None:
            s += f" target={st.target} remaining={_time(st.remaining)}"
        return s
    if op == SCENE_REGISTER_STATUS:
        r = decode_scene_register_status(p)
        return f"Scene Register Status status={r.status} current={r.current} scenes={list(r.scenes)}"
    if op == SENSOR_STATUS:
        return f"Sensor Status {_sensor(p)}"
    if (status_name := SIG_PROP_STATUS.get(op)) is not None:
        pid = int.from_bytes(p[:2], "little")
        return f"{status_name} {_property_text(pid, p[2:], access=True, sig=True)}"
    if op in SIG_PROP_GET:
        pid = int.from_bytes(p[:2], "little")
        return f"{SIG_PROP_GET[op]} {_property_text(pid, None, sig=True)}"
    if op == GEN_DTT_STATUS:
        return f"Default Transition Time Status {_time(p[0])}"
    if op == GEN_ONPOWERUP_STATUS:
        return f"OnPowerUp Status {p[0]}"
    if op in (TIME_SET, TIME_STATUS):
        return f"Time {'Set' if op == TIME_SET else 'Status'} {_tai_time(p)}"
    if op == TIME_GET:
        return "Time Get"
    if op in config_messages.CONFIG_NAMES:
        return config_messages.describe_config(op, p, devkey=devkey)
    if op in (GEN_ONOFF_SET, GEN_ONOFF_SET_UNACK):
        return (
            f"Generic OnOff Set{'' if op == GEN_ONOFF_SET else ' Unack'} {'ON' if p[0] else 'OFF'} tid={p[1]}"
            + (f" transition={_time(p[2])} delay={p[3] * 5}ms" if len(p) >= 4 else "")
        )
    if op in (LIGHT_LIGHTNESS_SET, LIGHT_LIGHTNESS_SET_UNACK):
        return f"Light Lightness Set{'' if op == LIGHT_LIGHTNESS_SET else ' Unack'} {int.from_bytes(p[:2], 'little')} tid={p[2]}"
    if op in (LIGHT_CTL_SET, LIGHT_CTL_SET_UNACK):
        return f"Light CTL Set{'' if op == LIGHT_CTL_SET else ' Unack'} l={int.from_bytes(p[:2], 'little')} t={int.from_bytes(p[2:4], 'little')}K tid={p[6]}"
    if op in (SCENE_RECALL, SCENE_RECALL_UNACK):
        return f"Scene Recall{'' if op == SCENE_RECALL else ' Unack'} scene={int.from_bytes(p[:2], 'little')} tid={p[2]}"
    if op == GEN_ONOFF_GET:
        return "Generic OnOff Get"
    if op == LIGHT_LIGHTNESS_GET:
        return "Light Lightness Get"
    if op == LIGHT_CTL_GET:
        return "Light CTL Get"
    if op == SCENE_GET:
        return "Scene Get"
    if op == SCENE_REGISTER_GET:
        return "Scene Register Get"
    if op in (SCENE_STORE, SCENE_STORE_UNACK, SCENE_DELETE, SCENE_DELETE_UNACK):
        name = (
            "Scene Store" if op in (SCENE_STORE, SCENE_STORE_UNACK) else "Scene Delete"
        )
        unack = "" if op in (SCENE_STORE, SCENE_DELETE) else " Unack"
        return f"{name}{unack} scene={int.from_bytes(p[:2], 'little')}"
    if op == SENSOR_GET:
        return "Sensor Get" + (
            f" prop 0x{int.from_bytes(p[:2], 'little'):04X}" if len(p) >= 2 else ""
        )
    return f"SIG op {op:04X} {_raw(p, devkey)}"


def sensor_values(p: bytes) -> list[tuple[int, bytes]]:
    """Unmarshal Sensor Status data (§4.2.14): list of (property_id, raw_value).

    Format A: bit0=0, 4-bit length-1, 11-bit property; Format B: bit0=1, 7-bit length-1, 16-bit property.
    ValueError when an entry ends inside its header or its raw value (the Length field "shall" match the
    value): a status cut short would otherwise yield `(property, b"")` and read as a zero reading.
    """
    out, i = [], 0
    while i < len(p):
        if p[i] & 1 == 0:
            hdr_len = 2
            hdr = int.from_bytes(p[i : i + 2], "little")
            length, prop = ((hdr >> 1) & 0xF) + 1, hdr >> 5
        else:
            hdr_len = 3
            length, prop = (
                ((p[i] >> 1) & 0x7F) + 1,
                int.from_bytes(p[i + 1 : i + 3], "little"),
            )
            if length == 128:
                length = 0
        if i + hdr_len + length > len(p):
            raise ValueError(f"truncated sensor data at offset {i}: {p.hex()}")
        i += hdr_len
        out.append((prop, p[i : i + length]))
        i += length
    return out


def _sensor(p: bytes) -> str:
    return "; ".join(
        f"prop=0x{prop:04X} raw={raw.hex()} le={int.from_bytes(raw, 'little') if raw else ''}"
        for prop, raw in sensor_values(p)
    )


def _tai_time(p: bytes) -> str:
    """Time Set / Time Status parameters (§5.2.1.2 / §5.2.1.4); a Status with TAI seconds 0 is 5 bytes (unknown time)."""
    tai_seconds = int.from_bytes(p[:5], "little")
    if tai_seconds == 0:
        return "unknown"
    delta_field = int.from_bytes(p[7:9], "little")
    authority, tai_utc_delta, quarters = (
        delta_field & 1,
        (delta_field >> 1) - 255,
        p[9] - 64,
    )
    # the Zone Offset field reaches +47:45 (and -16:00), beyond what `datetime.timezone` holds (under ±24:00):
    # the local time is computed by hand and the offset written as ISO 8601 does, for every value the field has
    offset = timedelta(minutes=15 * quarters)
    sign, minutes = ("-" if quarters < 0 else "+"), abs(quarters) * 15
    try:
        utc = TAI_EPOCH + timedelta(
            seconds=tai_seconds - tai_utc_delta, microseconds=p[5] * 1_000_000 // 256
        )
        local = (utc + offset).replace(tzinfo=None).isoformat()
        when = f"{local}{sign}{minutes // 60:02d}:{minutes % 60:02d}"
    except (
        OverflowError
    ):  # TAI Seconds is a u40, about 34 800 years: past what a datetime holds
        return f"tai={tai_seconds} (out of range) authority={authority} tai_utc_delta={tai_utc_delta}"
    return (
        f"{when} tai={tai_seconds} uncertainty={p[6] * 10}ms authority={authority}"
        f" tai_utc_delta={tai_utc_delta}"
    )
