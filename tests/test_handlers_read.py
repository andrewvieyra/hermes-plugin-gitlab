import json
import os
import unittest
from unittest import mock

from .base import PluginTestCase
from .fake_gitlab import SECRET_LINE
from .helpers import submodule

client_mod = submodule("client")


class Status(PluginTestCase):
    def test_status_reports_identity_and_scopes(self):
        out = self.call("gitlab_api", path="status")
        self.assertTrue(out["success"])
        self.assertEqual(out["gitlab"]["version"], "17.4.1")
        self.assertEqual(out["user"]["username"], "hermes-bot")
        self.assertEqual(out["token"]["scopes"], ["api"])
        self.assertEqual(out["plugin"]["write_mode"], "full")
        self.assertEqual(out["warnings"], [])
        self.assertIn("Authenticated as hermes-bot", out["report"])

    def test_status_warns_on_read_only_token_and_expiry(self):
        self.gl.token_info["scopes"] = ["read_api"]
        self.gl.token_info["expires_at"] = "2026-09-12"
        import datetime

        self.gl.token_info["expires_at"] = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
        out = self.call("gitlab_api", path="status")
        self.assertTrue(any("lacks the 'api' scope" in w for w in out["warnings"]))
        self.assertTrue(any("expires in 3 day" in w for w in out["warnings"]))

    def test_unconfigured_reports_config_error(self):
        client_mod = submodule("client")
        self.handlers.set_client_factory(lambda: client_mod.GitLabClient.from_env({}))
        out = self.call("gitlab_api", path="status")
        self.assertFalse(out["success"])
        self.assertIn("GITLAB_URL and GITLAB_TOKEN", out["error"])


class Search(PluginTestCase):
    def test_projects_by_name_and_namespace(self):
        out = self.call("gitlab_search", query="api")
        self.assertTrue(out["success"])
        self.assertEqual([p["path_with_namespace"] for p in out["results"]], ["platform/api"])
        sent = self.gl.calls[-1]["params"]
        self.assertTrue(sent["membership"])
        self.assertTrue(sent["search_namespaces"])
        out = self.call("gitlab_search", query="platform", scope="projects", membership=False)
        self.assertEqual(out["returned"], 2)
        self.assertNotIn("membership", self.gl.calls[-1]["params"])

    def test_group_scoped_projects(self):
        out = self.call("gitlab_search", query="", scope="projects", group="platform")
        self.assertFalse(out["success"])  # query is required
        out = self.call("gitlab_search", query="web", scope="projects", group="platform")
        self.assertEqual(out["results"][0]["path_with_namespace"], "platform/web")
        self.assertEqual(self.gl.calls[-1]["path"], "groups/platform/projects")

    def test_issue_search_in_project_and_globally(self):
        out = self.call("gitlab_search", query="login", scope="issues", project="platform/api", state="opened")
        self.assertEqual([i["iid"] for i in out["results"]], [1])
        self.assertEqual(self.gl.calls[-1]["path"], "projects/platform%2Fapi/search")
        out = self.call("gitlab_search", query="sandbox", scope="issues")
        self.assertEqual(out["results"][0]["project_id"], 3)

    def test_blobs_need_advanced_search(self):
        out = self.call("gitlab_search", query="TOKEN", scope="blobs", project="platform/api")
        self.assertFalse(out["success"])
        self.assertIn("advanced search", out["error"])
        self.gl.advanced_search = True
        self.gl.files[(1, "main")]["secrets.txt"] = f"TOKEN={SECRET_LINE}\n"
        out = self.call("gitlab_search", query="TOKEN", scope="blobs", project="platform/api")
        self.assertTrue(out["success"])
        self.assertGreaterEqual(out["returned"], 2)
        self.assertNotIn(SECRET_LINE, json.dumps(out))
        self.assertEqual(out["redacted"], 1)

    def test_bad_scope(self):
        self.assertFalse(self.call("gitlab_search", query="x", scope="planets")["success"])


