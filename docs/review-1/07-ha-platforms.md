# PLT — services, platforms, diagnostics, strings

> Fixed and removed from this file: PLT-01, PLT-02, PLT-03, PLT-04, PLT-05, PLT-06, PLT-07, PLT-08, PLT-09, PLT-10. What changed and why is in `10-implementation-log.md`.

## Shard summary

| Severity | Count | IDs |
|---|---|---|
| P0 | 0 | — |
| P1 | 1 | PLT-04 |
| P2 | 6 | PLT-01, PLT-02, PLT-03, PLT-05, PLT-06, PLT-07 |
| P3 | 3 | PLT-08, PLT-09, PLT-10 |

PLT-01 was also raised by the messages reviewer as a lead; it is confirmed by running `climate._on_sensor_status(hub, m, b"\x9e")`, which raises `ValueError: truncated sensor data`.

**Checked and found clean:**
- **Unique ids.** No collisions within a platform: loads, keys and blinds use `Device.unique_id`; config entities `{uuid}-{location}-{spec.name}`; node entities `node:{uuid}-fault|identify|clear-faults`; `{uuid}-battery`; `{mesh}-scene-N`; `{mesh}-proxy`. They are stable across the previous fix pass's `_led` ordinal change, which still keys on the primary element's location.
- **Light.** brightness ↔ lightness: HA 1 maps to lightness 257, and lightness 1..128 floors at brightness 1, so 1 % never turns the light off. Colour modes match the kind, and kelvin is clamped to the read range.
- **Cover.** `_to_ha` / `_to_level` invert consistently and round-trip. Tilt is offered only in blinds mode with a slat element. `supported_features` follows `has_tilt`. The the previous fix pass once-per-link mode read is correct.
- **Climate.** The level ↔ °C mapping is exact at 5 / 30 °C. min/max/step are consistent. The only hvac mode is `heat`, presets are derived, `none` is a no-op, and preset temperatures are read on demand.
- **Sensor / number / select.** Every device_class / state_class / unit combination is valid for HA 2026.9: ENERGY Wh `total_increasing`, DURATION h `total_increasing`, number `ms`/`s` DURATION, `K` TEMPERATURE_DELTA, lx ILLUMINANCE. MS32 is exposed in seconds with a seconds unit. A select option outside the list renders as unknown.
- **Event entity.** All event types are declared and unknown vendor codes are filtered. The bus event carries device_id / key / type.
- **Device-trigger validation and the logbook describers:** clean, apart from PLT-06.
- **Diagnostics.** The gateway token, host and export paths are redacted, and so are the proxy / unknown-node MACs through recursive `async_redact_data`. Node UUIDs are masked everywhere through `redact_node_uuids` (the canonical dashed UUIDs make the 18-character prefix correct). Secret vendor properties are never cached. No NetKey / AppKey / DevKey appears. `network_id` and the IV index are public (they are in beacons). Only the gap in PLT-08 remains.
- **Strings.** Every literal entity `translation_key` exists under its *own* platform, not just under some section. `_key` variants carry `{key}`. Exception placeholders match every `_validation` / `_failure` / `HomeAssistantError` call site (checked with a script, 57 call sites). `en.json` stays in lockstep through `test_translations.py`.
- **Services.** They are registered once in `async_setup` and never removed on unload. Per-entry locks survive reloads. `entity_id: all` is rejected. Room and scene names are validated in `MeshConfigurator`. Cross-network targets are refused.

**Minor notes, not recorded as findings:**
- `config_entities.py:103` still says blinds are "not derived yet".
- `light.async_turn_on` with only a colour temperature on an *off* CTL light turns it on at 100 %, which is intended and commented, but it differs from how HA usually restores the last level.
- A gateway-mode press shows twice in the logbook: the event entity's state change and the `EVENT_BUTTON_ACTION` describer.
