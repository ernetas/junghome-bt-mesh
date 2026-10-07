"""The sequence-number store: where nonce reuse is decided (`HAState`, its store, the `.backup` copy, the floor).

Every PDU Home Assistant sends is encrypted under the nonce (SRC, SEQ, IV index), so a counter that ever goes back
reuses one. This module holds everything that keeps it from going back across restarts, reloads, a lost or
restored store and a second client on our address: the per-mesh store (`seq_store`), its `.backup` copy and the
repair floor, the skip-ahead the `seq_store_lost` repair and a restored backup continue from, the legacy migration,
`HAState`, the client's `LocalState` backed by them, and where an address's numbers start (`async_load_state`: the
store, the backup, the floor, the repair issue, the restore skip, the skip-ahead of an address used before, one hub
per mesh). Nothing here depends on the hub (`coordinator.py`), which is built on the `HAState` `async_load_state`
returns.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn, TypedDict, cast

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.file import WriteError
from homeassistant.util.hass_dict import HassKey

from .const import (
    DOMAIN,
    ISSUE_DUPLICATE_MESH,
    ISSUE_RESTORE_TOO_OLD,
    ISSUE_SEQ_STORE_LOST,
    ISSUE_SEQ_STORE_UNWRITABLE,
    SEQ_SKIP_AHEAD,
    issue_id,
    learn_more_url,
)
from .jhmesh.client import (
    IV_INDEX_MAX,
    IV_ORIGIN_BEACON,
    IV_ORIGIN_LOCAL,
    IV_UPDATE_MAX_STATE,
    IV_UPDATE_MIN_STATE,
    NET_KEY_INDEX,
    SEQ_GUARD_FIRST_BEACON,
    SEQ_MAX,
    SEQ_TX_LIMIT,
    LocalState,
    SequenceExhausted,
    SequenceStalled,
)
from .jhmesh.crypto import NetKeyMaterial
from .jhmesh.keyrefresh import KeyRefreshRecord
from .jhmesh.state import check_range
from .jhmesh.vault import recognise

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .identity import VaultKeeper
    from .jhmesh.cdb import CDB

_LOGGER = logging.getLogger(__name__)


# how far from 0 the counter jumps when nothing at all is left of an address's numbers (`const.SEQ_SKIP_AHEAD`); also
# how far a restored record no write time is known of (an older version wrote it) continues
SEQ_SKIP_UNKNOWN: Final = 1 << 22
# The send rate (`SendRate`): numbers handed out per day, measured over windows of a day of the hub's running time.
# A window shorter than SEQ_RATE_MIN_SPAN measures nothing yet: until one is longer, or one completed, an address is
# taken to send SEQ_RATE_UNMEASURED a day — about 12 million a year, what some thirty metering sockets polled every
# 300 s send (`const.SEQ_SKIP_AHEAD`).
SEQ_RATE_WINDOW: Final = 86_400.0
SEQ_RATE_MIN_SPAN: Final = 3_600.0
SEQ_RATE_UNMEASURED: Final = 1 << 15
# A restored record continues this many times the numbers its rate sends during the backup's age: the rate may have
# grown since the backup (more devices, more of them unreachable), and the skip is all that covers it
SEQ_RESTORE_RATE_FACTOR: Final = 2
# how far ahead of the clock a restored record's write time may lie before the clock, not the record, is taken
# to be wrong (seconds; a clock slewed by NTP between the backup and the start)
SEQ_CLOCK_TOLERANCE: Final = 300.0


STORAGE_VERSION = 1
# The sequence-number store's minor version: 2 adds an address record's optional `seq_guard` (`LocalState.seq_guard`,
# written by the `seq_store_lost` repair), 3 its optional `in_backup` (the mark of a Home Assistant backup being
# taken, `backup.py`), 4 the mesh-level part next to `addresses`, `{"mesh": {"key_refresh": …}}` (the key refresh
# followed, `HAState`: the address's own record keeps a copy, for an older reader), 5 an address record's optional
# `address_shared` (`[IV index, seq]` another client sent from the address, `HAState.address_shared`), 6 its
# `written_at` (when Home Assistant wrote it), `send_rate` (`SendRate.to_stored`) and, next to the mark, `backup_at`
# (when the backup began). A minor bump: Home Assistant loads a store of a higher minor version with the same major
# one as it is when the reader has no migration for it, so an older integration reads these records (and ignores the
# fields) rather than failing to start.
SEQ_STORAGE_MINOR_VERSION = 6
# Sequence-number persistence (`HAState`): the margin added to a counter found in use, and how many numbers may
# pass between two forced store writes — see the class docstring for the arithmetic.
SEQ_RESTART_MARGIN = 512
SEQ_SAVE_EVERY = 64
# how far the counter may run past the floor entry the `.floor` file durably holds before `HAState` writes a new one:
# a quarter of what a repair with nothing else left continues past it (SEQ_SKIP_UNKNOWN), so a
# floor write that fails has three more chances before sends are held back for it
SEQ_FLOOR_EVERY = 1 << 20
SEQ_STALL_RETRY = 5.0  # seconds between forced-save retries while reserve_seq() is refusing to hand out numbers


class SeqRecord(TypedDict, total=False):
    """One address's record in the sequence-number store: `{"addresses": {"<src>": record}, "mesh": {…}}`.

    What `LocalState.to_stored` writes, without `src` (the record's key), plus Home Assistant's own: `clean` (the
    hub closed it: no restart margin), `address_shared`, when it was written (`written_at`, seconds since the epoch)
    and the address's send rate (`send_rate`), and a backup's mark `in_backup` with the time the backup began
    (`backup_at`) (see `SEQ_STORAGE_MINOR_VERSION`). A floor entry (`seq_floor_store_for_uuid`) holds `iv_index`, `seq` and
    `seq_guard` only. A record as read is whatever the file held: `LocalState.parse_record` checks it.
    """

    seq: int
    iv_index: int
    iv_update_active: bool
    iv_known: bool
    rpl: dict[str, list[int]]
    seq_peak: int
    seq_peak_from: int
    key_refresh: dict[str, Any] | None
    seq_guard: int
    iv_changed_at: float
    iv_recovered_at: float
    iv_update_origin: str
    iv_update_started_at: float
    iv_update_confirmed: bool
    iv_update_confirmed_at: float
    iv_update_abandoned: str
    mesh_iv_changed_at: float
    clean: bool
    address_shared: list[int]
    written_at: float
    send_rate: dict[str, Any]
    in_backup: str
    backup_at: float


SEQ_STORES: HassKey[dict[str, SeqStore]] = HassKey(f"{DOMAIN}_seq_stores")
SEQ_OWNERS: HassKey[dict[str, HAState]] = HassKey(f"{DOMAIN}_seq_owners")
# the mark of the Home Assistant backup being taken right now (`backup.async_pre_backup`): every record written
# while it is set carries it (`HAState._snapshot`), so a start that finds a record with a mark this process did not
# set knows the record came back from a backup, or that Home Assistant stopped during one
SEQ_BACKUP_TOKEN: HassKey[str] = HassKey(f"{DOMAIN}_seq_backup_token")
# ... and when that backup began (seconds since the epoch), written next to the mark (`backup_at`)
SEQ_BACKUP_AT: HassKey[float] = HassKey(f"{DOMAIN}_seq_backup_at")
# the fields of a backup's mark, which a record loses with it (`_without_mark`)
_MARK_FIELDS: Final = ("in_backup", "backup_at")


def wall_now() -> float:
    """Return the wall clock (seconds since the epoch), looked up at call time (the tests set their own).

    The send rate and a restored record's age are measured with it: they have to mean the same across a restart and a
    restore, which the monotonic clock does not.
    """
    return time.time()


def _utc(timestamp: float | None) -> str | None:
    """Return a wall-clock time (seconds since the epoch) as ISO 8601 in UTC; None stays None."""
    return (
        None if timestamp is None else dt_util.utc_from_timestamp(timestamp).isoformat()
    )


def local_time(timestamp: float | None, unknown: str = "") -> str:
    """Return a wall-clock time as a message shows it: local, to the second; `unknown` for None."""
    if timestamp is None:
        return unknown
    return (
        dt_util.as_local(dt_util.utc_from_timestamp(timestamp))
        .replace(microsecond=0)
        .isoformat(sep=" ")
    )


def iv_update_summary(state: LocalState) -> dict[str, Any]:
    """Describe the last IV Update of `state`, as the IV Update actions answer and the diagnostics show it.

    Who started it (`home_assistant`, `beacon`; None when none was seen since this was kept), when, whether and when
    the mesh took one Home Assistant started (`LocalState.iv_update_confirmed`, `iv_update_confirmed_at`), whether it
    is still in progress, and the window Mesh Protocol 1.1 §3.11.5 gives its return to Normal Operation: 96 to 144
    hours after the mesh took it (unknown until then). While one Home Assistant started waits for the
    mesh, `waiting_for_mesh_until` says when it is given up (144 hours after the start);
    `abandoned` says why one was (`not_taken`, `aborted`). `mesh_iv_changed_at`: the last change of the mesh's IV
    state its beacons showed (`LocalState.mesh_iv_changed_at`).
    """
    started, taken = state.iv_update_started_at, state.iv_update_confirmed_at
    waiting = state.mesh_iv_index != state.iv_index  # only while one of ours waits
    return {
        "started_by": {
            IV_ORIGIN_LOCAL: "home_assistant",
            IV_ORIGIN_BEACON: "beacon",
        }.get(state.iv_update_origin or ""),
        "started_at": _utc(started),
        "confirmed": state.iv_update_confirmed,
        "confirmed_at": _utc(taken),
        "in_progress": state.iv_update_active,
        "normal_operation_from": _utc(
            None if taken is None else taken + IV_UPDATE_MIN_STATE
        ),
        "normal_operation_by": _utc(
            None if taken is None else taken + IV_UPDATE_MAX_STATE
        ),
        "waiting_for_mesh_until": _utc(
            started + IV_UPDATE_MAX_STATE if waiting and started is not None else None
        ),
        "abandoned": state.iv_update_abandoned,
        "mesh_iv_changed_at": _utc(state.mesh_iv_changed_at),
    }


class SeqStore(Store[dict[str, Any]]):
    """A `Store` that remembers the last payload it actually wrote to disk (`written`).

    `Store._async_handle_write_data` catches a `WriteError` (a full disk, a filesystem remounted read-only —
    the usual way an SD card dies) with only a log line, and by then it has already cleared its own pending
    data, so nothing else notices the write never landed. `HAState.reserve_seq` reads `written` to tell what a
    restart would actually load, so it can hold back sends the moment that stops matching what has been sent.
    `write_error` keeps the text of the last write's `WriteError` (None once a write lands) for the repair that
    names it (`seq_store_unwritable`) and the diagnostics.
    """

    written: dict[str, Any] | None = None
    write_error: str | None = None

    def __init__(
        self, hass: HomeAssistant, version: int, key: str, **kwargs: Any
    ) -> None:
        """Set up a `Store` at the sequence store's minor version (`SEQ_STORAGE_MINOR_VERSION`) unless told otherwise."""
        kwargs.setdefault("minor_version", SEQ_STORAGE_MINOR_VERSION)
        super().__init__(hass, version, key, **kwargs)

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: Any
    ) -> Any:
        """1.1 … 1.5 → 1.6: the records stay as they are (no `seq_guard`: no guard pending; no `in_backup`: no mark).

        No `mesh` part: the key refresh is read from the address's own record (`_stored_key_refresh`); no
        `address_shared`: no other client seen; no `written_at` / `send_rate`: a restore of the record continues
        SEQ_SKIP_UNKNOWN (`_restore_skip`), and its rate is measured from now on.
        """
        if old_major_version != STORAGE_VERSION:
            raise NotImplementedError
        return old_data

    async def _async_write_data(self, data: dict[str, Any]) -> None:
        try:
            await super()._async_write_data(data)
        except WriteError as err:
            self.write_error = str(err)
            raise  # `Store` logs it; `written` still lags on a failure
        self.written = data["data"]
        self.write_error = None


