"""The gateway REST client: registration (approval wait and password), the project fetch and its fallback,
the error mapping, the certificate pin on every request, and that no response body ever reaches the log.

The pin itself against real TLS servers is proven in `test_tls.py`; here the mocked session shows that every
request carries it and that aiohttp's mismatch becomes `GatewayCertificateMismatch`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.junghome_ble.const import (
    CONF_GATEWAY_FINGERPRINT,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_TOKEN,
    CONF_SOURCE,
    DOMAIN,
    GATEWAY_USER_NAME,
)
from custom_components.junghome_ble.gateway_api import (
    GatewayAuthError,
    GatewayBusy,
    GatewayCertificateMismatch,
    GatewayConfig,
    GatewayError,
    GatewayHealthEntry,
    GatewayNoProject,
    GatewayNotApproved,
    GatewayUnreachable,
    GatewayVersion,
    JungHomeGatewayApi,
    api_for_entry,
    as_export,
    parse_config,
)
from custom_components.junghome_ble.tls import fingerprint_ssl

from .conftest import CDB_PATH, SHARE_EXPORT_PATH

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

HOST = "junghome.local"
API = f"https://{HOST}/api/junghome"
TOKEN = "eyJ.tok.en"
FINGERPRINT = "ab" * 32
OTHER_FINGERPRINT = "cd" * 32


@pytest.fixture
def api(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> JungHomeGatewayApi:
    return JungHomeGatewayApi(
        async_get_clientsession(hass, verify_ssl=False), HOST, FINGERPRINT
    )


def _share_export() -> dict[str, Any]:
    return json.loads(Path(SHARE_EXPORT_PATH).read_text())


def _bare_cdb() -> dict[str, Any]:
    return json.loads(Path(CDB_PATH).read_text())["meshNetwork"]


def _netkey() -> str:
    return str(_bare_cdb()["netKeys"][0]["key"])


# --------------------------------------------------------------------------- version


async def test_version(
    api: JungHomeGatewayApi, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{API}/version/",
        json={
            "api_version": "1.5.0",
            "version_release": "2.1.3",
            "version_build": "2840",
        },
    )
    assert await api.version() == GatewayVersion(
        api="1.5.0", release="2.1.3", build="2840"
    )
    method, url, data, headers = aioclient_mock.mock_calls[0]
    assert (method, str(url), data, headers) == ("GET", f"{API}/version/", None, {})


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"exc": TimeoutError()}, GatewayUnreachable),
        ({"exc": aiohttp.ClientConnectionError("refused")}, GatewayUnreachable),
        ({"status": 500, "json": {"error": "Internal server error"}}, GatewayError),
        ({"text": "<html>not json</html>"}, GatewayError),
    ],
    ids=["timeout", "connection", "http_500", "not_json"],
)
async def test_version_errors(
    api: JungHomeGatewayApi,
    aioclient_mock: AiohttpClientMocker,
    kwargs: dict[str, Any],
    error: type[GatewayError],
) -> None:
    aioclient_mock.get(f"{API}/version/", **kwargs)
    with pytest.raises(error):
        await api.version()


# --------------------------------------------------------------------------- registration


async def test_register_waits_for_approval(
    api: JungHomeGatewayApi, aioclient_mock: AiohttpClientMocker
) -> None:
    """The POST blocks until the gateway answers (the user approving in the app); the token is kept."""
    approved = asyncio.Event()

    async def hold(method: str, url: Any, data: Any) -> Any:
        await approved.wait()
        return AiohttpClientMockResponse(method=method, url=url, json={"token": TOKEN})

    aioclient_mock.post(f"{API}/register", side_effect=hold)
    task = asyncio.ensure_future(api.register())
    await asyncio.sleep(0)
    assert not task.done()
    approved.set()
    assert await task == TOKEN
    assert api.token == TOKEN
    method, _, data, headers = aioclient_mock.mock_calls[0]
    assert (method, data, headers) == ("POST", {"user_name": GATEWAY_USER_NAME}, {})


async def test_register_custom_name(
    api: JungHomeGatewayApi, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(f"{API}/register", json={"token": TOKEN})
    assert await api.register("Kitchen HA") == TOKEN
    assert aioclient_mock.mock_calls[0][2] == {"user_name": "Kitchen HA"}


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        (
            {"status": 400, "json": {"error": "Error during register."}},
            GatewayNotApproved,
        ),
        ({"status": 401}, GatewayAuthError),
        ({"status": 200, "json": {}}, GatewayError),
        ({"status": 200, "json": {"token": ""}}, GatewayError),
        ({"status": 200, "text": ""}, GatewayError),
        ({"status": 503}, GatewayError),
        ({"exc": TimeoutError()}, GatewayUnreachable),
    ],
    ids=[
        "not_approved",
        "unauthorized",
        "no_token",
        "empty_token",
        "empty_body",
        "http_503",
        "timeout",
    ],
)
async def test_register_errors(
    api: JungHomeGatewayApi,
    aioclient_mock: AiohttpClientMocker,
    kwargs: dict[str, Any],
    error: type[GatewayError],
) -> None:
    aioclient_mock.post(f"{API}/register", **kwargs)
    with pytest.raises(error):
        await api.register()
    assert api.token is None


async def test_register_by_password(
    api: JungHomeGatewayApi, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(f"{API}/register/by-password", json={"token": TOKEN})
    assert await api.register_by_password("secret") == TOKEN
    assert api.token == TOKEN
    assert aioclient_mock.mock_calls[0][2] == {"password": "secret"}


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"status": 401, "json": {"error": "Unauthorized"}}, GatewayAuthError),
        ({"status": 500}, GatewayError),
        ({"status": 200, "json": {"nope": 1}}, GatewayError),
        ({"exc": aiohttp.ClientError()}, GatewayUnreachable),
    ],
    ids=["wrong_password", "http_500", "no_token", "client_error"],
)
async def test_register_by_password_errors(
    api: JungHomeGatewayApi,
    aioclient_mock: AiohttpClientMocker,
    kwargs: dict[str, Any],
    error: type[GatewayError],
) -> None:
    aioclient_mock.post(f"{API}/register/by-password", **kwargs)
    with pytest.raises(error):
        await api.register_by_password("secret")
    assert api.token is None


# --------------------------------------------------------------------------- project export


async def test_fetch_project(
    api: JungHomeGatewayApi, aioclient_mock: AiohttpClientMocker
) -> None:
    """`/project/junghome` is the app's share export as uploaded: returned verbatim, token in the header."""
    api.token = TOKEN
    aioclient_mock.get(f"{API}/project/junghome", json=_share_export())
    assert await api.fetch_project() == _share_export()
    method, url, _, headers = aioclient_mock.mock_calls[0]
    assert (method, str(url), headers) == (
        "GET",
        f"{API}/project/junghome",
        {"token": TOKEN},
    )
    assert aioclient_mock.call_count == 1  # no fallback needed


