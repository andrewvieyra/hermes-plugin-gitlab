import unittest

from .base import PluginTestCase
from .fake_gitlab import FakeGitLab, _Response, seeded
from .helpers import submodule

client_mod = submodule("client")


class AuthAndConfig(unittest.TestCase):
    def test_auth_headers(self):
        self.assertEqual(client_mod.auth_headers("glpat-abc"), {"PRIVATE-TOKEN": "glpat-abc"})
        self.assertEqual(client_mod.auth_headers("Bearer oauth-token"), {"Authorization": "Bearer oauth-token"})
        self.assertEqual(client_mod.auth_headers("  bearer x "), {"Authorization": "Bearer x"})

    def test_verify_from_env(self):
        self.assertTrue(client_mod.verify_from_env(None))
        self.assertTrue(client_mod.verify_from_env("true"))
        self.assertFalse(client_mod.verify_from_env("false"))
        self.assertFalse(client_mod.verify_from_env("0"))
        self.assertEqual(client_mod.verify_from_env("/etc/ssl/private-ca.pem"), "/etc/ssl/private-ca.pem")

    def test_settings_from_env(self):
        env = {
            "GITLAB_URL": "https://gitlab.example.com/api/v4/",
            "GITLAB_TOKEN": "t",
            "GITLAB_VERIFY_SSL": "false",
            "GITLAB_TIMEOUT": "5",
        }
        s = client_mod.settings_from_env(env)
        self.assertEqual(s, {"url": "https://gitlab.example.com", "token": "t", "verify": False, "timeout": 5.0})
        with self.assertRaises(client_mod.ConfigError):
            client_mod.settings_from_env({"GITLAB_URL": "https://x"})
        with self.assertRaises(client_mod.ConfigError):
            client_mod.settings_from_env({"GITLAB_URL": "x", "GITLAB_TOKEN": "t"})
        self.assertFalse(client_mod.is_configured({}))
        self.assertTrue(client_mod.is_configured({"GITLAB_URL": "http://x", "GITLAB_TOKEN": "t"}))

    def test_encoding(self):
        self.assertEqual(client_mod.project_id("group/sub/proj"), "group%2Fsub%2Fproj")
        self.assertEqual(client_mod.project_id(12), "12")
        self.assertEqual(client_mod.project_id("12"), "12")
        self.assertEqual(client_mod.encode("src/app.py"), "src%2Fapp.py")
        with self.assertRaises(client_mod.GitLabError):
            client_mod.project_id("")

    def test_validate_path(self):
        self.assertEqual(client_mod.validate_path("/api/v4/projects/1/releases/"), "projects/1/releases")
        self.assertEqual(client_mod.validate_path("projects/group%2Fproj/labels"), "projects/group%2Fproj/labels")
        for bad in ("https://gitlab.test/api/v4/user", "projects?x=1", "../etc", "", "projects/1/../2", "a b"):
            with self.assertRaises(client_mod.GitLabError):
                client_mod.validate_path(bad)

    def test_error_summary(self):
        self.assertEqual(client_mod.error_summary({"message": "404 Project Not Found"}), "404 Project Not Found")
        self.assertEqual(client_mod.error_summary({"message": {"title": ["can't be blank"]}}), "title: can't be blank")
        self.assertEqual(
            client_mod.error_summary({"error": "invalid_token", "error_description": "expired"}), "expired"
        )
        self.assertEqual(client_mod.error_summary(["Source branch not found"]), "Source branch not found")


