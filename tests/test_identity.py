"""Home Assistant's provisioner identity and key vault in the integration (review-3 N1).

The option is off by default, and then nothing of it reaches a file: the first tests prove the export on disk and
what goes to the gateway are exactly what the library writes without it. With the option on, every file written or
uploaded carries Home Assistant's provisioner entry (after the app's), its node at its address, and the nodes it
provisioned; rooms and scenes it creates take their addresses from its own ranges.

Runs on the `mesh_config` bench (real `ProxyClient`, fake proxy, the export in a temporary directory) and on
`identity.VaultKeeper` over an in-memory store; the storage glue itself against a real `hass`.
"""

# ruff: noqa: F811 - the bench's fixtures are imported from test_mesh_config and requested by name

from __future__ import annotations

import base64
import copy
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome_ble import config_flow, identity
from custom_components.junghome_ble import mesh_config as mc
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_UNICAST,
    DOMAIN,
    ISSUE_ADDRESS_IN_USE,
    ISSUE_ADDRESS_RESERVED,
    OPTION_PROVISIONER_IDENTITY,
)
from custom_components.junghome_ble.identity import VaultKeeper, async_vault_keeper
from custom_components.junghome_ble.jhmesh.export import ProjectFile
from custom_components.junghome_ble.jhmesh.vault import Ranges, Vault

