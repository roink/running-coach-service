"""Worker maintenance must preserve queued work and resume the existing conversation."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/running-coach"))
from aiohttp import web
from coach.worker import Worker


class WorkerMaintenance(unittest.IsolatedAsyncioTestCase):
    async def test_pause_resumes_thread_without_claiming_queued_jobs(self):
        worker = Worker.__new__(Worker)
        worker.state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (worker.state / "maintenance.pause").touch()
        worker.workspace = worker.state
        worker.ready = False
        worker.queue = Mock()
        worker.queue.get.return_value = "fixture-thread"
        worker.queue.interrupted.return_value = []
        worker.rpc = SimpleNamespace(start=AsyncMock(), request=AsyncMock())
        with patch("coach.worker.asyncio.sleep", side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await worker.run()
        self.assertTrue(worker.ready)
        self.assertTrue((worker.state / "maintenance.paused").exists())
        worker.queue.claim.assert_not_called()
        self.assertEqual(worker.rpc.request.call_args.args[0], "thread/resume")
        self.assertEqual(worker.rpc.request.call_args.args[1]["threadId"], "fixture-thread")

    async def test_health_requires_login_and_live_appserver(self):
        worker = Worker.__new__(Worker)
        worker.ready = True
        worker.rpc = SimpleNamespace(
            process=SimpleNamespace(returncode=None),
            request=AsyncMock(return_value={"account": None}),
        )
        with self.assertRaises(web.HTTPServiceUnavailable):
            await worker.health(None)
        worker.rpc.request.return_value = {"account": {"type": "chatgpt"}}
        self.assertEqual((await worker.health(None)).status, 200)
        worker.rpc.process.returncode = 1
        with self.assertRaises(web.HTTPServiceUnavailable):
            await worker.health(None)


if __name__ == "__main__":
    unittest.main()
