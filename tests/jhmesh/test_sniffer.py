"""jhmesh.sniffer: sniffer records, Nordic pcap parsing and the passive decoder."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

import pytest

from jhmesh.crypto import AppKeyMaterial, NetKeyMaterial, aes_cmac, ccm_encrypt
from jhmesh.pdu import (
    encode_opcode,
    lower_segments_access,
    lower_unsegmented_access,
    network_encrypt,
    proxy_config_set_filter,
    proxy_frame,
    segment_ack,
    upper_encrypt_app,
    upper_encrypt_dev,
)
from jhmesh.sniffer import (
    AD_BEACON,
    AD_MESSAGE,
    AD_PB_ADV,
    COPY_MEMORY,
    LINKTYPE_NORDIC_BLE,
    PRUNE_EVERY,
    SEGMENT_MEMORY,
    MeshDecoder,
    SniffRecord,
    ad_structures,
    format_address,
    mesh_records,
    read_pcap,
)
from tests.jhmesh.conftest import refreshing_cdb
from tests.jhmesh.fixture_network import LIGHT_SWITCH, OUR_ADDRESS, SOCKET, onoff_status

if TYPE_CHECKING:
    from jhmesh.cdb import CDB

GATEWAY = 0x00DC
GROUP = 0xC061
h = bytes.fromhex


# ----------------------------------------------------------------------------- on-air builders


class Air:
    """Builds the advertising-bearer PDUs the nodes of the fixture network would send."""

    def __init__(self, cdb: CDB, iv_index: int = 0) -> None:
        self.cdb = cdb
        self.nk = cdb.net_keys[0]
        self.ak = cdb.app_keys[0]
        self.iv = iv_index
        self.seq = 0x1000
        self.now = 1000.0

    def _next(self) -> int:
        self.seq += 1
        return self.seq

    def record(
        self,
        pdu: bytes,
        kind: str = "msg",
        ttl_seen: int | None = None,
        dt: float = 0.01,
    ) -> SniffRecord:
        self.now += dt
        return SniffRecord(
            self.now, 37, -50, "0A:0B:0C:0D:0E:0F", kind, pdu, ts_us=int(self.now * 1e6)
        )

    def access(
        self,
        src: int,
        dst: int,
        access_pdu: bytes,
        ttl: int = 5,
        seq: int | None = None,
    ) -> bytes:
        seq = self._next() if seq is None else seq
        upper = upper_encrypt_app(self.ak, self.iv, seq, src, dst, access_pdu)
        return network_encrypt(
            self.nk,
            self.iv,
            False,
            ttl,
            seq,
            src,
            dst,
            lower_unsegmented_access(self.ak.aid, upper),
        )

    def devkey(
        self, src: int, dst: int, access_pdu: bytes, node_addr: int, ttl: int = 5
    ) -> bytes:
        seq = self._next()
        node = self.cdb.node_by_addr(node_addr)
        assert node is not None
        upper = upper_encrypt_dev(node.dev_key, self.iv, seq, src, dst, access_pdu)
        return network_encrypt(
            self.nk,
            self.iv,
            False,
            ttl,
            seq,
            src,
            dst,
            lower_unsegmented_access(0, upper, akf=False),
        )

    def segments(
        self, src: int, dst: int, access_pdu: bytes, akf: bool = True
    ) -> list[bytes]:
        seq0 = self._next()
        if akf:
            upper = upper_encrypt_app(self.ak, self.iv, seq0, src, dst, access_pdu)
            aid = self.ak.aid
        else:
            node = self.cdb.node_by_addr(src)
            assert node is not None
            upper = upper_encrypt_dev(node.dev_key, self.iv, seq0, src, dst, access_pdu)
            aid = 0
        out = []
        for i, lower in enumerate(lower_segments_access(aid, seq0, upper, akf=akf)):
            seq = seq0 if i == 0 else self._next()
            out.append(
                network_encrypt(self.nk, self.iv, False, 5, seq, src, dst, lower)
            )
        return out

    def control(self, src: int, dst: int, transport: bytes) -> bytes:
        return network_encrypt(
            self.nk, self.iv, True, 5, self._next(), src, dst, transport
        )

    def beacon(
        self,
        iv_index: int = 0,
        iv_update: bool = False,
        key_refresh: bool = False,
        key=None,
    ) -> bytes:
        nk = key or self.nk
        flags = (1 if key_refresh else 0) | (2 if iv_update else 0)
        body = bytes([flags]) + nk.network_id + iv_index.to_bytes(4, "big")
        return b"\x01" + body + aes_cmac(nk.beacon_key, body)[:8]

    def private_beacon(
        self, iv_index: int = 0, iv_update: bool = False, key=None
    ) -> bytes:
        """A Mesh Private beacon (Mesh Protocol 1.1 §3.10.4): Random ‖ AES-CCM(Flags ‖ IV Index) with an 8-byte tag."""
        nk = key or self.nk
        random = bytes(range(13))
        data = bytes([2 if iv_update else 0]) + iv_index.to_bytes(4, "big")
        return b"\x02" + random + ccm_encrypt(nk.private_beacon_key, random, data, 8)


@pytest.fixture
def air(cdb: CDB) -> Air:
    return Air(cdb)


@pytest.fixture
def decoder(cdb: CDB) -> MeshDecoder:
    return MeshDecoder(cdb)


# ----------------------------------------------------------------------------- records


def test_record_json_round_trip():
    rec = SniffRecord(
        1700000000.123456,
        38,
        -61,
        "2F:AC:F8:13:BF:42",
        "msg",
        h("aabbcc"),
        ts_us=12345,
        count=3,
    )
    line = rec.to_json()
    assert '"n":3' in line
    assert '"ts_us":12345' in line
    assert SniffRecord.from_json(line) == rec
    minimal = SniffRecord(1.0, 37, -40, "00:00:00:00:00:01", "beacon", h("01"))
    assert "ts_us" not in minimal.to_json()
    assert '"n"' not in minimal.to_json()
    assert SniffRecord.from_json(minimal.to_json()) == minimal


@pytest.mark.parametrize(
    "line",
    [
        "",
        "not json",
        "[]",
        '{"t": 1}',
        '{"t":"x","ch":1,"rssi":1,"adv":"a","kind":"msg","pdu":"zz"}',
        pytest.param(  # CRY-04: int(inf)
            '{"t":1,"ch":1e400,"rssi":0,"adv":"a","kind":"msg","pdu":"00"}',
            id="overflow",
        ),
        pytest.param("[" * 100000, id="deep-nesting"),  # CRY-04: json.loads recursion
    ],
)
def test_record_from_json_rejects_garbage(line: str):
    with pytest.raises(ValueError, match="not a sniffer record"):
        SniffRecord.from_json(line)


def test_ad_structures_stop_at_padding_and_overrun():
    data = h("020106") + h("032a1122") + h("00ff") + h("0509aa")
    assert list(ad_structures(data)) == [(0x01, h("06")), (0x2A, h("1122"))]
    assert list(ad_structures(h("03"))) == []
    assert list(ad_structures(b"")) == []


def test_mesh_records_keep_only_mesh_ad_types():
    adv = h("020106") + h("032a1122") + h("042b010203") + h("0229ab") + h("03ff2705")
    recs = mesh_records(5.0, 39, -70, "AA:BB:CC:DD:EE:FF", adv, ts_us=99)
    assert [(r.kind, r.pdu) for r in recs] == [
        ("msg", h("1122")),
        ("beacon", h("010203")),
        ("pbadv", h("ab")),
    ]
    assert recs[0].time == 5.0
    assert recs[0].channel == 39
    assert recs[0].rssi == -70
    assert recs[0].ts_us == 99
    assert (AD_MESSAGE, AD_BEACON, AD_PB_ADV) == (0x2A, 0x2B, 0x29)


def test_format_address():
    assert format_address(h("2facf813bf42")) == "2F:AC:F8:13:BF:42"


# ----------------------------------------------------------------------------- pcap


def nordic_packet(
    adv_data: bytes,
    channel: int = 38,
    rssi: int = 55,
    ts_us: int = 123456,
    crc_ok: bool = True,
    adv_type: int = 0x2,
    protover: int = 3,
    packet_id: int = 0x02,
    header_len: int = 10,
) -> bytes:
    adv_a = h("42bf13f8ac2f")  # 2F:AC:F8:13:BF:42 on air (LSB first)
    ble = (
        h("d6be898e") + bytes([adv_type, len(adv_a) + len(adv_data)]) + adv_a + adv_data
    )
    ble_header = bytes(
        [header_len, 0x01 if crc_ok else 0x00, channel, rssi]
    ) + struct.pack("<HL", 7, ts_us)
    payload = ble_header + ble
    return (
        bytes([0])
        + struct.pack("<HBHB", len(payload), protover, 1, packet_id)
        + payload
    )


def pcap(
    *packets: bytes,
    magic: int = 0xA1B2C3D4,
    linktype: int = LINKTYPE_NORDIC_BLE,
    order: str = "<",
) -> bytes:
    out = struct.pack(order + "LHHiLLL", magic, 2, 4, 0, 0, 0xFFFF, linktype)
    for i, p in enumerate(packets):
        out += (
            struct.pack(order + "LLLL", 1_700_000_000 + i, 250_000, len(p), len(p)) + p
        )
    return out


def test_read_pcap_yields_mesh_records():
    mesh = h("032a1122")
    data = pcap(
        nordic_packet(mesh),
        nordic_packet(h("020106")),
        nordic_packet(mesh, crc_ok=False),
    )
    recs = list(read_pcap(data))
    assert len(recs) == 1
    rec = recs[0]
    assert rec.time == pytest.approx(1_700_000_000.25)
    assert (rec.channel, rec.rssi, rec.adv, rec.kind, rec.pdu, rec.ts_us) == (
        38,
        -55,
        "2F:AC:F8:13:BF:42",
        "msg",
        h("1122"),
        123456,
    )


def test_read_pcap_big_endian_and_nanoseconds():
    mesh = h("032a1122")
    be = pcap(nordic_packet(mesh), magic=0xA1B2C3D4, order=">")
    assert len(list(read_pcap(be))) == 1
    ns = pcap(nordic_packet(mesh), magic=0xA1B23C4D)
    assert next(iter(read_pcap(ns))).time == pytest.approx(
        1_700_000_000 + 250_000 / 1e9
    )


def test_read_pcap_skips_other_packet_kinds():
    mesh = h("032a1122")
    data = pcap(
        nordic_packet(mesh, protover=2),  # older protocol
        nordic_packet(mesh, packet_id=0x06),  # data PDU
        nordic_packet(mesh, header_len=9),  # unexpected BLE header
        nordic_packet(mesh, adv_type=0x1),  # ADV_DIRECT_IND has no AdvData
        b"\x00\x01\x02",  # truncated record
        bytes([0])
        + struct.pack("<HBHB", 10, 3, 1, 2)
        + bytes([10, 1, 37, 40])
        + bytes(6)
        + h("d6be898e"),  # nothing after the access address
        bytes([0])
        + struct.pack("<HBHB", 10, 3, 1, 2)
        + bytes([10, 1, 37, 40])
        + bytes(6)
        + h("d6be898e")
        + h("0203aabbcc"),  # short AdvA
        nordic_packet(mesh),
    )
    assert len(list(read_pcap(data))) == 1


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (b"short", "not a pcap"),
        (pcap(magic=0x12345678), "not a pcap"),
        (pcap(linktype=256), "link type 256"),
    ],
)
def test_read_pcap_rejects_other_files(data: bytes, match: str):
    with pytest.raises(ValueError, match=match):
        list(read_pcap(data))


# ----------------------------------------------------------------------------- decoder


def test_decoder_access_message_and_copies(air: Air, decoder: MeshDecoder):
    pdu = air.access(LIGHT_SWITCH, GROUP, onoff_status(True), ttl=4)
    first = decoder.feed(air.record(pdu))
    assert first.kind == "access"
    assert first.message is not None
    assert (
        first.message.src,
        first.message.dst,
        first.message.ttl,
        first.message.key,
    ) == (LIGHT_SWITCH, GROUP, 4, "app0")
    assert "Generic OnOff Status present=ON" in first.text
    assert first.text.startswith("0148→C061 ttl=4 seq=")
    # a relay copy: same src/seq, lower TTL, different bytes
    seq = first.message.seq
    relayed = air.access(LIGHT_SWITCH, GROUP, onoff_status(True), ttl=3, seq=seq)
    copy = decoder.feed(air.record(relayed))
    assert copy.kind == "copy"
    assert copy.copies == 2
    assert copy.net is not None
    assert copy.net.ttl == 3
    assert copy.text == f"copy 2 of 0148→C061 seq={seq:06X}"
    assert decoder.feed(air.record(relayed)).copies == 3
    assert decoder.stats == {"access": 1, "copy": 2}
    assert decoder.sources == {LIGHT_SWITCH: 1}


def test_decoder_devkey_message_by_destination_and_source(
    air: Air, decoder: MeshDecoder
):
    node = air.cdb.node_by_addr(LIGHT_SWITCH)
    assert node is not None
    request = air.devkey(
        OUR_ADDRESS, node.unicast, encode_opcode(0x8008), node.unicast
    )  # Config Composition Data Get
    got = decoder.feed(air.record(request))
    assert got.kind == "access"
    assert got.message is not None
    assert got.message.key == f"dev:{node.unicast:04X}"
    reply = air.devkey(
        node.unicast, OUR_ADDRESS, encode_opcode(0x8003) + b"\x00", node.unicast
    )
    got = decoder.feed(air.record(reply))
    assert got.kind == "access"
    assert got.message is not None
    assert got.message.key == f"dev:{node.unicast:04X}"


def test_decoder_devkey_message_between_two_known_nodes_tries_both_keys(
    air: Air, decoder: MeshDecoder
):
    a, b = air.cdb.node_by_addr(LIGHT_SWITCH), air.cdb.node_by_addr(SOCKET)
    assert a is not None
    assert b is not None
    pdu = air.devkey(
        a.unicast, b.unicast, encode_opcode(0x8008), b.unicast
    )  # encrypted with the destination's key
    got = decoder.feed(air.record(pdu))
    assert got.kind == "access"
    assert got.message is not None
    assert got.message.key == f"dev:{b.unicast:04X}"


def test_decoder_unknown_device_key_is_undecryptable(air: Air, decoder: MeshDecoder):
    node = air.cdb.node_by_addr(LIGHT_SWITCH)
    assert node is not None
    # encrypted with 0148's DevKey but sent between two addresses we cannot map to it
    upper = upper_encrypt_dev(
        node.dev_key, 0, 0x2000, OUR_ADDRESS, 0x0BBB, encode_opcode(0x8008)
    )
    pdu = network_encrypt(
        air.nk,
        0,
        False,
        5,
        0x2000,
        OUR_ADDRESS,
        0x0BBB,
        lower_unsegmented_access(0, upper, akf=False),
    )
    got = decoder.feed(air.record(pdu))
    assert got.kind == "undecryptable"
    assert "undecryptable upper transport (akf=0 aid=0)" in got.text


def test_decoder_wrong_app_key_is_undecryptable(air: Air, decoder: MeshDecoder):
    other = AppKeyMaterial(key=bytes(16), aid=air.ak.aid)  # same AID, different key
    upper = upper_encrypt_app(
        other, 0, 0x2001, LIGHT_SWITCH, GROUP, onoff_status(False)
    )
    pdu = network_encrypt(
        air.nk,
        0,
        False,
        5,
        0x2001,
        LIGHT_SWITCH,
        GROUP,
        lower_unsegmented_access(other.aid, upper),
    )
    assert decoder.feed(air.record(pdu)).kind == "undecryptable"
    # an AID no key of ours has
    pdu = network_encrypt(
        air.nk,
        0,
        False,
        5,
        0x2002,
        LIGHT_SWITCH,
        GROUP,
        lower_unsegmented_access((other.aid + 1) & 0x3F, upper),
    )
    assert decoder.feed(air.record(pdu)).kind == "undecryptable"


def test_decoder_foreign_and_undecryptable_network_pdus(air: Air, decoder: MeshDecoder):
    pdu = air.access(LIGHT_SWITCH, GROUP, onoff_status(True))
    foreign = bytes([(pdu[0] & 0x80) | ((air.nk.nid + 1) & 0x7F)]) + pdu[1:]
    got = decoder.feed(air.record(foreign))
    assert got.kind == "foreign"
    assert got.text.startswith("foreign network PDU (NID")
    corrupt = pdu[:-1] + bytes([pdu[-1] ^ 0xFF])
    got = decoder.feed(air.record(corrupt))
    assert got.kind == "undecryptable"
    assert got.text.startswith("undecryptable network PDU")
    assert decoder.feed(air.record(h("aabb"), kind="pbadv")).kind == "pbadv"


def test_decoder_segmented_message(air: Air, decoder: MeshDecoder):
    status = (
        encode_opcode(0x4A) + h("1a00") + b"\x01" + b"02020002"
    )  # Generic Manufacturer Property Status, 2 segments
    parts = air.segments(0x0232, GATEWAY, status)
    assert len(parts) == 2
    first = decoder.feed(air.record(parts[0]))
    assert first.kind == "segment"
    assert "segment 1/2 (SeqAuth" in first.text
    done = decoder.feed(air.record(parts[1]))
    assert done.kind == "access"
    assert done.message is not None
    assert done.message.access_pdu == status
    assert done.message.seq == first.net.seq
    assert done.record is first.record  # reported at the time the first segment arrived
    assert decoder.stats == {"segment": 1, "access": 1}


def test_decoder_does_not_report_a_retransmitted_segmented_message_twice(
    air: Air, decoder: MeshDecoder
):
    """CRY-02: lower-transport retransmissions carry fresh SEQs (no copy) but the same SeqZero: after the message
    completed they are reported as segments of it, not as a second access message."""
    status = encode_opcode(0x4A) + h("1a00") + b"\x01" + b"02020002"
    parts = air.segments(0x0232, GATEWAY, status)
    seq0 = air.seq - len(parts) + 1
    assert [decoder.feed(air.record(p)).kind for p in parts] == ["segment", "access"]
    upper = upper_encrypt_app(air.ak, air.iv, seq0, 0x0232, GATEWAY, status)
    again = [
        network_encrypt(air.nk, air.iv, False, 5, air._next(), 0x0232, GATEWAY, lower)
        for lower in lower_segments_access(air.ak.aid, seq0, upper)
    ]
    results = [decoder.feed(air.record(p)) for p in again]
    assert [r.kind for r in results] == ["segment", "segment"]
    assert "retransmission of a completed message" in results[-1].text
    assert decoder.sources[0x0232] == 1
    assert decoder.stats["access"] == 1


def test_decoder_segmented_message_out_of_order_and_duplicates(
    air: Air, decoder: MeshDecoder
):
    status = encode_opcode(0x4A) + h("1a00") + b"\x01" + b"02020002"
    parts = air.segments(0x0232, GATEWAY, status)
    assert decoder.feed(air.record(parts[1])).kind == "segment"
    assert decoder.feed(air.record(parts[1])).kind == "copy"
    assert decoder.feed(air.record(parts[0])).kind == "access"


def test_decoder_segmented_devkey_message(air: Air, decoder: MeshDecoder):
    node = air.cdb.node_by_addr(LIGHT_SWITCH)
    assert node is not None
    composition = encode_opcode(0x02) + bytes(20)
    parts = air.segments(node.unicast, OUR_ADDRESS, composition, akf=False)
    for part in parts[:-1]:
        assert decoder.feed(air.record(part)).kind == "segment"
    done = decoder.feed(air.record(parts[-1]))
    assert done.kind == "access"
    assert done.message is not None
    assert done.message.key == f"dev:{node.unicast:04X}"


def test_decoder_segmented_message_with_unknown_key(air: Air, decoder: MeshDecoder):
    other = AppKeyMaterial(key=bytes(16), aid=air.ak.aid)
    upper = upper_encrypt_app(other, 0, 0x3000, 0x0232, GATEWAY, bytes(20))
    lowers = lower_segments_access(other.aid, 0x3000, upper)
    for i, lower in enumerate(lowers):
        got = decoder.feed(
            air.record(
                network_encrypt(air.nk, 0, False, 5, 0x3000 + i, 0x0232, GATEWAY, lower)
            )
        )
    assert got.kind == "undecryptable"
    assert "undecryptable segmented message" in got.text


def test_decoder_control_messages(air: Air, decoder: MeshDecoder):
    ack = decoder.feed(air.record(air.control(GATEWAY, 0x0232, segment_ack(4788, 0x3))))
    assert ack.kind == "control"
    assert "Segment Ack seq_zero=4788 block=00000003" in ack.text
    obo = decoder.feed(
        air.record(air.control(GATEWAY, 0x0232, segment_ack(1, 0x1, obo=True)))
    )
    assert obo.text.endswith("(on behalf)")
    hb = decoder.feed(air.record(air.control(GATEWAY, 0xFFFF, b"\x0a\x00\x05\x00\x01")))
    assert "Heartbeat 00050001" in hb.text
    other = decoder.feed(air.record(air.control(GATEWAY, 0xFFFF, b"\x05\x01\x02")))
    assert "control op 05 0102" in other.text
    segctl = decoder.feed(
        air.record(air.control(GATEWAY, 0xFFFF, b"\x80\x00\x00\x00\x00"))
    )
    assert segctl.kind == "control"
    assert "segmented control PDU" in segctl.text


def test_decoder_beacons_follow_the_iv_index(air: Air, decoder: MeshDecoder):
    got = decoder.feed(air.record(air.beacon(), kind="beacon"))
    assert got.kind == "beacon"
    assert got.text == "beacon iv=0 flags=- auth=ok"
    assert got.beacon is not None
    assert decoder.iv_index == 0
    got = decoder.feed(
        air.record(air.beacon(iv_index=1, iv_update=True), kind="beacon")
    )
    assert got.text == "beacon iv=1 flags=iv_update auth=ok"
    assert decoder.iv_index == 1
    both = decoder.feed(
        air.record(
            air.beacon(iv_index=1, iv_update=True, key_refresh=True), kind="beacon"
        )
    )
    assert both.text == "beacon iv=1 flags=key_refresh,iv_update auth=ok"
    # a forged beacon does not move the index
    body = bytes([0]) + air.nk.network_id + (7).to_bytes(4, "big")
    forged = decoder.feed(air.record(b"\x01" + body + bytes(8), kind="beacon"))
    assert forged.text == "beacon iv=7 flags=- auth=FAIL"
    assert decoder.iv_index == 1
    # messages under the new index decode, so do stragglers under the old one
    air.iv = 1
    assert (
        decoder.feed(
            air.record(air.access(LIGHT_SWITCH, GROUP, onoff_status(True)))
        ).kind
        == "access"
    )
    air.iv = 0
    assert (
        decoder.feed(air.record(air.access(SOCKET, GROUP, onoff_status(True)))).kind
        == "access"
    )


def test_decoder_foreign_beacons(air: Air, decoder: MeshDecoder, cdb: CDB):
    other = NetKeyMaterial.derive(bytes(range(16)))
    got = decoder.feed(air.record(air.beacon(key=other), kind="beacon"))
    assert got.kind == "foreign"
    assert got.text.startswith("foreign beacon")
    # a private beacon no key of ours opens: its network is unknown, not named after a beacon type it is not
    private = air.private_beacon(iv_index=9, key=other)
    got = decoder.feed(air.record(private, kind="beacon"))
    assert got.kind == "foreign"
    assert got.text == f"private beacon (unknown network) {private.hex()}"
    assert got.beacon is None
    assert decoder.iv_index == 0
    # so is one of ours changed on the way (the tag covers the Random too), or cut short
    ours = air.private_beacon(iv_index=9)
    for bad in (ours[:1] + bytes([ours[1] ^ 1]) + ours[2:], ours[:26]):
        got = decoder.feed(air.record(bad, kind="beacon"))
        assert got.kind == "foreign"
        assert got.text.startswith("private beacon (unknown network)")
    assert decoder.iv_index == 0


def test_decoder_private_beacons_of_our_network(air: Air, decoder: MeshDecoder):
    """A Mesh Private beacon opened with the private beacon key of our NetKey: named, and it moves the IV index."""
    got = decoder.feed(air.record(air.private_beacon(), kind="beacon"))
    assert got.kind == "beacon"
    assert got.text == "private beacon iv=0 flags=- auth=ok"
    assert got.beacon is not None
    assert got.beacon.private
    assert got.beacon.network_id == air.nk.network_id
    got = decoder.feed(
        air.record(air.private_beacon(iv_index=3, iv_update=True), kind="beacon")
    )
    assert got.text == "private beacon iv=3 flags=iv_update auth=ok"
    assert decoder.iv_index == 3
    # the Secure Network beacon keeps its own name
    assert not decoder.feed(air.record(air.beacon(), kind="beacon")).beacon.private


def test_decoder_private_beacon_spec_vector(cdb: CDB):
    """Mesh Protocol 1.1 sample data, private beacon IVU: flags 02, IV index 1010abcd under NetKey f7a2…2b00."""
    cdb.net_keys = {0: NetKeyMaterial.derive(h("f7a2a44f8e8a8029064f173ddc1e2b00"))}
    decoder = MeshDecoder(cdb, 0)
    beacon = h("02435f18f85cf78a3121f58478a561e488e7cbf3174f022a514741")
    got = decoder.feed(SniffRecord(1.0, 37, -40, "00:00:00:00:00:01", "beacon", beacon))
    assert got.text == f"private beacon iv={0x1010ABCD} flags=iv_update auth=ok"
    assert decoder.iv_index == 0x1010ABCD


def test_decoder_unprovisioned_device_beacons(air: Air, decoder: MeshDecoder):
    """A device waiting to be provisioned: its UUID and OOB Information (named), and the URI hash when it has one."""
    uuid = h("70cf7c9732a345b691494810d2e9cbf4")
    got = decoder.feed(air.record(b"\x00" + uuid + b"\x00\x00", kind="beacon"))
    assert got.kind == "unprovisioned"
    assert got.text == (
        "unprovisioned device beacon 70CF7C97-32A3-45B6-9149-4810D2E9CBF4 oob=0000"
    )
    assert got.device is not None
    assert got.device.uuid == "70CF7C97-32A3-45B6-9149-4810D2E9CBF4"
    assert got.beacon is None
    got = decoder.feed(
        air.record(b"\x00" + uuid + b"\x08\x02" + h("d97478b3"), kind="beacon")
    )
    assert got.text == (
        "unprovisioned device beacon 70CF7C97-32A3-45B6-9149-4810D2E9CBF4"
        " oob=0802 (URI, on box) uri_hash=d97478b3"
    )
    assert got.device is not None
    assert got.device.oob_info == 0x0802
    # too short for UUID and OOB: not an unprovisioned device beacon
    short = decoder.feed(air.record(b"\x00" + uuid, kind="beacon"))
    assert short.kind == "foreign"
    assert decoder.stats["unprovisioned"] == 2


def test_decoder_of_an_export_written_mid_key_refresh_opens_both_keys(
    air: Air, cdb: CDB
):
    """Phase 1 of a key refresh: the export's `key` is the new NetKey, and the nodes still send with `oldKey`."""
    decoder = MeshDecoder(refreshing_cdb(bytes(range(0x10, 0x20)), 1))
    got = decoder.feed(air.record(air.access(SOCKET, GROUP, onoff_status(True))))
    assert got.kind == "access"
    assert decoder.feed(air.record(air.beacon(), kind="beacon")).kind == "beacon"
    air.nk = decoder.cdb.net_keys[0]  # and those that took the new one already
    got = decoder.feed(air.record(air.access(SOCKET, GROUP, onoff_status(False))))
    assert got.kind == "access"


