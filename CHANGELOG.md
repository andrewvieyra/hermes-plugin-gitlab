# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `gitlab_repo` `action=project`: project metadata (default branch, visibility, merge settings).
- `gitlab_commit` without `actions` creates `branch` from `start_branch` (bound to that head with
  `expected_head_sha`); action `branch.create` in the audit trail.
- `gitlab_pipeline_write` `run` accepts `inputs` (pipeline inputs, GitLab 17.7+).
- `gitlab_pipelines` `get` lists trigger jobs with their downstream pipelines (`bridges`); `gitlab_issues`
  `get` lists `linked_issues`.
- Commit previews clip each file's content to 200 characters so a dry run does not echo a large commit back.
- `gitlab_mr_write` `action=cancel_auto_merge`; `gitlab_merge_requests` `get` lists `closes_issues`;
  `gitlab_pipelines` `list` takes `name`.
- `docs/api-coverage.md`: the GitLab REST API endpoint by endpoint, with the tool and action that covers it,
  what the escape hatch reaches, and what is refused.

### Security
- Redirects are never followed: a 3xx from GitLab is an error, so `PRIVATE-TOKEN` cannot be sent to another host.
- Raw paths are rejected when a percent-decoded segment is `..`, and the deny-lists also match the path
  with `..` resolved, in case a reverse proxy normalises it before GitLab.
- `gitlab_api` refuses `triggers` and `tokens` surfaces for every method, and `POST projects`, `POST groups`,
  `merged_branches` and LDAP/SAML group links for non-GET.
- Raw `gitlab_api` results (GET and write responses) are secret-redacted like every other read, and GET
  results larger than `max_file_bytes` are clipped.
- Secret redaction covers routable GitLab tokens (with `.` segments), legacy runner registration tokens
  and OpenAI/Anthropic, Stripe, Google, npm, Hugging Face and PyPI token formats.
- `discussion_id` is validated (letters and digits only) before it is interpolated into a URL: with a
  normalising reverse proxy, `../merge?sha=…` on `resolve` could otherwise have merged without `allow_merge`.
- `sudo`, `private_token`, `access_token`, `oauth_token` and `job_token` are refused in raw params and bodies,
  so a call always runs as the configured token and user.
- More refused surfaces: `trigger`, single `pipeline_schedules/:id` (lists its variables), error-tracking
  `client_keys`, project and group `audit_events` for every method; `fork`, issue `move`/`clone`,
  `access_requests`, `invitations`, `billable_members`, `job_token_scope`, `pages`, `protect`/`unprotect`,
  `reset_approvals` and project transfer for non-GET.
- The plugin's own token is scrubbed from every tool result regardless of `redact_secrets`; the user's
  triggering message is redacted before it is recorded in the audit trail; project search results, branch and
  tag listings and typed write responses go through user-text redaction too.
- Job-log `search` patterns are capped at 200 characters and refused when they could backtrack exponentially
  or cubically (a repeated group containing a quantifier or an alternation, backreferences, counted repeats
  above 100, more than two unbounded quantifiers), searched within the first 500 characters of each line under
  a 2-second budget that is reported when it runs out; `/gitlab audit` lines cannot be broken by newlines.
- Raw `gitlab_api` writes to `merge_requests/:iid/merge`, `.../approve` and `repository/commits` are refused
  and point at the typed tool: `allow_raw_writes` alone could otherwise merge without `allow_merge` or a
  head-sha check. `DELETE` / rotate on `personal_access_tokens/self` is refused.

### Fixed
- Diff comments on renamed files send the file's real `old_path` instead of repeating `new_path`.
- GitLab URLs under a relative URL root (`https://host/gitlab/group/project`) resolve to the right project;
  group, admin and explore URLs are no longer mistaken for projects.
- A builder bug is reported as a rejected write with an audit event instead of a bare exception message.

## [0.1.0] - 2026-09-09

### Added
- Read tools: `gitlab_search` (projects, issues, MRs, milestones, users, code, commits), `gitlab_repo`
  (tree, file with line ranges, commits, commit with diff, compare, branches, tags), `gitlab_issues`
  (filtered lists, one issue with discussions and related MRs), `gitlab_merge_requests` (lists, one MR
  with approvals and head pipeline, diffs, review threads, commits, pipelines), `gitlab_pipelines`
  (lists, one pipeline with jobs and failures, jobs, job, log tail or regex search) and `gitlab_api`
  (GET escape hatch and `status`). GitLab URLs are accepted wherever a project is expected.
- Write tools: `gitlab_issue_write` (create, update, comment), `gitlab_mr_write` (create, update,
  comment including diff-line threads, approve, unapprove, merge, rebase, resolve), `gitlab_pipeline_write`
  (run, retry, cancel, play) and `gitlab_commit` (atomic multi-file commits, branch creation). Every
  write tool supports `dry_run`.
- One write gate: `write_mode` (`full`, `operator_only`, `read_only`), `allow_merge`, `allow_raw_writes`,
  `allow_raw_delete`, the `write_projects` glob allow-list, and deny-lists for credential surfaces (all
  methods) and administrative surfaces (non-GET) on `gitlab_api`.
- Revision-bound writes: `sha` for approve and merge, `expected_head_sha` for commits, MR head for diff
  comments; heads are re-read before the write and a change is reported as a conflict.
- Operator staging: in `operator_only` mode model writes are stored with their exact payload and run by
  a human with `/gitlab run <id>` or `hermes gitlab run <id>` under a cross-process lock; staged writes
  expire (`max_staged_age_hours`), are bound to the GitLab URL, and are pruned after `staged_retention_days`.
- Secret redaction in job logs, file contents, diffs and code search results (`redact_secrets`), and
  token scrubbing in every error message.
- Audit trail for SIEM ingestion: actor records (model vs operator, platform, chat, user, session,
  triggering message) and an append-only `audit.jsonl` stream covering every write, refusal, rejection,
  conflict, preview and staged-write event, plus optional read events (`audit_reads`). Optional
  forwarding to syslog (RFC 5424, JSON or CEF) and HTTP collectors (JSON or Splunk HEC) via `audit_sinks`.
- `/gitlab` slash command and `hermes gitlab` CLI: `status`, `mr`, `pending`, `show`, `run`, `drop`,
  `prune`, `audit`.
- Bundled `gitlab:workflow` skill.
- Test suite with an in-memory fake GitLab and a loader test through Hermes' `PluginManager`.

### Review pass before release
- Diff-comment positions are resolved against the merge request diff: unchanged lines get both line numbers, as
  GitLab requires, and lines outside the diff are rejected before anything is sent.
- `gitlab_api` deny-lists match percent-decoded and lower-cased paths, closing an encoding bypass.
- Issues `assignee: None` / `Any` use `assignee_id`; pipelines `list` no longer forwards the job `scope` filter.
- `discussions` scans every page and counts unresolved threads over all of them.
- File line ranges are taken from the whole file before the byte cap, and invalid ranges are errors.
- Diff rendering skips a file that does not fit and keeps later files that do.
- Empty-string booleans mean "not given" and junk booleans are rejected; `audit_log_path` keeps its case.
- Operator reads through `/gitlab` and the CLI are audited as operator; raw writes no longer emit a read event.
- An interrupted staged run is finalised as `failed` and a `running` document can be dropped.
- Secret redaction also covers issue and MR descriptions, comments, commit messages and titles.
