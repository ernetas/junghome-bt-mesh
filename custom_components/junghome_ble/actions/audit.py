"""The network actions: `sync_gateway`, `export_network`, `audit_network`, `approve_gateway_client`."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir

from custom_components.junghome_ble.const import (
    DOMAIN,
    ISSUE_GATEWAY_CERTIFICATE,
    ISSUE_GATEWAY_TOKEN,
    issue_id,
)
from custom_components.junghome_ble.gateway_api import (
    GatewayAuthError,
    GatewayBusy,
    GatewayCertificateMismatch,
    GatewayError,
    api_for_entry,
)
from custom_components.junghome_ble.jhmesh.audit import report
from custom_components.junghome_ble.jhmesh.devices import BATTERY_PIDS
from custom_components.junghome_ble.mesh_config import (
    MeshConfigurator,
    token_rejected_open,
)

from .common import (
    _ENTRY_FIELD,
    ATTR_CONFIG_ENTRY,
    ATTR_DEVICE,
    _configurator,
    _hub,
    _lock,
    _run,
    _validation,
)
from .resolve import _entry_for_hub_services, _resolve_node

if TYPE_CHECKING:
    # Home Assistant validates with probatio from 2026.10 and aliases `voluptuous` to it on import; the floor
    # release (hacs.json) has no probatio, so the schemas are built with voluptuous and typed as probatio.
    import probatio as vol
else:
    import voluptuous as vol

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse

_LOGGER = logging.getLogger(__name__)


SYNC_GATEWAY_SCHEMA = vol.Schema(_ENTRY_FIELD)
ATTR_FLAVOUR = "flavour"
EXPORT_FLAVOURS = ("share", "cdb")  # the app's share file, the mesh database
EXPORT_NETWORK_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_FLAVOUR, default="share"): vol.In(EXPORT_FLAVOURS),
        **_ENTRY_FIELD,
    }
)
ATTR_CLIENT = "client"
APPROVE_GATEWAY_CLIENT_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_CLIENT): vol.All(cv.string, vol.Length(min=1)), **_ENTRY_FIELD}
)
AUDIT_NETWORK_SCHEMA = vol.All(
    vol.Schema({vol.Optional(ATTR_DEVICE): cv.string, **_ENTRY_FIELD}),
    cv.has_at_most_one_key(ATTR_DEVICE, ATTR_CONFIG_ENTRY),
)


async def _sync_gateway(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Hand the export on disk to the gateway (the retry after a failed automatic upload)."""
    entry_id = _entry_for_hub_services(hass, call.data)
    await _run(hass, entry_id, lambda c: c.sync_gateway(), needs_link=False)
    return None


# ------------------------------------------------------------------ audit


async def _export_network(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Answer the export the entry uses, as the app's share file (default) or the CDB flavour.

    For a backup, or to hand the installation to the app ("import from file") with what Home Assistant changed.
    Admin only: the answer carries every key of the mesh (NetKey, AppKey, each node's device key).
    """
    entry_id = _entry_for_hub_services(hass, call.data)
    configurator = _configurator(hass, entry_id)
    async with _lock(hass, entry_id):
        return await configurator.async_export(call.data[ATTR_FLAVOUR])


async def _audit_network(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Compare the nodes' Configuration Servers with the export: every mains node, or the node `device` names.

    Gets only, so nothing is recorded or reloaded; the entry's lock keeps a configuration change from running in
    between, and the call waits for the link like the others. Battery nodes sleep and would answer nothing: the
    network-wide audit lists them as `skipped` (naming one as `device` asks it all the same). Answers
    `jhmesh.audit.report` — per node its node-wide states and findings — plus `skipped`.
    """
    if (device_id := call.data.get(ATTR_DEVICE)) is not None:
        entry_id, unicast = _resolve_node(hass, device_id)
    else:
        entry_id, unicast = _entry_for_hub_services(hass, call.data), None
    response: dict[str, Any] = {}

    async def operation(configurator: MeshConfigurator) -> bool:
        # the hub the lock handed us: a previous call's reload replaces it
        hub = configurator.hub
        if unicast is None:
            provisioned = sorted(
                (n for n in hub.cdb.nodes if n.pid is not None),
                key=lambda n: n.unicast,
            )
            nodes = [n for n in provisioned if n.pid not in BATTERY_PIDS]
            skipped = [n for n in provisioned if n.pid in BATTERY_PIDS]
        else:
            nodes, skipped = [n for n in hub.cdb.nodes if n.unicast == unicast], []
        try:
            results = await hub.async_audit(nodes)
        except ConnectionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="send_failed"
            ) from err
        response.update(report(results), skipped=[f"{n.unicast:04X}" for n in skipped])
        return False

    await _run(hass, entry_id, operation)
    return response


def _gateway_failure(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )


async def _approve_gateway_client(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """List the API clients waiting for approval at the gateway; approve the one `client` names.

    Admin only, and only explicit: nothing is approved without a name, and only a name the gateway lists as waiting
    right now (`GET config`, `api_client_name_asking`) — an approved client gets the gateway's whole API, the
    export with every key included. The gateway is asked under the export's rules: pinned to its certificate
    (`api_for_entry`), only once the gateway node vouched for that pin, not while it rejects Home Assistant's
    token; a rejected token and another certificate raise their repairs as the upload does. Resetting the
    permissions and the gateway's network settings stay with the app. Unverified on air.
    """
    entry_id = _entry_for_hub_services(hass, call.data)
    hub = _hub(hass, entry_id)
    api = api_for_entry(hass, hub.entry)
    if api is None:
        raise _validation("approve_no_gateway")
    if (distrust := await hub.async_gateway_distrust()) is not None:
        raise _gateway_failure(
            "approve_gateway_distrusted", host=api.host, error=distrust
        )
    if token_rejected_open(hass, hub.entry):
        raise _gateway_failure("approve_gateway_token_rejected", host=api.host)
    name: str | None = call.data.get(ATTR_CLIENT)
    try:
        waiting = list((await api.config()).clients_asking)
        if name is not None:
            if name not in waiting:
                raise _validation(
                    "approve_client_not_waiting",
                    client=name,
                    waiting=", ".join(waiting) or "-",
                )
            await api.approve_client(name)
    except GatewayAuthError as err:
        if hub.configurator is not None:
            hub.configurator.report_token_rejected(api)
        raise _gateway_failure("approve_gateway_token_rejected", host=api.host) from err
    except GatewayCertificateMismatch as err:
        hub.async_raise_certificate_issue()
        raise _gateway_failure(
            "gateway_certificate_changed",
            host=api.host,
            expected=err.expected,
            observed=err.observed,
        ) from err
    except GatewayBusy as err:
        raise _gateway_failure("approve_gateway_busy", host=api.host) from err
    except GatewayError as err:
        raise _gateway_failure(
            "approve_gateway_failed", host=api.host, error=str(err)
        ) from err
    for issue in (ISSUE_GATEWAY_TOKEN, ISSUE_GATEWAY_CERTIFICATE):
        ir.async_delete_issue(hass, DOMAIN, issue_id(hub.entry, issue))
    if name is not None:
        _LOGGER.info("Approved the API client %r at the gateway %s", name, api.host)
        waiting.remove(name)
    response: dict[str, Any] = {"approved": name, "waiting": waiting}
    return response if call.return_response else None
