"""Read-only service probes and offline candidate protocol checks; never starts a turn."""

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

from coach.appserver import AppServer
from coach.worker import RESULT_SCHEMA, Worker


def fetch(url, token):
    request = Request(url, headers={"Authorization": "Bearer " + token})
    with urlopen(request, timeout=8) as response:
        return json.load(response)


def bot():
    config = json.loads(
        Path(os.getenv("COACH_MATRIX_CONFIG") or os.environ["MATRIX_ALERT_CONFIG"]).read_text()
    )
    state = json.loads(Path("/state/sync-health.json").read_text())
    if not 0 <= time.time() - state["last_success"] < 90:
        raise RuntimeError("Matrix sync heartbeat stale")
    identity = fetch(
        config["homeserver"] + "/_matrix/client/v3/account/whoami", config["access_token"]
    )
    if identity["user_id"] != config["user_id"] or identity["device_id"] != config["device_id"]:
        raise RuntimeError("Matrix device identity changed")
    encrypted = fetch(
        config["homeserver"]
        + "/_matrix/client/v3/rooms/"
        + config["room_id"]
        + "/state/m.room.encryption/",
        config["access_token"],
    )
    if encrypted.get("algorithm") != "m.megolm.v1.aes-sha2":
        raise RuntimeError("Room encryption missing")
    # Verify stores are readable without opening a second Olm client.
    with sqlite3.connect("file:/state/queue.sqlite3?mode=ro", uri=True) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Queue integrity check failed")


def worker():
    token = Path(os.environ["COACH_TOKEN_FILE"]).read_text().strip()
    if fetch("http://127.0.0.1:8080/health", token) != {"ready": True}:
        raise RuntimeError("Worker not ready")


def data():
    config = json.loads(Path(os.environ["COACH_DATA_CONFIG"]).read_text())
    result = fetch("http://127.0.0.1:8080/inventory?limit=1", config["token"])
    if "activities" not in result:
        raise RuntimeError("Training inventory unavailable")


async def candidate(schema_root):
    # Validate the exact request fields we rely on against this binary's schemas.
    import jsonschema

    policy = Worker.policy(SimpleNamespace(workspace=Path("/tmp/coach-check")))
    for filename, params in [
        ("ThreadStartParams", policy),
        ("ThreadResumeParams", {"threadId": "fixture", **policy}),
        (
            "TurnStartParams",
            {
                "threadId": "fixture",
                "input": [{"type": "text", "text": "fixture"}],
                "cwd": policy["cwd"],
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "enabled"},
                "outputSchema": RESULT_SCHEMA,
            },
        ),
    ]:
        schema = json.loads((schema_root / "v2" / (filename + ".json")).read_text())
        jsonschema.validate(params, schema)
    # Exercise real stdio initialization, thread creation and resume without inference.
    Path(policy["cwd"]).mkdir(parents=True, exist_ok=True)
    rpc = AppServer()
    try:
        await rpc.start()
        result = await rpc.request("thread/start", policy)
        if not result["thread"]["id"]:
            raise RuntimeError("Thread creation failed")
    finally:
        await rpc.close()


async def resume_history():
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    shutil.copytree("/history", home / "sessions")
    thread = json.loads(Path("/thread.json").read_text())["thread_id"]
    policy = Worker.policy(SimpleNamespace(workspace=Path("/tmp/coach-check")))
    Path(policy["cwd"]).mkdir(parents=True, exist_ok=True)
    rpc = AppServer()
    try:
        await rpc.start()
        resumed = await rpc.request("thread/resume", {"threadId": thread, **policy})
        if resumed["thread"]["id"] != thread:
            raise RuntimeError("Restored conversation resume failed")
    finally:
        await rpc.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["bot", "worker", "data", "candidate", "resume"])
    args = parser.parse_args()
    if args.mode == "candidate":
        Path(os.environ["CODEX_HOME"]).mkdir(parents=True, exist_ok=True)
        root = Path("/tmp/schemas")
        subprocess.run(
            ["codex", "app-server", "generate-json-schema", "--out", str(root)], check=True
        )
        asyncio.run(candidate(root))
    elif args.mode == "resume":
        asyncio.run(resume_history())
    else:
        {"bot": bot, "worker": worker, "data": data}[args.mode]()
    print(args.mode + ": ready")


if __name__ == "__main__":
    main()
