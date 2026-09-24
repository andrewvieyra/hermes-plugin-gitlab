import json
import unittest

from .base import PluginTestCase
from .fake_gitlab import _Response


class Issues(PluginTestCase):
    def test_create_resolves_names_and_audits(self):
        out = self.call_kw(
            "gitlab_issue_write",
            {
                "project": "platform/api",
                "action": "create",
                "title": "Rotate keys",
                "description": "soon",
                "labels": ["ops"],
                "assignees": ["andrew"],
                "milestone": "v1.0",
                "due_date": "2026-10-01",
            },
            task_id="t-1",
            user_task="Open an issue to rotate keys",
        )
        self.assertTrue(out["success"], out)
        self.assertEqual(out["action"], "issue.create")
        self.assertEqual(out["result"]["iid"], 4)
        self.assertEqual(out["result"]["assignees"], ["andrew"])
        self.assertEqual(out["result"]["milestone"], "v1.0")
        self.assertIn("Issue #4 created", out["report"])
        sent = self.gl.writes()[-1]["json"]
        self.assertEqual(sent["assignee_ids"], [2])
        self.assertEqual(sent["milestone_id"], 5)
        self.assertEqual(sent["labels"], "ops")
        done = self.events("write_done")[-1]
        self.assertEqual(done["actor"]["kind"], "model")
        self.assertEqual(done["actor"]["task_id"], "t-1")
        self.assertEqual(done["actor"]["request"], "Open an issue to rotate keys")
        self.assertEqual(done["project"], "platform/api")
        self.assertEqual(done["result"]["iid"], 4)
        self.assertEqual(done["http_status"], 201)

    def test_rejections(self):
        out = self.call("gitlab_issue_write", project="platform/api", action="create")
        self.assertTrue(out["rejected"])
        self.assertEqual(out["field"], "title")
        self.assertEqual(self.events("write_rejected")[-1]["action"], "issue.create")
        self.assertIn(
            "no user with username",
            self.call("gitlab_issue_write", project=1, action="create", title="x", assignees=["ghost"])["error"],
        )
        self.assertIn(
            "milestone", self.call("gitlab_issue_write", project=1, action="create", title="x", milestone="v9")["error"]
        )
        self.assertIn(
            "YYYY-MM-DD",
            self.call("gitlab_issue_write", project=1, action="create", title="x", due_date="tomorrow")["error"],
        )
        self.assertIn("nothing to update", self.call("gitlab_issue_write", project=1, action="update", iid=1)["error"])
        self.assertIn("action must be", self.call("gitlab_issue_write", project=1, action="delete", iid=1)["error"])
        self.assertEqual(self.gl.writes(), [])

    def test_update_close_and_labels(self):
        out = self.call(
            "gitlab_issue_write",
            project="platform/api",
            action="update",
            iid=1,
            state="close",
            add_labels=["wontfix"],
            remove_labels=["bug"],
        )
        self.assertTrue(out["success"])
        self.assertEqual(out["result"]["state"], "closed")
        self.assertEqual(self.gl.issues[1][1]["labels"], ["backend", "wontfix"])
        self.assertEqual(self.gl.writes()[-1]["json"]["state_event"], "close")
        out = self.call("gitlab_issue_write", project=1, action="update", iid=1, state="reopen", assignees=[])
        self.assertEqual(out["result"]["state"], "opened")
        self.assertEqual(self.gl.writes()[-1]["json"]["assignee_ids"], [0])

    def test_comment_reply_and_internal(self):
        out = self.call(
            "gitlab_issue_write", project="platform/api", action="comment", iid=1, body="On it.", internal=True
        )
        self.assertTrue(out["success"])
        self.assertTrue(self.gl.writes()[-1]["json"]["internal"])
        self.assertIn("#note_", out["report"])
        did = self.gl.discussions[(1, "issues", 1)][0]["id"]
        out = self.call("gitlab_issue_write", project=1, action="comment", iid=1, body="Reply", discussion_id=did)
        self.assertTrue(out["success"])
        self.assertEqual(self.gl.writes()[-1]["path"], f"projects/1/issues/1/discussions/{did}/notes")
        self.assertEqual(len(self.gl.discussions[(1, "issues", 1)][0]["notes"]), 3)

    def test_dry_run_sends_nothing(self):
        out = self.call("gitlab_issue_write", project="platform/api", action="create", title="Preview", dry_run=True)
        self.assertTrue(out["success"])
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["preview"]["method"], "POST")
        self.assertEqual(out["preview"]["path"], "/api/v4/projects/platform%2Fapi/issues")
        self.assertEqual(out["preview"]["json"]["title"], "Preview")
        self.assertEqual(self.gl.writes(), [])
        self.assertEqual(self.events("write_previewed")[-1]["action"], "issue.create")


