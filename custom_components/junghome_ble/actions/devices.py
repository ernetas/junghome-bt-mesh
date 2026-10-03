"""The device actions: `find_new_devices`, `add_device`, `reset_pending_device`, `remove_device`, `locate_node`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .. import onboard
from ..const import (
    DEFAULT_ALLOW_PROVISIONING,
    DOMAIN,
    LOCATE_MIN_SECONDS,
    LOCATE_SECONDS,
    OPTION_ALLOW_PROVISIONING,
)
from ..jhmesh import config_messages as C
from .common import (
    _DRY_RUN_FIELD,
    _ENTRY_FIELD,
    ATTR_CONFIRM,
    ATTR_DEVICE,
    ATTR_DRY_RUN,
    ATTR_FORCE,
    ATTR_NAME,
    _answer,
    _execute,
    _hub,
    _run,
    _validation,
)
from .resolve import _entry_for_hub_services, _resolve_node

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse

    from ..identity import VaultKeeper
    from ..mesh_config import MeshConfigurator


ATTR_ADDRESS = "address"
REMOVE_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE): cv.string,
        vol.Optional(ATTR_FORCE, default=False): cv.boolean,
        vol.Optional(ATTR_CONFIRM, default=False): cv.boolean,
        **_DRY_RUN_FIELD,
    }
)
FIND_NEW_DEVICES_SCHEMA = vol.Schema({})
ATTR_STATIC_OOB = "static_oob"


def _static_oob(value: Any) -> bytes:
    """Return the Static OOB value hexadecimal text gives (16 or 32 bytes; spaces, `-` and `:` ignored).

    The error never repeats the value: it authenticates the device's provisioning (review-4 P4-8).
    """
    text = "".join(c for c in cv.string(value) if c not in " -:")
    try:
        raw = bytes.fromhex(text)
    except ValueError as err:
        raise vol.Invalid("not hexadecimal") from err
    if len(raw) not in (16, 32):
        raise vol.Invalid("not 16 or 32 bytes (32 or 64 hexadecimal digits)")
    return raw


ADD_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ADDRESS): cv.string,
        vol.Required(ATTR_NAME): cv.string,
        vol.Optional(ATTR_STATIC_OOB): _static_oob,
        **_ENTRY_FIELD,
    }
)
ATTR_UUID = "uuid"
ATTR_UNICAST = "unicast"
ATTR_DURATION = "duration"
LOCATE_NODE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE): cv.string,
        vol.Optional(ATTR_DURATION, default=LOCATE_SECONDS): vol.All(
            vol.Coerce(int), vol.Range(min=LOCATE_MIN_SECONDS, max=LOCATE_SECONDS)
        ),
    }
)


def _unicast(value: Any) -> int:
    """Return the primary unicast address hexadecimal text names (`0D20`, `0x0D20`)."""
    try:
        address = int(cv.string(value), 16)
    except ValueError as err:
        raise vol.Invalid("not a hexadecimal address") from err
    if not 0x0001 <= address <= 0x7FFF:
        raise vol.Invalid("not a unicast address (0001-7FFF)")
    return address


RESET_PENDING_DEVICE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(ATTR_UUID): cv.string,
            vol.Optional(ATTR_UNICAST): _unicast,
            vol.Optional(ATTR_FORCE, default=False): cv.boolean,
            **_ENTRY_FIELD,
        }
    ),
    cv.has_at_least_one_key(ATTR_UUID, ATTR_UNICAST),
)


async def _find_new_devices(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """List the JUNG devices nearby that are not in a mesh yet (advertising the provisioning service)."""
    return cast("ServiceResponse", {"devices": onboard.unprovisioned_devices(hass)})


async def _add_device(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Provision and commission a new JUNG device, record it, reload the entry (review-3 N3, experimental).

    Refused unless the entry's *Allow Home Assistant to add devices* option is on. Admin only: it hands the
    device the mesh's keys.
    """
    entry_id = _entry_for_hub_services(hass, call.data)
    if not _hub(hass, entry_id).entry.options.get(
        OPTION_ALLOW_PROVISIONING, DEFAULT_ALLOW_PROVISIONING
    ):
        raise _validation("add_device_not_allowed")
    response: dict[str, Any] = {}
    keepers: list[VaultKeeper] = []

    async def operation(configurator: MeshConfigurator) -> bool:
        # the hub the lock handed us, on a live link (commissioning goes through it): a previous call's reload
        # replaces it
        keepers.append(configurator.hub.vault)
        response.update(
            await onboard.async_add_device(
                hass,
                configurator.hub,
                configurator,
                call.data[ATTR_ADDRESS],
                call.data[ATTR_NAME],
                call.data.get(ATTR_STATIC_OOB),
            )
        )
        return True

    try:
        # a new node: its vault record and its pending issue are the setup's (`model_update` leaves them to it)
        await _run(hass, entry_id, operation, reload=True)
    finally:
        # a device provisioned but not recorded is pending now (the reload after a success clears it)
        _update_pending_issue(hass, entry_id, keepers)
    return response


