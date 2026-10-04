"""Read-only audit of the nodes' Configuration Servers against the export (CLI `config audit`, HA `audit_network`).

What the export says a node holds and what the node holds can drift apart: a Config message the app sent that
never arrived, a node re-provisioned by hand, an edit made with another tool — and nothing on the mesh ever says
so. The audit asks each node, with Gets only (it never sends a Set), and compares:

- the node-wide states, one Get each: Relay (with its retransmit), Network Transmit, Default TTL, Secure Network
  Beacon, GATT Proxy, Friend — against the export's `features`, `relayRetransmit`, `networkTransmit`, `defaultTTL`
  and `secureNetworkBeacon` (a state the export does not record is reported, not compared);
- the keys the node holds: NetKey Get, and AppKey Get for every NetKey the export gives the node —
  against its `netKeys` and `appKeys`. Indexes only: a key list carries no key, and no key is ever compared;
- for every model of every element except the Configuration Server / Client (device-key models: no
  publication, no subscriptions, no AppKey): Model Publication Get, SIG / Vendor Model Subscription Get and
  SIG / Vendor Model App Get — against the model's `publish`, `subscribe` and `bind`.

A node that answers none of the node-wide Gets is reported unanswered and not asked about its models: a sleeping
battery node or one without power would otherwise cost a timeout per model. A model that refuses a Get (a status
such as *Not a Subscribe Model*) is fine as long as the export expects nothing of that kind from it.

Every difference is a `Finding`. Two deviations a healthy installation shows are reported like the others, one
of them under a kind of its own: the Scene Server / Scene Setup Server subscriptions the export lists but the
nodes never got (`scene_subscriptions_missing`: phantom entries, scenes are recalled to all-nodes —
docs/hidden-features.md §9), and the gateway's GATT Proxy, which its export entry records as not supported. A
third is no problem at all and is kept apart, in `NodeAudit.notes`: a client model (`CLIENT_MODELS`) subscribed
to the element group of a load on its node, which the export does not list there (`client_subscriptions`: on air
a light node's key element's Light Lightness and Light CTL Clients hear their load's statuses that way, §9).

The transport and the pacing stay the caller's: an `Exchange` sends one Get and returns the reply (None when the
node stays silent), a `Runner` runs a batch of them — `run_chunked` a few at a time with a pause in between, the
integration its connect-time refresh pacing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from . import config_messages as C
from .cdb import parse_address

if TYPE_CHECKING:
    from .cdb import Node
    from .client import AccessMessage, ProxyClient

__all__ = [
    "APP_KEYS_EXTRA",
    "APP_KEYS_UNBOUND",
    "CHUNK",
    "CLIENT_MODELS",
    "CLIENT_SUBSCRIPTIONS",
    "CONFIG_MODELS",
    "KEYS_EXTRA",
    "KEYS_MISSING",
    "KEYS_REFUSED",
    "KEYS_UNANSWERED",
    "KEY_LISTS",
    "LOAD_SERVERS",
    "NODE_UNANSWERED",
    "PAUSE",
    "PUBLICATION_DIFFERS",
    "SCENE_MODELS",
    "SCENE_SUBSCRIPTIONS_MISSING",
    "SETTINGS",
    "SETTING_DIFFERS",
    "SETTING_UNANSWERED",
    "SUBSCRIPTIONS_EXTRA",
    "SUBSCRIPTIONS_MISSING",
    "Exchange",
    "Finding",
    "ModelAudit",
    "NodeAudit",
    "Query",
    "Runner",
    "audit_node",
    "client_exchange",
    "evaluate",
    "export_keys",
    "export_settings",
    "key_queries",
    "model_queries",
    "report",
    "run_chunked",
    "setting_queries",
]

CONFIG_MODELS = frozenset({"0000", "0001"})  # Configuration Server / Client
SCENE_MODELS = frozenset({"1203", "1204"})  # Scene Server / Scene Setup Server
# Generic OnOff / Level / Default Transition Time / Power OnOff, Scene, Light Lightness / CTL / HSL clients: one
# subscribed to a load's element group hears the load's statuses, and that is all (`CLIENT_SUBSCRIPTIONS`)
CLIENT_MODELS = frozenset(
    {"1001", "1003", "1005", "1009", "1205", "1302", "1305", "1309"}
)
# a load's state servers: where the export has them publish is the load's element group
LOAD_SERVERS = frozenset({"1000", "1002", "1300", "1303", "1306"})
CHUNK = (
    5  # Gets in flight at once (what the app and the integration's state refresh do)
)
PAUSE = 0.5  # seconds between two chunks

# Finding kinds (the model ones for unanswered / refused Gets are `<publication|subscriptions|app_keys>_<...>`)
NODE_UNANSWERED = "node_unanswered"
SETTING_UNANSWERED = "setting_unanswered"
SETTING_DIFFERS = "setting_differs"
PUBLICATION_DIFFERS = "publication_differs"
SUBSCRIPTIONS_MISSING = "subscriptions_missing"
SCENE_SUBSCRIPTIONS_MISSING = "scene_subscriptions_missing"
SUBSCRIPTIONS_EXTRA = "subscriptions_extra"
# a note, not a finding: a client model subscribed to a load's element group the export does not list on it
CLIENT_SUBSCRIPTIONS = "client_subscriptions"
APP_KEYS_UNBOUND = "app_keys_unbound"
APP_KEYS_EXTRA = "app_keys_extra"
# the node's keys (`setting` names the list: `net_keys` or `app_keys`)
KEYS_MISSING = "keys_missing"
KEYS_EXTRA = "keys_extra"
KEYS_UNANSWERED = "keys_unanswered"
KEYS_REFUSED = "keys_refused"
KEY_LISTS = ("net_keys", "app_keys")

# the node-wide Gets: query kind, builder, status opcode, and the settings its status carries
_SETTING_GETS: tuple[tuple[str, Callable[[], bytes], int, tuple[str, ...]], ...] = (
    ("relay", C.relay_get, C.CONFIG_RELAY_STATUS, ("relay", "relay_retransmit")),
    (
        "network_transmit",
        C.network_transmit_get,
        C.CONFIG_NETWORK_TRANSMIT_STATUS,
        ("network_transmit",),
    ),
    ("default_ttl", C.default_ttl_get, C.CONFIG_DEFAULT_TTL_STATUS, ("default_ttl",)),
    ("beacon", C.beacon_get, C.CONFIG_BEACON_STATUS, ("beacon",)),
    ("gatt_proxy", C.gatt_proxy_get, C.CONFIG_GATT_PROXY_STATUS, ("gatt_proxy",)),
    ("friend", C.friend_get, C.CONFIG_FRIEND_STATUS, ("friend",)),
)
SETTINGS = tuple(name for *_, names in _SETTING_GETS for name in names)


@dataclass(frozen=True)
class Query:
    """One Get of the audit: to which node, what it asks (`kind`), and for a model's Get the element and model."""

    node: int
    kind: str
    pdu: bytes
    expect: int
    element: int | None = None
    model: str | None = None
    net_key: int | None = None  # an AppKey Get's NetKey index

    def matches(self, message: AccessMessage) -> bool:
        """Whether `message` answers this Get: it decodes, and a model's status echoes this element and model.

        Several Gets to one node are in flight at once and share status opcodes; the echo keeps the answer about
        one model from passing for another's — and an AppKey List about one NetKey for another's.
        """
        try:
            decoded = C.decode_config(message.opcode, message.params)
        except ValueError:
            return False
        if self.net_key is not None:
            return (
                isinstance(decoded, C.AppKeyList)
                and decoded.net_key_index == self.net_key
            )
        if self.model is None:
            return True
        return (
            isinstance(
                decoded,
                (C.ModelPublicationStatus, C.ModelSubscriptionList, C.ModelAppList),
            )
            and decoded.element == self.element
            and decoded.model == C.model_id(self.model)
        )


