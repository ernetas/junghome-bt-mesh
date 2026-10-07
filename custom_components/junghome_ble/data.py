"""What the integration keeps in `hass.data` beside its entries' hubs, in one typed place.

`jung_data(hass)` returns the run's `JungHomeData` (under `DATA`), made on first use. Each registry keeps its
accessor where it is used — `entry_lock` and `store_lock` here, `configurator.store.plan_journal` and `plan_history`,
`configurator.store.held_scenes`, `model_update.remember_device_rooms`, `schedules.scheduler` — and none of them is
dropped with a hub: a lock, a journal or a store outlives the entry's reloads.

Still under keys of their own: the registries that hold a class of the module that fills them (`seq_store`'s stores
and owners, `identity.VAULT_KEEPERS`, `node_info.NODE_VERSION_STORES`, `properties.reader.READERS`,
`configurator.store.GATEWAY_SYNCS`, `coordinator.KNOWN_MESHES`, `actions.common.CONFIGURATORS`): typed here they
would make this module and that one a cycle again for the type checker; and the few whose key is read as such
(`entity.UPDATE_READS`, `node_info.NODE_VERSIONS`, `configurator.store.UPLOAD_RETRIES`,
`seq_store.SEQ_BACKUP_TOKEN`, which `backup.py` sets and clears as a key).
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.storage import Store

    from .protocols import ScheduleSlots


@dataclass
class JungHomeData:
    """The integration's registries in `hass.data`, by entry id."""

    # one lock per entry, kept across reloads, around everything that works on a hub and may replace it: the service
    # calls (`actions.common._run`) and the unknown-node refresh's reload (`ExportWatch._reload_for_export`)
    entry_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # one lock per entry, kept across reloads, around every operation on its export (`configurator.store.ExportStore`):
    # the configurator a reload makes waits for a plan the one before still runs
    store_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # the plan journal of each entry (`configurator.store.plan_journal`) and its last calls that ran a plan
    # (`configurator.store.plan_history`, memory only)
    plan_journals: dict[str, Store[dict[str, Any]]] = field(default_factory=dict)
    plan_histories: dict[str, deque[dict[str, Any]]] = field(default_factory=dict)
    # the scene numbers a forced `delete_scene` left in a register (`configurator.store.held_scenes`)
    held_scenes: dict[str, Store[dict[str, Any]]] = field(default_factory=dict)
    # each device's room before an export change (`model_update.remember_device_rooms`), until
    # `async_sync_areas` takes it
    rooms_before: dict[str, dict[str, str | None]] = field(default_factory=dict)
    # the entry's JH Scheduler cache (`schedules.scheduler`), read by the clocks (`node_clocks`)
    schedulers: dict[str, ScheduleSlots] = field(default_factory=dict)


DATA: HassKey[JungHomeData] = HassKey(f"{DOMAIN}_data")


def jung_data(hass: HomeAssistant) -> JungHomeData:
    """Return the integration's registries in `hass.data`, made on first use."""
    if (data := hass.data.get(DATA)) is None:
        data = hass.data[DATA] = JungHomeData()
    return data


def entry_lock(hass: HomeAssistant, entry_id: str) -> asyncio.Lock:
    """Return the entry's lock (`JungHomeData.entry_locks`), created on first use."""
    return jung_data(hass).entry_locks.setdefault(entry_id, asyncio.Lock())


def store_lock(hass: HomeAssistant, entry_id: str) -> asyncio.Lock:
    """Return the lock of the entry's export (`JungHomeData.store_locks`), created on first use."""
    return jung_data(hass).store_locks.setdefault(entry_id, asyncio.Lock())
