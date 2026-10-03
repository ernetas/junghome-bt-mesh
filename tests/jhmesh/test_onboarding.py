"""jhmesh.onboarding: a new node addressed, commissioned, read back and recorded (review-3 N3)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from jhmesh import audit as A
from jhmesh import commission
from jhmesh import config_messages as C
from jhmesh.cdb import InvalidExport
from jhmesh.export import ExportError, ProjectFile
from jhmesh.onboarding import (
    CommissioningError,
    DeviceCount,
    free_unicast_block,
    missing_devices,
    node_entry,
    node_for,
    record,
)
from jhmesh.onboarding import (
    commission as run_commission,
)
from jhmesh.pdu import decode_opcode, encode_opcode

from .conftest import LIGHT_2G, FakeBleak, FastAsyncio, composition_params

if TYPE_CHECKING:
    from jhmesh.cdb import CDB, Node
    from jhmesh.client import ProxyClient

NEW_UUID = "11111111-2222-4333-8444-555555555555"
NEW_KEY = bytes(range(0xA0, 0xB0))
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# the Config statuses that carry a status code before the request's fields
WITH_CODE = {
    C.CONFIG_APPKEY_STATUS,
    C.CONFIG_MODEL_APP_STATUS,
    C.CONFIG_MODEL_PUBLICATION_STATUS,
    C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
}


class FreshNode:
    """A factory-fresh node's Configuration Server: Success to every Set, and afterwards what it was told."""

    def __init__(
        self, plan: commission.Plan, template: Node, refuse: int | None = None
    ) -> None:
        self.expect = {decode_opcode(s.pdu)[0]: s.expect for s in plan.steps}
        self.composition = composition_params(
            template
        )  # the template's: the same product
        self.refuse = refuse  # the request opcode answered with status 0x02 (Invalid AppKey Index)
        self.silent: int | None = None
        self.seen: list[int] = []

    def __call__(self, node: int, access: bytes) -> bytes | None:
        op, _cid, params = decode_opcode(access)
        self.seen.append(op)
        if op == self.silent:
            return None
        status = self.expect[op]
        if status in WITH_CODE:
            code = b"\x02" if op == self.refuse else b"\x00"
            body = params[:3] if status == C.CONFIG_APPKEY_STATUS else params
            return encode_opcode(status) + code + body
        if status == C.CONFIG_COMPOSITION_DATA_STATUS:
            return encode_opcode(status) + self.composition
        return encode_opcode(status) + params


def template_of(cdb: CDB) -> Any:
    node = cdb.node_by_addr(LIGHT_2G)
    assert node is not None
    return node


def test_free_unicast_block_stays_clear_of_every_use(cdb: CDB) -> None:
    assert free_unicast_block(cdb, 2) == 0x7FFE
    assert free_unicast_block(cdb, 2, avoid=[0x7FFF]) == 0x7FFD  # 7FFD..7FFE
    cdb.provisioner_unicast_ranges.append((0x0001, 0x7FFF))
    assert free_unicast_block(cdb, 1) is None


def test_node_for_follows_the_template(cdb: CDB) -> None:
    template = template_of(cdb)
    node = node_for(
        template, uuid=NEW_UUID, unicast=0x7FFE, dev_key=NEW_KEY, name="New"
    )
    assert node.uuid == NEW_UUID.upper()
    assert [e.address for e in node.elements] == [
        0x7FFE + i for i in range(len(template.elements))
    ]
    assert node.pid == template.pid
    assert node.elements[0].raw_models is not template.elements[0].raw_models


