# 55 — Split `MeshConfigurator` into planning, store, executor and domains

Phase P4 · Wave 15 · Size M–L · Closes: A4-5 (report 8, brief B4).

Follow the [conventions](README.md#conventions) in full. Behaviour-identical refactor: one "Internal:" bullet,
docs only for the module table.

## Goal

The plan builders become pure (`ProjectFile` + CDB in, steps out), the executor is one small module, the export
store and gateway sync are separate. Public operation names on `MeshConfigurator` do not change.

## Background

`MeshConfigurator` (about 2600 lines, 94 methods) mixes file I/O and identity, gateway upload / adopt / sync, the
room / key plan builders, the executor (`_send`, `_silence`, `_request`, `_record`), thresholds and sensors, and
scenes. Above the class sit about 360 lines of pure module-level helpers (`ConfigStep`, `ordered`, `replay`, the
`applied_*` texts, wiring helpers).

## Read first

`mesh_config.py` (whole; find ranges by name — line numbers drifted after Phase 0–3 briefs), `services.py` `_run`
(`recorded` / `adopted` reset per call), `tests/test_mesh_config.py` patch targets (`mc.CONFIG_TIMEOUT`,
`KEY_MODE_TIMEOUT`, `SCENE_TIMEOUT`, `load_project`, `write_private_with_backup`, `_listed_macs`, `asyncio`,
`patch.object(mesh_config, "element_groups" | "threshold_client")`).

## Steps

1. New package `custom_components/junghome_ble/configurator/`: `plan.py` (ConfigStep, ordered, replay, applied
   texts, KeyPlan, row-drop helpers), `wiring.py` (pure helpers through `load_project`). Re-export from
   `mesh_config.py`; run the gates; commit.
2. Extract `store.py` (`ExportStore`: path, lock, recorded, adopted, `_load` … `sync_gateway`, upload retry and the
   plan journal from brief 05), then `executor.py` (`PlanExecutor(store, hub)`), then `rooms.py`, `scenes.py`,
   `thresholds.py`. Gates after each step. Prefer plain functions for pure planners.
3. `mesh_config.py` keeps `MeshConfigurator` as a facade with every public method delegating, and `hub`, `path`,
   `lock`, `recorded`, `adopted`, `upload_retry` as read/write properties.
4. Move monkeypatch targets to where the code now reads each name; keep each constant defined in exactly one module.
   `patch.object(mesh_config.MeshConfigurator, "set_rooms")` stays.
5. Replace the two `noqa: SLF001` cross-class reaches with public `read()` / `upload()` on `ExportStore`. Leave
   `pf._matches_function` (brief 62).
6. Keep `_record`'s exact call order: `happened` → row drops → `prepare` → `replay` → `_save`, under the same lock.

## Tests to add

`tests/test_configurator_plan.py`: build a room-link plan from the fixture export through `ProjectFile` with no
`hass`, check `ordered()` — proves the planners are pure.

## Acceptance criteria

Gates pass, snapshots unchanged, no assertion changed; `mesh_config.py` under about 600 lines; `configurator/plan.py`
and `wiring.py` import no `homeassistant` (planners raise a small `PlanError(key, placeholders)` the facade
translates).

## Verifiable on air here?

Regression only: a `rename_room` and an `assign_key` dry run (brief 49) behave as before.

## Risks / off-by-default / "unverified on air"

The upload-retry task reads `entry.runtime_data.configurator`, which must stay the facade; the journal replay (05)
must keep its call site.

## Depends on

None within wave 15. Lands after 49 (last behaviour change). 62 builds on it.

## Files touched

`mesh_config.py`, new `configurator/{__init__,plan,wiring,store,executor,rooms,scenes,thresholds}.py`,
`tests/test_mesh_config.py` (patch targets), new `tests/test_configurator_plan.py`, `CHANGELOG.md`,
`docs/ha-integration.md` (module table).
