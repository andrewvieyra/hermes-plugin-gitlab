import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .helpers import submodule

store_mod = submodule("store")


def _doc(staged_id, status="staged", **extra):
    doc = {"id": staged_id, "status": status, "created_at": store_mod.now_iso(), "request": {"summary": "s"}}
    doc.update(extra)
    return doc


class StagedStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_mod.StagedStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_is_exclusive_and_mode_0600(self):
        doc = _doc("glw-20260909T120000Z-aaaa")
        self.store.create(doc)
        path = Path(self.tmp.name) / "glw-20260909T120000Z-aaaa.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        clash = _doc("glw-20260909T120000Z-aaaa")
        self.store.create(clash)
        self.assertNotEqual(clash["id"], "glw-20260909T120000Z-aaaa")
        self.assertEqual(len(self.store.list()), 2)
        self.assertEqual(json.loads(path.read_text())["status"], "staged")

    def test_save_load_resolve(self):
        self.store.create(_doc("glw-20260909T120000Z-4f1a"))
        self.store.create(_doc("glw-20260909T120001Z-4f1b"))
        self.assertEqual(self.store.resolve("4f1a"), ("glw-20260909T120000Z-4f1a", ["glw-20260909T120000Z-4f1a"]))
        self.assertEqual(self.store.resolve("4f1")[0], None)
        self.assertEqual(len(self.store.resolve("4f1")[1]), 2)
        self.assertEqual(self.store.resolve("")[1], [])
        self.assertEqual(self.store.resolve("../x")[1], [])
        doc = self.store.load("glw-20260909T120000Z-4f1a")
        doc["status"] = "done"
        self.store.save(doc)
        self.assertEqual(self.store.load("glw-20260909T120000Z-4f1a")["status"], "done")
        self.assertEqual([d["id"] for d in self.store.list(status="staged")], ["glw-20260909T120001Z-4f1b"])
        self.assertIsNone(self.store.load("glw-missing"))
        self.assertIsNone(self.store.load("../../etc/passwd"))

    def test_prune_keeps_staged_and_running(self):
        old = (
            (datetime.now(timezone.utc) - timedelta(days=100)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        for status in ("done", "staged", "running", "failed"):
            doc = _doc(f"glw-20260101T000000Z-{status[:4]}", status)
            self.store.create(doc)
            doc["updated_at"] = old
            path = Path(self.tmp.name) / f"{doc['id']}.json"
            path.write_text(json.dumps(doc))
        removed = self.store.prune(90, dry_run=True)
        self.assertEqual(sorted(r["status"] for r in removed), ["done", "failed"])
        self.assertEqual(len(self.store.list()), 4)
        self.store.prune(90)
        self.assertEqual(sorted(d["status"] for d in self.store.list()), ["running", "staged"])
        self.assertEqual(self.store.prune(0), [])

    def test_exclusive_lock_times_out(self):
        held = threading.Event()
        release = threading.Event()

        def holder():
            other = store_mod.StagedStore(Path(self.tmp.name))
            with other.exclusive(1.0):
                held.set()
                release.wait(5)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        held.wait(5)
        with self.assertRaises(TimeoutError), self.store.exclusive(0.2):
            pass
        release.set()
        thread.join(5)
        with self.store.exclusive(1.0):
            pass

    def test_create_gives_up_after_attempts(self):
        original = store_mod.new_id
        store_mod.new_id = lambda: "glw-20260909T190000Z-bbbb"
        try:
            self.store.create(_doc("glw-20260909T190000Z-bbbb"))
            with self.assertRaises(RuntimeError):
                self.store.create(_doc("glw-20260909T190000Z-bbbb"), attempts=3)
        finally:
            store_mod.new_id = original

    def test_ids_and_default_dir(self):
        a, b = store_mod.new_id(), store_mod.new_id()
        self.assertTrue(a.startswith("glw-") and "Z-" in a)
        self.assertNotEqual(a, b)
        self.assertTrue(str(store_mod.default_staged_dir()).endswith("staged"))


if __name__ == "__main__":
    unittest.main()
