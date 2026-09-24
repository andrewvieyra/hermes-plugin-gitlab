# Forwarding audit events

The audit file (`audit.jsonl`, see [audit.md](audit.md)) is always written and is the source of
truth. Sinks forward the same events, as they happen, to a syslog server or an HTTP collector for
installs that have no log shipper on the Hermes machine.

## Configuration

`plugins.entries.gitlab.settings.audit_sinks` is a list. Any mix of sinks works, and each event goes
to all of them.

```yaml
plugins:
  entries:
    gitlab:
      settings:
        audit_sinks:
          - type: syslog
            host: 10.0.0.5
            port: 514
            protocol: udp        # udp | tcp | tls
            format: json         # json | cef
            facility: local0     # any standard facility name
            app_name: hermes-gitlab
          - type: http
            url: https://splunk.example.com:8088/services/collector/event
            headers:
              Authorization: "Splunk ${SPLUNK_HEC_TOKEN}"
            format: hec          # json | hec
            timeout: 5
```

`${VAR}` in a header value is replaced from the environment at send time, so the token lives in
`~/.hermes/.env` and never in `config.yaml`.

| Sink option | Meaning |
|---|---|
| `protocol` | `udp` (default), `tcp`, or `tls`. TLS uses the system trust store; `ca_file` points at a private CA, `verify_ssl: false` disables verification for a lab. |
| `framing` | TCP framing: `newline` (default for tcp, what rsyslog, syslog-ng, Graylog and Wazuh accept) or `octet-counting` (RFC 6587, default and required for tls per RFC 5425). |
| `format` | `json`: the audit record as the syslog message body. `cef`: ArcSight Common Event Format, below. |
| `facility` | Syslog facility name, default `local0`. |
| `timeout` | Connect and send timeout in seconds, default 5. |
| `verify_ssl` | For `http` sinks over HTTPS and `tls` syslog. Default true. |
| `sourcetype` | HEC only, default `hermes:gitlab`. |

## Delivery rules

- Events are queued and sent from one background thread. A GitLab operation never waits for a sink.
- Each event is tried three times per sink with a short backoff, then dropped for that sink. The
  file still has it.
- The queue holds 1,000 events. If a sink is down for long enough to fill it, further events are
  dropped for the sinks, never for the file. Failures are logged once per sink every five minutes,
  not once per event.
- On shutdown the plugin waits up to two seconds for the queue to drain.

Sinks are for convenience and near-real-time alerting. If you need guaranteed delivery, ship the
file with Filebeat, Fluent Bit, Vector or a forwarder, which buffer to disk.

## Syslog details

Messages are RFC 5424:

```
<133>1 2026-09-09T19:35:50Z hermes-01 hermes-gitlab 4242 write_done - {"ts":"2026-09-09T19:35:50Z","event":"write_done",…}
```

`PRI` is facility × 8 + severity. `MSGID` is the event name. The message body is the full JSON
record, so a receiver that parses JSON gets every field.

Severity mapping:

| Severity | Events |
|---|---|
| warning (4) | `write_refused`, `write_rejected`, `write_failed`, `write_conflict`, `read_failed`, `staged_expired`, `staged_run` with an outcome other than `done` |
| notice (5) | `write_done`, `write_staged`, `staged_run`, `staged_dropped`, `staged_pruned` |
| informational (6) | everything else (`write_previewed`, `read_done`) |

## CEF

With `format: cef` the body is one Common Event Format line, which ArcSight, Splunk, QRadar,
Sentinel, Graylog and Wazuh parse natively:

```
CEF:0|andrewvieyra|hermes-plugin-gitlab|0.5.0|write_refused|write refused|7|rt=1757446550000 dvchost=hermes-01 dvcpid=4242 request=https://gitlab.example.com suser=Andrew cs1Label=staged_id cs2Label=actor_kind cs2=model cs3Label=via cs3=tool cs4Label=platform cs4=signal cs5Label=chat cs5=Andrew cs6Label=project cs6=platform/api act=mr.merge externalId=42 cs7Label=web_url cs7=https://… msg=mr.merge refused: allow_merge is off…
```

| CEF key | Source |
|---|---|
| `rt` | event time, epoch milliseconds |
| `dvchost`, `dvcpid` | host and pid |
| `request` | `gitlab_url` |
| `suser` | actor user name or id |
| `act` | `action` (`mr.merge`, `commit.create`, …) |
| `externalId` | target iid or id |
| `cn1` | HTTP status |
| `outcome` | staged-run outcome |
| `msg` | error, reason or summary |
| `cs1` … `cs8` | staged id, actor kind, via, platform, chat, project, target web URL, request text |

CEF severity is 7 for warnings, 4 for notices, 2 for informational.

## HTTP details

`format: json` posts the record as the body. `format: hec` wraps it for Splunk's HTTP Event
Collector: `{"time": <epoch>, "sourcetype": "hermes:gitlab", "host": …, "event": <record>}`. Any
collector that accepts a JSON POST with a static header works with `format: json`.

## Receiver examples

- **rsyslog**: `module(load="imudp") input(type="imudp" port="514")`, or `imtcp` for TCP. Store the
  raw line and let your search tool parse the JSON body.
- **Graylog**: a Syslog UDP or TCP input; enable "Store full message". Add a JSON extractor on the
  `message` field.
- **Wazuh**: a `<remote>` syslog block on the manager; then a decoder for `hermes-gitlab`.
- **Splunk HEC**: create an HEC token with sourcetype `hermes:gitlab`; use `format: hec`.
- **Elastic**: point Filebeat at the file instead; it maps `ts` to `@timestamp` with one processor.

TLS syslog is implemented with the standard library's certificate verification. It is not
covered by an automated test; verify it against your receiver before relying on it.
