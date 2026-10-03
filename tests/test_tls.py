"""TLS certificate pinning of the gateway client: the wire-level proof, and the flow around it.

Two real HTTPS servers on the loopback interface, each with its own self-signed certificate — the "gateway" and an
"impostor" answering the same routes — and Home Assistant's own no-verify session, exactly as the integration uses
it. What is pinned by a real `aiohttp.Fingerprint` must reach the gateway and must be refused by the impostor at the
TLS handshake, i.e. before the request line, the `token` header or the password is written. Each server records
every request it ever sees, so "nothing was sent" is asserted, not assumed.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import shutil
import ssl
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome_ble.config_flow import certificate_issue_id
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_PASSWORD,
    CONF_GATEWAY_PIN_SOURCE,
    CONF_GATEWAY_TOKEN,
    CONF_MESH_UUID,
    CONF_METADATA_DIR,
    CONF_SOURCE,
    CONF_UNICAST,
    DOMAIN,
    PIN_FROM_USER,
    STORAGE_DIR,
)
from custom_components.junghome_ble.gateway_api import (
    GatewayCertificateMismatch,
    JungHomeGatewayApi,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.client import AccessMessage, ProxyClient
from custom_components.junghome_ble.jhmesh.pdu import decode_opcode, encode_opcode
from custom_components.junghome_ble.tls import (
    CONF_GATEWAY_FINGERPRINT,
    PROBE_DIGEST,
    PROPERTY_GATEWAY_FINGERPRINT,
    async_learn_fingerprint,
    async_read_mesh_fingerprint,
    fingerprint_ssl,
    format_fingerprint,
    normalize_fingerprint,
)

from .conftest import (
    CDB_PATH,
    SHARE_EXPORT_PATH,
    FakeProxyLink,
    settle,
    setup_entry,
    wait_for_link,
    wait_until,
)
from .helpers import advanced, through_areas

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from homeassistant.core import HomeAssistant

TOKEN = "synthetic-token-1234"  # never a real credential
PASSWORD = "netkey-pw"
SHARE_EXPORT = json.loads(Path(SHARE_EXPORT_PATH).read_text())
MESH_UUID = "1BAF3ADE-0000-4000-8000-000000000001"
GATEWAY_NODE = 0x00DC  # the gateway node of the fixture network (pid 0x000B)
OUR = 0x0D00


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    with patch(
        "custom_components.junghome_ble.async_setup_entry", return_value=True
    ) as mock:
        yield mock


def _self_signed(directory: Path, name: str) -> tuple[Path, Path, str]:
    """Write a fresh self-signed certificate + key; return paths and the certificate's SHA-256 hex."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "junghome.local")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    crt, pem = directory / f"{name}.crt", directory / f"{name}.key"
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return crt, pem, digest


class _Server:
    """One HTTPS server on the loopback interface with its own certificate.

    Answers every gateway route the integration touches with a minimal valid body and records
    `(method, path, token header, JSON body)` for each request it actually receives.
    """

    def __init__(self, directory: Path, name: str) -> None:
        crt, key, self.fingerprint = _self_signed(directory, name)
        self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ssl_context.load_cert_chain(str(crt), str(key))
        self.seen: list[tuple[str, str, str | None, Any]] = []
        self.host = ""
        self._runner: web.AppRunner | None = None

    async def _handler(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        self.seen.append(
            (request.method, request.path, request.headers.get("token"), body)
        )
        path = request.path
        if path.endswith("/version/"):
            return web.json_response({"api_version": "1.5.0"})
        if path.endswith(("/register", "/register/by-password")):
            return web.json_response({"token": TOKEN})
        if path.endswith("/project/junghome"):
            if request.headers.get("token") != TOKEN:
                return web.json_response({"error": "Unauthorized"}, status=401)
            return web.json_response(SHARE_EXPORT)
        return web.json_response({}, status=404)

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0, ssl_context=self.ssl_context)
        await site.start()
        assert self._runner.addresses
        port = self._runner.addresses[0][1]
        self.host = f"127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


