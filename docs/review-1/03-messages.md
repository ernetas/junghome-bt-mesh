# MSG — messages, config messages, vendor models

> Fixed and removed from this file: MSG-01, MSG-02, MSG-03, MSG-04, MSG-05, MSG-06, MSG-07, MSG-08. What changed and why is in `10-implementation-log.md`.

## Shard summary

| Severity | Count | IDs |
|---|---|---|
| P0 | 0 | — |
| P1 | 0 | — |
| P2 | 3 | MSG-01 (heartbeat Subscription CountLog decoded with the Publication formula), MSG-02 (`describe` OverflowError on a far-future Time message; kills the sniffer), MSG-06 (scene-action read-out accepts a stale status for another scene; coordinator side, cross-shard) |
| P3 | 5 | MSG-03, MSG-04, MSG-05, MSG-07, MSG-08 |

**Checked and found clean:**
- Every SIG opcode constant, checked against Mesh Model / Profile 1.0.1: Generic OnOff/Level/Delta/Move/DTT/OnPowerUp/Battery/Location/Property, Light Lightness/CTL incl. Default and Range, Scene incl. Delete 0x829E/F, Sensor, Time, Health, all Config opcodes. The JUNG vendor opcodes and their `C0|op, 27 05` encoding match `vendor-models.md` §2.3. The 1-byte opcode spaces don't collide: Config 0x00-0x03/0x06 vs Health 0x04/0x05 and the SIG tables, checked programmatically.
- Key-index packing (`_pack_key_indexes`, net index in the low 12 bits, the same as Zephyr's `key_idx_pack_pair`). Publication layout: credential flag at bit 12, RFU bits, period steps/resolution, retransmit count/steps, 12/14-byte status, and vendor model id as company LE + model LE. Round-trip fuzz over 2000 random parameter sets found no differences.
- Composition Data page 0 parsing with truncation checks; Model App / Subscription Status and List (SIG/vendor width from the opcode); AppKey/NetKey/Key Refresh status; Heartbeat Publication/Subscription layouts, and their Set builders' CountLog/PeriodLog/TTL/feature validation; `describe_config` key redaction, including under devkey.
- Time Set encoding: TAI seconds equals the naive UTC difference from 2000-01-01 plus the TAI-UTC delta, which I derived as correct. The authority bit sits in bit 0 under delta+255, the zone is in quarters+64, and Time Zone and TAI-UTC Delta Status decode correctly.
- Transition-time encoding: resolution table, 0x3F unknown, the previous fix pass's Set guard, delay ×5 ms. Sensor Status Format A/B marshalling, including the 0x7F zero-length case. Sensor Descriptor tolerance packing; Battery flag bit positions; Location sentinels.
- The vendor models: JH Scheduler bit layouts for sub 0/1/2/15 and Scene Action Setup list/single/absent forms against `vendor-models.md` §4. Round-trip fuzz of `Schedule.encode`, `Action.encode`/`decode_action` and `scene_action_set`/`decode_scene_action_status` found no differences.
- Fuzzing `messages.describe` with random payloads (0-23 bytes) for every known SIG, Config and JUNG vendor opcode found only MSG-02. The secret property 0xC001 never reaches `describe` output as hex, whatever the opcode or length (checked for every vendor/SIG property op).
- TID: an 8-bit wrapping global counter, and every Set builder draws a fresh one unless `tid` is given.

**Cross-shard leads (not recorded as findings here):** `binary_sensor.py:145` (`_on_detector_sensor_status`) and `climate.py:135` (`_on_sensor_status`) call `M.sensor_values(p)` without catching its documented `ValueError`. A truncated Sensor Status then logs "on_message handler failed" with a traceback, and skips any handler chained after it. Shard 07 should check this.
