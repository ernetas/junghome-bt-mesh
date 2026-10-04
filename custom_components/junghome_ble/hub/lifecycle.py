"""The timers and tasks one hub runs, in one registry that stops them all, and the back-off its retries use.

Every timer handle and background task the hub cancels when it stops lives here under a fixed name (`TIMERS`,
`KEYED`, `TASKS`), wherever the component that arms it is: `JungHomeHub.async_stop` cancels them through the
registry (`cancel_timers`, `cancel_keyed`, `async_cancel_tasks`), in the order of those tuples, which is the order the
hub always stopped them in. Work the stop leaves alone (the export fetch and the heartbeat re-probe in flight, the
retry of a failed upload) is not registered.

`Backoff` is the pause before the next try that grows with every failure in a row: the connection loop's, doubling up
to a cap (`LinkManager.connection_loop`), and the gateway export re-fetch's, stepping through a schedule
(`ExportWatch`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import CALLBACK_TYPE

_LOGGER = logging.getLogger(__name__)

# the timers, in the order the hub cancels them when it stops (`Lifecycle.cancel_timers`)
TIMERS = (
    "adv",  # the proxy advertisements (`LinkManager.adv_seen`)
    "time",  # the daily Time Set (`Clock.send_time_daily`)
    "energy",  # the energy poll (`Energy.arm_poll`)
    "heartbeats",  # the heartbeat check (`Liveness.check_heartbeats`)
    "seq_check",  # the sequence-space check (`Issues.check_sequence_space`)
    "grace",  # the end of the link-loss grace (`LinkManager._start_grace`)
    "ha_stop",  # Home Assistant's stop (`JungHomeHub.async_start`)
    "echo",  # the check that a command was answered (`JungHomeHub._command`)
    "offset_change",  # the Time Set after a change of the UTC offset (`Clock.arm_offset_change`)
    "seq_stall",  # the look at a stalled store (`Issues.seq_stall_started`)
    "export_refresh",  # the next fetch of the gateway's export (`ExportWatch._schedule_export_refresh`)
    "filter_watch",  # the Filter Status watchdog (`LinkManager._connect_to`)
)
# the timers kept per node or element, in the order the hub cancels them (`Lifecycle.cancel_keyed`)
KEYED = (
    "recheck",  # node unicast → its pending re-ask (`Liveness._schedule_recheck`)
    "transition_reread",  # load element → the read after a transition (`JungHomeHub._reread_after_transition`)
    "locating",  # node unicast → the end of its Node Identity advert (`JungHomeHub.async_locate`)
)
# the background tasks, in the order the hub cancels them (`Lifecycle.async_cancel_tasks`)
TASKS = (
    "link",  # the connection loop (`LinkManager.connection_loop`)
    "refresh",  # the connect-time sequence of the link (`Refresh.after_connect`)
    "energy",  # an energy poll (`Energy._poll_energy_periodic`)
    "heartbeats",  # a renewal of the heartbeat publications (`Liveness.check_heartbeats`)
)


async def async_cancel_task(task: asyncio.Task[None] | None) -> None:
    """Cancel `task` and wait for it, without raising an error it had already failed with.

    `cancel()` is a no-op on a task that is already done; a task that finished with an exception before we
    got here then re-raises it from `await task`, which is not an error this cancel caused and was never
    ours to raise — `async_stop` used to abort right there, skipping the disconnect and the counter close
    that must always run. A cancellation of the caller itself (this coroutine's own task being torn down,
    not merely the cancellation we just issued to `task`) still propagates.
    """
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
    except (
        Exception
    ):  # a task that had already failed: its error was not ours to raise here
        _LOGGER.debug("background task %s had failed", task.get_name(), exc_info=True)


class Lifecycle:
    """The named timers and tasks of one hub (module docstring); none registered yet."""

    def __init__(self) -> None:
        """Start with every name free."""
        self._timers: dict[str, CALLBACK_TYPE | None] = dict.fromkeys(TIMERS)
        self._keyed: dict[str, dict[int, CALLBACK_TYPE]] = {name: {} for name in KEYED}
        self._tasks: dict[str, asyncio.Task[None] | None] = dict.fromkeys(TASKS)

    def timer(self, name: str) -> CALLBACK_TYPE | None:
        """Return the cancel of the timer registered as `name`; None when none is."""
        return self._timers[name]

    def set_timer(self, name: str, unsub: CALLBACK_TYPE | None) -> None:
        """Register `unsub` as the timer `name`; None forgets it (it fired, or was cancelled by its owner)."""
        self._timers[name] = unsub

    def cancel_timer(self, name: str) -> None:
        """Cancel the timer `name`, if one is registered."""
        if (unsub := self._timers[name]) is not None:
            unsub()
            self._timers[name] = None

    def keyed(self, name: str) -> dict[int, CALLBACK_TYPE]:
        """Return the timers `name` by node or element: the owner adds and removes them there."""
        return self._keyed[name]

    def task(self, name: str) -> asyncio.Task[None] | None:
        """Return the task registered as `name`; None when none is."""
        return self._tasks[name]

    def set_task(self, name: str, task: asyncio.Task[None] | None) -> None:
        """Register `task` as `name`; None forgets it."""
        self._tasks[name] = task

    async def async_cancel_task(self, name: str) -> None:
        """Cancel the task `name` and wait for it (`async_cancel_task`), then forget it."""
        await async_cancel_task(self._tasks[name])
        self._tasks[name] = None

    def cancel_timers(self) -> None:
        """Cancel every timer, in `TIMERS` order."""
        for name in TIMERS:
            self.cancel_timer(name)

    def cancel_keyed(self) -> None:
        """Cancel every timer kept per node or element, in `KEYED` order, then forget them."""
        for cancel in [c for name in KEYED for c in self._keyed[name].values()]:
            cancel()
        for timers in self._keyed.values():
            timers.clear()

    async def async_cancel_tasks(self) -> None:
        """Cancel every task and wait for it, in `TASKS` order; one that raises stops the rest (`async_cancel_task`)."""
        for name in TASKS:
            await self.async_cancel_task(name)

    def pending(self) -> list[str]:
        """Return the names of what is registered now, timers, then keyed timers, then tasks (each in stop order)."""
        return [
            *(name for name in TIMERS if self._timers[name] is not None),
            *(name for name in KEYED if self._keyed[name]),
            *(name for name in TASKS if self._tasks[name] is not None),
        ]


class Backoff:
    """The pause before the next try, growing with every failure in a row and starting over after a success.

    Either doubling from `first` up to `cap` (the connection loop's pause), or stepping through `schedule`, whose
    last delay repeats (the gateway export's re-fetch).
    """

    def __init__(
        self, first: float = 0.0, cap: float = 0.0, schedule: Sequence[float] = ()
    ) -> None:
        """Start at the first delay: `first` for a doubling back-off, else the schedule's first."""
        self._first, self._cap, self._schedule = first, cap, tuple(schedule)
        self.failures = 0  # in a row, since the last `reset`
        self.delay = self._schedule[0] if self._schedule else first  # the next pause

    def grow(self) -> float:
        """Return the pause for this failure and move on to the next one."""
        pause = self.delay
        self.failures += 1
        if self._schedule:
            self.delay = self._schedule[min(self.failures, len(self._schedule) - 1)]
        else:
            self.delay = min(pause * 2, self._cap)
        return pause

    def reset(self) -> None:
        """Start over at the first delay: the last try succeeded."""
        self.failures = 0
        self.delay = self._schedule[0] if self._schedule else self._first
