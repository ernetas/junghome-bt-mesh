"""Mini-actuator inputs: named E1 / E2, a state entity per input, and edge evaluation (0x5009) — a mode switch and
two behaviour selects over one byte.

Runs against `fixtures/Blinds.json`: its blinds actuator mini (0500, inputs 0502 / 0503) and blinds PP2 puck (0700,
inputs 0701 / 0702) are the only mini actuators with inputs in the synthetic networks.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.select import SERVICE_SELECT_OPTION
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_OPTION,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_get_device_automations,
    mock_restore_cache,
)

from custom_components.junghome_ble import config_entities as C
from custom_components.junghome_ble.binary_sensor import JungHomeInputState
from custom_components.junghome_ble.const import CONF_CDB_PATH, CONF_UNICAST, DOMAIN
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import properties as P
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.devices import Metadata, build_devices

from . import property_helpers as ph
from .conftest import FIXTURES, FakeProxyLink, settle, setup_entry, wait_for_link
from .helpers import NODE_BLIND, entity_id
from .property_helpers import PropertyMesh, fake_hub

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

fast_timeouts, mesh = ph.fast_timeouts, ph.mesh

BLINDS_PATH = FIXTURES / "Blinds.json"
INPUT_E1 = 0x0502  # the blinds actuator mini's first input (location 0x40)
PID_EDGE = 0x5009
UID_MODE = f"{NODE_BLIND}-0040-input_edge_mode"
UID_RISING = f"{NODE_BLIND}-0040-input_edge_rising"
UID_FALLING = f"{NODE_BLIND}-0040-input_edge_falling"
UID_STATE_E1 = f"{NODE_BLIND}-0040-input_state"
# edge mode, rising edge = switch on (1 << 1), falling edge = switch off (2 << 3)
EDGE_ON_OFF = bytes([2 << 3 | 1 << 1 | 1])


def blinds_hub() -> Any:
    cdb = CDB.load(BLINDS_PATH)
    return ph.with_inserts(
        SimpleNamespace(
            cdb=cdb,
            devices=build_devices(cdb, Metadata.from_export(cdb.export_meta)),
            states={},
            device_ids={},
            entry=SimpleNamespace(title="test", entry_id="entry"),
        )
    )


def test_one_switch_and_two_selects_per_input_of_a_mini_actuator() -> None:
    hub = blinds_hub()
    switches = C.edge_detection_targets(hub, "switch")
    selects = C.edge_detection_targets(hub, "select")
    assert [(t.address, t.key, t.translation_key) for t in switches] == [
        (0x0502, "E1", "input_edge_mode_key"),
        (0x0503, "E2", "input_edge_mode_key"),
        (0x0701, "E1", "input_edge_mode_key"),
        (0x0702, "E2", "input_edge_mode_key"),
    ]
    assert [(t.address, t.part) for t in selects[:2]] == [
        (0x0502, "rising"),
        (0x0502, "falling"),
    ]
    assert len(selects) == 8
    targets = switches + selects
    assert not any(t.enabled_default for t in targets)
    assert {t.specs for t in targets} == {(P.PROPERTIES[PID_EDGE],)}
    assert len({t.unique_id for t in targets}) == 12
    assert C.edge_detection_targets(hub, "number") == []
    # push-buttons, sockets and the energy puck without inputs have none
    assert C.edge_detection_targets(fake_hub(), "switch") == []


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """The blinds network, the one with mini-actuator inputs."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME blinds test",
        unique_id="1fbd2c61a4b6e5a4",
        data={CONF_CDB_PATH: str(BLINDS_PATH), CONF_UNICAST: "0D00"},
    )


@pytest.fixture
def edges_enabled(hass: HomeAssistant) -> None:
    """The user enabled the three edge entities of input E1 (hidden by default)."""
    registry = er.async_get(hass)
    for domain, uid in (
        ("switch", UID_MODE),
        ("select", UID_RISING),
        ("select", UID_FALLING),
    ):
        registry.async_get_or_create(domain, DOMAIN, uid, disabled_by=None)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> tuple[str, str, str]:
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    return (
        entity_id(hass, "switch", UID_MODE),
        entity_id(hass, "select", UID_RISING),
        entity_id(hass, "select", UID_FALLING),
    )


async def _select(hass: HomeAssistant, eid: str, option: str) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: eid, ATTR_OPTION: option},
        blocking=True,
    )


async def _switch(hass: HomeAssistant, service: str, eid: str) -> None:
    await hass.services.async_call(
        SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: eid}, blocking=True
    )


