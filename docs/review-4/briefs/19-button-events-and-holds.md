# 19 — Button events from the hub, key-aware triggers, bounded holds

Phase P1 · Wave 5 · Size S–M · Closes: D24 (H4-2), R4-7 — folds in H I-3 / U4-9 (key-aware triggers).

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

Device triggers and logbook lines for keys keep working when a key's event entity is disabled; each key offers the
trigger subtypes its connection can produce first; every dimming hold ends after a maximum time and on link loss or
stop.

## Background

`custom_components/junghome_ble` is a Home Assistant integration for JUNG HOME Bluetooth Mesh; wall keys arrive as
mesh messages the hub (`coordinator.py`) turns into gestures, then into `event` entities and a bus event.

- **H4-2.** `EVENT_BUTTON_ACTION` is fired only by `JungHomeButtonEvent._fire_bus_events` (`event.py:184-211`);
  `device_trigger.py:157-182` and `logbook.py:95` listen only to it. Disabling `event.<key>` silently stops every
  device-trigger automation and logbook line for that key. Scene recalls already have a hub-side fallback
  (`coordinator.py:3983-3995`).
- **H I-3 / U4-9.** `async_get_triggers` offers all 16 `TRIGGER_SUBTYPES` for every key (64 on a 4-key gang),
  although `event.py:72-90` knows each key's `connection` and the key mode (`0x5003`) is read.
- **R4-7.** In `coordinator.py:4060-4125` only Delta holds get the `DIM_HOLD_QUIET` timer; a Move hold ends only on a
  Move 0 or a new start. `_on_disconnect` and `async_stop` clear holds silently (`:1671-1673`); the vendor gestures'
  `_hold_side` (`:4214-4217`) has the same gap. A lost Move-stop means `hold_end` never fires and a dim-while-held
  automation never stops.

## Read first

- `event.py`, `device_trigger.py`, `logbook.py` (whole).
- `coordinator.py`: `fire_button`, `add_event_listener`, `_button_event` (~4180), `_dim_hold` (~4060-4125),
  `_on_scene_recall` (~3983-4030), `_on_disconnect` (~2557) — after brief 15, the `_drop_link` / link-loss hook.
- `sensor.py` `JungHomeKeyMode`; `strings.json` `device_automation`.
- `tests/test_device_trigger.py`, `tests/test_event.py`, `tests/test_logbook.py`, `tests/test_coordinator.py` holds.

## Steps

1. Fire `EVENT_BUTTON_ACTION` from the hub's event fan-out: `device_id` from
   `hub.device_ids[buttons_device_id(...)]`, `entity_id` from the entity registry (may be absent). The entity only
   triggers its own event and writes state. Keep the scene fallback's "only when no key event" rule.
2. `async_get_triggers`: when the key's connection / mode is known, offer gateway → click, double_click, hold_start,
   hold_end and their sides; load / room → press_on, press_off, dim; scene → scene. Unknown → all, as today.
   Validation stays permissive so saved automations keep working. Same filter for the event entity's
   `event_types` only if it does not change unique IDs or registry entries.
3. Add `DIM_HOLD_MAX` (about 30 s) in `const.py`; arm it for every hold kind, Move and vendor `_hold_side` included.
4. On link loss (brief 15's hook) and in `async_stop`, end open holds: by decision M11 either fire `hold_end` with an
   attribute `reason: link_lost` / `stopped`, or drop them and document it.
5. Update the docstrings and the docs' device-trigger section.

## Tests to add

- Event entity disabled in the registry → a key event still runs the device trigger and the logbook describes it
  (no `entity_id`).
- Entity enabled → the bus event fires exactly once.
- Trigger list per connection kind; unknown → all 16.
- A Move hold ends after `DIM_HOLD_MAX`; a hold is ended by a link loss and by `async_stop` (per M11).

## Acceptance criteria

Gates green; `tests/test_snapshots.py` unchanged; no double firing.

## Verifiable on air here?

Yes, person needed: a gateway-mode rocker for clicks / holds, a load-wired key for press_on / press_off. Disable the
key's event entity and check a device-trigger automation still fires. Holds: a push-button wired to a dimmer in SIG
mode; capture first with `tools/mesh_sniff.py capture` while someone holds the key, then pull the link (breaker on
the proxy node) mid-hold.

## Risks / off-by-default / "unverified on air"

Double firing during the transition; `hold_end` on link loss may stop an automation while the node still dims
(hence the `reason` attribute). The SIG-wired hold derivation stays "unverified on air".

## Depends on

15 (link-loss hook). Decision M11. Brief 46 (blueprints) builds on it.

## Files touched

`event.py`, `device_trigger.py`, `logbook.py`, `coordinator.py`, `const.py`, `strings.json`,
`translations/en.json`, `tests/test_device_trigger.py`, `tests/test_event.py`, `tests/test_logbook.py`,
`tests/test_coordinator.py`, `CHANGELOG.md`, `docs/ha-integration.md`.