async def test_fetch_project_falls_back_to_cdb(
    api: JungHomeGatewayApi, aioclient_mock: AiohttpClientMocker
) -> None:
    """Older firmware without `/project/junghome`: the bare CDB from `/project/cdb`, wrapped like the iOS file."""
    api.token = TOKEN
    aioclient_mock.get(f"{API}/project/junghome", status=404)
    aioclient_mock.get(f"{API}/project/cdb", json=_bare_cdb())
    assert await api.fetch_project() == {"meshNetwork": _bare_cdb()}
    assert [str(c[1]) for c in aioclient_mock.mock_calls] == [
        f"{API}/project/junghome",
        f"{API}/project/cdb",
    ]
    assert aioclient_mock.mock_calls[1][3] == {"token": TOKEN}


async def test_fetch_project_cdb_already_wrapped(
    api: JungHomeGatewayApi, aioclient_mock: AiohttpClientMocker
) -> None:
    api.token = TOKEN
    aioclient_mock.get(
        f"{API}/project/junghome", status=200, text=""
    )  # no project uploaded yet
    aioclient_mock.get(f"{API}/project/cdb", json={"meshNetwork": _bare_cdb()})
    assert await api.fetch_project() == {"meshNetwork": _bare_cdb()}


@pytest.mark.parametrize(
    ("junghome", "cdb", "error"),
    [
        ({"status": 404}, {"status": 404}, GatewayNoProject),
        ({"status": 500}, {"status": 500}, GatewayError),
        ({"json": None}, {"json": {}}, GatewayNoProject),
        (
            {"json": {"version": "1.1", "meta": {}}},
            {"json": {"nodes": []}},
            GatewayNoProject,
        ),
        ({"status": 401}, {"json": _bare_cdb()}, GatewayAuthError),
        ({"status": 404}, {"status": 401}, GatewayAuthError),
        ({"exc": TimeoutError()}, {"json": _bare_cdb()}, GatewayUnreachable),
    ],
    ids=[
        "both_404",
        "both_500",
        "empty_bodies",
        "unusable_bodies",
        "unauthorized",
        "unauthorized_fallback",
        "timeout",
    ],
)
async def test_fetch_project_errors(
    api: JungHomeGatewayApi,
    aioclient_mock: AiohttpClientMocker,
    junghome: dict[str, Any],
    cdb: dict[str, Any],
    error: type[GatewayError],
) -> None:
    api.token = TOKEN
    aioclient_mock.get(f"{API}/project/junghome", **junghome)
    aioclient_mock.get(f"{API}/project/cdb", **cdb)
    with pytest.raises(error):
        await api.fetch_project()


