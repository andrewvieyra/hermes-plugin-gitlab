"""Builders: tool arguments -> :class:`executor.WriteRequest`.

Each builder validates the arguments, resolves names to ids with read calls, records the
preconditions the write depends on, and returns the exact request. Nothing here sends a write;
:func:`executor.perform` does, under the gate. Builders raise :class:`executor.WriteError` for bad
input and let :class:`client.GitLabError` propagate for failed lookups.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from . import render, targets
from .client import GitLabClient, GitLabError
from .executor import WriteError, WriteRequest
from .settings import Settings

ISSUE_ACTIONS = ("create", "update", "comment")
MR_ACTIONS = ("create", "update", "comment", "approve", "unapprove", "merge", "rebase", "resolve")
PIPELINE_ACTIONS = ("run", "retry", "cancel", "play")
COMMIT_ACTIONS = ("create", "update", "delete", "move", "chmod")
ISSUE_TYPES = ("issue", "incident", "test_case", "task")
RAW_METHODS = ("POST", "PUT", "PATCH", "DELETE")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_VAR_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_COMMIT_ACTIONS = 100
MAX_COMMIT_BYTES = 2_000_000


# -- argument helpers -----------------------------------------------------------------------------


def _project(client: GitLabClient, args: Dict[str, Any]) -> str:
    try:
        return targets.project_arg(args.get("project"), client.base_url)
    except GitLabError as exc:
        raise WriteError(str(exc), field="project") from None


def _action(args: Dict[str, Any], allowed: tuple) -> str:
    action = str(args.get("action") or "").strip().lower()
    if action not in allowed:
        raise WriteError(f"action must be one of {', '.join(allowed)} (got {action!r})", field="action")
    return action


def _iid(args: Dict[str, Any], name: str = "iid") -> int:
    try:
        return targets.positive_int(args.get(name), name)
    except GitLabError as exc:
        raise WriteError(str(exc), field=name) from None


def _text(args: Dict[str, Any], key: str, *, required: bool = False, limit: int = 1_000_000) -> Optional[str]:
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise WriteError(f"{key} is required", field=key)
        return None
    if not isinstance(value, str):
        value = str(value)
    if len(value) > limit:
        raise WriteError(f"{key} is longer than {limit} characters", field=key)
    return value


def _flag(args: Dict[str, Any], key: str, default: Optional[bool] = None) -> Optional[bool]:
    """Boolean argument: ``None`` and ``""`` mean "not given" (*default*); junk is rejected."""
    try:
        return targets.to_bool(args.get(key), key, default)
    except GitLabError as exc:
        raise WriteError(str(exc), field=key) from None


def _set_flag(payload: Dict[str, Any], args: Dict[str, Any], key: str, param: Optional[str] = None) -> None:
    """Copy a boolean argument into the payload only when the model actually gave one."""
    value = _flag(args, key)
    if value is not None:
        payload[param or key] = value


def _date(args: Dict[str, Any], key: str) -> Optional[str]:
    value = args.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
        raise WriteError(f"{key} must be YYYY-MM-DD (got {value!r})", field=key)
    return value.strip()


def _sha(args: Dict[str, Any], key: str, *, required: bool = False) -> Optional[str]:
    value = args.get(key)
    if value is None or value == "":
        if required:
            raise WriteError(f"{key} is required", field=key)
        return None
    text = str(value).strip()
    if not _SHA_RE.match(text):
        raise WriteError(f"{key} must be a commit sha (7 to 40 hex characters, got {text!r})", field=key)
    return text


def _user_ids(client: GitLabClient, names: Any, field: str) -> Optional[List[int]]:
    """Usernames -> ids. ``[]`` means "clear"; ``None`` means "not given"."""
    if names is None:
        return None
    if isinstance(names, str) and not names.strip():
        return []
    ids: List[int] = []
    for name in targets.str_list(names):
        if name.isdigit():
            ids.append(int(name))
            continue
        found = client.user_by_username(name)
        if not found or not found.get("id"):
            raise WriteError(f"{field}: no user with username {name!r}", field=field)
        ids.append(int(found["id"]))
    return ids


def _milestone_id(client: GitLabClient, project: str, title: Any) -> Optional[int]:
    """Milestone title -> id. ``""`` clears (GitLab takes 0); ``None`` means not given."""
    if title is None:
        return None
    text = str(title).strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    rows = client.get(f"{targets.p_project(project)}/milestones", {"title": text, "include_ancestors": True})
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get("title", "")).lower() == text.lower():
            return int(row["id"])
    raise WriteError(f"milestone: no milestone titled {text!r} in {project}", field="milestone")


def _labels_into(payload: Dict[str, Any], args: Dict[str, Any]) -> None:
    for key in ("labels", "add_labels", "remove_labels"):
        if args.get(key) is not None:
            value = targets.labels_arg(args[key])
            payload[key] = value if value is not None else ""


def _target(kind: str, iid: Optional[int] = None, web_url: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"kind": kind}
    if iid is not None:
        out["iid"] = iid
    if web_url:
        out["web_url"] = web_url
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def _expand_sha(client: GitLabClient, project: str, iid: int, sha: Optional[str]) -> Optional[str]:
    """GitLab compares ``sha`` exactly, so a short sha the model copied from a listing is expanded to
    the full head when it is a prefix of it. A sha that does not match is kept as given: the
    executor's precondition then reports the conflict with both values."""
    if not sha or len(sha) >= 40:
        return sha
    head = str((client.get(targets.p_mr(project, iid)) or {}).get("sha") or "")
    return head if head.lower().startswith(sha.lower()) else sha


