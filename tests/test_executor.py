"""The write gate: raw-path deny-lists, typed-only endpoints, authentication parameters and previews."""

import unittest

from .base import PluginTestCase
from .helpers import submodule

executor = submodule("executor")


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

    def test_deny_lists_match_paths_with_a_format_suffix(self):
        # GitLab routes a path with one format suffix (variables.json, .txt, %2Ejson) like the path without
        # it, so the lists must not be anchored on the literal last segment
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


class TypedOnly(PluginTestCase):
    def test_raw_merge_approve_commit_and_token_revocation_are_refused(self):
        self.configure(allow_raw_writes=True, allow_raw_delete=True, allow_merge=True)
        head = self.gl.mrs[1][10]["sha"]
        before = len(self.gl.writes())
        for method, path, body in (
            ("PUT", "projects/1/merge_requests/10/merge", {"sha": head}),
            ("PUT", "projects/platform%2Fapi/merge_requests/10/merge", None),
            ("POST", "projects/1/merge_requests/10/approve", {"sha": head}),
            ("POST", "projects/1/repository/commits", {"branch": "main", "commit_message": "x", "actions": []}),
            (
                "PUT",
                "projects/1/repository/files/README%2Emd",
                {"branch": "main", "content": "x", "commit_message": "x"},
            ),
            ("POST", "projects/1/repository/files/new.txt", {"branch": "main", "content": "x", "commit_message": "x"}),
            ("DELETE", "projects/1/repository/files/old.txt", None),
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
        out = self.call("gitlab_api", method="PUT", path="projects/1/repository/files/README.md", body={"content": "x"})
        self.assertIn("gitlab_commit", out["error"])
        self.assertIsNone(executor.raw_path_refusal("GET", "projects/1/repository/files/README.md"))
        self.assertIsNone(executor.raw_path_refusal("GET", "projects/1/repository/files/README.md/raw"))
        self.assertIsNone(executor.raw_path_refusal("POST", "projects/1/repository/commits/abc123/cherry_pick"))


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


if __name__ == "__main__":
    unittest.main()
