"""`junghome_ble.audit_network`: the read-only Configuration Server audit as an action, and its diagnostics trace;
`junghome_ble.locate_node`, the other device-key action of review-4 F4-15.

The hub's real client asks the fake link's nodes, whose Configuration Servers answer from the fixture export
(`FakeConfigServers`, device-key crypto and segmentation included); `jhmesh.audit` itself is tested in
`tests/jhmesh/test_audit.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
    get_diagnostics_for_device,
)

from custom_components.junghome_ble import coordinator
from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh.pdu import decode_opcode, encode_opcode

from .helpers import MESH_UUID, NODE_GATEWAY, UID_LIGHT_CTL, UID_LIGHT_SWITCH
from .jhmesh.conftest import FakeConfigServers

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

    from custom_components.junghome_ble.jhmesh.cdb import CDB

    from .conftest import FakeProxyLink

GATEWAY, SWITCH, CTL, SOCKET_NODE, DIMMER, ACTUATOR = (
    0x00DC,
    0x0148,
    0x0232,
    0x0172,
    0x0300,
    0x0400,
)
MAINS = [
    "00DC",
    "0148",
    "0172",
    "0232",
    "0300",
    "0400",
]  # every provisioned node but the app's phone
GETS = {
    C.CONFIG_RELAY_GET,
    C.CONFIG_NETWORK_TRANSMIT_GET,
    C.CONFIG_DEFAULT_TTL_GET,
    C.CONFIG_BEACON_GET,
    C.CONFIG_GATT_PROXY_GET,
    C.CONFIG_FRIEND_GET,
    C.CONFIG_NETKEY_GET,
    C.CONFIG_APPKEY_GET,
    C.CONFIG_MODEL_PUBLICATION_GET,
    C.CONFIG_SIG_MODEL_SUBSCRIPTION_GET,
    C.CONFIG_VENDOR_MODEL_SUBSCRIPTION_GET,
    C.CONFIG_SIG_MODEL_APP_GET,
    C.CONFIG_VENDOR_MODEL_APP_GET,
}
GATEWAY_RESULT = {
    "name": "Gateway",
    "answered": True,
    "settings": {
        "relay": {"export": 1, "node": 1},
        "relay_retransmit": {"export": None, "node": {"count": 3, "interval": 90}},
        "network_transmit": {"export": None, "node": {"count": 3, "interval": 100}},
        "default_ttl": {"export": 5, "node": 5},
        "beacon": {"export": None, "node": True},
        "gatt_proxy": {"export": 1, "node": 1},
        "friend": {"export": 2, "node": 2},
    },
    "keys": {
        "net_keys": {"export": [0], "node": [0]},
        "app_keys": {"export": [0], "node": [0]},
    },
    "models": 4,
    "findings": [],
}
SCENE_FINDING = {
    "kind": "scene_subscriptions_missing",
    "element": "0148",
    "model": "1203",
    "expected": ["C00F", "FEF5"],
}


@pytest.fixture
def servers(cdb: CDB, fake_link: FakeProxyLink) -> FakeConfigServers:
    config = FakeConfigServers(cdb)
    fake_link.config_reply = config
    return config


@pytest.fixture(autouse=True)
def short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A silent node's Gets time out in milliseconds (the chunk pauses are `fast_sleep`'s)."""
    monkeypatch.setattr(coordinator, "AUDIT_TIMEOUT", 0.05)


async def audit(hass: HomeAssistant, **data: Any) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN, "audit_network", data, blocking=True, return_response=True
    )
    assert response is not None
    return dict(response)


def device_id(hass: HomeAssistant, entry: MockConfigEntry, identifier: str) -> str:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, identifier), entry.entry_id
    )
    assert device is not None, identifier
    return device.id


def asked(fake_link: FakeProxyLink) -> set[int]:
    return {node for _src, node, _pdu in fake_link.config_sent}


async def test_every_mains_node_is_audited_and_battery_nodes_are_skipped(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    servers: FakeConfigServers,
) -> None:
    """No target: every provisioned mains node; the silent ones reported, a battery node left out and listed."""
    hub = init_integration.runtime_data
    battery = next(n for n in hub.cdb.nodes if n.unicast == DIMMER)
    battery.pid = 0x0005  # a battery wall transmitter, as far as this call is concerned
    servers.silent |= {SWITCH, CTL, SOCKET_NODE, ACTUATOR}
    fake_link.config_sent.clear()

    response = await audit(hass)

    assert response == {
        "nodes": {
            "00DC": GATEWAY_RESULT,
            **{
                node: {
                    "name": hub.cdb.node_by_addr(int(node, 16)).name,
                    "answered": False,
                    "settings": {},
                    "keys": {},
                    "models": 0,
                    "findings": [{"kind": "node_unanswered"}],
                }
                for node in ("0148", "0172", "0232", "0400")
            },
        },
        "unanswered": ["0148", "0172", "0232", "0400"],
        "findings": 4,
        "skipped": ["0300"],
    }
    assert asked(fake_link) == {GATEWAY, SWITCH, CTL, SOCKET_NODE, ACTUATOR}
    # read-only: every device-key message was a Get
    assert {decode_opcode(pdu)[0] for _src, _node, pdu in fake_link.config_sent} <= GETS
    assert sorted(f"{u:04X}" for u in hub.audits) == [n for n in MAINS if n != "0300"]


