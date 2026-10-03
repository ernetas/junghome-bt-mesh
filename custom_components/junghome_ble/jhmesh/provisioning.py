"""Provisioning a new node over PB-GATT as a provisioner: No-OOB authentication, FIPS P-256 ECDH (Mesh Profile 1.0.1 §5.4).

What the JUNG HOME app does (`docs/android/transport-provisioning.md` §3.3, `docs/android/network-logic.md` §3.1):
connect to the unprovisioned device's Mesh Provisioning Service (0x1827), send *Invite* with a 5 s attention timer,
then *Start* with algorithm 0 (BTM_ECDH_P256_CMAC_AES128_AES_CCM), no OOB public key and authentication method 0
(No OOB) — the only method the app ever uses, which is why every node of an export says `"security": "insecure"`.
Everything else here is the specification:

    Provisioner                         Device
    Invite (attention)          →
                                ←       Capabilities (elements, algorithms, OOB support)
    Start (0, 0, 0, 0, 0)       →
    Public Key (X ‖ Y)          →
                                ←       Public Key (X ‖ Y)
    Confirmation                →
                                ←       Confirmation
    Random                      →
                                ←       Random            (the provisioner checks the device's confirmation)
    Data (encrypted, MIC 8)     →
                                ←       Complete

`Provisioner` is the protocol as a transport-independent state machine: `start()` gives the Invite, `feed()` takes
each PDU from the device and returns what to send next, `result` holds the device key and element count once
*Complete* arrived. `provision()` drives it over a bleak-like GATT client: proxy PDUs of type 0x03 with the proxy
SAR framing (§6.3.1; `pdu.proxy_frame` / `ProxyReassembler`) on Mesh Provisioning Data In (0x2ADB, write without
response) / Data Out (0x2ADC, notify).

A provisioner never sends *Failed* — that PDU is the device's; the provisioner aborts by closing the link (§5.4.2).
So every failure here is a `ProvisioningError` for the caller, who then disconnects (`provision` leaves the
connection to its caller, like `ProxyClient.attach`). The device forgets a half-finished session when the link
closes; nothing reaches the device's storage before *Complete*.

The device key is known one step before the device learns anything: it is derived from the device's Random, before
the Data PDU goes out. `provision(on_device_key=…)` hands it over right there, so a caller can put it somewhere safe
first (Home Assistant's vault) and abort, by raising, before the device gets an address and the network's keys
(review-4 D15; unverified on air). Once the Data PDU went out, a failure no longer proves the device has nothing: a
lost *Complete* looks like any other timeout.

Key material (the ECDH secret, the session key, the NetKey inside `ProvisioningData`, the device key) never takes
part in a `repr()` and is never logged; only the PDU types and lengths are.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec

from .crypto import aes_cmac, ccm_decrypt, ccm_encrypt, k1, s1
from .pdu import BEACON_UNPROVISIONED, PROXY_PROVISIONING, ProxyReassembler, proxy_frame

log = logging.getLogger("jhmesh.provisioning")

MESH_PROVISIONING_SERVICE = "00001827-0000-1000-8000-00805f9b34fb"
MESH_PROVISIONING_DATA_IN = "00002adb-0000-1000-8000-00805f9b34fb"
MESH_PROVISIONING_DATA_OUT = "00002adc-0000-1000-8000-00805f9b34fb"

# provisioning PDU types (§5.4.1): the low 6 bits of the first octet, the top 2 bits are padding (0)
INVITE = 0x00
CAPABILITIES = 0x01
START = 0x02
PUBLIC_KEY = 0x03
INPUT_COMPLETE = 0x04
CONFIRMATION = 0x05
RANDOM = 0x06
DATA = 0x07
COMPLETE = 0x08
FAILED = 0x09
PDU_NAMES = {
    INVITE: "Invite",
    CAPABILITIES: "Capabilities",
    START: "Start",
    PUBLIC_KEY: "Public Key",
    INPUT_COMPLETE: "Input Complete",
    CONFIRMATION: "Confirmation",
    RANDOM: "Random",
    DATA: "Data",
    COMPLETE: "Complete",
    FAILED: "Failed",
}
# parameter length of every PDU the provisioner receives (§5.4.1); the others it only sends
_RECEIVED_LENGTHS = {
    CAPABILITIES: 11,
    PUBLIC_KEY: 64,
    CONFIRMATION: 16,
    RANDOM: 16,
    COMPLETE: 0,
    FAILED: 1,
}

# the error codes of a Provisioning Failed PDU (§5.4.1.10)
ERROR_NAMES = {
    0x00: "Prohibited",
    0x01: "Invalid PDU",
    0x02: "Invalid Format",
    0x03: "Unexpected PDU",
    0x04: "Confirmation Failed",
    0x05: "Out of Resources",
    0x06: "Decryption Failed",
    0x07: "Unexpected Error",
    0x08: "Cannot Assign Addresses",
}

ALGORITHM_FIPS_P256 = (
    0x00  # Start: BTM_ECDH_P256_CMAC_AES128_AES_CCM, the only algorithm of Mesh 1.0.x
)
ALGORITHMS_FIPS_P256_BIT = (
    0x0001  # Capabilities: the same algorithm as a bit of the Algorithms field
)
PUBLIC_KEY_NO_OOB = (
    0x00  # Start: the device's public key comes in a Public Key PDU, not out of band
)
AUTH_NO_OOB = 0x00  # Start: authentication method No OOB (AuthValue = 16 zero octets)
AUTH_VALUE_NO_OOB = bytes(16)

KEY_REFRESH_FLAG = 0x01  # Provisioning Data Flags (§5.4.2.5): bit 0 Key Refresh Phase 2, bit 1 IV Update active
IV_UPDATE_FLAG = 0x02

PROTOCOL_TIMEOUT = 60.0  # seconds without a PDU from the device: the provisioning protocol's own timer (§5.4)
GATT_WRITE_TIMEOUT = (
    5.0  # one write without response; normally milliseconds (as `client.GATT_TIMEOUT`)
)
DEFAULT_ATTENTION = 5  # seconds; what the app's `identifyNode(uuid)` asks for (transport-provisioning.md §3.3)

# the OOB Information field of the Unprovisioned Device beacon / PB-GATT service data (§3.9.2)
OOB_INFO_NAMES = {
    0: "other",
    1: "URI",
    2: "2D code",
    3: "bar code",
    4: "NFC",
    5: "number",
    6: "string",
    11: "on box",
    12: "inside box",
    13: "on paper",
    14: "inside manual",
    15: "on device",
}


class ProvisioningError(Exception):
    """Provisioning cannot go on: the device misbehaved, a check failed, or it took too long.

    The caller closes the link — that is how a provisioner aborts (module docstring). The message never carries
    key material.
    """


class ProvisioningFailed(ProvisioningError):
    """The device sent Provisioning Failed with `code` (`ERROR_NAMES`)."""

    def __init__(self, code: int) -> None:
        """Record the device's error code."""
        self.code = code
        super().__init__(
            f"device reported Provisioning Failed: {ERROR_NAMES.get(code, f'error {code:#04x}')}"
        )


