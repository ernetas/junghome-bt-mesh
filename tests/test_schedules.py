"""Schedules on the loads: the JH Scheduler slots (`schedules.py`), their sensor and the five schedule actions.

The fake proxy link serves every scheduler-hosting element's slots (`conftest.FakeScheduler`), so the actions
run through the real proxy client: Get / Set, the Status matched by its header byte, a silent Set read back.
"""

from __future__ import annotations

import json
import re
from datetime import time as dt_time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.const import STATE_UNKNOWN, EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.junghome_ble import const, services
from custom_components.junghome_ble import schedules as S
from custom_components.junghome_ble.config_entities import property_reader
from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.cover import closedness_to_level
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import vendor_models as V
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.devices import (
    Button,
    Metadata,
    build_devices,
)
from custom_components.junghome_ble.jhmesh.pdu import ALL_NODES

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
    BUTTON_WC,
    LIGHT_CTL,
    LIGHT_DIMMER,
    LIGHT_SWITCH,
    OUR_ADDRESS,
    SOCKET,
    UID_LIGHT_CTL,
    UID_LIGHT_DIMMER,
    UID_SOCKET,
    entity_id,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

mesh = ph.mesh

UID_SCHEDULES_DIMMER = f"{UID_LIGHT_DIMMER}-schedules"
ALL_DAYS = list(V.DAYS)
RESERVED = 1  # a schedule type the app never writes
STRINGS = json.loads(
    (Path(S.__file__).parent / "strings.json").read_text(encoding="utf-8")
)


@pytest.fixture
def fast_scheduler() -> Generator[None]:
    """An unanswered JH Scheduler Get / Set gives up after milliseconds."""
    with (
        patch.object(const, "PROPERTY_READ_TIMEOUT", 0.01),
        patch.object(const, "PROPERTY_WRITE_TIMEOUT", 0.01),
    ):
        yield


async def call(
    hass: HomeAssistant, service: str, data: dict[str, Any], *, response: bool = False
) -> Any:
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=response
    )


def light(hass: HomeAssistant, uid: str = UID_LIGHT_DIMMER) -> str:
    return entity_id(hass, "light", uid)


# --------------------------------------------------------------------------- the action of a slot


@pytest.mark.parametrize(
    ("kind", "data", "action", "fields"),
    [
        (
            "switch",
            {"action": "on"},
            V.Action(V.ACTION_SWITCH, on=True),
            {"action": "on"},
        ),
        (
            "socket",
            {"action": "off"},
            V.Action(V.ACTION_SWITCH, on=False),
            {"action": "off"},
        ),
        (
            "dimmer",
            {"action": "on"},
            V.Action(V.ACTION_LIGHTNESS, lightness=V.LIGHTNESS_MAX),
            {"action": "on", "brightness_pct": 100},
        ),
        (
            "dimmer",
            {"brightness_pct": 40},
            V.Action(V.ACTION_LIGHTNESS, lightness=round(0.4 * V.LIGHTNESS_MAX)),
            {"action": "on", "brightness_pct": 40},
        ),
        (
            "dimmer",
            {"action": "off"},
            V.Action(V.ACTION_LIGHTNESS, lightness=0),
            {"action": "off"},
        ),
        (
            "ctl",
            {"brightness_pct": 50, "color_temp_kelvin": 2740},
            V.Action(
                V.ACTION_LIGHTNESS_CT,
                lightness=round(0.5 * V.LIGHTNESS_MAX),
                temperature_k=2700,
            ),
            {"action": "on", "brightness_pct": 50, "color_temp_kelvin": 2700},
        ),
        (
            "ctl",
            {"action": "off", "color_temp_kelvin": 4000},
            V.Action(V.ACTION_LIGHTNESS_CT, lightness=0, temperature_k=4000),
            {"action": "off", "color_temp_kelvin": 4000},
        ),
        (
            "ctl",
            {"action": "on"},
            V.Action(V.ACTION_LIGHTNESS, lightness=V.LIGHTNESS_MAX),
            {"action": "on", "brightness_pct": 100},
        ),
        (
            "blind",
            {"position": 100, "tilt_position": 0},
            V.Action(
                V.ACTION_BLINDS,
                blind=closedness_to_level(0),
                slat=closedness_to_level(100),
            ),
            {"position": 100, "tilt_position": 0},
        ),
        (
            "blind",
            {"position": 30},
            V.Action(
                V.ACTION_BLINDS,
                blind=closedness_to_level(70),
                slat=closedness_to_level(70),
            ),
            {"position": 30, "tilt_position": 30},
        ),
        (
            "thermostat",
            {"temperature": 21.5},
            V.Action(V.ACTION_TEMPERATURE, temperature_c=21.5),
            {"temperature": 21.5},
        ),
    ],
)
def test_schedule_action_per_kind(
    kind: str, data: dict[str, Any], action: V.Action, fields: dict[str, Any]
) -> None:
    """Each kind of load gets the app's action; `action_fields` reads it back in the call's terms."""
    assert S.schedule_action(kind, data) == action
    assert S.action_fields(action) == fields
    # what the element answers with decodes to the same action, so a write confirms
    assert V.decode_action(action.encode()) == action


