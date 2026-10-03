"""jhmesh.commission: the app's post-provisioning Config sequence, planned from a template node of the export."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from jhmesh import commission
from jhmesh import config_messages as C
from jhmesh.cdb import CDB, Node
from jhmesh.export import AllocationCrowded
from jhmesh.pdu import decode_opcode

from .conftest import CDB_PATH

NEW = 0x0D10
TEMPLATE = 0x0148  # push-button 1-gang with a light insert: element 0 load, 0x0149 / 0x014A keys


def network() -> dict[str, Any]:
    return json.loads(CDB_PATH.read_text())["meshNetwork"]


def load(net: dict[str, Any]) -> CDB:
    return CDB.from_network(copy.deepcopy(net))


def node(cdb: CDB, unicast: int) -> Node:
    found = cdb.node_by_addr(unicast)
    assert found is not None
    return found


def raw_node(net: dict[str, Any], unicast: str) -> dict[str, Any]:
    return next(n for n in net["nodes"] if n["unicastAddress"] == unicast)


def decoded(step: commission.Step) -> tuple[int, bytes]:
    opcode, _cid, params = decode_opcode(step.pdu)
    return opcode, params


def ops(plan: commission.Plan, phase: str) -> list[str]:
    return [s.text for s in plan.steps if s.phase == phase]


def test_plan_follows_the_apps_step_machine(cdb: CDB):
    plan = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE))
    assert plan.unicast == NEW
    assert plan.elements == 3
    assert plan.template == TEMPLATE
    assert plan.phases() == [
        "SetWhitelistFilter",
        "RequestCompositionData",
        "SetConfiguration",
        "SetBlacklistFilter",
        "CreateElementGroups",
        "FinishConfiguration",
    ]
    assert all(s.destination == NEW for s in plan.steps)
    assert all(s.evidence.startswith("docs/") for s in plan.steps)
    # every step builds, decodes and is answered by the status the builders' module names
    for step in plan.steps:
        opcode, _params = decoded(step)
        assert opcode in C.CONFIG_NAMES
        assert step.expect in C.CONFIG_NAMES
        assert "??" not in step.text

    first = plan.steps[0]
    assert decoded(first) == (
        C.CONFIG_APPKEY_ADD,
        C.appkey_add(cdb.app_keys[0].key)[1:],
    )
    assert first.expect == C.CONFIG_APPKEY_STATUS
    # the AppKey is in the PDU, never in the text or the repr
    key_hex = cdb.app_keys[0].key.hex()
    assert key_hex not in first.text.lower()
    assert key_hex not in repr(plan).lower()
    assert first.text == "Config AppKey Add netkey=0 appkey=0 key=<16 bytes>"


def test_binds_the_bind_list_first_then_what_the_messenger_binds(cdb: CDB):
    plan = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE))
    steps = [s for s in plan.steps if s.phase == "RequestCompositionData"]
    assert steps[0].text == "Config Composition Data Get page=0"
    binds = [decoded(s)[1] for s in steps[1:]]
    assert all(s.expect == C.CONFIG_MODEL_APP_STATUS for s in steps[1:])
    expected_list = [
        C.model_app_bind(NEW, m)[2:]
        for m in (
            "1200",
            "1201",
            "1011",
            "1012",
            "1000",
            "1004",
            "1006",
            "1007",
            "1203",
            "1204",
            "05271013",
            "05271011",
            "05271016",
            "05271017",
        )
    ]
    expected_list += [
        C.model_app_bind(NEW + 1, m)[2:]
        for m in (
            "1001",
            "1003",
            "1302",
            "1205",
            "1305",
            "05271015",
            "05271013",
            "05271011",
        )
    ]
    expected_list += [
        C.model_app_bind(NEW + 2, m)[2:] for m in ("05271015", "05271013", "05271011")
    ]
    later = [
        C.model_app_bind(NEW, "0002")[2:],
        C.model_app_bind(NEW, "1013")[2:],
        C.model_app_bind(NEW, "05271012")[2:],
        C.model_app_bind(NEW + 1, "05271012")[2:],
        C.model_app_bind(NEW + 2, "05271012")[2:],
    ]
    assert binds == expected_list + later
    assert all("bind list" in s.evidence for s in steps[1 : 1 + len(expected_list)])
    assert all("messenger" in s.evidence for s in steps[1 + len(expected_list) :])
    # the Configuration Server is never bound, although the export lists it bound
    assert C.model_app_bind(NEW, "0000")[2:] not in binds


def test_node_wide_settings_fall_back_to_what_the_installation_holds(cdb: CDB):
    plan = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE))
    assert ops(plan, "SetConfiguration") == [
        "Config GATT Proxy Set enabled",
        "Config AppKey Add netkey=0 appkey=0 key=<16 bytes>",
        "Config Default TTL Set ttl=5",
        "Config Relay Set enabled retx=2/8",
        "Config Network Transmit Set count=2 steps=9",
    ]
    assert ops(plan, "SetBlacklistFilter") == ["Config Beacon Set enabled"]
    by_text = {s.text: s.evidence for s in plan.steps}
    assert "value from the template" in by_text["Config Default TTL Set ttl=5"]
    assert (
        "hidden-features.md §4"
        in by_text["Config Network Transmit Set count=2 steps=9"]
    )
    assert "state from the template" in by_text["Config Relay Set enabled retx=2/8"]
    assert (
        "the app (on unless a battery device)" in by_text["Config Beacon Set enabled"]
    )


def test_node_wide_settings_are_copied_from_the_template():
    net = network()
    tmpl = raw_node(net, "0148")
    tmpl.update(
        defaultTTL=7,
        features={"relay": 0, "proxy": 1, "friend": 2, "lowPower": 2},
        relayRetransmit={"count": 3, "interval": 90},
        networkTransmit={"count": 3, "interval": 100},
        secureNetworkBeacon=False,
    )
    cdb = load(net)
    plan = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE))
    assert ops(plan, "SetConfiguration")[2:] == [
        "Config Default TTL Set ttl=7",
        "Config Relay Set disabled retx=2/8",
        "Config Network Transmit Set count=2 steps=9",
    ]
    assert ops(plan, "SetBlacklistFilter") == ["Config Beacon Set disabled"]
    assert all(
        "from the template" in s.evidence and "hidden-features" not in s.evidence
        for s in plan.steps
        if s.phase in ("SetConfiguration", "SetBlacklistFilter")
        and decoded(s)[0] != C.CONFIG_APPKEY_ADD
        and decoded(s)[0] != C.CONFIG_GATT_PROXY_SET
    )


def test_element_groups_mirror_the_templates_own_group_wiring(cdb: CDB):
    plan = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE))
    # C000 / C001 are taken, C002 is the lowest free address of the phone's range C000..C64B
    assert [(g.address, g.name, g.element) for g in plan.groups] == [
        (0xC002, "element group #0xD10", NEW),
        (0xC003, "element group #0xD11", NEW + 1),
    ]
    steps = [s for s in plan.steps if s.phase == "CreateElementGroups"]
    assert [decoded(s)[1] for s in steps] == [
        C.model_publication_set(NEW, 0xC002, "1000")[1:],
        C.model_subscription_add(NEW, 0xC002, "1000")[2:],
        C.model_publication_set(NEW, 0xC002, "1203")[1:],
        C.model_subscription_add(NEW, 0xC002, "1203")[2:],
        C.model_publication_set(NEW, 0xC002, "05271013")[1:],
        C.model_subscription_add(NEW, 0xC002, "05271013")[2:],
        C.model_publication_set(NEW + 1, 0xC003, "05271013")[1:],
        C.model_subscription_add(NEW + 1, 0xC003, "05271013")[2:],
    ]
    assert [s.expect for s in steps[:2]] == [
        C.CONFIG_MODEL_PUBLICATION_STATUS,
        C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
    ]
    texts = " ".join(s.text for s in plan.steps)
    # user configuration of the template is not copied: its room (C00F), its key connection to the gateway (C005)
    assert "C00F" not in texts
    assert "C005" not in texts


def test_device_type_groups_and_the_sensor_server(cdb: CDB):
    lamp = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE))
    assert ops(lamp, "FinishConfiguration") == [
        "Config Model Subscription Add elem=0D10 address=FEF5 model=1000",
        "Config Model Subscription Add elem=0D10 address=FEF5 model=1203",
    ]
    socket = commission.plan(cdb, NEW, 2, node(cdb, 0x0172))
    assert ops(socket, "FinishConfiguration") == [
        "Config Model Subscription Add elem=0D10 address=FEF8 model=1000",
        "Config Model Subscription Add elem=0D10 address=FEF8 model=1203",
    ]
    # the power sensor's Sensor Server publishes to its element group on the template: not part of the set-up
    texts = [s.text for s in socket.steps if s.phase == "CreateElementGroups"]
    assert not any("model=1100" in t for t in texts)
    assert [g.element for g in socket.groups] == [NEW, NEW + 1]


def test_pp2_time_keeper_subscription_and_battery_devices():
    net = network()
    tmpl = raw_node(net, "0300")
    tmpl["pid"] = "0005"  # a wall transmitter: battery powered
    primary = tmpl["elements"][0]["models"]
    primary.append({"modelId": "1200", "bind": [0], "subscribe": ["FEFF", "C00F"]})
    primary.append({"modelId": "1102", "bind": [], "subscribe": ["FEFF"]})
    cdb = load(net)
    plan = commission.plan(cdb, NEW, 2, node(cdb, 0x0300))
    assert ops(plan, "FinishConfiguration")[-1] == (
        "Config Model Subscription Add elem=0D10 address=FEFF model=1200"
    )
    assert "time keeper" in plan.steps[-2].evidence
    assert ops(plan, "SetBlacklistFilter") == ["Config Beacon Set disabled"]
    assert plan.phases()[-1] == "DisableProxy"
    assert ops(plan, "DisableProxy") == ["Config GATT Proxy Set disabled"]
    # an unbound model is not bound
    assert not any("model=1102" in s.text for s in plan.steps)


def test_publish_only_wiring_and_an_element_group_nothing_uses():
    net = network()
    key_a = raw_node(net, "0148")["elements"][1]["models"]
    vendor = next(m for m in key_a if m["modelId"] == "05271013")
    vendor["subscribe"] = []  # publishes to its element group, does not listen to it
    # the third element gets a group on the template, but none of its supported servers is wired to it
    net["groups"].append({"name": "element group #0x14A", "address": "C0F0"})
    cdb = load(net)
    plan = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE))
    assert ops(plan, "CreateElementGroups")[-1] == (
        "Config Model Publication Set elem=0D11 publish=C003 model=05271013 appkey=0 cred=0 ttl=255"
        " period=0/0 retx=0/0"
    )
    assert [g.element for g in plan.groups] == [NEW, NEW + 1]


def test_android_exports_get_decimal_element_group_names():
    net = network()
    for group in net["groups"]:
        if group["name"].startswith("element group #0x"):
            group["name"] = f"element group #{int(group['name'][15:], 16)}"
    cdb = load(net)
    plan = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE))
    assert [g.name for g in plan.groups] == [
        "element group #3344",
        "element group #3345",
    ]


def test_group_range_override_and_exhaustion(cdb: CDB):
    plan = commission.plan(
        cdb, NEW, 3, node(cdb, TEMPLATE), group_range=(0xD000, 0xD0FF)
    )
    assert [g.address for g in plan.groups] == [0xD000, 0xD001]
    with pytest.raises(ValueError, match=r"no free group address left in C005\.\.C005"):
        commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE), group_range=(0xC005, 0xC005))
    net = network()
    net["provisioners"] = []
    bare = load(net)
    with pytest.raises(ValueError, match="no provisioner group range"):
        commission.plan(bare, NEW, 3, node(bare, TEMPLATE))


def test_element_groups_from_the_top_of_the_range_when_asked(cdb: CDB):
    """Review-4 W4-2: Home Assistant without a provisioner of its own (`onboard` passes "top") takes the highest
    free groups of the app's range — the app does not know them and gives its next room the lowest free one."""
    plan = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE), policy="top")
    assert [g.address for g in plan.groups] == [0xC64B, 0xC64A]
    # a range reaching into the device-type groups stops below them
    plan = commission.plan(
        cdb, NEW, 3, node(cdb, TEMPLATE), group_range=(0xFE00, 0xFEFF), policy="top"
    )
    assert [g.address for g in plan.groups] == [0xFEF4, 0xFEF3]
    with pytest.raises(AllocationCrowded):
        commission.plan(
            cdb, NEW, 3, node(cdb, TEMPLATE), group_range=(0xC000, 0xC03F), policy="top"
        )


