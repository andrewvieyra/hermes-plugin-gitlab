# Audit trail

Every write, refusal, rejection, conflict, preview and staged-write action is recorded in an
append-only stream, `audit.jsonl`. Staged writes are also files whose state changes as they progress.
The stream is what a SIEM or log shipper should read.

All timestamps are UTC in ISO 8601 with a `Z` suffix. Nothing in either output contains the GitLab
token.

## Where

| Output | Location | Setting |
|---|---|---|
| Staged writes | `<HERMES_HOME>/plugin-data/gitlab/staged/<id>.json` | `max_staged_age_hours`, `staged_retention_days` |
| Event stream | `<HERMES_HOME>/plugin-data/gitlab/audit.jsonl` | `audit_log` (on/off), `audit_log_path` (absolute path override), `audit_reads` |

Set `audit_log_path` to a location your collector already watches, for example
`/var/log/hermes/gitlab-audit.jsonl`. The writer opens the file in append mode for every event, so
`logrotate` with `copytruncate` is safe, and several Hermes processes (gateway and CLI) can append to
the same file without coordination. `/gitlab audit [N]` shows the last N events in a session.

## The actor record

Attached to every event and stored in staged writes under `requested_by` and `run.actor`. Keys are
always present; unknown values are `null` so field mappings stay stable.

| Field | Meaning |
|---|---|
| `kind` | `model` when the model called a tool, `operator` when a human used `/gitlab` or `hermes gitlab` |
| `via` | `tool`, `slash` or `cli` |
| `platform` | Gateway platform: `signal`, `telegram`, `discord`, `bluebubbles`, `cli`, … |
| `source` | Hermes session source (`gateway`, `cli`, `tui`, `desktop`, `api_server`, …) |
| `profile` | Hermes profile, when multiplexing |
| `chat_id`, `chat_name`, `chat_type`, `thread_id`, `scope_id` | Where the conversation happened; `chat_type` is `dm`, `group`, `channel` or `thread` |
| `user_id`, `user_id_alt`, `user_name` | The platform user who sent the message |
| `message_id` | The triggering message, where the platform exposes one |
| `session_key`, `session_id`, `task_id` | Hermes session identifiers, for joining with Hermes' own logs |
| `cron` | `true` when the turn ran inside a Hermes cron job |
| `os_user` | The OS account running Hermes |
| `request` | The user's message that led to the call, secret-redacted (when `redact_secrets` is on) and truncated to 500 characters; `null` when `audit_include_request` is off |

Identity comes from Hermes' per-session context, which the gateway binds for every turn. In a plain
CLI session most platform fields are `null` and `os_user` is the meaningful identity.

## Events

One JSON object per line. Common fields on every event:

| Field | Meaning |
|---|---|
| `ts` | UTC timestamp |
| `schema` | Event schema version, currently `1` |
| `plugin` | `gitlab` |
| `event` | Event name, below |
| `gitlab_url` | The GitLab instance |
| `action` | What was attempted: `issue.create`, `issue.update`, `issue.comment`, `mr.create`, `mr.update`, `mr.comment`, `mr.approve`, `mr.unapprove`, `mr.merge`, `mr.rebase`, `mr.resolve`, `pipeline.run`, `pipeline.retry`, `pipeline.cancel`, `pipeline.play`, `commit.create`, `api.POST` / `api.PUT` / `api.PATCH` / `api.DELETE` / `api.GET`; for reads `<tool>.<action>` such as `repo.file` |
| `project` | Project id or path as given |
| `target` | `{"kind": "merge_request", "iid": 42, "web_url": ...}`, `{"kind": "commit", "branch": ...}`, `{"kind": "raw", "path": ...}`, or `null` |
| `staged_id` | The staged write this event belongs to, or `null` |
| `actor` | The actor record |
| `host`, `pid` | Where the event was produced |

