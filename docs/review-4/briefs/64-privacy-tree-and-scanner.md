# 64 — Tree scrub, `.gitignore`, privacy scanner, developer tooling

Phase P5 · Wave 21 · Size M · Closes: Q4-14, Q4-15, Q4-18; folds in Q4 improvements D1 (pre-commit), D2 (privacy
scanner), D3 (noxfile).

Follow the [conventions](README.md#conventions) in full. This brief is about personal data: describe what you
remove in the commit message and report by kind and file, **never by value** (no paths, names, addresses, MACs,
UUIDs, e-mail addresses or zone names quoted anywhere).

## Goal

The working tree carries nothing that profiles the maintainer, sensitive inputs cannot be committed by accident, and
a scanner keeps it that way in CI and before every commit.

## Background

Review 3 pseudonymised the installation's identifiers. What remains (report 7):

- `docs/cross-repo-analysis.md` (top and one later paragraph) contains home-directory paths of the maintainer's
  workspace and of a gateway SD-card dump; `docs/hidden-features.md` (two places) and
  `docs/parity/captures-inventory.json` name the sniffer host's home layout; `docs/sniffer.md` names the HA host's OS
  and group setup; `docs/bluetooth-recheck.md` says when the occupant was away.
- `tests/jhmesh/test_messages.py` (time-zone tests) uses a zone name that hints at the maintainer's region.
- Test gateway IPs are RFC 1918 home-range addresses (`tests/test_diagnostics.py`, `test_coordinator.py`,
  `test_config_flow.py`, `tests/jhmesh/test_properties.py`, others) while the MACs already follow documentation
  ranges.
- `.gitignore` does not cover `android/` as a whole (an APK or notes file there can be committed), an export saved
  as `docs/JungHome.json` or elsewhere, `.claude/settings.local.json`, or `*.tmp` (`git check-ignore` confirms).

## Read first

The files above; `README.md` (pseudonym policy, fixtures note); `tools/mesh_report.py --anonymise`;
`tools/mesh-sniff.service`; `.github/workflows/ci.yml` (`lint` job).

## Steps

1. Replace workspace / home / dump paths with neutral placeholders (`<workspace>`, `<capture host>`); drop the
   occupancy remark; name the HA host's OS only where it matters; rename the test zone to a neutral name and offset
   (for example a generic UTC+2 label); move test IPs to `192.0.2.x` / `198.51.100.x`.
2. `.gitignore`: all of `android/`, `**/JungHome*.json` and `**/MeshNetwork*.json` with `!tests/fixtures/**`
   exceptions, `.claude/`, `*.tmp`. Check that no tracked file becomes ignored unintentionally.
3. `tools/privacy_scan.py` (stdlib only) failing on: MACs outside the documentation / placeholder patterns unless in
   an allowlist file of documented pseudonyms; UUIDs outside an allowlist (spec GATT UUIDs, pseudonyms, fixture
   patterns); IPs outside documentation and loopback ranges; 128-bit hex not in an allowlist of Mesh Profile sample
   vectors and fixture patterns; absolute home paths and `~/` paths; with `--history`, any `Claude-Session:` trailer
   or personal e-mail pattern in `git log`. The allowlist lives in the repository. Output names file and line and
   the *kind* of match, never the matched value.
4. A CI step in `lint` running the tree scan; `.pre-commit-config.yaml` with ruff, ruff format, `check-json` /
   `yaml` / `toml`, mypy on push, and the privacy hook.
5. `noxfile.py` with sessions matching the CI jobs one to one (`lint`, `types`, `tests`, `tests-library-3.13`,
   `build`, `package`, `regen-fixtures`, `snapshots`).

## Tests to add

`tests/test_privacy_scan.py`: synthetic positives (a made-up MAC outside the ranges, a random UUID, a home path, a
random 128-bit hex, a home-range IP) are flagged; allowlisted values pass; the scan over the tree is clean; output
never contains the matched value.

## Acceptance criteria

Gates pass; `tools/privacy_scan.py` is clean on the tree; `git check-ignore` covers the listed paths; a local
comparison script (not committed) confirms no file holds a value that appears in the pre-scrub commit, printing
counts only.

## Verifiable on air here?

Local only.

## Risks / off-by-default / "unverified on air"

False positives: keep the allowlist small and explicit. Docs that describe the installation's structure (node count,
product mix) stay; the maintainer decides on trimming `docs/network-topology.md`.

## Depends on

None hard; runs late so the test-IP edits do not collide with feature briefs. 65 depends on it.

## Files touched

`docs/cross-repo-analysis.md`, `docs/hidden-features.md`, `docs/sniffer.md`, `docs/bluetooth-recheck.md`,
`docs/parity/captures-inventory.json`, `tools/mesh-sniff.service`, `tests/jhmesh/test_messages.py`, tests with
gateway IPs, `.gitignore`, `README.md` (pseudonym note), new `tools/privacy_scan.py`, new `tools/privacy_allowlist.txt`,
new `tests/test_privacy_scan.py`, new `.pre-commit-config.yaml`, new `noxfile.py`, `.github/workflows/ci.yml`,
`CHANGELOG.md`.
