"""Mesh PDU encoding/decoding: network, lower/upper transport, access opcodes, proxy protocol, beacons.

Only what a GATT-proxy client needs: unsegmented + segmented *receive*, unsegmented *send*,
segment acknowledgement, proxy configuration, secure network beacon.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag

from .crypto import (
    AppKeyMaterial,
    NetKeyMaterial,
    aes_cmac,
    aes_ecb,
    ccm_decrypt,
    ccm_encrypt,
)

__all__ = [
    "ALL_FRIENDS",
    "ALL_NODES",
    "ALL_PROXIES",
    "ALL_RELAYS",
    "BEACON_PRIVATE",
    "BEACON_SECURE",
    "BEACON_UNPROVISIONED",
    "FILTER_BLACKLIST",
    "FILTER_WHITELIST",
    "NET_HEADER_LEN",
    "NONCE_APP",
    "NONCE_DEVICE",
    "OPCODE_RFU",
    "PROXY_BEACON",
    "PROXY_CONFIG",
    "PROXY_NETWORK_PDU",
    "PROXY_PDU_MAX",
    "PROXY_PROVISIONING",
    "SEGMENTS_MAX",
    "SEGMENT_DATA_MAX",
    "UNASSIGNED",
    "UNSEGMENTED_UPPER_MAX",
    "UNSEGMENTED_UPPER_MIN",
    "NetworkPDU",
    "ProxyReassembler",
    "SecureNetworkBeacon",
    "SegmentInfo",
    "decode_opcode",
    "encode_opcode",
    "is_group",
    "is_unicast",
    "lower_segments_access",
    "lower_unsegmented_access",
    "network_decrypt",
    "network_encrypt",
    "parse_beacon",
    "parse_lower",
    "parse_private_beacon",
    "proxy_config_add_addresses",
    "proxy_config_set_filter",
    "proxy_frame",
    "segment_ack",
    "seq_auth_from",
    "upper_decrypt",
    "upper_encrypt",
    "upper_encrypt_app",
    "upper_encrypt_dev",
]

log = logging.getLogger("jhmesh")

UNASSIGNED = 0x0000
ALL_PROXIES = 0xFFFC
ALL_FRIENDS = 0xFFFD
ALL_RELAYS = 0xFFFE
ALL_NODES = 0xFFFF


def is_unicast(addr: int) -> bool:
    """Tell whether `addr` is a unicast address (0x0001-0x7FFF)."""
    return 0 < addr < 0x8000


def is_group(addr: int) -> bool:
    """Tell whether `addr` is a group address (0xC000 and above)."""
    return addr >= 0xC000


# ----------------------------------------------------------------------------- network layer


@dataclass
class NetworkPDU:
    """A decrypted Network PDU: header fields and the transport PDU it carries.

    `iv_index` is the index the IVI bit resolved to, the one the PDU authenticated under: the upper transport
    is decrypted, and the replay list keyed, under that same index (public: beacons carry it too).
    """

    ivi: int
    iv_index: int
    nid: int
    ctl: bool
    ttl: int
    seq: int
    src: int
    dst: int
    transport_pdu: bytes


def _net_nonce(kind: int, ctl_ttl: int, seq: int, src: int, iv_index: int) -> bytes:
    return (
        bytes([kind, ctl_ttl])
        + seq.to_bytes(3, "big")
        + src.to_bytes(2, "big")
        + b"\x00\x00"
        + iv_index.to_bytes(4, "big")
    )


def network_encrypt(
    nk: NetKeyMaterial,
    iv_index: int,
    ctl: bool,
    ttl: int,
    seq: int,
    src: int,
    dst: int,
    transport_pdu: bytes,
    proxy: bool = False,
) -> bytes:
    """Build an obfuscated, encrypted Network PDU (§3.4.4 / §3.8.7)."""
    ctl_ttl = (0x80 if ctl else 0) | (ttl & 0x7F)
    mic_len = 8 if ctl else 4
    nonce = _net_nonce(
        0x03 if proxy else 0x00, 0x00 if proxy else ctl_ttl, seq, src, iv_index
    )
    enc = ccm_encrypt(
        nk.enc_key, nonce, dst.to_bytes(2, "big") + transport_pdu, mic_len
    )
    header = bytes([ctl_ttl]) + seq.to_bytes(3, "big") + src.to_bytes(2, "big")
    pecb = aes_ecb(nk.priv_key, bytes(5) + iv_index.to_bytes(4, "big") + enc[:7])
    obfuscated = bytes(a ^ b for a, b in zip(header, pecb[:6], strict=True))
    return bytes([((iv_index & 1) << 7) | nk.nid]) + obfuscated + enc


NET_HEADER_LEN = (
    7  # IVI|NID octet + the six obfuscated header octets (CTL|TTL, SEQ, SRC)
)


def network_decrypt(
    nk: NetKeyMaterial, iv_index: int, pdu: bytes, proxy: bool = False
) -> NetworkPDU | None:
    """Decrypt a Network PDU. Tries iv_index and iv_index-1 according to the IVI bit.

    None when the PDU is not ours or not well formed: NID mismatch, NetMIC failure, too short for its CTL
    (§3.4.4: DST + at least one transport octet + a 32-bit NetMIC for access, 64-bit for control PDUs — so the
    caller always gets a non-empty `transport_pdu`), a non-unicast SRC (§3.4.3), or an unassigned DST outside
    the proxy configuration nonce (the only messages allowed to carry it, §6.5).
    """
    if len(pdu) < NET_HEADER_LEN + 2 + 1 + 4 or (pdu[0] & 0x7F) != nk.nid:
        return None
    ivi = pdu[0] >> 7
    iv = iv_index if (iv_index & 1) == ivi else iv_index - 1
    if iv < 0:
        return None
    pecb = aes_ecb(
        nk.priv_key,
        bytes(5) + iv.to_bytes(4, "big") + pdu[NET_HEADER_LEN : NET_HEADER_LEN + 7],
    )
    header = bytes(a ^ b for a, b in zip(pdu[1:7], pecb[:6], strict=True))
    ctl_ttl, seq, src = (
        header[0],
        int.from_bytes(header[1:4], "big"),
        int.from_bytes(header[4:6], "big"),
    )
    ctl = bool(ctl_ttl & 0x80)
    mic_len = 8 if ctl else 4
    if len(pdu) < NET_HEADER_LEN + 2 + 1 + mic_len or not is_unicast(src):
        return None
    nonce = _net_nonce(
        0x03 if proxy else 0x00, 0x00 if proxy else ctl_ttl, seq, src, iv
    )
    try:
        plain = ccm_decrypt(nk.enc_key, nonce, pdu[NET_HEADER_LEN:], mic_len)
    except InvalidTag:  # only an authentication failure is "not ours"; anything else is a bug and must surface
        return None
    dst = int.from_bytes(plain[:2], "big")
    if dst == UNASSIGNED and not proxy:
        return None
    return NetworkPDU(ivi, iv, nk.nid, ctl, ctl_ttl & 0x7F, seq, src, dst, plain[2:])


# ----------------------------------------------------------------------------- access opcodes


OPCODE_RFU = 0x7F  # the one 1-byte opcode value that is reserved (§3.7.3.1)


def encode_opcode(opcode: int, company_id: int | None = None) -> bytes:
    """Encode a 1-, 2- or (with `company_id`) 3-byte access opcode (§3.7.3.1).

    ValueError for anything the wire cannot carry unambiguously: a 1-byte opcode is 0x00-0x7E, a 2-byte one
    0x8000-0xBFFF (its first octet has the 10 bit prefix), a vendor opcode 0x00-0x3F with a 16-bit company id.
    Silently masking (the old behaviour) produced bytes that decode as a *different* message.
    """
    if company_id is not None:
        if not 0 <= opcode <= 0x3F:
            raise ValueError(f"vendor opcode {opcode:#x} is not 6-bit")
        if not 0 <= company_id <= 0xFFFF:
            raise ValueError(f"company id {company_id:#x} is not 16-bit")
        return bytes([0xC0 | opcode]) + company_id.to_bytes(2, "little")
    if 0 <= opcode < OPCODE_RFU:
        return bytes([opcode])
    if 0x8000 <= opcode <= 0xBFFF:
        return opcode.to_bytes(2, "big")
    raise ValueError(
        f"opcode {opcode:#x} is not a 1-byte (0x00-0x7E) or 2-byte (0x8000-0xBFFF) SIG opcode"
    )


def decode_opcode(access_pdu: bytes) -> tuple[int, int | None, bytes]:
    """Return (opcode, company_id or None, parameters).

    ValueError when the PDU is empty, ends inside its opcode or carries the reserved value 0x7F (§3.7.3.1):
    an access PDU that authenticated fine may still be malformed, and a truncated 2-/3-byte opcode would
    otherwise be read as a bogus opcode / company id.
    """
    if not access_pdu:
        raise ValueError("empty access PDU")
    b0 = access_pdu[0]
    if b0 < 0x80:
        if b0 == OPCODE_RFU:
            raise ValueError("opcode 0x7F is reserved for future use")
        return b0, None, access_pdu[1:]
    if b0 < 0xC0:
        if len(access_pdu) < 2:
            raise ValueError(f"2-byte opcode truncated: {access_pdu.hex()}")
        return int.from_bytes(access_pdu[:2], "big"), None, access_pdu[2:]
    if len(access_pdu) < 3:
        raise ValueError(f"vendor opcode truncated: {access_pdu.hex()}")
    return b0 & 0x3F, int.from_bytes(access_pdu[1:3], "little"), access_pdu[3:]


# ----------------------------------------------------------------------------- upper transport


def _app_nonce(
    kind: int, szmic: int, seq: int, src: int, dst: int, iv_index: int
) -> bytes:
    return (
        bytes([kind, szmic << 7])
        + seq.to_bytes(3, "big")
        + src.to_bytes(2, "big")
        + dst.to_bytes(2, "big")
        + iv_index.to_bytes(4, "big")
    )


NONCE_APP = 0x01  # application nonce (§3.8.5.3): AppKey-encrypted access messages
NONCE_DEVICE = 0x02  # device nonce (§3.8.5.2): DevKey-encrypted Config Server messages


def upper_encrypt(
    key: bytes,
    kind: int,
    iv_index: int,
    seq: int,
    src: int,
    dst: int,
    access_pdu: bytes,
    szmic: int = 0,
) -> bytes:
    """Encrypt an access PDU with an app (`kind` NONCE_APP) or device key (NONCE_DEVICE).

    The TransMIC is 4 bytes, or 8 when `szmic` is set. Inverse of `upper_decrypt`.
    """
    return ccm_encrypt(
        key,
        _app_nonce(kind, szmic, seq, src, dst, iv_index),
        access_pdu,
        8 if szmic else 4,
    )


def upper_encrypt_app(
    ak: AppKeyMaterial,
    iv_index: int,
    seq: int,
    src: int,
    dst: int,
    access_pdu: bytes,
    szmic: int = 0,
) -> bytes:
    """Encrypt an access PDU with the AppKey; the TransMIC is 4 bytes, or 8 when `szmic` is set."""
    return upper_encrypt(ak.key, NONCE_APP, iv_index, seq, src, dst, access_pdu, szmic)


def upper_encrypt_dev(
    dev_key: bytes,
    iv_index: int,
    seq: int,
    src: int,
    dst: int,
    access_pdu: bytes,
    szmic: int = 0,
) -> bytes:
    """Encrypt an access PDU with a node's device key (device nonce, for Config Server messages)."""
    return upper_encrypt(
        dev_key, NONCE_DEVICE, iv_index, seq, src, dst, access_pdu, szmic
    )


