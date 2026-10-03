# 36 — Probe and expose firmware-only properties

Phase P2 · Wave 9 · Size M · Closes: F4-9, F4-3 (review-3 F18), F4-11 (review-3 F25), F4-10.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

Settle with one supervised probe what the firmware-only property ids do, then expose the useful ones as expert
entities (config category, disabled by default).

## Background

The integration (`custom_components/junghome_ble`, library `jhmesh`) builds its config entities from the property
catalogue in `jhmesh/properties.py`; `config_entities.describe` (`config_entities.py:184-240`) skips ids marked
firmware-only. Candidates:

- `0x0F00` transmission settings (`0100` on key elements and the socket meter), `0x0F01` / `0x0F02` current / all-time
  runtime statistics, `0x500C` key toggle enable, `0x000E` server state publish request (F4-9). If `0x0F00` sets the
  meter's publication rhythm (about 65 s today, the Sensor Setup Server holds no cadence), live power gets faster
  without polling.
- Hotel dim value `0x1008`, basic-light enable `0x1009`, night dim value `0x1011`, presentation mode enable / time
  `0x1012` / `0x1013` (F4-3; names from the gateway firmware's property list and APK strings; raw values were read on
  the DALI insert, `docs/hidden-features.md` §2).
- Run-on remaining time: with *Run-on time* `0x1007` set, does an OnOff Status carry `[present][target][remaining]`?
  `coordinator.py:3738-3745` keeps `target_on` and drops `remaining` (F4-11).
- LED colours outside the app's palette: the wire format is `[r][g][b][mode]`, 0..100 per channel
  (`properties.py:541`); `JungHomeLedColour` (`select.py:100-152`) shows `None` for any other colour (F4-10).

## Read first

`jhmesh/properties.py:1160-1300`; `config_entities.py:184-240`; `docs/hidden-features.md` §2, §7 items 5–6, §9–§10;
`docs/android/properties.md`; `select.py:100-152`; `coordinator.py:3730-3750`.

## Steps

1. **Probe (maintainer, someone at home, `tools/mesh_poc.py listen` or the sniffer running, every value restored in
   the same session):**
   1. `prop get` `0x0F00`, `0x0F01`, `0x0F02` on a push-button primary, a key element, a mini actuator, the socket's
      main and meter elements; twice, minutes apart (do the counters move?).
   2. Meter: `prop set <meter> 0x0F00` to `0000`, `0200`, `0101` in turn; watch the Sensor Status rhythm for a few
      minutes each; restore `0100`.
   3. Key: set `0x500C` to 0, press the key: does a single key stop toggling? Restore 1.
   4. DALI insert / dimmer: `0x1009` = 1 — does "off" leave the light at the hotel value? Restore 0. Change
      `0x1011` and look for a switch-on level change at night. Read the presentation struct after changing its time
      field; **never enable presentation mode unattended**.
   5. Run-on: set `0x1007` to a short time on a light, switch on, `get <element>`: is `remaining` present? Restore.
   6. LED: `prop set` a non-palette colour on a key element's LED: accepted and shown?
   Write every outcome into `docs/hidden-features.md` and `docs/android/properties.md` before building.
2. Codecs (replace `Raw`) only for ids the probe settled; keep their source "firmware" with access set.
3. An allow-list of firmware ids proven on air that `describe` turns into entities (number for percent values,
   switch for enables), config category, disabled by default.
4. If step 1.5 shows `remaining`: keep it in the element state and add a timestamp sensor *Switches off at*.
5. If step 1.6 works: an optional RGB `light` entity per LED state (on / off colour), disabled by default; keep the
   select.

## Tests to add

Codec round-trips in `tests/jhmesh` (Hypothesis welcome); entity creation gated by product / kind; writes and
read-backs through the fake link; the remaining-time sensor from a Status with a remaining field (frozen clock).

## Acceptance criteria

All gates green; `docs/hidden-features.md` §7 items 5–6 updated with outcomes; only probe-settled ids become entities.

## Verifiable on air here?

Yes, with the DALI insert, dimmer, switch insert, socket meter, keys and minis — only through the supervised probe in
step 1 (person at home).

## Risks / off-by-default / "unverified on air"

Presentation mode may switch loads on its own: keep it off. A `0x0F00` change may stop the meter publishing until
restored. Every new entity is disabled by default; anything the probe did not fully settle says "unverified on air".

## Depends on

Brief 34 (*soft*: node clocks help interpret night-light behaviour).

## Files touched

`jhmesh/properties.py`, `config_entities.py`, platforms for the new entities (`number.py`, `switch.py`, `sensor.py`,
`light.py`), `coordinator.py` (remaining time), `strings.json`, `translations/en.json`, `docs/hidden-features.md`,
`docs/android/properties.md`, `docs/ha-integration.md`, `CHANGELOG.md`, tests.