def told(plan: commission.Plan) -> dict[tuple[int, str], dict[str, Any]]:
    """What the plan told each model: its publication, subscriptions and AppKey binding."""
    answers: dict[tuple[int, str], dict[str, Any]] = {}
    for step in plan.steps:
        op, _cid, p = decode_opcode(step.pdu)
        if op == C.CONFIG_MODEL_SUBSCRIPTION_ADD:
            element = int.from_bytes(p[:2], "little")
            model = C.model_id_str(C.decode_model_id(p[4:]))
            answers.setdefault((element, model), {}).setdefault("subscribe", []).append(
                int.from_bytes(p[2:4], "little")
            )
        if op == C.CONFIG_MODEL_PUBLICATION_SET:
            element = int.from_bytes(p[:2], "little")
            model = C.model_id_str(C.decode_model_id(p[9:]))
            answers.setdefault((element, model), {})["publish"] = int.from_bytes(
                p[2:4], "little"
            )
        if op == C.CONFIG_MODEL_APP_BIND:
            element = int.from_bytes(p[:2], "little")
            model = C.model_id_str(C.decode_model_id(p[4:]))
            answers.setdefault((element, model), {})["bind"] = [0]
    return answers


def read_back(
    unicast: int, answers: dict[tuple[int, str], dict[str, Any]]
) -> A.NodeAudit:
    """The audit's read-back of a node that holds `answers`, plus one model whose Gets went unanswered."""
    audit = A.NodeAudit(node=unicast, name="New", answered=True)
    for (element, model), got in answers.items():
        audit.models.append(
            A.ModelAudit(
                element,
                model,
                0,
                (),
                (),
                node_publish=got.get("publish", 0),
                node_subscribe=tuple(got.get("subscribe", ())),
                node_app_keys=tuple(got.get("bind", ())),
            )
        )
    audit.models.append(
        A.ModelAudit(unicast, "1000", 0, (), ())
    )  # unanswered: stays empty
    return audit


async def commissioned(
    proxy: ProxyClient, link: FakeBleak, plan: commission.Plan, template: Node
) -> list[str]:
    """Run the plan against a fresh node that accepts everything; return the phases announced."""
    fresh = FreshNode(plan, template)
    link.auto_ack()
    link.auto_config(fresh)
    phases: list[str] = []

    async def on_phase(phase: str) -> None:
        phases.append(phase)

    done = await run_commission(proxy, plan, timeout=0.2, retries=1, on_phase=on_phase)
    assert len(fresh.seen) == len(plan.steps)
    assert done.composition is not None  # planned again from the node's answer
    assert done.groups == plan.groups
    return phases


async def test_a_new_node_is_commissioned_read_back_and_recorded(
    cdb: CDB, proxy: ProxyClient, link: FakeBleak, fast: FastAsyncio, tmp_path: Path
) -> None:
    template = template_of(cdb)
    count = len(template.elements)
    unicast = free_unicast_block(cdb, count)
    assert unicast is not None
    plan = commission.plan(cdb, unicast, count, template)
    node = node_for(
        template, uuid=NEW_UUID, unicast=unicast, dev_key=NEW_KEY, name="New"
    )
    await proxy.attach(link)
    proxy.add_node(node)
    proxy.add_node(node)  # twice: known once
    assert cdb.nodes.count(node) == 1
    phases = await commissioned(proxy, link, plan, template)
    assert phases == plan.phases()
    assert phases.index("SetTime") == phases.index("SetBlacklistFilter") + 2

    answers = told(plan)
    audit = read_back(unicast, answers)

    pf = ProjectFile.load(FIXTURES / "JungHome.json")
    template_raw = next(
        n for n in pf.net["nodes"] if n["unicastAddress"] == f"{template.unicast:04X}"
    )
    entry = node_entry(
        template_raw, uuid=NEW_UUID, unicast=unicast, dev_key=NEW_KEY, name="New"
    )
    assert entry["deviceKey"] == NEW_KEY.hex().upper()
    assert all(
        m["subscribe"] == [] and m["bind"] == [] and "publish" not in m
        for e in entry["elements"]
        for m in e["models"]
    )
    template_pf = pf.cdb.node_by_addr(template.unicast)
    assert template_pf is not None
    added = record(pf, template_pf, entry, audit, plan, "New")
    assert pf.cdb.node_by_addr(unicast) is added
    for (element, model), got in answers.items():
        el = pf.cdb.element(element)
        assert el is not None
        raw = next(m for m in el.raw_models if m["modelId"] == model)
        if "publish" in got:
            assert pf.publication(element, model) == got["publish"]
        if "subscribe" in got:
            assert sorted(el.subscriptions(model)) == sorted(got["subscribe"])
        if "bind" in got:
            assert raw["bind"] == [0]
    assert {g.address for g in plan.groups} <= set(pf.cdb.groups)
    rows = [
        d
        for d in pf.meta["devices"]
        if isinstance(d.get("deviceId"), dict)
        and d["deviceId"]["nodeId"].upper() == NEW_UUID.upper()
    ]
    assert rows
    assert rows[0]["name"].startswith("New")
    out = tmp_path / "export.json"
    pf.save(out)
    assert ProjectFile.load(out).cdb.node_by_addr(unicast) is not None
    # the same node twice, or an entry the export cannot hold: refused, the file unchanged
    with pytest.raises(ExportError, match="already"):
        pf.add_node_entry(copy.deepcopy(entry))
    broken = copy.deepcopy(entry)
    broken["UUID"] = "22222222-2222-4222-8222-222222222222"
    broken["unicastAddress"] = "zz"
    count_before = len(pf.net["nodes"])
    with pytest.raises(InvalidExport):
        pf.add_node_entry(broken, [(0xC0EE, "x")])
    assert len(pf.net["nodes"]) == count_before


