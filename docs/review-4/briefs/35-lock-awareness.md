# 35 — Lock awareness on lights and sockets

Phase P2 · Wave 9 · Size S–M · Closes: F4-2 (report 6 brief B2); ledger rows `ui:state:lockfunctioncapability`,
`ui:uc:toggledevice`.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

A load locked by the app, a key or Home Assistant shows `locked` on its light / socket entity, commands to it are
refused with a clear message, and a locked load is never reported unreachable.

## Background

The repository is a Home Assistant custom integration (`custom_components/junghome_ble`) for JUNG HOME Bluetooth Mesh
devices, with its mesh library `custom_components/junghome_ble/jhmesh` (also `jhmesh/`) and CLI tools in `tools/`.

- The lock state is LBC Admin property `0x0009` (`EnforcedOutput`, codec in `jhmesh/properties.py`). It is read only
  while the disabled-by-default *Lock* switch / *Lock function* select exist (`config_entities.py:1915-1990`,
  `switch.py:359-388`).
- `light.py` and the socket switch carry no `locked` attribute and do not refuse commands; only the cover does
  (`cover.py:327-340`, `_check_unlocked`).
- The app reads `0x0009` of every load element when its device list opens and disables the controls.
- Open question: what a locked load answers to an OnOff / Lightness Set. If nothing, `_load_command`
  (`coordinator.py:4467-4500`) times out and `_missed_answer` (`coordinator.py:3272`) can mark a healthy, merely
  locked node unreachable.

## Read first

- `config_entities.py` `LockFunctionEntity` (`:1915-1990`), `switch.py` `JungHomeLockSwitch` (`:359-388`),
  `cover.py:327-400` (the refusal pattern to copy).
- `jhmesh/properties.py` `EnforcedOutput`; `coordinator.py` `_load_command`, `_missed_answer`, `ElementState`, the
  connect-time refresh and `_chunked`.
- `docs/parity/ledger-ui.json` rows above; `docs/gap-analysis/control-and-state.md` §2.6, §2.11.

## Steps

1. **Probe first (maintainer):** lock the DALI light with its *Lock* switch in HA (or `tools/mesh_poc.py prop set
   <element> enforced_output …`), then `tools/mesh_poc.py set <element> on` and `lightness <element> 30000` with
   `tools/mesh_poc.py listen` running in another shell. Record whether the node answers (a Status with the unchanged
   state, or nothing). Lock from the app and watch `listen`: does a lock arrive unsolicited? Unlock. Write the result
   into `docs/hidden-features.md`.
2. Read `0x0009` (LBC Admin Get `C2 27 05 [09 00]`) of every lockable load once per link, chunked with the existing
   refresh (e.g. five per chunk). Share the read with `LockFunctionEntity` through `PropertyReader.read`'s `since`
   dedupe so the *Lock* switch stops issuing its own Get.
3. Keep the result in `ElementState` (`lock: EnforcedOutput | None`, plus `lock_until`).
4. Light and socket entities: extra attributes `locked`, `lock_until`.
5. `async_turn_on` / `async_turn_off` raise `ServiceValidationError(translation_key="load_locked")` while locked. When
   the lock had a time limit that should have ended, re-read once before refusing.
6. `_missed_answer` does not count a silence from a load known locked (only if the probe shows silence).
7. Toggle of an unknown state: keep HA's default unless deliberately overriding it to send OFF like the app
   (`ui:uc:toggledevice`); document the choice.
8. Update the ledger rows (status, code, tests, `missing`).

## Tests to add

- The refresh issues the `0x0009` Gets in chunks; the shared read is not duplicated by the *Lock* switch.
- Attributes on light and socket; refusal message (translations).
- A timed lock: expiry triggers a re-read before refusing (frozen clock).
- No unreachable marking for a locked load that stays silent.

## Acceptance criteria

All gates green; ledger rows moved to implemented (or partial with the on-air part named); `docs/ha-integration.md`
describes the attributes and the refusal.

## Verifiable on air here?

Yes: lights and sockets, after the probe in step 1 (a person watching the light is helpful, not required).

## Risks / off-by-default / "unverified on air"

One extra Get per load per link — keep it inside the refresh chunking. Mark the refusal and the silence handling
"unverified on air" until the probe confirms the node's behaviour. Never block a lock that ended unseen: re-read
first.

## Depends on

None. Brief 38 (lock-function keys) depends on this one.

## Files touched

`coordinator.py`, `light.py`, `switch.py`, `config_entities.py`, `strings.json`, `translations/en.json`,
`docs/parity/ledger-ui.json`, `docs/hidden-features.md`, `docs/ha-integration.md`, `CHANGELOG.md`, tests
(`tests/test_light.py`, `tests/test_switch.py`, `tests/test_reachability.py`).