from .conftest import SHARE_EXPORT_PATH, FakeProxyLink, setup_entry
from .helpers import find_issue
from .test_mesh_config import (  # noqa: F401 - fixtures among them
    OUR_SRC,
    Bench,
    FakeGateway,
    MemoryStore,
    app_upload,
    bench,
    fast,
    gateway_doc,
    with_gateway,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

OURS = Ranges((0x0D00, 0x0DFF), (0xFDF5, 0xFEF4), (0xFF00, 0xFFFF))
NEW_UUID = "11111111-2222-4333-8444-555555555555"
NEW_KEY = bytes(range(0x40, 0x50))


def identity_on(bench: Bench) -> None:
    bench.hub.entry.options = {OPTION_PROVISIONER_IDENTITY: True}


def store(bench: Bench) -> MemoryStore:
    return bench.hub.vault._store  # type: ignore[return-value]


def expected_room_file(bench: Bench, room: str) -> ProjectFile:
    """The library alone, without anything of N1: the original export with the room added (from the top)."""
    pf = ProjectFile.loads(bench.original, path=bench.path)
    pf.allocation = "top"
    pf.add_group(room)
    return pf


def ours_in(pf: ProjectFile, vault: Vault) -> tuple[dict[str, Any], dict[str, Any]]:
    """Home Assistant's provisioner entry and node entry in `pf` (the entry is the last provisioner)."""
    provisioner = pf.net["provisioners"][-1]
    assert provisioner["UUID"] == vault.uuid
    node = next(n for n in pf.net["nodes"] if n["UUID"] == vault.uuid)
    return provisioner, node


# ----------------------------------------------------------------------------- option off: nothing changes


async def test_option_off_writes_and_uploads_exactly_what_it_did_before(
    bench: Bench, with_gateway: FakeGateway
) -> None:
    """The default path: byte for byte what the library writes without N1, on disk and at the gateway; the vault
    is never merged (it would raise here) and nothing is stored for a plain change."""
    with_gateway.serve_uploads = True
    with patch.object(Vault, "merge_into", side_effect=AssertionError("merged")):
        assert await bench.configurator.create_room("Attic") == 0xC64B
        exported = await bench.configurator.async_export("share")
        assert await bench.configurator.sync_gateway() is False
    expected = expected_room_file(bench, "Attic")
    written = bench.path.read_bytes()
    expected.net["timestamp"] = ProjectFile.loads(written).net["timestamp"]
    assert written == expected.render().encode()
    assert with_gateway.uploads[0] == json.loads(expected.share_json())
    assert with_gateway.uploads[1] == with_gateway.uploads[0]
    assert exported["export"] == json.loads(expected.share_json())
    assert [p["provisionerName"] for p in expected.net["provisioners"]] == ["iPhone"]
    assert store(bench).saves == []
    assert bench.hub.vault.vault is None


# ----------------------------------------------------------------------------- option on


async def test_option_on_writes_our_provisioner_and_node_into_the_file_and_the_upload(
    bench: Bench, with_gateway: FakeGateway
) -> None:
    identity_on(bench)
    # rooms are allocated in our group range now, not the phone's
    assert await bench.configurator.create_room("Attic") == 0xFDF5
    vault = bench.hub.vault.vault
    assert vault is not None
    assert vault.ranges == OURS
    on_disk = bench.reload()
    provisioner, node = ours_in(on_disk, vault)
    assert [p["provisionerName"] for p in on_disk.net["provisioners"]] == [
        "iPhone",
        "Home Assistant",
    ]  # never first: the iOS library takes the first as its own
    assert provisioner["allocatedUnicastRange"] == [
        {"lowAddress": "0D00", "highAddress": "0DFF"}
    ]
    assert node["unicastAddress"] == f"{OUR_SRC:04X}"
    # HA's address is ours in the file: taken by our node, inside our range, and nobody else's
    assert on_disk.cdb.unicast_is_free(OUR_SRC, own=vault.uuid)
    assert OUR_SRC not in on_disk.cdb.used_unicasts(vault.uuid)
    # what went to the gateway is what is on disk
    assert with_gateway.uploads == [json.loads(on_disk.share_json())]
    # the vault is kept: identity and ranges, no key in the log
    assert Vault.from_dict(store(bench).saves[-1]) == vault
    saves = len(store(bench).saves)
    # the next change neither duplicates the entry nor stores the vault again
    assert await bench.configurator.create_room("Loft") == 0xFDF6
    again = bench.reload()
    assert [p["provisionerName"] for p in again.net["provisioners"]] == [
        "iPhone",
        "Home Assistant",
    ]
    assert sum(n["UUID"] == vault.uuid for n in again.net["nodes"]) == 1
    assert len(store(bench).saves) == saves


async def test_option_on_export_and_sync_carry_the_identity(
    bench: Bench, with_gateway: FakeGateway
) -> None:
    identity_on(bench)
    with_gateway.serve_uploads = True
    exported = await bench.configurator.async_export("share")
    assert bench.file_unchanged()  # rendered, not written
    vault = bench.hub.vault.vault
    assert vault is not None
    assert exported["export"]["meta"] == json.loads(bench.original)["meta"]
    shown = ProjectFile.loads(json.dumps(exported["export"]).encode())
    ours_in(shown, vault)
    # a sync writes the file first, then uploads exactly that
    assert await bench.configurator.sync_gateway() is False
    on_disk = bench.reload()
    ours_in(on_disk, vault)
    assert with_gateway.uploads == [json.loads(on_disk.share_json())]
    assert mc.app_copy_path(bench.path).read_bytes() == bench.original
    # a second sync has nothing to add: the upload is the file as it is
    assert await bench.configurator.sync_gateway() is False
    assert with_gateway.uploads[1] == with_gateway.uploads[0]


async def test_an_adopted_app_upload_gets_the_identity_back_and_goes_up_again(
    bench: Bench, with_gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    """The app never downloads the file: its upload lacks our entry. Adopted before a change, the entry is put
    back and the result handed to the gateway, even with no merge base kept (no change of HA's before)."""
    identity_on(bench)
    with_gateway.serve_uploads = True
    app = ProjectFile.load(bench.path)
    app.add_group("From the app")
    with_gateway.doc = app_upload(app)
    with caplog.at_level(logging.INFO):
        await bench.configurator.create_room("Attic")
    vault = bench.hub.vault.vault
    assert vault is not None
    on_disk = bench.reload()
    ours_in(on_disk, vault)
    assert {"From the app", "Attic"} <= set(on_disk.user_groups().values())
    assert (
        len(with_gateway.uploads) == 2
    )  # the adopted file with our entry, then the change
    assert with_gateway.uploads[-1] == json.loads(on_disk.share_json())
    for upload in with_gateway.uploads:
        ours_in(ProjectFile.loads(json.dumps(upload).encode()), vault)


async def test_an_adopted_export_that_does_not_load_is_left_as_it_is(
    bench: Bench, with_gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    identity_on(bench)
    doc = gateway_doc(bench.path, room="From the app")
    net = json.loads(base64.b64decode(doc["network"]))
    net["nodes"][1]["deviceKey"] = "nope"
    doc["network"] = base64.b64encode(json.dumps(net).encode()).decode()
    with_gateway.doc = doc
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.create_room("Attic")
    assert exc.value.translation_key == "service_export_load_failed"
    assert "provisioner entry was not added to the gateway's export" in caplog.text
    assert with_gateway.uploads == []


async def test_no_range_fits_leaves_the_file_as_it_was_before(
    bench: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    """HA's address inside the phone's range (a second app user took it): no identity, the change goes on."""
    identity_on(bench)
    bench.hub.proxy.state.src = 0x0100
    assert await bench.configurator.create_room("Attic") == 0xC64B
    assert "provisioner entry was left out" in caplog.text
    expected = expected_room_file(bench, "Attic")
    assert bench.reload().net["provisioners"] == expected.net["provisioners"]
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.async_identity_ranges()
    assert exc.value.translation_key == "provisioner_identity_no_range"
    assert "0100" in exc.value.translation_placeholders["error"]


async def test_identity_ranges_are_chosen_once_and_kept(bench: Bench) -> None:
    assert await bench.configurator.async_identity_ranges() == OURS
    assert bench.file_unchanged()
    assert Vault.from_dict(store(bench).saves[-1]).ranges == OURS
    assert await bench.configurator.async_identity_ranges() == OURS
    assert len(store(bench).saves) == 1


def recorded_node(bench: Bench) -> dict[str, Any]:
    """Record a node Home Assistant provisioned (a copy of the dimmer at 0D10) in the vault only; its entry."""
    pf = bench.reload()
    template = next(n for n in pf.net["nodes"] if n["unicastAddress"] == "0300")
    entry = copy.deepcopy(template)
    entry.update(
        UUID=NEW_UUID,
        unicastAddress="0D10",
        deviceKey=NEW_KEY.hex().upper(),
        name="New",
    )
    pf.add_node_entry(entry, [(0xFDF5, "element group #0xD10")])
    vault = bench.hub.vault.identity()
    vault.remember_provisioned(NEW_UUID, 0x0D10, 2, NEW_KEY)
    vault.remember_recorded(pf, NEW_UUID)
    return entry


async def test_a_node_the_file_lost_is_put_back(
    bench: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    identity_on(bench)
    entry = recorded_node(bench)
    await bench.configurator.create_room("Attic")
    on_disk = bench.reload()
    assert next(n for n in on_disk.net["nodes"] if n["UUID"] == NEW_UUID) == entry
    assert on_disk.cdb.groups[0xFDF5] == "element group #0xD10"
    assert on_disk.user_groups()[0xFDF6] == "Attic"  # next to the node's element group
    assert NEW_KEY.hex() not in caplog.text.lower()


async def test_skipped_and_stale_vault_nodes_are_reported(
    bench: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    identity_on(bench)
    recorded_node(bench)
    # someone else's room took the node's element group meanwhile
    pf = bench.reload()
    pf.add_group("Someone's room", address=0xFDF5)
    pf.save(force=True)
    await bench.configurator.create_room("Attic")
    assert "was not put back" in caplog.text
    assert "one of its element groups is in use" in caplog.text
    vault = bench.hub.vault.vault
    assert vault is not None
    assert NEW_UUID in vault.nodes  # kept: it may fit again
    # the file has the node excluded (the app removed it): the vault's copy goes
    pf = bench.reload()
    pf.net["nodes"].append({**vault.nodes[NEW_UUID].entry, "excluded": True})  # type: ignore[dict-item]
    pf.save(force=True)
    await bench.configurator.create_room("Loft")
    assert "the vault's copy is dropped" in caplog.text
    assert NEW_UUID not in vault.nodes
    assert NEW_UUID not in {n["uuid"] for n in store(bench).saves[-1]["nodes"]}


# ----------------------------------------------------------------------------- the store


async def test_keeper_loads_saves_and_sets_an_unreadable_vault_aside(
    caplog: pytest.LogCaptureFixture,
) -> None:
    asides: dict[str, MemoryStore] = {}

    def aside(stamp: str) -> MemoryStore:
        return asides.setdefault(stamp, MemoryStore())

    empty = VaultKeeper(MemoryStore(), aside)  # type: ignore[arg-type]
    await empty.async_load()
    assert empty.own_uuid is None
    await empty.async_save()  # nothing to keep
    assert empty._store.saves == []  # type: ignore[attr-defined]
    vault = empty.identity()
    assert empty.identity() is vault
    await empty.async_save()
    await empty.async_save()  # unchanged: written once
    assert empty._store.saves == [vault.to_dict()]  # type: ignore[attr-defined]

    good = VaultKeeper(MemoryStore(vault.to_dict()), aside)  # type: ignore[arg-type]
    await good.async_load()
    assert good.own_uuid == vault.uuid
    await good.async_save()
    assert good._store.saves == []  # type: ignore[attr-defined]

    # an unreadable vault is copied aside under a name of its own each time, and removed from its place
    for n, version in enumerate((98, 99), start=1):
        bad_data = {**vault.to_dict(), "version": version}
        stored = MemoryStore(bad_data)
        bad = VaultKeeper(stored, aside)  # type: ignore[arg-type]
        with patch.object(
            identity.dt_util,
            "utcnow",
            return_value=datetime(2026, 1, 15, 12, 0, n, tzinfo=UTC),
        ):
            await bad.async_load()
        assert bad.vault is None
        assert stored.data is None
    assert [a.saves for a in asides.values()] == [
        [{**vault.to_dict(), "version": 98}],
        [{**vault.to_dict(), "version": 99}],
    ]
    assert list(asides) == ["20260115T120001000000Z", "20260115T120002000000Z"]
    assert "does not read back" in caplog.text
    assert vault.node_key.hex() not in caplog.text.lower()


async def test_a_device_key_whose_write_failed_silently_is_written_by_the_next_save(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """The lost-device-key scenario (review-4 D15): HA's `Store.async_save` swallows a `WriteError` (a full disk, a
    filesystem remounted read-only) with only a log line. The keeper must not take that for written: the next save
    retries, so a restart finds the device key Home Assistant just handed out."""
    keeper = await async_vault_keeper(hass, "abcd-mesh")
    keeper.identity().remember_provisioned(NEW_UUID, 0x0D10, 2, NEW_KEY)
    with patch(
        "homeassistant.helpers.storage.Store._async_write_data",
        side_effect=WriteError("read-only file system"),
    ):
        landed = await keeper.async_save()
    key = "junghome_ble.vault.abcd-mesh"
    assert key not in hass_storage
    assert landed is False
    assert keeper.write_error == "read-only file system"
    assert await keeper.async_save()  # writable again, nothing changed in memory since
    assert keeper.write_error is None
    for stored in (key, f"{key}.backup"):
        nodes = hass_storage[stored]["data"]["nodes"]
        assert [(n["uuid"], n["deviceKey"]) for n in nodes] == [
            (NEW_UUID, NEW_KEY.hex().upper())
        ]
    # what a restart reads
    hass.data.pop(identity.VAULT_KEEPERS)
    again = await async_vault_keeper(hass, "abcd-mesh")
    assert again.vault is not None
    assert again.vault.nodes[NEW_UUID].dev_key == NEW_KEY


async def test_a_failed_write_is_retried_and_listeners_hear_of_every_save() -> None:
    """`async_save` says whether the vault landed; a failed one is written by the next save even unchanged, and a
    copy that fails is retried the same way without failing the save."""
    store, backup = MemoryStore(), MemoryStore(path="memory.backup")
    keeper = VaultKeeper(store, lambda _s: MemoryStore(), backup)  # type: ignore[arg-type]
    heard: list[str | None] = []
    remove = keeper.async_add_listener(lambda: heard.append(keeper.write_error))
    assert keeper.path == "memory"
    keeper.identity().remember_provisioned(NEW_UUID, 0x0D10, 2, NEW_KEY)
    store.fail = backup.fail = "disk full"
    assert not await keeper.async_save()
    assert keeper.write_error == "disk full"
    assert (
        store.saves == backup.saves == []
    )  # the copy follows only a write that landed
    store.fail = None
    assert (
        await keeper.async_save()
    )  # the copy still fails: retried next time, the save landed all the same
    assert backup.write_error == "disk full"
    backup.fail = None
    assert await keeper.async_save()
    assert len(store.saves) == 1  # unchanged and written: not again
    assert backup.saves == store.saves
    assert await keeper.async_save()
    assert len(backup.saves) == 1
    assert heard == ["disk full", None, None, None]
    remove()
    await keeper.async_save()
    assert len(heard) == 4


async def test_a_vault_not_written_for_another_reason_says_so() -> None:
    """A write that neither landed nor reported an error (Home Assistant stopping: `Store` defers it)."""

    class Deferred(MemoryStore):
        async def async_save(self, data: dict[str, Any]) -> None:
            pass

    keeper = VaultKeeper(Deferred(), lambda _s: MemoryStore())  # type: ignore[arg-type]
    keeper.identity()
    assert not await keeper.async_save()
    assert keeper.write_error == "not written"


async def test_an_unreadable_vault_whose_copy_fails_is_kept_untouched(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review-4 D15: the original is removed only once its copy aside landed. Until then it stays as it is, the
    vault lives in memory, and every save tries the copy again before it writes."""
    vault = Vault.create()
    bad_data = {**vault.to_dict(), "version": 99}
    stored = MemoryStore(bad_data)
    aside = MemoryStore(path="memory.unreadable")
    aside.fail = "read-only file system"
    keeper = VaultKeeper(stored, lambda _s: aside)  # type: ignore[arg-type]
    await keeper.async_load()
    assert keeper.vault is None
    assert stored.data is bad_data  # kept
    assert (
        "could not be copied to memory.unreadable (read-only file system)"
        in caplog.text
    )
    keeper.identity().remember_provisioned(NEW_UUID, 0x0D10, 2, NEW_KEY)
    assert not await keeper.async_save()
    assert (
        keeper.write_error
        == "the unreadable vault in its place could not be copied aside"
    )
    assert stored.data is bad_data  # not written over
    aside.fail = None
    assert await keeper.async_save()
    assert aside.saves == [bad_data]
    assert stored.data is not None
    assert Vault.from_dict(stored.data).nodes[NEW_UUID].dev_key == NEW_KEY
    assert NEW_KEY.hex() not in caplog.text.lower()


async def test_the_backup_copy_stands_in_for_a_missing_or_unreadable_vault(
    caplog: pytest.LogCaptureFixture,
) -> None:
    vault = Vault.create()
    vault.remember_provisioned(NEW_UUID, 0x0D10, 2, NEW_KEY)
    good = vault.to_dict()
    # missing (Home Assistant renamed a file that is no JSON aside): the copy, written back in its place
    store, backup = MemoryStore(), MemoryStore(good, path="memory.backup")
    keeper = VaultKeeper(store, lambda _s: MemoryStore(), backup)  # type: ignore[arg-type]
    await keeper.async_load()
    assert keeper.vault == vault
    assert store.saves == [good]
    assert backup.saves == []  # it holds that already
    assert "its backup copy memory.backup is used" in caplog.text
    # unreadable: set aside, then the copy takes its place
    asides: dict[str, MemoryStore] = {}

    def aside(stamp: str) -> MemoryStore:
        return asides.setdefault(stamp, MemoryStore())

    bad = {**good, "version": 99}
    store, backup = MemoryStore(bad), MemoryStore(good, path="memory.backup")
    keeper = VaultKeeper(store, aside, backup)  # type: ignore[arg-type]
    await keeper.async_load()
    assert keeper.vault == vault
    assert [a.saves for a in asides.values()] == [[bad]]
    assert store.saves == [good]
    # both unreadable: both set aside (the copy's under a name of its own), a new vault begun
    asides.clear()
    store, backup = MemoryStore(bad), MemoryStore(bad, path="memory.backup")
    keeper = VaultKeeper(store, aside, backup)  # type: ignore[arg-type]
    with patch.object(
        identity.dt_util,
        "utcnow",
        return_value=datetime(2000, 1, 1, 0, 0, 1, tzinfo=UTC),
    ):
        await keeper.async_load()
    assert keeper.vault is None
    assert list(asides) == [
        "20000101T000001000000Z",
        "20000101T000001000000Z.backup",
    ]
    assert store.data is None
    assert backup.data is None
    # nothing at all: nothing to read
    keeper = VaultKeeper(MemoryStore(), aside, MemoryStore())  # type: ignore[arg-type]
    await keeper.async_load()
    assert keeper.vault is None
    assert NEW_KEY.hex() not in caplog.text.lower()


async def test_a_readable_vault_brings_its_copy_up_to_date() -> None:
    """A vault from before the copy existed gets one at load; a copy that matches is not written again."""
    good = Vault.create().to_dict()
    store, backup = MemoryStore(good), MemoryStore(path="memory.backup")
    keeper = VaultKeeper(store, lambda _s: MemoryStore(), backup)  # type: ignore[arg-type]
    await keeper.async_load()
    assert backup.saves == [good]
    assert store.saves == []
    store, backup = MemoryStore(good), MemoryStore(good, path="memory.backup")
    keeper = VaultKeeper(store, lambda _s: MemoryStore(), backup)  # type: ignore[arg-type]
    await keeper.async_load()
    assert await keeper.async_save()
    assert store.saves == backup.saves == []


async def test_an_old_vault_file_loads_and_gets_its_copy(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A vault file an earlier version wrote (store version 1, a pending node without planned groups, no copy)."""
    key = "junghome_ble.vault.abcd-mesh"
    old = {
        "version": 1,
        "uuid": NEW_UUID,
        "name": "Home Assistant",
        "nodeKey": "00" * 16,
        "ranges": None,
        "nodes": [
            {
                "uuid": "00005EFF-FE00-5377-0000-000000000000",
                "unicast": "0D10",
                "elements": 2,
                "deviceKey": NEW_KEY.hex().upper(),
                "entry": None,
                "groups": [],
                "devices": [],
            }
        ],
    }
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": old}
    keeper = await async_vault_keeper(hass, "abcd-mesh")
    assert keeper.vault is not None
    assert [n.unicast for n in keeper.vault.groups_unknown] == [0x0D10]
    assert hass_storage[f"{key}.backup"]["data"] == old


async def test_one_keeper_per_mesh_in_private_storage(hass: HomeAssistant) -> None:
    keeper = await async_vault_keeper(hass, "ABCD-mesh")
    assert await async_vault_keeper(hass, "abcd-mesh") is keeper
    assert keeper.vault is None
    keeper.identity()
    await keeper.async_save()
    assert keeper._store.key == "junghome_ble.vault.abcd-mesh"
    assert keeper._aside("T1").key == "junghome_ble.vault.abcd-mesh.unreadable.T1"
    assert keeper._backup is not None
    assert keeper._backup.key == "junghome_ble.vault.abcd-mesh.backup"
    assert all(
        isinstance(s, identity.TrackedStore) and s._private
        for s in (keeper._store, keeper._backup, keeper._aside("T1"))
    )
    assert identity.VAULT_STORAGE_VERSION == 1


# ----------------------------------------------------------------------------- a lost vault


def merged_export(tmp_path: Path) -> tuple[Path, Vault]:
    """A copy of the fixture export into which an earlier run merged Home Assistant's identity at 0D00."""
    path = tmp_path / "JungHome.json"
    shutil.copy(SHARE_EXPORT_PATH, path)
    pf = ProjectFile.load(path)
    vault = Vault.create()
    vault.merge_into(pf, OUR_SRC)
    pf.save(force=True)
    return path, vault


async def test_recover_takes_the_identity_back_from_the_file() -> None:
    """The keeper's side: no vault, or a fresh one the file does not know — the file's entry is ours again."""
    pf = ProjectFile.load(Path(SHARE_EXPORT_PATH))
    old = Vault.create()
    old.merge_into(pf, OUR_SRC)
    lost = VaultKeeper(MemoryStore(), lambda _s: MemoryStore())  # type: ignore[arg-type]
    assert await lost.async_recover(pf.cdb, OUR_SRC)
    assert lost.own_uuid == old.uuid
    assert lost._store.saves  # type: ignore[attr-defined]
    assert not await lost.async_recover(pf.cdb, OUR_SRC)  # already ours
    fresh = VaultKeeper(MemoryStore(), lambda _s: MemoryStore())  # type: ignore[arg-type]
    fresh.identity().remember_provisioned(NEW_UUID, 0x0D10, 2, NEW_KEY)
    assert await fresh.async_recover(pf.cdb, OUR_SRC)
    assert fresh.own_uuid == old.uuid
    assert NEW_UUID in fresh.identity().nodes  # its nodes stay
    # a vault whose own entry the file has as well is not replaced by another one that looks like ours
    both = VaultKeeper(MemoryStore(), lambda _s: MemoryStore())  # type: ignore[arg-type]
    other = both.identity()
    other.merge_into(pf, 0x0E00)
    assert not await both.async_recover(pf.cdb, OUR_SRC)
    assert both.own_uuid == other.uuid
    # nothing of ours in the file: nothing to take
    assert not await both.async_recover(
        ProjectFile.load(Path(SHARE_EXPORT_PATH)).cdb, OUR_SRC
    )


@pytest.mark.parametrize("stored", [None, {"version": 99}], ids=["lost", "unreadable"])
async def test_setup_recovers_a_lost_or_unreadable_vault(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    stored: dict[str, Any] | None,
) -> None:
    """The export names Home Assistant (node at its address, its range) from a run whose vault is gone: the setup
    takes the identity back instead of refusing its own address as in use."""
    path, old = merged_export(tmp_path)
    mesh = ProjectFile.load(path).cdb.mesh_uuid.lower()
    key = f"{DOMAIN}.vault.{mesh}"
    if stored is not None:
        hass_storage[key] = {
            "version": 1,
            "minor_version": 1,
            "key": key,
            "data": stored,
        }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={CONF_CDB_PATH: str(path), CONF_UNICAST: "0D00", "source": "upload"},
        options={OPTION_PROVISIONER_IDENTITY: True},
    )
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.vault.own_uuid == old.uuid
    assert find_issue(hass, ISSUE_ADDRESS_IN_USE) is None
    assert find_issue(hass, ISSUE_ADDRESS_RESERVED) is None
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["uuid"] == old.uuid
    if stored is not None:
        assert [
            k for k in hass_storage if k.startswith(f"{key}.unreadable.")
        ]  # the unreadable one kept aside
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_config_flow_knows_our_entry_without_a_vault(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    path, old = merged_export(tmp_path)
    cdb = ProjectFile.load(path).cdb
    assert await config_flow._own_uuid(hass, cdb, OUR_SRC) == old.uuid  # recognised
    keeper = await async_vault_keeper(hass, cdb.mesh_uuid)
    assert keeper.vault is None  # nothing written by the flow
    keeper.vault = old
    assert await config_flow._own_uuid(hass, cdb, OUR_SRC) == old.uuid
    keeper.vault = Vault.create()
    other = ProjectFile.load(path)
    keeper.vault.merge_into(other, 0x0E00)
    assert await config_flow._own_uuid(hass, other.cdb, 0x0E00) == keeper.vault.uuid


# ----------------------------------------------------------------------------- failures


async def test_a_merge_that_fails_stops_the_action_and_changes_nothing(
    bench: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed stored entry (marked excluded: the merge fails past the validation) — the export stays as it
    was, the action fails with a translated error, nothing of the key reaches the log."""
    identity_on(bench)
    recorded_node(bench)
    vault = bench.hub.vault.vault
    assert vault is not None
    vault.nodes[NEW_UUID].entry["excluded"] = True  # type: ignore[index]
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.create_room("Attic")
    assert exc.value.translation_key == "provisioner_identity_failed"
    assert exc.value.translation_placeholders == {"error": "AssertionError"}
    assert bench.file_unchanged()
    assert NEW_KEY.hex() not in caplog.text.lower()