def upper_decrypt(
    key: bytes,
    kind: int,
    iv_index: int,
    seq_auth: int,
    src: int,
    dst: int,
    payload: bytes,
    szmic: int,
) -> bytes | None:
    """Decrypt an upper transport PDU with an app or device key; None when the MIC does not verify.

    Only the authentication failure is swallowed: a nonce field out of range (a negative SeqAuth, say) or a
    key of the wrong size is a programming error and raises, rather than posing as "undecryptable" — which the
    application reads as "the export's keys are stale".
    """
    try:
        return ccm_decrypt(
            key,
            _app_nonce(kind, szmic, seq_auth, src, dst, iv_index),
            payload,
            8 if szmic else 4,
        )
    except InvalidTag:
        return None


# ----------------------------------------------------------------------------- lower transport


@dataclass
class SegmentInfo:
    """The header fields of one segment of a segmented access message."""

    akf: bool
    aid: int
    szmic: int
    seq_zero: int
    seg_o: int
    seg_n: int
    data: bytes


def _access_header(seg: bool, akf: bool, aid: int) -> int:
    """First octet of an access lower transport PDU: SEG | AKF | AID (§3.5.2.1/§3.5.2.2)."""
    return (0x80 if seg else 0) | (0x40 if akf else 0) | (aid & 0x3F)


