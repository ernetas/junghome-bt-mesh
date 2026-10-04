"""Config entities: the device parameters of the JUNG HOME app's *Parameters* tab as HA entities.

Every parameter is a JUNG vendor property (`jhmesh/properties.py`, catalogue of `docs/android/properties.md`).
Which entities exist, and the element and HA device each one is bound to, is `properties/targets.py`; the reads,
writes and the status handlers that cache the values are `properties/reader.py`. This module holds the entity
bases over them (`ConfigEntity`, `PropertyEntity`, `SetupStateEntity`, the lock function's, a load's `LoadLock`)
and re-exports what the platforms import from the other two. `number.py`, `select.py`, `switch.py` and
`button.py` only wrap the targets in the platform's entity class.

The status handlers register when `properties/reader.py` is imported, which this module does at its top: the
moment they registered at before the split.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later

from .const import (
    CONFIG_REREAD_INTERVAL,
    DOMAIN,
    PROPERTY_READ_CHUNK,
    SIGNAL_UPDATE,
)
from .entity import JungHomeEntity
from .errors import mesh_errors
from .jhmesh import properties as P
from .jhmesh.devices import BATTERY_PIDS
from .jhmesh.properties import (
    SIG_SOFTWARE_VERSION,
)
from .properties.reader import (
    PROPERTY_SCHEDULER_STATUS,
    READERS,
    PropertyReader,
    applied,
    cacheable,
    cached_value,
    check_outcome,
    is_secret,
    property_reader,
    redacted,
    vendor_server,
)
from .properties.targets import (
    BLIND_PROPERTIES,
    FIRMWARE_ENTITIES,
    FIRST_PAGE,
    GATEWAY_API_STATUS,
    GATEWAY_IP,
    LED_SYNC_PAIRS,
    LIGHTNESS_DEFAULT,
    LIGHTNESS_RANGE,
    LOAD_KINDS,
    ON_POWER_UP,
    PROPERTY_DEVICE_LOCK,
    PROPERTY_KEY_MODE,
    PROPERTY_LOCK,
    PROPERTY_PRESENCE_CONTROL,
    PROPERTY_REFERENCE_RUN,
    PROPERTY_WALKING_TEST,
    EdgeDetectionTarget,
    EntityTarget,
    FlagTarget,
    LedSyncTarget,
    NightModeTarget,
    Page,
    PropertyTarget,
    SetupTarget,
    ValueTarget,
    _elements_for,
    _node_page,
    blind_targets,
    config_targets,
    describe,
    descriptions,
    device_lock_targets,
    edge_detection_targets,
    gateway_ip_targets,
    gateway_status_targets,
    key_mode_targets,
    led_sync_targets,
    lock_targets,
    night_mode_targets,
    node_version,
    property_id_targets,
    retired_unique_ids,
    setup_targets,
)

if TYPE_CHECKING:
    from .coordinator import JungHomeHub
    from .entity import UpdateRead
    from .jhmesh.devices import Thermostat
    from .jhmesh.properties import PropertySpec


LOCK_EXPIRY_MARGIN: Final = (
    5.0  # seconds after a timed lock should have ended before its switch reads it back
)


# What the platforms, `__init__.py`, `keep_awake.py`, `thresholds.py`, `schedules.py` and the tests import from here:
# the entity bases below, and the names that moved to `properties/`, so none of those changed.
__all__ = [
    "ATTR_CONTINUOUS_ON_OFF",
    "ATTR_CONTROLLED_BY",
    "ATTR_LOCKED",
    "ATTR_LOCK_UNTIL",
    "BLIND_PROPERTIES",
    "DOMAIN",
    "FIRMWARE_ENTITIES",
    "FIRST_PAGE",
    "FORCED_HINTS",
    "FORCED_OFF_SPEC",
    "GATEWAY_API_STATUS",
    "GATEWAY_IP",
    "LED_SYNC_PAIRS",
    "LIGHTNESS_DEFAULT",
    "LIGHTNESS_RANGE",
    "LOAD_KINDS",
    "LOCK_MODES",
    "LOCK_SPEC",
    "ON_POWER_UP",
    "PROPERTY_DEVICE_LOCK",
    "PROPERTY_FORCED_OFF",
    "PROPERTY_KEY_MODE",
    "PROPERTY_LOCK",
    "PROPERTY_PRESENCE_CONTROL",
    "PROPERTY_READ_CHUNK",
    "PROPERTY_REFERENCE_RUN",
    "PROPERTY_SCHEDULER_STATUS",
    "PROPERTY_WALKING_TEST",
    "READERS",
    "SIG_SOFTWARE_VERSION",
    "THERMOSTAT_GATED",
    "VALUE_GATES",
    "ConfigEntity",
    "EdgeDetectionEntity",
    "EdgeDetectionTarget",
    "EntityTarget",
    "FlagEntity",
    "FlagTarget",
    "LedSyncTarget",
    "LoadLock",
    "LockFunctionEntity",
    "NightModeTarget",
    "Page",
    "PropertyEntity",
    "PropertyReader",
    "PropertyTarget",
    "SetupStateEntity",
    "SetupTarget",
    "ValueTarget",
    "_elements_for",
    "_node_page",
    "applied",
    "blind_targets",
    "cacheable",
    "cached_value",
    "check_outcome",
    "config_targets",
    "describe",
    "descriptions",
    "device_lock_targets",
    "edge_detection_targets",
    "gateway_ip_targets",
    "gateway_status_targets",
    "is_secret",
    "key_mode_targets",
    "led_sync_targets",
    "lock_mode",
    "lock_targets",
    "night_mode_targets",
    "node_version",
    "property_id_targets",
    "property_reader",
    "redacted",
    "retired_unique_ids",
    "setup_targets",
    "u16",
    "vendor_server",
]

LOCK_SPEC = P.PROPERTIES[PROPERTY_LOCK]
# a light's / socket's lock attributes (`LoadLock`)
ATTR_LOCKED, ATTR_LOCK_UNTIL = "locked", "lock_until"
# a load's room thermostats and a detector relay's continuous on / off (`LoadLock`)
ATTR_CONTROLLED_BY, ATTR_CONTINUOUS_ON_OFF = "controlled_by", "continuous_on_off"
PROPERTY_FORCED_OFF = 0x6016
FORCED_OFF_SPEC = P.PROPERTIES[PROPERTY_FORCED_OFF]
# How a detector's continuous on / off ends, as the app's banner says per product (`LampDetailActivity`,
# `detector_forced_off_*`): the ON / OFF button of the motion detector 0x0008, the programming button of the
# presence detector 0x0009, the slide switch's middle position on the others (`load_forced_short`); the translation
# key is `load_forced_<on|off>_<hint>`.
FORCED_HINTS: dict[int, str] = {0x08: "button", 0x09: "presence"}
# Cells the app offers only for some values of another property of the same element (`docs/gap-analysis/
# device-settings.md` §7.2, §9.2): property -> (the property it follows, the values it is offered for), every pair
# must hold. Such an entity is unavailable otherwise, as the app hides or disables the cell; while the other value is
# not known it is offered (`PropertyEntity.gated`), and the other value's own entity reads it. The blind's slat
# cells only in operation mode *blinds* (0x1104, `SlatCompatible`, `C1847c` / `C1854j`), its slat time hidden for a
# shutter (`C1855k`), the positions on power only for *stored position* (0x1105, `C1851g` / `C1854j`); a detector's
# switch-on brightness not in day mode (0x6015, `DayModeCompatible`). Unverified on air: no blind or detector here.
VALUE_GATES: dict[int, tuple[tuple[int, frozenset[object]], ...]] = {
    0x1103: ((0x1104, frozenset({"blinds", "awning"})),),
    0x1106: ((0x1105, frozenset({"move_to_stored_position"})),),
    0x1107: (
        (0x1105, frozenset({"move_to_stored_position"})),
        (0x1104, frozenset({"blinds"})),
    ),
    0x110B: ((0x1104, frozenset({"blinds"})),),
    0x600F: ((0x6015, frozenset({False})),),
}
# A load's run-on time, manual off, on / off delay, switch blocking time and prewarning: the app disables them while
# a room thermostat switches the load (`RtrConnectionHandler.b()`, `rtr_connection_parameter_info_text`; the load's
# `thermostats_of` entry, `jhmesh.devices.thermostat_links`). Unverified on air: no room thermostat here.
THERMOSTAT_GATED = frozenset({0x1001, 0x1002, 0x1007, 0x100A, 0x100B, 0x100D})

# ----------------------------------------------------------------------------- entity base


class ConfigEntity(JungHomeEntity):
    """A config entity of one element, read through the hub's reader (`_read`) until it answers, then again later.

    A value changed in the app is answered to the app's address, so Home Assistant hears nothing of it: once
    read, the entity is read again on a later link when CONFIG_REREAD_INTERVAL has passed, and
    `homeassistant.update_entity` reads it at once (`_reread`). A battery node's (`BATTERY_PIDS`) sleeps at link-up
    and would not answer: it is read when one of the node's keys reports an event instead (`_on_key_event`). A
    change to it runs under `changing` and a silent node is reported as asleep (`asleep`): the user wakes it with a
    key press and tries again.
    """

    _attr_entity_category: EntityCategory | None = EntityCategory.CONFIG

    def __init__(self, hub: JungHomeHub, target: EntityTarget) -> None:
        """Bind to `target`."""
        super().__init__(hub, target.address, target.unique_id, target.device_info)
        self.target = target
        self._attr_translation_key = target.translation_key
        if target.key:
            self._attr_translation_placeholders = {"key": target.key}
        self._attr_entity_registry_enabled_default = target.enabled_default
        self._attr_extra_state_attributes = {"mesh_address": f"{target.address:04X}"}
        self._read_done = not target.read
        self._read_at: float | None = (
            None  # when the last read that got every value ended (`time.monotonic()`)
        )
        self._read_pending = False
        self._read_link: int | None = (
            None  # `hub.link_count` of the link the read was last queued on
        )
        self._battery = target.node.pid in BATTERY_PIDS
        # a battery node's keys, whose events say it is awake (`_on_key_event`)
        self._keys = (
            tuple(b.address for b in hub.devices.buttons if b.node is target.node)
            if self._battery
            else ()
        )

    @property
    def listened(self) -> tuple[int, ...]:
        """A battery node's keys, whose events say it is awake; none for a mains node."""
        return self._keys

    @property
    def reader(self) -> PropertyReader:
        """The hub's property reader."""
        return property_reader(self.hass, self.hub)

    async def async_added_to_hass(self) -> None:
        """Subscribe to updates, then read the values once the link is up (a battery node's: its keys too)."""
        await super().async_added_to_hass()
        for address in self.listened:
            self.async_on_remove(
                self.hub.add_event_listener(address, self._on_key_event)
            )
        self._maybe_read()

    @callback
    def _handle_update(self) -> None:
        self._maybe_read()
        super()._handle_update()

    @callback
    def _maybe_read(self) -> None:
        """Queue the read when the link is up and it is due (`_read_due`) — once per link.

        A read that got no answer (or was cut by a lost link) is queued again on the next link, not on the next
        update of this one: the element was asked and stayed silent, asking again through the same link would only
        add to the traffic that may have drowned the first attempt. So is the re-read of values read
        CONFIG_REREAD_INTERVAL ago: at most once per link and per interval, behind the connect-time traffic like
        the first read. Never for a battery node (`_on_key_event`). A read still queued from a lost link is queued
        again regardless: the reader drops the lost link's copy (`PropertyReader.schedule`) and keeps one.
        """
        if not self.hub.connected or self._battery:
            return
        self.reader.schedule_version(self.target.node)
        if not self._read_due() or self._read_link == self.hub.link_count:
            return
        self._read_pending = True
        self._read_link = self.hub.link_count
        self.reader.schedule(self.address, self._initial_read)

    @callback
    def _on_key_event(self, event: str, attrs: dict[str, Any]) -> None:
        """Read now that a key of the battery node reported: it is awake for a moment, too short for the queue.

        A read that got no answer is tried again at the next key event, as the battery level is
        (`sensor.JungHomeBatterySensor`); one that got every value at the first key event after
        CONFIG_REREAD_INTERVAL (`_read_due`).
        """
        if not self.hub.connected or self._read_pending or not self._read_due():
            return
        self._read_pending = True
        self.hub.entry.async_create_background_task(
            self.hass,
            self._initial_read(),
            f"{DOMAIN} property read {self.address:04X}",
        )

    def _read_due(self) -> bool:
        """Whether to read: nothing read yet (or the last read went unanswered), or the last full read is old.

        An entity with nothing to read (`EntityTarget.read` off) is never due.
        """
        if not self._read_done:
            return True
        return (
            self._read_at is not None
            and time.monotonic() - self._read_at >= CONFIG_REREAD_INTERVAL
        )

    async def _initial_read(self) -> None:
        try:
            self._read_done = await self._read()
            if self._read_done:
                self._read_at = time.monotonic()
        finally:
            self._read_pending = False

    async def _read(self) -> bool:
        """Read what the entity shows; True when all of it is cached afterwards."""
        raise NotImplementedError

    def _update_read(self) -> UpdateRead | None:
        """`homeassistant.update_entity` reads the values now (`_reread`); an entity with nothing to read, nothing."""
        if not self.target.read:
            return None
        return self.target.unique_id, self._reread

    async def _reread(self) -> None:
        """Read what the entity shows now, for `homeassistant.update_entity`: by default the per-link read."""
        await self._read()

    @asynccontextmanager
    async def changing(self) -> AsyncIterator[None]:
        """Hold the element's `modifying` lock and keep a battery node awake, from reading the value to writing it."""
        async with (
            self.reader.modifying(self.address),
            self.hub.keep_awake.hold([self.address]),
        ):
            yield

    def asleep(self) -> HomeAssistantError:
        """Return the error for a battery node that did not answer a change: press one of its keys, then change again."""
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="setting_asleep",
            translation_placeholders={"entity": self.entity_id},
        )