class ProvisioningTimeout(ProvisioningError, TimeoutError):
    """The device did not send the next PDU within the protocol timeout."""


# ----------------------------------------------------------------------------- PDUs and their fields


@dataclass(frozen=True)
class Capabilities:
    """The device's Provisioning Capabilities (§5.4.1.2); multi-octet fields are big-endian, as in every PDU here."""

    elements: int
    algorithms: int
    public_key_type: int
    static_oob_type: int
    output_oob_size: int
    output_oob_action: int
    input_oob_size: int
    input_oob_action: int

    @classmethod
    def parse(cls, params: bytes) -> Capabilities:
        """Decode the 11 parameter octets (the caller checked the length)."""
        return cls(
            elements=params[0],
            algorithms=int.from_bytes(params[1:3], "big"),
            public_key_type=params[3],
            static_oob_type=params[4],
            output_oob_size=params[5],
            output_oob_action=int.from_bytes(params[6:8], "big"),
            input_oob_size=params[8],
            input_oob_action=int.from_bytes(params[9:11], "big"),
        )

    def pack(self) -> bytes:
        """Encode the 11 parameter octets (what the device sends; the provisioner hashes it into ConfirmationInputs)."""
        return (
            bytes([self.elements])
            + self.algorithms.to_bytes(2, "big")
            + bytes(
                [
                    self.public_key_type,
                    self.static_oob_type,
                    self.output_oob_size,
                ]
            )
            + self.output_oob_action.to_bytes(2, "big")
            + bytes([self.input_oob_size])
            + self.input_oob_action.to_bytes(2, "big")
        )


