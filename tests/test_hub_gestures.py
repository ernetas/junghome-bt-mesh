"""The hub's button gestures (`hub/gestures.py`): key events, repeat suppression and the click delay option."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.junghome_ble.const import (
    BUTTON_REPEAT_WINDOW,
    CONF_CDB_PATH,
    CONF_METADATA_DIR,
    CONF_UNICAST,
    DOMAIN,
    DOUBLE_CLICK_WINDOW,
    OPTION_CLICK_DELAY,
    TID_REPEAT_WINDOW,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.pdu import (
    ALL_NODES,
    encode_opcode,
)

from .conftest import (
    CDB_PATH,
    META_DIR,
    FakeProxyLink,
    settle,
    setup_entry,
    wait_for_link,
)
from .helpers import (
    BUTTON_CLICK,
    BUTTON_DIMMER,
    BUTTON_HOLD_END,
    BUTTON_HOLD_START,
    BUTTON_WC,
    GROUP_DIMMER,
    ROCKER_A,
    vendor_button_event,
)
from .test_coordinator import (
    events_of,
    hub_of,
)
from .test_coordinator import (
    no_property_reads as no_property_reads,  # noqa: PLC0414  # the autouse fixture
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant


ROCKER_DOWN_CLICK, ROCKER_UP_CLICK, ROCKER_DOWN_HOLD, ROCKER_UP_HOLD = (
    0x00,
    0x01,
    0x02,
    0x03,
)


async def test_vendor_button_events(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    hub = hub_of(init_integration)
    got = events_of(hub, BUTTON_WC)

    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_CLICK))
    fake_link.inject(
        BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_CLICK)
    )  # the firmware publishes every event twice
    assert got == [("click", {"counter": 1})]

    fake_link.inject(
        BUTTON_WC, 0xC005, vendor_button_event(2, BUTTON_CLICK)
    )  # a second click inside the window
    assert got[-1] == ("double_click", {"counter": 2})

    freezer.tick(1)
    fake_link.inject(
        BUTTON_WC, 0xC005, vendor_button_event(3, BUTTON_CLICK)
    )  # too late for a double click
    assert got[-1] == ("click", {"counter": 3})

    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(4, BUTTON_HOLD_START))
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(5, BUTTON_HOLD_END))
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(6, 0x07))
    assert got[-3:] == [
        ("hold_start", {"counter": 4}),
        ("hold_end", {"counter": 5}),
        ("code_07", {"counter": 6}),
    ]

    freezer.tick(BUTTON_REPEAT_WINDOW)
    fake_link.inject(
        BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_CLICK)
    )  # the counter wrapped around: a new click
    assert got[-1] == ("click", {"counter": 1})
    # events of one key never leak to another
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(1, BUTTON_CLICK))
    assert got[-1] == ("click", {"counter": 1})
    assert len(got) == 7


async def test_double_press_copies_interleave(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """On air a double press is `16 05, 17 05, 16 05, 17 05`: each event published twice ~1 s apart, the copies
    interleaved with the second press. One click and one double click, nothing more."""
    hub = hub_of(init_integration)
    got = events_of(hub, BUTTON_WC)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(0x16, BUTTON_CLICK))
    freezer.tick(0.3)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(0x17, BUTTON_CLICK))
    freezer.tick(0.7)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(0x16, BUTTON_CLICK))
    freezer.tick(0.3)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(0x17, BUTTON_CLICK))
    assert got == [("click", {"counter": 0x16}), ("double_click", {"counter": 0x17})]

    # a hold right after, both copies as well
    freezer.tick(0.5)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(0x18, BUTTON_HOLD_START))
    freezer.tick(1)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(0x18, BUTTON_HOLD_START))
    freezer.tick(1)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(0x19, BUTTON_HOLD_END))
    freezer.tick(1)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(0x19, BUTTON_HOLD_END))
    assert got[2:] == [
        ("hold_start", {"counter": 0x18}),
        ("hold_end", {"counter": 0x19}),
    ]


async def test_button_counter_wraps_around(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    hub = hub_of(init_integration)
    got = events_of(hub, BUTTON_WC)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(0xFF, BUTTON_CLICK))
    freezer.tick(0.2)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(0x00, BUTTON_CLICK))
    freezer.tick(0.8)
    fake_link.inject(
        BUTTON_WC, 0xC005, vendor_button_event(0xFF, BUTTON_CLICK)
    )  # second copies
    freezer.tick(0.2)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(0x00, BUTTON_CLICK))
    assert got == [("click", {"counter": 0xFF}), ("double_click", {"counter": 0x00})]

    freezer.tick(
        BUTTON_REPEAT_WINDOW
    )  # once the window passed a counter may come round again
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(0x00, BUTTON_HOLD_START))
    assert got[-1] == ("hold_start", {"counter": 0x00})
    assert len(got) == 3


async def test_rocker_half_events_carry_their_side(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rocker in gateway mode reports codes 0-3 (`docs/cross-repo-analysis.md` §1.2): clicks and holds of its lower /
    upper half, with the side as an attribute; the release (4) takes the side of the hold it ends."""
    hub = hub_of(init_integration)
    got = events_of(hub, ROCKER_A)
    other = events_of(hub, BUTTON_WC)

    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(1, ROCKER_DOWN_CLICK))
    assert got == [("click", {"counter": 1, "side": "down"})]
    freezer.tick(0.3)
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(2, ROCKER_DOWN_CLICK))
    assert got[-1] == ("double_click", {"counter": 2, "side": "down"})

    # a click of the other half right after is a click of another key, not a double click
    freezer.tick(1)
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(3, ROCKER_UP_CLICK))
    freezer.tick(0.3)
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(4, ROCKER_DOWN_CLICK))
    assert got[-2:] == [
        ("click", {"counter": 3, "side": "up"}),
        ("click", {"counter": 4, "side": "down"}),
    ]

    # holds: the release carries the side of the hold it ends, per element
    freezer.tick(1)
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(5, ROCKER_DOWN_HOLD))
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_HOLD_START))
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(6, BUTTON_HOLD_END))
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(2, BUTTON_HOLD_END))
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(7, ROCKER_UP_HOLD))
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(8, BUTTON_HOLD_END))
    assert got[-4:] == [
        ("hold_start", {"counter": 5, "side": "down"}),
        ("hold_end", {"counter": 6, "side": "down"}),
        ("hold_start", {"counter": 7, "side": "up"}),
        ("hold_end", {"counter": 8, "side": "up"}),
    ]
    assert other == [("hold_start", {"counter": 1}), ("hold_end", {"counter": 2})]

    # a release without a hold, and the single-key codes: no side
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(9, BUTTON_HOLD_END))
    freezer.tick(1)
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(10, BUTTON_CLICK))
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(11, BUTTON_HOLD_START))
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(12, BUTTON_HOLD_END))
    assert got[-4:] == [
        ("hold_end", {"counter": 9}),
        ("click", {"counter": 10}),
        ("hold_start", {"counter": 11}),
        ("hold_end", {"counter": 12}),
    ]

    # a single-key click and a rocker click of one side are not a double click of each other either
    freezer.tick(1)
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(13, BUTTON_CLICK))
    freezer.tick(0.2)
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(14, ROCKER_UP_CLICK))
    freezer.tick(0.2)
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(15, ROCKER_UP_CLICK))
    assert got[-3:] == [
        ("click", {"counter": 13}),
        ("click", {"counter": 14, "side": "up"}),
        ("double_click", {"counter": 15, "side": "up"}),
    ]
    assert "not in the firmware's table" not in caplog.text