UNSEGMENTED_UPPER_MAX = 15  # upper transport PDU incl. TransMIC that fits one unsegmented access PDU (§3.5.2.1)
UNSEGMENTED_UPPER_MIN = 5  # 1 access octet + the 32-bit TransMIC (§3.5.2.1)
SEGMENT_DATA_MAX = (
    12  # segment payload (§3.5.2.2: 96 bits for every segment but the last)
)
SEGMENTS_MAX = 32  # SegN is 5 bits


def lower_unsegmented_access(aid: int, upper_pdu: bytes, akf: bool = True) -> bytes:
    """Wrap an upper transport PDU (up to 15 bytes) as an unsegmented access lower transport PDU.

    `akf` False marks a device-key message (AID must then be 0). ValueError when it does not fit.
    """
    if len(upper_pdu) > UNSEGMENTED_UPPER_MAX:
        raise ValueError(
            f"unsegmented access PDU carries at most {UNSEGMENTED_UPPER_MAX} bytes incl. TransMIC, not {len(upper_pdu)}"
        )
    return bytes([_access_header(False, akf, aid)]) + upper_pdu


def lower_segments_access(
    aid: int,
    seq0: int,
    upper_pdu: bytes,
    seg_size: int = 12,
    szmic: int = 0,
    akf: bool = True,
) -> list[bytes]:
    """Split an upper transport PDU into segmented lower transport PDUs (§3.5.2.2). SeqZero = seq0 & 0x1FFF.

    ValueError when more than 32 segments would be needed: SegN is a 5-bit field, so a longer message cannot
    be sent as one segmented message at all (the field would wrap and every receiver mis-assemble it).
    """
    parts = [upper_pdu[i : i + seg_size] for i in range(0, len(upper_pdu), seg_size)]
    seg_n = len(parts) - 1
    if seg_n >= SEGMENTS_MAX:
        raise ValueError(
            f"access PDU of {len(upper_pdu)} bytes needs {seg_n + 1} segments, more than the {SEGMENTS_MAX} one message can carry"
        )
    out = []
    for seg_o, part in enumerate(parts):
        hdr = (szmic << 23) | ((seq0 & 0x1FFF) << 10) | (seg_o << 5) | seg_n
        out.append(
            bytes([_access_header(True, akf, aid)]) + hdr.to_bytes(3, "big") + part
        )
    return out


