"""Catalogue of JUNG HOME device properties: id -> name, hosting server, codec, element rule, products, firmware.

Mirrors `docs/android/properties.md` §1 (the app's tables, the gateway firmware's extra ids in §1.10 and the
on-air corrections at the end of that file); which product exposes which property comes from
`docs/gap-analysis/device-settings.md`. Pure data plus value codecs: no Home Assistant, no transport.

Wire facts the codecs rely on (`docs/android/vendor-models.md` §3.3): an LBC property value travels as
`[propertyId u16 LE][userAccess u8 (Admin Set and every Status)][value...]`; integers inside the value are
little-endian unless a codec says otherwise (the energy charts are big-endian). The `server` of a spec is the
vendor server that *hosts* the property (the one the app writes through); reads of admin-hosted properties also
work through the User server (`CE 27 05`, seen on air for 0x5003 KeyMode).

Names are stable snake_case identifiers (they become translation keys).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

__all__ = [
    "ACTUATOR_FUNCTION",
    "ALL_PRODUCTS",
    "ASCII_VERSION",
    "ASTRO_MODE",
    "BLIND_HOSTS",
    "BLIND_MODE",
    "BOOL",
    "BUTTON_LAYOUT",
    "CONTROLLER_TYPE",
    "DELAY_MS",
    "DETECTORS",
    "DETECTOR_MODE",
    "DEVICE_LOCK_FLAGS",
    "DIMMER_HOSTS",
    "DIM_MODE",
    "EDGE_BEHAVIOUR",
    "ENERGY_HOSTS",
    "ENFORCED_OUTPUT",
    "ENFORCE_LOCK",
    "ENFORCE_UNLOCK",
    "ENFORCE_VALUE",
    "EPOCH_1970",
    "FORCED_OFF",
    "GATEWAY",
    "GATEWAY_STATUS_FLAGS",
    "HVAC_MODE",
    "INSERT_TYPE",
    "KEY_EVENT",
    "KEY_HOSTS",
    "KEY_MODE",
    "KEY_MODE_ID",
    "KEY_MODE_PROPERTY",
    "KEY_PROPERTY_MODE",
    "KEY_VALUE_DOWN",
    "KEY_VALUE_UP",
    "LAMP_HOSTS",
    "LED_COLOURS",
    "LED_HOSTS",
    "LED_NIGHT_MODE_OFF",
    "LED_NIGHT_MODE_ON",
    "LED_PALETTES",
    "LOAD_HOSTS",
    "LOCKABLE",
    "MAIN_PAGE",
    "MINI_ACTUATORS",
    "MINI_BLINDS",
    "MINI_DALI",
    "MINI_DIMMER",
    "MINI_INPUT",
    "MINI_SWITCH",
    "MOVE_ON_POWER",
    "MS32",
    "NOT_GATEWAY",
    "OPERATION_SITE",
    "PB_BATTERY",
    "PB_MAINS",
    "PERCENT",
    "POSITION",
    "PRESENCE_DETECTOR",
    "PRIORITY_LOCKOUT",
    "PRIORITY_NORMAL",
    "PRIORITY_WIND_ALARM",
    "PROPERTIES",
    "PUSH_BUTTONS",
    "RAW",
    "REDACTED",
    "RGB_MODE",
    "RTR",
    "S16",
    "SENSOR_SELECTION",
    "SIG_PROPERTIES",
    "SOCKETS",
    "SOCKET_METERING",
    "TEMP_001C",
    "TWO_GANG",
    "U8",
    "U16",
    "U24",
    "U32",
    "UNLOCK",
    "VALVE_OUTPUT",
    "VERSION_LE",
    "WIND_ALARM",
    "Access",
    "AsciiVersion",
    "AstroRegister",
    "AstroRegisterCodec",
    "AstroStatus",
    "AstroStatusCodec",
    "Bool",
    "Codec",
    "Counter",
    "DateUTC",
    "Delay",
    "Duration",
    "EdgeDetection",
    "EdgeDetectionCodec",
    "Element",
    "EnergyChart",
    "EnforcedOutput",
    "EnforcedOutputCodec",
    "Enum",
    "Flags",
    "InsertId",
    "InsertIdCodec",
    "Int",
    "KeyEvent",
    "KeyEventCodec",
    "LedColour",
    "LedMode",
    "LedSlot",
    "Percent",
    "Position",
    "PropertyMode",
    "PropertyModeCodec",
    "PropertySpec",
    "Raw",
    "RgbMode",
    "Scaled",
    "SceneConfig",
    "SceneConfigCodec",
    "Server",
    "Text",
    "Threshold",
    "ThresholdCodec",
    "Timestamp7",
    "Version",
    "VersionLE",
    "by_name",
    "decode",
    "describe_status",
    "encode",
    "for_product",
    "format_value",
    "key_lock",
    "key_lock_values",
    "led_colour_name",
    "led_palette",
    "led_property_id",
    "lock_output",
    "parse_version",
    "spec_for",
    "supported",
]

Server = Literal[
    "admin", "manufacturer", "user", "sig_admin", "sig_manufacturer", "sensor"
]
Access = Literal["ro", "rw", "wo"]
Element = Literal["node", "load", "key", "detector", "led", "aux", "meter"]
"""Which element a property is addressed to (`docs/gap-analysis/device-settings.md` §1.2):

- `node`: the primary element (hosts the LBC Admin Property Server; for detectors the *first* element);
- `load`: the load/output element (location 0x0001 / 0x0002) the parameter belongs to;
- `key`: the key / binary-input element (location 0x0040 + n);
- `detector`: the sensor element of a detector = its *highest* element;
- `led`: the primary element; the LED index is encoded in the property id (`led_property_id`);
- `aux`: the vendor-only element (location 0x0044) every push-button carries; on air its Manufacturer / User servers
  list exactly the wind-alert pair `0xA200` / `0xA201` (`docs/hidden-features.md` §1);
- `meter`: the metering element of a socket / energy puck (location 0x0040, the Sensor Server) — its SIG property
  servers hold the energy counters (`docs/hidden-features.md` §2).
