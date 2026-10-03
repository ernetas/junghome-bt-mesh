"""The hub: connection loop, state refresh, decoding of mesh messages, button gestures, beacons."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import re
import shutil
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Generator, Mapping
from datetime import UTC, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import ANY, AsyncMock, PropertyMock, patch

import pytest
from bleak.exc import BleakError
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    EVENT_STATE_REPORTED,
    STATE_OFF,
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
    async_fire_time_changed_exact,
)

from custom_components.junghome_ble import coordinator
from custom_components.junghome_ble.const import (
    BUTTON_REPEAT_WINDOW,
    CONF_CDB_PATH,
    CONF_GATEWAY_FINGERPRINT,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_PIN_SOURCE,
    CONF_GATEWAY_SYNCED,
    CONF_GATEWAY_TOKEN,
    CONF_HEARTBEATS_PUBLISHING,
    CONF_METADATA_DIR,
    CONF_SOURCE,
    CONF_UNICAST,
    DOMAIN,
    DOUBLE_CLICK_WINDOW,
    ENERGY_POLL_INTERVAL,
    EXPORT_STALE_THRESHOLD,
    FILTER_STATUS_TIMEOUT,
    HEARTBEAT_PERIOD_LOG,
    HEARTBEAT_RECONFIGURE_INTERVAL,
    HEARTBEAT_REPROBE_INTERVAL,
    ISSUE_EXPORT_STALE,
    ISSUE_GATEWAY_CERTIFICATE,
    ISSUE_KEY_REFRESH,
    ISSUE_PDUS_DROPPED,
    ISSUE_UNKNOWN_NODES,
    ISSUE_UNKNOWN_NODES_GATEWAY,
    KEEP_ALIVE_TIMEOUT,
    LINK_IDLE_TIMEOUT,
    OPTION_CLICK_DELAY,
    OPTION_HEARTBEATS,
    PIN_FROM_MESH,
    PIN_FROM_USER,
    REQUEST_ATTEMPTS,
    SIGNAL_CONNECTION,
    SIGNAL_UPDATE,
    TID_REPEAT_WINDOW,
    TIME_SET_INTERVAL,
    UNREACHABLE_RECHECK,
    UNREACHABLE_REPROBE,
)
from custom_components.junghome_ble.coordinator import (
    GENERIC_LEVEL_OPCODES,
    SIG_PROPERTY_STATUS_OPCODES,
    STATUS_HANDLERS,
    JungHomeHub,
    entry_lock,
    register_status_handler,
)
from custom_components.junghome_ble.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.junghome_ble.gateway_api import (
    GatewayCertificateMismatch,
    GatewayError,
    GatewayUnreachable,
    JungHomeGatewayApi,
)
from custom_components.junghome_ble.jhmesh import client as client_mod
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import vendor_models as V
from custom_components.junghome_ble.jhmesh.advert import JUNG_COMPANY_ID
from custom_components.junghome_ble.jhmesh.client import AccessMessage, ProxyClient
from custom_components.junghome_ble.jhmesh.crypto import NetKeyMaterial, aes_cmac
from custom_components.junghome_ble.jhmesh.pdu import (
    ALL_NODES,
    PROXY_BEACON,
    PROXY_NETWORK_PDU,
    decode_opcode,
    encode_opcode,
    network_encrypt,
)
from custom_components.junghome_ble.mesh_config import export_digest, gateway_sync

from .conftest import (
    CDB_PATH,
    META_DIR,
    PROXY_ADDRESS,
    PROXY_NODE,
    SHARE_EXPORT_PATH,
    STATE_GET_REPLIES,
    FakeProxyLink,
    make_service_info,
    settle,
    setup_entry,
    wait_for_link,
    wait_until,
)
from .helpers import (
    BUTTON_CLICK,
    BUTTON_DIMMER,
    BUTTON_HOLD_END,
    BUTTON_HOLD_START,
    BUTTON_WC,
    GATEWAY,
    GROUP_DIMMER,
    LIGHT_CTL,
    LIGHT_CTL_TEMPERATURE,
    LIGHT_DIMMER,
    LIGHT_OUT1,
    LIGHT_OUT2,
    LIGHT_SWITCH,
    MAC_LIGHT_CTL,
    OUR_ADDRESS,
    PROPERTY_ENERGY_SINCE_TURN_ON,
    PROPERTY_POWER_ON_TIME,
    PROPERTY_PRECISE_TOTAL_ENERGY,
    PROPERTY_TOTAL_ENERGY,
    ROCKER_A,
    SENSOR_CURRENT,
    SENSOR_POWER,
    SENSOR_VOLTAGE,
    SEQ_STORE_KEY,
    SOCKET,
    SOCKET_SENSOR,
    UID_LIGHT_CTL,
    UID_LIGHT_DIMMER,
    UID_LIGHT_SWITCH,
    UID_PROXY,
    UID_SOCKET,
    admin_property_status,
    ctl_range_status,
    ctl_status,
    ctl_temperature_status,
    entity_id,
    find_issue,
    lightness_status,
    onoff_status,
    property_absent_status,
    sensor_replies,
    sensor_status,
    vendor_button_event,
)
from .property_helpers import PropertyMesh

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

    from custom_components.junghome_ble.coordinator import MessageKey, StatusHandler

SECOND_PROXY = MAC_LIGHT_CTL  # node 0232's MAC
GROUP_SWITCH = 0xC061  # the group LIGHT_SWITCH publishes its status to

HOURS_GET = M.generic_property_get("admin", PROPERTY_POWER_ON_TIME)
ENERGY_GET = M.generic_property_get("manufacturer", PROPERTY_PRECISE_TOTAL_ENERGY)
RESETTABLE_GET = M.generic_property_get("admin", PROPERTY_TOTAL_ENERGY)
SINCE_ON_GET = M.generic_property_get("manufacturer", PROPERTY_ENERGY_SINCE_TURN_ON)
# the connect-time / periodic counter poll of the one metering socket, in `COUNTER_READS` order: the power-on
# hours from its main element, the three energy counters from its meter element
COUNTER_GETS = [
    (OUR_ADDRESS, SOCKET, HOURS_GET),
    (OUR_ADDRESS, SOCKET_SENSOR, ENERGY_GET),
    (OUR_ADDRESS, SOCKET_SENSOR, RESETTABLE_GET),
    (OUR_ADDRESS, SOCKET_SENSOR, SINCE_ON_GET),
]
STATE_REPLIES = {  # what a healthy mesh answers to each Get of the connect-time refresh and the energy poll
    M.generic_onoff_get(): onoff_status(False),
    M.light_ctl_get(): ctl_status(0, 4000),
    M.light_lightness_get(): lightness_status(0),
    **sensor_replies(
        power=(38).to_bytes(2, "little"),
        voltage=(230).to_bytes(2, "little"),
        current=(2).to_bytes(2, "little"),
    ),
    M.light_ctl_temperature_range_get(): ctl_range_status(2700, 6500),
    M.light_ctl_temperature_get(): ctl_temperature_status(4000),
    HOURS_GET: admin_property_status(
        PROPERTY_POWER_ON_TIME, (42).to_bytes(3, "little")
    ),
    ENERGY_GET: admin_property_status(
        PROPERTY_PRECISE_TOTAL_ENERGY,
        (210198).to_bytes(4, "little"),
        access=1,
        opcode=M.GEN_MANU_PROP_STATUS,
    ),
    RESETTABLE_GET: admin_property_status(
        PROPERTY_TOTAL_ENERGY, (210040).to_bytes(4, "little")
    ),
    SINCE_ON_GET: admin_property_status(
        PROPERTY_ENERGY_SINCE_TURN_ON,
        (1009).to_bytes(4, "little"),
        access=1,
        opcode=M.GEN_MANU_PROP_STATUS,
    ),
}
# after the counters, the scene members are asked what they do in their scenes: the one member of scene 1
# (LIGHT_SWITCH) lists its scenes, then answers for scene 1
SCENE_GETS_PDUS = [
    (OUR_ADDRESS, LIGHT_SWITCH, V.scene_action_get()),
    (OUR_ADDRESS, LIGHT_SWITCH, V.scene_action_get(1)),
]
STATE_REPLIES[V.scene_action_get()] = encode_opcode(
    V.SCENE_ACTION_SETUP_STATUS, M.JUNG_CID
) + bytes.fromhex("0000 0100 0000")
STATE_REPLIES[V.scene_action_get(1)] = encode_opcode(
    V.SCENE_ACTION_SETUP_STATUS, M.JUNG_CID
) + bytes.fromhex("0100 01 01 00000000")
# last of all, every node (the gateway too, not the phone) is asked for its Health fault register, in export order
FAULT_GETS_PDUS = [
    (OUR_ADDRESS, node, M.health_fault_get())
    for node in (GATEWAY, LIGHT_SWITCH, LIGHT_CTL, SOCKET, LIGHT_DIMMER, LIGHT_OUT1)
]
FAULT_STATUS = encode_opcode(M.HEALTH_FAULT_STATUS) + bytes.fromhex("00 2705 81")
# every node answers its Health Fault Get with JUNG's usual 0x81
STATE_REPLIES[M.health_fault_get()] = FAULT_STATUS
# PDUs of the connect-time refresh: 5 lights + socket + the three qualified Sensor Gets of its meter + the CTL
# light's temperature range + its temperature element's colour temperature; they go out as 9 jobs (the meter's three
# Gets are one sequential job) in chunks of 5
REFRESH_GETS = 11
BROADCASTS = (
    2  # before the refresh: Time Set, then the home location, both to all nodes
)
REFRESH_FIRST_CHUNK = 5  # PDUs of the first chunk: the five lights
ENERGY_GETS = len(COUNTER_GETS)  # the four counters of the one metering socket
SCENE_GETS = len(
    SCENE_GETS_PDUS
)  # the scene-action reads of the one scene member (answered link)
FAULT_GETS = len(FAULT_GETS_PDUS)  # one Health Fault Get per node
# last, the current scene of every element holding a scene register (the fixture's scene 1 is stored on 0148)
CURRENT_SCENE_PDUS = [(OUR_ADDRESS, LIGHT_SWITCH, M.scene_get())]
CURRENT_SCENE_GETS = len(CURRENT_SCENE_PDUS)
CONNECT_TAIL = (
    SCENE_GETS + FAULT_GETS + CURRENT_SCENE_GETS
)  # PDUs after the energy poll: the scene reads, the fault survey, the current scenes
AFTER_ENERGY = (
    ENERGY_GETS + CONNECT_TAIL
)  # PDUs from the energy poll to the end of the connect sequence


def answer_gets(link: FakeProxyLink) -> None:
    """Make the fake proxy's mesh answer every state Get with a unicast status from the element, as a healthy JUNG mesh does."""
    original = link.write_gatt_char

    async def write_and_answer(
        char: str, data: bytes, response: bool | None = None
    ) -> None:
        before = len(link.sent)
        await original(char, data, response)
        for src, dst, access in link.sent[before:]:
            if (reply := STATE_REPLIES.get(access)) is not None:
                link.inject(dst, src, reply)

    link.write_gatt_char = write_and_answer  # type: ignore[method-assign]


@pytest.fixture
def answering_link(fake_link: FakeProxyLink) -> FakeProxyLink:
    answer_gets(fake_link)
    return fake_link


@pytest.fixture(autouse=True)
def no_property_reads() -> Generator[None]:
    """Keep the config entities' initial property reads out of the hub's traffic.

    Every config entity (`config_entities.ConfigEntity`) queues a property or setup-state Get after each connection.
    The tests here assert on exactly what the hub itself sends and receives (`fake_link.sent`, REFRESH_GETS /
    ENERGY_GETS, the drop-detection counters, the link watchdog's silence), and the fake mesh does not answer
    those Gets, so without this patch every test would see dozens of extra PDUs and stray 3 s timeouts. The
    reads have their own tests (test_config_entities.py and the platform modules); this is the one place they
    are switched off, so a hub test that needs them must opt out explicitly.
    """
    with patch(
        "custom_components.junghome_ble.config_entities.ConfigEntity._maybe_read"
    ):
        yield


@pytest.fixture
async def init_answered(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    fast_sleep: list[float],
) -> MockConfigEntry:
    """Like `init_integration`, but the mesh answers the connect-time refresh, so it completes at once."""
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    return mock_config_entry


@pytest.fixture
def refresh_gate() -> Generator[asyncio.Event]:
    """Hold every refresh at the pause between its chunks of Gets (the 0.5 s sleep) until the gate opens; every other
    `asyncio.sleep` returns after one loop iteration, like `fast_sleep`."""
    gate = asyncio.Event()
    real_sleep = asyncio.sleep

    async def gated_sleep(delay: float, result: Any = None) -> Any:
        if delay == 0.5:
            await gate.wait()
        else:
            await real_sleep(0)
        return result

    with patch("custom_components.junghome_ble.coordinator.asyncio.sleep", gated_sleep):
        yield gate


def assert_time_set(
    sent: tuple[int, int, bytes], when: datetime, zone: timedelta | None = None
) -> None:
    """The PDU is a Time Set to all nodes carrying `when` (to the second) and its zone offset."""
    src, dst, pdu = sent
    assert (src, dst) == (OUR_ADDRESS, ALL_NODES)
    assert pdu[0] == M.TIME_SET
    assert len(pdu) == 11
    tai = int.from_bytes(pdu[1:6], "little")
    elapsed = when.astimezone(UTC) - M.TAI_EPOCH
    assert abs(tai - (elapsed.days * 86400 + elapsed.seconds + M.TAI_UTC_DELTA)) <= 1
    zone = when.utcoffset() if zone is None else zone
    assert zone is not None
    assert pdu[10] == int(zone.total_seconds()) // 900 + 64


def hub_of(entry: MockConfigEntry) -> JungHomeHub:
    return entry.runtime_data


def never_done(hass: HomeAssistant) -> asyncio.Task[Any]:
    """A stand-in for a poll still in progress (untracked by HA, so `async_block_till_done` does not wait for it)."""
    return hass.loop.create_task(asyncio.Event().wait())


def events_of(hub: JungHomeHub, addr: int) -> list[tuple[str, dict[str, Any]]]:
    """Record every button event the hub fires for an element."""
    got: list[tuple[str, dict[str, Any]]] = []
    hub.add_event_listener(addr, lambda event, attrs: got.append((event, attrs)))
    return got


# --------------------------------------------------------------------------- connection loop


