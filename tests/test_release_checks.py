"""scripts/release_checks.sh: the checks release.yml runs on a tag, run here on fixture trees on every push."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "release_checks.sh"
# Absolute paths: the commands below are fixed, only their arguments vary (all of them written by these tests).
BASH = shutil.which("bash") or "/bin/bash"
GIT = shutil.which("git") or "/usr/bin/git"

CHANGELOG = """\
# Changelog

## 0.3.10 (unreleased)

- Not this one.

## 0.3.1 (stable)

### Fixed

- The fix.

## 0.3.0 (stable)

## 0.2.0 (stable)

- Older.
"""


def _tree(
    root: Path,
    *,
    manifest: str = "0.3.1",
    pyproject: str = "0.3.1",
    changelog: str = CHANGELOG,
) -> Path:
    """The files the script reads, at the paths release.yml's checkout has them."""
    integration = root / "custom_components" / "junghome_ble"
    integration.mkdir(parents=True)
    (integration / "manifest.json").write_text(
        json.dumps({"domain": "junghome_ble", "version": manifest})
    )
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "jhmesh"\nversion = "{pyproject}"\n'
    )
    (root / "CHANGELOG.md").write_text(changelog)
    return root


def _run(
    root: Path, tag: str, *args: str, **env: str
) -> subprocess.CompletedProcess[str]:
    environ = {
        k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GIT_"))
    }
    environ |= {"GITHUB_REF_NAME": tag, **env}
    return subprocess.run(  # noqa: S603  # the repository's own script on a fixture tree
        [BASH, str(SCRIPT), *args],
        cwd=root,
        env=environ,
        capture_output=True,
        text=True,
        check=False,
    )


def test_versions_match(tmp_path: Path) -> None:
    result = _run(_tree(tmp_path), "v0.3.1", "versions")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tag=0.3.1 manifest=0.3.1 pyproject=0.3.1" in result.stdout


@pytest.mark.parametrize(
    ("manifest", "pyproject", "message"),
    [
        (
            "0.3.0",
            "0.3.1",
            "does not match custom_components/junghome_ble/manifest.json version (0.3.0)",
        ),
        (
            "0.3.1",
            "0.3.0",
            "does not match the jhmesh version in pyproject.toml (0.3.0)",
        ),
    ],
)
def test_versions_mismatch_fails(
    tmp_path: Path, manifest: str, pyproject: str, message: str
) -> None:
    result = _run(
        _tree(tmp_path, manifest=manifest, pyproject=pyproject), "v0.3.1", "versions"
    )
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert message in result.stdout


def test_versions_pre_release_tag_must_match_too(tmp_path: Path) -> None:
    """A beta carries its own version (0.3.1b1) in both files, so a tag of the plain version does not match it."""
    assert _run(_tree(tmp_path), "v0.3.1b1", "versions").returncode == 1


def test_changelog_dated_heading(tmp_path: Path) -> None:
    result = _run(_tree(tmp_path), "v0.3.1", "changelog")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "## 0.3.1 (stable)"


def test_changelog_missing_heading_fails(tmp_path: Path) -> None:
    """0.3.10's heading is no heading for 0.3.1 (and the dots are not regex wildcards)."""
    result = _run(
        _tree(tmp_path, changelog=CHANGELOG.replace("## 0.3.1 ", "## 0.3x1 ")),
        "v0.3.1",
        "changelog",
    )
    assert result.returncode == 1
    assert (
        '::error file=CHANGELOG.md::No "## 0.3.1" heading in CHANGELOG.md.'
        in result.stdout
    )


def test_changelog_unreleased_heading_fails(tmp_path: Path) -> None:
    result = _run(_tree(tmp_path), "v0.3.10", "changelog")
    assert result.returncode == 1
    assert "still calls 0.3.10 unreleased" in result.stdout


def test_changelog_not_checked_for_a_pre_release(tmp_path: Path) -> None:
    result = _run(_tree(tmp_path), "v0.3.10rc1", "changelog")
    assert result.returncode == 0
    assert "Pre-release version (0.3.10rc1)" in result.stdout


def test_notes_are_the_version_section(tmp_path: Path) -> None:
    notes = tmp_path / "notes.md"
    result = _run(_tree(tmp_path), "v0.3.1", "notes", str(notes))
    assert result.returncode == 0, result.stdout + result.stderr
    assert notes.read_text() == "\n### Fixed\n\n- The fix.\n\n"


def test_notes_of_a_pre_release_take_its_version_section(tmp_path: Path) -> None:
    notes = tmp_path / "notes.md"
    assert _run(_tree(tmp_path), "v0.3.10-rc1", "notes", str(notes)).returncode == 0
    assert notes.read_text() == "\n- Not this one.\n\n"


def test_notes_empty_section_fails(tmp_path: Path) -> None:
    result = _run(_tree(tmp_path), "v0.3.0", "notes", str(tmp_path / "notes.md"))
    assert result.returncode == 1
    assert 'The "## 0.3.0" section of CHANGELOG.md is empty.' in result.stdout


def test_notes_pre_release_without_section_says_so(tmp_path: Path) -> None:
    notes = tmp_path / "notes.md"
    result = _run(_tree(tmp_path), "v0.4.0b1", "notes", str(notes))
    assert result.returncode == 0
    assert "::warning file=CHANGELOG.md::" in result.stdout
    assert (
        notes.read_text()
        == "Pre-release of 0.4.0: CHANGELOG.md has no section for it yet.\n"
    )


def test_notes_need_a_version_tag(tmp_path: Path) -> None:
    result = _run(_tree(tmp_path), "nightly", "notes", str(tmp_path / "notes.md"))
    assert result.returncode == 1
    assert "does not start with a vX.Y.Z version" in result.stdout


def _git(root: Path, *args: str) -> str:
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": os.devnull,  # no signing, hooks or templates of whoever runs the tests
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "",
    }
    result = subprocess.run(  # noqa: S603  # on a repository made in tmp_path
        [GIT, *args], cwd=root, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_on_main(tmp_path: Path) -> None:
    """A commit main has passes; one only on a feature branch does not (origin/main as release.yml's checkout has it)."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "on main")
    merged = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", merged)
    _git(tmp_path, "switch", "-q", "-c", "feature")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "feature only")
    feature = _git(tmp_path, "rev-parse", "HEAD")

    passed = _run(tmp_path, "v0.3.1", "on-main", GITHUB_SHA=merged)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    failed = _run(tmp_path, "v0.3.1", "on-main", GITHUB_SHA=feature)
    assert failed.returncode == 1
    assert f"The tagged commit ({feature}) is not on main" in failed.stdout


def test_usage(tmp_path: Path) -> None:
    assert _run(tmp_path, "v0.3.1").returncode == 2
    assert _run(tmp_path, "v0.3.1", "notes").returncode != 0  # no FILE
