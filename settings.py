"""Operator-tunable settings, read from ``plugins.entries.gitlab.settings`` in config.yaml.

Defaults are conservative: merges and raw writes are off, every list and body is capped, and staged
writes expire. Nothing here is read from the environment; connection settings live in :mod:`client`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

WRITE_MODES = ("full", "operator_only", "read_only")


@dataclass
class Settings:
    write_mode: str = "full"  # full | operator_only | read_only
    allow_merge: bool = False  # gitlab_mr_write action=merge
    allow_raw_writes: bool = False  # gitlab_api with POST/PUT/PATCH
    allow_raw_delete: bool = False  # gitlab_api with DELETE (also needs allow_raw_writes)
    write_projects: List[str] = field(default_factory=list)  # glob allow-list on path_with_namespace; empty = any
    max_results: int = 100  # cap per list call
    max_diff_bytes: int = 120_000  # cap on rendered diff text per call
    max_file_bytes: int = 200_000  # cap on decoded file content per call
    max_log_lines: int = 300  # cap on job log lines per call
    max_staged_age_hours: int = 24  # staged writes older than this are refused by `run`
    staged_retention_days: int = 90  # finished staged writes are pruned after this; 0 keeps them
    redact_secrets: bool = True  # mask token-shaped strings in job logs, file contents and diffs
    audit_log: bool = True
    audit_log_path: str = ""
    audit_include_request: bool = True
    audit_reads: bool = False  # also record read tool calls (one event each)
    audit_sinks: List[Dict[str, Any]] = field(default_factory=list)  # see sinks.py

    @classmethod
    def from_ctx(cls, ctx: Any) -> Settings:
        """Build from a Hermes ``PluginContext``; every read is guarded so registration never fails."""
        base = cls()
        values: Dict[str, Any] = {}
        for name, default in asdict(base).items():
            try:
                raw = ctx.get_config(name, default=default)
            except Exception:
                raw = default
            values[name] = coerce(name, raw, default)
        settings = cls(**values)
        if settings.write_mode not in WRITE_MODES:
            settings.write_mode = "full"
        return settings

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def public(self) -> Dict[str, Any]:
        """The subset worth showing in status output (no sink configuration, which may name hosts)."""
        return {
            "write_mode": self.write_mode,
            "allow_merge": self.allow_merge,
            "allow_raw_writes": self.allow_raw_writes,
            "allow_raw_delete": self.allow_raw_delete,
            "write_projects": list(self.write_projects),
            "redact_secrets": self.redact_secrets,
            "audit_log": self.audit_log,
            "audit_reads": self.audit_reads,
            "audit_sinks": len(self.audit_sinks),
        }


def coerce(name: str, raw: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw) if raw is not None else default
    if isinstance(default, int):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return value if value >= 0 else default
    if isinstance(default, list):
        if isinstance(raw, str) and name == "write_projects":
            raw = [x.strip() for x in raw.split(",")]
        if not isinstance(raw, list):
            return default
        if name == "audit_sinks":
            return [x for x in raw if isinstance(x, dict)]
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(default, str):
        if raw is None:
            return default
        text = str(raw).strip()
        return text.lower() if name == "write_mode" else text  # paths keep their case
    return raw if raw is not None else default


_current = Settings()


def get_settings() -> Settings:
    return _current


def set_settings(settings: Settings) -> None:
    global _current
    _current = settings
