"""jhmesh.commission: the app's post-provisioning Config sequence by the app's rules, planned from a template node of
the export and again from the node's own Composition Data (review-4 F4-13)."""

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

from .conftest import CDB_PATH, composition_params

NEW = 0x0D10
TEMPLATE = 0x0148  # push-button 1-gang with a light insert: element 0 load, 0x0149 / 0x014A keys
LIGHT_2G = 0x0232  # push-button 2-gang, CTL light on 0x0232 / 0x0233
SOCKET = 0x0172
PUCK = 0x0400  # a PP2 puck (product 0x0010) with two light outputs


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


def composition(template: Node, **kw: int) -> C.CompositionData:
    return C.decode_composition_data(composition_params(template, **kw))


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
        "RequestRequiredData",  # the caller's two: no Config message
        "SetTime",
        "CreateElementGroups",
        "FinishConfiguration",
    ]
    assert plan.time_server == NEW  # the primary element hosts the Time Server
    assert plan.device_class == "lamp"
    assert plan.composition is None
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


def test_the_bind_list_binds_what_the_node_has_bound_or_not():
    """The app binds every model of its list the node has (§3.3), whatever the template has bound; the messenger's
    models only where the template has them bound."""
    net = network()
    primary = raw_node(net, "0148")["elements"][0]["models"]
    for model in primary:
        if model["modelId"] in ("1000", "0002"):
            model["bind"] = []
    cdb = load(net)
    plan = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE))
    texts = ops(plan, "RequestCompositionData")
    assert "Config Model App Bind elem=0D10 appkey=0 model=1000" in texts
    assert "Config Model App Bind elem=0D10 appkey=0 model=0002" not in texts


def test_node_wide_settings_are_the_apps(cdb: CDB):
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
    assert "the app's value" in by_text["Config Default TTL Set ttl=5"]
    assert (
        "hidden-features.md §4"
        in by_text["Config Network Transmit Set count=2 steps=9"]
    )
    assert "on unless a battery device" in by_text["Config Beacon Set enabled"]


def test_node_wide_settings_ignore_what_the_template_holds():
    """`setconfiguration`: the app sends its own values, not another node's (one Home Assistant or a user changed)."""
    net = network()
    tmpl = raw_node(net, "0148")
    tmpl.update(
        defaultTTL=7,
        features={"relay": 0, "proxy": 1, "friend": 2, "lowPower": 2},
        relayRetransmit={"count": 4, "interval": 200},
        networkTransmit={"count": 5, "interval": 300},
        secureNetworkBeacon=False,
    )
    cdb = load(net)
    plan = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE))
    assert ops(plan, "SetConfiguration")[2:] == [
        "Config Default TTL Set ttl=5",
        "Config Relay Set enabled retx=2/8",
        "Config Network Transmit Set count=2 steps=9",
    ]
    assert ops(plan, "SetBlacklistFilter") == ["Config Beacon Set enabled"]


def test_element_groups_by_the_apps_rule(cdb: CDB):
    plan = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE))
    # C000 / C001 are taken, C002 is the lowest free address of the phone's range C000..C64B; every element with a
    # supported server gets one, as on the installation (the key element 0x014A included)
    assert [(g.address, g.name, g.element) for g in plan.groups] == [
        (0xC002, "element group #0xD10", NEW),
        (0xC003, "element group #0xD11", NEW + 1),
        (0xC004, "element group #0xD12", NEW + 2),
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
        C.model_publication_set(NEW + 2, 0xC004, "05271013")[1:],
        C.model_subscription_add(NEW + 2, 0xC004, "05271013")[2:],
    ]
    assert [s.expect for s in steps[:2]] == [
        C.CONFIG_MODEL_PUBLICATION_STATUS,
        C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
    ]
    texts = " ".join(s.text for s in plan.steps)
    # user configuration of the template is not copied: its room (C00F), its key connection to the gateway (C005)
    assert "C00F" not in texts
    assert "C005" not in texts
    # neither the LBC Admin server (never wired on the installation) nor the Sensor Server
    assert "model=05271011" not in " ".join(ops(plan, "CreateElementGroups"))


