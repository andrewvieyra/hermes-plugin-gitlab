"""Load the plugin through Hermes' real ``PluginManager`` when a Hermes checkout is importable.

Set ``HERMES_AGENT_ROOT`` (or run from an environment where ``hermes_cli`` imports) to enable.
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _hermes_importable() -> bool:
    root = os.environ.get("HERMES_AGENT_ROOT")
    if root and root not in sys.path:
        sys.path.insert(0, root)
    return importlib.util.find_spec("hermes_cli") is not None


@unittest.skipUnless(_hermes_importable(), "hermes_cli not importable (set HERMES_AGENT_ROOT)")
class LoadThroughPluginManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = Path(self.tmp) / "hermes-home"
        (self.home / "plugins").mkdir(parents=True)
        shutil.copytree(
            REPO_ROOT,
            self.home / "plugins" / "gitlab",
            ignore=shutil.ignore_patterns("tests", ".git", "__pycache__", ".github"),
        )
        (self.home / "config.yaml").write_text(
            'plugins:\n  enabled: [gitlab]\n  entries:\n    gitlab:\n      settings:\n        allow_merge: true\n        max_results: 7\n        write_projects: ["platform/*"]\n'
        )
        self._env = dict(os.environ)
        os.environ["HERMES_HOME"] = str(self.home)
        os.environ["GITLAB_URL"] = "https://gitlab.test"
        os.environ["GITLAB_TOKEN"] = "glpat-test"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_registration_through_real_loader(self):
        from hermes_cli.plugins import PluginManager
        from tools.registry import registry

        mgr = PluginManager()
        mgr.discover_and_load()
        loaded = {p["name"]: p for p in mgr.list_plugins()}
        self.assertIn("gitlab", loaded, loaded)
        self.assertIsNone(loaded["gitlab"]["error"])
        self.assertEqual(loaded["gitlab"]["tools"], 10)
        self.assertGreaterEqual(loaded["gitlab"]["commands"], 1)
        for name in (
            "gitlab_search",
            "gitlab_repo",
            "gitlab_issues",
            "gitlab_merge_requests",
            "gitlab_pipelines",
            "gitlab_api",
            "gitlab_issue_write",
            "gitlab_mr_write",
            "gitlab_pipeline_write",
            "gitlab_commit",
        ):
            self.assertIsNotNone(registry.get_schema(name), name)
            self.assertEqual(registry.get_toolset_for_tool(name), "gitlab")
        self.assertIsNotNone(mgr.find_plugin_skill("gitlab:workflow"))

        mod = sys.modules["hermes_plugins.gitlab"]
        self.assertEqual(mod.settings.get_settings().max_results, 7)
        self.assertTrue(mod.settings.get_settings().allow_merge)
        self.assertEqual(mod.settings.get_settings().write_projects, ["platform/*"])

        # a dispatched call goes through the registry and comes back as structured JSON (no network: bad path)
        out = json.loads(registry.dispatch("gitlab_api", {"path": "projects/1/variables"}))
        self.assertFalse(out["success"])
        self.assertTrue(out["refused"])


if __name__ == "__main__":
    unittest.main()
