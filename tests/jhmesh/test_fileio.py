"""jhmesh.fileio: the one atomic writer of every file that can hold mesh keys (review-4 D1, S4-10)."""

from __future__ import annotations

import os
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jhmesh import fileio
from jhmesh.export import ProjectFile
from jhmesh.fileio import atomic_write, backup_paths, keep_backup, temp_path

from .conftest import CDB_PATH


def test_temp_names_of_two_threads_differ(tmp_path: Path) -> None:
    """S4-10: `ProjectFile.save` named its temp file after the process only, so two threads saving one export
    truncated each other's half-written file; every writer now goes through `temp_path`."""
    target = tmp_path / "MeshNetwork.json"
    names: dict[str, Path] = {}

    def name(label: str) -> None:
        names[label] = temp_path(target)

    workers = [threading.Thread(target=name, args=(label,)) for label in "ab"]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert names["a"] != names["b"]
    assert {p.parent for p in names.values()} == {tmp_path}
    assert all(
        p.name.startswith(f".MeshNetwork.json.{os.getpid()}.") for p in names.values()
    )


def test_the_backup_copy_is_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S4-10: the copy is what is left to go back to when the next write turns out bad; it was renamed into
    place on top of data that may never have reached the disk."""
    target = tmp_path / "export.json"
    target.write_bytes(b"old")
    synced: list[int] = []  # the inode of every file or directory fsynced
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        synced.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    keep_backup(target.resolve(), 0o600)
    bak = backup_paths(target)[0]
    assert bak.read_bytes() == b"old"
    assert synced == [bak.stat().st_ino]
    # and through the writer: the temp file (now the target), the backup copy, then the directory
    synced.clear()
    atomic_write(target, b"new", private=True, backup=True)
    assert synced == [
        target.stat().st_ino,
        bak.stat().st_ino,
        tmp_path.stat().st_ino,
    ]
    assert bak.read_bytes() == b"old"
    assert target.read_bytes() == b"new"


def test_a_non_private_write_follows_the_umask_or_the_target(tmp_path: Path) -> None:
    """`private=False` is a plain atomic write: a new file gets the umask's mode, an existing one keeps its own."""
    old_umask = os.umask(0o022)
    try:
        fresh = tmp_path / "sub" / "plain.txt"  # the directory is created
        atomic_write(fresh, b"x", private=False)
        assert stat.S_IMODE(fresh.stat().st_mode) == 0o644
        fresh.chmod(0o640)
        atomic_write(fresh, b"y", private=False, backup=True)
        assert stat.S_IMODE(fresh.stat().st_mode) == 0o640
        bak = backup_paths(fresh)[0]
        assert bak.read_bytes() == b"x"
        assert (
            stat.S_IMODE(bak.stat().st_mode) == 0o600
        )  # a backup is never looser than 0600
    finally:
        os.umask(old_umask)


def test_a_private_write_through_a_symlink_keeps_the_link(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_bytes(b"old")
    real.chmod(0o644)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    atomic_write(link, b"new", private=True)
    assert link.is_symlink()
    assert real.read_bytes() == b"new"
    assert stat.S_IMODE(real.stat().st_mode) == fileio.PRIVATE_MODE


# ----------------------------------------------------------------------------- the CDB timestamp


@pytest.mark.parametrize(
    ("loaded", "now", "expected"),
    [
        # a clock behind the file: the file's own second, never an older one
        (
            "2026-03-01T12:30:45Z",
            datetime(2026, 1, 1, tzinfo=UTC),
            "2026-03-01T12:30:45Z",
        ),
        # a sub-second stamp of another writer: a millisecond past it, in whole seconds
        (
            "2026-03-01T12:30:45.999Z",
            datetime(2026, 1, 1, tzinfo=UTC),
            "2026-03-01T12:30:46Z",
        ),
        # no zone (read as UTC)
        (
            "2026-03-01T12:30:45",
            datetime(2026, 1, 1, tzinfo=UTC),
            "2026-03-01T12:30:45Z",
        ),
        # a clock ahead of the file: now
        (
            "2026-03-01T12:30:45Z",
            datetime(2026, 3, 2, 8, 0, 0, tzinfo=UTC),
            "2026-03-02T08:00:00Z",
        ),
        # nothing to compare with: now
        ("not a time", datetime(2026, 1, 1, tzinfo=UTC), "2026-01-01T00:00:00Z"),
    ],
    ids=["clock_behind", "sub_second", "naive", "clock_ahead", "unparseable"],
)
def test_touch_never_goes_backwards(loaded: str, now: datetime, expected: str) -> None:
    """S4-10: `touch` wrote `now` even when it was older than the loaded timestamp, so a host with its clock
    behind the app's phone stamped its change as older than the file it was made from."""
    pf = ProjectFile.load(CDB_PATH)
    pf.loaded_timestamp = loaded
    assert pf.touch(now) == expected
    assert pf.net["timestamp"] == expected


def test_touch_reads_the_clock_when_not_given_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real clock (patched here to sit behind the file) goes through the same floor."""
    pf = ProjectFile.load(CDB_PATH)
    pf.loaded_timestamp = "2026-03-01T12:30:45Z"

    class Behind(datetime):
        @classmethod
        def now(cls, tz: object = None) -> Behind:  # type: ignore[override]
            return cls(2020, 1, 1, tzinfo=UTC)

    monkeypatch.setattr("jhmesh.export.datetime", Behind)
    assert pf.touch() == "2026-03-01T12:30:45Z"
