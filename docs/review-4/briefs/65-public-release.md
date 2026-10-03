# 65 — Fresh public repository and first-release notes

Phase P5 · Wave 22 · Size M · Closes: D8 (Q4-3), Q4-16; folds in Q4 improvement D4 (CONTRIBUTING), the issue
templates and the release-readiness checklist. The brand images are already in
`custom_components/junghome_ble/brand/` (copied by the maintainer); nothing about brand here.

Follow the [conventions](README.md#conventions) in full. Most steps here are the maintainer's (*human*): an agent
prepares files and scripts in its worktree and stops before anything that creates, pushes to or changes a GitHub
repository.

## Goal

A public repository that carries none of the maintainer's personal data in its tree or history, with release notes
a HACS user can read.

## Background

Review 3 scrubbed the tree; history still holds every original value. The existing private repository has 129
commits, every one with the maintainer's personal author e-mail and authoring time zone, 66 `Claude-Session:`
trailers, commit messages that list what the scrub replaced, and a pushed Renovate branch based on the unscrubbed
first commit. GitHub keeps unreachable commits and pull-request refs fetchable by SHA after a force push, so making
*this* repository public after a rewrite can still expose the originals. `CHANGELOG.md` is one large `## 0.3.0`
section citing `docs/review-*` and upgrade steps from a 0.2 that was never published; HACS shows the release body in
its update dialog. Decisions M2 (new repository recommended) and M13 (version naming, artifact clean-up).

## Read first

`README.md`, `SECURITY.md`, `DISCLAIMER.md`, `CHANGELOG.md`, `.github/workflows/release.yml` (tag on `main`, version
match, CHANGELOG heading rule, notes extraction, PyPI trusted publishing), `docs/roadmap.md` ("Going public"),
`tools/privacy_scan.py` (brief 64).

## Steps

1. CHANGELOG: a short "first public release" section a user can read (what works, which device classes are
   "unverified on air", security notes, how to report). Move the review fix log to `docs/dev/history.md` (or the
   docs location brief 43 chose). Drop the 0.2 upgrade steps or say they concern private test installs only.
2. `CONTRIBUTING.md`: venv and requirements files, test commands (`-n auto`), `noxfile.py` sessions, fixture
   regeneration, snapshot update and review, the pseudonym / privacy policy, "never commit an export".
3. `.github/ISSUE_TEMPLATE/`: a bug report asking for redacted diagnostics and HA / integration versions; a device
   report for blinds, room thermostats, detectors, puck and battery transmitters.
4. Prepare `scripts/make_public_tree.sh` (not run by the agent): an orphan branch with one commit of the cleaned
   tree, author and committer set to the GitHub noreply identity, a neutral author date chosen by the maintainer, a
   message with no session trailer; then `tools/privacy_scan.py` (tree and `--history`) and an offline secret scanner
   (gitleaks or trufflehog) over that single commit.
5. Maintainer steps (write them as a checklist in `docs/dev/release.md`): create a **new** empty public repository
   and push the single commit; archive the private repository (do not flip it); delete the stale Renovate branch on
   the private remote; in the new repository enable private vulnerability reporting, branch protection on `main`
   requiring CI, tag protection on `v*`, a required reviewer on the `pypi` environment; register the PyPI pending
   publisher for `jhmesh` with the new repository and workflow; re-point `manifest.json` `documentation` /
   `issue_tracker`, README links and `hacs.json` if the repository name changes; delete old CI artifacts once if
   still on the old repository (decision M13).
6. Commit messages of this brief carry the usual trailer in the *private* worktree; the public commit has none.

## Tests to add

A docs link test over `CONTRIBUTING.md` and the new pages (reuse brief 43's link checker); a test that
`CHANGELOG.md`'s top section has no `docs/review-` reference.

## Acceptance criteria

Gates pass; the prepared script produces, in a scratch clone, one commit whose author is the noreply identity and
whose tree passes `tools/privacy_scan.py --history` and the secret scanner; `git log` shows no trailer.

## Verifiable on air here?

Local only; the GitHub steps are the maintainer's.

## Risks / off-by-default / "unverified on air"

Losing history: keep the private repository archived. Pseudonymised docs still describe the installation's
structure. Confirm no fork of the private repository was ever made.

## Depends on

02 (no world-readable keys in the first public version), 06 (CI without the artifact quota), 43 (docs layout), 64
(scanner and scrub); decisions M2, M13. 66 follows.

## Files touched

`CHANGELOG.md`, new `docs/dev/history.md`, new `docs/dev/release.md`, new `CONTRIBUTING.md`, new
`.github/ISSUE_TEMPLATE/bug_report.yml`, `device_report.yml`, `config.yml`, new `scripts/make_public_tree.sh`,
`README.md` (links), tests for links and the changelog.