def test_decoder_iv_index_override_and_export_lower_bound(cdb: CDB):
    assert MeshDecoder(cdb).iv_index == cdb.iv_index
    assert MeshDecoder(cdb, 5).iv_index == 5


def test_decoder_prunes_old_copies_and_segments(air: Air, decoder: MeshDecoder):
    pdu = air.access(LIGHT_SWITCH, GROUP, onoff_status(True))
    assert decoder.feed(air.record(pdu)).kind == "access"
    parts = air.segments(0x0232, GATEWAY, bytes(20))
    assert decoder.feed(air.record(parts[0])).kind == "segment"
    # fill the feed counter up to the prune, far enough in the future for both tables to expire
    air.now += max(COPY_MEMORY, SEGMENT_MEMORY) + 1
    filler = air.access(SOCKET, GROUP, onoff_status(False))
    for _ in range(PRUNE_EVERY - 2):
        decoder.feed(air.record(filler, dt=0))
    assert (
        decoder.feed(air.record(pdu, dt=0)).kind == "access"
    )  # forgotten, so no longer a copy
    assert (
        decoder.feed(air.record(parts[1], dt=0)).kind == "segment"
    )  # the first segment was dropped


# ----------------------------------------------------------------------------- followed connection (ATT → proxy PDUs)


