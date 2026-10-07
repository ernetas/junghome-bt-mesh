"""Logbook descriptions of the button-action and scene-recalled bus events."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.core import Event
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_load_integrations
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.junghome_ble import texts
from custom_components.junghome_ble.const import (
    DOMAIN,
    EVENT_BUTTON_ACTION,
    EVENT_PLAN,
    EVENT_SCENE_RECALLED,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.logbook import async_describe_events

from .conftest import settle, setup_entry, wait_for_link
from .helpers import (
    BUTTON_CLICK,
    BUTTON_HOLD_START,
    BUTTON_WC,
    ROCKER_A,
    ROCKER_B,
    UID_ROCKER_A,
    vendor_button_event,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .conftest import FakeProxyLink

type Describer = Callable[[Event], dict[str, str]]


async def load_translations(hass: HomeAssistant, language: str = "en") -> None:
    """Cache the integration's translations for `language`, as Home Assistant does when it sets the integration up."""
    hass.config.language = language
    await async_load_integrations(hass, {DOMAIN})


def describers(hass: HomeAssistant) -> dict[str, Describer]:
    """Register the describers the way the logbook does and return them by event type."""
    registered: dict[str, Describer] = {}

    def _capture(domain: str, event_type: str, describe: Describer) -> None:
        assert domain == DOMAIN
        registered[event_type] = describe

    async_describe_events(hass, _capture)
    return registered


def test_describes_every_event(hass: HomeAssistant) -> None:
    assert set(describers(hass)) == {
        EVENT_BUTTON_ACTION,
        EVENT_SCENE_RECALLED,
        EVENT_PLAN,
    }


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
        "entity_id": "event.wc_wc_mirror_button",
    }

    fake_link.inject(ROCKER_A, 0xC005, vendor_button_event(1, BUTTON_HOLD_START))
    await hass.async_block_till_done()
    assert describe(events[-1]) == {
        "name": "Living room rocker Button A",
        "message": "hold started",
        "entity_id": "event.living_room_living_room_rocker_button_a",
    }

    fake_link.inject(ROCKER_B, 0xFFFF, M.scene_recall(2, ack=False, tid=3))
    await hass.async_block_till_done()
    assert describe(events[-1]) == {
        "name": "Living room rocker Button B",
        "message": "recalled scene 2",
        "entity_id": "event.living_room_living_room_rocker_button_b",
    }


