"""The configurator's planners are pure (review-4 brief 55): a `ProjectFile` in, Config steps out.

A room link planned straight from the fixture export — no `hass`, no hub, no proxy link — and put in the order it
goes out on air (`ordered`): the room's lamps subscribe to the key's own group first, the key's old subscriptions
are dropped last. The same plan `assign_key` sends (`test_mesh_config.WIRE_ROCKER_A_TO_WC`, `UNLISTEN_ROCKER_A`)
minus the key's own client wiring, which `assign_key` adds to it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.junghome_ble.configurator.plan import PlanError
from custom_components.junghome_ble.configurator.wiring import (
    find_element,
    plan_room_link,
)
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh.devices import Metadata
from custom_components.junghome_ble.jhmesh.export import KEY_MODE_LIGHT, ProjectFile
from custom_components.junghome_ble.jhmesh.plan import ordered

ANDROID_PATH = Path(__file__).parent / "fixtures" / "JungHome-android.json"
DALI_NODE, ROCKER_A, DALI_GROUP, ROCKER_A_GROUP = 0x0232, 0x0234, 0xC044, 0xC04F
SWITCH_NODE, SWITCH_LOAD = 0x0148, 0x0148
DIMMER_NODE, DIMMER_LOAD = 0x0300, 0x0300
WC = 0xC00F


def test_a_room_link_is_planned_without_home_assistant() -> None:
    pf = ProjectFile.load(ANDROID_PATH)
    before = pf.snapshot()
    key = find_element(pf, ROCKER_A)
    plan = plan_room_link(pf, key, "wc", None, Metadata.from_export(pf.meta))
    assert (plan.key_mode, plan.publish, plan.mode) == (
        KEY_MODE_LIGHT,
        ROCKER_A_GROUP,
        "light",
    )
    assert plan.prepare == {
        "kind": "room_link",
        "key": ROCKER_A,
        "room": WC,
        "publish": ROCKER_A_GROUP,
        "function": "LIGHT",
    }
    sent = [(step.node, step.pdu) for step in ordered(plan.steps)]
    adds = [
        (SWITCH_NODE, C.model_subscription_add(SWITCH_LOAD, ROCKER_A_GROUP, "1000")),
        (DIMMER_NODE, C.model_subscription_add(DIMMER_LOAD, ROCKER_A_GROUP, "1000")),
        (DIMMER_NODE, C.model_subscription_add(DIMMER_LOAD, ROCKER_A_GROUP, "1002")),
    ]
    assert (
        sent[: len(adds)] == adds
    )  # additive steps first: the old wiring keeps working until the end
    drops = sent[len(adds) :]
    assert drops
    assert all(not step.additive for step in ordered(plan.steps)[len(adds) :])
    for model in ("1001", "05271015"):
        assert (
            DALI_NODE,
            C.model_subscription_delete(ROCKER_A, DALI_GROUP, model),
        ) in drops
    # the planner edited the export it was given, and nothing else: the record is the caller's to write
    assert pf.snapshot() != before
    assert ProjectFile.load(ANDROID_PATH).snapshot() == before


def test_a_planner_refuses_with_a_plan_error() -> None:
    pf = ProjectFile.load(ANDROID_PATH)
    key = find_element(pf, ROCKER_A)
    with pytest.raises(PlanError) as exc:
        plan_room_link(pf, key, "No such room", None, Metadata())
    assert (exc.value.key, exc.value.placeholders) == (
        "service_no_room",
        {"room": "No such room"},
    )