def att_write(value: bytes, handle: int = 0x1C, request: bool = False) -> bytes:
    return bytes([0x12 if request else 0x52]) + handle.to_bytes(2, "little") + value


def att_notify(value: bytes, handle: int = 0x1F) -> bytes:
    return b"\x1b" + handle.to_bytes(2, "little") + value


def gatt(air: Air, att: bytes, direction: str = "m2s") -> SniffRecord:
    air.now += 0.01
    return SniffRecord(
        air.now, 17, -55, "D6:BE:89:8E", "gatt", att, direction=direction
    )


def test_gatt_record_json_round_trip():
    rec = SniffRecord(
        1.0, 17, -55, "D6:BE:89:8E", "gatt", h("521c0000"), direction="m2s"
    )
    assert '"dir":"m2s"' in rec.to_json()
    assert SniffRecord.from_json(rec.to_json()) == rec


def test_gatt_proxy_network_pdus_decode_like_air_traffic(
    air: Air, decoder: MeshDecoder
):
    # the client writes an OnOff Set (proxy PDU type 0) to Data In, in one write
    set_pdu = air.access(OUR_ADDRESS, LIGHT_SWITCH, h("8202") + h("0101"))
    (frame,) = proxy_frame(0, set_pdu, 244)
    got = decoder.feed(gatt(air, att_write(frame)))
    assert got.kind == "access"
    assert got.text.startswith("[gatt →node] 0D00→0148 ttl=5")
    assert "Generic OnOff Set" in got.text
    # the same PDU heard on air afterwards is a copy of it
    assert decoder.feed(air.record(set_pdu)).kind == "copy"
    # the node's status comes back as a notification from Data Out, segmented over two ATT PDUs (SAR)
    status = air.access(LIGHT_SWITCH, OUR_ADDRESS, onoff_status(True))
    frames = proxy_frame(0, status, 20)
    assert len(frames) >= 2
    first = decoder.feed(gatt(air, att_notify(frames[0]), "s2m"))
    assert first.kind == "gatt"
    assert "proxy PDU segment" in first.text
    for frame in frames[1:-1]:
        assert decoder.feed(gatt(air, att_notify(frame), "s2m")).kind == "gatt"
    done = decoder.feed(gatt(air, att_notify(frames[-1]), "s2m"))
    assert done.kind == "access"
    assert done.text.startswith("[gatt node→] 0148→0D00")
    # a segmented *access* message arriving over GATT keeps the tag on the segment lines too
    parts = air.segments(
        0x0232, OUR_ADDRESS, encode_opcode(0x4A) + h("1a00") + b"\x01" + b"02020002"
    )
    seg = decoder.feed(gatt(air, att_notify(proxy_frame(0, parts[0], 244)[0]), "s2m"))
    assert seg.kind == "segment"
    assert seg.text.startswith("[gatt node→] 0232→0D00")
    final = decoder.feed(gatt(air, att_notify(proxy_frame(0, parts[1], 244)[0]), "s2m"))
    assert final.kind == "access"
    assert final.text.startswith("[gatt node→] ")


