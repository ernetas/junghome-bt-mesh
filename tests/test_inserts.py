"""A node's insert and key layout from the export, its JUNG advertisement or a Get (`inserts.py`, review-4 F4-12)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome_ble import coordinator, model_update
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_UNICAST,
    DOMAIN,
    ISSUE_INSERT_MISMATCH,
)
from custom_components.junghome_ble.diagnostics import async_get_device_diagnostics
from custom_components.junghome_ble.inserts import (
    NodeInserts,
    apply_reported,
    node_adverts,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.advert import (
    JUNG_COMPANY_ID,
    JungAdvertisement,
)
from custom_components.junghome_ble.jhmesh.devices import (
    Blind,
    Light,
    build_devices,
)
from custom_components.junghome_ble.jhmesh.properties import InsertId

from .conftest import (
    CDB_PATH,
    FakeProxyLink,
    make_service_info,
    settle,
    setup_entry,
    wait_for_link,
)
from .helpers import (
    LIGHT_CTL,
    LIGHT_DIMMER,
    LIGHT_SWITCH,
    MAC_LIGHT_CTL,
    MAC_LIGHT_SWITCH,
    NODE_LIGHT_CTL,
    NODE_LIGHT_SWITCH,
    entity_id,
    find_issue,
)
from .test_coordinator import answer_gets

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from custom_components.junghome_ble.jhmesh.cdb import CDB

NODE_0148 = f"node:{NODE_LIGHT_SWITCH}"
NODE_0232 = f"node:{NODE_LIGHT_CTL}"
BUTTONS_0148 = f"{NODE_LIGHT_SWITCH}-0040-buttons"
KEY_0148 = f"{NODE_LIGHT_SWITCH}-0040"  # its one key (element 0149), an event entity
INSERT_GET = M.vendor_property_get("user", 0x0002)  # LBC User Get InsertId
LAYOUT_GET = M.vendor_property_get("admin", 0x5001)  # LBC Admin Get ButtonLayout
STRINGS_JSON = (
    Path(__file__).parent.parent / "custom_components" / DOMAIN / "strings.json"
)


def jung_record(pid: int, function: int, layout: int, mac: str) -> dict[int, bytes]:
    """The manufacturer data of a node's type-3 JUNG record (`jhmesh.advert`), as HA hands it over."""
    return {
        JUNG_COMPANY_ID: bytes([3])
        + pid.to_bytes(2, "little")
        + function.to_bytes(2, "little")
        + layout.to_bytes(2, "little")
        + bytes.fromhex(mac.replace(":", ""))[::-1]
    }


def model_of(hass: HomeAssistant, entry: MockConfigEntry, ident: str) -> str | None:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ident), entry.entry_id
    )
    assert device is not None
    return device.model


def position_of(hass: HomeAssistant, unique_id: str) -> str | None:
    state = hass.states.get(entity_id(hass, "event", unique_id))
    assert state is not None
    return state.attributes.get("position")


