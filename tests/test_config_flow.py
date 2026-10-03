"""Config flow: manual setup, Bluetooth discovery and reconfiguration, every validation error.

The three export sources (fetched from the gateway, uploaded, a path on the host) and the gateway re-fetch on
reconfigure are covered here; the REST client itself is tested in `test_gateway_api.py`. The certificate learn
step is stubbed here (`mock_learn`): the mocked HTTP session has no certificate to learn; `test_tls.py` runs the
same flows against real TLS servers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import stat
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from habluetooth.models import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    SOURCE_BLUETOOTH,
    SOURCE_USER,
    ConfigEntryState,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components import junghome_ble
from custom_components.junghome_ble import config_flow
from custom_components.junghome_ble.config_flow import (
    CONF_MESH_UUID,
    JungHomeConfigFlow,
    forget_stored_export,
    pre_reconfigure_path,
    proxy_in_range,
)
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_EXPORT_FILE,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_PASSWORD,
    CONF_GATEWAY_PIN_SOURCE,
    CONF_GATEWAY_SYNCED,
    CONF_GATEWAY_TOKEN,
    CONF_METADATA_DIR,
    CONF_SOURCE,
    CONF_UNICAST,
    DOMAIN,
    GATEWAY_DEFAULT_HOST,
    GATEWAY_DOMAIN,
    GATEWAY_USER_NAME,
    OPTION_ALLOW_PROVISIONING,
    OPTION_CLICK_DELAY,
    OPTION_HEARTBEATS,
    OPTION_PROVISIONER_IDENTITY,
    PIN_FROM_MESH,
    PIN_FROM_USER,
    STORAGE_DIR,
)
from custom_components.junghome_ble.coordinator import seq_store_for_uuid
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.client import MESH_PROXY_SERVICE
from custom_components.junghome_ble.jhmesh.crypto import NetKeyMaterial
from custom_components.junghome_ble.mesh_config import (
    app_copy_path,
    export_digest,
    gateway_sync,
)
from custom_components.junghome_ble.tls import CONF_GATEWAY_FINGERPRINT

from .conftest import (
    CDB_PATH,
    META_DIR,
    SHARE_EXPORT_PATH,
    make_node_identity_info,
    make_service_info,
    settle,
    setup_entry,
    wait_for_link,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

MESH_UUID = "1BAF3ADE-0000-4000-8000-000000000001"
OTHER_MESH_UUID = "2BEC62AA-0000-4000-8000-000000000002"
USER_INPUT = {CONF_CDB_PATH: CDB_PATH, CONF_METADATA_DIR: META_DIR, CONF_UNICAST: "d00"}
FORM_INPUT = {
    CONF_CDB_PATH: CDB_PATH,
    CONF_METADATA_DIR: META_DIR,
    CONF_UNICAST: "0D00",
}  # what the form holds
ENTRY_DATA = {
    **FORM_INPUT,
    CONF_MESH_UUID: MESH_UUID,
    CONF_SOURCE: "path",
}  # what the entry records
OTHER_NETKEY = "a1b2c3d4e5f60718293a4b5c6d7e8f90"

HOST = "192.168.1.50"
API = f"https://{HOST}/api/junghome"
TOKEN = "tok-first"
TOKEN_2 = "tok-second"
FINGERPRINT = "ab" * 32  # what the (stubbed) learn step reports for every host
OTHER_FINGERPRINT = "cd" * 32
GATEWAY_INPUT = {CONF_GATEWAY_HOST: HOST, CONF_UNICAST: "0d00"}
PASSWORD_INPUT = {**GATEWAY_INPUT, CONF_GATEWAY_PASSWORD: "netkey-pw"}
PROGRESS = (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE)
GATEWAY_DATA = {
    CONF_GATEWAY_HOST: HOST,
    CONF_GATEWAY_TOKEN: TOKEN,
    CONF_GATEWAY_FINGERPRINT: FINGERPRINT,
    CONF_GATEWAY_PIN_SOURCE: PIN_FROM_USER,  # learned at first contact: the hub checks it over the mesh
}  # what a gateway entry records beyond the export itself


@pytest.fixture(autouse=True)
def mock_learn() -> Generator[AsyncMock]:
    """Stub the certificate learn step: the mocked session presents no certificate."""
    with patch(
        "custom_components.junghome_ble.config_flow.async_learn_fingerprint",
        AsyncMock(return_value=FINGERPRINT),
    ) as mock:
        yield mock


def _mismatch(expected: str = FINGERPRINT) -> aiohttp.ServerFingerprintMismatch:
    """What aiohttp raises at the handshake when the responder's certificate is not the pinned one."""
    return aiohttp.ServerFingerprintMismatch(
        bytes.fromhex(expected), bytes.fromhex(OTHER_FINGERPRINT), HOST, 443
    )


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    with patch(
        "custom_components.junghome_ble.async_setup_entry", return_value=True
    ) as mock:
        yield mock


def _export_variant(tmp_path: Path, name: str, **fields: Any) -> tuple[str, bytes]:
    """A copy of the synthetic export with some top-level `meshNetwork` fields replaced; returns (path, network id)."""
    raw = json.loads(Path(CDB_PATH).read_text())
    raw["meshNetwork"].update(fields)
    path = tmp_path / name
    path.write_text(json.dumps(raw))
    return str(path), CDB.load(path).net_keys[0].network_id


@pytest.fixture
def refreshed_network(tmp_path: Path) -> tuple[str, bytes]:
    """The same mesh (same meshUUID) after a NetKey refresh: a new NetKey, hence a new Network ID."""
    raw = json.loads(Path(CDB_PATH).read_text())
    return _export_variant(
        tmp_path,
        "Refreshed.json",
        netKeys=[{**raw["meshNetwork"]["netKeys"][0], "key": OTHER_NETKEY}],
    )


@pytest.fixture
def other_mesh(tmp_path: Path) -> tuple[str, bytes]:
    """An export of a different mesh network: another meshUUID and another NetKey."""
    raw = json.loads(Path(CDB_PATH).read_text())
    return _export_variant(
        tmp_path,
        "OtherMesh.json",
        meshUUID=OTHER_MESH_UUID,
        netKeys=[{**raw["meshNetwork"]["netKeys"][0], "key": OTHER_NETKEY}],
    )


def _entry(recorded_uuid: str | None, cdb_path: str = CDB_PATH) -> MockConfigEntry:
    """An entry as created by the current flow (mesh UUID recorded) or by an older version (not recorded)."""
    data = {CONF_CDB_PATH: cdb_path, CONF_METADATA_DIR: META_DIR, CONF_UNICAST: "0D00"}
    if recorded_uuid is not None:
        data[CONF_MESH_UUID] = recorded_uuid
    return MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data=data,
    )


def _gateway_entry(
    hass: HomeAssistant,
    token: str = TOKEN,
    fingerprint: str | None = FINGERPRINT,
    source: str = "gateway",
    pin_source: str = PIN_FROM_USER,
) -> MockConfigEntry:
    """An entry whose export was fetched from the gateway (or uploaded): a copy of the fixture in our store."""
    stored = _stored(hass)
    stored.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(CDB_PATH, stored)
    data = {
        CONF_CDB_PATH: str(stored),
        CONF_METADATA_DIR: "",
        CONF_UNICAST: "0D00",
        CONF_MESH_UUID: MESH_UUID,
        CONF_SOURCE: source,
        CONF_GATEWAY_HOST: HOST,
        CONF_GATEWAY_TOKEN: token,
    }
    if fingerprint is not None:
        data[CONF_GATEWAY_FINGERPRINT] = fingerprint
        data[CONF_GATEWAY_PIN_SOURCE] = pin_source
    return MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data=data,
    )


async def _choose(hass: HomeAssistant, result: Any, option: str) -> Any:
    """Pick `option` on the menu the flow is showing."""
    assert result["type"] is FlowResultType.MENU, result
    assert option in result["menu_options"]
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": option}
    )


async def _start_user_flow(hass: HomeAssistant) -> str:
    """Manual setup up to the "path on the host" form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    assert result["menu_options"] == ["gateway", "upload", "path"]
    result = await _choose(hass, result, "path")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "path"
    assert result["errors"] == {}
    return result["flow_id"]


async def _start_gateway_flow(hass: HomeAssistant) -> Any:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _choose(hass, result, "gateway")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway"
    assert result["errors"] == {}
    return result


async def _advance_progress(hass: HomeAssistant, result: Any) -> Any:
    """Drive the flow through its waiting-for-approval progress step."""
    for _ in range(10):  # a stuck flow fails instead of hanging
        if result["type"] not in PROGRESS:
            break
        if result["type"] is FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result


def _stored(hass: HomeAssistant, mesh_uuid: str = MESH_UUID) -> Path:
    return Path(hass.config.path(STORAGE_DIR, f"{mesh_uuid}.json"))


def _incoming_files(hass: HomeAssistant) -> list[Path]:
    store = Path(hass.config.path(STORAGE_DIR))
    return sorted(store.glob(".incoming-*")) if store.is_dir() else []


def _read_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def _share_export() -> dict[str, Any]:
    return _read_json(SHARE_EXPORT_PATH)


def _bare_cdb() -> dict[str, Any]:
    return json.loads(Path(CDB_PATH).read_text())["meshNetwork"]


def _sequence(*responses: dict[str, Any]) -> Any:
    """A side effect answering one registered URL differently on successive requests."""
    queue = list(responses)

    async def side_effect(method: str, url: Any, data: Any) -> Any:
        kwargs = queue.pop(0) if len(queue) > 1 else queue[0]
        return AiohttpClientMockResponse(method=method, url=url, **kwargs)

    return side_effect


def _mock_gateway(
    aioclient_mock: AiohttpClientMocker, export: dict[str, Any] | None = None
) -> None:
    """A gateway that answers the probe, both registrations and the project fetch."""
    aioclient_mock.get(f"{API}/version/", json={"api_version": "1.5.0"})
    aioclient_mock.post(f"{API}/register", json={"token": TOKEN})
    aioclient_mock.post(f"{API}/register/by-password", json={"token": TOKEN})
    aioclient_mock.get(
        f"{API}/project/junghome",
        json=export if export is not None else _share_export(),
    )


def _calls(aioclient_mock: AiohttpClientMocker, path: str) -> list[Any]:
    return [c for c in aioclient_mock.mock_calls if str(c[1]).endswith(path)]


# --------------------------------------------------------------------------- user flow (path on the host)


async def test_user_flow(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    network_id: bytes,
) -> None:
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "JUNG HOME mesh 1BAF3ADE"
    assert (
        result["data"] == ENTRY_DATA
    )  # the address is normalised to 4 upper-case hex digits
    assert result["result"].unique_id == network_id.hex() == "1fbd2c61a4b6e5a4"
    assert len(mock_setup_entry.mock_calls) == 1
    assert _incoming_files(hass) == []  # nothing copied for a path on the host
    assert not _stored(hass).exists()


async def test_user_flow_share_export(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    network_id: bytes,
) -> None:
    """The app's `JungHome.json` (share via file) is accepted as well; it needs no metadata directory."""
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_CDB_PATH: SHARE_EXPORT_PATH, CONF_UNICAST: "0D00"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_CDB_PATH: SHARE_EXPORT_PATH,
        CONF_METADATA_DIR: "",
        CONF_UNICAST: "0D00",
        CONF_MESH_UUID: MESH_UUID,
        CONF_SOURCE: "path",
    }
    assert result["result"].unique_id == network_id.hex()