# -- issues ---------------------------------------------------------------------------------------


def issue(client: GitLabClient, args: Dict[str, Any], settings: Settings) -> WriteRequest:
    action = _action(args, ISSUE_ACTIONS)
    project = _project(client, args)
    if action == "create":
        return _issue_create(client, project, args)
    if action == "update":
        return _issue_update(client, project, args)
    return _issue_comment(client, project, args)


def _issue_create(client: GitLabClient, project: str, args: Dict[str, Any]) -> WriteRequest:
    title = _text(args, "title", required=True, limit=255)
    payload: Dict[str, Any] = {"title": title}
    description = _text(args, "description")
    if description is not None:
        payload["description"] = description
    _labels_into(payload, args)
    assignees = _user_ids(client, args.get("assignees"), "assignees")
    if assignees is not None:
        payload["assignee_ids"] = assignees
    milestone = _milestone_id(client, project, args.get("milestone"))
    if milestone:
        payload["milestone_id"] = milestone
    due = _date(args, "due_date")
    if due:
        payload["due_date"] = due
    _set_flag(payload, args, "confidential")
    if args.get("issue_type"):
        kind = str(args["issue_type"]).strip().lower()
        if kind not in ISSUE_TYPES:
            raise WriteError(f"issue_type must be one of {', '.join(ISSUE_TYPES)}", field="issue_type")
        payload["issue_type"] = kind
    return WriteRequest(
        action="issue.create",
        method="POST",
        path=f"{targets.p_project(project)}/issues",
        summary=f"Create issue in {project}: {title}",
        project=project,
        json=payload,
        target=_target("issue"),
        result_kind="issue",
    )


def _issue_update(client: GitLabClient, project: str, args: Dict[str, Any]) -> WriteRequest:
    iid = _iid(args)
    payload: Dict[str, Any] = {}
    for key, limit in (("title", 255), ("description", 1_000_000)):
        value = _text(args, key, limit=limit)
        if value is not None:
            payload[key] = value
    _labels_into(payload, args)
    assignees = _user_ids(client, args.get("assignees"), "assignees")
    if assignees is not None:
        payload["assignee_ids"] = assignees or [0]
    milestone = _milestone_id(client, project, args.get("milestone"))
    if milestone is not None:
        payload["milestone_id"] = milestone
    due = _date(args, "due_date")
    if due:
        payload["due_date"] = due
    for key in ("confidential", "discussion_locked"):
        _set_flag(payload, args, key)
    state = str(args.get("state") or "").strip().lower()
    if state:
        if state not in {"close", "closed", "reopen", "reopened", "open"}:
            raise WriteError("state must be close or reopen", field="state")
        payload["state_event"] = "close" if state.startswith("close") else "reopen"
    if not payload:
        raise WriteError(
            "nothing to update: pass title, description, labels, assignees, milestone, due_date, state, ..."
        )
    changed = ", ".join(sorted(payload))
    return WriteRequest(
        action="issue.update",
        method="PUT",
        path=targets.p_issue(project, iid),
        summary=f"Update issue #{iid} in {project}: {changed}",
        project=project,
        json=payload,
        target=_target("issue", iid),
        result_kind="issue",
    )


