"""A Hypothesis state machine over `HAState`, the integration's sequence store: no (IV index, seq) is sent twice.

The machine is one mesh's counter for one address in Home Assistant: sends (single numbers and whole segmented
rounds, and long runs that take the counter millions further), beacons and IV Updates, the debounced save firing,
the hub reloaded in the same process (closed
cleanly, or a successor started while the old hub is still stopping — HAC-02), Home Assistant killed with writes
still pending, the storage writes of the store or of its `.backup` copy failing (a full disk, a filesystem gone
read-only), a copy lost or unreadable, the `seq_store_lost` repair (and the floor file it keeps), Home Assistant
backups taken (the integration's `backup` platform hooks around them) and any of them restored later, however much
was sent since — and after every step checks that nothing it was handed was handed before under the same transmit IV
index. A hub's state is built the way the integration builds it: `seq_store.async_load_state` picks the record (the
store, else the backup, else the repair issue) and builds the `HAState` (`JungHomeHub.async_create` calls it).

The address sends at a constant rate, which its records measure (review-5 S5-1): the wall clock the store reads
(`seq_store.wall_now`) moves with every number handed out, RATE numbers a second, the months of a long run too, and
by nothing else — so what a restore skips (twice the rate over the backup's age) is what the design promises to
cover, and a restore of a backup taken long before still continues past every number sent since.

Hypothesis runs in an executor thread; every step is a coroutine on Home Assistant's loop, where the store and
`HAState` live.

What the design does not claim to survive is left out: both copies deleted without a trace (no record, no corrupt
file to show numbers were sent: the address starts at 0, documented), the repair's floor file damaged, two hubs of
one mesh running side by side (refused by `_refuse_duplicate_mesh`), and
of backups: restoring one whose pre-backup write did not land (the hook logs it and lets the backup go on) or that
was taken while the address's setup was refused (no hub owned its counter), restoring a second one after a restore
(the same, an older or a newer one), and restoring one taken before a Home Assistant that stopped during a backup or
before a `seq_store_lost` repair — nothing on disk survives a restore, so a restore continues from what the archive
holds alone, and a jump of the counter is no traffic its rate counts: a start from records taken before it lands
on the numbers sent after it (`backup_taken`, `restore`; documented in `backup.py`).
"""

from __future__ import annotations

import asyncio
import copy
import itertools
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.util.file import WriteError
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome_ble import seq_store as seq_store_module
from custom_components.junghome_ble.backup import async_post_backup, async_pre_backup
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_METADATA_DIR,
    CONF_UNICAST,
    DOMAIN,
)
from custom_components.junghome_ble.identity import async_vault_keeper
from custom_components.junghome_ble.jhmesh.client import (
    IV_RECOVERY_MIN_INTERVAL,
    IV_UPDATE_MIN_STATE,
    SEQ_GUARD_FIRST_BEACON,
    SEQ_TX_LIMIT,
    SequenceExhausted,
)
from custom_components.junghome_ble.jhmesh.keyrefresh import KeyRefreshRecord
from custom_components.junghome_ble.seq_store import (
    SEQ_BACKUP_STORES,
    SEQ_BACKUP_TOKEN,
    SEQ_FLOOR_EVERY,
    SEQ_FLOOR_STORES,
    SEQ_OWNERS,
    SEQ_RESTART_MARGIN,
    SEQ_SKIP_UNKNOWN,
    SEQ_STALL_RETRY,
    SEQ_STORES,
    STORAGE_VERSION,
    HAState,
    SeqStore,
    async_load_state,
    async_skip_seq_store_ahead,
)

from .conftest import CDB_PATH, META_DIR

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# Hypothesis runs in an executor thread here; the first time it does, it registers the thread's own PRNG and its
# garbage-collection heuristic takes the thread-local reference for none at all (hypothesis/internal/entropy.py)
pytestmark = pytest.mark.filterwarnings(
    "ignore:It looks like `register_random` was passed an object"
)

UNICAST = 0x0D00
KEY = f"{UNICAST:04X}"
_meshes = itertools.count(1)
# the address's send rate, numbers per second (8640 a day: the wall clock moves 10 s per number handed out), and the
# wall clock it starts from
RATE = 0.1
WALL_START = 1_000_000_000.0


class Disk:
    """Which writes fail right now, and which store objects belong to a Home Assistant that was killed."""

    def __init__(self) -> None:
        self.failing: set[str] = set()  # "primary" / "backup" / "floor"
        # id() of store objects of a killed process: what they still write is lost
        self.dead: set[int] = set()