def _write(path: Path, text: str) -> str:
    path.write_text(text)
    return str(path)


def _malformed(tmp: Path, name: str, mutate: Any) -> str:
    """The synthetic export with one field of `meshNetwork` replaced (a shape the loader must refuse)."""
    raw = json.loads(Path(CDB_PATH).read_text())
    mutate(raw["meshNetwork"])
    return _write(tmp / name, json.dumps(raw))


@pytest.mark.parametrize(
    ("make_path", "error"),
    [
        (lambda tmp: str(tmp / "missing.json"), "cannot_load"),
        (lambda tmp: _write(tmp / "garbage.json", "not json"), "cannot_load"),
        (lambda tmp: _write(tmp / "empty.json", "{}"), "invalid_export"),
        (
            lambda tmp: _write(
                tmp / "no_nodes.json", '{"meshNetwork": {"meshUUID": "x"}}'
            ),
            "invalid_export",
        ),
        (
            lambda tmp: _malformed(
                tmp,
                "int_address.json",
                lambda n: n["nodes"][0].update(unicastAddress=5),
            ),
            "invalid_export",
        ),
        (
            lambda tmp: _malformed(
                tmp, "no_netkey.json", lambda n: n.update(netKeys=[])
            ),
            "invalid_export",
        ),
        (
            lambda tmp: _malformed(
                tmp, "provisioners.json", lambda n: n.update(provisioners="x")
            ),
            "invalid_export",
        ),
        (
            lambda tmp: _malformed(
                tmp, "uuid.json", lambda n: n.update(meshUUID="../../../etc/passwd")
            ),
            "invalid_export",
        ),
    ],
    ids=[
        "missing",
        "invalid_json",
        "not_an_export",
        "incomplete",
        "address_not_a_string",
        "no_netkey_0",
        "provisioners_not_a_list",
        "mesh_uuid_not_a_uuid",
    ],
)
async def test_user_flow_cannot_load(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    tmp_path: Path,
    make_path: Any,
    error: str,
) -> None:
    """An unreadable file is `cannot_load`; a JSON document that is not a usable export is `invalid_export`."""
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {**USER_INPUT, CONF_CDB_PATH: make_path(tmp_path)}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    # the form keeps what was typed
    assert result["data_schema"]({})[CONF_CDB_PATH] == make_path(tmp_path)


async def test_user_flow_metadata_not_a_directory(
    hass: HomeAssistant, mock_bluetooth_env: dict[str, Any], mock_setup_entry: AsyncMock
) -> None:
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {**USER_INPUT, CONF_METADATA_DIR: CDB_PATH}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_METADATA_DIR: "not_a_directory"}


async def test_user_flow_metadata_malformed(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A metadata directory whose files are not the app's is refused by the flow, not by the first setup."""
    bad = tmp_path / "scene_metadata.json"
    bad.write_text("[1, 2, 3]")  # the app writes key/value pairs
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {**USER_INPUT, CONF_METADATA_DIR: str(tmp_path)}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_METADATA_DIR: "invalid_metadata"}
    assert f"Metadata not usable: {bad}" in caplog.text
    assert mock_setup_entry.mock_calls == []


def test_proxy_in_range_ignores_other_adverts(
    hass: HomeAssistant, cdb: CDB, mock_bluetooth_env: dict[str, Any]
) -> None:
    """Only our Network ID or a Node Identity of one of our nodes counts: other service data, another mesh's
    Network ID and a Node Identity hash no node of ours produces do not."""
    other = make_node_identity_info(cdb, 0x0148)
    foreign_hash = BluetoothServiceInfoBleak.from_device_and_advertisement_data(
        BLEDevice("AA:BB:CC:DD:EE:01", None, {}),
        AdvertisementData(
            local_name=None,
            manufacturer_data={},
            service_data={MESH_PROXY_SERVICE: b"\x01" + bytes(16)},
            service_uuids=[MESH_PROXY_SERVICE],
            tx_power=None,
            rssi=-50,
            platform_data=(),
        ),
        "local",
        0.0,
        True,
    )
    no_mesh = BluetoothServiceInfoBleak.from_device_and_advertisement_data(
        BLEDevice("AA:BB:CC:DD:EE:02", None, {}),
        AdvertisementData(
            local_name=None,
            manufacturer_data={},
            service_data={"0000180f-0000-1000-8000-00805f9b34fb": b"\x64"},
            service_uuids=[],
            tx_power=None,
            rssi=-50,
            platform_data=(),
        ),
        "local",
        0.0,
        True,
    )
    mock_bluetooth_env["infos"] = [
        no_mesh,
        make_service_info(bytes(8), address="AA:BB:CC:DD:EE:03"),
        foreign_hash,
    ]
    assert not proxy_in_range(hass, cdb)
    mock_bluetooth_env["infos"].append(other)
    assert proxy_in_range(hass, cdb)


def test_proxy_in_range_accepts_either_key_of_an_export_written_mid_key_refresh(
    hass: HomeAssistant, cdb: CDB, mock_bluetooth_env: dict[str, Any]
) -> None:
    """Phase 1: the export's `key` is the new NetKey, and the proxies still advertise under its `oldKey`."""
    raw = _bare_cdb()
    raw["netKeys"][0].update(key=OTHER_NETKEY, oldKey=raw["netKeys"][0]["key"], phase=1)
    refreshing = CDB.from_network(raw)
    mock_bluetooth_env["infos"] = [make_service_info(cdb.net_keys[0].network_id)]
    assert proxy_in_range(hass, refreshing)
    mock_bluetooth_env["infos"] = [make_node_identity_info(cdb, 0x0148)]
    assert proxy_in_range(hass, refreshing)


async def test_user_flow_accepts_a_proxy_advertising_node_identity(
    hass: HomeAssistant,
    cdb: CDB,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
) -> None:
    """A Node Identity advertisement of one of the export's nodes proves a proxy is in range as well."""
    mock_bluetooth_env["infos"] = [make_node_identity_info(cdb, 0x0232)]
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("unicast", ["zz", "0000", "8000", "", "0x"])
async def test_user_flow_invalid_address(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    unicast: str,
) -> None:
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {**USER_INPUT, CONF_UNICAST: unicast}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_UNICAST: "invalid_address"}


@pytest.mark.parametrize(
    "unicast",
    ["0149", "0500", "0ccc", "0D05"],
    ids=["element", "in_phone_range", "range_end", "excluded"],
)
async def test_user_flow_address_in_use(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    tmp_path: Path,
    unicast: str,
) -> None:
    """Element addresses, the phone's allocated unicast range (0001-0CCC) and excluded addresses are all taken."""
    path, _ = _export_variant(
        tmp_path,
        "Exclusions.json",
        networkExclusions=[{"ivIndex": 0, "addresses": ["0002", "0D05"]}],
    )
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {**USER_INPUT, CONF_CDB_PATH: path, CONF_UNICAST: unicast}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_UNICAST: "address_in_use"}

    # the first address above the phone's range is fine (so is the default, 0D00: test_user_flow)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {**USER_INPUT, CONF_CDB_PATH: path, CONF_UNICAST: "0CCD"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_UNICAST] == "0CCD"


async def test_user_flow_no_proxy_visible(
    hass: HomeAssistant, mock_bluetooth_env: dict[str, Any], mock_setup_entry: AsyncMock
) -> None:
    other = make_service_info(b"\x11" * 8)  # a proxy of some other mesh
    identity = make_service_info(b"\x33" * 8, address="11:22:33:44:55:66")
    identity.service_data = {
        next(iter(identity.service_data)): b"\x01" + b"\x33" * 16
    }  # Node Identity: no Network ID in it
    mock_bluetooth_env["infos"] = [other, identity]
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_proxy_visible"}

    # the errors clear once a node of our network shows up
    mock_bluetooth_env["infos"].append(
        make_service_info(CDB.load(Path(CDB_PATH)).net_keys[0].network_id)
    )
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_metadata_optional(
    hass: HomeAssistant, mock_bluetooth_env: dict[str, Any], mock_setup_entry: AsyncMock
) -> None:
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_CDB_PATH: CDB_PATH, CONF_UNICAST: "0D00"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {**ENTRY_DATA, CONF_METADATA_DIR: ""}


