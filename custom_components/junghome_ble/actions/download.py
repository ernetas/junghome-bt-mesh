"""The `download_export` action: a short-lived signed link to the export Home Assistant holds.

The export on the host carries Home Assistant's changes, which the app never receives unless a gateway entry uploads
them. The action answers the path of `export_view.ExportDownloadView` for the entry, signed with Home Assistant's
own `async_sign_path` for `LINK_LIFETIME` and for the session of the administrator who asks — the browser tab of
*Developer tools → Actions*, the API token of a script. The signature is a bearer credential: whoever holds the link
opens it without a login for five minutes, as that administrator (the view checks the administrator again), so the
texts tell the owner to open it themselves and pass it to no one. The path comes with the absolute URL Home
Assistant knows for itself, when it knows one, so it can be opened from the response. A call without such a session
(an automation of the system) is refused: a link signed for nobody would open for nobody, and one signed for Home
Assistant's content user would answer 403. Neither the link nor its signature is logged; the log says that a link
was made, and for whom.

The file is the export as Home Assistant last wrote it. An open *the JUNG HOME app changed the installation* or
*devices missing from the export* repair means the app knows more than that file: imported into the app, it would
take those changes back out, so the response says `stale` (and the guide says to load the app's export first).

That the JUNG HOME app imports the downloaded file — with the rows Home Assistant wrote — is unverified with the
app (docs/on-air-sweep.md).
"""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.http.const import KEY_HASS_REFRESH_TOKEN_ID, KEY_HASS_USER
from homeassistant.components.websocket_api.connection import current_connection
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.http import current_request
from homeassistant.helpers.network import NoURLAvailableError, get_url

from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    DOMAIN,
    ISSUE_APP_CHANGED,
    ISSUE_UNKNOWN_NODES,
    issue_id,
)
from custom_components.junghome_ble.export_view import export_path

from .common import _ENTRY_FIELD, ATTR_CONFIG_ENTRY, ATTR_DEVICE, _validation
from .resolve import _registry_device

if TYPE_CHECKING:
    # Home Assistant validates with probatio from 2026.10 and aliases `voluptuous` to it on import; the floor
    # release (hacs.json) has no probatio, so the schemas are built with voluptuous and typed as probatio.
    import probatio as vol
else:
    import voluptuous as vol

if TYPE_CHECKING:
    from homeassistant.auth.models import User
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse

_LOGGER = logging.getLogger(__name__)

LINK_LIFETIME: Final = timedelta(minutes=5)
DOWNLOAD_EXPORT_SCHEMA = vol.All(
    vol.Schema({vol.Optional(ATTR_DEVICE): cv.string, **_ENTRY_FIELD}),
    cv.has_at_most_one_key(ATTR_DEVICE, ATTR_CONFIG_ENTRY),
)


def _session_of(user_id: str | None) -> tuple[str, User] | None:
    """Return the refresh token of the session the call came through, with its user, when it is `user_id`'s.

    A call from the frontend runs inside its websocket connection, one through the REST API inside its request:
    either names the refresh token the caller holds, which the signature is made for. Anything else — no user, a
    connection of another user — has no session to sign for.
    """
    if user_id is None:
        return None
    connection = current_connection.get()
    if (
        connection is not None
        and connection.refresh_token_id is not None
        and connection.user.id == user_id
    ):
        return connection.refresh_token_id, connection.user
    request = current_request.get()
    if (
        request is not None
        and KEY_HASS_REFRESH_TOKEN_ID in request
        and request[KEY_HASS_USER].id == user_id
    ):
        return request[KEY_HASS_REFRESH_TOKEN_ID], request[KEY_HASS_USER]
    return None


def _entry_with_export(hass: HomeAssistant, entry_id: str | None) -> str:
    """Return the entry whose export to hand over: `entry_id`'s, else the only loaded one, else the only one.

    Loaded or not: an entry in setup retry, or one that failed to set up, still has its export on the host
    (`export_view.async_export_bytes`), and a copy of it is wanted most then. An ignored discovery has none.
    """
    if entry_id is not None:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN or CONF_CDB_PATH not in entry.data:
            raise _validation("service_unknown_entry", id=entry_id)
        return entry_id
    entries = [
        e for e in hass.config_entries.async_entries(DOMAIN) if CONF_CDB_PATH in e.data
    ]
    loaded = [e for e in entries if e.state is ConfigEntryState.LOADED]
    candidates = loaded or entries
    if not candidates:
        raise _validation("service_entry_not_loaded")
    if len(candidates) > 1:
        raise _validation("service_entry_ambiguous")
    return candidates[0].entry_id


def _absolute(hass: HomeAssistant, path: str) -> str | None:
    """Return `path` as a full URL: on the address the caller reached Home Assistant at, else its configured one.

    None when Home Assistant knows no address of its own (no internal or external URL, no usable request host):
    the caller then puts the path after the address they use.
    """
    for current in (True, False):
        with suppress(NoURLAvailableError):
            return get_url(hass, require_current_request=current) + path
    return None


def _stale(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Whether the app is known to hold changes the file lacks: `app_changed` or `unknown_nodes` is open."""
    registry = ir.async_get(hass)
    return any(
        (issue := registry.async_get_issue(DOMAIN, issue_id(entry, key))) is not None
        and issue.active
        for key in (ISSUE_APP_CHANGED, ISSUE_UNKNOWN_NODES)
    )


async def _download_export(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Answer `{"url", "absolute_url", "expires_in", "stale"}`: the entry's export, signed for five minutes.

    Administrators only. The entry is `config_entry_id`'s, `device`'s (any device of the entry), or the only one
    loaded (the only one at all when none is), loaded or not (`_entry_with_export`). `url` is a path on Home
    Assistant's own address, `absolute_url` the same with that address (None when Home Assistant knows none), `stale`
    whether the app holds changes the file lacks (`_stale`). Unverified with the app: no app has been seen importing
    the file.
    """
    if (device_id := call.data.get(ATTR_DEVICE)) is not None:
        _device, entry_id = _registry_device(hass, device_id)
    else:
        entry_id = _entry_with_export(hass, call.data.get(ATTR_CONFIG_ENTRY))
    entry = hass.config_entries.async_get_entry(entry_id)
    assert entry is not None  # resolved just above
    if (session := _session_of(call.context.user_id)) is None:
        raise _validation("download_export_no_session")
    token, user = session
    url = async_sign_path(
        hass, export_path(entry_id), LINK_LIFETIME, refresh_token_id=token
    )
    _LOGGER.info(
        "Made a download link for the export of %s, valid for %d s, for %s",
        entry.title,
        LINK_LIFETIME.total_seconds(),
        user.name,
    )
    return {
        "url": url,
        "absolute_url": _absolute(hass, url),
        "expires_in": int(LINK_LIFETIME.total_seconds()),
        "stale": _stale(hass, entry),
    }
