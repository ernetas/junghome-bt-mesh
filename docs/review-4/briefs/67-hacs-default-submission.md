# 67 — hacs/default submission (HACS-3)

Phase P6 · Wave 24 · Size S · Closes: the last HACS default-store requirement (the pull request to hacs/default).

Follow the [conventions](README.md#conventions) in full. The pull request is opened by the owner from their personal
GitHub account (*human*); an agent prepares the README changes and the PR text, and opens nothing.

## Goal

The integration is listed in the HACS default store.

## Background

HACS's default store needs: a public repository with a description, issues enabled and topics (these hold); at least
one full release made after the HACS action and hassfest pass (brief 66); `hacs.json` with `name` (present, with
`render_readme` and the HA floor); the manifest keys `domain`, `documentation`, `issue_tracker`, `codeowners`, `name`,
`version` (present); one integration under `custom_components/`; brand images (present in `custom_components/junghome_ble/brand/`); then a pull request to
`hacs/default` that adds the repository, in alphabetical order, to the `integration` list file, with the PR template
filled in. `DISCLAIMER.md` exists; the README does not yet say near the top that the project is unofficial.
Decisions M14 (timing) and M15 (display name wording).

## Read first

`README.md`, `DISCLAIMER.md`, `hacs.json`, `custom_components/junghome_ble/manifest.json`, the hacs/default
repository's README, its `integration` file and its pull-request template (read them on GitHub when preparing).

## Steps

1. README: a line near the top saying the project is unofficial and not affiliated with or endorsed by JUNG (link
   `DISCLAIMER.md`); an "Install" section with the my.home-assistant.io HACS repository redirect link
   (`https://my.home-assistant.io/redirect/hacs_repository/?owner=<owner>&repository=<repository>&category=integration`,
   with the public repository's owner and name filled in) and the manual HACS custom-repository steps until the
   listing is merged.
2. `hacs.json`: leave out `country` (JUNG HOME is sold in several European countries; the key would hide it
   elsewhere) unless the maintainer decides otherwise. Apply decision M15 to `name` in `hacs.json` and
   `manifest.json` if "unofficial" wording is chosen (keep both names identical).
3. Write `docs/dev/hacs-default-pr.md`: the exact line to insert into hacs/default's `integration` file (the
   `owner/repository` string at its alphabetical position) and answers to every question of the PR template (checks
   that the repository is public, has a release, passes the HACS action and hassfest, brand images present, not a
   fork, etc.).
4. Maintainer: fork hacs/default from the personal account, apply the one-line change, open the PR with the prepared
   text, and answer review comments.

## Tests to add

The README link check from brief 43 covers the new links (the my.home-assistant.io URL is external: check format
only, no network).

## Acceptance criteria

Gates pass; the README shows the disclaimer line and the Install section; the PR text covers every template item;
after merge, the integration appears in HACS's store search on a test HA.

## Verifiable on air here?

Local only.

## Risks / off-by-default / "unverified on air"

Reviewers of hacs/default may ask for changes (topics, description, release notes); keep the release from 66 as the
reference. Listing makes the integration visible to many users at once: decide M14 (submit right away, or after the
release has run as a custom repository for a while).

## Depends on

66; decisions M14, M15.

## Files touched

`README.md`, `hacs.json` (only if M15 or `country` changes it), `custom_components/junghome_ble/manifest.json` (only
for M15), new `docs/dev/hacs-default-pr.md`, `CHANGELOG.md`.