def test_thermostat_temperature_is_what_the_element_stores() -> None:
    """The target goes on air in centi-degrees: the action compares equal to the one read back."""
    action = S.schedule_action("thermostat", {"temperature": 20.333})
    assert action.temperature_c == 20.33
    assert V.decode_action(action.encode()) == action


@pytest.mark.parametrize(
    ("kind", "data", "error", "key"),
    [
        (
            "socket",
            {"action": "on", "brightness_pct": 50},
            "brightness_pct does not",
            "not_applicable",
        ),
        (
            "dimmer",
            {"position": 10},
            "position does not apply to a dimmer load",
            "not_applicable",
        ),
        ("thermostat", {}, "needs a temperature", "needs_temperature"),
        ("blind", {"tilt_position": 10}, "needs a position", "needs_position"),
        ("switch", {}, "needs an action", "needs_action"),
        (
            "dimmer",
            {"action": "off", "brightness_pct": 20},
            "goes with the action on",
            "brightness_off",
        ),
    ],
)
def test_schedule_action_refuses_what_does_not_fit(
    kind: str, data: dict[str, Any], error: str, key: str
) -> None:
    """The English message for the log; a translation key whose message takes the placeholders given, and `name`."""
    with pytest.raises(S.ActionError, match=error) as err:
        S.schedule_action(kind, data)
    assert err.value.key == f"schedule_action_{key}"
    message = STRINGS["exceptions"][err.value.key]["message"]
    assert set(re.findall(r"\{(\w+)\}", message)) == {"name", *err.value.placeholders}


def test_action_fields_of_nothing() -> None:
    assert S.action_fields(None) == {}
    assert S.action_fields(V.NO_ACTION) == {"action": "none"}


# --------------------------------------------------------------------------- slot contents


def test_build_schedule() -> None:
    """A timed schedule fires at its time on every day by default; an astro one has a window and an offset."""
    timed = S.build_schedule(3, {"trigger": "time", "time": dt_time(7, 30)})
    assert timed == V.Schedule(3, 3, frozenset(V.DAYS), (7, 30), (7, 30), 0)
    astro = S.build_schedule(
        0,
        {
            "trigger": "sunset",
            "enabled": False,
            "weekdays": ["sat", "sun"],
            "not_after": dt_time(21, 0),
            "offset": -20,
        },
    )
    assert astro == V.Schedule(
        0, 6, frozenset({"sat", "sun"}), V.UNSET_TIME, (21, 0), -20
    )
    assert S.build_schedule(1, {"trigger": "sunrise"}).not_before == V.UNSET_TIME


def test_slot_as_dict() -> None:
    timed = S.Slot(
        V.Schedule(2, 3, frozenset({"fri", "mon"}), (6, 5), (6, 5), 0),
        V.Action(V.ACTION_SWITCH, on=True),
    )
    assert timed.index == 2
    assert timed.enabled
    assert timed.as_dict() == {
        "slot": 2,
        "trigger": "time",
        "enabled": True,
        "weekdays": ["mon", "fri"],
        "time": "06:05",
        "action": "on",
    }
    astro = S.Slot(
        V.Schedule(4, 4, frozenset({"sun"}), (6, 0), V.UNSET_TIME, 15),
        None,
        V.EffectiveTime(4, 4, frozenset({"sun"}), (7, 27), 15),
    )
    assert not astro.enabled
    assert astro.as_dict() == {
        "slot": 4,
        "trigger": "sunrise",
        "enabled": False,
        "weekdays": ["sun"],
        "not_before": "06:00",
        "not_after": None,
        "offset": 15,
        "effective_time": "07:27",
    }
    reserved = S.Slot(V.Schedule(5, RESERVED, frozenset(), (0, 0), (0, 0), 0))
    assert reserved.as_dict()["trigger"] == "reserved"
    assert "effective_time" not in reserved.as_dict()


# --------------------------------------------------------------------------- which loads get the sensor


