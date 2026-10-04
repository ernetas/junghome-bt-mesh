"""The export a configurator plans on: its file, the provisioner identity, the plan journal, the gateway's copy.

`ExportStore` (review-4 brief 55) is the part of `MeshConfigurator` that reads and writes: the export the config
entry points at (read fresh for every change, written atomically, `recorded` / `adopted` for `actions.common._run`),
Home Assistant's provisioner entry merged into it, the plan journal a crash leaves, dry runs, and the gateway's copy —
adopted when only the app changed it, uploaded after every change and retried as the app retries it. The translated
service errors of the configurator are raised from here (`_validation`, `_failure`, `translated`).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections import deque
from collections.abc import Callable, Coroutine, Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Protocol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_GATEWAY_LAST_SYNC,
    CONF_GATEWAY_SYNCED,
    CONF_METADATA_DIR,
    DEFAULT_PROVISIONER_IDENTITY,
    DOMAIN,
    GATEWAY_UPLOAD_RETRIES,
    GATEWAY_UPLOAD_RETRY_DELAY,
    ISSUE_CARRY_OVER_CONFLICT,
    ISSUE_GATEWAY_CERTIFICATE,
    ISSUE_GATEWAY_SYNC,
    ISSUE_GATEWAY_TOKEN,
    OPTION_PROVISIONER_IDENTITY,
    SIGNAL_GATEWAY_SYNCED,
    learn_more_url,
)
from custom_components.junghome_ble.coordinator import issue_id
from custom_components.junghome_ble.data import jung_data
from custom_components.junghome_ble.gateway_api import (
    GatewayAuthError,
    GatewayCertificateMismatch,
    GatewayError,
    GatewayUnreachable,
    JungHomeGatewayApi,
    api_for_entry,
)
from custom_components.junghome_ble.jhmesh.advert import mac_from_uuid
from custom_components.junghome_ble.jhmesh.cdb import CDB, InvalidExport
from custom_components.junghome_ble.jhmesh.export import (
    ExportError,
    InvalidName,
    NewerExportError,
    ProjectFile,
    hexaddr,
    timestamp_advanced,
    write_private,
    write_private_with_backup,
)
from custom_components.junghome_ble.jhmesh.merge import (
    Change,
    apply_changes,
    diff_documents,
)
from custom_components.junghome_ble.jhmesh.vault import RangeError, Ranges

from .plan import PlanError
from .wiring import (
    app_copy_path,
    export_digest,
    held,
    load_project,
    name_error,
    pre_adopt_path,
    shown,
)

if TYPE_CHECKING:
    from custom_components.junghome_ble.coordinator import JungHomeHub
    from custom_components.junghome_ble.jhmesh.plan import ConfigStep
    from custom_components.junghome_ble.protocols import GatewayHost

_LOGGER = logging.getLogger(__name__)


def _validation(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )


def _failure(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )


def _name_error(err: InvalidName, name: str) -> ServiceValidationError:
    """Return the service error for a name the app refuses (blank, a lone `%`, a rename past the sheet's limit)."""
    return _translate(name_error(err, name))


def _translate(err: PlanError) -> ServiceValidationError:
    """Return the translated service error a planner's refusal names."""
    return _validation(err.key, **err.placeholders)


@contextmanager
def translated() -> Iterator[None]:
    """Raise a planner's refusal (`PlanError`) as the translated service error it names, with the same cause."""
    try:
        yield
    except PlanError as err:
        raise _translate(err) from err.__cause__


class _GatewayUnusable(Exception):
    """The gateway is not asked at all: its pin is not vouched for, or it rejected the token (a retry cures neither)."""

    def __init__(self, cause: str, *, token: bool = False) -> None:
        super().__init__(cause)
        self.cause = cause
        self.token = token


class _NoMergeBase(Exception):
    """`_carry_over` was asked to merge but has no base: nothing tells HA's changes from the app's."""


TOKEN_REJECTED = "the gateway no longer accepts Home Assistant's access token"  # noqa: S105 - a log text


def _listed_macs(text: str) -> set[str]:
    """Blocking: the MACs of an export's nodes, parsed as the hub will load it (every node's keys derived)."""
    net, meta = CDB.parse(text)
    return {
        mac
        for node in CDB.from_network(net, meta).nodes
        if (mac := mac_from_uuid(node.uuid)) is not None
    }


def token_rejected_open(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Whether the entry's `gateway_token_rejected` repair is open: raised since Home Assistant started, not cleared.

    An issue raised before a restart comes back from the registry inactive (it is not persistent): it is not open,
    so the first rejection after the restart is reported — and the reauth started — again.
    """
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(entry, ISSUE_GATEWAY_TOKEN)
    )
    return issue is not None and issue.active


PLAN_JOURNAL_VERSION = 1


def plan_journal(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Return the entry's plan journal (`.storage/junghome_ble.<entry id>.plan_journal`), one instance per entry.

    `{"action", "steps", "accepted", "prepare", "happened"}` of the plan being sent (`PlanExecutor.send`);
    gone when no plan's outcome is left unrecorded. It holds Config PDUs and addresses, no key material.
    """
    journals = jung_data(hass).plan_journals
    if entry_id not in journals:
        journals[entry_id] = Store(
            hass, PLAN_JOURNAL_VERSION, f"{DOMAIN}.{entry_id}.plan_journal"
        )
    return journals[entry_id]


GATEWAY_SYNC_VERSION = 1
# s: a burst of syncs (an adopt and its upload) is one write; a record a restart loses is no harm (`_identical`)
GATEWAY_SYNC_SAVE_DELAY = 1.0
GATEWAY_SYNCS: HassKey[dict[str, GatewaySync]] = HassKey(f"{DOMAIN}_gateway_syncs")


class GatewaySync:
    """What Home Assistant last exchanged with an entry's gateway (`.storage/junghome_ble.<entry id>.gateway_sync`).

    `synced`: the content digest (`export_digest`) the gateway and the file last both held; `last_sync`: when Home
    Assistant last uploaded its export (ISO 8601, UTC; the app's `gateway_last_sync`, the *Last export upload*
    sensor, told through `SIGNAL_GATEWAY_SYNCED`). Review-4 H I-10: both lived in `entry.data` up to 1.0.0, so
    every sync rewrote the config entries file and woke every listener of the entry. The first load takes them over
    from there (a new gateway entry's flow seeds `CONF_GATEWAY_SYNCED` the same way); the keys stay in `entry.data`,
    so a downgrade reads the value they had then. A digest, a time, no key material.
    """

    def __init__(
        self, hass: HomeAssistant, entry_id: str, store: Store[dict[str, Any]]
    ) -> None:
        """Bind to the entry's store; nothing is read until `async_load`."""
        self.hass = hass
        self.entry_id = entry_id
        self.store = store
        self.synced: str | None = None
        self.last_sync: str | None = None
        self.loaded = False

    def _data(self) -> dict[str, Any]:
        return {"synced": self.synced, "last_sync": self.last_sync}

    async def async_load(self, entry: ConfigEntry) -> None:
        """Read the record once per Home Assistant run (setup reads it); an entry without one takes `entry.data`'s."""
        if self.loaded:
            return
        data = await self.store.async_load()
        if data is None:
            data = {
                "synced": entry.data.get(CONF_GATEWAY_SYNCED),
                "last_sync": entry.data.get(CONF_GATEWAY_LAST_SYNC),
            }
            if any(v is not None for v in data.values()):
                await self.store.async_save(data)
        self.synced, self.last_sync = data.get("synced"), data.get("last_sync")
        self.loaded = True

    async def async_seed(self, entry: ConfigEntry, digest: str | None) -> None:
        """Record the digest of an export a reconfigure fetched from the gateway (the flow, before the reload)."""
        await self.async_load(entry)
        self.synced = digest
        await self.store.async_save(self._data())

    @callback
    def record(self, digest: str, *, uploaded: bool = False) -> None:
        """Record `digest` as synced; after an upload also its time, which the *Last export upload* sensor hears of."""
        self.synced = digest
        if uploaded:
            self.last_sync = datetime.now(UTC).isoformat()
        self.store.async_delay_save(self._data, GATEWAY_SYNC_SAVE_DELAY)
        if uploaded:
            async_dispatcher_send(
                self.hass, SIGNAL_GATEWAY_SYNCED.format(self.entry_id)
            )


def gateway_sync(hass: HomeAssistant, entry_id: str) -> GatewaySync:
    """Return the entry's `GatewaySync` record, one instance per entry (it outlives the entry's reloads)."""
    records = hass.data.setdefault(GATEWAY_SYNCS, {})
    if entry_id not in records:
        records[entry_id] = GatewaySync(
            hass,
            entry_id,
            Store(hass, GATEWAY_SYNC_VERSION, f"{DOMAIN}.{entry_id}.gateway_sync"),
        )
    return records[entry_id]


async def async_remove_gateway_sync(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the entry's `GatewaySync` record, with the entry."""
    await gateway_sync(hass, entry_id).store.async_remove()
    hass.data[GATEWAY_SYNCS].pop(entry_id, None)


async def run_to_end[T](work: Coroutine[Any, Any, T]) -> T:
    """Await `work` to its end even when the caller is cancelled meanwhile, then pass the cancellation on (D12).

    For what must not stop half-way once the mesh holds a change: the write that records it, the update (or
    reload) that makes the device model follow it. `asyncio.shield` returns at the first cancellation and leaves the work
    running behind the caller's back — past the lock the caller holds; here the caller keeps waiting, as often
    as it is cancelled, and raises `CancelledError` once the work is done (chained to the work's own error,
    should it fail). A cancellation of the work itself (the loop shutting down) ends the wait at once.
    """
    task = asyncio.ensure_future(work)
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError as err:
            cancelled = err
    if cancelled is None:
        return task.result()
    if not task.cancelled() and (error := task.exception()) is not None:
        raise cancelled from error
    raise cancelled


# ------------------------------------------------------------------ outcomes and dry runs (review-4 W I3, W I6, W I7)


@dataclass
class PlanOutcome:
    """What the running call's plans did on the mesh (`PlanExecutor.send`): its response, its error, the logbook.

    `actions.common._run` starts a new one per call; a call can run several plans (`set_threshold` socket by socket, a
    scene's keys before its members), which add up. `steps` are the messages of every plan (node, description: no
    key material), `nodes` the nodes that accepted one, `summary` the logbook line of a call that finished (a
    translation key of the `exceptions` section, `plan_*`, and its placeholders).
    """

    action: str | None = None
    applied: int = 0
    total: int = 0
    nodes: list[int] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    summary: tuple[str, dict[str, str]] | None = None


PLAN_HISTORY_SIZE = 5  # finished or stopped calls the diagnostics show per entry


def plan_history(hass: HomeAssistant, entry_id: str) -> deque[dict[str, Any]]:
    """Return the entry's last calls that ran a plan, oldest first (`actions.common._report_plan`; memory only).

    `{"action", "outcome", "applied", "total", "steps", "error"}`: step texts and an error key, no key material.
    """
    histories = jung_data(hass).plan_histories
    return histories.setdefault(entry_id, deque(maxlen=PLAN_HISTORY_SIZE))


@dataclass
class DryRun:
    """A dry run's plan (`MeshConfigurator.dry_run`): the export it planned on, as read and as the plan leaves it."""

    pf: ProjectFile | None = None
    before: dict[str, Any] | None = None
    steps: list[str] = field(default_factory=list)
    diff: list[dict[str, str]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


class _Planned(Exception):
    """Raised where a dry run's operation would send or write: it unwinds the operation, holding nothing."""


# the dry run of the running task, if any: a context variable, so a call of another task on the same configurator
# (the unknown-node refresh, another entry's action) is never taken for one
_DRY_RUN: ContextVar[DryRun | None] = ContextVar(f"{DOMAIN}_dry_run", default=None)


class ExportStore:
    """The export of one hub's configurator, the plan journal and the gateway's copy; operations hold `lock`."""

    def __init__(self, hub: JungHomeHub) -> None:
        """Bind to `hub`; nothing is loaded until an operation runs."""
        self.hub = hub
        self.lock = asyncio.Lock()
        # whether the running operation wrote the export: `actions.common._run` has the model follow it after a stopped
        # plan that recorded what the mesh accepted, as after a finished one (the device model must follow the file)
        self.recorded = False
        # whether it adopted the gateway's export first: the device model changed even when the change itself
        # then had nothing to do
        self.adopted = False
        # whether the plan journal holds a plan whose outcome the export does not record yet
        self.journaled = False

    @property
    def dry(self) -> bool:
        """Whether the running task's operation is a dry run (`dry_run`): it must send, write and adopt nothing."""
        return _DRY_RUN.get() is not None

    async def dry_run(
        self, operation: Callable[[], Coroutine[Any, Any, Any]]
    ) -> dict[str, Any]:
        """Run `operation` as far as its plan; answer `{"dry_run", "steps", "diff"}` and send, write, adopt nothing.

        The export is read from disk (`load`): the gateway is not asked, so nothing of it is adopted, and Home
        Assistant's provisioner entry is merged into the read copy from a copy of the vault, which is not saved
        either. Where the real run would send its plan or write the export (`PlanExecutor.send`, `save`; a removal's
        reset and a key link's vendor writes before that), the plan is noted — its messages in the order they would go
        out, each with the device it goes to — with how the export would change (`diff_documents`, keys never shown),
        and the operation is unwound. Its checks run as they would: a call the real run refuses is refused. An
        operation with nothing to do answers no steps and no change. With a gateway, the real run plans on the
        gateway's export when the app changed the installation since, which this one does not look at.
        """
        dry = DryRun()
        token = _DRY_RUN.set(dry)
        try:
            await operation()
        except _Planned:
            pass
        finally:
            _DRY_RUN.reset(token)
        return {"dry_run": True, "steps": dry.steps, "diff": dry.diff, **dry.extra}

    def planned(
        self,
        plan: Iterable[ConfigStep] = (),
        *,
        first: Iterable[str] = (),
        then: Iterable[str] = (),
        **extra: Any,
    ) -> NoReturn:
        """End a dry run where the real run would send or write: note the messages and how the export would change.

        `plan` is the Config plan in the order it would go out; `first` / `then` describe what goes out before /
        after it (a node's reset, a key's vendor writes), `extra` joins the answer (a new room's address).
        """
        dry = _DRY_RUN.get()
        assert dry is not None
        assert dry.pf is not None
        assert dry.before is not None
        dry.steps = [
            *first,
            *(f"{self.node_name(step.node)}: {step.what}" for step in plan),
            *then,
        ]
        dry.diff = [
            {
                "path": change.where(),
                "before": shown(change.old, change.path),
                "after": shown(change.new, change.path),
            }
            for change in diff_documents(dry.before, dry.pf.snapshot())
        ]
        dry.extra = extra
        raise _Planned

    async def _load_read_only(self, dry: DryRun) -> ProjectFile:
        """`load` for a dry run: the export on disk, with the provisioner identity merged from a copy of the vault."""
        pf = await self.read()
        vault = self.hub.vault.vault
        if self.identity_enabled and vault is not None:
            try:
                await self.hub.hass.async_add_executor_job(
                    copy.deepcopy(vault).merge_into, pf, self.hub.proxy.state.src
                )
            # the real run says why (`with_identity`); the dry run plans without the entry
            except Exception as err:
                _LOGGER.debug(
                    "Dry run without the provisioner entry: %s", type(err).__name__
                )
        dry.pf, dry.before = pf, pf.snapshot()
        return pf

    def node_name(self, unicast: int) -> str:
        """`0232 (Kitchen)`: a node by its address, with the name of the load at that address or the node's own."""
        device = self.hub.devices.by_address.get(unicast)
        node = self.hub.cdb.node_by_addr(unicast)
        name = (
            device.name
            if device is not None
            else (node.name if node is not None else None)
        )
        return f"{hexaddr(unicast)} ({name})" if name else hexaddr(unicast)

    def member_name(self, element: int) -> str:
        """`0232 (Kitchen)`: a register element by its address, with the name of its load when the hub has one."""
        device = self.hub.devices.by_address.get(element)
        if device is None:
            return hexaddr(element)
        return f"{hexaddr(element)} ({device.name})"

    @property
    def path(self) -> str:
        """The export the config entry points at."""
        return str(self.hub.entry.data[CONF_CDB_PATH])

    async def load(self, *, fresh: bool = False) -> ProjectFile:
        """Read the export a mutation plans against: the gateway's when that is newer, else the copy on disk.

        Every mutation starts here, under the lock. `recorded` and `adopted` are not reset here but per call
        (`actions.common._run`): one call can run several mutations (`set_threshold` wires socket by socket), and a
        later one that fails must not hide the export an earlier one wrote. `fresh`: a gateway entry whose gateway
        did not answer is refused instead of planning on the copy on disk, which may lack what the app made since
        (review-4 W4-3: what judges by what the export *lacks* must not fall back silently). A dry run reads the
        disk alone and writes nothing (`_load_read_only`).
        """
        if (dry := _DRY_RUN.get()) is not None:
            return await self._load_read_only(dry)
        answered = await self._adopt_gateway_export()
        if fresh and not answered and self.gateway is not None:
            raise _failure("service_gateway_export_unavailable")
        pf = await self.read()
        await self.with_identity(pf)
        return pf

    async def read(self) -> ProjectFile:
        """Read the export on disk as it is (`load_project`); a translated error when it does not load."""
        try:
            return await self.hub.hass.async_add_executor_job(
                load_project,
                self.path,
                self.hub.entry.data.get(CONF_METADATA_DIR) or None,
            )
        except InvalidExport as err:
            raise _failure(
                "service_export_load_failed", path=self.path, error=str(err)
            ) from err
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
            ExportError,
        ) as err:
            raise _failure(
                "service_export_load_failed",
                path=self.path,
                error=f"{type(err).__name__}: {err}",
            ) from err

    async def save(self, pf: ProjectFile, *, upload: bool = True) -> None:
        """Write `pf`, then hand it to the gateway; once started, the write runs to its end whatever cancels the call.

        The file is what the mesh holds (D12): a call cancelled half-way through writing it would leave the nodes
        ahead of it; once written, the plan journal is done with. The upload is not held to its end: cancelled,
        skipped while Home Assistant stops (it would hold the shutdown up for a gateway that may not answer) or
        not asked for (`upload=False`), it is left to `sync_gateway` or the next change, which uploads the export
        as it is on disk then. A dry run ends here, before anything is written (`planned`).
        """
        if self.dry:
            self.planned()
        try:
            await run_to_end(self._save_file(pf))
        except asyncio.CancelledError:
            if self.gateway is not None:
                _LOGGER.warning(
                    "%s was written but not handed to the gateway (cancelled); sync_gateway or the next change does",
                    self.path,
                )
            raise
        if self.gateway is None or not upload:
            return
        if self.hub.hass.is_stopping:
            _LOGGER.warning(
                "%s was written but not handed to the gateway: Home Assistant is stopping; sync_gateway or the next "
                "change does",
                self.path,
            )
            return
        await self._upload_or_retry(pf)

    async def _save_file(self, pf: ProjectFile) -> None:
        await self.with_identity(pf)
        await self._write(pf)
        self.recorded = True
        await self.journal_close()

    @property
    def journal(self) -> Store[dict[str, Any]]:
        """The entry's plan journal (`plan_journal`)."""
        return plan_journal(self.hub.hass, self.hub.entry.entry_id)

    async def journal_save(self, data: dict[str, Any]) -> None:
        """Write the plan being sent and how far it got (`PlanExecutor.send`) into the plan journal."""
        # a copy: a save deferred to Home Assistant's final write must not see the next step's count
        await self.journal.async_save(dict(data))
        self.journaled = True

    async def journal_close(self) -> None:
        """Remove the plan journal: the export records the plan's outcome, or the mesh holds nothing of it."""
        if self.journaled:
            # emptied first: `Store` keeps a write still pending (deferred while Home Assistant stops) or data it
            # loaded, and would hand that back to the next load in this run even after the file is gone
            await self.journal.async_save({})
            await self.journal.async_remove()
            self.journaled = False

    async def _write(self, pf: ProjectFile) -> None:
        """Write `pf` to the entry's export (the app's last upload kept first as the merge base, with a gateway)."""
        if self.gateway is not None:
            # the file before Home Assistant's first change is the app's last upload: the merge base from now on
            await self.hub.hass.async_add_executor_job(self._keep_app_copy)
        try:
            await self.hub.hass.async_add_executor_job(pf.save)
        except NewerExportError as err:
            raise _failure("service_export_newer", path=self.path) from err
        except (OSError, ExportError) as err:
            raise _failure(
                "service_export_write_failed", path=self.path, error=str(err)
            ) from err
        _LOGGER.info("Wrote the mesh export %s", self.path)

    @property
    def identity_enabled(self) -> bool:
        """Whether the entry's *provisioner identity* option is on (off by default: nothing of it reaches a file)."""
        return bool(
            self.hub.entry.options.get(
                OPTION_PROVISIONER_IDENTITY, DEFAULT_PROVISIONER_IDENTITY
            )
        )

    async def with_identity(self, pf: ProjectFile) -> bool:
        """With the option on, put Home Assistant's provisioner entry, its node and the vault's nodes into `pf`.

        `jhmesh.vault.Vault.merge_into`, idempotent: True when `pf` changed. Groups and scenes of `pf` are then
        allocated in Home Assistant's ranges. A file no range fits (Home Assistant's address taken, a space full)
        or that would not load with them is left as it is, with a warning — the change it is part of goes on. The
        vault is saved when the choice of ranges or its nodes changed. With the option off nothing happens here.
        """
        if not self.identity_enabled:
            return False
        keeper = self.hub.vault
        vault = keeper.identity()
        try:
            result = await self.hub.hass.async_add_executor_job(
                vault.merge_into, pf, self.hub.proxy.state.src
            )
        except (RangeError, InvalidExport) as err:
            _LOGGER.warning(
                "Home Assistant's provisioner entry was left out of %s: %s",
                self.path,
                err,
            )
            return False
        except Exception as err:
            # `merge_into` put the file back as it was; the action stops here rather than write half a merge.
            # Only the type is logged: a malformed stored entry may carry a device key
            _LOGGER.error(
                "Merging Home Assistant's provisioner entry into %s failed (%s)",
                self.path,
                type(err).__name__,
            )
            raise _failure(
                "provisioner_identity_failed", error=type(err).__name__
            ) from err
        for uuid in result.stale:
            vault.forget(uuid)
            _LOGGER.info(
                "The export holds node %s in another shape than Home Assistant recorded it (removed or provisioned "
                "anew); the vault's copy is dropped",
                uuid,
            )
        for uuid, why in result.skipped.items():
            _LOGGER.warning(
                "The node %s Home Assistant provisioned was not put back into %s: %s",
                uuid,
                self.path,
                why,
            )
        await keeper.async_save()
        return result.changed

    async def _identity_text(self, text: str) -> tuple[str, int]:
        """`with_identity` on an export's text (an adopted gateway export): (the text, 1 when it changed, else 0)."""
        try:
            pf = await self.hub.hass.async_add_executor_job(
                ProjectFile.loads, text.encode()
            )
        except (InvalidExport, ValueError, KeyError, TypeError, AttributeError) as err:
            _LOGGER.warning(
                "Home Assistant's provisioner entry was not added to the gateway's export: %s",
                err,
            )
            return text, 0
        if not await self.with_identity(pf):
            return text, 0
        pf.touch()
        return pf.render(), 1

    async def async_identity_ranges(self) -> Ranges:
        """Home Assistant's ranges in the export on disk, chosen now if need be (and kept in the vault).

        For `add_device` with the option on: the new node goes into Home Assistant's unicast range and its element
        groups into its group range. A translated error when no range fits.
        """
        async with self.lock:
            pf = await self.read()
            keeper = self.hub.vault
            try:
                ranges = await self.hub.hass.async_add_executor_job(
                    keeper.identity().ensure_ranges, pf, self.hub.proxy.state.src
                )
            except RangeError as err:
                raise _failure("provisioner_identity_no_range", error=str(err)) from err
            await keeper.async_save()
            return ranges

    def _keep_app_copy(self) -> None:
        """Blocking: copy the export on disk to `app_copy_path` unless one is kept already.

        Called right before a save, after `load` read the file: it exists.
        """
        target = app_copy_path(self.path)
        if not target.exists():
            write_private(target, Path(self.path).read_bytes())

    def _keep_pre_adopt_copy(self) -> None:
        """Blocking: copy the export on disk to `pre_adopt_path`, right before an adopted gateway export replaces it.

        `_gateway_state` digested the file just before: it exists (an `OSError` otherwise fails the adoption).
        """
        write_private(pre_adopt_path(self.path), Path(self.path).read_bytes())

    def _carry_over(
        self, text: str, required: bool = False
    ) -> tuple[str, int, list[Change]]:
        """Blocking: put Home Assistant's changes onto the gateway's export `text`; (result, applied, conflicts).

        Review-3 W1: the app uploads its whole project after every change but never downloads one, so its upload
        lacks what Home Assistant changed since — which the nodes still hold. The changes are the difference
        between the app's previous upload (`app_copy_path`) and the file on disk; `jhmesh.merge` applies them
        to the new upload, and a change the app overrode meanwhile (its Config messages went out later) is left
        as the app has it and reported. Without a kept copy, or with an unreadable one, the upload is taken as
        it is (the behaviour before the merge existed) — unless `required` (the file changed since the last
        sync, too): then `_NoMergeBase`, and the caller refuses rather than drop HA's changes.
        """
        base_path = app_copy_path(self.path)
        try:
            base = ProjectFile.load(base_path)
            ours = ProjectFile.load(Path(self.path))
            theirs = ProjectFile.loads(text.encode())
        except FileNotFoundError as err:
            if required:
                raise _NoMergeBase from err
            return text, 0, []
        except (
            InvalidExport,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
            ExportError,
        ) as err:
            _LOGGER.warning(
                "Home Assistant's changes were not carried over onto the gateway's export: %s",
                err,
            )
            if required:
                raise _NoMergeBase from err
            return text, 0, []
        changes = diff_documents(base.snapshot(), ours.snapshot())
        if not changes:
            return text, 0, []
        doc = {"network": theirs.net, "meta": theirs.meta}
        before = theirs.snapshot()
        applied, conflicts = apply_changes(doc, changes)
        if theirs.snapshot() == before:
            return text, 0, conflicts
        theirs.touch()
        return theirs.render(), len(applied), conflicts

    @property
    def gateway(self) -> JungHomeGatewayApi | None:
        """The entry's gateway client, or None when the entry was not set up from a gateway."""
        return api_for_entry(self.hub.hass, self.hub.entry)

    async def _upload_or_retry(self, pf: ProjectFile) -> None:
        """Hand `pf` to the gateway after a change; a failure is tried again in the background, as the app does.

        The app retries a failed upload twice, 15 s apart (`ProjectFileSyncServiceImpl`: flow `retry(2)`, delay
        15 000 ms), whatever failed. So does this, for the failures a later attempt can fix: the gateway could not
        be asked or refused the POST (unreachable, busy with another configuration request, an HTTP error). A
        refusal of ours is final: a gateway holding changes HA has not seen, a pin it contradicts, a rejected
        token — the repairs say what to do. A later change, or `sync_gateway`, supersedes a pending retry.

        The retry is Home Assistant's task, not the entry's, and kept in `hass.data` by entry id: a change the hub
        cannot follow in place reloads the entry right after (`actions.common._run`), which replaces the hub and this
        configurator while the retry waits, and would cancel a task of the entry's.
        """
        self.cancel_upload_retry()
        if await self.upload(pf, raise_on_failure=False) == "failed":
            hass, entry_id = self.hub.hass, self.hub.entry.entry_id
            hass.data.setdefault(UPLOAD_RETRIES, {})[entry_id] = (
                hass.async_create_background_task(
                    _retry_upload(hass, entry_id, GATEWAY_UPLOAD_RETRIES),
                    f"{DOMAIN} gateway upload retry",
                )
            )

    @property
    def upload_retry(self) -> asyncio.Task[None] | None:
        """The entry's pending retry of a failed automatic upload, if any (`_upload_or_retry`)."""
        return self.hub.hass.data.get(UPLOAD_RETRIES, {}).get(self.hub.entry.entry_id)

    def cancel_upload_retry(self) -> None:
        """Drop the entry's pending retry of a failed upload: a newer upload supersedes it."""
        cancel_upload_retry(self.hub.hass, self.hub.entry.entry_id)

    async def upload(  # noqa: PLR0911  # one outcome per check
        self, pf: ProjectFile, *, raise_on_failure: bool
    ) -> Literal["synced", "failed", "refused"]:
        """Hand the export to the gateway, after checking it still holds what HA last synced (or what the file holds).

        The gateway rebuilds its whole installation from what is POSTed, so uploading over a change the app made
        since would erase it — the check that guards `_adopt_gateway_export` runs again here, right before the
        POST, because time passes between planning and saving. Nothing goes to a gateway whose pin the gateway
        node has not vouched for (`JungHomeHub.async_gateway_distrust`), nor with a token it rejected. Returns
        (unless `raise_on_failure` raised) "synced", "failed" when trying again later may work (the gateway could
        not be asked, or refused the POST), or "refused" when it cannot (`_upload_or_retry`).
        """
        try:
            state = await self._gateway_state()
        except _GatewayUnusable as err:
            api = self.gateway
            assert api is not None
            if err.token:
                if raise_on_failure:
                    raise _failure(
                        "service_gateway_token_rejected", host=api.host
                    ) from err
                return "refused"
            self._sync_refused(
                api,
                err.cause,
                raise_on_failure,
                "service_gateway_sync_failed",
                host=api.host,
                error=err.cause,
            )
            return "refused"
        api = (
            self.gateway
        )  # after the check: it may have followed the gateway to a new address
        assert api is not None
        if state is None:
            self._sync_refused(
                api,
                "the gateway could not be checked",
                raise_on_failure,
                "service_gateway_sync_failed",
                host=api.host,
                error="the gateway could not be checked",
            )
            return "failed"
        _text, stamp, gateway_digest, disk_digest = state
        if gateway_digest is not None and gateway_digest == disk_digest:
            self._identical(gateway_digest)
        elif gateway_digest != self._synced_digest(disk_digest):
            disk_stamp = await self._disk_timestamp_or_none()
            self._sync_refused(
                api,
                "the gateway holds changes Home Assistant has not seen; fetch the export again",
                raise_on_failure,
                "service_gateway_export_newer",
                host=api.host,
                gateway=stamp,
                file=disk_stamp or "no timestamp",
            )
            return "refused"
        doc = json.loads(pf.share_json())
        try:
            await api.upload_project(doc)
        except GatewayAuthError as err:
            self.report_token_rejected(api)
            if raise_on_failure:
                raise _failure("service_gateway_token_rejected", host=api.host) from err
            return "refused"
        except GatewayError as err:
            _LOGGER.warning(
                "The changed mesh export could not be handed to the gateway %s: %s — the app will show the old "
                "state until an upload succeeds (a change's is tried again twice, or `junghome_ble.sync_gateway`)",
                api.host,
                err,
            )
            ir.async_create_issue(
                self.hub.hass,
                DOMAIN,
                issue_id(self.hub.entry, ISSUE_GATEWAY_SYNC),
                is_fixable=True,  # its repair runs `sync_gateway` (`repairs.GatewaySyncFlow`)
                data={"entry_id": self.hub.entry.entry_id},
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_GATEWAY_SYNC,
                learn_more_url=learn_more_url(ISSUE_GATEWAY_SYNC),
                translation_placeholders={"host": api.host, "error": str(err)},
            )
            if raise_on_failure:
                raise _failure(
                    "service_gateway_sync_failed", host=api.host, error=str(err)
                ) from err
            return "failed"
        # the per-entry issue, and the one issue of every entry before it was per entry
        for issue in (issue_id(self.hub.entry, ISSUE_GATEWAY_SYNC), ISSUE_GATEWAY_SYNC):
            ir.async_delete_issue(self.hub.hass, DOMAIN, issue)
        uploaded_digest = export_digest(doc)
        assert uploaded_digest is not None  # `pf.share_json()` always carries `meta`
        self._mark_synced(uploaded_digest, uploaded=True)
        _LOGGER.info("Handed the mesh export to the gateway %s", api.host)
        return "synced"

    def _sync_refused(
        self,
        api: JungHomeGatewayApi,
        cause: str,
        raise_on_failure: bool,
        key: str,
        **placeholders: str,
    ) -> None:
        """Raise the repair issue for an upload that did not go out, and the translated error when asked to."""
        _LOGGER.warning(
            "The changed mesh export was not handed to the gateway %s: %s",
            api.host,
            cause,
        )
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            issue_id(self.hub.entry, ISSUE_GATEWAY_SYNC),
            is_fixable=True,
            data={"entry_id": self.hub.entry.entry_id},
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_GATEWAY_SYNC,
            learn_more_url=learn_more_url(ISSUE_GATEWAY_SYNC),
            translation_placeholders={"host": api.host, "error": cause},
        )
        if raise_on_failure:
            raise _failure(key, **placeholders)

    def report_token_rejected(self, api: GatewayHost) -> None:
        """Raise the repair for a token the gateway rejects, and start Home Assistant's reauthentication.

        Logged, and the reauth flow started, once per outage — while the repair is open: a change's check,
        `sync_gateway` and the status polls all end up here (and Home Assistant starts no second reauth flow while
        one is in progress). Not `ConfigEntryAuthFailed`: that would stop the entry, and only the gateway sync
        needs the token — the mesh keeps working. The reauth flow's success clears the repair. A reload aborts the
        flow; the set-up entry starts it again while the repair is open (`__init__.async_setup_entry`).
        """
        hass, entry = self.hub.hass, self.hub.entry
        first = not token_rejected_open(hass, entry)
        if first:
            _LOGGER.warning(
                "The gateway %s no longer accepts Home Assistant's access token: the export is not handed to it "
                "until access is granted again (Home Assistant asks for it: Settings → Devices & services)",
                api.host,
            )
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id(entry, ISSUE_GATEWAY_TOKEN),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_GATEWAY_TOKEN,
            learn_more_url=learn_more_url(ISSUE_GATEWAY_TOKEN),
            translation_placeholders={"host": api.host, "title": entry.title},
        )
        if first:
            entry.async_start_reauth(hass)

    async def _gateway_state(self) -> tuple[str, str, str | None, str | None] | None:
        """(text, stamp, gateway digest, disk digest) of the gateway's export against what is on disk.

        None when the gateway cannot be asked, its export does not parse, or belongs to another mesh
        (`_gateway_export` already logs why); `_GatewayUnusable` when it must not be asked. A legacy entry
        (nothing synced yet) gets a baseline here: if the gateway is not ahead by the old timestamp rule, nothing
        has changed as far as HA can tell, so today's digest becomes the baseline; otherwise the digest stays
        unset for this call, and `_synced_digest` falls back to the disk digest, which treats the whole
        difference as the gateway's change alone — the best a legacy entry can do without history.
        """
        await self._sync.async_load(self.hub.entry)
        fetched = await self._gateway_export()
        if fetched is None:
            return None
        text, stamp = fetched
        gateway_digest = export_digest(json.loads(text))
        try:
            disk_digest = await self.hub.hass.async_add_executor_job(
                self._digest_on_disk
            )
        except OSError:
            return None  # unreadable file: not a judgeable "changed" — `read` explains it, next
        if gateway_digest is not None and self._sync.synced is None:
            try:
                on_disk_stamp = await self.hub.hass.async_add_executor_job(
                    self._timestamp_on_disk
                )
            except OSError:
                on_disk_stamp = None
            newer = bool(stamp) and (
                on_disk_stamp is None or timestamp_advanced(stamp, on_disk_stamp)
            )
            if not newer:
                self._mark_synced(gateway_digest)
        return text, stamp, gateway_digest, disk_digest

    @property
    def _sync(self) -> GatewaySync:
        """The entry's record of what it last exchanged with the gateway (`_gateway_state` loads it)."""
        return gateway_sync(self.hub.hass, self.hub.entry.entry_id)

    def _synced_digest(self, disk_digest: str | None) -> str | None:
        """Return what HA last synced with the gateway; a legacy entry with nothing recorded falls back to the disk digest."""
        synced = self._sync.synced
        return synced if synced is not None else disk_digest

    def _digest_on_disk(self) -> str | None:
        """Blocking: the digest of the export on disk (None when it parses but carries no `meta`).

        Raises `OSError` for a file that cannot even be read — `_gateway_state`'s caller lets that fail the
        whole check rather than judge "changed" from nothing, the same way a failed gateway fetch does.
        """
        try:
            return export_digest(json.loads(Path(self.path).read_bytes()))
        except ValueError:
            return None

    def _mark_synced(self, digest: str, *, uploaded: bool = False) -> None:
        """Record the digest HA last exchanged with the gateway in the entry's `GatewaySync` record, not `entry.data`.

        After an upload also its time (what the app keeps as `gateway_last_sync` and the *Last export upload* sensor
        shows); an adopted gateway export is no upload and leaves it.
        """
        self._sync.record(digest, uploaded=uploaded)

    def _identical(self, digest: str) -> None:
        """Record the export both the gateway and the file hold as synced, whatever the record says (review-4 S4-6).

        An upload whose record was lost (Home Assistant stopped between the POST and the record's delayed save)
        left the record behind both copies: judged by it, every later change was refused as "both changed".
        """
        if self._sync.synced != digest:
            _LOGGER.info(
                "The gateway holds the export on disk: recorded as synced with it"
            )
            self._mark_synced(digest)

    async def _gateway_export(self) -> tuple[str, str] | None:
        """Ask the gateway for this mesh's export: (text, CDB timestamp), or None when there is nothing usable.

        None when the entry has no gateway, the gateway cannot be asked (logged; the upload after the change
        will raise the repair issue and `sync_gateway` asks again), what it hands out does not parse or is
        another mesh's export. `_GatewayUnusable` when it must not be asked: its pin is not vouched for, it
        presents another certificate (the `gateway_certificate_changed` repair), or it rejects the token (the
        `gateway_token_rejected` repair). An answer clears both repairs: the pinned gateway took the token.
        """
        api = self.gateway
        if api is None:
            return None
        if (distrust := await self.hub.async_gateway_distrust()) is not None:
            raise _GatewayUnusable(distrust)
        try:
            try:
                doc = await api.fetch_project()
            except (GatewayUnreachable, GatewayCertificateMismatch):
                # moved to another address: the gateway node says where it is now
                if not await self.hub.async_follow_gateway():
                    raise
                followed = self.gateway
                assert (
                    followed is not None
                )  # the entry still names the gateway it followed
                api = followed
                doc = await api.fetch_project()
        except GatewayAuthError as err:
            self.report_token_rejected(api)
            raise _GatewayUnusable(TOKEN_REJECTED, token=True) from err
        except GatewayCertificateMismatch as err:
            self.hub.async_raise_certificate_issue()
            raise _GatewayUnusable(
                "it presents another certificate than the pinned one"
            ) from err
        except GatewayError as err:
            _LOGGER.warning(
                "Could not ask the gateway %s for its export: %s", api.host, err
            )
            return None
        for issue in (ISSUE_GATEWAY_TOKEN, ISSUE_GATEWAY_CERTIFICATE):
            ir.async_delete_issue(
                self.hub.hass, DOMAIN, issue_id(self.hub.entry, issue)
            )
        text = json.dumps(doc)
        try:
            net, _meta = CDB.parse(text)
            mesh_uuid = str(net.get("meshUUID", ""))
        except (InvalidExport, ValueError, KeyError, TypeError, AttributeError) as err:
            _LOGGER.warning(
                "The gateway %s handed out an export that does not parse: %s",
                api.host,
                err,
            )
            return None
        if mesh_uuid.lower() != self.hub.cdb.mesh_uuid.lower():
            _LOGGER.warning(
                "The gateway %s holds the export of another mesh (%s); not used",
                api.host,
                mesh_uuid,
            )
            return None
        return text, str(net.get("timestamp", ""))

    def _timestamp_on_disk(self) -> str | None:
        """Blocking: the CDB timestamp of the export on disk (None when the bytes are not an export)."""
        return ProjectFile.file_timestamp(Path(self.path).read_bytes())

    async def _disk_timestamp_or_none(self) -> str | None:
        """`_timestamp_on_disk`, for a "both changed" error message only: an unreadable file names no timestamp."""
        try:
            return await self.hub.hass.async_add_executor_job(self._timestamp_on_disk)
        except OSError:
            return None

    async def _adopt_gateway_export(self) -> bool:
        """Replace the copy on disk by the gateway's export when it, and only it, changed since HA last synced.

        The app uploads its project to the gateway after every change (network-features.md §8.2) and the file of
        an entry set up from a gateway is written by nobody else, so a gateway digest that differs from what HA
        last synced means the app added, renamed or linked something HA does not know yet. Planning on HA's copy
        and uploading the result would make the gateway "rebuild its device database" without those changes — and
        the gateway would then serve an export without them. Adopting first keeps them; the plan is then
        made on the gateway's copy (the hub's device model follows it after the change). When HA's
        own copy changed too (an earlier upload never reached the gateway), HA's changes are carried over onto it
        all the same (`_adopt`); only without the app's previous upload to tell them apart is the change refused
        until the entry is fetched again. A gateway that must not be asked is not: the plan is
        made on disk, and the upload after the change reports why it did not go out. True when the copy on disk
        now holds what the gateway's export holds: adopted, or the gateway unchanged since HA last synced; False
        without a gateway, when it was not asked or did not answer, and for a bare database left beside a bare
        file (nothing tells what either lacks).
        """
        try:
            state = await self._gateway_state()
        except _GatewayUnusable as err:
            _LOGGER.debug("Planning on the copy on disk: %s", err.cause)
            return False
        if state is None:
            return (
                False  # the upload after the change decides later, from its own check
            )
        if await self._adopt(state, "before the change"):
            self.recorded = self.adopted = True
            return True
        return state[2] is not None

    async def _adopt(
        self,
        state: tuple[str, str, str | None, str | None],
        purpose: str,
        *,
        bare: bool = False,
    ) -> bool:
        """Write the gateway's export (`_gateway_state`), HA's changes carried over, over the copy on disk; True when written.

        Only when the gateway changed since HA last synced, and not into what the file holds already (identical
        content is recorded as synced). When the file changed too — an upload that never reached the gateway —
        HA's changes are carried over all the same, as when only the gateway changed (review-4 S4-6: refusing
        left fetching again as the only way out, which dropped them): the app's previous upload (`app_copy_path`)
        tells them apart, and a change the app overrode is reported (`_report_conflicts`); without that copy the
        change is refused (`service_gateway_export_newer`). A gateway export without `meta` (the bare
        `/project/cdb` database) never replaces a share export; over a file without one either it is taken only
        when `bare` (the unknown-node refresh — a change plans on such a file as it is). The file is written
        atomically with its backups — the copy it replaces kept apart as well (`pre_adopt_path`) — and the digest
        recorded as synced. Unverified on air: no app has imported a file merged this way yet.
        """
        text, stamp, gateway_digest, disk_digest = state
        api = self.gateway
        assert api is not None  # `_gateway_state` is None without one
        both_changed = False
        if gateway_digest is None:
            if disk_digest is None and not bare:
                return False  # the disk copy has nothing worth protecting either; plan on it as today
            if disk_digest is not None:
                _LOGGER.warning(
                    "The gateway %s answered with the bare device database only (no device names or room "
                    "links); not adopted over the share export on disk",
                    api.host,
                )
                raise _failure("service_gateway_export_incomplete", host=api.host)
        elif disk_digest is not None:
            # the disk holds a real export: compare by content against what HA last synced with the gateway
            synced = self._synced_digest(disk_digest)
            if gateway_digest == disk_digest:
                self._identical(gateway_digest)
                return False  # the same export on both sides: plan on disk
            if gateway_digest == synced:
                return False  # unchanged: plan on disk
            both_changed = disk_digest != synced
        # disk_digest is None: the file on disk is not a real export (first run, or something else overwrote it)
        # while the gateway's is — the opposite of an unreadable file, where preferring the gateway is unsafe
        disk_stamp = (
            await self._disk_timestamp_or_none()
        )  # for the log and a refusal; read before the write below
        try:
            merged, carried, conflicts = await self.hub.hass.async_add_executor_job(
                self._carry_over, text, both_changed
            )
        except _NoMergeBase as err:
            # HA's own copy changed too, and nothing tells which of its differences are its own changes
            raise _failure(
                "service_gateway_export_newer",
                host=api.host,
                gateway=stamp,
                file=disk_stamp or "no timestamp",
            ) from err
        if self.identity_enabled:
            merged, identity_added = await self._identity_text(merged)
            carried += identity_added
        try:
            await self.hub.hass.async_add_executor_job(self._keep_pre_adopt_copy)
            await self.hub.hass.async_add_executor_job(
                write_private_with_backup, Path(self.path), merged.encode()
            )
            # the app's own upload is the base the next one is compared with
            await self.hub.hass.async_add_executor_job(
                write_private, app_copy_path(self.path), text.encode()
            )
        except OSError as err:
            raise _failure(
                "service_export_write_failed", path=self.path, error=str(err)
            ) from err
        if gateway_digest is not None:
            self._mark_synced(gateway_digest)
        _LOGGER.info(
            "The gateway %s holds a newer export (%s) than the copy on disk (%s): adopted it %s",
            api.host,
            stamp,
            disk_stamp or "no timestamp",
            purpose,
        )
        self._report_conflicts(conflicts)
        if carried:
            _LOGGER.info(
                "Kept %d change(s) Home Assistant made that the app's export lacks; handing the result to the gateway",
                carried,
            )
            await self._upload_or_retry(await self.read())
        return True

    def _report_conflicts(self, conflicts: list[Change]) -> None:
        """Raise the `carry_over_conflict` repair for an adopt that kept the app's version over Home Assistant's.

        Review-4 W4-5: such an adopt used to be a log line only, while the file lost what the nodes still hold (a
        room Home Assistant created is gone from the export, its members still subscribed to it). An adopt
        without conflicts clears the repair. The paths also go into the issue's data, for diagnostics.
        """
        issue = issue_id(self.hub.entry, ISSUE_CARRY_OVER_CONFLICT)
        if not conflicts:
            ir.async_delete_issue(self.hub.hass, DOMAIN, issue)
            return
        paths = [c.where() for c in conflicts]
        _LOGGER.warning(
            "The app changed what Home Assistant had changed too; the app's version is kept for: %s",
            ", ".join(paths),
        )
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            issue,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_CARRY_OVER_CONFLICT,
            learn_more_url=learn_more_url(ISSUE_CARRY_OVER_CONFLICT),
            translation_placeholders={
                "title": self.hub.entry.title,
                "paths": ", ".join(paths),
                "held": "; ".join(
                    f"{where}: {held(c)}"
                    for where, c in zip(paths, conflicts, strict=True)
                ),
            },
            data={"paths": "\n".join(paths)},
        )

    async def adopt_for_unknown_nodes(self, macs: Sequence[str]) -> list[str]:
        """Adopt the gateway's export when it lists nodes of `macs` (this mesh's unknown nodes); return those.

        The unknown-node refresh (`ExportWatch._refresh_export_from_gateway`), on the path of every gateway
        write here: under the lock, fetched by `_gateway_state` (this mesh's export, from a gateway whose pin is
        vouched for) and written by `_adopt` — only when the gateway alone changed since HA last synced, never a
        bare `/project/cdb` database over a share export, with the `.bak` and the synced digest recorded (a later
        change would otherwise take the gateway for ahead of HA). [] when nothing was adopted, which is logged;
        nothing is raised — nobody asked for this.
        """
        async with self.lock:
            try:
                state = await self._gateway_state()
            except _GatewayUnusable as err:
                _LOGGER.warning(
                    "The gateway's export was not fetched for the unknown node(s) %s: %s",
                    ", ".join(macs),
                    err.cause,
                )
                return []
            if state is None:
                return []  # `_gateway_export` said why
            try:
                listed = await self.hub.hass.async_add_executor_job(
                    _listed_macs, state[0]
                )
            except (
                InvalidExport,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
            ) as err:
                _LOGGER.warning(
                    "The gateway handed out an export that does not parse: %s", err
                )
                return []
            found = [mac for mac in macs if mac in listed]
            if not found:
                _LOGGER.info(
                    "The gateway's export does not list the unknown node(s) %s either",
                    ", ".join(macs),
                )
                return []
            try:
                adopted = await self._adopt(state, f"for {', '.join(found)}", bare=True)
            except HomeAssistantError as err:
                _LOGGER.warning(
                    "The gateway's export lists %s but was not adopted: %s",
                    ", ".join(found),
                    err.translation_key,
                )
                return []
            if not adopted:
                _LOGGER.info(
                    "The gateway's export lists %s, but nothing Home Assistant has not synced already",
                    ", ".join(found),
                )
            return found if adopted else []

    async def adopt_if_gateway_changed(self, *, raise_errors: bool = False) -> bool:
        """Adopt the gateway's export when it changed since Home Assistant last synced; True when the file was written.

        Following the app (`app_follow.AppFollower`, review-4 U4-6): after the phone went quiet on the mesh, every
        `GATEWAY_SYNC_PERIOD`, and from the *Fetch export from gateway* button. One GET under the lock, with every
        guard of `adopt_for_unknown_nodes`: nothing while the `gateway_token_rejected` repair is open, nothing from a
        gateway whose pin is not vouched for or that presents another certificate (`_gateway_state`; its repair points
        to Reconfigure, a rejected token to the re-authentication — nothing is accepted or registered anew here), an
        unchanged digest or one only Home Assistant's file moved from writes nothing, and both changed is merged as
        `_adopt` does (refused without the app's previous upload). Never a bare `/project/cdb` database over a share
        export. Logged, not raised, unless `raise_errors` (the button): then a translated error says why nothing was
        adopted.
        """
        api = self.gateway
        if api is None:
            if raise_errors:
                raise _validation("service_no_gateway")
            return False
        async with self.lock:
            cause: str | None = None
            state = None
            if token_rejected_open(self.hub.hass, self.hub.entry):
                cause = TOKEN_REJECTED
            else:
                try:
                    state = await self._gateway_state()
                except _GatewayUnusable as err:
                    cause = err.cause
            if cause is not None:
                _LOGGER.info(
                    "The gateway %s was not asked for its export: %s", api.host, cause
                )
                if raise_errors:
                    raise _failure("gateway_fetch_refused", host=api.host, error=cause)
                return False
            if state is None:
                if raise_errors:  # `_gateway_export` logged why
                    raise _failure("service_gateway_export_unavailable")
                return False
            try:
                adopted = await self._adopt(state, "to follow the app")
            except HomeAssistantError as err:
                _LOGGER.warning(
                    "The gateway's export was not adopted: %s", err.translation_key
                )
                if raise_errors:
                    raise
                return False
            if not adopted:
                _LOGGER.debug(
                    "The gateway's export holds nothing Home Assistant has not synced"
                )
            return adopted

    async def async_current_export(self) -> ProjectFile:
        """Return the export as a change would plan on it now, for a plan made outside the configurator (`add_device`).

        What `load` reads, under the lock: the gateway's export when only it changed since Home Assistant last
        synced (adopted — `recorded` / `adopted` then tell `actions.common._run` to follow it even if the call fails
        later), refused when both sides changed, else the copy on disk; with the provisioner identity on, the vault's
        nodes merged in. The running hub's CDB is the export as the hub last took it over, which misses what the app
        added since.
        """
        async with self.lock:
            return await self.load()

    async def async_export(self, flavour: str) -> dict[str, Any]:
        """Return the export on disk rendered as `flavour` (`share` / `cdb`), with its mesh and timestamp."""
        async with self.lock:
            pf = await self.read()
            await self.with_identity(pf)  # as the next save would write it
        rendered = pf.render("share" if flavour == "share" else "cdb")
        return {
            "flavour": flavour,
            "mesh_uuid": pf.cdb.mesh_uuid,
            "timestamp": pf.loaded_timestamp,
            "export": json.loads(rendered),
        }

    async def sync_gateway(self) -> bool:
        """Upload the export the entry points at to the gateway, as it is on disk (the retry of a failed sync).

        Refused when the gateway holds changes Home Assistant has not seen: uploading would erase them (the
        gateway rebuilds its installation from what is POSTed), and nothing is merged here — the next change
        takes the gateway's export over and carries HA's changes onto it (`_adopt`). The same export on both
        sides is no refusal (`_identical`).
        """
        if self.gateway is None:
            raise _validation("service_no_gateway")
        async with self.lock:
            pf = await self.read()
            if await self.with_identity(pf):
                # the file first (`ProjectFile.save` bumps its timestamp like every writer), so the gateway gets
                # what is on disk: the synced digest compares the two
                await self._write(pf)
            self.cancel_upload_retry()  # this upload supersedes it
            await self.upload(pf, raise_on_failure=True)
            return False


