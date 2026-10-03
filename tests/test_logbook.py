"""Logbook descriptions of the button-action and scene-recalled bus events."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import Event
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.junghome_ble.const import (
    DOMAIN,
    EVENT_BUTTON_ACTION,
    EVENT_SCENE_RECALLED,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.logbook import async_describe_events

from .helpers import (
    BUTTON_CLICK,
    BUTTON_HOLD_START,
    BUTTON_WC,
    ROCKER_A,
    ROCKER_B,
    vendor_button_event,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .conftest import FakeProxyLink

type Describer = Callable[[Event], dict[str, str]]


def describers(hass: HomeAssistant) -> dict[str, Describer]:
    """Register the describers the way the logbook does and return them by event type."""
    registered: dict[str, Describer] = {}

    def _capture(domain: str, event_type: str, describe: Describer) -> None:
        assert domain == DOMAIN
        registered[event_type] = describe

    async_describe_events(hass, _capture)
    return registered


def test_describes_both_events(hass: HomeAssistant) -> None:
    assert set(describers(hass)) == {EVENT_BUTTON_ACTION, EVENT_SCENE_RECALLED}


async def test_button_actions_are_named_after_the_entity(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A live key entity gives the line its friendly name; the line is filed under that entity."""
    describe = describers(hass)[EVENT_BUTTON_ACTION]
    events = async_capture_events(hass, EVENT_BUTTON_ACTION)

    fake_link.inject(BUTTON_WC, 0xC005, vendor_button_event(1, BUTTON_CLICK))
    await hass.async_block_till_done()
    assert describe(events[-1]) == {
        "name": "WC mirror button",
        "message": "clicked",
        "entity_id": "event.wc_mirror_button",
    }

    fake_link.inject(ROCKER_A, 0xC005, vendor_button_event(1, BUTTON_HOLD_START))
    await hass.async_block_till_done()
    assert describe(events[-1]) == {
        "name": "Living room rocker Button A",
        "message": "hold started",
        "entity_id": "event.living_room_rocker_button_a",
    }

    fake_link.inject(ROCKER_B, 0xFFFF, M.scene_recall(2, ack=False, tid=3))
    await hass.async_block_till_done()
    assert describe(events[-1]) == {
        "name": "Living room rocker Button B",
        "message": "recalled scene 2",
        "entity_id": "event.living_room_rocker_button_b",
    }


async def test_button_action_falls_back_to_the_device_name(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Without a live entity (a line from before a re-export), the device name and key letter stand in."""
    describe = describers(hass)[EVENT_BUTTON_ACTION]
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=init_integration.entry_id,
        identifiers={(DOMAIN, "gone-buttons")},
        name="Kitchen keys",
    )
    assert describe(
        Event(
            EVENT_BUTTON_ACTION,
            {
                "device_id": device.id,
                "entity_id": "event.no_longer_there",
                "key": "B",
                "type": "press_on",
            },
        )
    ) == {
        "name": "Kitchen keys Button B",
        "message": "pressed on / up",
        "entity_id": "event.no_longer_there",
    }
    dr.async_get(hass).async_update_device(device.id, name_by_user="Cooker")
    assert describe(
        Event(EVENT_BUTTON_ACTION, {"device_id": device.id, "key": "B", "type": "dim"})
    ) == {"name": "Cooker Button B", "message": "dimming"}
    # neither entity nor device known, and an event type from a newer version: still a readable line
    assert describe(
        Event(EVENT_BUTTON_ACTION, {"device_id": "gone", "key": "C", "type": "shake"})
    ) == {"name": "Button C", "message": "shake"}
    assert describe(Event(EVENT_BUTTON_ACTION, {"type": "scene"})) == {
        "name": "Button ?",
        "message": "recalled scene ?",
    }


async def test_scene_recall_from_a_key(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A wall key recalling a scene reads 'All off was recalled by Living room rocker', under the scene entity."""
    describe = describers(hass)[EVENT_SCENE_RECALLED]
    events = async_capture_events(hass, EVENT_SCENE_RECALLED)
    fake_link.inject(ROCKER_B, 0xFFFF, M.scene_recall(2, ack=False, tid=3))
    await hass.async_block_till_done()
    assert describe(events[-1]) == {
        "name": "All off",
        "message": "was recalled by Living room rocker",
        "entity_id": "scene.all_off",
    }


def test_scene_recall_fallbacks(hass: HomeAssistant) -> None:
    """Without a name the number stands in, without a device only 'was recalled' is said."""
    describe = describers(hass)[EVENT_SCENE_RECALLED]
    assert describe(Event(EVENT_SCENE_RECALLED, {"scene": 7, "source": "0D00"})) == {
        "name": "Scene 7",
        "message": "was recalled",
    }
    assert describe(Event(EVENT_SCENE_RECALLED, {})) == {
        "name": "Scene",
        "message": "was recalled",
    }
    assert describe(
        Event(
            EVENT_SCENE_RECALLED,
            {
                "scene": 1,
                "name": "WC off",
                "device_id": "gone",
                "entity_id": "scene.wc_off",
            },
        )
    ) == {"name": "WC off", "message": "was recalled", "entity_id": "scene.wc_off"}