@dataclass(frozen=True)
class ProvisioningData:
    """What the new node receives in the Data PDU (§5.4.2.5): NetKey, its index, flags, IV index, primary unicast.

    The NetKey stays out of `repr()`.
    """

    net_key: bytes = field(repr=False)
    unicast: int
    key_index: int = 0
    iv_index: int = 0
    key_refresh: bool = False  # the NetKey is in key refresh phase 2
    iv_update: bool = False  # the network is in the IV Update procedure

    def __post_init__(self) -> None:
        """Refuse what the device could only answer with Failed (or worse, accept)."""
        if len(self.net_key) != 16:
            raise ValueError(f"NetKey must be 16 bytes, got {len(self.net_key)}")
        if not 0x0001 <= self.unicast <= 0x7FFF:
            raise ValueError(
                f"unicast address must be 0001..7FFF, got {self.unicast:#06x}"
            )
        if not 0 <= self.key_index <= 0xFFF:
            raise ValueError(f"NetKey index must be 0..4095, got {self.key_index}")
        if not 0 <= self.iv_index <= 0xFFFFFFFF:
            raise ValueError(f"IV index must be a 32-bit value, got {self.iv_index}")

    @property
    def flags(self) -> int:
        """Return the Flags octet: bit 0 Key Refresh, bit 1 IV Update."""
        return (KEY_REFRESH_FLAG if self.key_refresh else 0) | (
            IV_UPDATE_FLAG if self.iv_update else 0
        )

    def pack(self) -> bytes:
        """Encode the 25 octets: `NetKey 16 ‖ KeyIndex u16 ‖ Flags u8 ‖ IVIndex u32 ‖ UnicastAddress u16`, big-endian."""
        return (
            self.net_key
            + self.key_index.to_bytes(2, "big")
            + bytes([self.flags])
            + self.iv_index.to_bytes(4, "big")
            + self.unicast.to_bytes(2, "big")
        )

    @classmethod
    def unpack(cls, data: bytes) -> ProvisioningData:
        """Decode the 25 octets (the device side; tests use it to check the round trip)."""
        if len(data) != 25:
            raise ValueError(f"provisioning data must be 25 bytes, got {len(data)}")
        flags = data[18]
        return cls(
            net_key=data[:16],
            key_index=int.from_bytes(data[16:18], "big"),
            key_refresh=bool(flags & KEY_REFRESH_FLAG),
            iv_update=bool(flags & IV_UPDATE_FLAG),
            iv_index=int.from_bytes(data[19:23], "big"),
            unicast=int.from_bytes(data[23:25], "big"),
        )


@dataclass(frozen=True)
class ProvisioningResult:
    """A provisioned node: its primary unicast, element count, capabilities and device key (not in `repr()`)."""

    unicast: int
    elements: int
    device_key: bytes = field(repr=False)
    capabilities: Capabilities

    @property
    def addresses(self) -> range:
        """Return the unicast addresses the node's elements now occupy."""
        return range(self.unicast, self.unicast + self.elements)


def pdu(pdu_type: int, params: bytes = b"") -> bytes:
    """Build a provisioning PDU: the type octet (padding bits 0) followed by its parameters."""
    return bytes([pdu_type]) + params


def invite(attention: int = DEFAULT_ATTENTION) -> bytes:
    """Provisioning Invite: `[Attention Timer u8]` seconds the device should draw attention to itself."""
    if not 0 <= attention <= 0xFF:
        raise ValueError(f"attention timer must be 0..255 s, got {attention}")
    return pdu(INVITE, bytes([attention]))


def start_no_oob() -> bytes:
    """Provisioning Start as the app sends it: FIPS P-256, no OOB public key, No OOB authentication, action/size 0."""
    return pdu(
        START, bytes([ALGORITHM_FIPS_P256, PUBLIC_KEY_NO_OOB, AUTH_NO_OOB, 0, 0])
    )


# ----------------------------------------------------------------------------- key derivation (§5.4.2.4, §5.4.2.5)