def seq_store_for_uuid(hass: HomeAssistant, mesh_uuid: str) -> SeqStore:
    """Return the sequence-number store of a mesh, keyed by its (lower-cased) UUID, for callers without a `CDB`.

    One `Store` object per mesh UUID for the life of `hass` (`SEQ_STORES`): `Store` keeps an older scheduled write
    from clobbering a newer one only within one object, and two live hubs of one mesh (an installation from before
    `_mesh_uuid_taken`) would otherwise race their writes. `atomic_writes=True` fsyncs before the rename;
    `private=True` keeps it owner-only, as a record holds the new NetKey while a key refresh is followed. The
    `.backup` and `.floor` stores are created the same way.
    """
    stores = hass.data.setdefault(SEQ_STORES, {})
    key = mesh_uuid.lower()
    if key not in stores:
        stores[key] = SeqStore(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.seq.{key}",
            private=True,
            atomic_writes=True,
        )
    return stores[key]


def seq_store(hass: HomeAssistant, cdb: CDB) -> SeqStore:
    """Return the sequence-number store of a mesh: keyed by the mesh UUID, shared by its entries and addresses."""
    return seq_store_for_uuid(hass, cdb.mesh_uuid)


SEQ_BACKUP_STORES: HassKey[dict[str, SeqStore]] = HassKey(f"{DOMAIN}_seq_backup_stores")


def seq_backup_store(hass: HomeAssistant, cdb: CDB) -> SeqStore:
    """Return the mesh's `.backup` copy of its sequence-number store, one object per mesh UUID like `seq_store`.

    Mirrors `LocalState`'s `.bak`: `HAState.persist` refreshes it every time it forces an immediate write of the
    primary, and `async_load_state` falls back to it when the primary, or our address's record in it, is
    unusable (HA already renamed a corrupt primary aside and returned `None`; see `Store._async_load_data`).
    """
    return seq_backup_store_for_uuid(hass, cdb.mesh_uuid)


def seq_backup_store_for_uuid(hass: HomeAssistant, mesh_uuid: str) -> SeqStore:
    """`seq_backup_store` by the mesh UUID itself (the `seq_store_lost` repair has no `CDB`)."""
    stores = hass.data.setdefault(SEQ_BACKUP_STORES, {})
    key = mesh_uuid.lower()
    if key not in stores:
        stores[key] = SeqStore(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.seq.{key}.backup",
            private=True,
            atomic_writes=True,
        )
    return stores[key]


SEQ_FLOOR_STORES: HassKey[dict[str, SeqStore]] = HassKey(f"{DOMAIN}_seq_floor_stores")


def seq_floor_store_for_uuid(hass: HomeAssistant, mesh_uuid: str) -> SeqStore:
    """Return the mesh's repair floor, `.storage/junghome_ble.seq.<mesh uuid>.floor`, one object per mesh UUID.

    Per address, an (IV index, sequence number) the address is known to have reached: where the `seq_store_lost`
    repair continued from (`async_skip_seq_store_ahead`), where a restored record or an `iv_index_mismatch` rewind
    continued from, and — so it keeps up with the counter — where `HAState` was whenever its
    transmit index changed and every SEQ_FLOOR_EVERY numbers. The two copies of the store are what the repair
    replaces when they are lost; this file is not, so a second loss of both still knows where the address got to —
    without it, "nothing readable" meant SEQ_SKIP_UNKNOWN from 0 every time, the very numbers the address had sent
    since the first repair, and a floor written by the repair alone was outrun once the address had sent about
    SEQ_SKIP_UNKNOWN more. A record here also counts as history (`async_load_state`).
    """
    stores = hass.data.setdefault(SEQ_FLOOR_STORES, {})
    key = mesh_uuid.lower()
    if key not in stores:
        stores[key] = SeqStore(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.seq.{key}.floor",
            private=True,
            atomic_writes=True,
        )
    return stores[key]


def _newest_corrupt_seq_store(path: str) -> str | None:
    """Name of the newest `<path>.corrupt.<timestamp>` HA's storage layer renamed a JSON-decode failure to.

    Run in the executor: this is a directory listing, and `async_load_state` runs on the event loop.
    """
    matches = sorted(Path(path).parent.glob(f"{Path(path).name}.corrupt.*"))
    return matches[-1].name if matches else None


def _usable_record(data: Any, key: str) -> SeqRecord | None:
    """Address `key`'s record in a loaded store when `LocalState` can resume from it, else None (absent or unusable)."""
    try:
        record = data["addresses"][key]
        LocalState.parse_record({**record, "src": key})
    except (KeyError, IndexError, ValueError, TypeError, AttributeError):
        return None
    return cast("SeqRecord", dict(record))


def _has_history(data: Any, key: str) -> bool:
    """Whether a loaded store says address `key` sent before: it holds a record for it, or is not a store at all."""
    if data is None:
        return False
    try:
        return key in data["addresses"]
    except (KeyError, IndexError, TypeError):
        return True  # something is there, but nothing says what: assume the worst


def _without_mark(record: Any, token: str | None) -> Any:
    """`record` without the mark of the backup `token` this process is taking (`HAState._snapshot` adds it back).

    A mark of any other backup stays: it says the record came back from that one, or that Home Assistant stopped
    during it, and the address skips ahead when it is next used (`_async_skip_restored_record`).
    """
    if token is None or not isinstance(record, dict):
        return record
    if record.get("in_backup") != token:
        return record
    return {name: value for name, value in record.items() if name not in _MARK_FIELDS}


def _addresses_of(data: Any) -> dict[str, Any] | None:
    """Return the `addresses` map of a loaded store when it is one."""
    addresses = data.get("addresses") if isinstance(data, dict) else None
    return addresses if isinstance(addresses, dict) else None


def _mesh_of(*datas: Any) -> dict[str, Any]:
    """Return the mesh-level part (`{"mesh": …}`, minor version 4) of the first loaded store that has one, else {}."""
    for data in datas:
        mesh = data.get("mesh") if isinstance(data, dict) else None
        if isinstance(mesh, dict):
            return mesh
    return {}


def _store_with(data: Any, addresses: dict[str, Any]) -> dict[str, Any]:
    """Return a store holding `addresses`, with `data`'s mesh-level part (a rewrite of the records must not drop it)."""
    mesh = _mesh_of(data)
    return {"addresses": addresses, **({"mesh": mesh} if mesh else {})}


def _stored_key_refresh(data: Any, key: str) -> Any:
    """Return the key refresh a loaded store records for a hub at address `key` (stored form; None: there is none).

    The mesh-level one (a key refresh belongs to the mesh, not to an address, so a new address after
    a followed refresh keeps its key), whatever it says, None included; a store an older version wrote has none and
    the address's own record holds it. A mesh-level one that does not parse falls back to the record's too.
    """
    mesh = _mesh_of(data)
    if "key_refresh" in mesh:
        stored = mesh["key_refresh"]
        try:
            if stored is not None:
                KeyRefreshRecord.from_stored(stored)
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.warning(
                "The mesh's key refresh in the sequence-number store is unusable (%s): reading the address's own",
                err,
            )
        else:
            return stored
    record = _usable_record(data, key)
    return None if record is None else record.get("key_refresh")


def _landed(store: SeqStore, loaded: Any) -> Any:
    """Return what `store` durably holds: its last write that landed (`written`), else what was `loaded` from disk.

    A write still on its way, which `Store.async_load` hands back, is not on disk yet and may never be.
    """
    return loaded if store.written is None else store.written


