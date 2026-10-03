"""Setup / unload / not-ready paths, device registry layout and stale-device handling."""

from __future__ import annotations

import copy
import json
import os
import shutil
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceEntryType
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.junghome_ble import (
    INCOMING_MAX_AGE,
    _reload_once_loaded,
    async_migrate_entry,
    async_remove_config_entry_device,
    sweep_incoming,
)
from custom_components.junghome_ble.config_flow import (
    certificate_issue_id,
    infer_source,
)
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_GATEWAY_FINGERPRINT,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_TOKEN,
    CONF_MESH_UUID,
    CONF_METADATA_DIR,
    CONF_SOURCE,
    CONF_UNICAST,
    DOMAIN,
    ISSUE_ADDRESS_IN_USE,
    ISSUE_ADDRESS_RESERVED,
    ISSUE_KEY_REFRESH,
    SEQ_SKIP_AHEAD,
    STORAGE_DIR,
)
from custom_components.junghome_ble.entity import mac_from_uuid

from .conftest import (
    CDB_PATH,
    SHARE_EXPORT_PATH,
    FakeProxyLink,
    make_node_identity_info,
    make_service_info,
    settle,
    setup_entry,
    wait_for_link,
)
from .helpers import (
    MAC_LIGHT_CTL,
    MAC_LIGHT_SWITCH,
    MESH_UUID,
    NODE_ACTUATOR,
    NODE_LIGHT_CTL,
    NODE_LIGHT_SWITCH,
    NODE_SOCKET,
    PROVISIONER_UUID,
    SEQ_STORE_KEY,
    find_issue,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.typing import WebSocketGenerator

    from custom_components.junghome_ble.jhmesh.cdb import CDB

MESH_ID = "mesh:1baf3ade-0000-4000-8000-000000000001"
NODE_0148 = f"node:{NODE_LIGHT_SWITCH}"
LIGHT_0148 = f"{NODE_LIGHT_SWITCH}-0001"
BUTTONS_0148 = f"{NODE_LIGHT_SWITCH}-0040-buttons"
SOCKET_0172 = f"{NODE_SOCKET}-0001"


async def test_setup_and_unload(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    entry = init_integration
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.connected
    registry = er.async_get(hass)
    light_id = registry.async_get_entity_id("light", DOMAIN, LIGHT_0148)
    assert light_id
    assert hass.states.get(light_id) is not None
    assert hass.states.get("scene.wc_off") is not None
    # one entity per light / socket / button / scene, sensors for the metering socket and the proxy diagnostic,
    # plus the config entities of every device parameter (counted in tests/test_config_entities.py)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    domains = sorted(e.domain for e in entities)
    assert domains == (
        ["binary_sensor"] * (6 + 2)  # Fault, one per node; the gateway's API status
        + ["button"] * 13  # Identify, Clear faults per node; Reset consumption
        + ["event"] * 4
        # the loads; All lights; the lights of WC, Living room and Kitchen
        + ["light"] * (5 + 1 + 3)
        # parameters; Lock time limit per load; dimmer setup (with the DALI insert's white area)
        + ["number"] * (24 + 6 + 7 + 2)
        + ["scene"] * 2
        # parameters, LED colours; behaviour after mains return
        + ["select"] * (11 + 6)
        # 7 socket sensors + Installed + the proxy and link state diagnostics; key mode per key; schedules and scenes
        # per load; 2 thresholds; last seen, signal, hops and last restart per (mains) node; IV index and the two
        # sequence gauges; two wear counters per light and socket; the gateway's address
        + ["sensor"] * (10 + 4 + 6 * 2 + 2 + 6 * 4 + 3 + 6 * 2 + 1)
        # socket, All sockets, the Kitchen's sockets, parameters, Lock, Lock operation and Lock factory reset per
        # device node, night mode, LED colour synchronisation of the 2-gang, previous brightness, sensor values for IoT
        + ["switch"] * (1 + 1 + 1 + 26 + 6 + 5 * 2 + 4 + 1 + 2 + 1)
    )

    # a live repair issue of this mesh is cleared with the unload (nothing of it is running any more)
    entry.runtime_data.report_key_refresh()
    assert find_issue(hass, ISSUE_KEY_REFRESH) is not None
    assert (
        find_issue(hass, ISSUE_KEY_REFRESH).issue_id == f"key_refresh_{entry.entry_id}"
    )
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not fake_link.is_connected
    assert find_issue(hass, ISSUE_KEY_REFRESH) is None


async def test_not_ready_without_visible_proxy(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
) -> None:
    mock_bluetooth_env["infos"] = []
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert mock_config_entry.error_reason_translation_key == "no_proxy_visible"


async def test_not_ready_without_bluetooth_says_so(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
) -> None:
    """No connectable scanner at all: not a mesh out of range but a Home Assistant without Bluetooth (the app's lock screen)."""
    mock_bluetooth_env["infos"] = []
    mock_bluetooth_env["scanners"] = 0
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert mock_config_entry.error_reason_translation_key == "bluetooth_unavailable"


async def _fire_setup_retry(
    hass: HomeAssistant, entry: MockConfigEntry, seconds: float
) -> None:
    """Advance time past the entry's scheduled setup retry (and any pending delayed store write) and let it finish.

    The retry runs as a background task and reads the export in the executor: `settle` waits for both.
    """
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await settle(hass)
    assert entry.state is not ConfigEntryState.SETUP_IN_PROGRESS


async def test_not_ready_retries_do_not_burn_sequence_numbers(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    hass_storage: dict[str, Any],
) -> None:
    """The +512 restart margin is applied when the sequence-number state is created; a setup that ends in
    ConfigEntryNotReady must not create it, or every retry would burn another 512 numbers (and the seq store
    would be rewritten each time). Two not-ready attempts, then a successful one: the margin is applied once."""
    key = SEQ_STORE_KEY
    stored = {
        "addresses": {
            "0D00": {
                "seq": 100,
                "iv_index": 0,
                "iv_update_active": False,
                "clean": False,
            }
        }
    }
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": copy.deepcopy(stored),
    }
    proxies = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    await _fire_setup_retry(
        hass, mock_config_entry, 6
    )  # first retry (5 s), still no proxy
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert (
        hass_storage[key]["data"] == stored
    )  # nothing was written: no state was created

    mock_bluetooth_env["infos"] = proxies
    await _fire_setup_retry(
        hass, mock_config_entry, 11
    )  # second retry (10 s), a proxy is in range now
    assert mock_config_entry.state is ConfigEntryState.LOADED
    await wait_for_link(hass, mock_config_entry)
    state = mock_config_entry.runtime_data.proxy.state
    assert state.seq == 100 + 512 + len(
        fake_link.raw_writes
    )  # one margin, then one number per PDU sent


@pytest.mark.parametrize(
    "cdb_path", ["/nowhere/MeshNetwork.json", __file__], ids=["missing", "not_json"]
)
async def test_setup_error_when_export_unreadable(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    cdb_path: str,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1fbd2c61a4b6e5a4",
        data={CONF_CDB_PATH: cdb_path, CONF_UNICAST: "0D00"},
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "cannot_load"
    assert entry.error_reason_translation_placeholders == {"path": cdb_path}


@pytest.mark.parametrize(
    ("unicast", "state", "raised", "severity"),
    [
        (
            "0148",
            ConfigEntryState.SETUP_ERROR,
            ISSUE_ADDRESS_IN_USE,
            ir.IssueSeverity.ERROR,
        ),
        (
            "0CCC",
            ConfigEntryState.LOADED,
            ISSUE_ADDRESS_RESERVED,
            ir.IssueSeverity.WARNING,
        ),
        ("0D00", ConfigEntryState.LOADED, None, None),
    ],
    ids=["node", "provisioner-range", "free"],
)
async def test_our_address_is_checked_against_the_export_at_every_setup(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    unicast: str,
    state: ConfigEntryState,
    raised: str | None,
    severity: ir.IssueSeverity | None,
) -> None:
    """Review-3 W3: the flow checks the address once, but the app provisions nodes and a second app user gets a
    provisioner range of their own. A node on our address stops the setup; a reserved one only warns."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={CONF_CDB_PATH: CDB_PATH, CONF_UNICAST: unicast},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is state
    for key in (ISSUE_ADDRESS_IN_USE, ISSUE_ADDRESS_RESERVED):
        issue = find_issue(hass, key)
        if key != raised:
            assert issue is None
            continue
        assert issue is not None
        assert issue.severity is severity
        assert issue.translation_placeholders == {
            "title": "JUNG HOME mesh test",
            "unicast": unicast,
            "suggestion": "0D00",
        }
    if raised == ISSUE_ADDRESS_IN_USE:
        assert entry.error_reason_translation_key == "address_in_use"
    if state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


def test_suggest_unicast_skips_nodes_ranges_and_exclusions(cdb: CDB) -> None:
    assert cdb.suggest_unicast() == 0x0D00
    # from inside the phone's range 0001-0CCC: the first address past it
    assert cdb.suggest_unicast(0x0148) == 0x0CCD
    cdb.provisioner_unicast_ranges.append((0x0CCD, 0x7FFF))
    assert cdb.suggest_unicast() is None


async def test_setup_error_names_a_malformed_metadata_file(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
) -> None:
    """A malformed app-container metadata file fails the setup with *its* path, not the export's."""
    bad = tmp_path / "device_metadata.json"
    bad.write_text("{}")  # the app writes a list
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1fbd2c61a4b6e5a4",
        data={
            CONF_CDB_PATH: CDB_PATH,
            CONF_METADATA_DIR: str(tmp_path),
            CONF_UNICAST: "0D00",
        },
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "cannot_load"
    assert entry.error_reason_translation_placeholders == {"path": str(bad)}


async def test_setup_accepts_a_proxy_advertising_node_identity(
    hass: HomeAssistant,
    cdb: CDB,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """A proxy that advertises Node Identity instead of the Network ID (right after provisioning, or with the
    app's identify on) counts as in range: the hub connects to it, so setup must not wait for a Network ID."""
    mock_bluetooth_env["infos"] = [make_node_identity_info(cdb, 0x0148)]
    await setup_entry(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    await wait_for_link(hass, mock_config_entry)
    assert mock_config_entry.runtime_data.connected
    assert mock_config_entry.runtime_data.proxy_node == 0x0148


async def test_setup_without_metadata(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """Without the app's metadata folder, devices and scenes get generic names from the export."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1fbd2c61a4b6e5a4",
        data={CONF_CDB_PATH: CDB_PATH, CONF_UNICAST: "0D00"},
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    devices = dr.async_get(hass)
    light = devices.async_get_device_by_identifier((DOMAIN, LIGHT_0148), entry.entry_id)
    assert light is not None
    assert light.name == "Push-button 1-gang 0148"
    assert hass.states.get("scene.scene_1") is not None


async def test_setup_from_share_export(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """The app's "share via file" export carries the names itself; nothing else in the export names things."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1fbd2c61a4b6e5a4",
        data={CONF_CDB_PATH: SHARE_EXPORT_PATH, CONF_UNICAST: "0D00"},
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    devices = dr.async_get(hass)
    light = devices.async_get_device_by_identifier((DOMAIN, LIGHT_0148), entry.entry_id)
    assert light is not None
    assert light.name == "WC mirror (share)"
    dali = devices.async_get_device_by_identifier(
        (DOMAIN, f"{NODE_LIGHT_CTL}-0001"), entry.entry_id
    )
    assert dali is not None
    assert dali.name == "Push-button 2-gang 0232"
    assert hass.states.get("scene.wc_off_share") is not None
    assert hass.states.get("scene.scene_2") is not None


@pytest.mark.parametrize(
    ("stored_src", "clean", "expected_seq", "warned"),
    [
        ("0D00", False, 100 + 512, False),
        ("0D00", True, 100, False),
        ("0E00", False, SEQ_SKIP_AHEAD + 512, True),
    ],
    ids=["same_address", "same_address_closed_cleanly", "address_unknown"],
)
async def test_local_state_restored_from_storage(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    stored_src: str,
    clean: bool,
    expected_seq: int,
    warned: bool,
) -> None:
    """The mesh store's record of the configured address continues: with the restart margin when it was in use
    when Home Assistant last stopped, exactly when it was closed cleanly. An address the store does not know
    starts SEQ_SKIP_AHEAD on (plus the margin) when the store knows other addresses (it may have been used: review-4
    S I5), with a warning; the record of the other address stays."""
    key = SEQ_STORE_KEY
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "addresses": {
                stored_src: {
                    "seq": 100,
                    "iv_index": 0,
                    "iv_update_active": False,
                    "clean": clean,
                }
            }
        },
    }
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    state = mock_config_entry.runtime_data.proxy.state
    assert state.src == 0x0D00
    assert state.seq == expected_seq + len(
        fake_link.raw_writes
    )  # the filter set and the state Gets after connecting
    assert (
        "Address 0D00 has no sequence-number record, but the store knows other addresses (0E00)"
        in caplog.text
    ) is warned
    await hass.async_block_till_done()
    addresses = hass_storage[key]["data"]["addresses"]
    assert set(addresses) == {stored_src, "0D00"}
    assert addresses["0D00"]["clean"] is False  # in use again


async def test_stale_devices_removed_on_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Devices of a previous export that are no longer in the file are detached from the entry on setup."""
    mock_config_entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    stale = devices.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "node:00000000-0000-0000-0000-000000000000")},
        name="Old node",
    )
    kept = devices.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, LIGHT_0148)},
        name="Old name",
    )

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await wait_for_link(hass, mock_config_entry)

    assert devices.async_get(stale.id) is None
    assert "Removing device Old node, no longer in the mesh export" in caplog.text
    kept_now = devices.async_get(kept.id)
    assert kept_now is not None
    assert kept_now.name == "WC mirror"  # re-registered by the light platform


async def test_device_registry_layout(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Mesh service device → node devices (with the MAC from the UUID) → load / button devices, linked with via_device_id."""
    entry_id = init_integration.entry_id
    devices = dr.async_get(hass)

    mesh = devices.async_get_device_by_identifier((DOMAIN, MESH_ID), entry_id)
    assert mesh is not None
    assert mesh.entry_type is DeviceEntryType.SERVICE
    assert mesh.name == "JUNG HOME mesh test"
    assert mesh.via_device_id is None

    node = devices.async_get_device_by_identifier((DOMAIN, NODE_0148), entry_id)
    assert node is not None
    assert node.name == "Push-button 1-gang 0148"
    assert node.model == "Push-button 1-gang"
    assert node.serial_number == MAC_LIGHT_SWITCH
    assert node.connections == {(CONNECTION_BLUETOOTH, MAC_LIGHT_SWITCH)}
    assert node.via_device_id == mesh.id
    assert (
        devices.async_get_device_by_connection(
            (CONNECTION_BLUETOOTH, MAC_LIGHT_SWITCH), entry_id
        )
        == node
    )

    light = devices.async_get_device_by_identifier((DOMAIN, LIGHT_0148), entry_id)
    assert light is not None
    assert light.name == "WC mirror"
    assert light.model == "Switched light"
    assert light.via_device_id == node.id
    assert (
        light.area_id == "wc"
    )  # suggested area from the room the load is subscribed to

    buttons = devices.async_get_device_by_identifier((DOMAIN, BUTTONS_0148), entry_id)
    assert buttons is not None
    assert buttons.name == "WC mirror button"
    assert buttons.model == "Push-buttons"
    assert buttons.via_device_id == node.id

    socket = devices.async_get_device_by_identifier((DOMAIN, SOCKET_0172), entry_id)
    assert socket is not None
    assert socket.name == "Boiler"
    assert (
        socket.model == "Socket (metering)"
    )  # the product's name: a plain socket (0x0C) says "Socket"
    assert socket.area_id == "kitchen"
    socket_node = devices.async_get_device_by_identifier(
        (DOMAIN, f"node:{NODE_SOCKET}"), entry_id
    )
    assert socket_node is not None
    assert socket.via_device_id == socket_node.id
    assert socket_node.model == "Socket (metering)"

    actuator = devices.async_get_device_by_identifier(
        (DOMAIN, f"node:{NODE_ACTUATOR}"), entry_id
    )
    assert actuator is not None
    assert actuator.model == "Switch actuator 1-gang 2-input energy"
    # the provisioner phone (no product id) gets no device
    assert (
        devices.async_get_device_by_identifier(
            (DOMAIN, f"node:{PROVISIONER_UUID}"), entry_id
        )
        is None
    )
    assert (
        len(dr.async_entries_for_config_entry(devices, entry_id)) == 1 + 6 + 5 + 1 + 3
    )


def test_mac_from_uuid() -> None:
    assert mac_from_uuid(NODE_LIGHT_CTL.upper()) == MAC_LIGHT_CTL
    assert mac_from_uuid(NODE_LIGHT_CTL) == MAC_LIGHT_CTL
    assert mac_from_uuid(PROVISIONER_UUID) is None  # a random UUID, not EUI-64
    assert mac_from_uuid("not-a-uuid") is None


async def test_remove_config_entry_device(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Only devices that are no longer in the export may be deleted from the UI."""
    entry = init_integration
    devices = dr.async_get(hass)
    stale = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "node:gone")}, name="Gone"
    )
    light = devices.async_get_device_by_identifier((DOMAIN, LIGHT_0148), entry.entry_id)
    assert light is not None

    assert await async_remove_config_entry_device(hass, entry, stale)
    assert not await async_remove_config_entry_device(hass, entry, light)

    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "config/device_registry/remove_config_entry",
            "config_entry_id": entry.entry_id,
            "device_id": stale.id,
        }
    )
    response = await client.receive_json()
    assert response["success"]
    assert devices.async_get(stale.id) is None

    await client.send_json_auto_id(
        {
            "type": "config/device_registry/remove_config_entry",
            "config_entry_id": entry.entry_id,
            "device_id": light.id,
        }
    )
    response = await client.receive_json()
    assert not response["success"]
    assert (
        response["error"]["message"]
        == "Failed to remove device entry, rejected by integration"
    )
    assert devices.async_get(light.id) is not None


async def test_remove_device_of_an_entry_that_is_not_loaded(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """No hub to ask (no runtime data): any device may go; the next setup adds back what the export has."""
    entry = init_integration
    light = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, LIGHT_0148), entry.entry_id
    )
    assert light is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await async_remove_config_entry_device(hass, entry, light)


@pytest.mark.parametrize(
    ("source", "in_store", "removed"),
    [
        ("gateway", False, True),
        ("upload", False, True),
        ("path", False, False),
        (None, False, False),
        (
            None,
            True,
            True,
        ),  # HAC-07: an entry never migrated, whose export we stored ourselves
        (None, "user-file", False),  # ... but a user's own file in our folder stays
    ],
)
async def test_removing_the_entry_deletes_only_exports_we_stored(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    source: str | None,
    in_store: bool | str,
    removed: bool,
) -> None:
    """A fetched or uploaded export is ours to delete with the entry; a user-provided path is not."""
    # the removal asks Bluetooth for the mesh's proxies first (nothing advertising here); without a stand-in this
    # passed only after another test had set Bluetooth up in the same process
    mock_bluetooth_env["infos"] = []
    folder = _store(hass) if in_store else tmp_path
    folder.mkdir(parents=True, exist_ok=True)
    export = folder / (
        f"{MESH_UUID.upper()}.json" if in_store is True else "export.json"
    )
    shutil.copy(CDB_PATH, export)
    data: dict[str, Any] = {CONF_CDB_PATH: str(export), CONF_UNICAST: "0D00"}
    if source is not None:
        data[CONF_SOURCE] = source
    entry = MockConfigEntry(domain=DOMAIN, unique_id="removable", data=data)
    entry.add_to_hass(hass)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert export.exists() is not removed
    # a missing file is not an error either
    if removed:
        entry2 = MockConfigEntry(domain=DOMAIN, unique_id="removable2", data=data)
        entry2.add_to_hass(hass)
        await hass.config_entries.async_remove(entry2.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("kind", "in_store", "name"),
    [
        ("gateway", True, f"{MESH_UUID.upper()}.json"),
        ("upload", True, f"{MESH_UUID.upper()}.json"),
        ("path", False, "export.json"),
        # a file of the user's own that happens to sit in our folder (it is named after the domain) stays theirs
        ("path", True, "JungHome.json"),
    ],
)
async def test_pre_source_entries_are_migrated(
    hass: HomeAssistant, tmp_path: Path, kind: str, in_store: bool, name: str
) -> None:
    """HAC-07: an entry from before `CONF_SOURCE` gets one before setup, judged like the flows would have set
    it — only a file the flows wrote (`<MESH UUID>.json` in our storage folder) is ours: with gateway credentials
    it came from the gateway, else from an upload; anything else is the user's path, which removal never deletes."""
    folder = _store(hass) if in_store else tmp_path
    folder.mkdir(parents=True, exist_ok=True)
    export = folder / name
    shutil.copy(CDB_PATH, export)
    data: dict[str, Any] = {CONF_CDB_PATH: str(export), CONF_UNICAST: "0D00"}
    if kind == "gateway":
        data |= {
            CONF_GATEWAY_HOST: "junghome.local",
            CONF_GATEWAY_TOKEN: "token",
            CONF_GATEWAY_FINGERPRINT: "ab" * 32,
        }
    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=1, data=data)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(
        entry.entry_id
    )  # the migration runs whether setup then works or not
    await hass.async_block_till_done()
    assert entry.data[CONF_SOURCE] == kind
    assert entry.minor_version == 2


def test_infer_source_counts_only_files_the_flows_wrote(hass: HomeAssistant) -> None:
    """HAC-07 re-review: the flows name the export `<MESH UUID>.json` (upper case) in our folder; nothing else in
    that folder is ours to delete or overwrite."""
    store = _store(hass)
    ours = str(store / f"{MESH_UUID.upper()}.json")
    creds = {
        CONF_GATEWAY_HOST: "junghome.local",
        CONF_GATEWAY_TOKEN: "token",
        CONF_GATEWAY_FINGERPRINT: "ab" * 32,
    }
    assert infer_source(hass, {CONF_CDB_PATH: ours}) == "upload"
    assert infer_source(hass, {CONF_CDB_PATH: ours, **creds}) == "gateway"
    assert (
        infer_source(hass, {CONF_CDB_PATH: ours, CONF_MESH_UUID: MESH_UUID}) == "upload"
    )
    other_mesh = "2BAF3ADE-0000-4000-8000-000000000001"
    assert (
        infer_source(hass, {CONF_CDB_PATH: ours, CONF_MESH_UUID: other_mesh}) == "path"
    )
    lower = str(store / f"{MESH_UUID.lower()}.json")
    assert infer_source(hass, {CONF_CDB_PATH: lower, **creds}) == "path"
    assert infer_source(hass, {CONF_CDB_PATH: str(store / "JungHome.json")}) == "path"


async def test_a_migrated_entry_keeps_its_source(hass: HomeAssistant) -> None:
    """HAC-07: an entry that already names its source only gets the new minor version."""
    data = {CONF_CDB_PATH: CDB_PATH, CONF_UNICAST: "0D00", CONF_SOURCE: "path"}
    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=1, data=data)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert (entry.data[CONF_SOURCE], entry.minor_version) == ("path", 2)


async def test_migrate_entry_leaves_other_versions_alone(hass: HomeAssistant) -> None:
    """HAC-07: HA only calls the migration for an older entry; called directly, it refuses a major version it does
    not know (a downgrade) and leaves an entry already at 1.2 as it is."""
    data = {CONF_CDB_PATH: CDB_PATH, CONF_UNICAST: "0D00"}
    newer = MockConfigEntry(domain=DOMAIN, version=2, data=data)
    newer.add_to_hass(hass)
    assert await async_migrate_entry(hass, newer) is False
    current = MockConfigEntry(
        domain=DOMAIN, unique_id="current", version=1, minor_version=2, data=data
    )
    current.add_to_hass(hass)
    assert await async_migrate_entry(hass, current) is True
    assert CONF_SOURCE not in current.data


# --------------------------------------------------------------------------- stored files (S5 / S8)


def _store(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(STORAGE_DIR))


async def test_removing_the_entry_takes_the_backup_and_orphaned_incoming_files(
    hass: HomeAssistant,
) -> None:
    """The stored export goes with its `.bak`; `.incoming-*` files no flow claims go too, a live flow's stays."""
    store = _store(hass)
    store.mkdir(parents=True)
    export = store / "1BAF3ADE-0000-4000-8000-000000000001.json"
    shutil.copy(CDB_PATH, export)
    bak = export.with_name(export.name + ".bak")
    shutil.copy(CDB_PATH, bak)
    orphan = store / ".incoming-deadbeef.json"
    orphan.write_text("{}")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="removable",
        data={CONF_CDB_PATH: str(export), CONF_UNICAST: "0D00", CONF_SOURCE: "gateway"},
    )
    entry.add_to_hass(hass)
    # a flow in progress owns its incoming file
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    live = store / f".incoming-{flow['flow_id']}.json"
    live.write_text("{}")
    issue_ids = [
        certificate_issue_id(entry.entry_id),
        f"gateway_sync_failed_{entry.entry_id}",
        f"gateway_token_rejected_{entry.entry_id}",
    ]
    for issue_id in issue_ids:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue_id.removesuffix(f"_{entry.entry_id}"),
            translation_placeholders={"host": "junghome.local"},
        )

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert sorted(p.name for p in store.iterdir()) == [live.name]
    for issue_id in issue_ids:
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    hass.config_entries.flow.async_abort(flow["flow_id"])


