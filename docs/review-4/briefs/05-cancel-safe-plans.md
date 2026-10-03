# 05 — Cancel-safe apply-and-record, plan journal

Phase P0 · Wave 1 · Size M · Closes: D12 (W4-4); folds in W I1 (plan journal), W I10 (stop-anywhere property test).

Follow the [conventions](README.md#conventions) in full.

## Goal

Whatever stops a configuration plan — refusal, silence, a lost link, task cancellation, an HA stop or crash — the
mesh export ends up recording exactly the Config steps the nodes accepted, and the entry reloads when it did.

## Background

HA custom integration `custom_components/junghome_ble`. `MeshConfigurator` (`mesh_config.py`) turns room / key /
scene / removal actions into ordered Config messages, sends them (`_send`) and records the accepted ones into the
export (`_record`, apply-and-record).

- `_send` (`mesh_config.py:2066-2121`) records only after `_request` returns a problem; an `asyncio.CancelledError`
  from `_request` propagates straight through. Same shape in the save-on-`HomeAssistantError` blocks of `store_scenes`
  (`:3006-3011`), `remove_from_scenes` (`:3193-3196`), `delete_scene` (`:3245-3257`) and `remove_node`
  (`:1582-1595`, whose `happened` record is only written by `_record`).
- `services._run` (`services.py:803-829`) reloads only on `HomeAssistantError` or a change.
- Reproduced: a plan cancelled after two accepted steps never calls `_record`. Triggers: an automation in
  `mode: restart`, `script.turn_off`, HA stopping. The export then disagrees with the nodes; a later `clear_key` /
  `delete_room` leaves the unrecorded subscriptions on the loads for good; a cancelled `remove_device` after the Node
  Reset leaves a reset node recorded as live.

## Read first

`mesh_config.py:2066-2219` (`_send`, `_request`, `_record`), `:1532-1602`, `:2986-3013`, `:3173-3267`;
`services.py:800-830`; `tests/test_mesh_config.py` (stop tests); `tests/sim` (simulated nodes).

## Steps

1. `_send`: `except asyncio.CancelledError:` → `await asyncio.shield(self._record(…accepted…))`, then re-raise.
2. Per-load loops (`store_scenes`, `remove_from_scenes`, `delete_scene`): save in `finally` when something changed.
3. `remove_node`: write the `excluded` / `happened` record on cancel too.
4. `_run`: reload when `recorded` on any exit path (`BaseException`), still under the entry lock; shield the reload
   start; never swallow `CancelledError`.
5. Journal (second commit): a `.storage` `Store` per entry written before a plan's first message
   (`{operation, steps, accepted index}`) and after each accepted step; removed on completion. At setup a non-empty
   journal is replayed into a fresh read of the export (as `_record` does) and a repair names the interrupted action.
   Skip the gateway upload during HA shutdown; leave it to `sync_gateway`.

## Tests to add

- Cancel after k accepted steps for `assign_key`, `set_room`, `store_scene`, `remove_device`: the export holds
  exactly k steps and the entry reloads (the reviewers' repro: today `_record` is never called).
- `CancelledError` is re-raised in every case.
- Journal: simulated crash mid-plan → replay at setup, repair raised; no journal left after success; replay twice is
  idempotent.
- Hypothesis "stop anywhere": for each plan builder, stop after k = 0..n accepted steps, record, and assert the export
  equals the simulated node state (`tests/sim`); re-running the action completes the plan.

## Acceptance criteria

Gates green; no `CancelledError` swallowed; property test in the default profile.

## Verifiable on air here?

Not needed (offline). Optional: cancel an `assign_key` on a push-button from a script with `mode: restart`, then run
`tools/mesh_poc.py config audit <node>` and compare with the export.

## Risks / off-by-default / "unverified on air"

Shielded writes delay HA shutdown by one file write. A journal replay must not double-apply: steps are idempotent,
replay into a fresh read.

## Depends on

None. Briefs 13, 22 and 49 edit the same functions later.

## Files touched

`mesh_config.py`, `services.py` (`_run`), `strings.json`, `translations/en.json` (journal repair),
`tests/test_mesh_config.py`, `tests/test_services.py`, a new property test module, `CHANGELOG.md`,
`docs/ha-integration.md`.
