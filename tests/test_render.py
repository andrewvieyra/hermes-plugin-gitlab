import unittest

from .fake_gitlab import FAILED_TRACE, SECRET_LINE
from .helpers import submodule

render = submodule("render")


class JobLogs(unittest.TestCase):
    def test_clean_strips_ansi_sections_and_progress(self):
        text = render.clean_job_log(FAILED_TRACE)
        self.assertNotIn("\x1b[", text)
        self.assertNotIn("section_start", text)
        self.assertNotIn("section_end", text)
        self.assertIn("Preparing the docker executor", text)
        self.assertIn("$ pytest -q", text)
        self.assertIn("Downloading 100%", text)
        self.assertNotIn("Downloading 10%", text)
        self.assertTrue(text.endswith("ERROR: Job failed: exit code 1"))

    def test_tail_and_search(self):
        text = render.clean_job_log(FAILED_TRACE)
        lines, total = render.tail_lines(text, 2)
        self.assertEqual(len(lines), 2)
        self.assertGreater(total, 5)
        snippet, total_lines, matches, _stopped = render.search_lines(text, "error|failed", context=1)
        self.assertEqual(total_lines, total)
        self.assertGreaterEqual(matches, 3)
        self.assertIn(">", snippet)
        self.assertIn("AssertionError", snippet)
        snippet, _t, matches, _stopped = render.search_lines(text, "(unbalanced", context=0)
        self.assertEqual(matches, 0)
        self.assertEqual(snippet, "")

    def test_redaction_levels(self):
        text = render.clean_job_log(FAILED_TRACE)
        masked, count = render.redact_log(text)
        self.assertNotIn(SECRET_LINE, masked)
        self.assertNotIn("hunter2hunter2", masked)
        self.assertIn("DB_PASSWORD=[REDACTED]", masked)
        self.assertGreaterEqual(count, 2)
        content, n = render.redact_content("password = 'hunter2hunter2'\nkey = 'glpat-ABCDEFGHIJKLMNOPQRSTUVWX'\n")
        self.assertIn("hunter2hunter2", content)  # generic assignments are left alone in code
        self.assertNotIn("glpat-ABCDEFGHIJKLMNOPQRSTUVWX", content)
        self.assertEqual(n, 1)
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"
        self.assertEqual(render.redact_content(pem)[0], "[REDACTED]")
        self.assertIn(
            "Authorization: Bearer [REDACTED]", render.redact_content("Authorization: Bearer abcdefgh.ijklmnop")[0]
        )
        self.assertIn("https://user:[REDACTED]@host/x", render.redact_content("https://user:s3cretpass@host/x")[0])


class Diffs(unittest.TestCase):
    ITEMS = [
        {
            "old_path": "a.py",
            "new_path": "a.py",
            "new_file": False,
            "deleted_file": False,
            "renamed_file": False,
            "diff": "@@ -1,2 +1,3 @@\n import x\n-y = 1\n+y = 2\n+z = 3\n",
        },
        {
            "old_path": "b.py",
            "new_path": "c.py",
            "new_file": False,
            "deleted_file": False,
            "renamed_file": True,
            "diff": "",
        },
        {
            "old_path": "gone.py",
            "new_path": "gone.py",
            "new_file": False,
            "deleted_file": True,
            "renamed_file": False,
            "diff": "@@ -1 +0,0 @@\n-print('bye')\n",
        },
        {
            "old_path": "big.txt",
            "new_path": "big.txt",
            "new_file": True,
            "deleted_file": False,
            "renamed_file": False,
            "diff": "@@ -0,0 +1,100 @@\n" + "\n".join("+" + "x" * 50 for _ in range(100)) + "\n",
        },
    ]

    def test_summary_and_headers(self):
        out = render.render_diffs(self.ITEMS, max_bytes=100_000)
        self.assertEqual([f["status"] for f in out["files"]], ["modified", "renamed", "deleted", "added"])
        self.assertEqual(out["files"][0]["additions"], 2)
        self.assertEqual(out["files"][0]["deletions"], 1)
        self.assertEqual(out["files"][1]["old_path"], "b.py")
        self.assertIn("diff --git a/a.py b/a.py", out["text"])
        self.assertIn("rename from b.py", out["text"])
        self.assertIn("+++ /dev/null", out["text"])
        self.assertIn("--- /dev/null", out["text"])
        self.assertFalse(out["truncated"])
        self.assertEqual(out["total_files"], 4)

    def test_path_filter_and_truncation(self):
        out = render.render_diffs(self.ITEMS, max_bytes=100_000, paths=["*.py"])
        self.assertEqual(out["total_files"], 3)
        self.assertNotIn("big.txt", out["text"])
        out = render.render_diffs(self.ITEMS, max_bytes=400)
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["files"]), 4)  # the summary is always complete
        self.assertIn("big.txt", out["omitted"])
        self.assertLessEqual(out["bytes"], 400)

    def test_single_oversized_file_is_headed(self):
        out = render.render_diffs([self.ITEMS[3]], max_bytes=300)
        self.assertTrue(out["truncated"])
        self.assertIn("[diff truncated]", out["text"])

    def test_redaction_hook(self):
        items = [{"old_path": "k", "new_path": "k", "diff": "+token = 'glpat-ABCDEFGHIJKLMNOPQRSTUVWX'\n"}]
        out = render.render_diffs(items, max_bytes=1000, redact_fn=render.redact_content)
        self.assertNotIn("glpat-ABCDEFGHIJKLMNOPQRSTUVWX", out["text"])
        self.assertEqual(out["redacted"], 1)


