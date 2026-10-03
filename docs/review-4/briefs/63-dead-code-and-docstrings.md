# 63 — Dead code, docstring trim, table-driven describe, CLI parser tables

Phase P4 · Wave 20 · Size S · Closes: A4-16, A4-17, A4-18, plus the absolute `jhmesh` imports.

Follow the [conventions](README.md#conventions) in full. Behaviour-identical: one "Internal:" bullet.

## Goal

Less text to maintain and to cover: dead code gone, docstrings no longer longer than the code they describe,
review-ID history out of the source, the two longest functions table-driven.

## Background

`mesh_config._confirms_key_mode` is dead. Library builders used only by tests (`model_subscription_delete_all`,
`model_subscription_overwrite`, `netkey_delete`, `key_refresh_phase_get`, `appkey_delete`,
`generic_location_global_get`, `PropertySpec.writable`, `proxy_config_add_addresses`) are public API (brief 57) and
stay. 92 functions have a docstring longer than their body; 96 comments / docstrings cite review IDs.
`messages._describe` (about 108 lines, `noqa C901`) and `config_messages._describe_params` (74) are long switch
chains; `tools/mesh_poc.build_parser` is 281 lines, `mesh_sniff.build_parser` 88, `mesh_report.render` 186. The
integration imports the bundled library relatively; `docs/roadmap.md` plans the absolute `jhmesh` switch.

## Read first

Report 8 §1.9 (rerun its measurement), `jhmesh/messages.py`, `jhmesh/config_messages.py`, `tools/mesh_poc.py`,
`tools/mesh_sniff.py`, `tools/mesh_report.py`, `docs/roadmap.md` (the requirements / absolute-import item).

## Steps

1. Delete `_confirms_key_mode` and anything else only tests use inside the integration.
2. Docstrings: cut those longer than a self-evident body to one line; move review-ID references to history (the
   CHANGELOG already records them); keep "why" sentences.
3. Table-driven `_describe` / `_describe_params` (opcode → formatter), output byte-identical.
4. CLI parsers as subcommand tables; `--help` output identical (compare before / after in a test).
5. Last commit: switch the integration's imports of the bundled library to the form `docs/roadmap.md` specifies, only
   if the maintainer has decided the PyPI requirement; otherwise leave relative imports and say so.

## Tests to add

`describe` golden outputs for every opcode in the catalogue; CLI `--help` golden outputs.

## Acceptance criteria

Gates pass, snapshots unchanged, no assertion changed; ruff complexity `noqa` on `_describe` removed.

## Verifiable on air here?

Regression only: CLI `listen` output and HA DEBUG lines read the same.

## Risks / off-by-default / "unverified on air"

Touches nearly every file: run it alone and last among code waves.

## Depends on

62.

## Files touched

Nearly every module (docstrings), `mesh_config.py` or `configurator/*`, `jhmesh/messages.py`,
`jhmesh/config_messages.py`, `tools/mesh_poc.py`, `tools/mesh_sniff.py`, `tools/mesh_report.py`, new golden tests,
`CHANGELOG.md`.
