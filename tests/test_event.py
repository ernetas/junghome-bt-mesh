"""Event platform: push-buttons and rockers."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.components.event import (
    ATTR_EVENT_TYPE,
    ATTR_EVENT_TYPES,
    EventDeviceClass,
)
from homeassistant.const import ATTR_DEVICE_CLASS, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.junghome_ble.const import (
    DIM_HOLD_MAX,
    DIM_HOLD_QUIET,
    DOMAIN,
    EVENT_BUTTON_ACTION,
    EVENT_SCENE_RECALLED,
)
from custom_components.junghome_ble.entity import (
    buttons_device_info,
    current_device_identifiers,
    light_device_info,
    node_device_info,
    socket_device_info,
)
from custom_components.junghome_ble.event import (
    EVENT_TYPES,
    JungHomeButtonEvent,
    fire_scene_recalled,
    publish_button_event,
    scene_name,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.devices import (
    Button,
    Devices,
    Light,
    Metadata,
    Socket,
    build_devices,
)
from custom_components.junghome_ble.jhmesh.pdu import encode_opcode

from .conftest import FakeProxyLink, settle, setup_entry, wait_for_link
from .helpers import (
    BUTTON_CLICK,
    BUTTON_DIMMER,
    BUTTON_HOLD_END,
    BUTTON_HOLD_START,
    BUTTON_WC,
    GROUP_DIMMER,
    NODE_LIGHT_CTL,
    ROCKER_A,
    ROCKER_B,
    UID_BUTTON_DIMMER,
    UID_BUTTON_WC,
    UID_ROCKER_A,
    UID_ROCKER_B,
    entity_id,
    vendor_button_event,
)
from .property_helpers import with_inserts

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.junghome_ble.jhmesh.cdb import CDB

NODE_ROCKER = NODE_LIGHT_CTL.upper()  # the 2-gang push-button (keys 0234 / 0235)
NODE_ROCKER_ID = f"node:{NODE_ROCKER.lower()}"


def devices_with_rocker_names(cdb: CDB, *names: tuple[list[int], str]) -> Devices:
    """The fixture network with the 2-gang node's keys named by `names` (app device entries); nothing else is named."""
    entries = [
        {"name": name, "deviceId": {"nodeId": NODE_ROCKER, "locationIds": locs}}
        for locs, name in names
    ]
    return build_devices(cdb, Metadata.from_export({"devices": entries}))


