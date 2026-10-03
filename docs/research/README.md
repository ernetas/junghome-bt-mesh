# Reverse engineering the JUNG HOME Bluetooth Mesh system

JUNG HOME (Albrecht JUNG GmbH, Bluetooth SIG company ID `0x0527`) is a Bluetooth SIG Mesh smart-home system.
The vendor apps (iOS/Android, v2.2.0) are built on Nordic's nRF Mesh library and store the whole network in the
standard Mesh CDB JSON format, which makes the system unusually approachable: with an app backup you already hold
every key, and the Android APK contains the vendor-model definitions in readable Kotlin.

Goal: understand the mesh layer well enough to control the devices from third-party software without the JUNG Gateway
(complementing the gateway-API based Home Assistant integration at https://github.com/ernetas/junghome).

The Home Assistant integration is what came out of it; this page is the research behind it, moved here from
the repository's front page.

## The notes

What was learnt, and how. These pages are records of the research: the [user guide](../user/README.md) and the
[reference](../ha-integration.md) say what the integration does today.

| Page | What it holds |
|---|---|
| [poc-gatt-proxy.md](../poc-gatt-proxy.md) | The proof of concept: design, run log and on-air findings (button events included) |
| [sniffer.md](../sniffer.md) | The nRF Sniffer setup, the capture and decode pipeline, what the first captures established |
| [on-air-sweep.md](../on-air-sweep.md) | The on-air verification checklist: what is still unverified, how to check it, pass criteria |
| [hidden-features.md](../hidden-features.md) | What the devices expose beyond the app: compositions, on-air property lists, unused SIG / Config states |
| [bluetooth-recheck.md](../bluetooth-recheck.md) | The sweep of every Bluetooth layer (advertising, GATT, mesh, models, Home Assistant side): verified vs open |
| [ios-app-data.md](../ios-app-data.md) | File formats, and everything learnt from the iOS app's data |
| [network-topology.md](../network-topology.md) | Generated (keys redacted, identifiers pseudonymised): nodes, elements, models, publications and subscriptions, rooms, scenes |
| [hop-matrix.md](../hop-matrix.md) | Measured: mesh hops between every pair of nodes (Heartbeat Subscription probe) |
| [android/vendor-models.md](../android/vendor-models.md) | The vendor models `0x0527:xxxx` and their messages (from the APK) |
| [android/properties.md](../android/properties.md) | The property-ID catalogue and enums (from the APK) |
| [android/network-logic.md](../android/network-logic.md) | Address allocation, connections, control paths (from the APK) |
| [android/transport-provisioning.md](../android/transport-provisioning.md) | Bluetooth discovery, provisioning, proxy, security (from the APK) |
| [android/firmware-products.md](../android/firmware-products.md) | The bundled firmware images, their product IDs and chip family |
| [gap-analysis/](../gap-analysis/) | Everything the app can do against what is implemented: [control and state](../gap-analysis/control-and-state.md), [device settings](../gap-analysis/device-settings.md), [network features](../gap-analysis/network-features.md) |
| [roadmap.md](../roadmap.md) | The consolidated to-do list towards "configure JUNG HOME without the app", and what is not worth doing |
| [cross-repo-analysis.md](../cross-repo-analysis.md) | The audit against the gateway integration and the gateway firmware: settled facts, divergences, the tracker |
| [parity/README.md](../parity/README.md) | The parity ledger: every message, property, product setting and screen of the app, and how this repository covers it |
| [review-1](../review-1/README.md), [review-2](../review-2/plan.md), [review-3](../review-3/plan.md), [review-4](../review-4/plan.md) | The code reviews: findings, plans and briefs |

## Running the PoC

The command-line tools work on the same export as the integration, from a checkout of this repository.

```
python3 -m venv .venv && .venv/bin/pip install bleak cryptography
.venv/bin/python tools/mesh_poc.py scan
.venv/bin/python tools/mesh_poc.py listen --seconds 60      # decrypted mesh sniffer
.venv/bin/python tools/mesh_poc.py get WC                   # room group or hex element address
.venv/bin/python tools/mesh_poc.py set 0148 on
.venv/bin/python tools/mesh_poc.py prop lists 0173               # which properties an element really serves
.venv/bin/python tools/mesh_poc.py config audit 0148              # export vs what the node really holds (Gets only)
.venv/bin/python tools/mesh_poc.py scene-actions 016A             # what the element does in each scene (not in the export)
.venv/bin/python tools/mesh_poc.py health FFFF                    # registered Health faults of every node
.venv/bin/python tools/mesh_poc.py config hops 0148 01A4          # mesh hops between two nodes (heartbeat probe, restored)
.venv/bin/python tools/mesh_poc.py config hopmatrix --json hops.json  # … between every pair (docs/hop-matrix.md, ~10 min)
.venv/bin/python tools/mesh_poc.py provision --scan               # unprovisioned devices: Device UUID, OOB info, product
.venv/bin/python tools/mesh_poc.py provision <uuid> --unicast 0D10 --yes  # provision only (No OOB), device key to a 0600 file
```
With a Nordic nRF Sniffer dongle next to the installation, `tools/mesh_sniff.py` records the whole advertising bearer
without joining the mesh (no address, no sequence numbers, no key on the capture host) and decodes it with the export's
keys — live over ssh or from a file / Nordic pcap (`docs/sniffer.md`).

Our own node identity (address + sequence number) is kept per address in `tools/.jhmesh_state_<ADDR>.json`
(owner-only: while a key refresh is followed it holds the new network key; an older, world-readable one is made
owner-only when loaded) — do not delete it (reusing sequence numbers gets our messages dropped by replay protection; if lost, pick a new address with
`--source`). The CLI defaults to `7FFF`; the Home Assistant integration uses `0D00` with its own store — never let two
clients send from the same address, each keeps its own counter and the nodes drop whichever lags. With
`--ha-storage <config>/.storage` the CLI also refuses every other address Home Assistant's store of the mesh holds.

## Reproducing the APK decompile

```
apkeep -a de.jung.junghome -d apk-pure android/        # fetches de.jung.junghome.xapk
unzip android/de.jung.junghome.xapk -d android/xapk
jadx -j 8 --show-bad-code -d android/jadx-out android/xapk/de.jung.junghome.apk
```
JUNG's code is under `de/jung/junghome/**` with class names intact; Nordic's library under `no/nordicsemi/android/mesh/**`.

## Status

The project's log, oldest first.

- [x] iOS dump fully decoded (`docs/ios-app-data.md`, `docs/network-topology.md`)
- [x] Android APK obtained and decompiled
- [x] Vendor models / opcodes documented from source (`docs/android/vendor-models.md`)
- [x] Property catalogue (`docs/android/properties.md`)
- [x] Network wiring logic (`docs/android/network-logic.md`)
- [x] Transport / provisioning (`docs/android/transport-provisioning.md`)
- [x] **Proof of concept: Bluetooth-only control works** — `tools/mesh_poc.py` switches lights, reads vendor properties and
      decodes all mesh traffic through any JUNG node's GATT proxy using only the exported keys (`docs/poc-gatt-proxy.md`)
- [x] Button events captured and decoded (vendor property `0x5012`: click / hold start / hold end)
- [x] Passive on-air capture with an nRF Sniffer (`tools/mesh_sniff.py`, `jhmesh.sniffer`): first captures confirmed the
      acked-Set reply rule from outside the network, corrected the gateway's TTL (5) and refined the publication doubling
      (`docs/sniffer.md`)
- [x] Device capabilities beyond the app (`docs/hidden-features.md`): on-air property lists (`prop lists`), the socket
      energy counters → **energy sensors**, Config Heartbeat → the **Node heartbeats** option (per-device availability),
      Health Attention → **Identify** button, Health faults → **Fault** binary sensor + **Clear faults** per node; nodes advertise
      from their MAC with a JUNG record (`jhmesh.advert`) →
      proxies named at once, **repair issue for nodes missing from the export**
- [x] Firmware corners (`docs/hidden-features.md` §10): the JUNG **Scene Action Setup** and **JH Scheduler** models
      decoded and verified on air (`jhmesh.vendor_models`, `scene-actions` / `sched` — the per-scene action the export
      lacks), Health faults surveyed / cleared / tested (`health`), Heartbeat Subscription as a **hop counter**
      (`config hops`), the meter's `5014` timestamp, `000E` as a dimmer's one-message state refresh, why the gateway's
      Mesh 1.1 models never answer
- [x] Roadmap step 12, scenes: `create_scene` / `store_scene` / `remove_from_scene` / `rename_scene` / `delete_scene`
      actions (Scene Store + JUNG Scene Action Setup + export write-back, verified live), scene entities list what every
      member does
- [x] Roadmap step 13, schedules: `get_schedules` / `create_schedule` / `update_schedule` / `enable_schedule` /
      `disable_schedule` / `delete_schedule` on the loads' own JH Scheduler (time, sunrise, sunset; the home location
      sent for astro schedules) and a *Schedules* sensor per load; metering-socket thresholds (`set_threshold` /
      `delete_threshold`, two sensors per socket) — not yet tried on a device
