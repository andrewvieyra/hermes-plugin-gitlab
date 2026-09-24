import unittest

from .helpers import submodule

targets = submodule("targets")
client_mod = submodule("client")

BASE = "https://gitlab.test"


class ParseUrl(unittest.TestCase):
    def test_merge_request_and_issue_urls(self):
        self.assertEqual(
            targets.parse_url(f"{BASE}/platform/api/-/merge_requests/12", BASE),
            {"project": "platform/api", "kind": "merge_request", "iid": 12},
        )
        self.assertEqual(
            targets.parse_url(f"{BASE}/platform/sub/api/-/issues/3#note_1", BASE),
            {"project": "platform/sub/api", "kind": "issue", "iid": 3},
        )
        self.assertEqual(targets.parse_url(f"{BASE}/platform/api/-/pipelines/100", BASE)["id"], 100)
        self.assertEqual(
            targets.parse_url(f"{BASE}/platform/api/-/jobs/1001", BASE),
            {"project": "platform/api", "kind": "job", "id": 1001},
        )

    def test_commit_blob_tree_and_project(self):
        self.assertEqual(targets.parse_url(f"{BASE}/platform/api/-/commit/abc123", BASE)["sha"], "abc123")
        blob = targets.parse_url(f"{BASE}/platform/api/-/blob/main/src/app.py", BASE)
        self.assertEqual((blob["kind"], blob["ref"], blob["path"]), ("blob", "main", "src/app.py"))
        self.assertEqual(targets.parse_url(f"{BASE}/platform/api", BASE), {"project": "platform/api", "kind": None})
        self.assertEqual(targets.parse_url(f"{BASE}/platform/api/merge_requests/7", BASE)["iid"], 7)  # legacy layout

    def test_other_host_and_non_urls(self):
        with self.assertRaises(client_mod.GitLabError):
            targets.parse_url("https://gitlab.com/platform/api/-/issues/1", BASE)
        self.assertIsNone(targets.parse_url("platform/api", BASE))
        self.assertIsNone(targets.parse_url(f"{BASE}/api/v4/projects/1", BASE))
        self.assertIsNone(targets.parse_url(f"{BASE}/onlygroup", BASE))


class ProjectArg(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(targets.project_arg(12), "12")
        self.assertEqual(targets.project_arg(" 12 "), "12")
        self.assertEqual(targets.project_arg("/platform/api/"), "platform/api")
        self.assertEqual(targets.project_arg(f"{BASE}/platform/api/-/merge_requests/1", BASE), "platform/api")
        for bad in (None, "", "platform", "bad path/x", True, "a//b"):
            with self.assertRaises(client_mod.GitLabError):
                targets.project_arg(bad, BASE)

    def test_ints_and_lists(self):
        self.assertEqual(targets.positive_int("7", "iid"), 7)
        for bad in (0, -1, "x", True, None):
            with self.assertRaises(client_mod.GitLabError):
                targets.positive_int(bad, "iid")
        self.assertIsNone(targets.optional_int("", "iid"))
        self.assertEqual(targets.str_list("a, b,,c"), ["a", "b", "c"])
        self.assertEqual(targets.str_list(["a", " b "]), ["a", "b"])
        self.assertEqual(targets.labels_arg(["bug", "backend"]), "bug,backend")
        self.assertIsNone(targets.labels_arg([]))

    def test_allowlist(self):
        self.assertTrue(targets.matches_allowlist("platform/api", []))
        self.assertTrue(targets.matches_allowlist("platform/api", ["platform/*"]))
        self.assertTrue(targets.matches_allowlist("platform/sub/api", ["platform/*"]))
        self.assertTrue(targets.matches_allowlist("Platform/API", ["platform/api"]))
        self.assertFalse(targets.matches_allowlist("other/api", ["platform/*", "andrew/sandbox"]))
        self.assertTrue(targets.matches_allowlist("andrew/sandbox", ["platform/*", "andrew/sandbox"]))

    def test_paths(self):
        self.assertEqual(targets.p_mr("platform/api", 3), "projects/platform%2Fapi/merge_requests/3")
        self.assertEqual(targets.p_file(1, "/src/app.py"), "projects/1/repository/files/src%2Fapp.py")
        self.assertEqual(targets.p_branch(1, "feature/login"), "projects/1/repository/branches/feature%2Flogin")
        self.assertEqual(targets.project_from_path("projects/platform%2Fapi/labels"), "platform/api")
        self.assertEqual(targets.project_from_path("projects/12"), "12")
        self.assertIsNone(targets.project_from_path("groups/1/projects"))


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


if __name__ == "__main__":
    unittest.main()
