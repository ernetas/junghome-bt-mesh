"""The hub's liveness component (`hub/liveness.py`): per-node reachability (the app's rule) and heartbeats."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import ANY, AsyncMock, PropertyMock, patch

from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.junghome_ble.const import (
    CONF_HEARTBEATS_PUBLISHING,
    HEARTBEAT_PERIOD_LOG,
    HEARTBEAT_RECONFIGURE_INTERVAL,
    HEARTBEAT_REPROBE_INTERVAL,
    OPTION_CLICK_DELAY,
    OPTION_HEARTBEATS,
    REQUEST_ATTEMPTS,
    UNREACHABLE_RECHECK,
    UNREACHABLE_REPROBE,
)
from custom_components.junghome_ble.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.client import ProxyClient
from custom_components.junghome_ble.jhmesh.pdu import (
    decode_opcode,
    encode_opcode,
)

from .conftest import (
    FakeProxyLink,
    settle,
    wait_for_link,
)
from .helpers import (
    BUTTON_DIMMER,
    LIGHT_CTL,
    LIGHT_DIMMER,
    LIGHT_SWITCH,
    OUR_ADDRESS,
    SOCKET,
    UID_LIGHT_DIMMER,
    UID_LIGHT_SWITCH,
    entity_id,
    onoff_status,
)
from .test_coordinator import (
    GROUP_SWITCH,
    answer_gets,
    hub_of,
    quiet_mesh,
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

if TYPE_CHECKING:
    import pytest
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

    from custom_components.junghome_ble.coordinator import JungHomeHub


async def test_one_unanswered_request_marks_the_node_unreachable_and_any_message_revives_it(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The app's rule: a request that exhausted its attempts sets the failed-message counter to the mark at once."""
    hub = hub_of(init_answered)
    light = entity_id(hass, "light", UID_LIGHT_DIMMER)
    other = entity_id(hass, "light", UID_LIGHT_SWITCH)
    assert hass.states.get(light).state != STATE_UNAVAILABLE
    freezer.tick(1)  # the refresh's answers came before this request was sent
    with patch.object(hub, "async_refresh_element", AsyncMock()) as refresh:
        hub.liveness.missed_answer(LIGHT_DIMMER, "dimmer", time.monotonic())
        await settle(hass)
        assert hass.states.get(light).state == STATE_UNAVAILABLE
        assert (
            hass.states.get(other).state != STATE_UNAVAILABLE
        )  # only that node's entities
        assert "did not answer a request (3 attempts in 9 s)" in caplog.text
        # review-3 C3: nothing else would ever ask it again while the link lasts — a slow, quiet re-probe does
        await tick(hass, freezer, UNREACHABLE_RECHECK + 1)
        refresh.assert_not_awaited()
        await tick(hass, freezer, UNREACHABLE_REPROBE)
        refresh.assert_awaited_once_with(LIGHT_DIMMER, "dimmer", quiet=True)
        # the re-probe's own silence: no second warning, the next re-probe is scheduled
        hub.liveness.missed_answer(LIGHT_DIMMER, "dimmer", time.monotonic(), full=False)
        assert caplog.text.count("did not answer a request") == 1
        assert list(hub.liveness.recheck) == [LIGHT_DIMMER]

    # any message from any of its elements: reachable again, nothing left to re-ask
    fake_link.inject(BUTTON_DIMMER, GROUP_SWITCH, onoff_status(True))
    await settle(hass)
    assert hass.states.get(light).state != STATE_UNAVAILABLE
    assert "is reachable again" in caplog.text
    assert not hub.liveness.recheck


