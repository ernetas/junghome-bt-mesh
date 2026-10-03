"""Device renames in Home Assistant, written into the app's project the way the app's own rename does.

The JUNG HOME app renames a device (a load, a gang of keys, a room thermostat or detector) with
`UpdateDeviceData.UpdateDeviceName`: the name check (`CheckNameInput`: not blank, no lone `%`; the rename sheet
takes at most 30 characters), a number suffix
when another device already has the name, then `meta.devices[].name` — never the CDB node name — and the export
upload to the gateway; nothing goes on air. Home Assistant gives an integration no rename hook of its own, so the
entry follows the device registry: a user naming one of those devices (`name_by_user`) has
`MeshConfigurator.rename_device` write the name the same way, in a Home Assistant background task rather than one
of the entry's (the reload after an adopted export would otherwise wait for the very task running it). The name the export then holds becomes the device's
own name and `name_by_user` is cleared, so Home Assistant shows what the app shows (a suffixed name included), and
a later rename in the app reaches Home Assistant with the next export it loads.

No proxy link is needed and the entry is not reloaded for it: the device's name is the only thing that changed,
and the registry carries it to the entities at once (an export the configurator adopted from the gateway first
still reloads, as after every action). A name the app would refuse leaves the export alone and raises the
`device_name_rejected` repair issue, which the next accepted rename clears; any other failure is logged. Renaming
the mesh device, or a node device that stands for no app device (an actuator's node, the gateway), stays Home
Assistant's alone.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ISSUE_DEVICE_NAME
from .coordinator import issue_id
from .entity import button_gang, buttons_device_id, node_identifier
from .services import async_configure

if TYPE_CHECKING:
    from . import JungHomeConfigEntry
    from .coordinator import JungHomeHub
    from .jhmesh.devices import Device
    from .mesh_config import MeshConfigurator

_LOGGER = logging.getLogger(__name__)

# the configurator's errors for a name the app's `CheckNameInput` refuses
NAME_ERRORS = frozenset(
    {"service_name_blank", "service_name_not_allowed", "service_name_too_long"}
)


def app_device(hub: JungHomeHub, device: dr.DeviceEntry) -> Device | None:
    """Return the mesh device standing for the app device a registry device shows; None when there is none.

    A load's device is the load, a buttons device its gang's first key, a node device the room thermostat or
    detector it carries (`node_device_info` names it after that unit); the mesh device and the other node devices
    stand for no app device.
    """
    ident = next((i for domain, i in device.identifiers if domain == DOMAIN), "")
    if ident.startswith("node:"):
        units: list[Device] = [*hub.devices.thermostats, *hub.devices.detectors]
        return next((u for u in units if node_identifier(u.node) == ident), None)
    if ident.endswith("-buttons"):
        return next(
            (
                b
                for b in hub.devices.buttons
                if buttons_device_id(button_gang(hub, b)) == ident
            ),
            None,
        )
    loads: list[Device] = [
        *hub.devices.lights,
        *hub.devices.sockets,
        *hub.devices.blinds,
    ]
    return next((d for d in loads if d.unique_id == ident), None)


@callback
def async_track_device_names(
    hass: HomeAssistant, entry: JungHomeConfigEntry
) -> CALLBACK_TYPE:
    """Follow the entry's devices being named by the user; returns the function that stops following."""

    @callback
    def renamed(data: dr.EventDeviceRegistryUpdatedData) -> bool:
        return data["action"] == "update" and "name_by_user" in data["changes"]

    @callback
    def handle(event: Event[dr.EventDeviceRegistryUpdatedData]) -> None:
        device = dr.async_get(hass).async_get(event.data["device_id"])
        if (
            not isinstance(device, dr.DeviceEntry)
            or device.config_entry_id != entry.entry_id
            or not device.name_by_user  # the name reset: the app's own name is back
        ):
            return
        target = app_device(entry.runtime_data, device)
        if target is None:
            return
        # not an entry task: a rename that adopted the gateway's export reloads the entry, and an unload waits
        # for the entry's own tasks — this one among them, which would only end after that wait (review-4 W4-10)
        hass.async_create_background_task(
            async_write_name(hass, entry, device, target.address, device.name_by_user),
            f"{DOMAIN} device rename",
        )

    return hass.bus.async_listen(
        dr.EVENT_DEVICE_REGISTRY_UPDATED, handle, event_filter=renamed
    )


async def async_write_name(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    device: dr.DeviceEntry,
    address: int,
    name: str,
) -> None:
    """Write `name` for the app device of the element at `address`, then name the registry device as the export does."""
    written: list[str] = []

    async def operation(configurator: MeshConfigurator) -> bool:
        written.append(await configurator.rename_device(address, name))
        return (
            configurator.adopted
        )  # a new name changes no entity; an adopted export may

    issue = issue_id(entry, ISSUE_DEVICE_NAME)
    try:
        await async_configure(hass, entry.entry_id, operation, needs_link=False)
    except ServiceValidationError as err:
        if err.translation_key not in NAME_ERRORS:
            _LOGGER.warning("The new name of %s was not written: %s", device.name, err)
            return
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_DEVICE_NAME,
            translation_placeholders={"name": name, "device": device.name or name},
        )
        return
    except HomeAssistantError as err:
        _LOGGER.warning("The new name of %s was not written: %s", device.name, err)
        return
    ir.async_delete_issue(hass, DOMAIN, issue)
    registry = dr.async_get(hass)
    if registry.async_get(device.id) is not None:
        registry.async_update_device(device.id, name=written[0], name_by_user=None)