def test_gatt_beacon_config_and_provisioning_pdus(air: Air, decoder: MeshDecoder):
    beacon = decoder.feed(
        gatt(air, att_notify(proxy_frame(1, air.beacon(), 244)[0]), "s2m")
    )
    assert beacon.kind == "beacon"
    assert beacon.text == "[gatt node→] beacon iv=0 flags=- auth=ok"
    # proxy configuration: Set Filter Type (blacklist) from the client, Filter Status from the proxy node
    set_filter = network_encrypt(
        air.nk,
        0,
        True,
        0,
        0x2000,
        OUR_ADDRESS,
        0,
        proxy_config_set_filter(1),
        proxy=True,
    )
    got = decoder.feed(gatt(air, att_write(proxy_frame(2, set_filter, 244)[0])))
    assert got.kind == "proxy"
    assert got.text == "[gatt →node] 0D00 proxy Set Filter Type blacklist"
    status = network_encrypt(
        air.nk,
        0,
        True,
        0,
        0x2001,
        LIGHT_SWITCH,
        0,
        h("03 01 0000".replace(" ", "")),
        proxy=True,
    )
    got = decoder.feed(gatt(air, att_notify(proxy_frame(2, status, 244)[0]), "s2m"))
    assert got.text == "[gatt node→] 0148 proxy Filter Status blacklist 0 addresses"
    add = network_encrypt(
        air.nk,
        0,
        True,
        0,
        0x2002,
        OUR_ADDRESS,
        0,
        h("01 c061 0148".replace(" ", "")),
        proxy=True,
    )
    got = decoder.feed(gatt(air, att_write(proxy_frame(2, add, 244)[0])))
    assert got.text == "[gatt →node] 0D00 proxy Add Addresses [C061,0148]"
    unknown_op = network_encrypt(
        air.nk, 0, True, 0, 0x2003, OUR_ADDRESS, 0, h("09aa"), proxy=True
    )
    assert decoder.feed(
        gatt(air, att_write(proxy_frame(2, unknown_op, 244)[0]))
    ).text.endswith("0D00 proxy opcode 09 aa")
    empty = network_encrypt(air.nk, 0, True, 0, 0x2004, OUR_ADDRESS, 0, b"", proxy=True)
    assert decoder.feed(
        gatt(air, att_write(proxy_frame(2, empty, 244)[0]))
    ).text.endswith(
        f"proxy configuration (undecryptable) {empty.hex()}"
    )  # 17 bytes: no room for an opcode, `network_decrypt` refuses it (was "(empty)")
    # a config PDU of another network / corrupted
    assert (
        "undecryptable"
        in decoder.feed(gatt(air, att_write(proxy_frame(2, b"\x00" * 20, 244)[0]))).text
    )
    # provisioning over PB-GATT
    invite = decoder.feed(gatt(air, att_write(proxy_frame(3, h("0005"), 244)[0])))
    assert invite.kind == "proxy"
    assert invite.text == "[gatt →node] PB-GATT Provisioning Invite 05"
    assert decoder.feed(
        gatt(air, att_write(proxy_frame(3, h("0f"), 244)[0]))
    ).text.endswith("type 0F")
    assert decoder.feed(
        gatt(air, att_write(proxy_frame(3, b"", 244)[0]))
    ).text.endswith("Provisioning (empty)")