class FlakySeqStore(SeqStore):
    """One of the mesh's files — the store, its `.backup` copy (sends are bounded by what it has written too) or the
    repair's `.floor` (`role`) — with writes that fail (logged by `Store`, not raised) or never land (process
    killed)."""

    disk: Disk
    role: str

    async def _async_write_data(self, data: dict[str, Any]) -> None:
        if id(self) in self.disk.dead:
            return
        if self.role in self.disk.failing:
            raise WriteError("No space left on device (injected)")
        await super()._async_write_data(data)


class _Cdb:
    """An export without a node or provisioner at our address (`_evidence_of_use` finds none in it)."""

    provisioners: tuple[()] = ()
    nodes: tuple[()] = ()

    def __init__(self, mesh_uuid: str) -> None:
        self.mesh_uuid = mesh_uuid

    def element(self, _address: int) -> None:
        return None


class HAStateMachine(RuleBasedStateMachine):
    """One address's counter in one mesh's store, across reloads, crashes, failing writes and lost copies."""

    def __init__(
        self, hass: HomeAssistant, hass_storage: dict[str, Any], entry: MockConfigEntry
    ) -> None:
        super().__init__()
        self.hass, self.storage, self.entry = hass, hass_storage, entry
        self.uuid = f"00000000-0000-4000-8000-{next(_meshes):012x}"
        self.primary_key = f"{DOMAIN}.seq.{self.uuid}"
        self.backup_key = f"{self.primary_key}.backup"
        self.disk = Disk()
        self.state: HAState | None = None
        # every store object built, for the tear-down
        self.stores: list[SeqStore] = []
        self.used: dict[tuple[int, int], int] = {}
        self.step = 0
        self.refused = False  # the last start raised the seq_store_lost issue
        self.network_iv = 0  # the highest IV index a beacon announced: the network's
        self.now = 0.0  # the wall clock beacons are applied at (`time_passes` moves it past the IV Update timing)
        # the wall clock the store reads (the module docstring: it moves with the numbers handed out)
        self.wall = WALL_START
        self._clock = patch.object(seq_store_module, "wall_now", lambda: self.wall)
        self._clock.start()
        # the sequence-number files of every backup taken since the last restore (and since nothing made a restore
        # of them one the design does not cover), oldest first
        self.archives: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ plumbing
    def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run one step on Home Assistant's loop (this thread is Hypothesis's)."""
        return asyncio.run_coroutine_threadsafe(coro, self.hass.loop).result(30)

    def _install_stores(self) -> None:
        """Fresh store objects for this mesh, as a newly started Home Assistant builds them."""
        for role, key, stores in (
            ("primary", self.primary_key, SEQ_STORES),
            ("backup", self.backup_key, SEQ_BACKUP_STORES),
            ("floor", f"{self.primary_key}.floor", SEQ_FLOOR_STORES),
        ):
            store = FlakySeqStore(self.hass, STORAGE_VERSION, key, atomic_writes=True)
            store.disk, store.role = self.disk, role
            self.hass.data.setdefault(stores, {})[self.uuid] = store
            self.stores.append(store)

    async def _create(self) -> None:
        """`seq_store.async_load_state` as `JungHomeHub.async_create` calls it: the hub's `HAState`."""
        self.state = None
        try:
            vault = await async_vault_keeper(self.hass, self.uuid)
            self.state = await async_load_state(
                self.hass, self.entry, _Cdb(self.uuid), UNICAST, vault
            )
            self.refused = False
        except ConfigEntryError:
            # the seq_store_lost issue: nothing starts until it is repaired
            self.refused = True
        except ConfigEntryNotReady:
            # a restored record whose floor could not be written: Home Assistant retries the setup later
            self.refused = False

    async def _die(self) -> None:
        """Home Assistant stops dead: whatever it has not written is lost, and nothing of its memory survives."""
        for store in self._stores():
            store._async_cleanup_delay_listener()
            store._async_cleanup_final_write_listener()
            self.disk.dead.add(id(store))
        # writes still queued: they go nowhere
        await self.hass.async_block_till_done()
        self.hass.data.get(SEQ_OWNERS, {}).pop(self.uuid, None)
        self.hass.data.pop(SEQ_BACKUP_TOKEN, None)

    def _seq_keys(self) -> tuple[str, ...]:
        """The mesh's sequence-number files in `.storage`: the store, its backup copy and the repair's floor."""
        return (self.primary_key, self.backup_key, f"{self.primary_key}.floor")

    def _stores(self) -> list[SeqStore]:
        return [s for s in self.stores if id(s) not in self.disk.dead]

    def record(self, key: str) -> dict[str, Any] | None:
        """Our address's record as the given store holds it on "disk" right now; None when missing or garbage."""
        try:
            rec = self.storage[key]["data"]["addresses"][KEY]
            int(rec["seq"])
        except (KeyError, TypeError, ValueError):
            return None
        return rec

    # ------------------------------------------------------------------ set-up / tear-down
    @initialize(
        start=st.sampled_from((None, 0, 5000, SEQ_TX_LIMIT - 600)),
        iv_index=st.sampled_from((0, 7)),
    )
    def start(self, start: int | None, iv_index: int) -> None:
        """A mesh never seen, or one whose store a previous run left (possibly near the end of the sequence space)."""
        self.network_iv = iv_index
        if start is not None:
            record = {
                "seq": start,
                "iv_index": iv_index,
                "iv_update_active": False,
                "clean": False,
            }
            for key in (self.primary_key, self.backup_key):
                self.storage[key] = {
                    "version": STORAGE_VERSION,
                    "key": key,
                    "data": {"addresses": {KEY: record}},
                }
        self._install_stores()
        self.run(self._create())

    def teardown(self) -> None:
        self._clock.stop()

        async def clean_up() -> None:
            await self.hass.async_block_till_done()
            for store in self.stores:
                store._async_cleanup_delay_listener()
                store._async_cleanup_final_write_listener()
            self.hass.data.get(SEQ_STORES, {}).pop(self.uuid, None)
            self.hass.data.get(SEQ_BACKUP_STORES, {}).pop(self.uuid, None)
            self.hass.data.get(SEQ_FLOOR_STORES, {}).pop(self.uuid, None)
            self.hass.data.get(SEQ_OWNERS, {}).pop(self.uuid, None)
            self.hass.data.pop(SEQ_BACKUP_TOKEN, None)
            self.storage.pop(self.primary_key, None)
            self.storage.pop(self.backup_key, None)
            self.storage.pop(f"{self.primary_key}.floor", None)

        self.run(clean_up())

    # ------------------------------------------------------------------ the hub
    @precondition(lambda self: self.state is not None)
    @rule(count=st.integers(1, 32))
    def send(self, count: int) -> None:
        """A message: one number, or a segmented message's round (up to 32 at once)."""
        self._note(self.run(self._reserve(count)), count)

    async def _reserve(self, count: int) -> tuple[int, int] | None:
        """(transmit IV index, first number) the hub hands out for a message of `count` numbers, if it sends one."""
        state = self.state
        assert state is not None
        tx = state.tx_iv_index
        try:
            return tx, state.reserve_seq(count)
        except SequenceExhausted:
            return None  # held back (the store is behind, or the space is used up): nothing sent

    def _note(self, got: tuple[int, int] | None, count: int) -> None:
        """Record what a message went out with; fail on a number handed out before under the same index."""
        if got is None:
            return
        tx, first = got
        self.step += 1
        self.wall += count / RATE
        for seq in range(first, first + count):
            assert 0 <= seq <= SEQ_TX_LIMIT
            assert (tx, seq) not in self.used, (
                f"nonce reuse: IV {tx} seq {seq:06X} (first sent at step {self.used[(tx, seq)]})"
            )
            self.used[(tx, seq)] = self.step

    @precondition(lambda self: self.state is not None)
    @rule(delta=st.sampled_from((-1, 0, 1, 2, 42)), update=st.booleans())
    def beacon(self, delta: int, update: bool) -> None:
        """A Secure Network Beacon from the proxy."""
        state = self.state
        assert state is not None
        iv_index = self._beacon_index(delta)
        if iv_index is None:
            return

        async def apply() -> None:
            state.apply_beacon(iv_index, update, now=self.now)

        self.run(apply())

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

    @precondition(lambda self: self.state is not None)
    @rule()
    def iv_update(self) -> None:
        """A whole IV Update: index + 1 "in progress", then normal operation (the sequence restarts at 0)."""
        state = self.state
        assert state is not None

        iv_index = self._beacon_index(1)
        if iv_index is None:
            return

        async def apply() -> None:
            self.now += IV_UPDATE_MIN_STATE  # the spec's minimum time in each state
            state.apply_beacon(iv_index, iv_update=True, now=self.now)
            self.now += IV_UPDATE_MIN_STATE
            state.apply_beacon(iv_index, iv_update=False, now=self.now)

        self.run(apply())

    @rule()
    def time_passes(self) -> None:
        """Pending writes run, the 2 s debounce fires, a held-back hub's retry interval elapses — and the wall clock
        moves past the IV Update timing (`LocalState.apply_beacon`: a recovery may follow 192 hours later)."""
        self.now += IV_RECOVERY_MIN_INTERVAL
        self.run(self._elapse())

    async def _elapse(self) -> None:
        await self.hass.async_block_till_done()
        for store in self._stores():
            if store._delay_handle is not None:
                store._async_cleanup_delay_listener()
                await store._async_callback_delayed_write()
        if self.state is not None and self.state._stalled_at is not None:
            self.state._stalled_at -= SEQ_STALL_RETRY
        await self.hass.async_block_till_done()

    @precondition(lambda self: self.state is not None)
    @rule(rounds=st.integers(1, 40))
    def busy(self, rounds: int) -> None:
        """A busy stretch: segmented rounds, time passing after each — the counter runs well past what any copy
        held when it began (what a restore, or a copy lost now, would have to account for)."""

        async def stretch() -> list[tuple[int, int] | None]:
            sent = []
            for _ in range(rounds):
                sent.append(await self._reserve(32))
                await self._elapse()
            return sent

        for got in self.run(stretch()):
            self._note(got, 32)

    @precondition(lambda self: self.state is not None)
    @rule(chunks=st.integers(1, 5))
    def long_run(self, chunks: int) -> None:
        """Months of traffic at once: the counter `chunks` times SEQ_FLOOR_EVERY on (`HAState.skip_ahead`, saved at
        once) as numbers handed out at the address's rate — counted by its `SendRate`, the wall clock moved by the
        time they take — then a send there: past what a repair would continue to from the floor written before, for
        the floor to keep up with (review-4 S4-8), and more than SEQ_SKIP_AHEAD, so a restore of a backup taken
        before has to skip by the rate (review-5 S5-1)."""
        state = self.state
        assert state is not None
        count = chunks * SEQ_FLOOR_EVERY

        async def run() -> None:
            before = state.seq
            state.skip_ahead(count)
            state.send_rate.note_sent(state.seq - before)
            self.wall += (state.seq - before) / RATE
            await self._elapse()

        self.run(run())
        self.send(count=1)

    @precondition(lambda self: self.state is not None)
    @rule()
    def reload(self) -> None:
        """The entry reloads (options, a new export): the hub stops — its counter saved as cleanly closed — and
        a new one starts in the same Home Assistant, on the same store objects."""
        state = self.state
        assert state is not None

        async def reload() -> None:
            await state.async_close()
            await self._create()

        self.run(reload())

    @precondition(lambda self: self.state is not None)
    @rule()
    def reload_while_stopping(self) -> None:
        """HAC-02: the successor starts while the old hub's stop still runs; the old one must not send or save."""
        old = self.state
        assert old is not None

        async def reload() -> None:
            await self._create()
            # (a successor that refused to start supersedes nothing)
            if self.state is not None:
                try:
                    old.reserve_seq(1)
                except SequenceExhausted:
                    pass
                else:
                    raise AssertionError(
                        "a superseded HAState handed out a sequence number"
                    )
            await old.async_close()

        self.run(reload())

    @rule()
    def killed(self) -> None:
        """Home Assistant dies: whatever it has not written is lost; it starts again from the storage files."""

        async def restart() -> None:
            await self._die()
            self._install_stores()
            await self._create()

        self.run(restart())

    @rule(during=st.sampled_from(("nothing", "reload", "killed")))
    def backup_taken(self, during: str) -> None:
        """A Home Assistant backup: the integration's pre-backup hook, the archive of the sequence-number files, the
        post-backup hook. `during` the backup the entry may reload (a successor starts between the hooks: its record
        must carry the backup's mark too, and its start must not take the mark for a restore), or Home Assistant may
        die before the post-backup hook ran (the files keep the mark: the next start skips ahead once).

        A restore is covered when a hub owned the counter and both copies could be written when the backup was
        taken; otherwise the archive is never restored. A backup Home Assistant died during leaves no archive, and
        the ones before it are not restored any more either: the start after the stop skipped ahead from the very
        records a restore of them may continue from, a jump no rate counts (the module docstring's exclusions).
        """

        async def take() -> dict[str, Any] | None:
            await async_pre_backup(self.hass)
            if during == "reload" and self.state is not None:
                await self.state.async_close()
                await self._create()
            covered = self.state is not None and not (
                {"primary", "backup"} & self.disk.failing
            )
            archive = {
                key: copy.deepcopy(self.storage[key])
                for key in self._seq_keys()
                if key in self.storage
            }
            if during == "killed":
                await self._die()
                self._install_stores()
                await self._create()
                return None
            await async_post_backup(self.hass)
            return archive if covered else None

        archive = self.run(take())
        if during == "killed":
            self.archives.clear()
        elif archive is not None:
            self.archives.append(archive)

    @precondition(lambda self: self.archives)
    @rule(which=st.integers(0, 1 << 16))
    def restore(self, which: int) -> None:
        """A backup taken at any earlier step is restored: Home Assistant stops, its configuration directory is
        replaced by the archive's — the store, its backup copy and the floor come back together, readable — and it
        starts again. The network does not go back with it: its IV index stays, and so does every number already
        sent, however long ago the backup was taken. No other backup is restored after it (the module docstring)."""
        archive = self.archives[which % len(self.archives)]
        self.archives.clear()

        async def restore() -> None:
            await self._die()
            for key in self._seq_keys():
                if key in archive:
                    self.storage[key] = copy.deepcopy(archive[key])
                else:
                    self.storage.pop(key, None)
            self._install_stores()
            await self._create()

        self.run(restore())
        if self.state is not None:
            # a restored hub sends at once (the proxy filter, the connect-time refresh)
            self.send(count=1)

    @precondition(lambda self: self.refused)
    @rule()
    def repair(self) -> None:
        """The user runs the `seq_store_lost` repair: the address continues far past any number left anywhere."""

        async def fix() -> None:
            await async_skip_seq_store_ahead(self.hass, self.uuid, KEY)
            await self._create()

        self.run(fix())
        self.archives.clear()  # a jump no rate counts: no backup from before it is restored

    # ------------------------------------------------------------------ the disk
    @rule(
        failing=st.sampled_from(
            ((), ("primary",), ("backup",), ("floor",), ("primary", "backup", "floor"))
        )
    )
    def disk_fails(self, failing: tuple[str, ...]) -> None:
        """What fails from now on: nothing, one file (EIO on its inode) or the whole disk (every file)."""
        self.disk.failing = set(failing)

    @rule(
        which=st.sampled_from(("primary", "backup")),
        how=st.sampled_from(("lost", "garbled")),
    )
    def copy_damaged(self, which: str, how: str) -> None:
        """One copy becomes unusable on disk: gone (HA renamed a corrupt file aside and loads nothing) or holding
        garbage for our address. A copy is only deleted while the other holds our record: both gone without a
        trace is the documented start-at-0 case.

        Both copies unreadable is the `seq_store_lost` repair's case: it continues SEQ_SKIP_UNKNOWN past the floor
        the hub keeps up with the counter (or past 0 without one), at any time — however far the counter ran since
        the last repair, and as often as both copies are lost (review-4 S4-8: the floor was the last repair's only,
        and the next repair reused every number sent beyond that distance).
        """
        key, other = (
            (self.primary_key, self.backup_key)
            if which == "primary"
            else (self.backup_key, self.primary_key)
        )
        if how == "lost":
            if self.record(other) is not None:
                self.storage.pop(key, None)
        elif self.record(key) is not None:
            self.storage[key]["data"]["addresses"][KEY] = {"seq": "garbage"}

    @invariant()
    def superseded_hubs_stay_quiet(self) -> None:
        """Only the newest `HAState` owns the mesh's counters."""
        if self.state is not None:
            assert self.hass.data[SEQ_OWNERS].get(self.uuid) is self.state


