"""The export as a download (review-4 U4-17): `export_view.ExportDownloadView` and `junghome_ble.download_export`.

The view serves an entry's export as it is on disk to an administrator — after the write of an operation running on
it — and the action answers a path to it signed for five minutes for the administrator who asks. The file here is
the fixture share export, a copy in the test's directory (synthetic keys); nothing of it is printed.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import voluptuous as vol
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome_ble.actions.common import CONFIGURATORS
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_METADATA_DIR,
    CONF_UNICAST,
    DOMAIN,
)
from custom_components.junghome_ble.export_view import EXPORT_FILENAME, export_path

from .conftest import CDB_PATH, FIXTURES, META_DIR

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.auth.models import User
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.typing import (
        ClientSessionGenerator,
        WebSocketGenerator,
    )

SHARE_EXPORT = FIXTURES / "JungHome.json"


@pytest.fixture
def export_file(tmp_path: Path) -> Path:
    """A copy of the fixture share export, where the entry keeps its export."""
    path = tmp_path / "junghome" / "JungHome.json"
    path.parent.mkdir()
    shutil.copy(SHARE_EXPORT, path)
    return path


@pytest.fixture
def mock_config_entry(export_file: Path) -> MockConfigEntry:
    """The entry `init_integration` sets up: one set up from the share export, kept at `export_file`."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1baf3ade-0000-4000-8000-000000000001",
        data={CONF_CDB_PATH: str(export_file), CONF_UNICAST: "0D00"},
    )


def read(path: Path | str) -> bytes:
    return Path(path).read_bytes()


def write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def gone(path: Path) -> None:
    """The export removed from the host."""
    path.unlink()


def unreadable(path: Path) -> None:
    """Something at the export's path that cannot be read as a file."""
    path.mkdir()


def restore(path: Path, data: bytes) -> None:
    path.rmdir()
    path.write_bytes(data)


async def ws_download(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, **data: Any
) -> dict[str, Any]:
    """Call the action from a websocket connection, as *Developer tools → Actions* does; return its response."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "call_service",
            "domain": DOMAIN,
            "service": "download_export",
            "service_data": data,
            "return_response": True,
        }
    )
    message = await client.receive_json()
    assert message["success"], message
    response: dict[str, Any] = message["result"]["response"]
    return response


# --------------------------------------------------------------------------- the view


async def test_an_administrator_gets_the_file_as_an_attachment(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    export_file: Path,
    hass_client: ClientSessionGenerator,
    hass_admin_user: User,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    client = await hass_client()
    response = await client.get(export_path(init_integration.entry_id))
    assert response.status == HTTPStatus.OK
    assert await response.read() == read(export_file)
    assert response.headers["Content-Type"] == "application/json"
    assert (
        response.headers["Content-Disposition"]
        == f'attachment; filename="{EXPORT_FILENAME}"'
    )
    assert EXPORT_FILENAME == "JungHome.json"  # what the app names its share file
    assert response.headers["Cache-Control"] == "no-store"
    assert (
        f"{hass_admin_user.name} downloaded the export of {init_integration.title}"
        in caplog.text
    )


async def test_a_mesh_database_keeps_its_own_name(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_client: ClientSessionGenerator,
) -> None:
    """An entry set up from the iOS app's mesh database and its metadata folder keeps the database: no share file,
    so it is not offered under the share file's name. (This one is not loaded: its file is read as it is.)"""
    database = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh database",
        data={CONF_CDB_PATH: CDB_PATH, CONF_METADATA_DIR: META_DIR},
    )
    database.add_to_hass(hass)
    client = await hass_client()
    response = await client.get(export_path(database.entry_id))
    assert response.status == HTTPStatus.OK
    assert await response.read() == read(CDB_PATH)
    assert (
        response.headers["Content-Disposition"]
        == 'attachment; filename="MeshNetwork.json"'
    )


async def test_a_user_who_is_no_administrator_is_refused(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_read_only_access_token: str,
) -> None:
    client = await hass_client(hass_read_only_access_token)
    response = await client.get(export_path(init_integration.entry_id))
    assert response.status == HTTPStatus.FORBIDDEN
    assert b'"network"' not in await response.read()


