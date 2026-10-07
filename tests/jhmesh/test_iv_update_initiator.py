"""A Hypothesis state machine over Home Assistant's own IV Update, against a modelled mesh (review 5).

The client (`ProxyClient` over `FakeBleak`, its `LocalState` on disk) starts an IV Update and the machine plays the
mesh behind its proxy: the proxy refuses it (it beacons the old index), takes it at once or late (within 96 hours of
its own last step it refuses first), or runs its own update; a key refresh starts mid-update; Home Assistant
restarts mid-update; the link is lost, at the completion too; a backup taken mid-update is restored (rewritten as the
integration does, `seq_store._async_skip_restored_record`). After every step:

- no (transmit IV index, sequence number) is handed out twice (review-5 S5-2: a restored record completing the update
  by the clock restarted the counter at 0 under an index used since);
- Home Assistant never transmits under an index the devices do not accept yet: the new one only once the mesh has
  been in IV Update in Progress there for 96 hours, or is back in Normal Operation (review-5 P5-1: counted from the
  local start, a late confirmation completed it at once);
- every beacon it sends is that of the key refresh's phase — the transmit key, the Key Refresh flag in Phase 2
  (review-5 P5-2);
- an update the mesh did not take is not kept beyond the 144 hours of §3.11.5 once a beacon of the link was seen
  (review-5 P5-3), and giving it up never takes Home Assistant ahead of the mesh.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import Coroutine
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

from jhmesh import client as client_mod
from jhmesh import state as state_mod
from jhmesh.cdb import CDB
from jhmesh.client import (
    IV_UPDATE_MIN_STATE,
    SEQ_GUARD_FIRST_BEACON,
    SEQ_TX_LIMIT,
    IVUpdateRefused,
    LocalState,
    ProxyClient,
    SequenceExhausted,
)
from jhmesh.crypto import NetKeyMaterial
from jhmesh.pdu import PROXY_BEACON, parse_beacon

from .conftest import CDB_PATH, OUR_SRC, FakeBleak, FastAsyncio

HOUR = 3600
SKIP = (
    1 << 20
)  # how far the integration skips a restored record (`const.SEQ_SKIP_AHEAD`)
NEW_KEY = NetKeyMaterial.derive(bytes(range(16)))  # the key refresh's new NetKey


class RefreshingClient(ProxyClient):
    """A client whose key refresh phase the machine sets: from Phase 2 on it transmits with the new key."""

    phase = 0

    @property
    def key_refresh_phase(self) -> int:
        return self.phase

    @property
    def nk(self) -> NetKeyMaterial:
        return NEW_KEY if self.phase == 2 else super().nk


class Mesh:
    """The mesh behind the proxy: its IV state and when it last changed (wall clock)."""

    def __init__(self, iv_index: int, since: float) -> None:
        self.iv_index, self.in_progress, self.since = iv_index, False, since

    @property
    def tx_iv_index(self) -> int:
        return self.iv_index - 1 if self.in_progress else self.iv_index

    def accepts(self, tx: int, now: float) -> bool:
        """Whether the devices take a PDU sent under `tx` — and §3.11.5's 96 h before anyone sends under the new one."""
        if tx <= self.tx_iv_index:
            return True
        return (
            tx == self.iv_index
            and self.in_progress
            and now - self.since >= IV_UPDATE_MIN_STATE
        )

    def step(self, now: float) -> None:
        """One step of the IV Update procedure."""
        if self.in_progress:
            self.in_progress = False
        else:
            self.iv_index, self.in_progress = self.iv_index + 1, True
        self.since = now