def _issue_comment(client: GitLabClient, project: str, args: Dict[str, Any]) -> WriteRequest:
    iid = _iid(args)
    body = _text(args, "body", required=True)
    discussion_id = _text(args, "discussion_id")
    payload: Dict[str, Any] = {"body": body}
    web_url = f"{client.base_url}/{project}/-/issues/{iid}" if not project.isdigit() else None
    if discussion_id:
        return WriteRequest(
            action="issue.comment",
            method="POST",
            path=f"{targets.p_issue(project, iid)}/discussions/{discussion_id}/notes",
            summary=f"Reply in thread {discussion_id[:12]} on issue #{iid} in {project}",
            project=project,
            json=payload,
            target=_target("issue", iid, web_url, discussion_id=discussion_id),
            result_kind="note",
        )
    _set_flag(payload, args, "internal")
    return WriteRequest(
        action="issue.comment",
        method="POST",
        path=f"{targets.p_issue(project, iid)}/notes",
        summary=f"Comment on issue #{iid} in {project}",
        project=project,
        json=payload,
        target=_target("issue", iid, web_url),
        result_kind="note",
    )


# -- merge requests -------------------------------------------------------------------------------


def merge_request(client: GitLabClient, args: Dict[str, Any], settings: Settings) -> WriteRequest:
    action = _action(args, MR_ACTIONS)
    project = _project(client, args)
    builder = {
        "create": _mr_create,
        "update": _mr_update,
        "comment": _mr_comment,
        "approve": _mr_approve,
        "unapprove": _mr_unapprove,
        "merge": _mr_merge,
        "rebase": _mr_rebase,
        "resolve": _mr_resolve,
    }[action]
    return builder(client, project, args)


def _draft_title(title: str, draft: Optional[bool]) -> str:
    is_draft = bool(re.match(r"^\s*(\[draft\]|draft:|\(draft\))", title, re.IGNORECASE))
    if draft is True and not is_draft:
        return f"Draft: {title}"
    if draft is False and is_draft:
        return re.sub(r"^\s*(\[draft\]|draft:|\(draft\))\s*", "", title, flags=re.IGNORECASE)
    return title


def _mr_web_url(client: GitLabClient, project: str, iid: int) -> Optional[str]:
    return f"{client.base_url}/{project}/-/merge_requests/{iid}" if not project.isdigit() else None


def _mr_create(client: GitLabClient, project: str, args: Dict[str, Any]) -> WriteRequest:
    source = _text(args, "source_branch", required=True, limit=255)
    target_branch = _text(args, "target_branch", limit=255)
    if not target_branch:
        target_branch = str(client.project(project).get("default_branch") or "")
        if not target_branch:
            raise WriteError("target_branch is required (the project has no default branch)", field="target_branch")
    title = _draft_title(_text(args, "title", required=True, limit=255) or "", _flag(args, "draft"))
    payload: Dict[str, Any] = {"source_branch": source, "target_branch": target_branch, "title": title}
    description = _text(args, "description")
    if description is not None:
        payload["description"] = description
    _labels_into(payload, args)
    for key, param in (("assignees", "assignee_ids"), ("reviewers", "reviewer_ids")):
        ids = _user_ids(client, args.get(key), key)
        if ids is not None:
            payload[param] = ids
    milestone = _milestone_id(client, project, args.get("milestone"))
    if milestone:
        payload["milestone_id"] = milestone
    _set_flag(payload, args, "remove_source_branch")
    _set_flag(payload, args, "squash")
    return WriteRequest(
        action="mr.create",
        method="POST",
        path=f"{targets.p_project(project)}/merge_requests",
        summary=f"Create merge request in {project}: {source} -> {target_branch}: {title}",
        project=project,
        json=payload,
        target=_target("merge_request"),
        result_kind="merge_request",
    )