async def test_a_refused_or_unanswered_step_stops_the_plan(
    cdb: CDB, proxy: ProxyClient, link: FakeBleak, fast: FastAsyncio
) -> None:
    template = template_of(cdb)
    count = len(template.elements)
    unicast = free_unicast_block(cdb, count)
    assert unicast is not None
    plan = commission.plan(cdb, unicast, count, template)
    node = node_for(
        template, uuid=NEW_UUID, unicast=unicast, dev_key=NEW_KEY, name="New"
    )
    await proxy.attach(link)
    proxy.add_node(node)
    fresh = FreshNode(plan, template, refuse=C.CONFIG_MODEL_APP_BIND)
    link.auto_ack()
    link.auto_config(fresh)
    with pytest.raises(CommissioningError, match="refused") as err:
        await run_commission(proxy, plan, timeout=0.2, retries=1)
    assert err.value.phase == "RequestCompositionData"
    fresh.refuse, fresh.silent = None, C.CONFIG_APPKEY_ADD
    with pytest.raises(CommissioningError, match="no answer") as err:
        await run_commission(proxy, plan, timeout=0.05, retries=1)
    assert err.value.phase == "SetWhitelistFilter"
    # another product answering: stopped right after its Composition Data, before any binding
    fresh.silent = None
    fresh.composition = composition_params(template, pid=0x0001)
    fresh.seen.clear()
    with pytest.raises(CommissioningError, match="product 0001, not 0002") as err:
        await run_commission(proxy, plan, timeout=0.2, retries=1)
    assert err.value.phase == "RequestCompositionData"
    assert C.CONFIG_MODEL_APP_BIND not in fresh.seen
    fresh.composition = b"\x01" + bytes(10)  # page 1: not one the plan understands
    with pytest.raises(CommissioningError, match="page 1"):
        await run_commission(proxy, plan, timeout=0.2, retries=1)


async def test_the_callers_phase_can_stop_the_commissioning(
    cdb: CDB, proxy: ProxyClient, link: FakeBleak, fast: FastAsyncio
) -> None:
    """The InsertId read goes in `RequestRequiredData`: what the caller raises there ends the commissioning before
    any element group is wired."""
    template = template_of(cdb)
    count = len(template.elements)
    unicast = free_unicast_block(cdb, count)
    assert unicast is not None
    plan = commission.plan(cdb, unicast, count, template)
    await proxy.attach(link)
    proxy.add_node(
        node_for(template, uuid=NEW_UUID, unicast=unicast, dev_key=NEW_KEY, name="New")
    )
    fresh = FreshNode(plan, template)
    link.auto_ack()
    link.auto_config(fresh)

    async def refuse(phase: str) -> None:
        if phase == "RequestRequiredData":
            raise CommissioningError("another insert", phase)

    with pytest.raises(CommissioningError, match="another insert"):
        await run_commission(proxy, plan, timeout=0.2, retries=1, on_phase=refuse)
    assert C.CONFIG_MODEL_PUBLICATION_SET not in fresh.seen


