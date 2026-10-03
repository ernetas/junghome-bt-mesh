# 60 — Remaining hub components and a lifecycle registry

Phase P4 · Wave 17 · Size L · Closes: A4-3 (remaining components), A4-13.

Follow the [conventions](README.md#conventions) in full. Behaviour-identical refactor: one "Internal:" bullet,
docs only for the module table.

## Goal

`JungHomeHub` becomes a composition root and public API over small components with explicit state, following brief
58's recipe; timers and tasks are cancelled from one registry.

## Background

The hub's clusters (report 8 §1.2): lifecycle and proxy discovery; unknown nodes and export refresh; gateway trust;
link manager (loop, connect, watchdog, keep-alive, filter watch, grace); connect-time reads; energy poll; time and
location; settle and counters; reachability; sequence and IV space; heartbeats; state cache and status handlers;
repair issues; commands. `async_stop` cancels 12 `_unsub_*`, 6 tasks and 4 per-node dicts by hand; back-off is kept as
a one-element list and an index in three places. Composition (not mixins) is the choice: mixins do not type-check
under `mypy --strict` and keep the 78 attributes in one namespace.

## Read first

`coordinator.py` (whole), brief 58's result (`hub_gestures.py`), `keep_awake.py`, `diagnostics.py` (`noqa: SLF001`
reach into heartbeats), `tests/test_coordinator.py` (about 145 `hub._private` references) and `tests/test_link_loss.py`,
`tests/test_reachability.py`.

## Steps

One commit per component, serial, gates after each, in this order:

1. `hub/liveness.py`: reachability + heartbeats (unreachable, recheck, missed answers, alive deadlines, reprobe); a
   public `configured_at` replaces the diagnostics reach-in.
2. `hub/energy.py` and `hub/clock.py`: meter polls, counters, reset, backfill trigger; time / location broadcast, DST.
3. `hub/export_watch.py`: unknown nodes, export refresh back-off, gateway trust (pin, certificate issue).
4. `hub/refresh.py`: connect-time reads.
5. `hub/issues.py`: the hub's repair issues.
6. `hub/link.py` (`LinkManager`) last: loop, proxy choice, connect, watchdog, keep-alive, filter watch, grace.
7. `hub/lifecycle.py`: a named timer / task registry and a `Backoff` helper; `async_stop` cancels through it **in
   exactly today's order**; fold the boxed back-off list and the export back-off index.
8. When the `hub/` package exists, move `hub_gestures.py` to `hub/gestures.py` (mechanical). The hub keeps one-line
   delegations for every method entities and services call.
9. After each component, move its tests out of `test_coordinator.py` into a matching test module and update
   attribute paths (`hub._x` → `hub.<component>._x`); never change an assertion.

## Tests to add

A test that `async_stop` cancels every registered handle (registry empty afterwards) and keeps the order.

## Acceptance criteria

Gates pass after every commit; snapshots unchanged; `coordinator.py` holds the composition root and delegations
only; no component imports a platform module.

## Verifiable on air here?

Regression only after each merge: connect, commands, a breaker-off reconnect (someone at home), energy sensors.

## Risks / off-by-default / "unverified on air"

Stop ordering; timers capturing bound methods; module-level patch targets that move (`LINK_IDLE_TIMEOUT`,
`LINK_LOSS_GRACE`, `establish_connection`, `UNREACHABLE_RECHECK`, …) must be patched where the moved code reads them.

## Depends on

58. 62 follows.

## Files touched

`coordinator.py`, new `hub/{__init__,liveness,energy,clock,export_watch,refresh,issues,link,lifecycle,gestures}.py`,
`hub_gestures.py` (moved), `diagnostics.py`, `tests/test_coordinator.py` split into per-component modules,
`tests/conftest.py` (patch targets), `CHANGELOG.md`, `docs/ha-integration.md` (module table).
