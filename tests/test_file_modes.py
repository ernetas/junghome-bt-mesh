"""Every file Home Assistant writes that can hold mesh key material is owner-only (review-4 D1, Q4-1 = P4-5).

The sequence-number stores were written 0644 by Home Assistant's storage helper (no `private=True`) while a record
held the new NetKey of a followed key refresh. Here every key-holding file is written for real, under umask 022,
through the integration's own code paths, and its mode read back.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable, Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.helpers import storage
from homeassistant.helpers.storage import STORAGE_DIR
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome_ble.config_flow import (
    _replace_keeping,
    pre_reconfigure_path,
)
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_METADATA_DIR,
    CONF_UNICAST,
    DOMAIN,
)
from custom_components.junghome_ble.identity import async_vault_keeper
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh.export import (
    write_private,
    write_private_with_backup,
)
from custom_components.junghome_ble.mesh_config import (
    MeshConfigurator,
    app_copy_path,
    pre_adopt_path,
)
from custom_components.junghome_ble.seq_store import (
    async_skip_seq_store_ahead,
    seq_floor_store_for_uuid,
    seq_store_for_uuid,
)

from .conftest import (
    CDB_PATH,
    META_DIR,
    FakeProxyLink,
    settle,
    setup_entry,
    wait_for_link,
)
from .helpers import LIGHT_SWITCH, MESH_UUID

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

NEW_KEY = bytes(range(0x40, 0x50))


def _storage(hass: HomeAssistant, key: str) -> Path:
    return Path(hass.config.path(STORAGE_DIR, key))


def _export(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(DOMAIN, f"{MESH_UUID}.json"))


# Every file that can hold key material, by what it is. A store or file added later that can hold a key belongs
# in this table too: the test below then proves it owner-only.
KEY_FILES: dict[str, Callable[[HomeAssistant], Path]] = {
    "seq store": lambda hass: _storage(hass, f"{DOMAIN}.seq.{MESH_UUID}"),
    "seq store backup": lambda hass: _storage(hass, f"{DOMAIN}.seq.{MESH_UUID}.backup"),
    "seq floor": lambda hass: _storage(hass, f"{DOMAIN}.seq.{MESH_UUID}.floor"),
    "vault": lambda hass: _storage(hass, f"{DOMAIN}.vault.{MESH_UUID}"),
    "vault backup": lambda hass: _storage(hass, f"{DOMAIN}.vault.{MESH_UUID}.backup"),
    "export": _export,
    "export .bak": lambda hass: _export(hass).with_name(f"{MESH_UUID}.json.bak"),
    "export .bak.1": lambda hass: _export(hass).with_name(f"{MESH_UUID}.json.bak.1"),
    "export .app": lambda hass: app_copy_path(_export(hass)),
    "export .pre-adopt": lambda hass: pre_adopt_path(_export(hass)),
    "export .pre-reconfigure": lambda hass: pre_reconfigure_path(_export(hass)),
}


@pytest.fixture
def loose_umask() -> Generator[None]:
    """Umask 022: what a default system gives Home Assistant, under which a plain write comes out 0644."""
    old = os.umask(0o022)
    yield
    os.umask(old)


@pytest.fixture
def stores_on_disk(hass_storage: dict[str, Any]) -> Generator[None]:
    """Write every `Store` to disk as well (the plugin keeps them in memory only), through HA's own writer.

    Set up after `hass_storage`, so it is undone before the plugin's own patch is.
    """
    in_memory = storage.Store._async_write_data

    async def both(store: storage.Store[Any], data: dict[str, Any]) -> None:
        await in_memory(store, data)  # what later loads read
        await store.hass.async_add_executor_job(store._write_data, data)

    with patch.object(storage.Store, "_async_write_data", both):
        yield


@pytest.fixture
async def key_files_written(
    hass: HomeAssistant,
    loose_umask: None,
    stores_on_disk: None,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> HomeAssistant:
    """Follow the app's key refresh, repair a lost address, keep a vault and write the export every way the
    integration does; every file of `KEY_FILES` exists afterwards."""
    export = _export(hass)
    await hass.async_add_executor_job(
        lambda: export.parent.mkdir(parents=True, exist_ok=True)
    )
    await hass.async_add_executor_job(shutil.copy, CDB_PATH, export)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={
            CONF_CDB_PATH: str(export),
            CONF_METADATA_DIR: META_DIR,
            CONF_UNICAST: "0D00",
        },
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    hub = entry.runtime_data
    # the app's key refresh, followed: the new NetKey goes into the sequence-number record
    fake_link.inject_from_provisioner(LIGHT_SWITCH, C.netkey_update(NEW_KEY))
    await settle(hass)
    assert hub.proxy.key_refresh_phase == 1
    # the export, every way the configurator writes it: a save (with its backup), the app's upload kept as the
    # merge base, the pre-adoption copy, an adopted export replacing the file
    configurator = MeshConfigurator(hub)
    await configurator.store._write(await configurator.store.read())
    await hass.async_add_executor_job(configurator.store._keep_app_copy)
    await hass.async_add_executor_job(configurator.store._keep_pre_adopt_copy)
    await hass.async_add_executor_job(
        write_private_with_backup, export, export.read_bytes()
    )
    # ... and a reconfigure fetching it again, the export it replaces kept beside it
    incoming = export.with_name(".incoming-flow.json")
    await hass.async_add_executor_job(write_private, incoming, export.read_bytes())
    await hass.async_add_executor_job(_replace_keeping, incoming, export)
    keeper = await async_vault_keeper(hass, MESH_UUID)
    keeper.identity()
    await keeper.async_save()
    assert await hass.config_entries.async_unload(
        entry.entry_id
    )  # the exact counter, written at once
    await hass.async_block_till_done()
    # the seq_store_lost repair of another address: the floor, then both copies (0D00's record stays)
    assert await async_skip_seq_store_ahead(hass, MESH_UUID, "0D05") is not None
    await hass.async_block_till_done()
    store = KEY_FILES["seq store"](hass)
    for name in ("seq store", "seq store backup"):
        text = await hass.async_add_executor_job(KEY_FILES[name](hass).read_text)
        assert '"key_refresh"' in text, name  # the file really holds a key
    # `SeqStore.written` still follows HA's private writer
    assert seq_store_for_uuid(hass, MESH_UUID).written is not None
    assert seq_floor_store_for_uuid(hass, MESH_UUID).written is not None
    assert store.exists()
    return hass


@pytest.mark.parametrize("name", list(KEY_FILES))
async def test_every_key_holding_file_is_owner_only(
    key_files_written: HomeAssistant, name: str
) -> None:
    path = KEY_FILES[name](key_files_written)
    mode = await key_files_written.async_add_executor_job(path.stat)
    assert stat.S_IMODE(mode.st_mode) == 0o600, f"{name}: {oct(mode.st_mode)}"