async def test_the_callers_phases_come_last_when_no_step_follows(
    proxy: ProxyClient, link: FakeBleak, fast: FastAsyncio
) -> None:
    """A node without element groups or device-type groups: the caller still gets its two phases, at the end."""
    pf = ProjectFile.load(FIXTURES / "JungHome.json")
    raw = next(n for n in pf.net["nodes"] if n["unicastAddress"] == "0148")
    for element in raw["elements"]:
        element["models"] = [
            m
            for m in element["models"]
            if m["modelId"] not in commission.SUPPORTED_SERVERS
        ]
    pf.cdb = type(pf.cdb).from_network(pf.net, pf.meta)
    template = pf.cdb.node_by_addr(0x0148)
    assert template is not None
    plan = commission.plan(pf.cdb, 0x7F00, 3, template)
    assert plan.phases()[-2:] == ["RequestRequiredData", "SetTime"]
    await proxy.attach(link)
    proxy.add_node(
        node_for(template, uuid=NEW_UUID, unicast=0x7F00, dev_key=NEW_KEY, name="New")
    )
    link.auto_ack()
    link.auto_config(FreshNode(plan, template))
    seen: list[str] = []

    async def on_phase(phase: str) -> None:
        seen.append(phase)

    await run_commission(proxy, plan, timeout=0.2, retries=1, on_phase=on_phase)
    assert seen == plan.phases()


def test_node_entry_keeps_the_templates_uuid_style(cdb: CDB) -> None:
    raw = {
        "UUID": "00005EFFFE0053140000000000000000",
        "elements": [{"models": [{"modelId": "1000"}]}],
        "heartbeatPub": {},
    }
    entry = node_entry(raw, uuid=NEW_UUID, unicast=0x7000, dev_key=NEW_KEY, name="x")
    assert entry["UUID"] == NEW_UUID.upper().replace("-", "")
    assert "heartbeatPub" not in entry


def test_the_templates_device_rows_are_cloned(cdb: CDB) -> None:
    """A template with app device rows: the new node gets the same split into devices, named after it."""
    pf = ProjectFile.load(FIXTURES / "JungHome.json")
    template = pf.cdb.node_by_addr(0x0148)
    assert template is not None
    raw = next(n for n in pf.net["nodes"] if n["unicastAddress"] == "0148")
    entry = node_entry(raw, uuid=NEW_UUID, unicast=0x7F00, dev_key=NEW_KEY, name="Hall")
    node = pf.add_node_entry(entry)
    assert pf.clone_device_rows(template, node, "Hall") >= 1
    rows = [
        d
        for d in pf.meta["devices"]
        if isinstance(d.get("deviceId"), dict)
        and d["deviceId"]["nodeId"].upper() == NEW_UUID.upper()
    ]
    assert rows[0]["name"].startswith("Hall")
    assert rows[0]["cachedGroupConnectionMetadata"] == []


def test_record_with_a_template_the_app_has_a_row_for(cdb: CDB) -> None:
    """The template's rows are cloned (not a guessed one); a row without MAC or connection cache clones as it is."""
    pf = ProjectFile.load(FIXTURES / "JungHome.json")
    template = pf.cdb.node_by_addr(0x0148)
    assert template is not None
    for row in pf.meta["devices"]:
        row.pop("macAddress", None)
        row.pop("cachedGroupConnectionMetadata", None)
    raw = next(n for n in pf.net["nodes"] if n["unicastAddress"] == "0148")
    entry = node_entry(raw, uuid=NEW_UUID, unicast=0x7F00, dev_key=NEW_KEY, name="Hall")
    plan = commission.Plan(0x7F00, len(template.elements), 0x0148, [], [])
    audit = A.NodeAudit(node=0x7F00, name="Hall", answered=True)
    node = record(pf, template, entry, audit, plan, "Hall")
    rows = [
        d
        for d in pf.meta["devices"]
        if isinstance(d.get("deviceId"), dict)
        and d["deviceId"]["nodeId"].upper() == NEW_UUID.upper()
    ]
    assert rows
    assert "macAddress" not in rows[0]
    assert pf.cdb.node_by_addr(0x7F00) is node