class InitiatorMachine(RuleBasedStateMachine):
    """Home Assistant's own IV Update against a mesh that refuses it, takes it, or runs its own."""

    def __init__(self) -> None:
        super().__init__()
        self.dir = Path(tempfile.mkdtemp(prefix="jhmesh-ivu-"))
        self.path = self.dir / "state.json"
        self.loop = asyncio.new_event_loop()
        self.now = 1_000 * HOUR
        self._wall_now, self._asyncio = state_mod._wall_now, client_mod.asyncio
        state_mod._wall_now = lambda: self.now
        client_mod.asyncio = FastAsyncio()
        self.cdb = CDB.load(CDB_PATH)
        self.mesh = Mesh(5, self.now - 1_000 * HOUR)
        self.state: LocalState | None = None
        self.proxy: RefreshingClient | None = None
        self.link: FakeBleak | None = None
        self.phase = 0
        self.checked = 0  # beacons of the current link already checked
        self.used: set[tuple[int, int]] = set()
        self.backup: dict[str, Any] | None = None

    def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return self.loop.run_until_complete(coro)

    async def _settle(self, turns: int = 6) -> None:
        for _ in range(turns):
            await asyncio.sleep(0)

    def settle(self) -> None:
        self.run(self._settle())

    # ------------------------------------------------------------------ set-up / tear-down
    @initialize()
    def setup(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "src": f"{OUR_SRC:04X}",
                    "seq": 100,
                    "iv_index": 5,
                    "iv_update_active": False,
                    "iv_known": True,
                    "iv_changed_at": self.mesh.since,
                }
            )
        )
        self._open()
        self._attach(beacon=True)

    def teardown(self) -> None:
        try:
            if self.proxy is not None:
                self.run(self.proxy.detach())
            if self.state is not None:
                self.state.close()
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()
        finally:
            state_mod._wall_now, client_mod.asyncio = self._wall_now, self._asyncio
            shutil.rmtree(self.dir, ignore_errors=True)

    def _open(self) -> None:
        self.state = LocalState(self.path, OUR_SRC)
        self.proxy = RefreshingClient(self.cdb, self.state)
        self.proxy.phase = self.phase

    def _attach(self, *, beacon: bool) -> None:
        assert self.proxy is not None
        link = FakeBleak(self.cdb)
        link.iv_index = self.mesh.iv_index
        self.link, self.checked = link, 0

        async def attach() -> None:
            assert self.proxy is not None
            await self.proxy.attach(link, filter_blacklist=False)
            if beacon:
                self._beacon()
            await self._settle()

        self.run(attach())

    def _beacon(self) -> None:
        """The proxy beacons the mesh's state (on a change, and on a new link)."""
        if self.link is not None:
            self.link.send_beacon(
                iv_index=self.mesh.iv_index, iv_update=self.mesh.in_progress
            )

    def _detach(self) -> None:
        self._check_beacons()
        assert self.proxy is not None
        self.run(self.proxy.detach())
        self.link = None

    def _close(self) -> None:
        """The process stops: the link goes, the state is written and released."""
        if self.link is not None:
            self._detach()
        assert self.state is not None
        self.state.close()
        self.state = self.proxy = None

    # ------------------------------------------------------------------ Home Assistant
    @precondition(lambda self: self.link is not None)
    @rule()
    def start(self) -> None:
        """`start_iv_update` (the action, `force`)."""
        assert self.proxy is not None
        try:
            self.run(self.proxy.start_iv_update())
        except (IVUpdateRefused, ConnectionError):
            return
        self.settle()

    @precondition(lambda self: self.link is not None)
    @rule()
    def abort(self) -> None:
        """`abort_iv_update`: never takes Home Assistant ahead of the mesh."""
        assert self.proxy is not None
        try:
            back = self.proxy.abort_iv_update()
        except IVUpdateRefused:
            return
        assert back <= self.mesh.iv_index

    @precondition(lambda self: self.state is not None)
    @rule(count=st.integers(1, 32))
    def send(self, count: int) -> None:
        """Home Assistant sends: a nonce is never used twice, and never under an index the devices do not take."""
        assert self.state is not None
        tx = self.state.tx_iv_index
        try:
            first = self.state.reserve_seq(count)
        except SequenceExhausted:
            return
        assert self.mesh.accepts(tx, self.now), (
            f"sent under IV index {tx}, the mesh at {self.mesh.iv_index}"
            f"{' in progress' if self.mesh.in_progress else ''} since {self.now - self.mesh.since} s"
        )
        for seq in range(first, first + count):
            assert (tx, seq) not in self.used, f"nonce reuse: IV {tx} seq {seq:06X}"
            self.used.add((tx, seq))

    @precondition(lambda self: self.state is not None)
    @rule(beacon=st.booleans())
    def restart(self, beacon: bool) -> None:
        """Home Assistant restarts; the proxy's beacon on the new link may come too late for the first turn."""
        self._close()
        self._open()
        self._attach(beacon=beacon)

    @precondition(lambda self: self.link is not None)
    @rule()
    def lose_link(self) -> None:
        """The link goes, writes failing first (a completion beacon lost with it included)."""
        assert self.link is not None
        self.link.write_error = OSError("link gone")
        self.settle()
        self._detach()

    @precondition(lambda self: self.link is None and self.proxy is not None)
    @rule(beacon=st.booleans())
    def relink(self, beacon: bool) -> None:
        self._attach(beacon=beacon)

    @precondition(lambda self: self.state is not None)
    @rule()
    def take_backup(self) -> None:
        """A Home Assistant backup copies the store (mid-update or not)."""
        assert self.state is not None
        self.state.persist()
        self.backup = json.loads(self.path.read_text())

    @precondition(
        lambda self: (
            self.state is not None
            and self.backup is not None
            and self.backup["seq"] + SKIP <= SEQ_TX_LIMIT
        )
    )
    @rule(beacon=st.booleans())
    def restore_backup(self, beacon: bool) -> None:
        """The backup comes back; the integration skips its record past every number sent since and leaves the
        guard pending the first beacon (a concrete one kept, the index then not known). Once: the same backup
        restored twice would skip to the same numbers again (outside what the skip claims)."""
        assert self.backup is not None
        self._close()
        record, self.backup = dict(self.backup), None
        guard = record.get("seq_guard")
        record["seq"] += SKIP
        if guard is None or guard == SEQ_GUARD_FIRST_BEACON:
            record["seq_guard"] = SEQ_GUARD_FIRST_BEACON
        else:
            record["iv_known"] = False
        for path in (self.path, self.path.with_suffix(".bak")):
            path.write_text(json.dumps(record))
        self._open()
        self._attach(beacon=beacon)

    @rule(phase=st.sampled_from((1, 2)))
    def key_refresh(self, phase: int) -> None:
        """The app runs a key refresh; Home Assistant follows it to `phase` (never back)."""
        if phase <= self.phase:
            return
        self._check_beacons()
        self.phase = phase
        if self.proxy is not None:
            self.proxy.phase = phase
            if self.link is not None:
                self.run(
                    self.proxy._send_iv_beacon()
                )  # a repeat right away, under the new phase

    # ------------------------------------------------------------------ the mesh
    @precondition(lambda self: self.link is not None)
    @rule(take=st.booleans())
    def proxy_answers(self, take: bool) -> None:
        """The proxy processes Home Assistant's beacon (§6.7): takes the update unless within 96 h of its own last
        step, or refuses; either way it beacons its state."""
        assert self.state is not None
        ours = (self.state.iv_index, self.state.iv_update_active)
        if (
            take
            and ours == (self.mesh.iv_index + 1, True)
            and not self.mesh.in_progress
            and self.now - self.mesh.since >= IV_UPDATE_MIN_STATE
        ):
            self.mesh.step(self.now)
        self.run(self._answer())

    async def _answer(self) -> None:
        self._beacon()
        await self._settle()

    @rule()
    def mesh_steps(self) -> None:
        """The mesh moves on by itself (a node ran low and started an update, or one in progress completes), at
        §3.11.5's pace; the proxy beacons it."""
        if self.now - self.mesh.since < IV_UPDATE_MIN_STATE:
            return
        self.mesh.step(self.now)
        if self.link is not None:
            self.run(self._answer())

    @rule(hours=st.sampled_from((1, 24, 47, 95, 96, 143, 144, 200)))
    def time_passes(self, hours: int) -> None:
        self.now += hours * HOUR
        if self.link is not None:
            self.settle()

    # ------------------------------------------------------------------ what always holds
    def _check_beacons(self) -> None:
        """Every beacon Home Assistant wrote on this link is authenticated with the phase's key and carries its flag."""
        if self.link is None:
            return
        beacons = [p for kind, p in self.link.outgoing if kind == PROXY_BEACON]
        key = NEW_KEY if self.phase == 2 else self.link.nk
        for payload in beacons[self.checked :]:
            parsed = parse_beacon(key, payload)
            assert parsed is not None
            assert parsed.authenticated, "a beacon not under the key of the phase"
            assert parsed.key_refresh == (self.phase == 2)
        self.checked = len(beacons)

    @invariant()
    def beacons_follow_the_key_refresh(self) -> None:
        self._check_beacons()

    @invariant()
    def never_ahead_of_the_mesh(self) -> None:
        if self.state is not None:
            assert self.mesh.accepts(self.state.tx_iv_index, self.now)

    @invariant()
    def an_update_not_taken_ends(self) -> None:
        """With a link whose proxy beaconed, an update of ours the mesh has not taken does not outlive §3.11.5."""
        if self.state is not None and self.proxy is not None and self.link is not None:
            if self.proxy.beacon_seen:
                assert not self.state.iv_update_overdue(self.now)


