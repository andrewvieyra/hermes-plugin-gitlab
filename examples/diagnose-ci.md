# Diagnosing a failed pipeline

User: "Why is CI red on feature/login?"

```json
{"tool": "gitlab_pipelines", "args": {"project": "platform/api", "action": "list", "ref": "feature/login", "limit": 1}}
{"tool": "gitlab_pipelines", "args": {"project": "platform/api", "action": "get", "pipeline_id": 4711}}
```

`failed_jobs` names the job(s) that failed without `allow_failure`. Search the log before reading it:

```json
{"tool": "gitlab_pipelines", "args": {"project": "platform/api", "action": "log", "job_id": 90210, "search": "error|fail|exception|denied", "context": 3}}
```

Widen only if needed:

```json
{"tool": "gitlab_pipelines", "args": {"project": "platform/api", "action": "log", "job_id": 90210, "tail_lines": 300}}
```

Token-shaped strings in logs are replaced with `[REDACTED]`; the `redacted` count says how many.

Once the user agrees, retry the job (or the whole pipeline):

```json
{"tool": "gitlab_pipeline_write", "args": {"project": "platform/api", "action": "retry", "job_id": 90210}}
```
