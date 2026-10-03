"""Config-entity framework: spec -> platform mapping, element resolution, defaults, the reader and the status handler."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.components.switch import (
    DOMAIN as SWITCH_DOMAIN,
)
from homeassistant.components.switch import (
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.junghome_ble import config_entities as C
from custom_components.junghome_ble import const
from custom_components.junghome_ble import keep_awake as keep_awake_mod
from custom_components.junghome_ble.binary_sensor import JungHomeDetectorOccupancy
from custom_components.junghome_ble.const import (
    DETECTOR_WALKING_TEST_DURATION,
    NODE_INFO_VENDOR,
    SIG_SOFTWARE_VERSION,
    SIGNAL_UPDATE,
)
from custom_components.junghome_ble.entity import update_reads
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import properties as P
from custom_components.junghome_ble.jhmesh.cdb import CDB, Element, Node
from custom_components.junghome_ble.jhmesh.devices import Metadata, build_devices
from custom_components.junghome_ble.jhmesh.pdu import encode_opcode
from custom_components.junghome_ble.jhmesh.properties import PropertySpec
from custom_components.junghome_ble.sensor import PROPERTY_INSTALLED

from . import property_helpers as ph
from .conftest import (
    FIXTURES,
    FakeProxyLink,
    settle,
    setup_entry,
    wait_for_link,
    wait_until,
)
from .helpers import (
    LIGHT_CTL,
    LIGHT_DIMMER,
    LIGHT_OUT1,
    LIGHT_OUT2,
    LIGHT_SWITCH,
    NODE_GATEWAY,
    NODE_LIGHT_DIMMER,
    OUR_ADDRESS,
    ROCKER_A,
    SOCKET,
    SOCKET_SENSOR,
    UID_BUTTON_WC,
    UID_LIGHT_CTL,
    UID_LIGHT_DIMMER,
    UID_LIGHT_SWITCH,
    UID_SOCKET,
    entity_id,
    vendor_button_event,
)
from .property_helpers import (
    NODE_0148,
    NODE_0172,
    NODE_0232,
    NODE_0400,
    PID_AUTO_DST,
    PID_DIM_MODE,
    PID_DIM_TO_WARM,
    PID_LED1_OFF,
    PID_LED1_ON,
    PID_LED2_OFF,
    PID_LED2_ON,
    PID_MANUAL_OFF,
    PID_ON_DELAY,
    PID_RUN_ON,
    PID_STATUS_LED,
    PropertyMesh,
    fake_hub,
    vendor_status,
)
from .test_binary_sensor import (
    BUTTON_CLICK,
    DETECTOR_MOTION,
    DETECTOR_PRESENCE,
    GROUP_GATEWAY,
    KEY_1G,
    KEY_2G_A,
    KEY_2G_B,
    TRANSMITTER_1G,
    TRANSMITTER_2G,
    UUID_1G,
    UUID_MOTION,
    UUID_PRESENCE,
    make_detectors_entry,
    start_detectors,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.junghome_ble.coordinator import JungHomeHub


# the fixtures every config-entity test module shares
fast_timeouts, mesh, init_with_mesh = ph.fast_timeouts, ph.mesh, ph.init_with_mesh


def spec(pid: int) -> PropertySpec:
    return P.PROPERTIES[pid]


# --------------------------------------------------------------------------- spec -> platform


def test_mapping_table() -> None:
    """The catalogue's writable app properties map to a platform by codec; the rest are left to other entities."""
    by_platform: dict[str, set[int]] = {}
    for d in C.descriptions():
        by_platform.setdefault(d.platform, set()).add(d.property_id)
    assert by_platform["number"] == {
        0x1001, 0x1002, 0x1007, 0x100D,  # load delays, run-on time, switching repeat time
        0x1101, 0x1102, 0x1103, 0x1106, 0x1107, 0x110A, 0x110B,  # blinds
        0x1203, 0x1204, 0x1205, 0x1224,  # RTR temperatures / offset
        0x6008, 0x6009, 0x600A, 0x600F, 0x6021,  # detector PIR areas, brightness threshold, repetition
    }  # fmt: skip
    assert by_platform["select"] == {
        0x0013, 0x1104, 0x1105, 0x1201, 0x120A, 0x120B, 0x1221, 0x1240, 0x6006, 0x6017,
        0xA001, 0xA002, 0xA004, 0xA005, 0xA007, 0xA008, 0xA00A, 0xA00B, 0xA00D, 0xA00E, 0xA010, 0xA011,  # LEDs 1-6
    }  # fmt: skip
    assert by_platform["switch"] == {
        0x0009,  # lock function, a switch class of its own
        0x000F, 0x0012, 0x100A, 0x100B, 0x100C, 0x100E, 0x1108, 0x120D, 0x1246, 0x1247, 0x6015, 0x5013,
    }  # fmt: skip
    assert by_platform["button"] == {0x110D}
    assert set(by_platform) == {"number", "select", "switch", "button"}
    assert len(C.descriptions()) == 56


@pytest.mark.parametrize(
    "pid",
    [
        0x0001,  # lock flags (bit order unverified)
        0x0002,  # read-only insert id
        0x0007, 0x0008,  # astro registers (struct)
        0x5001, 0x5003,  # re-provisioning / connection flow
        0x5002, 0x5004, 0x5005, 0x5006, 0x5007, 0x5008, 0x5009,  # structs / raw key configuration
        0x5010, 0x5011, 0x5012,  # read-only charts and events
        0x1014, 0x1208, 0x6001, 0x6003,  # unsafe / no UI / paired
        0x6016,  # set on the detector: a read-only sensor (review-3 P3)
        0x000E, 0x1008, 0xA000, 0xA003,  # firmware-only ids
        0xC001,  # gateway credentials
    ],
)  # fmt: skip
def test_not_mapped(pid: int) -> None:
    assert C.describe(spec(pid)) is None


def test_describe_per_codec() -> None:
    d = C.describe(spec(PID_RUN_ON))
    assert d is not None
    assert (d.platform, d.translation_key, d.read, d.write) == (
        "number",
        "timed_on_duration",
        True,
        "set",
    )
    assert C.describe(spec(PID_DIM_MODE)).platform == "select"  # type: ignore[union-attr]
    assert C.describe(spec(PID_AUTO_DST)).platform == "switch"  # type: ignore[union-attr]
    led_on, led_off = C.describe(spec(PID_LED1_ON)), C.describe(spec(PID_LED2_OFF))
    assert led_on is not None
    assert led_off is not None
    assert (led_on.platform, led_on.translation_key) == ("select", "led_colour_on")
    assert (led_off.platform, led_off.translation_key) == ("select", "led_colour_off")
    button = C.describe(spec(0x110D))
    assert button is not None
    assert (button.platform, button.read) == ("button", False)
    status_led = C.describe(spec(PID_STATUS_LED))
    assert status_led is not None
    assert (status_led.platform, status_led.translation_key, status_led.read, status_led.write) == (
        "switch", "status_led", False, "status",
    )  # fmt: skip
    # a write-only app property (none exists today) would be a button too
    trigger = PropertySpec(
        0x7FFF, "trigger", "admin", "wo", P.BOOL, products=P.ALL_PRODUCTS
    )
    assert C.describe(trigger).platform == "button"  # type: ignore[union-attr]


def test_vendor_server() -> None:
    assert C.vendor_server(spec(PID_RUN_ON)) == "admin"
    assert C.vendor_server(spec(PID_STATUS_LED)) == "manufacturer"
    with pytest.raises(ValueError, match="not a vendor property"):
        C.vendor_server(P.SIG_PROPERTIES[0x006A])


# --------------------------------------------------------------------------- element resolution


def targets_of(hub: Any, node_uuid: str, pid: int) -> list[C.PropertyTarget]:
    return [
        t
        for t in C.config_targets(hub)
        if t.node.uuid.lower() == node_uuid and t.spec.id == pid
    ]


def test_fixture_network_counts() -> None:
    targets = C.config_targets(fake_hub())
    assert Counter(t.description.platform for t in targets) == {
        "number": 24,
        "select": 11,
        "switch": 32,  # 26 + a lock on each of the 6 loads
    }
    assert len(targets) == 67
    assert len(C.night_mode_targets(fake_hub())) == 4
    assert sum(t.enabled_default for t in targets) == 32
    assert (
        len({t.unique_id for t in targets}) == 67
    )  # one entity per (device, element, property)
    for platform in ("number", "select", "switch", "button"):
        assert all(
            t.description.platform == platform
            for t in C.config_targets(fake_hub(), platform)
        )


def test_node_properties_go_to_the_primary_element_once() -> None:
    hub = fake_hub()
    # a 2-channel actuator: one automatic-DST entity, on the first output's device, not one per channel
    (t,) = targets_of(hub, NODE_0400, PID_AUTO_DST)
    assert (t.address, t.page, t.enabled_default) == (LIGHT_OUT1, "lamp", True)
    assert t.device_info["name"] == "Kitchen ceiling"
    assert t.unique_id == f"{NODE_0400}-0001-automatic_dst"
    # a socket: the primary element is the socket itself
    (t,) = targets_of(hub, NODE_0172, PID_AUTO_DST)
    assert (t.address, t.page, t.device_info["name"]) == (SOCKET, "socket", "Boiler")


@pytest.mark.parametrize("locations", [[0x0001, 0x0040], [0x0001]])
def test_automatic_dst_only_on_a_node_with_a_load(locations: list[int]) -> None:
    """The app shows the cell on a lamp / socket / blind page: a push-button with an extension or no insert has none."""
    hub = fake_hub()
    node = _synthetic_node(0x01, locations)
    for element in node.elements[1:]:
        element.models = ["1001"]  # a key
    hub.cdb.nodes.append(node)
    hub.devices = build_devices(hub.cdb)
    assert targets_of(hub, node.uuid.lower(), PID_AUTO_DST) == []
    assert [t.address for t in targets_of(hub, NODE_0148, PID_AUTO_DST)] == [
        LIGHT_SWITCH
    ]


def test_load_properties_go_to_every_load_and_respect_the_load_kind() -> None:
    hub = fake_hub()
    assert [t.address for t in targets_of(hub, NODE_0400, PID_ON_DELAY)] == [
        LIGHT_OUT1,
        LIGHT_OUT2,
    ]
    assert [t.unique_id[-13:] for t in targets_of(hub, NODE_0400, PID_ON_DELAY)] == [
        "0001-on_delay",
        "0002-on_delay",
    ]
    # dim mode: dimmer inserts only, not tunable white (the app's `TunableWhiteLampDevice` is not
    # `DimModeCompatible`); warm dimming: DALI only; blinds parameters: no blind load yet
    assert {t.address for t in targets_of(hub, NODE_0148, PID_DIM_MODE)} == set()
    assert {t.address for t in targets_of(hub, NODE_LIGHT_DIMMER, PID_DIM_MODE)} == {
        LIGHT_DIMMER
    }
    assert {t.address for t in targets_of(hub, NODE_0232, PID_DIM_MODE)} == set()
    assert {t.address for t in targets_of(hub, NODE_0232, PID_DIM_TO_WARM)} == {
        LIGHT_CTL
    }
    assert targets_of(hub, NODE_LIGHT_DIMMER, PID_DIM_TO_WARM) == []
    assert not any(t.spec.id in C.BLIND_PROPERTIES for t in C.config_targets(hub))
    # the socket has no lamp-only parameters (prewarning, invert output)
    assert targets_of(hub, NODE_0172, 0x100A) == []


