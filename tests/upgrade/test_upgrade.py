"""What a released version stored, loaded into the current code (Q4 C4).

Each `v<version>.json` here is the state a released version left behind on the fixture network: its config entry
(version, minor version, data, options) and the integration's `.storage` documents, with the fixture directory
written as `{fixtures}`. `v1.0.0.json` was produced by the v1.0.0 tag's own test harness: `init_integration` with an
answering mesh, the *WC mirror* light switched on and off, the entry unloaded, then the entry and every
`junghome_ble.*` document of `hass_storage` dumped. It is synthetic (the fixture network, the fixture's keys), and
holds no clock time. Add one per release the same way, from that release's tag.

The current code must set the entry up from it, migrate the entry and the stores to the versions it writes now,
carry the sequence number on (the first PDU it sends is numbered above everything the release sent: no nonce is
reused across the upgrade) and keep the replay list the release stored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome_ble.config_flow import JungHomeConfigFlow
from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.seq_store import SEQ_STORAGE_MINOR_VERSION
from tests.conftest import FIXTURES, FakeProxyLink, settle, setup_entry, wait_for_link
from tests.helpers import LIGHT_SWITCH, UID_LIGHT_SWITCH, entity_id, onoff_status

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

RELEASES = sorted(Path(__file__).parent.glob("v*.json"))
OUR_ADDRESS = "0D00"


def load_release(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8").replace("{fixtures}", FIXTURES.as_posix())
    return json.loads(text)


def test_there_is_a_release() -> None:
    assert "v1.0.0.json" in {p.name for p in RELEASES}


@pytest.mark.parametrize("path", RELEASES, ids=lambda p: p.stem)
async def test_upgrade_from_a_release(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    path: Path,
) -> None:
    release = load_release(path)
    stored = release["storage"]
    hass_storage.update(json.loads(json.dumps(stored)))
    entry = MockConfigEntry(domain=DOMAIN, **release["entry"])
    seq_key = next(
        k
        for k in stored
        if k.startswith(f"{DOMAIN}.seq.") and not k.endswith(".backup")
    )
    record = stored[seq_key]["data"]["addresses"][OUR_ADDRESS]

    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)

    assert entry.state is ConfigEntryState.LOADED
    assert (entry.version, entry.minor_version) == (
        JungHomeConfigFlow.VERSION,
        JungHomeConfigFlow.MINOR_VERSION,
    )
    # the sequence number goes on from where the release stopped: nothing it sent is sent again
    assert fake_link.rpl[int(OUR_ADDRESS, 16)][1] >= record["seq"]
    assert not fake_link.replayed

    # the release's replay list holds: a status the release had already seen is dropped, a newer one taken
    light = entity_id(hass, "light", UID_LIGHT_SWITCH)
    before = hass.states.get(light).state
    last_seen = record["rpl"][f"{LIGHT_SWITCH:04X}"][1]
    fake_link.src_seq[LIGHT_SWITCH] = (
        last_seen - 1
    )  # `inject` sends the next one: the number the release saw
    fake_link.inject(LIGHT_SWITCH, 0xC061, onoff_status(before != "on"))
    await hass.async_block_till_done()
    assert hass.states.get(light).state == before
    fake_link.inject(LIGHT_SWITCH, 0xC061, onoff_status(before != "on"))
    await hass.async_block_till_done()
    assert hass.states.get(light).state != before

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    migrated = hass_storage[seq_key]
    assert (migrated["version"], migrated["minor_version"]) == (
        stored[seq_key]["version"],
        SEQ_STORAGE_MINOR_VERSION,
    )
    assert migrated["data"]["addresses"][OUR_ADDRESS]["seq"] > record["seq"]
