"""Config entry and device diagnostics: a useful summary that never contains key material, host paths or MACs."""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import asdict, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import ConfigEntryState
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
    OPTION_CLICK_DELAY,
    OPTION_HEARTBEATS,
)
from custom_components.junghome_ble.coordinator import SEQ_RESTART_MARGIN
from custom_components.junghome_ble.diagnostics import redact_paths
from custom_components.junghome_ble.hub.issues import Issues
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.client import MESH_PROXY_SERVICE
from custom_components.junghome_ble.jhmesh.pdu import (
    encode_opcode,
)
from custom_components.junghome_ble.jhmesh.stats import LinkStats

from . import key_scan
from .conftest import (
    CDB_PATH,
    FIXTURES,
    META_DIR,
    PROXY_ADDRESS,
    FakeProxyLink,
    make_service_info,
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
    assert (link["unknown_nodes"], link["history"]) == ([], [])  # the first link is up

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


async def test_diagnostics_count_what_the_link_carried(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """`link_stats` (review-4 A4-14): the current link's counts, every link's since the entry loaded, and the links."""
    fake_link.inject(LIGHT_CTL, 0xC044, ctl_status(30000, 4000))
    fake_link.inject(
        SOCKET,
        OUR_ADDRESS,
        admin_property_status(PROPERTY_POWER_ON_TIME, (12345).to_bytes(3, "little")),
    )
    await hass.async_block_till_done()
    hub = init_integration.runtime_data

    result = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)

    stats = result["link_stats"]
    assert stats["links"] == 1
    # the first link is all there was
    assert stats["current"] == stats["total"] == asdict(hub.proxy.link_stats)
    assert list(stats["current"]) == [f.name for f in fields(LinkStats)]
    current = stats["current"]
    assert current["messages"] >= 2  # the two injected above
    assert current["messages_to_us"] >= 1  # the power-on time, unicast to us
    assert (current["undecryptable"], current["replays_dropped"]) == (0, 0)


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
    fake_link.inject(GATEWAY, PHONE, manufacturer_status(PID_GATEWAY_IP, b"192.0.2.20"))
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
    assert kept[PID_GATEWAY_IP] == b"192.0.2.20"  # ... but the address it shows

    dump = json.dumps(
        await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    )
    assert "C001" not in dump
    assert "192.0.2.20" not in dump
    assert b"192.0.2.20".hex() not in dump
    assert TOKEN.decode() not in dump
    assert TOKEN.hex() not in dump

    # belt and braces: a secret that did land in the cache is redacted by the diagnostics themselves
    hub.states.setdefault(GATEWAY, hub.element_state(GATEWAY)).properties[PID_TOKEN] = (
        TOKEN
    )
    hub.states[GATEWAY].properties[PID_GATEWAY_IP] = b"192.0.2.20"
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


@pytest.fixture
def no_time_keeper_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The time keeper repair stays shut from before the setup on (requested ahead of `init_integration`)."""
    monkeypatch.setattr(Issues, "report_time_keeper", lambda self: None)


async def test_diagnostics_include_the_options_and_the_open_repairs(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    no_time_keeper_check: None,
    init_integration: MockConfigEntry,
) -> None:
    """Review-4 H4-6: the options are part of the picture, and so are the repairs this integration has open.

    The fixture project has a PP2 puck, so the time keeper repair (F4-14) may open on its own once every candidate
    answered its time role, sooner or later under load: it is kept out here, the issues listed are the test's own.
    """
    result = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    assert (result["options"], result["issues"]) == ({}, [])
    assert result["plans"] == []  # no action ran a plan yet (review-4 W I7)
    hass.config_entries.async_update_entry(
        init_integration, options={OPTION_CLICK_DELAY: True}
    )
    await hass.async_block_till_done()  # the entry reloads with them
    issue = f"unknown_nodes_{init_integration.entry_id}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="unknown_nodes",
    )
    ir.async_create_issue(
        hass,
        "other_domain",
        "not_ours",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="x",
    )
    result = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    assert result["options"] == {OPTION_CLICK_DELAY: True}
    assert result["issues"] == [issue]


async def test_diagnostics_show_the_node_clocks_but_not_their_location(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Each node's clock as it last answered (`node_clocks.py`); its stored location only as compared with home."""
    result = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    assert result["clocks"] == {}
    latitude, longitude = hass.config.latitude, hass.config.longitude
    fake_link.inject(
        LIGHT_CTL,
        OUR_ADDRESS,
        encode_opcode(M.GEN_LOCATION_GLOBAL_STATUS)
        + M.generic_location_global_set(latitude, longitude, 12)[1:],
    )
    fake_link.inject(LIGHT_CTL, OUR_ADDRESS, encode_opcode(M.TIME_STATUS) + bytes(5))
    await settle(hass)
    result = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    [clock] = result["clocks"].values()
    assert list(result["clocks"]) == ["0232"]
    assert clock["read"] is not None
    del clock["read"]
    assert clock == {
        "offset": None,
        "has_time": False,  # it answered it has no time
        "zone_offset": None,
        "zone_expected": None,
        "location": "home",
        "wrong": "no time",
    }
    text = json.dumps(result)
    assert f"{latitude:.3f}" not in text
    assert f"{longitude:.3f}" not in text
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"node:{NODE_ROCKER.lower()}"), init_integration.entry_id
    )
    assert device is not None
    node = await get_diagnostics_for_device(hass, hass_client, init_integration, device)
    assert node["clock"]["location"] == "home"
    assert node["clock"]["wrong"] == "no time"


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
    [ended] = result["link"].pop("history")  # the link that just went
    # no link up: the counts are the last link's, the only one there was
    assert result["link_stats"]["links"] == 1
    assert result["link_stats"]["current"] == result["link_stats"]["total"]
    assert (ended["reason"], ended["proxy_node"]) == ("the proxy disconnected", "0148")
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
    assert result["clock"] is None  # no Time Status from it yet

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


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        (None, None),
        (
            "OSError: [Errno 30] Read-only file system",
            "OSError: [Errno 30] Read-only file system",
        ),
        (
            "[Errno 28] No space left on device: '/srv/someone/ha/.storage/junghome_ble.seq'",
            f"[Errno 28] No space left on device: {REDACTED}",
        ),
        (
            "[Errno 18] Invalid cross-device link: '/config/a.tmp' -> \"/config/it's.json\"",
            f"[Errno 18] Invalid cross-device link: {REDACTED} -> {REDACTED}",
        ),
        ("cannot open /config/x.json, giving up", f"cannot open {REDACTED}, giving up"),
        # a home-relative path, joined at run time so tools/privacy_scan.py does not take it for a real one
        (f"cannot open {Path('~') / 'x.json'}", f"cannot open {REDACTED}"),
        (
            "read/write 1/3 failed",
            "read/write 1/3 failed",
        ),  # no path: a slash inside a word stays
    ],
)
def test_paths_are_redacted_from_error_texts(
    text: str | None, shown: str | None
) -> None:
    """An OS error's text names its file, whose path can carry the user's name: the path goes, the error stays."""
    assert redact_paths(text) == shown


