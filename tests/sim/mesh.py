"""The simulated mesh: nodes of the fixture CDB on a topology, the air between them, and the books the teardown
invariants are checked against.

    mesh = Mesh(seed=3, loss=LossModel(loss=0.1))
    proxy = mesh.proxy(0x0148)
    client = ProxyClient(mesh.client_cdb(), LocalState(None, CLIENT), ttl=5)
    mesh.watch(client)
    await client.attach(proxy.connect())
    ...
    mesh.assert_invariants()   # after `await mesh.settle()`

Invariants (`violations()`):

- every (SRC, IV index, SEQ) the client transmits is unique, and they reach the proxy in increasing order (the
  nodes' replay lists would drop a lower one after a higher one: the client must write in the order it reserved);
- nothing is replayed: no node's replay list rejects a PDU of the client (on an air that reorders nothing);
- nothing is undecryptable: every network PDU of the client opens at the proxy, every upper transport PDU at its
  destination, and the client counts no PDU it could not open (`watch`);
- nothing is lost that the loss model did not drop: every PDU the client sent to a node's element reached that
  node, and every PDU a node sent to the client reached the client's link — unless a copy of it was dropped (loss,
  a rule, the proxy filter, no link) or it is still in flight.
"""

from __future__ import annotations

import asyncio
import itertools
import random
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jhmesh import config_messages as C
from jhmesh.cdb import CDB
from jhmesh.crypto import NetKeyMaterial
from jhmesh.pdu import NetworkPDU, is_unicast, network_decrypt

from .loss import LossModel, Packet
from .node import Key, Received, SimNode, key_of
from .proxy import ProxyNode
from .topology import FIXTURE_HOP_MATRIX, Topology

if TYPE_CHECKING:
    from jhmesh.cdb import Node
    from jhmesh.client import AccessMessage, ProxyClient

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CDB_PATH = FIXTURES / "MeshNetwork.json"
CLIENT_ADDRESS = 0x0D00  # the unicast the client sends from (as in the library and integration tests)
PROVISIONER = 0x0001  # the phone that provisioned the fixture network
OPENED_CACHE = 50_000  # network PDUs `Mesh.open` remembers before it starts over


@dataclass
class SentMessage:
    """An access message a simulated node sent: the keys of every transmission of each of its segments."""

    src: int
    dst: int
    access: bytes
    segments: list[set[Key]]


class Quirks:
    """Firmware behaviour of the simulated nodes, as flags.

    `set_reply_by_publication`: JUNG answers a state-changing acknowledged Set only by publishing the Status to the
    model's publication address (the element group), a unicast Status only when nothing changed.
    `seq_block`: after a restart the sequence number continues at the next multiple of this (JUNG stores it in
    blocks of 0x10000); 0 = exactly where it was.
    `proxy_forwards_every_copy`: the proxy hands its client every copy of a PDU it hears (retransmissions, the
    same PDU relayed by several neighbours), not only the first its message cache lets through — the client's
    replay protection is then all that keeps a message from being handed out twice.
    """

    def __init__(
        self,
        *,
        set_reply_by_publication: bool = True,
        seq_block: int = 0x10000,
        proxy_forwards_every_copy: bool = False,
    ) -> None:
        self.set_reply_by_publication = set_reply_by_publication
        self.seq_block = seq_block
        self.proxy_forwards_every_copy = proxy_forwards_every_copy