class Requests(unittest.TestCase):
    def setUp(self):
        self.gl = seeded()
        self.slept = []
        self.client = client_mod.GitLabClient(self.gl.base_url, self.gl.token, session=self.gl, sleep=self.slept.append)

    def test_version_and_auth(self):
        self.assertEqual(self.client.version()["version"], "17.4.1")
        sent = self.gl.calls[-1]["headers"]
        self.assertEqual(sent["PRIVATE-TOKEN"], self.gl.token)
        self.assertNotIn("Content-Type", sent)  # no body, no content type
        bad = client_mod.GitLabClient(self.gl.base_url, "wrong-token-value", session=self.gl)
        with self.assertRaises(client_mod.GitLabError) as ctx:
            bad.version()
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("401", str(ctx.exception))

    def test_404_structured(self):
        with self.assertRaises(client_mod.GitLabError) as ctx:
            self.client.project("nope/nothing")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.to_dict()["body"]["message"], "404 Project Not Found")

    def test_validation_errors_are_field_errors(self):
        with self.assertRaises(client_mod.GitLabError) as ctx:
            self.client.request("POST", "projects/1/issues", json={"title": "x" * 300})
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(list(ctx.exception.field_errors()), ["title"])

    def test_paginate_follows_next_page_and_caps(self):
        gl = FakeGitLab()
        for uid in (1, 2, 3):
            gl.users[uid] = {"id": uid, "username": f"u{uid}", "name": f"U{uid}", "state": "active"}
        gl.add_project(1, "g/p")
        gl.add_commit(1, "main", "init", {"README.md": "x"})
        for i in range(1, 8):
            gl.add_issue(1, i, f"issue {i}")
        c = client_mod.GitLabClient(gl.base_url, gl.token, session=gl)
        rows, total, truncated = c.paginate("projects/1/issues", max_results=5, per_page=2)
        self.assertEqual([r["iid"] for r in rows], [1, 2, 3, 4, 5])
        self.assertEqual(total, 7)
        self.assertTrue(truncated)
        self.assertEqual(len([x for x in gl.calls if x["method"] == "GET"]), 3)
        rows, total, truncated = c.paginate("projects/1/issues", max_results=50, per_page=3)
        self.assertEqual(len(rows), 7)
        self.assertFalse(truncated)

    def test_paginate_without_total_header(self):
        rows, total, truncated = self.client.paginate("projects/1/repository/commits", max_results=2, per_page=2)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(total)
        self.assertTrue(truncated)  # a next page exists
        rows, total, truncated = self.client.paginate("projects/1/repository/commits", max_results=100)
        self.assertFalse(truncated)

    def test_429_retries_once_with_retry_after(self):
        self.gl.rate_limit_once = True
        self.assertEqual(self.client.version()["version"], "17.4.1")
        self.assertEqual(self.slept, [0.0])
        self.assertEqual([c["path"] for c in self.gl.calls], ["version", "version"])

    def test_token_is_scrubbed_from_errors(self):
        token = self.gl.token

        def leak(method, path, params, body):
            if path == "version":
                return _Response(500, {"message": f"boom {token} boom"})
            return None

        self.gl.fail_on = leak
        with self.assertRaises(client_mod.GitLabError) as ctx:
            self.client.version()
        self.assertNotIn(token, str(ctx.exception))
        self.assertIn("***", str(ctx.exception))

    def test_transport_error_wrapped(self):
        class Boom:
            def request(self, *a, **k):
                raise ConnectionError("refused")

        c = client_mod.GitLabClient("https://x", "glpat-a", session=Boom())
        with self.assertRaises(client_mod.GitLabError) as ctx:
            c.version()
        self.assertEqual(ctx.exception.status, 0)
        self.assertIn("refused", str(ctx.exception))

    def test_raw_text_and_token_self(self):
        job_id = next(j for j in self.gl.jobs[1].values() if j["name"] == "test")["id"]
        text = self.client.request("GET", f"projects/1/jobs/{job_id}/trace", raw=True)
        self.assertIn("pytest", text)
        self.assertEqual(self.client.token_self()["scopes"], ["api"])
        self.gl.token_info = None
        self.assertIsNone(self.client.token_self())

    def test_user_lookup(self):
        self.assertEqual(self.client.user_by_username("@Andrew")["id"], 2)
        self.assertIsNone(self.client.user_by_username("ghost"))


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


class PathValidation(PluginTestCase):
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

    def test_encoded_query_and_fragment_are_rejected(self):
        # `?` and `=` are refused as given; encoded, they pass the character check and a decoding proxy
        # would turn `issues%3Fsudo%3Droot` into a query string the params check never saw
        before = len(self.gl.calls)
        for path in (
            "projects/1/issues%3Fsudo%3Droot",
            "projects/1/issues%253Fsudo%253Droot",
            "projects/1/issues%23frag",
            "projects/1/issues%3fsudo%3droot",
        ):
            with self.assertRaises(client_mod.GitLabError, msg=path):
                client_mod.validate_path(path)
            out = self.call("gitlab_api", path=path)
            self.assertFalse(out["success"], (path, out))
        self.assertEqual(len(self.gl.calls), before)
        self.assertEqual(client_mod.validate_path("projects/platform%2Fapi/issues"), "projects/platform%2Fapi/issues")


if __name__ == "__main__":
    unittest.main()
