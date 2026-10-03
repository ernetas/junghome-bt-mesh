"""Repair flows: the fixable issues of the integration.

`seq_store_lost` (setup refused: an address with history has no usable sequence-number record) and
`pdus_dropped` (the nodes drop our messages as replays) are both fixed the same way: the counter continues past
every number the mesh may have seen (`coordinator.SEQ_SKIP_AHEAD`), then the entry is set up again or the link
is renewed. `iv_index_mismatch`, when Home Assistant's IV index is ahead of the mesh's and it can go back, takes it
back to the mesh's index (`JungHomeHub.async_rewind_iv_index`) and sets the entry up again. `address_shared` (another
client sends from Home Assistant's address) continues past the numbers it was seen with
(`JungHomeHub.async_skip_past_shared`), which lets Home Assistant send again. `plan_interrupted` (a
configuration change cut off by a stop or crash, recorded at the next setup) is a notice: confirming it dismisses it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.repairs import (
    ConfirmRepairFlow,
    RepairsFlow,
    RepairsFlowResult,
)
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    ISSUE_ADDRESS_SHARED,
    ISSUE_IV_INDEX_MISMATCH,
    ISSUE_PDUS_DROPPED,
    ISSUE_PLAN_INTERRUPTED,
    ISSUE_SEQ_STORE_LOST,
)
from .coordinator import async_skip_seq_store_ahead

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


class SkipAheadFlow(RepairsFlow):
    """Confirm, then continue the address's sequence numbers past the ones the mesh may know.

    For an IV index ahead of the mesh's (`iv_index_mismatch`): go back to the mesh's index, above every number sent
    since. For another client on the address (`address_shared`): past the numbers it was seen with.
    """

    def __init__(self, kind: str, data: dict[str, Any]) -> None:
        """Remember which issue this fixes (`kind`, an `ISSUE_*` key) and the issue's data."""
        self.kind = kind
        self.issue_data = data

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Show the confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Skip ahead once confirmed; abort when the entry is gone or no longer in the state the issue described."""
        if user_input is None:
            issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders=(
                    issue.translation_placeholders if issue is not None else None
                ),
            )
        entry = self.hass.config_entries.async_get_entry(
            str(self.issue_data.get("entry_id"))
        )
        if entry is None:
            return self.async_abort(reason="entry_gone")
        if self.kind == ISSUE_SEQ_STORE_LOST:
            return await self._async_skip_lost_record(entry)
        hub = getattr(entry, "runtime_data", None)
        if hub is None:
            return self.async_abort(reason="entry_gone")
        if self.kind == ISSUE_IV_INDEX_MISMATCH:
            # the hub checks again that it is still ahead: the mesh may have caught up, or the state moved
            if reason := await hub.async_rewind_iv_index(
                int(self.issue_data["network"])
            ):
                return self.async_abort(reason=reason)
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        elif self.kind == ISSUE_ADDRESS_SHARED:
            await hub.async_skip_past_shared()  # deletes the issue
        else:
            # the issue stays until a device answers: that is the proof the skip was enough
            await hub.async_skip_ahead()
        return self.async_create_entry(data={})

    async def _async_skip_lost_record(self, entry: ConfigEntry) -> RepairsFlowResult:
        """`seq_store_lost`: write the record past every number sent, then set the entry up again.

        Aborted when the floor's write did not land (review-4 S4-7): nothing else was written, the setup stays
        refused, and so the issue stays too — it used to be deleted all the same, leaving nothing to repair from.
        """
        if (
            await async_skip_seq_store_ahead(
                self.hass,
                str(self.issue_data["mesh_uuid"]),
                str(self.issue_data["unicast"]),
            )
            is None
        ):
            return self.async_abort(reason="floor_not_written")
        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Return the flow fixing `issue_id` (`<ISSUE_*>_<entry id>`)."""
    if issue_id.startswith(f"{ISSUE_PLAN_INTERRUPTED}_"):
        return ConfirmRepairFlow()
    kind = next(
        key
        for key in (
            ISSUE_SEQ_STORE_LOST,
            ISSUE_PDUS_DROPPED,
            ISSUE_IV_INDEX_MISMATCH,
            ISSUE_ADDRESS_SHARED,
        )
        if issue_id.startswith(f"{key}_")
    )
    return SkipAheadFlow(kind, data or {})