def test_the_rule_not_the_templates_wiring():
    """A template wired otherwise (a server publishing only, an element group nothing uses) changes nothing."""
    net = network()
    key_a = raw_node(net, "0148")["elements"][1]["models"]
    vendor = next(m for m in key_a if m["modelId"] == "05271013")
    vendor["subscribe"] = []
    net["groups"].append({"name": "element group #0x14A", "address": "C0F0"})
    cdb = load(net)
    plan = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE))
    assert ops(plan, "CreateElementGroups")[6:8] == [
        (
            "Config Model Publication Set elem=0D11 publish=C003 model=05271013 appkey=0 cred=0 ttl=255"
            " period=0/0 retx=0/0"
        ),
        "Config Model Subscription Add elem=0D11 address=C003 model=05271013",
    ]


def test_device_type_groups_and_the_sensor_server(cdb: CDB):
    lamp = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE))
    assert ops(lamp, "FinishConfiguration") == [
        "Config Model Subscription Add elem=0D10 address=FEF5 model=1000",
        "Config Model Subscription Add elem=0D10 address=FEF5 model=1203",
    ]
    socket = commission.plan(cdb, NEW, 2, node(cdb, SOCKET))
    assert socket.device_class == "socket"
    assert ops(socket, "FinishConfiguration") == [
        "Config Model Subscription Add elem=0D10 address=FEF8 model=1000",
        "Config Model Subscription Add elem=0D10 address=FEF8 model=1203",
    ]
    # the power sensor's Sensor Server: not part of the set-up; its element gets a group for its LBC server
    texts = [s.text for s in socket.steps if s.phase == "CreateElementGroups"]
    assert not any("model=1100" in t for t in texts)
    assert [g.element for g in socket.groups] == [NEW, NEW + 1]
    # a tunable-white light: both load elements, every SIG supported server, never a vendor one
    ctl = commission.plan(cdb, NEW, 5, node(cdb, LIGHT_2G))
    assert ops(ctl, "FinishConfiguration") == [
        f"Config Model Subscription Add elem={a} address=FEF5 model={m}"
        for a, m in (
            ("0D10", "1000"),
            ("0D10", "1002"),
            ("0D10", "1300"),
            ("0D10", "1303"),
            ("0D10", "1203"),
            ("0D11", "1002"),
            ("0D11", "1306"),
        )
    ]


def test_a_room_thermostat_and_blinds():
    net = network()
    raw_node(net, "0172")["pid"] = "000A"
    cdb = load(net)
    rtr = commission.plan(cdb, NEW, 2, node(cdb, SOCKET))
    assert rtr.device_class == "rtr"
    assert {t.split()[5] for t in ops(rtr, "FinishConfiguration")} == {"address=FEF9"}
    # a 2-gang push-button with a blinds insert: the position element (its first level server) and the slats (the
    # last one)
    cdb = load(network())
    blind = commission.plan(cdb, NEW, 5, node(cdb, LIGHT_2G), function=5)
    assert blind.device_class == "blind"
    finish = ops(blind, "FinishConfiguration")
    assert [t for t in finish if "FEF7" in t] == [
        "Config Model Subscription Add elem=0D11 address=FEF7 model=1002",
        "Config Model Subscription Add elem=0D11 address=FEF7 model=1306",
    ]
    assert all("elem=0D10" in t for t in finish if "FEF6" in t)
    # one level element: no slats; none at all: no device-type group
    net = network()
    raw_node(net, "0232")["elements"][1]["models"] = [
        m
        for m in raw_node(net, "0232")["elements"][1]["models"]
        if m["modelId"] != "1002"
    ]
    cdb = load(net)
    single = commission.plan(cdb, NEW, 5, node(cdb, LIGHT_2G), function=5)
    assert not any("FEF7" in t for t in ops(single, "FinishConfiguration"))
    flat = commission.plan(cdb, NEW, 3, node(cdb, TEMPLATE), function=5)
    assert "FinishConfiguration" not in flat.phases()


