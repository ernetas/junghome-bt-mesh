"""Fixtures for the ``jhmesh`` library tests: synthetic CDB, a fake bleak-like proxy node, fast sleeps."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from jhmesh import client as client_mod
from jhmesh import config_messages as C
from jhmesh import standalone as standalone_mod
from jhmesh.cdb import CDB
from jhmesh.client import (
    MESH_PROXY_DATA_IN,
    MESH_PROXY_DATA_OUT,
    LocalState,
    ProxyClient,
)
from jhmesh.crypto import aes_cmac, ccm_encrypt
from jhmesh.pdu import (
    NONCE_DEVICE,
    PROXY_BEACON,
    PROXY_CONFIG,
    PROXY_NETWORK_PDU,
    NetworkPDU,
    ProxyReassembler,
    _app_nonce,
    decode_opcode,
    encode_opcode,
    lower_segments_access,
    lower_unsegmented_access,
    network_decrypt,
    network_encrypt,
    proxy_frame,
    segment_ack,
    seq_auth_from,
    upper_decrypt,
    upper_encrypt_app,
)

from .hypothesis_profiles import load as load_hypothesis_profile

load_hypothesis_profile()

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CDB_PATH = FIXTURES / "MeshNetwork.json"
META_DIR = FIXTURES / "Application Support"

OUR_SRC = 0x0D00  # the unicast address we use as a client
PROXY_NODE = 0x0148  # "Push-button 1-gang" (element 0 = light, 0x0149 = button A)
LIGHT_2G = 0x0232  # "Push-button 2-gang" (CTL light; 0x0233 temperature element; 0x0234/0x0235 buttons)
SOCKET = 0x0172  # "Socket" (0x0173 = power sensor element)
SENSOR = 0x0173
GATEWAY = 0x00DC
PHONE = 0x0001
GROUP_WC = 0xC00F
GROUP_LIVING = 0xC010
ELEMENT_GROUP_148 = 0xC061


@pytest.fixture
def enable_custom_integrations() -> None:
    """Override the HA plugin fixture so these tests never spin up a HomeAssistant instance."""


@pytest.fixture
def cdb() -> CDB:
    return CDB.load(CDB_PATH)


def refreshing_cdb(new_key: bytes, phase: int) -> CDB:
    """The fixture network as an export written mid key refresh: `key` is `new_key`, `oldKey` the fixture's."""
    net, _ = CDB.parse(CDB_PATH.read_text())
    entry = net["netKeys"][0]
    entry.update(oldKey=entry["key"], key=new_key.hex(), phase=phase)
    return CDB.from_network(net)


_real_sleep = asyncio.sleep