@pytest.mark.slow_ok
async def test_ha_state_never_reuses_a_nonce(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Seq store machine",
        data={CONF_CDB_PATH: CDB_PATH, CONF_METADATA_DIR: META_DIR, CONF_UNICAST: KEY},
    )
    entry.add_to_hass(hass)
    await hass.async_add_executor_job(
        run_state_machine_as_test, lambda: HAStateMachine(hass, hass_storage, entry)
    )


async def test_a_backup_whose_writes_fail_holds_sends_back(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Seq store machine",
        data={CONF_CDB_PATH: CDB_PATH, CONF_METADATA_DIR: META_DIR, CONF_UNICAST: KEY},
    )
    entry.add_to_hass(hass)

    def steps() -> None:
        # the counterexample the machine found before the fix: the backup's writes fail while the store's land,
        # the store is then lost, and the restart restores from the stale copy
        machine = HAStateMachine(hass, hass_storage, entry)
        try:
            machine.start(start=None, iv_index=0)
            machine.disk.failing.add("backup")
            machine.killed()
            machine.killed()
            machine.send(count=1)
            machine.copy_damaged(how="lost", which="primary")
            machine.killed()
            machine.killed()
            machine.send(count=1)
        finally:
            machine.teardown()
        # and a hub whose backup cannot be written sends no further than a restore from the stuck copy starts
        machine = HAStateMachine(hass, hass_storage, entry)
        try:
            machine.start(start=None, iv_index=0)
            machine.time_passes()
            backup = machine.record(machine.backup_key)
            assert backup is not None
            machine.disk.failing.add("backup")
            for _ in range(40):
                machine.send(count=32)
                machine.time_passes()
            assert machine.used
            sent = max(seq for _tx, seq in machine.used)
            assert sent < backup["seq"] + SEQ_RESTART_MARGIN
            assert machine.record(machine.backup_key) == backup
        finally:
            machine.teardown()

    await hass.async_add_executor_job(steps)


