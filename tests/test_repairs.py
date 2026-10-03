"""Repair issues: every one links to its entry in the user guide, and the review-4 U4-5 fix flows (`repairs.py`).

The *Learn more* anchors are checked against the page as GitHub renders its headings (`test_docs.anchors`), and every
`ir.async_create_issue` call of the integration is checked to pass one. Each fix flow runs here against a real issue
where a hub raises it, with its success and its aborts: the entry gone or not running, the gateway refusing, an
upload that does not pass. No test talks to a real gateway: its answers are mocked HTTP.
"""

from __future__ import annotations

import ast
import json
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome_ble import const, repairs
from custom_components.junghome_ble.config_flow import CONF_MESH_UUID, export_path
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_EXPORT_FILE,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_PIN_SOURCE,
    CONF_GATEWAY_SYNCED,
    CONF_GATEWAY_TOKEN,
    CONF_METADATA_DIR,
    CONF_SOURCE,
    CONF_UNICAST,
    DOMAIN,
    ISSUE_ADDRESS_IN_USE,
    ISSUE_DEVICE_NAME,
    ISSUE_EXPORT_STALE,
    ISSUE_GATEWAY_SYNC,
    ISSUE_KEY_REFRESH,
    ISSUE_LEARN_MORE,
    LEARN_MORE_PAGE,
    PIN_FROM_USER,
    STORAGE_DIR,
    learn_more_url,
)
from custom_components.junghome_ble.coordinator import issue_id
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.mesh_config import (
    MeshConfigurator,
    export_digest,
)
from custom_components.junghome_ble.services import _configurator
from custom_components.junghome_ble.tls import CONF_GATEWAY_FINGERPRINT

from .conftest import (
    CDB_PATH,
    META_DIR,
    SHARE_EXPORT_PATH,
    settle,
    wait_for_link,
)
from .helpers import UID_LIGHT_SWITCH, find_issue
from .test_docs import anchors

if TYPE_CHECKING:
    from collections.abc import Generator

    from homeassistant.components.repairs import RepairsFlow
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )

    from custom_components.junghome_ble.coordinator import JungHomeHub

    from .conftest import FakeProxyLink

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / DOMAIN
MESH_UUID = "1BAF3ADE-0000-4000-8000-000000000001"
HOST = "gateway.example"
API = f"https://{HOST}/api/junghome"
TOKEN = "tok-first"
FINGERPRINT = "ab" * 32
OTHER_FINGERPRINT = "cd" * 32
SHARE_EXPORT = json.loads(Path(SHARE_EXPORT_PATH).read_text(encoding="utf-8"))
BARE_EXPORT = json.loads(Path(CDB_PATH).read_text(encoding="utf-8"))


def write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def hub_of(entry: MockConfigEntry) -> JungHomeHub:
    return entry.runtime_data  # type: ignore[no-any-return]


async def start(hass: HomeAssistant, issue: ir.IssueEntry) -> RepairsFlow:
    """The issue's fix flow, bound as Home Assistant's repairs flow manager binds it."""
    flow = await repairs.async_create_fix_flow(hass, issue.issue_id, issue.data)
    flow.hass, flow.issue_id, flow.flow_id = hass, issue.issue_id, uuid.uuid4().hex
    return flow


def raised(
    hass: HomeAssistant, key: str, translation_key: str | None = None
) -> ir.IssueEntry:
    """The open issue `key`, with its *Learn more* link (by the wording it was raised with)."""
    issue = find_issue(hass, key)
    assert issue is not None, key
    assert issue.is_fixable
    assert issue.learn_more_url == learn_more_url(translation_key or key)
    return issue


def incoming_files(hass: HomeAssistant) -> list[Path]:
    store = Path(hass.config.path(STORAGE_DIR))
    return sorted(store.glob(".incoming-*")) if store.is_dir() else []


# ----------------------------------------------------------------------------- learn more