def _hub_of(path: Path, meta: Metadata | None = None) -> Any:
    hub = ph.fake_hub()
    hub.cdb = CDB.load(path)
    hub.devices = build_devices(
        hub.cdb, meta or Metadata.from_export(hub.cdb.export_meta)
    )
    return hub


def test_schedule_targets() -> None:
    """Every load element with a JH Scheduler, off by default, under the load's device; keys get none."""
    hub = ph.fake_hub()
    targets = S.schedule_targets(hub)
    assert [(t.address, t.kind, t.page) for t in targets] == [
        (LIGHT_SWITCH, "switch", "lamp"),
        (LIGHT_CTL, "ctl", "lamp"),
        (SOCKET, "socket", "socket"),
        (LIGHT_DIMMER, "dimmer", "lamp"),
        (0x0400, "switch", "lamp"),
        (0x0401, "switch", "lamp"),
    ]
    assert targets[3].unique_id == UID_SCHEDULES_DIMMER
    assert not any(t.enabled_default for t in targets)
    assert {t.specs for t in targets} == {()}
    assert {t.translation_key for t in targets} == {"schedules"}
    # a key whose element hosted one (none does) and a device on no element of the export get no sensor
    hub.cdb.element(BUTTON_WC).models.append(S.SCHEDULER_MODEL)
    assert isinstance(hub.devices.by_address[BUTTON_WC], Button)
    hub.devices.by_address[0x7FFF] = hub.devices.by_address[LIGHT_DIMMER]
    assert len(S.schedule_targets(hub)) == 6


def test_schedule_targets_of_blinds_and_thermostats() -> None:
    blinds = S.schedule_targets(_hub_of(FIXTURES / "Blinds.json"))
    assert [(t.address, t.kind, t.page) for t in blinds if t.kind == "blind"] == [
        (0x0500, "blind", "blind"),
        (0x0600, "blind", "blind"),
        (0x0700, "blind", "blind"),
    ]
    rtr = _hub_of(FIXTURES / "MeshNetwork-rtr.json", Metadata())
    (thermostat,) = [t for t in S.schedule_targets(rtr) if t.kind == "thermostat"]
    assert (thermostat.address, thermostat.page) == (0x0500, "rtr")
    assert thermostat.device_info["name"].startswith("Room thermostat")


# --------------------------------------------------------------------------- the actions