async def test_user_flow_already_configured(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_refuses_a_second_entry_for_an_already_configured_mesh(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
    refreshed_network: tuple[str, bytes],
) -> None:
    """A NetKey refresh gives an already-configured mesh a *new* Network ID, so the unique-id de-duplication above
    cannot catch a second "Add integration" of its (freshly fetched/uploaded) export — only its unchanged mesh
    UUID can. Two entries sharing one mesh would also share its sequence-number store (`coordinator.seq_store`,
    keyed on the mesh UUID) without coordinating their writes to it (each holds its own stale copy of the other
    addresses' records and can roll the other's counter backwards on its next load) — refuse instead and point at
    the existing entry's Reconfigure, exactly as it is documented."""
    mock_config_entry.add_to_hass(hass)
    new_path, new_id = refreshed_network
    mock_bluetooth_env["infos"] = [make_service_info(new_id)]
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {**FORM_INPUT, CONF_CDB_PATH: new_path}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "path"
    assert result["errors"] == {"base": "mesh_already_configured"}
    assert [e.entry_id for e in hass.config_entries.async_entries(DOMAIN)] == [
        mock_config_entry.entry_id
    ]
    assert mock_setup_entry.mock_calls == []


# --------------------------------------------------------------------------- gateway


async def test_gateway_flow_password(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
    network_id: bytes,
    mock_learn: AsyncMock,
) -> None:
    """With the network-key password the token comes at once; the export is fetched, stored and validated."""
    _mock_gateway(aioclient_mock)
    result = await _start_gateway_flow(hass)
    assert result["data_schema"]({})[CONF_GATEWAY_HOST] == GATEWAY_DEFAULT_HOST
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], PASSWORD_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    stored = _stored(hass)
    assert result["data"] == {
        CONF_SOURCE: "gateway",
        CONF_CDB_PATH: str(stored),
        CONF_METADATA_DIR: "",
        CONF_UNICAST: "0D00",
        CONF_MESH_UUID: MESH_UUID,
        CONF_GATEWAY_SYNCED: export_digest(_share_export()),
        **GATEWAY_DATA,
    }
    assert result["result"].unique_id == network_id.hex()
    # the export is on disk, private, verbatim, and no temporary file is left behind
    assert json.loads(stored.read_text()) == _share_export()
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert _incoming_files(hass) == []
    # the password went to by-password only; the fetch carried the token; no app approval was requested
    assert _calls(aioclient_mock, "/register/by-password")[0][2] == {
        "password": "netkey-pw"
    }
    assert _calls(aioclient_mock, "/project/junghome")[0][3] == {"token": TOKEN}
    assert _calls(aioclient_mock, "/register") == []
    assert len(mock_setup_entry.mock_calls) == 1
    # the certificate was learned once, before the first request, for the host as typed
    mock_learn.assert_awaited_once()
    assert mock_learn.call_args[0][1] == HOST


async def test_gateway_flow_app_approval(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Without a password the flow waits (progress step) until the request is approved in the app."""
    approved = asyncio.Event()

    async def wait_for_approval(method: str, url: Any, data: Any) -> Any:
        assert data == {"user_name": GATEWAY_USER_NAME}
        await approved.wait()
        return AiohttpClientMockResponse(method=method, url=url, json={"token": TOKEN})

    aioclient_mock.get(f"{API}/version/", json={"api_version": "1.5.0"})
    aioclient_mock.post(f"{API}/register", side_effect=wait_for_approval)
    aioclient_mock.get(f"{API}/project/junghome", json=_share_export())

    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**GATEWAY_INPUT, CONF_GATEWAY_HOST: f"https://{HOST}/"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "gateway_register"
    assert result["progress_action"] == "waiting_for_approval"
    assert result["description_placeholders"] == {
        "host": HOST,
        "user_name": GATEWAY_USER_NAME,
    }

    approved.set()
    result = await _advance_progress(hass, result)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GATEWAY_TOKEN] == TOKEN
    assert result["data"][CONF_GATEWAY_HOST] == HOST  # scheme and slash stripped
    assert _stored(hass).is_file()


async def test_gateway_flow_not_approved_then_retry(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The gateway gives up after 180 s (HTTP 400): back to the form with the reason, and a retry works."""
    _mock_gateway(aioclient_mock)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/version/", json={"api_version": "1.5.0"})
    aioclient_mock.post(
        f"{API}/register",
        side_effect=_sequence(
            {"status": 400, "json": {"error": "Error during register."}},
            {"json": {"token": TOKEN}},
        ),
    )
    aioclient_mock.get(f"{API}/project/junghome", json=_share_export())

    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], GATEWAY_INPUT
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    result = await _advance_progress(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway"
    assert result["errors"] == {"base": "not_approved"}
    assert result["data_schema"]({})[CONF_GATEWAY_HOST] == HOST  # kept

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], GATEWAY_INPUT
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    result = await _advance_progress(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(_calls(aioclient_mock, "/register")) == 2


async def test_gateway_flow_fetch_fails_after_approval(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Approved, but the gateway has nothing to hand out: the gateway form comes back with the reason."""
    aioclient_mock.get(f"{API}/version/", json={"api_version": "1.5.0"})
    aioclient_mock.post(f"{API}/register", json={"token": TOKEN})
    aioclient_mock.get(f"{API}/project/junghome", status=404)
    aioclient_mock.get(f"{API}/project/cdb", status=404)
    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], GATEWAY_INPUT
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    result = await _advance_progress(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway"
    assert result["errors"] == {"base": "no_project"}
    assert result["data_schema"]({}) == {CONF_GATEWAY_HOST: HOST, CONF_UNICAST: "0D00"}


@pytest.mark.parametrize(
    ("mocks", "user_input", "errors"),
    [
        (
            [("get", "/version/", {"exc": TimeoutError()})],
            PASSWORD_INPUT,
            {"base": "cannot_connect"},
        ),
        ("learn_fails", PASSWORD_INPUT, {"base": "cannot_connect"}),
        (
            [
                ("get", "/version/", {"status": 404}),  # answers HTTP: reachable
                ("post", "/register/by-password", {"status": 401}),
            ],
            PASSWORD_INPUT,
            {CONF_GATEWAY_PASSWORD: "invalid_auth"},
        ),
        (
            [("post", "/register/by-password", {"status": 500})],
            PASSWORD_INPUT,
            {"base": "gateway_error"},
        ),
        (
            [
                ("get", "/project/junghome", {"status": 404}),
                ("get", "/project/cdb", {"status": 404}),
            ],
            PASSWORD_INPUT,
            {"base": "no_project"},
        ),
        (
            [("get", "/project/junghome", {"status": 401})],
            PASSWORD_INPUT,
            {"base": "token_rejected"},
        ),
        (
            [],
            {**PASSWORD_INPUT, CONF_GATEWAY_HOST: " "},
            {CONF_GATEWAY_HOST: "invalid_host"},
        ),
        (
            [],
            {**PASSWORD_INPUT, CONF_UNICAST: "9000"},
            {CONF_UNICAST: "invalid_address"},
        ),
        (
            [],
            {**PASSWORD_INPUT, CONF_UNICAST: "0148"},  # an element of the fetched mesh
            {CONF_UNICAST: "address_in_use"},
        ),
    ],
    ids=[
        "unreachable",
        "learn_fails",
        "wrong_password",
        "gateway_error",
        "no_project",
        "token_rejected",
        "invalid_host",
        "invalid_address",
        "address_in_use",
    ],
)
async def test_gateway_flow_errors(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
    mock_learn: AsyncMock,
    mocks: Any,
    user_input: dict[str, Any],
    errors: dict[str, str],
) -> None:
    if mocks == "learn_fails":
        mock_learn.side_effect = aiohttp.ClientConnectionError("refused")
        mocks = []
    for (
        method,
        path,
        kwargs,
    ) in mocks:  # the failing answers first: the first match wins
        getattr(aioclient_mock, method)(f"{API}{path}", **kwargs)
    _mock_gateway(aioclient_mock)

    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway"
    assert result["errors"] == errors
    assert _incoming_files(hass) == []
    assert not _stored(hass).exists()
    assert len(mock_setup_entry.mock_calls) == 0


async def test_gateway_flow_token_rejected_registers_anew(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A token the gateway rejects on the fetch is dropped: the next submit obtains a fresh one."""
    aioclient_mock.get(f"{API}/version/", json={"api_version": "1.5.0"})
    aioclient_mock.post(
        f"{API}/register/by-password",
        side_effect=_sequence({"json": {"token": TOKEN}}, {"json": {"token": TOKEN_2}}),
    )
    aioclient_mock.get(
        f"{API}/project/junghome",
        side_effect=_sequence({"status": 401}, {"json": _share_export()}),
    )
    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], PASSWORD_INPUT
    )
    assert result["errors"] == {"base": "token_rejected"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], PASSWORD_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GATEWAY_TOKEN] == TOKEN_2
    assert _calls(aioclient_mock, "/project/junghome")[-1][3] == {"token": TOKEN_2}


