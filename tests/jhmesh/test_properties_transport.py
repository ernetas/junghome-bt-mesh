"""Property tests (Hypothesis) of the transport: segmentation → reassembly, proxy SAR at every MTU, the crypto layers.

- Lower transport: a segmented access message (any payload up to the 32-segment limit, SZMIC 0 or 1, AppKey or
  device key, to our unicast or to a group, under the current or the previous IV index), its segments sent in any
  order, duplicated, lost and retransmitted with fresh sequence numbers, possibly interleaved with a second
  message from another node, is delivered by `ProxyClient` exactly once, intact, the moment its last missing
  segment arrives, and (to our unicast) acknowledged in full; the replay list ends at the completing segment.
- Proxy SAR: at every ATT MTU from 23 to 517 a proxy PDU is cut into frames that fit the MTU and put back together
  by `ProxyReassembler` (both directions of `ProxyClient` included), whatever the PDU type and length.
- Network / upper transport: encrypt → decrypt is the identity under both IVI resolutions and both nonces, and a
  single flipped bit anywhere makes the PDU undecryptable; opcodes, lower transport headers and SeqAuth
  reconstruction round-trip; beacons authenticate exactly when untouched.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from jhmesh import client as client_mod
from jhmesh.cdb import CDB
from jhmesh.client import SEQ_MAX, AccessMessage, LocalState, ProxyClient
from jhmesh.crypto import NetKeyMaterial, aes_cmac
from jhmesh.pdu import (
    NONCE_APP,
    NONCE_DEVICE,
    PROXY_BEACON,
    PROXY_CONFIG,
    PROXY_NETWORK_PDU,
    PROXY_PDU_MAX,
    PROXY_PROVISIONING,
    SEGMENTS_MAX,
    NetworkPDU,
    ProxyReassembler,
    SegmentInfo,
    decode_opcode,
    encode_opcode,
    lower_segments_access,
    lower_unsegmented_access,
    network_decrypt,
    network_encrypt,
    parse_beacon,
    parse_lower,
    proxy_frame,
    seq_auth_from,
    upper_decrypt,
    upper_encrypt,
)

from .conftest import (
    CDB_PATH,
    GROUP_WC,
    LIGHT_2G,
    OUR_SRC,
    PROXY_NODE,
    SOCKET,
    FakeBleak,
    FastAsyncio,
)

# BLE's default ATT MTU and the largest one ATT allows
ATT_MTU_MIN, ATT_MTU_MAX = 23, 517
# read-only here: every example builds its own client and link on it
CDB_FIXTURE = CDB.load(CDB_PATH)

keys16 = st.binary(min_size=16, max_size=16)
unicast = st.integers(0x0001, 0x7FFF)
iv_indexes = st.integers(1, 0xFFFFFFFF)  # >= 1 so that "the previous index" exists


@st.composite
def access_pdus(draw: st.DrawFn, max_size: int) -> bytes:
    """A well-formed access PDU (1-, 2- or 3-byte opcode, then parameters) of at most `max_size` bytes."""
    kind = draw(st.sampled_from(("sig1", "sig2", "vendor")))
    if kind == "sig1":
        op = encode_opcode(draw(st.integers(0x00, 0x7E)))
    elif kind == "sig2":
        op = encode_opcode(draw(st.integers(0x8000, 0xBFFF)))
    else:
        op = encode_opcode(draw(st.integers(0, 0x3F)), draw(st.integers(0, 0xFFFF)))
    return op + draw(st.binary(max_size=max_size - len(op)))


# ============================================================================= lower transport reassembly


@dataclass
class Outgoing:
    """One segmented message as a node sends it: what it carries and every network PDU transmitted for it."""

    src: int
    dst: int
    iv_index: int
    seq0: int  # SeqAuth: the first transmission (of segment 0) carries it; SeqZero is its low 13 bits
    access: bytes
    key: str  # what `AccessMessage.key` must say
    segments: list[bytes]  # the lower transport PDUs
    # (segment index, network seq, network PDU) of every transmission, in the order the sender made them
    transmissions: list[tuple[int, int, bytes]]

    def transmit(self, i: int) -> int:
        """Send segment `i` once more, with the sender's next sequence number; return its transmission index."""
        seq = self.transmissions[-1][1] + 1 if self.transmissions else self.seq0
        pdu = network_encrypt(
            CDB_FIXTURE.net_keys[0],
            self.iv_index,
            False,
            3,
            seq,
            self.src,
            self.dst,
            self.segments[i],
        )
        self.transmissions.append((i, seq, pdu))
        return len(self.transmissions) - 1