async def test_a_node_heard_from_while_asked_is_busy_not_unreachable(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Any message from the node after the request went out: it is there (the app completes a pending request on the
    node's User Property Status and resets the counter on any status). Asked again later with the full budget."""
    hub = hub_of(init_answered)
    freezer.tick(1)
    asked = time.monotonic()
    freezer.tick(1)
    # a User Property Status the node publishes (the message the app takes for any pending request's answer)
    fake_link.inject(
        BUTTON_DIMMER,
        GROUP_SWITCH,
        M.vendor_property_status("user", 0x5001, b"\x00\x00"),
    )
    await settle(hass)
    with patch.object(hub, "async_refresh_element", AsyncMock()) as refresh:
        hub.liveness.missed_answer(LIGHT_DIMMER, "dimmer", asked)
        assert not hub.unreachable
        await tick(hass, freezer, UNREACHABLE_RECHECK + 1)
        refresh.assert_awaited_once_with(LIGHT_DIMMER, "dimmer", quiet=False)
        hub.liveness.missed_answer(0x0999, "switch", asked)  # an address no node owns
    assert not hub.unreachable


async def test_a_short_probe_is_no_verdict_and_battery_nodes_are_never_marked(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
) -> None:
    """A one-attempt keep-alive miss is re-asked with the full budget; a sleeping battery node is not "unreachable"."""
    hub = hub_of(init_answered)
    freezer.tick(1)
    with patch.object(hub, "async_refresh_element", AsyncMock()) as refresh:
        hub.liveness.missed_answer(LIGHT_DIMMER, "switch", time.monotonic(), full=False)
        assert not hub.unreachable
        await tick(hass, freezer, UNREACHABLE_RECHECK + 1)
        refresh.assert_awaited_once_with(LIGHT_DIMMER, "switch", quiet=False)
    # the fixture network has no battery node: the socket stands in for a wall transmitter (PID 0x0005)
    socket = hub.cdb.node_by_addr(SOCKET)
    assert socket is not None
    with patch.object(socket, "pid", 0x0005):
        hub.liveness.missed_answer(SOCKET, "switch", time.monotonic())
    assert not hub.unreachable
    assert SOCKET not in hub.liveness.recheck


async def test_a_new_link_drops_pending_re_asks_but_keeps_an_unreachable_node(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    freezer: FrozenDateTimeFactory,
) -> None:
    hub = hub_of(init_answered)
    freezer.tick(1)
    with patch.object(hub, "async_refresh_element", AsyncMock()) as refresh:
        hub.liveness.missed_answer(LIGHT_CTL, "ctl", time.monotonic())
        hub.liveness.missed_answer(LIGHT_DIMMER, "dimmer", time.monotonic(), full=False)
        assert hub.unreachable == {LIGHT_CTL}
        fake_link.drop_link()
        await settle(hass)
        await tick(
            hass, freezer, UNREACHABLE_RECHECK + 1
        )  # the link is down: nobody to ask
        refresh.assert_not_awaited()
    await wait_for_link(hass, init_answered)
    await settle(hass)
    # the new link's refresh was answered by everyone, 0232 too: it is back
    assert not hub.unreachable
    assert not hub.liveness.recheck


async def test_a_re_ask_due_while_the_link_is_down_is_dropped(
    hass: HomeAssistant, init_answered: MockConfigEntry
) -> None:
    hub = hub_of(init_answered)
    hub.liveness.missed_answer(LIGHT_DIMMER, "dimmer", time.monotonic(), full=False)
    hub.liveness.recheck[LIGHT_DIMMER]()  # cancel the timer: it is run by hand below
    with (
        patch.object(
            type(hub), "connected", new_callable=PropertyMock, return_value=False
        ),
        patch.object(hub, "async_refresh_element", AsyncMock()) as refresh,
    ):
        hub.liveness._recheck_node(
            LIGHT_DIMMER, LIGHT_DIMMER, "dimmer", dt_util.utcnow()
        )
        await settle(hass)
    refresh.assert_not_awaited()
    assert not hub.liveness.recheck


async def test_unanswered_state_get_and_keep_alive_count(
    hass: HomeAssistant, init_answered: MockConfigEntry
) -> None:
    """A state Get with its full budget is a verdict; its quiet re-probe and a keep-alive are one attempt, not one."""
    hub = hub_of(init_answered)
    with (
        patch.object(
            hub.proxy, "request", AsyncMock(side_effect=TimeoutError)
        ) as request,
        patch.object(hub.liveness, "missed_answer") as missed,
    ):
        await hub.async_refresh_element(LIGHT_DIMMER, "dimmer")
        assert request.call_args.kwargs["retries"] == REQUEST_ATTEMPTS
        missed.assert_called_once_with(LIGHT_DIMMER, "dimmer", ANY, full=True)
        missed.reset_mock()
        await hub.async_refresh_element(LIGHT_DIMMER, "dimmer", quiet=True)
        missed.assert_called_once_with(LIGHT_DIMMER, "dimmer", ANY, full=False)
        missed.reset_mock()
        await hub.link._keep_alive()
    assert missed.call_args_list[0].args[1] == "switch"
    assert missed.call_args_list[0].kwargs == {"full": False}


async def test_stop_cancels_a_pending_re_ask(
    hass: HomeAssistant, init_answered: MockConfigEntry
) -> None:
    hub = hub_of(init_answered)
    hub.liveness.missed_answer(LIGHT_DIMMER, "dimmer", time.monotonic(), full=False)
    assert hub.liveness.recheck
    await hass.config_entries.async_unload(init_answered.entry_id)
    await hass.async_block_till_done()
    assert not hub.liveness.recheck


HEARTBEAT_NODES = [
    0x00DC,
    0x0148,
    0x0232,
    0x0172,
    0x0300,
    0x0400,
]  # every provisioned mains node, CDB order


HEARTBEAT_SET = C.heartbeat_publication_set(OUR_ADDRESS, HEARTBEAT_PERIOD_LOG, ttl=5)


HEARTBEAT_OFF = C.heartbeat_publication_set(0x0000, C.HEARTBEAT_PERIOD_OFF, count_log=0)


ALL_PUBLISHING = [f"{node:04X}" for node in sorted(HEARTBEAT_NODES)]


HEARTBEAT_TIMEOUT = (
    64 * 3.5
)  # HEARTBEAT_PERIOD_LOG 7 = 64 s, HEARTBEAT_MISSED_BEATS 3 + half a period


async def start_with_heartbeats(
    hass: HomeAssistant, entry: MockConfigEntry, link: FakeProxyLink
) -> JungHomeHub:
    """Set the entry up with the heartbeat option on and a mesh that answers the refresh."""
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={OPTION_HEARTBEATS: True})
    answer_gets(link)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_link(hass, entry)
    await settle(hass)
    return hub_of(entry)


async def test_heartbeats_off_by_default(
    hass: HomeAssistant, init_answered: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Without the option nothing is configured on the nodes and every node counts as alive."""
    hub = hub_of(init_answered)
    assert hub.heartbeats_enabled is False
    assert fake_link.config_sent == []
    assert hub.lifecycle.timer("heartbeats") is None
    assert hub.node_alive(LIGHT_SWITCH)
    fake_link.inject_heartbeat(LIGHT_SWITCH, OUR_ADDRESS)
    await hass.async_block_till_done()
    assert hub.heartbeats[LIGHT_SWITCH].hops == 1  # still recorded, for the diagnostics
    assert hub.node_alive(LIGHT_SWITCH)
    diagnostics = await async_get_config_entry_diagnostics(hass, init_answered)
    assert diagnostics["heartbeats"] == {"enabled": False}


async def test_heartbeats_are_configured_once_per_reconfigure_interval(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """With the option on, the connect-time refresh ends with a Heartbeat Publication Set (device key) to every mains
    node; a new link inside HEARTBEAT_RECONFIGURE_INTERVAL does not repeat it, one after it does."""
    hub = await start_with_heartbeats(hass, mock_config_entry, fake_link)
    assert hub.heartbeats_enabled
    assert fake_link.config_sent == [
        (OUR_ADDRESS, node, HEARTBEAT_SET) for node in HEARTBEAT_NODES
    ]
    assert hub.liveness.configured_at is not None
    assert set(hub.liveness._alive_deadline) == set(HEARTBEAT_NODES)
    assert hub.node_alive(LIGHT_SWITCH)
    assert hub.heartbeat_timeout == HEARTBEAT_TIMEOUT

    fake_link.config_sent.clear()
    fake_link.drop_link()
    await settle(hass)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    assert fake_link.config_sent == []  # the publications persist in the nodes

    # a link that stays up renews the publications when the interval is over (the periodic check does it); the
    # fake nodes never beat, so the dead-node probing (its own test below) is kept out of the way here
    with (
        patch("custom_components.junghome_ble.hub.link.LINK_IDLE_TIMEOUT", 10 * 3600.0),
        patch.object(hub.liveness, "_reprobe_dead", AsyncMock()),
    ):
        await tick(hass, freezer, HEARTBEAT_RECONFIGURE_INTERVAL - 60)
        assert fake_link.config_sent == []
        await tick(hass, freezer, 90)
        assert [pdu for _s, _n, pdu in fake_link.config_sent] == [HEARTBEAT_SET] * len(
            HEARTBEAT_NODES
        )
        assert hub.lifecycle.task("heartbeats") is not None
        assert hub.lifecycle.task("heartbeats").done()
        fake_link.config_sent.clear()
        # ... and so does a new link after the interval
        await tick(hass, freezer, HEARTBEAT_RECONFIGURE_INTERVAL + 1)
    fake_link.drop_link()
    await settle(hass)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    assert [pdu for _s, _n, pdu in fake_link.config_sent] == [HEARTBEAT_SET] * len(
        HEARTBEAT_NODES
    )


async def test_silent_node_goes_unavailable_until_heard_again(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A node that neither beats nor says anything for the timeout has its entities marked unavailable; a Heartbeat
    (or any message) brings them back."""
    hub = await start_with_heartbeats(hass, mock_config_entry, fake_link)
    light = entity_id(hass, "light", UID_LIGHT_SWITCH)
    dimmer = entity_id(hass, "light", UID_LIGHT_DIMMER)
    assert hass.states.get(light).state != STATE_UNAVAILABLE

    # a few beats from the dimmer's node keep it alive; the switch's node says nothing (the fake mesh would answer
    # the dead-node probe for it, which is the probe's own test, so that is kept out of the way here)
    with (
        patch("custom_components.junghome_ble.hub.link.LINK_IDLE_TIMEOUT", 10 * 3600.0),
        patch.object(hub.liveness, "_reprobe_dead", AsyncMock()),
    ):
        for _ in range(3):
            await tick(hass, freezer, 64)
            fake_link.inject_heartbeat(LIGHT_DIMMER, OUR_ADDRESS, init_ttl=5, ttl=3)
            await hass.async_block_till_done()
        assert hub.node_alive(LIGHT_SWITCH)  # 192 s: not yet
        await tick(hass, freezer, 64)
        assert not hub.node_alive(LIGHT_SWITCH)
        assert hub.node_alive(LIGHT_DIMMER)
        assert hass.states.get(light).state == STATE_UNAVAILABLE
        assert hass.states.get(dimmer).state != STATE_UNAVAILABLE
        assert "Push-button 1-gang has not been heard from" in caplog.text
        assert hub.heartbeats[LIGHT_DIMMER].hops == 2

        # a Heartbeat revives it ...
        fake_link.inject_heartbeat(LIGHT_SWITCH, OUR_ADDRESS)
        await hass.async_block_till_done()
        assert hub.node_alive(LIGHT_SWITCH)
        assert hass.states.get(light).state != STATE_UNAVAILABLE
        assert "Push-button 1-gang is back" in caplog.text

        # ... and so does any other message from the node, once it has died again
        await tick(hass, freezer, HEARTBEAT_TIMEOUT + 30)
        assert not hub.node_alive(LIGHT_SWITCH)
        fake_link.inject(LIGHT_SWITCH, GROUP_SWITCH, onoff_status(True))
        await hass.async_block_till_done()
        assert hub.node_alive(LIGHT_SWITCH)
        assert hass.states.get(light).state == "on"

    # the diagnostics show the per-node picture
    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    beats = diagnostics["heartbeats"]
    assert beats["enabled"] is True
    assert beats["configured"] is True
    assert beats["timeout"] == HEARTBEAT_TIMEOUT
    assert beats["nodes"]["0148"]["alive"] is True
    assert beats["nodes"]["0148"]["hops"] == 1
    assert beats["nodes"]["0300"]["hops"] == 2  # the dimmer's node
    assert beats["nodes"]["0172"] == {
        "alive": True,
        "last_beat_age": None,
        "hops": None,
    }
    assert isinstance(beats["nodes"]["0300"]["last_beat_age"], int)

    # a heartbeat or message from an address that is no node of ours is ignored
    fake_link.inject_heartbeat(0x0BBB, OUR_ADDRESS)
    hub.liveness.mark_alive(0x0BBB)
    await hass.async_block_till_done()
    assert 0x0BBB not in hub.heartbeats


async def test_dead_node_is_asked_for_heartbeats_again_and_a_rebooted_one_comes_back(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A node that rebooted lost its heartbeat publication (seen live): while it counts as dead it is
    sent the Heartbeat Publication Set again every HEARTBEAT_REPROBE_INTERVAL, and the answer revives it."""
    hub = await start_with_heartbeats(hass, mock_config_entry, fake_link)
    light = entity_id(hass, "light", UID_LIGHT_SWITCH)
    configure = C.heartbeat_publication_set(OUR_ADDRESS, HEARTBEAT_PERIOD_LOG, ttl=5)

    def sets_to(node: int) -> int:
        return sum(
            1
            for _s, dst, pdu in fake_link.config_sent
            if dst == node and pdu == configure
        )

    async def check(seconds: float) -> None:
        """One periodic check after `seconds`, then enough clock for an unanswered probe to time out."""
        await tick(hass, freezer, seconds)
        for _ in range(2):
            freezer.tick(3.1)
            async_fire_time_changed(hass)
            await settle(hass)

    with patch(
        "custom_components.junghome_ble.hub.link.LINK_IDLE_TIMEOUT", 10 * 3600.0
    ):
        assert sets_to(LIGHT_SWITCH) == 1  # the connect-time configuration
        fake_link.answer_config = False  # the node is really gone: silence
        await check(HEARTBEAT_TIMEOUT + 30)
        assert not hub.node_alive(LIGHT_SWITCH)
        assert hass.states.get(light).state == STATE_UNAVAILABLE
        assert (
            sets_to(LIGHT_SWITCH) == 2
        )  # asked again in the check that marked it dead
        await check(HEARTBEAT_REPROBE_INTERVAL / 2)
        assert sets_to(LIGHT_SWITCH) == 2  # not before the interval
        await check(HEARTBEAT_REPROBE_INTERVAL)
        assert sets_to(LIGHT_SWITCH) == 3
        assert not hub.node_alive(LIGHT_SWITCH)
        # the node is back (rebooted, publication empty): the next probe is answered -> alive and beating again
        fake_link.answer_config = True
        await check(HEARTBEAT_REPROBE_INTERVAL)
        assert sets_to(LIGHT_SWITCH) == 4
        assert hub.node_alive(LIGHT_SWITCH)
        assert hass.states.get(light).state != STATE_UNAVAILABLE
        assert "Push-button 1-gang is back" in caplog.text
        # alive nodes are left alone
        fake_link.inject_heartbeat(LIGHT_SWITCH, OUR_ADDRESS)
        await check(HEARTBEAT_REPROBE_INTERVAL)
        assert sets_to(LIGHT_SWITCH) == 4
        # a refusal is still an answer from the node (alive), but the refusal is logged
        fake_link.config_refuse = 0x0A
        await check(HEARTBEAT_TIMEOUT + 30)
        assert "refused the heartbeat reconfigure" in caplog.text
        assert hub.node_alive(LIGHT_SWITCH)  # it spoke; only heartbeats stay off
        # no link: no probing of a dead node
        fake_link.config_refuse = 0
        fake_link.answer_config = False
        await check(HEARTBEAT_TIMEOUT + 30)
        assert not hub.node_alive(LIGHT_SWITCH)
        before = len(fake_link.config_sent)
        with patch.object(type(hub), "connected", PropertyMock(return_value=False)):
            await check(HEARTBEAT_REPROBE_INTERVAL)
        assert len(fake_link.config_sent) == before
    # a link lost during a probe ends it quietly
    with (
        patch.object(
            ProxyClient, "request_config", side_effect=ConnectionError("gone")
        ),
        caplog.at_level(logging.DEBUG),
    ):
        await hub.liveness._reprobe_dead(hub.heartbeat_nodes[:1])
    assert "heartbeat reprobe aborted" in caplog.text


async def test_node_whose_beats_stopped_is_reconfigured_while_its_traffic_keeps_it_alive(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """A metering socket after a power cut: it publishes readings every minute, so it never counts as
    dead, but its heartbeat publication is gone. Once its last beat is a whole timeout old it is sent the
    configuration again — without ever becoming unavailable — and left alone once it beats again."""
    hub = await start_with_heartbeats(hass, mock_config_entry, fake_link)
    light = entity_id(hass, "light", UID_LIGHT_SWITCH)
    configure = C.heartbeat_publication_set(OUR_ADDRESS, HEARTBEAT_PERIOD_LOG, ttl=5)

    def sets_to(node: int) -> int:
        return sum(
            1
            for _s, dst, pdu in fake_link.config_sent
            if dst == node and pdu == configure
        )

    with patch(
        "custom_components.junghome_ble.hub.link.LINK_IDLE_TIMEOUT", 10 * 3600.0
    ):
        assert sets_to(LIGHT_SWITCH) == 1
        fake_link.inject_heartbeat(
            LIGHT_SWITCH, OUR_ADDRESS
        )  # it beat once after the configuration
        await hass.async_block_till_done()
        await tick(hass, freezer, HEARTBEAT_TIMEOUT - 30)
        assert sets_to(LIGHT_SWITCH) == 1  # the beat is not a timeout old yet
        fake_link.inject(
            LIGHT_SWITCH, OUR_ADDRESS, onoff_status(True)
        )  # other traffic: alive
        await hass.async_block_till_done()
        await tick(hass, freezer, 60)
        assert hub.node_alive(LIGHT_SWITCH)
        assert hass.states.get(light).state != STATE_UNAVAILABLE
        assert sets_to(LIGHT_SWITCH) == 2  # beats stopped: configured again
        await tick(hass, freezer, HEARTBEAT_REPROBE_INTERVAL / 2)
        assert sets_to(LIGHT_SWITCH) == 2  # not before the interval
        fake_link.inject_heartbeat(LIGHT_SWITCH, OUR_ADDRESS)  # beating again
        await hass.async_block_till_done()
        await tick(hass, freezer, HEARTBEAT_REPROBE_INTERVAL)
        assert sets_to(LIGHT_SWITCH) == 2
        # a beat from before the current configuration does not count as "stopped" (the configuration itself
        # is what makes it beat again; the renewal handles a node that never does)
        hub.liveness.configured_at = time.monotonic() + 1
        fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, onoff_status(False))
        await hass.async_block_till_done()
        await tick(
            hass, freezer, HEARTBEAT_TIMEOUT - 30
        )  # beat age well past the timeout, node still alive
        assert hub.node_alive(LIGHT_SWITCH)
        assert sets_to(LIGHT_SWITCH) == 2


async def test_nodes_are_not_marked_dead_while_the_link_is_down(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Silence during a proxy outage is the link's fault: nothing is marked dead (the entities are unavailable
    anyway, and one warning per node would only repeat the link's own); the reconnect gives every node a fresh
    timeout, after which real silence counts again."""
    hub = await start_with_heartbeats(hass, mock_config_entry, fake_link)
    assert set(hub.liveness._alive_deadline) == set(HEARTBEAT_NODES)
    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert not hub.connected
    with (
        patch("custom_components.junghome_ble.hub.link.LINK_IDLE_TIMEOUT", 10 * 3600.0),
        patch.object(hub.liveness, "_reprobe_dead", AsyncMock()),
    ):
        await tick(hass, freezer, 2 * HEARTBEAT_TIMEOUT)
        assert hub.liveness._dead_nodes == set()
        assert "marking it unavailable" not in caplog.text

        mock_bluetooth_env["infos"] = infos
        mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
        await wait_for_link(hass, mock_config_entry)
        await settle(hass)
        assert hub.connected
        await tick(hass, freezer, HEARTBEAT_TIMEOUT - 30)
        assert hub.liveness._dead_nodes == set()  # a full timeout from the reconnect
        await tick(hass, freezer, 60)
        assert hub.liveness._dead_nodes == set(
            HEARTBEAT_NODES
        )  # the fake nodes never beat


async def test_heartbeat_configuration_tolerates_refusals_and_silence(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A node refusing the Set is logged and never counted dead; one not answering costs one attempt, no retry."""
    fake_link.config_refuse = 0x0B  # "Cannot Set"
    hub = await start_with_heartbeats(hass, mock_config_entry, fake_link)
    assert "Gateway refused the heartbeat configure" in caplog.text
    assert hub.liveness._alive_deadline == {}
    with patch(
        "custom_components.junghome_ble.hub.link.LINK_IDLE_TIMEOUT", 10 * 3600.0
    ):
        await tick(hass, freezer, HEARTBEAT_TIMEOUT + 60)
    assert hub.node_alive(LIGHT_SWITCH)  # nothing was promised, nothing is missed

    # unanswered: the Set goes out once per node, the round completes
    fake_link.config_refuse = 0
    fake_link.answer_config = False
    fake_link.config_sent.clear()
    hub.liveness.configured_at = None
    caplog.clear()
    task = hass.async_create_background_task(hub.liveness.configure_heartbeats(), "hb")
    for _ in range(4):
        freezer.tick(3.1)
        async_fire_time_changed(hass)
        await settle(hass)
    assert task.done()
    assert [n for _s, n, _p in fake_link.config_sent] == HEARTBEAT_NODES
    assert "00DC did not answer the heartbeat configure" in caplog.text
    assert hub.liveness.configured_at is not None
    # a node that did not answer the Set is exactly what liveness is for (off, out of range): it gets a deadline
    # like the others, counts as dead when it passes and is asked again; an answer to that brings it back
    assert set(hub.liveness._alive_deadline) == set(HEARTBEAT_NODES)
    assert hub.node_alive(LIGHT_SWITCH)
    quiet_mesh(hub)  # the energy poll's answers would keep the socket's node alive
    with (
        patch("custom_components.junghome_ble.hub.link.LINK_IDLE_TIMEOUT", 10 * 3600.0),
        patch.object(hub.liveness, "_reprobe_dead", AsyncMock()) as reprobe,
    ):
        await tick(hass, freezer, HEARTBEAT_TIMEOUT + 60)
        assert not hub.node_alive(LIGHT_SWITCH)
        assert hub.liveness._dead_nodes == set(HEARTBEAT_NODES)
        assert (
            "has not been heard from for 224 s: marking it unavailable" in caplog.text
        )
        assert reprobe.await_count == 1
        assert [n.unicast for n in reprobe.await_args.args[0]] == HEARTBEAT_NODES
    fake_link.inject_heartbeat(LIGHT_SWITCH, OUR_ADDRESS)
    await hass.async_block_till_done()
    assert hub.node_alive(LIGHT_SWITCH)
    assert "is back (heard from it again)" in caplog.text

    # a link lost in the middle ends the round quietly
    hub.liveness.configured_at = None
    with patch.object(
        ProxyClient, "request_config", side_effect=ConnectionError("gone")
    ):
        await hub.liveness.configure_heartbeats()
        await hub.liveness.async_disable_heartbeats()
    assert hub.liveness.configured_at is None


async def test_switching_heartbeats_off_tells_the_nodes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """Turning the option off sends a disabling Heartbeat Publication Set to every node before the entry reloads —
    one round and one reload, although recording the confirmation updates the entry (and its listener) again."""
    hub = await start_with_heartbeats(hass, mock_config_entry, fake_link)
    fake_link.config_sent.clear()
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        await hass.config_entries.options.async_configure(
            result["flow_id"], {OPTION_CLICK_DELAY: False, OPTION_HEARTBEATS: False}
        )
        await hass.async_block_till_done()
        await settle(hass)
    assert [s for s in fake_link.config_sent if s[2] == HEARTBEAT_OFF] == [
        (OUR_ADDRESS, node, HEARTBEAT_OFF) for node in HEARTBEAT_NODES
    ]
    reload.assert_awaited_once_with(mock_config_entry.entry_id)
    assert mock_config_entry.runtime_data is not hub
    assert mock_config_entry.runtime_data.heartbeats_enabled is False


async def test_heartbeats_disabled_after_reconnect_when_disable_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """HAC-11: the nodes publish their heartbeats indefinitely; switching the option off while the link is down
    cannot tell them, so the entry remembers they still publish and the next link after the reload does."""
    await start_with_heartbeats(hass, mock_config_entry, fake_link)
    assert mock_config_entry.data[CONF_HEARTBEATS_PUBLISHING] == ALL_PUBLISHING
    fake_link.connect_errors = [ConnectionError("busy")] * 5
    fake_link.drop_link()
    await hass.async_block_till_done()
    fake_link.config_sent.clear()
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {OPTION_CLICK_DELAY: False, OPTION_HEARTBEATS: False}
    )
    await hass.async_block_till_done()
    assert fake_link.config_sent == []  # no link: the disable round could not go out
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    assert [
        (dst, pdu) for _src, dst, pdu in fake_link.config_sent if pdu == HEARTBEAT_OFF
    ] == [(node, HEARTBEAT_OFF) for node in HEARTBEAT_NODES]
    assert mock_config_entry.data[CONF_HEARTBEATS_PUBLISHING] == []
    fake_link.config_sent.clear()
    fake_link.drop_link()  # done: the next link does not ask again
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    assert fake_link.config_sent == []


async def test_heartbeats_still_publishing_until_every_node_confirmed(
    hass: HomeAssistant, init_answered: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """HAC-11: a node refusing the disabling Set stays on the list, and the next link asks it — and only it —
    again (the others confirmed; one node gone for good no longer costs a whole round per link)."""
    hub = hub_of(init_answered)
    hass.config_entries.async_update_entry(
        init_answered,
        data={**init_answered.data, CONF_HEARTBEATS_PUBLISHING: ALL_PUBLISHING},
    )
    stubborn = HEARTBEAT_NODES[1]

    def reply(node: int, access: bytes) -> bytes | None:
        op, _cid, params = decode_opcode(access)
        if op != C.CONFIG_HEARTBEAT_PUBLICATION_SET:
            return None
        refuse = 1 if node == stubborn else 0
        return (
            encode_opcode(C.CONFIG_HEARTBEAT_PUBLICATION_STATUS)
            + bytes([refuse])
            + params
        )

    fake_link.config_reply = reply
    await hub.refresh.after_connect()
    assert init_answered.data[CONF_HEARTBEATS_PUBLISHING] == [f"{stubborn:04X}"]
    fake_link.config_sent.clear()
    fake_link.config_reply = None
    await hub.refresh.after_connect()
    assert [
        dst for _src, dst, pdu in fake_link.config_sent if pdu == HEARTBEAT_OFF
    ] == [stubborn]
    assert init_answered.data[CONF_HEARTBEATS_PUBLISHING] == []


async def test_heartbeats_recorded_as_one_flag_are_every_nodes_to_tell(
    hass: HomeAssistant, init_answered: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """An entry written by an earlier 0.3.0 build holds `True` for "still publishing": every heartbeat node is
    asked, and the list replaces the flag."""
    hub = hub_of(init_answered)
    hass.config_entries.async_update_entry(
        init_answered, data={**init_answered.data, CONF_HEARTBEATS_PUBLISHING: True}
    )
    assert hub.liveness.heartbeats_publishing == set(HEARTBEAT_NODES)
    await hub.refresh.after_connect()
    assert sorted(
        dst for _src, dst, pdu in fake_link.config_sent if pdu == HEARTBEAT_OFF
    ) == sorted(HEARTBEAT_NODES)
    assert init_answered.data[CONF_HEARTBEATS_PUBLISHING] == []


async def test_heartbeat_disable_counts_only_a_node_that_stopped(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C3: a success status to the disabling Set is not enough — one that still shows a publication (destination,
    period and count set) leaves the node on the list of those to tell again."""
    hub = hub_of(init_answered)
    hass.config_entries.async_update_entry(
        init_answered,
        data={**init_answered.data, CONF_HEARTBEATS_PUBLISHING: ALL_PUBLISHING},
    )
    still = HEARTBEAT_NODES[2]
    publishing = C.heartbeat_publication_set(OUR_ADDRESS, HEARTBEAT_PERIOD_LOG, ttl=5)

    def reply(node: int, access: bytes) -> bytes | None:
        op, _cid, params = decode_opcode(access)
        if op != C.CONFIG_HEARTBEAT_PUBLICATION_SET:
            return None
        if node == still:
            params = decode_opcode(publishing)[2]
        return encode_opcode(C.CONFIG_HEARTBEAT_PUBLICATION_STATUS) + b"\x00" + params

    fake_link.config_reply = reply
    await hub.liveness.async_disable_heartbeats()
    assert init_answered.data[CONF_HEARTBEATS_PUBLISHING] == [f"{still:04X}"]
    assert (
        f"answered the heartbeat disable but still publishes to {OUR_ADDRESS:04X}"
        in caplog.text
    )


async def test_disable_round_stops_the_heartbeat_work_first(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """C3: switching the option off stops the heartbeat check timer and cancels a renewal or reprobe round in
    flight before the disable round goes out: their configure Sets would switch nodes that confirmed the disable
    back on, and nothing would tell them again."""
    hub = await start_with_heartbeats(hass, mock_config_entry, fake_link)
    assert hub.lifecycle.timer("heartbeats") is not None
    release = asyncio.Event()
    hub.lifecycle.set_task("heartbeats", hass.async_create_task(release.wait()))
    hub.liveness.reprobe_task = hass.async_create_task(release.wait())
    renewal, reprobe = hub.lifecycle.task("heartbeats"), hub.liveness.reprobe_task
    seen: list[tuple[object, bool, bool]] = []
    disable = hub.liveness.async_disable_heartbeats

    async def recording() -> None:
        seen.append(
            (
                hub.lifecycle.timer("heartbeats"),
                renewal.cancelled(),
                reprobe.cancelled(),
            )
        )
        await disable()

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    with patch.object(hub.liveness, "async_disable_heartbeats", recording):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {OPTION_CLICK_DELAY: False, OPTION_HEARTBEATS: False}
        )
        await hass.async_block_till_done()
        await settle(hass)
    assert seen == [(None, True, True)]
    assert mock_config_entry.runtime_data is not hub
