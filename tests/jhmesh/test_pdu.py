"""jhmesh.pdu: network layer, opcodes, transport layers, proxy protocol, beacons."""

from __future__ import annotations

import logging

import pytest

from jhmesh import pdu as pdu_mod
from jhmesh.crypto import AppKeyMaterial, NetKeyMaterial, aes_cmac, ccm_encrypt
from jhmesh.pdu import (
    ALL_FRIENDS,
    ALL_NODES,
    ALL_PROXIES,
    ALL_RELAYS,
    FILTER_BLACKLIST,
    FILTER_WHITELIST,
    PROXY_BEACON,
    PROXY_CONFIG,
    PROXY_NETWORK_PDU,
    PROXY_PDU_MAX,
    UNASSIGNED,
    NetworkPDU,
    ProxyReassembler,
    SecureNetworkBeacon,
    SegmentInfo,
    decode_opcode,
    encode_opcode,
    is_group,
    is_unicast,
    lower_segments_access,
    lower_unsegmented_access,
    network_decrypt,
    network_encrypt,
    parse_beacon,
    parse_lower,
    parse_private_beacon,
    proxy_config_add_addresses,
    proxy_config_set_filter,
    proxy_frame,
    segment_ack,
    seq_auth_from,
    upper_decrypt,
    upper_encrypt_app,
)

h = bytes.fromhex

NK = NetKeyMaterial.derive(h("7dd7364cd842ad18c17c2b820c84c3d6"))
AK = AppKeyMaterial.derive(h("63964771734fbd76e3b40519d1d94a48"))
IV = 0x12345678


# ----------------------------------------------------------------------------- address predicates


@pytest.mark.parametrize(
    ("addr", "unicast", "group"),
    [
        (UNASSIGNED, False, False),
        (0x0001, True, False),
        (0x7FFF, True, False),
        (0x8000, False, False),
        (0xBFFF, False, False),
        (0xC000, False, True),
        (ALL_PROXIES, False, True),
        (ALL_FRIENDS, False, True),
        (ALL_RELAYS, False, True),
        (ALL_NODES, False, True),
    ],
)
def test_address_predicates(addr: int, unicast: bool, group: bool):
    assert is_unicast(addr) is unicast
    assert is_group(addr) is group


# ----------------------------------------------------------------------------- network layer


def test_network_encrypt_spec_vector():
    """Mesh Profile §8.3.1 message #1."""
    pdu = network_encrypt(
        NK,
        IV,
        ctl=True,
        ttl=0,
        seq=1,
        src=0x1201,
        dst=0xFFFD,
        transport_pdu=h("034b50057e400000010000"),
    )
    assert pdu == h("68eca487516765b5e5bfdacbaf6cb7fb6bff871f035444ce83a670df")


def test_network_decrypt_spec_vector():
    n = network_decrypt(
        NK, IV, h("68eca487516765b5e5bfdacbaf6cb7fb6bff871f035444ce83a670df")
    )
    assert n == NetworkPDU(
        ivi=0,
        iv_index=IV,
        nid=0x68,
        ctl=True,
        ttl=0,
        seq=1,
        src=0x1201,
        dst=0xFFFD,
        transport_pdu=h("034b50057e400000010000"),
    )


def test_network_decrypt_reports_the_iv_index_it_used():
    """CRY-03: the IV index the IVI bit resolved to travels with the PDU, so upper-transport decryption and the
    replay list use the one the network layer authenticated under, not a re-derivation from mutable state."""
    pdu = network_encrypt(NK, 6, False, 5, 1, 0x0001, 0xC000, b"\x40" + bytes(5))
    decoded = network_decrypt(NK, 7, pdu)
    assert decoded is not None
    assert decoded.iv_index == 6  # IVI differs: the previous index
    decoded = network_decrypt(NK, 6, pdu)
    assert decoded is not None
    assert decoded.iv_index == 6


def test_parse_lower_rejects_an_unsegmented_access_pdu_without_room_for_a_transmic():
    """CRY-05: an unsegmented upper transport PDU holds at least one access octet and a 32-bit TransMIC
    (§3.5.2.1); a shorter one is malformed, not a key problem ("undecryptable")."""
    for n in range(5):
        with pytest.raises(ValueError, match="unsegmented access PDU"):
            parse_lower(b"\x40" + bytes(n), False)
    assert parse_lower(b"\x40" + bytes(5), False) == ("unseg", True, 0, bytes(5))


def test_network_round_trip_access_pdu():
    pdu = network_encrypt(
        NK,
        7,
        ctl=False,
        ttl=5,
        seq=0xABCDEF,
        src=0x0D00,
        dst=0xC00F,
        transport_pdu=b"\x66" + bytes(10),
    )
    assert pdu[0] == 0x80 | NK.nid  # IVI bit set for an odd IV index
    n = network_decrypt(NK, 7, pdu)
    assert n is not None
    assert (n.ivi, n.ctl, n.ttl, n.seq, n.src, n.dst, n.transport_pdu) == (
        1,
        False,
        5,
        0xABCDEF,
        0x0D00,
        0xC00F,
        b"\x66" + bytes(10),
    )


