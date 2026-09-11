# Operator staging

With `write_mode: operator_only`, a model write is stored instead of sent. The model sees:

```json
{
  "success": true,
  "staged": true,
  "staged_id": "glw-20260909T193012Z-4f1a",
  "action": "mr.merge",
  "summary": "Merge !42 in platform/api at 9f2c1e7a0b3d",
  "expires_at": "2026-09-10T19:30:12Z",
  "preview": {"method": "PUT", "path": "/api/v4/projects/platform%2Fapi/merge_requests/42/merge", "json": {"sha": "9f2c…", "should_remove_source_branch": true}, "preconditions": [{"kind": "mr_head", "project": "platform/api", "iid": 42, "sha": "9f2c…"}], "requires": ["allow_merge"], "irreversible": true},
  "command": "/gitlab run glw-20260909T193012Z-4f1a",
  "cli": "hermes gitlab run glw-20260909T193012Z-4f1a",
  "note": "write_mode is operator_only: nothing was sent. Tell the user what was staged and give them the command above (/gitlab show <id> inspects it first). Do not retry the tool."
}
```

The operator, in any Hermes session or a shell:

```
/gitlab pending
glw-20260909T193012Z-4f1a  staged  Merge !42 in platform/api at 9f2c1e7a0b3d
    by model via tool on signal for Andrew in dm Andrew at 2026-09-09 12:30:12 PDT

/gitlab show 4f1a
glw-20260909T193012Z-4f1a  staged  Merge !42 in platform/api at 9f2c1e7a0b3d
  PUT /api/v4/projects/platform%2Fapi/merge_requests/42/merge
  requested 2026-09-09 12:30:12 PDT by model via tool on signal for Andrew in dm Andrew; expires 2026-09-10 12:30:12 PDT
  requires mr_head 42 at 9f2c1e7a0b3d
  needs settings allow_merge
  payload: {"params": null, "json": {"sha": "9f2c…", "should_remove_source_branch": true}}

/gitlab run 4f1a
Merge request !42 merged: Hash passwords before comparing [merged] https://gitlab.example.com/platform/api/-/merge_requests/42 merge commit 5d1e0c2b7a9f
```

Running re-applies the gate (so `allow_merge` must still be on) and re-checks the head sha; a branch
that moved while the write waited is reported as a conflict and nothing is sent. `/gitlab drop 4f1a`
discards it; expired writes are refused and marked `expired`.
