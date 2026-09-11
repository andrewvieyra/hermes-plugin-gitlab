# Security

## Reporting

Report vulnerabilities privately through GitHub's security advisory form for this repository
rather than a public issue. You will get an acknowledgement within a few days.

## Threat model

The plugin runs in-process inside Hermes with a GitLab token. The things that can go wrong, and
what the plugin does about each:

| Threat | Control |
|---|---|
| The token leaks into chat, logs or files | Read from the environment at call time; sent only as a header; scrubbed from every error message; never written to staged files or the audit stream. |
| Fetched content steers the model (prompt injection in an issue, MR, README or CI log) | The plugin cannot stop influence; it bounds consequences: `write_mode`, `allow_*` flags, `write_projects`, deny-lists, revision-bound writes, and an audit event for every attempt. `operator_only` puts a human between the model and every write. The skill tells the model to treat content as data. |
| The model merges or commits the wrong revision | `merge` and `approve` take the head `sha`; `gitlab_commit` takes `expected_head_sha`; diff comments bind to the MR head. Heads are re-read right before the write and a change stops it; GitLab's own 409 is honoured. |
| The model reaches beyond the projects it should touch | `write_projects` glob allow-list, checked after resolving numeric ids to paths. Reads are governed by the token's visibility. |
| The escape hatch is used to change permissions or read secrets | `gitlab_api` refuses credential and settings surfaces for every method (variables, tokens, hooks, keys, admin) and membership, permission, protected-branch, runner and project/group settings for non-GET, regardless of flags. Paths are matched after percent-decoding (repeatedly) and lower-casing, so `%76ariables` and `VARIABLES` are refused like `variables`. Raw writes and raw DELETE each need an explicit flag. |
| Secrets in CI logs, repository files or user-written text reach the model provider | `redact_secrets` masks token formats, private keys, bearer headers and basic-auth URLs in job logs, file contents, diffs, code-search snippets, issue and MR descriptions, comments and commit messages, plus `password=`-style assignments in job logs. Raw `gitlab_api` responses are not scanned. It is pattern-based and not exhaustive. |
| Large responses exhaust context or budget | Caps on list sizes, diff bytes, file bytes and log lines; truncation is reported. |
| Staged writes are tampered with or run twice | Files are created `0600` with `O_EXCL`; runs claim the file under a cross-process lock; a staged write is bound to its GitLab URL, expires, and is re-gated and re-checked when run. |
| Audit sink credentials end up in `config.yaml` | Header values take `${VAR}` placeholders resolved from the environment at send time. |
| Registration performs network I/O | It does not; Hermes' Plugin Doctor verifies this. |

## What the plugin does not do

- It does not verify that the model asked the user before a write in `full` mode. That is the
  skill's instruction; the lock is `operator_only`.
- It does not enforce GitLab permissions. Protected branches, approval rules and roles are GitLab's
  job; scope the token accordingly. A `read_api` token turns this plugin into a read-only integration.
- It does not sandbox itself from Hermes. Plugins run as in-process Python with the same privileges
  as the agent.
- It does not encrypt `plugin-data`. Staged writes hold payloads (comment bodies, file contents)
  and the audit stream holds the user's triggering messages; protect that directory like the token.
- It does not redact raw `gitlab_api` responses, and its redaction is pattern-based: a secret in an
  unusual format passes through.

## Recommended deployment

1. A dedicated bot user with the minimum role, or a project/group access token, never an
   administrator's personal token. `/gitlab status` warns when the token belongs to an admin.
2. `read_api` until writes are needed; `api` after.
3. `write_projects` set to the projects the agent should change.
4. `operator_only` on any gateway shared with other people, and anywhere cron jobs run.
5. `allow_merge` off unless merging is the point, and then with GitLab approval rules in place.
6. `audit_log_path` pointed at a directory your log shipper watches, or `audit_sinks` configured.
