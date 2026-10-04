"""Backup platform: mark the sequence-number records while Home Assistant takes a backup.

A restored backup brings `.storage/junghome_ble.seq.<mesh uuid>`, its `.backup` copy and the repair's `.floor` back
together, readable, and nothing in them could tell that numbers were sent since: the next start resumed below them
and reused AES-CCM nonces under the mesh's keys. `async_pre_backup` therefore marks every record with a token of
this backup and waits until both copies on disk carry it, so the archive holds only marked records;
`async_post_backup` removes the mark again. A start that finds a mark this process did not set
(`JungHomeHub.async_create`) continues SEQ_SKIP_AHEAD past the record — 2^20 of the 2^24 numbers of an IV index, once
per restore — and so does the start after a Home Assistant that stopped between the two hooks. Unverified on air: a
backup taken and restored on this installation, with the lights answering at once afterwards, is the check.

Supervisor backups run the same hooks: the Supervisor calls Home Assistant's `backup/start` and `backup/end` before
and after it archives the configuration directory (read from Home Assistant's source; unverified on air).

What is not covered: a mesh whose entry was not loaded during this run of Home Assistant (no `HAState`, nothing to
mark: its records come back unmarked); a backup whose writes did not land within BACKUP_WRITE_TIMEOUT (logged; the
backup is never blocked); and the same backup restored twice, or an older one after a newer one — nothing outside
the restored files survives a restore, so the second start continues from the same point as the first and repeats
what that one sent.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import TYPE_CHECKING

from .const import DOMAIN
from .coordinator import SEQ_BACKUP_TOKEN, SEQ_OWNERS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import HAState

_LOGGER = logging.getLogger(__name__)

# How long the hooks wait for both copies of every mesh's store to be written (seconds); a backup is never held
# back longer than this
BACKUP_WRITE_TIMEOUT = 10.0


async def async_pre_backup(hass: HomeAssistant) -> None:
    """Mark every mesh's records with a fresh token and wait (bounded) until both copies on disk carry it."""
    token = secrets.token_hex(16)
    hass.data[SEQ_BACKUP_TOKEN] = token
    owners = list(hass.data.get(SEQ_OWNERS, {}).values())
    for state in owners:
        state.backup_token = token
    await _async_save_all(hass, owners)
    unmarked = [state for state in owners if not state.carries_backup_token(token)]
    if unmarked:
        _LOGGER.warning(
            "The sequence-number store of %s was not written before the backup: a restore of this backup can "
            "repeat sequence numbers sent after it",
            ", ".join(f"address {state.src:04X}" for state in unmarked),
        )


async def async_post_backup(hass: HomeAssistant) -> None:
    """Remove the mark again: the records written from now on are not in the archive."""
    hass.data.pop(SEQ_BACKUP_TOKEN, None)
    owners = list(hass.data.get(SEQ_OWNERS, {}).values())
    for state in owners:
        state.backup_token = None
    await _async_save_all(hass, owners)


async def _async_save_all(hass: HomeAssistant, owners: list[HAState]) -> None:
    """Write every owner's two copies now; wait at most BACKUP_WRITE_TIMEOUT for them (they go on afterwards)."""
    if not owners:
        return
    saves = [
        hass.async_create_task(state.async_save_now(), f"{DOMAIN} seq store backup")
        for state in owners
    ]
    await asyncio.wait(saves, timeout=BACKUP_WRITE_TIMEOUT)
