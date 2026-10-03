"""jhmesh.keyrefresh: a key refresh is followed only on proof that the mesh moved (review-4 D4).

The unit tests walk the rules one by one; the Hypothesis state machine throws every mix of requests, statuses,
beacons and restarts at a follower and checks after each step that the export's key is never dropped, nor another
key transmitted with, without the proof the module promises.
"""

from __future__ import annotations

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

from jhmesh.keyrefresh import (
    MAX_CANDIDATES,
    PROOF_BEACON,
    PROOF_EXPORT,
    PROOF_PROXY,
    PROOF_STATUSES,
    KeyRefreshFollower,
    KeyRefreshRecord,
    Moved,
    describe_proof,
)

EXPORT = bytes(16)  # the export's NetKey
NEW = bytes(range(16))
OTHER = bytes(range(16, 32))
A, B, C, PROXY = 0x0232, 0x0172, 0x0300, 0x0148


def key(n: int) -> bytes:
    return bytes([n]) * 16


# ============================================================================= the record


def test_a_record_round_trips_and_keeps_the_old_shape() -> None:
    bare = KeyRefreshRecord(NEW, 1)
    assert bare.to_stored() == {
        "key": NEW.hex(),
        "phase": 1,
    }  # what every record before review 4 looked like
    assert KeyRefreshRecord.from_stored(bare.to_stored()) == bare
    assert not bare.proven
    full = KeyRefreshRecord(
        NEW,
        3,
        PROOF_STATUSES,
        frozenset({A, B}),
        {1: frozenset({A}), 3: frozenset({A, B}), 2: frozenset()},
    )
    stored = full.to_stored()
    assert stored == {
        "key": NEW.hex(),
        "phase": 3,
        "proof": "statuses",
        "nodes": ["0172", "0232"],
        "confirmed": {"1": ["0232"], "3": ["0172", "0232"]},
    }
    again = KeyRefreshRecord.from_stored(stored)
    assert again.proven
    assert (again.key, again.phase, again.proof, again.nodes) == (
        NEW,
        3,
        PROOF_STATUSES,
        frozenset({A, B}),
    )
    assert again.confirmed == {1: frozenset({A}), 3: frozenset({A, B})}
    assert NEW.hex() not in repr(full)  # key material never in a repr


@pytest.mark.parametrize(
    ("record", "error"),
    [
        ([], TypeError),
        ({"phase": 1}, KeyError),
        ({"key": "00", "phase": 1}, ValueError),
        ({"key": NEW.hex()}, KeyError),
        ({"key": NEW.hex(), "phase": 0}, ValueError),
        ({"key": NEW.hex(), "phase": True}, ValueError),
        ({"key": NEW.hex(), "phase": "2"}, ValueError),
        ({"key": NEW.hex(), "phase": 2, "proof": "rumour"}, ValueError),
        ({"key": NEW.hex(), "phase": 2, "confirmed": ["2"]}, TypeError),
        ({"key": NEW.hex(), "phase": 2, "confirmed": {"4": []}}, ValueError),
        ({"key": NEW.hex(), "phase": 2, "confirmed": {"2": "0232"}}, TypeError),
        ({"key": NEW.hex(), "phase": 2, "nodes": ["8000"]}, ValueError),
        ({"key": NEW.hex(), "phase": 2, "nodes": ["0000"]}, ValueError),
        ({"key": NEW.hex(), "phase": 2, "nodes": ["xyz"]}, ValueError),
    ],
)
def test_a_record_that_is_not_one_is_refused(
    record: Any, error: type[Exception]
) -> None:
    with pytest.raises(error):
        KeyRefreshRecord.from_stored(record)


