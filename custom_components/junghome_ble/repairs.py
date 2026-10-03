"""Repair flows: the fixable issues of the integration.

`seq_store_lost` (setup refused: an address with history has no usable sequence-number record) and
`pdus_dropped` (the nodes drop our messages as replays) are both fixed the same way: the counter continues past
every number the mesh may have seen (`coordinator.SEQ_SKIP_AHEAD`), then the entry is set up again or the link
is renewed. `iv_index_mismatch`, when Home Assistant's IV index is ahead of the mesh's and it can go back, takes it
back to the mesh's index (`JungHomeHub.async_rewind_iv_index`) and sets the entry up again. `address_shared` (another
client sends from Home Assistant's address) continues past the numbers it was seen with
(`JungHomeHub.async_skip_past_shared`), which lets Home Assistant send again. `plan_interrupted` (a
configuration change cut off by a stop or crash, recorded at the next setup) is a notice: confirming it dismisses it.
`node_clock_wrong` (a node that may run schedules has a wrong clock or zone offset, `node_clocks.py`) sends Time Set
now and asks those nodes again; their answers clear it. Unverified on air.

Review-4 U4-5 added the fixes a user would otherwise look up the way to: `gateway_sync_failed` runs the action
*Sync gateway* (`GatewaySyncFlow`); `address_in_use` moves Home Assistant to the free address it suggests
(`FreeAddressFlow`); `unknown_nodes`, `export_stale`, `key_refresh` and `app_changed` (review-4 U4-6, raised for an
entry set up from a file only) load a new export — fetched again from the gateway with the access the entry holds, or
uploaded for an entry set up from a file (`NewExportFlow`, with the config flow's own steps:
`config_flow.async_fetch_to_store`, `async_take_upload`, `async_replace_export`);
`device_name_rejected` asks for a name the app accepts (`DeviceNameFlow`). Each changes something only once
confirmed, and none touches the sequence numbers: a new address starts its own record by the store's rules.
Unverified on air.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.repairs import (
    ConfirmRepairFlow,
    RepairsFlow,
    RepairsFlowResult,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    TextSelector,
)

from .config_flow import (
    LOAD_ERRORS,
    SOURCE_GATEWAY,
    SOURCE_UPLOAD,
    _discard,
    _FormError,
    _gateway_error_key,
    async_fetch_to_store,
    async_known_pin,
    async_replace_export,
    async_take_upload,
    async_update_and_reload,
    async_validate_stored,
    gateway_entry_data,
    incoming_path,
)
from .const import (
    CONF_CDB_PATH,
    CONF_EXPORT_FILE,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_TOKEN,
    CONF_METADATA_DIR,
    CONF_SOURCE,
    CONF_UNICAST,
    DOMAIN,
    ISSUE_ADDRESS_IN_USE,
    ISSUE_ADDRESS_SHARED,
    ISSUE_APP_CHANGED,
    ISSUE_DEVICE_NAME,
    ISSUE_EXPORT_STALE,
    ISSUE_GATEWAY_SYNC,
    ISSUE_IV_INDEX_MISMATCH,
    ISSUE_KEY_REFRESH,
    ISSUE_NODE_CLOCK_WRONG,
    ISSUE_PDUS_DROPPED,
    ISSUE_PLAN_INTERRUPTED,
    ISSUE_SEQ_STORE_LOST,
    ISSUE_UNKNOWN_NODES,
)
from .coordinator import async_skip_seq_store_ahead, forget_known_mesh
from .gateway_api import (
    GatewayAuthError,
    GatewayCertificateMismatch,
    GatewayError,
    JungHomeGatewayApi,
)
from .jhmesh.cdb import CDB
from .jhmesh.export import RENAME_MAX_LENGTH, InvalidName, check_name
from .services import async_configure

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .mesh_config import MeshConfigurator

# the device name field of `DeviceNameFlow`
FIELD_NAME = "name"
RECONFIGURED = "reconfigure_successful"  # `async_replace_export`'s answer when the entry took the new export


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


class SendTimeFlow(RepairsFlow):
    """Confirm, then send Time Set now and ask the nodes with a wrong clock again (`NodeClocks.async_fix`)."""

    def __init__(self, data: dict[str, Any]) -> None:
        """Remember the issue's data (its entry)."""
        self.issue_data = data

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Show the confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Send the time once confirmed; abort when the entry is gone or has no link.

        The issue stays until the nodes answer with the right time: that is the proof the Time Set reached them.
        """
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
        hub = None if entry is None else getattr(entry, "runtime_data", None)
        if hub is None:
            return self.async_abort(reason="entry_gone")
        if not await hub.clocks.async_fix():
            return self.async_abort(reason="not_connected")
        return self.async_create_entry(data={})


class _IssueFlow(RepairsFlow):
    """A fix flow of one entry's issue: the issue's data names the entry (`entry_id`)."""

    def __init__(self, data: dict[str, Any]) -> None:
        """Remember the issue's data."""
        self.issue_data = data

    def _entry(self) -> ConfigEntry | None:
        return self.hass.config_entries.async_get_entry(
            str(self.issue_data.get("entry_id"))
        )

    def _placeholders(self, **extra: str) -> dict[str, str]:
        """Return the issue's placeholders (its text is the flow's) and `extra`; only `extra` once the issue is gone."""
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
        known = dict(issue.translation_placeholders or {}) if issue is not None else {}
        return {**known, **extra}

    def _done(self) -> RepairsFlowResult:
        """End the flow after a fix went through: the issue goes (the next check raises it again if need be)."""
        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        return self.async_create_entry(data={})


