"""The per-mesh sequence-number store: nonce safety across entries, addresses, bursts and the 0.2 store."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.junghome_ble import coordinator, repairs
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_MESH_UUID,
    CONF_METADATA_DIR,
    CONF_UNICAST,
    DOMAIN,
    ISSUE_DUPLICATE_MESH,
    ISSUE_IV_INDEX_AHEAD,
    ISSUE_IV_INDEX_MISMATCH,
    ISSUE_PDUS_DROPPED,
    ISSUE_SEQ_STORE_LOST,
    ISSUE_SEQ_STORE_UNWRITABLE,
    SEQ_SKIP_AHEAD,
    SEQ_SKIP_UNKNOWN,
)
from custom_components.junghome_ble.coordinator import (
    SEQ_RESTART_MARGIN,
    SEQ_SAVE_EVERY,
    SEQ_STALL_RETRY,
    SEQ_STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    HAState,
    JungHomeHub,
    SeqStore,
    merge_legacy_seq_store,
    seq_store,
)
from custom_components.junghome_ble.jhmesh.client import (
    IV_UPDATE_MIN_STATE,
    SEQ_GUARD_FIRST_BEACON,
    SEQ_TX_LIMIT,
    SequenceExhausted,
    SequenceStalled,
)

from .conftest import (
    CDB_PATH,
    META_DIR,
    FakeProxyLink,
    settle,
    setup_entry,
    wait_for_link,
    wait_until,
)
from .helpers import MESH_UUID, SEQ_STORE_KEY, find_issue

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

ENTRY_DATA = {
    CONF_CDB_PATH: CDB_PATH,
    CONF_METADATA_DIR: META_DIR,
    CONF_UNICAST: "0D00",
}


def stored_addresses(hass_storage: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The address records the mesh's store holds right now, without the replay lists (W2's `to_stored`)."""
    return {
        src: {key: value for key, value in record.items() if key != "rpl"}
        for src, record in hass_storage[SEQ_STORE_KEY]["data"]["addresses"].items()
    }


def hub_of(entry: MockConfigEntry) -> JungHomeHub:
    return entry.runtime_data


async def test_removing_and_re_adding_the_entry_keeps_counting(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    hass_storage: dict[str, Any],
) -> None:
    """Remove the integration and add it again for the same mesh and address (the standard troubleshooting step):
    the new entry continues the sequence numbers the old one reached — the store belongs to the mesh, not to the
    entry, and the removal leaves it alone — so no nonce (address, sequence number, IV index) is ever reused."""
    old = hub_of(init_integration)
    seq = old.state.seq
    assert seq > 0
    await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()
    assert stored_addresses(hass_storage)["0D00"] == {
        "seq": seq,
        "iv_index": 0,
        "iv_update_active": False,
        "iv_known": True,  # the proxy's connect-time beacon authenticated
        "clean": True,  # nothing more was sent after the unload: stored as exact
        "seq_peak": 0,
        "seq_peak_from": 0,
    }

    writes = len(fake_link.raw_writes)
    fresh = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data=dict(ENTRY_DATA),
    )
    assert fresh.entry_id != init_integration.entry_id
    await setup_entry(hass, fresh)
    await wait_for_link(hass, fresh)
    state = hub_of(fresh).state
    assert state.src == 0x0D00
    assert state.seq == seq + len(fake_link.raw_writes) - writes  # continued, no gap
    await hass.async_block_till_done()
    assert stored_addresses(hass_storage)["0D00"]["clean"] is False