def test_lock_goes_to_every_lockable_load_disabled_by_default() -> None:
    """The lock function (0x0009) is on the app's device page, not its Parameters page: hidden until enabled."""
    targets = C.lock_targets(fake_hub())
    assert [(t.address, t.page, t.device_info["name"]) for t in targets] == [
        (LIGHT_SWITCH, "lamp", "WC mirror"),
        (LIGHT_CTL, "lamp", "Living room DALI"),
        (SOCKET, "socket", "Boiler"),
        (LIGHT_DIMMER, "lamp", "WC ceiling"),
        (LIGHT_OUT1, "lamp", "Kitchen ceiling"),
        (LIGHT_OUT2, "lamp", "2-channel actuator 0400 out 2"),
    ]
    assert {t.translation_key for t in targets} == {"lock"}
    assert not any(t.enabled_default for t in targets)
    assert targets[2].unique_id == f"{NODE_0172}-0001-enforced_output"


def test_blinds_are_lockable_too() -> None:
    cdb = CDB.load(FIXTURES / "Blinds.json")
    hub = ph.with_inserts(
        SimpleNamespace(
            cdb=cdb,
            devices=build_devices(cdb, Metadata.from_export(cdb.export_meta)),
            states={},
            device_ids={},
            entry=SimpleNamespace(title="test", entry_id="entry"),
        )
    )
    blinds = {b.address for b in hub.devices.blinds}
    locks = {t.address: t.page for t in C.lock_targets(hub)}
    assert blinds <= set(locks)
    assert {locks[a] for a in blinds} == {"blind"}


def test_key_properties_go_to_each_key_with_the_letter_when_the_device_groups_keys() -> (
    None
):
    hub = fake_hub()
    (single,) = targets_of(hub, NODE_0148, PID_STATUS_LED)
    assert (single.address, single.key, single.translation_key, single.page) == (
        0x0149, None, "status_led", "control_switch",
    )  # fmt: skip
    assert single.device_info["name"] == "WC mirror button"
    assert single.unique_id == f"{NODE_0148}-0040-key_status_led"
    a, b = targets_of(hub, NODE_0232, PID_STATUS_LED)
    assert (a.address, a.key, a.translation_key) == (ROCKER_A, "A", "status_led_key")
    assert (b.address, b.key, b.translation_key) == (0x0235, "B", "status_led_key")
    assert a.device_info == b.device_info
    # sockets and the 2-channel actuator have no keys with an LED
    assert targets_of(hub, NODE_0172, PID_STATUS_LED) == []
    assert targets_of(hub, NODE_0400, PID_STATUS_LED) == []


def test_led_properties_are_addressed_to_the_primary_element_under_the_key_device() -> (
    None
):
    hub = fake_hub()
    (led1,) = targets_of(hub, NODE_0148, PID_LED1_ON)
    assert (led1.address, led1.key, led1.translation_key) == (
        LIGHT_SWITCH,
        None,
        "led_colour_on",
    )
    assert led1.device_info["name"] == "WC mirror button"
    assert led1.unique_id == f"{NODE_0148}-0001-led1_mode_on"
    (led1,), (led2,) = (
        targets_of(hub, NODE_0232, PID_LED1_OFF),
        targets_of(hub, NODE_0232, PID_LED2_OFF),
    )
    assert (led1.address, led1.key, led1.translation_key) == (
        LIGHT_CTL,
        "A",
        "led_colour_off_key",
    )
    assert (led2.address, led2.key, led2.translation_key) == (
        LIGHT_CTL,
        "B",
        "led_colour_off_key",
    )
    # a socket keeps its LED under the socket device; a 1-gang has no second LED
    (sock,) = targets_of(hub, NODE_0172, PID_LED1_ON)
    assert (sock.address, sock.key, sock.device_info["name"], sock.page) == (
        SOCKET,
        None,
        "Boiler",
        "socket",
    )
    assert targets_of(hub, NODE_0148, PID_LED2_ON) == []
    assert targets_of(hub, NODE_0172, PID_LED2_ON) == []


def test_night_mode_targets() -> None:
    hub = fake_hub()
    by_node = {t.node.uuid.lower(): t for t in C.night_mode_targets(hub)}
    assert set(by_node) == {
        NODE_0148,
        NODE_0232,
        NODE_0172,
        NODE_LIGHT_DIMMER,
    }
    two_gang = by_node[NODE_0232]
    assert two_gang.property_ids == (
        PID_LED1_ON,
        PID_LED1_OFF,
        PID_LED2_ON,
        PID_LED2_OFF,
    )
    assert (two_gang.address, two_gang.key, two_gang.translation_key) == (
        LIGHT_CTL,
        None,
        "led_night_mode",
    )
    assert two_gang.device_info["name"] == "Living room rocker"
    assert [s.id for s in two_gang.specs] == list(two_gang.property_ids)
    assert two_gang.unique_id == f"{NODE_0232}-0001-led_night_mode"
    socket = by_node[NODE_0172]
    assert (socket.property_ids, socket.device_info["name"]) == (
        (PID_LED1_ON, PID_LED1_OFF),
        "Boiler",
    )


def _synthetic_node(pid: int, locations: list[int]) -> Node:
    node = Node(
        "AAAAAAAA-0000-0000-0000-000000000001", "synthetic", 0x0700, b"\0" * 16, pid
    )
    node.elements = [
        Element(0x0700 + i, loc, [], node) for i, loc in enumerate(locations)
    ]
    return node


def test_led_entities_follow_the_key_ordinal_not_the_location() -> None:
    """`LedPosition` counts the node's keys (docs/android/properties.md §LedPosition): a 2-gang whose keys sit at
    0x40 and 0x42 has LED 2 on key C — not on the lamp device with key C left without LEDs (P2-23)."""
    hub = fake_hub()
    node = _synthetic_node(0x02, [0x0001, 0x0040, 0x0042])  # a 2-gang push-button
    for element in node.elements[1:]:
        element.models = [
            "1001"
        ]  # a Generic OnOff Client is what makes a key element a key
    hub.cdb.nodes.append(node)
    hub.devices = build_devices(hub.cdb)
    (led1,) = targets_of(hub, node.uuid.lower(), PID_LED1_ON)
    (led2,) = targets_of(hub, node.uuid.lower(), PID_LED2_ON)
    assert (led1.address, led1.key) == (0x0700, "A")
    assert (led2.address, led2.key) == (0x0700, "C")
    assert led2.device_info == led1.device_info  # the keys' device, not the lamp's
    assert led2.device_info["name"] != "synthetic 0700"
    # a node with fewer keys than LEDs keeps the surplus LED on its primary element (a socket has none at all)
    node.elements[2].models = []
    hub.devices = build_devices(hub.cdb)
    (led2,) = targets_of(hub, node.uuid.lower(), PID_LED2_ON)
    assert (led2.address, led2.key, led2.device_info["name"]) == (
        0x0700,
        None,
        "synthetic 0700",
    )


@pytest.mark.parametrize(
    ("pid", "has_night_mode"), [(0x01, True), (0x05, False), (0x06, False)]
)
def test_no_night_mode_on_battery_wall_transmitters(
    pid: int, has_night_mode: bool
) -> None:
    """The app hides *Night mode* on a battery device (`G(0)` = `!Q1()`), though its LED modes are candidates."""
    hub = fake_hub()
    node = _synthetic_node(pid, [0x0001, 0x0040])
    node.elements[1].models = ["1001"]
    hub.cdb.nodes.append(node)
    hub.devices = build_devices(hub.cdb)
    assert targets_of(hub, node.uuid.lower(), PID_LED1_ON)  # the colours stay
    uuids = {t.node.uuid.lower() for t in C.night_mode_targets(hub)}
    assert (node.uuid.lower() in uuids) is has_night_mode


def test_retired_unique_ids_are_the_gated_out_entities() -> None:
    """What earlier versions created and the app's gates now leave out: the DALI tunable-white dim mode, automatic
    DST on a push-button without a load, night mode on a battery wall transmitter; none of it is still a target."""
    hub = fake_hub()
    no_load = _synthetic_node(0x01, [0x0001, 0x0040])
    no_load.elements[1].models = ["1001"]
    battery = Node(
        "AAAAAAAA-0000-0000-0000-000000000002", "battery", 0x0710, b"\0" * 16, 0x05
    )
    battery.elements = [
        Element(0x0710 + i, loc, [], battery) for i, loc in enumerate([0x0001, 0x0040])
    ]
    battery.elements[1].models = ["1001"]
    hub.cdb.nodes += [no_load, battery]
    hub.devices = build_devices(hub.cdb)
    retired = C.retired_unique_ids(hub)
    assert ("select", f"{NODE_0232}-0001-dim_mode") in retired
    assert ("switch", f"{no_load.uuid.lower()}-0001-automatic_dst") in retired
    assert any(
        p == "switch"
        and uid.startswith(battery.uuid.lower())
        and uid.endswith("-led_night_mode")
        for p, uid in retired
    )
    assert len(retired) == 3
    current = {t.unique_id for t in C.config_targets(hub)} | {
        t.unique_id for t in C.night_mode_targets(hub)
    }
    assert not current & {uid for _, uid in retired}


@pytest.mark.parametrize(
    ("pid", "has_led"), [(0x02, True), (0x05, False), (0x06, False)]
)
def test_status_led_only_on_mains_push_buttons(pid: int, has_led: bool) -> None:
    """Review-3 P1: a battery wall transmitter sleeps between presses; the write is lost, yet the switch showed
    the written value as applied for good."""
    hub = fake_hub()
    node = _synthetic_node(pid, [0x0001, 0x0040])
    node.elements[1].models = ["1001"]
    hub.cdb.nodes.append(node)
    hub.devices = build_devices(hub.cdb)
    assert bool(targets_of(hub, node.uuid.lower(), PID_STATUS_LED)) is has_led


def test_detector_and_aux_elements() -> None:
    hub = fake_hub()
    detector = _synthetic_node(0x07, [0x0001, 0x0040, 0x0041])
    hub.cdb.nodes.append(detector)
    (t,) = targets_of(
        hub, detector.uuid.lower(), 0x600F
    )  # brightness threshold: the highest element
    assert (t.address, t.page, t.enabled_default) == (0x0702, "detector", True)
    assert t.device_info["name"] == "synthetic 0700"
    (t,) = targets_of(hub, detector.uuid.lower(), 0x6006)  # operating mode: expert
    assert (t.address, t.enabled_default) == (0x0702, False)
    # no property lives on the aux element yet; the rule resolves it all the same
    aux = PropertySpec(
        0x7FFE, "aux_thing", "admin", "rw", P.BOOL, "aux", products=P.PUSH_BUTTONS
    )
    node_0148 = hub.cdb.node_by_addr(LIGHT_SWITCH)
    assert node_0148 is not None
    assert [e.address for e in C._elements_for(hub, node_0148, aux)] == [0x014A]
    assert C._elements_for(hub, detector, aux) == []


