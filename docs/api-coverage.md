# GitLab REST API coverage

How the plugin maps onto the [GitLab REST API](https://docs.gitlab.com/api/rest/), endpoint by
endpoint. "Typed" means a purpose-built tool action with validation, summarising and (for writes)
the gate and preconditions. "Escape hatch" means `gitlab_api`: GET is always available except on the
refused surfaces; other methods need `allow_raw_writes` (`allow_raw_delete` for DELETE) and pass the
same gate. "Refused" means no flag enables it; see [architecture.md](architecture.md) for the lists.

All paths are under `/api/v4`. The plugin speaks REST only: GraphQL (`/api/graphql`) is not reachable.

## Projects and search

| Endpoint | Coverage |
|---|---|
| `GET /projects`, `GET /groups/:id/projects` | typed: `gitlab_search` `scope=projects` (name search, membership, archived) |
| `GET /projects/:id` | typed: `gitlab_repo` `action=project` |
| `GET /search`, `GET /projects/:id/search`, `GET /groups/:id/search` | typed: `gitlab_search` (issues, merge_requests, milestones, users, blobs, commits, wiki_blobs, notes) |
| `POST /projects`, `PUT /projects/:id`, `DELETE /projects/:id`, `POST /projects/:id/fork`, `transfer`, `archive` | refused |
| `GET /groups`, `GET /groups/:id`, subgroups, descendant groups | escape hatch (GET) |
| `POST /groups`, `PUT /groups/:id`, `DELETE /groups/:id` | refused |
| `GET /projects/:id/languages`, `statistics`, `badges`, `topics` (GET) | escape hatch |

## Repository

| Endpoint | Coverage |
|---|---|
| `GET /projects/:id/repository/tree` | typed: `gitlab_repo` `tree` |
| `GET /projects/:id/repository/files/:path` | typed: `gitlab_repo` `file` (decoded, line ranges, binaries reported); `/raw` and `/blame` via escape hatch |
| `GET /projects/:id/repository/commits`, `.../commits/:sha`, `.../commits/:sha/diff` | typed: `gitlab_repo` `commits`, `commit` |
| `GET /projects/:id/repository/commits/:sha/comments`, `/statuses`, `/refs`, `/merge_requests`, `/signature` | escape hatch (GET) |
| `POST /projects/:id/repository/commits` | typed: `gitlab_commit` (atomic actions, `start_branch`, `expected_head_sha`); refused through `gitlab_api` |
| `POST /projects/:id/repository/commits/:sha/cherry_pick`, `/revert`, `/statuses` | escape hatch (write) |
| `GET /projects/:id/repository/compare` | typed: `gitlab_repo` `compare` |
| `GET /projects/:id/repository/branches`, `.../branches/:name` | typed: `gitlab_repo` `branches` |
| `POST /projects/:id/repository/branches` | typed: `gitlab_commit` without `actions` |
| `DELETE /projects/:id/repository/branches/:name` | escape hatch (needs `allow_raw_delete`) |
| `DELETE /projects/:id/repository/merged_branches` | refused |
| `GET /projects/:id/repository/tags` | typed: `gitlab_repo` `tags` |
| `POST` / `DELETE /projects/:id/repository/tags` | escape hatch |
| `GET /projects/:id/repository/archive`, `contributors`, `merge_base` | escape hatch (GET; archives are clipped) |
| `POST` / `PUT` / `DELETE /projects/:id/repository/files/:path` (single-file commit) | typed: `gitlab_commit` with one action; refused through `gitlab_api` |
| `protected_branches`, `protected_tags` | GET via escape hatch; changes refused |

## Issues

| Endpoint | Coverage |
|---|---|
| `GET /issues`, `GET /projects/:id/issues`, `GET /groups/:id/issues` (group: escape hatch) | typed: `gitlab_issues` `list` |
| `GET /projects/:id/issues/:iid` | typed: `gitlab_issues` `get` |
| `GET .../issues/:iid/discussions`, `/related_merge_requests`, `/links` | typed: inside `gitlab_issues` `get` |
| `GET .../issues/:iid/notes`, `/participants`, `/closed_by`, `/time_stats`, `/resource_label_events` | escape hatch (GET) |
| `POST /projects/:id/issues` | typed: `gitlab_issue_write` `create` |
| `PUT /projects/:id/issues/:iid` (title, description, labels, assignees, milestone, due date, confidential, lock, state) | typed: `gitlab_issue_write` `update` |
| `POST .../issues/:iid/notes`, `POST .../discussions/:id/notes` | typed: `gitlab_issue_write` `comment` (`internal`, `discussion_id`) |
| `POST .../issues/:iid/links`, `/subscribe`, `/todo`, `/time_estimate`, `/add_spent_time` | escape hatch (write) |
| `POST .../issues/:iid/move`, `/clone` | refused (writes to a project the allow-list never saw) |
| `DELETE /projects/:id/issues/:iid` | escape hatch (needs `allow_raw_delete`) |
| Epics, work items (`/groups/:id/epics`, `/work_items` GraphQL-first) | escape hatch (REST parts only) |

## Merge requests

| Endpoint | Coverage |
|---|---|
| `GET /merge_requests`, `GET /projects/:id/merge_requests` | typed: `gitlab_merge_requests` `list` |
| `GET /projects/:id/merge_requests/:iid` (+ diverged count, rebase status) | typed: `get` |
| `GET .../merge_requests/:iid/approvals` | typed: inside `get` |
| `GET .../merge_requests/:iid/closes_issues` | typed: inside `get` |
| `GET .../merge_requests/:iid/diffs` (15.7+) | typed: `diffs` |
| `GET .../merge_requests/:iid/discussions` | typed: `discussions` |
| `GET .../merge_requests/:iid/commits` | typed: `commits` |
| `GET .../merge_requests/:iid/pipelines` | typed: `pipelines` |
| `GET .../merge_requests/:iid/versions`, `/participants`, `/reviewers`, `/approval_state`, `/related_issues`, `/notes`, `/changes` (deprecated) | escape hatch (GET) |
| `POST /projects/:id/merge_requests` | typed: `gitlab_mr_write` `create` |
| `PUT /projects/:id/merge_requests/:iid` | typed: `update` (draft via title prefix, as GitLab does) |
| `POST .../merge_requests/:iid/notes`, `/discussions` (with position), `/discussions/:id/notes` | typed: `comment` |
| `PUT .../merge_requests/:iid/discussions/:id` (`resolved`) | typed: `resolve` |
| `POST .../merge_requests/:iid/approve`, `/unapprove` | typed: `approve` (with `sha`), `unapprove`; `approve` is refused through `gitlab_api` |
| `PUT .../merge_requests/:iid/merge` | typed: `merge` (`sha` required, `allow_merge`, auto-merge flags); refused through `gitlab_api` |
| `POST .../merge_requests/:iid/cancel_merge_when_pipeline_succeeds` | typed: `cancel_auto_merge` |
| `PUT .../merge_requests/:iid/rebase` | typed: `rebase` |
| `POST .../merge_requests/:iid/pipelines` (run MR pipeline), `/subscribe`, `/todo`, `/time_estimate` | escape hatch (write) |
| `PUT .../merge_requests/:iid/reset_approvals`, approval rules, approval settings | refused |
| `DELETE /projects/:id/merge_requests/:iid` | escape hatch (needs `allow_raw_delete`) |
| Draft notes (`/draft_notes`, batched review submission) | escape hatch (write); the typed tool posts comments one at a time |

## Pipelines and jobs

| Endpoint | Coverage |
|---|---|
| `GET /projects/:id/pipelines`, `.../pipelines/latest` | typed: `gitlab_pipelines` `list` (filters, `latest`) |
| `GET .../pipelines/:id`, `.../pipelines/:id/jobs`, `.../pipelines/:id/bridges` | typed: `get` (jobs, failures, per-stage counts, downstream pipelines) |
| `GET .../pipelines/:id/test_report`, `/test_report_summary` | escape hatch (GET) |
| `GET .../pipelines/:id/variables` | refused (values) |
| `GET /projects/:id/jobs`, `.../jobs/:id` | typed: `jobs`, `job` |
| `GET .../jobs/:id/trace` | typed: `log` (cleaned, redacted, tail or regex search) |
| `GET .../jobs/:id/artifacts`, `/artifacts/:path` | escape hatch (GET; binary results are clipped) |
| `POST /projects/:id/pipeline` (with `variables`, `inputs`) | typed: `gitlab_pipeline_write` `run` |
| `POST .../pipelines/:id/retry`, `/cancel` | typed: `retry`, `cancel` |
| `POST .../jobs/:id/retry`, `/cancel`, `/play` | typed: `retry`, `cancel`, `play` |
| `POST .../jobs/:id/erase`, `/artifacts/keep`, `DELETE .../pipelines/:id`, `PUT .../pipelines/:id/metadata` | escape hatch |
| `GET /projects/:id/pipeline_schedules` | escape hatch (GET); a single schedule (`/:id`) is refused because it lists variable values |
| `POST /projects/:id/trigger/pipeline` | refused (uses a trigger token) |
| Runners | GET via escape hatch; changes refused |

## Users, tokens, instance

| Endpoint | Coverage |
|---|---|
| `GET /user` | typed: `gitlab_api` `path: status` |
| `GET /personal_access_tokens/self` (16.0+) | typed: `status` (scopes, expiry warnings); `DELETE` and `rotate` on it are refused |
| `GET /version` | typed: `status` |
| `GET /users?username=` | typed: inside assignee, reviewer and milestone resolution; `gitlab_search` `scope=users` |
| `GET /users/:id`, `/users/:id/status`, `/user/status` | escape hatch (GET) |
| Everything under `/users` for non-GET, `/user/keys`, `/user/emails`, impersonation tokens, `/keys` | refused |
| `/admin/*`, `/application/*`, `/license`, `/sidekiq`, `/geo`, `/system_hooks`, `/broadcast_messages`, `/audit_events` | refused |

## Everything else

| Area | Coverage |
|---|---|
| Milestones (`/projects/:id/milestones`, group milestones) | title lookup inside issue and MR writes; escape hatch for lists and changes |
| Labels (`/projects/:id/labels`, group labels) | escape hatch (GET and, with the flag, changes) |
| Releases, environments, deployments, feature flags, freeze periods | escape hatch |
| Wikis (`/projects/:id/wikis`), snippets | escape hatch |
| Packages, container registry, dependency proxy | escape hatch (GET; deletes need `allow_raw_delete`) |
| Members, access requests, invitations, group shares | GET via escape hatch; changes refused |
| Variables (project, group, instance), deploy tokens, deploy keys, secure files, hooks, integrations | refused for every method |
| Project and group import / export | refused |
| Notification settings, todos, events | escape hatch |
| Keyset pagination, `sudo`, alternate credentials | not offered: pagination is page-based (`x-next-page`); `sudo` and credential parameters are refused |