async def test_legacy_per_entry_store_is_folded_into_the_mesh_store(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    no_connect_beacon: None,
    fast_sleep: list[float],
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 0.2 store (keyed by the entry id, one address) is moved into the mesh's store and removed; the counter
    continues with the restart margin, as the old store never marked a clean close."""
    legacy = f"{DOMAIN}.{mock_config_entry.entry_id}"
    hass_storage[legacy] = {
        "version": 1,
        "minor_version": 1,
        "key": legacy,
        "data": {"src": "0d00", "seq": 3000, "iv_index": 2, "iv_update_active": True},
    }
    # the mesh is where the record says (the hub never goes back to the fake's default IV index 0), and beacons
    # nothing that would end the update
    fake_link.iv_index, fake_link.iv_update = 2, True
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await hass.async_block_till_done()
    assert legacy not in hass_storage
    assert (
        "Moved the sequence-number record of address 0d00 into the mesh's store"
        in caplog.text
    )
    state = hub_of(mock_config_entry).state
    assert (state.iv_index, state.iv_update_active) == (2, True)
    assert state.seq == 3000 + SEQ_RESTART_MARGIN + len(fake_link.raw_writes)
    record = stored_addresses(hass_storage)["0D00"]
    assert (record["iv_index"], record["iv_update_active"], record["clean"]) == (
        2,
        True,
        False,
    )


async def test_legacy_store_of_a_never_loaded_entry_survives_its_removal(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    hass_storage: dict[str, Any],
) -> None:
    """HAC-06: the legacy fold used to run only inside `JungHomeHub.async_create`, which a setup stuck in
    SETUP_RETRY (no proxy seen yet, common right after a restart) never reaches. Removing the entry there used
    to strand its 0.2 store; a re-added entry then started at 0 and reused every nonce the old one had sent."""
    legacy = f"{DOMAIN}.{mock_config_entry.entry_id}"
    hass_storage[legacy] = {
        "version": 1,
        "minor_version": 1,
        "key": legacy,
        "data": {"src": "0D00", "seq": 3000},
    }
    saved_infos = list(mock_bluetooth_env["infos"])
    mock_bluetooth_env[
        "infos"
    ] = []  # no proxy visible: setup cannot get past proxy_in_range
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert (
        legacy not in hass_storage
    )  # migrated despite never reaching JungHomeHub.async_create
    assert stored_addresses(hass_storage)["0D00"]["seq"] == 3000

    assert await hass.config_entries.async_remove(mock_config_entry.entry_id)
    mock_bluetooth_env["infos"] = saved_infos
    fresh = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data=dict(ENTRY_DATA),
    )
    await setup_entry(hass, fresh)
    await wait_for_link(hass, fresh)
    assert hub_of(fresh).state.seq >= 3000 + SEQ_RESTART_MARGIN


async def test_legacy_store_of_an_entry_whose_setup_never_ran_migrates_on_removal(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HAC-06's second path: an entry `async_setup_entry` never even started for (added, then removed straight
    away) still migrates its legacy store on removal — falling back to the CDB file for the mesh UUID, since
    this entry (like every `ENTRY_DATA` in this file) predates `CONF_MESH_UUID`."""
    legacy = f"{DOMAIN}.{mock_config_entry.entry_id}"
    hass_storage[legacy] = {
        "version": 1,
        "minor_version": 1,
        "key": legacy,
        "data": {"src": "0D00", "seq": 3000},
    }
    mock_config_entry.add_to_hass(hass)
    with caplog.at_level(logging.INFO):
        assert await hass.config_entries.async_remove(mock_config_entry.entry_id)
    assert legacy not in hass_storage
    assert stored_addresses(hass_storage)["0D00"]["seq"] == 3000
    assert (
        "Moved the sequence-number record of address 0D00 into the mesh's store"
        in caplog.text
    )


async def test_legacy_store_migrates_on_removal_using_the_stored_mesh_uuid(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """A newer entry already carries `CONF_MESH_UUID`: removal uses it directly, with no need to fall back to
    loading the CDB file."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="entry-with-mesh-uuid",
        data={**ENTRY_DATA, CONF_MESH_UUID: MESH_UUID},
    )
    legacy = f"{DOMAIN}.{entry.entry_id}"
    hass_storage[legacy] = {
        "version": 1,
        "minor_version": 1,
        "key": legacy,
        "data": {"src": "0D00", "seq": 3000},
    }
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_remove(entry.entry_id)
    assert legacy not in hass_storage
    assert stored_addresses(hass_storage)["0D00"]["seq"] == 3000


async def test_legacy_store_is_kept_when_its_mesh_cannot_be_found(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HAC-06's last-resort guard: a legacy record is deleted only after it has been saved into its mesh's
    store — when the mesh cannot even be identified (no `CONF_MESH_UUID`, and the CDB file is gone too), the
    file is kept and a WARNING names it, rather than silently losing the only record of numbers already sent."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="orphaned-cdb-path",
        data={
            CONF_CDB_PATH: "/no/such/export.json",
            CONF_METADATA_DIR: META_DIR,
            CONF_UNICAST: "0D00",
        },
    )
    legacy = f"{DOMAIN}.{entry.entry_id}"
    hass_storage[legacy] = {
        "version": 1,
        "minor_version": 1,
        "key": legacy,
        "data": {"src": "0D00", "seq": 3000},
    }
    entry.add_to_hass(hass)
    with caplog.at_level(logging.WARNING):
        assert await hass.config_entries.async_remove(entry.entry_id)
    assert legacy in hass_storage
    assert (
        f"could not tell which mesh {entry.title}'s sequence-number record"
        in caplog.text
    )


@pytest.mark.parametrize(
    ("existing", "legacy", "expected"),
    [
        (None, {"src": "0D00", "seq": 5}, {"seq": 5}),
        ({}, {"src": "0d00", "seq": 5, "iv_index": 0}, {"seq": 5, "iv_index": 0}),
        (
            {"addresses": {"0D00": {"seq": 900, "iv_index": 0, "clean": True}}},
            {"src": "0D00", "seq": 5, "iv_index": 0},
            {"seq": 900, "iv_index": 0, "clean": True},
        ),
        (
            {"addresses": {"0D00": {"seq": 900, "iv_index": 0, "clean": True}}},
            {"src": "0D00", "seq": 5, "iv_index": 1},
            {"seq": 5, "iv_index": 1},
        ),
        (
            {"addresses": {"0D00": {"seq": 900, "iv_index": 1}}},
            {"src": "0D00", "seq": 901, "iv_index": 1},
            {"seq": 901, "iv_index": 1},
        ),
        (
            # HAC-08: existing has iv_index 5 but is still "in progress" (transmits under 4); legacy is at 5,
            # normal operation. Ranking by the raw iv_index alone (5 >= 5, 100 >= 50) would keep the existing
            # record, which is actually behind by transmit index.
            {
                "addresses": {
                    "0D00": {"seq": 100, "iv_index": 5, "iv_update_active": True}
                }
            },
            {"src": "0D00", "seq": 50, "iv_index": 5, "iv_update_active": False},
            {"seq": 50, "iv_index": 5, "iv_update_active": False},
        ),
    ],
    ids=[
        "no_store_yet",
        "empty_store",
        "existing_record_further_along",
        "legacy_iv_index_ahead",
        "legacy_seq_ahead",
        "legacy_ahead_by_transmit_index_despite_a_lower_iv_index",
    ],
)
def test_merge_legacy_seq_store(
    existing: dict[str, Any] | None,
    legacy: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    """Two records for one address: the one further along by transmit IV index, then sequence number, wins."""
    assert merge_legacy_seq_store(existing, legacy) == {"addresses": {"0D00": expected}}


async def test_an_address_switched_away_and_back_continues_its_counter(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reconfiguring the address to 0D01 starts a fresh sequence space (with the warning: the store cannot know
    whether 0D01 was used from elsewhere); back on 0D00 the old counter continues, and the store keeps both."""
    entry = init_integration
    seq_0d00 = hub_of(entry).state.seq
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_UNICAST: "0D01"}
    )
    await hass.async_block_till_done()  # the update listener reloads the entry
    await wait_for_link(hass, entry)
    state = hub_of(entry).state
    assert state.src == 0x0D01
    assert (
        "Address 0D01 has no sequence-number record in this mesh's store (known: 0D00)"
        in caplog.text
    )
    writes = len(fake_link.raw_writes)
    seq_0d01 = state.seq
    assert (
        0 < seq_0d01 < SEQ_RESTART_MARGIN
    )  # fresh: no margin, just what this link sent

    caplog.clear()
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_UNICAST: "0D00"}
    )
    await hass.async_block_till_done()
    await wait_for_link(hass, entry)
    state = hub_of(entry).state
    assert state.src == 0x0D00
    assert state.seq == seq_0d00 + len(fake_link.raw_writes) - writes
    assert "has no sequence-number record" not in caplog.text
    await hass.async_block_till_done()
    addresses = stored_addresses(hass_storage)
    assert addresses["0D01"] == {
        "seq": seq_0d01,
        "iv_index": 0,
        "iv_update_active": False,
        "iv_known": True,  # the proxy's connect-time beacon authenticated
        "clean": True,
        "seq_peak": 0,
        "seq_peak_from": 0,
    }
    assert addresses["0D00"]["clean"] is False


