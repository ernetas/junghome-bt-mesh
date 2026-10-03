"""The app's key refresh, followed by the hub (review-3 N2b) only on proof that the mesh moved (review-4 D4)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_IGNORE
from homeassistant.data_entry_flow import FlowResultType

from custom_components.junghome_ble import vault_refresh
from custom_components.junghome_ble.const import (
    CONF_UNICAST,
    DOMAIN,
    ISSUE_IV_INDEX_MISMATCH,
    ISSUE_KEY_REFRESH,
    ISSUE_VAULT_KEY_REFRESH,
)
from custom_components.junghome_ble.coordinator import (
    async_apply_followed_key_refresh,
    seq_store_for_uuid,
)
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.crypto import NetKeyMaterial
from custom_components.junghome_ble.jhmesh.onboarding import node_for
from custom_components.junghome_ble.jhmesh.pdu import decode_opcode, encode_opcode
from custom_components.junghome_ble.jhmesh.vault import RefreshProgress, VaultNode

from .conftest import (
    CDB_PATH,
    FakeProxyLink,
    make_service_info,
    settle,
    wait_for_link,
    wait_until,
)
from .helpers import LIGHT_CTL, LIGHT_SWITCH, MESH_UUID, SEQ_STORE_KEY, find_issue

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.junghome_ble.coordinator import JungHomeHub

NEW_KEY = bytes(range(0x40, 0x50))
NEW_ID = NetKeyMaterial.derive(NEW_KEY).network_id
PHONE = 0x0001
OTHER_ADDRESS = "11:22:33:44:55:66"  # a proxy no node of the export advertises from
# a device Home Assistant added (`add_device`): in its vault, not in the export, and so not in the app's database
VAULT_NODE = 0x7FF0
VAULT_UUID = "00005EFF-FE00-5399-0000-000000000000"
VAULT_KEY = bytes(range(0x60, 0x70))


def phase_status(phase: int) -> bytes:
    """Config Key Refresh Phase Status, success, NetKey index 0."""
    return encode_opcode(C.CONFIG_KEY_REFRESH_PHASE_STATUS) + bytes([0, 0, 0, phase])


def netkey_status() -> bytes:
    """Config NetKey Status, success, NetKey index 0."""
    return encode_opcode(C.CONFIG_NETKEY_STATUS) + bytes(3)


class VaultDevice:
    """The Configuration Server of the device Home Assistant added: NetKey Update and Phase Set, as the spec has them.

    `silent`: it answers nothing (off, out of range, or it never got the new key).
    """

    def __init__(self) -> None:
        self.phase = 0
        self.silent = False

    def __call__(self, node: int, access: bytes) -> bytes | None:
        if node != VAULT_NODE or self.silent:
            return None
        op, _cid, p = decode_opcode(access)
        if op == C.CONFIG_NETKEY_UPDATE:
            self.phase = 1
            return netkey_status()
        if op == C.CONFIG_KEY_REFRESH_PHASE_SET:
            self.phase = 2 if p[2] == 2 else 0
            return phase_status(self.phase)
        return None


def put_in_vault(
    hub: JungHomeHub, fake_link: FakeProxyLink, progress: RefreshProgress | None = None
) -> VaultNode:
    """A device `add_device` provisioned (pending: in no export) — kept in the vault, holding its key on air."""
    template = hub.cdb.node_by_addr(LIGHT_CTL)
    assert template is not None
    count = len(template.elements)
    node = hub.vault.identity().remember_provisioned(
        VAULT_UUID, VAULT_NODE, count, VAULT_KEY, (), progress
    )
    fake_link.cdb.nodes.append(
        node_for(
            template, uuid=VAULT_UUID, unicast=VAULT_NODE, dev_key=VAULT_KEY, name="New"
        )
    )
    return node


def sent_to_vault_node(fake_link: FakeProxyLink) -> list[bytes]:
    return [
        access for _src, node, access in fake_link.config_sent if node == VAULT_NODE
    ]


async def app_refresh(
    hass: HomeAssistant, fake_link: FakeProxyLink, *, up_to: int = 3
) -> list[list[bytes]]:
    """The app's key refresh as the proxy node (0148) shows it, proven at each step; what the vault device got after each.

    Phase 1: its NetKey Update to the proxy node and the proxy's NetKey Status (the proxy's word is proof); Phase 2:
    the proxy beacons with the new key, the Key Refresh flag set; Phase 3: the flag clear.
    """
    seen = []
    fake_link.inject_from_provisioner(LIGHT_SWITCH, C.netkey_update(NEW_KEY))
    await settle(hass)
    seen.append(sent_to_vault_node(fake_link))
    fake_link.inject_config(LIGHT_SWITCH, PHONE, netkey_status())
    await settle(hass)
    seen.append(sent_to_vault_node(fake_link))
    if up_to >= 2:
        fake_link.nk = NetKeyMaterial.derive(NEW_KEY)
        fake_link.inject_beacon(key_refresh=True)
        await settle(hass)
        seen.append(sent_to_vault_node(fake_link))
    if up_to >= 3:
        fake_link.inject_beacon()
        await settle(hass)
        seen.append(sent_to_vault_node(fake_link))
    return seen


async def test_the_apps_key_refresh_is_followed_and_survives_a_reload(
    hass: HomeAssistant,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    """NetKey Update, Phase Set 2, then the new key's beacons: HA transmits with the new key from phase 2 and, once
    the refresh is complete, the entry's unique id is the new Network ID. A reload keeps the new key although the
    export still has the old one, and finds the proxies advertising the new Network ID.

    Review 4: Phase Set 2 alone (a request) no longer moves HA; the proxy's beacon under the new key does."""
    hub = init_integration.runtime_data
    old = fake_link.nk
    new = NetKeyMaterial.derive(NEW_KEY)
    fake_link.inject_beacon(
        key_refresh=True, new_key=True
    )  # before HA knows the new key: the repair
    await settle(hass)
    assert find_issue(hass, ISSUE_KEY_REFRESH) is not None
    fake_link.inject_from_provisioner(LIGHT_SWITCH, C.netkey_update(NEW_KEY))
    await settle(hass)
    assert hub.proxy.key_refresh_phase == 1
    assert find_issue(hass, ISSUE_KEY_REFRESH) is None  # followed: nothing to repair
    fake_link.inject_from_provisioner(LIGHT_SWITCH, C.key_refresh_phase_set(2))
    await settle(hass)
    assert hub.proxy.key_refresh_phase == 1  # a request is no proof
    fake_link.nk = new  # the proxy takes the new key too, and beacons with it
    fake_link.inject_beacon(key_refresh=True)
    await settle(hass)
    assert hub.proxy.key_refresh_phase == 2
    fake_link.undecryptable.clear()
    fake_link.sent.clear()
    await hub.set_onoff(LIGHT_SWITCH, True)
    assert fake_link.sent  # opened by the mesh under the new key
    assert not fake_link.undecryptable
    fake_link.inject_beacon()  # the new key, no flag: phase 3
    await settle(hass)
    assert hub.proxy.key_refresh_phase == 0
    assert init_integration.unique_id == new.network_id.hex()
    # a reload: the export still has the old key, the proxies advertise the new Network ID
    assert fake_link.nk is not old
    mock_bluetooth_env["infos"] = [make_service_info(new.network_id)]
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await wait_for_link(hass, init_integration)
    hub = init_integration.runtime_data
    assert hub.proxy.nk.key == NEW_KEY
    assert hub.cdb.net_keys[0].key == NEW_KEY