async def test_entities(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    # a node with one key: the button device *is* the entity
    wc = hass.states.get(entity_id(hass, "event", UID_BUTTON_WC))
    assert wc is not None
    assert wc.entity_id == "event.wc_mirror_button"
    assert wc.state == STATE_UNKNOWN
    assert wc.attributes[ATTR_DEVICE_CLASS] == EventDeviceClass.BUTTON
    assert wc.attributes[ATTR_EVENT_TYPES] == EVENT_TYPES
    assert wc.attributes["mesh_address"] == "0149"
    assert wc.attributes["location"] == "0040"

    # a rocker: one entity per key, named after the key
    rocker_a = hass.states.get(entity_id(hass, "event", UID_ROCKER_A))
    rocker_b = hass.states.get(entity_id(hass, "event", UID_ROCKER_B))
    assert rocker_a is not None
    assert rocker_a.entity_id == "event.living_room_rocker_button_a"
    assert rocker_b is not None
    assert rocker_b.entity_id == "event.living_room_rocker_button_b"
    assert rocker_a.attributes["friendly_name"] == "Living room rocker Button A"
    assert rocker_b.attributes["location"] == "0041"

    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    a, b = (
        registry.async_get(rocker_a.entity_id),
        registry.async_get(rocker_b.entity_id),
    )
    assert a is not None
    assert b is not None
    assert a.device_id == b.device_id  # both keys on one button device
    device = devices.async_get(a.device_id)
    assert device is not None
    assert device.identifiers == {
        (DOMAIN, f"{UID_ROCKER_A}-buttons")
    }  # keyed on the gang's first key
    assert device.name == "Living room rocker"
    node = devices.async_get_device_by_identifier(
        (DOMAIN, NODE_ROCKER_ID), init_integration.entry_id
    )
    assert node is not None
    assert device.via_device_id == node.id


async def test_one_device_per_gang(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    cdb: CDB,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """A 2-gang node whose rockers are named separately in the app gets one device per gang, each named after it."""
    devices = devices_with_rocker_names(
        cdb, ([0x40, 0x44], "Left rocker"), ([0x41, 0x44], "Right rocker")
    )
    with patch(
        "custom_components.junghome_ble.load_network", return_value=(cdb, devices)
    ):
        await setup_entry(hass, mock_config_entry)

    registry, devreg = er.async_get(hass), dr.async_get(hass)
    a, b = (
        registry.async_get(entity_id(hass, "event", UID_ROCKER_A)),
        registry.async_get(entity_id(hass, "event", UID_ROCKER_B)),
    )
    assert a is not None
    assert b is not None
    assert a.device_id != b.device_id
    assert (a.entity_id, b.entity_id) == (
        "event.left_rocker",
        "event.right_rocker",
    )  # one key per gang: the device *is* the button
    left, right = devreg.async_get(a.device_id), devreg.async_get(b.device_id)
    assert left is not None
    assert right is not None
    assert left.identifiers == {(DOMAIN, f"{UID_ROCKER_A}-buttons")}
    assert right.identifiers == {(DOMAIN, f"{UID_ROCKER_B}-buttons")}
    assert (left.name, right.name) == ("Left rocker", "Right rocker")
    node = devreg.async_get_device_by_identifier(
        (DOMAIN, NODE_ROCKER_ID), mock_config_entry.entry_id
    )
    assert node is not None
    assert left.via_device_id == node.id
    assert right.via_device_id == node.id
    assert {
        (DOMAIN, f"{UID_ROCKER_A}-buttons"),
        (DOMAIN, f"{UID_ROCKER_B}-buttons"),
    } <= current_device_identifiers(mock_config_entry.runtime_data)


async def test_same_named_gangs_stay_separate_devices(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    cdb: CDB,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """Gangs are keyed on the app's device entries (their location sets, `Button.gang`), not on their names:
    two rockers of one node the user gave the same name are still two devices, each named that."""
    devices = devices_with_rocker_names(
        cdb, ([0x40, 0x44], "Rocker"), ([0x41, 0x44], "Rocker")
    )
    with patch(
        "custom_components.junghome_ble.load_network", return_value=(cdb, devices)
    ):
        await setup_entry(hass, mock_config_entry)

    hub = mock_config_entry.runtime_data
    assert hub.metadata is devices.metadata
    assert hub.metadata.entry_for(NODE_ROCKER, 0x41) == ([0x41, 0x44], "Rocker")
    registry, devreg = er.async_get(hass), dr.async_get(hass)
    a, b = (
        registry.async_get(entity_id(hass, "event", UID_ROCKER_A)),
        registry.async_get(entity_id(hass, "event", UID_ROCKER_B)),
    )
    assert a is not None
    assert b is not None
    assert a.device_id != b.device_id
    assert (a.entity_id, b.entity_id) == ("event.rocker", "event.rocker_2")
    left, right = devreg.async_get(a.device_id), devreg.async_get(b.device_id)
    assert left is not None
    assert right is not None
    assert left.identifiers == {(DOMAIN, f"{UID_ROCKER_A}-buttons")}
    assert right.identifiers == {(DOMAIN, f"{UID_ROCKER_B}-buttons")}
    assert (left.name, right.name) == ("Rocker", "Rocker")


async def test_keys_without_app_names_share_one_device(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    cdb: CDB,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """Without app metadata every key of a node forms one gang: one device per node, named after the node."""
    with patch(
        "custom_components.junghome_ble.load_network",
        return_value=(cdb, devices_with_rocker_names(cdb)),
    ):
        await setup_entry(hass, mock_config_entry)

    registry = er.async_get(hass)
    a, b = (
        registry.async_get(entity_id(hass, "event", UID_ROCKER_A)),
        registry.async_get(entity_id(hass, "event", UID_ROCKER_B)),
    )
    assert a is not None
    assert b is not None
    assert a.device_id == b.device_id
    device = dr.async_get(hass).async_get(a.device_id)
    assert device is not None
    assert device.identifiers == {(DOMAIN, f"{UID_ROCKER_A}-buttons")}
    assert device.name == "Push-button 2-gang 0232 buttons"
    assert (
        hass.states.get(a.entity_id).attributes["friendly_name"]
        == "Push-button 2-gang 0232 buttons Button A"
    )
    assert (
        hass.states.get(b.entity_id).attributes["friendly_name"]
        == "Push-button 2-gang 0232 buttons Button B"
    )


def test_device_info_without_parents(cdb: CDB) -> None:
    """Device info stays valid before the parent devices exist (no `via_device_id`), for a node whose UUID is not
    a MAC (the phone), and for loads without rooms."""
    hub = with_inserts(
        SimpleNamespace(
            cdb=cdb, devices=Devices(), device_ids={}, states={}, node_info=lambda _: {}
        )
    )
    node = cdb.nodes[0]  # the provisioning phone: random UUID, no product
    info = node_device_info(hub, node)
    assert "via_device_id" not in info
    assert "connections" not in info
    assert info["serial_number"] is None
    assert info["model"] == "Product None"

    light = Light(node.elements[0].address, "uid-light", "A light", node, "dimmer")
    assert light_device_info(hub, light) == DeviceInfo(
        identifiers={(DOMAIN, "uid-light")},
        name="A light",
        manufacturer="JUNG",
        model="Dimmable light",
    )
    socket = Socket(node.elements[0].address, "uid-socket", "A socket", node, None)
    assert (
        socket_device_info(hub, socket)
        == DeviceInfo(
            identifiers={(DOMAIN, "uid-socket")},
            name="A socket",
            manufacturer="JUNG",
            model="Product None",  # the product's name: "Socket (metering)" / "Socket" on real nodes
        )
    )
    button = Button(
        node.elements[0].address, "uid-button", "Keys A", node, 0x40, "A", "Keys"
    )
    assert buttons_device_info(hub, [button]) == DeviceInfo(
        identifiers={(DOMAIN, f"{node.uuid.lower()}-0040-buttons")},
        name="Keys",
        manufacturer="JUNG",
        model="Push-buttons",
    )


async def test_vendor_events(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    eid = entity_id(hass, "event", UID_BUTTON_WC)

    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_CLICK))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.state != STATE_UNKNOWN
    assert state.attributes[ATTR_EVENT_TYPE] == "click"
    assert state.attributes["counter"] == 1

    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(2, BUTTON_HOLD_START))
    await hass.async_block_till_done()
    assert hass.states.get(eid).attributes[ATTR_EVENT_TYPE] == "hold_start"
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(3, BUTTON_HOLD_END))
    await hass.async_block_till_done()
    assert hass.states.get(eid).attributes[ATTR_EVENT_TYPE] == "hold_end"
    assert hass.states.get(eid).attributes["counter"] == 3

    fake_link.inject(
        BUTTON_WC, 0xC005, vendor_button_event(4, 0x07)
    )  # a code we do not know: nothing happens
    await hass.async_block_till_done()
    assert hass.states.get(eid).attributes[ATTR_EVENT_TYPE] == "hold_end"
    assert hass.states.get(eid).attributes["counter"] == 3

    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(5, BUTTON_CLICK))
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(6, BUTTON_CLICK))
    await hass.async_block_till_done()
    assert hass.states.get(eid).attributes[ATTR_EVENT_TYPE] == "double_click"
    assert hass.states.get(eid).attributes["counter"] == 6