class MergeRequests(PluginTestCase):
    def test_create_defaults_target_and_draft(self):
        out = self.call(
            "gitlab_mr_write",
            project="platform/api",
            action="create",
            source_branch="old-feature",
            title="Revive",
            draft=True,
            reviewers=["andrew", "dana"],
            remove_source_branch=True,
        )
        self.assertTrue(out["success"], out)
        self.assertEqual(out["result"]["target_branch"], "main")
        self.assertEqual(out["result"]["title"], "Draft: Revive")
        self.assertTrue(out["result"]["draft"])
        self.assertEqual(out["result"]["reviewers"], ["andrew", "dana"])
        self.assertTrue(self.gl.writes()[-1]["json"]["remove_source_branch"])
        out = self.call("gitlab_mr_write", project=1, action="create", source_branch="ghost", title="x")
        self.assertFalse(out["success"])
        self.assertIn("Source branch not found", out["error"])
        self.assertEqual(self.events("write_failed")[-1]["http_status"], 400)

    def test_update_draft_toggle_needs_current_title(self):
        out = self.call("gitlab_mr_write", project="platform/api", action="update", iid=12, draft=False)
        self.assertTrue(out["success"])
        self.assertEqual(out["result"]["title"], "Experimental cache")
        self.assertFalse(self.gl.mrs[1][12]["draft"])
        out = self.call("gitlab_mr_write", project=1, action="update", iid=10, state="close", add_labels=["stale"])
        self.assertEqual(out["result"]["state"], "closed")

    def test_comments(self):
        out = self.call("gitlab_mr_write", project="platform/api", action="comment", iid=10, body="LGTM overall")
        self.assertTrue(out["success"])
        self.assertEqual(self.gl.writes()[-1]["path"], "projects/platform%2Fapi/merge_requests/10/notes")
        did = self.gl.discussions[(1, "merge_requests", 10)][0]["id"]
        out = self.call("gitlab_mr_write", project=1, action="comment", iid=10, body="Agreed", discussion_id=did)
        self.assertTrue(out["success"])
        self.assertEqual(self.gl.discussions[(1, "merge_requests", 10)][0]["notes"][-1]["body"], "Agreed")

    def test_diff_comment_fills_shas_and_binds_to_head(self):
        out = self.call(
            "gitlab_mr_write",
            project="platform/api",
            action="comment",
            iid=10,
            body="Hash this",
            position={"new_path": "src/login.py", "new_line": 2},
        )
        self.assertTrue(out["success"], out)
        sent = self.gl.writes()[-1]["json"]["position"]
        self.assertEqual(sent["head_sha"], self.gl.mrs[1][10]["sha"])
        self.assertEqual(sent["base_sha"], self.gl.mrs[1][10]["diff_refs"]["base_sha"])
        self.assertEqual(sent["old_path"], "src/login.py")
        self.assertEqual(out["result"]["notes"][0]["position"]["new_line"], 2)
        # the head moves between the builder's read and the executor's precondition check -> conflict, nothing sent
        real = dict(self.gl.mrs[1][10])
        reads = {"n": 0}

        def moving_head(method, path, params, body):
            if method == "GET" and path == "projects/1/merge_requests/10":
                reads["n"] += 1
                if reads["n"] == 2:
                    return _Response(200, dict(real, sha="f" * 40))
            return None

        self.gl.fail_on = moving_head
        writes_before = len(self.gl.writes())
        out = self.call(
            "gitlab_mr_write",
            project=1,
            action="comment",
            iid=10,
            body="x",
            position={"new_path": "src/login.py", "new_line": 2},
        )
        self.assertFalse(out["success"])
        self.assertTrue(out["conflict"])
        self.assertEqual(out["precondition"]["kind"], "mr_head")
        self.assertEqual(len(self.gl.writes()), writes_before)
        self.gl.fail_on = None
        self.assertIn(
            "position needs a line",
            self.call("gitlab_mr_write", project=1, action="comment", iid=10, body="x", position={"new_path": "a"})[
                "error"
            ],
        )

    def test_diff_comment_on_context_and_removed_lines(self):
        def comment(**position):
            return self.call("gitlab_mr_write", project=1, action="comment", iid=10, body="x", position=position)

        def sent_position():
            return self.gl.writes()[-1]["json"]["position"]

        # an unchanged line needs both numbers: the builder fills in old_line from the diff
        out = comment(new_path="src/app.py", new_line=4)
        self.assertTrue(out["success"], out)
        self.assertEqual((sent_position()["old_line"], sent_position()["new_line"]), (3, 4))
        # the same kind of line named by its old number
        out = comment(new_path="src/app.py", old_line=1)
        self.assertTrue(out["success"], out)
        self.assertEqual((sent_position()["old_line"], sent_position()["new_line"]), (1, 1))
        # an added line gets new_line only
        out = comment(new_path="src/app.py", new_line=2)
        self.assertTrue(out["success"], out)
        self.assertEqual(sent_position()["new_line"], 2)
        self.assertIsNone(sent_position().get("old_line"))
        # a removed line gets old_line only
        self.gl.mr_diffs[(1, 10)].append(
            dict(
                self.gl.mr_diffs[(1, 10)][0],
                old_path="docs/old.md",
                new_path="docs/old.md",
                diff="@@ -1,3 +1,2 @@\n a\n-b\n c\n",
            )
        )
        out = comment(new_path="docs/old.md", old_line=2)
        self.assertTrue(out["success"], out)
        self.assertEqual(sent_position()["old_line"], 2)
        self.assertIsNone(sent_position().get("new_line"))
        # lines outside a hunk, mismatched pairs and untouched files are rejected before anything is sent
        writes_before = len(self.gl.writes())
        for position, message in (
            ({"new_path": "src/app.py", "new_line": 99}, "is not in the diff of !10"),
            ({"new_path": "src/app.py", "new_line": 4, "old_line": 2}, "is old line 3, not 2"),
            ({"new_path": "README.md", "new_line": 1}, "is not changed in !10"),
        ):
            out = comment(**position)
            self.assertFalse(out["success"], out)
            self.assertIn(message, out["error"])
        self.assertEqual(len(self.gl.writes()), writes_before)

    def test_approve_with_and_without_sha(self):
        sha = self.gl.mrs[1][10]["sha"]
        out = self.call("gitlab_mr_write", project="platform/api", action="approve", iid=10, sha=sha)
        self.assertTrue(out["success"], out)
        self.assertIn("0 approval(s) still required", out["report"])
        out = self.call("gitlab_mr_write", project=1, action="unapprove", iid=10)
        self.assertTrue(out["success"])
        out = self.call("gitlab_mr_write", project=1, action="approve", iid=10, sha="0" * 40)
        self.assertFalse(out["success"])
        self.assertTrue(out["conflict"])
        self.assertIn("changed since it was read", out["error"])
        self.assertEqual(self.events("write_conflict")[-1]["precondition"]["kind"], "mr_head")
        out = self.call("gitlab_mr_write", project=1, action="approve", iid=10, dry_run=True)
        self.assertIn("No sha given", out["preview"]["notes"][0])

    def test_merge_gate_sha_and_conflicts(self):
        sha = self.gl.mrs[1][10]["sha"]
        out = self.call("gitlab_mr_write", project="platform/api", action="merge", iid=10, sha=sha)
        self.assertFalse(out["success"])
        self.assertTrue(out["refused"])
        self.assertIn("allow_merge", out["error"])
        self.assertEqual(self.gl.writes(), [])
        self.assertEqual(self.events("write_refused")[-1]["action"], "mr.merge")
        self.configure(allow_merge=True)
        out = self.call("gitlab_mr_write", project=1, action="merge", iid=10)
        self.assertTrue(out["rejected"])
        self.assertEqual(out["field"], "sha")
        out = self.call(
            "gitlab_mr_write", project=1, action="merge", iid=10, sha=sha[:8], should_remove_source_branch=True
        )
        self.assertTrue(out["success"], out)
        self.assertEqual(self.gl.writes()[-1]["json"]["sha"], sha)  # a short sha is expanded to the full head
        self.assertEqual(out["result"]["state"], "merged")
        self.assertIn("merge commit", out["report"])
        self.assertTrue(self.events("write_done")[-1]["irreversible"])
        self.assertNotIn("feature/login", self.gl.branches[1])
        out = self.call("gitlab_mr_write", project=1, action="merge", iid=10, sha=sha)
        self.assertTrue(out["conflict"])
        self.assertIn("is merged, not open", out["error"])

    def test_merge_409_from_gitlab_is_a_conflict(self):
        self.configure(allow_merge=True)
        sha = self.gl.mrs[1][10]["sha"]

        def race(method, path, params, body):
            if method == "PUT" and path.endswith("/merge"):
                return _Response(409, {"message": "409 Conflict: SHA does not match HEAD of source branch"})
            return None

        self.gl.fail_on = race
        out = self.call("gitlab_mr_write", project=1, action="merge", iid=10, sha=sha)
        self.assertTrue(out["conflict"])
        self.assertEqual(out["gitlab"]["status"], 409)
        self.assertEqual(self.events("write_conflict")[-1]["http_status"], 409)

    def test_merge_when_pipeline_succeeds_sends_both_names(self):
        self.configure(allow_merge=True)
        out = self.call(
            "gitlab_mr_write",
            project=1,
            action="merge",
            iid=10,
            sha=self.gl.mrs[1][10]["sha"],
            merge_when_pipeline_succeeds=True,
            dry_run=True,
        )
        self.assertTrue(out["preview"]["json"]["auto_merge"])
        self.assertTrue(out["preview"]["json"]["merge_when_pipeline_succeeds"])
        self.assertTrue(out["preview"]["irreversible"])

    def test_rebase_and_resolve(self):
        out = self.call("gitlab_mr_write", project=1, action="rebase", iid=10, skip_ci=True)
        self.assertTrue(out["success"])
        self.assertTrue(out["result"]["rebase_in_progress"])
        self.assertEqual(self.gl.writes()[-1]["params"], {"skip_ci": True})
        did = self.gl.discussions[(1, "merge_requests", 10)][0]["id"]
        out = self.call("gitlab_mr_write", project=1, action="resolve", iid=10, discussion_id=did)
        self.assertTrue(out["success"])
        self.assertTrue(out["result"]["resolved"])
        self.assertIn(
            "discussion_id is required", self.call("gitlab_mr_write", project=1, action="resolve", iid=10)["error"]
        )


