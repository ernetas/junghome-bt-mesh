"""The IV Update actions: Home Assistant starts an IV Update of its mesh, or aborts one (unverified on air).

Every sender of a mesh stops at the end of the 24-bit sequence space of the current IV index until an IV Update moves
the mesh to the next one (Mesh Protocol 1.1 §3.11.5). A node at risk of running out is expected to start it; whether
JUNG devices do is unverified, so an administrator can have Home Assistant start it, as a GATT Proxy Client: its
proxy processes a Secure Network beacon from it as any other (§6.7) and carries the update to the mesh
(`ProxyClient.start_iv_update`). It cannot be undone once the mesh took it — the IV index only goes up — so the call
needs `confirm: true`, and is refused unless some sender is past three quarters of the space (the
`sequence_space_low` repair's condition) or `force: true` is given; refused too without a link, during a key refresh,
and within 96 hours of the last change of the IV state, each with its own reason (with the mesh's last IV change seen
in a beacon, `LocalState.mesh_iv_changed_at`: the proxy refuses within 96 hours of its own). The answer gives the new
IV index and when Normal Operation is due.

`abort_iv_update` gives up one the mesh has not taken yet (`ProxyClient.abort_iv_update`): back at the old index,
which nothing was sent under but beacons; also `confirm: true`. Home Assistant gives one up by itself 144 hours after
the start (review-5 P5-3, the `iv_update_not_taken` repair).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.jhmesh.state import IVUpdateRefused
from custom_components.junghome_ble.seq_store import iv_update_summary, local_time

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

    from custom_components.junghome_ble.jhmesh.state import LocalState
    from custom_components.junghome_ble.mesh_config import MeshConfigurator


START_IV_UPDATE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIRM): cv.boolean,
        vol.Optional(ATTR_FORCE, default=False): cv.boolean,
        **_ENTRY_FIELD,
    }
)
ABORT_IV_UPDATE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIRM): cv.boolean,
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
ABORT_REFUSALS: Final = {
    "not_started": "abort_iv_update_not_started",
    "taken": "abort_iv_update_taken",
    "iv_unknown": "abort_iv_update_iv_unknown",
}
# what a refusal names when no change of the mesh's IV state was seen in a beacon (any language)
NOT_SEEN: Final = "—"


def _refused(
    err: IVUpdateRefused, state: LocalState, keys: dict[str, str]
) -> ServiceValidationError:
    """Return the action's error for a refusal of the library: its own words, the indexes and times filled in."""
    return _validation(
        keys[err.reason],
        iv_index=str(state.iv_index),
        not_before=local_time(err.not_before),
        mesh_changed=local_time(state.mesh_iv_changed_at, NOT_SEEN),
    )


async def _start_iv_update(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Start an IV Update of the mesh (admin only, `confirm` required; unverified on air)."""
    if not call.data[ATTR_CONFIRM]:
        raise _validation("start_iv_update_needs_confirm")
    entry_id = _entry_for_hub_services(hass, call.data)
    response = await async_start_iv_update(hass, entry_id, force=call.data[ATTR_FORCE])
    return response if call.return_response else None


async def async_start_iv_update(
    hass: HomeAssistant, entry_id: str, *, force: bool = False
) -> dict[str, Any]:
    """Start an IV Update of the entry's mesh with the action's guards; answer the new IV index and its timing.

    The action's work after its `confirm`, and the confirmed `sequence_space_low` repair's
    (`repairs.StartIVUpdateFlow`). Refused with the action's errors: not needed unless `force`, no link, a key
    refresh, an update in progress or too recent, the highest index. Unverified on air.
    """
    if not force and _hub(hass, entry_id).issues.sequence_space_low() is None:
        raise _validation("start_iv_update_not_needed")
    response: dict[str, Any] = {}

    async def operation(configurator: MeshConfigurator) -> bool:
        hub = configurator.hub
        state = hub.proxy.state
        try:
            new = await hub.proxy.start_iv_update()
        except IVUpdateRefused as err:
            raise _refused(err, state, REFUSALS) from err
        except ConnectionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="service_not_connected"
            ) from err
        except OSError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="start_iv_update_not_stored"
            ) from err
        # the mesh has not taken it yet: the sequence space is still judged at the old index; a repair of the last
        # update given up goes
        hub.issues.check_iv_update()
        response.update(
            iv_index=new,
            transmit_iv_index=state.tx_iv_index,
            **iv_update_summary(state),
        )
        return False  # the export did not change

    await _run(hass, entry_id, operation)
    return response


async def _abort_iv_update(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Abort the IV Update Home Assistant started that the mesh has not taken (admin only, `confirm` required).

    Unverified on air.
    """
    if not call.data[ATTR_CONFIRM]:
        raise _validation("abort_iv_update_needs_confirm")
    entry_id = _entry_for_hub_services(hass, call.data)
    response: dict[str, Any] = {}

    async def operation(configurator: MeshConfigurator) -> bool:
        hub = configurator.hub
        state = hub.proxy.state
        try:
            back = hub.proxy.abort_iv_update()
        except IVUpdateRefused as err:
            raise _refused(err, state, ABORT_REFUSALS) from err
        except ConnectionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="service_not_connected"
            ) from err
        # back at the mesh's index: the sequence space's repair, if a sender still runs low
        hub.issues.check_iv_update()
        response.update(
            iv_index=back,
            transmit_iv_index=state.tx_iv_index,
            **iv_update_summary(state),
        )
        return False  # the export did not change

    await _run(hass, entry_id, operation)
    return response if call.return_response else None