ROCKER_DOWN_CLICK, ROCKER_UP_CLICK, ROCKER_DOWN_HOLD, ROCKER_UP_HOLD = (
    0x00,
    0x01,
    0x02,
    0x03,
)


async def test_rocker_events_carry_the_side(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A rocker in gateway mode reports its lower / upper half (codes 0-3): the same four event types as a single key,
    plus a `side` attribute; the release takes the side of the hold it ends. A single key's events have no `side`."""
    eid = entity_id(hass, "event", UID_ROCKER_A)
    actions = async_capture_events(hass, EVENT_BUTTON_ACTION)

    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(1, ROCKER_UP_CLICK))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.attributes[ATTR_EVENT_TYPE] == "click"
    assert state.attributes["side"] == "up"
    assert state.attributes["counter"] == 1
    assert actions[-1].data == {
        "device_id": _device_of(hass, eid),
        "entity_id": eid,
        "key": "A",
        "type": "click",
        "counter": 1,
        "side": "up",
    }

    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(2, ROCKER_DOWN_HOLD))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert (state.attributes[ATTR_EVENT_TYPE], state.attributes["side"]) == (
        "hold_start",
        "down",
    )
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(3, BUTTON_HOLD_END))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert (state.attributes[ATTR_EVENT_TYPE], state.attributes["side"]) == (
        "hold_end",
        "down",
    )
    assert actions[-1].data["side"] == "down"

    # no new event types: the rocker's entity declares the same list as every key
    assert state.attributes[ATTR_EVENT_TYPES] == EVENT_TYPES

    # the single-key codes (5 / 6 / 4) carry no side, and a stale one is not carried over
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(4, BUTTON_HOLD_START))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.attributes[ATTR_EVENT_TYPE] == "hold_start"
    assert "side" not in state.attributes
    assert "side" not in actions[-1].data
    fake_link.inject(ROCKER_A, 0xC044, vendor_button_event(5, BUTTON_HOLD_END))
    await hass.async_block_till_done()
    assert "side" not in hass.states.get(eid).attributes


