"""The hub's connect-time reads (`hub/refresh.py`): the state refresh, the drop detection, the slower reads after it."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.junghome_ble import coordinator
from custom_components.junghome_ble.const import ISSUE_PDUS_DROPPED
from custom_components.junghome_ble.hub import refresh
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import vendor_models as V
from custom_components.junghome_ble.jhmesh.client import AccessMessage
from custom_components.junghome_ble.jhmesh.pdu import (
    ALL_NODES,
    encode_opcode,
)

from .conftest import (
    STATE_GET_REPLIES,
    FakeProxyLink,
    settle,
)
from .helpers import (
    LIGHT_CTL,
    LIGHT_CTL_TEMPERATURE,
    LIGHT_DIMMER,
    LIGHT_OUT1,
    LIGHT_OUT2,
    LIGHT_SWITCH,
    OUR_ADDRESS,
    UID_LIGHT_CTL,
    ctl_status,
    entity_id,
    find_issue,
    onoff_status,
)
from .test_coordinator import (
    BROADCASTS,
    CONNECT_TAIL,
    COUNTER_GETS,
    CURRENT_SCENE_GETS,
    CURRENT_SCENE_PDUS,
    ENERGY_GETS,
    FAULT_GETS,
    FAULT_GETS_PDUS,
    GROUP_SWITCH,
    REFRESH_GETS,
    SCENE_GETS_PDUS,
    answer_gets,
    hub_of,
    stall_seq,
)
from .test_coordinator import (
    answering_link as answering_link,  # noqa: PLC0414  # the fixture
)
from .test_coordinator import (
    init_answered as init_answered,  # noqa: PLC0414  # the fixture
)
from .test_coordinator import (
    no_property_reads as no_property_reads,  # noqa: PLC0414  # the autouse fixture
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant


async def test_refresh_stops_on_send_failure(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = hub_of(init_answered)
    fake_link.sent.clear()
    fake_link.write_error = OSError("GATT write failed")
    await hub.refresh._refresh_all()
    assert fake_link.sent == []
    assert "refresh aborted: proxy write failed: GATT write failed" in caplog.text

    fake_link.write_error = None
    fake_link.sent.clear()
    hub.proxy.client = None  # nothing to send through
    await hub.refresh._refresh_all()
    assert fake_link.sent == []
    assert "refresh aborted: not connected to a proxy" in caplog.text


async def test_unanswered_refresh_while_the_mesh_is_busy_raises_a_repair(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nodes silently drop our PDUs after a lost sequence store or an address collision; the connect-time refresh notices."""
    hub = hub_of(init_integration)
    assert (
        len(fake_link.sent) == BROADCASTS + 5
    )  # Time Set and location, then the first chunk of Gets is waiting for replies
    # traffic that is not for us: the status LIGHT_SWITCH publishes when the gateway polls it (same source and opcode
    # as our Get, so the request matcher takes it — a reply to us it is not)
    fake_link.inject(LIGHT_SWITCH, GROUP_SWITCH, onoff_status(True))
    await settle(hass)
    for _ in range(
        6
    ):  # every Get times out, is retried twice (the app's three attempts); then the second chunk, whose meter job
        # sends its three single-attempt Gets one after the other
        freezer.tick(3.1)
        async_fire_time_changed(hass)
        await settle(hass)
    assert (
        len(fake_link.sent) == 28
    )  # 5 + 2 * 4 retries + (socket, meter, range, temperature) + 2 * (socket retry, meter, range retry, temperature
    # retry), after the Time Set and the location, then the energy Get (the refresh did complete)
    assert [dst for _, dst, _ in fake_link.sent].count(ALL_NODES) == BROADCASTS
    issue = find_issue(hass, ISSUE_PDUS_DROPPED)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.is_fixable  # the fix skips the counter ahead (`repairs.py`)
    assert issue.translation_key == ISSUE_PDUS_DROPPED
    assert issue.translation_placeholders == {
        "title": "JUNG HOME mesh test",
        "unicast": "0D00",
    }
    assert (
        "No JUNG device answered the state refresh although the link works: the nodes discard our messages "
        "(stale sequence number, or address 0D00 is used by another client)"
    ) in caplog.text

    # the first message addressed to us proves the nodes accept our PDUs again
    fake_link.inject(LIGHT_CTL, OUR_ADDRESS, ctl_status(0, 3000))
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None

    # ... and so does an answered refresh
    hub.report_pdus_dropped(True)
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is not None
    answer_gets(fake_link)
    await hub.refresh._refresh_all()
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None


