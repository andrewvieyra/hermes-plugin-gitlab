"""Second review pass: transport hardening, deny-list coverage, raw-result hygiene, diff positions on
renamed files, branch creation, pipeline inputs, project metadata and URL parsing."""

import json
import os
import time
import unittest
from unittest import mock

from .base import PluginTestCase
from .fake_gitlab import SECRET_LINE, _Response
from .helpers import submodule

client_mod = submodule("client")
executor = submodule("executor")
render = submodule("render")
targets = submodule("targets")


class Transport(PluginTestCase):
    def test_redirects_are_reported_not_followed(self):
        def redirect(method, path, params, body):
            if path == "user":
                return _Response(302, None, {"Location": "https://elsewhere.example/api/v4/user"})
            return None

        self.gl.fail_on = redirect
        with self.assertRaises(client_mod.GitLabError) as ctx:
            self.client.get("user")
        self.assertEqual(ctx.exception.status, 302)
        self.assertIn("elsewhere.example", str(ctx.exception))
        self.assertIn("not followed", str(ctx.exception))
        out = self.call("gitlab_api", path="status")
        self.assertFalse(out["success"])
        self.assertIn("redirect", out["error"])

    def test_encoded_traversal_is_rejected(self):
        for bad in ("projects/1/%2e%2e/admin/users", "projects/1/%252e%252e/admin", "%2e/projects/1", "a/%5c..%5cb"):
            with self.assertRaises(client_mod.GitLabError, msg=bad):
                client_mod.validate_path(bad)
        self.assertEqual(client_mod.validate_path("projects/platform%2Fapi/labels"), "projects/platform%2Fapi/labels")
        before = len(self.gl.calls)
        out = self.call("gitlab_api", path="projects/1/%2e%2e/%2e%2e/admin/users")
        self.assertFalse(out["success"])
        self.assertIn("invalid path", out["error"])
        self.assertEqual(len(self.gl.calls), before)


class DenyLists(PluginTestCase):
    def test_resolved_traversal_and_new_surfaces(self):
        self.assertIsNotNone(executor.raw_path_refusal("GET", "projects/1/../../admin/users"))
        self.assertIn("admin/users", executor.path_forms("projects/1/%2e%2e/%2e%2e/admin/users"))
        for path in ("projects/1/triggers", "projects/1/cluster_agents/3/tokens", "groups/2/variables/KEY"):
            self.assertTrue(self.call("gitlab_api", path=path).get("refused"), path)
        self.configure(allow_raw_writes=True, allow_raw_delete=True)
        before = len(self.gl.writes())
        for method, path in (
            ("POST", "projects"),
            ("POST", "groups"),
            ("DELETE", "projects/1/repository/merged_branches"),
            ("POST", "groups/2/ldap_group_links"),
        ):
            out = self.call("gitlab_api", method=method, path=path, body={"name": "x"} if method == "POST" else None)
            self.assertTrue(out.get("refused"), (method, path, out))
        self.assertEqual(len(self.gl.writes()), before)
        self.assertIsNone(executor.raw_path_refusal("GET", "projects"))
        self.assertIsNotNone(executor.raw_path_refusal("POST", "projects/1/fork"))
        self.assertIsNone(executor.raw_path_refusal("POST", "projects/1/repository/branches"))