def test_gatt_other_att_pdus_are_named(air: Air, decoder: MeshDecoder):
    assert decoder.feed(gatt(air, h("02f700"))).text == "ATT Exchange MTU Request f700"
    assert decoder.feed(gatt(air, h("13"), "s2m")).text == "ATT Write Response"
    assert decoder.feed(gatt(air, h("ff01"))).text == "ATT opcode FF 01"
    assert decoder.feed(gatt(air, b"")).text == "ATT (empty)"
    assert (
        decoder.feed(gatt(air, h("521c"))).text == "ATT Write Command 1c"
    )  # too short for a value: named, not fed to the reassembler
    bogus = decoder.feed(
        gatt(air, att_write(b"\x04\xaa"))
    )  # proxy PDU type 4: undefined
    assert bogus.kind == "gatt"
    assert bogus.text == "[gatt →node] proxy PDU type 4 aa"
    assert decoder.stats["gatt"] == 6
    # Write Requests and Indications cannot carry proxy PDUs (§7.2.2.1: Data In is write-without-response only,
    # Data Out notify only): a CCCD subscription during setup used to be fed to the reassembler and reported as
    # a "foreign beacon 00"; now it is named like the rest of the setup traffic
    cccd = decoder.feed(gatt(air, att_write(h("0100"), handle=0x1D, request=True)))
    assert cccd.kind == "gatt"
    assert cccd.text == "ATT Write Request 1d000100"
    indication = decoder.feed(gatt(air, b"\x1d\x1f\x00\x41\x01", "s2m"))
    assert indication.text == "ATT Handle Value Indication 1f004101"
    assert "s2c" not in decoder._proxy_sar  # neither started a reassembly
    assert decoder._proxy_sar["c2s"]._buf == {}


