"""The gateway's own status over its REST API (`gateway_status.py`): the app's gateway pages as diagnostics.

The gateway client is replaced by a fake answering `GET config` / `GET healthstatus` from what each test sets;
the REST calls themselves have their tests in test_gateway_api.py.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_GATEWAY_FINGERPRINT,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_LAST_SYNC,
    CONF_GATEWAY_PIN_SOURCE,
    CONF_GATEWAY_SYNCED,
    CONF_GATEWAY_TOKEN,
    CONF_SOURCE,
    CONF_UNICAST,
    DOMAIN,
    GATEWAY_HEALTH_INTERVAL,
    GATEWAY_STATUS_INTERVAL,
    ISSUE_GATEWAY_CERTIFICATE,
    ISSUE_GATEWAY_TOKEN,
    PIN_FROM_MESH,
    PIN_FROM_USER,
)
from custom_components.junghome_ble.coordinator import issue_id
from custom_components.junghome_ble.gateway_api import (
    GatewayAuthError,
    GatewayBusy,
    GatewayCertificateMismatch,
    GatewayConfig,
    GatewayHealthEntry,
    GatewayUnreachable,
    JungHomeGatewayApi,
)
from custom_components.junghome_ble.gateway_status import gateway_polls
from custom_components.junghome_ble.mesh_config import export_digest

from .conftest import SHARE_EXPORT_PATH, settle, setup_entry, wait_for_link
from .helpers import NODE_GATEWAY, entity_id

if TYPE_CHECKING:
    from collections.abc import Generator

    from homeassistant.core import HomeAssistant

    from .conftest import FakeProxyLink

HOST = "junghome.local"
SYNCED = export_digest(json.loads(Path(SHARE_EXPORT_PATH).read_text()))
CONFIG = GatewayConfig(
    release="2.1.3",
    build="2840",
    serial="1234567890",
    cloud_registered=True,
    cloud_connected=True,
    mesh_device_missing=False,
    mesh_error=False,
    cloud_error=False,
    ip_error=True,
    api_clients=("Home Assistant (Bluetooth Mesh)", "junghome"),
    clients_asking=("Someone",),
)
LOG = [
    GatewayHealthEntry("ERROR", "2026-01-15 10:00:00", "Cloud", "no route"),
    GatewayHealthEntry("DEBUG", "2026-01-15 10:00:01", "noise", ""),
    GatewayHealthEntry("WARNING", "2026-01-15 10:00:02", "Mesh", "slow"),
]


class FakeRest:
    """What the gateway answers: a value, or an exception to raise; the hosts every call went to.

    `once` answers the next `GET config` before `config` does.
    """

    def __init__(self) -> None:
        self.config: Any = CONFIG
        self.health: Any = LOG
        self.once: list[Exception] = []
        self.calls: list[tuple[str, str]] = []

    def answer(self, what: str, host: str) -> Any:
        self.calls.append((what, host))
        value = (
            self.once.pop(0) if what == "config" and self.once else getattr(self, what)
        )
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def rest() -> Generator[FakeRest]:
    fake = FakeRest()

    async def config(api: JungHomeGatewayApi) -> GatewayConfig:
        return fake.answer("config", api.host)  # type: ignore[no-any-return]

    async def health_status(api: JungHomeGatewayApi) -> list[GatewayHealthEntry]:
        return fake.answer("health", api.host)  # type: ignore[no-any-return]

    with (
        patch.object(JungHomeGatewayApi, "config", config),
        patch.object(JungHomeGatewayApi, "health_status", health_status),
    ):
        yield fake


async def gateway_entry(
    hass: HomeAssistant, tmp_path: Path, **data: Any
) -> MockConfigEntry:
    path = tmp_path / "JungHome.json"
    shutil.copy(SHARE_EXPORT_PATH, path)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={
            CONF_CDB_PATH: str(path),
            CONF_UNICAST: "0D00",
            CONF_SOURCE: "gateway",
            CONF_GATEWAY_SYNCED: SYNCED,
            CONF_GATEWAY_HOST: HOST,
            CONF_GATEWAY_TOKEN: "tok.en",
            CONF_GATEWAY_FINGERPRINT: "ab" * 32,
            CONF_GATEWAY_PIN_SOURCE: PIN_FROM_MESH,
            **data,
        },
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    return entry


@pytest.fixture
async def entry(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    rest: FakeRest,
    entity_registry_enabled_by_default: None,
) -> MockConfigEntry:
    return await gateway_entry(hass, tmp_path)


def uid(key: str) -> str:
    return f"node:{NODE_GATEWAY}-gateway_{key}"


def state(hass: HomeAssistant, domain: str, key: str) -> Any:
    found = hass.states.get(entity_id(hass, domain, uid(key)))
    assert found is not None
    return found


async def poll(hass: HomeAssistant, seconds: float = GATEWAY_STATUS_INTERVAL) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await settle(hass)


async def test_the_gateway_pages_as_diagnostics(
    hass: HomeAssistant, entry: MockConfigEntry, rest: FakeRest
) -> None:
    """Versions, serial, access requests and clients, the four indicators and the error log, on the gateway's
    node device, diagnostic and off by default; asked once each as soon as they are added."""
    assert state(hass, "sensor", "firmware").state == "2.1.3"
    assert state(hass, "sensor", "firmware_build").state == "2840"
    assert state(hass, "sensor", "serial").state == "1234567890"
    requests = state(hass, "sensor", "access_requests")
    assert requests.state == "1"
    assert requests.attributes["clients"] == ["Someone"]
    clients = state(hass, "sensor", "api_clients")
    assert clients.state == "2"
    assert clients.attributes["clients"] == list(CONFIG.api_clients)
    assert state(hass, "binary_sensor", "network_problem").state == STATE_ON
    assert state(hass, "binary_sensor", "mesh_problem").state == STATE_OFF
    assert state(hass, "binary_sensor", "cloud_problem").state == STATE_OFF
    assert state(hass, "binary_sensor", "cloud_connected").state == STATE_ON
    log = state(hass, "sensor", "error_log")
    assert log.state == "2"  # DEBUG hidden, as the app does by default
    assert [e["description"] for e in log.attributes["entries"]] == ["Cloud", "Mesh"]
    assert log.attributes["entries"][0] == {
        "level": "ERROR",
        "time": "2026-01-15 10:00:00",
        "description": "Cloud",
        "details": "no route",
    }
    assert rest.calls.count(("config", HOST)) == 1  # nine entities, one request
    assert rest.calls.count(("health", HOST)) == 1

    registry = er.async_get(hass)
    for domain, key in (("sensor", "firmware"), ("binary_sensor", "cloud_problem")):
        reg = registry.async_get(entity_id(hass, domain, uid(key)))
        assert reg is not None
        assert reg.entity_category is EntityCategory.DIAGNOSTIC
        assert reg.translation_key == f"gateway_{key}"

    # the status every 30 s, the error log every 5 minutes
    rest.config = GatewayConfig(
        **{**CONFIG.__dict__, "clients_asking": (), "mesh_error": True}
    )
    await poll(hass)
    assert state(hass, "sensor", "access_requests").state == "0"
    assert state(hass, "binary_sensor", "mesh_problem").state == STATE_ON
    assert rest.calls.count(("health", HOST)) == 1
    await poll(hass, GATEWAY_HEALTH_INTERVAL)
    assert rest.calls.count(("health", HOST)) == 2


async def test_a_missing_field_reads_empty(
    hass: HomeAssistant, entry: MockConfigEntry, rest: FakeRest
) -> None:
    rest.config = GatewayConfig(**{**CONFIG.__dict__, "release": "", "serial": ""})
    await poll(hass)
    assert state(hass, "sensor", "firmware").state == STATE_UNKNOWN
    assert state(hass, "sensor", "serial").state == STATE_UNKNOWN


async def test_entities_are_off_by_default_and_nothing_is_polled_then(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    rest: FakeRest,
) -> None:
    await gateway_entry(hass, tmp_path)
    registry = er.async_get(hass)
    for domain, key in (
        ("sensor", "firmware"),
        ("sensor", "error_log"),
        ("sensor", "last_sync"),
        ("binary_sensor", "network_problem"),
    ):
        reg = registry.async_get(entity_id(hass, domain, uid(key)))
        assert reg is not None
        assert reg.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    await poll(hass, GATEWAY_HEALTH_INTERVAL)
    assert rest.calls == []


async def test_no_gateway_entities_without_a_gateway_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert gateway_polls(hass, init_integration.runtime_data) is None
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, uid("firmware")) is None


async def test_failures_make_the_entities_unavailable_and_raise_the_repairs(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    rest: FakeRest,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected token raises its repair, and the polls stop sending it while the repair is open (logged once);
    another certificate raises the certificate repair; the next answer clears both. A busy or failing gateway is
    just unavailable."""
    firmware = entity_id(hass, "sensor", uid("firmware"))
    issues = ir.async_get(hass)

    rest.config = rest.health = GatewayAuthError("GET config: HTTP 401")
    await poll(hass)
    assert hass.states.get(firmware).state == STATE_UNAVAILABLE  # type: ignore[union-attr]
    assert issues.async_get_issue(DOMAIN, issue_id(entry, ISSUE_GATEWAY_TOKEN))
    asked = len(rest.calls)
    for _ in range(3):
        await poll(hass, GATEWAY_HEALTH_INTERVAL)
    assert (
        len(rest.calls) == asked
    )  # nothing is exchanged until the entry is reconfigured
    assert hass.states.get(firmware).state == STATE_UNAVAILABLE  # type: ignore[union-attr]
    assert caplog.text.count("access token: nothing is exchanged with it") == 1
    # reconfiguring clears the repair (`config_flow`): the polls ask again
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry, ISSUE_GATEWAY_TOKEN))
    rest.health = LOG

    rest.config = GatewayBusy("busy")
    await poll(hass)
    assert hass.states.get(firmware).state == STATE_UNAVAILABLE  # type: ignore[union-attr]

    rest.config = GatewayCertificateMismatch(HOST, "ab" * 32, "cd" * 32)
    with patch.object(
        type(entry.runtime_data), "async_follow_gateway", return_value=False
    ) as follow:  # the gateway node says it is still there
        await poll(hass)
    follow.assert_awaited_once()
    assert issues.async_get_issue(DOMAIN, issue_id(entry, ISSUE_GATEWAY_CERTIFICATE))

    rest.config = CONFIG
    await poll(hass)
    assert hass.states.get(firmware).state == "2.1.3"  # type: ignore[union-attr]
    for issue in (ISSUE_GATEWAY_TOKEN, ISSUE_GATEWAY_CERTIFICATE):
        assert issues.async_get_issue(DOMAIN, issue_id(entry, issue)) is None


