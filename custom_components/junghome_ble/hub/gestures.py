"""Button gestures of one hub: clicks, double clicks, holds, dimming holds, repeat suppression and their listeners.

The hub's registered handlers for what keys send (`JungHomeHub._on_onoff_set`, `_on_level_set`, `_on_scene_recall`,
`_on_vendor_property_set`) stay in `coordinator.py`, so they are in `STATUS_HANDLERS` before any platform chains onto
them (`dispatch.chain_status_handler`); they hand the gestures to `ButtonGestures`, which keeps their state: the
clicks held back for the `click_delay` option, the last click per key for double clicks, the holds in progress
with their end timers, the recent copies of each message the firmware sends twice, and the event listeners. The
hub delivers and publishes through it (`JungHomeHub.fire_button`, `add_event_listener`) and ends what is pending
when it stops (`cancel_all`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Final, Protocol

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later

from custom_components.junghome_ble.const import (
    ATTR_REASON,
    DEFAULT_CLICK_DELAY,
    HOLD_END_LINK_LOST,
    HOLD_END_STOPPED,
    HOLD_END_TIMEOUT,
    KEY_EVENT_RELEASE,
    KEY_EVENT_SIDE_DOWN,
    KEY_EVENT_SIDE_UP,
    KEY_EVENTS,
    OPTION_CLICK_DELAY,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.devices import Button
from custom_components.junghome_ble.protocols import HubPort

if TYPE_CHECKING:
    from homeassistant.core import CALLBACK_TYPE

    from custom_components.junghome_ble.jhmesh.client import AccessMessage

    from .link import LinkEnd


class GesturesHub(HubPort, Protocol):
    """What the gestures ask of the hub besides `HubPort`: the end of every link, to end the holds."""

    def async_on_link_loss(self, listener: Callable[[LinkEnd], None]) -> CALLBACK_TYPE:
        """Call `listener` with every link's end; returns the unsubscribe."""


_LOGGER = logging.getLogger(__name__)


DOUBLE_CLICK_WINDOW: Final = 0.5  # seconds between two clicks to report a double click
BUTTON_REPEAT_WINDOW: Final = 3.0  # a vendor button event with a counter seen this recently is the firmware's second copy
# (the sniffer measured the copy spacing of status publications at 0.9-2.3 s, docs/sniffer.md; the counter is per press,
# so a real second press is never mistaken for a copy whatever the window)
# Hold-to-dim. What a rocker wired straight to a dimmer sends while it is held is not captured on air;
# the SIG ways are a Generic Move Set (a delta to start, 0 to stop) or a Generic Delta Set transaction (one TID,
# growing deltas while held, `TID_REPEAT_WINDOW`). A Delta transaction has no stop message: its hold is taken to end
# this many seconds after its last Set (the key's cadence is a guess; the firmware's second copies are dropped first).
DIM_HOLD_QUIET: Final = 1.5
# Every hold — a Move or Delta one above, or a gateway-mode key's vendor `hold_start` — ends at the latest this many
# seconds after it started: a lost stop (a Move 0, a release) used to leave it open, and a
# dim-while-held automation dimming for ever. Fading through the whole range takes a few seconds; nobody holds a key
# this long on purpose. A hold ended without its stop carries `reason` (HOLD_END_REASONS) in its `hold_end`.
DIM_HOLD_MAX: Final = 30.0
TID_REPEAT_WINDOW: Final = 6.0  # repeats of a SIG client message: one transaction (TID) lives 6 s (Mesh Model §3.3.1.2)


# Offset of the TID in the parameters of the client messages a rocker sends (OnOff Set, Scene Recall, Level/Delta/Move Set).
TID_OFFSET = {
    M.GEN_ONOFF_SET: 1,
    M.GEN_ONOFF_SET_UNACK: 1,
    M.SCENE_RECALL: 2,
    M.SCENE_RECALL_UNACK: 2,
    M.GEN_LEVEL_SET: 2,
    M.GEN_LEVEL_SET_UNACK: 2,
    0x8209: 4,
    0x820A: 4,
    0x820B: 2,
    0x820C: 2,
}