def test_pages_of_other_device_types() -> None:
    hub = fake_hub()
    rtr = _synthetic_node(0x0A, [0x0001, 0x0040])
    mini_input = _synthetic_node(0x16, [0x0001, 0x0040, 0x0041])
    hub.cdb.nodes += [rtr, mini_input]
    rtr_targets = targets_of(hub, rtr.uuid.lower(), 0x1247) + targets_of(
        hub, rtr.uuid.lower(), 0x1224
    )
    assert [(t.address, t.page, t.enabled_default) for t in rtr_targets] == [
        (0x0700, "rtr", True),
        (0x0700, "rtr", False),
    ]
    assert C._node_page(mini_input) == "mini"
    assert (
        targets_of(hub, mini_input.uuid.lower(), PID_STATUS_LED) == []
    )  # no LED on a binary input


def test_enabled_by_default_is_the_first_parameters_page() -> None:
    hub = fake_hub()
    enabled = {
        node: {
            t.spec.id
            for t in C.config_targets(hub)
            if t.node.uuid.lower() == node and t.enabled_default
        }
        for node in (NODE_0148, NODE_0232, NODE_0172, NODE_0400)
    }
    assert enabled[NODE_0148] == {
        PID_AUTO_DST,
        PID_RUN_ON,
        PID_MANUAL_OFF,
        PID_STATUS_LED,
        PID_LED1_ON,
        PID_LED1_OFF,
    }
    assert enabled[NODE_0232] == {
        PID_AUTO_DST, PID_RUN_ON, PID_MANUAL_OFF, PID_DIM_TO_WARM, PID_STATUS_LED,
        PID_LED1_ON, PID_LED1_OFF, PID_LED2_ON, PID_LED2_OFF,
    }  # fmt: skip
    assert enabled[NODE_0172] == {
        PID_AUTO_DST,
        PID_RUN_ON,
        PID_MANUAL_OFF,
        PID_LED1_ON,
        PID_LED1_OFF,
    }
    assert enabled[NODE_0400] == {PID_AUTO_DST, PID_RUN_ON, PID_MANUAL_OFF}
    disabled = {
        t.spec.id
        for t in C.config_targets(hub)
        if t.node.uuid.lower() == NODE_0232 and not t.enabled_default
    }
    assert disabled == {
        PID_ON_DELAY,
        0x1002,
        0x100D,
        0x100A,
        0x100C,
        C.PROPERTY_LOCK,
    }  # no dim mode: the DALI load is tunable white


def test_firmware_gating_uses_the_cached_software_version() -> None:
    def hub_with_version(raw: bytes | None) -> Any:
        hub = fake_hub()
        if raw is not None:
            hub.states[LIGHT_SWITCH] = SimpleNamespace(
                properties={C.SIG_SOFTWARE_VERSION: raw}
            )
        return hub

    node = fake_hub().cdb.node_by_addr(LIGHT_SWITCH)
    assert node is not None
    assert C.node_version(fake_hub(), node) is None
    assert C.node_version(hub_with_version(b"02020002"), node) == "2.2.0.2"
    assert C.node_version(hub_with_version(b"garbage"), node) is None
    # automatic DST needs 1.1.0.0: gone on an older node, kept when the version is unknown or malformed
    assert targets_of(hub_with_version(b"01000000"), NODE_0148, PID_AUTO_DST) == []
    assert len(targets_of(hub_with_version(b"01010000"), NODE_0148, PID_AUTO_DST)) == 1
    assert len(targets_of(hub_with_version(None), NODE_0148, PID_AUTO_DST)) == 1
    assert len(targets_of(hub_with_version(b"garbage"), NODE_0148, PID_AUTO_DST)) == 1


def test_entity_target_base_is_abstract() -> None:
    hub = fake_hub()
    node = hub.cdb.node_by_addr(LIGHT_SWITCH)
    assert node is not None
    base = C.EntityTarget(
        node=node,
        address=1,
        unique_id="x",
        device_info={"name": "x"},
        page="lamp",  # type: ignore[typeddict-item]
    )
    with pytest.raises(NotImplementedError):
        _ = base.specs
    with pytest.raises(NotImplementedError):
        _ = base.translation_key


async def test_config_entity_base_has_nothing_to_read() -> None:
    """Every config entity says what it reads (`PropertyEntity`: its properties, `SetupStateEntity`: its state)."""
    hub = fake_hub()
    node = hub.cdb.node_by_addr(LIGHT_SWITCH)
    assert node is not None
    target = C.SetupTarget(
        node=node,
        address=LIGHT_SWITCH,
        unique_id="x",
        device_info={"name": "x"},
        page="lamp",
        state=C.ON_POWER_UP,
        entity="power_on_behaviour",
    )
    with pytest.raises(NotImplementedError):
        await C.ConfigEntity(hub, target)._read()


# --------------------------------------------------------------------------- status handler and cache


async def test_vendor_status_is_cached_and_signalled(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    hub: JungHomeHub = init_with_mesh.runtime_data
    signalled: list[None] = []
    async_dispatcher_connect(
        hass,
        SIGNAL_UPDATE.format(init_with_mesh.entry_id, LIGHT_SWITCH),
        lambda: signalled.append(None),
    )
    # an unsolicited publication (any of the three families, any destination) lands in the cache
    fake_link.inject(
        LIGHT_SWITCH,
        0xC061,
        vendor_status(0x05, PID_RUN_ON, (30000).to_bytes(4, "little")),
    )
    fake_link.inject(
        LIGHT_SWITCH,
        OUR_ADDRESS,
        vendor_status(0x0B, 0x0003, bytes.fromhex("0d020100"), access=1),
    )
    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, vendor_status(0x11, 0x5003, b"\x06"))
    fake_link.inject(
        LIGHT_SWITCH, OUR_ADDRESS, encode_opcode(0x05, M.JUNG_CID) + b"\x07\x10"
    )  # too short
    await hass.async_block_till_done()
    props = hub.states[LIGHT_SWITCH].properties
    assert props[PID_RUN_ON] == (30000).to_bytes(4, "little")
    assert props[0x0003] == bytes.fromhex("0d020100")
    assert props[0x5003] == b"\x06"
    assert len(signalled) == 3
    eid = entity_id(hass, "number", f"{UID_LIGHT_SWITCH}-timed_on_duration")
    assert hass.states.get(eid).state == "30.0"


GATEWAY, PHONE = (
    0x00DC,
    0x0001,
)  # the fixture network's gateway node and the provisioning phone
PID_API_TOKEN, PID_GATEWAY_IP, PID_API_STATUS = 0xC001, 0xC002, 0xC000


def test_secret_and_cacheable() -> None:
    """The gateway's API token is the one secret; of its block only what its sensors show is cached, the address
    redacted from the diagnostics; unknown ids are never cached."""
    assert C.is_secret(PID_API_TOKEN)
    assert not C.is_secret(PID_GATEWAY_IP)
    assert not C.is_secret(PID_RUN_ON)
    assert not C.is_secret(0xEEEE)  # not catalogued
    assert not C.cacheable(PID_API_TOKEN)
    assert not C.cacheable(
        0xC003
    )  # the certificate fingerprint: `tls.py` reads it itself
    assert C.cacheable(PID_GATEWAY_IP)
    assert C.cacheable(PID_API_STATUS)
    assert not C.cacheable(0xEEEE)
    assert C.redacted(PID_API_TOKEN)
    assert C.redacted(PID_GATEWAY_IP)
    assert not C.redacted(PID_API_STATUS)
    assert not C.redacted(PID_RUN_ON)
    assert C.cacheable(PID_RUN_ON)
    assert C.cacheable(0x0003)  # every product's secure-element version, read-only
    assert C.cacheable(0x5003)  # key mode: catalogued although no entity exposes it
    assert all(
        C.cacheable(d.property_id) for d in C.descriptions()
    )  # everything an entity reads is cached