# the pending retry of each entry's failed automatic upload (`ExportStore._upload_or_retry`), by entry id
UPLOAD_RETRIES: HassKey[dict[str, asyncio.Task[None]]] = HassKey(
    f"{DOMAIN}_upload_retry"
)
RELOAD_POLL = 1.0  # seconds between looks at an entry a retry found mid-reload


class _Retrying(Protocol):
    """What a retry uses of the entry's configurator of the moment (`mesh_config.MeshConfigurator`)."""

    @property
    def lock(self) -> asyncio.Lock:
        """The configurator's lock: one operation at a time."""

    @property
    def store(self) -> ExportStore:
        """The configurator's export."""


async def _retry_upload(hass: HomeAssistant, entry_id: str, left: int) -> None:
    """Upload the entry's export again every `GATEWAY_UPLOAD_RETRY_DELAY` seconds, `left` times at most.

    Each attempt goes through the entry's configurator of the moment (a reload after the change may have replaced
    the one that failed), under its lock, with the export as it is on disk then: every later save cancels this before it
    uploads its own, so that is still the changed export. An attempt that comes while the entry is being set up
    or reloaded (its setup lock held) waits for that; an entry not loaded after all (unloaded, disabled, its
    setup failed) or an export that does not load ends the retries.
    """
    try:
        for attempt in range(1, left + 1):
            await asyncio.sleep(GATEWAY_UPLOAD_RETRY_DELAY)
            entry = hass.config_entries.async_get_entry(entry_id)
            while entry is not None and entry.setup_lock.locked():
                await asyncio.sleep(RELOAD_POLL)
                entry = hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.state is not ConfigEntryState.LOADED:
                _LOGGER.info(
                    "Not handing the mesh export to the gateway again: the entry is not loaded"
                )
                break
            configurator: _Retrying = entry.runtime_data.configurator
            async with configurator.lock:
                _LOGGER.info(
                    "Handing the mesh export to the gateway again (retry %d of %d)",
                    attempt,
                    left,
                )
                try:
                    pf = await configurator.store.read()
                except HomeAssistantError as err:
                    _LOGGER.warning(
                        "Not handing the mesh export to the gateway again: %s", err
                    )
                    break
                if (
                    await configurator.store.upload(pf, raise_on_failure=False)
                    != "failed"
                ):
                    break
    finally:
        retries = hass.data.get(UPLOAD_RETRIES, {})
        if retries.get(entry_id) is asyncio.current_task():
            del retries[entry_id]


@callback
def cancel_upload_retry(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the entry's pending retry of a failed upload (a newer upload supersedes it, or the entry is removed)."""
    if (task := hass.data.get(UPLOAD_RETRIES, {}).pop(entry_id, None)) is not None:
        task.cancel()