@pytest.fixture
async def servers(tmp_path: Path, socket_enabled: None) -> Any:
    """A `gateway` and an `impostor` server with distinct certificates (loopback only; sockets enabled)."""
    gateway, impostor = _Server(tmp_path, "gateway"), _Server(tmp_path, "impostor")
    await gateway.start()
    await impostor.start()
    assert gateway.fingerprint != impostor.fingerprint
    yield SimpleNamespace(gateway=gateway, impostor=impostor)
    await gateway.stop()
    await impostor.stop()


# --------------------------------------------------------------------------- tls.py against real sockets


async def test_learn_reads_the_digest_without_sending_a_request(
    hass: HomeAssistant, servers: Any
) -> None:
    """The learn step yields the peer's SHA-256 and never gets past the handshake."""
    session = async_get_clientsession(hass, verify_ssl=False)
    assert await async_learn_fingerprint(session, servers.gateway.host) == (
        servers.gateway.fingerprint
    )
    assert servers.gateway.seen == []  # no request line, no headers, nothing


async def test_learn_reports_an_unreachable_host_as_a_client_error(
    hass: HomeAssistant, servers: Any
) -> None:
    await servers.impostor.stop()
    session = async_get_clientsession(hass, verify_ssl=False)
    with pytest.raises(aiohttp.ClientError):
        await async_learn_fingerprint(session, servers.impostor.host)


async def test_learn_refuses_a_peer_that_accepts_the_probe_digest(
    hass: HomeAssistant, aioclient_mock: Any
) -> None:
    """A "successful" probe (only a test double ignoring `ssl=` can produce one) is not a pin."""
    aioclient_mock.get("https://gw/api/junghome/version/", json={"api_version": "1"})
    session = async_get_clientsession(hass, verify_ssl=False)
    with pytest.raises(aiohttp.ClientConnectionError):
        await async_learn_fingerprint(session, "gw")


async def test_learn_timeout_is_bounded(hass: HomeAssistant) -> None:
    """A peer that accepts TCP and never finishes the handshake times out."""
    session = async_get_clientsession(hass, verify_ssl=False)
    gate = asyncio.Event()

    async def _hang(*_args: object, **_kwargs: object) -> None:
        await gate.wait()

    with (
        patch.object(session, "_request", _hang),
        patch("custom_components.junghome_ble.tls.LEARN_TIMEOUT", 0.01),
        pytest.raises(TimeoutError),
    ):
        await async_learn_fingerprint(session, "gw")
    gate.set()


def test_fingerprint_helpers() -> None:
    digest = "ab" * 32
    assert normalize_fingerprint(digest) == digest
    assert normalize_fingerprint(digest.upper()) == digest
    assert normalize_fingerprint(format_fingerprint(digest)) == digest
    assert normalize_fingerprint(" " + digest + "\n") == digest
    for junk in (None, "", "ab" * 31, "zz" * 32, 42, b"ab" * 32):
        assert normalize_fingerprint(junk) is None
    assert format_fingerprint("abcd") == "AB:CD"
    assert format_fingerprint("") == ""
    # one object per digest, so aiohttp's connection pool keys stay stable
    assert fingerprint_ssl(digest) is fingerprint_ssl(digest)
    assert fingerprint_ssl(digest).fingerprint == bytes.fromhex(digest)
    with pytest.raises(ValueError, match="SHA-256"):
        fingerprint_ssl("ab" * 16)  # MD5-length: aiohttp would refuse it too
    with pytest.raises(ValueError, match="hexadecimal"):
        fingerprint_ssl("not hex")
    assert len(PROBE_DIGEST) == 32


# --------------------------------------------------------------------------- the REST client


