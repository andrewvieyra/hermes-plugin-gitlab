"""Tool handlers: ``handler(args: dict, **kwargs) -> str`` (always a JSON string, never raises).

Read handlers fetch, summarise and cap. Write handlers build a :class:`executor.WriteRequest` with
:mod:`writes` and hand it to :func:`executor.perform`; they never call GitLab directly. Everything
here is also reachable from the ``/gitlab`` slash command and the ``hermes gitlab`` CLI through
:mod:`commands`.

Named ``handlers`` rather than ``tools`` so the module can never shadow Hermes' own ``tools`` package
when the repository root is on ``sys.path`` (tests, editable checkouts).
"""

from __future__ import annotations

import base64
import contextlib
import functools
import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import audit, executor, render, targets, writes
from .client import GitLabClient, GitLabError, encode, is_configured, validate_path
from .executor import WriteError
from .settings import get_settings
from .store import get_store
from .version import __version__

_client_factory: Callable[[], GitLabClient] = GitLabClient.from_env

SEARCH_SCOPES = (
    "projects",
    "issues",
    "merge_requests",
    "milestones",
    "users",
    "blobs",
    "commits",
    "wiki_blobs",
    "notes",
)
ADVANCED_SCOPES = {"blobs", "commits", "wiki_blobs", "notes"}
REPO_ACTIONS = ("project", "tree", "file", "commits", "commit", "compare", "branches", "tags")
ISSUE_ACTIONS = ("list", "get")
MR_ACTIONS = ("list", "get", "diffs", "discussions", "commits", "pipelines")
PIPELINE_ACTIONS = ("list", "get", "jobs", "job", "log")
TOKEN_EXPIRY_WARNING_DAYS = 14


def set_client_factory(factory: Optional[Callable[[], GitLabClient]]) -> None:
    """Test seam: swap how handlers obtain a client."""
    global _client_factory
    _client_factory = factory or GitLabClient.from_env


def check_requirements() -> bool:
    """``check_fn`` for registration: tools are exposed only when GITLAB_URL and GITLAB_TOKEN are set."""
    return is_configured()


def check_write_requirements() -> bool:
    """``check_fn`` for the write tools: hidden entirely when write_mode is read_only."""
    return is_configured() and get_settings().write_mode != "read_only"


# -- plumbing -------------------------------------------------------------------------------------


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _ok(**payload: Any) -> str:
    return _json({"success": True, **payload})


def _fail(message: str, **extra: Any) -> str:
    return _json({"success": False, "error": message, **extra})


