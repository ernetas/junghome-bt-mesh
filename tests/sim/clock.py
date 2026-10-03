"""A virtual clock for the simulated mesh: an event loop whose time jumps to the next timer instead of sleeping.

`run(main)` runs a coroutine on a `VirtualTimeLoop`: `asyncio.sleep`, `wait_for`, `call_later` and every timeout of
the client work as usual, but nothing waits in real time — when no callback is ready the loop advances its clock
straight to the next scheduled one. A simulated hour of beacons and retransmissions takes as long as the Python it
runs. `patch_monotonic` makes `time.monotonic()` inside the library read the same clock (the reassembly expiry,
`last_rx`, message timestamps).

An executor job (`run_in_executor`: Home Assistant's file reads, imports, its storage) runs on its thread while the
loop waits for it, and its result is ready when the call returns: the clock stands still meanwhile (it would
otherwise jump to the next timer and fire a timeout the job's result would have beaten), and where the result lands
among the loop's callbacks is the same on every run. A job that is not through within `EXECUTOR_BLOCK` (one that
needs the loop itself to finish) is waited for the ordinary way, the clock still standing.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import selectors
import time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

# how long a loop with nothing scheduled waits (in real time) for another thread before calling it a deadlock
_REAL_IDLE_LIMIT = 0.2
_EXECUTOR_POLL = 0.05  # real seconds between looks while an executor job runs (its result wakes the loop sooner)
EXECUTOR_BLOCK = 5.0  # real seconds `run_in_executor` waits for a job in place before it lets the loop run on
DEFAULT_LIMIT = (
    7 * 24 * 3600.0
)  # a week of virtual time: far beyond any test, short of hanging on a beacon timer


class VirtualDeadlock(RuntimeError):
    """The simulation waits for something that nothing will ever do: no timer, no ready callback, no thread."""


class VirtualTimeout(RuntimeError):
    """The virtual clock passed the loop's limit: a coroutine awaits something that never happens."""


class _VirtualSelector(selectors.DefaultSelector):  # type: ignore[misc,valid-type]
    """The loop's selector: polls without blocking and advances the virtual clock by the timeout instead."""

    loop: VirtualTimeLoop

    def select(self, timeout: float | None = None) -> list[Any]:
        ready = super().select(0)
        if ready:
            return ready  # type: ignore[no-any-return]
        if self.loop.executor_jobs:
            # a thread is at work: its result is the next thing to happen, not the next timer
            return super().select(_EXECUTOR_POLL)  # type: ignore[no-any-return]
        if timeout is None:
            # nothing scheduled: only another thread (an executor job) could still wake the loop
            ready = super().select(_REAL_IDLE_LIMIT)
            if not ready:
                raise VirtualDeadlock(
                    f"nothing scheduled at virtual time {self.loop.time():.3f}s"
                )
            return ready  # type: ignore[no-any-return]
        if timeout > 0:
            self.loop.advance(timeout)
        return []


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """An event loop on a virtual clock that starts at 0 and only moves when the loop would otherwise wait."""

    def __init__(self, limit: float = DEFAULT_LIMIT) -> None:
        self._virtual_now = 0.0
        self.limit = limit
        self.executor_jobs: set[asyncio.Future[Any]] = set()
        selector = _VirtualSelector()
        selector.loop = self
        super().__init__(selector)

    def run_in_executor(
        self, executor: Any, func: Any, *args: Any
    ) -> asyncio.Future[Any]:
        self._check_closed()
        if executor is None:
            executor = self._default_executor
            if executor is None:
                executor = self._default_executor = (
                    concurrent.futures.ThreadPoolExecutor(thread_name_prefix="asyncio")
                )
        running = executor.submit(func, *args)
        concurrent.futures.wait([running], timeout=EXECUTOR_BLOCK)
        if not running.done():
            job = asyncio.wrap_future(running, loop=self)
            self.executor_jobs.add(job)
            job.add_done_callback(self.executor_jobs.discard)
            return job
        job = self.create_future()
        if (error := running.exception()) is not None:
            job.set_exception(error)
        else:
            job.set_result(running.result())
        return job

    def _virtual_time(self) -> float:
        return self._virtual_now

    @property
    def time(self) -> Callable[[], float]:  # type: ignore[override]
        """The virtual clock, `loop.time()` as on any loop.

        A property so that it stays: Home Assistant binds `time.monotonic` as the `time` of the loop it runs on
        (`homeassistant.runner.configure_event_loop`, which its test fixtures call for every test's loop).
        """
        return self._virtual_time

    @time.setter
    def time(self, _clock: Callable[[], float]) -> None:
        pass  # the virtual clock is the point of this loop

    def advance(self, seconds: float) -> None:
        self._virtual_now += seconds
        if self._virtual_now > self.limit:
            raise VirtualTimeout(
                f"virtual time passed {self.limit:g}s: something waits forever"
            )


def run[T](main: Coroutine[Any, Any, T], *, limit: float = DEFAULT_LIMIT) -> T:
    """Run `main` to completion on a fresh `VirtualTimeLoop` and close the loop."""
    with asyncio.Runner(loop_factory=lambda: VirtualTimeLoop(limit)) as runner:
        return runner.run(main)


class VirtualClocks:
    """Stand-in for the `time` module: `monotonic()` reads a running `VirtualTimeLoop`, everything else is real.

    `time()` (the wall clock) reads the virtual clock too, plus `wall_offset`: a test moves the wall clock days ahead
    without running days of beacons (the IV Update's 96 hours a step, `LocalState.apply_beacon`).
    """

    def __init__(self) -> None:
        self.wall_offset = 0.0

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)

    @staticmethod
    def monotonic() -> float:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return time.monotonic()
        return loop.time() if isinstance(loop, VirtualTimeLoop) else time.monotonic()

    def time(self) -> float:
        return self.monotonic() + self.wall_offset


def patch_monotonic(monkeypatch: pytest.MonkeyPatch, *modules: Any) -> VirtualClocks:
    """Make `time.monotonic()` and `time.time()` in each module (their `time` global) read the virtual clock while
    one runs; returns the stand-in, whose `wall_offset` moves the wall clock."""
    clocks = VirtualClocks()
    for module in modules:
        monkeypatch.setattr(module, "time", clocks)
    return clocks
