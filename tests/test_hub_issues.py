"""The hub's repair issues (`hub/issues.py`): the key refresh and the stale export the traffic shows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers import issue_registry as ir

from custom_components.junghome_ble.const import (
    EXPORT_STALE_THRESHOLD,
    ISSUE_EXPORT_STALE,
    ISSUE_KEY_REFRESH,
)
from custom_components.junghome_ble.jhmesh.crypto import NetKeyMaterial, aes_cmac
from custom_components.junghome_ble.jhmesh.pdu import (
    PROXY_BEACON,
    PROXY_NETWORK_PDU,
    network_encrypt,
)

from .conftest import (
    PROXY_ADDRESS,
    FakeProxyLink,
    make_service_info,
    wait_for_link,
)
from .helpers import (
    LIGHT_SWITCH,
    find_issue,
    onoff_status,
)
from .test_coordinator import (
    GROUP_SWITCH,
    SECOND_PROXY,
    hub_of,
)
from .test_coordinator import (
    no_property_reads as no_property_reads,  # noqa: PLC0414  # the autouse fixture
)

if TYPE_CHECKING:
    import pytest
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_key_refresh_beacon_raises_a_repair_issue(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    fake_link.inject_beacon(iv_index=0, key_refresh=False)
    await hass.async_block_till_done()
    assert find_issue(hass, ISSUE_KEY_REFRESH) is None

    # review-3 T3: flagged and authenticated means our keys are the new ones already — nothing to do
    fake_link.inject_beacon(iv_index=0, key_refresh=True)
    await hass.async_block_till_done()
    assert find_issue(hass, ISSUE_KEY_REFRESH) is None

    # Phase 2 beacons are secured with the new key: the one our key cannot open is the sign
    fake_link.inject_beacon(iv_index=0, key_refresh=True, new_key=True)
    await hass.async_block_till_done()
    issue = find_issue(hass, ISSUE_KEY_REFRESH)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.is_fixable  # by a new export (`repairs.NewExportFlow`)
    assert issue.data == {"entry_id": init_integration.entry_id}
    assert issue.translation_key == ISSUE_KEY_REFRESH
    assert issue.translation_placeholders == {"title": "JUNG HOME mesh test"}


async def test_key_refresh_issue_is_cleared_by_a_successful_restart(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The remedy is a reconfiguration with a new export, which reloads the entry."""
    fake_link.inject_beacon(key_refresh=True, new_key=True)
    assert find_issue(hass, ISSUE_KEY_REFRESH) is not None

    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await wait_for_link(hass, init_integration)
    assert find_issue(hass, ISSUE_KEY_REFRESH) is None

    fake_link.inject_beacon(
        key_refresh=True, new_key=True
    )  # a mesh that is still refreshing raises it again
    assert find_issue(hass, ISSUE_KEY_REFRESH) is not None


def inject_foreign(link: FakeProxyLink, count: int, *, beacon: bool = False) -> None:
    """Deliver traffic encrypted with another network's keys: what a proxy forwards after a completed key refresh."""
    foreign = NetKeyMaterial.derive(bytes(range(16)))
    for _ in range(count):
        link._deliver(
            PROXY_NETWORK_PDU,
            network_encrypt(
                foreign,
                0,
                False,
                3,
                link._next(),
                LIGHT_SWITCH,
                GROUP_SWITCH,
                b"\x00" + bytes(8),
            ),
        )
    if beacon:
        body = b"\x00" + foreign.network_id + bytes(4)
        link._deliver(
            PROXY_BEACON, b"\x01" + body + aes_cmac(foreign.beacon_key, body)[:8]
        )


async def test_undecryptable_traffic_alone_raises_the_export_stale_repair(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A link that forwards only PDUs our keys cannot open (and a beacon they cannot authenticate) is a mesh whose keys changed."""
    hub = hub_of(init_integration)
    assert hub.proxy.rx_undecryptable == 0

    inject_foreign(
        fake_link, EXPORT_STALE_THRESHOLD - 2, beacon=True
    )  # 19 with the beacon: one short
    await hass.async_block_till_done()
    assert hub.proxy.rx_undecryptable == EXPORT_STALE_THRESHOLD - 2
    assert find_issue(hass, ISSUE_EXPORT_STALE) is None

    inject_foreign(fake_link, 1)
    await hass.async_block_till_done()
    issue = find_issue(hass, ISSUE_EXPORT_STALE)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.is_fixable  # by a new export (`repairs.NewExportFlow`)
    assert issue.translation_key == ISSUE_EXPORT_STALE
    assert issue.translation_placeholders == {"title": "JUNG HOME mesh test"}
    assert (
        f"Nothing heard through proxy node {PROXY_ADDRESS} can be decrypted with the keys of the export "
        f"({EXPORT_STALE_THRESHOLD} messages so far)" in caplog.text
    )
    inject_foreign(fake_link, 5)  # raised once, not on every further PDU
    await hass.async_block_till_done()
    assert caplog.text.count("can be decrypted with the keys of the export") == 1

    # the first message our keys open proves the export fits: the issue goes away
    fake_link.inject(LIGHT_SWITCH, GROUP_SWITCH, onoff_status(True))
    await hass.async_block_till_done()
    assert find_issue(hass, ISSUE_EXPORT_STALE) is None
    assert hub.states[LIGHT_SWITCH].on is True


async def test_undecryptable_traffic_next_to_decodable_traffic_is_no_stale_export(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Another mesh in range, or a node the export does not know: as long as something decodes, the keys are fine."""
    fake_link.inject(LIGHT_SWITCH, GROUP_SWITCH, onoff_status(False))
    inject_foreign(fake_link, EXPORT_STALE_THRESHOLD * 2, beacon=True)
    await hass.async_block_till_done()
    assert find_issue(hass, ISSUE_EXPORT_STALE) is None
    # an authenticated beacon is not "undecodable" either
    fake_link.inject_beacon()
    hub = hub_of(init_integration)
    assert hub.issues.undecodable_link == EXPORT_STALE_THRESHOLD * 2 + 1
    stats = hub.proxy.link_stats
    assert (stats.undecryptable, stats.beacons_unauthenticated) == (
        EXPORT_STALE_THRESHOLD * 2,
        1,
    )


async def test_export_stale_counts_restart_with_every_link(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """A reconnect starts from zero: the old link's undecryptable PDUs do not count against the new one (and vice versa)."""
    hub = hub_of(init_integration)
    inject_foreign(fake_link, EXPORT_STALE_THRESHOLD - 1)
    mock_bluetooth_env["infos"].append(
        make_service_info(network_id, address=SECOND_PROXY, rssi=-40)
    )
    fake_link.drop_link()
    await wait_for_link(hass, init_integration, connected=False)
    await wait_for_link(hass, init_integration)
    assert hub.proxy_address == SECOND_PROXY
    assert (hub.issues.undecodable_link, hub.proxy.rx_undecryptable) == (0, 0)
    inject_foreign(fake_link, EXPORT_STALE_THRESHOLD - 1)
    await hass.async_block_till_done()
    assert find_issue(hass, ISSUE_EXPORT_STALE) is None
    inject_foreign(fake_link, 1)
    await hass.async_block_till_done()
    assert find_issue(hass, ISSUE_EXPORT_STALE) is not None

    # a successful restart (the reconfiguration with a fresh export reloads the entry) clears it
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await wait_for_link(hass, init_integration)
    assert find_issue(hass, ISSUE_EXPORT_STALE) is None
