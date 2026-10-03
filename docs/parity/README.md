# Parity ledger: the JUNG HOME app and the air vs this integration

A closed-list comparison. Each domain has an **inventory**, with one item per thing the app (JUNG HOME Android 2.2.0,
decompiled with jadx into the gitignored `android/jadx-out/`) or the air (decoded sniffer captures) has. Each domain
also has a **ledger**, with exactly one row per inventory item that says how this repository covers it.
`tests/test_parity.py` fails when an item has no row, when a row has no decision, or when a row cites code or tests
that do not exist. A rerun of the review therefore reports something new only when its inputs change (a new app version,
a new message type on air). Each new item then gets a row, decided with the rules below.

| Domain | Inventory | Ledger | What it enumerates |
|---|---|---|---|
| `msg` | `inventory-msg.json` | `ledger-msg.json` | every access / proxy message the app encodes or parses |
| `prop` | `inventory-prop.json` | `ledger-prop.json` | every SIG / JUNG property id the app reads or writes |
| `prod` | `inventory-prod.json` | `ledger-prod.json` | products, inserts, key modes, and each product's settings |
| `net` | `inventory-net.json` | `ledger-net.json` | network configuration: rooms, connections, scenes, timers, gateway, sharing |
| `mgmt` | `inventory-mgmt.json` | `ledger-mgmt.json` | provisioning, keys, IV, sequence, proxy, SAR, DFU, time, health, timings |
| `ui` | `inventory-ui.json` | `ledger-ui.json` | what a user sees and does at runtime, per device type |
| `air` | `inventory-air.json` | `ledger-air.json` | every distinct message type seen on air (`captures-inventory.json`) |

Ids are mechanical (`<domain>:<kind>:<key>`: an opcode, a property id, a class or resource name). They never change
with wording. Each inventory's `method` field records how it was enumerated.

## Row schema

```json
{"id": "...", "status": "implemented|partial|gap|na", "code": ["path::symbol"], "tests": ["tests/...::test"],
 "missing": "partial/gap: exactly what is missing",
 "class": "build|ha-native|internal|declined|dup",
 "verify": "offline|on-air|absent-hardware",
 "reason": "na: why, with a decision-record or design citation",
 "dup_of": "id (class dup only)"}
```

- `implemented` means code plus a test that exercises it. Code without a test is `partial` with `missing: "test"`.
- `class` is required for every row that is not `implemented`, and `verify` for every `build` row.
- An `implemented` row whose only counterpart is a command-line tool (`tools/`) says so: its `note` starts with
  `CLI only:` and names the command. One whose only gap is an on-air check stays `implemented`, with a `note`
  starting `Unverified on air` that says what to check.
- A `declined` row's `reason` cites its decision record; the review-4 declines are one line each, with their
  evidence, in `docs/roadmap.md` under *Not worth doing*.
- A `code` citation names a symbol, not a line: `path::symbol`, where `path` is relative to the repository root
  and `symbol` is something the file defines. In a `.py` file that is a function, class or method qualified by
  what encloses it (`coordinator.py::JungHomeHub._refresh_all`, `coordinator.py::register_status_handler.register`),
  or an assignment at module or class level (`const.py::REQUEST_ATTEMPTS`, `Class.attr`); a function's locals are
  not symbols. In a `.json` file it is a key's dotted path (`strings.json::exceptions.device_not_reachable`).
  `path:line::symbol` may point into a long symbol; the line must then lie within the symbol or at most 5 lines
  above it (decorators, a comment). Code added above a symbol changes none of its citations, and a line that
  drifted out of its symbol is reported instead of silently naming the neighbour. Prose (`missing`, `reason`,
  `note`) uses the same form.

## Classification rules (apply in this order; the first match wins)

1. **dup**: the same behaviour is already a row under another id, in this or another domain. For example, a UI
   string that names a setting already listed under `prod`, or an interactor whose only effect is a message
   already listed under `msg`. Set `status: na`, `class: dup`, `dup_of`.
2. **declined**: a decision record (`docs/review-*/plan.md`, `docs/roadmap.md`, `docs/ha-integration.md`,
   `CHANGELOG.md`) deliberately left this out. Examples: a feature that stays with the app, or N6/N9/N10/N11/N13 in review-3.
   Set `status: na`, `class: declined`, and a `reason` that cites the record. Without a citable record the row is
   not declined.
3. **ha-native**: app chrome that Home Assistant provides itself, or that has no meaning in HA. Examples:
   favourites, icons, navigation, onboarding tutorials, snackbars, dialogs, localisation strings, account or cloud
   login, app settings, the app's own DB tables. Set `status: na`, `class: ha-native`. An error *condition* the
   app reports (as opposed to its dialog text) is not ha-native: HA needs its own error for it.
4. **internal**: an implementation detail of the app with no observable effect on the mesh or the user. Examples:
   a debug screen, a logging constant, a DB migration, a coroutine wrapper. Set `status: na`, `class: internal`.
   A timing, retry count or timeout that changes on-air behaviour is **not** internal: compare it with ours, and if
   ours is deliberately different, record why in `reason` (status `implemented` if ours is justified and tested).
5. **build**: everything else. It stays `gap` or `partial`, with `missing` saying what to build. `verify` says
   whether it can be proven offline (unit or simulated-mesh test), needs an on-air check on this installation, or
   needs hardware this installation lacks (puck, blinds, RTR, detectors, battery devices).

## Keeping it closed

- `tools/parity.py check` does what the test does (every id has a row, every row a decision, every `code` symbol is
  defined in its file and every `tests` function exists), and prints the rows that are still `build`.
- `tools/parity.py air <decoded.json>...` lists (kind, opcode, property) tuples of decoded captures that are not in
  `inventory-air.json`: run it on the sniffer host after `mesh_sniff.py decode --json`.
- `tools/parity.py apk <jadx-out>` re-extracts the mechanical anchors (opcode literals, property ids, interactor /
  fragment / view-model classes, string resources, update descriptors) and lists every anchor that
  `anchors.json` does not map to an inventory id. On a new app version, run it, add items for the unmapped
  anchors, and decide their rows.