class Pipelines(PluginTestCase):
    def failed_id(self):
        return next(p["id"] for p in self.gl.pipelines[1].values() if p["status"] == "failed")

    def test_run_with_variables(self):
        out = self.call(
            "gitlab_pipeline_write",
            project="platform/api",
            action="run",
            ref="main",
            variables={"DEPLOY_ENV": "staging", "DRY": True},
        )
        self.assertTrue(out["success"], out)
        self.assertEqual(out["result"]["status"], "pending")
        self.assertEqual(
            self.gl.writes()[-1]["json"]["variables"],
            [{"key": "DEPLOY_ENV", "value": "staging"}, {"key": "DRY", "value": "True"}],
        )
        self.assertIn(
            "invalid key",
            self.call("gitlab_pipeline_write", project=1, action="run", ref="main", variables={"bad key": "x"})[
                "error"
            ],
        )
        out = self.call("gitlab_pipeline_write", project=1, action="run", ref="ghost")
        self.assertFalse(out["success"])
        self.assertIn("Reference not found", out["error"])

    def test_retry_cancel_play(self):
        pid = self.failed_id()
        test_job = next(j["id"] for j in self.gl.jobs[1].values() if j["name"] == "test")
        manual = next(j["id"] for j in self.gl.jobs[1].values() if j["name"] == "deploy")
        out = self.call("gitlab_pipeline_write", project=1, action="retry", job_id=test_job)
        self.assertTrue(out["success"])
        self.assertEqual(out["result"]["status"], "pending")
        out = self.call("gitlab_pipeline_write", project=1, action="cancel", pipeline_id=pid)
        self.assertEqual(out["result"]["status"], "canceled")
        out = self.call("gitlab_pipeline_write", project=1, action="play", job_id=manual)
        self.assertTrue(out["success"])
        self.assertIn(
            "exactly one of",
            self.call("gitlab_pipeline_write", project=1, action="retry", pipeline_id=pid, job_id=test_job)["error"],
        )
        self.assertIn("job_id", self.call("gitlab_pipeline_write", project=1, action="play")["error"])


