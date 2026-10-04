"""The read-only Firmware `update` entity per node (review-4 F4-18, U4-11): the node's version against the app's.

The table is checked against `docs/android/firmware-products.md`, which it is taken from; the entity's state for
an equal, an older and an unknown version; and that nothing installs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.update import (
    ATTR_INSTALLED_VERSION,
    ATTR_LATEST_VERSION,
    ATTR_RELEASE_SUMMARY,
    UpdateEntityFeature,
)
from homeassistant.const import (
    ATTR_SUPPORTED_FEATURES,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.junghome_ble.const import (
    DOMAIN,
)
from custom_components.junghome_ble.jhmesh import properties as P
from custom_components.junghome_ble.jhmesh.properties import (
    SIG_SOFTWARE_VERSION,
)
from custom_components.junghome_ble.update import (
    BUNDLED_FIRMWARE,
    JungHomeFirmware,
    build_entities,
)

from .helpers import NODE_GATEWAY, NODE_LIGHT_SWITCH, NODE_SOCKET, entity_id

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

DOCS = Path(__file__).parent.parent / "docs" / "android" / "firmware-products.md"


def uid(node: str) -> str:
    return f"{node}-firmware"


def bundled_in_the_docs() -> dict[int, str]:
    """product id → version of every `lb-connect-*` row of the table (the STM32 co-processor row left out)."""
    out: dict[int, str] = {}
    for line in DOCS.read_text().splitlines():
        row = re.match(r"\| `lb-connect-[^`]+` \| ([\d.]+) \| ([^|]+) \|", line)
        if row is None:
            continue
        for part in row[2].split(","):
            first, _, last = part.strip().partition("\N{EN DASH}")
            for pid in range(int(first), int(last or first) + 1):
                out[pid] = row[1]
    return out


def test_the_table_is_the_apps() -> None:
    assert bundled_in_the_docs() == BUNDLED_FIRMWARE


def report_version(
    hass: HomeAssistant, entry: MockConfigEntry, node: int, v: str
) -> None:
    hub = entry.runtime_data
    hub.element_state(node).properties[SIG_SOFTWARE_VERSION] = P.ASCII_VERSION.encode(v)
    hub.notify_update(node)


async def test_equal_older_and_unknown_versions(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    init_integration: MockConfigEntry,
) -> None:
    """Equal or newer: no update; older: an update, pointing to the app; not reported yet: unknown."""
    eid = entity_id(hass, "update", uid(NODE_LIGHT_SWITCH))
    switch = init_integration.runtime_data.cdb.node_by_addr(0x0148)
    assert switch is not None
    switch.pid = 1  # a push-button: the app bundles 2.2.0.2

    report_version(hass, init_integration, 0x0148, "2.2.0.2")
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state is not None
    assert state.state == STATE_OFF
    assert state.attributes[ATTR_INSTALLED_VERSION] == "2.2.0.2"
    assert state.attributes[ATTR_LATEST_VERSION] == "2.2.0.2"
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == UpdateEntityFeature(0)
    assert "Update it with the JUNG HOME app" in state.attributes[ATTR_RELEASE_SUMMARY]

    report_version(hass, init_integration, 0x0148, "2.1.9.0")
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_ON  # type: ignore[union-attr]
    report_version(hass, init_integration, 0x0148, "2.2.1.0")
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_OFF  # type: ignore[union-attr]

    switch.pid = 0x7F  # a product the app has no image for
    report_version(hass, init_integration, 0x0148, "2.2.0.2")
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_LATEST_VERSION] is None
    assert state.attributes[ATTR_RELEASE_SUMMARY] is None


async def test_a_version_not_reported_yet_is_unknown(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    init_integration: MockConfigEntry,
) -> None:
    hub = init_integration.runtime_data
    socket = hub.cdb.node_by_addr(0x0172)
    assert socket is not None
    hub.element_state(socket.unicast).properties.pop(SIG_SOFTWARE_VERSION, None)
    hub.notify_update(socket.unicast)
    await hass.async_block_till_done()
    state = hass.states.get(entity_id(hass, "update", uid(NODE_SOCKET)))
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_INSTALLED_VERSION] is None


async def test_nothing_installs(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    init_integration: MockConfigEntry,
) -> None:
    """No install feature, so `update.install` is refused by Home Assistant; the entity refuses it too."""
    eid = entity_id(hass, "update", uid(NODE_LIGHT_SWITCH))
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "update", "install", {"entity_id": eid}, blocking=True
        )
    firmware = next(
        e
        for e in build_entities(init_integration.runtime_data)
        if isinstance(e, JungHomeFirmware) and e.node.unicast == 0x0148
    )
    with pytest.raises(HomeAssistantError) as err:
        await firmware.async_install(None, backup=False)
    assert err.value.translation_key == "firmware_update_in_app"
    assert err.value.translation_domain == DOMAIN


async def test_one_per_node_but_the_gateway_diagnostic_and_off_by_default(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    entries: list[Any] = [
        e
        for e in er.async_entries_for_config_entry(registry, init_integration.entry_id)
        if e.domain == "update"
    ]
    hub = init_integration.runtime_data
    nodes = [n for n in hub.cdb.nodes if n.pid is not None and n.pid not in P.GATEWAY]
    assert sorted(e.unique_id for e in entries) == sorted(
        uid(n.uuid.lower()) for n in nodes
    )
    assert registry.async_get_entity_id("update", DOMAIN, uid(NODE_GATEWAY)) is None
    for entry in entries:
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert entry.entity_category is EntityCategory.DIAGNOSTIC
        assert entry.translation_key == "firmware"