class RawResults(PluginTestCase):
    def test_get_results_are_redacted_and_capped(self):
        self.gl.mrs[1][10]["description"] = f"deploy with {SECRET_LINE} please"
        out = self.call("gitlab_api", path="projects/1/merge_requests/10")
        self.assertTrue(out["success"], out)
        self.assertNotIn(SECRET_LINE, out["result"]["description"])
        self.assertEqual(out["redacted"], 1)
        self.configure(redact_secrets=False)
        out = self.call("gitlab_api", path="projects/1/merge_requests/10")
        self.assertIn(SECRET_LINE, out["result"]["description"])
        self.configure(max_file_bytes=300)
        out = self.call("gitlab_api", path="projects/1/repository/files/docs%2Fguide.md", params={"ref": "main"})
        self.assertTrue(out["success"], out)
        self.assertTrue(out["truncated"])
        self.assertIsInstance(out["result"], str)
        self.assertIn("max_file_bytes", out["result"])
        out = self.call("gitlab_api", path="projects/1/issues", paginate=True, limit=3)
        self.assertTrue(out["success"], out)
        self.assertTrue(out["truncated"])
        self.assertIsInstance(out["results"], str)
        self.assertEqual(out["returned"], 3)

    def test_raw_write_results_are_redacted(self):
        self.configure(allow_raw_writes=True)
        out = self.call(
            "gitlab_api", method="POST", path="projects/1/labels", body={"name": SECRET_LINE, "color": "#123456"}
        )
        self.assertTrue(out["success"], out)
        self.assertNotIn(SECRET_LINE, json.dumps(out["result"]))
        self.assertIn(SECRET_LINE, self.gl.writes()[-1]["json"]["name"])  # what was sent is untouched


class Redaction(unittest.TestCase):
    def test_more_token_formats(self):
        # third-party key shapes are joined at runtime, so the repository holds nothing a scanner reads as a live key
        samples = {
            "glpat-abcdefghijklmnopqrst.01.0a1b2c3d4": "routable gitlab token",
            "GR1348941abcdefghijklmnopqrstuvwxyz": "runner registration",
            "sk-ant-api03-" + "abcdefghijklmnopqrstuvwxyz0123456789": "anthropic key",
            "sk-proj-" + "abcdefghijklmnopqrstuvwxyz": "openai key",
            "sk_live_" + "abcdefghijklmnopqrstuvwxyz": "stripe key",
            "AIza" + "SyA1234567890abcdefghijklmnopqrstuv": "google key",
            "npm_" + "a" * 36: "npm token",
            "hf_" + "b" * 34: "hugging face token",
        }
        for token, label in samples.items():
            text, count = render.redact_content(f"key = {token} # {label}")
            self.assertNotIn(token, text, label)
            self.assertNotIn(token[-8:], text, label)
            self.assertGreaterEqual(count, 1, label)
            self.assertIn(label, text)
        self.assertEqual(render.redact_content("skip-this, sk-short and a task_list")[1], 0)

    def test_redact_any_walks_everything_and_copies(self):
        obj = {"a": [SECRET_LINE, {"b": f"x {SECRET_LINE}"}], "c": 3, "d": None}
        out, count = render.redact_any(obj)
        self.assertEqual(count, 2)
        self.assertNotIn(SECRET_LINE, json.dumps(out))
        self.assertEqual((out["c"], out["d"]), (3, None))
        self.assertIn(SECRET_LINE, obj["a"][0])


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


class Previews(PluginTestCase):
    def test_commit_preview_clips_content_but_stages_all_of_it(self):
        big = "x" * 5000
        actions = [{"action": "create", "file_path": "big.txt", "content": big}]
        out = self.call(
            "gitlab_commit", project="platform/api", branch="main", commit_message="big", actions=actions, dry_run=True
        )
        self.assertTrue(out["dry_run"], out)
        shown = out["preview"]["json"]["actions"][0]["content"]
        self.assertLess(len(shown), 400)
        self.assertIn("more characters", shown)
        self.assertEqual(self.gl.writes(), [])
        self.configure(write_mode="operator_only")
        out = self.call("gitlab_commit", project="platform/api", branch="main", commit_message="big", actions=actions)
        self.assertTrue(out["staged"], out)
        self.assertIn("more characters", out["preview"]["json"]["actions"][0]["content"])
        self.assertEqual(self.store.load(out["staged_id"])["request"]["json"]["actions"][0]["content"], big)


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


