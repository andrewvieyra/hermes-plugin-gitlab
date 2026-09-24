import json
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from .base import PluginTestCase


class ReadOnly(PluginTestCase):
    def test_write_tools_hidden_and_refused(self):
        # the check_fns decide what Hermes registers: reads need a configured token, writes also need a mode
        env = {"GITLAB_URL": "https://gitlab.test", "GITLAB_TOKEN": "glpat-test-token-1234"}
        with mock.patch.dict(os.environ, env):
            self.configure(write_mode="read_only")
            self.assertTrue(self.handlers.check_requirements())
            self.assertFalse(self.handlers.check_write_requirements())
            self.configure(write_mode="full")
            self.assertTrue(self.handlers.check_write_requirements())
        with mock.patch.dict(os.environ, {"GITLAB_URL": "", "GITLAB_TOKEN": ""}):
            self.assertFalse(self.handlers.check_requirements())
            self.assertFalse(self.handlers.check_write_requirements())
        self.configure(write_mode="read_only")
        out = self.call("gitlab_issue_write", project=1, action="create", title="x")
        self.assertTrue(out["refused"])
        self.assertEqual(out["write_mode"], "read_only")
        self.configure(write_mode="read_only", allow_raw_writes=True)
        self.assertTrue(self.call("gitlab_api", method="POST", path="projects/1/labels", body={"name": "x"})["refused"])
        self.assertEqual(self.gl.writes(), [])
        self.assertEqual(self.events("write_refused")[-1]["write_mode"], "read_only")
        self.assertTrue(self.call("gitlab_issues", project=1)["success"])