async def test_a_key_with_its_event_entity_disabled_is_still_described(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """Review-4 H4-2: the hub publishes the key's event without `entity_id`; the line names the device and key."""
    er.async_get(hass).async_get_or_create(
        "event", DOMAIN, UID_ROCKER_A, disabled_by=er.RegistryEntryDisabler.USER
    )
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    describe = describers(hass)[EVENT_BUTTON_ACTION]
    events = async_capture_events(hass, EVENT_BUTTON_ACTION)
    fake_link.inject(ROCKER_A, 0xC005, vendor_button_event(1, BUTTON_CLICK))
    await hass.async_block_till_done()
    assert "entity_id" not in events[-1].data
    assert describe(events[-1]) == {
        "name": "Living room rocker Button A",
        "message": "clicked",
    }


async def test_a_hold_ended_without_its_release_says_why(hass: HomeAssistant) -> None:
    """Decision M11: a `hold_end` the hub made up (DIM_HOLD_MAX, a lost link, a stop) carries its reason."""
    await load_translations(hass)
    describe = describers(hass)[EVENT_BUTTON_ACTION]
    for reason, said in (
        ("timeout", "no release heard in time"),
        ("link_lost", "link lost"),
        ("stopped", "integration stopped"),
        ("newer", "newer"),
    ):
        event = Event(
            EVENT_BUTTON_ACTION, {"key": "A", "type": "hold_end", "reason": reason}
        )
        assert describe(event) == {
            "name": "Button A",
            "message": f"hold released ({said})",
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


async def test_scene_recall_fallbacks(hass: HomeAssistant) -> None:
    """Without a name the number stands in, without a device only 'was recalled' is said."""
    await load_translations(hass)
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


async def test_plan_lines_are_translated(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Review-4 W I7: a plan's line is an `exceptions` text of the integration, in the server's language (English
    when it has none); a line nobody wrote a text for shows its key."""
    describe = describers(hass)[EVENT_PLAN]
    placeholders = {
        "action": "junghome_ble.set_threshold",
        "applied": "2",
        "total": "2",
        "messages": "2",
    }
    event = Event(
        EVENT_PLAN,
        {
            "name": "JUNG HOME mesh test",
            "message": "plan_finished",
            "placeholders": placeholders,
        },
    )
    assert describe(event) == {
        "name": "JUNG HOME mesh test",
        "message": "junghome_ble.set_threshold finished; 2 messages",
    }
    hass.config.language = "fr"  # no French text: English
    assert describe(event)["message"] == (
        "junghome_ble.set_threshold finished; 2 messages"
    )
    await load_translations(hass, "de")
    assert describe(event)["message"] == (
        "junghome_ble.set_threshold abgeschlossen; 2 Nachrichten"
    )
    assert describe(Event(EVENT_PLAN, {"message": "plan_unknown"})) == {
        "name": "junghome_ble",
        "message": "plan_unknown",
    }


async def test_lines_follow_the_server_language(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Review-4 U4-16: with the server in German every line is German — the key, what it did, a hold's end, a
    scene recalled by a key or without one."""
    await load_translations(hass, "de")
    button = describers(hass)[EVENT_BUTTON_ACTION]
    scene = describers(hass)[EVENT_SCENE_RECALLED]
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=init_integration.entry_id,
        identifiers={(DOMAIN, "kitchen-buttons")},
        name="Küche",
    )
    assert button(
        Event(
            EVENT_BUTTON_ACTION, {"device_id": device.id, "key": "A", "type": "click"}
        )
    ) == {"name": "Küche Taste A", "message": "geklickt"}
    assert button(
        Event(
            EVENT_BUTTON_ACTION, {"key": "B", "type": "hold_end", "reason": "link_lost"}
        )
    ) == {"name": "Taste B", "message": "Halten beendet (Verbindung verloren)"}
    assert button(
        Event(EVENT_BUTTON_ACTION, {"key": "C", "type": "scene", "scene": 4})
    ) == {
        "name": "Taste C",
        "message": "hat Szene 4 abgerufen",
    }
    assert scene(
        Event(
            EVENT_SCENE_RECALLED,
            {"scene": 4, "name": "Alles aus", "device_id": device.id},
        )
    ) == {"name": "Alles aus", "message": "wurde von Küche abgerufen"}
    assert scene(Event(EVENT_SCENE_RECALLED, {"scene": 7})) == {
        "name": "Szene 7",
        "message": "wurde abgerufen",
    }
    assert scene(Event(EVENT_SCENE_RECALLED, {})) == {
        "name": "Szene",
        "message": "wurde abgerufen",
    }


def test_without_cached_translations_the_line_shows_what_it_has(
    hass: HomeAssistant,
) -> None:
    """Not reachable in Home Assistant (it caches the translations before the logbook asks): the bare values."""
    button = describers(hass)[EVENT_BUTTON_ACTION]
    scene = describers(hass)[EVENT_SCENE_RECALLED]
    with patch(f"{texts.__name__}.async_get_cached_translations", return_value={}):
        assert button(
            Event(EVENT_BUTTON_ACTION, {"key": "A", "type": "scene", "scene": 2})
        ) == {"name": "A", "message": "2"}
        assert scene(Event(EVENT_SCENE_RECALLED, {})) == {"name": "", "message": ""}


def test_a_text_the_placeholders_do_not_fit_is_passed_over(hass: HomeAssistant) -> None:
    """A row an older version stored, of a text that gained a placeholder since: the server's language's text does
    not fit, so the English one is tried, then none — never an exception for every such row of the logbook."""
    path = "component.junghome_ble.exceptions.x.message"
    cached = {
        "de": {path: "{neu} von {alt}"},
        "en": {path: "{old} only"},
    }
    hass.config.language = "de"
    with patch(
        f"{texts.__name__}.async_get_cached_translations",
        side_effect=lambda _hass, language, _category, _domain: cached[language],
    ):
        assert (
            texts.cached_text(hass, "exceptions", "x.message", {"old": "1"}) == "1 only"
        )
        assert texts.cached_text(hass, "exceptions", "x.message", {}) is None
        cached["en"][path] = "{0} {bad"
        assert texts.cached_text(hass, "exceptions", "x.message", {"old": "1"}) is None