class Repo(PluginTestCase):
    def test_tree(self):
        out = self.call("gitlab_repo", project="platform/api", action="tree")
        self.assertEqual({e["path"] for e in out["entries"]}, {"README.md", "src", "docs", "bin"})
        out = self.call("gitlab_repo", project="platform/api", action="tree", path="src", ref="feature/login")
        self.assertIn("src/login.py", [e["path"] for e in out["entries"]])
        out = self.call("gitlab_repo", project=1, action="tree", recursive=True)
        self.assertIn("docs/guide.md", [e["path"] for e in out["entries"]])

    def test_file_content_ranges_and_binary(self):
        out = self.call("gitlab_repo", project="platform/api", action="file", path="src/app.py")
        self.assertTrue(out["success"])
        self.assertIn("def main", out["content"])
        self.assertEqual(out["total_lines"], 7)
        self.assertEqual(out["ref"], "HEAD")
        out = self.call(
            "gitlab_repo", project="platform/api", action="file", path="docs/guide.md", start_line=2, end_line=4
        )
        self.assertEqual(out["content"], "line 1\nline 2\nline 3")
        self.assertEqual(out["lines"], "2-4")
        out = self.call("gitlab_repo", project="platform/api", action="file", path="bin/data.bin")
        self.assertTrue(out["binary"])
        self.assertIsNone(out["content"])
        out = self.call("gitlab_repo", project="platform/api", action="file", path="missing.txt")
        self.assertFalse(out["success"])
        self.assertEqual(out["status"], 404)

    def test_file_range_is_sliced_before_the_cap(self):
        self.configure(max_file_bytes=40)
        out = self.call(
            "gitlab_repo", project="platform/api", action="file", path="docs/guide.md", start_line=50, end_line=52
        )
        self.assertTrue(out["success"], out)
        self.assertEqual(out["content"], "line 49\nline 50\nline 51")
        self.assertEqual(out["lines"], "50-52")
        self.assertFalse(out["truncated"])
        self.assertEqual(out["total_lines"], 61)
        out = self.call("gitlab_repo", project="platform/api", action="file", path="docs/guide.md", start_line=60)
        self.assertEqual(out["lines"], "60-61")
        self.assertEqual(out["content"], "line 59\n")
        for args, message in (
            ({"start_line": 100}, "past the end"),
            ({"start_line": 5, "end_line": 2}, "before start_line"),
        ):
            out = self.call("gitlab_repo", project="platform/api", action="file", path="docs/guide.md", **args)
            self.assertFalse(out["success"], out)
            self.assertIn(message, out["error"])
            self.assertEqual(out["total_lines"], 61)

    def test_file_cap_and_redaction(self):
        self.configure(max_file_bytes=40)
        out = self.call("gitlab_repo", project="platform/api", action="file", path="docs/guide.md")
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["content"].encode()), 40)
        self.gl.files[(1, "main")]["cfg.env"] = f"GL={SECRET_LINE}\n"
        out = self.call("gitlab_repo", project="platform/api", action="file", path="cfg.env")
        self.assertNotIn(SECRET_LINE, out["content"])
        self.assertEqual(out["redacted"], 1)
        self.configure(redact_secrets=False)
        out = self.call("gitlab_repo", project="platform/api", action="file", path="cfg.env")
        self.assertIn(SECRET_LINE, out["content"])

    def test_commits_and_commit_with_diff(self):
        out = self.call("gitlab_repo", project="platform/api", action="commits", limit=2)
        self.assertEqual(out["returned"], 2)
        self.assertIsNone(out["count"])
        self.assertTrue(out["truncated"])
        out = self.call("gitlab_repo", project="platform/api", action="commits", ref="feature/login")
        self.assertEqual([c["title"] for c in out["commits"]], ["Wire login route", "Add login handler"])
        sha = out["commits"][0]["id"]
        out = self.call("gitlab_repo", project="platform/api", action="commit", sha=sha[:8])
        self.assertEqual(out["commit"]["title"], "Wire login route")
        self.assertIn("+from login import login", out["diff"])
        self.assertEqual(out["diff_summary"]["files"][0]["path"], "src/app.py")
        out = self.call("gitlab_repo", project="platform/api", action="commit", sha=sha, include_diff=False)
        self.assertNotIn("diff", out)

    def test_compare_branches_and_tags(self):
        out = self.call(
            "gitlab_repo", project="platform/api", action="compare", **{"from": "main", "to": "feature/login"}
        )
        self.assertEqual(len(out["commits"]), 2)
        self.assertIn("src/login.py", out["diff"])
        out = self.call("gitlab_repo", project="platform/api", action="branches")
        self.assertEqual({b["name"] for b in out["branches"]}, {"main", "feature/login", "old-feature"})
        out = self.call("gitlab_repo", project="platform/api", action="branches", name="feature/login")
        self.assertEqual(out["branch"]["commit"]["id"], self.gl.head(1, "feature/login"))
        out = self.call("gitlab_repo", project="platform/api", action="tags")
        self.assertEqual(out["tags"][0]["name"], "v1.0.0")

    def test_bad_inputs(self):
        self.assertIn(
            "action must be one of", self.call("gitlab_repo", project="platform/api", action="blame")["error"]
        )
        self.assertIn("project", self.call("gitlab_repo", action="tree")["error"])
        self.assertIn("path is required", self.call("gitlab_repo", project=1, action="file")["error"])
        self.assertEqual(self.call("gitlab_repo", project="nope/none", action="tree")["status"], 404)


