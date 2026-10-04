"""The hub's link manager (`hub/link.py`): proxy choice, connection, back-off, watchdog, keep-alive, Filter Status."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Generator
from datetime import timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from bleak.exc import BleakError
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.junghome_ble import coordinator
from custom_components.junghome_ble.const import (
    FILTER_STATUS_TIMEOUT,
    ISSUE_PDUS_DROPPED,
    KEEP_ALIVE_TIMEOUT,
    LINK_IDLE_TIMEOUT,
    SIGNAL_CONNECTION,
)
from custom_components.junghome_ble.hub import link as link_mod
from custom_components.junghome_ble.hub.link import LinkManager
from custom_components.junghome_ble.jhmesh import client as client_mod
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.client import ProxyClient
from custom_components.junghome_ble.jhmesh.pdu import ALL_NODES

from .conftest import (
    PROXY_ADDRESS,
    PROXY_NODE,
    FakeProxyLink,
    make_service_info,
    settle,
    setup_entry,
    wait_for_link,
    wait_until,
)
from .helpers import (
    LIGHT_CTL,
    LIGHT_DIMMER,
    LIGHT_OUT1,
    LIGHT_SWITCH,
    OUR_ADDRESS,
    SOCKET,
    UID_LIGHT_DIMMER,
    UID_LIGHT_SWITCH,
    UID_PROXY,
    entity_id,
    find_issue,
    onoff_status,
)
from .test_coordinator import (
    AFTER_ENERGY,
    BROADCASTS,
    CONNECT_TAIL,
    COUNTER_GETS,
    GROUP_SWITCH,
    REFRESH_FIRST_CHUNK,
    REFRESH_GETS,
    SECOND_PROXY,
    STATE_REPLIES,
    answer_gets,
    hub_of,
    quiet_mesh,
    stall_seq,
    stop_answering,
    tick,
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
from .test_coordinator import (
    refresh_gate as refresh_gate,  # noqa: PLC0414  # the fixture
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant


async def test_refresh_of_a_lost_link_is_cancelled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    refresh_gate: asyncio.Event,
) -> None:
    """A quick reconnect must not leave the previous link's refresh polling through the new one. Each link sets
    the nodes' clocks before its refresh (review-4 R I-5): a link lost before its refresh was through still did."""
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    hub = hub_of(mock_config_entry)
    first_link = BROADCASTS + REFRESH_FIRST_CHUNK
    assert (
        len(answering_link.sent) == first_link
    )  # Time Set and location, the first chunk; the refresh waits at the pause
    assert answering_link.sent[0][2][0] == M.TIME_SET
    old = hub.refresh.task
    assert old is not None
    assert not old.done()

    answering_link.drop_link()
    await wait_for_link(hass, mock_config_entry, connected=False)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    assert old.cancelled()
    assert answering_link.connect_count == 2
    assert hub.refresh.task is not None
    assert hub.refresh.task is not old
    assert (
        len(answering_link.sent) == 2 * first_link
    )  # the new link's broadcasts and first chunk, nothing more from the old refresh
    assert answering_link.sent[first_link][2][0] == M.TIME_SET

    refresh_gate.set()
    await settle(hass)
    second_chunk = REFRESH_GETS - REFRESH_FIRST_CHUNK
    assert (
        len(answering_link.sent) == 2 * first_link + second_chunk + AFTER_ENERGY
    )  # only the new refresh completed (its second chunk, energy poll, scene and fault reads; not the old
    # refresh's second chunk) — the first link lasted too short for its reads to count as fresh
    assert [dst for _, dst, _ in answering_link.sent].count(ALL_NODES) == 2 * BROADCASTS
    assert answering_link.sent[-AFTER_ENERGY:-CONNECT_TAIL] == COUNTER_GETS
    assert hub.refresh.task.done()


