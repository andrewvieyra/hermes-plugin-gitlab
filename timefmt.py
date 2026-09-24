"""Human-facing time rendering. Storage is always UTC; reports show the operator's zone.

The zone comes from Hermes' ``timezone`` setting (``hermes_time.get_timezone``) when Hermes is
importable, else the host's local zone. Nothing here is used for comparisons or persistence.
"""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Optional


def get_zone() -> Optional[tzinfo]:
    """Configured display zone, or ``None`` meaning the host's local zone."""
    try:
        from hermes_time import get_timezone  # type: ignore

        return get_timezone()
    except Exception:
        return None


def parse_utc(stamp: Optional[str]) -> Optional[datetime]:
    """A stored stamp as an aware datetime (naive stamps are taken as UTC), or ``None`` when it does not parse."""
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def local(stamp: Optional[str], zone: Optional[tzinfo] = None) -> str:
    """``2026-09-09 12:35:50 PDT`` for a stored UTC stamp; the input unchanged when it does not parse."""
    parsed = parse_utc(stamp)
    if parsed is None:
        return stamp or ""
    zone = zone if zone is not None else get_zone()
    shown = parsed.astimezone(zone) if zone is not None else parsed.astimezone()
    name = shown.tzname() or ""
    return shown.strftime("%Y-%m-%d %H:%M:%S") + (f" {name}" if name else "")


def ago(stamp: Optional[str], now: Optional[datetime] = None) -> str:
    """``3h ago`` / ``2d ago`` for listings; empty when the stamp does not parse."""
    parsed = parse_utc(stamp)
    if parsed is None:
        return ""
    now = now or datetime.now(timezone.utc)
    seconds = max(0, int((now - parsed).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"
