"""Another client on Home Assistant's address (review-4 S I2): detected, sends refused, skipped past by the repair."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.junghome_ble.const import (
    DOMAIN,
    ISSUE_ADDRESS_SHARED,
    ISSUE_ADDRESS_SHARED_AGAIN,
    ISSUE_PDUS_DROPPED,
)
from custom_components.junghome_ble.coordinator import (
    SEQ_RESTART_MARGIN,
    AddressShared,
    JungHomeHub,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.client import (
    SEQ_GUARD_FIRST_BEACON,
    SEQ_MAX,
)
from custom_components.junghome_ble.seq_store import _stored_address_shared

from .conftest import FakeProxyLink, settle, wait_for_link, wait_until
from .helpers import OUR_ADDRESS, SEQ_STORE_KEY, find_issue
from .test_seq_store import run_fix_flow

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

LIGHT = 0x0232


def hub_of(entry: MockConfigEntry) -> JungHomeHub:
    return entry.runtime_data


def other_client_sends(fake_link: FakeProxyLink) -> int:
    """A message from Home Assistant's own address, as another client sends one; returns its sequence number."""
    fake_link.inject(OUR_ADDRESS, LIGHT, M.generic_onoff_set(True))
    return fake_link.src_seq[OUR_ADDRESS]


async def test_another_client_on_our_address_stops_sends_until_the_repair(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = hub_of(init_integration)
    seq = hub.state.seq
    hub._report_pdus_dropped(
        True
    )  # what the nodes dropping our PDUs looked like so far

    with caplog.at_level(logging.ERROR):
        seen = other_client_sends(fake_link)
    assert seen > seq
    assert hub.state.address_shared == (0, seen)
    issue = find_issue(hass, ISSUE_ADDRESS_SHARED)
    assert issue is not None
    assert (issue.translation_key, issue.is_fixable) == (ISSUE_ADDRESS_SHARED, True)
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders == {
        "title": "JUNG HOME mesh test",
        "unicast": "0D00",
    }
    assert (
        "Another Bluetooth mesh client sends from Home Assistant's address 0D00"
        in caplog.text
    )
    # the shared address is the explanation: no `pdus_dropped` next to it, and none raised while it is open
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None
    hub._report_pdus_dropped(True)
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None

    # nothing goes out, not even after waiting: no number is taken, none can collide
    with pytest.raises(AddressShared):
        await hub.proxy.send_access(LIGHT, M.generic_onoff_set(True))
    with pytest.raises(AddressShared):
        await hub._while_seq_stalls(
            lambda: hub.proxy.send_access(LIGHT, M.generic_onoff_set(True))
        )
    assert hub.state.seq == seq
    await hass.async_block_till_done()
    record = hass_storage[SEQ_STORE_KEY]["data"]["addresses"]["0D00"]
    assert record["address_shared"] == [0, seen]  # a restart keeps refusing

    assert (await run_fix_flow(hass, ISSUE_ADDRESS_SHARED))["type"] == "create_entry"
    assert find_issue(hass, ISSUE_ADDRESS_SHARED) is None
    assert hub.state.address_shared is None
    assert hub.state.seq == seen + 1 + SEQ_RESTART_MARGIN
    assert "continue from" in caplog.text
    await hass.async_block_till_done()
    assert (
        "address_shared" not in hass_storage[SEQ_STORE_KEY]["data"]["addresses"]["0D00"]
    )
    sent = len(fake_link.sent)
    await hub.proxy.send_access(LIGHT, M.generic_onoff_set(True))  # sends again
    assert len(fake_link.sent) == sent + 1
    assert fake_link.connect_count == 1  # the link had its filter: kept

    # an echo of our own number is not another sighting; the other client still sending above us is, and now
    # the issue asks for another address
    fake_link.src_seq[OUR_ADDRESS] = hub.state.seq - 2
    other_client_sends(fake_link)
    assert find_issue(hass, ISSUE_ADDRESS_SHARED) is None
    hub.proxy._foreign_reported_at = None  # past the library's rate limit
    fake_link.src_seq[OUR_ADDRESS] = hub.state.seq + 10
    again = other_client_sends(fake_link)
    issue = find_issue(hass, ISSUE_ADDRESS_SHARED)
    assert issue is not None
    assert issue.translation_key == ISSUE_ADDRESS_SHARED_AGAIN
    assert hub.state.address_shared == (0, again)


async def test_a_stored_sighting_refuses_sends_after_a_restart(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    hub = hub_of(init_integration)
    seen = other_client_sends(fake_link)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert (
        find_issue(hass, ISSUE_ADDRESS_SHARED) is None
    )  # an unloaded mesh shows no live problems

    assert await hass.config_entries.async_setup(init_integration.entry_id)
    await wait_for_link(hass, init_integration)
    await settle(hass)
    hub = hub_of(init_integration)
    assert hub.state.address_shared == (0, seen)
    issue = find_issue(hass, ISSUE_ADDRESS_SHARED)
    assert issue is not None
    assert issue.translation_key == ISSUE_ADDRESS_SHARED
    seq = hub.state.seq
    with pytest.raises(AddressShared):
        await hub.proxy.send_access(LIGHT, M.generic_onoff_set(True))
    assert hub.state.seq == seq
    assert hub.proxy.proxy_addr is None  # the filter request was refused too

    # the repair renews a link whose proxy never took our filter
    assert (await run_fix_flow(hass, ISSUE_ADDRESS_SHARED))["type"] == "create_entry"
    await wait_until(hass, lambda: fake_link.connect_count == 3, what="a new link")
    await wait_for_link(hass, init_integration)
    assert hub.state.seq >= seen + 1 + SEQ_RESTART_MARGIN


async def test_the_repair_after_the_sighting_was_cleared_only_deletes_the_issue(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    hub = hub_of(init_integration)
    seq = hub.state.seq
    hub._report_address_shared()  # an issue whose sighting is gone (cleared meanwhile)
    assert (await run_fix_flow(hass, ISSUE_ADDRESS_SHARED))["type"] == "create_entry"
    assert find_issue(hass, ISSUE_ADDRESS_SHARED) is None
    assert hub.state.seq == seq


async def test_skipping_past_a_sighting_under_the_next_iv_index_guards_it(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The other client already transmits under the index an IV Update moves to: the counter must not restart at 0
    there, so the guard covers it; one already guarding further, or waiting for the first beacon, keeps its own."""
    state = hub_of(init_integration).state
    state.iv_index, state.iv_update_active = 1, True  # transmitting under 0
    for guard, expected in (
        (None, 1),
        (0, 1),
        (3, 3),
        (SEQ_GUARD_FIRST_BEACON, SEQ_GUARD_FIRST_BEACON),
    ):
        state.seq_guard = guard
        assert state.note_address_shared(1, 10)  # the first sighting
        assert state.address_shared == (1, 10)
        assert not state.note_address_shared(1, 5)  # lower: the highest is kept
        assert state.address_shared == (1, 10)
        state.skip_past_shared()
        assert state.seq_guard == expected
    assert state.skip_past_shared() is None  # nothing seen
    # a number at the very end: the counter stops at SEQ_MAX, which is never sent
    state.note_address_shared(0, SEQ_MAX)
    assert state.skip_past_shared() == SEQ_MAX


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({}, None),
        (None, None),
        ({"address_shared": [2, 900]}, (2, 900)),
        ({"address_shared": [2]}, None),
        ({"address_shared": ["x", 1]}, None),
        ({"address_shared": [0, SEQ_MAX + 1]}, None),
        ({"address_shared": 7}, None),
    ],
)
def test_a_stored_sighting_is_range_checked(
    record: Any, expected: tuple[int, int] | None, caplog: pytest.LogCaptureFixture
) -> None:
    assert _stored_address_shared(record) == expected
    if expected is None and record and "address_shared" in record:
        assert "Ignoring an unusable record of another client" in caplog.text


async def test_the_issue_goes_with_the_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    other_client_sends(fake_link)
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_ADDRESS_SHARED}_{init_integration.entry_id}"
    )
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert find_issue(hass, ISSUE_ADDRESS_SHARED) is None
