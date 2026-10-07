"""The backup platform and the restored-record skip (review-4 D5): no restore resumes below numbers already sent."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.junghome_ble import backup
from custom_components.junghome_ble import seq_store as seq_store_module
from custom_components.junghome_ble.const import (
    DOMAIN,
    ISSUE_RESTORE_TOO_OLD,
    SEQ_SKIP_AHEAD,
    issue_id,
    learn_more_url,
)
from custom_components.junghome_ble.coordinator import JungHomeHub, seq_store
from custom_components.junghome_ble.jhmesh.client import (
    SEQ_GUARD_FIRST_BEACON,
    SEQ_TX_LIMIT,
)
from custom_components.junghome_ble.seq_store import (
    SEQ_BACKUP_AT,
    SEQ_BACKUP_TOKEN,
    SEQ_OWNERS,
    SEQ_RATE_UNMEASURED,
    SEQ_RESTART_MARGIN,
    SEQ_SKIP_UNKNOWN,
    SEQ_STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    ClockBehind,
    HAState,
    SendRate,
    SeqStore,
    _restore_skip,
    _without_mark,
    restore_coverage,
    seq_backup_store,
)

from .conftest import wait_for_link
from .helpers import SEQ_STORE_KEY

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

BACKUP_KEY = f"{SEQ_STORE_KEY}.backup"
FLOOR_KEY = f"{SEQ_STORE_KEY}.floor"
# the wall clock of the tests that set it (`seq_store.wall_now`), and a day of it
NOW = 1_800_000_000.0
DAY = 86_400.0
# the send rate a record keeps after a day of sending 1000 numbers (`SendRate.to_stored`)
A_DAY_OF_1000 = {"per_day": 1000.0, "sent": 0, "seconds": 0.0}


def hub_of(entry: MockConfigEntry) -> JungHomeHub:
    return entry.runtime_data


def record_in(hass_storage: dict[str, Any], key: str, src: str = "0D00") -> Any:
    return hass_storage[key]["data"]["addresses"][src]


def stored(record: dict[str, Any]) -> dict[str, Any]:
    """A store document holding `record` for 0D00, as Home Assistant's storage helper writes it."""
    return {
        "version": STORAGE_VERSION,
        "minor_version": SEQ_STORAGE_MINOR_VERSION,
        "data": {"addresses": {"0D00": record}},
    }