async def test_edges_are_hidden_config_entities_of_the_key(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    await _setup(hass, mock_config_entry)
    registry = er.async_get(hass)
    for domain, uid in (
        ("switch", UID_MODE),
        ("select", UID_RISING),
        ("select", UID_FALLING),
    ):
        entry = registry.async_get(entity_id(hass, domain, uid))
        assert entry is not None
        assert entry.entity_category is EntityCategory.CONFIG
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert (INPUT_E1, PID_EDGE) not in mesh.gets


async def test_each_field_rewrites_the_byte_keeping_the_others(
    hass: HomeAssistant, edges_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    mesh.values[INPUT_E1, PID_EDGE] = EDGE_ON_OFF
    mode, rising, falling = await _setup(hass, mock_config_entry)
    assert mesh.gets.count((INPUT_E1, PID_EDGE)) == 1  # three entities, one read
    assert hass.states.get(mode).state == STATE_ON
    assert hass.states.get(rising).state == "on"
    assert hass.states.get(falling).state == "off"
    assert hass.states.get(rising).attributes["options"] == [
        "no_reaction",
        "on",
        "off",
        "toggle",
    ]

    await _switch(hass, SERVICE_TURN_OFF, mode)
    assert mesh.sets[-1] == (INPUT_E1, PID_EDGE, bytes([2 << 3 | 1 << 1]))
    assert hass.states.get(mode).state == STATE_OFF
    await _select(hass, rising, "toggle")
    assert mesh.sets[-1] == (INPUT_E1, PID_EDGE, bytes([2 << 3 | 3 << 1]))
    await _select(hass, falling, "no_reaction")
    assert mesh.sets[-1] == (INPUT_E1, PID_EDGE, bytes([3 << 1]))
    await _switch(hass, SERVICE_TURN_ON, mode)
    assert mesh.sets[-1] == (INPUT_E1, PID_EDGE, bytes([3 << 1 | 1]))
    assert (
        hass.states.get(mode).state,
        hass.states.get(rising).state,
        hass.states.get(falling).state,
    ) == (STATE_ON, "toggle", "no_reaction")


async def test_an_unknown_byte_is_read_before_it_is_written(
    hass: HomeAssistant, edges_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
    fast_timeouts: None,
) -> None:  # fmt: skip
    mesh.silent.add((INPUT_E1, PID_EDGE))
    mode, rising, _ = await _setup(hass, mock_config_entry)
    assert hass.states.get(mode).state == STATE_UNKNOWN
    assert hass.states.get(rising).state == STATE_UNKNOWN

    # still silent: nothing is guessed, nothing written
    with pytest.raises(HomeAssistantError) as exc:
        await _select(hass, rising, "toggle")
    assert exc.value.translation_key == "edge_detection_unknown"
    assert not any(pid == PID_EDGE for _, pid, _ in mesh.sets)

    # the input answers now: read first, then the byte with only the rising edge changed
    mesh.silent.clear()
    mesh.values[INPUT_E1, PID_EDGE] = EDGE_ON_OFF
    await _switch(hass, SERVICE_TURN_OFF, mode)
    assert mesh.sets[-1] == (INPUT_E1, PID_EDGE, bytes([2 << 3 | 1 << 1]))


# ----------------------------------------------------------------------------- the input's state (review-3 F10)


def test_an_input_alone_on_its_device_is_named_after_it() -> None:
    """Two inputs of one app device are *Input state E1* / *E2*; an input that is a device of its own is unnamed."""
    hub = blinds_hub()
    first, second = hub.devices.by_address[0x0502], hub.devices.by_address[0x0503]
    both = JungHomeInputState(hub, first)
    assert (both.translation_key, both.translation_placeholders) == (
        "input_state_key",
        {"key": "E1"},
    )
    first.gang, second.gang = (0x40,), (0x41,)
    alone = JungHomeInputState(hub, first)
    assert alone.translation_key == "input_state"
    assert alone.unique_id == UID_STATE_E1
    assert not alone.entity_registry_enabled_default


async def test_input_state_follows_what_the_input_publishes(
    hass: HomeAssistant, mesh: PropertyMesh, fake_link: FakeProxyLink,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "binary_sensor", DOMAIN, UID_STATE_E1, disabled_by=None
    )
    await _setup(hass, mock_config_entry)
    state_id = entity_id(hass, "binary_sensor", UID_STATE_E1)
    assert hass.states.get(state_id).state == STATE_UNKNOWN
    assert hass.states.get(state_id).attributes["mesh_address"] == "0502"
    # the input's event entity is named after the input, as in the app
    event = registry.async_get(entity_id(hass, "event", f"{NODE_BLIND}-0040"))
    assert event.translation_key == "input"

    fake_link.inject(INPUT_E1, 0xC000, M.generic_onoff_set(True, ack=False, tid=1))
    await hass.async_block_till_done()
    assert hass.states.get(state_id).state == STATE_ON
    # a scene recall carries no level: the state stays
    fake_link.inject(INPUT_E1, 0xFFFF, M.scene_recall(1, ack=False, tid=2))
    await hass.async_block_till_done()
    assert hass.states.get(state_id).state == STATE_ON
    fake_link.inject(INPUT_E1, 0xC000, M.generic_onoff_set(False, ack=False, tid=3))
    await hass.async_block_till_done()
    assert hass.states.get(state_id).state == STATE_OFF


async def test_input_state_is_hidden_by_default(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    await _setup(hass, mock_config_entry)
    hidden = er.async_get(hass).async_get(
        entity_id(hass, "binary_sensor", UID_STATE_E1)
    )
    assert hidden is not None
    assert hidden.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_input_state_is_restored(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "binary_sensor", DOMAIN, UID_STATE_E1, disabled_by=None
    )
    other = registry.async_get_or_create(
        "binary_sensor", DOMAIN, f"{NODE_BLIND}-0041-input_state", disabled_by=None
    )
    mock_restore_cache(
        hass,
        [State(entry.entity_id, STATE_ON), State(other.entity_id, "unavailable")],
    )
    await _setup(hass, mock_config_entry)
    assert hass.states.get(entry.entity_id).state == STATE_ON
    assert hass.states.get(other.entity_id).state == STATE_UNKNOWN


async def test_inputs_offer_device_triggers_named_e1_e2(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    await _setup(hass, mock_config_entry)
    event = er.async_get(hass).async_get(entity_id(hass, "event", f"{NODE_BLIND}-0040"))
    device = dr.async_get(hass).async_get(event.device_id)
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device.id
    )
    assert {t["type"] for t in triggers if t.get("domain") == DOMAIN} == {"e1", "e2"}
