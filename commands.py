"""Operator entry points that bypass the model: the ``/gitlab`` slash command inside a session and the
``hermes gitlab`` CLI subcommand. Both reuse the handlers so behaviour is identical everywhere."""

from __future__ import annotations

import json
import shlex
from typing import Any, List

from . import audit, handlers, render, timefmt
from .store import get_store

_HELP = """\
/gitlab — GitLab for Hermes

  status                        Connectivity, GitLab version, token identity and scopes, write mode
  mr <project> <iid>            Summary of a merge request: status, approvals, pipeline, unresolved threads
  pending                       Staged writes waiting for an operator
  show <id>                     One staged write: what it will send, who asked, preconditions
                                (<id> may be an unambiguous suffix, e.g. 4f1a)
  run <id>                      Execute a staged write (the operator path in write_mode operator_only)
  drop <id>                     Discard a staged write
  prune [--days N] [--dry-run]  Delete finished staged writes older than N days (default: staged_retention_days)
  audit [N]                     Last N audit events (default 20)
"""


def _parse(raw: str) -> List[str]:
    """Shell-style split of the slash command's argument string; whitespace split when the quoting is broken."""
    try:
        return shlex.split(raw or "")
    except ValueError:
        return (raw or "").split()


def _result(text: str) -> str:
    """Render a handler's JSON for a human: the ``report`` when present, else pretty JSON."""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if not data.get("success", True) and data.get("error"):
        return f"Error: {data['error']}"
    if data.get("report"):
        return data["report"]
    return json.dumps(data, indent=2, ensure_ascii=False)


def _resolve(fragment: str) -> Any:
    """A staged id from what the operator typed, or the message to show when it is unknown or ambiguous."""
    staged_id, candidates = get_store().resolve(fragment)
    if staged_id is None:
        if not candidates:
            return f"No staged write matches {fragment!r}. Use /gitlab pending to list them."
        return f"{fragment!r} is ambiguous; matches:\n" + "\n".join(f"  {c}" for c in candidates)
    return staged_id