def _update_pending_issue(
    hass: HomeAssistant, entry_id: str, keepers: list[VaultKeeper]
) -> None:
    """Bring the entry's `pending_device` repair issue up to date with the vault the call used (if it got one)."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if keepers and entry is not None:
        onboard.async_update_pending_issue(hass, entry, keepers[0])


async def _reset_pending_device(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Reset a device `add_device` provisioned but did not record, and forget it (review-4 D2, unverified on air).

    Refused unless the entry's *Allow Home Assistant to add devices* option is on. Admin only: it sends a Config
    Node Reset with a device key only the vault holds. `force` forgets a device that does not confirm its reset.
    """
    entry_id = _entry_for_hub_services(hass, call.data)
    if not _hub(hass, entry_id).entry.options.get(
        OPTION_ALLOW_PROVISIONING, DEFAULT_ALLOW_PROVISIONING
    ):
        raise _validation("add_device_not_allowed")
    response: dict[str, Any] = {}
    keepers: list[VaultKeeper] = []

    async def operation(configurator: MeshConfigurator) -> bool:
        keepers.append(configurator.hub.vault)
        response.update(
            await onboard.async_reset_pending_device(
                configurator.hub,
                uuid=call.data.get(ATTR_UUID),
                unicast=call.data.get(ATTR_UNICAST),
                force=call.data[ATTR_FORCE],
            )
        )
        return False  # the export did not change: nothing to reload

    try:
        await _run(hass, entry_id, operation)
    finally:
        _update_pending_issue(hass, entry_id, keepers)
    return response if call.return_response else None


async def _remove_device(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Reset a node and take it out of the network, then reload (review-3 N4, experimental; irreversible).

    Refused unless the entry's *Allow Home Assistant to add devices* option is on, and without `confirm` (it
    cannot be undone; a dry run needs none); `force` records the removal of a node that does not confirm its
    reset (one that is gone for good).
    """
    if not call.data[ATTR_CONFIRM] and not call.data[ATTR_DRY_RUN]:
        raise _validation("remove_device_needs_confirm")
    entry_id, unicast = _resolve_node(hass, call.data[ATTR_DEVICE])
    if unicast is None:
        raise _validation("remove_device_mesh")
    if not _hub(hass, entry_id).entry.options.get(
        OPTION_ALLOW_PROVISIONING, DEFAULT_ALLOW_PROVISIONING
    ):
        raise _validation("add_device_not_allowed")
    force = call.data[ATTR_FORCE]
    # locked, on a live link, and reloaded after a stop too: the reset is recorded even when the unwiring stops
    result = await _execute(
        hass,
        call,
        entry_id,
        lambda c: c.remove_node(unicast, force=force),
        reload=True,
    )
    return _answer(call, [result])


async def _locate_node(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Have a node advertise its Node Identity for `duration` seconds (review-4 F4-15; unverified on air).

    Admin only: a Config Node Identity Set, sealed with the node's device key, then the same Set off
    (`JungHomeHub.async_locate`). It changes nothing but what the node advertises, and the node stops by itself
    within a minute. Locked like the other actions, on a live link.
    """
    entry_id, unicast = _resolve_node(hass, call.data[ATTR_DEVICE])
    if unicast is None:
        raise _validation("remove_device_mesh")
    seconds = call.data[ATTR_DURATION]
    response: dict[str, Any] = {}

    async def operation(configurator: MeshConfigurator) -> bool:
        hub = configurator.hub
        node = hub.cdb.node_by_addr(unicast)
        assert node is not None  # `_resolve_node` found it in this export
        try:
            status = await hub.async_locate(node, seconds)
        except TimeoutError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="locate_no_answer",
                translation_placeholders={"node": f"{unicast:04X}"},
            ) from err
        except (ConnectionError, OSError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="send_failed"
            ) from err
        if not status.ok:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="locate_refused",
                translation_placeholders={
                    "node": f"{unicast:04X}",
                    "status": status.status_name,
                },
            )
        if status.identity != C.NODE_IDENTITY_RUNNING:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="locate_not_supported",
                translation_placeholders={"node": f"{unicast:04X}"},
            )
        response.update(node=f"{unicast:04X}", seconds=seconds)
        return False

    await _run(hass, entry_id, operation)
    return response if call.return_response else None
