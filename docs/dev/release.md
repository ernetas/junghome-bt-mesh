# Releases

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