@dataclass(frozen=True)
class Finding:
    """One difference between the export and the node, JSON-ready: addresses as hex, AppKeys as indexes."""

    kind: str
    element: int | None = None
    model: str | None = None
    setting: str | None = None
    expected: Any = None
    actual: Any = None

    def as_dict(self) -> dict[str, Any]:
        """Return the finding with the fields that apply to it."""
        out: dict[str, Any] = {"kind": self.kind}
        if self.element is not None:
            out["element"] = f"{self.element:04X}"
        for name in ("model", "setting", "expected", "actual"):
            if (value := getattr(self, name)) is not None:
                out[name] = value
        return out


@dataclass
class ModelAudit:
    """One model of one element: what the export records and what the node answered.

    A node value of None is a Get the node did not answer — or refused, and then `refused` names the status.
    """

    element: int
    model: str
    export_publish: int  # 0x0000: the export records no publication
    export_subscribe: tuple[int, ...]
    export_app_keys: tuple[int, ...]
    node_publish: int | None = None
    node_subscribe: tuple[int, ...] | None = None
    node_app_keys: tuple[int, ...] | None = None
    refused: dict[str, str] = field(default_factory=dict)  # query kind → status name


@dataclass
class NodeAudit:
    """The audit of one node; `findings` is empty when the node holds what the export says.

    `notes` are deviations from the export that are no problem (`CLIENT_SUBSCRIPTIONS`): shown, never counted.
    """

    node: int
    name: str
    answered: bool
    settings: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )  # setting → {"export": …, "node": …}
    keys: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )  # `net_keys` / `app_keys` → {"export": [index, …], "node": [index, …]}
    models: list[ModelAudit] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    notes: list[Finding] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return the node's result for an action response or the diagnostics (model rows counted, not listed).

        `notes` only when there are any.
        """
        out: dict[str, Any] = {
            "name": self.name,
            "answered": self.answered,
            "settings": self.settings,
            "keys": self.keys,
            "models": len(self.models),
            "findings": [f.as_dict() for f in self.findings],
        }
        if self.notes:
            out["notes"] = [f.as_dict() for f in self.notes]
        return out


def report(audits: Iterable[NodeAudit]) -> dict[str, Any]:
    """Summarise several nodes: each node's result by its address, the silent nodes, the number of findings.

    Notes are not findings and are not counted.
    """
    audits = list(audits)
    return {
        "nodes": {f"{a.node:04X}": a.as_dict() for a in audits},
        "unanswered": [f"{a.node:04X}" for a in audits if not a.answered],
        "findings": sum(len(a.findings) for a in audits),
    }


# ----------------------------------------------------------------------------- the Gets


def setting_queries(node: Node) -> list[Query]:
    """List the six node-wide Gets."""
    return [
        Query(node.unicast, kind, build(), expect)
        for kind, build, expect, _names in _SETTING_GETS
    ]


def _key_indexes(value: Any) -> list[int] | None:
    """Read a CDB node's `netKeys` / `appKeys` (`[{"index": 0, …}]`) as sorted indexes; None when not a list."""
    if not isinstance(value, list):
        return None
    return sorted(
        index
        for entry in value
        if isinstance(entry, dict) and (index := _int(entry.get("index"))) is not None
    )