# ----------------------------------------------------------------------------- malformed but authenticated


def test_decoder_reports_malformed_transport_pdus_instead_of_raising(
    air: Air, decoder: MeshDecoder
):
    """Anyone with the NetKey can send an authenticated PDU the transport layers cannot have produced; every
    one of these used to escape `MeshDecoder.feed` as IndexError / OverflowError and abort `mesh_sniff decode`."""
    # an upper transport PDU that is only a TransMIC: decrypts to an empty access PDU (§3.7.3 needs an opcode)
    seq = air._next()
    upper = upper_encrypt_app(air.ak, 0, seq, 0x0232, GATEWAY, b"")
    got = decoder.feed(
        air.record(
            network_encrypt(
                air.nk,
                0,
                False,
                5,
                seq,
                0x0232,
                GATEWAY,
                lower_unsegmented_access(air.ak.aid, upper),
            )
        )
    )
    assert got.kind == "malformed"
    assert got.text == (
        f"0232→{GATEWAY:04X} ttl=5 seq={seq:06X} malformed: unsegmented access PDU carries 4 bytes,"
        " fewer than 5"
    )  # too short for the lower transport already (CRY-05)
    assert got.net is not None
    # a segmented access PDU cut inside its header
    seq = air._next()
    got = decoder.feed(
        air.record(
            network_encrypt(
                air.nk, 0, False, 5, seq, 0x0232, GATEWAY, bytes([0xC0 | air.ak.aid])
            )
        )
    )
    assert got.kind == "malformed"
    assert got.text.endswith(
        f"malformed: segmented access PDU truncated: {0xC0 | air.ak.aid:02x}"
    )
    # a segment whose SeqZero lies before this IV index began (SeqAuth would be negative)
    (lower,) = lower_segments_access(air.ak.aid, 0x1FFE, upper)
    got = decoder.feed(
        air.record(network_encrypt(air.nk, 0, False, 5, 5, 0x0232, GATEWAY, lower))
    )
    assert got.kind == "malformed"
    assert "malformed: SeqZero 0x1ffe lies before sequence number 0x000005" in got.text
    assert decoder._segments == {}
    # the decoder keeps going
    assert (
        decoder.feed(air.record(air.access(0x0232, GATEWAY, onoff_status(True)))).kind
        == "access"
    )
    assert decoder.stats["malformed"] == 3


