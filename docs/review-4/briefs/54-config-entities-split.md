# 54 — Split `config_entities.py`

Phase P4 · Wave 15 · Size M · Closes: A4-4 (report 8, brief B3).

Follow the [conventions](README.md#conventions) in full. Behaviour-identical refactor: one "Internal:" bullet,
docs only for the module table.

## Goal

Three cohesive modules instead of one file of about 2100 lines: target discovery (which entities exist), the
property reader and cache (transport), and the entity base classes (HA).

## Background

`config_entities.py` holds target discovery (`PropertyEntityDescription`, `describe`, `EntityTarget` and
subclasses, `SetupState`, `node_version`, twelve `*_targets` functions consumed by seven platforms), the status
handlers and read cache (`is_secret`, `cacheable`, `redacted`, `_on_vendor_property_status`, `_on_setup_status`,
`status_owner`, `applied`, `vendor_server`), `PropertyReader` with `READERS` / `property_reader`, and the entity
bases (`ConfigEntity`, `PropertyEntity`, `SetupStateEntity`, `lock_mode`, `u16`, `retired_unique_ids`).

## Read first

`config_entities.py` (whole), every importer (`grep -rn "config_entities" custom_components tests`), the tests that
patch `config_entities.PROPERTY_READ_TIMEOUT` and `config_entities.ConfigEntity`.

## Steps

1. Create package `custom_components/junghome_ble/properties/` with `__init__.py` (docstring only), `targets.py`
   (discovery) and `reader.py` (handlers, cache, `PropertyReader`, `check_outcome`, `cached_value`). Move verbatim.
2. `config_entities.py` keeps the entity bases and re-exports every moved name that platforms, `__init__.py`,
   `keep_awake.py`, `thresholds.py`, `schedules.py` or `coordinator.py` import — none of those files changes.
3. Registration timing: `reader.py` registers its status handlers on import and `config_entities.py` imports it at
   top level, so registration happens at the same moment as today. Check no reader opcode is chained elsewhere
   (`grep -n "register_status_handler\|chain_status_handler\|_after(" custom_components`).
4. Move the `PROPERTY_READ_TIMEOUT` patch targets to `properties.reader` (or wherever brief 11 made the timeout read
   at call time).
5. Inside the integration always import `.properties.reader` relatively; `jhmesh.properties` is a different module.

## Tests to add

One test asserting the vendor status handler is registered after `import custom_components.junghome_ble.config_entities`.

## Acceptance criteria

Gates pass, snapshots unchanged, no assertion changed, no platform file changed; `config_entities.py` under about
500 lines; `properties/targets.py` imports no `homeassistant.helpers.entity*`.

## Verifiable on air here?

Regression only: config entities (LED colour, run-on time) read and write as before.

## Risks / off-by-default / "unverified on air"

Handler registration order; the `properties` name clash with `jhmesh.properties`.

## Depends on

None (wave 15 runs 52–57 in parallel with re-exports). Brief 32 and 36 must have landed.

## Files touched

`config_entities.py`, new `properties/__init__.py`, `properties/targets.py`, `properties/reader.py`, tests with patch
targets, `CHANGELOG.md`, `docs/ha-integration.md` (module table).
