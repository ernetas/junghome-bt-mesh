# junghome-bt-mesh — JUNG HOME over Bluetooth Mesh, without the gateway

**For Home Assistant users:** `junghome_ble` controls JUNG HOME lights, sockets, blinds, push-buttons, thermostats and
detectors directly over Bluetooth Mesh — through Home Assistant's Bluetooth adapter or an ESPHome Bluetooth proxy, no
JUNG Gateway needed (it works next to one too, and keeps it in sync).

- **Install:** HACS → *Integrations* → ⋮ → *Custom repositories* → this repository's URL, category *Integration*;
  install *JUNG HOME (Bluetooth Mesh)* and restart Home Assistant. Manual install: copy
  `custom_components/junghome_ble/` (the mesh stack is inside it) into your configuration's `custom_components/`.
- **Set up:** *Settings → Devices & services → Add integration → JUNG HOME (Bluetooth Mesh)*, then fetch the network
  from your JUNG HOME Gateway, or upload the export the JUNG HOME app shares (*JungHome.json*). Everything else —
  devices, rooms, scenes, key connections, schedules — comes from that export.
- **What you get, and what is experimental:** [docs/ha-integration.md](docs/ha-integration.md) — devices and entities,
  actions (rooms, key connections, scenes, schedules, thresholds, network audit, adding and removing devices),
  diagnostics, repairs and troubleshooting.
- **Security:** the export holds every key of your mesh; see [SECURITY.md](SECURITY.md).

The rest of this README is the reverse-engineering project behind it.

---

## Reverse engineering the JUNG HOME Bluetooth Mesh system

JUNG HOME (Albrecht JUNG GmbH, Bluetooth SIG company ID `0x0527`) is a Bluetooth SIG Mesh smart-home system.
The vendor apps (iOS/Android, v2.2.0) are built on Nordic's nRF Mesh library and store the whole network in the
standard Mesh CDB JSON format, which makes the system unusually approachable: with an app backup you already hold
every key, and the Android APK contains the vendor-model definitions in readable Kotlin.