@st.composite
def segmented_messages(draw: st.DrawFn, src: int, iv_index: int) -> Outgoing:
    """A segmented access message from `src` and the transmissions a sender with retransmissions makes for it."""
    ak = CDB_FIXTURE.app_keys[0]
    szmic = draw(st.integers(0, 1))
    mic = 8 if szmic else 4
    devkey = draw(st.booleans())
    dst = OUR_SRC if devkey else draw(st.sampled_from((OUR_SRC, GROUP_WC)))
    access = draw(access_pdus(SEGMENTS_MAX * 12 - mic))
    seq0 = draw(st.integers(0, SEQ_MAX - 4 * SEGMENTS_MAX))
    node = CDB_FIXTURE.node_by_addr(src)
    assert node is not None
    if devkey:
        upper = upper_encrypt(
            node.dev_key, NONCE_DEVICE, iv_index, seq0, src, dst, access, szmic
        )
        segs = lower_segments_access(0, seq0, upper, szmic=szmic, akf=False)
        key = f"dev:{src:04X}"
    else:
        upper = upper_encrypt(
            ak.key, NONCE_APP, iv_index, seq0, src, dst, access, szmic
        )
        segs = lower_segments_access(ak.aid, seq0, upper, szmic=szmic)
        key = "app0"
    msg = Outgoing(src, dst, iv_index, seq0, access, key, segs, [])
    # the sender's first round (segment 0 first: it carries SeqAuth itself), then retransmission rounds of whatever
    # it chooses, each transmission with a fresh number as a real sender takes them
    order = list(range(len(segs)))
    rounds = [
        order,
        *draw(
            st.lists(st.lists(st.sampled_from(order), max_size=len(segs)), max_size=2)
        ),
    ]
    for i in (i for r in rounds for i in r):
        msg.transmit(i)
    return msg


@st.composite
def air_schedules(draw: st.DrawFn, messages: list[Outgoing]) -> list[tuple[int, int]]:
    """What reaches the client: (message, transmission) pairs, reordered, duplicated and lossy across messages.

    A segment whose every transmission was lost is retransmitted at the end (the sender keeps going until it is
    acknowledged), so every message completes eventually.
    """
    events = [
        (m, t) for m, msg in enumerate(messages) for t in range(len(msg.transmissions))
    ]
    events = draw(st.permutations(events))
    lost = draw(st.sets(st.sampled_from(events)))
    duplicated = draw(st.lists(st.sampled_from(events), max_size=4))
    schedule = [e for e in events if e not in lost]
    for e in duplicated:
        schedule.insert(draw(st.integers(0, len(schedule))), e)
    for m, msg in enumerate(messages):
        heard = {msg.transmissions[t][0] for mm, t in schedule if mm == m}
        schedule += [
            (m, msg.transmit(i)) for i in range(len(msg.segments)) if i not in heard
        ]
    return schedule


class _ErrorLog(logging.Handler):
    """Collects what the library logs at ERROR: the notification handler turns any exception into such a line."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def _no_errors_logged() -> Iterator[None]:
    handler = _ErrorLog()
    logging.getLogger("jhmesh").addHandler(handler)
    try:
        yield
    finally:
        logging.getLogger("jhmesh").removeHandler(handler)
    assert [r.getMessage() for r in handler.records] == []


async def _settle(client: ProxyClient) -> None:
    """Let the client's background work (segment acks: two frames each at a small MTU) run to its end."""
    for _ in range(10_000):
        if not client._tasks:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail(f"background tasks still running: {client._tasks}")
    await asyncio.sleep(0)


