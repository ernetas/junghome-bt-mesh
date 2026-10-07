"""Battery nodes: keep a sleeping transmitter awake while Home Assistant configures it.

A battery wall transmitter or battery binary-input puck (`BATTERY_PIDS`) sleeps between key presses and answers
nothing then; a key press wakes it for a moment. The app never configures one blind: its detail page runs
`KeepLowPowerDeviceAwake`, an acknowledged LBC Admin Get of the ButtonLayout (`0x5001`, `C2 27 05 01 50`) to the
node every 6 s while no other BLE process runs, and 1 s after one went unanswered it asks again, showing "press the
button to wake it" meanwhile (`docs/android/network-logic.md` §5.4, `docs/gap-analysis/control-and-state.md`
§1.4 / §2.9, `device-settings.md` §3.2).

`KeepAwake.hold` does the same around a Config plan or a property change that addresses such a node: one keep-alive
task per node however many operations hold it, stopped when the last one ends (a task that died is replaced by the
next hold). Like the app's, it stays quiet while the operation itself talks to the node — a Get goes out only once
`KEEP_AWAKE_INTERVAL` passed without a message from it (`JungHomeHub.last_heard`). It can still overlap an operation's
request that is waiting out its retries on a silent node; the replies do not mix: the keep-alive's is matched on the
ButtonLayout id, and the operation's Admin requests on theirs (`PropertyReader._get` / `write`,
`configurator.executor.PlanExecutor._admin_status`; the Config messages have their own opcodes). It does not wake the node: the
operation's first message still has to find it awake, and a node that stays silent is reported as asleep, asking the
user to press one of its keys and run the action again (`mesh_config`, `config_entities`); a lost link is reported as
such. Mains nodes are never held. How long a transmitter stays awake after a key press or a message, and whether it
needs the keep-alive at all during a short change, is unverified on air.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final

from .const import (
    DOMAIN,
    KEEP_AWAKE_INTERVAL,
    LINK_WAIT_STEP,
)
from .jhmesh import messages as M
from .jhmesh.devices import BATTERY_PIDS

if TYPE_CHECKING:
    from .jhmesh.access import AccessMessage
    from .jhmesh.cdb import CDB
    from .protocols import HubPort

_LOGGER = logging.getLogger(__name__)


KEEP_AWAKE_RETRY: Final = 1.0  # ... asked again this many seconds after one went unanswered, as the app does ...
KEEP_AWAKE_TIMEOUT: Final = 3.0  # ... each waiting this long (the app's property-read timeout; the notes give the keep-alive none of its own)


PROPERTY_BUTTON_LAYOUT = (
    0x5001  # the app's keep-alive property: every battery product hosts it
)
_LAYOUT_ID = PROPERTY_BUTTON_LAYOUT.to_bytes(2, "little")


def sleepy_node(cdb: CDB, address: int) -> int | None:
    """Return the unicast of the battery node owning element `address`, None for a mains node or an unknown one."""
    node = cdb.node_by_addr(address)
    return node.unicast if node is not None and node.pid in BATTERY_PIDS else None


def _is_layout(message: AccessMessage) -> bool:
    return message.params[:2] == _LAYOUT_ID


class KeepAwake:
    """The keep-alive tasks of one hub: one per battery node that an operation holds, reference-counted."""

    def __init__(self, hub: HubPort) -> None:
        """Bind to `hub` (its CDB, proxy, `last_heard` and entry); no task until a battery node is held."""
        self.hub = hub
        self._holders: Counter[int] = Counter()
        self._tasks: dict[int, asyncio.Task[None]] = {}

    @asynccontextmanager
    async def hold(self, addresses: Iterable[int]) -> AsyncIterator[None]:
        """Keep the battery nodes owning `addresses` awake for the duration of the block; a no-op for mains nodes."""
        nodes = {
            unicast
            for address in addresses
            if (unicast := sleepy_node(self.hub.cdb, address)) is not None
        }
        # released in `finally`, even when starting a later node's task failed
        held: list[int] = []
        try:
            for unicast in nodes:
                self._holders[unicast] += 1
                held.append(unicast)
                task = self._tasks.get(unicast)
                # none yet, or one that died: start it (again)
                if task is None or task.done():
                    self._tasks[unicast] = self.hub.entry.async_create_background_task(
                        self.hub.hass,
                        self._keep_alive(unicast),
                        f"{DOMAIN} keep {unicast:04X} awake",
                    )
            yield
        finally:
            for unicast in held:
                self._holders[unicast] -= 1
                if not self._holders[unicast]:
                    del self._holders[unicast]
                    if (task := self._tasks.pop(unicast, None)) is not None:
                        task.cancel()

    async def _keep_alive(self, unicast: int) -> None:
        """Ask the node for its ButtonLayout whenever it was quiet for `KEEP_AWAKE_INTERVAL`, until cancelled.

        The operation holding the node sends its first message as the task starts, so the quiet time counts from
        then; an unanswered Get (or one the link could not send) is repeated `KEEP_AWAKE_RETRY` later, the app's.
        Anything else is a bug: logged, and the task ends (the next `hold` starts a new one). While there is no link
        it waits for one rather than sending into "not connected" every `KEEP_AWAKE_RETRY`.
        """
        quiet_since = time.monotonic()
        while True:
            if not self.hub.link_up:
                await self.hub.async_wait_connected(LINK_WAIT_STEP)
                continue
            heard = max(quiet_since, self.hub.last_heard.get(unicast, quiet_since))
            wait = heard + KEEP_AWAKE_INTERVAL - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            try:
                await self.hub.proxy.request(
                    unicast,
                    M.vendor_property_get("admin", PROPERTY_BUTTON_LAYOUT),
                    M.VENDOR_PROPERTY_STATUS_OPCODES["admin"],
                    timeout=KEEP_AWAKE_TIMEOUT,
                    retries=1,
                    expect_cid=M.JUNG_CID,
                    match=_is_layout,
                )
            except (TimeoutError, ConnectionError, OSError) as err:
                _LOGGER.debug(
                    "%04X did not answer the keep-alive Get: %r", unicast, err
                )
                quiet_since = time.monotonic() - KEEP_AWAKE_INTERVAL + KEEP_AWAKE_RETRY
            except Exception:
                _LOGGER.exception("Keep-alive of %04X stopped", unicast)
                return
            else:
                quiet_since = time.monotonic()
