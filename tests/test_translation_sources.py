"""tools/translation_sources.py: the record of the English each translation follows, and its refresh.

`tests/test_translations.py` checks the committed record against the English; here the tool runs on a repository
made in `tmp_path` with one English key and two languages.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools import translation_sources as ts

GIT = shutil.which("git") or "git"


def _git(root: Path, *args: str) -> None:
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": os.devnull,  # no signing, hooks or templates of whoever runs the tests
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "",
    }
    subprocess.run(  # noqa: S603  # on a repository made in tmp_path
        [GIT, *args], cwd=root, env=env, capture_output=True, check=True
    )


def _write(path: Path, data: dict[str, object]) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """English, German and French of `a.title`, committed with their record."""
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
    ):  # set when a hook runs the tests
        monkeypatch.delenv(name, raising=False)
    translations = tmp_path / "translations"
    translations.mkdir()
    monkeypatch.setattr(ts, "ROOT", tmp_path)
    monkeypatch.setattr(ts, "TRANSLATIONS", translations)
    monkeypatch.setattr(ts, "EN", translations / "en.json")
    monkeypatch.setattr(ts, "RECORD", tmp_path / "record.json")
    _write(translations / "en.json", {"a": {"title": "Room"}})
    _write(translations / "de.json", {"a": {"title": "Raum"}})
    _write(translations / "fr.json", {"a": {"title": "Zone"}})
    assert ts.main([]) == 0  # no record yet: every key is new
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "first")
    return tmp_path


def test_the_committed_record_is_the_english() -> None:
    """The repository's own record (what `test_translations` also checks), through the tool's `--check`."""
    assert ts.main(["--check"]) == 0


def test_changed_english_waits_for_every_language(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    translations = repo / "translations"
    _write(translations / "en.json", {"a": {"title": "Room of the app"}, "b": "New"})
    assert ts.main(["--check"]) == 1
    assert capsys.readouterr().out.split() == ["stale:", "a.title", "stale:", "b"]

    _write(translations / "de.json", {"a": {"title": "Raum der App"}, "b": "Neu"})
    _write(
        translations / "it.json", {"a": {"title": "Stanza"}}
    )  # new since HEAD: nothing to compare with
    assert ts.main([]) == 1
    err = capsys.readouterr().err
    assert "a.title: English changed, not re-translated since HEAD in fr\n" in err
    assert json.loads((repo / "record.json").read_text())["a.title"] == ts.source_hash(
        "Room"
    )

    _write(translations / "fr.json", {"a": {"title": "Zone de l'app"}, "b": "Nouveau"})
    assert ts.main([]) == 0
    assert "1 changed, 1 new, 0 gone" in capsys.readouterr().out
    assert ts.main(["--check"]) == 0


def test_since_names_the_revision_before_the_english_changed(repo: Path) -> None:
    translations = repo / "translations"
    _write(translations / "en.json", {"a": {"title": "Room of the app"}})
    _git(repo, "commit", "-q", "-am", "English only")
    for lang, text in (("de", "Raum der App"), ("fr", "Zone de l'app")):
        _write(translations / f"{lang}.json", {"a": {"title": text}})
    _git(repo, "commit", "-q", "-am", "translations")
    assert ts.main([]) == 1  # nothing changed since HEAD
    assert ts.main(["--since", "HEAD~2"]) == 0


def test_same_meaning_accepts_a_changed_key_untranslated(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo / "translations" / "en.json", {"a": {"title": "Room "}})
    assert ts.main(["--same-meaning", "a.title", "b"]) == 2
    assert "['b']" in capsys.readouterr().err
    assert ts.main(["--same-meaning", "a.title"]) == 0
    assert ts.main(["--check"]) == 0


def test_since_is_a_revision_not_an_option(repo: Path) -> None:
    _write(repo / "translations" / "en.json", {"a": {"title": "Room of the app"}})
    with pytest.raises(SystemExit) as exited:
        ts.main(["--since=--output=x"])
    assert exited.value.code == 2