def test_network_decrypt_uses_previous_iv_index_when_ivi_differs():
    pdu = network_encrypt(
        NK,
        1,
        ctl=False,
        ttl=3,
        seq=10,
        src=0x0002,
        dst=0x0003,
        transport_pdu=b"\x40\x01\x02\x03\x04",
    )
    assert network_decrypt(NK, 1, pdu) is not None  # same index, IVI matches
    assert (
        network_decrypt(NK, 2, pdu) is not None
    )  # IVI bit differs → tries iv_index - 1 = 1
    assert (
        network_decrypt(NK, 3, pdu) is None
    )  # IVI matches but index 3 ≠ 1 → MIC fails
    assert network_decrypt(NK, 0, pdu) is None  # iv_index - 1 < 0


def test_network_decrypt_rejects_wrong_nid_or_short_pdu():
    pdu = network_encrypt(
        NK,
        0,
        ctl=False,
        ttl=3,
        seq=10,
        src=0x0002,
        dst=0x0003,
        transport_pdu=b"\x40\x01\x02\x03\x04",
    )
    other = NetKeyMaterial.derive(bytes(16))
    assert other.nid != NK.nid
    assert network_decrypt(other, 0, pdu) is None
    assert network_decrypt(NK, 0, pdu[:13]) is None
    assert network_decrypt(NK, 0, b"") is None
    tampered = pdu[:-1] + bytes([pdu[-1] ^ 0x01])
    assert network_decrypt(NK, 0, tampered) is None


def test_network_proxy_nonce_round_trip():
    pdu = network_encrypt(
        NK,
        0,
        ctl=True,
        ttl=0,
        seq=5,
        src=0x0D00,
        dst=0x0000,
        transport_pdu=proxy_config_set_filter(FILTER_BLACKLIST),
        proxy=True,
    )
    n = network_decrypt(NK, 0, pdu, proxy=True)
    assert n is not None
    assert (n.ctl, n.ttl, n.seq, n.src, n.dst, n.transport_pdu) == (
        True,
        0,
        5,
        0x0D00,
        0x0000,
        b"\x00\x01",
    )
    # the proxy nonce is not interchangeable with the network nonce
    assert network_decrypt(NK, 0, pdu, proxy=False) is None
    assert (
        network_decrypt(
            NK,
            0,
            network_encrypt(NK, 0, True, 0, 5, 0x0D00, 0x0000, b"\x00\x01"),
            proxy=True,
        )
        is None
    )


def test_network_decrypt_rejects_a_control_pdu_shorter_than_its_mic_allows():
    """The old guard was the *access* minimum (14 bytes): a 15-17-byte control PDU with a valid 64-bit NetMIC
    decrypted to 0-2 plaintext bytes — a garbage DST and an empty transport PDU that crashed `parse_lower`."""
    empty = network_encrypt(
        NK, 0, ctl=True, ttl=0, seq=1, src=0x0148, dst=ALL_FRIENDS, transport_pdu=b""
    )
    assert (
        len(empty) == 17
    )  # header 7 + DST 2 + 64-bit NetMIC: authenticates, carries nothing
    assert network_decrypt(NK, 0, empty) is None
    for cut in (1, 2):  # 15 / 16 bytes: the old guard let these through as well
        assert network_decrypt(NK, 0, empty[:-cut]) is None
    ok = network_encrypt(
        NK,
        0,
        ctl=True,
        ttl=0,
        seq=1,
        src=0x0148,
        dst=ALL_FRIENDS,
        transport_pdu=b"\x0a\x00\x05\x00\x01",
    )
    assert len(ok) == 22  # 7 + 2 + 5 + 8
    for cut in range(1, 6):
        assert network_decrypt(NK, 0, ok[:-cut]) is None
    n = network_decrypt(NK, 0, ok)
    assert n is not None
    assert (n.ctl, n.dst, n.transport_pdu) == (
        True,
        ALL_FRIENDS,
        b"\x0a\x00\x05\x00\x01",
    )
    # a proxy configuration PDU (control, proxy nonce) needs its opcode too
    assert (
        network_decrypt(
            NK,
            0,
            network_encrypt(
                NK,
                0,
                ctl=True,
                ttl=0,
                seq=1,
                src=1,
                dst=0,
                transport_pdu=b"",
                proxy=True,
            ),
            proxy=True,
        )
        is None
    )


@pytest.mark.parametrize("src", [UNASSIGNED, 0x8000, 0xBFFF, 0xC000, ALL_NODES])
def test_network_decrypt_rejects_a_non_unicast_source(src: int):
    """§3.4.3: SRC shall be unicast; a group / virtual / unassigned SRC would key the replay list and the
    reassembly contexts of every receiver on an address no node can own."""
    pdu = network_encrypt(
        NK, 0, ctl=False, ttl=3, seq=1, src=src, dst=0x0148, transport_pdu=b"\x00\x01"
    )
    assert network_decrypt(NK, 0, pdu) is None
    pdu = network_encrypt(
        NK,
        0,
        ctl=True,
        ttl=0,
        seq=1,
        src=src,
        dst=0,
        transport_pdu=b"\x00\x01",
        proxy=True,
    )
    assert network_decrypt(NK, 0, pdu, proxy=True) is None


