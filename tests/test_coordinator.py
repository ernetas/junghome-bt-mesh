"""The hub: connection loop, state refresh, decoding of mesh messages, button gestures, beacons."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import shutil
import time
from collections.abc import Generator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    EVENT_STATE_REPORTED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Event, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_fire_time_changed_exact,
)

from custom_components.junghome_ble import app_follow, coordinator
from custom_components.junghome_ble.const import (
    APP_QUIET_AFTER,
    APP_SYNC_MIN_INTERVAL,
    BUTTON_REPEAT_WINDOW,
    CONF_CDB_PATH,
    CONF_GATEWAY_FINGERPRINT,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_PIN_SOURCE,
    CONF_GATEWAY_SYNCED,
    CONF_GATEWAY_TOKEN,
    CONF_METADATA_DIR,
    CONF_SOURCE,
    CONF_UNICAST,
    DOMAIN,
    DOUBLE_CLICK_WINDOW,
    GATEWAY_SYNC_PERIOD,
    ISSUE_APP_CHANGED,
    ISSUE_GATEWAY_TOKEN,
    ISSUE_KEY_REFRESH,
    ISSUE_PDUS_DROPPED,
    OPTION_CLICK_DELAY,
    OPTION_FOLLOW_APP,
    OPTION_GATEWAY_CHECK,
    PIN_FROM_MESH,
    PIN_FROM_USER,
    SIGNAL_UPDATE,
    TID_REPEAT_WINDOW,
    learn_more_url,
)
from custom_components.junghome_ble.coordinator import (
    GENERIC_LEVEL_OPCODES,
    SIG_PROPERTY_STATUS_OPCODES,
    STATUS_HANDLERS,
    JungHomeHub,
    register_status_handler,
)
from custom_components.junghome_ble.gateway_api import (
    GatewayError,
    JungHomeGatewayApi,
)
from custom_components.junghome_ble.jhmesh import client as client_mod
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import state as state_mod
from custom_components.junghome_ble.jhmesh import vendor_models as V
from custom_components.junghome_ble.jhmesh.client import AccessMessage
from custom_components.junghome_ble.jhmesh.pdu import (
    ALL_NODES,
    encode_opcode,
)
from custom_components.junghome_ble.mesh_config import export_digest, gateway_sync

from .conftest import (
    CDB_PATH,
    META_DIR,
    PROXY_ADDRESS,
    SHARE_EXPORT_PATH,
    FakeProxyLink,
    StateTransitions,
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
    """Keep the config entities' initial property reads and the loads' lock reads out of the hub's traffic.

    Every config entity (`config_entities.ConfigEntity`) queues a property or setup-state Get after each connection,
    every light and socket a Get of its lock (`config_entities.LoadLock`).
    The tests here assert on exactly what the hub itself sends and receives (`fake_link.sent`, REFRESH_GETS /
    ENERGY_GETS, the drop-detection counters, the link watchdog's silence), and the fake mesh does not answer
    those Gets, so without this patch every test would see dozens of extra PDUs and stray 3 s timeouts. The
    reads have their own tests (test_config_entities.py and the platform modules); this is the one place they
    are switched off, so a hub test that needs them must opt out explicitly.
    """
    with (
        patch(
            "custom_components.junghome_ble.config_entities.ConfigEntity._maybe_read"
        ),
        patch(
            "custom_components.junghome_ble.config_entities.LoadLock._maybe_read_lock"
        ),
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
    task = hub.refresh.task
    assert task is not None
    assert not task.done()
    await hub.async_stop()
    assert task.cancelled()
    assert hub.refresh.task is None
    assert hub.link.task is None
    await hub.link._watch_link()  # a link that is already gone: returns at once


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

    hub.liveness.heartbeat_task = hass.async_create_task(boom())
    await (
        hass.async_block_till_done()
    )  # the task is done with the exception before async_stop cancels it

    await hub.async_stop()  # must not raise
    assert not fake_link.is_connected
    assert hub.liveness.heartbeat_task is None


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
    deadlines = dict(hub.liveness._alive_deadline)
    hub.proxy.request_config = AsyncMock(
        return_value=SimpleNamespace(params=b"\x00\x00\x00")
    )
    assert (
        await hub.liveness._set_heartbeat(node, b"", "configure") is False
    )  # no raise
    hub.proxy.request_config.assert_awaited_once()
    assert hub.liveness._alive_deadline == deadlines
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


def quiet_mesh(hub: JungHomeHub) -> None:
    """Stop the periodic energy poll: on a mesh without a gateway nothing else is on air at night."""
    assert hub.energy.unsub_energy is not None
    hub.energy.unsub_energy()
    hub.energy.unsub_energy = None


def stop_answering(link: FakeProxyLink) -> None:
    """Undo `answer_gets`: the proxy still takes our writes but nothing comes back through it."""
    link.write_gatt_char = FakeProxyLink.write_gatt_char.__get__(link)  # type: ignore[method-assign]


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    freezer.tick(seconds)
    async_fire_time_changed(hass)
    await settle(hass)


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
        await hub.while_seq_stalls(held_back) == retries + 1
    )  # through on the last retry
    calls.clear()
    retries += 1  # one refusal more than the deadline allows
    with pytest.raises(client_mod.SequenceStalled):
        await hub.while_seq_stalls(held_back)
    assert len(calls) == retries
    assert fast_sleep == [coordinator.SEQ_STALL_RETRY] * (2 * retries - 2)


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
    )  # vendor property status without the access byte and value (properties/reader.py handles complete ones)
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
    )  # truncated setup status (properties/reader.py handles complete ones)
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
        # the config entities' handler for the three vendor property Status opcodes (properties/reader.py)
        *((M.JUNG_CID, op) for op in M.VENDOR_PROPERTY_STATUS_OPCODES.values()),
        # the battery sensors' handler (sensor.py); the detector handlers of binary_sensor.py chain behind the
        # Sensor Status and OnOff Set rows above instead of adding rows
        (None, M.GEN_BATTERY_STATUS),
        # the fault register (binary_sensor.py's fault entities)
        (None, M.HEALTH_FAULT_STATUS),
        # the config entities' SIG setup states (properties.targets.SETUP_STATES)
        (None, M.GEN_ONPOWERUP_STATUS),
        (None, M.LIGHT_LIGHTNESS_RANGE_STATUS),
        (None, M.LIGHT_LIGHTNESS_DEFAULT_STATUS),
        (None, M.LIGHT_CTL_DEFAULT_STATUS),
        # the nodes' clocks, zones and stored locations (node_clocks.py)
        (None, M.TIME_STATUS),
        (None, M.TIME_ZONE_STATUS),
        (None, M.GEN_LOCATION_GLOBAL_STATUS),
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


def test_onoff_set_reaches_the_key_gestures_before_the_detectors() -> None:
    """The coordinator's OnOff Set handler (the rocker's `press_on` / `press_off`) runs before the detector's.

    binary_sensor.py chains its handler behind the one the table holds when it is imported, so the coordinator's
    (which hands the message to `ButtonGestures`) must be registered by then (review-4 A4-3).
    """
    from custom_components.junghome_ble import binary_sensor  # noqa: PLC0415

    order: list[str] = []
    hub = MagicMock()
    hub.gestures.from_button.side_effect = lambda m, p: order.append("keys")
    m = MagicMock(src=0x0100, dst=0xC000)
    with patch.object(
        binary_sensor,
        "detector_at",
        side_effect=lambda hub, src: order.append("detectors"),
    ):
        for opcode in (M.GEN_ONOFF_SET, M.GEN_ONOFF_SET_UNACK):
            STATUS_HANDLERS[None, opcode](hub, m, b"\x01\x07")
    assert order == ["keys", "detectors"] * 2


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
    assert hub.rx_to_us == 2  # counted for drop detection all the same


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


def wall_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The wall clock the IV Update timing reads (`LocalState.apply_beacon`), moved by the test: `clock[0] += …`."""
    clock = [1_000_000.0]
    monkeypatch.setattr(state_mod, "_wall_now", lambda: clock[0])
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
    sequence restart reaches the mesh's store (`seq_store.seq_store`), not just the in-memory state — an IV
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
        await hub.clock.send_time()
    await hub.clock.send_location()  # the one refusal is used up: this goes out
    assert [dst for _src, dst, _pdu in fake_link.sent] == [ALL_NODES]


