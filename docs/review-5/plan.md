# Review 5 — findings and plan

## Scope and method

Nine parallel read-only reviews of the whole repository after review 4's waves 1–23 had landed (1.4.0 and the
LED night-mode and topology-text fixes after it), split as review 4 was: protocol `P5`, state `S5`, runtime `R5`,
configuration `W5`, Home Assistant `H5`, functionality `F5`, quality `Q5`, architecture `A5`, UX `U5`. Each reviewer
reported only new findings, or review-4 fixes that are incomplete or regressed, pinned to code in the current tree,
reproduced in a scratch copy where possible. The reports themselves were working notes and are not kept; every finding
is summarised below and carried into a brief.

Counts: **high 1 · medium 20 · low 41** (62 findings; two pairs describe the same defect, P5-3 = S5-3 and the
`force` overlap U5-8 / a W improvement). Incomplete or regressed review-4 items: D2 / W4-1 (W5-1), A4-1 (A5-1), A4-17
(A5-4), Q4-3 / Q4-14 / Q4-18 (Q5-4), Q4-4 (Q5-6), Q4-20 (Q5-7), F4-1 (F5-4), F4-12 (F5-3).

## Findings

### High

| ID | Defect | Fix | Brief |
|---|---|---|---|
| S5-1 | A record restored from a Home Assistant backup skips a fixed 2^20 numbers whatever the backup's age; the integration's own periodic traffic (energy polls, heartbeat publication Sets to dead nodes) passes 2^20 within months, so an old backup reuses nonces and Home Assistant goes mute | Record the wall time and a send-rate checkpoint per record; on a restore skip `max(2^20, 2 × rate × age)`, refuse with a repair when that would pass the limit; correct the `const.py` rationale | 78 |

### Medium