def test_the_proof_is_described_without_the_key() -> None:
    assert (
        describe_proof(Moved(2, NEW, PROOF_BEACON))
        == "the proxy's beacon under the new key"
    )
    assert (
        describe_proof(Moved(2, NEW, PROOF_STATUSES, (B, A)))
        == "Key Refresh Phase Status from nodes 0172, 0232"
    )
    assert (
        describe_proof(Moved(0, NEW, PROOF_PROXY, (PROXY,)))
        == "Key Refresh Phase Status from the proxy node 0148"
    )
    assert describe_proof(Moved(1, NEW)) == "none"
    assert (
        describe_proof(Moved(1, NEW, PROOF_STATUSES, (B, A)))
        == "the new key confirmed by nodes 0172, 0232"
    )
    assert (
        describe_proof(Moved(1, NEW, PROOF_PROXY, (PROXY,)))
        == "the new key confirmed by the proxy node 0148"
    )


# ============================================================================= the rules


def test_requests_alone_never_move_it() -> None:
    f = KeyRefreshFollower(EXPORT)
    assert (f.phase, f.tx_key, f.rx_keys, f.new_key, f.record()) == (
        0,
        EXPORT,
        (EXPORT,),
        None,
        None,
    )
    assert f.learn(EXPORT, A) is None  # the key we have
    assert f.learn(NEW, A) == Moved(1, NEW)
    assert f.learn(NEW, B) is None  # the same key to another node
    for node in (A, B, PROXY):
        f.requested(node, 2)
        f.requested(node, 3)
    assert (f.phase, f.tx_key, f.rx_keys) == (1, EXPORT, (EXPORT, NEW))
    record = f.record()
    assert record == KeyRefreshRecord(NEW, 1, None, frozenset({A, B}))


def test_one_nodes_statuses_are_no_proof_but_two_are() -> None:
    f = KeyRefreshFollower(EXPORT)
    f.learn(NEW, A)
    f.learn(NEW, B)
    f.netkey_status(A, ok=True)
    f.netkey_status(B, ok=False)
    assert f.phase_status(A, 2, PROXY) is None
    assert f.phase_status(A, 2, PROXY) is None  # the same node again
    assert (
        f.phase_status(C, 2, PROXY) is None
    )  # no Update seen to it: whose key would it speak for?
    moved = f.phase_status(B, 2, None)
    assert moved == Moved(2, NEW, PROOF_STATUSES, (B, A))
    assert (f.phase, f.tx_key) == (2, NEW)
    assert f.phase_status(B, 2, None) is None  # already there
    # phase 0 from a node that was not asked to revoke, or never confirmed holding the key: nothing
    f.learn(NEW, C)
    f.requested(C, 3)
    f.requested(A, 3)
    assert f.phase_status(A, 0, None) is None
    moved = f.phase_status(
        C, 0, None
    )  # C never confirmed, yet the refresh is proven (trusted): it counts
    assert moved == Moved(0, NEW, PROOF_STATUSES, (A, C))
    assert (f.phase, f.current, f.tx_key, f.rx_keys) == (0, NEW, NEW, (NEW,))
    assert f.record() == KeyRefreshRecord(
        NEW,
        3,
        PROOF_STATUSES,
        frozenset({A, B, C}),
        {1: frozenset({A}), 2: frozenset({A, B}), 3: frozenset({A, C})},
    )


def test_phase_three_needs_a_node_that_held_the_key() -> None:
    """Phase 0 after Phase Set 3 is also what a node answers that never took the key: counted only from one that
    confirmed it (NetKey Status, or a phase 1 / 2 status) while nothing is proven yet."""
    f = KeyRefreshFollower(EXPORT)
    for node in (A, B):
        f.learn(NEW, node)
        f.requested(node, 3)
    assert f.phase_status(A, 0, None) is None
    assert f.phase_status(B, 0, None) is None
    assert f.phase == 1
    f.netkey_status(A, ok=True)
    f.phase_status(B, 1, None)
    assert f.phase_status(A, 0, None) is None
    assert f.phase_status(B, 0, None) == Moved(0, NEW, PROOF_STATUSES, (B, A))


def test_the_proxys_word_is_enough() -> None:
    f = KeyRefreshFollower(EXPORT)
    f.learn(NEW, PROXY)
    assert f.phase_status(PROXY, 2, None) is None  # the proxy not known yet
    assert f.phase_status(PROXY, 2, PROXY) == Moved(2, NEW, PROOF_PROXY, (PROXY,))
    f.requested(PROXY, 3)
    assert f.phase_status(PROXY, 0, PROXY) == Moved(0, NEW, PROOF_PROXY, (PROXY,))


