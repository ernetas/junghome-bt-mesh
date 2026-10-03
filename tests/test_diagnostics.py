"""Config entry and device diagnostics: a useful summary that never contains key material, host paths or MACs."""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
    get_diagnostics_for_device,
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
)
from custom_components.junghome_ble.coordinator import SEQ_RESTART_MARGIN
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.pdu import (
    encode_opcode,
)

from . import key_scan
from .conftest import (
    CDB_PATH,
    FIXTURES,
    META_DIR,
    PROXY_ADDRESS,
    FakeProxyLink,
    settle,
    setup_entry,
    wait_for_link,
)
from .helpers import (
    LIGHT_CTL,
    MESH_UUID,
    NODE_BLIND,
    NODE_GATEWAY,
    NODE_LIGHT_CTL,
    NODE_LIGHT_SWITCH,
    OUR_ADDRESS,
    PROPERTY_POWER_ON_TIME,
    SENSOR_POWER,
    SOCKET,
    SOCKET_SENSOR,
    UID_LIGHT_CTL,
    UID_ROCKER_A,
    admin_property_status,
    ctl_range_status,
    ctl_status,
    sensor_status,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

NODE_ROCKER = NODE_LIGHT_CTL.upper()
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", re.IGNORECASE)


def masked(text: str, unicast: str) -> str:
    """`text` as the diagnostics show it: the MAC half of the node UUID it contains replaced by the node's unicast."""
    return UUID_RE.sub(f"xxxxxxxx-xxxx-{unicast}", text.lower(), count=1)


GATEWAY, PHONE = (
    0x00DC,
    0x0001,
)  # the fixture network's gateway node and the provisioning phone
NODE_GATEWAY_ID = f"node:{NODE_GATEWAY}"
TOKEN = b"SECRET-TOKEN-abc123"  # the gateway's API token (vendor Manufacturer property 0xC001), 19 bytes
PID_TOKEN, PID_GATEWAY_IP, PID_FINGERPRINT = 0xC001, 0xC002, 0xC003
# the blinds network (`tests/test_cover.py`): a blinds actuator mini with a slat element, node 0500
BLINDS_PATH = str(FIXTURES / "Blinds.json")
BLIND, BLIND_SLAT = 0x0500, 0x0501
UUID_BLIND = NODE_BLIND.upper()
UID_BLIND = f"{UUID_BLIND.lower()}-0001"


def manufacturer_status(pid: int, value: bytes) -> bytes:
    """LBC Manufacturer Property Status `CB 27 05 [pid][access 1][value]`, as the gateway answers a read of its credentials."""
    return M.vendor_property_status("manufacturer", pid, value, user_access=1)


# every key of the networks these tests set up (the base one, the blinds one), derived keys included, and the
# gateway's API token: none may be in a diagnostics download, in any encoding (`key_scan.leaks`)
SECRETS = (
    key_scan.secrets(CDB.load(Path(CDB_PATH)))
    | key_scan.secrets(CDB.load(Path(BLINDS_PATH)))
    | {"gateway API token": TOKEN}
)


def assert_no_secrets(result: dict[str, Any]) -> None:
    """No key (`SECRETS`), host path or MAC in a diagnostics `result`; a MAC also not as the EUI-64 of a node UUID."""
    dump = json.dumps(result)
    assert not key_scan.leaks(dump, SECRETS)
    dump = dump.lower()
    for identity in (
        PROXY_ADDRESS.lower(),
        NODE_LIGHT_SWITCH[:18],  # the same MAC, as the EUI-64 inside node 0148's UUID
        NODE_LIGHT_CTL[:18],  # node 0232's
        NODE_BLIND[:18],  # the blinds network's node 0500
        CDB_PATH.lower(),
        META_DIR.lower(),
    ):
        assert identity not in dump


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    fake_link.inject(LIGHT_CTL, 0xC044, ctl_status(30000, 4000))
    fake_link.inject(LIGHT_CTL, 0xC044, ctl_range_status(2700, 6500))
    fake_link.inject(
        SOCKET_SENSOR,
        0xC001,
        sensor_status((SENSOR_POWER, (1234).to_bytes(2, "little"))),
    )
    fake_link.inject(
        SOCKET,
        OUR_ADDRESS,
        admin_property_status(PROPERTY_POWER_ON_TIME, (12345).to_bytes(3, "little")),
    )
    await hass.async_block_till_done()
    hub = init_integration.runtime_data
    hub.states[SOCKET].properties = {
        0x5003: b"\x06",
        0x5013: b"\x01\x00",
    }  # raw vendor property values, as a property status handler stores them

    result = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)

    assert_no_secrets(result)
    assert "key" not in result["network"]

    assert result["entry"] == {
        CONF_CDB_PATH: REDACTED,
        CONF_METADATA_DIR: REDACTED,
        CONF_UNICAST: "0D00",
        CONF_SOURCE: "path",  # given by the entry migration (HAC-07)
    }  # paths describe the host
    assert result["network"] == {
        "mesh_uuid": "1BAF3ADE-0000-4000-8000-000000000001",
        "network_id": "1fbd2c61a4b6e5a4",  # public: it is what the nodes advertise
        "nodes": 7,
        "groups": 18,
        "scenes": [1, 2],
    }
    seq = (
        init_integration.runtime_data.proxy.state.seq
    )  # how many PDUs the start-up refresh took is the hub's business
    assert seq > 0
    headroom = init_integration.runtime_data.state.durable_headroom
    # a healthy store: written, so the restart margin lies ahead of us; nothing held back, no write failed
    assert 0 < headroom <= SEQ_RESTART_MARGIN
    assert result["local"] == {
        "src": "0D00",
        "seq": seq,
        "iv_index": 0,
        "iv_update_active": False,
        "stalled_for": None,
        "last_write_error": None,
        "durable_headroom": headroom,
        "address_shared": None,
    }

    # no key refresh, no device Home Assistant added (review-4 D11: phases and Network IDs only, never a key)
    assert result["key_refresh"] == {
        "phase": 0,
        "proven_phase": None,
        "vault_nodes": {},
        "lagging": [],
    }
    # every node answered, or is still within its three misses
    assert result["unreachable"] == []
    init_integration.runtime_data.unreachable.add(0x0300)
    again = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    assert again["unreachable"] == ["0300"]
    init_integration.runtime_data.unreachable.clear()

    link = result["link"]
    assert link["connected"] is True
    assert (
        link["proxy_address"] == REDACTED
    )  # a Bluetooth MAC; the mesh address below says which node it is
    assert link["proxy_node"] == "0148"
    assert isinstance(link["connected_since"], float)
    assert (link["mtu"], link["proxy_config_dropped"]) == (247, 0)
    assert link["visible_proxies"] == [
        {"address": REDACTED, "rssi": -50, "node": "0148"}
    ]  # the MAC names the node
    assert link["unknown_nodes"] == []

    devices = result["devices"]
    assert [light["address"] for light in devices["lights"]] == [
        "0148",
        "0232",
        "0300",
        "0400",
        "0401",
    ]
    assert devices["lights"][1] == {
        "address": "0232",
        "name": "Living room DALI",
        "kind": "ctl",
        "rooms": ["Living room"],
        "meter": None,
    }
    assert devices["sockets"] == [
        {"address": "0172", "name": "Boiler", "sensor": "0173"}
    ]
    assert [button["address"] for button in devices["buttons"]] == [
        "0149",
        "0234",
        "0235",
        "0301",
    ]
    assert (
        devices["buttons"][0]
        == {
            "address": "0149",
            "name": "WC mirror button A",
            "device": "xxxxxxxx-xxxx-0148-0000-000000000000-0040-buttons",  # the node UUID's MAC half masked
        }
    )
    assert devices["scenes"] == [
        {"number": 1, "name": "WC off", "timer": False},
        {"number": 2, "name": "All off", "timer": False},
    ]

    assert set(result["states"]) == {"0232", "0172"}
    assert result["states"]["0232"]["lightness"] == 30000
    assert result["states"]["0232"]["kelvin"] == 4000
    assert result["states"]["0232"]["on"] is True
    assert (
        result["states"]["0232"]["kelvin_min"],
        result["states"]["0232"]["kelvin_max"],
    ) == (2700, 6500)
    assert result["states"]["0232"]["properties"] == {}
    assert result["states"]["0172"]["power_w"] == 123.4
    assert result["states"]["0172"]["on"] is None
    assert result["states"]["0172"]["power_on_hours"] == 12345
    assert (
        result["states"]["0172"]["energy_wh"] is None
    )  # never read in this test; the field exists
    assert result["states"]["0172"]["properties"] == {
        "5003": "06",
        "5013": "0100",
    }  # bytes are not JSON: hex pid → hex value
    assert hub.states[SOCKET].properties == {
        0x5003: b"\x06",
        0x5013: b"\x01\x00",
    }  # the cache itself is untouched