class ProjectMetadata(PluginTestCase):
    def test_repo_project_action(self):
        out = self.call("gitlab_repo", project="platform/api", action="project")
        self.assertTrue(out["success"], out)
        self.assertEqual(out["metadata"]["path_with_namespace"], "platform/api")
        self.assertEqual(out["metadata"]["default_branch"], "main")
        out = self.call("gitlab_repo", project=f"{self.gl.base_url}/platform/api/-/merge_requests/10", action="project")
        self.assertEqual(out["metadata"]["id"], 1)
        out = self.call(
            "gitlab_pipelines", action="get", project=1, pipeline_id=self.gl.mrs[1][10]["head_pipeline"]["id"]
        )
        self.assertTrue(out["success"], out)
        self.assertEqual(out["bridges"], [])


class Urls(unittest.TestCase):
    def test_relative_url_root_and_reserved_paths(self):
        base = "https://gitlab.test/gitlab"
        parsed = targets.parse_url("https://gitlab.test/gitlab/platform/api/-/issues/3", base)
        self.assertEqual((parsed["project"], parsed["kind"], parsed["iid"]), ("platform/api", "issue", 3))
        self.assertEqual(targets.project_arg("https://gitlab.test/gitlab/platform/api", base), "platform/api")
        for url in (
            "https://gitlab.test/groups/platform/-/issues",
            "https://gitlab.test/admin/users/x",
            "https://gitlab.test/explore/projects/topics",
        ):
            self.assertIsNone(targets.parse_url(url), url)
            with self.assertRaises(client_mod.GitLabError):
                targets.project_arg(url, "https://gitlab.test")


class PathInjection(PluginTestCase):
    def test_discussion_ids_are_validated_before_use(self):
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


class AuthOverride(PluginTestCase):
    def test_sudo_and_credential_parameters_are_refused(self):
        self.configure(allow_raw_writes=True)
        before = len(self.gl.calls)
        for method, extra in (
            ("GET", {"params": {"sudo": "root"}}),
            ("GET", {"params": {"PRIVATE_TOKEN": "glpat-somebody-elses-token-1"}}),
            ("POST", {"body": {"name": "x", "sudo": 2}}),
            ("POST", {"params": {"access_token": "y"}, "body": {"name": "x"}}),
            ("POST", {"params": {"job_token": "y"}, "body": {"name": "x"}}),
            # Rack parses `sudo[]=root` as sudo => ["root"], and GitLab's username lookup takes an array
            ("GET", {"params": {"sudo[]": "root"}}),
            ("POST", {"params": {"sudo[x]": "root"}, "body": {"name": "x"}}),
            ("POST", {"body": {"name": "x", " SUDO []": 2}}),
        ):
            out = self.call("gitlab_api", method=method, path="projects/1/labels", **extra)
            self.assertTrue(out.get("refused") or out.get("rejected"), (method, extra, out))
            self.assertIn("configured token", out["error"])
        self.assertEqual(len(self.gl.calls), before)
        self.assertGreaterEqual(len(self.events("write_refused")) + len(self.events("write_rejected")), 8)
        # the gate re-checks a staged raw request, so a tampered document cannot smuggle one in either
        self.assertIsNotNone(executor.auth_override(None, {"Sudo": 1}))
        self.assertIsNotNone(executor.auth_override({"sudo[]": "root"}, None))
        self.assertIsNone(executor.auth_override({"search": "x", "labels[]": "bug"}, {"name": "y"}))

    def test_gate_revalidates_a_staged_raw_path(self):
        # a staged file edited on disk cannot smuggle in a path the builder would have rejected
        self.configure(allow_raw_writes=True)
        req = executor.WriteRequest(action="api.POST", method="POST", path="projects/1/%2e%2e/admin", summary="x")
        reason = executor.gate(self.client, req, self.settings, {})
        self.assertIsNotNone(reason)
        self.assertIn("invalid path", reason)
        req = executor.WriteRequest(
            action="api.POST", method="POST", path="projects/1/labels", summary="x", params={"sudo": 1}
        )
        self.assertIn("configured token", executor.gate(self.client, req, self.settings, {}))
        req = executor.WriteRequest(action="api.POST", method="POST", path="projects/1/labels", summary="x")
        self.assertIsNone(executor.gate(self.client, req, self.settings, {}))