def test_beacons_under_a_candidate_move_it() -> None:
    f = KeyRefreshFollower(EXPORT)
    assert f.beacon(NEW, True) is None  # not a candidate
    f.learn(NEW, A)
    assert f.beacon(NEW, True) == Moved(2, NEW, PROOF_BEACON)
    assert f.beacon(NEW, True) is None
    assert f.beacon(NEW, False) == Moved(0, NEW, PROOF_BEACON)
    assert f.rx_keys == (NEW,)


def test_a_new_candidate_during_phase_two_keeps_the_switched_key() -> None:
    f = KeyRefreshFollower(EXPORT)
    f.learn(NEW, A)
    f.beacon(NEW, True)
    assert f.learn(OTHER, A) is None  # accepted, no move
    assert (f.phase, f.tx_key, f.rx_keys, f.new_key) == (
        2,
        NEW,
        (EXPORT, NEW, OTHER),
        NEW,
    )
    # the other key proven too: it takes over, the first stays accepted until a phase 3
    assert f.beacon(OTHER, True) == Moved(2, OTHER, PROOF_BEACON)
    assert (f.tx_key, f.rx_keys) == (OTHER, (EXPORT, NEW, OTHER))


def test_the_key_most_nodes_were_sent_leads() -> None:
    f = KeyRefreshFollower(EXPORT)
    f.learn(NEW, A)
    f.learn(NEW, B)
    f.learn(OTHER, C)  # later, one node
    assert f.new_key == NEW
    f.learn(OTHER, B)  # the app sent B the other key since: B's vote moves with it
    assert f.new_key == OTHER
    tie = KeyRefreshFollower(EXPORT)
    tie.learn(NEW, A)
    tie.learn(OTHER, B)
    assert tie.new_key == OTHER  # one node each: the later learnt


def test_a_completion_the_export_lacks_outlives_a_later_phase_one() -> None:
    f = KeyRefreshFollower(EXPORT)
    f.learn(NEW, A)
    f.beacon(NEW, False)
    done = f.record()
    assert done is not None
    assert (done.key, done.phase) == (NEW, 3)
    f.learn(OTHER, A)  # a second refresh before the export was fetched again
    assert (
        f.record() == done
    )  # a restart keeps transmitting with NEW, not the export's revoked key
    f.beacon(OTHER, True)
    record = f.record()
    assert record is not None
    assert (record.key, record.phase) == (OTHER, 2)


def test_a_flood_of_candidates_keeps_the_key_the_nodes_vouch_for() -> None:
    """A node forging Update after Update fills the candidates; the newest of the least vouched-for goes."""
    f = KeyRefreshFollower(EXPORT)
    for node in (B, C, PROXY):
        f.learn(NEW, node)
    for n in range(1, 10):
        f.learn(key(n), A)
    assert len(f.rx_keys) == 1 + MAX_CANDIDATES
    assert NEW in f.rx_keys
    assert key(1) in f.rx_keys  # the oldest forged ones stay: the newest goes
    assert key(9) in f.rx_keys
    # statuses of the evicted key's node now count for nothing (the node's entry went with it)
    assert f.new_key == NEW


def test_kept_keys_are_never_evicted() -> None:
    """The export's refresh and every proven key stay, even past the limit (only proofs add those)."""
    f, _ = KeyRefreshFollower.resume(EXPORT, (key(1), 1), None)
    for n in range(2, 2 + MAX_CANDIDATES):
        f.learn(key(n), A)
        f.beacon(key(n), True)
    assert len(f.rx_keys) == 2 + MAX_CANDIDATES
    f.learn(key(99), A)  # nothing to evict: one more
    assert len(f.rx_keys) == 3 + MAX_CANDIDATES


# ============================================================================= resuming


def test_resume_without_anything() -> None:
    f, keep = KeyRefreshFollower.resume(EXPORT, None, None)
    assert (f.phase, f.rx_keys, keep) == (0, (EXPORT,), True)