async def test_pinned_client_reaches_the_gateway_and_not_the_impostor(
    hass: HomeAssistant, servers: Any
) -> None:
    """Every request of the client is pinned; the impostor sees neither the password nor the token."""
    session = async_get_clientsession(hass, verify_ssl=False)
    api = JungHomeGatewayApi(session, servers.gateway.host, servers.gateway.fingerprint)
    assert (await api.version()).api == "1.5.0"
    assert await api.register_by_password(PASSWORD) == TOKEN
    assert await api.fetch_project() == SHARE_EXPORT
    assert servers.gateway.seen == [
        ("GET", "/api/junghome/version/", None, None),
        ("POST", "/api/junghome/register/by-password", None, {"password": PASSWORD}),
        ("GET", "/api/junghome/project/junghome", TOKEN, None),
    ]

    api = JungHomeGatewayApi(
        session, servers.impostor.host, servers.gateway.fingerprint, TOKEN
    )
    for call in (
        api.version,
        api.register,
        lambda: api.register_by_password(PASSWORD),
        api.fetch_project,
    ):
        with pytest.raises(GatewayCertificateMismatch) as excinfo:
            await call()
        assert excinfo.value.host == servers.impostor.host
        assert excinfo.value.expected == servers.gateway.fingerprint
        assert excinfo.value.observed == servers.impostor.fingerprint
        assert TOKEN not in str(excinfo.value)
    assert servers.impostor.seen == []
    with pytest.raises(ValueError, match="SHA-256"):
        JungHomeGatewayApi(session, servers.gateway.host, "ab" * 16)


# --------------------------------------------------------------------------- the config flow end to end


async def _choose(hass: HomeAssistant, result: Any, option: str) -> Any:
    assert result["type"] is FlowResultType.MENU, result
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": option}
    )


def _stored(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(STORAGE_DIR, f"{MESH_UUID}.json"))


def _gateway_entry(
    hass: HomeAssistant, host: str, fingerprint: str | None
) -> MockConfigEntry:
    """A gateway entry whose export is already in the store (a copy of the fixture)."""
    stored = _stored(hass)
    stored.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(CDB_PATH, stored)
    data = {
        CONF_CDB_PATH: str(stored),
        CONF_METADATA_DIR: "",
        CONF_UNICAST: "0D00",
        CONF_MESH_UUID: MESH_UUID,
        CONF_SOURCE: "gateway",
        CONF_GATEWAY_HOST: host,
        CONF_GATEWAY_TOKEN: TOKEN,
    }
    if fingerprint is not None:
        data[CONF_GATEWAY_FINGERPRINT] = fingerprint
    return MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data=data,
    )