test_the_initiator_keeps_the_procedure = pytest.mark.slow_ok(InitiatorMachine.TestCase)


@pytest.fixture(autouse=True)
def quiet_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """The state file's flushes are page-cache writes here (process crashes, not power cuts): skip them."""
    monkeypatch.setattr(os, "fsync", lambda _fd: None)


def drive(*steps: tuple[str, dict[str, Any]]) -> InitiatorMachine:
    """Run the machine through `steps` (rule name, arguments), checking every invariant after each; torn down."""
    machine = InitiatorMachine()
    checks = (
        machine.beacons_follow_the_key_refresh,
        machine.never_ahead_of_the_mesh,
        machine.an_update_not_taken_ends,
    )
    try:
        machine.setup()
        for name, kwargs in steps:
            getattr(machine, name)(**kwargs)
            for check in checks:
                check()
    finally:
        machine.teardown()
    return machine


def step(name: str, **kwargs: Any) -> tuple[str, dict[str, Any]]:
    return name, kwargs


# The cases the machine is for, each as a fixed sequence too: what a search might take a while to hit.
SCENARIOS = {
    # P5-3: the proxy refuses (within its own 96 h); 144 h on, the update is given up
    "the proxy refuses": [
        step("time_passes", hours=1),
        step("start"),
        step("proxy_answers", take=False),
        step("time_passes", hours=143),
        step("send", count=3),
        step("time_passes", hours=1),
        step("send", count=3),
    ],
    # P5-1: taken 95 h after the start; 96 h after that, not after the start
    "the proxy takes it late": [
        step("start"),
        step("time_passes", hours=95),
        step("proxy_answers", take=True),
        step("time_passes", hours=24),
        step("send", count=2),
        step("time_passes", hours=96),
        step("send", count=2),
    ],
    "the proxy takes it at once": [
        step("start"),
        step("proxy_answers", take=True),
        step("time_passes", hours=96),
        step("send", count=2),
        step("mesh_steps"),
        step("send", count=2),
    ],
    # P5-2: Phase 2 of a key refresh reached mid-update: the repeats and the completion carry it
    "a key refresh starts mid-update": [
        step("start"),
        step("proxy_answers", take=True),
        step("key_refresh", phase=1),
        step("time_passes", hours=24),
        step("key_refresh", phase=2),
        step("time_passes", hours=96),
    ],
    "Home Assistant restarts mid-update": [
        step("start"),
        step("proxy_answers", take=True),
        step("restart", beacon=False),
        step("time_passes", hours=96),
        step("send", count=2),
        step("restart", beacon=True),
        step("send", count=2),
    ],
    "the link is lost at the completion": [
        step("start"),
        step("proxy_answers", take=True),
        step("time_passes", hours=95),
        step("lose_link"),
        step("time_passes", hours=1),
        step("relink", beacon=False),
        step("send", count=2),
    ],
    # S5-2: restored without the proxy's beacon in time; numbers went out under 6 from 0 since the backup
    "a backup taken mid-update is restored": [
        step("start"),
        step("proxy_answers", take=True),
        step("take_backup"),
        step("time_passes", hours=96),
        step("send", count=5),
        step("restore_backup", beacon=False),
        step("send", count=5),
        step("proxy_answers", take=False),
        step("send", count=5),
    ],
}


@pytest.mark.parametrize("steps", SCENARIOS.values(), ids=SCENARIOS)
def test_scenario(steps: list[tuple[str, dict[str, Any]]]) -> None:
    drive(*steps)


def test_a_refused_update_ends_back_at_the_meshs_index() -> None:
    machine = drive(*SCENARIOS["the proxy refuses"])
    assert machine.mesh.iv_index == 5
    assert {tx for tx, _ in machine.used} == {5}