async def set_up(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    return entry.runtime_data


@pytest.fixture
def bare_entry() -> MockConfigEntry:
    """The fixture export without its app metadata: no InsertId cached for any node."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={CONF_CDB_PATH: CDB_PATH, CONF_UNICAST: "0D00"},
    )


async def test_the_advert_names_the_insert_and_the_keys(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    network_id: bytes,
) -> None:
    """The node device's model names its insert, the buttons device's the key layout, each key its position; a new
    layout in a later advert shows at once."""
    mock_bluetooth_env["infos"] = [
        make_service_info(
            network_id, manufacturer_data=jung_record(1, 0, 0, MAC_LIGHT_SWITCH)
        )
    ]
    hub = await set_up(hass, mock_config_entry)
    assert hub.inserts.adverts[LIGHT_SWITCH] == JungAdvertisement(
        3, 1, 0, 0, MAC_LIGHT_SWITCH
    )
    assert (
        model_of(hass, mock_config_entry, NODE_0148)
        == "Push-button 1-gang (Switch insert)"
    )
    assert (
        model_of(hass, mock_config_entry, BUTTONS_0148)
        == "Push-buttons (Button top / bottom)"
    )
    assert position_of(hass, KEY_0148) == "top"
    # the 2-gang advertised nothing: its export's DALI insert, no layout
    assert (
        model_of(hass, mock_config_entry, NODE_0232)
        == "Push-button 2-gang (DALI insert)"
    )
    assert (
        model_of(hass, mock_config_entry, f"{NODE_LIGHT_CTL}-0040-buttons")
        == "Push-buttons"
    )

    callback_ = mock_bluetooth_env["callbacks"][0]
    advert = make_service_info(
        network_id, manufacturer_data=jung_record(1, 0, 1, MAC_LIGHT_SWITCH)
    )
    callback_(advert, BluetoothChange.ADVERTISEMENT)
    await hass.async_block_till_done()
    assert model_of(hass, mock_config_entry, BUTTONS_0148) == "Push-buttons (Rocker)"
    assert position_of(hass, KEY_0148) == "rocker"
    # the same record again, one without the JUNG record, another product's: nothing changes
    for again in (
        advert,
        make_service_info(network_id),
        make_service_info(
            network_id, manufacturer_data=jung_record(3, 5, 0, MAC_LIGHT_SWITCH)
        ),
    ):
        callback_(again, BluetoothChange.ADVERTISEMENT)
    assert hub.inserts.adverts[LIGHT_SWITCH].button_layout == 1

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, NODE_0148), mock_config_entry.entry_id
    )
    assert device is not None
    diagnostics = await async_get_device_diagnostics(hass, mock_config_entry, device)
    assert diagnostics["insert"] == {
        "export_function": 0,
        "export_layout": None,
        "advert": {"function": 0, "layout": 1},
        "built_with": 0,
        "function": 0,
        "layout": 1,
    }
    assert MAC_LIGHT_SWITCH not in str(diagnostics["insert"])


async def test_without_an_export_insert_the_advertised_one_decides(
    hass: HomeAssistant,
    bare_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    network_id: bytes,
) -> None:
    """Fallback order, export then advert: the 2-gang's lamp and level servers are a blind when it advertises a
    blinds insert and the export cached none; a node that advertises nothing is asked (and here says nothing)."""
    mock_bluetooth_env["infos"] = [
        make_service_info(network_id),
        make_service_info(
            network_id,
            address=MAC_LIGHT_CTL,
            rssi=-90,
            manufacturer_data=jung_record(2, 5, 5, MAC_LIGHT_CTL),
        ),
    ]
    hub = await set_up(hass, bare_entry)
    assert isinstance(hub.devices.by_address.get(LIGHT_CTL), Blind)
    assert model_of(hass, bare_entry, NODE_0232) == "Push-button 2-gang (Blinds insert)"
    # the connect-time step (`test_the_reads_are_a_connect_step`)
    assert await hub.inserts.read_unknown()
    assert hub.inserts._unsupported == {LIGHT_SWITCH, LIGHT_DIMMER}
    # 0148 and 0300 advertised nothing: asked for their InsertId and layout, they answered without a value
    asked = {
        (dst, pdu)
        for _src, dst, pdu in fake_link.sent
        if pdu in (INSERT_GET, LAYOUT_GET)
    }
    assert asked == {
        (LIGHT_SWITCH, INSERT_GET),
        (LIGHT_SWITCH, LAYOUT_GET),
        (LIGHT_DIMMER, INSERT_GET),
        (LIGHT_DIMMER, LAYOUT_GET),
    }
    assert model_of(hass, bare_entry, NODE_0148) == "Push-button 1-gang"


async def test_a_node_nothing_told_about_is_asked_and_its_answer_kept(
    hass: HomeAssistant,
    bare_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fallback order, the Get last: an InsertId and a ButtonLayout answered show at once and decide the devices
    from the next setup on, without asking again."""
    fake_link.node_info[LIGHT_CTL, 0x0002] = bytes.fromhex(
        "05000200"
    )  # blinds, generic insert
    fake_link.node_info[LIGHT_CTL, 0x5001] = bytes.fromhex("0500")  # rocker | rocker
    caplog.set_level(logging.INFO, "custom_components.junghome_ble.inserts")
    hub = await set_up(hass, bare_entry)
    assert await hub.inserts.read_unknown()
    await hass.async_block_till_done()
    assert hub.node_info(LIGHT_CTL)["insert_id"] == bytes.fromhex("05000200")
    assert isinstance(
        hub.devices.by_address.get(LIGHT_CTL), Light
    )  # built before the answer
    assert model_of(hass, bare_entry, NODE_0232) == "Push-button 2-gang (Blinds insert)"
    assert (
        model_of(hass, bare_entry, f"{NODE_LIGHT_CTL}-0040-buttons")
        == "Push-buttons (Rocker | Rocker)"
    )
    assert position_of(hass, f"{NODE_LIGHT_CTL}-0040") == "left_rocker"
    assert "Node 0232 reports its insert now" in caplog.text
    # the device diagnostics decode the answers: function and insert type, the layout
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, NODE_0232), bare_entry.entry_id
    )
    assert device is not None
    diagnostics = await async_get_device_diagnostics(hass, bare_entry, device)
    assert diagnostics["node_info"]["insert_id"] == str(
        InsertId("blind", "generic_insert")
    )
    assert diagnostics["node_info"]["button_layout"] == "one_left_one_right"
    assert diagnostics["insert"]["function"] == 5
    assert diagnostics["insert"]["built_with"] is None

    fake_link.sent.clear()
    assert await hass.config_entries.async_reload(bare_entry.entry_id)
    await wait_for_link(hass, bare_entry)
    await settle(hass)
    hub = bare_entry.runtime_data
    assert isinstance(hub.devices.by_address.get(LIGHT_CTL), Blind)
    assert await hub.inserts.read_unknown()
    assert (LIGHT_CTL, INSERT_GET) not in {
        (dst, pdu) for _src, dst, pdu in fake_link.sent
    }