"""
Version = tuple[int, int, int, int]

# ----------------------------------------------------------------------------- product groups
# Product ids from `docs/android/properties.md` §4 (company 0x0527).
PB_MAINS = frozenset(
    {0x01, 0x02}
)  # push-button 1-/2-gang (host an insert: switch / dimmer / DALI / blinds)
PB_BATTERY = frozenset({0x05, 0x06})  # wall transmitters (no insert)
PUSH_BUTTONS = PB_MAINS | PB_BATTERY
SOCKETS = frozenset({0x03, 0x0C})
SOCKET_METERING = frozenset({0x03})
DETECTORS = frozenset({0x07, 0x08, 0x09})
PRESENCE_DETECTOR = frozenset(
    {0x09}
)  # three PIR segments; the motion detectors 0x07 / 0x08 have two
RTR = frozenset({0x0A})
GATEWAY = frozenset({0x0B})
MINI_SWITCH = frozenset(
    {0x04, 0x10, 0x11}
)  # switch actuator minis / pucks (0x10 = energy metering)
MINI_DIMMER = frozenset({0x12})
MINI_BLINDS = frozenset({0x0D, 0x13})
MINI_DALI = frozenset({0x14})
MINI_INPUT = frozenset({0x15, 0x16})  # binary-input pucks (no load)
MINI_ACTUATORS = MINI_SWITCH | MINI_DIMMER | MINI_BLINDS | MINI_DALI | MINI_INPUT
ALL_PRODUCTS = PUSH_BUTTONS | SOCKETS | DETECTORS | RTR | GATEWAY | MINI_ACTUATORS
KEY_HOSTS = (
    PUSH_BUTTONS | MINI_ACTUATORS
)  # key / binary-input elements with the 0527:1015 client
LAMP_HOSTS = (
    PB_MAINS | MINI_SWITCH | MINI_DIMMER | MINI_DALI | DETECTORS
)  # lamp loads incl. the detector relay
LOAD_HOSTS = LAMP_HOSTS | SOCKETS
DIMMER_HOSTS = PB_MAINS | MINI_DIMMER | MINI_DALI
BLIND_HOSTS = PB_MAINS | MINI_BLINDS
ENERGY_HOSTS = frozenset(
    {0x03, 0x10}
)  # metering socket, energy puck (charts, total energy)
LOCKABLE = (
    LOAD_HOSTS | MINI_BLINDS
)  # 0x0009 EnforceOutput: lamps, sockets, blinds, mini actuators
LED_HOSTS = PUSH_BUTTONS | SOCKETS
TWO_GANG = frozenset({0x02, 0x06})
NOT_GATEWAY = ALL_PRODUCTS - GATEWAY


# ----------------------------------------------------------------------------- codecs


class Codec(ABC):
    """Encoder/decoder pair for one value type; `kind` tells the entity layer what the Python value looks like."""

    kind: str = "raw"

    @abstractmethod
    def encode(self, value: Any) -> bytes:
        """Return the wire bytes for `value`; raises ValueError when it does not fit."""

    @abstractmethod
    def decode(self, data: bytes) -> Any:
        """Return the Python value for the wire bytes; raises ValueError when they are too short or malformed."""


def _need(data: bytes, n: int) -> None:
    if len(data) < n:
        raise ValueError(f"need {n} byte(s), got {len(data)}")


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"expected an integer, got {value!r}")
    return value


def _to_bytes(value: int, size: int, *, signed: bool = False) -> bytes:
    try:
        return value.to_bytes(size, "little", signed=signed)
    except OverflowError as e:
        raise ValueError(f"{value} does not fit in {size} byte(s)") from e


@dataclass(frozen=True)
class Int(Codec):
    """Little-endian integer of `size` bytes (decode ignores trailing bytes)."""

    size: int = 1
    signed: bool = False
    kind = "int"

    def encode(self, value: Any) -> bytes:
        """Encode an int (bools are rejected)."""
        return _to_bytes(_as_int(value), self.size, signed=self.signed)

    def decode(self, data: bytes) -> int:
        """Decode the first `size` bytes."""
        _need(data, self.size)
        return int.from_bytes(data[: self.size], "little", signed=self.signed)


@dataclass(frozen=True)
class Counter(Codec):
    """Unsigned LE integer of variable reply length (SIG energy / hour counters): decode uses every byte, encode `size`.

    The GSS reserves the top two values of these characteristics: all-ones is "value is not known" (Energy
    0xFFFFFF, Energy32 0xFFFFFFFF, Time Hour 24 0xFFFFFF) and all-ones minus one "value is not valid" (Energy 0xFFFFFE,
    Energy32 0xFFFFFFFE); both decode to None, of whatever length the firmware sent (3 or 4 bytes on air) — a
    `total_increasing` energy sensor must never see 4.29 GWh.
    """

    size: int = 4
    kind = "int"

    def encode(self, value: Any) -> bytes:
        """Encode an int in `size` bytes (the app resets counters with an int32 0)."""
        return _to_bytes(_as_int(value), self.size)

    def decode(self, data: bytes) -> int | None:
        """Decode all bytes as one LE integer; the "not known" / "not valid" markers decode to None."""
        _need(data, 1)
        raw = int.from_bytes(data, "little")
        if raw >= (1 << (8 * len(data))) - 2:
            return None
        return raw


class Bool(Codec):
    """One byte, 0 / 1."""

    kind = "bool"

    def encode(self, value: Any) -> bytes:
        """Encode truthiness as one byte."""
        return bytes([1 if value else 0])

    def decode(self, data: bytes) -> bool:
        """Decode the first byte (any non-zero value is True)."""
        _need(data, 1)
        return data[0] != 0


@dataclass(frozen=True)
class Enum(Codec):
    """`size`-byte LE integer with named values; decode returns the name, or the raw integer when unmapped.

    `values` takes no part in the hash (a dict is unhashable; equality still compares it), so a `PropertySpec`
    holding this codec can be a set member or a dict key like any other frozen value.
    """

    values: Mapping[int, str] = field(hash=False)
    size: int = 1
    kind = "enum"

    @property
    def options(self) -> tuple[str, ...]:
        """Return the names in table order."""
        return tuple(self.values.values())

    def encode(self, value: Any) -> bytes:
        """Encode a name (or a raw integer)."""
        if isinstance(value, str):
            for raw, name in self.values.items():
                if name == value:
                    return _to_bytes(raw, self.size)
            raise ValueError(f"unknown option {value!r}; choose from {self.options}")
        return _to_bytes(_as_int(value), self.size)

    def decode(self, data: bytes) -> str | int:
        """Decode to the name, or to the integer when the table has no entry for it."""
        _need(data, self.size)
        raw = int.from_bytes(data[: self.size], "little")
        return self.values.get(raw, raw)


@dataclass(frozen=True)
class Scaled(Codec):
    """LE integer times `scale` (0.01 °C temperatures, 0.1 W power, ...); the `unknown` raw value decodes to None.

    An unsigned field without an explicit `unknown` treats all-ones as "value is not known": that is the GSS
    convention for every unsigned characteristic the catalogue scales (Power 0xFFFFFF, Electric Current and
    Voltage 0xFFFF, …), and a signed one has its own marker (0x7F / 0x8000). Without it a socket reporting "not
    known" would read as 1.6 MW.
    """

    size: int
    scale: float
    signed: bool = False
    unknown: int | None = None
    kind = "float"

    @property
    def unknown_raw(self) -> int | None:
        """The raw value that decodes to "not known": `unknown`, else all-ones for an unsigned field.

        Only `decode` uses the implicit marker: encoding None still needs a catalogue-named `unknown` (the
        unsigned characteristics are read-only sensor values, nothing writes "not known" to them).
        """
        if self.unknown is not None or self.signed:
            return self.unknown
        return (1 << (8 * self.size)) - 1

    def encode(self, value: Any) -> bytes:
        """Encode a number (None encodes the `unknown` marker when the catalogue names one)."""
        if value is None:
            if self.unknown is None:
                raise ValueError("a value is required")
            return _to_bytes(self.unknown, self.size, signed=self.signed)
        return _to_bytes(
            round(float(value) / self.scale), self.size, signed=self.signed
        )

    def decode(self, data: bytes) -> float | None:
        """Decode to a float rounded to the scale's precision; the "not known" marker decodes to None."""
        _need(data, self.size)
        raw = int.from_bytes(data[: self.size], "little", signed=self.signed)
        if raw == self.unknown_raw:
            return None
        # the scale's decimal places from its exponent: `str(0.00001)` is "1e-05", which has no "." to count after
        digits = max(0, -int(Decimal(str(self.scale)).normalize().as_tuple().exponent))
        return round(raw * self.scale, digits)


class Percent(Codec):
    """One byte 0..255 shown as 0..100 % (PIR sensitivity)."""

    kind = "percent"

    def encode(self, value: Any) -> bytes:
        """Encode 0..100 % as 0..255."""
        return bytes([self._raw(value)])

    def decode(self, data: bytes) -> int:
        """Decode 0..255 to 0..100 %."""
        _need(data, 1)
        return round(data[0] * 100 / 255)

    @staticmethod
    def _raw(value: Any) -> int:
        pct = float(value)
        if not 0 <= pct <= 100:
            raise ValueError(f"{pct} % is outside 0..100")
        return round(pct * 255 / 100)


class Position(Percent):
    """Blind / slat position in % with the app's wire quirks (`Position.kt`).

    100 % is sent as 254 and the raw values 0 and 255 are swapped, so 0 % <-> 255 and 100 % <-> 254 (a raw 0
    reads back as 100 %). The swap on *read* is documented (`docs/android/properties.md` §2.17, `q.java:22-31`);
    whether the app also swaps on *write* is not settled: this codec swaps both ways so its own round trip holds,
    and only a device can say which end a written raw 0 / 255 drives to (`docs/cross-repo-analysis.md` §8).
    """

    def encode(self, value: Any) -> bytes:
        """Encode 0..100 % (100 -> 254, then swap the ends)."""
        raw = self._raw(value)
        return bytes([_swap_ends(254 if raw == 255 else raw)])

    def decode(self, data: bytes) -> int:
        """Swap the ends, then scale to %."""
        _need(data, 1)
        return round(_swap_ends(data[0]) * 100 / 255)


def _swap_ends(raw: int) -> int:
    return {0: 255, 255: 0}.get(raw, raw)


@dataclass(frozen=True)
class Duration(Codec):
    """Unsigned LE integer in `unit` (ms or s) on the wire; seconds (float) in Python."""

    size: int
    unit: Literal["ms", "s"] = "ms"
    kind = "duration"

    def encode(self, value: Any) -> bytes:
        """Encode seconds."""
        seconds = float(value)
        if seconds < 0:
            raise ValueError(f"negative duration {seconds}")
        return _to_bytes(
            round(seconds * 1000 if self.unit == "ms" else seconds), self.size
        )

    def decode(self, data: bytes) -> float:
        """Decode to seconds."""
        _need(data, self.size)
        raw = int.from_bytes(data[: self.size], "little")
        return raw / 1000 if self.unit == "ms" else float(raw)


@dataclass(frozen=True)
class Delay(Duration):
    """A switch-on / switch-off delay (0x1001 / 0x1002): int32 LE ms, read the way the app reads it.

    The app takes the value as a *signed* int and shows anything outside 0 ms .. 24 h as off (0), anything above
    4 h as 4 h (`GeneralOffDelay` / `GeneralOnDelay`, `ui/project/gateway/detail/z.java:518` / `:527`). The
    factory value 0xFFFFFFFF (-1, on air for 0x1002) is therefore off, not 49 days. Writes are the
    plain Duration's.
    """

    size: int = 4
    unit: Literal["ms", "s"] = "ms"
    valid_ms: int = 86_400_000  # the app's range end, `c.f47137a` / `d.f47141a`
    shown_ms: int = 14_400_000  # what the app's picker ends at, and what it clamps a longer value to

    def decode(self, data: bytes) -> float:
        """Decode to seconds: 0 when the app would show off, at most 4 h."""
        _need(data, self.size)
        raw = int.from_bytes(data[: self.size], "little", signed=True)
        return 0.0 if not 0 <= raw <= self.valid_ms else min(raw, self.shown_ms) / 1000


class Raw(Codec):
    """Opaque bytes (layout unknown or property-specific)."""

    kind = "raw"

    def encode(self, value: Any) -> bytes:
        """Pass bytes through."""
        if not isinstance(value, bytes | bytearray):
            raise ValueError(f"expected bytes, got {value!r}")
        return bytes(value)

    def decode(self, data: bytes) -> bytes:
        """Pass bytes through."""
        return bytes(data)


@dataclass(frozen=True)
class Text(Codec):
    """UTF-8 string, NUL-padded on the wire (gateway credentials); `secret` values are never shown by `describe_status`."""

    secret: bool = False
    kind = "string"

    def encode(self, value: Any) -> bytes:
        """Encode a str."""
        if not isinstance(value, str):
            raise ValueError(f"expected a string, got {value!r}")
        return value.encode()

    def decode(self, data: bytes) -> str:
        """Strip NULs and surrounding whitespace."""
        return data.replace(b"\0", b"").decode(errors="replace").strip()


EPOCH_1970 = date(1970, 1, 1)


class DateUTC(Codec):
    """SIG `Date UTC` characteristic (uint24 days since 1970-01-01; 0 = unknown) <-> ISO date string.

    Device Date of Manufacture 0x000C reads `39 4B 00` = day 19257 = 2022-09-22 on a socket (field-tested).
    """

    kind = "date"

    def encode(self, value: Any) -> bytes:
        """Encode an ISO date (`YYYY-MM-DD`) or None (unknown)."""
        if value is None:
            return bytes(3)
        if not isinstance(value, str):
            raise ValueError(f"expected an ISO date, got {value!r}")
        days = (date.fromisoformat(value) - EPOCH_1970).days
        if not 0 < days <= 0xFFFFFF:
            raise ValueError(f"{value} is outside the Date UTC range")
        return _to_bytes(days, 3)

    def decode(self, data: bytes) -> str | None:
        """Decode the first three bytes; None for the unknown value 0; ValueError past the year 9999.

        `date + timedelta` raises OverflowError (not ValueError) for a day count above 2 932 896, which the
        `describe*` fallbacks do not catch — an all-ones Date UTC from a node crashed the CLI's `prop get`.
        """
        _need(data, 3)
        days = int.from_bytes(data[:3], "little")
        if days == 0:
            return None
        try:
            return (EPOCH_1970 + timedelta(days=days)).isoformat()
        except OverflowError as e:
            raise ValueError(
                f"not a date: {data[:3].hex()} ({days} days since 1970)"
            ) from e


