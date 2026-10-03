# 66 — First full release and a green HACS run (HACS-2)

Phase P6 · Wave 23 · Size S–M · Closes: the HACS default-store requirements "a full GitHub release made after the
HACS action and hassfest pass" and "no ignored validators" (plan "HACS (default store)").

Follow the [conventions](README.md#conventions) in full. The tag and the release are the maintainer's (*human*);
an agent prepares and verifies, and stops before tagging.

## Goal

On the public repository every HACS validator and hassfest pass with nothing ignored, and `v0.3.0` exists as a full
GitHub release (not only a tag) whose notes are the CHANGELOG section, installable in HACS as a custom repository.

## Background

`ci.yml`'s `validate` job ignores `hacsjson` and `integration_manifest` while the repository is private (computed from
`github.event.repository.visibility`). The brand images are present in `custom_components/junghome_ble/brand/`
(the HACS brands validator accepts a local `brand/icon.png`), so `brands` must not be ignored either; if the
maintainer's brand change has not yet removed it from `INPUT_IGNORE`, do so here. HACS's default store needs at least one full
release made after these pass. `release.yml` builds and publishes: tag on `main`, version match with `manifest.json`
and `pyproject.toml`, a CHANGELOG heading for the version that is no longer "(unreleased)" (its section becomes the
release notes), the zip and the `jhmesh` dist, and PyPI trusted publishing. `dependency-transparency` (PyPI release
plus a `requirements` entry) is an HA quality-scale rule, **not** a HACS requirement: a HACS listing need not wait
for it (decision M14).

## Read first

`.github/workflows/ci.yml` (`validate`, `package`), `.github/workflows/release.yml`, `hacs.json`, `manifest.json`,
`CHANGELOG.md`, `docs/dev/release.md` (brief 65), the plan's HACS section.

## Steps

1. On the first CI run of the public repository, confirm the `validate` job runs the HACS action with an empty ignore
   list and hassfest, both green. If anything still needs an exemption, fix the cause instead.
2. Simplify `ci.yml`: once the repository is public, drop the visibility expression and the comment that explains
   the private-repository exemptions (keep a note in `docs/dev/release.md`).
3. Prepare the release commit: the CHANGELOG heading for 0.3.0 marked released according to `release.yml`'s heading
   rule (the maintainer fills it in when tagging), versions consistent in `manifest.json` / `pyproject.toml`.
4. Maintainer: push the tag `v0.3.0` on `main`; `release.yml` creates a full GitHub release with the CHANGELOG
   section as notes and the zip attached; confirm it is a release, not a draft or pre-release.
5. Maintainer: on a test HA (not the production one), add the repository in HACS as a custom repository of type
   integration, install `v0.3.0`, restart, check the integration loads and the brand icon shows in HA's UI (the
   HACS panel itself may not show it yet).
6. PyPI: decide (M14) whether to publish `jhmesh` now; if yes, `release.yml`'s PyPI job runs with the pending
   publisher from brief 65; the `requirements` switch stays a separate decision.

## Tests to add

None in code; if step 2 changes `ci.yml`, run actionlint / zizmor (brief 06's lint step).

## Acceptance criteria

The public repository's latest CI run is green with no ignored validator; a full GitHub release `v0.3.0` exists with
the CHANGELOG notes and the zip; installation through HACS as a custom repository works on a test HA.

## Verifiable on air here?

Local only (a test HA instance); no mesh traffic involved.

## Risks / off-by-default / "unverified on air"

A tag that fails a `release.yml` check must be deleted and re-pushed after the fix; never move a published release's
tag. Do not install the test build on the production HA without a backup.

## Depends on

06, 65; decision M14. 67 follows.

## Files touched

`.github/workflows/ci.yml`, `.github/workflows/release.yml` (only if a check needs fixing), `CHANGELOG.md`,
`docs/dev/release.md`.
