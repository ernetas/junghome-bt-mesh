"""Starting an IV Update as a proxy client (review-4 P I-11; Mesh Protocol 1.1 §3.11.5, §3.10.3.1, §6.7).

`LocalState.start_iv_update` moves to IV index + 1, IV Update in Progress, persisted before anything is sent, under
its guards (Normal Operation, 96 h since the last change, no key refresh, an index a beacon confirmed);
`ProxyClient.start_iv_update` sends the proxy the authenticated Secure Network Beacon of it, repeats it every Beacon
Interval, takes the proxy's beacon back as the confirmation and returns to Normal Operation 96 h later.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from jhmesh import client as client_mod
from jhmesh import state as state_mod
from jhmesh.client import (
    IV_BEACON_INTERVAL,
    IV_BEACON_INTERVAL_MAX,
    IV_INDEX_MAX,
    IV_ORIGIN_BEACON,
    IV_ORIGIN_LOCAL,
    IV_UPDATE_MAX_STATE,
    IV_UPDATE_MIN_STATE,
    IVUpdateRefused,
    LocalState,
    ProxyClient,
    _AckState,
)
from jhmesh.crypto import NetKeyMaterial
from jhmesh.keyrefresh import KeyRefreshRecord
from jhmesh.pdu import (
    PROXY_BEACON,
    SecureNetworkBeacon,
    parse_beacon,
    secure_network_beacon,
)

from .conftest import OUR_SRC, FakeBleak, FastAsyncio

if TYPE_CHECKING:
    from jhmesh.cdb import CDB

HOUR = 3600
T0 = 1_000 * HOUR  # some wall-clock time well past the epoch


def h(s: str) -> bytes:
    return bytes.fromhex(s)


def known(
    iv_index: int = 5, *, at: float | None = T0, path: Path | None = None
) -> LocalState:
    """A state that learnt `iv_index` from a beacon and last changed its IV state at `at` (None: not known)."""
    state = LocalState(path, OUR_SRC)
    state.iv_index, state.iv_known, state.seq = iv_index, True, 100
    state.iv_changed_at = at
    return state


class Clock:
    """The wall clock `jhmesh.state` reads (`_wall_now`), set by the test."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock(T0 + IV_UPDATE_MIN_STATE)
    monkeypatch.setattr(state_mod, "_wall_now", clock)
    return clock


# ----------------------------------------------------------------------------- the beacon


def test_the_beacon_is_the_spec_sample() -> None:
    """Mesh Protocol 1.1 §8.4.4: the Secure Network beacon of an IV Update in progress."""
    nk = NetKeyMaterial.derive(h("7dd7364cd842ad18c17c2b820c84c3d6"))
    beacon = secure_network_beacon(nk, 0x12345679, iv_update=True)
    assert beacon == h("01023ecaff672f67337012345679c2af80ad072a135c")
    assert parse_beacon(nk, beacon) == SecureNetworkBeacon(
        key_refresh=False,
        iv_update=True,
        network_id=h("3ecaff672f673370"),
        iv_index=0x12345679,
        authenticated=True,
    )
    flagged = parse_beacon(
        nk, secure_network_beacon(nk, 7, iv_update=False, key_refresh=True)
    )
    assert flagged is not None
    assert (flagged.key_refresh, flagged.iv_update, flagged.authenticated) == (
        True,
        False,
        True,
    )


# ----------------------------------------------------------------------------- the guards


def test_start_moves_to_the_next_index_in_progress(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = known(5, path=path)
    assert state.start_iv_update(now=T0 + IV_UPDATE_MIN_STATE) == 6
    assert (state.iv_index, state.iv_update_active, state.tx_iv_index, state.seq) == (
        6,
        True,
        5,
        100,
    )
    assert (
        state.iv_update_origin,
        state.iv_update_started_at,
        state.iv_update_confirmed,
    ) == (
        IV_ORIGIN_LOCAL,
        T0 + IV_UPDATE_MIN_STATE,
        False,
    )
    assert state.iv_changed_at == T0 + IV_UPDATE_MIN_STATE
    for stored in (
        json.loads(path.read_text()),
        json.loads(path.with_suffix(".bak").read_text()),
    ):
        assert {
            k: stored[k]
            for k in (
                "iv_index",
                "iv_update_active",
                "iv_update_origin",
                "iv_update_confirmed",
            )
        } == {
            "iv_index": 6,
            "iv_update_active": True,
            "iv_update_origin": "local",
            "iv_update_confirmed": False,
        }
    state.close()
    # a restart keeps who started it, when, and that the mesh has not confirmed it yet
    again = LocalState(path, OUR_SRC)
    assert (
        again.iv_update_origin,
        again.iv_update_started_at,
        again.iv_update_confirmed,
    ) == (
        IV_ORIGIN_LOCAL,
        T0 + IV_UPDATE_MIN_STATE,
        False,
    )
    again.close()


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda s: setattr(s, "iv_update_active", True), "in_progress"),
        (
            lambda s: setattr(s, "key_refresh", KeyRefreshRecord(bytes(16), 1)),
            "key_refresh",
        ),
        (
            lambda s: setattr(s, "key_refresh", KeyRefreshRecord(bytes(16), 2)),
            "key_refresh",
        ),
        (lambda s: setattr(s, "iv_known", False), "iv_unknown"),
        (lambda s: setattr(s, "iv_changed_at", None), "iv_unknown"),
        (lambda s: setattr(s, "iv_index", IV_INDEX_MAX), "iv_max"),
        (lambda s: setattr(s, "iv_changed_at", T0 + 1), "too_early"),
    ],
)
def test_start_is_refused(setup: Any, reason: str) -> None:
    state = known(5)
    setup(state)
    before = state.to_stored()
    with pytest.raises(IVUpdateRefused) as refused:
        state.start_iv_update(now=T0 + IV_UPDATE_MIN_STATE)
    assert refused.value.reason == reason
    assert state.to_stored() == before  # nothing moved