async def test_the_store_write_error_is_shown_without_its_path(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
) -> None:
    """`local.last_write_error` is the raw `WriteError` text, which ends in the file it failed on."""
    store = init_integration.runtime_data.state._store
    store.write_error = "[Errno 30] Read-only file system: '/srv/someone/.storage/x'"
    try:
        result = await get_diagnostics_for_config_entry(
            hass, hass_client, init_integration
        )
    finally:
        store.write_error = None
    assert result["local"]["last_write_error"] == (
        f"[Errno 30] Read-only file system: {REDACTED}"
    )
    assert "someone" not in json.dumps(result)


OTHER_MESH = bytes.fromhex("1122334455667788")  # another installation's Network ID


async def test_diagnostics_while_the_entry_retries(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
) -> None:
    """Review-4 H4-6: an entry waiting for a proxy node has no hub; its download used to fail (HTTP 500) exactly when
    it would help. It shows the state and why, the options, what Bluetooth sees and the export's summary instead."""
    mock_bluetooth_env["infos"] = [
        make_service_info(OTHER_MESH, address="00:00:5E:00:53:99", rssi=-70)
    ]  # a proxy, but of another mesh
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={OPTION_CLICK_DELAY: True, OPTION_HEARTBEATS: False}
    )
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY

    result = await get_diagnostics_for_config_entry(
        hass, hass_client, mock_config_entry
    )

    assert_no_secrets(result)
    assert result["entry"] == {
        CONF_CDB_PATH: REDACTED,
        CONF_METADATA_DIR: REDACTED,
        CONF_UNICAST: "0D00",
        CONF_SOURCE: "path",
    }
    assert result["options"] == {OPTION_CLICK_DELAY: True, OPTION_HEARTBEATS: False}
    assert (result["state"], result["reason_key"]) == (
        "setup_retry",
        "no_proxy_visible",
    )
    assert result["network"] == {
        "mesh_uuid": "1BAF3ADE-0000-4000-8000-000000000001",
        "network_id": "1fbd2c61a4b6e5a4",
        "nodes": 7,
        "groups": 18,
        "scenes": [1, 2],
    }
    assert result["bluetooth"] == {
        "connectable_scanners": 1,
        "proxies": [
            {
                "address": REDACTED,
                "rssi": -70,
                "kind": "network_id",
                "matches_export": False,
                "node": None,
            }
        ],
    }
    assert result["issues"] == []
    assert "00:00:5e:00:53:99" not in json.dumps(result).lower()