async def test_diagnostics_list_the_open_carry_over_conflicts(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
) -> None:
    """Review-4 W4-5: the paths of the open `carry_over_conflict` repair, every UUID in them redacted."""
    result = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    assert result["carry_over_conflicts"] == []
    issue = f"carry_over_conflict_{init_integration.entry_id}"
    paths = [
        "network.groups[C64B]",
        "network.nodes[30FB10FFFE1234560000000000000000].elements[0].models[1000].subscribe",
    ]
    for data in (None, {"paths": "\n".join(paths)}):
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="carry_over_conflict",
            translation_placeholders={"title": "x", "paths": "", "held": ""},
            data=data,
        )
        again = await get_diagnostics_for_config_entry(
            hass, hass_client, init_integration
        )
        if data is None:
            assert again["carry_over_conflicts"] == []
    assert again["carry_over_conflicts"] == [
        "network.groups[C64B]",
        f"network.nodes[{REDACTED}].elements[0].models[1000].subscribe",
    ]
    ir.async_delete_issue(hass, DOMAIN, issue)


async def test_the_gateway_token_never_reaches_the_cache_or_the_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The phone app reads the gateway's credentials over the mesh (Manufacturer properties 0xC001-0xC003) and the
    proxy forwards the gateway's answers to us: the token and the fingerprint are not cached, so neither diagnostics
    download can carry the API token (which hands out the export with every mesh key), and the mesh log redacts it.
    The address is cached (the gateway's *IP address* sensor shows it) and redacted from the diagnostics."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(
        logging.DEBUG, logger="jhmesh"
    )  # whatever level an earlier test left the mesh logger at
    hub = init_integration.runtime_data
    fake_link.inject(GATEWAY, PHONE, manufacturer_status(PID_TOKEN, TOKEN))
    fake_link.inject(
        GATEWAY, PHONE, manufacturer_status(PID_GATEWAY_IP, b"192.168.1.20")
    )
    fake_link.inject(GATEWAY, PHONE, manufacturer_status(PID_FINGERPRINT, b"AB" * 16))
    await settle(hass)
    assert (
        "gateway_api_token=<redacted>" in caplog.text
    )  # the Status was decoded and logged ...
    assert TOKEN.decode() not in caplog.text
    assert TOKEN.hex() not in caplog.text  # ... without its value
    kept = hub.states[GATEWAY].properties
    assert PID_TOKEN not in kept  # nothing of the gateway's credentials is kept ...
    assert PID_FINGERPRINT not in kept
    assert kept[PID_GATEWAY_IP] == b"192.168.1.20"  # ... but the address it shows

    dump = json.dumps(
        await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    )
    assert "C001" not in dump
    assert "192.168.1.20" not in dump
    assert b"192.168.1.20".hex() not in dump
    assert TOKEN.decode() not in dump
    assert TOKEN.hex() not in dump

    # belt and braces: a secret that did land in the cache is redacted by the diagnostics themselves
    hub.states.setdefault(GATEWAY, hub.element_state(GATEWAY)).properties[PID_TOKEN] = (
        TOKEN
    )
    hub.states[GATEWAY].properties[PID_GATEWAY_IP] = b"192.168.1.20"
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, NODE_GATEWAY_ID), init_integration.entry_id
    )
    assert device is not None
    for result in (
        await get_diagnostics_for_config_entry(hass, hass_client, init_integration),
        await get_diagnostics_for_device(hass, hass_client, init_integration, device),
    ):
        dump = json.dumps(result)
        assert TOKEN.decode() not in dump
        assert TOKEN.hex() not in dump
        assert result["states"]["00DC"]["properties"] == {
            "C001": REDACTED,
            "C002": REDACTED,
        }