async def test_a_silent_gateway_is_looked_for_over_the_mesh_once_per_outage(
    hass: HomeAssistant, entry: MockConfigEntry, rest: FakeRest
) -> None:
    """Like the app, a gateway that stops answering is looked for again over the mesh (its 0xC002 address): the
    status is then read where it is now. Here once per outage, not on every failed poll."""
    follows: list[str] = []
    moved = False

    async def follow(hub: Any) -> bool:
        follows.append(hub.entry.data[CONF_GATEWAY_HOST])
        if not moved:
            return False
        hass.config_entries.async_update_entry(
            hub.entry, data={**hub.entry.data, CONF_GATEWAY_HOST: "192.0.2.77"}
        )
        return True

    firmware = entity_id(hass, "sensor", uid("firmware"))
    with patch.object(type(entry.runtime_data), "async_follow_gateway", follow):
        rest.config = GatewayUnreachable("GET config: ClientConnectorError")
        await poll(hass)
        await poll(hass)
        assert follows == [HOST]  # not again while the outage lasts
        assert hass.states.get(firmware).state == STATE_UNAVAILABLE  # type: ignore[union-attr]

        rest.config = CONFIG
        await poll(hass)  # back: the next outage may look again
        moved = True
        rest.once = [GatewayUnreachable("GET config: ClientConnectorError")]
        rest.calls.clear()
        await poll(hass)
    assert follows == [HOST, HOST]
    assert rest.calls == [("config", HOST), ("config", "192.0.2.77")]
    assert hass.states.get(firmware).state == "2.1.3"  # type: ignore[union-attr]


