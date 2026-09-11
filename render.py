"""Shape GitLab's verbose objects into what the model needs, and render text for operators.

GitLab returns sixty-field merge requests and megabyte job logs. Everything that reaches the model
goes through a summariser here so tool results stay inside Hermes' result budget. Job logs go
through :func:`redact_log`; file contents, diffs, code-search snippets and user-authored text
(descriptions, comments, commit messages) through :func:`redact_content` / :func:`redact_fields`.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import timefmt

TEXT_CLIP = 20_000  # description bodies
NOTE_CLIP = 6_000  # comment bodies
LINE_CLIP = 2_000  # one log line

# -- text utilities -------------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[=>]")
_SECTION_RE = re.compile(r"section_(?:start|end):\d+:[A-Za-z0-9_.\-]+(?:\[[^\]]*\])?\r?(?:\x1b\[0K)?")


def clip(text: Any, limit: int) -> str:
    """Cut *text* to *limit* characters with an explicit marker (never silently)."""
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [truncated {len(text) - limit} chars]"


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def clean_job_log(text: str) -> str:
    """Remove GitLab section markers and ANSI colour, normalise line endings, and keep only the last
    segment of lines that were rewritten with carriage returns (progress bars)."""
    text = _SECTION_RE.sub("", text or "")
    text = strip_ansi(text)
    lines: List[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if "\r" in line:  # keep the last rendered segment; a bare trailing CR must not erase the line
            segments = [s for s in line.split("\r") if s]
            line = segments[-1] if segments else ""
        lines.append(clip(line.rstrip(), LINE_CLIP))
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def tail_lines(text: str, count: int) -> Tuple[List[str], int]:
    lines = text.split("\n") if text else []
    count = max(1, int(count))
    return lines[-count:], len(lines)


def search_lines(text: str, pattern: str, *, context: int = 2, max_matches: int = 50) -> Tuple[str, int, int]:
    """Lines matching *pattern* (regex, case-insensitive; falls back to a literal) with *context*
    lines around each, merged into windows. Returns ``(snippet, total_lines, match_count)``."""
    lines = text.split("\n") if text else []
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        regex = re.compile(re.escape(pattern), re.IGNORECASE)
    hits = [i for i, line in enumerate(lines) if regex.search(line)]
    shown = hits[: max(1, int(max_matches))]
    if not shown:
        return "", len(lines), 0
    windows: List[List[int]] = []
    for hit in shown:
        start, end = max(0, hit - context), min(len(lines) - 1, hit + context)
        if windows and start <= windows[-1][1] + 1:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    hit_set = set(shown)
    out: List[str] = []
    for index, (start, end) in enumerate(windows):
        if index:
            out.append("--")
        for number in range(start, end + 1):
            marker = ">" if number in hit_set else " "
            out.append(f"{marker}{number + 1:6d}: {lines[number]}")
    return "\n".join(out), len(lines), len(hits)


# -- secret redaction -----------------------------------------------------------------------------

# High-confidence token formats: safe to mask in code, diffs and logs alike.
_HIGH_CONFIDENCE = [
    ("gitlab token", re.compile(r"\bgl[a-z]{2,6}-[A-Za-z0-9_.\-]{20,}")),  # routable tokens carry '.' segments
    ("gitlab runner registration token", re.compile(r"\bGR1348941[A-Za-z0-9_\-]{20,}")),
    ("github token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("openai / anthropic key", re.compile(r"\bsk-(?:ant-|proj-|svcacct-)?[A-Za-z0-9_\-]{20,}")),
    ("stripe key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("hugging face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("pypi token", re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_\-]{40,}")),
    ("bearer header", re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-]{8,}")),
    ("basic auth url", re.compile(r"(?i)(https?://[^/\s:@]+:)[^@\s/]{4,}(?=@)")),
]
# Generic key=value shapes: only for job logs, where a false positive costs nothing.
_LOG_ONLY = [
    (
        "credential assignment",
        re.compile(
            r"(?i)\b([A-Za-z0-9_]*(?:password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|private[_\-]?key)[A-Za-z0-9_]*\s*[=:]\s*)['\"]?([^\s'\"]{8,})"
        ),
    ),
]
_MASK = "[REDACTED]"


def _apply(text: str, rules: Iterable[Tuple[str, re.Pattern[str]]]) -> Tuple[str, int]:
    count = 0
    for _name, regex in rules:

        def _sub(match: re.Match[str]) -> str:
            if match.groups():
                keep = match.group(1) or ""
                return keep + _MASK
            return _MASK

        text, n = regex.subn(_sub, text)
        count += n
    return text, count


def redact_content(text: str) -> Tuple[str, int]:
    """Mask high-confidence secrets (token formats, private keys) in file contents and diffs."""
    return _apply(text or "", _HIGH_CONFIDENCE)


def redact_log(text: str) -> Tuple[str, int]:
    """Mask everything :func:`redact_content` does plus ``password=...`` style assignments."""
    text, first = _apply(text or "", _HIGH_CONFIDENCE)
    text, second = _apply(text, _LOG_ONLY)
    return text, first + second


def redact_fields(obj: Any, keys: Iterable[str]) -> Tuple[Any, int]:
    """:func:`redact_content` applied in place to every string under one of *keys*, at any depth of
    dicts and lists (issue and MR descriptions, note bodies, commit messages, search snippets)."""
    wanted = set(keys)
    count = 0
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in wanted and isinstance(value, str):
                obj[key], n = redact_content(value)
                count += n
            elif isinstance(value, (dict, list)):
                _, n = redact_fields(value, wanted)
                count += n
    elif isinstance(obj, list):
        for value in obj:
            _, n = redact_fields(value, wanted)
            count += n
    return obj, count


def redact_any(obj: Any) -> Tuple[Any, int]:
    """:func:`redact_content` applied to every string at any depth of dicts and lists (raw API results,
    whose shape is unknown). Returns a redacted copy and the count; the input is left untouched."""
    if isinstance(obj, str):
        return redact_content(obj)
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        count = 0
        for key, value in obj.items():
            out[key], n = redact_any(value)
            count += n
        return out, count
    if isinstance(obj, list):
        items: List[Any] = []
        count = 0
        for value in obj:
            item, n = redact_any(value)
            items.append(item)
            count += n
        return items, count
    return obj, 0


# -- summaries -------------------------------------------------------------------------------------


def _get(obj: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(key)
    return default if obj is None else obj


def user(u: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(u, dict):
        return None
    return {"id": u.get("id"), "username": u.get("username"), "name": u.get("name")}


def username(u: Any) -> Optional[str]:
    return u.get("username") if isinstance(u, dict) else None


def usernames(items: Any) -> List[str]:
    return [x["username"] for x in (items or []) if isinstance(x, dict) and x.get("username")]


def user_full(u: Any) -> Dict[str, Any]:
    u = u if isinstance(u, dict) else {}
    return {k: u.get(k) for k in ("id", "username", "name", "state", "web_url", "is_admin", "bot") if k in u}


def project(p: Any, full: bool = False) -> Dict[str, Any]:
    p = p if isinstance(p, dict) else {}
    out = {
        "id": p.get("id"),
        "path_with_namespace": p.get("path_with_namespace"),
        "name": p.get("name"),
        "default_branch": p.get("default_branch"),
        "web_url": p.get("web_url"),
        "description": clip(p.get("description"), 500) or None,
        "visibility": p.get("visibility"),
        "archived": p.get("archived"),
        "last_activity_at": p.get("last_activity_at"),
    }
    if p.get("topics"):
        out["topics"] = p["topics"]
    if full:
        for key in (
            "created_at",
            "ssh_url_to_repo",
            "http_url_to_repo",
            "star_count",
            "forks_count",
            "open_issues_count",
            "empty_repo",
            "merge_method",
            "only_allow_merge_if_pipeline_succeeds",
            "only_allow_merge_if_all_discussions_are_resolved",
            "squash_option",
            "remove_source_branch_after_merge",
        ):
            if key in p:
                out[key] = p[key]
        if isinstance(p.get("namespace"), dict):
            out["namespace"] = {k: p["namespace"].get(k) for k in ("id", "full_path", "kind")}
    return out


def milestone(m: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(m, dict):
        return None
    return {k: m.get(k) for k in ("id", "iid", "title", "state", "due_date", "start_date", "web_url")}


def issue(i: Any, full: bool = False) -> Dict[str, Any]:
    i = i if isinstance(i, dict) else {}
    out = {
        "iid": i.get("iid"),
        "id": i.get("id"),
        "project_id": i.get("project_id"),
        "title": i.get("title"),
        "state": i.get("state"),
        "issue_type": i.get("issue_type"),
        "author": username(i.get("author")),
        "assignees": usernames(i.get("assignees")),
        "labels": i.get("labels") or [],
        "milestone": _get(i, "milestone", "title"),
        "due_date": i.get("due_date"),
        "confidential": i.get("confidential"),
        "user_notes_count": i.get("user_notes_count"),
        "created_at": i.get("created_at"),
        "updated_at": i.get("updated_at"),
        "closed_at": i.get("closed_at"),
        "web_url": i.get("web_url"),
        "reference": _get(i, "references", "full"),
    }
    if full:
        out["description"] = clip(i.get("description"), TEXT_CLIP)
        out["closed_by"] = username(i.get("closed_by"))
        out["weight"] = i.get("weight")
        out["time_stats"] = i.get("time_stats")
        out["discussion_locked"] = i.get("discussion_locked")
        out["merge_requests_count"] = i.get("merge_requests_count")
    return out


def merge_request(m: Any, full: bool = False) -> Dict[str, Any]:
    m = m if isinstance(m, dict) else {}
    head = m.get("head_pipeline") if isinstance(m.get("head_pipeline"), dict) else None
    out = {
        "iid": m.get("iid"),
        "id": m.get("id"),
        "project_id": m.get("project_id"),
        "title": m.get("title"),
        "state": m.get("state"),
        "draft": m.get("draft", m.get("work_in_progress")),
        "author": username(m.get("author")),
        "assignees": usernames(m.get("assignees")),
        "reviewers": usernames(m.get("reviewers")),
        "labels": m.get("labels") or [],
        "source_branch": m.get("source_branch"),
        "target_branch": m.get("target_branch"),
        "sha": m.get("sha"),
        "detailed_merge_status": m.get("detailed_merge_status") or m.get("merge_status"),
        "has_conflicts": m.get("has_conflicts"),
        "changes_count": m.get("changes_count"),
        "user_notes_count": m.get("user_notes_count"),
        "head_pipeline": {"id": head.get("id"), "status": head.get("status"), "web_url": head.get("web_url")}
        if head
        else None,
        "milestone": _get(m, "milestone", "title"),
        "created_at": m.get("created_at"),
        "updated_at": m.get("updated_at"),
        "merged_at": m.get("merged_at"),
        "merged_by": username(m.get("merged_by") or m.get("merge_user")),
        "web_url": m.get("web_url"),
        "reference": _get(m, "references", "full"),
    }
    if full:
        out["description"] = clip(m.get("description"), TEXT_CLIP)
        out["diff_refs"] = m.get("diff_refs")
        out["merge_commit_sha"] = m.get("merge_commit_sha")
        out["squash"] = m.get("squash")
        out["squash_on_merge"] = m.get("squash_on_merge")
        out["should_remove_source_branch"] = m.get("should_remove_source_branch")
        out["force_remove_source_branch"] = m.get("force_remove_source_branch")
        out["merge_when_pipeline_succeeds"] = m.get("merge_when_pipeline_succeeds")
        out["blocking_discussions_resolved"] = m.get("blocking_discussions_resolved")
        out["diverged_commits_count"] = m.get("diverged_commits_count")
        out["rebase_in_progress"] = m.get("rebase_in_progress")
        out["source_project_id"] = m.get("source_project_id")
        out["target_project_id"] = m.get("target_project_id")
        out["closed_at"] = m.get("closed_at")
    return out


def approvals(a: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(a, dict):
        return None
    return {
        "approved": a.get("approved"),
        "approvals_required": a.get("approvals_required"),
        "approvals_left": a.get("approvals_left"),
        "approved_by": [username(x.get("user")) for x in (a.get("approved_by") or []) if isinstance(x, dict)],
        "user_has_approved": a.get("user_has_approved"),
        "user_can_approve": a.get("user_can_approve"),
    }


def pipeline(p: Any, full: bool = False) -> Dict[str, Any]:
    p = p if isinstance(p, dict) else {}
    out = {
        "id": p.get("id"),
        "iid": p.get("iid"),
        "project_id": p.get("project_id"),
        "status": p.get("status"),
        "source": p.get("source"),
        "ref": p.get("ref"),
        "sha": p.get("sha"),
        "name": p.get("name"),
        "user": username(p.get("user")),
        "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
        "started_at": p.get("started_at"),
        "finished_at": p.get("finished_at"),
        "duration": p.get("duration"),
        "queued_duration": p.get("queued_duration"),
        "web_url": p.get("web_url"),
    }
    if full:
        out["before_sha"] = p.get("before_sha")
        out["tag"] = p.get("tag")
        out["yaml_errors"] = p.get("yaml_errors")
        out["coverage"] = p.get("coverage")
        out["detailed_status"] = _get(p, "detailed_status", "text")
    return out


def job(j: Any) -> Dict[str, Any]:
    j = j if isinstance(j, dict) else {}
    artifacts = j.get("artifacts") if isinstance(j.get("artifacts"), list) else []
    return {
        "id": j.get("id"),
        "name": j.get("name"),
        "stage": j.get("stage"),
        "status": j.get("status"),
        "ref": j.get("ref"),
        "allow_failure": j.get("allow_failure"),
        "failure_reason": j.get("failure_reason"),
        "duration": j.get("duration"),
        "queued_duration": j.get("queued_duration"),
        "started_at": j.get("started_at"),
        "finished_at": j.get("finished_at"),
        "pipeline_id": _get(j, "pipeline", "id"),
        "runner": _get(j, "runner", "description"),
        "coverage": j.get("coverage"),
        "artifacts": [a.get("filename") for a in artifacts if isinstance(a, dict) and a.get("filename")],
        "web_url": j.get("web_url"),
    }


def bridge(b: Any) -> Dict[str, Any]:
    """A trigger job together with the downstream pipeline it started."""
    out = job(b)
    down = b.get("downstream_pipeline") if isinstance(b, dict) else None
    out["downstream_pipeline"] = (
        {k: down.get(k) for k in ("id", "project_id", "status", "ref", "sha", "web_url")}
        if isinstance(down, dict)
        else None
    )
    return out


def position(p: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(p, dict):
        return None
    return {k: p.get(k) for k in ("new_path", "old_path", "new_line", "old_line", "position_type")}


def note(n: Any) -> Dict[str, Any]:
    n = n if isinstance(n, dict) else {}
    out = {
        "id": n.get("id"),
        "author": username(n.get("author")),
        "created_at": n.get("created_at"),
        "updated_at": n.get("updated_at"),
        "system": n.get("system"),
        "type": n.get("type"),
        "resolvable": n.get("resolvable"),
        "resolved": n.get("resolved"),
        "resolved_by": username(n.get("resolved_by")),
        "internal": n.get("internal", n.get("confidential")),
        "body": clip(n.get("body"), NOTE_CLIP),
    }
    pos = position(n.get("position"))
    if pos:
        out["position"] = pos
    return out


def discussion(d: Any) -> Dict[str, Any]:
    d = d if isinstance(d, dict) else {}
    notes = [note(n) for n in (d.get("notes") or []) if isinstance(n, dict)]
    resolvable = any(n.get("resolvable") for n in notes)
    resolved = resolvable and all(n.get("resolved") for n in notes if n.get("resolvable"))
    return {
        "id": d.get("id"),
        "individual_note": d.get("individual_note"),
        "resolvable": resolvable,
        "resolved": resolved if resolvable else None,
        "notes": notes,
    }


def discussion_is_system(d: Any) -> bool:
    notes = (d or {}).get("notes") or []
    return bool(notes) and all(isinstance(n, dict) and n.get("system") for n in notes)


def commit(c: Any, full: bool = False) -> Dict[str, Any]:
    c = c if isinstance(c, dict) else {}
    out = {
        "id": c.get("id"),
        "short_id": c.get("short_id"),
        "title": c.get("title"),
        "author_name": c.get("author_name"),
        "authored_date": c.get("authored_date"),
        "committed_date": c.get("committed_date"),
        "parent_ids": [str(p)[:12] for p in (c.get("parent_ids") or [])],
        "web_url": c.get("web_url"),
    }
    if full:
        out["message"] = clip(c.get("message"), TEXT_CLIP)
        out["author_email"] = c.get("author_email")
        out["committer_name"] = c.get("committer_name")
        out["stats"] = c.get("stats")
        out["status"] = c.get("status")
        out["last_pipeline"] = pipeline(c.get("last_pipeline")) if isinstance(c.get("last_pipeline"), dict) else None
    return out


def branch(b: Any) -> Dict[str, Any]:
    b = b if isinstance(b, dict) else {}
    c = b.get("commit") if isinstance(b.get("commit"), dict) else {}
    return {
        "name": b.get("name"),
        "default": b.get("default"),
        "protected": b.get("protected"),
        "merged": b.get("merged"),
        "commit": {
            "id": c.get("id"),
            "short_id": c.get("short_id"),
            "title": c.get("title"),
            "committed_date": c.get("committed_date"),
        },
        "web_url": b.get("web_url"),
    }


def tag(t: Any) -> Dict[str, Any]:
    t = t if isinstance(t, dict) else {}
    c = t.get("commit") if isinstance(t.get("commit"), dict) else {}
    return {
        "name": t.get("name"),
        "message": clip(t.get("message"), 500) or None,
        "target": t.get("target"),
        "protected": t.get("protected"),
        "commit": {
            "id": c.get("id"),
            "short_id": c.get("short_id"),
            "title": c.get("title"),
            "committed_date": c.get("committed_date"),
        },
        "release": _get(t, "release", "tag_name"),
    }


def tree_entry(e: Any) -> Dict[str, Any]:
    e = e if isinstance(e, dict) else {}
    return {"path": e.get("path"), "type": e.get("type"), "mode": e.get("mode"), "id": e.get("id")}


def search_result(scope: str, item: Any) -> Dict[str, Any]:
    if scope == "projects":
        return project(item)
    if scope == "issues":
        return issue(item)
    if scope == "merge_requests":
        return merge_request(item)
    if scope == "commits":
        return commit(item)
    if scope == "milestones":
        return milestone(item) or {}
    if scope == "users":
        return user_full(item)
    if scope in {"blobs", "wiki_blobs"}:
        item = item if isinstance(item, dict) else {}
        return {
            "path": item.get("path") or item.get("filename"),
            "ref": item.get("ref"),
            "project_id": item.get("project_id"),
            "startline": item.get("startline"),
            "data": clip(item.get("data"), NOTE_CLIP),
        }
    if scope == "notes":
        return note(item)
    return item if isinstance(item, dict) else {"value": item}


# -- diffs ----------------------------------------------------------------------------------------


def diff_status(item: Dict[str, Any]) -> str:
    if item.get("new_file"):
        return "added"
    if item.get("deleted_file"):
        return "deleted"
    if item.get("renamed_file"):
        return "renamed"
    return "modified"


def count_changes(diff_text: str) -> Tuple[int, int]:
    adds = dels = 0
    for line in (diff_text or "").split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            adds += 1
        elif line.startswith("-") and not line.startswith("---"):
            dels += 1
    return adds, dels


def _path_matches(item: Dict[str, Any], patterns: List[str]) -> bool:
    if not patterns:
        return True
    candidates = [p for p in (item.get("new_path"), item.get("old_path")) if p]
    return any(
        fnmatch.fnmatchcase(c, pat) or c == pat or c.startswith(pat.rstrip("/") + "/")
        for c in candidates
        for pat in patterns
    )


def render_diffs(
    items: List[Dict[str, Any]],
    *,
    max_bytes: int,
    paths: Optional[List[str]] = None,
    redact_fn: Optional[Callable[[str], Tuple[str, int]]] = None,
) -> Dict[str, Any]:
    """Unified-diff text for a list of GitLab diff objects, capped at *max_bytes*, with a per-file
    summary that is always complete even when the text is truncated."""
    files: List[Dict[str, Any]] = []
    chunks: List[str] = []
    used = 0
    truncated = False
    omitted: List[str] = []
    redacted = 0
    selected = [item for item in items if isinstance(item, dict) and _path_matches(item, paths or [])]
    for item in selected:
        new_path, old_path = item.get("new_path") or "", item.get("old_path") or ""
        status = diff_status(item)
        text = item.get("diff") or ""
        if redact_fn is not None and text:
            text, n = redact_fn(text)
            redacted += n
        adds, dels = count_changes(text)
        entry = {"path": new_path or old_path, "status": status, "additions": adds, "deletions": dels}
        if status == "renamed":
            entry["old_path"] = old_path
        if item.get("too_large") or item.get("collapsed"):
            entry["note"] = "diff not returned by GitLab (too large or collapsed)"
        if item.get("generated_file"):
            entry["generated"] = True
        files.append(entry)
        header = [f"diff --git a/{old_path or new_path} b/{new_path or old_path}"]
        if status == "added":
            header.append("new file")
        elif status == "deleted":
            header.append("deleted file")
        elif status == "renamed":
            header += [f"rename from {old_path}", f"rename to {new_path}"]
        header.append(f"--- {'/dev/null' if status == 'added' else 'a/' + old_path}")
        header.append(f"+++ {'/dev/null' if status == 'deleted' else 'b/' + new_path}")
        block = "\n".join(header) + "\n" + (text if text.endswith("\n") or not text else text + "\n")
        size = len(block.encode("utf-8"))
        if used + size <= max_bytes:
            chunks.append(block)
            used += size
            continue
        truncated = True
        if chunks:  # does not fit: skip it, later (smaller) files may still fit
            omitted.append(entry["path"])
            continue
        marker = "\n… [diff truncated]\n"  # the first file alone exceeds the budget: keep its head
        keep = block.encode("utf-8")[: max(0, max_bytes - len(marker.encode("utf-8")))].decode("utf-8", "ignore")
        chunks.append(keep + marker)
        used += len((keep + marker).encode("utf-8"))
    return {
        "text": "".join(chunks),
        "files": files,
        "total_files": len(selected),
        "shown_files": len(selected) - len(omitted),
        "truncated": truncated,
        "omitted": omitted,
        "redacted": redacted,
        "bytes": used,
    }


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def locate_diff_line(
    diff_text: str, *, new_line: Optional[int] = None, old_line: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """Find a line in a unified diff by its new-file number (or old-file number when *new_line* is
    ``None``). Returns ``{"kind": added|removed|context, "new_line", "old_line"}`` with the number
    that does not apply set to ``None``, or ``None`` when the line is not inside any hunk."""
    old_no = new_no = None
    for raw in (diff_text or "").split("\n"):
        header = _HUNK_RE.match(raw)
        if header:
            old_no, new_no = int(header.group(1)), int(header.group(3))
            continue
        if old_no is None or new_no is None or not raw or raw.startswith("\\"):
            continue
        tag = raw[0]
        if tag == "+":
            entry = {"kind": "added", "new_line": new_no, "old_line": None}
            new_no += 1
        elif tag == "-":
            entry = {"kind": "removed", "new_line": None, "old_line": old_no}
            old_no += 1
        else:
            entry = {"kind": "context", "new_line": new_no, "old_line": old_no}
            new_no += 1
            old_no += 1
        if new_line is not None:
            if entry["new_line"] == new_line:
                return entry
        elif old_line is not None and entry["old_line"] == old_line:
            return entry
    return None


# -- operator text --------------------------------------------------------------------------------


def mr_report(
    mr: Dict[str, Any],
    appr: Optional[Dict[str, Any]],
    unresolved: Optional[int],
    project_path: str,
    *,
    unresolved_truncated: bool = False,
) -> str:
    head = mr.get("head_pipeline") or {}
    lines = [
        f"!{mr.get('iid')} {mr.get('title')}  [{mr.get('state')}{' draft' if mr.get('draft') else ''}]",
        f"  {project_path}: {mr.get('source_branch')} -> {mr.get('target_branch')}  sha {str(mr.get('sha') or '')[:12]}",
        f"  author {mr.get('author')}  reviewers {', '.join(mr.get('reviewers') or []) or '-'}  labels {', '.join(mr.get('labels') or []) or '-'}",
        f"  merge status {mr.get('detailed_merge_status')}  conflicts {mr.get('has_conflicts')}  changes {mr.get('changes_count')}",
        f"  pipeline {head.get('status') or '-'}{(' #' + str(head.get('id'))) if head.get('id') else ''}",
    ]
    if appr:
        lines.append(
            f"  approvals {len(appr.get('approved_by') or [])}/{appr.get('approvals_required')} required, "
            f"{appr.get('approvals_left')} left ({', '.join(appr.get('approved_by') or []) or 'nobody'})"
        )
    if unresolved is not None:
        lines.append(f"  unresolved threads {unresolved}{' or more (scan truncated)' if unresolved_truncated else ''}")
    lines.append(f"  updated {timefmt.local(mr.get('updated_at'))}  {mr.get('web_url')}")
    return "\n".join(lines)


def status_report(data: Dict[str, Any]) -> str:
    gl, me, tok, plugin = data.get("gitlab") or {}, data.get("user") or {}, data.get("token"), data.get("plugin") or {}
    lines = [
        f"GitLab {gl.get('version', '?')} ({gl.get('revision', '?')}) at {data.get('url')}",
        f"Authenticated as {me.get('username')} ({me.get('name')}){' [admin]' if me.get('is_admin') else ''}",
    ]
    if tok:
        expires = tok.get("expires_at") or "never"
        lines.append(f"Token {tok.get('name') or '?'}: scopes {', '.join(tok.get('scopes') or [])}; expires {expires}")
    else:
        lines.append("Token: scopes not readable (OAuth token or older GitLab)")
    lines.append(
        f"Plugin {plugin.get('version')}: write_mode {plugin.get('write_mode')}, allow_merge {plugin.get('allow_merge')}, "
        f"allow_raw_writes {plugin.get('allow_raw_writes')}, write_projects {plugin.get('write_projects') or 'any'}"
    )
    for warning in data.get("warnings") or []:
        lines.append(f"Warning: {warning}")
    return "\n".join(lines)


def staged_report(doc: Dict[str, Any]) -> str:
    req = doc.get("request") or {}
    lines = [
        f"{doc.get('id')}  {doc.get('status')}  {req.get('summary')}",
        f"  {req.get('method')} /api/v4/{req.get('path')}",
        f"  requested {timefmt.local(doc.get('created_at'))} by {doc.get('requested_by_text') or '?'}; expires {timefmt.local(doc.get('expires_at'))}",
    ]
    if req.get("preconditions"):
        for pre in req["preconditions"]:
            lines.append(
                f"  requires {pre.get('kind')} {pre.get('branch') or pre.get('iid') or ''} at {str(pre.get('sha') or '')[:12]}"
            )
    if req.get("requires"):
        lines.append(f"  needs settings {', '.join(req['requires'])}")
    run = doc.get("run")
    if run:
        lines.append(
            f"  run {run.get('outcome')} at {timefmt.local(run.get('finished_at'))}: {run.get('report') or run.get('error') or ''}"
        )
    return "\n".join(lines)


def audit_line(record: Dict[str, Any]) -> str:
    actor = record.get("actor") or {}
    who = actor.get("user_name") or actor.get("user_id") or actor.get("os_user") or "?"
    where = actor.get("platform") or actor.get("via") or "?"
    target = record.get("target") or {}
    what = f"{record.get('action') or ''} {record.get('project') or ''}".strip()
    if target.get("iid") or target.get("id"):
        what += f" {target.get('kind', '')}{'!' if target.get('kind') == 'merge_request' else '#'}{target.get('iid') or target.get('id')}"
    detail = record.get("error") or record.get("reason") or record.get("summary") or ""
    return f"{timefmt.local(record.get('ts'))}  {record.get('event'):<16} {who}@{where}  {what}  {clip(detail, 120)}"
