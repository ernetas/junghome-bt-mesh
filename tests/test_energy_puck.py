"""The energy puck (0x0010): its output is a metered light with the socket's meter entities (review-3 F3).

Against the synthetic `MeshNetwork-puck.json`, the base network plus a puck whose composition is a guess (no puck is
in the maintainer's network): the switched output on the primary element 0610, inputs E1 / E2 at 0611 / 0612, the
meter (Sensor Server) at 0613. What the puck gets follows the app's `MeasureLampDevice` (`docs/android/properties.md`
§4): power, the energy counters, the charts and their reset — not the socket's voltage, current, power-on hours or
thresholds.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_METADATA_DIR,
    CONF_UNICAST,
    DOMAIN,
)
from custom_components.junghome_ble.coordinator import (
    PROPERTY_PRECISE_TOTAL_ENERGY,
    ElementState,
    lacks_precise_energy,
)
from custom_components.junghome_ble.diagnostics import _device_summary
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.devices import Light

from . import test_button as TB
from . import test_coordinator as TC
from .conftest import (
    FIXTURES,
    META_DIR,
    FakeProxyLink,
    settle,
    setup_entry,
    wait_for_link,
    wait_until,
)
from .helpers import (
    OUR_ADDRESS,
    PROPERTY_POWER_ON_TIME,
    PROPERTY_TOTAL_ENERGY,
    SENSOR_CURRENT,
    SENSOR_POWER,
    SENSOR_VOLTAGE,
    UID_SOCKET,
    device_name_of,
    entity_id,
    onoff_status,
    property_absent_status,
    sensor_status,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# the fixtures of the socket's tests, used here as they are there (`no_property_reads` is autouse)
answering_link, init_answered, no_property_reads = (
    TC.answering_link,
    TC.init_answered,
    TC.no_property_reads,
)
fast_requests = TB.fast_requests
ZERO, answer_counters, press = TB.ZERO, TB.answer_counters, TB.press
COUNTER_GETS = (TC.HOURS_GET, TC.ENERGY_GET, TC.RESETTABLE_GET, TC.SINCE_ON_GET)

PUCK_CDB_PATH = FIXTURES / "MeshNetwork-puck.json"
PUCK, PUCK_METER = 0x0610, 0x0613
SOCKET, SOCKET_METER = 0x0172, 0x0173
UID_PUCK = "00005eff-fe00-5361-0000-000000000000-0001"
UID_PUCK_RESET = f"{UID_PUCK}-reset_consumption"


@pytest.fixture
def cdb() -> CDB:
    return CDB.load(Path(PUCK_CDB_PATH))


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={
            CONF_CDB_PATH: str(PUCK_CDB_PATH),
            CONF_METADATA_DIR: META_DIR,
            CONF_UNICAST: "0D00",
        },
    )


def entities_of(hass: HomeAssistant, entry: MockConfigEntry, uid: str) -> set[str]:
    """`domain:suffix` of every entity the entry registered under the load with unique id `uid`."""
    return {
        f"{e.domain}:{e.unique_id.removeprefix(f'{uid}-')}"
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id.startswith(f"{uid}-")
    }


async def test_the_puck_output_is_a_metered_light(
    hass: HomeAssistant, init_answered: MockConfigEntry
) -> None:
    """The output stays a light; its meter's element is none of the entities, the inputs remain E1 / E2."""
    hub = init_answered.runtime_data
    light = hub.devices.by_address[PUCK]
    assert isinstance(light, Light)
    assert (light.kind, light.meter_address) == ("switch", PUCK_METER)
    assert [load.address for load in hub.devices.metered] == [0x0172, PUCK]
    assert hub.devices.by_meter(PUCK_METER) is light
    assert PUCK_METER not in hub.devices.by_address
    assert [b.key for b in hub.devices.buttons if b.node is light.node] == ["E1", "E2"]
    assert entity_id(hass, "light", UID_PUCK) == "light.kitchen_energy_puck_0610"
    summary = _device_summary(hub, light.node)
    assert [(li["address"], li["meter"]) for li in summary["lights"]] == [
        ("0610", "0613")
    ]