async def test_gateway_flow_cdb_fallback(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Firmware without `/project/junghome`: the bare CDB from `/project/cdb` is stored in the iOS shape."""
    aioclient_mock.get(f"{API}/version/", json={"api_version": "1.1.0"})
    aioclient_mock.post(f"{API}/register/by-password", json={"token": TOKEN})
    aioclient_mock.get(f"{API}/project/junghome", status=404)
    aioclient_mock.get(f"{API}/project/cdb", json=_bare_cdb())

    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], PASSWORD_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert json.loads(_stored(hass).read_text()) == {"meshNetwork": _bare_cdb()}
    assert result["data"][CONF_MESH_UUID] == MESH_UUID


async def test_gateway_flow_no_proxy_visible_keeps_token(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
    service_info: Any,
) -> None:
    """A fetched export that fails validation is deleted again; the token survives, so the retry needs no
    second registration."""
    _mock_gateway(aioclient_mock)
    mock_bluetooth_env["infos"] = []
    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], PASSWORD_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_proxy_visible"}
    assert not _stored(hass).exists()
    assert _incoming_files(hass) == []

    mock_bluetooth_env["infos"] = [service_info]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], GATEWAY_INPUT
    )  # no password this time: the token from the first round is reused
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(_calls(aioclient_mock, "/register/by-password")) == 1
    assert _calls(aioclient_mock, "/register") == []
    assert len(_calls(aioclient_mock, "/project/junghome")) == 2


async def test_gateway_flow_cannot_store(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    _mock_gateway(aioclient_mock)
    result = await _start_gateway_flow(hass)
    with patch(
        "custom_components.junghome_ble.config_flow._write_private",
        side_effect=OSError("read-only"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], PASSWORD_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_store"}


async def test_gateway_flow_from_bluetooth_network_mismatch(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The discovered proxy belongs to another mesh than the export the gateway serves."""
    _mock_gateway(aioclient_mock)
    other = make_service_info(b"\x22" * 8, address="11:22:33:44:55:66")
    mock_bluetooth_env["infos"].append(other)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=other
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _choose(hass, result, "gateway")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], PASSWORD_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway"
    assert result["errors"] == {"base": "network_mismatch"}
    assert not _stored(hass).exists()
    assert _incoming_files(hass) == []


async def test_gateway_flow_already_configured(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    _mock_gateway(aioclient_mock)
    mock_config_entry.add_to_hass(hass)
    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], PASSWORD_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert not _stored(hass).exists()
    assert _incoming_files(hass) == []


# --------------------------------------------------------------------------- the certificate pin


async def test_gateway_flow_certificate_changed_during_approval(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The access request meets another certificate than the one just learned: the confirm step follows the
    progress step, and confirming asks for access again with the new pin; no repair issue for a new entry."""
    aioclient_mock.get(f"{API}/version/", json={"api_version": "1.5.0"})
    aioclient_mock.post(
        f"{API}/register",
        side_effect=_sequence({"exc": _mismatch()}, {"json": {"token": TOKEN}}),
    )
    aioclient_mock.get(f"{API}/project/junghome", json=_share_export())
    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], GATEWAY_INPUT
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    result = await _advance_progress(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway_certificate"
    assert result["description_placeholders"] == {
        "host": HOST,
        "observed": "CD:" * 31 + "CD",
        "expected": "AB:" * 31 + "AB",
    }
    assert not [
        i for i in ir.async_get(hass).issues if i[0] == DOMAIN
    ]  # nothing to warn about: no entry yet

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert (
        result["type"] is FlowResultType.SHOW_PROGRESS
    )  # a new access request, pinned anew
    result = await _advance_progress(hass, result)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GATEWAY_FINGERPRINT] == OTHER_FINGERPRINT
    assert len(_calls(aioclient_mock, "/register")) == 2


async def test_gateway_flow_certificate_changed_at_the_fetch_after_approval(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Approved, then the fetch is refused at the handshake: confirm, and the fetch (only) is repeated."""
    aioclient_mock.get(f"{API}/version/", json={"api_version": "1.5.0"})
    aioclient_mock.post(f"{API}/register", json={"token": TOKEN})
    aioclient_mock.get(
        f"{API}/project/junghome",
        side_effect=_sequence({"exc": _mismatch()}, {"json": _share_export()}),
    )
    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], GATEWAY_INPUT
    )
    result = await _advance_progress(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway_certificate"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GATEWAY_FINGERPRINT] == OTHER_FINGERPRINT
    assert result["data"][CONF_GATEWAY_TOKEN] == TOKEN
    assert len(_calls(aioclient_mock, "/register")) == 1
    assert len(_calls(aioclient_mock, "/project/junghome")) == 2


async def test_gateway_flow_certificate_changed_at_the_password_probe(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The probe before the password is refused: the form's input is replayed after the confirmation."""
    aioclient_mock.get(
        f"{API}/version/",
        side_effect=_sequence({"exc": _mismatch()}, {"json": {"api_version": "1"}}),
    )
    aioclient_mock.post(f"{API}/register/by-password", json={"token": TOKEN})
    aioclient_mock.get(f"{API}/project/junghome", json=_share_export())
    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], PASSWORD_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway_certificate"
    assert (
        _calls(aioclient_mock, "/register/by-password") == []
    )  # the password stayed home
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GATEWAY_FINGERPRINT] == OTHER_FINGERPRINT
    assert len(_calls(aioclient_mock, "/register/by-password")) == 1


async def test_gateway_flow_certificate_changed_at_the_password_registration(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The certificate changes between the probe and the password request: the password stays home."""
    aioclient_mock.get(f"{API}/version/", json={"api_version": "1.5.0"})
    aioclient_mock.post(
        f"{API}/register/by-password",
        side_effect=_sequence({"exc": _mismatch()}, {"json": {"token": TOKEN}}),
    )
    aioclient_mock.get(f"{API}/project/junghome", json=_share_export())
    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], PASSWORD_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway_certificate"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GATEWAY_FINGERPRINT] == OTHER_FINGERPRINT


def _raise_gateway_issues(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    """The runtime's certificate and token repairs of `entry` (the hub and the configurator raise them)."""
    ids = [
        f"gateway_certificate_changed_{entry.entry_id}",
        f"gateway_token_rejected_{entry.entry_id}",
    ]
    for issue_id in ids:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue_id.rsplit("_", 1)[0],
            translation_placeholders={"host": HOST, "title": entry.title},
        )
    return ids