async def test_the_mesh_device_audits_the_network_and_the_diagnostics_keep_the_result(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    servers: FakeConfigServers,
) -> None:
    servers.subscribe[SWITCH, "1203"] = [0xC061]  # as on air: the element group only
    mesh = device_id(hass, init_integration, f"mesh:{MESH_UUID}")

    response = await audit(hass, device=mesh)

    assert sorted(response["nodes"]) == MAINS
    assert response["unanswered"] == response["skipped"] == []
    assert response["findings"] == 1
    assert response["nodes"]["0148"]["findings"] == [SCENE_FINDING]
    assert response["nodes"]["0148"]["models"] == 30
    assert response["nodes"]["00DC"] == GATEWAY_RESULT

    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, init_integration
    )
    assert diagnostics["audit"] == response["nodes"]
    switch = dr.async_get(hass).async_get(
        device_id(hass, init_integration, UID_LIGHT_SWITCH)
    )
    assert switch is not None
    node = await get_diagnostics_for_device(hass, hass_client, init_integration, switch)
    assert node["audit"] == response["nodes"]["0148"]
    keys = [n.dev_key.hex() for n in init_integration.runtime_data.cdb.nodes]
    assert not any(key in json.dumps(diagnostics).lower() for key in keys)


@pytest.mark.parametrize(
    ("identifier", "node"),
    [(UID_LIGHT_CTL, "0232"), (f"node:{NODE_GATEWAY}", "00DC")],
)
async def test_a_device_audits_its_node_only(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    servers: FakeConfigServers,
    identifier: str,
    node: str,
) -> None:
    """A load device, or a node device itself: that node, whatever powers it."""
    fake_link.config_sent.clear()
    response = await audit(hass, device=device_id(hass, init_integration, identifier))
    assert list(response["nodes"]) == [node]
    assert response["skipped"] == []
    assert asked(fake_link) == {int(node, 16)}


async def test_a_node_missing_an_appkey_is_a_finding(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    servers: FakeConfigServers,
) -> None:
    """The keys a node holds, by index only: AppKey 0 missing is reported, and no key reaches the answer."""
    servers.node_app_keys[GATEWAY] = []
    response = await audit(
        hass, device=device_id(hass, init_integration, f"node:{NODE_GATEWAY}")
    )
    gateway = response["nodes"]["00DC"]
    assert gateway["keys"]["app_keys"] == {"export": [0], "node": []}
    assert gateway["findings"] == [
        {"kind": "keys_missing", "setting": "app_keys", "expected": [0]}
    ]
    cdb = init_integration.runtime_data.cdb
    keys = [
        *(k.key.hex() for k in (*cdb.net_keys.values(), *cdb.app_keys.values())),
        *(n.dev_key.hex() for n in cdb.nodes),
    ]
    assert not any(key in json.dumps(response).lower() for key in keys)


