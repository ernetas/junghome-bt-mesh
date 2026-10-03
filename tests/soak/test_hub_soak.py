"""A seeded soak: the hub over a scaled simulated mesh while its proxy keeps dropping the link (review-4 R4-1, R4-5).

The link to the proxy 0148 is dropped after `HOLDS` virtual seconds (drawn per link), on an air that loses and doubles PDUs at
random (seeded), for `FLAPS` links, with a command to a load four hops away sent right after each drop; then the
link is left alone. Throughout, and at the end:

- per source, every (IV index, SEQ) the hub sends is unique, nothing it sends is replayed or undecryptable, nothing
  is lost that the loss model did not drop (the `Mesh` invariants, checked at teardown);
- the grace is honoured: an entity stays available for `LINK_LOSS_GRACE` after a link is lost and turns unavailable
  after it when no new link came (sampled every virtual second); a command sent in the grace goes out on the next
  link, or fails once the grace is over — it is never applied otherwise;
- bounded: the `PropertyReader` queue holds each job once and never more than the first links queued, and no
  container of the hub, its reader or its proxy client holds more than the network has entities and elements; the
  tasks on the loop do not pile up from link to link;
- once the link holds, the connect-time refresh completes (the link state turns `connected`) and every load's state
  is known.

Everything runs on virtual time (`tests/soak/conftest.py`): by default 80 nodes and a handful of links in a few
seconds of wall time; `SIM_SOAK=long` runs 300 nodes through 40 links (a minute or two).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections import Counter, deque
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import event as ha_event
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.junghome_ble as integration
from custom_components.junghome_ble import (
    binary_sensor,
    climate,
    config_entities,
    coordinator,
    cover,
    keep_awake,
    sensor,
    switch,
)
from custom_components.junghome_ble.config_entities import READERS
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_METADATA_DIR,
    CONF_UNICAST,
    DOMAIN,
    LINK_CONNECTED,
    LINK_LOSS_GRACE,
    PROPERTY_READ_CHUNK,
    REFRESH_CHUNK,
)
from custom_components.junghome_ble.jhmesh import client as client_mod
from tests.conftest import META_DIR, setup_entry
from tests.helpers import LIGHT_OUT1, NODE_ACTUATOR, UID_LIGHT_SWITCH, entity_id
from tests.sim import LossModel, patch_monotonic
from tests.sim.synthetic import PROXY, build_mesh, scaled_network

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from tests.conftest import SimLinks
    from tests.sim import Mesh

_LOGGER = logging.getLogger(__name__)

LONG = os.environ.get("SIM_SOAK") == "long"
SEED = int(os.environ.get("SIM_SOAK_SEED", "27"))
NODES = 300 if LONG else 80
FLAPS = 40 if LONG else 6
# virtual seconds a link holds, drawn (seeded) for each link: mostly short (under `SHORT_LINK`: the back-off grows,
# the node is set aside), now and then long enough to reset the back-off
HOLDS = (30.0, 45.0, 45.0, 90.0)
LOSS = LossModel(loss=0.02, duplicate=0.02)
LINK_WAIT = 600.0  # virtual seconds the hub may take for its next link (the back-off tops out at 60 s)
SETTLE_LIMIT = 1800.0  # virtual seconds the stable link has to finish its refresh in
TOLERANCE = 1.0  # virtual seconds an availability change may lag (the sampler's period)
UID_FAR = f"{NODE_ACTUATOR}-0001"  # the 2-channel actuator's first channel (0400), four hops from the proxy
# every module that reads `time`: their monotonic and wall clocks follow the virtual one
TIMED = (
    binary_sensor,
    climate,
    client_mod,
    config_entities,
    coordinator,
    cover,
    integration,
    keep_awake,
    sensor,
    switch,
)

pytestmark = [pytest.mark.sim, pytest.mark.slow_ok, pytest.mark.link_loss_grace]


@pytest.fixture
async def sim_mesh(tmp_path: Path) -> AsyncGenerator[Mesh]:
    """The scaled synthetic network on a lossy air (seeded), its export in `tmp_path`; closed and checked after."""
    mesh = build_mesh(
        scaled_network(NODES, seed=SEED),
        tmp_path / "MeshNetwork.json",
        seed=SEED,
        loss=LOSS,
    )
    yield mesh
    await mesh.close()
    mesh.assert_invariants()


@dataclass
class Sample:
    """The hub as the sampler saw it at one virtual second."""

    at: float
    link_up: bool
    lost_at: float | None
    available: bool


def containers(hub: Any) -> dict[str, int]:
    """The size of every dict, list, set and deque the hub, its property reader and its proxy client hold."""
    reader = hub.hass.data[READERS][hub.entry.entry_id]
    sizes: dict[str, int] = {}
    for owner, obj in (("hub", hub), ("reader", reader), ("client", hub.proxy)):
        for name, value in vars(obj).items():
            if isinstance(value, dict | list | set | deque):
                sizes[f"{owner}.{name}"] = len(value)
    return sizes


async def until(predicate: Callable[[], bool], limit: float, what: str) -> None:
    """Let virtual time pass, a second at a time, until `predicate` holds; fail after `limit` virtual seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while not predicate():
        assert loop.time() < deadline, f"{what} not reached within {limit:g} virtual s"
        await asyncio.sleep(1.0)