async def test_a_second_loss_of_both_copies_continues_past_the_first_repair(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Seq store machine",
        data={CONF_CDB_PATH: CDB_PATH, CONF_METADATA_DIR: META_DIR, CONF_UNICAST: KEY},
    )
    entry.add_to_hass(hass)

    def steps() -> None:
        # the machine's counterexample: both copies lost, repaired (SEQ_SKIP_UNKNOWN from 0), both lost again after
        # a send there — and the second repair, with nothing readable, went to SEQ_SKIP_UNKNOWN from 0 again,
        # repeating IV 0 seq 0x400200. The floor file now remembers where the first one continued.
        machine = HAStateMachine(hass, hass_storage, entry)
        try:
            machine.start(start=None, iv_index=0)
            machine.copy_damaged(how="lost", which="primary")
            machine.copy_damaged(how="garbled", which="backup")
            machine.killed()
            assert machine.refused
            machine.repair()
            assert machine.state is not None
            assert machine.state.seq == SEQ_SKIP_UNKNOWN + SEQ_RESTART_MARGIN
            machine.copy_damaged(how="lost", which="backup")
            machine.copy_damaged(how="garbled", which="primary")
            assert machine.record(machine.primary_key) is None
            assert machine.record(machine.backup_key) is None
            machine.send(count=1)
            for _ in range(4):
                machine.killed()
                assert machine.refused
            machine.repair()
            assert machine.state is not None
            assert machine.state.seq == 2 * SEQ_SKIP_UNKNOWN + SEQ_RESTART_MARGIN
            machine.send(count=1)
            # a floor that cannot be written stops the repair before the copies are touched: still refused
            machine.copy_damaged(how="lost", which="backup")
            machine.copy_damaged(how="garbled", which="primary")
            machine.disk.failing.add("floor")
            machine.killed()
            machine.repair()
            assert machine.refused
            assert machine.record(machine.primary_key) is None
            machine.disk.failing.clear()
            machine.repair()
            assert machine.state is not None
            assert machine.state.seq == 3 * SEQ_SKIP_UNKNOWN + SEQ_RESTART_MARGIN
            machine.send(count=1)
        finally:
            machine.teardown()

    await hass.async_add_executor_job(steps)


