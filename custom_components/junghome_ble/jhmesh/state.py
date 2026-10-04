"""Our node's mesh state: unicast address, sequence number, IV index state, replay list — and where it is kept.

`LocalState` is what keeps every nonce (SRC, SEQ, IV index) we send unique, across restarts: it hands out sequence
numbers, follows the IV index from Secure Network Beacons and remembers the last sequence number accepted per source.
It persists to a JSON file of its own (the CLI) or, subclassed, wherever the subclass says (the Home Assistant
integration's `HAState` overrides `load` / `persist` / `persist_now` / `reserve_seq`). `ProxyClient` (`client.py`)
takes its numbers from it; `client` re-exports every public name defined here, so imports from there keep working.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import IO, Any, cast

try:
    import fcntl
except ImportError:  # pragma: no cover  # not POSIX: the state file cannot be locked, `LocalState` says so once
    fcntl = None  # type: ignore[assignment]

from .fileio import PRIVATE_MODE, atomic_write
from .keyrefresh import KeyRefreshRecord

__all__ = [
    "IV_INDEX_MAX",
    "IV_RECOVERY_MIN_INTERVAL",
    "IV_UPDATE_MIN_STATE",
    "SEQ_GUARD_FIRST_BEACON",
    "SEQ_MAX",
    "SEQ_TX_LIMIT",
    "LocalState",
    "SequenceExhausted",
    "SequenceStalled",
    "StateInUse",
]

log = logging.getLogger("jhmesh")

SEQ_MAX = 0xFFFFFF  # sequence numbers are 24 bits (§3.4.4.3)
IV_INDEX_MAX = 0xFFFFFFFF  # the IV index is 32 bits (§3.10.5)
# IV Update timing (Mesh Protocol 1.1 §3.10.5, §3.10.6), in wall-clock seconds: at least 96 h in Normal Operation and
# in IV Update in Progress before the next step, at most one IV Index Recovery per 192 h (`LocalState.apply_beacon`)
IV_UPDATE_MIN_STATE = 96 * 3600
IV_RECOVERY_MIN_INTERVAL = 192 * 3600
# `LocalState.seq_guard` before the first authenticated beacon has named the index it covers
SEQ_GUARD_FIRST_BEACON = -1
SEQ_TX_LIMIT = 0xFFFF00  # we never transmit above this: a wrap would replay SeqAuths and every node would drop us


class SequenceExhausted(ConnectionError):
    """The 24-bit sequence space of the current IV index is used up; only an IV Update lets us transmit again.

    A `ConnectionError` so every send path reports it like a lost link; the application can wait for the next
    Secure Network Beacon (which restarts the sequence with the new transmit index) or act on it.
    """


class SequenceStalled(SequenceExhausted):
    """Sequence numbers are held back for a moment (a subclass's store has not durably written the last ones).

    Back-pressure, not exhaustion: the same send succeeds once the store catches up, normally within a second.
    A subclass of `SequenceExhausted` so code that only knows the latter still treats it as "cannot send now".
    """


def _wall_now() -> float:
    """Return the wall clock (seconds since the epoch), looked up at call time.

    The IV Update timing guards (`LocalState.apply_beacon`) store when the IV state last changed with the counter, so
    the time has to mean the same after a restart: the monotonic clock starts over with the host.
    """
    return time.time()


def _check_time(what: str, value: Any) -> float:
    """Return a stored wall-clock time as a float; TypeError / ValueError when it is not a finite, non-negative number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{what} {value!r} is not a time")
    if not 0 <= value < float("inf"):
        raise ValueError(f"{what} {value!r} is out of range")
    return float(value)


def _check_range(what: str, value: int, low: int, high: int) -> None:
    """`ValueError` unless `low <= value <= high` (a stored record's field, `LocalState.parse_record`)."""
    if not low <= value <= high:
        raise ValueError(f"{what} {value} is outside {low:#x}..{high:#x}")


class StateInUse(RuntimeError):
    """Another process holds the state file: two clients sharing one source address would reuse nonces.

    The sequence number is the only thing that keeps the AES-CCM nonce (SRC, SEQ, IV index) unique. Two
    processes loading the same file each take numbers from their own copy of the counter, so the second one
    must not start at all — not wait: whichever finished last would also write the lower counter back.
    """


class LocalState:
    """Our node identity: unicast address, 24-bit sequence number, IV index state, replay list.

    Subclass and override `load()` / `persist()` to change where it is stored (HA uses its storage helper;
    `to_dict()` is the identity + counter, `to_stored()` adds the replay list, and `load()` may hand either
    back). `restart_margin` is added to the sequence number on load so a crash between the last send and
    the last save can never make us reuse a sequence number (replay protection would drop us).

    With a `path` the file is written atomically and owner-only (`fileio.atomic_write`: a temp file + `os.replace`,
    so a crash mid-write cannot leave a torn file; 0600, as it can hold a network key) and an exclusive `flock` on
    `<path>.lock` is held for the life of the object: a second process on the same file gets `StateInUse`. Every
    `BACKUP_EVERY` numbers the state is copied to `<path>.bak`; an unreadable state file (a torn write of an older
    version, a disk fault) falls back to it, with the margin covering what the copy lags — only when neither is
    readable does the counter start at 0, with a WARNING.

    The sequence never wraps: above `SEQ_TX_LIMIT` `next_seq` / `reserve_seq` raise `SequenceExhausted`
    until an IV Update restarts it (`apply_beacon`) — a silent wrap to 0 would reuse every SeqAuth of the
    current IV index and every node's replay list would drop us for good.

    `rpl` is the replay protection list (§3.8.8: the last accepted sequence number per source, per IV index);
    it is stored with the counter so a PDU recorded off the air cannot be accepted again after a restart.
    `RPL_MAX` bounds it (a full list admits no new source, `admits`) and `note_received` persists it at most every
    `RPL_FLUSH_EVERY` entries, plus `flush()` when the link goes.
    """

    BACKUP_EVERY = 256  # sequence numbers between two `.bak` copies (< restart_margin, so the copy is safe to resume from)
    RPL_MAX = 2048  # sources remembered; a unicast space holds 32 767, real meshes a few dozen
    RPL_FLUSH_EVERY = (
        64  # replay-list updates between two persists on a receive-only link
    )

    def __init__(
        self,
        path: Path | None,
        default_src: int,
        restart_margin: int = 512,
        configured_src_wins: bool = False,
    ) -> None:
        """Restore src/seq/IV state from `path` when present, else start fresh at `default_src`; persist the result.

        `StateInUse` when another process holds the file's lock. Whatever else stops the start (the first write
        failing on a full disk) releases the lock again before it propagates: the half-built object is not a
        client, and a retry in the same process must not be told the file is "in use by another process" — its
        own — for as long as anything holds the exception.
        """
        self.path = path
        self.src, self.seq, self.iv_index, self.iv_update_active = (
            default_src,
            0,
            0,
            False,
        )
        self.iv_known = False  # never learnt an IV index yet: apply_beacon's recovery bound does not apply
        self.rpl: dict[
            int, tuple[int, int]
        ] = {}  # src -> (iv_index, seq) of the last accepted message
        self._lock_file: IO[str] | None = None
        self._backup_seq = 0  # counter value the `.bak` copy holds
        self._backup_tx: int | None = (
            None  # transmit IV index the `.bak` copy holds (None: not written by us yet)
        )
        self._rpl_dirty = 0  # replay-list updates since the last persist
        # `admits` warns once, when the full list first refuses a source
        self._rpl_full_warned = False
        self._last_parse_error: Exception | None = None
        # a key refresh the mesh went through: the new NetKey and the phase (1 or 2 while it runs,
        # 3 once the old key is revoked but the export still holds it) with the proof that moved it there,
        # learnt from the provisioner's messages (`ProxyClient._follow_key_refresh`); None when there is none
        self.key_refresh: KeyRefreshRecord | None = None
        # the counter was moved past numbers sent under an IV index nobody remembers (a sequence-number record
        # lost and skipped past, `seq_guard` in `apply_beacon`): the highest index it still covers, or
        # SEQ_GUARD_FIRST_BEACON until a beacon names it; None when there is nothing to guard
        self.seq_guard: int | None = None
        # when the IV state last changed, and when the last IV Index Recovery was (wall-clock seconds, `_wall_now`):
        # `apply_beacon` keeps the spec's 96 h / 192 h apart from them. None: not known (a record from before they
        # were kept, a fresh state): no restriction
        self.iv_changed_at: float | None = None
        self.iv_recovered_at: float | None = None
        # the highest sequence number reached under any transmit index from `seq_peak_from` on other than the current
        # one (taken whenever the sequence restarts at 0): what `rewind_iv_index` has to continue past. A record from
        # before it was kept knows only its own transmit index; a fresh address has sent nothing anywhere
        self.seq_peak = 0
        self.seq_peak_from = 0
        # beacon indexes a timing refusal was logged for (once each)
        self._iv_refused: set[int] = set()
        if path is not None:
            self._acquire_lock(path)
        try:
            self._start(default_src, restart_margin, configured_src_wins)
        except BaseException:
            self._release_lock()
            raise

    def _start(
        self, default_src: int, restart_margin: int, configured_src_wins: bool
    ) -> None:
        """Restore the stored record (or start fresh) and write it back, `.bak` included (`__init__`)."""
        restored_ok = True
        d = self.load()
        if d:
            try:
                self._restore(d, default_src, restart_margin, configured_src_wins)
            except (KeyError, ValueError, TypeError) as err:
                restored_ok = False
                log.warning(
                    "stored sequence state is unusable (%s): starting at 0 — every node drops our messages "
                    "until the counter passes the numbers they know; use another source address if it does not recover",
                    err,
                )
        elif self._last_parse_error is not None:
            restored_ok = False
            log.warning(
                "stored sequence state is unusable (%s): starting at 0 — every node drops our messages "
                "until the counter passes the numbers they know; use another source address if it does not recover",
                self._last_parse_error,
            )
        self.persist()
        # a fresh start or a successful restore refreshes `.bak`; a record that could not be restored at all
        # leaves an existing `.bak` alone — it may be the only good copy — until the counter moves on from 0
        if not restored_ok:
            self._backup_tx = self.tx_iv_index
        self._backup(force=restored_ok)

    @staticmethod
    def parse_record(
        d: dict[str, Any],
    ) -> tuple[
        int, int, int, bool, bool, dict[int, tuple[int, int]], KeyRefreshRecord | None
    ]:
        """Parse a stored record (`to_stored()` / `to_dict()` form) into locals; raises on bad values, mutates nothing."""
        stored_src = int(d["src"], 16)
        iv_index = int(d.get("iv_index", 0))
        iv_update_active = bool(d.get("iv_update_active", False))
        # a record from before `iv_known` existed: a non-zero index (or an update in progress) can only have come
        # from a beacon, so it counts as known and keeps its recovery bound; index 0 may be the fresh state
        # `__init__` persists before any beacon, so it stays unknown — the first authenticated beacon decides.
        iv_known = bool(d.get("iv_known", iv_index != 0 or iv_update_active))
        seq = int(d["seq"])
        rpl = {
            int(src, 16): (int(entry[0]), int(entry[1]))
            for src, entry in (d.get("rpl") or {}).items()
        }
        # out-of-range values convert fine but make every send raise OverflowError (`to_bytes`) for good, or lock
        # a source out of the replay list forever: a record holding one is as unusable as a torn one, and the
        # `.bak` copy gets its chance instead
        _check_range("source address", stored_src, 1, 0x7FFF)
        _check_range("sequence number", seq, 0, SEQ_MAX)
        _check_range("IV index", iv_index, 0, IV_INDEX_MAX)
        for src, (entry_iv, entry_seq) in rpl.items():
            _check_range("replay-list source", src, 1, 0x7FFF)
            _check_range("replay-list IV index", entry_iv, 0, IV_INDEX_MAX)
            _check_range("replay-list sequence number", entry_seq, 0, SEQ_MAX)
        key_refresh: KeyRefreshRecord | None = None
        if (kr := d.get("key_refresh")) is not None:
            key_refresh = KeyRefreshRecord.from_stored(kr)
        LocalState.parse_seq_guard(d)
        LocalState.parse_iv_times(d)
        LocalState.parse_seq_peak(d)
        return stored_src, seq, iv_index, iv_update_active, iv_known, rpl, key_refresh

    @staticmethod
    def parse_seq_guard(d: dict[str, Any]) -> int | None:
        """Return the stored record's `seq_guard` (absent: None, as in every record written before it existed)."""
        guard = d.get("seq_guard")
        if guard is None:
            return None
        if isinstance(guard, bool) or not isinstance(guard, int):
            raise TypeError(f"sequence guard {guard!r} is not an IV index")
        _check_range("sequence guard", guard, SEQ_GUARD_FIRST_BEACON, IV_INDEX_MAX)
        return guard

    @staticmethod
    def parse_iv_times(d: dict[str, Any]) -> tuple[float | None, float | None]:
        """Return the stored record's (`iv_changed_at`, `iv_recovered_at`); absent: None, as before they were kept."""
        changed, recovered = d.get("iv_changed_at"), d.get("iv_recovered_at")
        return (
            None if changed is None else _check_time("IV change time", changed),
            None if recovered is None else _check_time("IV recovery time", recovered),
        )

    @staticmethod
    def parse_seq_peak(d: dict[str, Any]) -> tuple[int, int]:
        """Return the stored record's (`seq_peak`, `seq_peak_from`).

        A record from before they were kept: nothing known below its own transmit index (whatever was sent there
        before is not on record), so the peak starts there at 0 — its own index is covered by its counter.
        """
        if "seq_peak" not in d and "seq_peak_from" not in d:
            iv_index = int(d.get("iv_index", 0))
            return 0, max(iv_index - 1 if d.get("iv_update_active") else iv_index, 0)
        peak, start = d.get("seq_peak"), d.get("seq_peak_from")
        if isinstance(peak, bool) or not isinstance(peak, int):
            raise TypeError(f"sequence peak {peak!r} is not a number")
        if isinstance(start, bool) or not isinstance(start, int):
            raise TypeError(f"sequence peak index {start!r} is not an IV index")
        _check_range("sequence peak", peak, 0, SEQ_MAX)
        _check_range("sequence peak index", start, 0, IV_INDEX_MAX)
        return peak, start

    def _restore(
        self,
        d: dict[str, Any],
        default_src: int,
        restart_margin: int,
        configured_src_wins: bool,
    ) -> None:
        """Adopt a stored record; ValueError / KeyError / TypeError when it is not one, before anything is assigned."""
        stored_src, seq, iv_index, iv_update_active, iv_known, rpl, key_refresh = (
            self.parse_record(d)
        )
        # every conversion succeeded: only now assign to self
        self.iv_index, self.iv_update_active, self.iv_known, self.rpl = (
            iv_index,
            iv_update_active,
            iv_known,
            rpl,
        )
        self.key_refresh = key_refresh
        self.iv_changed_at, self.iv_recovered_at = self.parse_iv_times(d)
        if stored_src == default_src or not configured_src_wins:
            self.src = stored_src
            self.seq = min(seq + restart_margin, SEQ_MAX)
            self.seq_guard = self.parse_seq_guard(d)
            self.seq_peak, self.seq_peak_from = self.parse_seq_peak(d)
        # else: a new address was configured → fresh sequence space, nothing to carry over

    def _acquire_lock(self, path: Path) -> None:
        """Hold `<path>.lock` exclusively until `close()` (or the object dies); it names the holder's pid.

        The lock file is separate from the state file because `persist` replaces the state file's inode on
        every write, and a lock travels with the inode.
        """
        if fcntl is None:
            log.warning(
                "cannot lock %s on this platform: never run two clients with one source address",
                path,
            )
            return
        lock_path = path.with_suffix(".lock")
        f = os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "r+")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = f.read(32).strip()
            f.close()
            raise StateInUse(
                f"{path} is in use by another process"
                + (f" (pid {holder})" if holder else "")
                + ": two clients must never share a source address"
            ) from None
        f.truncate(0)
        f.write(f"{os.getpid()}\n")
        f.flush()
        self._lock_file = f

    def close(self) -> None:
        """Write pending replay-list updates and release the lock (a CLI process just exits; tests reuse files)."""
        self.flush()
        self._release_lock()

    def _release_lock(self) -> None:
        if self._lock_file is not None:
            self._lock_file.close()  # closing the descriptor releases the flock
            self._lock_file = None

    # -- persistence hooks
    def load(self) -> dict[str, Any] | None:
        """Read the stored state; None when there is none.

        An unreadable state file, or one with bad values, falls back to the `.bak` copy (WARNING); an
        unreadable copy too is "no state". `_last_parse_error` remembers the last bad-values failure so
        `__init__` can still warn when no candidate is usable.
        """
        if self.path is None:
            return None
        self._last_parse_error = None
        for candidate in (self.path, self.path.with_suffix(".bak")):
            if not candidate.exists():
                continue
            self._tighten(candidate)
            try:
                d = json.loads(candidate.read_text())
            except (OSError, ValueError) as err:
                log.warning(
                    "%s is unreadable (%s), trying the backup copy", candidate, err
                )
                continue
            if not isinstance(d, dict) or not {"src", "seq"} <= d.keys():
                log.warning(
                    "%s does not hold a state record, trying the backup copy", candidate
                )
                continue
            try:
                self.parse_record(d)
            except (KeyError, ValueError, TypeError) as err:
                log.warning(
                    "%s holds unusable values (%s), trying the backup copy",
                    candidate,
                    err,
                )
                self._last_parse_error = err
                continue
            if candidate != self.path:
                log.warning("sequence state restored from %s", candidate)
            return cast("dict[str, Any]", d)
        return None

    @staticmethod
    def _tighten(path: Path) -> None:
        """Make a state file group- or other-readable (an older version wrote it with the umask's mode) owner-only.

        The record can hold a network key (`to_stored`). Logged once: the next load finds it owner-only already.
        """
        try:
            if path.stat().st_mode & 0o077:
                path.chmod(PRIVATE_MODE)
                log.info("%s was readable by others; made it owner-only", path)
        except OSError as err:
            log.warning("could not make %s owner-only: %s", path, err)

    def persist(self) -> None:
        """Write the state to `path`, if one is set — atomically, so a crash mid-write leaves the previous file."""
        self._rpl_dirty = 0
        if self.path:
            self._write(self.path, self.to_stored())

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        """Write `data` to `path` atomically (`fileio.atomic_write`), owner-only: it holds a key refresh's NetKey."""
        atomic_write(path, json.dumps(data).encode(), private=True)

    def _backup(self, force: bool = False) -> None:
        """Copy the state to `<path>.bak` when it moved `BACKUP_EVERY` or to another transmit index (or none is there).

        `force` always writes — used after a fresh start or a successful restore, so `.bak` picks up the new
        IV state and the margin-bumped counter at once, and after an IV index change. The copy's own transmit
        index is compared too, so that a *failed* forced write is made up by the next `reserve_seq` rather than
        skipped: the sequence restarts at 0 with a new transmit index, so "fewer than `BACKUP_EVERY` past the
        copy" held for the first numbers of the new index while the copy was still on the old one, and a
        restore from it went back to that index — the next beacon then restarted the sequence at 0 under the
        new one, reusing every number sent there (found by the property tests' state machine). As a write of the
        copy raises out of `reserve_seq` before any number is handed out, nothing is ever sent that the copy
        would not cover.
        """
        if self.path is None:
            return
        bak = self.path.with_suffix(".bak")
        if (
            not force
            and bak.exists()
            and self._backup_tx == self.tx_iv_index
            and 0 <= self.seq - self._backup_seq < self.BACKUP_EVERY
        ):
            return
        self._write(bak, self.to_stored())
        self._backup_seq, self._backup_tx = self.seq, self.tx_iv_index

    def flush(self) -> None:
        """Persist now if replay-list updates are pending (`note_received` batches them)."""
        if self._rpl_dirty:
            self.persist()

    def to_dict(self) -> dict[str, Any]:
        """Return the identity and counter as the JSON-serialisable dict (what diagnostics show)."""
        return {
            "src": f"{self.src:04X}",
            "seq": self.seq,
            "iv_index": self.iv_index,
            "iv_update_active": self.iv_update_active,
        }

    def to_stored(self) -> dict[str, Any]:
        """Return everything `persist` stores.

        `to_dict()` plus the replay list (`{"0148": [iv_index, seq]}` per entry) and `iv_known` — not in
        `to_dict()`, which diagnostics and many tests compare as a whole — and a key refresh in progress (the new
        NetKey: never in `to_dict()`), the sequence guard, the IV timing and the sequence peak.
        """
        stored: dict[str, Any] = {
            **self.to_dict(),
            "iv_known": self.iv_known,
            "rpl": {f"{src:04X}": list(entry) for src, entry in self.rpl.items()},
            "seq_peak": self.seq_peak,
            "seq_peak_from": self.seq_peak_from,
        }
        if self.key_refresh is not None:
            stored["key_refresh"] = self.key_refresh.to_stored()
        if self.seq_guard is not None:
            stored["seq_guard"] = self.seq_guard
        if self.iv_changed_at is not None:
            stored["iv_changed_at"] = self.iv_changed_at
        if self.iv_recovered_at is not None:
            stored["iv_recovered_at"] = self.iv_recovered_at
        return stored

    def persist_now(self) -> None:
        """Persist without delay: `persist` already does; a subclass that debounces it writes at once here."""
        self.persist()

    def set_key_refresh(self, refresh: KeyRefreshRecord | None) -> None:
        """Record a key refresh in progress, or its end, and persist it at once (`persist_now`).

        A debounced write lost the new key to a crash right after it was learnt: the mesh had moved
        on with it, and nothing else could tell it again.
        """
        if refresh != self.key_refresh:
            self.key_refresh = refresh
            self.persist_now()

    # -- replay protection list
    def admits(self, src: int) -> bool:
        """Whether PDUs from `src` may be accepted at all: it has an entry, or the list has room for one.

        A full list refuses new sources rather than evicting an old entry. Eviction would let anyone holding the
        NetKey (a Heartbeat needs nothing more) flood the list with made-up sources until a real node's entry
        goes, then replay that node's recorded PDUs; refusing costs only messages from sources first heard once
        `RPL_MAX` others were — far beyond any real mesh — until an IV Update purges the list (`purge_rpl`).
        """
        if src in self.rpl or len(self.rpl) < self.RPL_MAX:
            return True
        if not self._rpl_full_warned:
            self._rpl_full_warned = True
            log.warning(
                "replay list full (%d sources): messages from sources not heard before are dropped",
                self.RPL_MAX,
            )
        return False

    def note_received(self, src: int, iv_index: int, seq: int) -> None:
        """Record `seq` as the last accepted from `src` under `iv_index` (never lower than what is stored).

        Persisted every `RPL_FLUSH_EVERY` updates: a listening client sends nothing, so nothing else would
        write the list; the sender side rewrites the file per sequence number anyway. A source the full list
        does not admit is not recorded (the receive path drops its PDUs before they get here).
        """
        entry = (iv_index, seq)
        if src in self.rpl and self.rpl[src] >= entry:
            return
        if not self.admits(src):
            return
        self.rpl[src] = entry
        self._rpl_dirty += 1
        if self._rpl_dirty >= self.RPL_FLUSH_EVERY:
            self.persist()

    def purge_rpl(self, oldest_iv_index: int) -> None:
        """Forget the sources whose last message came under an IV index below `oldest_iv_index`."""
        before = len(self.rpl)
        self.rpl = {src: e for src, e in self.rpl.items() if e[0] >= oldest_iv_index}
        if len(self.rpl) != before:
            self.persist()

    # -- sequence / IV handling
    @property
    def tx_iv_index(self) -> int:
        """IV index used for transmission: during 'IV Update in Progress' nodes keep sending with the old index."""
        return self.iv_index - 1 if self.iv_update_active else self.iv_index

    def next_seq(self) -> int:
        """Take the next sequence number and persist; `SequenceExhausted` above `SEQ_TX_LIMIT`."""
        return self.reserve_seq(1)

    def reserve_seq(self, count: int) -> int:
        """Take `count` consecutive sequence numbers (a segmented message) and persist; return the first.

        Raises `SequenceExhausted` when any of them would lie above `SEQ_TX_LIMIT`; nothing is consumed then.
        """
        s = self.seq
        if s + count - 1 > SEQ_TX_LIMIT:
            raise SequenceExhausted(
                f"sequence number {s:06X} is at the end of the 24-bit space; waiting for an IV Update"
            )
        self.seq = s + count
        self.persist()
        self._backup()
        return s

    def apply_beacon(
        self, iv_index: int, iv_update: bool, now: float | None = None
    ) -> bool:
        """Follow the Secure Network Beacon (Mesh Profile §3.10.5). Returns True if anything changed.

        Normal Operation → IV Update in Progress only for index + 1; a beacon with our *current* index and the flag
        set while we are in Normal Operation is a lagging node still on an update we already completed — ignoring it
        is what keeps the transmit index (and so the sequence space) from ever going backwards. An index further
        ahead (≤ 42) is adopted as it is (IV Index Recovery, §3.10.6) — that bound is for a node that was already
        in the network and missed updates; a fresh state that has never learnt an index (`not self.iv_known`, a
        client being provisioned) adopts the first authenticated beacon unconditionally instead, or it could
        never join a network whose index has already moved past 42.

        Timing: a state that knows its index keeps the spec's minimum times, on the wall clock (`now`,
        default `_wall_now()`) from the stored `iv_changed_at` / `iv_recovered_at` — a step between Normal Operation
        and IV Update in Progress waits `IV_UPDATE_MIN_STATE` (96 h) after the last change of the IV state, an IV
        Index Recovery `IV_RECOVERY_MIN_INTERVAL` (192 h) after the last recovery. Without them any NetKey holder
        (perhaps anyone in range, if a proxy forwards ADV-bearer beacons) could ratchet the index 42 at a time with
        authenticated beacons, until every node dropped our PDUs and no real beacon was ever ahead of us again. An
        unknown time does not restrict (a record from before they were kept; the fresh state's first beacon stamps
        nothing). A refusal is logged at WARNING once per index. A clock that reads earlier than a stored time went
        back (or the time was stamped by a clock running ahead): the stored time is pulled back to now, so such a
        jump delays the next step by one full period at most instead of until the clock catches up.

        `seq_guard` keeps the sequence from restarting at 0 under a transmit index it covers: after a lost record
        was skipped past (the integration's `seq_store_lost` repair), the lost numbers were sent under an index
        nobody remembers — at most the one of the first authenticated beacon heard afterwards, since the network's
        index never goes back, or one more when that beacon came from a proxy still behind an update. Adopting
        that index used to restart the sequence at 0 there, over those very numbers (found by the property tests'
        state machine); now the counter carries on under every index up to one past it, and only an index beyond
        starts from 0 again (and ends the guard). Carrying on under an index is always safe; a guard one too high
        only spends sequence space. A guard stored with an unknown index (`rewind_iv_index`) is raised to one past
        the first beacon's index the same way, never lowered.
        """
        limit = (
            self.iv_index + 42 if self.iv_known else 0xFFFFFFFF
        )  # IV index is 32 bits
        if iv_index < self.iv_index or iv_index > limit:
            return False
        was_known, self.iv_known = self.iv_known, True
        if self.seq_guard is not None and (
            self.seq_guard == SEQ_GUARD_FIRST_BEACON or not was_known
        ):
            # the numbers the guard is for were sent before this beacon, so under its index at most — or the next
            # one, if this proxy has not caught up with an update yet
            self.seq_guard = max(self.seq_guard, min(iv_index + 1, IV_INDEX_MAX))
        if iv_index == self.iv_index:
            if iv_update or not self.iv_update_active:
                return False  # same state, or a stale "in progress" for an update we have completed
            # In Progress → Normal Operation: the update completes
            update_active, recovery = False, False
        elif iv_index == self.iv_index + 1 and iv_update and not self.iv_update_active:
            # Normal Operation → In Progress: keep transmitting with the old index
            update_active, recovery = True, False
        else:
            # adopt whatever the network is at
            update_active, recovery = iv_update, True
        now = _wall_now() if now is None else now
        if was_known and self._too_early(iv_index, recovery, now):
            return False
        old_tx = self.tx_iv_index
        self.iv_index, self.iv_update_active = iv_index, update_active
        if was_known:
            self.iv_changed_at = now
            if recovery:
                self.iv_recovered_at = now
        self._iv_refused.clear()
        if self.tx_iv_index > old_tx:
            if self.seq_guard is None or self.tx_iv_index > self.seq_guard:
                # what `rewind_iv_index` must stay above, should the index ever have to go back over these
                self.seq_peak = max(self.seq_peak, self.seq)
                self.seq = 0  # sequence numbers restart with a new transmit IV index — never otherwise
                self.seq_guard = None
            # else: numbers under this index may have been sent before the record was lost; keep counting on
        self.persist()
        self._backup(
            force=True
        )  # any IV state change must not leave `.bak` behind on the old index
        return True

    def _too_early(self, iv_index: int, recovery: bool, now: float) -> bool:
        """Whether a change of the IV state to `iv_index` comes sooner than the spec allows (`apply_beacon`)."""
        if (self.iv_changed_at or 0.0) > now or (self.iv_recovered_at or 0.0) > now:
            # the clock went back: the wait starts over from now rather than until the clock catches up
            if self.iv_changed_at is not None:
                self.iv_changed_at = min(self.iv_changed_at, now)
            if self.iv_recovered_at is not None:
                self.iv_recovered_at = min(self.iv_recovered_at, now)
            self.persist()
        if recovery:
            since, wait = self.iv_recovered_at, IV_RECOVERY_MIN_INTERVAL
        else:
            since, wait = self.iv_changed_at, IV_UPDATE_MIN_STATE
        if since is None or now - since >= wait:
            return False
        if iv_index not in self._iv_refused:
            self._iv_refused.add(iv_index)
            log.warning(
                "ignoring a beacon's IV index %d (%s): the last %s was less than %d h ago (%d h) — "
                "a forged or replayed beacon, or a clock that jumped",
                iv_index,
                "IV Index Recovery" if recovery else "IV Update step",
                "recovery" if recovery else "change",
                wait // 3600,
                int((now - since) // 3600),
            )
        return True

    def can_rewind_to(self, iv_index: int) -> bool:
        """Whether `rewind_iv_index(iv_index)` keeps every nonce unique.

        The index has to be behind ours, and the record has to know every sequence number sent from there on
        (`seq_peak_from`).
        """
        return self.seq_peak_from <= iv_index < self.iv_index

    def rewind_point(self) -> tuple[int, int]:
        """Return the (counter, `seq_guard`) a `rewind_iv_index` would continue from now.

        Above every number sent from `seq_peak_from` on, guarded up to our index: for a caller that records them
        before going back (the integration's repair floor).
        """
        guard = SEQ_GUARD_FIRST_BEACON if self.seq_guard is None else self.seq_guard
        return max(self.seq, self.seq_peak), max(self.iv_index, guard)

    def rewind_iv_index(self, iv_index: int) -> int:
        """Go back to the network's `iv_index`, which is behind ours (the integration's `iv_index_mismatch` repair).

        Ahead of the network means pushed there (forged beacons, a store of another mesh): every node drops our PDUs
        and every real beacon is "behind" us, so nothing else ever brings us back. Going back must not reuse a
        nonce: the counter continues above every number sent under any index from `iv_index` up to ours
        (`seq_peak` holds the earlier indexes', the counter our own), and `seq_guard` keeps it from restarting at 0
        under any of them until the network has passed ours. The index is stored as not known (`iv_known`): the
        first authenticated beacon is adopted as it is, as a fresh state's would be, and raises the guard to one
        past its own index should the network have moved on meanwhile. Replay-list entries under an index above
        `iv_index` are dropped — the network never reached one, so only a NetKey holder sent them, and they would
        refuse every real message of their source. The IV timing stays: a rewind gives a pusher no fresh recovery.

        Returns the counter it continues from; ValueError when `can_rewind_to` says no.
        """
        if not self.can_rewind_to(iv_index):
            raise ValueError(
                f"cannot go back from IV index {self.iv_index} to {iv_index} without reusing sequence numbers"
            )
        self.seq, self.seq_guard = self.rewind_point()
        self.seq_peak = self.seq
        self.iv_index, self.iv_update_active, self.iv_known = iv_index, False, False
        self.rpl = {src: e for src, e in self.rpl.items() if e[0] <= iv_index}
        self._iv_refused.clear()
        self.persist()
        self._backup(force=True)
        return self.seq
