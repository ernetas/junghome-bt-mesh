"""The app's key refresh, followed by the hub (review-3 N2b) only on proof that the mesh moved (review-4 D4)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from custom_components.junghome_ble.const import (
    ISSUE_IV_INDEX_MISMATCH,
    ISSUE_KEY_REFRESH,
)
from custom_components.junghome_ble.coordinator import (
    async_apply_followed_key_refresh,
    seq_store_for_uuid,
)
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.crypto import NetKeyMaterial
from custom_components.junghome_ble.jhmesh.pdu import encode_opcode

from .conftest import (
    CDB_PATH,
    FakeProxyLink,
    make_service_info,
    settle,
    wait_for_link,
)
from .helpers import LIGHT_CTL, LIGHT_SWITCH, MESH_UUID, find_issue

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

NEW_KEY = bytes(range(0x40, 0x50))
PHONE = 0x0001


def phase_status(phase: int) -> bytes:
    """Config Key Refresh Phase Status, success, NetKey index 0."""
    return encode_opcode(C.CONFIG_KEY_REFRESH_PHASE_STATUS) + bytes([0, 0, 0, phase])


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
