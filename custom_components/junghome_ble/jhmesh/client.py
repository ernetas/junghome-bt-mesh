"""GATT Proxy client for a Bluetooth Mesh network, transport-agnostic.

The `ProxyClient` does everything above the GATT link: proxy protocol, beacons, network/transport
crypto, segmentation, request/response matching. It is handed an already-connected bleak-style
client via `attach()` — by `standalone.py` (plain bleak on a local adapter) or by the Home Assistant
integration (HA's Bluetooth stack, which may be an ESPHome proxy).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any

from .config_messages import (
    CONFIG_KEY_REFRESH_PHASE_SET,
    CONFIG_KEY_REFRESH_PHASE_STATUS,
    CONFIG_NAMES,
    CONFIG_NETKEY_STATUS,
    CONFIG_NETKEY_UPDATE,
)
from .crypto import NetKeyMaterial
from .keyrefresh import KeyRefreshFollower, Moved, describe_proof
from .messages import describe, set_shown_by
from .pdu import (
    BEACON_PRIVATE,
    FILTER_BLACKLIST,
    NONCE_APP,
    NONCE_DEVICE,
    PROXY_BEACON,
    PROXY_CONFIG,
    PROXY_NETWORK_PDU,
    NetworkPDU,
    ProxyReassembler,
    SecureNetworkBeacon,
    SegmentInfo,
    decode_opcode,
    is_unicast,
    lower_segments_access,
    lower_unsegmented_access,
    network_decrypt,
    network_encrypt,
    parse_beacon,
    parse_lower,
    parse_private_beacon,
    proxy_config_set_filter,
    proxy_frame,
    secure_network_beacon,
    segment_ack,
    seq_auth_from,
    upper_decrypt,
    upper_encrypt,
)

# `LocalState`, its exceptions and constants moved to `state`; re-exported below. `_check_range` keeps
# an explicit alias instead: it is private, so not in `__all__`, and the integration checks its stored records with it.
from .state import (
    IV_ABANDONED_ABORTED,
    IV_ABANDONED_NOT_TAKEN,
    IV_INDEX_MAX,
    IV_ORIGIN_BEACON,
    IV_ORIGIN_LOCAL,
    IV_RECOVERY_MIN_INTERVAL,
    IV_UPDATE_MAX_STATE,
    IV_UPDATE_MIN_STATE,
    SEQ_GUARD_FIRST_BEACON,
    SEQ_MAX,
    SEQ_TX_LIMIT,
    IVUpdateRefused,
    LocalState,
    SequenceExhausted,
    SequenceStalled,
    StateInUse,
)
from .state import (
    _check_range as _check_range,  # noqa: PLC0414  # the alias is the re-export
)
from .stats import LinkStats

if TYPE_CHECKING:
    from .cdb import CDB, Node

__all__ = [
    "CLASSIFY_CACHE_SIZE",
    "FILTER_ACK_TIMEOUT",
    "FILTER_RESEND_TRIES",
    "FILTER_RESEND_WAIT",
    "FILTER_SET_TRIES",
    "FOREIGN_SOURCE_REPORT_INTERVAL",
    "GATT_TIMEOUT",
    "HEARTBEAT_OPCODE",
    "IV_BEACON_INTERVAL",
    "IV_BEACON_INTERVAL_MAX",
    "MESH_PROXY_DATA_IN",
    "MESH_PROXY_DATA_OUT",
    "MESH_PROXY_SERVICE",
    "NET_KEY_INDEX",
    "SAR_ACK_DELAY_INCREMENT",
    "SAR_ACK_RETRANSMISSIONS",
    "SAR_DISCARD_TIMEOUT",
    "SAR_SEGMENTS_THRESHOLD",
    "SAR_SEGMENT_INTERVAL",
    "SEGMENT_ACK_TIMEOUT",
    "SEGMENT_RESTARTS",
    "SEGMENT_RETRIES",
    "SEGMENT_SIZE",
    "AccessMessage",
    "Heartbeat",
    "ProxyCandidate",
    "ProxyClient",
    "classify_proxy_advert",
]
# Compatibility re-exports: defined in `state` since `LocalState` moved there; the integration's `HAState`, the CLI,
# scripts and tests import them from here. Plain names in `__all__` rather than `X as X` aliases, which ruff's PLC0414
# rejects; mypy treats both as an explicit re-export.
__all__ += [
    "IV_ABANDONED_ABORTED",
    "IV_ABANDONED_NOT_TAKEN",
    "IV_INDEX_MAX",
    "IV_ORIGIN_BEACON",
    "IV_ORIGIN_LOCAL",
    "IV_RECOVERY_MIN_INTERVAL",
    "IV_UPDATE_MAX_STATE",
    "IV_UPDATE_MIN_STATE",
    "SEQ_GUARD_FIRST_BEACON",
    "SEQ_MAX",
    "SEQ_TX_LIMIT",
    "IVUpdateRefused",
    "LocalState",
    "SequenceExhausted",
    "SequenceStalled",
    "StateInUse",
]

log = logging.getLogger("jhmesh")
# per-PDU lines — what was sent and received, what was dropped and why — go to a child logger: traffic
# can be logged at DEBUG alone, or left out of a `jhmesh` DEBUG log; link lifecycle and anomalies stay on `jhmesh`
trace = logging.getLogger("jhmesh.trace")

MESH_PROXY_SERVICE = "00001828-0000-1000-8000-00805f9b34fb"
MESH_PROXY_DATA_IN = "00002add-0000-1000-8000-00805f9b34fb"
MESH_PROXY_DATA_OUT = "00002ade-0000-1000-8000-00805f9b34fb"

SEGMENT_SIZE = 12
NET_KEY_INDEX = (
    0  # the primary NetKey, the only one JUNG networks use (`cdb.net_keys[0]`)
)
SEGMENT_ACK_TIMEOUT = 1.5
SEGMENT_RETRIES = 4
SEGMENT_RESTARTS = (
    2  # how often a segmented message starts over because the IV index moved mid-way
)
FILTER_RESEND_TRIES = 10  # a subclass may hold reserve_seq() back until its store catches up (HAState); retry, not give up
FILTER_RESEND_WAIT = 1.0  # between those retries
FILTER_ACK_TIMEOUT = (
    2.0  # the proxy answers Set Filter Type with a Filter Status within milliseconds
)
FILTER_SET_TRIES = 3  # Set Filter Type requests per link without a Filter Status before giving up on it
GATT_TIMEOUT = 5.0  # one GATT write (a frame, without response), subscription, unsubscription or disconnect; normally ms
# a PDU from our own address with a number we never handed out is reported at most this often per link (seconds):
# another client on the address sends steadily, and one report (with the highest number seen) is the news
FOREIGN_SOURCE_REPORT_INTERVAL = 60.0
# The SAR Receiver state (Mesh Protocol 1.1 §4.2.49) at the spec's defaults — a proxy client has no Configuration
# Server to set it: acknowledgment delay increment 2.5 (state 0b001, §4.2.49.2), one transmission of each Segment
# Acknowledgment (retransmissions count 0b00, §4.2.49.3) for messages of more than 3 segments (§4.2.49.1), a
# reassembly discarded 10 s after its last new segment (0b0001, §4.2.49.4), a segment reception interval of 60 ms
# (0b0101, §4.2.49.5). `ProxyClient._on_segment` applies them (§3.5.3.4)
SAR_ACK_DELAY_INCREMENT = 2.5
SAR_ACK_RETRANSMISSIONS = 0
SAR_SEGMENTS_THRESHOLD = 3
SAR_DISCARD_TIMEOUT = 10.0
SAR_SEGMENT_INTERVAL = 0.060  # seconds
# the Beacon Interval (§3.10.3.1) of an IV Update this client started (`ProxyClient._run_iv_update`): 10 s, the
# shortest, which the spec's formula gives a node that observes no other beacons (a proxy client hears only its
# proxy's), until the mesh took the update; then 600 s, the longest
IV_BEACON_INTERVAL = 10.0
IV_BEACON_INTERVAL_MAX = 600.0
# proxy service data classified (`ProxyClient.classify_service_data`) kept by its bytes, least recently seen dropped
# first: a Node Identity costs one AES per node per key, and a proxy repeats the same advert for its whole session
CLASSIFY_CACHE_SIZE = 256


@dataclass
class ProxyCandidate:
    """A proxy node seen advertising, as reported by a scan."""

    address: str  # BLE address / CoreBluetooth UUID
    rssi: int
    kind: str  # 'network-id' | 'node-identity' | 'private-network-id' | 'private-node-identity'
    node_addr: int | None = None
    name: str | None = None
    device: Any = None  # backend BLEDevice, if available
    adv: Any = (
        None  # backend AdvertisementData (local name, manufacturer data), if available
    )


def classify_proxy_advert(
    sd: bytes, keys: Sequence[NetKeyMaterial], nodes: Iterable[int]
) -> tuple[str, int | None] | None:
    """Whose Mesh Proxy service data (0x1828) `sd` is: (kind, node address) under one of `keys`, or None.

    Identification types (§7.2.2.2): 0x00 Network ID (`network-id`, in the clear), 0x01 Node Identity
    (`node-identity`, a hash of one of the unicast `nodes`); Mesh Protocol 1.1 Proxy Privacy adds 0x02 Private Network
    Identity (`private-network-id`) and 0x03 Private Node Identity (`private-node-identity`), Hash ‖ Random with
    nothing in the clear — those two are unverified on air. A Node Identity costs one AES per node per key.
    """
    if not sd:
        return None
    if sd[0] == 0x00:
        ours = any(sd[1:9] == k.network_id for k in keys)
        return ("network-id", None) if ours else None
    if len(sd) < 17 or sd[0] not in (0x01, 0x02, 0x03):
        return None
    h, rnd = sd[1:9], sd[9:17]
    if sd[0] == 0x02:
        ours = any(k.private_network_identity(rnd) == h for k in keys)
        return ("private-network-id", None) if ours else None
    private = sd[0] == 0x03
    for a in nodes:
        for k in keys:
            mine = (
                k.private_node_identity(rnd, a)
                if private
                else k.node_identity_hash(rnd, a)
            )
            if mine == h:
                return ("private-node-identity" if private else "node-identity"), a
    return None


def _now() -> float:
    """Return the monotonic clock, looked up at call time (a frozen clock in tests then stamps messages consistently)."""
    return time.monotonic()


@dataclass
class AccessMessage:
    """A received access message with its network-layer context.

    `repr()` leaves the payload out: a device-key message the sniffer decrypted may be a Config AppKey Add or
    NetKey Update carrying a mesh key in the clear, and a stray `%r` / debugger print must not leak it. `str()`
    shows the redacting `describe` of the payload instead.
    """

    src: int
    dst: int
    ttl: int
    seq: int
    opcode: int
    company_id: int | None
    params: bytes = field(repr=False)
    access_pdu: bytes = field(repr=False)
    key: str  # 'app0' | 'dev:<addr>'
    received: float = field(default_factory=_now)

    def __str__(self) -> str:
        """Format the message for logs: route, TTL, sequence number, key and a decoded description.

        A device-key message may be a key refresh carrying new keys: `describe` shows nothing undecoded of it.
        """
        return (
            f"{self.src:04X}→{self.dst:04X} ttl={self.ttl} seq={self.seq:06X} [{self.key}]"
            f" {describe(self.access_pdu, devkey=self.key.startswith('dev:'))}"
        )


def _log_orphaned_write(task: asyncio.Task[None]) -> None:
    """Retrieve what a write whose caller was cancelled raised (`ProxyClient._write`), so nothing is left unread."""
    if not task.cancelled() and (err := task.exception()) is not None:
        log.debug("proxy write finished after its caller left: %s", err)


class _Lazy:
    """A log argument rendered only when the record is (`describe` ran for every send at any level)."""

    __slots__ = ("render",)

    def __init__(self, render: Callable[[], str]) -> None:
        self.render = render

    def __str__(self) -> str:
        return self.render()


@dataclass(frozen=True)
class Heartbeat:
    """A Heartbeat transport control message (§3.6.7): a node saying "alive" to the address it was configured with.

    `init_ttl` is the TTL it left the node with, `ttl` the TTL it arrived with, so `hops` is the relay count on
    the way to our proxy; `features` are the node's active features (bit 0 relay, 1 proxy, 2 friend, 3 low power).
    """

    src: int
    dst: int
    init_ttl: int
    ttl: int
    features: int
    received: float = field(default_factory=_now)

    @property
    def hops(self) -> int:
        """Relays on the way to us, the proxy's own forwarding to its client included: InitTTL - TTL.

        A proxy relaying to its GATT client decrements the TTL (§3.4.6.3), so 0 is the proxy node itself and a node
        the proxy hears directly reads 1. The spec's Hops (InitTTL - RxTTL + 1) is one more.
        """
        return max(self.init_ttl - self.ttl, 0)


HEARTBEAT_OPCODE = 0x0A


def _retrieve(fut: asyncio.Future[Any]) -> None:
    """Mark a waiter's failed future as read once its request is over.

    A link loss fails every waiter's future (`_release_link`) while the send itself may raise its own
    `ConnectionError` first; nobody then reads the future's, and asyncio reports "Future exception was never
    retrieved" at ERROR when it is collected.
    """
    if fut.done() and not fut.cancelled():
        fut.exception()


@dataclass
class _AckState:
    block: int = 0
    cancelled: bool = False  # BlockAck 0: the receiver cannot take the message (§3.5.3.3), stop sending it
    event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class _TxKey:
    """The upper-transport key of an outgoing message: AppKey 0, or the device key of the node addressed.

    Everything else on the send path (sequence numbers, IV index, segmentation, acks) is the same for both;
    only the nonce type and the AKF/AID bits of the lower transport header differ (§3.5.2, §3.8.5).
    """

    key: bytes = field(repr=False)  # key material never in logs or error text
    nonce: int  # NONCE_APP | NONCE_DEVICE
    akf: bool
    aid: int
    label: str  # 'app0' | 'dev:<addr>', as in AccessMessage.key
    # the NetKey the network layer is sealed with; None = the one we transmit with when it is written (`nk`)
    net: NetKeyMaterial | None = field(default=None, repr=False)

    @property
    def devkey(self) -> bool:
        """Whether this is a device key (the message may carry key material: `describe` hides what it cannot decode)."""
        return self.nonce == NONCE_DEVICE


class ProxyClient:
    """The mesh client above a GATT proxy link: filters, beacons, crypto, segmentation, request/response."""

    def __init__(
        self,
        cdb: CDB,
        state: LocalState,
        ttl: int = 5,
        on_message: Callable[[AccessMessage], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        on_beacon: Callable[[SecureNetworkBeacon], None] | None = None,
        on_filter_status: Callable[[int], None] | None = None,
        on_undecryptable: Callable[[], None] | None = None,
        on_heartbeat: Callable[[Heartbeat], None] | None = None,
        on_key_refresh: Callable[[int, NetKeyMaterial], None] | None = None,
        on_foreign_own_source: Callable[[int, int], None] | None = None,
        on_iv_update_abandoned: Callable[[], None] | None = None,
        on_control: Callable[[int, int], None] | None = None,
    ) -> None:
        """Set up for the first NetKey/AppKey of `cdb`; nothing is connected until `attach()`.

        Callbacks (all optional, all called from the notification handler): `on_message` for every decoded access
        message, `on_disconnect` when the link is lost, `on_beacon` for every Secure Network Beacon (authenticated
        or not), `on_filter_status(proxy_unicast)` when the proxy's Filter Status reveals which node we are
        connected to (`proxy_addr`), `on_undecryptable` for every network PDU our keys cannot open
        (`rx_undecryptable` counts them per link — the signature of an export whose keys are stale), and
        `on_heartbeat` for every Heartbeat control message the proxy forwards (`Heartbeat`), `on_key_refresh(phase,
        new_key)` when the mesh's key refresh moves (`_follow_key_refresh`; phase 0 = completed, the new key is the
        only one now), `on_foreign_own_source(iv_index, seq)` when a PDU from our own address carries a number we
        never handed out — another client uses the address (`_on_own_source`; at most once per
        `FOREIGN_SOURCE_REPORT_INTERVAL` per link, with the highest number seen so far), `on_iv_update_abandoned` when
        an IV Update this client started is given up because the mesh did not take it (`_run_iv_update`),
        `on_control(src, opcode)` for every control PDU our keys authenticated that is no replay (a Segment Ack, a
        Heartbeat, a Friend message: traffic a link watchdog may count, before `on_heartbeat`).
        """
        self.cdb = cdb
        self.state = state
        self.ttl = ttl
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self.on_beacon = on_beacon
        self.on_filter_status = on_filter_status
        self.on_undecryptable = on_undecryptable
        self.on_heartbeat = on_heartbeat
        self.on_key_refresh = on_key_refresh
        self.on_foreign_own_source = on_foreign_own_source
        self.on_iv_update_abandoned = on_iv_update_abandoned
        self.on_control = on_control
        # the NetKeys derived so far (`_net_key`), pruned to those we accept whenever the key refresh moves
        self._net_keys: dict[bytes, NetKeyMaterial] = {}
        self._kr = self._resume_key_refresh(cdb, state)
        # `classify_service_data`'s verdicts by service data, for the keys it was given then (`_kr.rx_keys`)
        self._classified: OrderedDict[bytes, tuple[str, int | None] | None] = (
            OrderedDict()
        )
        self._classified_for: tuple[bytes, ...] = ()
        self.ak = cdb.app_keys[0]
        self.client: Any = None
        self.proxy_addr: int | None = (
            None  # unicast of the proxy node, learnt from its Filter Status
        )
        self.connected_at: float | None = None
        self.last_rx = 0.0  # monotonic time the proxy last delivered anything (`StandaloneLink` silence watchdog)
        # the current link's counts (`link_stats`, started over by every attach) and the sum of the links before it
        self._stats = LinkStats()
        self._past_stats = LinkStats()
        # on the current link: Set Filter Type requests actually written — none means the proxy was never
        # asked, so a missing Filter Status says nothing about it discarding our PDUs (the HA hub's watchdog)
        self.filter_writes = 0
        # on the current link: the highest (IV index, SEQ) from our own address we never handed out — proof that
        # another client sends from it (`_on_own_source`) — and when it was last reported (monotonic)
        self.foreign_own_source: tuple[int, int] | None = None
        self._foreign_reported_at: float | None = None
        # on the current link: the (IV index, SEQ) of the last proxy configuration PDU taken
        self._proxy_config_last: tuple[int, int] | None = None
        self._reasm = ProxyReassembler()
        self._segments: dict[tuple[int, int], dict[str, Any]] = {}
        # source -> (IV index, SeqAuth) of its last segmented message delivered: a retransmission of it (fresh
        # sequence numbers, the sender missed our ack) after its reassembly expired is not delivered twice
        self._seq_auth_done: dict[int, tuple[int, int]] = {}
        # (match, future, shows): `shows` tests whether a status shows an acknowledged Set's requested state
        # (`set_shown_by`), None for any other request (`_deliver`)
        self._waiters: list[
            tuple[
                Callable[[AccessMessage], bool],
                asyncio.Future[AccessMessage],
                Callable[[bytes], bool] | None,
            ]
        ] = []
        self._ack_waiters: dict[tuple[int, int], _AckState] = {}
        self._send_lock = asyncio.Lock()
        self._sar_locks: dict[
            int, asyncio.Lock
        ] = {}  # one segmented message per destination (`_sar_lock`)
        self._write_lock = (
            asyncio.Lock()
        )  # one proxy PDU's SAR frames must not interleave with another's
        self._tasks: set[asyncio.Task[None]] = (
            set()
        )  # segment acks / filter re-sends in flight
        self._filter_type: int | None = (
            None  # proxy filter we asked for on this link, once it was sent
        )
        # the notification handler subscribed on the current link (`attach`); a released or replaced client's
        # handler finds it changed and drops what it still delivers
        self._link_notify: Callable[[Any, bytearray], None] | None = None
        self._beacon_seen = asyncio.Event()
        self._filter_acked = (
            asyncio.Event()
        )  # a Filter Status arrived since the last Set Filter Type
        self._filter_task: asyncio.Task[None] | None = (
            None  # the one filter (re-)send running in the background
        )
        # the beacons and the return to Normal Operation of an IV Update this client started (`_run_iv_update`)
        self._iv_task: asyncio.Task[None] | None = None
        self._ready = (
            asyncio.Event()
        )  # set once attach() is through (notifications, proxy filter)
        self._dev_key_of_element = {
            e.address: n.dev_key for n in cdb.nodes for e in n.elements
        }
        # the JUNG devices (a product id; the phones in `nodes[]` have none): their primary elements are what a
        # provisioner configures and what answers, their elements never send a provisioner's request (`_follow_key_refresh`)
        self._device_primaries: set[int] = set()
        self._device_elements: set[int] = set()
        for n in cdb.nodes:
            self._note_device(n)

    def _note_device(self, node: Node) -> None:
        if node.pid is not None:
            self._device_primaries.add(node.unicast)
            self._device_elements.update(e.address for e in node.elements)

    def _resume_key_refresh(self, cdb: CDB, state: LocalState) -> KeyRefreshFollower:
        """Take up a key refresh in progress: the one the export was written in, then the one `state` followed.

        A stored Phase 2 or 3 without its proof is a candidate again (`KeyRefreshFollower.resume`).
        """
        current = cdb.net_keys[NET_KEY_INDEX]
        self._net_keys[current.key] = current
        exported: tuple[bytes, int] | None = None
        if (refresh := cdb.net_key_refresh.get(NET_KEY_INDEX)) is not None:
            # the export was written mid key refresh: its `key` is the new one, the network may still use the old
            old, phase = refresh
            self._net_keys[old.key] = old
            exported, current = (current.key, phase), old
        follower, keep = KeyRefreshFollower.resume(
            current.key, exported, state.key_refresh
        )
        if not keep:
            state.set_key_refresh(None)
        return follower

    def _net_key(self, key: bytes) -> NetKeyMaterial:
        """Return the derived NetKey material of `key` (derived once)."""
        if (material := self._net_keys.get(key)) is None:
            material = self._net_keys[key] = NetKeyMaterial.derive(key)
        return material

    @property
    def key_refresh_phase(self) -> int:
        """0 = normal operation, 1 = a new NetKey accepted, 2 = transmitting with the proven new key."""
        return self._kr.phase

    @property
    def nk(self) -> NetKeyMaterial:
        """The NetKey we transmit with: the new one from a proven key refresh Phase 2 on, the export's before (§3.10.4.1)."""
        return self._net_key(self._kr.tx_key)

    @property
    def rx_net_keys(self) -> tuple[NetKeyMaterial, ...]:
        """The NetKeys we accept: the export's (its old one mid key refresh), and during a refresh the new ones too."""
        return tuple(self._net_key(k) for k in self._kr.rx_keys)

    @property
    def key_refresh_target(self) -> tuple[int, bytes] | None:
        """The phase (1, 2 or 3) of the followed key refresh that is proven, with its new key; None when none is.

        What Home Assistant may take the nodes only it knows to (`KeyRefreshFollower.distribution`):
        never a key one node's word alone gave.
        """
        return self._kr.distribution

    def add_node(self, node: Node) -> None:
        """Know a node the export does not have yet (one just provisioned): its device key, its elements."""
        if node not in self.cdb.nodes:
            self.cdb.nodes.append(node)
        self.cdb.reindex()
        self._classified.clear()  # its Node Identity was nobody's until now
        for element in node.elements:
            self._dev_key_of_element[element.address] = node.dev_key
        self._note_device(node)

    def remove_node(self, node: Node) -> None:
        """Forget a node `add_node` made known (one reset before any export recorded it); the others stay as they are."""
        self.cdb.nodes[:] = [n for n in self.cdb.nodes if n is not node]
        self.cdb.reindex()
        self._classified.clear()
        for element in node.elements:
            self._dev_key_of_element.pop(element.address, None)
            if (owner := self.cdb.node_by_addr(element.address)) is not None:
                self._dev_key_of_element[element.address] = owner.dev_key

    def forget_sources(self, addresses: Iterable[int]) -> None:
        """Forget what the replay protection holds for `addresses`: a node newly placed there starts at SEQ 0.

        Only for addresses the caller has just handed to a new node (provisioning): the entries are those of a
        node that sent from them before — reset since — and would drop every message of the new one until its
        sequence numbers passed the old one's (§3.8.8). The shorter replay list is persisted (`LocalState.persist`).
        """
        gone = set(addresses)
        for src in gone:
            self._seq_auth_done.pop(src, None)
        if gone & self.state.rpl.keys():
            self.state.rpl = {s: e for s, e in self.state.rpl.items() if s not in gone}
            self.state.persist()

    @property
    def beacon_seen(self) -> bool:
        """Whether an authenticated Secure Network Beacon arrived on the current link (cleared by every attach)."""
        return self._beacon_seen.is_set()

    def __repr__(self) -> str:
        """Link summary only — never the keys (`repr()` of a client may end up in a traceback or a diagnostics dump)."""
        proxy = f"{self.proxy_addr:04X}" if self.proxy_addr is not None else None
        return (
            f"ProxyClient(src={self.state.src:04X}, proxy={proxy}, connected={self.connected},"
            f" nid={self.nk.nid}, aid={self.ak.aid})"
        )

    @property
    def link_stats(self) -> LinkStats:
        """What the current link (the last one, while none is up) carried and dropped: a copy, `LinkStats`.

        Started over by every `attach`: what the last link saw says nothing about this one.
        """
        return replace(self._stats, garbage=self._reasm.dropped)

    @property
    def total_stats(self) -> LinkStats:
        """What every link since this client was made carried and dropped, the current one included: a copy."""
        return self._past_stats + self.link_stats

    @property
    def rx_undecryptable(self) -> int:
        """Since attach(): network PDUs that failed NID / MIC / upper-transport decryption (`LinkStats.undecryptable`)."""
        return self._stats.undecryptable

    @property
    def rx_garbage(self) -> int:
        """Since attach(): proxy PDUs dropped before authentication because they outgrew `PROXY_PDU_MAX`.

        A proxy that streams oversize SAR continuations is either broken or hostile (it only needs the public
        Network ID to be connected to); the application can read this next to `rx_undecryptable`.
        """
        return self._reasm.dropped

    @property
    def rx_proxy_config_dropped(self) -> int:
        """Since attach(): proxy configuration PDUs dropped (not CTL=1 / DST=0, undecryptable, or a replay of one taken)."""
        return self._stats.proxy_config_dropped

    # ------------------------------------------------------------------ discovery helpers
    def classify_service_data(self, sd: bytes) -> tuple[str, int | None] | None:
        """Interpret Mesh Proxy service data (0x1828). Returns (kind, node_addr) if it belongs to our network.

        The verdict is kept by the bytes (`CLASSIFY_CACHE_SIZE`): every advert of every proxy in
        range comes here, a Node Identity costs one AES per node per key, and another network's — the one no node
        matches, so the whole scan — repeats for as long as its proxy advertises. The kept verdicts are for the
        keys accepted when they were made: a key refresh moving on (`_kr.rx_keys`) drops them all, and so does a
        node made known or forgotten (`add_node`, `remove_node`).
        """
        if not sd:
            return None
        keys = self._kr.rx_keys
        if keys != self._classified_for:
            self._classified.clear()
            self._classified_for = keys
        sd = bytes(sd)
        if sd in self._classified:
            self._classified.move_to_end(sd)
            return self._classified[sd]
        verdict = self._classify(sd)
        self._classified[sd] = verdict
        if len(self._classified) > CLASSIFY_CACHE_SIZE:
            self._classified.popitem(last=False)
        return verdict

    def _classify(self, sd: bytes) -> tuple[str, int | None] | None:
        """`classify_service_data` without the cache."""
        # during a key refresh, proxies advertise either key's identity
        return classify_proxy_advert(
            sd, self.rx_net_keys, (n.unicast for n in self.cdb.nodes)
        )

    # ------------------------------------------------------------------ link management
    @property
    def connected(self) -> bool:
        """Whether a client is attached and (if it reports it) still connected."""
        return self.client is not None and bool(
            getattr(self.client, "is_connected", True)
        )

    @property
    def ready(self) -> bool:
        """Whether `attach()` is through: connected, notifications subscribed and the proxy filter requested.

        `connected` turns True as soon as `attach()` has the client (the HA hub relies on that meaning); a
        request sent before `ready` can miss its reply, or group statuses the default filter still drops.
        """
        return self._ready.is_set() and self.connected

    @property
    def mtu(self) -> int:
        """ATT MTU of the current link, re-read on every use.

        BlueZ still reports 23 right after connecting and only learns the negotiated value (247 with JUNG nodes)
        a little later.
        """
        return (
            (getattr(self.client, "mtu_size", None) or 23)
            if self.client is not None
            else 23
        )

    async def attach(
        self, client: Any, filter_blacklist: bool = True, beacon_wait: float = 0.0
    ) -> None:
        """Take over an already-connected bleak-style client (has write_gatt_char/start_notify/mtu_size).

        With `beacon_wait` > 0 the first filter request waits up to that long for the proxy's Secure Network Beacon
        (sent right after the subscription) so it is encrypted with the network's current IV index rather than the
        stored one. Whatever fails after the hand-over releases the client again — a half-attached connection would
        otherwise hold one of the (few) connection slots of the adapter or ESPHome proxy for good.

        A client still attached (a transport that reported `is_connected` False without its disconnected callback)
        is released first — its waiters fail, its tasks stop, it is disconnected — so nothing of the old link is
        carried over to the new one.
        """
        if self.client is not None:
            log.debug(
                "attach over a still-attached client: releasing %s",
                getattr(self.client, "address", "?"),
            )
            old = self._release_link("re-attached")
            if old is not client:
                await self._disconnect(old)
            else:
                await self._stop_notify(old)  # subscribed again below
        self._ready.clear()  # a previous attach whose link dropped mid-way may have left it set
        self.client = client
        self._filter_type = None
        self._beacon_seen.clear()
        self._filter_acked.clear()
        self.proxy_addr = (
            None  # the previous link's node until this proxy's Filter Status arrives
        )
        self._reset_link_counters()
        self.connected_at = self.last_rx = (
            time.monotonic()
        )  # a fresh link counts as heard from

        def on_notify(char: Any, data: bytearray) -> None:
            # a client we released (or re-attached, whose old subscription a transport may keep) can still
            # deliver: its PDUs must not set the proxy address, advance the replay list or feed our reassembly
            if self._link_notify is on_notify:
                self._on_notify(char, data)

        self._link_notify = on_notify
        try:
            # bounded like every GATT call: a subscription that never completes would hold the
            # caller's connection loop — and the connection slot — for good
            await asyncio.wait_for(
                client.start_notify(MESH_PROXY_DATA_OUT, on_notify), GATT_TIMEOUT
            )
            log.info(
                "attached to proxy %s, MTU %d",
                getattr(client, "address", "?"),
                self.mtu,
            )
            if filter_blacklist:
                if beacon_wait > 0:
                    await self._wait_for_beacon(beacon_wait)
                try:
                    await self.set_filter(FILTER_BLACKLIST)
                except SequenceStalled as err:
                    # back-pressure (HAState: the store's last save has not landed — a connect-time beacon that
                    # moved the IV index forces one), not exhaustion: without a filter the proxy stays on its
                    # default whitelist and every group publication is lost for the whole link
                    log.info("proxy filter held back (%s): sending it shortly", err)
                    self._filter_type = FILTER_BLACKLIST
                    self._start_filter_task(
                        FILTER_BLACKLIST, "sequence numbers were held back"
                    )
                except SequenceExhausted:
                    # stay attached, receive-only: the docstring promises recovery once a beacon moves the
                    # transmit index and resets SEQ, but that beacon is never delivered if attach() detaches
                    # first. `_filter_type` is only a hint for `_on_notify`'s re-send — nothing else may read
                    # it as "the filter is actually in place".
                    log.warning(
                        "sequence space exhausted: staying attached to learn the IV index from the proxy's beacon"
                    )
                    self._filter_type = FILTER_BLACKLIST
                else:
                    # a beacon that moved the IV index during the write has a re-send running already
                    running = (
                        self._filter_task is not None and not self._filter_task.done()
                    )
                    if not self._filter_acked.is_set() and not running:
                        self._start_filter_task(FILTER_BLACKLIST, None, sent=1)
            if self.client is client:  # the link may have dropped during the attach
                self._ready.set()
                self._start_iv_task(sent=False)
        except BaseException:
            log.debug(
                "attach to %s failed, releasing the connection",
                getattr(client, "address", "?"),
            )
            await self.detach()
            raise

    def _reset_link_counters(self) -> None:
        """Start the per-link counts and marks over (`attach`): what the last link saw says nothing about this one.

        The last link's counts go into the totals first (`total_stats`), and its reassembly (its `garbage`) with them.
        """
        self._past_stats = self.total_stats
        self._stats = LinkStats()
        self._reasm = ProxyReassembler()
        self._proxy_config_last = None
        self.foreign_own_source = self._foreign_reported_at = None

    async def _wait_for_beacon(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._beacon_seen.wait(), timeout)
        except asyncio.TimeoutError:
            log.debug(
                "no beacon within %.1fs, sending the proxy filter with the stored IV index",
                timeout,
            )

    async def detach(self, disconnect: bool = True) -> None:
        """Drop the attached client (disconnecting it unless told not to) and cancel background work.

        Pending requests and segment acknowledgements fail with `ConnectionError`, exactly as on a lost link. A
        client kept connected is unsubscribed from Mesh Proxy Data Out (best effort; whatever it still delivers
        is dropped either way).
        """
        client = self._release_link("proxy detached")
        if client is None:
            return
        if not disconnect:
            await self._stop_notify(client)
            return
        await self._disconnect(client)

    @staticmethod
    async def _disconnect(client: Any) -> None:
        """Disconnect `client`, best effort and bounded by `GATT_TIMEOUT`.

        A transport whose disconnect never returns (a stuck BlueZ or ESPHome proxy call) must not hold up the
        caller: the link is released already, and the transport's own timeout ends the connection eventually.
        Unverified on air.
        """
        try:
            await asyncio.wait_for(client.disconnect(), GATT_TIMEOUT)
        except TimeoutError:
            log.warning(
                "disconnecting %s did not complete within %gs; leaving it",
                getattr(client, "address", "?"),
                GATT_TIMEOUT,
            )
        except Exception:
            log.debug("disconnect failed", exc_info=True)

    @staticmethod
    async def _stop_notify(client: Any) -> None:
        """Unsubscribe `client` from Mesh Proxy Data Out, best effort and bounded by `GATT_TIMEOUT`."""
        try:
            await asyncio.wait_for(
                client.stop_notify(MESH_PROXY_DATA_OUT), GATT_TIMEOUT
            )
        except Exception:
            log.debug("stop_notify failed", exc_info=True)

    def _release_link(self, reason: str) -> Any:
        """Forget the current client and fail everything waiting on it; return the client for the caller to close."""
        client, self.client = self.client, None
        self._link_notify = None
        self._ready.clear()
        self.connected_at = None
        self.proxy_addr = None
        self._filter_type = None
        self.filter_writes = 0  # the next link starts with none
        self._cancel_tasks()
        for _, fut, _ in self._waiters:
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        self._waiters.clear()
        for acked in self._ack_waiters.values():
            acked.event.set()  # wake the sender: its next write fails with "not connected"
        self._ack_waiters.clear()
        self.state.flush()  # replay-list entries a receive-only link collected
        return client

    def handle_disconnected(self, client: Any = None) -> None:
        """Call from the transport's disconnected callback.

        Pass the client it fired for when the transport provides it: a late callback from a client we already
        gave up on must not tear down the current link.
        """
        if client is not None and client is not self.client:
            log.debug(
                "ignoring disconnect of a client that is no longer ours (%s)",
                getattr(client, "address", "?"),
            )
            return
        log.warning("proxy link lost")
        self._release_link("proxy disconnected")
        if self.on_disconnect:
            self.on_disconnect()

    async def set_filter(self, filter_type: int) -> None:
        """Proxy Configuration 'Set Filter Type' (§6.5.1). Blacklist + empty list = receive everything.

        The proxy's connect-time beacon often lands while the first filter write is in flight (a GATT write
        yields to the loop); if it moved the IV index meanwhile the request just written was encrypted with a
        stale one and silently dropped, so it is sent again with the current index before returning.

        Reserved and written under `_send_lock` like any send: a segmented round writes the numbers it reserved
        one segment at a time, and a request that took its number meanwhile would reach the air between them.
        Without a link it fails before taking a number (`_refuse_unlinked`).
        """
        while True:
            async with self._send_lock:
                self._refuse_unlinked()
                iv = self.state.tx_iv_index
                pdu = network_encrypt(
                    self.nk,
                    iv,
                    ctl=True,
                    ttl=0,
                    seq=self.state.next_seq(),
                    src=self.state.src,
                    dst=0x0000,
                    transport_pdu=proxy_config_set_filter(filter_type),
                    proxy=True,
                )
                self._filter_acked.clear()
                await self._write(PROXY_CONFIG, pdu)
                self.filter_writes += 1
            self._filter_type = filter_type
            if self.state.tx_iv_index == iv:
                break
            log.info("IV index changed during the proxy filter write, sending it again")
        await asyncio.sleep(0.3)

    def _start_filter_task(
        self, filter_type: int, reason: str | None, sent: int = 0
    ) -> None:
        """Run `_resend_filter` in the background, replacing one still running (one filter conversation per link)."""
        if self._filter_task is not None and not self._filter_task.done():
            self._filter_task.cancel()
        task = asyncio.get_running_loop().create_task(
            self._resend_filter(filter_type, reason, sent)
        )
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        self._filter_task = task

    async def _resend_filter(
        self,
        filter_type: int,
        reason: str | None = "IV index changed",
        sent: int = 0,
    ) -> None:
        """Make sure the proxy took the filter: send it (again) until a Filter Status answers it.

        The proxy answers every Set Filter Type it accepts with a Filter Status (§6.6.3), so that status is the
        acknowledgement. A request encrypted with a stale IV index, or dropped over the air, leaves the proxy on
        its default whitelist (unicast-only reception — every group publication is lost), so it is sent up to
        `FILTER_SET_TRIES` times per call, `FILTER_ACK_TIMEOUT` apart. `sent` counts requests already written
        (attach's own), which are waited for first.

        A subclass whose `reserve_seq` holds numbers back until its store is durably written (`HAState`,
        `SequenceStalled`) refuses right after an IV change or at a link's first send: that refusal is retried
        every `FILTER_RESEND_WAIT` up to `FILTER_RESEND_TRIES` times rather than given up on at once.
        """
        if reason is not None:
            log.info("%s, re-sending the proxy filter", reason)
        stalls = 0
        while True:
            if sent:
                try:
                    await asyncio.wait_for(
                        self._filter_acked.wait(), FILTER_ACK_TIMEOUT
                    )
                    return
                except asyncio.TimeoutError:
                    if sent >= FILTER_SET_TRIES:
                        log.warning(
                            "the proxy answered none of %d proxy filter requests: it may be discarding our "
                            "messages (stale sequence number, or our address used by another client)",
                            sent,
                        )
                        return
                    log.info(
                        "no Filter Status from the proxy, sending the proxy filter again"
                    )
            try:
                await self.set_filter(filter_type)
            except SequenceStalled as err:
                stalls += 1
                if stalls >= FILTER_RESEND_TRIES:
                    log.warning("proxy filter re-send failed: %s", err)
                    return
                await asyncio.sleep(FILTER_RESEND_WAIT)
                continue
            except ConnectionError as err:
                log.log(
                    logging.WARNING
                    if isinstance(err, SequenceExhausted)
                    else logging.DEBUG,
                    "proxy filter re-send failed: %s",
                    err,
                )
                return
            sent += 1

    def _refuse_unlinked(self) -> None:
        """Fail a send that has no link before it takes a sequence number; called under `_send_lock`.

        `next_seq` / `reserve_seq` persist what they hand out, and `_write` only finds the missing link after
        that: every send while unlinked (a background reader, a keep-alive) used a number for nothing, and one
        after the application closed its store reopened it (a subclass's clean-close mark). The check sits under
        the lock, right before the reservation, so the order of reserving and writing stays as it was.
        """
        if self.client is None:
            raise ConnectionError("not connected to a proxy")

    async def _write(self, msg_type: int, payload: bytes) -> None:
        """Write one proxy PDU, all of its SAR frames on the one link that was current when the write lock came.

        Shielded from the caller's cancellation: a PDU cut off between its SAR frames leaves the
        proxy reassembling it, and it takes the next PDU's frames for the rest — both lost, the next one to a
        different message. A cancelled caller stops waiting; the frames still go out, and what their write
        raises then is only logged.
        """
        # the lock is taken here, in the caller's order, and handed to the write: the air must see our PDUs in
        # the order their sequence numbers were reserved
        await self._write_lock.acquire()
        if self.client is None or len(payload) + 1 <= self.mtu - 3:
            # no link (fails at once) or one frame: nothing to cut in half
            await self._write_frames(msg_type, payload)
            return
        task = asyncio.get_running_loop().create_task(
            self._write_frames(msg_type, payload)
        )
        task.add_done_callback(_log_orphaned_write)
        await asyncio.shield(task)

    async def _write_frames(self, msg_type: int, payload: bytes) -> None:
        """Write the SAR frames of one proxy PDU; entered holding the write lock (`_write`), which it releases."""
        try:
            try:
                client, stats = self.client, self._stats
                if client is None:
                    raise ConnectionError("not connected to a proxy")
                for frame in proxy_frame(msg_type, payload, self.mtu - 3):
                    if self.client is not client:
                        raise ConnectionError("proxy link changed during the write")
                    try:
                        await asyncio.wait_for(
                            client.write_gatt_char(
                                MESH_PROXY_DATA_IN, frame, response=False
                            ),
                            GATT_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        # a write without response that never completes (a stalled link, a transport whose
                        # buffers never drain): report it like a lost link rather than hold the locks forever
                        raise ConnectionError(
                            f"proxy write not completed within {GATT_TIMEOUT:g}s"
                        ) from None
                # counted for the link it was written on, should another be attached by now
                stats.tx += 1
            finally:
                self._write_lock.release()
        except ConnectionError:
            raise
        except Exception as err:  # transport-specific failures (bleak.BleakError, OSError, …) → one type for callers
            raise ConnectionError(f"proxy write failed: {err}") from err

    # ------------------------------------------------------------------ background tasks
    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Run a coroutine in the background, keeping a reference until it finishes; return its task.

        A bare create_task may be garbage collected mid-way; failures are logged, never raised into the
        notification callback. Every one is cancelled when the link goes (`_cancel_tasks`).
        """
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("background task failed", exc_info=task.exception())

    def _cancel_tasks(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    # ------------------------------------------------------------------ sending
    @property
    def _app_key(self) -> _TxKey:
        return _TxKey(self.ak.key, NONCE_APP, True, self.ak.aid, "app0")

    def _dev_key(self, node_unicast: int) -> _TxKey:
        """Device key of the node whose *primary* element is `node_unicast` (the Config Server lives there)."""
        node = self.cdb.node_by_addr(node_unicast)
        if node is None:
            raise ValueError(f"{node_unicast:04X} is not an element of any known node")
        if node.unicast != node_unicast:
            raise ValueError(
                f"{node_unicast:04X} is a secondary element of node {node.unicast:04X};"
                " Config messages go to the primary unicast"
            )
        return _TxKey(node.dev_key, NONCE_DEVICE, False, 0, f"dev:{node_unicast:04X}")

    async def send_access(
        self, dst: int, access_pdu: bytes, ttl: int | None = None
    ) -> int:
        """Send an access message with AppKey 0 (segmented automatically above 11 bytes). Returns first seq used."""
        return await self._send(dst, access_pdu, ttl, self._app_key)

    async def send_config(
        self, node_unicast: int, access_pdu: bytes, *, ttl: int | None = None
    ) -> int:
        """Send a Config Server message to a node, encrypted with its device key. Returns first seq used.

        `node_unicast` must be the primary unicast of a node in the CDB (ValueError otherwise). Device nonce
        (type 0x02), AKF 0, AID 0, master credentials; segmented above 11 bytes exactly like `send_access`
        (Publication Set and every Composition Data Status are), with SZMIC 0.
        """
        return await self._send(
            node_unicast, access_pdu, ttl, self._dev_key(node_unicast)
        )

    async def _send(
        self,
        dst: int,
        access_pdu: bytes,
        ttl: int | None,
        key: _TxKey,
        on_locked: Callable[[], None] | None = None,
    ) -> int:
        """Send under `_send_lock`; `on_locked` runs once the lock is taken, right before anything is written.

        A request registers its reply waiter there: registered before the lock, a status the node published
        while the request still queued (behind another send, say) would pass for its answer.

        The lock makes reserving sequence numbers and writing them one step: the nodes' replay protection drops a
        number lower than one they already accepted from us, so what is reserved first must reach the air
        first; segment acks (`_send_ack`) and proxy filter requests (`set_filter`) take it too. A segmented message
        holds it only while one round of segments is reserved and written, not while it waits for the
        acknowledgement (up to seconds per round from an absent node, with every light command queued
        behind it); segmented messages to one destination go one at a time (`_sar_lock`).
        """
        ttl = self.ttl if ttl is None else ttl
        if len(access_pdu) > 11:
            async with self._sar_lock(dst):
                return await self._send_segmented(dst, access_pdu, ttl, key, on_locked)
        async with self._send_lock:
            self._refuse_unlinked()
            if on_locked is not None:
                on_locked()
            # the transmit IV index is read under the lock: a beacon may complete an IV Update while we queue
            iv = self.state.tx_iv_index
            seq = self.state.next_seq()
            upper = upper_encrypt(
                key.key, key.nonce, iv, seq, self.state.src, dst, access_pdu
            )
            net = network_encrypt(
                self.nk if key.net is None else key.net,
                iv,
                False,
                ttl,
                seq,
                self.state.src,
                dst,
                lower_unsegmented_access(key.aid, upper, akf=key.akf),
            )
            trace.debug(
                "TX %04X→%04X seq=%06X [%s] %s",
                self.state.src,
                dst,
                seq,
                key.label,
                _Lazy(partial(describe, access_pdu, devkey=key.devkey)),
            )
            await self._write(PROXY_NETWORK_PDU, net)
            return seq

    def _sar_lock(self, dst: int) -> asyncio.Lock:
        """Return the lock that keeps segmented messages to `dst` one at a time (one SeqZero per destination)."""
        lock = self._sar_locks.get(dst)
        if lock is None:
            lock = self._sar_locks[dst] = asyncio.Lock()
        return lock

    async def _send_segmented(
        self,
        dst: int,
        access_pdu: bytes,
        ttl: int,
        key: _TxKey,
        on_locked: Callable[[], None] | None = None,
    ) -> int:
        """Send a segmented access message; returns its SeqAuth (the first sequence number).

        Every retransmission round re-reads the transmit IV index: when an IV Update completes in between, the
        upper transport PDU (encrypted under the old index and SeqAuth) can no longer be verified by anyone, so
        the message starts over under the new index with a fresh SeqAuth.
        """
        n_segments = -(
            -(len(access_pdu) + 4) // SEGMENT_SIZE
        )  # 4-byte TransMIC (SZMIC 0)
        for attempt in range(SEGMENT_RESTARTS + 1):
            seq0 = await self._send_segments_once(
                dst,
                access_pdu,
                ttl,
                key,
                n_segments,
                on_locked if attempt == 0 else None,
            )
            if seq0 is not None:
                return seq0
            if attempt < SEGMENT_RESTARTS:
                log.info(
                    "IV index changed during the segmented message to %04X, starting it over",
                    dst,
                )
        # the index keeps moving: something is badly wrong with what we hear
        raise TimeoutError(
            f"segmented message to {dst:04X} not acknowledged (IV index kept changing)"
        )

    async def _send_segments_once(
        self,
        dst: int,
        access_pdu: bytes,
        ttl: int,
        key: _TxKey,
        n_segments: int,
        on_locked: Callable[[], None] | None = None,
    ) -> int | None:
        """One segmented transmission with its retransmission rounds; None when the IV index moved in between.

        Each round reserves and writes its segments under `_send_lock` and waits for the acknowledgement
        outside it. `ConnectionError` when the link goes away meanwhile: `_release_link` wakes the ack wait like
        an ack would, and neither a "not acknowledged" nor another round's sequence numbers would be right for
        that.
        """
        link = self.client
        # released by `_segment_rounds` once the first round is written
        await self._send_lock.acquire()
        try:
            self._refuse_unlinked()
            if on_locked is not None:
                on_locked()
            iv = self.state.tx_iv_index
            # all of the first round's numbers, or SequenceExhausted
            seq0 = self.state.reserve_seq(n_segments)
            upper = upper_encrypt(
                key.key, key.nonce, iv, seq0, self.state.src, dst, access_pdu
            )
            segments = lower_segments_access(
                key.aid, seq0, upper, SEGMENT_SIZE, akf=key.akf
            )
            if len(segments) != n_segments:  # pragma: no cover — same arithmetic twice
                raise RuntimeError(
                    "segment count differs from the reserved sequence numbers"
                )
        except BaseException:
            self._send_lock.release()
            raise
        trace.debug(
            "TX %04X→%04X seq=%06X [%s] segmented x%d %s",
            self.state.src,
            dst,
            seq0,
            key.label,
            n_segments,
            _Lazy(partial(describe, access_pdu, devkey=key.devkey)),
        )
        return await self._segment_rounds(dst, ttl, segments, link, iv, seq0, key.net)

    async def _segment_rounds(
        self,
        dst: int,
        ttl: int,
        segments: list[bytes],
        link: Any,
        iv: int,
        seq0: int,
        net: NetKeyMaterial | None = None,
    ) -> int | None:
        """Run the rounds of `_send_segments_once`; entered holding `_send_lock`, released after the first writes."""
        seq_zero = seq0 & 0x1FFF
        pending = set(range(len(segments)))
        ack_key = (dst, seq_zero)
        acked = _AckState()
        if is_unicast(dst):
            self._ack_waiters[ack_key] = acked
        try:
            for attempt in range(SEGMENT_RETRIES):
                if attempt:
                    await self._send_lock.acquire()
                try:
                    if attempt and self.state.tx_iv_index != iv:
                        return None
                    # before any number is reserved for a round
                    if self.client is not link:
                        raise ConnectionError(
                            "proxy link lost during the segmented message"
                        )
                    order = sorted(pending)
                    # one synchronous reservation: every number of this round belongs to `iv`'s sequence space,
                    # whatever beacon arrives while the writes below yield to the loop
                    if attempt == 0:
                        seqs = [seq0 + i for i in order]
                    else:
                        first = self.state.reserve_seq(len(order))
                        seqs = [first + k for k in range(len(order))]
                        # a group's second round is no retransmission: it always goes twice
                        if is_unicast(dst):
                            self._stats.segment_retransmissions += len(order)
                    acked.event.clear()  # before the writes: an ack may land while the last one is in flight
                    await self._write_segments(dst, ttl, iv, segments, order, seqs, net)
                finally:
                    self._send_lock.release()
                if not is_unicast(dst):
                    if attempt == 1:
                        return seq0  # groups: send everything twice, no acks
                    await asyncio.sleep(0.3)
                    continue
                await self._wait_for_acks(acked, pending, dst, attempt)
                if acked.cancelled:
                    raise TimeoutError(
                        f"segmented message to {dst:04X} cancelled by the receiver (BlockAck 0: busy or out of resources)"
                    )
                # whatever came in — during the writes, the wait or the grace period — counts
                pending = {i for i in pending if not (acked.block >> i) & 1}
                if not pending:
                    return seq0  # delivered, even if the link went away during the grace period
                if self.client is not link:
                    raise ConnectionError(
                        "proxy link lost during the segmented message"
                    )
        finally:
            self._ack_waiters.pop(ack_key, None)
        raise TimeoutError(f"segmented message to {dst:04X} not acknowledged")

    async def _write_segments(
        self,
        dst: int,
        ttl: int,
        iv: int,
        segments: list[bytes],
        order: list[int],
        seqs: list[int],
        net_key: NetKeyMaterial | None = None,
    ) -> None:
        """Write the segments `order` names, each with its sequence number from `seqs` (under `net_key`, else `nk`)."""
        for i, seq in zip(order, seqs, strict=True):
            net = network_encrypt(
                self.nk if net_key is None else net_key,
                iv,
                False,
                ttl,
                seq,
                self.state.src,
                dst,
                segments[i],
            )
            await self._write(PROXY_NETWORK_PDU, net)

    @staticmethod
    async def _wait_for_acks(
        acked: _AckState, pending: set[int], dst: int, attempt: int
    ) -> None:
        """Wait for a round's acknowledgement; when it leaves segments open, a short grace for late ones."""
        try:
            await asyncio.wait_for(acked.event.wait(), SEGMENT_ACK_TIMEOUT)
            if any(not (acked.block >> i) & 1 for i in pending):
                # grace: acks for later segments usually follow within ~50 ms
                await asyncio.sleep(0.25)
        except asyncio.TimeoutError:
            trace.debug("no segment ack from %04X (attempt %d)", dst, attempt + 1)

    async def request(
        self,
        dst: int,
        access_pdu: bytes,
        expect_opcode: int,
        timeout: float = 3.0,
        retries: int = 3,
        expect_cid: int | None = None,
        match: Callable[[AccessMessage], bool] | None = None,
    ) -> AccessMessage:
        """Send and wait for a status from dst (any source if dst is a group).

        Each unanswered attempt is logged at DEBUG only; the `TimeoutError` after the last one is the caller's to
        report (a WARNING per attempt made every dead element log several lines on each link-up,
        where the HA hub logs one line per node that goes unreachable).

        `timeout` is the wait for the reply of each of the `retries` attempts, counted from when its send is
        through: the wait for the send lock (other sends queued first) and the send itself come on top. Those are
        bounded separately — every GATT write by `GATT_TIMEOUT` (`ConnectionError` past it), a segmented send by
        its acknowledgement rounds — so a request never hangs, but it can take longer than `timeout` times `retries`.

        Matches on source + opcode only, not on destination: JUNG firmware answers a state-changing
        acknowledged Set solely by *publishing* the status to the element's group, and only sends a
        unicast reply when nothing changed (Get, or a retransmitted Set with the same TID).

        `match` narrows the reply further (a predicate over the decoded status — the scene number of a
        Scene Register Status, say) so a late reply to an earlier request to the same element cannot
        satisfy this one. Only the oldest waiter a status fits is resolved by it — except that an acknowledged
        load Set's waiter passes over a status that does not show the state it asks for while a later request
        takes it (`set_shown_by`, `_deliver`): a Get out to the same element answered with the old state while the
        Set was lost on the air does not confirm the Set, which is sent again on its next attempt.
        """
        extra = match

        def match_status(m: AccessMessage) -> bool:
            return (
                m.opcode == expect_opcode
                and m.company_id == expect_cid
                and (not is_unicast(dst) or m.src == dst)
                and (extra is None or extra(m))
            )

        return await self._request(
            dst,
            access_pdu,
            self._app_key,
            match_status,
            timeout,
            retries,
            set_shown_by(access_pdu),
        )

    async def request_config(
        self,
        node_unicast: int,
        access_pdu: bytes,
        expect_opcode: int,
        *,
        timeout: float = 3.0,
        retries: int = 3,
        match: Callable[[AccessMessage], bool] | None = None,
        old_net_key: bool = False,
    ) -> AccessMessage:
        """Send a Config message to a node (device key, `send_config`) and wait for its Config status.

        The reply must come from the node's primary unicast, carry `expect_opcode` (a SIG opcode) and have
        been decrypted with that node's device key (`AccessMessage.key == 'dev:<addr>'`). `match` narrows it
        further — the element / address / model the status echoes, so a retransmitted duplicate of the
        previous step's status cannot pass for this one's.

        `old_net_key` seals the network layer with the key every node holds until a key refresh completes (the
        export's, `KeyRefreshFollower.current`) rather than the one we transmit with — they differ in a proven
        Phase 2 only: a node still waiting for its NetKey Update accepts nothing else (relayed in
        Phase 2 under the key it came with, §3.10.4.1 — unverified on air).
        """
        key = self._dev_key(node_unicast)
        if old_net_key:
            key = replace(key, net=self._net_key(self._kr.current))
        extra = match

        def match_status(m: AccessMessage) -> bool:
            return (
                m.src == node_unicast
                and m.opcode == expect_opcode
                and m.company_id is None
                and m.key == key.label
                and (extra is None or extra(m))
            )

        return await self._request(
            node_unicast, access_pdu, key, match_status, timeout, retries
        )

    async def _request(
        self,
        dst: int,
        access_pdu: bytes,
        key: _TxKey,
        match: Callable[[AccessMessage], bool],
        timeout: float,
        retries: int,
        shows: Callable[[bytes], bool] | None = None,
    ) -> AccessMessage:
        for attempt in range(retries):
            fut: asyncio.Future[AccessMessage] = (
                asyncio.get_running_loop().create_future()
            )
            try:
                await self._send(
                    dst,
                    access_pdu,
                    None,
                    key,
                    partial(self._waiters.append, (match, fut, shows)),
                )
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                if fut.done() and not fut.cancelled() and fut.exception() is None:
                    # a segmented send whose acks got lost: the node applied it and answered all the same
                    return fut.result()
                self._stats.request_timeouts += 1
                log.debug(
                    "no response from %04X (attempt %d/%d)", dst, attempt + 1, retries
                )
            finally:
                self._drop_waiter(fut)
                _retrieve(fut)
        raise TimeoutError(f"no response from {dst:04X}")

    async def collect(
        self, dst: int, access_pdu: bytes, expect_opcode: int, window: float = 2.0
    ) -> list[AccessMessage]:
        """Send to a group and collect one matching status per responding element within the window.

        `ConnectionError` if the link is lost during the window (like `request`; the future every waiter carries
        is what a link loss fails, so it is awaited here rather than left to report "never retrieved"). A
        `TimeoutError` of the send itself (a segmented message to a unicast that never acknowledged it) propagates:
        an empty list would read as "nobody answered" when nothing was asked.
        """
        got: list[AccessMessage] = []
        seen: set[int] = set()

        def match(m: AccessMessage) -> bool:
            if m.opcode == expect_opcode and m.src not in seen:
                seen.add(m.src)
                got.append(m)
            return False  # never resolves the future: only a link loss does

        fut: asyncio.Future[AccessMessage] = asyncio.get_running_loop().create_future()
        register = partial(self._waiters.append, (match, fut, None))
        try:
            # registered once the send lock is ours (`_send`): nothing published while queued is collected
            await self._send(dst, access_pdu, None, self._app_key, register)
            try:
                await asyncio.wait_for(fut, window)
            except asyncio.TimeoutError:
                pass  # the window elapsed: that is the normal end
        finally:
            self._drop_waiter(fut)
            _retrieve(fut)
        return got

    def _drop_waiter(self, fut: asyncio.Future[AccessMessage]) -> None:
        """Remove a request's waiter *in place*.

        A request still queued for the send lock holds a bound `self._waiters.append`, so the list object must
        never be replaced.
        """
        self._waiters[:] = [w for w in self._waiters if w[1] is not fut]

    # ------------------------------------------------------------------ receiving
    def _on_notify(self, _char: Any, data: bytearray) -> None:
        if not data:
            return
        self.last_rx = time.monotonic()
        try:
            r = self._reasm.feed(bytes(data))
            if r is None:
                return
            msg_type, payload = r
            if msg_type == PROXY_NETWORK_PDU:
                self._stats.rx += 1
                self._on_network_pdu(payload)
            elif msg_type == PROXY_BEACON:
                b = self._parse_beacon(payload)
                if b:
                    trace.info(
                        "%sbeacon: iv_index=%d iv_update=%s key_refresh=%s auth=%s",
                        "private " if b.private else "",
                        b.iv_index,
                        b.iv_update,
                        b.key_refresh,
                        b.authenticated,
                    )
                    if b.authenticated:
                        self._beacon_seen.set()
                        if self.state.apply_beacon(b.iv_index, b.iv_update):
                            self._iv_state_changed()
                    else:
                        self._stats.beacons_unauthenticated += 1
                    if b.key_refresh:
                        # Phase 2 beacons are secured with the *new* key (§3.10.4): one ours authenticates means
                        # ours are the new keys already; one it cannot is the only sign that they are being
                        # replaced (or a foreign beacon: the flag itself is not authenticated)
                        log.log(
                            logging.INFO if b.authenticated else logging.WARNING,
                            "key refresh in progress%s",
                            ""
                            if b.authenticated
                            else " (beacon not authenticated by our key): the exported keys may be being replaced",
                        )
                    if self.on_beacon:
                        self.on_beacon(b)
            elif msg_type == PROXY_CONFIG:
                self._on_proxy_config(payload)
            else:
                log.info("proxy msg type %d: %s", msg_type, payload.hex())
        except Exception:
            log.exception("error handling proxy PDU %s", data.hex())

    def _iv_state_changed(self) -> None:
        """Act on a move of the IV state (a beacon, or the end of an update we started): log, purge, re-send the filter.

        The filter goes again because it was sealed with the old transmit index, which may be over now.
        """
        log.warning(
            "IV state now index=%d update_active=%s (tx index %d)",
            self.state.iv_index,
            self.state.iv_update_active,
            self.state.tx_iv_index,
        )
        self._purge_rpl()
        if self._filter_type is not None:
            self._start_filter_task(self._filter_type, "IV index changed")

    # ------------------------------------------------------------------ IV Update initiation
    async def start_iv_update(self) -> int:
        """Start an IV Update and send the proxy our Secure Network Beacon of it; return the new IV index.

        Mesh Protocol 1.1 §6.7: the proxy processes a beacon from its client as any other (§3.10.3.1), moves to IV
        Update in Progress (§3.11.5) and beacons the new state to the mesh and back to us — the confirmation
        (`LocalState.iv_update_confirmed`). The state moves first and is on disk (`LocalState.persist_durably`)
        before the beacon is written; a write that did not land puts it back and raises (OSError). Refused with
        `ConnectionError` without a link, `IVUpdateRefused` by `LocalState.start_iv_update`'s rules (`iv_unknown`
        when no beacon of this link named the index, `key_refresh` while this client follows one). A link lost
        before the beacon went leaves the update started: the next link sends it. The beacon is repeated and the
        update completed by `_run_iv_update`. Unverified on air.
        """
        if not self.ready:
            raise ConnectionError("not connected to a proxy")
        if self.key_refresh_phase:
            raise IVUpdateRefused(
                "key_refresh", f"a key refresh is in phase {self.key_refresh_phase}"
            )
        new = self.state.start_iv_update(index_confirmed=self.beacon_seen)
        try:
            await self.state.persist_durably()
        except BaseException:
            self.state.revert_iv_update_start()
            raise
        log.warning(
            "IV Update started: IV index %d in progress (still transmitting with %d)",
            new,
            self.state.tx_iv_index,
        )
        self._purge_rpl()
        self._start_iv_task(sent=await self._send_iv_beacon())
        return new

    async def _send_iv_beacon(self) -> bool:
        """Write our Secure Network Beacon of the IV state to the proxy; False when the link could not take it.

        A beacon takes no sequence number. It is that of the key refresh's current phase (§3.10.3): authenticated with
        the key we transmit with (`nk`: the new one from a proven Phase 2 on) and with the Key Refresh flag set in
        Phase 2. A key refresh may start during the update's 96 h; a beacon under the new key with the flag clear
        tells a node in Phase 1 or 2 that the refresh is over (§3.11.4.1), and it revokes the old key — cutting off
        every node the provisioner had not reached yet (review-5 P5-2).
        """
        payload = secure_network_beacon(
            self.nk,
            self.state.iv_index,
            iv_update=self.state.iv_update_active,
            key_refresh=self.key_refresh_phase == 2,
        )
        try:
            await self._write(PROXY_BEACON, payload)
        except ConnectionError as err:
            log.debug("IV Update beacon not sent: %s", err)
            return False
        trace.info(
            "beacon sent: iv_index=%d iv_update=%s key_refresh=%s",
            self.state.iv_index,
            self.state.iv_update_active,
            self.key_refresh_phase == 2,
        )
        return True

    def abort_iv_update(self) -> int:
        """Abort the IV Update this client started before the mesh took it; return the IV index it went back to.

        `ConnectionError` without a link; `IVUpdateRefused` by `LocalState.abort_iv_update`'s rules (`iv_unknown`
        unless a beacon of this link said where the mesh is). The beacons stop (`_run_iv_update` ends with the
        update; the transmit index never moved, so the proxy filter stands). Unverified on air.
        """
        if not self.ready:
            raise ConnectionError("not connected to a proxy")
        back = self.state.abort_iv_update(index_confirmed=self.beacon_seen)
        log.warning("IV Update aborted: back to IV index %d", back)
        return back

    def _start_iv_task(self, sent: bool) -> None:
        """Run `_run_iv_update` while an update this client started is in progress (one per link)."""
        state = self.state
        if not (state.iv_update_active and state.iv_update_origin == IV_ORIGIN_LOCAL):
            return
        if self._iv_task is not None and not self._iv_task.done():
            return
        self._iv_task = self._spawn(self._run_iv_update(sent))

    async def _run_iv_update(self, sent: bool) -> None:
        """Beacon the update this client started every Beacon Interval, and end it when `LocalState.iv_update_due`.

        `IV_BEACON_INTERVAL` until the mesh took it (`iv_update_confirmed`), `IV_BEACON_INTERVAL_MAX` after; `sent`:
        the first beacon of this link went already. The return to Normal Operation (§3.11.5) is deferred while a
        segmented message of ours awaits its acknowledgment: its SeqAuth would not survive the sequence restarting
        at 0. Then the proxy gets our beacon of Normal Operation. An update the mesh has not taken 144 h after its
        start (`LocalState.iv_update_overdue`) is given up once a beacon of this link was seen — still unconfirmed,
        so the proxy beaconed the old index — back at that index (`LocalState.abandon_iv_update`), and
        `on_iv_update_abandoned` is told (review-5 P5-3). Ends when the update ends (a beacon of the mesh can end it
        first) or the link goes.
        """
        state = self.state
        while state.iv_update_active and state.iv_update_origin == IV_ORIGIN_LOCAL:
            if self.beacon_seen and state.iv_update_overdue():
                state.abandon_iv_update(IV_ABANDONED_NOT_TAKEN)
                log.warning(
                    "IV Update to IV index %d given up: the mesh did not take it within %d h; back to IV index %d "
                    "(nothing was sent under the new index)",
                    state.iv_index + 1,
                    IV_UPDATE_MAX_STATE // 3600,
                    state.iv_index,
                )
                if self.on_iv_update_abandoned:
                    self.on_iv_update_abandoned()
                return
            if state.iv_update_due():
                if not self._ack_waiters and state.complete_iv_update():
                    log.warning("IV Update completed: back to Normal Operation")
                    self._iv_state_changed()
                    await self._send_iv_beacon()
                    return
                await asyncio.sleep(SEGMENT_ACK_TIMEOUT)
                continue
            if not sent and not await self._send_iv_beacon():
                return
            sent = False
            await asyncio.sleep(
                IV_BEACON_INTERVAL_MAX
                if state.iv_update_confirmed
                else IV_BEACON_INTERVAL
            )

    def _on_proxy_config(self, payload: bytes) -> None:
        """Take a proxy configuration PDU (§6.5): the Filter Status that names the proxy node and acknowledges our filter.

        It gets the checks every other PDU gets: CTL=1 and DST unassigned, as §6.5 requires of
        these and only these, and no replay — an (IV index, SEQ) at or below the last one taken on this link is
        dropped. Without them a recorded Filter Status could be played back to set `proxy_addr` and stand in for
        the acknowledgement of a filter the proxy never took. What is dropped is counted
        (`rx_proxy_config_dropped`).
        """
        n = self._network_decrypt(payload, proxy=True)
        if n is None or not n.ctl or n.dst != 0x0000:
            self._stats.proxy_config_dropped += 1
            trace.debug(
                "proxy configuration PDU dropped: %s",
                "not ours"
                if n is None
                else f"CTL={int(n.ctl)} DST={n.dst:04X} from {n.src:04X}",
            )
            return
        if self._proxy_config_last is not None and (
            (n.iv_index, n.seq) <= self._proxy_config_last
        ):
            self._stats.proxy_config_dropped += 1
            self._stats.proxy_config_replays += 1
            trace.debug(
                "proxy configuration PDU from %04X seq %06X dropped: a replay",
                n.src,
                n.seq,
            )
            return
        self._proxy_config_last = (n.iv_index, n.seq)
        pdu = n.transport_pdu
        if len(pdu) >= 4 and pdu[0] == 0x03:
            self.proxy_addr = n.src
            self._filter_acked.set()
            log.info(
                "proxy filter status: type=%s list_size=%d (proxy node %04X)",
                "blacklist" if pdu[1] else "whitelist",
                int.from_bytes(pdu[2:4], "big"),
                n.src,
            )
            if self.on_filter_status:
                self.on_filter_status(n.src)
        elif pdu[0] == 0x03:
            trace.debug(
                "proxy filter status from %04X too short (%d octets), dropped",
                n.src,
                len(pdu),
            )
        else:
            log.info("proxy config pdu %s", payload.hex())

    def _network_decrypt(
        self, payload: bytes, proxy: bool = False
    ) -> NetworkPDU | None:
        """Open a network PDU with any NetKey we accept (`rx_net_keys`); None when none opens it."""
        for key in self.rx_net_keys:
            n = network_decrypt(key, self.state.iv_index, payload, proxy=proxy)
            if n is not None:
                return n
        return None

    def _parse_beacon(self, payload: bytes) -> SecureNetworkBeacon | None:
        """Parse a Secure Network or Mesh Private beacon with every key we accept; the first that authenticates it wins.

        A beacon secured with a new key moves a key refresh on (§3.10.4.1): Key Refresh flag set = Phase 2,
        clear = Phase 3 (the old key is revoked). It comes from the proxy node itself, which beacons with the keys
        it uses: proof that the mesh moved (`KeyRefreshFollower.beacon`).

        A proxy with Mesh Protocol 1.1 privacy on sends Mesh Private beacons instead (§3.10.4; unverified on air): the
        same flags and IV index, sealed with the private beacon key, so they move the IV state and prove a key
        refresh alike. Only the key it was made with opens one: a private beacon none of ours opens is None, not an
        unauthenticated beacon.
        """
        private = payload[:1] == bytes([BEACON_PRIVATE])
        parse = parse_private_beacon if private else parse_beacon
        first: SecureNetworkBeacon | None = None
        for key in self.rx_net_keys:
            b = parse(key, payload)
            if b is None:
                if private:
                    continue  # not this key's: perhaps the next one's
                return None
            if b.authenticated:
                if key.key != self._kr.current:
                    self._key_refresh_moved(self._kr.beacon(key.key, b.key_refresh))
                return b
            first = first or b
        if private:
            trace.debug("Mesh Private beacon that no key of ours opens dropped")
        return first

    def _follow_key_refresh(self, msg: AccessMessage) -> None:
        """Learn a key refresh from the provisioner's own messages (§3.10.4, §4.3.2.8, §4.3.2.46).

        The app refreshes the NetKey by sending every node a Config NetKey Update with the new key, then Config Key
        Refresh Phase Set 2 and 3 (`docs/android/transport-provisioning.md` §4.2). They are sealed with each node's
        device key, which the export gives us, and the blacklist filter forwards them: without following them the
        link goes deaf at Phase 2 and dead at Phase 3, until the entry is set up again from a new export.

        A node can seal the same messages with its own device key, so they are followed only as evidence
        (`keyrefresh`): a NetKey Update or Phase Set counts when it is addressed to a JUNG device's primary
        element, opened with *that* device's key and not sent from a device's element; a NetKey Status or Key
        Refresh Phase Status when a device's primary element sealed it with its own key. Only proof moves the
        refresh past Phase 1, never a request.
        """
        if not msg.key.startswith("dev:") or msg.company_id is not None:
            return
        p = msg.params
        moved: Moved | None = None
        if msg.opcode in (CONFIG_NETKEY_UPDATE, CONFIG_KEY_REFRESH_PHASE_SET):
            if len(p) < 3 or int.from_bytes(p[:2], "little") & 0x0FFF != NET_KEY_INDEX:
                return
            if not (
                msg.dst in self._device_primaries
                and msg.key == f"dev:{msg.dst:04X}"
                and msg.src not in self._device_elements
            ):
                log.log(
                    logging.WARNING
                    if msg.opcode == CONFIG_NETKEY_UPDATE
                    else logging.DEBUG,
                    "Config %s %04X→%04X not followed: it does not come from a provisioner",
                    CONFIG_NAMES[msg.opcode],
                    msg.src,
                    msg.dst,
                )
                return
            if msg.opcode == CONFIG_KEY_REFRESH_PHASE_SET:
                self._kr.requested(msg.dst, p[2])
            elif len(p) >= 18:
                moved = self._kr.learn(bytes(p[2:18]), msg.dst)
        elif msg.opcode in (CONFIG_NETKEY_STATUS, CONFIG_KEY_REFRESH_PHASE_STATUS):
            if (
                len(p) < 3
                or int.from_bytes(p[1:3], "little") & 0x0FFF != NET_KEY_INDEX
                or msg.src not in self._device_primaries
                or msg.key != f"dev:{msg.src:04X}"
            ):
                return
            if msg.opcode == CONFIG_NETKEY_STATUS:
                moved = self._kr.netkey_status(msg.src, p[0] == 0, self.proxy_addr)
            elif len(p) >= 4 and p[0] == 0:
                moved = self._kr.phase_status(msg.src, p[3], self.proxy_addr)
        else:
            return
        self._key_refresh_moved(moved)

    def _key_refresh_moved(self, moved: Moved | None) -> None:
        """Persist the followed refresh; when it moved, log how it was proven and tell the application."""
        self.state.set_key_refresh(self._kr.record())
        accepted = set(self._kr.rx_keys)
        for key in [k for k in self._net_keys if k not in accepted]:
            del self._net_keys[key]
        if moved is None:
            return
        new = self._net_key(moved.key)
        if moved.phase == 1 and moved.proof is None:
            log.warning(
                "the provisioner is refreshing the network key: following it (phase 1): the new key is accepted, "
                "not used until the mesh proves it moved"
            )
        elif moved.phase == 1:
            log.warning(
                "key refresh phase 1 proven (proof: %s): the new key is the provisioner's",
                describe_proof(moved),
            )
        else:
            log.warning(
                "key refresh %s (proof: %s): %s",
                "phase 2" if moved.phase == 2 else "complete",
                describe_proof(moved),
                "transmitting with the new network key"
                if moved.phase == 2
                else "the new network key is the only one now",
            )
            if self._filter_type is not None and self.client is not None:
                self._start_filter_task(self._filter_type, "network key changed")
        if self.on_key_refresh:
            try:
                self.on_key_refresh(moved.phase, new)
            except Exception:
                log.exception("on_key_refresh handler failed")

    def _on_network_pdu(self, payload: bytes) -> None:
        n = self._network_decrypt(payload)
        if n is None:
            trace.debug("undecryptable network PDU %s", payload.hex())
            self._count_undecryptable()
            return
        if n.src == self.state.src:
            self._on_own_source(n)
            return
        try:
            kind = parse_lower(n.transport_pdu, n.ctl)
        except ValueError as err:  # authenticated with the NetKey, yet not a lower transport PDU: dropped, no trace
            trace.debug(
                "RX %04X→%04X malformed lower transport PDU dropped: %s",
                n.src,
                n.dst,
                err,
            )
            return
        if kind[0] == "ctl":
            # the replay list covers control PDUs too (§3.8.8): a recorded Heartbeat would keep a dead node alive,
            # a recorded Segment Ack acknowledge a message the node never got
            iv = n.iv_index
            if self._is_replay(n.src, iv, n.seq):
                self._stats.replays_dropped += 1
                trace.debug("replay from %04X seq %06X ignored (control)", n.src, n.seq)
                return
            op, p = kind[1], kind[2]
            if self.on_control:
                try:
                    self.on_control(n.src, op)
                except Exception:
                    log.exception("on_control handler failed")
            if op == 0x00 and len(p) >= 6:
                self.state.note_received(n.src, iv, n.seq)
                hdr = int.from_bytes(p[:2], "big")
                seq_zero, block = (hdr >> 2) & 0x1FFF, int.from_bytes(p[2:6], "big")
                trace.debug(
                    "RX %04X→%04X Segment Ack seq_zero=%d block=%08X",
                    n.src,
                    n.dst,
                    seq_zero,
                    block,
                )
                w = self._ack_waiters.get((n.src, seq_zero))
                if w and n.dst == self.state.src:
                    if block == 0:
                        w.cancelled = (
                            True  # §3.5.3.3: the receiver cancelled the message
                        )
                    w.block |= block
                    w.event.set()
            elif op == HEARTBEAT_OPCODE and len(p) >= 3:
                self.state.note_received(n.src, iv, n.seq)
                beat = Heartbeat(
                    n.src, n.dst, p[0] & 0x7F, n.ttl, int.from_bytes(p[1:3], "big")
                )
                trace.debug(
                    "RX %04X→%04X Heartbeat init_ttl=%d hops=%d features=%04X",
                    n.src,
                    n.dst,
                    beat.init_ttl,
                    beat.hops,
                    beat.features,
                )
                if self.on_heartbeat:
                    try:
                        self.on_heartbeat(beat)
                    except Exception:
                        log.exception("on_heartbeat handler failed")
            else:
                trace.debug(
                    "RX %04X→%04X control op %02X %s", n.src, n.dst, op, p.hex()
                )
            return
        if kind[0] == "unseg":
            _, akf, aid, upper = kind
            self._deliver(n, akf, aid, upper, szmic=0, seq_auth=n.seq)
        elif kind[0] == "seg":
            self._on_segment(n, kind[1])

    def _on_own_source(self, n: NetworkPDU) -> None:
        """Take a PDU from our own address: ours echoed back by a relay or the proxy, or another client's.

        An echo carries a number we handed out (`_handed_out`) and is dropped silently, as always. Anything else
        proves a second client on our address — a second Home Assistant, the CLI on its address, a restored store —
        whose numbers ours will run into: every one we send that it has sent too reuses a nonce, and the nodes drop
        as replays whatever lies below its last. Reported (`on_foreign_own_source`, with the highest number seen on
        this link) at most once per `FOREIGN_SOURCE_REPORT_INTERVAL`, and dropped too.
        """
        seen = (n.iv_index, n.seq)
        if self._handed_out(*seen):
            return  # our own message echoed back by the proxy/relay
        if self.foreign_own_source is None or seen > self.foreign_own_source:
            self.foreign_own_source = seen
        now = _now()
        if (
            self._foreign_reported_at is not None
            and now - self._foreign_reported_at < FOREIGN_SOURCE_REPORT_INTERVAL
        ):
            return
        self._foreign_reported_at = now
        iv, seq = self.foreign_own_source
        log.warning(
            "a PDU from our own address %04X under IV index %d with sequence number %06X, which we never sent: "
            "another client uses this address",
            n.src,
            iv,
            seq,
        )
        if self.on_foreign_own_source:
            try:
                self.on_foreign_own_source(iv, seq)
            except Exception:
                log.exception("on_foreign_own_source handler failed")

    def _handed_out(self, iv: int, seq: int) -> bool:
        """Whether (`iv`, `seq`) may be a number our address sent, as far as `state` can tell.

        Under the transmit index everything below the counter was handed out. Under an older index (an echo from
        before an IV Update completed) everything below the counter and `seq_peak` may have been, from
        `seq_peak_from` on; below that the record knows nothing, so it may be ours. A newer index we have never
        transmitted under — unless `seq_guard` says the counter carried on under it (a lost record skipped past,
        an IV index gone back), where again everything below the counter may be ours.
        """
        state = self.state
        tx = state.tx_iv_index
        if iv == tx:
            return seq < state.seq
        if iv > tx:
            guard = state.seq_guard
            return (
                guard is not None
                and (guard == SEQ_GUARD_FIRST_BEACON or iv <= guard)
                and seq < state.seq
            )
        return iv < state.seq_peak_from or seq < max(state.seq, state.seq_peak)

    def _is_replay(self, src: int, iv: int, seq: int) -> bool:
        """Whether `seq` under `iv` is at or below the last sequence number accepted from `src` (§3.8.8).

        A new source the full replay list cannot take counts as a replay too (`LocalState.admits`): without an
        entry, nothing would stop its PDUs from being accepted again.
        """
        last = self.state.rpl.get(src)
        if last is None:
            return not self.state.admits(src)
        return (iv, seq) <= last

    def _purge_rpl(self) -> None:
        """Drop replay-list entries of IV indexes older than the two still receivable (current and current - 1).

        The list must survive an IV Update (§3.8.8): nodes keep sending under the old index for hours, so a PDU
        captured before the beacon would be accepted again if the list were simply cleared.
        """
        self.state.purge_rpl(self.state.iv_index - 1)

    def _on_segment(self, n: NetworkPDU, s: SegmentInfo) -> None:
        """Reassemble a segmented message and acknowledge it as Mesh Protocol 1.1 §3.5.3.4 (Reassembly behavior) says.

        A reassembly is kept per (source, SeqZero). A new segment (First / Next Segment) of a message to our address
        starts the SAR Acknowledgment timer again — min(SegN + 0.5, `SAR_ACK_DELAY_INCREMENT`) segment reception
        intervals (`SAR_SEGMENT_INTERVAL`) — and when it fires the segments received so far are acknowledged
        (`_ack_timer`); the segment that completes the message stops it and acknowledges every segment at once (Last
        Segment). A segment already received (Repeated Segment) changes nothing. The SAR Discard timer: a reassembly
        with no new segment for `SAR_DISCARD_TIMEOUT` is dropped, segments and all, when the next segment arrives
        (nothing can complete it meanwhile). A segment of the message last completed from that source (Most Recent
        SeqAuth) is acknowledged again as complete — the sender did not hear our acknowledgment — at most once per
        `SAR_ACK_DELAY_INCREMENT` intervals, and is not delivered again. Only messages to our own address are
        acknowledged: a group's or another node's never.
        """
        key = (n.src, s.seq_zero)
        now = time.monotonic()
        for k in [
            k for k, v in self._segments.items() if now - v["t"] > SAR_DISCARD_TIMEOUT
        ]:
            self._drop_reassembly(k)
        if s.seg_o > s.seg_n:
            trace.debug(
                "RX %04X→%04X segment %d of %d: SegO beyond SegN, dropped",
                n.src,
                n.dst,
                s.seg_o,
                s.seg_n + 1,
            )
            return
        try:
            seq_auth = seq_auth_from(n.seq, s.seq_zero)
        except ValueError as err:
            trace.debug("RX %04X→%04X segment dropped: %s", n.src, n.dst, err)
            return
        st = self._segments.get(key)
        if st is None:
            st = self._start_reassembly(n, s, key, seq_auth, now)
        if st is None or not self._fits_reassembly(n, s, st, seq_auth):
            return
        ours = n.dst == self.state.src
        if st["done"]:
            st["t"] = now  # keeps the entry: it answers the sender's retransmissions
            if ours:  # Most Recent SeqAuth: the sender did not see our ack; repeat it
                self._ack_complete(n.src, s.seq_zero, st, now)
            return
        if s.seg_o in st["parts"]:
            return  # Repeated Segment: ignored, no timer restarts
        st["t"] = now  # the SAR Discard timer starts again with every new segment
        st["parts"][s.seg_o] = s.data
        if (
            len(st["parts"]) == s.seg_n + 1
        ):  # every index 0..SegN is present (SegO was range-checked)
            data = b"".join(st["parts"][i] for i in range(s.seg_n + 1))
            st["done"], st["parts"] = True, {}
            self._stop_ack_timer(st)
            self._seq_auth_done[n.src] = max(
                self._seq_auth_done.get(n.src, (-1, -1)),
                (n.iv_index, st["seq_auth"]),
            )
            if ours:
                self._ack_complete(n.src, s.seq_zero, st, now)
            self._deliver(
                n, s.akf, s.aid, data, s.szmic, st["seq_auth"], segmented=True
            )
        elif ours:
            self._stop_ack_timer(st)
            delay = min(s.seg_n + 0.5, SAR_ACK_DELAY_INCREMENT) * SAR_SEGMENT_INTERVAL
            st["ack_task"] = self._spawn(self._ack_timer(key, st, delay))

    def _start_reassembly(
        self,
        n: NetworkPDU,
        s: SegmentInfo,
        key: tuple[int, int],
        seq_auth: int,
        now: float,
    ) -> dict[str, Any] | None:
        """Start the reassembly a segment opens, or None when the segment is a replay (`_on_segment`).

        The replay check belongs to the *start* of a reassembly (checking the completed message would drop a
        late-completing one after acknowledging it). It is on the segment's own sequence number, as §3.8.8
        checks every PDU: a recorded segment replayed later carries its old number, below the list, while
        a retransmission carries a fresh one — checking the message's SeqAuth instead would lock a message
        out for good once anything the node sent after it (a Heartbeat, a Segment Ack) moved the list past
        that SeqAuth. A message already delivered is recognised by its SeqAuth instead (`_seq_auth_done`): an
        older one is a replay, the last one a retransmission to acknowledge again (Most Recent SeqAuth, §3.5.3.4).
        """
        done = self._seq_auth_done.get(n.src, (-1, -1))
        if self._is_replay(n.src, n.iv_index, n.seq) or (n.iv_index, seq_auth) < done:
            self._stats.replays_dropped += 1
            trace.debug(
                "replay from %04X seq %06X ignored (segment of SeqAuth %06X)",
                n.src,
                n.seq,
                seq_auth,
            )
            return None
        st: dict[str, Any] = {
            "parts": {},
            "n": s.seg_n,
            "t": now,
            "done": (n.iv_index, seq_auth) == done,
            "seq_auth": seq_auth,
            "acked_at": None,  # monotonic time of the last complete acknowledgment
            "ack_task": None,  # the SAR Acknowledgment timer running
        }
        self._segments[key] = st
        return st

    @staticmethod
    def _fits_reassembly(
        n: NetworkPDU, s: SegmentInfo, st: dict[str, Any], seq_auth: int
    ) -> bool:
        """Whether a segment belongs to the reassembly of its (source, SeqZero): same SegN, same SeqAuth."""
        if s.seg_n != st["n"]:
            # a different SegN under the same SeqZero: not the message being assembled
            trace.debug(
                "RX %04X→%04X segment %d of %d contradicts the %d segments announced, dropped",
                n.src,
                n.dst,
                s.seg_o,
                s.seg_n + 1,
                st["n"] + 1,
            )
            return False
        if seq_auth != st["seq_auth"]:
            # same SeqZero, another message (8192·k earlier): not a part of this one
            trace.debug(
                "RX %04X→%04X segment of SeqAuth %06X does not belong to reassembly %06X, dropped",
                n.src,
                n.dst,
                seq_auth,
                st["seq_auth"],
            )
            return False
        return True

    def _drop_reassembly(self, key: tuple[int, int]) -> None:
        """Discard a reassembly whose SAR Discard timer ran out, with its acknowledgment timer (§3.5.3.4)."""
        self._stop_ack_timer(self._segments.pop(key))

    def _stop_ack_timer(self, st: dict[str, Any]) -> None:
        """Stop a reassembly's SAR Acknowledgment timer, if one is running."""
        task, st["ack_task"] = st["ack_task"], None
        if task is not None:
            task.cancel()
            self._tasks.discard(task)

    async def _ack_timer(
        self, key: tuple[int, int], st: dict[str, Any], delay: float
    ) -> None:
        """Run a reassembly's SAR Acknowledgment timer: when it fires, acknowledge the segments received so far."""
        await asyncio.sleep(delay)
        # fired: a new segment starts a timer of its own rather than cancel this acknowledgment
        st["ack_task"] = None
        block = 0
        for i in st["parts"]:
            block |= 1 << i
        await self._send_ack(key[0], key[1], block, self._ack_transmissions(st["n"]))

    def _ack_complete(
        self, src: int, seq_zero: int, st: dict[str, Any], now: float
    ) -> None:
        """Acknowledge every segment of a completed message, at most once per `SAR_ACK_DELAY_INCREMENT` intervals."""
        last = st["acked_at"]
        if (
            last is not None
            and now - last < SAR_ACK_DELAY_INCREMENT * SAR_SEGMENT_INTERVAL
        ):
            return
        st["acked_at"] = now
        self._spawn(
            self._send_ack(
                src,
                seq_zero,
                (1 << (st["n"] + 1)) - 1,
                self._ack_transmissions(st["n"]),
            )
        )

    @staticmethod
    def _ack_transmissions(seg_n: int) -> int:
        """How often a Segment Acknowledgment goes: once, or 1 + the retransmissions count above the threshold (§3.5.3.4)."""
        if seg_n + 1 > SAR_SEGMENTS_THRESHOLD:
            return SAR_ACK_RETRANSMISSIONS + 1
        return 1

    async def _send_ack(
        self, dst: int, seq_zero: int, block: int, transmissions: int = 1
    ) -> None:
        """Acknowledge a segmented message; its sequence number is reserved and written under `_send_lock`.

        Taken outside the lock, the number could land in the middle of a segmented round, whose numbers are all
        reserved before its first segment is written: the ack reached the air ahead of the round's remaining
        segments with a higher number, and the nodes' replay protection dropped those. `transmissions` > 1 repeats
        it, each with a new sequence number, `SAR_SEGMENT_INTERVAL` apart (§3.5.3.4).
        """
        if not is_unicast(dst):
            return
        for sent in range(transmissions):
            if sent:
                await asyncio.sleep(SAR_SEGMENT_INTERVAL)
            try:
                async with self._send_lock:
                    self._refuse_unlinked()
                    pdu = network_encrypt(
                        self.nk,
                        self.state.tx_iv_index,
                        ctl=True,
                        ttl=self.ttl,
                        seq=self.state.next_seq(),
                        src=self.state.src,
                        dst=dst,
                        transport_pdu=segment_ack(seq_zero, block),
                    )
                    await self._write(PROXY_NETWORK_PDU, pdu)
            except (
                ConnectionError
            ):  # link gone, or the sequence space exhausted: nothing we can send
                return

    def _count_undecryptable(self) -> None:
        """One more PDU our keys cannot open: count it and tell the application.

        Every JUNG node relays, so a stale NetKey (a completed key refresh after the export) shows as an unbroken
        stream of these while nothing decodes; a single one is normal (another mesh in range, a node we do not know).
        """
        self._stats.undecryptable += 1
        if self.on_undecryptable:
            self.on_undecryptable()

    def _deliver(
        self,
        n: NetworkPDU,
        akf: bool,
        aid: int,
        upper: bytes,
        szmic: int,
        seq_auth: int,
        segmented: bool = False,
    ) -> None:
        """Decrypt the upper transport PDU, apply replay protection, decode and hand the message out.

        The replay list holds the highest network sequence number accepted per source (§3.8.8, "the last received").
        An unsegmented message is checked here against it; a segmented one was checked on its SeqAuth when its
        first segment was admitted (`_on_segment`) and only advances the list here — by the sequence number of
        the segment that completed it, never backwards. Nothing is advanced before the PDU decoded completely.
        """
        iv = n.iv_index
        access, key = None, ""
        if akf:
            if aid == self.ak.aid:
                access = upper_decrypt(
                    self.ak.key, NONCE_APP, iv, seq_auth, n.src, n.dst, upper, szmic
                )
                key = "app0"
        else:
            for addr in (n.src, n.dst):
                dk = self._dev_key_of_element.get(addr)
                if dk:
                    access = upper_decrypt(
                        dk, NONCE_DEVICE, iv, seq_auth, n.src, n.dst, upper, szmic
                    )
                    if access is not None:
                        key = f"dev:{addr:04X}"
                        break
        if access is None:
            trace.debug(
                "RX %04X→%04X undecryptable upper transport (akf=%s aid=%d)",
                n.src,
                n.dst,
                akf,
                aid,
            )
            self._count_undecryptable()
            return
        if not segmented and self._is_replay(n.src, iv, n.seq):
            self._stats.replays_dropped += 1
            trace.debug("replay from %04X seq %06X ignored", n.src, seq_auth)
            return
        try:
            op, cid, params = decode_opcode(access)
        except ValueError as err:  # authenticated, yet no access message (§3.7.3): dropped before the list moves
            trace.debug(
                "RX %04X→%04X malformed access PDU dropped: %s", n.src, n.dst, err
            )
            return
        self.state.note_received(n.src, iv, n.seq)
        self._stats.messages += 1
        if n.dst == self.state.src:
            self._stats.messages_to_us += 1
        msg = AccessMessage(n.src, n.dst, n.ttl, n.seq, op, cid, params, access, key)
        self._follow_key_refresh(msg)
        trace.debug(
            "RX %s", msg
        )  # per-message traffic stays out of INFO (a token read would show up there)
        # every pending predicate sees it (collect() gathers through its own, never resolving), but one status
        # answers one request: the oldest waiter it fits, skipping an acknowledged Set's when the status does not
        # show the state the Set asks for and a later waiter takes it — a Get to the same element answered with the
        # old state while the Set was lost on the air. With no other waiter it still answers the
        # oldest: a load that clamped the value answers its Set with a state the Set did not ask for.
        fits = [
            (fut, shows)
            for match, fut, shows in list(self._waiters)
            if not fut.done() and match(msg)
        ]
        answered = next(
            (fut for fut, shows in fits if shows is None or shows(msg.params)),
            fits[0][0] if fits else None,
        )
        if answered is not None:
            answered.set_result(msg)
        if self.on_message:
            try:
                self.on_message(msg)
            except Exception:
                log.exception("on_message handler failed")