@given(
    data=st.data(),
    two=st.booleans(),
    previous_iv=st.booleans(),
    mtu=st.integers(ATT_MTU_MIN, ATT_MTU_MAX),
)
def test_segmented_messages_are_reassembled_once_whatever_the_air_does(
    data: st.DataObject, two: bool, previous_iv: bool, mtu: int
) -> None:
    state_iv = data.draw(iv_indexes)
    iv_index = state_iv - 1 if previous_iv else state_iv
    sources = (PROXY_NODE, LIGHT_2G) if two else (PROXY_NODE,)
    messages = [data.draw(segmented_messages(src, iv_index)) for src in sources]
    schedule = data.draw(air_schedules(messages))

    async def scenario() -> None:
        got: list[AccessMessage] = []
        state = LocalState(None, OUR_SRC)
        state.iv_index = state_iv
        client = ProxyClient(CDB_FIXTURE, state, on_message=got.append)
        link = FakeBleak(CDB_FIXTURE, mtu_size=mtu)
        link.iv_index = state_iv
        await client.attach(link, filter_blacklist=False)
        seen: list[set[int]] = [set() for _ in messages]
        completed_by: list[int | None] = [None] * len(messages)
        # the Segment Acks §3.5.3.4 asks of a receiver: the full block on completion and for every segment heard
        # after it (the sender missed the ack; its 150 ms limit is off here). The acknowledgment timer of a partial
        # block never fires: the segments arrive without the loop running in between
        expected_acks: list[list[int]] = [[] for _ in messages]
        for m, t in schedule:
            msg = messages[m]
            i, seq, pdu = msg.transmissions[t]
            link.deliver(PROXY_NETWORK_PDU, pdu)
            done_before = completed_by[m] is not None
            seen[m].add(i)
            if completed_by[m] is None and len(seen[m]) == len(msg.segments):
                completed_by[m] = seq
            if done_before or completed_by[m] is not None:
                expected_acks[m].append(sum(1 << j for j in seen[m]))
            delivered = [g for g in got if g.src == msg.src]
            assert len(delivered) == (1 if completed_by[m] is not None else 0)
        await _settle(client)
        assert client.rx_undecryptable == 0
        for m, msg in enumerate(messages):
            (delivered,) = [g for g in got if g.src == msg.src]
            op, cid, params = decode_opcode(msg.access)
            assert (
                delivered.src,
                delivered.dst,
                delivered.access_pdu,
                delivered.key,
            ) == (
                msg.src,
                msg.dst,
                msg.access,
                msg.key,
            )
            assert (delivered.opcode, delivered.company_id, delivered.params) == (
                op,
                cid,
                params,
            )
            # the network number of the segment that completed it
            assert delivered.seq == completed_by[m]
            acks = [(a[2], a[3]) for a in link.sent_acks() if a[1] == msg.src]
            if msg.dst == OUR_SRC:
                assert acks == [
                    (msg.seq0 & 0x1FFF, block) for block in expected_acks[m]
                ]
            else:
                assert acks == []  # group traffic is never acknowledged
            # the replay list holds the completing number: nothing sent before it can be accepted again
            assert state.rpl[msg.src] == (iv_index, completed_by[m])
        assert len(got) == len(messages)
        await client.detach()

    with _no_errors_logged(), patch.object(client_mod, "SAR_ACK_DELAY_INCREMENT", 0):
        asyncio.run(scenario())


# ============================================================================= proxy SAR at every MTU