def virtual_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every clock the hub and Home Assistant's time trackers read follows the loop's virtual one.

    The wall clock starts at the real time and moves with the loop; a decision on a real clock would make the run
    depend on how fast the machine is.
    """
    clocks = patch_monotonic(monkeypatch, *TIMED, ha_event)
    clocks.wall_offset = time.time()

    def utcnow() -> datetime:
        return datetime.fromtimestamp(clocks.time(), UTC)

    monkeypatch.setattr(dt_util, "utcnow", utcnow)
    monkeypatch.setattr(ha_event, "time_tracker_utcnow", utcnow)
    monkeypatch.setattr(ha_event, "time_tracker_timestamp", clocks.time)


def check_grace(samples: list[Sample]) -> None:
    """Available with a link and within the grace after a loss, unavailable once the grace is over."""
    for s in samples:
        if s.link_up:
            assert s.available, f"unavailable with a link up, at {s.at:.0f} s"
        elif s.lost_at is not None:
            down = s.at - s.lost_at
            if down < LINK_LOSS_GRACE - TOLERANCE:
                assert s.available, f"unavailable {down:.0f} s into the grace"
            elif down > LINK_LOSS_GRACE + TOLERANCE:
                assert not s.available, f"still available {down:.0f} s after the loss"


async def command_in_the_grace(
    hass: HomeAssistant, hub: Any, far: str, load: Any, flap: int
) -> str:
    """Switch the far light right after a link was lost; wait for the next link and the command's end.

    "applied": the command went through and the load is as asked; "no link": it failed for want of a link (the
    grace ran out first), and the load is untouched; "unanswered": it went out, and the air lost what the load
    answered or what it was sent — the load is either way. "confirmed, not applied": it went through although the
    load is not as asked — a Set lost on the air whose waiter took the Status that answered another request to the
    element (a Get of the new link's refresh). The light's state shows the load's either way.
    """
    on = flap % 2 == 0
    before = load.on
    command = hass.async_create_task(
        hass.services.async_call(
            LIGHT_DOMAIN,
            SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: far},
            blocking=True,
        )
    )
    await until(lambda: hub.connected, LINK_WAIT, "the next link")
    await until(command.done, LINK_WAIT, "the command")
    error = command.exception()
    if error is None:
        if load.on is not on:
            await until(
                lambda: hass.states.get(far).state == ("on" if load.on else "off"),
                LINK_WAIT,
                "the light showing its load",
            )
            return "confirmed, not applied"
        return "applied"
    assert isinstance(error, HomeAssistantError), error
    if error.translation_key == "send_failed":
        assert load.on is before, (
            f"link {flap + 2}: a command that found no link was applied"
        )
        return "no link"
    assert error.translation_key == "device_not_reachable", error
    return "unanswered"


@dataclass
class Record:
    """What the soak saw: availability every virtual second; per link, the containers' sizes, the tasks on the loop
    by coroutine and what became of the command sent in the grace."""

    samples: list[Sample] = field(default_factory=list)
    sizes: list[dict[str, int]] = field(default_factory=list)
    tasks: list[Counter[str]] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)


async def flap(hass: HomeAssistant, hub: Any, mesh: Mesh, record: Record) -> None:
    """Drop the link `FLAPS` times, a command to the far light right after each drop; then wait for a full refresh."""
    loop = asyncio.get_running_loop()
    far = entity_id(hass, "light", UID_FAR)
    servers = mesh.node(LIGHT_OUT1).servers
    assert servers is not None
    await until(lambda: hub.connected, 120, "the first link")
    holds = random.Random(SEED)  # noqa: S311  # a repeatable schedule, not a secret
    for link in range(FLAPS):
        await asyncio.sleep(holds.choice(HOLDS))
        mesh.proxy(PROXY).drop_link()
        dropped_at = loop.time()
        outcome = await command_in_the_grace(
            hass, hub, far, servers.state[LIGHT_OUT1], link
        )
        back = loop.time() - dropped_at
        if outcome == "no link":
            assert back >= LINK_LOSS_GRACE - TOLERANCE, (
                f"link {link + 2} came {back:.0f} s after the loss, yet the command found none"
            )
        record.outcomes.append(outcome)
        record.sizes.append(containers(hub))
        record.tasks.append(
            Counter(
                getattr(t.get_coro(), "__qualname__", "?") for t in asyncio.all_tasks()
            )
        )
    await until(
        lambda: hub.link_state == LINK_CONNECTED, SETTLE_LIMIT, "a complete refresh"
    )


def check_bounds(hass: HomeAssistant, hub: Any, record: Record) -> None:
    """Each queued read once, no more than the first links queued; nothing larger than the network; single tasks."""
    reader = hass.data[READERS][hub.entry.entry_id]
    assert len(reader._jobs) == len(reader._queued)
    assert (
        max(s["reader._jobs"] for s in record.sizes) <= record.sizes[0]["reader._jobs"]
    )
    registry = er.async_get(hass)
    entities = len(er.async_entries_for_config_entry(registry, hub.entry.entry_id))
    bound = entities + sum(len(n.elements) for n in hub.cdb.nodes)
    for name, size in record.sizes[-1].items():
        assert size <= bound, f"{name} holds {size} entries (bound {bound})"
    # one connection loop, at most one connect-time sequence and one reader worker; reads in chunks
    for counts in record.tasks:
        assert counts["JungHomeHub._connection_loop"] == 1
        assert counts["JungHomeHub._after_connect"] <= 1
        assert counts["PropertyReader._run"] <= 1
        busiest = counts.most_common(1)[0]
        assert busiest[1] <= max(PROPERTY_READ_CHUNK, REFRESH_CHUNK), busiest


async def test_flapping_links_keep_the_invariants(
    hass: HomeAssistant,
    sim_mesh: Mesh,
    sim_link: SimLinks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    virtual_clocks(monkeypatch)
    loop = asyncio.get_running_loop()
    # Home Assistant's fixtures run every test's loop in debug mode, which records a stack for every timer: the
    # hundreds of thousands the air schedules would take most of the run
    loop.set_debug(False)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh soak",
        unique_id="1fbd2c61a4b6e5a4",
        data={
            CONF_CDB_PATH: str(sim_mesh.cdb_path),
            CONF_METADATA_DIR: META_DIR,
            CONF_UNICAST: "0D00",
        },
    )
    started = time.monotonic()
    await setup_entry(hass, entry)
    hub = entry.runtime_data
    eid = entity_id(hass, "light", UID_LIGHT_SWITCH)
    record = Record()

    async def sample() -> None:
        while True:
            state = hass.states.get(eid)
            record.samples.append(
                Sample(
                    loop.time(),
                    hub.connected and hub._link_end is None,
                    hub._lost_at,
                    state is not None and state.state != STATE_UNAVAILABLE,
                )
            )
            await asyncio.sleep(1.0)

    sampler = asyncio.create_task(sample())
    try:
        await flap(hass, hub, sim_mesh, record)
    finally:
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
    _LOGGER.info(
        "soak: %d nodes, %d connects, %.0f virtual s in %.1f s, commands %s, %s",
        NODES,
        sim_link.connect_count,
        loop.time(),
        time.monotonic() - started,
        record.outcomes,
        sim_mesh.stats(),
    )
    assert sim_link.connect_count >= FLAPS + 1
    assert "applied" in record.outcomes, (
        "no command went through in the grace: nothing of it was tested"
    )
    # every load's state is known once the link held
    loads = [d.address for d in (*hub.devices.lights, *hub.devices.sockets)]
    missing = [f"{a:04X}" for a in loads if hub.states.get(a) is None]
    assert not missing, f"no state for {missing}"
    check_grace(record.samples)
    check_bounds(hass, hub, record)

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
