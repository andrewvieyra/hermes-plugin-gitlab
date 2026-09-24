"""Every write goes through here, whichever tool asked for it.

A handler turns its arguments into a :class:`WriteRequest`: the exact HTTP call, the project it
touches, the preconditions that must still hold, and the settings flags it needs. :func:`perform`
then applies the same rules to every request, in this order:

1. **Gate.** ``write_mode`` (``read_only`` refuses everything), required flags such as
   ``allow_merge``, the ``write_projects`` allow-list, and the sensitive/administrative path
   deny-lists for raw API calls.
2. **Preview.** With ``dry_run`` the request is described and nothing is sent.
3. **Stage.** In ``operator_only`` mode a model-initiated request is written to disk and returned
   with the command a human must run; nothing is sent.
4. **Preconditions.** Head SHAs recorded when the model read the object are re-read and compared;
   a change since then stops the write.
5. **Execute** and audit the outcome.

Nothing in this module is GitLab-resource specific beyond the two precondition kinds.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from . import audit, render, targets
from .client import GitLabClient, GitLabError, validate_path
from .settings import Settings
from .store import IN_FLIGHT, StagedStore, new_id, now_iso


class WriteError(Exception):
    """The arguments cannot be turned into a request. Reported as ``rejected``; nothing was sent."""

    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.field = field


# Credential and secret stores: refused through gitlab_api for every method, GET included, because
# their responses put secrets into the model's context.
_SENSITIVE = [
    r"(^|/)variables(/|$)",
    r"(^|/)deploy_tokens(/|$)",
    r"(^|/)access_tokens(/|$)",
    r"(^|/)deploy_keys(/|$)",
    r"(^|/)hooks(/|$)",
    r"(^|/)triggers(/|$)",  # pipeline trigger tokens
    r"(^|/)trigger(/|$)",  # trigger/pipeline runs with a trigger token: another credential
    r"(^|/)pipeline_schedules/[^/]+$",  # one schedule's record includes its variables with values
    r"(^|/)client_keys(/|$)",  # error tracking DSN keys
    r"(^|/)tokens(/|$)",  # cluster agent tokens and similar
    r"(^|/)secure_files(/|$)",
    r"(^|/)integrations(/|$)",
    r"(^|/)services(/|$)",
    r"(^|/)(export|import)(/|$)",
    r"^personal_access_tokens(?!/self$)",
    r"^user/(keys|gpg_keys|emails|personal_access_tokens|impersonation_tokens|credentials)",
    r"^users/[^/]+/(keys|gpg_keys|emails|impersonation_tokens|personal_access_tokens)",
    r"^application/",
    r"^admin/",
    r"^broadcast_messages",
    r"^license",
    r"^sidekiq",
    r"^system_hooks",
    r"^keys(/|$)",
    r"^geo(/|$)",
    r"(^|/)audit_events(/|$)",
    r"^import/",
]
# Membership, permissions, settings and destructive project/group operations: refused for non-GET
# raw calls even when ``allow_raw_writes`` is on. These need a human with the right role, not an agent.
_ADMIN = [
    r"^users(/|$)",
    r"^groups/[^/]+$",
    r"^projects/[^/]+$",
    r"^(projects|groups)$",  # creating projects or groups
    r"(^|/)merged_branches$",  # deletes every merged branch at once
    r"(^|/)(ldap_group_links|saml_group_links)(/|$)",
    r"(^|/)(access_requests|invitations|billable_members)(/|$)",  # membership by other names
    r"(^|/)job_token_scope(/|$)",
    r"(^|/)pages(/|$)",
    r"(^|/)(protect|unprotect)$",  # legacy branch protection
    r"(^|/)reset_approvals$",
    r"(^|/)fork(/|$)",  # creates a project in another namespace
    r"(^|/)(move|clone)$",  # moves an issue into a project the allow-list never saw
    r"^groups/[^/]+/projects/[^/]+$",  # transfers a project into a group
    r"^personal_access_tokens/self$",  # readable for `status`; revoking or rotating the token in use is not
    r"(^|/)members(/|$)",
    r"(^|/)share(/|$)",
    r"(^|/)protected_(branches|tags|environments)(/|$)",
    r"(^|/)approval_rules(/|$)",
    r"^projects/[^/]+/approvals(/|$)",
    r"^projects/[^/]+/approval_settings",
    r"(^|/)runners(/|$)",
    r"(^|/)(transfer|archive|unarchive|restore)$",
    r"(^|/)(remote_mirrors|push_rule|housekeeping)(/|$)",
    r"(^|/)mirror(/|$)",
    r"^namespaces(/|$)",
    r"^topics(/|$)",
    r"^applications(/|$)",
    r"^oauth(/|$)",
]
# Writes a typed tool guards with a precondition or an extra flag. Through the escape hatch they would
# run with neither, so they are refused and the model is pointed at the typed tool.
_TYPED_ONLY = [
    (
        r"(^|/)merge_requests/\d+/(merge|approve)$",
        "gitlab_mr_write, which binds the write to the reviewed head sha and honours allow_merge",
    ),
    (r"(^|/)repository/commits$", "gitlab_commit, which validates the actions and binds the commit to the branch head"),
]
_SENSITIVE_RE = [re.compile(p) for p in _SENSITIVE]
_ADMIN_RE = [re.compile(p) for p in _ADMIN]
_TYPED_ONLY_RE = [(re.compile(p), hint) for p, hint in _TYPED_ONLY]
# GitLab's API routes accept one format suffix on the last segment and ignore it: `variables.json`,
# `variables.txt` and `merge.foo` reach `variables` and `merge`. Two suffixes or a bare dot do not route.
_FORMAT_SUFFIX_RE = re.compile(r"(?<=[^/.])\.[^/.]+$")
# Query or body keys that change who a call runs as. ``sudo`` impersonates another user with an admin
# token; the others would swap the credential for one the model supplies.
_AUTH_PARAMS = {"sudo", "private_token", "access_token", "oauth_token", "job_token"}


def auth_override(params: Any, body: Any) -> Optional[str]:
    """Why a raw call is refused because of an authentication parameter, or ``None``. Checked wherever
    the model controls the query or the body (``gitlab_api`` GET and every raw write, again at run time
    for staged ones); the typed tools only forward whitelisted keys."""
    for source, name in ((params, "params"), (body, "body")):
        if isinstance(source, dict):
            for key in source:
                # Rack folds `sudo[]` and `sudo[x]` into `sudo` (an array or a hash, which GitLab's user
                # lookup accepts), so the name before any bracket is what counts.
                if str(key).strip().lower().split("[", 1)[0].strip() in _AUTH_PARAMS:
                    return f"gitlab_api refused: {name}.{key} would change who the call runs as; it must run as the configured token and user"
    return None


def path_forms(path: str) -> List[str]:
    """Every spelling GitLab may resolve *path* to: as given, percent-decoded (repeatedly, for double
    encoding), lower-cased, with duplicate slashes collapsed and ``..`` segments resolved (a reverse
    proxy may do that before GitLab sees the path), each with and without a format suffix on the last
    segment (GitLab routes ``variables.json`` like ``variables``). The deny-lists match all of them, so
    ``projects/1/%76ariables``, ``PROJECTS/1//VARIABLES``, ``projects/1/variables.json`` or
    ``projects/1/%2e%2e/%2e%2e/admin`` cannot slip past. The encoded form is kept too, so
    ``projects/group%2Fproj`` still matches the project-level rules."""
    forms: List[str] = []
    current = path
    for _ in range(3):
        for form in (current, current.lower()):
            form = re.sub(r"/{2,}", "/", form).strip("/")
            for candidate in (form, posixpath.normpath(form) if form else form):
                for spelling in (candidate, _FORMAT_SUFFIX_RE.sub("", candidate)):
                    if spelling and spelling not in forms:
                        forms.append(spelling)
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return forms


def raw_path_refusal(method: str, path: str) -> Optional[str]:
    """Why a raw ``gitlab_api`` call to *path* is refused, or ``None`` when it may proceed."""
    forms = path_forms(path)
    for regex in _SENSITIVE_RE:
        if any(regex.search(form) for form in forms):
            return f"gitlab_api refused: {path!r} is a credential or settings surface this plugin never touches"
    if method.upper() != "GET":
        for regex in _ADMIN_RE:
            if any(regex.search(form) for form in forms):
                return f"gitlab_api refused: {method.upper()} {path!r} changes membership, permissions or settings; do it in the GitLab UI"
        for regex, hint in _TYPED_ONLY_RE:
            if any(regex.search(form) for form in forms):
                return f"gitlab_api refused: {method.upper()} {path!r} has a typed tool: use {hint}"
    return None


@dataclass
class WriteRequest:
    """The exact write a tool wants to make, plus everything the gate needs to judge it."""

    action: str  # "issue.create", "mr.merge", "commit.create", "api.POST", ...
    method: str
    path: str  # relative to /api/v4
    summary: str  # one line for humans
    project: Optional[str] = None  # id or path as given
    params: Optional[Dict[str, Any]] = None
    json: Any = None
    target: Dict[str, Any] = field(default_factory=dict)  # {"kind", "iid"|"id", "web_url"}
    preconditions: List[Dict[str, Any]] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)  # settings flags that must be true
    result_kind: str = "raw"
    irreversible: bool = False
    notes: List[str] = field(default_factory=list)  # advisory notes for the preview

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable form; this is what a staged-write file stores."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WriteRequest:
        """Rebuild from :meth:`to_dict` output, ignoring keys an older file may carry."""
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def preview(self) -> Dict[str, Any]:
        """What ``dry_run`` shows: the call, the payload (commit contents clipped), preconditions and required flags."""
        payload = self.json
        if self.action == "commit.create" and isinstance(payload, dict) and isinstance(payload.get("actions"), list):
            payload = dict(payload, actions=[_clip_action(a) for a in payload["actions"]])
        return {
            "summary": self.summary,
            "method": self.method,
            "path": f"/api/v4/{self.path}",
            "params": self.params,
            "json": payload,
            "project": self.project,
            "preconditions": self.preconditions,
            "requires": self.requires,
            "irreversible": self.irreversible,
            "notes": self.notes,
        }


_PREVIEW_CONTENT_CHARS = 200


def _clip_action(action: Any) -> Any:
    """A commit preview shows the head of each file, not the whole content the model just sent."""
    if not isinstance(action, dict) or not isinstance(action.get("content"), str):
        return action
    content = action["content"]
    if len(content) <= _PREVIEW_CONTENT_CHARS:
        return action
    rest = len(content) - _PREVIEW_CONTENT_CHARS
    return dict(
        action, content=content[:_PREVIEW_CONTENT_CHARS] + f"… [{rest} more characters; the full content is sent]"
    )


# -- gate -----------------------------------------------------------------------------------------


def resolve_project_path(client: GitLabClient, project: Any, cache: Dict[str, Any]) -> str:
    """``path_with_namespace`` for an id or path (one GET, memoised per call)."""
    key = str(project)
    if key not in cache:
        if not key.isdigit() and "/" in key:
            cache[key] = key.strip("/")
        else:
            cache[key] = str(client.project(project).get("path_with_namespace") or key)
    return cache[key]


def gate(client: GitLabClient, req: WriteRequest, settings: Settings, cache: Dict[str, Any]) -> Optional[str]:
    """Refusal reason, or ``None`` when the request may proceed (subject to mode-based staging)."""
    if settings.write_mode == "read_only":
        return f"{req.action} refused: write_mode is read_only on this install; reads still work"
    missing = [flag for flag in req.requires if not getattr(settings, flag, False)]
    if missing:
        return (
            f"{req.action} refused: {', '.join(missing)} is off. The operator enables it with "
            f"plugins.entries.gitlab.settings.{missing[0]}: true in config.yaml"
        )
    if req.action.startswith("api."):
        try:  # a staged document is re-gated when it runs, so the path is validated here too, not only in the builder
            validate_path(req.path)
        except GitLabError as exc:
            return f"{req.action} refused: {exc}"
        reason = raw_path_refusal(req.method, req.path) or auth_override(req.params, req.json)
        if reason:
            return reason
    if settings.write_projects:
        if not req.project:
            return f"{req.action} refused: write_projects is set but the request names no project"
        path = resolve_project_path(client, req.project, cache)
        if not targets.matches_allowlist(path, settings.write_projects):
            return (
                f"{req.action} refused: project {path} is not in write_projects ({', '.join(settings.write_projects)})"
            )
    return None


def should_stage(actor: Dict[str, Any], settings: Settings) -> bool:
    """``operator_only`` stages model-initiated writes; operator paths (slash command, CLI) execute directly."""
    return settings.write_mode == "operator_only" and actor.get("kind") != "operator"


# -- preconditions --------------------------------------------------------------------------------


def _sha_matches(expected: str, actual: str) -> bool:
    """Prefix match for shas of at least 7 characters, exact match for shorter ones; case-insensitive."""
    expected, actual = (expected or "").strip().lower(), (actual or "").strip().lower()
    if not expected or not actual:
        return False
    return actual.startswith(expected) if len(expected) >= 7 else actual == expected


def check_preconditions(client: GitLabClient, req: WriteRequest) -> Optional[Dict[str, Any]]:
    """Re-read every recorded head and compare. Returns a conflict description, or ``None``."""
    for pre in req.preconditions:
        kind = pre.get("kind")
        expected = str(pre.get("sha") or "")
        if kind == "branch_head":
            branch = client.get(targets.p_branch(pre["project"], pre["branch"]))
            actual = str(((branch or {}).get("commit") or {}).get("id") or "")
            what = f"branch {pre['branch']}"
        elif kind == "mr_head":
            mr = client.get(targets.p_mr(pre["project"], pre["iid"]))
            state = (mr or {}).get("state")
            if state and state != "opened":
                return {
                    "kind": kind,
                    "expected": "opened",
                    "actual": state,
                    "error": f"merge request !{pre['iid']} is {state}, not open",
                }
            actual = str((mr or {}).get("sha") or "")
            what = f"merge request !{pre['iid']} head"
        else:
            continue
        if not _sha_matches(expected, actual):
            return {
                "kind": kind,
                "expected": expected,
                "actual": actual,
                "error": f"{what} changed since it was read: expected {expected[:12]}, found {actual[:12]}. Re-read it, review again, and retry with the new sha.",
            }
    return None


# -- results --------------------------------------------------------------------------------------

_VERBS = {
    "create": "created",
    "update": "updated",
    "comment": "commented on",
    "approve": "approved",
    "unapprove": "unapproved",
    "merge": "merged",
    "rebase": "rebase started for",
    "resolve": "resolved thread on",
    "cancel_auto_merge": "auto-merge cancelled for",
    "run": "started",
    "retry": "retried",
    "cancel": "cancelled",
    "play": "played",
}


def describe_result(req: WriteRequest, body: Any) -> Dict[str, Any]:
    """Trimmed result data, a one-line report, and the ids worth putting in the audit event."""
    kind = req.result_kind
    verb = _VERBS.get(req.action.split(".", 1)[-1], "done")
    data: Any
    ids: Dict[str, Any] = {}
    report: str
    if kind == "issue":
        data = render.issue(body, full=False)
        ids = {"iid": data.get("iid"), "id": data.get("id"), "web_url": data.get("web_url")}
        report = f"Issue #{data.get('iid')} {verb}: {data.get('title')} [{data.get('state')}] {data.get('web_url')}"
    elif kind == "merge_request":
        data = render.merge_request(body, full=False)
        ids = {"iid": data.get("iid"), "id": data.get("id"), "web_url": data.get("web_url"), "sha": data.get("sha")}
        report = (
            f"Merge request !{data.get('iid')} {verb}: {data.get('title')} [{data.get('state')}] {data.get('web_url')}"
        )
        if req.action == "mr.merge" and isinstance(body, dict) and body.get("merge_commit_sha"):
            report += f" merge commit {str(body['merge_commit_sha'])[:12]}"
    elif kind == "note":
        data = render.note(body)
        anchor = f"{req.target.get('web_url')}#note_{data.get('id')}" if req.target.get("web_url") else None
        ids = {"note_id": data.get("id"), "web_url": anchor}
        report = (
            f"Comment {data.get('id')} posted on {req.target.get('kind', 'item')} {req.target.get('iid') or ''}".strip()
        )
        if anchor:
            report += f" {anchor}"
    elif kind == "discussion":
        data = render.discussion(body)
        first = (data.get("notes") or [{}])[0]
        ids = {"discussion_id": data.get("id"), "note_id": first.get("id")}
        report = f"Thread {str(data.get('id') or '')[:12]} {verb} {req.target.get('kind', '')} {req.target.get('iid') or ''}".strip()
    elif kind == "pipeline":
        data = render.pipeline(body, full=True)
        ids = {"id": data.get("id"), "web_url": data.get("web_url")}
        report = f"Pipeline {data.get('id')} {verb} on {data.get('ref')}: {data.get('status')} {data.get('web_url')}"
    elif kind == "job":
        data = render.job(body)
        ids = {"id": data.get("id"), "web_url": data.get("web_url")}
        report = f"Job {data.get('id')} {data.get('name')} {verb}: {data.get('status')} {data.get('web_url')}"
    elif kind == "commit":
        data = render.commit(body, full=True)
        ids = {"sha": data.get("id"), "web_url": data.get("web_url")}
        report = f"Commit {data.get('short_id')} {verb}: {data.get('title')} {data.get('web_url')}"
    elif kind == "branch":
        data = render.branch(body)
        head = data.get("commit") or {}
        ids = {"branch": data.get("name"), "sha": head.get("id"), "web_url": data.get("web_url")}
        report = f"Branch {data.get('name')} {verb} at {head.get('short_id')} {data.get('web_url')}"
    elif kind == "approval":
        data = render.approvals(body) or body
        ids = {"iid": req.target.get("iid")}
        report = f"Merge request !{req.target.get('iid')} {verb}"
        if isinstance(data, dict) and data.get("approvals_left") is not None:
            report += f": {data.get('approvals_left')} approval(s) still required"
    elif kind == "rebase":
        data = body if isinstance(body, dict) else {"response": body}
        ids = {"iid": req.target.get("iid")}
        report = f"Rebase requested for !{req.target.get('iid')} (GitLab performs it asynchronously; re-read the merge request)"
    else:
        data = body
        if isinstance(data, list) and len(data) > 50:
            data = data[:50]
        report = f"{req.method} /api/v4/{req.path} succeeded"
        if isinstance(body, dict):
            ids = {k: body.get(k) for k in ("id", "iid", "web_url") if k in body}
    return {"data": data, "report": report, "ids": ids}


# -- perform --------------------------------------------------------------------------------------


def _emit(
    event: str, client: GitLabClient, req: WriteRequest, actor: Dict[str, Any], staged_id: Optional[str], **details: Any
) -> None:
    """Audit event carrying the request's action, project and target."""
    audit.emit(
        event,
        actor=actor,
        gitlab_url=client.base_url,
        action=req.action,
        project=req.project,
        target=req.target or None,
        staged_id=staged_id,
        **details,
    )