class GatewaySyncFlow(_IssueFlow):
    """`gateway_sync_failed`: confirm, then hand the export to the gateway again, as the action *Sync gateway* does.

    `MeshConfigurator.sync_gateway`, under the actions' lock: it refuses when the gateway holds changes Home Assistant
    has not seen (uploading would erase them), and a failure raises the issue again with its new cause, so the abort
    only says what went wrong. Unverified on air.
    """

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Show the confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Upload once confirmed; abort when the entry is gone or not running, or with the gateway's refusal."""
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders=self._placeholders(),
            )
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")

        async def sync(configurator: MeshConfigurator) -> bool:
            return await configurator.sync_gateway()

        try:
            await async_configure(self.hass, entry.entry_id, sync, needs_link=False)
        except HomeAssistantError as err:
            return self.async_abort(
                reason="sync_failed", description_placeholders={"error": str(err)}
            )
        return self._done()


class FreeAddressFlow(_IssueFlow):
    """`address_in_use`: confirm, then move Home Assistant to the free address the export leaves (`CDB.suggest_unicast`).

    The address is worked out from the export on disk when the flow starts and checked again before it is used. Only
    the entry's address changes, as *Reconfigure → Advanced → Our unicast address* would change it, and the entry is
    set up again from it; the sequence-number store keeps one record per address, so the new one continues its own
    record or starts one by the store's rules (`HAState`): no number is sent twice. Unverified on air.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        """Remember the issue's data; the address is suggested when the flow starts."""
        super().__init__(data)
        self._suggestion: int | None = None

    async def _async_export(self, entry: ConfigEntry) -> CDB | None:
        """Return the entry's export as it is on disk; None when it cannot be read."""
        try:
            return await self.hass.async_add_executor_job(
                CDB.load, Path(entry.data[CONF_CDB_PATH])
            )
        except LOAD_ERRORS:
            return None

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Work out the free address, then show the confirmation."""
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")
        if (cdb := await self._async_export(entry)) is None:
            return self.async_abort(reason="cannot_load")
        self._suggestion = cdb.suggest_unicast()
        if self._suggestion is None:
            return self.async_abort(reason="no_free_address")
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Move to the suggested address once confirmed, if the export still leaves it free."""
        assert self._suggestion is not None
        suggestion = f"{self._suggestion:04X}"
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders=self._placeholders(suggestion=suggestion),
            )
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")
        cdb = await self._async_export(entry)
        if cdb is None:
            return self.async_abort(reason="cannot_load")
        if not cdb.unicast_is_free(self._suggestion):
            return self.async_abort(
                reason="address_taken",
                description_placeholders={"suggestion": suggestion},
            )
        # discovery and the setup know the mesh by the new address from the next setup on
        forget_known_mesh(self.hass, entry.entry_id)
        async_update_and_reload(
            self.hass, entry, {**entry.data, CONF_UNICAST: suggestion}
        )
        return self._done()