def test_every_issue_wording_links_to_a_heading_of_the_maintenance_page() -> None:
    """Review-4 U4-5: the link of every translation key exists as GitHub renders the published user guide."""
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    documentation = manifest["documentation"]
    assert documentation.rsplit("/", 1)[0] + "/maintenance.md" == LEARN_MORE_PAGE
    page = ROOT / LEARN_MORE_PAGE.removeprefix(
        "https://github.com/ernetas/junghome-bt-mesh/blob/main/"
    )
    offered = anchors(page.read_text(encoding="utf-8"))
    assert set(ISSUE_LEARN_MORE) == set(strings["issues"])
    assert {k: a for k, a in ISSUE_LEARN_MORE.items() if a not in offered} == {}
    assert learn_more_url(ISSUE_KEY_REFRESH) == (
        f"{LEARN_MORE_PAGE}#jung-home-mesh-keys-are-changing"
    )


def _issue_calls() -> list[tuple[str, ast.Call]]:
    """(`file:line`, call) of every `ir.async_create_issue(...)` in the integration."""
    found: list[tuple[str, ast.Call]] = []
    for path in sorted(COMPONENT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found.extend(
            (f"{path.name}:{node.lineno}", node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "async_create_issue"
        )
    return found


ISSUE_CALLS = _issue_calls()


def test_the_creation_sites_are_found() -> None:
    assert len(ISSUE_CALLS) >= 27


@pytest.mark.parametrize(
    "call", [c for _, c in ISSUE_CALLS], ids=[site for site, _ in ISSUE_CALLS]
)
def test_every_created_issue_has_a_learn_more_link(call: ast.Call) -> None:
    """Each site passes `learn_more_url=learn_more_url(<its translation key>)`."""
    keywords = {k.arg: k.value for k in call.keywords}
    link = keywords.get("learn_more_url")
    assert isinstance(link, ast.Call)
    assert isinstance(link.func, ast.Name)
    assert link.func.id == "learn_more_url"
    assert ast.dump(link.args[0]) == ast.dump(keywords["translation_key"])


# ----------------------------------------------------------------------------- gateway_sync_failed


async def test_the_sync_repair_hands_the_export_to_the_gateway(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    configurator = _configurator(hass, init_integration.entry_id)
    configurator._sync_refused(
        SimpleNamespace(host=HOST),  # type: ignore[arg-type]
        "the gateway could not be checked",
        False,
        "service_gateway_sync_failed",
    )
    issue = raised(hass, ISSUE_GATEWAY_SYNC)
    assert issue.data == {"entry_id": init_integration.entry_id}
    flow = await start(hass, issue)
    assert isinstance(flow, repairs.GatewaySyncFlow)
    form = await flow.async_step_init()
    assert form["step_id"] == "confirm"
    assert form["description_placeholders"] == {
        "host": HOST,
        "error": "the gateway could not be checked",
    }

    # this entry has no gateway: the action's own refusal, and the issue stays
    result = await flow.async_step_confirm({})
    assert result["type"] == "abort"
    assert result["reason"] == "sync_failed"
    assert result["description_placeholders"]["error"]
    assert find_issue(hass, ISSUE_GATEWAY_SYNC) is not None

    with patch.object(
        MeshConfigurator, "sync_gateway", AsyncMock(return_value=False)
    ) as sync:
        result = await flow.async_step_confirm({})
    sync.assert_awaited_once()
    assert result["type"] == "create_entry"
    assert find_issue(hass, ISSUE_GATEWAY_SYNC) is None


async def test_the_sync_repair_needs_a_running_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    flow = await start(
        hass,
        SimpleNamespace(  # type: ignore[arg-type]
            issue_id=issue_id(init_integration, ISSUE_GATEWAY_SYNC),
            data={"entry_id": init_integration.entry_id},
        ),
    )
    form = await flow.async_step_init()
    assert form["description_placeholders"] == {}  # no issue open: nothing to fill in
    with patch.object(MeshConfigurator, "sync_gateway", AsyncMock()) as sync:
        await hass.config_entries.async_unload(init_integration.entry_id)
        result = await flow.async_step_confirm({})
        assert result["reason"] == "not_loaded"
        flow.issue_data = {"entry_id": "gone"}
        result = await flow.async_step_confirm({})
        assert result["reason"] == "entry_gone"
    sync.assert_not_awaited()


# ----------------------------------------------------------------------------- address_in_use


@pytest.fixture
def taken_entry() -> MockConfigEntry:
    """An entry whose address is a node's of its export (0148, the light switch)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={
            CONF_CDB_PATH: CDB_PATH,
            CONF_METADATA_DIR: META_DIR,
            CONF_UNICAST: "0148",
        },
    )


async def test_the_address_repair_moves_home_assistant_to_the_free_address(
    hass: HomeAssistant,
    taken_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    taken_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(taken_entry.entry_id)
    assert taken_entry.state is ConfigEntryState.SETUP_ERROR
    issue = raised(hass, ISSUE_ADDRESS_IN_USE)
    assert issue.data == {"entry_id": taken_entry.entry_id}
    flow = await start(hass, issue)
    assert isinstance(flow, repairs.FreeAddressFlow)
    form = await flow.async_step_init()
    assert form["step_id"] == "confirm"
    suggestion = CDB.load(Path(CDB_PATH)).suggest_unicast()
    assert suggestion is not None
    assert form["description_placeholders"] == {
        "title": "JUNG HOME mesh test",
        "unicast": "0148",
        "suggestion": f"{suggestion:04X}",
    }

    result = await flow.async_step_confirm({})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert taken_entry.data[CONF_UNICAST] == f"{suggestion:04X}"
    await wait_for_link(hass, taken_entry)
    assert taken_entry.state is ConfigEntryState.LOADED
    assert find_issue(hass, ISSUE_ADDRESS_IN_USE) is None
    assert hub_of(taken_entry).state.src == suggestion


async def test_the_address_repair_aborts_without_a_free_address(
    hass: HomeAssistant, taken_entry: MockConfigEntry, tmp_path: Path
) -> None:
    taken_entry.add_to_hass(hass)
    issue = SimpleNamespace(
        issue_id=issue_id(taken_entry, ISSUE_ADDRESS_IN_USE),
        data={"entry_id": taken_entry.entry_id},
    )
    flow = await start(hass, issue)  # type: ignore[arg-type]
    with patch.object(CDB, "suggest_unicast", return_value=None):
        assert (await flow.async_step_init())["reason"] == "no_free_address"

    # the export changed under the form: the address shown is a node's now
    form = await flow.async_step_init()
    assert form["step_id"] == "confirm"
    with patch.object(CDB, "unicast_is_free", return_value=False):
        result = await flow.async_step_confirm({})
    assert result["reason"] == "address_taken"
    assert taken_entry.data[CONF_UNICAST] == "0148"  # nothing changed

    garbage = write(tmp_path / "garbage.json", "not json")
    hass.config_entries.async_update_entry(
        taken_entry, data={**taken_entry.data, CONF_CDB_PATH: garbage}
    )
    assert (await flow.async_step_confirm({}))["reason"] == "cannot_load"
    assert (await flow.async_step_init())["reason"] == "cannot_load"

    flow.issue_data = {"entry_id": "gone"}
    assert (await flow.async_step_init())["reason"] == "entry_gone"
    assert (await flow.async_step_confirm({}))["reason"] == "entry_gone"


# ----------------------------------------------------------------------------- a new export: upload


@contextmanager
def uploaded(path: str | Exception) -> Generator[Any]:
    """Home Assistant's upload of `path` (or its failure), as the config flow's tests stub it."""

    @contextmanager
    def process(hass: HomeAssistant, file_id: str) -> Generator[Path]:
        if isinstance(path, Exception):
            raise path
        yield Path(path)

    with patch(
        "custom_components.junghome_ble.config_flow.process_uploaded_file",
        side_effect=process,
    ) as consumed:
        yield consumed


UPLOAD = {CONF_EXPORT_FILE: "0123456789abcdef"}


async def test_the_new_export_repair_takes_an_upload(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    hub_of(init_integration).report_key_refresh()
    issue = raised(hass, ISSUE_KEY_REFRESH)
    assert issue.data == {"entry_id": init_integration.entry_id}
    flow = await start(hass, issue)
    assert isinstance(flow, repairs.NewExportFlow)
    form = await flow.async_step_init()
    assert form["step_id"] == "upload"  # an entry set up from a file
    assert form["description_placeholders"] == {"title": "JUNG HOME mesh test"}

    with uploaded(SHARE_EXPORT_PATH) as consumed:
        result = await flow.async_step_upload(UPLOAD)
    consumed.assert_called_once()
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    stored = export_path(hass, MESH_UUID)
    assert init_integration.data[CONF_SOURCE] == "upload"
    assert init_integration.data[CONF_CDB_PATH] == str(stored)
    assert init_integration.data[CONF_UNICAST] == "0D00"
    assert init_integration.data[CONF_MESH_UUID] == MESH_UUID
    assert json.loads(stored.read_text()) == SHARE_EXPORT
    assert (
        init_integration.data[CONF_CDB_PATH] != CDB_PATH
    )  # the user's own file is left alone
    assert incoming_files(hass) == []
    assert find_issue(hass, ISSUE_KEY_REFRESH) is None
    await wait_for_link(hass, init_integration)
    assert init_integration.state is ConfigEntryState.LOADED
    flow.async_remove()  # nothing left to delete


async def test_the_app_changed_notice_is_fixed_by_a_new_export_from_either_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Review-4 U4-6: `app_changed` (an entry set up from a file) offers the new-export upload; any new export that
    loads — this repair's, another one's, Reconfigure's (`async_replace_export`) — clears it."""
    follower = hub_of(init_integration).app_follow
    assert follower is not None
    phone_config = SimpleNamespace(src=0x0001, dst=0x0148)
    follower._report(phone_config)  # type: ignore[arg-type]
    issue = raised(hass, const.ISSUE_APP_CHANGED)
    flow = await start(hass, issue)
    assert isinstance(flow, repairs.NewExportFlow)
    form = await flow.async_step_init()
    assert form["step_id"] == "upload"
    assert form["description_placeholders"] == {"title": "JUNG HOME mesh test"}

    # the key-refresh repair's new export clears it too
    hub_of(init_integration).report_key_refresh()
    other = await start(hass, raised(hass, ISSUE_KEY_REFRESH))
    with uploaded(SHARE_EXPORT_PATH):
        result = await other.async_step_upload(UPLOAD)
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert find_issue(hass, const.ISSUE_APP_CHANGED) is None
    await wait_for_link(hass, init_integration)

    # and this one's own, once raised again by the new hub
    follower = hub_of(init_integration).app_follow
    assert follower is not None
    follower._report(phone_config)  # type: ignore[arg-type]
    flow = await start(hass, raised(hass, const.ISSUE_APP_CHANGED))
    with uploaded(SHARE_EXPORT_PATH):
        result = await flow.async_step_upload(UPLOAD)
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert find_issue(hass, const.ISSUE_APP_CHANGED) is None
    await wait_for_link(hass, init_integration)


@pytest.mark.parametrize(
    ("upload", "error"),
    [
        (ValueError("File does not exist"), "upload_failed"),
        ("garbage", "cannot_load"),
        ("taken", "address_in_use"),
    ],
)
async def test_the_new_export_repair_refuses_an_upload_that_does_not_pass(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    tmp_path: Path,
    upload: Any,
    error: str,
) -> None:
    """The file leaves Home Assistant's upload folder whatever fails, and is not kept in ours."""
    if upload == "garbage":
        upload = write(tmp_path / "garbage.json", "not json")
    hub_of(init_integration).report_key_refresh()
    flow = await start(hass, raised(hass, ISSUE_KEY_REFRESH))
    with (
        uploaded(SHARE_EXPORT_PATH if upload == "taken" else upload) as consumed,
        # "taken": the new export puts a node on Home Assistant's address, a field the repair's form does not have
        patch.object(CDB, "unicast_is_free", return_value=upload != "taken"),
    ):
        result = await flow.async_step_upload(UPLOAD)
    consumed.assert_called_once()
    assert result["type"] == "form"
    assert result["step_id"] == "upload"
    assert result["errors"] == {"base": error}
    assert incoming_files(hass) == []
    assert not export_path(hass, MESH_UUID).exists()
    assert find_issue(hass, ISSUE_KEY_REFRESH) is not None

    # a copy still in flight when the flow goes away (abandoned mid-check) goes with it
    assert isinstance(flow, repairs.NewExportFlow)
    assert flow._incoming is not None
    write(flow._incoming, "{}")
    flow.async_remove()
    await hass.async_block_till_done()
    assert incoming_files(hass) == []


async def test_the_new_export_repair_refuses_another_mesh_and_a_gone_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry, tmp_path: Path
) -> None:
    # the same keys, another mesh UUID: the proxies in range match it
    other = {
        "meshNetwork": {
            **BARE_EXPORT["meshNetwork"],
            "meshUUID": "2BEC62AA-0000-4000-8000-000000000002",
        }
    }
    hub_of(init_integration).report_key_refresh()
    issue = raised(hass, ISSUE_KEY_REFRESH)
    flow = await start(hass, issue)
    with uploaded(write(tmp_path / "other.json", json.dumps(other))):
        result = await flow.async_step_upload(UPLOAD)
    assert result["type"] == "abort"
    assert result["reason"] == "network_mismatch"
    assert incoming_files(hass) == []
    assert init_integration.data[CONF_CDB_PATH] == CDB_PATH

    # the entry removed while the form was open: the file is still taken in, then deleted
    flow.issue_data = {"entry_id": "gone"}
    with uploaded(SHARE_EXPORT_PATH) as consumed:
        result = await flow.async_step_upload(UPLOAD)
    consumed.assert_called_once()
    assert result["reason"] == "entry_gone"
    assert incoming_files(hass) == []
    assert (await flow.async_step_init())["reason"] == "entry_gone"
    assert (await flow.async_step_gateway_refetch())["reason"] == "entry_gone"


# ----------------------------------------------------------------------------- a new export: the gateway


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    with patch(
        "custom_components.junghome_ble.async_setup_entry", return_value=True
    ) as mock:
        yield mock


def gateway_entry(
    hass: HomeAssistant, fingerprint: str | None = FINGERPRINT
) -> MockConfigEntry:
    """An entry set up from the gateway, not running: a copy of the fixture export in our store."""
    stored = export_path(hass, MESH_UUID)
    stored.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(CDB_PATH, stored)
    data = {
        CONF_CDB_PATH: str(stored),
        CONF_METADATA_DIR: "",
        CONF_UNICAST: "0D00",
        CONF_MESH_UUID: MESH_UUID,
        CONF_SOURCE: "gateway",
        CONF_GATEWAY_HOST: HOST,
        CONF_GATEWAY_TOKEN: TOKEN,
        CONF_GATEWAY_PIN_SOURCE: PIN_FROM_USER,
    }
    if fingerprint is not None:
        data[CONF_GATEWAY_FINGERPRINT] = fingerprint
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data=data,
        minor_version=2,
    )
    entry.add_to_hass(hass)
    return entry


def stale_issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry:
    """`export_stale` as the hub raises it (`JungHomeHub._report_export_stale`)."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry, ISSUE_EXPORT_STALE),
        is_fixable=True,
        data={"entry_id": entry.entry_id},
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_EXPORT_STALE,
        learn_more_url=learn_more_url(ISSUE_EXPORT_STALE),
        translation_placeholders={"title": entry.title},
    )
    return raised(hass, ISSUE_EXPORT_STALE)


async def test_the_new_export_repair_fetches_from_the_gateway_again(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    export = SHARE_EXPORT
    aioclient_mock.get(f"{API}/project/junghome", json=export)
    entry = gateway_entry(hass)
    flow = await start(hass, stale_issue(hass, entry))
    with patch(
        "custom_components.junghome_ble.config_flow.async_learn_fingerprint"
    ) as learn:
        form = await flow.async_step_init()
        assert form["step_id"] == "gateway_refetch"
        assert form["description_placeholders"] == {
            "title": "JUNG HOME mesh test",
            "host": HOST,
        }
        assert aioclient_mock.call_count == 0  # nothing before the confirmation
        result = await flow.async_step_gateway_refetch({})
    learn.assert_not_called()  # the entry's pin, nothing learned anew
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert aioclient_mock.mock_calls[0][3] == {"token": TOKEN}
    stored = export_path(hass, MESH_UUID)
    assert json.loads(stored.read_text()) == export
    assert stored.with_name(stored.name + ".pre-reconfigure").is_file()
    assert entry.data[CONF_GATEWAY_SYNCED] == export_digest(export)
    assert entry.data[CONF_GATEWAY_FINGERPRINT] == FINGERPRINT
    assert entry.data[CONF_SOURCE] == "gateway"
    assert len(mock_setup_entry.mock_calls) == 1  # set up again from it
    assert find_issue(hass, ISSUE_EXPORT_STALE) is None
    assert incoming_files(hass) == []


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ({"status": 401}, "token_rejected"),
        (
            {
                "exc": aiohttp.ServerFingerprintMismatch(
                    bytes.fromhex(FINGERPRINT),
                    bytes.fromhex(OTHER_FINGERPRINT),
                    HOST,
                    443,
                )
            },
            "certificate_changed",
        ),
    ],
    ids=["token_rejected", "certificate_changed"],
)
async def test_the_new_export_repair_aborts_when_the_gateway_refuses(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
    answer: dict[str, Any],
    reason: str,
) -> None:
    aioclient_mock.get(f"{API}/project/junghome", **answer)
    entry = gateway_entry(hass)
    before = export_path(hass, MESH_UUID).read_bytes()
    flow = await start(hass, stale_issue(hass, entry))
    result = await flow.async_step_gateway_refetch({})
    assert result["type"] == "abort"
    assert result["reason"] == reason
    assert result["description_placeholders"] == {"host": HOST}
    assert export_path(hass, MESH_UUID).read_bytes() == before
    assert entry.data[CONF_GATEWAY_TOKEN] == TOKEN
    mock_setup_entry.assert_not_called()
    assert find_issue(hass, ISSUE_EXPORT_STALE) is not None
    assert incoming_files(hass) == []


async def test_the_new_export_repair_keeps_the_form_for_a_gateway_out_of_reach(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.get(f"{API}/project/junghome", exc=aiohttp.ClientError("down"))
    entry = gateway_entry(hass)
    flow = await start(hass, stale_issue(hass, entry))
    result = await flow.async_step_gateway_refetch({})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}

    # it answers, but with an export no proxy in range belongs to
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/project/junghome", json=SHARE_EXPORT)
    mock_bluetooth_env["infos"].clear()
    result = await flow.async_step_gateway_refetch({})
    assert result["errors"] == {"base": "no_proxy_visible"}
    assert incoming_files(hass) == []
    mock_setup_entry.assert_not_called()


async def test_the_new_export_repair_sends_nothing_without_a_pin(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    entry = gateway_entry(hass, fingerprint=None)
    flow = await start(hass, stale_issue(hass, entry))
    result = await flow.async_step_gateway_refetch({})
    assert result["reason"] == "no_gateway_pin"
    assert aioclient_mock.call_count == 0


# ----------------------------------------------------------------------------- device_name_rejected


async def renamed(hass: HomeAssistant, device: str, name: str) -> None:
    """Name a registry device as a user does, and let the rename it starts finish (`test_services.renamed`)."""
    dr.async_get(hass).async_update_device(device, name_by_user=name)
    await settle(hass)


async def test_the_name_repair_renames_the_device(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier(
        (DOMAIN, UID_LIGHT_SWITCH), init_integration.entry_id
    )
    assert device is not None
    with patch.object(
        MeshConfigurator,
        "rename_device",
        AsyncMock(
            side_effect=[
                ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="service_name_not_allowed",
                ),
                "Mirror",
            ]
        ),
    ) as rename:
        await renamed(hass, device.id, "50% off")
        issue = raised(hass, ISSUE_DEVICE_NAME)
        assert issue.data == {
            "entry_id": init_integration.entry_id,
            "device_id": device.id,
        }
        flow = await start(hass, issue)
        assert isinstance(flow, repairs.DeviceNameFlow)
        form = await flow.async_step_init()
        assert form["step_id"] == "name"
        assert form["data_schema"]({}) == {"name": "50% off"}  # to edit
        for name, error in (
            ("  ", "name_blank"),
            ("100%", "name_not_allowed"),
            ("x" * 31, "name_too_long"),
        ):
            result = await flow.async_step_name({"name": name})
            assert result["errors"] == {"name": error}, name
            assert result["data_schema"]({}) == {"name": name}  # kept as typed
        result = await flow.async_step_name({"name": "Mirror"})
        assert result["type"] == "create_entry"
        await settle(hass)
    assert rename.await_args_list[-1].args[-1] == "Mirror"
    assert find_issue(hass, ISSUE_DEVICE_NAME) is None
    device = registry.async_get(device.id)
    assert device is not None
    assert (device.name, device.name_by_user) == ("Mirror", None)


async def test_the_name_repair_aborts(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, UID_LIGHT_SWITCH), init_integration.entry_id
    )
    assert device is not None
    issue = SimpleNamespace(
        issue_id=issue_id(init_integration, ISSUE_DEVICE_NAME),
        data={"entry_id": init_integration.entry_id, "device_id": "gone"},
    )
    flow = await start(hass, issue)  # type: ignore[arg-type]
    assert (await flow.async_step_init())["reason"] == "device_gone"
    flow.issue_data = {"entry_id": init_integration.entry_id, "device_id": device.id}
    await hass.config_entries.async_unload(init_integration.entry_id)
    assert (await flow.async_step_name({"name": "Mirror"}))["reason"] == "not_loaded"
    flow.issue_data = {"entry_id": "gone"}
    assert (await flow.async_step_name())["reason"] == "entry_gone"


# ----------------------------------------------------------------------------- the dispatch


@pytest.mark.parametrize(
    ("prefix", "flow"),
    [
        (ISSUE_GATEWAY_SYNC, repairs.GatewaySyncFlow),
        (ISSUE_ADDRESS_IN_USE, repairs.FreeAddressFlow),
        (const.ISSUE_UNKNOWN_NODES, repairs.NewExportFlow),
        (ISSUE_EXPORT_STALE, repairs.NewExportFlow),
        (ISSUE_KEY_REFRESH, repairs.NewExportFlow),
        (ISSUE_DEVICE_NAME, repairs.DeviceNameFlow),
        (const.ISSUE_PDUS_DROPPED, repairs.SkipAheadFlow),
        (const.ISSUE_NODE_CLOCK_WRONG, repairs.SendTimeFlow),
    ],
)
async def test_each_fixable_issue_gets_its_flow(
    hass: HomeAssistant, prefix: str, flow: type
) -> None:
    assert isinstance(
        await repairs.async_create_fix_flow(hass, f"{prefix}_0123", None), flow
    )
