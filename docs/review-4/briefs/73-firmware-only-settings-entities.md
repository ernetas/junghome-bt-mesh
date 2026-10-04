# 73 — Firmware-only settings as entities, from the sweep's readings

Phase P2 · Wave 22 · Size M · Closes: the second half of brief 36 (F4-3, F4-9, F4-10, F4-11) for what the on-air sweep
settled.

Follow the [conventions](README.md#conventions) in full.

## Goal

The firmware-only properties whose layout and effect the sweep established become config entities, through the
existing property-entity machinery (`FIRMWARE_ENTITIES`), disabled by default.

## Background

`properties/targets.py` holds `FIRMWARE_ENTITIES = frozenset()`: brief 36 waited for the probe of
`docs/on-air-sweep.md` C6. The sweep has run (results A7 and C6; `docs/hidden-features.md` §13 has the tables):

- `basic_light_function_enable` (DALI insert primary): `00` / `01`; with `01`, an OFF leaves the light on at the
  hotel value (seen: hotel `0x33` → lightness 13107, i.e. the value is in 1/255 of full).
- the hotel value and the night value (1 byte, 1/255); the night value's effect was not seen (needs the dark).
- `presentation_mode_enable` / `presentation_mode_time` (8 bytes each): layout not understood — **leave out**.
- `transmission_settings` `0x0F00` on key, input and meter elements: `0100` default; `0000` and `0101` are answered
  `0100`, `0200` is kept; no effect seen on the meter's rhythm in four minutes — a select of the accepted values
  `0100` / `0200` only if its meaning can be stated from the app decompile notes (`docs/android/properties.md`);
  otherwise leave it out and say why.
- `key_toggle_enable` (`01` on key and input elements): its effect needs a person — expose it only if the app has
  the same setting (then its meaning is the app's), else leave out.
- the LED colour `[r][g][b][mode]`: a value outside the app's palette (`32143c00`) is accepted and read back; the LED
  showing it is unverified. Offer a free RGB colour for the LED where the app offers the palette, if the existing LED
  entity's design allows it cleanly; otherwise record why not.
- the run-on remaining time is never reported (C6 step 4): nothing to add.

## Read first

`properties/targets.py` (`FIRMWARE_ENTITIES`, how a spec becomes an entity), `jhmesh/properties.py` (the catalogue,
codecs, ids), `config_entities.py` and the platforms it feeds (`switch.py`, `number.py`, `select.py`, `light.py`),
`docs/hidden-features.md` §2 and §13, `docs/android/properties.md`, `docs/on-air-sweep.md` results A7 and C6, brief 36,
the parity ledger rows for these ids (`docs/parity/`).

## Steps

1. Codecs in `jhmesh/properties.py` for each id you expose (hotel / night as a percentage over 1/255, the enable flags
   as booleans), each with the id, layout and source (the sweep's session and sequence number, no date).
2. Add their ids to `FIRMWARE_ENTITIES`; entities disabled by default, entity category *config*, on the elements the
   sweep saw serving them (a DALI insert's primary for the light functions). Names and icons translated:
   `strings.json`, `en.json`, `icons.json` and every other `translations/*.json` (translated, matching each file's
   terminology).
3. Parity ledger: the rows of the exposed ids move to `implemented` citing the code and the sweep; the left-out ids
   say why.
4. Docs: `docs/user/entities.md` (each new entity: what it does, that the night value's effect is unverified on
   air), `docs/ha-integration.md`, `docs/hidden-features.md` §13 (which ids are now entities); CHANGELOG under
   `## 1.3.0 (unreleased)` (create above `## 1.2.0` if missing; never edit released sections), *Added*.

## Tests to add

Codec round trips with the sweep's bytes; entity creation only on elements serving the id; disabled by default; a
write sends the Set and the read-back updates the state; snapshot updates reviewed.

## Acceptance criteria

Gates green, `tests/test_parity.py`, translations complete.

## Verifiable on air here?

Yes for the basic light function and the hotel value (DALI insert, harmless and restorable, as in C6); the night
value needs the dark and a person.