def test_templates_without_element_groups_need_no_group_range():
    net = network()
    net["provisioners"] = []
    net["groups"] = [
        g for g in net["groups"] if not g["name"].startswith("element group")
    ]
    cdb = load(net)
    plan = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE))
    assert plan.groups == []
    assert "CreateElementGroups" not in plan.phases()


def test_refuses_what_cannot_be_planned(cdb: CDB):
    template = node(cdb, TEMPLATE)
    with pytest.raises(ValueError, match=r"2 element.*has 3"):
        commission.plan(cdb, NEW, 2, template)
    with pytest.raises(
        ValueError, match=r"0171\.\.0173 are not free \(0172, 0173 in use or excluded\)"
    ):
        commission.plan(cdb, 0x0171, 3, template)
    with pytest.raises(ValueError, match=r"\(0002 in use or excluded\)"):
        commission.plan(cdb, 0x0002, 3, template)  # networkExclusions of the export
    with pytest.raises(ValueError, match=r"7FFE\.\.8000 are not free$"):
        commission.plan(cdb, 0x7FFE, 3, template)
    with pytest.raises(ValueError, match="not free"):
        commission.plan(cdb, 0, 3, template)
    with pytest.raises(ValueError, match="no AppKey 5"):
        commission.plan(cdb, NEW, 3, template, app_key_index=5)