def _rows_of(pf: ProjectFile, uuid: str) -> list[dict[str, Any]]:
    return [
        d
        for d in pf.meta["devices"]
        if isinstance(d.get("deviceId"), dict)
        and d["deviceId"]["nodeId"].upper() == uuid.upper()
    ]


def test_the_new_nodes_rows_carry_the_function_it_advertised(cdb: CDB) -> None:
    """F4-12: a push-button takes any insert, so its rows say the insert it advertised, not its template's; the
    app's missing-devices check counts its rows against what that insert calls for."""
    pf = ProjectFile.load(FIXTURES / "JungHome.json")
    template = pf.cdb.node_by_addr(
        0x0148
    )  # one row only: the share fixture names its load alone
    assert template is not None
    raw = next(n for n in pf.net["nodes"] if n["unicastAddress"] == "0148")
    entry = node_entry(raw, uuid=NEW_UUID, unicast=0x7F00, dev_key=NEW_KEY, name="Hall")
    plan = commission.Plan(0x7F00, len(template.elements), 0x0148, [], [])
    audit = A.NodeAudit(node=0x7F00, name="Hall", answered=True)
    node = record(pf, template, entry, audit, plan, "Hall", 5)
    rows = _rows_of(pf, NEW_UUID)
    assert [r["deviceId"]["actuatorFunctionId"] for r in rows] == [5]
    assert missing_devices(pf, node, 5) == DeviceCount(recorded=1, expected=2)
    assert missing_devices(pf, node, 6) is None  # an extension insert: the keys alone
    assert missing_devices(pf, node, 0xFFFF) is None  # no count for an unset function


def test_a_template_without_rows_gets_one_with_the_advertised_function(
    cdb: CDB,
) -> None:
    pf = ProjectFile.load(FIXTURES / "JungHome.json")
    template = pf.cdb.node_by_addr(0x0232)  # no app device row in the share fixture
    assert template is not None
    raw = next(n for n in pf.net["nodes"] if n["unicastAddress"] == "0232")
    entry = node_entry(raw, uuid=NEW_UUID, unicast=0x7F00, dev_key=NEW_KEY, name="Hall")
    plan = commission.Plan(0x7F00, len(template.elements), 0x0232, [], [])
    audit = A.NodeAudit(node=0x7F00, name="Hall", answered=True)
    record(pf, template, entry, audit, plan, "Hall", 5)
    assert [r["deviceId"]["actuatorFunctionId"] for r in _rows_of(pf, NEW_UUID)] == [5]
    # without one, the composition's guess (`export.guess_actuator_function`) stays
    other = "11111111-2222-4333-8444-666666666666"
    entry = node_entry(raw, uuid=other, unicast=0x7E00, dev_key=NEW_KEY, name="Den")
    record(
        pf, template, entry, audit, commission.Plan(0x7E00, 5, 0x0232, [], []), "Den"
    )
    assert [r["deviceId"]["actuatorFunctionId"] for r in _rows_of(pf, other)] == [4]


