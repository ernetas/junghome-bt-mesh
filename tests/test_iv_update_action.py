"""`junghome_ble.start_iv_update`: Home Assistant starts an IV Update of its mesh (review-4 P I-11).

The hub's real client sends the fake proxy its Secure Network beacon; the fake follows it as Mesh Protocol 1.1 §6.7
has a proxy do and beacons the new state back. The library side (the guards, the beacon, the return to Normal
Operation) is tested in `tests/jhmesh/test_iv_update_start.py`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.junghome_ble.const import DOMAIN, ISSUE_SEQUENCE_SPACE_LOW
from custom_components.junghome_ble.hub.issues import SEQUENCE_SPACE_WARN
from custom_components.junghome_ble.jhmesh import state as state_mod
from custom_components.junghome_ble.jhmesh.keyrefresh import KeyRefreshRecord
from custom_components.junghome_ble.jhmesh.state import (
    IV_INDEX_MAX,
    IV_UPDATE_MAX_STATE,
    IV_UPDATE_MIN_STATE,
)

from .conftest import settle
from .helpers import SEQ_STORE_KEY, find_issue

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

    from custom_components.junghome_ble.coordinator import JungHomeHub

    from .conftest import FakeProxyLink

T0 = 1_800_000_000.0  # the wall clock of the tests (seconds since the epoch)
COMPONENT = Path(__file__).parents[1] / "custom_components" / DOMAIN


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The wall clock the IV timing reads (`jhmesh.state._wall_now`), moved by the test."""
    clock = [T0]
    monkeypatch.setattr(state_mod, "_wall_now", lambda: clock[0])
    return clock


def hub_of(entry: MockConfigEntry) -> JungHomeHub:
    return entry.runtime_data


async def start(hass: HomeAssistant, **data: Any) -> Any:
    return await hass.services.async_call(
        DOMAIN, "start_iv_update", data, blocking=True, return_response=True
    )


def running_low(hub: JungHomeHub) -> None:
    """A node past three quarters of the sequence space, and the repair that says so."""
    hub.proxy.state.rpl[0x0148] = (hub.proxy.state.iv_index, SEQUENCE_SPACE_WARN + 5)
    hub.issues.check_sequence_space()


def utc(timestamp: float) -> str:
    return dt_util.utc_from_timestamp(timestamp).isoformat()


async def test_start_sends_the_beacon_and_answers_the_new_index(
    hass: HomeAssistant,
    clock: list[float],
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    hass_storage: dict[str, Any],
    hass_client: ClientSessionGenerator,
) -> None:
    hub = hub_of(init_integration)
    state = hub.proxy.state
    running_low(hub)
    assert find_issue(hass, ISSUE_SEQUENCE_SPACE_LOW) is not None
    seq = state.seq
    answer = await start(hass, confirm=True)
    assert answer == {
        "iv_index": 1,
        "transmit_iv_index": 0,  # the old one, while in progress
        "started_by": "home_assistant",
        "started_at": utc(T0),
        "confirmed": False,
        "in_progress": True,
        "normal_operation_from": utc(T0 + IV_UPDATE_MIN_STATE),
        "normal_operation_by": utc(T0 + IV_UPDATE_MAX_STATE),
    }
    assert fake_link.beacons_in == [(1, True)]
    assert state.seq == seq  # a beacon takes no sequence number
    # stored before the beacon went, and the new index's space is untouched: the repair is gone
    stored = hass_storage[SEQ_STORE_KEY]["data"]["addresses"]["0D00"]
    assert (
        stored["iv_index"],
        stored["iv_update_active"],
        stored["iv_update_origin"],
    ) == (
        1,
        True,
        "local",
    )
    assert find_issue(hass, ISSUE_SEQUENCE_SPACE_LOW) is None
    # the proxy took it and beaconed it back (§6.7): confirmed, and the diagnostics say who started it
    await settle(hass)
    assert state.iv_update_confirmed
    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, init_integration
    )
    assert diagnostics["local"]["iv_update"] == {
        **{k: answer[k] for k in answer if k not in ("iv_index", "transmit_iv_index")},
        "confirmed": True,
    }
    # a second call while it runs is refused, with the index in its words
    with pytest.raises(ServiceValidationError) as refused:
        await start(hass, confirm=True, force=True)
    assert refused.value.translation_key == "start_iv_update_in_progress"
    assert refused.value.translation_placeholders["iv_index"] == "1"
    await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()


