"""Adding a device from Home Assistant (review-3 N3): the `find_new_devices` / `add_device` actions, simulated."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome_ble import mesh_config, onboard
from custom_components.junghome_ble.actions import common
from custom_components.junghome_ble.configurator import nodes as nodes_mod
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_SOURCE,
    CONF_UNICAST,
    DOMAIN,
    ISSUE_VAULT_UNWRITABLE,
    OPTION_ALLOW_PROVISIONING,
    OPTION_PROVISIONER_IDENTITY,
    SERVICE_LINK_WAIT,
)
from custom_components.junghome_ble.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.junghome_ble.entity import mesh_identifier, node_identifier
from custom_components.junghome_ble.gateway_api import JungHomeGatewayApi
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.advert import JungAdvertisement
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.crypto import NetKeyMaterial
from custom_components.junghome_ble.jhmesh.devices import (
    Metadata,
    build_devices,
    element_group_address,
)
from custom_components.junghome_ble.jhmesh.export import (
    AllocationCrowded,
    ProjectFile,
    cdb_element_groups,
    suffixed_name,
)
from custom_components.junghome_ble.jhmesh.onboarding import (
    free_unicast_block,
    node_for,
)
from custom_components.junghome_ble.jhmesh.pdu import decode_opcode, encode_opcode
from custom_components.junghome_ble.jhmesh.provisioning import (
    DATA,
    MESH_PROVISIONING_SERVICE,
    ProvisioningData,
)
from custom_components.junghome_ble.jhmesh.vault import RefreshProgress
from custom_components.junghome_ble.services import CONFIGURATORS

from .conftest import (
    CDB_PATH,
    META_DIR,
    PROXY_NODE,
    SHARE_EXPORT_PATH,
    FakeProxyLink,
    make_service_info,
    settle,
    setup_entry,
    wait_for_link,
)
from .helpers import english, export_model_status, find_issue
from .jhmesh.conftest import FakeConfigServers, composition_params
from .jhmesh.test_provisioning import FakeDevice
from .test_services import GATEWAY_DATA

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from custom_components.junghome_ble.coordinator import JungHomeHub
    from custom_components.junghome_ble.jhmesh.cdb import Node

NEW_MAC = "00:00:5E:00:53:77"
NEW_UUID = bytes.fromhex("0000 5EFF FE00 5377 0000 0000 0000 0000".replace(" ", ""))
TEMPLATE = 0x0232  # "Living room DALI": the product the new device advertises

# a fresh node's Configuration Server answers a Set with its status: the request's fields after a status code, or
# the value it now holds
SET_STATUS = {
    C.CONFIG_APPKEY_ADD: (C.CONFIG_APPKEY_STATUS, True),
    C.CONFIG_MODEL_APP_BIND: (C.CONFIG_MODEL_APP_STATUS, True),
    C.CONFIG_MODEL_PUBLICATION_SET: (C.CONFIG_MODEL_PUBLICATION_STATUS, True),
    C.CONFIG_MODEL_SUBSCRIPTION_ADD: (C.CONFIG_MODEL_SUBSCRIPTION_STATUS, True),
    C.CONFIG_DEFAULT_TTL_SET: (C.CONFIG_DEFAULT_TTL_STATUS, False),
    C.CONFIG_RELAY_SET: (C.CONFIG_RELAY_STATUS, False),
    C.CONFIG_NETWORK_TRANSMIT_SET: (C.CONFIG_NETWORK_TRANSMIT_STATUS, False),
    C.CONFIG_BEACON_SET: (C.CONFIG_BEACON_STATUS, False),
    C.CONFIG_GATT_PROXY_SET: (C.CONFIG_GATT_PROXY_STATUS, False),
}


@contextmanager
def refused(
    kind: type[HomeAssistantError], key: str
) -> Iterator[pytest.ExceptionInfo[HomeAssistantError]]:
    """Expect `kind` raised with the translation key `key` (the message is the translated text)."""
    with pytest.raises(kind) as caught:
        yield caught
    assert caught.value.translation_key == key


class FreshNodes:
    """The new node's Configuration Server: Sets recorded and answered, Gets answered from what was recorded.

    Its Composition Data is the template's (`composition`: what another product would answer instead); a Config
    Node Reset is confirmed only with `confirm_reset` (silent otherwise: the device stays pending), and listed in
    `resets`.
    """

    def __init__(self, link: FakeProxyLink, template: int = TEMPLATE) -> None:
        self.servers = FakeConfigServers(link.cdb)
        self.refuse = False
        node = link.cdb.node_by_addr(template)
        assert node is not None
        self.composition = composition_params(node)
        self.confirm_reset = False
        self.resets: list[int] = []

    def __call__(self, node: int, access: bytes) -> bytes | None:
        op, _cid, p = decode_opcode(access)
        if op == C.CONFIG_COMPOSITION_DATA_GET:
            return encode_opcode(C.CONFIG_COMPOSITION_DATA_STATUS) + self.composition
        if op == C.CONFIG_NODE_RESET:
            self.resets.append(node)
            return (
                encode_opcode(C.CONFIG_NODE_RESET_STATUS)
                if self.confirm_reset
                else None
            )
        if op not in SET_STATUS:
            return self.servers(node, access)
        status, coded = SET_STATUS[op]
        element = int.from_bytes(p[:2], "little")
        if op == C.CONFIG_MODEL_APP_BIND:
            model = C.model_id_str(C.decode_model_id(p[4:]))
            self.servers.app_keys[element, model] = [0]
        elif op == C.CONFIG_MODEL_PUBLICATION_SET:
            model = C.model_id_str(C.decode_model_id(p[9:]))
            self.servers.publish[element, model] = int.from_bytes(p[2:4], "little")
        elif op == C.CONFIG_MODEL_SUBSCRIPTION_ADD:
            model = C.model_id_str(C.decode_model_id(p[4:]))
            self.servers.subscribe.setdefault((element, model), []).append(
                int.from_bytes(p[2:4], "little")
            )
        if not coded:
            return encode_opcode(status) + p
        code = b"\x05" if self.refuse else b"\x00"  # 0x05: Insufficient Resources
        body = p[:3] if op == C.CONFIG_APPKEY_ADD else p
        return encode_opcode(status) + code + body


def reset_and_answer(_node: int, access: bytes) -> bytes | None:
    """A node that confirms its Config Node Reset, and the others answering the unwiring."""
    op, _cid, params = decode_opcode(access)
    if op == C.CONFIG_NODE_RESET:
        return encode_opcode(C.CONFIG_NODE_RESET_STATUS)
    status, coded = SET_STATUS.get(op, (0, False))
    if not status:
        return None
    return encode_opcode(status) + (b"\x00" if coded else b"") + params


@pytest.fixture(autouse=True)
def quick_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new device that does not confirm its reset (`onboard._reset_new_node`) is given up in milliseconds."""
    monkeypatch.setattr(onboard, "NODE_RESET_TIMEOUT", 0.01)


