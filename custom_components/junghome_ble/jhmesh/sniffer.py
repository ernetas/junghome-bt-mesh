"""Passive decoding of sniffed mesh traffic: BLE sniffer records → decrypted access messages.

A GATT proxy client (`ProxyClient`) only sees what its proxy node forwards, after the network cache dropped the
relay copies and with the TTL the last relay left. A BLE sniffer next to the installation (Nordic nRF Sniffer for
Bluetooth LE, `docs/sniffer.md`) sees every advertising-bearer PDU as sent: the originator's TTL, every relay and
network-transmit copy, RSSI and channel — and needs no mesh address, no sequence numbers and no proxy slot, so it
can watch the app, the gateway and Home Assistant talk to the nodes without taking part.

The capture side is key-free: `tools/mesh_sniff.py capture` runs next to the dongle and writes one JSON line per
mesh AD structure (`SniffRecord`). `MeshDecoder` turns those lines (or a Nordic pcap) into `AccessMessage`s with
the keys of an export, collapsing the copies of one network PDU and reassembling segmented messages. It keeps no
replay list: a passive observer wants to see retransmissions, not drop them. Retransmitted segments of a message it
already completed are reported as segments, though, not as a second message.
"""

from __future__ import annotations

import json
import struct
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from .client import AccessMessage
from .pdu import (
    BEACON_PRIVATE,
    NONCE_APP,
    NONCE_DEVICE,
    PROXY_BEACON,
    PROXY_CONFIG,
    PROXY_NETWORK_PDU,
    PROXY_PROVISIONING,
    NetworkPDU,
    ProxyReassembler,
    SecureNetworkBeacon,
    SegmentInfo,
    decode_opcode,
    network_decrypt,
    parse_beacon,
    parse_lower,
    parse_private_beacon,
    seq_auth_from,
    upper_decrypt,
)
from .provisioning import UnprovisionedDevice, parse_unprovisioned_beacon

if TYPE_CHECKING:
    from .cdb import CDB
    from .crypto import AppKeyMaterial, NetKeyMaterial

__all__ = [
    "AD_BEACON",
    "AD_MESSAGE",
    "AD_PB_ADV",
    "COPY_MEMORY",
    "LINKTYPE_NORDIC_BLE",
    "PRUNE_EVERY",
    "SEGMENT_MEMORY",
    "MeshDecoder",
    "SniffRecord",
    "ad_structures",
    "format_address",
    "mesh_records",
    "read_pcap",
]

# AD types of the mesh advertising bearer (Mesh Profile §3.3.1 / CSS): PB-ADV, Network PDU, Mesh Beacon
AD_PB_ADV, AD_MESSAGE, AD_BEACON = 0x29, 0x2A, 0x2B
AD_KINDS: dict[int, str] = {AD_PB_ADV: "pbadv", AD_MESSAGE: "msg", AD_BEACON: "beacon"}
GATT_KIND = "gatt"  # an ATT PDU captured inside a followed connection (`pdu` = the ATT PDU, `direction` set)

# ATT opcodes (Core Vol 3 Part F §3.4): the ones that carry Mesh Proxy PDUs, and the names of the rest
ATT_WRITE_REQUEST, ATT_WRITE_RESPONSE, ATT_WRITE_COMMAND = 0x12, 0x13, 0x52
ATT_NOTIFICATION, ATT_INDICATION = 0x1B, 0x1D
ATT_NAMES = {
    0x01: "Error Response",
    0x02: "Exchange MTU Request",
    0x03: "Exchange MTU Response",
    0x04: "Find Information Request",
    0x05: "Find Information Response",
    0x06: "Find By Type Value Request",
    0x07: "Find By Type Value Response",
    0x08: "Read By Type Request",
    0x09: "Read By Type Response",
    0x0A: "Read Request",
    0x0B: "Read Response",
    0x0C: "Read Blob Request",
    0x0D: "Read Blob Response",
    0x10: "Read By Group Type Request",
    0x11: "Read By Group Type Response",
    ATT_WRITE_REQUEST: "Write Request",
    ATT_WRITE_RESPONSE: "Write Response",
    ATT_WRITE_COMMAND: "Write Command",
    ATT_NOTIFICATION: "Handle Value Notification",
    ATT_INDICATION: "Handle Value Indication",
    0x1E: "Handle Value Confirmation",
}
PROXY_CONFIG_NAMES = {
    0x00: "Set Filter Type",
    0x01: "Add Addresses",
    0x02: "Remove Addresses",
    0x03: "Filter Status",
}
FILTER_TYPE_NAMES = {0x00: "whitelist", 0x01: "blacklist"}
PROVISIONING_NAMES = {  # PB-GATT PDU types (Mesh Profile §5.4.1); payloads stay hex (Data is encrypted anyway)
    0x00: "Invite",
    0x01: "Capabilities",
    0x02: "Start",
    0x03: "Public Key",
    0x04: "Input Complete",
    0x05: "Confirmation",
    0x06: "Random",
    0x07: "Data",
    0x08: "Complete",
    0x09: "Failed",
}