async def test_secret_and_unknown_vendor_statuses_are_not_cached(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The gateway answers the phone app's reads of its credentials over the mesh; the proxy forwards them to us. The
    token and the certificate fingerprint never land in the state cache (the diagnostics dump it), nor does a
    property the catalogue does not know; the address and API status its sensors show do."""
    hub: JungHomeHub = init_with_mesh.runtime_data
    signalled: list[None] = []
    async_dispatcher_connect(
        hass,
        SIGNAL_UPDATE.format(init_with_mesh.entry_id, GATEWAY),
        lambda: signalled.append(None),
    )
    before = dict(hub.states[GATEWAY].properties)  # its sensors read at link-up
    fake_link.inject(
        GATEWAY, PHONE, vendor_status(0x0B, PID_API_TOKEN, b"tok", access=1)
    )
    fake_link.inject(GATEWAY, PHONE, vendor_status(0x0B, 0xC003, b"ab" * 32, access=1))
    fake_link.inject(GATEWAY, OUR_ADDRESS, vendor_status(0x05, 0xEEEE, b"\x01\x02"))
    await hass.async_block_till_done()
    assert hub.states[GATEWAY].properties == before
    assert signalled == []
    fake_link.inject(
        GATEWAY, PHONE, vendor_status(0x0B, PID_GATEWAY_IP, b"10.0.0.2", access=1)
    )
    fake_link.inject(
        GATEWAY, PHONE, vendor_status(0x0B, PID_API_STATUS, b"\x03", access=1)
    )
    await hass.async_block_till_done()
    assert hub.states[GATEWAY].properties[PID_GATEWAY_IP] == b"10.0.0.2"
    assert hub.states[GATEWAY].properties[PID_API_STATUS] == b"\x03"
    assert len(signalled) == 2


async def test_gateway_status_and_address(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    mesh: PropertyMesh,
    fast_sleep: list[float],
    fake_link: FakeProxyLink,
) -> None:
    """The gateway's API status flags and its address, read once per link from its node, diagnostic (the waiting
    flag, off by default, enabled here)."""
    mesh.values[GATEWAY, PID_API_STATUS] = (
        b"\x02"  # a client waits for approval, the API is not up
    )
    mesh.values[GATEWAY, PID_GATEWAY_IP] = b"10.0.0.7\x00\x00"
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    available = entity_id(
        hass, "binary_sensor", f"node:{NODE_GATEWAY}-gateway_api_available"
    )
    waiting = entity_id(
        hass,
        "binary_sensor",
        f"node:{NODE_GATEWAY}-gateway_client_waiting_for_approval",
    )
    address = entity_id(hass, "sensor", f"node:{NODE_GATEWAY}-gateway_ip")
    assert hass.states.get(available).state == STATE_OFF
    assert hass.states.get(waiting).state == STATE_ON
    assert hass.states.get(address).state == "10.0.0.7"
    assert mesh.gets.count((GATEWAY, PID_API_STATUS)) == 1  # two sensors, one read
    assert mesh.gets.count((GATEWAY, PID_GATEWAY_IP)) == 1
    registry = er.async_get(hass)
    for eid in (available, waiting, address):
        entry = registry.async_get(eid)
        assert entry is not None
        assert entry.entity_category is EntityCategory.DIAGNOSTIC
        assert entry.disabled_by is None
        device = dr.async_get(hass).async_get(entry.device_id)
        assert device is not None
        assert (C.DOMAIN, f"node:{NODE_GATEWAY}") in device.identifiers

    # the API came up, the client was approved
    fake_link.inject(
        GATEWAY, PHONE, vendor_status(0x0B, PID_API_STATUS, b"\x01", access=1)
    )
    await settle(hass)
    assert hass.states.get(available).state == STATE_ON
    assert hass.states.get(waiting).state == STATE_OFF
    # a Status without a value is dropped, as the app's resolver drops it: the last flags and address stay
    for pid in (PID_API_STATUS, PID_GATEWAY_IP):
        fake_link.inject(GATEWAY, PHONE, vendor_status(0x0B, pid, b"", access=1))
    await settle(hass)
    assert hass.states.get(available).state == STATE_ON
    assert hass.states.get(address).state == "10.0.0.7"


async def test_initial_reads_are_chunked_delayed_and_deduplicated(
    hass: HomeAssistant,
    init_with_mesh: MockConfigEntry,
    mesh: PropertyMesh,
    fast_sleep: list[float],
) -> None:
    """After the link is up the enabled entities read their properties: a start delay, then five elements at a time."""
    assert 3.0 in fast_sleep  # PROPERTY_READ_DELAY
    assert (
        fast_sleep.count(0.5) >= 2
    )  # PROPERTY_READ_PAUSE between chunks (and the hub's own refresh)
    # the first chunk goes to five distinct elements (which five depends on the order the platforms queued their
    # reads in, which HA does not fix); the nodes' LBC version blocks are asked once each, with the node's version
    identity = [(addr, pid) for addr, pid in mesh.gets if pid in NODE_INFO_VENDOR]
    assert Counter(identity)[LIGHT_SWITCH, 0x0003] == 1
    assert not any(pid == 0x0005 for _, pid in identity)  # an RTR's only
    mesh.gets = [get for get in mesh.gets if get not in identity]
    first = {addr for addr, _ in mesh.gets[:5]}
    assert len(first) == 5
    assert first <= {
        LIGHT_SWITCH,
        LIGHT_CTL,
        SOCKET,
        SOCKET_SENSOR,
        LIGHT_DIMMER,
        LIGHT_OUT1,
        LIGHT_OUT2,
        GATEWAY,  # its API status and IP address
    }
    # every enabled, readable property was read exactly once: the LED colours are shared by the colour selects
    # and the night-mode switch, and the status LED is never read
    counts = Counter(mesh.gets)
    assert set(counts.values()) == {1}
    assert (LIGHT_SWITCH, PID_LED1_ON) in counts
    assert not any(pid == PID_STATUS_LED for _, pid in counts)
    assert (
        SOCKET_SENSOR,
        PROPERTY_INSTALLED,
    ) in counts  # the socket's *Installed* sensor reads through the same queue
    assert (GATEWAY, C.GATEWAY_API_STATUS) in counts  # two sensors, one read
    assert (LIGHT_SWITCH, C.PROPERTY_DEVICE_LOCK) in counts  # *Lock operation*
    # enabled targets minus the four status LEDs, plus *Installed*, the gateway's status and address, and the
    # device-lock word of the five device nodes (*Lock operation*)
    assert len(counts) == 32 - 4 + 1 + 2 + 5
    hub: JungHomeHub = init_with_mesh.runtime_data
    assert hub.states[LIGHT_SWITCH].properties[PID_RUN_ON] == b"\0\0\0\0"


async def test_unanswered_read_stays_unknown_and_is_repeated_on_the_next_link(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    mesh: PropertyMesh,
    fast_sleep: list[float],
    fast_timeouts: None,
) -> None:
    """An element that does not answer its Get leaves the entity unknown and the read open: not asked again on
    this link (an update of the element does not re-queue it), asked again on the next link, where a device that
    was asleep or out of range usually answers."""
    mesh.silent.add((LIGHT_SWITCH, PID_RUN_ON))
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    eid = entity_id(hass, "number", f"{UID_LIGHT_SWITCH}-timed_on_duration")
    number = hass.data[DATA_INSTANCES]["number"].get_entity(eid)
    assert isinstance(number, C.PropertyEntity)
    await wait_until(
        hass, lambda: mesh.gets.count((LIGHT_SWITCH, PID_RUN_ON)) == 3
    )  # all three attempts (the app's)
    await wait_until(hass, lambda: not number._read_pending)  # ... timed out
    assert not number._read_done  # the value is still unknown: the read stays open
    await wait_until(
        hass, lambda: (LIGHT_SWITCH, PID_MANUAL_OFF) in mesh.gets
    )  # the queue moved on
    assert hass.states.get(eid).state == STATE_UNKNOWN
    manual = entity_id(hass, "switch", f"{UID_LIGHT_SWITCH}-manual_off_enable")
    assert (
        hass.states.get(manual).state == "off"
    )  # the others on the same element were read fine

    # an update of the element on the same link does not ask again
    async_dispatcher_send(
        hass, SIGNAL_UPDATE.format(mock_config_entry.entry_id, LIGHT_SWITCH)
    )
    await settle(hass, 50)
    assert mesh.gets.count((LIGHT_SWITCH, PID_RUN_ON)) == 3

    # the next link does; this time the device answers and the entity gets its value
    mesh.silent.discard((LIGHT_SWITCH, PID_RUN_ON))
    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    mesh.link.drop_link()
    await settle(hass)
    mock_bluetooth_env["infos"] = infos
    mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    assert mock_config_entry.runtime_data.connected
    await wait_until(hass, lambda: number._read_done)
    assert mesh.gets.count((LIGHT_SWITCH, PID_RUN_ON)) == 4
    assert hass.states.get(eid).state != STATE_UNKNOWN


async def test_read_cut_by_a_lost_link_is_retried_on_the_next_link(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """A read that could not even be sent (link gone) is not counted as done: it runs again once reconnected."""
    original = fake_link.write_gatt_char
    failed: list[tuple[int, int, bytes]] = []

    async def vendor_writes_fail(
        char: str, data: bytes, response: bool | None = None
    ) -> None:
        await original(char, data, response)
        if fake_link.sent and fake_link.sent[-1][2][0] & 0xC0 == 0xC0:
            failed.append(fake_link.sent[-1])
            raise ConnectionError("proxy gone")

    fake_link.write_gatt_char = vendor_writes_fail  # type: ignore[method-assign]
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    hub: JungHomeHub = mock_config_entry.runtime_data
    assert (LIGHT_SWITCH, PID_RUN_ON) in {
        (dst, int.from_bytes(a[3:5], "little")) for _, dst, a in failed
    }
    assert LIGHT_SWITCH not in hub.states  # nothing was heard from the element

    fake_link.write_gatt_char = original  # type: ignore[method-assign]
    mesh = PropertyMesh(fake_link)
    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    mock_bluetooth_env["infos"] = infos
    mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    assert mock_config_entry.runtime_data.connected
    assert (LIGHT_SWITCH, PID_RUN_ON) in mesh.gets
    assert hub.states[LIGHT_SWITCH].properties[PID_RUN_ON] == b"\0\0\0\0"


async def test_reads_wait_for_a_link_instead_of_failing_without_one(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """Review-4 R4-4: with no proxy in range the reader worked its whole queue into "not connected" (each Get taking
    a sequence number before failing). It waits for the link instead and reads once there is one."""
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    hub: JungHomeHub = mock_config_entry.runtime_data
    reader = hass.data[C.READERS][mock_config_entry.entry_id]
    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert not hub.connected
    ran: list[bool] = []

    async def job() -> None:
        ran.append(hub.connected)

    reader.schedule(LIGHT_SWITCH, job)
    await settle(hass)
    assert ran == []  # held while there is no link
    # queued for the next link, whichever it is (`link` None), with the reads the lost link queued again
    assert reader._queued[LIGHT_SWITCH, job].link is None

    PropertyMesh(fake_link)  # the queued reads are answered
    mock_bluetooth_env["infos"] = infos
    mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    assert ran == [True]


def _queue_reader() -> tuple[C.PropertyReader, SimpleNamespace]:
    """A reader over a hub stand-in whose worker never starts: the queue alone."""

    def start(_hass: object, coro: Any, _name: str) -> SimpleNamespace:
        coro.close()
        return SimpleNamespace(done=lambda: False)

    hub = SimpleNamespace(
        link_count=1,
        connected=True,
        hass=None,
        entry=SimpleNamespace(async_create_background_task=start),
    )
    return C.PropertyReader(hub), hub  # type: ignore[arg-type]


async def test_a_job_is_queued_once_and_dropped_with_its_link() -> None:
    """Review-4 R4-5: a job still waiting is not queued again but counted for the current link; one of a link that
    went away is dropped when its turn comes; one queued while no link was up waits for any. A chunk takes one job
    per element, at most PROPERTY_READ_CHUNK, the rest keeps its order."""
    reader, hub = _queue_reader()

    async def job() -> None:
        pass

    async def other() -> None:
        pass

    reader.schedule(LIGHT_SWITCH, job)
    reader.schedule(LIGHT_SWITCH, job)  # still waiting: kept once
    reader.schedule(LIGHT_SWITCH, other)
    reader.schedule(SOCKET, job)  # another element: a job of its own
    assert [(q.addr, q.job) for q in reader._jobs] == [
        (LIGHT_SWITCH, job),
        (LIGHT_SWITCH, other),
        (SOCKET, job),
    ]
    hub.link_count = 2  # a new link: only what its entities queued again is read
    reader.schedule(LIGHT_SWITCH, other)
    assert reader._take_chunk() == [other]
    assert not reader._jobs
    assert not reader._queued

    hub.connected = False
    reader.schedule(LIGHT_SWITCH, job)  # no link: for the next one
    hub.connected, hub.link_count = True, 3
    reader.schedule(LIGHT_SWITCH, other)
    reader.schedule(SOCKET, other)
    assert reader._take_chunk() == [job, other]
    assert reader._take_chunk() == [other]

    addrs = range(0x0100, 0x0100 + C.PROPERTY_READ_CHUNK + 1)
    for addr in addrs:
        reader.schedule(addr, job)
    assert len(reader._take_chunk()) == C.PROPERTY_READ_CHUNK
    assert [q.addr for q in reader._jobs] == [addrs[-1]]


async def test_quick_drops_leave_one_version_job_per_node(
    hass: HomeAssistant,
    fast_timeouts: None,
    init_with_mesh: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Review-4 R4-5, the reviewer's repro: five quick drops while the queue waits held five version reads per node,
    each read in turn. A job is queued once, for the link that is up."""
    from homeassistant.config_entries import ConfigEntryState  # noqa: PLC0415

    hub: JungHomeHub = init_with_mesh.runtime_data
    reader = C.property_reader(hass, hub)
    entry = hub.entry
    reader._version_read.clear()  # as if no node had answered yet: each link asks again
    # the worker that starts on the next link waits for the entry to load: the queue stays as the links leave it
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    try:
        for _ in range(5):
            fake_link.drop_link()
            await wait_for_link(hass, init_with_mesh, connected=False)
            await wait_for_link(hass, init_with_mesh)
            await settle(hass)
        assert fake_link.connect_count == 6
        versions = Counter(q.addr for q in reader._jobs if q.key == "version")
        assert versions
        assert set(versions.values()) == {1}
        assert max(Counter((q.addr, q.key) for q in reader._jobs).values()) == 1
        assert {q.link for q in reader._jobs} == {hub.link_count}
    finally:
        entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    await settle(hass)


