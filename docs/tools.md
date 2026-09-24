# Tool reference

`project` everywhere: a numeric id, a `group/project` path (subgroups fine), or a GitLab web URL of
the project or of one of its issues, merge requests, pipelines, jobs or commits. `iid` is the number
GitLab shows (`#12`, `!12`). Every list takes `limit`, capped by `max_results`; the default is 20
for searches, commit history, issues, merge requests and pipelines, 50 for branches, tags,
discussion threads, MR commits and jobs, and 100 for `tree` and `gitlab_api` with `paginate`.
Lists report `count` (when GitLab provides it), `returned` and `truncated`. Every result that can
carry user-written text or code reports `redacted`, the number of token-shaped strings masked.

Required token scope: `read_api` for the read tools and `gitlab_api` GET; `api` for the write tools
and non-GET `gitlab_api`. The role GitLab requires is noted where it matters.

## gitlab_search

| Parameter | Notes |
|---|---|
| `scope` | `projects` (default), `issues`, `merge_requests`, `milestones`, `users`, `blobs`, `commits`, `wiki_blobs`, `notes` |
| `query` | required |
| `project`, `group` | restrict the search |
| `ref` | blobs / commits within a project |
| `state` | `opened` / `closed` for issues and MRs |
| `membership` | projects: only projects the token's user belongs to (default true) |
| `archived` | projects: only archived (true) / only active (false) |
| `order_by`, `sort` | passed through |

`projects` uses `GET /projects?search=&search_namespaces=true` (or `GET /groups/:id/projects`);
other scopes use `GET /search`, `/groups/:id/search` or `/projects/:id/search`. `blobs`, `commits`,
`notes` and `wiki_blobs` need advanced search (blobs: or exact code search); the tool reports when
the instance refuses the scope. Code snippets are redacted for token formats.

## gitlab_repo

| `action` | Parameters | Returns |
|---|---|---|
| `project` | – | `metadata`: id, path, default branch, visibility, merge settings, namespace |
| `tree` | `path`, `ref`, `recursive` | entries (`path`, `type`, `mode`) |
| `file` | `path` (required), `ref` (default `HEAD`), `start_line`, `end_line` | decoded `content` (or `binary: true` with size), `total_lines`, `lines`, `commit_id`, `last_commit_id`, `truncated`, `redacted`. The range is taken from the whole file and then capped, so slices past `max_file_bytes` work; `start_line` past the end or `end_line` before `start_line` is an error |
| `commits` | `ref`, `path`, `since`, `until`, `author`, `all`, `first_parent` | commits (no `count`: GitLab omits `x-total` here) |
| `commit` | `sha` (required), `include_diff` (default true), `paths`, `max_bytes` | `commit` with stats, `diff` text, `diff_summary` |
| `compare` | `from`, `to` (required), `straight`, `paths`, `max_bytes` | `commits`, `diff`, `diff_summary`, `compare_timeout` |

Every diff-returning action (`commit`, `compare`, and `gitlab_merge_requests` `diffs`) has the same
shape: `diff` is the text and `diff_summary` holds `files` (path, status, additions, deletions),
`total_files`, `shown_files`, `truncated`, `omitted`, `redacted` and `bytes`.
| `branches` | `search`, or `name` for one branch | branches with head commit; `branch` for one |
| `tags` | `search`, `order_by`, `sort` | tags |

Diffs are unified with `diff --git` headers, capped at `max_bytes` (never above `max_diff_bytes`).
A file that does not fit is skipped and later, smaller files still get their turn; `omitted` names
the files left out and `diff_summary.files` stays complete. Merge requests with more than 1,000
changed files report `files_truncated`.

## gitlab_issues

| `action` | Parameters |
|---|---|
| `list` (default) | `project` (omit for all projects the token sees), `state`, `labels` (all must match), `milestone`, `assignee` (username, or `None` / `Any`), `author`, `search`, `iids`, `issue_type`, `confidential`, `scope`, `order_by`, `sort`, `updated_after`, `updated_before`, `created_after`, `created_before` |
| `get` | `project`, `iid` (required), `include_discussions` (default true), `include_system` (default false), `limit` for threads. Returns `issue`, `discussions`, `related_merge_requests` and `linked_issues` (each with its `link_type`) |

`get` returns the issue with its description, `discussions` (threads with notes; system notes such
as label changes excluded unless asked, every page scanned), `discussions_total`, and
`related_merge_requests`.

## gitlab_merge_requests