def test_the_new_node_gets_the_apps_insert_and_layout_rows(cdb: CDB) -> None:
    """Review-4 F4-6: the template's `actuatorExports` / `buttonLayoutExports` rows, on the new node's own element,
    with the insert and the layout it advertised; without an advert, the template's."""
    pf = ProjectFile.load(FIXTURES / "JungHome-android.json")
    template = pf.cdb.node_by_addr(0x0148)
    assert template is not None
    raw = next(n for n in pf.net["nodes"] if n["unicastAddress"] == "0148")
    entry = node_entry(raw, uuid=NEW_UUID, unicast=0x7F00, dev_key=NEW_KEY, name="Hall")
    plan = commission.Plan(0x7F00, len(template.elements), 0x0148, [], [])
    audit = A.NodeAudit(node=0x7F00, name="Hall", answered=True)
    record(pf, template, entry, audit, plan, "Hall", 2, 0)
    assert pf.meta["actuatorExports"][-1] == {
        "actuatorId": {"actuatorFunctionId": 2, "insertType": 2},
        "elementAddress": 0x7F00,
    }
    assert pf.meta["buttonLayoutExports"][-1] == {"mode": 0, "elementAddress": 0x7F00}
    other = "11111111-2222-4333-8444-666666666666"
    entry = node_entry(raw, uuid=other, unicast=0x7E00, dev_key=NEW_KEY, name="Den")
    plan = commission.Plan(0x7E00, len(template.elements), 0x0148, [], [])
    record(pf, template, entry, audit, plan, "Den")
    assert pf.meta["actuatorExports"][-1] == {
        "actuatorId": {"actuatorFunctionId": 0, "insertType": 2},
        "elementAddress": 0x7E00,
    }
    assert pf.meta["buttonLayoutExports"][-1] == {"mode": 1, "elementAddress": 0x7E00}


# ----------------------------------------------------------------------------- the client's part (review-4 D2, P4-6)


async def test_beacon_seen_is_per_link(proxy: ProxyClient, cdb: CDB) -> None:
    """`add_device` provisions only with an IV index a beacon confirmed on the current link."""
    assert not proxy.beacon_seen
    link = FakeBleak(cdb)
    link.beacon_on_subscribe = True
    await proxy.attach(link, beacon_wait=1.0)
    assert proxy.beacon_seen
    await proxy.detach()
    quiet = FakeBleak(cdb)
    quiet.beacon_on_subscribe = False
    await proxy.attach(quiet)
    assert not proxy.beacon_seen  # the previous link's beacon does not count
    await proxy.detach()


def test_forget_sources_clears_the_replay_protection_of_new_addresses(
    proxy: ProxyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node newly placed at addresses a reset node sent from starts at SEQ 0: what the replay list remembers
    for them would drop its replies, so it goes (and is written); everything else stays."""
    writes: list[int] = []
    monkeypatch.setattr(proxy.state, "persist", lambda: writes.append(1))
    proxy.state.rpl = {0x7FFE: (0, 500), 0x7FFF: (0, 9), 0x0148: (0, 70)}
    proxy._seq_auth_done[0x7FFE] = (0, 480)
    proxy._seq_auth_done[0x0148] = (0, 60)
    proxy.forget_sources(range(0x7FFE, 0x8000))
    assert proxy.state.rpl == {0x0148: (0, 70)}
    assert proxy._seq_auth_done == {0x0148: (0, 60)}
    assert writes == [1]
    proxy.forget_sources([0x7FFE])  # nothing left to forget: nothing written
    assert writes == [1]


def test_remove_node_undoes_add_node(proxy: ProxyClient, cdb: CDB) -> None:
    template = template_of(cdb)
    new = node_for(template, uuid=NEW_UUID, unicast=0x7FFE, dev_key=NEW_KEY, name="New")
    proxy.add_node(new)
    assert proxy.cdb.node_by_addr(0x7FFE) is new
    proxy.remove_node(new)
    assert proxy.cdb.node_by_addr(0x7FFE) is None
    assert 0x7FFE not in proxy._dev_key_of_element
    # one made known on top of a node of the export: removing it gives the addresses back to that node
    clash = node_for(
        template, uuid=NEW_UUID, unicast=LIGHT_2G, dev_key=NEW_KEY, name="Clash"
    )
    proxy.add_node(clash)
    assert proxy._dev_key_of_element[LIGHT_2G] == NEW_KEY
    proxy.remove_node(clash)
    assert proxy.cdb.node_by_addr(LIGHT_2G) is template
    assert proxy._dev_key_of_element[LIGHT_2G] == template.dev_key