async def test_unanswered_refresh_in_a_silent_mesh_is_not_reported(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    no_connect_beacon: None,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Without any traffic at all (not even a beacon) an unanswered refresh proves nothing: the proxy may be dead or out
    of reach of the loads, which the link watchdog handles."""
    for _ in range(6):
        freezer.tick(3.1)
        async_fire_time_changed(hass)
        await settle(hass)
    assert (
        len(fake_link.sent) == 30
    )  # 8 Gets tried three times + the meter's 3 single attempts, then the Time Set, the location and the energy Get
    assert [dst for _, dst, _ in fake_link.sent].count(ALL_NODES) == BROADCASTS
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None


@pytest.fixture
def silent_temperature_elements(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake's tunable-white lights answer their state Gets, but not a Light CTL Temperature Get to their
    temperature element (list it before `init_integration`)."""
    monkeypatch.delitem(STATE_GET_REPLIES, M.LIGHT_CTL_TEMP_GET)


async def test_an_unanswered_temperature_element_leaves_a_tunable_white_light_available(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    answering_mesh: FakeProxyLink,
    silent_temperature_elements: None,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """The refresh asks a CTL light's node three times (state, range, temperature element). The temperature element's
    Get is the gateway's read, one the app never sends: its silence does not count toward reachability, where one
    unanswered request would mark the node at once. A light that answered its own Gets stays available and is not
    asked again."""
    hub = hub_of(init_integration)
    for _ in range(6):
        freezer.tick(3.1)
        async_fire_time_changed(hass)
        await settle(hass)
    assert [dst for _, dst, _ in fake_link.sent].count(
        ALL_NODES
    ) == BROADCASTS  # the refresh did complete
    # all three attempts of the temperature element's Get went unanswered
    assert (
        fake_link.sent.count(
            (OUR_ADDRESS, LIGHT_CTL_TEMPERATURE, M.light_ctl_temperature_get())
        )
        == 3
    )
    assert not hub.unreachable
    assert LIGHT_CTL not in hub.liveness.recheck
    assert (
        hass.states.get(entity_id(hass, "light", UID_LIGHT_CTL)).state
        != STATE_UNAVAILABLE
    )


async def test_unanswered_refresh_after_an_authenticated_beacon_is_reported(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A proxy whose beacon authenticated but which forwarded nothing decodable and answered nothing is one that dropped
    our filter request (stuck on its empty default whitelist): there is no other traffic to hear by construction."""
    fake_link.inject_beacon()
    for _ in range(6):
        freezer.tick(3.1)
        async_fire_time_changed(hass)
        await settle(hass)
    assert [dst for _, dst, _ in fake_link.sent].count(
        ALL_NODES
    ) == BROADCASTS  # the refresh did complete
    issue = find_issue(hass, ISSUE_PDUS_DROPPED)
    assert issue is not None
    assert issue.translation_placeholders == {
        "title": "JUNG HOME mesh test",
        "unicast": "0D00",
    }
    assert (
        "No JUNG device answered the state refresh although the link works"
        in caplog.text
    )

    # anything decodable that is not for us makes it the busy-mesh case again; the first reply to us clears it
    fake_link.inject(LIGHT_CTL, OUR_ADDRESS, ctl_status(0, 3000))
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None


async def test_connect_reads_are_not_repeated_soon_after_a_link_that_held(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review-4 R I-5: the scene actions, the fault registers and the current scenes are not read again by a link
    that follows a link of at least SHORT_LINK within CONNECT_STEP_FRESH of their last round; after a short link,
    or once the window has passed, they are. The clock, the location, the refresh and the energy poll go out on
    every link."""
    hub = hub_of(init_answered)
    tail = SCENE_GETS_PDUS + FAULT_GETS_PDUS + CURRENT_SCENE_PDUS

    async def link_after(lasted: float) -> list[tuple[int, int, bytes]]:
        hub.previous_link = coordinator.LinkEnd("the proxy disconnected", None, lasted)
        fake_link.sent.clear()
        await hub.refresh.after_connect()
        return fake_link.sent

    with caplog.at_level(logging.DEBUG, logger="custom_components.junghome_ble"):
        sent = await link_after(coordinator.SHORT_LINK)
    assert len(sent) == BROADCASTS + REFRESH_GETS + ENERGY_GETS
    assert sent[-ENERGY_GETS:] == COUNTER_GETS
    assert "faults read 0 s ago: not asked again on this link" in caplog.text
    # after a short link: everything again
    assert (await link_after(coordinator.SHORT_LINK - 1))[-CONNECT_TAIL:] == tail
    # past the window: again
    for name in hub.refresh.connect_steps_done:
        hub.refresh.connect_steps_done[name] -= refresh.CONNECT_STEP_FRESH
    assert (await link_after(coordinator.SHORT_LINK))[-CONNECT_TAIL:] == tail
    # a round the link cut short does not count: the next link reads again
    for name in hub.refresh.connect_steps_done:
        hub.refresh.connect_steps_done[name] -= refresh.CONNECT_STEP_FRESH
    with patch.object(hub.refresh, "_get_faults", AsyncMock(return_value=False)):
        await link_after(coordinator.SHORT_LINK)
    assert (await link_after(coordinator.SHORT_LINK))[-FAULT_GETS:] == FAULT_GETS_PDUS


async def test_a_stalled_store_delays_the_connect_sequence_without_ending_it(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """C2: store back-pressure (`SequenceExhausted`, a `ConnectionError`) is no lost link: the refused sends are
    retried every SEQ_STALL_RETRY seconds and the whole connect-time sequence still goes out — the refresh, Time
    Set and location, the energy poll, the scene actions and the fault survey."""
    hub = hub_of(init_answered)

    def unicasts() -> list[
        tuple[int, bytes]
    ]:  # the Time Set, broadcast, carries the time
        return sorted(
            (dst, pdu) for _src, dst, pdu in fake_link.sent if dst != ALL_NODES
        )

    expected = unicasts()  # the first link's own connect sequence
    fake_link.sent.clear()
    fast_sleep.clear()
    refused = stall_seq(hub, 3)
    await hub.refresh.after_connect()
    assert len(refused) == 3
    assert fast_sleep.count(coordinator.SEQ_STALL_RETRY) == 3
    assert unicasts() == expected
    assert [dst for _src, dst, _pdu in fake_link.sent].count(ALL_NODES) == BROADCASTS
    assert fake_link.sent[-FAULT_GETS - CURRENT_SCENE_GETS : -CURRENT_SCENE_GETS] == (
        FAULT_GETS_PDUS
    )


def scene_status(src: int, params: bytes) -> AccessMessage:
    raw = encode_opcode(V.SCENE_ACTION_SETUP_STATUS, M.JUNG_CID) + params
    return AccessMessage(
        src,
        OUR_ADDRESS,
        3,
        0,
        V.SCENE_ACTION_SETUP_STATUS,
        M.JUNG_CID,
        params,
        raw,
        "app0",
    )


def replies_honouring_match(
    answers: dict[bytes, list[AccessMessage]],
) -> Callable[..., Awaitable[AccessMessage]]:
    """A `ProxyClient.request` stand-in: each Get sees its statuses in order and, like the real client, takes the
    first one its `match` accepts (any, without one) — so a late duplicate lands on whichever Get is waiting."""

    async def request(
        dst: int,
        pdu: bytes,
        expect: int,
        match: Callable[[AccessMessage], bool] | None = None,
        **_kw: Any,
    ) -> AccessMessage:
        for msg in answers[pdu]:
            if match is None or match(msg):
                return msg
        raise TimeoutError

    return request


async def test_scene_action_read_tolerates_a_malformed_status_and_a_lost_link(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = hub_of(init_answered)
    assert hub.scene_actions == {1: {LIGHT_SWITCH: V.Action(V.ACTION_SWITCH, on=True)}}
    hub.scene_actions.clear()
    # one byte names no scene: it answers no Get (MSG-06's match), so the read ends as unanswered
    short = {V.scene_action_get(): [scene_status(LIGHT_SWITCH, b"\x00")]}
    with (
        caplog.at_level(logging.DEBUG),
        patch.object(hub.proxy, "request", replies_honouring_match(short)),
    ):
        await hub.refresh.get_scene_actions()
    assert hub.scene_actions == {}
    assert "did not answer its Scene Action Setup Get" in caplog.text
    with patch.object(hub, "chunked", side_effect=ConnectionError("gone")):
        await hub.refresh.get_scene_actions()  # logged, not raised
    assert "scene action read aborted" in caplog.text


async def test_a_late_scene_action_status_does_not_shift_onto_the_next_scene(
    hass: HomeAssistant, init_answered: MockConfigEntry
) -> None:
    """MSG-06: a duplicate answer to the Get for scene 8 that arrives while the Get for scene 9 waits must not be
    taken as scene 9's action; the status names its scene."""
    hub = hub_of(init_answered)
    on, off = V.Action(V.ACTION_SWITCH, on=True), V.Action(V.ACTION_SWITCH, on=False)
    answers = {
        V.scene_action_get(): [
            scene_status(LIGHT_DIMMER, bytes.fromhex("000008000900"))
        ],
        V.scene_action_get(8): [scene_status(LIGHT_DIMMER, b"\x08\x00" + on.encode())],
        V.scene_action_get(9): [
            scene_status(
                LIGHT_DIMMER, b"\x08\x00" + on.encode()
            ),  # scene 8's late duplicate
            scene_status(LIGHT_DIMMER, b"\x09\x00" + off.encode()),
        ],
    }
    with patch.object(hub.proxy, "request", replies_honouring_match(answers)):
        await hub.refresh._get_scene_actions_of(LIGHT_DIMMER)
    assert hub.scene_actions[8][LIGHT_DIMMER] == on
    assert hub.scene_actions[9][LIGHT_DIMMER] == off


async def test_scene_action_dropped_when_element_no_longer_lists_it(
    hass: HomeAssistant, init_answered: MockConfigEntry
) -> None:
    """HAC-12: the element's scene list replaces what was known of it, so a scene it no longer has an action for
    stops showing the old one; an unanswered list leaves everything as it was."""
    hub = hub_of(init_answered)
    hub.scene_actions = {
        7: {LIGHT_DIMMER: None},
        9: {LIGHT_DIMMER: None, LIGHT_SWITCH: None},
    }
    answers = {
        V.scene_action_get(): [
            scene_status(LIGHT_DIMMER, bytes.fromhex("000008000000"))
        ],
        V.scene_action_get(8): [scene_status(LIGHT_DIMMER, b"\x08\x00")],
    }
    with patch.object(hub.proxy, "request", replies_honouring_match(answers)):
        await hub.refresh._get_scene_actions_of(LIGHT_DIMMER)
    assert LIGHT_DIMMER not in hub.scene_actions.get(9, {})
    assert 7 not in hub.scene_actions  # its only member no longer lists it
    assert (
        LIGHT_SWITCH in hub.scene_actions[9]
    )  # another element's entry is not this list's to drop
    assert hub.scene_actions[8] == {LIGHT_DIMMER: None}

    with patch.object(
        hub.proxy, "request", replies_honouring_match({V.scene_action_get(): []})
    ):
        await hub.refresh._get_scene_actions_of(
            LIGHT_DIMMER
        )  # no list: nothing is dropped
    assert hub.scene_actions[8] == {LIGHT_DIMMER: None}


async def test_scene_actions_are_read_from_every_channel_of_a_member_node(
    hass: HomeAssistant, init_answered: MockConfigEntry
) -> None:
    """The export lists the element the Scene Store went to, the node's primary for both channels of a
    two-channel node: the second channel's action is read from its own element too, as the app asks each channel."""
    hub = hub_of(init_answered)
    hub.cdb.scenes.clear()
    hub.cdb.scenes[2] = [LIGHT_OUT1]
    hub.scene_actions.clear()
    on, off = V.Action(V.ACTION_SWITCH, on=True), V.Action(V.ACTION_SWITCH, on=False)
    answers = {
        LIGHT_OUT1: {
            V.scene_action_get(): [scene_status(LIGHT_OUT1, bytes.fromhex("00000200"))],
            V.scene_action_get(2): [
                scene_status(LIGHT_OUT1, b"\x02\x00" + on.encode())
            ],
        },
        LIGHT_OUT2: {
            V.scene_action_get(): [scene_status(LIGHT_OUT2, bytes.fromhex("00000200"))],
            V.scene_action_get(2): [
                scene_status(LIGHT_OUT2, b"\x02\x00" + off.encode())
            ],
        },
    }

    async def request(dst: int, pdu: bytes, expect: int, **kw: Any) -> AccessMessage:
        return await replies_honouring_match(answers[dst])(dst, pdu, expect, **kw)

    with patch.object(hub.proxy, "request", request):
        await hub.refresh.get_scene_actions()
    assert hub.scene_actions == {2: {LIGHT_OUT1: on, LIGHT_OUT2: off}}
