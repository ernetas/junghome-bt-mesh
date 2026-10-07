"""The `download_export` action: a short-lived signed link to the export Home Assistant holds (review-4 U4-17).

The export on the host carries Home Assistant's changes, which the app never receives unless a gateway entry uploads
them. The action answers the path of `export_view.ExportDownloadView` for the entry, signed with Home Assistant's
own `async_sign_path` for `LINK_LIFETIME` and for the session of the administrator who asks — the browser tab of
*Developer tools → Actions*, the API token of a script — so the link opens without a login only for five minutes and
only as that administrator (the view checks it again). A call without such a session (an automation of the system)
is refused: a link signed for nobody would open for nobody, and one signed for Home Assistant's content user would
answer 403. Neither the link nor its signature is logged; the log says that a link was made, and for whom.

That the JUNG HOME app imports the downloaded file — with the rows Home Assistant wrote — is unverified with the
app (docs/on-air-sweep.md).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Final

import voluptuous as vol
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.http.const import KEY_HASS_REFRESH_TOKEN_ID, KEY_HASS_USER
from homeassistant.components.websocket_api.connection import current_connection
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.http import current_request

from custom_components.junghome_ble.const import CONF_CDB_PATH, DOMAIN
from custom_components.junghome_ble.export_view import export_path

from .common import _ENTRY_FIELD, ATTR_CONFIG_ENTRY, ATTR_DEVICE, _validation
from .resolve import _registry_device

if TYPE_CHECKING:
    from homeassistant.auth.models import User
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


async def _download_export(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Answer `{"url", "expires_in"}`: the entry's export, signed for five minutes (admin only).

    The entry is `config_entry_id`'s, `device`'s (any device of the entry), or the only one loaded (the only one
    at all when none is), loaded or not (`_entry_with_export`). The URL is a path on Home Assistant's own address. Unverified with the app: no app has been seen importing the file.
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
    return {"url": url, "expires_in": int(LINK_LIFETIME.total_seconds())}
