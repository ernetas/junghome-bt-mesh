"""A Hypothesis state machine over `LocalState`: no (transmit IV index, sequence number) is ever handed out twice.

The nonce of every PDU we send is (SRC, SEQ, IV index); reusing one breaks AES-CCM and every node's replay list
drops us. The machine drives the file-backed state through what can happen to it — single and segmented sends,
every kind of beacon (IV Update start / completion, a lagging node, recovery, far-ahead and stale indexes),
clean restarts and crashes, writes of the state file or of its `.bak` copy that fail, and the state file lost or
torn so that a restart falls back to the copy — and checks, after every step, that nothing it was handed was
handed before, under the transmit index it was sent with.

What the design does *not* claim to survive is left out: both copies lost at once (the counter restarts at 0 with
a WARNING, documented) and a second process on the same file (`StateInUse`, tested on its own).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from jhmesh.client import (
    IV_INDEX_MAX,
    IV_UPDATE_MIN_STATE,
    SEQ_GUARD_FIRST_BEACON,
    SEQ_TX_LIMIT,
    LocalState,
    SequenceExhausted,
)

from .conftest import OUR_SRC

SKIP_UNKNOWN = (
    1 << 22
)  # how far the integration's `seq_store_lost` repair skips when no number is left
# the spec's minimum time between two steps of an IV Update
HOURS_96 = IV_UPDATE_MIN_STATE
MARGIN = (
    512  # LocalState's default restart margin, what the CLI and the standalone link use
)


class FlakyState(LocalState):
    """`LocalState` whose writes of the state file or of its `.bak` copy can be made to fail (a full disk, EIO)."""

    def __init__(self, fail: set[str], *args: Any, **kwargs: Any) -> None:
        self.fail = fail  # "primary" / "backup": which writes raise (shared with the machine, changed live)
        super().__init__(*args, **kwargs)

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        kind = "backup" if path.suffix == ".bak" else "primary"
        if kind in self.fail:
            raise OSError(28, f"No space left on device (injected, {kind})")
        super()._write(path, data)


def record(path: Path) -> dict[str, Any] | None:
    """The record a restart could resume from `path`; None when it is missing, torn or unusable."""
    try:
        data = json.loads(path.read_text())
        LocalState.parse_record(data)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return data


def readable(path: Path) -> bool:
    """Whether a restart could resume from `path`."""
    return record(path) is not None


class LocalStateMachine(RuleBasedStateMachine):
    """One client's sequence state on disk, across restarts, crashes, failing writes and a lost state file."""

    def __init__(self) -> None:
        super().__init__()
        self.dir = Path(tempfile.mkdtemp(prefix="jhmesh-seq-"))
        self.path = self.dir / "state.json"
        self.fail: set[str] = set()
        self.state: FlakyState | None = None
        # (tx IV index, seq) -> step that sent it
        self.used: dict[tuple[int, int], int] = {}
        self.step = 0
        self.network_iv = 0  # the highest IV index a beacon announced: the network's
        self.now = 0.0  # the wall clock the beacons are applied at (`time_passes`)

    # ------------------------------------------------------------------ set-up / tear-down
    @initialize(
        start=st.sampled_from((None, 0, 1000, SEQ_TX_LIMIT - 700, SEQ_TX_LIMIT - 5)),
        iv_index=st.sampled_from((0, 1, 5, 0xFFFFFFF0)),
        update=st.booleans(),
    )
    def start(self, start: int | None, iv_index: int, update: bool) -> None:
        """A fresh state, or one a previous run left (possibly right at the end of the sequence space)."""
        self.network_iv = iv_index if start is not None else 0
        if start is not None:
            self.path.write_text(
                json.dumps(
                    {
                        "src": f"{OUR_SRC:04X}",
                        "seq": start,
                        "iv_index": iv_index,
                        "iv_update_active": update,
                    }
                )
            )
        self._open()

    def teardown(self) -> None:
        if self.state is not None:
            self._release(clean=False)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _open(self) -> None:
        self.state = None
        state = FlakyState.__new__(FlakyState)
        try:
            state.__init__(self.fail, self.path, OUR_SRC, restart_margin=MARGIN)  # type: ignore[misc]
        except OSError:
            # the disk refuses the very first write: nothing starts, nothing is sent, and the process exits —
            # which drops the lock the half-built state took (in-process it would not: see
            # test_a_failed_start_keeps_the_state_file_locked)
            if getattr(state, "_lock_file", None) is not None:
                state._lock_file.close()
            return
        self.state = state

    def _release(self, *, clean: bool) -> None:
        assert self.state is not None
        if clean:
            try:
                self.state.close()
            except OSError:
                pass  # the final replay-list flush failed; the lock goes with the process all the same
        # a crash: the kernel drops the flock with the process
        if self.state._lock_file is not None:
            self.state._lock_file.close()
            self.state._lock_file = None
        self.state = None

    # ------------------------------------------------------------------ what the client does
    @precondition(lambda self: self.state is not None)
    @rule(count=st.integers(1, 32))
    def send(self, count: int) -> None:
        """An unsegmented message (1) or the first round of a segmented one (up to 32 numbers at once)."""
        assert self.state is not None
        tx = self.state.tx_iv_index
        try:
            first = self.state.reserve_seq(count)
        except (SequenceExhausted, OSError):
            return  # nothing sent
        assert self.state.tx_iv_index == tx
        self.step += 1
        for seq in range(first, first + count):
            assert 0 <= seq <= SEQ_TX_LIMIT
            assert (tx, seq) not in self.used, (
                f"nonce reuse: IV {tx} seq {seq:06X} (first sent at step {self.used[(tx, seq)]})"
            )
            self.used[(tx, seq)] = self.step

    @precondition(lambda self: self.state is not None)
    @rule(delta=st.sampled_from((-1, 0, 1, 2, 3, 42, 43)), update=st.booleans())
    def beacon(self, delta: int, update: bool) -> None:
        """An authenticated Secure Network Beacon: the next index (IV Update), the current one, a lagging or
        far-ahead node's."""
        assert self.state is not None
        iv_index = self._beacon_index(delta)
        if iv_index is None:
            return
        try:
            self.state.apply_beacon(iv_index, update, now=self.now)
        except OSError:
            pass  # the client logs it (the notification handler catches everything); the state moved in memory

    @precondition(lambda self: self.state is not None)
    @rule()
    def iv_update(self) -> None:
        """A whole IV Update as the network runs it: index + 1 "in progress", then back to normal operation."""
        assert self.state is not None
        target = self._beacon_index(1)
        if (
            target is None
        ):  # a beacon's IV index is 32 bits: the network cannot go further
            return
        for update in (True, False):
            self.now += HOURS_96  # the spec's minimum time in each state
            try:
                self.state.apply_beacon(target, update, now=self.now)
            except OSError:
                pass

    @rule(hours=st.sampled_from((1, 95, 96, 191, 192, 1000)))
    def time_passes(self, hours: int) -> None:
        """Wall-clock time passes: around the IV Update minimum times (96 h a step, 192 h between recoveries)."""
        self.now += hours * 3600

    def _beacon_index(self, delta: int) -> int | None:
        """The index a beacon announces: relative to the network's (the highest index announced so far) once the
        client has caught up with it, never below it before — the first beacon a client hears after a lost record
        comes from a node on the network's index, not from one lagging behind it (the guard trusts it:
        `LocalState.seq_guard`)."""
        assert self.state is not None
        if delta < 0 and self.state.iv_index < self.network_iv:
            return None
        iv_index = max(self.state.iv_index, self.network_iv) + delta
        if not 0 <= iv_index <= 0xFFFFFFFF:
            return None
        self.network_iv = max(self.network_iv, iv_index)
        return iv_index

    # ------------------------------------------------------------------ what happens to the process and the disk
    @precondition(lambda self: self.state is not None)
    @rule(clean=st.booleans())
    def restart(self, clean: bool) -> None:
        """The process stops (closed properly, or killed) and starts again from what is on disk."""
        self._release(clean=clean)
        self._open()

    @precondition(
        lambda self: (
            self.state is not None and all(seq < SKIP_UNKNOWN for _, seq in self.used)
        )
    )
    @rule()
    def both_copies_lost_and_skipped(self) -> None:
        """Both files are lost and the record is rebuilt past every number the address may have sent, under an IV
        index nobody knows — what the integration's `seq_store_lost` repair writes (`SEQ_SKIP_UNKNOWN` numbers
        from 0, only while fewer were sent: that is what it promises to cover)."""
        self._release(clean=False)
        self.path.with_suffix(".bak").unlink(missing_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "src": f"{OUR_SRC:04X}",
                    "seq": SKIP_UNKNOWN,
                    "iv_index": 0,
                    "iv_known": False,
                    "seq_guard": self.skip_guard(),
                }
            )
        )
        self._open()

    def skip_guard(self) -> int:
        """The guard the repair writes: the first beacon names it (the integration also carries a guard its repair
        floor holds, `tests/jhmesh/test_iv_timing.py`)."""
        return SEQ_GUARD_FIRST_BEACON

    @precondition(lambda self: self.state is None)
    @rule()
    def start_again(self) -> None:
        """A start that failed on the disk is tried again."""
        self._open()

    @rule(which=st.sampled_from(("primary", "backup")), failing=st.booleans())
    def disk(self, which: str, failing: bool) -> None:
        """Writes of one file start or stop failing (the other file's writes may still land: EIO on one inode)."""
        if failing:
            self.fail.add(which)
        else:
            self.fail.discard(which)

    @rule(
        fault=st.sampled_from(
            ("primary torn", "primary lost", "backup torn", "backup lost")
        )
    )
    def lose_a_file(self, fault: str) -> None:
        """One copy becomes unreadable (a torn write of an older version, a disk fault, a user deleting it) —
        only while the other one is intact: losing both is the documented start-at-0 case."""
        which, how = fault.split()
        target = self.path if which == "primary" else self.path.with_suffix(".bak")
        other = self.path.with_suffix(".bak") if which == "primary" else self.path
        if record(other) is None:
            return
        if how == "torn":
            target.write_text('{"src": "0D00", "seq": 12')
        else:
            target.unlink(missing_ok=True)

    @invariant()
    def a_running_state_is_on_disk(self) -> None:
        """Whatever runs could be resumed from disk right now, by one copy or the other."""
        if self.state is not None:
            assert readable(self.path) or readable(self.path.with_suffix(".bak"))


