# 58 — First hub component: `ButtonGestures`

Phase P4 · Wave 16 · Size M · Closes: A4-3, first component (report 8, brief B8).

Follow the [conventions](README.md#conventions) in full. Behaviour-identical refactor: one "Internal:" bullet,
docs only for the module table.

## Goal

Move the gesture logic out of `JungHomeHub` into a component with its own state (as `KeepAwake(hub)` already is) and
set the recipe for the remaining components (brief 60).

## Background

`JungHomeHub` is about 3350 lines with a 200-line `__init__` setting 78 attributes. Gestures (clicks, double clicks,
holds, dim holds, repeat suppression, event listeners, the `DimHold` dataclass, `fire_button`,
`add_event_listener`) form one contiguous cluster of about 200 lines. Brief 19 changed this code (hub-side bus
events, hold timeouts, holds ended on link loss); move the result.

## Read first

`coordinator.py` gestures cluster and `DimHold`, `async_stop` (the delayed-click and dim-hold cancels),
`keep_awake.py` (the component precedent), `dispatch.py` (brief 53), callers of `add_event_listener` /
`fire_button` (`event.py`, `binary_sensor.py`, `sensor.py`, `config_entities.py`), `tests/test_coordinator.py` and
`tests/test_event.py` (`hub._dim_holds`, `hub._event_listeners`, `hub._button_last_click`).

## Steps

1. `custom_components/junghome_ble/hub_gestures.py` with `class ButtonGestures(hub)`. Move the gesture state out of
   `JungHomeHub.__init__` (`_delayed_clicks`, `_button_last_click`, `_hold_side`, `_dim_holds`, `_unknown_codes`,
   `_button_recent`, `_sig_recent`, `_event_listeners`, `click_delay`) and the gesture methods; rewrite
   `self.<hub thing>` to `self.hub.<hub thing>`.
2. On the hub: `self.gestures = ButtonGestures(self)`; keep `add_event_listener` / `fire_button` as one-line
   delegations; `click_delay` as a property if read; the option is still read from `entry.options` at construction.
3. Registered handlers (`_on_onoff_set`, `_on_level_set`, `_on_vendor_property_set`, `_on_scene_recall`) stay
   registered under the same opcodes during `coordinator.py`'s import, before any platform chains onto them
   (`binary_sensor.py` chains onto `GEN_ONOFF_SET` and must run after).
4. `async_stop` calls `self.gestures.cancel_all()` at the same position in the stop sequence; timers capture
   `self.gestures.<method>`.
5. Update test attribute paths only.

## Tests to add

A test asserting the coordinator's `GEN_ONOFF_SET` handler runs before the detector's, if none exists.

## Acceptance criteria

Gates pass, snapshots unchanged (event entities included); `JungHomeHub` loses 15+ methods and 9 attributes;
`grep -n "_dim_holds\|_delayed_clicks" coordinator.py` is empty; `hub_gestures.py` imports no platform module.

## Verifiable on air here?

Regression only, with someone at home: a gateway-mode rocker click and a hold still produce the same events.

## Risks / off-by-default / "unverified on air"

Handler order on shared opcodes; cancel handles after the move.

## Depends on

52 and 53 merged. 60 follows serially.

## Files touched

`coordinator.py`, new `hub_gestures.py`, `tests/test_coordinator.py`, `tests/test_event.py`, `CHANGELOG.md`,
`docs/ha-integration.md` (module table).