def perform(
    client: GitLabClient,
    req: WriteRequest,
    *,
    actor: Dict[str, Any],
    settings: Settings,
    store: Optional[StagedStore] = None,
    dry_run: bool = False,
    staged_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Gate, preview, stage, check and execute one write. Always returns a result dict; never raises."""
    base: Dict[str, Any] = {
        "action": req.action,
        "summary": req.summary,
        "project": req.project,
        "target": req.target or None,
    }
    cache: Dict[str, Any] = {}
    try:
        reason = gate(client, req, settings, cache)
    except GitLabError as exc:
        _emit("write_rejected", client, req, actor, staged_id, error=str(exc), http_status=exc.status)
        return {"success": False, "rejected": True, "error": f"could not resolve the project: {exc}", **base}
    if reason:
        _emit(
            "write_refused",
            client,
            req,
            actor,
            staged_id,
            reason=reason,
            dry_run=dry_run,
            write_mode=settings.write_mode,
        )
        return {"success": False, "refused": True, "error": reason, "write_mode": settings.write_mode, **base}
    if dry_run:
        _emit("write_previewed", client, req, actor, staged_id, summary=req.summary, method=req.method, path=req.path)
        return {
            "success": True,
            "dry_run": True,
            "preview": req.preview(),
            "note": "Nothing was sent. Show this to the user; call again without dry_run to execute.",
            **base,
        }
    if staged_id is None and should_stage(actor, settings):
        if store is None:  # never fall through to an immediate write in operator_only mode
            reason = f"{req.action} refused: write_mode is operator_only but no staged-write store is available"
            _emit("write_refused", client, req, actor, staged_id, reason=reason, write_mode=settings.write_mode)
            return {"success": False, "refused": True, "error": reason, "write_mode": settings.write_mode, **base}
        return stage(client, req, actor=actor, settings=settings, store=store)
    try:
        conflict = check_preconditions(client, req)
    except GitLabError as exc:
        _emit(
            "write_failed",
            client,
            req,
            actor,
            staged_id,
            error=str(exc),
            http_status=exc.status,
            request=client.last_call,
            phase="precondition",
        )
        return {"success": False, "error": f"precondition check failed: {exc}", "gitlab": exc.to_dict(), **base}
    if conflict:
        _emit("write_conflict", client, req, actor, staged_id, error=conflict["error"], precondition=conflict)
        return {"success": False, "conflict": True, "error": conflict["error"], "precondition": conflict, **base}
    try:
        body = client.request(req.method, req.path, params=req.params, json=req.json)
    except GitLabError as exc:
        event = "write_conflict" if exc.status == 409 else "write_failed"
        _emit(event, client, req, actor, staged_id, error=str(exc), http_status=exc.status, request=client.last_call)
        return {
            "success": False,
            "conflict": exc.status == 409,
            "error": str(exc),
            "gitlab": exc.to_dict(),
            "request": client.last_call,
            **base,
        }
    described = describe_result(req, body)
    if req.result_kind == "raw" and settings.redact_secrets:
        described["data"], _redacted = render.redact_any(described["data"])
    _emit(
        "write_done",
        client,
        req,
        actor,
        staged_id,
        summary=req.summary,
        http_status=client.last_call.get("status"),
        request=client.last_call,
        result=described["ids"],
        irreversible=req.irreversible,
    )
    return {
        "success": True,
        "report": described["report"],
        "result": described["data"],
        "request": client.last_call,
        **base,
    }


# -- staging --------------------------------------------------------------------------------------


def _expires_at(created_at: str, hours: int) -> str:
    """Expiry stamp for a staged write created at *created_at* (at least one hour ahead)."""
    parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return (parsed + timedelta(hours=max(1, hours))).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def is_expired(doc: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """Whether a staged document's ``expires_at`` has passed; an unparsable stamp counts as expired."""
    now = now or datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(doc.get("expires_at")).replace("Z", "+00:00")) <= now
    except ValueError:
        return True


