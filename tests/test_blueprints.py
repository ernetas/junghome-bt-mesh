"""The automation blueprints of `blueprints/automation/junghome_ble/` (review-4 U4-3).

Each file loads with Home Assistant's YAML loader (`!input`), validates as an automation blueprint of the pinned
release, renders with inputs into an automation that validates, and does what its description says. The integration
is not set up: the blueprints listen to the `junghome_ble_button_action` bus event and to ordinary entities, so the
tests fire that event and set states themselves, and mock the actions the automations call. Time runs on the frozen
clock (`freezer` + `async_fire_time_changed`), so a hold's dimming loop and an off delay are stepped, not waited for.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant.components.automation.config import (
    AUTOMATION_BLUEPRINT_SCHEMA,
    ValidationStatus,
    async_validate_config_item,
)
from homeassistant.components.blueprint import Blueprint
from homeassistant.const import (
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    CONF_TYPE,
    EVENT_CALL_SERVICE,
)
from homeassistant.core import Event, callback
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.junghome_ble.const import ATTR_KEY, DOMAIN, EVENT_BUTTON_ACTION
from custom_components.junghome_ble.event import EVENT_TYPES

from .conftest import SETTLE_MAX_TURNS, _loop_idle

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

ROOT = Path(__file__).resolve().parent.parent
FOLDER = Path("blueprints/automation/junghome_ble")
BLUEPRINTS = sorted(p.name for p in (ROOT / FOLDER).glob("*.yaml"))
BUTTON_BLUEPRINTS = (
    "rocker_dim_jung_light.yaml",
    "rocker_light_control.yaml",
    "rocker_scene_selector.yaml",
)
# where the public repository serves a file raw: the blueprint's `source_url` and the import links of the guide
RAW = "https://raw.githubusercontent.com/ernetas/junghome-bt-mesh/main/"
GUIDE = ROOT / "docs/user/buttons-and-automations.md"
IMPORT_LINK = re.compile(
    r"\((https://my\.home-assistant\.io/redirect/blueprint_import/\?[^)\s]+)\)"
)

KEY = "event.hall_buttons_button_a"
OTHER_KEY = "event.hall_buttons_button_b"
LIGHTS = ["light.hall", "light.stairs"]
JUNG_LIGHT = "light.desk_dimmer"
AUTOMATION = "automation.blueprint_test"


def test_the_folder_holds_the_blueprints() -> None:
    assert BLUEPRINTS == [
        "appliance_finished.yaml",
        "device_offline_notify.yaml",
        "presence_lighting.yaml",
        "rocker_dim_jung_light.yaml",
        "rocker_light_control.yaml",
        "rocker_scene_selector.yaml",
    ]


@pytest.mark.parametrize("name", BLUEPRINTS)
def test_every_blueprint_loads_and_validates(name: str) -> None:
    """HA's loader and blueprint schema accept it, the pinned release supports it, it says where it comes from."""
    data = load_yaml_dict(ROOT / FOLDER / name)
    blueprint = Blueprint(
        data,
        path=f"junghome_ble/{name}",
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )
    assert blueprint.validate() is None
    assert blueprint.metadata["source_url"] == RAW + (FOLDER / name).as_posix()
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert blueprint.metadata["homeassistant"]["min_version"] == hacs["homeassistant"]
    assert blueprint.metadata["description"].strip()
    for key, spec in blueprint.inputs.items():
        assert spec["name"], key


@pytest.mark.parametrize("name", BUTTON_BLUEPRINTS)
def test_button_blueprints_listen_to_the_integrations_event(name: str) -> None:
    """They match the bus event the hub fires, on event types the keys report, and say what a key needs."""
    data = load_yaml_dict(ROOT / FOLDER / name)
    types = set()
    for trigger in data["triggers"]:
        assert trigger["trigger"] == "event"
        assert trigger["event_type"] == EVENT_BUTTON_ACTION
        assert set(trigger["event_data"]) == {ATTR_ENTITY_ID, CONF_TYPE}
        types.add(trigger["event_data"][CONF_TYPE])
    assert types <= set(EVENT_TYPES)
    # a `dim` arrives many times during a hold and would end the dimming loop
    assert "dim" not in types
    description = data["blueprint"]["description"]
    for needed in (
        "linked to the JUNG HOME Gateway",
        "#a-key-that-only-talks-to-home-assistant",
        "still",  # a key wired to a JUNG load still drives it
        "Unverified on air",
    ):
        assert needed in description, needed


