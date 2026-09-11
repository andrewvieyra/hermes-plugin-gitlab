# Architecture

## Goals

1. The agent can read anything the token can see, in a form that fits the model's budget, and
   change GitLab only through requests that were built, gated, bound to a revision and audited.
2. The rules run the same way on every call: they live in Python, not in the model's memory.
3. The plugin stays inside Hermes' documented plugin surface so it survives Hermes upgrades, and
   runs in-process: no MCP sidecar, VM or container.
4. Coverage without sprawl: ten tools cover the daily work; `gitlab_api` reaches the rest of the
   REST API under the same gate.

## Modules

| Module | Responsibility | Depends on |
|---|---|---|
| `client` | HTTP: auth header, pagination, path encoding, error typing, 429 retry, token scrubbing | `requests` (lazy) |
| `targets` | Project references and GitLab URLs, iids, labels, allow-list matching, API path builders | `client` |
| `render` | Summaries of GitLab objects, diff rendering, job log cleaning, secret redaction, operator text | `timefmt` |
| `writes` | Tool arguments -> `WriteRequest` (validation, name-to-id lookups, preconditions, required flags) | `client`, `targets`, `executor` |
| `executor` | The gate, preview, staging, preconditions, execution, result description, audit events | `client`, `targets`, `render`, `audit`, `store`, `settings` |
| `store` | Staged-write persistence with exclusive create, atomic save, cross-process lock, retention | Hermes `plugin_storage` (optional) |
| `audit` | Actor capture from Hermes' session context; append-only `audit.jsonl` | Hermes `gateway.session_context` (optional) |
| `sinks` | Optional syslog / HTTP forwarding of audit records on a background thread | none |
| `timefmt` | UTC stamps rendered in the Hermes-configured zone for reports only | Hermes `hermes_time` (optional) |
| `settings` | Operator settings with coercion and defaults | Hermes `ctx.get_config` (optional) |
| `schemas` | Tool schemas | none |
| `handlers` | Tool handlers: args to JSON, error mapping, client factory seam, operator entry points | everything above |
| `commands` | `/gitlab` and `hermes gitlab` built on the handlers | `handlers` |
| `__init__` | `register(ctx)` | all |

Dependencies point downward only. Nothing below `handlers` imports Hermes at module load, which is
why the whole engine is testable with plain `unittest` and the fake GitLab.

## Reads

A read handler resolves `project` (id, path or URL), calls one or a few endpoints, and returns a
summary. Three rules apply everywhere:

- **Summarise.** `render.merge_request`, `render.issue`, `render.job` and friends keep the fields a
  reviewer needs (state, people, labels, branches, sha, pipeline, URLs) and drop the rest. `full=True`
  adds descriptions and diff refs for single-object reads.
- **Cap and say so.** Lists stop at `max_results`; diffs at `max_diff_bytes` (the per-file summary
  stays complete, omitted files are listed); files at `max_file_bytes`; logs at `max_log_lines`.
  Every capped result carries `truncated` and, where useful, how to ask for the next slice.
- **Redact.** Job logs pass through `render.redact_log` (token formats, private keys, bearer
  headers, basic-auth URLs, `password=` assignments); file contents, diffs and code search results
  through `render.redact_content` (token formats only, to avoid mangling code); user-written text
  (descriptions, comment bodies, commit messages, titles) through `render.redact_fields` with the
  same patterns. The count of replacements is reported so a `[REDACTED]` marker is explainable.
- **Scan discussions completely.** GitLab lists discussions oldest first and a long-lived MR has
  hundreds of system notes ahead of the review threads, so `discussions` walks every page (up to
  2,000 discussions) and counts unresolved threads over all of them, returning at most `limit`.
- **Resolve diff positions.** GitLab anchors a diff note by `(old_line, new_line)`: an added line
  needs `new_line`, a removed line `old_line`, an unchanged line both. The comment builder reads the
  MR diff, locates the line and fills in the missing side, and rejects lines outside the diff.

## Writes

```
 handler ──► writes.<builder>(client, args, settings) ──► WriteRequest
                                                            │ action, method, path, params, json
                                                            │ project, target, summary
                                                            │ preconditions, requires, irreversible
                                                            ▼
                                  executor.perform(client, req, actor, settings, store, dry_run)
                                    1. gate            read_only? required flags? raw deny-lists? write_projects?
                                    2. dry_run         -> write_previewed, return preview
                                    3. operator_only   -> stage on disk, return command
                                    4. preconditions   re-read branch/MR head, compare sha
                                    5. execute         one HTTP call; 409 -> conflict
                                    6. describe        summary of the created/changed object
                                    every step emits an audit event
```