- [x] Roadmap step 14, gateway sync: every rewritten export is handed to the gateway as the app does (`sync_gateway`
      retries), and an entry set up from the gateway fetches the gateway's export by itself when an unknown node of the
      mesh advertises — new devices appear without user action (quality scale `dynamic-devices` done)
- [x] `jhmesh` hardened: segmented TX/RX with acks (tested on air), IV-update handling, reconnect loop, device model
- [x] Home Assistant integration scaffolded (`custom_components/junghome_ble`, import-checked vs HA 2026.9.2, hub logic unit-tested) and documented per the Integration Quality Scale `docs-*` rules — `docs/ha-integration.md`
- [x] Gap analysis of the whole app (`docs/gap-analysis/`) and consolidated `docs/roadmap.md`
- [x] Live test of the integration in HA (ESPHome proxies on the maintainer's installation; first run,
      energy sensors, node heartbeats on every node and the Identify button verified live)
- [x] Roadmap step 0/1: vendor property Set + config entities (`number` / `select` / `switch` / `button` from the
      codec table), DevKey config messages, project-file write-back (rooms and key connections as HA actions)
- [x] Roadmap step 2: blinds (cover), thermostat (climate), detectors (motion / occupancy, illuminance), battery
      transmitters (battery level) — implemented from the specifications and the gateway firmware, **unverified on
      hardware** (the maintainer owns none of these devices; reports welcome)