def test_decoder_drops_segments_that_cannot_belong_to_their_message(
    air: Air, decoder: MeshDecoder
):
    """SegO beyond SegN, or a SegN contradicting the first segment, made the join KeyError (the client got the
    same guards in `_on_segment`); a passive observer names and drops them like the client does."""
    status = encode_opcode(0x4A) + h("1a00") + b"\x01" + b"02020002"
    parts = air.segments(0x0232, GATEWAY, status)  # 2 segments, SegN 1
    first = decoder.feed(air.record(parts[0]))
    assert first.kind == "segment"

    def raw(seq_zero: int, seg_o: int, seg_n: int) -> bytes:
        hdr = (seq_zero << 10) | (seg_o << 5) | seg_n
        return bytes([0xC0 | air.ak.aid]) + hdr.to_bytes(3, "big") + bytes(12)

    seq_zero = first.net.seq & 0x1FFF
    beyond = decoder.feed(
        air.record(
            network_encrypt(
                air.nk, 0, False, 5, air._next(), 0x0232, GATEWAY, raw(seq_zero, 3, 1)
            )
        )
    )
    assert beyond.kind == "segment"
    assert beyond.text.endswith("segment 4/2: SegO beyond SegN, dropped")
    contradicting = decoder.feed(
        air.record(
            network_encrypt(
                air.nk, 0, False, 5, air._next(), 0x0232, GATEWAY, raw(seq_zero, 0, 3)
            )
        )
    )
    assert contradicting.kind == "segment"
    assert contradicting.text.endswith(
        "segment 1/4 contradicts the 2 segments announced, dropped"
    )
    assert len(decoder._segments) == 1
    done = decoder.feed(
        air.record(parts[1])
    )  # the real second segment still completes the message
    assert done.kind == "access"
    assert done.message is not None
    assert done.message.access_pdu == status
    assert decoder._segments == {}