# --------------------------------------------------------------------------- socket counters (SIG Generic Property Status)


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


# --------------------------------------------------------------------------- an entry set up from the gateway

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


# ----------------------------------------------------------------------------- following the app (review-4 U4-6)

PHONE = (
    0x0001  # the export's provisioner node: no product id, the phone running the app
)
PHONE_GET = encode_opcode(M.GEN_ONOFF_GET)  # plain traffic of the open app
SUBSCRIPTION_ADD = encode_opcode(C.CONFIG_MODEL_SUBSCRIPTION_ADD) + bytes.fromhex(
    "480101c00010"
)  # 0148 joins C001 on its OnOff server: a room edit in the app


def _renamed(share: dict[str, Any], name: str = "WC basin (app)") -> dict[str, Any]:
    """The share export with the first device renamed, as the app uploads it after a rename."""
    doc = copy.deepcopy(share)
    doc["meta"]["devices"][0]["name"] = name
    return doc


async def _later(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    """Let `seconds` pass for the timers, then for what they started (the fetch's background task, the follow).

    The test asks for `freezer` before the entry, so the timers the setup started run on the frozen clock. The
    gateway's status polls that come due meanwhile get no answer (and never reach the network).
    """
    freezer.tick(timedelta(seconds=seconds))
    with (
        patch.object(
            JungHomeGatewayApi,
            "config",
            AsyncMock(side_effect=GatewayError("not here")),
        ),
        patch.object(
            JungHomeGatewayApi,
            "health_status",
            AsyncMock(side_effect=GatewayError("not here")),
        ),
    ):
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    follower = hub_of_any(hass).app_follow
    assert follower is not None
    await wait_until(
        hass,
        lambda: follower.task is None or follower.task.done(),
        what="the fetch that follows the app",
    )
    await settle(hass)


def hub_of_any(hass: HomeAssistant) -> JungHomeHub:
    """The hub of the one loaded entry (a follow in place keeps it, a reload would replace it)."""
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    return hub_of(entry)  # type: ignore[arg-type]


class FetchCounter:
    """`fetch_project` patched onto the gateway client: counts the GETs, answers `answer` (or raises it)."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls = 0

    async def fetch_project(self) -> dict[str, Any]:
        """Patched onto the class as a bound method: called without the API instance."""
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer  # type: ignore[no-any-return]


async def _gateway_entry(
    hass: HomeAssistant,
    tmp_path: Path,
    *,
    options: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
) -> MockConfigEntry:
    """`gateway_entry` with other options or data (the test asks for the link and Bluetooth fixtures)."""
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
            **(data or {}),
        },
        options=dict(options or {}),
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    return entry


async def test_the_phone_heard_then_quiet_fetches_once_and_follows_the_app(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    gateway_entry: MockConfigEntry,
    answering_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
    state_transitions: StateTransitions,
) -> None:
    """Review-4 U4-6: the app renames a device and uploads its project; Home Assistant hears the phone on the mesh,
    waits for it to go quiet, asks the gateway once and follows the renamed export in place — no Reconfigure, no
    entity through `unavailable`, the gateway's copy recorded as synced."""
    hub = hub_of(gateway_entry)
    follower = hub.app_follow
    assert follower is not None
    share = _read_json(SHARE_EXPORT_PATH)
    gw = FetchCounter(_renamed(share))
    with patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project):
        answering_link.inject(PHONE, LIGHT_SWITCH, PHONE_GET)
        await settle(hass)
        assert gw.calls == 0  # nothing while the phone may still be at it
        await _later(hass, freezer, APP_QUIET_AFTER - 30)
        assert gw.calls == 0
        await _later(hass, freezer, 31)
    assert gw.calls == 1
    assert hub_of(gateway_entry) is hub
    assert hub.link_count == 1
    assert hub.devices.by_address[LIGHT_SWITCH].name == "WC basin (app)"
    assert _read_json(gateway_entry.data[CONF_CDB_PATH]) == _renamed(share)
    assert gateway_sync(hass, gateway_entry.entry_id).synced == export_digest(
        _renamed(share)
    )
    assert "following it" in caplog.text
    assert state_transitions.lost() == []


