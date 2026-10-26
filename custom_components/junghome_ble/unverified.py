"""The device types no real device has answered yet, and the option that lets Home Assistant write to them.

The maintainer owns no blind, room thermostat, detector, battery transmitter (the wall transmitters and the battery
binary-input puck) or energy puck: what the integration sends them follows the app and the gateway, but none of it
was seen working on a real device — a blind's Generic Move Set, say, carries a transition byte nobody captured
(`const.COVER_MOVE_TRANSITION`). Until the entry's option *Send commands and settings to unverified devices*
(`OPTION_UNVERIFIED_WRITES`, experimental, off by default) is on, such a device is only read: every place that writes
to a device asks `refuse_unverified` with the addresses it is about to write to, before anything is sent, and the
call is refused when one of them reaches such a device. The option applies at once (`const.LIVE_OPTIONS`).

Gated: the entities' commands and settings (the hub's Sets, the property reader's writes, a meter reset, the time
keeper) and the actions' writes (the rewiring plans and the keys' properties, scenes, schedules). Not gated: reads;
the central functions of lights and sockets (*All lights*, a room's lights) and scene recalls, which an energy
puck's or a detector's output among the members takes as any switch does; and the network upkeep every node takes
alike, seen working on the maintainer's devices — identify, clear faults, locate, heartbeats, the time, a key
refresh, an IV Update.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Final

from homeassistant.exceptions import ServiceValidationError

from .const import DEFAULT_UNVERIFIED_WRITES, DOMAIN, OPTION_UNVERIFIED_WRITES
from .device_info import address_label
from .jhmesh.devices import (
    BATTERY_PIDS,
    DETECTOR_PIDS,
    THERMOSTAT_PIDS,
    meter_element,
)
from .jhmesh.pdu import is_group

if TYPE_CHECKING:
    from .jhmesh.cdb import Node
    from .protocols import HubView

# the switch actuator 1-gang 2-input energy; the 2-channel actuator shares its product id, but has no meter
ENERGY_PUCK_PID: Final = 0x0010
# the products that are of an unverified type whatever they drive; the energy puck and a blind are told by what the
# node has (`is_unverified`)
UNVERIFIED_PIDS: Final = frozenset({*THERMOSTAT_PIDS, *DETECTOR_PIDS, *BATTERY_PIDS})


def unverified_writes_on(hub: HubView) -> bool:
    """Whether the entry's option *Send commands and settings to unverified devices* is on."""
    return bool(
        hub.entry.options.get(OPTION_UNVERIFIED_WRITES, DEFAULT_UNVERIFIED_WRITES)
    )


def is_unverified(hub: HubView, node: Node) -> bool:
    """Whether `node` is of a type no real device has answered yet: `UNVERIFIED_PIDS`, the energy puck, a blind.

    The energy puck is product 0x0010 with a meter (`meter_element`): the 2-channel actuator of the same product id
    has none, and was seen working. A push-button with a blinds insert drives a blind: it counts as one, its keys'
    settings included.
    """
    if node.pid in UNVERIFIED_PIDS:
        return True
    if node.pid == ENERGY_PUCK_PID and meter_element(node) is not None:
        return True
    return any(blind.node.unicast == node.unicast for blind in hub.devices.blinds)


def _reached(hub: HubView, addresses: Iterable[int]) -> Iterator[tuple[int, Node]]:
    """Yield the elements a message to each of `addresses` reaches, with their nodes.

    An element is itself; a device-type group (*All blinds*, *All RTRs*) or a room is the members that listen to it.
    """
    for address in addresses:
        if is_group(address):
            members = hub.devices.central.get(address)
            if members is None:
                members = hub.devices.room_members.get(address, [])
            for member in members:
                yield member.address, member.node
        elif (node := hub.cdb.node_by_addr(address)) is not None:
            yield address, node


def refuse_unverified(hub: HubView, addresses: Iterable[int]) -> None:
    """Refuse a write to `addresses` that reaches a device of an unverified type while the option is off.

    Raises ServiceValidationError naming the first such device; asked before anything of the call is sent.
    """
    if unverified_writes_on(hub):
        return
    for address, node in _reached(hub, addresses):
        if is_unverified(hub, node):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unverified_device",
                translation_placeholders={"device": address_label(hub, address)},
            )
