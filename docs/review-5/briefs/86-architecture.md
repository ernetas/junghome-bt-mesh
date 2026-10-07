# 86 — Architecture: the start decision into the store, shims, shared matching, API surface

Review 5 · Wave 25 · Closes: A5-1, A5-3, A5-5, A5-6 (decision M18), A5-7.

Follow the [conventions](../../review-4/briefs/README.md#conventions) in full, with these additions: CHANGELOG bullets
go under `## 1.5.0 (unreleased)` (create it above the newest released section; never edit released sections); every
new "unverified on air" marker is cited in `docs/on-air-sweep.md`. The findings are summarised in
[`../plan.md`](../plan.md); the review reports with each finding's full evidence are handed over in the prompt.

## Goal

Behaviour-identical moves; snapshots unchanged.

## Steps

1. A5-1: where an address's sequence numbers start (store, backup, floor, the issue, the restore skip, the
   evidence-of-use start, one hub per mesh) moves from the hub into `seq_store`.
2. A5-3: production code imports from the defining modules; the shims stay for tests only (or go).
3. A5-5: the pre-flight's Get / Status matching and the export reading share the audit's helpers.
4. A5-6 / M18: names used by nothing but their module and the pin leave `__all__`; the pin follows.
5. A5-7: `jhmesh.vaultrefresh`'s logger name.
6. Improvements: one node-row snapshot for *Mesh overview* and *Mesh topology*; `AccessMessage` into its own module;
   SAR out of `ProxyClient` into `jhmesh/sar.py`.

## Tests to add

Snapshots and golden files unchanged; `tools/import_graph.py --lazy --check` clean.

## Files touched

`coordinator.py`, `seq_store.py`, `jhmesh/`, `services.py`, `sensor.py`, `mesh_topology.py`
