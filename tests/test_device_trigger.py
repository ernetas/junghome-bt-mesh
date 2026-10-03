"""Device triggers: one per key and event type of a buttons device, matched on the bus event."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import voluptuous as vol
from homeassistant.components import automation
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.components.device_automation.exceptions import (
    InvalidDeviceAutomationConfig,
)
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_get_device_automations,
)

from custom_components.junghome_ble.config_entities import PROPERTY_KEY_MODE
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_METADATA_DIR,
    CONF_UNICAST,
    DOMAIN,
)
from custom_components.junghome_ble.device_trigger import (
    CONF_SUBTYPE,
    TRIGGER_SUBTYPES,
    TRIGGER_TYPES,
    async_validate_trigger_config,
    key_subtypes,
)
from custom_components.junghome_ble.event import EVENT_TYPES
from custom_components.junghome_ble.jhmesh.devices import KeyConnection

from .conftest import CDB_PATH, META_DIR, settle, setup_entry, wait_for_link
from .helpers import (
    BUTTON_CLICK,
    BUTTON_DIMMER,
    BUTTON_HOLD_START,
    BUTTON_WC,
    MESH_UUID,
    ROCKER_A,
    ROCKER_B,
    UID_BUTTON_DIMMER,
    UID_BUTTON_WC,
    UID_LIGHT_SWITCH,
    UID_ROCKER_A,
    vendor_button_event,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import FakeProxyLink

TEST_EVENT = "junghome_ble_test_fired"


def _device_id(hass: HomeAssistant, entry: MockConfigEntry, identifier: str) -> str:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, identifier), entry.entry_id
    )
    assert device is not None
    return device.id


async def _our_triggers(hass: HomeAssistant, device_id: str) -> list[tuple[str, str]]:
    """The (type, subtype) pairs this integration offers for `device_id`; other domains' triggers are dropped."""
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device_id
    )
    return [
        (t[CONF_TYPE], t[CONF_SUBTYPE])
        for t in triggers
        if t.get(CONF_DOMAIN) == DOMAIN
    ]


def _trigger(device_id: str, key: str, subtype: str) -> dict[str, str]:
    return {
        CONF_PLATFORM: "device",
        CONF_DOMAIN: DOMAIN,
        CONF_DEVICE_ID: device_id,
        CONF_TYPE: key,
        CONF_SUBTYPE: subtype,
    }


async def _automation_on(
    hass: HomeAssistant, device_id: str, key: str, subtype: str
) -> list[str]:
    """Set up one automation on a device trigger; returns the list its runs append to."""
    fired: list[str] = []
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "trigger": _trigger(device_id, key, subtype),
                    "action": {"event": TEST_EVENT},
                }
            ]
        },
    )
    await hass.async_block_till_done()
    hass.bus.async_listen(TEST_EVENT, lambda _event: fired.append("x"))
    return fired


def test_vocabulary() -> None:
    """The trigger types are the four key letters and a mini actuator's two inputs; the subtypes the event entity's
    event types, same order, then one per half of a gateway-mode rocker for each gesture that carries a side
    (PLT-06)."""
    assert TRIGGER_TYPES == ("a", "b", "c", "d", "e1", "e2")
    assert (
        *EVENT_TYPES,
        "click_up",
        "click_down",
        "double_click_up",
        "double_click_down",
        "hold_start_up",
        "hold_start_down",
        "hold_end_up",
        "hold_end_down",
    ) == TRIGGER_SUBTYPES


GATEWAY = [
    "click",
    "double_click",
    "hold_start",
    "hold_end",
    "click_up",
    "click_down",
    "double_click_up",
    "double_click_down",
    "hold_start_up",
    "hold_start_down",
    "hold_end_up",
    "hold_end_down",
]
LOAD = ["hold_start", "hold_end", "press_on", "press_off", "dim"]