class Timestamp7(Codec):
    """A 7-byte broken-down local time: `[year - 1900 u16 LE][month][day][hour][minute][second]` <-> ISO string.

    Firmware property 0x5014 on the metering sockets' meter element reads `7c 00 0a 03 0e 2e 2e` = 2024-10-03
    14:46:46 on one socket and `7c 00 0a 0c 16 28 0f` = 2024-10-12 22:40:15 on the other: constant
    per device, different between devices, in the sockets' local time — a stored moment (commissioning? the last
    energy counter reset?), not a clock. The C `struct tm` convention (year since 1900) fits the bytes; the month
    is taken as 1-based (October), which is the only unverified assumption.
    """

    kind = "datetime"
    YEAR_BASE = 1900

    def encode(self, value: Any) -> bytes:
        """Encode an ISO local timestamp (`YYYY-MM-DDTHH:MM:SS`)."""
        if not isinstance(value, str):
            raise ValueError(f"expected an ISO timestamp, got {value!r}")
        when = datetime.fromisoformat(value)
        year = when.year - self.YEAR_BASE
        if not 0 <= year <= 0xFFFF:
            raise ValueError(f"{value} is outside the range of this field")
        return _to_bytes(year, 2) + bytes(
            [when.month, when.day, when.hour, when.minute, when.second]
        )

    def decode(self, data: bytes) -> str:
        """Decode the seven bytes (ValueError for a month / day / time that is not a date)."""
        _need(data, 7)
        year = int.from_bytes(data[:2], "little") + self.YEAR_BASE
        try:
            when = datetime(year, data[2], data[3], data[4], data[5], data[6])  # noqa: DTZ001  # the node's local wall time, no zone on the wire
        except ValueError as err:
            raise ValueError(f"not a timestamp: {data.hex()}") from err
        return when.isoformat()


class AsciiVersion(Codec):
    """SIG 0x001A Device Software Revision: 8 ASCII digits `"02020002"` <-> `"2.2.0.2"` (field-tested)."""

    kind = "version"

    def encode(self, value: Any) -> bytes:
        """Encode `"2.2.0.2"` as `b"02020002"`; every part is two digits, 0..99, or `decode` could not read it."""
        parts = [int(part) for part in str(value).split(".")]
        if not all(0 <= part <= 99 for part in parts):
            raise ValueError(f"version {value!r}: every part must be 0..99")
        return "".join(f"{part:02d}" for part in parts).encode()

    def decode(self, data: bytes) -> str:
        """Split into groups of two digits, strip the leading zero, join with dots (the app's rule)."""
        text = data.replace(b"\0", b"").decode(errors="replace")
        if not text.isdigit() or len(text) % 2:
            raise ValueError(f"not an ASCII version: {data!r}")
        return ".".join(str(int(text[i : i + 2])) for i in range(0, len(text), 2))


class VersionLE(Codec):
    """LBC version blocks (0x0003 .. 0x0005): bytes reversed, dotted decimal; `0d 02 01 00` <-> `"0.1.2.13"` (field-tested)."""

    kind = "version"

    def encode(self, value: Any) -> bytes:
        """Encode `"0.1.2.13"` as `0d 02 01 00`."""
        return bytes(int(part) for part in reversed(str(value).split(".")))

    def decode(self, data: bytes) -> str:
        """Reverse the bytes and join them with dots."""
        _need(data, 1)
        return ".".join(str(b) for b in reversed(data))


@dataclass(frozen=True)
class Flags(Codec):
    """Bit field: `names[i]` is bit *i* of a `size`-byte LE word; Python value is `{name: bool}` (or an int on encode)."""

    names: tuple[str, ...]
    size: int = 1
    kind = "flags"

    def encode(self, value: Any) -> bytes:
        """Encode a `{name: bool}` mapping (missing names are 0) or a raw integer."""
        if isinstance(value, Mapping):
            unknown = set(value) - set(self.names)
            if unknown:
                raise ValueError(f"unknown flag(s) {sorted(unknown)}")
            raw = sum(1 << i for i, name in enumerate(self.names) if value.get(name))
            return _to_bytes(raw, self.size)
        return _to_bytes(_as_int(value), self.size)

    def decode(self, data: bytes) -> dict[str, bool]:
        """Decode to `{name: bool}` (bits without a name are dropped)."""
        _need(data, self.size)
        raw = int.from_bytes(data[: self.size], "little")
        return {name: bool(raw >> i & 1) for i, name in enumerate(self.names)}


LED_NIGHT_MODE_ON, LED_NIGHT_MODE_OFF = 5, 0


@dataclass(frozen=True)
class LedMode:
    """LED colour (0..100 per channel) plus night mode (wire mode byte 5 = on, 0 = off)."""

    red: int
    green: int
    blue: int
    night_mode: bool = False

    @property
    def rgb(self) -> tuple[int, int, int]:
        """Return the colour triple."""
        return (self.red, self.green, self.blue)


class RgbMode(Codec):
    """0xA0xx LED mode: `[red u8][green u8][blue u8][mode u8]` <-> `LedMode` (`docs/android/properties.md` §1.8)."""

    kind = "rgb_mode"

    def encode(self, value: Any) -> bytes:
        """Encode a `LedMode` (channels 0..100)."""
        if not isinstance(value, LedMode):
            raise ValueError(f"expected LedMode, got {value!r}")
        if not all(0 <= c <= 100 for c in value.rgb):
            raise ValueError(f"LED channels must be 0..100 %, got {value.rgb}")
        mode = LED_NIGHT_MODE_ON if value.night_mode else LED_NIGHT_MODE_OFF
        return bytes([*value.rgb, mode])

    def decode(self, data: bytes) -> LedMode:
        """Decode four bytes; any mode byte other than 5 reads as night mode off (as the app does)."""
        _need(data, 4)
        return LedMode(data[0], data[1], data[2], data[3] == LED_NIGHT_MODE_ON)


ENFORCE_UNLOCK, ENFORCE_VALUE, ENFORCE_LOCK = 0, 1, 2
PRIORITY_NORMAL, PRIORITY_LOCKOUT, PRIORITY_WIND_ALARM = 1, 254, 255


@dataclass(frozen=True)
class EnforcedOutput:
    """0x0009 lock function.

    `command`: 0 unlock / 1 enforce `value` / 2 lock the current state; `priority`: 1 normal, 254 lock-out
    protection, 255 wind alarm; `time_s`: 0 = no limit; `value`: the enforced level bytes.
    """

    command: int
    priority: int = PRIORITY_NORMAL
    time_s: int = 0
    value: bytes = b""

    @property
    def locked(self) -> bool:
        """Return the app's "locked" badge: any command other than unlock / unknown."""
        return self.command not in (ENFORCE_UNLOCK, 0xFF)

    @property
    def wind_alarm(self) -> bool:
        """Return whether this is a lock with the wind-alarm priority."""
        return self.locked and self.priority == PRIORITY_WIND_ALARM


# `01 FF 00 00 00 00` as the app sends it, and the plain unlock.
WIND_ALARM = EnforcedOutput(ENFORCE_VALUE, PRIORITY_WIND_ALARM, 0, b"\x00\x00")
UNLOCK = EnforcedOutput(ENFORCE_UNLOCK)


def lock_output(time_s: int = 0, *, lockout: bool = False) -> EnforcedOutput:
    """Build the app's "lock current state" (`02 01 <s>`) or "lock-out protection" (`02 FE <s>`)."""
    priority = PRIORITY_LOCKOUT if lockout else PRIORITY_NORMAL
    return EnforcedOutput(ENFORCE_LOCK, priority, time_s)


class EnforcedOutputCodec(Codec):
    """`[command u8][priority u8][time u16 LE s][value...]`; time and value are optional on the wire."""

    kind = "struct"

    def encode(self, value: Any) -> bytes:
        """Encode an `EnforcedOutput` (always with the time field, like the app)."""
        if not isinstance(value, EnforcedOutput):
            raise ValueError(f"expected EnforcedOutput, got {value!r}")
        head = bytes([value.command, value.priority]) + _to_bytes(value.time_s, 2)
        return head + value.value

    def decode(self, data: bytes) -> EnforcedOutput:
        """Decode; a missing time means no limit.

        As the app's parser (`docs/android/properties.md` §1.2 0x0009): the time is absent when fewer than two
        bytes follow the priority, and whatever remains is the value — a 3-byte status keeps its third byte.
        """
        _need(data, 2)
        if len(data) >= 4:
            return EnforcedOutput(
                data[0], data[1], int.from_bytes(data[2:4], "little"), bytes(data[4:])
            )
        return EnforcedOutput(data[0], data[1], 0, bytes(data[2:]))


@dataclass(frozen=True)
class Threshold:
    """0x5004 / 0x5005 power threshold: switch after `time_s` seconds of `power_w` (None = no threshold)."""

    power_w: float | None
    time_s: int
    active: bool
    sensor_property: int = 0x0081  # Active Power Loadside, the only sensor the app uses