class Issues(PluginTestCase):
    def test_list_filters(self):
        out = self.call(
            "gitlab_issues", project="platform/api", state="opened", labels=["bug", "backend"], assignee="@andrew"
        )
        self.assertEqual([i["iid"] for i in out["issues"]], [1])
        self.assertEqual(out["filters"]["labels"], "bug,backend")
        self.assertEqual(out["filters"]["assignee_username"], "andrew")
        out = self.call("gitlab_issues", project="platform/api", state="all")
        self.assertEqual(out["count"], 3)
        self.assertNotIn("description", out["issues"][0])
        out = self.call("gitlab_issues", project="platform/api", iids=[2, 3])
        self.assertEqual({i["iid"] for i in out["issues"]}, {2, 3})

    def test_global_list(self):
        out = self.call("gitlab_issues", author="andrew")
        self.assertEqual(self.gl.calls[-1]["path"], "issues")
        self.assertEqual(out["filters"]["scope"], "all")
        self.assertEqual([i["project_id"] for i in out["issues"]], [1, 1, 1, 3])

    def test_get_with_discussions_and_related(self):
        out = self.call("gitlab_issues", action="get", project="https://gitlab.test/platform/api/-/issues/1", iid=1)
        self.assertTrue(out["success"])
        self.assertEqual(out["issue"]["title"], "Login fails with 401 after token refresh")
        self.assertIn("description", out["issue"])
        self.assertEqual(len(out["discussions"]), 1)  # the system note thread is dropped
        self.assertEqual(out["discussions"][0]["notes"][1]["author"], "dana")
        self.assertEqual([m["iid"] for m in out["related_merge_requests"]], [10])
        out = self.call("gitlab_issues", action="get", project=1, iid=1, include_system=True)
        self.assertEqual(len(out["discussions"]), 2)
        out = self.call("gitlab_issues", action="get", project=1, iid=99)
        self.assertFalse(out["success"])
        self.assertEqual(out["status"], 404)