def test_the_gaps_are_listed_with_their_reasons(cdb: CDB):
    plan = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE))
    assert plan.not_covered is commission.NOT_COVERED
    assert all(g.what and g.why for g in plan.not_covered)
    whats = " ".join(g.what for g in plan.not_covered)
    for topic in (
        "proxy filter",
        "InsertId",
        "Time Set",
        "detectors",
        "rocker",
        "relay",
    ):
        assert topic in whats


def test_element_groups_skip_every_group_the_export_uses():
    """Not only the CDB `groups[]`: a room the app keeps in `meta` alone, a room link, a cached key connection and
    a group some node still publishes or listens to are taken too (`export.group_addresses_in_use`, as for a new
    room) — the new node's element group would otherwise share their listeners."""
    net = network()
    before = load(net)
    plan = commission.plan(before, NEW, 3, node(before, TEMPLATE))
    assert [g.address for g in plan.groups] == [0xC002, 0xC003]
    socket = raw_node(net, "0172")["elements"][0]["models"][0]
    # a group entry another tool dropped while the node still listens / publishes to it
    socket["subscribe"] = [*socket.get("subscribe", []), "C006"]
    socket["publish"] = {**socket.get("publish", {}), "address": "C007"}
    meta = {
        "userGroups": [{"address": 0xC002, "name": "Attic"}],  # a room the CDB lacks
        "elementConnectionGroups": [{"groupAddress": "C003"}],
        "devices": [
            {
                "name": "key",
                "cachedGroupConnectionMetadata": [{"publishAddress": "C004"}],
            }
        ],
    }
    cdb = CDB.from_network(copy.deepcopy(net), meta)
    plan = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE))
    assert [g.address for g in plan.groups] == [0xC008, 0xC009]


def test_reserved_groups_are_never_allocated(cdb: CDB):
    """Review-4 D2: the element groups of a node Home Assistant provisioned but never recorded are in no export,
    yet that node may hold them: the next plan keeps clear of them."""
    template = node(cdb, TEMPLATE)
    first = commission.plan(cdb, NEW, 3, template, group_range=(0xD000, 0xD0FF))
    taken = {g.address for g in first.groups}
    second = commission.plan(
        cdb, NEW, 3, template, group_range=(0xD000, 0xD0FF), reserved_groups=taken
    )
    assert [g.address for g in second.groups] == [0xD002, 0xD003]
    assert not taken & {g.address for g in second.groups}