class ThresholdCodec(Codec):
    """`[sensorPropertyId u16 LE][time u16 LE s][power u24 LE x 0.1 W, 0xFFFFFF = none][active u8]`."""

    kind = "struct"

    def encode(self, value: Any) -> bytes:
        """Encode a `Threshold`."""
        if not isinstance(value, Threshold):
            raise ValueError(f"expected Threshold, got {value!r}")
        raw = 0xFFFFFF if value.power_w is None else round(value.power_w * 10)
        return (
            _to_bytes(value.sensor_property, 2)
            + _to_bytes(value.time_s, 2)
            + _to_bytes(raw, 3)
            + BOOL.encode(value.active)
        )

    def decode(self, data: bytes) -> Threshold:
        """Decode the 8-byte struct."""
        _need(data, 8)
        raw = int.from_bytes(data[4:7], "little")
        return Threshold(
            None if raw == 0xFFFFFF else raw / 10,
            int.from_bytes(data[2:4], "little"),
            data[7] == 1,
            int.from_bytes(data[:2], "little"),
        )


@dataclass(frozen=True)
class SceneConfig:
    """0x5002 key scene: the scene a key in KeyMode *scene* recalls, with the transition time in ms."""

    scene: int
    transition_ms: int = 0


class SceneConfigCodec(Codec):
    """`[sceneId u16 LE][transitionTime u32 LE ms]`."""

    kind = "struct"

    def encode(self, value: Any) -> bytes:
        """Encode a `SceneConfig`."""
        if not isinstance(value, SceneConfig):
            raise ValueError(f"expected SceneConfig, got {value!r}")
        return _to_bytes(value.scene, 2) + _to_bytes(value.transition_ms, 4)

    def decode(self, data: bytes) -> SceneConfig:
        """Decode the 6-byte struct."""
        _need(data, 6)
        return SceneConfig(
            int.from_bytes(data[:2], "little"), int.from_bytes(data[2:6], "little")
        )


@dataclass(frozen=True)
class PropertyMode:
    """0x5006: the property a key in KeyMode *property* writes, statefully (up/down values) or statelessly."""

    property_id: int
    stateful: bool = True


class PropertyModeCodec(Codec):
    """`[targetPropertyId u16 LE][mode u8: 0 stateless / 1 stateful]`."""

    kind = "struct"

    def encode(self, value: Any) -> bytes:
        """Encode a `PropertyMode`."""
        if not isinstance(value, PropertyMode):
            raise ValueError(f"expected PropertyMode, got {value!r}")
        return _to_bytes(value.property_id, 2) + BOOL.encode(value.stateful)

    def decode(self, data: bytes) -> PropertyMode:
        """Decode the 3-byte struct."""
        _need(data, 3)
        return PropertyMode(int.from_bytes(data[:2], "little"), data[2] == 1)


# A key in KeyMode *property* (3) as `SetLockingFunctionConnection` leaves it (`network-logic.md` §2.6): KeySetPropertyMode
# 0x5006 names EnforceOutput 0x0009, statefully; its up / on half 0x5007 locks the target, its down / off half 0x5008
# unlocks it. Which property the key's LBC User Property client then sets on the target is the firmware's business.
ENFORCED_OUTPUT, KEY_MODE_PROPERTY = 0x0009, 3
KEY_PROPERTY_MODE, KEY_VALUE_UP, KEY_VALUE_DOWN, KEY_MODE_ID = (
    0x5006,
    0x5007,
    0x5008,
    0x5003,
)


def key_lock_values(time_s: int = 0) -> tuple[bytes, bytes, bytes]:
    """Return the 0x5006, 0x5007 and 0x5008 values of a key that locks its target's current state (unverified on air).

    `(0x0009, stateful)`; up = `lock_output(time_s)` (`02 01 <s>`, 0 = no limit), down = `UNLOCK` (`00 01 00 00`),
    the unlock the app's *Lock* page sends (`LockFunctionViewModelDelegate$unlockDevice`). Nobody has watched the app
    write a key's lock link yet: `docs/on-air-sweep.md` D9 captures it.
    """
    codec = EnforcedOutputCodec()
    return (
        PropertyModeCodec().encode(PropertyMode(ENFORCED_OUTPUT, stateful=True)),
        codec.encode(lock_output(time_s)),
        codec.encode(UNLOCK),
    )


def key_lock(values: Mapping[int, bytes]) -> EnforcedOutput | None:
    """Decode a key's cached 0x5006 / 0x5007 (and KeyMode 0x5003, when known) into the lock its up half sets.

    None unless KeySetPropertyMode names EnforceOutput statefully and the up value is a lock (any command but unlock):
    a key the app or `assign_key` wired otherwise was reset to `(0, stateless)` first (`ResetKeySetPropertyMode`). A
    known KeyMode other than *property* means the values are left over. A value that does not decode counts as none.
    The app reads the same two properties to show a key's lock connection (`GetConnection.h()`). Unverified on air.
    """
    mode_value = values.get(KEY_MODE_ID)
    if mode_value is not None and mode_value[:1] != bytes([KEY_MODE_PROPERTY]):
        return None
    try:
        mode = PropertyModeCodec().decode(values.get(KEY_PROPERTY_MODE, b""))
        up = EnforcedOutputCodec().decode(values.get(KEY_VALUE_UP, b""))
    except ValueError:
        return None
    if mode != PropertyMode(ENFORCED_OUTPUT, stateful=True) or not up.locked:
        return None
    return up


EDGE_BEHAVIOUR = {0: "no_reaction", 1: "on", 2: "off", 3: "toggle"}


@dataclass(frozen=True)
class EdgeDetection:
    """0x5009 binary-input evaluation: `edge_mode` False = state, True = edge; behaviours per `EDGE_BEHAVIOUR`."""

    edge_mode: bool
    rising: str = "no_reaction"
    falling: str = "no_reaction"


class EdgeDetectionCodec(Codec):
    """One byte: bits 7:5 = 0, bits 4:3 = falling-edge behaviour, bits 2:1 = rising-edge behaviour, bit 0 = mode."""

    kind = "struct"
    _behaviour = Enum(EDGE_BEHAVIOUR)

    def encode(self, value: Any) -> bytes:
        """Encode an `EdgeDetection`."""
        if not isinstance(value, EdgeDetection):
            raise ValueError(f"expected EdgeDetection, got {value!r}")
        falling = self._behaviour.encode(value.falling)[0]
        rising = self._behaviour.encode(value.rising)[0]
        if (
            falling > 3 or rising > 3
        ):  # 2-bit fields: wider would spill into the next field / reserved bits
            raise ValueError(
                f"edge behaviour {falling} / {rising} does not fit its 2 bits (0..3)"
            )
        return bytes([falling << 3 | rising << 1 | (1 if value.edge_mode else 0)])

    def decode(self, data: bytes) -> EdgeDetection:
        """Decode one byte (every 2-bit behaviour value has a name)."""
        _need(data, 1)
        b = data[0]
        return EdgeDetection(
            bool(b & 1), EDGE_BEHAVIOUR[b >> 1 & 3], EDGE_BEHAVIOUR[b >> 3 & 3]
        )


ASTRO_MODE = {0: "static", 1: "sunrise", 2: "sunset", 3: "reserved"}


@dataclass(frozen=True)
class AstroRegister:
    """0x0007 astro scheduler register: `offset_min` relative to sunrise/sunset, bounded by earliest / latest time."""

    index: int
    mode: str | int = "static"
    offset_min: int = 0
    earliest: tuple[int, int] = (0, 0)  # (hour, minute)
    latest: tuple[int, int] = (23, 59)


class AstroRegisterCodec(Codec):
    """5 bytes, one LE 40-bit field (`docs/android/properties.md` §1.9).

    Bits 3:0 index, 7:4 mode, 15:8 offset (int8 minutes), 20:16 earliest hour, 26:21 earliest minute, 31:27
    latest hour, 37:32 latest minute, 39:38 zero. The Get for this property carries the register index as one
    extra byte.
    """

    kind = "struct"
    _mode = Enum(ASTRO_MODE)

    def encode(self, value: Any) -> bytes:
        """Encode an `AstroRegister`."""
        if not isinstance(value, AstroRegister):
            raise ValueError(f"expected AstroRegister, got {value!r}")
        if not (0 <= value.index <= 15 and -128 <= value.offset_min <= 127):
            raise ValueError(
                f"index {value.index} / offset {value.offset_min} out of range"
            )
        for hour, minute in (value.earliest, value.latest):
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError(f"bad time {hour}:{minute}")
        mode = self._mode.encode(value.mode)[0]
        if mode > 0xF:  # a 4-bit field: wider would land in the offset byte
            raise ValueError(f"astro mode {mode} does not fit its 4 bits (0..15)")
        word = (
            value.index
            | mode << 4
            | (value.offset_min & 0xFF) << 8
            | value.earliest[0] << 16
            | value.earliest[1] << 21
            | value.latest[0] << 27
            | value.latest[1] << 32
        )
        return _to_bytes(word, 5)

    def decode(self, data: bytes) -> AstroRegister:
        """Decode the 40-bit field."""
        _need(data, 5)
        f = int.from_bytes(data[:5], "little")
        offset = f >> 8 & 0xFF
        return AstroRegister(
            f & 0xF,
            self._mode.decode(bytes([f >> 4 & 0xF])),
            offset - 256 if offset > 127 else offset,
            (f >> 16 & 0x1F, f >> 21 & 0x3F),
            (f >> 27 & 0x1F, f >> 32 & 0x3F),
        )


@dataclass(frozen=True)
class AstroStatus:
    """0x0008: bit *i* of `active` = register *i* active; bit *i* of `astro` = register *i* in astro (1) / static (0) mode."""

    active: int
    astro: int


class AstroStatusCodec(Codec):
    """`uint32 LE`: low 16 bits = active mask, high 16 bits = mode mask."""

    kind = "struct"

    def encode(self, value: Any) -> bytes:
        """Encode an `AstroStatus`."""
        if not isinstance(value, AstroStatus):
            raise ValueError(f"expected AstroStatus, got {value!r}")
        return _to_bytes(value.active, 2) + _to_bytes(value.astro, 2)

    def decode(self, data: bytes) -> AstroStatus:
        """Decode 4 bytes."""
        _need(data, 4)
        return AstroStatus(
            int.from_bytes(data[:2], "little"), int.from_bytes(data[2:4], "little")
        )