async def test_user_flow_learns_the_certificate_before_the_password_leaves(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    servers: Any,
) -> None:
    """First contact: the pin is learned with a bare handshake; every request afterwards carries it."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _choose(hass, result, "gateway")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_GATEWAY_HOST: servers.gateway.host,
            CONF_GATEWAY_PASSWORD: PASSWORD,
            **advanced("0d00"),
        },
    )
    await hass.async_block_till_done()
    result = await through_areas(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GATEWAY_FINGERPRINT] == servers.gateway.fingerprint
    assert result["data"][CONF_GATEWAY_TOKEN] == TOKEN
    # the learn handshake left no trace; exactly the three requests of the flow reached the gateway
    assert [s[:3] for s in servers.gateway.seen] == [
        ("GET", "/api/junghome/version/", None),
        ("POST", "/api/junghome/register/by-password", None),
        ("GET", "/api/junghome/project/junghome", TOKEN),
    ]


async def test_refetch_with_a_changed_certificate_asks_before_the_token_leaves(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    servers: Any,
) -> None:
    """The entry is pinned to the gateway; the responder at its address is the impostor.

    The re-fetch is refused at the handshake (the impostor saw nothing), the `gateway_certificate` step shows
    both fingerprints, and only the confirmation lets the token go to the new certificate — which is then the one
    recorded, as the user's (the hub compares it with the gateway node's report before using it).
    """
    entry = _gateway_entry(hass, servers.impostor.host, servers.gateway.fingerprint)
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await _choose(hass, result, "gateway_refetch")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], advanced("0D00")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway_certificate"
    assert result["description_placeholders"] == {
        "host": servers.impostor.host,
        "observed": format_fingerprint(servers.impostor.fingerprint),
        "expected": format_fingerprint(servers.gateway.fingerprint),
    }
    assert servers.impostor.seen == []
    assert entry.data[CONF_GATEWAY_FINGERPRINT] == servers.gateway.fingerprint
    # the flow raises no repair of its own: closing this form leaves nothing behind
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, certificate_issue_id(entry.entry_id))
        is None
    )

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    result = await through_areas(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_GATEWAY_FINGERPRINT] == servers.impostor.fingerprint
    assert entry.data[CONF_GATEWAY_PIN_SOURCE] == PIN_FROM_USER
    assert [s[:3] for s in servers.impostor.seen] == [
        ("GET", "/api/junghome/project/junghome", TOKEN)
    ]
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, certificate_issue_id(entry.entry_id))
        is None
    )


async def test_gateway_form_with_a_changed_certificate_resumes_after_confirmation(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    servers: Any,
) -> None:
    """The full gateway form on reconfigure, same host, a password: the probe already meets the new certificate."""
    entry = _gateway_entry(hass, servers.impostor.host, servers.gateway.fingerprint)
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await _choose(hass, result, "gateway")
    form_input = {
        CONF_GATEWAY_HOST: servers.impostor.host,
        CONF_GATEWAY_PASSWORD: PASSWORD,
        **advanced("0d00"),
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], form_input
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway_certificate"
    assert servers.impostor.seen == []

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    result = await through_areas(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_GATEWAY_FINGERPRINT] == servers.impostor.fingerprint
    assert [s[:3] for s in servers.impostor.seen] == [
        ("GET", "/api/junghome/version/", None),
        ("POST", "/api/junghome/register/by-password", None),
        ("GET", "/api/junghome/project/junghome", TOKEN),
    ]


async def test_legacy_entry_without_a_pin_learns_it_on_refetch(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    servers: Any,
) -> None:
    """An entry from before pinning existed: trust on first use, then recorded."""
    entry = _gateway_entry(hass, servers.gateway.host, None)
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await _choose(hass, result, "gateway_refetch")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], advanced("0D00")
    )
    await hass.async_block_till_done()
    result = await through_areas(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_GATEWAY_FINGERPRINT] == servers.gateway.fingerprint
    assert [s[:3] for s in servers.gateway.seen] == [
        ("GET", "/api/junghome/project/junghome", TOKEN)
    ]


async def test_unreachable_host_at_the_learn_step(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    servers: Any,
) -> None:
    await servers.gateway.stop()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _choose(hass, result, "gateway")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_GATEWAY_HOST: servers.gateway.host,
            CONF_GATEWAY_PASSWORD: PASSWORD,
            **advanced("0d00"),
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway"
    assert result["errors"] == {"base": "cannot_connect"}


# --------------------------------------------------------------------------- the pin from the mesh


def _fingerprint_status(value: bytes) -> AccessMessage:
    """The gateway node's Manufacturer Property Status for 0xC003 carrying `value`."""
    op = M.VENDOR_PROPERTY_STATUS_OPCODES["manufacturer"]
    params = PROPERTY_GATEWAY_FINGERPRINT.to_bytes(2, "little") + b"\x01" + value
    return AccessMessage(
        GATEWAY_NODE,
        OUR,
        3,
        0,
        op,
        M.JUNG_CID,
        params,
        encode_opcode(op, M.JUNG_CID) + params,
        "app0",
    )


@pytest.fixture
def mesh_answer() -> dict[str, Any]:
    """What the fake gateway node answers a property Get with: `value` bytes, an exception, or raw messages.

    Raw messages (one, or a list in the order they arrive) are what the node sends meanwhile: the request takes
    the first its `match` accepts, as `ProxyClient.request` does, and times out when none fits.
    """
    return {"value": None, "asked": []}