EventListener = Callable[[str, dict[str, Any]], None]


@dataclass
class DimHold:
    """A key's dimming hold in progress (`ButtonGestures.dim_hold`): how it started and its end timers.

    `kind` is the message that started it (`move` / `delta`), `tid` its transaction, `direction` `up` / `down`,
    `target` the address the key dims (hex); `quiet` cancels the end of a Delta transaction's hold, `limit` its end
    at DIM_HOLD_MAX.
    """

    kind: str
    tid: int
    direction: str
    target: str
    quiet: Callable[[], None] | None = None
    limit: Callable[[], None] | None = None

    def cancel_quiet(self) -> None:
        """Stop the quiet timer, if one runs."""
        if self.quiet is not None:
            self.quiet()
            self.quiet = None

    def cancel_timers(self) -> None:
        """Stop both end timers."""
        self.cancel_quiet()
        if self.limit is not None:
            self.limit()
            self.limit = None


@dataclass
class KeyHold:
    """A gateway-mode key's hold (`ButtonGestures.button_event`): the side it started on and its end at DIM_HOLD_MAX.

    `ended`: the hold was ended without its release (DIM_HOLD_MAX, the link) and stays here only so that release,
    when it still comes, ends nothing a second time.
    """

    side: str | None
    limit: Callable[[], None] | None = None
    ended: bool = False

    def cancel_limit(self) -> None:
        """Stop the end timer, if one runs."""
        if self.limit is not None:
            self.limit()
            self.limit = None


