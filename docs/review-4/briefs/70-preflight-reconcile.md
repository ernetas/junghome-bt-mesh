# 70 — Pre-flight reconcile before destructive plans

Phase P3 · Wave 22 · Size M · Closes: W I4 (pre-flight reconcile before destructive plans).

Follow the [conventions](README.md#conventions) in full.

## Goal

Before a plan that removes or overwrites configuration on a node (a key's connection rewired, a node's
subscriptions or publication replaced, a scene register deleted, a node removed or reset, element groups changed), the
configurator reads what the plan is about to change on the nodes themselves and compares it with what the export
says. When the nodes disagree with the export (the app changed them since the export, a half-applied earlier plan, a
device reset), the plan stops before its first write and reports the difference, instead of overwriting state the
export no longer describes.

## Background

Every plan is computed from the export (`configurator/`, `jhmesh/plan.py`); the nodes are only told, never asked.
`docs/ha-integration.md` already admits that "a change a node refuses half-way is not rolled back". Dry runs exist
(`MeshConfigurator.dry_run`, brief 49), and `delete_unused_scenes` already reads before deleting (brief 13); the
audit (`jhmesh/audit.py`) can read a node's subscriptions, publications, keys and bindings. What is missing is the
read *before* a destructive write as part of the plan itself.

## Read first

`configurator/` (the plan builders, `store.py` and the dry-run context), `jhmesh/plan.py` (`PlanExecutor`, the plan
model and step kinds), `jhmesh/audit.py` (what it reads and how), `actions/` (which actions run destructive plans,
their response schemas), brief 49 (dry runs, structured responses), brief 13, `docs/ha-integration.md` on the
configurator and its limitations.

## Steps

1. Classify plan steps as destructive or not (a removal, a delete, a reset, or a Set that replaces a value the
   export holds) in the plan model, in `jhmesh/plan.py`, with a test pinning the classification of every step kind.
2. A pre-flight stage in `PlanExecutor` (or just before it): for every destructive step, the matching Get (Model
   Subscription Get, Model Publication Get, Scene Register Get, Model App Get …), one per element and model,
   bounded and through the existing reader / send path. Compare with the export's view of that element and model.
3. On a difference: no write at all; the action fails with a translated error naming the node, the element, the
   model and both values (expected from the export, found on the node), and suggests exporting from the app again.
   A `force: true` field on the destructive actions skips the comparison (documented, admin only, as the others).
4. Unanswered Gets in pre-flight: the plan does not start (the node is asleep or unreachable), with the existing
   unreachable error.
5. Dry runs include the pre-flight reads and report the differences in their structured response.
6. Docs: `docs/ha-integration.md` (configurator section and the limitation), `docs/user/` where the actions are
   described; CHANGELOG under `## 1.3.0 (unreleased)` (create it above `## 1.2.0`; never edit released sections),
   *Added*. Every new string in `strings.json`, `translations/en.json` and, translated, in every other
   `translations/*.json`.

## Tests to add

Per destructive step kind: export and node agree → the plan runs; they differ → nothing is written, the error names
both values; `force` → it runs; a node that does not answer the pre-flight Get → nothing written. A dry run reports a
difference. Non-destructive plans send no extra Gets (the existing snapshots and message counts unchanged for them).

## Acceptance criteria

Gates green; existing plan snapshots change only by the added Gets in destructive plans, reviewed.

## Verifiable on air here?

Yes, harmlessly with a dry run of a destructive action against a node the app changed since the export. Unverified on
air until then.