async def test_mid_refresh_a_proxy_of_the_new_key_is_not_offered_as_a_new_mesh(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    """Review-4 H4-4: from the first move of a refresh the hub follows on, the new key's Network ID is this mesh
    for discovery, before the entry's unique id holds it (from an address the export lacks: a new node)."""
    await app_refresh(hass, fake_link, up_to=1)
    assert init_integration.unique_id != NEW_ID.hex()
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=make_service_info(NEW_ID, address=OTHER_ADDRESS),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize("ignore", [False, True], ids=["pending", "ignored"])
async def test_the_completed_refresh_takes_the_new_network_id_from_a_discovery(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    ignore: bool,
) -> None:
    """H4-4: the new key's proxies were offered as a new mesh before the hub knew the key, and the user left the
    discovery pending or pressed *Ignore*. At completion the flow is aborted and the ignored entry removed before
    the unique id moves: no "already in use" error, no core repair."""
    caplog.set_level(logging.INFO)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=make_service_info(NEW_ID, address=OTHER_ADDRESS),
    )
    assert result["type"] is FlowResultType.FORM
    if ignore:
        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IGNORE},
            data={"unique_id": NEW_ID.hex(), "title": "ignored"},
        )
        await hass.async_block_till_done()
    ignored = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, NEW_ID.hex())
    assert (ignored is not None) is ignore
    await app_refresh(hass, fake_link)
    await hass.async_block_till_done()
    assert init_integration.unique_id == NEW_ID.hex()
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    assert hass.config_entries.async_entries(DOMAIN) == [init_integration]
    assert "already in use" not in caplog.text
    assert ("Removing the ignored discovery" in caplog.text) is ignore