async def test_gateway_entry_data_is_redacted(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    tmp_path: Path,
) -> None:
    """An entry set up from the gateway carries its host and API token: both are redacted, the rest is shown."""
    export = tmp_path / "export.json"
    shutil.copy(CDB_PATH, export)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME via gateway",
        unique_id="1fbd2c61a4b6e5a4",
        data={
            CONF_CDB_PATH: str(export),
            CONF_METADATA_DIR: "",
            CONF_UNICAST: "0D00",
            CONF_MESH_UUID: MESH_UUID.upper(),
            CONF_SOURCE: "gateway",
            CONF_GATEWAY_HOST: "junghome-1234.local",
            CONF_GATEWAY_TOKEN: "gw-token-0123456789abcdef",
            CONF_GATEWAY_FINGERPRINT: "ab" * 32,
        },
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)

    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert (
        result["entry"]
        == {
            CONF_CDB_PATH: REDACTED,
            CONF_METADATA_DIR: "",  # nothing to redact in an empty value
            CONF_UNICAST: "0D00",
            CONF_MESH_UUID: MESH_UUID.upper(),
            CONF_SOURCE: "gateway",
            CONF_GATEWAY_HOST: REDACTED,
            CONF_GATEWAY_TOKEN: REDACTED,
            CONF_GATEWAY_FINGERPRINT: REDACTED,  # PLT-08: a stable, unique id of the user's gateway
        }
    )
    dump = json.dumps(result)
    assert "ab" * 32 not in dump
    assert "junghome-1234" not in dump
    assert "gw-token" not in dump
    assert str(tmp_path) not in dump
    assert_no_secrets(result)