def _furthest(records: Sequence[Any]) -> tuple[int, int] | None:
    """Return the furthest (transmit IV index, seq) of the `records` that still read as numbers in range, if any.

    Out of range is not a number a record can hold: an IV index past 2^32 - 1 made the repair write
    a record no start could use, and a floor no later repair got past; a negative counter put the target at 0 under
    its index, over the numbers sent there.
    """
    best: tuple[int, int] | None = None
    for record in records:
        try:
            check_range("IV index", int(record.get("iv_index", 0)), 0, IV_INDEX_MAX)
            check_range("sequence number", int(record.get("seq", 0)), 0, SEQ_MAX)
            rank = _tx_rank(record)
        except (AttributeError, ValueError, TypeError):
            continue
        if best is None or rank > best:
            best = rank
    return best


def _seq_skip_target(records: Sequence[Any], floor: Any = None) -> tuple[int, int]:
    """(IV index, sequence number) to continue from when no record is usable: past the furthest one left.

    Whatever still reads as numbers in range counts, however broken the rest of its record; SEQ_SKIP_AHEAD beyond
    it. When nothing does, SEQ_SKIP_UNKNOWN from the last point known for sure: the floor entry (`floor`, as the
    `.floor` file durably holds it: `_landed`), else 0 — the floor also wins over records that are behind it. Never
    past `SEQ_TX_LIMIT`.
    """
    best = _furthest(records)
    known = _furthest([floor])
    if known is not None and (best is None or known > best):
        iv_index, seq = known[0], known[1] + SEQ_SKIP_UNKNOWN
    elif best is None:
        iv_index, seq = 0, SEQ_SKIP_UNKNOWN
    else:
        iv_index, seq = best[0], best[1] + SEQ_SKIP_AHEAD
    return max(iv_index, 0), min(max(seq, 0), SEQ_TX_LIMIT)


def _carried_seq_guard(records: Sequence[Any]) -> int:
    """Return the highest index a `seq_guard` of the `records` (a floor entry) names, else SEQ_GUARD_FIRST_BEACON."""
    guard = SEQ_GUARD_FIRST_BEACON
    for record in records:
        try:
            value = LocalState.parse_seq_guard(record)
        except (AttributeError, ValueError, TypeError):
            continue
        if value is not None:
            guard = max(guard, value)
    return guard


def _non_negative(what: str, value: Any) -> float:
    """Return a stored number as a float; TypeError / ValueError unless it is a finite, non-negative number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{what} {value!r} is not a number")
    if not 0 <= value < math.inf:
        raise ValueError(f"{what} {value!r} is out of range")
    return float(value)


def _stored_time(record: Mapping[str, Any], name: str) -> float | None:
    """Return a record's wall-clock time `name` (`written_at`, `backup_at`); None when it has none, or none usable."""
    try:
        return _non_negative(name, record[name])
    except (KeyError, TypeError, ValueError):
        return None


class SendRate:
    """How many sequence numbers an address hands out per day: what a restored record of it skips (`_restore_skip`).

    Measured over windows of SEQ_RATE_WINDOW of the hub's running time — the time between two writes of the
    record (`tick`) while an `HAState` of the address runs; a stopped Home Assistant adds none, so its downtime does
    not dilute the rate — and the numbers handed out in that time (`note_sent`; a jump of the counter, by a skip or
    a repair, is no traffic and is not counted). A completed window's rate replaces `measured` when it is higher,
    and otherwise takes it half-way down: a busy day counts at once, a quiet one by halves, so a window diluted by a
    clock that jumped forward within one tick still leaves half the rate, which SEQ_RESTORE_RATE_FACTOR doubles.
    The window in progress counts too once it is SEQ_RATE_MIN_SPAN long; before any window measured that much,
    SEQ_RATE_UNMEASURED is assumed (`per_day`). Unverified on air: the rates are measured on the fake proxy only.
    """

    def __init__(
        self, measured: float | None = None, sent: int = 0, seconds: float = 0.0
    ) -> None:
        """Start from what a record kept: the last windows' rate (`measured`, per day), the open window's numbers and time."""
        self.measured = measured
        self.sent = sent
        self.seconds = seconds
        self._ticked: float | None = (
            None  # the wall clock of the last tick; None: this run has not ticked yet
        )

    @classmethod
    def from_stored(cls, stored: Any) -> SendRate:
        """Read a record's `send_rate`; nothing measured when it has none, or one that is not one (logged)."""
        if stored is None:
            return cls()
        try:
            measured = stored.get("per_day")
            sent = stored["sent"]
            if isinstance(sent, bool) or not isinstance(sent, int) or sent < 0:
                raise ValueError(f"numbers sent {sent!r} is not a count")
            return cls(
                None if measured is None else _non_negative("send rate", measured),
                sent,
                _non_negative("send-rate window", stored["seconds"]),
            )
        except (AttributeError, KeyError, TypeError, ValueError) as err:
            _LOGGER.warning(
                "Ignoring an unusable send rate in the sequence-number store (%s)", err
            )
            return cls()

    def to_stored(self) -> dict[str, Any]:
        """Return the record's `send_rate`: `{"per_day": measured, "sent": …, "seconds": …}`."""
        return {"per_day": self.measured, "sent": self.sent, "seconds": self.seconds}

    def note_sent(self, count: int) -> None:
        """Count `count` numbers handed out into the open window."""
        self.sent += count

    def tick(self, now: float) -> None:
        """Add the running time since the last tick to the open window; close the window once it is a day long.

        The first tick of a run only starts the clock. A clock set back adds nothing.
        """
        if self._ticked is not None:
            self.seconds += max(now - self._ticked, 0.0)
        self._ticked = now
        if self.seconds >= SEQ_RATE_WINDOW:
            rate = self.sent * SEQ_RATE_WINDOW / self.seconds
            self.measured = (
                rate
                if self.measured is None or rate >= self.measured
                else (self.measured + rate) / 2
            )
            self.sent, self.seconds = 0, 0.0

    def per_day(self) -> float:
        """Numbers per day a restore takes the address to have sent since its record was written (the class docstring)."""
        current = (
            self.sent * SEQ_RATE_WINDOW / self.seconds
            if self.seconds >= SEQ_RATE_MIN_SPAN
            else None
        )
        if self.measured is None:
            if current is not None:
                return current
            return max(
                float(SEQ_RATE_UNMEASURED),
                self.sent * SEQ_RATE_WINDOW / SEQ_RATE_MIN_SPAN,
            )
        return self.measured if current is None else max(self.measured, current)


def restore_coverage(per_day: float, seq: int) -> dict[str, Any]:
    """Describe what a restore of an address sending `per_day` numbers a day, at `seq` now, would skip (diagnostics).

    For a backup taken now: `numbers_per_day`; `minimum_covers_days`, the age up to which its restore skips only
    the minimum, SEQ_SKIP_AHEAD; `restore_covers_days`, the age past which its restore would skip beyond the end of
    the sequence space and send nothing (`restore_too_old`). None: any age (nothing is sent).
    """
    per_skip = SEQ_RESTORE_RATE_FACTOR * per_day
    left = SEQ_TX_LIMIT - seq
    return {
        "numbers_per_day": round(per_day),
        "minimum_covers_days": None
        if per_skip <= 0
        else round(SEQ_SKIP_AHEAD / per_skip, 1),
        "restore_covers_days": 0.0
        if left < SEQ_SKIP_AHEAD
        else None
        if per_skip <= 0
        else round(left / per_skip, 1),
    }


class ClockBehind(ValueError):
    """A restored record says it was written later than the clock says it is now: the clock is wrong (not set yet)."""


def _restore_skip(
    records: Sequence[Any], now: float
) -> tuple[int, float | None, float]:
    """(numbers to skip, the backup's age in seconds, numbers per day) for a restored address with `records` (both copies).

    The age runs from the earliest time the records give (their write, the backup's start): every number sent
    since then may be in use. The skip covers SEQ_RESTORE_RATE_FACTOR times what the highest rate of the copies
    sends in that time, at least SEQ_SKIP_AHEAD. Records no write time is known of (an older version wrote them)
    continue SEQ_SKIP_UNKNOWN, age None. `ClockBehind` when the earliest time lies more than SEQ_CLOCK_TOLERANCE
    ahead of `now`: no age can be measured with that clock.
    """
    usable = [record for record in records if isinstance(record, Mapping)]
    times = [
        time_
        for record in usable
        for name in ("written_at", "backup_at")
        if (time_ := _stored_time(record, name)) is not None
    ]
    rate = max(
        (SendRate.from_stored(record.get("send_rate")).per_day() for record in usable),
        default=float(SEQ_RATE_UNMEASURED),
    )
    if not times:
        return SEQ_SKIP_UNKNOWN, None, rate
    since = min(times)
    if since - now > SEQ_CLOCK_TOLERANCE:
        raise ClockBehind(f"written at {since:.0f}, the clock says {now:.0f}")
    age = max(now - since, 0.0)
    skip = math.ceil(SEQ_RESTORE_RATE_FACTOR * rate * age / SEQ_RATE_WINDOW)
    return max(SEQ_SKIP_AHEAD, skip), age, rate


async def async_rewind_seq_floor(
    hass: HomeAssistant, mesh_uuid: str, key: str, iv_index: int, seq: int, guard: int
) -> bool:
    """Record in the repair floor where the `iv_index_mismatch` repair continues address `key` from.

    Written before the address goes back to the mesh's `iv_index` (`JungHomeHub.async_rewind_iv_index`), like the
    `seq_store_lost` repair's own floor: should both copies of the store be lost later, that repair continues from
    here, under the mesh's index and with the guard over every index the address used while ahead (an entry at
    the old index would put it back ahead of the mesh). An earlier entry is merged in: the higher counter, and a
    guard over its index too. False when the write did not land.
    """
    floor_store = seq_floor_store_for_uuid(hass, mesh_uuid)
    floors = _addresses_of(await floor_store.async_load()) or {}
    known = _furthest([floors.get(key)])
    if known is not None:
        seq = max(seq, known[1])
        guard = max(guard, known[0], _carried_seq_guard([floors.get(key)]))
    data = {
        "addresses": {
            **floors,
            key: SeqRecord(iv_index=iv_index, seq=seq, seq_guard=guard),
        }
    }
    await floor_store.async_save(data)
    return floor_store.written is data