async def test_an_unusable_gateway_is_not_asked(
    hass: HomeAssistant, entry: MockConfigEntry, rest: FakeRest
) -> None:
    """A pin the gateway node has not vouched for, or an entry that lost its credentials: nothing is sent."""
    polls = gateway_polls(hass, entry.runtime_data)
    assert polls is not None
    rest.calls.clear()
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_GATEWAY_PIN_SOURCE: PIN_FROM_USER}
    )
    with pytest.raises(UpdateFailed, match="vouched"):
        await polls.config._async_update_data()
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_GATEWAY_TOKEN: ""}
    )
    with pytest.raises(UpdateFailed, match="no usable gateway"):
        await polls.config._async_update_data()
    assert rest.calls == []


async def test_last_export_upload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """When Home Assistant last handed its export to the gateway, from the entry; an upload updates it at once."""
    last = entity_id(hass, "sensor", uid("last_sync"))
    assert hass.states.get(last).state == STATE_UNKNOWN  # type: ignore[union-attr]
    stamp = datetime(2026, 1, 16, 8, 0, tzinfo=UTC)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_GATEWAY_LAST_SYNC: stamp.isoformat()}
    )
    await settle(hass)
    shown = hass.states.get(last)
    assert shown is not None
    assert shown.state == "2026-01-16T08:00:00+00:00"
    assert shown.attributes["device_class"] == "timestamp"