def test_network_decrypt_rejects_an_unassigned_destination_outside_proxy_configuration():
    pdu = network_encrypt(
        NK,
        0,
        ctl=False,
        ttl=3,
        seq=1,
        src=0x0148,
        dst=UNASSIGNED,
        transport_pdu=b"\x00\x01",
    )
    assert network_decrypt(NK, 0, pdu) is None
    pdu = network_encrypt(
        NK,
        0,
        ctl=True,
        ttl=3,
        seq=1,
        src=0x0148,
        dst=UNASSIGNED,
        transport_pdu=b"\x00\x01",
    )
    assert network_decrypt(NK, 0, pdu) is None
    # …while the proxy configuration messages are defined with DST 0x0000 (§6.5) and still decrypt
    pdu = network_encrypt(
        NK,
        0,
        ctl=True,
        ttl=0,
        seq=1,
        src=0x0148,
        dst=UNASSIGNED,
        transport_pdu=b"\x03\x01\x00\x00",
        proxy=True,
    )
    n = network_decrypt(NK, 0, pdu, proxy=True)
    assert n is not None
    assert n.dst == UNASSIGNED
    # every unicast and group destination passes
    for dst in (0x0001, 0x7FFF, 0xC000, ALL_NODES):
        pdu = network_encrypt(
            NK,
            0,
            ctl=False,
            ttl=3,
            seq=1,
            src=0x0148,
            dst=dst,
            transport_pdu=b"\x00\x01",
        )
        n = network_decrypt(NK, 0, pdu)
        assert n is not None
        assert n.dst == dst


def test_decrypt_swallows_only_the_authentication_failure():
    """`except Exception` hid a wrong key size, a negative SeqAuth (OverflowError) or a bad tag length as
    'undecryptable' — which the application interprets as 'the export's keys are stale'."""
    pdu = network_encrypt(
        NK,
        0,
        ctl=False,
        ttl=3,
        seq=1,
        src=0x0148,
        dst=0x0D00,
        transport_pdu=b"\x00\x01",
    )
    bad_key = NetKeyMaterial(
        key=NK.key,
        nid=NK.nid,
        enc_key=NK.enc_key + b"\x00",
        priv_key=NK.priv_key,
        network_id=NK.network_id,
        identity_key=NK.identity_key,
        beacon_key=NK.beacon_key,
        private_beacon_key=NK.private_beacon_key,
    )
    with pytest.raises(ValueError, match="key must be 128, 192, or 256 bits"):
        network_decrypt(bad_key, 0, pdu)
    upper = upper_encrypt_app(AK, IV, 0x000123, 0x0D00, 0x0148, b"\x82\x02", 0)
    with pytest.raises(ValueError, match="key must be 128, 192, or 256 bits"):
        upper_decrypt(AK.key + b"\x00", 0x01, IV, 0x000123, 0x0D00, 0x0148, upper, 0)
    with pytest.raises(OverflowError):
        upper_decrypt(AK.key, 0x01, IV, -2, 0x0D00, 0x0148, upper, 0)
    with pytest.raises(OverflowError):
        upper_decrypt(AK.key, 0x01, IV, 0x1000000, 0x0D00, 0x0148, upper, 0)
    # …while a wrong MIC, a short ciphertext and a wrong key are still just None
    assert (
        upper_decrypt(AK.key, 0x01, IV, 0x000123, 0x0D00, 0x0148, upper[:3], 0) is None
    )
    assert upper_decrypt(AK.key, 0x01, IV, 0x000123, 0x0D00, 0x0148, b"", 0) is None
    assert (
        upper_decrypt(bytes(16), 0x01, IV, 0x000123, 0x0D00, 0x0148, upper, 0) is None
    )


def test_network_ctl_uses_64_bit_mic():
    ctl = network_encrypt(
        NK, 0, ctl=True, ttl=0, seq=1, src=1, dst=2, transport_pdu=b"\x00"
    )
    acc = network_encrypt(
        NK, 0, ctl=False, ttl=0, seq=1, src=1, dst=2, transport_pdu=b"\x00"
    )
    assert len(ctl) - len(acc) == 4
    assert len(acc) == 1 + 6 + 2 + 1 + 4


# ----------------------------------------------------------------------------- opcodes


@pytest.mark.parametrize(
    ("opcode", "cid", "encoded"),
    [
        (0x00, None, b"\x00"),
        (0x52, None, b"\x52"),
        (0x7E, None, b"\x7e"),
        (0x8000, None, b"\x80\x00"),
        (0x8201, None, b"\x82\x01"),
        (0xBFFF, None, b"\xbf\xff"),
        (0x00, 0x0000, b"\xc0\x00\x00"),
        (0x02, 0x0527, b"\xc2\x27\x05"),
        (0x3F, 0x1234, b"\xff\x34\x12"),
        (0x3F, 0xFFFF, b"\xff\xff\xff"),
    ],
)
def test_encode_opcode(opcode: int, cid: int | None, encoded: bytes):
    assert encode_opcode(opcode, cid) == encoded