async def test_reconfigure_refetch_certificate_changed_clears_the_repairs(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The repairs the runtime raised point here: the flow raises none of its own (an abandoned flow leaves
    nothing behind), and a finished one clears them. A certificate the user confirms is recorded as theirs, which
    the hub checks over the mesh before using it."""
    aioclient_mock.get(
        f"{API}/project/junghome",
        side_effect=_sequence(
            {"exc": _mismatch()},
            {"exc": _mismatch()},
            {"json": _share_export()},
        ),
    )
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    abandoned = await _start_reconfigure(hass, entry, "gateway_refetch")
    abandoned = await hass.config_entries.flow.async_configure(
        abandoned["flow_id"], {CONF_UNICAST: "0D00"}
    )
    assert abandoned["step_id"] == "gateway_certificate"
    hass.config_entries.flow.async_abort(abandoned["flow_id"])
    assert not [i for (d, i) in ir.async_get(hass).issues if d == DOMAIN]

    issues = _raise_gateway_issues(hass, entry)
    result = await _start_reconfigure(hass, entry, "gateway_refetch")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UNICAST: "0D00"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway_certificate"
    assert entry.data[CONF_GATEWAY_FINGERPRINT] == FINGERPRINT  # untouched so far

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_GATEWAY_FINGERPRINT] == OTHER_FINGERPRINT
    assert entry.data[CONF_GATEWAY_PIN_SOURCE] == PIN_FROM_USER
    for issue_id in issues:
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_reconfigure_refuses_to_override_a_pin_the_mesh_vouched_for(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The entry's pin was confirmed by the gateway node over the mesh: a responder with another certificate is
    not the gateway, so there is no one-click override — the flow ends, nothing was sent, the pin stays."""
    aioclient_mock.get(f"{API}/project/junghome", exc=_mismatch())
    entry = _gateway_entry(hass, pin_source=PIN_FROM_MESH)
    entry.add_to_hass(hass)
    result = await _start_reconfigure(hass, entry, "gateway_refetch")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UNICAST: "0D00"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "certificate_vouched_by_mesh"
    assert result["description_placeholders"]["host"] == HOST
    assert entry.data[CONF_GATEWAY_FINGERPRINT] == FINGERPRINT
    assert entry.data[CONF_GATEWAY_PIN_SOURCE] == PIN_FROM_MESH


async def test_reconfigure_refetch_with_a_corrupt_pin_learns_anew(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
    mock_learn: AsyncMock,
) -> None:
    """A hand-edited fingerprint in the entry reads as "no pin": trust on first use again, then recorded."""
    aioclient_mock.get(f"{API}/project/junghome", json=_share_export())
    entry = _gateway_entry(hass, fingerprint="not a digest")
    entry.add_to_hass(hass)
    result = await _start_reconfigure(hass, entry, "gateway_refetch")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UNICAST: "0D00"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert entry.data[CONF_GATEWAY_FINGERPRINT] == FINGERPRINT
    mock_learn.assert_awaited_once()


# --------------------------------------------------------------------------- reauth (a token the gateway rejects)


async def _start_reauth(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    """The reauth flow `MeshConfigurator.report_token_rejected` starts, up to its form."""
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {}
    return result


async def test_reauth_by_password_renews_the_token_only(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
    mock_learn: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The password brings a new token at once, pinned to the entry's certificate: the token alone changes, the
    token repair goes, nothing is fetched and the entry is not set up again. Neither secret is logged."""
    caplog.set_level(logging.DEBUG)
    aioclient_mock.get(f"{API}/version/", json={"api_version": "1.5.0"})
    aioclient_mock.post(f"{API}/register/by-password", json={"token": TOKEN_2})
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    issues = _raise_gateway_issues(hass, entry)
    before = dict(entry.data)

    result = await _start_reauth(hass, entry)
    assert result["description_placeholders"] == {
        "host": HOST,
        "title": entry.title,
        "user_name": GATEWAY_USER_NAME,
        "name": entry.title,  # Home Assistant's own, from the reauth context
    }
    assert set(result["data_schema"]({})) == set()  # the password is optional
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GATEWAY_PASSWORD: "netkey-pw"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {**before, CONF_GATEWAY_TOKEN: TOKEN_2}
    assert _calls(aioclient_mock, "/register/by-password")[0][2] == {
        "password": "netkey-pw"
    }
    assert _calls(aioclient_mock, "/project/junghome") == []
    assert _calls(aioclient_mock, "/register") == []
    mock_learn.assert_not_awaited()  # the entry's pin
    assert len(mock_setup_entry.mock_calls) == 0
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, issues[1]) is None  # the token repair
    assert registry.async_get_issue(DOMAIN, issues[0])  # the pin did not change
    assert "netkey-pw" not in caplog.text
    assert TOKEN_2 not in caplog.text


async def test_reauth_wrong_password(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected password is an error on its field; the entry keeps its token and the repair stays."""
    caplog.set_level(logging.DEBUG)
    aioclient_mock.get(f"{API}/version/", json={"api_version": "1.5.0"})
    aioclient_mock.post(f"{API}/register/by-password", status=401)
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    issues = _raise_gateway_issues(hass, entry)
    result = await _start_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GATEWAY_PASSWORD: "wrong-pw"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {CONF_GATEWAY_PASSWORD: "invalid_auth"}
    assert entry.data[CONF_GATEWAY_TOKEN] == TOKEN
    assert ir.async_get(hass).async_get_issue(DOMAIN, issues[1])
    assert "wrong-pw" not in caplog.text


async def test_reauth_by_approval_in_the_app(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Without a password the flow waits for the access request to be approved in the app, then stores the token."""
    approved = asyncio.Event()

    async def wait_for_approval(method: str, url: Any, data: Any) -> Any:
        assert data == {"user_name": GATEWAY_USER_NAME}
        await approved.wait()
        return AiohttpClientMockResponse(
            method=method, url=url, json={"token": TOKEN_2}
        )

    aioclient_mock.get(f"{API}/version/", json={"api_version": "1.5.0"})
    aioclient_mock.post(f"{API}/register", side_effect=wait_for_approval)
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    issues = _raise_gateway_issues(hass, entry)
    result = await _start_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "gateway_register"
    assert result["description_placeholders"] == {
        "host": HOST,
        "user_name": GATEWAY_USER_NAME,
    }
    approved.set()
    result = await _advance_progress(hass, result)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_GATEWAY_TOKEN] == TOKEN_2
    assert ir.async_get(hass).async_get_issue(DOMAIN, issues[1]) is None
    assert _calls(aioclient_mock, "/project/junghome") == []


async def test_reauth_approval_times_out(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The gateway gives up on the request (HTTP 400 after its three minutes): back to the reauth form, which
    says so; submitting again asks anew."""
    aioclient_mock.get(f"{API}/version/", json={"api_version": "1.5.0"})
    aioclient_mock.post(
        f"{API}/register",
        side_effect=_sequence({"status": 400}, {"json": {"token": TOKEN_2}}),
    )
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    result = await _start_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _advance_progress(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "not_approved"}
    assert entry.data[CONF_GATEWAY_TOKEN] == TOKEN

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _advance_progress(hass, result)
    await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_GATEWAY_TOKEN] == TOKEN_2
    assert len(_calls(aioclient_mock, "/register")) == 2


async def test_reauth_unreachable_gateway(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The probe gets no answer: the form says so before any password or access request goes out."""
    aioclient_mock.get(f"{API}/version/", exc=aiohttp.ClientConnectionError("down"))
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    result = await _start_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GATEWAY_PASSWORD: "netkey-pw"}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    assert _calls(aioclient_mock, "/register/by-password") == []


async def test_reauth_certificate_changed(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The gateway presents another certificate than the entry's pin: nothing is sent until the user vouches for
    it; then the password goes out under the new pin, which the entry records as the user's (the hub checks it
    over the mesh before using it), and the certificate repair goes with the token repair."""
    aioclient_mock.get(
        f"{API}/version/",
        side_effect=_sequence({"exc": _mismatch()}, {"json": {"api_version": "1.5.0"}}),
    )
    aioclient_mock.post(f"{API}/register/by-password", json={"token": TOKEN_2})
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    issues = _raise_gateway_issues(hass, entry)
    result = await _start_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GATEWAY_PASSWORD: "netkey-pw"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway_certificate"
    assert result["description_placeholders"]["observed"] == "CD:" * 31 + "CD"
    assert _calls(aioclient_mock, "/register/by-password") == []

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_GATEWAY_TOKEN] == TOKEN_2
    assert entry.data[CONF_GATEWAY_FINGERPRINT] == OTHER_FINGERPRINT
    assert entry.data[CONF_GATEWAY_PIN_SOURCE] == PIN_FROM_USER
    for issue in issues:
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue) is None


async def test_reauth_certificate_changed_during_approval(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The access request meets another certificate: the certificate step follows the progress step, and
    confirming asks for access again under the new pin."""
    aioclient_mock.get(f"{API}/version/", json={"api_version": "1.5.0"})
    aioclient_mock.post(
        f"{API}/register",
        side_effect=_sequence({"exc": _mismatch()}, {"json": {"token": TOKEN_2}}),
    )
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    result = await _start_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _advance_progress(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway_certificate"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    result = await _advance_progress(hass, result)
    await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_GATEWAY_TOKEN] == TOKEN_2
    assert entry.data[CONF_GATEWAY_FINGERPRINT] == OTHER_FINGERPRINT
    assert len(_calls(aioclient_mock, "/register")) == 2


async def test_reauth_refuses_a_certificate_the_mesh_contradicts(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The entry's pin was vouched for by the gateway node: a responder with another certificate is not the
    gateway, so the reauth ends without sending the password, and the entry is untouched."""
    aioclient_mock.get(f"{API}/version/", exc=_mismatch())
    entry = _gateway_entry(hass, pin_source=PIN_FROM_MESH)
    entry.add_to_hass(hass)
    before = dict(entry.data)
    result = await _start_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GATEWAY_PASSWORD: "netkey-pw"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "certificate_vouched_by_mesh"
    assert entry.data == before
    assert _calls(aioclient_mock, "/register/by-password") == []


async def test_reconfigure_ends_a_pending_reauth(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A reconfigure that finishes leaves the entry with a token the gateway took: the reauth waiting for the
    user ends with it, like the token repair (Home Assistant aborts an entry's reauth flows when it reloads it)."""
    aioclient_mock.get(f"{API}/project/junghome", json=_share_export())
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    reauth = await _start_reauth(hass, entry)
    result = await _start_reconfigure(hass, entry, "gateway_refetch")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UNICAST: "0D00"}
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert not [
        f
        for f in hass.config_entries.flow.async_progress()
        if f["flow_id"] == reauth["flow_id"]
    ]


# --------------------------------------------------------------------------- stored files


async def test_incoming_file_is_discarded_when_the_flow_goes_away(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
) -> None:
    """A fetched / uploaded file still under its temporary name when the flow is removed is deleted with it."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert isinstance(flow, JungHomeConfigFlow)
    incoming = flow._incoming_path()
    incoming.parent.mkdir(parents=True, exist_ok=True)
    incoming.write_text("{}")
    flow._incoming = incoming
    hass.config_entries.flow.async_abort(result["flow_id"])
    # the removal deletes the file in the executor, a job started outside HA's tracked tasks
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _incoming_files(hass) == []
    flow.async_remove()  # idempotent once the file is forgotten


async def test_incoming_file_is_discarded_on_an_unexpected_error(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Whatever stops the validation of a fetched file (not only a form error) deletes the file."""
    _mock_gateway(aioclient_mock)
    result = await _start_gateway_flow(hass)
    with (
        patch(
            "custom_components.junghome_ble.config_flow.validate_input",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], PASSWORD_INPUT
        )
    assert _incoming_files(hass) == []
    assert not _stored(hass).exists()

    # ... and whatever stops the finish after validation (here: the identity check)
    result = await _start_gateway_flow(hass)
    with (
        patch(
            "custom_components.junghome_ble.config_flow.JungHomeConfigFlow.async_set_unique_id",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], PASSWORD_INPUT
        )
    assert _incoming_files(hass) == []
    assert not _stored(hass).exists()


async def test_upload_copy_failure_leaves_no_partial_file(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
) -> None:
    """A copy that fails half-way (the file already created) is deleted before the error is shown."""

    def half_copy(hass_: Any, file_id: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("{")
        raise OSError("disk full")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _choose(hass, result, "upload")
    with patch(
        "custom_components.junghome_ble.config_flow._copy_upload", side_effect=half_copy
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _upload_input()
        )
    assert result["errors"] == {"base": "upload_failed"}
    assert _incoming_files(hass) == []


async def test_reconfigure_away_from_the_store_deletes_the_stored_export(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
) -> None:
    """A gateway / upload entry moved to a path on the host: the stored copy and its `.bak` go."""
    entry = _gateway_entry(hass, source="upload")
    entry.add_to_hass(hass)
    stored = _stored(hass)
    bak = stored.with_name(stored.name + ".bak")
    bak.write_bytes(stored.read_bytes())
    result = await _start_reconfigure(hass, entry, "path")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], FORM_INPUT
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_CDB_PATH] == CDB_PATH
    assert entry.data[CONF_SOURCE] == "path"
    assert not stored.exists()
    assert not bak.exists()


async def test_reconfigure_of_a_path_entry_deletes_nothing(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
    tmp_path: Path,
) -> None:
    """A user-provided export is never the integration's to delete, whatever the reconfigure does."""
    copy = tmp_path / "MeshNetwork.json"
    shutil.copy(CDB_PATH, copy)
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, CONF_CDB_PATH: str(copy)}
    )
    result = await _start_reconfigure(hass, mock_config_entry, "path")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], FORM_INPUT
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert copy.exists()