async def test_setup_sweeps_stale_incoming_files(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """`async_setup` deletes `.incoming-*` files older than an hour (a crashed flow), keeps fresh ones."""
    store = _store(hass)
    store.mkdir(parents=True)
    stale = store / ".incoming-old.json"
    stale.write_text("{}")
    old = time.time() - INCOMING_MAX_AGE - 60
    os.utime(stale, (old, old))
    fresh = store / ".incoming-new.json"
    fresh.write_text("{}")
    other = store / "1BAF3ADE-0000-4000-8000-000000000001.json"
    other.write_text("{}")
    os.utime(other, (old, old))
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert sorted(p.name for p in store.iterdir()) == [fresh.name, other.name]
    assert "Removed 1 stale incoming export file(s)" in caplog.text


def _malformed_export(tmp_path: Path, mutate: Any) -> Path:
    raw = json.loads(Path(CDB_PATH).read_text())
    mutate(raw["meshNetwork"])
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(raw))
    return bad


def test_sweep_incoming(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "junghome_ble"
    assert sweep_incoming(store, 0.0) == 0  # no store directory yet
    store.mkdir()
    for name in (".incoming-a.json", ".incoming-b.json", ".incoming-keep.json"):
        (store / name).write_text("{}")
    assert sweep_incoming(store, 0.0, frozenset({"keep"})) == 2
    assert [p.name for p in store.iterdir()] == [".incoming-keep.json"]
    assert sweep_incoming(store, INCOMING_MAX_AGE) == 0  # too young

    def refuse(self: Path) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", refuse)
    assert sweep_incoming(store, 0.0) == 0  # an undeletable file is skipped, not fatal
    assert [p.name for p in store.iterdir()] == [".incoming-keep.json"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda n: n["nodes"][0].__setitem__("unicastAddress", 5),
        lambda n: n.__setitem__("netKeys", []),
        lambda n: n.__setitem__("provisioners", "x"),
        lambda n: n.__setitem__("meshUUID", 7),
        lambda n: n["nodes"][0]["elements"][0].__setitem__("models", None),
    ],
    ids=["address_int", "no_netkeys", "provisioners_str", "uuid_int", "models_none"],
)
async def test_setup_error_when_export_is_malformed(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    tmp_path: Path,
    mutate: Any,
) -> None:
    """A document with the wrong types is a setup error with the translated reason, never a traceback."""
    bad = _malformed_export(tmp_path, mutate)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1fbd2c61a4b6e5a4",
        data={CONF_CDB_PATH: str(bad), CONF_UNICAST: "0D00"},
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "cannot_load"


