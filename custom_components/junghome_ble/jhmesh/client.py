"""GATT Proxy client for a Bluetooth Mesh network, transport-agnostic.

The `ProxyClient` does everything above the GATT link: proxy protocol, beacons, network/transport
crypto, segmentation, request/response matching. It is handed an already-connected bleak-style
client via `attach()` — by `standalone.py` (plain bleak on a local adapter) or by the Home Assistant
integration (HA's Bluetooth stack, which may be an ESPHome proxy).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, cast

try:
    import fcntl
except ImportError:  # pragma: no cover  # not POSIX: the state file cannot be locked, `LocalState` says so once
    fcntl = None  # type: ignore[assignment]

from .config_messages import (
    CONFIG_KEY_REFRESH_PHASE_SET,
    CONFIG_KEY_REFRESH_PHASE_STATUS,
    CONFIG_NAMES,
    CONFIG_NETKEY_STATUS,
    CONFIG_NETKEY_UPDATE,
)
from .crypto import NetKeyMaterial
from .fileio import PRIVATE_MODE, atomic_write
from .keyrefresh import KeyRefreshFollower, KeyRefreshRecord, Moved, describe_proof
from .messages import describe
from .pdu import (
    FILTER_BLACKLIST,
    NONCE_APP,
    NONCE_DEVICE,
    PROXY_BEACON,
    PROXY_CONFIG,
    PROXY_NETWORK_PDU,
    NetworkPDU,
    ProxyReassembler,
    SecureNetworkBeacon,
    SegmentInfo,
    decode_opcode,
    is_unicast,
    lower_segments_access,
    lower_unsegmented_access,
    network_decrypt,
    network_encrypt,
    parse_beacon,
    parse_lower,
    proxy_config_set_filter,
    proxy_frame,
    segment_ack,
    seq_auth_from,
    upper_decrypt,
    upper_encrypt,
)

if TYPE_CHECKING:
    from .cdb import CDB, Node

log = logging.getLogger("jhmesh")

MESH_PROXY_SERVICE = "00001828-0000-1000-8000-00805f9b34fb"
MESH_PROXY_DATA_IN = "00002add-0000-1000-8000-00805f9b34fb"
MESH_PROXY_DATA_OUT = "00002ade-0000-1000-8000-00805f9b34fb"

SEGMENT_SIZE = 12
NET_KEY_INDEX = (
    0  # the primary NetKey, the only one JUNG networks use (`cdb.net_keys[0]`)
)
SEGMENT_ACK_TIMEOUT = 1.5
SEGMENT_RETRIES = 4
SEGMENT_RESTARTS = (
    2  # how often a segmented message starts over because the IV index moved mid-way
)
SEQ_MAX = 0xFFFFFF  # sequence numbers are 24 bits (§3.4.4.3)
IV_INDEX_MAX = 0xFFFFFFFF  # the IV index is 32 bits (§3.10.5)
# IV Update timing (Mesh Protocol 1.1 §3.10.5, §3.10.6), in wall-clock seconds: at least 96 h in Normal Operation and
# in IV Update in Progress before the next step, at most one IV Index Recovery per 192 h (`LocalState.apply_beacon`)
IV_UPDATE_MIN_STATE = 96 * 3600
IV_RECOVERY_MIN_INTERVAL = 192 * 3600
# `LocalState.seq_guard` before the first authenticated beacon has named the index it covers
SEQ_GUARD_FIRST_BEACON = -1
SEQ_TX_LIMIT = 0xFFFF00  # we never transmit above this: a wrap would replay SeqAuths and every node would drop us
FILTER_RESEND_TRIES = 10  # a subclass may hold reserve_seq() back until its store catches up (HAState); retry, not give up
FILTER_RESEND_WAIT = 1.0  # between those retries
FILTER_ACK_TIMEOUT = (
    2.0  # the proxy answers Set Filter Type with a Filter Status within milliseconds
)
FILTER_SET_TRIES = 3  # Set Filter Type requests per link without a Filter Status before giving up on it
GATT_TIMEOUT = 5.0  # one GATT write (a frame, without response), subscription, unsubscription or disconnect; normally ms
# a PDU from our own address with a number we never handed out is reported at most this often per link (seconds):
# another client on the address sends steadily, and one report (with the highest number seen) is the news
FOREIGN_SOURCE_REPORT_INTERVAL = 60.0


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


@dataclass
class ProxyCandidate:
    """A proxy node seen advertising, as reported by a scan."""

    address: str  # BLE address / CoreBluetooth UUID
    rssi: int
    kind: str  # 'network-id' | 'node-identity'
    node_addr: int | None = None
    name: str | None = None
    device: Any = None  # backend BLEDevice, if available
    adv: Any = (
        None  # backend AdvertisementData (local name, manufacturer data), if available
    )


def _now() -> float:
    """Return the monotonic clock, looked up at call time (a frozen clock in tests then stamps messages consistently)."""
    return time.monotonic()


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


@dataclass
class AccessMessage:
    """A received access message with its network-layer context.

    `repr()` leaves the payload out: a device-key message the sniffer decrypted may be a Config AppKey Add or
    NetKey Update carrying a mesh key in the clear, and a stray `%r` / debugger print must not leak it. `str()`
    shows the redacting `describe` of the payload instead.
    """

    src: int
    dst: int
    ttl: int
    seq: int
    opcode: int
    company_id: int | None
    params: bytes = field(repr=False)
    access_pdu: bytes = field(repr=False)
    key: str  # 'app0' | 'dev:<addr>'
    received: float = field(default_factory=_now)

    def __str__(self) -> str:
        """Format the message for logs: route, TTL, sequence number, key and a decoded description.

        A device-key message may be a key refresh carrying new keys: `describe` shows nothing undecoded of it.
        """
        return (
            f"{self.src:04X}→{self.dst:04X} ttl={self.ttl} seq={self.seq:06X} [{self.key}]"
            f" {describe(self.access_pdu, devkey=self.key.startswith('dev:'))}"
        )


def _log_orphaned_write(task: asyncio.Task[None]) -> None:
    """Retrieve what a write whose caller was cancelled raised (`ProxyClient._write`), so nothing is left unread."""
    if not task.cancelled() and (err := task.exception()) is not None:
        log.debug("proxy write finished after its caller left: %s", err)


class _Lazy:
    """A log argument rendered only when the record is (review-3 T9: `describe` ran for every send at any level)."""

    __slots__ = ("render",)

    def __init__(self, render: Callable[[], str]) -> None:
        self.render = render

    def __str__(self) -> str:
        return self.render()


@dataclass(frozen=True)
class Heartbeat:
    """A Heartbeat transport control message (§3.6.7): a node saying "alive" to the address it was configured with.

    `init_ttl` is the TTL it left the node with, `ttl` the TTL it arrived with, so `hops` is the relay count on
    the way to our proxy; `features` are the node's active features (bit 0 relay, 1 proxy, 2 friend, 3 low power).
    """

    src: int
    dst: int
    init_ttl: int
    ttl: int
    features: int
    received: float = field(default_factory=_now)

    @property
    def hops(self) -> int:
        """Relays between the node and our proxy (0 = the proxy heard it directly)."""
        return max(self.init_ttl - self.ttl, 0)


HEARTBEAT_OPCODE = 0x0A


def _retrieve(fut: asyncio.Future[Any]) -> None:
    """Mark a waiter's failed future as read once its request is over.

    A link loss fails every waiter's future (`_release_link`) while the send itself may raise its own
    `ConnectionError` first; nobody then reads the future's, and asyncio reports "Future exception was never
    retrieved" at ERROR when it is collected.
    """
    if fut.done() and not fut.cancelled():
        fut.exception()


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
        # a key refresh the mesh went through (review-3 N2b): the new NetKey and the phase (1 or 2 while it runs,
        # 3 once the old key is revoked but the export still holds it) with the proof that moved it there (review-4
        # D4), learnt from the provisioner's messages (`ProxyClient._follow_key_refresh`); None when there is none
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
        """Write `data` to `path` atomically and owner-only (`fileio.atomic_write`): a temp file, fsynced, renamed over.

        `os.replace` alone only guarantees a reader never observes a torn file; without an fsync first, a
        crash right after the rename can still leave the new name pointing at content the filesystem never
        wrote out to disk (the delayed-allocation behaviour that made the old truncate-then-write pattern
        leave an empty file after a power loss, one rename later). Owner-only because the record holds the new
        NetKey while a key refresh is followed (`to_stored`); the umask's mode left it world-readable.
        """
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

        A debounced write lost the new key to a crash right after it was learnt (review-4 S4-9): the mesh had moved
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

        Timing (review-4 D10): a state that knows its index keeps the spec's minimum times, on the wall clock (`now`,
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


@dataclass
class _AckState:
    block: int = 0
    cancelled: bool = False  # BlockAck 0: the receiver cannot take the message (§3.5.3.3), stop sending it
    event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class _TxKey:
    """The upper-transport key of an outgoing message: AppKey 0, or the device key of the node addressed.

    Everything else on the send path (sequence numbers, IV index, segmentation, acks) is the same for both;
    only the nonce type and the AKF/AID bits of the lower transport header differ (§3.5.2, §3.8.5).
    """

    key: bytes = field(repr=False)  # key material never in logs or error text
    nonce: int  # NONCE_APP | NONCE_DEVICE
    akf: bool
    aid: int
    label: str  # 'app0' | 'dev:<addr>', as in AccessMessage.key
    # the NetKey the network layer is sealed with; None = the one we transmit with when it is written (`nk`)
    net: NetKeyMaterial | None = field(default=None, repr=False)

    @property
    def devkey(self) -> bool:
        """Whether this is a device key (the message may carry key material: `describe` hides what it cannot decode)."""
        return self.nonce == NONCE_DEVICE


class ProxyClient:
    """The mesh client above a GATT proxy link: filters, beacons, crypto, segmentation, request/response."""

    def __init__(
        self,
        cdb: CDB,
        state: LocalState,
        ttl: int = 5,
        on_message: Callable[[AccessMessage], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        on_beacon: Callable[[SecureNetworkBeacon], None] | None = None,
        on_filter_status: Callable[[int], None] | None = None,
        on_undecryptable: Callable[[], None] | None = None,
        on_heartbeat: Callable[[Heartbeat], None] | None = None,
        on_key_refresh: Callable[[int, NetKeyMaterial], None] | None = None,
        on_foreign_own_source: Callable[[int, int], None] | None = None,
    ) -> None:
        """Set up for the first NetKey/AppKey of `cdb`; nothing is connected until `attach()`.

        Callbacks (all optional, all called from the notification handler): `on_message` for every decoded access
        message, `on_disconnect` when the link is lost, `on_beacon` for every Secure Network Beacon (authenticated
        or not), `on_filter_status(proxy_unicast)` when the proxy's Filter Status reveals which node we are
        connected to (`proxy_addr`), `on_undecryptable` for every network PDU our keys cannot open
        (`rx_undecryptable` counts them per link — the signature of an export whose keys are stale), and
        `on_heartbeat` for every Heartbeat control message the proxy forwards (`Heartbeat`), `on_key_refresh(phase,
        new_key)` when the mesh's key refresh moves (`_follow_key_refresh`; phase 0 = completed, the new key is the
        only one now), `on_foreign_own_source(iv_index, seq)` when a PDU from our own address carries a number we
        never handed out — another client uses the address (`_on_own_source`; at most once per
        `FOREIGN_SOURCE_REPORT_INTERVAL` per link, with the highest number seen so far).
        """
        self.cdb = cdb
        self.state = state
        self.ttl = ttl
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self.on_beacon = on_beacon
        self.on_filter_status = on_filter_status
        self.on_undecryptable = on_undecryptable
        self.on_heartbeat = on_heartbeat
        self.on_key_refresh = on_key_refresh
        self.on_foreign_own_source = on_foreign_own_source
        # the NetKeys derived so far (`_net_key`), pruned to those we accept whenever the key refresh moves
        self._net_keys: dict[bytes, NetKeyMaterial] = {}
        self._kr = self._resume_key_refresh(cdb, state)
        self.ak = cdb.app_keys[0]
        self.client: Any = None
        self.proxy_addr: int | None = (
            None  # unicast of the proxy node, learnt from its Filter Status
        )
        self.connected_at: float | None = None
        self.last_rx = 0.0  # monotonic time the proxy last delivered anything (`StandaloneLink` silence watchdog)
        self.rx_undecryptable = 0  # since attach(): network PDUs that failed NID / MIC / upper-transport decryption
        # on the current link: Set Filter Type requests actually written — none means the proxy was never
        # asked, so a missing Filter Status says nothing about it discarding our PDUs (the HA hub's watchdog)
        self.filter_writes = 0
        # on the current link: the highest (IV index, SEQ) from our own address we never handed out — proof that
        # another client sends from it (`_on_own_source`) — and when it was last reported (monotonic)
        self.foreign_own_source: tuple[int, int] | None = None
        self._foreign_reported_at: float | None = None
        # on the current link: proxy configuration PDUs dropped (not CTL=1 / DST=0, undecryptable, or a replay of
        # one already taken), and the (IV index, SEQ) of the last one taken (review-4 P4-7)
        self.rx_proxy_config_dropped = 0
        self._proxy_config_last: tuple[int, int] | None = None
        self._reasm = ProxyReassembler()
        self._segments: dict[tuple[int, int], dict[str, Any]] = {}
        # source -> (IV index, SeqAuth) of its last segmented message delivered: a retransmission of it (fresh
        # sequence numbers, the sender missed our ack) after its reassembly expired is not delivered twice
        self._seq_auth_done: dict[int, tuple[int, int]] = {}
        self._waiters: list[
            tuple[Callable[[AccessMessage], bool], asyncio.Future[AccessMessage]]
        ] = []
        self._ack_waiters: dict[tuple[int, int], _AckState] = {}
        self._send_lock = asyncio.Lock()
        self._sar_locks: dict[
            int, asyncio.Lock
        ] = {}  # one segmented message per destination (`_sar_lock`)
        self._write_lock = (
            asyncio.Lock()
        )  # one proxy PDU's SAR frames must not interleave with another's
        self._tasks: set[asyncio.Task[None]] = (
            set()
        )  # segment acks / filter re-sends in flight
        self._filter_type: int | None = (
            None  # proxy filter we asked for on this link, once it was sent
        )
        # the notification handler subscribed on the current link (`attach`); a released or replaced client's
        # handler finds it changed and drops what it still delivers
        self._link_notify: Callable[[Any, bytearray], None] | None = None
        self._beacon_seen = asyncio.Event()
        self._filter_acked = (
            asyncio.Event()
        )  # a Filter Status arrived since the last Set Filter Type
        self._filter_task: asyncio.Task[None] | None = (
            None  # the one filter (re-)send running in the background
        )
        self._ready = (
            asyncio.Event()
        )  # set once attach() is through (notifications, proxy filter)
        self._dev_key_of_element = {
            e.address: n.dev_key for n in cdb.nodes for e in n.elements
        }
        # the JUNG devices (a product id; the phones in `nodes[]` have none): their primary elements are what a
        # provisioner configures and what answers, their elements never send a provisioner's request (`_follow_key_refresh`)
        self._device_primaries: set[int] = set()
        self._device_elements: set[int] = set()
        for n in cdb.nodes:
            self._note_device(n)

    def _note_device(self, node: Node) -> None:
        if node.pid is not None:
            self._device_primaries.add(node.unicast)
            self._device_elements.update(e.address for e in node.elements)

    def _resume_key_refresh(self, cdb: CDB, state: LocalState) -> KeyRefreshFollower:
        """Take up a key refresh in progress: the one the export was written in, then the one `state` followed.

        A stored Phase 2 or 3 without its proof is a candidate again (`KeyRefreshFollower.resume`).
        """
        current = cdb.net_keys[NET_KEY_INDEX]
        self._net_keys[current.key] = current
        exported: tuple[bytes, int] | None = None
        if (refresh := cdb.net_key_refresh.get(NET_KEY_INDEX)) is not None:
            # the export was written mid key refresh: its `key` is the new one, the network may still use the old
            old, phase = refresh
            self._net_keys[old.key] = old
            exported, current = (current.key, phase), old
        follower, keep = KeyRefreshFollower.resume(
            current.key, exported, state.key_refresh
        )
        if not keep:
            state.set_key_refresh(None)
        return follower

    def _net_key(self, key: bytes) -> NetKeyMaterial:
        """Return the derived NetKey material of `key` (derived once)."""
        if (material := self._net_keys.get(key)) is None:
            material = self._net_keys[key] = NetKeyMaterial.derive(key)
        return material

    @property
    def key_refresh_phase(self) -> int:
        """0 = normal operation, 1 = a new NetKey accepted, 2 = transmitting with the proven new key."""
        return self._kr.phase

    @property
    def nk(self) -> NetKeyMaterial:
        """The NetKey we transmit with: the new one from a proven key refresh Phase 2 on, the export's before (§3.10.4.1)."""
        return self._net_key(self._kr.tx_key)

    @property
    def rx_net_keys(self) -> tuple[NetKeyMaterial, ...]:
        """The NetKeys we accept: the export's (its old one mid key refresh), and during a refresh the new ones too."""
        return tuple(self._net_key(k) for k in self._kr.rx_keys)

    @property
    def key_refresh_target(self) -> tuple[int, bytes] | None:
        """The phase (1, 2 or 3) of the followed key refresh that is proven, with its new key; None when none is.

        What Home Assistant may take the nodes only it knows to (`KeyRefreshFollower.distribution`, review-4 D11):
        never a key one node's word alone gave.
        """
        return self._kr.distribution

    def add_node(self, node: Node) -> None:
        """Know a node the export does not have yet (one just provisioned): its device key, its elements."""
        if node not in self.cdb.nodes:
            self.cdb.nodes.append(node)
        for element in node.elements:
            self._dev_key_of_element[element.address] = node.dev_key
        self._note_device(node)

    def remove_node(self, node: Node) -> None:
        """Forget a node `add_node` made known (one reset before any export recorded it); the others stay as they are."""
        self.cdb.nodes[:] = [n for n in self.cdb.nodes if n is not node]
        for element in node.elements:
            self._dev_key_of_element.pop(element.address, None)
            if (owner := self.cdb.node_by_addr(element.address)) is not None:
                self._dev_key_of_element[element.address] = owner.dev_key

    def forget_sources(self, addresses: Iterable[int]) -> None:
        """Forget what the replay protection holds for `addresses`: a node newly placed there starts at SEQ 0.

        Only for addresses the caller has just handed to a new node (provisioning): the entries are those of a
        node that sent from them before — reset since — and would drop every message of the new one until its
        sequence numbers passed the old one's (§3.8.8). The shorter replay list is persisted (`LocalState.persist`).
        """
        gone = set(addresses)
        for src in gone:
            self._seq_auth_done.pop(src, None)
        if gone & self.state.rpl.keys():
            self.state.rpl = {s: e for s, e in self.state.rpl.items() if s not in gone}
            self.state.persist()

    @property
    def beacon_seen(self) -> bool:
        """Whether an authenticated Secure Network Beacon arrived on the current link (cleared by every attach)."""
        return self._beacon_seen.is_set()

    def __repr__(self) -> str:
        """Link summary only — never the keys (`repr()` of a client may end up in a traceback or a diagnostics dump)."""
        proxy = f"{self.proxy_addr:04X}" if self.proxy_addr is not None else None
        return (
            f"ProxyClient(src={self.state.src:04X}, proxy={proxy}, connected={self.connected},"
            f" nid={self.nk.nid}, aid={self.ak.aid})"
        )

    @property
    def rx_garbage(self) -> int:
        """Since attach(): proxy PDUs dropped before authentication because they outgrew `PROXY_PDU_MAX`.

        A proxy that streams oversize SAR continuations is either broken or hostile (it only needs the public
        Network ID to be connected to); the application can read this next to `rx_undecryptable`.
        """
        return self._reasm.dropped

    # ------------------------------------------------------------------ discovery helpers
    def classify_service_data(self, sd: bytes) -> tuple[str, int | None] | None:
        """Interpret Mesh Proxy service data (0x1828). Returns (kind, node_addr) if it belongs to our network."""
        if not sd:
            return None
        keys = (
            self.rx_net_keys
        )  # during a key refresh, proxies advertise either key's identity
        if sd[0] == 0x00 and any(sd[1:9] == k.network_id for k in keys):
            return "network-id", None
        if sd[0] == 0x01 and len(sd) >= 17:
            h, rnd = sd[1:9], sd[9:17]
            for n in self.cdb.nodes:
                if any(k.node_identity_hash(rnd, n.unicast) == h for k in keys):
                    return "node-identity", n.unicast
        return None

    # ------------------------------------------------------------------ link management
    @property
    def connected(self) -> bool:
        """Whether a client is attached and (if it reports it) still connected."""
        return self.client is not None and bool(
            getattr(self.client, "is_connected", True)
        )

    @property
    def ready(self) -> bool:
        """Whether `attach()` is through: connected, notifications subscribed and the proxy filter requested.

        `connected` turns True as soon as `attach()` has the client (the HA hub relies on that meaning); a
        request sent before `ready` can miss its reply, or group statuses the default filter still drops.
        """
        return self._ready.is_set() and self.connected

    @property
    def mtu(self) -> int:
        """ATT MTU of the current link, re-read on every use.

        BlueZ still reports 23 right after connecting and only learns the negotiated value (247 with JUNG nodes)
        a little later.
        """
        return (
            (getattr(self.client, "mtu_size", None) or 23)
            if self.client is not None
            else 23
        )

    async def attach(
        self, client: Any, filter_blacklist: bool = True, beacon_wait: float = 0.0
    ) -> None:
        """Take over an already-connected bleak-style client (has write_gatt_char/start_notify/mtu_size).

        With `beacon_wait` > 0 the first filter request waits up to that long for the proxy's Secure Network Beacon
        (sent right after the subscription) so it is encrypted with the network's current IV index rather than the
        stored one. Whatever fails after the hand-over releases the client again — a half-attached connection would
        otherwise hold one of the (few) connection slots of the adapter or ESPHome proxy for good.

        A client still attached (a transport that reported `is_connected` False without its disconnected callback)
        is released first — its waiters fail, its tasks stop, it is disconnected — so nothing of the old link is
        carried over to the new one.
        """
        if self.client is not None:
            log.debug(
                "attach over a still-attached client: releasing %s",
                getattr(self.client, "address", "?"),
            )
            old = self._release_link("re-attached")
            if old is not client:
                await self._disconnect(old)
            else:
                await self._stop_notify(old)  # subscribed again below
        self._ready.clear()  # a previous attach whose link dropped mid-way may have left it set
        self.client = client
        self._reasm = ProxyReassembler()
        self._filter_type = None
        self._beacon_seen.clear()
        self._filter_acked.clear()
        self.proxy_addr = (
            None  # the previous link's node until this proxy's Filter Status arrives
        )
        self._reset_link_counters()
        self.connected_at = self.last_rx = (
            time.monotonic()
        )  # a fresh link counts as heard from

        def on_notify(char: Any, data: bytearray) -> None:
            # a client we released (or re-attached, whose old subscription a transport may keep) can still
            # deliver: its PDUs must not set the proxy address, advance the replay list or feed our reassembly
            if self._link_notify is on_notify:
                self._on_notify(char, data)

        self._link_notify = on_notify
        try:
            # bounded like every GATT call (review-4 R4-11): a subscription that never completes would hold the
            # caller's connection loop — and the connection slot — for good
            await asyncio.wait_for(
                client.start_notify(MESH_PROXY_DATA_OUT, on_notify), GATT_TIMEOUT
            )
            log.info(
                "attached to proxy %s, MTU %d",
                getattr(client, "address", "?"),
                self.mtu,
            )
            if filter_blacklist:
                if beacon_wait > 0:
                    await self._wait_for_beacon(beacon_wait)
                try:
                    await self.set_filter(FILTER_BLACKLIST)
                except SequenceStalled as err:
                    # back-pressure (HAState: the store's last save has not landed — a connect-time beacon that
                    # moved the IV index forces one), not exhaustion: without a filter the proxy stays on its
                    # default whitelist and every group publication is lost for the whole link (review-3 T2)
                    log.info("proxy filter held back (%s): sending it shortly", err)
                    self._filter_type = FILTER_BLACKLIST
                    self._start_filter_task(
                        FILTER_BLACKLIST, "sequence numbers were held back"
                    )
                except SequenceExhausted:
                    # stay attached, receive-only: the docstring promises recovery once a beacon moves the
                    # transmit index and resets SEQ, but that beacon is never delivered if attach() detaches
                    # first. `_filter_type` is only a hint for `_on_notify`'s re-send — nothing else may read
                    # it as "the filter is actually in place".
                    log.warning(
                        "sequence space exhausted: staying attached to learn the IV index from the proxy's beacon"
                    )
                    self._filter_type = FILTER_BLACKLIST
                else:
                    # a beacon that moved the IV index during the write has a re-send running already
                    running = (
                        self._filter_task is not None and not self._filter_task.done()
                    )
                    if not self._filter_acked.is_set() and not running:
                        self._start_filter_task(FILTER_BLACKLIST, None, sent=1)
            if self.client is client:  # the link may have dropped during the attach
                self._ready.set()
        except BaseException:
            log.debug(
                "attach to %s failed, releasing the connection",
                getattr(client, "address", "?"),
            )
            await self.detach()
            raise

    def _reset_link_counters(self) -> None:
        """Start the per-link counts and marks over (`attach`): what the last link saw says nothing about this one."""
        self.rx_undecryptable = 0
        self.rx_proxy_config_dropped = 0
        self._proxy_config_last = None
        self.foreign_own_source = self._foreign_reported_at = None

    async def _wait_for_beacon(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._beacon_seen.wait(), timeout)
        except asyncio.TimeoutError:
            log.debug(
                "no beacon within %.1fs, sending the proxy filter with the stored IV index",
                timeout,
            )

    async def detach(self, disconnect: bool = True) -> None:
        """Drop the attached client (disconnecting it unless told not to) and cancel background work.

        Pending requests and segment acknowledgements fail with `ConnectionError`, exactly as on a lost link. A
        client kept connected is unsubscribed from Mesh Proxy Data Out (best effort; whatever it still delivers
        is dropped either way).
        """
        client = self._release_link("proxy detached")
        if client is None:
            return
        if not disconnect:
            await self._stop_notify(client)
            return
        await self._disconnect(client)

    @staticmethod
    async def _disconnect(client: Any) -> None:
        """Disconnect `client`, best effort and bounded by `GATT_TIMEOUT` (review-4 R4-11).

        A transport whose disconnect never returns (a stuck BlueZ or ESPHome proxy call) must not hold up the
        caller: the link is released already, and the transport's own timeout ends the connection eventually.
        Unverified on air.
        """
        try:
            await asyncio.wait_for(client.disconnect(), GATT_TIMEOUT)
        except TimeoutError:
            log.warning(
                "disconnecting %s did not complete within %gs; leaving it",
                getattr(client, "address", "?"),
                GATT_TIMEOUT,
            )
        except Exception:
            log.debug("disconnect failed", exc_info=True)

    @staticmethod
    async def _stop_notify(client: Any) -> None:
        """Unsubscribe `client` from Mesh Proxy Data Out, best effort and bounded by `GATT_TIMEOUT`."""
        try:
            await asyncio.wait_for(
                client.stop_notify(MESH_PROXY_DATA_OUT), GATT_TIMEOUT
            )
        except Exception:
            log.debug("stop_notify failed", exc_info=True)

    def _release_link(self, reason: str) -> Any:
        """Forget the current client and fail everything waiting on it; return the client for the caller to close."""
        client, self.client = self.client, None
        self._link_notify = None
        self._ready.clear()
        self.connected_at = None
        self.proxy_addr = None
        self._filter_type = None
        self.filter_writes = 0  # the next link starts with none
        self._cancel_tasks()
        for _, fut in self._waiters:
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        self._waiters.clear()
        for acked in self._ack_waiters.values():
            acked.event.set()  # wake the sender: its next write fails with "not connected"
        self._ack_waiters.clear()
        self.state.flush()  # replay-list entries a receive-only link collected
        return client

    def handle_disconnected(self, client: Any = None) -> None:
        """Call from the transport's disconnected callback.

        Pass the client it fired for when the transport provides it: a late callback from a client we already
        gave up on must not tear down the current link.
        """
        if client is not None and client is not self.client:
            log.debug(
                "ignoring disconnect of a client that is no longer ours (%s)",
                getattr(client, "address", "?"),
            )
            return
        log.warning("proxy link lost")
        self._release_link("proxy disconnected")
        if self.on_disconnect:
            self.on_disconnect()

    async def set_filter(self, filter_type: int) -> None:
        """Proxy Configuration 'Set Filter Type' (§6.5.1). Blacklist + empty list = receive everything.

        The proxy's connect-time beacon often lands while the first filter write is in flight (a GATT write
        yields to the loop); if it moved the IV index meanwhile the request just written was encrypted with a
        stale one and silently dropped, so it is sent again with the current index before returning.

        Reserved and written under `_send_lock` like any send: a segmented round writes the numbers it reserved
        one segment at a time, and a request that took its number meanwhile would reach the air between them.
        Without a link it fails before taking a number (`_refuse_unlinked`).
        """
        while True:
            async with self._send_lock:
                self._refuse_unlinked()
                iv = self.state.tx_iv_index
                pdu = network_encrypt(
                    self.nk,
                    iv,
                    ctl=True,
                    ttl=0,
                    seq=self.state.next_seq(),
                    src=self.state.src,
                    dst=0x0000,
                    transport_pdu=proxy_config_set_filter(filter_type),
                    proxy=True,
                )
                self._filter_acked.clear()
                await self._write(PROXY_CONFIG, pdu)
                self.filter_writes += 1
            self._filter_type = filter_type
            if self.state.tx_iv_index == iv:
                break
            log.info("IV index changed during the proxy filter write, sending it again")
        await asyncio.sleep(0.3)

    def _start_filter_task(
        self, filter_type: int, reason: str | None, sent: int = 0
    ) -> None:
        """Run `_resend_filter` in the background, replacing one still running (one filter conversation per link)."""
        if self._filter_task is not None and not self._filter_task.done():
            self._filter_task.cancel()
        task = asyncio.get_running_loop().create_task(
            self._resend_filter(filter_type, reason, sent)
        )
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        self._filter_task = task

    async def _resend_filter(
        self,
        filter_type: int,
        reason: str | None = "IV index changed",
        sent: int = 0,
    ) -> None:
        """Make sure the proxy took the filter: send it (again) until a Filter Status answers it.

        The proxy answers every Set Filter Type it accepts with a Filter Status (§6.6.3), so that status is the
        acknowledgement. A request encrypted with a stale IV index, or dropped over the air, leaves the proxy on
        its default whitelist (unicast-only reception — every group publication is lost), so it is sent up to
        `FILTER_SET_TRIES` times per call, `FILTER_ACK_TIMEOUT` apart. `sent` counts requests already written
        (attach's own), which are waited for first.

        A subclass whose `reserve_seq` holds numbers back until its store is durably written (`HAState`,
        `SequenceStalled`) refuses right after an IV change or at a link's first send: that refusal is retried
        every `FILTER_RESEND_WAIT` up to `FILTER_RESEND_TRIES` times rather than given up on at once.
        """
        if reason is not None:
            log.info("%s, re-sending the proxy filter", reason)
        stalls = 0
        while True:
            if sent:
                try:
                    await asyncio.wait_for(
                        self._filter_acked.wait(), FILTER_ACK_TIMEOUT
                    )
                    return
                except asyncio.TimeoutError:
                    if sent >= FILTER_SET_TRIES:
                        log.warning(
                            "the proxy answered none of %d proxy filter requests: it may be discarding our "
                            "messages (stale sequence number, or our address used by another client)",
                            sent,
                        )
                        return
                    log.info(
                        "no Filter Status from the proxy, sending the proxy filter again"
                    )
            try:
                await self.set_filter(filter_type)
            except SequenceStalled as err:
                stalls += 1
                if stalls >= FILTER_RESEND_TRIES:
                    log.warning("proxy filter re-send failed: %s", err)
                    return
                await asyncio.sleep(FILTER_RESEND_WAIT)
                continue
            except ConnectionError as err:
                log.log(
                    logging.WARNING
                    if isinstance(err, SequenceExhausted)
                    else logging.DEBUG,
                    "proxy filter re-send failed: %s",
                    err,
                )
                return
            sent += 1

    def _refuse_unlinked(self) -> None:
        """Fail a send that has no link before it takes a sequence number; called under `_send_lock`.

        `next_seq` / `reserve_seq` persist what they hand out, and `_write` only finds the missing link after
        that: every send while unlinked (a background reader, a keep-alive) used a number for nothing, and one
        after the application closed its store reopened it (a subclass's clean-close mark). The check sits under
        the lock, right before the reservation, so the order of reserving and writing stays as it was.
        """
        if self.client is None:
            raise ConnectionError("not connected to a proxy")

    async def _write(self, msg_type: int, payload: bytes) -> None:
        """Write one proxy PDU, all of its SAR frames on the one link that was current when the write lock came.

        Shielded from the caller's cancellation (review-3 T8): a PDU cut off between its SAR frames leaves the
        proxy reassembling it, and it takes the next PDU's frames for the rest — both lost, the next one to a
        different message. A cancelled caller stops waiting; the frames still go out, and what their write
        raises then is only logged.
        """
        # the lock is taken here, in the caller's order, and handed to the write: the air must see our PDUs in
        # the order their sequence numbers were reserved
        await self._write_lock.acquire()
        if self.client is None or len(payload) + 1 <= self.mtu - 3:
            # no link (fails at once) or one frame: nothing to cut in half
            await self._write_frames(msg_type, payload)
            return
        task = asyncio.get_running_loop().create_task(
            self._write_frames(msg_type, payload)
        )
        task.add_done_callback(_log_orphaned_write)
        await asyncio.shield(task)

    async def _write_frames(self, msg_type: int, payload: bytes) -> None:
        """Write the SAR frames of one proxy PDU; entered holding the write lock (`_write`), which it releases."""
        try:
            try:
                client = self.client
                if client is None:
                    raise ConnectionError("not connected to a proxy")
                for frame in proxy_frame(msg_type, payload, self.mtu - 3):
                    if self.client is not client:
                        raise ConnectionError("proxy link changed during the write")
                    try:
                        await asyncio.wait_for(
                            client.write_gatt_char(
                                MESH_PROXY_DATA_IN, frame, response=False
                            ),
                            GATT_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        # a write without response that never completes (a stalled link, a transport whose
                        # buffers never drain): report it like a lost link rather than hold the locks forever
                        raise ConnectionError(
                            f"proxy write not completed within {GATT_TIMEOUT:g}s"
                        ) from None
            finally:
                self._write_lock.release()
        except ConnectionError:
            raise
        except Exception as err:  # transport-specific failures (bleak.BleakError, OSError, …) → one type for callers
            raise ConnectionError(f"proxy write failed: {err}") from err

    # ------------------------------------------------------------------ background tasks
    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run a coroutine in the background, keeping a reference until it finishes.

        A bare create_task may be garbage collected mid-way; failures are logged, never raised into the
        notification callback.
        """
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("background task failed", exc_info=task.exception())

    def _cancel_tasks(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    # ------------------------------------------------------------------ sending
    @property
    def _app_key(self) -> _TxKey:
        return _TxKey(self.ak.key, NONCE_APP, True, self.ak.aid, "app0")

    def _dev_key(self, node_unicast: int) -> _TxKey:
        """Device key of the node whose *primary* element is `node_unicast` (the Config Server lives there)."""
        node = self.cdb.node_by_addr(node_unicast)
        if node is None:
            raise ValueError(f"{node_unicast:04X} is not an element of any known node")
        if node.unicast != node_unicast:
            raise ValueError(
                f"{node_unicast:04X} is a secondary element of node {node.unicast:04X};"
                " Config messages go to the primary unicast"
            )
        return _TxKey(node.dev_key, NONCE_DEVICE, False, 0, f"dev:{node_unicast:04X}")

    async def send_access(
        self, dst: int, access_pdu: bytes, ttl: int | None = None
    ) -> int:
        """Send an access message with AppKey 0 (segmented automatically above 11 bytes). Returns first seq used."""
        return await self._send(dst, access_pdu, ttl, self._app_key)

    async def send_config(
        self, node_unicast: int, access_pdu: bytes, *, ttl: int | None = None
    ) -> int:
        """Send a Config Server message to a node, encrypted with its device key. Returns first seq used.

        `node_unicast` must be the primary unicast of a node in the CDB (ValueError otherwise). Device nonce
        (type 0x02), AKF 0, AID 0, master credentials; segmented above 11 bytes exactly like `send_access`
        (Publication Set and every Composition Data Status are), with SZMIC 0.
        """
        return await self._send(
            node_unicast, access_pdu, ttl, self._dev_key(node_unicast)
        )

    async def _send(
        self,
        dst: int,
        access_pdu: bytes,
        ttl: int | None,
        key: _TxKey,
        on_locked: Callable[[], None] | None = None,
    ) -> int:
        """Send under `_send_lock`; `on_locked` runs once the lock is taken, right before anything is written.

        A request registers its reply waiter there: registered before the lock, a status the node published
        while the request still queued (behind another send, say) would pass for its answer.

        The lock makes reserving sequence numbers and writing them one step: the nodes' replay protection drops a
        number lower than one they already accepted from us, so what is reserved first must reach the air
        first; segment acks (`_send_ack`) and proxy filter requests (`set_filter`) take it too. A segmented message
        holds it only while one round of segments is reserved and written, not while it waits for the
        acknowledgement (up to seconds per round from an absent node — review-3 T4: every light command queued
        behind it); segmented messages to one destination go one at a time (`_sar_lock`).
        """
        ttl = self.ttl if ttl is None else ttl
        if len(access_pdu) > 11:
            async with self._sar_lock(dst):
                return await self._send_segmented(dst, access_pdu, ttl, key, on_locked)
        async with self._send_lock:
            self._refuse_unlinked()
            if on_locked is not None:
                on_locked()
            # the transmit IV index is read under the lock: a beacon may complete an IV Update while we queue
            iv = self.state.tx_iv_index
            seq = self.state.next_seq()
            upper = upper_encrypt(
                key.key, key.nonce, iv, seq, self.state.src, dst, access_pdu
            )
            net = network_encrypt(
                self.nk if key.net is None else key.net,
                iv,
                False,
                ttl,
                seq,
                self.state.src,
                dst,
                lower_unsegmented_access(key.aid, upper, akf=key.akf),
            )
            log.debug(
                "TX %04X→%04X seq=%06X [%s] %s",
                self.state.src,
                dst,
                seq,
                key.label,
                _Lazy(partial(describe, access_pdu, devkey=key.devkey)),
            )
            await self._write(PROXY_NETWORK_PDU, net)
            return seq

    def _sar_lock(self, dst: int) -> asyncio.Lock:
        """Return the lock that keeps segmented messages to `dst` one at a time (one SeqZero per destination)."""
        lock = self._sar_locks.get(dst)
        if lock is None:
            lock = self._sar_locks[dst] = asyncio.Lock()
        return lock

    async def _send_segmented(
        self,
        dst: int,
        access_pdu: bytes,
        ttl: int,
        key: _TxKey,
        on_locked: Callable[[], None] | None = None,
    ) -> int:
        """Send a segmented access message; returns its SeqAuth (the first sequence number).

        Every retransmission round re-reads the transmit IV index: when an IV Update completes in between, the
        upper transport PDU (encrypted under the old index and SeqAuth) can no longer be verified by anyone, so
        the message starts over under the new index with a fresh SeqAuth.
        """
        n_segments = -(
            -(len(access_pdu) + 4) // SEGMENT_SIZE
        )  # 4-byte TransMIC (SZMIC 0)
        for attempt in range(SEGMENT_RESTARTS + 1):
            seq0 = await self._send_segments_once(
                dst,
                access_pdu,
                ttl,
                key,
                n_segments,
                on_locked if attempt == 0 else None,
            )
            if seq0 is not None:
                return seq0
            if attempt < SEGMENT_RESTARTS:
                log.info(
                    "IV index changed during the segmented message to %04X, starting it over",
                    dst,
                )
        # the index keeps moving: something is badly wrong with what we hear
        raise TimeoutError(
            f"segmented message to {dst:04X} not acknowledged (IV index kept changing)"
        )

    async def _send_segments_once(
        self,
        dst: int,
        access_pdu: bytes,
        ttl: int,
        key: _TxKey,
        n_segments: int,
        on_locked: Callable[[], None] | None = None,
    ) -> int | None:
        """One segmented transmission with its retransmission rounds; None when the IV index moved in between.

        Each round reserves and writes its segments under `_send_lock` and waits for the acknowledgement
        outside it. `ConnectionError` when the link goes away meanwhile: `_release_link` wakes the ack wait like
        an ack would, and neither a "not acknowledged" nor another round's sequence numbers would be right for
        that.
        """
        link = self.client
        # released by `_segment_rounds` once the first round is written
        await self._send_lock.acquire()
        try:
            self._refuse_unlinked()
            if on_locked is not None:
                on_locked()
            iv = self.state.tx_iv_index
            # all of the first round's numbers, or SequenceExhausted
            seq0 = self.state.reserve_seq(n_segments)
            upper = upper_encrypt(
                key.key, key.nonce, iv, seq0, self.state.src, dst, access_pdu
            )
            segments = lower_segments_access(
                key.aid, seq0, upper, SEGMENT_SIZE, akf=key.akf
            )
            if len(segments) != n_segments:  # pragma: no cover — same arithmetic twice
                raise RuntimeError(
                    "segment count differs from the reserved sequence numbers"
                )
        except BaseException:
            self._send_lock.release()
            raise
        log.debug(
            "TX %04X→%04X seq=%06X [%s] segmented x%d %s",
            self.state.src,
            dst,
            seq0,
            key.label,
            n_segments,
            _Lazy(partial(describe, access_pdu, devkey=key.devkey)),
        )
        return await self._segment_rounds(dst, ttl, segments, link, iv, seq0, key.net)

    async def _segment_rounds(
        self,
        dst: int,
        ttl: int,
        segments: list[bytes],
        link: Any,
        iv: int,
        seq0: int,
        net: NetKeyMaterial | None = None,
    ) -> int | None:
        """Run the rounds of `_send_segments_once`; entered holding `_send_lock`, released after the first writes."""
        seq_zero = seq0 & 0x1FFF
        pending = set(range(len(segments)))
        ack_key = (dst, seq_zero)
        acked = _AckState()
        if is_unicast(dst):
            self._ack_waiters[ack_key] = acked
        try:
            for attempt in range(SEGMENT_RETRIES):
                if attempt:
                    await self._send_lock.acquire()
                try:
                    if attempt and self.state.tx_iv_index != iv:
                        return None
                    # before any number is reserved for a round
                    if self.client is not link:
                        raise ConnectionError(
                            "proxy link lost during the segmented message"
                        )
                    order = sorted(pending)
                    # one synchronous reservation: every number of this round belongs to `iv`'s sequence space,
                    # whatever beacon arrives while the writes below yield to the loop
                    if attempt == 0:
                        seqs = [seq0 + i for i in order]
                    else:
                        first = self.state.reserve_seq(len(order))
                        seqs = [first + k for k in range(len(order))]
                    acked.event.clear()  # before the writes: an ack may land while the last one is in flight
                    await self._write_segments(dst, ttl, iv, segments, order, seqs, net)
                finally:
                    self._send_lock.release()
                if not is_unicast(dst):
                    if attempt == 1:
                        return seq0  # groups: send everything twice, no acks
                    await asyncio.sleep(0.3)
                    continue
                await self._wait_for_acks(acked, pending, dst, attempt)
                if acked.cancelled:
                    raise TimeoutError(
                        f"segmented message to {dst:04X} cancelled by the receiver (BlockAck 0: busy or out of resources)"
                    )
                # whatever came in — during the writes, the wait or the grace period — counts
                pending = {i for i in pending if not (acked.block >> i) & 1}
                if not pending:
                    return seq0  # delivered, even if the link went away during the grace period
                if self.client is not link:
                    raise ConnectionError(
                        "proxy link lost during the segmented message"
                    )
        finally:
            self._ack_waiters.pop(ack_key, None)
        raise TimeoutError(f"segmented message to {dst:04X} not acknowledged")

    async def _write_segments(
        self,
        dst: int,
        ttl: int,
        iv: int,
        segments: list[bytes],
        order: list[int],
        seqs: list[int],
        net_key: NetKeyMaterial | None = None,
    ) -> None:
        """Write the segments `order` names, each with its sequence number from `seqs` (under `net_key`, else `nk`)."""
        for i, seq in zip(order, seqs, strict=True):
            net = network_encrypt(
                self.nk if net_key is None else net_key,
                iv,
                False,
                ttl,
                seq,
                self.state.src,
                dst,
                segments[i],
            )
            await self._write(PROXY_NETWORK_PDU, net)

    @staticmethod
    async def _wait_for_acks(
        acked: _AckState, pending: set[int], dst: int, attempt: int
    ) -> None:
        """Wait for a round's acknowledgement; when it leaves segments open, a short grace for late ones."""
        try:
            await asyncio.wait_for(acked.event.wait(), SEGMENT_ACK_TIMEOUT)
            if any(not (acked.block >> i) & 1 for i in pending):
                # grace: acks for later segments usually follow within ~50 ms
                await asyncio.sleep(0.25)
        except asyncio.TimeoutError:
            log.debug("no segment ack from %04X (attempt %d)", dst, attempt + 1)

    async def request(
        self,
        dst: int,
        access_pdu: bytes,
        expect_opcode: int,
        timeout: float = 3.0,
        retries: int = 3,
        expect_cid: int | None = None,
        quiet: bool = False,
        match: Callable[[AccessMessage], bool] | None = None,
    ) -> AccessMessage:
        """Send and wait for a status from dst (any source if dst is a group).

        `quiet` logs unanswered attempts at DEBUG instead of WARNING — for optional reads (a property the
        device may simply not have) where silence is an answer, not a fault.

        `timeout` is the wait for the reply of each of the `retries` attempts, counted from when its send is
        through: the wait for the send lock (other sends queued first) and the send itself come on top. Those are
        bounded separately — every GATT write by `GATT_TIMEOUT` (`ConnectionError` past it), a segmented send by
        its acknowledgement rounds — so a request never hangs, but it can take longer than `timeout` times `retries`.

        Matches on source + opcode only, not on destination: JUNG firmware answers a state-changing
        acknowledged Set solely by *publishing* the status to the element's group, and only sends a
        unicast reply when nothing changed (Get, or a retransmitted Set with the same TID).

        `match` narrows the reply further (a predicate over the decoded status — the scene number of a
        Scene Register Status, say) so a late reply to an earlier request to the same element cannot
        satisfy this one. Only the oldest waiter a status fits is resolved by it.
        """
        extra = match

        def match_status(m: AccessMessage) -> bool:
            return (
                m.opcode == expect_opcode
                and m.company_id == expect_cid
                and (not is_unicast(dst) or m.src == dst)
                and (extra is None or extra(m))
            )

        return await self._request(
            dst, access_pdu, self._app_key, match_status, timeout, retries, quiet=quiet
        )

    async def request_config(
        self,
        node_unicast: int,
        access_pdu: bytes,
        expect_opcode: int,
        *,
        timeout: float = 3.0,
        retries: int = 3,
        match: Callable[[AccessMessage], bool] | None = None,
        old_net_key: bool = False,
    ) -> AccessMessage:
        """Send a Config message to a node (device key, `send_config`) and wait for its Config status.

        The reply must come from the node's primary unicast, carry `expect_opcode` (a SIG opcode) and have
        been decrypted with that node's device key (`AccessMessage.key == 'dev:<addr>'`). `match` narrows it
        further — the element / address / model the status echoes, so a retransmitted duplicate of the
        previous step's status cannot pass for this one's.

        `old_net_key` seals the network layer with the key every node holds until a key refresh completes (the
        export's, `KeyRefreshFollower.current`) rather than the one we transmit with — they differ in a proven
        Phase 2 only: a node still waiting for its NetKey Update accepts nothing else (review-4 D11; relayed in
        Phase 2 under the key it came with, §3.10.4.1 — unverified on air).
        """
        key = self._dev_key(node_unicast)
        if old_net_key:
            key = replace(key, net=self._net_key(self._kr.current))
        extra = match

        def match_status(m: AccessMessage) -> bool:
            return (
                m.src == node_unicast
                and m.opcode == expect_opcode
                and m.company_id is None
                and m.key == key.label
                and (extra is None or extra(m))
            )

        return await self._request(
            node_unicast, access_pdu, key, match_status, timeout, retries
        )

    async def _request(
        self,
        dst: int,
        access_pdu: bytes,
        key: _TxKey,
        match: Callable[[AccessMessage], bool],
        timeout: float,
        retries: int,
        quiet: bool = False,
    ) -> AccessMessage:
        for attempt in range(retries):
            fut: asyncio.Future[AccessMessage] = (
                asyncio.get_running_loop().create_future()
            )
            try:
                await self._send(
                    dst,
                    access_pdu,
                    None,
                    key,
                    partial(self._waiters.append, (match, fut)),
                )
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                if fut.done() and not fut.cancelled() and fut.exception() is None:
                    # a segmented send whose acks got lost: the node applied it and answered all the same
                    return fut.result()
                log.log(
                    logging.DEBUG if quiet else logging.WARNING,
                    "no response from %04X (attempt %d/%d)",
                    dst,
                    attempt + 1,
                    retries,
                )
            finally:
                self._drop_waiter(fut)
                _retrieve(fut)
        raise TimeoutError(f"no response from {dst:04X}")

    async def collect(
        self, dst: int, access_pdu: bytes, expect_opcode: int, window: float = 2.0
    ) -> list[AccessMessage]:
        """Send to a group and collect one matching status per responding element within the window.

        `ConnectionError` if the link is lost during the window (like `request`; the future every waiter carries
        is what a link loss fails, so it is awaited here rather than left to report "never retrieved"). A
        `TimeoutError` of the send itself (a segmented message to a unicast that never acknowledged it) propagates:
        an empty list would read as "nobody answered" when nothing was asked.
        """
        got: list[AccessMessage] = []
        seen: set[int] = set()

        def match(m: AccessMessage) -> bool:
            if m.opcode == expect_opcode and m.src not in seen:
                seen.add(m.src)
                got.append(m)
            return False  # never resolves the future: only a link loss does

        fut: asyncio.Future[AccessMessage] = asyncio.get_running_loop().create_future()
        register = partial(self._waiters.append, (match, fut))
        try:
            # registered once the send lock is ours (`_send`): nothing published while queued is collected
            await self._send(dst, access_pdu, None, self._app_key, register)
            try:
                await asyncio.wait_for(fut, window)
            except asyncio.TimeoutError:
                pass  # the window elapsed: that is the normal end
        finally:
            self._drop_waiter(fut)
            _retrieve(fut)
        return got

    def _drop_waiter(self, fut: asyncio.Future[AccessMessage]) -> None:
        """Remove a request's waiter *in place* (review-3 T1).

        A request still queued for the send lock holds a bound `self._waiters.append`, so the list object must
        never be replaced.
        """
        self._waiters[:] = [w for w in self._waiters if w[1] is not fut]

    # ------------------------------------------------------------------ receiving
    def _on_notify(self, _char: Any, data: bytearray) -> None:
        if not data:
            return
        self.last_rx = time.monotonic()
        try:
            r = self._reasm.feed(bytes(data))
            if r is None:
                return
            msg_type, payload = r
            if msg_type == PROXY_NETWORK_PDU:
                self._on_network_pdu(payload)
            elif msg_type == PROXY_BEACON:
                b = self._parse_beacon(payload)
                if b:
                    log.info(
                        "beacon: iv_index=%d iv_update=%s key_refresh=%s auth=%s",
                        b.iv_index,
                        b.iv_update,
                        b.key_refresh,
                        b.authenticated,
                    )
                    if b.authenticated:
                        self._beacon_seen.set()
                        if self.state.apply_beacon(b.iv_index, b.iv_update):
                            log.warning(
                                "IV state now index=%d update_active=%s (tx index %d)",
                                self.state.iv_index,
                                self.state.iv_update_active,
                                self.state.tx_iv_index,
                            )
                            self._purge_rpl()
                            if self._filter_type is not None:
                                self._start_filter_task(
                                    self._filter_type, "IV index changed"
                                )
                    if b.key_refresh:
                        # Phase 2 beacons are secured with the *new* key (§3.10.4): one ours authenticates means
                        # ours are the new keys already; one it cannot is the only sign that they are being
                        # replaced (or a foreign beacon: the flag itself is not authenticated)
                        log.log(
                            logging.INFO if b.authenticated else logging.WARNING,
                            "key refresh in progress%s",
                            ""
                            if b.authenticated
                            else " (beacon not authenticated by our key): the exported keys may be being replaced",
                        )
                    if self.on_beacon:
                        self.on_beacon(b)
            elif msg_type == PROXY_CONFIG:
                self._on_proxy_config(payload)
            else:
                log.info("proxy msg type %d: %s", msg_type, payload.hex())
        except Exception:
            log.exception("error handling proxy PDU %s", data.hex())

    def _on_proxy_config(self, payload: bytes) -> None:
        """Take a proxy configuration PDU (§6.5): the Filter Status that names the proxy node and acknowledges our filter.

        It gets the checks every other PDU gets (review-4 P4-7): CTL=1 and DST unassigned, as §6.5 requires of
        these and only these, and no replay — an (IV index, SEQ) at or below the last one taken on this link is
        dropped. Without them a recorded Filter Status could be played back to set `proxy_addr` and stand in for
        the acknowledgement of a filter the proxy never took. What is dropped is counted
        (`rx_proxy_config_dropped`).
        """
        n = self._network_decrypt(payload, proxy=True)
        if n is None or not n.ctl or n.dst != 0x0000:
            self.rx_proxy_config_dropped += 1
            log.debug(
                "proxy configuration PDU dropped: %s",
                "not ours"
                if n is None
                else f"CTL={int(n.ctl)} DST={n.dst:04X} from {n.src:04X}",
            )
            return
        if self._proxy_config_last is not None and (
            (n.iv_index, n.seq) <= self._proxy_config_last
        ):
            self.rx_proxy_config_dropped += 1
            log.debug(
                "proxy configuration PDU from %04X seq %06X dropped: a replay",
                n.src,
                n.seq,
            )
            return
        self._proxy_config_last = (n.iv_index, n.seq)
        pdu = n.transport_pdu
        if len(pdu) >= 4 and pdu[0] == 0x03:
            self.proxy_addr = n.src
            self._filter_acked.set()
            log.info(
                "proxy filter status: type=%s list_size=%d (proxy node %04X)",
                "blacklist" if pdu[1] else "whitelist",
                int.from_bytes(pdu[2:4], "big"),
                n.src,
            )
            if self.on_filter_status:
                self.on_filter_status(n.src)
        elif pdu[0] == 0x03:
            log.debug(
                "proxy filter status from %04X too short (%d octets), dropped",
                n.src,
                len(pdu),
            )
        else:
            log.info("proxy config pdu %s", payload.hex())

    def _network_decrypt(
        self, payload: bytes, proxy: bool = False
    ) -> NetworkPDU | None:
        """Open a network PDU with any NetKey we accept (`rx_net_keys`); None when none opens it."""
        for key in self.rx_net_keys:
            n = network_decrypt(key, self.state.iv_index, payload, proxy=proxy)
            if n is not None:
                return n
        return None

    def _parse_beacon(self, payload: bytes) -> SecureNetworkBeacon | None:
        """Parse a Secure Network Beacon with every key we accept; the first that authenticates it wins.

        A beacon secured with a new key moves a key refresh on (§3.10.4.1): Key Refresh flag set = Phase 2,
        clear = Phase 3 (the old key is revoked). It comes from the proxy node itself, which beacons with the keys
        it uses: proof that the mesh moved (`KeyRefreshFollower.beacon`).
        """
        first: SecureNetworkBeacon | None = None
        for key in self.rx_net_keys:
            b = parse_beacon(key, payload)
            if b is None:
                return None
            if b.authenticated:
                if key.key != self._kr.current:
                    self._key_refresh_moved(self._kr.beacon(key.key, b.key_refresh))
                return b
            first = first or b
        return first

    def _follow_key_refresh(self, msg: AccessMessage) -> None:
        """Learn a key refresh from the provisioner's own messages (review-3 N2b; §3.10.4, §4.3.2.8, §4.3.2.46).

        The app refreshes the NetKey by sending every node a Config NetKey Update with the new key, then Config Key
        Refresh Phase Set 2 and 3 (`docs/android/transport-provisioning.md` §4.2). They are sealed with each node's
        device key, which the export gives us, and the blacklist filter forwards them: without following them the
        link goes deaf at Phase 2 and dead at Phase 3, until the entry is set up again from a new export.

        A node can seal the same messages with its own device key, so they are followed only as evidence (review-4
        D4, `keyrefresh`): a NetKey Update or Phase Set counts when it is addressed to a JUNG device's primary
        element, opened with *that* device's key and not sent from a device's element; a NetKey Status or Key
        Refresh Phase Status when a device's primary element sealed it with its own key. Only proof moves the
        refresh past Phase 1, never a request.
        """
        if not msg.key.startswith("dev:") or msg.company_id is not None:
            return
        p = msg.params
        moved: Moved | None = None
        if msg.opcode in (CONFIG_NETKEY_UPDATE, CONFIG_KEY_REFRESH_PHASE_SET):
            if len(p) < 3 or int.from_bytes(p[:2], "little") & 0x0FFF != NET_KEY_INDEX:
                return
            if not (
                msg.dst in self._device_primaries
                and msg.key == f"dev:{msg.dst:04X}"
                and msg.src not in self._device_elements
            ):
                log.log(
                    logging.WARNING
                    if msg.opcode == CONFIG_NETKEY_UPDATE
                    else logging.DEBUG,
                    "Config %s %04X→%04X not followed: it does not come from a provisioner",
                    CONFIG_NAMES[msg.opcode],
                    msg.src,
                    msg.dst,
                )
                return
            if msg.opcode == CONFIG_KEY_REFRESH_PHASE_SET:
                self._kr.requested(msg.dst, p[2])
            elif len(p) >= 18:
                moved = self._kr.learn(bytes(p[2:18]), msg.dst)
        elif msg.opcode in (CONFIG_NETKEY_STATUS, CONFIG_KEY_REFRESH_PHASE_STATUS):
            if (
                len(p) < 3
                or int.from_bytes(p[1:3], "little") & 0x0FFF != NET_KEY_INDEX
                or msg.src not in self._device_primaries
                or msg.key != f"dev:{msg.src:04X}"
            ):
                return
            if msg.opcode == CONFIG_NETKEY_STATUS:
                moved = self._kr.netkey_status(msg.src, p[0] == 0, self.proxy_addr)
            elif len(p) >= 4 and p[0] == 0:
                moved = self._kr.phase_status(msg.src, p[3], self.proxy_addr)
        else:
            return
        self._key_refresh_moved(moved)

    def _key_refresh_moved(self, moved: Moved | None) -> None:
        """Persist the followed refresh; when it moved, log how it was proven and tell the application."""
        self.state.set_key_refresh(self._kr.record())
        accepted = set(self._kr.rx_keys)
        for key in [k for k in self._net_keys if k not in accepted]:
            del self._net_keys[key]
        if moved is None:
            return
        new = self._net_key(moved.key)
        if moved.phase == 1 and moved.proof is None:
            log.warning(
                "the provisioner is refreshing the network key: following it (phase 1): the new key is accepted, "
                "not used until the mesh proves it moved"
            )
        elif moved.phase == 1:
            log.warning(
                "key refresh phase 1 proven (proof: %s): the new key is the provisioner's",
                describe_proof(moved),
            )
        else:
            log.warning(
                "key refresh %s (proof: %s): %s",
                "phase 2" if moved.phase == 2 else "complete",
                describe_proof(moved),
                "transmitting with the new network key"
                if moved.phase == 2
                else "the new network key is the only one now",
            )
            if self._filter_type is not None and self.client is not None:
                self._start_filter_task(self._filter_type, "network key changed")
        if self.on_key_refresh:
            try:
                self.on_key_refresh(moved.phase, new)
            except Exception:
                log.exception("on_key_refresh handler failed")

    def _on_network_pdu(self, payload: bytes) -> None:
        n = self._network_decrypt(payload)
        if n is None:
            log.debug("undecryptable network PDU %s", payload.hex())
            self._count_undecryptable()
            return
        if n.src == self.state.src:
            self._on_own_source(n)
            return
        try:
            kind = parse_lower(n.transport_pdu, n.ctl)
        except ValueError as err:  # authenticated with the NetKey, yet not a lower transport PDU: dropped, no trace
            log.debug(
                "RX %04X→%04X malformed lower transport PDU dropped: %s",
                n.src,
                n.dst,
                err,
            )
            return
        if kind[0] == "ctl":
            # the replay list covers control PDUs too (§3.8.8): a recorded Heartbeat would keep a dead node alive,
            # a recorded Segment Ack acknowledge a message the node never got
            iv = n.iv_index
            if self._is_replay(n.src, iv, n.seq):
                log.debug("replay from %04X seq %06X ignored (control)", n.src, n.seq)
                return
            op, p = kind[1], kind[2]
            if op == 0x00 and len(p) >= 6:
                self.state.note_received(n.src, iv, n.seq)
                hdr = int.from_bytes(p[:2], "big")
                seq_zero, block = (hdr >> 2) & 0x1FFF, int.from_bytes(p[2:6], "big")
                log.debug(
                    "RX %04X→%04X Segment Ack seq_zero=%d block=%08X",
                    n.src,
                    n.dst,
                    seq_zero,
                    block,
                )
                w = self._ack_waiters.get((n.src, seq_zero))
                if w and n.dst == self.state.src:
                    if block == 0:
                        w.cancelled = (
                            True  # §3.5.3.3: the receiver cancelled the message
                        )
                    w.block |= block
                    w.event.set()
            elif op == HEARTBEAT_OPCODE and len(p) >= 3:
                self.state.note_received(n.src, iv, n.seq)
                beat = Heartbeat(
                    n.src, n.dst, p[0] & 0x7F, n.ttl, int.from_bytes(p[1:3], "big")
                )
                log.debug(
                    "RX %04X→%04X Heartbeat init_ttl=%d hops=%d features=%04X",
                    n.src,
                    n.dst,
                    beat.init_ttl,
                    beat.hops,
                    beat.features,
                )
                if self.on_heartbeat:
                    try:
                        self.on_heartbeat(beat)
                    except Exception:
                        log.exception("on_heartbeat handler failed")
            else:
                log.debug("RX %04X→%04X control op %02X %s", n.src, n.dst, op, p.hex())
            return
        if kind[0] == "unseg":
            _, akf, aid, upper = kind
            self._deliver(n, akf, aid, upper, szmic=0, seq_auth=n.seq)
        elif kind[0] == "seg":
            self._on_segment(n, kind[1])

    def _on_own_source(self, n: NetworkPDU) -> None:
        """Take a PDU from our own address: ours echoed back by a relay or the proxy, or another client's (review-4 S I2).

        An echo carries a number we handed out (`_handed_out`) and is dropped silently, as always. Anything else
        proves a second client on our address — a second Home Assistant, the CLI on its address, a restored store —
        whose numbers ours will run into: every one we send that it has sent too reuses a nonce, and the nodes drop
        as replays whatever lies below its last. Reported (`on_foreign_own_source`, with the highest number seen on
        this link) at most once per `FOREIGN_SOURCE_REPORT_INTERVAL`, and dropped too.
        """
        seen = (n.iv_index, n.seq)
        if self._handed_out(*seen):
            return  # our own message echoed back by the proxy/relay
        if self.foreign_own_source is None or seen > self.foreign_own_source:
            self.foreign_own_source = seen
        now = _now()
        if (
            self._foreign_reported_at is not None
            and now - self._foreign_reported_at < FOREIGN_SOURCE_REPORT_INTERVAL
        ):
            return
        self._foreign_reported_at = now
        iv, seq = self.foreign_own_source
        log.warning(
            "a PDU from our own address %04X under IV index %d with sequence number %06X, which we never sent: "
            "another client uses this address",
            n.src,
            iv,
            seq,
        )
        if self.on_foreign_own_source:
            try:
                self.on_foreign_own_source(iv, seq)
            except Exception:
                log.exception("on_foreign_own_source handler failed")

    def _handed_out(self, iv: int, seq: int) -> bool:
        """Whether (`iv`, `seq`) may be a number our address sent, as far as `state` can tell.

        Under the transmit index everything below the counter was handed out. Under an older index (an echo from
        before an IV Update completed) everything below the counter and `seq_peak` may have been, from
        `seq_peak_from` on; below that the record knows nothing, so it may be ours. A newer index we have never
        transmitted under — unless `seq_guard` says the counter carried on under it (a lost record skipped past,
        an IV index gone back), where again everything below the counter may be ours.
        """
        state = self.state
        tx = state.tx_iv_index
        if iv == tx:
            return seq < state.seq
        if iv > tx:
            guard = state.seq_guard
            return (
                guard is not None
                and (guard == SEQ_GUARD_FIRST_BEACON or iv <= guard)
                and seq < state.seq
            )
        return iv < state.seq_peak_from or seq < max(state.seq, state.seq_peak)

    def _is_replay(self, src: int, iv: int, seq: int) -> bool:
        """Whether `seq` under `iv` is at or below the last sequence number accepted from `src` (§3.8.8).

        A new source the full replay list cannot take counts as a replay too (`LocalState.admits`): without an
        entry, nothing would stop its PDUs from being accepted again.
        """
        last = self.state.rpl.get(src)
        if last is None:
            return not self.state.admits(src)
        return (iv, seq) <= last

    def _purge_rpl(self) -> None:
        """Drop replay-list entries of IV indexes older than the two still receivable (current and current - 1).

        The list must survive an IV Update (§3.8.8): nodes keep sending under the old index for hours, so a PDU
        captured before the beacon would be accepted again if the list were simply cleared.
        """
        self.state.purge_rpl(self.state.iv_index - 1)

    def _on_segment(self, n: NetworkPDU, s: SegmentInfo) -> None:
        key = (n.src, s.seq_zero)
        now = time.monotonic()
        for k in [k for k, v in self._segments.items() if now - v["t"] > 10]:
            del self._segments[k]
        if s.seg_o > s.seg_n:
            log.debug(
                "RX %04X→%04X segment %d of %d: SegO beyond SegN, dropped",
                n.src,
                n.dst,
                s.seg_o,
                s.seg_n + 1,
            )
            return
        try:
            seq_auth = seq_auth_from(n.seq, s.seq_zero)
        except ValueError as err:
            log.debug("RX %04X→%04X segment dropped: %s", n.src, n.dst, err)
            return
        st = self._segments.get(key)
        if st is None:
            # The replay check belongs to the *start* of a reassembly (checking the completed message would drop a
            # late-completing one after acknowledging it). It is on the segment's own sequence number, as §3.8.8
            # checks every PDU: a recorded segment replayed later carries its old number, below the list, while
            # a retransmission carries a fresh one — checking the message's SeqAuth instead would lock a message
            # out for good once anything the node sent after it (a Heartbeat, a Segment Ack) moved the list past
            # that SeqAuth. A message already delivered is recognised by its SeqAuth instead (`_seq_auth_done`).
            if self._is_replay(n.src, n.iv_index, n.seq) or (
                (n.iv_index, seq_auth) <= self._seq_auth_done.get(n.src, (-1, -1))
            ):
                log.debug(
                    "replay from %04X seq %06X ignored (segment of SeqAuth %06X)",
                    n.src,
                    n.seq,
                    seq_auth,
                )
                return
            st = self._segments[key] = {
                "parts": {},
                "n": s.seg_n,
                "t": now,
                "done": False,
                "seq_auth": seq_auth,
            }
        if (
            s.seg_n != st["n"]
        ):  # a different SegN under the same SeqZero: not the message being assembled
            log.debug(
                "RX %04X→%04X segment %d of %d contradicts the %d segments announced, dropped",
                n.src,
                n.dst,
                s.seg_o,
                s.seg_n + 1,
                st["n"] + 1,
            )
            return
        if (
            seq_auth != st["seq_auth"]
        ):  # same SeqZero, another message (8192·k earlier): not a part of this one
            log.debug(
                "RX %04X→%04X segment of SeqAuth %06X does not belong to reassembly %06X, dropped",
                n.src,
                n.dst,
                seq_auth,
                st["seq_auth"],
            )
            return
        st["t"] = (
            now  # §3.5.3.4: the incomplete timer restarts with every segment, and keeps a done entry fresh
        )
        if st["done"]:
            if n.dst == self.state.src:  # sender did not see our ack; repeat it
                self._spawn(self._send_ack(n.src, s.seq_zero, (1 << (s.seg_n + 1)) - 1))
            return
        st["parts"][s.seg_o] = s.data
        block = 0
        for i in st["parts"]:
            block |= 1 << i
        if (
            len(st["parts"]) == s.seg_n + 1
        ):  # every index 0..SegN is present (SegO was range-checked)
            data = b"".join(st["parts"][i] for i in range(s.seg_n + 1))
            st["done"], st["parts"] = True, {}
            self._seq_auth_done[n.src] = max(
                self._seq_auth_done.get(n.src, (-1, -1)),
                (n.iv_index, st["seq_auth"]),
            )
            if n.dst == self.state.src:
                self._spawn(self._send_ack(n.src, s.seq_zero, block))
            self._deliver(
                n, s.akf, s.aid, data, s.szmic, st["seq_auth"], segmented=True
            )
        elif (
            s.seg_o == s.seg_n and n.dst == self.state.src
        ):  # last segment arrived, some missing → partial ack
            self._spawn(self._send_ack(n.src, s.seq_zero, block))

    async def _send_ack(self, dst: int, seq_zero: int, block: int) -> None:
        """Acknowledge a segmented message; its sequence number is reserved and written under `_send_lock`.

        Taken outside the lock, the number could land in the middle of a segmented round, whose numbers are all
        reserved before its first segment is written: the ack reached the air ahead of the round's remaining
        segments with a higher number, and the nodes' replay protection dropped those.
        """
        if not is_unicast(dst):
            return
        try:
            async with self._send_lock:
                self._refuse_unlinked()
                pdu = network_encrypt(
                    self.nk,
                    self.state.tx_iv_index,
                    ctl=True,
                    ttl=self.ttl,
                    seq=self.state.next_seq(),
                    src=self.state.src,
                    dst=dst,
                    transport_pdu=segment_ack(seq_zero, block),
                )
                await self._write(PROXY_NETWORK_PDU, pdu)
        except (
            ConnectionError
        ):  # link gone, or the sequence space exhausted: nothing we can send
            pass

    def _count_undecryptable(self) -> None:
        """One more PDU our keys cannot open: count it and tell the application.

        Every JUNG node relays, so a stale NetKey (a completed key refresh after the export) shows as an unbroken
        stream of these while nothing decodes; a single one is normal (another mesh in range, a node we do not know).
        """
        self.rx_undecryptable += 1
        if self.on_undecryptable:
            self.on_undecryptable()

    def _deliver(
        self,
        n: NetworkPDU,
        akf: bool,
        aid: int,
        upper: bytes,
        szmic: int,
        seq_auth: int,
        segmented: bool = False,
    ) -> None:
        """Decrypt the upper transport PDU, apply replay protection, decode and hand the message out.

        The replay list holds the highest network sequence number accepted per source (§3.8.8, "the last received").
        An unsegmented message is checked here against it; a segmented one was checked on its SeqAuth when its
        first segment was admitted (`_on_segment`) and only advances the list here — by the sequence number of
        the segment that completed it, never backwards. Nothing is advanced before the PDU decoded completely.
        """
        iv = n.iv_index
        access, key = None, ""
        if akf:
            if aid == self.ak.aid:
                access = upper_decrypt(
                    self.ak.key, NONCE_APP, iv, seq_auth, n.src, n.dst, upper, szmic
                )
                key = "app0"
        else:
            for addr in (n.src, n.dst):
                dk = self._dev_key_of_element.get(addr)
                if dk:
                    access = upper_decrypt(
                        dk, NONCE_DEVICE, iv, seq_auth, n.src, n.dst, upper, szmic
                    )
                    if access is not None:
                        key = f"dev:{addr:04X}"
                        break
        if access is None:
            log.debug(
                "RX %04X→%04X undecryptable upper transport (akf=%s aid=%d)",
                n.src,
                n.dst,
                akf,
                aid,
            )
            self._count_undecryptable()
            return
        if not segmented and self._is_replay(n.src, iv, n.seq):
            log.debug("replay from %04X seq %06X ignored", n.src, seq_auth)
            return
        try:
            op, cid, params = decode_opcode(access)
        except ValueError as err:  # authenticated, yet no access message (§3.7.3): dropped before the list moves
            log.debug(
                "RX %04X→%04X malformed access PDU dropped: %s", n.src, n.dst, err
            )
            return
        self.state.note_received(n.src, iv, n.seq)
        msg = AccessMessage(n.src, n.dst, n.ttl, n.seq, op, cid, params, access, key)
        self._follow_key_refresh(msg)
        log.debug(
            "RX %s", msg
        )  # per-message traffic stays out of INFO (a token read would show up there)
        resolved = False
        for match, fut in list(self._waiters):
            if fut.done():
                continue
            # every pending predicate sees it (collect() gathers through its own, never resolving), but one status
            # answers one request: the oldest waiter it fits
            if match(msg) and not resolved:
                fut.set_result(msg)
                resolved = True
        if self.on_message:
            try:
                self.on_message(msg)
            except Exception:
                log.exception("on_message handler failed")
