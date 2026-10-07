# The HACS default-store pull request

How the integration is submitted to the HACS default store (review-4 brief 67), and the text of the pull request.

## Requirements, and where each one holds

| HACS requirement | Holds by |
|---|---|
| Public GitHub repository, not archived, not a fork, with a description, topics and issues enabled | the repository settings |
| At least one full GitHub release, made after the validation actions passed | `release.yml` re-runs the whole of `ci.yml` against the tag before it publishes |
| The HACS action passes with no validator ignored | `ci.yml` job `validate`, step *HACS validation* (no `ignore` input) |
| The hassfest action passes | `ci.yml` job `validate`, step *hassfest* |
| A valid `manifest.json` | hassfest and the HACS `integration_manifest` validator |
| `hacs.json` with `name` in the released version | `hacs.json` |
| Brand images | `custom_components/junghome_ble/brand/` (`icon.png` and the others) |
| No `country` key unless the integration serves only some countries | left out: JUNG HOME is sold in several European countries |
| Submitted by the owner, from a personal account | the maintainer |

## The change

One line in `hacs/default`'s `integration` file, in its case-insensitive alphabetical order, right after the
maintainer's gateway integration:

```diff
   "ernetas/junghome",
+  "ernetas/junghome-bt-mesh",
   "esbenwiberg/easyiq",
```

Made on a branch of the maintainer's fork of `hacs/default`, branched from `master`; the pull request goes to
`hacs/default:master`.

## Pull request

Title: `Adds new integration [ernetas/junghome-bt-mesh]`

Body: the repository's pull request template with every box ticked and the three links filled in: the release, and
the `ci.yml` run on the released commit (it holds both the HACS action and hassfest, so the two links are the same
run).

Do not request reviews; the HACS team picks pull requests up in order, and their bots run the checks (brands,
manifest, HACS validation, archived, releases, owner, repository, JSON lint and sort order) on the pull request.