@pytest.mark.parametrize(
    ("status", "error"),
    [(429, GatewayBusy), (500, GatewayError)],
    ids=["busy", "server_error"],
)
async def test_fetch_project_does_not_fall_back_to_cdb_for_a_busy_or_failing_gateway(
    api: JungHomeGatewayApi,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    error: type[GatewayError],
) -> None:
    """The bare CDB has no `meta`: falling back to it for a gateway that is merely busy or erroring would adopt
    a document that strips every device name and room link from the file (CFG-04)."""
    api.token = TOKEN
    aioclient_mock.get(f"{API}/project/junghome", status=status)
    aioclient_mock.get(f"{API}/project/cdb", json=_bare_cdb())
    with pytest.raises(error):
        await api.fetch_project()
    assert aioclient_mock.call_count == 1


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("share", "same"),
        ({"meshNetwork": {"nodes": []}}, "same"),
        ({"nodes": [], "netKeys": []}, {"meshNetwork": {"nodes": [], "netKeys": []}}),
        ({"nodes": []}, None),
        (
            {"network": {"meshNetwork": {}}},
            None,
        ),  # decoded `network`: not something the loader reads
        ([], None),
        ("text", None),
        (None, None),
    ],
    ids=[
        "export_dto",
        "ios_wrapper",
        "bare_cdb",
        "cdb_without_keys",
        "decoded_network",
        "list",
        "string",
        "null",
    ],
)
def test_as_export(body: Any, expected: Any) -> None:
    if body == "share":
        body = _share_export()
    if expected == "same":
        expected = body
    assert as_export(body) == expected


# --------------------------------------------------------------------------- the certificate pin


async def test_every_request_is_pinned(
    hass: HomeAssistant, api: JungHomeGatewayApi, aioclient_mock: AiohttpClientMocker
) -> None:
    """The session's `ssl=` argument of every request is the cached `Fingerprint` of the client's digest."""
    seen: list[Any] = []
    session = async_get_clientsession(hass, verify_ssl=False)
    original = session._request

    async def spy(method: str, url: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("ssl"))
        return await original(method, url, **kwargs)

    aioclient_mock.get(f"{API}/version/", json={"api_version": "1.5.0"})
    aioclient_mock.post(f"{API}/register", json={"token": TOKEN})
    aioclient_mock.post(f"{API}/register/by-password", json={"token": TOKEN})
    aioclient_mock.get(f"{API}/project/junghome", json=_share_export())
    aioclient_mock.post(f"{API}/config", json={"message": "OK"})
    with patch.object(session, "_request", spy):
        await api.version()
        await api.register()
        await api.register_by_password("pw")
        await api.fetch_project()
        await api.upload_project(_share_export())
    assert seen == [fingerprint_ssl(FINGERPRINT)] * 5
    assert all(s is fingerprint_ssl(FINGERPRINT) for s in seen)
    assert api.fingerprint == FINGERPRINT