@pytest.mark.parametrize(
    ("opcode", "cid"),
    [
        (0x7F, None),  # RFU (§3.7.3.1) — was encoded as b"\x7f" before
        (0x80, None),  # was b"\x00\x80": decodes as opcode 0x00 with a parameter
        (0x7FFF, None),
        (0xC000, None),  # was b"\xc0\x00": decodes as a vendor opcode
        (0x10000, None),
        (-1, None),
        (0x40, 0x0527),  # was masked to 0x00
        (0x7F, 0x0527),  # was masked to 0x3F
        (-1, 0x0527),
        (0x02, 0x10000),
        (0x02, -1),
    ],
)
def test_encode_opcode_refuses_what_the_wire_cannot_carry(opcode: int, cid: int | None):
    """A silently masked / mis-framed opcode decodes as a *different* message at the receiver."""
    with pytest.raises(ValueError, match=r"opcode|company id"):
        encode_opcode(opcode, cid)


@pytest.mark.parametrize(
    ("pdu", "expected"),
    [
        (b"\x52\xaa\xbb", (0x52, None, b"\xaa\xbb")),
        (b"\x7e", (0x7E, None, b"")),
        (b"\x82\x04\x01", (0x8204, None, b"\x01")),
        (b"\x80\x00", (0x8000, None, b"")),
        (b"\xc2\x27\x05\x03\x50", (0x02, 0x0527, b"\x03\x50")),
        (b"\xff\xff\xff", (0x3F, 0xFFFF, b"")),
        (b"\x00", (0x00, None, b"")),
    ],
)
def test_decode_opcode(pdu: bytes, expected: tuple):
    assert decode_opcode(pdu) == expected


@pytest.mark.parametrize(
    ("pdu", "match"),
    [
        (b"", "empty access PDU"),
        (b"\x7f", "reserved"),
        (b"\x7f\x01", "reserved"),
        (b"\x82", "2-byte opcode truncated"),  # was (0x82, None, b"")
        (b"\xbf", "2-byte opcode truncated"),
        (b"\xc2", "vendor opcode truncated"),
        (b"\xc2\x27", "vendor opcode truncated"),  # was (2, 0x27, b"")
    ],
)
def test_decode_opcode_rejects_empty_truncated_and_reserved(pdu: bytes, match: str):
    """An upper transport PDU that is only a TransMIC verifies fine, but §3.7.3 requires the opcode."""
    with pytest.raises(ValueError, match=match):
        decode_opcode(pdu)


def test_opcode_round_trip():
    for op, cid in [(0x5E, None), (0x8245, None), (0x11, 0x0527)]:
        assert decode_opcode(encode_opcode(op, cid) + b"\x99") == (op, cid, b"\x99")


# ----------------------------------------------------------------------------- upper transport


@pytest.mark.parametrize("szmic", [0, 1])
def test_upper_transport_app_round_trip(szmic: int):
    access = b"\x82\x02\x01\x07"
    upper = upper_encrypt_app(AK, IV, 0x000123, 0x0D00, 0x0148, access, szmic)
    assert len(upper) == len(access) + (8 if szmic else 4)
    assert (
        upper_decrypt(AK.key, 0x01, IV, 0x000123, 0x0D00, 0x0148, upper, szmic)
        == access
    )
    # any change to the nonce inputs or the MIC size breaks authentication
    assert (
        upper_decrypt(AK.key, 0x02, IV, 0x000123, 0x0D00, 0x0148, upper, szmic) is None
    )
    assert (
        upper_decrypt(AK.key, 0x01, IV, 0x000124, 0x0D00, 0x0148, upper, szmic) is None
    )
    assert (
        upper_decrypt(AK.key, 0x01, IV, 0x000123, 0x0D00, 0x0149, upper, szmic) is None
    )
    assert (
        upper_decrypt(AK.key, 0x01, IV, 0x000123, 0x0D00, 0x0148, upper, 1 - szmic)
        is None
    )


# ----------------------------------------------------------------------------- lower transport


def test_lower_unsegmented_access():
    assert lower_unsegmented_access(0x26, b"\x01\x02") == b"\x66\x01\x02"
    assert lower_unsegmented_access(0xFF, b"") == b"\x7f"  # AID masked to 6 bits
    upper = b"\x01\x02" + bytes(
        4
    )  # the smallest parse_lower takes: an access octet and a TransMIC (CRY-05)
    assert parse_lower(lower_unsegmented_access(0x26, upper), ctl=False) == (
        "unseg",
        True,
        0x26,
        upper,
    )
    assert len(lower_unsegmented_access(0x26, bytes(15))) == 16
    with pytest.raises(
        ValueError, match=r"at most 15 bytes incl\. TransMIC, not 16"
    ):  # was an `assert`, gone under -O
        lower_unsegmented_access(0x26, bytes(16))


def test_lower_segments_round_trip():
    upper = bytes(range(30))
    segs = lower_segments_access(0x26, 0x00ABCD, upper, seg_size=12)
    assert len(segs) == 3
    infos = []
    for i, s in enumerate(segs):
        assert s[0] == 0x80 | 0x40 | 0x26
        kind, info = parse_lower(s, ctl=False)
        assert kind == "seg"
        assert isinstance(info, SegmentInfo)
        assert (
            info.akf,
            info.aid,
            info.szmic,
            info.seq_zero,
            info.seg_o,
            info.seg_n,
        ) == (True, 0x26, 0, 0xABCD & 0x1FFF, i, 2)
        infos.append(info)
    assert b"".join(i.data for i in infos) == upper
    assert [len(i.data) for i in infos] == [12, 12, 6]