def stage(
    client: GitLabClient, req: WriteRequest, *, actor: Dict[str, Any], settings: Settings, store: StagedStore
) -> Dict[str, Any]:
    """Write the request to the staged store for an operator and return the instructions the model relays."""
    created = now_iso()
    doc: Dict[str, Any] = {
        "id": new_id(),
        "version": 1,
        "status": "staged",
        "gitlab_url": client.base_url,
        "created_at": created,
        "expires_at": _expires_at(created, settings.max_staged_age_hours),
        "request": req.to_dict(),
        "requested_by": actor,
        "requested_by_text": audit.describe_actor(actor),
        "audit": audit.environment(),
    }
    store.create(doc)
    _emit("write_staged", client, req, actor, doc["id"], summary=req.summary, expires_at=doc["expires_at"])
    return {
        "success": True,
        "staged": True,
        "staged_id": doc["id"],
        "action": req.action,
        "summary": req.summary,
        "project": req.project,
        "target": req.target or None,
        "expires_at": doc["expires_at"],
        "preview": req.preview(),
        "command": f"/gitlab run {doc['id']}",
        "cli": f"hermes gitlab run {doc['id']}",
        "note": (
            "write_mode is operator_only: nothing was sent. Tell the user what was staged and give them the "
            "command above (/gitlab show <id> inspects it first). Do not retry the tool."
        ),
    }