class NewExportFlow(_IssueFlow):
    """`unknown_nodes`, `export_stale`, `key_refresh`, `app_changed`: replace the entry's export, as Reconfigure does.

    An entry set up from the gateway fetches it again with the access and the certificate pin it holds (the gateway
    node's report over the mesh first, `config_flow.async_known_pin`) after a confirmation; nothing is learned or
    requested anew here — a changed certificate or a refused token aborts and points to Reconfigure or the
    re-authentication. Any other entry gets an upload form: the file holds every key, so it leaves Home Assistant's
    upload folder before anything is checked, and is deleted unless it passes. The new export goes through the
    config flow's checks and its end (`async_replace_export`: the replaced copy kept, the entry set up again).
    Unverified on air.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        """Remember the issue's data; no file is in flight yet."""
        super().__init__(data)
        self._incoming: Path | None = None

    @callback
    def async_remove(self) -> None:
        """Delete a half-validated export when the flow goes away (finished, aborted or abandoned)."""
        if self._incoming is not None:
            self.hass.async_add_executor_job(_discard, self._incoming)
            self._incoming = None

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Fetch from the gateway for an entry set up from it, else ask for the file."""
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")
        if entry.data.get(CONF_SOURCE) == SOURCE_GATEWAY:
            return await self.async_step_gateway_refetch()
        return await self.async_step_upload()

    async def async_step_gateway_refetch(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Fetch the gateway's export once confirmed; a gateway that cannot be reached leaves the form to retry."""
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")
        host = str(entry.data.get(CONF_GATEWAY_HOST) or "")
        errors: dict[str, str] = {}
        if user_input is not None:
            token = entry.data.get(CONF_GATEWAY_TOKEN)
            known = await async_known_pin(self.hass, entry, host)
            if not (host and token and known):
                return self.async_abort(
                    reason="no_gateway_pin", description_placeholders={"host": host}
                )
            fingerprint, pin_source, _from = known
            api = JungHomeGatewayApi(
                async_get_clientsession(self.hass, verify_ssl=False),
                host,
                fingerprint,
                str(token),
            )
            unicast = str(entry.data[CONF_UNICAST])
            self._incoming = incoming_path(self.hass, self.flow_id)
            try:
                doc, cdb = await async_fetch_to_store(
                    self.hass, api, self._incoming, unicast
                )
            except GatewayCertificateMismatch:
                return self.async_abort(
                    reason="certificate_changed",
                    description_placeholders={"host": host},
                )
            except GatewayAuthError:
                return self.async_abort(
                    reason="token_rejected", description_placeholders={"host": host}
                )
            except GatewayError as err:
                errors = {"base": _gateway_error_key(err)}
            except _FormError as err:
                errors = _base_errors(err)
            else:
                return await self._async_replace(
                    entry, gateway_entry_data(api, unicast, pin_source, doc), cdb
                )
        return self.async_show_form(
            step_id="gateway_refetch",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders=self._placeholders(host=host),
        )

    async def async_step_upload(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Take the uploaded export, check it against the entry's address, then load it."""
        errors: dict[str, str] = {}
        if user_input is not None:
            # out of Home Assistant's upload folder before anything can fail: the file holds every key
            self._incoming = incoming_path(self.hass, self.flow_id)
            try:
                await async_take_upload(
                    self.hass, user_input[CONF_EXPORT_FILE], self._incoming
                )
                entry = self._entry()
                if entry is None:
                    await self.hass.async_add_executor_job(_discard, self._incoming)
                    return self.async_abort(reason="entry_gone")
                unicast = str(entry.data[CONF_UNICAST])
                cdb = await async_validate_stored(self.hass, self._incoming, unicast)
            except _FormError as err:
                errors = _base_errors(err)
            else:
                data = {
                    CONF_SOURCE: SOURCE_UPLOAD,
                    CONF_METADATA_DIR: "",
                    CONF_UNICAST: unicast,
                }
                return await self._async_replace(entry, data, cdb)
        return self.async_show_form(
            step_id="upload",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EXPORT_FILE): FileSelector(
                        FileSelectorConfig(accept=".json,application/json")
                    )
                }
            ),
            errors=errors,
            description_placeholders=self._placeholders(),
        )

    async def _async_replace(
        self, entry: ConfigEntry, data: dict[str, Any], cdb: CDB
    ) -> RepairsFlowResult:
        """Point the entry at the validated export (the config flow's end); abort with its refusal."""
        incoming, self._incoming = self._incoming, None
        reason = await async_replace_export(self.hass, entry, data, cdb, incoming)
        if reason != RECONFIGURED:
            return self.async_abort(reason=reason)
        return self._done()


