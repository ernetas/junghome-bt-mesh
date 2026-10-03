"""jhmesh.client.LocalState on a file: locking, atomic writes, torn files and the backup copy, the replay list."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import stat
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from jhmesh import client as client_mod
from jhmesh.client import LocalState, ProxyClient, StateInUse
from jhmesh.keyrefresh import KeyRefreshRecord

from .conftest import LIGHT_2G, OUR_SRC, PROXY_NODE, FakeBleak

if TYPE_CHECKING:
    from jhmesh.cdb import CDB

h = bytes.fromhex
ONOFF_STATUS_ON = h("820401")


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


def read(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text()))


# ----------------------------------------------------------------------------- locking


def test_second_instance_on_the_same_file_is_refused(state_file: Path):
    """P1-3: two processes sharing one counter would each take the same sequence numbers (one nonce, two
    plaintexts). The lock is exclusive for the life of the object; `close()` releases it."""
    first = LocalState(state_file, OUR_SRC)
    first.next_seq()
    lock = state_file.with_suffix(".lock")
    assert lock.read_text().strip() == str(os.getpid())
    with pytest.raises(
        StateInUse,
        match=rf"state.json is in use by another process \(pid {os.getpid()}\)",
    ):
        LocalState(state_file, OUR_SRC)
    assert read(state_file)["seq"] == 1  # the refused instance wrote nothing
    assert first.seq == 1
    other = LocalState(
        state_file.with_name("other.json"), 0x0D02
    )  # another address: its own file, no conflict
    other.close()
    first.close()
    second = LocalState(
        state_file, OUR_SRC
    )  # released: continues the counter with the margin
    assert second.seq == 1 + 512
    second.close()
    second.close()  # idempotent


def test_lock_held_by_another_process_is_reported_without_a_pid_when_it_left_none(
    state_file: Path,
):
    lock = state_file.with_suffix(".lock")
    lock.write_text("")
    with lock.open("r+") as f:  # stands in for the other process
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(StateInUse, match=r"another process: two clients"):
            LocalState(state_file, OUR_SRC)
    s = LocalState(state_file, OUR_SRC)  # the other process is gone
    s.close()


def test_without_fcntl_the_state_warns_once_and_carries_on(
    state_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr(client_mod, "fcntl", None)
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        s = LocalState(state_file, OUR_SRC)
    assert "cannot lock" in caplog.text
    assert not state_file.with_suffix(".lock").exists()
    t = LocalState(state_file, OUR_SRC)  # nothing stops a second one on such a platform
    s.close()
    t.close()


def test_state_without_a_path_needs_no_lock_backup_or_flush():
    s = LocalState(None, OUR_SRC)
    s.note_received(PROXY_NODE, 0, 5)
    s.flush()
    s.purge_rpl(0)
    s.close()
    assert s.rpl == {PROXY_NODE: (0, 5)}


# ----------------------------------------------------------------------------- atomic writes, torn files, backup


def test_persist_writes_a_temporary_file_and_replaces_the_state_atomically(
    state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """P2-5: `write_text` truncates first — a crash in between left an empty file that made every later start die.
    The state goes to a temporary sibling and is renamed over the old file, so a failure leaves the previous state
    intact."""
    s = LocalState(state_file, OUR_SRC)
    s.seq = 5
    s.persist()
    replaced: list[tuple[str, str]] = []
    real_replace = Path.replace

    def spy(self: Path, target: Path) -> Path:
        replaced.append((self.name, Path(target).name))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy)
    s.seq = 6
    s.persist()
    assert replaced == [
        (f".state.json.{os.getpid()}.{threading.get_ident()}.tmp", "state.json")
    ]
    assert read(state_file)["seq"] == 6
    monkeypatch.undo()

    def crash(fd: int) -> None:
        raise OSError("disk full")  # the new content never reached the disk

    monkeypatch.setattr(os, "fsync", crash)
    s.seq = 7
    with pytest.raises(OSError, match="disk full"):
        s.persist()
    monkeypatch.undo()
    assert read(state_file)["seq"] == 6  # the old file is untouched
    assert not list(state_file.parent.glob(".*.tmp"))  # and no temporary file is left
    s.close()


def test_the_state_and_its_backup_are_owner_only_under_a_loose_umask(
    state_file: Path, caplog: pytest.LogCaptureFixture
):
    """Brief 02: the record holds the new NetKey while a key refresh is followed (`to_stored`); the state file and
    its `.bak` were written with the umask's mode — 0644, world-readable — and are 0600 now."""
    old_umask = os.umask(0o022)
    try:
        s = LocalState(state_file, OUR_SRC)
        s.set_key_refresh(KeyRefreshRecord(bytes(range(16)), 1))
        s._backup(force=True)
        s.close()
        bak = state_file.with_suffix(".bak")
        assert "key_refresh" in read(state_file)
        assert "key_refresh" in read(bak)
        for path in (state_file, bak):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name
        # a file an older version left world-readable is made owner-only on load, logged once
        state_file.chmod(0o644)
        with caplog.at_level(logging.INFO, logger="jhmesh"):
            LocalState(state_file, OUR_SRC, restart_margin=0).close()
            assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
            # the backup copy too, when the load gets to it (a torn state file)
            state_file.write_text("torn")
            bak.chmod(0o640)
            LocalState(state_file, OUR_SRC, restart_margin=0).close()
            assert stat.S_IMODE(bak.stat().st_mode) == 0o600
            LocalState(
                state_file, OUR_SRC, restart_margin=0
            ).close()  # nothing left to tighten
        tightened = [r for r in caplog.records if "owner-only" in r.getMessage()]
        assert [(r.levelno, r.args[0]) for r in tightened] == [  # type: ignore[index]
            (logging.INFO, state_file),
            (logging.INFO, bak),
        ]
        assert "000102030405" not in caplog.text
    finally:
        os.umask(old_umask)


