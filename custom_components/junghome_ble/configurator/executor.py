"""Sending a plan: Config steps one by one, apply-and-record on a stop, the journal a crash leaves, the replies.

`PlanExecutor` sends what a planner built — additive steps first, battery nodes first and kept
awake — through the hub's proxy link, judges every Status, and when the plan stops (a refusal, silence, a lost link, a
cancellation) records the accepted steps into a fresh copy of the export through the `ExportStore`. Before a plan that
removes or overwrites what the export says a node holds, it reads that from the nodes and compares (`preflight`). It
also holds the requests that wait for an answer outside a Config plan (the keys' LBC Admin properties, scene registers
and actions) and their timeouts, and counts what the running call's plans did (`outcome`). `Operations` is what
every group of operations (rooms, keys, scenes, thresholds, nodes) starts from.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from custom_components.junghome_ble.const import (
    DOMAIN,
    ISSUE_PLAN_INTERRUPTED,
    learn_more_url,
)
from custom_components.junghome_ble.coordinator import issue_id
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import vendor_models as V
from custom_components.junghome_ble.jhmesh.export import hexaddr
from custom_components.junghome_ble.jhmesh.plan import (
    Check,
    ConfigStep,
    Difference,
    element_of,
    ordered,
    preflight_checks,
    replay,
)
from custom_components.junghome_ble.keep_awake import sleepy_node

from .plan import (
    APPLIED_KEY_WIRED,
    APPLIED_NOTHING,
    Applied,
    Note,
    _step_from_json,
    _step_json,
    applied_text,
)
from .store import ExportStore, PlanOutcome, _failure, applied_message, run_to_end
from .wiring import _confirms_property, _confirms_scene_action, record_room_link

if TYPE_CHECKING:
    from custom_components.junghome_ble.coordinator import JungHomeHub
    from custom_components.junghome_ble.jhmesh.client import AccessMessage
    from custom_components.junghome_ble.jhmesh.export import ProjectFile

_LOGGER = logging.getLogger(__name__)

VENDOR_ADMIN_STATUS = 0x05  # LBC Admin Property Status `C5 27 05`

CONFIG_TIMEOUT = 3.0  # seconds per Config request attempt
CONFIG_RETRIES = 2
KEY_MODE_TIMEOUT = 2.0  # a vendor Set may be answered by a group publication or not at all; then we read back
SCENE_TIMEOUT = 2.0  # Scene Store / Delete and Scene Action Setup Set: same rule, read back when unanswered


class PlanExecutor:
    """Sends the plans of one hub's configurator and records what the mesh accepted (`ExportStore`)."""

    def __init__(self, store: ExportStore) -> None:
        """Send through `store`'s hub, record into `store`'s export."""
        self.store = store
        # what the running call's plans did (`actions.common._run` starts a new one per call)
        self.outcome = PlanOutcome()

    @property
    def hub(self) -> JungHomeHub:
        """The hub whose link the plans go out on."""
        return self.store.hub

    def plan_response(self) -> dict[str, Any]:
        """Answer what the call's plans did: `{"applied", "total", "recorded", "nodes"}`.

        `applied` of the `total` Config messages were accepted, `recorded` whether the export was written,
        `nodes` the devices that took a message. Unverified on air: the counts of a real plan.
        """
        return {
            "applied": self.outcome.applied,
            "total": self.outcome.total,
            "recorded": self.store.recorded,
            "nodes": [self.store.node_name(n) for n in self.outcome.nodes],
        }

    async def async_replay_journal(self) -> bool:
        """At setup: record what a plan Home Assistant stopped or crashed in the middle of left on the mesh.

        The journal says which plan ran and how many of its steps the nodes had accepted; they are replayed into a
        fresh read of the export as `record` does after a stop (idempotent: a crash during this replay replays
        the same again), and a repair issue names the interrupted action. The record is not handed to the
        gateway here — the link that vouches for it is not up yet — but left to `sync_gateway` or the next
        change. A record that cannot be written keeps the journal for the next start; an unreadable journal is
        dropped. True when the export was written: the caller sets the entry up again from it.

        Read and recorded under the export's lock, which outlives a reload (`data.store_lock`): a reload that does
        not wait for the entry lock (an options save, the UI's *Reload*) while an action's plan still runs on the
        hub before waits here for that plan, which records itself and closes the journal — nothing is recorded
        twice, nor written by two at once.
        """
        async with self.store.lock:
            data = await self.store.journal.async_load()
            if not data:
                return False
            self.store.journaled = True
            try:
                plan = [_step_from_json(row) for row in data["steps"]]
                accepted = plan[: int(data["accepted"])]
                action = str(data["action"])
                prepare, happened = data.get("prepare"), data.get("happened")
            except (KeyError, TypeError, ValueError, IndexError) as err:
                _LOGGER.warning("Dropping an unreadable plan journal: %s", err)
                await self.store.journal_close()
                return False
            self.store.recorded = False
            try:
                await self.record(
                    accepted, plan, prepare=prepare, happened=happened, upload=False
                )
            except HomeAssistantError as err:
                _LOGGER.error(
                    "%s was interrupted after %d of %d Config messages; they could not be recorded in %s yet "
                    "(tried again at the next start): %s",
                    action,
                    len(accepted),
                    len(plan),
                    self.store.path,
                    err,
                )
                return False
            except Exception:
                # the replay must not stop the setup; it would fail the same way at every start
                _LOGGER.exception(
                    "Dropping a plan journal that does not apply to %s", self.store.path
                )
                await self.store.journal_close()
                return False
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            issue_id(self.hub.entry, ISSUE_PLAN_INTERRUPTED),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PLAN_INTERRUPTED,
            learn_more_url=learn_more_url(ISSUE_PLAN_INTERRUPTED),
            translation_placeholders={
                "title": self.hub.entry.title,
                "action": action,
                "accepted": str(len(accepted)),
                "total": str(len(plan)),
            },
        )
        return self.store.recorded

    def _bookkeeping(self, record: ProjectFile, note: Note) -> None:
        """Apply a plan's bookkeeping that is no Config step (`Note`) to `record`; once applied, again is a no-op."""
        if note["kind"] == "room":
            if note["address"] not in record.cdb.groups:
                record.add_group(note["name"], address=note["address"])
        elif note["kind"] == "room_link":
            record_room_link(
                record,
                element_of(record, note["key"]),
                note["room"],
                note["publish"],
                note["function"],
                self.hub.metadata,
                keep=True,
            )
        elif (reset := record.cdb.node_by_addr(note["node"])) is not None:
            # "excluded": the node the plan's `ExportStore.load` found, read again; a record that has it
            # excluded no longer lists it
            record.exclude_node(reset, note["iv_index"])

    async def send(
        self,
        steps: Iterable[ConfigStep],
        *,
        action: str,
        prepare: Note | None = None,
        happened: Note | None = None,
        applied: Callable[[int, int], Applied] = applied_text,
        as_planned: bool = False,
        check: bool = True,
        registers: Iterable[Check] = (),
    ) -> None:
        """Send the plan, additive steps first; the first refusal, silence or lost link stops it, apply-and-record.

        The steps accepted before the stop are what the mesh holds now: they are replayed into a fresh copy of
        the export (the planned file claims the whole plan) and that copy is written — and handed to the gateway
        — before the error is raised, which says how many messages were applied. Nothing is rolled back: every
        Config message is idempotent, so running the action again with the same target completes the plan.
        `prepare`, when given, is the plan's bookkeeping that isn't one of `steps` (e.g. a room the plan
        creates), applied to the fresh record before the accepted steps are replayed into it. `happened` is what
        the mesh already holds from before the plan (a node's reset): it goes into the record even when no step
        was accepted. `applied` words the error's account of the stop from (accepted, total).

        A battery node's steps go first (`ordered`) and the node is kept awake while the plan is sent
        (`KeepAwake.hold`); one it does not answer stops the plan as *asleep* (`_request`). `as_planned` sends the
        steps in the order given instead, none dropped: a plan that repeats the app's own sequence, such as a
        publication reset (`Publication Set 0x0000`, then the address again).

        A plan cancelled from outside (an automation in `mode: restart`, `script.turn_off`, Home Assistant
        stopping) is recorded the same way, held to its end (`run_to_end`), before the cancellation goes on. The
        step in flight when it came is not recorded: the node may or may not have taken it, as when it stays
        silent, and the next run sends it again. What a crash stops is in the plan journal: the plan and
        how many of its steps were accepted, written before its first message and after every accepted one, and
        removed once the export records the outcome; the next setup records what it says (`async_replay_journal`).
        `action` names the plan there, for the repair issue.

        A plan with a step to a node the hub counts as unreachable (`JungHomeHub.node_alive`: a request it left
        unanswered, or its heartbeats missing) is refused before its first message, naming them:
        it would only stop at that node after CONFIG_TIMEOUT times (1 + CONFIG_RETRIES), with the steps before it
        applied. `happened` is still recorded. Battery nodes are never marked so; they go the keep-awake way.
        Unverified on air.

        The call's `outcome` counts the plan and what was accepted of it (its response, error and logbook line); a
        dry run ends here with the plan noted, before anything is journaled or sent (`ExportStore.planned`).

        Before its first message the plan's destructive steps are read from the nodes and compared with the export
        (`preflight`, with the scene `registers` the call deletes from afterwards); a dry run does those reads too.
        `check=False` skips them: the caller read them already, or the plan sets what the node holds whatever the
        export says (a switch's).
        """
        plan, sleepy = self.in_order(steps, as_planned=as_planned)
        if self.store.dry:
            if check:
                await self.preflight(plan, registers=registers)
            self.store.planned(plan, nodes=(c.node for c in registers))
        outcome = self.outcome
        outcome.action = action
        outcome.total += len(plan)
        outcome.steps += [f"{hexaddr(s.node)}: {s.what}" for s in plan]
        accepted: list[ConfigStep] = []
        problem = self._unreachable(s.node for s in plan)
        journal: dict[str, Any] = {
            "action": action,
            "steps": [_step_json(s) for s in plan],
            "accepted": 0,
            "prepare": prepare,
            "happened": happened,
        }
        if problem is None and check:
            await self.preflight(
                plan, registers=registers, applied=applied(0, len(plan))
            )
        try:
            if problem is None:
                if plan or happened is not None:
                    await self.store.journal_save(journal)
                async with self.hub.keep_awake.hold(sleepy):
                    for step in plan:
                        problem = await self._request(step)
                        if problem is not None:
                            break
                        accepted.append(step)
                        outcome.applied += 1
                        if step.node not in outcome.nodes:
                            outcome.nodes.append(step.node)
                        journal["accepted"] = len(accepted)
                        await self.store.journal_save(journal)
        except asyncio.CancelledError:
            await self._record_stopped(accepted, plan, prepare, happened)
            raise
        if problem is None:
            return
        key, placeholders = problem
        err = _failure(
            key,
            **placeholders,
            applied=applied_message(self.hub.hass, applied(len(accepted), len(plan))),
        )
        if (
            record_err := await self._record_stopped(accepted, plan, prepare, happened)
        ) is not None:
            raise err from record_err
        raise err

    def in_order(
        self, steps: Iterable[ConfigStep], *, as_planned: bool = False
    ) -> tuple[list[ConfigStep], set[int]]:
        """Put the plan in the order `send` sends it (`ordered`, battery nodes first); return it and those nodes."""
        steps = list(steps)
        sleepy = {
            unicast
            for s in steps
            if (unicast := sleepy_node(self.hub.cdb, s.node)) is not None
        }
        return (steps if as_planned else ordered(steps, sleepy)), sleepy

    async def _record_stopped(
        self,
        accepted: list[ConfigStep],
        plan: list[ConfigStep],
        prepare: Note | None,
        happened: Note | None,
    ) -> HomeAssistantError | None:
        """`record` a stopped plan, held to its end; the error (logged) when the record could not be written."""
        try:
            await run_to_end(
                self.record(accepted, plan, prepare=prepare, happened=happened)
            )
        except HomeAssistantError as record_err:
            _LOGGER.error(
                "%d of %d Config messages were applied on the mesh but could not be recorded in %s: %s",
                len(accepted),
                len(plan),
                self.store.path,
                record_err,
            )
            return record_err
        return None

    def _dead(self, nodes: Iterable[int]) -> list[int]:
        """Return the mains nodes among `nodes` the hub counts as unreachable (`JungHomeHub.node_alive`), sorted."""
        return sorted(
            {
                n
                for n in nodes
                if sleepy_node(self.hub.cdb, n) is None and not self.hub.node_alive(n)
            }
        )

    def _unreachable(self, nodes: Iterable[int]) -> tuple[str, dict[str, str]] | None:
        """Return the error key and placeholders refusing a plan to `nodes` for the unreachable ones; None when all are there."""
        dead = self._dead(nodes)
        if not dead:
            return None
        return "service_nodes_unreachable", {
            "nodes": ", ".join(self.store.node_name(n) for n in dead)
        }

    async def preflight(
        self,
        plan: Iterable[ConfigStep],
        *,
        registers: Iterable[Check] = (),
        applied: Applied = APPLIED_NOTHING,
        passable: bool = False,
    ) -> None:
        """Before a plan's first write, read from the nodes what it removes or overwrites and compare with the export.

        One Get per element and model a destructive step changes (`jhmesh.plan.preflight_checks`: Model Subscription
        Get, Model Publication Get), and a Scene Register Get per register in `registers`, one at a time, to the
        battery nodes kept awake; compared with the export the plan was made from (`ExportStore.base`). A node that
        no longer holds what the export says — the app changed it since the export, a device was reset, an earlier
        plan stopped half-way — stops the plan before its first message (`service_preflight_differs`, naming the
        node, the Get, both values and how many more differ): the plan would overwrite what the export does not
        describe. A node that does not answer stops it as a plan step would (asleep, or no reply), before
        anything was sent; nodes the hub already counts as unreachable (`_unreachable`) are refused before the
        first read, every one named, rather than waited for one by one. `applied` is what the error says was done
        before. An action called with `skip_preflight` skips all of it (`ExportStore.forced`).

        `passable`: the call passes over the nodes it cannot reach (`delete_scene` with `force`): an unreachable
        node is not read and a silent one is no error — the plan skips them — but a difference still stops it.

        A dry run reads the same and notes what it found in its answer (`ExportStore.preflight_found`) instead of
        raising; without a link it notes every node as unanswered. Plans that only add (a Subscription Add, a
        Model App Bind, a publication where the export records none) send no Get. What the comparison came to —
        the Gets, the differences, or that it was skipped — goes into the call's history (`PlanOutcome.preflight`).
        Unverified on air.
        """
        base = self.store.base
        assert base is not None  # every plan is made on what `ExportStore.load` read
        checks = [*preflight_checks(plan, base), *registers]
        if not checks:
            return
        if self.store.forced:
            self._note_preflight(skipped=len(checks))
            return
        if not self.store.dry:
            if (
                not passable
                and (problem := self._unreachable(c.node for c in checks)) is not None
            ):
                key, placeholders = problem
                raise _failure(
                    key, **placeholders, applied=applied_message(self.hub.hass, applied)
                )
            dead = set(self._dead(c.node for c in checks))
            checks = [c for c in checks if c.node not in dead]
        differences: list[Difference] = []
        silent: list[int] = []
        if self.store.dry and not self.hub.connected:
            silent = list(dict.fromkeys(c.node for c in checks))
        else:
            async with self.hub.keep_awake.hold(c.element for c in checks):
                for item in checks:
                    if item.node in silent:
                        continue
                    reply = await self._read(item, applied)
                    if reply is None:
                        if not self.store.dry and not passable:
                            raise _failure(
                                self._silence(item.node),
                                node=self.store.node_name(item.node),
                                message=item.what,
                                applied=applied_message(self.hub.hass, applied),
                            )
                        silent.append(item.node)
                    elif (found := item.compare(base, reply)) is not None:
                        differences.append(found)
        self._note_preflight(len(checks), differences, silent)
        if self.store.dry:
            self.store.preflight_found(
                [
                    d.as_dict() | {"node": self.store.node_name(d.node)}
                    for d in differences
                ],
                [self.store.node_name(n) for n in silent],
            )
            return
        if differences:
            first = differences[0]
            raise _failure(
                "service_preflight_differs",
                node=self.store.node_name(first.node),
                message=first.what,
                expected=", ".join(first.expected) or "-",
                found=", ".join(first.found) or "-",
                others=str(len(differences) - 1),
                applied=applied_message(self.hub.hass, applied),
            )

    def _note_preflight(
        self,
        checks: int = 0,
        differences: Iterable[Difference] = (),
        silent: Iterable[int] = (),
        *,
        skipped: int = 0,
    ) -> None:
        """Add what a pre-flight came to to the call's outcome (`PlanOutcome.preflight`), for its history.

        `{"checks", "differences", "unanswered", "skipped"}`: the Gets compared, the differences found (as a dry
        run answers them), the nodes that stayed silent, the Gets `skip_preflight` skipped. A call can run several
        pre-flights (`set_threshold` socket by socket): they add up.
        """
        noted = self.outcome.preflight or {
            "checks": 0,
            "differences": [],
            "unanswered": [],
            "skipped": 0,
        }
        noted["checks"] += checks
        noted["differences"] += [d.as_dict() for d in differences]
        noted["unanswered"] += [hexaddr(n) for n in silent]
        noted["skipped"] += skipped
        self.outcome.preflight = noted

    async def _read(self, item: Check, applied: Applied) -> AccessMessage | None:
        """Send one pre-flight Get and return its answer; None when the node stays silent (or, dry, out of reach).

        A lost link, or a node the running hub has no device key for, stops a real run as it stops a plan step.
        """
        try:
            if item.devkey:
                return await self.hub.proxy.request_config(
                    item.node,
                    item.pdu,
                    item.expect,
                    timeout=CONFIG_TIMEOUT,
                    retries=CONFIG_RETRIES,
                    match=item.matches,
                )
            return await self.hub.proxy.request(
                item.element,
                item.pdu,
                item.expect,
                timeout=CONFIG_TIMEOUT,
                retries=CONFIG_RETRIES,
            )
        except TimeoutError:
            return None
        except ValueError as err:
            if self.store.dry:
                return None
            raise _failure(
                "service_export_unknown_node",
                node=self.store.node_name(item.node),
                path=self.store.path,
                applied=applied_message(self.hub.hass, applied),
            ) from err
        except (ConnectionError, OSError) as err:
            if self.store.dry:
                return None
            raise _failure(
                "service_send_failed",
                node=self.store.node_name(item.node),
                message=item.what,
                applied=applied_message(self.hub.hass, applied),
            ) from err

    def _silence(self, node: int) -> str:
        """Return the error key for a node that did not answer: *asleep* for a battery node (press a key, then run)."""
        if sleepy_node(self.hub.cdb, node) is not None:
            return "service_node_asleep"
        return "service_no_reply"

    async def _request(self, step: ConfigStep) -> tuple[str, dict[str, str]] | None:
        """Send one Config message and judge its Status; None when accepted, else the error key + placeholders.

        The Status must echo the step (`ConfigStep.matches`): a duplicate of the previous step's answer is not
        this one's. A node the running hub does not know (the export on disk was replaced by a newer one naming
        a re-provisioned node, without a reload) has no device key here: `ProxyClient._dev_key` raises
        `ValueError`, reported as the export being newer than what is loaded. A battery node that stays silent is
        reported as asleep (`_silence`), asking for a key press and a new run instead of the no-reply error. The
        errors name the node with its device's name (`ExportStore.node_name`), not by its address alone.
        """
        what = step.what
        node = self.store.node_name(step.node)
        try:
            reply = await self.hub.proxy.request_config(
                step.node,
                step.pdu,
                step.expect,
                timeout=CONFIG_TIMEOUT,
                retries=CONFIG_RETRIES,
                match=step.matches,
            )
        except TimeoutError:
            return self._silence(step.node), {
                "node": node,
                "message": what,
            }
        except ValueError:
            return "service_export_unknown_node", {
                "node": node,
                "path": self.store.path,
            }
        except (ConnectionError, OSError):
            return "service_send_failed", {"node": node, "message": what}
        try:
            status = C.decode_config(reply.opcode, reply.params)
        except ValueError:
            status = None
        if not isinstance(status, C.ConfigStatus) or not status.ok:
            name = (
                status.status_name
                if isinstance(status, C.ConfigStatus)
                else "malformed status"
            )
            return "service_config_refused", {
                "node": node,
                "message": what,
                "status": name,
            }
        _LOGGER.debug("%04X accepted %s", step.node, what)
        return None

    async def record(
        self,
        accepted: list[ConfigStep],
        plan: list[ConfigStep],
        *,
        prepare: Note | None = None,
        happened: Note | None = None,
        upload: bool = True,
    ) -> None:
        """Write what the mesh holds after a plan stopped: a fresh copy of the export with the accepted steps replayed.

        `happened` (what the mesh holds from before the plan) goes in first, and alone is reason enough to write.
        A key's room-link row is dropped only once every step of `plan` tagged `unlinks=key` was accepted: while
        any of them is still pending, the row is what makes the next run's `unlink_room_steps` unsubscribe the
        loads that never got there this time. `prepare` runs next — after that drop, so a new room link's row it
        records is not taken for the old one's — and before the replay, so a step that subscribes to a room the
        plan itself creates has something to subscribe to in this fresh copy too. Every part of it is idempotent,
        so a journal replayed twice records the same. `upload=False` leaves the gateway to `sync_gateway` or the
        next change.
        """
        if not accepted and happened is None:
            await (
                self.store.journal_close()
            )  # the mesh holds nothing new: nothing to record, now or after a crash
            return
        record = await self.store.read()
        # as `ExportStore.load` planned it: with the provisioner identity on, the nodes only the vault had are in it too
        await self.store.with_identity(record)
        if happened is not None:
            self._bookkeeping(record, happened)
        done = {id(s) for s in accepted}
        pending = {
            s.unlinks for s in plan if s.unlinks is not None and id(s) not in done
        }
        finished = {s.unlinks for s in accepted if s.unlinks is not None} - pending
        for key in finished:
            record.drop_room_links(key)
        if prepare is not None:
            self._bookkeeping(record, prepare)
        for step in accepted:
            replay(record, step)
        await self.store.save(record, upload=upload)
        _LOGGER.warning(
            "The plan stopped after %d accepted Config message(s); %s records what the mesh holds now",
            len(accepted),
            self.store.path,
        )

    async def _admin_status(
        self,
        key: int,
        pdu: bytes,
        prop: int,
        timeout: float,
        retries: int,
        applied: Applied = APPLIED_KEY_WIRED,
    ) -> AccessMessage | None:
        """Send an LBC Admin request to the key element and wait for its Admin Status; None when it stays silent.

        Only a Status of `prop` answers it: not a battery key's keep-alive (`keep_awake.py`), nor a late one.
        A lost link names the node and the message, and says what `applied` before it.
        """
        try:
            return await self.hub.proxy.request(
                key,
                pdu,
                VENDOR_ADMIN_STATUS,
                timeout=timeout,
                retries=retries,
                expect_cid=M.JUNG_CID,
                match=lambda m: m.params[:2] == prop.to_bytes(2, "little"),
            )
        except TimeoutError:
            return None
        except (ConnectionError, OSError) as err:
            raise _failure(
                "service_send_failed",
                node=hexaddr(key),
                message=M.describe(pdu),
                applied=applied_message(self.hub.hass, applied),
            ) from err

    async def write_key_property(
        self, key: int, prop: int, value: bytes, applied: Applied
    ) -> bool:
        """LBC Admin Property Set of `prop` on the key, then confirm: the Set's status, or a Get when none arrives.

        JUNG firmware answers a state-changing acknowledged Set by publishing the status to the element's group
        (if at all), so the Set is given a short wait and the value is read back otherwise. Returns whether the key
        reports `value`; silence raises, saying what was `applied` before.
        """
        pdu = M.vendor_property_set("admin", prop, value)
        reply = await self._admin_status(key, pdu, prop, KEY_MODE_TIMEOUT, 1, applied)
        if reply is None or not _confirms_property(reply.params, prop, value):
            _LOGGER.debug(
                "%04X: no status for property %04X, reading it back", key, prop
            )
            reply = await self._admin_status(
                key,
                M.vendor_property_get("admin", prop),
                prop,
                CONFIG_TIMEOUT,
                CONFIG_RETRIES,
                applied,
            )
            if reply is None:
                raise _failure(
                    self._silence(key),
                    node=hexaddr(key),
                    message=M.describe(pdu),
                    applied=applied_message(self.hub.hass, applied),
                )
        return _confirms_property(reply.params, prop, value)

    async def reply(  # the request, its wait, and what an error says was applied before it
        self,
        element: int,
        pdu: bytes,
        expect: int,
        timeout: float,
        retries: int,
        applied: Applied,
        scene: int | None = None,
    ) -> AccessMessage | None:
        """Send an AppKey request to `element` and wait for its status; None when it stays silent.

        `scene`: a Scene Action Setup request's scene — a status naming another scene does not answer it:
        matched on source and opcode alone, a late duplicate of the element's answer about another scene would
        pass for it. One too short to name any scene still does: the callers treat it as malformed.
        A lost link names the node and the message, and says what `applied` before it (as a stopped plan does).
        """
        fits = None if scene is None else V.scene_action_reply_to(scene)
        try:
            return await self.hub.proxy.request(
                element,
                pdu,
                expect,
                timeout=timeout,
                retries=retries,
                expect_cid=M.JUNG_CID
                if expect == V.SCENE_ACTION_SETUP_STATUS
                else None,
                match=None
                if fits is None
                else lambda m: len(m.params) < 2 or fits(m.params),
            )
        except TimeoutError:
            return None
        except (ConnectionError, OSError) as err:
            raise _failure(
                "service_send_failed",
                node=hexaddr(element),
                message=M.describe(pdu),
                applied=applied_message(self.hub.hass, applied),
            ) from err

    async def scene_register(
        self, element: int, pdu: bytes, applied: Applied
    ) -> tuple[M.SceneRegister, bool]:
        """Send a Scene Store / Delete and return the element's Scene Register afterwards, and whether it was read back.

        The acknowledged Store / Delete is given a short wait for its Scene Register Status; JUNG firmware tends to
        publish state changes to the model's (unset) publish address instead of replying, so the register is read
        back when nothing arrives. A read-back register always carries status Success — whether the Store took
        is then told by the scene being in it or not, which the caller words accordingly. `applied` is what the
        error says about the steps before this one.
        """
        read_back = False
        reply = await self.reply(
            element, pdu, M.SCENE_REGISTER_STATUS, SCENE_TIMEOUT, 1, applied
        )
        if reply is None:
            _LOGGER.debug("%04X: no Scene Register Status, reading it back", element)
            read_back = True
            reply = await self.reply(
                element,
                M.scene_register_get(),
                M.SCENE_REGISTER_STATUS,
                CONFIG_TIMEOUT,
                CONFIG_RETRIES,
                applied,
            )
        if reply is None:
            raise _failure(
                "service_no_reply",
                node=hexaddr(element),
                message=M.describe(pdu),
                applied=applied_message(self.hub.hass, applied),
            )
        try:
            return M.decode_scene_register_status(reply.params), read_back
        except ValueError as err:
            raise _failure(
                "service_config_refused",
                node=hexaddr(element),
                message=M.describe(pdu),
                status="malformed status",
                applied=applied_message(self.hub.hass, applied),
            ) from err

    async def scene_action(
        self, element: int, scene: int, action: V.Action | None, applied: Applied
    ) -> None:
        """Scene Action Setup Set (`action` None removes) and confirm it — from the Set's status or a Get."""
        pdu = V.scene_action_set(scene, action or V.NO_ACTION)
        reply = await self.reply(
            element, pdu, V.SCENE_ACTION_SETUP_STATUS, SCENE_TIMEOUT, 1, applied, scene
        )
        if reply is None or not _confirms_scene_action(reply.params, scene, action):
            _LOGGER.debug(
                "%04X: no Scene Action Setup Status, reading it back", element
            )
            reply = await self.reply(
                element,
                V.scene_action_get(scene),
                V.SCENE_ACTION_SETUP_STATUS,
                CONFIG_TIMEOUT,
                CONFIG_RETRIES,
                applied,
                scene,
            )
            if reply is None:
                raise _failure(
                    "service_no_reply",
                    node=hexaddr(element),
                    message=M.describe(pdu),
                    applied=applied_message(self.hub.hass, applied),
                )
        if not _confirms_scene_action(reply.params, scene, action):
            raise _failure(
                "service_scene_action_not_applied",
                address=hexaddr(element),
                scene=str(scene),
                applied=applied_message(self.hub.hass, applied),
            )

    async def read_reply(
        self,
        element: int,
        pdu: bytes,
        expect: int,
        applied: Applied,
        scene: int | None = None,
    ) -> AccessMessage | None:
        """`reply` with a read's budget (CONFIG_TIMEOUT, CONFIG_RETRIES): a Get, or the read-back of a Set."""
        return await self.reply(
            element, pdu, expect, CONFIG_TIMEOUT, CONFIG_RETRIES, applied, scene
        )


class Operations:
    """What a group of the configurator's operations works with: the export store and the plan executor."""

    def __init__(self, store: ExportStore, executor: PlanExecutor) -> None:
        """Plan on `store`'s export, send through `executor`."""
        self.store = store
        self.executor = executor

    @property
    def hub(self) -> JungHomeHub:
        """The hub whose mesh the operations configure."""
        return self.store.hub