async def test_certificate_mismatch_is_its_own_error(
    api: JungHomeGatewayApi,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """aiohttp's handshake-time refusal (nothing was sent) carries both digests, and the token is not logged."""
    caplog.set_level(logging.DEBUG)
    api.token = TOKEN
    mismatch = aiohttp.ServerFingerprintMismatch(
        bytes.fromhex(FINGERPRINT), bytes.fromhex(OTHER_FINGERPRINT), HOST, 443
    )
    aioclient_mock.get(f"{API}/project/junghome", exc=mismatch)
    with pytest.raises(GatewayCertificateMismatch) as excinfo:
        await api.fetch_project()
    assert not isinstance(excinfo.value, GatewayUnreachable)
    assert (excinfo.value.host, excinfo.value.expected, excinfo.value.observed) == (
        HOST,
        FINGERPRINT,
        OTHER_FINGERPRINT,
    )
    assert "presents a certificate other than the pinned one" in caplog.text
    assert TOKEN not in caplog.text
    assert TOKEN not in str(excinfo.value)


async def test_client_refuses_a_malformed_fingerprint(hass: HomeAssistant) -> None:
    session = async_get_clientsession(hass, verify_ssl=False)
    with pytest.raises(ValueError, match="SHA-256"):
        JungHomeGatewayApi(session, HOST, "ab" * 16)
    with pytest.raises(ValueError, match="hexadecimal"):
        JungHomeGatewayApi(session, HOST, "zz" * 32)


# --------------------------------------------------------------------------- logging


async def test_bodies_never_logged(
    api: JungHomeGatewayApi,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Even at DEBUG the log carries routes and statuses only: no token, no key, no export."""
    caplog.set_level(logging.DEBUG)
    aioclient_mock.get(f"{API}/version/", json={"api_version": "1.5.0"})
    aioclient_mock.post(f"{API}/register", json={"token": TOKEN})
    aioclient_mock.get(f"{API}/project/junghome", status=500, json={"error": "boom"})
    await api.version()
    await api.register()
    with pytest.raises(GatewayError):
        await api.fetch_project()
    aioclient_mock.clear_requests()
    aioclient_mock.post(
        f"{API}/register/by-password", status=401, json={"error": "Unauthorized"}
    )
    with pytest.raises(GatewayAuthError):
        await api.register_by_password("secret-pw")

    log = caplog.text
    assert "gateway junghome.local" in log
    assert "HTTP 500" in log
    assert "HTTP 401" in log
    for secret in (TOKEN, _netkey(), "secret-pw", "boom", _bare_cdb()["meshUUID"]):
        assert secret not in log


async def test_upload_project(
    api: JungHomeGatewayApi, aioclient_mock: AiohttpClientMocker
) -> None:
    """`POST /config {"data": {"project_file": <export>}}` with the token — the app's upload after every change."""
    api.token = TOKEN
    aioclient_mock.post(f"{API}/config", json={"message": "OK"})
    await api.upload_project(_share_export())
    method, url, body, headers = aioclient_mock.mock_calls[0]
    assert (method, str(url), headers) == ("POST", f"{API}/config", {"token": TOKEN})
    assert body == {"data": {"project_file": _share_export()}}


async def test_upload_project_errors(
    api: JungHomeGatewayApi, aioclient_mock: AiohttpClientMocker
) -> None:
    api.token = TOKEN
    aioclient_mock.post(
        f"{API}/config", status=429, json={"error": "only a single request allowed"}
    )
    with pytest.raises(GatewayBusy):
        await api.upload_project(_share_export())
    aioclient_mock.clear_requests()
    aioclient_mock.post(
        f"{API}/config", status=400, json={"error": "validation error: schema"}
    )
    with pytest.raises(GatewayError, match=r"HTTP 400 \(validation error: schema\)"):
        await api.upload_project(_share_export())
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{API}/config", status=500, text="boom")
    with pytest.raises(GatewayError, match=r"HTTP 500$"):
        await api.upload_project(_share_export())
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{API}/config", status=401)
    with pytest.raises(GatewayAuthError):
        await api.upload_project(_share_export())


# --------------------------------------------------------------------------- status and error log


async def test_config(
    api: JungHomeGatewayApi, aioclient_mock: AiohttpClientMocker
) -> None:
    """`GET config` with the token: the app's `GatewayConfigDTO`, less the IP settings and the project file."""
    api.token = TOKEN
    aioclient_mock.get(
        f"{API}/config",
        json={
            "version_release": "2.1.3",
            "version_build": 2840,
            "system_serial": "1234567890",
            "ip_address": "192.0.2.10",
            "ip_mac": "00:00:5E:00:53:0D",
            "project_file": "JungHome.json",
            "cloud_register": True,
            "cloud_connect": False,
            "btmesh_device_not_available": False,
            "btmesh_error": True,
            "cloud_error": False,
            "ip_error": False,
            "api_clients": ["Home Assistant (Bluetooth Mesh)", "junghome"],
            "api_client_name_asking": ["Someone"],
        },
    )
    assert await api.config() == GatewayConfig(
        release="2.1.3",
        build="2840",
        serial="1234567890",
        cloud_registered=True,
        cloud_connected=False,
        mesh_device_missing=False,
        mesh_error=True,
        cloud_error=False,
        ip_error=False,
        api_clients=("Home Assistant (Bluetooth Mesh)", "junghome"),
        clients_asking=("Someone",),
    )
    method, url, data, headers = aioclient_mock.mock_calls[0]
    assert (method, str(url), data, headers) == (
        "GET",
        f"{API}/config",
        None,
        {"token": TOKEN},
    )


def test_parse_config_of_a_sparse_body() -> None:
    """Missing fields read empty / off; the firmware's own `api_client_name_asking` is one name (a string), which
    names nobody when empty; flags are on only when the gateway says `true`."""
    assert parse_config({"api_client_name_asking": "Someone", "cloud_error": 1}) == (
        GatewayConfig(
            release="",
            build="",
            serial="",
            cloud_registered=False,
            cloud_connected=False,
            mesh_device_missing=False,
            mesh_error=False,
            cloud_error=False,
            ip_error=False,
            api_clients=(),
            clients_asking=("Someone",),
        )
    )
    empty = parse_config({"api_client_name_asking": "", "api_clients": None})
    assert empty is not None
    assert (empty.api_clients, empty.clients_asking) == ((), ())
    assert parse_config(["not", "an", "object"]) is None


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"status": 429}, GatewayBusy),
        ({"status": 500, "json": {"error": "boom"}}, GatewayError),
        ({"json": ["not an object"]}, GatewayError),
        ({"status": 401}, GatewayAuthError),
    ],
    ids=["busy", "http_500", "not_an_object", "token_rejected"],
)
async def test_config_errors(
    api: JungHomeGatewayApi,
    aioclient_mock: AiohttpClientMocker,
    kwargs: dict[str, Any],
    error: type[GatewayError],
) -> None:
    api.token = TOKEN
    aioclient_mock.get(f"{API}/config", **kwargs)
    with pytest.raises(error):
        await api.config()


