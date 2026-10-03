"""Number platform: numeric device parameters (delays, run-on time, ...)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.number import (
    ATTR_MAX,
    ATTR_MIN,
    ATTR_MODE,
    ATTR_STEP,
    ATTR_VALUE,
    SERVICE_SET_VALUE,
    NumberDeviceClass,
    NumberMode,
)
from homeassistant.components.number import (
    DOMAIN as NUMBER_DOMAIN,
)
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_ENTITY_ID,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfTime,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES

from custom_components.junghome_ble import config_entities as C
from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import properties as P
from custom_components.junghome_ble.number import JungHomePropertyNumber, codec_range

from . import property_helpers as ph
from .conftest import FakeProxyLink, settle, setup_entry, wait_for_link, wait_until
from .helpers import (
    LIGHT_SWITCH,
    OUR_ADDRESS,
    SOCKET,
    UID_LIGHT_SWITCH,
    UID_SOCKET,
    entity_id,
)
from .property_helpers import (
    PID_RUN_ON,
    PropertyMesh,
    fake_hub,
    vendor_status,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

fast_timeouts, mesh, init_with_mesh = ph.fast_timeouts, ph.mesh, ph.init_with_mesh

UID_RUN_ON = f"{UID_LIGHT_SWITCH}-timed_on_duration"
UID_ON_DELAY = f"{UID_LIGHT_SWITCH}-on_delay"
UID_BLOCKING = f"{UID_SOCKET}-switch_blocking_time"
PID_BLOCKING = 0x100D


def ms(seconds: float) -> bytes:
    return round(seconds * 1000).to_bytes(4, "little")


async def test_entity_and_initial_read(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    mesh.values[LIGHT_SWITCH, PID_RUN_ON] = ms(120)
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    eid = entity_id(hass, "number", UID_RUN_ON)
    assert eid == "number.wc_wc_mirror_run_on_time"
    state = hass.states.get(eid)
    assert state is not None
    assert state.state == "120.0"
    assert state.attributes[ATTR_MIN] == 0
    assert state.attributes[ATTR_MAX] == 14400
    assert state.attributes[ATTR_STEP] == 1
    assert state.attributes[ATTR_MODE] == NumberMode.BOX
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfTime.SECONDS
    assert state.attributes[ATTR_DEVICE_CLASS] == NumberDeviceClass.DURATION
    assert state.attributes["property_id"] == "0x1007"
    entry = er.async_get(hass).async_get(eid)
    assert entry is not None
    assert entry.entity_category is EntityCategory.CONFIG
    # the app read it with an Admin Property Get: `C2 27 05 [07 10]`
    assert (
        OUR_ADDRESS,
        LIGHT_SWITCH,
        M.vendor_property_get("admin", PID_RUN_ON),
    ) in mesh.link.sent


async def test_expert_parameters_are_disabled_by_default(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    eid = entity_id(hass, "number", UID_ON_DELAY)
    entry = registry.async_get(eid)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(eid) is None


async def test_enabled_expert_parameter_is_read_and_written(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    er.async_get(hass).async_get_or_create(
        "number", DOMAIN, UID_BLOCKING, disabled_by=None
    )  # the user enabled it
    mesh.values[SOCKET, PID_BLOCKING] = (250).to_bytes(2, "little")
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    eid = entity_id(hass, "number", UID_BLOCKING)
    state = hass.states.get(eid)
    assert state.state == "250"
    assert state.attributes[ATTR_MIN] == 100
    assert state.attributes[ATTR_MAX] == 10000
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfTime.MILLISECONDS

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: eid, ATTR_VALUE: 500},
        blocking=True,
    )
    # an integer codec gets a whole number: `C3 27 05 [0D 10][03][F4 01]`
    assert mesh.sets[-1] == (SOCKET, PID_BLOCKING, (500).to_bytes(2, "little"))
    assert mesh.link.sent[-1] == (
        OUR_ADDRESS, SOCKET, M.vendor_property_set("admin", PID_BLOCKING, (500).to_bytes(2, "little")),
    )  # fmt: skip
    assert hass.states.get(eid).state == "500"


async def test_factory_delay_reads_as_off_and_is_still_written(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """0xFFFFFFFF, the switch-off delay a socket reported on air, is off (0 s) as in the app, not 49 days."""
    uid = f"{UID_SOCKET}-off_delay"
    er.async_get(hass).async_get_or_create("number", DOMAIN, uid, disabled_by=None)
    mesh.values[SOCKET, 0x1002] = b"\xff\xff\xff\xff"
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    eid = entity_id(hass, "number", uid)
    assert hass.states.get(eid).state == "0.0"

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: eid, ATTR_VALUE: 30},
        blocking=True,
    )
    assert mesh.sets[-1] == (SOCKET, 0x1002, ms(30))
    assert hass.states.get(eid).state == "30.0"


async def test_set_value_confirmed_by_the_status(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry, mesh: PropertyMesh
) -> None:
    eid = entity_id(hass, "number", UID_RUN_ON)
    assert hass.states.get(eid).state == "0.0"
    gets = len(mesh.gets)
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: eid, ATTR_VALUE: 90},
        blocking=True,
    )
    # the app's acknowledged Admin Set: `C3 27 05 [07 10][01][u32 LE ms]` (userAccess READ, on air)
    assert mesh.link.sent[-1] == (
        OUR_ADDRESS, LIGHT_SWITCH, bytes.fromhex("c32705") + bytes.fromhex("0710") + b"\x01" + ms(90),
    )  # fmt: skip
    assert hass.states.get(eid).state == "90.0"
    assert len(mesh.gets) == gets  # the Status that answered the Set was enough


async def test_set_value_without_status_is_read_back(
    hass: HomeAssistant,
    init_with_mesh: MockConfigEntry,
    mesh: PropertyMesh,
    fast_timeouts: None,
) -> None:
    eid = entity_id(hass, "number", UID_RUN_ON)
    mesh.confirm_sets = False
    mesh.values[LIGHT_SWITCH, PID_RUN_ON] = ms(
        45
    )  # what the device reports once asked again
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: eid, ATTR_VALUE: 45},
        blocking=True,
    )
    assert mesh.sets[-1] == (LIGHT_SWITCH, PID_RUN_ON, ms(45))
    assert mesh.gets[-1] == (LIGHT_SWITCH, PID_RUN_ON)
    assert hass.states.get(eid).state == "45.0"


async def test_set_value_the_read_back_does_not_show_raises(
    hass: HomeAssistant,
    init_with_mesh: MockConfigEntry,
    mesh: PropertyMesh,
    fast_timeouts: None,
) -> None:
    eid = entity_id(hass, "number", UID_RUN_ON)
    mesh.confirm_sets = False  # no Status; the read-back reports the old value
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: eid, ATTR_VALUE: 45},
            blocking=True,
        )
    assert exc.value.translation_key == "setting_not_applied"
    assert exc.value.translation_placeholders == {"entity": eid}
    assert hass.states.get(eid).state == "0.0"


async def test_unsolicited_status_updates_the_value(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    eid = entity_id(hass, "number", UID_RUN_ON)
    fake_link.inject(LIGHT_SWITCH, 0xC061, vendor_status(0x05, PID_RUN_ON, ms(3600)))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "3600.0"
    fake_link.inject(
        LIGHT_SWITCH, 0xC061, vendor_status(0x05, PID_RUN_ON, b"\x01")
    )  # malformed
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_UNKNOWN


async def test_unanswered_read_stays_unknown(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float], fast_timeouts: None,
) -> None:  # fmt: skip
    mesh.silent.add((LIGHT_SWITCH, PID_RUN_ON))
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await wait_until(hass, lambda: mesh.gets.count((LIGHT_SWITCH, PID_RUN_ON)) == 2)
    eid = entity_id(hass, "number", UID_RUN_ON)
    assert hass.states.get(eid).state == STATE_UNKNOWN


async def test_send_failure_raises(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    eid = entity_id(hass, "number", UID_RUN_ON)
    fake_link.write_error = ConnectionError("proxy disconnected")
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: eid, ATTR_VALUE: 1},
            blocking=True,
        )
    assert exc.value.translation_key == "send_failed"


async def test_value_the_codec_rejects_raises(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry
) -> None:
    eid = entity_id(hass, "number", UID_RUN_ON)
    entity = hass.data[DATA_INSTANCES]["number"].get_entity(eid)
    assert isinstance(entity, JungHomePropertyNumber)
    with pytest.raises(HomeAssistantError) as exc:
        await entity.async_write_value(-1)
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "value_rejected"
    placeholders = exc.value.translation_placeholders
    assert placeholders is not None
    assert placeholders["entity"] == eid
    assert "negative duration" in placeholders["error"]


def test_codec_ranges() -> None:
    assert codec_range(P.PERCENT) == (0, 100, 1)
    assert codec_range(P.POSITION) == (0, 100, 1)
    assert codec_range(P.MS32) == (0, 0xFFFFFFFF / 1000, 0.001)
    assert codec_range(P.Duration(2, "s")) == (0, 0xFFFF, 1)
    assert codec_range(P.TEMP_001C) == (-327.68, 327.67, 0.01)
    assert codec_range(P.U8) == (0, 255, 1)
    assert codec_range(P.S16) == (-32768, 32767, 1)
    with pytest.raises(TypeError):
        codec_range(P.BOOL)


def test_percent_properties_are_sliders_and_units_map() -> None:
    """A detector's PIR area (no such device in the fixture): slider 0-100 %, no device class."""
    hub = fake_hub()
    node = hub.cdb.node_by_addr(LIGHT_SWITCH)
    assert node is not None
    description = C.describe(P.PROPERTIES[0x6008])
    assert description is not None
    target = C.PropertyTarget(
        description=description, node=node, address=0x0702, unique_id="x", device_info={"name": "d"},  # type: ignore[typeddict-item]
        page="detector",
    )  # fmt: skip
    number = JungHomePropertyNumber(hub, target)
    assert number.mode is NumberMode.SLIDER
    assert (number.native_min_value, number.native_max_value, number.native_step) == (
        0,
        100,
        1,
    )
    assert number.native_unit_of_measurement == "%"
    assert number.device_class is None
    # a property without a catalogue range or unit falls back to the wire type
    description = C.describe(P.PROPERTIES[0x6021])
    assert description is not None
    plain = JungHomePropertyNumber(hub, C.PropertyTarget(
        description=description, node=node, address=0x0702, unique_id="y", device_info={"name": "d"},  # type: ignore[typeddict-item]
        page="detector",
    ))  # fmt: skip
    assert (plain.native_min_value, plain.native_max_value, plain.native_step) == (
        0,
        255,
        1,
    )
    assert plain.native_unit_of_measurement is None
    assert plain.mode is NumberMode.BOX
    # the RTR sensor offset is a temperature *difference*: unit K, device class temperature_delta (not temperature,
    # which would convert it like an absolute reading)
    description = C.describe(P.PROPERTIES[0x1224])
    assert description is not None
    offset = JungHomePropertyNumber(hub, C.PropertyTarget(
        description=description, node=node, address=0x0700, unique_id="z", device_info={"name": "d"},  # type: ignore[typeddict-item]
        page="rtr",
    ))  # fmt: skip
    assert (offset.native_unit_of_measurement, offset.device_class) == (
        "K",
        NumberDeviceClass.TEMPERATURE_DELTA,
    )
    assert (offset.native_min_value, offset.native_max_value, offset.native_step) == (
        -5,
        5,
        0.5,
    )
