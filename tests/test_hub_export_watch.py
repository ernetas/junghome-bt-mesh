"""The hub's export watch (`hub/export_watch.py`): unknown nodes, the gateway's export refresh and the gateway's trust."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome_ble import coordinator
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_GATEWAY_FINGERPRINT,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_PIN_SOURCE,
    CONF_GATEWAY_SYNCED,
    CONF_GATEWAY_TOKEN,
    CONF_SOURCE,
    CONF_UNICAST,
    DOMAIN,
    ISSUE_GATEWAY_CERTIFICATE,
    ISSUE_UNKNOWN_NODES,
    ISSUE_UNKNOWN_NODES_GATEWAY,
    PIN_FROM_MESH,
    PIN_FROM_USER,
)
from custom_components.junghome_ble.coordinator import (
    JungHomeHub,
    entry_lock,
)
from custom_components.junghome_ble.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.junghome_ble.gateway_api import (
    GatewayCertificateMismatch,
    GatewayError,
    GatewayUnreachable,
    JungHomeGatewayApi,
)
from custom_components.junghome_ble.hub import export_watch
from custom_components.junghome_ble.hub.export_watch import ExportWatch
from custom_components.junghome_ble.jhmesh.advert import JUNG_COMPANY_ID
from custom_components.junghome_ble.mesh_config import export_digest, gateway_sync

from .conftest import (
    CDB_PATH,
    SHARE_EXPORT_PATH,
    FakeProxyLink,
    StateTransitions,
    make_service_info,
    settle,
    setup_entry,
    wait_for_link,
    wait_until,
)
from .helpers import (
    GATEWAY,
    find_issue,
)
from .property_helpers import PropertyMesh
from .test_coordinator import (
    GATEWAY_DATA,
    SECOND_PROXY,
    _read_bytes,
    _read_json,
    hub_of,
    tick,
)
from .test_coordinator import (
    answering_link as answering_link,  # noqa: PLC0414  # the fixture
)
from .test_coordinator import (
    gateway_entry as gateway_entry,  # noqa: PLC0414  # the fixture
)
from .test_coordinator import (
    init_answered as init_answered,  # noqa: PLC0414  # the fixture
)
from .test_coordinator import (
    no_property_reads as no_property_reads,  # noqa: PLC0414  # the autouse fixture
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant


async def test_unknown_node_of_our_network_raises_a_repair(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A proxy advert with our Network ID from a MAC the export does not know = a node added after the export."""
    hub = hub_of(init_answered)
    callback_ = mock_bluetooth_env["callbacks"][0]
    # other networks, non-MAC addresses (macOS UUIDs) and known nodes raise nothing
    callback_(
        make_service_info(bytes(8), address="AA:BB:CC:DD:EE:01"),
        BluetoothChange.ADVERTISEMENT,
    )
    callback_(
        make_service_info(network_id, address="5509DA5D-8030-253D-A681-1A25C5C09316"),
        BluetoothChange.ADVERTISEMENT,
    )
    callback_(
        make_service_info(network_id, address=SECOND_PROXY),
        BluetoothChange.ADVERTISEMENT,
    )
    assert hub.unknown_nodes == {}
    assert find_issue(hass, ISSUE_UNKNOWN_NODES) is None

    # an unknown MAC with our network id, carrying the JUNG record (a 2-gang push-button)
    record = {
        JUNG_COMPANY_ID: bytes.fromhex("03020000000500 9e000010fb30".replace(" ", ""))
    }
    callback_(
        make_service_info(
            network_id, address="30:fb:10:00:00:9e", manufacturer_data=record
        ),
        BluetoothChange.ADVERTISEMENT,
    )
    assert list(hub.unknown_nodes) == ["30:FB:10:00:00:9E"]
    assert hub.unknown_nodes["30:FB:10:00:00:9E"].product_id == 2
    issue = find_issue(hass, ISSUE_UNKNOWN_NODES)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    # not set up from a gateway: nothing to fetch, the wording without one (review-4 H4-8: no prose in placeholders)
    assert issue.translation_key == ISSUE_UNKNOWN_NODES
    assert issue.translation_placeholders == {
        "title": "JUNG HOME mesh test",
        "count": "1",
        "devices": "Push-button 2-gang 30:FB:10:00:00:9E",
    }
    assert set(issue.translation_placeholders) == issue_text_placeholders(
        ISSUE_UNKNOWN_NODES, "upload"
    )
    assert (
        "30:FB:10:00:00:9E belongs to this mesh but is not in the export (Push-button 2-gang 30:FB:10:00:00:9E)"
        in caplog.text
    )

    # seen again: nothing changes; a second one without a record is listed by its address
    callback_(
        make_service_info(
            network_id, address="30:FB:10:00:00:9E", manufacturer_data=record
        ),
        BluetoothChange.ADVERTISEMENT,
    )
    callback_(
        make_service_info(network_id, address="30:FB:10:00:00:01"),
        BluetoothChange.ADVERTISEMENT,
    )
    assert len(hub.unknown_nodes) == 2
    issue = find_issue(hass, ISSUE_UNKNOWN_NODES)
    assert issue is not None
    assert issue.translation_placeholders["count"] == "2"
    assert issue.translation_placeholders["devices"] == (
        "30:FB:10:00:00:01, Push-button 2-gang 30:FB:10:00:00:9E"
    )
    diagnostics = await async_get_config_entry_diagnostics(hass, init_answered)
    assert diagnostics["link"]["unknown_nodes"] == [
        {"address": "**REDACTED**", "product_id": None},
        {"address": "**REDACTED**", "product_id": 2},
    ]
    assert "30:FB:10:00:00:9E" not in str(diagnostics)