def parse_lower(transport_pdu: bytes, ctl: bool) -> tuple[Any, ...]:
    """Return ('ctl', opcode, params) | ('segctl', None) | ('unseg', akf, aid, upper) | ('seg', SegmentInfo).

    ValueError for a PDU the lower transport layer cannot have produced: empty, an unsegmented access PDU with
    no room for an access octet and its TransMIC, a segmented access PDU shorter than its 4-byte header plus one
    data byte, or segment data that is not 12 bytes on a segment other than the last (§3.5.2.2) — a truncated header would otherwise parse as a "complete" one-segment message that the
    client acknowledges and counts as undecryptable.
    """
    if not transport_pdu:
        raise ValueError("empty lower transport PDU")
    b0 = transport_pdu[0]
    seg = bool(b0 & 0x80)
    if ctl:
        if seg:
            return ("segctl", None)
        return ("ctl", b0 & 0x7F, transport_pdu[1:])
    akf, aid = bool(b0 & 0x40), b0 & 0x3F
    if not seg:
        if len(transport_pdu) - 1 < UNSEGMENTED_UPPER_MIN:
            raise ValueError(
                f"unsegmented access PDU carries {len(transport_pdu) - 1} bytes, fewer than {UNSEGMENTED_UPPER_MIN}"
            )
        return ("unseg", akf, aid, transport_pdu[1:])
    if len(transport_pdu) < 5:
        raise ValueError(f"segmented access PDU truncated: {transport_pdu.hex()}")
    hdr = int.from_bytes(transport_pdu[1:4], "big")
    seg_o, seg_n, data = (hdr >> 5) & 0x1F, hdr & 0x1F, transport_pdu[4:]
    if len(data) > SEGMENT_DATA_MAX or (
        seg_o < seg_n and len(data) != SEGMENT_DATA_MAX
    ):
        raise ValueError(
            f"segment {seg_o} of {seg_n + 1} carries {len(data)} bytes, not {SEGMENT_DATA_MAX}"
        )
    return (
        "seg",
        SegmentInfo(
            akf, aid, (hdr >> 23) & 1, (hdr >> 10) & 0x1FFF, seg_o, seg_n, data
        ),
    )


