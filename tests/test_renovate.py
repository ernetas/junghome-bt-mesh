"""renovate.json and the pins it moves, checked offline.

Renovate's own validator (`npx --package renovate renovate-config-validator --strict renovate.json`) needs the
network and a Node toolchain; these checks hold what the configuration is for: every rule says why, the
pre-commit manager is on, and every hook revision is a frozen tag the three-day rule ages.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parent.parent
RENOVATE = ROOT / "renovate.json"
PRE_COMMIT = ROOT / ".pre-commit-config.yaml"
FROZEN = re.compile(
    r"^\s*rev: (?P<sha>[0-9a-f]{40})  # frozen: (?P<tag>v\S+)$", re.MULTILINE
)


def _config() -> dict[str, Any]:
    config: dict[str, Any] = json.loads(RENOVATE.read_text(encoding="utf-8"))
    return config


def test_known_top_level_options() -> None:
    """A misspelt option is ignored by Renovate without a word; only the ones used here are expected."""
    config = _config()
    assert config["$schema"] == "https://docs.renovatebot.com/renovate-schema.json"
    assert set(config) == {"$schema", "extends", "packageRules"}


def test_every_rule_says_why_and_matches_something() -> None:
    for rule in _config()["packageRules"]:
        assert rule.get("description", "").strip(), rule
        assert any(key.startswith("match") for key in rule), rule


def test_the_pre_commit_manager_is_on_and_aged() -> None:
    """The manager is off by default; its tags come from github-tags, which the minimum-release-age rule covers."""
    config = _config()
    assert ":enablePreCommit" in config["extends"]
    aged = [rule for rule in config["packageRules"] if "minimumReleaseAge" in rule]
    assert len(aged) == 1
    assert "github-tags" in aged[0]["matchDatasources"]
    assert any(
        rule.get("matchManagers") == ["pre-commit"] for rule in config["packageRules"]
    )


def test_every_hook_repository_is_a_frozen_github_tag() -> None:
    """`rev: <sha>  # frozen: vX`: the form Renovate's pre-commit manager reads and moves (commit and tag)."""
    text = PRE_COMMIT.read_text(encoding="utf-8")
    remote = [
        repo["repo"]
        for repo in yaml.safe_load(text)["repos"]
        if repo["repo"] not in {"local", "meta"}
    ]
    assert remote
    assert all(repo.startswith("https://github.com/") for repo in remote), remote
    assert len(FROZEN.findall(text)) == len(remote)


def test_ruff_hook_matches_the_lint_pin() -> None:
    """The ruff group moves both in one PR; until then they must agree."""
    text = PRE_COMMIT.read_text(encoding="utf-8")
    hook = re.search(r"ruff-pre-commit\n\s+rev: [0-9a-f]{40}  # frozen: v(\S+)", text)
    pin = re.search(
        r"^ruff==(\S+)$",
        (ROOT / "requirements-lint.txt").read_text("utf-8"),
        re.MULTILINE,
    )
    assert hook is not None
    assert pin is not None
    assert hook.group(1) == pin.group(1)
    assert any(
        "astral-sh/ruff-pre-commit" in rule.get("matchPackageNames", [])
        and "ruff" in rule["matchPackageNames"]
        for rule in _config()["packageRules"]
    )


def test_the_python_of_the_workflow_jobs_is_held() -> None:
    """Renovate's github-actions manager moves `setup-python`'s `python-version` (it proposed one such update before):
    every job sets up Home Assistant's Python, not the newest one, so that update is off. The jobs and
    `noxfile.py` then name the same version, moved by hand when Home Assistant moves."""
    held = [
        rule
        for rule in _config()["packageRules"]
        if rule.get("matchManagers") == ["github-actions"]
        and rule.get("matchDepNames") == ["python"]
    ]
    assert len(held) == 1
    assert held[0].get("enabled") is False
    nox = re.search(
        r'^PYTHON = "(\S+)"', (ROOT / "noxfile.py").read_text("utf-8"), re.MULTILINE
    )
    assert nox is not None
    versions = {
        (workflow.name, name): step["with"]["python-version"]
        for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        for name, job in yaml.safe_load(workflow.read_text("utf-8"))["jobs"].items()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/setup-python@")
        and "${{"
        not in str(step["with"]["python-version"])  # `library`: requires-python
    }
    assert len(versions) >= 6, versions
    assert set(versions.values()) == {nox[1]}, versions