def test_too_early_says_from_when() -> None:
    state = known(5)
    with pytest.raises(IVUpdateRefused) as refused:
        state.start_iv_update(now=T0 + HOUR)
    assert refused.value.not_before == T0 + IV_UPDATE_MIN_STATE
    assert "96 h" in str(refused.value)
    # a clock that went back: a full period from now, not until it catches up
    with pytest.raises(IVUpdateRefused) as refused:
        state.start_iv_update(now=T0 - HOUR)
    assert refused.value.not_before == T0 - HOUR + IV_UPDATE_MIN_STATE


def test_a_completed_key_refresh_does_not_refuse() -> None:
    state = known(5)
    state.key_refresh = KeyRefreshRecord(
        bytes(16), 3
    )  # the old key revoked: over in the mesh
    assert state.start_iv_update(now=T0 + IV_UPDATE_MIN_STATE) == 6


def test_no_known_change_needs_the_index_from_a_beacon_of_this_link(
    clock: Clock,
) -> None:
    state = known(5, at=None)
    with pytest.raises(IVUpdateRefused, match="confirmed by a beacon"):
        state.start_iv_update()
    assert state.start_iv_update(index_confirmed=True) == 6
    assert state.iv_update_started_at == clock.now  # the wall clock by default


def test_a_write_that_fails_puts_the_state_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    state = known(5, path=path)
    state.persist()
    before = state.to_stored()

    def full_disk(_path: Path, _data: dict[str, Any]) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(state, "_write", full_disk)
    with pytest.raises(OSError, match="no space"):
        state.start_iv_update(now=T0 + IV_UPDATE_MIN_STATE)
    assert state.to_stored() == before
    monkeypatch.undo()
    assert json.loads(path.read_text())["iv_index"] == 5
    state.close()