async def test_battery_node_is_read_when_a_key_wakes_it_not_at_link_up(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    fast_timeouts: None,
) -> None:
    """A wall transmitter sleeps at link-up: its LED entities ask nothing then (nor its version); a key event reads
    them at once, one that stays unanswered is read again at the next event, one that answered never again."""
    mesh = PropertyMesh(fake_link)
    entry = await start_detectors(hass, make_detectors_entry(), fake_link)
    await settle(hass, 200)
    battery = {TRANSMITTER_1G, KEY_1G, TRANSMITTER_2G, KEY_2G_A, KEY_2G_B}
    assert not any(addr in battery for addr, _ in mesh.gets)
    version = M.generic_property_get("manufacturer", SIG_SOFTWARE_VERSION)
    assert not any(dst in battery and pdu == version for _, dst, pdu in fake_link.sent)
    eid = next(
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id.startswith(UUID_1G.lower())
        and e.unique_id.endswith("-led1_mode_on")
    )
    assert hass.states.get(eid).state == STATE_UNKNOWN

    def reading() -> bool:
        return any(
            e._read_pending
            for domain in ("select", "switch")
            for e in hass.data[DATA_INSTANCES][domain].entities
            if isinstance(e, C.ConfigEntity) and e.target.node.unicast == TRANSMITTER_1G
        )

    async def key_event(counter: int) -> None:
        fake_link.inject(
            KEY_1G, GROUP_GATEWAY, vendor_button_event(counter, BUTTON_CLICK)
        )
        await hass.async_block_till_done()
        await wait_until(hass, lambda: not reading())

    mesh.silent.add((TRANSMITTER_1G, PID_LED1_ON))  # asleep again before it answered
    await key_event(1)
    assert (TRANSMITTER_1G, PID_LED1_ON) in mesh.gets
    assert (TRANSMITTER_1G, PID_LED1_OFF) in mesh.gets  # its other LED entities too
    assert hass.states.get(eid).state == STATE_UNKNOWN
    mesh.silent.clear()
    await key_event(2)
    assert hass.states.get(eid).state == "no_color"
    gets = len(mesh.gets)
    await key_event(3)
    assert mesh.gets[gets:] == []  # all read: nothing to ask


async def test_a_battery_node_change_is_held_awake_and_a_silent_one_is_asleep(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    fast_timeouts: None,
) -> None:
    """Review-3 W4 / F24: a wall transmitter's LED colour change keeps the node awake (`KeepAwake.hold`) from the
    read of its night-mode byte to the Set; asleep — neither the read nor, later, the Set and its read-back
    answered — the change fails asking for a key press, not as an unknown value or a device out of range."""
    mesh = PropertyMesh(fake_link)
    entry = await start_detectors(hass, make_detectors_entry(), fake_link)
    hub: JungHomeHub = entry.runtime_data
    eid = next(
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id.startswith(UUID_1G.lower())
        and e.unique_id.endswith("-led1_mode_on")
    )
    holds: list[list[int]] = []
    hold = hub.keep_awake.hold

    def spy(addresses: Any) -> Any:
        holds.append(list(addresses))
        return hold(holds[-1])

    async def select(option: str) -> None:
        await hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: eid, "option": option},
            blocking=True,
        )

    with patch.object(hub.keep_awake, "hold", spy):
        mesh.silent.add((TRANSMITTER_1G, PID_LED1_ON))
        with pytest.raises(HomeAssistantError) as exc:
            await select("red")  # the night-mode byte is read first: nothing answers
        assert exc.value.translation_key == "setting_asleep"
        assert exc.value.translation_placeholders == {"entity": eid}
        assert (TRANSMITTER_1G, PID_LED1_ON) in mesh.gets
        assert mesh.sets == []
        assert holds == [[TRANSMITTER_1G]]
        mesh.silent.clear()
        await select("red")  # awake: read, then written
        assert mesh.sets == [(TRANSMITTER_1G, PID_LED1_ON, b"\x64\x00\x00\x00")]
        assert holds[1:] == [
            [TRANSMITTER_1G],
            [TRANSMITTER_1G],
        ]  # the change, and its write inside it
        mesh.silent.add((TRANSMITTER_1G, PID_LED1_ON))
        with pytest.raises(HomeAssistantError) as exc:
            await select("green")  # known now: the Set and its read-back go unanswered
        assert exc.value.translation_key == "setting_asleep"
        assert len(mesh.sets) == 2
    assert hub.keep_awake._tasks == {}
    assert hass.states.get(eid).state == "red"


PID_BUTTON_LAYOUT = 0x5001  # the keep-alive's property (`keep_awake.py`)
LAYOUT_STATUS = vendor_status(0x05, PID_BUTTON_LAYOUT, b"\x03\x00")


async def _transmitter_led(
    hass: HomeAssistant, fake_link: FakeProxyLink
) -> tuple[JungHomeHub, str, Callable[[str], Awaitable[None]]]:
    """Start the detectors network; return its hub, the 1-gang wall transmitter's LED 1 colour and a selector."""
    entry = await start_detectors(hass, make_detectors_entry(), fake_link)
    eid = next(
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id.startswith(UUID_1G.lower())
        and e.unique_id.endswith("-led1_mode_on")
    )

    async def select(option: str) -> None:
        await hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: eid, "option": option},
            blocking=True,
        )

    return entry.runtime_data, eid, select


async def _spin_until(predicate: Callable[[], bool]) -> None:
    """`wait_until` without its `async_block_till_done`, which would wait for the change the test holds open."""
    for _ in range(1000):
        if predicate():
            return
        await ph.real_wait(0.01)
    raise AssertionError("condition not met in time")


