# 53 — One dispatch helper, one error mapper, pure conversions

Phase P4 · Wave 15 · Size S · Closes: A4-2, A4-6, A4-7 (report 8, brief B2).

Follow the [conventions](README.md#conventions) in full. Behaviour-identical refactor: one "Internal:" bullet in
`CHANGELOG.md`, docs only for the module table.

## Goal

- One status-handler chaining helper instead of two copies.
- One context manager mapping transport errors to translated `HomeAssistantError`s instead of 11 hand-written blocks.
- Pure conversions out of platform modules, so the configurator and services stop importing platforms.

## Background

- Status handlers are registered in a module-global dict in `coordinator.py` (`STATUS_HANDLERS`,
  `register_status_handler`) by import side effects; platforms chain onto an opcode by hand with two equivalent
  helpers: `binary_sensor.py` (`chain_status_handler`, `_chained`) and `climate.py` (`_after`).
- `TimeoutError` / `(ConnectionError, OSError)` → `HomeAssistantError("send_failed")` blocks sit in `button.py` (3),
  `entity.py`, `scene.py`, `climate.py`, `config_entities.py` (3), `services.py` (2).
- Layers point the wrong way: `mesh_config.py` and `services.py` import `climate.level_to_temperature` /
  `temperature_to_level`; `schedules.py` imports `cover.closedness_to_level`.

## Read first

`binary_sensor.py` (chain helper), `climate.py` (`_after`, conversions), `cover.py` (conversions), `button.py`,
`entity.py` (`_send`), `scene.py`, `coordinator.py` (`STATUS_HANDLERS`, `register_status_handler`),
`tests/test_binary_sensor.py` (imports `chain_status_handler` from `binary_sensor`).

## Steps

1. `dispatch.py`: move `chain_status_handler` and `_chained` from `binary_sensor.py`; re-export from
   `binary_sensor.py`. Replace `climate._after` with it after proving the call order is identical (both capture the
   earlier handler at decoration time and call earlier, then new). Keep decorators where they are so registration
   order does not change.
2. `errors.py`: `@contextmanager def mesh_errors(*, timeout_key=None, placeholders=None)`. `TimeoutError` →
   `timeout_key` when set, otherwise `send_failed` (as today, `TimeoutError` is an `OSError`);
   `ConnectionError` / `OSError` → `send_failed`; always `raise … from err`; anything else propagates unchanged.
   Apply it in `button.py`, `entity.py` (placeholder read at raise time), `scene.py`, `climate.py` only. Read each
   site first: one that catches only `(ConnectionError, OSError)` gets `timeout_key=None` semantics preserved.
3. `conversions.py`: move `temperature_to_level`, `level_to_temperature`, `closedness_to_level`,
   `level_to_closedness` with their constants; re-export from `climate.py` / `cover.py`.
4. Do not touch `coordinator.py`, `config_entities.py`, `services.py`, `mesh_config.py`, `schedules.py`: they keep the
   old names via re-exports. Adopting `mesh_errors` at the `config_entities.py` / `services.py` sites is a one-line
   follow-up after 54 and 56 have merged.

## Tests to add

`tests/test_errors.py`: every mapping with and without `timeout_key`, `__cause__` chaining, an unrelated exception
passing through. A handler-order test for a chained opcode if none exists.

## Acceptance criteria

Gates pass, snapshots unchanged, no assertion changed. `climate.py` has no `_after`. `conversions.py` imports no
platform module.

## Verifiable on air here?

Regression only: a light command and a rocker press behave as before.

## Risks / off-by-default / "unverified on air"

Exception order (`TimeoutError` before `OSError`); ruff `F401` on re-exports — use the `as` form.

## Depends on

None. 58 builds on `dispatch.py`.

## Files touched

New `dispatch.py`, `errors.py`, `conversions.py`; `binary_sensor.py`, `climate.py`, `cover.py`, `button.py`,
`entity.py`, `scene.py`; new `tests/test_errors.py`; `CHANGELOG.md`; `docs/dev/architecture.md` (module table).
