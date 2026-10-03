"""The devices Home Assistant added, carried through the app's key refresh (review-4 D11; `jhmesh.vaultrefresh`).

The app hands a new NetKey only to the devices of its own database; one Home Assistant added (`onboard.py`) is not
in it and would be cut off when the refresh completes. `VaultKeyRefresh` (`hub.vault_refresh`) takes every device
of the vault (`identity.py`) along once the followed refresh is *proven* (`jhmesh.keyrefresh`): NetKey Update at
Phase 1, Key Refresh Phase Set 2 at Phase 2, Phase Set 3 once the refresh is proven complete — each step under the
device's own key, each only once the one before it was confirmed. It runs in the background whenever the followed
refresh moves (`JungHomeHub._on_key_refresh`) and on every new link, so a device that was off or out of range is
taken up later; what each device confirmed is kept in the vault.

A device that did not confirm the end of the refresh once Home Assistant reached it raises the repair issue
`vault_key_refresh_lagging` naming its address; the next link tries again, and the issue clears once every device
confirmed (or was forgotten). Nothing is sent without a vault device, nor before the refresh is proven: it is in
effect off unless `add_device` (itself behind an option, off by default) was used. Unverified on air.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ISSUE_VAULT_KEY_REFRESH, learn_more_url
from .jhmesh.vaultrefresh import carry, target_of, wanted

if TYPE_CHECKING:
    from .coordinator import JungHomeHub

_LOGGER = logging.getLogger(__name__)

# seconds to wait for a device's status, per attempt (three attempts), as for any Config request
REQUEST_TIMEOUT = 3.0


class VaultKeyRefresh:
    """Takes the vault's devices as far through the followed key refresh as it is proven (see the module docstring)."""

    def __init__(self, hub: JungHomeHub, issue: str) -> None:
        """Work for `hub`; `issue` is the id of its `vault_key_refresh_lagging` repair issue."""
        self._hub = hub
        self._issue = issue
        # the pass running, if any (`JungHomeHub.async_stop` cancels it)
        self.task: asyncio.Task[None] | None = None
        self._running = False  # set by the pass itself: a task started eagerly runs before `task` is assigned
        self._again = False
        # devices that did not confirm the end of the refresh on the last pass that reached them
        self.lagging: set[int] = set()

    @callback
    def schedule(self) -> None:
        """Run a pass in the background, unless the vault holds no device; one running already runs once more."""
        vault = self._hub.vault.vault
        if vault is None or not vault.nodes:
            return
        if self._running:
            self._again = True
            return
        self.task = self._hub.entry.async_create_background_task(
            self._hub.hass, self._run(), f"{DOMAIN} vault key refresh"
        )

    async def _run(self) -> None:
        self._running = True
        try:
            while True:
                self._again = False
                try:
                    await self._pass()
                except ConnectionError as err:
                    _LOGGER.debug("vault key refresh stopped with the link: %s", err)
                    return
                if not self._again:
                    return
        finally:
            self._running = False

    async def _pass(self) -> None:
        """Take every vault device towards where the refresh is proven to be, one confirmed step after the other."""
        hub = self._hub
        proxy = hub.proxy
        vault = hub.vault.vault
        if vault is None or not proxy.connected:
            return
        target = target_of(proxy)
        current = proxy.nk.key
        lagging: set[int] = set()
        for node in sorted(vault.nodes.values(), key=lambda n: n.unicast):
            want = wanted(target, current, node)
            if want is None:
                continue
            done = await carry(
                proxy, node, want, timeout=REQUEST_TIMEOUT, on_progress=self._save
            )
            if done is False and want.phase == 3:
                lagging.add(node.unicast)
        await self._save()
        self.lagging = lagging
        self.update_issue()

    async def _save(self) -> None:
        """Keep what the devices confirmed; a vault that cannot be written is logged (never a key) and carried on."""
        try:
            await self._hub.vault.async_save()
        except Exception as err:
            _LOGGER.warning(
                "The key refresh progress of the devices Home Assistant added could not be kept in the vault (%s)",
                type(err).__name__,
            )

    @callback
    def update_issue(self) -> None:
        """Raise the repair issue for the lagging devices still in the vault; clear it when there is none."""
        vault = self._hub.vault.vault
        known = (
            {n.unicast for n in vault.nodes.values()} if vault is not None else set()
        )
        lagging = sorted(self.lagging & known)
        if not lagging:
            ir.async_delete_issue(self._hub.hass, DOMAIN, self._issue)
            return
        _LOGGER.warning(
            "The device(s) Home Assistant added at %s did not confirm the new network key: they are cut off "
            "until they do (tried again with every new link)",
            ", ".join(f"{a:04X}" for a in lagging),
        )
        ir.async_create_issue(
            self._hub.hass,
            DOMAIN,
            self._issue,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_VAULT_KEY_REFRESH,
            learn_more_url=learn_more_url(ISSUE_VAULT_KEY_REFRESH),
            translation_placeholders={
                "title": self._hub.entry.title,
                "addresses": ", ".join(f"{a:04X}" for a in lagging),
            },
        )

    def diagnostics(self) -> dict[str, Any]:
        """Where the followed refresh is, how far it is proven and each vault device's phase (no keys)."""
        proxy = self._hub.proxy
        target = target_of(proxy)
        vault = self._hub.vault.vault
        nodes = vault.nodes.values() if vault is not None else []
        return {
            "phase": proxy.key_refresh_phase,
            "proven_phase": None if target is None else target.phase,
            "vault_nodes": {
                f"{n.unicast:04X}": None
                if n.key_refresh is None
                else {
                    "network_id": n.key_refresh.network_id.hex(),
                    "phase": n.key_refresh.phase,
                }
                for n in sorted(nodes, key=lambda n: n.unicast)
            },
            "lagging": [f"{a:04X}" for a in sorted(self.lagging)],
        }