| `action` | Parameters | Returns |
|---|---|---|
| `list` (default) | `project` (optional), `state`, `scope`, `labels`, `milestone`, `author`, `assignee` (username, `None`, `Any`), `reviewer`, `search`, `source_branch`, `target_branch`, `draft`, `iids`, `order_by`, `sort`, date filters | merge requests |
| `get` | `project`, `iid` | `merge_request` (description, `sha`, `detailed_merge_status`, `has_conflicts`, `head_pipeline`, `diff_refs`, `diverged_commits_count`, `rebase_in_progress`), `approvals`, `closes_issues` |
| `diffs` | `project`, `iid`, `paths`, `max_bytes` | `diff` text and `diff_summary` (see gitlab_repo) |
| `discussions` | `project`, `iid`, `include_system`, `only_unresolved`, `limit` | threads with `id`, `resolvable`, `resolved`, notes with `position` (`new_path`, `old_path`, `new_line`, `old_line`). Every page is scanned (up to 2,000 discussions) so `unresolved` counts all open threads, not one page; `matched` is the number of threads that passed the filters, `returned` how many were kept under `limit`, `scanned_all` whether the scan reached the last page |
| `commits` | `project`, `iid` | commits |
| `pipelines` | `project`, `iid` | pipelines |

## gitlab_pipelines

| `action` | Parameters | Returns |
|---|---|---|
| `list` (default) | `project`, `status`, `ref`, `sha`, `source`, `username`, `name`, `order_by`, `sort`, `updated_after`, `updated_before`; or `latest: true` with optional `ref` | pipelines, or `pipeline` |
| `get` | `project`, `pipeline_id`, `include_retried` | `pipeline`, `jobs`, `failed_jobs` (failed and not `allow_failure`), `stages` (status counts per stage), `bridges` (trigger jobs with their `downstream_pipeline`) |
| `jobs` | `project`, `pipeline_id` (optional: project-wide when omitted), `scope` (job statuses; ignored by `list`, which takes `status`), `include_retried` | jobs |
| `job` | `project`, `job_id` | `job` |
| `log` | `project`, `job_id`, `tail_lines` (default 200, capped by `max_log_lines`), or `search` (regex, at most 200 characters, no repeated groups containing quantifiers or alternations, no backreferences, at most two unbounded quantifiers) with `context` (default 2) and `max_matches` (default 50) | `job`, `log`, `total_lines`, `returned_lines` / `matches`, `truncated`, `redacted`. A search looks at the first 500 characters of each line and stops after 2 seconds, reported as `truncated` with a `note` |

Logs have ANSI colour and GitLab section markers removed and carriage-return progress lines
collapsed. With `search`, matching lines are prefixed `>` and shown with their line numbers.

## gitlab_api

| Parameter | Notes |
|---|---|
| `method` | `GET` (default), `POST`, `PUT`, `PATCH`, `DELETE` |
| `path` | relative to `/api/v4`, e.g. `projects/42/releases`; `status` is a virtual path |
| `params` | query parameters |
| `body` | JSON body for POST/PUT/PATCH |
| `paginate`, `limit` | GET: follow `x-next-page` up to `limit` items |
| `dry_run` | non-GET: preview |

`path: status` returns the GitLab version, the authenticated user, the token's name, scopes and
expiry (when the instance supports `personal_access_tokens/self`), the plugin version and public
settings, and warnings (read-only token with writes enabled, expiring or revoked token, admin token).

GET refuses the sensitive surfaces listed in [architecture.md](architecture.md); paths are matched
after percent-decoding and lower-casing and without a format suffix, so encoded, upper-case or
`.json` spellings are refused too. Non-GET requires
`allow_raw_writes` (`allow_raw_delete` too for DELETE), passes the write gate (mode, `write_projects`
when the path is under `projects/:id/`), and refuses administrative surfaces. Non-GET calls are
audited as `api.<METHOD>` and their results are returned as GitLab sent them (lists cut at 50).

Raw results are secret-redacted like every other read when `redact_secrets` is on (`redacted` counts the
masks). A GET result larger than `max_file_bytes` is returned as clipped text with `truncated: true`;
narrow it with `params`, page with `paginate` and `limit`, or use a typed tool. A path is rejected when
any percent-decoded segment is `..` or the decoded path holds a `?` or `#` (query strings belong in
`params`), and the deny-lists also match the path with `..` resolved.
Redirects are never followed: a 3xx from GitLab is reported as an error so the token is not sent to
another host; set `GITLAB_URL` to the address GitLab redirects to. `sudo`, `private_token`,
`access_token`, `oauth_token` and `job_token` are refused in `params` and `body` for every method: a
call always runs as the configured token and user. Writes a typed tool guards are refused through the
escape hatch: merging or approving a merge request and creating a commit (through `repository/commits`
or a single file under `repository/files/:path`) must go through `gitlab_mr_write` / `gitlab_commit`, so
`allow_raw_writes` cannot bypass `allow_merge` or the head-sha binding. Revoking or rotating the token in use (`personal_access_tokens/self`) is refused too.

## gitlab_issue_write

Needs the `api` scope and at least Reporter (create, comment) or the issue's author / Reporter (update).

| `action` | Parameters |
|---|---|
| `create` | `title` (required), `description`, `labels`, `assignees` (usernames), `milestone` (title), `due_date` (`YYYY-MM-DD`), `confidential`, `issue_type` |
| `update` | `iid`, any of `title`, `description`, `labels` (replace), `add_labels`, `remove_labels`, `assignees` (`[]` clears), `milestone` (`""` clears), `due_date`, `confidential`, `discussion_locked`, `state` (`close` / `reopen`) |
| `comment` | `iid`, `body` (required), `discussion_id` (reply in a thread), `internal` (members-only note) |