async def test_a_battery_node_change_sends_the_keep_alive(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review-3 W4 / F24: while a wall transmitter's LED colour change waits for the node, its ButtonLayout is asked
    for (the app's keep-alive, on a short interval here), its Status taken by neither side's other request; none
    once the change is done."""
    mesh = PropertyMesh(fake_link)
    hub, _, select = await _transmitter_led(hass, fake_link)
    monkeypatch.setattr(keep_awake_mod, "KEEP_AWAKE_INTERVAL", 0.05)
    mesh.silent.add((TRANSMITTER_1G, PID_LED1_ON))  # asleep at the first Get ...
    with patch.object(const, "PROPERTY_READ_TIMEOUT", 1.0):
        change = asyncio.ensure_future(select("red"))
        await _spin_until(lambda: (TRANSMITTER_1G, PID_BUTTON_LAYOUT) in mesh.gets)
        mesh.silent.clear()  # ... awake at the second
        await change
    assert mesh.sets == [(TRANSMITTER_1G, PID_LED1_ON, b"\x64\x00\x00\x00")]
    keep_alives = [get for get in mesh.gets if get[1] == PID_BUTTON_LAYOUT]
    assert set(keep_alives) == {(TRANSMITTER_1G, PID_BUTTON_LAYOUT)}
    assert hub.keep_awake._tasks == {}
    # a keep-alive loop left running would be due at once now, and send under `settle`
    monkeypatch.setattr(keep_awake_mod, "KEEP_AWAKE_INTERVAL", 0.0)
    await settle(hass)
    assert [get for get in mesh.gets if get[1] == PID_BUTTON_LAYOUT] == keep_alives


async def test_a_keep_alive_status_does_not_answer_a_changes_get(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """The keep-alive's ButtonLayout Status from the node, arriving while the change's own Get of the LED byte waits,
    is not taken for its answer: the Get's next attempt is, and the change goes through."""
    mesh = PropertyMesh(fake_link)
    hub, _, select = await _transmitter_led(hass, fake_link)
    mesh.silent.add((TRANSMITTER_1G, PID_LED1_ON))
    with patch.object(const, "PROPERTY_READ_TIMEOUT", 0.5):
        change = asyncio.ensure_future(select("red"))
        await _spin_until(lambda: (TRANSMITTER_1G, PID_LED1_ON) in mesh.gets)
        fake_link.inject(TRANSMITTER_1G, OUR_ADDRESS, LAYOUT_STATUS)
        mesh.silent.clear()  # awake: the second attempt is answered
        await change
    assert mesh.gets.count((TRANSMITTER_1G, PID_LED1_ON)) == 2
    assert mesh.sets == [(TRANSMITTER_1G, PID_LED1_ON, b"\x64\x00\x00\x00")]
    assert hub.keep_awake._tasks == {}


async def test_a_keep_alive_status_does_not_answer_a_changes_set(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """The same for the Set: a ButtonLayout Status before the Set's own is not its confirmation (which would send
    a read-back the sleeping node leaves unanswered); the Set's Status is."""
    mesh = PropertyMesh(fake_link)
    _, eid, select = await _transmitter_led(hass, fake_link)
    await select("red")  # the byte is known now: the next change only sets it
    gets = list(mesh.gets)
    mesh.silent.add((TRANSMITTER_1G, PID_LED1_ON))
    with patch.object(const, "PROPERTY_WRITE_TIMEOUT", 2.0):
        change = asyncio.ensure_future(select("green"))
        await _spin_until(lambda: len(mesh.sets) == 2)
        fake_link.inject(TRANSMITTER_1G, OUR_ADDRESS, LAYOUT_STATUS)
        fake_link.inject(
            TRANSMITTER_1G,
            OUR_ADDRESS,
            vendor_status(0x05, PID_LED1_ON, mesh.sets[-1][2]),
        )
        await change
    assert mesh.gets == gets  # no read-back
    assert hass.states.get(eid).state == "green"


async def test_a_lost_link_during_a_battery_node_change_is_not_asleep(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """The link goes while the change reads the LED byte: the error is the send failure, not *asleep* — pressing a
    key of the transmitter would not help — and the node is not held any longer."""
    hub, _, select = await _transmitter_led(hass, fake_link)
    with (
        patch.object(
            hub.proxy,
            "request",
            AsyncMock(side_effect=ConnectionError("not connected to a proxy")),
        ),
        pytest.raises(HomeAssistantError) as exc,
    ):
        await select("red")
    assert exc.value.translation_key == "send_failed"
    assert hub.keep_awake._tasks == {}


async def test_reader_is_per_entry_and_dropped_on_unload(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry
) -> None:
    hub: JungHomeHub = init_with_mesh.runtime_data
    reader = C.property_reader(hass, hub)
    assert C.property_reader(hass, hub) is reader
    assert hass.data[C.READERS] == {init_with_mesh.entry_id: reader}
    assert await hass.config_entries.async_unload(init_with_mesh.entry_id)
    await hass.async_block_till_done()
    assert hass.data[C.READERS] == {}


async def test_reads_wait_for_the_entry_to_load_and_stop_if_it_never_does(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry
) -> None:
    """The worker holds its first chunk until every platform has queued its jobs; an entry that fails reads nothing."""
    from homeassistant.config_entries import ConfigEntryState  # noqa: PLC0415

    hub: JungHomeHub = init_with_mesh.runtime_data
    reader = C.property_reader(hass, hub)
    ran: list[int] = []

    async def job() -> None:
        ran.append(1)

    # a loaded entry: the job runs (sleeps are instant under init_with_mesh's fast_sleep)
    reader.schedule(LIGHT_SWITCH, job)
    # ... and the worker is through: one still draining its queue would take the next job without waiting
    await wait_until(
        hass,
        lambda: ran == [1] and reader._worker is not None and reader._worker.done(),
    )
    # still setting up: the worker waits; a failed setup ends it without reading
    entry = hub.entry
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    try:
        reader.schedule(LIGHT_SWITCH, job)
        await settle(hass)
        assert ran == [1]  # held back
        entry._async_set_state(hass, ConfigEntryState.SETUP_ERROR, None)
        await settle(hass)
        assert ran == [1]  # never read: the entry did not load
        assert reader._worker is not None
        assert reader._worker.done()
    finally:
        entry._async_set_state(hass, ConfigEntryState.LOADED, None)


async def test_write_confirmed_by_status_needs_no_re_read(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry, mesh: PropertyMesh
) -> None:
    hub: JungHomeHub = init_with_mesh.runtime_data
    reader = C.property_reader(hass, hub)
    gets = len(mesh.gets)
    assert await reader.write(LIGHT_SWITCH, spec(PID_RUN_ON), 90) == "applied"
    assert mesh.sets[-1] == (LIGHT_SWITCH, PID_RUN_ON, (90000).to_bytes(4, "little"))
    assert hub.states[LIGHT_SWITCH].properties[PID_RUN_ON] == (90000).to_bytes(
        4, "little"
    )
    assert len(mesh.gets) == gets  # the Status answering the Set was enough


async def test_write_without_status_is_read_back(
    hass: HomeAssistant,
    init_with_mesh: MockConfigEntry,
    mesh: PropertyMesh,
    fast_sleep: list[float],
    fast_timeouts: None,
) -> None:
    hub: JungHomeHub = init_with_mesh.runtime_data
    reader = C.property_reader(hass, hub)
    mesh.confirm_sets = False
    mesh.values[LIGHT_SWITCH, PID_RUN_ON] = (5000).to_bytes(
        4, "little"
    )  # what the device will report
    del fast_sleep[:]
    assert await reader.write(LIGHT_SWITCH, spec(PID_RUN_ON), 5) == "applied"
    assert mesh.sets[-1] == (LIGHT_SWITCH, PID_RUN_ON, (5000).to_bytes(4, "little"))
    assert 0.5 in fast_sleep  # PROPERTY_REREAD_DELAY
    assert mesh.gets[-1] == (LIGHT_SWITCH, PID_RUN_ON)
    assert hub.states[LIGHT_SWITCH].properties[PID_RUN_ON] == (5000).to_bytes(
        4, "little"
    )


async def test_write_answered_with_another_property_is_read_back(
    hass: HomeAssistant,
    init_with_mesh: MockConfigEntry,
    mesh: PropertyMesh,
    fake_link: FakeProxyLink,
    fast_timeouts: None,
) -> None:
    """A Status of a different property from the same element is not the Set's confirmation."""
    hub: JungHomeHub = init_with_mesh.runtime_data
    reader = C.property_reader(hass, hub)
    mesh.confirm_sets = False
    original = fake_link.write_gatt_char

    async def write_and_publish(
        char: str, data: bytes, response: bool | None = None
    ) -> None:
        await original(char, data, response)
        if mesh.sets:
            fake_link.inject(
                LIGHT_SWITCH, 0xC061, vendor_status(0x05, PID_AUTO_DST, b"\x01")
            )

    fake_link.write_gatt_char = write_and_publish  # type: ignore[method-assign]
    mesh.values[LIGHT_SWITCH, PID_RUN_ON] = (7000).to_bytes(4, "little")
    assert await reader.write(LIGHT_SWITCH, spec(PID_RUN_ON), 7) == "applied"
    assert mesh.gets[-1] == (LIGHT_SWITCH, PID_RUN_ON)


async def test_write_neither_answered_nor_shown_is_not_a_success(
    hass: HomeAssistant,
    init_with_mesh: MockConfigEntry,
    mesh: PropertyMesh,
    fast_timeouts: None,
) -> None:
    """No Status and no read-back: `no_answer`; a read-back with another value: `not_applied` (the cache shows it)."""
    hub: JungHomeHub = init_with_mesh.runtime_data
    reader = C.property_reader(hass, hub)
    mesh.silent.add((LIGHT_SWITCH, PID_RUN_ON))
    assert await reader.write(LIGHT_SWITCH, spec(PID_RUN_ON), 7) == "no_answer"
    mesh.silent.clear()
    mesh.confirm_sets = False  # the device reports what it held before: 0
    assert await reader.write(LIGHT_SWITCH, spec(PID_RUN_ON), 7) == "not_applied"
    assert hub.states[LIGHT_SWITCH].properties[PID_RUN_ON] == bytes(4)


def test_applied_compares_the_bytes_and_a_lock_by_its_state() -> None:
    run_on, lock = spec(PID_RUN_ON), spec(C.PROPERTY_LOCK)
    assert C.applied(run_on, b"\x01\x00\x00\x00", b"\x01\x00\x00\x00")
    assert not C.applied(run_on, b"\x01\x00\x00\x00", b"\x00\x00\x00\x00")
    assert not C.applied(run_on, b"\x01\x00\x00\x00", None)
    # a timed lock of the current state: the element reports what it keeps (the time left, the state it holds)
    sent = lock.codec.encode(P.lock_output(600))
    assert C.applied(lock, sent, bytes([2, 1]) + (540).to_bytes(2, "little") + b"\x01")
    assert not C.applied(lock, sent, lock.codec.encode(P.UNLOCK))
    assert not C.applied(lock, sent, b"\x02")  # too short to be a lock state


def test_check_outcome_raises_for_what_the_element_did_not_confirm() -> None:
    C.check_outcome("applied", "switch.x")
    C.check_outcome(
        "not_applied", "button.x", compare=False
    )  # a trigger reads back what it does
    for outcome in ("no_answer", "not_applied"):
        with pytest.raises(HomeAssistantError) as exc:
            C.check_outcome(outcome, "switch.x")
        assert exc.value.translation_key == f"setting_{outcome}"
        assert exc.value.translation_placeholders == {"entity": "switch.x"}


async def test_get_answered_with_another_property_is_not_marked_fresh(
    hass: HomeAssistant,
    init_with_mesh: MockConfigEntry,
    mesh: PropertyMesh,
    fake_link: FakeProxyLink,
    fast_timeouts: None,
) -> None:
    hub: JungHomeHub = init_with_mesh.runtime_data
    reader = C.property_reader(hass, hub)
    mesh.silent.add((LIGHT_SWITCH, PID_ON_DELAY))
    original = fake_link.write_gatt_char

    async def write_and_publish(
        char: str, data: bytes, response: bool | None = None
    ) -> None:
        await original(char, data, response)
        fake_link.inject(
            LIGHT_SWITCH, 0xC061, vendor_status(0x05, PID_AUTO_DST, b"\x01")
        )

    fake_link.write_gatt_char = write_and_publish  # type: ignore[method-assign]
    assert not await reader.read(LIGHT_SWITCH, spec(PID_ON_DELAY))  # nothing cached
    assert PID_ON_DELAY not in hub.states[LIGHT_SWITCH].properties
    assert (LIGHT_SWITCH, PID_ON_DELAY) not in reader._read_at
    # a property answered a moment ago is not asked again while fresh
    gets = mesh.gets.count((LIGHT_SWITCH, PID_RUN_ON))
    assert await reader.read(LIGHT_SWITCH, spec(PID_RUN_ON))
    assert mesh.gets.count((LIGHT_SWITCH, PID_RUN_ON)) == gets


async def test_write_status_needs_no_reply(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    hub: JungHomeHub = init_with_mesh.runtime_data
    reader = C.property_reader(hass, hub)
    fake_link.sent.clear()
    await reader.write_status(0x0149, spec(PID_STATUS_LED), True)
    assert fake_link.sent == [
        (
            OUR_ADDRESS,
            0x0149,
            bytes.fromhex("d12705") + bytes.fromhex("135003") + b"\x01",
        )
    ]
    assert hub.states[0x0149].properties[PID_STATUS_LED] == b"\x01"


async def test_entities_are_config_category_with_property_attributes(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    eid = entity_id(hass, "number", f"{UID_LIGHT_SWITCH}-timed_on_duration")
    entry = registry.async_get(eid)
    assert entry is not None
    assert entry.entity_category is EntityCategory.CONFIG
    assert entry.disabled_by is None
    state = hass.states.get(eid)
    assert state.attributes["mesh_address"] == "0148"
    assert state.attributes["property_id"] == "0x1007"
    assert state.name == "WC mirror Run-on time"
    expert = registry.async_get(
        entity_id(hass, "number", f"{UID_LIGHT_SWITCH}-on_delay")
    )
    assert expert is not None
    assert expert.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    night = hass.states.get(
        entity_id(hass, "switch", f"{NODE_0232}-0001-led_night_mode")
    )
    assert night.attributes["property_id"] == "0xA001, 0xA002, 0xA004, 0xA005"
    assert night.name == "Living room rocker LED night mode"
    led = hass.states.get(entity_id(hass, "select", f"{NODE_0232}-0001-led2_mode_on"))
    assert led.name == "Living room rocker LED colour (switched on) B"
    assert (
        hass.states.get(entity_id(hass, "select", f"{UID_SOCKET}-led1_mode_on")).name
        == "Boiler LED colour (switched on)"
    )
    assert (
        hass.states.get(entity_id(hass, "switch", f"{UID_LIGHT_CTL}-dim_to_warm")).name
        == "Living room DALI Warm dimming"
    )


async def test_failing_read_job_is_logged_and_the_queue_goes_on(
    hass: HomeAssistant,
    init_with_mesh: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub: JungHomeHub = init_with_mesh.runtime_data
    reader = C.property_reader(hass, hub)
    ran: list[int] = []

    async def bad() -> None:
        raise RuntimeError("boom")

    async def good() -> None:
        ran.append(1)

    with caplog.at_level("DEBUG", logger="custom_components.junghome_ble"):
        reader.schedule(LIGHT_SWITCH, bad)
        reader.schedule(LIGHT_SWITCH, good)  # same element: the next chunk
        await settle(hass, 50)
    assert ran == [1]
    assert "property read failed: RuntimeError('boom')" in caplog.text


def test_node_page_for_every_product_family() -> None:
    assert C._node_page(_synthetic_node(0x03, [0x0001])) == "socket"
    assert C._node_page(_synthetic_node(0x0A, [0x0001])) == "rtr"
    assert C._node_page(_synthetic_node(0x08, [0x0001])) == "detector"
    assert C._node_page(_synthetic_node(0x15, [0x0001])) == "mini"
    assert C._node_page(_synthetic_node(0x05, [0x0001])) == "control_switch"
    assert C._node_page(_synthetic_node(0x0B, [0x0001])) == "gateway"


def test_device_lock_flags_per_product_lock_operation_enabled() -> None:
    """*Lock operation* and *Lock factory reset* on every device node, addressed to its primary element and shown
    under that element's device; *Key lock* and *Lock configuration* on a room thermostat too; never on the
    gateway. Only *Lock operation* is on by default: the app's normal list, its bit confirmed on air."""
    hub = fake_hub()
    targets = C.device_lock_targets(hub)
    assert [
        (t.address, t.flag, t.bit) for t in targets if t.node.unicast == LIGHT_SWITCH
    ] == [
        (LIGHT_SWITCH, "local_devices_lock", 2),
        (LIGHT_SWITCH, "factory_reset_time_limit", 1),
    ]
    assert {t.node.unicast for t in targets} == {0x0148, 0x0232, 0x0172, 0x0300, 0x0400}
    assert {t.flag for t in targets if t.enabled_default} == {"local_devices_lock"}
    assert targets[0].unique_id == f"{NODE_0148}-0001-local_devices_lock"
    assert targets[0].translation_key == "local_devices_lock"
    assert targets[0].specs == (spec(C.PROPERTY_DEVICE_LOCK),)
    assert (
        targets[0].device_info["name"] == "WC mirror"
    )  # the load the primary element hosts
    rtr = _synthetic_node(0x0A, [0x0001])
    hub.cdb.nodes.append(rtr)
    hub.devices = build_devices(hub.cdb)
    assert [
        (t.flag, t.bit, t.page, t.enabled_default)
        for t in C.device_lock_targets(hub)
        if t.node is rtr
    ] == [
        ("local_devices_lock", 2, "rtr", True),
        ("factory_reset_time_limit", 1, "rtr", False),
        ("key_lock", 3, "rtr", False),  # its bit not seen on air yet
        ("configuration_lock", 4, "rtr", False),
    ]


def test_gateway_status_targets() -> None:
    """The gateway's API status flags and its address, on the gateway's node device (read-only); the waiting flag
    is off by default (a gateway without `api_client_name_asking` reports it set for good)."""
    hub = fake_hub()
    status = C.gateway_status_targets(hub)
    ip = C.gateway_ip_targets(hub)
    assert [(t.address, t.flag, t.bit, t.translation_key) for t in status] == [
        (GATEWAY, "api_available", 0, "gateway_api_available"),
        (
            GATEWAY,
            "client_waiting_for_approval",
            1,
            "gateway_client_waiting_for_approval",
        ),
    ]
    assert status[0].unique_id == f"node:{NODE_GATEWAY}-gateway_api_available"
    assert [(t.address, t.unique_id, t.specs) for t in ip] == [
        (GATEWAY, f"node:{NODE_GATEWAY}-gateway_ip", (spec(C.GATEWAY_IP),))
    ]
    assert all(t.page == "gateway" for t in (*status, *ip))
    assert [t.enabled_default for t in (*status, *ip)] == [True, False, True]
    assert ip[0].device_info["name"] == "Gateway 00DC"


def test_blind_targets_follow_the_property_products() -> None:
    """One entity per blind on its position element; a blind on a product without the property gets none."""
    cdb = CDB.load(FIXTURES / "Blinds.json")
    hub = SimpleNamespace(
        cdb=cdb,
        devices=build_devices(cdb, Metadata.from_export(cdb.export_meta)),
        device_ids={},
    )
    kitchen = hub.devices.blinds[0]
    locks = C.blind_targets(hub, C.PROPERTY_LOCK, "wind_alarm", enabled_default=False)  # type: ignore[arg-type]
    assert [t.address for t in locks] == [0x0500, 0x0600, 0x0700]
    assert locks[0].unique_id == f"{kitchen.unique_id}-wind_alarm"
    assert (locks[0].page, locks[0].enabled_default, locks[0].translation_key) == (
        "blind",
        False,
        "wind_alarm",
    )
    assert locks[0].device_info["name"] == "Kitchen blind"
    kitchen.node.pid = (
        0x04  # a switch actuator mini: lockable, but no blinds parameters
    )
    runs = C.blind_targets(
        hub, C.PROPERTY_REFERENCE_RUN, "reference_run_active", enabled_default=True
    )  # type: ignore[arg-type]
    assert [t.address for t in runs] == [0x0600, 0x0700]


# --------------------------------------------------------------------------- the Status builder (messages.py)


def test_vendor_property_status_builder() -> None:
    """`[pid u16 LE][userAccess u8][value]` behind C5 / CB / D1 27 05, for every server."""
    assert (
        M.vendor_property_status("user", 0x5013, b"\x01")
        == bytes.fromhex("d12705") + bytes.fromhex("135003") + b"\x01"
    )
    assert M.vendor_property_status(
        "admin", 0x1001, bytes(4), user_access=1
    ) == bytes.fromhex("c52705") + bytes.fromhex("011001") + bytes(4)
    assert M.vendor_property_status(
        "manufacturer", 0x0003, bytes.fromhex("0d020100")
    ) == bytes.fromhex("cb2705") + bytes.fromhex("030003") + bytes.fromhex("0d020100")
    assert (
        M.describe(M.vendor_property_status("user", 0x5013, b"\x01"))
        == "LBC User Property Status prop 0x5013 access=3 value=01 key_status_led=on"
    )
    with pytest.raises(ValueError, match="user access 4"):
        M.vendor_property_status("user", 0x5013, b"\x01", user_access=4)
    with pytest.raises(KeyError):
        M.vendor_property_status("owner", 1, b"")  # type: ignore[arg-type]
    assert M.VENDOR_PROPERTY_STATUS_OPCODES == {
        "admin": 0x05,
        "manufacturer": 0x0B,
        "user": 0x11,
    }


# --------------------------------------------------------------------------- a detector's walking test (switch.py)

PID_WALKING_TEST, PID_PRESENCE_CONTROL, PID_PIR = 0x6001, 0x6003, 0x6005
UID_WALK_MOTION = f"{UUID_MOTION.lower()}-0040-walking_test"
UID_WALK_PRESENCE = f"{UUID_PRESENCE.lower()}-0040-walking_test"
WALKING_TEST_OVER = timedelta(seconds=DETECTOR_WALKING_TEST_DURATION + 1)


async def start_detectors_with_mesh(
    hass: HomeAssistant, fake_link: FakeProxyLink, values: dict[tuple[int, int], bytes]
) -> PropertyMesh:
    """The detectors network with answering devices and both walking tests enabled (only they: everything enabled
    would queue reads nothing here answers in front of theirs)."""
    registry = er.async_get(hass)
    for uid in (UID_WALK_MOTION, UID_WALK_PRESENCE):
        registry.async_get_or_create("switch", "junghome_ble", uid)
    mesh = PropertyMesh(fake_link, values)
    await start_detectors(hass, make_detectors_entry(), fake_link)
    for uid in (UID_WALK_MOTION, UID_WALK_PRESENCE):
        eid = entity_id(hass, "switch", uid)
        await wait_until(
            hass, lambda eid=eid: hass.states.get(eid).state != STATE_UNKNOWN
        )
    return mesh


@pytest.fixture
async def walking(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    fast_timeouts: None,
) -> AsyncGenerator[PropertyMesh]:
    with patch.object(JungHomeDetectorOccupancy, "_maybe_refresh"):
        yield await start_detectors_with_mesh(hass, fake_link, {})


async def _walk(hass: HomeAssistant, service: str, uid: str) -> None:
    await hass.services.async_call(
        SWITCH_DOMAIN,
        service,
        {ATTR_ENTITY_ID: entity_id(hass, "switch", uid)},
        blocking=True,
    )


def _walking_sets(mesh: PropertyMesh) -> list[tuple[int, int, bytes]]:
    return [s for s in mesh.sets if s[1] in (PID_WALKING_TEST, PID_PRESENCE_CONTROL)]


async def test_walking_test_is_off_by_default(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """Unverified on air: a config switch per detector, registered disabled."""
    await start_detectors(hass, make_detectors_entry(), fake_link)
    registry = er.async_get(hass)
    for uid in (UID_WALK_MOTION, UID_WALK_PRESENCE):
        entry = registry.async_get(entity_id(hass, "switch", uid))
        assert entry is not None
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert entry.entity_category is EntityCategory.CONFIG


async def test_walking_test_runs_like_the_apps(
    hass: HomeAssistant, walking: PropertyMesh
) -> None:
    """On: the test flag, then the presence control; the zones every second; both off again after five minutes."""
    mesh = walking
    eid = entity_id(hass, "switch", UID_WALK_PRESENCE)
    state = hass.states.get(eid)
    assert state.attributes["friendly_name"] == "Presence detector 0510 Walking test"
    assert state.state == STATE_OFF
    assert "pir_zones" not in state.attributes
    await _walk(hass, SERVICE_TURN_ON, UID_WALK_PRESENCE)
    assert _walking_sets(mesh) == [
        (DETECTOR_PRESENCE, PID_WALKING_TEST, b"\x01"),
        (DETECTOR_PRESENCE, PID_PRESENCE_CONTROL, b"\x01"),
    ]
    assert hass.states.get(eid).state == STATE_ON
    # the zones, polled on the Manufacturer server: bit 0 = A, 1 = B, 2 = C on the ceiling detector
    mesh.values[DETECTOR_PRESENCE, PID_PIR] = bytes([0b101])
    start = dt_util.utcnow()
    async_fire_time_changed(hass, start + timedelta(seconds=1.5))
    await settle(hass)
    assert (DETECTOR_PRESENCE, PID_PIR) in mesh.gets
    assert hass.states.get(eid).attributes["pir_zones"] == ["a", "c"]
    mesh.values[DETECTOR_PRESENCE, PID_PIR] = b"\x00"
    async_fire_time_changed(hass, start + timedelta(seconds=3))
    await settle(hass)
    assert hass.states.get(eid).attributes["pir_zones"] == []
    # five minutes on: stopped, and nothing is polled any more
    async_fire_time_changed(hass, start + WALKING_TEST_OVER)
    await settle(hass)
    assert _walking_sets(mesh)[2:] == [
        (DETECTOR_PRESENCE, PID_WALKING_TEST, b"\x00"),
        (DETECTOR_PRESENCE, PID_PRESENCE_CONTROL, b"\x00"),
    ]
    state = hass.states.get(eid)
    assert state.state == STATE_OFF
    assert "pir_zones" not in state.attributes
    polls = mesh.gets.count((DETECTOR_PRESENCE, PID_PIR))
    async_fire_time_changed(hass, start + 2 * WALKING_TEST_OVER)
    await settle(hass)
    assert mesh.gets.count((DETECTOR_PRESENCE, PID_PIR)) == polls
    assert len(_walking_sets(mesh)) == 4


async def test_walking_test_stopped_by_hand_and_restarted(
    hass: HomeAssistant, walking: PropertyMesh
) -> None:
    """Off stops the poll and the pending end; starting again counts the five minutes from then."""
    mesh = walking
    eid = entity_id(hass, "switch", UID_WALK_MOTION)
    start = dt_util.utcnow()
    await _walk(hass, SERVICE_TURN_ON, UID_WALK_MOTION)
    await _walk(hass, SERVICE_TURN_OFF, UID_WALK_MOTION)
    assert hass.states.get(eid).state == STATE_OFF
    polls = mesh.gets.count((DETECTOR_MOTION, PID_PIR))
    async_fire_time_changed(hass, start + timedelta(seconds=5))
    await settle(hass)
    assert mesh.gets.count((DETECTOR_MOTION, PID_PIR)) == polls
    # the wall detector has two zones: bit 2 is no zone of it
    mesh.values[DETECTOR_MOTION, PID_PIR] = bytes([0b111])
    await _walk(hass, SERVICE_TURN_ON, UID_WALK_MOTION)
    async_fire_time_changed(hass, start + timedelta(seconds=200))
    await settle(hass)
    assert hass.states.get(eid).attributes["pir_zones"] == ["a", "b"]
    # started again while running: the end is counted from now (a new timer replaces the pending one)
    entity = hass.data[DATA_INSTANCES]["switch"].get_entity(eid)
    first_end = entity._stop
    await _walk(hass, SERVICE_TURN_ON, UID_WALK_MOTION)
    assert entity._stop is not None
    assert entity._stop is not first_end
    async_fire_time_changed(hass, start + WALKING_TEST_OVER)
    await settle(hass)
    assert hass.states.get(eid).state == STATE_OFF


async def test_a_walking_test_found_running_is_ended_and_a_silent_detector_logged(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    fast_timeouts: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A test the app started is followed from its read: polled, and stopped five minutes later; a stop the
    detector does not confirm is a warning, not an error nobody would see."""
    with patch.object(JungHomeDetectorOccupancy, "_maybe_refresh"):
        mesh = await start_detectors_with_mesh(
            hass, fake_link, {(DETECTOR_MOTION, PID_WALKING_TEST): b"\x01"}
        )
        eid = entity_id(hass, "switch", UID_WALK_MOTION)
        assert hass.states.get(eid).state == STATE_ON
        start = dt_util.utcnow()
        async_fire_time_changed(hass, start + timedelta(seconds=2))
        await settle(hass)
        assert (DETECTOR_MOTION, PID_PIR) in mesh.gets
        # one question at a time: a poll while the last one is unanswered asks nothing more
        entity = hass.data[DATA_INSTANCES]["switch"].get_entity(eid)
        mesh.silent.add((DETECTOR_MOTION, PID_PIR))
        gets = len(mesh.gets)
        entity._poll_zones(None)
        entity._poll_zones(None)
        await wait_until(hass, entity._poll_read.done)
        assert (
            mesh.gets[gets:] == [(DETECTOR_MOTION, PID_PIR)] * 3
        )  # one read, three attempts
        mesh.silent.add((DETECTOR_MOTION, PID_WALKING_TEST))
        async_fire_time_changed(hass, start + WALKING_TEST_OVER)
        await wait_until(
            hass, lambda: "the walking test was not stopped" in caplog.text
        )
        assert (DETECTOR_MOTION, PID_WALKING_TEST, b"\x00") in mesh.sets


# --------------------------------------------------------------------------- update entity, re-reads, review-4 H4-10


async def update_entity(hass: HomeAssistant, eid: str) -> None:
    await hass.services.async_call(
        "homeassistant", "update_entity", {ATTR_ENTITY_ID: eid}, blocking=True
    )


UID_RUN_ON = f"{UID_LIGHT_SWITCH}-timed_on_duration"


async def test_update_entity_reads_a_config_entity_now(
    hass: HomeAssistant,
    init_with_mesh: MockConfigEntry,
    mesh: PropertyMesh,
    fake_link: FakeProxyLink,
) -> None:
    """Review-4 H4-10: a value changed in the app is answered to the app only; `homeassistant.update_entity` asks
    the device again at once — even within PROPERTY_READ_FRESH of the last read — and shows the new value. The same
    element and value are asked at most once per UPDATE_READ_INTERVAL."""
    hub: JungHomeHub = init_with_mesh.runtime_data
    eid = entity_id(hass, "number", UID_RUN_ON)
    before = hass.states.get(eid).state
    changed = (90_000).to_bytes(4, "little")
    mesh.values[LIGHT_SWITCH, PID_RUN_ON] = changed  # set in the app
    gets = mesh.gets.count((LIGHT_SWITCH, PID_RUN_ON))
    await update_entity(hass, eid)
    assert mesh.gets.count((LIGHT_SWITCH, PID_RUN_ON)) == gets + 1
    assert hub.states[LIGHT_SWITCH].properties[PID_RUN_ON] == changed
    assert hass.states.get(eid).state != before
    await update_entity(hass, eid)  # at once again: rate-limited
    assert mesh.gets.count((LIGHT_SWITCH, PID_RUN_ON)) == gets + 1

    # a setup state is asked again too, however fresh its last answer
    fake_link.sent.clear()
    await update_entity(
        hass, entity_id(hass, "number", f"{UID_LIGHT_DIMMER}-lightness_min")
    )
    assert (LIGHT_DIMMER, M.light_lightness_range_get()) in {
        (dst, pdu) for _, dst, pdu in fake_link.sent
    }

    # a switch with nothing to read (the status LED, written the gateway's way) asks nothing
    fake_link.sent.clear()
    await update_entity(
        hass, entity_id(hass, "switch", f"{UID_BUTTON_WC}-key_status_led")
    )
    assert fake_link.sent == []

    # without a link nothing is asked; a link lost under the read is only logged (no error from the action)
    update_reads(hub).clear()
    with patch.object(
        type(hub), "connected", new_callable=PropertyMock, return_value=False
    ):
        await update_entity(hass, eid)
    assert mesh.gets.count((LIGHT_SWITCH, PID_RUN_ON)) == gets + 1
    with patch.object(
        C.PropertyReader, "fetch", AsyncMock(side_effect=ConnectionError("gone"))
    ) as fetch:
        await update_entity(hass, eid)
    fetch.assert_awaited_once()
    assert hub.states[LIGHT_SWITCH].properties[PID_RUN_ON] == changed


async def test_config_entity_reread_defaults_to_its_read() -> None:
    """An entity that reads something else than properties or a setup state re-reads with its own read."""
    hub = fake_hub()
    node = hub.cdb.node_by_addr(LIGHT_SWITCH)
    assert node is not None
    target = C.SetupTarget(
        node=node,
        address=LIGHT_SWITCH,
        unique_id="x",
        device_info={"name": "x"},
        page="lamp",
        state=C.ON_POWER_UP,
        entity="power_on_behaviour",
    )
    entity = C.ConfigEntity(hub, target)
    read = AsyncMock(return_value=True)
    with patch.object(entity, "_read", read):
        assert entity._update_read() == ("x", entity._reread)
        await entity._reread()
    read.assert_awaited_once()


def age(entity: C.ConfigEntity) -> None:
    """Move the entity's last read, and every answer its reader holds, CONFIG_REREAD_INTERVAL into the past."""
    assert entity._read_at is not None
    entity._read_at -= const.CONFIG_REREAD_INTERVAL
    read_at = entity.reader._read_at
    for key in read_at:
        read_at[key] -= const.CONFIG_REREAD_INTERVAL


async def test_config_values_are_read_again_on_a_link_after_the_interval(
    hass: HomeAssistant,
    init_with_mesh: MockConfigEntry,
    mesh: PropertyMesh,
    fake_link: FakeProxyLink,
) -> None:
    """Review-4 H4-10: values read once were never read again, so a change made in the app stayed hidden. They are
    read again on a later link once CONFIG_REREAD_INTERVAL has passed — not on a link before that, and once per
    link — through the reader's queue like the first read."""
    eid = entity_id(hass, "number", UID_RUN_ON)
    number = hass.data[DATA_INSTANCES]["number"].get_entity(eid)
    assert isinstance(number, C.PropertyEntity)
    assert number._read_done
    assert number._read_at is not None

    async def new_link() -> None:
        fake_link.drop_link()
        await wait_for_link(hass, init_with_mesh, connected=False)
        await wait_for_link(hass, init_with_mesh)
        await settle(hass)

    gets = mesh.gets.count((LIGHT_SWITCH, PID_RUN_ON))
    await new_link()  # within the interval: not asked
    assert mesh.gets.count((LIGHT_SWITCH, PID_RUN_ON)) == gets

    changed = (90_000).to_bytes(4, "little")
    mesh.values[LIGHT_SWITCH, PID_RUN_ON] = changed  # set in the app meanwhile
    hub: JungHomeHub = init_with_mesh.runtime_data
    age(number)
    await new_link()
    assert mesh.gets.count((LIGHT_SWITCH, PID_RUN_ON)) == gets + 1
    assert hub.states[LIGHT_SWITCH].properties[PID_RUN_ON] == changed
    # once per link: another update of the element on this link asks nothing, even were the interval over
    age(number)
    async_dispatcher_send(
        hass, SIGNAL_UPDATE.format(init_with_mesh.entry_id, LIGHT_SWITCH)
    )
    await settle(hass)
    assert mesh.gets.count((LIGHT_SWITCH, PID_RUN_ON)) == gets + 1


async def test_battery_node_update_entity_is_skipped_and_reread_at_a_key_event(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    fast_timeouts: None,
) -> None:
    """A wall transmitter sleeps: `homeassistant.update_entity` asks it nothing (it would not answer). Its values
    are read again at the first key event after CONFIG_REREAD_INTERVAL, the moment it is awake."""
    mesh = PropertyMesh(fake_link)
    entry = await start_detectors(hass, make_detectors_entry(), fake_link)
    await settle(hass)
    eid = next(
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id.startswith(UUID_1G.lower())
        and e.unique_id.endswith("-led1_mode_on")
    )
    select = hass.data[DATA_INSTANCES]["select"].get_entity(eid)
    assert isinstance(select, C.PropertyEntity)

    async def key_event(counter: int) -> None:
        fake_link.inject(
            KEY_1G, GROUP_GATEWAY, vendor_button_event(counter, BUTTON_CLICK)
        )
        await hass.async_block_till_done()
        await wait_until(hass, lambda: not select._read_pending)

    await key_event(1)
    assert select._read_done
    gets = len(mesh.gets)
    fake_link.sent.clear()
    await update_entity(hass, eid)
    assert fake_link.sent == []
    await key_event(2)  # read within the interval: nothing to ask
    assert (TRANSMITTER_1G, PID_LED1_ON) not in mesh.gets[gets:]
    age(select)
    await key_event(3)
    assert (TRANSMITTER_1G, PID_LED1_ON) in mesh.gets[gets:]