async def test_diagnostics_of_an_unloaded_entry_check_the_proxies_against_the_export(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    """Unloaded, the proxies are matched against the export: its Network ID, a Node Identity that resolves to one of
    its nodes (named by address), one that does not, a Mesh 1.1 private kind; a device page falls back to the same."""
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"mesh:{MESH_UUID}"), init_integration.entry_id
    )
    assert device is not None
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert init_integration.state is ConfigEntryState.NOT_LOADED
    netkey = CDB.load(Path(CDB_PATH)).net_keys[0]
    rnd = bytes(range(8))

    def advert(address: str, rssi: int, data: bytes) -> Any:
        info = make_service_info(b"", address=address, rssi=rssi)
        info.service_data[MESH_PROXY_SERVICE] = data
        return info

    mock_bluetooth_env["infos"] = [
        mock_bluetooth_env["infos"][0],  # ours, by Network ID (-50)
        advert(
            "00:00:5E:00:53:21",
            -40,
            b"\x01" + netkey.node_identity_hash(rnd, 0x0232) + rnd,
        ),
        advert("00:00:5E:00:53:22", -60, b"\x01" + bytes(8) + rnd),
        advert("00:00:5E:00:53:23", -80, b"\x02" + bytes(16)),
        advert(
            "00:00:5E:00:53:25",
            -70,
            b"\x03" + netkey.private_node_identity(rnd, 0x0232) + rnd,
        ),
        advert("00:00:5E:00:53:26", -85, b"\x04" + bytes(16)),
        advert(
            "00:00:5E:00:53:24", -90, b""
        ),  # no proxy service data at all: not listed
    ]
    result = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    assert_no_secrets(result)
    assert result["state"] == "not_loaded"
    assert result["bluetooth"]["proxies"] == [
        {"address": REDACTED, "rssi": -40, "kind": "node_identity", "matches_export": True, "node": "0232"},
        {"address": REDACTED, "rssi": -50, "kind": "network_id", "matches_export": True, "node": None},
        {"address": REDACTED, "rssi": -60, "kind": "node_identity", "matches_export": False, "node": None},
        {"address": REDACTED, "rssi": -70, "kind": "private_node_identity", "matches_export": True, "node": "0232"},
        {"address": REDACTED, "rssi": -80, "kind": "private_network_identity", "matches_export": False, "node": None},
        {"address": REDACTED, "rssi": -85, "kind": "type 04", "matches_export": False, "node": None},
    ]  # fmt: skip
    assert "00:00:5e:00:53:2" not in json.dumps(result).lower()
    assert (
        await get_diagnostics_for_device(hass, hass_client, init_integration, device)
        == result
    )


async def test_diagnostics_when_the_export_does_not_load(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    tmp_path: Path,
) -> None:
    """A failed entry (its export gone) still downloads: the error's kind, never its path; no proxy can be matched."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={CONF_CDB_PATH: str(tmp_path / "gone.json"), CONF_UNICAST: "0D00"},
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR

    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert (result["state"], result["reason_key"]) == ("setup_error", "cannot_load")
    assert result["network"] == {"error": "FileNotFoundError"}
    assert result["bluetooth"]["proxies"] == [
        {
            "address": REDACTED,
            "rssi": -50,
            "kind": "network_id",
            "matches_export": None,
            "node": None,
        }
    ]
    assert str(tmp_path) not in json.dumps(result)