async def test_an_unchanged_export_is_not_followed(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    gateway_entry: MockConfigEntry,
    answering_link: FakeProxyLink,
) -> None:
    """The phone only switched a light: the gateway's digest has not moved, nothing is written or followed."""
    hub = hub_of(gateway_entry)
    original = _read_bytes(gateway_entry.data[CONF_CDB_PATH])
    gw = FetchCounter(_read_json(SHARE_EXPORT_PATH))
    with (
        patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project),
        patch.object(hub, "follow_adopted_export") as follow,
    ):
        answering_link.inject(PHONE, LIGHT_SWITCH, PHONE_GET)
        await settle(hass)
        await _later(hass, freezer, APP_QUIET_AFTER + 1)
    assert gw.calls == 1
    follow.assert_not_called()
    assert _read_bytes(gateway_entry.data[CONF_CDB_PATH]) == original


async def test_a_burst_of_app_edits_is_one_fetch_and_the_next_waits_out_the_rate_limit(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    gateway_entry: MockConfigEntry,
    answering_link: FakeProxyLink,
) -> None:
    """Every message from the phone restarts the wait, so a burst of edits is one GET once it is over; activity right
    after a fetch waits until APP_SYNC_MIN_INTERVAL passed since it."""
    gw = FetchCounter(_read_json(SHARE_EXPORT_PATH))
    with patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project):
        for _ in range(4):
            answering_link.inject(PHONE, LIGHT_SWITCH, PHONE_GET)
            answering_link.inject_from_provisioner(LIGHT_SWITCH, SUBSCRIPTION_ADD)
            await settle(hass)
            await _later(hass, freezer, APP_QUIET_AFTER / 2)
        assert gw.calls == 0
        await _later(hass, freezer, APP_QUIET_AFTER / 2 + 1)
        assert gw.calls == 1
        # the app again, right away: the quiet alone is not enough now
        answering_link.inject(PHONE, LIGHT_SWITCH, PHONE_GET)
        await settle(hass)
        await _later(hass, freezer, APP_QUIET_AFTER + 1)
        assert gw.calls == 1
        await _later(hass, freezer, APP_SYNC_MIN_INTERVAL + 1)
        assert gw.calls == 2