class OperatorOnly(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.configure(write_mode="operator_only")

    def stage(self, **args):
        args.setdefault("project", "platform/api")
        return self.call_kw("gitlab_issue_write", args, task_id="t-9", user_task="please comment")

    def test_model_writes_are_staged_not_sent(self):
        out = self.stage(action="comment", iid=1, body="Staged hello")
        self.assertTrue(out["success"], out)
        self.assertTrue(out["staged"])
        self.assertTrue(out["staged_id"].startswith("glw-"))
        self.assertEqual(out["command"], f"/gitlab run {out['staged_id']}")
        self.assertIn("operator_only", out["note"])
        self.assertEqual(self.gl.writes(), [])
        doc = self.store.load(out["staged_id"])
        self.assertEqual(doc["status"], "staged")
        self.assertEqual(doc["request"]["json"]["body"], "Staged hello")
        self.assertEqual(doc["requested_by"]["kind"], "model")
        self.assertEqual(doc["requested_by"]["request"], "please comment")
        staged = self.events("write_staged")[-1]
        self.assertEqual(staged["staged_id"], out["staged_id"])
        self.assertEqual(staged["action"], "issue.comment")

    def test_dry_run_is_not_staged(self):
        out = self.stage(action="comment", iid=1, body="x", dry_run=True)
        self.assertTrue(out["dry_run"])
        self.assertNotIn("staged", out)
        self.assertEqual(self.store.list(limit=10), [])

    def test_operator_runs_the_staged_write(self):
        staged_id = self.stage(action="comment", iid=1, body="Staged hello")["staged_id"]
        text = self.commands.run(["pending"])
        self.assertIn(staged_id, text)
        self.assertIn("Comment on issue #1", text)
        text = self.commands.run(["show", staged_id[-4:]])
        self.assertIn("POST /api/v4/projects/platform%2Fapi/issues/1/notes", text)
        self.assertIn("Staged hello", text)
        text = self.commands.run(["run", staged_id], via="cli")
        self.assertIn("Comment", text)
        self.assertEqual(len(self.gl.writes()), 1)
        doc = self.store.load(staged_id)
        self.assertEqual(doc["status"], "done")
        self.assertEqual(doc["run"]["actor"]["kind"], "operator")
        self.assertEqual(doc["run"]["actor"]["via"], "cli")
        done = self.events("write_done")[-1]
        self.assertEqual(done["staged_id"], staged_id)
        self.assertEqual(done["actor"]["kind"], "operator")
        self.assertEqual(self.events("staged_run")[-1]["outcome"], "done")
        text = self.commands.run(["run", staged_id])
        self.assertIn("already done", text)
        self.assertEqual(len(self.gl.writes()), 1)

    def test_interrupted_run_is_finalised_as_failed(self):
        staged_id = self.stage(action="comment", iid=1, body="Interrupted")["staged_id"]

        def interrupt(method, path, params, body):
            if method == "POST":
                raise KeyboardInterrupt
            return None

        self.gl.fail_on = interrupt
        actor = {"kind": "operator", "via": "cli"}
        with self.assertRaises(KeyboardInterrupt):
            self.handlers.run_staged(staged_id, actor)
        self.gl.fail_on = None
        doc = self.store.load(staged_id)
        self.assertEqual(doc["status"], "failed")
        self.assertEqual(doc["run"]["outcome"], "failed")
        self.assertIn("interrupted by KeyboardInterrupt", doc["run"]["error"])
        event = self.events("staged_run")[-1]
        self.assertEqual(event["outcome"], "failed")
        self.assertEqual(event["staged_id"], staged_id)
        self.assertIn("already failed", self.commands.run(["run", staged_id]))
        self.assertIn("already failed", self.commands.run(["drop", staged_id]))
        # a document left ``running`` by a hard kill can still be dropped
        doc["status"] = "running"
        self.store.save(doc)
        self.assertIn(staged_id, self.commands.run(["pending"]))
        text = self.commands.run(["drop", staged_id])
        self.assertIn("dropped", text.lower())
        self.assertEqual(self.store.load(staged_id)["status"], "dropped")
        self.assertEqual(self.events("staged_dropped")[-1]["staged_id"], staged_id)

    def test_run_refuses_while_another_process_holds_the_lock(self):
        staged_id = self.stage(action="comment", iid=1, body="Locked")["staged_id"]
        held, release = threading.Event(), threading.Event()

        def holder():
            other = self.store_mod.StagedStore(self.store.directory)
            with other.exclusive(5.0):
                held.set()
                release.wait(5)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(5))
        self.store.claim_timeout = 0.2
        try:
            text = self.commands.run(["run", staged_id])
        finally:
            release.set()
            thread.join(5)
        self.assertIn("being run by another process", text)
        self.assertEqual(self.store.load(staged_id)["status"], "staged")
        self.assertEqual(self.gl.writes(), [])
        self.assertEqual(self.events("write_refused")[-1]["staged_id"], staged_id)
        self.assertIn("Comment", self.commands.run(["run", staged_id]))
        self.assertEqual(self.store.load(staged_id)["status"], "done")

    def test_run_still_enforces_flags_and_preconditions(self):
        sha = self.gl.mrs[1][10]["sha"]
        out = self.call("gitlab_mr_write", project=1, action="merge", iid=10, sha=sha)
        self.assertTrue(out["refused"])  # allow_merge is off: refused before staging
        self.configure(write_mode="operator_only", allow_merge=True)
        staged_id = self.call("gitlab_mr_write", project=1, action="merge", iid=10, sha=sha)["staged_id"]
        self.configure(write_mode="operator_only", allow_merge=False)
        text = self.commands.run(["run", staged_id])
        self.assertIn("allow_merge", text)
        self.assertEqual(self.store.load(staged_id)["status"], "refused")
        self.configure(write_mode="operator_only", allow_merge=True)
        staged_id = self.call("gitlab_mr_write", project=1, action="merge", iid=10, sha=sha)["staged_id"]
        self.gl.mrs[1][10]["sha"] = "a" * 40  # branch moved while it waited
        text = self.commands.run(["run", staged_id])
        self.assertIn("changed since it was read", text)
        self.assertEqual(self.store.load(staged_id)["status"], "conflict")
        self.assertEqual(self.gl.mrs[1][10]["state"], "opened")

    def test_expiry_url_binding_and_drop(self):
        staged_id = self.stage(action="comment", iid=1, body="old")["staged_id"]
        doc = self.store.load(staged_id)
        doc["expires_at"] = (
            (datetime.now(timezone.utc) - timedelta(minutes=1))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        self.store.save(doc)
        text = self.commands.run(["run", staged_id])
        self.assertIn("expired", text)
        self.assertEqual(self.store.load(staged_id)["status"], "expired")
        self.assertEqual(self.events("staged_expired")[-1]["staged_id"], staged_id)
        staged_id = self.stage(action="comment", iid=1, body="elsewhere")["staged_id"]
        doc = self.store.load(staged_id)
        doc["gitlab_url"] = "https://other.example"
        self.store.save(doc)
        self.assertIn("GITLAB_URL", self.commands.run(["run", staged_id]))
        self.assertEqual(self.store.load(staged_id)["status"], "staged")  # a URL mismatch does not consume it
        self.commands.run(["drop", staged_id])
        staged_id = self.stage(action="comment", iid=1, body="never mind")["staged_id"]
        self.assertIn("Dropped", self.commands.run(["drop", staged_id]))
        self.assertEqual(self.store.load(staged_id)["status"], "dropped")
        self.assertIn("already dropped", self.commands.run(["run", staged_id]))
        self.assertEqual(self.gl.writes(), [])
        self.assertEqual(self.commands.run(["pending"]), "No staged writes.")

    def test_unknown_and_ambiguous_ids(self):
        a = self.stage(action="comment", iid=1, body="a")["staged_id"]
        b = self.stage(action="comment", iid=1, body="b")["staged_id"]
        self.assertIn("No staged write matches", self.commands.run(["run", "zzzz"]))
        self.assertIn("ambiguous", self.commands.run(["run", "glw-"]))
        self.assertIn("Dropped", self.commands.run(["drop", a[-4:]]))
        self.assertIn("Dropped", self.commands.run(["drop", b]))

    def test_direct_executor_refuses_unknown(self):
        out = self.handlers.run_staged("glw-nope", {"kind": "operator", "via": "cli"})
        self.assertTrue(out["refused"])


class FullMode(PluginTestCase):
    def test_writes_execute_immediately(self):
        out = self.call("gitlab_issue_write", project=1, action="comment", iid=1, body="now")
        self.assertTrue(out["success"])
        self.assertNotIn("staged", out)
        self.assertEqual(len(self.gl.writes()), 1)
        self.assertEqual(self.store.list(limit=10), [])

    def test_settings_coercion(self):
        Settings = self.settings_mod.Settings

        class Ctx:
            def __init__(self, values):
                self.values = values

            def get_config(self, key, default=None):
                return self.values.get(key, default)

        s = Settings.from_ctx(
            Ctx(
                {
                    "write_mode": "Operator_Only",
                    "allow_merge": "yes",
                    "write_projects": "platform/*, andrew/sandbox",
                    "max_results": "-5",
                    "audit_sinks": [{"type": "syslog", "host": "h"}, "junk"],
                }
            )
        )
        self.assertEqual(s.write_mode, "operator_only")
        self.assertTrue(s.allow_merge)
        self.assertEqual(s.write_projects, ["platform/*", "andrew/sandbox"])
        self.assertEqual(s.max_results, 100)
        self.assertEqual(len(s.audit_sinks), 1)
        self.assertEqual(Settings.from_ctx(Ctx({"write_mode": "bogus"})).write_mode, "full")
        self.assertEqual(Settings.from_ctx(Ctx({"write_projects": ["a", " ", "b"]})).write_projects, ["a", "b"])
        mixed = Settings.from_ctx(Ctx({"audit_log_path": "/Var/Log/GitLab/Audit.jsonl", "write_mode": " FULL "}))
        self.assertEqual(mixed.audit_log_path, "/Var/Log/GitLab/Audit.jsonl")  # paths keep their case
        self.assertEqual(mixed.write_mode, "full")
        self.assertEqual(s.public()["audit_sinks"], 1)
        self.assertEqual(json.loads(json.dumps(s.to_dict()))["write_mode"], "operator_only")


if __name__ == "__main__":
    unittest.main()