def export_keys(node: Node) -> dict[str, list[int] | None]:
    """Return the key indexes the export gives `node`; None for a list it does not record."""
    return {
        "net_keys": _key_indexes(node.raw.get("netKeys")),
        "app_keys": _key_indexes(node.raw.get("appKeys")),
    }


def key_queries(node: Node) -> list[Query]:
    """NetKey Get, then an AppKey Get per NetKey the export gives the node (the primary one when it records none)."""
    nets = export_keys(node)["net_keys"] or [0]
    return [
        Query(node.unicast, "net_keys", C.netkey_get(), C.CONFIG_NETKEY_LIST),
        *(
            Query(
                node.unicast,
                "app_keys",
                C.appkey_get(net),
                C.CONFIG_APPKEY_LIST,
                net_key=net,
            )
            for net in nets
        ),
    ]


def _model_gets(node: Node, element: int, model: str) -> tuple[Query, Query, Query]:
    vendor = C.is_vendor_model(model)
    return (
        Query(
            node.unicast,
            "publication",
            C.model_publication_get(element, model),
            C.CONFIG_MODEL_PUBLICATION_STATUS,
            element,
            model,
        ),
        Query(
            node.unicast,
            "subscriptions",
            C.model_subscription_get(element, model),
            C.CONFIG_VENDOR_MODEL_SUBSCRIPTION_LIST
            if vendor
            else C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST,
            element,
            model,
        ),
        Query(
            node.unicast,
            "app_keys",
            C.model_app_get(element, model),
            C.CONFIG_VENDOR_MODEL_APP_LIST if vendor else C.CONFIG_SIG_MODEL_APP_LIST,
            element,
            model,
        ),
    )


