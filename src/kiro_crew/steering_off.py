"""The steering files one chat has switched off: resolved paths, never bodies.

One JSON file under the data home maps a chat's session key to the sorted
resolved paths it excludes. Context assembly reads :func:`steering_off_for` once
per turn and hands the set to the two writers Crew owns (the Claude Code
``[Steering resources]`` load and the folder steering section), which skip those
documents. A chat with nothing switched off reads an empty set, so its turn text
is unchanged. The dashboard's per-chat steering route writes it.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_FILE_NAME = "chat_steering_off.json"
# One gateway process writes this file; the lock keeps two toggles of one
# process from losing each other's read-modify-write.
_WRITE_LOCK = threading.Lock()


def _store_path() -> Path:
    from kiro_crew.config.paths import data_home

    return data_home() / _FILE_NAME


class SteeringOffStoreUnreadable(OSError):
    """The store exists but cannot be read, so a write would erase it."""


def _read_all(*, strict: bool = False) -> dict[str, list[str]]:
    """The whole store; ``{}`` when it is missing or unreadable.

    *strict* is the write path's read: an existing store that cannot be read raises
    :class:`SteeringOffStoreUnreadable` instead, so a switch never overwrites every
    other chat's record with an empty one. An empty file is a valid empty store (the
    sandbox pre-creates the leaf as ``{}``).
    """
    try:
        text = _store_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        if strict:
            raise SteeringOffStoreUnreadable(str(exc)) from exc
        logger.warning("steering-off store unreadable, read as empty: %s", type(exc).__name__)
        return {}
    try:
        raw = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        if strict:
            raise SteeringOffStoreUnreadable("the store is not valid JSON") from exc
        logger.warning("steering-off store unreadable, read as empty: %s", type(exc).__name__)
        return {}
    if not isinstance(raw, dict):
        if strict:
            raise SteeringOffStoreUnreadable("the store is not a JSON object")
        return {}
    return {
        key: [p for p in paths if isinstance(p, str) and p]
        for key, paths in raw.items()
        if isinstance(key, str) and isinstance(paths, list)
    }


def steering_off_for(session_key: str | None) -> frozenset[str]:
    """The resolved paths *session_key* switched off; empty for none."""
    if not session_key:
        return frozenset()
    return frozenset(_read_all().get(session_key, ()))


def set_steering_off(session_key: str, path: str, off: bool) -> frozenset[str]:
    """Switch one resolved *path* off (or back on) for *session_key*; the new set."""
    with _WRITE_LOCK:
        store = _read_all(strict=True)
        current = set(store.get(session_key, ()))
        if off:
            current.add(path)
        else:
            current.discard(path)
        if current:
            store[session_key] = sorted(current)
        else:
            store.pop(session_key, None)
        target = _store_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_in_place(target, (json.dumps(store, indent=1, sort_keys=True) + "\n").encode())
        return frozenset(current)


def _write_in_place(path: Path, data: bytes) -> None:
    """Rewrite *path* on its own inode, as ``shared_crew.preview.write_stored`` does.

    The leaf is sealed read-only in the sandbox (``sandbox._CREW_READONLY_LEAVES``):
    a running Linux sandbox bind-mounts this inode at the name, and a rename over a
    mount point in another namespace detaches that mount and would leave the new
    file writable from inside. So an existing record is truncated and rewritten,
    and an absent one is created with ``O_EXCL``. A symlink, reparse point or
    hard-linked record is refused, never followed. A torn read during the rewrite
    reads as nothing switched off.
    """
    base = os.O_WRONLY | getattr(os, "O_BINARY", 0)
    base |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, base | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise OSError(f"refusing to rewrite {path.name}: it is a link") from None
        fd = os.open(path, base)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError(f"refusing to rewrite {path.name}: not a single-link regular file")
        named = os.lstat(path)
        if (named.st_dev, named.st_ino) != (st.st_dev, st.st_ino):
            raise OSError(f"refusing to rewrite {path.name}: it changed while opening")
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        while data:
            data = data[os.write(fd, data) :]
        os.fsync(fd)
    finally:
        os.close(fd)