ACTUATOR_FUNCTION = {
    0: "switch",
    1: "two_gang_switch",
    2: "dimming",
    3: "two_gang_dimming",
    4: "tw_dimming",
    5: "blind",
    6: "extension",
    7: "not_available",
    8: "rtr",
    9: "gateway",
    0xFFFF: "unset",
}
INSERT_TYPE = {0: "unknown", 1: "no_insert", 2: "generic_insert"}


@dataclass(frozen=True)
class InsertId:
    """0x0002: the actuator function (insert) of a load element and the insert type."""

    function: str | int
    insert_type: str | int = "unknown"


class InsertIdCodec(Codec):
    """`[actuatorFunctionId u16 LE][insertType u16 LE]` (4 bytes, field-tested order) or the function alone (2 bytes)."""

    kind = "struct"
    _function = Enum(ACTUATOR_FUNCTION, size=2)
    _insert = Enum(INSERT_TYPE, size=2)

    def encode(self, value: Any) -> bytes:
        """Encode an `InsertId` (devices reject Sets; useful for building test statuses)."""
        if not isinstance(value, InsertId):
            raise ValueError(f"expected InsertId, got {value!r}")
        return self._function.encode(value.function) + self._insert.encode(
            value.insert_type
        )

    def decode(self, data: bytes) -> InsertId:
        """Decode 2 or 4 bytes."""
        _need(data, 2)
        insert = self._insert.decode(data[2:4]) if len(data) >= 4 else "unknown"
        return InsertId(self._function.decode(data), insert)


KEY_EVENT = {
    0: "pushed_down",
    1: "pushed_up",
    2: "held_down",
    3: "held_up",
    4: "released",
    5: "pushed",
    6: "held",
}


@dataclass(frozen=True)
class KeyEvent:
    """0x5012 button event published by a key in KeyMode *gateway*; `counter` repeats on the firmware's second copy."""

    counter: int
    event: str | int


class KeyEventCodec(Codec):
    """`[counter u8][event u8]` (`docs/poc-gatt-proxy.md` "Button events")."""

    kind = "struct"
    _event = Enum(KEY_EVENT)

    def encode(self, value: Any) -> bytes:
        """Encode a `KeyEvent`."""
        if not isinstance(value, KeyEvent):
            raise ValueError(f"expected KeyEvent, got {value!r}")
        return bytes([value.counter]) + self._event.encode(value.event)

    def decode(self, data: bytes) -> KeyEvent:
        """Decode 2 bytes."""
        _need(data, 2)
        return KeyEvent(data[0], self._event.decode(data[1:2]))


@dataclass(frozen=True)
class EnergyChart(Codec):
    """0x5010 / 0x5011 energy history, read-only.

    Big-endian samples of `sample_size` bytes x 0.1 (Wh), entry *i* = *i* hours / days ago; an all-ones sample is
    invalid and decodes to None.
    """

    sample_size: int
    kind = "list"

    def encode(self, value: Any) -> bytes:
        """Reject: the charts are read-only."""
        raise ValueError("energy charts are read-only")

    def decode(self, data: bytes) -> list[float | None]:
        """Decode whole samples (a trailing partial sample is ignored)."""
        n, invalid = self.sample_size, (1 << 8 * self.sample_size) - 1
        samples = [
            int.from_bytes(data[i : i + n], "big")
            for i in range(0, len(data) - n + 1, n)
        ]
        return [None if s == invalid else s / 10 for s in samples]


# Shared codec instances.
U8, U16, U24, U32, S16 = Int(1), Int(2), Int(3), Int(4), Int(2, signed=True)
BOOL, RAW, PERCENT, POSITION, RGB_MODE = Bool(), Raw(), Percent(), Position(), RgbMode()
# sint16 LE 0.01 °C, 0x8000 = unknown (gateway firmware).
TEMP_001C = Scaled(2, 0.01, signed=True, unknown=-0x8000)
MS32 = Duration(4, "ms")  # uint32 LE milliseconds, exposed in seconds
DELAY_MS = Delay()  # the on / off delays: MS32 on the wire, out-of-range values read as the app shows them
VERSION_LE, ASCII_VERSION = VersionLE(), AsciiVersion()

# ----------------------------------------------------------------------------- enums (docs/android/properties.md §2)
# fmt: off
KEY_MODE = {0: "light", 1: "move", 2: "scene", 3: "property", 4: "rtr", 5: "switch", 6: "gateway", 0xFF: "unknown"}
BUTTON_LAYOUT = {
    0: "one_top_one_bottom", 1: "one_rocker", 2: "two_left_two_right", 3: "one_left_two_right",
    4: "two_left_one_right", 5: "one_left_one_right", 0xFF: "unknown",
}
DIM_MODE = {1: "leading_edge", 2: "trailing_edge", 5: "unknown"}
BLIND_MODE = {0: "blinds", 1: "shutter", 3: "awning", 4: "unknown"}
MOVE_ON_POWER = {
    0: "no_reaction", 1: "move_up", 2: "move_down", 3: "stop", 4: "move_to_stored_position",
    5: "position_for_network_failure",
}
HVAC_MODE = {0: "none", 1: "comfort", 2: "eco", 3: "frost"}  # gateway: heat / cool / frost
CONTROLLER_TYPE = {1: "pi_control", 2: "two_point_control", 3: "unknown"}
MAIN_PAGE = {1: "target_temperature", 2: "current_temperature", 3: "clock", 4: "unknown"}
SENSOR_SELECTION = {1: "internal", 2: "external", 3: "internal_and_external", 4: "unknown"}
VALVE_OUTPUT = {0: "normally_closed", 1: "normally_open", 2: "unknown"}
DETECTOR_MODE = {0: "guard", 1: "presence", 2: "unknown"}
OPERATION_SITE = {7: "indoor", 0x70: "outdoor", 0: "unknown"}  # firmware: two base-sensitivity presets
FORCED_OFF = {0: "inactive", 2: "off", 3: "on"}
DEVICE_LOCK_FLAGS = (  # 0x0001, bit0 first: the app's *encoder* order, which the gateway's value map corroborates
    "local_factory_reset_lock",
    "factory_reset_time_limit",
    "local_devices_lock",  # UI "Lock operation"
    "key_lock",  # RTR "Key lock"
    "configuration_lock",  # RTR "Lock configuration on the unit"
)
GATEWAY_STATUS_FLAGS = ("api_available", "client_waiting_for_approval")

# ----------------------------------------------------------------------------- LED palettes (RGB 0..100, from the app)
LedColour = tuple[int, int, int]
LED_COLOURS = ("red", "green", "white", "blue", "violet", "orange", "yellow", "cyan", "no_color")
_PALETTE_ROCKER_1G: Mapping[str, LedColour] = {  # Z7/a.java:81
    "red": (100, 0, 0), "green": (4, 100, 0), "white": (80, 66, 32), "blue": (0, 4, 100), "violet": (100, 0, 48),
    "orange": (100, 27, 0), "yellow": (96, 77, 0), "cyan": (0, 100, 48), "no_color": (0, 0, 0),
}
_PALETTE_ROCKER_2G: Mapping[str, LedColour] = {**_PALETTE_ROCKER_1G, "red": (75, 0, 0), "orange": (100, 29, 0)}  # Z7/g.java:84
_PALETTE_WT_1G: Mapping[str, LedColour] = {**_PALETTE_ROCKER_2G, "red": (100, 0, 0), "green": (6, 100, 0), "blue": (0, 14, 100)}  # Z7/i.java:58
_PALETTE_WT_2G: Mapping[str, LedColour] = {**_PALETTE_ROCKER_2G, "red": (80, 0, 0), "blue": (0, 9, 100)}  # Z7/j.java:58
_PALETTE_SOCKET: Mapping[str, LedColour] = {  # Y7/p0.java:24
    "red": (100, 0, 0), "green": (0, 60, 0), "white": (40, 58, 42), "blue": (0, 7, 92), "violet": (80, 0, 100),
    "orange": (80, 21, 0), "yellow": (84, 100, 0), "cyan": (0, 60, 46), "no_color": (0, 0, 0),
}
LED_PALETTES: Mapping[int, Mapping[str, LedColour]] = {
    0x01: _PALETTE_ROCKER_1G, 0x02: _PALETTE_ROCKER_2G, 0x05: _PALETTE_WT_1G, 0x06: _PALETTE_WT_2G,
    0x03: _PALETTE_SOCKET, 0x0C: _PALETTE_SOCKET,
}
# fmt: on


def led_palette(product_id: int) -> Mapping[str, LedColour]:
    """Return the colour name -> RGB table the app offers for this product (KeyError when it has no LED)."""
    return LED_PALETTES[product_id]


def led_colour_name(rgb: tuple[int, int, int], product_id: int) -> str | None:
    """Return the palette name of an exact RGB triple, or None when the device shows a colour the app cannot name."""
    palette = led_palette(product_id)
    return next((name for name, c in palette.items() if c == tuple(rgb)), None)


LedSlot = Literal["channel", "on", "off"]
_LED_SLOTS: tuple[LedSlot, ...] = ("channel", "on", "off")


def led_property_id(led: int, slot: LedSlot) -> int:
    """Return the property id of LED `led` (1..16): `0xA000 + 3*(led-1)` + 0 channel selection / 1 on mode / 2 off mode."""
    if not 1 <= led <= 16:
        raise ValueError(f"LED index {led} outside 1..16")
    return 0xA000 + 3 * (led - 1) + _LED_SLOTS.index(slot)


# ----------------------------------------------------------------------------- the spec


@dataclass(frozen=True)
class PropertySpec:
    """One device property: where it lives, how the app treats it, how its value is coded, who has it."""

    id: int
    name: str
    server: Server
    access: Access
    codec: Codec
    element: Element = "node"
    unit: str | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    products: frozenset[int] = field(default_factory=frozenset)
    firmware_min: Version | None = (
        None  # device software version (SIG 0x001A) the property needs
    )
    set_access: int = (
        3  # Admin Set userAccess byte (the app: 1 for DimMode, DeviceLock, delays)
    )
    source: Literal["app", "firmware"] = (
        "app"  # app = the JUNG HOME app uses it; firmware = gateway id list only
    )

    @property
    def readable(self) -> bool:
        """Return whether the app reads it (everything but write-only triggers)."""
        return self.access != "wo"

    @property
    def writable(self) -> bool:
        """Return whether the app writes it."""
        return self.access != "ro"

    @property
    def vendor(self) -> bool:
        """Return whether an LBC vendor server hosts it (the `vendor_property_*` builders) rather than a SIG model."""
        return self.server in ("admin", "manufacturer", "user")

    @property
    def secret(self) -> bool:
        """Return whether the value is a credential (`Text(secret=True)`): never shown, not even as raw hex."""
        return isinstance(self.codec, Text) and self.codec.secret


