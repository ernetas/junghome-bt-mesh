# 62 — Plan model into `jhmesh`, Protocols and TypedDicts, `const.py` diet

Phase P4 · Wave 19 · Size M · Closes: A4-10, A4-11, A4-12.

Follow the [conventions](README.md#conventions) in full. Behaviour-identical refactor: one "Internal:" bullet,
docs only for the module table.

## Goal

The pure plan model lives in the library next to the commissioning plan; export-format knowledge leaves the HA glue;
the hub's surface is typed; constants live with their owners.

## Background

- Two plan models: HA's `ConfigStep` / `ordered` / `replay` (now `configurator/plan.py`, brief 55) and the library's
  `commission.Step`. `mesh_config` / `configurator` access raw export JSON (about 39 `.get("…")` and 19 `["…"]` on
  keys such as `elementAddress`, `cachedGroupConnectionMetadata`, `keyModeSceneConfigExports`) and reaches into
  `pf._matches_function`.
- 21 integration modules form one `TYPE_CHECKING` cycle because everything annotates with `JungHomeHub`; no Protocol
  or TypedDict exists; 13 `HassKey`s spread over six modules.
- `const.py`: 179 names, 115 used by one other module (52 by the hub alone); protocol facts sit in the HA layer.

## Read first

`configurator/plan.py`, `jhmesh/commission.py`, `jhmesh/export.py` (`ProjectFile`), `const.py`, the `HassKey`s
(`grep -rn "HassKey" custom_components`), report 8 §1.4–§1.7 measurements (rerun them: line numbers moved).

## Steps

1. `jhmesh/plan.py`: `ConfigStep`, `ordered`, `replay`, merged with `commission.Step` (one model; keep re-exports).
2. `ProjectFile` methods for every raw meta-row access in the configurator; make `_matches_function` public.
3. `protocols.py`: `HubView` (what entities need) and `MeshPort` (cdb + proxy + hass, for `keep_awake`, `tls`,
   `energy_history`); annotate satellites with them to break the cycle.
4. `TypedDict`s for export meta rows, seq-store records and the diagnostics JSON.
5. `data.py`: `JungHomeData` replacing the 13 `HassKey`s (keep accessors).
6. Move single-consumer constants to their owners, protocol constants to `jhmesh`, timing budgets into one documented
   table per subsystem. Keep `const.py` for HA-facing keys (`DOMAIN`, `CONF_*`, `OPTION_*`, `PLATFORMS`, `SIGNAL_*`).

## Tests to add

An AST layer test: no module imports a platform module except HA; `jhmesh` imports nothing from the integration.

## Acceptance criteria

Gates pass (mypy strict, 3.13 library job), snapshots unchanged, no assertion changed; the `TYPE_CHECKING` cycle is
gone (show the import-graph script output in the report).

## Verifiable on air here?

Regression only.

## Risks / off-by-default / "unverified on air"

Constant moves break module-level patch targets; grep the tests for each moved name.

## Depends on

55, 57, 60.

## Files touched

New `jhmesh/plan.py`, `jhmesh/commission.py`, `jhmesh/export.py`, `configurator/*`, new `protocols.py`, new `data.py`,
`const.py` and its consumers, tests (patch targets, layer test), `CHANGELOG.md`, `docs/dev/architecture.md` (module table).
