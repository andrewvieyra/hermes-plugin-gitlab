import re
import unittest

from .helpers import REPO_ROOT, submodule


class VersionConsistency(unittest.TestCase):
    def test_all_versions_agree(self):
        version = submodule("version").__version__
        manifest = (REPO_ROOT / "plugin.yaml").read_text()
        self.assertRegex(manifest, rf"(?m)^version: {re.escape(version)}$", "plugin.yaml")
        skill = (REPO_ROOT / "SKILL.md").read_text()
        self.assertRegex(skill, rf"(?m)^version: {re.escape(version)}$", "SKILL.md")
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text()
        self.assertIn(f"## [{version}]", changelog)

    def test_manifest_lists_every_tool(self):
        manifest = (REPO_ROOT / "plugin.yaml").read_text()
        schemas = submodule("schemas")
        for schema in schemas.ALL:
            self.assertIn(f"  - {schema['name']}\n", manifest)
        self.assertEqual(len(schemas.ALL), 10)
        settings = submodule("settings").Settings()
        for name in settings.to_dict():
            self.assertIn(f"  {name}:\n", manifest, name)

    def test_readme_documents_every_setting_and_tool(self):
        readme = (REPO_ROOT / "README.md").read_text()
        for name in submodule("settings").Settings().to_dict():
            self.assertIn(name, readme, name)
        for schema in submodule("schemas").ALL:
            self.assertIn(f"`{schema['name']}`", readme, schema["name"])

    def test_schema_shapes(self):
        for schema in submodule("schemas").ALL:
            self.assertEqual(schema["parameters"]["type"], "object")
            self.assertFalse(schema["parameters"]["additionalProperties"])
            for required in schema["parameters"].get("required", []):
                self.assertIn(required, schema["parameters"]["properties"], (schema["name"], required))
            self.assertLess(len(schema["description"]), 1200, schema["name"])


if __name__ == "__main__":
    unittest.main()
