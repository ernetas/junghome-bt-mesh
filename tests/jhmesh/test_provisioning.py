"""jhmesh.provisioning: the Mesh Profile 1.0.1 §8.7 provisioning sample data, and a PB-GATT run against a fake device.

Every §8.7 value below is checked by computation, not only against the next one: the public keys follow from the
private keys, the ECDH secret from both, and so on down to the encrypted provisioning data — so the table is
self-consistent, and a single wrong digit anywhere would fail the chain.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from cryptography.exceptions import InvalidTag

from jhmesh import provisioning as P
from jhmesh.crypto import hmac_sha256
from jhmesh.pdu import PROXY_CONFIG, PROXY_PROVISIONING, ProxyReassembler, proxy_frame

h = bytes.fromhex

# ----------------------------------------------------------------------------- Mesh Profile 1.0.1 §8.7

PROV_PRIVATE = h("06a516693c9aa31a6084545d0c5db641b48572b97203ddffb7ac73f7d0457663")
PROV_PUBLIC = h(
    "2c31a47b5779809ef44cb5eaaf5c3e43d5f8faad4a8794cb987e9b03745c78dd"
    "919512183898dfbecd52e2408e43871fd021109117bd3ed4eaf8437743715d4f"
)
DEV_PRIVATE = h("529aa0670d72cd6497502ed473502b037e8803b5c60829a5a3caa219505530ba")
DEV_PUBLIC = h(
    "f465e43ff23d3f1b9dc7dfc04da8758184dbc966204796eccf0d6cf5e16500cc"
    "0201d048bcbbd899eeefc424164e33c201c2b010ca6b4d43a8a155cad8ecb279"
)
ECDH_SECRET = h("ab85843a2f6d883f62e5684b38e307335fe6e1945ecd19604105c6f23221eb69")
INVITE_VALUE = h("00")  # attention timer 0
CAPABILITIES_VALUE = h(
    "0100010000000000000000"
)  # 1 element, FIPS P-256, no OOB of any kind
START_VALUE = h("0000000000")
CONFIRMATION_INPUTS = (
    INVITE_VALUE + CAPABILITIES_VALUE + START_VALUE + PROV_PUBLIC + DEV_PUBLIC
)
CONFIRMATION_SALT = h("5faabe187337c71cc6c973369dcaa79a")
CONFIRMATION_KEY = h("e31fe046c68ec339c425fc6629f0336f")
RANDOM_PROVISIONER = h("8b19ac31d58b124c946209b5db1021b9")
RANDOM_DEVICE = h("55a2a2bca04cd32ff6f346bd0a0c1a3a")
CONFIRMATION_PROVISIONER = h("b38a114dfdca1fe153bd2c1e0dc46ac2")
CONFIRMATION_DEVICE = h("eeba521c196b52cc2e37aa40329f554e")
PROVISIONING_SALT = h("a21c7d45f201cf9489a2fb57145015b4")
SESSION_KEY = h("c80253af86b33dfa450bbdb2a191fea3")
SESSION_NONCE = h("da7ddbe78b5f62b81d6847487e")
DEVICE_KEY = h("0520adad5e0142aa3e325087b4ec16d8")
NET_KEY = h("efb2255e6422d330088e09bb015ed707")
SAMPLE_DATA = P.ProvisioningData(
    net_key=NET_KEY, key_index=0x0567, iv_index=0x01020304, unicast=0x0B0C
)
PROVISIONING_DATA = h("efb2255e6422d330088e09bb015ed707056700010203040b0c")
ENCRYPTED_DATA = h("d0bd7f4a89a2ff6222af59a90a60ad58acfe3123356f5cec29")
DATA_MIC = h("73e0ec50783b10c7")


def test_sample_public_keys_and_ecdh_secret():
    assert P.public_key_bytes(PROV_PRIVATE) == PROV_PUBLIC
    assert P.public_key_bytes(DEV_PRIVATE) == DEV_PUBLIC
    assert P.ecdh_secret(PROV_PRIVATE, DEV_PUBLIC) == ECDH_SECRET
    assert P.ecdh_secret(DEV_PRIVATE, PROV_PUBLIC) == ECDH_SECRET


def test_sample_confirmation():
    assert P.confirmation_salt(CONFIRMATION_INPUTS) == CONFIRMATION_SALT
    assert P.confirmation_key(ECDH_SECRET, CONFIRMATION_SALT) == CONFIRMATION_KEY
    assert (
        P.confirmation(CONFIRMATION_KEY, RANDOM_PROVISIONER) == CONFIRMATION_PROVISIONER
    )
    assert P.confirmation(CONFIRMATION_KEY, RANDOM_DEVICE) == CONFIRMATION_DEVICE


def test_sample_session_and_device_key():
    salt = P.provisioning_salt(CONFIRMATION_SALT, RANDOM_PROVISIONER, RANDOM_DEVICE)
    assert salt == PROVISIONING_SALT
    assert P.session_key(ECDH_SECRET, salt) == SESSION_KEY
    assert P.session_nonce(ECDH_SECRET, salt) == SESSION_NONCE
    assert P.device_key(ECDH_SECRET, salt) == DEVICE_KEY


def test_sample_provisioning_data():
    assert SAMPLE_DATA.pack() == PROVISIONING_DATA
    encrypted = P.encrypt_provisioning_data(
        SESSION_KEY, SESSION_NONCE, PROVISIONING_DATA
    )
    assert encrypted == ENCRYPTED_DATA + DATA_MIC
    assert (
        P.decrypt_provisioning_data(SESSION_KEY, SESSION_NONCE, encrypted)
        == PROVISIONING_DATA
    )
    assert P.ProvisioningData.unpack(PROVISIONING_DATA) == SAMPLE_DATA
    with pytest.raises(InvalidTag):
        P.decrypt_provisioning_data(
            SESSION_KEY, SESSION_NONCE, encrypted[:-1] + bytes([encrypted[-1] ^ 1])
        )


def sample_session(**kw: Any) -> P.Provisioner:
    return P.Provisioner(
        SAMPLE_DATA,
        attention=0,
        private_key=PROV_PRIVATE,
        random=RANDOM_PROVISIONER,
        **kw,
    )


def test_state_machine_replays_the_sample_exchange():
    prov = sample_session()
    assert prov.start() == h("00") + INVITE_VALUE
    assert prov.expecting == "Capabilities"
    out = prov.feed(h("01") + CAPABILITIES_VALUE)
    assert out == [h("02") + START_VALUE, h("03") + PROV_PUBLIC]
    assert prov.capabilities == P.Capabilities(1, 1, 0, 0, 0, 0, 0, 0)
    assert prov.feed(h("03") + DEV_PUBLIC) == [h("05") + CONFIRMATION_PROVISIONER]
    assert prov.feed(h("05") + CONFIRMATION_DEVICE) == [h("06") + RANDOM_PROVISIONER]
    assert prov.feed(h("06") + RANDOM_DEVICE) == [h("07") + ENCRYPTED_DATA + DATA_MIC]
    assert not prov.done
    assert prov.expecting == "Complete"
    assert prov.feed(h("08")) == []
    assert prov.done
    assert prov.expecting == "nothing"
    result = prov.result
    assert result is not None
    assert result.device_key == DEVICE_KEY
    assert result.elements == 1
    assert result.unicast == 0x0B0C
    assert list(result.addresses) == [0x0B0C]
    assert result.capabilities.pack() == CAPABILITIES_VALUE


# ----------------------------------------------------------------------------- fields, builders, repr


def test_key_material_stays_out_of_repr():
    prov = sample_session()
    prov.start()
    for pdu_ in (
        h("01") + CAPABILITIES_VALUE,
        h("03") + DEV_PUBLIC,
        h("05") + CONFIRMATION_DEVICE,
        h("06") + RANDOM_DEVICE,
        h("08"),
    ):
        prov.feed(pdu_)
    for text in (repr(prov), repr(SAMPLE_DATA), repr(prov.result)):
        for secret in (NET_KEY, DEVICE_KEY, SESSION_KEY, ECDH_SECRET, PROV_PRIVATE):
            assert secret.hex() not in text.lower()
    assert repr(prov) == "Provisioner(unicast=0x0b0c, state='complete')"


def test_provisioning_data_flags_and_validation():
    data = P.ProvisioningData(bytes(16), 0x0D10, key_refresh=True, iv_update=True)
    assert data.flags == 3
    assert data.pack()[18] == 3
    assert P.ProvisioningData.unpack(data.pack()) == data
    assert P.ProvisioningData(bytes(16), 1, key_refresh=True).flags == 1
    assert P.ProvisioningData(bytes(16), 1, iv_update=True).flags == 2
    for kw, text in (
        ({"net_key": bytes(15), "unicast": 1}, "NetKey"),
        ({"net_key": bytes(16), "unicast": 0}, "unicast"),
        ({"net_key": bytes(16), "unicast": 0x8000}, "unicast"),
        ({"net_key": bytes(16), "unicast": 1, "key_index": 0x1000}, "index"),
        ({"net_key": bytes(16), "unicast": 1, "iv_index": 1 << 32}, "IV"),
    ):
        with pytest.raises(ValueError, match=text):
            P.ProvisioningData(**kw)
    with pytest.raises(ValueError, match="25 bytes"):
        P.ProvisioningData.unpack(bytes(24))


def test_builders_and_key_helpers():
    assert P.invite() == h("0005")
    assert P.invite(255) == h("00ff")
    with pytest.raises(ValueError, match="attention"):
        P.invite(256)
    assert P.start_no_oob() == h("020000000000")
    assert P.pdu(P.COMPLETE) == h("08")
    scalar = P.generate_private_key()
    assert len(scalar) == 32
    assert len(P.public_key_bytes(scalar)) == 64
    with pytest.raises(ValueError, match="private key"):
        P.public_key_bytes(bytes(31))
    with pytest.raises(ValueError, match="public key"):
        P.ecdh_secret(scalar, bytes(63))
    with pytest.raises(ValueError, match="Invalid EC key"):
        P.ecdh_secret(scalar, bytes(64))  # (0, 0) is not on the curve
    with pytest.raises(ValueError, match="RandomProvisioner"):
        P.Provisioner(SAMPLE_DATA, random=bytes(15))


def test_fresh_sessions_use_fresh_keys_and_randoms():
    a, b = P.Provisioner(SAMPLE_DATA), P.Provisioner(SAMPLE_DATA)
    assert a.public_key != b.public_key
    assert a._random != b._random


def test_failed_error_names():
    assert str(P.ProvisioningFailed(0x04)).endswith("Confirmation Failed")
    assert str(P.ProvisioningFailed(0x42)).endswith("error 0x42")
    assert P.ProvisioningFailed(0x08).code == 0x08


def test_service_data_and_oob_names():
    data = h("30fb10fffe123456 0000000000000000".replace(" ", "")) + h("4802")
    uuid, oob = P.parse_provisioning_service_data(data) or ("", 0)
    assert uuid == "30FB10FF-FE12-3456-0000-000000000000"
    assert oob == 0x4802
    device = P.UnprovisionedDevice(uuid, oob)
    assert device.oob_names == ["URI", "on box", "inside manual"]
    assert P.UnprovisionedDevice(uuid, 0x0180).oob_names == ["bit 7", "bit 8"]
    assert P.parse_provisioning_service_data(data[:17]) is None


# ----------------------------------------------------------------------------- state machine failure paths


def at_public_key() -> P.Provisioner:
    prov = sample_session()
    prov.start()
    prov.feed(h("01") + CAPABILITIES_VALUE)
    return prov


def at_confirmation() -> P.Provisioner:
    prov = at_public_key()
    prov.feed(h("03") + DEV_PUBLIC)
    return prov


@pytest.mark.parametrize(
    ("setup", "received", "error"),
    [
        (sample_session, h("01") + CAPABILITIES_VALUE, "no provisioning PDU expected"),
        (at_public_key, b"", "empty"),
        (at_public_key, h("43") + DEV_PUBLIC, "padding"),
        (at_public_key, h("05") + CONFIRMATION_DEVICE, "unexpected Confirmation"),
        (at_public_key, h("2a"), "unexpected type 0x2a"),
        (at_public_key, h("03") + DEV_PUBLIC[:-1], "63 parameter bytes"),
        (at_public_key, h("03") + PROV_PUBLIC, "equals the provisioner's"),
        (at_public_key, h("03") + bytes(64), "not a valid P-256 point"),
        (
            at_confirmation,
            h("05") + CONFIRMATION_PROVISIONER,
            "equals the provisioner's",
        ),
        (at_confirmation, h("09"), "unexpected Failed"),  # a Failed without its code
    ],
)
def test_protocol_violations_end_the_session(
    setup: Callable[[], P.Provisioner], received: bytes, error: str
):
    prov = setup()
    with pytest.raises(P.ProvisioningError, match=error):
        prov.feed(received)
    assert prov.state == P.State.FAILED
    with pytest.raises(P.ProvisioningError, match="no provisioning PDU expected"):
        prov.feed(h("08"))


def test_capabilities_that_cannot_work():
    prov = sample_session()
    prov.start()
    with pytest.raises(P.ProvisioningError, match="0 elements"):
        prov.feed(h("01") + h("0000010000000000000000"))
    prov = sample_session()
    prov.start()
    with pytest.raises(P.ProvisioningError, match="FIPS P-256"):
        prov.feed(h("01") + h("0100040000000000000000"))  # bit 2: no algorithm of ours


def test_a_wrong_device_confirmation_fails():
    prov = at_confirmation()
    prov.feed(h("05") + bytes(16))
    with pytest.raises(P.ProvisioningError, match="does not verify"):
        prov.feed(h("06") + RANDOM_DEVICE)
    assert prov.result is None


def test_the_devices_failed_pdu_is_raised_with_its_code():
    prov = at_confirmation()
    with pytest.raises(P.ProvisioningFailed) as err:
        prov.feed(h("0904"))
    assert err.value.code == 4
    assert prov.state == P.State.FAILED


def test_start_only_once():
    prov = sample_session()
    prov.start()
    with pytest.raises(P.ProvisioningError, match="already started"):
        prov.start()


# ----------------------------------------------------------------------------- PB-GATT against a fake device


class FakeDevice:
    """A bleak-like client of an unprovisioned node: a minimal provisionee on the device side of the protocol.

    It derives everything with its own private key and random, so a run proves both ends reach the same
    session and device keys; knobs make it misbehave.
    """

    def __init__(
        self,
        *,
        elements: int = 3,
        mtu_size: int | None = 23,
        private_key: bytes = DEV_PRIVATE,
        random: bytes = RANDOM_DEVICE + RANDOM_DEVICE[::-1],
        algorithms: int = 1,
        oob_type: int = 0,
        static_oob: bytes = b"",
    ) -> None:
        self.elements = elements
        self.mtu_size = mtu_size
        self.private_key = private_key
        self.random = random
        self.algorithms = algorithms
        self.oob_type = oob_type
        self.static_oob = static_oob  # the device's own value, as printed on it
        self.start: bytes = b""
        self.auth = b""
        self.notify: Callable[[Any, bytearray], None] | None = None
        self.writes: list[bytes] = []
        self.received: list[bytes] = []
        self.reasm = ProxyReassembler()
        self.inputs = b""
        self.confirmation_key = b""
        self.confirmation_salt = b""
        self.secret = b""
        self.provisioner_confirmation = b""
        self.data: P.ProvisioningData | None = None
        self.device_key = b""
        self.silent_after: int | None = (
            None  # stop answering once this PDU type arrived
        )
        self.fail_on: tuple[int, int] | None = (
            None  # (PDU type, error code): answer it with Failed
        )
        self.wrong_confirmation = False
        self.noise_first = False  # a proxy configuration PDU before the Capabilities
        self.write_error: BaseException | None = None
        self.stop_notify_error: Exception | None = None
        self.stopped = 0

    async def start_notify(
        self, char: str, cb: Callable[[Any, bytearray], None]
    ) -> None:
        assert char == P.MESH_PROVISIONING_DATA_OUT
        self.notify = cb

    async def stop_notify(self, char: str) -> None:
        assert char == P.MESH_PROVISIONING_DATA_OUT
        self.stopped += 1
        if self.stop_notify_error is not None:
            raise self.stop_notify_error

    async def write_gatt_char(
        self, char: str, data: bytes, response: bool | None = None
    ) -> None:
        assert char == P.MESH_PROVISIONING_DATA_IN
        assert response is False
        if self.write_error is not None:
            raise self.write_error
        assert len(data) <= (self.mtu_size or 23) - 3
        self.writes.append(bytes(data))
        whole = self.reasm.feed(bytes(data))
        if whole is None:
            return
        msg_type, payload = whole
        assert msg_type == PROXY_PROVISIONING
        self.received.append(payload)
        for reply in self.handle(payload):
            asyncio.get_running_loop().call_soon(self.send, PROXY_PROVISIONING, reply)

    def send(self, msg_type: int, payload: bytes) -> None:
        assert self.notify is not None
        for frame in proxy_frame(msg_type, payload, (self.mtu_size or 23) - 3):
            self.notify(None, bytearray(frame))

    def handle(self, pdu_: bytes) -> list[bytes]:  # noqa: PLR0911  # one branch per PDU type
        kind, params = pdu_[0], pdu_[1:]
        if self.silent_after is not None and self.silent_after in {
            p[0] for p in self.received
        }:
            return []
        if self.fail_on is not None and self.fail_on[0] == kind:
            return [P.pdu(P.FAILED, bytes([self.fail_on[1]]))]
        if kind == P.INVITE:
            caps = P.Capabilities(
                self.elements, self.algorithms, 0, self.oob_type, 0, 0, 0, 0
            ).pack()
            self.inputs = params + caps
            if self.noise_first:
                asyncio.get_running_loop().call_soon(self.send, PROXY_CONFIG, h("0300"))
            return [P.pdu(P.CAPABILITIES, caps)]
        if kind == P.START:
            self.start = params
            self.inputs += params
            # the spec's rules, written out here rather than taken from the module under test (§5.4.2.4)
            self.auth = (
                self.static_oob if params[2] == P.AUTH_STATIC_OOB else bytes(self.size)
            )
            return []
        if kind == P.PUBLIC_KEY:
            own = P.public_key_bytes(self.private_key)
            self.inputs += params + own
            self.secret = P.ecdh_secret(self.private_key, params)
            if self.hmac:
                self.confirmation_salt = hmac_sha256(bytes(32), self.inputs)  # s2
                t = hmac_sha256(self.confirmation_salt, self.secret + self.auth)
                self.confirmation_key = hmac_sha256(t, b"prck256")  # k5
            else:
                self.confirmation_salt = P.confirmation_salt(self.inputs)
                self.confirmation_key = P.confirmation_key(
                    self.secret, self.confirmation_salt
                )
            return [P.pdu(P.PUBLIC_KEY, own)]
        if kind == P.CONFIRMATION:
            self.provisioner_confirmation = params
            random = bytes(self.size) if self.wrong_confirmation else self.own_random
            return [P.pdu(P.CONFIRMATION, self.confirm(random))]
        if kind == P.RANDOM:
            if self.confirm(params) != self.provisioner_confirmation:
                return [P.pdu(P.FAILED, h("04"))]
            self.provisioner_random = params
            return [P.pdu(P.RANDOM, self.own_random)]
        assert kind == P.DATA
        salt = P.provisioning_salt(
            self.confirmation_salt, self.provisioner_random, self.own_random
        )
        plain = P.decrypt_provisioning_data(
            P.session_key(self.secret, salt), P.session_nonce(self.secret, salt), params
        )
        self.data = P.ProvisioningData.unpack(plain)
        self.device_key = P.device_key(self.secret, salt)
        return [P.pdu(P.COMPLETE)]

    @property
    def hmac(self) -> bool:
        return self.start[:1] == bytes([P.ALGORITHM_HMAC_SHA256])

    @property
    def size(self) -> int:
        return 32 if self.hmac else 16

    @property
    def own_random(self) -> bytes:
        return self.random[: self.size]

    def confirm(self, random: bytes) -> bytes:
        if self.hmac:
            return hmac_sha256(self.confirmation_key, random)
        return P.confirmation(self.confirmation_key, random, self.auth)


DATA = P.ProvisioningData(
    net_key=h("00112233445566778899aabbccddeeff"), unicast=0x0D10, iv_index=7
)


async def test_provision_over_gatt_with_sar_framing():
    device = FakeDevice()
    seen: list[P.Capabilities] = []
    result = await P.provision(device, DATA, check=seen.append)
    assert device.data == DATA  # the device decrypted exactly what we meant to send
    assert result.device_key == device.device_key
    assert result.elements == 3
    assert list(result.addresses) == [0x0D10, 0x0D11, 0x0D12]
    assert seen == [result.capabilities]
    assert [p[0] for p in device.received] == [0, 2, 3, 5, 6, 7]
    assert device.received[0] == h("0005")  # the app's 5 s attention timer
    # a 23-byte MTU carries 19 octets of provisioning PDU per frame: the 65-octet public key goes as 4 frames
    public_key_frames = [
        w for w in device.writes if w[0] & 0x3F == PROXY_PROVISIONING and w[0] >> 6
    ]
    assert [w[0] >> 6 for w in public_key_frames][:4] == [1, 2, 2, 3]
    assert device.stopped == 1


async def test_provision_without_a_known_mtu_and_with_noise():
    device = FakeDevice(mtu_size=None)
    device.noise_first = True  # a stray proxy configuration PDU is ignored
    result = await P.provision(device, DATA)
    assert result.device_key == device.device_key


async def test_provision_reports_the_devices_failed_pdu():
    device = FakeDevice()
    device.fail_on = (P.PUBLIC_KEY, 0x05)
    with pytest.raises(P.ProvisioningFailed, match="Out of Resources"):
        await P.provision(device, DATA)
    assert device.stopped == 1


async def test_provision_rejects_a_device_that_cannot_confirm():
    device = FakeDevice()
    device.wrong_confirmation = True
    with pytest.raises(P.ProvisioningError, match="does not verify"):
        await P.provision(device, DATA)
    assert P.DATA not in [p[0] for p in device.received]  # the NetKey never left


async def test_provision_times_out_on_a_silent_device():
    device = FakeDevice()
    device.silent_after = P.CONFIRMATION
    prov = P.Provisioner(DATA)
    with pytest.raises(
        P.ProvisioningTimeout, match=r"no Confirmation from the device within 0\.05s"
    ):
        await P.provision(device, DATA, timeout=0.05, provisioner=prov)
    assert prov.state == P.State.FAILED


async def test_provision_check_can_refuse_the_capabilities():
    device = FakeDevice(elements=4)
    prov = P.Provisioner(DATA)

    def check(caps: P.Capabilities) -> None:
        raise P.ProvisioningError(f"{caps.elements} elements do not fit")

    with pytest.raises(P.ProvisioningError, match="4 elements do not fit"):
        await P.provision(device, DATA, check=check, provisioner=prov)
    assert prov.state == P.State.FAILED
    assert [p[0] for p in device.received] == [
        P.INVITE
    ]  # neither Start nor our public key went out


async def test_provision_turns_write_failures_into_provisioning_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    device = FakeDevice()
    device.write_error = OSError("link gone")
    device.stop_notify_error = OSError("also gone")  # only logged
    with pytest.raises(P.ProvisioningError, match="write failed: link gone"):
        await P.provision(device, DATA)
    device = FakeDevice()
    device.write_error = TimeoutError()
    monkeypatch.setattr(P, "GATT_WRITE_TIMEOUT", 0.01)
    with pytest.raises(P.ProvisioningError, match=r"not completed within 0\.01s"):
        await P.provision(device, DATA)


async def test_provision_hands_over_the_device_key_before_the_data_pdu():
    """`on_device_key` (review-4 D15): the device key once it is derived, before the device has its address or a
    key; the session goes on once the hook returned."""
    device = FakeDevice()
    prov = P.Provisioner(DATA)
    assert prov.derived_key is None
    seen: list[tuple[bytes, list[int]]] = []

    async def keep(key: bytes) -> None:
        seen.append((key, [p[0] for p in device.received]))

    result = await P.provision(device, DATA, provisioner=prov, on_device_key=keep)
    assert seen == [(result.device_key, [0, 2, 3, 5, 6])]  # no Data PDU yet
    assert result.device_key == device.device_key == prov.derived_key


async def test_provision_aborts_before_the_data_pdu_when_the_hook_raises():
    """A device key that cannot be kept: the session ends before the Data PDU, the device learns nothing."""
    device = FakeDevice()
    prov = P.Provisioner(DATA)

    async def unwritable(_key: bytes) -> None:
        raise OSError("read-only file system")

    with pytest.raises(OSError, match="read-only"):
        await P.provision(device, DATA, provisioner=prov, on_device_key=unwritable)
    assert prov.state == P.State.FAILED
    assert P.DATA not in [p[0] for p in device.received]
    assert device.data is None
    assert device.stopped == 1


# ----------------------------------------------------------------------------- the method (review-4 P4-8)

OOB16 = h("00112233445566778899aabbccddeeff")
OOB32 = OOB16 + h("ffeeddccbbaa99887766554433221100")


def caps(algorithms: int = 1, oob_type: int = 0, **kw: int) -> P.Capabilities:
    fields = {
        "elements": 1,
        "algorithms": algorithms,
        "public_key_type": 0,
        "static_oob_type": oob_type,
        "output_oob_size": 0,
        "output_oob_action": 0,
        "input_oob_size": 0,
        "input_oob_action": 0,
    }
    return P.Capabilities(**{**fields, **kw})


@pytest.mark.parametrize(
    ("offered", "size", "method"),
    [
        (caps(), None, P.Method(0, 0)),  # what the app always does
        (caps(algorithms=3), None, P.Method(1, 0)),  # the HMAC algorithm first
        (caps(algorithms=2), None, P.Method(1, 0)),
        (caps(oob_type=1), None, P.Method(0, 0)),  # Static OOB offered, no value held
        (caps(oob_type=1), 16, P.Method(0, 1)),
        (caps(algorithms=3, oob_type=1), 16, P.Method(0, 1)),  # authentication first
        (caps(algorithms=3, oob_type=1), 32, P.Method(1, 1)),
        (caps(algorithms=2, oob_type=3), 32, P.Method(1, 1)),
    ],
)
def test_the_strongest_method_offered(
    offered: P.Capabilities, size: int | None, method: P.Method
):
    assert P.choose_method(offered, size) == method


@pytest.mark.parametrize(
    ("offered", "size", "error"),
    [
        (caps(algorithms=4), None, "does not offer FIPS P-256"),
        (caps(), 16, "offers no Static OOB"),
        (caps(oob_type=1), 32, "does not take a 32-octet Static OOB value"),
        (caps(algorithms=2, oob_type=1), 16, "does not take a 16-octet"),
        (caps(algorithms=3, oob_type=3), 16, "does not take a 16-octet"),
        (caps(algorithms=3, oob_type=3), None, "only OOB-authenticated"),
    ],
)
def test_methods_that_cannot_be_used(
    offered: P.Capabilities, size: int | None, error: str
):
    with pytest.raises(P.ProvisioningError, match=error):
        P.choose_method(offered, size)


def test_start_pdus_of_each_method():
    assert P.Method().start() == P.start_no_oob() == h("020000000000")
    assert P.Method(1, 1).start() == h("020100010000")
    assert (P.Method().size, P.Method(1, 0).size) == (16, 32)


def test_the_capability_record_names_what_was_offered_and_used():
    offered = caps(
        algorithms=0x0007,
        oob_type=3,
        public_key_type=1,
        output_oob_size=4,
        output_oob_action=0x0011,
        input_oob_size=2,
        input_oob_action=0x0021,
    )
    assert P.capability_record(offered, P.Method(1, 1)) == {
        "algorithms": [
            "BTM_ECDH_P256_CMAC_AES128_AES_CCM",
            "BTM_ECDH_P256_HMAC_SHA256_AES_CCM",
            "bit 2",
        ],
        "publicKeyOob": True,
        "staticOob": True,
        "onlyOob": True,
        "outputOob": {"size": 4, "actions": ["blink", "output alphanumeric"]},
        "inputOob": {"size": 2, "actions": ["push", "bit 5"]},
        "used": {
            "algorithm": "BTM_ECDH_P256_HMAC_SHA256_AES_CCM",
            "authentication": "Static OOB",
        },
    }
    assert P.capability_record(caps(), None)["used"] is None


def test_session_values_of_the_wrong_size_are_refused():
    with pytest.raises(ValueError, match="16 or 32"):
        P.Provisioner(SAMPLE_DATA, random=bytes(15))
    with pytest.raises(
        ValueError, match=r"Static OOB value must be 16 or 32 bytes, got 20"
    ):
        P.Provisioner(SAMPLE_DATA, static_oob=bytes(20))
    prov = sample_session()  # a 16-octet random injected: the HMAC algorithm takes 32
    prov.start()
    with pytest.raises(P.ProvisioningError, match="takes 32"):
        prov.feed(h("01") + caps(algorithms=3).pack())


async def test_provision_with_the_hmac_algorithm():
    device = FakeDevice(algorithms=3)
    prov = P.Provisioner(DATA)
    result = await P.provision(device, DATA, provisioner=prov)
    assert result.method == prov.method == P.Method(1, 0)
    assert device.start == h("0100000000")
    assert device.data == DATA
    assert result.device_key == device.device_key
    # Confirmation and Random are 32 octets each (+ the type octet)
    sizes = [len(p) for p in device.received if p[0] in (P.CONFIRMATION, P.RANDOM)]
    assert sizes == [33, 33]


@pytest.mark.parametrize(
    ("algorithms", "oob_type", "value", "start"),
    [(1, 1, OOB16, "0000010000"), (3, 3, OOB32, "0100010000")],
)
async def test_provision_with_static_oob(
    algorithms: int, oob_type: int, value: bytes, start: str
):
    device = FakeDevice(algorithms=algorithms, oob_type=oob_type, static_oob=value)
    prov = P.Provisioner(DATA, static_oob=value)
    result = await P.provision(device, DATA, provisioner=prov)
    assert device.start == h(start)
    assert result.method.auth == P.AUTH_STATIC_OOB
    assert device.data == DATA
    assert result.device_key == device.device_key
    assert value.hex() not in repr(prov).lower()
    assert value.hex() not in repr(result).lower()


@pytest.mark.parametrize(
    ("algorithms", "oob_type", "value"), [(1, 1, OOB16), (3, 3, OOB32)]
)
async def test_a_wrong_static_oob_value_never_reaches_the_data(
    algorithms: int, oob_type: int, value: bytes
):
    """A man in the middle without the device's value: the device's check of our confirmation fails."""
    device = FakeDevice(algorithms=algorithms, oob_type=oob_type, static_oob=value)
    wrong = P.Provisioner(DATA, static_oob=bytes(len(value)))
    with pytest.raises(P.ProvisioningFailed, match="Confirmation Failed"):
        await P.provision(device, DATA, provisioner=wrong)
    assert P.DATA not in [p[0] for p in device.received]


async def test_a_device_that_cannot_confirm_fails_with_the_hmac_algorithm():
    device = FakeDevice(algorithms=2)
    device.wrong_confirmation = True
    with pytest.raises(P.ProvisioningError, match="does not verify"):
        await P.provision(device, DATA)
    assert P.DATA not in [p[0] for p in device.received]


async def test_a_refused_method_ends_the_session_before_start():
    device = FakeDevice(
        algorithms=3, oob_type=3
    )  # takes only OOB-authenticated provisioning
    with pytest.raises(P.ProvisioningError, match="only OOB-authenticated"):
        await P.provision(device, DATA)
    assert [p[0] for p in device.received] == [P.INVITE]


def test_short_confirmations_are_refused_with_the_hmac_algorithm():
    prov = P.Provisioner(SAMPLE_DATA, private_key=PROV_PRIVATE)
    prov.start()
    prov.feed(h("01") + caps(algorithms=2).pack())
    prov.feed(h("03") + DEV_PUBLIC)
    with pytest.raises(P.ProvisioningError, match="16 parameter bytes"):
        prov.feed(h("05") + CONFIRMATION_DEVICE)