async def test_the_new_keys_proxies_are_ours_once_the_refresh_is_followed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    """Review-4 R4-8: the advert verdicts the client keeps do not outlive a key change — a proxy advertising the new
    Network ID was another network's until the refresh was followed, and is one of ours from then on."""
    hub = init_integration.runtime_data
    advert = make_service_info(NEW_ID, address="30:FB:10:00:02:01")
    mock_bluetooth_env["infos"] = [advert]
    assert hub.visible_proxies() == []
    fake_link.inject_from_provisioner(LIGHT_SWITCH, C.netkey_update(NEW_KEY))
    await settle(hass)
    assert hub.proxy.key_refresh_phase == 1
    assert hub.visible_proxies() == [advert]


async def test_a_completed_key_refresh_survives_a_new_unicast_address(
    hass: HomeAssistant,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    hass_storage: dict[str, Any],
) -> None:
    """Review-4 S4-9: the followed key lived in the address's record, so a unicast address changed after the
    refresh completed started with the export's revoked key — deaf and mute until the export was fetched again. It
    is the mesh's now (`{"mesh": {"key_refresh": …}}`): the new address keeps the new key."""
    await app_refresh(hass, fake_link)
    hub = init_integration.runtime_data
    assert hub.proxy.key_refresh_phase == 0  # complete, proven
    await hass.async_block_till_done()
    new = NetKeyMaterial.derive(NEW_KEY)
    mock_bluetooth_env["infos"] = [make_service_info(new.network_id)]
    hass.config_entries.async_update_entry(
        init_integration, data={**init_integration.data, CONF_UNICAST: "0D01"}
    )
    await hass.async_block_till_done()  # the update listener reloads the entry
    await wait_for_link(hass, init_integration)
    hub = init_integration.runtime_data
    assert hub.state.src == 0x0D01
    assert hub.cdb.net_keys[0].key == NEW_KEY
    assert hub.proxy.nk.key == NEW_KEY
    assert hub.state.key_refresh is not None
    assert hub.state.key_refresh.key == NEW_KEY
    stored = hass_storage[SEQ_STORE_KEY]["data"]
    assert stored["mesh"]["key_refresh"]["phase"] == 3
    # the record's copy, for an older version
    assert stored["addresses"]["0D01"]["key_refresh"] == stored["mesh"]["key_refresh"]