def test_the_guide_imports_every_blueprint_from_the_public_repository() -> None:
    """One my.home-assistant.io import link per blueprint, to its raw file on `main`, and the manual folder."""
    text = GUIDE.read_text(encoding="utf-8")
    urls = {
        parse_qs(urlparse(link).query)["blueprint_url"][0]
        for link in IMPORT_LINK.findall(text)
    }
    assert urls == {RAW + (FOLDER / name).as_posix() for name in BLUEPRINTS}
    assert "<config>/blueprints/automation/junghome_ble/" in text


# ----------------------------------------------------------------------------- running them


def _copy(name: str, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / FOLDER / name, folder / name)


async def use_blueprint(hass: HomeAssistant, name: str, inputs: dict[str, Any]) -> None:
    """Install `name` as the manual install does, check its automation validates, and set it up."""
    await hass.async_add_executor_job(
        _copy, name, Path(hass.config.path("blueprints/automation/junghome_ble"))
    )
    config = {
        "id": "blueprint_test",
        "alias": "Blueprint test",
        "use_blueprint": {"path": f"junghome_ble/{name}", "input": inputs},
    }
    assert await async_setup_component(hass, "blueprint", {})
    validated = await async_validate_config_item(hass, "blueprint_test", config)
    assert validated is not None
    assert validated.validation_status == ValidationStatus.OK
    assert await async_setup_component(hass, "automation", {"automation": [config]})
    await hass.async_block_till_done()
    state = hass.states.get(AUTOMATION)
    assert state is not None
    assert state.state == "on"


async def run_ready() -> None:
    """Run what can run without time passing.

    Not `hass.async_block_till_done()`: an automation's run is one of Home Assistant's tracked tasks, so it would
    wait for a delay or a `for` that only the frozen clock can end.
    """
    loop = asyncio.get_running_loop()
    for _ in range(SETTLE_MAX_TURNS):
        await asyncio.sleep(0)
        if _loop_idle(loop):
            return


async def key_event(
    hass: HomeAssistant, event_type: str, entity_id: str = KEY, **attrs: Any
) -> None:
    """Fire the bus event the hub fires for a key (`event.publish_button_event`)."""
    hass.bus.async_fire(
        EVENT_BUTTON_ACTION,
        {
            ATTR_DEVICE_ID: "0123456789abcdef0123456789abcdef",
            ATTR_ENTITY_ID: entity_id,
            ATTR_KEY: "A",
            CONF_TYPE: event_type,
            "counter": 1,
            **attrs,
        },
    )
    await run_ready()


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float, times: int = 1
) -> None:
    """Let `seconds` pass `times` times, running what falls due each time."""
    for _ in range(times):
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass)
        await run_ready()


Calls = list[tuple[str, dict[str, Any]]]


def record(hass: HomeAssistant, domain: str, *names: str) -> Calls:
    """Mock `domain`'s actions `names`; return (action, data) of every call of them, in order, target included."""
    for name in names:
        async_mock_service(hass, domain, name)
    seen: Calls = []

    @callback
    def called(event: Event) -> None:
        if event.data["domain"] == domain:
            seen.append((event.data["service"], dict(event.data["service_data"])))

    hass.bus.async_listen(EVENT_CALL_SERVICE, called)
    return seen


@pytest.fixture
def light_calls(hass: HomeAssistant) -> Calls:
    """Every light action the automation calls."""
    return record(hass, "light", "turn_on", "turn_off", "toggle")


