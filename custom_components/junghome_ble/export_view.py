"""The export Home Assistant holds, as a download for an administrator (`/api/junghome_ble/export/<entry id>`).

An entry keeps its export on the host (`CONF_CDB_PATH`; set up from the gateway, the export it last fetched or
adopted), and Home Assistant writes its own changes into it — rooms, scenes, keys, thresholds — which the app never
receives unless a gateway entry uploads them. This view hands the file over as it is on disk, under the name the app
gives its own share file, to re-import it into the app or keep it as a backup without shell access to the host. The
file holds every key of the installation, so the view answers an administrator only, and is reached in a browser
through a short-lived signed link (`async_sign_path`) the action `junghome_ble.download_export` makes
(`actions/download.py`); without a signature or a session it answers 401. The bytes are never logged, nor is the
link.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final

from aiohttp import hdrs, web
from homeassistant.components.http.const import KEY_HASS_USER
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView

from .actions.common import CONFIGURATORS
from .const import CONF_CDB_PATH, CONF_METADATA_DIR, DOMAIN

if TYPE_CHECKING:
    from homeassistant.auth.models import User
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

EXPORT_URL: Final = f"/api/{DOMAIN}/export/{{entry_id}}"
# the name the JUNG HOME app gives its share file (*Project → Share via file*), which its import takes
EXPORT_FILENAME: Final = "JungHome.json"


def export_filename(entry: ConfigEntry) -> str:
    """Return the name the download is saved under: the app's share file, or the file's own name.

    An entry set up from the iOS app's mesh database and its metadata folder (`CONF_METADATA_DIR`) keeps the
    database, which is no share file: it keeps its own name (`MeshNetwork.json`), and `export_network` renders
    the share file from it.
    """
    if entry.data.get(CONF_METADATA_DIR):
        return Path(str(entry.data[CONF_CDB_PATH])).name
    return EXPORT_FILENAME


def export_path(entry_id: str) -> str:
    """Return the view's path for one entry (unsigned: `async_sign_path` signs it)."""
    return EXPORT_URL.format(entry_id=entry_id)


async def async_export_bytes(hass: HomeAssistant, entry: ConfigEntry) -> bytes:
    """Read the entry's export as it is on disk, after the write of an operation running on it.

    A loaded entry's file is read under its configurator's lock (`ExportStore.async_file`): a plan being sent and the
    write recording it end first. An entry not loaded has nobody writing its file — every write replaces it whole
    (`write_private`) — so it is read as it is. `OSError` when it cannot be.
    """
    configurator = hass.data.get(CONFIGURATORS, {}).get(entry.entry_id)
    if configurator is not None:
        return await configurator.store.async_file()
    return await hass.async_add_executor_job(
        Path(str(entry.data[CONF_CDB_PATH])).read_bytes
    )


class ExportDownloadView(HomeAssistantView):
    """`GET /api/junghome_ble/export/<entry id>`: the entry's export file, for an administrator only."""

    url = EXPORT_URL
    name = f"api:{DOMAIN}:export"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str) -> web.Response:
        """Answer the file as an attachment (`export_filename`, never cached); 403 to a user who is no administrator.

        404 for an id that is no entry of this integration, or whose export is gone from the host; 500 when the
        file cannot be read. Each download is logged with the user's name — not the file, not the link.
        """
        hass = request.app[KEY_HASS]
        user: User = request[KEY_HASS_USER]
        if not user.is_admin:
            return self.json_message(
                "Only an administrator may download the JUNG HOME export: it holds every key of the installation",
                HTTPStatus.FORBIDDEN,
            )
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            return self.json_message(
                "No JUNG HOME Bluetooth Mesh entry has this id", HTTPStatus.NOT_FOUND
            )
        try:
            body = await async_export_bytes(hass, entry)
        except FileNotFoundError:
            return self.json_message(
                "This JUNG HOME entry keeps no export on the host (the file is gone): set it up again from the app's "
                "export or the gateway",
                HTTPStatus.NOT_FOUND,
            )
        except OSError as err:
            _LOGGER.warning(
                "The export of %s could not be read for a download: %s",
                entry.title,
                type(err).__name__,
            )
            return self.json_message(
                "The JUNG HOME export could not be read; the log says why",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        _LOGGER.info("%s downloaded the export of %s", user.name, entry.title)
        return web.Response(
            body=body,
            content_type="application/json",
            headers={
                hdrs.CONTENT_DISPOSITION: f'attachment; filename="{export_filename(entry)}"',
                hdrs.CACHE_CONTROL: "no-store",
            },
        )