async def test_a_forged_key_refresh_moves_nothing_across_a_reload(
    hass: HomeAssistant,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Review 4 (D4, P4-1): one node — not the proxy — seals NetKey Update (a key of its choice), Phase Set 2 and
    Phase Set 3 with its own device key, to itself and posing as the app, and confirms every phase itself. HA
    accepted the key, transmitted with it, dropped the real one and moved the entry's unique id; after a restart it
    was deaf and mute. Now nothing moves: the unique id, the transmit key and, after a reload, the export's key
    are what they were."""
    hub = init_integration.runtime_data
    old = fake_link.nk
    unique_id = init_integration.unique_id
    for src in (LIGHT_CTL, PHONE):
        for pdu in (
            C.netkey_update(NEW_KEY),
            C.key_refresh_phase_set(2),
            C.key_refresh_phase_set(3),
        ):
            fake_link.inject_from_provisioner(LIGHT_CTL, pdu, src=src)
            await settle(hass)
    for phase in (1, 2, 0):
        fake_link.inject_config(LIGHT_CTL, PHONE, phase_status(phase))
    await settle(hass)
    assert hub.proxy.key_refresh_phase == 1  # the key is accepted, never used
    assert hub.proxy.nk.key == old.key
    assert init_integration.unique_id == unique_id
    fake_link.sent.clear()
    await hub.set_onoff(LIGHT_SWITCH, True)
    assert fake_link.sent  # the mesh still opens what we send
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await wait_for_link(hass, init_integration)
    hub = init_integration.runtime_data
    assert hub.cdb.net_keys[0].key == old.key
    assert hub.proxy.nk.key == old.key
    assert init_integration.unique_id == unique_id


async def test_an_iv_index_out_of_reach_raises_an_issue(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Review-3 T5: a mesh 43 or more IV updates ahead (or two behind) ignores Home Assistant, and the beacon that
    says so was dropped silently."""
    hub = init_integration.runtime_data
    assert hub.proxy.state.iv_known
    fake_link.inject_beacon(iv_index=hub.proxy.state.iv_index + 43)
    await settle(hass)
    issue = find_issue(hass, ISSUE_IV_INDEX_MISMATCH)
    assert issue is not None
    assert issue.translation_placeholders["mesh"] == "43"
    fake_link.inject_beacon(iv_index=0)  # within reach again
    await settle(hass)
    assert find_issue(hass, ISSUE_IV_INDEX_MISMATCH) is None
    hub.proxy.state.iv_index = 5
    fake_link.inject_beacon(iv_index=3)  # two behind: our store is ahead of the mesh
    await settle(hass)
    assert find_issue(hass, ISSUE_IV_INDEX_MISMATCH) is not None


async def test_a_followed_key_refresh_meets_an_export_written_mid_refresh(
    hass: HomeAssistant,
) -> None:
    """An export written mid key refresh (`CDB.net_key_refresh`): the followed refresh that completed on its new
    key ends the export's too, and one that completed on its old key is older than the export."""
    raw = json.loads(await hass.async_add_executor_job(Path(CDB_PATH).read_text))[
        "meshNetwork"
    ]
    old = raw["netKeys"][0]["key"]
    raw["netKeys"][0].update(key=NEW_KEY.hex(), oldKey=old, phase=1)
    for followed, refreshing in ((NEW_KEY.hex(), False), (old, True)):
        await seq_store_for_uuid(hass, MESH_UUID).async_save(
            {
                "addresses": {
                    "0D00": {
                        "seq": 1,
                        "key_refresh": {"key": followed, "phase": 3, "proof": "beacon"},
                    }
                }
            }
        )
        cdb = CDB.from_network(json.loads(json.dumps(raw)))
        await async_apply_followed_key_refresh(hass, cdb, 0x0D00)
        assert cdb.net_keys[0].key == NEW_KEY
        assert bool(cdb.net_key_refresh) is refreshing


@pytest.mark.parametrize(
    "refresh",
    [
        None,
        {"key": NEW_KEY.hex(), "phase": 2, "proof": "beacon"},
        {"key": NEW_KEY.hex(), "phase": 3},
    ],
    ids=["none", "in_progress", "unproven"],
)
async def test_only_a_proven_completion_replaces_the_exports_key(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    refresh: dict[str, Any] | None,
) -> None:
    """A completion recorded without proof — before review 4, or forged by a node — leaves the export's key in
    place (the client takes the key up as a candidate, and the proxy's next beacon proves a real one)."""
    record: dict[str, Any] = {"seq": 1}
    if refresh is not None:
        record["key_refresh"] = refresh
    await seq_store_for_uuid(hass, MESH_UUID).async_save(
        {"addresses": {"0D00": record}}
    )
    cdb = CDB.load(Path(CDB_PATH))
    exported = cdb.net_keys[0].key
    with caplog.at_level(logging.WARNING):
        await async_apply_followed_key_refresh(hass, cdb, 0x0D00)
    assert cdb.net_keys[0].key == exported
    assert ("complete but unproven" in caplog.text) is (
        refresh is not None and "proof" not in refresh
    )


# ============================================================================= devices Home Assistant added (D11)


async def test_a_device_home_assistant_added_is_carried_through_the_apps_key_refresh(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Review 4 (D11): the app hands the new key only to the devices of its database; one Home Assistant added gets
    the NetKey Update, Phase Set 2 and Phase Set 3 from Home Assistant, each once the step is proven, in order. It
    keeps working under the new key alone, and the vault says how far it came (no key)."""
    hub = init_integration.runtime_data
    vault_node = put_in_vault(hub, fake_link)
    device = VaultDevice()
    fake_link.config_reply = device
    seen = await app_refresh(hass, fake_link)
    update, set2, set3 = (
        C.netkey_update(NEW_KEY),
        C.key_refresh_phase_set(2),
        C.key_refresh_phase_set(3),
    )
    assert seen == [
        [],  # learnt from one Update: nothing handed out yet
        [update],  # proven by the proxy's own NetKey Status
        [update, set2],
        [update, set2, set3],
    ]
    assert device.phase == 0  # normal operation, on the new key
    assert vault_node.key_refresh == RefreshProgress(NEW_ID, 3)
    stored = await hub.vault._store.async_load()
    assert stored is not None
    assert stored["nodes"][0]["keyRefresh"] == {
        "networkId": NEW_ID.hex().upper(),
        "phase": 3,
    }
    assert NEW_KEY.hex() not in json.dumps(stored).lower()
    assert (
        hub.proxy.cdb.node_by_addr(VAULT_NODE) is None
    )  # made known for the exchanges only
    assert find_issue(hass, ISSUE_VAULT_KEY_REFRESH) is None
    assert hub.vault_refresh.diagnostics() == {
        "phase": 0,
        "proven_phase": 3,
        "vault_nodes": {"7FF0": {"network_id": NEW_ID.hex(), "phase": 3}},
        "lagging": [],
    }


async def test_a_silent_device_raises_the_repair_and_a_later_link_clears_it(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    monkeypatch: pytest.MonkeyPatch,
    fast_sleep: list[float],
    mock_bluetooth_env: dict[str, Any],
) -> None:
    """A device that does not confirm the end of the refresh once Home Assistant reached it is named by a repair
    issue; the next link takes it up again (from where it stopped) and clears the issue once it confirmed."""
    monkeypatch.setattr(vault_refresh, "REQUEST_TIMEOUT", 0.01)
    hub = init_integration.runtime_data
    vault_node = put_in_vault(
        hub, fake_link, RefreshProgress(NEW_ID, 2)
    )  # got as far as Phase 2
    device = VaultDevice()
    device.phase, device.silent = 2, True
    fake_link.config_reply = device
    seen = await app_refresh(hass, fake_link)
    assert seen[:3] == [[], [], []]  # there already
    await wait_until(
        hass,
        lambda: find_issue(hass, ISSUE_VAULT_KEY_REFRESH) is not None,
        what="the lagging device's repair issue",
    )
    issue = find_issue(hass, ISSUE_VAULT_KEY_REFRESH)
    assert issue is not None
    assert issue.translation_placeholders == {
        "title": init_integration.title,
        "addresses": "7FF0",
    }
    assert vault_node.key_refresh == RefreshProgress(NEW_ID, 2)
    assert hub.vault_refresh.diagnostics()["lagging"] == ["7FF0"]
    device.silent = False
    # the proxies advertise the new key's Network ID now
    mock_bluetooth_env["infos"] = [make_service_info(NEW_ID)]
    fake_link.drop_link()
    await wait_for_link(hass, init_integration, connected=False)
    await wait_for_link(hass, init_integration)
    await wait_until(
        hass,
        lambda: find_issue(hass, ISSUE_VAULT_KEY_REFRESH) is None,
        what="the repair issue cleared",
    )
    assert device.phase == 0
    assert vault_node.key_refresh == RefreshProgress(NEW_ID, 3)
    assert sent_to_vault_node(fake_link).count(C.key_refresh_phase_set(3)) == 4
    # forgotten (reset with `force`): no longer named
    hub.vault_refresh.lagging = {VAULT_NODE}
    hub.vault.identity().forget(VAULT_UUID)
    hub.vault_refresh.update_issue()
    assert find_issue(hass, ISSUE_VAULT_KEY_REFRESH) is None


async def test_a_forged_key_refresh_reaches_no_device_home_assistant_added(
    hass: HomeAssistant,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """One node seals NetKey Update (a key of its choice), Phase Set 2 and 3 with its own device key and confirms
    every phase itself: the key is never handed to the devices Home Assistant added, and no Phase Set goes out."""
    hub = init_integration.runtime_data
    put_in_vault(hub, fake_link)
    fake_link.config_reply = VaultDevice()
    for pdu in (
        C.netkey_update(NEW_KEY),
        C.key_refresh_phase_set(2),
        C.key_refresh_phase_set(3),
    ):
        fake_link.inject_from_provisioner(LIGHT_CTL, pdu, src=PHONE)
    fake_link.inject_config(LIGHT_CTL, PHONE, netkey_status())
    for phase in (1, 2, 0):
        fake_link.inject_config(LIGHT_CTL, PHONE, phase_status(phase))
    await settle(hass)
    assert hub.proxy.key_refresh_phase == 1
    assert sent_to_vault_node(fake_link) == []
    # a new link's pass: still nothing
    hub.vault_refresh.schedule()
    await settle(hass)
    assert sent_to_vault_node(fake_link) == []


async def test_the_pass_stops_with_the_link_and_keeps_going_without_a_vault_store(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = init_integration.runtime_data
    refresh = hub.vault_refresh
    refresh.schedule()  # no vault: nothing to do, no task
    assert refresh.task is None
    put_in_vault(hub, fake_link)
    device = VaultDevice()
    fake_link.config_reply = device
    # a vault that cannot be written: logged (no key), the device still taken along
    save = AsyncMock(side_effect=OSError("disk full"))
    monkeypatch.setattr(hub.vault, "async_save", save)
    with caplog.at_level(logging.WARNING):
        await app_refresh(hass, fake_link, up_to=1)
    assert device.phase == 1
    assert save.await_count >= 2
    assert "could not be kept in the vault (OSError)" in caplog.text
    assert VAULT_KEY.hex() not in caplog.text.lower()
    # a pass asked for while one runs runs once more after it; one whose link goes stops
    calls: list[int] = []

    async def lost(*_args: Any, **_kwargs: Any) -> bool:
        calls.append(1)
        if len(calls) == 1:
            refresh.schedule()  # while running
            return True
        raise ConnectionError("link lost")

    monkeypatch.setattr(vault_refresh, "carry", lost)
    monkeypatch.setattr(vault_refresh, "wanted", lambda *_args: object())
    refresh.schedule()
    await settle(hass)
    assert len(calls) == 2
    # no link: nothing asked
    monkeypatch.setattr(type(hub.proxy), "connected", property(lambda _self: False))
    await refresh._pass()
    assert len(calls) == 2