async def async_skip_seq_store_ahead(
    hass: HomeAssistant, mesh_uuid: str, key: str
) -> int | None:
    """Write a record for address `key` past every number it may have sent (the `seq_store_lost` repair).

    The repair floor (`seq_floor_store_for_uuid`) is written first, then both copies of the mesh's store; the
    other addresses' records stay as they are. Returns the new counter; `HAState` adds the restart margin on top
    (the record is not marked clean). None, with nothing else written, when the floor's write did not land: a
    later repair would not know this one's numbers, so the setup stays refused rather than continue without it.
    """
    store = seq_store_for_uuid(hass, mesh_uuid)
    backup = seq_backup_store_for_uuid(hass, mesh_uuid)
    floor_store = seq_floor_store_for_uuid(hass, mesh_uuid)
    data = await store.async_load()
    backup_data = await backup.async_load()
    floors = _addresses_of(_landed(floor_store, await floor_store.async_load())) or {}
    records = [
        addresses.get(key)
        for addresses in (_addresses_of(data), _addresses_of(backup_data))
        if addresses is not None
    ]
    iv_index, seq = _seq_skip_target(records, floors.get(key))
    # a guard an earlier `iv_index_mismatch` repair left (`async_rewind_seq_floor`): numbers were sent under every
    # index up to it, so it stays — the first beacon raises it to its own index + 1 if that is higher
    guard = _carried_seq_guard([floors.get(key)])
    floor_entry: SeqRecord = {"iv_index": iv_index, "seq": seq}
    if guard != SEQ_GUARD_FIRST_BEACON:
        floor_entry["seq_guard"] = guard
    floor = {"addresses": {**floors, key: floor_entry}}
    await floor_store.async_save(floor)
    if floor_store.written is not floor:
        _LOGGER.error(
            "Could not write the sequence-number floor of address %s: not skipping ahead without it",
            key,
        )
        return None
    base = _addresses_of(data) or _addresses_of(backup_data) or {}
    new = _store_with(
        data if _mesh_of(data) else backup_data,
        {
            **base,
            key: {
                "seq": seq,
                "iv_index": iv_index,
                "iv_update_active": False,
                "iv_known": False,  # the first authenticated beacon decides
                "rpl": {},
                "clean": False,
                # whatever was lost was sent under an index nobody knows: no restart at 0 under any index up to
                # the first beacon's (`LocalState.apply_beacon`)
                "seq_guard": guard,
            },
        },
    )
    await store.async_save(new)
    await backup.async_save(new)
    _LOGGER.warning(
        "Sequence numbers of address %s continue from %06X (IV index %d), past any it may have sent",
        key,
        seq,
        iv_index,
    )
    return seq


