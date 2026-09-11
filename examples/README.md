# Examples

Ready-to-use tool calls, as the model would make them. Adjust project paths and ids for your
instance. Every write example works with `"dry_run": true` first, which returns the exact payload
without sending it.

| File | What it shows |
|---|---|
| `review-mr.md` | The read sequence for a merge-request review and the comment that follows |
| `diagnose-ci.md` | Finding why a pipeline failed from the job log |
| `commit-and-mr.json` | A `gitlab_commit` payload that creates a branch, then the `gitlab_mr_write` that opens the MR |
| `triage-issue.json` | Labelling, assigning and commenting on an issue in one update plus a comment |
| `operator-staging.md` | What the model and the operator each see in `write_mode: operator_only` |

Try the slash command inside a Hermes session first:

```
/gitlab status
/gitlab mr platform/api 42
```
