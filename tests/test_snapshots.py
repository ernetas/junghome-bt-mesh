"""Registry snapshots: what the integration registers for every synthetic fixture network.

The `.ambr` files under `tests/snapshots/` pin it, at two depths. `test_registry_identity` covers every fixture
network (the base one, blinds, RTR, detectors, puck, the Android share export, and the base network's share export
set up from the gateway, which adds the gateway's REST status entities): per entity its unique id, platform,
translation key, category, `disabled_by` and a `hidden_by` that is set, per device its identifiers, connections
and `via_device` — what a user's installation keys its entities, automations and dashboards on, and what
`tools/gen_entity_reference.py` builds the user guide's entity reference from. Most of those devices are
"unverified on air" (no blind, RTR, detector or puck has been captured), so a unique id changed there would
otherwise orphan every user's entities unseen. The base network is also pinned in full, per platform: names,
capabilities, device classes and the state after the initial refresh; device names, models and serial numbers.
A changed unique id or device identifier (a registry migration in disguise) fails here rather than silently
orphaning the entities of every installation. When a change is intended, regenerate with
`pytest tests/test_snapshots.py --snapshot-update` and review the diff.

The per-platform pass enables every entity (`snapshot_platform` needs a state for each), which hides `disabled_by`;
a second pass sets the platforms with disabled-by-default entities up as shipped and pins which entities start
disabled, so an `entity_registry_enabled_default` change is a reviewed diff too.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    snapshot_platform,
)

from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_GATEWAY_FINGERPRINT,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_PIN_SOURCE,
    CONF_GATEWAY_SYNCED,
    CONF_GATEWAY_TOKEN,
    CONF_METADATA_DIR,
    CONF_SOURCE,
    CONF_UNICAST,
    DOMAIN,
    PIN_FROM_MESH,
    PLATFORMS,
)
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.mesh_config import export_digest

from .conftest import (
    CDB_PATH,
    FIXTURES,
    META_DIR,
    SHARE_EXPORT_PATH,
    FakeProxyLink,
    settle,
    setup_entry,
    wait_for_link,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from syrupy.assertion import SnapshotAssertion

# Platforms with entities that are disabled by default on the fixture network (diagnostic socket sensors, expert
# device parameters); the others register everything enabled, which the first pass shows as `disabled_by: None`.
PLATFORMS_WITH_DISABLED_ENTITIES = ["number", "select", "sensor", "switch", "update"]
# every fixture network: its export and metadata directory (None: a share export, the names travel inside)
NETWORKS: dict[str, tuple[Path, str | None]] = {
    "base": (Path(CDB_PATH), META_DIR),
    "blinds": (FIXTURES / "Blinds.json", None),
    "rtr": (FIXTURES / "MeshNetwork-rtr.json", META_DIR),
    "detectors": (FIXTURES / "MeshNetwork-detectors.json", None),
    "puck": (FIXTURES / "MeshNetwork-puck.json", META_DIR),
    "android": (FIXTURES / "JungHome-android.json", None),
    "gateway": (Path(SHARE_EXPORT_PATH), None),
}
# the networks whose entry is set up from the gateway, as the config flow stores one (no REST call is made: the
# gateway's status entities start disabled, so nothing polls)
GATEWAY_SOURCED: dict[str, dict[str, Any]] = {
    "gateway": {
        CONF_SOURCE: "gateway",
        CONF_GATEWAY_SYNCED: export_digest(
            json.loads(Path(SHARE_EXPORT_PATH).read_text(encoding="utf-8"))
        ),
        CONF_GATEWAY_HOST: "junghome.local",
        CONF_GATEWAY_TOKEN: "tok.en",
        CONF_GATEWAY_FINGERPRINT: "ab" * 32,
        CONF_GATEWAY_PIN_SOURCE: PIN_FROM_MESH,
    },
}


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_platform_entities(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
    init_platform: Callable[[str], Awaitable[MockConfigEntry]],
    snapshot: SnapshotAssertion,
    platform: str,
) -> None:
    """Every entity registry entry and state of one platform, with the disabled-by-default ones enabled.

    The clock is frozen at a synthetic moment: the nodes' *Last seen* diagnostics show when they answered.
    """
    freezer.move_to("2020-01-01T00:00:00+00:00")
    entry = await init_platform(platform)
    if not er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        # A platform nothing on the fixture network needs (covers and thermostats: it has no blind or RTR):
        # `snapshot_platform` refuses an empty platform, so pin the emptiness itself instead.
        assert snapshot == []
        return
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


@pytest.fixture
async def init_platform_as_shipped(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> AsyncGenerator[Callable[[str], Awaitable[MockConfigEntry]]]:
    """`init_platform` without the enable-everything patch: the registry ends up as a user would find it."""
    loaded: list[str] = []
    with patch("custom_components.junghome_ble.PLATFORMS", loaded):

        async def _setup(platform: str) -> MockConfigEntry:
            loaded[:] = [platform]
            await setup_entry(hass, mock_config_entry)
            await wait_for_link(hass, mock_config_entry)
            await settle(hass)
            return mock_config_entry

        yield _setup

        if mock_config_entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(mock_config_entry.entry_id)
            await hass.async_block_till_done()


@pytest.mark.parametrize("platform", PLATFORMS_WITH_DISABLED_ENTITIES)
async def test_platform_enabled_by_default(
    entity_registry: er.EntityRegistry,
    init_platform_as_shipped: Callable[[str], Awaitable[MockConfigEntry]],
    snapshot: SnapshotAssertion,
    platform: str,
) -> None:
    """Which entities of one platform start disabled (`disabled_by`), as the integration ships them."""
    entry = await init_platform_as_shipped(platform)
    entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert entries, f"no {platform} entities registered"
    disabled_by = {
        e.entity_id: e.disabled_by.value if e.disabled_by else None
        for e in sorted(entries, key=lambda e: e.entity_id)
    }
    assert any(disabled_by.values()), (
        f"{platform}: nothing is disabled by default any more"
    )
    assert disabled_by == snapshot


async def test_devices(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    snapshot: SnapshotAssertion,
) -> None:
    """Every device registry entry of the entry, plus the `via_device` link resolved to identifiers.

    The extension replaces `via_device_id` with `<ANY>` (registry ids are random per run), so the topology is
    snapshotted separately: which device each one hangs off, by identifier.
    """
    registry = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(registry, init_integration.entry_id)
    assert devices, "no devices registered"
    by_id = {device.id: device for device in devices}
    for device in sorted(devices, key=lambda device: min(device.identifiers)):
        identifier = min(device.identifiers)[1]
        assert device == snapshot(name=f"{identifier}-device")
        via = by_id[device.via_device_id] if device.via_device_id else None
        assert (sorted(via.identifiers) if via else None) == snapshot(
            name=f"{identifier}-via"
        )


def _value(enum: Any) -> Any:
    """An enum member's value, so the snapshot reads `diagnostic` and survives a renamed member; None stays."""
    return None if enum is None else enum.value


