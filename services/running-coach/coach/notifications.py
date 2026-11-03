"""Independent encrypted NAS alert sender; no model or training-data access."""

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path

from coach.artifacts import read_export
from coach.gateway import Gateway
from coach.lifecycle import run_service


class Notifications(Gateway):
    async def receive(self, room, event):
        return

    async def process(self):
        for path in sorted(Path("/spool").glob("*.json"))[:100]:
            event = json.loads(read_export(Path("/spool"), path.name, limit=16_384))
            if set(event) - {"id", "severity", "source", "timestamp", "message"}:
                raise ValueError("Unexpected alert fields")
            if event["severity"] not in {"info", "warning", "critical"}:
                raise ValueError("Invalid alert severity")
            if any(
                not isinstance(event.get(key), str)
                for key in ["id", "source", "timestamp", "message"]
            ):
                raise ValueError("Invalid alert record")
            if len(event["message"]) > 4000:
                raise ValueError("Alert exceeds message budget")
            job_id = "alert-" + hashlib.sha256(event["id"].encode()).hexdigest()
            if self.queue.enqueue(job_id, event):
                self.queue.complete(
                    job_id,
                    {
                        "text": f"[{event['severity'].upper()}] {event['source']}\n"
                        f"{event['message']}\n{event['timestamp']}",
                        "images": [],
                    },
                )
            path.unlink()
        for job in self.queue.pending_deliveries():
            await self.deliver(job)


def main():
    logging.basicConfig(level=logging.WARNING)
    config = json.loads(Path(os.environ["MATRIX_ALERT_CONFIG"]).read_text())
    sender = Notifications(config, Path("/state"), Path("/unused"), Path("/unused"))
    asyncio.run(run_service(sender.run()))


if __name__ == "__main__":
    main()