async def stop_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_hooks_mark_both_copies_and_clear_the_mark(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Every record the archive can hold carries the backup's mark — both copies, every address — and the mark
    goes again afterwards; the counter itself is not touched."""
    hub = hub_of(init_integration)
    hass_storage[SEQ_STORE_KEY]["data"]["addresses"]["0D07"] = {"seq": 5}
    hub.state._addresses["0D07"] = {"seq": 5}
    hub.state._addresses["0D09"] = "not a record"
    seq = hub.state.seq
    with patch.object(seq_store_module, "wall_now", return_value=NOW):
        await backup.async_pre_backup(hass)
    token = hass.data[SEQ_BACKUP_TOKEN]
    assert len(token) == 32
    assert hass.data[SEQ_BACKUP_AT] == NOW
    assert hub.state.backup_token == token
    assert hub.state.carries_backup_token(token)
    for key in (SEQ_STORE_KEY, BACKUP_KEY):
        assert record_in(hass_storage, key)["in_backup"] == token
        assert record_in(hass_storage, key)["backup_at"] == NOW
        assert record_in(hass_storage, key, "0D07") == {
            "seq": 5,
            "in_backup": token,
            "backup_at": NOW,
        }
        assert record_in(hass_storage, key, "0D09") == "not a record"
    assert hub.state.seq == seq

    await backup.async_post_backup(hass)
    assert SEQ_BACKUP_TOKEN not in hass.data
    assert SEQ_BACKUP_AT not in hass.data
    assert hub.state.backup_token is None
    assert hub.state.backup_at is None
    for key in (SEQ_STORE_KEY, BACKUP_KEY):
        assert "in_backup" not in record_in(hass_storage, key)
        assert "backup_at" not in record_in(hass_storage, key)
        assert record_in(hass_storage, key, "0D07") == {"seq": 5}
    # a normal restart after a finished backup is no restore: the margin only
    await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert hub_of(init_integration).state.seq < seq + SEQ_RESTART_MARGIN + 100


async def test_the_hooks_without_any_mesh_loaded(hass: HomeAssistant) -> None:
    await backup.async_pre_backup(hass)
    assert SEQ_BACKUP_TOKEN in hass.data
    await backup.async_post_backup(hass)
    assert SEQ_BACKUP_TOKEN not in hass.data


async def test_a_write_that_does_not_land_in_time_does_not_hold_the_backup_back(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = hub_of(init_integration)
    release = asyncio.Event()
    save_now = hub.state.async_save_now

    async def stuck() -> None:
        await release.wait()
        await save_now()

    with (
        patch.object(backup, "BACKUP_WRITE_TIMEOUT", 0.01),
        patch.object(hub.state, "async_save_now", stuck),
    ):
        await backup.async_pre_backup(hass)
    assert "address 0D00" in caplog.text
    assert "a restore of this backup can repeat sequence numbers" in caplog.text
    assert "in_backup" not in record_in(hass_storage, SEQ_STORE_KEY)
    release.set()
    await hass.async_block_till_done()
    # the write goes on and lands after all
    assert (
        record_in(hass_storage, SEQ_STORE_KEY)["in_backup"]
        == hass.data[SEQ_BACKUP_TOKEN]
    )
    await backup.async_post_backup(hass)


async def test_a_failing_write_is_logged(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with patch(
        "homeassistant.helpers.storage.Store._async_write_data",
        side_effect=WriteError("No space left on device (injected)"),
    ):
        await backup.async_pre_backup(hass)
    assert "was not written before the backup" in caplog.text
    assert not hub_of(init_integration).state.carries_backup_token(
        hass.data[SEQ_BACKUP_TOKEN]
    )
    await backup.async_post_backup(hass)


async def test_a_superseded_owner_is_left_alone(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Only the mesh's current `HAState` is marked and writes; the one it replaced writes nothing (HAC-02)."""
    old = hub_of(init_integration)
    store = seq_store(hass, old.cdb)
    new_state = HAState(
        store,
        await store.async_load(),
        0x0D00,
        old.cdb.mesh_uuid.lower(),
        seq_backup_store(hass, old.cdb),
    )
    await hass.async_block_till_done()
    assert list(hass.data[SEQ_OWNERS].values()) == [new_state]
    await backup.async_pre_backup(hass)
    token = hass.data[SEQ_BACKUP_TOKEN]
    assert new_state.carries_backup_token(token)
    assert old.state.backup_token is None
    written = dict(hass_storage[SEQ_STORE_KEY])
    await old.state.async_save_now()
    await hass.async_block_till_done()
    assert hass_storage[SEQ_STORE_KEY] == written
    await backup.async_post_backup(hass)
    assert "in_backup" not in record_in(hass_storage, SEQ_STORE_KEY)


async def test_an_owner_without_a_backup_copy(hass: HomeAssistant) -> None:
    store = SeqStore(hass, STORAGE_VERSION, f"{DOMAIN}.seq.test-d5", atomic_writes=True)
    state = HAState(store, None, 0x0D00, "test-d5")
    await backup.async_pre_backup(hass)
    assert state.carries_backup_token(hass.data[SEQ_BACKUP_TOKEN])
    await backup.async_post_backup(hass)
    assert not state.carries_backup_token("x")
    hass.data[SEQ_OWNERS].pop("test-d5")


async def test_a_stopped_hub_keeps_its_record_clean_through_a_backup(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """An entry unloaded before the backup is marked too (it may be started and send before a restore), and its
    record stays `clean`: the hooks do not reopen a closed counter."""
    await stop_entry(hass, init_integration)
    seq = record_in(hass_storage, SEQ_STORE_KEY)["seq"]
    await backup.async_pre_backup(hass)
    record = record_in(hass_storage, SEQ_STORE_KEY)
    assert record["in_backup"] == hass.data[SEQ_BACKUP_TOKEN]
    assert record["clean"] is True
    assert record["seq"] == seq
    await backup.async_post_backup(hass)
    assert record_in(hass_storage, SEQ_STORE_KEY)["clean"] is True


async def test_a_reload_during_a_backup_is_no_restore(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """A successor started between the hooks finds the mark this process set: it does not skip ahead, and it
    writes the mark too (the archive may be taken after its first write); the post-backup hook clears it."""
    hub = hub_of(init_integration)
    seq = hub.state.seq
    # another address's record, as an earlier backup left it: its mark stays through this one
    hub.state._addresses["0D08"] = {"seq": 6, "in_backup": "an older backup's"}
    await backup.async_pre_backup(hass)
    token = hass.data[SEQ_BACKUP_TOKEN]
    hub.state._addresses["0D07"] = {"seq": 5}
    await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    state = hub_of(init_integration).state
    assert state.seq < seq + 100  # closed cleanly, continued exactly: no skip
    assert state.seq_guard is None
    assert state.backup_token == token
    assert record_in(hass_storage, SEQ_STORE_KEY)["in_backup"] == token
    await backup.async_post_backup(hass)
    assert "in_backup" not in record_in(hass_storage, SEQ_STORE_KEY)
    assert record_in(hass_storage, SEQ_STORE_KEY, "0D07") == {"seq": 5}
    # another backup's mark stays: that record came back from it
    assert record_in(hass_storage, SEQ_STORE_KEY, "0D08")["in_backup"] == (
        "an older backup's"
    )


async def test_a_restored_record_skips_ahead_floor_first(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A record whose mark this process did not set (restored, or Home Assistant stopped during a backup) continues
    past the further copy — a day old at 1000 numbers a day: the minimum, SEQ_SKIP_AHEAD — guarded until the first
    beacon; the floor is written before either copy, and both copies before the hub starts."""
    restore_issue = issue_id(init_integration, ISSUE_RESTORE_TOO_OLD)
    await stop_entry(hass, init_integration)
    restored = {
        "seq": 7000,
        "iv_index": 0,
        "iv_update_active": False,
        "iv_known": True,
        "rpl": {"0148": [0, 9]},
        "clean": True,
        "written_at": NOW - DAY,
        "send_rate": A_DAY_OF_1000,
        "in_backup": "from the archive",
        "backup_at": NOW - DAY,
    }
    hass_storage[SEQ_STORE_KEY] = stored(restored)
    hass_storage[BACKUP_KEY] = stored({**restored, "seq": 6900, "clean": False})
    order: list[str] = []
    write = SeqStore._async_write_data

    async def record_order(self: SeqStore, data: dict[str, Any]) -> None:
        order.append(self.key)
        await write(self, data)

    with (
        patch.object(SeqStore, "_async_write_data", record_order),
        patch.object(seq_store_module, "wall_now", return_value=NOW),
    ):
        assert await hass.config_entries.async_setup(init_integration.entry_id)
        await wait_for_link(hass, init_integration)
    assert order[:3] == [FLOOR_KEY, SEQ_STORE_KEY, BACKUP_KEY]
    assert (
        "was restored from a backup (1.0 days old), or Home Assistant stopped during one"
        in caplog.text
    )
    assert record_in(hass_storage, FLOOR_KEY) == {
        "iv_index": 0,
        "seq": 7000 + SEQ_SKIP_AHEAD,
    }
    state = hub_of(init_integration).state
    assert state.seq >= 7000 + SEQ_SKIP_AHEAD + SEQ_RESTART_MARGIN
    assert state.iv_known is True
    assert state.rpl[0x0148][1] >= 9
    # the guard was pending (`SEQ_GUARD_FIRST_BEACON`) until the proxy's beacon named the network's index, 0
    assert state.seq_guard == 1
    for key in (SEQ_STORE_KEY, BACKUP_KEY):
        assert "in_backup" not in record_in(hass_storage, key)
        assert "backup_at" not in record_in(hass_storage, key)
        assert record_in(hass_storage, key)["seq"] >= 7000 + SEQ_SKIP_AHEAD
    assert ir.async_get(hass).async_get_issue(DOMAIN, restore_issue) is None


async def test_a_restored_record_skips_past_the_further_copy(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The backup copy ahead of the store is the one skipped past; a floor already further than that is kept; near
    the end of the space the counter stops at `SEQ_TX_LIMIT` — not quietly (review-5 S5-1): nothing is sent under
    the index, and `restore_too_old` says why, until the counter is below the limit again. A record an older
    version wrote (no write time) skips SEQ_SKIP_UNKNOWN."""
    restore_issue = issue_id(init_integration, ISSUE_RESTORE_TOO_OLD)
    await stop_entry(hass, init_integration)
    restored = {"seq": 100, "iv_index": 0, "clean": False, "in_backup": "x"}
    hass_storage[SEQ_STORE_KEY] = stored(restored)
    hass_storage[BACKUP_KEY] = stored({**restored, "seq": SEQ_TX_LIMIT - 5})
    floor = {"iv_index": 9, "seq": 1}
    hass_storage[FLOOR_KEY] = stored(floor)
    written: list[dict[str, Any]] = []
    write = SeqStore._async_write_data

    async def keep(self: SeqStore, data: dict[str, Any]) -> None:
        if self.key == SEQ_STORE_KEY:
            written.append(data["data"]["addresses"]["0D00"])
        await write(self, data)

    with patch.object(SeqStore, "_async_write_data", keep):
        assert await hass.config_entries.async_setup(init_integration.entry_id)
        await hass.async_block_till_done()
    assert record_in(hass_storage, FLOOR_KEY) == floor
    assert written[0]["seq"] == SEQ_TX_LIMIT
    assert written[0]["seq_guard"] == SEQ_GUARD_FIRST_BEACON
    hub = hub_of(init_integration)
    assert hub.state.seq > SEQ_TX_LIMIT
    issues = ir.async_get(hass)
    issue = issues.async_get_issue(DOMAIN, restore_issue)
    assert issue is not None
    assert issue.is_persistent
    assert issue.learn_more_url == learn_more_url(ISSUE_RESTORE_TOO_OLD)
    assert "of unknown age" in caplog.text
    hub.issues.check_sequence_space()
    assert issues.async_get_issue(DOMAIN, restore_issue) is not None
    # an IV Update moved the counter on (it restarts at 0): the issue goes with the next check (put back at once:
    # nothing may go out with the counter at 0 here)
    held = hub.state.seq
    hub.state.seq = 0
    hub.issues.check_sequence_space()
    hub.state.seq = held
    assert issues.async_get_issue(DOMAIN, restore_issue) is None


async def test_a_restored_record_keeps_a_rewind_guard(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """A record an `iv_index_mismatch` rewind left with a concrete guard (numbers went out under every index up to
    42) keeps it through the restore skip, in the store and the floor, with the index stored as not known: the
    placeholder would let the first beacon set a guard below the indexes those numbers used."""
    await stop_entry(hass, init_integration)
    restored = {
        "seq": 100,
        "iv_index": 0,
        "clean": False,
        "seq_guard": 42,
        "written_at": NOW,
        "in_backup": "x",
    }
    hass_storage[SEQ_STORE_KEY] = stored(restored)
    hass_storage[BACKUP_KEY] = stored(restored)
    hass_storage.pop(FLOOR_KEY, None)
    written: list[dict[str, Any]] = []
    write = SeqStore._async_write_data

    async def keep(self: SeqStore, data: dict[str, Any]) -> None:
        if self.key == SEQ_STORE_KEY:
            written.append(data["data"]["addresses"]["0D00"])
        await write(self, data)

    with (
        patch.object(SeqStore, "_async_write_data", keep),
        patch.object(seq_store_module, "wall_now", return_value=NOW),
    ):
        assert await hass.config_entries.async_setup(init_integration.entry_id)
        await hass.async_block_till_done()
    assert written[0]["seq"] == 100 + SEQ_SKIP_AHEAD
    assert written[0]["seq_guard"] == 42
    assert written[0]["iv_known"] is False
    assert record_in(hass_storage, FLOOR_KEY)["seq_guard"] == 42
    assert hub_of(init_integration).state.seq_guard is not None
    assert hub_of(init_integration).state.seq_guard >= 42


async def test_a_restored_record_whose_floor_cannot_be_written_waits(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Not ready, both copies untouched (still marked): a retry once the disk takes writes again skips ahead."""
    await stop_entry(hass, init_integration)
    restored = {"seq": 7000, "iv_index": 0, "clean": False, "in_backup": "x"}
    hass_storage[SEQ_STORE_KEY] = stored(restored)
    hass_storage[BACKUP_KEY] = stored(restored)
    write = SeqStore._async_write_data

    async def floor_fails(self: SeqStore, data: dict[str, Any]) -> None:
        if self.key == FLOOR_KEY:
            raise WriteError("No space left on device (injected)")
        await write(self, data)

    with patch.object(SeqStore, "_async_write_data", floor_fails):
        assert not await hass.config_entries.async_setup(init_integration.entry_id)
    assert init_integration.state is ConfigEntryState.SETUP_RETRY
    assert "Could not write the sequence-number floor of address 0D00" in caplog.text
    assert record_in(hass_storage, SEQ_STORE_KEY) == restored
    assert FLOOR_KEY not in hass_storage
    await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(init_integration.entry_id)
    await hass.async_block_till_done()
    assert hub_of(init_integration).state.seq >= 7000 + SEQ_SKIP_AHEAD


async def test_the_mark_never_reaches_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
) -> None:
    await backup.async_pre_backup(hass)
    token = hass.data[SEQ_BACKUP_TOKEN]
    state = hub_of(init_integration).state
    assert "in_backup" not in state.to_dict()
    assert "in_backup" not in state.to_stored()
    result = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    dumped = json.dumps(result)
    assert token not in dumped
    assert "in_backup" not in dumped
    await backup.async_post_backup(hass)


def test_without_mark() -> None:
    assert _without_mark({"in_backup": "a"}, None) == {"in_backup": "a"}
    assert _without_mark("garbage", "a") == "garbage"
    assert _without_mark({"in_backup": "b", "seq": 1}, "a") == {
        "in_backup": "b",
        "seq": 1,
    }
    assert _without_mark({"in_backup": "a", "seq": 1}, "a") == {"seq": 1}


async def test_the_hooks_are_quiet_when_nothing_is_wrong(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        await backup.async_pre_backup(hass)
        await backup.async_post_backup(hass)
    assert "backup" not in caplog.text


# ----------------------------------------------------------------------------- review-5 S5-1: the skip by rate and age


async def test_an_old_backup_skips_twice_what_its_address_sends_in_its_age(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A backup 100 days old of an address sending 20 000 numbers a day continues 2 * 20 000 * 100 past the record —
    the fixed 2^20 covered 52 of those days (the energy polls of five sockets): the 4 000 000 sent since are not
    reused. The age runs from the earlier of the write and the backup's start; the higher rate of the copies counts."""
    restore_issue = issue_id(init_integration, ISSUE_RESTORE_TOO_OLD)
    await stop_entry(hass, init_integration)
    restored = {
        "seq": 7000,
        "iv_index": 0,
        "clean": False,
        "written_at": NOW - 99 * DAY,
        "send_rate": {"per_day": 15000.0, "sent": 0, "seconds": 0.0},
        "in_backup": "x",
        "backup_at": NOW - 100 * DAY,
    }
    hass_storage[SEQ_STORE_KEY] = stored(restored)
    hass_storage[BACKUP_KEY] = stored(
        {**restored, "send_rate": {"per_day": 20000.0, "sent": 0, "seconds": 0.0}}
    )
    with patch.object(seq_store_module, "wall_now", return_value=NOW):
        assert await hass.config_entries.async_setup(init_integration.entry_id)
        await hass.async_block_till_done()
    assert "(100.0 days old)" in caplog.text
    assert record_in(hass_storage, FLOOR_KEY)["seq"] == 7000 + 4_000_000
    assert hub_of(init_integration).state.seq >= 7000 + 4_000_000 + SEQ_RESTART_MARGIN
    assert ir.async_get(hass).async_get_issue(DOMAIN, restore_issue) is None


async def test_a_restored_record_waits_for_a_clock_behind_it(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A host without a clock of its own starts before its time is set: the record says it was written a day from
    now, so no age can be measured — not ready, nothing written, and the retry with the clock set skips ahead."""
    await stop_entry(hass, init_integration)
    restored = {
        "seq": 7000,
        "iv_index": 0,
        "clean": False,
        "written_at": NOW + DAY,
        "in_backup": "x",
    }
    hass_storage[SEQ_STORE_KEY] = stored(restored)
    hass_storage[BACKUP_KEY] = stored(restored)
    with patch.object(seq_store_module, "wall_now", return_value=NOW):
        assert not await hass.config_entries.async_setup(init_integration.entry_id)
    assert init_integration.state is ConfigEntryState.SETUP_RETRY
    assert "the clock is behind the time it was written" in caplog.text
    assert record_in(hass_storage, SEQ_STORE_KEY) == restored
    await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    with patch.object(seq_store_module, "wall_now", return_value=NOW + 2 * DAY):
        assert await hass.config_entries.async_setup(init_integration.entry_id)
        await hass.async_block_till_done()
    assert hub_of(init_integration).state.seq >= 7000 + SEQ_SKIP_AHEAD


def test_the_send_rate_is_read_leniently() -> None:
    assert SendRate.from_stored(None).to_stored() == {
        "per_day": None,
        "sent": 0,
        "seconds": 0.0,
    }
    kept = {"per_day": 12.5, "sent": 3, "seconds": 60.0}
    assert SendRate.from_stored(kept).to_stored() == kept
    assert SendRate.from_stored({**kept, "per_day": None}).measured is None
    for broken in (
        "not a rate",
        {"sent": 3},
        {**kept, "sent": -1},
        {**kept, "sent": True},
        {**kept, "per_day": "fast"},
        {**kept, "seconds": float("inf")},
    ):
        assert SendRate.from_stored(broken).to_stored() == {
            "per_day": None,
            "sent": 0,
            "seconds": 0.0,
        }


def test_the_send_rate_counts_running_time_by_the_day() -> None:
    """The first tick of a run starts the clock, a clock set back adds nothing; a day of running time closes the
    window: a busier day replaces the rate at once, a quieter one takes it half-way down."""
    rate = SendRate()
    rate.tick(NOW)
    assert rate.seconds == 0.0
    rate.note_sent(500)
    rate.tick(NOW - 10)  # set back
    assert rate.seconds == 0.0
    rate.tick(NOW + DAY / 2)
    assert rate.seconds == DAY / 2 + 10
    rate.tick(NOW + DAY)
    assert rate.measured == pytest.approx(500 * DAY / (DAY + 10))
    assert (rate.sent, rate.seconds) == (0, 0.0)
    rate.measured = 1000.0
    rate.note_sent(4000)
    rate.tick(NOW + 2 * DAY)
    assert rate.measured == 4000.0
    rate.note_sent(1000)
    rate.tick(NOW + 3 * DAY)
    assert rate.measured == 2500.0


def test_the_send_rate_per_day() -> None:
    """Nothing measured: SEQ_RATE_UNMEASURED, or the burst of the first hour taken as a whole hour's when higher;
    an hour of the window measures; a measured rate counts until the window in progress is higher."""
    assert SendRate().per_day() == SEQ_RATE_UNMEASURED
    assert SendRate(None, 1000, 60.0).per_day() == SEQ_RATE_UNMEASURED
    assert SendRate(None, 10_000, 60.0).per_day() == 10_000 * 24
    assert SendRate(None, 100, 2 * 3600.0).per_day() == 1200
    assert SendRate(5000.0, 100_000, 60.0).per_day() == 5000.0
    assert SendRate(5000.0, 100, 3600.0).per_day() == 5000.0
    assert SendRate(5000.0, 1000, 3600.0).per_day() == 24_000


def test_the_restore_skip() -> None:
    """Twice the higher rate over the age from the earliest time the copies give, at least SEQ_SKIP_AHEAD; records
    without a usable time (an older version's) skip SEQ_SKIP_UNKNOWN; a clock behind by more than the tolerance
    raises `ClockBehind`, by less counts as no age."""
    rate = {"per_day": 1000.0, "sent": 0, "seconds": 0.0}
    assert _restore_skip([None, "garbage"], NOW) == (
        SEQ_SKIP_UNKNOWN,
        None,
        SEQ_RATE_UNMEASURED,
    )
    assert _restore_skip([{"written_at": "x", "send_rate": rate}], NOW) == (
        SEQ_SKIP_UNKNOWN,
        None,
        1000.0,
    )
    assert _restore_skip([{"written_at": NOW - DAY, "send_rate": rate}], NOW) == (
        SEQ_SKIP_AHEAD,
        DAY,
        1000.0,
    )
    old = [
        {"written_at": NOW - 900 * DAY, "send_rate": rate},
        {"written_at": NOW - 800 * DAY, "backup_at": NOW - 1000 * DAY},
    ]
    assert _restore_skip(old, NOW) == (
        2 * SEQ_RATE_UNMEASURED * 1000,
        1000 * DAY,
        SEQ_RATE_UNMEASURED,
    )
    assert _restore_skip([{"written_at": NOW + 60}], NOW)[1] == 0.0
    with pytest.raises(ClockBehind):
        _restore_skip([{"written_at": NOW + 3600}], NOW)


def test_restore_coverage() -> None:
    assert restore_coverage(1000.0, 0) == {
        "numbers_per_day": 1000,
        "minimum_covers_days": round(SEQ_SKIP_AHEAD / 2000, 1),
        "restore_covers_days": round(SEQ_TX_LIMIT / 2000, 1),
    }
    # nothing sent: any age; too little space left for even the minimum: none
    assert restore_coverage(0.0, 0) == {
        "numbers_per_day": 0,
        "minimum_covers_days": None,
        "restore_covers_days": None,
    }
    assert restore_coverage(0.0, SEQ_TX_LIMIT - 5)["restore_covers_days"] == 0.0


async def test_the_send_rates_of_every_address(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Per address of the store, ours live (the diagnostics' `local.send_rates`); a record that is none is left out."""
    state = hub_of(init_integration).state
    state._addresses["0D07"] = {
        "seq": 5,
        "send_rate": {"per_day": 2000.0, "sent": 0, "seconds": 0.0},
    }
    state._addresses["0D09"] = "not a record"
    rates = state.send_rates()
    assert set(rates) == {"0D00", "0D07"}
    assert rates["0D07"] == restore_coverage(2000.0, 5)
    assert rates["0D00"] == restore_coverage(state.send_rate.per_day(), state.seq)