async def test_triggers_listed_per_key_of_the_device(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A 1-key gang offers key A only, a 2-key gang keys A and B, each with the event types its wiring produces
    (review-4 H I-3, U4-9), in a fixed order: the WC key is linked to the gateway, the dimmer's key and rocker key A
    are wired to loads; rocker key B, wired to a load in the export too, is rewired to a scene here."""
    hub = init_integration.runtime_data
    assert [
        (b.key, b.connection.kind if b.connection else None)
        for b in hub.devices.buttons
    ] == [("A", "gateway"), ("A", "device"), ("B", "device"), ("A", "device")]
    hub.devices.by_address[ROCKER_B].connection = KeyConnection("scene", 0xFFFF)
    assert await _our_triggers(
        hass, _device_id(hass, init_integration, f"{UID_BUTTON_WC}-buttons")
    ) == [("a", subtype) for subtype in GATEWAY]
    assert await _our_triggers(
        hass, _device_id(hass, init_integration, f"{UID_BUTTON_DIMMER}-buttons")
    ) == [("a", subtype) for subtype in LOAD]
    assert await _our_triggers(
        hass, _device_id(hass, init_integration, f"{UID_ROCKER_A}-buttons")
    ) == [*(("a", subtype) for subtype in LOAD), ("b", "scene")]


async def test_trigger_subtypes_per_connection_and_key_mode(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The key mode the node reported wins over the export's connection; a room or another group is a load's
    wiring; a key whose wiring neither tells (no connection, a mode whose messages are not known) offers all 16."""
    hub = init_integration.runtime_data
    button = hub.devices.by_address[BUTTON_DIMMER]
    for kind, expected in (
        ("room", LOAD),
        ("group", LOAD),
        ("scene", ["scene"]),
        ("gateway", GATEWAY),
        ("device", LOAD),
    ):
        button.connection = KeyConnection(kind, 0xC071)
        assert list(key_subtypes(hub, button)) == expected, kind
    button.connection = None
    assert key_subtypes(hub, button) == TRIGGER_SUBTYPES
    state = hub.element_state(BUTTON_DIMMER)
    for mode, expected in (
        (6, GATEWAY),
        (2, ["scene"]),
        (0, LOAD),
        (5, LOAD),
    ):
        state.properties[PROPERTY_KEY_MODE] = bytes([mode])
        assert list(key_subtypes(hub, button)) == expected, mode
    state.properties[PROPERTY_KEY_MODE] = bytes([1])  # move: blinds, not mapped
    assert key_subtypes(hub, button) == TRIGGER_SUBTYPES
    button.connection = KeyConnection("scene", 0xFFFF)
    assert list(key_subtypes(hub, button)) == ["scene"]  # the export decides then
    state.properties[PROPERTY_KEY_MODE] = bytes([6])
    assert list(key_subtypes(hub, button)) == GATEWAY  # the node's own mode first


async def test_other_devices_offer_no_triggers(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Loads, nodes and the mesh device carry no button triggers."""
    hub = init_integration.runtime_data
    for identifier in (
        UID_LIGHT_SWITCH,
        f"node:{hub.cdb.nodes[1].uuid.lower()}",
        f"mesh:{MESH_UUID}",
    ):
        assert (
            await _our_triggers(hass, _device_id(hass, init_integration, identifier))
            == []
        )


async def test_trigger_fires_on_its_key_and_event_only(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A `click` trigger on key A runs on A's clicks, not on B's clicks nor on A's other gestures."""
    fired = await _automation_on(
        hass,
        _device_id(hass, init_integration, f"{UID_ROCKER_A}-buttons"),
        "a",
        "click",
    )

    fake_link.inject(ROCKER_A, 0xC005, vendor_button_event(1, BUTTON_CLICK))
    await hass.async_block_till_done()
    assert len(fired) == 1

    fake_link.inject(ROCKER_B, 0xC005, vendor_button_event(1, BUTTON_CLICK))
    fake_link.inject(ROCKER_A, 0xC005, vendor_button_event(2, BUTTON_HOLD_START))
    await hass.async_block_till_done()
    assert len(fired) == 1

    # the next click on A within the double-click window is a double_click, which is another subtype
    fake_link.inject(ROCKER_A, 0xC005, vendor_button_event(3, BUTTON_CLICK))
    await hass.async_block_till_done()
    assert len(fired) == 1

    init_integration.runtime_data.gestures._button_last_click.clear()  # as if the window had passed
    fake_link.inject(ROCKER_A, 0xC005, vendor_button_event(4, BUTTON_CLICK))
    await hass.async_block_till_done()
    assert len(fired) == 2


async def test_side_subtype_fires_on_its_half_only(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """PLT-06: a gateway-mode rocker's halves differ only in the event's side; `click_up` runs on the upper half's
    clicks only, where plain `click` runs on either."""
    device_id = _device_id(hass, init_integration, f"{UID_ROCKER_A}-buttons")
    fired = await _automation_on(hass, device_id, "a", "click_up")
    fake_link.inject(
        ROCKER_A, 0xC005, vendor_button_event(1, 0x00)
    )  # pushed down: the lower half
    await hass.async_block_till_done()
    assert len(fired) == 0
    init_integration.runtime_data.gestures._button_last_click.clear()  # as if the window had passed
    fake_link.inject(ROCKER_A, 0xC005, vendor_button_event(2, 0x01))  # pushed up
    await hass.async_block_till_done()
    assert len(fired) == 1


async def test_trigger_on_another_gang_ignores_this_one(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Key A of the WC button and key A of the rocker are different devices."""
    fired = await _automation_on(
        hass,
        _device_id(hass, init_integration, f"{UID_BUTTON_WC}-buttons"),
        "a",
        "click",
    )
    fake_link.inject(ROCKER_A, 0xC005, vendor_button_event(1, BUTTON_CLICK))
    await hass.async_block_till_done()
    assert fired == []


async def test_trigger_runs_with_the_keys_event_entity_disabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """Review-4 H4-2: the hub publishes the key's events, so disabling `event.<key>` leaves its device triggers
    working; a subtype the key's wiring does not list (saved before a rewiring) still validates and attaches."""
    er.async_get(hass).async_get_or_create(
        "event", DOMAIN, UID_BUTTON_WC, disabled_by=er.RegistryEntryDisabler.USER
    )
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    assert hass.states.get("event.wc_wc_mirror_button") is None
    device_id = _device_id(hass, mock_config_entry, f"{UID_BUTTON_WC}-buttons")
    assert await async_validate_trigger_config(
        hass, _trigger(device_id, "a", "press_on")
    ) == _trigger(device_id, "a", "press_on")
    fired = await _automation_on(hass, device_id, "a", "hold_start")
    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_HOLD_START))
    await hass.async_block_till_done()
    assert len(fired) == 1


async def test_invalid_trigger_is_rejected(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A key the device does not have fails validation; a subtype outside the vocabulary fails the schema."""
    device_id = _device_id(hass, init_integration, f"{UID_ROCKER_A}-buttons")
    assert await async_validate_trigger_config(
        hass, _trigger(device_id, "b", "scene")
    ) == _trigger(device_id, "b", "scene")
    with pytest.raises(InvalidDeviceAutomationConfig, match="no key C"):
        await async_validate_trigger_config(hass, _trigger(device_id, "c", "click"))
    with pytest.raises(vol.Invalid):
        await async_validate_trigger_config(hass, _trigger(device_id, "a", "tap"))
    with pytest.raises(vol.Invalid):
        await async_validate_trigger_config(hass, _trigger(device_id, "e", "click"))


async def test_validation_accepts_a_device_it_cannot_resolve(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """An unknown device id is accepted as it is, so a restart before the entry loads keeps automations valid."""
    config = _trigger("does-not-exist", "d", "click")
    assert await async_validate_trigger_config(hass, config) == config


async def test_no_triggers_for_a_foreign_or_stale_device(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A device with only another domain's identifiers, or one the export no longer has, offers nothing."""
    registry = dr.async_get(hass)
    foreign = registry.async_get_or_create(
        config_entry_id=init_integration.entry_id,
        identifiers={("other_domain", "some-device")},
    )
    assert await _our_triggers(hass, foreign.id) == []
    stale = registry.async_get_or_create(
        config_entry_id=init_integration.entry_id,
        identifiers={(DOMAIN, "00000000-0000-0000-0000-000000000000-0040-buttons")},
    )
    assert await _our_triggers(hass, stale.id) == []


async def test_no_triggers_while_the_entry_is_not_loaded(hass: HomeAssistant) -> None:
    """A buttons device of an entry that is not loaded, or of another domain's entry, resolves to no triggers."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="0000000000000000",
        data={
            CONF_CDB_PATH: CDB_PATH,
            CONF_METADATA_DIR: META_DIR,
            CONF_UNICAST: "0D00",
        },
    )
    entry.add_to_hass(hass)  # never set up
    other = MockConfigEntry(domain="other_domain", unique_id="x")
    other.add_to_hass(hass)
    registry = dr.async_get(hass)
    for owner in (entry, other):
        device = registry.async_get_or_create(
            config_entry_id=owner.entry_id,
            identifiers={(DOMAIN, f"{UID_ROCKER_A}-buttons")},
        )
        assert await _our_triggers(hass, device.id) == []
        config = _trigger(device.id, "c", "click")
        assert await async_validate_trigger_config(hass, config) == config