def test_revert_only_undoes_an_unconfirmed_start_of_ours(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = known(5)
    state.revert_iv_update_start()  # nothing started: nothing to undo
    assert state.iv_index == 5
    state.start_iv_update(now=T0 + IV_UPDATE_MIN_STATE)
    state.apply_beacon(6, True, now=T0 + IV_UPDATE_MIN_STATE + 1)  # the mesh took it
    state.revert_iv_update_start()
    assert (state.iv_index, state.iv_update_active) == (6, True)
    # a revert whose write fails is only logged: the state in memory is what counts
    state = known(5)
    state.start_iv_update(now=T0 + IV_UPDATE_MIN_STATE)

    def broken() -> None:
        raise OSError("disk gone")

    state.persist_now = broken  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        state.revert_iv_update_start()
    assert (state.iv_index, state.iv_update_active, state.iv_update_origin) == (
        5,
        False,
        None,
    )
    assert "could not write back the IV state" in caplog.text


async def test_persist_durably_writes_at_once(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = known(5, path=path)
    state.seq = 4242
    await state.persist_durably()
    assert json.loads(path.read_text())["seq"] == 4242
    state.close()


# ----------------------------------------------------------------------------- confirmation and the way back


def test_the_proxys_beacon_back_confirms_it(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = known(5, path=path)
    state.start_iv_update(now=T0 + IV_UPDATE_MIN_STATE)
    assert not state.apply_beacon(
        5, False, now=T0 + IV_UPDATE_MIN_STATE + 1
    )  # the mesh still at 5: ignored
    assert not state.iv_update_confirmed
    # the proxy took it (§6.7: it beacons a change back): no change of the IV state, but persisted at once
    assert not state.apply_beacon(6, True, now=T0 + IV_UPDATE_MIN_STATE + 2)
    assert state.iv_update_confirmed
    assert json.loads(path.read_text())["iv_update_confirmed"] is True
    state.close()


def test_the_update_completes_96_hours_later_once_confirmed() -> None:
    started = T0 + IV_UPDATE_MIN_STATE
    state = known(5)
    state.start_iv_update(now=started)
    state.seq = 0x123456
    assert not state.iv_update_due(
        now=started + IV_UPDATE_MAX_STATE
    )  # never taken by the mesh: never
    assert not state.complete_iv_update(now=started + IV_UPDATE_MAX_STATE)
    # the 96 h count from when the mesh took it (review-5 P5-1)
    state.apply_beacon(6, True, now=started + 1)
    assert state.iv_update_confirmed_at == started + 1
    assert not state.iv_update_due(now=started + IV_UPDATE_MIN_STATE)
    assert state.iv_update_due(now=started + 1 + IV_UPDATE_MIN_STATE)
    assert state.complete_iv_update(now=started + 1 + IV_UPDATE_MIN_STATE)
    assert (state.iv_index, state.iv_update_active, state.tx_iv_index, state.seq) == (
        6,
        False,
        6,
        0,
    )
    assert state.seq_peak == 0x123456  # what a rewind would have to stay above
    assert state.iv_changed_at == started + 1 + IV_UPDATE_MIN_STATE
    assert not state.iv_update_due(now=started + IV_UPDATE_MAX_STATE)
    # the record of who started the last update stays for the diagnostics
    assert (state.iv_update_origin, state.iv_update_started_at) == (
        IV_ORIGIN_LOCAL,
        started,
    )


def test_an_update_a_beacon_started_is_left_to_the_beacons() -> None:
    state = known(5)
    assert state.apply_beacon(6, True, now=T0 + IV_UPDATE_MIN_STATE)
    assert (
        state.iv_update_origin,
        state.iv_update_started_at,
        state.iv_update_confirmed,
    ) == (
        IV_ORIGIN_BEACON,
        T0 + IV_UPDATE_MIN_STATE,
        True,
    )
    assert not state.iv_update_due(now=T0 + 10 * IV_UPDATE_MAX_STATE)


@pytest.mark.parametrize(
    "fields",
    [
        {"iv_update_origin": "gateway"},
        {"iv_update_origin": "local", "iv_update_confirmed": "yes"},
        {"iv_update_started_at": -1},
        {"iv_update_started_at": "now"},
    ],
)
def test_a_record_with_an_unusable_update_start_is_refused(
    fields: dict[str, Any],
) -> None:
    with pytest.raises((ValueError, TypeError)):
        LocalState.parse_record({"src": "0D00", "seq": 0, **fields})


def test_a_record_from_before_knows_no_update_start() -> None:
    assert LocalState.parse_iv_update({"src": "0D00", "seq": 0}) == (None, None, False)


# ----------------------------------------------------------------------------- the client


@pytest.fixture
def started_state(tmp_path: Path) -> Iterator[LocalState]:
    state = known(5, path=tmp_path / "state.json")
    yield state
    state.close()


@pytest.fixture
async def linked(
    cdb: CDB, started_state: LocalState, fast: FastAsyncio, clock: Clock
) -> tuple[ProxyClient, FakeBleak]:
    link = FakeBleak(cdb)
    link.iv_index = 5
    link.beacon_on_subscribe = True
    proxy = ProxyClient(cdb, started_state)
    await proxy.attach(link, beacon_wait=1.0)
    assert proxy.beacon_seen
    return proxy, link


def beacons(link: FakeBleak) -> list[tuple[int, bool]]:
    """The beacons the client wrote, as (IV index, IV Update flag), each checked to authenticate."""
    out = []
    for kind, payload in link.outgoing:
        if kind == PROXY_BEACON:
            b = parse_beacon(link.nk, payload)
            assert b is not None
            assert b.authenticated
            assert not b.key_refresh
            out.append((b.iv_index, b.iv_update))
    return out


async def settle(turns: int = 3) -> None:
    """Let the client's background work run a few loop turns (its sleeps are instant: `fast`)."""
    for _ in range(turns):
        await asyncio.sleep(0)


async def test_start_without_a_link_is_refused(
    cdb: CDB, started_state: LocalState
) -> None:
    proxy = ProxyClient(cdb, started_state)
    with pytest.raises(ConnectionError):
        await proxy.start_iv_update()
    assert started_state.iv_index == 5


async def test_start_during_a_followed_key_refresh_is_refused(
    linked: tuple[ProxyClient, FakeBleak], monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy, link = linked
    monkeypatch.setattr(ProxyClient, "key_refresh_phase", property(lambda _self: 2))
    with pytest.raises(IVUpdateRefused) as refused:
        await proxy.start_iv_update()
    assert refused.value.reason == "key_refresh"
    assert beacons(link) == []
    await proxy.detach()


async def test_start_needs_a_beacon_of_this_link_without_a_known_change(
    cdb: CDB, tmp_path: Path, fast: FastAsyncio, clock: Clock
) -> None:
    state = known(5, at=None, path=tmp_path / "state.json")
    link = FakeBleak(cdb)
    link.iv_index = 5
    proxy = ProxyClient(cdb, state)
    await proxy.attach(link)  # no beacon on this link
    with pytest.raises(IVUpdateRefused) as refused:
        await proxy.start_iv_update()
    assert refused.value.reason == "iv_unknown"
    link.send_beacon(iv_index=5)
    assert await proxy.start_iv_update() == 6
    await proxy.detach()
    state.close()


async def test_start_persists_then_beacons_and_repeats_until_the_proxy_takes_it(
    linked: tuple[ProxyClient, FakeBleak],
    started_state: LocalState,
    fast: FastAsyncio,
    caplog: pytest.LogCaptureFixture,
) -> None:
    proxy, link = linked
    assert started_state.path is not None
    on_disk: list[tuple[int, bool]] = []
    write = link.write_gatt_char

    async def write_and_look(
        char: str, data: bytes, response: bool | None = None
    ) -> None:
        if (
            data[0] & 0x3F == PROXY_BEACON
        ):  # what the store holds when a beacon goes out
            stored = json.loads(started_state.path.read_text())  # type: ignore[union-attr]
            on_disk.append((stored["iv_index"], stored["iv_update_active"]))
        await write(char, data, response)

    link.write_gatt_char = write_and_look  # type: ignore[method-assign]
    started_state.rpl = {
        0x0148: (4, 9),
        0x0149: (5, 9),
    }  # index 4 is not receivable in progress at 6
    fast.sleeps.clear()
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        assert await proxy.start_iv_update() == 6
    assert (
        "IV Update started: IV index 6 in progress (still transmitting with 5)"
        in caplog.text
    )
    assert beacons(link) == [(6, True)]
    assert on_disk == [(6, True)]  # on disk before it went
    assert started_state.rpl == {0x0149: (5, 9)}
    assert link.outgoing[-1][1] == secure_network_beacon(link.nk, 6, iv_update=True)
    # repeated every Beacon Interval until the proxy beacons it back
    await settle()
    assert beacons(link)[:2] == [(6, True), (6, True)]
    assert fast.sleeps[:2] == [IV_BEACON_INTERVAL, IV_BEACON_INTERVAL]
    link.send_beacon(iv_index=6, iv_update=True)
    assert started_state.iv_update_confirmed
    fast.sleeps.clear()
    await settle()
    assert set(fast.sleeps) == {IV_BEACON_INTERVAL_MAX}
    # traffic still goes under the old index
    link.send_beacon(iv_index=6, iv_update=True)
    assert started_state.tx_iv_index == 5
    await proxy.detach()
    assert proxy._tasks == set()


async def test_start_whose_store_did_not_take_it_sends_nothing(
    linked: tuple[ProxyClient, FakeBleak],
    started_state: LocalState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy, link = linked

    async def not_written() -> None:
        raise OSError("the store did not take it")

    monkeypatch.setattr(started_state, "persist_durably", not_written)
    with pytest.raises(OSError, match="did not take it"):
        await proxy.start_iv_update()
    assert (started_state.iv_index, started_state.iv_update_active) == (5, False)
    assert beacons(link) == []
    await proxy.detach()


async def test_a_link_lost_before_the_beacon_leaves_it_to_the_next_link(
    linked: tuple[ProxyClient, FakeBleak], started_state: LocalState, cdb: CDB
) -> None:
    proxy, link = linked
    link.write_error = OSError("link gone")
    assert await proxy.start_iv_update() == 6
    await settle()
    assert beacons(link) == []
    assert proxy._tasks == set()  # the repeat gave up with the link
    assert (started_state.iv_index, started_state.iv_update_active) == (6, True)
    await proxy.detach()
    # the next link gets the beacon at once
    other = FakeBleak(cdb)
    other.iv_index = 5
    await proxy.attach(other)
    await settle(1)
    assert beacons(other)[:1] == [(6, True)]
    proxy._start_iv_task(sent=True)  # one per link: a second start is a no-op
    assert len([t for t in proxy._tasks if t is proxy._iv_task]) == 1
    await proxy.detach()


async def test_the_update_returns_to_normal_96_hours_later(
    linked: tuple[ProxyClient, FakeBleak],
    started_state: LocalState,
    clock: Clock,
    fast: FastAsyncio,
    caplog: pytest.LogCaptureFixture,
) -> None:
    proxy, link = linked
    await proxy.start_iv_update()
    link.send_beacon(iv_index=6, iv_update=True)
    started_state.seq = 0x200
    # a segmented message of ours awaits its acknowledgment: the return waits for it (§3.11.5)
    proxy._ack_waiters[(0x0148, 1)] = _AckState()
    clock.now += IV_UPDATE_MIN_STATE
    fast.sleeps.clear()
    await settle()
    assert started_state.iv_update_active
    assert client_mod.SEGMENT_ACK_TIMEOUT in fast.sleeps
    del proxy._ack_waiters[(0x0148, 1)]
    link.iv_index = 6
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        await settle(5)
    assert "IV Update completed: back to Normal Operation" in caplog.text
    assert (
        started_state.iv_index,
        started_state.iv_update_active,
        started_state.tx_iv_index,
    ) == (
        6,
        False,
        6,
    )
    assert beacons(link)[-1] == (6, False)
    # the filter goes again under the new transmit index, whose sequence restarted at 0
    assert link.config_pdus[-1].iv_index == 6
    assert started_state.seq == 1
    assert proxy._iv_task is not None
    assert proxy._iv_task.done()
    await proxy.detach()


async def test_a_beacon_of_the_mesh_may_end_it_first(
    linked: tuple[ProxyClient, FakeBleak], started_state: LocalState, clock: Clock
) -> None:
    proxy, link = linked
    await proxy.start_iv_update()
    started = clock.now
    link.send_beacon(iv_index=6, iv_update=True)
    await settle()
    assert started_state.apply_beacon(6, False, now=started + IV_UPDATE_MIN_STATE)
    await settle()
    assert proxy._iv_task is not None
    assert proxy._iv_task.done()
    await proxy.detach()


async def test_the_completion_beacon_lost_with_the_link_is_only_logged(
    linked: tuple[ProxyClient, FakeBleak], started_state: LocalState, clock: Clock
) -> None:
    proxy, link = linked
    await proxy.start_iv_update()
    link.send_beacon(iv_index=6, iv_update=True)
    await settle()
    clock.now += IV_UPDATE_MIN_STATE
    link.write_error = OSError("link gone")
    await settle(5)
    assert not started_state.iv_update_active  # completed all the same
    await proxy.detach()


# ----------------------------------------------------------------------------- review 5: the follow-ups


def test_a_late_confirmation_waits_96_hours_from_itself() -> None:
    """Review-5 P5-1: the proxy took it 95 h after the start (refusing it within its own 96 h first): 96 more."""
    started = T0 + IV_UPDATE_MIN_STATE
    state = known(5)
    state.start_iv_update(now=started)
    state.apply_beacon(6, True, now=started + 95 * HOUR)
    assert state.iv_update_confirmed_at == started + 95 * HOUR
    assert not state.iv_update_due(now=started + IV_UPDATE_MIN_STATE)
    assert not state.complete_iv_update(now=started + 190 * HOUR)
    assert state.complete_iv_update(now=started + 191 * HOUR)
    # an update a beacon started was taken when it started
    beaconed = known(5)
    beaconed.apply_beacon(6, True, now=started)
    assert beaconed.iv_update_confirmed_at == started


def test_a_record_confirmed_before_the_time_was_kept_counts_from_its_start(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    record = {
        "src": f"{OUR_SRC:04X}",
        "seq": 10,
        "iv_index": 6,
        "iv_update_active": True,
        "iv_known": True,
        "iv_changed_at": T0,
        "iv_update_origin": "local",
        "iv_update_started_at": T0,
        "iv_update_confirmed": True,
    }
    path.write_text(json.dumps(record))
    state = LocalState(path, OUR_SRC)
    assert state.iv_update_confirmed_at == T0  # what that version counted from
    assert state.iv_update_due(now=T0 + IV_UPDATE_MIN_STATE)
    state.close()
    # unconfirmed: nothing to count from
    path.write_text(json.dumps({**record, "iv_update_confirmed": False}))
    state = LocalState(path, OUR_SRC)
    assert state.iv_update_confirmed_at is None
    state.close()


@pytest.mark.parametrize(
    "fields",
    [
        {"iv_update_abandoned": "lost"},
        {"iv_update_confirmed_at": -1},
        {"mesh_iv_changed_at": "yesterday"},
    ],
)
def test_a_record_with_an_unusable_update_outcome_is_refused(
    fields: dict[str, Any],
) -> None:
    with pytest.raises((ValueError, TypeError)):
        LocalState.parse_record({"src": "0D00", "seq": 0, **fields})


def test_the_outcome_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = known(5, path=path)
    state.apply_beacon(5, False, now=T0)
    state.start_iv_update(now=T0 + IV_UPDATE_MIN_STATE)
    state.apply_beacon(6, True, now=T0 + IV_UPDATE_MIN_STATE + 1)
    state.persist()
    state.close()
    again = LocalState(path, OUR_SRC)
    assert again.iv_update_confirmed_at == T0 + IV_UPDATE_MIN_STATE + 1
    assert again.mesh_iv_changed_at == T0 + IV_UPDATE_MIN_STATE + 1
    again.close()


def test_the_meshs_last_iv_change_is_a_beacon_ahead_of_the_last() -> None:
    """The mesh's last IV change seen in a beacon, which the refusals name (review-5 P5 improvement)."""
    state = known(5, at=None)
    state.apply_beacon(5, False, now=T0)  # where the mesh is: no change
    assert state.mesh_iv_changed_at is None
    state.apply_beacon(4, False, now=T0 + 1)  # a node behind: no change either
    state.apply_beacon(5, False, now=T0 + 2)
    assert state.mesh_iv_changed_at is None
    # a change this state does not follow (too early after its own start) is the mesh's all the same
    state.start_iv_update(now=T0 + 3, index_confirmed=True)
    state.apply_beacon(6, True, now=T0 + 4)
    assert state.mesh_iv_changed_at == T0 + 4
    state.apply_beacon(6, True, now=T0 + 5)  # the same again
    assert state.mesh_iv_changed_at == T0 + 4
    # out of reach (more than 42 ahead): not noted
    state.apply_beacon(100, False, now=T0 + 6)
    assert state.mesh_iv_changed_at == T0 + 4


def test_the_mesh_index_is_the_old_one_while_ours_waits() -> None:
    """Review-5 S5-3: what `sequence_space_low` judges while the mesh has not taken our update."""
    state = known(5)
    assert state.mesh_iv_index == 5
    state.start_iv_update(now=T0 + IV_UPDATE_MIN_STATE)
    assert state.mesh_iv_index == 5
    state.apply_beacon(6, True, now=T0 + IV_UPDATE_MIN_STATE + 1)
    assert state.mesh_iv_index == 6


def test_an_update_the_mesh_does_not_take_is_overdue_after_144_hours() -> None:
    """Review-5 P5-3: §3.11.5's bound on IV Update in Progress."""
    started = T0 + IV_UPDATE_MIN_STATE
    state = known(5)
    assert not state.iv_update_overdue(now=started)
    state.start_iv_update(now=started)
    assert not state.iv_update_overdue(now=started + IV_UPDATE_MAX_STATE - 1)
    assert state.iv_update_overdue(now=started + IV_UPDATE_MAX_STATE)
    state.apply_beacon(6, True, now=started + IV_UPDATE_MAX_STATE)  # taken after all
    assert not state.iv_update_overdue(now=started + 10 * IV_UPDATE_MAX_STATE)


def test_giving_up_goes_back_to_the_old_index_and_keeps_counting(
    tmp_path: Path,
) -> None:
    """Nothing was sent under the new index: the sequence carries on under the old one, no nonce twice."""
    path = tmp_path / "state.json"
    started = T0 + IV_UPDATE_MIN_STATE
    state = known(5, path=path)
    state.start_iv_update(now=started)
    sent = {(state.tx_iv_index, state.next_seq()) for _ in range(5)}
    assert state.abandon_iv_update(state_mod.IV_ABANDONED_NOT_TAKEN)
    assert (state.iv_index, state.iv_update_active, state.tx_iv_index) == (5, False, 5)
    assert state.iv_update_abandoned == state_mod.IV_ABANDONED_NOT_TAKEN
    assert state.iv_changed_at is None  # a change the mesh never made is not kept
    assert not {(state.tx_iv_index, state.next_seq()) for _ in range(5)} & sent
    stored = json.loads(path.read_text())
    assert (stored["iv_index"], stored["iv_update_abandoned"]) == (5, "not_taken")
    assert "iv_changed_at" not in stored
    # nothing left to give up
    assert not state.abandon_iv_update(state_mod.IV_ABANDONED_NOT_TAKEN)
    # a new start needs the index from a beacon of the link, and clears the outcome
    with pytest.raises(IVUpdateRefused, match="confirmed by a beacon"):
        state.start_iv_update(now=started + 1)
    state.start_iv_update(now=started + 1, index_confirmed=True)
    assert state.iv_update_abandoned is None
    state.close()


def test_a_beacon_of_the_mesh_clears_the_outcome() -> None:
    state = known(5)
    state.start_iv_update(now=T0 + IV_UPDATE_MIN_STATE)
    state.abandon_iv_update(state_mod.IV_ABANDONED_NOT_TAKEN)
    state.apply_beacon(5, False, now=T0 + IV_UPDATE_MAX_STATE)  # no move: kept
    assert state.iv_update_abandoned == state_mod.IV_ABANDONED_NOT_TAKEN
    assert state.apply_beacon(6, True, now=T0 + IV_UPDATE_MAX_STATE)  # the mesh's own
    assert state.iv_update_abandoned is None


def test_one_confirmed_or_started_by_a_beacon_is_not_given_up() -> None:
    state = known(5)
    state.start_iv_update(now=T0 + IV_UPDATE_MIN_STATE)
    state.apply_beacon(6, True, now=T0 + IV_UPDATE_MIN_STATE + 1)
    assert not state.abandon_iv_update(state_mod.IV_ABANDONED_NOT_TAKEN)
    beaconed = known(5)
    beaconed.apply_beacon(6, True, now=T0 + IV_UPDATE_MIN_STATE)
    assert not beaconed.abandon_iv_update(state_mod.IV_ABANDONED_ABORTED)
    assert beaconed.iv_update_active


def test_abort_is_refused_unless_ours_waits_and_a_beacon_said_where_the_mesh_is() -> (
    None
):
    state = known(5)
    with pytest.raises(IVUpdateRefused) as refused:
        state.abort_iv_update(index_confirmed=True)
    assert refused.value.reason == "not_started"
    state.start_iv_update(now=T0 + IV_UPDATE_MIN_STATE)
    with pytest.raises(IVUpdateRefused) as refused:
        state.abort_iv_update(index_confirmed=False)
    assert refused.value.reason == "iv_unknown"
    assert state.iv_update_active
    assert state.abort_iv_update(index_confirmed=True) == 5
    assert state.iv_update_abandoned == state_mod.IV_ABANDONED_ABORTED
    # taken by the mesh: no way back (§3.11.5)
    state.start_iv_update(now=T0 + IV_UPDATE_MIN_STATE + 1, index_confirmed=True)
    state.apply_beacon(6, True, now=T0 + IV_UPDATE_MIN_STATE + 2)
    with pytest.raises(IVUpdateRefused) as refused:
        state.abort_iv_update(index_confirmed=True)
    assert refused.value.reason == "taken"
    assert "IV index 6" in str(refused.value)


def restored_mid_update(path: Path) -> LocalState:
    """A record restored from a backup taken during our own IV Update 5 → 6 (confirmed), as the integration
    rewrites one (`seq_store._async_skip_restored_record`): far ahead, the guard pending the first beacon."""
    path.write_text(
        json.dumps(
            {
                "src": f"{OUR_SRC:04X}",
                "seq": 1_050_088,
                "iv_index": 6,
                "iv_update_active": True,
                "iv_known": True,
                "iv_changed_at": T0,
                "iv_update_origin": "local",
                "iv_update_started_at": T0,
                "iv_update_confirmed": True,
                "iv_update_confirmed_at": T0,
                "seq_guard": state_mod.SEQ_GUARD_FIRST_BEACON,
            }
        )
    )
    return LocalState(path, OUR_SRC)


def test_a_restored_record_mid_update_does_not_complete_by_the_clock(
    tmp_path: Path,
) -> None:
    """Review-5 S5-2: since the backup, Home Assistant completed the update and sent numbers under 6 from 0 on; the
    restored record completing it by the clock restarted the counter at 0 there. Only a beacon may move it on."""
    state = restored_mid_update(tmp_path / "state.json")
    later = T0 + 30 * 24 * HOUR
    assert not state.iv_update_due(now=later)
    assert not state.complete_iv_update(now=later)
    assert (state.tx_iv_index, state.seq_guard) == (5, state_mod.SEQ_GUARD_FIRST_BEACON)
    # the rule itself, whatever the caller: no move of the transmit index forward past a pending guard
    assert not state._set_iv_state(6, False, later)
    assert state.tx_iv_index == 5
    # the mesh's beacon names the index: the counter carries on under it
    seq = state.seq
    assert state.apply_beacon(6, False, now=later)
    assert (state.tx_iv_index, state.seq, state.seq_guard) == (6, seq, 7)
    state.close()


def test_a_restored_record_mid_update_completes_after_a_beacon_names_the_index(
    tmp_path: Path,
) -> None:
    """The mesh still in progress at 6: its beacon raises the guard to 7, and the completion then keeps counting."""
    state = restored_mid_update(tmp_path / "state.json")
    later = T0 + IV_UPDATE_MIN_STATE
    assert not state.apply_beacon(
        6, True, now=later
    )  # the same state: only the guard moves
    assert state.seq_guard == 7
    seq = state.seq
    assert state.complete_iv_update(now=later)
    assert (state.tx_iv_index, state.seq) == (6, seq)
    state.close()


async def test_every_beacon_carries_the_key_refresh_phase(
    linked: tuple[ProxyClient, FakeBleak], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review-5 P5-2: a key refresh that starts during the update; Phase 2 beacons are under the new key with the
    flag (§3.10.3) — one under the new key without it tells a node in Phase 1 or 2 that the refresh is over."""
    proxy, link = linked
    await proxy.start_iv_update()
    new = NetKeyMaterial.derive(bytes(range(16)))
    phase = [1]
    monkeypatch.setattr(ProxyClient, "key_refresh_phase", property(lambda _s: phase[0]))
    monkeypatch.setattr(
        ProxyClient,
        "nk",
        property(lambda _s: new if phase[0] == 2 else link.nk),
    )
    link.outgoing.clear()
    await proxy._send_iv_beacon()  # Phase 1: the old key, no flag
    old_key = parse_beacon(link.nk, link.outgoing[-1][1])
    assert old_key is not None
    assert (old_key.authenticated, old_key.key_refresh) == (True, False)
    phase[0] = 2
    await proxy._send_iv_beacon()  # Phase 2: the new key, the flag
    new_key = parse_beacon(new, link.outgoing[-1][1])
    assert new_key is not None
    assert (new_key.authenticated, new_key.key_refresh, new_key.iv_update) == (
        True,
        True,
        True,
    )
    await proxy.detach()


async def test_an_update_not_taken_is_given_up_after_144_hours(
    linked: tuple[ProxyClient, FakeBleak],
    started_state: LocalState,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review-5 P5-3: the proxy keeps beaconing the old index; 144 h on the update is given up and the caller told."""
    proxy, _link = linked
    told: list[int] = []
    proxy.on_iv_update_abandoned = lambda: told.append(started_state.iv_index)
    await proxy.start_iv_update()
    clock.now += IV_UPDATE_MAX_STATE - 1
    await settle()
    assert started_state.iv_update_active  # not yet
    clock.now += 1
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        await settle()
    assert "IV Update to IV index 6 given up" in caplog.text
    assert (started_state.iv_index, started_state.iv_update_active) == (5, False)
    assert started_state.iv_update_abandoned == state_mod.IV_ABANDONED_NOT_TAKEN
    assert told == [5]
    assert proxy._iv_task is not None
    assert proxy._iv_task.done()
    await proxy.detach()


async def test_an_overdue_update_waits_for_a_beacon_of_the_link(
    cdb: CDB, tmp_path: Path, fast: FastAsyncio, clock: Clock
) -> None:
    """Without a beacon of this link nothing says the mesh is still at the old index: it is not given up; no one
    is told when nobody asked to be."""
    state = known(5, path=tmp_path / "state.json")
    state.start_iv_update(now=clock.now)
    link = FakeBleak(cdb)
    link.iv_index = 5
    proxy = ProxyClient(cdb, state)
    clock.now += IV_UPDATE_MAX_STATE
    await proxy.attach(link)
    await settle()
    assert state.iv_update_active
    link.send_beacon(iv_index=5)
    await settle()
    assert not state.iv_update_active
    await proxy.detach()
    state.close()


async def test_abort_needs_a_link(cdb: CDB, started_state: LocalState) -> None:
    proxy = ProxyClient(cdb, started_state)
    with pytest.raises(ConnectionError):
        proxy.abort_iv_update()


async def test_abort_goes_back_and_the_beacons_stop(
    linked: tuple[ProxyClient, FakeBleak],
    started_state: LocalState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    proxy, link = linked
    await proxy.start_iv_update()
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        assert proxy.abort_iv_update() == 5
    assert "IV Update aborted: back to IV index 5" in caplog.text
    assert started_state.iv_update_abandoned == state_mod.IV_ABANDONED_ABORTED
    await settle()
    assert proxy._iv_task is not None
    assert proxy._iv_task.done()
    sent = len(beacons(link))
    await settle()
    assert len(beacons(link)) == sent
    await proxy.detach()


def test_a_recovery_into_the_meshs_next_update_is_the_meshs() -> None:
    """Our update to 6 was taken; the link came back days later with the mesh in its own update to 7. The IV Index
    Recovery lands in that one: it is the mesh's, and our old confirmation must not complete it at once (found by
    the initiator state machine)."""
    started = T0 + IV_UPDATE_MIN_STATE
    state = known(5)
    state.start_iv_update(now=started)
    state.apply_beacon(6, True, now=started)
    later = started + 200 * HOUR
    assert state.apply_beacon(7, True, now=later)
    assert (state.iv_index, state.iv_update_active, state.tx_iv_index) == (7, True, 6)
    assert (state.iv_update_origin, state.iv_update_confirmed_at) == (
        IV_ORIGIN_BEACON,
        later,
    )
    assert not state.iv_update_due(now=later + IV_UPDATE_MAX_STATE)
