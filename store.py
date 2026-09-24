"""Staged-write persistence: one JSON file per staged write under the plugin's data directory.

A staged write is a fully built request the model asked for while ``write_mode`` was
``operator_only``. It waits on disk until a human runs it with ``/gitlab run`` or ``hermes gitlab run``,
drops it, or it expires. The file is rewritten at every state change so an interrupted run leaves a
record of what was attempted.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PLUGIN_NAME = "gitlab"
PREFIX = "glw"
IN_FLIGHT = ("running",)


def default_staged_dir() -> Path:
    """``<HERMES_HOME>/plugin-data/gitlab/staged`` — via Hermes' helper when importable."""
    base: Optional[Path] = None
    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore

        base = plugin_data_dir(PLUGIN_NAME)
    except Exception:
        home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
        base = Path(home) / "plugin-data" / PLUGIN_NAME
    return Path(base) / "staged"


def now_iso() -> str:
    """UTC now as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id() -> str:
    """``glw-<ISO 8601 basic UTC timestamp>-<4 hex>``, e.g. ``glw-20260909T193012Z-4f1a``: filename-safe,
    sorts chronologically, and the ``Z`` makes the zone explicit."""
    return f"{PREFIX}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(2)}"


class StagedStore:
    """One JSON file per staged write under a directory, with in-process and cross-process locking."""

    claim_timeout: float = 30.0  # seconds to wait for the cross-process lock when claiming

    def __init__(self, directory: Optional[Path] = None):
        self._dir = Path(directory) if directory else None
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        """The in-process lock, for callers that need to group several operations."""
        return self._lock

    @contextlib.contextmanager
    def exclusive(self, timeout: float = 30.0):
        """Serialise claims across processes (gateway and ``hermes gitlab`` run separately) with an
        advisory lock on ``<staged>/.lock``. Holds the in-process lock too. Raises ``TimeoutError``
        when another holder does not release within *timeout* seconds."""
        with self._lock:
            lock_path = self.directory / ".lock"
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                _acquire(fd, timeout)
                try:
                    yield
                finally:
                    _release(fd)
            finally:
                os.close(fd)

    @property
    def directory(self) -> Path:
        """The staged directory, created on first use."""
        if self._dir is None:
            self._dir = default_staged_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    def _path(self, staged_id: str) -> Path:
        if not staged_id or "/" in staged_id or "\\" in staged_id or ".." in staged_id:
            raise ValueError(f"invalid staged id {staged_id!r}")
        return self.directory / f"{staged_id}.json"

    def create(self, doc: Dict[str, Any], *, attempts: int = 5) -> None:
        """First save. The file is created exclusively (``O_EXCL``) with mode 0600, so an id that
        already exists can never be overwritten; on a collision the document gets a fresh id."""
        with self._lock:
            for _ in range(attempts):
                path = self._path(doc["id"])
                doc["updated_at"] = now_iso()
                payload = json.dumps(doc, indent=2, ensure_ascii=False, default=str)
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    doc["id"] = new_id()
                    continue
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                return
            raise RuntimeError(f"could not allocate a unique staged id after {attempts} attempts")

    def save(self, doc: Dict[str, Any]) -> None:
        """Persist an existing document (atomic replace)."""
        with self._lock:
            path = self._path(doc["id"])
            tmp = path.with_suffix(".json.tmp")
            doc["updated_at"] = now_iso()
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(doc, indent=2, ensure_ascii=False, default=str))
            os.replace(tmp, path)

    def load(self, staged_id: str) -> Optional[Dict[str, Any]]:
        """The document for *staged_id*, or ``None`` when there is none (or the id is malformed)."""
        with self._lock:
            try:
                path = self._path(staged_id)
            except ValueError:
                return None
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

    def list(self, status: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        """Documents newest first, optionally only those with *status*."""
        with self._lock:
            docs: List[Dict[str, Any]] = []
            for path in self.directory.glob(f"{PREFIX}-*.json"):
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if status and doc.get("status") != status:
                    continue
                docs.append(doc)
        docs.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        return docs[: max(1, limit)]

    def resolve(self, fragment: str) -> Tuple[Optional[str], List[str]]:
        """Map what a human typed to an id: exact id, else an unambiguous suffix or substring."""
        fragment = (fragment or "").strip()
        if not fragment:
            return None, []
        with self._lock:
            try:
                if self._path(fragment).exists():
                    return fragment, [fragment]
            except ValueError:
                return None, []
            ids = sorted(p.stem for p in self.directory.glob(f"{PREFIX}-*.json"))
        needle = fragment.lower()
        candidates = [i for i in ids if i.lower().endswith("-" + needle) or i.lower().endswith(needle)]
        if not candidates:
            candidates = [i for i in ids if needle in i.lower()]
        return (candidates[0], candidates) if len(candidates) == 1 else (None, candidates)

    def prune(
        self, max_age_days: int, *, now: Optional[datetime] = None, dry_run: bool = False
    ) -> List[Dict[str, Any]]:
        """Remove finished documents older than *max_age_days* (``staged`` and ``running`` ones are
        kept). ``max_age_days <= 0`` disables pruning."""
        if max_age_days <= 0:
            return []
        now = now or datetime.now(timezone.utc)
        removed: List[Dict[str, Any]] = []
        with self._lock:
            for doc in self.list(limit=100_000):
                if doc.get("status") in ("staged",) + IN_FLIGHT:
                    continue
                stamp = doc.get("updated_at") or doc.get("created_at") or ""
                try:
                    updated = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                except ValueError:
                    continue
                age_days = (now - updated).total_seconds() / 86400
                if age_days < max_age_days:
                    continue
                removed.append({"id": doc["id"], "status": doc.get("status"), "age_days": round(age_days, 1)})
                if not dry_run:
                    self._path(doc["id"]).unlink(missing_ok=True)
        return removed

    def delete(self, staged_id: str) -> bool:
        """Remove a document; ``True`` when it existed."""
        with self._lock:
            path = self._path(staged_id)
            if path.exists():
                path.unlink()
                return True
            return False


def _acquire(fd: int, timeout: float) -> None:
    """Blocking advisory lock with a deadline: ``fcntl.flock``, or ``msvcrt.locking`` on Windows."""
    try:
        import fcntl
    except ImportError:  # Windows: msvcrt byte-range lock
        import msvcrt

        deadline = time.monotonic() + timeout
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("staged store is locked by another process") from None
                time.sleep(0.05)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError("staged store is locked by another process") from None
            time.sleep(0.05)


def _release(fd: int) -> None:
    """Release the advisory lock taken by :func:`_acquire`."""
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    except ImportError:
        import msvcrt

        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


_store: Optional[StagedStore] = None
_store_lock = threading.Lock()


def get_store() -> StagedStore:
    """The process-wide store, created lazily under the plugin's data directory."""
    global _store
    with _store_lock:
        if _store is None:
            _store = StagedStore()
        return _store


def set_store(store: Optional[StagedStore]) -> None:
    """Test seam / profile switch: replace the process-wide store."""
    global _store
    with _store_lock:
        _store = store