async def test_a_swapped_insert_raises_the_repair_until_the_advert_agrees(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    network_id: bytes,
) -> None:
    """The export cached a switch insert for 0148; the node advertises a blinds insert: re-export."""
    mock_bluetooth_env["infos"] = [
        make_service_info(
            network_id, manufacturer_data=jung_record(1, 5, 0, MAC_LIGHT_SWITCH)
        )
    ]
    hub = await set_up(hass, mock_config_entry)
    issue = find_issue(hass, ISSUE_INSERT_MISMATCH)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == ISSUE_INSERT_MISMATCH
    assert issue.translation_placeholders == {
        "title": "JUNG HOME mesh test",
        "devices": "Push-button 1-gang 0148 (Switch insert → Blinds insert)",
    }
    text = json.loads(STRINGS_JSON.read_text())["issues"][ISSUE_INSERT_MISMATCH]
    assert set(re.findall(r"\{(\w+)\}", text["title"] + text["description"])) == set(
        issue.translation_placeholders
    )
    # the export's insert decides the devices and the model; the advert only reports
    assert isinstance(hub.devices.by_address.get(LIGHT_SWITCH), Light)
    assert (
        model_of(hass, mock_config_entry, NODE_0148)
        == "Push-button 1-gang (Switch insert)"
    )

    # an unknown function in the report: the insert's name is its number
    mock_bluetooth_env["callbacks"][0](
        make_service_info(
            network_id, manufacturer_data=jung_record(1, 6, 0, MAC_LIGHT_SWITCH)
        ),
        BluetoothChange.ADVERTISEMENT,
    )
    issue = find_issue(hass, ISSUE_INSERT_MISMATCH)
    assert issue is not None
    assert issue.translation_placeholders["devices"].endswith(
        "(Switch insert → Extension insert)"
    )
    hub.inserts.labels.clear()  # no translation loaded: the numbers stand in
    hub.inserts.report_mismatch()
    issue = find_issue(hass, ISSUE_INSERT_MISMATCH)
    assert issue is not None
    assert issue.translation_placeholders["devices"].endswith("(0 → 6)")

    mock_bluetooth_env["callbacks"][0](
        make_service_info(
            network_id, manufacturer_data=jung_record(1, 0, 0, MAC_LIGHT_SWITCH)
        ),
        BluetoothChange.ADVERTISEMENT,
    )
    assert find_issue(hass, ISSUE_INSERT_MISMATCH) is None


