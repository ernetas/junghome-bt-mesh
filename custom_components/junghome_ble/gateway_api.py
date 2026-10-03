"""A tiny REST client for the JUNG HOME Gateway: registration and the project export.

Base URL `https://<host>/api/junghome`; the token travels in the `token` header. The gateway's certificate is
self-signed, so instead of CA verification every request is **pinned** to the certificate's SHA-256 (`tls.py`):
the client refuses to exist without a fingerprint, and aiohttp checks it right after the TLS handshake — before
the request line, the password or the token is written — raising `GatewayCertificateMismatch` otherwise. Only
what the config flow and the configurator need: `version()` as a reachability probe, the two ways to obtain a
token (`register()` waits until the user approves the request in the app, `register_by_password()` is immediate),
`fetch_project()` for the app's export the gateway holds and `upload_project()` to hand a changed export back (the
app's `POST config {"data": {"project_file": …}}`); `config()` and `health_status()` read what the app's gateway
pages show (its status and its error log, `gateway_status.py`); `approve_client()` approves an API client's access
request as the app's *Access permissions* page does (`POST config {"data": {"api_client_accept": …}}`, review-4
F4-17). The app's *revoke all* (`api_client_reset`, which would revoke Home Assistant's own token too) and the
gateway's network settings are left out on purpose. Response bodies are never logged: the export carries every mesh
key and the register replies carry the token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_GATEWAY_FINGERPRINT,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_TOKEN,
    CONF_SOURCE,
    GATEWAY_PROBE_TIMEOUT,
    GATEWAY_REGISTER_TIMEOUT,
    GATEWAY_REQUEST_TIMEOUT,
    GATEWAY_UPLOAD_TIMEOUT,
    GATEWAY_USER_NAME,
)
from .tls import fingerprint_ssl

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404
HTTP_TOO_MANY = 429  # the gateway takes one configuration request at a time


class GatewayError(Exception):
    """The gateway did not do what was asked (an unexpected HTTP status or body)."""


class GatewayUnreachable(GatewayError):
    """No HTTP answer at all: wrong host, gateway off, DNS or TLS trouble, or a timeout."""


class GatewayAuthError(GatewayError):
    """HTTP 401: the password is wrong, or the gateway no longer accepts the token."""


class GatewayNotApproved(GatewayError):
    """The access request was not approved in the app before the gateway gave up on it (HTTP 400)."""


class GatewayBusy(GatewayError):
    """The gateway is applying another configuration request (HTTP 429); try again in a minute."""


class GatewayNoProject(GatewayError):
    """The gateway holds no project export: both project routes failed, or the body is not a mesh export."""


class GatewayCertificateMismatch(GatewayError):
    """The responder at the host presents another certificate than the pinned one; nothing was sent to it.

    `expected` and `observed` are SHA-256 hex digests (the pin and what the peer presented): what the config flow
    shows the user before asking whether to trust the new certificate.
    """

    def __init__(self, host: str, expected: str, observed: str) -> None:
        """Record the host and both digests; the message names neither the token nor a path."""
        super().__init__(f"the certificate of gateway {host} changed")
        self.host = host
        self.expected = expected
        self.observed = observed


@dataclass(frozen=True)
class GatewayVersion:
    """What `GET /version/` reports."""

    api: str  # api-server version, e.g. "1.5.0"; the project routes exist from 1.5.0 on
    release: str  # gateway firmware release, e.g. "2.1.3" ("0.0.0" until the middleware has reported it)
    build: str


@dataclass(frozen=True)
class GatewayConfig:
    """What `GET config` reports that Home Assistant shows: the app's `GatewayConfigDTO`, less the IP settings.

    The IP settings (address, subnet, DNS, router, MAC, DHCP) are left out: the address is the entry's and the
    node's (`0xC002`), the rest is the network's business. So is `project_file`, whatever the gateway puts there.
    """

    release: str  # firmware release, e.g. "2.1.3"
    build: str
    serial: str
    cloud_registered: bool
    cloud_connected: bool
    mesh_device_missing: bool  # btmesh_device_not_available: the gateway's own Bluetooth radio is not there
    mesh_error: bool
    cloud_error: bool
    ip_error: bool
    api_clients: tuple[
        str, ...
    ]  # the third-party API clients the gateway accepts (Home Assistant among them)
    clients_asking: tuple[str, ...]  # access requests waiting for approval in the app


@dataclass(frozen=True)
class GatewayHealthEntry:
    """One entry of the gateway's error log (`GET healthstatus`, the app's `GatewayHealthStatusEntryDto`)."""

    level: str  # e.g. "ERROR", "WARNING", "DEBUG": the app hides DEBUG entries unless told otherwise
    time: str  # as the gateway writes it
    description: str
    details: str


def _names(value: Any) -> tuple[str, ...]:
    """Return the client names of a `GET config` field: a list (the app's DTO), or one name.

    The firmware compares its own `api_client_name_asking` with the empty string, so it may be one name; anything
    else names nobody.
    """
    if isinstance(value, list):
        return tuple(str(v) for v in value if v)
    if isinstance(value, str) and value:
        return (value,)
    return ()


def parse_config(body: Any) -> GatewayConfig | None:
    """Return the status in a `GET config` body, None when the body is no object; missing fields read empty / off."""
    if not isinstance(body, dict):
        return None

    def text(key: str) -> str:
        value = body.get(key)
        return "" if value is None else str(value)

    return GatewayConfig(
        release=text("version_release"),
        build=text("version_build"),
        serial=text("system_serial"),
        cloud_registered=body.get("cloud_register") is True,
        cloud_connected=body.get("cloud_connect") is True,
        mesh_device_missing=body.get("btmesh_device_not_available") is True,
        mesh_error=body.get("btmesh_error") is True,
        cloud_error=body.get("cloud_error") is True,
        ip_error=body.get("ip_error") is True,
        api_clients=_names(body.get("api_clients")),
        clients_asking=_names(body.get("api_client_name_asking")),
    )


def parse_health(body: Any) -> list[GatewayHealthEntry] | None:
    """Return the entries of a `GET healthstatus` body (a list of objects), None when it is no list."""
    if not isinstance(body, list):
        return None
    return [
        GatewayHealthEntry(
            level=str(item.get("level") or ""),
            time=str(item.get("time") or ""),
            description=str(item.get("description") or ""),
            details=str(item.get("details") or ""),
        )
        for item in body
        if isinstance(item, dict)
    ]


def api_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> JungHomeGatewayApi | None:
    """Return the gateway client of an entry set up from a gateway (host, token and pinned certificate), else None.

    A malformed fingerprint in the entry yields None too: there is nothing to pin to, so nothing is sent. So does
    an entry whose export comes from a path or an upload (`CONF_SOURCE`): that file is the user's, not a copy of
    the gateway's, so it is neither refreshed from the gateway nor uploaded to it, whatever credentials the
    entry still carries. Every set-up entry names its source (`__init__.async_migrate_entry`).
    """
    data = entry.data
    if data.get(CONF_SOURCE) != "gateway":
        return None
    host, token, fingerprint = (
        data.get(CONF_GATEWAY_HOST),
        data.get(CONF_GATEWAY_TOKEN),
        data.get(CONF_GATEWAY_FINGERPRINT),
    )
    if not (host and token and fingerprint):
        return None
    try:
        return JungHomeGatewayApi(
            async_get_clientsession(hass, verify_ssl=False),
            str(host),
            str(fingerprint),
            str(token),
        )
    except ValueError:
        return None


def _token_of(body: Any) -> str | None:
    token = body.get("token") if isinstance(body, dict) else None
    return str(token) if token else None


def as_export(body: Any) -> dict[str, Any] | None:
    """Return the body in a shape `CDB.parse` accepts, or None when it is not a mesh export.

    `GET /project/junghome` returns the app's `ExportDto` as it was uploaded (`{"version", "meta", "network":
    "<Base64 CDB JSON>"}`, keys camel-cased); `GET /project/cdb` returns the decoded CDB, which is wrapped as
    `{"meshNetwork": …}` — the iOS `MeshNetwork.json` shape — so both land in one loader.
    """
    if not isinstance(body, dict):
        return None
    if isinstance(body.get("network"), str):
        return body
    if isinstance(body.get("meshNetwork"), dict):
        return body
    if isinstance(body.get("nodes"), list) and isinstance(body.get("netKeys"), list):
        return {"meshNetwork": body}
    return None


class JungHomeGatewayApi:
    """The few gateway calls the integration makes; one instance per host."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        fingerprint: str,
        token: str | None = None,
    ) -> None:
        """Bind to `host` (IP or name), pinned to the certificate with SHA-256 `fingerprint` (hex).

        `token` may be filled in later by `register*`. A malformed fingerprint raises `ValueError` here, so a
        client that cannot pin never exists.
        """
        self._session = session
        self.host = host
        self.fingerprint = fingerprint
        self._ssl = fingerprint_ssl(fingerprint)
        self.token = token

    @property
    def base_url(self) -> str:
        """The API root of this gateway."""
        return f"https://{self.host}/api/junghome"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        json: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> tuple[int, Any]:
        """Return (status, decoded JSON body or None); transport failures become `GatewayUnreachable`.

        The connection is pinned to the fingerprint: a peer with another certificate is refused at the TLS
        handshake (`GatewayCertificateMismatch`), before the request — headers and body included — is written.
        """
        headers = {"token": self.token} if auth and self.token else {}
        try:
            async with self._session.request(
                method,
                f"{self.base_url}/{path}",
                json=json,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                ssl=self._ssl,
                # a redirect would carry the token header to wherever the gateway points (review-3 S2): the
                # gateway's API never redirects, so a 3xx is an answer like any other non-200
                allow_redirects=False,
            ) as response:
                status = response.status
                try:
                    body = await response.json(content_type=None)
                except ValueError:
                    body = None
        except aiohttp.ServerFingerprintMismatch as err:
            _LOGGER.warning(
                "gateway %s presents a certificate other than the pinned one; nothing was sent to it",
                self.host,
            )
            raise GatewayCertificateMismatch(
                self.host, err.expected.hex(), err.got.hex()
            ) from err
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug(
                "%s %s on gateway %s failed: %s",
                method,
                path,
                self.host,
                type(err).__name__,
            )
            raise GatewayUnreachable(f"{method} {path}: {type(err).__name__}") from err
        _LOGGER.debug("%s %s on gateway %s: HTTP %s", method, path, self.host, status)
        if status == HTTP_UNAUTHORIZED:
            raise GatewayAuthError(f"{method} {path}: HTTP 401")
        return status, body

    async def version(self) -> GatewayVersion:
        """`GET /version/` (no token needed): the API and firmware versions; doubles as a reachability probe."""
        status, body = await self._request(
            "GET", "version/", timeout=GATEWAY_PROBE_TIMEOUT, auth=False
        )
        if status != HTTP_OK or not isinstance(body, dict):
            raise GatewayError(f"GET version/: HTTP {status}")
        return GatewayVersion(
            api=str(body.get("api_version", "")),
            release=str(body.get("version_release", "")),
            build=str(body.get("version_build", "")),
        )

    async def config(self) -> GatewayConfig:
        """`GET config`: the gateway's status, what the app polls for its gateway pages (`PollGatewayConfig`)."""
        status, body = await self._request(
            "GET", "config", timeout=GATEWAY_REQUEST_TIMEOUT
        )
        if status == HTTP_TOO_MANY:
            raise GatewayBusy("the gateway is busy with another configuration request")
        if status != HTTP_OK or (config := parse_config(body)) is None:
            raise GatewayError(f"GET config: HTTP {status}")
        return config

    async def health_status(self) -> list[GatewayHealthEntry]:
        """`GET healthstatus`: the gateway's error log, in the order it lists it (what the app's log page shows)."""
        status, body = await self._request(
            "GET", "healthstatus", timeout=GATEWAY_REQUEST_TIMEOUT
        )
        if status != HTTP_OK or (entries := parse_health(body)) is None:
            raise GatewayError(f"GET healthstatus: HTTP {status}")
        return entries

    async def approve_client(self, name: str) -> None:
        """Approve the API client `name` asks for access as (`POST config {"data": {"api_client_accept": name}}`).

        What the app's *Access permissions → Open requests* does (`PermissionsDTO`); the client then gets a token
        for the gateway's whole API. `GatewayBusy` on 429, `GatewayError` with the gateway's message otherwise.
        Unverified on air.
        """
        await self._post_config({"api_client_accept": name}, GATEWAY_REQUEST_TIMEOUT)

    async def register(self, user_name: str = GATEWAY_USER_NAME) -> str:
        """`POST /register`: ask for a token and wait until the user approves the request in the app.

        The gateway holds the request open for 180 s (Settings → Gateway → Access permissions → Open requests in
        the app) and answers 400 when that window passes.
        """
        status, body = await self._request(
            "POST",
            "register",
            timeout=GATEWAY_REGISTER_TIMEOUT,
            json={"user_name": user_name},
            auth=False,
        )
        if status == HTTP_BAD_REQUEST:
            raise GatewayNotApproved("the access request was not approved in time")
        if status != HTTP_OK or (token := _token_of(body)) is None:
            raise GatewayError(f"POST register: HTTP {status} without a token")
        self.token = token
        return token

    async def register_by_password(self, password: str) -> str:
        """`POST /register/by-password`: a token right away for the gateway's network-key password (401 if wrong)."""
        status, body = await self._request(
            "POST",
            "register/by-password",
            timeout=GATEWAY_REQUEST_TIMEOUT,
            json={"password": password},
            auth=False,
        )
        if status != HTTP_OK or (token := _token_of(body)) is None:
            raise GatewayError(
                f"POST register/by-password: HTTP {status} without a token"
            )
        self.token = token
        return token

    async def fetch_project(self) -> dict[str, Any]:
        """Return the mesh export the gateway holds, in a shape `CDB.parse` accepts.

        `GET /project/junghome` first (the app's export with the names). The `/project/cdb` fallback (keys and
        topology only, wrapped as `{"meshNetwork": …}`) is for firmware without the route (404) or a 200 with no
        project uploaded yet — never for a busy or failing gateway, whose current export the fallback cannot
        stand in for (adopting the bare CDB would strip every device name and room link from the file). Both
        routes need the token. The body is returned, never logged.
        """
        status, body = await self._request(
            "GET", "project/junghome", timeout=GATEWAY_REQUEST_TIMEOUT
        )
        if status == HTTP_OK and (doc := as_export(body)) is not None:
            return doc
        if status == HTTP_TOO_MANY:
            raise GatewayBusy("the gateway is busy with another configuration request")
        if status not in (HTTP_OK, HTTP_NOT_FOUND):
            raise GatewayError(f"GET project/junghome: HTTP {status}")
        _LOGGER.debug(
            "GET project/junghome on gateway %s answered HTTP %s without a usable export",
            self.host,
            status,
        )
        status, body = await self._request(
            "GET", "project/cdb", timeout=GATEWAY_REQUEST_TIMEOUT
        )
        if status == HTTP_OK and (doc := as_export(body)) is not None:
            return doc
        _LOGGER.debug(
            "GET project/cdb on gateway %s answered HTTP %s without a usable export",
            self.host,
            status,
        )
        raise GatewayNoProject("the gateway holds no project export")

    async def upload_project(self, export: dict[str, Any]) -> None:
        """Hand a changed export to the gateway — what the app does after every change it makes.

        `POST /config {"data": {"project_file": <share export>}}`; the gateway validates it, reconfigures itself
        from the CDB, rebuilds its device database and answers `200` once the self-configuration went through (a
        few seconds). `GatewayBusy` on 429 (another configuration request is running, retry in a minute),
        `GatewayError` with the gateway's message otherwise.
        """
        await self._post_config({"project_file": export}, GATEWAY_UPLOAD_TIMEOUT)

    async def _post_config(self, data: dict[str, Any], timeout: float) -> None:
        """`POST config {"data": data}`, every command's route; `GatewayBusy` on 429, `GatewayError` unless 200."""
        status, body = await self._request(
            "POST", "config", timeout=timeout, json={"data": data}
        )
        if status == HTTP_TOO_MANY:
            raise GatewayBusy("the gateway is busy with another configuration request")
        if status != HTTP_OK:
            detail = body.get("error") if isinstance(body, dict) else None
            raise GatewayError(
                f"POST config: HTTP {status}" + (f" ({detail})" if detail else "")
            )
