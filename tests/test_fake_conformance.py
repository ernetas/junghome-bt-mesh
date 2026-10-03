"""One behaviour for the three proxy fakes: `FakeProxyLink` (the integration's tests), `FakeBleak` (the library's) and
the simulated proxy node of `tests/sim`.

Each test drives a fake through the same bare GATT client (`Probe`: proxy PDUs written and read as bytes, no
`ProxyClient` in between) and checks what a JUNG proxy and its nodes do (Mesh Profile §3.5, §3.8.8, §3.10.5, §6.5):

- the Set Filter Type request is answered by a Filter Status of the type asked for, an empty list, from the proxy
  node, under the proxy nonce;
- the segments of a message to a unicast element are acknowledged by that element with every segment's bit, and a
  node's own segmented message arrives as segments that reassemble and decrypt, the client's acknowledgement taken;
- a network PDU at or below the last (IV index, SEQ) of its source is dropped (replay protection);
- during an IV Update the client's PDUs are taken under either index (the IVI bit tells them apart) and the nodes
  transmit under the old one; afterwards under the new one.

Where a fake models something by an adapter's hand rather than on its own (`FakeBleak` has no node of its own that
transmits under an IV index), the adapter says so.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import pytest

from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.client import (
    MESH_PROXY_DATA_IN,
    MESH_PROXY_DATA_OUT,
)
from custom_components.junghome_ble.jhmesh.pdu import (
    FILTER_BLACKLIST,
    FILTER_WHITELIST,
    NONCE_APP,
    PROXY_BEACON,
    PROXY_CONFIG,
    PROXY_NETWORK_PDU,
    NetworkPDU,
    ProxyReassembler,
    encode_opcode,
    lower_segments_access,
    lower_unsegmented_access,
    network_decrypt,
    network_encrypt,
    parse_beacon,
    parse_lower,
    proxy_frame,
    segment_ack,
    seq_auth_from,
    upper_decrypt,
    upper_encrypt_app,
)

from .conftest import CDB_PATH, FakeProxyLink, check_link_teardown
from .helpers import LIGHT_CTL, LIGHT_SWITCH, OUR_ADDRESS
from .jhmesh.conftest import FakeBleak
from .sim import Mesh

PROXY = LIGHT_SWITCH  # every fake plays node 0148 as the proxy
NODE = LIGHT_CTL  # a load one hop from it in the simulation
UNKNOWN = encode_opcode(
    0x82F0
)  # a SIG opcode no node of the fixture serves: nothing but the transport reacts
LONG = UNKNOWN + bytes(range(20))  # ... long enough to be segmented
WAIT = 5.0  # real seconds a fake (the simulation runs on milliseconds of real latency) may take to answer


async def until(predicate: Callable[[], Any], what: str) -> None:
    """Spin the loop until `predicate` holds (the simulation's latencies are real milliseconds here)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT
    while not predicate():
        assert loop.time() < deadline, f"{what} not seen within {WAIT:g} s"
        await asyncio.sleep(0.002)


class Probe:
    """A bare GATT client of a proxy link: writes proxy PDUs from `OUR_ADDRESS`, keeps what the link notifies."""

    def __init__(self, link: Any, cdb: CDB) -> None:
        self.link = link
        self.nk, self.ak = cdb.net_keys[0], cdb.app_keys[0]
        self.iv_index = 0  # what the mesh's beacons said last
        self.seq = 0x000200
        self.got: list[tuple[int, bytes]] = []
        self._reasm = ProxyReassembler()

    async def subscribe(self) -> None:
        await self.link.start_notify(MESH_PROXY_DATA_OUT, self._notify)

    def _notify(self, _char: Any, data: bytearray) -> None:
        got = self._reasm.feed(bytes(data))
        if got is not None:
            self.got.append(got)
            if (
                got[0] == PROXY_BEACON
                and (beacon := parse_beacon(self.nk, got[1])) is not None
            ):
                self.iv_index = beacon.iv_index

    def next_seq(self, count: int = 1) -> int:
        first = self.seq
        self.seq += count
        return first

    async def write(self, msg_type: int, payload: bytes) -> None:
        mtu = getattr(self.link, "mtu_size", None) or 23
        for frame in proxy_frame(msg_type, payload, mtu - 3):
            await self.link.write_gatt_char(MESH_PROXY_DATA_IN, frame, response=False)

    async def set_filter(self, filter_type: int) -> None:
        await self.write(
            PROXY_CONFIG,
            network_encrypt(
                self.nk,
                self.iv_index,
                True,
                0,
                self.next_seq(),
                OUR_ADDRESS,
                0x0000,
                bytes([0x00, filter_type]),
                proxy=True,
            ),
        )

    def access_pdus(
        self, dst: int, access: bytes, *, iv_index: int | None = None
    ) -> list[bytes]:
        """The network PDUs of an AppKey message to `dst` (segmented when long), each under a SEQ of its own."""
        iv = self.iv_index if iv_index is None else iv_index
        seq0 = self.seq
        upper = upper_encrypt_app(self.ak, iv, seq0, OUR_ADDRESS, dst, access)
        lowers = (
            [lower_unsegmented_access(self.ak.aid, upper)]
            if len(upper) <= 15
            else lower_segments_access(self.ak.aid, seq0, upper)
        )
        return [
            network_encrypt(
                self.nk, iv, False, 4, self.next_seq(), OUR_ADDRESS, dst, lower
            )
            for lower in lowers
        ]

    async def send(
        self, dst: int, access: bytes, *, iv_index: int | None = None
    ) -> None:
        for pdu in self.access_pdus(dst, access, iv_index=iv_index):
            await self.write(PROXY_NETWORK_PDU, pdu)

    async def ack(self, dst: int, seq_zero: int, block: int) -> None:
        await self.write(
            PROXY_NETWORK_PDU,
            network_encrypt(
                self.nk,
                self.iv_index,
                True,
                4,
                self.next_seq(),
                OUR_ADDRESS,
                dst,
                segment_ack(seq_zero, block),
            ),
        )

    def config(self) -> list[NetworkPDU]:
        out = []
        for msg_type, payload in self.got:
            if msg_type == PROXY_CONFIG:
                net = network_decrypt(self.nk, self.iv_index, payload, proxy=True)
                assert net is not None, (
                    "a proxy configuration PDU the client cannot open"
                )
                out.append(net)
        return out

    def network(self) -> list[NetworkPDU]:
        out = []
        for msg_type, payload in self.got:
            if msg_type == PROXY_NETWORK_PDU:
                net = network_decrypt(self.nk, self.iv_index, payload)
                assert net is not None, "a network PDU the client cannot open"
                out.append(net)
        return out

    def acks_from(self, src: int) -> list[tuple[int, int]]:
        """(SeqZero, BlockAck) of the Segment Acknowledgments `src` sent us."""
        out = []
        for net in self.network():
            if net.ctl and net.src == src and net.transport_pdu[0] == 0x00:
                hdr = int.from_bytes(net.transport_pdu[1:3], "big")
                out.append(
                    ((hdr >> 2) & 0x1FFF, int.from_bytes(net.transport_pdu[3:7], "big"))
                )
        return out

    def messages_from(self, src: int) -> list[tuple[int, bytes, list[int]]]:
        """(IV index, access PDU, the SEQ of each segment) of every AppKey message from `src` to us, reassembled."""
        out = []
        parts: dict[int, dict[int, tuple[bytes, int]]] = {}
        for net in self.network():
            if net.ctl or net.src != src or net.dst != OUR_ADDRESS:
                continue
            lower = parse_lower(net.transport_pdu, net.ctl)
            if lower[0] == "seg":
                info = lower[1]
                got = parts.setdefault(info.seq_zero, {})
                got[info.seg_o] = (info.data, net.seq)
                if len(got) != info.seg_n + 1:
                    continue
                upper = b"".join(got[i][0] for i in range(info.seg_n + 1))
                seq_auth, seqs = (
                    seq_auth_from(net.seq, info.seq_zero),
                    [got[i][1] for i in range(info.seg_n + 1)],
                )
                del parts[info.seq_zero]
            else:
                upper, seq_auth, seqs = lower[3], net.seq, [net.seq]
            access = upper_decrypt(
                self.ak.key,
                NONCE_APP,
                net.iv_index,
                seq_auth,
                net.src,
                net.dst,
                upper,
                0,
            )
            assert access is not None, "a node's message the client cannot open"
            out.append((net.iv_index, access, seqs))
        return out


class FakeProxyLinkAdapter:
    """`FakeProxyLink`: the integration's fake, its nodes answering from the fixture export."""

    name = "FakeProxyLink"

    def __init__(self) -> None:
        self.cdb = CDB.load(Path(CDB_PATH))
        self.link = FakeProxyLink(self.cdb)
        self.link.beacon_on_subscribe = False

    def move_iv(self, iv_index: int, update: bool) -> None:
        self.link.inject_beacon(iv_index, update)

    def emit(self, src: int, access: bytes) -> None:
        self.link.inject(src, OUR_ADDRESS, access)

    def accepted(self, dst: int) -> list[bytes]:
        return [a for s, d, a in self.link.sent if s == OUR_ADDRESS and d == dst]

    def ack_taken(self, src: int, block: int) -> bool:
        return any(
            (s, d, b) == (OUR_ADDRESS, src, block)
            for s, d, _z, b in self.link.sent_acks
        )

    def expect_replays(self) -> None:
        self.link.expect_replays = True

    def replays(self) -> int:
        return len(self.link.replayed)

    async def close(self) -> None:
        check_link_teardown(self.link)


class FakeBleakAdapter:
    """`FakeBleak`: the library's fake. Its segments are acknowledged once `auto_ack` is on, as here; it keeps no
    transmit IV index of its own, so the adapter sends under the one the mesh would (`tx_iv`)."""

    name = "FakeBleak"

    def __init__(self) -> None:
        self.cdb = CDB.load(Path(CDB_PATH))
        self.link = FakeBleak(self.cdb, proxy_node=PROXY)
        self.link.auto_ack()
        self.tx_iv = 0

    def move_iv(self, iv_index: int, update: bool) -> None:
        self.link.iv_index = iv_index
        self.tx_iv = iv_index - 1 if update else iv_index
        self.link.send_beacon(iv_index=iv_index, iv_update=update)

    def emit(self, src: int, access: bytes) -> None:
        self.link.send_access(src, OUR_ADDRESS, access, iv_index=self.tx_iv)

    def accepted(self, dst: int) -> list[bytes]:
        return [
            a
            for s, d, _t, _q, a in self.link.sent_access()
            if s == OUR_ADDRESS and d == dst
        ]

    def ack_taken(self, src: int, block: int) -> bool:
        return any(
            (s, d, b) == (OUR_ADDRESS, src, block)
            for s, d, _z, b in self.link.sent_acks()
        )

    def expect_replays(self) -> None:
        self.link.expect_replays = True

    def replays(self) -> int:
        return len(self.link.replayed)

    async def close(self) -> None:
        assert self.link.expect_replays or not self.link.replayed


class SimAdapter:
    """The simulated proxy node 0148 of `tests/sim`, its nodes on the fixture network's chain (no loss)."""

    name = "sim"

    def __init__(self) -> None:
        self.cdb = CDB.load(Path(CDB_PATH))
        self.mesh = Mesh(cdb_path=Path(CDB_PATH))
        self.link = self.mesh.proxy(PROXY).connect(mtu=69)
        self.replays_expected = False

    def move_iv(self, iv_index: int, update: bool) -> None:
        assert iv_index == self.mesh.iv_index + (1 if update else 0)
        if update:
            self.mesh.start_iv_update()
        else:
            self.mesh.complete_iv_update()

    def emit(self, src: int, access: bytes) -> None:
        node = self.mesh.node(src)
        node.send(src, OUR_ADDRESS, access, node.app_tx(0))

    def accepted(self, dst: int) -> list[bytes]:
        node = self.mesh.node(dst)
        return [
            encode_opcode(m.opcode, m.company_id) + m.params
            for m in node.received
            if m.src == OUR_ADDRESS and m.dst == dst
        ]

    def ack_taken(self, src: int, block: int) -> bool:
        # a node whose segmented message was acknowledged in full is through with it, and did not give it up
        node = self.mesh.node(src)
        return not node._tx_sar and not node.sar_failed

    def expect_replays(self) -> None:
        self.replays_expected = True

    def replays(self) -> int:
        return len(self.mesh.replays) + sum(
            1 for v in self.mesh.violations_ if "reused" in v
        )

    async def close(self) -> None:
        await self.mesh.close()
        problems = self.mesh.violations()
        if self.replays_expected:  # what the test replayed on purpose
            problems = [
                p
                for p in problems
                if not any(w in p for w in ("as a replay", "reused", "after ("))
            ]
        assert not problems, problems


ADAPTERS = (FakeProxyLinkAdapter, FakeBleakAdapter, SimAdapter)


@pytest.fixture(params=ADAPTERS, ids=[a.name for a in ADAPTERS])
async def fake(request: pytest.FixtureRequest) -> AsyncGenerator[Any]:
    adapter = request.param()
    yield adapter
    await adapter.close()


@pytest.fixture
async def probe(fake: Any) -> Probe:
    probe = Probe(fake.link, fake.cdb)
    await probe.subscribe()
    return probe


@pytest.mark.parametrize("filter_type", [FILTER_WHITELIST, FILTER_BLACKLIST])
async def test_set_filter_type_is_answered_with_the_type_asked_for(
    fake: Any, probe: Probe, filter_type: int
) -> None:
    await probe.set_filter(filter_type)
    await until(probe.config, "a Filter Status")
    status = probe.config()[-1]
    assert (status.src, status.dst, status.ctl, status.ttl) == (PROXY, 0x0000, True, 0)
    assert status.transport_pdu == bytes([0x03, filter_type, 0, 0])


async def test_segments_to_a_unicast_element_are_acknowledged(
    fake: Any, probe: Probe
) -> None:
    await probe.set_filter(FILTER_BLACKLIST)
    pdus = probe.access_pdus(NODE, LONG)
    assert len(pdus) > 1
    seq_zero = (probe.seq - len(pdus)) & 0x1FFF
    for pdu in pdus:
        await probe.write(PROXY_NETWORK_PDU, pdu)
    every = (1 << len(pdus)) - 1
    await until(lambda: (seq_zero, every) in probe.acks_from(NODE), "the full Ack")
    assert all(z == seq_zero for z, _ in probe.acks_from(NODE))
    await until(lambda: LONG in fake.accepted(NODE), "the message at the node")


async def test_a_nodes_segmented_message_arrives_and_takes_our_ack(
    fake: Any, probe: Probe
) -> None:
    await probe.set_filter(FILTER_BLACKLIST)
    fake.emit(NODE, LONG)
    await until(lambda: probe.messages_from(NODE), "the node's message")
    _iv_index, access, seqs = probe.messages_from(NODE)[0]
    assert access == LONG
    assert len(seqs) > 1
    assert seqs == list(
        range(seqs[0], seqs[0] + len(seqs))
    )  # one SEQ per segment, in order
    every = (1 << len(seqs)) - 1
    await probe.ack(NODE, seqs[0] & 0x1FFF, every)
    await until(lambda: fake.ack_taken(NODE, every), "our Ack at the node")


async def test_a_replayed_or_older_pdu_is_dropped(fake: Any, probe: Probe) -> None:
    fake.expect_replays()
    await probe.set_filter(FILTER_BLACKLIST)
    first, second = UNKNOWN + b"\x01", UNKNOWN + b"\x02"
    (old,) = probe.access_pdus(NODE, first)
    (new,) = probe.access_pdus(NODE, second)
    await probe.write(PROXY_NETWORK_PDU, new)
    await until(lambda: second in fake.accepted(NODE), "the newer PDU")
    await probe.write(PROXY_NETWORK_PDU, new)  # the same PDU again
    await probe.write(PROXY_NETWORK_PDU, old)  # a lower SEQ after a higher one
    third = UNKNOWN + b"\x03"
    await probe.send(NODE, third)
    await until(lambda: third in fake.accepted(NODE), "a fresh PDU after them")
    assert fake.accepted(NODE).count(second) == 1
    assert first not in fake.accepted(NODE)
    assert fake.replays() >= 1


async def test_pdus_under_both_indexes_during_an_iv_update(
    fake: Any, probe: Probe
) -> None:
    await probe.set_filter(FILTER_BLACKLIST)
    fake.move_iv(1, update=True)
    await until(lambda: probe.iv_index == 1, "the beacon of the update")
    old, new = UNKNOWN + b"\x10", UNKNOWN + b"\x11"
    await probe.send(NODE, old, iv_index=0)
    await probe.send(NODE, new, iv_index=1)
    await until(lambda: new in fake.accepted(NODE), "the PDU under the new index")
    assert old in fake.accepted(NODE)
    status = UNKNOWN + b"\x20"
    fake.emit(NODE, status)
    await until(lambda: probe.messages_from(NODE), "the node's message")
    assert probe.messages_from(NODE)[-1][:2] == (
        0,
        status,
    )  # nodes still transmit under the old index

    fake.move_iv(1, update=False)
    later = UNKNOWN + b"\x21"
    await until(lambda: len(probe.got) and probe.iv_index == 1, "the end of the update")
    fake.emit(NODE, later)
    await until(lambda: len(probe.messages_from(NODE)) == 2, "the node's next message")
    assert probe.messages_from(NODE)[-1][:2] == (1, later)
    after = UNKNOWN + b"\x12"
    await probe.send(NODE, after, iv_index=1)
    await until(lambda: after in fake.accepted(NODE), "a PDU under the new index")