@pytest.fixture
async def connected_gateway_entry(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    mesh_answer: dict[str, Any],
    servers: Any,
) -> MockConfigEntry:
    """A gateway entry, loaded and connected to the fake proxy, whose gateway node answers over the mesh."""

    async def request(
        self: ProxyClient,
        dst: int,
        pdu: bytes,
        expect: int,
        *,
        match: Callable[[AccessMessage], bool] | None = None,
        **_kw: Any,
    ) -> AccessMessage:
        op, cid, params = decode_opcode(pdu)
        pid = int.from_bytes(params[:2], "little") if len(params) >= 2 else None
        if cid == M.JUNG_CID and op == 0x08 and pid == PROPERTY_GATEWAY_FINGERPRINT:
            mesh_answer["asked"].append((dst, pid))
            answer = mesh_answer["value"]
            if isinstance(answer, Exception):
                raise answer
            if isinstance(answer, AccessMessage):
                answer = [answer]
            elif isinstance(answer, bytes):
                answer = [_fingerprint_status(answer)]
            for m in answer or ():
                if match is None or match(m):
                    return m
        raise TimeoutError("no response")

    entry = _gateway_entry(hass, servers.gateway.host, "ab" * 32)  # a stale pin
    with patch.object(ProxyClient, "request", request):
        await setup_entry(hass, entry)
        await wait_for_link(hass, entry)
        await settle(hass)
        # the hub compares the (unvouched) pin with the node's report on its first link: not what these test
        await wait_until(
            hass,
            lambda: (
                not any(
                    t.get_name().endswith("gateway certificate check")
                    for t in entry._background_tasks
                )
            ),
            what="the link-time certificate check",
        )
        mesh_answer["asked"].clear()
        yield entry


async def test_mesh_provided_fingerprint_wins_over_the_stored_one(
    hass: HomeAssistant,
    connected_gateway_entry: MockConfigEntry,
    mesh_answer: dict[str, Any],
    servers: Any,
) -> None:
    """The hub is connected: the gateway node's own report is the pin, not the (stale) entry value or TOFU."""
    entry = connected_gateway_entry
    mesh_answer["value"] = format_fingerprint(servers.gateway.fingerprint).encode()
    with patch(
        "custom_components.junghome_ble.config_flow.async_learn_fingerprint",
        side_effect=AssertionError("must not learn when the mesh knows"),
    ):
        result = await entry.start_reconfigure_flow(hass)
        result = await _choose(hass, result, "gateway_refetch")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], advanced("0D00")
        )
        await hass.async_block_till_done()
    result = await through_areas(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mesh_answer["asked"] == [(GATEWAY_NODE, PROPERTY_GATEWAY_FINGERPRINT)]
    assert entry.data[CONF_GATEWAY_FINGERPRINT] == servers.gateway.fingerprint
    assert [s[:3] for s in servers.gateway.seen] == [
        ("GET", "/api/junghome/project/junghome", TOKEN)
    ]


async def test_mesh_provided_fingerprint_refuses_an_impostor(
    hass: HomeAssistant,
    connected_gateway_entry: MockConfigEntry,
    mesh_answer: dict[str, Any],
    servers: Any,
) -> None:
    """The entry points at the impostor's address; the mesh says otherwise: refused before the token leaves, and
    with no one-click override — the gateway itself vouched for another certificate."""
    entry = connected_gateway_entry
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_GATEWAY_HOST: servers.impostor.host}
    )
    mesh_answer["value"] = servers.gateway.fingerprint.encode() + b"\0\0"
    result = await entry.start_reconfigure_flow(hass)
    result = await _choose(hass, result, "gateway_refetch")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], advanced("0D00")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "certificate_vouched_by_mesh"
    assert result["description_placeholders"] == {
        "host": servers.impostor.host,
        "observed": format_fingerprint(servers.impostor.fingerprint),
        "expected": format_fingerprint(servers.gateway.fingerprint),
    }
    assert servers.impostor.seen == []
    assert entry.data[CONF_GATEWAY_FINGERPRINT] == "ab" * 32


