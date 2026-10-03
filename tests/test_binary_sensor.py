"""Binary sensor platform: the detectors' motion / occupancy (detectors network) and the nodes' fault registers.

`tests/fixtures/MeshNetwork-detectors.json` (`make_fixture.build_detectors`) has a motion detector (relay 0500, sensor
element 0501 driving the relay's element group C0A0), a ceiling presence detector (relay 0510, sensor element 0511
driving the Living room group C010) and two battery wall transmitters (0520 with key 0521 linked to the gateway; 0530
with key A 0531 linked to the gateway and key B 0532 wired to the motion detector's relay). Nothing of this is verified
on hardware: the compositions follow the documented rules only.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.junghome_ble import binary_sensor
from custom_components.junghome_ble.binary_sensor import (
    JungHomeDetectorOccupancy,
    chain_status_handler,
    detector_at,
)
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_UNICAST,
    DETECTOR_MOTION_HOLD,
    DETECTOR_PROPERTY_ILLUMINANCE,
    DETECTOR_PROPERTY_PRESENCE,
    DOMAIN,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.devices import (
    BuildContext,
    Detector,
    Metadata,
    build_devices,
)
from custom_components.junghome_ble.jhmesh.pdu import encode_opcode

from .conftest import FIXTURES, FakeProxyLink, settle, setup_entry, wait_for_link
from .helpers import (
    GATEWAY,
    LIGHT_CTL,
    LIGHT_DIMMER,
    LIGHT_OUT1,
    LIGHT_SWITCH,
    NODE_ACTUATOR,
    NODE_LIGHT_CTL,
    NODE_LIGHT_SWITCH,
    NODE_MOTION,
    NODE_PRESENCE,
    NODE_SOCKET,
    NODE_TRANSMITTER_1G,
    NODE_TRANSMITTER_2G,
    OUR_ADDRESS,
    SOCKET,
    device_name_of,
    entity_id,
    sensor_status,
    vendor_button_event,
)
from .property_helpers import vendor_status

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

DETECTORS_CDB_PATH = str(FIXTURES / "MeshNetwork-detectors.json")

# elements of the detectors network
RELAY_MOTION, DETECTOR_MOTION = 0x0500, 0x0501
RELAY_PRESENCE, DETECTOR_PRESENCE = 0x0510, 0x0511
TRANSMITTER_1G, KEY_1G = 0x0520, 0x0521
TRANSMITTER_2G, KEY_2G_A, KEY_2G_B = 0x0530, 0x0531, 0x0532
GROUP_RELAY_MOTION, GROUP_SENSOR_MOTION = 0xC0A0, 0xC0A1
GROUP_SENSOR_PRESENCE, GROUP_LIVING = 0xC0A3, 0xC010
GROUP_GATEWAY = 0xC005

UUID_MOTION = NODE_MOTION.upper()
UUID_PRESENCE = NODE_PRESENCE.upper()
UUID_1G = NODE_TRANSMITTER_1G.upper()
UUID_2G = NODE_TRANSMITTER_2G.upper()
UID_MOTION = f"{UUID_MOTION.lower()}-0040-motion"
UID_OCCUPANCY = f"{UUID_PRESENCE.lower()}-0040-occupancy"
UID_KEY_1G = f"{UUID_1G.lower()}-0040"

BUTTON_CLICK = 0x05


def presence_status(detected: bool, lux_raw: bytes | None = None) -> bytes:
    """Sensor Status of a detector: Presence Detected (one byte), optionally with Present Illuminance."""
    values: list[tuple[int, bytes]] = [(DETECTOR_PROPERTY_PRESENCE, bytes([detected]))]
    if lux_raw is not None:
        values.append((DETECTOR_PROPERTY_ILLUMINANCE, lux_raw))
    return sensor_status(*values)


@pytest.fixture(autouse=True)
def no_property_reads() -> Generator[None]:
    """Keep the config entities' parameter reads (and the loads' lock reads) out of the traffic these tests look at."""
    with (
        patch(
            "custom_components.junghome_ble.config_entities.ConfigEntity._maybe_read"
        ),
        patch(
            "custom_components.junghome_ble.config_entities.LoadLock._maybe_read_lock"
        ),
    ):
        yield


def make_detectors_entry() -> MockConfigEntry:
    """A config entry for the detectors network (no metadata directory: names are the node labels)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME detectors test",
        unique_id="1fbd2c61a4b6e5a4",  # same fake keys as the main fixture: same Network ID, same fake proxy
        data={CONF_CDB_PATH: DETECTORS_CDB_PATH, CONF_UNICAST: "0D00"},
    )


