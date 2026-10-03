# MOD — properties, devices, CDB, export

> Fixed and removed from this file: MOD-02, MOD-03, MOD-04, MOD-01, MOD-06, MOD-07, MOD-08, MOD-09, MOD-10, MOD-11, MOD-12. What changed and why is in `10-implementation-log.md`. Still open: MOD-05.

### MOD-05: `Position.encode` swaps 0 ↔ 255 on write, the app analysis says the swap is on read only — 0 % may go on air as "fully closed"
- **Severity:** P3
- **Confidence:** low (code and doc disagree; the decompiled `p056e8/q.java` is not in the tree to settle it)
- **Location:** `custom_components/junghome_ble/jhmesh/properties.py:276` (class `Position.encode`)
- **Problem:** `docs/android/properties.md` §1.4 (0x1106) and §2.17 say the app converts `percent→raw = round(pct/100·255)`, maps 100 % → 254, and swaps `255→0`/`0→255` **on read** (`q.java:22-31`). `Position.encode` also swaps on write, so 0 % is sent as raw 255. If the doc is right, setting `blind_position_on_power` / `slat_position_on_power` / the ventilation positions (0x1106, 0x1107, 0x110A, 0x110B) to 0 % programs the opposite end position, while HA reads it back as 0 % (the codec round-trips with itself, so no test catches it).
  ```python
  raw = self._raw(value)
  return bytes([_swap_ends(254 if raw == 255 else raw)])  # 0 % -> 0xFF
  ```
- **Failure scenario:** user sets "Blind position on power" to 0 % → Admin Set `06 11 03 FF`; if the device reads raw 0xFF as 100 %, the blind drives to the other end after a power failure while HA shows 0 %.
- **Fix:** settle the direction first (read the decompiled `p056e8/q.java:13-52` or do one on-air write of 0 % and a Get). If the swap is read-only, change `encode` to `return bytes([254 if raw == 255 else raw])` and update the class docstring; otherwise correct `properties.md` §1.4/§2.17 to say "both directions". Either way the doc and the code must say the same thing.
- **Test:** (if the swap is read-only) `tests/jhmesh/test_properties.py::test_position_encode_does_not_swap` — `assert P.POSITION.encode(0) == b"\x00"`, `assert P.POSITION.encode(100) == b"\xfe"`, `assert P.POSITION.decode(b"\xff") == 0`.
- **Verify:** `python -m pytest tests/jhmesh/test_properties.py -k position -q`
- **Related:** —

## Shard summary

**Counts:** P0: 0 · P1: 0 · P2: 4 (MOD-01, MOD-02, MOD-03, MOD-04) · P3: 8 (MOD-05 … MOD-12).

**Checked and found clean**
- `properties.py` codecs against `docs/android/properties.md` §1–§2 and the field-tested corrections: Int/Enum/Bool widths and signedness; `TEMP_001C` (sint16 0.01 °C, 0x8000 unknown); SIG 0x004F Temperature 8 (sint8 0.5 °C, 0x7F unknown); Power/Current/Voltage `Scaled` all-ones sentinels (the previous fix pass is correct); `Counter` not-known/not-valid markers for Energy/Energy32 (the previous fix pass fix correct; it also nulls Time Hour 24's valid 0xFFFFFE — immaterial); `DateUTC` overflow → ValueError (fix correct); `EnforcedOutputCodec` 3-byte status (fix matches the app's parser); InsertId byte order (field-tested order); VersionLE; ThresholdCodec (0xFFFFFF none); SceneConfig/PropertyMode/KeyEvent layouts; EnergyChart big-endian + all-ones; AstroRegister/AstroStatus bit layout (decode side); LED id arithmetic and product sets; `describe_status` never raises for any codec (all failures are ValueError) and redacts secrets before the hex fallback.
- `devices.py` rule table against every fixture composition (MeshNetwork, Android share, Blinds, RTR, detectors) and the real layouts in `docs/network-topology.md` (2-gang keys at 0x40/0x42, 2-output loads at 1/2, CTL second element at location 1): sockets vs meter element, dimmer/CTL kinds, blind + slat selection, detector vs button (1100 exclusion), thermostat first, battery flag; no mutable defaults shared between devices; `Metadata` loaders (strict iOS files, lenient share `meta`).
- `cdb.py`: key lists (index 0, 16-byte keys, unique indices), unicast arithmetic for multi-element nodes (address = unicast + index, range and uniqueness across nodes), UUID canonicalisation, virtual-label parsing (the previous fix pass) incl. groups/publish/subscribe, provisioner ranges, exclusions/IV index, error messages never quote key material, `repr` hides keys.
- `export.py`: round-trip layout sniffing and key order, header preservation, `_meta` null-list handling and `_rows`/`_keeps` (the previous fix pass is correct), room/scene/device-name mutators and their `ModelChange` lists, `remove_group` clean-up, the newer-export guard (digest + timestamp), `save` temp+fsync+replace with symlink write-through and 0600 `.bak` (the previous fix pass is correct), no key material in any exception text.
