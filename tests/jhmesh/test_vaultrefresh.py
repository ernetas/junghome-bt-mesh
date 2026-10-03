"""jhmesh.vaultrefresh: the nodes only Home Assistant knows, carried through the app's key refresh (review-4 D11).

The steps against a stand-in client (what it was asked, with which key, what it answered); the whole procedure
against the simulated mesh is in `test_sim_mesh.py`.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from jhmesh import config_messages as C
from jhmesh.cdb import Element, Node
from jhmesh.crypto import NetKeyMaterial
from jhmesh.vault import RefreshProgress, Vault, VaultNode
from jhmesh.vaultrefresh import Target, carry, target_of, wanted

OLD = bytes(16)
NEW = bytes(range(16))
NEW_ID = NetKeyMaterial.derive(NEW).network_id
OLD_ID = NetKeyMaterial.derive(OLD).network_id
UUID = "11111111-2222-4333-8444-555555555555"
DEV_KEY = bytes(range(0x40, 0x50))
UNICAST = 0x7FF0


def netkey_status(status: int = 0, index: int = 0) -> bytes:
    return bytes([status]) + index.to_bytes(2, "little")


def phase_status(phase: int, status: int = 0, index: int = 0) -> bytes:
    return bytes([status]) + index.to_bytes(2, "little") + bytes([phase])


class StandIn:
    """What `carry` uses of a `ProxyClient`: the CDB, add / remove a node, and Config requests answered from `answers`.

    `answers[opcode]` lists the parameters of every status the node sends for a request expecting `opcode`; the
    first the request's `match` takes is its answer, none: the node is silent (TimeoutError).
    """

    def __init__(self, nodes: list[Node] | None = None) -> None:
        self.cdb = SimpleNamespace(
            nodes=list(nodes or []), node_by_addr=self._node_by_addr
        )
        self.key_refresh_target: tuple[int, bytes] | None = None
        self.answers: dict[int, list[bytes]] = {}
        self.sent: list[tuple[int, bytes, bool]] = []  # (node, access PDU, old_net_key)
        self.known_while_asked: list[bool] = []

    def _node_by_addr(self, address: int) -> Node | None:
        return next(
            (
                n
                for n in self.cdb.nodes
                if any(e.address == address for e in n.elements)
            ),
            None,
        )

    def add_node(self, node: Node) -> None:
        self.cdb.nodes.append(node)

    def remove_node(self, node: Node) -> None:
        self.cdb.nodes.remove(node)

    async def request_config(
        self,
        node_unicast: int,
        access_pdu: bytes,
        expect_opcode: int,
        *,
        timeout: float,
        match: Any,
        old_net_key: bool,
    ) -> Any:
        self.sent.append((node_unicast, access_pdu, old_net_key))
        self.known_while_asked.append(self._node_by_addr(node_unicast) is not None)
        for params in self.answers.get(expect_opcode, []):
            reply = SimpleNamespace(params=params)
            if match(reply):
                return reply
        raise TimeoutError


def vault_node(progress: RefreshProgress | None = None) -> VaultNode:
    node = Vault.create().remember_provisioned(UUID, UNICAST, 2, DEV_KEY)
    node.key_refresh = progress
    return node


def cdb_node(uuid: str = UUID, dev_key: bytes = DEV_KEY) -> Node:
    node = Node(uuid, "Hall light", UNICAST, dev_key, 0x0001)
    node.elements = [Element(a, 0, [], node) for a in (UNICAST, UNICAST + 1)]
    return node


async def test_only_what_the_client_proved_is_a_target() -> None:
    client = StandIn()
    assert target_of(client) is None  # type: ignore[arg-type]
    client.key_refresh_target = (2, NEW)
    target = target_of(client)  # type: ignore[arg-type]
    assert target == Target(2, NEW, NEW_ID)
    assert NEW.hex() not in repr(target)  # key material never in a repr


@pytest.mark.parametrize(
    ("target", "progress", "expected"),
    [
        (None, None, None),  # no refresh
        (Target.of(1, NEW), None, 1),
        (Target.of(1, NEW), RefreshProgress(NEW_ID, 1), None),  # there already
        (Target.of(2, NEW), RefreshProgress(NEW_ID, 1), 2),
        (Target.of(3, NEW), RefreshProgress(NEW_ID, 3), None),
        (
            Target.of(2, NEW),
            RefreshProgress(OLD_ID, 3),
            2,
        ),  # an earlier refresh's: this one starts over
        # the refresh over and the export holding its key: the end, if the node has not confirmed it
        (None, RefreshProgress(NEW_ID, 2), 3),
        (None, RefreshProgress(NEW_ID, 3), None),
        (
            None,
            RefreshProgress(OLD_ID, 1),
            None,
        ),  # a refresh to a key the mesh does not use
    ],
)
def test_where_a_node_is_to_be_taken(
    target: Target | None, progress: RefreshProgress | None, expected: int | None
) -> None:
    want = wanted(target, NEW, vault_node(progress))
    assert (None if want is None else want.phase) == expected
    if want is not None:
        assert (want.key, want.network_id) == (NEW, NEW_ID)


async def test_the_whole_way_with_a_node_the_client_does_not_know() -> None:
    """A pending node (in no export): made known for the exchange and forgotten again; every confirmed step kept
    and reported. The NetKey Update goes under the old key, the Phase Sets under the one transmitted with."""
    client = StandIn()
    client.answers = {
        C.CONFIG_NETKEY_STATUS: [netkey_status(index=1), netkey_status()],
        C.CONFIG_KEY_REFRESH_PHASE_STATUS: [phase_status(2), phase_status(0)],
    }
    node = vault_node()
    saved: list[RefreshProgress | None] = []

    async def save() -> None:
        saved.append(node.key_refresh)

    for phase in (1, 2):
        assert await carry(client, node, Target.of(phase, NEW), on_progress=save)  # type: ignore[arg-type]
    client.answers[C.CONFIG_KEY_REFRESH_PHASE_STATUS] = [phase_status(0)]
    assert await carry(client, node, Target.of(3, NEW))  # type: ignore[arg-type]
    assert client.sent == [
        (UNICAST, C.netkey_update(NEW), True),
        (UNICAST, C.key_refresh_phase_set(2), False),
        (UNICAST, C.key_refresh_phase_set(3), False),
    ]
    assert client.known_while_asked == [True] * 3
    assert client.cdb.nodes == []  # forgotten again
    assert saved == [RefreshProgress(NEW_ID, 1), RefreshProgress(NEW_ID, 2)]
    assert node.key_refresh == RefreshProgress(NEW_ID, 3)
    # there already: nothing sent
    assert await carry(client, node, Target.of(3, NEW))  # type: ignore[arg-type]
    assert len(client.sent) == 3


async def test_a_node_the_client_knows_is_asked_as_it_is() -> None:
    known = cdb_node()
    client = StandIn([known])
    client.answers = {C.CONFIG_NETKEY_STATUS: [netkey_status()]}
    node = vault_node(
        RefreshProgress(OLD_ID, 3)
    )  # an earlier refresh's progress: starts over
    assert await carry(client, node, Target.of(1, NEW))  # type: ignore[arg-type]
    assert client.cdb.nodes == [known]
    assert node.key_refresh == RefreshProgress(NEW_ID, 1)


@pytest.mark.parametrize(
    "other",
    [
        cdb_node(uuid="00000000-0000-4000-8000-000000000001"),
        cdb_node(dev_key=bytes(16)),
    ],
)
async def test_a_node_whose_addresses_are_another_nodes_is_left_alone(
    other: Node, caplog: pytest.LogCaptureFixture
) -> None:
    client = StandIn([other])
    node = vault_node()
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        assert await carry(client, node, Target.of(1, NEW)) is None  # type: ignore[arg-type]
    assert client.sent == []
    assert "another node of the export holds its addresses" in caplog.text
    assert client.cdb.nodes == [other]


async def test_silence_and_refusals_stop_it_where_it_is(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = StandIn()
    node = vault_node()
    with caplog.at_level(logging.WARNING, logger="jhmesh"):
        # silent: nothing confirmed
        assert await carry(client, node, Target.of(1, NEW)) is False  # type: ignore[arg-type]
        assert "did not answer its key refresh step" in caplog.text
        # the NetKey Update refused in Phase 1: stop
        client.answers = {C.CONFIG_NETKEY_STATUS: [netkey_status(0x0B)]}
        assert await carry(client, node, Target.of(1, NEW)) is False  # type: ignore[arg-type]
        assert "refused the new network key (Cannot Update)" in caplog.text
        # ... in Phase 2 it may hold the key already: its Phase Status says so
        client.answers[C.CONFIG_KEY_REFRESH_PHASE_STATUS] = [phase_status(2)]
        assert await carry(client, node, Target.of(2, NEW))  # type: ignore[arg-type]
        assert node.key_refresh == RefreshProgress(NEW_ID, 2)
        # Phase Set 3 refused, or answered with a phase that is not normal operation: stop
        client.answers[C.CONFIG_KEY_REFRESH_PHASE_STATUS] = [phase_status(2, 0x0B)]
        assert await carry(client, node, Target.of(3, NEW)) is False  # type: ignore[arg-type]
        client.answers[C.CONFIG_KEY_REFRESH_PHASE_STATUS] = [phase_status(1)]
        assert await carry(client, node, Target.of(3, NEW)) is False  # type: ignore[arg-type]
        assert "did not take key refresh phase 3" in caplog.text
        # silent at Phase 3 (it never got the new key): stop
        client.answers.clear()
        assert await carry(client, node, Target.of(3, NEW)) is False  # type: ignore[arg-type]
    assert node.key_refresh == RefreshProgress(NEW_ID, 2)
    assert NEW.hex() not in caplog.text.lower()
    assert DEV_KEY.hex() not in caplog.text.lower()


async def test_statuses_of_another_subnet_or_cut_short_are_not_answers() -> None:
    client = StandIn()
    client.answers = {
        C.CONFIG_NETKEY_STATUS: [b"\x00\x00", netkey_status(index=1)],
        C.CONFIG_KEY_REFRESH_PHASE_STATUS: [phase_status(2)[:3]],
    }
    node = vault_node()
    assert await carry(client, node, Target.of(1, NEW)) is False  # type: ignore[arg-type]
    node.key_refresh = RefreshProgress(NEW_ID, 1)
    assert await carry(client, node, Target.of(2, NEW)) is False  # type: ignore[arg-type]
