# 56 — Split `services.py` into per-domain action modules

Phase P4 · Wave 15 · Size M · Closes: A4-8 (report 8, brief B5).

Follow the [conventions](README.md#conventions) in full. Behaviour-identical refactor: one "Internal:" bullet,
docs only for the module table.

## Goal

`services.py` is only the registration table; each action domain gets its own module with schemas and handlers.

## Background

`services.py` (about 1600 lines) holds the schemas, a 90-line registration table, and handlers for rooms, keys,
scenes, gateway / export / devices / audit, schedules and thresholds, plus shared plumbing (`_run`,
`async_configure`, `_wait_for_link`, resolvers, `CONFIGURATORS`).

## Read first

`services.py` (whole), importers (`switch.py` and `device_names.py` import `async_configure`),
`tests/test_services.py` (`patch.object(svc, "_configurator")`, `svc.has_thresholds`, `c._lock`,
`c._entry_for_hub_services`, `c._bound`).

## Steps

1. New package `custom_components/junghome_ble/actions/` (not `services/`, so HA's handling of `services.yaml` is
   untouched): `common.py` (`CONFIGURATORS`, register / unregister, `_bound`, `_hub`, `_configurator`, resolvers,
   `_run`, `async_configure`, `_wait_for_link`, shared `ATTR_*`, `Load`), `rooms.py`, `keys.py`, `scenes.py`,
   `schedules.py` (import the top-level module as `from .. import schedules`), `thresholds.py`, `devices.py`,
   `audit.py`.
2. Move verbatim, one domain per commit, gates after each.
3. `services.py` keeps `SERVICE_*`, `RESPONSES`, `async_setup_services` with the handler list in the same order
   (registration order is visible), and re-exports every name imported elsewhere, including the `_lock` alias.
4. Point the tests' `patch.object(svc, ...)` at `actions.common`, where handlers resolve the name.

## Tests to add

None expected.

## Acceptance criteria

Gates pass, snapshots unchanged, no assertion changed, no platform or `__init__.py` edit; `services.py` under about
250 lines; every `actions/*.py` under about 400 lines.

## Verifiable on air here?

Regression only: a dry run and `audit_network` from the HA developer tools.

## Risks / off-by-default / "unverified on air"

The admin-gated registrations from briefs 03 and 13 must stay admin-gated; check the registration table diff.

## Depends on

None within wave 15. Lands after 49. Adopt 53's `mesh_errors` at the two `send_failed` sites afterwards.

## Files touched

`services.py`, new `actions/{__init__,common,rooms,keys,scenes,schedules,thresholds,devices,audit}.py`,
`tests/test_services.py` (patch targets), `CHANGELOG.md`, `docs/dev/architecture.md` (module table).