async def test_the_gateway_is_checked_every_few_hours_without_the_phone(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    gateway_entry: MockConfigEntry,
    answering_link: FakeProxyLink,
) -> None:
    """The periodic check: what the app changed while Home Assistant did not hear the phone."""
    share = _read_json(SHARE_EXPORT_PATH)
    gw = FetchCounter(_renamed(share, "Checked"))
    with patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project):
        await _later(hass, freezer, GATEWAY_SYNC_PERIOD + 1)
    assert gw.calls == 1
    assert hub_of(gateway_entry).devices.by_address[LIGHT_SWITCH].name == "Checked"


async def test_following_the_app_can_be_switched_off(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """Both options off: neither the phone nor the clock makes Home Assistant ask the gateway."""
    entry = await _gateway_entry(
        hass,
        tmp_path,
        options={OPTION_FOLLOW_APP: False, OPTION_GATEWAY_CHECK: False},
    )
    follower = hub_of(entry).app_follow
    assert follower is not None
    gw = FetchCounter(_read_json(SHARE_EXPORT_PATH))
    with patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project):
        answering_link.inject(PHONE, LIGHT_SWITCH, PHONE_GET)
        await settle(hass)
        assert follower._unsub_quiet is None
        await _later(hass, freezer, GATEWAY_SYNC_PERIOD + 1)
    assert gw.calls == 0


