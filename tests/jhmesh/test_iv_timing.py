"""IV Update timing (Mesh Protocol 1.1 §3.10.5, §3.10.6) and the rewind of an IV index pushed ahead (review-4 D10).

Authenticated beacons used to move `LocalState` as fast as they arrived: ten beacons each 42 ahead took it from 5
to 425, every node then dropped its PDUs, and no real beacon was accepted again (they are all "behind"). Now a step
between Normal Operation and IV Update in Progress waits 96 hours after the last change, a recovery 192 hours after
the last recovery, and a state pushed ahead anyway can go back (`rewind_iv_index`) without reusing a nonce.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest
from hypothesis import strategies as st
from hypothesis.stateful import invariant, precondition, rule

from jhmesh import client as client_mod
from jhmesh.client import (
    IV_INDEX_MAX,
    IV_RECOVERY_MIN_INTERVAL,
    IV_UPDATE_MIN_STATE,
    SEQ_GUARD_FIRST_BEACON,
    LocalState,
)

from .conftest import OUR_SRC
from .test_properties_local_state import LocalStateMachine

HOUR = 3600
T0 = 1_000 * HOUR  # some wall-clock time well past the epoch


def known(iv_index: int = 5, *, seq: int = 100, at: float = T0) -> LocalState:
    """A state that learnt `iv_index` and last changed its IV state at `at`."""
    state = LocalState(None, OUR_SRC)
    state.iv_index, state.iv_known, state.seq = iv_index, True, seq
    state.iv_changed_at = state.iv_recovered_at = at
    return state


# ----------------------------------------------------------------------------- timing


def test_repeated_beacons_42_ahead_cannot_ratchet_the_iv_index() -> None:
    """The reviewer's repro: only the first IV Index Recovery is followed, the rest come within 192 hours of it."""
    state = LocalState(None, OUR_SRC)
    assert state.apply_beacon(5, False)  # a fresh state learns the network's index
    for _ in range(10):
        state.apply_beacon(state.iv_index + 42, False)
    assert state.iv_index == 5 + 42


def test_a_second_recovery_waits_192_hours() -> None:
    state = LocalState(None, OUR_SRC)
    state.apply_beacon(5, False, now=T0)
    assert state.apply_beacon(47, False, now=T0 + HOUR)  # nothing known: allowed
    assert state.iv_recovered_at == T0 + HOUR
    assert not state.apply_beacon(89, False, now=T0 + 193 * HOUR - 1)
    assert state.apply_beacon(
        48, True, now=T0 + 193 * HOUR - 1
    )  # +1 in progress: a step, 96 h are up
    assert (state.iv_index, state.iv_update_active) == (48, True)
    # 192 h after the last recovery: the next one is allowed, however recent the last step (§3.10.6 bounds
    # recoveries only)
    assert state.apply_beacon(89, False, now=T0 + 193 * HOUR)
    assert (state.iv_index, state.iv_update_active, state.seq) == (89, False, 0)
    assert state.iv_recovered_at == state.iv_changed_at == T0 + 193 * HOUR


def test_a_legitimate_iv_update_is_followed_96_hours_a_step() -> None:
    state = known(5)
    assert not state.apply_beacon(6, True, now=T0 + 95 * HOUR)
    assert state.apply_beacon(6, True, now=T0 + 96 * HOUR)
    assert (state.tx_iv_index, state.seq) == (
        5,
        100,
    )  # still transmitting with the old index
    assert not state.apply_beacon(6, False, now=T0 + 191 * HOUR)
    assert state.apply_beacon(6, False, now=T0 + 192 * HOUR)
    assert (state.tx_iv_index, state.seq, state.iv_changed_at) == (
        6,
        0,
        T0 + 192 * HOUR,
    )
    assert state.iv_recovered_at == T0  # no recovery among them