def _mr_update(client: GitLabClient, project: str, args: Dict[str, Any]) -> WriteRequest:
    iid = _iid(args)
    payload: Dict[str, Any] = {}
    title = _text(args, "title", limit=255)
    draft = _flag(args, "draft")
    if draft is not None and title is None:
        title = str(client.get(targets.p_mr(project, iid)).get("title") or "")
    if title is not None:
        payload["title"] = _draft_title(title, draft)
    for key in ("description", "target_branch"):
        value = _text(args, key)
        if value is not None:
            payload[key] = value
    _labels_into(payload, args)
    for key, param in (("assignees", "assignee_ids"), ("reviewers", "reviewer_ids")):
        ids = _user_ids(client, args.get(key), key)
        if ids is not None:
            payload[param] = ids or [0]
    milestone = _milestone_id(client, project, args.get("milestone"))
    if milestone is not None:
        payload["milestone_id"] = milestone
    for key in ("squash", "remove_source_branch", "discussion_locked"):
        _set_flag(payload, args, key)
    state = str(args.get("state") or "").strip().lower()
    if state:
        if state not in {"close", "closed", "reopen", "reopened", "open"}:
            raise WriteError("state must be close or reopen", field="state")
        payload["state_event"] = "close" if state.startswith("close") else "reopen"
    if not payload:
        raise WriteError("nothing to update: pass title, description, labels, reviewers, state, draft, ...")
    return WriteRequest(
        action="mr.update",
        method="PUT",
        path=targets.p_mr(project, iid),
        summary=f"Update merge request !{iid} in {project}: {', '.join(sorted(payload))}",
        project=project,
        json=payload,
        target=_target("merge_request", iid, _mr_web_url(client, project, iid)),
        result_kind="merge_request",
    )


def _mr_comment(client: GitLabClient, project: str, args: Dict[str, Any]) -> WriteRequest:
    iid = _iid(args)
    body = _text(args, "body", required=True)
    discussion_id = _text(args, "discussion_id")
    web_url = _mr_web_url(client, project, iid)
    if discussion_id:
        return WriteRequest(
            action="mr.comment",
            method="POST",
            path=f"{targets.p_mr(project, iid)}/discussions/{discussion_id}/notes",
            summary=f"Reply in thread {discussion_id[:12]} on !{iid} in {project}",
            project=project,
            json={"body": body},
            target=_target("merge_request", iid, web_url, discussion_id=discussion_id),
            result_kind="note",
        )
    pos = args.get("position")
    if pos:
        if not isinstance(pos, dict):
            raise WriteError(
                "position must be an object: new_path plus new_line (added or unchanged line) or old_line (removed line)",
                field="position",
            )
        new_path, old_path = pos.get("new_path"), pos.get("old_path")
        if not (new_path or old_path):
            raise WriteError("position needs new_path (or old_path for a removed file)", field="position")
        try:
            new_line = int(pos["new_line"]) if pos.get("new_line") not in (None, "") else None
            old_line = int(pos["old_line"]) if pos.get("old_line") not in (None, "") else None
        except (TypeError, ValueError):
            raise WriteError("position.new_line and position.old_line must be integers", field="position") from None
        if new_line is None and old_line is None:
            raise WriteError(
                "position needs a line: new_line for an added or unchanged line, old_line for a removed line "
                "(an unchanged line may give both)",
                field="position",
            )
        mr = client.get(targets.p_mr(project, iid))
        refs = mr.get("diff_refs") if isinstance(mr, dict) else None
        if not isinstance(refs, dict) or not refs.get("head_sha"):
            raise WriteError(f"merge request !{iid} has no diff_refs; is it empty?", field="position")
        position = {
            "base_sha": refs.get("base_sha"),
            "start_sha": refs.get("start_sha"),
            "head_sha": refs.get("head_sha"),
            "position_type": "text",
            "new_path": new_path or old_path,
            "old_path": old_path or new_path,
        }
        position.update(
            _locate_position(client, project, iid, position["new_path"], position["old_path"], new_line, old_line)
        )
        where = f"{position['new_path']}:" + (
            f"{position['new_line']}" if position.get("new_line") is not None else f"old {position.get('old_line')}"
        )
        return WriteRequest(
            action="mr.comment",
            method="POST",
            path=f"{targets.p_mr(project, iid)}/discussions",
            summary=f"Diff comment on !{iid} in {project} at {where}",
            project=project,
            json={"body": body, "position": position},
            target=_target("merge_request", iid, web_url or mr.get("web_url"), sha=refs.get("head_sha")),
            preconditions=[{"kind": "mr_head", "project": project, "iid": iid, "sha": refs.get("head_sha")}],
            result_kind="discussion",
        )
    payload: Dict[str, Any] = {"body": body}
    _set_flag(payload, args, "internal")
    return WriteRequest(
        action="mr.comment",
        method="POST",
        path=f"{targets.p_mr(project, iid)}/notes",
        summary=f"Comment on !{iid} in {project}",
        project=project,
        json=payload,
        target=_target("merge_request", iid, web_url),
        result_kind="note",
    )