async def test_without_a_login_or_a_signature_nothing_is_served(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    client = await hass_client_no_auth()
    response = await client.get(export_path(init_integration.entry_id))
    assert response.status == HTTPStatus.UNAUTHORIZED
    forged = await client.get(
        f"{export_path(init_integration.entry_id)}?authSig=not-a-signature"
    )
    assert forged.status == HTTPStatus.UNAUTHORIZED


async def test_an_id_that_is_no_entry_of_ours_is_not_found(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_client: ClientSessionGenerator,
) -> None:
    other = MockConfigEntry(domain="other_integration")
    other.add_to_hass(hass)
    client = await hass_client()
    for entry_id in ("no-such-entry", other.entry_id):
        response = await client.get(export_path(entry_id))
        assert response.status == HTTPStatus.NOT_FOUND
        assert "No JUNG HOME" in (await response.json())["message"]


async def test_an_export_gone_from_the_host_is_not_found_an_unreadable_one_fails(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    export_file: Path,
    hass_client: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = await hass_client()
    original = read(export_file)
    gone(export_file)
    response = await client.get(export_path(init_integration.entry_id))
    assert response.status == HTTPStatus.NOT_FOUND
    assert "keeps no export" in (await response.json())["message"]
    unreadable(export_file)
    response = await client.get(export_path(init_integration.entry_id))
    assert response.status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert "could not be read for a download: IsADirectoryError" in caplog.text
    restore(export_file, original)


async def test_the_running_operation_writes_the_file_first(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    export_file: Path,
    hass_client: ClientSessionGenerator,
) -> None:
    """A plan being sent holds the configurator's lock until the write that records it: the download waits for it
    and serves what was written, never the file from before the change."""
    configurator = hass.data[CONFIGURATORS][init_integration.entry_id]
    client = await hass_client()
    written = read(export_file) + b"\n"
    async with configurator.lock:
        download = asyncio.ensure_future(
            client.get(export_path(init_integration.entry_id))
        )
        for _ in range(20):
            await asyncio.sleep(0)
        assert not download.done()
        write(export_file, written)  # the operation's write
    response = await download
    assert response.status == HTTPStatus.OK
    assert await response.read() == written


async def test_an_entry_not_loaded_is_served_as_it_is_on_disk(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    export_file: Path,
    hass_client: ClientSessionGenerator,
) -> None:
    """Nothing writes the export of an entry no hub runs (a link made before a reload still works meanwhile)."""
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert init_integration.entry_id not in hass.data.get(CONFIGURATORS, {})
    client = await hass_client()
    response = await client.get(export_path(init_integration.entry_id))
    assert response.status == HTTPStatus.OK
    assert await response.read() == read(export_file)


# --------------------------------------------------------------------------- the action and the signed link


async def test_the_signed_link_opens_without_a_login_for_five_minutes(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    export_file: Path,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    hass_admin_user: User,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    answer = await ws_download(hass, hass_ws_client)
    assert set(answer) == {"url", "expires_in"}
    assert answer["expires_in"] == 300
    url = answer["url"]
    assert url.startswith(f"{export_path(init_integration.entry_id)}?authSig=")
    client = await hass_client_no_auth()
    response = await client.get(url)
    assert response.status == HTTPStatus.OK
    assert await response.read() == read(export_file)
    freezer.tick(timedelta(seconds=290))
    assert (await client.get(url)).status == HTTPStatus.OK
    freezer.tick(timedelta(seconds=20))
    assert (await client.get(url)).status == HTTPStatus.UNAUTHORIZED
    # the integration's log says that a link was made and for whom; neither the link nor its signature is in it
    # (Home Assistant's own HTTP logs — the access log, the ban warning for the expired one — are not ours)
    assert (
        f"Made a download link for the export of {init_integration.title}, valid for 300 s, "
        f"for {hass_admin_user.name}"
    ) in caplog.text
    signature = url.split("authSig=", 1)[1]
    ours = [
        r
        for r in caplog.records
        if r.name.startswith((f"custom_components.{DOMAIN}", "jhmesh"))
    ]
    assert ours
    for record in ours:
        text = record.getMessage()
        assert signature not in text
        assert "authSig" not in text


async def test_a_link_through_the_rest_api_is_signed_for_its_token(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    export_file: Path,
    hass_client: ClientSessionGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """A script calling `POST /api/services/…` with an administrator's token gets a link signed for that token."""
    assert await async_setup_component(hass, "api", {})
    client = await hass_client()
    response = await client.post(
        f"/api/services/{DOMAIN}/download_export?return_response", json={}
    )
    assert response.status == HTTPStatus.OK
    url = (await response.json())["service_response"]["url"]
    download = await (await hass_client_no_auth()).get(url)
    assert download.status == HTTPStatus.OK
    assert await download.read() == read(export_file)


async def test_any_device_of_the_entry_names_it(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    device = dr.async_entries_for_config_entry(
        dr.async_get(hass), init_integration.entry_id
    )[0]
    answer = await ws_download(hass, hass_ws_client, device=device.id)
    assert answer["url"].startswith(f"{export_path(init_integration.entry_id)}?")
    answer = await ws_download(
        hass, hass_ws_client, config_entry_id=init_integration.entry_id
    )
    assert answer["url"].startswith(f"{export_path(init_integration.entry_id)}?")
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "download_export",
            {"device": "no-such-device"},
            blocking=True,
            return_response=True,
        )
    assert err.value.translation_key == "service_unknown_device"
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "download_export",
            {"device": device.id, "config_entry_id": init_integration.entry_id},
            blocking=True,
            return_response=True,
        )


async def test_a_call_without_a_session_to_sign_for_is_refused(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_admin_user: User,
) -> None:
    """An automation of the system has no user; an administrator's call that came through no connection or request
    (a context made up in code) has no session: a link signed for nobody would open for nobody."""
    for context in (None, Context(user_id=hass_admin_user.id)):
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(
                DOMAIN,
                "download_export",
                {},
                blocking=True,
                return_response=True,
                context=context,
            )
        assert err.value.translation_key == "download_export_no_session"


async def test_a_user_who_is_no_administrator_gets_no_link(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_read_only_user: User,
) -> None:
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "download_export",
            {},
            blocking=True,
            return_response=True,
            context=Context(user_id=hass_read_only_user.id),
        )