class OutputHygiene(PluginTestCase):
    def test_own_token_is_scrubbed_from_every_result(self):
        self.configure(redact_secrets=False)
        self.gl.mrs[1][10]["description"] = "leaked 0123456789abcdefXYZ here"
        with mock.patch.dict(os.environ, {"GITLAB_TOKEN": "Bearer 0123456789abcdefXYZ"}):
            out = self.call("gitlab_merge_requests", action="get", project=1, iid=10)
        self.assertTrue(out["success"], out)
        self.assertNotIn("0123456789abcdefXYZ", out["merge_request"]["description"])
        self.assertIn("***", out["merge_request"]["description"])
        self.assertEqual(client_mod.scrub_env("a short one", {"GITLAB_TOKEN": "short"}), "a short one")

    def test_user_text_in_every_listing_is_redacted(self):
        self.gl.projects[1]["description"] = f"see {SECRET_LINE}"
        out = self.call("gitlab_search", scope="projects", query="api")
        self.assertTrue(out["success"], out)
        self.assertNotIn(SECRET_LINE, json.dumps(out))
        self.assertGreaterEqual(out["redacted"], 1)
        self.gl.branches[1]["main"]["commit"]["title"] = f"Rotate {SECRET_LINE}"
        out = self.call("gitlab_repo", project=1, action="branches")
        self.assertNotIn(SECRET_LINE, json.dumps(out))
        self.gl.mrs[1][10]["description"] = f"deploy {SECRET_LINE}"
        out = self.call("gitlab_mr_write", project=1, action="update", iid=10, title="New title")
        self.assertTrue(out["success"], out)
        self.assertNotIn(SECRET_LINE, json.dumps(out["result"]))

    def test_audit_request_text_is_redacted(self):
        out = self.call_kw(
            "gitlab_issue_write",
            {"project": 1, "action": "comment", "iid": 1, "body": "x"},
            user_task=f"use {SECRET_LINE} to comment",
        )
        self.assertTrue(out["success"], out)
        actor = self.events("write_done")[-1]["actor"]
        self.assertNotIn(SECRET_LINE, actor["request"])
        self.assertIn("[REDACTED]", actor["request"])
        line = render.audit_line({"ts": "2026-09-11T00:00:00Z", "event": "write_failed", "error": "one\ntwo\r\nthree"})
        self.assertNotIn("\n", line)
        self.assertIn("one two three", line)


class MoreDenyLists(PluginTestCase):
    def test_second_pass_surfaces(self):
        for path in (
            "projects/1/pipeline_schedules/5",
            "projects/1/error_tracking/client_keys",
            "projects/1/audit_events",
            "projects/1/trigger/pipeline",
        ):
            self.assertTrue(self.call("gitlab_api", path=path).get("refused"), path)
        self.assertIsNone(executor.raw_path_refusal("GET", "projects/1/pipeline_schedules"))
        self.configure(allow_raw_writes=True, allow_raw_delete=True)
        before = len(self.gl.writes())
        for method, path in (
            ("POST", "projects/1/fork"),
            ("POST", "projects/1/issues/1/move"),
            ("POST", "projects/1/issues/1/clone"),
            ("PATCH", "projects/1/job_token_scope"),
            ("POST", "projects/1/access_requests"),
            ("POST", "projects/1/invitations"),
            ("POST", "groups/2/projects/1"),
            ("PUT", "projects/1/repository/branches/main/protect"),
            ("PUT", "projects/1/merge_requests/10/reset_approvals"),
            ("DELETE", "projects/1/pages"),
        ):
            out = self.call("gitlab_api", method=method, path=path, body={"a": 1} if method != "DELETE" else None)
            self.assertTrue(out.get("refused"), (method, path, out))
        self.assertEqual(len(self.gl.writes()), before)
        self.assertIsNone(executor.raw_path_refusal("GET", "projects/1/forks"))
        self.assertIsNone(executor.raw_path_refusal("POST", "projects/1/pipeline_schedules/5/play"))
        self.assertIsNone(executor.raw_path_refusal("POST", "projects/1/releases"))


