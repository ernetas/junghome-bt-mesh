"""TLS certificate pinning for the JUNG HOME Gateway (the REST client of `gateway_api.py`).

The gateway serves its REST API over HTTPS with a **self-signed** certificate (CN `junghome.local`), so
certificate-authority verification is impossible and the default host is an mDNS name anyone on the LAN can claim.
With verification simply off, the very first request would hand an impostor the network-key password or the stored
token — and the token hands out the export with every mesh key.

The JUNG app closes that hole by pinning: it reads the gateway's certificate SHA-256 over the mesh (Manufacturer
Property `0xC003 gateway_fingerprint`, `jhmesh.properties`) and accepts only that certificate. This integration does
the same, with two sources for the pin:

- **over the mesh** (`async_read_mesh_fingerprint`), when the hub is connected — the mesh is authenticated with the
  AppKey, so this is the gateway itself vouching for its certificate; it wins over everything else;
- **trust on first use** (`async_learn_fingerprint`) otherwise: the digest is learned at first contact through a
  connection pinned to a digest no certificate can have (`PROBE_DIGEST`), which aiohttp aborts at the handshake and
  reports the peer's real digest for. No request line, no header, nothing leaves on that connection.

Every gateway request then passes `ssl=fingerprint_ssl(digest)`: aiohttp compares the certificate right after the
handshake, before a single HTTP byte — and so before the `token` header or the password — is written, and raises
`ServerFingerprintMismatch` otherwise. The config flow stores the pin next to the host and token
(`CONF_GATEWAY_FINGERPRINT`, with where it came from in `CONF_GATEWAY_PIN_SOURCE`) and, on a mismatch during a
re-fetch, asks the user to confirm the new certificate before anything is sent again — unless the gateway node
vouched for the pin, which no click overrides. A pin the mesh has not vouched for is compared with the node's report
by the hub before the gateway is used at runtime (`JungHomeHub.async_gateway_distrust`).
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import TYPE_CHECKING, Final

import aiohttp

from .const import CONF_GATEWAY_FINGERPRINT  # re-exported for the flow and tests
from .jhmesh import messages as M
from .jhmesh.devices import GATEWAY_PID

if TYPE_CHECKING:
    from .protocols import MeshPort

_LOGGER = logging.getLogger(__name__)

__all__ = ["CONF_GATEWAY_FINGERPRINT"]

# Length of a SHA-256 hex digest, the only form `CONF_GATEWAY_FINGERPRINT` holds.
FINGERPRINT_HEX_LENGTH: Final = 64

# A digest no certificate can have (SHA-256 preimage resistance). Pinning a connection to it guarantees aiohttp
# aborts at the handshake and reports the peer's real digest in `ServerFingerprintMismatch.got` — the learn step.
PROBE_DIGEST: Final = bytes(32)

# Bound for the learn-time handshake: the gateway is on the LAN, it either completes a TLS handshake within seconds
# or it is not answering, and a config-flow form is waiting on the result.
LEARN_TIMEOUT: Final = 10.0

# The gateway's certificate fingerprint as the app reads it: LBC Manufacturer Property 0xC003 of the gateway node.
PROPERTY_GATEWAY_FINGERPRINT: Final = 0xC003
MESH_READ_TIMEOUT: Final = (
    2.0  # seconds per attempt; two attempts keep a reconfigure form waiting 4 s at most
)
MESH_READ_ATTEMPTS: Final = 2


@lru_cache(maxsize=16)
def fingerprint_ssl(fingerprint: str) -> aiohttp.Fingerprint:
    """Return the `ssl=` argument that pins a connection to `fingerprint` (a SHA-256 hex digest).

    Cached per digest deliberately: aiohttp keys its connection pool on the `ssl` object by identity, so a fresh
    `Fingerprint` per request would defeat keep-alive. Raises `ValueError` on a malformed digest (a hand-edited
    entry) rather than silently pinning nothing.
    """
    digest = bytes.fromhex(fingerprint)
    if len(digest) != len(PROBE_DIGEST):
        raise ValueError("TLS fingerprint must be a SHA-256 hex digest")
    return aiohttp.Fingerprint(digest)


def normalize_fingerprint(value: object) -> str | None:
    """Return `value` as a stored fingerprint (64 lower-case hex digits), or None if it is not one.

    Accepts the digest with or without `:` separators and in either case, so a value read from the gateway over
    the mesh or copied from an `openssl` printout compares equal to a learned one.
    """
    if not isinstance(value, str):
        return None
    text = value.replace(":", "").strip().lower()
    if len(text) != FINGERPRINT_HEX_LENGTH:
        return None
    try:
        bytes.fromhex(text)
    except ValueError:
        return None
    return text


def format_fingerprint(fingerprint: str) -> str:
    """Render a hex digest in the conventional `AB:CD:...` form for display."""
    text = fingerprint.upper()
    return ":".join(text[i : i + 2] for i in range(0, len(text), 2))


async def async_learn_fingerprint(session: aiohttp.ClientSession, host: str) -> str:
    """Return the SHA-256 hex digest of the certificate `host` presents, without sending a request.

    Opens one TLS connection pinned to `PROBE_DIGEST`; aiohttp closes it at the handshake and the real digest
    comes back in the mismatch error. Network failures propagate as the `aiohttp.ClientError` / `TimeoutError`
    every caller already handles as "cannot connect". A peer that *accepts* the probe digest cannot exist; should
    the request somehow complete (a test double that ignores `ssl=`), that is reported as a connection error too,
    so a caller never proceeds without a real fingerprint.
    """
    url = f"https://{host}/api/junghome/version/"
    try:
        async with (
            asyncio.timeout(LEARN_TIMEOUT),
            session.get(url, ssl=aiohttp.Fingerprint(PROBE_DIGEST)),
        ):
            pass
    except aiohttp.ServerFingerprintMismatch as err:
        return err.got.hex()
    raise aiohttp.ClientConnectionError(
        f"{host} completed a TLS handshake without presenting a certificate"
    )


async def async_read_mesh_fingerprint(hub: MeshPort) -> str | None:
    """Ask the gateway node over the mesh for its certificate fingerprint (property 0xC003); None when it cannot.

    The gateway node is the one with product id `GATEWAY_PID` in the export; a mesh without one, no link, a
    silent gateway or an answer that is not a SHA-256 digest all read as "unknown" and the caller falls back to
    trust on first use. Only a Status of 0xC003 answers: the node's other Manufacturer Property Statuses (its IP
    address, which following the gateway asks for) come with the same opcode.
    """
    if not hub.connected:
        return None
    key = PROPERTY_GATEWAY_FINGERPRINT.to_bytes(2, "little")
    for node in hub.cdb.nodes:
        if node.pid != GATEWAY_PID:
            continue
        try:
            reply = await hub.proxy.request(
                node.unicast,
                M.vendor_property_get("manufacturer", PROPERTY_GATEWAY_FINGERPRINT),
                M.VENDOR_PROPERTY_STATUS_OPCODES["manufacturer"],
                timeout=MESH_READ_TIMEOUT,
                retries=MESH_READ_ATTEMPTS,
                expect_cid=M.JUNG_CID,
                match=lambda m: m.params[:2] == key,
            )
        except (TimeoutError, ConnectionError, OSError) as err:
            _LOGGER.debug(
                "gateway node %04X did not report its certificate: %s",
                node.unicast,
                type(err).__name__,
            )
            return None
        params = reply.params
        if len(params) < 3:
            return None
        text = params[3:].replace(b"\0", b"").decode(errors="replace")
        fingerprint = normalize_fingerprint(text)
        if fingerprint is None:
            _LOGGER.debug(
                "gateway node %04X reported a certificate fingerprint of %d characters, not a SHA-256 digest",
                node.unicast,
                len(text),
            )
        return fingerprint
    return None
