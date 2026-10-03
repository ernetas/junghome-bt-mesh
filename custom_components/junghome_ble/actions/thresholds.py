"""The socket threshold actions `set_threshold` and `delete_threshold`: a property plus wiring (`thresholds.py`)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.helpers import config_validation as cv

from ..entity import load_entity_id
from ..jhmesh.devices import Socket
from ..thresholds import (
    CLEARED,
    OTHER_THRESHOLD,
    THRESHOLD_PROPERTIES,
    ThresholdProgress,
    Which,
    current_threshold,
    has_thresholds,
    planned_threshold,
    write_threshold,
)
from .common import ONOFF_LOAD_TYPES, TARGETS_SCHEMA, _answer, _hub, _run, _validation
from .resolve import _device_of_entity, _resolve_loads

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse

    from ..coordinator import JungHomeHub
    from ..jhmesh.properties import Threshold
    from ..mesh_config import MeshConfigurator


ATTR_THRESHOLD = "threshold"
ATTR_DEVICES = "devices"
THRESHOLD_POWER_MAX = 1677721.4  # W: 24 bits of 0.1 W, all ones meaning "none"

SET_THRESHOLD_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(ATTR_THRESHOLD): vol.In(THRESHOLD_PROPERTIES),
            vol.Optional("power"): vol.All(
                vol.Coerce(float), vol.Range(min=0, max=THRESHOLD_POWER_MAX)
            ),
            vol.Optional("duration"): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=0xFFFF)
            ),
            vol.Optional("enabled"): cv.boolean,
            vol.Optional(ATTR_DEVICES): cv.entity_ids,
            **cv.ENTITY_SERVICE_FIELDS,
        }
    ),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
)
DELETE_THRESHOLD_SCHEMA = TARGETS_SCHEMA


# ------------------------------------------------------------------ thresholds


async def _threshold_sockets(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, list[int]]:
    """Return the call's metering sockets by entry; each must have the thresholds (the product measures)."""
    out: dict[str, list[int]] = {}
    for load in await _resolve_loads(hass, call, (Socket,)):
        hub = _hub(hass, load.entry_id)
        socket = hub.devices.by_address[load.address]
        assert isinstance(socket, Socket)
        if not has_thresholds(hub, socket):
            raise _validation(
                "threshold_not_supported", name=load_entity_id(hass, socket)
            )
        out.setdefault(load.entry_id, []).append(load.address)
    return out


def _threshold_devices(
    hass: HomeAssistant, entity_ids: list[str], entry_id: str
) -> list[int]:
    """Return the load elements behind `devices`: lights and sockets (an OnOff server) of the socket's network."""
    out: list[int] = []
    for entity_id in entity_ids:
        owner, device, _ = _device_of_entity(hass, entity_id)
        if not isinstance(device, ONOFF_LOAD_TYPES):
            raise _validation("service_not_a_load", name=entity_id)
        if owner != entry_id:
            raise _validation("threshold_other_network")
        out.append(device.address)
    return out


def _socket(hub: JungHomeHub, address: int) -> Socket:
    socket = hub.devices.by_address[address]
    assert isinstance(socket, Socket)
    return socket


async def _set_threshold(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Write a socket's switch-on or switch-off threshold; `devices` replaces the loads both thresholds switch.

    In the app's order: the threshold first, then the wiring. Every socket's wiring checks and value (read when
    the call leaves a field out) come before anything is written, so a refused call leaves every socket as it was.
    Without `devices`, a call that disables the threshold
    (`enabled: false`) while the socket's other one is not active either unwires the loads as the app's disable
    (`ToggleThreshold`) does (`MeshConfigurator.unwire_threshold`); an other threshold the socket does not tell
    about keeps them wired. Editing the level or duration of a disabled threshold is no disable: the loads stay.
    A failure names the thresholds and sockets already written (`ThresholdProgress`, W4-13).
    """
    which: Which = call.data[ATTR_THRESHOLD]
    needs_current = not {"power", "duration", "enabled"} <= set(call.data)
    results: list[dict[str, Any]] = []
    for entry_id, sockets in (await _threshold_sockets(hass, call)).items():
        devices = (
            _threshold_devices(hass, call.data[ATTR_DEVICES], entry_id)
            if ATTR_DEVICES in call.data
            else None
        )

        async def operation(
            configurator: MeshConfigurator,
            sockets: list[int] = sockets,
            devices: list[int] | None = devices,
        ) -> bool:
            # the hub the lock handed us: the one a previous call's reload left
            hub = configurator.hub
            values: list[Threshold] = []
            for address in sockets:
                socket = _socket(hub, address)
                if devices is not None:
                    await configurator.check_threshold_devices(address, devices)
                current = (
                    await current_threshold(hass, hub, socket, which)
                    if needs_current
                    else None
                )
                values.append(
                    planned_threshold(current, call.data, load_entity_id(hass, socket))
                )
            progress = ThresholdProgress()
            changed = False
            for address, value in zip(sockets, values, strict=True):
                socket = _socket(hub, address)
                await write_threshold(hass, hub, socket, which, value, progress)
                if devices is not None:
                    changed = (
                        await configurator.set_threshold_devices(
                            address, devices, applied=progress.applied
                        )
                        or changed
                    )
                elif call.data.get("enabled") is False:
                    other = await current_threshold(
                        hass, hub, socket, OTHER_THRESHOLD[which]
                    )
                    if other is not None and not other.active:
                        changed = (
                            await configurator.unwire_threshold(
                                address, applied=progress.applied
                            )
                            or changed
                        )
                progress.finish(address)
            return changed

        results.append(await _run(hass, entry_id, operation))
    return _answer(call, results)


async def _delete_threshold(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Clear both thresholds of the sockets, then unwire the loads they switched and reset the client's publication (the app's delete)."""
    results: list[dict[str, Any]] = []
    for entry_id, sockets in (await _threshold_sockets(hass, call)).items():

        async def operation(
            configurator: MeshConfigurator, sockets: list[int] = sockets
        ) -> bool:
            hub = configurator.hub
            progress = (
                ThresholdProgress()
            )  # a failure names what was cleared before it (W4-13)
            changed = False
            for address in sockets:
                for which in THRESHOLD_PROPERTIES:
                    await write_threshold(
                        hass, hub, _socket(hub, address), which, CLEARED, progress
                    )
                changed = (
                    await configurator.unwire_threshold(
                        address, applied=progress.applied
                    )
                    or changed
                )
                progress.finish(address)
            return changed

        results.append(await _run(hass, entry_id, operation))
    return _answer(call, results)