def generate_private_key() -> bytes:
    """Return a fresh P-256 private scalar (32 octets, big-endian)."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_numbers().private_value.to_bytes(32, "big")


def _private_key(scalar: bytes) -> ec.EllipticCurvePrivateKey:
    if len(scalar) != 32:
        raise ValueError(f"P-256 private key must be 32 bytes, got {len(scalar)}")
    return ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())


def public_key_bytes(scalar: bytes) -> bytes:
    """Return the Public Key PDU value of a private scalar: X ‖ Y, 32 octets each, big-endian."""
    numbers = _private_key(scalar).public_key().public_numbers()
    return numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")


def ecdh_secret(private_scalar: bytes, peer_public: bytes) -> bytes:
    """ECDHSecret: the X coordinate of the shared P-256 point (32 octets).

    `peer_public` (X ‖ Y) must be a point on the curve — `cryptography` checks that and raises `ValueError` for
    one that is not (an invalid-curve attack would otherwise leak bits of our private key).
    """
    if len(peer_public) != 64:
        raise ValueError(f"P-256 public key must be 64 bytes, got {len(peer_public)}")
    peer = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), b"\x04" + peer_public
    )
    return _private_key(private_scalar).exchange(ec.ECDH(), peer)


def confirmation_salt(confirmation_inputs: bytes) -> bytes:
    """ConfirmationSalt = s1(ConfirmationInputs)."""
    return s1(confirmation_inputs)


def confirmation_key(secret: bytes, salt: bytes) -> bytes:
    """ConfirmationKey = k1(ECDHSecret, ConfirmationSalt, "prck")."""
    return k1(secret, salt, b"prck")


def confirmation(
    key: bytes, random: bytes, auth_value: bytes = AUTH_VALUE_NO_OOB
) -> bytes:
    """Return Confirmation = AES-CMAC_ConfirmationKey(Random ‖ AuthValue)."""
    return aes_cmac(key, random + auth_value)


def provisioning_salt(
    conf_salt: bytes, random_provisioner: bytes, random_device: bytes
) -> bytes:
    """ProvisioningSalt = s1(ConfirmationSalt ‖ RandomProvisioner ‖ RandomDevice)."""
    return s1(conf_salt + random_provisioner + random_device)


def session_key(secret: bytes, prov_salt: bytes) -> bytes:
    """SessionKey = k1(ECDHSecret, ProvisioningSalt, "prsk")."""
    return k1(secret, prov_salt, b"prsk")


def session_nonce(secret: bytes, prov_salt: bytes) -> bytes:
    """SessionNonce = the 13 least significant octets of k1(ECDHSecret, ProvisioningSalt, "prsn")."""
    return k1(secret, prov_salt, b"prsn")[-13:]


def device_key(secret: bytes, prov_salt: bytes) -> bytes:
    """DeviceKey = k1(ECDHSecret, ProvisioningSalt, "prdk")."""
    return k1(secret, prov_salt, b"prdk")


def encrypt_provisioning_data(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """Encrypted Provisioning Data ‖ MIC: AES-CCM under the session key and nonce, 8-octet MIC (33 octets)."""
    return ccm_encrypt(key, nonce, data, 8)


def decrypt_provisioning_data(key: bytes, nonce: bytes, encrypted: bytes) -> bytes:
    """Decrypt as the device does (`encrypt_provisioning_data` reversed); `InvalidTag` on a bad MIC."""
    return ccm_decrypt(key, nonce, encrypted, 8)


# ----------------------------------------------------------------------------- the state machine


class State:
    """Where a `Provisioner` stands: the PDU it waits for is in `Provisioner.expecting`."""

    IDLE = "idle"
    INVITE_SENT = "invite sent"
    PUBLIC_KEY_SENT = "public key sent"
    CONFIRMATION_SENT = "confirmation sent"
    RANDOM_SENT = "random sent"
    DATA_SENT = "data sent"
    COMPLETE = "complete"
    FAILED = "failed"


_EXPECTED = {
    State.INVITE_SENT: CAPABILITIES,
    State.PUBLIC_KEY_SENT: PUBLIC_KEY,
    State.CONFIRMATION_SENT: CONFIRMATION,
    State.RANDOM_SENT: RANDOM,
    State.DATA_SENT: COMPLETE,
}


class Provisioner:
    """The provisioner side of one provisioning session (No OOB, FIPS P-256), independent of any transport.

    `private_key` (a P-256 scalar) and `random` (RandomProvisioner) may be injected so a run is deterministic —
    the tests replay the specification's sample data (§8.7) that way; left out, both are fresh random values.
    Once `feed` raised, the session is over (`state` is `FAILED`) and every further `feed` raises too.
    """

    def __init__(
        self,
        data: ProvisioningData,
        *,
        attention: int = DEFAULT_ATTENTION,
        private_key: bytes | None = None,
        random: bytes | None = None,
    ) -> None:
        """Prepare a session that will hand `data` to the device."""
        self.data = data
        self._invite = invite(attention)
        self._private = (
            private_key if private_key is not None else generate_private_key()
        )
        self.public_key = public_key_bytes(self._private)
        self._random = random if random is not None else os.urandom(16)
        if len(self._random) != 16:
            raise ValueError(
                f"RandomProvisioner must be 16 bytes, got {len(self._random)}"
            )
        self.state = State.IDLE
        self.capabilities: Capabilities | None = None
        self._inputs = b""  # ConfirmationInputs, built as the PDUs go by
        self._secret = b""
        self._conf_salt = b""
        self._conf_key = b""
        self._our_confirmation = b""
        self._device_confirmation = b""
        self._device_key = b""
        self.result: ProvisioningResult | None = None

    def __repr__(self) -> str:
        """Show where the session stands, never a key."""
        return f"Provisioner(unicast={self.data.unicast:#06x}, state={self.state!r})"

    @property
    def done(self) -> bool:
        """Tell whether the device answered Complete."""
        return self.state == State.COMPLETE

    @property
    def derived_key(self) -> bytes | None:
        """The device key once the device's Random verified (before the Data PDU is sent); None before."""
        return self._device_key or None

    @property
    def expecting(self) -> str:
        """Name the PDU the session waits for (for error messages and logs)."""
        expected = _EXPECTED.get(self.state)
        return PDU_NAMES[expected] if expected is not None else "nothing"

    def start(self) -> bytes:
        """Begin the session: return the Invite PDU to send."""
        if self.state != State.IDLE:
            raise ProvisioningError(f"session already started ({self.state})")
        self.state = State.INVITE_SENT
        self._inputs = self._invite[1:]
        return self._invite

    def feed(self, received: bytes) -> list[bytes]:
        """Take one provisioning PDU from the device; return the PDUs to send in reply, in order.

        Raises `ProvisioningFailed` for the device's Failed PDU and `ProvisioningError` for anything the
        protocol does not allow at this point (an unexpected or malformed PDU, a public key not on the curve,
        a confirmation that does not verify); the session is then over.
        """
        try:
            return self._feed(received)
        except ProvisioningError:
            self.state = State.FAILED
            raise

    def _feed(self, received: bytes) -> list[bytes]:
        if self.state in (State.IDLE, State.COMPLETE, State.FAILED):
            raise ProvisioningError(f"no provisioning PDU expected ({self.state})")
        if not received:
            raise ProvisioningError("empty provisioning PDU")
        if received[0] & 0xC0:
            raise ProvisioningError(
                f"provisioning PDU padding bits set ({received[0]:#04x})"
            )
        pdu_type, params = received[0], received[1:]
        name = PDU_NAMES.get(pdu_type, f"type {pdu_type:#04x}")
        if pdu_type == FAILED and len(params) == 1:
            raise ProvisioningFailed(params[0])
        if pdu_type != _EXPECTED[self.state]:
            raise ProvisioningError(
                f"unexpected {name} while waiting for {self.expecting}"
            )
        if len(params) != _RECEIVED_LENGTHS[pdu_type]:
            raise ProvisioningError(f"{name} has {len(params)} parameter bytes")
        log.debug("provisioning: received %s", name)
        handler = {
            CAPABILITIES: self._on_capabilities,
            PUBLIC_KEY: self._on_public_key,
            CONFIRMATION: self._on_confirmation,
            RANDOM: self._on_random,
            COMPLETE: self._on_complete,
        }[pdu_type]
        return handler(params)

    def _on_capabilities(self, params: bytes) -> list[bytes]:
        caps = Capabilities.parse(params)
        if caps.elements == 0:
            raise ProvisioningError(
                "device reports 0 elements"
            )  # prohibited by §5.4.1.2
        if not caps.algorithms & ALGORITHMS_FIPS_P256_BIT:
            raise ProvisioningError(
                f"device does not offer FIPS P-256 (algorithms {caps.algorithms:#06x})"
            )
        self.capabilities = caps
        start = start_no_oob()
        self._inputs += params + start[1:] + self.public_key
        self.state = State.PUBLIC_KEY_SENT
        return [start, pdu(PUBLIC_KEY, self.public_key)]

    def _on_public_key(self, params: bytes) -> list[bytes]:
        if hmac.compare_digest(params, self.public_key):
            # a device that echoes our own key would share a secret it never had to compute
            raise ProvisioningError("device public key equals the provisioner's")
        try:
            self._secret = ecdh_secret(self._private, params)
        except ValueError as err:
            raise ProvisioningError(
                "device public key is not a valid P-256 point"
            ) from err
        self._inputs += params
        self._conf_salt = confirmation_salt(self._inputs)
        self._conf_key = confirmation_key(self._secret, self._conf_salt)
        self._our_confirmation = confirmation(self._conf_key, self._random)
        self.state = State.CONFIRMATION_SENT
        return [pdu(CONFIRMATION, self._our_confirmation)]

    def _on_confirmation(self, params: bytes) -> list[bytes]:
        if hmac.compare_digest(params, self._our_confirmation):
            # reflected: a device that only mirrors our confirmation (and later our random) would pass the check
            raise ProvisioningError("device confirmation equals the provisioner's")
        self._device_confirmation = params
        self.state = State.RANDOM_SENT
        return [pdu(RANDOM, self._random)]

    def _on_random(self, params: bytes) -> list[bytes]:
        expected = confirmation(self._conf_key, params)
        if not hmac.compare_digest(expected, self._device_confirmation):
            raise ProvisioningError(
                "device confirmation does not verify (Confirmation Failed)"
            )
        salt = provisioning_salt(self._conf_salt, self._random, params)
        encrypted = encrypt_provisioning_data(
            session_key(self._secret, salt),
            session_nonce(self._secret, salt),
            self.data.pack(),
        )
        self._device_key = device_key(self._secret, salt)
        self.state = State.DATA_SENT
        return [pdu(DATA, encrypted)]

    def _on_complete(self, params: bytes) -> list[bytes]:
        assert self.capabilities is not None
        self.result = ProvisioningResult(
            unicast=self.data.unicast,
            elements=self.capabilities.elements,
            device_key=self._device_key,
            capabilities=self.capabilities,
        )
        self.state = State.COMPLETE
        return []


