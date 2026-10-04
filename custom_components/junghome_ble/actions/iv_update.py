"""The `start_iv_update` action: Home Assistant starts an IV Update of its mesh (review-4 P I-11; unverified on air).

Every sender of a mesh stops at the end of the 24-bit sequence space of the current IV index until an IV Update moves
the mesh to the next one (Mesh Protocol 1.1 §3.11.5). A node at risk of running out is expected to start it; whether
JUNG devices do is unverified, so an administrator can have Home Assistant start it, as a GATT Proxy Client: its
proxy processes a Secure Network beacon from it as any other (§6.7) and carries the update to the mesh
(`ProxyClient.start_iv_update`). It cannot be undone — the IV index only goes up — so the call needs `confirm: true`,
and is refused unless some sender is past three quarters of the space (the `sequence_space_low` repair's condition)
or `force: true` is given; refused too without a link, during a key refresh, and within 96 hours of the last change
of the IV state, each with its own reason. The answer gives the new IV index and when Normal Operation is due.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.jhmesh.state import IVUpdateRefused
from custom_components.junghome_ble.seq_store import iv_update_summary

from .common import (
    _ENTRY_FIELD,
    ATTR_CONFIRM,
    ATTR_FORCE,
    _hub,
    _run,
    _validation,
)
from .resolve import _entry_for_hub_services

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse

    from custom_components.junghome_ble.mesh_config import MeshConfigurator


START_IV_UPDATE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIRM): cv.boolean,
        vol.Optional(ATTR_FORCE, default=False): cv.boolean,
        **_ENTRY_FIELD,
    }
)
# `IVUpdateRefused.reason` → the error the action shows
REFUSALS: Final = {
    "in_progress": "start_iv_update_in_progress",
    "key_refresh": "start_iv_update_key_refresh",
    "iv_unknown": "start_iv_update_iv_unknown",
    "too_early": "start_iv_update_too_early",
    "iv_max": "start_iv_update_iv_max",
}


async def _start_iv_update(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Start an IV Update of the mesh (admin only, `confirm` required; unverified on air)."""
    if not call.data[ATTR_CONFIRM]:
        raise _validation("start_iv_update_needs_confirm")
    entry_id = _entry_for_hub_services(hass, call.data)
    if (
        not call.data[ATTR_FORCE]
        and _hub(hass, entry_id).issues.sequence_space_low() is None
    ):
        raise _validation("start_iv_update_not_needed")
    response: dict[str, Any] = {}

    async def operation(configurator: MeshConfigurator) -> bool:
        hub = configurator.hub
        state = hub.proxy.state
        try:
            new = await hub.proxy.start_iv_update()
        except IVUpdateRefused as err:
            raise _validation(
                REFUSALS[err.reason],
                iv_index=str(state.iv_index),
                not_before=""
                if err.not_before is None
                else dt_util.as_local(dt_util.utc_from_timestamp(err.not_before))
                .replace(microsecond=0)
                .isoformat(sep=" "),
            ) from err
        except ConnectionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="service_not_connected"
            ) from err
        except OSError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="start_iv_update_not_stored"
            ) from err
        hub.issues.check_sequence_space()  # the new index's space is untouched: the repair clears
        response.update(
            iv_index=new,
            transmit_iv_index=state.tx_iv_index,
            **iv_update_summary(state),
        )
        return False  # the export did not change

    await _run(hass, entry_id, operation)
    return response if call.return_response else None