test_local_state_never_reuses_a_nonce = pytest.mark.slow_ok(LocalStateMachine.TestCase)


@pytest.fixture(autouse=True)
def no_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """`os.fsync` returns at once: the crashes simulated here are process crashes, which the page cache survives,
    so the flush changes nothing a test can see; its thousands of real flushes were most of the machine's run time.
    """
    monkeypatch.setattr(os, "fsync", lambda _fd: None)


def test_a_failed_backup_at_an_iv_change_is_made_up_before_the_next_send(
    tmp_path: Path,
) -> None:
    """The copy's forced write at an IV change fails: no number goes out under the new transmit index until the
    copy is on it too, so a restore from the copy (the state file lost) never goes back to the old index and
    restarts the new one's sequence at 0 over numbers already sent (what the state machine found)."""
    fail: set[str] = set()
    path = tmp_path / "state.json"
    state = FlakyState(fail, path, OUR_SRC)
    used = {(state.tx_iv_index, state.next_seq()) for _ in range(3)}
    # EIO on the copy's inode, or the disk full just when the copy is rewritten
    fail.add("backup")
    with pytest.raises(OSError, match="injected"):
        # IV Index Recovery: transmit index 0 -> 2, sequence from 0
        state.apply_beacon(2, iv_update=False)
    assert state.tx_iv_index == 2
    # the copy is still on index 0: every send retries it first, and fails with it
    for _ in range(3):
        with pytest.raises(OSError, match="injected"):
            state.next_seq()
    assert json.loads(path.with_suffix(".bak").read_text())["iv_index"] == 0
    fail.clear()
    used |= {(state.tx_iv_index, state.next_seq()) for _ in range(10)}
    assert json.loads(path.with_suffix(".bak").read_text())["iv_index"] == 2
    state.close()
    path.unlink()  # the state file is lost; the copy is intact
    state = FlakyState(fail, path, OUR_SRC)
    assert state.tx_iv_index == 2
    state.apply_beacon(2, iv_update=False)  # the proxy's beacon on the next connection
    sent = {(state.tx_iv_index, state.next_seq()) for _ in range(3)}
    state.close()
    assert not sent & used


