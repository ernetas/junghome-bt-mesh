"""The JUNG HOME Gateway's own status over its REST API: what the app's gateway pages show.

The app polls `GET config` every 5 s (10 s after an error) while it runs (`PollGatewayConfig`), and reads `GET
healthstatus` while its error-log page is open. Here two coordinators do the same for an entry set up from a
gateway, on the gateway node's device, each only while one of its entities is enabled (every one is diagnostic and
off by default): the status every `GATEWAY_STATUS_INTERVAL` (30 s — Home Assistant polls all day, the app only while
it is open), the error log every `GATEWAY_HEALTH_INTERVAL` (5 min). Firmware version and build, serial number, the
access requests waiting for approval in the app (the app's permissions indicator counts them), the approved API
clients, the Network / Bluetooth Mesh / Cloud indicators, the non-debug entries of the error log. The time of the
last upload of Home Assistant's export (the app's `gateway_last_sync`) is in the entry's
`configurator.store.GatewaySync` record.

The gateway is asked under the same rules as the export's fetch and upload (`configurator/store.py`): only with a pin
the gateway node vouched for (`JungHomeHub.gateway_vouched`; the vouching itself runs on every link), a rejected token
raises the `gateway_token_rejected` repair and starts the reauth flow (the polls stop asking while the repair is
open), another certificate the `gateway_certificate_changed` one. Like the app, a gateway that stops answering is
looked for again over the mesh (`JungHomeHub.async_follow_gateway`: its `0xC002` address and `0xC003` certificate) —
once per outage here, not on every failed poll, and never its token (`0xC001` is the app's). An answer clears both
repairs.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import EntityCategory
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    DOMAIN,
    GATEWAY_HEALTH_INTERVAL,
    GATEWAY_STATUS_INTERVAL,
    ISSUE_GATEWAY_CERTIFICATE,
    ISSUE_GATEWAY_TOKEN,
    issue_id,
)
from .entity import node_device_info
from .gateway_api import (
    GatewayAuthError,
    GatewayCertificateMismatch,
    GatewayConfig,
    GatewayError,
    GatewayHealthEntry,
    GatewayUnreachable,
    JungHomeGatewayApi,
    api_for_entry,
)
from .jhmesh.devices import GATEWAY_PID
from .mesh_config import token_rejected_open

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import JungHomeHub
    from .jhmesh.cdb import Node

_LOGGER = logging.getLogger(__name__)


class GatewayPoll[T](DataUpdateCoordinator[T]):
    """One gateway REST read, polled while an entity listens (`DataUpdateCoordinator` polls only then)."""

    def __init__(
        self,
        hass: HomeAssistant,
        hub: JungHomeHub,
        name: str,
        interval: float,
        call: Callable[[JungHomeGatewayApi], Awaitable[T]],
    ) -> None:
        """Bind to the hub's entry; `call` is the request."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=hub.entry,
            name=f"{DOMAIN} gateway {name}",
            update_interval=timedelta(seconds=interval),
        )
        self.hub = hub
        self._call = call
        self._may_follow = (
            True  # the gateway is looked for over the mesh once per outage
        )

    async def _async_update_data(self) -> T:
        """Ask the gateway, following it once to a new address; every failure becomes `UpdateFailed`."""
        hub = self.hub
        api = api_for_entry(self.hass, hub.entry)
        if api is None:
            raise UpdateFailed("the entry has no usable gateway")
        if not hub.gateway_vouched:
            raise UpdateFailed(
                "the gateway node has not vouched for the pinned certificate"
            )
        if token_rejected_open(self.hass, hub.entry):
            # not every interval with a token the gateway rejected: the reauth flow (or a reconfigure, or an export
            # fetch that is answered) clears the issue, and the polls ask again
            raise UpdateFailed(
                f"the gateway {api.host} rejects the token; grant access again"
            )
        try:
            try:
                result = await self._call(api)
            except (GatewayUnreachable, GatewayCertificateMismatch):
                follow, self._may_follow = self._may_follow, False
                if not follow or not await hub.async_follow_gateway():
                    raise
                followed = api_for_entry(self.hass, hub.entry)
                assert (
                    followed is not None
                )  # the entry still names the gateway it followed
                api = followed
                result = await self._call(api)
        except GatewayAuthError as err:
            if hub.configurator is not None:
                hub.configurator.report_token_rejected(api)
            raise UpdateFailed(f"the gateway {api.host} rejects the token") from err
        except GatewayCertificateMismatch as err:
            hub.async_raise_certificate_issue()
            raise UpdateFailed(
                f"the gateway {api.host} presents another certificate than the pinned one"
            ) from err
        except GatewayError as err:
            raise UpdateFailed(f"the gateway {api.host}: {err}") from err
        self._may_follow = True
        for issue in (ISSUE_GATEWAY_TOKEN, ISSUE_GATEWAY_CERTIFICATE):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id(hub.entry, issue))
        return result


