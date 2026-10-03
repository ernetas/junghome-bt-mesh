"""Atomic, owner-only file writes: the one writer for every file of `jhmesh` that can hold mesh key material.

An export holds every mesh key; the CLI's state file (`client.LocalState`) holds a new NetKey while it follows a
key refresh. Both used to be written by hand-rolled temp-file-and-rename code, one of them with the umask's mode,
so the state file and its `.bak` came out world-readable on a default umask. Everything now goes through
`atomic_write`. Kept apart from `export` so `client` can use it without loading the project-file model.
"""

from __future__ import annotations

import os
import shutil
import stat
import threading
from pathlib import Path

PRIVATE_MODE = (
    0o600  # owner read/write only: the mode of every private file written here
)
BACKUP_GENERATIONS = 3  # previous contents kept beside a file written with `backup=True` (`backup_paths`)


def fsync_dir(path: Path) -> None:
    """Fsync a directory so a preceding rename/replace into it survives a crash; a no-op where unsupported."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def temp_path(real: Path) -> Path:
    """Return the temporary sibling `atomic_write` writes `real`'s new content to before the rename.

    Process and thread id in the name: two writers of one target (an executor job and the event loop, two hubs of
    one mesh, the CLI beside Home Assistant on a shared directory) never truncate each other's half-written file.
    """
    return real.with_name(f".{real.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def backup_paths(path: Path) -> list[Path]:
    """Return the backup generations of `path`, newest first: `<name>.bak`, `<name>.bak.1`, `<name>.bak.2`, …."""
    return [
        path.with_name(f"{path.name}.bak{f'.{n}' if n else ''}")
        for n in range(BACKUP_GENERATIONS)
    ]


def copy_private(src: Path, dst: Path, mode: int) -> None:
    """Copy `src` to `dst` created fresh with `mode` (at most 0600), fsynced: never a world-readable copy of the keys.

    The fsync matters as much as for the target: the copy is what is left to go back to when the next write turns
    out bad, and an unsynced copy can be empty after a power loss while the rename that follows it is not.
    """
    dst.unlink(missing_ok=True)
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
        shutil.copyfileobj(inp, out)
        out.flush()
        os.fsync(out.fileno())
    dst.chmod(
        mode
    )  # `os.open` applies the umask to `mode`; the backup gets exactly the target's mode


def keep_backup(real: Path, mode: int) -> None:
    """Copy `real` to `<name>.bak`, the older generations moved one down and the oldest dropped (review-3 W7).

    One generation was not enough: every write replaced it, so a bad change noticed one change later — or a
    gateway export adopted and saved over right away — left nothing to go back to.
    """
    generations = backup_paths(real)
    for older, newer in zip(generations[:0:-1], generations[-2::-1], strict=True):
        if newer.exists():
            newer.replace(older)
    copy_private(real, generations[0], mode)


def atomic_write(
    path: Path, data: bytes, *, private: bool, backup: bool = False
) -> None:
    """Write `data` to `path` atomically: a temp file, fsynced, renamed over the target, then the directory fsynced.

    The target is always either its old content in full or the new one in full, whatever crashes or loses power
    partway through: `os.replace` alone only rules out a torn file, the fsync before it a lost one (delayed
    allocation can leave the new name pointing at data never written out). Creates the directory. A symlinked
    target is written *through*: the real file gets the new content, the link stays a link.

    `private`: the temp file is created 0600 whatever the umask, and the target ends at 0600 — or at an existing
    target's own mode where that is stricter still (a user-provided export kept read-only) — never looser.
    Without it a new target gets the umask's mode and an existing one keeps its own, as a plain write would.

    `backup`: an existing target's content is first copied to `<name>.bak` (fsynced, at the target's mode but
    never looser than 0600), the older generations rotated down (`backup_paths`).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    real = path.resolve()
    tmp = temp_path(real)
    try:
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            PRIVATE_MODE if private else 0o666,
        )
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        existing = stat.S_IMODE(real.stat().st_mode) if real.exists() else None
        mode = existing
        if private:
            mode = PRIVATE_MODE if existing is None else existing & PRIVATE_MODE
        if mode is not None:
            tmp.chmod(
                mode
            )  # `os.open` applied the umask; the target gets exactly this mode
        if backup and existing is not None:
            keep_backup(real, existing & PRIVATE_MODE)
        tmp.replace(real)
        fsync_dir(real.parent)
    finally:
        tmp.unlink(missing_ok=True)