def _audited_models(node: Node) -> list[tuple[int, dict[str, Any]]]:
    """(element address, CDB model entry) of every model with an AppKey side: all but the Configuration models."""
    return [
        (element.address, raw)
        for element in node.elements
        for raw in element.raw_models
        if raw["modelId"] not in CONFIG_MODELS
    ]


def model_queries(node: Node) -> list[Query]:
    """Publication, subscription and AppKey Gets of every audited model, model by model."""
    return [
        query
        for element, raw in _audited_models(node)
        for query in _model_gets(node, element, raw["modelId"])
    ]


# ----------------------------------------------------------------------------- running them

Exchange = Callable[[Query], Awaitable["AccessMessage | None"]]
Runner = Callable[[Sequence[Callable[[], Awaitable[object]]]], Awaitable[None]]


async def run_chunked(
    jobs: Sequence[Callable[[], Awaitable[object]]],
    *,
    chunk: int = CHUNK,
    pause: float = PAUSE,
) -> None:
    """Run the jobs `chunk` at a time with `pause` seconds in between: a node answers a burst, not a flood."""
    for i in range(0, len(jobs), chunk):
        if i:
            await asyncio.sleep(pause)
        await asyncio.gather(*(job() for job in jobs[i : i + chunk]))


def client_exchange(
    client: ProxyClient, *, timeout: float = 3.0, retries: int = 2
) -> Exchange:
    """Make an `Exchange` of a proxy client's device-key requests; a Get unanswered after `retries` gives None.

    A lost link (`ConnectionError`) is raised: the audit cannot go on without one.
    """

    async def exchange(query: Query) -> AccessMessage | None:
        try:
            return await client.request_config(
                query.node,
                query.pdu,
                query.expect,
                timeout=timeout,
                retries=retries,
                match=query.matches,
            )
        except TimeoutError:
            return None

    return exchange


async def audit_node(
    exchange: Exchange, node: Node, *, run: Runner = run_chunked
) -> NodeAudit:
    """Ask `node` for its node-wide states and keys, then — when it answered any — for every audited model's; compare."""
    replies: dict[Query, AccessMessage | None] = {}

    async def ask(query: Query) -> None:
        replies[query] = await exchange(query)

    await run(
        [partial(ask, query) for query in setting_queries(node) + key_queries(node)]
    )
    if not any(replies.values()):
        return NodeAudit(
            node.unicast, node.name, answered=False, findings=[Finding(NODE_UNANSWERED)]
        )
    await run([partial(ask, query) for query in model_queries(node)])
    return evaluate(node, replies)


# ----------------------------------------------------------------------------- comparing


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _transmit(value: Any) -> dict[str, int] | None:
    """Read a CDB `networkTransmit` / `relayRetransmit`: `count` transmissions, `interval` ms apart."""
    if not isinstance(value, dict):
        return None
    count, interval = _int(value.get("count")), _int(value.get("interval"))
    if count is None or interval is None:
        return None
    return {"count": count, "interval": interval}


def _wire_transmit(count: int, steps: int) -> dict[str, int]:
    """Put a Relay / Network Transmit status field in the export's terms.

    The wire counts *re*transmissions and interval steps of 10 ms minus one (§4.2.19, §4.2.20), the CDB
    transmissions and milliseconds.
    """
    return {"count": count + 1, "interval": (steps + 1) * 10}


def export_settings(node: Node) -> dict[str, Any]:
    """Return the node-wide states the export records for `node`; None for one it does not record (as a number)."""
    raw = node.raw
    features = raw.get("features")
    features = features if isinstance(features, dict) else {}
    beacon = raw.get("secureNetworkBeacon")
    return {
        "relay": _int(features.get("relay")),
        "relay_retransmit": _transmit(raw.get("relayRetransmit")),
        "network_transmit": _transmit(raw.get("networkTransmit")),
        "default_ttl": _int(raw.get("defaultTTL")),
        "beacon": beacon if isinstance(beacon, bool) else None,
        "gatt_proxy": _int(features.get("proxy")),
        "friend": _int(features.get("friend")),
    }


