import json
import os
import tempfile
import unittest
from pathlib import Path

from .base import PluginTestCase
from .helpers import submodule

_SESSION = {
    "HERMES_SESSION_PLATFORM": "signal",
    "HERMES_SESSION_SOURCE": "gateway",
    "HERMES_SESSION_CHAT_ID": "+15550001111",
    "HERMES_SESSION_CHAT_NAME": "Andrew",
    "HERMES_SESSION_CHAT_TYPE": "dm",
    "HERMES_SESSION_USER_ID": "+15550001111",
    "HERMES_SESSION_USER_ID_ALT": "uuid-1234",
    "HERMES_SESSION_USER_NAME": "Andrew",
    "HERMES_SESSION_KEY": "signal:dm:+15550001111",
    "HERMES_SESSION_ID": "sess-abc",
    "HERMES_SESSION_MESSAGE_ID": "m-77",
}


class _EnvMixin:
    def set_session(self, **overrides):
        self._saved = {k: os.environ.get(k) for k in list(_SESSION) + ["HERMES_CRON_SESSION"] + list(overrides)}
        os.environ.update(_SESSION)
        os.environ.update(overrides)

    def clear_session(self):
        for k, v in getattr(self, "_saved", {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class ActorCapture(_EnvMixin, PluginTestCase):
    def tearDown(self):
        self.clear_session()
        super().tearDown()

    def test_model_actor_from_session_context_and_kwargs(self):
        self.set_session()
        actor = self.audit.capture_actor(
            {"task_id": "t-1", "session_id": "sess-override", "user_task": "Merge !10 please"}, via="tool"
        )
        self.assertEqual(actor["kind"], "model")
        self.assertEqual(actor["via"], "tool")
        self.assertEqual(actor["platform"], "signal")
        self.assertEqual(actor["user_name"], "Andrew")
        self.assertEqual(actor["chat_type"], "dm")
        self.assertEqual(actor["session_id"], "sess-override")
        self.assertEqual(actor["task_id"], "t-1")
        self.assertEqual(actor["message_id"], "m-77")
        self.assertEqual(actor["request"], "Merge !10 please")
        self.assertFalse(actor["cron"])
        self.assertEqual(self.audit.describe_actor(actor), "model via tool on signal for Andrew in dm Andrew")

    def test_operator_actor_and_cron(self):
        self.set_session(HERMES_CRON_SESSION="1")
        actor = self.audit.capture_actor({}, via="cli")
        self.assertEqual(actor["kind"], "operator")
        self.assertTrue(actor["cron"])
        self.assertIsNone(actor["request"])
        self.assertIn("[cron]", self.audit.describe_actor(actor))
        self.clear_session()
        actor = self.audit.capture_actor({}, via="cli")
        self.assertIsNone(actor["platform"])
        self.assertTrue(self.audit.describe_actor(actor).startswith("operator via cli"))

    def test_request_text_respects_setting_and_limit(self):
        self.configure(audit_include_request=False)
        self.assertIsNone(self.audit.capture_actor({"user_task": "hi"})["request"])
        self.configure(audit_include_request=True)
        long = "x" * 900
        self.assertEqual(len(self.audit.capture_actor({"user_task": long})["request"]), self.audit.REQUEST_TEXT_LIMIT)

    def test_request_text_is_redacted_before_it_is_recorded(self):
        # a token pasted into chat must not reach audit.jsonl or a sink; redact_secrets governs it
        pasted = "use glpat-abcdefghijklmnopqrst to comment"
        self.configure(redact_secrets=True, audit_include_request=True)
        self.assertEqual(self.audit.capture_actor({"user_task": pasted})["request"], "use [REDACTED] to comment")
        self.configure(redact_secrets=False, audit_include_request=True)
        self.assertEqual(self.audit.capture_actor({"user_task": pasted})["request"], pasted)

    def test_actor_keys_are_stable(self):
        keys = set(self.audit.capture_actor({}))
        self.set_session()
        self.assertEqual(set(self.audit.capture_actor({"task_id": "t"})), keys)


class LogWriting(PluginTestCase):
    def test_records_have_common_fields_and_are_appended(self):
        self.audit.emit(
            "write_done",
            actor={"kind": "model"},
            gitlab_url="https://g",
            action="issue.create",
            project="g/p",
            target={"kind": "issue", "iid": 1},
            http_status=201,
        )
        self.audit.emit("write_refused", actor=None, gitlab_url="https://g", reason="nope")
        rows = self.events()
        self.assertEqual([r["event"] for r in rows], ["write_done", "write_refused"])
        for row in rows:
            for key in (
                "ts",
                "schema",
                "plugin",
                "event",
                "gitlab_url",
                "action",
                "project",
                "target",
                "staged_id",
                "actor",
                "host",
                "pid",
            ):
                self.assertIn(key, row)
        self.assertEqual(rows[0]["target"]["iid"], 1)
        self.assertEqual(rows[1]["reason"], "nope")
        self.assertTrue(rows[0]["ts"].endswith("Z"))
        self.assertEqual(self.audit_path.stat().st_mode & 0o777, 0o600)
        tail = self.audit.get_audit_log().tail(1)
        self.assertEqual(tail[0]["event"], "write_refused")

    def test_disabled_log_writes_nothing(self):
        self.audit.set_audit_log(self.audit.AuditLog(self.audit_path, enabled=False))
        self.assertIsNone(self.audit.emit("write_done", actor=None, gitlab_url=None))
        self.assertFalse(self.audit_path.exists())
        self.assertEqual(self.audit.get_audit_log().tail(5), [])

    def test_default_log_from_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom.jsonl"
            self.configure(audit_log_path=str(path))
            self.audit.set_audit_log(None)
            log = self.audit.get_audit_log()
            self.assertEqual(log.path, path)
            log.emit("write_done", actor=None, gitlab_url=None)
            self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_environment_block(self):
        env = self.audit.environment()
        self.assertEqual(env["plugin_version"], submodule("version").__version__)
        self.assertIn("host", env)
        self.assertIn("os_user", env)

    def test_write_failure_does_not_raise(self):
        log = self.audit.AuditLog(Path("/nonexistent-root-dir-for-tests/x/audit.jsonl"))
        self.assertIsNotNone(log.emit("write_done", actor=None, gitlab_url=None))

    def test_events_are_json_lines(self):
        self.audit.emit("write_done", actor=None, gitlab_url=None, summary="a\nb")
        line = self.audit_path.read_text().splitlines()[0]
        self.assertEqual(json.loads(line)["summary"], "a\nb")


if __name__ == "__main__":
    unittest.main()
