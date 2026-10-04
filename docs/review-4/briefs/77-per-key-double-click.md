# 77 — Double click per key, without delaying every other key

Phase P3 · Wave 23 · Size S–M · Closes: U4-19 (per-key double click).

Follow the [conventions](README.md#conventions) in full.

## Goal

A key that has a double-click automation gets a clean `click` / `double_click` distinction; every other key keeps
reporting its `click` at once. Today the choice is all or nothing.

## Background

`hub/gestures.py` derives `double_click` from two clicks of the same key within `DOUBLE_CLICK_WINDOW` (0.5 s). The
entry option `click_delay` (`OPTION_CLICK_DELAY`) holds back every `click` of every key for that window so a double
press reports no click — which makes every single press of every key half a second slower. Without the option, a
double press reports `click`, `click`, `double_click`.

## Read first

`hub/gestures.py` (clicks, double clicks, the click delay, listeners), `event.py` (the key event entities, their
`event_types`), `device_trigger.py`, `config_flow.py` (the options flow, `OPTION_CLICK_DELAY`), the blueprints in
`blueprints/automation/junghome_ble/` that use double clicks, `docs/user/buttons-and-automations.md`.

## Steps

1. The click delay per key: a set of key elements (or event entity ids) whose clicks are held back. Choose the
   cleanest source and say why in the module docstring — preferred: automatic, from what listens (a key whose
   `double_click` has a device trigger or an automation using the event entity's `double_click` gets the delay; one
   whose double clicks nobody uses does not), if Home Assistant offers a reliable way to know; otherwise an options-flow
   multi-select of keys with the delay (*Keys that wait for a double click*).
2. Keep the entry-wide `click_delay` as "every key" for compatibility (an existing entry with it on behaves as today);
   migrate nothing silently.
3. The event entity's attributes say whether the key waits for a double click; the docs explain the trade-off (half a
   second on that key only).
4. Docs: `docs/user/buttons-and-automations.md`, `docs/ha-integration.md` (options), the blueprints' descriptions if
   they mention the option; strings in `strings.json`, `en.json` and every translation, translated. CHANGELOG under
   `## 1.4.0 (unreleased)` (create above `## 1.3.0` if missing; never edit released sections), *Added*.

## Tests to add

A key with the delay: single press → one `click` after the window; double press → `double_click` only. A key without
it: single press → `click` at once; double press → `click`, `click`, `double_click`. Both keys pressed interleaved.
The entry-wide option still delays every key. The options flow (if chosen) round-trips the selection; a removed key in
the selection is dropped.

## Acceptance criteria

Gates green; existing gesture tests unchanged for entries without the new setting.

## Verifiable on air here?

Yes, with a person at two keys. Unverified on air until then.