def run_staged(
    client: GitLabClient, staged_id: str, *, actor: Dict[str, Any], settings: Settings, store: StagedStore
) -> Dict[str, Any]:
    """Execute a staged write on behalf of an operator. Claims the document under the cross-process
    lock so the gateway and the CLI cannot both run it."""
    doc = store.load(staged_id)
    if doc is None:
        audit.emit(
            "write_refused", actor=actor, gitlab_url=client.base_url, staged_id=staged_id, reason="unknown staged id"
        )
        return {"success": False, "refused": True, "error": f"unknown staged id {staged_id!r}"}
    req = WriteRequest.from_dict(doc.get("request") or {})

    def refuse(reason: str, **extra: Any) -> Dict[str, Any]:
        _emit("write_refused", client, req, actor, doc["id"], reason=reason, **extra)
        return {"success": False, "refused": True, "error": reason, "staged_id": doc["id"], "status": doc.get("status")}

    if doc.get("gitlab_url") != client.base_url:
        return refuse(f"staged for {doc.get('gitlab_url')}, but GITLAB_URL is {client.base_url}")
    if doc.get("status") != "staged":
        return refuse(f"staged write {doc['id']} is already {doc.get('status')}")
    if is_expired(doc):
        doc["status"] = "expired"
        store.save(doc)
        _emit("staged_expired", client, req, actor, doc["id"], expires_at=doc.get("expires_at"))
        return refuse(f"staged write {doc['id']} expired at {doc.get('expires_at')}; ask for it again")
    try:
        with store.exclusive(store.claim_timeout):
            fresh = store.load(doc["id"]) or doc
            if fresh.get("status") != "staged":
                return refuse(f"staged write {doc['id']} is already {fresh.get('status')}")
            doc = fresh
            doc["status"] = "running"
            doc["run"] = {"started_at": now_iso(), "actor": actor}
            store.save(doc)
    except TimeoutError:
        return refuse(f"staged write {doc['id']} is being run by another process; try again shortly")
    try:
        result = perform(client, req, actor=actor, settings=settings, store=store, staged_id=doc["id"])
    except BaseException as exc:  # Ctrl-C, MemoryError, a bug: never leave the document "running"
        doc["status"] = "failed"
        doc["run"].update(
            {
                "finished_at": now_iso(),
                "outcome": "failed",
                "error": (
                    f"interrupted by {type(exc).__name__}; the request may or may not have reached GitLab, "
                    "check before staging it again"
                ),
            }
        )
        store.save(doc)
        _emit(
            "staged_run",
            client,
            req,
            actor,
            doc["id"],
            outcome="failed",
            summary=req.summary,
            error=doc["run"]["error"],
        )
        raise
    if result.get("success"):
        outcome = "done"
    elif result.get("refused"):
        outcome = "refused"
    elif result.get("conflict"):
        outcome = "conflict"
    else:
        outcome = "failed"
    doc["status"] = outcome
    doc["run"].update(
        {
            "finished_at": now_iso(),
            "outcome": outcome,
            "report": result.get("report"),
            "error": result.get("error"),
            "request": result.get("request"),
            "result": result.get("result") if isinstance(result.get("result"), dict) else None,
        }
    )
    store.save(doc)
    _emit("staged_run", client, req, actor, doc["id"], outcome=outcome, summary=req.summary, error=result.get("error"))
    result["staged_id"] = doc["id"]
    result["outcome"] = outcome
    return result


def drop_staged(client: GitLabClient, staged_id: str, *, actor: Dict[str, Any], store: StagedStore) -> Dict[str, Any]:
    """Discard a staged write. A document left ``running`` by a hard kill (power loss, SIGKILL) can be
    dropped too; that is the only way out for it, and the run may or may not have reached GitLab."""
    doc = store.load(staged_id)
    if doc is None:
        return {"success": False, "error": f"unknown staged id {staged_id!r}"}
    if doc.get("status") not in ("staged",) + IN_FLIGHT:
        return {"success": False, "error": f"staged write {doc['id']} is already {doc.get('status')}"}
    req = WriteRequest.from_dict(doc.get("request") or {})
    doc["status"] = "dropped"
    doc["run"] = {"finished_at": now_iso(), "actor": actor, "outcome": "dropped"}
    store.save(doc)
    _emit("staged_dropped", client, req, actor, doc["id"], summary=req.summary)
    return {"success": True, "staged_id": doc["id"], "status": "dropped", "summary": req.summary}