async def test_diagnostics_without_link(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)

    result = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    assert result["link"] == {
        "connected": False,
        "proxy_address": None,
        "proxy_node": None,
        "connected_since": None,
        "mtu": init_integration.runtime_data.proxy.mtu,
        "proxy_config_dropped": 0,
        "visible_proxies": [],
        "unknown_nodes": [],
    }
    assert result["states"] == {}


@pytest.mark.parametrize(
    "identifier",
    [f"node:{NODE_ROCKER.lower()}", UID_LIGHT_CTL, f"{UID_ROCKER_A}-buttons"],
)
async def test_device_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    identifier: str,
) -> None:
    """The node device and the load / buttons devices hanging off it all describe that node."""
    fake_link.inject(LIGHT_CTL, 0xC044, ctl_status(30000, 4000))
    await hass.async_block_till_done()
    init_integration.runtime_data.states[LIGHT_CTL].properties[0x1001] = b"\x02"
    hub = init_integration.runtime_data
    for name, raw in {
        "software_version": b"02020002",
        "hardware_revision": b"10000000" + bytes(8),
        "manufacturer_name": b"Albrecht Jung GmbH & Co.KG" + bytes(10),
        "secure_element_version": bytes.fromhex("0d020100"),
        "bootloader_version": b"",  # does not decode: shown raw
        "time_role": b"\x03",
        "from_a_later_version": b"\x01",
        "stm32_version.unsupported": b"02020002",  # answered without a value
        "software_version.unsupported": b"01000000",  # the value it has now wins
    }.items():
        hub.remember_node_info(0x0232, name, raw)
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, identifier), init_integration.entry_id
    )
    assert device is not None

    result = await get_diagnostics_for_device(
        hass, hass_client, init_integration, device
    )
    # the app's node details (A18): what the node told about itself, decoded
    assert result["node_info"] == {
        "bootloader_version": "",
        "from_a_later_version": "01",
        "hardware_revision": "10000000",
        "manufacturer_name": "Albrecht Jung GmbH & Co.KG",
        "secure_element_version": "0.1.2.13",
        "software_version": "2.2.0.2",
        "stm32_version": "not supported",
        "time_role": "client",
    }

    assert_no_secrets(result)
    assert result["identifiers"] == [masked(identifier, "0232")]
    node = result["node"]
    assert node["uuid"] == masked(NODE_ROCKER, "0232")
    assert node["name"] == "Push-button 2-gang"
    assert node["unicast"] == "0232"
    assert node["pid"] == 2
    assert node["product"] == "Push-button 2-gang"
    assert [(e["address"], e["location"]) for e in node["elements"]] == [
        ("0232", "0001"),
        ("0233", "0001"),
        ("0234", "0040"),
        ("0235", "0041"),
        ("0236", "0044"),
    ]
    assert "1303" in node["elements"][0]["models"]
    assert set(node) == {"uuid", "name", "unicast", "pid", "product", "elements"}

    assert result["devices"] == {
        "lights": [
            {
                "address": "0232",
                "name": "Living room DALI",
                "kind": "ctl",
                "rooms": ["Living room"],
                "meter": None,
            }
        ],
        "sockets": [],
        "buttons": [
            {
                "address": "0234",
                "name": "Living room rocker A",
                "device": masked(f"{UID_ROCKER_A}-buttons", "0232"),
            },
            {
                "address": "0235",
                "name": "Living room rocker B",
                "device": masked(f"{UID_ROCKER_A}-buttons", "0232"),
            },
        ],
        "blinds": [],
        "thermostats": [],
        "detectors": [],
    }
    assert set(result["states"]) == {
        "0232"
    }  # only this node's elements, only those heard from
    assert result["states"]["0232"]["lightness"] == 30000
    assert result["states"]["0232"]["kelvin"] == 4000
    assert result["states"]["0232"]["properties"] == {"1001": "02"}