class Provisioner(SimNode):
    """The phone that provisioned the network: a participant with every device key and no models of its own.

    `config(node, pdu, expect)` sends a Config message under the node's device key and returns the Status.
    """

    def __init__(self, mesh: Mesh, node: Node) -> None:
        super().__init__(mesh, node, servers=False)
        self._waiters: list[tuple[int, int, asyncio.Future[Received]]] = []

    def dev_keys_for(self, net: NetworkPDU) -> list[bytes]:
        owner = self.mesh.owner(net.src)
        return [owner.dev_key] if owner is not None else []

    def on_access(self, msg: Received) -> None:
        for src, opcode, fut in list(self._waiters):
            if not fut.done() and msg.src == src and msg.opcode == opcode:
                fut.set_result(msg)
                return

    async def config(
        self,
        node: int,
        pdu: bytes,
        expect: int,
        *,
        timeout: float = 3.0,
        retries: int = 3,
    ) -> Received:
        dev_key = self.mesh.nodes[node].dev_key
        for _ in range(retries):
            fut: asyncio.Future[Received] = asyncio.get_running_loop().create_future()
            entry = (node, expect, fut)
            self._waiters.append(entry)
            try:
                self.send(self.addr, node, pdu, self.dev_tx(dev_key))
                return await asyncio.wait_for(fut, timeout)
            except TimeoutError:
                continue
            finally:
                self._waiters.remove(entry)
        raise TimeoutError(f"no {expect:#06x} from {node:04X}")

    async def send_app(self, dst: int, pdu: bytes) -> None:
        """Send an access message under AppKey 0 (a switch pressed in the app, say)."""
        await self.send_now(self.addr, dst, pdu, self.app_tx(0))

    async def refresh_net_key(
        self, new_key: bytes, *, nodes: list[int] | None = None, between: float = 1.0
    ) -> None:
        """Refresh the NetKey as the app does: NetKey Update to every node, then Phase Set 2, then Phase Set 3."""
        targets = nodes if nodes is not None else self.mesh.configured_nodes()
        new = NetKeyMaterial.derive(new_key)
        for node in targets:
            await self.expect_ok(node, C.netkey_update(new_key), C.CONFIG_NETKEY_STATUS)
        self.key_refresh(1, new)
        await asyncio.sleep(between)
        for node in targets:
            await self.expect_ok(
                node, C.key_refresh_phase_set(2), C.CONFIG_KEY_REFRESH_PHASE_STATUS
            )
        self.key_refresh(2)
        self.mesh.key_refresh_moved()
        await asyncio.sleep(between)
        for node in targets:
            await self.expect_ok(
                node, C.key_refresh_phase_set(3), C.CONFIG_KEY_REFRESH_PHASE_STATUS
            )
        self.key_refresh(3)
        self.mesh.net_key = new
        self.mesh.key_refresh_moved()

    async def expect_ok(self, node: int, pdu: bytes, expect: int) -> None:
        reply = await self.config(node, pdu, expect)
        decoded = C.decode_config(reply.opcode, reply.params)
        assert getattr(decoded, "ok", True), (
            f"{node:04X} refused {pdu.hex()}: {decoded}"
        )


