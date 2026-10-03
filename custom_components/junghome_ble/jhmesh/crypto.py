"""Bluetooth Mesh security functions (Mesh Profile 1.0.1 §3.8, Mesh Protocol 1.1 privacy) — pure Python on `cryptography`."""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptography.hazmat.primitives import cmac
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

ZERO16 = bytes(16)


def aes_cmac(key: bytes, msg: bytes) -> bytes:
    """AES-CMAC of `msg` under `key`."""
    c = cmac.CMAC(algorithms.AES(key))
    c.update(msg)
    return c.finalize()


def aes_ecb(key: bytes, block: bytes) -> bytes:
    """e(k, p): encrypt one 16-byte block with AES-128."""
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()  # noqa: S305  # the spec's e(k, p) is one raw AES-128 block: ECB *is* the primitive
    return enc.update(block) + enc.finalize()


def s1(m: bytes) -> bytes:
    """Salt generation function s1: AES-CMAC of `m` under the all-zero key."""
    return aes_cmac(ZERO16, m)


def k1(n: bytes, salt: bytes, p: bytes) -> bytes:
    """Apply derivation function k1 (behind the identity and beacon keys)."""
    return aes_cmac(aes_cmac(salt, n), p)


def k2(n: bytes, p: bytes) -> tuple[int, bytes, bytes]:
    """Return (NID, EncryptionKey, PrivacyKey)."""
    t = aes_cmac(s1(b"smk2"), n)
    t1 = aes_cmac(t, p + b"\x01")
    t2 = aes_cmac(t, t1 + p + b"\x02")
    t3 = aes_cmac(t, t2 + p + b"\x03")
    return t1[15] & 0x7F, t2, t3


def k3(n: bytes) -> bytes:
    """Apply derivation function k3: the 64-bit Network ID of a NetKey."""
    t = aes_cmac(s1(b"smk3"), n)
    return aes_cmac(t, b"id64" + b"\x01")[8:]


def k4(n: bytes) -> int:
    """Apply derivation function k4: the 6-bit AID of an AppKey."""
    t = aes_cmac(s1(b"smk4"), n)
    return aes_cmac(t, b"id6" + b"\x01")[15] & 0x3F


def ccm_encrypt(
    key: bytes, nonce: bytes, plaintext: bytes, mic_len: int, aad: bytes = b""
) -> bytes:
    """AES-CCM encrypt, returning the ciphertext followed by a `mic_len`-byte MIC."""
    return AESCCM(key, tag_length=mic_len).encrypt(nonce, plaintext, aad)


def ccm_decrypt(
    key: bytes, nonce: bytes, ciphertext_and_mic: bytes, mic_len: int, aad: bytes = b""
) -> bytes:
    """AES-CCM decrypt and verify the MIC; raises `InvalidTag` when it does not match."""
    return AESCCM(key, tag_length=mic_len).decrypt(nonce, ciphertext_and_mic, aad)


@dataclass(frozen=True)
class NetKeyMaterial:
    """A NetKey with everything derived from it (master security credentials).

    Only the NID and the Network ID (both public: they are on air in every PDU / advertisement) take part in
    `repr()`; the key material never does, so a logged CDB or client cannot leak it.
    """

    key: bytes = field(repr=False)
    nid: int
    enc_key: bytes = field(repr=False)
    priv_key: bytes = field(repr=False)
    network_id: bytes
    identity_key: bytes = field(repr=False)
    beacon_key: bytes = field(repr=False)
    private_beacon_key: bytes = field(repr=False)

    @classmethod
    def derive(cls, netkey: bytes) -> NetKeyMaterial:
        """Derive NID, encryption/privacy keys, Network ID, identity, beacon and private beacon keys from `netkey`."""
        nid, enc, priv = k2(netkey, b"\x00")  # master security credentials
        return cls(
            key=netkey,
            nid=nid,
            enc_key=enc,
            priv_key=priv,
            network_id=k3(netkey),
            identity_key=k1(netkey, s1(b"nkik"), b"id128" + b"\x01"),
            beacon_key=k1(netkey, s1(b"nkbk"), b"id128" + b"\x01"),
            private_beacon_key=k1(netkey, s1(b"nkpk"), b"id128" + b"\x01"),
        )

    def node_identity_hash(self, random8: bytes, address: int) -> bytes:
        """Hash used in the Node Identity proxy advertisement (§7.2.2.2.3)."""
        return aes_ecb(
            self.identity_key, bytes(6) + random8 + address.to_bytes(2, "big")
        )[8:]

    def private_network_identity(self, random8: bytes) -> bytes:
        """Hash used in the Private Network Identity proxy advertisement (Mesh Protocol 1.1 §7.2.2.2.4).

        e(IdentityKey, Network ID ‖ Random) mod 2^64: the Network ID is no longer in the clear, so only a holder of
        the NetKey can tell which network a proxy with Proxy Privacy on belongs to.
        """
        return aes_ecb(self.identity_key, self.network_id + random8)[8:]

    def private_node_identity(self, random8: bytes, address: int) -> bytes:
        """Hash used in the Private Node Identity proxy advertisement (Mesh Protocol 1.1 §7.2.2.2.5).

        e(IdentityKey, Padding ‖ 0x03 ‖ Random ‖ Address) mod 2^64 with 5 octets of zero padding: the Node Identity
        block (`node_identity_hash`) with the identification type in its last padding octet, so neither hash can be
        passed off as the other.
        """
        block = bytes(5) + b"\x03" + random8 + address.to_bytes(2, "big")
        return aes_ecb(self.identity_key, block)[8:]


@dataclass(frozen=True)
class AppKeyMaterial:
    """An AppKey with its 6-bit AID (the key itself stays out of `repr()`)."""

    key: bytes = field(repr=False)
    aid: int

    @classmethod
    def derive(cls, appkey: bytes) -> AppKeyMaterial:
        """Derive the AID of `appkey`."""
        return cls(key=appkey, aid=k4(appkey))
