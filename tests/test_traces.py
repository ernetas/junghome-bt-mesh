"""Replay the traces of `tests/traces/` through the fake proxy and pin what Home Assistant made of them (Q4 T5).

A trace (`tools/trace_to_fixture.py`, `docs/dev/testing.md` *Replayed traces*) is on-air traffic of an installation
turned into traffic of the fixture network: one message per line with the gap to the previous one. Each is injected
as its node sent it (`FakeProxyLink.inject`, with the TTL it was heard with) on a clock that moves by the trace's
gaps, and every state change and every bus event of the integration is snapshotted, so a change in how the hub
reads real traffic (the doubled publications, the unicast replies, the button counters) shows up as a diff.

Each trace is also held to what may be committed to a public repository: only the converter's fields, only fixture
or pool addresses, no MAC, no device-key or key-carrying message, and network PDUs that are exactly the trace's
access PDUs encrypted under the fixture's keys — so nothing else can hide in them.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh.advert import mac_from_uuid
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.pdu import decode_opcode
from custom_components.junghome_ble.jhmesh.sniffer import MeshDecoder, SniffRecord

from . import key_scan
from .conftest import CDB_PATH, FakeProxyLink
from .traces import make_traces

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from syrupy.assertion import SnapshotAssertion

TRACES = sorted((Path(__file__).parent / "traces").glob("*.ndjson"))
FIELDS = {"delay", "src", "dst", "ttl", "access", "pdus"}
MAC = re.compile(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")
UNICAST_POOL, GROUP_POOL = (
    (0x7000, 0x7FFF),
    (0xC800, 0xCFFF),
)  # `trace_to_fixture.UNICAST_POOL` / `GROUP_POOL`

pytestmark = pytest.mark.trace


def read_trace(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def fixture_addresses(cdb: CDB) -> set[int]:
    return {e.address for n in cdb.nodes for e in n.elements} | set(cdb.groups)


def allowed(address: int, fixture: set[int]) -> bool:
    """A fixture address, a pool address of the converter, unassigned or a fixed group."""
    return (
        address in fixture
        or UNICAST_POOL[0] <= address <= UNICAST_POOL[1]
        or GROUP_POOL[0] <= address <= GROUP_POOL[1]
        or address == 0
        or address >= 0xFF00
    )


def test_there_is_a_trace() -> None:
    """The synthetic one at least; one per verified device class once the maintainer has captured them."""
    assert "synthetic.ndjson" in {p.name for p in TRACES}


@pytest.mark.parametrize("path", TRACES, ids=lambda p: p.stem)
def test_trace_holds_only_fixture_material(path: Path) -> None:
    """Nothing of an installation: the converter's fields, fixture or pool addresses, no MAC, no key in any form."""
    cdb = CDB.load(Path(CDB_PATH))
    fixture = fixture_addresses(cdb)
    text = path.read_text(encoding="utf-8")
    fixture_macs = {m for n in cdb.nodes if (m := mac_from_uuid(n.uuid)) is not None}
    assert {m.upper().replace("-", ":") for m in MAC.findall(text)} <= fixture_macs
    # the stand-in installation the synthetic trace was made from: none of its keys or identities either
    standin = CDB.from_network(make_traces.standin_network())
    assert key_scan.leaks(text, key_scan.secrets(standin)) == []
    assert standin.mesh_uuid.replace("-", "") not in text.upper()
    decoder = MeshDecoder(cdb)
    for number, doc in enumerate(read_trace(path), 1):
        where = f"{path.name} line {number}"
        assert set(doc) == FIELDS, where
        src, dst = int(doc["src"], 16), int(doc["dst"], 16)
        assert allowed(src, fixture), where
        assert allowed(dst, fixture), where
        assert isinstance(doc["delay"], float | int), where
        assert doc["delay"] >= 0, where
        access = bytes.fromhex(doc["access"])
        opcode, company, _params = decode_opcode(access)
        # device-key traffic (Config messages, every key-carrying one) is never part of a trace
        assert company is not None or opcode not in C.CONFIG_NAMES, where
        assert opcode not in C.KEY_CARRYING_OPCODES or company is not None, where
        # the network PDUs are this access PDU under the fixture's keys and nothing else
        decoded = [
            decoder.feed(SniffRecord(float(number), 37, 0, "", "msg", bytes.fromhex(p)))
            for p in doc["pdus"]
        ]
        last = decoded[-1]
        assert last.kind == "access", where
        assert last.message is not None
        assert (last.message.src, last.message.dst, last.message.ttl) == (
            src,
            dst,
            doc["ttl"],
        ), where
        assert last.message.key == "app0", where
        assert last.message.access_pdu == access, where


@pytest.mark.parametrize("path", TRACES, ids=lambda p: p.stem)
async def test_replay(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    snapshot: SnapshotAssertion,
    path: Path,
) -> None:
    """Every state change and every integration event the trace causes, in order."""
    freezer.move_to("2020-01-01T00:00:00+00:00")
    devices = dr.async_get(hass)
    seen: list[dict[str, Any]] = []

    @callback
    def record(event: Event[Any]) -> None:
        if event.event_type == EVENT_STATE_CHANGED:
            new = event.data["new_state"]
            if new is not None:
                seen.append(
                    {
                        "entity_id": event.data["entity_id"],
                        "state": new.state,
                        "attributes": dict(new.attributes),
                    }
                )
        elif event.event_type.startswith(DOMAIN):
            data = dict(event.data)
            if (device := devices.async_get(data.pop("device_id", ""))) is not None:
                data["device"] = sorted(device.identifiers)
            seen.append({"event": event.event_type, "data": data})

    unsub = hass.bus.async_listen("*", record)
    for doc in read_trace(path):
        freezer.tick(timedelta(seconds=doc["delay"]))
        async_fire_time_changed(hass)
        fake_link.inject(
            int(doc["src"], 16),
            int(doc["dst"], 16),
            bytes.fromhex(doc["access"]),
            ttl=doc["ttl"],
        )
        await hass.async_block_till_done()
    unsub()
    assert seen == snapshot
