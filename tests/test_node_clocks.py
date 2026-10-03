"""The nodes' clocks, zone offsets and stored locations (review-4 F4-8, `node_clocks.py`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, PropertyMock, patch

from homeassistant.const import STATE_UNKNOWN
from homeassistant.util import dt as dt_util

from custom_components.junghome_ble import const, repairs
from custom_components.junghome_ble.const import ISSUE_NODE_CLOCK_WRONG
from custom_components.junghome_ble.coordinator import JungHomeHub, issue_id
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.pdu import ALL_NODES, encode_opcode
from custom_components.junghome_ble.node_clocks import zone_sent
from custom_components.junghome_ble.schedules import Slot, build_schedule, scheduler

from .conftest import FakeProxyLink, settle
from .helpers import (
    GATEWAY,
    LIGHT_CTL,
    LIGHT_DIMMER,
    LIGHT_SWITCH,
    NODE_LIGHT_SWITCH,
    OUR_ADDRESS,
    SOCKET,
    entity_id,
    find_issue,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

ACTUATOR, ACTUATOR_CHANNEL_2 = (
    0x0400,
    0x0401,
)  # the 2-channel actuator: a Time Server on both elements
TIME_SERVERS = {
    LIGHT_SWITCH,
    LIGHT_CTL,
    SOCKET,
    LIGHT_DIMMER,
    ACTUATOR,
}  # one per mains node, the gateway has none


def hub_of(entry: MockConfigEntry) -> JungHomeHub:
    return entry.runtime_data


def hhmm(minutes: int) -> str:
    sign = "-" if minutes < 0 else "+"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def time_status(offset: float = 0.0, zone: int | None = None) -> bytes:
    """A Time Status `offset` seconds off Home Assistant's clock, with the zone offset Time Set sends (or `zone`)."""
    minutes = zone_sent(dt_util.now()) if zone is None else zone
    when = dt_util.now() + timedelta(seconds=offset)
    return (
        encode_opcode(M.TIME_STATUS)
        + M.time_set(when, zone_offset=timedelta(minutes=minutes))[1:]
    )


def zone_status(zone: int) -> bytes:
    """A Time Zone Status with `zone` minutes in force and no change ahead."""
    quarters = zone // 15 + 64
    return encode_opcode(M.TIME_ZONE_STATUS) + bytes([quarters, quarters]) + bytes(5)


def location_status(
    latitude: float | None, longitude: float | None, altitude: int | None = 0
) -> bytes:
    """A Generic Location Global Status: the Set's fields under the Status opcode."""
    return (
        encode_opcode(M.GEN_LOCATION_GLOBAL_STATUS)
        + M.generic_location_global_set(latitude, longitude, altitude)[1:]
    )


def clock_replies(
    hass: HomeAssistant, offset: float = 0.0, *, silent: frozenset[int] = frozenset()
) -> Callable[[int, bytes], bytes | None]:
    """The nodes' Time and Location Servers: every Get answered (`offset` seconds off), except by `silent` elements."""

    def reply(dst: int, access: bytes) -> bytes | None:
        if dst in silent:
            return None
        if access == M.time_get():
            return time_status(offset)
        if access == M.time_zone_get():
            return zone_status(zone_sent(dt_util.now()))
        if access == M.generic_location_global_get():
            return location_status(hass.config.latitude, hass.config.longitude)
        return None

    return reply


def sensor_value(hass: HomeAssistant) -> str:
    state = hass.states.get(
        entity_id(hass, "sensor", f"{NODE_LIGHT_SWITCH.lower()}-clock_offset")
    )
    assert state is not None
    return state.state


def placeholders(hass: HomeAssistant) -> dict[str, str] | None:
    issue = find_issue(hass, ISSUE_NODE_CLOCK_WRONG)
    return None if issue is None else issue.translation_placeholders


def wrong(entry: MockConfigEntry, unicast: int, reason: str) -> str:
    """The repair's text for one node: its name, its address, what is wrong."""
    node = hub_of(entry).cdb.node_by_addr(unicast)
    assert node is not None
    return f"{node.name} {unicast:04X} ({reason})"