async def test_the_puck_gets_the_meter_entities_not_the_socket_extras(
    hass: HomeAssistant, init_answered: MockConfigEntry
) -> None:
    """Power, the three energy counters, *Installed* and *Reset consumption* under the light's device.

    No voltage, current or power-on hours (the app reads none on the puck), no thresholds (the socket's alone);
    the socket keeps all of its own.
    """
    puck = entities_of(hass, init_answered, UID_PUCK)
    assert {
        "sensor:power",
        "sensor:energy",
        "sensor:energy_resettable",
        "sensor:energy_since_on",
        "sensor:installed",
        "button:reset_consumption",
    } <= puck
    assert (
        not {
            "sensor:voltage",
            "sensor:current",
            "sensor:power_on_time",
            "sensor:switch_on_threshold",
            "sensor:switch_off_threshold",
        }
        & puck
    )
    assert {
        "sensor:voltage",
        "sensor:current",
        "sensor:power_on_time",
        "sensor:switch_on_threshold",
    } <= entities_of(hass, init_answered, UID_SOCKET)
    for domain, key in (
        ("sensor", "power"),
        ("sensor", "installed"),
        ("button", "reset_consumption"),
    ):
        assert device_name_of(hass, entity_id(hass, domain, f"{UID_PUCK}-{key}")) == (
            "Energy puck 0610"
        )
    # *Sensor values for IoT systems* sits on the metered output, as the socket's sits on the socket
    publication = entity_id(
        hass, "switch", f"{UID_PUCK.removesuffix('-0001')}-sensor_publication"
    )
    assert device_name_of(hass, publication) == "Energy puck 0610"
    reset = er.async_get(hass).async_get(entity_id(hass, "button", UID_PUCK_RESET))
    assert reset is not None
    assert reset.entity_category is EntityCategory.DIAGNOSTIC
    assert reset.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_the_meter_is_asked_for_power_and_the_energy_counters(
    hass: HomeAssistant, init_answered: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The connect-time refresh asks the puck's meter for its power only, the energy poll for the meter's counters.

    Their answers land on the output's element, like a socket's; a publication of the meter does too.
    """
    hub = init_answered.runtime_data
    to_puck = [
        (dst, pdu) for _, dst, pdu in fake_link.sent if dst in (PUCK, PUCK_METER)
    ]
    assert (PUCK_METER, M.sensor_get(SENSOR_POWER)) in to_puck
    assert (PUCK_METER, M.sensor_get(SENSOR_VOLTAGE)) not in to_puck
    assert (PUCK_METER, M.sensor_get(SENSOR_CURRENT)) not in to_puck
    assert [(dst, pdu) for dst, pdu in to_puck if pdu in COUNTER_GETS] == [
        (PUCK_METER, TC.ENERGY_GET),
        (PUCK_METER, TC.RESETTABLE_GET),
        (PUCK_METER, TC.SINCE_ON_GET),
    ]
    st = hub.states[PUCK]
    assert (
        st.power_w,
        st.energy_wh,
        st.energy_resettable_wh,
        st.energy_since_on_wh,
    ) == (
        3.8,
        210198,
        210040,
        1009,
    )
    assert st.power_on_hours is None
    assert PUCK_METER not in hub.states

    fake_link.inject(
        PUCK_METER, 0xC0A3, sensor_status((SENSOR_POWER, (1234).to_bytes(2, "little")))
    )
    await hass.async_block_till_done()
    power = hass.states.get(entity_id(hass, "sensor", f"{UID_PUCK}-power"))
    assert power is not None
    assert power.state == "123.4"
    energy = hass.states.get(entity_id(hass, "sensor", f"{UID_PUCK}-energy"))
    assert energy is not None
    assert energy.state == "210.198"


@pytest.fixture
async def init_puck_reset(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    fast_requests: None,
) -> MockConfigEntry:
    """The puck network set up and connected, with the puck's Reset consumption button enabled by the user."""
    er.async_get(hass).async_get_or_create(
        "button", DOMAIN, UID_PUCK_RESET, disabled_by=None
    )
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    hub = mock_config_entry.runtime_data
    await wait_until(
        hass,
        lambda: hub._refresh_task is None or hub._refresh_task.done(),
        what="the connect-time refresh",
    )
    # nobody answered the refresh either (every request gives up after milliseconds here), so every node counts as
    # unreachable (`Liveness.missed_answer`); a status from the puck brings it back, as any message from it does
    fake_link.inject(PUCK, 0xC000, onoff_status(False))
    await hass.async_block_till_done()
    return mock_config_entry


async def test_reset_consumption_zeroes_the_meter_total_only(
    hass: HomeAssistant, init_puck_reset: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The app's 0x006A Set to the meter; no power-on hours to zero on the puck's output."""
    hub = init_puck_reset.runtime_data
    st = hub.element_state(PUCK)
    st.energy_wh, st.energy_resettable_wh = 210198, 210040
    answer_counters(fake_link, set_value=ZERO, get_value=None)
    fake_link.sent.clear()
    await press(hass, entity_id(hass, "button", UID_PUCK_RESET))
    assert fake_link.sent == [
        (
            OUR_ADDRESS,
            PUCK_METER,
            M.generic_property_set("admin", PROPERTY_TOTAL_ENERGY, ZERO),
        )
    ]
    assert M.generic_property_set("admin", PROPERTY_POWER_ON_TIME, ZERO) not in [
        pdu for _, _, pdu in fake_link.sent
    ]
    assert (st.energy_wh, st.energy_resettable_wh) == (210198, 0)


def test_energy_total_is_the_lifetime_counter_unless_the_meter_lacks_it() -> None:
    """*Energy* reads 0x0072; on a fallen-back load 0x006A, the counter the app reads on the puck."""
    st = ElementState(energy_wh=210198, energy_resettable_wh=210040)
    assert st.energy_total == 210198
    st.energy_fallback = True
    assert st.energy_total == 210040
    st.energy_resettable_wh = None
    assert st.energy_total is None  # no mixing of the two counters


async def test_energy_falls_back_where_the_meter_says_it_has_no_lifetime_total(
    hass: HomeAssistant, init_answered: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A 0x0072 Status carrying the id alone makes the puck's *Energy* its 0x006A; a socket never falls back."""
    hub = init_answered.runtime_data
    energy = entity_id(hass, "sensor", f"{UID_PUCK}-energy")
    absent = property_absent_status(
        PROPERTY_PRECISE_TOTAL_ENERGY, opcode=M.GEN_MANU_PROP_STATUS
    )
    fake_link.inject(SOCKET_METER, OUR_ADDRESS, absent)
    await hass.async_block_till_done()
    assert not hub.states[SOCKET].energy_fallback
    state = hass.states.get(energy)
    assert state is not None
    assert state.state == "210.198"

    fake_link.inject(PUCK_METER, OUR_ADDRESS, absent)
    await hass.async_block_till_done()
    assert hub.states[PUCK].energy_fallback
    state = hass.states.get(energy)
    assert state is not None
    assert state.state == "210.04"


async def test_a_meter_without_a_manufacturer_server_is_not_asked_for_it(
    hass: HomeAssistant, init_answered: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """No Generic Manufacturer Property Server on the puck's meter: no 0x0072 / 0x000D Gets, *Energy* is 0x006A.

    The socket keeps its reads whatever its meter's composition (the fixture's lacks the server; on air it answers).
    """
    hub = init_answered.runtime_data
    puck, socket = hub.devices.by_address[PUCK], hub.devices.by_address[SOCKET]
    assert not lacks_precise_energy(hub.cdb, puck)
    meter = hub.cdb.element(PUCK_METER)
    assert meter is not None
    meter.models.remove("1012")
    assert lacks_precise_energy(hub.cdb, puck)
    assert not lacks_precise_energy(hub.cdb, socket)
    fake_link.sent.clear()
    await hub._get_counters(puck)
    assert [(dst, pdu) for _, dst, pdu in fake_link.sent if pdu in COUNTER_GETS] == [
        (PUCK_METER, TC.RESETTABLE_GET)
    ]
    assert hub.states[PUCK].energy_fallback
    assert hub.states[PUCK].energy_total == hub.states[PUCK].energy_resettable_wh