def test_lower_segments_szmic_and_limits():
    segs = lower_segments_access(0x01, 0, bytes(13), seg_size=12, szmic=1)
    assert parse_lower(segs[0], ctl=False)[1].szmic == 1
    assert parse_lower(segs[1], ctl=False)[1].szmic == 1
    assert len(lower_segments_access(0x01, 0, bytes(12 * 32))) == 32
    with pytest.raises(
        ValueError, match="385 bytes needs 33 segments, more than the 32"
    ):  # was an `assert`: SegN is 5 bits, a wrapped value would mis-assemble everywhere
        lower_segments_access(0x01, 0, bytes(12 * 32 + 1))


def test_seq_auth_from_reconstructs_first_segment_seq():
    seq0 = 0x001000
    segs = lower_segments_access(0x26, seq0, bytes(30))
    for i, s in enumerate(segs):
        info = parse_lower(s, ctl=False)[1]
        assert seq_auth_from(seq0 + i, info.seq_zero) == seq0


def test_seq_auth_from_wraps_around_seq_zero_boundary():
    # SeqZero is 13 bits: a message started at 0x001FFE has later segments at 0x002000+, whose
    # seq & ~0x1FFF | seq_zero would point *after* the segment's own seq → subtract 0x2000.
    seq0 = 0x001FFE
    segs = lower_segments_access(0x26, seq0, bytes(30))
    for i, s in enumerate(segs):
        info = parse_lower(s, ctl=False)[1]
        assert info.seq_zero == 0x1FFE
        assert seq_auth_from(seq0 + i, info.seq_zero) == seq0
    assert seq_auth_from(0x002001, 0x1FFE) == 0x001FFE
    assert seq_auth_from(0x001FFF, 0x1FFE) == 0x001FFE
    assert seq_auth_from(0x123456, 0x123456 & 0x1FFF) == 0x123456


def test_seq_auth_from_refuses_a_first_segment_before_the_iv_index_began():
    """SeqZero above a SEQ still below 0x2000 would make SeqAuth negative: no such first segment exists under
    this IV index (§3.5.3.1), yet the value went into a nonce, the sniffer's cache key and the replay list."""
    with pytest.raises(
        ValueError, match="SeqZero 0x1ffe lies before sequence number 0x000005"
    ):
        seq_auth_from(5, 0x1FFE)
    with pytest.raises(ValueError, match="SeqZero 0x0001 lies before"):
        seq_auth_from(0, 1)
    assert seq_auth_from(0, 0) == 0
    assert seq_auth_from(0x1FFF, 0x1FFF) == 0x1FFF
    assert seq_auth_from(0x2000, 0x1FFF) == 0x1FFF


def test_parse_lower_control_pdus():
    assert parse_lower(b"\x0a\x01\x02", ctl=True) == ("ctl", 0x0A, b"\x01\x02")
    assert parse_lower(b"\x80\x00\x00\x00", ctl=True) == ("segctl", None)
    assert parse_lower(b"\x00" + bytes(6), ctl=True) == ("ctl", 0x00, bytes(6))
    assert parse_lower(b"\x0a", ctl=True) == ("ctl", 0x0A, b"")


@pytest.mark.parametrize("ctl", [False, True])
def test_parse_lower_rejects_an_empty_pdu(ctl: bool):
    with pytest.raises(ValueError, match="empty lower transport PDU"):
        parse_lower(b"", ctl=ctl)


@pytest.mark.parametrize(
    "pdu", [b"\xe6", b"\xe6\x00", b"\xe6\x00\x00", b"\xe6\x00\x00\x00"]
)
def test_parse_lower_rejects_a_truncated_segment_header(pdu: bytes):
    """1-4 bytes with SEG set parsed as a 'complete' single-segment message with empty data: the client
    acknowledged it, counted it as undecryptable and blocked SeqZero 0 of that source for 10 s."""
    with pytest.raises(ValueError, match="segmented access PDU truncated"):
        parse_lower(pdu, ctl=False)


def test_parse_lower_checks_the_segment_data_length():
    def segment(seg_o: int, seg_n: int, data: bytes) -> bytes:
        hdr = (seg_o << 5) | seg_n
        return b"\xe6" + hdr.to_bytes(3, "big") + data

    assert (
        parse_lower(segment(0, 0, b"\x01"), ctl=False)[1].data == b"\x01"
    )  # single, short
    assert parse_lower(segment(1, 1, bytes(5)), ctl=False)[1].seg_o == 1  # last, short
    assert len(parse_lower(segment(0, 1, bytes(12)), ctl=False)[1].data) == 12
    with pytest.raises(ValueError, match="segment 0 of 2 carries 11 bytes, not 12"):
        parse_lower(segment(0, 1, bytes(11)), ctl=False)  # not the last: must be full
    with pytest.raises(ValueError, match="carries 13 bytes, not 12"):
        parse_lower(segment(1, 1, bytes(13)), ctl=False)  # nothing carries more than 12
    # SegO beyond SegN is left to the callers (they log and drop it); only the length is checked here
    assert parse_lower(segment(5, 0, bytes(12)), ctl=False)[1].seg_o == 5