class PropertyEntity(ConfigEntity):
    """A config entity bound to the properties of one element: values from the state cache, writes through the reader."""

    def __init__(self, hub: JungHomeHub, target: EntityTarget) -> None:
        """Bind to `target`."""
        super().__init__(hub, target)
        self.specs = target.specs
        self.spec = self.specs[0]
        self._attr_extra_state_attributes["property_id"] = ", ".join(
            f"0x{s.id:04X}" for s in self.specs
        )

    @property
    def gates(self) -> tuple[tuple[PropertySpec, frozenset[object]], ...]:
        """The other properties of the element the cell follows, with the values it is offered for (`VALUE_GATES`)."""
        return tuple(
            (P.PROPERTIES[pid], values)
            for pid, values in VALUE_GATES.get(self.spec.id, ())
        )

    @property
    def gated(self) -> bool:
        """Whether the app would not offer the cell now: a followed value outside its set, or a thermostat's load.

        A followed value not known yet gates nothing, nor does one the catalogue cannot name (an enumeration's
        raw integer): what cannot be read is not hidden. The entity does not ask for the value: its own entity
        does (the cover reads the operation mode at every link, the day-mode switch is on by default, the power-on
        select reads its value once enabled), and a second Get to an element that left the first unanswered only
        adds to the traffic.
        """
        if self.spec.id in THERMOSTAT_GATED and self.hub.devices.thermostats_of.get(
            self.address
        ):
            return True
        return any(
            (value := self.value_of(spec)) is not None
            and not (isinstance(spec.codec, P.Enum) and isinstance(value, int))
            and value not in values
            for spec, values in self.gates
        )

    @property
    def available(self) -> bool:
        """Available with the link, unless the app would not offer the cell now (`gated`)."""
        return super().available and not self.gated

    async def _read(self) -> bool:
        done = True
        for spec in self.specs:
            done = await self.reader.read(self.address, spec) and done
        return done

    async def _reread(self) -> None:
        """Ask for every property of the entity, however recently another entity read it (`read_current`'s `since`)."""
        since = time.monotonic()
        for spec in self.specs:
            await self.read_current(spec, since=since)

    async def read_current(
        self, spec: PropertySpec, *, since: float | None = None
    ) -> None:
        """Read `spec` before a change that keeps the rest of its value; a battery node that stays silent is asleep.

        A lost link fails the change as a send failure, a battery node's included: it is not the node's sleep.
        `since`: ask unless the element answered after that moment (`PropertyReader.read`), for
        `homeassistant.update_entity` — by default a value read within PROPERTY_READ_FRESH is taken as it is.
        """
        with mesh_errors():
            answered = await self.reader.fetch(self.address, spec, since=since)
        if not answered and self._battery:
            raise self.asleep()

    def value_of(self, spec: PropertySpec) -> Any:
        """Return the decoded cached value of `spec`, None when unknown or malformed."""
        raw = self.reader.cached(self.address, spec)
        if raw is None:
            return None
        try:
            return spec.codec.decode(raw)
        except ValueError:
            return None

    @property
    def property_value(self) -> Any:
        """The decoded value of the (first) property."""
        return self.value_of(self.spec)

    async def async_write_value(
        self,
        value: Any,
        spec: PropertySpec | None = None,
        *,
        compare: bool | None = None,
    ) -> None:
        """Write `value` (a decoded Python value) to `spec` (default: the entity's property).

        `compare`: whether a read-back showing another value is an error (default: when the entity reads its value;
        a trigger reads back what it is doing). An entity that is not read itself but writes readable properties
        passes True.
        """
        spec = self.spec if spec is None else spec
        by_status = (
            isinstance(self.target, PropertyTarget)
            and self.target.description.write == "status"
        )
        try:
            with mesh_errors():
                async with self.hub.keep_awake.hold([self.address]):
                    if by_status:
                        await self.reader.write_status(self.address, spec, value)
                        return
                    outcome = await self.reader.write(self.address, spec, value)
        except ValueError as err:  # the value does not fit the codec
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="value_rejected",
                translation_placeholders={"entity": self.entity_id, "error": str(err)},
            ) from err
        if outcome == "no_answer" and self._battery:
            raise self.asleep()
        # a trigger (a button, nothing to read) reads back what it is doing, not the value written
        check_outcome(
            outcome,
            self.entity_id,
            compare=self.target.read if compare is None else compare,
        )