@pytest.mark.parametrize(
    ("pid", "function", "models", "kind"),
    [
        (0x000A, None, (), "rtr"),
        (0x0003, None, (), "socket"),
        (0x000C, None, (), "socket"),
        (0x000D, None, (), "blind"),
        (0x0013, None, (), "blind"),
        (0x0004, None, (), "lamp"),
        (0x0012, None, (), "lamp"),
        (0x000B, None, ("1000",), None),  # the gateway
        (0x0007, None, ("1000",), None),  # a detector
        (0x0005, None, (), None),  # a wall transmitter
        (None, None, ("1000",), None),  # a phone
        (0x0001, 5, ("1000",), "blind"),
        (0x0001, 0, (), "lamp"),
        (0x0002, 4, (), "lamp"),
        (0x0001, 6, ("1000",), None),  # an extension insert
        (0x0001, None, ("1000",), "lamp"),  # no InsertId: the composition decides
        (0x0001, None, ("1002",), "blind"),
        (0x0001, None, ("1002", "1300"), "lamp"),
        (0x0001, None, ("1203",), None),
    ],
)
def test_device_class(
    pid: int | None, function: int | None, models: tuple[str, ...], kind: str | None
):
    shape = [commission.Shape(0x0001, models), commission.Shape(0x0040, ("1000",))]
    assert commission.device_class(pid, function, shape) == kind


def test_pp2_time_keeper_subscription_and_battery_devices():
    cdb = load(network())
    puck = commission.plan(cdb, NEW, 2, node(cdb, PUCK))
    assert ops(puck, "FinishConfiguration")[-1] == (
        "Config Model Subscription Add elem=0D10 address=FEFF model=1200"
    )
    assert "time keeper" in puck.steps[-1].evidence
    # a puck without a Time Server: nothing for the time keeper
    net = network()
    for element in raw_node(net, "0400")["elements"]:
        element["models"] = [m for m in element["models"] if m["modelId"] != "1200"]
    cdb = load(net)
    bare = commission.plan(cdb, NEW, 2, node(cdb, PUCK))
    assert bare.time_server is None
    assert not any("FEFF" in t for t in ops(bare, "FinishConfiguration"))
    # a wall transmitter: battery powered, no device-type group, its proxy off at the end
    net = network()
    raw_node(net, "0300")["pid"] = "0005"
    cdb = load(net)
    plan = commission.plan(cdb, NEW, 2, node(cdb, 0x0300))
    assert ops(plan, "SetBlacklistFilter") == ["Config Beacon Set disabled"]
    assert "FinishConfiguration" not in plan.phases()
    assert plan.phases()[-1] == "DisableProxy"
    assert ops(plan, "DisableProxy") == ["Config GATT Proxy Set disabled"]


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
        "element group #3346",
    ]


def test_group_range_override_and_exhaustion(cdb: CDB):
    plan = commission.plan(
        cdb, NEW, 3, node(cdb, TEMPLATE), group_range=(0xD000, 0xD0FF)
    )
    assert [g.address for g in plan.groups] == [0xD000, 0xD001, 0xD002]
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
    assert [g.address for g in plan.groups] == [0xC64B, 0xC64A, 0xC649]
    # a range reaching into the device-type groups stops below them
    plan = commission.plan(
        cdb, NEW, 3, node(cdb, TEMPLATE), group_range=(0xFE00, 0xFEFF), policy="top"
    )
    assert [g.address for g in plan.groups] == [0xFEF4, 0xFEF3, 0xFEF2]
    with pytest.raises(AllocationCrowded):
        commission.plan(
            cdb, NEW, 3, node(cdb, TEMPLATE), group_range=(0xC000, 0xC03F), policy="top"
        )


def test_elements_without_a_supported_server_need_no_group():
    net = network()
    net["provisioners"] = []
    for element in raw_node(net, "0148")["elements"]:
        element["models"] = [
            m
            for m in element["models"]
            if m["modelId"] not in commission.SUPPORTED_SERVERS
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
        "time keeper",
    ):
        assert topic in whats


def test_element_groups_skip_every_group_the_export_uses():
    """Not only the CDB `groups[]`: a room the app keeps in `meta` alone, a room link, a cached key connection and
    a group some node still publishes or listens to are taken too (`export.group_addresses_in_use`, as for a new
    room) — the new node's element group would otherwise share their listeners."""
    net = network()
    before = load(net)
    plan = commission.plan(before, NEW, 3, node(before, TEMPLATE))
    assert [g.address for g in plan.groups] == [0xC002, 0xC003, 0xC004]
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
    assert [g.address for g in plan.groups] == [0xC008, 0xC009, 0xC00A]