@pytest.mark.parametrize("phase", [1, 2])
def test_resume_an_export_written_mid_refresh(phase: int) -> None:
    f, _ = KeyRefreshFollower.resume(EXPORT, (NEW, phase), None)
    assert (f.phase, f.rx_keys) == (phase, (EXPORT, NEW))
    assert f.tx_key == (NEW if phase == 2 else EXPORT)
    record = f.record()
    assert record is not None
    # review-4 D11: the export's own refresh is the provisioner's at phase 1 too, so it may be handed out
    assert record.proof == PROOF_EXPORT
    assert f.distribution == (phase, NEW)
    # statuses of nodes no Update was seen to count for the export's own refresh
    f.requested(A, 3)
    f.requested(B, 3)
    f.phase_status(A, 0, None)
    assert f.phase_status(B, 0, None) == Moved(0, NEW, PROOF_STATUSES, (B, A))


def test_resume_a_stored_record() -> None:
    proven2 = KeyRefreshRecord(
        NEW, 2, PROOF_BEACON, frozenset({A}), {2: frozenset({A})}
    )
    f, keep = KeyRefreshFollower.resume(EXPORT, None, proven2)
    assert (f.phase, f.tx_key, keep) == (2, NEW, True)
    assert f.record() == proven2
    # the same key as the export's refresh, proven further: the further phase
    f, _ = KeyRefreshFollower.resume(EXPORT, (NEW, 1), proven2)
    assert (f.phase, f.rx_keys) == (2, (EXPORT, NEW))
    # the export has the key already: behind us (phase 3 kept for the next setup)
    _, keep = KeyRefreshFollower.resume(
        NEW, None, KeyRefreshRecord(NEW, 2, PROOF_BEACON)
    )
    assert keep is False
    _, keep = KeyRefreshFollower.resume(
        NEW, None, KeyRefreshRecord(NEW, 3, PROOF_BEACON)
    )
    assert keep is True
    # a proven completion: the new key alone, the record kept until the export has it
    done = KeyRefreshRecord(NEW, 3, PROOF_PROXY)
    f, _ = KeyRefreshFollower.resume(EXPORT, (OTHER, 2), done)
    assert (f.phase, f.current, f.rx_keys, f.record()) == (0, NEW, (NEW,), done)


@pytest.mark.parametrize("phase", [2, 3])
def test_resume_an_unproven_record_as_a_candidate(phase: int) -> None:
    f, keep = KeyRefreshFollower.resume(
        EXPORT, None, KeyRefreshRecord(NEW, phase, None, frozenset({A}))
    )
    assert (f.phase, f.tx_key, f.rx_keys, keep) == (1, EXPORT, (EXPORT, NEW), True)
    # its nodes still count: A's status speaks for it
    assert f.phase_status(A, 2, A) == Moved(2, NEW, PROOF_PROXY, (A,))


def test_resume_the_export_refresh_and_another_stored_key() -> None:
    f, _ = KeyRefreshFollower.resume(EXPORT, (NEW, 2), KeyRefreshRecord(OTHER, 1))
    assert (f.phase, f.tx_key, f.rx_keys) == (2, NEW, (EXPORT, NEW, OTHER))


# ============================================================================= proven Phase 1 (review-4 D11)