class EdgeDetectionEntity(PropertyEntity):
    """One field of an input's edge evaluation; a change rewrites the byte with the other two fields as read."""

    target: EdgeDetectionTarget

    @property
    def edge_detection(self) -> P.EdgeDetection | None:
        """The decoded byte; None until the input reported it."""
        value = self.property_value
        return value if isinstance(value, P.EdgeDetection) else None

    async def async_write_part(self, **change: Any) -> None:
        """Write the byte with `change` applied; the current byte is read first when not known, never guessed."""
        async with self.changing():
            current = self.edge_detection
            if current is None:
                await self.read_current(self.spec)
                current = self.edge_detection
            if current is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="edge_detection_unknown",
                    translation_placeholders={"entity": self.entity_id},
                )
            await self.async_write_value(replace(current, **change))


class FlagEntity(PropertyEntity):
    """An entity over one flag of a bit-field property (`FlagTarget`): the device lock, the gateway's API status."""

    target: FlagTarget

    @property
    def word(self) -> int | None:
        """The whole bit field as the element reported it, bits without a name included; None until read (or short)."""
        value = self.property_value  # decodes only a complete word
        raw = self.reader.cached(self.address, self.spec)
        if value is None or raw is None:
            return None
        codec = self.spec.codec
        assert isinstance(codec, P.Flags)
        return int.from_bytes(raw[: codec.size], "little")

    @property
    def flag(self) -> bool | None:
        """The entity's flag; None until the element reported the word."""
        word = self.word
        return None if word is None else bool(word >> self.target.bit & 1)