# ----------------------------------------------------------------------------- PB-GATT driver


async def provision(
    client: Any,
    data: ProvisioningData,
    *,
    attention: int = DEFAULT_ATTENTION,
    timeout: float = PROTOCOL_TIMEOUT,
    check: Callable[[Capabilities], None] | None = None,
    provisioner: Provisioner | None = None,
    on_device_key: Callable[[bytes], Awaitable[None]] | None = None,
) -> ProvisioningResult:
    """Provision the device behind `client`, an already-connected bleak-like client of its 0x1827 service.

    `client` needs `start_notify` / `stop_notify` / `write_gatt_char` and optionally `mtu_size` (as for
    `ProxyClient.attach`). `check` sees the device's capabilities before anything is sent in reply and may raise
    (a `ProvisioningError`, say, when the node's element count does not fit at the unicast address) to abort
    before the device learns any address. Each PDU from the device must arrive within `timeout` seconds.
    `on_device_key` is awaited with the device key once it is derived, before the Data PDU is sent (module
    docstring): whatever it raises aborts the session before the device learns its address or a key. Optional:
    without it nothing changes (unverified on air).

    The caller owns the connection and closes it afterwards — on success (the node leaves the provisioning
    service for the proxy service anyway) and on any error (which is how a provisioner aborts). `provisioner`
    lets a caller inject a prepared session (deterministic keys in tests).
    """
    prov = (
        provisioner
        if provisioner is not None
        else Provisioner(data, attention=attention)
    )
    received: asyncio.Queue[bytes] = asyncio.Queue()
    reasm = ProxyReassembler()

    def on_notify(_char: Any, frame: bytearray) -> None:
        whole = reasm.feed(bytes(frame))
        if whole is None:
            return
        msg_type, payload = whole
        if msg_type != PROXY_PROVISIONING:
            log.debug(
                "proxy PDU of type %d on the provisioning service ignored", msg_type
            )
            return
        received.put_nowait(payload)

    await client.start_notify(MESH_PROVISIONING_DATA_OUT, on_notify)
    try:
        await _write(client, prov.start())
        checked = False
        while not prov.done:
            waiting_for = prov.expecting
            try:
                incoming = await asyncio.wait_for(received.get(), timeout)
            except TimeoutError:
                prov.state = State.FAILED
                raise ProvisioningTimeout(
                    f"no {waiting_for} from the device within {timeout:g}s"
                ) from None
            replies = prov.feed(incoming)
            if not checked and prov.capabilities is not None:
                checked = True
                if check is not None:
                    try:
                        check(prov.capabilities)
                    except BaseException:
                        prov.state = State.FAILED
                        raise
            # the Random verified: `replies` is the Data PDU and the device key is known (`_on_random`). One
            # iteration only: the next PDU ends the session (Complete) or fails it
            if on_device_key is not None and prov.state == State.DATA_SENT:
                key = prov.derived_key
                assert key is not None
                try:
                    await on_device_key(key)
                except BaseException:
                    prov.state = State.FAILED
                    raise
            for reply in replies:
                await _write(client, reply)
    finally:
        try:
            await asyncio.wait_for(
                client.stop_notify(MESH_PROVISIONING_DATA_OUT), GATT_WRITE_TIMEOUT
            )
        except Exception:
            log.debug("stop_notify on the provisioning service failed", exc_info=True)
    assert prov.result is not None
    log.info(
        "provisioned unicast %04X with %d element(s)",
        prov.result.unicast,
        prov.result.elements,
    )
    return prov.result