def test_segment_ack_encoding():
    assert segment_ack(0, 0) == b"\x00\x00\x00\x00\x00\x00\x00"
    assert segment_ack(0x1FFF, 0xFFFFFFFF, obo=True) == b"\x00\xff\xfc\xff\xff\xff\xff"
    ack = segment_ack(0x0ABC, 0b101)
    hdr = int.from_bytes(ack[1:3], "big")
    assert (hdr >> 15, (hdr >> 2) & 0x1FFF, hdr & 0x3) == (0, 0x0ABC, 0)
    assert int.from_bytes(ack[3:7], "big") == 0b101
    assert parse_lower(ack, ctl=True) == ("ctl", 0x00, ack[1:])
    assert (
        segment_ack(0x2ABC, 1)[1:3] == segment_ack(0x0ABC, 1)[1:3]
    )  # seq_zero masked to 13 bits


# ----------------------------------------------------------------------------- proxy protocol


def test_proxy_frame_single_pdu_when_it_fits():
    assert proxy_frame(PROXY_NETWORK_PDU, b"\x01\x02\x03", mtu=4) == [
        b"\x00\x01\x02\x03"
    ]
    assert proxy_frame(PROXY_BEACON, b"", mtu=20) == [b"\x01"]


def test_proxy_frame_sar_bits_for_small_mtu():
    payload = bytes(range(10))
    frames = proxy_frame(PROXY_CONFIG, payload, mtu=5)  # 4 payload bytes per frame
    assert [f[0] for f in frames] == [0x40 | 2, 0x80 | 2, 0xC0 | 2]
    assert [f[1:] for f in frames] == [payload[0:4], payload[4:8], payload[8:10]]
    frames = proxy_frame(PROXY_NETWORK_PDU, payload, mtu=6)
    assert [f[0] for f in frames] == [0x40, 0xC0]
    # a degenerate MTU still makes progress one byte at a time
    frames = proxy_frame(PROXY_NETWORK_PDU, b"ab", mtu=1)
    assert frames == [b"\x40a", b"\xc0b"]


def test_proxy_reassembler_round_trip():
    r = ProxyReassembler()
    payload = bytes(range(40))
    frames = proxy_frame(PROXY_NETWORK_PDU, payload, mtu=8)
    assert len(frames) > 2
    for f in frames[:-1]:
        assert r.feed(f) is None
    assert r.feed(frames[-1]) == (PROXY_NETWORK_PDU, payload)
    assert r._buf == {}
    assert r.feed(b"\x02\xaa") == (PROXY_CONFIG, b"\xaa")


def test_proxy_reassembler_interleaves_message_types_and_restarts():
    r = ProxyReassembler()
    assert r.feed(b"\x40\x01") is None  # start net pdu
    assert r.feed(b"\x41\x09") is None  # start beacon
    assert r.feed(b"\x80\x02") is None  # continue net pdu
    assert r.feed(b"\xc1\x0a") == (PROXY_BEACON, b"\x09\x0a")
    assert (
        r.feed(b"\x40\x07") is None
    )  # a new first segment discards the earlier partial
    assert r.feed(b"\xc0\x08") == (PROXY_NETWORK_PDU, b"\x07\x08")
    # a continuation / last without a first segment is an orphan, not the start of a PDU (CRY-01)
    assert r.feed(b"\x82\x55") is None
    assert r.feed(b"\xc2\x66") is None
    assert r.orphaned == 2


def test_proxy_reassembler_bounds_the_pre_auth_buffer(caplog: pytest.LogCaptureFixture):
    """Nothing here is authenticated yet (a rogue proxy only needs the public Network ID to be connected to): a
    stream of continuation frames that never ends must not grow the buffer without limit."""
    r = ProxyReassembler(max_len=8)
    assert r.feed(b"\x40" + bytes(4)) is None
    assert (
        r.feed(b"\x80" + bytes(4)) is None
    )  # 8 bytes: exactly the cap, still buffered
    assert len(r._buf[PROXY_NETWORK_PDU]) == 8
    assert r.feed(b"\x41" + bytes(3)) is None  # another type has its own buffer
    with caplog.at_level(logging.DEBUG, logger="jhmesh"):
        assert (
            r.feed(b"\x80" + b"\x01") is None
        )  # 9: over the cap → the whole PDU is dropped
    assert r.dropped == 1
    assert PROXY_NETWORK_PDU not in r._buf
    assert "proxy PDU of type 0 longer than 8 bytes dropped (1 so far)" in caplog.text
    assert r.feed(b"\xc1" + bytes(2)) == (
        PROXY_BEACON,
        bytes(5),
    )  # the other type was unaffected
    for _ in range(
        50
    ):  # the rogue keeps going: every fragment after a drop is an orphan, buffered nowhere (CRY-01)
        r.feed(b"\x80" + bytes(7))
    assert PROXY_NETWORK_PDU not in r._buf
    assert (r.dropped, r.orphaned) == (1, 50)
    # a new first frame always starts over cleanly
    assert r.feed(b"\x40" + b"\x07") is None
    assert r.feed(b"\xc0" + b"\x08") == (PROXY_NETWORK_PDU, b"\x07\x08")
    # a single oversize complete frame is dropped the same way; a maximal one is not
    assert r.feed(b"\x00" + bytes(9)) is None
    assert r.dropped == 2
    assert r.feed(b"\x00" + bytes(8)) == (PROXY_NETWORK_PDU, bytes(8))
    assert r.feed(b"\x42" + bytes(8)) is None
    assert r.feed(b"\xc2" + b"") == (
        PROXY_CONFIG,
        bytes(8),
    )  # a last frame carrying nothing keeps it at the cap
    assert r._buf == {}