class ButtonGestures:
    """The button gestures of one hub (module docstring): their state, timers and listeners."""

    def __init__(self, hub: GesturesHub) -> None:
        """Bind to `hub` (its `hass`, devices and link-loss listeners); nothing pending, the option read from its entry."""
        self.hub = hub
        # Option: report a `click` only once a second click can no longer turn it into a double click.
        self.click_delay = bool(
            hub.entry.options.get(OPTION_CLICK_DELAY, DEFAULT_CLICK_DELAY)
        )
        self._delayed_clicks: dict[
            int, tuple[Callable[[], None], int, str | None]
        ] = {}  # element → (cancel the timer, counter and side of the click held back)
        self._button_last_click: dict[
            int, tuple[float, str | None]
        ] = {}  # element → (when, side) of the last click, for double clicks
        self._key_holds: dict[
            int, KeyHold
        ] = {}  # element → the gateway-mode hold in progress, whose side is handed to its release
        self._dim_holds: dict[
            int, DimHold
        ] = {}  # element → the dimming hold in progress
        self._unknown_codes: set[tuple[int, int]] = set()  # (element, code) logged once
        self._button_recent: dict[
            int, list[tuple[int, float]]
        ] = {}  # element → (counter, when) of recent events
        self._sig_recent: dict[
            tuple[int, int, bytes], float
        ] = {}  # (src, opcode, params) → when
        self._event_listeners: dict[int, list[EventListener]] = {}
        hub.async_on_link_loss(self._end_holds_on_link_loss)

    def cancel_all(self) -> None:
        """Drop the clicks held back and end every hold (`HOLD_END_STOPPED`): the hub stops (`JungHomeHub.async_stop`)."""
        for addr in list(self._delayed_clicks):
            self._cancel_delayed_click(addr)
        self._end_holds(HOLD_END_STOPPED)
        self._key_holds.clear()

    def dim_hold(self, m: AccessMessage, p: bytes) -> None:
        """Derive `hold_start` / `hold_end` with a `direction` from a key's Move / Delta Sets.

        The vendor gestures of a gateway-mode key say when a hold starts and ends; a key wired straight to a dimmer
        only sends its Level client's messages. Unverified on air (`DIM_HOLD_QUIET`), from the SIG semantics: a Move
        Set with a delta starts a hold (its sign is the direction: `up` brighter, `down` darker) and a Move Set 0
        ends it; the first Delta Set of a transaction (TID) starts one, the next ones of the same transaction
        continue it, and it ends `DIM_HOLD_QUIET` seconds after the last — or at a Delta Set 0. A new start while a
        hold runs (its stop was lost) ends that hold first. A Level Set moves to a level: no hold. Whatever its kind,
        a hold ends `DIM_HOLD_MAX` seconds after it started, with `reason: timeout` (a lost Move 0
        used to leave it open for good); a Move 0 that still comes then ends nothing.
        """
        if m.opcode in (M.GEN_MOVE_SET, M.GEN_MOVE_SET_UNACK) and len(p) >= 3:
            kind, delta, tid = (
                "move",
                int.from_bytes(p[:2], "little", signed=True),
                p[2],
            )
        elif m.opcode in (M.GEN_DELTA_SET, M.GEN_DELTA_SET_UNACK) and len(p) >= 5:
            kind, delta, tid = (
                "delta",
                int.from_bytes(p[:4], "little", signed=True),
                p[4],
            )
        else:
            return
        addr, hold = m.src, self._dim_holds.get(m.src)
        if delta == 0:
            if hold is not None:
                self._end_dim_hold(addr)
            return
        direction = KEY_EVENT_SIDE_UP if delta > 0 else KEY_EVENT_SIDE_DOWN
        if hold is not None and (hold.kind, hold.tid, hold.direction) == (
            kind,
            tid,
            direction,
        ):
            if kind == "delta":  # the transaction goes on: its end is later
                self._quiet_dim_hold(addr, hold)
            return
        if hold is not None:
            self._end_dim_hold(addr)
        hold = DimHold(kind, tid, direction, f"{m.dst:04X}")
        self._dim_holds[addr] = hold
        if kind == "delta":
            self._quiet_dim_hold(addr, hold)
        hold.limit = async_call_later(
            self.hub.hass, DIM_HOLD_MAX, partial(self._dim_hold_limit, addr)
        )
        self.fire_button(
            addr, "hold_start", {"target": hold.target, "direction": direction}
        )

    def _quiet_dim_hold(self, addr: int, hold: DimHold) -> None:
        """(Re)arm the end of a Delta transaction's hold, `DIM_HOLD_QUIET` seconds from now."""
        hold.cancel_quiet()
        hold.quiet = async_call_later(
            self.hub.hass, DIM_HOLD_QUIET, partial(self._dim_hold_quiet, addr)
        )

    @callback
    def _dim_hold_quiet(self, addr: int, _now: datetime) -> None:
        self._dim_holds[addr].quiet = None
        self._end_dim_hold(addr)

    @callback
    def _dim_hold_limit(self, addr: int, _now: datetime) -> None:
        self._dim_holds[addr].limit = None
        self._end_dim_hold(addr, HOLD_END_TIMEOUT)

    def _end_dim_hold(self, addr: int, reason: str | None = None) -> None:
        """End the dimming hold of `addr`: its `hold_end`, with `reason` when its stop did not come (HOLD_END_REASONS)."""
        hold = self._dim_holds.pop(addr)
        hold.cancel_timers()
        attrs: dict[str, Any] = {"target": hold.target, "direction": hold.direction}
        if reason is not None:
            attrs[ATTR_REASON] = reason
        self.fire_button(addr, "hold_end", attrs)

    def _end_key_hold(self, addr: int, reason: str) -> None:
        """End the gateway-mode hold of `addr` without its release: `hold_end` with its side and `reason`.

        The hold stays known as ended, so the release that may still come is not a second `hold_end`.
        """
        hold = self._key_holds[addr]
        hold.cancel_limit()
        hold.ended = True
        attrs: dict[str, Any] = {ATTR_REASON: reason}
        if hold.side is not None:
            attrs["side"] = hold.side
        self.fire_button(addr, "hold_end", attrs)

    @callback
    def _key_hold_limit(self, addr: int, _now: datetime) -> None:
        self._key_holds[addr].limit = None
        self._end_key_hold(addr, HOLD_END_TIMEOUT)

    def _end_holds(self, reason: str) -> None:
        """End every hold in progress, its `hold_end` saying why (a `reason`, not a silent drop).

        Without a link the stop cannot be heard, and a stopping hub hears nothing more, so a dim-while-held
        automation would otherwise never be told to stop. The `reason` lets it tell this from a real release: the
        key may still be held, and a node wired straight to a dimmer may still be dimming.
        """
        for addr in list(self._dim_holds):
            self._end_dim_hold(addr, reason)
        for addr, hold in list(self._key_holds.items()):
            if not hold.ended:
                self._end_key_hold(addr, reason)

    @callback
    def _end_holds_on_link_loss(self, _end: LinkEnd) -> None:
        self._end_holds(HOLD_END_LINK_LOST)

    def is_button(self, addr: int) -> bool:
        """Whether the element at `addr` is a key."""
        return isinstance(self.hub.devices.by_address.get(addr), Button)

    def from_button(self, m: AccessMessage, p: bytes) -> bool:
        """Whether `m` is a client message from a key element, and not the firmware's second copy of it."""
        return self.is_button(m.src) and not self.is_repeat(m.src, m.opcode, p)

    @callback
    def add_event_listener(self, addr: int, cb: EventListener) -> Callable[[], None]:
        """Register `cb` for button events from `addr`; returns the unsubscribe callable."""
        self._event_listeners.setdefault(addr, []).append(cb)
        return lambda: self._event_listeners[addr].remove(cb)

    @callback
    def fire_button(self, addr: int, event: str, attrs: dict[str, Any]) -> None:
        """Deliver a button event of the element at `addr` to its listeners, then publish it on the bus.

        The bus event (`event.publish_button_event`, through `JungHomeHub.publish_button_event`: this module imports no
        platform) comes from here rather than from the key's event entity, so a disabled entity no longer silences
        the key's device triggers and logbook lines; it follows the listeners, so an automation it
        starts sees the entity's new state.
        """
        for cb in self._event_listeners.get(addr, []):
            cb(event, attrs)
        self.hub.publish_button_event(addr, event, attrs)

    def is_repeat(self, src: int, op: int, p: bytes) -> bool:
        """Second copy of a client message (the firmware publishes everything twice, ~1 s apart, fresh SEQ, same TID).

        The payload is part of the key on purpose: a Delta Set transaction legitimately reuses its TID with growing
        deltas while the key is held, and those must all reach the `dim` listeners.
        """
        offset = TID_OFFSET.get(op)
        if offset is None or len(p) <= offset:
            return False  # no TID to compare
        now = time.monotonic()
        self._sig_recent = {
            k: t for k, t in self._sig_recent.items() if now - t < TID_REPEAT_WINDOW
        }
        key = (src, op, p)
        if key in self._sig_recent:
            return True
        self._sig_recent[key] = now
        return False

    @staticmethod
    def _event_attrs(counter: int, side: str | None) -> dict[str, Any]:
        """Return the attributes of a vendor button event: the counter, plus the rocker side when the code carries one."""
        attrs: dict[str, Any] = {"counter": counter}
        if side is not None:
            attrs["side"] = side
        return attrs

    def button_event(self, addr: int, counter: int, code: int) -> None:
        """Turn a KEY_EVT code into a gesture event (`KEY_EVENTS`), deduplicated and with double clicks derived.

        A rocker half's codes (0-3) carry the side as the `side` attribute; a release (4) ends the hold that
        started on the same element and reports that hold's side, so `hold_start` / `hold_end` pair up. A double
        click is two clicks of the same key, hence of the same side, within DOUBLE_CLICK_WINDOW. A code the
        firmware table does not know is still delivered (as `code_xx`: the key is awake, which the battery sensor
        cares about) and logged once per key and code.
        """
        now = time.monotonic()
        # every event is published twice by the firmware, 1-2 s apart; on a double press the copies interleave with
        # the second press (16 05, 17 05, 16 05, 17 05), so the last counter alone is not enough to spot them
        recent = [
            (c, t)
            for c, t in self._button_recent.get(addr, ())
            if now - t < BUTTON_REPEAT_WINDOW
        ]
        self._button_recent[addr] = recent
        if any(c == counter for c, _ in recent):
            return
        recent.append((counter, now))
        if (known := KEY_EVENTS.get(code)) is None:
            if (addr, code) not in self._unknown_codes:
                self._unknown_codes.add((addr, code))
                _LOGGER.info(
                    "Key %04X sent button event code 0x%02X (counter %d), which is not in the firmware's table; "
                    "further ones are not logged",
                    addr,
                    code,
                    counter,
                )
            name, side = f"code_{code:02x}", None
        else:
            name, side = known
        if name == "hold_start":
            self._start_key_hold(addr, side)
        elif code == KEY_EVENT_RELEASE:
            hold = self._key_holds.pop(addr, None)
            if hold is not None:
                hold.cancel_limit()
                if hold.ended:
                    return  # its hold_end went out already (DIM_HOLD_MAX, the link)
                side = hold.side
        if name == "click":
            last = self._button_last_click.get(addr)
            self._button_last_click[addr] = (now, side)
            if (
                last is not None
                and last[1] == side
                and now - last[0] <= DOUBLE_CLICK_WINDOW
            ):
                # a click held back is the first half of this double click: never reported on its own
                self._cancel_delayed_click(addr)
                self.fire_button(addr, "double_click", self._event_attrs(counter, side))
                return
            # a click of the other half of the rocker is no double click: a click held back is reported first
            self._flush_delayed_click(addr)
            if self.click_delay:
                # hold it back until a second click can no longer follow
                cancel = async_call_later(
                    self.hub.hass,
                    DOUBLE_CLICK_WINDOW,
                    partial(self._delayed_click_due, addr, counter, side),
                )
                self._delayed_clicks[addr] = (cancel, counter, side)
                return
        else:
            # any other gesture ends the wait: the click is reported first, then the gesture, in order
            self._flush_delayed_click(addr)
        self.fire_button(addr, name, self._event_attrs(counter, side))

    def _start_key_hold(self, addr: int, side: str | None) -> None:
        """Note a gateway-mode hold of `addr`, ending at DIM_HOLD_MAX unless its release comes first.

        A hold still open (its release was lost) ends first, with a plain `hold_end`, as a dimming hold does.
        """
        if (running := self._key_holds.pop(addr, None)) is not None:
            running.cancel_limit()
            if not running.ended:
                self._flush_delayed_click(addr)
                attrs: dict[str, Any] = {}
                if running.side is not None:
                    attrs["side"] = running.side
                self.fire_button(addr, "hold_end", attrs)
        self._key_holds[addr] = KeyHold(
            side,
            async_call_later(
                self.hub.hass, DIM_HOLD_MAX, partial(self._key_hold_limit, addr)
            ),
        )

    @callback
    def _delayed_click_due(
        self, addr: int, counter: int, side: str | None, _now: datetime
    ) -> None:
        self._delayed_clicks.pop(addr, None)
        self.fire_button(addr, "click", self._event_attrs(counter, side))

    def _cancel_delayed_click(self, addr: int) -> tuple[int, str | None] | None:
        """Drop the click held back for `addr`, if any; returns its counter and side."""
        if (held := self._delayed_clicks.pop(addr, None)) is None:
            return None
        held[0]()
        return held[1], held[2]

    def _flush_delayed_click(self, addr: int) -> None:
        """Report the click held back for `addr` now, if any."""
        if (held := self._cancel_delayed_click(addr)) is not None:
            self.fire_button(addr, "click", self._event_attrs(*held))
