"""The gateway's own status over its REST API (`gateway_status.py`): the app's gateway pages as diagnostics.

The gateway client is replaced by a fake answering `GET config` / `GET healthstatus` from what each test sets;
the REST calls themselves have their tests in test_gateway_api.py.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
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
    CONF_GATEWAY_PASSWORD,
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
from custom_components.junghome_ble.mesh_config import export_digest, gateway_sync

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

    `once` answers the next `GET config` before `config` does; `approve` is what an approval raises (None: it
    lands, the name is kept in `approved`).
    """

    def __init__(self) -> None:
        self.config: Any = CONFIG
        self.health: Any = LOG
        self.approve: Exception | None = None
        self.approved: list[str] = []
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

    async def approve_client(api: JungHomeGatewayApi, name: str) -> None:
        fake.calls.append(("approve", api.host))
        if fake.approve is not None:
            raise fake.approve
        fake.approved.append(name)

    with (
        patch.object(JungHomeGatewayApi, "config", config),
        patch.object(JungHomeGatewayApi, "health_status", health_status),
        patch.object(JungHomeGatewayApi, "approve_client", approve_client),
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


def reauth_flows(hass: HomeAssistant, entry: MockConfigEntry) -> list[Any]:
    """The entry's reauthentication flows in progress."""
    return list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


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
    """A rejected token raises its repair and starts one reauth flow, and the polls stop sending it while the
    repair is open (logged once); another certificate raises the certificate repair; the next answer clears both.
    A busy or failing gateway is just unavailable."""
    firmware = entity_id(hass, "sensor", uid("firmware"))
    issues = ir.async_get(hass)

    rest.config = rest.health = GatewayAuthError("GET config: HTTP 401")
    await poll(hass)
    assert hass.states.get(firmware).state == STATE_UNAVAILABLE  # type: ignore[union-attr]
    assert issues.async_get_issue(DOMAIN, issue_id(entry, ISSUE_GATEWAY_TOKEN))
    assert len(reauth_flows(hass, entry)) == 1
    asked = len(rest.calls)
    for _ in range(3):
        await poll(hass, GATEWAY_HEALTH_INTERVAL)
    assert (
        len(rest.calls) == asked
    )  # nothing is exchanged until access is granted again
    assert hass.states.get(firmware).state == STATE_UNAVAILABLE  # type: ignore[union-attr]
    assert caplog.text.count("access token: the export is not handed to it") == 1
    assert len(reauth_flows(hass, entry)) == 1
    # the reauth flow clears the repair (`config_flow`): the polls ask again
    hass.config_entries.flow.async_abort(reauth_flows(hass, entry)[0]["flow_id"])
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


async def test_a_rejection_from_before_a_restart_is_reported_again(
    hass: HomeAssistant, entry: MockConfigEntry, rest: FakeRest
) -> None:
    """The token repair is not persistent: one raised before a restart comes back from the registry inactive. The
    polls then ask again, and a rejection raises it anew and starts the reauth flow, instead of the polls staying
    silent with nothing on screen."""
    issues = ir.async_get(hass)
    token_issue = issue_id(entry, ISSUE_GATEWAY_TOKEN)
    ir.async_create_issue(
        hass,
        DOMAIN,
        token_issue,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_GATEWAY_TOKEN,
        translation_placeholders={"host": HOST, "title": entry.title},
    )
    restored = issues.async_get_issue(DOMAIN, token_issue)
    assert restored is not None
    issues.issues[(DOMAIN, token_issue)] = replace(restored, active=False)

    rest.config = rest.health = GatewayAuthError("GET config: HTTP 401")
    await poll(hass)
    asked = issues.async_get_issue(DOMAIN, token_issue)
    assert asked is not None
    assert asked.active
    assert ("config", HOST) in rest.calls
    assert len(reauth_flows(hass, entry)) == 1


async def test_a_reload_asks_again_while_the_token_repair_is_open(
    hass: HomeAssistant, entry: MockConfigEntry, rest: FakeRest
) -> None:
    """Home Assistant aborts an entry's reauth flows when it reloads it — and most actions reload right after the
    change that met the rejected token. While the token repair is open the set-up entry asks again."""
    rest.config = rest.health = GatewayAuthError("GET config: HTTP 401")
    await poll(hass)
    (first,) = reauth_flows(hass, entry)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await wait_for_link(hass, entry)
    await settle(hass)
    (again,) = reauth_flows(hass, entry)
    assert again["flow_id"] != first["flow_id"]

    hass.config_entries.flow.async_abort(again["flow_id"])
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry, ISSUE_GATEWAY_TOKEN))
    rest.config, rest.health = CONFIG, LOG
    assert await hass.config_entries.async_reload(entry.entry_id)
    await wait_for_link(hass, entry)
    await settle(hass)
    assert reauth_flows(hass, entry) == []  # no repair, no question