@pytest.fixture
async def provisioning_entry(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> MockConfigEntry:
    """An entry with *Allow Home Assistant to add devices* on, over a copy of the fixture export it may write."""
    path = tmp_path / "JungHome.json"
    shutil.copy(SHARE_EXPORT_PATH, path)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={CONF_CDB_PATH: str(path), CONF_UNICAST: "0D00", "source": "upload"},
        options={OPTION_ALLOW_PROVISIONING: True},
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    return entry


def new_device_advert(
    hub: JungHomeHub, network_id: bytes, mac: str = NEW_MAC, uuid: bytes = NEW_UUID
) -> Any:
    """What the new device advertises: the provisioning service with its UUID, the JUNG record with its product."""
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    assert template.pid is not None
    info = make_service_info(
        network_id,
        address=mac,
        manufacturer_data={
            0x0527: bytes([1]) + template.pid.to_bytes(2, "little") + b"\x00\x00"
        },
    )
    info.service_data.clear()
    info.service_data[MESH_PROVISIONING_SERVICE] = uuid + b"\x00\x00"
    return info


async def test_find_new_devices_lists_the_jung_devices_not_in_a_mesh(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    hub = provisioning_entry.runtime_data
    info = new_device_advert(hub, network_id)
    foreign = make_service_info(network_id, address="00:00:5E:00:53:88")
    foreign.service_data.clear()
    foreign.service_data[MESH_PROVISIONING_SERVICE] = (
        NEW_UUID + b"\x00\x00"
    )  # no JUNG record
    mock_bluetooth_env["infos"].extend([info, foreign])
    found = await hass.services.async_call(
        DOMAIN, "find_new_devices", {}, blocking=True, return_response=True
    )
    assert found == {
        "devices": [
            {
                "address": NEW_MAC,
                "uuid": "00005EFF-FE00-5377-0000-000000000000",
                "product_id": hub.cdb.node_by_addr(TEMPLATE).pid,
                "rssi": -50,
            }
        ]
    }


@contextmanager
def vault_unwritable() -> Iterator[None]:
    """Every write of a vault store fails as on a read-only or full disk: HA's `Store` logs it and returns."""
    write = Store._async_write_data

    async def refuse_the_vault(store: Store[Any], data: dict[str, Any]) -> None:
        if store.key.startswith(f"{DOMAIN}.vault."):
            raise WriteError("read-only file system")
        await write(store, data)

    with patch.object(Store, "_async_write_data", refuse_the_vault):
        yield


def assert_element_groups_from_the_top(hub: JungHomeHub, node: Node) -> None:
    """A new node's element groups come from the top of the app's group range (C000..C64B), where the app allocates
    last: the app does not know them until it imports a file, and gives its next room the lowest free group (W4-2)."""
    own = {e.address for e in node.elements}
    groups = sorted(
        (
            g
            for e, g in cdb_element_groups(hub.cdb, hub.cdb.export_meta).items()
            if e in own
        ),
        reverse=True,
    )
    assert groups
    assert groups == list(range(0xC64B, 0xC64B - len(groups), -1))


async def test_add_device_provisions_commissions_and_records_it(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    network_id: bytes,
) -> None:
    """The whole path against simulated devices: the provisionee of the spec's protocol, the fresh node's
    Configuration Server; the export ends up with the node as it answered the read-back."""
    hub = provisioning_entry.runtime_data
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    device = FakeDevice(elements=len(template.elements), mtu_size=69)
    add = hub.proxy.add_node

    def add_to_both(
        node: Any,
    ) -> None:  # the fake mesh learns the new node's device key too
        add(node)
        fake_link.cdb.nodes.append(node)

    hub.proxy.add_node = add_to_both  # type: ignore[method-assign]
    fresh = FreshNodes(fake_link)
    fake_link.config_reply = fresh
    with (
        patch.object(onboard, "establish_connection", AsyncMock(return_value=device)),
        patch.object(onboard, "NODE_BOOT_DELAY", 0.0),
    ):
        result = await hass.services.async_call(
            DOMAIN,
            "add_device",
            {"address": NEW_MAC, "name": "Hall light"},
            blocking=True,
            return_response=True,
        )
    assert result is not None
    unicast = int(str(result["unicast"]), 16)
    assert result["elements"] == len(template.elements)
    assert result["template"] == f"{TEMPLATE:04X}"
    # the app's missing-devices check (F4-12): the template has no app device row in the share fixture, so the new
    # node gets one, where a push-button with a switch insert (what it advertised) is two devices
    assert result["missing_devices"] == {"recorded": 1, "expected": 2}
    assert device.data is not None
    assert device.data.unicast == unicast
    assert device.data.net_key == hub.proxy.nk.key
    await hass.async_block_till_done()
    await wait_for_link(hass, provisioning_entry)
    hub = provisioning_entry.runtime_data  # reloaded with the new node
    node = hub.cdb.node_by_addr(unicast)
    assert node is not None
    assert node.name == "Hall light"
    assert node.dev_key == device.device_key
    assert_element_groups_from_the_top(hub, node)
    assert fresh.servers.app_keys  # it was bound ...
    for (element, model), keys in fresh.servers.app_keys.items():
        el = hub.cdb.element(element)
        assert el is not None
        raw = next(m for m in el.raw_models if m["modelId"] == model)
        assert raw["bind"] == keys  # ... and the export says what it answered
    # the vault keeps the node as recorded (review-3 N1); the file has no provisioner entry of ours (option off)
    vault = hub.vault.vault
    assert vault is not None
    kept = vault.nodes[node.uuid]
    assert kept.recorded
    assert kept.dev_key == device.device_key
    assert [p.name for p in hub.cdb.provisioners] == ["iPhone"]
    # removed from the network: out of the vault too
    fake_link.config_reply = reset_and_answer
    await hass.services.async_call(
        DOMAIN,
        "remove_device",
        {"device": node_device_id(hass, hub, unicast), "confirm": True},
        blocking=True,
    )
    assert node.uuid not in vault.nodes


def test_the_template_is_a_node_with_the_advertised_insert_when_there_is_one() -> None:
    """F4-12: a push-button takes any insert; the export's node of the same product and insert is the better model."""
    cdb = CDB.load(Path(CDB_PATH))
    build_devices(
        cdb, Metadata(Path(META_DIR) / "device_metadata.json")
    )  # the cached InsertIds: 0148 switch, 0300 dimming

    def template(pid: int, function: int) -> int:
        return onboard._template(cdb, JungAdvertisement(1, pid, function, 0)).unicast

    assert template(0x0001, 2) == 0x0300  # the dimming one
    assert template(0x0001, 0) == 0x0148
    assert (
        template(0x0001, 5) == 0x0148
    )  # no blinds insert in the export: the first of the product
    assert template(0x0001, 0xFF) == 0x0148  # not an insert
    assert template(0x0003, 0) == 0x0172  # a socket has no insert to match


def test_the_advertised_layout_is_a_key_products_known_one() -> None:
    """Review-4 F4-6: what goes into the new node's `buttonLayoutExports` row instead of its template's."""
    assert onboard._advertised_layout(JungAdvertisement(1, 0x0002, 0, 5)) == 5
    assert onboard._advertised_layout(JungAdvertisement(1, 0x0002, 0, 9)) is None
    assert onboard._advertised_layout(JungAdvertisement(1, 0x0003, 0, 1)) is None


async def test_add_device_refusals(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    network_id: bytes,
) -> None:
    hub = provisioning_entry.runtime_data
    call = {"address": NEW_MAC, "name": "Hall light"}
    with refused(ServiceValidationError, "add_device_not_found"):
        await hass.services.async_call(DOMAIN, "add_device", call, blocking=True)
    info = new_device_advert(hub, network_id)
    mock_bluetooth_env["infos"].append(info)
    # a product the network has no device of: no template to copy
    record = info.manufacturer_data[0x0527]
    info.manufacturer_data[0x0527] = (
        record[:1] + (0x3F).to_bytes(2, "little") + record[3:]
    )
    with refused(ServiceValidationError, "add_device_no_template"):
        await hass.services.async_call(DOMAIN, "add_device", call, blocking=True)
    info.manufacturer_data.clear()
    with refused(ServiceValidationError, "add_device_not_jung"):
        await hass.services.async_call(DOMAIN, "add_device", call, blocking=True)
    mock_bluetooth_env["infos"][-1] = new_device_advert(hub, network_id)
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    with (
        patch.object(onboard, "free_unicast_block", return_value=None),
        refused(HomeAssistantError, "add_device_no_address"),
    ):
        await hass.services.async_call(DOMAIN, "add_device", call, blocking=True)
    # the top of the app's group range crowded by the app's own groups: refused before anything goes on air
    with (
        patch.object(
            onboard.commission,
            "plan",
            side_effect=AllocationCrowded("group address", 0xC040, 3),
        ),
        refused(HomeAssistantError, "add_device_groups_crowded"),
    ):
        await hass.services.async_call(DOMAIN, "add_device", call, blocking=True)
    with (
        patch.object(
            onboard, "establish_connection", AsyncMock(side_effect=OSError("busy"))
        ),
        refused(HomeAssistantError, "add_device_connect_failed"),
    ):
        await hass.services.async_call(DOMAIN, "add_device", call, blocking=True)
    # a device of another composition is refused before it learns an address
    device = FakeDevice(elements=len(template.elements) + 1, mtu_size=69)
    device.disconnect = AsyncMock(side_effect=OSError("gone"))  # type: ignore[method-assign]
    with (
        patch.object(onboard, "establish_connection", AsyncMock(return_value=device)),
        refused(HomeAssistantError, "add_device_provisioning_failed"),
    ):
        await hass.services.async_call(DOMAIN, "add_device", call, blocking=True)
    assert device.data is None
    # provisioned, then the node refuses its configuration
    add = hub.proxy.add_node

    def add_to_both(node: Any) -> None:
        add(node)
        fake_link.cdb.nodes.append(node)

    hub.proxy.add_node = add_to_both  # type: ignore[method-assign]
    fresh = FreshNodes(fake_link)
    fresh.refuse = True
    fake_link.config_reply = fresh
    with (
        patch.object(
            onboard,
            "establish_connection",
            AsyncMock(
                return_value=FakeDevice(elements=len(template.elements), mtu_size=69)
            ),
        ),
        patch.object(onboard, "NODE_BOOT_DELAY", 0.0),
        refused(HomeAssistantError, "add_device_commissioning_failed"),
    ):
        await hass.services.async_call(DOMAIN, "add_device", call, blocking=True)
    # provisioned but not recorded: its device key is kept all the same (pending)
    vault = hub.vault.vault
    assert vault is not None
    assert [n.uuid for n in vault.pending] == ["00005EFF-FE00-5377-0000-000000000000"]


async def test_add_device_needs_the_option(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
) -> None:
    hass.config_entries.async_update_entry(
        provisioning_entry, options={OPTION_ALLOW_PROVISIONING: False}
    )
    await hass.async_block_till_done()
    await wait_for_link(hass, provisioning_entry)
    with refused(ServiceValidationError, "add_device_not_allowed"):
        await hass.services.async_call(
            DOMAIN,
            "add_device",
            {"address": NEW_MAC, "name": "x"},
            blocking=True,
        )


def node_device_id(hass: HomeAssistant, hub: JungHomeHub, unicast: int) -> str:
    """The registry id of the node device of `unicast`."""
    node = hub.cdb.node_by_addr(unicast)
    assert node is not None
    return hub.device_ids[node_identifier(node)]


async def test_remove_device_resets_it_and_takes_it_out(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Review-3 N4: Config Node Reset first; once the node confirmed, the others' wiring to it goes and the file
    records it excluded (handed on like any change); the entry reloads without it."""
    hub = provisioning_entry.runtime_data
    resets: list[int] = []

    def answer(node: int, access: bytes) -> bytes | None:
        op, _cid, params = decode_opcode(access)
        if op == C.CONFIG_NODE_RESET:
            resets.append(node)
            return encode_opcode(C.CONFIG_NODE_RESET_STATUS)
        status, coded = SET_STATUS.get(op, (0, False))
        if not status:
            return None
        return encode_opcode(status) + (b"\x00" if coded else b"") + params

    fake_link.config_reply = answer
    device = node_device_id(hass, hub, 0x0300)
    await hass.services.async_call(
        DOMAIN, "remove_device", {"device": device, "confirm": True}, blocking=True
    )
    assert resets == [0x0300]
    await hass.async_block_till_done()
    await wait_for_link(hass, provisioning_entry)
    hub = provisioning_entry.runtime_data
    assert hub.cdb.node_by_addr(0x0300) is None
    assert {0x0300, 0x0301} <= hub.cdb.excluded_addresses


async def test_remove_device_needs_confirm_and_a_dry_run_sends_nothing(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Review-4 W I9: a removal cannot be undone, so it needs `confirm: true`; a dry run needs none and answers
    the reset and the others' unwiring without sending either, the export and the vault untouched."""
    hub = provisioning_entry.runtime_data
    sent: list[bytes] = []
    fake_link.config_reply = lambda _node, access: sent.append(access)  # type: ignore[func-returns-value]
    device = node_device_id(hass, hub, 0x0300)
    path = Path(provisioning_entry.data[CONF_CDB_PATH])
    before = await hass.async_add_executor_job(path.read_bytes)
    with refused(ServiceValidationError, "remove_device_needs_confirm"):
        await hass.services.async_call(
            DOMAIN, "remove_device", {"device": device}, blocking=True
        )
    response = await hass.services.async_call(
        DOMAIN,
        "remove_device",
        {"device": device, "dry_run": True},
        blocking=True,
        return_response=True,
    )
    assert isinstance(response, dict)
    assert response["dry_run"] is True
    assert response["steps"][0].endswith(": Config Node Reset")
    assert any(d["path"].startswith("network.nodes[") for d in response["diff"])
    assert sent == []
    assert await hass.async_add_executor_job(path.read_bytes) == before
    assert provisioning_entry.state is ConfigEntryState.LOADED
    assert provisioning_entry.runtime_data is hub  # no reload
    assert hub.cdb.node_by_addr(0x0300) is not None


async def test_remove_device_refusals(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    fake_link: FakeProxyLink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = provisioning_entry.runtime_data
    monkeypatch.setattr(nodes_mod, "NODE_RESET_TIMEOUT", 0.01)
    monkeypatch.setattr(nodes_mod, "RESET_ADVERT_WAIT", 0.01)
    fake_link.config_reply = lambda _node, _access: None  # nobody answers
    with refused(HomeAssistantError, "remove_device_unconfirmed"):
        await hass.services.async_call(
            DOMAIN,
            "remove_device",
            {"device": node_device_id(hass, hub, 0x0300), "confirm": True},
            blocking=True,
        )
    assert hub.cdb.node_by_addr(0x0300) is not None  # nothing changed
    configurator = hass.data[CONFIGURATORS][provisioning_entry.entry_id]
    with refused(ServiceValidationError, "service_unknown_element"):
        await configurator.remove_node(
            0x0301
        )  # a secondary element: not a node's address
    with refused(ServiceValidationError, "remove_device_gateway"):
        await hass.services.async_call(
            DOMAIN,
            "remove_device",
            {"device": node_device_id(hass, hub, 0x00DC), "confirm": True},
            blocking=True,
        )
    with refused(ServiceValidationError, "remove_device_mesh"):
        await hass.services.async_call(
            DOMAIN,
            "remove_device",
            {"device": hub.device_ids[mesh_identifier(hub)], "confirm": True},
            blocking=True,
        )
    # gone for good: `force` records the removal all the same
    await hass.services.async_call(
        DOMAIN,
        "remove_device",
        {"device": node_device_id(hass, hub, 0x0300), "confirm": True, "force": True},
        blocking=True,
    )
    await hass.async_block_till_done()
    await wait_for_link(hass, provisioning_entry)
    assert provisioning_entry.runtime_data.cdb.node_by_addr(0x0300) is None
    # without the option: refused
    hass.config_entries.async_update_entry(
        provisioning_entry, options={OPTION_ALLOW_PROVISIONING: False}
    )
    await hass.async_block_till_done()
    await wait_for_link(hass, provisioning_entry)
    hub = provisioning_entry.runtime_data
    with refused(ServiceValidationError, "add_device_not_allowed"):
        await hass.services.async_call(
            DOMAIN,
            "remove_device",
            {"device": node_device_id(hass, hub, 0x0400), "confirm": True},
            blocking=True,
        )


def reset_advert(hub: JungHomeHub, network_id: bytes, unicast: int) -> Any:
    """What node `unicast` advertises once reset: the provisioning service with its own Device UUID."""
    node = hub.cdb.node_by_addr(unicast)
    assert node is not None
    return new_device_advert(
        hub, network_id, uuid=bytes.fromhex(node.uuid.replace("-", ""))
    )


@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
async def test_an_unconfirmed_reset_is_looked_for_among_new_devices(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    network_id: bytes,
    monkeypatch: pytest.MonkeyPatch,
    stale: bool,
) -> None:
    """Review-4 W4-7: a node can take the reset and lose its status. Advertising as a new device after the reset,
    it is recorded as removed; one that already advertised so before it (a scanner's stale advert data) proves
    nothing, and the error says it may have been reset."""
    monkeypatch.setattr(nodes_mod, "NODE_RESET_TIMEOUT", 0.01)
    monkeypatch.setattr(nodes_mod, "RESET_ADVERT_WAIT", 0.05)
    monkeypatch.setattr(nodes_mod, "RESET_ADVERT_POLL", 0.01)
    hub = provisioning_entry.runtime_data
    advert = reset_advert(hub, network_id, 0x0300)
    if stale:
        mock_bluetooth_env["infos"].append(advert)

    def reset_lost(node: int, access: bytes) -> bytes | None:
        op, _cid, _params = decode_opcode(access)
        if op == C.CONFIG_NODE_RESET:
            if advert not in mock_bluetooth_env["infos"]:
                mock_bluetooth_env["infos"].append(advert)  # it took the reset ...
            return None  # ... and its status is lost
        return reset_and_answer(node, access)

    fake_link.config_reply = reset_lost
    device = node_device_id(hass, hub, 0x0300)
    path = Path(provisioning_entry.data[CONF_CDB_PATH])
    if stale:
        with refused(HomeAssistantError, "remove_device_unconfirmed") as caught:
            await hass.services.async_call(
                DOMAIN,
                "remove_device",
                {"device": device, "confirm": True},
                blocking=True,
            )
        assert caught.value.translation_placeholders == {"address": "0300"}
        assert "may have been reset" in str(caught.value)
        assert ProjectFile.load(path).cdb.node_by_addr(0x0300) is not None
        return
    await hass.services.async_call(
        DOMAIN, "remove_device", {"device": device, "confirm": True}, blocking=True
    )
    await hass.async_block_till_done()
    await wait_for_link(hass, provisioning_entry)
    pf = ProjectFile.load(path)
    assert pf.cdb.node_by_addr(0x0300) is None
    assert {0x0300, 0x0301} <= pf.cdb.excluded_addresses


async def test_the_node_carrying_the_link_is_removed_only_with_force(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Review-4 W4-7: its reset ends the link its confirmation would come back on — refused without `force`,
    nothing sent. With `force` the link lost on its reset is no "nothing changed": the removal is recorded and
    the others' wiring goes once a link is up."""
    path = Path(provisioning_entry.data[CONF_CDB_PATH])
    pf = ProjectFile.load(path)
    key = pf.cdb.element(0x0301)
    assert key is not None
    pf.set_publication(
        key.node, key, "1001", PROXY_NODE
    )  # a key switching the proxy node's load
    pf.save()
    await hass.config_entries.async_reload(provisioning_entry.entry_id)
    await wait_for_link(hass, provisioning_entry)
    await settle(hass)
    hub = provisioning_entry.runtime_data
    proxy = hub.proxy_node
    assert proxy == PROXY_NODE
    device = node_device_id(hass, hub, proxy)
    sent: list[int] = []

    def answer(node: int, access: bytes) -> bytes | None:
        sent.append(node)
        return reset_and_answer(node, access)

    fake_link.config_reply = answer
    with refused(ServiceValidationError, "remove_device_proxy") as caught:
        await hass.services.async_call(
            DOMAIN, "remove_device", {"device": device, "confirm": True}, blocking=True
        )
    assert caught.value.translation_placeholders == {"address": f"{proxy:04X}"}
    assert sent == []
    request = hub.proxy.request_config

    async def link_lost_on_reset(
        node: int, pdu: bytes, *args: Any, **kwargs: Any
    ) -> Any:
        if pdu == C.node_reset():
            raise ConnectionError("the proxy went away")
        return await request(node, pdu, *args, **kwargs)

    waits: list[float] = []
    wait_connected = hub.async_wait_connected

    async def wait_for_the_next_link(timeout: float) -> bool:
        waits.append(timeout)
        return await wait_connected(timeout)

    with (
        patch.object(hub.proxy, "request_config", link_lost_on_reset),
        patch.object(hub, "async_wait_connected", wait_for_the_next_link),
    ):
        await hass.services.async_call(
            DOMAIN,
            "remove_device",
            {"device": device, "confirm": True, "force": True},
            blocking=True,
        )
    assert SERVICE_LINK_WAIT in waits  # before the unwiring
    assert sent == [0x0300]  # the key's publication to it, taken away
    await hass.async_block_till_done()
    await wait_for_link(hass, provisioning_entry)
    pf = ProjectFile.load(path)
    assert pf.cdb.node_by_addr(proxy) is None
    assert proxy in pf.cdb.excluded_addresses
    assert pf.publication(0x0301, "1001") is None


async def test_add_device_with_the_provisioner_identity_uses_home_assistants_ranges(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    network_id: bytes,
) -> None:
    """Review-3 N1: with the option on, the new node and its element groups go into Home Assistant's own ranges,
    the file gets Home Assistant's provisioner entry and node, and the entry still sets up with its address in
    it (review-3 W3); removing the node drops it from the vault."""
    hass.config_entries.async_update_entry(
        provisioning_entry,
        options={OPTION_ALLOW_PROVISIONING: True, OPTION_PROVISIONER_IDENTITY: True},
    )
    await hass.async_block_till_done()
    await wait_for_link(hass, provisioning_entry)
    hub = provisioning_entry.runtime_data
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    device = FakeDevice(elements=len(template.elements), mtu_size=69)
    add = hub.proxy.add_node

    def add_to_both(node: Any) -> None:
        add(node)
        fake_link.cdb.nodes.append(node)

    hub.proxy.add_node = add_to_both  # type: ignore[method-assign]
    fake_link.config_reply = FreshNodes(fake_link)
    with (
        patch.object(onboard, "establish_connection", AsyncMock(return_value=device)),
        patch.object(onboard, "NODE_BOOT_DELAY", 0.0),
    ):
        result = await hass.services.async_call(
            DOMAIN,
            "add_device",
            {"address": NEW_MAC, "name": "Hall light"},
            blocking=True,
            return_response=True,
        )
    assert result is not None
    unicast = int(str(result["unicast"]), 16)
    await hass.async_block_till_done()
    await wait_for_link(hass, provisioning_entry)
    assert (
        provisioning_entry.state is ConfigEntryState.LOADED
    )  # our own node at our address is no clash
    hub = provisioning_entry.runtime_data
    vault = hub.vault.vault
    assert vault is not None
    assert vault.ranges is not None
    low, high = vault.ranges.unicast
    assert low <= 0x0D00 < unicast <= high  # inside our range, above our own address
    ours = hub.cdb.own_provisioner(vault.uuid)
    assert ours is not None
    assert hub.cdb.provisioners[-1] is ours
    assert ours.unicast == [vault.ranges.unicast]
    node = hub.cdb.node_by_addr(unicast)
    assert node is not None
    groups = [
        a for a, name in hub.cdb.groups.items() if name.endswith(f"#0x{unicast:X}")
    ]
    assert groups
    assert all(vault.ranges.group[0] <= a <= vault.ranges.group[1] for a in groups)
    assert vault.nodes[node.uuid].recorded
    # removed again: out of the vault too (the export's excluded entry already tells the merge so)
    fake_link.config_reply = reset_and_answer
    await hass.services.async_call(
        DOMAIN,
        "remove_device",
        {"device": node_device_id(hass, hub, unicast), "confirm": True},
        blocking=True,
    )
    assert node.uuid not in vault.nodes


ACTUATOR = 0x0400  # "2-channel actuator": nothing is wired to it in the fixture export


async def wired_to_the_actuator(hass: HomeAssistant, entry: MockConfigEntry) -> Path:
    """Make keys 0149 (node 0148) and 0301 (node 0300) publish to the actuator, reload; return the export's path."""
    path = Path(entry.data[CONF_CDB_PATH])
    pf = ProjectFile.load(path)
    for key in (0x0149, 0x0301):
        element = pf.cdb.element(key)
        assert element is not None
        pf.set_publication(element.node, element, "1001", ACTUATOR)
    pf.save()
    await hass.config_entries.async_reload(entry.entry_id)
    await wait_for_link(hass, entry)
    await settle(hass)
    return path


@pytest.mark.parametrize(
    ("refusing", "accepted"), [(0x0148, 0), (0x0300, 1)], ids=["first", "second"]
)
async def test_a_removal_whose_unwiring_stops_still_records_the_reset_node(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    fake_link: FakeProxyLink,
    refusing: int,
    accepted: int,
) -> None:
    """The reset cannot be taken back: when a message taking the others' wiring away then fails — the first one
    too — the export still records the node as removed (excluded, its addresses in `networkExclusions`, out of
    the vault) with what the others accepted, the error says so, and the entry reloads without it before the
    call returns."""
    path = await wired_to_the_actuator(hass, provisioning_entry)
    hub = provisioning_entry.runtime_data
    actuator = hub.cdb.node_by_addr(ACTUATOR)
    assert actuator is not None
    vault = hub.vault.identity()
    vault.remember_provisioned(
        actuator.uuid, ACTUATOR, len(actuator.elements), bytes(16)
    )
    unwired: list[int] = []

    def answer(node: int, access: bytes) -> bytes | None:
        if (held := export_model_status(path, access)) is not None:
            return held  # the pre-flight reads before the reset: the others hold what the export says
        op, _cid, params = decode_opcode(access)
        if op == C.CONFIG_NODE_RESET:
            return encode_opcode(C.CONFIG_NODE_RESET_STATUS)
        assert op == C.CONFIG_MODEL_PUBLICATION_SET
        unwired.append(node)
        code = b"\x05" if node == refusing else b"\x00"  # 0x05: Insufficient Resources
        return encode_opcode(C.CONFIG_MODEL_PUBLICATION_STATUS) + code + params

    fake_link.config_reply = answer
    with refused(HomeAssistantError, "service_config_refused") as caught:
        await hass.services.async_call(
            DOMAIN,
            "remove_device",
            {"device": node_device_id(hass, hub, ACTUATOR), "confirm": True},
            blocking=True,
        )
    assert unwired == [0x0148, 0x0300][: accepted + 1]
    placeholders = caught.value.translation_placeholders
    assert placeholders["applied"] == english(
        mesh_config.applied_removed(ACTUATOR, accepted, 2)
    )
    pf = ProjectFile.load(path)
    assert pf.cdb.node_by_addr(ACTUATOR) is None
    assert {ACTUATOR, ACTUATOR + 1} <= pf.cdb.excluded_addresses
    # an accepted message is recorded as unwired; a refused or unsent one is still wired on its node
    assert pf.publication(0x0149, "1001") == (None if accepted else ACTUATOR)
    assert pf.publication(0x0301, "1001") == ACTUATOR
    assert actuator.uuid not in vault.nodes
    # the device model followed before the call returned
    reloaded = provisioning_entry.runtime_data
    assert reloaded is not hub
    assert reloaded.cdb.node_by_addr(ACTUATOR) is None


async def test_a_lost_link_during_the_reset_is_a_translated_error(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
) -> None:
    hub = provisioning_entry.runtime_data
    with (
        patch.object(
            hub.proxy, "request_config", AsyncMock(side_effect=ConnectionError("gone"))
        ),
        refused(HomeAssistantError, "service_send_failed") as caught,
    ):
        await hass.services.async_call(
            DOMAIN,
            "remove_device",
            {"device": node_device_id(hass, hub, 0x0300), "confirm": True},
            blocking=True,
        )
    assert caught.value.translation_placeholders == {
        "node": "0300",
        "message": "Config Node Reset",
        "applied": english(mesh_config.APPLIED_NOTHING),
    }
    assert ProjectFile.load(
        Path(provisioning_entry.data[CONF_CDB_PATH])
    ).cdb.node_by_addr(0x0300)


async def test_adding_and_removing_run_on_the_hub_the_lock_hands_them(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
) -> None:
    """A call that waited for the entry's lock while a previous call's reload replaced the hub runs on the new
    hub and its configurator, once its link is up — not on the ones it saw when it was made."""
    entry_id = provisioning_entry.entry_id
    real_lock = common._lock
    current: list[bool] = []

    @asynccontextmanager
    async def lock_after_a_reload(
        hass: HomeAssistant, entry_id: str
    ) -> AsyncIterator[None]:
        # the reload of the call before this one, landing while this one waits for the lock
        await hass.config_entries.async_reload(entry_id)
        async with real_lock(hass, entry_id):
            yield

    def is_current(
        hub: JungHomeHub, configurator: mesh_config.MeshConfigurator
    ) -> None:
        current.append(
            hub is provisioning_entry.runtime_data
            and configurator is hass.data[CONFIGURATORS][entry_id]
            and configurator.hub is hub
            and hub.connected
        )

    async def add(
        _hass: HomeAssistant,
        hub: JungHomeHub,
        configurator: mesh_config.MeshConfigurator,
        _address: str,
        _name: str,
        _static_oob: bytes | None,
    ) -> dict[str, Any]:
        is_current(hub, configurator)
        return {"unicast": "0D20"}

    async def remove(
        configurator: mesh_config.MeshConfigurator,
        _unicast: int,
        *,
        force: bool = False,
    ) -> bool:
        is_current(configurator.hub, configurator)
        return False

    device = node_device_id(hass, provisioning_entry.runtime_data, 0x0300)
    with (
        patch.object(common, "_lock", lock_after_a_reload),
        patch.object(onboard, "async_add_device", add),
        patch.object(mesh_config.MeshConfigurator, "remove_node", remove),
    ):
        result = await hass.services.async_call(
            DOMAIN,
            "add_device",
            {"address": NEW_MAC, "name": "Hall light"},
            blocking=True,
            return_response=True,
        )
        await hass.services.async_call(
            DOMAIN, "remove_device", {"device": device, "confirm": True}, blocking=True
        )
    assert result == {"unicast": "0D20"}
    assert current == [True, True]
    await wait_for_link(hass, provisioning_entry)


async def test_add_device_plans_on_the_gateways_newer_export(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    network_id: bytes,
) -> None:
    """The app changed the network since the last reload (here: it excluded the very addresses Home Assistant would
    have given the device) and uploaded that to the gateway: the device's addresses are planned on the gateway's
    export, adopted first, not on the export the hub loaded — they reach the device long before it is recorded."""
    hass.config_entries.async_update_entry(
        provisioning_entry,
        data={**provisioning_entry.data, **GATEWAY_DATA, CONF_SOURCE: "gateway"},
    )
    await hass.async_block_till_done()
    await wait_for_link(hass, provisioning_entry)
    await settle(hass)
    hub = provisioning_entry.runtime_data
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    count = len(template.elements)
    stale = free_unicast_block(
        hub.cdb, count, avoid=[hub.proxy.state.src], own=hub.vault.own_uuid
    )
    assert stale is not None
    newer = ProjectFile.load(Path(provisioning_entry.data[CONF_CDB_PATH]))
    newer.net.setdefault("networkExclusions", []).append(
        {
            "ivIndex": hub.proxy.state.iv_index,
            "addresses": [f"{a:04X}" for a in range(stale, stale + count)],
        }
    )
    newer.touch(datetime.now(UTC) + timedelta(minutes=1))
    held = json.loads(newer.share_json())
    uploads: list[dict[str, Any]] = []

    async def fetch_project(self: JungHomeGatewayApi) -> dict[str, Any]:
        return held

    async def upload_project(self: JungHomeGatewayApi, export: dict[str, Any]) -> None:
        uploads.append(export)

    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    device = FakeDevice(elements=count, mtu_size=69)
    add = hub.proxy.add_node

    def add_to_both(node: Any) -> None:
        add(node)
        fake_link.cdb.nodes.append(node)

    hub.proxy.add_node = add_to_both  # type: ignore[method-assign]
    fake_link.config_reply = FreshNodes(fake_link)
    with (
        patch.object(JungHomeGatewayApi, "fetch_project", fetch_project),
        patch.object(JungHomeGatewayApi, "upload_project", upload_project),
        patch.object(onboard, "establish_connection", AsyncMock(return_value=device)),
        patch.object(onboard, "NODE_BOOT_DELAY", 0.0),
    ):
        result = await hass.services.async_call(
            DOMAIN,
            "add_device",
            {"address": NEW_MAC, "name": "Hall light"},
            blocking=True,
            return_response=True,
        )
    assert result is not None
    unicast = int(str(result["unicast"]), 16)
    assert unicast + count <= stale  # below the block the app excluded
    assert device.data is not None
    assert device.data.unicast == unicast
    assert uploads  # the record went to the gateway as after any change
    await wait_for_link(hass, provisioning_entry)
    hub = provisioning_entry.runtime_data
    # the app's change is kept
    assert set(range(stale, stale + count)) <= hub.cdb.excluded_addresses
    node = hub.cdb.node_by_addr(unicast)
    assert node is not None
    assert node.name == "Hall light"


# ----------------------------------------------------------------------------- pending nodes (review-4 D2, P4-6, W4-11)

SECOND_MAC = "00:00:5E:00:53:78"
SECOND_UUID = bytes.fromhex("00005EFFFE0053780000000000000000")
PENDING = 0x7FF0  # a pending node's primary address in the tests that put one into the vault themselves
PENDING_UUID = "00005EFF-FE00-5399-0000-000000000000"
PENDING_KEY = bytes(range(0x60, 0x70))


class RefusingFrom(FreshNodes):
    """A fresh node that refuses (where a status code says so) every Set from the `k`-th one on."""

    def __init__(self, link: FakeProxyLink, k: int) -> None:
        super().__init__(link)
        self.k = k
        self.sets = 0

    def __call__(self, node: int, access: bytes) -> bytes | None:
        if decode_opcode(access)[0] in SET_STATUS:
            self.sets += 1
            self.refuse = self.sets >= self.k
        return super().__call__(node, access)


def learn_new_nodes(hub: JungHomeHub, fake_link: FakeProxyLink) -> list[dict[int, Any]]:
    """Let the fake mesh learn each node the hub makes known; return the replay list as it was at each such call."""
    add = hub.proxy.add_node
    seen: list[dict[int, Any]] = []

    def add_to_both(node: Any) -> None:
        seen.append(dict(hub.proxy.state.rpl))
        add(node)
        fake_link.cdb.nodes.append(node)

    hub.proxy.add_node = add_to_both  # type: ignore[method-assign]
    return seen


@pytest.mark.parametrize("earlier", [False, True], ids=["new", "pending_before"])
async def test_add_device_stops_before_the_data_when_the_vault_cannot_be_written(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
    earlier: bool,
) -> None:
    """Review-4 D15: the device key is on disk before the device gets its Provisioning Data. A vault write that
    fails (HA's `Store` only logs it) stops the provisioning right there: the device learns nothing, nothing is
    kept or reserved for it (a record of an earlier attempt stays as it was), the repair names its address (never
    a key), and the next save that lands clears it."""
    hub = provisioning_entry.runtime_data
    before = (
        hub.vault.identity().remember_provisioned(
            "00005EFF-FE00-5377-0000-000000000000", 0x7F00, 1, bytes(16)
        )
        if earlier
        else None
    )
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    device = FakeDevice(elements=len(template.elements), mtu_size=69)
    with (
        vault_unwritable(),
        patch.object(onboard, "establish_connection", AsyncMock(return_value=device)),
        refused(HomeAssistantError, "add_device_vault_unwritable") as caught,
    ):
        await hass.services.async_call(
            DOMAIN,
            "add_device",
            {"address": NEW_MAC, "name": "Hall light"},
            blocking=True,
        )
    address = caught.value.translation_placeholders["unicast"]
    assert caught.value.translation_placeholders["error"] == "read-only file system"
    assert DATA not in [
        p[0] for p in device.received
    ]  # neither the NetKey nor an address left
    assert device.data is None
    vault = hub.vault.vault
    assert vault is not None
    assert vault.nodes == ({} if before is None else {before.uuid: before})
    if before is None:
        assert pending_issue(hass, provisioning_entry) is None
    issue = find_issue(hass, ISSUE_VAULT_UNWRITABLE)
    assert issue is not None
    assert issue.translation_placeholders["address"] == address
    assert issue.translation_placeholders["error"] == "read-only file system"
    ours = [r.getMessage() for r in caplog.records if r.name == onboard.__name__]
    assert any(f"new device at {address} could not be written" in m for m in ours)
    assert not any(re.search("[0-9A-Fa-f]{32}", m) for m in ours)  # no key
    # writable again: the next save lands (the identity begun for the device) and the repair goes
    assert await hub.vault.async_save()
    assert find_issue(hass, ISSUE_VAULT_UNWRITABLE) is None


async def test_a_provisioning_not_confirmed_after_the_data_keeps_the_device_pending(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    network_id: bytes,
) -> None:
    """Once the Data PDU went out, a failure does not prove the device has nothing: it is sent a reset under the
    key kept for it (the mesh does not know that key: this device never took its data, so nothing answers), and
    the record kept before it stays pending, so its addresses stay reserved and `reset_pending_device` can reach
    it."""
    fake_link.expect_undecryptable = True
    hub = provisioning_entry.runtime_data
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    device = FakeDevice(elements=len(template.elements), mtu_size=69)
    device.fail_on = (DATA, 0x06)  # Decryption Failed, in answer to the Data PDU
    with (
        patch.object(onboard, "establish_connection", AsyncMock(return_value=device)),
        refused(HomeAssistantError, "add_device_provisioning_unconfirmed") as caught,
    ):
        await hass.services.async_call(
            DOMAIN,
            "add_device",
            {"address": NEW_MAC, "name": "Hall light"},
            blocking=True,
        )
    address = caught.value.translation_placeholders["unicast"]
    vault = hub.vault.vault
    assert vault is not None
    assert [f"{n.unicast:04X}" for n in vault.pending] == [address]
    issue = pending_issue(hass, provisioning_entry)
    assert issue is not None
    assert issue.translation_placeholders["addresses"] == address


def pending_issue(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"pending_device_{entry.entry_id}"
    )


@pytest.mark.parametrize("k", [1, 6], ids=["first_step", "a_later_step"])
async def test_a_pending_node_keeps_its_addresses_and_element_groups(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    network_id: bytes,
    k: int,
) -> None:
    """The reviewers' repro: device A is provisioned, then its commissioning is refused at step k; device B added
    next gets other addresses and other element groups (before, both got the same block and groups). What the
    replay list held for A's new addresses (a node reset since) is gone before A answers anything, A is named by
    the repair issue, and B's name, another device's already, is numbered as the app numbers it."""
    hub = provisioning_entry.runtime_data
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    count = len(template.elements)
    rpl_at_add = learn_new_nodes(hub, fake_link)
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    expected = free_unicast_block(
        hub.cdb, count, avoid=[hub.proxy.state.src], own=hub.vault.own_uuid
    )
    assert expected is not None
    stale = (
        hub.proxy.state.iv_index,
        0xFFFFFF,
    )  # far past any number the fake mesh sends
    for address in range(expected, expected + count):
        hub.proxy.state.rpl[address] = stale
    fake_link.config_reply = RefusingFrom(fake_link, k)
    with (
        patch.object(
            onboard,
            "establish_connection",
            AsyncMock(return_value=FakeDevice(elements=count, mtu_size=69)),
        ),
        patch.object(onboard, "NODE_BOOT_DELAY", 0.0),
        refused(HomeAssistantError, "add_device_commissioning_failed") as caught,
    ):
        await hass.services.async_call(
            DOMAIN,
            "add_device",
            {"address": NEW_MAC, "name": "Hall light"},
            blocking=True,
        )
    placeholders = caught.value.translation_placeholders
    first = int(placeholders["unicast"], 16)
    assert first == expected
    assert "refused" in placeholders["error"]  # A's answers got through
    assert not set(range(first, first + count)) & rpl_at_add[0].keys()
    vault = hub.vault.vault
    assert vault is not None
    (pending,) = vault.pending
    first_groups = {address for address, _name in pending.groups}
    assert first_groups  # the planned element groups are kept with it
    issue = pending_issue(hass, provisioning_entry)
    assert issue is not None
    assert issue.translation_placeholders["addresses"] == f"{first:04X}"
    # device B
    taken = ProjectFile.load(
        Path(provisioning_entry.data[CONF_CDB_PATH])
    ).device_names()
    mock_bluetooth_env["infos"].append(
        new_device_advert(hub, network_id, SECOND_MAC, SECOND_UUID)
    )
    fake_link.config_reply = FreshNodes(fake_link)
    with (
        patch.object(
            onboard,
            "establish_connection",
            AsyncMock(return_value=FakeDevice(elements=count, mtu_size=69)),
        ),
        patch.object(onboard, "NODE_BOOT_DELAY", 0.0),
    ):
        result = await hass.services.async_call(
            DOMAIN,
            "add_device",
            {"address": SECOND_MAC, "name": taken[0].upper()},
            blocking=True,
            return_response=True,
        )
    assert result is not None
    assert result["name"] == suffixed_name(taken[0].upper(), taken)
    second = int(str(result["unicast"]), 16)
    assert not set(range(second, second + count)) & set(range(first, first + count))
    await hass.async_block_till_done()
    await wait_for_link(hass, provisioning_entry)
    hub = provisioning_entry.runtime_data
    node = hub.cdb.node_by_addr(second)
    assert node is not None
    own = {e.address for e in node.elements}
    second_groups = {
        a for a, name in hub.cdb.groups.items() if element_group_address(name) in own
    }
    assert second_groups
    assert not second_groups & first_groups
    # A is still pending after the reload: the issue stays
    issue = pending_issue(hass, provisioning_entry)
    assert issue is not None
    assert issue.translation_placeholders["addresses"] == f"{first:04X}"


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("  ", "service_name_blank"),
        ("50% off", "service_name_not_allowed"),
        ("x" * 31, "add_device_name_too_long"),
    ],
)
async def test_add_device_refuses_a_name_before_anything_goes_on_air(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    name: str,
    key: str,
) -> None:
    hub = provisioning_entry.runtime_data
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    connect = AsyncMock()
    with (
        patch.object(onboard, "establish_connection", connect),
        refused(ServiceValidationError, key),
    ):
        await hass.services.async_call(
            DOMAIN, "add_device", {"address": NEW_MAC, "name": name}, blocking=True
        )
    connect.assert_not_awaited()


@pytest.mark.parametrize("missing", ["beacon", "iv_index"])
async def test_add_device_needs_an_iv_index_a_beacon_confirmed_on_this_link(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
    missing: str,
) -> None:
    """Review-4 P4-6: the device keeps the IV index of its Provisioning Data; one no beacon confirmed on the
    current link (a stored state from before an IV Update) is not handed out."""
    hub = provisioning_entry.runtime_data
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    if missing == "beacon":
        hub.proxy._beacon_seen.clear()  # a link no beacon arrived on yet
    else:
        hub.proxy.state.iv_known = False
    connect = AsyncMock()
    try:
        with (
            patch.object(onboard, "establish_connection", connect),
            refused(ServiceValidationError, "add_device_no_beacon"),
        ):
            await hass.services.async_call(
                DOMAIN,
                "add_device",
                {"address": NEW_MAC, "name": "Hall light"},
                blocking=True,
            )
    finally:
        hub.proxy.state.iv_known = True
    connect.assert_not_awaited()
    assert hub.vault.vault is None


def put_pending(
    hub: JungHomeHub, fake_link: FakeProxyLink, unicast: int = PENDING
) -> None:
    """A device provisioned at `unicast` and never recorded: in the vault, and holding its key on air."""
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    count = len(template.elements)
    hub.vault.identity().remember_provisioned(
        PENDING_UUID, unicast, count, PENDING_KEY, [(0xC0F0, "element group")]
    )
    fake_link.cdb.nodes.append(
        node_for(
            template,
            uuid=PENDING_UUID,
            unicast=unicast,
            dev_key=PENDING_KEY,
            name="Pending",
        )
    )


async def test_reset_pending_device_answered_or_silent(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    fake_link: FakeProxyLink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repair issue comes with the setup that finds a pending device; a device that does not confirm its reset
    stays pending (the link forgets the key it was made to know for the reset), one that confirms is forgotten
    and the issue goes."""
    hub = provisioning_entry.runtime_data
    put_pending(hub, fake_link)
    await hub.vault.async_save()
    await hass.config_entries.async_reload(provisioning_entry.entry_id)
    await wait_for_link(hass, provisioning_entry)
    hub = provisioning_entry.runtime_data
    issue = pending_issue(hass, provisioning_entry)
    assert issue is not None
    assert issue.translation_placeholders == {
        "title": provisioning_entry.title,
        "addresses": f"{PENDING:04X}",
    }
    monkeypatch.setattr(onboard, "NODE_RESET_TIMEOUT", 0.01)
    fake_link.config_reply = lambda _node, _access: None
    with refused(HomeAssistantError, "reset_pending_device_no_answer"):
        await hass.services.async_call(
            DOMAIN, "reset_pending_device", {"unicast": "7FF0"}, blocking=True
        )
    vault = hub.vault.vault
    assert vault is not None
    assert [n.unicast for n in vault.pending] == [PENDING]
    assert hub.proxy.cdb.node_by_addr(PENDING) is None
    assert pending_issue(hass, provisioning_entry) is not None
    resets: list[int] = []

    def answer(node: int, access: bytes) -> bytes | None:
        resets.append(node)
        return reset_and_answer(node, access)

    fake_link.config_reply = answer
    result = await hass.services.async_call(
        DOMAIN,
        "reset_pending_device",
        {"uuid": PENDING_UUID.lower(), "unicast": "0x7ff0"},
        blocking=True,
        return_response=True,
    )
    assert result == {"uuid": PENDING_UUID, "unicast": "7FF0", "confirmed": True}
    assert resets == [PENDING]
    assert not vault.pending
    assert hub.proxy.cdb.node_by_addr(PENDING) is None
    assert pending_issue(hass, provisioning_entry) is None


async def test_reset_pending_device_forced_and_refused(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    fake_link: FakeProxyLink,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = provisioning_entry.runtime_data
    put_pending(hub, fake_link)
    vault = hub.vault.vault
    assert vault is not None
    # named wrongly: an unknown UUID, a UUID with another device's address, an address nothing pending is at
    for call in (
        {"uuid": "00000000-0000-4000-8000-000000000000"},
        {"uuid": PENDING_UUID, "unicast": "7FF1"},
        {"unicast": "7FF1"},
    ):
        with refused(ServiceValidationError, "reset_pending_device_unknown"):
            await hass.services.async_call(
                DOMAIN, "reset_pending_device", call, blocking=True
            )
    for call in ({}, {"unicast": "zz"}, {"unicast": "8000"}, {"force": True}):
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN, "reset_pending_device", call, blocking=True
            )
    # the link lost while sending: nothing changed
    with (
        patch.object(
            hub.proxy, "request_config", AsyncMock(side_effect=ConnectionError("gone"))
        ),
        refused(HomeAssistantError, "reset_pending_device_send_failed"),
    ):
        await hass.services.async_call(
            DOMAIN, "reset_pending_device", {"unicast": "7FF0"}, blocking=True
        )
    assert vault.pending
    assert hub.proxy.cdb.node_by_addr(PENDING) is None
    # gone for good: forced, forgotten unanswered
    monkeypatch.setattr(onboard, "NODE_RESET_TIMEOUT", 0.01)
    fake_link.config_reply = lambda _node, _access: None
    result = await hass.services.async_call(
        DOMAIN,
        "reset_pending_device",
        {"unicast": "7FF0", "force": True},
        blocking=True,
        return_response=True,
    )
    assert result == {"uuid": PENDING_UUID, "unicast": "7FF0", "confirmed": False}
    assert not vault.pending
    assert "did not confirm its reset" in caplog.text
    # an export node at its address now: no reset sent (it would go out with that node's key), only `force` forgets
    put_pending(hub, fake_link, 0x0300)
    sent = len(fake_link.config_sent)
    with refused(ServiceValidationError, "reset_pending_device_address_in_use"):
        await hass.services.async_call(
            DOMAIN, "reset_pending_device", {"unicast": "0300"}, blocking=True
        )
    await hass.services.async_call(
        DOMAIN,
        "reset_pending_device",
        {"uuid": PENDING_UUID, "force": True},
        blocking=True,
    )
    assert len(fake_link.config_sent) == sent
    assert not vault.pending
    assert hub.cdb.node_by_addr(0x0300) is not None  # the export's node is left alone
    # without the option: refused
    hass.config_entries.async_update_entry(
        provisioning_entry, options={OPTION_ALLOW_PROVISIONING: False}
    )
    await hass.async_block_till_done()
    await wait_for_link(hass, provisioning_entry)
    with refused(ServiceValidationError, "add_device_not_allowed"):
        await hass.services.async_call(
            DOMAIN, "reset_pending_device", {"unicast": "7FF0"}, blocking=True
        )


async def test_an_older_vaults_pending_node_is_warned_about(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pending node an earlier version kept without its element groups: which groups it holds is unknown, so
    they cannot be kept free — said in the log when the next device is planned."""
    hub = provisioning_entry.runtime_data
    put_pending(hub, fake_link)
    vault = hub.vault.vault
    assert vault is not None
    vault.nodes[PENDING_UUID].groups_known = False
    assert onboard._reserved_groups(hub) == {0xC0F0}
    assert "does not say which element groups it holds" in caplog.text
    assert f"{PENDING:04X}" in caplog.text


async def test_a_device_configured_but_not_recorded_is_reset_from_home_assistant(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    network_id: bytes,
) -> None:
    """Configured, then the record failed: the error says so (not "configuration failed"), the device is pending,
    and `reset_pending_device` reaches it with the key the link already knows it by, then lets it go."""
    hub = provisioning_entry.runtime_data
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    learn_new_nodes(hub, fake_link)
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    fake_link.config_reply = FreshNodes(fake_link)
    with (
        patch.object(
            onboard,
            "establish_connection",
            AsyncMock(
                return_value=FakeDevice(elements=len(template.elements), mtu_size=69)
            ),
        ),
        patch.object(onboard, "NODE_BOOT_DELAY", 0.0),
        patch.object(
            mesh_config.MeshConfigurator,
            "record_node",
            AsyncMock(side_effect=HomeAssistantError("the export could not be saved")),
        ),
        refused(HomeAssistantError, "add_device_record_failed") as caught,
    ):
        await hass.services.async_call(
            DOMAIN,
            "add_device",
            {"address": NEW_MAC, "name": "Hall light"},
            blocking=True,
        )
    placeholders = caught.value.translation_placeholders
    assert placeholders["error"] == "the export could not be saved"
    unicast = int(placeholders["unicast"], 16)
    known = hub.proxy.cdb.node_by_addr(unicast)
    assert known is not None  # commissioned through the link, which knows its key
    assert pending_issue(hass, provisioning_entry) is not None
    fake_link.config_reply = reset_and_answer
    result = await hass.services.async_call(
        DOMAIN,
        "reset_pending_device",
        {"uuid": known.uuid},
        blocking=True,
        return_response=True,
    )
    assert result == {
        "uuid": known.uuid,
        "unicast": f"{unicast:04X}",
        "confirmed": True,
    }
    assert hub.proxy.cdb.node_by_addr(unicast) is None
    assert pending_issue(hass, provisioning_entry) is None


async def test_add_device_is_refused_while_a_key_refresh_is_in_phase_one(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    network_id: bytes,
) -> None:
    """Review-4 D11: in Phase 1 the device would get the key being retired, and miss the rest of the refresh."""
    hub = provisioning_entry.runtime_data
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    fake_link.inject_from_provisioner(
        TEMPLATE, C.netkey_update(bytes(range(0x40, 0x50)))
    )
    await settle(hass)
    assert hub.proxy.key_refresh_phase == 1
    connect = AsyncMock()
    with (
        patch.object(onboard, "establish_connection", connect),
        refused(ServiceValidationError, "add_device_key_refresh"),
    ):
        await hass.services.async_call(
            DOMAIN,
            "add_device",
            {"address": NEW_MAC, "name": "Hall light"},
            blocking=True,
        )
    connect.assert_not_awaited()
    assert hub.vault.vault is None


def test_a_device_provisioned_in_phase_two_starts_there() -> None:
    """Phase 2 hands out the new key with the Key Refresh flag: the vault records the device at Phase 2 of that
    refresh (by the key's Network ID), so `vault_refresh.py` takes it on to Phase 3 only."""
    new = bytes(range(0x40, 0x50))
    data = ProvisioningData(
        net_key=new, unicast=0x7FF0, iv_index=0, iv_update=False, key_refresh=True
    )
    assert onboard._refresh_progress(data) == RefreshProgress(
        NetKeyMaterial.derive(new).network_id, 2
    )
    plain = ProvisioningData(
        net_key=new, unicast=0x7FF0, iv_index=0, iv_update=False, key_refresh=False
    )
    assert onboard._refresh_progress(plain) is None


# ----------------------------------------------------------------------------- review-4 F4-13, P4-8: brief 40


async def add_new_device(hass: HomeAssistant, **fields: Any) -> dict[str, Any] | None:
    """Call `add_device` for the new device, with the boot delay skipped."""
    with patch.object(onboard, "NODE_BOOT_DELAY", 0.0):
        return await hass.services.async_call(
            DOMAIN,
            "add_device",
            {"address": NEW_MAC, "name": "Hall light", **fields},
            blocking=True,
            return_response=True,
        )


async def test_add_device_lists_its_steps_and_sends_the_time(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    network_id: bytes,
) -> None:
    """The app's sequence, planned again from the node's own composition: the InsertId read of a push-button,
    Time Set to its Time Server, the steps in the response; the vault and the diagnostics keep what it offered."""
    hub = provisioning_entry.runtime_data
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    learn_new_nodes(hub, fake_link)
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    fake_link.config_reply = FreshNodes(fake_link)
    device = FakeDevice(elements=len(template.elements), mtu_size=69)
    with patch.object(onboard, "establish_connection", AsyncMock(return_value=device)):
        result = await add_new_device(hass)
    assert result is not None
    unicast = int(str(result["unicast"]), 16)
    assert result["steps"] == [
        "Provisioning",
        "SetWhitelistFilter",
        "RequestCompositionData",
        "SetConfiguration",
        "SetBlacklistFilter",
        "RequestRequiredData",
        "SetTime",
        "CreateElementGroups",
        "FinishConfiguration",
        "ReadBack",
        "Recording",
    ]
    assert result["provisioning"] == {
        "algorithm": "BTM_ECDH_P256_CMAC_AES128_AES_CCM",
        "authentication": "No OOB",
    }
    sent = [(dst, decode_opcode(access)[0]) for _src, dst, access in fake_link.sent]
    assert (
        unicast,
        M.TIME_SET,
    ) in sent  # to its own Time Server, besides the broadcasts
    insert_get = M.vendor_property_get("user", 0x0002)
    assert (unicast, insert_get) in [(d, a) for _s, d, a in fake_link.sent]
    await hass.async_block_till_done()
    await wait_for_link(hass, provisioning_entry)
    hub = provisioning_entry.runtime_data
    vault = hub.vault.vault
    assert vault is not None
    kept = next(n for n in vault.nodes.values() if n.unicast == unicast)
    assert kept.capabilities is not None
    assert kept.capabilities["used"] == result["provisioning"]
    assert kept.capabilities["algorithms"] == ["BTM_ECDH_P256_CMAC_AES128_AES_CCM"]
    diagnostics = await async_get_config_entry_diagnostics(hass, provisioning_entry)
    assert diagnostics["added_devices"][f"{unicast:04X}"] == {
        "recorded": True,
        "provisioning": kept.capabilities,
    }


async def test_add_device_with_the_devices_static_oob_value(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    network_id: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review-4 P4-8: a device offering Static OOB and the HMAC algorithm is provisioned with both when its value is
    given — the value appears in no log, no error and not in the vault."""
    hub = provisioning_entry.runtime_data
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    learn_new_nodes(hub, fake_link)
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    fake_link.config_reply = FreshNodes(fake_link)
    value = bytes(range(0x30, 0x50))
    device = FakeDevice(
        elements=len(template.elements),
        mtu_size=69,
        algorithms=3,
        oob_type=1,
        static_oob=value,
    )
    with patch.object(onboard, "establish_connection", AsyncMock(return_value=device)):
        result = await add_new_device(hass, static_oob=value.hex(":"))
    assert result is not None
    assert result["provisioning"] == {
        "algorithm": "BTM_ECDH_P256_HMAC_SHA256_AES_CCM",
        "authentication": "Static OOB",
    }
    assert device.start == bytes.fromhex("0100010000")
    assert value.hex() not in caplog.text.lower()
    vault = hub.vault.vault
    assert vault is not None
    assert value.hex() not in json.dumps(vault.to_dict()).lower()
    await hass.async_block_till_done()
    await wait_for_link(hass, provisioning_entry)


async def test_a_device_taking_only_authenticated_provisioning_needs_its_value(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    hub = provisioning_entry.runtime_data
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    device = FakeDevice(
        elements=len(template.elements), mtu_size=69, algorithms=3, oob_type=3
    )
    with (
        patch.object(onboard, "establish_connection", AsyncMock(return_value=device)),
        refused(HomeAssistantError, "add_device_provisioning_failed") as caught,
    ):
        await add_new_device(hass)
    assert "only OOB-authenticated" in caught.value.translation_placeholders["error"]
    assert [p[0] for p in device.received] == [0x00]  # the Invite, nothing after it
    vault = hub.vault.vault
    assert vault is None or not vault.nodes


@pytest.mark.parametrize(
    "value", ["00", "zz" * 16, "00" * 20, 5], ids=["short", "hex", "size", "number"]
)
async def test_a_static_oob_value_that_cannot_be_one_is_refused(
    hass: HomeAssistant, provisioning_entry: MockConfigEntry, value: Any
) -> None:
    with pytest.raises(
        vol.Invalid, match=r"not hexadecimal|not 16 or 32 bytes"
    ) as caught:
        await add_new_device(hass, static_oob=value)
    assert "zz" not in str(caught.value)


async def test_another_product_answering_is_reset_and_forgotten(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    network_id: bytes,
) -> None:
    """Its Composition Data is not the template's: refused before any binding, reset as the app does, and once it
    confirmed it is a new device again — nothing pending, nothing reserved (`nodenotconfigured`)."""
    hub = provisioning_entry.runtime_data
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    learn_new_nodes(hub, fake_link)
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    fresh = FreshNodes(fake_link)
    fresh.composition = composition_params(template, pid=0x0001)
    fresh.confirm_reset = True
    fake_link.config_reply = fresh
    device = FakeDevice(elements=len(template.elements), mtu_size=69)
    with (
        patch.object(onboard, "establish_connection", AsyncMock(return_value=device)),
        refused(HomeAssistantError, "add_device_node_not_configured") as caught,
    ):
        await add_new_device(hass)
    placeholders = caught.value.translation_placeholders
    assert placeholders["step"] == "RequestCompositionData"
    assert "product 0001, not 0002" in placeholders["error"]
    unicast = int(placeholders["unicast"], 16)
    assert fresh.resets == [unicast]
    assert not fresh.servers.app_keys  # nothing was bound
    vault = hub.vault.vault
    assert vault is not None
    assert not vault.nodes
    assert hub.proxy.cdb.node_by_addr(unicast) is None
    assert pending_issue(hass, provisioning_entry) is None


async def test_a_push_button_with_another_insert_is_reset(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    network_id: bytes,
) -> None:
    """It advertised a switch insert, its InsertId says blinds: the plan's groups would be a lamp's. Refused in
    `RequestRequiredData`, before any element group; this one does not confirm its reset: it stays pending."""
    hub = provisioning_entry.runtime_data
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    learn_new_nodes(hub, fake_link)
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    fresh = FreshNodes(fake_link)
    fake_link.config_reply = fresh
    insert_get = M.vendor_property_get("user", 0x0002)

    def blinds_insert(_element: int, access: bytes) -> bytes | None:
        if access != insert_get:
            return None
        status = encode_opcode(M.VENDOR_PROPERTY_STATUS_OPCODES["user"], M.JUNG_CID)
        return status + bytes.fromhex("0200010500")

    fake_link.app_reply = blinds_insert
    device = FakeDevice(elements=len(template.elements), mtu_size=69)
    with (
        patch.object(onboard, "establish_connection", AsyncMock(return_value=device)),
        refused(HomeAssistantError, "add_device_commissioning_failed") as caught,
    ):
        await add_new_device(hass)
    placeholders = caught.value.translation_placeholders
    assert placeholders["step"] == "RequestRequiredData"
    assert "carries insert 5, it was planned for insert 0" in placeholders["error"]
    assert not fresh.servers.publish  # no element group wired
    assert set(fresh.resets) == {int(placeholders["unicast"], 16)}  # three attempts
    assert pending_issue(hass, provisioning_entry) is not None


async def test_a_commissioning_over_its_budget_is_reset(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    network_id: bytes,
) -> None:
    hub = provisioning_entry.runtime_data
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    learn_new_nodes(hub, fake_link)
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    fresh = FreshNodes(fake_link)
    fresh.confirm_reset = True
    fake_link.config_reply = fresh

    async def stuck(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.Event().wait()

    device = FakeDevice(elements=len(template.elements), mtu_size=69)
    with (
        patch.object(onboard, "establish_connection", AsyncMock(return_value=device)),
        patch.object(onboard, "run_commission", stuck),
        patch.object(onboard, "COMMISSIONING_BUDGET", 0.01),
        refused(HomeAssistantError, "add_device_node_not_configured") as caught,
    ):
        await add_new_device(hass)
    assert (
        "not finished within 0.01 s" in caught.value.translation_placeholders["error"]
    )
    assert caught.value.translation_placeholders["step"] == "Provisioning"


async def test_a_lost_link_during_the_commissioning_is_reset(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    network_id: bytes,
) -> None:
    hub = provisioning_entry.runtime_data
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    learn_new_nodes(hub, fake_link)
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    fresh = FreshNodes(fake_link)
    fresh.confirm_reset = True
    fake_link.config_reply = fresh
    device = FakeDevice(elements=len(template.elements), mtu_size=69)
    with (
        patch.object(onboard, "establish_connection", AsyncMock(return_value=device)),
        patch.object(
            onboard, "run_commission", AsyncMock(side_effect=ConnectionError("lost"))
        ),
        refused(HomeAssistantError, "add_device_node_not_configured") as caught,
    ):
        await add_new_device(hass)
    assert caught.value.translation_placeholders["error"] == "lost"


class LostComplete(FakeDevice):
    """A device that takes its Provisioning Data, but whose Complete never arrives."""

    def handle(self, pdu_: bytes) -> list[bytes]:
        replies = super().handle(pdu_)
        return [] if pdu_[0] == DATA else replies


async def test_a_device_whose_complete_was_lost_is_reset(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    network_id: bytes,
) -> None:
    """The app's 30 s for the whole provisioning ran out after the Data PDU: the device may hold its data, so it
    is sent a reset with the key kept in the vault — confirmed, it is new again and nothing stays reserved."""
    hub = provisioning_entry.runtime_data
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    learn_new_nodes(hub, fake_link)
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    fresh = FreshNodes(fake_link)
    fresh.confirm_reset = True
    fake_link.config_reply = fresh
    device = LostComplete(elements=len(template.elements), mtu_size=69)
    with (
        patch.object(onboard, "establish_connection", AsyncMock(return_value=device)),
        patch.object(onboard, "PROVISIONING_BUDGET", 0.05),
        refused(HomeAssistantError, "add_device_provisioning_reset") as caught,
    ):
        await add_new_device(hass)
    assert device.data is not None  # it took its data
    unicast = int(caught.value.translation_placeholders["unicast"], 16)
    assert (
        caught.value.translation_placeholders["error"] == "not completed within 0.05 s"
    )
    assert fresh.resets == [unicast]
    vault = hub.vault.vault
    assert vault is not None
    assert not vault.nodes
    assert hub.proxy.cdb.node_by_addr(unicast) is None
    assert pending_issue(hass, provisioning_entry) is None


async def test_a_running_add_device_cancelled_closes_the_link(
    hass: HomeAssistant,
    provisioning_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """`provisioningaborting`: cancelling the action's task closes the PB-GATT link (the device forgets the
    half-finished session); nothing was kept for a device that never got as far as its key."""
    hub = provisioning_entry.runtime_data
    template = hub.cdb.node_by_addr(TEMPLATE)
    assert template is not None
    mock_bluetooth_env["infos"].append(new_device_advert(hub, network_id))
    device = FakeDevice(elements=len(template.elements), mtu_size=69)
    device.silent_after = 0x02  # waits for the device's public key forever
    device.disconnect = AsyncMock()  # type: ignore[method-assign]
    with patch.object(onboard, "establish_connection", AsyncMock(return_value=device)):
        task = hass.async_create_task(add_new_device(hass))
        while len(device.received) < 3:  # noqa: ASYNC110 - the fake device has no event to wait on
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    device.disconnect.assert_awaited()
    vault = hub.vault.vault
    assert vault is None or not vault.nodes