class MergeRequestExtras(PluginTestCase):
    def test_cancel_auto_merge_closes_issues_pipeline_name_and_pattern_cap(self):
        out = self.call("gitlab_mr_write", project=1, action="cancel_auto_merge", iid=10, dry_run=True)
        self.assertTrue(out["dry_run"], out)
        self.assertTrue(out["preview"]["path"].endswith("/merge_requests/10/cancel_merge_when_pipeline_succeeds"))
        self.assertEqual(out["preview"]["method"], "POST")
        self.assertEqual(out["preview"]["requires"], [])
        out = self.call("gitlab_merge_requests", action="get", project=1, iid=10)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["closes_issues"], [])
        out = self.call("gitlab_pipelines", project=1, name="nightly")
        self.assertTrue(out["success"], out)
        self.assertEqual(self.gl.calls[-1]["params"].get("name"), "nightly")
        job_id = self.call("gitlab_pipelines", project=1, action="jobs")["jobs"][0]["id"]
        out = self.call("gitlab_pipelines", project=1, action="log", job_id=job_id, search="(a+)+" * 60)
        self.assertFalse(out["success"])
        self.assertIn("200 characters", out["error"])


class RawTypedOnly(PluginTestCase):
    def test_raw_merge_approve_commit_and_token_revocation_are_refused(self):
        self.configure(allow_raw_writes=True, allow_raw_delete=True, allow_merge=True)
        head = self.gl.mrs[1][10]["sha"]
        before = len(self.gl.writes())
        for method, path, body in (
            ("PUT", "projects/1/merge_requests/10/merge", {"sha": head}),
            ("PUT", "projects/platform%2Fapi/merge_requests/10/merge", None),
            ("POST", "projects/1/merge_requests/10/approve", {"sha": head}),
            ("POST", "projects/1/repository/commits", {"branch": "main", "commit_message": "x", "actions": []}),
            ("DELETE", "personal_access_tokens/self", None),
            ("POST", "personal_access_tokens/self/rotate", None),
        ):
            out = self.call("gitlab_api", method=method, path=path, body=body)
            self.assertTrue(out.get("refused"), (method, path, out))
        self.assertEqual(len(self.gl.writes()), before)
        self.assertEqual(self.gl.mrs[1][10]["state"], "opened")
        out = self.call("gitlab_api", method="PUT", path="projects/1/merge_requests/10/merge")
        self.assertIn("gitlab_mr_write", out["error"])
        self.assertTrue(self.call("gitlab_api", path="personal_access_tokens/self")["success"])
        self.assertIsNone(executor.raw_path_refusal("POST", "projects/1/merge_requests/10/notes"))
        self.assertIsNone(executor.raw_path_refusal("POST", "projects/1/merge_requests/10/unapprove"))
        self.assertIsNone(executor.raw_path_refusal("GET", "projects/1/merge_requests/10/merge_ref"))
        self.assertIsNone(executor.raw_path_refusal("GET", "projects/1/repository/commits"))


