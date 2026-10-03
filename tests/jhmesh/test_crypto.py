"""jhmesh.crypto against the Mesh Profile 1.0.1 sample data (§8.1, §8.4.3, §8.6.2) and the Mesh Protocol 1.1 privacy."""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from jhmesh.crypto import (
    ZERO16,
    AppKeyMaterial,
    NetKeyMaterial,
    aes_cmac,
    aes_ecb,
    ccm_decrypt,
    ccm_encrypt,
    k1,
    k2,
    k3,
    k4,
    s1,
)

h = bytes.fromhex

SAMPLE_NETKEY = h("7dd7364cd842ad18c17c2b820c84c3d6")
SAMPLE_APPKEY = h("63964771734fbd76e3b40519d1d94a48")


def test_s1_sample():
    assert s1(b"test") == h("b73cefbd641ef2ea598c2b6efb62f79c")


def test_s1_is_cmac_with_zero_key():
    assert s1(b"abc") == aes_cmac(ZERO16, b"abc")


def test_k1_sample():
    n = h("3216d1509884b533248541792b877f98")
    salt = h("2ba14ffa0df84a2831938d57d276cab4")
    p = h("5a09d60797eeb4478aada59db3352a0d")
    assert k1(n, salt, p) == h("f6ed15a8934afbe7d83e8dcb57fcf5d7")


def test_k2_sample_master_credentials():
    nid, enc, priv = k2(h("f7a2a44f8e8a8029064f173ddc1e2b00"), b"\x00")
    assert nid == 0x7F
    assert enc == h("9f589181a0f50de73c8070c7a6d27f46")
    assert priv == h("4c715bd4a64b938f99b453351653124f")


def test_k3_sample():
    assert k3(h("f7a2a44f8e8a8029064f173ddc1e2b00")) == h("ff046958233db014")


def test_k4_sample():
    assert k4(h("3216d1509884b533248541792b877f98")) == 0x38


def test_netkey_material_sample():
    nk = NetKeyMaterial.derive(SAMPLE_NETKEY)
    assert nk.key == SAMPLE_NETKEY
    assert nk.nid == 0x68
    assert nk.enc_key == h("0953fa93e7caac9638f58820220a398e")
    assert nk.priv_key == h("8b84eedec100067d670971dd2aa700cf")
    assert nk.network_id == h("3ecaff672f673370")
    assert nk.identity_key == h("84396c435ac48560b5965385253e210c")
    assert nk.beacon_key == h("5423d967da639a99cb02231a83f7d254")


def test_private_beacon_key_sample():
    """Mesh Protocol 1.1 sample data, private beacon: NetKey f7a2…2b00 → PrivateBeaconKey 6be7…f1bb (k1, "nkpk")."""
    nk = NetKeyMaterial.derive(h("f7a2a44f8e8a8029064f173ddc1e2b00"))
    assert nk.private_beacon_key == h("6be76842460b2d3a5850d4698409f1bb")


def test_netkey_material_is_frozen():
    nk = NetKeyMaterial.derive(SAMPLE_NETKEY)
    with pytest.raises(AttributeError):
        nk.nid = 1  # type: ignore[misc]


def test_appkey_material_sample():
    ak = AppKeyMaterial.derive(SAMPLE_APPKEY)
    assert ak.key == SAMPLE_APPKEY
    assert ak.aid == 0x26


def test_node_identity_hash_sample():
    """§8.6.2: Random 34ae608fbbc1f2c6, address 0x1201 → Hash 00861765aefcc57b."""
    nk = NetKeyMaterial.derive(SAMPLE_NETKEY)
    assert nk.node_identity_hash(h("34ae608fbbc1f2c6"), 0x1201) == h("00861765aefcc57b")
    assert nk.node_identity_hash(h("34ae608fbbc1f2c6"), 0x1202) != h("00861765aefcc57b")
    assert len(nk.node_identity_hash(bytes(8), 1)) == 8