def test_forget_stored_export_takes_the_backup_along(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    stored = store / "X.json"
    stored.write_text("{}")
    for suffix in (
        ".bak",
        ".bak.1",
        ".bak.2",
        ".app",
        ".pre-adopt",
        ".pre-reconfigure",
    ):
        stored.with_name("X.json" + suffix).write_text("{}")
    forget_stored_export(stored)
    assert sorted(p.name for p in store.iterdir()) == []
    forget_stored_export(stored)  # already gone: not an error


# --------------------------------------------------------------------------- upload


@contextmanager
def _uploaded(path: str) -> Generator[Path]:
    yield Path(path)


def _upload_input(unicast: str = "0d00") -> dict[str, str]:
    return {CONF_EXPORT_FILE: str(uuid.uuid4()), CONF_UNICAST: unicast}


async def test_upload_flow(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    network_id: bytes,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _choose(hass, result, "upload")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "upload"
    user_input = _upload_input()
    with patch(
        "custom_components.junghome_ble.config_flow.process_uploaded_file",
        return_value=_uploaded(SHARE_EXPORT_PATH),
    ) as process:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input
        )
    await hass.async_block_till_done()
    assert process.call_args[0][1] == user_input[CONF_EXPORT_FILE]

    assert result["type"] is FlowResultType.CREATE_ENTRY
    stored = _stored(hass)
    assert result["data"] == {
        CONF_SOURCE: "upload",
        CONF_CDB_PATH: str(stored),
        CONF_METADATA_DIR: "",
        CONF_UNICAST: "0D00",
        CONF_MESH_UUID: MESH_UUID,
    }
    assert result["result"].unique_id == network_id.hex()
    assert json.loads(stored.read_text()) == _share_export()
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert _incoming_files(hass) == []


@pytest.mark.parametrize(
    ("upload", "user_input", "errors"),
    [
        (ValueError("File does not exist"), _upload_input(), {"base": "upload_failed"}),
        (_uploaded(CDB_PATH), _upload_input("0148"), {CONF_UNICAST: "address_in_use"}),
        (_uploaded(CDB_PATH), _upload_input("xyz"), {CONF_UNICAST: "invalid_address"}),
        ("garbage", _upload_input(), {"base": "cannot_load"}),
        ("malformed", _upload_input(), {"base": "invalid_export"}),
    ],
    ids=[
        "upload_failed",
        "address_in_use",
        "invalid_address",
        "cannot_load",
        "invalid_export",
    ],
)
async def test_upload_flow_errors(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    tmp_path: Path,
    upload: Any,
    user_input: dict[str, str],
    errors: dict[str, str],
) -> None:
    if upload == "garbage":
        upload = _uploaded(_write(tmp_path / "garbage.json", "not json"))
    elif upload == "malformed":
        upload = _uploaded(
            _malformed(tmp_path, "bad.json", lambda n: n.update(netKeys="x"))
        )
    kwargs = (
        {"side_effect": upload}
        if isinstance(upload, Exception)
        else {"return_value": upload}
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _choose(hass, result, "upload")
    with patch(
        "custom_components.junghome_ble.config_flow.process_uploaded_file", **kwargs
    ) as consumed:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input
        )
    # taken out of Home Assistant's upload folder whatever fails (it holds every key), and not kept in ours
    consumed.assert_called_once()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "upload"
    assert result["errors"] == errors
    assert (
        result["data_schema"]({CONF_EXPORT_FILE: str(uuid.uuid4())})[CONF_UNICAST]
        == user_input[CONF_UNICAST]
    )  # the address typed is kept
    assert _incoming_files(hass) == []
    assert not _stored(hass).exists()


# --------------------------------------------------------------------------- bluetooth discovery


async def test_bluetooth_flow(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    service_info: Any,
    network_id: bytes,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    assert result["description_placeholders"] == {"network_id": network_id.hex()}
    flow = hass.config_entries.flow.async_get(result["flow_id"])
    assert flow["context"]["title_placeholders"] == {"network_id": network_id.hex()}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    result = await _choose(hass, result, "path")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "path"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ENTRY_DATA
    assert result["result"].unique_id == network_id.hex()
    assert len(mock_setup_entry.mock_calls) == 1


async def test_user_flow_finishes_while_a_discovery_of_the_same_mesh_is_pending(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    service_info: Any,
    network_id: bytes,
) -> None:
    """CFG-13: a proxy in range leaves a "Discovered" card for its mesh; a manual setup of that mesh must finish
    (and the card go with the new entry) rather than abort at its last step with "already_in_progress"."""
    discovered = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info
    )
    assert discovered["step_id"] == "bluetooth_confirm"
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == network_id.hex()
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []


async def test_bluetooth_flow_network_mismatch(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    service_info: Any,
) -> None:
    """The discovered proxy belongs to another mesh than the export that was provided."""
    other = make_service_info(b"\x22" * 8, address="11:22:33:44:55:66")
    mock_bluetooth_env["infos"].append(other)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=other
    )
    assert result["step_id"] == "bluetooth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _choose(hass, result, "path")
    assert result["step_id"] == "path"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "path"
    assert result["errors"] == {"base": "network_mismatch"}
    assert len(mock_setup_entry.mock_calls) == 0


@pytest.mark.parametrize(
    "service_data",
    [b"", b"\x00\x01\x02", b"\x01" + b"\x33" * 16],
    ids=["empty", "short", "node_identity"],
)
async def test_bluetooth_flow_not_supported(
    hass: HomeAssistant, mock_bluetooth_env: dict[str, Any], service_data: bytes
) -> None:
    info = make_service_info(b"\x00" * 8)
    info.service_data = {next(iter(info.service_data)): service_data}
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


async def test_bluetooth_flow_already_configured(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_config_entry: MockConfigEntry,
    service_info: Any,
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --------------------------------------------------------------------------- reconfigure


async def _start_reconfigure(
    hass: HomeAssistant, entry: MockConfigEntry, option: str
) -> Any:
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reconfigure"
    return await _choose(hass, result, option)


async def test_reconfigure_flow(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == [
        "gateway",
        "upload",
        "path",
    ]  # no gateway known: nothing to fetch again
    result = await _choose(hass, result, "path")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "path"
    assert result["errors"] == {}
    assert result["data_schema"]({}) == dict(
        mock_config_entry.data
    )  # pre-filled with the current values

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CDB_PATH: CDB_PATH, CONF_METADATA_DIR: "", CONF_UNICAST: "0d01"},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    # the mesh UUID is recorded from now on, the Network ID stays the unique_id
    assert mock_config_entry.data == {
        CONF_CDB_PATH: CDB_PATH,
        CONF_METADATA_DIR: "",
        CONF_UNICAST: "0D01",
        CONF_MESH_UUID: MESH_UUID,
        CONF_SOURCE: "path",
    }
    assert mock_config_entry.unique_id == "1fbd2c61a4b6e5a4"
    assert len(mock_setup_entry.mock_calls) == 1  # the entry was reloaded


async def test_reconfigure_flow_error(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    before = dict(mock_config_entry.data)
    result = await _start_reconfigure(hass, mock_config_entry, "path")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**FORM_INPUT, CONF_CDB_PATH: "/nowhere.json"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "path"
    assert result["errors"] == {"base": "cannot_load"}
    assert result["data_schema"]({})[CONF_CDB_PATH] == "/nowhere.json"
    assert mock_config_entry.data == before

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], FORM_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


@pytest.mark.parametrize(
    "recorded_uuid",
    [MESH_UUID, MESH_UUID.lower(), None],
    ids=["recorded", "recorded_lowercase", "legacy_entry"],
)
async def test_reconfigure_flow_after_key_refresh(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    refreshed_network: tuple[str, bytes],
    recorded_uuid: str | None,
) -> None:
    """After a NetKey refresh the export of the *same* mesh carries a new Network ID: the documented recovery
    (export again, Reconfigure) must succeed and move the entry's unique_id to the new Network ID."""
    new_path, new_id = refreshed_network
    mock_bluetooth_env["infos"] = [
        make_service_info(new_id)
    ]  # the proxies already advertise the new Network ID
    entry = _entry(recorded_uuid)
    entry.add_to_hass(hass)
    # ... which is why Bluetooth discovery has already offered the "new" network
    discovery = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=make_service_info(new_id)
    )
    assert discovery["type"] is FlowResultType.FORM

    result = await _start_reconfigure(hass, entry, "path")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**FORM_INPUT, CONF_CDB_PATH: new_path}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == new_id.hex() != "1fbd2c61a4b6e5a4"
    assert entry.data == {**ENTRY_DATA, CONF_CDB_PATH: new_path}
    assert len(mock_setup_entry.mock_calls) == 1
    # the stale discovery flow is gone, and the refreshed network is no longer offered as a new one
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    discovery = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=make_service_info(new_id)
    )
    assert discovery["type"] is FlowResultType.ABORT
    assert discovery["reason"] == "already_configured"


async def test_reconfigure_applies_the_key_refresh_the_hub_followed(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
) -> None:
    """The hub followed a key refresh to its end (the new key is in the address's sequence record) and the export
    still has the old key. The setup puts the followed key in (`async_apply_followed_key_refresh`); the flow did
    not, so it saw no proxy of the mesh (they advertise the new Network ID) and would record the old one.
    (Review 4: the record carries the proof the hub moved on, as every completion it stores now does.)"""
    new = NetKeyMaterial.derive(bytes.fromhex(OTHER_NETKEY))
    await seq_store_for_uuid(hass, MESH_UUID).async_save(
        {
            "addresses": {
                "0D00": {
                    "seq": 100,
                    "key_refresh": {
                        "key": OTHER_NETKEY,
                        "phase": 3,
                        "proof": "beacon",
                    },
                }
            }
        }
    )
    mock_bluetooth_env["infos"] = [make_service_info(new.network_id)]
    entry = _entry(MESH_UUID)
    entry.add_to_hass(hass)
    result = await _start_reconfigure(hass, entry, "path")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], FORM_INPUT
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == new.network_id.hex()