async def test_a_device_that_is_no_node_of_ours_is_refused(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    servers: FakeConfigServers,
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await audit(hass, device="no-such-device")
    assert err.value.translation_key == "service_unknown_device"
    # a registry leftover: our identifier scheme, but nothing of the export behind it any more
    stale = dr.async_get(hass).async_get_or_create(
        config_entry_id=init_integration.entry_id,
        identifiers={(DOMAIN, "00005eff-fe00-5399-0000-000000000000-0001")},
    )
    with pytest.raises(ServiceValidationError) as err:
        await audit(hass, device=stale.id)
    assert err.value.translation_key == "service_unknown_device"
    assert servers.seen == []
    with pytest.raises(vol.Invalid):
        await audit(hass, device=stale.id, config_entry_id=init_integration.entry_id)


async def test_a_lost_link_fails_the_action(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    hub = init_integration.runtime_data
    with (
        patch.object(hub, "async_audit", side_effect=ConnectionError("gone")),
        pytest.raises(HomeAssistantError) as err,
    ):
        await audit(hass, config_entry_id=init_integration.entry_id)
    assert err.value.translation_key == "send_failed"


# ----------------------------------------------------------------------------- locate_node (review-4 F4-15)


async def locate(hass: HomeAssistant, **data: Any) -> Any:
    return await hass.services.async_call(
        DOMAIN, "locate_node", data, blocking=True, return_response=True
    )


def identity_sets(fake_link: FakeProxyLink, node: int) -> list[bool]:
    """The Node Identity Sets sent to `node`, as on (True) / off (False)."""
    return [
        bool(pdu[-1])
        for _src, dst, pdu in fake_link.config_sent
        if dst == node and decode_opcode(pdu)[0] == C.CONFIG_NODE_IDENTITY_SET
    ]


async def later(hass: HomeAssistant, seconds: float) -> None:
    """Fire what is due `seconds` from now (the clock itself stands still), and the Set off it sends."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()
    offs = [
        t
        for t in hass.config_entries.async_entries(DOMAIN)[0]._background_tasks
        if t.get_name().endswith(" off")
    ]
    if offs:
        await asyncio.gather(*offs)
    await hass.async_block_till_done()


async def test_locate_switches_the_node_identity_on_and_off_again(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    servers: FakeConfigServers,
) -> None:
    """A Node Identity Set on to the node behind the device, the Set off after the time asked for; a second call
    restarts the count. Nothing else is sent."""
    fake_link.config_sent.clear()
    switch = device_id(hass, init_integration, UID_LIGHT_SWITCH)

    # without asking for the response the action answers nothing; the default is the spec's 60 s
    await hass.services.async_call(
        DOMAIN, "locate_node", {"device": switch}, blocking=True
    )
    assert identity_sets(fake_link, SWITCH) == [True]
    assert servers.identity[SWITCH] == C.NODE_IDENTITY_RUNNING
    # again, for 10 s: the off comes 10 s after this call, and only once
    assert await locate(hass, device=switch, duration=10) == {
        "node": "0148",
        "seconds": 10,
    }
    await later(hass, 9)
    assert identity_sets(fake_link, SWITCH) == [True, True]
    await later(hass, 11)
    assert identity_sets(fake_link, SWITCH) == [True, True, False]
    assert servers.identity[SWITCH] == C.NODE_IDENTITY_STOPPED
    await later(hass, 61)
    assert identity_sets(fake_link, SWITCH) == [True, True, False]
    assert asked(fake_link) == {SWITCH}


async def test_a_node_that_cannot_or_will_not_advertise_its_identity_is_reported(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    servers: FakeConfigServers,
) -> None:
    """Not supported, refused or silent: an error each, and no Set off follows."""
    switch = device_id(hass, init_integration, UID_LIGHT_SWITCH)
    servers.identity[SWITCH] = C.NODE_IDENTITY_NOT_SUPPORTED
    with pytest.raises(HomeAssistantError) as err:
        await locate(hass, device=switch)
    assert err.value.translation_key == "locate_not_supported"
    servers.identity_status[SWITCH] = 0x04
    with pytest.raises(HomeAssistantError) as err:
        await locate(hass, device=switch)
    assert err.value.translation_key == "locate_refused"
    assert err.value.translation_placeholders == {
        "node": "0148",
        "status": "Invalid NetKey Index",
    }
    servers.silent.add(SWITCH)
    with pytest.raises(HomeAssistantError) as err:
        await locate(hass, device=switch)
    assert err.value.translation_key == "locate_no_answer"
    await later(hass, 61)
    assert False not in identity_sets(fake_link, SWITCH)
    mesh = device_id(hass, init_integration, f"mesh:{MESH_UUID}")
    with pytest.raises(ServiceValidationError) as err:
        await locate(hass, device=mesh)
    assert err.value.translation_key == "remove_device_mesh"
    hub = init_integration.runtime_data
    with (
        patch.object(hub, "async_locate", side_effect=ConnectionError("gone")),
        pytest.raises(HomeAssistantError) as err,
    ):
        await locate(hass, device=switch)
    assert err.value.translation_key == "send_failed"
    with pytest.raises(vol.Invalid):
        await locate(hass, device=switch, duration=61)


async def test_a_malformed_status_is_not_taken_for_the_answer(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    servers: FakeConfigServers,
) -> None:
    """A truncated Node Identity Status does not decode: the Set is asked again and the next answer counts."""
    garbled: list[int] = []

    def answer(node: int, access: bytes) -> bytes | None:
        if not garbled:
            garbled.append(node)
            return encode_opcode(C.CONFIG_NODE_IDENTITY_STATUS) + b"\x00"
        return servers(node, access)

    fake_link.config_reply = answer
    switch = device_id(hass, init_integration, UID_LIGHT_SWITCH)
    assert await locate(hass, device=switch) == {"node": "0148", "seconds": 60}
    assert garbled == [SWITCH]
    assert identity_sets(fake_link, SWITCH) == [True, True]  # asked again


async def test_an_off_that_goes_unanswered_is_only_logged(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    servers: FakeConfigServers,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    await locate(hass, device=device_id(hass, init_integration, UID_LIGHT_CTL))
    servers.silent.add(CTL)
    await later(hass, 61)
    assert "0232: Node Identity not switched off" in caplog.text


async def test_unloading_the_entry_drops_the_pending_off(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    servers: FakeConfigServers,
) -> None:
    """The node stops by itself within a minute: nothing is sent after the entry stopped."""
    await locate(hass, device=device_id(hass, init_integration, UID_LIGHT_SWITCH))
    await hass.config_entries.async_unload(init_integration.entry_id)
    await later(hass, 61)
    assert identity_sets(fake_link, SWITCH) == [True]