STRINGS_JSON = (
    Path(__file__).parent.parent / "custom_components" / "junghome_ble" / "strings.json"
)


def issue_text_placeholders(key: str, step: str) -> set[str]:
    """The placeholders the `key` repair's title and the fix flow's first step (`step`) use (strings.json)."""
    issue = json.loads(STRINGS_JSON.read_text())["issues"][key]
    text = issue["title"] + issue["fix_flow"]["step"][step]["description"]
    return set(re.findall(r"\{(\w+)\}", text))


async def test_unknown_node_issue_is_gone_after_a_reload_with_it_in_the_export(
    hass: HomeAssistant,
    init_answered: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """The issue is tied to the running hub: a reload (with a new export) starts clean and only re-raises it when the
    node is still missing and still advertising."""
    hub = hub_of(init_answered)
    mock_bluetooth_env["callbacks"][0](
        make_service_info(network_id, address="30:FB:10:00:00:01"),
        BluetoothChange.ADVERTISEMENT,
    )
    assert find_issue(hass, ISSUE_UNKNOWN_NODES) is not None
    assert hub.unknown_nodes
    await hass.config_entries.async_reload(init_answered.entry_id)
    await hass.async_block_till_done()
    assert init_answered.runtime_data is not hub
    assert init_answered.runtime_data.unknown_nodes == {}
    assert find_issue(hass, ISSUE_UNKNOWN_NODES) is None


NEW_MAC = "30:FB:10:00:00:01"


NEW_UUID = "30FB10FF-FE00-0001-0000-000000000000"


def _export_with_new_node(share_export: dict[str, Any]) -> dict[str, Any]:
    """The share export plus a copy of node 0148 provisioned as 0500 from the MAC NEW_MAC."""
    net = json.loads(base64.b64decode(share_export["network"]).decode())
    node = copy.deepcopy(next(n for n in net["nodes"] if n["unicastAddress"] == "0148"))
    node["UUID"], node["unicastAddress"], node["name"] = NEW_UUID, "0500", "Newcomer"
    net["nodes"].append(node)
    return {
        **share_export,
        "network": base64.b64encode(json.dumps(net).encode()).decode(),
    }


@pytest.mark.parametrize(
    "source", ["path", "upload", None], ids=["path", "upload", "unrecorded"]
)
async def test_unknown_node_leaves_a_users_export_alone(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    fast_sleep: list[float],
    network_id: bytes,
    source: str | None,
) -> None:
    """An entry that keeps the gateway's host and token but was (re)configured to a file of the user's own is not
    refreshed from the gateway: the fetch would overwrite that file in place. The repair issue does not promise
    the gateway's help either."""
    path = tmp_path / "MeshNetwork.json"
    shutil.copy(CDB_PATH, path)
    data = {CONF_CDB_PATH: str(path), CONF_UNICAST: "0D00", **GATEWAY_DATA}
    if source is not None:
        data[CONF_SOURCE] = source
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data=data,
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    original = _read_bytes(str(path))
    with patch.object(JungHomeGatewayApi, "fetch_project", AsyncMock()) as fetch:
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        await hass.async_block_till_done()
        await settle(hass)
    assert fetch.await_count == 0
    assert _read_bytes(str(path)) == original
    issue = find_issue(hass, ISSUE_UNKNOWN_NODES)
    assert issue is not None
    assert (
        issue.translation_key == ISSUE_UNKNOWN_NODES
    )  # the gateway is not where its export comes from
    assert "host" not in issue.translation_placeholders
    assert hub_of(entry).export_watch._export_refresh is None


async def test_unknown_node_fetches_the_gateways_export_and_follows_it(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
    state_transitions: StateTransitions,
) -> None:
    """An entry from a gateway asks the gateway for its export when a node of our mesh is not in ours; when that
    export lists the node it replaces the file and the hub takes it over — the app uploads its project right
    after provisioning, so this is how a new device shows up without the user. In place (review-4 D23): the link
    stays up, no entity passes through `unavailable`, and the new node's light is there and asked for its state.

    It is the configurator's gateway write: the old file is kept as `.bak` and the adopted export's digest is
    recorded as synced, so the next change does not take the gateway for ahead of Home Assistant."""
    hub = hub_of(gateway_entry)
    share = _read_json(SHARE_EXPORT_PATH)
    original = _read_bytes(gateway_entry.data[CONF_CDB_PATH])
    fetched: list[str] = []

    async def fetch_project(self: JungHomeGatewayApi) -> dict[str, Any]:
        fetched.append(self.host)
        return _export_with_new_node(share)

    with patch.object(JungHomeGatewayApi, "fetch_project", fetch_project):
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        issue = find_issue(hass, ISSUE_UNKNOWN_NODES)
        assert issue is not None  # raised at once; the fetch runs in the background
        # the wording that names the gateway, whose host is its one extra placeholder (review-4 H4-8)
        assert issue.translation_key == ISSUE_UNKNOWN_NODES_GATEWAY
        assert issue.translation_placeholders == {
            "title": gateway_entry.title,
            "count": "1",
            "devices": NEW_MAC,
            "host": "junghome.local",
        }
        assert set(issue.translation_placeholders) == issue_text_placeholders(
            ISSUE_UNKNOWN_NODES_GATEWAY, "gateway_refetch"
        )
        await hass.async_block_till_done()
        await wait_until(
            hass,
            lambda: (
                hub.export_watch._export_refresh is not None
                and hub.export_watch._export_refresh.done()
            ),
            what="the gateway export fetch and write",
        )  # a background task `block_till_done` does not wait for; its write is real (fsynced) I/O
        await wait_for_link(hass, gateway_entry)
        await settle(hass)
    assert fetched == ["junghome.local"]
    assert "following it" in caplog.text
    assert "reloading to follow the export" not in caplog.text
    assert hub_of(gateway_entry) is hub
    assert hub.link_count == 1
    assert hub.node_for_address(NEW_MAC) is not None
    assert hub.node_for_address(NEW_MAC).unicast == 0x0500
    assert hub.unknown_nodes == {}
    assert find_issue(hass, ISSUE_UNKNOWN_NODES) is None
    assert state_transitions.lost() == []
    newcomer = hass.states.get("light.wc_newcomer_0500")
    assert newcomer is not None
    assert newcomer.state == "off"  # its OnOff Status answered the state read
    saved = _read_json(gateway_entry.data[CONF_CDB_PATH])
    assert saved == _export_with_new_node(share)
    path = Path(gateway_entry.data[CONF_CDB_PATH])
    assert path.with_name(path.name + ".bak").read_bytes() == original
    assert gateway_sync(hass, gateway_entry.entry_id).synced == export_digest(
        _export_with_new_node(share)
    )


async def test_an_adopted_export_that_lists_some_unknown_nodes_leaves_the_others_reported(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """Followed in place (review-4 D23), the export the gateway knew takes its node off the repair; one it does not
    list either stays on it."""
    hub = hub_of(gateway_entry)
    other = "30:FB:10:00:00:42"
    hub.unknown_nodes[other] = None
    fetch = AsyncMock(return_value=_export_with_new_node(_read_json(SHARE_EXPORT_PATH)))
    with patch.object(JungHomeGatewayApi, "fetch_project", fetch):
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        await hass.async_block_till_done()
        await wait_until(
            hass,
            lambda: NEW_MAC not in hub.unknown_nodes and hub.node_for_address(NEW_MAC),
            what="the export followed",
        )
    assert hub_of(gateway_entry) is hub
    assert list(hub.unknown_nodes) == [other]
    issue = find_issue(hass, ISSUE_UNKNOWN_NODES)
    assert issue is not None
    assert (
        issue.translation_placeholders["count"],
        issue.translation_placeholders["devices"],
    ) == ("1", other)


class GatewayAnswers:
    """The gateway's answers to `fetch_project`, one per unknown node advertising (each triggers one refresh)."""

    def __init__(
        self, hass: HomeAssistant, entry: MockConfigEntry, env: dict[str, Any]
    ) -> None:
        self.hass, self.entry, self.env = hass, entry, env
        self.answers: list[Any] = []
        self.macs = iter(f"30:FB:10:00:00:{n:02X}" for n in range(2, 20))

    async def fetch_project(self) -> dict[str, Any]:
        """Patched onto the class as a bound method: called without the API instance."""
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[no-any-return]

    async def advert(self, network_id: bytes, mac: str | None = None) -> None:
        """A node advertises; wait for the refresh it started (a background task with real executor I/O)."""
        self.env["callbacks"][0](
            make_service_info(network_id, address=mac or next(self.macs)),
            BluetoothChange.ADVERTISEMENT,
        )
        await self.hass.async_block_till_done()
        hub = hub_of(self.entry)
        await wait_until(
            self.hass,
            lambda: (
                hub.export_watch._export_refresh is None
                or hub.export_watch._export_refresh.done()
            ),
            what="the gateway export refresh",
        )


async def test_gateway_export_refresh_leaves_the_issue_when_it_cannot_help(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = hub_of(gateway_entry)
    share = _read_json(SHARE_EXPORT_PATH)
    original = _read_bytes(gateway_entry.data[CONF_CDB_PATH])
    gw = GatewayAnswers(hass, gateway_entry, mock_bluetooth_env)
    other_mesh = _export_with_new_node(share)
    net = json.loads(base64.b64decode(other_mesh["network"]).decode())
    net["meshUUID"] = "00000000-0000-4000-8000-000000000099"
    other_mesh["network"] = base64.b64encode(json.dumps(net).encode()).decode()
    with patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project):
        gw.answers.append(GatewayError("GET project/junghome: HTTP 500"))
        await gw.advert(network_id, NEW_MAC)
        assert "Could not ask the gateway junghome.local for its export" in caplog.text
        assert hub_of(gateway_entry) is hub  # no reload
        # the same MAC again: no second fetch (one per node)
        await gw.advert(network_id, NEW_MAC)
        assert not gw.answers
        assert hub_of(gateway_entry) is hub
        # a second unknown node: the gateway's export does not list either of them
        gw.answers.append(share)
        await gw.advert(network_id)
        assert (
            "does not list the unknown node(s) 30:FB:10:00:00:01, 30:FB:10:00:00:02"
            in caplog.text
        )
        # another mesh's export, an export that does not parse
        gw.answers.append(other_mesh)
        await gw.advert(network_id)
        assert "holds the export of another mesh" in caplog.text
        gw.answers.append({"network": "not base64!", "meta": {}})
        await gw.advert(network_id)
        assert "does not parse" in caplog.text
    assert hub_of(gateway_entry) is hub
    assert _read_bytes(gateway_entry.data[CONF_CDB_PATH]) == original
    assert find_issue(hass, ISSUE_UNKNOWN_NODES) is not None
    assert len(hub.unknown_nodes) == 4


async def test_gateway_export_refresh_overwrites_nothing_it_must_not(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The guards of every gateway write: a file that cannot be written, the bare `/project/cdb` database over the
    share export (it has every name and room link), a file changed since the last sync (a change never handed
    over) while the gateway's changed too, and a gateway holding just what was synced last."""
    hub = hub_of(gateway_entry)
    share = _read_json(SHARE_EXPORT_PATH)
    newer = _export_with_new_node(share)
    original = _read_bytes(gateway_entry.data[CONF_CDB_PATH])
    gw = GatewayAnswers(hass, gateway_entry, mock_bluetooth_env)
    with patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project):
        gw.answers.append(newer)
        with patch(
            "custom_components.junghome_ble.configurator.store.write_private_with_backup",
            side_effect=OSError("read-only"),
        ):
            await gw.advert(network_id, NEW_MAC)
        assert "was not adopted: service_export_write_failed" in caplog.text
        bare = json.loads(base64.b64decode(newer["network"]).decode())
        gw.answers.append({"meshNetwork": bare})
        await gw.advert(network_id)
        assert "was not adopted: service_gateway_export_incomplete" in caplog.text
        # the file changed since the last sync too, and no copy of the app's last upload tells what HA changed
        gateway_sync(hass, gateway_entry.entry_id).synced = "0" * 64
        gw.answers.append(newer)
        await gw.advert(network_id)
        assert "was not adopted: service_gateway_export_newer" in caplog.text
        gateway_sync(hass, gateway_entry.entry_id).synced = export_digest(newer)
        gw.answers.append(newer)
        await gw.advert(network_id)
        assert "but nothing Home Assistant has not synced already" in caplog.text
    assert hub_of(gateway_entry) is hub
    assert _read_bytes(gateway_entry.data[CONF_CDB_PATH]) == original


async def test_gateway_export_refresh_takes_a_bare_database_over_a_bare_file(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    fast_sleep: list[float],
    network_id: bytes,
) -> None:
    """An entry set up from a gateway that only had `/project/cdb`: neither side has names to protect, and the
    refresh takes the gateway's database when it lists the new node (a change would plan on the file as it is)."""
    share = _read_json(SHARE_EXPORT_PATH)
    bare = json.loads(base64.b64decode(share["network"]).decode())
    path = tmp_path / "JungHome.json"
    path.write_text(json.dumps({"meshNetwork": bare}))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={
            CONF_CDB_PATH: str(path),
            CONF_UNICAST: "0D00",
            CONF_SOURCE: "gateway",
            **GATEWAY_DATA,
        },
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    newer = json.loads(
        base64.b64decode(_export_with_new_node(share)["network"]).decode()
    )
    with patch.object(
        JungHomeGatewayApi,
        "fetch_project",
        AsyncMock(return_value={"meshNetwork": newer}),
    ):
        hub = hub_of(entry)
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        await hass.async_block_till_done()
        await wait_until(
            hass,
            lambda: (
                hub.export_watch._export_refresh is not None
                and hub.export_watch._export_refresh.done()
            ),
            what="the gateway export fetch and write",
        )
        await hass.async_block_till_done()
        await wait_for_link(hass, entry)
        await settle(hass)
    assert _read_json(str(path)) == {"meshNetwork": newer}
    assert hub_of(entry).node_for_address(NEW_MAC) is not None


async def test_gateway_export_refresh_waits_for_the_configurator_and_a_link(
    hass: HomeAssistant, gateway_entry: MockConfigEntry
) -> None:
    """Without a configurator or a link nothing is fetched (the next link asks); the issue stands meanwhile."""
    hub = hub_of(gateway_entry)
    hub.unknown_nodes[NEW_MAC] = None
    configurator, hub.configurator = hub.configurator, None
    hub.export_watch.request_refresh()
    assert hub.export_watch._export_refresh is None
    hub.configurator = configurator
    with patch.object(
        JungHomeHub, "connected", new_callable=PropertyMock, return_value=False
    ):
        hub.export_watch.request_refresh()
    assert hub.export_watch._export_refresh is None


async def test_gateway_export_refresh_is_retried_until_the_app_uploaded(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    gateway_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """Review-3 C1: the fetch was tried once per MAC, ever — and that one attempt usually came before the app had
    uploaded its project, so a node added in the app never appeared until a reload. Unanswered, it is repeated
    after each EXPORT_REFRESH_BACKOFF delay."""
    hub = hub_of(gateway_entry)
    share = _read_json(SHARE_EXPORT_PATH)
    fetch = AsyncMock(side_effect=[share, share, _export_with_new_node(share)])

    async def fetched(count: int) -> None:
        await wait_until(
            hass,
            lambda: (
                fetch.await_count == count
                and hub.export_watch._export_refresh is not None
                and hub.export_watch._export_refresh.done()
            ),
            what=f"export fetch {count}",
        )

    with (
        patch.object(JungHomeGatewayApi, "fetch_project", fetch),
        patch.object(hub, "follow_export", AsyncMock()) as follow,
    ):
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        await fetched(1)
        assert hub.lifecycle.timer("export_refresh") is not None
        await tick(hass, freezer, export_watch.EXPORT_REFRESH_BACKOFF[0] + 1)
        await fetched(2)
        follow.assert_not_called()
        await tick(hass, freezer, export_watch.EXPORT_REFRESH_BACKOFF[0] + 1)
        assert fetch.await_count == 2  # the second delay is longer
        await tick(hass, freezer, export_watch.EXPORT_REFRESH_BACKOFF[1])
        await fetched(3)
        await hass.async_block_till_done()
        follow.assert_awaited_once_with()
    assert hub.lifecycle.timer("export_refresh") is None


async def test_following_the_adopted_export_waits_for_a_running_service_call(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """Review-3 W11: the unknown-node refresh reloaded the entry without the entry's lock, tearing the hub down
    under a service call working on it. Following the adopted export now waits for the lock; once it has it, a hub
    a reload replaced meanwhile (it read the adopted export already) does not follow it a second time."""
    hub = hub_of(gateway_entry)
    fetch = AsyncMock(return_value=_export_with_new_node(_read_json(SHARE_EXPORT_PATH)))
    lock = entry_lock(hass, gateway_entry.entry_id)
    with (
        patch.object(JungHomeGatewayApi, "fetch_project", fetch),
        patch.object(hub, "follow_export", AsyncMock()) as reload,
    ):
        await lock.acquire()  # a service call is running
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        # not `wait_until` / `settle`: they flush HA's tasks, and the reload waiting for the lock is one of them
        deadline = time.monotonic() + 10
        while not (
            hub.export_watch._export_refresh is not None
            and hub.export_watch._export_refresh.done()
        ):
            assert time.monotonic() < deadline, "the gateway export fetch and write"
            await asyncio.sleep(0)
        for _ in range(10):
            await asyncio.sleep(0)
        reload.assert_not_called()
        lock.release()
        await hass.async_block_till_done()
        reload.assert_awaited_once_with()
        # a hub that is no longer the entry's, or an entry no longer loaded: nothing to follow
        reload.reset_mock()
        with patch.object(gateway_entry, "runtime_data", object()):
            await hub.export_watch._reload_for_export()
        gateway_entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
        await hub.export_watch._reload_for_export()
        gateway_entry.mock_state(hass, ConfigEntryState.LOADED)
        reload.assert_not_called()


async def test_a_new_link_asks_the_gateway_again_and_stop_cancels_the_retry(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    answering_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """An unknown node heard while the link was down is asked about when the next link comes up."""
    hub = hub_of(gateway_entry)
    share = _read_json(SHARE_EXPORT_PATH)
    fetch = AsyncMock(return_value=share)
    with patch.object(JungHomeGatewayApi, "fetch_project", fetch):
        hub.unknown_nodes[NEW_MAC] = None
        with patch.object(
            JungHomeHub, "connected", new_callable=PropertyMock, return_value=False
        ):
            hub.export_watch.request_refresh()  # heard while the link is down
        await hass.async_block_till_done()
        assert fetch.await_count == 0
        answering_link.drop_link()
        await wait_until(hass, lambda: answering_link.connect_count == 2)
        await wait_for_link(hass, gateway_entry)
        await wait_until(
            hass,
            lambda: (
                hub.export_watch._export_refresh is not None
                and hub.export_watch._export_refresh.done()
            ),
            what="the export fetch of the new link",
        )
        assert fetch.await_count == 1
    assert hub.lifecycle.timer("export_refresh") is not None
    await hub.async_stop()
    assert hub.lifecycle.timer("export_refresh") is None


async def test_gateway_export_refresh_runs_one_fetch_at_a_time(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """Two nodes appearing while one fetch is in flight share that fetch."""
    hub = hub_of(gateway_entry)
    share = _read_json(SHARE_EXPORT_PATH)
    gate = asyncio.Event()
    calls = 0

    async def slow_fetch(self: JungHomeGatewayApi) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        await gate.wait()
        return share

    with patch.object(JungHomeGatewayApi, "fetch_project", slow_fetch):
        for mac in ("30:FB:10:00:00:06", "30:FB:10:00:00:07"):
            mock_bluetooth_env["callbacks"][0](
                make_service_info(network_id, address=mac),
                BluetoothChange.ADVERTISEMENT,
            )
        await asyncio.sleep(0)
        gate.set()
        await hass.async_block_till_done()
    assert calls == 1
    assert len(hub.unknown_nodes) == 2


NEW_GATEWAY_HOST = "192.0.2.77"


NEW_FINGERPRINT = "cd" * 32


def gateway_node_says(
    link: FakeProxyLink, host: bytes, fingerprint: bytes
) -> PropertyMesh:
    """The gateway node's LBC Manufacturer server answers `0xC002` / `0xC003` (NUL-padded text, as on air)."""
    return PropertyMesh(link, {(GATEWAY, 0xC002): host, (GATEWAY, 0xC003): fingerprint})


async def test_gateway_is_followed_to_its_new_address(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    answering_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """Unreachable at the stored host: the gateway node's own properties say where it is now; fetched from there.

    The address follows; the node reports the pinned certificate (with colons: normalised), which stays the pin,
    and the token stays the one Home Assistant registered.
    """
    gateway_node_says(
        answering_link,
        NEW_GATEWAY_HOST.encode() + b"\0\0",
        ":".join(["AB"] * 32).encode(),
    )
    hosts: list[str] = []

    async def fetch_project(self: JungHomeGatewayApi) -> dict[str, Any]:
        hosts.append(self.host)
        if self.host == GATEWAY_DATA[CONF_GATEWAY_HOST]:
            raise GatewayUnreachable("GET project/junghome: ClientConnectorError")
        raise GatewayError(
            "GET project/junghome: HTTP 500"
        )  # reached; what it says is another test

    with patch.object(JungHomeGatewayApi, "fetch_project", fetch_project):
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        await hass.async_block_till_done()
    assert hosts == [GATEWAY_DATA[CONF_GATEWAY_HOST], NEW_GATEWAY_HOST]
    data = gateway_entry.data
    assert data[CONF_GATEWAY_HOST] == NEW_GATEWAY_HOST
    assert data[CONF_GATEWAY_FINGERPRINT] == GATEWAY_DATA[CONF_GATEWAY_FINGERPRINT]
    assert data[CONF_GATEWAY_TOKEN] == GATEWAY_DATA[CONF_GATEWAY_TOKEN]
    assert hub_of(gateway_entry).entry is gateway_entry  # no reload for it
    assert find_issue(hass, ISSUE_GATEWAY_CERTIFICATE) is None


async def test_a_certificate_reported_over_the_mesh_is_never_adopted(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    answering_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Anyone with a node's keys can answer on the mesh: a certificate other than the pin stops the gateway's use
    and raises the repair pointing to Reconfigure — the pin stays, nothing is fetched from the new address, and
    the next unknown node does not ask the gateway either. The address itself is still followed."""
    gateway_node_says(
        answering_link, NEW_GATEWAY_HOST.encode(), NEW_FINGERPRINT.encode()
    )
    hosts: list[str] = []

    async def fetch_project(self: JungHomeGatewayApi) -> dict[str, Any]:
        hosts.append(self.host)
        raise GatewayCertificateMismatch(self.host, self.fingerprint, NEW_FINGERPRINT)

    with patch.object(JungHomeGatewayApi, "fetch_project", fetch_project):
        for mac in (NEW_MAC, "30:FB:10:00:00:02"):
            mock_bluetooth_env["callbacks"][0](
                make_service_info(network_id, address=mac),
                BluetoothChange.ADVERTISEMENT,
            )
            await hass.async_block_till_done()
    assert hosts == [GATEWAY_DATA[CONF_GATEWAY_HOST]]  # once: then no more
    data = gateway_entry.data
    assert data[CONF_GATEWAY_FINGERPRINT] == GATEWAY_DATA[CONF_GATEWAY_FINGERPRINT]
    assert data[CONF_GATEWAY_HOST] == NEW_GATEWAY_HOST
    issue = find_issue(hass, ISSUE_GATEWAY_CERTIFICATE)
    assert issue is not None
    assert issue.translation_placeholders == {"host": NEW_GATEWAY_HOST}
    assert "the gateway is not used until the entry is reconfigured" in caplog.text
    assert "export was not fetched for the unknown node(s)" in caplog.text


async def test_a_pinned_certificate_the_host_no_longer_presents_raises_the_repair(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    answering_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """The node vouches for the pin, the host presents another certificate: nothing was sent (TLS), the repair is
    raised; the next answer of the pinned gateway clears it."""
    gateway_node_says(
        answering_link,
        GATEWAY_DATA[CONF_GATEWAY_HOST].encode(),
        GATEWAY_DATA[CONF_GATEWAY_FINGERPRINT].encode(),
    )
    answers: list[Any] = [
        GatewayCertificateMismatch("junghome.local", "ab" * 32, NEW_FINGERPRINT),
        _read_json(SHARE_EXPORT_PATH),
    ]

    async def fetch_project(self: JungHomeGatewayApi) -> dict[str, Any]:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    with patch.object(JungHomeGatewayApi, "fetch_project", fetch_project):
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        await hass.async_block_till_done()
        assert find_issue(hass, ISSUE_GATEWAY_CERTIFICATE) is not None
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address="30:FB:10:00:00:02"),
            BluetoothChange.ADVERTISEMENT,
        )
        await hass.async_block_till_done()
    assert not answers
    assert find_issue(hass, ISSUE_GATEWAY_CERTIFICATE) is None


@pytest.mark.parametrize(
    "host",
    [b"192.0.2.300", b"gw.example:8443", b"-bad-.lan", b"fe80::1", b"x" * 64],
    ids=["octet", "port", "label", "ipv6", "long_label"],
)
async def test_an_address_that_is_no_host_is_not_followed(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    answering_link: FakeProxyLink,
    host: bytes,
) -> None:
    gateway_node_says(
        answering_link, host, GATEWAY_DATA[CONF_GATEWAY_FINGERPRINT].encode()
    )
    assert await hub_of(gateway_entry).async_follow_gateway() is False
    assert gateway_entry.data[CONF_GATEWAY_HOST] == GATEWAY_DATA[CONF_GATEWAY_HOST]


def test_gateway_hosts() -> None:
    assert coordinator.is_gateway_host("192.168.1.20")
    assert coordinator.is_gateway_host("junghome.local")
    assert coordinator.is_gateway_host("JungHome-2")
    assert not coordinator.is_gateway_host("192.168.1")
    assert not coordinator.is_gateway_host("a..b")


async def test_gateway_follow_without_news(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    answering_link: FakeProxyLink,
) -> None:
    """Nothing changes when the node repeats what the entry holds, says nothing, or there is no gateway node."""
    hub = hub_of(gateway_entry)
    gateway_node_says(
        answering_link,
        GATEWAY_DATA[CONF_GATEWAY_HOST].encode(),
        GATEWAY_DATA[CONF_GATEWAY_FINGERPRINT].encode(),
    )
    assert await hub.async_follow_gateway() is False
    with patch.object(hub.proxy, "request", AsyncMock(side_effect=TimeoutError)):
        assert await hub.async_follow_gateway() is False
    gateway = next(n for n in hub.cdb.nodes if n.unicast == GATEWAY)
    with patch.object(gateway, "pid", 0x01):
        assert await hub.async_follow_gateway() is False
    with patch.object(
        JungHomeHub, "connected", new_callable=PropertyMock, return_value=False
    ):
        assert await hub.async_follow_gateway() is False
    assert gateway_entry.data[CONF_GATEWAY_HOST] == GATEWAY_DATA[CONF_GATEWAY_HOST]
    assert find_issue(hass, ISSUE_GATEWAY_CERTIFICATE) is None


async def test_gateway_follow_needs_a_gateway_entry(
    hass: HomeAssistant, init_answered: MockConfigEntry
) -> None:
    assert await hub_of(init_answered).async_follow_gateway() is False


async def test_gateway_that_cannot_be_followed_stays_unreachable(
    hass: HomeAssistant,
    gateway_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def unreachable(self: JungHomeGatewayApi) -> dict[str, Any]:
        raise GatewayUnreachable("GET project/junghome: ClientConnectorError")

    with (
        patch.object(JungHomeGatewayApi, "fetch_project", unreachable),
        patch.object(
            JungHomeHub, "async_follow_gateway", AsyncMock(return_value=False)
        ) as follow,
    ):
        mock_bluetooth_env["callbacks"][0](
            make_service_info(network_id, address=NEW_MAC),
            BluetoothChange.ADVERTISEMENT,
        )
        await hass.async_block_till_done()
    follow.assert_awaited_once()
    assert "Could not ask the gateway junghome.local for its export" in caplog.text


async def unverified_gateway_entry(
    hass: HomeAssistant, tmp_path: Path, **data: Any
) -> MockConfigEntry:
    """A gateway entry whose pin was learned at first contact (or predates the record): not vouched for yet."""
    path = tmp_path / "JungHome.json"
    shutil.copy(SHARE_EXPORT_PATH, path)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={
            CONF_CDB_PATH: str(path),
            CONF_UNICAST: "0D00",
            CONF_SOURCE: "gateway",
            CONF_GATEWAY_SYNCED: export_digest(_read_json(SHARE_EXPORT_PATH)),
            **GATEWAY_DATA,
            CONF_GATEWAY_PIN_SOURCE: PIN_FROM_USER,
            **data,
        },
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
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
    return entry


async def test_the_first_link_vouches_for_a_pin_the_gateway_node_confirms(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The pin a user trusted at setup is compared with the node's report on the first link: equal, it is vouched
    for from then on (recorded, never asked again)."""
    mesh = gateway_node_says(
        answering_link,
        GATEWAY_DATA[CONF_GATEWAY_HOST].encode(),
        GATEWAY_DATA[CONF_GATEWAY_FINGERPRINT].upper().encode(),
    )
    entry = await unverified_gateway_entry(hass, tmp_path)
    assert (GATEWAY, 0xC003) in mesh.gets
    assert entry.data[CONF_GATEWAY_PIN_SOURCE] == PIN_FROM_MESH
    assert "confirmed the certificate pinned for the gateway" in caplog.text
    mesh.gets.clear()
    assert await hub_of(entry).async_gateway_distrust() is None
    assert mesh.gets == []


async def test_a_pin_the_gateway_node_contradicts_is_not_used(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    fast_sleep: list[float],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A LAN impostor pinned at setup: the node reports another certificate, so the gateway is never asked (no
    token, no export sent there) and the repair points to Reconfigure."""
    gateway_node_says(
        answering_link,
        GATEWAY_DATA[CONF_GATEWAY_HOST].encode(),
        NEW_FINGERPRINT.encode(),
    )
    entry = await unverified_gateway_entry(hass, tmp_path)
    assert entry.data[CONF_GATEWAY_PIN_SOURCE] == PIN_FROM_USER
    issue = find_issue(hass, ISSUE_GATEWAY_CERTIFICATE)
    assert issue is not None
    assert issue.translation_placeholders == {"host": GATEWAY_DATA[CONF_GATEWAY_HOST]}
    gw = GatewayAnswers(hass, entry, mock_bluetooth_env)
    with patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project):
        await gw.advert(
            network_id, NEW_MAC
        )  # no answer queued: asking would fail the refresh
    assert "export was not fetched for the unknown node(s)" in caplog.text
    assert (
        await hub_of(entry).async_gateway_distrust()
        == coordinator.GATEWAY_CERTIFICATE_CHANGED
    )


async def test_an_unconfirmed_pin_is_not_used_and_asked_again(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    answering_link: FakeProxyLink,
    fast_sleep: list[float],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The node does not answer: nothing is exchanged with the gateway (said so), the mesh works as ever, and the
    next use of the gateway asks again — once the node answers, the gateway is used."""
    gateway_node_says(
        answering_link,
        GATEWAY_DATA[CONF_GATEWAY_HOST].encode(),
        GATEWAY_DATA[CONF_GATEWAY_FINGERPRINT].encode(),
    )
    silent = patch.object(ExportWatch, "_gateway_text", AsyncMock(return_value=None))
    with silent:
        entry = await unverified_gateway_entry(hass, tmp_path)
    assert "has not confirmed the certificate pinned" in caplog.text
    assert entry.data[CONF_GATEWAY_PIN_SOURCE] == PIN_FROM_USER
    hub = hub_of(entry)
    gw = GatewayAnswers(hass, entry, mock_bluetooth_env)
    gw.answers.append(_read_json(SHARE_EXPORT_PATH))
    with patch.object(JungHomeGatewayApi, "fetch_project", gw.fetch_project):
        with silent:
            await gw.advert(network_id, NEW_MAC)
        assert gw.answers  # not asked
        assert "export was not fetched for the unknown node(s)" in caplog.text
        await gw.advert(network_id)
    assert not gw.answers
    assert entry.data[CONF_GATEWAY_PIN_SOURCE] == PIN_FROM_MESH
    # without a link, or without a gateway node, the node cannot be asked
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_GATEWAY_PIN_SOURCE: PIN_FROM_USER}
    )
    with patch.object(
        JungHomeHub, "connected", new_callable=PropertyMock, return_value=False
    ):
        assert await hub.async_gateway_distrust() == coordinator.GATEWAY_UNVERIFIED