def parse_version(text: str) -> Version:
    """Parse `"2.2.0.2"` into `(2, 2, 0, 2)` (missing parts are 0)."""
    parts = [int(p) for p in text.split(".")]
    if not 1 <= len(parts) <= 4:
        raise ValueError(f"not a version: {text!r}")
    parts += [0] * (4 - len(parts))
    return (parts[0], parts[1], parts[2], parts[3])


def supported(spec: PropertySpec, version: Version | str | None) -> bool:
    """Return whether a device running `version` (SIG 0x001A, e.g. `"2.2.0.2"`) supports the property; unknown -> True.

    A revision `parse_version` cannot read (more than four digit pairs decode fine from the wire) is unknown too.
    """
    if spec.firmware_min is None or version is None:
        return True
    if isinstance(version, str):
        try:
            version = parse_version(version)
        except ValueError:
            return True
    return version >= spec.firmware_min


def _fw(
    pid: int,
    name: str,
    category: frozenset[int],
    codec: Codec = RAW,
    access: Access = "ro",
    server: Server = "admin",
    **kw: Any,
) -> PropertySpec:
    """Build a firmware-only id (`docs/android/properties.md` §1.10): layout unknown unless a codec is given."""
    return PropertySpec(
        pid, name, server, access, codec, products=category, source="firmware", **kw
    )


def _led_specs() -> list[PropertySpec]:
    """Build the 16 LED triples (`docs/android/properties.md` §1.8): the app knows on/off modes of LEDs 1..6, the firmware 16."""
    out: list[PropertySpec] = []
    for led in range(1, 17):
        products = LED_HOSTS if led == 1 else TWO_GANG if led == 2 else frozenset()
        source: Literal["app", "firmware"] = "app" if led <= 6 else "firmware"
        slots: tuple[tuple[LedSlot, Codec, str], ...] = (
            ("channel", RAW, "channel_selection"),
            ("on", RGB_MODE, "mode_on"),
            ("off", RGB_MODE, "mode_off"),
        )
        for slot, codec, suffix in slots:
            out.append(
                PropertySpec(
                    led_property_id(led, slot),
                    f"led{led}_{suffix}",
                    "admin",
                    "rw",
                    codec,
                    "led",
                    products=products,
                    source="firmware" if slot == "channel" else source,
                )
            )
    return out


