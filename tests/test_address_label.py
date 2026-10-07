"""Errors name a device as the device list does (`device_info.address_label`), the address in brackets.

Set up from a copy of the fixture share export (synthetic keys) in the test's directory: renaming a load's device
writes the name into the export Home Assistant holds, which must not be the fixture itself.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome_ble.const import CONF_CDB_PATH, CONF_UNICAST, DOMAIN
from custom_components.junghome_ble.device_info import (
    address_label,
    button_gang,
    buttons_device_id,
    node_identifier,
)
from custom_components.junghome_ble.jhmesh.devices import Button

from .conftest import FIXTURES

if TYPE_CHECKING:
    from pathlib import Path

    from homeassistant.core import HomeAssistant


@pytest.fixture
def mock_config_entry(tmp_path: Path) -> MockConfigEntry:
    """The entry `init_integration` sets up: one set up from a copy of the share export."""
    path = tmp_path / "junghome" / "JungHome.json"
    path.parent.mkdir()
    shutil.copy(FIXTURES / "JungHome.json", path)
    return MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1baf3ade-0000-4000-8000-000000000001",
        data={CONF_CDB_PATH: str(path), CONF_UNICAST: "0D00"},
    )


async def test_every_kind_of_address_by_the_name_its_device_shows(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A load by its own device, a key by its gang's device and letter, another element by its node's device; an
    address no node has stays bare. The user's rename comes first, as in the device list."""
    hub = init_integration.runtime_data
    assert address_label(hub, 0x0148) == "WC mirror (share) (0148)"  # the light
    assert address_label(hub, 0x0149) == "Push-button 1-gang 0148 buttons A (0149)"
    # the socket's meter element: its node device, whose fallback name says the node's address
    assert address_label(hub, 0x0173) == "Socket 0172 (0173)"
    assert address_label(hub, 0x0300) == "Push-button 1-gang 0300"  # said once
    assert address_label(hub, 0x0D00) == "0D00"  # Home Assistant's own
    assert address_label(hub, 0xC001) == "C001"  # a group
    registry = dr.async_get(hass)
    socket = hub.cdb.node_by_addr(0x0172)
    key = hub.devices.by_address[0x0149]
    assert socket is not None
    assert isinstance(key, Button)
    for identifier, name in (
        (node_identifier(socket), "Boiler socket"),
        (buttons_device_id(button_gang(hub, key)), "Mirror keys"),
        (hub.devices.by_address[0x0148].unique_id, "Mirror light"),
    ):
        device = registry.async_get_device_by_identifier(
            (DOMAIN, identifier), init_integration.entry_id
        )
        assert device is not None
        registry.async_update_device(device.id, name_by_user=name)
    assert address_label(hub, 0x0173) == "Boiler socket (0173)"
    assert address_label(hub, 0x0149) == "Mirror keys A (0149)"
    assert address_label(hub, 0x0148) == "Mirror light (0148)"