async def test_unknown_button_codes_are_delivered_and_logged_once(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A code outside the firmware's table still reaches the listeners (the key is awake) and is logged once per key
    and code, with its counter."""
    hub = hub_of(init_integration)
    wc, rocker = events_of(hub, BUTTON_WC), events_of(hub, ROCKER_A)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(6, 0x07))
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(7, 0x07))
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(8, 0x09))
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(1, 0x07))
    assert wc == [
        ("code_07", {"counter": 6}),
        ("code_07", {"counter": 7}),
        ("code_09", {"counter": 8}),
    ]
    assert rocker == [("code_07", {"counter": 1})]
    lines = [
        record
        for record in caplog.records
        if record.levelname == "INFO"
        and "not in the firmware's table" in record.message
    ]
    assert [line.message.split(", which")[0] for line in lines] == [
        "Key 0149 sent button event code 0x07 (counter 6)",
        "Key 0149 sent button event code 0x09 (counter 8)",
        "Key 0234 sent button event code 0x07 (counter 1)",
    ]


async def test_sig_button_messages(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    hub = hub_of(init_integration)
    got = events_of(hub, BUTTON_DIMMER)
    fake_link.inject(
        BUTTON_DIMMER, GROUP_DIMMER, M.generic_onoff_set(True, ack=False, tid=1)
    )
    fake_link.inject(
        BUTTON_DIMMER, GROUP_DIMMER, M.generic_onoff_set(False, ack=True, tid=2)
    )
    fake_link.inject(BUTTON_DIMMER, ALL_NODES, M.scene_recall(2, ack=False, tid=3))
    fake_link.inject(BUTTON_DIMMER, ALL_NODES, M.scene_recall(1, ack=True, tid=4))
    fake_link.inject(
        BUTTON_DIMMER,
        GROUP_DIMMER,
        encode_opcode(M.GEN_LEVEL_SET_UNACK) + b"\x00\x40\x05",
    )
    fake_link.inject(
        BUTTON_DIMMER, GROUP_DIMMER, encode_opcode(0x820B) + b"\x00\x10\x06"
    )  # Generic Move Set
    assert got == [
        ("press_on", {"target": "C070"}),
        ("press_off", {"target": "C070"}),
        ("scene", {"scene": 2}),
        ("scene", {"scene": 1}),
        ("dim", {"target": "C070", "raw": "004005"}),
        ("dim", {"target": "C070", "raw": "001006"}),
        ("hold_start", {"target": "C070", "direction": "up"}),  # test_event.py
    ]


async def test_sig_button_messages_are_deduplicated(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Rockers publish their client messages twice as well (fresh SEQ, same TID): the second copy within the
    transaction window is dropped, a new TID, a changed payload or another element are not."""
    hub = hub_of(init_integration)
    got = events_of(hub, BUTTON_DIMMER)
    other = events_of(hub, ROCKER_A)
    on = M.generic_onoff_set(True, ack=False, tid=7)
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, on)
    freezer.tick(1)
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, on)  # the copy
    fake_link.inject(
        BUTTON_DIMMER, GROUP_DIMMER, M.generic_onoff_set(True, ack=False, tid=8)
    )  # a new press
    fake_link.inject(ROCKER_A, GROUP_DIMMER, on)  # same message, other element
    recall = M.scene_recall(2, ack=True, tid=9)
    fake_link.inject(BUTTON_DIMMER, ALL_NODES, recall)
    fake_link.inject(BUTTON_DIMMER, ALL_NODES, recall)
    delta = encode_opcode(0x820A) + (100).to_bytes(
        4, "little", signed=True
    )  # Generic Delta Set Unack
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, delta + b"\x0a")
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, delta + b"\x0a")
    fake_link.inject(
        BUTTON_DIMMER,
        GROUP_DIMMER,
        encode_opcode(0x820A) + (200).to_bytes(4, "little", signed=True) + b"\x0a",
    )  # same transaction, larger delta
    short = (
        encode_opcode(M.GEN_ONOFF_SET_UNACK) + b"\x01"
    )  # no TID at all: nothing to compare, never deduplicated
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, short)
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, short)
    assert got == [
        ("press_on", {"target": "C070"}),
        ("press_on", {"target": "C070"}),
        ("scene", {"scene": 2}),
        ("dim", {"target": "C070", "raw": "640000000a"}),
        ("hold_start", {"target": "C070", "direction": "up"}),  # test_event.py
        ("dim", {"target": "C070", "raw": "c80000000a"}),
        ("press_on", {"target": "C070"}),
        ("press_on", {"target": "C070"}),
    ]
    assert other == [("press_on", {"target": "C070"})]

    freezer.tick(
        TID_REPEAT_WINDOW
    )  # the transaction is over: the same TID means a new press
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, on)
    assert got[-1:] == [("press_on", {"target": "C070"})]
    assert len(got) == 9