async def test_sig_events(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    eid = entity_id(hass, "event", UID_BUTTON_DIMMER)
    other = entity_id(hass, "event", UID_ROCKER_B)

    fake_link.inject(
        BUTTON_DIMMER, GROUP_DIMMER, M.generic_onoff_set(True, ack=False, tid=1)
    )
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.attributes[ATTR_EVENT_TYPE] == "press_on"
    assert state.attributes["target"] == "C070"
    assert hass.states.get(other).state == STATE_UNKNOWN

    fake_link.inject(
        BUTTON_DIMMER, GROUP_DIMMER, M.generic_onoff_set(False, ack=False, tid=2)
    )
    await hass.async_block_till_done()
    assert hass.states.get(eid).attributes[ATTR_EVENT_TYPE] == "press_off"

    fake_link.inject(ROCKER_B, 0xFFFF, M.scene_recall(2, ack=False, tid=3))
    await hass.async_block_till_done()
    assert hass.states.get(other).attributes[ATTR_EVENT_TYPE] == "scene"
    assert hass.states.get(other).attributes["scene"] == 2

    fake_link.inject(
        BUTTON_DIMMER,
        GROUP_DIMMER,
        encode_opcode(M.GEN_LEVEL_SET_UNACK) + b"\x00\x40\x05",
    )
    await hass.async_block_till_done()
    assert hass.states.get(eid).attributes[ATTR_EVENT_TYPE] == "dim"
    assert hass.states.get(eid).attributes["raw"] == "004005"


def _move(delta: int, tid: int) -> bytes:
    return M.generic_move_set(delta, ack=False, tid=tid, transition=0x01)


def _delta(delta: int, tid: int) -> bytes:
    return M.generic_delta_set(delta, ack=False, tid=tid)


def _later(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)


async def test_holds_are_derived_from_a_keys_move_and_delta_sets(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """A rocker wired to a dimmer: Move / Delta sequences give `hold_start` / `hold_end` with a direction (F12)."""
    hub = init_integration.runtime_data
    got: list[tuple[str, dict[str, Any]]] = []
    hub.add_event_listener(
        BUTTON_DIMMER,
        lambda event, attrs: got.append((event, attrs)) if event != "dim" else None,
    )
    up = {"target": "C070", "direction": "up"}
    down = {"target": "C070", "direction": "down"}

    # Move: a delta starts the hold, its second copy is dropped, 0 ends it; a 0 without a hold is just a dim
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, _move(0x1000, 1))
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, _move(0x1000, 1))
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, _move(0, 2))
    await hass.async_block_till_done()
    assert got == [("hold_start", up), ("hold_end", up)]
    state = hass.states.get(entity_id(hass, "event", UID_BUTTON_DIMMER))
    assert (state.attributes[ATTR_EVENT_TYPE], state.attributes["direction"]) == (
        "hold_end",
        "up",
    )
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, _move(0, 3))
    await hass.async_block_till_done()
    assert got == [("hold_start", up), ("hold_end", up)]

    # Delta: one transaction is one hold; each of its Sets pushes the end back, which comes when the key goes quiet
    got.clear()
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, _delta(-500, 5))
    await hass.async_block_till_done()
    first_end = hub._dim_holds[BUTTON_DIMMER].quiet
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, _delta(-1000, 5))
    await hass.async_block_till_done()
    assert got == [("hold_start", down)]
    assert hub._dim_holds[BUTTON_DIMMER].quiet not in (None, first_end)
    _later(hass, freezer, DIM_HOLD_QUIET)
    await hass.async_block_till_done()
    assert got == [("hold_start", down), ("hold_end", down)]
    assert BUTTON_DIMMER not in hub._dim_holds

    # a Delta 0 ends a Delta hold at once, and its timer with it
    got.clear()
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, _delta(800, 6))
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, _delta(0, 7))
    _later(hass, freezer, DIM_HOLD_QUIET * 4)
    await hass.async_block_till_done()
    assert got == [("hold_start", up), ("hold_end", up)]

    # a start while a hold runs (its stop was lost) ends that hold first; a Level Set or a short Move is no hold
    got.clear()
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, _move(0x1000, 8))
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, _move(-0x1000, 9))
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, _move(-0x1000, 9) + b"\x00")
    fake_link.inject(
        BUTTON_DIMMER, GROUP_DIMMER, encode_opcode(M.GEN_MOVE_SET_UNACK) + b"\x00\x10"
    )
    fake_link.inject(
        BUTTON_DIMMER, GROUP_DIMMER, M.generic_level_set(0, ack=False, tid=10)
    )
    await hass.async_block_till_done()
    assert got == [("hold_start", up), ("hold_end", up), ("hold_start", down)]


