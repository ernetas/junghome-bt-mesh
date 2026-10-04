"""The last known state of one mesh element, as the hub caches it (`JungHomeHub.states`, `element_state`).

It lived in `coordinator.py`, which re-exports it; here a module the hub is made of can name it without importing
the hub.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

from .jhmesh.properties import PROPERTIES, EnforcedOutput

PROPERTY_LOCK = 0x0009  # EnforceOutput: a load's lock (`ElementState.note_lock`)


@dataclass
class ElementState:
    """Last known state of one mesh element (a load, a socket, a level or battery element, a property owner).

    `on` / `lightness` / `kelvin` are the *present* values of the last status (what the element is doing now, what
    the app shows); `target_*` is where a transition is heading, equal to the present value when the element is idle
    or the status came in its short form. Nothing renders the targets yet: they are kept because they are free.
    """

    on: bool | None = None
    lightness: int | None = None  # 0..65535
    kelvin: int | None = None
    target_on: bool | None = None
    target_lightness: int | None = None
    target_kelvin: int | None = None
    # when the element will be off by its last Generic OnOff Status: on, heading off, with a known remaining time
    # (a run-on time running out, or a fade to off); None otherwise (`_on_onoff_status`)
    off_at: datetime | None = None
    # a CTL light's own temperature range (Light CTL Temperature Range Status); None until read
    kelvin_min: int | None = None
    kelvin_max: int | None = None
    level: int | None = None  # Generic Level, -32768..32767 (blinds position / slat)
    target_level: int | None = None  # a moving blind's destination; level when idle
    battery: int | None = None  # Generic Battery level, percent
    power_w: float | None = None
    voltage_v: float | None = None
    current_a: float | None = None
    # the polled SIG counters of a metering socket (COUNTER_READS); None until read or while the socket says "unknown"
    power_on_hours: int | None = None
    energy_wh: int | None = (
        None  # 0x0072 precise total energy: lifetime, nothing resets it
    )
    energy_resettable_wh: int | None = (
        None  # 0x006A total energy: what the app shows and its "reset" zeroes
    )
    energy_since_on_wh: int | None = (
        None  # 0x000D energy since the socket was switched on
    )
    # the load's meter does not serve 0x0072, so its *Energy* is 0x006A (`energy_total`); never set on a socket
    energy_fallback: bool = False
    properties: dict[int, bytes] = field(
        default_factory=dict
    )  # raw property values by property id (SIG or JUNG)
    # raw SIG setup-server states by their Status opcode (OnPowerUp, Lightness Range / Default, CTL Default)
    setup: dict[int, bytes] = field(default_factory=dict)
    # a node's registered Health faults (its primary element; JUNG's vendor codes 0x81 / 0x80); None until read
    faults: tuple[int, ...] | None = None
    # the element's current scene (Scene Status / Scene Register Status; 0 = none): what its Scene Server says it
    # last recalled and still shows; None until the element said
    scene: int | None = None
    # a load's lock function (0x0009, `note_lock`): None until it reported one; and when a timed lock should end
    lock: EnforcedOutput | None = None
    lock_until: datetime | None = None
    updated: float = field(default_factory=time.monotonic)

    def note_lock(self, raw: bytes) -> None:
        """Take a reported lock function (0x0009); its time limit counts from now, as the *Lock* switch counts it.

        Whether a Status carries the time left or the time the lock was set for is not known (a read-back of a
        timed lock that still holds pushes `lock_until` on by the whole limit again). A malformed value is ignored.
        """
        try:
            value: EnforcedOutput = PROPERTIES[PROPERTY_LOCK].codec.decode(raw)
        except ValueError:
            return
        self.lock = value
        self.lock_until = (
            dt_util.utcnow() + timedelta(seconds=value.time_s)
            if value.locked and value.time_s
            else None
        )

    @property
    def locked(self) -> bool:
        """Whether the load reported a lock that has not run out yet (its time limit, when it had one)."""
        if self.lock is None or not self.lock.locked:
            return False
        return self.lock_until is None or dt_util.utcnow() < self.lock_until

    @property
    def energy_total(self) -> int | None:
        """The *Energy* sensor's counter: the lifetime total 0x0072, or 0x006A where the meter does not serve it.

        Only a load other than a metering socket falls back (`Energy._get_counters`, `_on_sig_property_status`),
        and only on a definite sign, never because 0x0072 went unanswered: 0x006A is at most 0x0072, so switching to
        it after a silence and back once 0x0072 answers would put the whole difference into one hour of the Energy
        dashboard. 0x006A is resettable; the app's "reset consumption" then shows as a meter reset, which
        `total_increasing` statistics handle.
        """
        return self.energy_resettable_wh if self.energy_fallback else self.energy_wh