def _config(api: JungHomeGatewayApi) -> Awaitable[GatewayConfig]:
    return api.config()


def _health(api: JungHomeGatewayApi) -> Awaitable[list[GatewayHealthEntry]]:
    return api.health_status()


@dataclass
class GatewayPolls:
    """The gateway node of an entry set up from a gateway, and the two polls of its REST API."""

    node: Node
    config: GatewayPoll[GatewayConfig]
    health: GatewayPoll[list[GatewayHealthEntry]]


def gateway_polls(hass: HomeAssistant, hub: JungHomeHub) -> GatewayPolls | None:
    """Return the hub's gateway polls, made on first use; None without a gateway (the entry's or a node)."""
    if (polls := hub.gateway_polls) is not None:
        assert isinstance(polls, GatewayPolls)  # only this function sets it
        return polls
    node = next((n for n in hub.cdb.nodes if n.pid == GATEWAY_PID), None)
    if node is None or api_for_entry(hass, hub.entry) is None:
        return None
    polls = hub.gateway_polls = GatewayPolls(
        node,
        GatewayPoll(hass, hub, "status", GATEWAY_STATUS_INTERVAL, _config),
        GatewayPoll(hass, hub, "error log", GATEWAY_HEALTH_INTERVAL, _health),
    )
    return polls


class GatewayPollEntity[T](CoordinatorEntity[GatewayPoll[T]]):
    """A diagnostic of the gateway's REST API on the gateway node's device; off by default, like every diagnostic."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, poll: GatewayPoll[T], node: Node, key: str) -> None:
        """Bind to `poll`; `key` names the entity (unique id suffix and translation key `gateway_<key>`)."""
        super().__init__(poll)
        self._attr_unique_id = f"node:{node.uuid.lower()}-gateway_{key}"
        self._attr_translation_key = f"gateway_{key}"
        self._attr_device_info = node_device_info(poll.hub, node)

    async def async_added_to_hass(self) -> None:
        """Start listening, and ask right away rather than one interval later when nothing was read yet."""
        await super().async_added_to_hass()
        if self.coordinator.data is None:
            self.coordinator.hub.entry.async_create_background_task(
                self.hass,
                self.coordinator.async_request_refresh(),
                f"{DOMAIN} first gateway poll",
            )

    @property
    def data(self) -> T | None:
        """The last answer; None until the first one."""
        return self.coordinator.data


def error_entries(entries: list[GatewayHealthEntry]) -> list[GatewayHealthEntry]:
    """Return the entries of the error log the app shows by default: all but DEBUG (`display_gateway_debug_log` off)."""
    return [e for e in entries if e.level.upper() != "DEBUG"]


def as_attributes(entry: GatewayHealthEntry) -> dict[str, Any]:
    """Return one error-log entry as a state attribute."""
    return {
        "level": entry.level,
        "time": entry.time,
        "description": entry.description,
        "details": entry.details,
    }