def _check_frames(msg_type: int, payload: bytes, att_mtu: int) -> list[bytes]:
    frames = proxy_frame(msg_type, payload, att_mtu - 3)
    # a write without response carries ATT_MTU - 3 bytes
    assert all(len(f) <= att_mtu - 3 for f in frames)
    assert all(f[0] & 0x3F == msg_type for f in frames)
    sar = [f[0] >> 6 for f in frames]
    if len(payload) + 1 <= att_mtu - 3:
        assert sar == [0]  # fits one frame: complete message, never split
    else:
        assert sar == [1] + [2] * (len(frames) - 2) + [3]
        # only the last frame may be short
        assert all(len(f) == att_mtu - 3 for f in frames[:-1])
        assert len(frames[-1]) > 1  # ... and never empty
    assert b"".join(f[1:] for f in frames) == payload
    return frames


def test_proxy_sar_round_trips_at_every_mtu_and_length() -> None:
    """Exhaustive over what the client can meet: every ATT MTU, every length up to the reassembler's bound."""
    for att_mtu in range(ATT_MTU_MIN, ATT_MTU_MAX + 1):
        reasm = ProxyReassembler()
        for length in range(PROXY_PDU_MAX + 1):
            payload = bytes((length + i) & 0xFF for i in range(length))
            frames = _check_frames(PROXY_NETWORK_PDU, payload, att_mtu)
            results = [reasm.feed(f) for f in frames]
            assert results[:-1] == [None] * (len(frames) - 1)
            assert results[-1] == (PROXY_NETWORK_PDU, payload)
        assert (reasm.dropped, reasm.orphaned) == (0, 0)


@given(
    att_mtu=st.integers(ATT_MTU_MIN, ATT_MTU_MAX),
    pdus=st.lists(
        st.tuples(
            st.sampled_from(
                (PROXY_NETWORK_PDU, PROXY_BEACON, PROXY_CONFIG, PROXY_PROVISIONING)
            ),
            st.binary(max_size=2 * PROXY_PDU_MAX),
        ),
        min_size=1,
        max_size=6,
    ),
)
def test_proxy_reassembler_recovers_every_pdu_of_a_stream(
    att_mtu: int, pdus: list[tuple[int, bytes]]
) -> None:
    """Back-to-back PDUs of any type: each fitting one comes out whole, in order; an oversize one is dropped and
    counted, and costs nothing of what follows."""
    reasm = ProxyReassembler()
    out = [
        r
        for t, p in pdus
        for f in _check_frames(t, p, att_mtu)
        if (r := reasm.feed(f)) is not None
    ]
    assert out == [(t, p) for t, p in pdus if len(p) <= PROXY_PDU_MAX]
    assert reasm.dropped == sum(len(p) > PROXY_PDU_MAX for _, p in pdus)


@given(
    # small MTUs: the only ones where PDUs of two types can interleave
    att_mtu=st.integers(ATT_MTU_MIN, 40),
    first=st.binary(min_size=60, max_size=PROXY_PDU_MAX),
    second=st.binary(max_size=PROXY_PDU_MAX),
    data=st.data(),
)
def test_proxy_reassembler_keeps_types_apart(
    att_mtu: int, first: bytes, second: bytes, data: st.DataObject
) -> None:
    """Frames of a Network PDU and of a proxy configuration PDU interleaved: each type is assembled on its own."""
    a = proxy_frame(PROXY_NETWORK_PDU, first, att_mtu - 3)
    b = proxy_frame(PROXY_CONFIG, second, att_mtu - 3)
    cut = data.draw(st.integers(1, len(a) - 1))
    reasm = ProxyReassembler()
    out = [r for f in (*a[:cut], *b, *a[cut:]) if (r := reasm.feed(f)) is not None]
    assert out == [(PROXY_CONFIG, second), (PROXY_NETWORK_PDU, first)]