async def start_detectors(
    hass: HomeAssistant, entry: MockConfigEntry, link: FakeProxyLink
) -> MockConfigEntry:
    """Set the entry up, let the hub attach to the fake link and run the connect-time refresh.

    The fake serves this network (same NetKey and AppKey as the main fixture): without its nodes' device keys a
    configuration message the hub sends them (a Sensor Server publication read reached once the timeouts are
    short) would be undecryptable, and the strict teardown would fail on whether the queue got there in time.
    """
    link.cdb = CDB.load(DETECTORS_CDB_PATH)
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    return entry


@pytest.fixture
def detectors_entry() -> MockConfigEntry:
    return make_detectors_entry()


@pytest.fixture
async def init_detectors(
    hass: HomeAssistant,
    detectors_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> MockConfigEntry:
    return await start_detectors(hass, detectors_entry, fake_link)


# --------------------------------------------------------------------------- device model


def test_detector_devices_from_the_detectors_network() -> None:
    """The Sensor Server element of a detector product is the detector; its relay stays a light; keys know their battery."""
    cdb = CDB.load(FIXTURES / "MeshNetwork-detectors.json")
    d = build_devices(cdb)
    assert [
        (x.address, x.kind, x.presence, x.relay_address, x.target, x.rooms)
        for x in d.detectors
    ] == [
        (DETECTOR_MOTION, "detector", False, RELAY_MOTION, GROUP_RELAY_MOTION, ["WC"]),
        (
            DETECTOR_PRESENCE,
            "detector",
            True,
            RELAY_PRESENCE,
            GROUP_LIVING,
            ["Living room"],
        ),
    ]
    motion = d.detectors[0]
    assert (motion.name, motion.unique_id, motion.location) == (
        "Motion detector 1 m 0500",
        f"{UUID_MOTION.lower()}-0040",
        0x40,
    )
    assert d.by_address[DETECTOR_MOTION] is motion
    assert [light.address for light in d.lights] == [RELAY_MOTION, RELAY_PRESENCE]
    assert [(b.address, b.key, b.battery) for b in d.buttons] == [
        (KEY_1G, "A", True),
        (KEY_2G_A, "A", True),
        (KEY_2G_B, "B", True),
    ]
    assert d.kinds() == {"switch", "button", "detector"}
    # the app's name for the detector element wins over the node label
    named = build_devices(
        cdb,
        Metadata.from_export(
            {
                "devices": [
                    {
                        "name": "Hallway detector",
                        "deviceId": {"nodeId": UUID_MOTION, "locationIds": [64]},
                    }
                ]
            }
        ),
    )
    assert named.detectors[0].name == "Hallway detector"
    assert named.detectors[1].name == "Presence detector 0510"


def test_detector_without_a_relay_and_odd_publications() -> None:
    """A detector node without an OnOff server has no relay and no rooms; unusable publish addresses are None."""
    raw = CDB.load(FIXTURES / "MeshNetwork-detectors.json").raw
    assert raw is not None
    node = next(n for n in raw["nodes"] if n["unicastAddress"] == "0500")
    node["elements"][0]["models"] = [
        m for m in node["elements"][0]["models"] if m["modelId"] != "1000"
    ]
    client = next(m for m in node["elements"][1]["models"] if m["modelId"] == "1001")
    client["publish"]["address"] = "0000"  # unassigned
    d = build_devices(CDB.from_network(raw))
    motion = d.detectors[0]
    assert (motion.relay_address, motion.target, motion.rooms) == (None, None, [])
    assert RELAY_MOTION not in d.by_address

    element = d.detectors[0].node.elements[1]
    client["publish"]["address"] = "0123456789ABCDEF0123456789ABCDEF"  # a virtual label
    assert BuildContext.publish_address(element, "1001") is None
    client["publish"]["address"] = "ZZZZ"
    assert BuildContext.publish_address(element, "1001") is None
    del client["publish"]
    assert BuildContext.publish_address(element, "1001") is None
    assert BuildContext.publish_address(element, "1100") == GROUP_SENSOR_MOTION
    assert BuildContext.publish_address(element, "9999") is None

    # the main fixture has no detector product: no detector is derived from the socket's meter element
    assert build_devices(CDB.load(FIXTURES / "MeshNetwork.json")).detectors == []


# --------------------------------------------------------------------------- entities


async def test_entities(hass: HomeAssistant, init_detectors: MockConfigEntry) -> None:
    """One motion entity per wall detector and one occupancy entity per ceiling detector, on the node device."""
    motion = entity_id(hass, "binary_sensor", UID_MOTION)
    occupancy = entity_id(hass, "binary_sensor", UID_OCCUPANCY)
    # the node device carries the detector's room (its relay's), so HA prefixes it like a load's
    assert motion == "binary_sensor.wc_motion_detector_1_m_0500_motion"
    assert occupancy == "binary_sensor.living_room_presence_detector_0510_occupancy"
    state = hass.states.get(motion)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_DEVICE_CLASS] == BinarySensorDeviceClass.MOTION
    assert state.attributes["mesh_address"] == "0501"
    assert state.attributes["relay"] == "0500"
    assert state.attributes["target"] == "C0A0"
    assert state.attributes["source"] is None
    presence = hass.states.get(occupancy)
    assert presence.attributes[ATTR_DEVICE_CLASS] == BinarySensorDeviceClass.OCCUPANCY
    assert presence.attributes["target"] == "C010"

    registry = er.async_get(hass)
    entry = registry.async_get(motion)
    assert entry is not None
    assert entry.entity_category is None
    device = dr.async_get(hass).async_get(entry.device_id)
    assert device is not None
    assert (DOMAIN, f"node:{UUID_MOTION.lower()}") in device.identifiers
    assert device.model == "Motion detector 1 m"
    assert (
        device.area_id == "wc"
    )  # the room of its relay output, like a load's own device
    # nothing else made a detector entity: the relays are lights, the transmitters' keys are events (the other
    # binary sensors are the nodes' fault registers and the gateway's API status)
    assert sorted(
        e.unique_id
        for e in er.async_entries_for_config_entry(registry, init_detectors.entry_id)
        if e.domain == "binary_sensor"
        and not e.unique_id.endswith("-fault")
        and "-gateway_" not in e.unique_id
    ) == sorted([UID_MOTION, UID_OCCUPANCY])
    hub = init_detectors.runtime_data
    assert detector_at(hub, DETECTOR_MOTION) is hub.devices.detectors[0]
    assert detector_at(hub, RELAY_MOTION) is None