def _locate_position(
    client: GitLabClient,
    project: str,
    iid: int,
    new_path: str,
    old_path: str,
    new_line: Optional[int],
    old_line: Optional[int],
) -> Dict[str, Any]:
    """GitLab needs ``new_line`` alone for an added line, ``old_line`` alone for a removed line, and
    BOTH for an unchanged line. Find the line in the merge request diff and fill in whichever side
    the model left out; refuse lines that are not in the diff (GitLab would answer 400)."""
    items, _total, _more = client.paginate(f"{targets.p_mr(project, iid)}/diffs", max_results=1000)
    item = next(
        (d for d in items if isinstance(d, dict) and (d.get("new_path") == new_path or d.get("old_path") == old_path)),
        None,
    )
    if item is None:
        raise WriteError(
            f"{new_path} is not changed in !{iid}; diff comments go on files in the merge request diff",
            field="position",
        )
    # GitLab wants the diff's own paths: for a renamed file old_path is the previous name.
    paths = {"new_path": item.get("new_path") or new_path, "old_path": item.get("old_path") or old_path}
    text = item.get("diff") or ""
    if not text:  # too large or collapsed: nothing to check against, send what the model gave
        return {**paths, **{k: v for k, v in (("new_line", new_line), ("old_line", old_line)) if v is not None}}
    located = render.locate_diff_line(text, new_line=new_line, old_line=old_line)
    if located is None:
        where = f"new line {new_line}" if new_line is not None else f"old line {old_line}"
        raise WriteError(
            f"{where} of {new_path} is not in the diff of !{iid}: diff comments go on added, removed or unchanged "
            "lines inside a hunk. Read action=diffs and use the hunk line numbers (new_line for added and unchanged "
            "lines, old_line for removed lines).",
            field="position",
        )
    if new_line is not None and old_line is not None and located["old_line"] != old_line:
        raise WriteError(
            f"new line {new_line} of {new_path} is old line {located['old_line']}, not {old_line}", field="position"
        )
    lines = {k: v for k, v in (("new_line", located["new_line"]), ("old_line", located["old_line"])) if v is not None}
    return {**paths, **lines}


def _mr_approve(client: GitLabClient, project: str, args: Dict[str, Any]) -> WriteRequest:
    iid = _iid(args)
    sha = _expand_sha(client, project, iid, _sha(args, "sha"))
    pre = [{"kind": "mr_head", "project": project, "iid": iid, "sha": sha}] if sha else []
    return WriteRequest(
        action="mr.approve",
        method="POST",
        path=f"{targets.p_mr(project, iid)}/approve",
        summary=f"Approve !{iid} in {project}" + (f" at {sha[:12]}" if sha else ""),
        project=project,
        json={"sha": sha} if sha else None,
        target=_target("merge_request", iid, _mr_web_url(client, project, iid), sha=sha),
        preconditions=pre,
        result_kind="approval",
        notes=[]
        if sha
        else [
            "No sha given: the approval is not bound to a specific head. Prefer passing sha from gitlab_merge_requests get."
        ],
    )


def _mr_unapprove(client: GitLabClient, project: str, args: Dict[str, Any]) -> WriteRequest:
    iid = _iid(args)
    return WriteRequest(
        action="mr.unapprove",
        method="POST",
        path=f"{targets.p_mr(project, iid)}/unapprove",
        summary=f"Remove approval from !{iid} in {project}",
        project=project,
        target=_target("merge_request", iid, _mr_web_url(client, project, iid)),
        result_kind="approval",
    )