def test_reserved_groups_are_never_allocated(cdb: CDB):
    """Review-4 D2: the element groups of a node Home Assistant provisioned but never recorded are in no export,
    yet that node may hold them: the next plan keeps clear of them."""
    template = node(cdb, TEMPLATE)
    first = commission.plan(cdb, NEW, 3, template, group_range=(0xD000, 0xD0FF))
    taken = {g.address for g in first.groups}
    second = commission.plan(
        cdb, NEW, 3, template, group_range=(0xD000, 0xD0FF), reserved_groups=taken
    )
    assert [g.address for g in second.groups] == [0xD003, 0xD004, 0xD005]
    assert not taken & {g.address for g in second.groups}


# ----------------------------------------------------------------------------- planned again from the node's answer


def test_resume_plans_from_the_nodes_composition_with_the_same_groups(cdb: CDB):
    template = node(cdb, TEMPLATE)
    plan = commission.plan(cdb, NEW, 3, template, policy="top")
    received = composition(template)
    resumed = plan.resume(received)
    assert resumed.composition is received
    assert resumed.groups == plan.groups
    assert resumed.time_server == plan.time_server
    # the node lists its SIG models before its vendor ones: the same messages
    assert sorted(s.text for s in resumed.steps) == sorted(s.text for s in plan.steps)
    assert resumed.steps[:2] == plan.steps[:2]
    # resumed twice (a retry): still the first plan's groups
    assert plan.resume(received).groups == plan.groups


def test_resume_keeps_the_groups_although_the_export_moved_on(cdb: CDB):
    """The groups went into the vault before the device got its keys: the resumed plan never allocates anew."""
    template = node(cdb, TEMPLATE)
    plan = commission.plan(cdb, NEW, 3, template)
    for group in plan.groups:
        cdb.groups[group.address] = "Someone else's room"
    assert plan.resume(composition(template)).groups == plan.groups


@pytest.mark.parametrize(
    ("change", "difference"),
    [
        ({"cid": 0x0059}, "company 0059, not 0527"),
        ({"pid": 0x0002}, "product 0002, not 0001"),
    ],
)
def test_another_product_is_refused(cdb: CDB, change: dict[str, int], difference: str):
    template = node(cdb, TEMPLATE)
    plan = commission.plan(cdb, NEW, 3, template)
    with pytest.raises(commission.CompositionMismatch) as err:
        plan.resume(composition(template, **change))
    assert err.value.differences == [difference]
    assert "template 0148" in str(err.value)


def test_other_elements_or_models_are_refused(cdb: CDB):
    template = node(cdb, TEMPLATE)
    plan = commission.plan(cdb, NEW, 3, template)
    net = network()
    raw = raw_node(net, "0148")
    raw["elements"][1]["location"] = "0041"
    raw["elements"][2]["models"].append(
        {"modelId": "1102", "bind": [], "subscribe": []}
    )
    raw["elements"][2]["models"] = [
        m for m in raw["elements"][2]["models"] if m["modelId"] != "05271015"
    ]
    other = node(load(net), TEMPLATE)
    with pytest.raises(commission.CompositionMismatch) as err:
        plan.resume(C.decode_composition_data(composition_params(other)))
    assert err.value.differences == [
        "element 1 at location 0041, not 0040",
        "element 2 models +1102 -05271015",
    ]
    del raw["elements"][2]
    fewer = node(load(net), TEMPLATE)
    with pytest.raises(commission.CompositionMismatch, match=r"2 element\(s\), not 3"):
        plan.resume(C.decode_composition_data(composition_params(fewer)))


def test_a_node_without_the_apps_required_servers_is_refused():
    """The app's `t1()` after the Composition Data: 0x1012 and 0x0527:1012 (its `NodeNotConfigured`)."""
    net = network()
    for element in raw_node(net, "0148")["elements"]:
        element["models"] = [
            m for m in element["models"] if m["modelId"] not in ("1012", "05271012")
        ]
    cdb = load(net)
    template = node(cdb, TEMPLATE)
    plan = commission.plan(cdb, NEW, 3, template)
    with pytest.raises(commission.CompositionMismatch) as err:
        plan.resume(composition(template))
    assert err.value.differences == ["no model 1012", "no model 05271012"]


def test_a_template_without_company_or_product_compares_the_elements_only(cdb: CDB):
    template = node(cdb, TEMPLATE)
    template.cid, template.pid = None, None
    plan = commission.plan(cdb, NEW, 3, template)
    assert plan.device_class is None
    resumed = plan.resume(composition(template, cid=0x0527, pid=0x0001))
    assert resumed.groups == plan.groups