class Summaries(unittest.TestCase):
    def test_merge_request_fields(self):
        mr = render.merge_request(
            {
                "iid": 1,
                "title": "t",
                "state": "opened",
                "author": {"username": "a"},
                "reviewers": [{"username": "r"}],
                "head_pipeline": {"id": 5, "status": "failed", "web_url": "u"},
                "merge_status": "can_be_merged",
                "references": {"full": "g/p!1"},
            }
        )
        self.assertEqual(mr["author"], "a")
        self.assertEqual(mr["reviewers"], ["r"])
        self.assertEqual(mr["head_pipeline"]["status"], "failed")
        self.assertEqual(mr["detailed_merge_status"], "can_be_merged")
        self.assertEqual(mr["reference"], "g/p!1")
        self.assertNotIn("description", mr)
        self.assertIn("description", render.merge_request({"description": "x" * 30000}, full=True))
        self.assertIn("[truncated", render.merge_request({"description": "x" * 30000}, full=True)["description"])

    def test_discussion_resolution(self):
        d = render.discussion(
            {
                "id": "d1",
                "notes": [
                    {"id": 1, "resolvable": True, "resolved": False, "body": "x"},
                    {"id": 2, "resolvable": True, "resolved": True, "body": "y"},
                ],
            }
        )
        self.assertTrue(d["resolvable"])
        self.assertFalse(d["resolved"])
        d = render.discussion({"id": "d2", "notes": [{"id": 3, "resolvable": False, "body": "z"}]})
        self.assertFalse(d["resolvable"])
        self.assertIsNone(d["resolved"])
        self.assertTrue(render.discussion_is_system({"notes": [{"system": True}]}))
        self.assertFalse(render.discussion_is_system({"notes": [{"system": True}, {"system": False}]}))

    def test_reports_render(self):
        mr = render.merge_request({"iid": 1, "title": "t", "state": "opened", "updated_at": "2026-09-09T10:00:00Z"})
        text = render.mr_report(mr, {"approved_by": ["a"], "approvals_required": 2, "approvals_left": 1}, 3, "g/p")
        self.assertIn("!1 t", text)
        self.assertIn("approvals 1/2", text)
        self.assertIn("unresolved threads 3", text)
        status = render.status_report(
            {
                "url": "https://g",
                "gitlab": {"version": "17"},
                "user": {"username": "u"},
                "token": None,
                "plugin": {"version": "0.1.0", "write_mode": "full"},
                "warnings": ["w"],
            }
        )
        self.assertIn("Warning: w", status)
        self.assertIn("not readable", status)
        line = render.audit_line(
            {
                "ts": "2026-09-09T10:00:00Z",
                "event": "write_done",
                "actor": {"user_name": "andrew", "platform": "signal"},
                "action": "mr.merge",
                "project": "g/p",
                "target": {"kind": "merge_request", "iid": 4},
                "summary": "Merged",
            }
        )
        self.assertIn("andrew@signal", line)
        self.assertIn("merge_request!4", line)

    def test_clip(self):
        self.assertEqual(render.clip("abc", 10), "abc")
        self.assertTrue(render.clip("abcdef", 3).startswith("abc…"))
        self.assertEqual(render.clip(None, 3), "")


class LocalTime(unittest.TestCase):
    def test_stamps_render_in_the_configured_zone_and_junk_passes_through(self):
        from zoneinfo import ZoneInfo

        timefmt = submodule("timefmt")
        original = timefmt.get_zone
        timefmt.get_zone = lambda: ZoneInfo("America/Los_Angeles")
        try:
            self.assertEqual(timefmt.local("2026-09-09T19:35:50Z"), "2026-09-09 12:35:50 PDT")
            self.assertEqual(timefmt.local("garbage"), "garbage")
            self.assertEqual(timefmt.local(None), "")
            self.assertEqual(timefmt.local(12345), 12345)  # a non-string stamp comes back unchanged, not an error
            line = render.audit_line({"ts": "2026-09-09T19:35:50Z", "event": "write_done", "summary": "s"})
            self.assertTrue(line.startswith("2026-09-09 12:35:50 PDT"), line)
        finally:
            timefmt.get_zone = original


if __name__ == "__main__":
    unittest.main()
