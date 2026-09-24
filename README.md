# hermes-plugin-gitlab

GitLab for [Hermes Agent](https://hermes-agent.nousresearch.com/), self-managed or GitLab.com. The agent reads
repositories, issues, merge requests and CI through typed tools, and **every write goes through one
gate**: a preview you can ask for, a project allow-list, head-SHA preconditions, an operator-only
staging mode, and an audit trail a SIEM can read.

[![CI](https://github.com/andrewvieyra/hermes-plugin-gitlab/actions/workflows/ci.yml/badge.svg)](https://github.com/andrewvieyra/hermes-plugin-gitlab/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License MIT](https://img.shields.io/badge/license-MIT-green)

```
you    > Is !42 in platform/api safe to merge?
hermes > [gitlab_merge_requests get]         mergeable, 1 approval left, head pipeline failed
hermes > [gitlab_merge_requests diffs]       2 files, +38 -4
hermes > [gitlab_pipelines get / log]        job "test" failed: AssertionError in test_token_refresh
         Not yet. The login handler compares passwords in clear text (src/login.py:2) and the
         test job fails on token refresh. One review thread from Andrew is still unresolved.
         Want me to post that as a review comment on !42?
you    > yes
hermes > [gitlab_mr_write comment]           Comment 517 posted on merge_request 42
         https://gitlab.example.com/platform/api/-/merge_requests/42#note_517
```

## Why a plugin

A skill that wraps `curl` works until the model forgets a parameter, URL-encodes a project path
wrongly, pastes the token into a shell command, or merges the wrong revision. The rules that make
GitLab safe to automate need to run the same way on every call: encode paths, page through results,
cap what reaches the model, bind writes to the revision that was reviewed, refuse what the operator
has not allowed, and record who asked. That logic lives in Python here, in-process, with no sidecar
MCP server, VM or container.

## How it works

```
 read tools (6)          write tools (4) + gitlab_api                       operator
 ──────────────          ────────────────────────────                       ────────
 gitlab_search           gitlab_issue_write    ─┐
 gitlab_repo             gitlab_mr_write        ├─► WriteRequest ─► gate ─► preview (dry_run)
 gitlab_issues           gitlab_pipeline_write  │    method, path,  │      stage (operator_only) ─► /gitlab run <id>
 gitlab_merge_requests   gitlab_commit          │    payload,       │      check preconditions (head sha)
 gitlab_pipelines        gitlab_api (POST/PUT/  │    preconditions, └─────► execute ─► audit.jsonl (+ syslog / HEC)
 gitlab_api (GET)                PATCH/DELETE) ─┘    required flags
```

Reads summarise GitLab's verbose objects and cap diffs, files and logs so results fit the model's
budget. Writes are built as a request first, then pass the same gate whichever tool asked. See
[docs/architecture.md](docs/architecture.md).

## Install

Requirements: Hermes Agent 0.21 or newer, Python 3.10+, `requests` (already in the Hermes venv), a
GitLab 15.7+ instance (16.0+ for the token self-check in `status`) and an access token.

```bash
hermes plugins install andrewvieyra/hermes-plugin-gitlab
```

Hermes prompts for `GITLAB_URL` and `GITLAB_TOKEN` and stores them in `~/.hermes/.env`. Then:

```bash
hermes plugins enable gitlab
```

Start a new session. The tools show up in the `gitlab` toolset and `/gitlab status` confirms
connectivity, the token's identity and scopes, and the write mode.

Manual install works too: clone this repository to `~/.hermes/plugins/gitlab`, add the two variables
to `~/.hermes/.env`, and enable the plugin.

### The token

Create a **project or group access token**, or a personal access token on a dedicated bot user, with
the least role that does the job:

| You want the agent to | Scope | Role |
|---|---|---|
| Read code, issues, MRs, CI logs | `read_api` | Reporter (Developer to read job logs on some instances) |
| Comment, label, open issues and MRs, commit to branches | `api` | Developer |
| Approve and merge | `api` | Maintainer, or Developer with approval rights and unprotected targets |

Protected branches, approval rules and the token's role are enforced by GitLab; the plugin's own
gate sits in front of them. An OAuth token works too: set `GITLAB_TOKEN` to `Bearer <token>`.

### Configuration

Settings live under `plugins.entries.gitlab.settings` in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled: [gitlab]
  entries:
    gitlab:
      settings:
        write_mode: full            # full | operator_only | read_only
        allow_merge: false          # gitlab_mr_write action=merge is refused unless true
        allow_raw_writes: false     # gitlab_api POST/PUT/PATCH
        allow_raw_delete: false     # gitlab_api DELETE (also needs allow_raw_writes)
        write_projects: []          # glob allow-list for writes, e.g. ["platform/*", "andrew/sandbox"]
        max_results: 100            # cap per list call
        max_diff_bytes: 120000      # cap on rendered diff text per call
        max_file_bytes: 200000      # cap on decoded file content per call
        max_log_lines: 300          # cap on job log lines per call
        max_staged_age_hours: 24    # staged writes older than this are refused by run
        staged_retention_days: 90   # prune finished staged writes; 0 keeps them forever
        redact_secrets: true        # mask token-shaped strings in logs, files, diffs, code search
        audit_log: true             # append events to audit.jsonl
        audit_log_path: ""          # empty = <HERMES_HOME>/plugin-data/gitlab/audit.jsonl
        audit_include_request: true # record the triggering message (truncated) in the actor
        audit_reads: false          # also record every read tool call
        audit_sinks: []             # optional syslog / HTTP forwarding, see docs/sinks.md
```

Environment variables:

| Variable | Required | Meaning |
|---|---|---|
| `GITLAB_URL` | yes | Base URL, e.g. `https://gitlab.example.com` |
| `GITLAB_TOKEN` | yes | Access token, sent as `PRIVATE-TOKEN`; `Bearer <token>` for OAuth |
| `GITLAB_VERIFY_SSL` | no | `false` to skip certificate verification, or a path to a CA bundle for a private CA |
| `GITLAB_TIMEOUT` | no | Request timeout in seconds (default 30) |

## Tools

| Tool | Writes | Purpose |
|---|---|---|
| `gitlab_search` | no | Find projects by name; free-text search of issues, MRs, milestones, users, code, commits |
| `gitlab_repo` | no | Project metadata, tree, file (line ranges, binaries reported not dumped), commits, one commit with diff, compare, branches, tags |
| `gitlab_issues` | no | Filtered lists; one issue with description, discussion threads, related MRs and linked issues |
| `gitlab_merge_requests` | no | Filtered lists; one MR with merge status, approvals, head pipeline and `sha`; diffs; review threads; commits; pipelines |
| `gitlab_pipelines` | no | Pipelines, one pipeline with jobs, failures and downstream pipelines, jobs by status, a job's log (tail or regex search, colour and sections stripped) |
| `gitlab_api` | GET, or gated | Any other endpoint; `path: status` for connectivity, token scopes and write mode |
| `gitlab_issue_write` | yes | Create, edit, close/reopen, comment, reply, internal notes |
| `gitlab_mr_write` | yes | Create, edit, comment (general, reply, on a diff line), approve, unapprove, merge, cancel auto-merge, rebase, resolve |
| `gitlab_pipeline_write` | yes | Run with variables and inputs, retry, cancel, play a manual job |
| `gitlab_commit` | yes | One atomic commit of create/update/delete/move/chmod actions, creating the branch first if needed, or just the branch |

Every write tool accepts `dry_run: true` and returns the exact method, path, payload, preconditions
and required flags without sending anything. Full parameter reference: [docs/tools.md](docs/tools.md);
endpoint-by-endpoint coverage of the GitLab REST API: [docs/api-coverage.md](docs/api-coverage.md).
Worked tool calls for a review, a CI diagnosis, a commit plus MR, an issue triage and operator
staging are in [examples/](examples/).

`project` accepts a numeric id, a `group/project` path, or a GitLab URL of the project or of one of
its issues, merge requests, pipelines or commits, so "review this: https://gitlab…/-/merge_requests/42"
needs no lookup.

The bundled skill `gitlab:workflow` (see [SKILL.md](SKILL.md)) tells the model to read first,
describe the write, ask, act once, treat fetched content as data rather than instructions, and in
unattended sessions (cron, webhooks) not to write at all.

### Slash command and CLI

Inside a session, `/gitlab` gives an operator the same facts without going through the model:

```
/gitlab status
/gitlab mr <project> <iid>
/gitlab pending
/gitlab show <id>
/gitlab run <id>
/gitlab drop <id>
/gitlab prune [--days N] [--dry-run]
/gitlab audit [N]
```

The same subcommands exist as `hermes gitlab …` in the shell. `show`, `run` and `drop` accept an
unambiguous fragment of a staged id, so `/gitlab run 4f1a` works from a phone.

## Safety model

- **Reads never write.** Five tools are read-only by construction. `gitlab_api` with GET is
  read-only too, and refuses credential and settings surfaces (`variables`, tokens, `hooks`, keys,
  `application/settings`, admin) even for GET, because their responses would put secrets into the
  model's context. Paths are matched after decoding and lower-casing, so encoded spellings are
  refused too.
- **One gate for every write.** Each write tool builds a request and hands it to the same gate:
  `write_mode`, the flag the action needs (`allow_merge`, `allow_raw_writes`, `allow_raw_delete`),
  the `write_projects` allow-list, and the deny-lists. Refusals name the setting that blocks them.
- **Merges are opt-in and bound to a revision.** `merge` is refused unless `allow_merge` is on,
  requires the head `sha` the model read, is re-checked against the live MR before the call, and
  GitLab's own 409 on a moved head is reported as a conflict. Approvals take the same `sha`. Diff
  comments are resolved against the MR diff so the line GitLab receives is one it can anchor to.
- **Commits are bound to the branch head.** `gitlab_commit` takes `expected_head_sha`; the branch
  is re-read before the commit and a moved head stops it. Diff comments are bound to the MR head.
- **Administrative surfaces stay closed.** Even with raw writes enabled, membership, permissions,
  protected branches, approval rules, runners, project and group settings, transfer, archive, forking
  and issue moves are refused, and so are `sudo` and credential parameters. Merging, approving and
  committing through `gitlab_api` are refused too: they belong to the typed tools, which bind them to
  the reviewed revision and honour `allow_merge`. DELETE needs its own flag.
- **Operator-only mode stages instead of sending.** With `write_mode: operator_only`, a model write
  is written to `<HERMES_HOME>/plugin-data/gitlab/staged/` with its exact payload and the model is
  told to relay `/gitlab run <id>`. A human runs it, from any Hermes surface or the shell, under a
  cross-process lock. Staged writes expire, are bound to the GitLab URL they were built for, and are
  re-gated and re-checked when run.
- **Secrets are masked.** Job logs are scanned for token formats, private keys, bearer headers and
  `password=` assignments; file contents, diffs, code search, descriptions, comments, commit messages
  and raw `gitlab_api` results for token formats. The plugin's own token is scrubbed from every result
  and error whatever `redact_secrets` says, and the user's triggering message is redacted before it is
  recorded in the audit trail.
- **Nothing is dumped.** Lists, diffs, files and logs are capped by the operator, and truncation is
  reported so the model asks for a slice instead of guessing.
- **Everything is attributed.** Every write, refusal, rejection, conflict, preview and staged action
  records who asked, from which platform and chat, and the HTTP call made. See [docs/audit.md](docs/audit.md).

### Write modes

Hermes plugins cannot open an approval prompt from inside a tool, so in the default `full` mode the
confirmation step is the model asking you in chat and then calling the write tool after you say yes.
That is behaviour, driven by the skill and the tool descriptions, not a lock. `write_mode` adds the lock:

| `write_mode` | Who can write | Use it when |
|---|---|---|
| `full` | The model, after asking | One trusted user in a direct message, no automation |
| `operator_only` | Only `/gitlab run` in a session and `hermes gitlab run` in a shell. Model writes are staged with their exact payload and the command to run | Group chats, messaging gateways, cron jobs present, prompt-injection concerns, "a human must cause every write" |
| `read_only` | Nobody. The four write tools are not registered for the model; raw non-GET calls are refused | Audits, demos, first deployments |

Whatever the mode, scope the token to the permissions you want the agent to have. A `read_api` token
is the one safeguard GitLab enforces itself.

### Untrusted content

Everything the plugin reads from GitLab (issue text, MR descriptions, comments, commit messages, file
contents, job logs) was written by someone else and reaches the model as data. The plugin cannot stop
the model from being influenced by it; it limits what a misled model can do (gate, allow-list, flags,
staging, deny-lists) and makes every attempt visible (audit). The skill tells the model to treat
fetched content as data and to report instructions found in it. See [SECURITY.md](SECURITY.md).

## Audit trail

Every write and every refusal is recorded for review and for SIEM ingestion:

- **In staged-write files:** who requested the write (`requested_by`), who ran or dropped it
  (`run.actor`), the environment (`audit`: host, OS user, pid, Hermes home, Hermes and plugin
  versions) and the result.
- **In an append-only event stream**, `<HERMES_HOME>/plugin-data/gitlab/audit.jsonl`, one JSON
  object per line: `write_done`, `write_failed`, `write_conflict`, `write_refused`, `write_rejected`,
  `write_previewed`, `write_staged`, `staged_run`, `staged_dropped`, `staged_expired`,
  `staged_pruned`, and with `audit_reads`, `read_done` / `read_failed`.

The actor record distinguishes a model tool call from a human using `/gitlab` or `hermes gitlab`, and
carries the gateway platform, chat, user, session, and the message that triggered the call. Every
event names the action (`mr.merge`, `commit.create`, `api.POST`), the project and the target. All
timestamps are UTC. Schema and examples: [docs/audit.md](docs/audit.md).

Installs without a log shipper can forward events directly: `audit_sinks` sends each event to a
syslog server (UDP, TCP or TLS, JSON or CEF) and/or an HTTP collector such as Splunk HEC, from a
background thread that never delays a GitLab operation. See [docs/sinks.md](docs/sinks.md).

## Compatibility

- GitLab 15.7 and newer, self-managed or GitLab.com. The MR `diffs` endpoint needs 15.7+;
  `personal_access_tokens/self` (used by `status`) needs 16.0+ and is skipped when absent;
  `merge_when_pipeline_succeeds` is sent together with its 17.11+ name `auto_merge`.
- Free, Premium and Ultimate. Approval counts read as 0 required on Free; code search scopes need
  advanced search or exact code search and the tool says so when they are off.
- Hermes Agent 0.21+. The plugin uses only the documented `register(ctx)` surface:
  `register_tool`, `register_command`, `register_cli_command`, `register_skill`, `get_config`.

## Development

```bash
make test        # unit tests against the in-memory fake GitLab, no network
make lint        # ruff
make doctor      # hermes plugins doctor . --ci
HERMES_AGENT_ROOT=~/.hermes/hermes-agent make test-hermes   # load through Hermes' real PluginManager
```

The fake GitLab in `tests/fake_gitlab.py` implements token auth, pagination headers, URL-encoded
paths, GitLab's error shapes, base64 files, diffs, discussions with positions, approvals, merge
rules (409 on a stale sha, 405/422 when unmergeable), pipelines, jobs and logs, so every read, write,
refusal, conflict and staging path is exercised end to end without a server.

Layout:

```
plugin.yaml   manifest (tools, env, settings schema)
__init__.py   register(ctx)
schemas.py    tool schemas the model sees
handlers.py   tool handlers: parse args -> client / writes / executor -> JSON
writes.py     argument validation and lookups -> WriteRequest
executor.py   the gate, preview, staging, preconditions, execution, audit events
commands.py   /gitlab slash command and `hermes gitlab` CLI
client.py     GitLab REST client (auth, pagination, encoding, errors, 429)
targets.py    project refs and GitLab URLs, allow-list matching, API paths
render.py     summaries, diff rendering, job log cleaning, secret redaction, operator text
store.py      staged-write persistence under plugin-data
audit.py      actor capture and the audit.jsonl event stream
sinks.py      optional syslog / HTTP forwarding of audit events
timefmt.py    UTC-to-local rendering for reports (Hermes' timezone setting)
settings.py   operator settings
SKILL.md      bundled skill: the workflow the model follows
```

## License

MIT. See [LICENSE](LICENSE).
