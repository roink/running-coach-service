"""Verify trust checks, image limits and ambiguous encrypted delivery retries."""

import asyncio
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/running-coach"))
from coach.egress import public_addresses
from coach.gateway import Gateway
from coach.images import normalize
from coach.queue import Queue
from nio import SyncResponse
from PIL import Image


class BoundaryTests(unittest.TestCase):
    def test_egress_rejects_private_mixed_and_mapped_destinations(self):
        for addresses in [
            ["127.0.0.1"],
            ["192.168.1.10"],
            ["169.254.169.254"],
            ["::1"],
            ["fc00::1"],
            ["::ffff:8.8.8.8"],
            ["1.1.1.1", "10.0.0.1"],
            ["224.0.0.1"],
        ]:
            with self.subTest(addresses=addresses), self.assertRaises(ValueError):
                public_addresses(addresses)
        self.assertEqual(public_addresses(["1.1.1.1"]), ["1.1.1.1"])

    def test_plaintext_and_unknown_sender_devices_are_not_trusted(self):
        gateway = Gateway.__new__(Gateway)
        gateway.config = {
            "trusted_devices": {
                "@fixture:example": {
                    "DEVICE": {"curve25519": "trusted-public-key", "ed25519": "signing-key"}
                }
            }
        }
        event = SimpleNamespace(
            sender="@fixture:example", decrypted=True, sender_key="trusted-public-key"
        )
        self.assertTrue(gateway.trusted_sender(event))
        event.decrypted = False
        self.assertFalse(gateway.trusted_sender(event))
        event.decrypted = True
        event.sender_key = "untrusted"
        self.assertFalse(gateway.trusted_sender(event))

    def test_image_normalization_and_non_image_rejection(self):
        image = io.BytesIO()
        Image.new("RGB", (4, 3), "green").save(image, format="JPEG")
        content, width, height = normalize(image.getvalue())
        self.assertTrue(content.startswith(b"\x89PNG"))
        self.assertEqual((width, height), (4, 3))
        with self.assertRaises(OSError):
            normalize(b"<svg>not a raster</svg>")


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_state_only_until_first_success_on_each_start(self):
        gateway = Gateway.__new__(Gateway)
        gateway.state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        gateway.queue = Mock()
        gateway.queue.get.return_value = "saved-cursor"
        gateway.process = AsyncMock()
        gateway.client = SimpleNamespace(
            should_upload_keys=False,
            outgoing_to_device_messages=[],
            close=AsyncMock(),
        )
        for _ in range(2):
            # A failed initial request must not skip rebuilding room state;
            # restarting with a persisted cursor must rebuild it again.
            gateway.client.sync = AsyncMock(
                side_effect=[
                    RuntimeError("temporary failure"),
                    SyncResponse.from_dict({"next_batch": "next-cursor"}),
                    SyncResponse.from_dict({"next_batch": "next-cursor"}),
                    asyncio.CancelledError(),
                ]
            )
            with patch("coach.gateway.asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(asyncio.CancelledError):
                    await gateway.run()
            requests = gateway.client.sync.call_args_list
            self.assertEqual(
                [call.kwargs["full_state"] for call in requests],
                [True, True, False, False],
            )
            self.assertTrue(all(call.kwargs["timeout"] == 10_000 for call in requests))
            self.assertEqual(requests[0].kwargs["since"], "saved-cursor")
        self.assertEqual(gateway.client.close.await_count, 2)
        self.assertEqual(gateway.queue.close.call_count, 2)

    async def test_retry_reuses_ciphertext_and_transaction_without_rerunning_analysis(self):
        class Response:
            def __init__(self, status):
                self.status = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def read(self):
                return b"{}"

        class Session:
            def __init__(self):
                self.requests = []

            def put(self, url, **kwargs):
                self.requests.append((url, kwargs["json"]))
                return Response(500 if len(self.requests) == 1 else 200)

        with tempfile.TemporaryDirectory() as directory:
            gateway = Gateway.__new__(Gateway)
            gateway.queue = Queue(Path(directory) / "queue.sqlite3")
            gateway.queue.db.executescript("""CREATE TABLE messages (
                job_id TEXT,part INTEGER,content TEXT,encrypted TEXT,sent INTEGER DEFAULT 0)""")
            gateway.queue.enqueue("fixture", {"text": "request"})
            gateway.queue.complete("fixture", {"text": "answer", "images": []})
            gateway.config = {
                "room_id": "!fixture:example",
                "homeserver": "https://example",
                "access_token": "fixture-not-a-secret",
            }
            gateway.verify_room = AsyncMock()
            encryptions = []

            def encrypt(*args):
                encryptions.append(args)
                return "m.room.encrypted", {"ciphertext": "fixture-ciphertext"}

            gateway.client = SimpleNamespace(
                olm=SimpleNamespace(should_share_group_session=lambda _: False), encrypt=encrypt
            )
            gateway.session = Session()
            job = gateway.queue.job("fixture")
            with self.assertRaises(RuntimeError):
                await gateway.deliver(job)
            self.assertEqual(gateway.queue.job("fixture")["state"], "completed")
            await gateway.deliver(job)
            self.assertEqual(len(encryptions), 1)
            self.assertEqual(gateway.session.requests[0], gateway.session.requests[1])
            self.assertEqual(gateway.queue.job("fixture")["state"], "delivered")
            gateway.queue.close()


if __name__ == "__main__":
    unittest.main()
