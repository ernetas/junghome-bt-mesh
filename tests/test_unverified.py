"""The unverified device types (`unverified.py`): which nodes they are, and what a write to an address reaches.

What each platform and action refuses while the option is off is tested with it (`test_cover.py`,
`test_climate.py`, `test_energy_puck.py`, `test_services.py`).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.junghome_ble.const import OPTION_UNVERIFIED_WRITES
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.devices import (
    ALL_BLINDS,
    ALL_LIGHTS,
    Metadata,
    build_devices,
)
from custom_components.junghome_ble.unverified import (
    _reached,
    is_unverified,
    refuse_unverified,
    unverified_writes_on,
)

from .conftest import FIXTURES
from .helpers import (
    NODE_BLIND,
    NODE_MOTION,
    NODE_PRESENCE,
    NODE_PUCK,
    NODE_SHUTTER,
    NODE_THERMOSTAT,
    NODE_TRANSMITTER_1G,
    NODE_TRANSMITTER_2G,
)

NODE_ENERGY_PUCK = "00005eff-fe00-5361-0000-000000000000"  # MeshNetwork-puck.json


def hub_of(export: str, **options: Any) -> Any:
    """A hub stand-in: the export's CDB and devices, an entry with `options`."""
    cdb = CDB.load(Path(FIXTURES / export))
    return SimpleNamespace(
        cdb=cdb,
        devices=build_devices(cdb, Metadata.from_export(cdb.export_meta)),
        entry=SimpleNamespace(options=options),
    )


@pytest.mark.parametrize(
    ("export", "unverified"),
    [
        # the maintainer's devices, the 2-channel actuator (0x0010, no meter) included
        ("MeshNetwork.json", set()),
        ("MeshNetwork-rtr.json", {NODE_THERMOSTAT}),
        (
            "MeshNetwork-detectors.json",
            {NODE_MOTION, NODE_PRESENCE, NODE_TRANSMITTER_1G, NODE_TRANSMITTER_2G},
        ),
        ("MeshNetwork-puck.json", {NODE_ENERGY_PUCK}),  # 0x0010 with a meter
        # the blinds actuator mini, the push-button with a blinds insert, the blinds PP2 puck
        ("Blinds.json", {NODE_BLIND, NODE_SHUTTER, NODE_PUCK}),
    ],
)
def test_the_unverified_nodes_of_each_network(
    export: str, unverified: set[str]
) -> None:
    hub = hub_of(export)
    assert {
        n.uuid.lower() for n in hub.cdb.nodes if is_unverified(hub, n)
    } == unverified


def test_a_group_reaches_its_members() -> None:
    """A device-type group or a room is the members listening to it; an element is itself; a stranger nothing."""
    hub = hub_of("Blinds.json")
    blinds = [b.address for b in hub.devices.blinds]
    assert [address for address, _ in _reached(hub, [ALL_BLINDS])] == blinds
    assert all(not is_unverified(hub, node) for _, node in _reached(hub, [ALL_LIGHTS]))
    room, members = next(iter(hub.devices.room_members.items()))
    assert [address for address, _ in _reached(hub, [room])] == [
        m.address for m in members
    ]
    assert [address for address, _ in _reached(hub, [blinds[0], 0x7000])] == [blinds[0]]


@pytest.mark.unverified_writes_off
def test_the_option_is_off_by_default_and_lets_every_write_through_when_on() -> None:
    """Off unless the entry says so; on, nothing is looked up (the refusal names the device through the registry)."""
    assert not unverified_writes_on(hub_of("Blinds.json"))
    hub = hub_of("Blinds.json", **{OPTION_UNVERIFIED_WRITES: True})
    assert unverified_writes_on(hub)
    refuse_unverified(hub, [ALL_BLINDS, *(b.address for b in hub.devices.blinds)])
    # verified devices only: nothing to refuse, even off
    hub = hub_of("MeshNetwork.json")
    refuse_unverified(hub, [n.unicast for n in hub.cdb.nodes])