async def test_the_repair_without_a_readable_record_keeps_counting_under_the_mesh_index(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Seq store machine",
        data={CONF_CDB_PATH: CDB_PATH, CONF_METADATA_DIR: META_DIR, CONF_UNICAST: KEY},
    )
    entry.add_to_hass(hass)

    def steps() -> None:
        # what the machine found before the fix: the repair continued under IV index 0, and the beacons that took
        # it to the mesh's index 2 restarted the sequence at 0 there, over the numbers the lost record had sent
        machine = HAStateMachine(hass, hass_storage, entry)
        try:
            machine.start(start=None, iv_index=0)
            machine.beacon(delta=2, update=False)  # the mesh is at IV index 2
            machine.send(count=1)
            machine.storage.pop(machine.primary_key)
            machine.storage[machine.backup_key]["data"]["addresses"][KEY] = {"seq": "x"}
            machine.killed()
            assert machine.refused  # the seq_store_lost issue
            machine.repair()
            assert machine.state is not None
            assert machine.state.seq_guard == SEQ_GUARD_FIRST_BEACON
            machine.killed()  # a restart before any beacon keeps the guard (it is in the record)
            assert machine.state is not None
            assert machine.state.seq_guard == SEQ_GUARD_FIRST_BEACON
            machine.beacon(delta=0, update=False)  # the proxy's first beacon: index 2
            assert machine.state.tx_iv_index == 2
            assert (
                machine.state.seq_guard == 3
            )  # one past it: the proxy may still be behind an update
            assert machine.state.seq >= SEQ_SKIP_UNKNOWN
            machine.send(count=1)
            machine.time_passes()
            machine.killed()  # the guard survives a restart too
            assert machine.state is not None
            assert machine.state.seq_guard == 3
            machine.iv_update()  # index 3 is still guarded: the counter carries on
            assert (machine.state.tx_iv_index, machine.state.seq_guard) == (3, 3)
            assert machine.state.seq >= SEQ_SKIP_UNKNOWN
            machine.send(count=1)
            machine.iv_update()  # index 4 was never used: the sequence starts over
            assert (machine.state.tx_iv_index, machine.state.seq_guard) == (4, None)
            assert machine.state.seq == 0
            machine.send(count=1)
        finally:
            machine.teardown()

    await hass.async_add_executor_job(steps)