async def test_the_app_is_followed_only_by_every_guard_of_the_gateway(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Not while the token repair is open, nor with a pin the gateway node has not vouched for (that check is the
    link's, over the mesh: the automatic fetch sends nothing on it), nor beside a fetch in flight or before the
    configurator."""
    entry = await _gateway_entry(
        hass,
        tmp_path,
        data={CONF_GATEWAY_PIN_SOURCE: PIN_FROM_USER},  # nothing vouched for yet
    )
    hub = hub_of(entry)
    follower = hub.app_follow
    assert follower is not None
    share = _read_json(SHARE_EXPORT_PATH)
    gw = FetchCounter(_renamed(share))
    caplog.set_level(logging.DEBUG, logger="custom_components.junghome_ble.app_follow")
    with (
        patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project),
        patch.object(JungHomeHub, "gateway_vouched", PropertyMock(return_value=False)),
    ):
        follower.request("a test")
        await hass.async_block_till_done()
    assert gw.calls == 0
    assert "Not fetching the gateway's export for a test now" in caplog.text
    ir.async_create_issue(
        hass,
        DOMAIN,
        coordinator.issue_id(entry, ISSUE_GATEWAY_TOKEN),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_GATEWAY_TOKEN,
    )
    with (
        patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project),
        patch.object(JungHomeHub, "gateway_vouched", PropertyMock(return_value=True)),
    ):
        follower.request("a test")
        await hass.async_block_till_done()
        assert gw.calls == 0
        # the configurator answers the same for the button
        assert hub.configurator is not None
        assert await hub.configurator.adopt_if_gateway_changed() is False
        assert gw.calls == 0
        ir.async_delete_issue(
            hass, DOMAIN, coordinator.issue_id(entry, ISSUE_GATEWAY_TOKEN)
        )
        # a fetch in flight takes the request; the phone's quiet waits once more behind it (that fetch may have asked
        # before the app uploaded); no configurator yet: nothing
        follower.task = hass.loop.create_future()  # type: ignore[assignment]
        follower.request("a test")
        follower._quiet(dt_util.utcnow())
        assert follower._unsub_quiet is not None
        follower.stop()
        follower.task.cancel()
        configurator, hub.configurator = hub.configurator, None
        follower.request("a test")
        hub.configurator = configurator
        await hass.async_block_till_done()
        assert gw.calls == 0


async def test_home_assistants_own_change_is_not_replaced_by_the_export_it_left(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    gateway_entry: MockConfigEntry,
) -> None:
    """Home Assistant changed its file and the upload has not gone out yet: the gateway still holds what was synced,
    so it is not ahead and nothing is taken over."""
    hub = hub_of(gateway_entry)
    follower = hub.app_follow
    assert follower is not None
    share = _read_json(SHARE_EXPORT_PATH)
    mine = _renamed(share, "Home Assistant's name")
    await hass.async_add_executor_job(
        Path(gateway_entry.data[CONF_CDB_PATH]).write_text, json.dumps(mine)
    )
    gw = FetchCounter(share)
    with (
        patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project),
        patch.object(hub, "follow_adopted_export") as follow,
    ):
        follower.request("a test")
        await _later(hass, freezer, 0)
    assert gw.calls == 1
    follow.assert_not_called()
    assert _read_json(gateway_entry.data[CONF_CDB_PATH]) == mine


async def test_a_bare_database_is_never_taken_over_the_share_export(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The gateway's `/project/cdb` fallback has no names or room links: logged, not adopted; nor is anything when
    the gateway does not answer."""
    hub = hub_of(gateway_entry)
    original = _read_bytes(gateway_entry.data[CONF_CDB_PATH])
    bare = {"meshNetwork": _read_json(CDB_PATH)["meshNetwork"]}
    gw = FetchCounter(bare)
    assert hub.configurator is not None
    with patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project):
        assert await hub.configurator.adopt_if_gateway_changed() is False
        assert "service_gateway_export_incomplete" in caplog.text
        gw.answer = GatewayError("GET project/junghome: HTTP 500")
        assert await hub.configurator.adopt_if_gateway_changed() is False
    assert "Could not ask the gateway junghome.local for its export" in caplog.text
    assert _read_bytes(gateway_entry.data[CONF_CDB_PATH]) == original


def _fetch_button(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    gateway = next(n for n in hub_of(entry).cdb.nodes if n.unicast == GATEWAY)
    return entity_id(
        hass, "button", f"node:{gateway.uuid.lower()}-fetch_gateway_export"
    )


async def test_the_fetch_button_follows_the_app_on_demand(
    hass: HomeAssistant, gateway_entry: MockConfigEntry
) -> None:
    """*Fetch export from gateway*: the same fetch, now; a press that fetched nothing says why."""
    hub = hub_of(gateway_entry)
    button = _fetch_button(hass, gateway_entry)
    share = _read_json(SHARE_EXPORT_PATH)
    gw = FetchCounter(_renamed(share, "Pressed"))

    async def press() -> None:
        await hass.services.async_call(
            "button", "press", {"entity_id": button}, blocking=True
        )
        await settle(hass)

    with patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project):
        await press()
        assert gw.calls == 1
        assert hub.devices.by_address[LIGHT_SWITCH].name == "Pressed"
        await press()  # unchanged now: no error
        assert gw.calls == 2

        gw.answer = GatewayError("GET project/junghome: HTTP 500")
        with pytest.raises(HomeAssistantError) as err:
            await press()
        assert err.value.translation_key == "service_gateway_export_unavailable"

        gw.answer = {"meshNetwork": _read_json(CDB_PATH)["meshNetwork"]}
        with pytest.raises(HomeAssistantError) as err:
            await press()
        assert err.value.translation_key == "service_gateway_export_incomplete"

        with (
            patch.object(
                JungHomeHub,
                "async_gateway_distrust",
                AsyncMock(return_value=coordinator.GATEWAY_UNVERIFIED),
            ),
            pytest.raises(HomeAssistantError) as err,
        ):
            await press()
        assert err.value.translation_key == "gateway_fetch_refused"
        assert err.value.translation_placeholders == {
            "host": "junghome.local",
            "error": coordinator.GATEWAY_UNVERIFIED,
        }

        ir.async_create_issue(
            hass,
            DOMAIN,
            coordinator.issue_id(gateway_entry, ISSUE_GATEWAY_TOKEN),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_GATEWAY_TOKEN,
        )
        calls = gw.calls
        with pytest.raises(HomeAssistantError) as err:
            await press()
        assert err.value.translation_key == "gateway_fetch_refused"
        assert gw.calls == calls  # the gateway was not asked


async def test_an_entry_from_a_file_has_no_fetch_button_and_fetches_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    hub = hub_of(init_integration)
    gateway = next(n for n in hub.cdb.nodes if n.unicast == GATEWAY)
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "button", DOMAIN, f"node:{gateway.uuid.lower()}-fetch_gateway_export"
        )
        is None
    )
    assert hub.configurator is not None
    assert await hub.configurator.adopt_if_gateway_changed() is False
    with pytest.raises(ServiceValidationError) as err:
        await hub.configurator.adopt_if_gateway_changed(raise_errors=True)
    assert err.value.translation_key == "service_no_gateway"


