"""A simulated mesh node: its keys and IV state, the network layer (message cache, replay list, relay) and the lower
and upper transport (segmentation both ways with acknowledgements), handing access messages to its servers.

What the node does to a PDU is reported to its `Mesh`, which keeps the books the teardown invariants read.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jhmesh.pdu import (
    ALL_NODES,
    ALL_PROXIES,
    ALL_RELAYS,
    NONCE_APP,
    NONCE_DEVICE,
    NetworkPDU,
    SegmentInfo,
    decode_opcode,
    is_unicast,
    lower_segments_access,
    lower_unsegmented_access,
    network_encrypt,
    parse_lower,
    segment_ack,
    seq_auth_from,
    upper_decrypt,
    upper_encrypt,
)

from .servers import Servers

if TYPE_CHECKING:
    from jhmesh.cdb import Node
    from jhmesh.crypto import AppKeyMaterial, NetKeyMaterial

    from .mesh import Mesh

Key = tuple[
    int, int, int
]  # (SRC, IV index, SEQ): one network PDU, whatever its TTL and whichever copy

SEGMENT_SIZE = 12
SAR_ROUNDS = 4  # transmissions of a segmented message to a unicast before giving up
SAR_ACK_WAIT = 0.5  # seconds a round waits for the Segment Acknowledgment
SAR_GROUP_REPEATS = (
    2  # a segmented message to a group is sent this often, unacknowledged
)
ACK_TIMER_BASE, ACK_TIMER_PER_TTL = 0.15, 0.05  # §3.5.3.4: 150 ms + 50 ms x TTL
INCOMPLETE_TIMEOUT = 10.0  # §3.5.3.4: a reassembly nothing arrives for is abandoned
RELAY_DELAY = 0.002


def key_of(net: NetworkPDU) -> Key:
    return (net.src, net.iv_index, net.seq)


@dataclass(frozen=True)
class TxKey:
    """The upper-transport key of a message a node sends: an AppKey (with its index) or a device key."""

    key: bytes = field(repr=False)
    nonce: int
    akf: bool
    aid: int
    label: str


@dataclass(frozen=True)
class Received:
    """An access message a node accepted (decrypted, not a replay)."""

    src: int
    dst: int
    ttl: int
    opcode: int
    company_id: int | None
    params: bytes
    key: str  # 'app<index>' | 'dev'
    seq_auth: int


@dataclass
class _RxSar:
    seg_n: int
    seq_auth: int
    iv_index: int
    akf: bool
    aid: int
    szmic: int
    parts: dict[int, bytes] = field(default_factory=dict)
    ack_timer: asyncio.TimerHandle | None = None
    expiry: asyncio.TimerHandle | None = None


@dataclass
class _TxSar:
    block: int = 0
    cancelled: bool = False
    event: asyncio.Event = field(default_factory=asyncio.Event)


class SimNode:
    """One node of the simulated mesh, built from its CDB entry.

    `fresh`: provisioned (network key, IV index, address, device key) but never configured — no AppKey, nothing
    bound, published or subscribed, as a node is right after provisioning. `servers=False`: a participant with no
    models of its own (the phone that acts as provisioner).
    """

    def __init__(
        self,
        mesh: Mesh,
        node: Node,
        *,
        fresh: bool = False,
        servers: bool = True,
    ) -> None:
        self.mesh = mesh
        self.node = node
        self.addr = node.unicast
        self.elements = [e.address for e in node.elements]
        self.element_set = frozenset(self.elements)
        self.dev_key = node.dev_key
        self.provisioned = True
        self.alive = True
        self.net_key: NetKeyMaterial = mesh.net_key
        self.new_net_key: NetKeyMaterial | None = None
        self.kr_phase = 0
        self.app_keys: dict[int, AppKeyMaterial] = {} if fresh else dict(mesh.app_keys)
        self.iv_index = mesh.iv_index
        self.iv_update = mesh.iv_update
        self.seq = mesh.initial_seq(self.addr)
        self.rpl: dict[int, tuple[int, int]] = {}
        self.cache: set[Key] = set()
        self._rx_sar: dict[tuple[int, int], _RxSar] = {}
        self._sar_done: dict[int, set[tuple[int, int]]] = {}
        self._tx_sar: dict[tuple[int, int], _TxSar] = {}
        self._sar_locks: dict[int, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self.received: list[Received] = []
        self.sar_failed: list[
            tuple[int, int]
        ] = []  # (dst, SeqAuth) of segmented sends never acknowledged
        self.servers = Servers(self, node, fresh=fresh) if servers else None

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.addr:04X})"

    # ------------------------------------------------------------------ keys and IV state
    @property
    def rx_net_keys(self) -> tuple[NetKeyMaterial, ...]:
        if self.new_net_key is None:
            return (self.net_key,)
        return (self.net_key, self.new_net_key)

    @property
    def tx_net_key(self) -> NetKeyMaterial:
        """The key refresh's new key from Phase 2 on (§3.10.4.1), the current one before."""
        if self.new_net_key is not None and self.kr_phase == 2:
            return self.new_net_key
        return self.net_key

    @property
    def tx_iv_index(self) -> int:
        return self.iv_index - 1 if self.iv_update else self.iv_index

    def set_iv(self, iv_index: int, iv_update: bool) -> None:
        """Follow the network's IV state; the sequence number restarts when the transmit index moves on."""
        old_tx = self.tx_iv_index
        self.iv_index, self.iv_update = iv_index, iv_update
        if self.tx_iv_index > old_tx:
            self.seq = 0

    def key_refresh(self, phase: int, new_key: NetKeyMaterial | None = None) -> None:
        """Move this node's key refresh: 1 with the new key, 2 (transmit with it), 3 (revoke the old one)."""
        if phase == 1:
            assert new_key is not None
            self.new_net_key, self.kr_phase = new_key, 1
        elif phase == 2 and self.new_net_key is not None:
            self.kr_phase = 2
        elif phase == 3 and self.new_net_key is not None:
            self.net_key, self.new_net_key, self.kr_phase = self.new_net_key, None, 0

    def next_seq(self) -> int:
        return self.reserve_seq(1)

    def reserve_seq(self, count: int) -> int:
        first = self.seq
        self.seq += count
        return first

    def restart(self) -> None:
        """Power-cycle: reassemblies and segmented sends are lost; the sequence number resumes per the quirk.

        JUNG firmware stores its sequence number in blocks: after a restart it continues at the next multiple of
        `Quirks.seq_block` (0 = exactly where it was).
        """
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        self._rx_sar.clear()
        self._tx_sar.clear()
        block = self.mesh.quirks.seq_block
        if block:
            self.seq = (self.seq // block + 1) * block

    def reset(self) -> None:
        """Config Node Reset: the node leaves the network — no keys, no configuration, deaf to everything."""
        self.provisioned = False
        self.app_keys.clear()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    def stop(self) -> list[asyncio.Task[Any]]:
        """Cancel every timer and task of this node (`Mesh.close`); returns the tasks, to be awaited."""
        for sid in list(self._rx_sar):
            self._abandon(sid)
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        self._tasks.clear()
        return tasks

    @property
    def busy(self) -> bool:
        """A task of this node still runs (an answer, a segmented send)."""
        return bool(self._tasks)

    def spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ network layer
    def listens(self, dst: int) -> bool:
        """Whether a PDU to `dst` is for this node's upper layers: one of its elements, or a group it is in."""
        if dst in self.element_set:
            return True
        if dst == ALL_NODES:
            return True
        if dst == ALL_RELAYS:
            return self.servers is not None and bool(self.servers.config.relay[0])
        if dst == ALL_PROXIES:
            return self.servers is not None and bool(self.servers.config.gatt_proxy)
        return self.servers is not None and self.servers.subscribed(dst)

    def open(
        self, raw: bytes, *, proxy: bool = False
    ) -> tuple[NetworkPDU, NetKeyMaterial] | None:
        for nk in self.rx_net_keys:
            net = self.mesh.open(nk, self.iv_index, raw, proxy=proxy)
            if net is not None:
                return net, nk
        return None

    def receive(self, raw: bytes) -> None:
        """A network PDU off the air."""
        if not (self.provisioned and self.alive):
            return
        opened = self.open(raw)
        if opened is None:
            self.mesh.note_undecryptable(self, raw, "adv")
            return
        self.network_rx(*opened, from_gatt=False)

    def network_rx(
        self, net: NetworkPDU, nk: NetKeyMaterial, *, from_gatt: bool
    ) -> None:
        key = key_of(net)
        if key in self.cache:
            return
        self.cache.add(key)
        if net.src in self.element_set:
            return  # our own PDU, relayed back to us
        if self.listens(net.dst):
            self.mesh.note_delivered(key, self)
            self.lower_rx(net)
        if net.dst not in self.element_set:
            self.forward(net, nk, from_gatt=from_gatt)

    def forward(self, net: NetworkPDU, nk: NetKeyMaterial, *, from_gatt: bool) -> None:
        """Relay (and, for a PDU from a proxy client, the proxy feature): TTL 2 or more, sent on with TTL - 1."""
        if net.ttl < 2:
            return
        relay_on = self.servers is not None and bool(self.servers.config.relay[0])
        if not (relay_on or from_gatt):
            return
        ttl = net.ttl - 1
        raw = network_encrypt(
            nk, net.iv_index, net.ctl, ttl, net.seq, net.src, net.dst, net.transport_pdu
        )
        count, steps = self.servers.config.relay[1:] if self.servers else (0, 0)
        self.mesh.radio(
            self,
            raw,
            _with_ttl(net, ttl),
            delay=RELAY_DELAY,
            copies=1 + count if self.mesh.retransmissions else 1,
            interval=(steps + 1) * 0.01,
        )

    def originate(
        self,
        src: int,
        dst: int,
        ttl: int,
        seq: int,
        iv_index: int,
        transport_pdu: bytes,
        *,
        ctl: bool = False,
    ) -> None:
        """Send a network PDU of our own on every bearer (the proxy node's GATT link too)."""
        nk = self.tx_net_key
        raw = network_encrypt(nk, iv_index, ctl, ttl, seq, src, dst, transport_pdu)
        net = NetworkPDU(
            iv_index & 1, iv_index, nk.nid, ctl, ttl, seq, src, dst, transport_pdu
        )
        self.cache.add(key_of(net))
        self.mesh.note_origin(self, net, raw)
        count, steps = self.servers.config.network_transmit if self.servers else (0, 0)
        self.mesh.radio(
            self,
            raw,
            net,
            copies=1 + count if self.mesh.retransmissions else 1,
            interval=(steps + 1) * 0.01,
        )
        self.after_originate(raw, net)

    def after_originate(self, raw: bytes, net: NetworkPDU) -> None:
        """Hook: the proxy node hands its own PDUs to its client too."""

    # ------------------------------------------------------------------ replay protection (§3.8.8)
    def _is_replay(self, net: NetworkPDU) -> bool:
        last = self.rpl.get(net.src)
        if last is not None and (net.iv_index, net.seq) <= last:
            self.mesh.note_replay(self, net)
            return True
        return False

    def _rpl_update(self, net: NetworkPDU) -> None:
        entry = (net.iv_index, net.seq)
        if entry > self.rpl.get(net.src, (-1, -1)):
            self.rpl[net.src] = entry

    # ------------------------------------------------------------------ lower transport
    def lower_rx(self, net: NetworkPDU) -> None:
        try:
            kind = parse_lower(net.transport_pdu, net.ctl)
        except ValueError:
            self.mesh.note_malformed(self, net)
            return
        if kind[0] == "seg":
            self._segment_rx(net, kind[1])
            return
        if kind[0] == "segctl" or self._is_replay(net):
            return
        if kind[0] == "ctl":
            self._rpl_update(net)
            self._control_rx(net, kind[1], kind[2])
            return
        _, akf, aid, upper = kind
        self._upper_rx(net, akf, aid, upper, 0, net.seq)

    def _control_rx(self, net: NetworkPDU, opcode: int, params: bytes) -> None:
        if opcode != 0x00 or len(params) < 6 or net.dst not in self.element_set:
            return
        hdr = int.from_bytes(params[:2], "big")
        seq_zero, block = (hdr >> 2) & 0x1FFF, int.from_bytes(params[2:6], "big")
        sar = self._tx_sar.get((net.src, seq_zero))
        if sar is None:
            return
        if block == 0:
            sar.cancelled = True
        sar.block |= block
        sar.event.set()

    def _segment_rx(self, net: NetworkPDU, s: SegmentInfo) -> None:
        unicast = net.dst in self.element_set
        if s.seg_o > s.seg_n:
            return
        try:
            seq_auth = seq_auth_from(net.seq, s.seq_zero)
        except ValueError:
            return
        if (net.iv_index, seq_auth) in self._sar_done.get(net.src, set()):
            if unicast:  # a retransmission: the sender missed our acknowledgement
                self._send_ack(net.src, net.dst, s.seq_zero, (1 << (s.seg_n + 1)) - 1)
            return
        sid = (net.src, s.seq_zero)
        sar = self._rx_sar.get(sid)
        if sar is None or (sar.iv_index, sar.seq_auth) != (net.iv_index, seq_auth):
            # a new message: the replay list checks its first segment to arrive (Zephyr does the same)
            if self._is_replay(net):
                return
            if sar is not None:
                self._abandon(sid)
            sar = self._rx_sar[sid] = _RxSar(
                s.seg_n, seq_auth, net.iv_index, s.akf, s.aid, s.szmic
            )
        if s.seg_n != sar.seg_n:
            return
        self._rpl_update(net)
        sar.parts[s.seg_o] = s.data
        loop = asyncio.get_running_loop()
        if sar.expiry is not None:
            sar.expiry.cancel()
        sar.expiry = loop.call_later(INCOMPLETE_TIMEOUT, self._abandon, sid)
        if len(sar.parts) == sar.seg_n + 1:
            self._abandon(sid)
            self._sar_done.setdefault(net.src, set()).add((net.iv_index, seq_auth))
            if unicast:
                self._send_ack(net.src, net.dst, s.seq_zero, (1 << (s.seg_n + 1)) - 1)
            upper = b"".join(sar.parts[i] for i in range(sar.seg_n + 1))
            self._upper_rx(net, sar.akf, sar.aid, upper, sar.szmic, seq_auth)
        elif unicast:
            if sar.ack_timer is not None:
                sar.ack_timer.cancel()
            sar.ack_timer = loop.call_later(
                ACK_TIMER_BASE + ACK_TIMER_PER_TTL * net.ttl,
                self._partial_ack,
                sid,
                net.dst,
            )

    def _abandon(self, sid: tuple[int, int]) -> None:
        sar = self._rx_sar.pop(sid, None)
        if sar is None:
            return
        for timer in (sar.ack_timer, sar.expiry):
            if timer is not None:
                timer.cancel()

    def _partial_ack(self, sid: tuple[int, int], element: int) -> None:
        sar = self._rx_sar.get(sid)
        if sar is None:
            return
        block = 0
        for i in sar.parts:
            block |= 1 << i
        self._send_ack(sid[0], element, sid[1], block)

    def _send_ack(self, dst: int, element: int, seq_zero: int, block: int) -> None:
        if not (self.provisioned and self.alive):
            return
        ttl = self.servers.config.default_ttl if self.servers else 5
        self.originate(
            element,
            dst,
            ttl,
            self.next_seq(),
            self.tx_iv_index,
            segment_ack(seq_zero, block),
            ctl=True,
        )

    # ------------------------------------------------------------------ upper transport and access
    def _upper_rx(
        self,
        net: NetworkPDU,
        akf: bool,
        aid: int,
        upper: bytes,
        szmic: int,
        seq_auth: int,
    ) -> None:
        access, label = None, ""
        if akf:
            candidates = [(i, k) for i, k in self.app_keys.items() if k.aid == aid]
            if not candidates:
                return  # an AppKey we do not have (a fresh node): not for us
            for index, ak in candidates:
                access = upper_decrypt(
                    ak.key,
                    NONCE_APP,
                    net.iv_index,
                    seq_auth,
                    net.src,
                    net.dst,
                    upper,
                    szmic,
                )
                if access is not None:
                    label = f"app{index}"
                    break
        elif net.dst in self.element_set:
            for dev_key in self.dev_keys_for(net):
                access = upper_decrypt(
                    dev_key,
                    NONCE_DEVICE,
                    net.iv_index,
                    seq_auth,
                    net.src,
                    net.dst,
                    upper,
                    szmic,
                )
                if access is not None:
                    label = "dev"
                    break
        else:
            return  # a device-key message is for one node only
        if access is None:
            self.mesh.note_upper_undecryptable(self, net)
            return
        self._rpl_update(net)
        try:
            opcode, cid, params = decode_opcode(access)
        except ValueError:
            self.mesh.note_malformed(self, net)
            return
        msg = Received(net.src, net.dst, net.ttl, opcode, cid, params, label, seq_auth)
        self.received.append(msg)
        if self.servers is not None:
            self.servers.dispatch(msg)
        self.on_access(msg)

    def dev_keys_for(self, net: NetworkPDU) -> list[bytes]:
        """The device keys a device-key message to us may be sealed with (ours; the provisioner knows them all)."""
        return [self.dev_key]

    def on_access(self, msg: Received) -> None:
        """Hook: a participant without servers (the provisioner) reads its replies here."""

    # ------------------------------------------------------------------ sending access messages
    def app_tx(self, index: int = 0) -> TxKey:
        ak = self.app_keys[index]
        return TxKey(ak.key, NONCE_APP, True, ak.aid, f"app{index}")

    def dev_tx(self, dev_key: bytes | None = None) -> TxKey:
        return TxKey(dev_key or self.dev_key, NONCE_DEVICE, False, 0, "dev")

    def send(
        self, src: int, dst: int, access: bytes, key: TxKey, ttl: int | None = None
    ) -> None:
        """Send an access message in the background (segmented above 11 bytes)."""
        if not (self.provisioned and self.alive):
            return
        self.spawn(self.send_now(src, dst, access, key, ttl))

    async def send_now(
        self, src: int, dst: int, access: bytes, key: TxKey, ttl: int | None = None
    ) -> None:
        if ttl is None:
            ttl = self.servers.config.default_ttl if self.servers else 5
        if len(access) <= 11:
            seq, iv = self.next_seq(), self.tx_iv_index
            upper = upper_encrypt(key.key, key.nonce, iv, seq, src, dst, access)
            self.mesh.note_sent(src, dst, access, 1).segments[0].add((src, iv, seq))
            self.originate(
                src,
                dst,
                ttl,
                seq,
                iv,
                lower_unsegmented_access(key.aid, upper, key.akf),
            )
            return
        lock = self._sar_locks.setdefault(dst, asyncio.Lock())
        async with lock:
            await self._send_segmented(src, dst, access, key, ttl)

    async def _send_segmented(
        self, src: int, dst: int, access: bytes, key: TxKey, ttl: int
    ) -> None:
        n = -(-(len(access) + 4) // SEGMENT_SIZE)
        iv, seq0 = self.tx_iv_index, self.reserve_seq(n)
        upper = upper_encrypt(key.key, key.nonce, iv, seq0, src, dst, access)
        segments = lower_segments_access(
            key.aid, seq0, upper, SEGMENT_SIZE, akf=key.akf
        )
        sent = self.mesh.note_sent(src, dst, access, n)
        if not is_unicast(dst):
            for repeat in range(SAR_GROUP_REPEATS):
                for i, seg in enumerate(segments):
                    seq = seq0 + i if repeat == 0 else self.next_seq()
                    sent.segments[i].add((src, iv, seq))
                    self.originate(src, dst, ttl, seq, iv, seg)
                await asyncio.sleep(0.05)
            return
        sar = _TxSar()
        self._tx_sar[(dst, seq0 & 0x1FFF)] = sar
        pending = set(range(n))
        try:
            for attempt in range(SAR_ROUNDS):
                sar.event.clear()
                for i in sorted(pending):
                    seq = seq0 + i if attempt == 0 else self.next_seq()
                    sent.segments[i].add((src, iv, seq))
                    self.originate(src, dst, ttl, seq, iv, segments[i])
                try:
                    await asyncio.wait_for(sar.event.wait(), SAR_ACK_WAIT)
                except TimeoutError:
                    pass
                if sar.cancelled:
                    return
                pending = {i for i in pending if not (sar.block >> i) & 1}
                if not pending:
                    return
            self.sar_failed.append((dst, seq0))
        finally:
            self._tx_sar.pop((dst, seq0 & 0x1FFF), None)


def _with_ttl(net: NetworkPDU, ttl: int) -> NetworkPDU:
    return NetworkPDU(
        net.ivi,
        net.iv_index,
        net.nid,
        net.ctl,
        ttl,
        net.seq,
        net.src,
        net.dst,
        net.transport_pdu,
    )