async def test_setup_failure_in_start_stops_the_hub(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
) -> None:
    """HAC-13: the hub's stop is registered before it starts, so whatever `async_start` had set up before it
    failed (the advertisement callback) is torn down with the failed setup."""
    with patch(
        "custom_components.junghome_ble.coordinator.async_track_time_interval",
        side_effect=RuntimeError("boom"),
    ):
        mock_config_entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert mock_bluetooth_env["callbacks"] == []


async def test_removing_the_entry_offers_the_proxies_to_discovery_again(
    hass: HomeAssistant, network_id: bytes
) -> None:
    """Review-3 C7: the proxies were matched to the removed entry; without a rediscovery HA never offers them."""
    proxy = make_service_info(network_id)
    other = make_service_info(network_id, address="00:00:5E:00:53:99")
    other.service_data.clear()  # something else advertising: not ours to offer
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="removable",
        data={CONF_CDB_PATH: CDB_PATH, CONF_UNICAST: "0D00", CONF_SOURCE: "path"},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.junghome_ble.bluetooth.async_discovered_service_info",
            return_value=[proxy, other],
        ),
        patch(
            "custom_components.junghome_ble.bluetooth.async_rediscover_address"
        ) as rediscover,
    ):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
    rediscover.assert_called_once_with(hass, proxy.address)


@pytest.mark.parametrize(
    ("state", "reloads"),
    [(ConfigEntryState.LOADED, 1), (ConfigEntryState.SETUP_ERROR, 0)],
)
async def test_a_replayed_plan_reloads_the_entry_once_its_setup_finished(
    hass: HomeAssistant, state: ConfigEntryState, reloads: int
) -> None:
    """The reload after a replayed plan journal waits for the setup to end, runs once, and only for a set-up entry."""
    callbacks: list[Any] = []
    removed: list[Any] = []

    class Entry:
        entry_id = "entry"
        state = ConfigEntryState.SETUP_IN_PROGRESS

        def async_on_state_change(self, func: Any) -> Any:
            callbacks.append(func)
            return lambda: removed.append(func)

    entry = Entry()
    with patch.object(hass.config_entries, "async_schedule_reload") as schedule:
        _reload_once_loaded(hass, entry)  # type: ignore[arg-type]
        entry.state = state
        callbacks[0]()
        callbacks[0]()  # a later change: nothing more
        await hass.async_block_till_done()
    assert schedule.call_count == reloads
    assert removed == callbacks