class Commits(PluginTestCase):
    def test_commit_to_existing_branch_with_precondition(self):
        head = self.gl.head(1, "feature/login")
        out = self.call(
            "gitlab_commit",
            project="platform/api",
            branch="feature/login",
            commit_message="Hash passwords\n\nUse bcrypt.",
            expected_head_sha=head,
            actions=[
                {"action": "update", "file_path": "src/login.py", "content": "import bcrypt\n"},
                {"action": "create", "file_path": "src/hash.py", "content": "x = 1\n"},
            ],
        )
        self.assertTrue(out["success"], out)
        self.assertEqual(out["result"]["title"], "Hash passwords")
        self.assertEqual(self.gl.files[(1, "feature/login")]["src/hash.py"], "x = 1\n")
        self.assertNotEqual(self.gl.head(1, "feature/login"), head)
        self.assertEqual(self.events("write_done")[-1]["result"]["sha"], self.gl.head(1, "feature/login"))
        out = self.call(
            "gitlab_commit",
            project=1,
            branch="feature/login",
            commit_message="stale",
            expected_head_sha=head,
            actions=[{"action": "delete", "file_path": "src/hash.py"}],
        )
        self.assertTrue(out["conflict"])
        self.assertIn("branch feature/login changed", out["error"])
        self.assertIn("src/hash.py", self.gl.files[(1, "feature/login")])

    def test_new_branch_needs_start_branch(self):
        out = self.call(
            "gitlab_commit",
            project=1,
            branch="fix/typo",
            commit_message="Fix typo",
            actions=[{"action": "update", "file_path": "README.md", "content": "# API\n"}],
        )
        self.assertTrue(out["rejected"])
        self.assertIn("start_branch", out["error"])
        out = self.call(
            "gitlab_commit",
            project=1,
            branch="fix/typo",
            start_branch="main",
            commit_message="Fix typo",
            expected_head_sha=self.gl.head(1, "main"),
            actions=[{"action": "update", "file_path": "README.md", "content": "# API\n"}],
        )
        self.assertTrue(out["success"], out)
        self.assertIn("fix/typo", self.gl.branches[1])
        self.assertEqual(self.gl.files[(1, "fix/typo")]["README.md"], "# API\n")
        self.assertEqual(self.gl.writes()[-1]["json"]["start_branch"], "main")
        self.assertEqual(self.gl.files[(1, "main")]["README.md"], "# api\n\nThe API service.\n\nGET /health\n")

    def test_gitlab_rejects_bad_actions(self):
        out = self.call(
            "gitlab_commit",
            project=1,
            branch="main",
            commit_message="x",
            actions=[{"action": "update", "file_path": "nope.py", "content": ""}],
        )
        self.assertFalse(out["success"])
        self.assertIn("doesn't exist", out["error"])
        self.assertEqual(self.events("write_failed")[-1]["http_status"], 400)
        for bad in (
            [{"action": "create", "file_path": "../x", "content": ""}],
            [{"action": "create", "file_path": "a.py"}],
            [{"action": "move", "file_path": "a.py"}],
            [{"action": "chmod", "file_path": "a.py"}],
            [{"action": "create", "file_path": "a.py", "content": "", "encoding": "hex"}],
            [{"action": "explode", "file_path": "a.py"}],
            "not a list",
            [],
        ):
            self.assertTrue(
                self.call("gitlab_commit", project=1, branch="main", commit_message="x", actions=bad).get("rejected"),
                bad,
            )

    def test_dry_run_notes(self):
        out = self.call(
            "gitlab_commit",
            project=1,
            branch="feature/login",
            commit_message="x",
            actions=[{"action": "delete", "file_path": "src/login.py"}],
            dry_run=True,
        )
        self.assertTrue(out["dry_run"])
        self.assertTrue(any("No expected_head_sha" in n for n in out["preview"]["notes"]))
        self.assertEqual(self.gl.writes(), [])


