"""The two JUNG vendor models beyond the property servers: JH Scheduler (`0x0527:1016`) and Scene Action Setup (`0x0527:1017`).

Both live on every load element. The Scene Action Setup server stores what the element does when a scene is
recalled — the app writes it with the scene (switch on/off, a lightness, lightness + colour temperature, a blind
and slat position, a target temperature) — and the CDB / export does not hold it, so this module is the only way
to read "what does this light do in scene 8". The JH Scheduler holds up to 16 time / astro schedules per element
with the same action payload; the user's installation has none, so only the empty statuses were seen on air.

Formats from the decompiled app (`docs/android/vendor-models.md` §4: fields are packed LSB-first from bit 0, so a
byte-aligned 16-bit field is a little-endian u16) and verified on air against nine nodes
(`docs/hidden-features.md` §10): the scene list, switching, lightness + colour temperature, the absent-scene reply
and every scheduler sub-command of an empty slot.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .advert import JUNG_COMPANY_ID
from .pdu import encode_opcode

__all__ = [
    "ACTION_BLINDS",
    "ACTION_LIGHTNESS",
    "ACTION_LIGHTNESS_CT",
    "ACTION_NAMES",
    "ACTION_NONE",
    "ACTION_PAYLOAD_LENGTH",
    "ACTION_SWITCH",
    "ACTION_TEMPERATURE",
    "DAYS",
    "JH_SCHEDULER_GET",
    "JH_SCHEDULER_SET",
    "JH_SCHEDULER_STATUS",
    "JUNG_CID",
    "LEVEL_MAX",
    "LEVEL_MIN",
    "LIGHTNESS_MAX",
    "NO_ACTION",
    "SCENE_ACTION_SETUP_GET",
    "SCENE_ACTION_SETUP_SET",
    "SCENE_ACTION_SETUP_STATUS",
    "SCENE_LIST",
    "SCENE_NONE",
    "SCHEDULER_MODEL",
    "SCHEDULE_TYPES",
    "SLOTS",
    "SLOT_STATUS",
    "SUB_ACTION",
    "SUB_EFFECTIVE_TIME",
    "SUB_LIST",
    "SUB_NAMES",
    "SUB_SCHEDULE",
    "UNSET_TIME",
    "Action",
    "EffectiveTime",
    "SceneActionStatus",
    "Schedule",
    "SchedulerStatus",
    "days_mask",
    "decode_action",
    "decode_scene_action_status",
    "decode_scheduler_status",
    "describe_vendor_model",
    "scene_action_get",
    "scene_action_reply_to",
    "scene_action_set",
    "scheduler_action_set",
    "scheduler_get",
    "scheduler_list_get",
    "scheduler_type_set",
]

# the JUNG vendor model of the JH Scheduler a load hosts (its messages: JH_SCHEDULER_GET / _SET / _STATUS)
SCHEDULER_MODEL = "05271016"

JUNG_CID = JUNG_COMPANY_ID

# 6-bit vendor opcodes (on air `C0 | op, 27 05`); `docs/android/vendor-models.md` §2
JH_SCHEDULER_GET, JH_SCHEDULER_STATUS, JH_SCHEDULER_SET = 0x12, 0x13, 0x14
SCENE_ACTION_SETUP_GET, SCENE_ACTION_SETUP_STATUS, SCENE_ACTION_SETUP_SET = (
    0x16,
    0x17,
    0x18,
)

# ----------------------------------------------------------------------------- actions (scene and scheduler alike)

ACTION_NONE, ACTION_SWITCH, ACTION_LIGHTNESS, ACTION_LIGHTNESS_CT = 0, 1, 2, 3
ACTION_BLINDS, ACTION_TEMPERATURE = 4, 5
ACTION_NAMES = {
    ACTION_NONE: "no action",
    ACTION_SWITCH: "switch",
    ACTION_LIGHTNESS: "lightness",
    ACTION_LIGHTNESS_CT: "lightness + colour temperature",
    ACTION_BLINDS: "blinds + slats",
    ACTION_TEMPERATURE: "target temperature",
}
ACTION_PAYLOAD_LENGTH = 5  # the 40-bit payload after the action code, always padded
LIGHTNESS_MAX = 0xFFFF
LEVEL_MIN, LEVEL_MAX = -32768, 32767


@dataclass(frozen=True)
class Action:
    """What an element does for a scene or a schedule: the action code and the fields that code uses.

    `lightness` is the Light Lightness value (0..65535), `temperature_k` the colour temperature in Kelvin,
    `blind` / `slat` the Generic Level positions (-32768..32767, the app maps percent onto that range),
    `temperature_c` the thermostat target in °C (sent in centi-degrees).
    """

    code: int
    on: bool | None = None
    lightness: int | None = None
    temperature_k: int | None = None
    blind: int | None = None
    slat: int | None = None
    temperature_c: float | None = None

    def encode(self) -> bytes:
        """Return the action code followed by its 40-bit payload (6 bytes), as both models carry it."""
        if self.code == ACTION_NONE:
            payload = b""
        elif self.code == ACTION_SWITCH:
            payload = bytes([1 if self.on else 0])
        elif self.code == ACTION_LIGHTNESS:
            payload = _lightness(self.lightness)
        elif self.code == ACTION_LIGHTNESS_CT:
            payload = _lightness(self.lightness) + _u16(
                self.temperature_k, "colour temperature", 0, 0xFFFF
            )
        elif self.code == ACTION_BLINDS:
            payload = _level(self.blind, "blind") + _level(self.slat, "slat")
        elif self.code == ACTION_TEMPERATURE:
            if self.temperature_c is None:
                raise ValueError("a target temperature action needs temperature_c")
            payload = _u16(round(self.temperature_c * 100), "temperature", 0, 0xFFFF)
        else:
            raise ValueError(f"unknown action code {self.code}")
        return bytes([self.code]) + payload.ljust(ACTION_PAYLOAD_LENGTH, b"\x00")

    def describe(self) -> str:
        """One phrase: `switch on`, `lightness 100% 2000K`, `blinds 50% slats 0%`, `target 21.5°C`, `no action`."""
        if self.code == ACTION_SWITCH:
            return f"switch {'on' if self.on else 'off'}"
        if self.code == ACTION_LIGHTNESS:
            return f"lightness {_percent(self.lightness)}"
        if self.code == ACTION_LIGHTNESS_CT:
            return f"lightness {_percent(self.lightness)} {self.temperature_k}K"
        if self.code == ACTION_BLINDS:
            return (
                f"blinds {_level_percent(self.blind)} slats {_level_percent(self.slat)}"
            )
        if self.code == ACTION_TEMPERATURE:
            return f"target {self.temperature_c:g}°C"
        return ACTION_NAMES.get(self.code, f"action {self.code}")


NO_ACTION = Action(ACTION_NONE)


def _u16(value: int | None, what: str, lo: int, hi: int) -> bytes:
    if value is None or not lo <= value <= hi:
        raise ValueError(f"{what} {value!r} is not {lo}..{hi}")
    return value.to_bytes(2, "little", signed=lo < 0)


def _lightness(value: int | None) -> bytes:
    return _u16(value, "lightness", 0, LIGHTNESS_MAX)


def _level(value: int | None, what: str) -> bytes:
    return _u16(value, what, LEVEL_MIN, LEVEL_MAX)


def _percent(lightness: int | None) -> str:
    return f"{round((lightness or 0) * 100 / LIGHTNESS_MAX)}%"


def _level_percent(level: int | None) -> str:
    return f"{round(((level or 0) - LEVEL_MIN) * 100 / (LEVEL_MAX - LEVEL_MIN))}%"


def decode_action(data: bytes) -> Action:
    """Decode an action code and its payload (`Action.encode` layout; a short payload reads as zero-padded)."""
    if not data:
        raise ValueError("action needs at least the action code")
    code, p = data[0], data[1:].ljust(ACTION_PAYLOAD_LENGTH, b"\x00")
    if code == ACTION_SWITCH:
        return Action(code, on=bool(p[0]))
    if code == ACTION_LIGHTNESS:
        return Action(code, lightness=int.from_bytes(p[:2], "little"))
    if code == ACTION_LIGHTNESS_CT:
        return Action(
            code,
            lightness=int.from_bytes(p[:2], "little"),
            temperature_k=int.from_bytes(p[2:4], "little"),
        )
    if code == ACTION_BLINDS:
        return Action(
            code,
            blind=int.from_bytes(p[:2], "little", signed=True),
            slat=int.from_bytes(p[2:4], "little", signed=True),
        )
    if code == ACTION_TEMPERATURE:
        return Action(code, temperature_c=int.from_bytes(p[:2], "little") / 100)
    return Action(code)


# ----------------------------------------------------------------------------- Scene Action Setup (0x0527:1017)

SCENE_LIST = 0  # scene number 0 in a Get asks for the list of scenes with an action
SCENE_NONE = 0xFFFF  # the scene number of the "no such scene" answer seen on air


def scene_action_reply_to(scene: int) -> Callable[[bytes], bool]:
    """Return whether a Scene Action Setup Status's params answer a Get for `scene` (`SCENE_LIST` = the list).

    Replies are otherwise matched on source and opcode alone, so a late duplicate answer to the Get for one
    scene would be taken for the answer to the next one asked of the same element.
    """

    def fits(params: bytes) -> bool:
        if len(params) < 2:
            return False
        got = int.from_bytes(params[:2], "little")
        return got == scene or (scene != SCENE_LIST and got == SCENE_NONE)

    return fits


def scene_action_get(scene: int = SCENE_LIST) -> bytes:
    """Scene Action Setup Get: the action for `scene`, or (scene 0) the list of scenes this element has actions for.

    The app sends the scene number as a u32; the firmware also takes a u16 (`docs/hidden-features.md` §10).
    """
    if not 0 <= scene <= 0xFFFF:
        raise ValueError(f"scene {scene} is not 0..65535")
    return encode_opcode(SCENE_ACTION_SETUP_GET, JUNG_CID) + scene.to_bytes(4, "little")


def scene_action_set(scene: int, action: Action = NO_ACTION) -> bytes:
    """Scene Action Setup Set (acknowledged by a Status): store `action` for `scene`; `NO_ACTION` removes it.

    Not exercised on air — the layout is the app's (`vendor-models.md` §4.2: scene u16, then the action code and
    payload unless the action is *none*).
    """
    if not 1 <= scene <= 0xFFFF:
        raise ValueError(f"scene {scene} is not 1..65535")
    body = scene.to_bytes(2, "little")
    if action.code != ACTION_NONE:
        body += action.encode()
    return encode_opcode(SCENE_ACTION_SETUP_SET, JUNG_CID) + body


@dataclass(frozen=True)
class SceneActionStatus:
    """A Scene Action Setup Status: the scene list (`scenes`, for a scene-0 Get) or one scene's `action`.

    `action` is None when the element stores nothing for that scene (a 2-byte status, e.g. `FF FF` for an
    unknown scene); `scenes` is None unless this is the list form.
    """

    scene: int
    action: Action | None = None
    scenes: tuple[int, ...] | None = None

    def describe(self) -> str:
        """`scenes=[8, 9, 10, 11]` or `scene 8: switch on` / `scene 65535: no action`."""
        if self.scenes is not None:
            return f"scenes={list(self.scenes)}"
        return f"scene {self.scene}: {self.action.describe() if self.action else 'no action'}"


def decode_scene_action_status(params: bytes) -> SceneActionStatus:
    """Decode a Scene Action Setup Status (`vendor-models.md` §4.2; list form terminated by a 0 entry or the end)."""
    if len(params) < 2:
        raise ValueError(f"Scene Action Setup Status needs 2 bytes, got {len(params)}")
    scene = int.from_bytes(params[:2], "little")
    if scene == SCENE_LIST:
        scenes: list[int] = []
        for i in range(2, len(params) - 1, 2):
            n = int.from_bytes(params[i : i + 2], "little")
            if n == 0:
                break
            scenes.append(n)
        return SceneActionStatus(scene, scenes=tuple(scenes))
    if len(params) == 2:
        return SceneActionStatus(scene)
    return SceneActionStatus(scene, decode_action(params[2:]))


# ----------------------------------------------------------------------------- JH Scheduler (0x0527:1016)

SUB_SCHEDULE, SUB_ACTION, SUB_EFFECTIVE_TIME, SUB_LIST = 0, 1, 2, 15
SUB_NAMES = {
    SUB_SCHEDULE: "schedule",
    SUB_ACTION: "action",
    SUB_EFFECTIVE_TIME: "effective time",
    SUB_LIST: "list",
}
SLOTS = 16
SCHEDULE_TYPES = {
    0: "available",
    1: "reserved",
    2: "timed_inactive",
    3: "timed_active",
    4: "sunrise_inactive",
    5: "sunrise_active",
    6: "sunset_inactive",
    7: "sunset_active",
}
SLOT_STATUS = {
    0: "available",
    1: "central_schedule_id_matched",
    2: "inactive",
    3: "active",
}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # bit 0 = Monday
# an astro window bound left open: hour 31, as the app's UI and the SIG scheduler's astro register write it
UNSET_TIME = (31, 0)


def _bits(data: bytes, start: int, width: int) -> int:
    """Read `width` bits starting at bit `start`, LSB first, the way the app's ByteBuffer reader packs fields."""
    value = 0
    for i in range(width):
        bit = start + i
        value |= ((data[bit >> 3] >> (bit & 7)) & 1) << i
    return value