def _node_settings(reply: AccessMessage) -> dict[str, Any]:
    """Return the settings one node-wide status carries, in the export's terms (`Query.matches` let it decode)."""
    decoded = C.decode_config(reply.opcode, reply.params)
    if isinstance(decoded, C.RelayStatus):
        return {
            "relay": decoded.relay,
            "relay_retransmit": _wire_transmit(
                decoded.retransmit_count, decoded.retransmit_interval_steps
            ),
        }
    if isinstance(decoded, C.NetworkTransmitStatus):
        return {
            "network_transmit": _wire_transmit(decoded.count, decoded.interval_steps)
        }
    if isinstance(decoded, C.DefaultTtlStatus):
        return {"default_ttl": decoded.ttl}
    if isinstance(decoded, C.BeaconStatus):
        return {"beacon": bool(decoded.beacon)}
    if isinstance(decoded, C.FriendStatus):
        return {"friend": decoded.friend}
    assert isinstance(decoded, C.GattProxyStatus)
    return {"gatt_proxy": decoded.gatt_proxy}


def _node_keys(
    node: Node, replies: Mapping[Query, AccessMessage | None]
) -> tuple[dict[str, list[int] | None], list[Finding]]:
    """Return the key indexes the node answered (None: not answered, or refused) and the findings of its key Gets.

    The AppKeys are the union over every NetKey asked about; one AppKey Get unanswered or refused leaves them
    unknown, since the union would be short.
    """
    held: dict[str, list[int] | None] = {}
    findings: list[Finding] = []
    app_keys: set[int] | None = set()
    for query in key_queries(node):
        reply = replies.get(query)
        if reply is None:
            findings.append(Finding(KEYS_UNANSWERED, setting=query.kind))
            if query.kind == "app_keys":
                app_keys = None
            else:
                held["net_keys"] = None
            continue
        decoded = C.decode_config(reply.opcode, reply.params)
        if isinstance(decoded, C.NetKeyList):
            held["net_keys"] = sorted(decoded.net_key_indexes)
            continue
        assert isinstance(
            decoded, C.AppKeyList
        )  # `Query.matches` let only this through
        if not decoded.ok:
            findings.append(
                Finding(KEYS_REFUSED, setting="app_keys", actual=decoded.status_name)
            )
            app_keys = None
        elif app_keys is not None:
            app_keys.update(decoded.app_key_indexes)
    held["app_keys"] = None if app_keys is None else sorted(app_keys)
    return held, findings


def _hex(addresses: Iterable[int]) -> list[str]:
    return [f"{a:04X}" for a in sorted(addresses)]


def _model_answer(row: ModelAudit, query: Query, reply: AccessMessage | None) -> None:
    """Record what the node answered to one of a model's Gets in its row."""
    if reply is None:
        return
    decoded = C.decode_config(reply.opcode, reply.params)
    assert isinstance(
        decoded, (C.ModelPublicationStatus, C.ModelSubscriptionList, C.ModelAppList)
    )
    if not decoded.ok:
        row.refused[query.kind] = decoded.status_name
    elif isinstance(decoded, C.ModelPublicationStatus):
        row.node_publish = decoded.publish_address
    elif isinstance(decoded, C.ModelSubscriptionList):
        row.node_subscribe = tuple(sorted(decoded.addresses))
    else:
        row.node_app_keys = tuple(sorted(decoded.app_key_indexes))


def _load_groups(node: Node) -> frozenset[int]:
    """Return the element groups of the node's loads: where the export has their state servers publish."""
    groups = {
        parse_address(publish["address"])
        for element in node.elements
        for raw in element.raw_models
        if raw["modelId"] in LOAD_SERVERS
        and isinstance(publish := raw.get("publish"), dict)
        and "address" in publish
    }
    return frozenset(groups - {0})


