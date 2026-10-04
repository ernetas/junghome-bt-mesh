"""jhmesh.plan: the plan model — a step's Status match, the order on air, the replay into the export, the builders."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jhmesh import commission
from jhmesh import config_messages as C
from jhmesh.export import ModelChange, ProjectFile, raw_model
from jhmesh.pdu import decode_opcode, encode_opcode
from jhmesh.plan import (
    APP_KEY_INDEX,
    ConfigStep,
    bind_step,
    config_step,
    config_steps,
    deletable,
    element_of,
    ordered,
    replay,
)

from .conftest import CDB_PATH, PROXY_NODE

KEY = 0x0149  # button A of the 1-gang push-button: Generic OnOff Client 1001 → C005
KEY_NODE = PROXY_NODE
GROUP = 0xC070
OTHER_GROUP = 0xC044
KEY_2G = 0x0234  # a button of the 2-gang push-button (node 0x0232)


def status(opcode: int, params: bytes) -> Any:
    """An access message as the client hands it to `ConfigStep.matches`."""
    sop, scid, sparams = decode_opcode(encode_opcode(opcode) + params)
    return SimpleNamespace(opcode=sop, company_id=scid, params=sparams)


def sub_status(element: int, address: int, model: bytes) -> Any:
    return status(
        C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
        b"\x00" + element.to_bytes(2, "little") + address.to_bytes(2, "little") + model,
    )


def subs(pf: ProjectFile, element: int, model: str) -> list[str]:
    return list(raw_model(element_of(pf, element), model).get("subscribe", []))


def test_a_step_describes_itself_without_its_pdu_in_repr() -> None:
    pdu = C.model_subscription_add(KEY, GROUP, "1001")
    step = ConfigStep(KEY_NODE, pdu, C.CONFIG_MODEL_SUBSCRIPTION_STATUS)
    assert step.destination == KEY_NODE
    assert "Subscription Add" in step.text
    assert step.what
    assert pdu.hex() not in repr(step)
    # the commissioning plan's step is the same class
    assert commission.Step is ConfigStep


def test_additive_steps_add_wiring() -> None:
    def kind(kind: Any, address: int = GROUP) -> bool:
        return ConfigStep(
            KEY_NODE, b"", 0, change=ModelChange(KEY, "1001", address, kind)
        ).additive

    assert ConfigStep(KEY_NODE, b"", 0).additive
    assert kind("subscribe")
    assert kind("publish")
    assert not kind("publish", 0)
    assert not kind("unsubscribe")


def test_a_step_matches_only_the_status_that_echoes_it() -> None:
    change = ModelChange(KEY, "1001", GROUP, "subscribe")
    step = ConfigStep(
        KEY_NODE, C.model_subscription_add(KEY, GROUP, "1001"), 0x801F, change=change
    )
    assert step.matches(sub_status(KEY, GROUP, b"\x01\x10"))
    assert not step.matches(sub_status(KEY, GROUP, b"\x03\x10"))
    assert not step.matches(sub_status(KEY, OTHER_GROUP, b"\x01\x10"))
    assert not step.matches(sub_status(KEY_2G, GROUP, b"\x01\x10"))
    # malformed: judged by the executor, not here
    assert step.matches(status(0x801F, b"\x00"))
    # another status: not judged here (the client matched the opcode)
    assert step.matches(status(C.CONFIG_APPKEY_STATUS, b"\x00\x00\x00\x00"))
    # a publication step compares the publish address; a clear echoes 0000
    clear = ConfigStep(
        KEY_NODE,
        C.model_publication_set(KEY, 0, "1001"),
        0x8019,
        change=ModelChange(KEY, "1001", 0, "publish"),
    )
    element = KEY.to_bytes(2, "little").hex()
    assert clear.matches(
        status(0x8019, bytes.fromhex(f"00 {element} 0000 0000 ff 00 00 0110"))
    )
    assert not clear.matches(
        status(0x8019, bytes.fromhex(f"00 {element} 70c0 0000 ff 00 00 0110"))
    )
    # a bind step compares element, model and AppKey index
    bind = ConfigStep(
        KEY_NODE,
        C.model_app_bind(KEY, "1003", APP_KEY_INDEX),
        0x803E,
        bind=(KEY, "1003"),
    )
    assert bind.matches(status(0x803E, bytes.fromhex(f"00 {element} 0000 0310")))
    assert not bind.matches(status(0x803E, bytes.fromhex(f"00 {element} 0100 0310")))
    assert not bind.matches(status(0x803E, bytes.fromhex(f"00 {element} 0000 0110")))
    assert not bind.matches(status(0x803E, bytes.fromhex("00 3402 0000 0310")))
    assert bind.matches(sub_status(KEY, GROUP, b"\x01\x10"))
    # a step without an edit matches whatever the client matched
    bare = ConfigStep(KEY_NODE, b"", 0x801F)
    assert bare.matches(sub_status(KEY, OTHER_GROUP, b"\x03\x10"))


def test_ordered_puts_additions_first_and_drops_superseded_clears() -> None:
    sub = ModelChange(KEY, "1001", GROUP, "subscribe")
    unsub_same = ModelChange(KEY, "1001", GROUP, "unsubscribe")
    unsub_other = ModelChange(KEY, "1001", OTHER_GROUP, "unsubscribe")
    clear_pub = ModelChange(KEY, "1001", 0, "publish")
    set_pub = ModelChange(KEY, "1001", GROUP, "publish")
    clear_other_pub = ModelChange(KEY, "1003", 0, "publish")
    steps = [
        ConfigStep(KEY_NODE, b"a", 0, change=clear_pub),
        ConfigStep(KEY_NODE, b"b", 0, change=unsub_same),
        ConfigStep(KEY_NODE, b"c", 0, change=unsub_other),
        ConfigStep(KEY_NODE, b"d", 0, change=clear_other_pub),
        ConfigStep(KEY_NODE, b"e", 0, bind=(KEY, "1001")),
        ConfigStep(KEY_NODE, b"f", 0, change=set_pub),
        ConfigStep(KEY_NODE, b"g", 0, change=sub),
    ]
    assert [s.pdu for s in ordered(steps)] == [b"e", b"f", b"g", b"c", b"d"]


def test_ordered_sends_the_sleepy_nodes_steps_first_within_each_half() -> None:
    add = ModelChange(KEY, "1001", GROUP, "subscribe")
    drop = ModelChange(KEY, "1001", OTHER_GROUP, "unsubscribe")
    sleepy = 0x0300
    steps = [
        ConfigStep(KEY_NODE, b"mains drop", 0, change=drop),
        ConfigStep(sleepy, b"sleepy drop", 0, change=drop),
        ConfigStep(KEY_NODE, b"mains add", 0, change=add),
        ConfigStep(sleepy, b"sleepy add", 0, change=add),
    ]
    assert [s.pdu for s in ordered(steps, {sleepy})] == [
        b"sleepy add",
        b"mains add",
        b"sleepy drop",
        b"mains drop",
    ]


def test_replay_applies_every_kind_of_step_idempotently() -> None:
    pf = ProjectFile.load(CDB_PATH)
    raw = raw_model(element_of(pf, KEY), "1003")
    raw["bind"] = []
    bind = ConfigStep(KEY_NODE, b"", 0, bind=(KEY, "1003"))
    replay(pf, bind)
    replay(pf, bind)
    assert raw["bind"] == [APP_KEY_INDEX]
    for step in (
        ConfigStep(
            KEY_NODE, b"", 0, change=ModelChange(KEY, "1001", GROUP, "subscribe")
        ),
        ConfigStep(
            KEY_NODE, b"", 0, change=ModelChange(KEY, "1001", 0xC005, "unsubscribe")
        ),
        ConfigStep(KEY_NODE, b"", 0, change=ModelChange(KEY, "1001", 0, "publish")),
    ):
        replay(pf, step)
        replay(pf, step)
    assert subs(pf, KEY, "1001") == ["C070"]
    assert "publish" not in raw_model(element_of(pf, KEY), "1001")
    replay(
        pf,
        ConfigStep(KEY_NODE, b"", 0, change=ModelChange(KEY, "1001", GROUP, "publish")),
    )
    assert raw_model(element_of(pf, KEY), "1001")["publish"]["address"] == "C070"


def test_deletable_names_the_groups_a_delete_can_carry() -> None:
    assert deletable(0xC000)
    assert deletable(0xFEFF)
    assert not deletable(0xFFFC)  # all-proxies
    assert not deletable(0x8000)  # virtual
    assert not deletable(0x0149)


def test_the_builders_mirror_each_edit() -> None:
    pf = ProjectFile.load(CDB_PATH)
    changes = [
        ModelChange(KEY, "1001", GROUP, "subscribe"),
        ModelChange(KEY, "1001", 0xC005, "unsubscribe"),
        ModelChange(KEY, "1001", GROUP, "publish"),
    ]
    steps = config_steps(pf, changes)
    assert [s.expect for s in steps] == [
        C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
        C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
        C.CONFIG_MODEL_PUBLICATION_STATUS,
    ]
    assert [s.change for s in steps] == changes
    assert {s.node for s in steps} == {KEY_NODE}
    assert steps[0].pdu == C.model_subscription_add(KEY, GROUP, "1001")
    assert steps[1].pdu == C.model_subscription_delete(KEY, 0xC005, "1001")
    assert steps[2] == config_step(pf, changes[2])
    assert steps[2].pdu == C.model_publication_set(KEY, GROUP, "1001")


@pytest.mark.parametrize("bound", [True, False])
def test_bind_step_binds_an_unbound_model_once(bound: bool) -> None:
    pf = ProjectFile.load(CDB_PATH)
    element = element_of(pf, KEY)
    if not bound:
        raw_model(element, "1003")["bind"] = []
    step = bind_step(element, "1003")
    if bound:
        assert step is None
        return
    assert step == ConfigStep(
        KEY_NODE,
        C.model_app_bind(KEY, "1003", APP_KEY_INDEX),
        C.CONFIG_MODEL_APP_STATUS,
        bind=(KEY, "1003"),
    )
    assert raw_model(element, "1003")["bind"] == [APP_KEY_INDEX]
    assert bind_step(element, "1003") is None