# pcap written by the nRF Sniffer extcap / `mesh_sniff.py capture --pcap`: LINKTYPE_NORDIC_BLE, protocol v3
PCAP_MAGIC_LE, PCAP_MAGIC_BE = 0xA1B2C3D4, 0xD4C3B2A1
PCAP_MAGIC_NS_LE, PCAP_MAGIC_NS_BE = 0xA1B23C4D, 0x4D3CB2A1
LINKTYPE_NORDIC_BLE = 272
NORDIC_PROTOVER = 3
NORDIC_EVENT_ADV_PDU = 0x02
NORDIC_BLE_HEADER_LEN = 10
ADV_PDU_TYPES_WITH_DATA = (
    0x0,
    0x2,
    0x6,
)  # ADV_IND, ADV_NONCONN_IND, ADV_SCAN_IND carry AdvData
BLE_ADDRESS_LEN = 6

COPY_MEMORY = (
    60.0  # seconds a network PDU stays known so its relay copies are recognised
)
SEGMENT_MEMORY = 30.0  # seconds an incomplete segmented message is kept
PRUNE_EVERY = 1000  # feeds between prunes of those two tables

DecodedKind = Literal[
    "access",  # a complete access message (unsegmented, or the last segment arrived)
    "copy",  # a relay / network-transmit copy of a network PDU already decoded
    "control",  # a lower-transport control message (segment ack, heartbeat, …)
    "segment",  # one segment of a message that is not complete yet
    "beacon",  # a Secure Network or Mesh Private beacon of our network
    "unprovisioned",  # an Unprovisioned Device beacon: a device waiting to be provisioned
    "undecryptable",  # our NID, but neither the NetKey nor the IV index opened it
    "malformed",  # authenticated, but not a PDU the transport / access layer can have produced (dropped)
    "foreign",  # another mesh network (NID / Network ID mismatch, a private beacon no key of ours opens)
    "pbadv",  # provisioning bearer traffic (not decoded)
    "proxy",  # a proxy configuration or PB-GATT provisioning PDU inside a followed connection
    "gatt",  # any other ATT traffic of a followed connection (discovery, MTU, write responses)
]