async def test_a_burst_is_stored_every_save_interval_without_waiting(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """The debounced save is moved back by every change, so a burst could outrun the restart margin; every
    SEQ_SAVE_EVERY numbers a write is started at once instead, and an IV change is written at once too."""
    state = hub_of(init_integration).state
    await hass.async_block_till_done()
    stored = stored_addresses(hass_storage)["0D00"]["seq"]
    assert SEQ_SAVE_EVERY < SEQ_RESTART_MARGIN
    while state.seq < stored + SEQ_SAVE_EVERY - 1:
        state.next_seq()
    await hass.async_block_till_done()
    assert stored_addresses(hass_storage)["0D00"]["seq"] == stored  # debounced only
    state.next_seq()
    await hass.async_block_till_done()  # the immediate write ran as its own task
    assert stored_addresses(hass_storage)["0D00"]["seq"] == stored + SEQ_SAVE_EVERY
    for _ in range(3):
        state.next_seq()
    await hass.async_block_till_done()
    assert stored_addresses(hass_storage)["0D00"]["seq"] == stored + SEQ_SAVE_EVERY
    freezer.tick(3)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert stored_addresses(hass_storage)["0D00"]["seq"] == stored + SEQ_SAVE_EVERY + 3

    peak = state.seq
    # transmit index unchanged: not urgent
    assert state.apply_beacon(1, iv_update=True, now=1000.0)
    await hass.async_block_till_done()
    assert stored_addresses(hass_storage)["0D00"]["iv_index"] == 0
    # the sequence space restarts (the spec's 96 hours later): written now
    assert state.apply_beacon(1, iv_update=False, now=1000.0 + IV_UPDATE_MIN_STATE)
    await hass.async_block_till_done()
    assert stored_addresses(hass_storage)["0D00"] == {
        "seq": 0,
        "iv_index": 1,
        "iv_update_active": False,
        "iv_known": True,
        "clean": False,
        "seq_peak": peak,
        "seq_peak_from": 0,
        "iv_changed_at": 1000.0 + IV_UPDATE_MIN_STATE,
    }


async def test_seq_store_hands_out_one_object_per_mesh(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """`seq_store` caches its `Store` by mesh UUID (`SEQ_STORES`) instead of a fresh object per call.

    Two live hubs of the same mesh — the config flow refuses a second entry for an already-configured mesh
    (`_mesh_uuid_taken`, `config_flow.py`), but a store edited by hand, or an installation upgraded from before
    that check, could still have two — would otherwise hold two independent `Store` objects for the identical
    `.storage` key, each only ever aware of its own scheduled write (`Store._data`, a single slot *per object*):
    whichever's write happened to reach disk last would win, with no relation to which one was more recent.
    Sharing the object at least makes the two take turns through the same slot instead of racing two files;
    `HAState._addresses` still only reflects what the store held when that particular hub was built (a second
    entry for one mesh is refused for exactly this reason — see `_mesh_uuid_taken`'s docstring)."""
    hub = hub_of(init_integration)
    same = seq_store(hass, hub.cdb)
    assert same is hub.state._store  # same mesh: the identical object, not a fresh one
    assert seq_store(hass, hub.cdb) is same  # calling it again does not build a new one

    other_object = replace(hub.cdb)  # a distinct CDB object, same mesh UUID string
    assert other_object is not hub.cdb
    assert seq_store(hass, other_object) is same  # keyed on the mesh UUID, not identity

    different_mesh = replace(hub.cdb, mesh_uuid=f"{hub.cdb.mesh_uuid[:-1]}0")
    assert seq_store(hass, different_mesh) is not same


async def test_seq_store_writes_atomically(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """HAC-01: the default writer has no fsync, so a torn write on power loss (constant here: every
    SEQ_SAVE_EVERY numbers) can leave a zero-length or truncated file — the CLI's own state file already
    guards against exactly this with fsync (`LocalState._write`'s docstring); the HA-side store needs it too."""
    hub = hub_of(init_integration)
    assert seq_store(hass, hub.cdb)._atomic_writes is True


async def test_lost_store_falls_back_to_the_backup(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HAC-01: when the primary is unreadable (HA's storage layer already renamed a corrupt file aside and
    `async_load` returns None), the mesh's `.backup` copy recovers the numbers already sent instead of every
    address silently starting over at 0 and reusing nonces."""
    hub = hub_of(init_integration)
    for _ in range(200):
        hub.state.next_seq()
    await hass.async_block_till_done()
    seq = hub.state.seq
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    del hass_storage[
        SEQ_STORE_KEY
    ]  # what HA's corrupt-file handling amounts to: async_load returns None
    assert await hass.config_entries.async_setup(init_integration.entry_id)
    await wait_for_link(hass, init_integration)
    assert hub_of(init_integration).state.seq >= seq
    assert (
        "Sequence-number record of address 0D00 unusable in the store, continuing from the backup copy"
        in caplog.text
    )


async def run_fix_flow(
    hass: HomeAssistant, key: str, issue: ir.IssueEntry | None = None
) -> dict[str, Any]:
    """Open the repair issue `key` of the entry (or `issue`, one already gone) and confirm its fix flow."""
    issue = issue or find_issue(hass, key)
    assert issue is not None
    assert issue.is_fixable
    flow = await repairs.async_create_fix_flow(hass, issue.issue_id, issue.data)
    flow.hass, flow.issue_id, flow.data = hass, issue.issue_id, issue.data
    form = await flow.async_step_init()
    assert form["type"] == "form"
    assert form["step_id"] == "confirm"
    return dict(await flow.async_step_confirm({}))


async def test_both_copies_lost_with_a_corrupt_file_refuses_to_start_at_0(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review-3 S1 (was HAC-01's logged error): neither copy has anything, but a `<store>.corrupt.<timestamp>` file
    (HA's own renamed-aside copy of a torn write) proves numbers were sent. Starting at 0 reused nonces; now the
    setup stops with a fixable issue, and its fix continues SEQ_SKIP_UNKNOWN from 0 (nothing is left to read)."""
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    del hass_storage[SEQ_STORE_KEY]
    del hass_storage[f"{SEQ_STORE_KEY}.backup"]
    with patch(
        "custom_components.junghome_ble.coordinator._newest_corrupt_seq_store",
        return_value=f"{SEQ_STORE_KEY}.corrupt.2026-01-01T00:00:00",
    ):
        assert not await hass.config_entries.async_setup(init_integration.entry_id)
        assert init_integration.state is ConfigEntryState.SETUP_ERROR
        assert (
            f"the corrupt store was saved as {SEQ_STORE_KEY}.corrupt.2026-01-01T00:00:00"
            in caplog.text
        )
        assert (await run_fix_flow(hass, ISSUE_SEQ_STORE_LOST))[
            "type"
        ] == "create_entry"
        await hass.async_block_till_done()
        await wait_for_link(hass, init_integration)
    assert init_integration.state is ConfigEntryState.LOADED
    assert hub_of(init_integration).state.seq >= SEQ_SKIP_UNKNOWN + SEQ_RESTART_MARGIN
    assert find_issue(hass, ISSUE_SEQ_STORE_LOST) is None
    assert (
        hass_storage[f"{SEQ_STORE_KEY}.backup"]["data"]["addresses"]["0D00"]["seq"]
        >= SEQ_SKIP_UNKNOWN
    )


async def test_a_record_that_parses_as_json_but_not_as_a_counter_uses_the_backup(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review-3 S1: `async_load` only fails on a JSON error; a record it returns that `LocalState` cannot resume
    from started the address at 0, and its first immediate save overwrote the good backup with that."""
    hub = hub_of(init_integration)
    for _ in range(300):
        hub.state.next_seq()
    await hass.async_block_till_done()
    seq = hub.state.seq
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    backup_seq = hass_storage[f"{SEQ_STORE_KEY}.backup"]["data"]["addresses"]["0D00"][
        "seq"
    ]
    assert backup_seq > 0
    hass_storage[SEQ_STORE_KEY]["data"]["addresses"]["0D00"]["seq"] = "garbage"
    assert await hass.config_entries.async_setup(init_integration.entry_id)
    await wait_for_link(hass, init_integration)
    assert hub_of(init_integration).state.seq >= backup_seq + SEQ_RESTART_MARGIN
    assert hub_of(init_integration).state.seq >= seq
    assert "unusable in the store, continuing from the backup copy" in caplog.text


async def test_a_record_unusable_in_both_copies_is_skipped_past_the_best_number_left(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Both records exist but neither resumes: the fix continues SEQ_SKIP_AHEAD past the highest number that still
    reads as one (here the backup's, the primary's being out of range)."""
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    primary = hass_storage[SEQ_STORE_KEY]["data"]["addresses"]["0D00"]
    backup = hass_storage[f"{SEQ_STORE_KEY}.backup"]["data"]["addresses"]["0D00"]
    primary["src_garbage"] = True
    primary["rpl"] = {"zz": [0, 0]}  # not a source address: the record does not parse
    primary["seq"] = 7000
    backup["seq"] = 9000
    backup["rpl"] = "not a map"  # not usable either, but its counter still reads
    assert not await hass.config_entries.async_setup(init_integration.entry_id)
    assert (await run_fix_flow(hass, ISSUE_SEQ_STORE_LOST))["type"] == "create_entry"
    await hass.async_block_till_done()
    await wait_for_link(hass, init_integration)
    assert hub_of(init_integration).state.seq >= 9000 + SEQ_SKIP_AHEAD


async def test_a_second_loss_of_both_copies_continues_past_the_first_repair(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Both copies lost twice: the second repair has nothing to read either, and used to continue SEQ_SKIP_UNKNOWN
    from 0 again — over every number sent since the first. The first repair's floor file carries it past them;
    it is history too, so both copies then deleted without a trace do not start the address at 0."""
    for _ in range(2):
        assert await hass.config_entries.async_unload(init_integration.entry_id)
        await hass.async_block_till_done()
        for key in (SEQ_STORE_KEY, f"{SEQ_STORE_KEY}.backup"):
            hass_storage[key]["data"]["addresses"]["0D00"]["seq"] = None
        assert not await hass.config_entries.async_setup(init_integration.entry_id)
        assert (await run_fix_flow(hass, ISSUE_SEQ_STORE_LOST))[
            "type"
        ] == "create_entry"
        await hass.async_block_till_done()
        await wait_for_link(hass, init_integration)
    assert hub_of(init_integration).state.seq >= 2 * SEQ_SKIP_UNKNOWN
    assert hass_storage[f"{SEQ_STORE_KEY}.floor"]["data"]["addresses"]["0D00"] == {
        "iv_index": 0,
        "seq": 2 * SEQ_SKIP_UNKNOWN,
    }
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    del hass_storage[SEQ_STORE_KEY]
    del hass_storage[f"{SEQ_STORE_KEY}.backup"]
    assert not await hass.config_entries.async_setup(init_integration.entry_id)
    assert find_issue(hass, ISSUE_SEQ_STORE_LOST) is not None


async def test_seq_store_lost_fix_aborts_without_its_entry(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    hass_storage[SEQ_STORE_KEY]["data"]["addresses"]["0D00"]["seq"] = None
    hass_storage[f"{SEQ_STORE_KEY}.backup"]["data"]["addresses"]["0D00"]["seq"] = None
    assert not await hass.config_entries.async_setup(init_integration.entry_id)
    issue = find_issue(hass, ISSUE_SEQ_STORE_LOST)
    assert issue is not None
    await hass.config_entries.async_remove(init_integration.entry_id)
    assert find_issue(hass, ISSUE_SEQ_STORE_LOST) is None  # removed with the entry ...
    result = await run_fix_flow(
        hass, ISSUE_SEQ_STORE_LOST, issue
    )  # ... a form still open
    assert result["type"] == "abort"
    assert result["reason"] == "entry_gone"


async def test_pdus_dropped_fix_skips_ahead_and_reconnects(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Review-3 S1: a store restored from an older backup regresses silently; the nodes then drop us as replays
    (`pdus_dropped`). The issue's fix moves the counter SEQ_SKIP_AHEAD on and renews the link."""
    hub = hub_of(init_integration)
    seq = hub.state.seq
    hub._report_pdus_dropped(True)
    assert (await run_fix_flow(hass, ISSUE_PDUS_DROPPED))["type"] == "create_entry"
    await wait_until(hass, lambda: fake_link.connect_count == 2, what="a new link")
    await wait_for_link(hass, init_integration)
    assert hub.state.seq >= seq + SEQ_SKIP_AHEAD
    assert fake_link.connect_count == 2
    hub.state.skip_ahead(SEQ_TX_LIMIT)
    assert hub.state.seq == SEQ_TX_LIMIT  # never past the end of the space


async def test_pdus_dropped_fix_aborts_without_a_loaded_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    hub_of(init_integration)._report_pdus_dropped(True)
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    init_integration.runtime_data = None  # type: ignore[assignment]
    hub = find_issue(hass, ISSUE_PDUS_DROPPED)
    assert hub is None  # an unloaded mesh shows no live problems ...
    ir.async_create_issue(  # ... but a stale one may still be open in the UI
        hass,
        DOMAIN,
        f"{ISSUE_PDUS_DROPPED}_{init_integration.entry_id}",
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_PDUS_DROPPED,
        data={"entry_id": init_integration.entry_id},
    )
    result = await run_fix_flow(hass, ISSUE_PDUS_DROPPED)
    assert result["type"] == "abort"


async def test_unload_stores_the_exact_counter_as_closed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """The unload writes the counter after the link is gone, marked clean; a hub stopped twice stays consistent."""
    entry = init_integration
    hub = hub_of(entry)
    for _ in range(5):
        hub.state.next_seq()
    seq = hub.state.seq
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert stored_addresses(hass_storage)["0D00"] == {
        "seq": seq,
        "iv_index": 0,
        "iv_update_active": False,
        "iv_known": True,  # the proxy's connect-time beacon authenticated
        "clean": True,
        "seq_peak": 0,
        "seq_peak_from": 0,
    }
    await hub.async_stop()
    assert stored_addresses(hass_storage)["0D00"]["seq"] == seq


async def test_a_superseded_hub_does_not_overwrite_the_store(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """HAC-02: `ConfigEntry._async_process_on_unload` waits at most 10 s for an on-unload job (`hub.async_stop`)
    before letting a reload go on to `async_setup_entry` anyway, so a slow BLE disconnect can still be running
    when a successor `HAState` starts counting. The old one's `async_close` must not then write its own, lower,
    counter with `clean: True` over what the successor has already sent.

    `new_state`'s numbers are debounced (`SEQ_SAVE_EVERY`, below), so `SEQ_SAVE_EVERY` of them are sent to force
    the immediate write that actually lands within the test — a debounced one needs 2 real seconds to fire,
    which nothing here advances.
    """
    old = hub_of(init_integration)
    store = seq_store(hass, old.cdb)
    new_state = HAState(
        store, await store.async_load(), 0x0D00, old.cdb.mesh_uuid.lower()
    )
    for _ in range(SEQ_SAVE_EVERY):
        new_state.next_seq()
    await hass.async_block_till_done()
    new_seq = new_state.seq
    assert (
        stored_addresses(hass_storage)["0D00"]["seq"] == new_seq
    )  # sanity: the new hub's write landed

    # a still-running old hub must not send at all: it can no longer persist what it hands out, and its `_limit`
    # reads the shared store's `written`, which the successor keeps advancing — it would otherwise be allowed
    # into the very numbers the successor is sending with
    old_seq = old.state.seq
    with pytest.raises(SequenceExhausted):
        old.state.next_seq()
    assert old.state.seq == old_seq
    # force persist()'s immediate-write branch: it must be a no-op too
    old.state._saved = None
    old.state.persist()
    await hass.async_block_till_done()
    assert stored_addresses(hass_storage)["0D00"]["seq"] == new_seq

    await old.state.async_close()
    await hass.async_block_till_done()
    assert stored_addresses(hass_storage)["0D00"]["seq"] == new_seq
    assert stored_addresses(hass_storage)["0D00"]["clean"] is False


async def test_a_second_entry_for_a_running_mesh_is_refused(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C6: the config flow refuses a second entry for an already-configured mesh (`_mesh_uuid_taken`), but an
    entry from before that check, or one edited by hand, can still cover the mesh of another. Its hub used to
    take the mesh's counters over and mute the running one; now it is not started, and raises
    `ISSUE_DUPLICATE_MESH` naming both. The issue is the refused entry's: its start once the other is gone, or its
    removal, ends it."""
    entry = init_integration
    hub = hub_of(entry)
    sibling = MockConfigEntry(
        domain=DOMAIN,
        title="Sibling mesh entry",
        unique_id="deadbeefcafe",
        # no mesh UUID recorded: the running hub is found all the same
        data={**ENTRY_DATA, CONF_UNICAST: "0D02"},
    )
    sibling.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(sibling.entry_id)
    await hass.async_block_till_done()
    assert sibling.state is ConfigEntryState.SETUP_ERROR
    assert sibling.reason == (
        f"{entry.title} already runs this JUNG HOME mesh; only one entry per mesh can be loaded. "
        "Remove one of the two entries"  # HA drops the final full stop
    )
    issue = find_issue(hass, ISSUE_DUPLICATE_MESH)
    assert issue is not None
    assert issue.issue_id == f"{ISSUE_DUPLICATE_MESH}_{sibling.entry_id}"
    assert issue.translation_placeholders == {
        "title": sibling.title,
        "others": entry.title,
    }
    assert f"{entry.title} already runs the JUNG HOME mesh" in caplog.text
    hub.state.next_seq()  # the running hub still owns the counters

    # the first one unloaded: the second starts, and its issue goes
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_reload(sibling.entry_id)
    await wait_for_link(hass, sibling)
    assert find_issue(hass, ISSUE_DUPLICATE_MESH) is None
    # ... and now the first one is the one refused; removing it removes its issue too
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    issue = find_issue(hass, ISSUE_DUPLICATE_MESH)
    assert issue is not None
    assert issue.issue_id == f"{ISSUE_DUPLICATE_MESH}_{entry.entry_id}"
    assert await hass.config_entries.async_remove(entry.entry_id)
    assert find_issue(hass, ISSUE_DUPLICATE_MESH) is None
    hub_of(sibling).state.next_seq()


async def test_a_legacy_record_that_cannot_be_saved_is_kept(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C1: `Store.async_save` only logs a failed write, so the 0.2 record used to be deleted whether or not the
    mesh's store got it — and the address then started over at 0, reusing every nonce it had sent. Kept until
    a save lands; the setup is not ready until then (it would start at 0 all the same)."""
    legacy = f"{DOMAIN}.{mock_config_entry.entry_id}"
    hass_storage[legacy] = {
        "version": 1,
        "minor_version": 1,
        "key": legacy,
        "data": {"src": "0D00", "seq": 3000},
    }
    mock_config_entry.add_to_hass(hass)
    with patch(
        "homeassistant.helpers.storage.Store._async_write_data",
        side_effect=WriteError("read-only file system"),
    ):
        assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert legacy in hass_storage
    assert SEQ_STORE_KEY not in hass_storage
    assert (
        "Could not save the sequence-number record of address 0D00 into the mesh's store"
        in caplog.text
    )

    # writable again: the retry moves the record and continues past it
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await wait_for_link(hass, mock_config_entry)
    assert legacy not in hass_storage
    assert hub_of(mock_config_entry).state.seq >= 3000 + SEQ_RESTART_MARGIN


async def test_sends_stop_when_the_store_cannot_be_written(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """HAC-05: a write that fails silently (a full disk, a filesystem remounted read-only) must not let sends
    keep running ahead of what a restart could actually recover — `reserve_seq` holds them back instead, well
    before the gap could reach `SEQ_RESTART_MARGIN`."""
    state = hub_of(init_integration).state
    await hass.async_block_till_done()
    stored = stored_addresses(hass_storage)["0D00"]["seq"]
    n = 0
    with patch(
        "homeassistant.helpers.storage.Store._async_write_data",
        side_effect=WriteError("disk full"),
    ):
        try:
            while True:
                state.next_seq()
                n += 1
                await asyncio.sleep(0)
        except SequenceExhausted:
            pass
    assert n >= 1
    assert state.seq <= stored + SEQ_RESTART_MARGIN
    # still within the retry window: refused again without forcing another save
    with pytest.raises(SequenceExhausted):
        state.next_seq()
    # the patch is gone, but the refusal is still throttled to one retry every SEQ_STALL_RETRY seconds
    state._stalled_at -= SEQ_STALL_RETRY
    with pytest.raises(SequenceExhausted):
        state.next_seq()  # this call forces the immediate save that lets the next one through
    await hass.async_block_till_done()
    state.next_seq()  # no longer refused


async def test_reserve_seq_refuses_without_a_durable_record(
    hass: HomeAssistant,
) -> None:
    """HAC-05's `_limit()`: nothing durable yet for our address (a fresh store that has not saved anything, or
    one whose last durable write does not cover us) makes a restart start at 0, so nothing may be reserved."""
    store = SeqStore(
        hass, STORAGE_VERSION, f"{DOMAIN}.seq.test-hac05", atomic_writes=True
    )
    state = HAState(store, None, 0x0D00, "test-hac05")
    await hass.async_block_till_done()
    assert store.written is not None
    store.written = None  # nothing durable covers this address (yet, or a store edited/reset out from under it)
    with pytest.raises(SequenceExhausted):
        state.next_seq()


async def test_a_backup_on_another_transmit_index_bounds_nothing(
    hass: HomeAssistant,
) -> None:
    """`_limit()`: a backup copy on another transmit index (its forced write at an IV change failed) is no bound at
    all — a restore from it would go back to that index — so it holds sends back like a store that wrote nothing.
    Covered here on purpose: the property machines reach it only now and then, as IV changes need time to pass."""
    store = SeqStore(
        hass, STORAGE_VERSION, f"{DOMAIN}.seq.test-limit", atomic_writes=True
    )
    backup = SeqStore(
        hass, STORAGE_VERSION, f"{DOMAIN}.seq.test-limit.backup", atomic_writes=True
    )
    state = HAState(store, None, 0x0D00, "test-limit", backup)
    await hass.async_block_till_done()
    assert state._limit() == (0, SEQ_RESTART_MARGIN)
    backup.written = {"addresses": {"0D00": {"seq": 0, "iv_index": 5}}}
    assert state._limit() == (0, 0)


async def test_reserve_seq_refuses_while_the_backup_is_on_another_transmit_index(
    hass: HomeAssistant,
) -> None:
    """`_limit()`: a `.backup` copy whose durable record transmits under another IV index than the store's is no
    bound a restore from it could keep to, so nothing is reserved until the copies agree again. (Until review 4
    only some random examples of `test_properties_seq_store` reached this, and the coverage gate flickered.)"""
    key = f"{DOMAIN}.seq.test-backup-index"
    store = SeqStore(hass, STORAGE_VERSION, key, atomic_writes=True)
    backup = SeqStore(hass, STORAGE_VERSION, f"{key}.backup", atomic_writes=True)
    state = HAState(store, None, 0x0D00, "test-backup-index", backup)
    await hass.async_block_till_done()
    assert store.written is not None
    assert backup.written is not None
    state.next_seq()  # the copies agree: numbers go out
    record = backup.written["addresses"]["0D00"]
    backup.written = {
        "addresses": {"0D00": {**record, "iv_index": record["iv_index"] + 1}}
    }
    assert state._limit() == (state.tx_iv_index, 0)
    with pytest.raises(SequenceExhausted):
        state.next_seq()


READ_ONLY = "OSError: [Errno 30] Read-only file system"


def run_into_the_stall(state: HAState) -> None:
    """Reserve until the store holds sends back (its writes fail)."""
    for _ in range(SEQ_RESTART_MARGIN + SEQ_SAVE_EVERY + 1):
        try:
            state.next_seq()
        except SequenceStalled:
            return
    pytest.fail("the store never held sends back")


async def test_a_store_that_cannot_be_written_raises_its_own_repair(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review-4 D9 (R4-2): review 3 listed "repair when the seq store refuses for minutes" as done, but nothing raised
    one: the user got `pdus_dropped` once the next link's filter request went unanswered — a request the store had
    held back, never sent — and its skip-ahead could not be written either. Now a minute of refusals raises
    `seq_store_unwritable`, naming the file and the error; the filter watchdog stays quiet about a request that
    never went out; and the first number handed out once a write lands clears the repair."""
    hub = hub_of(init_integration)
    state = hub.state
    await hass.async_block_till_done()
    with patch(
        "homeassistant.helpers.storage.Store._async_write_data",
        side_effect=WriteError(READ_ONLY),
    ):
        run_into_the_stall(state)
        await hass.async_block_till_done()
        assert state.last_write_error == READ_ONLY
        # the link drops and comes back while the store refuses: the new link's filter request is held back
        fake_link.drop_link()
        await settle(hass)
        assert hub.connected
        assert hub.proxy.filter_writes == 0
        freezer.tick(coordinator.SEQ_STALL_ISSUE_AFTER)
        async_fire_time_changed(hass)
        await settle(hass)
        issue = find_issue(hass, ISSUE_SEQ_STORE_UNWRITABLE)
        assert issue is not None
        assert issue.severity is ir.IssueSeverity.ERROR
        assert not issue.is_fixable
        assert issue.translation_placeholders == {
            "title": init_integration.title,
            "path": state._store.path,
            "error": READ_ONLY,
        }
        assert find_issue(hass, ISSUE_PDUS_DROPPED) is None
        assert "has not been written for 60 s" in caplog.text
        stalled_for = state.stalled_for
        assert stalled_for is not None
        assert stalled_for >= coordinator.SEQ_STALL_ISSUE_AFTER
        assert state.durable_headroom == 0

    # writable again: the next refusal's forced save lands, and the number after it clears the repair
    freezer.tick(SEQ_STALL_RETRY)
    with pytest.raises(SequenceStalled):
        state.next_seq()
    await hass.async_block_till_done()
    state.next_seq()
    assert find_issue(hass, ISSUE_SEQ_STORE_UNWRITABLE) is None
    assert state.stalled_for is None
    assert state.last_write_error is None
    assert "Sequence-number store written again after" in caplog.text


async def test_a_short_stall_raises_no_repair(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
) -> None:
    """The repair is for a store that stays unwritable: a stall that ended — by a number handed out, or by a write
    that landed with nothing sent since — raises nothing, and a new stall gets a full minute of its own."""
    hub = hub_of(init_integration)
    state = hub.state
    await hass.async_block_till_done()
    failing = patch(
        "homeassistant.helpers.storage.Store._async_write_data",
        side_effect=WriteError(READ_ONLY),
    )
    with failing:
        run_into_the_stall(state)
    freezer.tick(SEQ_STALL_RETRY)
    with pytest.raises(SequenceStalled):
        state.next_seq()  # forces the save that lands
    await hass.async_block_till_done()
    state.next_seq()  # the first stall is over
    with failing:
        run_into_the_stall(
            state
        )  # a second one, SEQ_STALL_RETRY into the first one's minute
        await hass.async_block_till_done()
    # the store catches up by itself, and nothing is sent until the second stall's minute is up
    state._saved = None
    state.persist()
    await hass.async_block_till_done()
    for _ in range(2):  # the first stall's minute, then the second's
        freezer.tick(coordinator.SEQ_STALL_ISSUE_AFTER - SEQ_STALL_RETRY)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert find_issue(hass, ISSUE_SEQ_STORE_UNWRITABLE) is None
    assert state.stalled_for is None


async def test_the_unwritable_repair_names_the_copy_that_failed(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """`HAState.report_unwritable` without a hub: the `.backup` copy's failure is named when it is the one that
    failed; with no entry to raise the repair for (or none known) it is only logged; nothing at all while sends go
    through, and nothing from a superseded `HAState` (HAC-02)."""
    entry = MockConfigEntry(domain=DOMAIN, title="mesh", data=ENTRY_DATA)
    entry.add_to_hass(hass)
    key = f"{DOMAIN}.seq.test-unwritable"
    store = SeqStore(hass, STORAGE_VERSION, key, atomic_writes=True)
    backup = SeqStore(hass, STORAGE_VERSION, f"{key}.backup", atomic_writes=True)
    state = HAState(store, None, 0x0D00, "test-unwritable", backup, entry.entry_id)
    await hass.async_block_till_done()
    state.report_unwritable()  # not stalled
    assert state.last_write_error is None
    store.written = None  # nothing durable: held back
    with pytest.raises(SequenceStalled):
        state.next_seq()
    # the saves that refusal forced land at once here (the storage is in memory): undo them
    store.written = None
    backup.write_error = "disk full"
    assert state._stall_cause() == (backup.path, "disk full")
    state.report_unwritable()
    issue = find_issue(hass, ISSUE_SEQ_STORE_UNWRITABLE)
    assert issue is not None
    assert issue.translation_placeholders == {
        "title": "mesh",
        "path": backup.path,
        "error": "disk full",
    }
    ir.async_delete_issue(hass, DOMAIN, issue.issue_id)
    backup.write_error = None
    for entry_id in (None, "no-such-entry"):
        state.entry_id = entry_id
        caplog.clear()
        state.report_unwritable()
        assert "(no error reported)" in caplog.text
        assert find_issue(hass, ISSUE_SEQ_STORE_UNWRITABLE) is None
    state.entry_id = entry.entry_id
    state.report_unwritable()
    issue = find_issue(hass, ISSUE_SEQ_STORE_UNWRITABLE)
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["error"] == "none reported"
    successor = HAState(store, None, 0x0D00, "test-unwritable", backup, entry.entry_id)
    ir.async_delete_issue(hass, DOMAIN, issue.issue_id)
    state.report_unwritable()  # superseded: its successor has the store
    assert find_issue(hass, ISSUE_SEQ_STORE_UNWRITABLE) is None
    await hass.async_block_till_done()
    assert successor.stalled_for is None


async def test_a_store_that_caught_up_ends_the_stall_without_a_send(
    hass: HomeAssistant,
) -> None:
    """A minute into a stall the store holds nothing back any more (the forced save landed) although nothing was
    sent since: the stall is over and no repair is raised."""
    store = SeqStore(
        hass, STORAGE_VERSION, f"{DOMAIN}.seq.test-caught-up", atomic_writes=True
    )
    state = HAState(store, None, 0x0D00, "test-caught-up")
    await hass.async_block_till_done()
    store.written = None
    with pytest.raises(SequenceStalled):
        state.next_seq()  # forces the save
    await hass.async_block_till_done()
    assert state.stalled_for is not None
    assert state.last_write_error is None
    state.report_unwritable()
    assert state.stalled_for is None
    assert find_issue(hass, ISSUE_SEQ_STORE_UNWRITABLE) is None


async def test_no_filter_request_written_is_no_dropped_pdus(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The Filter Status watchdog raises `pdus_dropped` only for a request that went out on this link while the
    store lets sends through: one never written (every attempt held back), or a store holding sends back right now,
    says nothing about the proxy discarding anything."""
    hub = hub_of(init_integration)
    hub.proxy.proxy_addr = None  # the status "never came"
    hub._beacon_authenticated = True
    hub.proxy.filter_writes = 0
    hub._filter_status_overdue(dt_util.utcnow())
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None
    hub.proxy.filter_writes = 1
    hub.state._stalled_since = 0.0
    hub._filter_status_overdue(dt_util.utcnow())
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None
    hub.state._stalled_since = None
    hub._filter_status_overdue(dt_util.utcnow())
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is not None


async def test_a_send_without_a_link_leaves_the_counter_and_its_clean_mark(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Review-4 R4-4, the reviewer's repro: ten sends with no link used ten numbers, and one after `async_stop` (the
    property reader, a keep-alive still running) cleared the clean-close mark the stop had just written — the next
    start then added the restart margin for nothing. Refused before a number is taken, they change neither."""
    hub = hub_of(init_integration)
    state = hub.state
    await hub.async_stop()
    await hass.async_block_till_done()
    seq = state.seq
    assert state._closed
    assert stored_addresses(hass_storage)["0D00"]["clean"] is True
    for _ in range(10):
        with pytest.raises(ConnectionError, match="not connected to a proxy"):
            await hub.proxy.send_access(0x0300, bytes.fromhex("8201"))
    await hass.async_block_till_done()
    assert state.seq == seq
    assert state._closed
    assert stored_addresses(hass_storage)["0D00"]["clean"] is True


def test_store_record_helpers_assume_the_worst_of_garbage() -> None:
    """A store that is not one says nothing about which address sent: it counts as history; numbers that do not
    read as numbers are passed over when looking for the furthest counter left."""
    assert coordinator._has_history(None, "0D00") is False
    assert coordinator._has_history({"addresses": {}}, "0D00") is False
    assert coordinator._has_history({"addresses": {"0D00": {}}}, "0D00") is True
    assert coordinator._has_history(["not", "a", "store"], "0D00") is True
    assert coordinator._has_history({"no addresses": 1}, "0D00") is True
    assert coordinator._usable_record({"addresses": {"0D00": "x"}}, "0D00") is None
    assert coordinator._seq_skip_target(["x", None, {"seq": "y"}]) == (
        0,
        SEQ_SKIP_UNKNOWN,
    )
    assert coordinator._seq_skip_target([{"seq": 5, "iv_index": 3}]) == (
        3,
        5 + SEQ_SKIP_AHEAD,
    )
    assert coordinator._seq_skip_target([{"seq": SEQ_TX_LIMIT}]) == (0, SEQ_TX_LIMIT)
    assert coordinator._seq_skip_target([{"seq": 9}, {"seq": 5}]) == (
        0,
        9 + SEQ_SKIP_AHEAD,
    )
    # an earlier repair's floor: SEQ_SKIP_UNKNOWN past it when nothing further along reads; a record past it wins
    floor = {"seq": SEQ_SKIP_UNKNOWN, "iv_index": 2}
    assert coordinator._seq_skip_target(["x"], floor) == (2, 2 * SEQ_SKIP_UNKNOWN)
    assert coordinator._seq_skip_target([{"seq": 9, "iv_index": 2}], floor) == (
        2,
        2 * SEQ_SKIP_UNKNOWN,
    )
    assert coordinator._seq_skip_target([{"seq": 9, "iv_index": 3}], floor) == (
        3,
        9 + SEQ_SKIP_AHEAD,
    )
    assert coordinator._seq_skip_target([], {"seq": "y"}) == (0, SEQ_SKIP_UNKNOWN)


async def test_an_unreadable_store_without_our_record_anywhere_starts_the_address_fresh(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The primary is gone (no corrupt copy either), the backup only knows other addresses: ours never sent, so it
    starts at 0 — the backup's other records are kept."""
    fake_link.expect_replays = (
        True  # it did send (the store is made up here): the mesh drops the repeats
    )
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    del hass_storage[SEQ_STORE_KEY]
    backup = hass_storage[f"{SEQ_STORE_KEY}.backup"]["data"]["addresses"]
    backup["0D07"] = backup.pop("0D00")
    assert await hass.config_entries.async_setup(init_integration.entry_id)
    await wait_for_link(hass, init_integration)
    assert (
        "sequence-number store unreadable, continuing from the backup copy"
        in caplog.text
    )
    assert "0D07" in hass_storage[SEQ_STORE_KEY]["data"]["addresses"]
    assert hub_of(init_integration).state.seq < SEQ_RESTART_MARGIN + 100


async def test_store_minor_versions_both_ways(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """1.1 records (no `seq_guard`, no `in_backup`) load unchanged into this version's store; a 1.3 store (with a
    guard and a backup's mark) loads unchanged into a reader of 1.1 that has no migration for it — an older
    integration keeps starting."""
    key = f"{DOMAIN}.seq.test-minor"
    record = {"seq": 7, "iv_index": 2, "iv_update_active": False, "clean": False}
    hass_storage[key] = {
        "version": STORAGE_VERSION,
        "key": key,
        "data": {"addresses": {"0D00": record}},
    }
    store = SeqStore(hass, STORAGE_VERSION, key, atomic_writes=True)
    assert store.minor_version == SEQ_STORAGE_MINOR_VERSION == 3
    assert await store.async_load() == {"addresses": {"0D00": record}}
    assert hass_storage[key]["minor_version"] == 3
    guarded = {**record, "seq_guard": SEQ_GUARD_FIRST_BEACON, "in_backup": "0" * 32}
    await store.async_save({"addresses": {"0D00": guarded}})
    older = Store(hass, STORAGE_VERSION, key)  # minor version 1, no migration
    assert await older.async_load() == {"addresses": {"0D00": guarded}}
    with pytest.raises(NotImplementedError):
        await store._async_migrate_func(STORAGE_VERSION + 1, 1, {})


# ----------------------------------------------------------------------------- an IV index ahead of the mesh (D10)


async def push_ahead(
    hass: HomeAssistant, hub: JungHomeHub, fake_link: FakeProxyLink
) -> int:
    """A forged beacon takes Home Assistant 42 ahead of the mesh (index 0), which then beacons its own index again.

    The mesh stays where it is, so it cannot open what Home Assistant sends meanwhile (the filter re-sent under
    the new index). Returns the counter the push restarted from 0 (what a rewind has to stay above).
    """
    for _ in range(3):
        hub.state.next_seq()
    peak = hub.state.seq
    fake_link.expect_undecryptable = True
    fake_link.inject_beacon(iv_index=42)
    fake_link.iv_index = 0  # forged: the mesh did not move
    await settle(hass)
    assert (hub.state.iv_index, hub.state.seq_peak) == (42, peak)
    fake_link.inject_beacon(iv_index=0)  # the mesh's own, now "behind" us: ignored
    await settle(hass)
    assert hub.state.iv_index == 42
    return peak


async def test_an_iv_index_pushed_ahead_is_taken_back_by_the_repair(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    hass_storage: dict[str, Any],
) -> None:
    """Review-4 D10 (H4-3): Home Assistant ahead of the mesh used to get a repair that could not work (edit the
    store, which the next start overwrites). Now the fix goes back to the mesh's index: the floor first, then both
    copies of the store, above every number sent from there on and guarded up to the old index; the setup that
    follows starts at the mesh's index and the mesh opens what it sends (no reused nonce: the fake's teardown)."""
    hass_storage[
        f"{SEQ_STORE_KEY}.floor"
    ] = {  # an earlier seq_store_lost repair's floor
        "version": STORAGE_VERSION,
        "minor_version": SEQ_STORAGE_MINOR_VERSION,
        "key": f"{SEQ_STORE_KEY}.floor",
        "data": {"addresses": {"0D00": {"iv_index": 0, "seq": 7}}},
    }
    hub = hub_of(init_integration)
    peak = await push_ahead(hass, hub, fake_link)
    issue = find_issue(hass, ISSUE_IV_INDEX_MISMATCH)
    assert issue is not None
    assert issue.translation_key == ISSUE_IV_INDEX_AHEAD
    assert issue.translation_placeholders["mesh"] == "0"
    assert issue.translation_placeholders["ours"] == "42"
    assert (await run_fix_flow(hass, ISSUE_IV_INDEX_MISMATCH))["type"] == "create_entry"
    assert find_issue(hass, ISSUE_IV_INDEX_MISMATCH) is None
    await hass.async_block_till_done()
    assert hass_storage[f"{SEQ_STORE_KEY}.floor"]["data"]["addresses"]["0D00"] == {
        "iv_index": 0,
        "seq": peak,
        "seq_guard": 42,
    }
    backup = hass_storage[f"{SEQ_STORE_KEY}.backup"]["data"]["addresses"]["0D00"]
    assert (backup["iv_index"], backup["seq_guard"], backup["iv_known"]) == (
        0,
        42,
        False,
    )
    assert backup["seq"] >= peak
    await wait_for_link(hass, init_integration)
    state = hub_of(init_integration).state
    assert state is not hub.state
    assert (state.iv_index, state.tx_iv_index, state.iv_known) == (0, 0, True)
    assert state.seq_guard == 42
    assert state.seq > peak
    primary = hass_storage[SEQ_STORE_KEY]["data"]["addresses"]["0D00"]
    assert (primary["iv_index"], primary["seq_guard"]) == (0, 42)
    assert find_issue(hass, ISSUE_IV_INDEX_MISMATCH) is None


async def test_a_record_lost_after_the_rewind_keeps_its_guard(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    hass_storage: dict[str, Any],
) -> None:
    """Both copies lost after a rewind: the seq_store_lost repair continues from the floor under the mesh's index and
    keeps the guard over the indexes used while ahead, so a later IV update cannot restart the counter at 0 there."""
    await push_ahead(hass, hub_of(init_integration), fake_link)
    assert (await run_fix_flow(hass, ISSUE_IV_INDEX_MISMATCH))["type"] == "create_entry"
    await hass.async_block_till_done()
    await wait_for_link(hass, init_integration)
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    del hass_storage[SEQ_STORE_KEY]
    del hass_storage[f"{SEQ_STORE_KEY}.backup"]
    assert not await hass.config_entries.async_setup(init_integration.entry_id)
    assert (await run_fix_flow(hass, ISSUE_SEQ_STORE_LOST))["type"] == "create_entry"
    record = hass_storage[SEQ_STORE_KEY]["data"]["addresses"]["0D00"]
    assert (record["iv_index"], record["iv_known"], record["seq_guard"]) == (
        0,
        False,
        42,
    )
    floor = hass_storage[f"{SEQ_STORE_KEY}.floor"]["data"]["addresses"]["0D00"]
    assert floor["seq_guard"] == 42
    await hass.async_block_till_done()
    await wait_for_link(hass, init_integration)
    # the first beacon (index 0) raises the guard to 1 at least: 42 stays
    assert hub_of(init_integration).state.seq_guard == 42


async def test_an_iv_index_mismatch_is_not_fixable_when_the_mesh_is_ahead(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Going back is the only thing the repair can do: a mesh out of reach *ahead* of Home Assistant gets the
    manual advice (a new unicast address), and a stale fix flow for it aborts without touching the counter."""
    hub = hub_of(init_integration)
    fake_link.inject_beacon(iv_index=43)
    await settle(hass)
    issue = find_issue(hass, ISSUE_IV_INDEX_MISMATCH)
    assert issue is not None
    assert not issue.is_fixable
    assert issue.translation_key == ISSUE_IV_INDEX_MISMATCH
    ir.async_create_issue(  # a fixable one from before, still open in the UI
        hass,
        DOMAIN,
        issue.issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_IV_INDEX_AHEAD,
        data={"entry_id": init_integration.entry_id, "network": 43},
    )
    seq = hub.state.seq
    result = await run_fix_flow(hass, ISSUE_IV_INDEX_MISMATCH)
    assert (result["type"], result["reason"]) == ("abort", "iv_index_not_ahead")
    assert (hub.state.iv_index, hub.state.seq) == (0, seq)
    fake_link.inject_beacon(iv_index=0)  # the fake's mesh back where Home Assistant is
    await settle(hass)


async def test_an_iv_index_ahead_without_a_known_peak_is_not_fixable(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """A record from before the sequence peak was kept knows nothing below its own index: going back could reuse
    numbers sent there, so the issue only advises a new unicast address."""
    hub = hub_of(init_integration)
    await push_ahead(hass, hub, fake_link)
    hub.state.seq_peak_from = 42
    fake_link.inject_beacon(iv_index=0)
    await settle(hass)
    issue = find_issue(hass, ISSUE_IV_INDEX_MISMATCH)
    assert issue is not None
    assert not issue.is_fixable
    assert issue.translation_key == ISSUE_IV_INDEX_MISMATCH


async def test_the_rewind_aborts_when_its_floor_is_not_written(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = hub_of(init_integration)
    await push_ahead(hass, hub, fake_link)
    floor = coordinator.seq_floor_store_for_uuid(hass, MESH_UUID)
    monkeypatch.setattr(floor, "async_save", AsyncMock())  # the write never lands
    result = await run_fix_flow(hass, ISSUE_IV_INDEX_MISMATCH)
    assert (result["type"], result["reason"]) == ("abort", "seq_store_not_written")
    assert hub.state.iv_index == 42  # nothing moved
    assert find_issue(hass, ISSUE_IV_INDEX_MISMATCH) is not None


async def test_the_rewind_aborts_when_the_state_moves_while_its_floor_is_written(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = hub_of(init_integration)
    await push_ahead(hass, hub, fake_link)
    floor = coordinator.seq_floor_store_for_uuid(hass, MESH_UUID)
    save = floor.async_save

    async def save_and_move(data: dict[str, Any]) -> None:
        await save(data)
        hub.state.seq_guard = 50  # what the floor holds no longer covers the state

    monkeypatch.setattr(floor, "async_save", save_and_move)
    result = await run_fix_flow(hass, ISSUE_IV_INDEX_MISMATCH)
    assert (result["type"], result["reason"]) == ("abort", "iv_index_not_ahead")
    assert hub.state.iv_index == 42