def test_phase_one_is_proven_by_two_nodes_holding_the_key() -> None:
    """What may be handed to the nodes only Home Assistant knows: never a key one node's word gave."""
    f = KeyRefreshFollower(EXPORT)
    f.learn(NEW, A)
    f.learn(NEW, B)
    assert f.distribution is None
    assert f.netkey_status(A, ok=True) is None  # one node
    assert f.netkey_status(B, ok=False) is None  # refused: no confirmation
    assert f.netkey_status(C, ok=True) is None  # no Update seen to it
    assert f.distribution is None
    moved = f.phase_status(
        B, 1, None
    )  # a Phase Status reporting phase 1 says it holds the key too
    assert moved == Moved(1, NEW, PROOF_STATUSES, (B, A))
    assert f.distribution == (1, NEW)
    assert f.netkey_status(B, ok=True) is None  # proven already
    assert (f.phase, f.tx_key) == (
        1,
        EXPORT,
    )  # the proof hands the key out; it moves nothing here
    assert f.record() == KeyRefreshRecord(
        NEW, 1, PROOF_STATUSES, frozenset({A, B}), {1: frozenset({A, B})}
    )
    # a restart keeps it handed out
    again, _ = KeyRefreshFollower.resume(EXPORT, None, f.record())
    assert again.distribution == (1, NEW)
    # Phase 2, then the completion: further each time
    f.phase_status(A, 2, None)
    f.phase_status(B, 2, None)
    assert f.distribution == (2, NEW)
    f.requested(A, 3)
    f.requested(B, 3)
    f.phase_status(A, 0, None)
    f.phase_status(B, 0, None)
    assert f.distribution == (3, NEW)
    done, _ = KeyRefreshFollower.resume(EXPORT, None, f.record())
    assert done.distribution == (3, NEW)
    # once the export holds the new key, there is nothing left to hand out
    caught_up, _ = KeyRefreshFollower.resume(NEW, None, f.record())
    assert caught_up.distribution is None


def test_the_proxys_netkey_status_proves_phase_one() -> None:
    f = KeyRefreshFollower(EXPORT)
    f.learn(NEW, PROXY)
    assert f.netkey_status(PROXY, ok=True, proxy=PROXY) == Moved(
        1, NEW, PROOF_PROXY, (PROXY,)
    )
    assert f.distribution == (1, NEW)


def test_a_forged_key_is_never_handed_out() -> None:
    """One node seals NetKey Update with a key of its choice and confirms every phase itself."""
    f = KeyRefreshFollower(EXPORT)
    f.learn(OTHER, A)
    f.requested(A, 3)
    f.netkey_status(A, ok=True)
    for phase in (1, 2, 0):
        f.phase_status(A, phase, None)
    assert f.distribution is None
    record = f.record()
    assert record is not None
    assert (record.key, record.phase, record.proof) == (OTHER, 1, None)
    # nor next to a proven one: the proven key, not the most recent
    f.learn(NEW, B)
    f.learn(NEW, C)
    f.netkey_status(B, ok=True)
    f.netkey_status(C, ok=True)
    assert f.distribution == (1, NEW)


def test_a_proven_phase_two_proves_phase_one() -> None:
    f = KeyRefreshFollower(EXPORT)
    f.learn(NEW, PROXY)
    assert f.phase_status(PROXY, 2, PROXY) == Moved(2, NEW, PROOF_PROXY, (PROXY,))
    assert f.distribution == (2, NEW)
    # a stored Phase 2 that a restart takes up: handed out too
    again, _ = KeyRefreshFollower.resume(EXPORT, None, f.record())
    assert again.distribution == (2, NEW)


# ============================================================================= the state machine

NODES = (A, B, C, PROXY)
KEYS = tuple(key(n) for n in range(1, 8))  # more than MAX_CANDIDATES