| ID | Defect | Fix | Brief |
|---|---|---|---|
| S5-2 | A record restored in the middle of Home Assistant's own IV Update completes it at the next link through a path without the pending-guard check | One rule every IV-state move goes through, with the guard | 79 |
| S5-3 = P5-3 | An IV Update Home Assistant started that the mesh never takes stays *in progress* for good: beacons forever, every new start refused, `sequence_space_low` cleared, no repair | A deadline (§3.11.5's 144 h): revert or surface a repair; an abort action | 79 |
| P5-1 | Home Assistant returns to Normal Operation 96 h after it *started* the update, not after the mesh *took* it | Measure from the confirmation | 79 |
| P5-2 | IV Update beacons ignore a key refresh that starts during the window (new key, Key Refresh flag 0) | Beacon with the phase's key and flag | 79 |
| S5-4 | A backup restore rolls the vault and the export back together; a device provisioned after the backup loses its device key and its unicast block is handed out again | Allocation avoids every source heard on air (RPL, last seen); document the rollback in `backup.py` | 78 |
| W5-1 | Room allocation ignores the vault's reserved element groups (D2 / W4-1 half fixed) | One reservation provider for every allocator | 80 |
| W5-2 | `room_area` resolves a room by area name only, ignoring the rooms→areas mapping and aliases; with `create` it makes a new mesh room | One per-entry room resolver | 80 |
| F5-2 | `store_scene` on channel 2 of a two-channel node without a Scene Action Setup server stores channel 1 and records it as the member | Refuse as the app does | 80 |
| R5-1 | After a double click, a further press within the window makes another `double_click` | Clear the last click once it completed a double click | 81 |
| H5-1 | Following an export in place leaves Home Assistant's cached name properties, so a kept entity keeps its old name | Clear the `cached_property` values too | 82 |
| U5-2 | The gateway discovery card is suppressed only by an exact `gateway_host` match: file entries, `junghome.local` and DHCP changes slip through | Match by the gateway's serial / mesh, not the host | 82 |
| U5-1 | 28 texts send users to *Devices & services → JUNG HOME*, which since M15 is the gateway integration's name | Say *JUNG HOME Bluetooth Mesh*, every language | 83 |
| U5-3 | The download link is a bearer URL for five minutes; the texts say it works only for the administrator who asked | Say what it is; answer an absolute URL | 83 |
| U5-4 | The FAQ tells a *file on the host* entry it has a copy of its own; it has not | Correct the FAQ | 83 |
| F5-1 | The time-keeper ledger rows still call a built feature a gap | Flip the rows; guard against stale ledgers | 84 |
| Q5-1 | `settle` counts a loop with only timers pending as idle; the connect-time work continues after it returns | Wait for the hub's work, not the loop | 85 |
| Q5-2 | Under `fast_sleep` the IV beacon loop never sleeps, `settle` burns its whole turn cap | Fail on an exhausted cap unless marked | 85 |
| Q5-4 | The public history still carries what brief 64 scrubbed from the tree (private IPs, MACs including one real push-button, UUIDs, home paths); `--history` checks only trailers and e-mail | `--history` scans added lines; the rewrite is decision M16 | 85, M16 |
| A5-1 | Where an address's sequence numbers start is still decided in the hub module (A4-1 incomplete) | Move it into `seq_store` | 86 |

### Low

| ID | Defect (one line) | Brief |
|---|---|---|
| S5-5 | Store, backup and floor all gone but the address used: the start skips 2^20 from 0, not `SEQ_SKIP_UNKNOWN` | 78 |
| P5-4 | The audit's client-model list holds `1009` (a server) and lacks `1008` | 84 |
| W5-3 | `_answer` raises on two entries' dry runs that both carry `preflight` | 80 |
| W5-4 | `set_threshold` writes the threshold before the pre-flight | 80 |
| W5-5 | Schedule create / update are not cancel-safe | 80 |
| W5-6 | `record_node` ignores the vault save result | 80 |
| W5-7 | The plan-journal replay at setup can race a plan still running | 80 |
| R5-2 | The Time Set retried while the store stalls resends the old timestamp | 81 |
| R5-3 | The property reader does not wait for the state refresh, and not at all on a later link | 81 |
| R5-4 | `cancel_all` runs before the link is detached; late key events arm timers nothing cancels | 81 |
| R5-5 | "Link up" is `proxy.connected`, before the beacon wait and the filter write (`ready` unused) | 81 |
| R5-6 | A gateway entry whose pin is unconfirmed sends a `0xC003` Get and logs a warning on every link | 81 |
| A5-2 | The topology image holds a link change behind its 60 s rate limit (the overview's fixed bug, copied) | 81 |
| H5-2 | `async_update_device(remove_config_entry_id=…)` is deprecated | 82 |
| H5-3 | `start_iv_update` was never translated (16 keys in 25 languages) | 82 |
| H5-4 | `carry_over_conflict` is not deleted on unload or removal | 82 |
| H5-5 | A removed key in `double_click_keys` keeps a MAC-derived id unmasked in the diagnostics | 82 |
| U5-5 | No connectable scanner at all reads as "no node in range" | 82 |
| U5-7 | Every options change reloads the entry, also picking one key for double clicks | 82 |
| U5-6 | Errors name devices by hex mesh address | 83 |
| U5-8 | One `force` means two things on three actions — decided below (M17) | 80 |
| U5-9 | `download_export` hands over a file that predates an open *app changed the installation* | 83 |
| U5-10 | The node device name format in the docs is stale | 83 |
| U5-11 | The German quick start misses the rooms-and-areas step | 83 |
| U5-12 | The README says `jhmesh` is on PyPI; it is not | 83 |
| F5-3 | A push-button advertising another insert keeps a phantom light; the repair is not fixable | 84 |
| F5-4 | The transitions decision has no owner, and B8 shows the planned switches are the wrong shape | 84 |
| F5-5 | Four more stale ledger rows (the SAR ack timer among them) | 84 |
| F5-6 | Two docs still say the DALI insert ignores the CTL Temperature Range Set | 83 |
| F5-7 | The roadmap's "where we are" table is stale | 83 |
| F5-8 | The removal docs call reset-first the app's order; the app unwires first | 83 |
| F5-9 | *Switches off at* can never show a value here; the run-on time is readable | 84 |
| Q5-3 | pytest-cov's per-worker data files are not ignored and race the in-suite privacy scan | 85 |
| Q5-5 | Three ESPHome proxy host names (room, model, half MAC) are in the tree | 85 |
| Q5-6 | Renovate can still move the Python version of the other CI jobs (Q4-4 incomplete) | 85 |
| Q5-7 | A negative check over a real 0.2 s remains; the testing doc says none does (Q4-20 incomplete) | 85 |
| A5-3 | Re-export shims still carry 17 production imports | 86 |
| A5-4 | Review / brief citations back in docstrings, one in the published library (A4-17 regressed) | 85 |
| A5-5 | The pre-flight re-implements the audit's Get / Status matching | 86 |
| A5-6 | The `jhmesh` public API is 1143 names, 251 used by nothing but the pin | 86, M18 |
| A5-7 | `jhmesh.vaultrefresh` names its logger by module path | 86 |

## Improvements taken into the briefs

Rate checkpoint and "days a restore skip covers" in the diagnostics (78); an IV-update initiator state-machine test,
the last mesh IV change seen in a beacon, spec section labels for Mesh Protocol 1.1, the heartbeat `hops` off-by-one
docstring (79); a dry run that reports unreachable and asleep nodes, reachability before the pre-flight reads,
pre-flight outcomes in the plan history (80); topology hop bands without heartbeats where the proxy's own reports
allow, heartbeats as link traffic for the watchdog, `drop_link` not dropping a newer link, a per-link config re-read
timer, a guard in `async_begin_rebuild` (81); Home Assistant warnings as test failures, `download_export` for an
unloaded entry, `cached_text` guarded, `CONFIG_SCHEMA`, the topology picture in Home Assistant's theme rather than
the OS's, a fixable `sequence_space_low`, an absolute download URL, readable pre-flight addresses, the options form
hints (82, 83); *Switches off at* from the run-on time, a fixable `insert_mismatch`, a stale-ledger guard in
`tools/parity.py` (84); `--history` content scan, `settle` failing on an exhausted cap, `package_ha.sh` from tracked
files, a half-MAC scanner pattern (85); one node-row snapshot for the overview and the picture, `AccessMessage` out of
`client.py`, SAR out of `ProxyClient` (86).

## Decisions

- **M16 — rewrite the public history again (Q5-4).** The history of 1.0.0 still holds what the tree no longer does,
  among it one real push-button's MAC with its location. A `git filter-repo --replace-text` rewrite moves every tag
  (v1.0.0–v1.4.0; the releases stay attached, as in the first rewrite). **The maintainer's call**; brief 85 builds the
  scan that lists the values either way.
- **M17 — split `force` (U5-8).** Taken by default: `force` keeps the action's own override; a new `skip_preflight`
  skips the comparison; `force` alone no longer skips it on `remove_from_room`, `delete_scene`, `remove_device`
  (an *Upgrading* note in the CHANGELOG).
- **M18 — shrink the `jhmesh` public API before any PyPI release (A5-6).** Taken by default: names nothing outside
  their module and the pin uses leave `__all__` (they stay importable); the pin follows.
- **M19 — transitions (F5-4).** Taken by default, from sweep B8: a transition only on a brightness change of a DALI
  or dimmer load (a Lightness Set with a transition), never on a switch insert, a CTL Set or an *on*; a scene takes
  Home Assistant's `transition` only when every member fades. Built off by default until a person has watched a fade
  (sweep B8's remaining step).

## Waves

| Wave | Briefs | Notes |
|---|---|---|
| 24 | 78 restore safety, 79 IV Update follow-ups, 80 configurator, 81 runtime, 82 Home Assistant, 83 texts and docs, 84 ledger and functionality, 85 tests and privacy | in parallel; `strings.json` / translations conflicts expected between 80, 82, 83, 84 |
| 25 | 86 architecture | behaviour-identical, after wave 24 |

## Status

Nothing landed yet.