async def test_holds_end_with_the_hub(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Stopping the entry ends every running hold with `reason: stopped` (decision M11, review-4 R4-7), and their
    timers with it: nothing fires afterwards."""
    hub = init_integration.runtime_data
    got: list[tuple[int, str, dict[str, Any]]] = []
    for addr in (BUTTON_DIMMER, ROCKER_A, BUTTON_WC):
        hub.add_event_listener(
            addr,
            lambda event, attrs, addr=addr: got.append((addr, event, attrs)),
        )
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, _delta(500, 1))
    fake_link.inject(ROCKER_A, GROUP_DIMMER, _move(0x1000, 1))
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_HOLD_START))
    await hass.async_block_till_done()
    got.clear()
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    up = {"target": "C070", "direction": "up", "reason": "stopped"}
    assert got == [
        (BUTTON_DIMMER, "hold_end", up),
        (ROCKER_A, "hold_end", up),
        (BUTTON_WC, "hold_end", {"reason": "stopped"}),
    ]
    _later(hass, freezer, DIM_HOLD_MAX * 2)
    await hass.async_block_till_done()
    assert len(got) == 3
    assert hub._dim_holds == {}
    assert hub._key_holds == {}


async def test_every_hold_ends_after_the_maximum(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """R4-7: a Move hold whose Move 0 is lost, and a gateway-mode hold whose release is lost, end DIM_HOLD_MAX
    after they started, with `reason: timeout`; the stop that still comes then ends nothing a second time."""
    hub = init_integration.runtime_data
    got: list[tuple[str, dict[str, Any]]] = []
    for addr in (BUTTON_DIMMER, ROCKER_A):
        hub.add_event_listener(
            addr,
            lambda event, attrs: got.append((event, attrs)) if event != "dim" else None,
        )
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, _move(-0x1000, 1))
    fake_link.inject(ROCKER_A, 0xC005, vendor_button_event(1, ROCKER_UP_HOLD))
    await hass.async_block_till_done()
    _later(hass, freezer, DIM_HOLD_MAX - 1)
    await hass.async_block_till_done()
    assert [event for event, _ in got] == ["hold_start", "hold_start"]
    _later(hass, freezer, 1)
    await hass.async_block_till_done()
    assert sorted(got[2:], key=str) == [
        ("hold_end", {"side": "up", "reason": "timeout"}),
        ("hold_end", {"target": "C070", "direction": "down", "reason": "timeout"}),
    ]
    state = hass.states.get(entity_id(hass, "event", UID_BUTTON_DIMMER))
    assert state.attributes["reason"] == "timeout"
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, _move(0, 2))
    fake_link.inject(ROCKER_A, 0xC005, vendor_button_event(2, BUTTON_HOLD_END))
    await hass.async_block_till_done()
    assert len(got) == 4
    # the release cleared the ended hold: the next release without a hold reports as it always did
    fake_link.inject(ROCKER_A, 0xC005, vendor_button_event(3, BUTTON_HOLD_END))
    await hass.async_block_till_done()
    assert got[4:] == [("hold_end", {"counter": 3})]

    # a Delta hold has its quiet end, and the maximum too: a key that never goes quiet (its quiet end pushed past
    # the maximum here) ends at DIM_HOLD_MAX, its quiet timer with it
    got.clear()
    with patch(
        "custom_components.junghome_ble.coordinator.DIM_HOLD_QUIET", DIM_HOLD_MAX * 2
    ):
        fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, _delta(500, 4))
        await hass.async_block_till_done()
    hold = hub._dim_holds[BUTTON_DIMMER]
    assert hold.quiet is not None
    _later(hass, freezer, DIM_HOLD_MAX)
    await hass.async_block_till_done()
    assert got == [
        ("hold_start", {"target": "C070", "direction": "up"}),
        ("hold_end", {"target": "C070", "direction": "up", "reason": "timeout"}),
    ]
    assert (hold.quiet, hold.limit) == (None, None)


async def test_a_new_hold_ends_a_gateway_hold_whose_release_was_lost(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A gateway-mode hold_start while a hold runs ends that hold first (a plain hold_end with its side), as a
    dimming hold does; a hold ended on its maximum is not ended again."""
    hub = init_integration.runtime_data
    got: list[tuple[str, dict[str, Any]]] = []
    hub.add_event_listener(ROCKER_A, lambda event, attrs: got.append((event, attrs)))
    fake_link.inject(ROCKER_A, 0xC005, vendor_button_event(1, ROCKER_DOWN_HOLD))
    fake_link.inject(ROCKER_A, 0xC005, vendor_button_event(2, ROCKER_UP_HOLD))
    await hass.async_block_till_done()
    assert got == [
        ("hold_start", {"counter": 1, "side": "down"}),
        ("hold_end", {"side": "down"}),
        ("hold_start", {"counter": 2, "side": "up"}),
    ]
    hub._key_holds[ROCKER_A].ended = True  # as if DIM_HOLD_MAX had ended it
    fake_link.inject(ROCKER_A, 0xC005, vendor_button_event(3, BUTTON_HOLD_START))
    await hass.async_block_till_done()
    assert got[3:] == [("hold_start", {"counter": 3})]


async def test_a_link_loss_ends_every_hold(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Decision M11: a lost link ends the running holds with `reason: link_lost` — their stop cannot be heard —
    and the release that comes on the next link ends nothing a second time."""
    hub = init_integration.runtime_data
    actions = async_capture_events(hass, EVENT_BUTTON_ACTION)
    fake_link.inject(BUTTON_DIMMER, GROUP_DIMMER, _move(0x1000, 1))
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_HOLD_START))
    await hass.async_block_till_done()
    actions.clear()
    fake_link.drop_link()
    await hass.async_block_till_done()
    assert [(e.data["key"], e.data["type"], e.data["reason"]) for e in actions] == [
        ("A", "hold_end", "link_lost"),
        ("A", "hold_end", "link_lost"),
    ]
    assert [e.data["device_id"] for e in actions] == [
        _device_of(hass, entity_id(hass, "event", UID_BUTTON_DIMMER)),
        _device_of(hass, entity_id(hass, "event", UID_BUTTON_WC)),
    ]
    assert hub._dim_holds == {}
    assert hub._key_holds[BUTTON_WC].ended
    hub._button_event(BUTTON_WC, 2, BUTTON_HOLD_END)
    await hass.async_block_till_done()
    assert len(actions) == 2
    assert BUTTON_WC not in hub._key_holds


async def test_availability_follows_the_link(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    eid = entity_id(hass, "event", UID_BUTTON_WC)
    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert hass.states.get(eid).state == STATE_UNAVAILABLE

    mock_bluetooth_env["infos"] = infos
    mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
    await wait_for_link(hass, init_integration)
    assert hass.states.get(eid).state == STATE_UNKNOWN


async def test_listener_is_removed_with_the_entity(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    hub = init_integration.runtime_data
    assert len(hub._event_listeners[BUTTON_WC]) == 1
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert hub._event_listeners[BUTTON_WC] == []


def _device_of(hass: HomeAssistant, eid: str) -> str:
    entry = er.async_get(hass).async_get(eid)
    assert entry is not None
    assert entry.device_id is not None
    return entry.device_id


async def test_bus_events_carry_the_documented_payload(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Every event an entity fires is re-emitted as junghome_ble_button_action: device, entity, key, type + attrs."""
    actions = async_capture_events(hass, EVENT_BUTTON_ACTION)
    scenes = async_capture_events(hass, EVENT_SCENE_RECALLED)
    wc, dimmer, rocker_b = (
        entity_id(hass, "event", UID_BUTTON_WC),
        entity_id(hass, "event", UID_BUTTON_DIMMER),
        entity_id(hass, "event", UID_ROCKER_B),
    )

    # a gateway-mode key: the vendor event's counter
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_CLICK))
    await hass.async_block_till_done()
    assert actions[-1].data == {
        "device_id": _device_of(hass, wc),
        "entity_id": wc,
        "key": "A",
        "type": "click",
        "counter": 1,
    }

    # a key wired to a load: the address it switched / dimmed
    fake_link.inject(
        BUTTON_DIMMER, GROUP_DIMMER, M.generic_onoff_set(False, ack=False, tid=1)
    )
    await hass.async_block_till_done()
    assert actions[-1].data == {
        "device_id": _device_of(hass, dimmer),
        "entity_id": dimmer,
        "key": "A",
        "type": "press_off",
        "target": "C070",
    }
    fake_link.inject(
        BUTTON_DIMMER,
        GROUP_DIMMER,
        encode_opcode(M.GEN_LEVEL_SET_UNACK) + b"\x00\x40\x05",
    )
    await hass.async_block_till_done()
    assert actions[-1].data == {
        "device_id": _device_of(hass, dimmer),
        "entity_id": dimmer,
        "key": "A",
        "type": "dim",
        "target": "C070",
        "raw": "004005",
    }
    assert scenes == []

    # a key wired to a scene: the button event plus the scene event, named from the export
    fake_link.inject(ROCKER_B, 0xFFFF, M.scene_recall(2, ack=False, tid=3))
    await hass.async_block_till_done()
    assert actions[-1].data == {
        "device_id": _device_of(hass, rocker_b),
        "entity_id": rocker_b,
        "key": "B",
        "type": "scene",
        "scene": 2,
    }
    assert [e.data for e in scenes] == [
        {
            "scene": 2,
            "name": "All off",
            "source": "0235",
            "entry_id": init_integration.entry_id,
            "device_id": _device_of(hass, rocker_b),
            "entity_id": "scene.all_off",
        }
    ]

    # an unknown vendor code fires nothing, on the entity or on the bus
    count = len(actions)
    fake_link.inject(ROCKER_A, 0xC005, vendor_button_event(9, 0x07))
    await hass.async_block_till_done()
    assert len(actions) == count


