"""Tool schemas — the contract the model sees. Keep descriptions precise: they are the only
guidance the model gets at call time (the bundled SKILL.md carries the longer workflow)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

PROJECT = {
    "type": "string",
    "description": "Project as a numeric id, a 'group/project' path, or a GitLab web URL of the project "
    "or of one of its issues, merge requests, pipelines or commits.",
}
IID = {
    "type": "integer",
    "minimum": 1,
    "description": "The number shown in GitLab (#12 for issues, !12 for merge requests).",
}


def _limit(defaults: str) -> Dict[str, Any]:
    return {
        "type": "integer",
        "minimum": 1,
        "description": f"Maximum items to return ({defaults}; the operator caps it).",
    }


LABELS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Label names (replaces the full label set on update; use add_labels/remove_labels to change some).",
}
USERNAMES = {
    "type": "array",
    "items": {"type": "string"},
    "description": "GitLab usernames. An empty list clears the field.",
}
DRY_RUN = {
    "type": "boolean",
    "description": "Validate and return exactly what would be sent (method, path, payload, preconditions) without sending it.",
}
DATE = {"type": "string", "description": "ISO 8601 date-time, e.g. 2026-09-01T00:00:00Z."}


def _schema(
    name: str, description: str, properties: Dict[str, Any], required: Optional[List[str]] = None
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


GITLAB_SEARCH = _schema(
    "gitlab_search",
    "Find things in GitLab by free text. Read-only. scope=projects finds repositories by name or path "
    "(the usual first call when the user names a repo); other scopes search issues, merge requests, "
    "milestones, users, or code (blobs), commits, wiki pages and comments where the instance has advanced "
    "search. Restrict to one project or group when known. For filtered lists (by state, label, author) "
    "use gitlab_issues / gitlab_merge_requests instead.",
    {
        "scope": {
            "type": "string",
            "enum": [
                "projects",
                "issues",
                "merge_requests",
                "milestones",
                "users",
                "blobs",
                "commits",
                "wiki_blobs",
                "notes",
            ],
            "description": "What to search (default projects).",
        },
        "query": {"type": "string", "description": "Search text."},
        "project": PROJECT,
        "group": {
            "type": "string",
            "description": "Group path to search within, e.g. 'platform' or 'platform/backend'.",
        },
        "ref": {"type": "string", "description": "Branch or tag for blobs/commits within a project."},
        "state": {"type": "string", "enum": ["opened", "closed"], "description": "For issues and merge_requests."},
        "confidential": {"type": "boolean", "description": "issues: only confidential (true) or only public (false)."},
        "membership": {
            "type": "boolean",
            "description": "projects only: limit to projects the token's user is a member of (default true).",
        },
        "archived": {
            "type": "boolean",
            "description": "projects only: include only archived (true) or only active (false) projects.",
        },
        "order_by": {"type": "string"},
        "sort": {"type": "string", "enum": ["asc", "desc"]},
        "limit": _limit("default 20"),
    },
    required=["query"],
)

GITLAB_REPO = _schema(
    "gitlab_repo",
    "Read a repository. Read-only. action=project returns the project's metadata (default branch, "
    "visibility, merge settings); tree lists a directory; file returns decoded text content "
    "(optionally a line range; binaries report size only); commits lists history (filter by ref, path, "
    "author, since/until); commit shows one commit with its diff; compare diffs two refs; branches and "
    "tags list refs (or one branch by name). Diffs and files are capped by the operator; ask for paths "
    "or line ranges to focus.",
    {
        "project": PROJECT,
        "action": {
            "type": "string",
            "enum": ["project", "tree", "file", "commits", "commit", "compare", "branches", "tags"],
        },
        "ref": {"type": "string", "description": "Branch, tag or sha (default: the default branch)."},
        "path": {"type": "string", "description": "Directory (tree/commits) or file path (file)."},
        "recursive": {"type": "boolean", "description": "tree: descend into subdirectories."},
        "start_line": {"type": "integer", "minimum": 1, "description": "file: first line to return (1-based)."},
        "end_line": {"type": "integer", "minimum": 1, "description": "file: last line to return (inclusive)."},
        "sha": {"type": "string", "description": "commit: the commit sha."},
        "from": {"type": "string", "description": "compare: base ref."},
        "to": {"type": "string", "description": "compare: head ref."},
        "straight": {"type": "boolean", "description": "compare: direct from..to comparison instead of merge-base."},
        "since": DATE,
        "until": DATE,
        "author": {"type": "string", "description": "commits: author name or email filter."},
        "all": {"type": "boolean", "description": "commits: all branches, not just ref."},
        "first_parent": {"type": "boolean", "description": "commits: follow only the first parent of merges."},
        "name": {"type": "string", "description": "branches: return this one branch."},
        "search": {"type": "string", "description": "branches/tags: name filter."},
        "include_diff": {"type": "boolean", "description": "commit: include the diff (default true)."},
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "commit/compare: only diff these paths or globs.",
        },
        "max_bytes": {
            "type": "integer",
            "minimum": 1000,
            "description": "commit/compare: cap on diff text (never above the operator's max_diff_bytes).",
        },
        "limit": _limit("default 100 for tree, 50 for branches and tags, 20 for commits"),
    },
    required=["project", "action"],
)

GITLAB_ISSUES = _schema(
    "gitlab_issues",
    "List or read issues. Read-only. action=list filters issues in a project (or across all projects the "
    "token can see when project is omitted) by state, labels, milestone, assignee, author, text and dates. "
    "action=get returns one issue with its description, discussion threads (system notes excluded) and "
    "related merge requests. To change an issue use gitlab_issue_write.",
    {
        "action": {"type": "string", "enum": ["list", "get"], "description": "Default list."},
        "project": PROJECT,
        "iid": IID,
        "state": {"type": "string", "enum": ["opened", "closed", "all"]},
        "labels": {
            "type": "array",
            "items": {"type": "string"},
            "description": "All of these labels (comma logic is AND).",
        },
        "milestone": {"type": "string", "description": "Milestone title, or 'None' / 'Any'."},
        "assignee": {"type": "string", "description": "Assignee username, or 'None' / 'Any'."},
        "author": {"type": "string", "description": "Author username."},
        "search": {"type": "string", "description": "Text in title or description."},
        "iids": {"type": "array", "items": {"type": "integer"}, "description": "Only these iids."},
        "issue_type": {"type": "string", "enum": ["issue", "incident", "test_case", "task"]},
        "confidential": {"type": "boolean"},
        "scope": {"type": "string", "enum": ["created_by_me", "assigned_to_me", "all"], "description": "Default all."},
        "order_by": {
            "type": "string",
            "enum": [
                "created_at",
                "updated_at",
                "priority",
                "due_date",
                "label_priority",
                "milestone_due",
                "popularity",
                "weight",
                "title",
            ],
        },
        "sort": {"type": "string", "enum": ["asc", "desc"]},
        "updated_after": DATE,
        "updated_before": DATE,
        "created_after": DATE,
        "created_before": DATE,
        "include_discussions": {"type": "boolean", "description": "get: include discussion threads (default true)."},
        "include_system": {
            "type": "boolean",
            "description": "get: include system notes such as label changes (default false).",
        },
        "limit": _limit("default 20 for list, 50 discussion threads for get"),
    },
)

GITLAB_MERGE_REQUESTS = _schema(
    "gitlab_merge_requests",
    "List or review merge requests. Read-only. action=list filters MRs in a project (or across projects when "
    "project is omitted). action=get returns one MR with description, merge status, head pipeline, approvals "
    "and the sha to pass when approving or merging. action=diffs returns the unified diff (use paths to focus; "
    "the operator caps size). action=discussions returns review threads with diff positions (use "
    "discussion_id to reply, only_unresolved to see what blocks). action=commits and action=pipelines list "
    "those. To comment, approve, merge or edit use gitlab_mr_write.",
    {
        "action": {
            "type": "string",
            "enum": ["list", "get", "diffs", "discussions", "commits", "pipelines"],
            "description": "Default list.",
        },
        "project": PROJECT,
        "iid": IID,
        "state": {"type": "string", "enum": ["opened", "closed", "merged", "locked", "all"]},
        "scope": {"type": "string", "enum": ["created_by_me", "assigned_to_me", "all"], "description": "Default all."},
        "labels": {"type": "array", "items": {"type": "string"}},
        "milestone": {"type": "string"},
        "author": {"type": "string", "description": "Author username."},
        "assignee": {"type": "string", "description": "Assignee username, or 'None' / 'Any'."},
        "reviewer": {"type": "string", "description": "Reviewer username."},
        "search": {"type": "string", "description": "Text in title or description."},
        "source_branch": {"type": "string"},
        "target_branch": {"type": "string"},
        "draft": {"type": "boolean", "description": "Only drafts (true) or only non-drafts (false)."},
        "iids": {"type": "array", "items": {"type": "integer"}},
        "order_by": {"type": "string", "enum": ["created_at", "updated_at", "title"]},
        "sort": {"type": "string", "enum": ["asc", "desc"]},
        "updated_after": DATE,
        "updated_before": DATE,
        "created_after": DATE,
        "created_before": DATE,
        "paths": {"type": "array", "items": {"type": "string"}, "description": "diffs: only these paths or globs."},
        "max_bytes": {
            "type": "integer",
            "minimum": 1000,
            "description": "diffs: cap on diff text (never above the operator's max_diff_bytes).",
        },
        "include_system": {"type": "boolean", "description": "discussions: include system notes (default false)."},
        "only_unresolved": {"type": "boolean", "description": "discussions: only threads that still block."},
        "limit": _limit("default 20 for list and pipelines, 50 for discussions and commits"),
    },
)

GITLAB_PIPELINES = _schema(
    "gitlab_pipelines",
    "Inspect CI. Read-only. action=list filters pipelines (or latest=true for the newest on a ref); action=get "
    "returns one pipeline with all its jobs, a per-stage status count and the failed jobs; action=jobs lists "
    "jobs (by pipeline_id, or project-wide) filtered by status; action=job returns one job; action=log returns "
    "the job's log with colour and section markers stripped: the last tail_lines lines, or with search, the "
    "matching lines with context (start with search='error|fail' to diagnose). Token-shaped strings are masked.",
    {
        "project": PROJECT,
        "action": {"type": "string", "enum": ["list", "get", "jobs", "job", "log"], "description": "Default list."},
        "pipeline_id": {"type": "integer", "minimum": 1},
        "job_id": {"type": "integer", "minimum": 1},
        "ref": {"type": "string", "description": "list: branch or tag."},
        "latest": {
            "type": "boolean",
            "description": "list: return only the latest pipeline for ref (default branch when ref is omitted).",
        },
        "status": {
            "type": "string",
            "enum": [
                "created",
                "waiting_for_resource",
                "preparing",
                "pending",
                "running",
                "success",
                "failed",
                "canceled",
                "skipped",
                "manual",
                "scheduled",
            ],
        },
        "sha": {"type": "string", "description": "list: pipelines for this commit."},
        "source": {
            "type": "string",
            "description": "list: push, web, trigger, schedule, api, merge_request_event, ...",
        },
        "username": {"type": "string", "description": "list: pipelines triggered by this user."},
        "scope": {
            "type": "array",
            "items": {"type": "string"},
            "description": 'jobs only: statuses to include, e.g. ["failed", "running"]. For pipelines use status.',
        },
        "include_retried": {"type": "boolean", "description": "get/jobs: include retried jobs."},
        "tail_lines": {
            "type": "integer",
            "minimum": 1,
            "description": "log: lines from the end to return (default 200).",
        },
        "search": {
            "type": "string",
            "description": "log: regex; returns matching lines with context instead of the tail.",
        },
        "context": {
            "type": "integer",
            "minimum": 0,
            "description": "log: context lines around each match (default 2).",
        },
        "max_matches": {"type": "integer", "minimum": 1, "description": "log: cap on matches (default 50)."},
        "order_by": {"type": "string", "enum": ["id", "status", "ref", "updated_at", "user_id"]},
        "sort": {"type": "string", "enum": ["asc", "desc"]},
        "updated_after": DATE,
        "updated_before": DATE,
        "limit": _limit("default 20 for list, 50 for jobs"),
    },
    required=["project"],
)

GITLAB_API = _schema(
    "gitlab_api",
    "Escape hatch for GitLab REST endpoints the other tools do not cover (releases, environments, labels, "
    "milestones, groups, ...). path is relative to /api/v4. GET is always allowed except on credential and "
    "settings surfaces (variables, tokens, hooks, keys, admin). Other methods need the operator to enable "
    "allow_raw_writes (and allow_raw_delete for DELETE) and go through the same write gate as every other "
    "write; membership, permission and settings endpoints are refused regardless. path='status' returns "
    "connectivity, the token's identity and scopes, and the plugin's write mode.",
    {
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "description": "Default GET."},
        "path": {"type": "string", "description": "e.g. 'projects/42/releases' or 'projects/group%2Fproj/labels'."},
        "params": {"type": "object", "description": "Query parameters.", "additionalProperties": True},
        "body": {"type": "object", "description": "JSON body for POST/PUT/PATCH.", "additionalProperties": True},
        "paginate": {"type": "boolean", "description": "GET: follow pagination up to limit items."},
        "limit": _limit("default 100 with paginate"),
        "dry_run": DRY_RUN,
    },
    required=["path"],
)

GITLAB_ISSUE_WRITE = _schema(
    "gitlab_issue_write",
    "Create, edit or comment on an issue. WRITES to GitLab: describe what you are about to do and get the "
    "user's confirmation first (dry_run=true shows the exact payload). action=create needs title; "
    "action=update needs iid and at least one field (state closes or reopens; add_labels/remove_labels edit "
    "labels without replacing them); action=comment needs iid and body (discussion_id replies in a thread, "
    "internal=true makes a note only members see). If the result says staged, relay the command it names to "
    "the user and stop.",
    {
        "project": PROJECT,
        "action": {"type": "string", "enum": ["create", "update", "comment"]},
        "iid": IID,
        "title": {"type": "string"},
        "description": {"type": "string", "description": "Markdown."},
        "labels": LABELS,
        "add_labels": {"type": "array", "items": {"type": "string"}},
        "remove_labels": {"type": "array", "items": {"type": "string"}},
        "assignees": USERNAMES,
        "milestone": {"type": "string", "description": "Milestone title (empty string clears it)."},
        "due_date": {"type": "string", "description": "YYYY-MM-DD."},
        "confidential": {"type": "boolean"},
        "issue_type": {"type": "string", "enum": ["issue", "incident", "test_case", "task"]},
        "state": {"type": "string", "enum": ["close", "reopen"], "description": "update: close or reopen the issue."},
        "discussion_locked": {"type": "boolean"},
        "body": {"type": "string", "description": "comment: Markdown body."},
        "discussion_id": {"type": "string", "description": "comment: reply in this thread (from gitlab_issues get)."},
        "internal": {"type": "boolean", "description": "comment: internal note, visible to members only."},
        "dry_run": DRY_RUN,
    },
    required=["project", "action"],
)

GITLAB_MR_WRITE = _schema(
    "gitlab_mr_write",
    "Act on a merge request. WRITES to GitLab: describe the action and get the user's confirmation first "
    "(dry_run=true shows the exact payload). action=create needs source_branch and title (target_branch "
    "defaults to the project default; draft=true prefixes 'Draft:'). action=update edits fields or closes/"
    "reopens. action=comment posts a note; with discussion_id it replies in a thread; with position "
    "{new_path, new_line} (new_line for an added or unchanged line, old_line for a removed line) it starts a thread on that diff line, bound to "
    "the current head. action=approve and action=merge take sha from gitlab_merge_requests get so they apply "
    "only to the reviewed revision; merge is refused unless the operator enabled allow_merge and is "
    "irreversible. action=rebase rebases onto the target; action=resolve resolves (or reopens with "
    "resolved=false) a thread. If the result says staged, relay the command it names and stop.",
    {
        "project": PROJECT,
        "action": {
            "type": "string",
            "enum": ["create", "update", "comment", "approve", "unapprove", "merge", "rebase", "resolve"],
        },
        "iid": IID,
        "source_branch": {"type": "string"},
        "target_branch": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string", "description": "Markdown."},
        "draft": {"type": "boolean", "description": "create/update: mark as draft (true) or ready (false)."},
        "labels": LABELS,
        "add_labels": {"type": "array", "items": {"type": "string"}},
        "remove_labels": {"type": "array", "items": {"type": "string"}},
        "assignees": USERNAMES,
        "reviewers": USERNAMES,
        "milestone": {"type": "string", "description": "Milestone title (empty string clears it)."},
        "remove_source_branch": {
            "type": "boolean",
            "description": "create/update: delete the source branch after merge.",
        },
        "squash": {"type": "boolean"},
        "state": {"type": "string", "enum": ["close", "reopen"], "description": "update: close or reopen."},
        "discussion_locked": {"type": "boolean"},
        "body": {"type": "string", "description": "comment: Markdown body."},
        "discussion_id": {
            "type": "string",
            "description": "comment: reply in this thread; resolve: the thread to resolve.",
        },
        "internal": {"type": "boolean", "description": "comment: internal note, visible to members only."},
        "position": {
            "type": "object",
            "description": "comment: anchor the thread to a diff line, using the numbers from action=diffs hunks. Added line: new_line. Removed line: old_line. Unchanged line: new_line (old_line is filled in for you, or give both). Lines outside the diff are rejected.",
            "properties": {
                "new_path": {"type": "string"},
                "old_path": {"type": "string"},
                "new_line": {"type": "integer", "minimum": 1},
                "old_line": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        },
        "resolved": {"type": "boolean", "description": "resolve: true to resolve (default), false to reopen."},
        "sha": {
            "type": "string",
            "description": "approve/merge: the head sha from gitlab_merge_requests get. Required for merge.",
        },
        "merge_when_pipeline_succeeds": {
            "type": "boolean",
            "description": "merge: set auto-merge instead of merging now.",
        },
        "should_remove_source_branch": {"type": "boolean", "description": "merge: delete the source branch."},
        "merge_commit_message": {"type": "string"},
        "squash_commit_message": {"type": "string"},
        "skip_ci": {"type": "boolean", "description": "rebase: do not run a pipeline for the rebase."},
        "dry_run": DRY_RUN,
    },
    required=["project", "action"],
)

GITLAB_PIPELINE_WRITE = _schema(
    "gitlab_pipeline_write",
    "Control CI. WRITES to GitLab: confirm with the user first. action=run starts a pipeline on ref with "
    "optional variables; action=retry re-runs a failed pipeline (pipeline_id) or job (job_id); action=cancel "
    "stops a pipeline or job; action=play starts a manual job (job_id). If the result says staged, relay the "
    "command it names and stop.",
    {
        "project": PROJECT,
        "action": {"type": "string", "enum": ["run", "retry", "cancel", "play"]},
        "ref": {"type": "string", "description": "run: branch or tag."},
        "variables": {
            "type": "object",
            "description": "run: CI variables as KEY: value.",
            "additionalProperties": {"type": "string"},
        },
        "inputs": {
            "type": "object",
            "description": "run: pipeline inputs (spec:inputs) as name: value, GitLab 17.7+.",
            "additionalProperties": True,
        },
        "pipeline_id": {"type": "integer", "minimum": 1},
        "job_id": {"type": "integer", "minimum": 1},
        "dry_run": DRY_RUN,
    },
    required=["project", "action"],
)

GITLAB_COMMIT = _schema(
    "gitlab_commit",
    "Commit file changes to a branch in one atomic commit. WRITES to GitLab: show the user the files and the "
    "message and get confirmation first (dry_run=true previews). branch must exist, or pass start_branch to "
    "create it from an existing branch. actions is an ordered list of create/update/delete/move/chmod "
    "operations with full file contents; omit actions (with start_branch) to only create the branch. Pass "
    "expected_head_sha (the branch head you read) so the commit is refused if someone pushed in between. "
    "Never commit secrets. Open a merge request afterwards with gitlab_mr_write rather than committing to "
    "protected branches.",
    {
        "project": PROJECT,
        "branch": {"type": "string", "description": "Target branch."},
        "start_branch": {"type": "string", "description": "Create branch from this branch when it does not exist yet."},
        "commit_message": {"type": "string", "description": "Required when actions is given."},
        "actions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "update", "delete", "move", "chmod"]},
                    "file_path": {"type": "string"},
                    "content": {
                        "type": "string",
                        "description": "Full new content for create/update (and optionally move).",
                    },
                    "previous_path": {"type": "string", "description": "move: the original path."},
                    "encoding": {"type": "string", "enum": ["text", "base64"], "description": "Default text."},
                    "execute_filemode": {"type": "boolean", "description": "chmod: set or clear the executable bit."},
                    "last_commit_id": {
                        "type": "string",
                        "description": "update/delete/move: last known commit of the file, for an extra per-file check.",
                    },
                },
                "required": ["action", "file_path"],
                "additionalProperties": False,
            },
        },
        "author_name": {"type": "string"},
        "author_email": {"type": "string"},
        "expected_head_sha": {
            "type": "string",
            "description": "Head sha of branch (or start_branch) as last read; the commit is refused if it moved.",
        },
        "dry_run": DRY_RUN,
    },
    required=["project", "branch"],
)

READ = (GITLAB_SEARCH, GITLAB_REPO, GITLAB_ISSUES, GITLAB_MERGE_REQUESTS, GITLAB_PIPELINES, GITLAB_API)
WRITE = (GITLAB_ISSUE_WRITE, GITLAB_MR_WRITE, GITLAB_PIPELINE_WRITE, GITLAB_COMMIT)
ALL = READ + WRITE