async def test_the_reads_are_a_connect_step(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """The Gets run after the link's other reads, once per link (`Refresh.connect_step`)."""
    answer_gets(fake_link)
    hub = await set_up(hass, mock_config_entry)
    hub.previous_link = coordinator.LinkEnd("the proxy disconnected", None, 0.0)
    with patch.object(
        hub.inserts, "read_unknown", AsyncMock(return_value=True)
    ) as step:
        await hub.refresh.after_connect()
    step.assert_awaited_once()
    assert "inserts" in hub.refresh.connect_steps_done


def test_node_adverts_take_the_records_of_the_nodes_alone(
    hass: HomeAssistant, mock_bluetooth_env: dict[str, Any], network_id: bytes, cdb: CDB
) -> None:
    mock_bluetooth_env["infos"] = [
        make_service_info(
            network_id, manufacturer_data=jung_record(1, 2, 0, MAC_LIGHT_SWITCH)
        ),
        # a record naming another product than the node at that MAC, a MAC no node has, no record at all
        make_service_info(
            network_id,
            address=MAC_LIGHT_CTL,
            manufacturer_data=jung_record(1, 2, 0, MAC_LIGHT_CTL),
        ),
        make_service_info(
            network_id,
            address="00:00:5E:00:53:99",
            manufacturer_data=jung_record(1, 2, 0, "00:00:5E:00:53:99"),
        ),
        make_service_info(network_id, address="00:00:5E:00:53:30"),
    ]
    assert node_adverts(hass, cdb) == {
        LIGHT_SWITCH: JungAdvertisement(3, 1, 2, 0, MAC_LIGHT_SWITCH)
    }


def test_apply_reported_rebuilds_only_when_the_model_changes(cdb: CDB) -> None:
    devices = build_devices(cdb)
    assert apply_reported(cdb, devices, {}, lambda _unicast: {}) is devices
    rebuilt = apply_reported(
        cdb,
        devices,
        {},
        lambda unicast: {"insert_id": b"\x05\x00"} if unicast == LIGHT_CTL else {},
    )
    assert rebuilt is not devices
    assert isinstance(rebuilt.by_address.get(LIGHT_CTL), Blind)


def fake_hub(cdb: CDB, request: AsyncMock) -> Any:
    hub = SimpleNamespace(
        cdb=cdb,
        devices=build_devices(cdb),
        node_info=lambda _unicast: {},
        remember_node_info=lambda *_args: None,
        proxy=SimpleNamespace(request=request),
        hass=None,
    )
    inserts = NodeInserts(hub, "insert_mismatch_entry")  # type: ignore[arg-type]
    inserts.changed = lambda _node: None  # type: ignore[method-assign]
    return inserts


async def test_a_silent_node_or_a_lost_link_leaves_the_reads_to_the_next_link(
    cdb: CDB,
) -> None:
    inserts = fake_hub(cdb, AsyncMock(side_effect=TimeoutError))
    assert not await inserts.read_unknown()
    assert not inserts._unsupported  # asked again on the next link
    inserts = fake_hub(cdb, AsyncMock(side_effect=ConnectionError))
    assert not await inserts.read_unknown()


async def test_a_node_without_the_server_or_with_a_known_layout_is_not_asked_for_it(
    cdb: CDB,
) -> None:
    request = AsyncMock(return_value=SimpleNamespace(params=b"\x02\x00\x01\x00\x00"))
    inserts = fake_hub(cdb, request)
    for node in cdb.nodes:
        if node.unicast == LIGHT_SWITCH:
            node.button_layout = 1  # the export's layout: only the InsertId is asked
        if node.unicast == LIGHT_DIMMER:
            for (
                element
            ) in node.elements:  # no LBC User server: nothing to ask the InsertId of
                element.models = [m for m in element.models if m != "05271013"]
    assert await inserts.read_unknown()
    assert [call.args[:2] for call in request.await_args_list] == [
        (LIGHT_SWITCH, INSERT_GET),
        (LIGHT_CTL, INSERT_GET),
        (LIGHT_CTL, LAYOUT_GET),
        (LIGHT_DIMMER, LAYOUT_GET),
    ]
    assert inserts._unsupported == {LIGHT_DIMMER}


async def test_following_the_export_in_place_keeps_the_advertised_insert(
    hass: HomeAssistant,
    bare_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    network_id: bytes,
) -> None:
    """Review-4 D23 with F4-12: a configuration change re-reads the export (`model_update`); the advertised insert
    still decides a push-button whose export has none, so the blind stays a blind and the hub follows in place
    instead of falling back to a reload."""
    mock_bluetooth_env["infos"] = [
        make_service_info(network_id),
        make_service_info(
            network_id,
            address=MAC_LIGHT_CTL,
            rssi=-90,
            manufacturer_data=jung_record(2, 5, 5, MAC_LIGHT_CTL),
        ),
    ]
    hub = await set_up(hass, bare_entry)
    assert isinstance(hub.devices.by_address.get(LIGHT_CTL), Blind)
    with patch.object(hass.config_entries, "async_reload") as reload:
        await model_update.async_follow_export(hass, bare_entry.entry_id)
        await settle(hass)
    reload.assert_not_called()
    assert bare_entry.runtime_data is hub
    assert isinstance(hub.devices.by_address.get(LIGHT_CTL), Blind)