# ----------------------------------------------------------------------------- rocker_light_control


@pytest.fixture
async def light_control(hass: HomeAssistant, light_calls: Calls) -> Calls:
    await use_blueprint(
        hass,
        "rocker_light_control.yaml",
        {
            "key": KEY,
            "lights": {ATTR_ENTITY_ID: LIGHTS},
            "double_click_action": [
                {"action": "test.double_click", "data": {"half": "{{ half }}"}}
            ],
        },
    )
    return light_calls


async def test_light_control_clicks_switch(
    hass: HomeAssistant, light_control: Calls
) -> None:
    """Upper half on, lower half off, a single key toggles; a key wired to a load presses on and off."""
    await key_event(hass, "click", side="up")
    await key_event(hass, "click", side="down")
    await key_event(hass, "click")
    await key_event(hass, "press_on", target="C001")
    await key_event(hass, "press_off", target="C001")
    target = {ATTR_ENTITY_ID: LIGHTS}
    assert light_control == [
        ("turn_on", target),
        ("turn_off", target),
        ("toggle", target),
        ("turn_on", target),
        ("turn_off", target),
    ]


async def test_light_control_ignores_other_keys_and_events(
    hass: HomeAssistant, light_control: Calls
) -> None:
    await key_event(hass, "click", OTHER_KEY, side="up")
    await key_event(hass, "scene", scene=3)
    await key_event(hass, "dim", target="C001", raw="00")
    await key_event(hass, "hold_end", side="up")  # no hold running: nothing to stop
    assert light_control == []


async def test_light_control_double_click_runs_its_action(
    hass: HomeAssistant, light_control: Calls
) -> None:
    double = async_mock_service(hass, "test", "double_click")
    await key_event(hass, "double_click", side="down")
    await key_event(hass, "double_click")
    assert [call.data["half"] for call in double] == ["down", ""]
    assert light_control == []


async def test_light_control_hold_dims_until_released(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    light_control: Calls,
) -> None:
    """A step at once and one every 0.35 s; a `dim` meanwhile keeps the loop, `hold_end` stops it."""
    await key_event(hass, "hold_start", side="up")
    await tick(hass, freezer, 0.35, times=3)
    await key_event(hass, "dim", target="C001", raw="00")
    await tick(hass, freezer, 0.35)
    await key_event(hass, "hold_end", side="up")
    await tick(hass, freezer, 0.35, times=5)
    step_up = ("turn_on", {ATTR_ENTITY_ID: LIGHTS, "brightness_step_pct": 10})
    assert light_control == [step_up] * 5


async def test_light_control_hold_down_and_a_derived_hold(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    light_control: Calls,
) -> None:
    """The lower half dims down; a key wired to a dimmer holds with `direction`; a new event ends the loop."""
    await key_event(hass, "hold_start", side="down")
    await tick(hass, freezer, 0.35)
    await key_event(hass, "hold_start", target="C001", direction="up")
    await key_event(hass, "click", side="up")
    await tick(hass, freezer, 0.35, times=3)
    assert [data.get("brightness_step_pct") for _, data in light_control] == [
        -10,
        -10,
        10,
        None,  # the click: switched on, and the loop is over
    ]


async def test_light_control_hold_stops_after_30_steps(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    light_control: Calls,
) -> None:
    """A release never heard (and no `hold_end` either) still ends the dimming."""
    await key_event(hass, "hold_start", side="up")
    await tick(hass, freezer, 0.35, times=40)
    assert len(light_control) == 30


async def test_light_control_single_key_hold_does_not_dim(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    light_control: Calls,
) -> None:
    await key_event(hass, "hold_start")
    await tick(hass, freezer, 0.35, times=3)
    assert light_control == []


# ----------------------------------------------------------------------------- rocker_dim_jung_light


@pytest.fixture
async def dim_calls(hass: HomeAssistant) -> Calls:
    calls = record(hass, DOMAIN, "start_dim", "stop_dim")
    await use_blueprint(
        hass, "rocker_dim_jung_light.yaml", {"key": KEY, "light": JUNG_LIGHT}
    )
    return calls