def _base_errors(err: _FormError) -> dict[str, str]:
    """Return the config flow's errors for the repair's form, which has no address field: as the form's own."""
    return {"base": next(iter(err.errors.values()))}


class DeviceNameFlow(_IssueFlow):
    """`device_name_rejected`: ask for a name the app accepts, then name the device with it in Home Assistant.

    Checked by the app's rules first (`check_name` with the rename sheet's `RENAME_MAX_LENGTH`); the name becomes
    the device's `name_by_user`, which `device_names.async_track_device_names` writes into the app's project as
    every rename — and that write clears the issue. Unverified on air.
    """

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Show the name form."""
        return await self.async_step_name()

    async def async_step_name(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Rename once the name passes; abort when the entry is not running or the device is gone."""
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")
        if entry.state is not ConfigEntryState.LOADED:
            # the rename is followed by the running entry only
            return self.async_abort(reason="not_loaded")
        registry = dr.async_get(self.hass)
        device = registry.async_get(str(self.issue_data.get("device_id")))
        if device is None:
            return self.async_abort(reason="device_gone")
        errors: dict[str, str] = {}
        name = user_input[FIELD_NAME] if user_input else (device.name_by_user or "")
        if user_input is not None:
            try:
                check_name(name, RENAME_MAX_LENGTH)
            except InvalidName as err:
                errors[FIELD_NAME] = f"name_{err.reason}"
            else:
                registry.async_update_device(device.id, name_by_user=name)
                return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="name",
            data_schema=vol.Schema(
                {vol.Required(FIELD_NAME, default=name): TextSelector()}
            ),
            errors=errors,
            description_placeholders=self._placeholders(),
        )


# the fix flow of each fixable issue, by the issue id's prefix (`<ISSUE_*>_<entry id>`)
FIX_FLOWS: dict[str, type[_IssueFlow]] = {
    ISSUE_GATEWAY_SYNC: GatewaySyncFlow,
    ISSUE_ADDRESS_IN_USE: FreeAddressFlow,
    ISSUE_UNKNOWN_NODES: NewExportFlow,
    ISSUE_APP_CHANGED: NewExportFlow,
    ISSUE_EXPORT_STALE: NewExportFlow,
    ISSUE_KEY_REFRESH: NewExportFlow,
    ISSUE_DEVICE_NAME: DeviceNameFlow,
}


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Return the flow fixing `issue_id` (`<ISSUE_*>_<entry id>`)."""
    if issue_id.startswith(f"{ISSUE_PLAN_INTERRUPTED}_"):
        return ConfirmRepairFlow()
    if issue_id.startswith(f"{ISSUE_NODE_CLOCK_WRONG}_"):
        return SendTimeFlow(data or {})
    for prefix, flow in FIX_FLOWS.items():
        if issue_id.startswith(f"{prefix}_"):
            return flow(data or {})
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
