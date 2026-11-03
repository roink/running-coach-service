"""Validate typed query bounds and late-import reconciliation without MongoDB."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/running-coach"))
from aiohttp import web
from coach.data import DataService, bounded_limit, utc_date
from coach.gateway import Gateway
from coach.queue import Queue


class DataTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_operators_and_invalid_date_ranges_are_rejected(self):
        service = DataService.__new__(DataService)
        with self.assertRaises(web.HTTPBadRequest):
            await service.history(SimpleNamespace(query={"$where": "untrusted"}))
        with self.assertRaises(ValueError):
            await service.history(
                SimpleNamespace(query={"start": "2020-01-01", "end": "2026-01-01"})
            )
        with self.assertRaises(ValueError):
            bounded_limit("100000")
        with self.assertRaises(ValueError):
            utc_date("2026-02-31")

    async def test_baseline_is_quiet_and_late_arrival_is_deduplicated(self):
        class Response:
            status = 200

            def __init__(self, rows):
                self.rows = rows

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def json(self):
                return {"activities": self.rows, "next": None}

        class Session:
            rows = [{"id": 100, "type": "Run"}]

            def get(self, url, **kwargs):
                return Response(self.rows)

        with tempfile.TemporaryDirectory() as directory:
            gateway = Gateway.__new__(Gateway)
            gateway.queue = Queue(Path(directory) / "queue.sqlite3")
            gateway.queue.db.execute("CREATE TABLE activity_seen(id INTEGER PRIMARY KEY)")
            gateway.config = {"data_url": "http://fixture", "data_token": "fixture"}
            gateway.session = Session()
            await gateway.poll_workouts()
            self.assertIsNone(gateway.queue.claim())
            gateway.session.rows = [{"id": 10, "type": "Run"}, {"id": 100, "type": "Run"}]
            gateway.queue.set("inventory_poll", 0)
            await gateway.poll_workouts()
            self.assertEqual(gateway.queue.claim()["id"], "workout-10")
            gateway.queue.set("inventory_poll", 0)
            await gateway.poll_workouts()
            self.assertIsNone(gateway.queue.claim())
            gateway.queue.close()


if __name__ == "__main__":
    unittest.main()