def _aes(key: bytes, block: bytes) -> bytes:
    """One AES-128 block straight from `cryptography`, not through `jhmesh.crypto`."""
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()  # noqa: S305  # the spec's e(k, p)
    return enc.update(block) + enc.finalize()


# The §8.6 sample's IdentityKey and Network ID (`test_netkey_material_sample`), with its Node Identity Random and address
SAMPLE_IDENTITY_KEY = h("84396c435ac48560b5965385253e210c")
SAMPLE_NETWORK_ID = h("3ecaff672f673370")
SAMPLE_RANDOM = h("34ae608fbbc1f2c6")


def test_private_network_identity_hash():
    """Mesh Protocol 1.1 §7.2.2.2.4: Hash = e(IdentityKey, Network ID ‖ Random) mod 2^64; the specification's sample
    data §8.6.3 (Service data using Private Network Identity): Hash d30f7229ef045435, the formula agreeing."""
    nk = NetKeyMaterial.derive(SAMPLE_NETKEY)
    want = _aes(SAMPLE_IDENTITY_KEY, SAMPLE_NETWORK_ID + SAMPLE_RANDOM)[8:]
    assert want == h("d30f7229ef045435")
    assert nk.private_network_identity(SAMPLE_RANDOM) == want
    assert nk.private_network_identity(bytes(8)) != want
    assert (
        NetKeyMaterial.derive(bytes(16)).private_network_identity(SAMPLE_RANDOM) != want
    )


def test_private_node_identity_hash():
    """Mesh Protocol 1.1 §7.2.2.2.5: Hash = e(IdentityKey, 0x0000000000 ‖ 0x03 ‖ Random ‖ Address) mod 2^64; the
    specification's sample data §8.6.4 (Service data using Private Node Identity): Hash 2c64a8cbca65bfe1 for address
    0x1201. The 0x03 keeps it apart from the Node Identity hash of the same Random and address
    (`test_node_identity_hash_sample`)."""
    nk = NetKeyMaterial.derive(SAMPLE_NETKEY)
    block = bytes(5) + b"\x03" + SAMPLE_RANDOM + (0x1201).to_bytes(2, "big")
    want = _aes(SAMPLE_IDENTITY_KEY, block)[8:]
    assert want == h("2c64a8cbca65bfe1")
    assert nk.private_node_identity(SAMPLE_RANDOM, 0x1201) == want
    assert nk.private_node_identity(SAMPLE_RANDOM, 0x1202) != want
    assert want != nk.node_identity_hash(SAMPLE_RANDOM, 0x1201)
    assert len(nk.private_node_identity(bytes(8), 1)) == 8


def test_beacon_authentication_sample():
    """§8.4.3: flags 0, network id 3ecaff672f673370, IV index 0x12345678 → auth 8ea261582f364f6f."""
    nk = NetKeyMaterial.derive(SAMPLE_NETKEY)
    body = b"\x00" + nk.network_id + (0x12345678).to_bytes(4, "big")
    assert aes_cmac(nk.beacon_key, body)[:8] == h("8ea261582f364f6f")


def test_aes_ecb_single_block():
    # FIPS-197 C.1 test vector
    key = h("000102030405060708090a0b0c0d0e0f")
    assert aes_ecb(key, h("00112233445566778899aabbccddeeff")) == h(
        "69c4e0d86a7b0430d8cdb78070b4c55a"
    )


@pytest.mark.parametrize("mic_len", [4, 8])
def test_ccm_round_trip(mic_len: int):
    key, nonce = bytes(range(16)), bytes(range(13))
    ct = ccm_encrypt(key, nonce, b"hello mesh", mic_len, aad=b"aad")
    assert len(ct) == len(b"hello mesh") + mic_len
    assert ccm_decrypt(key, nonce, ct, mic_len, aad=b"aad") == b"hello mesh"
    with pytest.raises(InvalidTag):
        ccm_decrypt(key, nonce, ct[:-1] + bytes([ct[-1] ^ 1]), mic_len, aad=b"aad")
    with pytest.raises(InvalidTag):
        ccm_decrypt(key, nonce, ct, mic_len, aad=b"other")
