"""A simulated proxy node and the GATT link the `ProxyClient` talks to (Mesh Profile §6).

`ProxyNode.connect()` returns a bleak-like client (`write_gatt_char` / `start_notify` / `stop_notify` /
`disconnect` / `mtu_size` / `is_connected`) for `ProxyClient.attach`. Behind it the node:

- reassembles the client's proxy PDUs and frames its own to the link's MTU (SAR, §6.3.1);
- keeps the proxy filter (§6.4, §6.6): a new connection starts on an empty white list; Set Filter Type, Add and
  Remove Addresses are answered with a Filter Status; the source of every network PDU from the client joins a
  white list (leaves a black list);
- forwards to the client what it hears on the air (and what it sends itself) when the filter lets the destination
  through, and relays the client's PDUs onto the air;
- sends a Secure Network Beacon when the client subscribes, every `beacon_interval` virtual seconds, and whenever
  its IV or key refresh state changes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from jhmesh.client import MESH_PROXY_DATA_IN, MESH_PROXY_DATA_OUT
from jhmesh.crypto import aes_cmac
from jhmesh.pdu import (
    FILTER_BLACKLIST,
    FILTER_WHITELIST,
    PROXY_BEACON,
    PROXY_CONFIG,
    PROXY_NETWORK_PDU,
    NetworkPDU,
    ProxyReassembler,
    network_encrypt,
    proxy_frame,
)

from .loss import Packet
from .node import Key, SimNode, key_of

if TYPE_CHECKING:
    from jhmesh.cdb import Node
    from jhmesh.crypto import NetKeyMaterial

    from .mesh import Mesh


class SimGattClient:
    """The client end of one GATT connection to a `ProxyNode`, shaped like a connected `BleakClient`."""

    def __init__(self, proxy: ProxyNode, mtu: int) -> None:
        self.proxy = proxy
        self.address = f"SIM:{proxy.addr:04X}"
        self.mtu_size = mtu
        self.is_connected = True
        self.notify: Callable[[Any, bytearray], None] | None = None
        self.disconnected_callback: Callable[[SimGattClient], None] | None = None
        self.writes = 0

    async def start_notify(
        self, char: str, callback: Callable[[Any, bytearray], None]
    ) -> None:
        assert char == MESH_PROXY_DATA_OUT
        self._check()
        self.notify = callback
        self.proxy.subscribed(self)

    async def stop_notify(self, char: str) -> None:
        assert char == MESH_PROXY_DATA_OUT
        self.notify = None

    async def write_gatt_char(
        self, char: str, data: bytes, response: bool | None = None
    ) -> None:
        assert char == MESH_PROXY_DATA_IN
        assert response is False
        self._check()
        assert len(data) <= self.mtu_size - 3, "a GATT write must fit the ATT MTU"
        self.writes += 1
        self.proxy.from_client(self, bytes(data))

    async def disconnect(self) -> None:
        if self.is_connected:
            self.is_connected = False
            self.proxy.closed(self)

    def _check(self) -> None:
        if not self.is_connected:
            raise ConnectionError("simulated GATT link is down")


class ProxyNode(SimNode):
    """A node with the GATT Proxy feature and (at most) one connected client."""

    def __init__(self, mesh: Mesh, node: Node, *, fresh: bool = False) -> None:
        super().__init__(mesh, node, fresh=fresh)
        self.link: SimGattClient | None = None
        self.filter_type = FILTER_WHITELIST
        self.filter: set[int] = set()
        self.beacon_interval = 10.0
        self.beacons_sent = 0
        self._reasm = ProxyReassembler()
        self._beacon_timer: asyncio.TimerHandle | None = None
        self._out: list[tuple[list[bytes], Key | None]] = []
        self._drain: asyncio.TimerHandle | None = None
        self.filter_statuses = 0
        # the copies `Quirks.proxy_forwards_every_copy` handed on to the client
        self.repeats_forwarded = 0

    # ------------------------------------------------------------------ the link
    def connect(self, mtu: int = 69) -> SimGattClient:
        """A new connection (the previous one, if any, is dropped): an empty white list, nothing reassembled."""
        if self.link is not None:
            self.drop_link()
        self.link = SimGattClient(self, mtu)
        self.mesh.client_proxy = self
        self.filter_type, self.filter = FILTER_WHITELIST, set()
        self._reasm = ProxyReassembler()
        return self.link

    def drop_link(self) -> None:
        """The connection is lost (out of range, the node restarts): the client's disconnected callback fires."""
        link = self.link
        if link is None:
            return
        link.is_connected = False
        self.closed(link)
        if link.disconnected_callback is not None:
            link.disconnected_callback(link)

    def stop(self) -> list[asyncio.Task[Any]]:
        """The node's timers and tasks (`SimNode.stop`), its link (closed without the callback) and its queue."""
        if self.link is not None:
            self.link.is_connected = False
            self.closed(self.link)
        if self._drain is not None:
            self._drain.cancel()
            self._drain = None
        return super().stop()

    def closed(self, link: SimGattClient) -> None:
        if link is self.link:
            self.link = None
            if self._beacon_timer is not None:
                self._beacon_timer.cancel()
                self._beacon_timer = None
            self._out.clear()

    def subscribed(self, link: SimGattClient) -> None:
        if link is self.link:
            self.send_beacon()
            self._schedule_beacon()

    def _schedule_beacon(self) -> None:
        if self._beacon_timer is not None:
            self._beacon_timer.cancel()
        self._beacon_timer = asyncio.get_running_loop().call_later(
            self.beacon_interval, self._beacon_tick
        )

    def _beacon_tick(self) -> None:
        self._beacon_timer = None
        if self.link is not None:
            self.send_beacon()
            self._schedule_beacon()

    @property
    def _live(self) -> bool:
        link = self.link
        return link is not None and link.is_connected and link.notify is not None

    # ------------------------------------------------------------------ beacons
    def beacon_payload(self) -> bytes:
        """Our Secure Network Beacon: secured with the new key during key refresh Phase 2 (§3.10.4)."""
        nk = self.tx_net_key
        flags = (1 if self.kr_phase == 2 else 0) | (2 if self.iv_update else 0)
        body = bytes([flags]) + nk.network_id + self.iv_index.to_bytes(4, "big")
        return b"\x01" + body + aes_cmac(nk.beacon_key, body)[:8]

    def send_beacon(self) -> None:
        if not (self._live and self.provisioned and self.alive):
            return
        packet = Packet("gatt-out", "beacon", None, self.addr)
        if self.mesh.loss.lost(packet, self.mesh.rng) is None:
            self.beacons_sent += 1
            self._to_link(PROXY_BEACON, self.beacon_payload())

    def state_changed(self) -> None:
        """IV index or key refresh moved: tell the client at once (a real proxy beacons on the change)."""
        self.send_beacon()

    # ------------------------------------------------------------------ client → proxy
    def from_client(self, link: SimGattClient, frame: bytes) -> None:
        if link is not self.link:
            return
        got = self._reasm.feed(frame)
        if got is None:
            return
        msg_type, payload = got
        if not (self.provisioned and self.alive):
            return
        if msg_type == PROXY_NETWORK_PDU:
            self._client_network(payload)
        elif msg_type == PROXY_CONFIG:
            self._client_config(payload)

    def _client_network(self, raw: bytes) -> None:
        opened = self.open(raw)
        if opened is None:
            self.mesh.note_undecryptable(self, raw, "gatt")
            return
        net, nk = opened
        self.mesh.note_client_tx(net, self)
        if self._gatt_lost(Packet("gatt-in", "network", net, None, self.addr)):
            return
        if self.filter_type == FILTER_WHITELIST:
            self.filter.add(net.src)
        else:
            self.filter.discard(net.src)
        self.network_rx(net, nk, from_gatt=True)

    def _client_config(self, raw: bytes) -> None:
        opened = self.open(raw, proxy=True)
        if opened is None:
            self.mesh.note_undecryptable(self, raw, "gatt")
            return
        net, _nk = opened
        self.mesh.note_client_tx(net, self)
        if self._gatt_lost(Packet("gatt-in", "config", net, None, self.addr)):
            return
        pdu = net.transport_pdu
        op = pdu[0]
        addresses = [
            int.from_bytes(pdu[i : i + 2], "big") for i in range(1, len(pdu) - 1, 2)
        ]
        if op == 0x00 and len(pdu) >= 2:
            self.filter_type, self.filter = pdu[1], set()
        elif op == 0x01:
            self.filter.update(addresses)
        elif op == 0x02:
            self.filter.difference_update(addresses)
        else:
            return
        self._filter_status()

    def _filter_status(self) -> None:
        nk, iv = self.tx_net_key, self.tx_iv_index
        seq = self.next_seq()
        transport = bytes([0x03, self.filter_type]) + len(self.filter).to_bytes(
            2, "big"
        )
        raw = network_encrypt(
            nk, iv, True, 0, seq, self.addr, 0x0000, transport, proxy=True
        )
        net = NetworkPDU(iv & 1, iv, nk.nid, True, 0, seq, self.addr, 0x0000, transport)
        self.mesh.note_origin(self, net, raw)
        if not self._gatt_lost(Packet("gatt-out", "config", net, self.addr)):
            self.filter_statuses += 1
            self._to_link(PROXY_CONFIG, raw)

    def _gatt_lost(self, packet: Packet) -> bool:
        why = self.mesh.loss.lost(packet, self.mesh.rng)
        if why is None:
            return False
        assert packet.net is not None
        self.mesh.note_drop(key_of(packet.net), why)
        return True

    # ------------------------------------------------------------------ air → client
    def _passes(self, dst: int) -> bool:
        if self.filter_type == FILTER_BLACKLIST:
            return dst not in self.filter
        return dst in self.filter

    def forward(self, net: NetworkPDU, nk: NetKeyMaterial, *, from_gatt: bool) -> None:
        super().forward(net, nk, from_gatt=from_gatt)
        if not from_gatt:
            self._forward_to_client(net, nk)

    def network_rx(
        self, net: NetworkPDU, nk: NetKeyMaterial, *, from_gatt: bool
    ) -> None:
        repeat = key_of(net) in self.cache
        super().network_rx(net, nk, from_gatt=from_gatt)
        if (
            repeat
            and self.mesh.quirks.proxy_forwards_every_copy
            and not from_gatt
            and net.dst not in self.element_set
            and net.src not in self.element_set
        ):
            self.repeats_forwarded += 1
            self._forward_to_client(net, nk)

    def _forward_to_client(self, net: NetworkPDU, nk: NetKeyMaterial) -> None:
        ttl = net.ttl - 1 if net.ttl >= 2 else net.ttl
        raw = network_encrypt(
            nk, net.iv_index, net.ctl, ttl, net.seq, net.src, net.dst, net.transport_pdu
        )
        self._to_client(raw, net)

    def after_originate(self, raw: bytes, net: NetworkPDU) -> None:
        self._to_client(raw, net)

    def _to_client(self, raw: bytes, net: NetworkPDU) -> None:
        key = key_of(net)
        if not self._live:
            if (
                self.mesh.client_proxy is self
            ):  # the client's proxy, between links: not a loss
                self.mesh.note_drop(key, "no-link")
            return
        if not self._passes(net.dst):
            self.mesh.note_drop(key, "filter")
            return
        if self._gatt_lost(Packet("gatt-out", "network", net, self.addr)):
            return
        self._to_link(PROXY_NETWORK_PDU, raw, key)

    def _to_link(self, msg_type: int, payload: bytes, key: Key | None = None) -> None:
        """Queue one proxy PDU's frames; they reach the client in order after the link latency."""
        link = self.link
        assert link is not None
        self._out.append((proxy_frame(msg_type, payload, link.mtu_size - 3), key))
        if self._drain is None:
            self._drain = asyncio.get_running_loop().call_later(
                self.mesh.loss.gatt_latency, self._deliver
            )

    def _deliver(self) -> None:
        self._drain = None
        out, self._out = self._out, []
        for frames, key in out:
            link = self.link
            if link is None or link.notify is None or not link.is_connected:
                return
            if key is not None:
                self.mesh.note_to_client(key)
            for frame in frames:
                link.notify(None, bytearray(frame))
