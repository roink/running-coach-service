"""Private stdio app-server client; never exposes arbitrary RPC to Matrix."""

import asyncio
import json


class RPCError(RuntimeError):
    pass


class AppServer:
    def __init__(
        self,
        command=(
            "codex",
            "-c",
            "features.apps=false",
            "-c",
            "features.plugins=false",
            "-c",
            "features.multi_agent=false",
            "app-server",
        ),
    ):
        self.command = command
        self.process = None
        self.reader = None
        self.pending = {}
        self.events = asyncio.Queue(maxsize=4096)
        self.sequence = 0

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self._read())
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "nas_running_coach",
                    "title": "NAS Running Coach",
                    "version": "0.1.0",
                }
            },
        )
        await self._send({"method": "initialized", "params": {}})

    async def _send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method, params, timeout=60):
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "id" in message and "method" in message:
                    # No escalation, shell approval, account refresh or dynamic tool grant
                    # is delegated to a user message. Fail closed on server requests.
                    await self._send(
                        {
                            "id": message["id"],
                            "error": {
                                "code": -32601,
                                "message": "Interactive permission requests are disabled",
                            },
                        }
                    )
                elif "id" in message:
                    future = self.pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(RPCError(str(message["error"])))
                        else:
                            future.set_result(message.get("result", {}))
                elif message.get("method") in {
                    "item/completed",
                    "turn/completed",
                    "turn/started",
                    "error",
                }:
                    self.events.put_nowait(message)
            raise RPCError("Codex app-server disconnected")
        except (Exception, asyncio.CancelledError) as error:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RPCError(type(error).__name__))
            if not isinstance(error, asyncio.CancelledError):
                try:
                    self.events.put_nowait({"method": "disconnected", "params": {}})
                except asyncio.QueueFull:
                    pass

    async def final_answer(self, thread_id, turn_id, timeout=600):
        answers = []
        async with asyncio.timeout(timeout):
            while True:
                message = await self.events.get()
                method, params = message["method"], message.get("params", {})
                if method == "disconnected":
                    raise RPCError("Codex app-server disconnected during turn")
                if params.get("threadId") != thread_id:
                    continue
                if params.get("turnId", turn_id) != turn_id:
                    continue
                if method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                        answers.append(item["text"])
                if method == "turn/completed":
                    turn = params["turn"]
                    if turn["id"] != turn_id:
                        continue
                    if turn.get("status") != "completed":
                        raise RPCError("Turn did not complete successfully")
                    if not answers:
                        raise RPCError("Completed turn has no final answer")
                    return answers[-1]

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