def _mr_merge(client: GitLabClient, project: str, args: Dict[str, Any]) -> WriteRequest:
    iid = _iid(args)
    sha = _sha(args, "sha")
    if not sha:
        raise WriteError(
            "sha is required for merge: pass the head sha from gitlab_merge_requests get, so the merge is bound to the revision that was reviewed",
            field="sha",
        )
    sha = _expand_sha(client, project, iid, sha) or sha
    payload: Dict[str, Any] = {"sha": sha}
    for key in ("squash", "should_remove_source_branch"):
        _set_flag(payload, args, key)
    for key in ("merge_commit_message", "squash_commit_message"):
        value = _text(args, key)
        if value is not None:
            payload[key] = value
    when_pipeline = _flag(args, "merge_when_pipeline_succeeds")
    if when_pipeline:
        payload["merge_when_pipeline_succeeds"] = True
        payload["auto_merge"] = True  # GitLab 17.11+ name for the same option; extra keys are ignored by older releases
    return WriteRequest(
        action="mr.merge",
        method="PUT",
        path=f"{targets.p_mr(project, iid)}/merge",
        summary=f"Merge !{iid} in {project} at {sha[:12]}" + (" when the pipeline succeeds" if when_pipeline else ""),
        project=project,
        json=payload,
        target=_target("merge_request", iid, _mr_web_url(client, project, iid), sha=sha),
        preconditions=[{"kind": "mr_head", "project": project, "iid": iid, "sha": sha}],
        requires=["allow_merge"],
        result_kind="merge_request",
        irreversible=True,
    )


def _mr_rebase(client: GitLabClient, project: str, args: Dict[str, Any]) -> WriteRequest:
    iid = _iid(args)
    params = {"skip_ci": True} if _flag(args, "skip_ci", False) else None
    return WriteRequest(
        action="mr.rebase",
        method="PUT",
        path=f"{targets.p_mr(project, iid)}/rebase",
        summary=f"Rebase !{iid} in {project} onto its target branch",
        project=project,
        params=params,
        target=_target("merge_request", iid, _mr_web_url(client, project, iid)),
        result_kind="rebase",
    )


def _mr_resolve(client: GitLabClient, project: str, args: Dict[str, Any]) -> WriteRequest:
    iid = _iid(args)
    discussion_id = _text(args, "discussion_id", required=True)
    resolved = _flag(args, "resolved", True)
    return WriteRequest(
        action="mr.resolve",
        method="PUT",
        path=f"{targets.p_mr(project, iid)}/discussions/{discussion_id}",
        summary=f"{'Resolve' if resolved else 'Unresolve'} thread {str(discussion_id)[:12]} on !{iid} in {project}",
        project=project,
        json={"resolved": bool(resolved)},
        target=_target("merge_request", iid, _mr_web_url(client, project, iid), discussion_id=discussion_id),
        result_kind="discussion",
    )


# -- pipelines ------------------------------------------------------------------------------------


def pipeline(client: GitLabClient, args: Dict[str, Any], settings: Settings) -> WriteRequest:
    action = _action(args, PIPELINE_ACTIONS)
    project = _project(client, args)
    if action == "run":
        ref = _text(args, "ref", required=True, limit=255)
        payload: Dict[str, Any] = {"ref": ref}
        variables = args.get("variables")
        if variables:
            if not isinstance(variables, dict):
                raise WriteError("variables must be an object of KEY: value pairs", field="variables")
            rows = []
            for key, value in variables.items():
                if not _VAR_KEY_RE.match(str(key)):
                    raise WriteError(f"variables: invalid key {key!r}", field="variables")
                rows.append({"key": str(key), "value": "" if value is None else str(value)})
            payload["variables"] = rows
        inputs = args.get("inputs")
        if inputs:
            if not isinstance(inputs, dict):
                raise WriteError("inputs must be an object of name: value pairs", field="inputs")
            payload["inputs"] = dict(inputs)
        extras = [
            f"{len(payload['variables'])} variable(s)" if variables else "",
            f"{len(inputs)} input(s)" if inputs else "",
        ]
        return WriteRequest(
            action="pipeline.run",
            method="POST",
            path=f"{targets.p_project(project)}/pipeline",
            summary=f"Run pipeline on {ref} in {project}"
            + (" with " + ", ".join(e for e in extras if e) if any(extras) else ""),
            project=project,
            json=payload,
            target=_target("pipeline", ref=ref),
            result_kind="pipeline",
        )
    job_id = targets.optional_int(args.get("job_id"), "job_id")
    pipeline_id = targets.optional_int(args.get("pipeline_id"), "pipeline_id")
    if action == "play":
        if not job_id:
            raise WriteError("play needs job_id (a manual job)", field="job_id")
        return WriteRequest(
            action="pipeline.play",
            method="POST",
            path=f"{targets.p_job(project, job_id)}/play",
            summary=f"Play manual job {job_id} in {project}",
            project=project,
            target=_target("job", id=job_id),
            result_kind="job",
        )
    if bool(job_id) == bool(pipeline_id):
        raise WriteError(f"{action} needs exactly one of pipeline_id or job_id", field="pipeline_id")
    if job_id:
        return WriteRequest(
            action=f"pipeline.{action}",
            method="POST",
            path=f"{targets.p_job(project, job_id)}/{action}",
            summary=f"{action.capitalize()} job {job_id} in {project}",
            project=project,
            target=_target("job", id=job_id),
            result_kind="job",
        )
    return WriteRequest(
        action=f"pipeline.{action}",
        method="POST",
        path=f"{targets.p_pipeline(project, pipeline_id)}/{action}",
        summary=f"{action.capitalize()} pipeline {pipeline_id} in {project}",
        project=project,
        target=_target("pipeline", id=pipeline_id),
        result_kind="pipeline",
    )