async def test_the_app_changing_a_file_entrys_devices_raises_a_repair_once(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An entry set up from a file fetches nothing: the phone configuring a device raises `app_changed` (fixable by
    the new-export repair, kept across restarts), once; the phone's plain control, a node's own traffic and a Config
    read raise nothing."""
    hub = hub_of(init_integration)
    gw = FetchCounter(_read_json(SHARE_EXPORT_PATH))
    with patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project):
        fake_link.inject(PHONE, LIGHT_SWITCH, PHONE_GET)
        fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, onoff_status(True))
        fake_link.inject_from_provisioner(
            LIGHT_SWITCH, encode_opcode(C.CONFIG_COMPOSITION_DATA_GET) + b"\x00"
        )
        await settle(hass)
        assert find_issue(hass, ISSUE_APP_CHANGED) is None
        assert hub.app_follow is not None
        assert hub.app_follow._unsub_quiet is None  # nothing to fetch from
        fake_link.inject_from_provisioner(LIGHT_SWITCH, SUBSCRIPTION_ADD)
        await settle(hass)
        issue = find_issue(hass, ISSUE_APP_CHANGED)
        assert issue is not None
        assert issue.is_fixable
        assert issue.is_persistent
        assert issue.data == {"entry_id": init_integration.entry_id}
        assert issue.translation_placeholders == {"title": init_integration.title}
        assert issue.learn_more_url == learn_more_url(ISSUE_APP_CHANGED)
        assert caplog.text.count("changed the configuration of 0148") == 1
        # a scene edit too, but the notice is up already
        fake_link.inject(
            PHONE, LIGHT_SWITCH, encode_opcode(M.SCENE_STORE) + b"\x02\x00"
        )
        await settle(hass)
    assert caplog.text.count("load the app's new export") == 1
    assert gw.calls == 0


async def test_an_app_changed_notice_from_before_a_restart_is_not_raised_again(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        coordinator.issue_id(init_integration, ISSUE_APP_CHANGED),
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_APP_CHANGED,
    )
    fake_link.inject(PHONE, LIGHT_SWITCH, encode_opcode(M.SCENE_DELETE) + b"\x02\x00")
    await settle(hass)
    assert "load the app's new export" not in caplog.text


async def test_app_changes_are_not_watched_with_the_option_off(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=mock_config_entry.title,
        unique_id=mock_config_entry.unique_id,
        data=mock_config_entry.data,
        options={OPTION_FOLLOW_APP: False},
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    fake_link.inject_from_provisioner(LIGHT_SWITCH, SUBSCRIPTION_ADD)
    await settle(hass)
    assert find_issue(hass, ISSUE_APP_CHANGED) is None


async def test_unloading_stops_the_wait_for_the_phones_quiet(
    hass: HomeAssistant, gateway_entry: MockConfigEntry, answering_link: FakeProxyLink
) -> None:
    follower = hub_of(gateway_entry).app_follow
    assert follower is not None
    answering_link.inject(PHONE, LIGHT_SWITCH, PHONE_GET)
    await settle(hass)
    assert follower._unsub_quiet is not None
    assert await hass.config_entries.async_unload(gateway_entry.entry_id)
    assert follower._unsub_quiet is None
    assert follower._unsub_periodic is None


def test_what_counts_as_the_phone_and_as_a_change() -> None:
    """Home Assistant's own address and a device are not the phone; a vendor message or a Config Get no change."""
    hub = SimpleNamespace(
        proxy=SimpleNamespace(state=SimpleNamespace(src=OUR_ADDRESS)),
        cdb=SimpleNamespace(
            node_by_addr={
                LIGHT_SWITCH: SimpleNamespace(pid=1),
                PHONE: SimpleNamespace(pid=None),
            }.get
        ),
        entry=SimpleNamespace(options={}, entry_id="e"),
    )
    follower = app_follow.AppFollower(hub)  # type: ignore[arg-type]

    def msg(
        src: int, opcode: int, key: str = "app0", cid: int | None = None
    ) -> AccessMessage:
        return AccessMessage(src, LIGHT_SWITCH, 3, 0, opcode, cid, b"", b"", key)

    assert follower.from_phone(msg(PHONE, M.GEN_ONOFF_GET))
    assert follower.from_phone(msg(0x0D42, M.GEN_ONOFF_GET))  # no node at all
    assert not follower.from_phone(msg(OUR_ADDRESS, M.GEN_ONOFF_GET))
    assert not follower.from_phone(msg(LIGHT_SWITCH, M.GEN_ONOFF_GET))
    change = app_follow.is_app_change
    assert change(msg(PHONE, C.CONFIG_MODEL_PUBLICATION_SET, "dev:0148"))
    assert not change(
        msg(PHONE, C.CONFIG_MODEL_PUBLICATION_SET)
    )  # 0x03 under the app key is not Config
    assert not change(msg(PHONE, C.CONFIG_MODEL_PUBLICATION_GET, "dev:0148"))
    assert change(msg(PHONE, M.SCENE_STORE_UNACK))
    assert not change(msg(PHONE, M.SCENE_STORE, cid=M.JUNG_CID))