def test_proxy_reassembler_drops_segments_without_a_first():
    """CRY-01: proxy SAR allows continuation / last frames only after a first one (Mesh Profile §6.3.1); an orphan,
    or the rest of a PDU dropped for its size, is never handed out as a complete PDU."""
    r = ProxyReassembler()
    assert r.feed(bytes([0xC0]) + b"tail") is None
    assert r.feed(bytes([0x80]) + b"mid") is None
    assert r.orphaned == 2
    r = ProxyReassembler()
    results = [r.feed(f) for f in proxy_frame(0, bytes(range(200)), 20)]
    assert r.dropped == 1
    assert all(x is None for x in results)
    payload = bytes(
        range(40)
    )  # a well-formed PDU still round-trips on the same instance
    frames = proxy_frame(PROXY_NETWORK_PDU, payload, mtu=8)
    assert [r.feed(f) for f in frames] == [None] * (len(frames) - 1) + [
        (PROXY_NETWORK_PDU, payload)
    ]


def test_proxy_reassembler_default_cap_fits_every_legitimate_proxy_pdu():
    assert PROXY_PDU_MAX == 128
    r = ProxyReassembler()
    assert r.max_len == PROXY_PDU_MAX
    provisioning = bytes(
        66
    )  # the longest proxy PDU of the spec: a Provisioning Public Key
    frames = proxy_frame(0x03, provisioning, mtu=20)
    assert [r.feed(f) for f in frames[:-1]] == [None] * (len(frames) - 1)
    assert r.feed(frames[-1]) == (0x03, provisioning)
    frames = proxy_frame(PROXY_NETWORK_PDU, bytes(129), mtu=247)
    assert r.feed(frames[0]) is None  # one notification at MTU 247 already exceeds it
    assert (r.dropped, r._buf) == (1, {})


def test_proxy_reassembler_ignores_an_empty_notification():
    """The first parser every notification hits is the pre-authentication boundary: total on its own, not
    only because both callers happen to guard `if not data` first."""
    r = ProxyReassembler()
    assert r.feed(b"") is None
    assert r.feed(b"\x40\x01") is None
    assert r.feed(b"") is None  # does not disturb a PDU being assembled
    assert r.feed(b"\xc0\x02") == (PROXY_NETWORK_PDU, b"\x01\x02")
    assert (r.dropped, r._buf) == (0, {})


def test_proxy_config_builders():
    assert proxy_config_set_filter(FILTER_WHITELIST) == b"\x00\x00"
    assert proxy_config_set_filter(FILTER_BLACKLIST) == b"\x00\x01"
    assert proxy_config_add_addresses([0x0D00, 0xC00F]) == b"\x01\x0d\x00\xc0\x0f"
    assert proxy_config_add_addresses([]) == b"\x01"


# ----------------------------------------------------------------------------- beacons


def _beacon(nk: NetKeyMaterial, flags: int, iv_index: int) -> bytes:
    body = bytes([flags]) + nk.network_id + iv_index.to_bytes(4, "big")
    return b"\x01" + body + aes_cmac(nk.beacon_key, body)[:8]


def test_parse_beacon_spec_vector():
    b = parse_beacon(
        NK, h("01" + "00" + "3ecaff672f673370" + "12345678" + "8ea261582f364f6f")
    )
    assert b == SecureNetworkBeacon(
        key_refresh=False,
        iv_update=False,
        network_id=h("3ecaff672f673370"),
        iv_index=0x12345678,
        authenticated=True,
    )


def test_parse_beacon_flags_and_auth():
    b = parse_beacon(NK, _beacon(NK, 0x03, 42))
    assert b is not None
    assert (b.key_refresh, b.iv_update, b.iv_index, b.authenticated) == (
        True,
        True,
        42,
        True,
    )
    b = parse_beacon(NK, _beacon(NK, 0x02, 42))
    assert b is not None
    assert (b.key_refresh, b.iv_update) == (False, True)
    bad = bytearray(_beacon(NK, 0x00, 42))
    bad[-1] ^= 0xFF
    b = parse_beacon(NK, bytes(bad))
    assert b is not None
    assert b.authenticated is False
    # a beacon of another network carries a valid MIC only under its own key
    other = NetKeyMaterial.derive(bytes(16))
    b = parse_beacon(NK, _beacon(other, 0x00, 42))
    assert b is not None
    assert b.authenticated is False
    assert b.network_id == other.network_id