class FollowerMachine(RuleBasedStateMachine):
    """Requests, statuses, beacons and restarts in any order; the model only remembers which proofs were offered."""

    @initialize(
        exported=st.none() | st.tuples(st.sampled_from(KEYS), st.sampled_from((1, 2)))
    )
    def start(self, exported: tuple[bytes, int] | None) -> None:
        self.exported = exported
        self.f, _ = KeyRefreshFollower.resume(EXPORT, exported, None)
        self.beacon_phase2 = False  # a beacon under a candidate was offered
        self.beacon_phase3 = False  # ... with the Key Refresh flag clear
        self.status2: set[int] = set()  # nodes that claimed phase 2 (or 0: past it)
        self.status0: set[int] = set()  # nodes that claimed phase 0
        self.held: set[int] = (
            set()
        )  # nodes that confirmed holding a key (any successful status)
        self.proxy_spoke = False  # ... the proxy among them, known as the proxy
        self.switched: set[bytes] = (
            set()
        )  # keys transmitted with since the last completion

    # -- what the follower may have been shown
    def _proof3(self) -> bool:
        return self.beacon_phase3 or PROXY in self.status0 or len(self.status0) >= 2

    def _proof2(self) -> bool:
        return (
            self._proof3()
            or self.beacon_phase2
            or PROXY in self.status2
            or len(self.status2) >= 2
        )

    @rule(k=st.sampled_from((*KEYS, EXPORT)), node=st.sampled_from(NODES))
    def learn(self, k: bytes, node: int) -> None:
        self.f.learn(k, node)

    @rule(node=st.sampled_from(NODES), transition=st.sampled_from((2, 3)))
    def requested(self, node: int, transition: int) -> None:
        self.f.requested(node, transition)

    @rule(node=st.sampled_from(NODES), ok=st.booleans(), proxy_known=st.booleans())
    def netkey_status(self, node: int, ok: bool, proxy_known: bool) -> None:
        if ok:
            self._held(node, proxy_known)
        self._moved(self.f.netkey_status(node, ok, PROXY if proxy_known else None))

    @rule(
        node=st.sampled_from(NODES),
        phase=st.sampled_from((0, 1, 2)),
        proxy_known=st.booleans(),
    )
    def phase_status(self, node: int, phase: int, proxy_known: bool) -> None:
        self._held(node, proxy_known)
        if phase in (0, 2):
            self.status2.add(node)
        if phase == 0:
            self.status0.add(node)
        self._moved(self.f.phase_status(node, phase, PROXY if proxy_known else None))

    @precondition(lambda self: len(self.f.rx_keys) > 1)
    @rule(data=st.data(), key_refresh=st.booleans())
    def beacon(self, data: st.DataObject, key_refresh: bool) -> None:
        # the client offers a beacon only when a candidate (not the current key) authenticated it
        k = data.draw(st.sampled_from(self.f.rx_keys[1:]))
        self.beacon_phase2 = True
        if not key_refresh:
            self.beacon_phase3 = True
        self._moved(self.f.beacon(k, key_refresh))

    @rule()
    def restart(self) -> None:
        record = self.f.record()
        if record is not None:
            assert KeyRefreshRecord.from_stored(record.to_stored()) == record
        # (a record the export has caught up with is dropped by the client: the follower is the same either way)
        self.f, _ = KeyRefreshFollower.resume(EXPORT, self.exported, record)
        self.switched = {self.f.tx_key} - {EXPORT}

    def _held(self, node: int, proxy_known: bool) -> None:
        self.held.add(node)
        self.proxy_spoke = self.proxy_spoke or (node == PROXY and proxy_known)

    def _moved(self, moved: Moved | None) -> None:
        if moved is None:
            return
        if moved.phase == 0:
            self.switched.clear()

    # -- what must hold whatever happened
    @invariant()
    def the_exports_key_is_never_dropped_without_proof(self) -> None:
        assert EXPORT in self.f.rx_keys or self._proof3()

    @invariant()
    def nothing_else_is_transmitted_with_without_proof(self) -> None:
        exported2 = (
            self.exported[0] if self.exported and self.exported[1] == 2 else None
        )
        tx = self.f.tx_key
        assert tx in (EXPORT, exported2) or self._proof2()
        if tx != EXPORT:
            self.switched.add(tx)

    @invariant()
    def a_key_transmitted_with_stays_accepted_until_a_proven_phase_three(self) -> None:
        assert self.switched <= set(self.f.rx_keys)

    @invariant()
    def nothing_is_handed_out_without_proof(self) -> None:
        """Review-4 D11: what the nodes only Home Assistant knows get is the export's refresh or a proven key."""
        assert (
            self.f.distribution is None
            or self.exported is not None
            or self.beacon_phase2
            or self.proxy_spoke
            or len(self.held) >= 2
        )

    @invariant()
    def candidates_stay_bounded(self) -> None:
        loose = [c for c in self.f._candidates.values() if not c.kept]
        assert len(loose) <= MAX_CANDIDATES


TestFollowerMachine = pytest.mark.slow_ok(FollowerMachine.TestCase)