# -- commits --------------------------------------------------------------------------------------


def _file_path(value: Any, field: str) -> str:
    text = str(value or "").strip().strip("/")
    if not text or any(seg in {"..", ""} for seg in text.split("/")) or "\\" in text:
        raise WriteError(f"{field}: invalid file path {value!r}", field=field)
    return text


def _branch_lookup(client: GitLabClient, project: str, branch: str) -> Optional[Dict[str, Any]]:
    try:
        return client.get(targets.p_branch(project, branch))
    except GitLabError as exc:
        if exc.status != 404:
            raise
        return None


def _branch_create(
    client: GitLabClient, project: str, branch: str, start_branch: Optional[str], expected: Optional[str]
) -> WriteRequest:
    """``gitlab_commit`` without ``actions``: only create *branch* from *start_branch*."""
    if not start_branch:
        raise WriteError(
            "actions is empty: pass file actions to commit, or start_branch to only create the branch", field="actions"
        )
    if _branch_lookup(client, project, branch) is not None:
        raise WriteError(f"branch {branch!r} already exists in {project}", field="branch")
    preconditions = (
        [{"kind": "branch_head", "project": project, "branch": start_branch, "sha": expected}] if expected else []
    )
    notes = (
        [] if expected else ["No expected_head_sha given: the branch starts at whatever start_branch points to now."]
    )
    return WriteRequest(
        action="branch.create",
        method="POST",
        path=f"{targets.p_project(project)}/repository/branches",
        summary=f"Create branch {branch} from {start_branch} in {project}",
        project=project,
        json={"branch": branch, "ref": start_branch},
        target=_target("branch", branch=branch, ref=start_branch),
        preconditions=preconditions,
        result_kind="branch",
        notes=notes,
    )