@given(
    att_mtu=st.integers(ATT_MTU_MIN, ATT_MTU_MAX),
    msg_type=st.sampled_from((PROXY_PROVISIONING, 0x3E)),
    payload=st.binary(max_size=PROXY_PDU_MAX),
)
def test_client_writes_and_reads_proxy_pdus_at_every_mtu(
    att_mtu: int, msg_type: int, payload: bytes
) -> None:
    """`ProxyClient` cuts what it writes to the link's MTU (the fake proxy reassembles it), and puts together what
    the proxy sends it cut to the same MTU (an unknown type is logged, a beacon parsed: nothing is lost)."""

    async def scenario() -> None:
        client = ProxyClient(CDB_FIXTURE, LocalState(None, OUR_SRC))
        link = FakeBleak(CDB_FIXTURE, mtu_size=att_mtu)
        await client.attach(link, filter_blacklist=False)
        await client._write(msg_type, payload)
        assert all(len(w) <= att_mtu - 3 for w in link.writes)
        assert link.outgoing == [(msg_type, payload)]
        seen: list[tuple[int, bytes]] = []
        with patch.object(
            client, "_on_network_pdu", lambda p: seen.append((PROXY_NETWORK_PDU, p))
        ):
            link.deliver(PROXY_NETWORK_PDU, payload)
        assert seen == [(PROXY_NETWORK_PDU, payload)]
        assert client._reasm._buf == {}  # nothing half-assembled left behind
        await client.detach()

    asyncio.run(scenario())


@given(
    att_mtu=st.integers(ATT_MTU_MIN, ATT_MTU_MAX),
    dst=st.sampled_from((PROXY_NODE, SOCKET, GROUP_WC)),
    access=access_pdus(SEGMENTS_MAX * 12 - 4),
)
def test_client_access_messages_reach_the_proxy_intact_at_every_mtu(
    att_mtu: int, dst: int, access: bytes
) -> None:
    """`send_access` end to end at any MTU: unsegmented or segmented (acknowledged by the fake node, or sent twice
    to a group), the fake proxy reassembles and decrypts exactly the access PDU that was sent."""

    async def scenario() -> None:
        client = ProxyClient(CDB_FIXTURE, LocalState(None, OUR_SRC), ttl=4)
        link = FakeBleak(CDB_FIXTURE, mtu_size=att_mtu)
        link.auto_ack()
        await client.attach(link, filter_blacklist=False)
        seq = await client.send_access(dst, access)
        await _settle(client)
        assert all(len(w) <= att_mtu - 3 for w in link.writes)
        sent = link.sent_access()
        # a group gets a segmented message twice
        copies = 2 if len(access) > 11 and dst == GROUP_WC else 1
        assert sent[:1] == [(OUR_SRC, dst, 4, seq, access)]
        assert len(sent) == copies
        await client.detach()

    with (
        patch.object(client_mod, "asyncio", FastAsyncio()),
        patch.object(client_mod, "SEGMENT_ACK_TIMEOUT", 0.01),
    ):
        asyncio.run(scenario())


# ============================================================================= network and upper transport crypto


@given(
    netkey=keys16,
    iv_index=iv_indexes,
    previous_iv=st.booleans(),
    ctl=st.booleans(),
    ttl=st.integers(0, 0x7F),
    seq=st.integers(0, SEQ_MAX),
    src=unicast,
    dst=st.integers(0, 0xFFFF),
    transport=st.binary(min_size=1, max_size=16),
    proxy=st.booleans(),
)
def test_network_encrypt_decrypt_round_trip(
    netkey: bytes,
    iv_index: int,
    previous_iv: bool,
    ctl: bool,
    ttl: int,
    seq: int,
    src: int,
    dst: int,
    transport: bytes,
    proxy: bool,
) -> None:
    # only the proxy configuration nonce may carry the unassigned address
    assume(dst != 0 or proxy)
    nk = NetKeyMaterial.derive(netkey)
    sent_iv = iv_index - 1 if previous_iv else iv_index
    pdu = network_encrypt(nk, sent_iv, ctl, ttl, seq, src, dst, transport, proxy=proxy)
    got = network_decrypt(nk, iv_index, pdu, proxy=proxy)
    assert got == NetworkPDU(
        sent_iv & 1, sent_iv, nk.nid, ctl, ttl, seq, src, dst, transport
    )
    # the other nonce never opens it: a network PDU cannot pass for a proxy configuration message, or back
    assert network_decrypt(nk, iv_index, pdu, proxy=not proxy) is None