async def test_event_listener_can_be_removed(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    hub = hub_of(init_integration)
    got: list[str] = []
    unsub = hub.add_event_listener(BUTTON_WC, lambda event, attrs: got.append(event))
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_CLICK))
    unsub()
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(2, BUTTON_HOLD_START))
    assert got == ["click"]


@pytest.fixture
async def delayed_clicks(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> MockConfigEntry:
    """The integration with the click-delay option switched on."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={
            CONF_CDB_PATH: CDB_PATH,
            CONF_METADATA_DIR: META_DIR,
            CONF_UNICAST: "0D00",
        },
        options={OPTION_CLICK_DELAY: True},
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    return entry


async def _let_time_pass(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    """Advance the clock and fire exactly the timers that are due (no half-second fudge: the window is 0.5 s)."""
    freezer.tick(seconds)
    async_fire_time_changed_exact(hass)
    await hass.async_block_till_done()


async def test_click_delay_is_off_by_default(init_integration: MockConfigEntry) -> None:
    assert hub_of(init_integration).click_delay is False


async def test_click_delay_reports_a_single_click_once_the_window_passed(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    delayed_clicks: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    hub = hub_of(delayed_clicks)
    assert hub.click_delay is True
    got = events_of(hub, BUTTON_WC)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_CLICK))
    fake_link.inject(
        BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_CLICK)
    )  # the firmware's second copy
    assert got == []  # held back
    await _let_time_pass(hass, freezer, DOUBLE_CLICK_WINDOW - 0.1)
    assert got == []
    await _let_time_pass(hass, freezer, 0.2)
    assert got == [("click", {"counter": 1})]
    # the next click, well after the first, is again a single one
    await _let_time_pass(hass, freezer, BUTTON_REPEAT_WINDOW)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(2, BUTTON_CLICK))
    await _let_time_pass(hass, freezer, DOUBLE_CLICK_WINDOW + 0.1)
    assert got == [("click", {"counter": 1}), ("click", {"counter": 2})]


async def test_click_delay_suppresses_the_click_of_a_double_press(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    delayed_clicks: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """On air: `16 05, 17 05, 16 05, 17 05` (each press twice, interleaved). Only the double click is reported."""
    hub = hub_of(delayed_clicks)
    got = events_of(hub, BUTTON_WC)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(0x16, BUTTON_CLICK))
    freezer.tick(0.3)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(0x17, BUTTON_CLICK))
    assert got == [("double_click", {"counter": 0x17})]
    await _let_time_pass(hass, freezer, 0.7)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(0x16, BUTTON_CLICK))
    await _let_time_pass(hass, freezer, 0.3)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(0x17, BUTTON_CLICK))
    await _let_time_pass(hass, freezer, 2)  # the held-back click never fires
    assert got == [("double_click", {"counter": 0x17})]


async def test_click_delay_reports_the_click_before_a_following_gesture(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    delayed_clicks: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """A hold right after a click ends the wait: the click is reported first, then the hold, in order."""
    hub = hub_of(delayed_clicks)
    got = events_of(hub, BUTTON_WC)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_CLICK))
    freezer.tick(0.2)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(2, BUTTON_HOLD_START))
    assert got == [("click", {"counter": 1}), ("hold_start", {"counter": 2})]
    await _let_time_pass(hass, freezer, 1)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(3, BUTTON_HOLD_END))
    await _let_time_pass(hass, freezer, 1)
    assert got[2:] == [("hold_end", {"counter": 3})]  # no second click from the timer


async def test_click_delay_is_per_key(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    delayed_clicks: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    hub = hub_of(delayed_clicks)
    wc, rocker = events_of(hub, BUTTON_WC), events_of(hub, ROCKER_A)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_CLICK))
    freezer.tick(0.2)
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(1, BUTTON_CLICK))
    assert (wc, rocker) == ([], [])  # neither is a double click of the other
    await _let_time_pass(hass, freezer, DOUBLE_CLICK_WINDOW)
    assert (wc, rocker) == ([("click", {"counter": 1})], [("click", {"counter": 1})])


async def test_click_delay_keeps_the_rocker_side(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    delayed_clicks: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """A held-back click keeps its side; a click of the other half ends the wait (it is another key's click) and is
    itself held back; only two clicks of the same half make a double click."""
    hub = hub_of(delayed_clicks)
    got = events_of(hub, ROCKER_A)
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(1, ROCKER_DOWN_CLICK))
    assert got == []
    await _let_time_pass(hass, freezer, DOUBLE_CLICK_WINDOW)
    assert got == [("click", {"counter": 1, "side": "down"})]

    await _let_time_pass(hass, freezer, BUTTON_REPEAT_WINDOW)
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(2, ROCKER_UP_CLICK))
    freezer.tick(0.2)
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(3, ROCKER_DOWN_CLICK))
    assert got[1:] == [
        ("click", {"counter": 2, "side": "up"})
    ]  # reported at once, the down click waits
    await _let_time_pass(hass, freezer, DOUBLE_CLICK_WINDOW)
    assert got[1:] == [
        ("click", {"counter": 2, "side": "up"}),
        ("click", {"counter": 3, "side": "down"}),
    ]

    await _let_time_pass(hass, freezer, BUTTON_REPEAT_WINDOW)
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(4, ROCKER_UP_CLICK))
    freezer.tick(0.2)
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(5, ROCKER_UP_CLICK))
    await _let_time_pass(hass, freezer, DOUBLE_CLICK_WINDOW + 1)
    assert got[3:] == [("double_click", {"counter": 5, "side": "up"})]


async def test_click_delay_pending_click_is_dropped_on_stop(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    delayed_clicks: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    hub = hub_of(delayed_clicks)
    got = events_of(hub, BUTTON_WC)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_CLICK))
    assert await hass.config_entries.async_unload(delayed_clicks.entry_id)
    await _let_time_pass(hass, freezer, DOUBLE_CLICK_WINDOW + 1)
    assert got == []