def _int(value: Any, default: int, *, minimum: int = 1, maximum: Optional[int] = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    number = max(minimum, number)
    return min(number, maximum) if maximum else number


def _flag(args: Dict[str, Any], key: str, default: bool) -> bool:
    """Boolean argument with a default; ``None`` and ``""`` mean "not given", junk is an error."""
    value = targets.to_bool(args.get(key), key, default)
    return default if value is None else value


def _actor_from(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Operator paths (slash command, CLI) pass a ready-made ``_actor``; a model tool call gets one
    captured from Hermes' session context plus the ``task_id`` / ``session_id`` / ``user_task`` kwargs."""
    given = kwargs.get("_actor")
    return dict(given) if isinstance(given, dict) else audit.capture_actor(kwargs, via=audit.VIA_MODEL)


def _gitlab_url() -> Optional[str]:
    with contextlib.suppress(Exception):
        return _client_factory().base_url
    return None


def _audit_read(name: str, args: Dict[str, Any], kwargs: Dict[str, Any], out: str) -> None:
    if not get_settings().audit_reads:
        return
    if name == "gitlab_api" and str(args.get("method") or "GET").strip().upper() != "GET":
        return  # non-GET raw calls are audited by _write as write_* events
    with contextlib.suppress(Exception):
        data = json.loads(out)
        ok = bool(data.get("success"))
        verb = args.get("action") or args.get("scope") or str(args.get("method") or "get").lower()
        audit.emit(
            "read_done" if ok else "read_failed",
            actor=_actor_from(kwargs),
            gitlab_url=_gitlab_url(),
            action=f"{name.replace('gitlab_', '')}.{verb}",
            project=str(args.get("project")) if args.get("project") else None,
            error=None if ok else data.get("error"),
        )


def tool(name: str, *, read: bool = True) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Convert every exception the handler can meet into a structured error result, and audit reads."""

    def decorate(fn: Callable[..., str]) -> Callable[..., str]:
        @functools.wraps(fn)
        def wrapper(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
            args = dict(args or {})
            try:
                out = fn(args, **kwargs)
            except WriteError as exc:
                out = _fail(str(exc), rejected=True, field=exc.field)
            except GitLabError as exc:
                out = _fail(str(exc), status=exc.status or None, gitlab=exc.to_dict())
            except Exception as exc:  # never let a handler raise into the agent loop
                out = _fail(f"{type(exc).__name__}: {exc}")
            if read:
                _audit_read(name, args, kwargs, out)
            return out

        return wrapper

    return decorate


def _project(client: GitLabClient, args: Dict[str, Any], *, required: bool = True) -> Optional[str]:
    if args.get("project") in (None, "") and not required:
        return None
    return targets.project_arg(args.get("project"), client.base_url)


def _action(args: Dict[str, Any], allowed: tuple, default: Optional[str] = None) -> str:
    action = str(args.get("action") or default or "").strip().lower()
    if action not in allowed:
        raise GitLabError(f"action must be one of {', '.join(allowed)} (got {action!r})")
    return action


def _limit(args: Dict[str, Any], default: int) -> int:
    return _int(args.get("limit"), default, maximum=get_settings().max_results)


def _pass(params: Dict[str, Any], args: Dict[str, Any], *keys: str) -> None:
    for key in keys:
        if args.get(key) not in (None, ""):
            params[key] = args[key]


def _redact_content(text: str) -> tuple:
    if get_settings().redact_secrets:
        return render.redact_content(text)
    return text, 0


def _content_redactor() -> Optional[Callable[[str], tuple]]:
    return render.redact_content if get_settings().redact_secrets else None


def _redact_any(obj: Any) -> Tuple[Any, int]:
    """Every string in a raw API result, at any depth (the shape is unknown, so no key list applies)."""
    if not get_settings().redact_secrets:
        return obj, 0
    return render.redact_any(obj)


def _cap_raw(body: Any, max_chars: int) -> Tuple[Any, bool]:
    """A raw GET result is returned as GitLab sent it unless it is larger than ``max_file_bytes``; then it
    is clipped to text so one call cannot dump a whole file or a huge list into the model's context."""
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return body, False
    return (
        render.clip(text, max_chars)
        + " (result larger than max_file_bytes: narrow it with params, use paginate with limit, or a typed tool)",
        True,
    )


_USER_TEXT_KEYS = ("description", "body", "message", "title", "data")


def _redact_user_text(obj: Any) -> Tuple[Any, int]:
    """Mask high-confidence secrets in user-authored text (descriptions, comments, commit messages,
    code-search snippets) before it reaches the model. In place; returns the object and the count."""
    if not get_settings().redact_secrets:
        return obj, 0
    return render.redact_fields(obj, _USER_TEXT_KEYS)


MAX_DIFF_FILES = 1000


def _diff_result(items: List[Dict[str, Any]], args: Dict[str, Any], *, more_files: bool = False) -> Dict[str, Any]:
    """One shape for every diff-returning action: ``diff`` (text) and ``diff_summary`` (files, caps)."""
    settings = get_settings()
    rendered = render.render_diffs(
        items,
        max_bytes=_int(args.get("max_bytes"), settings.max_diff_bytes, maximum=settings.max_diff_bytes),
        paths=targets.str_list(args.get("paths")),
        redact_fn=_content_redactor(),
    )
    text = rendered.pop("text")
    if more_files:
        rendered["truncated"] = True
        rendered["files_truncated"] = True
        rendered["note"] = (
            f"GitLab reports more than {MAX_DIFF_FILES} changed files; only the first {MAX_DIFF_FILES} were fetched"
        )
    return {"diff": text, "diff_summary": rendered}


MAX_DISCUSSION_PAGES = 20


def _scan_discussions(
    client: GitLabClient, path: str, *, include_system: bool, only_unresolved: bool, limit: int
) -> Dict[str, Any]:
    """Walk the discussion pages (GitLab returns them oldest first, and on a long-lived MR the first
    pages are mostly system notes), filter while collecting, and count unresolved threads over
    everything scanned rather than over one page."""
    threads: List[Dict[str, Any]] = []
    unresolved = matched = 0
    total: Optional[int] = None
    scanned_all = False
    page = 1
    for _ in range(MAX_DISCUSSION_PAGES):
        rows = client.get(path, {"per_page": 100, "page": page})
        if not isinstance(rows, list):
            raise GitLabError(f"GET {path} did not return a list", path=path)
        headers = client.last_headers
        if headers.get("x-total", "").isdigit():
            total = int(headers["x-total"])
        for row in rows:
            if not include_system and render.discussion_is_system(row):
                continue
            thread = render.discussion(row)
            if thread["resolvable"] and thread["resolved"] is False:
                unresolved += 1
            elif only_unresolved:
                continue
            matched += 1
            if len(threads) < limit:
                threads.append(thread)
        next_page = headers.get("x-next-page", "")
        if not rows or not next_page.isdigit():
            scanned_all = True
            break
        page = int(next_page)
    return {
        "discussions": threads,
        "count": total,
        "matched": matched,
        "returned": len(threads),
        "unresolved": unresolved,
        "truncated": matched > len(threads) or not scanned_all,
        "scanned_all": scanned_all,
    }


# -- status ---------------------------------------------------------------------------------------


def status(client: GitLabClient) -> Dict[str, Any]:
    settings = get_settings()
    version = client.version()
    me = client.current_user()
    token = client.token_self()
    warnings: List[str] = []
    token_out: Optional[Dict[str, Any]] = None
    if token:
        scopes = token.get("scopes") or []
        token_out = {
            k: token.get(k) for k in ("name", "scopes", "expires_at", "active", "revoked", "last_used_at", "created_at")
        }
        if "api" not in scopes and settings.write_mode != "read_only":
            warnings.append("token lacks the 'api' scope: every write will fail with 403 (read_api is read-only)")
        if not any(s in scopes for s in ("api", "read_api")):
            warnings.append("token has neither 'api' nor 'read_api' scope: most calls will fail")
        if token.get("revoked") or token.get("active") is False:
            warnings.append("token is revoked or inactive")
        expires = token.get("expires_at")
        if expires:
            with contextlib.suppress(Exception):
                from datetime import date

                days = (date.fromisoformat(str(expires)[:10]) - date.today()).days
                if days < 0:
                    warnings.append(f"token expired on {expires}")
                elif days <= TOKEN_EXPIRY_WARNING_DAYS:
                    warnings.append(f"token expires in {days} day(s) on {expires}")
    if me.get("is_admin"):
        warnings.append("token belongs to an administrator; prefer a dedicated bot user with the minimum role")
    data = {
        "url": client.base_url,
        "gitlab": {k: version.get(k) for k in ("version", "revision", "enterprise") if k in version},
        "user": render.user_full(me),
        "token": token_out,
        "plugin": {"version": __version__, **settings.public()},
        "warnings": warnings,
    }
    data["report"] = render.status_report(data)
    return data


# -- read tools -----------------------------------------------------------------------------------


@tool("gitlab_search")
def gitlab_search(args: Dict[str, Any], **_: Any) -> str:
    client = _client_factory()
    scope = str(args.get("scope") or "projects").strip().lower()
    if scope not in SEARCH_SCOPES:
        return _fail(f"scope must be one of {', '.join(SEARCH_SCOPES)}")
    query = str(args.get("query") or "").strip()
    if not query:
        return _fail("query is required")
    limit = _limit(args, 20)
    project = _project(client, args, required=False)
    group = str(args.get("group") or "").strip().strip("/") or None
    if scope == "projects":
        params: Dict[str, Any] = {"search": query, "simple": True, "order_by": "last_activity_at", "sort": "desc"}
        if group:
            path = f"groups/{encode(group)}/projects"
            params["include_subgroups"] = True
        else:
            path = "projects"
            params["search_namespaces"] = True
            if _flag(args, "membership", True):
                params["membership"] = True
        archived = targets.to_bool(args.get("archived"), "archived")
        if archived is not None:
            params["archived"] = archived
        rows, total, truncated = client.paginate(path, params, max_results=limit)
        return _ok(
            scope=scope,
            query=query,
            count=total,
            returned=len(rows),
            truncated=truncated,
            results=[render.project(r) for r in rows],
        )
    params = {"scope": scope, "search": query}
    _pass(params, args, "state", "ref", "order_by", "sort")
    confidential = targets.to_bool(args.get("confidential"), "confidential")
    if confidential is not None:
        params["confidential"] = confidential
    if project:
        path = f"{targets.p_project(project)}/search"
    elif group:
        path = f"groups/{encode(group)}/search"
    else:
        path = "search"
    try:
        rows, total, truncated = client.paginate(path, params, max_results=limit)
    except GitLabError as exc:
        if exc.status in (400, 422) and scope in ADVANCED_SCOPES:
            return _fail(
                f"{exc}. The {scope!r} scope needs advanced search (or exact code search for blobs) enabled on this GitLab; "
                "use gitlab_repo to read files and commits directly.",
                status=exc.status,
            )
        raise
    results, redacted = _redact_user_text([render.search_result(scope, r) for r in rows])
    return _ok(
        scope=scope,
        query=query,
        project=project,
        group=group,
        count=total,
        returned=len(results),
        truncated=truncated,
        redacted=redacted,
        results=results,
    )


@tool("gitlab_repo")
def gitlab_repo(args: Dict[str, Any], **_: Any) -> str:
    client = _client_factory()
    settings = get_settings()
    action = _action(args, REPO_ACTIONS)
    project = _project(client, args)
    ref = str(args.get("ref") or "").strip() or None
    base = targets.p_project(project)
    if action == "project":
        return _ok(project=project, metadata=render.project(client.project(project), full=True))
    if action == "tree":
        params: Dict[str, Any] = {}
        _pass(params, args, "path")
        if ref:
            params["ref"] = ref
        if _flag(args, "recursive", False):
            params["recursive"] = True
        rows, total, truncated = client.paginate(f"{base}/repository/tree", params, max_results=_limit(args, 100))
        return _ok(
            project=project,
            ref=ref,
            path=args.get("path") or "",
            count=total,
            returned=len(rows),
            truncated=truncated,
            entries=[render.tree_entry(r) for r in rows],
        )
    if action == "file":
        path = str(args.get("path") or "").strip().strip("/")
        if not path:
            return _fail("path is required for action=file")
        body = client.get(targets.p_file(project, path), {"ref": ref or "HEAD"})
        raw = (
            base64.b64decode(body.get("content") or "")
            if body.get("encoding", "base64") == "base64"
            else (body.get("content") or "").encode("utf-8")
        )
        size = int(body.get("size") or len(raw))
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _ok(
                project=project,
                path=path,
                ref=ref or "HEAD",
                binary=True,
                size=size,
                blob_id=body.get("blob_id"),
                commit_id=body.get("commit_id"),
                content=None,
            )
        lines = text.split("\n")
        total_lines = len(lines)
        start, end = 1, total_lines
        if args.get("start_line") or args.get("end_line"):  # slice the whole file, then cap the slice
            start = _int(args.get("start_line"), 1)
            end = _int(args.get("end_line"), total_lines, maximum=total_lines)
            if start > total_lines:
                return _fail(
                    f"start_line {start} is past the end of the file ({total_lines} lines)", total_lines=total_lines
                )
            if end < start:
                return _fail(f"end_line {end} is before start_line {start}", total_lines=total_lines)
            text = "\n".join(lines[start - 1 : end])
        truncated = False
        encoded = text.encode("utf-8")
        if len(encoded) > settings.max_file_bytes:
            text = encoded[: settings.max_file_bytes].decode("utf-8", "ignore")
            truncated = True
        text, redacted = _redact_content(text)
        return _ok(
            project=project,
            path=path,
            ref=ref or "HEAD",
            binary=False,
            size=size,
            blob_id=body.get("blob_id"),
            commit_id=body.get("commit_id"),
            last_commit_id=body.get("last_commit_id"),
            total_lines=total_lines,
            lines=f"{start}-{end}",
            truncated=truncated,
            redacted=redacted,
            content=text,
        )
    if action == "commits":
        params = {}
        _pass(params, args, "since", "until", "path", "author")
        if ref:
            params["ref_name"] = ref
        if _flag(args, "all", False):
            params["all"] = True
        if _flag(args, "first_parent", False):
            params["first_parent"] = True
        rows, total, truncated = client.paginate(f"{base}/repository/commits", params, max_results=_limit(args, 20))
        commits, redacted = _redact_user_text([render.commit(r) for r in rows])
        return _ok(
            project=project,
            ref=ref,
            count=total,
            returned=len(rows),
            truncated=truncated,
            redacted=redacted,
            commits=commits,
        )
    if action == "commit":
        sha = str(args.get("sha") or "").strip()
        if not sha:
            return _fail("sha is required for action=commit")
        body = client.get(targets.p_commit(project, sha), {"stats": True})
        commit, redacted = _redact_user_text(render.commit(body, full=True))
        out: Dict[str, Any] = {"project": project, "commit": commit, "redacted": redacted}
        if _flag(args, "include_diff", True):
            full_sha = str(body.get("id") or sha)
            items, _total, more = client.paginate(
                f"{targets.p_commit(project, full_sha)}/diff", max_results=MAX_DIFF_FILES
            )
            out.update(_diff_result(items, args, more_files=more))
        return _ok(**out)
    if action == "compare":
        frm, to = str(args.get("from") or "").strip(), str(args.get("to") or "").strip()
        if not frm or not to:
            return _fail("from and to are required for action=compare (branch, tag or sha)")
        params = {"from": frm, "to": to}
        if _flag(args, "straight", False):
            params["straight"] = True
        body = client.get(f"{base}/repository/compare", params)
        commits, redacted = _redact_user_text([render.commit(c) for c in (body.get("commits") or [])])
        return _ok(
            **{"project": project, "from": frm, "to": to},
            commits=commits,
            redacted=redacted,
            compare_timeout=body.get("compare_timeout"),
            compare_same_ref=body.get("compare_same_ref"),
            web_url=body.get("web_url"),
            **_diff_result(body.get("diffs") or [], args),
        )
    if action == "branches":
        name = str(args.get("name") or "").strip()
        if name:
            return _ok(project=project, branch=render.branch(client.get(targets.p_branch(project, name))))
        params = {}
        _pass(params, args, "search")
        rows, total, truncated = client.paginate(f"{base}/repository/branches", params, max_results=_limit(args, 50))
        return _ok(
            project=project,
            count=total,
            returned=len(rows),
            truncated=truncated,
            branches=[render.branch(r) for r in rows],
        )
    params = {}
    _pass(params, args, "search", "order_by", "sort")
    rows, total, truncated = client.paginate(f"{base}/repository/tags", params, max_results=_limit(args, 50))
    return _ok(
        project=project, count=total, returned=len(rows), truncated=truncated, tags=[render.tag(r) for r in rows]
    )


@tool("gitlab_issues")
def gitlab_issues(args: Dict[str, Any], **_: Any) -> str:
    client = _client_factory()
    action = _action(args, ISSUE_ACTIONS, default="list")
    if action == "get":
        project = _project(client, args)
        iid = targets.positive_int(args.get("iid"), "iid")
        body = client.get(targets.p_issue(project, iid))
        out: Dict[str, Any] = {"project": project, "issue": render.issue(body, full=True)}
        if _flag(args, "include_discussions", True):
            scan = _scan_discussions(
                client,
                f"{targets.p_issue(project, iid)}/discussions",
                include_system=_flag(args, "include_system", False),
                only_unresolved=False,
                limit=_limit(args, 50),
            )
            out["discussions"] = scan["discussions"]
            out["discussions_total"] = scan["count"]
            out["discussions_truncated"] = scan["truncated"]
        with contextlib.suppress(GitLabError):
            related, _t, _u = client.paginate(f"{targets.p_issue(project, iid)}/related_merge_requests", max_results=20)
            out["related_merge_requests"] = [render.merge_request(m) for m in related]
        with contextlib.suppress(GitLabError):
            links, _t, _u = client.paginate(f"{targets.p_issue(project, iid)}/links", max_results=20)
            out["linked_issues"] = [
                dict(render.issue(row), link_type=row.get("link_type")) for row in links if isinstance(row, dict)
            ]
        out, out["redacted"] = _redact_user_text(out)
        return _ok(**out)
    project = _project(client, args, required=False)
    params: Dict[str, Any] = {}
    _pass(
        params,
        args,
        "state",
        "milestone",
        "search",
        "order_by",
        "sort",
        "updated_after",
        "updated_before",
        "created_after",
        "created_before",
        "issue_type",
        "scope",
        "in",
    )
    labels = targets.labels_arg(args.get("labels"))
    if labels:
        params["labels"] = labels
    if args.get("assignee"):
        name = str(args["assignee"]).lstrip("@")
        if name.lower() in {"none", "any"}:
            params["assignee_id"] = name.capitalize()  # only assignee_id understands the None/Any wildcards
        else:
            params["assignee_username"] = name
    if args.get("author"):
        params["author_username"] = str(args["author"]).lstrip("@")
    if args.get("iids"):
        params["iids[]"] = [int(x) for x in targets.str_list(args["iids"])]
    confidential = targets.to_bool(args.get("confidential"), "confidential")
    if confidential is not None:
        params["confidential"] = confidential
    if not project:
        params.setdefault("scope", "all")
    path = f"{targets.p_project(project)}/issues" if project else "issues"
    rows, total, truncated = client.paginate(path, params, max_results=_limit(args, 20))
    issues, redacted = _redact_user_text([render.issue(r) for r in rows])
    return _ok(
        project=project,
        count=total,
        returned=len(rows),
        truncated=truncated,
        redacted=redacted,
        filters=params,
        issues=issues,
    )


@tool("gitlab_merge_requests")
def gitlab_merge_requests(args: Dict[str, Any], **_: Any) -> str:
    client = _client_factory()
    action = _action(args, MR_ACTIONS, default="list")
    if action == "list":
        project = _project(client, args, required=False)
        params: Dict[str, Any] = {}
        _pass(
            params,
            args,
            "state",
            "scope",
            "milestone",
            "search",
            "source_branch",
            "target_branch",
            "order_by",
            "sort",
            "updated_after",
            "updated_before",
            "created_after",
            "created_before",
            "in",
        )
        labels = targets.labels_arg(args.get("labels"))
        if labels:
            params["labels"] = labels
        if args.get("author"):
            params["author_username"] = str(args["author"]).lstrip("@")
        if args.get("reviewer"):
            params["reviewer_username"] = str(args["reviewer"]).lstrip("@")
        if args.get("assignee"):
            name = str(args["assignee"]).lstrip("@")
            if name.lower() in {"none", "any"}:
                params["assignee_id"] = name.capitalize()
            else:
                found = client.user_by_username(name)
                if not found:
                    return _fail(f"no user with username {name!r}")
                params["assignee_id"] = found["id"]
        draft = targets.to_bool(args.get("draft"), "draft")
        if draft is not None:
            params["wip"] = "yes" if draft else "no"
        if args.get("iids"):
            params["iids[]"] = [int(x) for x in targets.str_list(args["iids"])]
        if not project:
            params.setdefault("scope", "all")
        path = f"{targets.p_project(project)}/merge_requests" if project else "merge_requests"
        rows, total, truncated = client.paginate(path, params, max_results=_limit(args, 20))
        merge_requests, redacted = _redact_user_text([render.merge_request(r) for r in rows])
        return _ok(
            project=project,
            count=total,
            returned=len(rows),
            truncated=truncated,
            redacted=redacted,
            filters=params,
            merge_requests=merge_requests,
        )
    project = _project(client, args)
    iid = targets.positive_int(args.get("iid"), "iid")
    base = targets.p_mr(project, iid)
    if action == "get":
        body = client.get(base, {"include_diverged_commits_count": True, "include_rebase_in_progress": True})
        approvals = None
        with contextlib.suppress(GitLabError):
            approvals = render.approvals(client.get(f"{base}/approvals"))
        merge_request, redacted = _redact_user_text(render.merge_request(body, full=True))
        return _ok(
            project=project,
            merge_request=merge_request,
            approvals=approvals,
            redacted=redacted,
            next_steps="Use action=diffs to read the change, action=discussions for review threads, and action=pipelines for CI. Pass sha to gitlab_mr_write for approve/merge.",
        )
    if action == "diffs":
        items, _total, more = client.paginate(f"{base}/diffs", max_results=MAX_DIFF_FILES)
        return _ok(project=project, iid=iid, **_diff_result(items, args, more_files=more))
    if action == "discussions":
        scan = _scan_discussions(
            client,
            f"{base}/discussions",
            include_system=_flag(args, "include_system", False),
            only_unresolved=_flag(args, "only_unresolved", False),
            limit=_limit(args, 50),
        )
        scan, redacted = _redact_user_text(scan)
        return _ok(project=project, iid=iid, redacted=redacted, **scan)
    if action == "commits":
        rows, total, truncated = client.paginate(f"{base}/commits", max_results=_limit(args, 50))
        commits, redacted = _redact_user_text([render.commit(r) for r in rows])
        return _ok(
            project=project,
            iid=iid,
            count=total,
            returned=len(rows),
            truncated=truncated,
            redacted=redacted,
            commits=commits,
        )
    rows, total, truncated = client.paginate(f"{base}/pipelines", max_results=_limit(args, 20))
    return _ok(
        project=project,
        iid=iid,
        count=total,
        returned=len(rows),
        truncated=truncated,
        pipelines=[render.pipeline(r) for r in rows],
    )


@tool("gitlab_pipelines")
def gitlab_pipelines(args: Dict[str, Any], **_: Any) -> str:
    client = _client_factory()
    settings = get_settings()
    action = _action(args, PIPELINE_ACTIONS, default="list")
    project = _project(client, args)
    base = targets.p_project(project)
    if action == "list":
        if _flag(args, "latest", False):
            params: Dict[str, Any] = {}
            _pass(params, args, "ref")
            return _ok(
                project=project, pipeline=render.pipeline(client.get(f"{base}/pipelines/latest", params), full=True)
            )
        params = {}
        _pass(
            params,
            args,
            "status",
            "ref",
            "sha",
            "source",
            "username",
            "order_by",
            "sort",
            "updated_after",
            "updated_before",
        )
        rows, total, truncated = client.paginate(f"{base}/pipelines", params, max_results=_limit(args, 20))
        return _ok(
            project=project,
            count=total,
            returned=len(rows),
            truncated=truncated,
            pipelines=[render.pipeline(r) for r in rows],
        )
    if action == "get":
        pipeline_id = targets.positive_int(args.get("pipeline_id"), "pipeline_id")
        body = client.get(targets.p_pipeline(project, pipeline_id))
        params = {"include_retried": True} if _flag(args, "include_retried", False) else {}
        rows, total, truncated = client.paginate(
            f"{targets.p_pipeline(project, pipeline_id)}/jobs", params, max_results=200
        )
        jobs = [render.job(j) for j in rows]
        bridges: List[Dict[str, Any]] = []
        with contextlib.suppress(GitLabError):  # trigger jobs and the downstream pipelines they started
            rows_b, _tb, _ub = client.paginate(f"{targets.p_pipeline(project, pipeline_id)}/bridges", max_results=50)
            bridges = [render.bridge(b) for b in rows_b]
        failed = [j for j in jobs if j.get("status") == "failed" and not j.get("allow_failure")]
        stages: Dict[str, Dict[str, int]] = {}
        for j in jobs:
            stage = stages.setdefault(str(j.get("stage")), {})
            stage[str(j.get("status"))] = stage.get(str(j.get("status")), 0) + 1
        return _ok(
            project=project,
            pipeline=render.pipeline(body, full=True),
            jobs=jobs,
            jobs_total=total,
            jobs_truncated=truncated,
            failed_jobs=failed,
            stages=stages,
            bridges=bridges,
        )
    if action == "jobs":
        pipeline_id = targets.optional_int(args.get("pipeline_id"), "pipeline_id")
        params = {}
        scope = targets.str_list(args.get("scope"))
        if scope:
            params["scope[]"] = scope
        if _flag(args, "include_retried", False) and pipeline_id:
            params["include_retried"] = True
        path = f"{targets.p_pipeline(project, pipeline_id)}/jobs" if pipeline_id else f"{base}/jobs"
        rows, total, truncated = client.paginate(path, params, max_results=_limit(args, 50))
        return _ok(
            project=project,
            pipeline_id=pipeline_id,
            count=total,
            returned=len(rows),
            truncated=truncated,
            jobs=[render.job(r) for r in rows],
        )
    job_id = targets.positive_int(args.get("job_id"), "job_id")
    job = client.get(targets.p_job(project, job_id))
    if action == "job":
        return _ok(project=project, job=render.job(job))
    trace = client.request("GET", f"{targets.p_job(project, job_id)}/trace", raw=True)
    text = render.clean_job_log(trace or "")
    redacted = 0
    if settings.redact_secrets:
        text, redacted = render.redact_log(text)
    search = str(args.get("search") or "").strip()
    if search:
        snippet, total_lines, matches = render.search_lines(
            text,
            search,
            context=_int(args.get("context"), 2, minimum=0, maximum=20),
            max_matches=_int(args.get("max_matches"), 50, maximum=500),
        )
        return _ok(
            project=project,
            job=render.job(job),
            total_lines=total_lines,
            matches=matches,
            search=search,
            redacted=redacted,
            log=snippet,
        )
    count = _int(args.get("tail_lines"), min(200, settings.max_log_lines), maximum=settings.max_log_lines)
    lines, total_lines = render.tail_lines(text, count)
    return _ok(
        project=project,
        job=render.job(job),
        total_lines=total_lines,
        returned_lines=len(lines),
        truncated=total_lines > len(lines),
        redacted=redacted,
        log="\n".join(lines),
    )


@tool("gitlab_api")
def gitlab_api(args: Dict[str, Any], **kwargs: Any) -> str:
    client = _client_factory()
    method = str(args.get("method") or "GET").strip().upper()
    raw_path = str(args.get("path") or "").strip()
    if raw_path.strip("/") == "status":
        return _ok(**status(client))
    if method != "GET":
        return _write("gitlab_api", writes.raw, args, kwargs)
    path = validate_path(raw_path)
    refusal = executor.raw_path_refusal("GET", path)
    if refusal:
        audit.emit(
            "write_refused",
            actor=_actor_from(kwargs),
            gitlab_url=client.base_url,
            action="api.GET",
            project=targets.project_from_path(path),
            target={"kind": "raw", "path": path},
            reason=refusal,
        )
        return _fail(refusal, refused=True)
    params = args.get("params") if isinstance(args.get("params"), dict) else {}
    settings = get_settings()
    if _flag(args, "paginate", False):
        rows, total, truncated = client.paginate(path, params, max_results=_limit(args, 100))
        rows, redacted = _redact_any(rows)
        results, clipped = _cap_raw(rows, settings.max_file_bytes)
        return _ok(
            method="GET",
            path=f"/api/v4/{path}",
            count=total,
            returned=len(rows),
            truncated=truncated or clipped,
            redacted=redacted,
            results=results,
        )
    body = client.get(path, params)
    truncated = False
    if isinstance(body, list) and len(body) > settings.max_results:
        body, truncated = body[: settings.max_results], True
    body, redacted = _redact_any(body)
    body, clipped = _cap_raw(body, settings.max_file_bytes)
    headers = client.last_headers
    pagination = {k: headers.get(k) for k in ("x-total", "x-page", "x-per-page", "x-next-page") if headers.get(k)}
    return _ok(
        method="GET",
        path=f"/api/v4/{path}",
        truncated=truncated or clipped,
        redacted=redacted,
        pagination=pagination or None,
        result=body,
    )


# -- write tools ----------------------------------------------------------------------------------

_last_prune_at: Optional[float] = None
_PRUNE_INTERVAL = 3600.0


def prune_staged(
    *, days: Optional[int] = None, dry_run: bool = False, actor: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Remove finished staged writes older than *days* (default ``staged_retention_days``)."""
    max_age = get_settings().staged_retention_days if days is None else int(days)
    removed = get_store().prune(max_age, dry_run=dry_run)
    if removed and not dry_run:
        audit.emit(
            "staged_pruned",
            actor=actor,
            gitlab_url=_gitlab_url(),
            max_age_days=max_age,
            count=len(removed),
            staged_ids=[r["id"] for r in removed],
        )
    return {"max_age_days": max_age, "dry_run": dry_run, "removed": removed}


def _maybe_prune(actor: Dict[str, Any]) -> None:
    """Opportunistic retention sweep, at most once per process per hour; never raises."""
    global _last_prune_at
    if get_settings().staged_retention_days <= 0:
        return
    if _last_prune_at is not None and time.monotonic() - _last_prune_at < _PRUNE_INTERVAL:
        return
    _last_prune_at = time.monotonic()
    with contextlib.suppress(Exception):
        prune_staged(actor=actor)


def _write(
    tool_name: str, builder: Callable[..., executor.WriteRequest], args: Dict[str, Any], kwargs: Dict[str, Any]
) -> str:
    client = _client_factory()
    settings = get_settings()
    actor = _actor_from(kwargs)
    label = f"{tool_name.replace('gitlab_', '').replace('_write', '')}.{args.get('action') or str(args.get('method') or '').upper() or 'create'}"
    project = str(args.get("project")) if args.get("project") else None
    try:
        req = builder(client, args, settings)
    except WriteError as exc:
        audit.emit(
            "write_rejected",
            actor=actor,
            gitlab_url=client.base_url,
            action=label,
            project=project,
            error=str(exc),
            field=exc.field,
        )
        return _fail(str(exc), rejected=True, field=exc.field)
    except GitLabError as exc:
        audit.emit(
            "write_rejected",
            actor=actor,
            gitlab_url=client.base_url,
            action=label,
            project=project,
            error=str(exc),
            http_status=exc.status,
        )
        return _fail(
            f"could not prepare the write: {exc}", rejected=True, status=exc.status or None, gitlab=exc.to_dict()
        )
    except Exception as exc:  # a builder bug still leaves an audit trail and never raises into the agent loop
        audit.emit(
            "write_rejected",
            actor=actor,
            gitlab_url=client.base_url,
            action=label,
            project=project,
            error=f"{type(exc).__name__}: {exc}",
        )
        return _fail(f"could not prepare the write: {type(exc).__name__}: {exc}", rejected=True)
    result = executor.perform(
        client, req, actor=actor, settings=settings, store=get_store(), dry_run=_flag(args, "dry_run", False)
    )
    _maybe_prune(actor)
    return _json(result)


@tool("gitlab_issue_write", read=False)
def gitlab_issue_write(args: Dict[str, Any], **kwargs: Any) -> str:
    return _write("gitlab_issue_write", writes.issue, args, kwargs)


@tool("gitlab_mr_write", read=False)
def gitlab_mr_write(args: Dict[str, Any], **kwargs: Any) -> str:
    return _write("gitlab_mr_write", writes.merge_request, args, kwargs)


@tool("gitlab_pipeline_write", read=False)
def gitlab_pipeline_write(args: Dict[str, Any], **kwargs: Any) -> str:
    return _write("gitlab_pipeline_write", writes.pipeline, args, kwargs)


@tool("gitlab_commit", read=False)
def gitlab_commit(args: Dict[str, Any], **kwargs: Any) -> str:
    return _write("gitlab_commit", writes.commit, args, kwargs)


# -- operator entry points (no model involved) ----------------------------------------------------


def run_staged(staged_id: str, actor: Dict[str, Any]) -> Dict[str, Any]:
    client = _client_factory()
    return executor.run_staged(client, staged_id, actor=actor, settings=get_settings(), store=get_store())


def drop_staged(staged_id: str, actor: Dict[str, Any]) -> Dict[str, Any]:
    client = _client_factory()
    return executor.drop_staged(client, staged_id, actor=actor, store=get_store())


def list_staged(status_filter: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    docs = get_store().list(status=status_filter, limit=limit)
    out = []
    for doc in docs:
        req = doc.get("request") or {}
        out.append(
            {
                "id": doc.get("id"),
                "status": doc.get("status"),
                "expired": doc.get("status") == "staged" and executor.is_expired(doc),
                "action": req.get("action"),
                "project": req.get("project"),
                "summary": req.get("summary"),
                "created_at": doc.get("created_at"),
                "expires_at": doc.get("expires_at"),
                "requested_by": doc.get("requested_by_text"),
            }
        )
    return out


HANDLERS: Dict[str, Callable[..., str]] = {
    "gitlab_search": gitlab_search,
    "gitlab_repo": gitlab_repo,
    "gitlab_issues": gitlab_issues,
    "gitlab_merge_requests": gitlab_merge_requests,
    "gitlab_pipelines": gitlab_pipelines,
    "gitlab_api": gitlab_api,
    "gitlab_issue_write": gitlab_issue_write,
    "gitlab_mr_write": gitlab_mr_write,
    "gitlab_pipeline_write": gitlab_pipeline_write,
    "gitlab_commit": gitlab_commit,
}
READ_TOOLS = (
    "gitlab_search",
    "gitlab_repo",
    "gitlab_issues",
    "gitlab_merge_requests",
    "gitlab_pipelines",
    "gitlab_api",
)
WRITE_TOOLS = ("gitlab_issue_write", "gitlab_mr_write", "gitlab_pipeline_write", "gitlab_commit")
