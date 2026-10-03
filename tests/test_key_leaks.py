"""No key where it does not belong, after a key refresh (review-4 Q T8, Q4-13).

The hub is set up with every logger at DEBUG (the mesh log decodes every frame), follows the app's key refresh to
its end and is reloaded onto the new key; then everything it left behind is scanned for every key the scenario
involved — the export's NetKey (now revoked), AppKey and device keys, the refresh's new NetKey, and the keys derived
from either NetKey — in every encoding `key_scan` knows: every file under the configuration directory, every store,
every log record (message, arguments and traceback) and the diagnostics of the entry and of each of its devices.

Two places hold a key on purpose, and only the key they need: the mesh's sequence-number store and its `.backup`
copy keep the followed refresh's new NetKey (`KeyRefreshRecord.to_stored`) until the export holds it
(`async_apply_followed_key_refresh`). Who may read those files is brief 02's (`tests/test_file_modes.py`).
"""

from __future__ import annotations

import json
import logging
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
    get_diagnostics_for_device,
)

from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.crypto import NetKeyMaterial

from . import key_scan
from .conftest import CDB_PATH, FakeProxyLink, make_service_info, settle, wait_for_link
from .helpers import LIGHT_SWITCH, SEQ_STORE_KEY

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

NEW_KEY = bytes(
    range(0x60, 0x70)
)  # the refresh's new NetKey: synthetic, like every key of the fixtures
NEW = "new NetKey"
# place -> the keys it holds on purpose
HOLDERS = {SEQ_STORE_KEY: {NEW}, f"{SEQ_STORE_KEY}.backup": {NEW}}
# the test harness's in-memory `Store` logs every write with its data ("Writing data to …"): that is the store,
# scanned as one below, not a log line of the integration
HARNESS_STORAGE_LOGGER = "pytest_homeassistant_custom_component.common"


@pytest.fixture
def debug_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Every logger at DEBUG from before the setup on (list it before `init_integration`)."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="jhmesh")
    return caplog


def record_text(record: logging.LogRecord) -> str:
    """All a handler could write of `record`: the message, its arguments one by one, the traceback."""
    parts = [record.getMessage(), repr(record.args)]
    if record.exc_info:
        parts.append("".join(traceback.format_exception(*record.exc_info)))
    if record.exc_text:
        parts.append(record.exc_text)
    return "\n".join(parts)


def written_files(config_dir: Path) -> dict[str, str]:
    """Every file under the configuration directory, by `file <relative path>`; every byte read as a character."""
    return {
        f"file {path.relative_to(config_dir)}": path.read_bytes().decode("latin-1")
        for path in sorted(config_dir.rglob("*"))
        if path.is_file()
    }


def misplaced(places: dict[str, str], keys: dict[str, bytes]) -> dict[str, list[str]]:
    """`{place: [leak, …]}` for every place whose text holds a key of `keys` it is not meant to hold (`HOLDERS`)."""
    out: dict[str, list[str]] = {}
    for place, text in places.items():
        allowed = HOLDERS.get(place, set())
        others = {name: key for name, key in keys.items() if name not in allowed}
        if found := key_scan.leaks(text, others):
            out[place] = found
    return out


async def follow_a_key_refresh(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    link: FakeProxyLink,
    bluetooth: dict[str, Any],
) -> None:
    """The app's refresh to `NEW_KEY`, proven by the proxy's beacons under it, then a reload onto the new key."""
    hub = entry.runtime_data
    new = NetKeyMaterial.derive(NEW_KEY)
    link.inject_from_provisioner(LIGHT_SWITCH, C.netkey_update(NEW_KEY))
    await settle(hass)
    link.inject_from_provisioner(LIGHT_SWITCH, C.key_refresh_phase_set(2))
    await settle(hass)
    link.nk = new  # the proxy takes the new key and beacons with it: the proof
    link.inject_beacon(key_refresh=True)
    await settle(hass)
    await hub.set_onoff(LIGHT_SWITCH, True)  # something sent under the new key
    link.inject_beacon()  # phase 3: the old key is revoked
    await settle(hass)
    assert hub.proxy.key_refresh_phase == 0
    assert entry.unique_id == new.network_id.hex()
    bluetooth["infos"] = [make_service_info(new.network_id)]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await wait_for_link(hass, entry)
    assert entry.runtime_data.proxy.nk.key == NEW_KEY


async def test_no_key_leaks_after_a_key_refresh(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_storage: dict[str, Any],
    debug_logs: pytest.LogCaptureFixture,
    answering_mesh: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    await follow_a_key_refresh(hass, init_integration, fake_link, mock_bluetooth_env)
    keys = key_scan.secrets(CDB.load(Path(CDB_PATH))) | key_scan.netkey_secrets(
        NEW, NetKeyMaterial.derive(NEW_KEY)
    )

    places = await hass.async_add_executor_job(
        written_files, Path(hass.config.config_dir)
    )
    for key, stored in hass_storage.items():
        places[key] = json.dumps(stored, default=repr)
    for i, record in enumerate(debug_logs.records):
        if record.name != HARNESS_STORAGE_LOGGER:
            places[f"log record {i} ({record.name})"] = record_text(record)
    places["config entry diagnostics"] = json.dumps(
        await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    )
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(
        registry, init_integration.entry_id
    ):
        result = await get_diagnostics_for_device(
            hass, hass_client, init_integration, device
        )
        places[f"diagnostics of {min(device.identifiers)[1]}"] = json.dumps(result)

    # the scan saw what it should: the mesh's frame log, every device, both copies of the store
    assert any(name.startswith("log record") for name in places)
    assert len([name for name in places if name.startswith("diagnostics of")]) > 10
    assert set(HOLDERS) <= set(places)
    assert misplaced(places, keys) == {}
    # ... and the holders do hold the new key, where the scan finds it
    for place in HOLDERS:
        assert key_scan.leaks(places[place], {NEW: NEW_KEY}) == [f"{NEW} as hex"]
