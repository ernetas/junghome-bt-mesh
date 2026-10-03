# 31 — Transitions on lights, scenes and key scenes

Phase P2 · Wave 8 · Size S–M · Closes: — (F4-1; review-3 F19 left open).

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

HA's `transition` works on JUNG dimmers and DALI lights and on scene activation where the firmware honours it;
nothing changes for kinds that do not.

## Background

`custom_components/junghome_ble` (HA integration) builds mesh messages with `jhmesh/messages.py`. The library cannot
build a Lightness Set with a transition (`messages.py:313`) nor a Scene Recall with one (`:380`); HA sends
`transition=0` on OnOff (`coordinator.py:4506`) and nothing on Lightness / CTL (`:4587`, `:4595`), so nodes use their
Default Transition Time (0 everywhere). `light.py` declares no `TRANSITION` feature; `scene.py:146` ignores
`transition`. Neither the app nor the gateway ever sends a transition, so JUNG support is unproven (class c); the DALI
insert ignores a *Default* Transition Time (`docs/hidden-features.md` §7.3). Dimmers do fade on rocker holds, and the
Generic Move / Delta path was verified on air.

Messages: Light Lightness Set `0x824C [L u16][tid][trans][delay]`; Light CTL Set `0x825E`; CTL Temperature Set
`0x8264`; Generic OnOff Set `0x8202`; Scene Recall `0x8242` / `0x8243 [scene u16][tid][trans][delay]`; fallback Generic
Delta Set `0x8209 [delta s32][tid][trans][delay]`; key → scene `0x5002 [scene u16][transition u32 ms]`. Transition
byte: bits 7-6 resolution (100 ms, 1 s, 10 s, 10 min), bits 5-0 steps 0..62 (63 prohibited); delay in 5 ms units.

## Read first

- `jhmesh/messages.py:260-390` (`_tid_transition`, `light_lightness_set`, `light_ctl_set`,
  `light_ctl_temperature_set`, `scene_recall`); `coordinator.py:4502-4660` (set_onoff … recall_scene) and the status
  handlers that keep `target_*` (~3730-3830); `light.py`; `scene.py`; `mesh_config.py` `assign_key` scene branch and
  `0x5002`.
- `docs/hidden-features.md` §3, §7.3; `docs/android/network-logic.md` (no transitions); Mesh Model §3.1.3.

## Steps

1. Agent: add `transition` / `delay` kwargs to the builders (the no-transition bytes stay identical) and
   `encode_transition(seconds)` / a decoder choosing the finest resolution; add `--transition SECONDS` to
   `tools/mesh_poc.py` `lightness`, `ctl`, `set`, `scene`.
2. **Probe (maintainer, someone watching the light):** with `tools/mesh_poc.py listen` (or the sniffer) running, on
   the DALI tunable-white light, a dimmer insert and a switch insert: `lightness <el> 6553 --transition 3`, back to
   65535 with `--transition 3`; `ctl … --transition 3`; `scene FFFF <n> --transition 3`. Record whether the Status
   carries target + remaining time and whether the light fades. If the DALI insert ignores Lightness transitions,
   try Generic Delta Set with a transition on its element. Write the results into `docs/hidden-features.md` (new
   subsection).
3. Build from the probe: a per-kind `TRANSITION_KINDS` table in `light.py`; `supported_features |= TRANSITION` only for
   those kinds; hub setters take `transition=None`; after a Set with a transition, refresh the element at remaining
   time + 1 s when the Status carried one; the scene entity passes `transition` (one for all nodes); *All lights*
   passes it on its unacknowledged Set; kinds without support ignore the kwarg silently (never raise).
4. Key scenes: `assign_key(scene=…, transition=…)` writes `0x5002` with milliseconds — only if the probe shows a
   key-recalled scene fades and brief 30 verified F15.
5. Parity ledger rows for `824c`, `825e`, `8264`, `8242/8243`, `8202`.

## Tests to add

Byte-exact builders with and without transition; `encode_transition` round trip, resolution boundaries and the 63
refusal (Hypothesis); light feature flag per kind, Set bytes, re-read scheduled from a Status with remaining time
(frozen clock); scene with transition; `0x5002` payload; CLI option parsing (`tests/test_cli.py`).

## Acceptance criteria

Gates green; ledger rows updated; if the probe cannot be run, ship the library and CLI part only (no entity feature).

## Verifiable on air here?

Yes, with the DALI tunable-white light, dimmer and switch inserts and scenes; the probe needs someone watching.

## Risks / off-by-default / "unverified on air"

Low (a fade or no fade). An unexpected ignored Set would surface as a timeout — test each kind first. Kinds not in
`TRANSITION_KINDS` keep today's bytes.

## Depends on

30 (F15 verified) for the key-scene part only.

## Files touched

`jhmesh/messages.py`, `coordinator.py`, `light.py`, `scene.py`, `mesh_config.py`, `tools/mesh_poc.py`,
`docs/hidden-features.md`, `docs/parity/ledger-msg.json`, `tests/jhmesh/test_messages.py`, `tests/test_light.py`,
`tests/test_scene.py`, `tests/test_cli.py`, `CHANGELOG.md`, `docs/ha-integration.md`.