async def test_dim_jung_light_starts_and_stops(
    hass: HomeAssistant, dim_calls: Calls
) -> None:
    """`hold_start` starts dimming by half (or a derived hold's direction), `hold_end` stops — a timed-out one too."""
    await key_event(hass, "hold_start", side="up")
    await key_event(hass, "hold_end", side="up")
    await key_event(hass, "hold_start", side="down")
    await key_event(hass, "hold_end", side="down", reason="timeout")
    await key_event(hass, "hold_start", target="C001", direction="up")
    await key_event(hass, "click", side="up")  # not a hold: nothing
    light = {ATTR_ENTITY_ID: [JUNG_LIGHT]}
    assert dim_calls == [
        ("start_dim", {**light, "direction": "up", "speed": 20}),
        ("stop_dim", light),
        ("start_dim", {**light, "direction": "down", "speed": 20}),
        ("stop_dim", light),
        ("start_dim", {**light, "direction": "up", "speed": 20}),
    ]


async def test_dim_jung_light_single_key_picks_the_direction(
    hass: HomeAssistant, dim_calls: Calls
) -> None:
    """A key without halves dims up from off or below half brightness, down from above."""
    hass.states.async_set(JUNG_LIGHT, "off")
    await key_event(hass, "hold_start")
    hass.states.async_set(JUNG_LIGHT, "on", {"brightness": 100})
    await key_event(hass, "hold_start")
    hass.states.async_set(JUNG_LIGHT, "on", {"brightness": 200})
    await key_event(hass, "hold_start")
    assert [data["direction"] for _, data in dim_calls] == ["up", "up", "down"]


# ----------------------------------------------------------------------------- rocker_scene_selector


GESTURES = (
    "click_up",
    "click_down",
    "double_click_up",
    "double_click_down",
    "hold_up",
    "hold_down",
)


async def test_scene_selector_runs_one_action_per_gesture(hass: HomeAssistant) -> None:
    ran = async_mock_service(hass, "test", "gesture")
    await use_blueprint(
        hass,
        "rocker_scene_selector.yaml",
        {"key": KEY}
        | {
            gesture: [{"action": "test.gesture", "data": {"gesture": gesture}}]
            for gesture in GESTURES
        },
    )
    await key_event(hass, "click", side="up")
    await key_event(hass, "click", side="down")
    await key_event(hass, "double_click", side="up")
    await key_event(hass, "double_click", side="down")
    await key_event(hass, "hold_start", side="up")
    await key_event(hass, "hold_end", side="up")  # not a gesture of its own
    await key_event(hass, "hold_start", side="down")
    await key_event(hass, "click")  # a single key: the upper half
    await key_event(hass, "press_on", target="C001")
    await key_event(hass, "press_off", target="C001")
    await key_event(hass, "hold_start", target="C001", direction="down")
    await key_event(hass, "click", OTHER_KEY, side="up")
    assert [call.data["gesture"] for call in ran] == [
        *GESTURES,
        "click_up",
        "click_up",
        "click_down",
        "hold_down",
    ]


async def test_scene_selector_leaves_empty_gestures_alone(hass: HomeAssistant) -> None:
    ran = async_mock_service(hass, "test", "gesture")
    await use_blueprint(
        hass,
        "rocker_scene_selector.yaml",
        {"key": KEY, "hold_down": [{"action": "test.gesture"}]},
    )
    for half in ("up", "down"):
        await key_event(hass, "click", side=half)
        await key_event(hass, "double_click", side=half)
    await key_event(hass, "hold_start", side="up")
    assert ran == []
    await key_event(hass, "hold_start", side="down")
    assert len(ran) == 1


# ----------------------------------------------------------------------------- presence_lighting


MOTION = "binary_sensor.hall_motion"
LUX = "sensor.hall_illuminance"


