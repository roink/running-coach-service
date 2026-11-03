"""Allowlisted encrypted Matrix chat; model process never gets Matrix credentials."""

import asyncio
import hashlib
import io
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp
from nio import (
    AsyncClient,
    AsyncClientConfig,
    RoomEncryptedImage,
    RoomMessageText,
    SyncResponse,
)
from nio.crypto.attachments import decrypt_attachment

from coach.artifacts import MAX_IMAGE_BYTES, read_export
from coach.images import normalize
from coach.lifecycle import run_service
from coach.queue import Queue

LOG = logging.getLogger(__name__)


class Gateway:
    def __init__(self, config, state, incoming, exports):
        self.config = config
        self.state, self.incoming, self.exports = state, incoming, exports
        self.queue = Queue(state / "queue.sqlite3")
        self.queue.db.executescript("""
          CREATE TABLE IF NOT EXISTS messages (
            job_id TEXT NOT NULL, part INTEGER NOT NULL, content TEXT NOT NULL,
            encrypted TEXT, sent INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(job_id,part));
          CREATE TABLE IF NOT EXISTS activity_seen (id INTEGER PRIMARY KEY);
        """)
        self.client = AsyncClient(
            config["homeserver"],
            config["user_id"],
            device_id=config["device_id"],
            store_path=str(state / "crypto"),
            config=AsyncClientConfig(
                encryption_enabled=True,
                pickle_key=config["pickle_key"],
                store_sync_tokens=False,
                request_timeout=45,
            ),
        )
        self.client.restore_login(config["user_id"], config["device_id"], config["access_token"])
        self.client.add_event_callback(self.receive, (RoomMessageText, RoomEncryptedImage))
        self.session = None

    def trusted_sender(self, event):
        devices = self.config.get("trusted_devices", {}).get(event.sender, {})
        return event.decrypted and event.sender_key in {
            device["curve25519"] for device in devices.values()
        }

    async def receive(self, room, event):
        if room.room_id != self.config["room_id"] or event.sender != self.config["athlete_id"]:
            return
        if not room.encrypted or not self.trusted_sender(event):
            LOG.warning("Ignored a message from an unverified device or plaintext event")
            return
        job_id = hashlib.sha256(event.event_id.encode()).hexdigest()
        if self.queue.job(job_id):
            return
        images = []
        text = event.body
        if isinstance(event, RoomEncryptedImage):
            encrypted = await self.download(event.url)
            plaintext = decrypt_attachment(
                encrypted, event.key["k"], event.hashes["sha256"], event.iv
            )
            content, _, _ = normalize(plaintext)
            filename = job_id + ".png"
            temporary = self.incoming / ("." + filename)
            temporary.write_bytes(content)
            temporary.replace(self.incoming / filename)
            images = [filename]
            text = "Analyze this image in the context of my running. Caption: " + event.body
        if len(text) > 32_000:
            LOG.warning("Ignored oversized Matrix message")
            return
        self.queue.enqueue(job_id, {"text": text, "images": images})

    async def download(self, mxc):
        parsed = urlsplit(mxc)
        hostname = self.config["server_name"]
        if parsed.scheme != "mxc" or parsed.netloc != hostname or parsed.query or parsed.fragment:
            raise ValueError("Only local homeserver media is accepted")
        media_id = parsed.path.removeprefix("/")
        if not media_id or "/" in media_id:
            raise ValueError("Invalid media ID")
        url = self.config["homeserver"] + "/_matrix/client/v1/media/download/"
        url += quote(hostname, safe="") + "/" + quote(media_id, safe="")
        async with self.session.get(
            url,
            headers={"Authorization": "Bearer " + self.config["access_token"]},
            allow_redirects=False,
        ) as response:
            response.raise_for_status()
            if response.status != 200:
                raise ValueError("Unexpected media redirect")
            content = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                content.extend(chunk)
                if len(content) > MAX_IMAGE_BYTES:
                    raise ValueError("Media exceeds download limit")
            return bytes(content)

    async def verify_room(self):
        room_id = self.config["room_id"]
        response = await self.client.joined_members(room_id)
        if not hasattr(response, "members"):
            raise RuntimeError("Could not load room membership")
        members = {member.user_id for member in response.members}
        if members != {self.config["user_id"], self.config["athlete_id"]}:
            raise RuntimeError("Unexpected members in private coach room")
        if not self.client.rooms[room_id].encrypted:
            raise RuntimeError("Coach room must be encrypted")
        if self.client.should_query_keys:
            await self.client.keys_query()
        for user_id, devices in self.config.get("trusted_devices", {}).items():
            for device_id, keys in devices.items():
                device = self.client.device_store[user_id].get(device_id)
                if (
                    device
                    and device.ed25519 == keys["ed25519"]
                    and device.curve25519 == keys["curve25519"]
                ):
                    self.client.verify_device(device)

    async def process(self):
        if self.config.get("proactive_enabled"):
            await self.poll_workouts()
        for row in self.queue.db.execute(
            "SELECT id FROM jobs WHERE state IN ('queued','running') ORDER BY created"
        ).fetchall():
            job = self.queue.job(row[0])
            headers = {"Authorization": "Bearer " + self.config["worker_token"]}
            url = self.config["worker_url"] + "/jobs"
            if job["state"] == "queued":
                async with self.session.post(
                    url, headers=headers, json={"id": job["id"], **job["payload"]}
                ) as response:
                    response.raise_for_status()
                    with self.queue.db:
                        self.queue.db.execute(
                            "UPDATE jobs SET state='running' WHERE id=?", (job["id"],)
                        )
            async with self.session.get(url + "/" + job["id"], headers=headers) as response:
                response.raise_for_status()
                status = await response.json()
            if status["state"] == "completed":
                self.queue.complete(job["id"], status["result"])
            elif status["state"] == "needs_review":
                self.queue.complete(
                    job["id"],
                    {
                        "text": "Der Coach konnte diese Anfrage nicht abschließen. "
                        "Der Auftrag wurde zur technischen Prüfung zurückgehalten "
                        f"(Referenz {job['id'][:16]}). Es wurde keine Analyse wiederholt.",
                        "images": [],
                    },
                )
        for job in self.queue.pending_deliveries():
            await self.deliver(job)

    async def poll_workouts(self):
        import time

        if time.time() - (self.queue.get("inventory_poll") or 0) < 60:
            return
        rows = []
        after = 0
        headers = {"Authorization": "Bearer " + self.config["data_token"]}
        for _ in range(100):
            url = self.config["data_url"] + f"/inventory?after={after}&limit=500"
            async with self.session.get(url, headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    raise RuntimeError("Workout reconciliation deferred")
                page = await response.json()
            rows.extend(page["activities"])
            if page["next"] is None:
                break
            if page["next"] <= after:
                raise ValueError("Inventory cursor did not advance")
            after = page["next"]
        else:
            raise ValueError("Inventory exceeds configured reconciliation bound")
        baseline = self.queue.get("inventory_baseline")
        with self.queue.db:
            for activity in rows:
                cursor = self.queue.db.execute(
                    "INSERT OR IGNORE INTO activity_seen VALUES(?)", (activity["id"],)
                )
                if cursor.rowcount and baseline and activity.get("type") == "Run":
                    job_id = f"workout-{activity['id']}"
                    payload = {
                        "text": (
                            f"New imported running workout: activity ID {activity['id']}. "
                            "Fetch it with python -m coach.training activity, then review "
                            "recent history, my goals and your memories. Analyze the session "
                            "and send a concise coaching recommendation. This is an import "
                            "event, not a request to test my fitness."
                        ),
                        "images": [],
                    }
                    self.queue.db.execute(
                        "INSERT OR IGNORE INTO jobs(id,payload,created) VALUES(?,?,?)",
                        (job_id, json.dumps(payload), time.time()),
                    )
            self.queue.db.execute(
                "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE "
                "SET value=excluded.value",
                ("inventory_baseline", "true"),
            )
        self.queue.set("inventory_poll", time.time())

    async def deliver(self, job):
        await self.verify_room()
        room_id = self.config["room_id"]
        rows = self.queue.db.execute(
            "SELECT * FROM messages WHERE job_id=? ORDER BY part", (job["id"],)
        ).fetchall()
        if not rows:
            result = job["result"]
            if len(result["text"]) > 40_000 or len(result["images"]) > 4:
                raise ValueError("Response exceeds delivery budget")
            text = result["text"]
            parts = [
                {"msgtype": "m.text", "body": text[index : index + 5000]}
                for index in range(0, len(text), 5000)
            ]
            for filename in result["images"]:
                filename = filename.removeprefix("exports/")
                content, width, height = normalize(read_export(self.exports, filename))
                uploaded, encryption = await self.client.upload(
                    io.BytesIO(content),
                    content_type="application/octet-stream",
                    filename="chart.png",
                    encrypt=True,
                    filesize=len(content),
                )
                if not hasattr(uploaded, "content_uri") or encryption is None:
                    raise RuntimeError("Encrypted media upload failed")
                parts.append(
                    {
                        "msgtype": "m.image",
                        "body": Path(filename).name,
                        "file": {**encryption, "url": uploaded.content_uri},
                        "info": {
                            "mimetype": "image/png",
                            "size": len(content),
                            "w": width,
                            "h": height,
                        },
                    }
                )
            with self.queue.db:
                for index, content in enumerate(parts):
                    self.queue.db.execute(
                        "INSERT INTO messages(job_id,part,content) VALUES(?,?,?)",
                        (job["id"], index, json.dumps(content)),
                    )
            rows = self.queue.db.execute(
                "SELECT * FROM messages WHERE job_id=? ORDER BY part", (job["id"],)
            ).fetchall()
        for row in rows:
            if row["sent"]:
                continue
            if row["encrypted"]:
                encrypted = json.loads(row["encrypted"])
            else:
                if self.client.olm.should_share_group_session(room_id):
                    await self.client.share_group_session(room_id, ignore_unverified_devices=False)
                _, encrypted = self.client.encrypt(
                    room_id, "m.room.message", json.loads(row["content"])
                )
                with self.queue.db:
                    self.queue.db.execute(
                        "UPDATE messages SET encrypted=? WHERE job_id=? AND part=?",
                        (json.dumps(encrypted), job["id"], row["part"]),
                    )
            transaction = f"coach-{job['id']}-{row['part']}"
            url = self.config["homeserver"] + "/_matrix/client/v3/rooms/"
            url += quote(room_id, safe="") + "/send/m.room.encrypted/" + transaction
            async with self.session.put(
                url,
                json=encrypted,
                allow_redirects=False,
                headers={"Authorization": "Bearer " + self.config["access_token"]},
            ) as response:
                if response.status != 200:
                    raise RuntimeError("Encrypted delivery did not succeed")
                await response.read()
            with self.queue.db:
                self.queue.db.execute(
                    "UPDATE messages SET sent=1 WHERE job_id=? AND part=?", (job["id"], row["part"])
                )
        self.queue.mark_delivered(job["id"])

    async def run(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as self.session:
            try:
                if self.client.should_upload_keys:
                    await self.client.keys_upload()
                # Rebuild in-memory room state once per process, even with a saved
                # cursor. Full-state syncs return immediately and cannot long poll.
                full_state = True
                while True:
                    try:
                        response = await self.client.sync(
                            timeout=10_000,
                            since=self.queue.get("cursor"),
                            full_state=full_state,
                            set_presence="offline",
                        )
                        if not isinstance(response, SyncResponse):
                            raise RuntimeError("Matrix sync failed")
                        self.queue.set("cursor", response.next_batch)
                        full_state = False
                        if self.client.should_upload_keys:
                            await self.client.keys_upload()
                        if self.client.outgoing_to_device_messages:
                            await self.client.send_to_device_messages()
                        await self.process()
                        heartbeat = self.state / "sync-health.json"
                        temporary = heartbeat.with_suffix(".tmp")
                        temporary.write_text(json.dumps({"last_success": time.time()}))
                        temporary.replace(heartbeat)
                    except Exception as error:
                        LOG.warning("Gateway operation deferred: %s", type(error).__name__)
                        await asyncio.sleep(10)
            finally:
                await self.client.close()
                self.queue.close()


def main():
    logging.basicConfig(level=logging.WARNING)
    config = json.loads(Path(os.environ["COACH_MATRIX_CONFIG"]).read_text())
    gateway = Gateway(config, Path("/state"), Path("/incoming"), Path("/exports"))
    asyncio.run(run_service(gateway.run()))


if __name__ == "__main__":
    main()