async def _async_skip_restored_record(
    hass: HomeAssistant,
    entry: ConfigEntry,
    mesh_uuid: str,
    key: str,
    data: dict[str, Any] | None,
    backup_data: dict[str, Any] | None,
    floor_data: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Continue past a record restored from a Home Assistant backup; return the store to start from.

    A backup restores the whole configuration directory, so the store, its `.backup` copy and the floor come back
    together, readable, `clean` or not, and nothing else could tell that every number sent since the backup was
    taken is in use: the start resumed below them, and an IV Update since then restarted the counter at 0 under
    the index they went out with. Every record written while a backup is being taken carries its mark (`in_backup`,
    `backup.async_pre_backup`); one that carries a mark this process did not set (a reload during the backup is no
    restore) is rewritten as the `seq_store_lost` repair does: past the further copy by what the address may have
    sent since the record was written (`_restore_skip`: twice its send rate over the backup's age, at least
    SEQ_SKIP_AHEAD: a fixed 2^20 was outrun within months by the hub's own polls), not `clean`,
    `seq_guard` pending the first beacon, the rest kept. A concrete guard the record,
    its copy or the floor already holds (an `iv_index_mismatch` rewind: numbers went out under every index up to
    it) is kept instead, with the index stored as not known, so the first beacon still raises it to one past its
    own index (`LocalState.apply_beacon`) rather than leaving it below the indexes used since. The floor first, then both
    copies, all before the `HAState` is built. A Home Assistant that stopped during a backup leaves the mark too:
    its next start skips ahead once, for nothing.

    A skip that would pass `SEQ_TX_LIMIT` is not capped quietly: the counter goes to the limit, so nothing is sent
    under this IV index any more, and the `restore_too_old` repair says so — an IV Update, or an address never
    used, is the way on (unverified on air). Not ready, with nothing written, while the clock is behind the record
    (`ClockBehind`: a host without a clock of its own, before its time is set), and with nothing else written when
    the floor's write does not land: a later loss of both copies would not know these numbers.
    """
    record = _usable_record(data, key)
    if record is None:
        return data
    mark = record.get("in_backup")
    if mark is None or mark == hass.data.get(SEQ_BACKUP_TOKEN):
        return data
    other = _usable_record(backup_data, key)
    try:
        skip, age, per_day = _restore_skip([record, other], wall_now())
    except ClockBehind as err:
        _LOGGER.error(
            "The sequence-number record of address %s was restored from a backup, but the clock is behind the "
            "time it was written (%s): waiting for the clock to be set",
            key,
            err,
        )
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="seq_clock_behind"
        ) from err
    if other is not None and _tx_rank(other) > _tx_rank(record):
        record = other
    tx, seq = _tx_rank(record)
    refused = seq + skip > SEQ_TX_LIMIT
    seq = min(seq + skip, SEQ_TX_LIMIT)
    floors = _addresses_of(floor_data) or {}
    guard = _carried_seq_guard([record, other, floors.get(key)])
    _LOGGER.warning(
        "The sequence-number record of address %s was restored from a backup (%s), or Home Assistant stopped during "
        "one: its numbers continue from %06X, past any sent since",
        key,
        "written at an unknown time"
        if age is None
        else f"{age / SEQ_RATE_WINDOW:.1f} days old",
        seq,
    )
    floor_store = seq_floor_store_for_uuid(hass, mesh_uuid)
    known = _furthest([floors.get(key)])
    entry_: dict[str, Any] = (
        {"iv_index": tx, "seq": seq}
        if known is None or (tx, seq) > known
        else dict(floors[key])
    )
    if guard != SEQ_GUARD_FIRST_BEACON:
        entry_["seq_guard"] = guard
    floor = {"addresses": {**floors, key: entry_}}
    await floor_store.async_save(floor)
    if floor_store.written is not floor:
        _LOGGER.error(
            "Could not write the sequence-number floor of address %s: not starting from a restored record "
            "without it",
            key,
        )
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="seq_floor_not_written"
        )
    restored = {
        name: value for name, value in record.items() if name not in _MARK_FIELDS
    }
    data = _store_with(
        data,
        {
            **(_addresses_of(data) or {}),
            key: {
                **restored,
                "seq": seq,
                "clean": False,
                "seq_guard": guard,
                **({} if guard == SEQ_GUARD_FIRST_BEACON else {"iv_known": False}),
            },
        },
    )
    await seq_store_for_uuid(hass, mesh_uuid).async_save(data)
    await seq_backup_store_for_uuid(hass, mesh_uuid).async_save(data)
    if refused:
        _report_restore_too_old(hass, entry, key, age, per_day)
    return data


def _report_restore_too_old(
    hass: HomeAssistant, entry: ConfigEntry, key: str, age: float | None, per_day: float
) -> None:
    """Raise `restore_too_old`: the restored address may have sent up to the end of its sequence space; it sends nothing.

    Persistent (it says why Home Assistant is mute after the restart that skipped); `Issues.check_sequence_space`
    deletes it once the counter is below `SEQ_TX_LIMIT` again (an IV Update moved it on, or another address).
    """
    _LOGGER.error(
        "Address %s may have sent to the end of its sequence numbers since the restored backup was taken (%s, about "
        "%.0f numbers a day): it sends nothing under this IV index — start an IV Update, or give Home Assistant an "
        "address it never used",
        key,
        "of unknown age" if age is None else f"{age / SEQ_RATE_WINDOW:.1f} days old",
        per_day,
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry, ISSUE_RESTORE_TOO_OLD),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_RESTORE_TOO_OLD,
        learn_more_url=learn_more_url(ISSUE_RESTORE_TOO_OLD),
        translation_placeholders={"title": entry.title, "unicast": key},
    )


async def async_apply_followed_key_refresh(
    hass: HomeAssistant, cdb: CDB, unicast: int
) -> None:
    """Put the NetKey of a key refresh the hub followed to its end in place of the export's stale one.

    The client stores the new key with the sequence numbers (`LocalState.key_refresh`, phase 3; at mesh level, so
    whichever address the hub uses: `_stored_key_refresh`) until the export holds it. Without this a setup would
    look for proxies of the old Network ID (none left) and talk with the revoked key. Only the in-memory `cdb` changes; the file is the gateway's or the user's. An export
    written mid key refresh (`CDB.net_key_refresh`) whose old key is the followed one is newer: it is left alone.

    Only a completion the client proved (the proxy's beacon under the new key, or the nodes' own
    statuses) is put in: one without proof was recorded before proofs were kept or forged by a node, and the client
    takes its key up as a candidate only (`KeyRefreshFollower.resume`).
    """
    data = await seq_store(hass, cdb).async_load()
    stored = _stored_key_refresh(data, f"{unicast:04X}")
    if stored is None:
        return
    refresh = KeyRefreshRecord.from_stored(stored)
    if refresh.phase != 3:
        return
    if not refresh.proven:
        _LOGGER.warning(
            "The stored key refresh is complete but unproven (recorded before proofs were kept, or forged by a "
            "node): keeping the export's network key"
        )
        return
    key = refresh.key
    exported = cdb.net_key_refresh.get(NET_KEY_INDEX)
    if exported is not None and exported[0].key == key:
        return
    if key != cdb.net_keys[NET_KEY_INDEX].key:
        _LOGGER.info(
            "Using the network key of the completed key refresh; the export still has the old one"
        )
        cdb.net_keys[NET_KEY_INDEX] = NetKeyMaterial.derive(key)
    # the export's own refresh, if it caught one, is over too: the old key is revoked
    cdb.net_key_refresh.pop(NET_KEY_INDEX, None)


def _report_seq_store_lost(
    hass: HomeAssistant,
    entry: ConfigEntry,
    mesh_uuid: str,
    key: str,
    corrupt: str | None,
) -> NoReturn:
    """Refuse to start an address with history from 0 (nonce reuse); offer the skip-ahead as a repair."""
    _LOGGER.error(
        "Neither copy of the sequence-number record of address %s is usable (%s): starting it at 0 would reuse "
        "nonces already sent — repair the issue to continue past them, or restore the store",
        key,
        f"the corrupt store was saved as {corrupt}" if corrupt else "both unreadable",
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry, ISSUE_SEQ_STORE_LOST),
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_SEQ_STORE_LOST,
        learn_more_url=learn_more_url(ISSUE_SEQ_STORE_LOST),
        translation_placeholders={"title": entry.title, "unicast": key},
        data={"entry_id": entry.entry_id, "mesh_uuid": mesh_uuid, "unicast": key},
    )
    raise ConfigEntryError(
        translation_domain=DOMAIN,
        translation_key="seq_store_lost",
        translation_placeholders={"unicast": key},
    )


def _tx_rank(record: Mapping[str, Any]) -> tuple[int, int]:
    """(transmit IV index, seq) of a stored record, for ranking which of two is further along.

    The transmit index is `iv_index - 1` while `iv_update_active`: ranked by the stored `iv_index` alone, a record
    still sending under the old index would outrank one that is further along.
    """
    iv = int(record.get("iv_index", 0))
    return (iv - 1 if record.get("iv_update_active") else iv, int(record.get("seq", 0)))


def merge_legacy_seq_store(
    data: dict[str, Any] | None, old: dict[str, Any]
) -> dict[str, Any]:
    """Fold a pre-0.3 per-entry store (one address: `{src, seq, iv_index, iv_update_active}`) into the per-mesh map.

    Two records for the same address (another entry of the same mesh already migrated, or a reconfigured address
    coming back): the one further along by transmit IV index, then sequence number, is the one to continue.
    """
    addresses = dict((data or {}).get("addresses") or {})
    src = str(old["src"]).upper()
    record = {key: value for key, value in old.items() if key != "src"}
    have = addresses.get(src)
    if have is None or _tx_rank(record) >= _tx_rank(have):
        addresses[src] = record
    return _store_with(data, addresses)


async def async_migrate_legacy_seq_store(
    hass: HomeAssistant, entry: ConfigEntry, mesh_uuid: str
) -> bool:
    """Fold a 0.2 per-entry sequence-number store (`junghome_ble.<entry_id>`) into its mesh's store, then remove it.

    Called from `async_setup_entry` right after `load_network` succeeds, before the not-ready check that can
    retry indefinitely without ever reaching `JungHomeHub.async_create` (an entry stuck in that retry
    never migrated, and removing it then stranded the legacy record — the very record a remove-and-re-add is
    supposed to preserve through `seq_store`); `async_create` also calls it, a no-op by the time it does; and
    `async_remove_entry` calls it too, so an entry that never got this far still migrates before its data is
    gone. A legacy record is deleted only after it has been saved into its mesh's store — never the reverse.

    "Saved" means the write landed (`SeqStore.written`): `Store.async_save` returns normally when the write
    failed (it only logs the `WriteError`) and writes nothing at all while Home Assistant is stopping. False
    when the legacy record is still there for that reason — the mesh's store does not hold its numbers yet.
    """
    legacy: Store[dict[str, Any]] = Store(
        hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
    )
    old = await legacy.async_load()
    if old is None:
        return True
    store = seq_store_for_uuid(hass, mesh_uuid)
    data = merge_legacy_seq_store(await store.async_load(), old)
    await store.async_save(data)
    if store.written is not data:
        _LOGGER.warning(
            "Could not save the sequence-number record of address %s into the mesh's store; keeping %s",
            old.get("src"),
            legacy.path,
        )
        return False
    await legacy.async_remove()
    _LOGGER.info(
        "Moved the sequence-number record of address %s into the mesh's store",
        old.get("src"),
    )
    return True


class AddressShared(SequenceStalled):
    """Another client sends from our address: no number is handed out until the `address_shared` repair skips past it.

    A `SequenceStalled` so whatever treats a refusal as "cannot send now" rather than as a lost link or real
    exhaustion does here too (the link stays, receive-only; a keep-alive refused is no verdict); not retried by
    `JungHomeHub.while_seq_stalls`, which only waits for a store to catch up — this one waits for the user.
    """


def _stored_address_shared(record: Any) -> tuple[int, int] | None:
    """Return the (IV index, seq) another client sent from the address, as its record holds it; None without one.

    A value that is not one (a store edited by hand) is dropped with a WARNING rather than failing the start: the
    next PDU of the other client raises the issue again.
    """
    raw = record.get("address_shared") if isinstance(record, dict) else None
    if raw is None:
        return None
    try:
        iv_index, seq = (int(value) for value in raw)
        check_range("IV index", iv_index, 0, IV_INDEX_MAX)
        check_range("sequence number", seq, 0, SEQ_MAX)
    except (TypeError, ValueError):
        _LOGGER.warning(
            "Ignoring an unusable record of another client on our address: %r", raw
        )
        return None
    return iv_index, seq


class HAState(LocalState):
    """Sequence-number store backed by HA's storage helper: one store per mesh, one counter per address ever used.

    The nonce of every PDU we send is (SRC, SEQ, IV index), so the counter has to outlive everything that can be
    re-created for the same mesh and address: the config entry (remove + add is the standard troubleshooting step)
    and the address itself (reconfigured away and back). The store is keyed by the mesh UUID (`seq_store`; it
    survives a key refresh, the Network ID does not) and maps every address this Home Assistant ever sent from in
    that mesh to what `LocalState.to_stored` holds for it (without the address: the counter, the IV state and the
    replay list): `{"addresses": {"0D00": {"seq": …, "iv_index": …, "iv_update_active": …, "rpl": …, "clean": …}}}`.
    What belongs to the mesh rather than to an address sits next to it, `{"mesh": {"key_refresh": …}}`: the key
    refresh followed — a new address after one keeps its key, and a copy in our own record keeps
    it for an older reader. An address the store does not know starts where `async_load_state` decides
    (`_evidence_of_use`): 0, or SEQ_SKIP_AHEAD when it may have sent before.

    Another client seen sending from the address (`address_shared`) is stored with its record, so a
    restart does not resume sending into that client's numbers: every send is refused (`AddressShared`) until the
    `address_shared` repair skips past them (`skip_past_shared`).

    With a `floor` (the mesh's `.floor` file) the floor entry of our address keeps up with the counter:
    a new one whenever the transmit index moved past the entry's and every SEQ_FLOOR_EVERY numbers, and
    sends wait for the file to durably hold our transmit index and never run SEQ_SKIP_UNKNOWN past its number
    (`_limit`) — the index and the distance the `seq_store_lost` repair continues from when both copies of the store
    are lost. Without that, the floor was only the last repair's, and the repair after a second loss reused every
    number sent beyond that distance, or under an index above the floor's when the first beacon after it was lower.

    Margin arithmetic: a change schedules a save 2 s after the *last* change (`Store.async_delay_save` moves a
    pending write to the latest time asked for, so a connect-time burst on a large installation defers it for as
    long as the burst lasts), and every SEQ_SAVE_EVERY numbers a write is started at once, outside that debounce.
    A crash therefore loses at most SEQ_SAVE_EVERY numbers plus the handful sent while that write is on its way —
    well inside the SEQ_RESTART_MARGIN added to a counter found in use. A counter closed cleanly (`async_close`,
    the last thing `JungHomeHub.async_stop` does, after the link is gone) is marked `clean` and continues without
    the margin: a reload (options, a fresh export, a reconfiguration) then costs none of the 16.7 M numbers per IV
    index. The first save after a load is an immediate one that clears the mark again, so a crash right after a
    start is covered by the margin too.
    """

    def __init__(
        self,
        store: SeqStore,
        data: dict[str, Any] | None,
        default_src: int,
        key: str,
        backup: SeqStore | None = None,
        entry_id: str | None = None,
        floor: SeqStore | None = None,
    ) -> None:
        """Wrap `store` (and, if given, its `.backup` copy and the `.floor`) and continue the configured address.

        `key` is the mesh UUID `seq_store` keys `store` by. Claiming ownership of it (`SEQ_OWNERS`) here, before
        `super().__init__` runs `persist()` for the first time, makes this the one `HAState` allowed to write —
        a reload can start a successor while `JungHomeHub.async_stop` is still finishing an old one behind it
        (`ConfigEntry._async_process_on_unload`'s 10 s wait does not block the reload), and the old one's
        `persist()`/`async_close()` must not overwrite what the new one has already sent. `entry_id`
        names the config entry whose hub this is: a successor must be of the same entry (`async_load_state`
        refuses another entry's hub for a mesh that already has a running one).
        """
        self._store = store
        self._key = key
        self.entry_id = entry_id
        self._backup_store = backup
        self._floor_store = floor
        # (tx IV index, seq) of the floor write last asked for: another is asked SEQ_FLOOR_EVERY on, or by a stall
        self._floor_asked: tuple[int, int] | None = None
        # `_restart_point` per copy (allow_clean: True = the store, False = the backup): the `written` object it was
        # read from (kept, so its id cannot be reused by another), our address then, and the point (None: no record)
        self._restart_points: dict[bool, tuple[Any, int, tuple[int, int] | None]] = {}
        hass = store.hass
        hass.data.setdefault(SEQ_OWNERS, {})[key] = self
        # a backup being taken right now (`backup.async_pre_backup`): its mark goes into every record this one
        # writes too, or a reload during the backup would leave the archive a record without it
        self.backup_token: str | None = hass.data.get(SEQ_BACKUP_TOKEN)
        self.backup_at: float | None = hass.data.get(SEQ_BACKUP_AT)
        self._addresses: dict[str, dict[str, Any]] = {
            src: _without_mark(record, self.backup_token)
            for src, record in ((data or {}).get("addresses") or {}).items()
        }
        self._closed = False
        self._saved: tuple[int, int] | None = (
            None  # (tx IV index, seq) of the last forced save
        )
        self._stalled_at: float | None = (
            None  # monotonic time reserve_seq() last forced a save while refusing (the retry throttle)
        )
        # monotonic time reserve_seq() first refused since the last one that succeeded; told to `stall_listener`
        # (the hub: it times `report_unwritable`, and cancels that timer when it stops)
        self._stalled_since: float | None = None
        # seconds of the stalls that ended (`held_back_total`: the link history's per-link share)
        self._stalled_before = 0.0
        self.stall_listener: Callable[[], None] | None = None
        # `seq_store_unwritable` was raised and not cleared since (the hub's stop clears it too, `_clear_issues`)
        self._stall_issue_open = False
        self._mesh = _mesh_of(data)
        src = f"{default_src:04X}"
        record = self._addresses.get(src)
        # (IV index, seq), the highest another client was seen sending from our address with; None: none seen (or
        # skipped past). Set before `super().__init__`, whose first `persist()` writes it back
        self.address_shared = _stored_address_shared(record)
        self._record = (
            None
            if record is None
            else {**record, "src": src, "key_refresh": _stored_key_refresh(data, src)}
        )
        margin = 0 if record is not None and record.get("clean") else SEQ_RESTART_MARGIN
        # what a restore of this record skips (`_restore_skip`), measured on from where the record left it
        self.send_rate = SendRate.from_stored(
            record.get("send_rate") if isinstance(record, dict) else None
        )
        self.send_rate.tick(wall_now())
        super().__init__(
            None, default_src, restart_margin=margin, configured_src_wins=True
        )

    def _owns_the_store(self) -> bool:
        """Whether this is still the mesh's current `HAState`: a superseded one must never write again."""
        return self._store.hass.data.get(SEQ_OWNERS, {}).get(self._key) is self

    def load(self) -> dict[str, Any] | None:
        """Return the configured address's record (with the address), as the store held it when the hub was created.

        Its key refresh is the mesh's (`_stored_key_refresh`).
        """
        return self._record

    def persist_now(self) -> None:
        """Start a write of both copies now rather than after the 2 s debounce (`set_key_refresh`)."""
        self._saved = None  # force the immediate-save branch
        self.persist()

    def persist(self) -> None:
        """Schedule a save: debounced by 2 s, or started at once every SEQ_SAVE_EVERY numbers, on an IV change and after a load.

        Called by `LocalState.__init__` too (the store is set before that runs): that first call is an immediate
        one, clearing the `clean` mark of a store that was closed properly. The immediate write is a task of its
        own (`Store.async_save`), not a zero delay: a pending delayed write would swallow the latter.

        `_addresses` (every *other* address' record) is the snapshot the store held when this `HAState` was
        built and is never refreshed: `seq_store` hands out one `Store` object per mesh (`SEQ_STORES`) so two
        live hubs of one mesh — the config flow refuses a second entry for an already-configured mesh
        (`_mesh_uuid_taken`), but a store edited by hand, or an installation upgraded from before that check,
        could still have two — take turns *scheduling* writes rather than racing independent files (`Store`
        never lets an older *scheduled* write land after a newer one, `homeassistant.helpers.storage.Store`'s
        single pending-write slot), but neither hub's `_addresses` ever learns the other moved: one running hub
        per mesh (`_refuse_duplicate_mesh`) is what actually avoids the race, not this store.

        A no-op once a successor `HAState` has taken over `_key`: the only numbers this instance has
        not itself persisted are fewer than `SEQ_SAVE_EVERY` plus whatever was in flight, all below what the
        successor loaded (it added `SEQ_RESTART_MARGIN`), so silently dropping this write is safe.
        """
        if not self._owns_the_store():
            return
        self._closed = False
        current = (self.tx_iv_index, self.seq)
        if (
            self._saved is None
            or current[0] != self._saved[0]
            or current[1] - self._saved[1] >= SEQ_SAVE_EVERY
        ):
            self._saved = current
            self._store.hass.async_create_task(
                self._store.async_save(self._snapshot()), f"{DOMAIN} seq store"
            )
            if self._backup_store is not None:
                # always `clean: False`: a restore from the backup must add the margin, it can lag by up to
                # SEQ_SAVE_EVERY plus whatever was in flight when the primary was last written
                self._backup_store.hass.async_create_task(
                    self._backup_store.async_save(self._snapshot(force_dirty=True)),
                    f"{DOMAIN} seq store backup",
                )
        else:
            self._store.async_delay_save(self._snapshot, 2)
        self._advance_floor()

    def _floor_point(self) -> tuple[int, int]:
        """(tx IV index, seq) of the floor entry the `.floor` file durably holds for us; (0, 0) without one.

        Without one, a repair continues SEQ_SKIP_UNKNOWN from 0 (`_seq_skip_target`): the same as an entry there.
        """
        written = None if self._floor_store is None else self._floor_store.written
        point = _furthest([(_addresses_of(written) or {}).get(f"{self.src:04X}")])
        return (0, 0) if point is None else point

    def _advance_floor(self) -> None:
        """Ask for a new floor entry when the counter moved past the one on disk (`SEQ_FLOOR_EVERY`, the class docstring).

        Never one behind it (a restored record or a rewind can leave the counter there). A concrete `seq_guard` the
        entry or the counter holds that still covers our index is carried: numbers went out under every index up to
        it (`async_rewind_seq_floor`). Written at once, as a task; `_limit` holds sends back until one lands, should
        the counter otherwise run too far past the last one that did.
        """
        if self._floor_store is None:
            return
        current = (self.tx_iv_index, self.seq)
        landed = self._floor_point()
        if current[0] < landed[0]:
            return
        for point in (landed, self._floor_asked):
            if (
                point is not None
                and current[0] == point[0]
                and current[1] - point[1] < SEQ_FLOOR_EVERY
            ):
                return
        self._floor_asked = current
        key = f"{self.src:04X}"
        floors = _addresses_of(self._floor_store.written) or {}
        entry: SeqRecord = {"iv_index": current[0], "seq": current[1]}
        guard = _carried_seq_guard([floors.get(key), {"seq_guard": self.seq_guard}])
        if guard >= current[0]:
            entry["seq_guard"] = guard
        self._floor_store.hass.async_create_task(
            self._floor_store.async_save({"addresses": {**floors, key: entry}}),
            f"{DOMAIN} seq floor",
        )

    def _limit(self) -> tuple[int, int]:
        """Return the (tx IV index, seq) every restart could continue from, given what both copies *durably* hold.

        A restart resumes from the store, or from the `.backup` copy when the store is lost or damaged,
        so both bound what may be sent: each copy's restart point is taken (`_restart_point`) and the lower
        one wins — a copy on another transmit index than ours is no bound at all, and holds sends back like a
        store that has written nothing. The backup's writes fail on their own (EIO on its file, a full disk
        between the two writes): bounding by the store alone let sends run on while the copy stayed behind, and
        a restore from it after the store was lost reused every number in between (found by the property tests'
        state machine).
        """
        tx, limit = self._restart_point(self._store.written, allow_clean=True)
        if self._backup_store is not None:
            backup_tx, backup_limit = self._restart_point(
                self._backup_store.written, allow_clean=False
            )
            if backup_tx != tx:
                return self.tx_iv_index, 0
            limit = min(limit, backup_limit)
        if self._floor_store is not None:
            floor_tx, floor_seq = self._floor_point()
            if floor_tx < tx:
                # a repair after both copies are lost continues under the floor's index, and its guard keeps the
                # counter going only up to one past the first beacon's — which may lie below ours
                return tx, 0
            if floor_tx == tx:
                # ... and this far past the floor's number; a floor ahead of us bounds nothing here
                limit = min(limit, floor_seq + SEQ_SKIP_UNKNOWN)
        return tx, limit

    def _restart_point(self, written: Any, *, allow_clean: bool) -> tuple[int, int]:
        """(tx IV index, seq) a restart from one copy's durably written content continues from.

        Nothing durable, nothing (usable) for our address: a restart from it starts at 0, so nothing may be
        reserved until a save lands. A `clean` record needs no margin (that is what marks a clean close) — in the
        store only: a restore from the backup always adds it, as `async_load_state` reads it (the copy is
        written with `clean: False`, `_snapshot`). Any other record gets `SEQ_RESTART_MARGIN`, exactly as a real
        restart's `LocalState.load()` would add.

        Read once per written content: checking the record parses the whole replay list, and
        `reserve_seq` asks for every PDU sent — a quarter of a millisecond with 600 sources, twice. A write never
        edits what landed before, it replaces `written` (`SeqStore._async_write_data`, the setup's own
        `store.written = data`), so the object it is read from tells whether the point still holds.
        """
        cached = self._restart_points.get(allow_clean)
        if cached is not None and cached[0] is written and cached[1] == self.src:
            point = cached[2]
        else:
            record = _usable_record(written, f"{self.src:04X}")
            if record is None:
                point = None
            else:
                tx, seq = _tx_rank(record)
                clean = allow_clean and bool(record.get("clean"))
                point = (tx, seq + (0 if clean else SEQ_RESTART_MARGIN))
            self._restart_points[allow_clean] = (written, self.src, point)
        return (self.tx_iv_index, 0) if point is None else point

    def reserve_seq(self, count: int) -> int:
        """Refuse to hand out numbers a restart could not tell were already used.

        `LocalState.reserve_seq` alone assumes every number it hands out is durably written soon after; nothing
        enforced that here, so a stalled or failing store write let sends run arbitrarily far ahead of what a
        restart would actually load, reusing nonces once it did. Retried every `SEQ_STALL_RETRY` seconds instead
        of once, because the immediate save this forces is itself async — see `persist`.

        A superseded `HAState` refuses outright: it can no longer persist what it hands out, and `_limit`
        reads the shared store's `written`, which the successor keeps advancing into the numbers it sends with.

        The first refusal since a number was last handed out starts a stall (`stalled_for`), which `stall_listener`
        (the hub) hears of, to raise `seq_store_unwritable` if it lasts; the next number handed out ends it.
        """
        if not self._owns_the_store():
            raise SequenceExhausted(
                "a newer hub of this mesh owns the sequence numbers now"
            )
        if self.address_shared is not None:
            raise AddressShared(
                f"another client sends from address {self.src:04X}: nothing is sent until the repair skips past it"
            )
        if self._held_back(count):
            now = time.monotonic()
            if self._stalled_since is None:
                self._stalled_since = now
                if self.stall_listener is not None:
                    self.stall_listener()
            if self._stalled_at is None or now - self._stalled_at >= SEQ_STALL_RETRY:
                if self._stalled_at is None:
                    _LOGGER.warning(
                        "Sequence-number store not written yet: holding back sends so no nonce is reused"
                    )
                self._stalled_at = now
                self._saved = None  # force the immediate-save branch
                self._floor_asked = (
                    None  # and a floor write, should that be what holds sends back
                )
                self.persist()
            raise SequenceStalled(
                "sequence-number store not written yet: holding back to keep nonces unique"
            )
        self._stalled_at = None
        if self._stalled_since is not None or self._stall_issue_open:
            self._end_stall()
        first = super().reserve_seq(count)
        self.send_rate.note_sent(count)
        return first

    def _held_back(self, count: int) -> bool:
        """Whether `count` more numbers lie beyond what a restart could continue from (`_limit`)."""
        tx, limit = self._limit()
        return self.tx_iv_index != tx or self.seq + count > limit

    def report_unwritable(self) -> None:
        """Raise `seq_store_unwritable` if sends are still held back (the hub calls it SEQ_STALL_ISSUE_AFTER into a stall).

        Without it a store that never lands a write (a full disk, an SD card remounted read-only) only showed as
        `pdus_dropped` once the proxy filter went unanswered, and that repair's skip-ahead cannot be written either.
        A store that caught up meanwhile, with nothing sent since, ends the stall here instead; a superseded
        `HAState` reports nothing, its successor has the store now.
        """
        if self._stalled_since is None or not self._owns_the_store():
            return
        if not self._held_back(1):
            self._close_stall()
            return
        path, error = self._stall_cause()
        _LOGGER.error(
            "The sequence-number store %s has not been written for %.0f s (%s): Home Assistant sends nothing to "
            "the mesh until a write lands",
            path,
            time.monotonic() - self._stalled_since,
            error or "no error reported",
        )
        hass = self._store.hass
        entry = (
            None
            if self.entry_id is None
            else hass.config_entries.async_get_entry(self.entry_id)
        )
        if entry is None:
            return
        self._stall_issue_open = True
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id(entry, ISSUE_SEQ_STORE_UNWRITABLE),
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_SEQ_STORE_UNWRITABLE,
            learn_more_url=learn_more_url(ISSUE_SEQ_STORE_UNWRITABLE),
            translation_placeholders={
                "title": entry.title,
                "path": path,
                "error": error or "none reported",
            },
        )

    def _stall_cause(self) -> tuple[str, str | None]:
        """(path, last write error) of the copy holding sends back: the first one whose last write failed (or the floor's)."""
        stores = [self._store]
        if self._backup_store is not None:
            stores.append(self._backup_store)
        if self._floor_store is not None:
            stores.append(self._floor_store)
        for store in stores:
            if store.write_error is not None:
                return store.path, store.write_error
        return self._store.path, None

    def _end_stall(self) -> None:
        """End a stall once a number was handed out: the store caught up, so its repair goes."""
        if self._stalled_since is not None:
            _LOGGER.info(
                "Sequence-number store written again after %.0f s: sending resumes",
                time.monotonic() - self._stalled_since,
            )
        self._close_stall()
        if self._stall_issue_open and self.entry_id is not None:
            ir.async_delete_issue(
                self._store.hass,
                DOMAIN,
                f"{ISSUE_SEQ_STORE_UNWRITABLE}_{self.entry_id}",
            )
        self._stall_issue_open = False

    def _close_stall(self) -> None:
        """Add the stall that ends now to `held_back_total`."""
        if self._stalled_since is not None:
            self._stalled_before += time.monotonic() - self._stalled_since
        self._stalled_since = None

    @property
    def held_back_total(self) -> float:
        """Seconds sends were held back in all, the running stall included (the hub's link history takes differences)."""
        return self._stalled_before + (self.stalled_for or 0.0)

    @property
    def stalled_for(self) -> float | None:
        """Seconds since sends were first held back, None while they are not (the hub's watchdog, diagnostics)."""
        if self._stalled_since is None:
            return None
        return time.monotonic() - self._stalled_since

    @property
    def last_write_error(self) -> str | None:
        """The last `WriteError` of the store or its `.backup` copy (`_stall_cause`); None when both last wrote."""
        return self._stall_cause()[1]

    @property
    def durable_headroom(self) -> int:
        """How many more numbers may go out before a send is held back for the store to catch up (`_limit`)."""
        tx, limit = self._limit()
        return max(limit - self.seq, 0) if tx == self.tx_iv_index else 0

    def skip_ahead(self, count: int) -> int:
        """Move the counter `count` numbers ahead (never past `SEQ_TX_LIMIT`), saved at once; return the new counter.

        Sends are held back until that save lands (`reserve_seq`), so nothing goes out that a restart could reuse.
        A counter past `SEQ_TX_LIMIT` already (its last number sent) stays: capping it moved it back onto that
        number (found by the property tests' state machine).
        """
        self.seq = max(self.seq, min(self.seq + count, SEQ_TX_LIMIT))
        self._saved = None  # force the immediate-save branch
        self.persist()
        return self.seq

    def note_address_shared(self, iv_index: int, seq: int) -> bool:
        """Record that another client sent (`iv_index`, `seq`) from our address; True when it is the first such record.

        Saved at once with the counter: from here on `reserve_seq` refuses (`AddressShared`), across a restart too.
        """
        seen = self.address_shared
        if seen is None or (iv_index, seq) > seen:
            self.address_shared = (iv_index, seq)
            self._saved = None  # force the immediate-save branch
            self.persist()
        return seen is None

    def skip_past_shared(self) -> int | None:
        """Continue past the other client's numbers (the `address_shared` repair); return the new counter, None if none.

        The counter goes SEQ_RESTART_MARGIN past the highest number seen (never past SEQ_MAX, which is never sent):
        the other client may have sent a few more since, and the nodes' replay lists know them. Seen under the next
        IV index (the mesh is in an IV Update, the other client already transmits under the new index), `seq_guard`
        keeps the counter from restarting at 0 there. Saved at once; sends wait for that save (`reserve_seq`).
        """
        seen = self.address_shared
        if seen is None:
            return None
        iv_index, seq = seen
        self.seq = max(self.seq, min(seq + 1 + SEQ_RESTART_MARGIN, SEQ_MAX))
        if iv_index > self.tx_iv_index:
            if self.seq_guard is None:
                self.seq_guard = iv_index
            elif self.seq_guard != SEQ_GUARD_FIRST_BEACON:
                self.seq_guard = max(self.seq_guard, iv_index)
            # else: the first beacon raises it past the index it states, this one at least
        self.address_shared = None
        self._saved = None  # force the immediate-save branch
        self.persist()
        return self.seq

    async def async_close(self) -> None:
        """Write the counter now, marked cleanly closed: the next load of this address needs no restart margin.

        A no-op once superseded: a slow `async_stop` finishing after a reload's successor has already
        started must not write its own, now-stale, counter over the successor's — see `persist`.
        """
        if not self._owns_the_store():
            return
        self._closed = True
        await self._store.async_save(self._snapshot())

    def _snapshot(self, *, force_dirty: bool = False) -> dict[str, Any]:
        """Return the whole store: every address's record, ours from the live state.

        `force_dirty` is for the backup copy: it must never claim a clean close, since it is always somewhat
        behind the primary and a restart from it must add the margin regardless of how this hub actually ended.

        Our record carries the time of the write (`written_at`) and the send rate (`send_rate`, its clock ticked
        here): what a restore of it skips (`_restore_skip`).

        While a backup is being taken (`backup_token`) every record carries its mark and the backup's start
        (`backup_at`), the other addresses' too: an address switched to after the backup and back again before a
        restore comes back just as stale as ours. A record that already carries an earlier backup's mark keeps it
        (it skips ahead either way when next used).
        """
        now = wall_now()
        self.send_rate.tick(now)
        record = {key: value for key, value in self.to_stored().items() if key != "src"}
        record["clean"] = False if force_dirty else self._closed
        if self.address_shared is not None:
            record["address_shared"] = list(self.address_shared)
        record["written_at"] = now
        record["send_rate"] = self.send_rate.to_stored()
        addresses = {**self._addresses, f"{self.src:04X}": record}
        if (token := self.backup_token) is not None:
            mark: dict[str, Any] = {"in_backup": token}
            if self.backup_at is not None:
                mark["backup_at"] = self.backup_at
            addresses = {
                src: other
                if not isinstance(other, dict) or "in_backup" in other
                else {**mark, **other}
                for src, other in addresses.items()
            }
        # the mesh's key refresh, whatever it is (None too: it ends a stale one another address's record still has)
        mesh = {**self._mesh, "key_refresh": record.get("key_refresh")}
        return {"addresses": addresses, "mesh": mesh}

    async def async_save_now(self) -> None:
        """Write both copies now and wait for the writes (the backup hooks: `backup.py`).

        `_closed` is left as it is — a hub stopped before the backup keeps its record `clean` — and a superseded
        `HAState` writes nothing.
        """
        if not self._owns_the_store():
            return
        saves = [self._store.async_save(self._snapshot())]
        if self._backup_store is not None:
            saves.append(
                self._backup_store.async_save(self._snapshot(force_dirty=True))
            )
        await asyncio.gather(*saves)

    async def persist_durably(self) -> None:
        """Write both copies now and check that the store holds our IV state (`ProxyClient.start_iv_update`).

        `Store` only logs a write that fails (`SeqStore`): what landed (`written`) tells. OSError when the store
        does not hold our IV index and update state — a failed write, or a superseded `HAState`, which writes
        nothing — so the IV Update is put back before its beacon goes.
        """
        await self.async_save_now()
        record = _usable_record(self._store.written, f"{self.src:04X}")
        if record is None or (
            record.get("iv_index"),
            record.get("iv_update_active"),
        ) != (self.iv_index, self.iv_update_active):
            raise OSError(
                "the sequence-number store does not hold the new IV state: "
                + (self._store.write_error or "not written")
            )

    def send_rates(self) -> dict[str, dict[str, Any]]:
        """Per address of the store, its send rate as the diagnostics show it (`restore_coverage`); ours live."""
        rates: dict[str, dict[str, Any]] = {}
        for src, record in self._addresses.items():
            usable = _usable_record({"addresses": {src: record}}, src)
            if usable is not None and src != f"{self.src:04X}":
                rate = SendRate.from_stored(usable.get("send_rate"))
                rates[src] = restore_coverage(rate.per_day(), int(usable["seq"]))
        rates[f"{self.src:04X}"] = restore_coverage(self.send_rate.per_day(), self.seq)
        return rates

    def carries_backup_token(self, token: str) -> bool:
        """Whether what both copies durably hold for our address carries the backup's mark `token`."""
        stores = [self._store, self._backup_store]
        return all(
            (record := _usable_record(store.written, f"{self.src:04X}")) is not None
            and record.get("in_backup") == token
            for store in stores
            if store is not None
        )


# the states of an entry whose hub may still send with its mesh's counters (FAILED_UNLOAD: its hub was never stopped)
HUB_RUNNING = (
    ConfigEntryState.SETUP_IN_PROGRESS,
    ConfigEntryState.LOADED,
    ConfigEntryState.FAILED_UNLOAD,
)


def _refuse_duplicate_mesh(
    hass: HomeAssistant, entry: ConfigEntry, mesh_uuid: str
) -> None:
    """Refuse a hub for a mesh another entry's hub is running: raise `ISSUE_DUPLICATE_MESH` for `entry`, then fail.

    `config_flow._mesh_uuid_taken` refuses a second entry for an already-configured mesh, so nothing created
    since that check exists can trigger this; an installation upgraded from before it (or an entry edited by
    hand) can still have two. Both running would share one store with two stale views of it (`HAState.persist`),
    and the later `HAState` takes the counters over (`SEQ_OWNERS`), so the first hub would refuse every send from
    then on — muted, with nothing but a DEBUG line to show. So the first one keeps running and this one does not
    start. The issue is `entry`'s: its removal deletes it (`async_remove_entry`), and so does its next successful
    start (`Issues.clear`). An owner of the same entry (a reload's predecessor) is no obstacle, nor is
    one whose entry is gone or no longer running.
    """
    owner = hass.data.get(SEQ_OWNERS, {}).get(mesh_uuid.lower())
    if owner is None or owner.entry_id in (None, entry.entry_id):
        return
    other = hass.config_entries.async_get_entry(owner.entry_id)
    if other is None or other.state not in HUB_RUNNING:
        return
    _LOGGER.error(
        "%s already runs the JUNG HOME mesh %s: %s is not started — remove one of the two entries",
        other.title,
        mesh_uuid,
        entry.title,
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry, ISSUE_DUPLICATE_MESH),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_DUPLICATE_MESH,
        learn_more_url=learn_more_url(ISSUE_DUPLICATE_MESH),
        translation_placeholders={"title": entry.title, "others": other.title},
    )
    raise ConfigEntryError(
        translation_domain=DOMAIN,
        translation_key=ISSUE_DUPLICATE_MESH,
        translation_placeholders={"others": other.title},
    )


def _evidence_of_use(
    cdb: CDB, unicast: int, keeper: VaultKeeper, *stores: Any
) -> str | None:
    """Why address `unicast`, which has no sequence-number record, may have sent before; None when nothing says so.

    The export holds Home Assistant's provisioner node, or any node, at it (`jhmesh.vault.recognise`); the vault
    keeps Home Assistant's identity in this mesh (it ran here, so a store existed); or the loaded `stores` (the store,
    its `.backup` copy, the floor) know other addresses (this Home Assistant sent in this mesh, a lost record of
    this one would look the same). Wrong only for a genuinely fresh address on an installation that matches: that
    one spends SEQ_SKIP_AHEAD of its 2^24 numbers, once — SEQ_SKIP_UNKNOWN when none of the stores is left at all
    (`async_load_state`).
    """
    if recognise(cdb, unicast) is not None:
        return "the export holds Home Assistant's provisioner node there"
    if cdb.element(unicast) is not None:
        return "a node of the export has that address"
    if keeper.vault is not None:
        return "the vault keeps Home Assistant's identity in this mesh"
    known = sorted({src for data in stores for src in _addresses_of(data) or {}})
    if known:
        return f"the store knows other addresses ({', '.join(known)})"
    return None


async def async_load_state(
    hass: HomeAssistant,
    entry: ConfigEntry,
    cdb: CDB,
    unicast: int,
    vault: VaultKeeper,
) -> HAState:
    """Return the `HAState` address `unicast` of `cdb`'s mesh sends from: where its sequence numbers start.

    The store, else its `.backup` copy, else the `seq_store_lost` issue when something shows the address sent before
    (its history, a corrupt file). One running hub per mesh: another entry's hub already owning the mesh's counters
    (`SEQ_OWNERS`) refuses this one (`_refuse_duplicate_mesh`). A record restored from a Home Assistant backup
    continues past every number sent since (`_async_skip_restored_record`). An address without a record anywhere
    starts at 0 only when nothing says it was used before (`_evidence_of_use`); otherwise SEQ_SKIP_AHEAD on —
    SEQ_SKIP_UNKNOWN when the store, its `.backup` and the floor are all gone (nothing bounds what was sent, and
    an installation stays at one IV index for years, so 2^20 is outrun within one; the same distance the
    `seq_store_lost` repair takes with nothing left). The hub builds itself on it (`JungHomeHub.async_create`).
    """
    store = seq_store(hass, cdb)
    backup = seq_backup_store(hass, cdb)
    floor = seq_floor_store_for_uuid(hass, cdb.mesh_uuid)
    data = await store.async_load()
    backup_data = await backup.async_load()
    floor_data = _landed(floor, await floor.async_load())
    floor.written = floor_data  # what `HAState` keeps the floor up from
    corrupt = (
        await hass.async_add_executor_job(_newest_corrupt_seq_store, store.path)
        if data is None
        else None
    )
    key = f"{unicast:04X}"
    if _usable_record(data, key) is None:
        # a record that parses as JSON but not as a counter used to start at 0 (reusing nonces)
        # and its first save overwrote the good backup; a lost store with history did the same
        if (record := _usable_record(backup_data, key)) is not None:
            _LOGGER.warning(
                "Sequence-number record of address %s unusable in the store, continuing from the backup copy",
                key,
            )
            base = _addresses_of(data) or _addresses_of(backup_data) or {}
            data = _store_with(
                data if _mesh_of(data) else backup_data, {**base, key: record}
            )
            await store.async_save(data)
        elif (
            _has_history(data, key)
            or _has_history(backup_data, key)
            or _has_history(floor_data, key)  # an earlier repair's floor
            or corrupt is not None
        ):
            _report_seq_store_lost(hass, entry, cdb.mesh_uuid, key, corrupt)
        elif data is None and backup_data is not None:
            _LOGGER.warning(
                "sequence-number store unreadable, continuing from the backup copy"
            )
            data = backup_data
            await store.async_save(data)
    restored = await _async_skip_restored_record(
        hass, entry, cdb.mesh_uuid, key, data, backup_data, floor_data
    )
    if restored is not data:
        data = backup_data = restored
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry, ISSUE_SEQ_STORE_LOST))
    # what is on disk right now, before anything new is reserved from it (both copies bound it: `_limit`)
    store.written = data
    backup.written = backup_data
    start = data
    if _usable_record(data, key) is None and (
        why := _evidence_of_use(cdb, unicast, vault, data, backup_data, floor_data)
    ):
        # only in what the `HAState` starts from — on disk it is the first save, which sends
        # wait for (`_limit`): a crash before it lands starts here again, with nothing sent
        nothing_left = data is None and backup_data is None and floor_data is None
        first = SEQ_SKIP_UNKNOWN if nothing_left else SEQ_SKIP_AHEAD
        _LOGGER.warning(
            "Address %s has no sequence-number record, but %s: its numbers start at %06X, past any it may "
            "have sent, rather than at 0",
            key,
            why,
            first,
        )
        start = _store_with(
            data,
            {
                **(_addresses_of(data) or {}),
                key: {
                    "seq": first,
                    "iv_index": 0,
                    "iv_update_active": False,
                    "iv_known": False,
                    "rpl": {},
                    "clean": False,
                    # sent under an index nobody knows: keep counting on up to the first beacon's
                    "seq_guard": SEQ_GUARD_FIRST_BEACON,
                },
            },
        )
    # no await from here to the HAState: two entries set up at once cannot both pass the check
    _refuse_duplicate_mesh(hass, entry, cdb.mesh_uuid)
    return HAState(
        store,
        start,
        unicast,
        cdb.mesh_uuid.lower(),
        backup,
        entry.entry_id,
        floor=floor,
    )