async def test_scene_recall_of_a_scene_the_export_does_not_know(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A number without a scene entity gets the app's name if the metadata has one, else a generic one."""
    hub = init_integration.runtime_data
    scenes = async_capture_events(hass, EVENT_SCENE_RECALLED)
    fake_link.inject(ROCKER_B, 0xFFFF, M.scene_recall(9, ack=False, tid=4))
    await hass.async_block_till_done()
    assert scenes[-1].data == {
        "scene": 9,
        "name": "Scene 9",
        "source": "0235",
        "entry_id": init_integration.entry_id,
        "device_id": _device_of(hass, entity_id(hass, "event", UID_ROCKER_B)),
    }

    hub.metadata.scenes[9] = "Party"
    assert scene_name(hub, 9) == "Party"
    assert scene_name(hub, 1) == "WC off"

    # the hook for a recall Home Assistant sends itself: no key, hence no device
    fire_scene_recalled(hass, hub, 1, 0x0D00)
    await hass.async_block_till_done()
    assert scenes[-1].data == {
        "scene": 1,
        "name": "WC off",
        "source": "0D00",
        "entry_id": init_integration.entry_id,
        "entity_id": "scene.wc_off",
    }


async def test_the_entity_does_not_publish_itself(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The entity only shows the event: the hub publishes it on the bus (`publish_button_event`), so a key event
    reaches the bus once, not once per listener."""
    hub = init_integration.runtime_data
    actions = async_capture_events(hass, EVENT_BUTTON_ACTION)
    entity = JungHomeButtonEvent(hub, hub.devices.buttons[0])
    entity.hass = hass
    entity.entity_id = "event.orphan"
    with patch.object(entity, "async_write_ha_state") as write:
        entity._on_event("click", {"counter": 1})
    await hass.async_block_till_done()
    write.assert_called_once()
    assert entity.state is not None  # the event reached the entity ...
    assert actions == []  # ... but not the bus


async def test_a_key_event_is_published_once(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """H4-2: with the key's event entity enabled, a key event is one bus event, after the entity took it."""
    eid = entity_id(hass, "event", UID_BUTTON_WC)
    seen: list[str | None] = []

    def _state_when_published(_event: Any) -> None:
        state = hass.states.get(eid)
        seen.append(state.attributes.get("counter") if state else None)

    hass.bus.async_listen(EVENT_BUTTON_ACTION, _state_when_published)
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(7, BUTTON_HOLD_START))
    await hass.async_block_till_done()
    assert seen == [7]


async def test_a_disabled_entity_leaves_the_bus_event_in_place(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """H4-2: a key whose event entity is disabled still publishes its events, without `entity_id`; its scene
    recall still publishes the scene event, with the key as its source and device."""
    registry = er.async_get(hass)
    for uid in (UID_BUTTON_WC, UID_ROCKER_B):
        registry.async_get_or_create(
            "event",
            DOMAIN,
            uid,
            disabled_by=er.RegistryEntryDisabler.USER,
        )
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    actions = async_capture_events(hass, EVENT_BUTTON_ACTION)
    scenes = async_capture_events(hass, EVENT_SCENE_RECALLED)
    assert hass.states.get(entity_id(hass, "event", UID_BUTTON_WC)) is None

    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_HOLD_START))
    fake_link.inject(ROCKER_B, 0xFFFF, M.scene_recall(2, ack=False, tid=3))
    await hass.async_block_till_done()
    wc_device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{UID_BUTTON_WC}-buttons"), mock_config_entry.entry_id
    )
    rocker_device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{UID_ROCKER_A}-buttons"), mock_config_entry.entry_id
    )
    assert wc_device is not None
    assert rocker_device is not None
    assert [e.data for e in actions] == [
        {"device_id": wc_device.id, "key": "A", "type": "hold_start", "counter": 1},
        {"device_id": rocker_device.id, "key": "B", "type": "scene", "scene": 2},
    ]
    assert [(e.data["source"], e.data["device_id"]) for e in scenes] == [
        ("0235", rocker_device.id)
    ]


async def test_no_bus_event_without_the_keys_device(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A key whose buttons device is not in the device registry publishes no button event (a trigger needs the
    device id); its scene recall still publishes the scene event, without a device. An element that is no key, and
    an event type the entities do not declare, publish nothing."""
    hub = init_integration.runtime_data
    actions = async_capture_events(hass, EVENT_BUTTON_ACTION)
    scenes = async_capture_events(hass, EVENT_SCENE_RECALLED)
    devices = dr.async_get(hass)
    device = devices.async_get_device_by_identifier(
        (DOMAIN, f"{UID_ROCKER_A}-buttons"), init_integration.entry_id
    )
    assert device is not None
    devices.async_remove_device(device.id)
    await hass.async_block_till_done()
    publish_button_event(hass, hub, ROCKER_A, "click", {"counter": 1})
    publish_button_event(hass, hub, ROCKER_B, "scene", {"scene": 2})
    publish_button_event(hass, hub, BUTTON_WC, "code_07", {"counter": 1})
    publish_button_event(hass, hub, 0x0300, "click", {"counter": 1})  # a load
    await hass.async_block_till_done()
    assert actions == []
    assert [(e.data["source"], "device_id" in e.data) for e in scenes] == [
        ("0235", False)
    ]