LOCK_MODES = {  # (command, priority) -> the `lock_mode` attribute
    (P.ENFORCE_LOCK, P.PRIORITY_NORMAL): "keep_state",
    (P.ENFORCE_LOCK, P.PRIORITY_LOCKOUT): "lockout_protection",
}


def lock_mode(value: P.EnforcedOutput) -> str:
    """Name a lock the way the app tells them apart: kept state, lock-out protection, wind alarm, enforced value."""
    if value.wind_alarm:
        return "wind_alarm"
    return LOCK_MODES.get((value.command, value.priority), "enforced_value")


class LockFunctionEntity(PropertyEntity):
    """An entity over a load's lock function (0x0009): the *Lock* switch, a blind's *Lock function* and *Wind alarm*.

    It is read once per link like every config entity, and read back when a timed lock should have ended — the device
    ends it on its own and is not known to tell anyone. A lock set by another client reaches it without a read: the
    load publishes its lock (an LBC User Property Status of 0x0009) to its element group when locked and on every
    Set it refuses while locked (seen on air, `docs/hidden-features.md` §12), and every vendor Status is taken. Entities of the same load that want the read-back at the same moment share one Get
    (`PropertyReader.read`'s `since`).
    """

    _expiry: CALLBACK_TYPE | None = None

    @property
    def lock(self) -> P.EnforcedOutput | None:
        """The lock state; None until the element reported it."""
        value = self.property_value
        return value if isinstance(value, P.EnforcedOutput) else None

    async def _read(self) -> bool:
        """Read the lock, unless the load answered since the link came up: its light or socket reads it once per link.

        That read (`LoadLock._maybe_read_lock`) and this one share one Get, whichever comes first.
        """
        return await self.reader.read(
            self.address, self.spec, since=self.hub.link_since
        )

    async def async_will_remove_from_hass(self) -> None:
        """Drop a pending read-back."""
        self._cancel_expiry()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_update(self) -> None:
        """Follow a timed lock whoever set it (the app, a key, another controller): read it back once it should end.

        A lock reported with a time limit gets the same read-back as one this entity sent; a read-back that still
        finds it locked schedules the next one.
        """
        value = self.lock
        if value is not None and value.locked and value.time_s:
            if self._expiry is None:
                self._expiry = async_call_later(
                    self.hass, value.time_s + LOCK_EXPIRY_MARGIN, self._lock_expired
                )
        else:
            self._cancel_expiry()
        super()._handle_update()

    async def async_lock(self, value: P.EnforcedOutput) -> None:
        """Send a lock; one with a time limit is read back when it should have ended, counted from now."""
        self._cancel_expiry()
        await self.async_write_value(value)
        if value.time_s:  # with the time sent, whatever the Status reported
            self._cancel_expiry()
            self._expiry = async_call_later(
                self.hass, value.time_s + LOCK_EXPIRY_MARGIN, self._lock_expired
            )

    async def async_unlock(self) -> None:
        """Unlock: command 0 with the priority, time and value of the lock last read, as the app does.

        Without a lock to release (none read, or the load reported itself unlocked) it is the plain unlock
        `00 01 00 00`: an unlocked load reports priority 0, and a load refuses an unlock with priority 0 (on air, a
        locked light answered `00 00 00 00` with the property id alone and stayed locked).
        """
        self._cancel_expiry()
        current = self.lock
        if current is not None and current.locked:
            await self.async_write_value(replace(current, command=P.ENFORCE_UNLOCK))
        else:
            await self.async_write_value(P.UNLOCK)

    @callback
    def _cancel_expiry(self) -> None:
        if self._expiry is not None:
            self._expiry()
            self._expiry = None

    @callback
    def _lock_expired(self, _now: Any) -> None:
        """Read the lock back once its time limit has passed: the device unlocked itself, and says so to no one."""
        self._expiry = None
        self.hub.entry.async_create_background_task(
            self.hass,
            self.reader.read(self.address, self.spec, since=time.monotonic()),
            f"{DOMAIN} lock read-back {self.address:04X}",
        )