@pytest.mark.parametrize(
    "answer",
    [
        None,
        ConnectionError("link lost"),
        "short",
        "other_property",
        b"not a digest",
        b"ab" * 20,
    ],
    ids=["silent", "link_lost", "short_status", "other_property", "text", "sha1"],
)
async def test_mesh_fingerprint_falls_back_when_the_gateway_node_cannot_help(
    hass: HomeAssistant,
    connected_gateway_entry: MockConfigEntry,
    mesh_answer: dict[str, Any],
    answer: Any,
) -> None:
    hub = connected_gateway_entry.runtime_data
    if answer == "short":  # the property id and nothing after it
        answer = AccessMessage(
            GATEWAY_NODE, OUR, 3, 0, 0x0B, M.JUNG_CID, b"\x03\xc0", b"", "app0"
        )
    elif answer == "other_property":
        answer = AccessMessage(
            GATEWAY_NODE, OUR, 3, 0, 0x0B, M.JUNG_CID, b"\x02\xc0\x01abc", b"", "app0"
        )
    mesh_answer["value"] = answer
    assert await async_read_mesh_fingerprint(hub) is None


async def test_mesh_fingerprint_is_not_taken_from_another_property(
    hass: HomeAssistant,
    connected_gateway_entry: MockConfigEntry,
    mesh_answer: dict[str, Any],
) -> None:
    """The gateway node's IP address (0xC002, which following the gateway asks for) arrives first: the read
    waits for the 0xC003 Status instead of taking that one for its answer."""
    hub = connected_gateway_entry.runtime_data
    op = M.VENDOR_PROPERTY_STATUS_OPCODES["manufacturer"]
    address = AccessMessage(
        GATEWAY_NODE, OUR, 3, 0, op, M.JUNG_CID, b"\x02\xc0\x01192.0.2.10", b"", "app0"
    )
    mesh_answer["value"] = [address, _fingerprint_status(b"ab" * 32)]
    assert await async_read_mesh_fingerprint(hub) == "ab" * 32


async def test_mesh_fingerprint_needs_a_link_and_a_gateway_node(
    hass: HomeAssistant,
    connected_gateway_entry: MockConfigEntry,
    mesh_answer: dict[str, Any],
) -> None:
    hub = connected_gateway_entry.runtime_data
    mesh_answer["value"] = b"ab" * 32
    assert await async_read_mesh_fingerprint(hub) == "ab" * 32
    for node in hub.cdb.nodes:
        if node.pid == 0x000B:
            node.pid = 0x0001
    assert await async_read_mesh_fingerprint(hub) is None  # no gateway in this mesh
    assert mesh_answer["asked"] == [(GATEWAY_NODE, PROPERTY_GATEWAY_FINGERPRINT)]
    with patch.object(ProxyClient, "connected", False):
        assert await async_read_mesh_fingerprint(hub) is None
    # the flow does not ask the mesh for an entry that is not loaded
    await hass.config_entries.async_unload(connected_gateway_entry.entry_id)
    await hass.async_block_till_done()
    with (
        patch(
            "custom_components.junghome_ble.config_flow.async_read_mesh_fingerprint",
            side_effect=AssertionError("not loaded"),
        ),
        patch(
            "custom_components.junghome_ble.config_flow.async_learn_fingerprint",
            AsyncMock(side_effect=aiohttp.ClientConnectionError("refused")),
        ) as learn,
    ):
        result = await connected_gateway_entry.start_reconfigure_flow(hass)
        result = await _choose(hass, result, "gateway")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_GATEWAY_HOST: "10.0.0.9", **advanced("0d00")},
        )
    assert result["errors"] == {"base": "cannot_connect"}
    learn.assert_awaited_once()