def test_a_failed_start_releases_the_state_file_lock(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    with pytest.raises(OSError, match="injected") as failed:
        FlakyState({"primary"}, path, OUR_SRC)
    # a caller that keeps the exception (the last error of a retry loop, a task's result, a log record with its
    # traceback) keeps the half-built object, and its lock, alive
    assert failed.tb is not None
    state = LocalState(path, OUR_SRC)
    state.close()


def test_a_skipped_record_keeps_counting_up_to_one_past_the_first_beacons_index(
    tmp_path: Path,
) -> None:
    """A record rebuilt past numbers sent under an unknown index (`seq_guard`): the first beacon's index — and
    every one up to one past it, in case that proxy is still behind an update — continues the counter; only an
    index beyond starts the sequence at 0 again."""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "src": f"{OUR_SRC:04X}",
                "seq": SKIP_UNKNOWN,
                "iv_index": 0,
                "iv_known": False,
                "seq_guard": SEQ_GUARD_FIRST_BEACON,
            }
        )
    )
    state = LocalState(path, OUR_SRC, restart_margin=MARGIN)
    assert state.seq_guard == SEQ_GUARD_FIRST_BEACON
    assert state.apply_beacon(
        5, iv_update=True
    )  # the mesh is mid-update to 5: transmitting under 4
    assert (state.tx_iv_index, state.seq_guard) == (4, 6)
    assert state.seq == SKIP_UNKNOWN + MARGIN
    first = state.next_seq()
    assert state.apply_beacon(
        5, iv_update=False, now=1 * HOURS_96
    )  # the update completes: 5 is still guarded
    assert state.tx_iv_index == 5
    assert state.seq == first + 1
    state.close()
    state = LocalState(
        path, OUR_SRC, restart_margin=MARGIN
    )  # the guard is stored with the counter
    assert state.seq_guard == 6
    assert state.apply_beacon(6, iv_update=True, now=2 * HOURS_96)
    assert state.apply_beacon(
        6, iv_update=False, now=3 * HOURS_96
    )  # 6 too: the first beacon's proxy may have been an update behind the mesh
    assert (state.tx_iv_index, state.seq_guard) == (6, 6)
    assert state.seq > first
    assert state.apply_beacon(7, iv_update=True, now=4 * HOURS_96)
    assert state.apply_beacon(
        7, iv_update=False, now=5 * HOURS_96
    )  # index 7 was never used: the sequence starts over
    assert (state.tx_iv_index, state.seq, state.seq_guard) == (7, 0, None)
    assert "seq_guard" not in json.loads(path.read_text())
    state.close()


def test_the_guard_never_passes_the_last_iv_index() -> None:
    state = LocalState(None, OUR_SRC)
    state.seq_guard = SEQ_GUARD_FIRST_BEACON
    state.apply_beacon(IV_INDEX_MAX, iv_update=False)
    assert state.seq_guard == IV_INDEX_MAX


@pytest.mark.parametrize("guard", [True, "5", 1.5, -2, 1 << 32])
def test_a_record_with_an_unusable_guard_is_refused(guard: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        LocalState.parse_record({"src": "0D00", "seq": 1, "seq_guard": guard})


def test_the_first_beacon_names_the_guard_even_when_it_changes_nothing() -> None:
    state = LocalState(None, OUR_SRC)
    state.seq_guard = SEQ_GUARD_FIRST_BEACON
    assert not state.apply_beacon(
        0, iv_update=False
    )  # index 0, as stored: nothing to follow
    assert state.seq_guard == 1