# fmt: off
_VENDOR_SPECS: list[PropertySpec] = [
    # --- §1.2 node / identification / security
    PropertySpec(0x0001, "device_lock", "admin", "rw", Flags(DEVICE_LOCK_FLAGS, 2), products=NOT_GATEWAY, set_access=1),  # read-modify-write per bit
    PropertySpec(0x0002, "insert_id", "user", "ro", InsertIdCodec(), products=ALL_PRODUCTS),
    PropertySpec(0x0003, "secure_element_version", "manufacturer", "ro", VERSION_LE, products=ALL_PRODUCTS),
    PropertySpec(0x0004, "bootloader_version", "manufacturer", "ro", VERSION_LE, products=ALL_PRODUCTS),
    PropertySpec(0x0005, "stm32_version", "manufacturer", "ro", VERSION_LE, products=RTR),  # firmware: APPLICATION_IMAGE_VERSION
    PropertySpec(0x0007, "astro_scheduler_register", "admin", "rw", AstroRegisterCodec(), products=NOT_GATEWAY),
    PropertySpec(0x0008, "astro_scheduler_status", "admin", "rw", AstroStatusCodec(), products=NOT_GATEWAY),
    PropertySpec(0x0009, "enforced_output", "admin", "rw", EnforcedOutputCodec(), element="load", products=LOCKABLE),
    PropertySpec(0x000F, "automatic_dst", "admin", "rw", BOOL, products=LOAD_HOSTS | MINI_BLINDS, firmware_min=(1, 1, 0, 0)),
    PropertySpec(0x0012, "touch_sensitivity", "admin", "rw", BOOL, products=RTR, firmware_min=(1, 0, 3, 3)),
    PropertySpec(0x0013, "dim_mode", "admin", "rw", Enum(DIM_MODE), element="load", products=DIMMER_HOSTS, set_access=1),
    # --- §1.10 general (firmware only)
    _fw(0x000A, "application_schema_version", ALL_PRODUCTS, VERSION_LE),
    _fw(0x000B, "stack_schema_version", ALL_PRODUCTS, VERSION_LE),
    _fw(0x000C, "master_schema_version", ALL_PRODUCTS, VERSION_LE),
    _fw(0x000D, "co_processor_schema_version", ALL_PRODUCTS, VERSION_LE),
    # write 1: a dimmer / DALI insert publishes every light state (OnOff, Level, Lightness, CTL) to its group at
    # once; switch inserts and sockets acknowledge and publish nothing (`hidden-features.md` §10)
    _fw(0x000E, "server_state_publish_request", ALL_PRODUCTS, U8, access="wo"),
    _fw(0x0010, "lpn_state_timeout", PB_BATTERY | MINI_INPUT),  # vendor id; SIG 0x0010 lives in SIG_PROPERTIES
    _fw(0x0011, "battery_test_raw_data", PB_BATTERY | MINI_INPUT),
    # Raw until the supervised probe settles them (review-4 brief 36, `docs/on-air-sweep.md` A7 / C6): 0x0F00 reads
    # `0100` on key elements and the socket's meter element, meaning unknown; the two runtime statistics sit on the
    # Manufacturer server of every node (`hidden-features.md` §2), empty on the socket
    _fw(0x0F00, "transmission_settings", ALL_PRODUCTS),
    _fw(0x0F01, "current_runtime_stats", ALL_PRODUCTS, server="manufacturer"),
    _fw(0x0F02, "all_time_runtime_stats", ALL_PRODUCTS, server="manufacturer"),
    # --- §1.3 load parameters (element = the load)
    PropertySpec(0x1001, "on_delay", "admin", "rw", DELAY_MS, element="load", unit="s", min=0, max=14400, step=1, products=LOAD_HOSTS, set_access=1),
    PropertySpec(0x1002, "off_delay", "admin", "rw", DELAY_MS, element="load", unit="s", min=0, max=14400, step=1, products=LOAD_HOSTS, set_access=1),
    PropertySpec(0x1007, "timed_on_duration", "admin", "rw", MS32, element="load", unit="s", min=0, max=14400, step=1, products=LOAD_HOSTS, set_access=1),  # "run-on time"
    PropertySpec(0x100A, "prewarning", "admin", "rw", BOOL, element="load", products=LAMP_HOSTS),
    PropertySpec(0x100B, "manual_off_enable", "admin", "rw", BOOL, element="load", products=LOAD_HOSTS),
    PropertySpec(0x100C, "invert_output", "admin", "rw", BOOL, element="load", products=LAMP_HOSTS),
    PropertySpec(0x100D, "switch_blocking_time", "admin", "rw", U16, element="load", unit="ms", min=100, max=10000, step=1, products=LOAD_HOSTS),
    PropertySpec(0x100E, "dim_to_warm", "admin", "rw", BOOL, element="load", products=PB_MAINS | MINI_DALI),
    # unsupported on FW 2.0.0.4 ("Lbc Property doesn't exist"); the exact minimum is unknown, 2.2.0.x works
    PropertySpec(0x1014, "rtr_operation_mode", "admin", "rw", BOOL, element="load", products=PB_MAINS | SOCKETS | MINI_SWITCH | RTR, firmware_min=(2, 0, 0, 5)),
    # hotel / basic light / night / presentation: listed by the DALI insert only (0x1008 = 51, 0x1009 = 0, 0x1011 =
    # 51, two 8-byte presentation records; `hidden-features.md` §2); names from the gateway firmware, semantics
    # and units unknown — Raw until the supervised probe settles them (on-air sweep C6)
    _fw(0x1008, "hotel_dimm_value", LOAD_HOSTS, access="rw"),
    _fw(0x1009, "basic_light_function_enable", LOAD_HOSTS, access="rw"),
    # u32 wear counters, read on the socket's load element (118 / 79, `docs/hidden-features.md` §2)
    _fw(0x100F, "total_off_on_cycles", LOAD_HOSTS, U32),
    _fw(0x1010, "power_on_cycles", LOAD_HOSTS, U32),
    _fw(0x1011, "night_dimm_value", LOAD_HOSTS, access="rw"),
    _fw(0x1012, "presentation_mode_enable", LOAD_HOSTS, access="rw"),
    _fw(0x1013, "presentation_mode_time", LOAD_HOSTS, access="rw"),
    # --- §1.4 blinds (element = the blind load)
    PropertySpec(0x1101, "motor_reversal_time", "admin", "rw", U16, element="load", unit="ms", min=300, max=10000, step=1, products=BLIND_HOSTS),
    PropertySpec(0x1102, "running_time", "admin", "rw", Duration(2, "s"), element="load", unit="s", min=5, max=600, step=1, products=BLIND_HOSTS),  # app writes u16 s (docs §5 q.2)
    PropertySpec(0x1103, "slats_move_time", "admin", "rw", U16, element="load", unit="ms", min=0, max=10000, step=1, products=BLIND_HOSTS),  # >= 300 in blinds mode
    PropertySpec(0x1104, "blind_operation_mode", "admin", "rw", Enum(BLIND_MODE), element="load", products=BLIND_HOSTS),
    PropertySpec(0x1105, "move_on_power_mode", "admin", "rw", Enum(MOVE_ON_POWER), element="load", products=BLIND_HOSTS),
    PropertySpec(0x1106, "blind_position_on_power", "admin", "rw", POSITION, element="load", unit="%", min=0, max=100, step=1, products=BLIND_HOSTS),
    PropertySpec(0x1107, "slat_position_on_power", "admin", "rw", POSITION, element="load", unit="%", min=0, max=100, step=1, products=BLIND_HOSTS),
    PropertySpec(0x1108, "blinds_invert_output", "admin", "rw", BOOL, element="load", products=BLIND_HOSTS),
    PropertySpec(0x110A, "blind_ventilation_position", "admin", "rw", POSITION, element="load", unit="%", min=0, max=100, step=1, products=BLIND_HOSTS),
    PropertySpec(0x110B, "slat_ventilation_position", "admin", "rw", POSITION, element="load", unit="%", min=0, max=100, step=1, products=BLIND_HOSTS),
    PropertySpec(0x110D, "reference_run", "admin", "rw", BOOL, element="load", products=BLIND_HOSTS),  # write True to start; reads True while running
    _fw(0x1109, "blinds_step_up_down", BLIND_HOSTS, access="rw"),
    _fw(0x110C, "blinds_wind_alert_enable", BLIND_HOSTS, access="rw"),
    # --- §1.5 room thermostat
    PropertySpec(0x1201, "controller_type", "admin", "rw", Enum(CONTROLLER_TYPE), products=RTR),
    PropertySpec(0x1203, "comfort_temperature", "admin", "rw", TEMP_001C, unit="°C", min=5, max=30, step=0.5, products=RTR),
    PropertySpec(0x1204, "eco_temperature", "admin", "rw", TEMP_001C, unit="°C", min=5, max=30, step=0.5, products=RTR),  # "standby"
    PropertySpec(0x1205, "frost_protection_temperature", "admin", "rw", TEMP_001C, unit="°C", min=5, max=30, step=0.5, products=RTR),  # "freeze"
    PropertySpec(0x1208, "heating_optimisation", "admin", "rw", BOOL, products=RTR),  # declared, no UI
    PropertySpec(0x120A, "valve_output", "admin", "rw", Enum(VALVE_OUTPUT), products=RTR),
    PropertySpec(0x120B, "hvac_mode", "admin", "rw", Enum(HVAC_MODE), products=RTR, firmware_min=(2, 2, 0, 0)),
    PropertySpec(0x120D, "boost_mode", "admin", "rw", BOOL, products=RTR),
    PropertySpec(0x1221, "sensor_selection", "admin", "rw", Enum(SENSOR_SELECTION), products=RTR),
    PropertySpec(0x1224, "sensor_offset", "admin", "rw", TEMP_001C, unit="K", min=-5, max=5, step=0.5, products=RTR),
    PropertySpec(0x1240, "main_page", "admin", "rw", Enum(MAIN_PAGE), products=RTR),
    PropertySpec(0x1246, "scheduler_enabled", "admin", "rw", BOOL, products=RTR),  # "automatic operation" (auto / manual)
    PropertySpec(0x1247, "backlight_auto_off", "admin", "rw", BOOL, products=RTR),
    PropertySpec(0x1249, "scheduler_function_status", "admin", "ro", BOOL, products=RTR),
    _fw(0x1202, "rtr_cooling_enable", RTR, access="rw"),
    _fw(0x1206, "rtr_cooling_temperature", RTR, TEMP_001C, "rw", unit="°C"),  # presumably like 0x1203..0x1205
    _fw(0x1207, "rtr_floor_max_temperature", RTR, TEMP_001C, "rw", unit="°C"),
    _fw(0x120C, "rtr_target_temperature", RTR, TEMP_001C, "rw", unit="°C"),  # the app sets the target via Generic Level
    _fw(0x120E, "rtr_holiday_temperature", RTR, TEMP_001C, "rw", unit="°C"),
    _fw(0x1220, "rtr_drop_of_temp_enable", RTR, access="rw"),
    _fw(0x1222, "rtr_btsens_enabled", RTR, access="rw"),
    _fw(0x1223, "rtr_sensor_temp_actual", RTR),
    _fw(0x1225, "rtr_drop_of_temp_state", RTR, BOOL),  # the app's DropOfTempState (0x1212) parser reads one byte
    _fw(0x1241, "rtr_fahrenheit_enable", RTR, access="rw"),
    _fw(0x1242, "rtr_temp_adjust_min", RTR, access="rw"),
    _fw(0x1243, "rtr_temp_adjust_max", RTR, access="rw"),
    _fw(0x1245, "rtr_extended_mode_enable", RTR, access="rw"),
    _fw(0x1300, "scene_escape_action_set1", frozenset(), access="rw"),
    _fw(0x1301, "scene_escape_action_set2", frozenset(), access="rw"),
    _fw(0x1302, "scene_escape_action_set3", frozenset(), access="rw"),
    _fw(0x1303, "scene_escape_action_set4", frozenset(), access="rw"),
    # --- §1.6 keys (per-key element unless noted)
    PropertySpec(0x5001, "button_layout", "admin", "rw", Enum(BUTTON_LAYOUT, size=2), products=KEY_HOSTS),  # changing it re-provisions
    PropertySpec(0x5002, "key_scene_config", "admin", "rw", SceneConfigCodec(), element="key", products=KEY_HOSTS),
    PropertySpec(0x5003, "key_mode", "admin", "rw", Enum(KEY_MODE), element="key", products=KEY_HOSTS),
    PropertySpec(0x5004, "turn_on_threshold", "admin", "rw", ThresholdCodec(), products=SOCKET_METERING),
    PropertySpec(0x5005, "turn_off_threshold", "admin", "rw", ThresholdCodec(), products=SOCKET_METERING),
    PropertySpec(0x5006, "key_property_mode", "admin", "rw", PropertyModeCodec(), element="key", products=KEY_HOSTS),
    PropertySpec(0x5007, "key_property_value_up", "admin", "rw", RAW, element="key", products=KEY_HOSTS),  # value bytes of the 0x5006 target
    PropertySpec(0x5008, "key_property_value_down", "admin", "rw", RAW, element="key", products=KEY_HOSTS),
    PropertySpec(0x5009, "input_edge_detection", "admin", "rw", EdgeDetectionCodec(), element="key", products=MINI_ACTUATORS),
    PropertySpec(0x5010, "daily_energy_chart", "user", "ro", EnergyChart(2), unit="Wh", products=ENERGY_HOSTS),
    PropertySpec(0x5011, "monthly_energy_chart", "user", "ro", EnergyChart(3), unit="Wh", products=ENERGY_HOSTS),
    PropertySpec(0x5012, "key_event", "user", "ro", KeyEventCodec(), element="key", products=KEY_HOSTS, source="firmware"),
    # the gateway writes the status LED with a User Property *Status* (opcode 0x11), not a Set (cross-repo §1.2)
    PropertySpec(0x5013, "key_status_led", "manufacturer", "rw", BOOL, element="key", products=KEY_HOSTS, source="firmware"),
    PropertySpec(0x5014, "meter_timestamp", "manufacturer", "ro", Timestamp7(), element="meter", products=ENERGY_HOSTS, source="firmware"),  # also on the User server
    _fw(0x500A, "key_rtr_temp_step_size", KEY_HOSTS, access="rw", element="key"),
    _fw(0x500C, "key_toggle_enable", KEY_HOSTS, access="rw", element="key"),  # reads 1; effect unknown (sweep C6)
    # --- §1.7 detectors (element = the sensor element, the node's highest)
    PropertySpec(0x6001, "walking_test", "admin", "rw", BOOL, element="detector", products=DETECTORS),
    PropertySpec(0x6003, "presence_control", "admin", "rw", BOOL, element="detector", products=DETECTORS),
    PropertySpec(0x6004, "current_brightness", "manufacturer", "ro", U16, element="detector", unit="lx", products=DETECTORS),  # app declares a Set, UI reads only
    PropertySpec(0x6005, "presence_control_pir", "manufacturer", "ro", U8, element="detector", products=DETECTORS),  # polled during the walking test
    PropertySpec(0x6006, "detector_operation_mode", "admin", "rw", Enum(DETECTOR_MODE), element="detector", products=DETECTORS),
    # the app's activation-area detents 0 / 25 / 50 / 75 / 100 % (`DetectorView`); segment C on the presence detector
    # only, the motion detectors have two (`p012a8/a.java` reads and writes A / B)
    PropertySpec(0x6008, "pir_sensor_a", "admin", "rw", PERCENT, element="detector", unit="%", min=0, max=100, step=25, products=DETECTORS),
    PropertySpec(0x6009, "pir_sensor_b", "admin", "rw", PERCENT, element="detector", unit="%", min=0, max=100, step=25, products=DETECTORS),
    PropertySpec(0x600A, "pir_sensor_c", "admin", "rw", PERCENT, element="detector", unit="%", min=0, max=100, step=25, products=PRESENCE_DETECTOR),
    # the app's slider moves in 5 lx steps (`SliderContentType.SWITCH_ON_BRIGHTNESS`)
    PropertySpec(0x600F, "switch_on_brightness", "admin", "rw", U16, element="detector", unit="lx", min=5, max=1000, step=5, products=DETECTORS),
    PropertySpec(0x6015, "day_mode", "admin", "rw", BOOL, element="detector", products=DETECTORS),
    PropertySpec(0x6016, "forced_off", "admin", "rw", Enum(FORCED_OFF), element="detector", products=DETECTORS),
    PropertySpec(0x6017, "operation_site", "admin", "rw", Enum(OPERATION_SITE), element="detector", products=DETECTORS),
    PropertySpec(0x6021, "repetition_time", "admin", "rw", U8, element="detector", products=DETECTORS),  # unit not visible in the app
    # declared by the app with a TODO() parser (1 byte read, layout unknown); present in the firmware's id list
    _fw(0x6007, "presence_simulation", DETECTORS, element="detector"),
    _fw(0x600B, "hysteresis_offset", DETECTORS, element="detector"),
    _fw(0x600C, "min_brightness_change", DETECTORS, element="detector"),
    _fw(0x600D, "min_hysteresis", DETECTORS, element="detector"),
    _fw(0x6010, "followup_time", DETECTORS, element="detector"),
    _fw(0x6011, "alarm_function", DETECTORS, element="detector"),
    _fw(0x6012, "impulse_mode", DETECTORS, element="detector"),
    _fw(0x6013, "dynamic_followup_time", DETECTORS, element="detector"),
    _fw(0x6014, "turn_off_prewarning", DETECTORS, element="detector"),
    _fw(0x6002, "presence_alarm_enable", DETECTORS, access="rw", element="detector"),
    _fw(0x600E, "min_switch_off_brightness", DETECTORS, access="rw", element="detector"),
    _fw(0x6018, "constant_light_enable", DETECTORS, access="rw", element="detector"),
    _fw(0x6019, "constant_light_set_value", DETECTORS, access="rw", element="detector"),
    _fw(0x601A, "constant_light_hysteresis_set_point", DETECTORS, access="rw", element="detector"),
    _fw(0x601B, "constant_light_hysteresis_switch_off", DETECTORS, access="rw", element="detector"),
    _fw(0x601C, "constant_light_min_step_size", DETECTORS, access="rw", element="detector"),
    _fw(0x601D, "constant_light_min_step_time", DETECTORS, access="rw", element="detector"),
    _fw(0x601E, "constant_light_max_lux_level", DETECTORS, access="rw", element="detector"),
    _fw(0x601F, "constant_light_reduction_ppart", DETECTORS, access="rw", element="detector"),
    _fw(0x6020, "night_light_level", DETECTORS, access="rw", element="detector"),
    # --- §1.8 LEDs
    *_led_specs(),
    _fw(0xA100, "battery_changed", PB_BATTERY | MINI_INPUT),
    # served by the vendor-only element (location 0x0044) on the Manufacturer / User servers — on every push-button,
    # whatever its insert (read on air, `docs/hidden-features.md` §1)
    PropertySpec(0xA200, "wind_alert_active", "manufacturer", "ro", RAW, element="aux", products=BLIND_HOSTS | PB_MAINS, source="firmware"),
    PropertySpec(0xA201, "wind_alert_priority", "manufacturer", "rw", RAW, element="aux", products=BLIND_HOSTS | PB_MAINS, source="firmware"),
    # --- §1.9 gateway (credentials: never log 0xC001)
    PropertySpec(0xC000, "gateway_api_status", "manufacturer", "ro", Flags(GATEWAY_STATUS_FLAGS), products=GATEWAY, source="firmware"),
    PropertySpec(0xC001, "gateway_api_token", "manufacturer", "ro", Text(secret=True), products=GATEWAY),
    PropertySpec(0xC002, "gateway_ip", "manufacturer", "ro", Text(), products=GATEWAY),
    PropertySpec(0xC003, "gateway_fingerprint", "manufacturer", "ro", Text(), products=GATEWAY),
]

