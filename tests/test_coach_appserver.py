"""Exercise the real stdio transport against a deterministic fake server."""

import asyncio
import signal
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/running-coach"))
from coach.appserver import AppServer

FAKE = r"""
import json,sys
def emit(value):
    print(json.dumps(value),flush=True)
for line in sys.stdin:
    m=json.loads(line)
    if m.get('method')=='initialize':
        emit({'id':m['id'],'result':{'userAgent':'fixture'}})
    elif m.get('method')=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'turn-fixture'}}})
        emit({'id':900,'method':'item/commandExecution/requestApproval','params':{}})
    elif m.get('id')==900:
        assert 'error' in m, 'approval must be rejected'
        emit({'method':'item/completed','params':{'threadId':'thread-fixture',
          'turnId':'turn-fixture','item':{'type':'agentMessage','text':'fixture answer',
          'phase':'final_answer'}}})
        emit({'method':'turn/completed','params':{'threadId':'thread-fixture',
          'turn':{'id':'turn-fixture','status':'completed'}}})
"""


class AppServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_cancels_service_cleanly(self):
        source = str(Path(__file__).resolve().parents[1] / "services/running-coach")
        script = (
            f"import sys;sys.path.insert(0,{source!r});import asyncio;"
            "from coach.lifecycle import run_service;"
            'print("ready",flush=True);asyncio.run(run_service(asyncio.sleep(600)))'
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c", script, stdout=asyncio.subprocess.PIPE
        )
        try:
            self.assertEqual(await process.stdout.readline(), b"ready\n")
            await asyncio.sleep(0.05)
            process.send_signal(signal.SIGTERM)
            self.assertEqual(await asyncio.wait_for(process.wait(), timeout=3), 0)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def test_final_response_and_fail_closed_permission_request(self):
        rpc = AppServer((sys.executable, "-u", "-c", FAKE))
        try:
            await rpc.start()
            result = await rpc.request("turn/start", {"threadId": "thread-fixture"})
            answer = await rpc.final_answer("thread-fixture", result["turn"]["id"], timeout=5)
            self.assertEqual(answer, "fixture answer")
        finally:
            await rpc.close()


if __name__ == "__main__":
    unittest.main()