async def test_link_loss_waits_for_a_proxy_and_wakes_on_advertisement(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = hub_of(init_integration)
    light = entity_id(hass, "light", UID_LIGHT_SWITCH)
    proxy_sensor = entity_id(hass, "sensor", UID_PROXY)
    assert hass.states.get(light).state != STATE_UNAVAILABLE

    # the proxy goes away and no other node of the mesh is advertising
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert not hub.connected
    assert hub.proxy_address is None
    assert hub.proxy_node is None
    assert hub.connected_since is None
    assert (
        "Lost the connection to the JUNG mesh; reconnecting to another proxy node"
        in caplog.text
    )
    assert hass.states.get(light).state == STATE_UNAVAILABLE
    assert (
        hass.states.get(proxy_sensor).state == STATE_UNKNOWN
    )  # the diagnostic sensor itself stays available
    scans = mock_bluetooth_env["scans"]

    # the 30 s wait times out: the loop looks again and keeps waiting
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await settle(hass)
    assert mock_bluetooth_env["scans"] > scans
    assert not hub.connected

    # another node starts advertising: the advertisement callback wakes the loop immediately
    mock_bluetooth_env["infos"] = [make_service_info(network_id, address=SECOND_PROXY)]
    assert len(mock_bluetooth_env["callbacks"]) == 1
    mock_bluetooth_env["callbacks"][0](
        mock_bluetooth_env["infos"][0], BluetoothChange.ADVERTISEMENT
    )
    await wait_for_link(hass, init_integration)
    assert hub.connected
    assert hub.proxy_address == SECOND_PROXY
    assert hass.states.get(light).state != STATE_UNAVAILABLE
    assert (
        f"Connected to the JUNG mesh through proxy node {SECOND_PROXY}" in caplog.text
    )

    # advertisements while connected are ignored
    mock_bluetooth_env["callbacks"][0](
        mock_bluetooth_env["infos"][0], BluetoothChange.ADVERTISEMENT
    )
    assert not hub.link._link_lost.is_set()


async def test_another_networks_adverts_do_not_wake_the_unlinked_loop(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """Review-4 R4-8: while unlinked, only an advert of *this* network wakes the connection loop. Every other
    network's proxies in range woke it before, each wake-up a `visible_proxies` pass that found nothing."""
    hub = hub_of(init_integration)
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert not hub.connected
    with patch.object(
        hub.link, "visible_proxies", wraps=hub.link.visible_proxies
    ) as visible:
        for n in range(100):
            foreign = make_service_info(
                bytes([n + 1]) * 8, address=f"30:FB:10:00:01:{n:02X}"
            )
            mock_bluetooth_env["callbacks"][0](foreign, BluetoothChange.ADVERTISEMENT)
            await asyncio.sleep(0)
        await settle(hass)
        assert not hub.link._link_lost.is_set()
        assert visible.call_count == 0
        assert not hub.unknown_nodes  # not ours either
        # a node of ours: woken at once
        mock_bluetooth_env["infos"] = [
            make_service_info(network_id, address=SECOND_PROXY)
        ]
        mock_bluetooth_env["callbacks"][0](
            mock_bluetooth_env["infos"][0], BluetoothChange.ADVERTISEMENT
        )
        await wait_for_link(hass, init_integration)
        assert visible.call_count >= 1
    assert hub.proxy_address == SECOND_PROXY


async def test_reconnects_to_the_strongest_visible_proxy(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    hub = hub_of(init_integration)
    assert hub.proxy_address == PROXY_ADDRESS
    mock_bluetooth_env["infos"].append(
        make_service_info(network_id, address=SECOND_PROXY, rssi=-40)
    )
    fake_link.drop_link()
    await wait_for_link(hass, init_integration, connected=False)
    await wait_for_link(hass, init_integration)
    assert hub.proxy_address == SECOND_PROXY
    assert fake_link.connect_count == 2


async def test_connect_failures_back_off_and_rotate_proxies(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    fast_sleep: list[float],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_bluetooth_env["infos"].append(
        make_service_info(network_id, address=SECOND_PROXY, rssi=-70)
    )
    fake_link.connect_errors = [BleakError(f"attempt {i}") for i in range(7)]
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    hub = hub_of(mock_config_entry)

    assert hub.connected
    assert fake_link.connect_count == 8
    assert [d for d in fast_sleep if d != 0.3][:7] == [
        2.0,
        4.0,
        8.0,
        16.0,
        32.0,
        60.0,
        60.0,
    ]  # doubling, capped at a minute
    assert (
        f"connecting to {PROXY_ADDRESS} failed: attempt 0; retry in 2s" in caplog.text
    )
    assert (
        f"connecting to {SECOND_PROXY} failed: attempt 1; retry in 4s" in caplog.text
    )  # a failed node is skipped for a while
    # the first failure of a down period is a WARNING (a link that never comes up must show in the log), the
    # retries are DEBUG
    assert [
        record.levelno
        for record in caplog.records
        if record.getMessage().startswith("connecting to")
    ] == [logging.WARNING] + [logging.DEBUG] * 6
    caplog.clear()
    assert (
        hass.states.get(entity_id(hass, "sensor", UID_PROXY)).state
        == "Push-button 1-gang 0148"
    )

    # a link that lasted resets the back-off (a short one would not: `test_a_flapping_proxy_is_passed_over`)
    fast_sleep.clear()
    freezer.tick(link_mod.SHORT_LINK)
    fake_link.connect_errors = [BleakError("again")]
    fake_link.drop_link()
    await wait_for_link(hass, mock_config_entry, connected=False)
    await wait_for_link(hass, mock_config_entry)
    assert hub.connected
    assert fast_sleep[:2] == [1.0, 2.0]
    assert [
        record.levelno
        for record in caplog.records
        if record.getMessage().startswith("connecting to")
    ] == [logging.WARNING]  # a new down period: warned again


async def drop_and_reconnect(
    hass: HomeAssistant, entry: MockConfigEntry, link: FakeProxyLink
) -> None:
    """Lose the link right after it came up and wait for the next one."""
    link.drop_link()
    await wait_for_link(hass, entry, connected=False)
    await wait_for_link(hass, entry)


async def test_a_flapping_proxy_is_passed_over(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review-4 R4-1, the flap repro: a link that came up and was lost seconds later left no trace, and the next pass
    picked the same strongest proxy again — six drops, six reconnects to it, each restarting the connect-time
    refresh. Lost within SHORT_LINK, a link is a failed connection: the pause grows, and after SHORT_LINK_STREAK of
    them in a row the node is passed over for the next one in range."""
    hub = hub_of(init_integration)
    mock_bluetooth_env["infos"].append(
        make_service_info(network_id, address=SECOND_PROXY, rssi=-70)
    )  # weaker: not chosen while the first one works
    fast_sleep.clear()
    for _ in range(3):
        assert hub.proxy_address == PROXY_ADDRESS
        await drop_and_reconnect(hass, init_integration, fake_link)
    assert hub.proxy_address == SECOND_PROXY
    assert fake_link.connect_count == 4
    pauses = [d for d in fast_sleep if d in (1.0, 2.0, 4.0, 8.0, 16.0)]
    assert pauses == [2.0, 4.0, 8.0]  # doubling across short links, not 1 s each
    assert (
        f"Proxy node {PROXY_ADDRESS} lost 3 links in a row within "
        f"{link_mod.SHORT_LINK:.0f} s of connecting" in caplog.text
    )

    # a link that lasts is a working one: the back-off starts over
    freezer.tick(link_mod.SHORT_LINK)
    fast_sleep.clear()
    await drop_and_reconnect(hass, init_integration, fake_link)
    assert (
        hub.proxy_address == SECOND_PROXY
    )  # the flapping node still sits out its cooldown
    await drop_and_reconnect(hass, init_integration, fake_link)
    pauses = [d for d in fast_sleep if d in (1.0, 2.0, 4.0, 8.0, 16.0)]
    assert pauses == [1.0, 2.0]
    assert hub.link._short_links[SECOND_PROXY] == 1

    # the only node in range is used however often it flaps; its back-off keeps growing, up to the cap
    mock_bluetooth_env["infos"] = mock_bluetooth_env["infos"][1:]
    fast_sleep.clear()
    for _ in range(3):
        await drop_and_reconnect(hass, init_integration, fake_link)
        assert hub.proxy_address == SECOND_PROXY
    pauses = [d for d in fast_sleep if d in (1.0, 2.0, 4.0, 8.0, 16.0)]
    assert pauses == [4.0, 8.0, 16.0]
    assert (
        caplog.text.count("links in a row") == 2
    )  # once per node that reached the streak


async def test_a_link_lost_while_it_is_set_up_is_a_failed_connection(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review-4 R4-3: a link lost during `attach()`'s settle after the filter request was reported connected, then
    lost: every entity went available and unavailable again for nothing."""
    real_set_filter = ProxyClient.set_filter
    filters = 0

    async def set_filter_then_drop(self: ProxyClient, filter_type: int) -> None:
        nonlocal filters
        await real_set_filter(self, filter_type)
        filters += 1
        if filters == 1:
            fake_link.drop_link()

    states: dict[str, list[str]] = defaultdict(list)

    @callback
    def record(event: Event[EventStateChangedData]) -> None:
        if (new := event.data["new_state"]) is not None:
            states[event.data["entity_id"]].append(new.state)

    hass.bus.async_listen(EVENT_STATE_CHANGED, record)
    with patch.object(ProxyClient, "set_filter", set_filter_then_drop):
        await setup_entry(hass, mock_config_entry)
        await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    assert fake_link.connect_count == 2
    assert states[entity_id(hass, "light", UID_LIGHT_DIMMER)][-1] != STATE_UNAVAILABLE
    flapped = [  # shown available, then unavailable again
        eid
        for eid, seq in states.items()
        if any(a != STATE_UNAVAILABLE == b for a, b in pairwise(seq))
    ]
    assert flapped == []
    assert caplog.text.count("Connected to the JUNG mesh through proxy node") == 1
    assert (
        f"connecting to {PROXY_ADDRESS} failed: the link was lost while it was set up"
        in caplog.text
    )


KEEP_ALIVE_GET = M.generic_onoff_get()


# the elements the keep-alive asks, in order: the first Generic OnOff Server of each node other than the proxy node
# (0148), in export order; the proxy node's own load comes last (`_keep_alive_targets`)
KEEP_ALIVE_TARGETS = [
    LIGHT_CTL,
    SOCKET,
    LIGHT_DIMMER,
    LIGHT_OUT1,
    LIGHT_SWITCH,
]


def keep_alive_gets(link: FakeProxyLink) -> list[int]:
    """Destinations of the keep-alive Gets sent so far."""
    return [dst for src, dst, pdu in link.sent if pdu == KEEP_ALIVE_GET]


async def test_silent_proxy_is_dropped_after_the_idle_timeout(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A proxy that stops forwarding never disconnects by itself: after LINK_IDLE_TIMEOUT of silence the watchdog sends
    keep-alive Gets, and when those go unanswered too it drops the link and prefers another node."""
    hub = hub_of(init_answered)
    quiet_mesh(hub)
    mock_bluetooth_env["infos"].append(
        make_service_info(network_id, address=SECOND_PROXY, rssi=-70)
    )  # weaker, so not chosen yet
    fake_link.sent.clear()

    # anything the proxy forwards keeps the link: here a beacon just before the timeout
    freezer.tick(LINK_IDLE_TIMEOUT - 10)
    fake_link.inject_beacon()
    await tick(hass, freezer, 20)
    assert hub.connected
    assert hub.proxy_address == PROXY_ADDRESS
    assert fake_link.connect_count == 1
    assert keep_alive_gets(fake_link) == []

    # ... a decoded network PDU as well
    freezer.tick(LINK_IDLE_TIMEOUT - 20)
    fake_link.inject(LIGHT_SWITCH, GROUP_SWITCH, onoff_status(True))
    await tick(hass, freezer, 20)
    assert hub.connected
    assert fake_link.connect_count == 1
    assert keep_alive_gets(fake_link) == []

    # ... and the proxy's own Filter Status (it is the proxy talking to us)
    freezer.tick(LINK_IDLE_TIMEOUT - 20)
    fake_link._answer_filter_status(OUR_ADDRESS)
    await tick(hass, freezer, 20)
    assert hub.connected
    assert fake_link.connect_count == 1
    assert keep_alive_gets(fake_link) == []

    # then silence, and the proxy stops delivering: a keep-alive Get goes out, unanswered, then to two more elements
    stop_answering(fake_link)
    await tick(hass, freezer, LINK_IDLE_TIMEOUT)
    assert keep_alive_gets(fake_link) == KEEP_ALIVE_TARGETS[:1]
    assert hub.connected
    assert "dropping the link" not in caplog.text
    await tick(hass, freezer, KEEP_ALIVE_TIMEOUT + 0.1)
    assert keep_alive_gets(fake_link) == KEEP_ALIVE_TARGETS[:2]
    assert hub.connected
    await tick(hass, freezer, KEEP_ALIVE_TIMEOUT + 0.1)
    assert keep_alive_gets(fake_link) == KEEP_ALIVE_TARGETS[:3]
    assert hub.connected

    # the third one unanswered: the link is dropped, the silent node is put in cooldown and the other proxy is used
    # (whose connect-time refresh sends OnOff Gets of its own, so the log tells the keep-alives apart from here on)
    await tick(hass, freezer, KEEP_ALIVE_TIMEOUT + 0.1)
    assert "0300 did not answer the keep-alive Get" in caplog.text
    assert "0400 did not answer the keep-alive Get" not in caplog.text
    assert (
        f"Nothing received from the JUNG mesh through proxy node {PROXY_ADDRESS} for "
        f"{LINK_IDLE_TIMEOUT + 20 + 3 * (KEEP_ALIVE_TIMEOUT + 0.1):.0f} s and no answer to a keep-alive Get; "
        "dropping the link" in caplog.text
    )
    assert (
        "Lost the connection to the JUNG mesh; reconnecting to another proxy node"
        in caplog.text
    )
    assert fake_link.connect_count == 2
    assert hub.connected
    assert hub.proxy_address == SECOND_PROXY
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None


async def test_quiet_mesh_is_kept_by_the_keep_alive(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A mesh where nothing publishes for hours (no gateway, night) keeps its link: each silence ends in one answered
    keep-alive Get to a load on another node than the proxy, and nothing is dropped, however long it goes on."""
    hub = hub_of(init_answered)
    quiet_mesh(hub)
    fake_link.sent.clear()
    assert hub.states[LIGHT_CTL].on is False
    STATE_REPLIES[KEEP_ALIVE_GET] = onoff_status(True)
    try:
        for cycle in range(1, 4):
            await tick(hass, freezer, LINK_IDLE_TIMEOUT - 1)
            assert keep_alive_gets(fake_link) == [LIGHT_CTL] * (cycle - 1)  # not yet
            await tick(hass, freezer, 2)
            assert keep_alive_gets(fake_link) == [LIGHT_CTL] * cycle
            assert hub.connected
            assert hub.proxy_address == PROXY_ADDRESS
            assert fake_link.connect_count == 1
    finally:
        STATE_REPLIES[KEEP_ALIVE_GET] = onoff_status(False)
    assert "dropping the link" not in caplog.text
    assert "Lost the connection" not in caplog.text
    assert (
        hub.states[LIGHT_CTL].on is True
    )  # the answer is a real status and is used as one


async def test_keep_alive_moves_on_from_a_silent_element(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unplugged lamp does not cost the link: the next element is asked, and its answer keeps it."""
    hub = hub_of(init_answered)
    quiet_mesh(hub)
    fake_link.sent.clear()
    answering = fake_link.write_gatt_char

    async def all_but_the_ctl_light(
        char: str, data: bytes, response: bool | None = None
    ) -> None:
        before = len(fake_link.sent)
        await FakeProxyLink.write_gatt_char(fake_link, char, data, response)
        for src, dst, access in fake_link.sent[before:]:
            if dst != LIGHT_CTL and (reply := STATE_REPLIES.get(access)) is not None:
                fake_link.inject(dst, src, reply)

    fake_link.write_gatt_char = all_but_the_ctl_light  # type: ignore[method-assign]
    await tick(hass, freezer, LINK_IDLE_TIMEOUT + 1)
    assert keep_alive_gets(fake_link) == [LIGHT_CTL]
    await tick(hass, freezer, KEEP_ALIVE_TIMEOUT + 0.1)
    assert keep_alive_gets(fake_link) == [LIGHT_CTL, SOCKET]
    await tick(hass, freezer, KEEP_ALIVE_TIMEOUT + 0.1)
    assert keep_alive_gets(fake_link) == [LIGHT_CTL, SOCKET]  # answered: no third
    assert hub.connected
    assert fake_link.connect_count == 1
    assert "0232 did not answer the keep-alive Get" in caplog.text
    assert "dropping the link" not in caplog.text
    fake_link.write_gatt_char = answering  # type: ignore[method-assign]


async def test_keep_alive_waits_out_a_stalled_store(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C2: a keep-alive Get the sequence-number store holds back is sent once the store lets it, to the same
    element: the refusal says nothing about the link, which used to be dropped for it."""
    hub = hub_of(init_answered)
    quiet_mesh(hub)
    fake_link.sent.clear()
    fast_sleep.clear()
    refused = stall_seq(hub, 2)
    await tick(hass, freezer, LINK_IDLE_TIMEOUT + 1)
    assert len(refused) == 2
    assert fast_sleep.count(coordinator.SEQ_STALL_RETRY) == 2
    assert keep_alive_gets(fake_link) == [LIGHT_CTL]
    assert hub.connected
    assert fake_link.connect_count == 1
    assert "dropping the link" not in caplog.text


async def test_keep_alive_returns_under_real_exhaustion(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """Review-4 R4-2 (S4-5), the reviewer's repro: `_while_seq_stalls` caught `SequenceExhausted` — the base class —
    and retried it for as long as the link was up, so with the 24-bit space really used up the keep-alive looped
    thousands of times, never returned, and the watchdog never dropped a silent proxy. Exhaustion is no
    back-pressure: it is raised at once, and the keep-alive's verdict is what arrived meanwhile (nothing here)."""
    hub = hub_of(init_answered)
    quiet_mesh(hub)
    state = hub.state
    state.seq = client_mod.SEQ_TX_LIMIT + 1
    state._saved = None  # written at once: the store holds nothing back, the refusal is the space's own
    state.persist()
    await hass.async_block_till_done()
    fake_link.sent.clear()
    fast_sleep.clear()
    # untracked: `settle` would wait forever for a tracked task that never ends
    keep_alive = asyncio.get_running_loop().create_task(hub.link._keep_alive())
    await settle(hass)
    try:
        assert keep_alive.done(), "the keep-alive still retries an exhausted space"
        assert keep_alive.result() is False
    finally:
        keep_alive.cancel()
    assert fast_sleep.count(coordinator.SEQ_STALL_RETRY) == 0
    assert keep_alive_gets(fake_link) == []
    assert state.seq == client_mod.SEQ_TX_LIMIT + 1


async def test_watchdog_drops_a_silent_proxy_while_the_store_is_stalled(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review-4 R4-2: a store that never lands a write (an SD card remounted read-only) held the keep-alive in its
    retries for as long as the link was up. A held-back send now waits SEQ_STALL_DEADLINE at most; then it is no
    verdict either way, nothing arrived meanwhile, and the silent proxy is dropped like after an unanswered Get."""
    hub = hub_of(init_answered)
    quiet_mesh(hub)
    fake_link.sent.clear()
    fast_sleep.clear()
    retries = int(coordinator.SEQ_STALL_DEADLINE / coordinator.SEQ_STALL_RETRY)
    refused = stall_seq(
        hub, retries + 1
    )  # the first try and every retry; the next link sends again
    await tick(hass, freezer, LINK_IDLE_TIMEOUT + 1)
    assert len(refused) == retries + 1
    assert fast_sleep[:retries] == [coordinator.SEQ_STALL_RETRY] * retries
    assert "no answer to a keep-alive Get; dropping the link" in caplog.text
    assert fake_link.connect_count == 2
    assert hub.connected
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None


@pytest.fixture
def no_rechecks() -> Generator[None]:
    """Never ask a node that missed a state Get again: the keep-alive tests count every OnOff Get as a keep-alive."""
    with patch("custom_components.junghome_ble.hub.liveness.UNREACHABLE_RECHECK", 1e9):
        yield


async def test_keep_alive_counts_other_traffic_and_a_lost_link_is_not_silent(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    no_rechecks: None,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Anything the proxy forwards while the keep-alive is out keeps the link; a link that disconnects meanwhile was
    lost (the node stays eligible), and a write the transport refuses while nothing came back is a dead link."""
    hub = hub_of(init_answered)
    quiet_mesh(hub)
    mock_bluetooth_env["infos"].append(
        make_service_info(network_id, address=SECOND_PROXY, rssi=-70)
    )
    stop_answering(fake_link)
    fake_link.sent.clear()

    # an unrelated publication arrives while the keep-alive waits: the link lives on, no second element is asked
    await tick(hass, freezer, LINK_IDLE_TIMEOUT + 1)
    assert keep_alive_gets(fake_link) == [LIGHT_CTL]
    fake_link.inject(LIGHT_SWITCH, GROUP_SWITCH, onoff_status(True))
    await tick(hass, freezer, KEEP_ALIVE_TIMEOUT + 0.1)
    assert keep_alive_gets(fake_link) == [LIGHT_CTL]
    await tick(hass, freezer, KEEP_ALIVE_TIMEOUT + 0.1)
    assert keep_alive_gets(fake_link) == [LIGHT_CTL]
    assert hub.connected
    assert fake_link.connect_count == 1
    assert "dropping the link" not in caplog.text

    # the proxy disconnects while a keep-alive is out: reconnect, to the same node (no cooldown)
    fake_link.sent.clear()
    await tick(hass, freezer, LINK_IDLE_TIMEOUT + 1)
    assert keep_alive_gets(fake_link) == [LIGHT_CTL]
    answer_gets(fake_link)  # so the new link's refresh is through at once
    fake_link.drop_link()
    await settle(hass)
    await wait_for_link(hass, init_answered)
    # the new link's connect sequence is through: nothing left to refuse but the keep-alive
    await settle(hass)
    assert fake_link.connect_count == 2
    assert hub.proxy_address == PROXY_ADDRESS
    assert "dropping the link" not in caplog.text

    # a transport that refuses the keep-alive write, without a disconnect: dropped like a silent proxy
    quiet_mesh(hub)  # the new link armed the energy poll again
    stop_answering(fake_link)
    fake_link.sent.clear()
    plain = fake_link.write_gatt_char

    async def refuse_once(char: str, data: bytes, response: bool | None = None) -> None:
        fake_link.write_gatt_char = plain  # type: ignore[method-assign]
        raise BleakError("gatt write failed")

    fake_link.write_gatt_char = refuse_once  # type: ignore[method-assign]
    await tick(hass, freezer, LINK_IDLE_TIMEOUT + 1)
    assert "keep-alive not sent: proxy write failed: gatt write failed" in caplog.text
    assert "dropping the link" in caplog.text
    await wait_for_link(hass, init_answered)
    assert fake_link.connect_count == 3
    assert hub.proxy_address == SECOND_PROXY


async def test_keep_alive_targets_prefer_other_nodes(
    init_answered: MockConfigEntry,
) -> None:
    """One Generic OnOff Server per node is a target, those on other nodes than the proxy first."""
    hub = hub_of(init_answered)
    assert hub.proxy_node == PROXY_NODE
    assert hub.link._keep_alive_targets() == KEEP_ALIVE_TARGETS
    hub.proxy_node = SOCKET
    assert hub.link._keep_alive_targets() == [
        LIGHT_SWITCH,
        LIGHT_CTL,
        LIGHT_DIMMER,
        LIGHT_OUT1,
        SOCKET,
    ]
    hub.proxy_node = None  # not named yet: no node is "the proxy"
    assert hub.link._keep_alive_targets() == [
        LIGHT_SWITCH,
        LIGHT_CTL,
        SOCKET,
        LIGHT_DIMMER,
        LIGHT_OUT1,
    ]


async def test_keep_alive_targets_skip_nodes_that_cannot_answer(
    init_answered: MockConfigEntry,
) -> None:
    """Review-3 C4: an unreachable node, one never heard from (after those heard) — and only when nothing else is
    left, any node at all: a healthy quiet link was dropped for asking an unplugged node three times."""
    hub = hub_of(init_answered)
    hub.unreachable.add(LIGHT_CTL)
    del hub.last_heard[SOCKET]
    assert hub.link._keep_alive_targets() == [
        LIGHT_DIMMER,
        LIGHT_OUT1,
        SOCKET,
        LIGHT_SWITCH,
    ]
    hub.unreachable.update({LIGHT_SWITCH, SOCKET, LIGHT_DIMMER, LIGHT_OUT1})
    assert (
        hub.link._keep_alive_targets()
        == [  # nobody left: better any than none, in export order
            LIGHT_SWITCH,
            LIGHT_CTL,
            SOCKET,
            LIGHT_DIMMER,
            LIGHT_OUT1,
        ]
    )


async def test_keep_alive_without_a_target_drops_the_link(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A mesh with nothing to ask (no OnOff server at all) falls back to plain silence detection."""
    hub = hub_of(init_answered)
    fake_link.sent.clear()
    with patch.object(hub.link, "_keep_alive_targets", return_value=[]):
        assert await hub.link._keep_alive() is False
    assert fake_link.sent == []


async def test_no_repeated_connection_signal_while_no_proxy(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    """HAC-09: while no proxy is in range the loop passes every 30 s; a pass that changes nothing must not make
    every entity write its state again."""
    hub = hub_of(init_integration)
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await wait_until(hass, lambda: not hub.connected and hub.proxy_address is None)
    signalled: list[None] = []
    async_dispatcher_connect(
        hass,
        SIGNAL_CONNECTION.format(init_integration.entry_id),
        lambda: signalled.append(None),
    )
    for _ in range(3):  # three more passes of the no-proxy branch
        hub.link._link_lost.set()
        await settle(hass)
    assert signalled == []


async def test_hub_works_through_a_proxy_whose_node_is_not_0148(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    network_id: bytes,
) -> None:
    """Nothing at the HA level hard-codes the proxy's own node as 0148 (the fixture's usual `PROXY_NODE`, and the
    address every other test in this module happens to connect through): the connect, state refresh, receive
    and command path all work the same when the fake mesh is reached through node 0232's MAC instead."""
    mock_bluetooth_env["infos"] = [make_service_info(network_id, address=SECOND_PROXY)]
    answer_gets(fake_link)
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)

    hub = hub_of(mock_config_entry)
    assert hub.proxy_address == SECOND_PROXY
    assert (
        hub.proxy_node == LIGHT_CTL
    )  # 0x0232, not the fixture's usual PROXY_NODE (0148)
    assert (
        fake_link.proxy_node == LIGHT_CTL
    )  # the fake answers Filter Status as that node too

    light = entity_id(hass, "light", UID_LIGHT_SWITCH)
    assert (
        hass.states.get(light).state != STATE_UNAVAILABLE
    )  # the refresh reached a node other than the proxy's own, through the proxy's own

    fake_link.sent.clear()
    await hub.set_onoff(LIGHT_SWITCH, True)
    assert fake_link.sent[-1][:2] == (OUR_ADDRESS, LIGHT_SWITCH)

    fake_link.inject(LIGHT_SWITCH, GROUP_SWITCH, onoff_status(True))
    await settle(hass)
    assert hass.states.get(light).state == STATE_ON


async def test_a_stalled_store_at_connect_still_sets_the_proxy_filter(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Review-3 T2: the filter request is a link's first send, and the store holds it back right after the
    connect-time beacon moved the IV index. `attach()` took that for exhaustion and never sent the filter: the
    proxy stayed on its default whitelist (every group publication lost for the link) and the Filter Status
    watchdog raised a misleading "address in use" repair. The request now goes out once the store catches up."""
    hub = hub_of(init_answered)
    answered: list[int] = []
    answer = fake_link._answer_filter_status

    def record(src: int) -> None:
        answered.append(src)
        answer(src)

    fake_link._answer_filter_status = record  # type: ignore[method-assign]
    fake_link.drop_link()
    await wait_for_link(hass, init_answered, connected=False)
    refused = stall_seq(hub, 1)
    await wait_for_link(hass, init_answered)
    await wait_until(hass, lambda: answered, what="the proxy filter")
    assert refused == [1]
    assert answered == [OUR_ADDRESS]
    assert hub.proxy.proxy_addr == PROXY_NODE
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None


async def test_an_unexpected_error_does_not_end_the_connection_loop(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review-3 C6: an exception escaping one pass of the loop ended the link task for good — no link again
    until a reload. It is logged, the link released, and the loop goes on after a pause."""
    hub = hub_of(init_answered)
    calls = 0
    real = LinkManager.visible_proxies

    def flaky(self: LinkManager) -> list[Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("scanner exploded")
        return real(self)

    with patch.object(LinkManager, "visible_proxies", flaky):
        fake_link.drop_link()
        await wait_until(hass, lambda: calls >= 2, what="the next pass")
        await wait_for_link(hass, init_answered)
    assert "Unexpected error in the JUNG mesh connection loop" in caplog.text
    assert link_mod.CONNECT_BACKOFF_MAX in fast_sleep
    assert hub.connected


async def test_proxy_node_is_learned_from_the_filter_status(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """The proxy's Bluetooth address (its MAC) names the node at once; the Filter Status confirms it later.

    A proxy advertising from an address the export does not know (no JUNG MAC) shows its address until the
    status arrives.
    """
    pending: list[int] = []
    fake_link._answer_filter_status = pending.append  # type: ignore[method-assign]
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    hub = hub_of(mock_config_entry)
    proxy_sensor = entity_id(hass, "sensor", UID_PROXY)
    assert hub.connected
    assert hub.proxy_node == 0x0148  # from the MAC, before any Filter Status
    assert hub.proxy.proxy_addr is None
    assert hass.states.get(proxy_sensor).state == "Push-button 1-gang 0148"
    signals: list[None] = []
    async_dispatcher_connect(
        hass,
        SIGNAL_CONNECTION.format(mock_config_entry.entry_id),
        lambda: signals.append(None),
    )

    assert pending == [OUR_ADDRESS]
    FakeProxyLink._answer_filter_status(fake_link, OUR_ADDRESS)
    await hass.async_block_till_done()
    assert hub.proxy_node == 0x0148
    assert hub.proxy.proxy_addr == 0x0148
    assert hass.states.get(proxy_sensor).state == "Push-button 1-gang 0148"
    assert (
        len(signals) == 0
    )  # the status confirmed what the address had said: nothing to announce

    # an address the export cannot map: the sensor shows it until the status names the node
    hub.proxy_node = None
    hub.proxy_address = "AA:BB:CC:DD:EE:FF"
    hub.link._set_available(True)
    await hass.async_block_till_done()
    assert hass.states.get(proxy_sensor).state == "AA:BB:CC:DD:EE:FF"
    hub.proxy.proxy_addr = None
    signals.clear()
    FakeProxyLink._answer_filter_status(fake_link, OUR_ADDRESS)
    await hass.async_block_till_done()
    assert hub.proxy_node == 0x0148
    assert len(signals) == 1  # now the status told something new

    FakeProxyLink._answer_filter_status(
        fake_link, OUR_ADDRESS
    )  # a repeated status changes nothing
    await hass.async_block_till_done()
    assert len(signals) == 1

    # a lost link forgets the status; the next link is named by its address again until its own status arrives
    fake_link.drop_link()
    await settle(hass)
    assert hub.proxy.proxy_addr is None
    assert hub.proxy_node in (
        None,
        0x0148,
    )  # None until the reconnect, then from the MAC


@pytest.fixture
def silent_filter(fake_link: FakeProxyLink) -> list[int]:
    """A proxy that swallows the filter request (no Filter Status): the whitelist stays empty and nothing is forwarded."""
    pending: list[int] = []
    fake_link._answer_filter_status = pending.append  # type: ignore[method-assign]
    return pending


async def test_missing_filter_status_raises_the_pdus_dropped_repair(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    silent_filter: list[int],
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A proxy whose beacon authenticated but which never answered the filter request discards our PDUs (stale sequence
    number or address collision, seen on air): the repair is raised after FILTER_STATUS_TIMEOUT, without any other
    traffic, and cleared by the Filter Status when it does arrive."""
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    hub = hub_of(mock_config_entry)
    assert hub.connected
    assert hub.proxy_node == 0x0148  # named by its MAC; the Filter Status is still due
    assert hub.proxy.proxy_addr is None
    assert silent_filter == [OUR_ADDRESS]
    assert hub.link.unsub_filter_watch is not None
    fake_link.inject_beacon()

    await tick(hass, freezer, FILTER_STATUS_TIMEOUT - 1)
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None
    await tick(hass, freezer, 1.1)
    assert hub.link.unsub_filter_watch is None
    issue = find_issue(hass, ISSUE_PDUS_DROPPED)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_key == ISSUE_PDUS_DROPPED
    assert issue.translation_placeholders == {
        "title": "JUNG HOME mesh test",
        "unicast": "0D00",
    }
    warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING"
        and "did not answer the proxy filter request" in r.message
    ]
    assert len(warnings) == 1
    assert (
        f"Proxy node {PROXY_ADDRESS} authenticated the mesh beacon but did not answer the proxy filter request "
        f"within {FILTER_STATUS_TIMEOUT:.0f} s: it discards our messages (stale sequence number, or address 0D00 "
        "is used by another client)"
    ) == warnings[0].message

    # the Filter Status (the proxy accepted a PDU of ours after all) clears it
    FakeProxyLink._answer_filter_status(fake_link, OUR_ADDRESS)
    await hass.async_block_till_done()
    assert hub.proxy_node == PROXY_NODE
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None


async def test_missing_filter_status_without_a_beacon_is_not_reported(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    no_connect_beacon: None,
    silent_filter: list[int],
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without an authenticated beacon a missing Filter Status proves nothing about our PDUs (a dead proxy or the wrong
    keys have their own detection), and a link that went away before the deadline is not judged either."""
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    hub = hub_of(mock_config_entry)
    await tick(hass, freezer, FILTER_STATUS_TIMEOUT + 0.1)
    assert hub.link.unsub_filter_watch is None
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None
    assert "did not answer the proxy filter request" not in caplog.text

    # a beacon that arrives later does not fire the watchdog retroactively; the next link gets a fresh one
    fake_link.inject_beacon()
    await tick(hass, freezer, FILTER_STATUS_TIMEOUT + 0.1)
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None
    fake_link.drop_link()
    await wait_for_link(hass, mock_config_entry, connected=False)
    await wait_for_link(hass, mock_config_entry)
    assert fake_link.connect_count == 2
    assert hub.link.unsub_filter_watch is not None
    assert (
        hub.beacon_authenticated is False
    )  # the previous link's beacon does not count for this one
    fake_link.inject_beacon()
    mock_bluetooth_env["infos"] = []  # nothing to reconnect to
    fake_link.drop_link()  # a lost link takes its watchdog with it
    await settle(hass)
    assert not hub.connected
    assert hub.link.unsub_filter_watch is None
    await tick(hass, freezer, FILTER_STATUS_TIMEOUT + 0.1)
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None


async def test_filter_status_watchdog_is_not_armed_when_the_proxy_answers(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The fake proxy answers the filter request during `attach()`: nothing to wait for."""
    hub = hub_of(init_integration)
    assert hub.proxy_node == PROXY_NODE
    assert hub.link.unsub_filter_watch is None


async def test_filter_status_watchdog_stops_with_the_hub(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    silent_filter: list[int],
    fast_sleep: list[float],
) -> None:
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    hub = hub_of(mock_config_entry)
    assert hub.link.unsub_filter_watch is not None
    fake_link.inject_beacon()
    await hub.async_stop()
    assert hub.link.unsub_filter_watch is None
    await tick(hass, freezer, FILTER_STATUS_TIMEOUT + 0.1)
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None