| Event | When | Extra fields |
|---|---|---|
| `write_rejected` | Arguments could not be turned into a request, or a lookup failed; nothing sent | `error`, `field`, `http_status` |
| `write_refused` | The gate stopped it: mode, missing flag, deny-list, allow-list, unknown or stale staged id, or `operator_only` without a store | `reason`, `dry_run`, `write_mode` |
| `write_previewed` | `dry_run` returned the payload | `summary`, `method`, `path` |
| `write_staged` | `operator_only`: the request was stored for a human | `summary`, `expires_at` |
| `write_conflict` | A precondition failed before the call, or GitLab answered 409 | `error`, `precondition` (`kind`, `expected`, `actual`) or `http_status`, `request` |
| `write_failed` | GitLab rejected the call, or a precondition read failed | `error`, `http_status`, `request`, `phase` |
| `write_done` | The write succeeded | `summary`, `http_status`, `request` (`method`, `path`, `status`), `result` (ids and URL of the object), `irreversible` |
| `staged_run` | An operator ran a staged write | `outcome` (`done`, `failed`, `conflict`, `refused`), `summary`, `error`; an interrupted run is `failed` with an error saying the request may or may not have reached GitLab |
| `staged_dropped` | An operator discarded a staged write | `summary` |
| `staged_expired` | A run was attempted after `expires_at` | `expires_at` |
| `staged_pruned` | Retention removed finished staged writes (`staged_id` is null) | `max_age_days`, `count`, `staged_ids` |
| `read_done`, `read_failed` | With `audit_reads`: a read tool call, including operator reads through `/gitlab` and `hermes gitlab` (actor kind `operator`). Non-GET `gitlab_api` calls are `write_*` events only | `error` on failure |

A staged write's lifecycle emits `write_staged`, then on run `write_done` (or `write_conflict`,
`write_failed`, `write_refused`) followed by `staged_run`, all carrying the same `staged_id`.

## Example

```json
{"ts":"2026-09-09T19:35:50Z","schema":1,"plugin":"gitlab","event":"write_done","gitlab_url":"https://gitlab.example.com","action":"mr.merge","project":"platform/api","target":{"kind":"merge_request","iid":42,"web_url":"https://gitlab.example.com/platform/api/-/merge_requests/42","sha":"9f2c1e7a0b3d4c5e6f708192a3b4c5d6e7f80912"},"staged_id":null,"actor":{"kind":"model","via":"tool","platform":"signal","source":"gateway","profile":null,"chat_id":"+15550001111","chat_name":"Andrew","chat_type":"dm","thread_id":null,"scope_id":null,"user_id":"+15550001111","user_id_alt":"9b1c…","user_name":"Andrew","message_id":"1757360110","session_key":"signal:dm:+15550001111","session_id":"7c0e…","task_id":"7c0e…","cron":false,"os_user":"hermes","request":"Merge !42 now that CI is green"},"host":"hermes-01","pid":41233,"summary":"Merge !42 in platform/api at 9f2c1e7a0b3d","http_status":200,"request":{"method":"PUT","path":"/api/v4/projects/platform%2Fapi/merge_requests/42/merge","status":200},"result":{"iid":42,"id":9912,"web_url":"https://gitlab.example.com/platform/api/-/merge_requests/42","sha":"9f2c…"},"irreversible":true}
```

## Ingestion notes

- To push events instead of tailing the file, configure `audit_sinks` (syslog or HTTP). See [sinks.md](sinks.md).
- The stream is newline-delimited JSON. Point Filebeat, Fluent Bit, Vector or the Splunk forwarder at
  the file with a JSON codec; every line is a self-contained record.
- Map `ts` to your timestamp field (`@timestamp` in Elastic).
- Useful searches: `event:write_done AND action:mr.merge` (every merge the agent made),
  `event:write_refused` (attempted writes that were stopped, with the reason),
  `event:write_conflict` (writes stopped because the target moved), `actor.kind:model AND
  actor.chat_type:group AND event:write_*` (a model wrote from a group chat), `actor.cron:true AND
  event:write_done` (unattended writes), `event:staged_run AND outcome:refused` (staged writes that
  no longer pass the gate).
- Join with GitLab's own audit events on `gitlab_url`, `project`, `target.iid` and time to see the
  change from GitLab's point of view; the `request.path` and `result` ids match GitLab's records.