class MergeRequests(PluginTestCase):
    def test_list_filters_and_username_resolution(self):
        out = self.call("gitlab_merge_requests", project="platform/api", state="opened", draft=False)
        self.assertEqual([m["iid"] for m in out["merge_requests"]], [10])
        self.assertEqual(out["filters"]["wip"], "no")
        out = self.call("gitlab_merge_requests", project="platform/api", reviewer="andrew", state="all")
        self.assertEqual([m["iid"] for m in out["merge_requests"]], [10])
        self.gl.mrs[1][12]["assignees"] = [self.gl.user(3)]
        out = self.call("gitlab_merge_requests", project="platform/api", assignee="dana", state="all")
        self.assertEqual(out["filters"]["assignee_id"], 3)
        self.assertEqual([m["iid"] for m in out["merge_requests"]], [12])
        self.assertFalse(self.call("gitlab_merge_requests", project=1, assignee="ghost")["success"])
        out = self.call("gitlab_merge_requests", state="all")
        self.assertEqual(self.gl.calls[-1]["path"], "merge_requests")
        self.assertEqual(out["count"], 4)

    def test_get_has_sha_pipeline_and_approvals(self):
        out = self.call("gitlab_merge_requests", action="get", project="platform/api", iid=10)
        mr = out["merge_request"]
        self.assertEqual(mr["sha"], self.gl.head(1, "feature/login"))
        self.assertEqual(mr["head_pipeline"]["status"], "failed")
        self.assertEqual(mr["reviewers"], ["andrew"])
        self.assertIn("diff_refs", mr)
        self.assertEqual(out["approvals"]["approvals_left"], 1)
        read = next(c for c in self.gl.calls if c["path"].endswith("/merge_requests/10") and c["method"] == "GET")
        self.assertIn("include_diverged_commits_count", read["params"])

    def test_diffs_with_paths_and_cap(self):
        out = self.call("gitlab_merge_requests", action="diffs", project="platform/api", iid=10)
        summary = out["diff_summary"]
        self.assertEqual([f["path"] for f in summary["files"]], ["src/login.py", "src/app.py"])
        self.assertIn("+def login", out["diff"])
        out = self.call("gitlab_merge_requests", action="diffs", project="platform/api", iid=10, paths=["src/app.py"])
        self.assertEqual(out["diff_summary"]["total_files"], 1)
        self.assertNotIn("+def login", out["diff"])
        self.configure(max_diff_bytes=150)
        out = self.call("gitlab_merge_requests", action="diffs", project="platform/api", iid=10, max_bytes=100_000)
        summary = out["diff_summary"]
        self.assertTrue(summary["truncated"])
        self.assertEqual(len(summary["files"]), 2)
        self.assertEqual(summary["omitted"], ["src/app.py"])

    def test_discussions_are_scanned_across_pages(self):
        key = (1, "merge_requests", 10)
        for i in range(150):
            self.gl.add_discussion(key, [self.gl._note(2, f"nit {i}", resolvable=True)])
        out = self.call("gitlab_merge_requests", action="discussions", project="platform/api", iid=10, limit=5)
        self.assertEqual(out["returned"], 5)
        self.assertEqual(out["count"], 154)
        self.assertEqual(out["unresolved"], 151)  # counted over every page, not just the first
        self.assertTrue(out["truncated"])
        self.assertTrue(out["scanned_all"])
        pages = [c["params"].get("page") for c in self.gl.calls if c["path"].endswith("/discussions")]
        self.assertEqual(pages, [1, 2])
        out = self.call(
            "gitlab_merge_requests",
            action="discussions",
            project="platform/api",
            iid=10,
            only_unresolved=True,
            limit=100,
        )
        self.assertEqual(out["returned"], 100)
        self.assertEqual(out["unresolved"], 151)
        self.assertTrue(all(d["resolved"] is False for d in out["discussions"]))

    def test_discussions_filters(self):
        out = self.call("gitlab_merge_requests", action="discussions", project="platform/api", iid=10)
        self.assertEqual(len(out["discussions"]), 3)
        self.assertEqual(out["unresolved"], 1)
        first = out["discussions"][0]
        self.assertEqual(first["notes"][0]["position"]["new_line"], 2)
        self.assertFalse(first["resolved"])
        out = self.call(
            "gitlab_merge_requests", action="discussions", project="platform/api", iid=10, only_unresolved=True
        )
        self.assertEqual(len(out["discussions"]), 1)
        out = self.call(
            "gitlab_merge_requests", action="discussions", project="platform/api", iid=10, include_system=True
        )
        self.assertEqual(len(out["discussions"]), 4)

    def test_commits_and_pipelines(self):
        out = self.call("gitlab_merge_requests", action="commits", project="platform/api", iid=10)
        self.assertEqual(len(out["commits"]), 2)
        out = self.call("gitlab_merge_requests", action="pipelines", project="platform/api", iid=10)
        self.assertEqual(out["pipelines"][0]["status"], "failed")