@given(
    netkey=keys16,
    iv_index=iv_indexes,
    ctl=st.booleans(),
    seq=st.integers(0, SEQ_MAX),
    src=unicast,
    dst=st.integers(1, 0xFFFF),
    transport=st.binary(min_size=1, max_size=16),
    data=st.data(),
)
def test_network_pdu_with_any_bit_flipped_is_rejected(
    netkey: bytes,
    iv_index: int,
    ctl: bool,
    seq: int,
    src: int,
    dst: int,
    transport: bytes,
    data: st.DataObject,
) -> None:
    nk = NetKeyMaterial.derive(netkey)
    pdu = bytearray(network_encrypt(nk, iv_index, ctl, 5, seq, src, dst, transport))
    bit = data.draw(st.integers(0, 8 * len(pdu) - 1))
    pdu[bit // 8] ^= 1 << (bit % 8)
    assert network_decrypt(nk, iv_index, bytes(pdu)) is None


@given(
    key=keys16,
    kind=st.sampled_from((NONCE_APP, NONCE_DEVICE)),
    iv_index=st.integers(0, 0xFFFFFFFF),
    seq=st.integers(0, SEQ_MAX),
    src=unicast,
    dst=st.integers(0, 0xFFFF),
    access=st.binary(min_size=1, max_size=380),
    szmic=st.integers(0, 1),
    data=st.data(),
)
def test_upper_transport_round_trip_and_nonce_binding(
    key: bytes,
    kind: int,
    iv_index: int,
    seq: int,
    src: int,
    dst: int,
    access: bytes,
    szmic: int,
    data: st.DataObject,
) -> None:
    upper = upper_encrypt(key, kind, iv_index, seq, src, dst, access, szmic)
    assert len(upper) == len(access) + (8 if szmic else 4)
    assert upper_decrypt(key, kind, iv_index, seq, src, dst, upper, szmic) == access
    # any other nonce field, the other MIC size or the other nonce type: not decryptable
    field = data.draw(st.sampled_from(("kind", "iv", "seq", "src", "dst", "szmic")))
    args: dict[str, Any] = {
        "kind": kind,
        "iv": iv_index,
        "seq": seq,
        "src": src,
        "dst": dst,
        "szmic": szmic,
    }
    args[field] = {
        "kind": NONCE_DEVICE if kind == NONCE_APP else NONCE_APP,
        "iv": iv_index ^ 1,
        "seq": seq ^ 1,
        "src": src ^ 1,
        "dst": dst ^ 1,
        "szmic": 1 - szmic,
    }[field]
    assert (
        upper_decrypt(
            key,
            args["kind"],
            args["iv"],
            args["seq"],
            args["src"],
            args["dst"],
            upper,
            args["szmic"],
        )
        is None
    )


# ============================================================================= opcodes, lower transport headers


@given(access=st.binary(max_size=40))
def test_decode_opcode_is_the_inverse_of_encode_opcode(access: bytes) -> None:
    """Any bytes: either rejected with ValueError, or the opcode re-encodes to exactly the bytes it came from."""
    malformed = (
        not access
        or access[0] == 0x7F
        or (0x80 <= access[0] < 0xC0 and len(access) < 2)
        or (access[0] >= 0xC0 and len(access) < 3)
    )
    if malformed:
        with pytest.raises(ValueError, match=r"opcode|access PDU"):
            decode_opcode(access)
        return
    op, cid, params = decode_opcode(access)
    assert encode_opcode(op, cid) + params == access


@given(
    aid=st.integers(0, 0x3F),
    akf=st.booleans(),
    seq0=st.integers(0, SEQ_MAX),
    szmic=st.integers(0, 1),
    upper=st.binary(min_size=1, max_size=SEGMENTS_MAX * 12),
)
def test_lower_segments_parse_back(
    aid: int, akf: bool, seq0: int, szmic: int, upper: bytes
) -> None:
    segs = lower_segments_access(aid, seq0, upper, szmic=szmic, akf=akf)
    assert len(segs) == -(-len(upper) // 12)
    parsed = [parse_lower(s, ctl=False) for s in segs]
    assert all(p[0] == "seg" for p in parsed)
    infos: list[SegmentInfo] = [p[1] for p in parsed]
    n = len(segs) - 1
    assert [(i.akf, i.aid, i.szmic, i.seq_zero, i.seg_o, i.seg_n) for i in infos] == [
        (akf, aid, szmic, seq0 & 0x1FFF, o, n) for o in range(len(segs))
    ]
    assert b"".join(i.data for i in infos) == upper


@given(extra=st.integers(1, 200))
def test_lower_segments_refuse_what_32_segments_cannot_carry(extra: int) -> None:
    with pytest.raises(ValueError, match="more than the 32"):
        lower_segments_access(0, 0, bytes(SEGMENTS_MAX * 12 + extra))


@given(
    aid=st.integers(0, 0x3F),
    akf=st.booleans(),
    upper=st.binary(min_size=5, max_size=15),
)
def test_unsegmented_access_parses_back(aid: int, akf: bool, upper: bytes) -> None:
    assert parse_lower(lower_unsegmented_access(aid, upper, akf=akf), ctl=False) == (
        "unseg",
        akf,
        aid,
        upper,
    )


@given(pdu=st.binary(max_size=20), ctl=st.booleans())
def test_parse_lower_takes_any_bytes(pdu: bytes, ctl: bool) -> None:
    """Whatever authenticated with the NetKey: parsed, or refused with ValueError — nothing else escapes."""
    try:
        parsed = parse_lower(pdu, ctl)
    except ValueError:
        return
    if parsed[0] == "seg":
        info = parsed[1]
        assert 1 <= len(info.data) <= 12
        assert info.seg_o >= info.seg_n or len(info.data) == 12


@given(seq0=st.integers(0, SEQ_MAX), later=st.integers(0, 0x1FFF))
def test_seq_auth_is_recovered_from_any_later_segment(seq0: int, later: int) -> None:
    assume(seq0 + later <= SEQ_MAX)
    assert seq_auth_from(seq0 + later, seq0 & 0x1FFF) == seq0


# ============================================================================= beacons


@given(
    netkey=keys16,
    iv_index=st.integers(0, 0xFFFFFFFF),
    key_refresh=st.booleans(),
    iv_update=st.booleans(),
    data=st.data(),
)
def test_secure_network_beacon_authenticates_exactly_when_untouched(
    netkey: bytes,
    iv_index: int,
    key_refresh: bool,
    iv_update: bool,
    data: st.DataObject,
) -> None:
    nk = NetKeyMaterial.derive(netkey)
    body = (
        bytes([int(key_refresh) | int(iv_update) << 1])
        + nk.network_id
        + iv_index.to_bytes(4, "big")
    )
    payload = b"\x01" + body + aes_cmac(nk.beacon_key, body)[:8]
    b = parse_beacon(nk, payload)
    assert b is not None
    assert (b.key_refresh, b.iv_update, b.network_id, b.iv_index, b.authenticated) == (
        key_refresh,
        iv_update,
        nk.network_id,
        iv_index,
        True,
    )
    # anything after the beacon type octet
    bit = data.draw(st.integers(8, 8 * len(payload) - 1))
    tampered = bytearray(payload)
    tampered[bit // 8] ^= 1 << (bit % 8)
    t = parse_beacon(nk, bytes(tampered))
    assert t is not None
    assert not t.authenticated
