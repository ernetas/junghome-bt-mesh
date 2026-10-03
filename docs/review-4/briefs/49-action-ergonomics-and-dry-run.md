# 49 — Dry runs, structured responses, action forms

Phase P3 · Wave 13 · Size M · Closes: improvements W I3, W I6, W I7, W I9, U4-13 (U4 F10).

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

Every rewiring action can show its plan without sending it, answers what it applied, and has a form a user can fill
in without knowing hex addresses.

## Background

The integration (`custom_components/junghome_ble`) builds each configurator plan as `ConfigStep`s before `_send`
(`mesh_config.py`), and `merge.diff_documents` exists. Brief 13 added a dry run to `delete_unused_scenes` only. The
`applied_*` helpers (`mesh_config.py:403-503`) already compute partial-success texts but the actions return nothing
structured. `services.yaml` takes scenes and rooms as typed names; `create_schedule` and `store_scene` show ten or
more fields at once; errors name hex addresses (`service_config_refused`). Only INFO / WARNING logs record finished
plans (`mesh_config.py:2214`, `:2536`).

## Read first

`services.py` (registration, `_run`, handlers), `services.yaml`, `mesh_config.py` (`_load`, `_send`, `_record`,
`applied_*`), `jhmesh/merge.py` (`diff_documents`), `logbook.py`, `strings.json` `services` and `exceptions`.

## Steps

1. `dry_run` on `set_room`, `assign_key`, `clear_key`, `delete_room`, `delete_scene`, `remove_device`, `create_room`,
   `create_scene`: the configurator builds the plan on a read-only `_load` variant (no adoption side effect) and returns
   `{steps: [step.what …], diff: diff_documents(before, after)}` without `_send` / `_save`.
2. Optional responses `{applied, total, recorded, nodes}` for every rewiring action; errors carry the same in
   `translation_placeholders`.
3. `confirm: true` required for `remove_device` and `delete_scene(force)` (decision as for M3/M8).
4. Logbook entry per finished or stopped plan ("Key 0151 now drives room Kitchen; 6 messages" style, translated);
   diagnostics keep the last few plans (step text, outcome, no keys).
5. `services.yaml` sections (collapsed *State to store*, *When*, *What*); `scene_entity` as an alternative to the
   scene name, `area` as an alternative to the room name; device names in error placeholders instead of hex.

## Tests to add

Dry runs send nothing (the fake link records no PDU) and write nothing; response shapes; `confirm` refusal; logbook
descriptions; the alternative selectors resolve to the same plan; translations test with sections.

## Acceptance criteria

All gates green; no dry run touches the export, the gateway or the mesh.

## Verifiable on air here?

Yes: dry runs on a push-button and lights are harmless; one real `assign_key` checks the response shape.

## Risks / off-by-default / "unverified on air"

`confirm` is a breaking change for scripts calling `remove_device` (document). The read-only `_load` must not adopt
the gateway's export.

## Depends on

Brief 05 (apply-and-record), brief 13 (admin gating, scene dry run), brief 22 (*soft*).

## Files touched

`services.py`, `services.yaml`, `mesh_config.py`, `logbook.py`, `diagnostics.py`, `strings.json`,
`translations/en.json`, `docs/user/`, `docs/ha-integration.md`, `CHANGELOG.md`, tests (`tests/test_services.py`,
`tests/test_mesh_config.py`, `tests/test_logbook.py`).
