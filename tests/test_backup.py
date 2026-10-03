"""The backup platform and the restored-record skip (review-4 D5): no restore resumes below numbers already sent."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.junghome_ble import backup
from custom_components.junghome_ble.const import DOMAIN, SEQ_SKIP_AHEAD
from custom_components.junghome_ble.coordinator import (
    SEQ_BACKUP_TOKEN,
    SEQ_OWNERS,
    SEQ_RESTART_MARGIN,
    SEQ_STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    HAState,
    JungHomeHub,
    SeqStore,
    _without_mark,
    seq_backup_store,
    seq_store,
)
from custom_components.junghome_ble.jhmesh.client import (
    SEQ_GUARD_FIRST_BEACON,
    SEQ_TX_LIMIT,
)

from .conftest import wait_for_link
from .helpers import SEQ_STORE_KEY

if TYPE_CHECKING:
    import pytest
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

BACKUP_KEY = f"{SEQ_STORE_KEY}.backup"
FLOOR_KEY = f"{SEQ_STORE_KEY}.floor"


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
    await backup.async_pre_backup(hass)
    token = hass.data[SEQ_BACKUP_TOKEN]
    assert len(token) == 32
    assert hub.state.backup_token == token
    assert hub.state.carries_backup_token(token)
    for key in (SEQ_STORE_KEY, BACKUP_KEY):
        assert record_in(hass_storage, key)["in_backup"] == token
        assert record_in(hass_storage, key, "0D07") == {"seq": 5, "in_backup": token}
        assert record_in(hass_storage, key, "0D09") == "not a record"
    assert hub.state.seq == seq

    await backup.async_post_backup(hass)
    assert SEQ_BACKUP_TOKEN not in hass.data
    assert hub.state.backup_token is None
    for key in (SEQ_STORE_KEY, BACKUP_KEY):
        assert "in_backup" not in record_in(hass_storage, key)
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
    SEQ_SKIP_AHEAD past the further copy, guarded until the first beacon; the floor is written before either
    copy, and both copies before the hub starts."""
    await stop_entry(hass, init_integration)
    restored = {
        "seq": 7000,
        "iv_index": 0,
        "iv_update_active": False,
        "iv_known": True,
        "rpl": {"0148": [0, 9]},
        "clean": True,
        "in_backup": "from the archive",
    }
    hass_storage[SEQ_STORE_KEY] = stored(restored)
    hass_storage[BACKUP_KEY] = stored({**restored, "seq": 6900, "clean": False})
    order: list[str] = []
    write = SeqStore._async_write_data

    async def record_order(self: SeqStore, data: dict[str, Any]) -> None:
        order.append(self.key)
        await write(self, data)

    with patch.object(SeqStore, "_async_write_data", record_order):
        assert await hass.config_entries.async_setup(init_integration.entry_id)
        await wait_for_link(hass, init_integration)
    assert order[:3] == [FLOOR_KEY, SEQ_STORE_KEY, BACKUP_KEY]
    assert (
        "was restored from a backup, or Home Assistant stopped during one"
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
        assert record_in(hass_storage, key)["seq"] >= 7000 + SEQ_SKIP_AHEAD


async def test_a_restored_record_skips_past_the_further_copy(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """The backup copy ahead of the store is the one skipped past; a floor already further than that is kept; near
    the end of the space the counter stops at `SEQ_TX_LIMIT`."""
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
    assert hub_of(init_integration).state.seq >= SEQ_TX_LIMIT


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

    with patch.object(SeqStore, "_async_write_data", keep):
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
