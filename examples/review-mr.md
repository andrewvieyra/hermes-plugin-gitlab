# Reviewing a merge request

User: "Review https://gitlab.example.com/platform/api/-/merge_requests/42 and tell me if it is safe to merge."

```json
{"tool": "gitlab_merge_requests", "args": {"action": "get", "project": "https://gitlab.example.com/platform/api/-/merge_requests/42", "iid": 42}}
{"tool": "gitlab_merge_requests", "args": {"action": "diffs", "project": "platform/api", "iid": 42}}
{"tool": "gitlab_merge_requests", "args": {"action": "discussions", "project": "platform/api", "iid": 42, "only_unresolved": true}}
{"tool": "gitlab_merge_requests", "args": {"action": "pipelines", "project": "platform/api", "iid": 42}}
```

The model reports: what the change does, risks it sees, test coverage, `detailed_merge_status`,
`has_conflicts`, approvals, head pipeline state, unresolved threads. Then it offers to post the review.

After a yes, a general comment:

```json
{"tool": "gitlab_mr_write", "args": {"action": "comment", "project": "platform/api", "iid": 42, "body": "Review summary …"}}
```

or a thread on a diff line (the plugin fills in `base_sha`, `start_sha` and `head_sha` and binds the
note to the head it read):

```json
{"tool": "gitlab_mr_write", "args": {"action": "comment", "project": "platform/api", "iid": 42, "body": "Compare hashes, not plain text.", "position": {"new_path": "src/login.py", "new_line": 2}}}
```

Approving binds to the reviewed revision; merging additionally needs `allow_merge: true`:

```json
{"tool": "gitlab_mr_write", "args": {"action": "approve", "project": "platform/api", "iid": 42, "sha": "9f2c1e7a0b3d4c5e6f708192a3b4c5d6e7f80912"}}
{"tool": "gitlab_mr_write", "args": {"action": "merge", "project": "platform/api", "iid": 42, "sha": "9f2c1e7a0b3d4c5e6f708192a3b4c5d6e7f80912", "should_remove_source_branch": true}}
```