A builder may read (look up users, milestones, the MR's `diff_refs`, whether a branch exists) but
never writes. Validation failures raise `WriteError` and are reported as `rejected` with the field;
lookup failures propagate as GitLab errors and are also `rejected`. Neither reaches the gate.

### The gate

| Check | Refusal names |
|---|---|
| `write_mode == read_only` | the mode |
| `req.requires` flags (`allow_merge`, `allow_raw_writes`, `allow_raw_delete`) | the setting to enable |
| `api.*` actions: sensitive paths (any method), admin paths (non-GET) | the surface |
| `write_projects` allow-list (numeric ids resolved to paths, memoised per call) | the project and the list |

Refusals are audited as `write_refused` with `reason`. A dry run is gated too, so a preview never
claims that a refused write would succeed.

### Preconditions

| Kind | Recorded by | Checked how |
|---|---|---|
| `mr_head` | `approve` (when `sha` given), `merge`, diff comments | GET the MR; state must be `opened`; `sha` must match (prefix of 7+ hex accepted; for approve and merge a short sha is expanded to the full head before sending, because GitLab compares exactly) |
| `branch_head` | `gitlab_commit` (when `expected_head_sha` given) | GET the branch (or `start_branch` for a new branch); `commit.id` must match |

A mismatch is `write_conflict` with `expected` and `actual`; nothing is sent. The check is read-then-
write, not atomic: a push landing between the check and the call is caught by GitLab's own 409 for
merge and approve, and reported as a conflict; for commits GitLab has no such check, so the window is
one request.

### Staging

In `operator_only` mode a model-initiated request that passes the gate is stored instead of sent:

```jsonc
{
  "id": "glw-20260909T193012Z-4f1a",
  "version": 1,
  "status": "staged",          // staged | running | done | failed | conflict | refused | dropped | expired
  "gitlab_url": "https://gitlab.example.com",
  "created_at": "2026-09-09T19:30:12Z",
  "expires_at": "2026-09-10T19:30:12Z",
  "request": { /* WriteRequest.to_dict() */ },
  "requested_by": { /* actor */ },
  "requested_by_text": "model via tool on signal for Andrew in dm Andrew",
  "audit": { "host": "hermes-01", "os_user": "hermes", "plugin_version": "0.1.0", "hermes_version": "0.21.0" },
  "run": { "started_at": "...", "finished_at": "...", "actor": { /* operator */ }, "outcome": "done", "report": "...", "request": {"method": "PUT", "path": "...", "status": 200} }
}
```

`/gitlab run <id>` (or the CLI) loads the document, refuses it when it is not `staged`, expired, or
built for another GitLab URL, claims it under the store's cross-process file lock (`staged -> running`),
and calls `executor.perform` with the operator's actor and `staged_id`. The gate and the preconditions
run again at that moment: a flag turned off since staging, or a branch that moved, stops it. The
outcome is written back to the file and emitted as `staged_run`. An interrupted run (Ctrl-C, a crash
in the plugin) is recorded as `failed` with a note that the request may or may not have reached
GitLab; a document left `running` by a hard kill can be dropped by an operator.

Files are created with `O_EXCL` and mode `0600`; ids are `glw-<UTC timestamp>-<4 hex>` so they sort
and cannot collide silently. `staged_retention_days` prunes finished documents on an hourly
opportunistic sweep and on `/gitlab prune`.

## Deny-lists for `gitlab_api`

Sensitive, every method (their responses carry secrets or their state is credentials):
`variables`, `deploy_tokens`, `access_tokens`, `deploy_keys`, `hooks`, `secure_files`, `integrations`,
`services`, `export`/`import`, `personal_access_tokens` (except `/self`), `user/keys` and similar,
`application/*`, `admin/*`, `broadcast_messages`, `license`, `sidekiq`, `system_hooks`, `keys`,
`geo`, `audit_events`.

Administrative, non-GET (a human with the right role should do these in the UI): `users`, a project
or group itself (`projects/:id`, `groups/:id`), `members`, `share`, `protected_*`, `approval_rules`,
project approval settings, `runners`, `transfer`/`archive`/`unarchive`/`restore`, mirrors, push
rules, housekeeping, `namespaces`, `topics`, `applications`, `oauth`.

The lists are regular expressions in `executor.py`; a refusal names the surface. They are matched
against every spelling GitLab could resolve the path to: as given, percent-decoded (repeatedly, for
double encoding), lower-cased, with duplicate slashes collapsed. The encoded form stays in the set so
`projects/group%2Fproj` still matches the project-level rules. A project literally named `hooks` or
`variables` is therefore unreachable through the raw tool; the typed tools are unaffected.

## Hermes integration

- `register(ctx)` performs no I/O beyond reading settings; Plugin Doctor blocks sockets during
  registration and this plugin passes.
- Read tools are registered with `check_fn=check_requirements`, write tools with
  `check_fn=check_write_requirements`, plus `requires_env`, so they are hidden from the model when
  `GITLAB_URL` / `GITLAB_TOKEN` are missing (or, for writes, when `write_mode` is `read_only`).
- Handlers follow the contract `handler(args, **kwargs) -> str` and never raise. The `task_id`,
  `session_id` and `user_task` kwargs Hermes passes feed the actor record.
- The staged store uses `plugins.plugin_storage.plugin_data_dir` when available so files follow the
  active Hermes profile, with a plain `$HERMES_HOME/plugin-data/gitlab` fallback.

## Non-goals

- Human approval UI inside a tool. Hermes plugins cannot open one; the skill and tool descriptions
  drive the model to ask, and `operator_only` is the hard version of that rule.
- Rollback. GitLab keeps its own history (revert a commit, reopen an issue, unapprove); merges are
  irreversible, which is why they are opt-in and revision-bound.
- Webhooks, GraphQL, container registry, packages, wikis as first-class tools. `gitlab_api` reaches
  the REST parts under the gate; add a typed tool when the workflow is common.
- Schema-aware validation of raw `gitlab_api` bodies. GitLab validates on write; the audit records
  the outcome.