async def test_presence_switches_on_and_off_after_the_delay(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    light_calls: Calls,
) -> None:
    hass.states.async_set(MOTION, "off")
    await use_blueprint(
        hass,
        "presence_lighting.yaml",
        {"sensor": MOTION, "lights": {ATTR_ENTITY_ID: LIGHTS}, "off_delay": 60},
    )
    target = {ATTR_ENTITY_ID: LIGHTS}
    hass.states.async_set(MOTION, "on")
    await run_ready()
    assert light_calls == [("turn_on", target)]
    hass.states.async_set(MOTION, "off")
    await tick(hass, freezer, 30)
    hass.states.async_set(
        MOTION, "on"
    )  # back before the delay ran out: the wait starts again
    await run_ready()
    hass.states.async_set(MOTION, "off")
    await tick(hass, freezer, 40)
    assert light_calls == [("turn_on", target)] * 2
    await tick(hass, freezer, 25)
    assert light_calls == [("turn_on", target)] * 2 + [("turn_off", target)]


async def test_presence_only_when_dark(hass: HomeAssistant, light_calls: Calls) -> None:
    hass.states.async_set(MOTION, "off")
    hass.states.async_set(LUX, "120")
    await use_blueprint(
        hass,
        "presence_lighting.yaml",
        {
            "sensor": MOTION,
            "lights": {ATTR_ENTITY_ID: LIGHTS},
            "illuminance_sensor": LUX,
            "illuminance_threshold": 50,
        },
    )
    for lux in ("120", "20", "unavailable"):
        hass.states.async_set(LUX, lux)
        hass.states.async_set(MOTION, "on")
        await run_ready()
        hass.states.async_set(
            MOTION, "unavailable"
        )  # neither a detection nor a clearing
        await run_ready()
    assert light_calls == [("turn_on", {ATTR_ENTITY_ID: LIGHTS})] * 2


# ----------------------------------------------------------------------------- appliance_finished


POWER = "sensor.washing_machine_power"


async def set_power(hass: HomeAssistant, watts: float) -> None:
    hass.states.async_set(POWER, str(watts), {"friendly_name": "Washing machine Power"})
    await run_ready()