class Mesh:
    """The nodes of the fixture CDB that the topology places, the radio between them and the invariant books."""

    def __init__(
        self,
        *,
        seed: int = 0,
        loss: LossModel | None = None,
        quirks: Quirks | None = None,
        topology: Topology | None = None,
        proxies: tuple[int, ...] = (0x0148, 0x0300),
        cdb_path: Path = CDB_PATH,
        client_address: int = CLIENT_ADDRESS,
        retransmissions: bool = False,
    ) -> None:
        self.cdb_path = cdb_path
        self.cdb = CDB.load(cdb_path)
        self.rng = random.Random(seed)  # noqa: S311  # a seeded simulation: repeatable, not secret
        self.loss = loss or LossModel()
        self.quirks = quirks or Quirks()
        self.topology = topology or Topology.from_hop_matrix(FIXTURE_HOP_MATRIX)
        self.retransmissions = retransmissions  # network transmit / relay retransmit copies (slower, more copies)
        self.net_key = self.cdb.net_keys[0]
        self.app_keys = dict(self.cdb.app_keys)
        self.iv_index = self.cdb.iv_index
        self.iv_update = False
        self.client_addresses = {client_address}
        self.nodes: dict[int, SimNode] = {}
        self.provisioner: Provisioner | None = None
        self.client_proxy: ProxyNode | None = (
            None  # the proxy the client connected to last
        )
        for node in self.cdb.nodes:
            if node.unicast not in self.topology.nodes:
                continue
            if node.unicast == PROVISIONER:
                self.provisioner = Provisioner(self, node)
                self.nodes[node.unicast] = self.provisioner
            elif node.unicast in proxies:
                self.nodes[node.unicast] = ProxyNode(self, node)
            else:
                self.nodes[node.unicast] = SimNode(self, node)
        # the books
        self.violations_: list[str] = []
        self.client_tx: dict[Key, NetworkPDU] = {}
        self._client_last: dict[int, tuple[int, int]] = {}
        self.dropped: dict[Key, set[str]] = {}
        self.delivered: set[tuple[Key, int]] = set()
        self.for_client: dict[Key, NetworkPDU] = {}
        self.to_client: set[Key] = set()
        self.in_flight: Counter[Key] = Counter()
        self.node_tx: set[Key] = set()
        self.replays: list[tuple[int, Key]] = []
        self.undecryptable: list[tuple[int, str, Key | None]] = []
        self._origin_of: dict[bytes, Key] = {}
        self._link_clock: dict[tuple[int, int], float] = {}
        # the arrivals still on the air, by token: `close` cancels them on a loop that outlives the simulation
        self._arrivals: dict[int, asyncio.TimerHandle] = {}
        self._tokens = itertools.count()
        self._opened: dict[
            tuple[NetKeyMaterial, int, bytes, bool], NetworkPDU | None
        ] = {}
        self._watched: list[tuple[ProxyClient, list[int]]] = []
        self.sent: list[SentMessage] = []
        self.client_got: Counter[tuple[int, int, bytes]] = Counter()
        self.overtaken: set[Key] = set()
        self._to_client_last: dict[int, tuple[int, int]] = {}
        self.transmissions = 0

    # ------------------------------------------------------------------ building
    def proxy(self, address: int) -> ProxyNode:
        node = self.nodes[address]
        assert isinstance(node, ProxyNode), f"{address:04X} is not a proxy node here"
        return node

    def node(self, address: int) -> SimNode:
        """The node that owns element `address`."""
        for node in self.nodes.values():
            if address in node.element_set:
                return node
        raise KeyError(f"{address:04X}")

    def owner(self, address: int) -> SimNode | None:
        try:
            return self.node(address)
        except KeyError:
            return None

    def client_cdb(self) -> CDB:
        """A separate copy of the fixture CDB for the client (it may add nodes to its own)."""
        return CDB.load(self.cdb_path)

    def add_node(
        self,
        node: Node,
        neighbours: list[int],
        *,
        fresh: bool = True,
        proxy: bool = False,
    ) -> SimNode:
        """Put a newly provisioned node on the air next to `neighbours`."""
        sim = (
            ProxyNode(self, node, fresh=fresh)
            if proxy
            else SimNode(self, node, fresh=fresh)
        )
        self.nodes[node.unicast] = sim
        self.topology.add(node.unicast, neighbours)
        return sim

    def configured_nodes(self) -> list[int]:
        return [
            a for a, n in self.nodes.items() if n.servers is not None and n.provisioned
        ]

    def initial_seq(self, address: int) -> int:
        return 0x000100 + (address & 0xFF) * 0x10

    def watch(self, client: ProxyClient) -> None:
        """Record what the client hands out and count what it cannot decrypt (both checked at teardown).

        The client's own callbacks keep working.
        """
        count = [0]
        on_undecryptable, on_message = client.on_undecryptable, client.on_message

        def counted() -> None:
            count[0] += 1
            if on_undecryptable is not None:
                on_undecryptable()

        def got(msg: AccessMessage) -> None:
            self.client_got[(msg.src, msg.dst, msg.access_pdu)] += 1
            if on_message is not None:
                on_message(msg)

        client.on_undecryptable = counted
        client.on_message = got
        self._watched.append((client, count))

    # ------------------------------------------------------------------ network-wide procedures
    def start_iv_update(self) -> None:
        """IV Update in Progress: every node moves to index + 1 and keeps transmitting with the old one."""
        self.iv_index += 1
        self.iv_update = True
        self._iv_moved()

    def complete_iv_update(self) -> None:
        """Back to Normal Operation: every node transmits with the new index, its sequence number from 0."""
        self.iv_update = False
        self._iv_moved()

    def _iv_moved(self) -> None:
        for node in self.nodes.values():
            node.set_iv(self.iv_index, self.iv_update)
        for node in self.nodes.values():
            if isinstance(node, ProxyNode):
                node.state_changed()

    def key_refresh_moved(self) -> None:
        for node in self.nodes.values():
            if isinstance(node, ProxyNode):
                node.state_changed()

    async def settle(self, seconds: float = 5.0) -> None:
        """Let what is in flight land (virtual time)."""
        await asyncio.sleep(seconds)

    # ------------------------------------------------------------------ the air
    def radio(
        self,
        sender: SimNode,
        raw: bytes,
        net: NetworkPDU,
        *,
        delay: float = 0.0,
        copies: int = 1,
        interval: float = 0.0,
    ) -> None:
        loop = asyncio.get_running_loop()
        key = key_of(net)
        self._origin_of.setdefault(raw, key)
        batches: dict[float, list[SimNode]] = {}
        for copy in range(copies):
            for neighbour in sorted(self.topology.neighbours(sender.addr)):
                receiver = self.nodes.get(neighbour)
                if receiver is None:
                    continue
                self.transmissions += 1
                why = self.loss.lost(
                    Packet("adv", "network", net, sender.addr, neighbour), self.rng
                )
                if why is not None:
                    self.note_drop(key, why)
                    continue
                at = loop.time() + delay + copy * interval + self.loss.latency
                if self.loss.reorder:
                    at += self.rng.uniform(0, self.loss.reorder)
                else:  # one link delivers in the order it sent
                    at = max(
                        at, self._link_clock.get((sender.addr, neighbour), 0.0) + 1e-6
                    )
                    self._link_clock[(sender.addr, neighbour)] = at
                arrivals = [at]
                if self.loss.duplicate and self.rng.random() < self.loss.duplicate:
                    arrivals.append(
                        at
                        + self.rng.uniform(0, max(self.loss.reorder, self.loss.latency))
                    )
                for when in arrivals:
                    self.in_flight[key] += 1
                    batches.setdefault(when, []).append(receiver)
        # one timer per arrival time, not per receiver: the neighbours of a sender mostly hear it at the same moment
        for when, receivers in batches.items():
            token = next(self._tokens)
            self._arrivals[token] = loop.call_at(
                when, self._arrive, receivers, raw, key, token
            )

    def _arrive(
        self, receivers: list[SimNode], raw: bytes, key: Key, token: int
    ) -> None:
        del self._arrivals[token]
        for receiver in receivers:
            self.in_flight[key] -= 1
            receiver.receive(raw)

    def open(
        self, nk: NetKeyMaterial, iv_index: int, raw: bytes, *, proxy: bool
    ) -> NetworkPDU | None:
        """`network_decrypt`, once per transmission: every neighbour of a sender hears the same bytes."""
        ident = (nk, iv_index, raw, proxy)
        if ident in self._opened:
            return self._opened[ident]
        if len(self._opened) >= OPENED_CACHE:
            self._opened.clear()
        net = self._opened[ident] = network_decrypt(nk, iv_index, raw, proxy=proxy)
        return net

    async def close(self) -> None:
        """Stop the simulation: nothing left on the air, no node's timer or task, no proxy link or beacon timer.

        A test on a loop of its own (`run`) needs none of it; one on a loop that goes on (Home Assistant's test loop,
        which fails a test that leaves a timer or a task behind) closes the mesh at teardown. What was still on the
        air stays counted as in flight, so the invariants hold the same afterwards.
        """
        for handle in self._arrivals.values():
            handle.cancel()
        self._arrivals.clear()
        tasks = [task for node in self.nodes.values() for task in node.stop()]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ the books
    def violation(self, text: str) -> None:
        self.violations_.append(text)

    def note_client_tx(self, net: NetworkPDU, proxy: ProxyNode) -> None:
        key = key_of(net)
        self.client_addresses.add(net.src)
        if key in self.client_tx:
            self.violation(
                f"client reused (SRC, IV, SEQ) = ({net.src:04X}, {net.iv_index}, {net.seq:06X}) via {proxy.addr:04X}"
            )
        last = self._client_last.get(net.src)
        entry = (net.iv_index, net.seq)
        if last is not None and entry <= last:
            self.violation(
                f"client PDU ({net.iv_index}, {net.seq:06X}) reached {proxy.addr:04X} after ({last[0]}, {last[1]:06X})"
            )
        self._client_last[net.src] = max(entry, last or entry)
        self.client_tx.setdefault(key, net)

    def note_origin(self, node: SimNode, net: NetworkPDU, raw: bytes) -> None:
        key = key_of(net)
        self._origin_of.setdefault(raw, key)
        if key in self.node_tx:
            self.violation(f"simulated node {node.addr:04X} reused {key}")
        self.node_tx.add(key)
        if net.dst in self.client_addresses:
            self.for_client[key] = net

    def note_drop(self, key: Key, why: str) -> None:
        self.dropped.setdefault(key, set()).add(why)

    def note_delivered(self, key: Key, node: SimNode) -> None:
        self.delivered.add((key, node.addr))

    def note_to_client(self, key: Key) -> None:
        """A PDU reached the client's link; one below what already reached it from its source was overtaken."""
        if key in self.to_client:
            return  # another copy (`Quirks.proxy_forwards_every_copy`)
        self.to_client.add(key)
        last = self._to_client_last.get(key[0])
        if last is not None and key[1:] < last:
            self.overtaken.add(key)
        self._to_client_last[key[0]] = max(key[1:], last or key[1:])

    def note_sent(
        self, src: int, dst: int, access: bytes, segments: int
    ) -> SentMessage:
        sent = SentMessage(src, dst, access, [set() for _ in range(segments)])
        self.sent.append(sent)
        return sent

    def note_replay(self, node: SimNode, net: NetworkPDU) -> None:
        key = key_of(net)
        self.replays.append((node.addr, key))
        if net.src in self.client_addresses:
            if self.loss.perfect:
                self.violation(f"{node.addr:04X} rejected client PDU {key} as a replay")
            else:
                self.note_drop(
                    key, "late"
                )  # overtaken on the air: the replay list drops it, as it must

    def note_undecryptable(self, node: SimNode, raw: bytes, bearer: str) -> None:
        origin = self._origin_of.get(raw)
        self.undecryptable.append((node.addr, bearer, origin))
        if bearer == "gatt" or (
            origin is not None and origin[0] in self.client_addresses
        ):
            self.violation(
                f"{node.addr:04X} could not decrypt a client PDU ({bearer}, {origin})"
            )

    def note_upper_undecryptable(self, node: SimNode, net: NetworkPDU) -> None:
        self.violation(
            f"{node.addr:04X} could not decrypt the upper transport PDU {key_of(net)} from {net.src:04X}"
        )

    def note_malformed(self, node: SimNode, net: NetworkPDU) -> None:
        self.violation(
            f"{node.addr:04X} got a malformed PDU {key_of(net)} from {net.src:04X}"
        )

    def violations(self) -> list[str]:
        out = list(self.violations_)
        for key, net in self.client_tx.items():
            target = self.owner(net.dst) if is_unicast(net.dst) else None
            if target is None or not target.provisioned or not target.alive:
                continue
            if key in self.dropped or self.in_flight[key] > 0:
                continue
            if (key, target.addr) not in self.delivered:
                out.append(
                    f"client PDU {key} to {net.dst:04X} lost, yet the loss model dropped nothing of it"
                )
        for key, net in self.for_client.items():
            if key in self.dropped or self.in_flight[key] > 0 or key in self.to_client:
                continue
            out.append(
                f"PDU {key} from {net.src:04X} to the client lost, yet nothing dropped it"
            )
        for _client, count in self._watched:
            if count[0]:
                out.append(f"the client could not decrypt {count[0]} PDU(s)")
        if self._watched:
            out += self._client_messages()
        return out

    def _client_messages(self) -> list[str]:
        """Every access message whose every segment reached the client's link was handed out; none twice.

        A message one of whose PDUs was overtaken on the air may rightly fall to the client's replay list.
        """
        out: list[str] = []
        reached: Counter[tuple[int, int, bytes]] = Counter()
        sent: Counter[tuple[int, int, bytes]] = Counter()
        for m in self.sent:
            triple = (m.src, m.dst, m.access)
            sent[triple] += 1
            keys = [k for segment in m.segments for k in segment]
            if any(k in self.overtaken for k in keys):
                continue
            if all(any(k in self.to_client for k in segment) for segment in m.segments):
                reached[triple] += 1
        for (src, dst, access), count in reached.items():
            if self.client_got[(src, dst, access)] < count:
                out.append(
                    f"the client lost {src:04X}→{dst:04X} {access.hex()} although it reached its link"
                )
        for (src, dst, access), count in self.client_got.items():
            if count > sent[(src, dst, access)]:
                out.append(
                    f"the client handed out {src:04X}→{dst:04X} {access.hex()} {count} times"
                )
        return out

    def assert_invariants(self) -> None:
        problems = self.violations()
        assert not problems, "simulated mesh invariants violated:\n" + "\n".join(
            problems[:40]
        )

    # ------------------------------------------------------------------ inspection helpers
    def client_pdus_to(self, dst: int) -> list[NetworkPDU]:
        return [n for n in self.client_tx.values() if n.dst == dst]

    def stats(self) -> dict[str, Any]:
        return {
            "transmissions": self.transmissions,
            "client_pdus": len(self.client_tx),
            "dropped": len(self.dropped),
            "replays": len(self.replays),
        }