def segment_ack(seq_zero: int, block_ack: int, obo: bool = False) -> bytes:
    """Lower transport control PDU 'Segment Acknowledgment' (§3.5.3.3), opcode 0x00."""
    hdr = ((1 if obo else 0) << 15) | ((seq_zero & 0x1FFF) << 2)
    return bytes([0x00]) + hdr.to_bytes(2, "big") + block_ack.to_bytes(4, "big")


def seq_auth_from(seq: int, seq_zero: int) -> int:
    """Reconstruct the SeqAuth (first-segment sequence number) from a segment's own seq and SeqZero.

    ValueError when no first segment can have had that number: SeqZero above `seq` with `seq` still below
    0x2000 would place it before the IV index began (§3.5.3.1: every segment of a message is sent under one IV
    index, and SEQ starts at 0 there). The value would be negative — and land in a nonce, a cache key and the
    replay list.
    """
    sa = (seq & ~0x1FFF) | seq_zero
    if sa > seq:
        sa -= 0x2000
    if sa < 0:
        raise ValueError(
            f"SeqZero {seq_zero:#06x} lies before sequence number {seq:#08x} under this IV index"
        )
    return sa


# ----------------------------------------------------------------------------- proxy protocol (§6)

PROXY_NETWORK_PDU = 0x00
PROXY_BEACON = 0x01
PROXY_CONFIG = 0x02
PROXY_PROVISIONING = 0x03

FILTER_WHITELIST = 0x00
FILTER_BLACKLIST = 0x01


def proxy_frame(msg_type: int, payload: bytes, mtu: int) -> list[bytes]:
    """Split into Proxy PDUs with SAR bits (§6.3.1)."""
    chunk = max(1, mtu - 1)
    if len(payload) <= chunk:
        return [bytes([msg_type]) + payload]
    parts = [payload[i : i + chunk] for i in range(0, len(payload), chunk)]
    out = []
    for i, p in enumerate(parts):
        sar = 0x40 if i == 0 else (0xC0 if i == len(parts) - 1 else 0x80)
        out.append(bytes([sar | msg_type]) + p)
    return out


PROXY_PDU_MAX = 128  # the longest legitimate proxy PDU payload is a 66-byte provisioning PDU; beyond this is garbage


class ProxyReassembler:
    """Reassemble proxy PDUs split by SAR across several GATT notifications.

    Everything here runs *before* any authentication (a rogue proxy only needs the public Network ID to be
    connected to), so the per-type buffer is bounded by `max_len`: a stream of continuation frames that never
    ends is dropped and counted in `dropped` rather than growing without limit, and a new first frame always
    starts over. A continuation or last frame with no first one before it (Mesh Profile §6.3.1 allows them only
    after a first) — including the rest of a PDU just dropped for its size — is counted in `orphaned` and dropped.
    """

    def __init__(self, max_len: int = PROXY_PDU_MAX) -> None:
        """Start with nothing buffered."""
        self._buf: dict[int, bytearray] = {}
        self.max_len = max_len
        self.dropped = 0  # oversize PDUs discarded — a link-quality / rogue-proxy signal for the application
        self.orphaned = 0  # continuation / last frames that arrived without a first one

    def feed(self, pdu: bytes) -> tuple[int, bytes] | None:
        """Add one notification; return (message type, payload) once a whole proxy PDU is complete.

        None while a PDU is still being assembled — and for a PDU that outgrew `max_len`, which is dropped.
        An empty notification carries no SAR/type octet and is ignored.
        """
        if not pdu:
            return None
        sar, t = pdu[0] >> 6, pdu[0] & 0x3F
        if sar == 0:
            if len(pdu) - 1 > self.max_len:
                self._drop(t)
                return None
            return t, bytes(pdu[1:])
        if sar == 1:
            self._buf[t] = bytearray()
        buf = self._buf.get(t)
        if buf is None or len(buf) + len(pdu) - 1 > self.max_len:
            self._reject(t, orphan=buf is None)
            return None
        buf += pdu[1:]
        if sar == 3:
            del self._buf[t]
            return t, bytes(buf)
        return None

    def _reject(self, t: int, *, orphan: bool) -> None:
        """Discard a frame and what it belongs to.

        An orphan is a continuation / last frame without a first (or the rest of a PDU dropped for its size):
        nothing is buffered for it. Otherwise the frame made its PDU outgrow `max_len`, and the whole PDU goes.
        """
        if orphan:
            self.orphaned += 1
            log.debug("proxy PDU segment of type %d without a first segment dropped", t)
            return
        del self._buf[t]
        self._drop(t)

    def _drop(self, t: int) -> None:
        self.dropped += 1
        log.debug(
            "proxy PDU of type %d longer than %d bytes dropped (%d so far)",
            t,
            self.max_len,
            self.dropped,
        )