def test_a_state_file_that_cannot_be_tightened_is_still_loaded(
    state_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """Not the file's owner (a state directory shared between users): the chmod fails with a WARNING, the
    counter still loads — refusing to start would not make the file any less readable."""
    s = LocalState(state_file, OUR_SRC, restart_margin=0)
    s.seq = 42
    s.persist()
    s.close()
    state_file.chmod(0o644)

    real_chmod = Path.chmod

    def refuse(self: Path, mode: int) -> None:
        if self == state_file:
            raise PermissionError("not the owner")
        real_chmod(self, mode)

    monkeypatch.setattr(Path, "chmod", refuse)
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        again = LocalState(state_file, OUR_SRC, restart_margin=0)
    assert again.seq == 42
    assert "could not make" in caplog.text
    again.close()


def test_persist_fsyncs_the_temporary_file_before_the_rename(
    state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """Without an fsync, a crash right after the rename can still leave the new name pointing at data the
    filesystem never wrote out — the atomic rename alone only rules out a *torn* file, not a lost one."""
    s = LocalState(state_file, OUR_SRC)
    synced: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    s.seq = 9
    s.persist()
    assert (
        synced
    )  # fsync ran at least once, before `.tmp` was renamed over the state file
    assert read(state_file)["seq"] == 9
    s.close()


def test_torn_state_file_falls_back_to_the_backup_copy(
    state_file: Path, caplog: pytest.LogCaptureFixture
):
    """A torn / unparsable state file (an older version's truncate-and-write, a disk fault) is not a crash: the
    `.bak` copy carries the counter, and the margin covers what the copy lags."""
    s = LocalState(state_file, OUR_SRC, restart_margin=0)
    s.seq = 100
    s.persist()
    s.note_received(PROXY_NODE, 0, 7)
    s.close()
    bak = state_file.with_suffix(".bak")
    assert (
        read(bak)["seq"] == 0
    )  # copied when the state was created; refreshed every BACKUP_EVERY numbers
    bak_text = (
        bak.read_text()
    )  # each reopen below now rewrites `.bak` too: reset it every time
    for torn in ("", '{"src": "0D00", "se', "[1, 2]", '{"seq": 5}'):
        bak.write_text(bak_text)
        state_file.write_text(torn)
        with caplog.at_level(logging.WARNING, logger="jhmesh"):
            t = LocalState(state_file, OUR_SRC)
        assert t.seq == 0 + 512, torn
        assert "trying the backup copy" in caplog.text
        assert f"sequence state restored from {bak}" in caplog.text
        assert read(state_file)["seq"] == 512  # rewritten valid at once
        t.close()
        caplog.clear()


def test_backup_copy_follows_the_counter_across_restarts(
    state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """CLI-02: `_restore` used to claim `.bak` held the margin-bumped counter without writing it, so every
    restart's jump was invisible to the copy — it kept whatever an earlier run had left, arbitrarily far
    behind the numbers actually used on air. (No real flushes: the torn file below is a process crash.)"""
    monkeypatch.setattr(os, "fsync", lambda _fd: None)
    highest_used = 0
    for _ in range(4):
        s = LocalState(state_file, OUR_SRC)
        for _ in range(200):
            highest_used = max(highest_used, s.next_seq())
        s.close()
    state_file.write_text("{torn")
    t = LocalState(state_file, OUR_SRC)
    assert t.seq > highest_used
    t.close()


def test_backup_copy_follows_an_iv_update(state_file: Path):
    """CLI-02: `apply_beacon` resets `seq` to 0 on a new transmit index, which used to make the lag negative
    and leave `.bak` on the old IV index until the counter caught up — potentially for the life of the index."""
    bak = state_file.with_suffix(".bak")
    s = LocalState(state_file, OUR_SRC)
    s.seq = 5000
    s.persist()
    s._backup_seq = -(
        10**9
    )  # force a write regardless of force=, unlike a real `force=True` call
    s._backup()
    s.apply_beacon(1, True)
    s.apply_beacon(1, False)
    for _ in range(100):
        s.next_seq()
    s.close()
    assert read(bak)["iv_index"] == 1
    state_file.write_text("{torn")
    t = LocalState(state_file, OUR_SRC)
    assert (t.tx_iv_index, t.seq) > (1, 99)
    t.close()


def test_backup_copy_is_left_alone_when_nothing_can_be_restored(
    state_file: Path,
):
    """CLI-02: when no candidate parses at all, the forced startup backup must not stomp an existing `.bak`
    with the seq-0 fallback state — it may be the only copy of a counter worth keeping."""
    bak = state_file.with_suffix(".bak")
    bak_record = {"src": "0D00", "seq": 9000, "iv_index": 7, "rpl": {"0148": "x"}}
    bak.write_text(
        json.dumps(bak_record)
    )  # itself unusable (bad rpl): nothing can be restored from either file
    state_file.write_text(json.dumps({"src": "0D00", "seq": None, "iv_index": 7}))
    s = LocalState(state_file, OUR_SRC)
    assert s.seq == 0  # nothing usable: fresh start
    assert (
        read(bak) == bak_record
    )  # left byte-for-byte alone, not overwritten with the seq-0 state
    s.close()


def test_backup_copy_follows_the_counter_every_256_numbers(state_file: Path):
    s = LocalState(state_file, OUR_SRC, restart_margin=0)
    bak = state_file.with_suffix(".bak")
    s.reserve_seq(255)
    assert read(bak)["seq"] == 0
    s.next_seq()  # 256 past the copy
    assert read(bak)["seq"] == 256
    s.reserve_seq(300)  # 556: 300 past the copy
    assert read(bak)["seq"] == 556
    s.close()
    state_file.write_text(
        ""
    )  # torn: the copy plus the margin is still ahead of every number ever used
    t = LocalState(state_file, OUR_SRC)
    assert t.seq == 556 + 512
    t.close()


def test_state_file_and_backup_both_unreadable_starts_at_zero_with_a_warning(
    state_file: Path, caplog: pytest.LogCaptureFixture
):
    state_file.write_text("{")
    state_file.with_suffix(".bak").write_text("{")
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        s = LocalState(state_file, OUR_SRC)
    assert s.seq == 0
    assert caplog.text.count("is unreadable") == 2
    assert read(state_file)["seq"] == 0
    s.close()


def test_record_with_unusable_values_starts_at_zero_with_a_warning(
    state_file: Path, caplog: pytest.LogCaptureFixture
):
    state_file.write_text(json.dumps({"src": "zz", "seq": 100}))
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        s = LocalState(state_file, OUR_SRC)
    assert (s.src, s.seq) == (OUR_SRC, 0)
    assert "stored sequence state is unusable" in caplog.text
    assert "use another source address" in caplog.text
    s.close()


def test_restore_of_a_subclass_provided_record_with_bad_values_starts_at_zero(
    caplog: pytest.LogCaptureFixture,
):
    """A subclass (`HAState`, from HA's own storage) may override `load()` to hand back an already-parsed
    record without going through `LocalState.load()`'s own validation — `_restore` must still refuse a bad
    value itself rather than partially assigning `self` and only then raising."""

    class RawRecordState(LocalState):
        def load(self) -> dict[str, Any]:
            return {"src": "0D00", "seq": None, "iv_index": 7}

    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        s = RawRecordState(None, OUR_SRC)
    assert (s.src, s.seq, s.iv_index) == (OUR_SRC, 0, 0)
    assert "stored sequence state is unusable" in caplog.text


def test_unusable_state_values_fall_back_to_the_backup_copy(
    state_file: Path, caplog: pytest.LogCaptureFixture
):
    """CLI-03: a record that parses as JSON and has both required keys, but a bad value (`seq: null`), used to
    be adopted as it was instead of falling back to a good `.bak` — partly assigned (`iv_index`) before the
    bad `seq` conversion raised, so the object ended up transmitting at seq 0 under the stored SRC and IV."""
    bak = state_file.with_suffix(".bak")
    s = LocalState(state_file, OUR_SRC)
    s.seq = 9000
    s.iv_index = 7
    s.persist()
    s._backup_seq = -(10**9)
    s._backup()
    s.close()
    state_file.write_text(json.dumps({"src": "0D00", "seq": None, "iv_index": 7}))
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        t = LocalState(state_file, OUR_SRC)
    assert t.seq == 9000 + 512
    assert t.iv_index == 7
    assert f"sequence state restored from {bak}" in caplog.text
    t.close()


def test_unusable_rpl_falls_back_to_the_backup_copy(state_file: Path):
    """CLI-03's second case: a malformed `rpl` entry must fall back too, never leave the restored counter in
    place while claiming (or silently) starting at 0."""
    s = LocalState(state_file, OUR_SRC)
    s.seq = 9000
    s.iv_index = 7
    s.persist()
    s._backup_seq = -(10**9)
    s._backup()
    s.close()
    state_file.write_text(
        json.dumps({"src": "0D00", "seq": 1, "iv_index": 7, "rpl": {"0148": "x"}})
    )
    t = LocalState(state_file, OUR_SRC)
    assert t.seq == 9000 + 512
    t.close()


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("seq", -1, "sequence number -1 is outside 0x0..0xffffff"),
        ("seq", 0x1000000, "sequence number 16777216 is outside"),
        ("iv_index", 1 << 32, "IV index 4294967296 is outside 0x0..0xffffffff"),
        ("iv_index", -1, "IV index -1 is outside"),
        ("src", "8000", "source address 32768 is outside 0x1..0x7fff"),
        ("src", "0000", "source address 0 is outside"),
        ("rpl", {"C001": [0, 1]}, "replay-list source 49153 is outside"),
        ("rpl", {"0148": [-1, 1]}, "replay-list IV index -1 is outside"),
        (
            "rpl",
            {"0148": [0, 1 << 24]},
            "replay-list sequence number 16777216 is outside",
        ),
    ],
)
def test_out_of_range_state_values_fall_back_to_the_backup_copy(
    state_file: Path,
    caplog: pytest.LogCaptureFixture,
    field: str,
    value: Any,
    error: str,
):
    """A value that converts but does not fit its field (a hand edit, a corrupted file) is unusable like a
    torn record: adopted, it would make every send raise OverflowError, so `.bak` is used instead."""
    s = LocalState(state_file, OUR_SRC)
    s.seq = 9000
    s.persist()
    s._backup_seq = -(10**9)
    s._backup()
    s.close()
    record: dict[str, Any] = {"src": "0D00", "seq": 1, "iv_index": 0}
    record[field] = value
    state_file.write_text(json.dumps(record))
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        t = LocalState(state_file, OUR_SRC)
    assert error in caplog.text
    assert t.seq == 9000 + 512
    t.next_seq()  # and it can send
    t.close()


# ----------------------------------------------------------------------------- replay list persistence


def test_replay_list_round_trips_through_the_state_file(state_file: Path):
    """§3.8.8: the list must survive a power cycle, or one captured PDU per source is accepted again."""
    s = LocalState(state_file, OUR_SRC)
    s.note_received(PROXY_NODE, 0, 0x010005)
    s.note_received(0x0232, 1, 3)
    s.persist()
    assert read(state_file)["rpl"] == {"0148": [0, 0x010005], "0232": [1, 3]}
    s.close()
    t = LocalState(state_file, OUR_SRC)
    assert t.rpl == {PROXY_NODE: (0, 0x010005), 0x0232: (1, 3)}
    assert t.to_stored()["rpl"] == {"0148": [0, 0x010005], "0232": [1, 3]}
    t.close()


def test_note_received_never_lowers_an_entry_and_flushes_in_batches(state_file: Path):
    s = LocalState(state_file, OUR_SRC)
    s.note_received(PROXY_NODE, 0, 10)
    s.note_received(PROXY_NODE, 0, 5)
    s.note_received(PROXY_NODE, 0, 10)
    assert s.rpl == {PROXY_NODE: (0, 10)}
    assert (
        read(state_file)["rpl"] == {}
    )  # one update: not written yet (a listener would rewrite per message)
    s.flush()
    assert read(state_file)["rpl"] == {"0148": [0, 10]}
    for i in range(LocalState.RPL_FLUSH_EVERY - 1):
        s.note_received(PROXY_NODE, 0, 11 + i)
    assert read(state_file)["rpl"] == {"0148": [0, 10]}
    s.note_received(PROXY_NODE, 0, 100)  # the RPL_FLUSH_EVERY-th update since the flush
    assert read(state_file)["rpl"] == {"0148": [0, 100]}
    s.next_seq()  # a send persists everything anyway
    s.note_received(PROXY_NODE, 0, 101)
    assert read(state_file)["rpl"] == {"0148": [0, 100]}
    s.close()  # close flushes
    assert read(state_file)["rpl"] == {"0148": [0, 101]}


def test_replay_list_is_bounded(
    state_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """A full list keeps every entry it has and admits no new source (evicting one would let a NetKey holder
    flood the list with made-up sources until a real node's entry went, then replay that node's PDUs)."""
    monkeypatch.setattr(LocalState, "RPL_MAX", 3)
    s = LocalState(state_file, OUR_SRC)
    for src, seq in ((0x0100, 50), (0x0200, 20), (0x0300, 30)):
        s.note_received(src, 0, seq)
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        assert not s.admits(0x0400)
        s.note_received(0x0400, 0, 40)  # a fourth source: not recorded, nothing evicted
        s.note_received(0x0500, 1, 1)
    assert s.rpl == {0x0100: (0, 50), 0x0200: (0, 20), 0x0300: (0, 30)}
    assert caplog.text.count("replay list full (3 sources)") == 1  # once, not per PDU
    s.note_received(0x0200, 1, 5)  # known sources keep advancing
    assert s.admits(0x0200)
    assert s.rpl[0x0200] == (1, 5)
    s.purge_rpl(1)  # an IV Update frees the room again
    assert s.admits(0x0400)
    s.note_received(0x0400, 1, 40)
    assert s.rpl == {0x0200: (1, 5), 0x0400: (1, 40)}
    s.close()


async def test_a_full_replay_list_drops_pdus_from_new_sources(
    cdb: CDB, monkeypatch: pytest.MonkeyPatch
):
    """The receive path checks `admits`: a new source's PDU is dropped like a replay (it would have no entry
    to stop its own replay), while the sources the list holds are still heard."""
    monkeypatch.setattr(LocalState, "RPL_MAX", 1)
    delivered: list[Any] = []
    proxy = ProxyClient(cdb, LocalState(None, OUR_SRC), on_message=delivered.append)
    link = FakeBleak(cdb)
    await proxy.attach(link)
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    # a second source: the list is full
    link.send_access(LIGHT_2G, OUR_SRC, ONOFF_STATUS_ON)
    link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    assert [m.src for m in delivered] == [PROXY_NODE, PROXY_NODE]
    assert list(proxy.state.rpl) == [PROXY_NODE]
    await proxy.detach()


def test_purge_rpl_persists_when_it_drops_something(state_file: Path):
    s = LocalState(state_file, OUR_SRC)
    s.rpl = {0x0100: (0, 5), 0x0200: (1, 7), 0x0300: (2, 1)}
    s.persist()
    s.purge_rpl(1)
    assert read(state_file)["rpl"] == {"0200": [1, 7], "0300": [2, 1]}
    s.rpl[0x0400] = (3, 9)
    s.purge_rpl(1)  # nothing to drop: no write
    assert read(state_file)["rpl"] == {"0200": [1, 7], "0300": [2, 1]}
    s.close()


async def test_a_lost_link_flushes_the_replay_list_of_a_receive_only_session(
    cdb: CDB, state_file: Path
):
    state = LocalState(state_file, OUR_SRC)
    proxy = ProxyClient(cdb, state)
    link = FakeBleak(cdb)
    await proxy.attach(link)
    seq = link.send_access(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    assert proxy.state.rpl == {PROXY_NODE: (0, seq)}
    assert read(state_file)["rpl"] == {}  # nothing sent since: not written yet
    proxy.handle_disconnected(link)
    assert read(state_file)["rpl"] == {"0148": [0, seq]}
    state.close()


async def test_replay_list_from_the_file_rejects_a_recorded_pdu_after_a_restart(
    cdb: CDB, state_file: Path
):
    """The scenario the persistence exists for: a PDU recorded before a restart is replayed after it."""
    state = LocalState(state_file, OUR_SRC)
    proxy = ProxyClient(cdb, state)
    link = FakeBleak(cdb)
    await proxy.attach(link)
    seq, (pdu,) = link.access_pdus(PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON)
    link.deliver(client_mod.PROXY_NETWORK_PDU, pdu)
    await proxy.detach()
    state.close()
    delivered: list[Any] = []
    restarted = ProxyClient(
        cdb, LocalState(state_file, OUR_SRC), on_message=delivered.append
    )
    assert restarted.state.rpl == {PROXY_NODE: (0, seq)}
    link2 = FakeBleak(cdb)
    await restarted.attach(link2)
    link2.deliver(client_mod.PROXY_NETWORK_PDU, pdu)  # the recording, replayed
    assert delivered == []
    link2.send_access(
        PROXY_NODE, OUR_SRC, ONOFF_STATUS_ON, seq=seq + 1
    )  # a genuine newer one
    assert len(delivered) == 1
    restarted.state.close()
