# 06 — CI without branch artifacts, faster, Renovate rule

Phase P1 · Wave 1 · Size M · Closes: D7 (Q4-2), D25 (Q4-4), D30 (Q4-9), D-low Q4-17, Q4-12 (actionlint / zizmor
part); folds in Q C3 (release checks as a script), C5 (durations in the log).

Follow the [conventions](README.md#conventions) in full.

## Goal

Branch and PR runs upload no artifacts, so a full artifact quota cannot fail CI; tag runs still release exactly the
files CI checked; tests run in parallel; Renovate cannot move the library job off the oldest supported Python; the
workflows are linted.

## Background

The repository ships a HA custom integration and the `jhmesh` wheel; `.github/workflows/ci.yml` builds and tests,
`release.yml` publishes on tags.

- `ci.yml:135-140` uploads `jhmesh-dist` and `:310-315` uploads `junghome_ble` on every push and PR (`retention-days: 1`
  only on newer runs). `library` (`:154`) and `package` (`:302`) depend on them; only `release.yml:115-118`, `:186-190`
  reads them. A full account quota fails `upload-artifact` → `build` fails → `library` skipped, tags cannot release.
- `renovate.json` has no rule for `python-version`; the open branch `renovate/python-3.x` changes the library job from
  3.13 to 3.14, leaving `requires-python >=3.13` untested.
- `ci.yml:92` runs the 2853 tests serially; `needs: [lint]` delays every job.
- `ci.yml:130` runs `pip install --upgrade pip` (unpinned) in the build path.
- No actionlint / zizmor step; `release.yml`'s shell checks run only on tags.

## Read first

`.github/workflows/ci.yml`, `.github/workflows/release.yml`, `renovate.json`, `requirements-build.txt`,
`scripts/package_ha.sh`, `README.md` (Releases section). Leave the HACS `INPUT_IGNORE` block (`ci.yml:280-295`)
alone: the maintainer has already removed `brands` from it, and the `hacsjson` / `integration_manifest` exemptions
are handled by brief 66 (first HACS release) on the public repository.

## Steps

1. Merge `build` into `library`: on 3.13 build sdist + wheel, `twine check --strict`, install the wheel, mypy 3.13,
   the library tests at 100 % line + branch.
2. Upload `jhmesh-dist` and `junghome_ble` only `if: github.ref_type == 'tag'`; keep `if-no-files-found: error` and
   `retention-days: 1`.
3. `package` on branches: build the zip and check it (`unzip -l` lists `junghome_ble/manifest.json`, no
   `__pycache__`), upload nothing.
4. Update `release.yml` `needs` and comments that name `build`.
5. `tests` job: `-n auto`; drop `needs: [lint]` from test jobs, add `lint` to `package`'s needs; print
   `--durations=20`.
6. Drop the pip upgrade or pin pip in `requirements-build.txt`.
7. Renovate: a `packageRule` (`matchManagers: ["github-actions"]`, `matchDepNames: ["python"]`, `matchFileNames`
   the library job, `allowedVersions: "<3.14"`), or read the version from `pyproject.toml` `requires-python`.
8. `lint`: actionlint and zizmor, pinned by SHA like the other actions; fix what they flag.
9. Move `release.yml`'s shell checks (tag on main, version match, CHANGELOG heading and notes extraction) into
   `scripts/release_checks.sh` and call it from the workflow.

## Tests to add

- `tests/test_release_checks.py`: runs `scripts/release_checks.sh` against temporary CHANGELOG / manifest fixtures
  (match, mismatch, missing heading).
- Run actionlint locally if available. In the report, state what each job does on a branch push and on a tag.

## Acceptance criteria

Gates green; a branch push needs no artifact storage; a tag still releases the zip and dist CI built; `library`
covers 100 % of the installed wheel on 3.13.

## Verifiable on air here?

Local only. The maintainer closes the Renovate PR and, once, deletes old artifacts by hand (`gh api`; decision M13).

## Risks / off-by-default / "unverified on air"

`github.ref_type` in a called workflow is the caller's value: verify on a pre-release tag. Compare wheel metadata once
after moving the build to 3.13.

## Depends on

None. Brief 66 (first HACS release) handles the remaining `INPUT_IGNORE` exemptions and runs the first release.

## Files touched

`.github/workflows/ci.yml`, `.github/workflows/release.yml`, `renovate.json`, `requirements-build.txt`, new
`scripts/release_checks.sh`, `tests/test_release_checks.py`, `README.md` (Releases), `CHANGELOG.md` (Internal).