class RawWrites(PluginTestCase):
    def test_raw_writes_need_flags(self):
        out = self.call("gitlab_api", method="POST", path="projects/1/labels", body={"name": "ops", "color": "#123456"})
        self.assertTrue(out["refused"])
        self.assertIn("allow_raw_writes", out["error"])
        self.configure(allow_raw_writes=True)
        out = self.call("gitlab_api", method="POST", path="projects/1/labels", body={"name": "ops", "color": "#123456"})
        self.assertTrue(out["success"], out)
        self.assertEqual(out["result"]["name"], "ops")
        self.assertEqual(self.events("write_done")[-1]["action"], "api.POST")
        out = self.call("gitlab_api", method="DELETE", path="projects/1/labels/ops")
        self.assertTrue(out["refused"])
        self.assertIn("allow_raw_delete", out["error"])
        self.configure(allow_raw_writes=True, allow_raw_delete=True)
        self.assertTrue(self.call("gitlab_api", method="DELETE", path="projects/1/labels/ops")["success"])

    def test_raw_writes_are_not_audited_as_reads(self):
        self.configure(allow_raw_writes=True, audit_reads=True)
        out = self.call("gitlab_api", method="POST", path="projects/1/labels", body={"name": "ops", "color": "#123456"})
        self.assertTrue(out["success"], out)
        self.assertEqual([e["event"] for e in self.events()], ["write_done"])
        self.assertTrue(self.call("gitlab_api", path="projects/1/labels")["success"])
        self.assertEqual([e["event"] for e in self.events()], ["write_done", "read_done"])

    def test_encoded_and_cased_admin_paths_are_refused(self):
        self.configure(allow_raw_writes=True, allow_raw_delete=True)
        before = len(self.gl.writes())
        for method, path, body in (
            ("POST", "projects/1/%6Dembers", {"user_id": 2}),
            ("PUT", "PROJECTS/1", {"name": "x"}),
            ("DELETE", "projects/1/%2568ooks/3", None),
        ):
            out = self.call("gitlab_api", method=method, path=path, body=body)
            self.assertTrue(out.get("refused"), (method, path, out))
        self.assertEqual(len(self.gl.writes()), before)

    def test_admin_and_sensitive_surfaces_stay_closed(self):
        self.configure(allow_raw_writes=True, allow_raw_delete=True)
        before = len(self.gl.writes())
        for method, path in (
            ("PUT", "projects/1"),
            ("DELETE", "projects/1"),
            ("POST", "projects/1/members"),
            ("PUT", "application/settings"),
            ("POST", "projects/1/variables"),
            ("POST", "projects/1/hooks"),
            ("DELETE", "groups/platform"),
            ("POST", "users"),
            ("POST", "projects/1/protected_branches"),
        ):
            out = self.call("gitlab_api", method=method, path=path, body={"x": 1})
            self.assertTrue(out.get("refused"), (method, path, out))
        self.assertEqual(len(self.gl.writes()), before)
        self.assertTrue(
            self.call(
                "gitlab_api",
                method="POST",
                path="projects/1/repository/branches",
                params={"branch": "b2", "ref": "main"},
                dry_run=True,
            )["success"]
        )

    def test_body_and_method_validation(self):
        self.configure(allow_raw_writes=True)
        self.assertIn(
            "body must be", self.call("gitlab_api", method="POST", path="projects/1/labels", body="x")["error"]
        )
        self.assertIn("method must be", self.call("gitlab_api", method="PATCHY", path="projects/1/labels")["error"])