async def test_create_list_toggle_delete(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A timed schedule goes into the first free slot; enable / disable flip its type; delete frees it."""
    dimmer = light(hass)
    assert await call(hass, "get_schedules", {"entity_id": dimmer}, response=True) == {
        dimmer: {"schedules": []}
    }
    fake_link.scheduler.schedules[LIGHT_DIMMER, 0] = V.Schedule(
        0, 3, frozenset({"mon"}), (6, 0), (6, 0), 0
    )
    created = await call(
        hass,
        "create_schedule",
        {
            "entity_id": dimmer,
            "trigger": "time",
            "time": "22:15",
            "weekdays": ["mon", "tue"],
            "brightness_pct": 30,
        },
        response=True,
    )
    assert created == {dimmer: {"slot": 1}}
    stored = fake_link.scheduler.schedules[LIGHT_DIMMER, 1]
    assert stored == V.Schedule(1, 3, frozenset({"mon", "tue"}), (22, 15), (22, 15), 0)
    assert fake_link.scheduler.actions[LIGHT_DIMMER, 1] == V.Action(
        V.ACTION_LIGHTNESS, lightness=round(0.3 * V.LIGHTNESS_MAX)
    )
    # a timed schedule sends no location (the connect-time broadcast to all nodes is not the schedule's)
    assert not [p for _s, d, p in fake_link.sent if p[:1] == b"\x42" and d != ALL_NODES]
    listed = await call(hass, "get_schedules", {"entity_id": dimmer}, response=True)
    assert [s["slot"] for s in listed[dimmer]["schedules"]] == [0, 1]
    assert listed[dimmer]["schedules"][1] == {
        "slot": 1,
        "trigger": "time",
        "enabled": True,
        "weekdays": ["mon", "tue"],
        "time": "22:15",
        "action": "on",
        "brightness_pct": 30,
    }

    await call(hass, "disable_schedule", {"entity_id": dimmer, "slot": 1})
    assert fake_link.scheduler.schedules[LIGHT_DIMMER, 1].type == 2
    sets = len(fake_link.sent)
    await call(hass, "disable_schedule", {"entity_id": dimmer, "slot": 1})
    # already disabled: read, not written
    assert not [
        p
        for _s, _d, p in fake_link.sent[sets:]
        if p[:3]
        == bytes([0xC0 | V.JH_SCHEDULER_SET]) + M.JUNG_CID.to_bytes(2, "little")
    ]
    await call(hass, "enable_schedule", {"entity_id": dimmer, "slot": 1})
    assert fake_link.scheduler.schedules[LIGHT_DIMMER, 1].type == 3

    await call(hass, "delete_schedule", {"entity_id": dimmer, "slot": 1})
    assert (LIGHT_DIMMER, 1) not in fake_link.scheduler.schedules
    assert (LIGHT_DIMMER, 1) not in fake_link.scheduler.actions
    hub = init_integration.runtime_data
    assert [s.index for s in S.scheduler(hass, hub).slots[LIGHT_DIMMER]] == [0]


async def test_astro_schedule_sends_the_home_location(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A sunset schedule is preceded by Home Assistant's home location, to the node's Location Setup Server."""
    hub = init_integration.runtime_data
    hub.cdb.element(LIGHT_CTL).models.append(S.LOCATION_SETUP_MODEL)
    ctl = light(hass, UID_LIGHT_CTL)
    created = await call(
        hass,
        "create_schedule",
        {
            "entity_id": ctl,
            "trigger": "sunset",
            "not_before": "18:00",
            "offset": -15,
            "color_temp_kelvin": 2700,
            "action": "on",
        },
        response=True,
    )
    assert created == {ctl: {"slot": 0}}
    location = M.generic_location_global_set(
        hass.config.latitude, hass.config.longitude, int(hass.config.elevation)
    )
    assert (OUR_ADDRESS, LIGHT_CTL, location) in fake_link.sent
    (slot,) = (await call(hass, "get_schedules", {"entity_id": ctl}, response=True))[
        ctl
    ]["schedules"]
    assert slot == {
        "slot": 0,
        "trigger": "sunset",
        "enabled": True,
        "weekdays": ALL_DAYS,
        "not_before": "18:00",
        "not_after": None,
        "offset": -15,
        "effective_time": "19:33",
        "action": "on",
        "brightness_pct": 100,
        "color_temp_kelvin": 2700,
    }


async def test_astro_schedule_without_a_location_server(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """No element of the node has a Location Setup Server: the schedule is written without the location."""
    socket = entity_id(hass, "switch", UID_SOCKET)
    assert await call(
        hass,
        "create_schedule",
        {"entity_id": socket, "trigger": "sunrise", "action": "on"},
        response=True,
    ) == {socket: {"slot": 0}}
    assert fake_link.scheduler.schedules[SOCKET, 0].type == 5
    assert not [p for _s, d, p in fake_link.sent if p[:1] == b"\x42" and d != ALL_NODES]


async def test_location_send_failure(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    hub = init_integration.runtime_data
    hub.cdb.element(LIGHT_CTL).models.append(S.LOCATION_SETUP_MODEL)
    with (
        patch.object(hub.proxy, "send_access", AsyncMock(side_effect=OSError)),
        pytest.raises(HomeAssistantError) as err,
    ):
        await call(
            hass,
            "create_schedule",
            {
                "entity_id": light(hass, UID_LIGHT_CTL),
                "trigger": "sunrise",
                "action": "on",
            },
        )
    assert err.value.translation_key == "send_failed"


async def test_create_without_response_and_by_device(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A device target works too; a load with no entity in the registry is named by its address."""
    registry = er.async_get(hass)
    dimmer = light(hass)
    device_id = registry.async_get(dimmer).device_id
    assert (
        await call(
            hass,
            "create_schedule",
            {
                "device_id": device_id,
                "trigger": "time",
                "time": "07:00",
                "action": "on",
            },
        )
        is None
    )
    assert fake_link.scheduler.actions[LIGHT_DIMMER, 0] == V.Action(
        V.ACTION_LIGHTNESS, lightness=V.LIGHTNESS_MAX
    )
    registry.async_remove(dimmer)
    await hass.async_block_till_done()
    assert dr.async_get(hass).async_get(device_id) is not None
    listed = await call(hass, "get_schedules", {"device_id": device_id}, response=True)
    assert list(listed) == [f"{LIGHT_DIMMER:04X}"]


async def test_quiet_sets_are_read_back(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    fast_scheduler: None,
) -> None:
    """An element that applies a Set without answering it: the Get after it confirms the write."""
    fake_link.scheduler.quiet_sets = True
    dimmer = light(hass)
    assert await call(
        hass,
        "create_schedule",
        {"entity_id": dimmer, "trigger": "time", "time": "07:00", "action": "off"},
        response=True,
    ) == {dimmer: {"slot": 0}}
    assert fake_link.scheduler.actions[LIGHT_DIMMER, 0] == V.Action(
        V.ACTION_LIGHTNESS, lightness=0
    )
    await call(hass, "delete_schedule", {"entity_id": dimmer, "slot": 0})
    assert (LIGHT_DIMMER, 0) not in fake_link.scheduler.schedules


@pytest.mark.parametrize("sub", [V.SUB_SCHEDULE, V.SUB_ACTION])
async def test_ignored_set_is_not_applied(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    fast_scheduler: None,
    sub: int,
) -> None:
    fake_link.scheduler.ignore_sets = {sub}
    with pytest.raises(HomeAssistantError) as err:
        await call(
            hass,
            "create_schedule",
            {
                "entity_id": light(hass),
                "trigger": "time",
                "time": "07:00",
                "action": "on",
            },
        )
    assert err.value.translation_key == "schedule_not_applied"
    assert err.value.translation_placeholders == {"address": "0300", "slot": "0"}
    # a schedule whose action did not take is freed again: it must not fire with the slot's old action
    assert (LIGHT_DIMMER, 0) not in fake_link.scheduler.schedules


async def test_unfreed_slot_is_reported(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    fast_scheduler: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The action is not taken and neither is the Set that would free the slot again: a warning says so, and
    the slot holds the new schedule inactive (it went in so), so it cannot fire with the slot's old action."""
    scheduler = fake_link.scheduler
    scheduler.ignore_sets = {V.SUB_ACTION}
    handle = scheduler.handle

    def deaf_after_the_action(element: int, is_set: bool, p: bytes) -> bytes | None:
        if is_set and p[0] >> 4 == V.SUB_ACTION:
            scheduler.ignore_sets.add(V.SUB_SCHEDULE)
        return handle(element, is_set, p)

    with (
        patch.object(scheduler, "handle", deaf_after_the_action),
        pytest.raises(HomeAssistantError),
    ):
        await call(
            hass,
            "create_schedule",
            {
                "entity_id": light(hass),
                "trigger": "time",
                "time": "07:00",
                "action": "on",
            },
        )
    assert "0300: slot 0 was not freed again" in caplog.text
    assert scheduler.schedules[LIGHT_DIMMER, 0].type == S.TRIGGERS["time"][False]


async def test_a_new_schedule_goes_active_after_its_action(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The schedule is written inactive, then its action, then a type-only Set makes it active; one asked for
    disabled stays as written."""
    scheduler = fake_link.scheduler
    handle = scheduler.handle
    sets: list[
        tuple[int, int | None]
    ] = []  # (sub-command, the slot's type once applied)

    def recording(element: int, is_set: bool, p: bytes) -> bytes | None:
        answer = handle(element, is_set, p)
        if is_set:
            held = scheduler.schedules.get((element, p[0] & 0xF))
            sets.append((p[0] >> 4, None if held is None else held.type))
        return answer

    data = {"entity_id": light(hass), "trigger": "time", "time": "07:00"}
    with patch.object(scheduler, "handle", recording):
        await call(hass, "create_schedule", {**data, "action": "on"})
        assert sets == [(V.SUB_SCHEDULE, 2), (V.SUB_ACTION, 2), (V.SUB_SCHEDULE, 3)]
        sets.clear()
        await call(hass, "create_schedule", {**data, "action": "off", "enabled": False})
        assert sets == [(V.SUB_SCHEDULE, 2), (V.SUB_ACTION, 2)]


async def test_a_failing_load_names_the_slots_already_created(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    fast_scheduler: None,
) -> None:
    """Review-3 W9: two loads, the second (the socket) stops taking Sets after the free-slot check. The dimmer
    holds the schedule already, and the error says so — the response that would have told never comes, and a
    retry as it is would give the dimmer the schedule twice. A call failing on its first load says only why."""
    scheduler = fake_link.scheduler
    handle = scheduler.handle

    def deaf_socket(element: int, is_set: bool, p: bytes) -> bytes | None:
        if is_set and element == SOCKET:
            return None
        return handle(element, is_set, p)

    dimmer, socket = light(hass), entity_id(hass, "switch", UID_SOCKET)
    data = {"trigger": "time", "time": "07:00", "action": "on"}
    with (
        patch.object(scheduler, "handle", deaf_socket),
        pytest.raises(HomeAssistantError) as err,
    ):
        await call(hass, "create_schedule", {"entity_id": [dimmer, socket], **data})
    assert err.value.translation_key == "schedule_partly_created"
    assert err.value.translation_placeholders == {
        "error": "The JUNG device 0172 did not take the change of schedule slot 0",
        "created": f"{dimmer} (slot 0)",
    }
    assert str(err.value).endswith(
        f"The schedule was already written to {dimmer} (slot 0): delete it there before calling again, or "
        "those devices run it twice"
    )
    assert (LIGHT_DIMMER, 0) in scheduler.schedules
    with (
        patch.object(scheduler, "handle", deaf_socket),
        pytest.raises(HomeAssistantError) as err,
    ):
        await call(hass, "create_schedule", {"entity_id": socket, **data})
    assert err.value.translation_key == "schedule_not_applied"


async def test_a_schedule_that_does_not_go_active_is_freed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    fast_scheduler: None,
) -> None:
    """Schedule and action are in, but the type-only Set that makes it active is not taken: freed again."""
    scheduler = fake_link.scheduler
    handle = scheduler.handle

    def no_activation(element: int, is_set: bool, p: bytes) -> bytes | None:
        if is_set and len(p) == 2 and p[0] >> 4 == V.SUB_SCHEDULE and p[1]:
            return None
        return handle(element, is_set, p)

    with (
        patch.object(scheduler, "handle", no_activation),
        pytest.raises(HomeAssistantError) as err,
    ):
        await call(
            hass,
            "create_schedule",
            {
                "entity_id": light(hass),
                "trigger": "time",
                "time": "07:00",
                "action": "on",
            },
        )
    assert err.value.translation_key == "schedule_not_applied"
    assert (LIGHT_DIMMER, 0) not in scheduler.schedules
    assert (LIGHT_DIMMER, 0) not in scheduler.actions


async def test_silent_element(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    fast_scheduler: None,
) -> None:
    fake_link.scheduler.silent.add(LIGHT_DIMMER)
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "get_schedules", {"entity_id": light(hass)}, response=True)
    assert err.value.translation_key == "schedule_no_reply"


async def test_a_list_without_its_slots_is_no_answer(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A list Status too short to hold the 16 slots (or the header-only answer to an unknown sub-command) says
    nothing about them: neither "no schedules" (the cache keeps what was read) nor "every slot used"."""
    dimmer = light(hass)
    scheduler = fake_link.scheduler
    scheduler.schedules[LIGHT_DIMMER, 0] = V.Schedule(
        0, 3, frozenset({"mon"}), (6, 0), (6, 0), 0
    )
    await call(hass, "get_schedules", {"entity_id": dimmer}, response=True)
    status = scheduler.status

    def header_only(element: int, index: int, sub: int) -> bytes:
        return bytes([0xF0]) if sub == V.SUB_LIST else status(element, index, sub)

    create = {"trigger": "time", "time": "07:00", "action": "on"}
    with patch.object(scheduler, "status", header_only):
        for service, data in (("get_schedules", {}), ("create_schedule", create)):
            with pytest.raises(HomeAssistantError) as err:
                await call(
                    hass,
                    service,
                    {"entity_id": dimmer, **data},
                    response=service == "get_schedules",
                )
            assert err.value.translation_key == "schedule_no_reply"
    hub = init_integration.runtime_data
    assert [s.index for s in S.scheduler(hass, hub).slots[LIGHT_DIMMER]] == [0]
    assert list(scheduler.schedules) == [(LIGHT_DIMMER, 0)]


async def test_lost_link(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    hub = init_integration.runtime_data
    with (
        patch.object(hub.proxy, "request", AsyncMock(side_effect=ConnectionError)),
        pytest.raises(HomeAssistantError) as err,
    ):
        await call(hass, "get_schedules", {"entity_id": light(hass)}, response=True)
    assert err.value.translation_key == "send_failed"


async def test_slots_full(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    for i in range(V.SLOTS):
        fake_link.scheduler.schedules[LIGHT_DIMMER, i] = V.Schedule(
            i, 2, frozenset(), (1, 0), (1, 0), 0
        )
    with pytest.raises(HomeAssistantError) as err:
        await call(
            hass,
            "create_schedule",
            {
                "entity_id": light(hass),
                "trigger": "time",
                "time": "07:00",
                "action": "on",
            },
        )
    assert err.value.translation_key == "schedule_slots_full"


async def test_a_full_load_stops_the_call_before_any_write(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Several loads: every one is checked for a free slot first, so a full one leaves nothing written on the
    others (a retry after freeing a slot would otherwise create their schedules a second time)."""
    for i in range(V.SLOTS):
        fake_link.scheduler.schedules[SOCKET, i] = V.Schedule(
            i, 2, frozenset(), (1, 0), (1, 0), 0
        )
    with pytest.raises(HomeAssistantError) as err:
        await call(
            hass,
            "create_schedule",
            {
                "entity_id": [light(hass), entity_id(hass, "switch", UID_SOCKET)],
                "trigger": "time",
                "time": "07:00",
                "action": "on",
            },
        )
    assert err.value.translation_key == "schedule_slots_full"
    assert err.value.translation_placeholders == {"address": f"{SOCKET:04X}"}
    assert not [key for key in fake_link.scheduler.schedules if key[0] != SOCKET]
    assert not fake_link.scheduler.actions


async def test_toggle_whose_read_back_is_unanswered(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    fast_scheduler: None,
) -> None:
    """The type-only Set is confirmed, the read of the slot after it is not answered: the call succeeds and the
    cache follows what was written, the rest of the slot as last read (nothing for a slot never read)."""
    dimmer = light(hass)
    scheduler = fake_link.scheduler
    await call(
        hass,
        "create_schedule",
        {"entity_id": dimmer, "trigger": "time", "time": "22:15", "brightness_pct": 30},
    )
    scheduler.schedules[LIGHT_DIMMER, 1] = V.Schedule(
        1, 3, frozenset({"mon"}), (6, 0), (6, 0), 0
    )
    handle = scheduler.handle

    def no_action_gets(element: int, is_set: bool, p: bytes) -> bytes | None:
        if not is_set and p and p[0] >> 4 == V.SUB_ACTION:
            return None
        return handle(element, is_set, p)

    with patch.object(scheduler, "handle", no_action_gets):
        for index in (0, 1):
            await call(hass, "disable_schedule", {"entity_id": dimmer, "slot": index})
    assert scheduler.schedules[LIGHT_DIMMER, 0].type == 2
    assert scheduler.schedules[LIGHT_DIMMER, 1].type == 2
    hub = init_integration.runtime_data
    first, second = S.scheduler(hass, hub).slots[LIGHT_DIMMER]
    assert (first.index, first.enabled, first.action) == (
        0,
        False,
        V.Action(V.ACTION_LIGHTNESS, lightness=round(0.3 * V.LIGHTNESS_MAX)),
    )
    assert (second.index, second.enabled, second.action) == (1, False, None)
    assert second.schedule == scheduler.schedules[LIGHT_DIMMER, 1]


@pytest.mark.parametrize(
    "service", ["enable_schedule", "disable_schedule", "delete_schedule"]
)
async def test_empty_or_reserved_slot(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    service: str,
) -> None:
    """A free slot is the caller's mistake; so is enabling one the app did not write (a reserved type)."""
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, service, {"entity_id": light(hass), "slot": 4})
    assert err.value.translation_key == "schedule_empty_slot"
    fake_link.scheduler.schedules[LIGHT_DIMMER, 4] = V.Schedule(
        4, RESERVED, frozenset(), (0, 0), (0, 0), 0
    )
    if service == "delete_schedule":
        await call(hass, service, {"entity_id": light(hass), "slot": 4})
        assert (LIGHT_DIMMER, 4) not in fake_link.scheduler.schedules
        return
    with pytest.raises(ServiceValidationError):
        await call(hass, service, {"entity_id": light(hass), "slot": 4})


async def test_load_without_a_scheduler(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    init_integration.runtime_data.cdb.element(LIGHT_DIMMER).models.remove(
        S.SCHEDULER_MODEL
    )
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "get_schedules", {"entity_id": light(hass)}, response=True)
    assert err.value.translation_key == "schedule_not_supported"
    assert err.value.translation_placeholders == {"name": light(hass)}


async def test_action_that_does_not_fit(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Checked for every load before anything goes on air."""
    socket = entity_id(hass, "switch", UID_SOCKET)
    with pytest.raises(ServiceValidationError) as err:
        await call(
            hass,
            "create_schedule",
            {
                "entity_id": [light(hass), socket],
                "trigger": "time",
                "time": "07:00",
                "brightness_pct": 50,
            },
        )
    assert err.value.translation_key == "schedule_action_not_applicable"
    assert err.value.translation_placeholders == {
        "name": socket,
        "fields": "brightness_pct",
    }
    assert f"does not fit {socket}: brightness_pct does not apply to it" in str(
        err.value
    )
    assert not fake_link.scheduler.schedules


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"trigger": "time"}, "needs `time`"),
        ({"trigger": "time", "time": "07:00", "offset": 5}, "offset go with"),
        ({"trigger": "sunset", "time": "07:00"}, "goes with the trigger"),
        ({"trigger": "sunset", "weekdays": []}, "length"),
        ({"trigger": "sunset", "weekdays": ["monday"]}, "value must be one of"),
    ],
)
def test_create_schema(data: dict[str, Any], error: str) -> None:
    with pytest.raises(vol.Invalid, match=error):
        services.CREATE_SCHEDULE_SCHEMA({"entity_id": "light.x", **data})


# --------------------------------------------------------------------------- the sensor


async def test_schedules_sensor(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    mesh: ph.PropertyMesh,
    fast_sleep: list[float],
) -> None:
    """Read once per link: the number of used slots, the slots as an attribute (not recorded); writes update it."""
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", DOMAIN, UID_SCHEDULES_DIMMER, disabled_by=None
    )
    fake_link.scheduler.schedules[LIGHT_DIMMER, 2] = V.Schedule(
        2, 7, frozenset({"sun"}), V.UNSET_TIME, V.UNSET_TIME, 0
    )
    fake_link.scheduler.actions[LIGHT_DIMMER, 2] = V.Action(
        V.ACTION_LIGHTNESS, lightness=0
    )
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    sensor = entity_id(hass, "sensor", UID_SCHEDULES_DIMMER)
    assert registry.async_get(sensor).entity_category is EntityCategory.DIAGNOSTIC
    state = hass.states.get(sensor)
    assert state.state == "1"
    assert state.attributes["mesh_address"] == "0300"
    assert state.attributes["schedules"] == [
        {
            "slot": 2,
            "trigger": "sunset",
            "enabled": True,
            "weekdays": ["sun"],
            "not_before": None,
            "not_after": None,
            "offset": 0,
            "effective_time": "19:48",
            "action": "off",
        }
    ]
    await call(hass, "delete_schedule", {"entity_id": light(hass), "slot": 2})
    state = hass.states.get(sensor)
    assert (state.state, state.attributes["schedules"]) == ("0", [])


async def test_schedules_sensor_unread(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    mesh: ph.PropertyMesh,
    fast_sleep: list[float],
    fast_scheduler: None,
) -> None:
    """An element that does not answer leaves the sensor unknown, without the attribute."""
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, UID_SCHEDULES_DIMMER, disabled_by=None
    )
    fake_link.scheduler.silent.add(LIGHT_DIMMER)
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    # the Gets time out in real time, which `settle` does not wait for
    reader = property_reader(hass, mock_config_entry.runtime_data)
    await wait_until(
        hass,
        lambda: reader._worker is None or reader._worker.done(),
        what="the reads",
    )
    state = hass.states.get(entity_id(hass, "sensor", UID_SCHEDULES_DIMMER))
    assert state.state == STATE_UNKNOWN
    assert "schedules" not in state.attributes


def test_scheduler_is_per_entry() -> None:
    """One scheduler per entry, dropped when the entry unloads."""
    unloads: list[Any] = []
    hub = SimpleNamespace(
        entry=SimpleNamespace(entry_id="e1", async_on_unload=unloads.append)
    )
    hass = SimpleNamespace(data={})
    first = S.scheduler(hass, hub)  # type: ignore[arg-type]
    assert S.scheduler(hass, hub) is first  # type: ignore[arg-type]
    unloads[0]()
    assert S.scheduler(hass, hub) is not first  # type: ignore[arg-type]


@pytest.mark.parametrize("code", [3, 1])
async def test_listed_slot_found_free(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    code: int,
) -> None:
    """A slot the list calls active but that reads as free (freed in between) is left out; one of a central
    scheduler (list state 1) is not even read, as the app hides it."""
    listing = fake_link.scheduler.status
    asked: list[tuple[int, int]] = []

    def status(element: int, index: int, sub: int) -> bytes:
        asked.append((index, sub))
        raw = listing(element, index, sub)
        if sub == V.SUB_LIST:  # slot 5 in state `code`, whatever the slot says
            return raw[:2] + (int.from_bytes(raw[2:], "little") | code << 10).to_bytes(
                4, "little"
            )
        return raw

    with patch.object(fake_link.scheduler, "status", status):
        listed = await call(
            hass, "get_schedules", {"entity_id": light(hass)}, response=True
        )
    assert listed == {light(hass): {"schedules": []}}
    assert ((5, V.SUB_SCHEDULE) in asked) is (code == 3)
