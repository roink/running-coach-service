"""No operational services, private data or real model calls are used."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/running-coach"))
from coach.artifacts import read_export
from coach.queue import Queue


class QueueTests(unittest.TestCase):
    def test_paused_automatic_work_does_not_block_interactive_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = Queue(Path(directory) / "queue.sqlite3")
            queue.enqueue("workout-fixture", {"text": "automatic"})
            queue.enqueue("interactive-fixture", {"text": "interactive"})
            self.assertEqual(queue.claim(allow_automatic=False)["id"], "interactive-fixture")
            self.assertIsNone(queue.claim(allow_automatic=False))
            self.assertEqual(queue.claim()["id"], "workout-fixture")
            queue.close()

    def test_duplicate_and_restart_do_not_replay_ambiguous_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.sqlite3"
            queue = Queue(path)
            self.assertTrue(queue.enqueue("event:1", {"text": "fixture"}))
            self.assertFalse(queue.enqueue("event:1", {"text": "duplicate"}))
            self.assertEqual(queue.claim()["id"], "event:1")
            queue.attach_turn("event:1", "thread-fixture", "turn-fixture")
            queue.close()
            queue = Queue(path)
            self.assertIsNone(queue.claim())
            self.assertEqual(queue.interrupted()[0]["turn_id"], "turn-fixture")
            queue.complete("event:1", {"text": "Finished", "images": []})
            self.assertEqual(len(queue.pending_deliveries()), 1)
            queue.mark_delivered("event:1")
            self.assertEqual(queue.pending_deliveries(), [])
            queue.close()


class ArtifactTests(unittest.TestCase):
    def test_rejects_escape_symlink_fifo_and_oversize(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "chart.png").write_bytes(b"fixture")
            (root / "link.png").symlink_to(root / "chart.png")
            (root / "linkdir").symlink_to(root, target_is_directory=True)
            os.mkfifo(root / "pipe")
            self.assertEqual(read_export(root, "chart.png"), b"fixture")
            for path in ["../chart.png", "/etc/passwd", "link.png", "linkdir/chart.png", "pipe"]:
                with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                    read_export(root, path)
            with self.assertRaises(ValueError):
                read_export(root, "chart.png", limit=2)


if __name__ == "__main__":
    unittest.main()
