import argparse
import io
import unittest
from contextlib import redirect_stdout

from .base import PluginTestCase


class Slash(PluginTestCase):
    def test_help_and_unknown(self):
        self.assertIn("/gitlab —", self.commands.slash_handler(""))
        self.assertIn("Unknown subcommand", self.commands.slash_handler("dance"))
        self.assertIn("/gitlab —", self.commands.slash_handler("help"))

    def test_status(self):
        text = self.commands.slash_handler("status")
        self.assertIn("GitLab 17.4.1", text)
        self.assertIn("Authenticated as hermes-bot", text)
        self.assertIn("scopes api", text)
        self.assertIn("write_mode full", text)

    def test_mr_summary(self):
        text = self.commands.slash_handler("mr platform/api 10")
        self.assertIn("!10 Add login handler", text)
        self.assertIn("feature/login -> main", text)
        self.assertIn("pipeline failed", text)
        self.assertIn("approvals 0/1", text)
        self.assertIn("unresolved threads 1", text)
        self.assertIn("Usage", self.commands.slash_handler("mr platform/api"))
        self.assertIn("Error", self.commands.slash_handler("mr platform/api 99"))

    def test_audit_tail_and_prune(self):
        self.assertIn("No audit events", self.commands.slash_handler("audit"))
        self.call("gitlab_issue_write", project=1, action="comment", iid=1, body="x")
        text = self.commands.slash_handler("audit 5")
        self.assertIn("write_done", text)
        self.assertIn("issue.comment", text)
        self.assertIn("Usage", self.commands.slash_handler("audit five"))
        self.assertIn("Nothing older", self.commands.slash_handler("prune"))
        self.configure(staged_retention_days=0)
        self.assertIn("disabled", self.commands.slash_handler("prune"))
        self.assertIn("Usage", self.commands.slash_handler("prune --days"))

    def test_operator_reads_are_audited_as_operator(self):
        self.configure(audit_reads=True)
        self.commands.slash_handler("status")
        self.commands.slash_handler("mr platform/api 10")
        self.commands.run(["status"], via=self.audit.VIA_CLI)
        events = self.events("read_done")
        self.assertGreaterEqual(len(events), 3)
        self.assertTrue(all(e["actor"]["kind"] == "operator" for e in events), events)
        self.assertEqual(events[0]["actor"]["via"], self.audit.VIA_SLASH)
        self.assertEqual(events[-1]["actor"]["via"], self.audit.VIA_CLI)
        self.assertEqual(self.events("write_done"), [])

    def test_cli_round_trip(self):
        parser = argparse.ArgumentParser()
        self.commands.setup_cli(parser)
        args = parser.parse_args(["mr", "platform/api", "10"])
        out = io.StringIO()
        with redirect_stdout(out):
            self.commands.cli_handler(args)
        self.assertIn("!10 Add login handler", out.getvalue())
        args = parser.parse_args(["prune", "--days", "5", "--dry-run"])
        with redirect_stdout(out):
            self.commands.cli_handler(args)
        self.assertIn("Nothing older than 5", out.getvalue())
        args = parser.parse_args(["audit", "3"])
        with redirect_stdout(out):
            self.commands.cli_handler(args)
        self.assertIn("No audit events", out.getvalue())
        args = parser.parse_args(["show", "zzzz"])
        with redirect_stdout(out):
            self.commands.cli_handler(args)
        self.assertIn("No staged write matches", out.getvalue())

    def test_shlex_fallback(self):
        self.assertIn("Unknown subcommand", self.commands.slash_handler("it's broken"))


if __name__ == "__main__":
    unittest.main()