async def _write(client: Any, provisioning_pdu: bytes) -> None:
    """Write one provisioning PDU as proxy PDUs of type 0x03, SAR-framed to the link's MTU."""
    mtu = getattr(client, "mtu_size", None) or 23
    log.debug(
        "provisioning: sending %s (%d bytes)",
        PDU_NAMES[provisioning_pdu[0]],
        len(provisioning_pdu),
    )
    for frame in proxy_frame(PROXY_PROVISIONING, provisioning_pdu, mtu - 3):
        try:
            await asyncio.wait_for(
                client.write_gatt_char(
                    MESH_PROVISIONING_DATA_IN, frame, response=False
                ),
                GATT_WRITE_TIMEOUT,
            )
        except TimeoutError:
            raise ProvisioningError(
                f"provisioning write not completed within {GATT_WRITE_TIMEOUT:g}s"
            ) from None
        except (
            Exception
        ) as err:  # bleak.BleakError, OSError, … → one type for the caller
            raise ProvisioningError(f"provisioning write failed: {err}") from err


# ----------------------------------------------------------------------------- unprovisioned devices


@dataclass(frozen=True)
class UnprovisionedDevice:
    """A device advertising the Mesh Provisioning Service (PB-GATT, §7.1): its Device UUID and OOB Information.

    The JUNG app keys unprovisioned devices by their MAC and reads the UUID from the same service data, ignoring
    the OOB field (`docs/android/transport-provisioning.md` §2.2).
    """

    uuid: str  # canonical: upper-case, dashed (as the export writes node UUIDs)
    oob_info: int
    address: str = ""
    rssi: int = 0
    name: str | None = None
    product_id: int | None = (
        None  # from the JUNG manufacturer record, when one was in the advertisement
    )
    device: Any = field(default=None, compare=False, repr=False)  # backend BLEDevice

    @property
    def oob_names(self) -> list[str]:
        """Name the OOB Information bits that are set."""
        return [
            OOB_INFO_NAMES.get(bit, f"bit {bit}")
            for bit in range(16)
            if self.oob_info >> bit & 1
        ]


def parse_provisioning_service_data(data: bytes) -> tuple[str, int] | None:
    """Read the 0x1827 service data: Device UUID (16 octets) ‖ OOB Information (u16, big-endian); None if malformed."""
    if len(data) != 18:
        return None
    raw = data[:16].hex().upper()
    uuid = f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    return uuid, int.from_bytes(data[16:18], "big")


def parse_unprovisioned_beacon(
    payload: bytes,
) -> tuple[UnprovisionedDevice, bytes | None] | None:
    """Read an Unprovisioned Device beacon (§3.9.2): type 0x00 ‖ the service data's UUID and OOB ‖ URI Hash (4, optional).

    None when it is not one. The URI hash (of the URI the OOB Information points at) is None when the beacon has none.
    """
    parsed = (
        parse_provisioning_service_data(payload[1:19])
        if payload[:1] == bytes([BEACON_UNPROVISIONED])
        else None
    )
    if parsed is None:
        return None
    uri_hash = bytes(payload[19:23]) if len(payload) >= 23 else None
    return UnprovisionedDevice(*parsed), uri_hash