def _pairs(pairs: set[tuple[str, str]]) -> str:
    """Registry identifiers or connections as `domain:value`, sorted, space-separated (`-`: none)."""
    return " ".join(sorted(f"{a}:{b}" for a, b in pairs)) or "-"


@pytest.mark.parametrize("network", NETWORKS)
async def test_registry_identity(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    snapshot: SnapshotAssertion,
    network: str,
) -> None:
    """What a user's installation keys on, for every fixture network, every platform set up as shipped.

    One line per entity, by unique id: platform, translation key, category, `disabled_by`, `hidden_by` when an
    entity starts hidden (only those: the lines of the others read as before) and the device it belongs to; one per
    device, by identifiers: connections and the device it hangs off (`via_device`, by identifiers:
    registry ids are random per run). A line each keeps the `.ambr` reviewable.
    """
    source, metadata = NETWORKS[network]
    path = tmp_path / source.name  # a copy: nothing written next to the fixture
    shutil.copy(source, path)
    # the fake's nodes are this network's (every fixture network has the same NetKey and AppKey)
    fake_link.cdb = CDB.load(path)
    data: dict[str, Any] = {CONF_CDB_PATH: str(path), CONF_UNICAST: "0D00"}
    if metadata is not None:
        data[CONF_METADATA_DIR] = metadata
    data.update(GATEWAY_SOURCED.get(network, {}))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",  # the same NetKey everywhere: the same Network ID
        data=data,
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)

    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    by_id = {device.id: _pairs(device.identifiers) for device in devices}
    entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert entities, f"no entities registered for the {network} network"
    assert {
        "entities": sorted(
            f"{e.unique_id} {e.domain} key={e.translation_key} "
            f"category={_value(e.entity_category)} disabled_by={_value(e.disabled_by)} "
            + (f"hidden_by={_value(e.hidden_by)} " if e.hidden_by else "")
            + f"device={by_id.get(e.device_id or '', '-')}"
            for e in entities
        ),
        "devices": sorted(
            f"{by_id[device.id]} connections={_pairs(device.connections)} "
            f"via={by_id.get(device.via_device_id or '', '-')}"
            for device in devices
        ),
    } == snapshot