def run(argv: List[str], *, via: str = audit.VIA_SLASH) -> str:
    """``via`` names the operator path for the audit trail: ``slash`` or ``cli``."""
    actor = audit.capture_actor({}, via=via)
    if not argv or argv[0] in {"help", "-h", "--help"}:
        return _HELP
    cmd, rest = argv[0], argv[1:]
    if cmd == "status":
        return _result(handlers.gitlab_api({"path": "status"}, _actor=actor))
    if cmd == "mr":
        if len(rest) < 2:
            return "Usage: /gitlab mr <project> <iid>"
        data = json.loads(
            handlers.gitlab_merge_requests({"action": "get", "project": rest[0], "iid": rest[1]}, _actor=actor)
        )
        if not data.get("success"):
            return f"Error: {data.get('error')}"
        unresolved = None
        scan_truncated = False
        threads = json.loads(
            handlers.gitlab_merge_requests(
                {"action": "discussions", "project": rest[0], "iid": rest[1], "only_unresolved": True}, _actor=actor
            )
        )
        if threads.get("success"):
            unresolved = threads.get("unresolved")
            scan_truncated = not threads.get("scanned_all", True)
        return render.mr_report(
            data["merge_request"],
            data.get("approvals"),
            unresolved,
            data.get("project") or rest[0],
            unresolved_truncated=scan_truncated,
        )
    if cmd == "pending":
        rows = handlers.list_staged(None, limit=100)
        rows = [r for r in rows if r["status"] in {"staged", "running"}]
        if not rows:
            return "No staged writes."
        lines = []
        for r in rows:
            flag = " (expired)" if r.get("expired") else ""
            lines.append(
                f"{r['id']}  {r['status']}{flag}  {r['summary']}\n    by {r.get('requested_by')} at {timefmt.local(r.get('created_at'))}"
            )
        return "\n".join(lines)
    if cmd == "prune":
        days = None
        dry = "--dry-run" in rest
        if "--days" in rest:
            try:
                days = int(rest[rest.index("--days") + 1])
            except (IndexError, ValueError):
                return "Usage: /gitlab prune [--days N] [--dry-run]"
        result = handlers.prune_staged(days=days, dry_run=dry, actor=actor)
        if result["max_age_days"] <= 0:
            return "Retention is disabled (staged_retention_days is 0); pass --days N to prune explicitly."
        if not result["removed"]:
            return f"Nothing older than {result['max_age_days']} days."
        verb = "Would remove" if dry else "Removed"
        lines = [f"{verb} {len(result['removed'])} staged write(s) older than {result['max_age_days']} days:"]
        lines += [f"  {r['id']}  {r['status']:<10} {r['age_days']}d" for r in result["removed"]]
        return "\n".join(lines)
    if cmd == "audit":
        count = 20
        if rest:
            try:
                count = max(1, int(rest[0]))
            except ValueError:
                return "Usage: /gitlab audit [N]"
        records = audit.get_audit_log().tail(count)
        if not records:
            return f"No audit events in {audit.get_audit_log().path}."
        return "\n".join(render.audit_line(r) for r in records)
    if cmd in {"show", "run", "drop"}:
        if not rest:
            return f"Usage: /gitlab {cmd} <id>"
        resolved = _resolve(rest[0])
        if not str(resolved).startswith("glw-"):
            return str(resolved)
        staged_id = str(resolved)
        if cmd == "show":
            doc = get_store().load(staged_id)
            if doc is None:
                return f"Unknown staged write {staged_id}."
            text = render.staged_report(doc)
            req = doc.get("request") or {}
            if req.get("json") is not None or req.get("params"):
                text += (
                    "\n  payload: "
                    + json.dumps(
                        {"params": req.get("params"), "json": req.get("json")}, ensure_ascii=False, default=str
                    )[:4000]
                )
            return text
        if cmd == "run":
            result = handlers.run_staged(staged_id, actor)
            if not result.get("success"):
                return f"Error: {result.get('error')}"
            return result.get("report") or json.dumps(result, indent=2, ensure_ascii=False, default=str)
        result = handlers.drop_staged(staged_id, actor)
        if not result.get("success"):
            return f"Error: {result.get('error')}"
        return f"Dropped {staged_id}: {result.get('summary')}"
    return f"Unknown subcommand: {cmd}\n\n{_HELP}"


def slash_handler(raw_args: str) -> str:
    """Entry point Hermes calls for ``/gitlab …`` inside a session."""
    return run(_parse(raw_args), via=audit.VIA_SLASH)


# -- argparse wiring for ``hermes gitlab ...`` ----------------------------------------------------


def setup_cli(parser: Any) -> None:
    """argparse subcommands for ``hermes gitlab …``."""
    sub = parser.add_subparsers(dest="gitlab_cmd")
    sub.add_parser("status", help="Connectivity, token identity and scopes, write mode")
    p = sub.add_parser("mr", help="Summary of a merge request")
    p.add_argument("project")
    p.add_argument("iid")
    sub.add_parser("pending", help="Staged writes waiting for an operator")
    for name, help_text in (
        ("show", "Show one staged write"),
        ("run", "Execute a staged write"),
        ("drop", "Discard a staged write"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("staged_id")
    p = sub.add_parser("prune", help="Delete finished staged writes older than the retention period")
    p.add_argument("--days", type=int, default=None, help="Override staged_retention_days")
    p.add_argument("--dry-run", action="store_true", help="List what would be removed")
    p = sub.add_parser("audit", help="Last N audit events")
    p.add_argument("count", nargs="?", type=int, default=20)


def cli_handler(args: Any) -> None:
    """Turn the parsed argparse namespace back into argv and run it as the CLI operator path."""
    argv: List[str] = [getattr(args, "gitlab_cmd", None) or "help"]
    for attr in ("project", "iid", "staged_id"):
        value = getattr(args, attr, None)
        if isinstance(value, str) and value:
            argv.append(value)
    if getattr(args, "days", None) is not None:
        argv += ["--days", str(args.days)]
    if getattr(args, "dry_run", False):
        argv.append("--dry-run")
    if argv[0] == "audit":
        argv.append(str(getattr(args, "count", 20)))
    print(run(argv, via=audit.VIA_CLI))