def proxy_config_set_filter(filter_type: int) -> bytes:
    """Build the proxy configuration Set Filter Type message."""
    return bytes([0x00, filter_type])


def proxy_config_add_addresses(addresses: list[int]) -> bytes:
    """Build the proxy configuration Add Addresses To Filter message."""
    return bytes([0x01]) + b"".join(a.to_bytes(2, "big") for a in addresses)


# ----------------------------------------------------------------------------- beacons (§3.9)


# beacon types (§3.9.1; Mesh Protocol 1.1 §3.10.1 adds the Mesh Private beacon)
BEACON_UNPROVISIONED, BEACON_SECURE, BEACON_PRIVATE = 0x00, 0x01, 0x02


@dataclass
class SecureNetworkBeacon:
    """A parsed Secure Network Beacon; `authenticated` says whether its MAC verified under our beacon key.

    `private`: an opened Mesh Private beacon instead (`parse_private_beacon`), which carries the same flags and IV
    index but no Network ID in the clear; `network_id` is then that of the key that opened it.
    """

    key_refresh: bool
    iv_update: bool
    network_id: bytes
    iv_index: int
    authenticated: bool
    private: bool = False


def parse_beacon(nk: NetKeyMaterial, payload: bytes) -> SecureNetworkBeacon | None:
    """Parse a Secure Network Beacon payload; None when it is not one."""
    if not payload or payload[0] != BEACON_SECURE or len(payload) < 22:
        return None
    flags, net_id, iv, auth = (
        payload[1],
        payload[2:10],
        int.from_bytes(payload[10:14], "big"),
        payload[14:22],
    )
    ok = hmac.compare_digest(
        aes_cmac(nk.beacon_key, payload[1:14])[:8], bytes(auth)
    )  # a MAC is compared in constant time, like the CCM tags inside `cryptography`
    return SecureNetworkBeacon(bool(flags & 1), bool(flags & 2), net_id, iv, ok)


def parse_private_beacon(
    nk: NetKeyMaterial, payload: bytes
) -> SecureNetworkBeacon | None:
    """Open a Mesh Private beacon (Mesh Protocol 1.1 §3.10.4) with `nk`'s private beacon key; None when it is not one.

    Random (13) || obfuscated Flags + IV Index (5) || Authentication Tag (8): the obfuscation and the tag are AES-CCM
    with the Random as nonce, so only the key it was made with opens it (a beacon of another network, or one of
    ours changed on the way, is None, not an unauthenticated beacon: nothing in it is readable without the key).
    """
    if not payload or payload[0] != BEACON_PRIVATE or len(payload) < 27:
        return None
    try:
        data = ccm_decrypt(nk.private_beacon_key, payload[1:14], payload[14:27], 8)
    except InvalidTag:
        return None
    flags, iv = data[0], int.from_bytes(data[1:5], "big")
    return SecureNetworkBeacon(
        bool(flags & 1), bool(flags & 2), nk.network_id, iv, True, private=True
    )