class FastAsyncio:
    """Stand-in for the ``asyncio`` module inside a library module: ``sleep`` records the requested delay and
    completes after a single loop turn; everything else is the real thing."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, delay: float, result: Any = None) -> Any:
        self.sleeps.append(delay)
        await _real_sleep(0)
        return result


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> FastAsyncio:
    """Make every ``asyncio.sleep`` in jhmesh.client / jhmesh.standalone instantaneous and shorten the ack timeout."""
    fa = FastAsyncio()
    monkeypatch.setattr(client_mod, "asyncio", fa)
    monkeypatch.setattr(standalone_mod, "asyncio", fa)
    monkeypatch.setattr(client_mod, "SEGMENT_ACK_TIMEOUT", 0.01)
    return fa


def upper_encrypt_dev(
    dev_key: bytes,
    iv_index: int,
    seq: int,
    src: int,
    dst: int,
    access: bytes,
    szmic: int = 0,
) -> bytes:
    """Upper transport encryption with a device key (nonce type 0x02) — the library only ever *decrypts* these."""
    return ccm_encrypt(
        dev_key,
        _app_nonce(0x02, szmic, seq, src, dst, iv_index),
        access,
        8 if szmic else 4,
    )


class FakeBleak:
    """A bleak-like GATT client that behaves as a JUNG proxy node of the fixture network.

    Everything the ``ProxyClient`` writes is reassembled and decrypted (``net_pdus`` / ``config_pdus``); a network
    PDU the replay protection drops lands in ``replayed`` instead; the Set Filter Type request is answered with a
    Filter Status of the type asked for, and ``responders`` may react to network PDUs.
    Tests inject traffic with ``send_access`` / ``send_devkey`` / ``send_ctl`` / ``send_beacon`` / ``deliver``.
    """

    def __init__(
        self,
        cdb: CDB,
        address: str = "AA:BB:CC:DD:EE:FF",
        mtu_size: int | None = 69,
        proxy_node: int = PROXY_NODE,
    ) -> None:
        self.cdb = cdb
        self.nk = cdb.net_keys[0]
        self.ak = cdb.app_keys[0]
        self.address = address
        self.mtu_size = mtu_size
        self.is_connected = True
        self.proxy_node = proxy_node
        self.iv_index = 0
        self.seq = 0x010000
        self.notify_cb: Callable[[Any, bytearray], None] | None = None
        self.writes: list[bytes] = []  # raw GATT frames as written
        self.outgoing: list[
            tuple[int, bytes]
        ] = []  # reassembled proxy PDUs (msg_type, payload)
        self.net_pdus: list[
            NetworkPDU
        ] = []  # decrypted network PDUs received from the client
        self.config_pdus: list[NetworkPDU] = []  # decrypted proxy configuration PDUs
        self.responders: list[Callable[[NetworkPDU], None]] = []
        self.answer_filter = True
        self.beacon_on_subscribe = (
            False  # a real proxy beacons right after the subscription
        )
        self.strict_decrypt = (
            True  # False: PDUs we cannot decrypt are dropped into ``undecryptable``
        )
        self.undecryptable: list[tuple[int, bytes]] = []
        # the nodes' replay list: source -> the last (IV index, SEQ) taken from it; and (src, IV index, SEQ) of
        # every PDU it dropped, which the `link` fixture's teardown fails on unless `expect_replays` is set
        self.rpl: dict[int, tuple[int, int]] = {}
        self.replayed: list[tuple[int, int, int]] = []
        self.expect_replays = False
        self.write_error: Exception | None = None
        self.notify_error: Exception | None = None
        self.disconnect_error: Exception | None = None
        self.disconnect_calls = 0
        self.stop_notify_error: Exception | None = None
        # the callback stays after a stop_notify: a transport may still deliver after unsubscribing
        self.stop_notify_calls = 0
        self._reasm = ProxyReassembler()

    # ---------------------------------------------------------------- bleak surface
    async def start_notify(
        self, char: str, cb: Callable[[Any, bytearray], None]
    ) -> None:
        assert char == MESH_PROXY_DATA_OUT
        if self.notify_error is not None:
            raise self.notify_error
        self.notify_cb = cb
        if self.beacon_on_subscribe:
            self.deliver_soon(PROXY_BEACON, self.beacon_payload(iv_index=self.iv_index))

    async def stop_notify(self, char: str) -> None:
        assert char == MESH_PROXY_DATA_OUT
        self.stop_notify_calls += 1
        if self.stop_notify_error is not None:
            raise self.stop_notify_error

    async def write_gatt_char(
        self, char: str, data: bytes, response: bool | None = None
    ) -> None:
        assert char == MESH_PROXY_DATA_IN
        assert response is False
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(bytes(data))
        r = self._reasm.feed(bytes(data))
        if r is None:
            return
        msg_type, payload = r
        self.outgoing.append((msg_type, payload))
        if msg_type == PROXY_CONFIG:
            n = network_decrypt(self.nk, self.iv_index, payload, proxy=True)
            if n is None:
                assert not self.strict_decrypt, (
                    "proxy config PDU must decrypt with the proxy nonce"
                )
                self.undecryptable.append(
                    (msg_type, payload)
                )  # a real proxy silently drops it
                return
            self.config_pdus.append(n)
            if self.answer_filter and n.transport_pdu[0] == 0x00:
                self.deliver_soon(
                    PROXY_CONFIG, self.filter_status_pdu(n.transport_pdu[1])
                )
        elif msg_type == PROXY_NETWORK_PDU:
            n = network_decrypt(self.nk, self.iv_index, payload)
            if n is None:
                assert not self.strict_decrypt, (
                    "network PDU must decrypt with the network nonce"
                )
                self.undecryptable.append((msg_type, payload))
                return
            if self._replay(n):
                return
            self.net_pdus.append(n)
            for resp in list(self.responders):
                resp(n)

    def _replay(self, n: NetworkPDU) -> bool:
        """The nodes' replay protection (§3.8.8): a PDU at or below the last (IV index, SEQ) of its source is
        dropped and listed in `replayed` — the network PDUs only, as `FakeProxyLink` and the simulated nodes do."""
        seen = (n.iv_index, n.seq)
        last = self.rpl.get(n.src)
        if last is not None and seen <= last:
            self.replayed.append((n.src, *seen))
            return True
        self.rpl[n.src] = seen
        return False

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.disconnect_error is not None:
            raise self.disconnect_error
        self.is_connected = False

    # ---------------------------------------------------------------- helpers: delivering to the client
    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def deliver(self, msg_type: int, payload: bytes) -> None:
        assert self.notify_cb is not None, (
            "client did not subscribe to Mesh Proxy Data Out"
        )
        for frame in proxy_frame(msg_type, payload, (self.mtu_size or 23) - 3):
            self.notify_cb(None, bytearray(frame))

    def deliver_soon(self, msg_type: int, payload: bytes) -> None:
        asyncio.get_running_loop().call_soon(self.deliver, msg_type, payload)

    def filter_status_pdu(self, filter_type: int = 1, list_size: int = 0) -> bytes:
        return network_encrypt(
            self.nk,
            self.iv_index,
            ctl=True,
            ttl=0,
            seq=self.next_seq(),
            src=self.proxy_node,
            dst=0x0000,
            transport_pdu=bytes([0x03, filter_type]) + list_size.to_bytes(2, "big"),
            proxy=True,
        )

    def access_pdus(
        self,
        src: int,
        dst: int,
        access: bytes,
        seq: int | None = None,
        ttl: int = 3,
        szmic: int = 0,
        iv_index: int | None = None,
        aid: int | None = None,
    ) -> tuple[int, list[bytes]]:
        """Network PDUs for an AppKey-0 access message (unsegmented if it fits, else segmented)."""
        iv = self.iv_index if iv_index is None else iv_index
        seq0 = self.next_seq() if seq is None else seq
        aid = self.ak.aid if aid is None else aid
        upper = upper_encrypt_app(self.ak, iv, seq0, src, dst, access, szmic)
        if len(access) <= 11 and not szmic:
            return seq0, [
                network_encrypt(
                    self.nk,
                    iv,
                    False,
                    ttl,
                    seq0,
                    src,
                    dst,
                    lower_unsegmented_access(aid, upper),
                )
            ]
        segs = lower_segments_access(aid, seq0, upper, szmic=szmic)
        self.seq = max(self.seq, seq0 + len(segs))
        return seq0, [
            network_encrypt(self.nk, iv, False, ttl, seq0 + i, src, dst, s)
            for i, s in enumerate(segs)
        ]

    def devkey_pdus(
        self,
        src: int,
        dst: int,
        access: bytes,
        dev_key: bytes,
        seq: int | None = None,
        ttl: int = 3,
    ) -> tuple[int, list[bytes]]:
        """Network PDUs for a DevKey-encrypted access message (AKF=0)."""
        seq0 = self.next_seq() if seq is None else seq
        upper = upper_encrypt_dev(dev_key, self.iv_index, seq0, src, dst, access)
        if len(access) <= 11:
            return seq0, [
                network_encrypt(
                    self.nk, self.iv_index, False, ttl, seq0, src, dst, b"\x00" + upper
                )
            ]
        segs = [
            bytes([s[0] & ~0x40]) + s[1:] for s in lower_segments_access(0, seq0, upper)
        ]
        self.seq = max(self.seq, seq0 + len(segs))
        return seq0, [
            network_encrypt(self.nk, self.iv_index, False, ttl, seq0 + i, src, dst, s)
            for i, s in enumerate(segs)
        ]

    def send_access(self, src: int, dst: int, access: bytes, **kw: Any) -> int:
        seq0, pdus = self.access_pdus(src, dst, access, **kw)
        for p in pdus:
            self.deliver(PROXY_NETWORK_PDU, p)
        return seq0

    def send_devkey(
        self, src: int, dst: int, access: bytes, dev_key: bytes, **kw: Any
    ) -> int:
        seq0, pdus = self.devkey_pdus(src, dst, access, dev_key, **kw)
        for p in pdus:
            self.deliver(PROXY_NETWORK_PDU, p)
        return seq0

    def send_ctl(self, src: int, dst: int, transport_pdu: bytes, ttl: int = 3) -> int:
        seq = self.next_seq()
        self.deliver(
            PROXY_NETWORK_PDU,
            network_encrypt(
                self.nk, self.iv_index, True, ttl, seq, src, dst, transport_pdu
            ),
        )
        return seq

    def send_ack(self, src: int, dst: int, seq_zero: int, block: int) -> None:
        self.send_ctl(src, dst, segment_ack(seq_zero, block))

    def beacon_payload(
        self,
        iv_index: int = 0,
        iv_update: bool = False,
        key_refresh: bool = False,
        valid: bool = True,
    ) -> bytes:
        flags = (1 if key_refresh else 0) | (2 if iv_update else 0)
        body = bytes([flags]) + self.nk.network_id + iv_index.to_bytes(4, "big")
        auth = aes_cmac(self.nk.beacon_key, body)[:8]
        if not valid:
            auth = bytes(a ^ 0xFF for a in auth)
        return b"\x01" + body + auth

    def send_beacon(self, **kw: Any) -> None:
        self.deliver(PROXY_BEACON, self.beacon_payload(**kw))

    # ---------------------------------------------------------------- helpers: inspecting what the client sent
    def sent_access(self) -> list[tuple[int, int, int, int, bytes]]:
        """All AppKey access messages we received from the client as (src, dst, ttl, seq_auth, access_pdu).

        Device-key messages (AKF=0) are left to ``sent_config``.
        """
        out: list[tuple[int, int, int, int, bytes]] = []
        segments: dict[
            tuple[int, int], dict[int, bytes]
        ] = {}  # (src, seq_zero) -> seg_o -> data
        for n in self.net_pdus:
            if n.ctl or not n.transport_pdu[0] & 0x40:
                continue
            iv = self.iv_index if (self.iv_index & 1) == n.ivi else self.iv_index - 1
            b0 = n.transport_pdu[0]
            if not b0 & 0x80:
                access = upper_decrypt(
                    self.ak.key, 0x01, iv, n.seq, n.src, n.dst, n.transport_pdu[1:], 0
                )
                assert access is not None, (
                    "unsegmented access message must decrypt with AppKey 0"
                )
                out.append((n.src, n.dst, n.ttl, n.seq, access))
                continue
            hdr = int.from_bytes(n.transport_pdu[1:4], "big")
            szmic, seq_zero, seg_o, seg_n = (
                (hdr >> 23) & 1,
                (hdr >> 10) & 0x1FFF,
                (hdr >> 5) & 0x1F,
                hdr & 0x1F,
            )
            parts = segments.setdefault((n.src, seq_zero), {})
            parts[seg_o] = n.transport_pdu[4:]
            if len(parts) == seg_n + 1:
                seq_auth = seq_auth_from(n.seq, seq_zero)
                data = b"".join(parts[i] for i in range(seg_n + 1))
                access = upper_decrypt(
                    self.ak.key, 0x01, iv, seq_auth, n.src, n.dst, data, szmic
                )
                assert access is not None, (
                    "reassembled segmented message must decrypt with AppKey 0"
                )
                out.append((n.src, n.dst, n.ttl, seq_auth, access))
                del segments[(n.src, seq_zero)]
        return out

    def sent_acks(self) -> list[tuple[int, int, int, int]]:
        """Segment Acknowledgments received from the client as (src, dst, seq_zero, block_ack)."""
        out = []
        for n in self.net_pdus:
            if n.ctl and n.transport_pdu[0] == 0x00:
                hdr = int.from_bytes(n.transport_pdu[1:3], "big")
                out.append(
                    (
                        n.src,
                        n.dst,
                        (hdr >> 2) & 0x1FFF,
                        int.from_bytes(n.transport_pdu[3:7], "big"),
                    )
                )
        return out

    def auto_ack(self, drop_once: set[int] | None = None) -> None:
        """Acknowledge every segment addressed to a unicast element as a real receiver would (one ack per
        received segment carrying the cumulative block). Segment indexes in ``drop_once`` are lost on their
        first transmission."""
        received: dict[tuple[int, int], set[int]] = {}
        drops = set(drop_once or ())

        def respond(n: NetworkPDU) -> None:
            if n.ctl or not n.transport_pdu[0] & 0x80 or n.dst >= 0x8000:
                return
            hdr = int.from_bytes(n.transport_pdu[1:4], "big")
            seq_zero, seg_o = (hdr >> 10) & 0x1FFF, (hdr >> 5) & 0x1F
            if seg_o in drops:
                drops.discard(seg_o)
                return
            got = received.setdefault((n.src, seq_zero), set())
            got.add(seg_o)
            block = 0
            for i in got:
                block |= 1 << i
            asyncio.get_running_loop().call_soon(
                self.send_ack, n.dst, n.src, seq_zero, block
            )

        self.responders.append(respond)

    # ---------------------------------------------------------------- helpers: DevKey (Config Server) traffic
    def dev_key(self, node: int) -> bytes:
        """Device key of the node whose primary unicast is ``node``."""
        n = self.cdb.node_by_addr(node)
        assert n is not None, f"{node:04X} is not an element of any node"
        assert n.unicast == node, f"{node:04X} is not a node's primary unicast"
        return n.dev_key

    def sent_config(self) -> list[tuple[int, int, int, int, bytes]]:
        """Device-key (AKF=0) access messages received from the client as (src, dst, ttl, seq_auth, access_pdu).

        Each one — unsegmented or reassembled — must decrypt with the *destination* node's device key under the
        device nonce; AID must be 0.
        """
        out: list[tuple[int, int, int, int, bytes]] = []
        segments: dict[tuple[int, int], dict[int, bytes]] = {}
        for n in self.net_pdus:
            if n.ctl or n.transport_pdu[0] & 0x40:
                continue  # control PDU, or an AppKey message (AKF=1)
            iv = self.iv_index if (self.iv_index & 1) == n.ivi else self.iv_index - 1
            assert n.transport_pdu[0] & 0x3F == 0, "AID must be 0 with AKF=0"
            if not n.transport_pdu[0] & 0x80:
                access = upper_decrypt(
                    self.dev_key(n.dst),
                    NONCE_DEVICE,
                    iv,
                    n.seq,
                    n.src,
                    n.dst,
                    n.transport_pdu[1:],
                    0,
                )
                assert access is not None, (
                    "config message must decrypt with the node's device key"
                )
                out.append((n.src, n.dst, n.ttl, n.seq, access))
                continue
            hdr = int.from_bytes(n.transport_pdu[1:4], "big")
            szmic, seq_zero, seg_o, seg_n = (
                (hdr >> 23) & 1,
                (hdr >> 10) & 0x1FFF,
                (hdr >> 5) & 0x1F,
                hdr & 0x1F,
            )
            parts = segments.setdefault((n.src, seq_zero), {})
            parts[seg_o] = n.transport_pdu[4:]
            if len(parts) == seg_n + 1:
                seq_auth = seq_auth_from(n.seq, seq_zero)
                data = b"".join(parts[i] for i in range(seg_n + 1))
                access = upper_decrypt(
                    self.dev_key(n.dst),
                    NONCE_DEVICE,
                    iv,
                    seq_auth,
                    n.src,
                    n.dst,
                    data,
                    szmic,
                )
                assert access is not None, (
                    "reassembled config message must decrypt with the node's device key"
                )
                out.append((n.src, n.dst, n.ttl, seq_auth, access))
                del segments[(n.src, seq_zero)]
        return out

    def auto_config(self, reply: Callable[[int, bytes], bytes | None]) -> None:
        """Behave as the nodes' Configuration Servers.

        For every complete device-key message the client sends to node ``dst``, ``reply(dst, access_pdu)``
        returns the Status access PDU to send back (DevKey-encrypted from the node to the client, segmented when
        needed) or None. Combine with ``auto_ack()`` so segmented requests complete.
        """
        answered = 0

        def respond(n: NetworkPDU) -> None:
            nonlocal answered
            if n.ctl or n.transport_pdu[0] & 0x40:
                return
            msgs = self.sent_config()
            for src, dst, _ttl, _seq, access in msgs[answered:]:
                status = reply(dst, access)
                if status is not None:
                    asyncio.get_running_loop().call_soon(
                        self.send_devkey, dst, src, status, self.dev_key(dst)
                    )
            answered = len(msgs)

        self.responders.append(respond)


# the node-wide states every fake node holds (wire fields): relay on, 2 retransmissions 90 ms apart; 2 network
# retransmissions 100 ms apart; TTL 5; beacon on; GATT proxy on — what the installation answered (hidden-features §9)
CONFIG_DEFAULTS: dict[str, Any] = {
    "relay": (1, 2, 8),
    "network_transmit": (2, 9),
    "default_ttl": 5,
    "beacon": 1,
    "gatt_proxy": 1,
}
_CONFIG_NODE_GETS = {
    C.CONFIG_RELAY_GET: "relay",
    C.CONFIG_NETWORK_TRANSMIT_GET: "network_transmit",
    C.CONFIG_DEFAULT_TTL_GET: "default_ttl",
    C.CONFIG_BEACON_GET: "beacon",
    C.CONFIG_GATT_PROXY_GET: "gatt_proxy",
}
_CONFIG_MODEL_GETS = {
    C.CONFIG_MODEL_PUBLICATION_GET: "publication",
    C.CONFIG_SIG_MODEL_SUBSCRIPTION_GET: "subscriptions",
    C.CONFIG_VENDOR_MODEL_SUBSCRIPTION_GET: "subscriptions",
    C.CONFIG_SIG_MODEL_APP_GET: "app_keys",
    C.CONFIG_VENDOR_MODEL_APP_GET: "app_keys",
}


def on_small_stack(func: Callable[[], object]) -> None:
    """Run `func` on a thread with an 8 MiB stack and re-raise what it raised.

    The JSON decoder's depth limit follows the C stack on Python 3.14, which a runner may leave unlimited: there a
    document nested 100 000 deep parses instead of raising RecursionError. A fixed stack makes that limit the same
    everywhere. On 3.13 the limit is a fixed count instead, and a stack too small for it (1 MiB) overflows before
    the count is reached — a segfault; 8 MiB holds it. The tests nest 1 000 000 deep, which raises on both versions
    with stacks up to 64 MiB.
    """
    raised: list[BaseException] = []

    def run() -> None:
        try:
            func()
        except BaseException as err:  # handed to the calling thread
            raised.append(err)

    size = threading.stack_size(8 << 20)
    try:
        thread = threading.Thread(target=run)
        thread.start()
    finally:
        threading.stack_size(size)
    thread.join()
    if raised:
        raise raised[0]


def pack_key_indexes(keys: list[int]) -> bytes:
    """AppKey indexes as a Model App List carries them (§4.3.1.1): two per 3 octets, an odd last one in 2."""
    out = b"".join(
        (keys[i] | keys[i + 1] << 12).to_bytes(3, "little")
        for i in range(0, len(keys) - 1, 2)
    )
    return out + (keys[-1].to_bytes(2, "little") if len(keys) % 2 else b"")


class FakeConfigServers:
    """The nodes' Configuration Servers as the audit reads them: every Get answered from the export.

    A callable for ``FakeBleak.auto_config`` and the integration tests' ``FakeProxyLink.config_reply``. What a node
    holds apart from the export is set per test: ``settings[node][kind]`` (wire fields, `CONFIG_DEFAULTS`' shape),
    ``publish`` / ``subscribe`` / ``app_keys`` by (element, model); ``refuse`` by (element, model, kind) gives
    that status code instead; ``silent`` nodes answer nothing, ``silent_gets`` swallows a Get by (element, model,
    kind) — or (node, kind) for a node-wide one. ``seen`` lists every (node, access pdu) asked.
    """

    def __init__(self, cdb: CDB) -> None:
        self.cdb = cdb
        self.settings: dict[int, dict[str, Any]] = {}
        self.publish: dict[tuple[int, str], int] = {}
        self.subscribe: dict[tuple[int, str], list[int]] = {}
        self.app_keys: dict[tuple[int, str], list[int]] = {}
        self.refuse: dict[tuple[int, str, str], int] = {}
        self.silent: set[int] = set()
        self.silent_gets: set[tuple[Any, ...]] = set()
        self.seen: list[tuple[int, bytes]] = []

    def _export(self, element: int, model: str) -> dict[str, Any]:
        el = self.cdb.element(element)
        assert el is not None
        return next(m for m in el.raw_models if m["modelId"] == model)

    def __call__(self, node: int, access: bytes) -> bytes | None:
        self.seen.append((node, access))
        op, _cid, p = decode_opcode(access)
        if node in self.silent:
            return None
        if op in _CONFIG_NODE_GETS:
            kind = _CONFIG_NODE_GETS[op]
            if (node, kind) in self.silent_gets:
                return None
            return self._node_status(node, kind)
        kind = _CONFIG_MODEL_GETS[op]
        element = int.from_bytes(p[:2], "little")
        model = C.model_id_str(C.decode_model_id(p[2:]))
        if (element, model, kind) in self.silent_gets:
            return None
        return self._model_status(element, model, kind, p)

    def _node_status(self, node: int, kind: str) -> bytes:
        value = {**CONFIG_DEFAULTS, **self.settings.get(node, {})}[kind]
        if kind == "relay":
            relay, count, steps = value
            return encode_opcode(C.CONFIG_RELAY_STATUS) + bytes(
                [relay, count | steps << 3]
            )
        if kind == "network_transmit":
            count, steps = value
            return encode_opcode(C.CONFIG_NETWORK_TRANSMIT_STATUS) + bytes(
                [count | steps << 3]
            )
        status = {
            "default_ttl": C.CONFIG_DEFAULT_TTL_STATUS,
            "beacon": C.CONFIG_BEACON_STATUS,
            "gatt_proxy": C.CONFIG_GATT_PROXY_STATUS,
        }[kind]
        return encode_opcode(status) + bytes([value])

    def _model_status(self, element: int, model: str, kind: str, get: bytes) -> bytes:
        """The model's status: `get` (element + model id) echoed behind the status byte, then its value."""
        status = bytes([self.refuse.get((element, model, kind), C.STATUS_SUCCESS)])
        export = self._export(element, model)
        vendor = C.is_vendor_model(model)
        if kind == "publication":
            publish = export.get("publish")
            address = self.publish.get(
                (element, model), int(publish["address"], 16) if publish else 0
            )
            fields = C.model_publication_set(element, address, model)[1:]
            return encode_opcode(C.CONFIG_MODEL_PUBLICATION_STATUS) + status + fields
        if kind == "subscriptions":
            addresses = self.subscribe.get(
                (element, model), [int(a, 16) for a in export.get("subscribe", [])]
            )
            opcode = (
                C.CONFIG_VENDOR_MODEL_SUBSCRIPTION_LIST
                if vendor
                else C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST
            )
            return (
                encode_opcode(opcode)
                + status
                + get
                + b"".join(a.to_bytes(2, "little") for a in addresses)
            )
        keys = self.app_keys.get((element, model), list(export.get("bind", [])))
        opcode = (
            C.CONFIG_VENDOR_MODEL_APP_LIST if vendor else C.CONFIG_SIG_MODEL_APP_LIST
        )
        return encode_opcode(opcode) + status + get + pack_key_indexes(keys)


@pytest.fixture
def state() -> LocalState:
    return LocalState(None, OUR_SRC)


class Recorder:
    def __init__(self) -> None:
        self.messages: list[Any] = []
        self.beacons: list[Any] = []
        self.disconnects = 0

    def on_message(self, m: Any) -> None:
        self.messages.append(m)

    def on_beacon(self, b: Any) -> None:
        self.beacons.append(b)

    def on_disconnect(self) -> None:
        self.disconnects += 1


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def proxy(
    cdb: CDB, state: LocalState, recorder: Recorder, fast: FastAsyncio
) -> ProxyClient:
    return ProxyClient(
        cdb,
        state,
        ttl=5,
        on_message=recorder.on_message,
        on_disconnect=recorder.on_disconnect,
        on_beacon=recorder.on_beacon,
    )


@pytest.fixture
def link(cdb: CDB) -> Iterator[FakeBleak]:
    """The fake proxy; the teardown fails on a PDU the nodes dropped as a replay (a reused sequence number),
    unless the test expects some (`expect_replays`)."""
    fake = FakeBleak(cdb)
    yield fake
    assert fake.expect_replays or not fake.replayed, (
        f"the client replayed (src, IV index, seq) {fake.replayed[:5]}"
    )


@pytest.fixture
async def attached(proxy: ProxyClient, link: FakeBleak) -> ProxyClient:
    await proxy.attach(link)
    assert proxy.proxy_addr == PROXY_NODE
    return proxy