async def test_health_status(
    api: JungHomeGatewayApi, aioclient_mock: AiohttpClientMocker
) -> None:
    """`GET healthstatus`: the error log, entries that are no object skipped, missing fields empty."""
    api.token = TOKEN
    aioclient_mock.get(
        f"{API}/healthstatus",
        json=[
            {"level": "ERROR", "time": "t1", "description": "d", "details": "x"},
            "garbage",
            {"level": "DEBUG"},
        ],
    )
    assert await api.health_status() == [
        GatewayHealthEntry("ERROR", "t1", "d", "x"),
        GatewayHealthEntry("DEBUG", "", "", ""),
    ]
    assert aioclient_mock.mock_calls[0][3] == {"token": TOKEN}
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/healthstatus", json={"not": "a list"})
    with pytest.raises(GatewayError, match="GET healthstatus: HTTP 200"):
        await api.health_status()


@pytest.mark.parametrize(
    ("source", "expect_client"),
    [(None, False), ("gateway", True), ("path", False), ("upload", False)],
)
async def test_api_for_entry_honours_the_exports_source(
    hass: HomeAssistant, source: str | None, expect_client: bool
) -> None:
    """A path / upload export is the user's file: no client, whatever credentials the entry still carries. An entry
    without `CONF_SOURCE` gets one from `async_migrate_entry` before it is set up (HAC-07); until then it is no
    gateway entry either."""
    data: dict[str, Any] = {
        CONF_GATEWAY_HOST: HOST,
        CONF_GATEWAY_TOKEN: TOKEN,
        CONF_GATEWAY_FINGERPRINT: FINGERPRINT,
    }
    if source is not None:
        data[CONF_SOURCE] = source
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    client = api_for_entry(hass, entry)
    assert (client is not None) is expect_client
    if client is not None:
        assert (client.host, client.token) == (HOST, TOKEN)
    assert api_for_entry(hass, MockConfigEntry(domain=DOMAIN, data={})) is None
    no_credentials = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: "gateway"})
    assert api_for_entry(hass, no_credentials) is None