class _Writer:
    """Append fields LSB-first from bit 0 (the app's writer); `bytes()` pads the last byte with zeros."""

    def __init__(self) -> None:
        self.value = 0
        self.bits = 0

    def add(self, value: int, width: int, what: str) -> _Writer:
        if not 0 <= value < 1 << width:
            raise ValueError(f"{what} {value} does not fit {width} bits")
        self.value |= value << self.bits
        self.bits += width
        return self

    def bytes(self) -> bytes:
        return self.value.to_bytes((self.bits + 7) // 8, "little")


def _header(index: int, sub: int) -> int:
    if not 0 <= index < SLOTS:
        raise ValueError(f"schedule slot {index} is not 0..15")
    if not 0 <= sub <= 0xF:
        raise ValueError(f"sub-command {sub} is not 0..15")
    return (sub << 4) | index


def scheduler_get(index: int, sub: int = SUB_SCHEDULE) -> bytes:
    """JH Scheduler Get for one slot: `sub` 0 schedule, 1 action, 2 computed effective time (`SUB_*`)."""
    return encode_opcode(JH_SCHEDULER_GET, JUNG_CID) + bytes([_header(index, sub)])


def scheduler_list_get(central_schedule_id: int = 0) -> bytes:
    """JH Scheduler Get sub-command 15: the state of all 16 slots (`SchedulerStatus.slots`).

    `central_schedule_id` is echoed and marks slots that belong to it as `central_schedule_id_matched`.
    """
    if not 0 <= central_schedule_id <= 0xFF:
        raise ValueError(f"central schedule id {central_schedule_id} is not 0..255")
    return encode_opcode(JH_SCHEDULER_GET, JUNG_CID) + bytes(
        [_header(0, SUB_LIST), central_schedule_id]
    )


def days_mask(days: frozenset[str] | set[str]) -> int:
    """Day names (`DAYS`) → the 7-bit Monday-first mask the scheduler uses."""
    unknown = set(days) - set(DAYS)
    if unknown:
        raise ValueError(f"unknown day(s) {sorted(unknown)}")
    return sum(1 << DAYS.index(d) for d in days)


def _day_names(mask: int) -> frozenset[str]:
    return frozenset(d for i, d in enumerate(DAYS) if mask >> i & 1)


def _offset(raw: int) -> int:
    """8-bit two's complement minutes around sunrise / sunset (positive = after)."""
    return raw - 256 if raw > 127 else raw


@dataclass(frozen=True)
class Schedule:
    """Slot contents (sub-command 0): when the schedule fires.

    `not_before` / `not_after` are (hour, minute) bounds — the trigger time itself for a timed schedule, the
    window an astro trigger is clamped to for a sunrise / sunset one (`UNSET_TIME` leaves a bound open);
    `offset_min` shifts an astro trigger.
    """

    index: int
    type: int
    days: frozenset[str]
    not_before: tuple[int, int]
    not_after: tuple[int, int]
    offset_min: int

    @property
    def type_name(self) -> str:
        """Return the type as a name: `timed_active`, `sunset_inactive`, …."""
        return SCHEDULE_TYPES.get(self.type, f"type {self.type}")

    def encode(self) -> bytes:
        """JH Scheduler Set sub-command 0, the full 8-byte form (`vendor-models.md` §4.1.1)."""
        if self.type not in SCHEDULE_TYPES:
            raise ValueError(f"unknown schedule type {self.type}")
        for what, (hour, minute) in (
            ("not-before", self.not_before),
            ("not-after", self.not_after),
        ):  # the 5-/6-bit fields also fit 25:61, which is not a time of day
            if (hour, minute) != UNSET_TIME and not (
                0 <= hour <= 23 and 0 <= minute <= 59
            ):
                raise ValueError(f"{what} {hour:02d}:{minute:02d} is not a time of day")
        w = _Writer().add(_header(self.index, SUB_SCHEDULE), 8, "header")
        w.add(self.type, 4, "type").add(0, 4, "pad")
        w.add(days_mask(self.days), 7, "days").add(0, 1, "pad")
        w.add(self.not_before[1], 6, "not-before minute").add(
            self.not_before[0], 5, "not-before hour"
        )
        w.add(self.not_after[1], 6, "not-after minute").add(
            self.not_after[0], 5, "not-after hour"
        )
        w.add(0, 2, "pad")
        if not -128 <= self.offset_min <= 127:
            raise ValueError(f"offset {self.offset_min} min is not -128..127")
        w.add(self.offset_min & 0xFF, 8, "offset").add(0, 8, "pad")
        return encode_opcode(JH_SCHEDULER_SET, JUNG_CID) + w.bytes()

    def describe(self) -> str:
        """`timed_active mon,tue 07:30-07:30 offset +0min`."""
        days = ",".join(d for d in DAYS if d in self.days) or "no days"
        return (
            f"{self.type_name} {days} {self.not_before[0]:02d}:{self.not_before[1]:02d}"
            f"-{self.not_after[0]:02d}:{self.not_after[1]:02d} offset {self.offset_min:+d}min"
        )


@dataclass(frozen=True)
class EffectiveTime:
    """Sub-command 2: the trigger time the node computed for the slot (astro schedules resolved)."""

    index: int
    type: int
    days: frozenset[str]
    time: tuple[int, int]
    offset_min: int

    def describe(self) -> str:
        """`sunset_active mon,tue at 19:42 offset -15min`."""
        days = ",".join(d for d in DAYS if d in self.days) or "no days"
        return (
            f"{SCHEDULE_TYPES.get(self.type, f'type {self.type}')} {days}"
            f" at {self.time[0]:02d}:{self.time[1]:02d} offset {self.offset_min:+d}min"
        )


@dataclass(frozen=True)
class SchedulerStatus:
    """A JH Scheduler Status: the slot, the sub-command and whichever of the four payloads it carries.

    A header-only status (1 byte) is the node's answer to a sub-command it does not know; `slots` (list form)
    holds 16 `SLOT_STATUS` codes, index 0 first.
    """

    index: int
    sub: int
    schedule: Schedule | None = None
    action: Action | None = None
    effective: EffectiveTime | None = None
    slots: tuple[int, ...] | None = None
    central_schedule_id: int | None = None

    def describe(self) -> str:
        """`slot 3 schedule: …`, `slots: 16 available`, `slot 0 sub 3: nothing`."""
        if self.slots is not None:
            counts: dict[str, int] = {}
            for code in self.slots:
                name = SLOT_STATUS.get(code, f"status {code}")
                counts[name] = counts.get(name, 0) + 1
            summary = ", ".join(f"{n} {name}" for name, n in counts.items())
            return f"slots (central id {self.central_schedule_id}): {summary}"
        what = SUB_NAMES.get(self.sub, f"sub {self.sub}")
        if self.schedule is not None:
            return f"slot {self.index} {what}: {self.schedule.describe()}"
        if self.action is not None:
            return f"slot {self.index} {what}: {self.action.describe()}"
        if self.effective is not None:
            return f"slot {self.index} {what}: {self.effective.describe()}"
        return f"slot {self.index} {what}: nothing"


def decode_scheduler_status(params: bytes) -> SchedulerStatus:
    """Decode a JH Scheduler Status of any sub-command (`vendor-models.md` §4.1.1-4.1.4)."""
    if not params:
        raise ValueError("JH Scheduler Status needs the header byte")
    index, sub = params[0] & 0xF, params[0] >> 4
    if sub == SUB_LIST and len(params) >= 6:
        slots = tuple(_bits(params, 16 + 2 * i, 2) for i in range(SLOTS))
        return SchedulerStatus(0, sub, slots=slots, central_schedule_id=params[1])
    if sub == SUB_SCHEDULE and len(params) >= 7:
        schedule = Schedule(
            index,
            _bits(params, 8, 4),
            _day_names(_bits(params, 16, 7)),
            (_bits(params, 30, 5), _bits(params, 24, 6)),
            (_bits(params, 41, 5), _bits(params, 35, 6)),
            _offset(_bits(params, 48, 8)),
        )
        return SchedulerStatus(index, sub, schedule=schedule)
    if sub == SUB_ACTION and len(params) >= 2:
        return SchedulerStatus(index, sub, action=decode_action(params[1:]))
    if sub == SUB_EFFECTIVE_TIME and len(params) >= 7:
        effective = EffectiveTime(
            index,
            _bits(params, 8, 4),
            _day_names(_bits(params, 16, 7)),
            (_bits(params, 30, 5), _bits(params, 24, 6)),
            _offset(_bits(params, 48, 8)),
        )
        return SchedulerStatus(index, sub, effective=effective)
    return SchedulerStatus(index, sub)


def scheduler_action_set(index: int, action: Action) -> bytes:
    """JH Scheduler Set sub-command 1: the action of a slot (7 bytes, `NO_ACTION` = five zero bytes). Not exercised on air."""
    return (
        encode_opcode(JH_SCHEDULER_SET, JUNG_CID)
        + bytes([_header(index, SUB_ACTION)])
        + action.encode()
    )


def scheduler_type_set(index: int, schedule_type: int) -> bytes:
    """JH Scheduler Set sub-command 0 in its 2-byte form: only the type — `0` (available) clears a slot. Not exercised on air."""
    if schedule_type not in SCHEDULE_TYPES:
        raise ValueError(f"unknown schedule type {schedule_type}")
    return encode_opcode(JH_SCHEDULER_SET, JUNG_CID) + bytes(
        [_header(index, SUB_SCHEDULE), schedule_type]
    )


# ----------------------------------------------------------------------------- describe hook for messages.describe()


def describe_vendor_model(opcode: int, params: bytes) -> str | None:  # noqa: PLR0911  # one branch per message
    """Text for the parameters of a JH Scheduler / Scene Action Setup message; None for other vendor opcodes."""
    if opcode == SCENE_ACTION_SETUP_STATUS:
        return decode_scene_action_status(params).describe() if len(params) >= 2 else ""
    if opcode == SCENE_ACTION_SETUP_GET:
        if len(params) < 2:
            return ""
        scene = int.from_bytes(params[:2], "little")
        return "list" if scene == SCENE_LIST else f"scene {scene}"
    if opcode == SCENE_ACTION_SETUP_SET:
        status = decode_scene_action_status(params)
        return f"scene {status.scene}: {status.action.describe() if status.action else 'remove'}"
    if opcode == JH_SCHEDULER_GET:
        if not params:
            return ""
        index, sub = params[0] & 0xF, params[0] >> 4
        if sub == SUB_LIST:
            return "slots" + (f" (central id {params[1]})" if len(params) > 1 else "")
        return f"slot {index} {SUB_NAMES.get(sub, f'sub {sub}')}"
    if opcode in (JH_SCHEDULER_STATUS, JH_SCHEDULER_SET):
        if not params:
            return ""
        if (
            opcode == JH_SCHEDULER_SET
            and len(params) == 2
            and params[0] >> 4 == SUB_SCHEDULE
        ):
            kind = SCHEDULE_TYPES.get(params[1], f"type {params[1]}")
            return f"slot {params[0] & 0xF} schedule type {kind}"  # the 2-byte "type only" Set
        return decode_scheduler_status(params).describe()
    return None