def _model_findings(
    row: ModelAudit, load_groups: frozenset[int]
) -> tuple[list[Finding], list[Finding]]:
    """List one model's differences and its notes.

    A difference is a Get unanswered, refused although the export expects something, or apart. A client model's subscriptions to `load_groups` (the element groups of its node's loads) that the export does
    not list are a note (`CLIENT_SUBSCRIPTIONS`); its other extra subscriptions are findings as any model's.
    """
    out: list[Finding] = []
    notes: list[Finding] = []
    here = partial(Finding, element=row.element, model=row.model)
    expected: dict[str, Any] = {
        "publication": row.export_publish,
        "subscriptions": row.export_subscribe,
        "app_keys": row.export_app_keys,
    }
    actual: dict[str, Any] = {
        "publication": row.node_publish,
        "subscriptions": row.node_subscribe,
        "app_keys": row.node_app_keys,
    }
    for kind in ("publication", "subscriptions", "app_keys"):
        if kind in row.refused:
            # a model without publication / subscriptions refuses their Get: fine while the export expects none
            if expected[kind]:
                out.append(here(f"{kind}_refused", actual=row.refused[kind]))
        elif actual[kind] is None:
            out.append(here(f"{kind}_unanswered"))
    if row.node_publish is not None and row.node_publish != row.export_publish:
        out.append(
            here(
                PUBLICATION_DIFFERS,
                expected=f"{row.export_publish:04X}",
                actual=f"{row.node_publish:04X}",
            )
        )
    if row.node_subscribe is not None:
        if missing := set(row.export_subscribe) - set(row.node_subscribe):
            kind = (
                SCENE_SUBSCRIPTIONS_MISSING
                if row.model in SCENE_MODELS
                else SUBSCRIPTIONS_MISSING
            )
            out.append(here(kind, expected=_hex(missing)))
        extra = set(row.node_subscribe) - set(row.export_subscribe)
        if row.model in CLIENT_MODELS and (heard := extra & load_groups):
            notes.append(here(CLIENT_SUBSCRIPTIONS, actual=_hex(heard)))
            extra -= heard
        if extra:
            out.append(here(SUBSCRIPTIONS_EXTRA, actual=_hex(extra)))
    if row.node_app_keys is not None:
        if missing := set(row.export_app_keys) - set(row.node_app_keys):
            out.append(here(APP_KEYS_UNBOUND, expected=sorted(missing)))
        if extra_keys := set(row.node_app_keys) - set(row.export_app_keys):
            out.append(here(APP_KEYS_EXTRA, actual=sorted(extra_keys)))
    return out, notes


def evaluate(node: Node, replies: Mapping[Query, AccessMessage | None]) -> NodeAudit:
    """Compare the node's answers (by the `Query` they answer, None or absent = unanswered) with the export."""
    audit = NodeAudit(node.unicast, node.name, answered=True)
    expected = export_settings(node)
    for (*_get, names), query in zip(_SETTING_GETS, setting_queries(node), strict=True):
        reply = replies.get(query)
        if reply is None:
            audit.findings += [Finding(SETTING_UNANSWERED, setting=n) for n in names]
            actual: dict[str, Any] = dict.fromkeys(names)
        else:
            actual = _node_settings(reply)
        for name, value in actual.items():
            audit.settings[name] = {"export": expected[name], "node": value}
            if (
                value is not None
                and expected[name] is not None
                and value != expected[name]
            ):
                audit.findings.append(
                    Finding(
                        SETTING_DIFFERS,
                        setting=name,
                        expected=expected[name],
                        actual=value,
                    )
                )
    expected_keys = export_keys(node)
    held, key_findings = _node_keys(node, replies)
    audit.findings += key_findings
    for name in KEY_LISTS:
        export, ours = expected_keys[name], held[name]
        audit.keys[name] = {"export": export, "node": ours}
        if export is None or ours is None:
            continue
        if missing := sorted(set(export) - set(ours)):
            audit.findings.append(Finding(KEYS_MISSING, setting=name, expected=missing))
        if extra := sorted(set(ours) - set(export)):
            audit.findings.append(Finding(KEYS_EXTRA, setting=name, actual=extra))
    load_groups = _load_groups(node)
    for element, raw in _audited_models(node):
        publish = raw.get("publish")
        row = ModelAudit(
            element,
            raw["modelId"],
            parse_address(publish["address"])
            if isinstance(publish, dict) and "address" in publish
            else 0,
            tuple(sorted(parse_address(a) for a in raw.get("subscribe", []))),
            tuple(sorted(raw.get("bind", []))),
        )
        for query in _model_gets(node, element, row.model):
            _model_answer(row, query, replies.get(query))
        audit.models.append(row)
        findings, notes = _model_findings(row, load_groups)
        audit.findings += findings
        audit.notes += notes
    return audit
