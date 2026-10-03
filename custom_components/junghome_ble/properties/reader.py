"""The config entities' property reader and cache: reads, writes and the status handlers that keep the values.

A vendor property's value lands in `ElementState.properties` through one status handler for the three vendor
Status opcodes, a SIG setup state's in `ElementState.setup` through another; `PropertyReader` (one per hub,
`property_reader`) asks for them and writes them. Which entities exist is `properties/targets.py`, the entity
classes are `config_entities.py`.

Read / write flow, the app's (`docs/gap-analysis/device-settings.md` §1.2): one acknowledged Get when the entity
is added or enabled and the link is up (never polled), an acknowledged Set on change followed by the Status
reply, or a re-read when nothing answered (a change neither answers nor shows is an error, not a success);
unsolicited Status publications (`C5 / CB / D1 27 05`) are applied as they arrive. Initial reads go through a
platform-level scheduler, `PROPERTY_READ_CHUNK` at a time, so a large installation does not flood the mesh when
the link comes up. A battery node sleeps then: its entities are read right after one of its keys reported. A change
to one keeps it awake the app's way while it runs (`keep_awake.py`), and one it does not answer fails as *asleep*,
asking for a key press first (review-3 W4 / F24).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from custom_components.junghome_ble import const
from custom_components.junghome_ble.const import (
    DOMAIN,
    LINK_WAIT_STEP,
    NODE_INFO,
    NODE_INFO_TIME_ROLE,
    NODE_INFO_UNSUPPORTED,
    NODE_INFO_VENDOR,
    PROPERTY_READ_CHUNK,
    PROPERTY_READ_DELAY,
    PROPERTY_READ_FRESH,
    PROPERTY_READ_PAUSE,
    PROPERTY_READ_RETRIES,
    PROPERTY_REREAD_DELAY,
    SIG_HARDWARE_REVISION,
    SIG_MANUFACTURER_NAME,
    SIG_SOFTWARE_VERSION,
)
from custom_components.junghome_ble.coordinator import (
    JungHomeHub,
    register_status_handler,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import properties as P
from custom_components.junghome_ble.jhmesh.pdu import decode_opcode

from .targets import (
    CTL_DEFAULT,
    GATEWAY_API_STATUS,
    GATEWAY_IP,
    LIGHTNESS_DEFAULT,
    PROPERTY_LOCK,
    PROPERTY_STATUS_LED,
    SETUP_STATES,
    STATUS_LED_PRODUCTS,
    SetupState,
)

if TYPE_CHECKING:
    from datetime import datetime

    from custom_components.junghome_ble.jhmesh.cdb import Node
    from custom_components.junghome_ble.jhmesh.client import AccessMessage
    from custom_components.junghome_ble.jhmesh.properties import PropertySpec

_LOGGER = logging.getLogger(__name__)

# how a write ended: confirmed (the Status answering the Set, or the read-back), the read-back reporting another
# value, neither the Set nor the read-back answered, or the Set answered by a Status without a value (the element
# does not have the property)
WriteOutcome = Literal["applied", "not_applied", "no_answer", "not_supported"]
VendorServer = Literal["admin", "manufacturer", "user"]
TIME_SETUP_SERVER = (
    "1201"  # the model a Time Role Get goes to (`PropertyReader._ask_time_role`)
)
# The room thermostat's automatic operation (0x1246) and its scheduler-function status (0x1249): the app's resolver
# feeds a reported 0x1249 into the same capability as 0x1246 (`resolver/C1952s1.java`, `docs/android/properties.md`
# §1.10), so a 0x1249 Status is cached as 0x1246 too (`_on_vendor_property_status`). Unverified on air.
PROPERTY_SCHEDULER_ENABLED, PROPERTY_SCHEDULER_STATUS = 0x1246, 0x1249

# ----------------------------------------------------------------------------- status handler


def is_secret(pid: int) -> bool:
    """Whether the vendor property is a credential (the gateway's API token, 0xC001): never cached, never shown."""
    spec = P.PROPERTIES.get(pid)
    return (
        spec is not None and isinstance(spec.codec, P.Text) and bool(spec.codec.secret)
    )


def cacheable(pid: int) -> bool:
    """Whether a vendor property Status value may be kept in `ElementState.properties`.

    Only catalogued properties are, and of the gateway's own block (0xC00x: API status, API token, IP, certificate
    fingerprint) only what its entities show, the API status and the IP (`gateway_status_targets`): the gateway
    answers the phone app's reads of the others over the mesh too, the proxy forwards those replies to us, nothing
    here renders them, and the cache ends up in the diagnostics.
    """
    spec = P.PROPERTIES.get(pid)
    return (
        spec is not None
        and not is_secret(pid)
        and (spec.products != P.GATEWAY or pid in (GATEWAY_API_STATUS, GATEWAY_IP))
    )


def redacted(pid: int) -> bool:
    """Whether the diagnostics hide a cached property: a secret, or the gateway's address (the entry's is too)."""
    return is_secret(pid) or pid == GATEWAY_IP


@register_status_handler(
    *M.VENDOR_PROPERTY_STATUS_OPCODES.values(), company_id=M.JUNG_CID
)
def _on_vendor_property_status(hub: JungHomeHub, m: AccessMessage, p: bytes) -> None:
    """Cache the value of a catalogued vendor property Status (`[pid u16][access u8][value…]`), solicited or not.

    A property the catalogue does not know, or a secret (`cacheable`), is left alone: a Status is still counted
    as traffic and answers a pending request either way. So is a Status without a value — the id alone, or the id
    and the access byte — which is how an element answers for a property it does not have (the metering socket's
    meter element to an Admin Set of 0x5003, on air): the app's resolver drops it
    (`StatusMessageResolver` `AbstractC1972z0`, `UtilsKt.k`) and keeps the value it had, so does this. The value is
    the sender's, except for a status LED written as a Status (`status_owner`). A thermostat's 0x1249 is its 0x1246
    as well (`PROPERTY_SCHEDULER_STATUS`).
    """
    if len(p) <= 3:
        return
    pid = int.from_bytes(p[:2], "little")
    if not cacheable(pid):
        return
    owner = status_owner(hub, m, pid)
    st = hub.element_state(owner)
    st.properties[pid] = bytes(p[3:])
    # a load's lock, which its light / socket entity acts on (`LoadLock`)
    if pid == PROPERTY_LOCK:
        st.note_lock(st.properties[pid])
    elif pid == PROPERTY_SCHEDULER_STATUS:
        st.properties[PROPERTY_SCHEDULER_ENABLED] = st.properties[pid]
    hub.notify_update(owner)


def status_owner(hub: JungHomeHub, m: AccessMessage, pid: int) -> int:
    """Return the element whose value the vendor Status `m` of `pid` carries: its sender, or the key it writes.

    The gateway drives a key's status LED with a User Property Status *to* the key element (on air: gateway →
    push-button, `air:access:11-0527:0x5013`; no reply follows), the way `PropertyReader.write_status` does. Such a
    Status is a write: the value is the receiving key's, so its status-LED switch follows what the gateway set.
    Only a User Status of the status LED to a mains push-button's element, from a node other than that push-button
    and not to Home Assistant, counts; every other Status describes its sender.
    """
    if (
        pid == PROPERTY_STATUS_LED
        and m.opcode == M.VENDOR_PROPERTY_STATUS_OPCODES["user"]
        and m.dst != hub.proxy.state.src
    ):
        node = hub.cdb.node_by_addr(m.dst)
        if (
            node is not None
            and node.pid in STATUS_LED_PRODUCTS
            and hub.cdb.node_by_addr(m.src) is not node
        ):
            return m.dst
    return m.src


@register_status_handler(*SETUP_STATES)
def _on_setup_status(hub: JungHomeHub, m: AccessMessage, p: bytes) -> None:
    """Cache a SIG setup-state Status, solicited or published; a CTL Default carries the Lightness Default too.

    The Light CTL Default state's lightness *is* the Light Lightness Default (Mesh Model spec §6.1.3.4): each Status
    updates the other's copy, so a CTL Default Set built from the cache keeps the switch-on brightness set last.
    """
    state = SETUP_STATES[m.opcode]
    if len(p) < state.size:
        return
    setup = hub.element_state(m.src).setup
    setup[state.status] = bytes(p[: state.size])
    if state is CTL_DEFAULT:
        setup[LIGHTNESS_DEFAULT.status] = bytes(p[:2])
    elif state is LIGHTNESS_DEFAULT and (ctl := setup.get(CTL_DEFAULT.status)):
        setup[CTL_DEFAULT.status] = bytes(p[:2]) + ctl[2:]
    hub.notify_update(m.src)


def property_id_of(m: AccessMessage) -> int | None:
    """Return the property id a vendor Status carries, None when it is too short."""
    return int.from_bytes(m.params[:2], "little") if len(m.params) >= 2 else None


def has_value(m: AccessMessage) -> bool:
    """Whether the vendor Status `m` carries a value after its property id and access byte."""
    return len(m.params) > 3


def is_status_of(pid: int, m: AccessMessage) -> bool:
    """Whether the vendor Status `m` carries property `pid` (the `match` of a request for it)."""
    return property_id_of(m) == pid


def applied(spec: PropertySpec, sent: bytes, held: bytes | None) -> bool:
    """Whether the value an element reports shows that a Set of `sent` took: the same bytes.

    A lock only by being locked or not: the time and value it reports are what it keeps, not what was sent (a
    lock of the current state carries no value).
    """
    if held is None:
        return False
    if not isinstance(spec.codec, P.EnforcedOutputCodec):
        return held == sent
    try:
        return spec.codec.decode(held).locked == spec.codec.decode(sent).locked
    except ValueError:
        return False


def vendor_server(spec: PropertySpec) -> VendorServer:
    """Return the LBC server hosting `spec`; config entities exist for vendor properties only."""
    if spec.server in ("admin", "manufacturer", "user"):
        return spec.server
    raise ValueError(f"{spec.name} is not a vendor property")


# ----------------------------------------------------------------------------- reads and writes

READERS: HassKey[dict[str, PropertyReader]] = HassKey(f"{DOMAIN}_property_readers")
Job = Callable[[], Awaitable[None]]


@dataclass(eq=False)
class _Queued:
    """A job in the reader's queue: the element it reads, what it reads (`key`), the link it was queued for.

    `link` is the hub's `link_count` the job was queued on; None for one queued while no link was up, which waits
    for the next link, whichever it is.
    """

    addr: int
    key: object
    link: int | None
    job: Job


class PropertyReader:
    """The mesh side of the config entities of one hub: rate-limited initial reads, serialised per element.

    `schedule` queues a job (an entity's first read) for an element. The worker starts `PROPERTY_READ_DELAY`
    after its first job, so the hub's connect-time state refresh goes first, then works the queue
    `PROPERTY_READ_CHUNK` jobs at a time (to distinct elements, since exchanges with one element are serialised)
    with a `PROPERTY_READ_PAUSE` between chunks, like that refresh. `read`, `write` and `write_status` talk to
    the element, one exchange per element at a time, so a Status is never taken for the answer to another
    property's Get. A property several entities share (the LED colours and the night mode) is read once: a read
    that succeeded within `PROPERTY_READ_FRESH` is not repeated.

    A job is queued once (review-4 R4-5): one still waiting is kept in its place and counted for the current link,
    and one queued on a link that went away is dropped when its turn comes — its entity queues it again on the next
    link if it still wants it. Several quick drops used to leave a copy per link in the queue, each read in turn.
    Unverified on air.

    An entity that rewrites a value other entities write too (an LED colour and the night-mode byte, the three
    fields of an edge-evaluation byte, the two ends of a lightness range) holds `modifying(addr)` from reading the
    current value to its write: two such changes at once would otherwise both start from the same value and the
    second Set would undo the first's.
    """

    def __init__(self, hub: JungHomeHub) -> None:
        """Bind to `hub`; no worker until the first job."""
        self.hub = hub
        self._jobs: deque[_Queued] = deque()
        # (address, key) -> its entry in `_jobs`, while it waits there
        self._queued: dict[tuple[int, object], _Queued] = {}
        self._worker: asyncio.Task[None] | None = None
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # per element, around a whole read-modify-write; `_locks` is taken inside it by each exchange
        self._modify_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._read_at: dict[
            tuple[int, int | str], float
        ] = {}  # (address, property id or setup state name) -> when it last answered
        # node unicast -> `hub.link_count` its version was last queued on (`schedule_version`)
        self._version_link: dict[int, int] = {}
        # node unicast -> when the last read of its node information that got every answer started (`_read_version`)
        self._version_read: dict[int, datetime] = {}
        # load address -> the time limit (s, 0 = none) its lock switch sends, set by its `number` entity
        self.lock_time_limits: dict[int, int] = {}
        # LED element address -> whether LED 1's colours are copied to LED 2 (`switch.JungHomeLedColourSync`)
        self.led_sync: dict[int, bool] = {}

    @callback
    def schedule(self, addr: int, job: Job, *, key: object = None) -> None:
        """Queue `job`, a read of the element at `addr`, and make sure the worker runs.

        `key` names what the job reads (by default the job itself: an entity's bound method equals itself on every
        call). A job with the same address and key still waiting is not queued again: it keeps its place and is
        counted for the current link.
        """
        link = self.hub.link_count if self.hub.connected else None
        ident = (addr, job if key is None else key)
        queued = self._queued.get(ident)
        if queued is not None:
            queued.link, queued.job = link, job
        else:
            self._queued[ident] = item = _Queued(addr, ident[1], link, job)
            self._jobs.append(item)
        if self._worker is None or self._worker.done():
            self._worker = self.hub.entry.async_create_background_task(
                self.hub.hass, self._run(), f"{DOMAIN} property reads"
            )

    def _take_chunk(self) -> list[Job]:
        """Dequeue up to `PROPERTY_READ_CHUNK` jobs for distinct elements, oldest first; drop those of lost links.

        One pass that rebuilds the queue: taking each job out with `deque.remove` cost a scan of the queue per job.
        """
        current = self.hub.link_count
        chunk: list[Job] = []
        addrs: set[int] = set()
        rest: deque[_Queued] = deque()
        for item in self._jobs:
            if item.link is not None and item.link != current:
                del self._queued[item.addr, item.key]  # queued on a link that is gone
            elif len(chunk) < PROPERTY_READ_CHUNK and item.addr not in addrs:
                del self._queued[item.addr, item.key]
                chunk.append(item.job)
                addrs.add(item.addr)
            else:
                rest.append(item)
        self._jobs = rest
        return chunk

    async def _wait_for_setup(self) -> bool:
        """Hold the first reads until every platform has queued its jobs (the entry is LOADED).

        Entities schedule their reads while their platform is being set up; starting the worker before the last
        platform is through would let the first chunk skip elements that are still to come. Returns False when
        the entry never loaded (the reads would go nowhere).
        """
        entry = self.hub.entry
        state = entry.state  # a local: the attribute changes while we wait below
        if state is not ConfigEntryState.SETUP_IN_PROGRESS:
            return state is ConfigEntryState.LOADED
        settled = asyncio.Event()
        unsub = entry.async_on_state_change(
            lambda: (
                settled.set()
                if entry.state is not ConfigEntryState.SETUP_IN_PROGRESS
                else None
            )
        )
        try:
            await settled.wait()
        finally:
            unsub()
        return entry.state is ConfigEntryState.LOADED

    async def _run(self) -> None:
        if not await self._wait_for_setup():
            return
        await asyncio.sleep(PROPERTY_READ_DELAY)
        while self._jobs:
            # no link: wait for one rather than run the queue into "not connected" (each read lost for nothing)
            while not self.hub.connected:
                await self.hub.async_wait_connected(LINK_WAIT_STEP)
            for result in await asyncio.gather(
                *(job() for job in self._take_chunk()), return_exceptions=True
            ):
                if isinstance(result, Exception):
                    _LOGGER.debug("property read failed: %r", result)
            if self._jobs:
                await asyncio.sleep(PROPERTY_READ_PAUSE)

    @callback
    def schedule_version(self, node: Node) -> None:
        """Queue a read of what the node tells about itself (`_read_version`), once per hub and node.

        The firmware gates (`targets.node_version`: illuminance scaling, `targets._candidates`, the thermostat's
        property set) need the software version and nothing else asks for it. The hub keeps what it learns across the
        entry's reloads and on disk (`node_info.NODE_VERSIONS`), so `targets._candidates` — which runs at setup, before
        any read, and again whenever the hub follows a changed export in place (`model_update`) — applies it from then
        on, a restart's included.

        A read that got every answer is not repeated until the hub sees the node restart (`hub.restarted`: a
        firmware update restarts it; review-4 R4-5) — asked on every link, it cost a Get per node at every link-up
        and piled up in the queue when links came and went. One that went unanswered is queued again on the next
        link, at most once per link. Unverified on air.
        """
        unicast = node.unicast
        read = self._version_read.get(unicast)
        restarted = self.hub.restarted.get(unicast)
        if self._version_link.get(unicast) == self.hub.link_count or (
            read is not None and (restarted is None or restarted < read)
        ):
            return
        self._version_link[unicast] = self.hub.link_count
        self.schedule(unicast, partial(self._read_version, node), key="version")

    async def _read_version(self, node: Node) -> None:
        """Ask the node for its software version, then for what else of its node information is not known yet.

        The app reads the identity block (SIG 0x0011, 0x001A, 0x0010) and the time role on every opening of the
        device page (the settings session); here the software version is asked once per hub and restart of the
        node (`schedule_version`) and the rest once for good — a node's hardware revision, manufacturer name and
        LBC version blocks (0x0003 .. 0x0005, `NODE_INFO_VENDOR`) do not change while it keeps its address, and
        every Get is traffic at link-up. The time role is kept the same way: only the device diagnostics show it,
        nothing acts on it, Home Assistant never sets it, and every node on air answered "client". A node that does
        not answer the first Get is not asked the rest on this link. SIG Statuses land in the hub's cache through
        its property handler, the others through `remember_node_info` here.

        An item the node answers without a value (it does not have it) is remembered as not supported under the
        software version it just answered (`NODE_INFO_UNSUPPORTED`), so it is not asked on every link either; a
        firmware update asks again. Silence is not an answer: that item, and the version with it, is asked on the
        next link.
        """
        addr = node.unicast
        started = dt_util.utcnow()
        async with self._locks[addr]:
            version = await self._ask_sig(addr, SIG_SOFTWARE_VERSION)
            if version is None:
                _LOGGER.debug(
                    "%04X did not answer the Get of its software version", addr
                )
                return
            known = self.hub.node_info(addr)
            complete = True

            def wanted(name: str) -> bool:
                return (
                    name not in known
                    and known.get(name + NODE_INFO_UNSUPPORTED) != version
                )

            for pid in (SIG_HARDWARE_REVISION, SIG_MANUFACTURER_NAME):
                name = NODE_INFO[pid]
                if wanted(name):
                    answer = await self._ask_sig(addr, pid)
                    complete = complete and answer is not None
                    if answer == b"":
                        self._unsupported(addr, name, version)
            for pid, name in NODE_INFO_VENDOR.items():
                if wanted(name) and (node.pid or 0) in P.PROPERTIES[pid].products:
                    answer = await self._ask_vendor_info(addr, pid, name)
                    complete = complete and answer is not None
                    if answer == b"":
                        self._unsupported(addr, name, version)
            if NODE_INFO_TIME_ROLE not in known:
                complete = await self._ask_time_role(node) and complete
            if complete:
                self._version_read[addr] = started

    def _unsupported(self, addr: int, name: str, version: bytes) -> None:
        """Remember that the node at `addr` has no item `name` under software version `version` (`_read_version`)."""
        _LOGGER.debug("%04X has no %s (software version %s)", addr, name, version.hex())
        self.hub.remember_node_info(addr, name + NODE_INFO_UNSUPPORTED, version)

    async def _ask(
        self,
        addr: int,
        pdu: bytes,
        opcode: int,
        what: str,
        *,
        cid: int | None = None,
        match: Callable[[AccessMessage], bool] | None = None,
    ) -> AccessMessage | None:
        """Send one Get of the node's information; its answer, None when it stayed silent."""
        try:
            return await self.hub.proxy.request(
                addr,
                pdu,
                opcode,
                timeout=const.PROPERTY_READ_TIMEOUT,
                retries=PROPERTY_READ_RETRIES,
                expect_cid=cid,
                match=match,
            )
        except TimeoutError:
            _LOGGER.debug("%04X did not answer the Get of its %s", addr, what)
            return None

    async def _ask_sig(self, addr: int, pid: int) -> bytes | None:
        """Ask for SIG Manufacturer property `pid` (the hub's handler keeps the answer).

        Returns the value answered (empty: a Status without one), None when the node stayed silent.
        """
        wanted = pid.to_bytes(2, "little")
        reply = await self._ask(
            addr,
            M.generic_property_get("manufacturer", pid),
            M.GEN_MANU_PROP_STATUS,
            NODE_INFO[pid],
            match=lambda m: m.params[:2] == wanted,
        )
        return None if reply is None else bytes(reply.params[3:])

    async def _ask_vendor_info(self, addr: int, pid: int, name: str) -> bytes | None:
        """Ask for LBC Manufacturer property `pid` and keep a value the node answers with as its `name`.

        Returns the value answered (empty: a Status without one), None when the node stayed silent.
        """
        reply = await self._ask(
            addr,
            M.vendor_property_get("manufacturer", pid),
            M.VENDOR_PROPERTY_STATUS_OPCODES["manufacturer"],
            name,
            cid=M.JUNG_CID,
            match=partial(is_status_of, pid),
        )
        if reply is None:
            return None
        value = bytes(reply.params[3:])
        if value:
            self.hub.remember_node_info(addr, name, value)
        return value

    async def _ask_time_role(self, node: Node) -> bool:
        """Send Time Role Get to the node's Time Setup Server (`1201`, every JUNG node has one) and keep its role.

        Returns False when the role is still to be asked: the node stayed silent or answered no role.
        """
        element = next(
            (e for e in node.elements if TIME_SETUP_SERVER in e.models), None
        )
        if element is None:
            return True
        reply = await self._ask(
            element.address, M.time_role_get(), M.TIME_ROLE_STATUS, NODE_INFO_TIME_ROLE
        )
        if reply is None:
            return False
        try:
            M.decode_time_role_status(reply.params)
        except ValueError as err:
            _LOGGER.debug("%04X: %s", element.address, err)
            return False
        self.hub.remember_node_info(node.unicast, NODE_INFO_TIME_ROLE, reply.params[:1])
        return True

    def modifying(self, addr: int) -> asyncio.Lock:
        """Return the lock a read-modify-write of a value of the element at `addr` holds (class docstring)."""
        return self._modify_locks[addr]

    def cached(self, addr: int, spec: PropertySpec) -> bytes | None:
        """Return the cached wire value of the property, None until the element reported it."""
        st = self.hub.states.get(addr)
        return st.properties.get(spec.id) if st else None

    async def read(
        self, addr: int, spec: PropertySpec, *, since: float | None = None
    ) -> bool:
        """Ask the element for the property; True when its value is cached afterwards (fresh, or just answered).

        `since` (a `time.monotonic()`) asks unless the element answered at or after that moment, however recent the
        last read: the value changes on the device's own (a timed lock ends), and several entities showing it that
        want it read back at once (the lock switch, select and wind alarm) share one Get.

        False when the element stayed silent through the attempts or the link went away: the caller keeps the read
        open and asks again on the next link (`PropertyEntity._maybe_read`, the cover's mode read) — a device
        that was asleep, out of range or drowned out by the connect-time traffic usually answers the next time.
        """
        try:
            return await self.fetch(addr, spec, since=since)
        except ConnectionError as err:
            _LOGGER.debug("read of %s from %04X aborted: %s", spec.name, addr, err)
            return False

    async def fetch(
        self, addr: int, spec: PropertySpec, *, since: float | None = None
    ) -> bool:
        """`read`, but a lost link raises `ConnectionError` instead of counting as silence.

        For a change that has to tell the two apart: a battery node that stays silent is asleep, a lost link is not
        (`PropertyEntity.read_current`).
        """
        async with self._locks[addr]:
            read_at = self._read_at.get((addr, spec.id), -1e9)
            fresh = (
                read_at >= since
                if since is not None
                else time.monotonic() - read_at < PROPERTY_READ_FRESH
            )
            if fresh and self.cached(addr, spec) is not None:
                return True
            await self._get(addr, spec)
        return self.cached(addr, spec) is not None

    async def _get(self, addr: int, spec: PropertySpec) -> bool:
        """Send the Get; True when the element answered with the property's Status.

        Only a Status of `spec` answers it: another property's (a battery node's keep-alive, `keep_awake.py`, or a
        late one) is not taken for it.
        """
        server = vendor_server(spec)
        try:
            await self.hub.proxy.request(
                addr,
                M.vendor_property_get(server, spec.id),
                M.VENDOR_PROPERTY_STATUS_OPCODES[server],
                timeout=const.PROPERTY_READ_TIMEOUT,
                retries=PROPERTY_READ_RETRIES,
                expect_cid=M.JUNG_CID,
                match=partial(is_status_of, spec.id),
            )
        except TimeoutError:
            _LOGGER.debug(
                "%04X did not answer the Get of %s (0x%04X)", addr, spec.name, spec.id
            )
            return False
        self._read_at[addr, spec.id] = time.monotonic()
        return True

    async def write(self, addr: int, spec: PropertySpec, value: Any) -> WriteOutcome:
        """Send an acknowledged Set with the encoded `value`; re-read the property when no Status answered it.

        Returns how it ended (`WriteOutcome`): a Status answering the Set counts as applied, whatever value it
        carries; a read-back only when it reports the value sent (`applied`). A Status without a value (`has_value`)
        answers the Set too, as in the app, which neither resends nor reads back then: the element does not have
        the property, and nothing is cached.
        """
        server = vendor_server(spec)
        raw = spec.codec.encode(value)
        pdu = M.vendor_property_set(
            server, spec.id, raw, ack=True, user_access=spec.set_access
        )
        async with self._locks[addr]:
            try:
                reply = await self.hub.proxy.request(
                    addr,
                    pdu,
                    M.VENDOR_PROPERTY_STATUS_OPCODES[server],
                    timeout=const.PROPERTY_WRITE_TIMEOUT,
                    retries=1,
                    expect_cid=M.JUNG_CID,
                    match=partial(is_status_of, spec.id),  # as `_get`
                )
            except TimeoutError:
                reply = None
            if reply is not None and not has_value(reply):
                _LOGGER.debug(
                    "%04X answered the Set of %s without a value: not supported",
                    addr,
                    spec.name,
                )
                return "not_supported"
            if reply is None or property_id_of(reply) != spec.id:
                _LOGGER.debug(
                    "%04X did not confirm the Set of %s; reading it back",
                    addr,
                    spec.name,
                )
                await asyncio.sleep(PROPERTY_REREAD_DELAY)
                if not await self._get(addr, spec):
                    return "no_answer"
                if not applied(spec, raw, self.cached(addr, spec)):
                    return "not_applied"
        return "applied"

    async def write_status(self, addr: int, spec: PropertySpec, value: Any) -> None:
        """Write the property the way the gateway drives the status LED: a User Property Status, no reply."""
        raw = spec.codec.encode(value)
        await self.hub.proxy.send_access(
            addr, M.vendor_property_status("user", spec.id, raw)
        )
        self.hub.element_state(addr).properties[spec.id] = raw
        self.hub.notify_update(addr)

    def cached_setup(self, addr: int, state: SetupState) -> bytes | None:
        """Return the cached Status parameters of the setup state, None until the element reported it."""
        st = self.hub.states.get(addr)
        return st.setup.get(state.status) if st else None

    async def read_setup(
        self, addr: int, state: SetupState, *, since: float | None = None
    ) -> bool:
        """Ask the element for the setup state, like `read` (`since` too): True when it is cached afterwards."""
        async with self._locks[addr]:
            read_at = self._read_at.get((addr, state.name), -1e9)
            fresh = (
                read_at >= since
                if since is not None
                else time.monotonic() - read_at < PROPERTY_READ_FRESH
            )
            if fresh and self.cached_setup(addr, state) is not None:
                return True
            try:
                await self._get_setup(addr, state)
            except ConnectionError as err:
                _LOGGER.debug("read of %s from %04X aborted: %s", state.name, addr, err)
                return False
        return self.cached_setup(addr, state) is not None

    async def _get_setup(self, addr: int, state: SetupState) -> bool:
        """Send the Get; True when the element answered it."""
        try:
            await self.hub.proxy.request(
                addr,
                state.get(),
                state.status,
                timeout=const.PROPERTY_READ_TIMEOUT,
                retries=PROPERTY_READ_RETRIES,
            )
        except TimeoutError:
            _LOGGER.debug("%04X did not answer the Get of its %s", addr, state.name)
            return False
        self._read_at[addr, state.name] = time.monotonic()
        return True

    async def write_setup(
        self, addr: int, state: SetupState, pdu: bytes
    ) -> WriteOutcome:
        """Send an acknowledged setup Set; re-read the state when no Status answered it. Returns how it ended.

        The Lightness Range Set is answered by a *publication* of its Status only (`device-settings.md` §13 q.3);
        a reply is matched on source and opcode, whatever its destination; the CTL Temperature Range Set was not
        answered at all on air (`hidden-features.md` §9), so its outcome is the read-back's. A Status whose status
        code is not Success (a range's Cannot Set Range Min / Max) answers a Set that did not take. A read-back
        counts as applied when the Status repeats the Set's parameters (a range's after its status code).
        """
        async with self._locks[addr]:
            try:
                reply = await self.hub.proxy.request(
                    addr,
                    pdu,
                    state.status,
                    timeout=const.PROPERTY_WRITE_TIMEOUT,
                    retries=1,
                )
            except TimeoutError:
                _LOGGER.debug(
                    "%04X did not confirm the Set of its %s; reading it back",
                    addr,
                    state.name,
                )
            else:
                if state.coded and reply.params[:1] != b"\x00":
                    _LOGGER.debug(
                        "%04X refused the Set of its %s: status code %s",
                        addr,
                        state.name,
                        reply.params[:1].hex() or "missing",
                    )
                    return "not_applied"
                return "applied"
            await asyncio.sleep(PROPERTY_REREAD_DELAY)
            if not await self._get_setup(addr, state):
                return "no_answer"
            _, _, params = decode_opcode(pdu)
            held = self.cached_setup(addr, state) or b""
            return "applied" if held.endswith(params) else "not_applied"


def property_reader(hass: HomeAssistant, hub: JungHomeHub) -> PropertyReader:
    """Return the hub's reader, created on first use and dropped when the entry unloads."""
    readers = hass.data.setdefault(READERS, {})
    entry_id = hub.entry.entry_id
    if entry_id not in readers:
        readers[entry_id] = PropertyReader(hub)

        def forget() -> None:
            readers.pop(entry_id, None)

        hub.entry.async_on_unload(forget)
    return readers[entry_id]


# ----------------------------------------------------------------------------- outcomes and cached values


def check_outcome(
    outcome: WriteOutcome, entity_id: str, *, compare: bool = True
) -> None:
    """Raise the translated error for a write the element did not confirm; `compare`: also for another value."""
    if outcome in ("no_answer", "not_supported") or (
        compare and outcome == "not_applied"
    ):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=f"setting_{outcome}",
            translation_placeholders={"entity": entity_id},
        )


def cached_value(hub: JungHomeHub, addr: int, spec: PropertySpec) -> Any:
    """Return the decoded cached value of `spec` on element `addr`, None when unknown or malformed."""
    st = hub.states.get(addr)
    raw = st.properties.get(spec.id) if st else None
    if raw is None:
        return None
    try:
        return spec.codec.decode(raw)
    except ValueError:
        return None