class Pipelines(PluginTestCase):
    def failed_id(self):
        return next(p["id"] for p in self.gl.pipelines[1].values() if p["status"] == "failed")

    def test_list_and_latest(self):
        out = self.call("gitlab_pipelines", project="platform/api", status="failed")
        self.assertEqual([p["ref"] for p in out["pipelines"]], ["feature/login"])
        out = self.call("gitlab_pipelines", project="platform/api", latest=True)
        self.assertEqual(out["pipeline"]["ref"], "main")
        self.assertEqual(out["pipeline"]["status"], "success")

    def test_get_with_jobs_and_failures(self):
        out = self.call("gitlab_pipelines", project="platform/api", action="get", pipeline_id=self.failed_id())
        self.assertEqual(out["pipeline"]["status"], "failed")
        self.assertEqual(len(out["jobs"]), 4)
        self.assertEqual([j["name"] for j in out["failed_jobs"]], ["test"])  # lint allows failure
        self.assertEqual(out["stages"]["test"], {"failed": 2})
        out = self.call(
            "gitlab_pipelines", project="platform/api", action="jobs", pipeline_id=self.failed_id(), scope=["manual"]
        )
        self.assertEqual([j["name"] for j in out["jobs"]], ["deploy"])
        self.assertEqual(self.gl.calls[-1]["params"]["scope[]"], ["manual"])

    def test_job_log_tail_search_and_redaction(self):
        job_id = next(j["id"] for j in self.gl.jobs[1].values() if j["name"] == "test")
        out = self.call("gitlab_pipelines", project="platform/api", action="job", job_id=job_id)
        self.assertEqual(out["job"]["failure_reason"], "script_failure")
        out = self.call("gitlab_pipelines", project="platform/api", action="log", job_id=job_id, tail_lines=3)
        self.assertEqual(out["returned_lines"], 3)
        self.assertTrue(out["truncated"])
        self.assertIn("ERROR: Job failed", out["log"])
        self.assertNotIn("\x1b", out["log"])
        out = self.call("gitlab_pipelines", project="platform/api", action="log", job_id=job_id, search="token was")
        self.assertEqual(out["matches"], 1)
        self.assertNotIn(SECRET_LINE, out["log"])
        self.assertIn("[REDACTED]", out["log"])
        self.assertGreaterEqual(out["redacted"], 2)
        self.configure(max_log_lines=2)
        out = self.call("gitlab_pipelines", project="platform/api", action="log", job_id=job_id, tail_lines=500)
        self.assertEqual(out["returned_lines"], 2)


