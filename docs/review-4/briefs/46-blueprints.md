# 46 — Blueprints and a button cookbook

Phase P3 · Wave 12 · Size M · Closes: U4-3 (U4 F4).

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

Owners import ready-made automations for the common wall-switch jobs without writing YAML.

## Background

The integration (`custom_components/junghome_ble`) fires the bus event `junghome_ble_button_action` for every key event
(from the hub since brief 19), with data `device_id`, `entity_id`, `key`, `type`, `side`, `target`, `scene`
(`event.py`, `docs/ha-integration.md` around `:419-422`). Hold-to-dim of a JUNG light exists as
`junghome_ble.start_dim` / `stop_dim` (verified on air, review-3 F12). There are no blueprints.

## Read first

`event.py`, `device_trigger.py`, `const.py` (`EVENT_BUTTON_ACTION`), the dim actions in `services.py` / `light.py`,
HA's blueprint docs and `homeassistant.components.blueprint.models`, the user guide's buttons page (brief 43).

## Steps

1. New directory `blueprints/automation/junghome_ble/` at the repository root (not part of the integration zip;
   `scripts/package_ha.sh` unchanged):
   - `rocker_light_control.yaml`: input `key` (entity selector, integration `junghome_ble`, domain `event`), target
     lights; click up / `press_on` → on, click down / `press_off` → off, click without side → toggle; `hold_start` by
     side → repeat `light.turn_on` with `brightness_step_pct` every 0.35 s, at most 30 times; `mode: restart` so
     `hold_end` (or any new event) ends the loop; optional double-click action.
   - `rocker_dim_jung_light.yaml`: a JUNG light only; `hold_start` → `start_dim` by side, `hold_end` → `stop_dim`.
   - `rocker_scene_selector.yaml`: up to six action inputs (click / double / hold, up / down).
   - `presence_lighting.yaml`: any motion / occupancy `binary_sensor` (generic; JUNG detectors are unverified),
     optional illuminance threshold, off delay, `mode: restart`.
   - `appliance_finished.yaml`: power sensor, running / idle thresholds, delay, notify action.
   - Optional `device_offline_notify.yaml` on brief 45's *Unreachable devices*, only if brief 45 is merged.
2. Each description says gestures need a key linked to the gateway, or the free-rocker recipe (brief 43), and that a
   key wired to a JUNG load still drives that load.
3. Docs: a *Blueprints* section in the user guide's buttons page (what, prerequisites, manual install into
   `<config>/blueprints/automation/junghome_ble/`, import links once the repository is public).

## Tests to add

`tests/test_blueprints.py`: (a) every file loads with HA's YAML loader (`!input`) and validates as an automation
blueprint; (b) per blueprint, copy it into `hass.config.path("blueprints/automation/junghome_ble/")`, set up
`automation` with `use_blueprint`, mock the target services (`async_mock_service`), fire the bus event (or set sensor
states) and assert the calls — including that `hold_end` stops the dim loop.

## Acceptance criteria

All gates green; all blueprints validate and behave in tests; integration coverage unchanged; docs list them.

## Verifiable on air here?

Yes with push-buttons and lights — a person pressing a gateway-mode rocker (clicks, holds) and a load-wired key.

## Risks / off-by-default / "unverified on air"

Nothing changes for existing installations. The presence blueprint is generic; the free-rocker path is "unverified
on air".

## Depends on

Brief 19 (bus event from the hub); brief 45 for the optional offline blueprint; brief 43 for the docs page.

## Files touched

New `blueprints/automation/junghome_ble/*.yaml`, new `tests/test_blueprints.py`, `docs/user/buttons-and-automations.md`,
`docs/ha-integration.md`, `CHANGELOG.md`.
