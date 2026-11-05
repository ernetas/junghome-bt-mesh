"""Which way a blind's position element is running, from its Generic Level Statuses and the Move Sets that start it.

The direction cannot be read off a status's target vs present alone. Seen on air from a shutter insert (fw 2.2.0.2):
a Level Set's statuses carry the destination and its real remaining time (`32x1s`, `3x100ms`), but a Move's (open /
close, from the wall button or from us) carry the level the move *started* at, always with `1x100ms` left, so the
blind moves away from that target. And every move starts and ends with a status one step off its target
(`present=-28655 target=-28656`) that says nothing — with present one *above* target whichever way the move goes,
so that offset cannot tell an open from a close either.

So, from statuses (`BlindMotion.on_status`): at rest for the short form, no time left, or present within one step of
target; else, for a status with more than one 100 ms step left, the way to its target, which it names (present
alone can lag: a blind reversing, statuses missed); else the way present moved since the last status, if that one
is recent enough to belong to the same move; else unchanged (the same status arrives on the group and as a reply).

A Move's first status showing motion comes ~3 s in, so the Move or Delta Set's own sign (`BlindMotion.on_command`)
stands in until then: taken once the blind confirms a Move — its first status, one step off the level it starts at,
`1x100ms` left — and dropped when the blind rests away from that level (a short reply at the start level is no
stop) or at the end it was running to. Whatever a lost status leaves behind is ended by the hub's silence watchdog
(`JungHomeHub._watch_motion`, `BlindMotion.rest`).

Only a blind's position element has one (`JungHomeHub._motion`): slats and lights take the same Level / Move Sets
but have no direction to show.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .conversions import level_to_closedness

# A blind's Move Set counts only if the blind confirms a Move within this long: ~100 ms on air, the rest is the way
# through the proxy. A sign nothing confirmed must not reach a later, unrelated move. A Set of the same sign heard
# within this long of a confirmation is taken for a retransmission of the one confirmed
MOVE_CONFIRM_WINDOW = 5.0
# A moving blind publishes about every 3 s on air: this long without a status, it is shown at rest and read again
# (`JungHomeHub._motion_silent`), so that a lost status cannot leave it moving. Also how old the last status may be
# for present's change since it to tell a direction: an older one may predate a move or reversal never heard
MOTION_SILENCE = 10.0
# After a stop, statuses the blind sent before it may still be on their way: for this long none of them shows a
# direction from where present went (a rest status still ends one, a new command ends the wait)
STOP_SETTLE = 2.0

LEVEL_OPEN, LEVEL_CLOSED = -32768, 32767


@dataclass
class BlindMotion:
    """One blind position element's direction: -1 toward -32768 (opening), +1 toward 32767 (closing), 0 at rest."""

    from_statuses: int = 0  # the direction its statuses tell
    # a Move or Delta Set's sign once sent or heard (`pending`, until `pending_until`), taken (`sign`) when the blind
    # confirms a Move (at `confirmed_at`), from the level it started at (`start`)
    pending: int = 0
    pending_until: float = -math.inf
    sign: int = 0
    confirmed_at: float = -math.inf
    start: int = 0
    # the present level of the last status and when it came (`time.monotonic()`)
    last_level: int | None = None
    last_heard: float = -math.inf
    # until when a stop keeps statuses from showing a direction (`STOP_SETTLE`)
    settle_until: float = -math.inf

    @property
    def direction(self) -> int:
        """The direction to show: its statuses', else its confirmed Move Set's."""
        return self.from_statuses or self.sign

    def on_status(
        self,
        present: int,
        target: int,
        remaining: float | None,
        short: bool,
        now: float,
    ) -> None:
        """Take a Generic Level Status: `remaining` None for the short form or "unknown" (0x3F: still moving)."""
        previous, heard = self.last_level, self.last_heard
        self.last_level, self.last_heard = present, now
        at_rest = short or remaining == 0
        near = abs(target - present) <= 1
        if near and not at_rest and remaining is not None and remaining <= 0.1:
            if self.pending and now < self.pending_until:
                self.sign, self.start, self.confirmed_at = self.pending, target, now
            self.pending = 0
        elif (
            at_rest
            and self.sign
            and (
                abs(present - self.start) > 1
                or present == (LEVEL_CLOSED if self.sign > 0 else LEVEL_OPEN)
            )
        ):
            self.sign = 0
        if at_rest or near:
            self.from_statuses = 0
        elif now < self.settle_until:
            pass  # sent before the stop: says nothing of now
        elif remaining is not None and remaining > 0.1:
            self.from_statuses = 1 if target > present else -1
        elif (
            previous is not None
            and now - heard <= MOTION_SILENCE
            and abs(present - previous) > 1
        ):
            self.from_statuses = 1 if present > previous else -1

    def on_command(self, delta: int | None, level: int | None, now: float) -> None:
        """Take a command, ours or heard: a Move or Delta Set's `delta`, 0 a stop, None a Level Set.

        A sign waits for the blind to confirm, for MOVE_CONFIRM_WINDOW; not for a blind already at the end it points
        to (it has nowhere to run), nor for a retransmission of a Set just confirmed. A stop ends the direction at
        once: the blind's status after a stop may be one it had sent before (`STOP_SETTLE`). A Level Set ends only
        what a Move Set stood in for: its own statuses name its destination.
        """
        if delta:
            sign = 1 if delta > 0 else -1
            self.settle_until = -math.inf
            at_end = level is not None and level_to_closedness(level) == (
                100 if sign > 0 else 0
            )
            retransmitted = (
                sign == self.sign and now - self.confirmed_at < MOVE_CONFIRM_WINDOW
            )
            self.pending = 0 if at_end or retransmitted else sign
            self.pending_until = now + MOVE_CONFIRM_WINDOW
            return
        self.pending = self.sign = 0
        if delta == 0:
            self.from_statuses = 0
            self.settle_until = now + STOP_SETTLE
        else:
            self.settle_until = -math.inf

    def on_send_failed(self, delta: int) -> None:
        """Forget the sign of our Move or Delta Set that never went out, unless something confirmed it meanwhile."""
        if delta and self.pending == (1 if delta > 0 else -1):
            self.pending = 0

    def rest(self) -> None:
        """Show it at rest: nothing heard from it for MOTION_SILENCE."""
        self.from_statuses = self.sign = 0