async def test_a_time_status_gives_the_offset_and_a_wrong_clock_raises_the_repair(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Any Time Status counts: the answer to the Time Set broadcast, to a Time Get, or a published one."""
    hub = hub_of(init_integration)
    assert sensor_value(hass) == STATE_UNKNOWN
    assert placeholders(hass) is None
    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, time_status(0.5))
    await settle(hass)
    assert sensor_value(hass) == "0.5"
    assert placeholders(hass) is None  # half a second: right

    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, time_status(75))
    await settle(hass)
    assert sensor_value(hass) == "75.0"
    assert placeholders(hass) == {
        "title": init_integration.title,
        "devices": wrong(init_integration, LIGHT_SWITCH, "+75 s"),
    }
    issue = find_issue(hass, ISSUE_NODE_CLOCK_WRONG)
    assert issue is not None
    assert issue.is_fixable
    assert issue.data == {"entry_id": init_integration.entry_id}

    # a node without a time
    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, encode_opcode(M.TIME_STATUS) + bytes(5))
    await settle(hass)
    assert sensor_value(hass) == STATE_UNKNOWN
    assert placeholders(hass) == {
        "title": init_integration.title,
        "devices": wrong(init_integration, LIGHT_SWITCH, "no time"),
    }

    # the right time clears it; the second element of a node counts for the node
    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, time_status(-1))
    fake_link.inject(ACTUATOR_CHANNEL_2, OUR_ADDRESS, time_status(-90))
    await settle(hass)
    assert sensor_value(hass) == "-1.0"
    assert placeholders(hass) == {
        "title": init_integration.title,
        "devices": wrong(init_integration, ACTUATOR, "-90 s"),
    }
    assert set(hub.clocks.clocks) == {LIGHT_SWITCH, ACTUATOR}
    fake_link.inject(ACTUATOR, OUR_ADDRESS, time_status())
    await settle(hass)
    assert placeholders(hass) is None


async def test_a_zone_offset_other_than_the_one_sent_raises_the_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The zone offset of a Time Status, then the one of a Time Zone Status, is compared with what Time Set carries."""
    hub = hub_of(init_integration)
    ours = zone_sent(dt_util.now())
    fake_link.inject(SOCKET, OUR_ADDRESS, time_status(zone=ours + 60))
    await settle(hass)
    assert placeholders(hass) == {
        "title": init_integration.title,
        "devices": wrong(init_integration, SOCKET, f"UTC{hhmm(ours + 60)}"),
    }
    fake_link.inject(SOCKET, OUR_ADDRESS, zone_status(ours))
    await settle(hass)
    assert placeholders(hass) is None
    fake_link.inject(SOCKET, OUR_ADDRESS, zone_status(ours - 120))
    await settle(hass)
    assert placeholders(hass) == {
        "title": init_integration.title,
        "devices": wrong(init_integration, SOCKET, f"UTC{hhmm(ours - 120)}"),
    }
    # a Time Status without a time leaves the zone as it was
    fake_link.inject(SOCKET, OUR_ADDRESS, encode_opcode(M.TIME_STATUS) + bytes(5))
    await settle(hass)
    assert hub.clocks.clocks[SOCKET].zone == ours - 120
    diagnostics = hub.clocks.diagnostics(SOCKET)
    assert diagnostics is not None
    assert diagnostics["has_time"] is False
    assert diagnostics["location"] is None  # not asked yet


def test_the_zone_sent_falls_back_to_utc_like_time_set() -> None:
    assert (
        zone_sent(datetime.now(tz=timezone(timedelta(hours=-3, minutes=-30)))) == -210
    )
    assert zone_sent(datetime.now(tz=timezone(timedelta(minutes=7)))) == 0
    assert zone_sent(datetime.now(tz=UTC)) == 0


async def test_malformed_and_foreign_statuses_are_ignored(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = hub_of(init_integration)
    caplog.set_level("DEBUG", "custom_components.junghome_ble.node_clocks")
    for status in (
        encode_opcode(M.TIME_STATUS) + bytes([1, 2, 3]),
        encode_opcode(M.TIME_ZONE_STATUS) + b"\x40",
        encode_opcode(M.GEN_LOCATION_GLOBAL_STATUS) + b"\x01",
    ):
        fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, status)
    for status in (time_status(), zone_status(0), location_status(None, None)):
        fake_link.inject(0x0999, OUR_ADDRESS, status)  # no node of the export
    await settle(hass)
    assert hub.clocks.clocks == {}
    assert "0148: not a Time Status" in caplog.text
    assert "0148: not a Time Zone Status" in caplog.text
    assert "0148: Generic Location Global Status: truncated PDU" in caplog.text
    assert hub.clocks.diagnostics(LIGHT_SWITCH) is None
    assert hub.clocks.offset(LIGHT_SWITCH) is None
    assert hub.clocks.wrong(LIGHT_SWITCH) is None


async def test_the_location_is_compared_with_home_never_shown(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    hub = hub_of(init_integration)
    fake_link.inject(LIGHT_DIMMER, OUR_ADDRESS, location_status(None, None))
    await settle(hass)
    assert hub.clocks.diagnostics(LIGHT_DIMMER) == {
        "read": None,
        "offset": None,
        "has_time": None,
        "zone_offset": None,
        "zone_expected": None,
        "location": "not configured",
        "wrong": None,
    }
    home = (hass.config.latitude, hass.config.longitude)
    for latitude, longitude, where in (
        (home[0] + 0.005, home[1] - 0.005, "home"),
        (home[0] + 0.5, home[1], "elsewhere"),
        (home[0], home[1] + 0.5, "elsewhere"),
    ):
        fake_link.inject(
            LIGHT_DIMMER, OUR_ADDRESS, location_status(latitude, longitude)
        )
        await settle(hass)
        diagnostics = hub.clocks.diagnostics(LIGHT_DIMMER)
        assert diagnostics is not None
        assert diagnostics["location"] == where
    assert placeholders(hass) is None  # the location is no part of the repair


async def test_nodes_known_to_have_no_schedules_raise_no_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A node counts while one of its loads hosts a JH Scheduler whose slots are not known to be empty."""
    hub = hub_of(init_integration)
    schedules = scheduler(hass, hub)
    schedules.slots[LIGHT_SWITCH] = []  # read: no slot used
    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, time_status(300))
    await settle(hass)
    assert hub.clocks.wrong(LIGHT_SWITCH) == "+300 s"
    assert placeholders(hass) is None
    schedules.slots[LIGHT_SWITCH] = [Slot(build_schedule(0, {"trigger": "sunset"}))]
    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, time_status(300))
    await settle(hass)
    assert placeholders(hass) == {
        "title": init_integration.title,
        "devices": wrong(init_integration, LIGHT_SWITCH, "+300 s"),
    }


async def test_the_daily_read_asks_every_mains_node_in_chunks(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Time Get and Time Zone Get to each Time Server, the Location Get where a Location Server is; battery nodes
    sleep and are not asked."""
    hub = hub_of(init_integration)
    switch = hub.cdb.element(LIGHT_SWITCH)
    dimmer = hub.cdb.node_by_addr(LIGHT_DIMMER)
    assert switch is not None
    assert dimmer is not None
    switch.models.append(
        "100E"
    )  # on every node on air; not in the fixture's compositions
    dimmer.pid = 0x0005  # a battery product now
    fake_link.app_reply = clock_replies(hass, 2.0)
    fake_link.sent.clear()
    with (
        patch("custom_components.junghome_ble.node_clocks.REFRESH_CHUNK", 2),
        patch("custom_components.junghome_ble.node_clocks.CLOCK_READ_PAUSE", 0),
    ):
        assert await hub.clocks.read_all()
    asked = {(dst, pdu) for _, dst, pdu in fake_link.sent}
    servers = TIME_SERVERS - {LIGHT_DIMMER}
    assert asked == (
        {(addr, M.time_get()) for addr in servers}
        | {(addr, M.time_zone_get()) for addr in servers}
        | {(LIGHT_SWITCH, M.generic_location_global_get())}
    )
    assert set(hub.clocks.clocks) == servers
    assert hub.clocks.offset(SOCKET) == 2.0
    diagnostics = hub.clocks.diagnostics(LIGHT_SWITCH)
    assert diagnostics is not None
    assert diagnostics["read"] is not None
    assert {k: v for k, v in diagnostics.items() if k != "read"} == {
        "offset": 2.0,
        "has_time": True,
        "zone_offset": zone_sent(dt_util.now()),
        "zone_expected": zone_sent(dt_util.now()),
        "location": "home",
        "wrong": None,
    }
    # a node without a Time Server (the gateway) has nothing to be asked
    gateway = hub.cdb.node_by_addr(GATEWAY)
    assert gateway is not None
    assert await hub.clocks._read(gateway)


async def test_a_silent_node_is_asked_nothing_more_and_a_lost_link_stops_the_read(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = hub_of(init_integration)
    caplog.set_level("DEBUG", "custom_components.junghome_ble.node_clocks")
    fake_link.app_reply = clock_replies(hass, silent=frozenset({SOCKET}))
    fake_link.sent.clear()
    with patch.object(const, "PROPERTY_READ_TIMEOUT", 0.01):
        assert not await hub.clocks.read_all()
    assert {pdu for _, dst, pdu in fake_link.sent if dst == SOCKET} == {M.time_get()}
    assert "0172 did not answer Time Get" in caplog.text
    assert set(hub.clocks.clocks) == TIME_SERVERS - {SOCKET}

    with patch.object(
        hub.proxy, "request", AsyncMock(side_effect=ConnectionError("gone"))
    ):
        assert not await hub.clocks.read_all()
    assert "clock read aborted: the link went away" in caplog.text

    hub.clocks._reading = True  # a read already runs: not a second one
    fake_link.sent.clear()
    assert not await hub.clocks.read_all()
    assert not fake_link.sent


async def test_the_repair_sends_the_time_and_asks_again(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, time_status(-600))
    await settle(hass)
    issue = find_issue(hass, ISSUE_NODE_CLOCK_WRONG)
    assert issue is not None
    flow = await repairs.async_create_fix_flow(hass, issue.issue_id, issue.data)
    assert isinstance(flow, repairs.SendTimeFlow)
    flow.hass, flow.issue_id = hass, issue.issue_id
    form = await flow.async_step_init()
    assert form["type"] == "form"
    assert form["step_id"] == "confirm"
    assert form["description_placeholders"] == issue.translation_placeholders

    # no link: nothing to send it over
    with patch.object(
        JungHomeHub, "connected", new_callable=PropertyMock, return_value=False
    ):
        result = await flow.async_step_confirm({})
    assert result["type"] == "abort"
    assert result["reason"] == "not_connected"

    fake_link.app_reply = clock_replies(hass)
    fake_link.sent.clear()
    result = await flow.async_step_confirm({})
    await settle(hass)
    assert result["type"] == "create_entry"
    sent = [(dst, pdu) for _, dst, pdu in fake_link.sent]
    assert sent[0][0] == ALL_NODES
    assert M.decode_opcode(sent[0][1])[0] == M.TIME_SET
    assert (LIGHT_SWITCH, M.time_get()) in sent
    assert {dst for dst, _ in sent[1:]} == {LIGHT_SWITCH}  # only the node it named
    assert find_issue(hass, ISSUE_NODE_CLOCK_WRONG) is None

    # the entry gone meanwhile
    gone = await repairs.async_create_fix_flow(
        hass,
        issue_id(init_integration, ISSUE_NODE_CLOCK_WRONG),
        {"entry_id": "gone"},
    )
    gone.hass = hass
    gone.issue_id = issue_id(init_integration, ISSUE_NODE_CLOCK_WRONG)
    form = await gone.async_step_init()
    assert form["description_placeholders"] is None  # the issue went too
    result = await gone.async_step_confirm({})
    assert result["type"] == "abort"
    assert result["reason"] == "entry_gone"


async def test_the_repair_goes_with_the_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, time_status(120))
    await settle(hass)
    assert find_issue(hass, ISSUE_NODE_CLOCK_WRONG) is not None
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert find_issue(hass, ISSUE_NODE_CLOCK_WRONG) is None