async def test_a_restored_backup_never_resumes_below_numbers_sent(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Seq store machine",
        data={CONF_CDB_PATH: CDB_PATH, CONF_METADATA_DIR: META_DIR, CONF_UNICAST: KEY},
    )
    entry.add_to_hass(hass)

    def steps() -> None:
        # review-4 S4-1, the reviewer's reproduction: a backup taken mid-run, sends going on, the backup restored —
        # primary, backup copy and floor all come back readable, and the restart resumed below the numbers sent
        machine = HAStateMachine(hass, hass_storage, entry)
        try:
            machine.start(start=None, iv_index=0)
            machine.send(count=1)
            machine.time_passes()
            machine.backup_taken(during="nothing")
            for _ in range(40):
                machine.send(count=32)
            machine.time_passes()
            machine.restore(which=0)
            machine.send(count=1)
        finally:
            machine.teardown()

    await hass.async_add_executor_job(steps)


async def test_a_backup_restored_after_months_of_traffic_skips_by_the_rate(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Seq store machine",
        data={CONF_CDB_PATH: CDB_PATH, CONF_METADATA_DIR: META_DIR, CONF_UNICAST: KEY},
    )
    entry.add_to_hass(hass)

    def steps() -> None:
        # review-5 S5-1: an hour and more of sending measured, a backup, then months of traffic at that rate (three
        # times 2^20 numbers) and the restore of the backup taken before them — the fixed 2^20 skip resumed below the
        # numbers sent since; twice the rate over the backup's age does not. A second backup, taken after the
        # months, may be the one restored too; the restore of a backup older than the space can cover sends nothing.
        for which in (0, 1):
            machine = HAStateMachine(hass, hass_storage, entry)
            try:
                machine.start(start=None, iv_index=0)
                for _ in range(15):
                    machine.busy(rounds=40)
                machine.backup_taken(during="nothing")
                machine.long_run(chunks=3)
                machine.backup_taken(during="nothing")
                machine.busy(rounds=10)
                machine.restore(which=which)
                assert machine.state is not None
                assert machine.state.seq > max(seq for _tx, seq in machine.used)
                machine.busy(rounds=10)
            finally:
                machine.teardown()
        machine = HAStateMachine(hass, hass_storage, entry)
        try:
            machine.start(start=None, iv_index=0)
            for _ in range(15):
                machine.busy(rounds=40)
            machine.backup_taken(during="nothing")
            machine.long_run(chunks=5)
            machine.long_run(chunks=4)
            machine.restore(which=0)
            assert machine.state is not None
            assert (
                machine.state.seq > SEQ_TX_LIMIT
            )  # refused: nothing goes out under this index
            sent = len(machine.used)
            machine.busy(rounds=5)
            assert len(machine.used) == sent
            machine.beacon(
                delta=0, update=False
            )  # the proxy's beacon on the next link: still index 0
            machine.iv_update()
            assert (
                machine.state.seq > SEQ_TX_LIMIT
            )  # the first beacon's index + 1 is guarded: still nothing
            machine.iv_update()  # the second update starts over
            assert machine.state.seq < SEQ_TX_LIMIT
            machine.busy(rounds=5)
        finally:
            machine.teardown()

    await hass.async_add_executor_job(steps)