class LoadLock(JungHomeEntity):
    """Lock awareness of a light or socket: what the app does with a locked load's controls.

    A load locked in the app, by a key or by its *Lock* switch keeps its state against every command. The app reads
    0x0009 of every load when its device list opens; the load also publishes its lock to its element group when
    locked, and again on every Set it refuses (an LBC User Property Status, seen on air), which lands here like
    any vendor Status. The entity reads it once per link (`_maybe_read_lock`), through the property
    reader's queue, which runs after the connect-time state refresh, `PROPERTY_READ_CHUNK` loads at a time like
    it; the *Lock* switch and select share that Get (`PropertyReader.read`'s `since`, `LockFunctionEntity._read`).
    The answer lands in `ElementState.lock`; the entity shows it as the `locked` and `lock_until` attributes (None:
    not known yet) and refuses a command while the load is locked (`JungHomeHub.load_locked`) — the app disables
    the controls. The lock is read again first, as it may have ended unseen: lifted in the app (a Get unless the
    load answered within PROPERTY_READ_FRESH), or run out — a lock past its time limit is asked anew, and a load
    that stays silent then is taken as unlocked. A command the load did not confirm although the node answered
    something meanwhile, or an on / off it answered with the other state (a Status with the old state),
    makes the entity read the lock too, and report it as locked when it is: the command failed for the lock,
    not for the reachability. On air a locked switch insert and DALI insert answered an OnOff or Lightness Set with
    a Status of their unchanged state (`docs/hidden-features.md` §12); the refusal itself is unverified on air.

    A toggle of a load whose state is not known switches it on (Home Assistant's default), where the app takes an
    unknown state for on and sends off (`ui:uc:toggledevice`): kept deliberately, as switching a load on is what a
    user toggling a load that shows nothing expects. Room and central commands (*All lights*, a room's lights) go
    out unacknowledged to every member and are not refused: a locked member ignores them, as in the app.

    The app disables a load's controls in two more cases (`LampDetailActivity`: `RtrConnectionMode`,
    `ForcedOffMode`), and so does this (unverified on air: no room thermostat or detector here). A
    load a room thermostat switches (`Devices.thermostats_of`, the app's RTR link) shows the thermostats' names as
    `controlled_by` and refuses commands: the thermostat would switch it back. A detector's relay shows the
    detector's continuous on / off (0x6016, read once per link with the lock) as `continuous_on_off` and refuses
    commands while it is `on` or `off`, with the product's instruction to end it (the app's `ForcedOffView` texts).
    """

    # `hub.link_count` of the link the lock read was last queued on
    _lock_link: int | None = None

    @property
    def lockable(self) -> bool:
        """Whether the load's node has the lock function (`P.LOCKABLE`)."""
        node = self.hub.cdb.node_by_addr(self.address)
        return node is not None and (node.pid or 0) in LOCK_SPEC.products

    @property
    def thermostats(self) -> list[Thermostat]:
        """The room thermostats that switch the load; none for most loads (`Devices.thermostats_of`)."""
        return self.hub.devices.thermostats_of.get(self.address, [])

    @property
    def detector_element(self) -> int | None:
        """The detector element of the load's node when the load is a detector's relay, else None.

        The detector's properties live on its highest element (`docs/gap-analysis/control-and-state.md` §2.8).
        """
        node = self.hub.cdb.node_by_addr(self.address)
        if node is None or (node.pid or 0) not in FORCED_OFF_SPEC.products:
            return None
        return node.elements[-1].address

    @property
    def continuous_on_off(self) -> str | None:
        """The detector's continuous on / off of a relay (`inactive`, `on`, `off`); None until read or unnamed."""
        detector = self.detector_element
        if detector is None:
            return None
        value = cached_value(self.hub, detector, FORCED_OFF_SPEC)
        return value if value in P.FORCED_OFF.values() else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The entity's attributes, plus `locked` and, while a timed lock holds, `lock_until` (ISO 8601, UTC).

        A load a thermostat switches has `controlled_by` (the thermostats' names), a detector's relay
        `continuous_on_off`; other loads have neither.
        """
        attrs = dict(self._attr_extra_state_attributes)
        if thermostats := self.thermostats:
            attrs[ATTR_CONTROLLED_BY] = [t.name for t in thermostats]
        if self.detector_element is not None:
            attrs[ATTR_CONTINUOUS_ON_OFF] = self.continuous_on_off
        st = self.hub.states.get(self.address)
        if st is None or st.lock is None:
            attrs[ATTR_LOCKED] = attrs[ATTR_LOCK_UNTIL] = None
            return attrs
        attrs[ATTR_LOCKED] = st.locked
        attrs[ATTR_LOCK_UNTIL] = (
            st.lock_until.isoformat() if st.locked and st.lock_until else None
        )
        return attrs

    async def async_added_to_hass(self) -> None:
        """Subscribe (a detector relay to its detector element too), then queue the lock read once the link is up."""
        await super().async_added_to_hass()
        for address in self.listened:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    SIGNAL_UPDATE.format(self.hub.entry.entry_id, address),
                    self._handle_update,
                )
            )
        self._maybe_read_lock()

    @property
    def listened(self) -> tuple[int, ...]:
        """A detector relay's detector element, whose continuous on / off it shows; nothing for other loads."""
        detector = self.detector_element
        return () if detector is None else (detector,)

    @callback
    def _handle_update(self) -> None:
        self._maybe_read_lock()
        super()._handle_update()

    @callback
    def _maybe_read_lock(self) -> None:
        """Queue the read of the lock, and of a detector relay's continuous on / off, once per link.

        A load that stayed silent is asked on the next link.
        """
        detector = self.detector_element
        if (
            not (self.lockable or detector is not None)
            or not self.hub.connected
            or self._lock_link == self.hub.link_count
        ):
            return
        self._lock_link = self.hub.link_count
        reader = property_reader(self.hass, self.hub)
        if self.lockable:
            reader.schedule(self.address, self._read_lock, key=PROPERTY_LOCK)
        if detector is not None:
            reader.schedule(
                detector, partial(self._read_forced, detector), key=PROPERTY_FORCED_OFF
            )

    async def _read_lock(self) -> None:
        """Ask for the lock unless the load answered since the link came up (the *Lock* switch's read, say)."""
        await property_reader(self.hass, self.hub).read(
            self.address, LOCK_SPEC, since=self.hub.link_since
        )

    async def _read_forced(self, detector: int) -> None:
        """Ask for the detector's continuous on / off unless it answered since the link came up (its sensor's read)."""
        await property_reader(self.hass, self.hub).read(
            detector, FORCED_OFF_SPEC, since=self.hub.link_since
        )

    async def _check_unlocked(self) -> None:
        """Refuse a command while the load is locked, a thermostat switches it, or its detector holds it.

        The lock and the continuous on / off are read again first, they may have ended unseen.
        """
        if thermostats := self.thermostats:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="load_thermostat_controlled",
                translation_placeholders={
                    "entity": self.entity_id,
                    "thermostats": ", ".join(t.name for t in thermostats),
                },
            )
        await self._check_not_forced()
        st = self.hub.states.get(self.address)
        if not self.lockable or st is None or st.lock is None or not st.lock.locked:
            return
        reader = property_reader(self.hass, self.hub)
        # a lock past its time limit is asked anew; another one unless the load answered within PROPERTY_READ_FRESH
        await reader.read(
            self.address,
            LOCK_SPEC,
            since=None if st.locked else time.monotonic(),
        )
        if self.hub.load_locked(self.address):
            raise self._locked()

    async def _send(self, command: Awaitable[None]) -> None:
        """Run the command; one the node answered something to but did not confirm is reported as locked when it is."""
        asked = time.monotonic()
        try:
            await super()._send(command)
        except HomeAssistantError as err:
            if (
                err.translation_key != "device_not_reachable"
                or not self.lockable
                or not self._heard_since(asked)
            ):
                raise
            await self._raise_if_locked(asked, err)
            raise

    async def _send_switch(self, command: Awaitable[None], on: bool | None) -> None:
        """`_send` an on / off command; one the load answered with the other state is reported as locked if it is.

        With no other request out to the load, such a Status counts for the Set. `on` is the state
        asked for; None: nothing to compare — a Set with a transition is answered with where the load is now, not
        where it is going.
        """
        asked = time.monotonic()
        await self._send(command)
        st = self.hub.states.get(self.address)
        if on is not None and st is not None and st.on is not on and self.lockable:
            await self._raise_if_locked(asked)

    async def _raise_if_locked(
        self, asked: float, cause: Exception | None = None
    ) -> None:
        """Read the lock unless the load answered it since `asked`; raise the refusal when it is locked."""
        await property_reader(self.hass, self.hub).read(
            self.address, LOCK_SPEC, since=asked
        )
        if self.hub.load_locked(self.address):
            raise self._locked() from cause

    def _heard_since(self, moment: float) -> bool:
        """Whether the load's node was heard from at or after `moment` (a `time.monotonic()`)."""
        node = self.hub.cdb.node_by_addr(self.address)
        return (
            node is not None and self.hub.last_heard.get(node.unicast, -1e9) >= moment
        )

    def _locked(self) -> ServiceValidationError:
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="load_locked",
            translation_placeholders={"entity": self.entity_id},
        )

    async def _check_not_forced(self) -> None:
        """Refuse a command while the relay's detector holds it on or off; read the state again first.

        The app's banner names how to end it per product (`LampDetailActivity`, `FORCED_HINTS`).
        """
        if self.continuous_on_off not in ("on", "off"):
            return
        detector = self.detector_element
        # a continuous on / off is only known for a detector's relay
        assert detector is not None
        await property_reader(self.hass, self.hub).read(detector, FORCED_OFF_SPEC)
        state = self.continuous_on_off
        if state not in ("on", "off"):
            return
        node = self.hub.cdb.node_by_addr(self.address)
        assert node is not None  # as `detector_element`
        hint = FORCED_HINTS.get(node.pid or 0)
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="load_forced_short"
            if hint is None
            else f"load_forced_{state}_{hint}",
            translation_placeholders={"entity": self.entity_id},
        )


