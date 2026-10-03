import json
import os
import threading
import time
import unittest

from transfer_web.endpoints import EndpointClient
from transfer_web.errors import AppError
from transfer_web.tasks import TaskManager, safe_utf8_cut

from .helpers import TempDirTestCase, local_config, needs_rsync


def make_manager(config):
    clients = {endpoint_id: EndpointClient(endpoint) for endpoint_id, endpoint in config.endpoints.items()}
    return TaskManager(config, clients)


def wait_for(manager, task_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = next(task for task in manager.history()["tasks"] if task["id"] == task_id)
        if task["status"] not in ("queued", "running"):
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not finish")


class Utf8CutTests(unittest.TestCase):
    def test_never_splits_a_character(self):
        data = "日志 a😀b".encode("utf-8")
        for end in range(len(data) + 1):
            safe_utf8_cut(data[:end]).decode("utf-8")


class TaskManagerTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.config = local_config(self.tmp)
        self.a = self.tmp / "a"
        self.b = self.tmp / "b"

    def test_rejects_bad_requests(self):
        manager = make_manager(self.config)
        bad = [
            {"from": "a", "to": "a", "items": ["x"]},
            {"from": "a", "to": "zzz", "items": ["x"]},
            {"from": "a", "to": "b", "items": []},
            {"from": "a", "to": "b", "items": [""]},
            {"from": "a", "to": "b", "items": ["../etc"]},
            {"from": "a", "to": "b", "items": ["x", "x"]},
        ]
        for payload in bad:
            with self.subTest(payload), self.assertRaises(AppError):
                manager.submit(payload)
        manager.shutdown()

    @needs_rsync
    def test_transfer_updates_changed_files_of_equal_or_smaller_size(self):
        """Regression for --append-verify, which silently skipped these files."""
        (self.a / "proj").mkdir()
        (self.a / "proj" / "same_size.txt").write_text("NEW-CONTENT-1")
        (self.a / "proj" / "shrunk.txt").write_text("short")
        (self.b / "proj").mkdir()
        (self.b / "proj" / "same_size.txt").write_text("OLD-CONTENT-1")
        (self.b / "proj" / "shrunk.txt").write_text("much longer old content")
        for path in (self.b / "proj").iterdir():
            os.utime(path, (0, 0))

        manager = make_manager(self.config)
        preview = manager.preview({"from": "a", "to": "b", "items": ["proj"], "destPath": ""})
        self.assertEqual(preview["updatedCount"], 2)
        task = manager.submit({"from": "a", "to": "b", "items": ["proj"], "destPath": ""})
        done = wait_for(manager, task["id"])
        manager.shutdown()

        self.assertEqual(done["status"], "completed", done["error"])
        self.assertEqual((self.b / "proj" / "same_size.txt").read_text(), "NEW-CONTENT-1")
        self.assertEqual((self.b / "proj" / "shrunk.txt").read_text(), "short")
        self.assertEqual(done["stats"]["transferredFiles"], 2)

    @needs_rsync
    def test_queue_history_log_and_missing_destination_preview(self):
        (self.a / "one.txt").write_text("1" * 1000)
        (self.a / "two.txt").write_text("2")
        manager = make_manager(self.config)
        preview = manager.preview({"from": "a", "to": "b", "items": ["one.txt", "two.txt"], "destPath": "new/deep"})
        self.assertFalse(preview["destExists"])
        self.assertEqual((preview["newFiles"], preview["transferSize"]), (2, 1001))

        first = manager.submit({"from": "a", "to": "b", "items": ["one.txt"], "destPath": "new/deep"})
        second = manager.submit({"from": "a", "to": "b", "items": ["two.txt"], "destPath": "new/deep", "skipExisting": True})
        self.assertEqual(wait_for(manager, first["id"])["status"], "completed")
        self.assertEqual(wait_for(manager, second["id"])["status"], "completed")
        self.assertTrue((self.b / "new" / "deep" / "two.txt").exists())

        history = manager.history()["tasks"]
        self.assertEqual([task["id"] for task in history[:2]], [second["id"], first["id"]])
        log = manager.read_log(first["id"])
        self.assertIn("one.txt", log["data"])
        self.assertNotIn("\r", log["data"])
        self.assertEqual(manager.read_log(first["id"], log["offset"])["data"], "")
        with self.assertRaises(AppError):
            manager.read_log("../../etc/passwd")
        manager.shutdown()

    def test_unfinished_tasks_become_interrupted_after_restart(self):
        self.config.state_dir.mkdir(parents=True)
        record = {"id": "20260101T000000Z-deadbeef", "from": "a", "to": "b", "items": ["x"], "destPath": "", "status": "running"}
        (self.config.state_dir / "tasks.json").write_text(json.dumps([record]))
        manager = make_manager(self.config)
        status = manager.status()
        manager.shutdown()
        self.assertIsNone(status["active"])
        self.assertEqual(status["last"]["status"], "interrupted")

    def test_cancel_queued_task(self):
        manager = make_manager(self.config)
        started = threading.Event()
        release = threading.Event()

        def blocking_run(task):
            started.set()
            release.wait(5)

        manager._run = blocking_run  # keep the first task "running"
        (self.a / "x").write_text("x")
        first = manager.submit({"from": "a", "to": "b", "items": ["x"], "destPath": ""})
        self.assertTrue(started.wait(5))
        second = manager.submit({"from": "a", "to": "b", "items": ["x"], "destPath": "other"})
        self.assertEqual([task["id"] for task in manager.status()["queued"]], [second["id"]])
        result = manager.cancel(second["id"])
        self.assertEqual(result["task"]["status"], "cancelled")
        self.assertEqual(manager.status()["queued"], [])
        release.set()
        wait_for(manager, first["id"])
        manager.shutdown()

    def test_malformed_history_is_ignored(self):
        self.config.state_dir.mkdir(parents=True)
        records = [{"id": "../../etc/passwd", "status": "completed"}, "junk", {"no": "id"}]
        (self.config.state_dir / "tasks.json").write_text(json.dumps(records))
        manager = make_manager(self.config)
        self.assertEqual(manager.history()["tasks"], [])
        manager.shutdown()

    def test_history_is_trimmed_to_limit(self):
        self.config.history_limit = 3
        self.config.state_dir.mkdir(parents=True)
        records = [
            {"id": f"20260101T00000{i}Z-0000000{i}", "from": "a", "to": "b", "items": ["x"], "destPath": "", "status": "completed"}
            for i in range(6)
        ]
        (self.config.state_dir / "tasks.json").write_text(json.dumps(records))
        manager = make_manager(self.config)
        self.assertEqual(len(manager.history()["tasks"]), 3)
        manager.shutdown()


if __name__ == "__main__":
    unittest.main()