async def test_device_diagnostics_of_a_blind_device(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """A blind device (`{uuid}-{location}` like a light or socket) resolves to its node like the other device kinds."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME blinds test",
        unique_id="1fbd2c61a4b6e5a4",
        data={CONF_CDB_PATH: BLINDS_PATH, CONF_UNICAST: "0D00"},
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    fake_link.inject(
        BLIND,
        OUR_ADDRESS,
        encode_opcode(M.GEN_LEVEL_STATUS) + (1000).to_bytes(2, "little"),
    )
    await settle(hass)
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, UID_BLIND), entry.entry_id
    )
    assert device is not None
    assert device.model == "Blind / shutter drive"

    result = await get_diagnostics_for_device(hass, hass_client, entry, device)

    assert_no_secrets(result)
    assert result["identifiers"] == [masked(UID_BLIND, "0500")]
    node = result["node"]
    assert (node["uuid"], node["unicast"], node["pid"]) == (
        masked(UUID_BLIND, "0500"),
        "0500",
        13,
    )
    assert node["product"] == "Blinds actuator 1-gang mini"
    assert [(e["address"], e["location"]) for e in node["elements"]] == [
        ("0500", "0001"),
        ("0501", "0001"),
        ("0502", "0040"),
        ("0503", "0041"),
    ]
    assert result["devices"]["blinds"] == [
        {
            "address": "0500",
            "name": "Kitchen blind",
            "slat": "0501",
            "rooms": ["Kitchen"],
        }
    ]
    assert result["devices"]["lights"] == []
    assert [b["address"] for b in result["devices"]["buttons"]] == ["0502", "0503"]
    assert set(result["states"]) == {"0500"}
    assert (
        result["states"]["0500"]["level"],
        result["states"]["0500"]["target_level"],
    ) == (1000, 1000)


async def test_device_diagnostics_of_the_mesh_device(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
) -> None:
    """The service device stands for the network: same content as the config entry diagnostics."""
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"mesh:{MESH_UUID}"), init_integration.entry_id
    )
    assert device is not None
    result = await get_diagnostics_for_device(
        hass, hass_client, init_integration, device
    )
    assert result == await get_diagnostics_for_config_entry(
        hass, hass_client, init_integration
    )
    assert_no_secrets(result)


async def test_device_diagnostics_of_an_unknown_device(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
) -> None:
    """A device that is not in the current export (only possible until the next reload prunes it) yields no node."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=init_integration.entry_id,
        identifiers={(DOMAIN, "node:gone"), ("other", "x")},
    )
    result = await get_diagnostics_for_device(
        hass, hass_client, init_integration, device
    )
    assert result == {"identifiers": ["node:gone"], "node": None}
