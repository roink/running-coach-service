"""Synthetic worker turns; no model calls, NAS access or private athlete data."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/running-coach"))
from coach.worker import Worker


class WorkerContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_notes_are_not_injected_and_followup_resumes_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            baseline = workspace / "AGENTS.md"
            baseline.write_text("Keep training sustainable.")
            worker = Worker(root, workspace, root, "fixture")
            worker.rpc = AsyncMock()
            worker.rpc.request.side_effect = [
                {"thread": {"id": "shared-thread"}},
                {"turn": {"id": "workout-turn"}},
                {},
                {"turn": {"id": "chat-turn"}},
            ]
            worker.rpc.final_answer.return_value = '{"text":"fixture answer","images":[]}'
            try:
                for job_id, text in [("workout-fixture", "Review run"), ("chat", "Why easy?")]:
                    worker.queue.enqueue(job_id, {"text": text})
                    job = worker.queue.claim()
                    await worker.analyze(job)
                    self.assertEqual(worker.queue.job(job_id)["state"], "completed")
                    baseline.write_text("Updated essential preference.")
                    (workspace / "ATHLETE.md").write_text("Optional detail sentinel")
                calls = worker.rpc.request.call_args_list
                self.assertEqual(
                    [c.args[0] for c in calls],
                    ["thread/start", "turn/start", "thread/resume", "turn/start"],
                )
                for call in [calls[1], calls[3]]:
                    self.assertEqual(call.args[1]["threadId"], "shared-thread")
                    self.assertNotIn("Optional detail sentinel", call.args[1]["input"][0]["text"])
                self.assertIn("Keep training sustainable.", calls[1].args[1]["input"][0]["text"])
                self.assertIn("Updated essential preference.", calls[3].args[1]["input"][0]["text"])
            finally:
                worker.queue.close()