@dataclass(frozen=True)
class SniffRecord:
    """One mesh AD structure as the sniffer saw it: when, where, how strong, and the raw bytes.

    `kind` is the AD type (`AD_KINDS`), `pdu` its value: a Network PDU, a mesh beacon (type byte first) or a
    PB-ADV PDU. `adv` is the (random, rotating) advertising address of the transmitter, `ts_us` the sniffer's own
    microsecond clock when it has one, `count` how many byte-identical copies a capture-side dedupe folded in.
    """

    time: float
    channel: int
    rssi: int
    adv: str
    kind: str
    pdu: bytes
    ts_us: int | None = None
    count: int = 1
    direction: str | None = None  # followed connection: "m2s" (central → node) or "s2m"

    def to_json(self) -> str:
        """One NDJSON line; the field names are the capture format `tools/mesh_sniff.py` writes."""
        doc: dict[str, Any] = {
            "t": round(self.time, 6),
            "ch": self.channel,
            "rssi": self.rssi,
            "adv": self.adv,
            "kind": self.kind,
            "pdu": self.pdu.hex(),
        }
        if self.ts_us is not None:
            doc["ts_us"] = self.ts_us
        if self.count != 1:
            doc["n"] = self.count
        if self.direction is not None:
            doc["dir"] = self.direction
        return json.dumps(doc, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> SniffRecord:
        """Parse a capture line; `ValueError` when it is not one."""
        try:
            doc = json.loads(line)
            return cls(
                time=float(doc["t"]),
                channel=int(doc["ch"]),
                rssi=int(doc["rssi"]),
                adv=str(doc["adv"]),
                kind=str(doc["kind"]),
                pdu=bytes.fromhex(doc["pdu"]),
                ts_us=None if doc.get("ts_us") is None else int(doc["ts_us"]),
                count=int(doc.get("n", 1)),
                direction=None if doc.get("dir") is None else str(doc["dir"]),
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            AttributeError,
            OverflowError,  # a float field of 1e400: int(inf)
            RecursionError,  # nesting deeper than json.loads recurses
        ) as err:
            raise ValueError(f"not a sniffer record: {line[:80]!r}") from err


def ad_structures(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield (AD type, value) of the AD structures in `data`; stops at a zero or overrunning length (padding)."""
    i = 0
    while i + 1 < len(data):
        length = data[i]
        if length == 0 or i + 1 + length > len(data):
            return
        yield data[i + 1], data[i + 2 : i + 1 + length]
        i += 1 + length


def mesh_records(
    time: float,
    channel: int,
    rssi: int,
    adv: str,
    adv_data: bytes,
    ts_us: int | None = None,
) -> list[SniffRecord]:
    """Return the mesh AD structures of one advertisement as records (usually one, the bearer never packs two)."""
    return [
        SniffRecord(time, channel, rssi, adv, AD_KINDS[ad_type], value, ts_us)
        for ad_type, value in ad_structures(adv_data)
        if ad_type in AD_KINDS
    ]


def format_address(raw: bytes) -> str:
    """Advertising address as printed everywhere: most significant byte first, colon separated."""
    return ":".join(f"{b:02X}" for b in raw)


# ----------------------------------------------------------------------------- Nordic BLE pcap files


def read_pcap(data: bytes) -> Iterator[SniffRecord]:
    """Yield the mesh AD structures of a Nordic BLE pcap (what the nRF Sniffer extcap and `capture --pcap` write).

    Record layout (protocol version 3, `SnifferAPI/Packet.py`): board id, payload length (LE16), protocol version,
    packet counter (LE16), packet id, then for an advertising PDU the 10-byte BLE header (length, flags, channel,
    RSSI as a positive number, event counter, timestamp) and the packet as received: access address, PDU header,
    length, AdvA, AdvData. Packets whose CRC failed are skipped, so is every packet id but "advertising PDU".
    """
    if len(data) < 24:
        raise ValueError("not a pcap file")
    magic = struct.unpack("<L", data[:4])[0]
    if magic in (PCAP_MAGIC_LE, PCAP_MAGIC_NS_LE):
        order = "<"
    elif magic in (PCAP_MAGIC_BE, PCAP_MAGIC_NS_BE):
        order = ">"
    else:
        raise ValueError("not a pcap file")
    nanoseconds = magic in (PCAP_MAGIC_NS_LE, PCAP_MAGIC_NS_BE)
    linktype = struct.unpack(order + "L", data[20:24])[0]
    if linktype != LINKTYPE_NORDIC_BLE:
        raise ValueError(
            f"pcap link type {linktype} is not Nordic BLE ({LINKTYPE_NORDIC_BLE})"
        )
    pos = 24
    while pos + 16 <= len(data):
        seconds, fraction, incl_len, _orig = struct.unpack(
            order + "LLLL", data[pos : pos + 16]
        )
        packet = data[pos + 16 : pos + 16 + incl_len]
        pos += 16 + incl_len
        record = _nordic_record(
            seconds + fraction / (1e9 if nanoseconds else 1e6), packet
        )
        if record is not None:
            yield from record


def _nordic_record(time: float, packet: bytes) -> list[SniffRecord] | None:
    """Return the mesh records of one Nordic BLE pcap packet; None when it is not a good advertising PDU."""
    header = 1 + 6  # board id + sniffer header
    if len(packet) < header + NORDIC_BLE_HEADER_LEN:
        return None
    protover, packet_id = packet[3], packet[6]
    if protover != NORDIC_PROTOVER or packet_id != NORDIC_EVENT_ADV_PDU:
        return None
    ble = packet[header : header + NORDIC_BLE_HEADER_LEN]
    if (
        ble[0] != NORDIC_BLE_HEADER_LEN or not ble[1] & 0x01
    ):  # header length, CRC-OK flag
        return None
    channel, rssi = ble[2], -ble[3]
    ts_us = struct.unpack("<L", ble[6:10])[0]
    pdu = packet[header + NORDIC_BLE_HEADER_LEN + 4 :]  # after the access address
    if len(pdu) < 2:
        return None
    adv_type, length = pdu[0] & 0x0F, pdu[1]
    body = pdu[2 : 2 + length]
    if adv_type not in ADV_PDU_TYPES_WITH_DATA or len(body) < BLE_ADDRESS_LEN:
        return None
    adv = format_address(body[:BLE_ADDRESS_LEN][::-1])
    return mesh_records(time, channel, rssi, adv, body[BLE_ADDRESS_LEN:], ts_us)


# ----------------------------------------------------------------------------- decoding


@dataclass
class Decoded:
    """What one sniffer record turned out to be; `text` is the one-line human form the CLI prints."""

    record: SniffRecord
    kind: DecodedKind
    text: str
    message: AccessMessage | None = None
    beacon: SecureNetworkBeacon | None = None
    device: UnprovisionedDevice | None = None  # "unprovisioned": its UUID and OOB
    net: NetworkPDU | None = None
    copies: int = 1  # for "copy": how many copies of that PDU so far, this one included


@dataclass
class _Copies:
    first: float
    count: int
    label: str


@dataclass
class _Assembly:
    first: SniffRecord
    info: SegmentInfo
    parts: dict[int, bytes] = field(default_factory=dict)


class MeshDecoder:
    """Decrypt sniffer records with the keys of one export; the IV index follows the beacons it sees.

    `stats` counts records per `DecodedKind`, `sources` the unique access messages per source address.
    """

    def __init__(self, cdb: CDB, iv_index: int | None = None) -> None:
        """Take the keys from `cdb`; `iv_index` overrides the export's lower bound until a beacon says otherwise."""
        self.cdb = cdb
        self.iv_index = cdb.iv_index if iv_index is None else iv_index
        # every key the network may send with: mid key refresh the old one too (`CDB.rx_net_keys`)
        self._net_keys: list[NetKeyMaterial] = [
            nk for index in cdb.net_keys for nk in cdb.rx_net_keys(index)
        ]
        self._app_keys: dict[
            int, list[tuple[int, AppKeyMaterial]]
        ] = {}  # AID → (key index, key)
        for index, ak in cdb.app_keys.items():
            self._app_keys.setdefault(ak.aid, []).append((index, ak))
        self._dev_key_of_element = {
            e.address: node.dev_key for node in cdb.nodes for e in node.elements
        }
        self._copies: dict[tuple[int, int, int], _Copies] = {}
        self._segments: dict[tuple[int, int], _Assembly] = {}
        # (src, SeqAuth) -> when its message completed: later retransmissions of its segments are not a new one
        self._completed: dict[tuple[int, int], float] = {}
        self._proxy_sar: dict[
            str, ProxyReassembler
        ] = {}  # per direction of a followed connection
        self._feeds = 0
        self.stats: Counter[str] = Counter()
        self.sources: Counter[int] = Counter()

    # -- feeding
    def feed(self, record: SniffRecord) -> Decoded:
        """Decode one record."""
        self._feeds += 1
        if self._feeds % PRUNE_EVERY == 0:
            self._prune(record.time)
        decoded = self._decode(record)
        self.stats[decoded.kind] += 1
        return decoded

    def _decode(self, record: SniffRecord) -> Decoded:
        if record.kind == "beacon":
            return self._decode_beacon(record, record.pdu)
        if record.kind == "pbadv":
            return Decoded(record, "pbadv", f"PB-ADV {record.pdu.hex()}")
        if record.kind == GATT_KIND:
            return self._decode_gatt(record)
        return self._decode_network(record, record.pdu, "")

    def _decode_gatt(self, record: SniffRecord) -> Decoded:
        """Decode an ATT PDU of a followed connection.

        Proxy PDUs (writes to Data In, notifications from Data Out) are reassembled (proxy SAR) and decoded like
        on-air traffic, tagged `[gatt →node]` / `[gatt node→]`; every other ATT PDU is named. Only Write
        Commands and Notifications can carry them (§7.2.2.1: Data In supports Write Without Response alone,
        Data Out Notify alone), so a Write Request — a CCCD subscription during connection setup, say — is
        named like discovery traffic rather than fed to the reassembler as a bogus proxy PDU.
        """
        att = record.pdu
        if not att:
            return Decoded(record, "gatt", "ATT (empty)")
        op = att[0]
        if op in (ATT_WRITE_COMMAND, ATT_NOTIFICATION) and len(att) >= 4:
            handle = int.from_bytes(att[1:3], "little")
            side = "c2s" if op == ATT_WRITE_COMMAND else "s2c"
            done = self._proxy_sar.setdefault(side, ProxyReassembler()).feed(att[3:])
            if done is None:
                return Decoded(
                    record,
                    "gatt",
                    f"ATT {ATT_NAMES[op]} handle {handle:04X}: proxy PDU segment",
                )
            return self._decode_proxy_pdu(record, side, *done)
        name = ATT_NAMES.get(op, f"opcode {op:02X}")
        return Decoded(record, "gatt", f"ATT {name} {att[1:].hex()}".rstrip())

    def _decode_proxy_pdu(
        self, record: SniffRecord, side: str, msg_type: int, payload: bytes
    ) -> Decoded:
        tag = f"[gatt {'→node' if side == 'c2s' else 'node→'}]"
        if msg_type == PROXY_NETWORK_PDU:
            return self._decode_network(record, payload, tag + " ")
        if msg_type == PROXY_BEACON:
            decoded = self._decode_beacon(record, payload)
            decoded.text = f"{tag} {decoded.text}"
            return decoded
        if msg_type == PROXY_CONFIG:
            return Decoded(record, "proxy", f"{tag} {self._proxy_config_text(payload)}")
        if msg_type == PROXY_PROVISIONING:
            kind = (
                PROVISIONING_NAMES.get(payload[0], f"type {payload[0]:02X}")
                if payload
                else "(empty)"
            )
            return Decoded(
                record,
                "proxy",
                f"{tag} PB-GATT Provisioning {kind} {payload[1:].hex()}".rstrip(),
            )
        return Decoded(
            record, "gatt", f"{tag} proxy PDU type {msg_type} {payload.hex()}"
        )

    def _proxy_config_text(self, payload: bytes) -> str:
        """Decrypt a proxy configuration message (network nonce type 0x03) and name it."""
        net = next(
            (
                opened
                for nk in self._net_keys
                if payload
                and (payload[0] & 0x7F) == nk.nid
                and (opened := network_decrypt(nk, self.iv_index, payload, proxy=True))
                is not None
            ),
            None,
        )
        if net is None:
            return f"proxy configuration (undecryptable) {payload.hex()}"
        p = (
            net.transport_pdu
        )  # never empty: `network_decrypt` refuses a PDU without room for the opcode
        name = PROXY_CONFIG_NAMES.get(p[0], f"opcode {p[0]:02X}")
        head = f"{net.src:04X} proxy {name}"
        if p[0] == 0x00 and len(p) >= 2:
            return f"{head} {FILTER_TYPE_NAMES.get(p[1], p[1])}"
        if p[0] in (0x01, 0x02):
            addrs = ",".join(
                f"{int.from_bytes(p[i : i + 2], 'big'):04X}"
                for i in range(1, len(p) - 1, 2)
            )
            return f"{head} [{addrs}]"
        if p[0] == 0x03 and len(p) >= 4:
            return f"{head} {FILTER_TYPE_NAMES.get(p[1], p[1])} {int.from_bytes(p[2:4], 'big')} addresses"
        return f"{head} {p[1:].hex()}".rstrip()

    def _decode_network(self, record: SniffRecord, pdu: bytes, tag: str) -> Decoded:
        net, nk = self._open(pdu)
        if net is None:
            if nk is None:
                nid = pdu[0] & 0x7F if pdu else 0
                return Decoded(
                    record, "foreign", f"{tag}foreign network PDU (NID {nid:02X})"
                )
            return Decoded(
                record, "undecryptable", f"{tag}undecryptable network PDU {pdu.hex()}"
            )
        copy_key = (net.src, net.seq, net.ivi)
        known = self._copies.get(copy_key)
        if known is not None:
            known.count += 1
            return Decoded(
                record,
                "copy",
                f"copy {known.count} of {known.label}",
                net=net,
                copies=known.count,
            )
        route = f"{net.src:04X}→{net.dst:04X}"
        self._copies[copy_key] = _Copies(record.time, 1, f"{route} seq={net.seq:06X}")
        head = f"{tag}{route} ttl={net.ttl} seq={net.seq:06X}"
        try:
            return self._transport(record, net, head, tag)
        except (
            ValueError
        ) as e:  # authenticated, but not a PDU the transport layers can have produced
            return Decoded(record, "malformed", f"{head} malformed: {e}", net=net)

    def _transport(
        self, record: SniffRecord, net: NetworkPDU, head: str, tag: str
    ) -> Decoded:
        """Decode the lower transport PDU of an opened network PDU; ValueError when it is malformed."""
        kind = parse_lower(net.transport_pdu, net.ctl)
        if kind[0] == "ctl":
            return Decoded(
                record, "control", f"{head} {_control_text(kind[1], kind[2])}", net=net
            )
        if kind[0] == "segctl":
            return Decoded(record, "control", f"{head} segmented control PDU", net=net)
        if kind[0] == "unseg":
            _, akf, aid, upper = kind
            message = self._access(net, net.seq, akf, aid, upper, 0)
            if message is None:
                return Decoded(
                    record,
                    "undecryptable",
                    f"{head} undecryptable upper transport (akf={int(akf)} aid={aid})",
                    net=net,
                )
            return self._complete(record, net, message, tag)
        return self._segment(record, net, kind[1], tag)

    def _segment(
        self, record: SniffRecord, net: NetworkPDU, info: SegmentInfo, tag: str = ""
    ) -> Decoded:
        seq_auth = seq_auth_from(net.seq, info.seq_zero)
        head = f"{tag}{net.src:04X}→{net.dst:04X} ttl={net.ttl} seq={net.seq:06X}"
        if (
            info.seg_o > info.seg_n
        ):  # the same guards as `ProxyClient._on_segment`: the join would KeyError
            return Decoded(
                record,
                "segment",
                f"{head} segment {info.seg_o + 1}/{info.seg_n + 1}: SegO beyond SegN, dropped",
                net=net,
            )
        if (net.src, seq_auth) in self._completed:
            return Decoded(
                record,
                "segment",
                f"{head} segment {info.seg_o + 1}/{info.seg_n + 1}: retransmission of a completed message"
                f" (SeqAuth {seq_auth:06X})",
                net=net,
            )
        assembly = self._segments.setdefault(
            (net.src, seq_auth), _Assembly(record, info)
        )
        if info.seg_n != assembly.info.seg_n:
            return Decoded(
                record,
                "segment",
                f"{head} segment {info.seg_o + 1}/{info.seg_n + 1} contradicts the {assembly.info.seg_n + 1} segments announced, dropped",
                net=net,
            )
        assembly.parts[info.seg_o] = info.data
        if len(assembly.parts) <= info.seg_n:
            return Decoded(
                record,
                "segment",
                f"{head} segment {info.seg_o + 1}/{info.seg_n + 1} (SeqAuth {seq_auth:06X})",
                net=net,
            )
        del self._segments[(net.src, seq_auth)]
        self._completed[(net.src, seq_auth)] = (
            record.time
        )  # decrypted or not: it is not retried either
        upper = b"".join(assembly.parts[i] for i in range(info.seg_n + 1))
        message = self._access(net, seq_auth, info.akf, info.aid, upper, info.szmic)
        if message is None:
            return Decoded(
                record,
                "undecryptable",
                f"{head} undecryptable segmented message (akf={int(info.akf)} aid={info.aid})",
                net=net,
            )
        return self._complete(assembly.first, net, message, tag)

    def _complete(
        self,
        record: SniffRecord,
        net: NetworkPDU,
        message: AccessMessage,
        tag: str = "",
    ) -> Decoded:
        self.sources[message.src] += 1
        return Decoded(record, "access", f"{tag}{message}", message=message, net=net)

    def _open(self, pdu: bytes) -> tuple[NetworkPDU | None, NetKeyMaterial | None]:
        """Decrypt a network PDU with the NetKey whose NID matches; (None, None) when no key has that NID."""
        matched: NetKeyMaterial | None = None
        for nk in self._net_keys:
            if pdu and (pdu[0] & 0x7F) == nk.nid:
                matched = nk
                net = network_decrypt(nk, self.iv_index, pdu)
                if net is not None:
                    return net, nk
        return None, matched

    def _access(
        self,
        net: NetworkPDU,
        seq_auth: int,
        akf: bool,
        aid: int,
        upper: bytes,
        szmic: int,
    ) -> AccessMessage | None:
        iv = net.iv_index
        access: bytes | None = None
        key = ""
        if akf:
            for index, ak in self._app_keys.get(aid, []):
                access = upper_decrypt(
                    ak.key, NONCE_APP, iv, seq_auth, net.src, net.dst, upper, szmic
                )
                if access is not None:
                    key = f"app{index}"
                    break
        else:
            for addr in (net.src, net.dst):
                dk = self._dev_key_of_element.get(addr)
                if dk is None:
                    continue
                access = upper_decrypt(
                    dk, NONCE_DEVICE, iv, seq_auth, net.src, net.dst, upper, szmic
                )
                if access is not None:
                    key = f"dev:{addr:04X}"
                    break
        if access is None:
            return None
        op, cid, params = decode_opcode(access)
        return AccessMessage(
            net.src, net.dst, net.ttl, seq_auth, op, cid, params, access, key
        )

    def _decode_beacon(self, record: SniffRecord, payload: bytes) -> Decoded:
        """Name a mesh beacon by its type (§3.9.1): Unprovisioned Device, Secure Network or Mesh Private."""
        unprovisioned = parse_unprovisioned_beacon(payload)
        if unprovisioned is not None:
            device, uri_hash = unprovisioned
            oob = f" ({', '.join(device.oob_names)})" if device.oob_info else ""
            uri = f" uri_hash={uri_hash.hex()}" if uri_hash is not None else ""
            return Decoded(
                record,
                "unprovisioned",
                f"unprovisioned device beacon {device.uuid} oob={device.oob_info:04X}{oob}{uri}",
                device=device,
            )
        for nk in self._net_keys:
            beacon = parse_beacon(nk, payload) or parse_private_beacon(nk, payload)
            if beacon is None or beacon.network_id != nk.network_id:
                continue
            if beacon.authenticated and beacon.iv_index > self.iv_index:
                self.iv_index = beacon.iv_index
            flags = ",".join(
                name
                for name, on in (
                    ("key_refresh", beacon.key_refresh),
                    ("iv_update", beacon.iv_update),
                )
                if on
            )
            text = f"beacon iv={beacon.iv_index} flags={flags or '-'} auth={'ok' if beacon.authenticated else 'FAIL'}"
            return Decoded(
                record,
                "beacon",
                f"private {text}" if beacon.private else text,
                beacon=beacon,
            )
        if payload[:1] == bytes([BEACON_PRIVATE]):
            # no key of ours opens it, and nothing but its key tells which network sent it
            text = f"private beacon (unknown network) {payload.hex()}"
            return Decoded(record, "foreign", text)
        return Decoded(record, "foreign", f"foreign beacon {payload.hex()}")

    def _prune(self, now: float) -> None:
        self._copies = {
            k: v for k, v in self._copies.items() if now - v.first < COPY_MEMORY
        }
        self._segments = {
            k: v
            for k, v in self._segments.items()
            if now - v.first.time < SEGMENT_MEMORY
        }
        self._completed = {
            k: t for k, t in self._completed.items() if now - t < SEGMENT_MEMORY
        }


def _control_text(opcode: int, params: bytes) -> str:
    if opcode == 0x00 and len(params) >= 6:
        header = int.from_bytes(params[:2], "big")
        return (
            f"Segment Ack seq_zero={(header >> 2) & 0x1FFF} block={int.from_bytes(params[2:6], 'big'):08X}"
            + (" (on behalf)" if header & 0x8000 else "")
        )
    if opcode == 0x0A:
        return f"Heartbeat {params.hex()}"
    return f"control op {opcode:02X} {params.hex()}"