def test_parse_beacon_rejects_wrong_type_or_length():
    assert parse_beacon(NK, b"") is None
    assert (
        parse_beacon(NK, b"\x00" + bytes(21)) is None
    )  # unprovisioned device beacon type
    assert parse_beacon(NK, _beacon(NK, 0, 1)[:21]) is None  # truncated
    assert (
        parse_beacon(NK, _beacon(NK, 0, 1) + b"\xff") is not None
    )  # trailing bytes tolerated


def test_parse_beacon_compares_the_authentication_value_in_constant_time(
    monkeypatch: pytest.MonkeyPatch,
):
    """The one MAC compared in Python (the CCM tags are checked inside `cryptography`) goes through
    `hmac.compare_digest`, whatever bytes-like the payload slice is."""
    calls: list[tuple[bytes, bytes]] = []
    real = pdu_mod.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((bytes(a), bytes(b)))
        return real(a, b)

    monkeypatch.setattr(pdu_mod.hmac, "compare_digest", spy)
    good = _beacon(NK, 0x00, 42)
    b = parse_beacon(NK, bytearray(good))
    assert b is not None
    assert b.authenticated is True
    assert calls == [(good[14:22], good[14:22])]
    b = parse_beacon(NK, good[:-1] + bytes([good[-1] ^ 1]))
    assert b is not None
    assert b.authenticated is False


# ----------------------------------------------------------------------------- prefix fuzz


def _every_prefix(*pdus: bytes) -> list[bytes]:
    return [p[:n] for p in pdus for n in range(len(p) + 1)] + [
        p + b"\xff" for p in pdus
    ]


def test_network_decrypt_never_raises_on_any_prefix():
    """Every prefix (and a one-byte suffix) of valid access, control and proxy configuration PDUs: None or a
    NetworkPDU with a non-empty transport PDU, never an exception."""
    access = network_encrypt(
        NK,
        IV,
        False,
        3,
        0x1234,
        0x0148,
        0x0D00,
        lower_unsegmented_access(AK.aid, bytes(8)),
    )
    control = network_encrypt(
        NK, IV, True, 3, 0x1234, 0x0148, 0x0D00, segment_ack(1, 1)
    )
    config = network_encrypt(
        NK, IV, True, 0, 0x1234, 0x0148, 0x0000, b"\x03\x01\x00\x00", proxy=True
    )
    for proxy in (False, True):
        for pdu in _every_prefix(access, control, config):
            n = network_decrypt(NK, IV, pdu, proxy=proxy)
            if n is not None:
                assert n.transport_pdu
                assert is_unicast(n.src)
                assert proxy or n.dst != UNASSIGNED
    assert network_decrypt(NK, IV, access) is not None
    assert network_decrypt(NK, IV, control) is not None
    assert network_decrypt(NK, IV, config, proxy=True) is not None


def test_parse_lower_raises_only_value_error_on_any_prefix():
    upper = bytes(30)
    valid = [
        lower_unsegmented_access(AK.aid, bytes(15)),
        lower_unsegmented_access(0, bytes(5), akf=False),
        *lower_segments_access(AK.aid, 0x0123, upper),
        *lower_segments_access(AK.aid, 0x0123, upper, szmic=1),
        segment_ack(1, 1),
        b"\x0a\x00\x05\x00\x01",
        b"\x80\x00\x00\x00\x00",
    ]
    for ctl in (False, True):
        for pdu in _every_prefix(*valid):
            try:
                kind = parse_lower(pdu, ctl)
            except ValueError:
                continue
            assert kind[0] in ("ctl", "segctl", "unseg", "seg")
            if kind[0] == "seg":
                assert 1 <= len(kind[1].data) <= 12


def test_decode_opcode_raises_only_value_error_on_any_prefix():
    valid = [
        encode_opcode(0x52) + b"\x01",
        encode_opcode(0x8202) + b"\x01\x02",
        encode_opcode(0x11, 0x0527) + b"\x03",
    ]
    for pdu in _every_prefix(*valid):
        try:
            op, cid, params = decode_opcode(pdu)
        except ValueError:
            continue
        assert encode_opcode(op, cid) + params == pdu


def test_upper_decrypt_and_beacon_never_raise_on_any_prefix():
    upper = upper_encrypt_app(AK, IV, 0x000123, 0x0D00, 0x0148, b"\x82\x02\x01", 0)
    upper8 = upper_encrypt_app(AK, IV, 0x000123, 0x0D00, 0x0148, b"\x82\x02\x01", 1)
    for szmic, payload in ((0, upper), (1, upper8)):
        for p in _every_prefix(payload):
            got = upper_decrypt(AK.key, 0x01, IV, 0x000123, 0x0D00, 0x0148, p, szmic)
            assert got is None or p == payload
    for p in _every_prefix(_beacon(NK, 0x02, 42)):
        b = parse_beacon(NK, p)
        assert b is None or len(p) >= 22
    private = (
        b"\x02" + bytes(13) + ccm_encrypt(NK.private_beacon_key, bytes(13), bytes(5), 8)
    )
    for p in _every_prefix(private):
        assert (parse_private_beacon(NK, p) is None) is (len(p) < len(private))
    r = ProxyReassembler()
    for p in _every_prefix(b"\x00" + bytes(20), b"\x41\x01", b"\x81\x02", b"\xc1\x03"):
        r.feed(p)