# SIG device properties (§1.1) served by the SIG Generic Property / Sensor models: a separate id space (the vendor
# ids 0x0010 / 0x0011 above are different properties). Which element serves what was read off the devices'
# own property lists (`docs/hidden-features.md` §2): the primary element has the identity block
# (0x000C, 0x0010, 0x0011, 0x001A) and 0x006D, the metering element (`element="meter"`) the energy counters — on
# the Admin server 0x006A (resettable), on the Manufacturer server 0x0072 and 0x000D; the User server mirrors all.
_SIG_SPECS: list[PropertySpec] = [
    PropertySpec(0x000C, "date_of_manufacture", "sig_manufacturer", "ro", DateUTC(), products=ALL_PRODUCTS, source="firmware"),  # field-tested
    PropertySpec(0x0010, "hardware_revision", "sig_manufacturer", "ro", Text(), products=ALL_PRODUCTS),  # ASCII, NUL-padded: b"10000000" + 8 NULs on air; the app stores the bytes reversed and shows none
    PropertySpec(0x0011, "manufacturer_name", "sig_manufacturer", "ro", Text(), products=ALL_PRODUCTS),
    PropertySpec(0x001A, "software_version", "sig_manufacturer", "ro", ASCII_VERSION, products=ALL_PRODUCTS),
    PropertySpec(0x006A, "total_energy", "sig_admin", "rw", Counter(4), unit="Wh", products=ENERGY_HOSTS, element="meter"),  # write 0 = reset; field-tested
    PropertySpec(0x006D, "power_on_time", "sig_admin", "rw", Counter(4), unit="h", products=SOCKET_METERING),  # uint24 h on air; reset int32 0
    PropertySpec(0x000D, "energy_since_turn_on", "sig_manufacturer", "ro", Counter(4), unit="Wh", products=ENERGY_HOSTS, element="meter", source="firmware"),  # field-tested
    PropertySpec(0x0072, "precise_total_energy", "sig_manufacturer", "ro", Counter(4), unit="Wh", products=ENERGY_HOSTS, element="meter", source="firmware"),  # field-tested
    PropertySpec(0x004F, "present_ambient_temperature", "sensor", "ro", Scaled(1, 0.5, signed=True, unknown=0x7F), unit="°C", products=RTR),
    PropertySpec(0x0052, "present_input_power", "sensor", "ro", Scaled(3, 0.1), unit="W", products=SOCKET_METERING, element="meter", source="firmware"),  # gateway `PresentDeviceInputPower`; reads 0 on air
    PropertySpec(0x0057, "present_input_current", "sensor", "ro", Scaled(2, 0.01), unit="A", products=SOCKET_METERING, element="meter", source="firmware"),  # gateway `PresentInputCurrent`; reads 0 on air
    PropertySpec(0x005C, "present_output_current", "sensor", "ro", Scaled(2, 0.01), unit="A", products=SOCKET_METERING, element="meter"),  # field-tested
    PropertySpec(0x005D, "present_output_voltage", "sensor", "ro", Scaled(2, 1.0), unit="V", products=SOCKET_METERING, element="meter"),  # 1 V steps, not SIG 1/64 V
    PropertySpec(0x0081, "active_power", "sensor", "ro", Scaled(3, 0.1), unit="W", products=ENERGY_HOSTS, element="meter"),
]
# fmt: on


def _index(specs: list[PropertySpec]) -> dict[int, PropertySpec]:
    out: dict[int, PropertySpec] = {}
    for spec in specs:
        if spec.id in out:
            raise ValueError(f"duplicate property id 0x{spec.id:04X}")
        out[spec.id] = spec
    return out


PROPERTIES: dict[int, PropertySpec] = _index(_VENDOR_SPECS)
"""Every LBC vendor property by id."""
SIG_PROPERTIES: dict[int, PropertySpec] = _index(_SIG_SPECS)
"""SIG device properties by id (Generic Property servers and the Sensor server)."""
_BY_NAME: dict[str, PropertySpec] = {s.name: s for s in [*_VENDOR_SPECS, *_SIG_SPECS]}


# ----------------------------------------------------------------------------- helpers


def spec_for(property_id: int, *, sig: bool = False) -> PropertySpec:
    """Return the spec of a vendor (default) or SIG property id; KeyError when unknown."""
    return (SIG_PROPERTIES if sig else PROPERTIES)[property_id]


def by_name(name: str) -> PropertySpec:
    """Return the spec with this (vendor or SIG) name; KeyError when unknown."""
    return _BY_NAME[name]


def for_product(
    product_id: int, *, include_firmware_only: bool = False
) -> list[PropertySpec]:
    """Return every property (vendor first, then SIG, by id) the app uses on this product; optionally the firmware-only ids too."""
    specs = sorted([*_VENDOR_SPECS, *_SIG_SPECS], key=lambda s: (not s.vendor, s.id))
    return [
        s
        for s in specs
        if product_id in s.products and (include_firmware_only or s.source == "app")
    ]


def encode(property_id: int, value: Any, *, sig: bool = False) -> bytes:
    """Return the wire bytes of `value` for the property (ValueError when it does not fit, KeyError when unknown)."""
    return spec_for(property_id, sig=sig).codec.encode(value)


def decode(property_id: int, data: bytes, *, sig: bool = False) -> Any:
    """Return the Python value of a property Status value (ValueError when malformed, KeyError when unknown)."""
    return spec_for(property_id, sig=sig).codec.decode(data)


REDACTED = "<redacted>"  # what a secret property's value reads as in logs and the CLI


def format_value(spec: PropertySpec, value: Any) -> str:
    """Render a decoded value for logs and the CLI (secrets are redacted)."""
    kind = spec.codec.kind
    if spec.secret:
        text = REDACTED
    elif kind == "bool":
        text = "on" if value else "off"
    elif kind == "flags":
        text = ",".join(name for name, on in value.items() if on) or "none"
    elif kind == "rgb_mode":
        night = "on" if value.night_mode else "off"
        text = f"rgb({value.red},{value.green},{value.blue}) night_mode={night}"
    elif kind == "list":
        text = "[" + ",".join("-" if v is None else f"{v:g}" for v in value) + "]"
    elif kind == "raw":
        text = value.hex() or "(empty)"
    elif kind in ("int", "float", "duration", "percent"):
        text = "unknown" if value is None else f"{value:g}{spec.unit or ''}"
    elif kind in ("date", "datetime"):
        text = "unknown" if value is None else str(value)
    else:  # enum names, versions, strings, struct reprs
        text = str(value)
    return text


def describe_status(property_id: int, data: bytes, *, sig: bool = False) -> str:
    """Return `name=value` for a property Status value; unknown ids and undecodable values fall back to hex, never raises.

    A secret property (`PropertySpec.secret`) reads `name=<redacted>` whatever the bytes: the hex fallback would
    otherwise print the gateway API token when a malformed value arrives.
    """
    try:
        spec = spec_for(property_id, sig=sig)
    except KeyError:
        return f"0x{property_id:04X}={data.hex() or '(empty)'}"
    if spec.secret:
        return f"{spec.name}={REDACTED}"
    try:
        return f"{spec.name}={format_value(spec, spec.codec.decode(data))}"
    except ValueError:
        return f"{spec.name}=?{data.hex()}"