@pytest.mark.parametrize(
    ("recorded_uuid", "cdb_path"),
    [(MESH_UUID, "/gone.json"), (None, CDB_PATH)],
    ids=["recorded", "legacy_entry"],
)
async def test_reconfigure_flow_network_mismatch(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    other_mesh: tuple[str, bytes],
    recorded_uuid: str | None,
    cdb_path: str,
) -> None:
    """An export of a different mesh (another meshUUID) must not be attached to this entry. The recorded mesh UUID
    decides even when the entry's own export is gone; an entry without one is judged by the export it uses."""
    other_path, other_id = other_mesh
    mock_bluetooth_env["infos"].append(
        make_service_info(other_id, address="11:22:33:44:55:66")
    )
    entry = _entry(recorded_uuid, cdb_path)
    entry.add_to_hass(hass)
    before = dict(entry.data)
    result = await _start_reconfigure(hass, entry, "path")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**FORM_INPUT, CONF_CDB_PATH: other_path}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "network_mismatch"
    assert entry.data == before
    assert entry.unique_id == "1fbd2c61a4b6e5a4"
    assert len(mock_setup_entry.mock_calls) == 0


async def test_reconfigure_flow_legacy_entry_without_readable_export(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    other_mesh: tuple[str, bytes],
) -> None:
    """An entry without a recorded mesh UUID whose export cannot be read has nothing to compare against: the new
    export is accepted and its mesh UUID recorded."""
    other_path, other_id = other_mesh
    mock_bluetooth_env["infos"].append(
        make_service_info(other_id, address="11:22:33:44:55:66")
    )
    entry = _entry(None, "/gone.json")
    entry.add_to_hass(hass)
    result = await _start_reconfigure(hass, entry, "path")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**FORM_INPUT, CONF_CDB_PATH: other_path}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {
        **ENTRY_DATA,
        CONF_CDB_PATH: other_path,
        CONF_MESH_UUID: OTHER_MESH_UUID,
    }
    assert entry.unique_id == other_id.hex()


async def test_reconfigure_of_a_legacy_entry_refuses_a_mesh_another_entry_owns(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    other_mesh: tuple[str, bytes],
) -> None:
    """CFG-10: with nothing to compare against, a legacy entry's reconfigure still refuses a mesh another entry
    already covers (two entries on one mesh share its sequence-number store), as the create path does."""
    other_path, other_id = other_mesh
    mock_bluetooth_env["infos"].append(
        make_service_info(other_id, address="11:22:33:44:55:66")
    )
    owner = MockConfigEntry(
        domain=DOMAIN,
        title="Other mesh",
        unique_id=other_id.hex(),
        data={
            CONF_CDB_PATH: other_path,
            CONF_METADATA_DIR: META_DIR,
            CONF_UNICAST: "0D00",
            CONF_MESH_UUID: OTHER_MESH_UUID,
        },
    )
    owner.add_to_hass(hass)
    entry = _entry(None, "/gone.json")
    entry.add_to_hass(hass)
    before = dict(entry.data)
    result = await _start_reconfigure(hass, entry, "path")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**FORM_INPUT, CONF_CDB_PATH: other_path}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "mesh_already_configured"
    assert dict(entry.data) == before


async def test_reconfigure_refetch(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
    mock_learn: AsyncMock,
) -> None:
    """A gateway entry offers "fetch again": one form, the stored token, no registration."""
    aioclient_mock.get(f"{API}/project/junghome", json=_share_export())
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["menu_options"] == ["gateway_refetch", "gateway", "upload", "path"]
    assert result["description_placeholders"] == {"host": HOST}
    result = await _choose(hass, result, "gateway_refetch")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway_refetch"
    assert result["description_placeholders"] == {"host": HOST}
    assert result["data_schema"]({}) == {CONF_UNICAST: "0D00"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UNICAST: "0d02"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {
        CONF_SOURCE: "gateway",
        CONF_CDB_PATH: str(_stored(hass)),
        CONF_METADATA_DIR: "",
        CONF_UNICAST: "0D02",
        CONF_MESH_UUID: MESH_UUID,
        CONF_GATEWAY_SYNCED: export_digest(_share_export()),
        **GATEWAY_DATA,
    }
    assert json.loads(_stored(hass).read_text()) == _share_export()
    assert _incoming_files(hass) == []
    assert aioclient_mock.mock_calls[0][3] == {"token": TOKEN}
    assert aioclient_mock.call_count == 1
    assert len(mock_setup_entry.mock_calls) == 1
    mock_learn.assert_not_awaited()  # the entry's pin was used, nothing learned anew


async def test_reconfigure_keeps_the_export_it_replaces(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Review-4 S4-6: fetching the export again was the way out of a gateway and a file that both changed, and it
    replaced the file without a copy — losing what Home Assistant had wired that the devices still use. The file it
    replaces is kept (`.pre-reconfigure`, owner-only), the directory fsynced, and the fetched export recorded as
    what the gateway and the file both hold (the entry's sync record, not only `entry.data`)."""
    aioclient_mock.get(f"{API}/project/junghome", json=_share_export())
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    before = _stored(hass).read_bytes()
    record = gateway_sync(hass, entry.entry_id)
    record.last_sync = "kept"
    record.loaded = True
    synced: list[Path] = []
    real_fsync_dir = config_flow.fsync_dir

    def spy(path: Path) -> None:
        synced.append(path)
        real_fsync_dir(path)

    result = await _start_reconfigure(hass, entry, "gateway_refetch")
    with patch.object(config_flow, "fsync_dir", spy):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_UNICAST: "0D00"}
        )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    kept = pre_reconfigure_path(_stored(hass))
    assert kept.read_bytes() == before
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert json.loads(_stored(hass).read_text()) == _share_export()
    assert synced == [_stored(hass).parent]
    assert record.synced == export_digest(_share_export())
    assert record.last_sync == "kept"  # a fetch is no upload


async def test_reconfigure_refetch_token_rejected(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The gateway no longer accepts the stored token (access reset in the app): a new approval is requested."""
    aioclient_mock.get(
        f"{API}/project/junghome",
        side_effect=_sequence({"status": 401}, {"json": _share_export()}),
    )
    aioclient_mock.post(f"{API}/register", json={"token": TOKEN_2})
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    result = await _start_reconfigure(hass, entry, "gateway_refetch")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UNICAST: "0D00"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "gateway_register"
    result = await _advance_progress(hass, result)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_GATEWAY_TOKEN] == TOKEN_2
    assert entry.data[CONF_CDB_PATH] == str(_stored(hass))
    assert _calls(aioclient_mock, "/project/junghome")[-1][3] == {"token": TOKEN_2}


async def test_reconfigure_refetch_registration_fails(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A rejected token and no approval: the gateway form comes up with the reason and the known host."""
    aioclient_mock.get(f"{API}/project/junghome", status=401)
    aioclient_mock.post(f"{API}/register", exc=TimeoutError())
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    before = dict(entry.data)
    result = await _start_reconfigure(hass, entry, "gateway_refetch")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UNICAST: "0D00"}
    )
    result = await _advance_progress(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway"
    assert result["errors"] == {"base": "cannot_connect"}
    assert result["data_schema"]({})[CONF_GATEWAY_HOST] == HOST
    assert entry.data == before


@pytest.mark.parametrize(
    ("user_input", "errors"),
    [
        ({CONF_UNICAST: "0"}, {CONF_UNICAST: "invalid_address"}),
        ({CONF_UNICAST: "0148"}, {CONF_UNICAST: "address_in_use"}),
    ],
    ids=["invalid_address", "address_in_use"],
)
async def test_reconfigure_refetch_bad_address(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
    user_input: dict[str, str],
    errors: dict[str, str],
) -> None:
    aioclient_mock.get(f"{API}/project/junghome", json=_share_export())
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    result = await _start_reconfigure(hass, entry, "gateway_refetch")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gateway_refetch"
    assert result["errors"] == errors
    assert result["data_schema"]({})[CONF_UNICAST] == user_input[CONF_UNICAST]
    assert _incoming_files(hass) == []


async def test_reconfigure_refetch_network_mismatch(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
    other_mesh: tuple[str, bytes],
) -> None:
    """The gateway serves another mesh (a different gateway at the old address): refused, nothing kept."""
    other_path, other_id = other_mesh
    mock_bluetooth_env["infos"].append(
        make_service_info(other_id, address="11:22:33:44:55:66")
    )
    aioclient_mock.get(f"{API}/project/junghome", json=_read_json(other_path))
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    before = dict(entry.data)
    result = await _start_reconfigure(hass, entry, "gateway_refetch")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UNICAST: "0D00"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "network_mismatch"
    assert entry.data == before
    assert not _stored(hass, OTHER_MESH_UUID).exists()
    assert _incoming_files(hass) == []


async def test_reconfigure_gateway_step_reuses_token(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The full gateway form on a gateway entry: the same host without a password reuses the stored token;
    a rejected token falls back to a new approval."""
    aioclient_mock.get(
        f"{API}/project/junghome",
        side_effect=_sequence({"status": 401}, {"json": _share_export()}),
    )
    aioclient_mock.post(f"{API}/register", json={"token": TOKEN_2})
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    result = await _start_reconfigure(hass, entry, "gateway")
    assert result["data_schema"]({}) == {CONF_GATEWAY_HOST: HOST, CONF_UNICAST: "0D00"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], GATEWAY_INPUT
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS  # token rejected: approval
    result = await _advance_progress(hass, result)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_GATEWAY_TOKEN] == TOKEN_2
    assert _calls(aioclient_mock, "/version/") == []  # no probe when a token is at hand


async def test_reconfigure_gateway_step_new_host(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A path entry moved to the gateway: the entry now records the gateway and the stored export."""
    _mock_gateway(aioclient_mock)
    mock_config_entry.add_to_hass(hass)
    result = await _start_reconfigure(hass, mock_config_entry, "gateway")
    assert result["data_schema"]({})[CONF_GATEWAY_HOST] == GATEWAY_DEFAULT_HOST
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], PASSWORD_INPUT
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data == {
        CONF_SOURCE: "gateway",
        CONF_CDB_PATH: str(_stored(hass)),
        CONF_METADATA_DIR: "",
        CONF_UNICAST: "0D00",
        CONF_MESH_UUID: MESH_UUID,
        CONF_GATEWAY_SYNCED: export_digest(_share_export()),
        **GATEWAY_DATA,
    }


async def test_reconfigure_gateway_step_other_gateway(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
    mock_learn: AsyncMock,
) -> None:
    """A different address than the entry's gateway: its token is not tried, access is requested anew."""
    other_api = "https://10.0.0.9/api/junghome"
    aioclient_mock.get(f"{other_api}/version/", json={"api_version": "1.5.0"})
    aioclient_mock.post(f"{other_api}/register", json={"token": TOKEN_2})
    aioclient_mock.get(f"{other_api}/project/junghome", json=_share_export())
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    result = await _start_reconfigure(hass, entry, "gateway")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**GATEWAY_INPUT, CONF_GATEWAY_HOST: "10.0.0.9"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    result = await _advance_progress(hass, result)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_GATEWAY_HOST] == "10.0.0.9"
    assert entry.data[CONF_GATEWAY_TOKEN] == TOKEN_2
    assert _calls(aioclient_mock, "/project/junghome")[0][3] == {"token": TOKEN_2}
    # another host: the entry's pin does not apply, the certificate was learned for the new one
    mock_learn.assert_awaited_once()
    assert mock_learn.call_args[0][1] == "10.0.0.9"