async def test_it_needs_confirm(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(vol.Invalid):
        await start(hass)
    with pytest.raises(ServiceValidationError) as refused:
        await start(hass, confirm=False, force=True)
    assert refused.value.translation_key == "start_iv_update_needs_confirm"
    assert hub_of(init_integration).proxy.state.iv_index == 0


async def test_it_is_refused_while_no_sender_runs_low_unless_forced(
    hass: HomeAssistant,
    clock: list[float],
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    with pytest.raises(ServiceValidationError) as refused:
        await start(hass, confirm=True)
    assert refused.value.translation_key == "start_iv_update_not_needed"
    assert fake_link.beacons_in == []
    # without asking for the response the action answers nothing
    assert (
        await hass.services.async_call(
            DOMAIN, "start_iv_update", {"confirm": True, "force": True}, blocking=True
        )
        is None
    )
    assert fake_link.beacons_in == [(1, True)]
    await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("setup", "key"),
    [
        (
            lambda s: setattr(s, "key_refresh", KeyRefreshRecord(bytes(16), 1)),
            "start_iv_update_key_refresh",
        ),
        (lambda s: setattr(s, "iv_known", False), "start_iv_update_iv_unknown"),
        (lambda s: setattr(s, "iv_index", IV_INDEX_MAX), "start_iv_update_iv_max"),
    ],
)
async def test_the_librarys_refusals_have_their_own_words(
    hass: HomeAssistant,
    clock: list[float],
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    setup: Any,
    key: str,
) -> None:
    state = hub_of(init_integration).proxy.state
    before = state.iv_index
    setup(state)
    try:
        with pytest.raises(ServiceValidationError) as refused:
            await start(hass, confirm=True, force=True)
    finally:
        state.iv_index = before
    assert refused.value.translation_key == key
    assert refused.value.translation_placeholders["not_before"] == ""
    assert fake_link.beacons_in == []


async def test_within_96_hours_of_the_last_change_it_says_from_when(
    hass: HomeAssistant,
    clock: list[float],
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    hub_of(init_integration).proxy.state.iv_changed_at = T0 - 3600
    with pytest.raises(ServiceValidationError) as refused:
        await start(hass, confirm=True, force=True)
    assert refused.value.translation_key == "start_iv_update_too_early"
    expected = dt_util.as_local(
        dt_util.utc_from_timestamp(T0 - 3600 + IV_UPDATE_MIN_STATE)
    )
    assert refused.value.translation_placeholders == {
        "iv_index": "0",
        "not_before": expected.replace(microsecond=0).isoformat(sep=" "),
    }
    assert fake_link.beacons_in == []


async def test_a_link_lost_mid_call_is_not_connected(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    hub = hub_of(init_integration)
    with (
        patch.object(
            hub.proxy, "start_iv_update", AsyncMock(side_effect=ConnectionError("gone"))
        ),
        pytest.raises(HomeAssistantError) as refused,
    ):
        await start(hass, confirm=True, force=True)
    assert refused.value.translation_key == "service_not_connected"


async def test_a_store_that_does_not_take_it_starts_nothing(
    hass: HomeAssistant,
    clock: list[float],
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """`HAState.persist_durably`: `Store` only logs a failed write; what landed tells, and the start is put back."""
    hub = hub_of(init_integration)
    with (
        patch(
            "homeassistant.helpers.storage.Store._async_write_data",
            side_effect=WriteError("disk full"),
        ),
        pytest.raises(HomeAssistantError) as refused,
    ):
        await start(hass, confirm=True, force=True)
    assert refused.value.translation_key == "start_iv_update_not_stored"
    assert isinstance(refused.value.__cause__, OSError)
    assert "disk full" in str(refused.value.__cause__)
    assert (
        hub.state.iv_index,
        hub.state.iv_update_active,
        hub.state.iv_update_origin,
    ) == (0, False, None)
    assert fake_link.beacons_in == []


async def test_a_superseded_state_writes_nothing_and_says_so(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    state = hub_of(init_integration).state
    await state.persist_durably()  # the store holds the state: fine
    state.iv_update_active = True
    try:
        with (
            patch.object(
                state, "async_save_now", AsyncMock()
            ),  # what a superseded one does
            pytest.raises(OSError, match="not written"),
        ):
            await state.persist_durably()
    finally:
        state.iv_update_active = False


def test_the_repair_names_the_action_and_keeps_its_placeholders() -> None:
    """Every language's `sequence_space_low` text mentions the action and keeps the placeholders it had."""
    texts = {
        path.name: json.loads(path.read_text(encoding="utf-8"))["issues"][
            "sequence_space_low"
        ]["description"]
        for path in [
            COMPONENT / "strings.json",
            *sorted((COMPONENT / "translations").glob("*.json")),
        ]
    }
    assert len(texts) == 27
    for name, text in texts.items():
        assert "`junghome_ble.start_iv_update`" in text, name
        assert set(re.findall(r"\{(\w+)\}", text)) == {
            "title",
            "source",
            "percent",
            "iv_index",
        }, name