class Allowlist(PluginTestCase):
    def test_write_projects_glob(self):
        self.configure(write_projects=["andrew/*"])
        out = self.call("gitlab_issue_write", project="platform/api", action="create", title="x")
        self.assertTrue(out["refused"])
        self.assertIn("write_projects", out["error"])
        self.assertEqual(self.gl.writes(), [])
        out = self.call("gitlab_issue_write", project="andrew/sandbox", action="create", title="x")
        self.assertTrue(out["success"], out)
        out = self.call("gitlab_issue_write", project=3, action="comment", iid=1, body="hi")  # numeric id is resolved
        self.assertTrue(out["success"], out)
        out = self.call("gitlab_issue_write", project=1, action="comment", iid=1, body="hi")
        self.assertTrue(out["refused"])
        self.configure(allow_raw_writes=True, write_projects=["andrew/*"])
        self.assertTrue(
            self.call("gitlab_api", method="POST", path="projects/platform%2Fapi/labels", body={"name": "x"})["refused"]
        )
        self.assertTrue(self.call("gitlab_api", method="POST", path="projects/3/labels", body={"name": "x"})["success"])

    def test_reads_are_not_restricted(self):
        self.configure(write_projects=["andrew/*"])
        self.assertTrue(self.call("gitlab_issues", project="platform/api")["success"])