class RawApi(PluginTestCase):
    def test_get_with_pagination_headers(self):
        out = self.call("gitlab_api", path="/api/v4/projects/1/releases")
        self.assertTrue(out["success"])
        self.assertEqual(out["result"][0]["tag_name"], "v1.0.0")
        self.assertEqual(out["pagination"]["x-total"], "1")
        out = self.call("gitlab_api", path="projects/platform%2Fapi/labels", paginate=True, limit=1)
        self.assertEqual(out["returned"], 1)
        self.assertTrue(out["truncated"])

    def test_sensitive_paths_are_refused_before_any_call(self):
        before = len(self.gl.calls)
        out = self.call("gitlab_api", path="projects/1/variables")
        self.assertFalse(out["success"])
        self.assertTrue(out["refused"])
        self.assertEqual(len(self.gl.calls), before)
        self.assertEqual(self.events("write_refused")[-1]["action"], "api.GET")
        for path in (
            "personal_access_tokens",
            "projects/1/hooks",
            "application/settings",
            "user/keys",
            "projects/1/deploy_tokens",
        ):
            self.assertTrue(self.call("gitlab_api", path=path).get("refused"), path)
        self.assertTrue(self.call("gitlab_api", path="personal_access_tokens/self")["success"])

    def test_deny_list_matches_encoded_and_cased_paths(self):
        before = len(self.gl.calls)
        for path in (
            "projects/1/%76ariables",  # percent-encoded
            "projects/1/%2576ariables",  # double-encoded
            "PROJECTS/1/VARIABLES",  # upper-case
            "projects/platform%2Fapi/variables",  # encoded project path
        ):
            out = self.call("gitlab_api", path=path)
            self.assertTrue(out.get("refused"), path)
        self.assertEqual(len(self.gl.calls), before)
        self.assertIn("projects/1/variables", self.executor.path_forms("projects/1/%2576ariables"))
        self.assertIsNotNone(self.executor.raw_path_refusal("GET", "projects/1//variables"))
        self.assertIsNotNone(self.executor.raw_path_refusal("GET", "Projects/1/Hooks/"))
        self.assertIsNotNone(self.executor.raw_path_refusal("POST", "projects/1/%6Dembers"))
        self.assertIsNone(self.executor.raw_path_refusal("GET", "projects/platform%2Fapi/labels"))
        self.assertIsNone(self.executor.raw_path_refusal("GET", "projects/platform%2Fapi/members"))

    def test_bad_paths(self):
        self.assertIn("relative", self.call("gitlab_api", path="https://gitlab.test/api/v4/user")["error"])
        self.assertIn("query parameters", self.call("gitlab_api", path="projects?search=x")["error"])
        self.assertIn("invalid path", self.call("gitlab_api", path="projects/../admin")["error"])

    def test_audit_reads_when_enabled(self):
        self.call("gitlab_repo", project="platform/api", action="tree")
        self.assertEqual(self.events(), [])
        self.configure(audit_reads=True)
        self.call("gitlab_repo", project="platform/api", action="tree")
        self.call("gitlab_repo", project="platform/api", action="file", path="nope")
        done, failed = self.events("read_done"), self.events("read_failed")
        self.assertEqual(done[0]["action"], "repo.tree")
        self.assertEqual(done[0]["project"], "platform/api")
        self.assertEqual(done[0]["actor"]["kind"], "model")
        self.assertIn("404", failed[0]["error"])


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


class ReadExtras(PluginTestCase):
    def test_closes_issues_and_pipeline_name_filter(self):
        out = self.call("gitlab_merge_requests", action="get", project=1, iid=10)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["closes_issues"], [])
        out = self.call("gitlab_pipelines", project=1, name="nightly")
        self.assertTrue(out["success"], out)
        self.assertEqual(self.gl.calls[-1]["params"].get("name"), "nightly")


class LogSearchGuard(PluginTestCase):
    def test_log_search_refuses_long_and_backtracking_patterns(self):
        job_id = self.call("gitlab_pipelines", project=1, action="jobs")["jobs"][0]["id"]
        out = self.call("gitlab_pipelines", project=1, action="log", job_id=job_id, search="(a+)+" * 60)
        self.assertFalse(out["success"])
        self.assertIn("200 characters", out["error"])
        out = self.call("gitlab_pipelines", project=1, action="log", job_id=job_id, search="(a+)+$")
        self.assertFalse(out["success"])
        self.assertIn("repeated group", out["error"])
        out = self.call("gitlab_pipelines", project=1, action="log", job_id=job_id, search="error|fail")
        self.assertTrue(out["success"], out)


if __name__ == "__main__":
    unittest.main()