async def test_appliance_finished_notifies_once_per_run(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Running above 10 W, finished after 3 minutes below 5 W; a pause shorter than that is not the end."""
    notified = async_mock_service(hass, "persistent_notification", "create")
    await set_power(hass, 0.5)
    await use_blueprint(hass, "appliance_finished.yaml", {"power_sensor": POWER})
    await set_power(hass, 7)  # standby above idle, below running: nothing starts
    await set_power(hass, 1)
    await tick(hass, freezer, 240)
    assert notified == []
    await set_power(hass, 1800)
    await set_power(hass, 2)
    await tick(hass, freezer, 60)
    await set_power(hass, 400)  # the pause was too short; the same run goes on
    await set_power(hass, 2)
    await tick(hass, freezer, 170)
    assert notified == []
    await tick(hass, freezer, 20)
    assert len(notified) == 1
    assert notified[0].data == {
        "title": "Appliance finished",
        "message": "Washing machine Power: the appliance has finished.",
    }
    await tick(hass, freezer, 600)
    assert len(notified) == 1


async def test_appliance_finished_runs_the_chosen_action(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    ran = async_mock_service(hass, "test", "finished")
    await set_power(hass, 0)
    await use_blueprint(
        hass,
        "appliance_finished.yaml",
        {
            "power_sensor": POWER,
            "running_threshold": 100,
            "idle_threshold": 20,
            "finished_after": 1,
            "notify_action": [{"action": "test.finished"}],
        },
    )
    await set_power(hass, 50)  # below this running threshold
    await set_power(hass, 10)
    await tick(hass, freezer, 120)
    assert ran == []
    await set_power(hass, 150)
    await set_power(hass, 10)
    await tick(hass, freezer, 61)
    assert len(ran) == 1


# ----------------------------------------------------------------------------- device_offline_notify


UNREACHABLE = "sensor.jung_home_mesh_unreachable_devices"


async def set_unreachable(hass: HomeAssistant, *names: str) -> None:
    """Write the *Unreachable devices* sensor as the integration does: the count, and the names in `devices`."""
    hass.states.async_set(UNREACHABLE, str(len(names)), {"devices": list(names)})
    await run_ready()


async def lose_link(hass: HomeAssistant) -> None:
    """The sensor without a link: unavailable, and without its attributes (HA writes none for it)."""
    hass.states.async_set(UNREACHABLE, "unavailable")
    await run_ready()


async def test_device_offline_notifies_after_the_delay(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Five minutes by default; a dropout shorter than that stays quiet; no word of the return unless asked."""
    notified = async_mock_service(hass, "persistent_notification", "create")
    await set_unreachable(hass)
    await use_blueprint(
        hass, "device_offline_notify.yaml", {"unreachable_sensor": UNREACHABLE}
    )
    await set_unreachable(hass, "Hall light")
    await tick(hass, freezer, 120)
    await set_unreachable(hass)  # back within the delay
    await tick(hass, freezer, 600)
    assert notified == []
    await set_unreachable(hass, "Hall light")
    await tick(hass, freezer, 240)
    await set_unreachable(hass, "Hall light", "Desk dimmer")
    await tick(hass, freezer, 61)
    assert [call.data for call in notified] == [
        {"title": "JUNG HOME device offline", "message": "Not answering: Hall light."}
    ]
    await tick(hass, freezer, 240)
    assert notified[1].data["message"] == "Not answering: Desk dimmer."
    await set_unreachable(hass)
    await tick(hass, freezer, 600)
    assert len(notified) == 2


async def test_device_offline_reports_the_return_of_each_device(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Devices that went together are reported together, and each back on its own; a link loss is no return."""
    offline = async_mock_service(hass, "test", "offline")
    back = async_mock_service(hass, "test", "back")
    await set_unreachable(hass)
    await use_blueprint(
        hass,
        "device_offline_notify.yaml",
        {
            "unreachable_sensor": UNREACHABLE,
            "offline_for": 1,
            "notify_back": True,
            "offline_action": [
                {"action": "test.offline", "data": {"devices": "{{ devices }}"}}
            ],
            "back_action": [
                {"action": "test.back", "data": {"devices": "{{ devices }}"}}
            ],
        },
    )
    await set_unreachable(hass, "Hall light", "Desk dimmer")
    await tick(hass, freezer, 61)
    assert [call.data["devices"] for call in offline] == [["Hall light", "Desk dimmer"]]
    await lose_link(hass)  # the list is gone, but nobody came back
    await set_unreachable(hass, "Hall light", "Desk dimmer")  # nor did anyone leave
    await tick(hass, freezer, 120)
    assert back == []
    assert len(offline) == 1
    await set_unreachable(hass, "Desk dimmer")
    assert [call.data["devices"] for call in back] == [["Hall light"]]
    await set_unreachable(hass)
    assert [call.data["devices"] for call in back] == [["Hall light"], ["Desk dimmer"]]


async def test_device_offline_waits_for_the_link_to_tell(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Without a link when the time is up, the list read once the link is back decides."""
    offline = async_mock_service(hass, "test", "offline")
    await set_unreachable(hass)
    await use_blueprint(
        hass,
        "device_offline_notify.yaml",
        {
            "unreachable_sensor": UNREACHABLE,
            "offline_for": 1,
            "offline_action": [
                {"action": "test.offline", "data": {"devices": "{{ devices }}"}}
            ],
        },
    )
    await set_unreachable(hass, "Hall light")
    await set_unreachable(hass, "Hall light", "Desk dimmer")
    await lose_link(hass)
    await tick(hass, freezer, 120)
    assert offline == []
    await set_unreachable(hass, "Desk dimmer")  # the light answered meanwhile
    assert [call.data["devices"] for call in offline] == [["Desk dimmer"]]