async def test_reconfigure_upload(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
) -> None:
    """A gateway entry given an uploaded export keeps the gateway so "fetch again" stays available."""
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    result = await _start_reconfigure(hass, entry, "upload")
    assert (
        result["data_schema"]({CONF_EXPORT_FILE: str(uuid.uuid4())})[CONF_UNICAST]
        == "0D00"
    )
    with patch(
        "custom_components.junghome_ble.config_flow.process_uploaded_file",
        return_value=_uploaded(SHARE_EXPORT_PATH),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _upload_input("0d03")
        )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {
        CONF_SOURCE: "upload",
        CONF_CDB_PATH: str(_stored(hass)),
        CONF_METADATA_DIR: "",
        CONF_UNICAST: "0D03",
        CONF_MESH_UUID: MESH_UUID,
        **GATEWAY_DATA,
    }
    assert Path(entry.data[CONF_CDB_PATH]) == _stored(hass)
    assert json.loads(_stored(hass).read_text()) == _share_export()  # replaced in place


@pytest.mark.parametrize("option", ["gateway_refetch", "upload"])
async def test_reconfigure_replacing_the_export_drops_the_stale_merge_base(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
    option: str,
) -> None:
    """The export fetched or uploaded again lands at the same path; the app's upload kept beside it as the merge
    base (`app_copy_path`) is an older one. `MeshConfigurator._carry_over` would take all the app changed in
    between for Home Assistant's own changes, and put it back onto the app's next upload."""
    aioclient_mock.get(f"{API}/project/junghome", json=_share_export())
    entry = _gateway_entry(hass)
    entry.add_to_hass(hass)
    base = app_copy_path(_stored(hass))
    base.write_text(json.dumps({"meshNetwork": {"stale": True}}))
    result = await _start_reconfigure(hass, entry, option)
    with patch(
        "custom_components.junghome_ble.config_flow.process_uploaded_file",
        return_value=_uploaded(SHARE_EXPORT_PATH),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_UNICAST: "0d00"} if option == "gateway_refetch" else _upload_input(),
        )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert json.loads(_stored(hass).read_text()) == _share_export()
    assert not base.exists()  # the next save keeps the export now on disk instead


async def test_reconfigure_menu_offers_the_gateway_import(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The import step is listed only while a JUNG HOME Gateway entry exists (the flow itself: test_migration.py)."""
    mock_config_entry.add_to_hass(hass)
    MockConfigEntry(
        domain=GATEWAY_DOMAIN, title="JUNG HOME Gateway", unique_id="serial-1"
    ).add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["gateway", "upload", "path", "import_gateway"]


# --------------------------------------------------------------------------- reconfigure: who reloads

# Home Assistant 2026.9 deprecates a config flow that reloads the entry itself while the integration has an update
# listener (`async_update_reload_and_abort` logs "has an update listener and should use it for scheduling a
# reload"); the listener does the reloading for a loaded entry, the flow only for one that is not loaded or whose
# data did not change. These tests count the setups and watch the frame helper for the deprecation report.


@pytest.fixture
def count_setups() -> Generator[AsyncMock]:
    """Count the entry setups (the second half of every reload) without replacing them."""
    with patch(
        "custom_components.junghome_ble.async_setup_entry",
        wraps=junghome_ble.async_setup_entry,
    ) as mock:
        yield mock


def _deprecation_reports(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the frame helper reported about this integration (a deprecated usage is a WARNING there)."""
    return [
        r.message
        for r in caplog.records
        if r.name == "homeassistant.helpers.frame" and r.levelno >= logging.WARNING
    ]


async def test_reconfigure_of_a_loaded_entry_reloads_once_through_the_listener(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    count_setups: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The flow updates the entry; the update listener sees the hub's inputs change and reloads, exactly once."""
    entry = init_integration
    hub = entry.runtime_data
    count_setups.reset_mock()
    result = await _start_reconfigure(hass, entry, "path")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**FORM_INPUT, CONF_UNICAST: "0d05"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    await wait_for_link(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data == {**ENTRY_DATA, CONF_UNICAST: "0D05"}
    assert entry.runtime_data is not hub
    assert entry.runtime_data.proxy.state.src == 0x0D05
    assert count_setups.call_count == 1
    assert _deprecation_reports(caplog) == []


async def test_reconfigure_of_an_unloaded_entry_reloads_it_explicitly(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: Any,
    fast_sleep: list[float],
    count_setups: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An entry that failed to set up has no update listener: the flow schedules the reload itself, once."""
    proxies = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    count_setups.reset_mock()

    mock_bluetooth_env["infos"] = proxies  # a proxy is in range again
    result = await _start_reconfigure(hass, mock_config_entry, "path")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], FORM_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    await wait_for_link(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.data == ENTRY_DATA
    assert mock_config_entry.runtime_data.connected
    assert count_setups.call_count == 1
    assert _deprecation_reports(caplog) == []


async def test_reconfigure_with_unchanged_data_still_reloads_once(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: Any,
    fast_sleep: list[float],
    count_setups: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same export named again (a re-fetch or re-upload lands in the same file, a key refresh included) leaves the
    entry data as it was, so no listener runs: the flow reloads, and only once."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data=ENTRY_DATA,
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    hub = entry.runtime_data
    count_setups.reset_mock()

    result = await _start_reconfigure(hass, entry, "path")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], FORM_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    await wait_for_link(hass, entry)
    assert entry.data == ENTRY_DATA
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not hub
    assert count_setups.call_count == 1
    assert _deprecation_reports(caplog) == []


async def test_gateway_only_data_update_does_not_reload(
    hass: HomeAssistant, init_integration: MockConfigEntry, count_setups: AsyncMock
) -> None:
    """A new gateway host / token / certificate pin changes nothing for the running hub; its own inputs do."""
    entry = init_integration
    hub = entry.runtime_data
    count_setups.reset_mock()
    assert hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_GATEWAY_HOST: HOST,
            CONF_GATEWAY_TOKEN: TOKEN_2,
            CONF_GATEWAY_FINGERPRINT: OTHER_FINGERPRINT,
        },
    )
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is hub
    assert not hub.needs_rebuild
    assert count_setups.call_count == 0

    assert hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_UNICAST: "0D07"}
    )
    await hass.async_block_till_done()
    await wait_for_link(hass, entry)
    assert entry.runtime_data is not hub
    assert entry.runtime_data.proxy.state.src == 0x0D07
    assert count_setups.call_count == 1


# --------------------------------------------------------------------------- options


async def test_options_flow(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_setup_entry: AsyncMock
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["data_schema"]({}) == {
        OPTION_CLICK_DELAY: False,
        OPTION_HEARTBEATS: False,
        OPTION_ALLOW_PROVISIONING: False,
        OPTION_PROVISIONER_IDENTITY: False,
    }  # the defaults
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {OPTION_CLICK_DELAY: True, OPTION_HEARTBEATS: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {
        OPTION_CLICK_DELAY: True,
        OPTION_HEARTBEATS: True,
        OPTION_ALLOW_PROVISIONING: False,
        OPTION_PROVISIONER_IDENTITY: False,
    }
    # the form offers the stored values next time
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["data_schema"]({}) == {
        OPTION_CLICK_DELAY: True,
        OPTION_HEARTBEATS: True,
        OPTION_ALLOW_PROVISIONING: False,
        OPTION_PROVISIONER_IDENTITY: False,
    }


async def test_options_change_reloads_the_entry(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    count_setups: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The hub reads the option when it starts, so a changed option reloads (once, through the update listener); a
    title change does not."""
    hub = init_integration.runtime_data
    assert hub.click_delay is False
    count_setups.reset_mock()
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {OPTION_CLICK_DELAY: True, OPTION_HEARTBEATS: False}
    )
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.LOADED
    assert init_integration.runtime_data is not hub
    assert init_integration.runtime_data.click_delay is True
    assert count_setups.call_count == 1
    assert _deprecation_reports(caplog) == []

    hub = init_integration.runtime_data
    hass.config_entries.async_update_entry(init_integration, title="Renamed")
    await hass.async_block_till_done()
    assert init_integration.runtime_data is hub
    assert count_setups.call_count == 1