def test_the_first_update_of_a_record_without_times_is_followed_at_once(
    tmp_path: Path,
) -> None:
    """A record from before the times were kept (or a fresh state) restricts nothing until it has seen a change."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"src": "0D00", "seq": 7, "iv_index": 5}))
    state = LocalState(path, OUR_SRC)
    assert (state.iv_changed_at, state.iv_recovered_at) == (None, None)
    assert state.apply_beacon(6, True, now=T0)
    assert not state.apply_beacon(6, False, now=T0 + HOUR)
    state.close()
    assert read(path)["iv_changed_at"] == T0
    assert "iv_recovered_at" not in read(path)


def test_a_fresh_state_adopts_its_first_beacon_without_a_time() -> None:
    """Joining a network mid-update must not hold its completion back 96 hours: the first beacon stamps nothing."""
    state = LocalState(None, OUR_SRC)
    assert state.apply_beacon(300, True, now=T0)
    assert (state.iv_changed_at, state.iv_recovered_at) == (None, None)
    assert state.apply_beacon(300, False, now=T0 + 1)
    assert state.iv_changed_at == T0 + 1


def test_a_recovery_after_an_outage_is_adopted_once() -> None:
    state = known(
        5, at=T0 - 400 * HOUR
    )  # away for weeks: the mesh went through four updates
    assert state.apply_beacon(9, False, now=T0)
    assert (state.iv_index, state.seq, state.iv_recovered_at) == (9, 0, T0)
    assert not state.apply_beacon(
        12, False, now=T0 + 10 * HOUR
    )  # another one so soon: refused
    assert state.iv_index == 9


def test_a_clock_that_jumped_back_delays_the_next_step_by_one_period_at_most() -> None:
    """Documented rule: a stored time later than the clock is pulled back to the clock, so the wait starts over
    once — not until the clock catches up with a time a year ahead."""
    state = known(5, at=T0)
    back = T0 - 365 * 24 * HOUR
    assert not state.apply_beacon(6, True, now=back)
    assert (state.iv_changed_at, state.iv_recovered_at) == (back, back)
    assert not state.apply_beacon(6, True, now=back + 95 * HOUR)
    assert state.apply_beacon(6, True, now=back + 96 * HOUR)
    state = known(5, at=T0)
    state.iv_changed_at = None  # a record that holds only one of the two times
    assert not state.apply_beacon(9, False, now=back)
    assert (state.iv_changed_at, state.iv_recovered_at) == (None, back)
    state = known(5, at=T0)
    state.iv_recovered_at = None  # ... or only the other
    assert not state.apply_beacon(6, True, now=back)
    assert (state.iv_changed_at, state.iv_recovered_at) == (back, None)


def test_the_wall_clock_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [T0]
    monkeypatch.setattr(client_mod, "_wall_now", lambda: now[0])
    state = known(5, at=T0 - IV_UPDATE_MIN_STATE)
    assert state.apply_beacon(6, True)
    assert state.iv_changed_at == T0
    now[0] += IV_UPDATE_MIN_STATE - 1
    assert not state.apply_beacon(6, False)


def test_a_refusal_is_logged_once_per_index(caplog: pytest.LogCaptureFixture) -> None:
    state = known(5)
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        for _ in range(3):
            assert not state.apply_beacon(47, False, now=T0 + HOUR)
            assert not state.apply_beacon(6, True, now=T0 + HOUR)
        assert not state.apply_beacon(46, False, now=T0 + HOUR)
    refusals = [r.getMessage() for r in caplog.records]
    assert len(refusals) == 3
    assert "IV index 47 (IV Index Recovery)" in refusals[0]
    assert "IV index 6 (IV Update step)" in refusals[1]
    # a change that is accepted starts the count over
    assert state.apply_beacon(6, True, now=T0 + IV_UPDATE_MIN_STATE)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        assert not state.apply_beacon(47, False, now=T0 + IV_UPDATE_MIN_STATE)
    assert len(caplog.records) == 1


def test_the_times_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = LocalState(path, OUR_SRC)
    state.apply_beacon(5, False, now=T0)
    assert state.apply_beacon(20, False, now=T0 + HOUR)
    state.close()
    state = LocalState(path, OUR_SRC)
    assert (state.iv_changed_at, state.iv_recovered_at) == (T0 + HOUR, T0 + HOUR)
    assert not state.apply_beacon(30, False, now=T0 + 2 * HOUR)
    assert state.apply_beacon(30, False, now=T0 + HOUR + IV_RECOVERY_MIN_INTERVAL)
    state.close()


def test_a_configured_new_address_keeps_the_iv_times(tmp_path: Path) -> None:
    """The IV state belongs to the network, not to the address: a fresh sequence space keeps it, times included."""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "src": "0D01",
                "seq": 7,
                "iv_index": 5,
                "iv_changed_at": T0,
                "seq_peak": 900,
                "seq_peak_from": 2,
            }
        )
    )
    state = LocalState(path, OUR_SRC, configured_src_wins=True)
    assert (state.src, state.seq, state.iv_changed_at) == (OUR_SRC, 0, T0)
    assert (state.seq_peak, state.seq_peak_from) == (0, 0)
    state.close()


@pytest.mark.parametrize(
    "fields",
    [
        {"iv_changed_at": True},
        {"iv_changed_at": "soon"},
        {"iv_recovered_at": -1},
        {"iv_recovered_at": float("nan")},
        {"iv_changed_at": float("inf")},
        {"seq_peak": 5},
        {"seq_peak_from": 5},
        {"seq_peak": True, "seq_peak_from": 0},
        {"seq_peak": 0, "seq_peak_from": "1"},
        {"seq_peak": 1 << 24, "seq_peak_from": 0},
        {"seq_peak": 0, "seq_peak_from": -1},
    ],
)
def test_a_record_with_unusable_timing_or_peak_is_refused(fields: dict) -> None:
    with pytest.raises((TypeError, ValueError)):
        LocalState.parse_record({"src": "0D00", "seq": 1, **fields})


# ----------------------------------------------------------------------------- rewind


def read(path: Path) -> dict:
    return dict(json.loads(path.read_text()))


def test_a_record_from_before_the_peak_knows_its_own_transmit_index_only() -> None:
    assert LocalState.parse_seq_peak({"iv_index": 9, "iv_update_active": True}) == (
        0,
        8,
    )
    assert LocalState.parse_seq_peak({}) == (0, 0)
    assert LocalState.parse_seq_peak({"seq_peak": 3, "seq_peak_from": 2}) == (3, 2)


def test_rewind_continues_above_every_number_sent_since_the_networks_index(
    tmp_path: Path,
) -> None:
    """Pushed from 5 to 47: the counter had reached 0x123456 under 5 and restarted at 0 under 47. Going back to 5
    continues above both, and keeps doing so under every index up to 47."""
    path = tmp_path / "state.json"
    state = LocalState(path, OUR_SRC)
    state.apply_beacon(5, False, now=T0)
    state.seq = 0x123456
    # forged, or a store of another mesh
    assert state.apply_beacon(47, False, now=T0 + HOUR)
    state.rpl = {0x0148: (5, 10), 0x0232: (47, 3)}
    state.reserve_seq(40)
    assert state.can_rewind_to(5)
    assert not state.can_rewind_to(47)
    assert state.rewind_iv_index(5) == 0x123456
    assert (state.iv_index, state.iv_update_active, state.iv_known) == (5, False, False)
    assert state.seq_guard == 47
    assert state.rpl == {0x0148: (5, 10)}  # nothing real was ever sent under 47
    assert state.iv_recovered_at == T0 + HOUR  # the pusher gains no fresh recovery
    stored = read(path)
    assert (stored["iv_index"], stored["seq"], stored["seq_guard"]) == (5, 0x123456, 47)
    assert read(path.with_suffix(".bak"))["iv_index"] == 5
    # the network's own beacon: learnt again, nothing else changes
    assert state.apply_beacon(5, False, now=T0 + 2 * HOUR) is False
    assert state.iv_known
    assert state.seq_guard == 47
    assert state.next_seq() == 0x123456
    # the network's next update is under 6: still guarded, the counter carries on
    assert state.apply_beacon(6, True, now=T0 + 100 * HOUR)
    assert state.apply_beacon(6, False, now=T0 + 200 * HOUR)
    assert (state.tx_iv_index, state.seq) == (6, 0x123457)
    state.close()


def test_rewind_is_refused_when_it_cannot_keep_the_nonces_unique() -> None:
    state = known(9)
    state.seq_peak_from = (
        9  # a record from before the peak was kept: nothing known below its index
    )
    assert not state.can_rewind_to(5)
    with pytest.raises(ValueError, match="without reusing"):
        state.rewind_iv_index(5)
    assert not state.can_rewind_to(9)  # not behind us
    state.seq_peak_from = 0
    assert state.can_rewind_to(5)


def test_a_rewound_state_raises_its_guard_to_the_first_beacons_index() -> None:
    """The network may have moved past our pushed index by the time its first beacon arrives: numbers were sent
    under that index too, so the guard covers it (and one more), never less than our old index."""
    state = known(5)
    assert state.apply_beacon(47, False, now=T0 + 200 * HOUR)
    state.rewind_iv_index(5)
    assert state.seq_guard == 47
    state.apply_beacon(
        60, True, now=T0 + 201 * HOUR
    )  # adopted as a fresh state's first beacon
    assert state.seq_guard == 61
    state = known(5)
    assert state.apply_beacon(47, False, now=T0 + 200 * HOUR)
    state.rewind_iv_index(5)
    state.apply_beacon(7, False, now=T0 + 201 * HOUR)
    assert state.seq_guard == 47


def test_rewind_keeps_a_higher_guard() -> None:
    state = known(5)
    state.seq_guard = 50
    state.iv_index = 47
    state.rewind_iv_index(5)
    assert state.seq_guard == 50


# ----------------------------------------------------------------------------- property: no nonce reuse


class IvTimingMachine(LocalStateMachine):
    """`LocalStateMachine` (sends, every kind of beacon, restarts, crashes, failing and lost files) plus beacons that
    push the index ahead of the network, the rewind back to it, and a wall clock that jumps back.

    The base's oracle stays: no (transmit IV index, sequence number) is handed out twice.
    """

    def __init__(self) -> None:
        super().__init__()
        self.floor_guard = SEQ_GUARD_FIRST_BEACON  # what the repair floor would carry (the integration's)
        # the transmit index the integration's floor holds: `HAState` writes it with every move up and sends
        # nothing under an index it does not hold yet (review-4 S4-8); a rewind writes the index it goes back to
        self.floor_iv = 0

    @invariant()
    def the_floor_follows_the_index(self) -> None:
        if self.state is not None:
            self.floor_iv = max(self.floor_iv, self.state.tx_iv_index)

    @precondition(lambda self: self.state is not None)
    @rule(delta=st.sampled_from((2, 5, 42)), update=st.booleans())
    def forged_push(self, delta: int, update: bool) -> None:
        """An authenticated beacon ahead of the network: a NetKey holder's, or one of another mesh's store."""
        assert self.state is not None
        if self.state.iv_index + delta > IV_INDEX_MAX:
            return
        try:
            self.state.apply_beacon(self.state.iv_index + delta, update, now=self.now)
        except OSError:
            pass

    @precondition(lambda self: self.state is not None)
    @rule(update=st.booleans())
    def network_beacon(self, update: bool) -> None:
        """The network's own beacon, behind a pushed state (ignored there), or the first after a rewind."""
        assert self.state is not None
        try:
            self.state.apply_beacon(self.network_iv, update, now=self.now)
        except OSError:
            pass

    @precondition(lambda self: self.state is not None)
    @rule()
    def rewind(self) -> None:
        """The `iv_index_mismatch` repair: offered only when we are ahead and the record knows enough."""
        assert self.state is not None
        target = self.network_iv
        if not (
            self.state.iv_known
            and target < self.state.iv_index - 1
            and self.state.can_rewind_to(target)
        ):
            return
        try:
            self.state.rewind_iv_index(target)
        except OSError:
            pass  # moved in memory; the next send retries the writes (`reserve_seq`)
        assert self.state.seq_guard is not None
        self.floor_guard = max(self.floor_guard, self.state.seq_guard)
        self.floor_iv = target

    @rule(hours=st.sampled_from((1, 96, 24 * 365)))
    def clock_jumps_back(self, hours: int) -> None:
        self.now = max(0.0, self.now - hours * HOUR)

    def skip_guard(self) -> int:
        """The integration's `seq_store_lost` repair carries the guard of an earlier rewind from its floor."""
        return self.floor_guard

    def skip_iv_index(self) -> int:
        """... and continues under its floor's index: below it, a first beacon forged lower than the index the
        address had reached set the guard below that index, and the counter restarted at 0 there (the `thorough`
        profile's counterexample)."""
        return self.floor_iv


@pytest.fixture(autouse=True)
def no_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """`os.fsync` returns at once, as for `LocalStateMachine`: the crashes simulated are process crashes."""
    monkeypatch.setattr(os, "fsync", lambda _fd: None)


# the run time is the example count's (the `thorough` profile runs it for minutes): exempt from the call budget
test_no_nonce_is_reused_across_iv_timing_and_rewinds = pytest.mark.slow_ok(
    IvTimingMachine.TestCase
)