class TokenHygiene(PluginTestCase):
    def test_token_never_appears_in_outputs_or_audit(self):
        token = self.gl.token
        outputs = [
            self.handlers.HANDLERS["gitlab_api"]({"path": "status"}),
            self.handlers.HANDLERS["gitlab_issue_write"]({"project": 1, "action": "create", "title": "t"}),
            self.handlers.HANDLERS["gitlab_repo"]({"project": 1, "action": "file", "path": "src/app.py"}),
            self.handlers.HANDLERS["gitlab_api"]({"path": "projects/1/variables"}),
        ]
        self.gl.fail_on = lambda m, p, q, b: _Response(500, {"message": f"leak {token}"}) if p == "version" else None
        outputs.append(self.handlers.HANDLERS["gitlab_api"]({"path": "status"}))
        for out in outputs:
            self.assertNotIn(token, out)
        self.assertNotIn(token, self.audit_path.read_text())
        self.assertNotIn(token, json.dumps(self.store.list(limit=10)))


class BooleanArguments(PluginTestCase):
    def test_empty_means_not_given_and_junk_is_rejected(self):
        to_bool = self.targets.to_bool
        self.assertIsNone(to_bool("", "confidential"))
        self.assertIsNone(to_bool(None, "confidential"))
        self.assertFalse(to_bool("", "confidential", default=False))
        self.assertTrue(to_bool(" YES ", "confidential"))
        self.assertFalse(to_bool("off", "confidential"))
        self.assertFalse(to_bool(0, "confidential"))
        with self.assertRaises(self.client_mod.GitLabError):
            to_bool("nope", "confidential")
        out = self.call("gitlab_issue_write", project=1, action="create", title="Plain", confidential="")
        self.assertTrue(out["success"], out)
        self.assertIsNot(self.gl.writes()[-1]["json"].get("confidential"), True)
        writes_before = len(self.gl.writes())
        out = self.call("gitlab_issue_write", project=1, action="create", title="Junk", confidential="nope")
        self.assertFalse(out["success"], out)
        self.assertIn("confidential must be true or false", out["error"])
        self.assertEqual(len(self.gl.writes()), writes_before)
        out = self.call("gitlab_issues", project=1, confidential="")
        self.assertTrue(out["success"], out)
        self.assertIsNone(self.gl.calls[-1]["params"].get("confidential"))


class DiscussionIds(PluginTestCase):
    def test_discussion_ids_are_validated_before_use(self):
        # the id is interpolated into a URL path: `../merge?sha=…` on resolve must never become a merge
        head = self.gl.mrs[1][10]["sha"]
        before = len(self.gl.writes())
        for bad in (f"../merge?sha={head}", "x/../../hooks", "abc#frag", "..", "a b", "%2e%2e/merge", "id;x"):
            for tool, args in (
                ("gitlab_mr_write", {"action": "resolve", "iid": 10, "discussion_id": bad}),
                ("gitlab_mr_write", {"action": "comment", "iid": 10, "body": "x", "discussion_id": bad}),
                ("gitlab_issue_write", {"action": "comment", "iid": 1, "body": "x", "discussion_id": bad}),
            ):
                out = self.call(tool, project=1, **args)
                self.assertTrue(out.get("rejected"), (tool, bad, out))
                self.assertEqual(out["field"], "discussion_id")
        self.assertEqual(len(self.gl.writes()), before)
        self.assertEqual(self.gl.mrs[1][10]["state"], "opened")
        good = self.gl.discussions[(1, "merge_requests", 10)][0]["id"]
        out = self.call("gitlab_mr_write", project=1, action="resolve", iid=10, discussion_id=good)
        self.assertTrue(out["success"], out)
        self.assertEqual(self.events("write_rejected")[0]["field"], "discussion_id")


class DiffPositions(PluginTestCase):
    def test_renamed_file_keeps_its_old_path(self):
        self.gl.mr_diffs[(1, 10)].append(
            dict(
                self.gl.mr_diffs[(1, 10)][0],
                old_path="src/old_name.py",
                new_path="src/new_name.py",
                renamed_file=True,
                new_file=False,
                diff="@@ -1,2 +1,2 @@\n a\n-b\n+c\n",
            )
        )

        def comment(**position):
            out = self.call("gitlab_mr_write", project=1, action="comment", iid=10, body="x", position=position)
            self.assertTrue(out["success"], out)
            return self.gl.writes()[-1]["json"]["position"]

        sent = comment(new_path="src/new_name.py", new_line=2)
        self.assertEqual(
            (sent["old_path"], sent["new_path"], sent["new_line"]), ("src/old_name.py", "src/new_name.py", 2)
        )
        self.assertIsNone(sent.get("old_line"))
        sent = comment(old_path="src/old_name.py", old_line=2)
        self.assertEqual(
            (sent["old_path"], sent["new_path"], sent["old_line"]), ("src/old_name.py", "src/new_name.py", 2)
        )
        self.assertIsNone(sent.get("new_line"))