def commit(client: GitLabClient, args: Dict[str, Any], settings: Settings) -> WriteRequest:
    project = _project(client, args)
    branch = _text(args, "branch", required=True, limit=255)
    raw_actions = args.get("actions")
    if raw_actions in (None, "", []):
        return _branch_create(
            client, project, branch, _text(args, "start_branch", limit=255), _sha(args, "expected_head_sha")
        )
    message = _text(args, "commit_message", required=True)
    if not isinstance(raw_actions, list):
        raise WriteError(
            "actions must be a list of file operations (omit it to only create the branch)", field="actions"
        )
    if len(raw_actions) > MAX_COMMIT_ACTIONS:
        raise WriteError(f"at most {MAX_COMMIT_ACTIONS} actions per commit", field="actions")
    actions: List[Dict[str, Any]] = []
    total = 0
    for index, item in enumerate(raw_actions):
        if not isinstance(item, dict):
            raise WriteError(f"actions[{index}] must be an object", field="actions")
        kind = str(item.get("action") or "").strip().lower()
        if kind not in COMMIT_ACTIONS:
            raise WriteError(f"actions[{index}].action must be one of {', '.join(COMMIT_ACTIONS)}", field="actions")
        entry: Dict[str, Any] = {
            "action": kind,
            "file_path": _file_path(item.get("file_path"), f"actions[{index}].file_path"),
        }
        if kind in {"create", "update"} or (kind == "move" and item.get("content") is not None):
            content = item.get("content")
            if content is None and kind != "move":
                raise WriteError(f"actions[{index}].content is required for {kind}", field="actions")
            if content is not None:
                entry["content"] = content if isinstance(content, str) else str(content)
                total += len(entry["content"].encode("utf-8"))
        if kind == "move":
            entry["previous_path"] = _file_path(item.get("previous_path"), f"actions[{index}].previous_path")
        if kind == "chmod":
            if not isinstance(item.get("execute_filemode"), bool):
                raise WriteError(
                    f"actions[{index}].execute_filemode (true/false) is required for chmod", field="actions"
                )
            entry["execute_filemode"] = item["execute_filemode"]
        encoding = str(item.get("encoding") or "text").lower()
        if encoding not in {"text", "base64"}:
            raise WriteError(f"actions[{index}].encoding must be text or base64", field="actions")
        if encoding == "base64":
            entry["encoding"] = "base64"
        if item.get("last_commit_id"):
            entry["last_commit_id"] = str(item["last_commit_id"])
        actions.append(entry)
    if total > MAX_COMMIT_BYTES:
        raise WriteError(f"commit content exceeds {MAX_COMMIT_BYTES} bytes", field="actions")
    payload: Dict[str, Any] = {"branch": branch, "commit_message": message, "actions": actions}
    for key in ("author_name", "author_email"):
        value = _text(args, key, limit=255)
        if value:
            payload[key] = value
    start_branch = _text(args, "start_branch", limit=255)
    expected = _sha(args, "expected_head_sha")
    notes: List[str] = []
    existing = _branch_lookup(client, project, branch)
    if existing is None:
        if not start_branch:
            raise WriteError(
                f"branch {branch!r} does not exist in {project}; pass start_branch to create it", field="start_branch"
            )
        payload["start_branch"] = start_branch
        checked = start_branch
        notes.append(f"branch {branch} will be created from {start_branch}")
    else:
        checked = branch
        if start_branch:
            notes.append(f"branch {branch} already exists; start_branch ignored")
    preconditions = (
        [{"kind": "branch_head", "project": project, "branch": checked, "sha": expected}] if expected else []
    )
    if not expected:
        notes.append("No expected_head_sha given: the commit is not bound to the branch head that was read.")
    kinds = ", ".join(f"{a['action']} {a['file_path']}" for a in actions[:5]) + (" …" if len(actions) > 5 else "")
    return WriteRequest(
        action="commit.create",
        method="POST",
        path=f"{targets.p_project(project)}/repository/commits",
        summary=f"Commit to {branch} in {project}: {message.splitlines()[0][:80]} ({kinds})",
        project=project,
        json=payload,
        target=_target("commit", branch=branch),
        preconditions=preconditions,
        result_kind="commit",
        notes=notes,
    )


# -- raw ------------------------------------------------------------------------------------------


def raw(client: GitLabClient, args: Dict[str, Any], settings: Settings) -> WriteRequest:
    method = str(args.get("method") or "GET").strip().upper()
    if method not in RAW_METHODS:
        raise WriteError(f"method must be one of {', '.join(RAW_METHODS)} for a write", field="method")
    try:
        path = targets.validate_path_safe(args.get("path"))
    except GitLabError as exc:
        raise WriteError(str(exc), field="path") from None
    params = args.get("params")
    if params is not None and not isinstance(params, dict):
        raise WriteError("params must be an object", field="params")
    body = args.get("body")
    if body is not None and not isinstance(body, (dict, list)):
        raise WriteError("body must be a JSON object or array", field="body")
    requires = ["allow_raw_writes"] + (["allow_raw_delete"] if method == "DELETE" else [])
    return WriteRequest(
        action=f"api.{method}",
        method=method,
        path=path,
        summary=f"{method} /api/v4/{path}",
        project=targets.project_from_path(path),
        params=params or None,
        json=body,
        target={"kind": "raw", "path": path},
        requires=requires,
        result_kind="raw",
        irreversible=method == "DELETE",
    )