async def test_detector_event_of_another_mesh_does_not_reach_this_entity(
    hass: HomeAssistant, init_detectors: MockConfigEntry
) -> None:
    """PLT-02: another mesh's detector at the same unicast must not switch this entity's motion state."""
    motion = entity_id(hass, "binary_sensor", UID_MOTION)
    other = SimpleNamespace(
        hass=hass,
        entry=SimpleNamespace(entry_id="other"),
        devices=init_detectors.runtime_data.devices,
    )
    binary_sensor._on_detector_onoff_set(
        other, SimpleNamespace(src=DETECTOR_MOTION), b"\x01"
    )
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == STATE_UNKNOWN


async def test_sensor_values_are_asked_for_after_every_connection(
    hass: HomeAssistant,
    init_detectors: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    """Each detector gets one property-qualified Sensor Get per value and link (the hub's own refresh only covers
    the relays); the unqualified form is the one JUNG's sensor server ignores."""
    gets = [(dst, pdu) for _, dst, pdu in fake_link.sent if pdu[:2] == M.sensor_get()]
    assert gets == [
        (DETECTOR_MOTION, M.sensor_get(DETECTOR_PROPERTY_PRESENCE)),
        (DETECTOR_MOTION, M.sensor_get(DETECTOR_PROPERTY_ILLUMINANCE)),
        (DETECTOR_PRESENCE, M.sensor_get(DETECTOR_PROPERTY_PRESENCE)),
        (DETECTOR_PRESENCE, M.sensor_get(DETECTOR_PROPERTY_ILLUMINANCE)),
    ]
    assert M.sensor_get() not in [pdu for _, _, pdu in fake_link.sent]
    assert (OUR_ADDRESS, RELAY_MOTION, M.generic_onoff_get()) in fake_link.sent

    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert (
        hass.states.get(entity_id(hass, "binary_sensor", UID_MOTION)).state
        == STATE_UNAVAILABLE
    )
    fake_link.sent.clear()
    mock_bluetooth_env["infos"] = infos
    mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
    await wait_for_link(hass, init_detectors)
    await settle(hass)
    assert (
        hass.states.get(entity_id(hass, "binary_sensor", UID_MOTION)).state
        == STATE_UNKNOWN
    )
    assert [dst for _, dst, pdu in fake_link.sent if pdu[:2] == M.sensor_get()] == [
        DETECTOR_MOTION,
        DETECTOR_MOTION,
        DETECTOR_PRESENCE,
        DETECTOR_PRESENCE,
    ]


async def test_sensor_get_reply_is_matched_on_its_property(
    hass: HomeAssistant, init_detectors: MockConfigEntry
) -> None:
    """Each of the two outstanding Gets takes only the Status carrying its property; a Status that does not parse
    (truncated) matches neither."""
    hub = init_detectors.runtime_data
    entity = next(
        e
        for e in hass.data["entity_components"]["binary_sensor"].entities
        if isinstance(e, JungHomeDetectorOccupancy) and e.address == DETECTOR_MOTION
    )
    with patch.object(hub.proxy, "request", AsyncMock()) as request:
        await entity._refresh()
    matchers = {call.kwargs["match"]: call.args[1] for call in request.await_args_list}
    assert sorted(matchers.values()) == sorted(
        [
            M.sensor_get(DETECTOR_PROPERTY_PRESENCE),
            M.sensor_get(DETECTOR_PROPERTY_ILLUMINANCE),
        ]
    )
    presence = next(
        m
        for m, get in matchers.items()
        if get == M.sensor_get(DETECTOR_PROPERTY_PRESENCE)
    )
    params = presence_status(True)[1:]  # the Status after its one-byte opcode
    assert presence(SimpleNamespace(params=params))
    assert not presence(SimpleNamespace(params=params[:-1]))  # truncated
    assert not presence(
        SimpleNamespace(
            params=sensor_status((DETECTOR_PROPERTY_ILLUMINANCE, b"\x10\x27"))[1:]
        )
    )


async def test_sensor_get_of_a_lost_link_is_repeated_with_the_next_one(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A Sensor Get the link swallowed (write failure) counts as not done; a silent detector is left alone."""
    hub = init_detectors.runtime_data
    entity = next(
        e
        for e in hass.data["entity_components"]["binary_sensor"].entities
        if isinstance(e, JungHomeDetectorOccupancy) and e.address == DETECTOR_MOTION
    )
    assert entity._refreshed
    fake_link.write_error = OSError("GATT write failed")
    entity._refreshed = False
    entity._maybe_refresh()
    await settle(hass)
    assert (
        not entity._refreshed
    )  # the write failed before anything was sent: try again with the next link

    fake_link.write_error = None
    with patch.object(hub.proxy, "request", side_effect=TimeoutError("no response")):
        entity._maybe_refresh()
        await settle(hass)
    assert (
        entity._refreshed
    )  # asked; the detector did not answer, which is not repeated


async def test_truncated_sensor_status_is_dropped_quietly(
    hass: HomeAssistant,
    init_detectors: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PLT-01: a detector's Sensor Status cut short is dropped by the chained detector handler, not raised into
    the client's "on_message handler failed" ERROR traceback; the next good one still counts."""
    motion = entity_id(hass, "binary_sensor", UID_MOTION)
    fake_link.inject(
        DETECTOR_MOTION, GROUP_SENSOR_MOTION, encode_opcode(M.SENSOR_STATUS) + b"\x9e"
    )
    await hass.async_block_till_done()
    assert "on_message handler failed" not in caplog.text
    assert hass.states.get(motion).state == STATE_UNKNOWN
    fake_link.inject(DETECTOR_MOTION, GROUP_SENSOR_MOTION, presence_status(True))
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == STATE_ON


async def test_presence_status_drives_the_state(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    motion = entity_id(hass, "binary_sensor", UID_MOTION)
    occupancy = entity_id(hass, "binary_sensor", UID_OCCUPANCY)
    hub = init_detectors.runtime_data

    fake_link.inject(
        DETECTOR_MOTION, GROUP_SENSOR_MOTION, presence_status(True, b"\x39\x30\x00")
    )
    await hass.async_block_till_done()
    state = hass.states.get(motion)
    assert state.state == STATE_ON
    assert state.attributes["source"] == "sensor_status"
    assert hass.states.get(occupancy).state == STATE_UNKNOWN  # another detector
    assert hub.states[DETECTOR_MOTION].properties == {
        DETECTOR_PROPERTY_PRESENCE: b"\x01",
        DETECTOR_PROPERTY_ILLUMINANCE: b"\x39\x30\x00",
    }

    # the answer to our Sensor Get comes unicast; a status without the presence property changes nothing
    fake_link.inject(DETECTOR_PRESENCE, OUR_ADDRESS, presence_status(True))
    fake_link.inject(
        DETECTOR_MOTION,
        GROUP_SENSOR_MOTION,
        sensor_status((DETECTOR_PROPERTY_ILLUMINANCE, b"\x10\x00\x00")),
    )
    fake_link.inject(
        DETECTOR_MOTION, GROUP_SENSOR_MOTION, sensor_status((0x0081, b"\x01\x00"))
    )  # not a detector property
    await hass.async_block_till_done()
    assert hass.states.get(occupancy).state == STATE_ON
    assert hass.states.get(motion).state == STATE_ON
    assert (
        hub.states[DETECTOR_MOTION].properties[DETECTOR_PROPERTY_ILLUMINANCE]
        == b"\x10\x00\x00"
    )
    assert 0x0081 not in hub.states[DETECTOR_MOTION].properties

    fake_link.inject(DETECTOR_MOTION, GROUP_SENSOR_MOTION, presence_status(False))
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == STATE_OFF

    # a Sensor Status from something that is not a detector (the relay) is not a detector reading
    fake_link.inject(RELAY_MOTION, GROUP_RELAY_MOTION, presence_status(True))
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == STATE_OFF
    assert RELAY_MOTION not in hub.states or not hub.states[RELAY_MOTION].properties


async def test_onoff_publication_is_motion_with_a_hold(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The detector switching its load means motion; it clears after DETECTOR_MOTION_HOLD, or at once on an off."""
    motion = entity_id(hass, "binary_sensor", UID_MOTION)
    relay = entity_id(hass, "light", f"{UUID_MOTION.lower()}-0001")

    fake_link.inject(
        DETECTOR_MOTION, GROUP_RELAY_MOTION, M.generic_onoff_set(True, ack=False, tid=1)
    )
    await hass.async_block_till_done()
    state = hass.states.get(motion)
    assert state.state == STATE_ON
    assert state.attributes["source"] == "onoff_set"
    assert (
        hass.states.get(relay).state == STATE_UNKNOWN
    )  # the load reports itself with a status, not our business here

    # the firmware's second copy re-arms the hold; the hold then clears the motion
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=DETECTOR_MOTION_HOLD - 5)
    )
    await hass.async_block_till_done()
    fake_link.inject(
        DETECTOR_MOTION, GROUP_RELAY_MOTION, M.generic_onoff_set(True, ack=False, tid=1)
    )
    await hass.async_block_till_done()
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=DETECTOR_MOTION_HOLD - 2)
    )
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == STATE_ON
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=DETECTOR_MOTION_HOLD + 1)
    )
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == STATE_OFF

    # an acknowledged Set counts too; an off publication clears at once
    fake_link.inject(
        DETECTOR_MOTION, GROUP_RELAY_MOTION, M.generic_onoff_set(True, ack=True, tid=2)
    )
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == STATE_ON
    fake_link.inject(
        DETECTOR_MOTION,
        GROUP_RELAY_MOTION,
        M.generic_onoff_set(False, ack=False, tid=3),
    )
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == STATE_OFF
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=DETECTOR_MOTION_HOLD + 1)
    )
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == STATE_OFF

    # a Set without parameters is nothing
    fake_link.inject(
        DETECTOR_MOTION, GROUP_RELAY_MOTION, encode_opcode(M.GEN_ONOFF_SET_UNACK)
    )
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == STATE_OFF


async def test_presence_status_wins_over_the_hold(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A Presence Detected status cancels a running hold in either direction."""
    motion = entity_id(hass, "binary_sensor", UID_MOTION)
    fake_link.inject(
        DETECTOR_MOTION, GROUP_RELAY_MOTION, M.generic_onoff_set(True, ack=False, tid=1)
    )
    await hass.async_block_till_done()
    fake_link.inject(DETECTOR_MOTION, GROUP_SENSOR_MOTION, presence_status(False))
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == STATE_OFF

    fake_link.inject(
        DETECTOR_MOTION, GROUP_RELAY_MOTION, M.generic_onoff_set(True, ack=False, tid=2)
    )
    await hass.async_block_till_done()
    fake_link.inject(DETECTOR_MOTION, GROUP_SENSOR_MOTION, presence_status(True))
    await hass.async_block_till_done()
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=DETECTOR_MOTION_HOLD + 1)
    )
    await hass.async_block_till_done()
    assert (
        hass.states.get(motion).state == STATE_ON
    )  # the status said so; no hold is running


def test_chain_status_handler_behind_nothing_and_behind_a_shared_handler() -> None:
    """A chained handler runs alone when the opcode had none; opcodes sharing a handler share the chained one."""
    calls: list[tuple[int, bytes]] = []
    table = binary_sensor.STATUS_HANDLERS
    with patch.dict(table):
        table[None, 0x7FFE] = lambda hub, m, p: calls.append((0, p))
        table[None, 0x7FFD] = table[None, 0x7FFE]
        chain_status_handler(0x7FFF, 0x7FFE, 0x7FFD)(
            lambda hub, m, p: calls.append((1, p))
        )
        assert table[None, 0x7FFE] is table[None, 0x7FFD]
        assert table[None, 0x7FFF] is not table[None, 0x7FFE]
        table[None, 0x7FFF](None, None, b"a")  # type: ignore[arg-type]
        table[None, 0x7FFE](None, None, b"b")  # type: ignore[arg-type]
    assert calls == [(1, b"a"), (0, b"b"), (1, b"b")]
    assert (None, 0x7FFF) not in table
    assert (None, M.GEN_ONOFF_SET) in table  # the real rows are untouched


async def test_chained_handlers_keep_the_coordinator_behaviour(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The rocker events of OnOff Sets from keys still fire, and a key's Set is no motion."""
    key_b = entity_id(hass, "event", f"{UUID_2G.lower()}-0041")
    motion = entity_id(hass, "binary_sensor", UID_MOTION)
    fake_link.inject(
        KEY_2G_B, GROUP_RELAY_MOTION, M.generic_onoff_set(True, ack=False, tid=7)
    )
    await hass.async_block_till_done()
    assert hass.states.get(key_b).attributes["event_type"] == "press_on"
    assert hass.states.get(motion).state == STATE_UNKNOWN


async def test_state_is_seeded_from_the_cache(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """An entity added after the detector reported (a re-added platform) starts from the cached reading."""
    hub = init_detectors.runtime_data
    hub.element_state(DETECTOR_PRESENCE).properties[DETECTOR_PROPERTY_PRESENCE] = (
        b"\x01"
    )
    entity = JungHomeDetectorOccupancy(hub, hub.devices.detectors[1])
    entity.hass = hass
    entity.entity_id = "binary_sensor.probe"
    await entity.async_added_to_hass()
    assert entity.is_on is True
    assert entity.extra_state_attributes["source"] == "sensor_status"
    fresh = JungHomeDetectorOccupancy(hub, hub.devices.detectors[0])
    fresh.hass = hass
    fresh.entity_id = "binary_sensor.probe2"
    await fresh.async_added_to_hass()
    assert fresh.is_on is None


async def test_removed_entity_drops_its_hold(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    motion = entity_id(hass, "binary_sensor", UID_MOTION)
    fake_link.inject(
        DETECTOR_MOTION, GROUP_RELAY_MOTION, M.generic_onoff_set(True, ack=False, tid=1)
    )
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == STATE_ON
    assert await hass.config_entries.async_unload(init_detectors.entry_id)
    await hass.async_block_till_done()
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=DETECTOR_MOTION_HOLD + 1)
    )
    await (
        hass.async_block_till_done()
    )  # the hold was cancelled with the entity: nothing to write


async def test_button_events_of_the_transmitters(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The transmitters' keys are ordinary event entities (the battery sensors of tests/test_sensor.py hang on them)."""
    key = entity_id(hass, "event", UID_KEY_1G)
    fake_link.inject(KEY_1G, GROUP_GATEWAY, vendor_button_event(1, BUTTON_CLICK))
    await hass.async_block_till_done()
    assert hass.states.get(key).attributes["event_type"] == "click"
    assert isinstance(
        init_detectors.runtime_data.devices.by_address[DETECTOR_MOTION], Detector
    )


async def test_battery_nodes_get_no_health_entities_and_are_not_surveyed(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A wall transmitter sleeps between key presses: no Fault / Identify / Clear faults, no Health Fault Get."""
    registry = er.async_get(hass)
    unique_ids = {
        e.unique_id
        for e in er.async_entries_for_config_entry(registry, init_detectors.entry_id)
    }
    for suffix in ("fault", "identify", "clear-faults"):
        assert f"node:{UUID_MOTION.lower()}-{suffix}" in unique_ids
        assert f"node:{UUID_1G.lower()}-{suffix}" not in unique_ids
        assert f"node:{UUID_2G.lower()}-{suffix}" not in unique_ids
    answer_fault_gets(fake_link, 0x81)
    fake_link.sent.clear()
    await init_detectors.runtime_data._get_faults()
    asked = {dst for _, dst, pdu in fake_link.sent if pdu == M.health_fault_get()}
    assert RELAY_MOTION in asked
    assert asked.isdisjoint({TRANSMITTER_1G, TRANSMITTER_2G})


async def test_onoff_off_schedules_no_hold(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """An *off* publication switches off at once and arms no hold timer; only an *on* does.

    A timer armed by an off would fire `_hold_expired` later and write the (unchanged) off state again — invisible
    in the state machine, so the scheduling itself is asserted.
    """
    motion = entity_id(hass, "binary_sensor", UID_MOTION)
    with patch(
        "custom_components.junghome_ble.binary_sensor.async_call_later",
        wraps=binary_sensor.async_call_later,
    ) as call_later:
        fake_link.inject(
            DETECTOR_MOTION,
            GROUP_RELAY_MOTION,
            M.generic_onoff_set(False, ack=False, tid=1),
        )
        await hass.async_block_till_done()
        assert hass.states.get(motion).state == STATE_OFF
        assert hass.states.get(motion).attributes["source"] == "onoff_set"
        call_later.assert_not_called()

        fake_link.inject(
            DETECTOR_MOTION,
            GROUP_RELAY_MOTION,
            M.generic_onoff_set(True, ack=False, tid=2),
        )
        await hass.async_block_till_done()
        assert hass.states.get(motion).state == STATE_ON
        assert call_later.call_count == 1
        assert call_later.call_args.args[1] == DETECTOR_MOTION_HOLD


async def test_motion_hold_is_the_relays_run_on_time(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The hold after an OnOff Set is the run-on time (0x1007) of the detector's relay once it has been read."""
    motion = entity_id(hass, "binary_sensor", UID_MOTION)
    entity = hass.data["entity_components"]["binary_sensor"].get_entity(motion)
    assert entity.hold_seconds == DETECTOR_MOTION_HOLD  # not read yet
    # the relay's run-on time entity reads it (or the relay publishes it): 30 s
    fake_link.inject(
        RELAY_MOTION,
        OUR_ADDRESS,
        vendor_status(0x05, 0x1007, (30000).to_bytes(4, "little")),
    )
    await hass.async_block_till_done()
    assert entity.hold_seconds == 30.0
    start = dt_util.utcnow()
    fake_link.inject(
        DETECTOR_MOTION, GROUP_RELAY_MOTION, M.generic_onoff_set(True, ack=False, tid=1)
    )
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == STATE_ON
    async_fire_time_changed(hass, start + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == STATE_OFF
    # zero means "stays on until switched off": no hold of its own, the default applies
    fake_link.inject(RELAY_MOTION, OUR_ADDRESS, vendor_status(0x05, 0x1007, bytes(4)))
    await hass.async_block_till_done()
    assert entity.hold_seconds == DETECTOR_MOTION_HOLD
    # a detector without a relay of its own has none to read
    hub = init_detectors.runtime_data
    bare = JungHomeDetectorOccupancy(
        hub, replace(hub.devices.detectors[0], relay_address=None)
    )
    assert bare.hold_seconds == DETECTOR_MOTION_HOLD


# --------------------------------------------------------------------------- fault register (main network)

UID_FAULT_WC = f"node:{NODE_LIGHT_SWITCH}-fault"
UID_FAULT_ROCKER = f"node:{NODE_LIGHT_CTL}-fault"
UID_FAULT_SOCKET = f"node:{NODE_SOCKET}-fault"
UID_FAULT_PUCK = f"node:{NODE_ACTUATOR}-fault"
FAULT_NODES = [GATEWAY, LIGHT_SWITCH, LIGHT_CTL, SOCKET, LIGHT_DIMMER, LIGHT_OUT1]


def fault_status(*faults: int, test_id: int = 0) -> bytes:
    """Health Fault Status of a JUNG node: `[test id][company 0527][fault ids…]`."""
    return (
        encode_opcode(M.HEALTH_FAULT_STATUS)
        + bytes([test_id])
        + M.JUNG_CID.to_bytes(2, "little")
        + bytes(faults)
    )


def answer_fault_gets(link: FakeProxyLink, *faults: int) -> None:
    """Make every node answer its Health Fault Get with the given codes (the hub's connect-time survey on a
    non-answering fake link stalls on the state refresh's timeouts, so the survey is driven directly here)."""
    original = link.write_gatt_char

    async def write_and_answer(
        char: str, data: bytes, response: bool | None = None
    ) -> None:
        before = len(link.sent)
        await original(char, data, response)
        for src, dst, access in link.sent[before:]:
            if access == M.health_fault_get():
                link.inject(dst, src, fault_status(*faults))

    link.write_gatt_char = write_and_answer  # type: ignore[method-assign]


async def test_fault_register_is_asked_of_every_node_and_shown_where_identify_is(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """One diagnostic *problem* entity per node, unknown until the node answered the Health Fault Get.

    The survey's place in the connect sequence is asserted in test_coordinator.py; here it runs by itself.
    """
    wc = entity_id(hass, "binary_sensor", UID_FAULT_WC)
    assert wc == "binary_sensor.wc_mirror_button_fault"
    assert (
        device_name_of(hass, wc) == "WC mirror button"
    )  # a push-button's keys, like Identify
    assert device_name_of(hass, entity_id(hass, "binary_sensor", UID_FAULT_ROCKER)) == (
        "Living room rocker"
    )
    socket = entity_id(hass, "binary_sensor", UID_FAULT_SOCKET)
    assert socket == "binary_sensor.kitchen_boiler_fault"
    assert device_name_of(hass, socket) == "Boiler"
    assert device_name_of(hass, entity_id(hass, "binary_sensor", UID_FAULT_PUCK)) == (
        "2-channel actuator 0400"
    )
    entry = er.async_get(hass).async_get(wc)
    assert entry is not None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    state = hass.states.get(wc)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_DEVICE_CLASS] == BinarySensorDeviceClass.PROBLEM
    assert state.attributes["faults"] is None

    hub = init_integration.runtime_data
    answer_fault_gets(fake_link, 0x81)
    fake_link.sent.clear()
    await hub._get_faults()
    await hass.async_block_till_done()
    # every node was asked once, the phone (no product) not at all; the answers filled the entities
    assert [dst for _, dst, pdu in fake_link.sent if pdu == M.health_fault_get()] == (
        FAULT_NODES
    )
    for uid in (UID_FAULT_WC, UID_FAULT_ROCKER, UID_FAULT_SOCKET, UID_FAULT_PUCK):
        state = hass.states.get(entity_id(hass, "binary_sensor", uid))
        assert state.state == STATE_ON
        assert state.attributes["faults"] == ["0x81 (vendor)"]


async def test_fault_status_drives_the_entity(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The registered codes make a problem; an empty register or an explicit *no fault* entry does not."""
    wc = entity_id(hass, "binary_sensor", UID_FAULT_WC)
    hub = init_integration.runtime_data

    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, fault_status(0x81, 0x80))
    await hass.async_block_till_done()
    state = hass.states.get(wc)
    assert state.state == STATE_ON
    assert state.attributes["faults"] == ["0x81 (vendor)", "0x80 (vendor)"]
    assert hub.states[LIGHT_SWITCH].faults == (0x81, 0x80)

    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, fault_status(0x00, test_id=0))
    await hass.async_block_till_done()
    state = hass.states.get(wc)
    assert state.state == STATE_OFF
    assert state.attributes["faults"] == []

    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, fault_status(0x01))
    await hass.async_block_till_done()
    state = hass.states.get(wc)
    assert state.state == STATE_ON
    assert state.attributes["faults"] == ["0x01 battery low warning"]

    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, fault_status())
    await hass.async_block_till_done()
    assert hass.states.get(wc).state == STATE_OFF

    # a truncated status (no company id) changes nothing; a Health Current Status is not the register
    fake_link.inject(
        LIGHT_SWITCH, OUR_ADDRESS, encode_opcode(M.HEALTH_FAULT_STATUS) + b"\x00"
    )
    fake_link.inject(
        LIGHT_SWITCH,
        OUR_ADDRESS,
        encode_opcode(M.HEALTH_CURRENT_STATUS) + bytes([0, 0x27, 0x05, 0x81]),
    )
    await hass.async_block_till_done()
    assert hass.states.get(wc).state == STATE_OFF
    assert hub.states[LIGHT_SWITCH].faults == ()


async def test_fault_register_survives_a_lost_link(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    """The last register is kept over a lost link (unavailable meanwhile) until the next survey replaces it."""
    wc = entity_id(hass, "binary_sensor", UID_FAULT_WC)
    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, fault_status(0x81))
    await hass.async_block_till_done()
    assert hass.states.get(wc).state == STATE_ON

    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert hass.states.get(wc).state == STATE_UNAVAILABLE
    mock_bluetooth_env["infos"] = infos
    mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
    await wait_for_link(hass, init_integration)
    await settle(hass)
    state = hass.states.get(wc)
    assert state.state == STATE_ON
    assert state.attributes["faults"] == ["0x81 (vendor)"]


async def test_fault_survey_stops_with_the_link(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A link that fails under the survey ends it quietly; a silent node is left alone."""
    hub = init_integration.runtime_data
    fake_link.sent.clear()
    fake_link.write_error = OSError("GATT write failed")
    await hub._get_faults()
    assert not fake_link.sent
    fake_link.write_error = None
    with patch.object(hub.proxy, "request", side_effect=TimeoutError("no response")):
        await hub._get_faults()
    assert (
        hub.states.get(LIGHT_SWITCH) is None or hub.states[LIGHT_SWITCH].faults is None
    )