async def test_initial_connection_and_refresh(
    hass: HomeAssistant,
    # before `init_answered`: the Time Set sent while it connects is compared with `dt_util.now()` below, which on
    # a wall clock moved on by however long a loaded runner took in between
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = hub_of(init_answered)
    assert hub.connected
    assert hub.proxy_address == PROXY_ADDRESS
    assert (
        hub.proxy_node == 0x0148
    )  # the fake proxy answers the filter status as node 0148
    assert hub.connected_since is not None
    assert f"Connected to the JUNG mesh through proxy node {PROXY_ADDRESS}" in [
        r.message for r in caplog.get_records("setup")
    ]
    assert (
        hass.states.get(entity_id(hass, "sensor", UID_PROXY)).state
        == "Push-button 1-gang 0148"
    )

    # every load was asked for its state with the Get matching its kind, in export order, our address as source;
    # the socket's meter for each of its readings by property (it ignores a Sensor Get without one), then the CTL
    # light for its temperature range; the mesh got the time and location first, right after the proxy filter (as
    # the app sends the time at start), and the metering socket was asked for its power-on hours, which nothing
    # publishes
    assert fake_link.sent[BROADCASTS : BROADCASTS + REFRESH_GETS] == [
        (OUR_ADDRESS, LIGHT_SWITCH, M.generic_onoff_get()),
        (OUR_ADDRESS, LIGHT_CTL, M.light_ctl_get()),
        (OUR_ADDRESS, LIGHT_DIMMER, M.light_lightness_get()),
        (OUR_ADDRESS, LIGHT_OUT1, M.generic_onoff_get()),
        (OUR_ADDRESS, LIGHT_OUT2, M.generic_onoff_get()),
        (OUR_ADDRESS, SOCKET, M.generic_onoff_get()),
        (OUR_ADDRESS, SOCKET_SENSOR, M.sensor_get(SENSOR_POWER)),
        (OUR_ADDRESS, SOCKET_SENSOR, M.sensor_get(SENSOR_VOLTAGE)),
        (OUR_ADDRESS, SOCKET_SENSOR, M.sensor_get(SENSOR_CURRENT)),
        (OUR_ADDRESS, LIGHT_CTL, M.light_ctl_temperature_range_get()),
        (OUR_ADDRESS, LIGHT_CTL_TEMPERATURE, M.light_ctl_temperature_get()),
    ]
    assert M.sensor_get() not in [pdu for _, _, pdu in fake_link.sent]
    assert len(fake_link.sent) == REFRESH_GETS + BROADCASTS + AFTER_ENERGY
    assert (
        fake_link.sent[-CONNECT_TAIL : -FAULT_GETS - CURRENT_SCENE_GETS]
        == SCENE_GETS_PDUS
    )
    assert fake_link.sent[-FAULT_GETS - CURRENT_SCENE_GETS : -CURRENT_SCENE_GETS] == (
        FAULT_GETS_PDUS
    )
    assert fake_link.sent[-CURRENT_SCENE_GETS:] == CURRENT_SCENE_PDUS
    assert (
        hub.states[LIGHT_SWITCH].scene == 0
    )  # its Scene Server says no scene is current
    assert hub.scene_actions == {1: {LIGHT_SWITCH: V.Action(V.ACTION_SWITCH, on=True)}}
    assert all(hub.states[node].faults == (0x81,) for _, node, _ in FAULT_GETS_PDUS)
    assert_time_set(fake_link.sent[0], dt_util.now())
    config = hass.config
    assert fake_link.sent[1] == (
        OUR_ADDRESS,
        ALL_NODES,
        M.generic_location_global_set(
            config.latitude, config.longitude, int(config.elevation)
        ),
    )
    assert fake_link.sent[REFRESH_GETS + BROADCASTS : -CONNECT_TAIL] == COUNTER_GETS
    assert 0.5 in fast_sleep  # a pause after every chunk of five Gets
    # the replies were applied, and a refresh that was answered is no reason for a repair
    assert hub.states[LIGHT_SWITCH].on is False
    assert hub.states[LIGHT_CTL].kelvin == 4000
    assert (hub.states[LIGHT_CTL].kelvin_min, hub.states[LIGHT_CTL].kelvin_max) == (
        2700,
        6500,
    )
    assert (
        hub.states[SOCKET].power_w,
        hub.states[SOCKET].voltage_v,
        hub.states[SOCKET].current_a,
    ) == (3.8, 230.0, 0.02)
    assert hub.states[SOCKET].power_on_hours == 42
    assert (
        hub.states[SOCKET].energy_wh,
        hub.states[SOCKET].energy_resettable_wh,
        hub.states[SOCKET].energy_since_on_wh,
    ) == (210198, 210040, 1009)
    assert SOCKET_SENSOR not in hub.states  # the meter's counters land on the socket
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None


async def test_refresh_stops_on_send_failure(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = hub_of(init_answered)
    fake_link.sent.clear()
    fake_link.write_error = OSError("GATT write failed")
    await hub._refresh_all()
    assert fake_link.sent == []
    assert "refresh aborted: proxy write failed: GATT write failed" in caplog.text

    fake_link.write_error = None
    fake_link.sent.clear()
    hub.proxy.client = None  # nothing to send through
    await hub._refresh_all()
    assert fake_link.sent == []
    assert "refresh aborted: not connected to a proxy" in caplog.text


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
    old = hub._refresh_task
    assert old is not None
    assert not old.done()

    answering_link.drop_link()
    await wait_for_link(hass, mock_config_entry, connected=False)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    assert old.cancelled()
    assert answering_link.connect_count == 2
    assert hub._refresh_task is not None
    assert hub._refresh_task is not old
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
    assert hub._refresh_task.done()


async def test_stop_cancels_a_running_refresh(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    refresh_gate: asyncio.Event,
) -> None:
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    hub = hub_of(mock_config_entry)
    task = hub._refresh_task
    assert task is not None
    assert not task.done()
    await hub.async_stop()
    assert task.cancelled()
    assert hub._refresh_task is None
    assert hub._task is None
    await hub._watch_link()  # a link that is already gone: returns at once


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
    assert not hub._link_lost.is_set()


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
    with patch.object(hub, "visible_proxies", wraps=hub.visible_proxies) as visible:
        for n in range(100):
            foreign = make_service_info(
                bytes([n + 1]) * 8, address=f"30:FB:10:00:01:{n:02X}"
            )
            mock_bluetooth_env["callbacks"][0](foreign, BluetoothChange.ADVERTISEMENT)
            await asyncio.sleep(0)
        await settle(hass)
        assert not hub._link_lost.is_set()
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


async def test_an_unchanged_status_writes_no_state(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    """Review-4 R I-10: a status that changes nothing an entity shows writes nothing; one that does is written, and
    so is a change of availability."""
    light = entity_id(hass, "light", UID_LIGHT_SWITCH)
    fake_link.inject(LIGHT_SWITCH, 0xC061, onoff_status(True))
    await hass.async_block_till_done()
    assert hass.states.get(light).state == STATE_ON
    written: list[str] = []

    @callback
    def ours(data: Mapping[str, Any]) -> bool:
        return bool(data["entity_id"] == light)

    @callback
    def note(event: Event[Any]) -> None:
        written.append(event.event_type)

    hass.bus.async_listen(EVENT_STATE_CHANGED, note, event_filter=ours)
    hass.bus.async_listen(EVENT_STATE_REPORTED, note, event_filter=ours)
    fake_link.inject(LIGHT_SWITCH, 0xC061, onoff_status(True))
    await hass.async_block_till_done()
    assert written == []
    fake_link.inject(LIGHT_SWITCH, 0xC061, onoff_status(False))
    await hass.async_block_till_done()
    assert written == [EVENT_STATE_CHANGED]
    assert hass.states.get(light).state == STATE_OFF
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()  # nothing a status changed, but the link went
    await settle(hass)
    assert hass.states.get(light).state == STATE_UNAVAILABLE


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
    freezer.tick(coordinator.SHORT_LINK)
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
        f"{coordinator.SHORT_LINK:.0f} s of connecting" in caplog.text
    )

    # a link that lasts is a working one: the back-off starts over
    freezer.tick(coordinator.SHORT_LINK)
    fast_sleep.clear()
    await drop_and_reconnect(hass, init_integration, fake_link)
    assert (
        hub.proxy_address == SECOND_PROXY
    )  # the flapping node still sits out its cooldown
    await drop_and_reconnect(hass, init_integration, fake_link)
    pauses = [d for d in fast_sleep if d in (1.0, 2.0, 4.0, 8.0, 16.0)]
    assert pauses == [1.0, 2.0]
    assert hub._short_links[SECOND_PROXY] == 1

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


async def test_stop_is_idempotent(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    hub = hub_of(init_integration)
    await hub.async_stop()
    assert not hub.connected
    assert not fake_link.is_connected
    assert mock_bluetooth_env["callbacks"] == []
    await hub.async_stop()


async def test_stop_disconnects_even_when_a_background_task_failed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """HAC-04: `_cancel`'s `task.cancel()` is a no-op on a task that has already finished with an exception, and
    `await task` then re-raises it — an error `async_stop` had no `try/finally` to survive, so it used to skip
    the disconnect and the counter close and leave the BLE connection (and its adapter/proxy slot) open."""
    hub = hub_of(init_integration)

    async def boom() -> None:
        raise ValueError("x")

    hub._heartbeat_task = hass.async_create_task(boom())
    await (
        hass.async_block_till_done()
    )  # the task is done with the exception before async_stop cancels it

    await hub.async_stop()  # must not raise
    assert not fake_link.is_connected
    assert hub._heartbeat_task is None


async def test_malformed_heartbeat_publication_status_is_ignored(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HAC-04: a truncated Heartbeat Publication Status must not raise `ValueError` out of `_set_heartbeat` — that
    also escapes `_configure_heartbeats`/`async_disable_heartbeats`, which catch only `ConnectionError`, and (via
    `_async_entry_updated`) can abort an options-change reload before it applies. Nor is it a confirmation: the
    node gets no liveness deadline from it."""
    caplog.set_level(logging.DEBUG, logger="custom_components.junghome_ble.coordinator")
    hub = hub_of(init_integration)
    node = hub.heartbeat_nodes[0]
    deadlines = dict(hub._alive_deadline)
    hub.proxy.request_config = AsyncMock(
        return_value=SimpleNamespace(params=b"\x00\x00\x00")
    )
    assert await hub._set_heartbeat(node, b"", "configure") is False  # no raise
    hub.proxy.request_config.assert_awaited_once()
    assert hub._alive_deadline == deadlines
    assert f"{node.unicast:04X}: malformed Heartbeat Publication Status" in caplog.text


async def test_cancel_still_propagates_a_cancellation_of_its_own_task(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """HAC-04: `_cancel` swallows the `CancelledError` its own `task.cancel()` produces, but must not swallow
    one that means `_cancel`'s own caller (here, the task running it) is itself being cancelled — that would
    turn a cancelled `async_stop` into one that quietly finishes instead of stopping partway as asked."""
    hub = hub_of(init_integration)
    release = asyncio.Event()

    async def stubborn() -> None:
        try:
            await release.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(
                0.01
            )  # still running when the wrapper below is cancelled
            raise

    task = hass.async_create_task(stubborn())
    await asyncio.sleep(0)

    wrapper = hass.async_create_task(hub._cancel(task))
    await asyncio.sleep(0)  # let it call task.cancel() and reach `await task`
    wrapper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wrapper


async def test_sequence_number_survives_a_reload(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    hass_storage: dict[str, Any],
) -> None:
    """Our sequence number is persisted (debounced, in the mesh's store under our address, marked in use) and
    continues exactly after a reload: the unload stored it as closed cleanly, so no restart margin is needed."""
    entry = init_answered
    hub = hub_of(entry)
    seq = hub.proxy.state.seq
    assert (
        seq == 1 + REFRESH_GETS + BROADCASTS + AFTER_ENERGY
    )  # filter set + the Gets, each answered at the first attempt, + Time Set and location + the energy poll + the
    # scene and fault reads
    freezer.tick(timedelta(seconds=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    saved = hass_storage[SEQ_STORE_KEY]["data"]["addresses"]
    assert set(saved) == {"0D00"}
    assert {k: v for k, v in saved["0D00"].items() if k != "rpl"} == {
        "seq": seq,
        "iv_index": 0,
        "iv_update_active": False,
        "iv_known": True,  # the proxy's connect-time beacon authenticated
        "clean": False,
        "seq_peak": 0,
        "seq_peak_from": 0,
    }
    assert saved["0D00"]["rpl"]  # the replay list travels with the counter

    writes = len(fake_link.raw_writes)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await wait_for_link(hass, entry)
    new_state = hub_of(entry).proxy.state
    assert new_state is not hub.proxy.state
    assert (
        new_state.seq == seq + len(fake_link.raw_writes) - writes
    )  # no margin, no gap


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


def quiet_mesh(hub: JungHomeHub) -> None:
    """Stop the periodic energy poll: on a mesh without a gateway nothing else is on air at night."""
    assert hub._unsub_energy is not None
    hub._unsub_energy()
    hub._unsub_energy = None


def stop_answering(link: FakeProxyLink) -> None:
    """Undo `answer_gets`: the proxy still takes our writes but nothing comes back through it."""
    link.write_gatt_char = FakeProxyLink.write_gatt_char.__get__(link)  # type: ignore[method-assign]


def keep_alive_gets(link: FakeProxyLink) -> list[int]:
    """Destinations of the keep-alive Gets sent so far."""
    return [dst for src, dst, pdu in link.sent if pdu == KEEP_ALIVE_GET]


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    freezer.tick(seconds)
    async_fire_time_changed(hass)
    await settle(hass)


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
    keep_alive = asyncio.get_running_loop().create_task(hub._keep_alive())
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


async def test_a_send_held_back_past_the_deadline_is_given_up(
    hass: HomeAssistant, init_answered: MockConfigEntry, fast_sleep: list[float]
) -> None:
    """`_while_seq_stalls` retries back-pressure every SEQ_STALL_RETRY for SEQ_STALL_DEADLINE, then raises it like a
    lost link; a send that goes through within that time returns its result."""
    hub = hub_of(init_answered)
    retries = int(coordinator.SEQ_STALL_DEADLINE / coordinator.SEQ_STALL_RETRY)
    calls: list[int] = []

    async def held_back() -> int:
        calls.append(1)
        if len(calls) <= retries:
            raise client_mod.SequenceStalled("held back")
        return len(calls)

    fast_sleep.clear()
    assert (
        await hub._while_seq_stalls(held_back) == retries + 1
    )  # through on the last retry
    calls.clear()
    retries += 1  # one refusal more than the deadline allows
    with pytest.raises(client_mod.SequenceStalled):
        await hub._while_seq_stalls(held_back)
    assert len(calls) == retries
    assert fast_sleep == [coordinator.SEQ_STALL_RETRY] * (2 * retries - 2)


# --------------------------------------------------------------------------- per-node reachability (the app's rule)


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
        hub._missed_answer(LIGHT_DIMMER, "dimmer", time.monotonic())
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
        hub._missed_answer(LIGHT_DIMMER, "dimmer", time.monotonic(), full=False)
        assert caplog.text.count("did not answer a request") == 1
        assert list(hub._recheck) == [LIGHT_DIMMER]

    # any message from any of its elements: reachable again, nothing left to re-ask
    fake_link.inject(BUTTON_DIMMER, GROUP_SWITCH, onoff_status(True))
    await settle(hass)
    assert hass.states.get(light).state != STATE_UNAVAILABLE
    assert "is reachable again" in caplog.text
    assert not hub._recheck


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
        hub._missed_answer(LIGHT_DIMMER, "dimmer", asked)
        assert not hub.unreachable
        await tick(hass, freezer, UNREACHABLE_RECHECK + 1)
        refresh.assert_awaited_once_with(LIGHT_DIMMER, "dimmer", quiet=False)
        hub._missed_answer(0x0999, "switch", asked)  # an address no node owns
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
        hub._missed_answer(LIGHT_DIMMER, "switch", time.monotonic(), full=False)
        assert not hub.unreachable
        await tick(hass, freezer, UNREACHABLE_RECHECK + 1)
        refresh.assert_awaited_once_with(LIGHT_DIMMER, "switch", quiet=False)
    # the fixture network has no battery node: the socket stands in for a wall transmitter (PID 0x0005)
    socket = hub.cdb.node_by_addr(SOCKET)
    assert socket is not None
    with patch.object(socket, "pid", 0x0005):
        hub._missed_answer(SOCKET, "switch", time.monotonic())
    assert not hub.unreachable
    assert SOCKET not in hub._recheck


async def test_a_new_link_drops_pending_re_asks_but_keeps_an_unreachable_node(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    freezer: FrozenDateTimeFactory,
) -> None:
    hub = hub_of(init_answered)
    freezer.tick(1)
    with patch.object(hub, "async_refresh_element", AsyncMock()) as refresh:
        hub._missed_answer(LIGHT_CTL, "ctl", time.monotonic())
        hub._missed_answer(LIGHT_DIMMER, "dimmer", time.monotonic(), full=False)
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
    assert not hub._recheck


async def test_a_re_ask_due_while_the_link_is_down_is_dropped(
    hass: HomeAssistant, init_answered: MockConfigEntry
) -> None:
    hub = hub_of(init_answered)
    hub._missed_answer(LIGHT_DIMMER, "dimmer", time.monotonic(), full=False)
    hub._recheck[LIGHT_DIMMER]()  # cancel the timer: it is run by hand below
    with (
        patch.object(
            type(hub), "connected", new_callable=PropertyMock, return_value=False
        ),
        patch.object(hub, "async_refresh_element", AsyncMock()) as refresh,
    ):
        hub._recheck_node(LIGHT_DIMMER, LIGHT_DIMMER, "dimmer", dt_util.utcnow())
        await settle(hass)
    refresh.assert_not_awaited()
    assert not hub._recheck


async def test_unanswered_state_get_and_keep_alive_count(
    hass: HomeAssistant, init_answered: MockConfigEntry
) -> None:
    """A state Get with its full budget is a verdict; its quiet re-probe and a keep-alive are one attempt, not one."""
    hub = hub_of(init_answered)
    with (
        patch.object(
            hub.proxy, "request", AsyncMock(side_effect=TimeoutError)
        ) as request,
        patch.object(hub, "_missed_answer") as missed,
    ):
        await hub.async_refresh_element(LIGHT_DIMMER, "dimmer")
        assert request.call_args.kwargs["retries"] == REQUEST_ATTEMPTS
        missed.assert_called_once_with(LIGHT_DIMMER, "dimmer", ANY, full=True)
        missed.reset_mock()
        await hub.async_refresh_element(LIGHT_DIMMER, "dimmer", quiet=True)
        missed.assert_called_once_with(LIGHT_DIMMER, "dimmer", ANY, full=False)
        missed.reset_mock()
        await hub._keep_alive()
    assert missed.call_args_list[0].args[1] == "switch"
    assert missed.call_args_list[0].kwargs == {"full": False}


async def test_stop_cancels_a_pending_re_ask(
    hass: HomeAssistant, init_answered: MockConfigEntry
) -> None:
    hub = hub_of(init_answered)
    hub._missed_answer(LIGHT_DIMMER, "dimmer", time.monotonic(), full=False)
    assert hub._recheck
    await hass.config_entries.async_unload(init_answered.entry_id)
    await hass.async_block_till_done()
    assert not hub._recheck


@pytest.fixture
def no_rechecks() -> Generator[None]:
    """Never ask a node that missed a state Get again: the keep-alive tests count every OnOff Get as a keep-alive."""
    with patch("custom_components.junghome_ble.coordinator.UNREACHABLE_RECHECK", 1e9):
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
    assert hub._keep_alive_targets() == KEEP_ALIVE_TARGETS
    hub.proxy_node = SOCKET
    assert hub._keep_alive_targets() == [
        LIGHT_SWITCH,
        LIGHT_CTL,
        LIGHT_DIMMER,
        LIGHT_OUT1,
        SOCKET,
    ]
    hub.proxy_node = None  # not named yet: no node is "the proxy"
    assert hub._keep_alive_targets() == [
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
    assert hub._keep_alive_targets() == [
        LIGHT_DIMMER,
        LIGHT_OUT1,
        SOCKET,
        LIGHT_SWITCH,
    ]
    hub.unreachable.update({LIGHT_SWITCH, SOCKET, LIGHT_DIMMER, LIGHT_OUT1})
    assert (
        hub._keep_alive_targets()
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
    with patch.object(hub, "_keep_alive_targets", return_value=[]):
        assert await hub._keep_alive() is False
    assert fake_link.sent == []


# --------------------------------------------------------------------------- incoming status messages


async def test_status_messages_update_element_state(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The short status form sets present and target alike; the entity always shows the *present* field."""
    hub = hub_of(init_integration)
    light = entity_id(hass, "light", UID_LIGHT_SWITCH)

    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, onoff_status(True))
    await hass.async_block_till_done()
    assert (hub.states[LIGHT_SWITCH].on, hub.states[LIGHT_SWITCH].target_on) == (
        True,
        True,
    )
    assert hass.states.get(light).state == STATE_ON

    fake_link.inject(LIGHT_DIMMER, 0xC00F, lightness_status(32768))
    fake_link.inject(LIGHT_CTL, 0xC010, ctl_status(65535, 4000))
    await hass.async_block_till_done()
    dimmer, ctl = hub.states[LIGHT_DIMMER], hub.states[LIGHT_CTL]
    assert (dimmer.lightness, dimmer.target_lightness, dimmer.on) == (
        32768,
        32768,
        True,
    )
    assert (ctl.lightness, ctl.kelvin, ctl.on) == (65535, 4000, True)
    assert (ctl.target_lightness, ctl.target_kelvin) == (65535, 4000)
    assert (
        hass.states.get(entity_id(hass, "light", UID_LIGHT_DIMMER)).attributes[
            "brightness"
        ]
        == 128
    )
    assert (
        hass.states.get(entity_id(hass, "light", UID_LIGHT_CTL)).attributes[
            "color_temp_kelvin"
        ]
        == 4000
    )


async def test_transitioning_statuses_show_the_present_state(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A long status (present ≠ target, remaining time) renders the present field, as the app does; the target is kept."""
    hub = hub_of(init_integration)
    light = entity_id(hass, "light", UID_LIGHT_SWITCH)

    fake_link.inject(
        LIGHT_SWITCH, OUR_ADDRESS, onoff_status(True, target=False, remaining=0x42)
    )  # still on, fading off
    await hass.async_block_till_done()
    assert (hub.states[LIGHT_SWITCH].on, hub.states[LIGHT_SWITCH].target_on) == (
        True,
        False,
    )
    assert hass.states.get(light).state == STATE_ON

    fake_link.inject(
        LIGHT_DIMMER, 0xC00F, lightness_status(32768, target=0, remaining=0x42)
    )
    fake_link.inject(
        LIGHT_CTL, 0xC010, ctl_status(65535, 4000, target=(0, 2700), remaining=0x42)
    )
    await hass.async_block_till_done()
    dimmer, ctl = hub.states[LIGHT_DIMMER], hub.states[LIGHT_CTL]
    assert (dimmer.lightness, dimmer.target_lightness, dimmer.on) == (32768, 0, True)
    assert (ctl.lightness, ctl.kelvin, ctl.on) == (65535, 4000, True)
    assert (ctl.target_lightness, ctl.target_kelvin) == (0, 2700)
    dimmer_state = hass.states.get(entity_id(hass, "light", UID_LIGHT_DIMMER))
    assert (dimmer_state.state, dimmer_state.attributes["brightness"]) == (
        STATE_ON,
        128,
    )
    ctl_state = hass.states.get(entity_id(hass, "light", UID_LIGHT_CTL))
    assert (ctl_state.state, ctl_state.attributes["color_temp_kelvin"]) == (
        STATE_ON,
        4000,
    )

    # the end of the fade arrives as a short status: present and target agree again
    fake_link.inject(LIGHT_DIMMER, 0xC00F, lightness_status(0))
    fake_link.inject(LIGHT_CTL, 0xC010, ctl_status(0, 2700))
    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, onoff_status(False))
    await hass.async_block_till_done()
    assert (dimmer.lightness, dimmer.target_lightness, dimmer.on) == (0, 0, False)
    assert (ctl.lightness, ctl.kelvin, ctl.target_kelvin, ctl.on) == (
        0,
        2700,
        2700,
        False,
    )
    assert hub.states[LIGHT_SWITCH].target_on is False
    for uid in (UID_LIGHT_SWITCH, UID_LIGHT_DIMMER, UID_LIGHT_CTL):
        assert hass.states.get(entity_id(hass, "light", uid)).state == STATE_OFF


async def test_ctl_range_status_sets_the_light_limits(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Light CTL Temperature Range Status fills `kelvin_min` / `kelvin_max`; failures and nonsense ranges do not."""
    hub = hub_of(init_integration)
    for bad in (
        ctl_range_status(2700, 6500, status=1),  # cannot-set-range-min: no range in it
        ctl_range_status(6500, 2700),  # inverted
        ctl_range_status(0, 0),  # empty
        encode_opcode(M.LIGHT_CTL_TEMP_RANGE_STATUS) + b"\x00\x8c\x0a",  # truncated
    ):
        fake_link.inject(LIGHT_CTL, OUR_ADDRESS, bad)
    await hass.async_block_till_done()
    assert LIGHT_CTL not in hub.states

    fake_link.inject(LIGHT_CTL, OUR_ADDRESS, ctl_range_status(2700, 6500))
    await hass.async_block_till_done()
    assert (hub.states[LIGHT_CTL].kelvin_min, hub.states[LIGHT_CTL].kelvin_max) == (
        2700,
        6500,
    )
    assert (
        hub.states[LIGHT_CTL].kelvin is None
    )  # the range says nothing about the state


async def test_ctl_temperature_status_updates_only_the_colour_temperature(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """P6: a Light CTL Temperature Status from the light's temperature element lands on the light, and changes
    its colour temperature alone — the lightness and on/off stay what the last CTL / Lightness Status said.
    Unverified on air, so anything odd is ignored: a short status, a temperature outside 800..20000 K, and the
    same status from an element that is not a CTL light's (a dimmer, the node's rocker)."""
    hub = hub_of(init_integration)
    light = entity_id(hass, "light", UID_LIGHT_CTL)
    fake_link.inject(LIGHT_CTL, OUR_ADDRESS, ctl_status(0x8000, 4000))
    await hass.async_block_till_done()

    fake_link.inject(LIGHT_CTL_TEMPERATURE, 0xC010, ctl_temperature_status(3000))
    await hass.async_block_till_done()
    ctl = hub.states[LIGHT_CTL]
    assert (ctl.kelvin, ctl.target_kelvin) == (3000, 3000)
    assert (ctl.lightness, ctl.target_lightness, ctl.on) == (0x8000, 0x8000, True)
    assert LIGHT_CTL_TEMPERATURE not in hub.states
    state = hass.states.get(light)
    assert (state.state, state.attributes["color_temp_kelvin"]) == (STATE_ON, 3000)

    # in a transition: present and target, as the other load statuses
    fake_link.inject(
        LIGHT_CTL_TEMPERATURE,
        0xC010,
        ctl_temperature_status(3500, target=5000, remaining=0x05),
    )
    await hass.async_block_till_done()
    assert (ctl.kelvin, ctl.target_kelvin) == (3500, 5000)
    # from the light's own element too
    fake_link.inject(LIGHT_CTL, OUR_ADDRESS, ctl_temperature_status(5000))
    await hass.async_block_till_done()
    assert (ctl.kelvin, ctl.target_kelvin) == (5000, 5000)

    for src, pdu in (
        (
            LIGHT_CTL_TEMPERATURE,
            encode_opcode(M.LIGHT_CTL_TEMP_STATUS) + b"\xb8\x0b\x00",
        ),
        (LIGHT_CTL_TEMPERATURE, ctl_temperature_status(0)),
        (LIGHT_CTL_TEMPERATURE, ctl_temperature_status(0xFFFF)),
        (LIGHT_CTL_TEMPERATURE, ctl_temperature_status(3000, target=0xFFFF)),
        (LIGHT_DIMMER, ctl_temperature_status(3000)),
        (ROCKER_A, ctl_temperature_status(3000)),
        (0x7F00, ctl_temperature_status(3000)),  # no element of the export
    ):
        fake_link.inject(src, OUR_ADDRESS, pdu)
    await hass.async_block_till_done()
    assert (ctl.kelvin, ctl.target_kelvin, ctl.lightness) == (5000, 5000, 0x8000)
    assert LIGHT_DIMMER not in hub.states or hub.states[LIGHT_DIMMER].kelvin is None
    assert ROCKER_A not in hub.states


async def test_wait_settled_needs_the_state_that_was_asked_for(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C5: a load that answers its Get with the state it had before the Set (it had not got to the Set yet, or
    lost it) is not settled, however idle it looks; the state asked for is, within a step of the load's own,
    and so is one it clamped or rounded further as long as it moved away from where it was."""
    hub = hub_of(init_answered)
    assert hub.states[LIGHT_DIMMER].lightness == 0  # the connect-time refresh
    wanted = round(0.4 * 0xFFFF)
    await hub.set_lightness(LIGHT_DIMMER, wanted)
    gets = len(fake_link.sent)
    assert not await hub.async_wait_settled(LIGHT_DIMMER, "dimmer")
    assert len(fake_link.sent) - gets == coordinator.SETTLE_ATTEMPTS  # asked every time
    try:
        STATE_REPLIES[M.light_lightness_get()] = lightness_status(wanted - 1)
        assert await hub.async_wait_settled(LIGHT_DIMMER, "dimmer")
        # asked again for what it already shows a step off: near enough, nothing had to move
        await hub.set_lightness(LIGHT_DIMMER, wanted)
        assert await hub.async_wait_settled(LIGHT_DIMMER, "dimmer")
        # clamped to its range: far from the request, but no longer the state from before the Set
        await hub.set_lightness(LIGHT_DIMMER, 0x0100)
        STATE_REPLIES[M.light_lightness_get()] = lightness_status(0x1999)
        assert await hub.async_wait_settled(LIGHT_DIMMER, "dimmer")
    finally:
        STATE_REPLIES[M.light_lightness_get()] = lightness_status(0)
    # nothing known before the Set, or never set through the hub: an answer at rest is all there is to go by
    del hub.states[LIGHT_SWITCH]
    await hub.set_onoff(LIGHT_SWITCH, True)
    assert await hub.async_wait_settled(LIGHT_SWITCH, "switch")
    assert await hub.async_wait_settled(SOCKET, "switch")
    assert "no response from" not in caplog.text


async def test_wait_settled_gives_a_silent_load_one_deadline(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C5: a load that does not answer at all ends the wait at SETTLE_TIMEOUT — not after a full request timeout
    for each of the SETTLE_ATTEMPTS Gets (about 70 s) — and its silence is no WARNING per Get."""
    hub = hub_of(init_answered)
    stop_answering(fake_link)
    await hub.set_lightness(LIGHT_DIMMER, 0x8000)
    started = time.monotonic()
    with (
        patch.object(coordinator, "SETTLE_TIMEOUT", 0.2),
        caplog.at_level(logging.DEBUG, logger="custom_components.junghome_ble"),
    ):
        assert not await hub.async_wait_settled(LIGHT_DIMMER, "dimmer")
    assert time.monotonic() - started < 2.0  # well inside one 3 s request timeout
    assert f"{LIGHT_DIMMER:04X} did not settle within" in caplog.text
    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "no response" in r.message
    ]


async def test_sensor_status_updates_socket_readings(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    hub = hub_of(init_integration)
    fake_link.inject(
        SOCKET_SENSOR,
        0xC001,
        sensor_status(
            (SENSOR_POWER, (1234).to_bytes(2, "little")),
            (SENSOR_VOLTAGE, (230).to_bytes(2, "little")),
        ),
    )
    fake_link.inject(
        SOCKET_SENSOR,
        0xC001,
        sensor_status((SENSOR_CURRENT, (537).to_bytes(2, "little")), (0x0042, b"\x01")),
    )
    fake_link.inject(SOCKET, 0xC000, onoff_status(True))
    await hass.async_block_till_done()
    st = hub.states[SOCKET]
    assert (st.power_w, st.voltage_v, st.current_a, st.on) == (123.4, 230.0, 5.37, True)
    assert (
        SOCKET_SENSOR not in hub.states
    )  # readings are stored on the socket, not on its sensor element
    assert hass.states.get(entity_id(hass, "switch", UID_SOCKET)).state == STATE_ON

    # the GSS "value is not known" markers clear a reading instead of showing 1.6 MW / 655 A; a value shorter than
    # its characteristic is its low bytes (as above: 2-byte values for the 3-byte Power), a truncated status
    # changes nothing
    fake_link.inject(
        SOCKET_SENSOR,
        0xC001,
        sensor_status(
            (SENSOR_POWER, b"\xff\xff\xff"),
            (SENSOR_CURRENT, b"\xff\xff"),
            (SENSOR_VOLTAGE, b"\xe7"),
        ),
    )
    await hass.async_block_till_done()
    assert (st.power_w, st.voltage_v, st.current_a) == (None, 231.0, None)
    fake_link.inject(
        SOCKET_SENSOR, 0xC001, sensor_status((SENSOR_POWER, b"\x0a\x00\x00"))[:-2]
    )
    await hass.async_block_till_done()
    assert st.power_w is None


async def test_unrelated_messages_are_ignored(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    hub = hub_of(init_integration)
    rocker = events_of(hub, ROCKER_A)
    fake_link.inject(
        LIGHT_SWITCH, OUR_ADDRESS, encode_opcode(M.GEN_DTT_STATUS) + b"\x00"
    )  # a SIG status we do not model
    fake_link.inject(
        LIGHT_SWITCH, OUR_ADDRESS, encode_opcode(M.GEN_ONOFF_STATUS)
    )  # status without parameters
    fake_link.inject(
        LIGHT_DIMMER, OUR_ADDRESS, encode_opcode(M.LIGHT_LIGHTNESS_STATUS) + b"\x01"
    )  # truncated lightness status
    fake_link.inject(
        LIGHT_CTL, OUR_ADDRESS, encode_opcode(M.LIGHT_CTL_STATUS) + b"\x01\x02\x03"
    )  # truncated CTL status
    fake_link.inject(
        LIGHT_SWITCH,
        OUR_ADDRESS,
        encode_opcode(0x11, M.JUNG_CID) + b"\x12\x50",
    )  # vendor property status without the access byte and value (config_entities.py handles complete ones)
    fake_link.inject(
        LIGHT_SWITCH, OUR_ADDRESS, encode_opcode(0x10, M.JUNG_CID) + b"\x03\x50\x01"
    )  # another vendor property
    fake_link.inject(
        LIGHT_SWITCH, OUR_ADDRESS, encode_opcode(0x10, 0x1234) + b"\x12\x50\x01\x05"
    )  # another manufacturer
    fake_link.inject(
        LIGHT_SWITCH, OUR_ADDRESS, sensor_status((SENSOR_POWER, b"\x01\x00"))
    )  # a sensor status from a load
    fake_link.inject(
        LIGHT_DIMMER,
        0xC044,
        encode_opcode(M.LIGHT_LIGHTNESS_RANGE_STATUS) + b"\x00\x01",
    )  # truncated setup status (config_entities.py handles complete ones)
    fake_link.inject(
        ROCKER_A, 0xC044, encode_opcode(M.GEN_ONOFF_SET_UNACK)
    )  # a set without parameters
    fake_link.inject(
        ROCKER_A, 0xC044, encode_opcode(M.SCENE_RECALL) + b"\x01"
    )  # too short
    await hass.async_block_till_done()
    assert hub.states == {}
    assert rocker == []


# --------------------------------------------------------------------------- status handler registry


@pytest.fixture
def scratch_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[MessageKey, StatusHandler]:
    """Let a test register handlers without leaking them into the module-wide table."""
    table = dict(STATUS_HANDLERS)
    monkeypatch.setattr(coordinator, "STATUS_HANDLERS", table)
    return table


def test_registry_has_one_row_per_built_in_message_type() -> None:
    assert set(STATUS_HANDLERS) == {
        (None, M.GEN_ONOFF_STATUS),
        (None, M.LIGHT_LIGHTNESS_STATUS),
        (None, M.LIGHT_CTL_STATUS),
        (None, M.LIGHT_CTL_TEMP_STATUS),
        (None, M.GEN_LEVEL_STATUS),
        (None, M.SENSOR_STATUS),
        (None, M.GEN_ONOFF_SET),
        (None, M.GEN_ONOFF_SET_UNACK),
        (None, M.SCENE_RECALL),
        (None, M.SCENE_RECALL_UNACK),
        (None, M.SCENE_STATUS),
        (None, M.SCENE_REGISTER_STATUS),
        *((None, op) for op in GENERIC_LEVEL_OPCODES),
        (None, M.LIGHT_CTL_TEMP_RANGE_STATUS),
        *((None, op) for op in SIG_PROPERTY_STATUS_OPCODES),
        (M.JUNG_CID, 0x10),
        (None, M.GEN_LEVEL_STATUS),  # the climate platform's handler (climate.py)
        # the config entities' handler for the three vendor property Status opcodes (config_entities.py)
        *((M.JUNG_CID, op) for op in M.VENDOR_PROPERTY_STATUS_OPCODES.values()),
        # the battery sensors' handler (sensor.py); the detector handlers of binary_sensor.py chain behind the
        # Sensor Status and OnOff Set rows above instead of adding rows
        (None, M.GEN_BATTERY_STATUS),
        # the fault register (binary_sensor.py's fault entities)
        (None, M.HEALTH_FAULT_STATUS),
        # the config entities' SIG setup states (config_entities.SETUP_STATES)
        (None, M.GEN_ONPOWERUP_STATUS),
        (None, M.LIGHT_LIGHTNESS_RANGE_STATUS),
        (None, M.LIGHT_LIGHTNESS_DEFAULT_STATUS),
        (None, M.LIGHT_CTL_DEFAULT_STATUS),
    }
    assert (
        STATUS_HANDLERS[None, M.GEN_ONOFF_SET]
        is STATUS_HANDLERS[None, M.GEN_ONOFF_SET_UNACK]
    )
    assert SIG_PROPERTY_STATUS_OPCODES == (
        M.GEN_USER_PROP_STATUS,
        M.GEN_ADMIN_PROP_STATUS,
        M.GEN_MANU_PROP_STATUS,
    )


async def test_element_signals_are_scoped_to_the_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """HAC-03: unicast addresses are only unique within one mesh, so an element's update signal names the entry
    too; another entry's element at the same address must not hear it."""
    hub = hub_of(init_integration)
    mine: list[int] = []
    other: list[int] = []
    async_dispatcher_connect(
        hass,
        SIGNAL_UPDATE.format(init_integration.entry_id, LIGHT_DIMMER),
        lambda: mine.append(1),
    )
    async_dispatcher_connect(
        hass, SIGNAL_UPDATE.format("other_entry", LIGHT_DIMMER), lambda: other.append(1)
    )
    hub.notify_update(LIGHT_DIMMER)
    await hass.async_block_till_done()
    assert mine == [1]
    assert other == []


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
        hub._link_lost.set()
        await settle(hass)
    assert signalled == []


async def test_messages_without_a_handler_are_ignored(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    scratch_handlers: dict[MessageKey, StatusHandler],
) -> None:
    """A message type with no row in the table changes nothing, however well-formed it is: no state, no signal."""
    hub = hub_of(init_integration)
    assert (None, M.GEN_DTT_STATUS) not in scratch_handlers
    signalled: list[None] = []
    async_dispatcher_connect(
        hass,
        SIGNAL_UPDATE.format(init_integration.entry_id, LIGHT_DIMMER),
        lambda: signalled.append(None),
    )
    fake_link.inject(
        LIGHT_DIMMER, OUR_ADDRESS, encode_opcode(M.GEN_DTT_STATUS) + b"\x40"
    )
    fake_link.inject(
        LIGHT_DIMMER,
        OUR_ADDRESS,
        encode_opcode(M.HEALTH_FAULT_STATUS, 0x1234) + b"\x01",
    )  # a vendor opcode that happens to share the number of a SIG one (0x05, which has a handler)
    await hass.async_block_till_done()
    assert hub.states == {}
    assert signalled == []
    assert hub._rx_to_us == 2  # counted for drop detection all the same


async def test_a_registered_handler_gets_the_hub_the_message_and_its_parameters(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    scratch_handlers: dict[MessageKey, StatusHandler],
) -> None:
    """A handler registered from outside (as a future module would) receives (hub, message, params) and reaches
    the element state through `element_state` / `notify_update`; vendor opcodes are keyed with their company id."""
    hub = hub_of(init_integration)
    calls: list[tuple[JungHomeHub, AccessMessage, bytes]] = []
    signalled: list[None] = []
    async_dispatcher_connect(
        hass,
        SIGNAL_UPDATE.format(init_integration.entry_id, LIGHT_DIMMER),
        lambda: signalled.append(None),
    )

    @register_status_handler(M.GEN_DTT_STATUS)
    def on_transition_time(h: JungHomeHub, m: AccessMessage, p: bytes) -> None:
        calls.append((h, m, p))
        h.element_state(m.src).level = int.from_bytes(p[:2], "little", signed=True)
        h.notify_update(m.src)

    @register_status_handler(0x11, company_id=M.JUNG_CID)
    def on_property_status(h: JungHomeHub, m: AccessMessage, p: bytes) -> None:
        h.element_state(m.src).properties[int.from_bytes(p[:2], "little")] = p[2:]

    assert scratch_handlers[None, M.GEN_DTT_STATUS] is on_transition_time
    assert scratch_handlers[M.JUNG_CID, 0x11] is on_property_status
    assert (
        None,
        M.GEN_DTT_STATUS,
    ) not in STATUS_HANDLERS  # the real table is untouched

    level = (-1234).to_bytes(2, "little", signed=True)
    fake_link.inject(LIGHT_DIMMER, OUR_ADDRESS, encode_opcode(M.GEN_DTT_STATUS) + level)
    await hass.async_block_till_done()
    assert len(calls) == 1
    h, m, p = calls[0]
    assert h is hub
    assert (m.src, m.dst, m.opcode, m.company_id) == (
        LIGHT_DIMMER,
        OUR_ADDRESS,
        M.GEN_DTT_STATUS,
        None,
    )
    assert p == level == m.params
    assert hub.states[LIGHT_DIMMER].level == -1234
    assert hub.states[LIGHT_DIMMER].on is None  # nothing else was touched
    assert signalled == [None]

    fake_link.inject(
        LIGHT_SWITCH, OUR_ADDRESS, encode_opcode(0x11, M.JUNG_CID) + b"\x03\x50\x06"
    )
    fake_link.inject(
        LIGHT_SWITCH, OUR_ADDRESS, encode_opcode(0x11, 0x1234) + b"\x03\x50\x07"
    )  # another manufacturer's 0x11: not ours
    await hass.async_block_till_done()
    assert hub.states[LIGHT_SWITCH].properties == {0x5003: b"\x06"}
    assert len(calls) == 1


# --------------------------------------------------------------------------- buttons


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


ROCKER_DOWN_CLICK, ROCKER_UP_CLICK, ROCKER_DOWN_HOLD, ROCKER_UP_HOLD = (
    0x00,
    0x01,
    0x02,
    0x03,
)


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


# --------------------------------------------------------------------------- beacons


async def test_key_refresh_beacon_raises_a_repair_issue(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    fake_link.inject_beacon(iv_index=0, key_refresh=False)
    await hass.async_block_till_done()
    assert find_issue(hass, ISSUE_KEY_REFRESH) is None

    # review-3 T3: flagged and authenticated means our keys are the new ones already — nothing to do
    fake_link.inject_beacon(iv_index=0, key_refresh=True)
    await hass.async_block_till_done()
    assert find_issue(hass, ISSUE_KEY_REFRESH) is None

    # Phase 2 beacons are secured with the new key: the one our key cannot open is the sign
    fake_link.inject_beacon(iv_index=0, key_refresh=True, new_key=True)
    await hass.async_block_till_done()
    issue = find_issue(hass, ISSUE_KEY_REFRESH)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert not issue.is_fixable
    assert issue.translation_key == ISSUE_KEY_REFRESH
    assert issue.translation_placeholders == {"title": "JUNG HOME mesh test"}


async def test_key_refresh_issue_is_cleared_by_a_successful_restart(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The remedy is a reconfiguration with a new export, which reloads the entry."""
    fake_link.inject_beacon(key_refresh=True, new_key=True)
    assert find_issue(hass, ISSUE_KEY_REFRESH) is not None

    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await wait_for_link(hass, init_integration)
    assert find_issue(hass, ISSUE_KEY_REFRESH) is None

    fake_link.inject_beacon(
        key_refresh=True, new_key=True
    )  # a mesh that is still refreshing raises it again
    assert find_issue(hass, ISSUE_KEY_REFRESH) is not None


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
    hub._report_pdus_dropped(True)
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is not None
    answer_gets(fake_link)
    await hub._refresh_all()
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
    assert LIGHT_CTL not in hub._recheck
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


def wall_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The wall clock the IV Update timing reads (`LocalState.apply_beacon`), moved by the test: `clock[0] += …`."""
    clock = [1_000_000.0]
    monkeypatch.setattr(client_mod, "_wall_now", lambda: clock[0])
    return clock


async def test_iv_update_beacon_changes_local_state(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = wall_clock(monkeypatch)
    state = hub_of(init_integration).proxy.state
    assert (state.iv_index, state.iv_update_active) == (0, False)
    fake_link.inject_beacon(iv_index=1, iv_update=True)
    assert (state.iv_index, state.iv_update_active, state.tx_iv_index) == (1, True, 0)
    fake_link.inject_beacon(iv_index=1, iv_update=False)  # too soon: still in progress
    assert (state.iv_index, state.iv_update_active, state.tx_iv_index) == (1, True, 0)
    clock[0] += client_mod.IV_UPDATE_MIN_STATE
    fake_link.inject_beacon(iv_index=1, iv_update=False)
    assert (state.iv_index, state.iv_update_active, state.tx_iv_index) == (1, False, 1)
    assert state.seq == 0  # sequence numbers restart with the new transmit IV index
    assert find_issue(hass, ISSUE_KEY_REFRESH) is None


async def test_iv_update_beacon_through_the_hub_persists_the_restart(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same sequence as `test_iv_update_beacon_changes_local_state`, but delivered as the fake proxy's own
    beacons through the hub (not called on `LocalState` directly): the transmit IV index follows and the
    sequence restart reaches the mesh's store (`coordinator.seq_store`), not just the in-memory state — an IV
    change is written at once, unlike the debounced sequence-number saves (`HAState.persist`)."""
    clock = wall_clock(monkeypatch)
    hub = hub_of(init_integration)
    state = hub.proxy.state
    await hass.async_block_till_done()
    stored = hass_storage[SEQ_STORE_KEY]["data"]["addresses"]["0D00"]
    assert (stored["iv_index"], stored["iv_update_active"]) == (0, False)

    fake_link.inject_beacon(
        iv_index=1, iv_update=True
    )  # in progress: still transmitting under index 0
    assert state.tx_iv_index == 0
    await hass.async_block_till_done()
    stored = hass_storage[SEQ_STORE_KEY]["data"]["addresses"]["0D00"]
    assert (
        stored["iv_index"] == 0
    )  # the transmit index has not moved: not urgent, only debounced

    peak = state.seq
    clock[0] += client_mod.IV_UPDATE_MIN_STATE
    fake_link.inject_beacon(
        iv_index=1, iv_update=False
    )  # normal operation, 96 hours later: the mesh has moved on
    assert (state.tx_iv_index, state.seq) == (1, 0)
    await hass.async_block_till_done()
    stored = hass_storage[SEQ_STORE_KEY]["data"]["addresses"]["0D00"]
    assert {k: v for k, v in stored.items() if k != "rpl"} == {
        "seq": 0,
        "iv_index": 1,
        "iv_update_active": False,
        "iv_known": True,
        "clean": False,
        "seq_peak": peak,
        "seq_peak_from": 0,
        "iv_changed_at": clock[0],
    }


# --------------------------------------------------------------------------- segmented sends


async def test_segmented_config_send_is_acknowledged(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A Config message over 11 bytes (Model Publication Set, §4.3.2.16, always segmented) goes out as lower
    transport segments the fake node acknowledges as a real one would (§3.5.3.3): the send completes, the mesh
    reassembles exactly what was sent, and every write it saw was acked."""
    hub = hub_of(init_integration)
    src = hub.proxy.state.src
    fake_link.segments.clear()
    fake_link.acked.clear()
    pdu = C.model_publication_set(
        LIGHT_SWITCH, GROUP_SWITCH, 0x1000
    )  # Generic OnOff Server
    assert len(pdu) > 11  # actually segmented, not the unsegmented boundary case
    await hub.proxy.send_config(LIGHT_SWITCH, pdu)
    assert fake_link.config_sent[-1] == (src, LIGHT_SWITCH, pdu)
    seg_os = {
        seg_o
        for seg_src, _dst, _seq_zero, seg_o in fake_link.segments
        if seg_src == src
    }
    assert seg_os == {0, 1}  # both segments of this 12-byte access PDU went out
    assert fake_link.acked  # the fake node sent at least one Segment Ack back
    assert fake_link.acked[-1][:2] == (LIGHT_SWITCH, src)


async def test_segmented_send_to_a_node_that_never_acks_times_out(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A node that never sends a Segment Acknowledgment (§3.5.3.3) makes the send exhaust its retries and raise —
    even though the node itself received every segment and reassembled the message on the very first attempt
    (a real node applies a segmented message it got in full and answers all the same, only the ack is lost;
    the sender has no way to tell the two apart, which is exactly why it keeps retrying)."""
    monkeypatch.setattr(client_mod, "SEGMENT_ACK_TIMEOUT", 0.01)
    hub = hub_of(init_integration)
    src = hub.proxy.state.src
    fake_link.ack_segments = False
    fake_link.segments.clear()
    pdu = C.model_publication_set(LIGHT_SWITCH, GROUP_SWITCH, 0x1000)
    with pytest.raises(TimeoutError, match="not acknowledged"):
        await hub.proxy.send_config(LIGHT_SWITCH, pdu)
    assert (
        len(fake_link.segments) == 2 * client_mod.SEGMENT_RETRIES
    )  # both segments, every attempt
    assert {seq_zero for _src, _dst, seq_zero, _seg_o in fake_link.segments} == {
        fake_link.segments[0][2]
    }  # same SeqAuth throughout: the IV index never moved, so there was no restart
    assert fake_link.acked == []  # the fake node never sent a Segment Ack
    assert fake_link.config_sent == [
        (src, LIGHT_SWITCH, pdu)
    ]  # ... yet it did receive and reassemble the message


# --------------------------------------------------------------------------- other proxy nodes


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


# --------------------------------------------------------------------------- commands


async def test_commands_are_clamped(
    hass: HomeAssistant, init_answered: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    hub = hub_of(init_answered)
    fake_link.sent.clear()
    await hub.set_lightness(LIGHT_DIMMER, 70000)
    await hub.set_lightness(LIGHT_DIMMER, -1)
    await hub.set_ctl(LIGHT_CTL, 70000, 2700)
    await hub.set_onoff(LIGHT_SWITCH, True)
    await hub.recall_scene(2)
    sent = fake_link.sent
    assert [(src, dst) for src, dst, _ in sent] == [
        (OUR_ADDRESS, dst)
        for dst in (LIGHT_DIMMER, LIGHT_DIMMER, LIGHT_CTL, LIGHT_SWITCH, ALL_NODES)
    ]
    assert [
        pdu for _, _, pdu in sent
    ] == [  # the TID is taken from the message: it is a running counter
        M.light_lightness_set(65535, tid=sent[0][2][4]),
        M.light_lightness_set(0, tid=sent[1][2][4]),
        M.light_ctl_set(65535, 2700, tid=sent[2][2][8]),
        M.generic_onoff_set(True, tid=sent[3][2][3], transition=0),
        M.scene_recall(2, ack=False, tid=sent[4][2][4]),
    ]


async def test_time_set_repeats_daily_while_connected(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Device timers have no other clock: the gateway never publishes time, so we do, once a day and after every connection."""
    hub = hub_of(init_answered)
    assert [dst for _, dst, _ in fake_link.sent].count(
        ALL_NODES
    ) == BROADCASTS  # the Time Set and location after the refresh
    assert hub._unsub_energy is not None
    hub._unsub_energy()  # a day of ticks would also fire the energy poll; it has its own tests
    hub._unsub_energy = None
    fake_link.sent.clear()

    # day-long jumps would trip the link watchdog (a silent proxy is another test); keep it out of the way
    with patch(
        "custom_components.junghome_ble.coordinator.LINK_IDLE_TIMEOUT",
        10 * TIME_SET_INTERVAL,
    ):
        freezer.tick(TIME_SET_INTERVAL - 60)
        async_fire_time_changed(hass)
        await settle(hass)
        assert not fake_link.sent  # not yet

        freezer.tick(120)
        async_fire_time_changed(hass)
        await settle(hass)
        assert len(fake_link.sent) == 1
        assert_time_set(fake_link.sent[0], dt_util.now())

        # nothing goes out while the link is down, and a failing send is only logged
        fake_link.sent.clear()
        with patch.object(
            JungHomeHub, "connected", new_callable=PropertyMock, return_value=False
        ):
            freezer.tick(TIME_SET_INTERVAL)
            async_fire_time_changed(hass)
            await settle(hass)
        assert not fake_link.sent

    fake_link.write_error = ConnectionError("gone")
    await hub._send_time()
    await hub._send_location()
    assert not fake_link.sent


async def test_time_set_falls_back_to_utc_for_an_odd_zone(
    hass: HomeAssistant, init_answered: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A zone offset Time Set cannot carry (not a quarter hour) still gets the devices a UTC clock."""
    odd = datetime(2026, 1, 15, 12, 0, tzinfo=timezone(timedelta(minutes=7)))
    fake_link.sent.clear()
    with patch(
        "custom_components.junghome_ble.coordinator.dt_util.now", return_value=odd
    ):
        await hub_of(init_answered)._send_time()
    assert len(fake_link.sent) == 1
    assert_time_set(fake_link.sent[0], odd, zone=timedelta(0))


async def test_time_set_goes_out_before_the_refresh(
    hass: HomeAssistant, init_answered: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Review-4 R I-5: Time Set and the location follow the proxy filter, before the refresh — sent after a complete
    refresh, they never went out on a link lost before it was through. A refresh cut short sends nothing more."""
    hub = hub_of(init_answered)
    fake_link.sent.clear()
    with patch.object(hub, "_refresh_all", AsyncMock(return_value=False)):
        await hub._after_connect()
    assert [dst for _, dst, _ in fake_link.sent] == [ALL_NODES] * BROADCASTS
    assert_time_set(fake_link.sent[0], dt_util.now())
    # a link already gone: nothing goes out, and nothing is raised
    fake_link.sent.clear()
    fake_link.write_error = ConnectionError("proxy disconnected")
    await hub._after_connect()
    assert not fake_link.sent


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
        hub._previous_link = coordinator.LinkEnd("the proxy disconnected", None, lasted)
        fake_link.sent.clear()
        await hub._after_connect()
        return fake_link.sent

    with caplog.at_level(logging.DEBUG, logger="custom_components.junghome_ble"):
        sent = await link_after(coordinator.SHORT_LINK)
    assert len(sent) == BROADCASTS + REFRESH_GETS + ENERGY_GETS
    assert sent[-ENERGY_GETS:] == COUNTER_GETS
    assert "faults read 0 s ago: not asked again on this link" in caplog.text
    # after a short link: everything again
    assert (await link_after(coordinator.SHORT_LINK - 1))[-CONNECT_TAIL:] == tail
    # past the window: again
    for name in hub._connect_steps_done:
        hub._connect_steps_done[name] -= coordinator.CONNECT_STEP_FRESH
    assert (await link_after(coordinator.SHORT_LINK))[-CONNECT_TAIL:] == tail
    # a round the link cut short does not count: the next link reads again
    for name in hub._connect_steps_done:
        hub._connect_steps_done[name] -= coordinator.CONNECT_STEP_FRESH
    with patch.object(hub, "_get_faults", AsyncMock(return_value=False)):
        await link_after(coordinator.SHORT_LINK)
    assert (await link_after(coordinator.SHORT_LINK))[-FAULT_GETS:] == FAULT_GETS_PDUS


def stall_seq(hub: JungHomeHub, refusals: int) -> list[int]:
    """Make the hub's sequence-number store refuse the next `refusals` reservations, as `HAState.reserve_seq`
    does while a save has not landed; returns the list the refused reservation sizes are appended to."""
    reserve = hub.state.reserve_seq
    refused: list[int] = []

    def stalling(count: int) -> int:
        if len(refused) < refusals:
            refused.append(count)
            raise client_mod.SequenceStalled(
                "sequence-number store not written yet: holding back to keep nonces unique"
            )
        return reserve(count)

    hub.state.reserve_seq = stalling  # type: ignore[method-assign]
    return refused


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
    await hub._after_connect()
    assert len(refused) == 3
    assert fast_sleep.count(coordinator.SEQ_STALL_RETRY) == 3
    assert unicasts() == expected
    assert [dst for _src, dst, _pdu in fake_link.sent].count(ALL_NODES) == BROADCASTS
    assert fake_link.sent[-FAULT_GETS - CURRENT_SCENE_GETS : -CURRENT_SCENE_GETS] == (
        FAULT_GETS_PDUS
    )


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
    real = JungHomeHub.visible_proxies

    def flaky(self: JungHomeHub) -> list[Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("scanner exploded")
        return real(self)

    with patch.object(JungHomeHub, "visible_proxies", flaky):
        fake_link.drop_link()
        await wait_until(hass, lambda: calls >= 2, what="the next pass")
        await wait_for_link(hass, init_answered)
    assert "Unexpected error in the JUNG mesh connection loop" in caplog.text
    assert coordinator.CONNECT_BACKOFF_MAX in fast_sleep
    assert hub.connected


async def test_a_stalled_store_on_a_lost_link_is_a_lost_link(
    hass: HomeAssistant, init_answered: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """C2: without a link the refusal is not waited out: the send fails like any other on a lost link."""
    hub = hub_of(init_answered)
    fake_link.sent.clear()
    stall_seq(hub, 1)
    with patch.object(
        JungHomeHub, "connected", new_callable=PropertyMock, return_value=False
    ):
        await hub._send_time()
    await hub._send_location()  # the one refusal is used up: this goes out
    assert [dst for _src, dst, _pdu in fake_link.sent] == [ALL_NODES]


# --------------------------------------------------------------------------- energy poll


async def test_energy_poll_repeats_while_connected(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """The counter is read after the connect-time refresh and every ENERGY_POLL_INTERVAL while a link is up."""
    hub = hub_of(init_answered)
    assert fake_link.sent[-AFTER_ENERGY:-CONNECT_TAIL] == COUNTER_GETS
    assert hub.states[SOCKET].power_on_hours == 42
    fake_link.sent.clear()

    # the poll interval is as long as the link watchdog's patience (a silent proxy is another test); keep it out of the way
    with patch(
        "custom_components.junghome_ble.coordinator.LINK_IDLE_TIMEOUT",
        10 * ENERGY_POLL_INTERVAL,
    ):
        freezer.tick(ENERGY_POLL_INTERVAL - 60)
        async_fire_time_changed(hass)
        await settle(hass)
        assert not fake_link.sent  # not yet

        STATE_REPLIES[HOURS_GET] = admin_property_status(
            PROPERTY_POWER_ON_TIME, (43).to_bytes(3, "little")
        )
        try:
            freezer.tick(120)
            async_fire_time_changed(hass)
            await settle(hass)
        finally:
            STATE_REPLIES[HOURS_GET] = admin_property_status(
                PROPERTY_POWER_ON_TIME, (42).to_bytes(3, "little")
            )
        assert fake_link.sent == COUNTER_GETS
        assert hub.states[SOCKET].power_on_hours == 43
        assert hub._energy_task is not None
        assert hub._energy_task.done()

        # nothing is asked while the link is down
        fake_link.sent.clear()
        with patch.object(
            JungHomeHub, "connected", new_callable=PropertyMock, return_value=False
        ):
            freezer.tick(ENERGY_POLL_INTERVAL)
            async_fire_time_changed(hass)
            await settle(hass)
        assert not fake_link.sent

        # ... nor while a poll (or the connect-time refresh that ends with one) is still running
        hub._energy_task = running = never_done(hass)
        freezer.tick(ENERGY_POLL_INTERVAL)
        async_fire_time_changed(hass)
        await settle(hass)
        assert not fake_link.sent
        assert hub._energy_task is running
        running.cancel()
        await settle(hass)

        # stopping the hub unsubscribes the timer and cancels a running poll
        hub._energy_task = task = never_done(hass)
        await hub.async_stop()
        assert task.cancelled()
        assert hub._energy_task is None
        assert hub._unsub_energy is None
        fake_link.sent.clear()
        freezer.tick(ENERGY_POLL_INTERVAL)
        async_fire_time_changed(hass)
        await settle(hass)
        assert not fake_link.sent


async def test_update_entity_reads_the_meter_now(
    hass: HomeAssistant, init_answered: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """`homeassistant.update_entity` on a meter sensor reads the socket's readings and counters at once, not at the
    next ENERGY_POLL_INTERVAL: the app's consumption page reads them every 5 s (`requestData`)."""
    hub = hub_of(init_answered)
    readings = [
        (OUR_ADDRESS, SOCKET_SENSOR, M.sensor_get(pid))
        for pid in (SENSOR_POWER, SENSOR_VOLTAGE, SENSOR_CURRENT)
    ]
    STATE_REPLIES[HOURS_GET] = admin_property_status(
        PROPERTY_POWER_ON_TIME, (44).to_bytes(3, "little")
    )
    try:
        fake_link.sent.clear()
        await hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": entity_id(hass, "sensor", f"{UID_SOCKET}-power")},
            blocking=True,
        )
        await settle(hass)
    finally:
        STATE_REPLIES[HOURS_GET] = admin_property_status(
            PROPERTY_POWER_ON_TIME, (42).to_bytes(3, "little")
        )
    assert fake_link.sent == readings + COUNTER_GETS
    assert hub.states[SOCKET].power_on_hours == 44

    # asked twice at once (every sensor of the load updated together): one read, the second call waits for it
    socket = hub.devices.by_address[SOCKET]
    gate = asyncio.Event()
    real_readings = hub._get_readings

    async def held(load: Any) -> None:
        await gate.wait()
        await real_readings(load)

    fake_link.sent.clear()
    with patch.object(hub, "_get_readings", held):
        first = hass.async_create_task(hub.async_refresh_meter(socket))
        await asyncio.sleep(0)
        second = hass.async_create_task(hub.async_refresh_meter(socket))
        await asyncio.sleep(0)
        assert not second.done()
        gate.set()
        await first
        await second
    assert fake_link.sent == readings + COUNTER_GETS
    assert not hub._meter_refreshes

    # no link: nothing raised, the cached values stay
    hub.states[SOCKET].power_on_hours = 44
    fake_link.write_error = OSError("GATT write failed")
    try:
        await hub.async_refresh_meter(socket)
    finally:
        fake_link.write_error = None
    assert hub.states[SOCKET].power_on_hours == 44
    assert not hub._meter_refreshes


async def test_energy_poll_is_cancelled_with_the_link(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    """A poll that the link outlives is cancelled with the link's refresh, and a failing send is only logged."""
    hub = hub_of(init_answered)
    hub._energy_task = task = never_done(hass)
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert task.cancelled()
    assert hub._energy_task is None

    fake_link.sent.clear()
    fake_link.write_error = ConnectionError("proxy disconnected")
    await hub._poll_energy()  # the link went away between the tick and the send
    assert not fake_link.sent


async def test_energy_poll_survives_an_unanswered_socket(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A socket that does not answer costs one attempt per counter (no retries), then the poll moves on."""
    hub = hub_of(init_answered)
    replies = {get: STATE_REPLIES.pop(get) for _src, _dst, get in COUNTER_GETS}
    fake_link.sent.clear()
    try:
        poll = hass.async_create_background_task(hub._poll_energy(), "poll")
        await settle(hass)
        assert fake_link.sent == COUNTER_GETS[:1]
        for n in range(
            2, len(COUNTER_GETS) + 1
        ):  # each timeout moves on to the next counter, no retry
            freezer.tick(3.1)
            async_fire_time_changed(hass)
            await settle(hass)
            assert fake_link.sent == COUNTER_GETS[:n]
        freezer.tick(3.1)
        async_fire_time_changed(hass)
        await settle(hass)
        assert fake_link.sent == COUNTER_GETS
        assert poll.done()
    finally:
        STATE_REPLIES.update(replies)
    assert "0172 did not answer its property Get 006D" in caplog.text
    assert "0173 did not answer its property Get 0072" in caplog.text
    assert hub.states[SOCKET].power_on_hours == 42  # the connect-time values stay
    assert hub.states[SOCKET].energy_wh == 210198


async def test_energy_poll_is_anchored_on_the_connection(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """The poll grid restarts with every link: the connect-time poll is tick 0, the next one ENERGY_POLL_INTERVAL later,
    wherever the integration's own start was (so a poll never lands right at the link watchdog's deadline)."""
    hub = hub_of(init_answered)
    timer = hub._unsub_energy
    assert timer is not None
    await tick(hass, freezer, 200)
    fake_link.drop_link()
    await settle(hass)
    await wait_for_link(hass, init_answered)
    assert fake_link.connect_count == 2
    assert hub._unsub_energy is not None
    assert (
        hub._unsub_energy is not timer
    )  # re-armed by the new link, the old timer is gone
    # the connect-time poll of the new link; the scene and fault reads of the first, a link that held, are fresh
    assert fake_link.sent[-ENERGY_GETS:] == COUNTER_GETS
    fake_link.sent.clear()

    await tick(
        hass, freezer, ENERGY_POLL_INTERVAL - 60
    )  # 500 s after the start: the old grid would poll now
    assert not fake_link.sent
    await tick(
        hass, freezer, 120
    )  # 360 s after the new link came up: the new grid does
    assert fake_link.sent == COUNTER_GETS


async def test_energy_poll_skips_meshes_without_a_metering_socket(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Without a metering socket there is nothing to poll, so no connection arms a timer."""
    hub = hub_of(init_integration)
    assert hub._unsub_energy is not None
    await hub.async_stop()
    hub.devices.sockets.clear()
    hub._stop = False
    await hub.async_start()
    await wait_for_link(hass, init_integration)
    assert hub.connected
    assert hub._unsub_energy is None
    await hub.async_stop()


async def test_sig_property_status_decoding(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The three SIG property Status opcodes feed the power-on-hour counter; all-ones and empty values mean "unknown"."""
    hub = hub_of(init_integration)

    fake_link.inject(
        SOCKET,
        OUR_ADDRESS,
        admin_property_status(
            PROPERTY_POWER_ON_TIME, (0x0102030405).to_bytes(5, "little")
        ),
    )  # the codec takes whatever length the firmware sends
    await hass.async_block_till_done()
    st = hub.states[SOCKET]
    assert st.power_on_hours == 0x0102030405
    fake_link.inject(
        SOCKET,
        OUR_ADDRESS,
        admin_property_status(
            PROPERTY_POWER_ON_TIME,
            (7).to_bytes(3, "little"),
            opcode=M.GEN_USER_PROP_STATUS,
        ),
    )
    await hass.async_block_till_done()
    assert st.power_on_hours == 7

    fake_link.inject(
        SOCKET,
        OUR_ADDRESS,
        admin_property_status(
            PROPERTY_POWER_ON_TIME, b"\xff\xff\xff", opcode=M.GEN_MANU_PROP_STATUS
        ),
    )  # the firmware's "unknown"
    await hass.async_block_till_done()
    assert st.power_on_hours is None
    st.power_on_hours = 7
    fake_link.inject(
        SOCKET_SENSOR,
        OUR_ADDRESS,
        admin_property_status(
            PROPERTY_PRECISE_TOTAL_ENERGY,
            b"\xfe\xff\xff\xff",
            opcode=M.GEN_MANU_PROP_STATUS,
        ),
    )  # the GSS "value is not valid" marker (Energy32 0xFFFFFFFE): never 4.29 GWh in a total_increasing sensor
    await hass.async_block_till_done()
    assert st.energy_wh is None
    st.power_on_hours = 7
    fake_link.inject(
        SOCKET, OUR_ADDRESS, admin_property_status(PROPERTY_POWER_ON_TIME, b"")
    )  # no value at all, `[pid][access]`: dropped as the app drops it (`AbstractC1929k1`), the last value stays
    await hass.async_block_till_done()
    assert st.power_on_hours == 7

    # other SIG properties and truncated statuses are ignored: the pid-only status an element answers a Get for a
    # property it does not have with (0x006A asked of the socket's main element), the software version, ...
    st.power_on_hours = 1
    st.energy_resettable_wh = 5
    fake_link.inject(SOCKET, OUR_ADDRESS, property_absent_status(PROPERTY_TOTAL_ENERGY))
    fake_link.inject(
        SOCKET,
        OUR_ADDRESS,
        property_absent_status(PROPERTY_TOTAL_ENERGY, opcode=M.GEN_USER_PROP_STATUS),
    )
    fake_link.inject(
        SOCKET, OUR_ADDRESS, admin_property_status(0x001A, b"2.2.0.2")
    )  # software version
    fake_link.inject(
        SOCKET, OUR_ADDRESS, encode_opcode(M.GEN_ADMIN_PROP_STATUS) + b"\x6d"
    )  # truncated
    fake_link.inject(
        LIGHT_SWITCH, OUR_ADDRESS, encode_opcode(M.GEN_ADMIN_PROP_STATUS)
    )  # empty
    await hass.async_block_till_done()
    assert st.power_on_hours == 1
    assert st.energy_resettable_wh == 5
    assert LIGHT_SWITCH not in hub.states
    # a counter from an element that is no socket's meter lands on that element itself
    fake_link.inject(
        LIGHT_SWITCH,
        OUR_ADDRESS,
        admin_property_status(PROPERTY_TOTAL_ENERGY, (9).to_bytes(4, "little")),
    )
    await hass.async_block_till_done()
    assert hub.states[LIGHT_SWITCH].energy_resettable_wh == 9


# --------------------------------------------------------------------------- click delay option


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


# --------------------------------------------------------------------------- stale export / proxy node timing


def inject_foreign(link: FakeProxyLink, count: int, *, beacon: bool = False) -> None:
    """Deliver traffic encrypted with another network's keys: what a proxy forwards after a completed key refresh."""
    foreign = NetKeyMaterial.derive(bytes(range(16)))
    for _ in range(count):
        link._deliver(
            PROXY_NETWORK_PDU,
            network_encrypt(
                foreign,
                0,
                False,
                3,
                link._next(),
                LIGHT_SWITCH,
                GROUP_SWITCH,
                b"\x00" + bytes(8),
            ),
        )
    if beacon:
        body = b"\x00" + foreign.network_id + bytes(4)
        link._deliver(
            PROXY_BEACON, b"\x01" + body + aes_cmac(foreign.beacon_key, body)[:8]
        )


async def test_undecryptable_traffic_alone_raises_the_export_stale_repair(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A link that forwards only PDUs our keys cannot open (and a beacon they cannot authenticate) is a mesh whose keys changed."""
    hub = hub_of(init_integration)
    assert hub.proxy.rx_undecryptable == 0

    inject_foreign(
        fake_link, EXPORT_STALE_THRESHOLD - 2, beacon=True
    )  # 19 with the beacon: one short
    await hass.async_block_till_done()
    assert hub.proxy.rx_undecryptable == EXPORT_STALE_THRESHOLD - 2
    assert find_issue(hass, ISSUE_EXPORT_STALE) is None

    inject_foreign(fake_link, 1)
    await hass.async_block_till_done()
    issue = find_issue(hass, ISSUE_EXPORT_STALE)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert not issue.is_fixable
    assert issue.translation_key == ISSUE_EXPORT_STALE
    assert issue.translation_placeholders == {"title": "JUNG HOME mesh test"}
    assert (
        f"Nothing heard through proxy node {PROXY_ADDRESS} can be decrypted with the keys of the export "
        f"({EXPORT_STALE_THRESHOLD} messages so far)" in caplog.text
    )
    inject_foreign(fake_link, 5)  # raised once, not on every further PDU
    await hass.async_block_till_done()
    assert caplog.text.count("can be decrypted with the keys of the export") == 1

    # the first message our keys open proves the export fits: the issue goes away
    fake_link.inject(LIGHT_SWITCH, GROUP_SWITCH, onoff_status(True))
    await hass.async_block_till_done()
    assert find_issue(hass, ISSUE_EXPORT_STALE) is None
    assert hub.states[LIGHT_SWITCH].on is True


async def test_undecryptable_traffic_next_to_decodable_traffic_is_no_stale_export(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Another mesh in range, or a node the export does not know: as long as something decodes, the keys are fine."""
    fake_link.inject(LIGHT_SWITCH, GROUP_SWITCH, onoff_status(False))
    inject_foreign(fake_link, EXPORT_STALE_THRESHOLD * 2, beacon=True)
    await hass.async_block_till_done()
    assert find_issue(hass, ISSUE_EXPORT_STALE) is None
    # an authenticated beacon is not "undecodable" either
    fake_link.inject_beacon()
    hub = hub_of(init_integration)
    assert hub._rx_undecodable_link == EXPORT_STALE_THRESHOLD * 2 + 1


async def test_export_stale_counts_restart_with_every_link(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """A reconnect starts from zero: the old link's undecryptable PDUs do not count against the new one (and vice versa)."""
    hub = hub_of(init_integration)
    inject_foreign(fake_link, EXPORT_STALE_THRESHOLD - 1)
    mock_bluetooth_env["infos"].append(
        make_service_info(network_id, address=SECOND_PROXY, rssi=-40)
    )
    fake_link.drop_link()
    await wait_for_link(hass, init_integration, connected=False)
    await wait_for_link(hass, init_integration)
    assert hub.proxy_address == SECOND_PROXY
    assert (hub._rx_undecodable_link, hub.proxy.rx_undecryptable) == (0, 0)
    inject_foreign(fake_link, EXPORT_STALE_THRESHOLD - 1)
    await hass.async_block_till_done()
    assert find_issue(hass, ISSUE_EXPORT_STALE) is None
    inject_foreign(fake_link, 1)
    await hass.async_block_till_done()
    assert find_issue(hass, ISSUE_EXPORT_STALE) is not None

    # a successful restart (the reconfiguration with a fresh export reloads the entry) clears it
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await wait_for_link(hass, init_integration)
    assert find_issue(hass, ISSUE_EXPORT_STALE) is None


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
    hub._set_available(True)
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


# --------------------------------------------------------------------------- Filter Status watchdog


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
    assert hub._unsub_filter_watch is not None
    fake_link.inject_beacon()

    await tick(hass, freezer, FILTER_STATUS_TIMEOUT - 1)
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None
    await tick(hass, freezer, 1.1)
    assert hub._unsub_filter_watch is None
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
    assert hub._unsub_filter_watch is None
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
    assert hub._unsub_filter_watch is not None
    assert (
        hub._beacon_authenticated is False
    )  # the previous link's beacon does not count for this one
    fake_link.inject_beacon()
    mock_bluetooth_env["infos"] = []  # nothing to reconnect to
    fake_link.drop_link()  # a lost link takes its watchdog with it
    await settle(hass)
    assert not hub.connected
    assert hub._unsub_filter_watch is None
    await tick(hass, freezer, FILTER_STATUS_TIMEOUT + 0.1)
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None


async def test_filter_status_watchdog_is_not_armed_when_the_proxy_answers(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The fake proxy answers the filter request during `attach()`: nothing to wait for."""
    hub = hub_of(init_integration)
    assert hub.proxy_node == PROXY_NODE
    assert hub._unsub_filter_watch is None


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
    assert hub._unsub_filter_watch is not None
    fake_link.inject_beacon()
    await hub.async_stop()
    assert hub._unsub_filter_watch is None
    await tick(hass, freezer, FILTER_STATUS_TIMEOUT + 0.1)
    assert find_issue(hass, ISSUE_PDUS_DROPPED) is None


# --------------------------------------------------------------------------- heartbeats (per-node liveness)

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
    assert hub._unsub_heartbeats is None
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
    assert hub._heartbeats_configured_at is not None
    assert set(hub._alive_deadline) == set(HEARTBEAT_NODES)
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
        patch(
            "custom_components.junghome_ble.coordinator.LINK_IDLE_TIMEOUT", 10 * 3600.0
        ),
        patch.object(hub, "_reprobe_dead", AsyncMock()),
    ):
        await tick(hass, freezer, HEARTBEAT_RECONFIGURE_INTERVAL - 60)
        assert fake_link.config_sent == []
        await tick(hass, freezer, 90)
        assert [pdu for _s, _n, pdu in fake_link.config_sent] == [HEARTBEAT_SET] * len(
            HEARTBEAT_NODES
        )
        assert hub._heartbeat_task is not None
        assert hub._heartbeat_task.done()
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
        patch(
            "custom_components.junghome_ble.coordinator.LINK_IDLE_TIMEOUT", 10 * 3600.0
        ),
        patch.object(hub, "_reprobe_dead", AsyncMock()),
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
    hub._mark_alive(0x0BBB)
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
        "custom_components.junghome_ble.coordinator.LINK_IDLE_TIMEOUT", 10 * 3600.0
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
        await hub._reprobe_dead(hub.heartbeat_nodes[:1])
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
        "custom_components.junghome_ble.coordinator.LINK_IDLE_TIMEOUT", 10 * 3600.0
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
        hub._heartbeats_configured_at = time.monotonic() + 1
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
    assert set(hub._alive_deadline) == set(HEARTBEAT_NODES)
    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert not hub.connected
    with (
        patch(
            "custom_components.junghome_ble.coordinator.LINK_IDLE_TIMEOUT", 10 * 3600.0
        ),
        patch.object(hub, "_reprobe_dead", AsyncMock()),
    ):
        await tick(hass, freezer, 2 * HEARTBEAT_TIMEOUT)
        assert hub._dead_nodes == set()
        assert "marking it unavailable" not in caplog.text

        mock_bluetooth_env["infos"] = infos
        mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
        await wait_for_link(hass, mock_config_entry)
        await settle(hass)
        assert hub.connected
        await tick(hass, freezer, HEARTBEAT_TIMEOUT - 30)
        assert hub._dead_nodes == set()  # a full timeout from the reconnect
        await tick(hass, freezer, 60)
        assert hub._dead_nodes == set(HEARTBEAT_NODES)  # the fake nodes never beat


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
    assert hub._alive_deadline == {}
    with patch(
        "custom_components.junghome_ble.coordinator.LINK_IDLE_TIMEOUT", 10 * 3600.0
    ):
        await tick(hass, freezer, HEARTBEAT_TIMEOUT + 60)
    assert hub.node_alive(LIGHT_SWITCH)  # nothing was promised, nothing is missed

    # unanswered: the Set goes out once per node, the round completes
    fake_link.config_refuse = 0
    fake_link.answer_config = False
    fake_link.config_sent.clear()
    hub._heartbeats_configured_at = None
    caplog.clear()
    task = hass.async_create_background_task(hub._configure_heartbeats(), "hb")
    for _ in range(4):
        freezer.tick(3.1)
        async_fire_time_changed(hass)
        await settle(hass)
    assert task.done()
    assert [n for _s, n, _p in fake_link.config_sent] == HEARTBEAT_NODES
    assert "00DC did not answer the heartbeat configure" in caplog.text
    assert hub._heartbeats_configured_at is not None
    # a node that did not answer the Set is exactly what liveness is for (off, out of range): it gets a deadline
    # like the others, counts as dead when it passes and is asked again; an answer to that brings it back
    assert set(hub._alive_deadline) == set(HEARTBEAT_NODES)
    assert hub.node_alive(LIGHT_SWITCH)
    quiet_mesh(hub)  # the energy poll's answers would keep the socket's node alive
    with (
        patch(
            "custom_components.junghome_ble.coordinator.LINK_IDLE_TIMEOUT", 10 * 3600.0
        ),
        patch.object(hub, "_reprobe_dead", AsyncMock()) as reprobe,
    ):
        await tick(hass, freezer, HEARTBEAT_TIMEOUT + 60)
        assert not hub.node_alive(LIGHT_SWITCH)
        assert hub._dead_nodes == set(HEARTBEAT_NODES)
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
    hub._heartbeats_configured_at = None
    with patch.object(
        ProxyClient, "request_config", side_effect=ConnectionError("gone")
    ):
        await hub._configure_heartbeats()
        await hub.async_disable_heartbeats()
    assert hub._heartbeats_configured_at is None


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
    await hub._after_connect()
    assert init_answered.data[CONF_HEARTBEATS_PUBLISHING] == [f"{stubborn:04X}"]
    fake_link.config_sent.clear()
    fake_link.config_reply = None
    await hub._after_connect()
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
    assert hub.heartbeats_publishing == set(HEARTBEAT_NODES)
    await hub._after_connect()
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
    await hub.async_disable_heartbeats()
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
    assert hub._unsub_heartbeats is not None
    release = asyncio.Event()
    hub._heartbeat_task = hass.async_create_task(release.wait())
    hub._reprobe_task = hass.async_create_task(release.wait())
    renewal, reprobe = hub._heartbeat_task, hub._reprobe_task
    seen: list[tuple[object, bool, bool]] = []
    disable = hub.async_disable_heartbeats

    async def recording() -> None:
        seen.append((hub._unsub_heartbeats, renewal.cancelled(), reprobe.cancelled()))
        await disable()

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    with patch.object(hub, "async_disable_heartbeats", recording):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {OPTION_CLICK_DELAY: False, OPTION_HEARTBEATS: False}
        )
        await hass.async_block_till_done()
        await settle(hass)
    assert seen == [(None, True, True)]
    assert mock_config_entry.runtime_data is not hub


# --------------------------------------------------------------------------- node identity from the Bluetooth address


async def test_nodes_are_known_by_their_mac(init_answered: MockConfigEntry) -> None:
    """JUNG nodes advertise from their public MAC, which the export encodes in the node UUID."""
    hub = hub_of(init_answered)
    assert hub.node_for_address(PROXY_ADDRESS).unicast == 0x0148
    assert hub.node_for_address(PROXY_ADDRESS.lower()).unicast == 0x0148
    assert hub.node_for_address(SECOND_PROXY).unicast == 0x0232
    assert hub.node_for_address("AA:BB:CC:DD:EE:FF") is None
    assert 0x0001 not in {
        n.unicast for n in hub.node_by_mac.values()
    }  # the phone has no MAC
    assert hub.proxy_node == 0x0148  # the connected proxy, named by its address


async def test_unknown_node_of_our_network_raises_a_repair(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A proxy advert with our Network ID from a MAC the export does not know = a node added after the export."""
    hub = hub_of(init_answered)
    callback_ = mock_bluetooth_env["callbacks"][0]
    # other networks, non-MAC addresses (macOS UUIDs) and known nodes raise nothing
    callback_(
        make_service_info(bytes(8), address="AA:BB:CC:DD:EE:01"),
        BluetoothChange.ADVERTISEMENT,
    )
    callback_(
        make_service_info(network_id, address="5509DA5D-8030-253D-A681-1A25C5C09316"),
        BluetoothChange.ADVERTISEMENT,
    )
    callback_(
        make_service_info(network_id, address=SECOND_PROXY),
        BluetoothChange.ADVERTISEMENT,
    )
    assert hub.unknown_nodes == {}
    assert find_issue(hass, ISSUE_UNKNOWN_NODES) is None

    # an unknown MAC with our network id, carrying the JUNG record (a 2-gang push-button)
    record = {
        JUNG_COMPANY_ID: bytes.fromhex("03020000000500 9e000010fb30".replace(" ", ""))
    }
    callback_(
        make_service_info(
            network_id, address="30:fb:10:00:00:9e", manufacturer_data=record
        ),
        BluetoothChange.ADVERTISEMENT,
    )
    assert list(hub.unknown_nodes) == ["30:FB:10:00:00:9E"]
    assert hub.unknown_nodes["30:FB:10:00:00:9E"].product_id == 2
    issue = find_issue(hass, ISSUE_UNKNOWN_NODES)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    # not set up from a gateway: nothing to fetch, the wording without one (review-4 H4-8: no prose in placeholders)
    assert issue.translation_key == ISSUE_UNKNOWN_NODES
    assert issue.translation_placeholders == {
        "title": "JUNG HOME mesh test",
        "count": "1",
        "devices": "Push-button 2-gang 30:FB:10:00:00:9E",
    }
    assert set(issue.translation_placeholders) == issue_text_placeholders(
        ISSUE_UNKNOWN_NODES
    )
    assert (
        "30:FB:10:00:00:9E belongs to this mesh but is not in the export (Push-button 2-gang 30:FB:10:00:00:9E)"
        in caplog.text
    )

    # seen again: nothing changes; a second one without a record is listed by its address
    callback_(
        make_service_info(
            network_id, address="30:FB:10:00:00:9E", manufacturer_data=record
        ),
        BluetoothChange.ADVERTISEMENT,
    )
    callback_(
        make_service_info(network_id, address="30:FB:10:00:00:01"),
        BluetoothChange.ADVERTISEMENT,
    )
    assert len(hub.unknown_nodes) == 2
    issue = find_issue(hass, ISSUE_UNKNOWN_NODES)
    assert issue is not None
    assert issue.translation_placeholders["count"] == "2"
    assert issue.translation_placeholders["devices"] == (
        "30:FB:10:00:00:01, Push-button 2-gang 30:FB:10:00:00:9E"
    )
    diagnostics = await async_get_config_entry_diagnostics(hass, init_answered)
    assert diagnostics["link"]["unknown_nodes"] == [
        {"address": "**REDACTED**", "product_id": None},
        {"address": "**REDACTED**", "product_id": 2},
    ]
    assert "30:FB:10:00:00:9E" not in str(diagnostics)


STRINGS_JSON = (
    Path(__file__).parent.parent / "custom_components" / "junghome_ble" / "strings.json"
)


def issue_text_placeholders(key: str) -> set[str]:
    """The placeholders the `key` repair's title and description use (strings.json)."""
    issue = json.loads(STRINGS_JSON.read_text())["issues"][key]
    return set(re.findall(r"\{(\w+)\}", issue["title"] + issue["description"]))


async def test_unknown_node_issue_is_gone_after_a_reload_with_it_in_the_export(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """The issue is tied to the running hub: a reload (with a new export) starts clean and only re-raises it when the
    node is still missing and still advertising."""
    hub = hub_of(init_answered)
    mock_bluetooth_env["callbacks"][0](
        make_service_info(network_id, address="30:FB:10:00:00:01"),
        BluetoothChange.ADVERTISEMENT,
    )
    assert find_issue(hass, ISSUE_UNKNOWN_NODES) is not None
    assert hub.unknown_nodes
    await hass.config_entries.async_reload(init_answered.entry_id)
    await hass.async_block_till_done()
    assert init_answered.runtime_data is not hub
    assert init_answered.runtime_data.unknown_nodes == {}
    assert find_issue(hass, ISSUE_UNKNOWN_NODES) is None


# --------------------------------------------------------------------------- scene actions


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
        await hub._get_scene_actions()
    assert hub.scene_actions == {}
    assert "did not answer its Scene Action Setup Get" in caplog.text
    with patch.object(hub, "_chunked", side_effect=ConnectionError("gone")):
        await hub._get_scene_actions()  # logged, not raised
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
        await hub._get_scene_actions_of(LIGHT_DIMMER)
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
        await hub._get_scene_actions_of(LIGHT_DIMMER)
    assert LIGHT_DIMMER not in hub.scene_actions.get(9, {})
    assert 7 not in hub.scene_actions  # its only member no longer lists it
    assert (
        LIGHT_SWITCH in hub.scene_actions[9]
    )  # another element's entry is not this list's to drop
    assert hub.scene_actions[8] == {LIGHT_DIMMER: None}

    with patch.object(
        hub.proxy, "request", replies_honouring_match({V.scene_action_get(): []})
    ):
        await hub._get_scene_actions_of(LIGHT_DIMMER)  # no list: nothing is dropped
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
        await hub._get_scene_actions()
    assert hub.scene_actions == {2: {LIGHT_OUT1: on, LIGHT_OUT2: off}}


# --------------------------------------------------------------------------- dynamic devices via the gateway

NEW_MAC = "30:FB:10:00:00:01"
NEW_UUID = "30FB10FF-FE00-0001-0000-000000000000"
GATEWAY_DATA = {
    CONF_GATEWAY_HOST: "junghome.local",
    CONF_GATEWAY_TOKEN: "tok.en",
    CONF_GATEWAY_FINGERPRINT: "ab" * 32,
    CONF_GATEWAY_PIN_SOURCE: PIN_FROM_MESH,  # vouched for: used without asking the mesh first
}


def _read_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())  # type: ignore[no-any-return]


def _read_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def _export_with_new_node(share_export: dict[str, Any]) -> dict[str, Any]:
    """The share export plus a copy of node 0148 provisioned as 0500 from the MAC NEW_MAC."""
    net = json.loads(base64.b64decode(share_export["network"]).decode())
    node = copy.deepcopy(next(n for n in net["nodes"] if n["unicastAddress"] == "0148"))
    node["UUID"], node["unicastAddress"], node["name"] = NEW_UUID, "0500", "Newcomer"
    net["nodes"].append(node)
    return {
        **share_export,
        "network": base64.b64encode(json.dumps(net).encode()).decode(),
    }


@pytest.fixture
async def gateway_entry(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    fast_sleep: list[float],
) -> MockConfigEntry:
    """An entry set up from a gateway: its export lives in a temporary copy the hub may overwrite, in sync with
    the gateway (the digest the config flow records)."""
    path = tmp_path / "JungHome.json"
    shutil.copy(SHARE_EXPORT_PATH, path)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={
            CONF_CDB_PATH: str(path),
            CONF_UNICAST: "0D00",
            CONF_SOURCE: "gateway",
            CONF_GATEWAY_SYNCED: export_digest(_read_json(SHARE_EXPORT_PATH)),
            **GATEWAY_DATA,
        },
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    return entry


@pytest.mark.parametrize(
    "source", ["path", "upload", None], ids=["path", "upload", "unrecorded"]
)
async def test_unknown_node_leaves_a_users_export_alone(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    fast_sleep: list[float],
    network_id: bytes,
    source: str | None,
) -> None:
    """An entry that keeps the gateway's host and token but was (re)configured to a file of the user's own is not
    refreshed from the gateway: the fetch would overwrite that file in place. The repair issue does not promise
    the gateway's help either."""
    path = tmp_path / "MeshNetwork.json"
    shutil.copy(CDB_PATH, path)
    data = {CONF_CDB_PATH: str(path), CONF_UNICAST: "0D00", **GATEWAY_DATA}
    if source is not None:
        data[CONF_SOURCE] = source
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data=data,
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    original = _read_bytes(str(path))
    with patch.object(JungHomeGatewayApi, "fetch_project", AsyncMock()) as fetch:
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        await hass.async_block_till_done()
        await settle(hass)
    assert fetch.await_count == 0
    assert _read_bytes(str(path)) == original
    issue = find_issue(hass, ISSUE_UNKNOWN_NODES)
    assert issue is not None
    assert (
        issue.translation_key == ISSUE_UNKNOWN_NODES
    )  # the gateway is not where its export comes from
    assert "host" not in issue.translation_placeholders
    assert hub_of(entry)._export_refresh is None


async def test_unknown_node_fetches_the_gateways_export_and_reloads(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An entry from a gateway asks the gateway for its export when a node of our mesh is not in ours; when that
    export lists the node it replaces the file and the entry reloads with it — the app uploads its project right
    after provisioning, so this is how a new device shows up without the user.

    It is the configurator's gateway write: the old file is kept as `.bak` and the adopted export's digest is
    recorded as synced, so the next change does not take the gateway for ahead of Home Assistant."""
    hub = hub_of(gateway_entry)
    share = _read_json(SHARE_EXPORT_PATH)
    original = _read_bytes(gateway_entry.data[CONF_CDB_PATH])
    fetched: list[str] = []

    async def fetch_project(self: JungHomeGatewayApi) -> dict[str, Any]:
        fetched.append(self.host)
        return _export_with_new_node(share)

    with patch.object(JungHomeGatewayApi, "fetch_project", fetch_project):
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        issue = find_issue(hass, ISSUE_UNKNOWN_NODES)
        assert issue is not None  # raised at once; the fetch runs in the background
        # the wording that names the gateway, whose host is its one extra placeholder (review-4 H4-8)
        assert issue.translation_key == ISSUE_UNKNOWN_NODES_GATEWAY
        assert issue.translation_placeholders == {
            "title": gateway_entry.title,
            "count": "1",
            "devices": NEW_MAC,
            "host": "junghome.local",
        }
        assert set(issue.translation_placeholders) == issue_text_placeholders(
            ISSUE_UNKNOWN_NODES_GATEWAY
        )
        await hass.async_block_till_done()
        await wait_until(
            hass,
            lambda: hub._export_refresh is not None and hub._export_refresh.done(),
            what="the gateway export fetch and write",
        )  # a background task `block_till_done` does not wait for; its write is real (fsynced) I/O
        await wait_for_link(hass, gateway_entry)
        await settle(hass)
    assert fetched == ["junghome.local"]
    assert "reloading with it" in caplog.text
    new_hub = hub_of(gateway_entry)
    assert new_hub is not hub
    assert new_hub.node_for_address(NEW_MAC) is not None
    assert new_hub.node_for_address(NEW_MAC).unicast == 0x0500
    assert find_issue(hass, ISSUE_UNKNOWN_NODES) is None
    saved = _read_json(gateway_entry.data[CONF_CDB_PATH])
    assert saved == _export_with_new_node(share)
    path = Path(gateway_entry.data[CONF_CDB_PATH])
    assert path.with_name(path.name + ".bak").read_bytes() == original
    assert gateway_sync(hass, gateway_entry.entry_id).synced == export_digest(
        _export_with_new_node(share)
    )


class GatewayAnswers:
    """The gateway's answers to `fetch_project`, one per unknown node advertising (each triggers one refresh)."""

    def __init__(
        self, hass: HomeAssistant, entry: MockConfigEntry, env: dict[str, Any]
    ) -> None:
        self.hass, self.entry, self.env = hass, entry, env
        self.answers: list[Any] = []
        self.macs = iter(f"30:FB:10:00:00:{n:02X}" for n in range(2, 20))

    async def fetch_project(self) -> dict[str, Any]:
        """Patched onto the class as a bound method: called without the API instance."""
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[no-any-return]

    async def advert(self, network_id: bytes, mac: str | None = None) -> None:
        """A node advertises; wait for the refresh it started (a background task with real executor I/O)."""
        self.env["callbacks"][0](
            make_service_info(network_id, address=mac or next(self.macs)),
            BluetoothChange.ADVERTISEMENT,
        )
        await self.hass.async_block_till_done()
        hub = hub_of(self.entry)
        await wait_until(
            self.hass,
            lambda: hub._export_refresh is None or hub._export_refresh.done(),
            what="the gateway export refresh",
        )


async def test_gateway_export_refresh_leaves_the_issue_when_it_cannot_help(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = hub_of(gateway_entry)
    share = _read_json(SHARE_EXPORT_PATH)
    original = _read_bytes(gateway_entry.data[CONF_CDB_PATH])
    gw = GatewayAnswers(hass, gateway_entry, mock_bluetooth_env)
    other_mesh = _export_with_new_node(share)
    net = json.loads(base64.b64decode(other_mesh["network"]).decode())
    net["meshUUID"] = "00000000-0000-4000-8000-000000000099"
    other_mesh["network"] = base64.b64encode(json.dumps(net).encode()).decode()
    with patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project):
        gw.answers.append(GatewayError("GET project/junghome: HTTP 500"))
        await gw.advert(network_id, NEW_MAC)
        assert "Could not ask the gateway junghome.local for its export" in caplog.text
        assert hub_of(gateway_entry) is hub  # no reload
        # the same MAC again: no second fetch (one per node)
        await gw.advert(network_id, NEW_MAC)
        assert not gw.answers
        assert hub_of(gateway_entry) is hub
        # a second unknown node: the gateway's export does not list either of them
        gw.answers.append(share)
        await gw.advert(network_id)
        assert (
            "does not list the unknown node(s) 30:FB:10:00:00:01, 30:FB:10:00:00:02"
            in caplog.text
        )
        # another mesh's export, an export that does not parse
        gw.answers.append(other_mesh)
        await gw.advert(network_id)
        assert "holds the export of another mesh" in caplog.text
        gw.answers.append({"network": "not base64!", "meta": {}})
        await gw.advert(network_id)
        assert "does not parse" in caplog.text
    assert hub_of(gateway_entry) is hub
    assert _read_bytes(gateway_entry.data[CONF_CDB_PATH]) == original
    assert find_issue(hass, ISSUE_UNKNOWN_NODES) is not None
    assert len(hub.unknown_nodes) == 4


async def test_gateway_export_refresh_overwrites_nothing_it_must_not(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The guards of every gateway write: a file that cannot be written, the bare `/project/cdb` database over the
    share export (it has every name and room link), a file changed since the last sync (a change never handed
    over) while the gateway's changed too, and a gateway holding just what was synced last."""
    hub = hub_of(gateway_entry)
    share = _read_json(SHARE_EXPORT_PATH)
    newer = _export_with_new_node(share)
    original = _read_bytes(gateway_entry.data[CONF_CDB_PATH])
    gw = GatewayAnswers(hass, gateway_entry, mock_bluetooth_env)
    with patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project):
        gw.answers.append(newer)
        with patch(
            "custom_components.junghome_ble.mesh_config.write_private_with_backup",
            side_effect=OSError("read-only"),
        ):
            await gw.advert(network_id, NEW_MAC)
        assert "was not adopted: service_export_write_failed" in caplog.text
        bare = json.loads(base64.b64decode(newer["network"]).decode())
        gw.answers.append({"meshNetwork": bare})
        await gw.advert(network_id)
        assert "was not adopted: service_gateway_export_incomplete" in caplog.text
        # the file changed since the last sync too, and no copy of the app's last upload tells what HA changed
        gateway_sync(hass, gateway_entry.entry_id).synced = "0" * 64
        gw.answers.append(newer)
        await gw.advert(network_id)
        assert "was not adopted: service_gateway_export_newer" in caplog.text
        gateway_sync(hass, gateway_entry.entry_id).synced = export_digest(newer)
        gw.answers.append(newer)
        await gw.advert(network_id)
        assert "but nothing Home Assistant has not synced already" in caplog.text
    assert hub_of(gateway_entry) is hub
    assert _read_bytes(gateway_entry.data[CONF_CDB_PATH]) == original


async def test_gateway_export_refresh_takes_a_bare_database_over_a_bare_file(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    fast_sleep: list[float],
    network_id: bytes,
) -> None:
    """An entry set up from a gateway that only had `/project/cdb`: neither side has names to protect, and the
    refresh takes the gateway's database when it lists the new node (a change would plan on the file as it is)."""
    share = _read_json(SHARE_EXPORT_PATH)
    bare = json.loads(base64.b64decode(share["network"]).decode())
    path = tmp_path / "JungHome.json"
    path.write_text(json.dumps({"meshNetwork": bare}))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={
            CONF_CDB_PATH: str(path),
            CONF_UNICAST: "0D00",
            CONF_SOURCE: "gateway",
            **GATEWAY_DATA,
        },
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    newer = json.loads(
        base64.b64decode(_export_with_new_node(share)["network"]).decode()
    )
    with patch.object(
        JungHomeGatewayApi,
        "fetch_project",
        AsyncMock(return_value={"meshNetwork": newer}),
    ):
        hub = hub_of(entry)
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        await hass.async_block_till_done()
        await wait_until(
            hass,
            lambda: hub._export_refresh is not None and hub._export_refresh.done(),
            what="the gateway export fetch and write",
        )
        await hass.async_block_till_done()
        await wait_for_link(hass, entry)
        await settle(hass)
    assert _read_json(str(path)) == {"meshNetwork": newer}
    assert hub_of(entry).node_for_address(NEW_MAC) is not None


async def test_gateway_export_refresh_waits_for_the_configurator_and_a_link(
    hass: HomeAssistant, gateway_entry: MockConfigEntry
) -> None:
    """Without a configurator or a link nothing is fetched (the next link asks); the issue stands meanwhile."""
    hub = hub_of(gateway_entry)
    hub.unknown_nodes[NEW_MAC] = None
    configurator, hub.configurator = hub.configurator, None
    hub._request_export_refresh()
    assert hub._export_refresh is None
    hub.configurator = configurator
    with patch.object(
        JungHomeHub, "connected", new_callable=PropertyMock, return_value=False
    ):
        hub._request_export_refresh()
    assert hub._export_refresh is None


async def test_gateway_export_refresh_is_retried_until_the_app_uploaded(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    gateway_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """Review-3 C1: the fetch was tried once per MAC, ever — and that one attempt usually came before the app had
    uploaded its project, so a node added in the app never appeared until a reload. Unanswered, it is repeated
    after each EXPORT_REFRESH_BACKOFF delay."""
    hub = hub_of(gateway_entry)
    share = _read_json(SHARE_EXPORT_PATH)
    fetch = AsyncMock(side_effect=[share, share, _export_with_new_node(share)])

    async def fetched(count: int) -> None:
        await wait_until(
            hass,
            lambda: (
                fetch.await_count == count
                and hub._export_refresh is not None
                and hub._export_refresh.done()
            ),
            what=f"export fetch {count}",
        )

    with (
        patch.object(JungHomeGatewayApi, "fetch_project", fetch),
        patch.object(hass.config_entries, "async_reload") as reload,
    ):
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        await fetched(1)
        assert hub._unsub_export_refresh is not None
        await tick(hass, freezer, coordinator.EXPORT_REFRESH_BACKOFF[0] + 1)
        await fetched(2)
        reload.assert_not_called()
        await tick(hass, freezer, coordinator.EXPORT_REFRESH_BACKOFF[0] + 1)
        assert fetch.await_count == 2  # the second delay is longer
        await tick(hass, freezer, coordinator.EXPORT_REFRESH_BACKOFF[1])
        await fetched(3)
        await hass.async_block_till_done()
        reload.assert_called_once_with(gateway_entry.entry_id)
    assert hub._unsub_export_refresh is None


async def test_the_reload_with_the_adopted_export_waits_for_a_running_service_call(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """Review-3 W11: the unknown-node refresh reloaded the entry without the entry's lock, tearing the hub down
    under a service call working on it. The reload now waits for the lock; once it has it, a hub the call's own
    reload replaced meanwhile (it read the adopted export already) is not reloaded a second time."""
    hub = hub_of(gateway_entry)
    fetch = AsyncMock(return_value=_export_with_new_node(_read_json(SHARE_EXPORT_PATH)))
    lock = entry_lock(hass, gateway_entry.entry_id)
    with (
        patch.object(JungHomeGatewayApi, "fetch_project", fetch),
        patch.object(hass.config_entries, "async_reload") as reload,
    ):
        await lock.acquire()  # a service call is running
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        # not `wait_until` / `settle`: they flush HA's tasks, and the reload waiting for the lock is one of them
        deadline = time.monotonic() + 10
        while not (hub._export_refresh is not None and hub._export_refresh.done()):
            assert time.monotonic() < deadline, "the gateway export fetch and write"
            await asyncio.sleep(0)
        for _ in range(10):
            await asyncio.sleep(0)
        reload.assert_not_called()
        lock.release()
        await hass.async_block_till_done()
        reload.assert_called_once_with(gateway_entry.entry_id)
        # a hub that is no longer the entry's, or an entry no longer loaded: nothing to reload
        reload.reset_mock()
        with patch.object(gateway_entry, "runtime_data", object()):
            await hub._reload_for_export()
        gateway_entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
        await hub._reload_for_export()
        gateway_entry.mock_state(hass, ConfigEntryState.LOADED)
        reload.assert_not_called()


async def test_a_new_link_asks_the_gateway_again_and_stop_cancels_the_retry(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    answering_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """An unknown node heard while the link was down is asked about when the next link comes up."""
    hub = hub_of(gateway_entry)
    share = _read_json(SHARE_EXPORT_PATH)
    fetch = AsyncMock(return_value=share)
    with patch.object(JungHomeGatewayApi, "fetch_project", fetch):
        hub.unknown_nodes[NEW_MAC] = None
        with patch.object(
            JungHomeHub, "connected", new_callable=PropertyMock, return_value=False
        ):
            hub._request_export_refresh()  # heard while the link is down
        await hass.async_block_till_done()
        assert fetch.await_count == 0
        answering_link.drop_link()
        await wait_until(hass, lambda: answering_link.connect_count == 2)
        await wait_for_link(hass, gateway_entry)
        await wait_until(
            hass,
            lambda: hub._export_refresh is not None and hub._export_refresh.done(),
            what="the export fetch of the new link",
        )
        assert fetch.await_count == 1
    assert hub._unsub_export_refresh is not None
    await hub.async_stop()
    assert hub._unsub_export_refresh is None


async def test_gateway_export_refresh_runs_one_fetch_at_a_time(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """Two nodes appearing while one fetch is in flight share that fetch."""
    hub = hub_of(gateway_entry)
    share = _read_json(SHARE_EXPORT_PATH)
    gate = asyncio.Event()
    calls = 0

    async def slow_fetch(self: JungHomeGatewayApi) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        await gate.wait()
        return share

    with patch.object(JungHomeGatewayApi, "fetch_project", slow_fetch):
        for mac in ("30:FB:10:00:00:06", "30:FB:10:00:00:07"):
            mock_bluetooth_env["callbacks"][0](
                make_service_info(network_id, address=mac),
                BluetoothChange.ADVERTISEMENT,
            )
        await asyncio.sleep(0)
        gate.set()
        await hass.async_block_till_done()
    assert calls == 1
    assert len(hub.unknown_nodes) == 2


# ----------------------------------------------------------------------------- following the gateway over the mesh

NEW_GATEWAY_HOST = "192.0.2.77"
NEW_FINGERPRINT = "cd" * 32


def gateway_node_says(
    link: FakeProxyLink, host: bytes, fingerprint: bytes
) -> PropertyMesh:
    """The gateway node's LBC Manufacturer server answers `0xC002` / `0xC003` (NUL-padded text, as on air)."""
    return PropertyMesh(link, {(GATEWAY, 0xC002): host, (GATEWAY, 0xC003): fingerprint})


async def test_gateway_is_followed_to_its_new_address(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    answering_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """Unreachable at the stored host: the gateway node's own properties say where it is now; fetched from there.

    The address follows; the node reports the pinned certificate (with colons: normalised), which stays the pin,
    and the token stays the one Home Assistant registered.
    """
    gateway_node_says(
        answering_link,
        NEW_GATEWAY_HOST.encode() + b"\0\0",
        ":".join(["AB"] * 32).encode(),
    )
    hosts: list[str] = []

    async def fetch_project(self: JungHomeGatewayApi) -> dict[str, Any]:
        hosts.append(self.host)
        if self.host == GATEWAY_DATA[CONF_GATEWAY_HOST]:
            raise GatewayUnreachable("GET project/junghome: ClientConnectorError")
        raise GatewayError(
            "GET project/junghome: HTTP 500"
        )  # reached; what it says is another test

    with patch.object(JungHomeGatewayApi, "fetch_project", fetch_project):
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        await hass.async_block_till_done()
    assert hosts == [GATEWAY_DATA[CONF_GATEWAY_HOST], NEW_GATEWAY_HOST]
    data = gateway_entry.data
    assert data[CONF_GATEWAY_HOST] == NEW_GATEWAY_HOST
    assert data[CONF_GATEWAY_FINGERPRINT] == GATEWAY_DATA[CONF_GATEWAY_FINGERPRINT]
    assert data[CONF_GATEWAY_TOKEN] == GATEWAY_DATA[CONF_GATEWAY_TOKEN]
    assert hub_of(gateway_entry).entry is gateway_entry  # no reload for it
    assert find_issue(hass, ISSUE_GATEWAY_CERTIFICATE) is None


async def test_a_certificate_reported_over_the_mesh_is_never_adopted(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    answering_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Anyone with a node's keys can answer on the mesh: a certificate other than the pin stops the gateway's use
    and raises the repair pointing to Reconfigure — the pin stays, nothing is fetched from the new address, and
    the next unknown node does not ask the gateway either. The address itself is still followed."""
    gateway_node_says(
        answering_link, NEW_GATEWAY_HOST.encode(), NEW_FINGERPRINT.encode()
    )
    hosts: list[str] = []

    async def fetch_project(self: JungHomeGatewayApi) -> dict[str, Any]:
        hosts.append(self.host)
        raise GatewayCertificateMismatch(self.host, self.fingerprint, NEW_FINGERPRINT)

    with patch.object(JungHomeGatewayApi, "fetch_project", fetch_project):
        for mac in (NEW_MAC, "30:FB:10:00:00:02"):
            mock_bluetooth_env["callbacks"][0](
                make_service_info(network_id, address=mac),
                BluetoothChange.ADVERTISEMENT,
            )
            await hass.async_block_till_done()
    assert hosts == [GATEWAY_DATA[CONF_GATEWAY_HOST]]  # once: then no more
    data = gateway_entry.data
    assert data[CONF_GATEWAY_FINGERPRINT] == GATEWAY_DATA[CONF_GATEWAY_FINGERPRINT]
    assert data[CONF_GATEWAY_HOST] == NEW_GATEWAY_HOST
    issue = find_issue(hass, ISSUE_GATEWAY_CERTIFICATE)
    assert issue is not None
    assert issue.translation_placeholders == {"host": NEW_GATEWAY_HOST}
    assert "the gateway is not used until the entry is reconfigured" in caplog.text
    assert "export was not fetched for the unknown node(s)" in caplog.text


async def test_a_pinned_certificate_the_host_no_longer_presents_raises_the_repair(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    answering_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """The node vouches for the pin, the host presents another certificate: nothing was sent (TLS), the repair is
    raised; the next answer of the pinned gateway clears it."""
    gateway_node_says(
        answering_link,
        GATEWAY_DATA[CONF_GATEWAY_HOST].encode(),
        GATEWAY_DATA[CONF_GATEWAY_FINGERPRINT].encode(),
    )
    answers: list[Any] = [
        GatewayCertificateMismatch("junghome.local", "ab" * 32, NEW_FINGERPRINT),
        _read_json(SHARE_EXPORT_PATH),
    ]

    async def fetch_project(self: JungHomeGatewayApi) -> dict[str, Any]:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    with patch.object(JungHomeGatewayApi, "fetch_project", fetch_project):
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        await hass.async_block_till_done()
        assert find_issue(hass, ISSUE_GATEWAY_CERTIFICATE) is not None
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address="30:FB:10:00:00:02"),
            BluetoothChange.ADVERTISEMENT,
        )
        await hass.async_block_till_done()
    assert not answers
    assert find_issue(hass, ISSUE_GATEWAY_CERTIFICATE) is None


@pytest.mark.parametrize(
    "host",
    [b"192.0.2.300", b"gw.example:8443", b"-bad-.lan", b"fe80::1", b"x" * 64],
    ids=["octet", "port", "label", "ipv6", "long_label"],
)
async def test_an_address_that_is_no_host_is_not_followed(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    answering_link: FakeProxyLink,
    host: bytes,
) -> None:
    gateway_node_says(
        answering_link, host, GATEWAY_DATA[CONF_GATEWAY_FINGERPRINT].encode()
    )
    assert await hub_of(gateway_entry).async_follow_gateway() is False
    assert gateway_entry.data[CONF_GATEWAY_HOST] == GATEWAY_DATA[CONF_GATEWAY_HOST]


def test_gateway_hosts() -> None:
    assert coordinator.is_gateway_host("192.168.1.20")
    assert coordinator.is_gateway_host("junghome.local")
    assert coordinator.is_gateway_host("JungHome-2")
    assert not coordinator.is_gateway_host("192.168.1")
    assert not coordinator.is_gateway_host("a..b")


async def test_gateway_follow_without_news(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    answering_link: FakeProxyLink,
) -> None:
    """Nothing changes when the node repeats what the entry holds, says nothing, or there is no gateway node."""
    hub = hub_of(gateway_entry)
    gateway_node_says(
        answering_link,
        GATEWAY_DATA[CONF_GATEWAY_HOST].encode(),
        GATEWAY_DATA[CONF_GATEWAY_FINGERPRINT].encode(),
    )
    assert await hub.async_follow_gateway() is False
    with patch.object(hub.proxy, "request", AsyncMock(side_effect=TimeoutError)):
        assert await hub.async_follow_gateway() is False
    gateway = next(n for n in hub.cdb.nodes if n.unicast == GATEWAY)
    with patch.object(gateway, "pid", 0x01):
        assert await hub.async_follow_gateway() is False
    with patch.object(
        JungHomeHub, "connected", new_callable=PropertyMock, return_value=False
    ):
        assert await hub.async_follow_gateway() is False
    assert gateway_entry.data[CONF_GATEWAY_HOST] == GATEWAY_DATA[CONF_GATEWAY_HOST]
    assert find_issue(hass, ISSUE_GATEWAY_CERTIFICATE) is None


async def test_gateway_follow_needs_a_gateway_entry(
    hass: HomeAssistant, init_answered: MockConfigEntry
) -> None:
    assert await hub_of(init_answered).async_follow_gateway() is False


async def test_gateway_that_cannot_be_followed_stays_unreachable(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def unreachable(self: JungHomeGatewayApi) -> dict[str, Any]:
        raise GatewayUnreachable("GET project/junghome: ClientConnectorError")

    with (
        patch.object(JungHomeGatewayApi, "fetch_project", unreachable),
        patch.object(
            JungHomeHub, "async_follow_gateway", AsyncMock(return_value=False)
        ) as follow,
    ):
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        await hass.async_block_till_done()
    follow.assert_awaited_once()
    assert "Could not ask the gateway junghome.local for its export" in caplog.text


# ----------------------------------------------------------------------------- the pin checked over the mesh (S1)


async def unverified_gateway_entry(
    hass: HomeAssistant, tmp_path: Path, **data: Any
) -> MockConfigEntry:
    """A gateway entry whose pin was learned at first contact (or predates the record): not vouched for yet."""
    path = tmp_path / "JungHome.json"
    shutil.copy(SHARE_EXPORT_PATH, path)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={
            CONF_CDB_PATH: str(path),
            CONF_UNICAST: "0D00",
            CONF_SOURCE: "gateway",
            CONF_GATEWAY_SYNCED: export_digest(_read_json(SHARE_EXPORT_PATH)),
            **GATEWAY_DATA,
            CONF_GATEWAY_PIN_SOURCE: PIN_FROM_USER,
            **data,
        },
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    await wait_until(
        hass,
        lambda: (
            not any(
                t.get_name().endswith("gateway certificate check")
                for t in entry._background_tasks
            )
        ),
        what="the link-time certificate check",
    )
    return entry


async def test_the_first_link_vouches_for_a_pin_the_gateway_node_confirms(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The pin a user trusted at setup is compared with the node's report on the first link: equal, it is vouched
    for from then on (recorded, never asked again)."""
    mesh = gateway_node_says(
        answering_link,
        GATEWAY_DATA[CONF_GATEWAY_HOST].encode(),
        GATEWAY_DATA[CONF_GATEWAY_FINGERPRINT].upper().encode(),
    )
    entry = await unverified_gateway_entry(hass, tmp_path)
    assert (GATEWAY, 0xC003) in mesh.gets
    assert entry.data[CONF_GATEWAY_PIN_SOURCE] == PIN_FROM_MESH
    assert "confirmed the certificate pinned for the gateway" in caplog.text
    mesh.gets.clear()
    assert await hub_of(entry).async_gateway_distrust() is None
    assert mesh.gets == []


async def test_a_pin_the_gateway_node_contradicts_is_not_used(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    fast_sleep: list[float],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A LAN impostor pinned at setup: the node reports another certificate, so the gateway is never asked (no
    token, no export sent there) and the repair points to Reconfigure."""
    gateway_node_says(
        answering_link,
        GATEWAY_DATA[CONF_GATEWAY_HOST].encode(),
        NEW_FINGERPRINT.encode(),
    )
    entry = await unverified_gateway_entry(hass, tmp_path)
    assert entry.data[CONF_GATEWAY_PIN_SOURCE] == PIN_FROM_USER
    issue = find_issue(hass, ISSUE_GATEWAY_CERTIFICATE)
    assert issue is not None
    assert issue.translation_placeholders == {"host": GATEWAY_DATA[CONF_GATEWAY_HOST]}
    gw = GatewayAnswers(hass, entry, mock_bluetooth_env)
    with patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project):
        await gw.advert(
            network_id, NEW_MAC
        )  # no answer queued: asking would fail the refresh
    assert "export was not fetched for the unknown node(s)" in caplog.text
    assert (
        await hub_of(entry).async_gateway_distrust()
        == coordinator.GATEWAY_CERTIFICATE_CHANGED
    )


async def test_an_unconfirmed_pin_is_not_used_and_asked_again(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    fast_sleep: list[float],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The node does not answer: nothing is exchanged with the gateway (said so), the mesh works as ever, and the
    next use of the gateway asks again — once the node answers, the gateway is used."""
    gateway_node_says(
        answering_link,
        GATEWAY_DATA[CONF_GATEWAY_HOST].encode(),
        GATEWAY_DATA[CONF_GATEWAY_FINGERPRINT].encode(),
    )
    silent = patch.object(JungHomeHub, "_gateway_text", AsyncMock(return_value=None))
    with silent:
        entry = await unverified_gateway_entry(hass, tmp_path)
    assert "has not confirmed the certificate pinned" in caplog.text
    assert entry.data[CONF_GATEWAY_PIN_SOURCE] == PIN_FROM_USER
    hub = hub_of(entry)
    gw = GatewayAnswers(hass, entry, mock_bluetooth_env)
    gw.answers.append(_read_json(SHARE_EXPORT_PATH))
    with patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project):
        with silent:
            await gw.advert(network_id, NEW_MAC)
        assert gw.answers  # not asked
        assert "export was not fetched for the unknown node(s)" in caplog.text
        await gw.advert(network_id)
    assert not gw.answers
    assert entry.data[CONF_GATEWAY_PIN_SOURCE] == PIN_FROM_MESH
    # without a link, or without a gateway node, the node cannot be asked
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_GATEWAY_PIN_SOURCE: PIN_FROM_USER}
    )
    with patch.object(
        JungHomeHub, "connected", new_callable=PropertyMock, return_value=False
    ):
        assert await hub.async_gateway_distrust() == coordinator.GATEWAY_UNVERIFIED