Goal: understand the mesh layer well enough to control the devices from third-party software without the JUNG Gateway
(complementing the gateway-API based Home Assistant integration at https://github.com/ernetas/junghome).

## Disclaimer & legal

This is an **independent, unofficial** project. It is **not** affiliated with, authorized, sponsored, or endorsed by
Albrecht JUNG GmbH & Co. KG. "JUNG", "JUNG HOME", and "LB Connect" are trademarks of their respective owner and are
used here **only descriptively** (nominative use) to identify the devices this software interoperates with.

- **Purpose — interoperability.** The code here is an independent implementation written against the public Bluetooth
  SIG Mesh specification, so that owners can operate **their own** devices without the vendor gateway. In the EU,
  studying, observing, and — where necessary — decompiling software to achieve interoperability of an independently
  created program is expressly permitted (Directive 2009/24/EC, Arts. 5(3) and 6), and Art. 8 renders contract terms
  that purport to forbid it unenforceable for that purpose.
- **No vendor software is redistributed here.** This repository contains **no** decompiled app source and **no**
  firmware images. The local `android/` decompile and the `ios/` app backup (which contains network keys) are
  developer-only inputs and are git-ignored — do not commit them. The integration's icon
  (`custom_components/junghome_ble/brand/`) is the JUNG HOME brand image as Home Assistant's brands repository
  publishes it for custom integrations; the JUNG and JUNG HOME names and marks belong to their owner.
- **Use with your own devices only.** Operating a Bluetooth Mesh network requires the network's keys, which belong to
  its owner. Use this only on devices and networks you own or are authorized to manage. You are responsible for your
  use of it.
- **No warranty.** Provided "as is" under the MIT License, without warranty of any kind. Interacting with device
  firmware carries risk (misconfiguration, loss of function, or voided manufacturer warranty). **Use at your own
  risk.**

See [DISCLAIMER.md](DISCLAIMER.md) for the full notice.

## Layout

```
ios/AppDomain-de.jung.junghome/   iOS app backup (KEYS INSIDE — never publish)
android/                           JUNG HOME 2.2.0 XAPK from APKPure + jadx decompile (android/jadx-out)
custom_components/junghome_ble/    Home Assistant integration (HA Bluetooth stack / ESPHome proxies), the mesh stack inside:
custom_components/junghome_ble/jhmesh/  Bluetooth Mesh stack: crypto, PDUs (segmentation), GATT-proxy client, device model (PyPI `jhmesh`)
jhmesh                             symlink to the above, so the CLI tools import it as top-level `jhmesh`
pyproject.toml, MANIFEST.in        the `jhmesh` sdist + wheel (README-pypi.md is its PyPI page); ruff, mypy, pytest, coverage settings
scripts/package_ha.sh              builds dist/junghome_ble.zip for unzipping into HA's custom_components/
tools/mesh_report.py               renders docs/network-topology.md from the iOS dump
tools/mesh_poc.py                  CLI: scan / listen (sniffer) / get / set / blink / lightness / ctl / scene / scene-actions / sched / health / prop / config / devices
tools/mesh_sniff.py                passive capture with a Nordic nRF Sniffer dongle (key-free) + offline/live decoding
docs/poc-gatt-proxy.md             PoC design, run log and on-air findings (incl. button events)
docs/sniffer.md                    nRF sniffer setup, the capture/decode pipeline, what the first captures established
docs/hidden-features.md            what the devices expose beyond the app: compositions, on-air property lists, unused SIG/Config states
docs/bluetooth-recheck.md          the full sweep of every Bluetooth layer (advertising, GATT, mesh, models, HA side): verified vs open
docs/ha-integration.md             HA integration: user docs (devices, entities, setup, troubleshooting) + developer notes
docs/ios-app-data.md               file formats + everything learned from the iOS dump
docs/network-topology.md           generated: nodes, elements, models, pub/sub wiring, rooms, scenes
docs/hop-matrix.md                 measured: mesh hops between every pair of nodes (Heartbeat Subscription probe)
docs/android/vendor-models.md      vendor models 0x0527:xxxx and their messages (from the APK)
docs/android/properties.md         property-ID catalogue and enums (from the APK)
docs/android/network-logic.md      address allocation, connections, control paths (from the APK)
docs/android/transport-provisioning.md  BLE discovery, provisioning, proxy, security (from the APK)
docs/android/firmware-products.md  bundled firmware images → product IDs, SoC family
docs/gap-analysis/*.md             everything the app can do vs. what we implement (control/state, device settings, network features)
docs/roadmap.md                    consolidated TODO towards "configure JUNG HOME without the app"
docs/cross-repo-analysis.md       audit vs the gateway integration + firmware dump: settled facts, divergences, bug/improvement tracker
tests/                             HA integration + mesh library test-suites (synthetic keys; 100 % line coverage of the package + integration and of the CLIs)
```

## Running the PoC

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
clients send from the same address, each keeps its own counter and the nodes drop whichever lags.

Tests (mesh library spec vectors + segmentation round trip, HA integration against a simulated proxy, registry
snapshots and translation lockstep; 100 % line coverage of `custom_components/junghome_ble` — the package and the
integration — and of `tools/`; CI gates 99.8 % line + branch overall, every integration line, and every line and branch
of the installed `jhmesh` wheel from `tests/jhmesh` alone on Python 3.13): `.venv/bin/python -m pytest tests -n auto`
after `.venv/bin/pip install -r requirements-test.txt` (Home Assistant and the whole test runner at the versions
`pytest-homeassistant-custom-component` pins, plus mypy: `.venv/bin/python -m mypy`). A `DeprecationWarning` fails the
test that triggers it (`pyproject.toml`). The fixtures
are synthetic throughout: the keys (`00112233…`-style patterns) and the identities — node UUIDs are EUI-64s over MACs
from the IANA documentation block `00:00:5E:00:53:xx` (RFC 7042), the provisioner UUID is a fixed fake one — and every
fixture regenerates byte-for-byte from `tests/fixtures/make_*.py`. Real identifiers (MACs, node UUIDs, the phone's
provisioner UUID, room names) appear only in the documentation of the maintainer's installation, above all
`docs/network-topology.md`; `docs/hop-matrix.md` names nodes by unicast address only.
Lint and formatting: `.venv/bin/python -m ruff check .` and `.venv/bin/python -m ruff format .` (the ruff that
`requirements-lint.txt` pins, as CI's `lint` job installs it: `.venv/bin/pip install -r requirements-lint.txt`;
configured in `pyproject.toml`; the `lint` job gates the others).

## Releases

HACS offers the repository's GitHub releases as installable versions, so every version is a tag: bump `"version"` in
`custom_components/junghome_ble/manifest.json` and `pyproject.toml` (the `jhmesh` library carries the same version),
date the version's `CHANGELOG.md` heading (it replaces "(unreleased)"), merge to `main`, then
`git tag vX.Y.Z && git push origin vX.Y.Z` on the merged commit. `.github/workflows/release.yml` re-runs the whole CI
(lint with actionlint, shellcheck and zizmor on the workflows and scripts, strict typing, tests, the `jhmesh` sdist +
wheel build and `twine check --strict` with the library tests on the oldest Python `requires-python` admits, floor
import, hassfest, HACS validation, the manual-install zip) against the tagged commit, refuses a tag that is not on
`main`, does not match either version or has no dated CHANGELOG section, creates the GitHub release with that
CHANGELOG section as its notes and `junghome_ble.zip` (the manual-install package from `scripts/package_ha.sh`)
attached, and only then publishes the `jhmesh` sdist + wheel to PyPI (job `pypi`, after `release`, so a refused tag
never burns an immutable PyPI version). Both uploads are the files CI built and checked in that run, never a rebuild:
CI stores them as artifacts only on a tag (a branch push or a pull request uploads nothing, so a full artifact quota
cannot fail it). The tag checks and the release notes are `scripts/release_checks.sh`, which
`tests/test_release_checks.py` runs on every push.
PyPI takes them by trusted publishing — no token in the repository: PyPI has to list this repository, `release.yml`
and the `pypi` environment as the project's trusted publisher. Before the first release no `jhmesh` project exists
yet, so that is a *pending* publisher, added once under the PyPI account's *Publishing* page
(<https://pypi.org/manage/account/publishing/>); it does not reserve the name until the first upload
(`docs/roadmap.md`). What gates: every HACS validator except, while the repository is not public, `hacsjson` and
`integration_manifest` (they download the raw files without the token) — see the `validate` job in `ci.yml`. A suffixed tag (`v1.1.0b1`, `v1.1.0rc1`) becomes a *pre-release*, which
HACS only shows to users who enabled beta versions for the repository. The zip is not declared as a HACS `zip_release`
on purpose: it carries a top-level `junghome_ble/` folder for unzipping into `custom_components/`, whereas HACS would
extract such an asset straight into `custom_components/junghome_ble/`; HACS installs from the tagged tree instead.
Dependency pins are kept current by Renovate (`renovate.json`; a new release is proposed once it is three days old,
a digest that follows a branch or a moving tag at once): the action SHAs (each at a release tag; hassfest, which has none, along its default branch), the digest of the HACS
validation image, the tags and digests of the actionlint and zizmor images, `requirements-lint.txt` (ruff),
`requirements-test.txt` (the Home Assistant test stack, mypy), `requirements-build.txt` (build, twine) and the
setuptools that `pyproject.toml` builds with. The library job's Python is read from `pyproject.toml`
`requires-python`, so there is no version there for Renovate to move.

## Reproducing the APK decompile

```
apkeep -a de.jung.junghome -d apk-pure android/        # fetches de.jung.junghome.xapk
unzip android/de.jung.junghome.xapk -d android/xapk
jadx -j 8 --show-bad-code -d android/jadx-out android/xapk/de.jung.junghome.apk
```
JUNG's code is under `de/jung/junghome/**` with class names intact; Nordic's library under `no/nordicsemi/android/mesh/**`.

## Status

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
- [x] Roadmap step 13, schedules: `get_schedules` / `create_schedule` / `enable_schedule` / `disable_schedule` /
      `delete_schedule` on the loads' own JH Scheduler (time, sunrise, sunset; the home location sent for astro
      schedules) and a *Schedules* sensor per load; metering-socket thresholds (`set_threshold` / `delete_threshold`,
      two sensors per socket) — not yet tried on a device
- [x] Roadmap step 14, gateway sync: every rewritten export is handed to the gateway as the app does (`sync_gateway`
      retries), and an entry set up from the gateway fetches the gateway's export by itself when an unknown node of the
      mesh advertises — new devices appear without user action (quality scale `dynamic-devices` done)
- [x] `jhmesh` hardened: segmented TX/RX with acks (tested on air), IV-update handling, reconnect loop, device model
- [x] Home Assistant integration scaffolded (`custom_components/junghome_ble`, import-checked vs HA 2026.9.2, hub logic unit-tested) and documented per the Integration Quality Scale `docs-*` rules — `docs/ha-integration.md`
- [x] Gap analysis of the whole app (`docs/gap-analysis/`) and consolidated `docs/roadmap.md`
- [x] Live test of the integration in HA (ESPHome proxies on the maintainer's installation; first run,
      energy sensors, node heartbeats on all 29 nodes and the Identify button verified live)
- [x] Roadmap step 0/1: vendor property Set + config entities (`number` / `select` / `switch` / `button` from the
      codec table), DevKey config messages, project-file write-back (rooms and key connections as HA actions)
- [x] Roadmap step 2: blinds (cover), thermostat (climate), detectors (motion / occupancy, illuminance), battery
      transmitters (battery level) — implemented from the specifications and the gateway firmware, **unverified on
      hardware** (the maintainer owns none of these devices; reports welcome)