async def test_the_reauth_flow_restores_the_gateway_without_a_reload(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    rest: FakeRest,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The gateway rejects the token: the reauth flow the poll started takes the gateway password, stores the new
    token and clears the repair. The hub is not set up again and no mesh entity goes unavailable at any point; the
    next poll asks with the new token. The pin comes from the gateway node over the mesh."""
    caplog.set_level(logging.DEBUG)
    hub = entry.runtime_data
    firmware = entity_id(hass, "sensor", uid("firmware"))
    tokens: list[str | None] = []

    async def config(api: JungHomeGatewayApi) -> GatewayConfig:
        tokens.append(api.token)
        return rest.answer("config", api.host)  # type: ignore[no-any-return]

    async def by_password(api: JungHomeGatewayApi, password: str) -> str:
        assert password == "netkey-pw"
        api.token = "tok.new"
        return "tok.new"

    def unique_id(hass: HomeAssistant, eid: str) -> str:
        reg = er.async_get(hass).async_get(eid)
        assert reg is not None
        return reg.unique_id

    def unavailable() -> set[str]:
        return {
            s.entity_id
            for s in hass.states.async_all()
            if s.state == STATE_UNAVAILABLE
            and (reg := er.async_get(hass).async_get(s.entity_id)) is not None
            and reg.platform == DOMAIN
        }

    mesh_down_before = unavailable() - {firmware}
    rest.config = rest.health = GatewayAuthError("GET config: HTTP 401")
    with patch.object(JungHomeGatewayApi, "config", config):
        await poll(hass)
    assert hass.states.get(firmware).state == STATE_UNAVAILABLE  # type: ignore[union-attr]
    gateway_down = unavailable() - mesh_down_before
    assert gateway_down
    assert all("-gateway_" in unique_id(hass, e) for e in gateway_down)
    (flow,) = reauth_flows(hass, entry)

    rest.config, rest.health = CONFIG, LOG
    with (
        patch.object(JungHomeGatewayApi, "version", return_value=None),
        patch.object(JungHomeGatewayApi, "register_by_password", by_password),
        patch(
            "custom_components.junghome_ble.config_flow.async_read_mesh_fingerprint",
            return_value="ab" * 32,
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_GATEWAY_PASSWORD: "netkey-pw"}
        )
    await settle(hass)
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_GATEWAY_TOKEN] == "tok.new"
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, issue_id(entry, ISSUE_GATEWAY_TOKEN))
        is None
    )
    assert entry.runtime_data is hub  # not reloaded
    assert unavailable() - mesh_down_before <= gateway_down

    with patch.object(JungHomeGatewayApi, "config", config):
        await poll(hass)
    assert tokens[-1] == "tok.new"
    assert hass.states.get(firmware).state == "2.1.3"  # type: ignore[union-attr]
    assert unavailable() == mesh_down_before
    assert "netkey-pw" not in caplog.text
    assert "tok.new" not in caplog.text


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
    """When Home Assistant last handed its export to the gateway, from the entry's sync record; an upload updates it
    at once, and nothing of it is written into the entry any more (review-4 H I-10)."""
    last = entity_id(hass, "sensor", uid("last_sync"))
    assert hass.states.get(last).state == STATE_UNKNOWN  # type: ignore[union-attr]
    data = dict(entry.data)
    record = gateway_sync(hass, entry.entry_id)
    record.record("cd" * 32)  # an adopted export: no upload, no time
    await settle(hass)
    assert hass.states.get(last).state == STATE_UNKNOWN  # type: ignore[union-attr]
    record.record("ef" * 32, uploaded=True)
    await settle(hass)
    shown = hass.states.get(last)
    assert shown is not None
    assert record.last_sync is not None
    uploaded = dt_util.parse_datetime(record.last_sync)
    assert uploaded is not None
    # the state is shown to the second
    assert dt_util.parse_datetime(shown.state) == uploaded.replace(microsecond=0)
    assert shown.attributes["device_class"] == "timestamp"
    assert entry.data == data


async def test_the_sync_record_is_taken_over_from_the_entry_once(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    rest: FakeRest,
    entity_registry_enabled_by_default: None,
    hass_storage: dict[str, Any],
) -> None:
    """An entry of 1.0.0 kept the synced digest and the last upload's time in `entry.data`: the first setup takes
    them into the entry's own store (left in `entry.data` for a downgrade), later syncs go there only, and a
    reload reads the store, not the stale `entry.data`. Removing the entry removes the store."""
    stamp = (dt_util.utcnow() - timedelta(hours=1)).replace(microsecond=0)
    entry = await gateway_entry(
        hass, tmp_path, **{CONF_GATEWAY_LAST_SYNC: stamp.isoformat()}
    )
    key = f"{DOMAIN}.{entry.entry_id}.gateway_sync"
    assert hass_storage[key]["data"] == {
        "synced": SYNCED,
        "last_sync": stamp.isoformat(),
    }
    shown = state(hass, "sensor", "last_sync")
    assert dt_util.parse_datetime(shown.state) == stamp
    gateway_sync(hass, entry.entry_id).record("ab" * 32, uploaded=True)
    await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    assert entry.data[CONF_GATEWAY_SYNCED] == SYNCED  # left as it was
    assert gateway_sync(hass, entry.entry_id).synced == "ab" * 32
    assert dt_util.parse_datetime(state(hass, "sensor", "last_sync").state) != stamp
    await hass.config_entries.async_remove(entry.entry_id)
    await settle(hass)
    assert key not in hass_storage


# --------------------------------------------------------------------------- approve_gateway_client (review-4 F4-17)


async def approve(hass: HomeAssistant, **data: Any) -> Any:
    return await hass.services.async_call(
        DOMAIN, "approve_gateway_client", data, blocking=True, return_response=True
    )


def refusal(caught: pytest.ExceptionInfo[HomeAssistantError]) -> str | None:
    return caught.value.translation_key


async def test_approve_lists_the_waiting_clients_and_approves_only_one_named(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    rest: FakeRest,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without a name nothing is approved; a name the gateway does not list as waiting is refused; the one named
    is approved, and the answer lists who is still waiting. The token never reaches the log."""
    caplog.set_level(logging.DEBUG)
    rest.config = replace(CONFIG, clients_asking=("ioBroker", "Someone"))
    assert await approve(hass) == {"approved": None, "waiting": ["ioBroker", "Someone"]}
    with pytest.raises(ServiceValidationError) as caught:
        await approve(hass, client="iobroker")  # exactly as listed
    assert refusal(caught) == "approve_client_not_waiting"
    assert caught.value.translation_placeholders == {
        "client": "iobroker",
        "waiting": "ioBroker, Someone",
    }
    assert rest.approved == []

    assert await approve(hass, client="ioBroker") == {
        "approved": "ioBroker",
        "waiting": ["Someone"],
    }
    assert rest.approved == ["ioBroker"]
    assert "Approved the API client 'ioBroker' at the gateway junghome.local" in (
        caplog.text
    )
    assert "tok.en" not in caplog.text
    # without asking for the response, the action answers nothing (and still approves)
    rest.config = replace(CONFIG, clients_asking=("Other",))
    await hass.services.async_call(
        DOMAIN, "approve_gateway_client", {"client": "Other"}, blocking=True
    )
    assert rest.approved == ["ioBroker", "Other"]
    rest.config = replace(CONFIG, clients_asking=())
    with pytest.raises(ServiceValidationError) as caught:
        await approve(hass, client="Other")
    assert caught.value.translation_placeholders["waiting"] == "-"  # type: ignore[index]


async def test_approve_failures_raise_the_repairs_and_send_nothing_unsafe(
    hass: HomeAssistant, entry: MockConfigEntry, rest: FakeRest
) -> None:
    """A rejected token raises its repair (and the reauth), after which the gateway is not asked; another
    certificate raises the certificate repair; busy and failing gateways say so; an answer clears both repairs."""
    issues = ir.async_get(hass)
    rest.approve = GatewayAuthError("POST config: HTTP 401")
    with pytest.raises(HomeAssistantError) as caught:
        await approve(hass, client="Someone")
    assert refusal(caught) == "approve_gateway_token_rejected"
    assert issues.async_get_issue(DOMAIN, issue_id(entry, ISSUE_GATEWAY_TOKEN))
    assert len(reauth_flows(hass, entry)) == 1
    asked = len(rest.calls)
    with pytest.raises(HomeAssistantError) as caught:
        await approve(hass, client="Someone")
    assert refusal(caught) == "approve_gateway_token_rejected"
    assert len(rest.calls) == asked  # not asked with a rejected token
    hass.config_entries.flow.async_abort(reauth_flows(hass, entry)[0]["flow_id"])
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry, ISSUE_GATEWAY_TOKEN))

    rest.approve = None
    rest.config = GatewayCertificateMismatch(HOST, "ab" * 32, "cd" * 32)
    with pytest.raises(HomeAssistantError) as caught:
        await approve(hass, client="Someone")
    assert refusal(caught) == "gateway_certificate_changed"
    assert caught.value.translation_placeholders == {
        "host": HOST,
        "expected": "ab" * 32,
        "observed": "cd" * 32,
    }
    assert issues.async_get_issue(DOMAIN, issue_id(entry, ISSUE_GATEWAY_CERTIFICATE))

    for error, key in (
        (GatewayBusy("busy"), "approve_gateway_busy"),
        (GatewayUnreachable("GET config: TimeoutError"), "approve_gateway_failed"),
    ):
        rest.config = error
        with pytest.raises(HomeAssistantError) as caught:
            await approve(hass, client="Someone")
        assert refusal(caught) == key
    assert rest.approved == []

    rest.config = CONFIG
    await approve(hass, client="Someone")
    assert rest.approved == ["Someone"]
    assert (
        issues.async_get_issue(DOMAIN, issue_id(entry, ISSUE_GATEWAY_CERTIFICATE))
        is None
    )


async def test_approve_needs_a_gateway_the_mesh_vouched_for(
    hass: HomeAssistant, entry: MockConfigEntry, rest: FakeRest, tmp_path: Path
) -> None:
    """A pin the gateway node has not vouched for is not used, and an entry without a gateway has none to ask."""
    rest.calls.clear()
    with (
        patch.object(
            type(entry.runtime_data),
            "async_gateway_distrust",
            return_value="the gateway node has not confirmed the pinned certificate",
        ),
        pytest.raises(HomeAssistantError) as caught,
    ):
        await approve(hass, client="Someone")
    assert refusal(caught) == "approve_gateway_distrusted"
    assert rest.calls == []
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_SOURCE: "upload"}
    )
    await settle(hass)  # the change reloads the entry
    with pytest.raises(ServiceValidationError) as caught:
        await approve(hass, client="Someone")
    assert refusal(caught) == "approve_no_gateway"
    assert rest.calls == []