class RegexGuard(unittest.TestCase):
    def test_dangerous_patterns_are_refused_and_safe_ones_run_fast(self):
        for bad in ("(a+)+$", "(a|aa)+", "((ab)*)*", ".*.*.*x", r"(\w)\1", r"\d{1,1000}", "(?P<x>a)(?P=x)", "(?:a+)*"):
            self.assertIsNotNone(render.regex_risk(bad), bad)
        for good in (
            "error|fail|exception|denied",
            "error.*token",
            "[+*]+x",
            r"\(+x",
            "(?:error|warn)",
            "(error|warn)?",
            "a{2,5}",
            r"^\s*Error:",
            "(?i:fatal)",
        ):
            self.assertIsNone(render.regex_risk(good), good)
        start = time.monotonic()
        _snippet, _total, matches, stopped = render.search_lines("a" * 300 + "\n" + "error here", "a*a*b|error")
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertEqual((matches, stopped), (1, False))
        # even a pattern that passes the guard cannot run past the budget: the scan stops and says so
        hostile = "\n".join(["a" * 2000] * 400)
        start = time.monotonic()
        _snippet, total, _matches, stopped = render.search_lines(hostile, "a*a*b", budget=0.3)
        self.assertLess(time.monotonic() - start, 3.0)
        self.assertTrue(stopped)
        self.assertEqual(total, 400)


class RegexGuardHandler(PluginTestCase):
    def test_log_search_refuses_a_nested_quantifier(self):
        job_id = self.call("gitlab_pipelines", project=1, action="jobs")["jobs"][0]["id"]
        out = self.call("gitlab_pipelines", project=1, action="log", job_id=job_id, search="(a+)+$")
        self.assertFalse(out["success"])
        self.assertIn("repeated group", out["error"])
        out = self.call("gitlab_pipelines", project=1, action="log", job_id=job_id, search="error|fail")
        self.assertTrue(out["success"], out)


class FormatSuffix(PluginTestCase):
    """GitLab routes a path with one format suffix (``variables.json``, ``.txt``, ``%2Ejson``) like the path
    without it, so the deny-lists must not be anchored on the literal last segment."""

    def test_deny_lists_match_paths_with_a_format_suffix(self):
        for path in (
            "projects/1/variables.json",
            "projects/1/VARIABLES.JSON",
            "projects/1/variables%2Ejson",
            "groups/2/variables.txt",
            "projects/1/hooks.foo",
            "projects/1/pipeline_schedules/5.json",
        ):
            self.assertTrue(self.call("gitlab_api", path=path).get("refused"), path)
        self.configure(allow_raw_writes=True, allow_raw_delete=True, allow_merge=True)
        head = self.gl.mrs[1][10]["sha"]
        before = len(self.gl.writes())
        for method, path, body in (
            ("PUT", "projects/1/merge_requests/10/merge.json", {"sha": head}),
            ("POST", "projects/1/merge_requests/10/approve.json", None),
            ("POST", "projects/1/repository/commits.json", {"branch": "main", "commit_message": "x", "actions": []}),
            ("POST", "projects/1/members.json", {"user_id": 2, "access_level": 50}),
            ("POST", "projects/1/protected_branches.json", {"name": "main"}),
            ("POST", "projects/1/fork.json", {"namespace_path": "elsewhere"}),
            ("DELETE", "personal_access_tokens/self.json", None),
        ):
            out = self.call("gitlab_api", method=method, path=path, body=body)
            self.assertTrue(out.get("refused"), (method, path, out))
        self.assertEqual(len(self.gl.writes()), before)
        self.assertEqual(self.gl.mrs[1][10]["state"], "opened")
        self.assertIn("projects/1/variables", executor.path_forms("projects/1/variables%2Ejson"))
        # the suffix is only dropped for matching: ordinary paths with one still go through
        self.assertIsNone(executor.raw_path_refusal("GET", "projects/1/issues.json"))
        self.assertIsNone(executor.raw_path_refusal("GET", "projects/1/repository/files/README.md"))
        self.assertIsNone(executor.raw_path_refusal("POST", "projects/1/labels.json"))


if __name__ == "__main__":
    unittest.main()