async def test_a_restored_backup_keeps_counting_under_an_index_moved_on_since(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Seq store machine",
        data={CONF_CDB_PATH: CDB_PATH, CONF_METADATA_DIR: META_DIR, CONF_UNICAST: KEY},
    )
    entry.add_to_hass(hass)

    def steps() -> None:
        # the IV index moved on between the backup and the restore: the first beacon after the restore restarted
        # the counter at 0 under the index the numbers sent since the backup went out with
        machine = HAStateMachine(hass, hass_storage, entry)
        try:
            machine.start(start=5000, iv_index=7)
            machine.beacon(delta=0, update=False)
            machine.backup_taken(during="nothing")
            machine.iv_update()
            for _ in range(40):
                machine.send(count=32)
            machine.time_passes()
            machine.restore(which=0)
            machine.beacon(delta=0, update=False)
            machine.send(count=1)
        finally:
            machine.teardown()

    await hass.async_add_executor_job(steps)


async def test_an_address_far_past_the_last_repair_loses_both_copies_twice(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Seq store machine",
        data={CONF_CDB_PATH: CDB_PATH, CONF_METADATA_DIR: META_DIR, CONF_UNICAST: KEY},
    )
    entry.add_to_hass(hass)

    def steps() -> None:
        # review-4 S4-8: the counter runs past SEQ_SKIP_UNKNOWN under one index, then both copies are lost twice.
        # With the floor only the repair wrote, the first repair continued SEQ_SKIP_UNKNOWN from 0, onto the numbers
        # sent just before the loss, and the second from the first's floor
        machine = HAStateMachine(hass, hass_storage, entry)
        try:
            machine.start(start=None, iv_index=0)
            machine.send(count=1)
            machine.long_run(chunks=4)
            machine.busy(rounds=40)
            for _ in range(2):
                machine.copy_damaged(how="lost", which="primary")
                machine.copy_damaged(how="garbled", which="backup")
                machine.killed()
                assert machine.refused
                machine.repair()
                assert machine.state is not None
                machine.busy(rounds=40)
        finally:
            machine.teardown()
        # a floor that cannot be written holds the counter back SEQ_SKIP_UNKNOWN past the last entry that landed
        # (none: from 0), so a repair from it still covers every number sent
        machine = HAStateMachine(hass, hass_storage, entry)
        try:
            machine.start(start=None, iv_index=0)
            machine.disk.failing.add("floor")
            machine.long_run(chunks=5)
            assert machine.state is not None
            assert machine.state.durable_headroom == 0
            assert not machine.used  # the long run's send was held back
            machine.copy_damaged(how="lost", which="primary")
            machine.copy_damaged(how="garbled", which="backup")
            machine.killed()
            assert machine.refused
            machine.disk.failing.clear()
            machine.repair()
            machine.busy(rounds=40)
        finally:
            machine.teardown()

    await hass.async_add_executor_job(steps)


async def test_a_key_refresh_survives_a_kill_right_after_it_was_learnt(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Seq store machine",
        data={CONF_CDB_PATH: CDB_PATH, CONF_METADATA_DIR: META_DIR, CONF_UNICAST: KEY},
    )
    entry.add_to_hass(hass)
    refresh = KeyRefreshRecord(bytes(range(0x40, 0x50)), 3, "beacon")

    def steps() -> None:
        # review-4 S4-9: the new key went out with the 2 s debounce; Home Assistant killed within those 2 s came
        # back with the old key only, the mesh having moved on
        machine = HAStateMachine(hass, hass_storage, entry)
        try:
            machine.start(start=5000, iv_index=7)
            assert machine.state is not None
            state = machine.state

            async def learn() -> None:
                state.set_key_refresh(refresh)
                await (
                    hass.async_block_till_done()
                )  # the writes started, not the 2 s timer

            machine.run(learn())
            machine.killed()
            assert machine.state is not None
            assert machine.state.key_refresh == refresh
            data = machine.storage[machine.primary_key]["data"]
            assert data["mesh"]["key_refresh"] == refresh.to_stored()
        finally:
            machine.teardown()

    await hass.async_add_executor_job(steps)