def u16(raw: bytes, offset: int = 0, *, signed: bool = False) -> int:
    """Return the little-endian 16-bit field of a Status at `offset`."""
    return int.from_bytes(raw[offset : offset + 2], "little", signed=signed)


class SetupStateEntity(ConfigEntity):
    """A config entity over a SIG setup state: its value is the cached Status, a change is the state's Set."""

    target: SetupTarget

    async def _read(self) -> bool:
        return await self.reader.read_setup(self.address, self.target.state)

    async def _reread(self) -> None:
        """Ask for the state now, however recently another entity read it (`homeassistant.update_entity`)."""
        await self.reader.read_setup(
            self.address, self.target.state, since=time.monotonic()
        )

    @property
    def setup_value(self) -> bytes | None:
        """The cached Status parameters; None until the element reported them."""
        return self.reader.cached_setup(self.address, self.target.state)

    async def async_current(self) -> bytes:
        """Return the state for a Set that keeps its other fields: read first when not known, never guessed."""
        raw = self.setup_value
        if raw is None:
            await self.reader.read_setup(self.address, self.target.state)
            raw = self.setup_value
        if raw is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="setup_state_unknown",
                translation_placeholders={"entity": self.entity_id},
            )
        return raw

    async def async_write_setup(self, build: Callable[..., bytes], *args: int) -> None:
        """Send the Set `build(*args)` makes; a value the builder refuses is the user's error, not a send failure."""
        try:
            pdu = build(*args)
        except ValueError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="value_rejected",
                translation_placeholders={"entity": self.entity_id, "error": str(err)},
            ) from err
        with mesh_errors():
            outcome = await self.reader.write_setup(
                self.address, self.target.state, pdu
            )
        check_outcome(outcome, self.entity_id)
