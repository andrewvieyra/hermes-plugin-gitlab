"""Audit trail: who asked for what, from where, and what happened.

Two outputs, both machine-facing and always UTC:

* **Actor records** embedded in staged-write files (``requested_by``, ``run.actor``) and in every event.
* **An append-only event stream**, one JSON object per line, at ``<plugin-data>/gitlab/audit.jsonl``
  (or ``audit_log_path``). This is what a SIEM tails.

Actor capture reads Hermes' per-session context (``gateway.session_context.get_session_env``), which
the gateway binds per turn and which falls back to process environment for the CLI. Every field is
best-effort and never raises: an audit failure must not block or fail a GitLab operation.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .settings import get_settings
from .store import default_staged_dir, now_iso
from .version import __version__

logger = logging.getLogger(__name__)

SCHEMA = 1
PLUGIN = "gitlab"
REQUEST_TEXT_LIMIT = 500

# Hermes session variables -> actor field names. Missing values are recorded as null so every
# event carries the same keys (stable field mapping for SIEM ingestion).
_SESSION_FIELDS = (
    ("HERMES_SESSION_PLATFORM", "platform"),
    ("HERMES_SESSION_SOURCE", "source"),
    ("HERMES_SESSION_PROFILE", "profile"),
    ("HERMES_SESSION_CHAT_ID", "chat_id"),
    ("HERMES_SESSION_CHAT_NAME", "chat_name"),
    ("HERMES_SESSION_CHAT_TYPE", "chat_type"),
    ("HERMES_SESSION_THREAD_ID", "thread_id"),
    ("HERMES_SESSION_SCOPE_ID", "scope_id"),
    ("HERMES_SESSION_USER_ID", "user_id"),
    ("HERMES_SESSION_USER_ID_ALT", "user_id_alt"),
    ("HERMES_SESSION_USER_NAME", "user_name"),
    ("HERMES_SESSION_MESSAGE_ID", "message_id"),
    ("HERMES_SESSION_KEY", "session_key"),
    ("HERMES_SESSION_ID", "session_id"),
)

VIA_MODEL = "tool"
VIA_SLASH = "slash"
VIA_CLI = "cli"


def _session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env  # type: ignore

        return get_session_env(name, "") or ""
    except Exception:
        return os.getenv(name, "") or ""


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _hermes_version() -> Optional[str]:
    try:
        from hermes_cli import __version__ as hv  # type: ignore

        return str(hv)
    except Exception:
        return None


def environment() -> Dict[str, Any]:
    """Process-level facts: where the plugin ran and which versions."""
    try:
        host = socket.gethostname()
    except Exception:
        host = None
    try:
        os_user = getpass.getuser()
    except Exception:
        os_user = None
    return {
        "host": host,
        "os_user": os_user,
        "pid": os.getpid(),
        "plugin_version": __version__,
        "hermes_version": _hermes_version(),
        "hermes_home": os.environ.get("HERMES_HOME") or None,
    }


def capture_actor(kwargs: Optional[Dict[str, Any]] = None, *, via: str = VIA_MODEL) -> Dict[str, Any]:
    """Who is acting and through which path. ``kwargs`` are the handler kwargs Hermes passed
    (``task_id``, ``session_id``, ``user_task``). ``via`` is ``tool`` (the model called a tool),
    ``slash`` (a human typed ``/gitlab``) or ``cli`` (a human ran ``hermes gitlab``)."""
    kwargs = kwargs or {}
    actor: Dict[str, Any] = {"kind": "model" if via == VIA_MODEL else "operator", "via": via}
    for env_name, field in _SESSION_FIELDS:
        value = _session_env(env_name)
        actor[field] = value or None
    if kwargs.get("session_id"):
        actor["session_id"] = str(kwargs["session_id"])
    actor["task_id"] = str(kwargs["task_id"]) if kwargs.get("task_id") else None
    actor["cron"] = _truthy(_session_env("HERMES_CRON_SESSION"))
    actor["os_user"] = environment()["os_user"]
    request = kwargs.get("user_task")
    if get_settings().audit_include_request and isinstance(request, str) and request.strip():
        text = request.strip()
        actor["request"] = text if len(text) <= REQUEST_TEXT_LIMIT else text[: REQUEST_TEXT_LIMIT - 1] + "…"
    else:
        actor["request"] = None
    return actor


def describe_actor(actor: Optional[Dict[str, Any]]) -> str:
    """One human-readable phrase: ``model via tool on signal for Andrew in dm Andrew`` or
    ``operator via cli (hermes)``."""
    if not actor:
        return "unknown"
    where = actor.get("platform") or actor.get("source") or ("cli" if actor.get("via") == VIA_CLI else "unknown")
    text = f"{actor.get('kind', 'unknown')} via {actor.get('via') or where}"
    if actor.get("via") in {VIA_MODEL, VIA_SLASH} and where not in {actor.get("via"), None}:
        text += f" on {where}"
    who = actor.get("user_name") or actor.get("user_id")
    if who:
        text += f" for {who}"
    elif actor.get("os_user"):
        text += f" ({actor['os_user']})"
    chat = actor.get("chat_name") or actor.get("chat_id")
    if chat and actor.get("chat_type"):
        text += f" in {actor['chat_type']} {chat}"
    if actor.get("cron"):
        text += " [cron]"
    return text


class AuditLog:
    """Append-only JSON Lines writer. One ``open`` per event keeps the file safe to rotate with
    ``copytruncate`` and lets several processes append without coordination (POSIX ``O_APPEND``)."""

    def __init__(self, path: Optional[Path] = None, enabled: bool = True):
        self._path = Path(path) if path else None
        self.enabled = enabled
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        if self._path is None:
            self._path = default_staged_dir().parent / "audit.jsonl"
        return self._path

    def emit(
        self,
        event: str,
        *,
        actor: Optional[Dict[str, Any]] = None,
        gitlab_url: Optional[str] = None,
        action: Optional[str] = None,
        project: Optional[str] = None,
        target: Optional[Dict[str, Any]] = None,
        staged_id: Optional[str] = None,
        **details: Any,
    ) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        record: Dict[str, Any] = {
            "ts": now_iso(),
            "schema": SCHEMA,
            "plugin": PLUGIN,
            "event": event,
            "gitlab_url": gitlab_url,
            "action": action,
            "project": project,
            "target": target,
            "staged_id": staged_id,
            "actor": actor,
            "host": environment()["host"],
            "pid": os.getpid(),
        }
        record.update(details)
        try:
            line = json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":"))
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:  # never let auditing break the operation it describes
            logger.warning("gitlab audit log write failed (%s): %s", self.path, exc)
        try:
            from . import sinks

            sinks.dispatch(record)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("gitlab audit sinks unavailable: %s", exc)
        return record

    def tail(self, count: int = 20) -> List[Dict[str, Any]]:
        """The last *count* records, oldest first. Reads the whole file; the audit log is small per day."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except Exception as exc:
            logger.warning("gitlab audit log read failed (%s): %s", self.path, exc)
            return []
        out: List[Dict[str, Any]] = []
        for line in lines[-max(1, count) :]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


_log: Optional[AuditLog] = None
_log_lock = threading.Lock()


def get_audit_log() -> AuditLog:
    """Process-wide log configured from settings (``audit_log``, ``audit_log_path``)."""
    global _log
    with _log_lock:
        if _log is None:
            s = get_settings()
            path = Path(s.audit_log_path).expanduser() if s.audit_log_path else None
            _log = AuditLog(path, enabled=s.audit_log)
        return _log


def set_audit_log(log: Optional[AuditLog]) -> None:
    """Test seam / settings reload."""
    global _log
    with _log_lock:
        _log = log


def emit(event: str, **fields: Any) -> Optional[Dict[str, Any]]:
    return get_audit_log().emit(event, **fields)