Result: `issue` summary (or `note`), a `report` line with the URL, and `request` (method, path, status).

## gitlab_mr_write

Needs `api`. Roles: Developer for create/update/comment; approval rights for approve; Maintainer (or
push rights on the target branch) for merge.

| `action` | Parameters | Binding |
|---|---|---|
| `create` | `source_branch` (required), `target_branch` (default: project default), `title` (required), `draft`, `description`, `labels`, `assignees`, `reviewers`, `milestone`, `remove_source_branch`, `squash` | |
| `update` | `iid`, any of `title`, `draft`, `description`, `target_branch`, `labels`/`add_labels`/`remove_labels`, `assignees`, `reviewers`, `milestone`, `squash`, `remove_source_branch`, `discussion_locked`, `state` | |
| `comment` | `iid`, `body`, plus `discussion_id` (reply) or `position` or `internal` | diff comments: MR head (`diff_refs` filled in, re-checked before posting) |
| `approve` | `iid`, `sha` (recommended) | `sha` when given; GitLab returns 409 on a mismatch |
| `unapprove` | `iid` | |
| `merge` | `iid`, `sha` (required), `squash`, `should_remove_source_branch`, `merge_commit_message`, `squash_commit_message`, `merge_when_pipeline_succeeds` | `allow_merge` must be on; `sha` re-checked; irreversible |
| `rebase` | `iid`, `skip_ci` | asynchronous: re-read the MR |
| `resolve` | `iid`, `discussion_id` (required), `resolved` (default true) | |
| `cancel_auto_merge` | `iid` | withdraws a pending merge-when-pipeline-succeeds |

`position` takes `new_path` (and `old_path` for renames) plus the line numbers from the `diffs` hunks.
GitLab needs `new_line` alone for an added line, `old_line` alone for a removed line, and both for
an unchanged line. Give `new_line` for added and unchanged lines and `old_line` for removed lines;
the plugin reads the diff, fills in the other side for unchanged lines, and rejects lines that are
not part of the diff (GitLab would answer 400). Boolean arguments accept true/false, yes/no, on/off
and 1/0; an empty string means "not given" and anything else is rejected. `discussion_id` must be the
id from a discussions listing (letters and digits); it is part of the URL path, so anything else is
rejected before a request is built.

## gitlab_pipeline_write

Needs `api` and Developer (run, retry, play) or the pipeline's owner / Maintainer (cancel).

| `action` | Parameters |
|---|---|
| `run` | `ref` (required), `variables` (`KEY: value`), `inputs` (pipeline inputs as `name: value`, GitLab 17.7+) |
| `retry` | exactly one of `pipeline_id`, `job_id` |
| `cancel` | exactly one of `pipeline_id`, `job_id` |
| `play` | `job_id` (a manual job) |

## gitlab_commit

Needs `api` and push rights on `branch` (protected branches refuse per GitLab's rules).

| Parameter | Notes |
|---|---|
| `branch` | required; must exist unless `start_branch` is given |
| `start_branch` | creates `branch` from this branch when it does not exist; ignored when it does |
| `commit_message` | required when `actions` is given |
| `actions` | 1 to 100 of `{action: create|update|delete|move|chmod, file_path, content, previous_path, encoding, execute_filemode, last_commit_id}`; total content under 2 MB; no `..` segments. Omit it, with `start_branch`, to only create `branch` (`POST repository/branches`, action `branch.create`); that is rejected when the branch already exists |
| `author_name`, `author_email` | optional |
| `expected_head_sha` | head of `branch` (or of `start_branch` for a new branch) as last read; a moved head stops the commit |

`dry_run` previews clip each file's `content` to its first 200 characters; the full content is what is sent
(and, in `operator_only` mode, what is staged).

Result: the commit (`id`, `short_id`, `title`, `stats`, `web_url`), or the branch for a branch-only
call. GitLab rejects `create` on an
existing path and `update`/`delete`/`move` on a missing one with a 400 that the tool reports verbatim.

## Common result fields

| Field | Meaning |
|---|---|
| `success` | the call did what was asked |
| `rejected: true`, `field` | arguments were invalid or a lookup failed; nothing sent |
| `refused: true`, `write_mode` | the gate stopped it; `error` names the setting or surface |
| `conflict: true`, `precondition` | the target changed since it was read, or GitLab answered 409 |
| `dry_run: true`, `preview` | nothing sent; `preview` has method, path, params, json, preconditions, requires, irreversible, notes |
| `staged: true`, `staged_id`, `command`, `cli`, `expires_at` | `operator_only`: stored for a human |
| `report` | one line for humans, with the URL where there is one |
| `request` | `method`, `path`, `status` of the HTTP call made |
| `gitlab` | GitLab's error (`status`, `body`) when a call failed |