class AutoMerge(PluginTestCase):
    def test_cancel_auto_merge_needs_no_flag(self):
        out = self.call("gitlab_mr_write", project=1, action="cancel_auto_merge", iid=10, dry_run=True)
        self.assertTrue(out["dry_run"], out)
        self.assertTrue(out["preview"]["path"].endswith("/merge_requests/10/cancel_merge_when_pipeline_succeeds"))
        self.assertEqual(out["preview"]["method"], "POST")
        self.assertEqual(out["preview"]["requires"], [])


class BranchCreation(PluginTestCase):
    def test_commit_without_actions_creates_the_branch(self):
        head = self.gl.head(1, "main")
        out = self.call(
            "gitlab_commit",
            project="platform/api",
            branch="feature/empty",
            start_branch="main",
            expected_head_sha=head,
            dry_run=True,
        )
        self.assertTrue(out["dry_run"], out)
        self.assertEqual(out["preview"]["method"], "POST")
        self.assertTrue(out["preview"]["path"].endswith("/repository/branches"))
        self.assertEqual(out["preview"]["json"], {"branch": "feature/empty", "ref": "main"})
        self.assertEqual(out["preview"]["preconditions"][0]["branch"], "main")
        out = self.call(
            "gitlab_commit", project="platform/api", branch="feature/empty", start_branch="main", expected_head_sha=head
        )
        self.assertTrue(out["success"], out)
        self.assertEqual(out["action"], "branch.create")
        self.assertIn("Branch feature/empty created", out["report"])
        self.assertEqual(out["result"]["commit"]["id"], head)
        done = self.events("write_done")[-1]
        self.assertEqual((done["action"], done["result"]["branch"]), ("branch.create", "feature/empty"))
        writes_before = len(self.gl.writes())
        out = self.call("gitlab_commit", project="platform/api", branch="feature/empty", start_branch="main")
        self.assertTrue(out["rejected"], out)
        self.assertIn("already exists", out["error"])
        self.assertEqual(len(self.gl.writes()), writes_before)
        out = self.call(
            "gitlab_commit",
            project="platform/api",
            branch="feature/empty",
            commit_message="add x",
            actions=[{"action": "create", "file_path": "x.txt", "content": "x\n"}],
            expected_head_sha=head,
        )
        self.assertTrue(out["success"], out)
        self.assertEqual(out["action"], "commit.create")

    def test_branch_only_needs_start_branch_and_a_fresh_head(self):
        out = self.call("gitlab_commit", project="platform/api", branch="feature/x")
        self.assertTrue(out["rejected"], out)
        self.assertIn("start_branch", out["error"])
        out = self.call(
            "gitlab_commit", project="platform/api", branch="feature/x", start_branch="main", expected_head_sha="0" * 40
        )
        self.assertTrue(out["conflict"], out)
        self.assertEqual(out["precondition"]["kind"], "branch_head")
        self.assertNotIn("feature/x", self.gl.branches[1])
        out = self.call("gitlab_commit", project="platform/api", branch="feature/x", actions="junk", commit_message="m")
        self.assertTrue(out["rejected"], out)
        self.assertEqual(out["field"], "actions")


class PipelineInputs(PluginTestCase):
    def test_inputs_are_passed_through(self):
        out = self.call(
            "gitlab_pipeline_write",
            project=1,
            action="run",
            ref="main",
            inputs={"environment": "staging", "replicas": 2},
            dry_run=True,
        )
        self.assertTrue(out["dry_run"], out)
        self.assertEqual(out["preview"]["json"]["inputs"], {"environment": "staging", "replicas": 2})
        self.assertIn("2 input(s)", out["summary"])
        out = self.call("gitlab_pipeline_write", project=1, action="run", ref="main", inputs="bad")
        self.assertTrue(out["rejected"], out)
        self.assertEqual(out["field"], "inputs")


if __name__ == "__main__":
    unittest.main()
