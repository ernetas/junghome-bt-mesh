"""jhmesh.audit: the read-only Configuration Server audit, over the real client and the fake proxy's Config Servers.

The nodes answer from the fixture export (`FakeConfigServers`), so a node that holds what the export says gives
no finding; each test moves one thing apart and checks the finding it becomes. Every message sent is a Get.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from jhmesh import audit as A
from jhmesh import config_messages as C
from jhmesh.client import AccessMessage, ProxyClient
from jhmesh.pdu import decode_opcode, encode_opcode

from .conftest import OUR_SRC, PROXY_NODE, FakeBleak, FakeConfigServers, FastAsyncio

if TYPE_CHECKING:
    from jhmesh.cdb import CDB

GATEWAY = 0x00DC  # four audited models; 05271013 publishes to and subscribes C005
GATEWAY_GROUP, LIVING = 0xC005, 0xC010
GETS = {
    C.CONFIG_RELAY_GET,
    C.CONFIG_NETWORK_TRANSMIT_GET,
    C.CONFIG_DEFAULT_TTL_GET,
    C.CONFIG_BEACON_GET,
    C.CONFIG_GATT_PROXY_GET,
    C.CONFIG_MODEL_PUBLICATION_GET,
    C.CONFIG_SIG_MODEL_SUBSCRIPTION_GET,
    C.CONFIG_VENDOR_MODEL_SUBSCRIPTION_GET,
    C.CONFIG_SIG_MODEL_APP_GET,
    C.CONFIG_VENDOR_MODEL_APP_GET,
}
# what the fake nodes hold (hidden-features §9), in the export's terms; the fixture export records relay,
# proxy and TTL only
SETTINGS = {
    "relay": {"export": 1, "node": 1},
    "relay_retransmit": {"export": None, "node": {"count": 3, "interval": 90}},
    "network_transmit": {"export": None, "node": {"count": 3, "interval": 100}},
    "default_ttl": {"export": 5, "node": 5},
    "beacon": {"export": None, "node": True},
    "gatt_proxy": {"export": 1, "node": 1},
}


@pytest.fixture
def servers(cdb: CDB, link: FakeBleak) -> FakeConfigServers:
    config = FakeConfigServers(cdb)
    link.auto_ack()
    link.auto_config(config)
    return config


@pytest.fixture
def paced(monkeypatch: pytest.MonkeyPatch, fast: FastAsyncio) -> FastAsyncio:
    """The audit's own pauses between chunks, instantaneous and recorded apart from the client's (`fast`)."""
    own = FastAsyncio()
    monkeypatch.setattr(A, "asyncio", own)
    return own


async def audit(client: ProxyClient, cdb: CDB, node: int) -> A.NodeAudit:
    found = cdb.node_by_addr(node)
    assert found is not None
    exchange = A.client_exchange(client, timeout=0.05, retries=1)
    return await A.audit_node(exchange, found)


def kinds(result: A.NodeAudit) -> list[dict[str, Any]]:
    return [f.as_dict() for f in result.findings]


async def test_a_node_holding_the_export_has_no_findings(
    attached: ProxyClient, cdb: CDB, servers: FakeConfigServers, paced: FastAsyncio
) -> None:
    result = await audit(attached, cdb, PROXY_NODE)

    assert result.answered
    assert result.findings == []
    assert result.settings == SETTINGS
    assert len(result.models) == 30  # 31 models, the Configuration Server left out
    assert {m.model for m in result.models} >= {"1000", "1203", "05271013"}
    assert "0000" not in {m.model for m in result.models}
    # five node-wide Gets, then three per model — Gets only, never a Set
    assert len(servers.seen) == 5 + 3 * 30
    assert {decode_opcode(pdu)[0] for _node, pdu in servers.seen} <= GETS
    assert (PROXY_NODE, C.model_app_get(PROXY_NODE, "05271013")) in servers.seen
    assert (PROXY_NODE, C.model_subscription_get(0x0149, "1001")) in servers.seen
    # five at a time: one chunk of settings, 18 of models, a pause between two chunks of a batch
    assert paced.sleeps == [A.PAUSE] * 17
    onoff = next(m for m in result.models if m.model == "1000")
    assert (onoff.node_publish, onoff.node_subscribe, onoff.node_app_keys) == (
        0xC061,
        (0xC00F, 0xC061, 0xFEF5),
        (0,),
    )
    assert A.report([result]) == {
        "nodes": {
            "0148": {
                "name": "Push-button 1-gang",
                "answered": True,
                "settings": SETTINGS,
                "models": 30,
                "findings": [],
            }
        },
        "unanswered": [],
        "findings": 0,
    }


async def test_differences_become_findings(
    attached: ProxyClient, cdb: CDB, servers: FakeConfigServers, paced: FastAsyncio
) -> None:
    servers.publish[GATEWAY, "05271013"] = 0xC000
    servers.subscribe[GATEWAY, "05271013"] = [LIVING]
    servers.app_keys[GATEWAY, "1001"] = []
    servers.app_keys[GATEWAY, "1013"] = [
        0,
        1,
        2,
    ]  # three indexes: a packed pair and a lone one
    servers.settings[GATEWAY] = {"default_ttl": 3, "gatt_proxy": 2}
    servers.silent_gets |= {(GATEWAY, "beacon"), (GATEWAY, "1013", "publication")}

    result = await audit(attached, cdb, GATEWAY)

    assert kinds(result) == [
        {
            "kind": "setting_differs",
            "setting": "default_ttl",
            "expected": 5,
            "actual": 3,
        },
        {"kind": "setting_unanswered", "setting": "beacon"},
        {
            "kind": "setting_differs",
            "setting": "gatt_proxy",
            "expected": 1,
            "actual": 2,
        },
        {
            "kind": "app_keys_unbound",
            "element": "00DC",
            "model": "1001",
            "expected": [0],
        },
        {"kind": "publication_unanswered", "element": "00DC", "model": "1013"},
        {
            "kind": "app_keys_extra",
            "element": "00DC",
            "model": "1013",
            "actual": [1, 2],
        },
        {
            "kind": "publication_differs",
            "element": "00DC",
            "model": "05271013",
            "expected": "C005",
            "actual": "C000",
        },
        {
            "kind": "subscriptions_missing",
            "element": "00DC",
            "model": "05271013",
            "expected": ["C005"],
        },
        {
            "kind": "subscriptions_extra",
            "element": "00DC",
            "model": "05271013",
            "actual": ["C010"],
        },
    ]
    assert result.settings["beacon"] == {"export": None, "node": None}
    assert result.settings["default_ttl"] == {"export": 5, "node": 3}
    assert A.report([result])["findings"] == 9


async def test_a_refused_get_counts_only_where_the_export_expects_something(
    attached: ProxyClient, cdb: CDB, servers: FakeConfigServers, paced: FastAsyncio
) -> None:
    invalid_model, not_subscribe = 0x02, 0x08
    servers.refuse |= {
        (GATEWAY, "05271013", "publication"): invalid_model,
        (GATEWAY, "05271013", "subscriptions"): not_subscribe,
        (GATEWAY, "05271013", "app_keys"): invalid_model,
        # the export expects neither of these: a model without them refuses the Get, and that is all
        (GATEWAY, "1001", "publication"): invalid_model,
        (GATEWAY, "1001", "subscriptions"): not_subscribe,
    }

    result = await audit(attached, cdb, GATEWAY)

    here = {"element": "00DC", "model": "05271013"}
    assert kinds(result) == [
        {"kind": "publication_refused", **here, "actual": "Invalid Model"},
        {"kind": "subscriptions_refused", **here, "actual": "Not a Subscribe Model"},
        {"kind": "app_keys_refused", **here, "actual": "Invalid Model"},
    ]
    row = next(m for m in result.models if m.model == "05271013")
    assert row.node_publish is None
    assert row.refused == {
        "publication": "Invalid Model",
        "subscriptions": "Not a Subscribe Model",
        "app_keys": "Invalid Model",
    }


async def test_scene_server_subscriptions_the_nodes_never_got_have_a_kind_of_their_own(
    attached: ProxyClient, cdb: CDB, servers: FakeConfigServers, paced: FastAsyncio
) -> None:
    """As on air: the Scene Server holds only its element group, the Scene Setup Server nothing."""
    servers.subscribe[PROXY_NODE, "1203"] = [0xC061]
    servers.subscribe[PROXY_NODE, "1204"] = []

    result = await audit(attached, cdb, PROXY_NODE)

    assert kinds(result) == [
        {
            "kind": "scene_subscriptions_missing",
            "element": "0148",
            "model": "1203",
            "expected": ["C00F", "FEF5"],
        },
        {
            "kind": "scene_subscriptions_missing",
            "element": "0148",
            "model": "1204",
            "expected": ["C00F", "C061", "FEF5"],
        },
    ]


async def test_a_silent_node_is_not_asked_about_its_models(
    attached: ProxyClient, cdb: CDB, servers: FakeConfigServers, paced: FastAsyncio
) -> None:
    servers.silent.add(GATEWAY)

    result = await audit(attached, cdb, GATEWAY)

    assert not result.answered
    assert len(servers.seen) == 5  # the node-wide Gets, once each
    assert result.as_dict() == {
        "name": "Gateway",
        "answered": False,
        "settings": {},
        "models": 0,
        "findings": [{"kind": "node_unanswered"}],
    }
    assert A.report([result]) == {
        "nodes": {"00DC": result.as_dict()},
        "unanswered": ["00DC"],
        "findings": 1,
    }


async def test_the_export_s_transmit_and_beacon_states_are_compared(
    attached: ProxyClient, cdb: CDB, servers: FakeConfigServers, paced: FastAsyncio
) -> None:
    """An export that records them (the app's does): transmissions and milliseconds against the wire's fields."""
    node = cdb.node_by_addr(GATEWAY)
    assert node is not None
    node.raw |= {
        "networkTransmit": {"count": 3, "interval": 100},
        "relayRetransmit": {"count": 3, "interval": 90},
        "secureNetworkBeacon": True,
    }
    assert (await audit(attached, cdb, GATEWAY)).findings == []

    servers.settings[GATEWAY] = {
        "relay": (1, 3, 8),
        "network_transmit": (2, 4),
        "beacon": 0,
    }
    result = await audit(attached, cdb, GATEWAY)

    assert kinds(result) == [
        {
            "kind": "setting_differs",
            "setting": "relay_retransmit",
            "expected": {"count": 3, "interval": 90},
            "actual": {"count": 4, "interval": 90},
        },
        {
            "kind": "setting_differs",
            "setting": "network_transmit",
            "expected": {"count": 3, "interval": 100},
            "actual": {"count": 3, "interval": 50},
        },
        {
            "kind": "setting_differs",
            "setting": "beacon",
            "expected": True,
            "actual": False,
        },
    ]


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {
            "features": "relay",
            "networkTransmit": [3, 100],
            "relayRetransmit": {"count": "3", "interval": 90},
            "defaultTTL": True,
            "secureNetworkBeacon": "yes",
        },
        {"features": {"relay": 1.0}, "networkTransmit": {"count": 3}},
    ],
)
def test_an_export_state_not_recorded_as_a_number_is_not_compared(
    cdb: CDB, raw: dict[str, Any]
) -> None:
    node = cdb.node_by_addr(GATEWAY)
    assert node is not None
    node.raw = raw
    assert A.export_settings(node) == dict.fromkeys(A.SETTINGS)


def test_evaluate_counts_a_missing_answer_as_unanswered(cdb: CDB) -> None:
    node = cdb.node_by_addr(GATEWAY)
    assert node is not None
    result = A.evaluate(node, {})
    assert [f.kind for f in result.findings] == ["setting_unanswered"] * 6 + [
        "publication_unanswered",
        "subscriptions_unanswered",
        "app_keys_unanswered",
    ] * 4


def _reply(opcode: int, params: bytes, src: int = GATEWAY) -> AccessMessage:
    access = encode_opcode(opcode) + params
    return AccessMessage(
        src, OUR_SRC, 3, 0, opcode, None, params, access, f"dev:{src:04X}"
    )


def test_a_query_takes_only_the_answer_about_its_own_model(cdb: CDB) -> None:
    """Several Gets to a node are in flight at once: a status is matched by the element and model it echoes."""
    node = cdb.node_by_addr(GATEWAY)
    assert node is not None
    publication, subscriptions, app_keys = A._model_gets(node, GATEWAY, "05271013")
    status = C.model_publication_set(GATEWAY, GATEWAY_GROUP, "05271013")[1:]
    assert publication.matches(
        _reply(C.CONFIG_MODEL_PUBLICATION_STATUS, b"\x00" + status)
    )
    other = C.model_publication_set(GATEWAY, GATEWAY_GROUP, "1001")[1:]
    assert not publication.matches(
        _reply(C.CONFIG_MODEL_PUBLICATION_STATUS, b"\x00" + other)
    )
    elsewhere = C.model_publication_set(0x0148, GATEWAY_GROUP, "05271013")[1:]
    assert not publication.matches(
        _reply(C.CONFIG_MODEL_PUBLICATION_STATUS, b"\x00" + elsewhere)
    )
    assert not publication.matches(_reply(C.CONFIG_MODEL_PUBLICATION_STATUS, b"\x00"))
    assert not subscriptions.matches(_reply(C.CONFIG_DEFAULT_TTL_STATUS, b"\x05"))
    assert app_keys.matches(
        _reply(C.CONFIG_VENDOR_MODEL_APP_LIST, bytes.fromhex("00dc00270513100000"))
    )
    ttl = next(q for q in A.setting_queries(node) if q.kind == "default_ttl")
    assert ttl.matches(_reply(C.CONFIG_DEFAULT_TTL_STATUS, b"\x05"))
    assert not ttl.matches(_reply(C.CONFIG_DEFAULT_TTL_STATUS, b""))


class _Client:
    """A client whose device-key requests end in `error`."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.kwargs: dict[str, Any] = {}

    async def request_config(self, *_args: Any, **kwargs: Any) -> AccessMessage:
        self.kwargs = kwargs
        raise self.error


async def test_the_exchange_answers_none_for_silence_and_raises_a_lost_link(
    cdb: CDB,
) -> None:
    node = cdb.node_by_addr(GATEWAY)
    assert node is not None
    query = A.setting_queries(node)[0]
    silent = _Client(TimeoutError())
    assert await A.client_exchange(silent, timeout=1.5, retries=4)(query) is None  # type: ignore[arg-type]
    assert silent.kwargs == {"timeout": 1.5, "retries": 4, "match": query.matches}
    with pytest.raises(ConnectionError):
        await A.client_exchange(_Client(ConnectionError("gone")))(query)  # type: ignore[arg-type]


async def test_run_chunked_paces_the_jobs(paced: FastAsyncio) -> None:
    done: list[int] = []

    async def job(i: int) -> None:
        done.append(i)

    await A.run_chunked([lambda i=i: job(i) for i in range(7)], chunk=3, pause=0.25)
    assert done == list(range(7))
    assert paced.sleeps == [0.25, 0.25]
