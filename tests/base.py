from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from .fake_gitlab import FakeGitLab, seeded
from .helpers import submodule


class PluginTestCase(unittest.TestCase):
    """Fresh fake GitLab, client, temp staged store, temp audit log and default settings for every test."""

    def setUp(self) -> None:
        self.client_mod = submodule("client")
        self.store_mod = submodule("store")
        self.settings_mod = submodule("settings")
        self.handlers = submodule("handlers")
        self.executor = submodule("executor")
        self.writes = submodule("writes")
        self.render = submodule("render")
        self.targets = submodule("targets")
        self.commands = submodule("commands")
        self.audit = submodule("audit")
        self.sinks = submodule("sinks")
        self.gl: FakeGitLab = seeded()
        self.client = self.client_mod.GitLabClient(
            self.gl.base_url, self.gl.token, session=self.gl, sleep=lambda _s: None
        )
        self._tmp = tempfile.TemporaryDirectory()
        self.store = self.store_mod.StagedStore(Path(self._tmp.name) / "staged")
        self.store_mod.set_store(self.store)
        self.settings = self.settings_mod.Settings()
        self.settings_mod.set_settings(self.settings)
        self.handlers.set_client_factory(lambda: self.client)
        self.audit_path = Path(self._tmp.name) / "audit.jsonl"
        self.audit.set_audit_log(self.audit.AuditLog(self.audit_path))

    def tearDown(self) -> None:
        self.sinks.set_worker(None)
        self.audit.set_audit_log(None)
        self.handlers.set_client_factory(None)
        self.store_mod.set_store(None)
        self.settings_mod.set_settings(self.settings_mod.Settings())
        self._tmp.cleanup()

    def configure(self, **overrides):
        self.settings = self.settings_mod.Settings(**overrides)
        self.settings_mod.set_settings(self.settings)
        return self.settings

    def call(self, _tool: str, **args):
        return json.loads(self.handlers.HANDLERS[_tool](args))

    def call_kw(self, _tool: str, args: dict, **kwargs):
        return json.loads(self.handlers.HANDLERS[_tool](args, **kwargs))

    def events(self, event: str | None = None):
        if not self.audit_path.exists():
            return []
        rows = [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines() if line]
        return [r for r in rows if event is None or r["event"] == event]
