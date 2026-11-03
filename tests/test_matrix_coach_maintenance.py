"""Offline recovery/upgrade safety fixtures; never contact the NAS."""

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "stack_maintenance", Path(__file__).resolve().parents[1] / "scripts/matrix-coach-maintenance.py"
)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class Safety(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.enterContext(patch.object(M, "STATE", self.root / "state"))
        self.enterContext(patch.object(M, "ROOT", self.root))
        self.enterContext(patch.object(M, "COACH", self.root / "coach"))
        M.STATE.mkdir()
        (M.COACH / "worker").mkdir(parents=True)
        (self.root / "quadlet").mkdir()
        self.report = {"stamp": "20261102T120000Z", "status": "running"}

    def test_registry_failure_defers_without_starting_an_upgrade(self):
        with patch.object(M, "run", side_effect=RuntimeError("registry offline")):
            with self.assertRaises(M.Deferred):
                M.pull_matrix(self.report)
        self.assertNotIn("phase", self.report)

    def test_registry_channels_never_cross_postgres_major(self):
        self.assertEqual(M.REGISTRY["matrix-db"], "docker.io/library/postgres:17-alpine")
        with patch.object(
            M,
            "run",
            side_effect=lambda *a, **k: "PostgreSQL) 18.0" if "--version" in a else "candidate",
        ):
            with self.assertRaisesRegex(RuntimeError, "major boundary"):
                M.pull_matrix(self.report)

    def test_drain_pauses_claims_and_stops_gateway_before_worker(self):
        with (
            patch.object(M, "run") as run,
            patch.object(M, "job_counts", side_effect=[{"running": 1}, {}, {}]),
            patch.object(M.time, "sleep"),
        ):
            M.quiesce(self.report, legacy=True)
        commands = [c.args for c in run.call_args_list]
        self.assertEqual(commands[0], ("systemctl", "stop", "coach-gateway.service"))
        self.assertEqual(commands[1], ("systemctl", "stop", "coach-worker.service"))
        self.assertTrue((M.COACH / "worker/maintenance.pause").exists())
        self.assertNotIn(("systemctl", "stop", "matrix-db.service"), commands)

    def test_idle_count_alone_is_insufficient_without_worker_acknowledgement(self):
        acknowledged = M.COACH / "worker/maintenance.paused"
        with (
            patch.object(M, "run"),
            patch.object(M, "job_counts", return_value={}),
            patch.object(M.time, "sleep", side_effect=lambda _: acknowledged.touch()) as sleep,
        ):
            M.quiesce(self.report)
        sleep.assert_called_once()

    def test_ambiguous_turn_after_stop_is_never_replayed(self):
        with (
            patch.object(M, "run"),
            patch.object(M, "job_counts", side_effect=[{}, {"running": 1}]),
        ):
            with self.assertRaisesRegex(RuntimeError, "raced"):
                M.quiesce(self.report, legacy=True)

    def test_failed_backup_restores_old_services_without_promoting(self):
        args = SimpleNamespace(mode="coach", bootstrap=False, rotate_database_password=False)
        with (
            patch.object(M, "guard"),
            patch.object(M, "image", return_value="old"),
            patch.object(M, "run"),
            patch.object(M, "fetch_coach", return_value={"coach-worker": "new"}),
            patch.object(M, "quiesce"),
            patch.object(M, "backup", side_effect=RuntimeError("backup")),
            patch.object(M, "start_stack") as start,
            patch.object(M, "resume") as resume,
            patch.object(M, "promote") as promote,
        ):
            with self.assertRaisesRegex(RuntimeError, "backup"):
                M.execute(args, self.report)
        promote.assert_not_called()
        start.assert_called_once_with(legacy=False)
        resume.assert_called_once()

    def test_failed_new_image_stops_writers_and_never_downgrades(self):
        args = SimpleNamespace(mode="matrix", bootstrap=False, rotate_database_password=False)
        with (
            patch.object(M, "guard"),
            patch.object(M, "image", return_value="old"),
            patch.object(M, "run"),
            patch.object(M, "pull_matrix", return_value={"matrix": "new"}),
            patch.object(M, "quiesce"),
            patch.object(M, "backup"),
            patch.object(M, "promote"),
            patch.object(M, "start_stack", side_effect=RuntimeError("readiness")),
            patch.object(M, "resume") as resume,
            patch.object(M.subprocess, "run") as stop,
        ):
            with self.assertRaisesRegex(RuntimeError, "readiness"):
                M.execute(args, self.report)
        self.assertEqual(stop.call_count, len(M.ORDER))
        self.assertTrue((M.STATE / "live-blocked").exists())
        resume.assert_not_called()

    def test_unchanged_registry_does_not_stop_or_backup(self):
        args = SimpleNamespace(mode="matrix")
        with (
            patch.object(M, "guard"),
            patch.object(M, "image", return_value="same"),
            patch.object(M, "run"),
            patch.object(M, "pull_matrix", return_value={"matrix": "same"}),
            patch.object(M, "quiesce") as stop,
            patch.object(M, "backup") as backup,
        ):
            M.execute(args, self.report)
        stop.assert_not_called()
        backup.assert_not_called()
        self.assertEqual(self.report["status"], "current")

    def test_startup_refuses_interrupted_transaction_and_failed_live_upgrade(self):
        report = {"status": "running", "phase": "promoting", "pid": 999999999, "process_start": "0"}
        (M.STATE / "last-run.json").write_text(json.dumps(report))
        with self.assertRaisesRegex(RuntimeError, "Interrupted"):
            M.startup_guard()
        report["phase"] = "preflight"
        (M.STATE / "last-run.json").write_text(json.dumps(report))
        M.startup_guard()
        (M.STATE / "live-blocked").touch()
        with self.assertRaisesRegex(RuntimeError, "Live upgrade"):
            M.startup_guard()

    def test_sqlite_restore_rejects_missing_stores_and_checks_real_databases(self):
        with self.assertRaisesRegex(RuntimeError, "Missing"):
            M.verify_sqlite(self.root)
        for i in range(4):
            with sqlite3.connect(self.root / f"fixture-{i}.db") as db:
                db.execute("create table fixture (id integer)")
        self.assertEqual(M.verify_sqlite(self.root), 4)

    def test_retention_keeps_failed_and_held_recovery_points(self):
        backups = self.root / "backups"
        backups.mkdir()
        failed = backups / "20261001T000000Z"
        failed.mkdir()
        for day in range(2, 6):
            stamp = f"2026100{day}T000000Z"
            p = backups / stamp
            p.mkdir()
            (p / "complete.json").write_text(
                json.dumps(
                    {
                        "stamp": stamp,
                        "snapshots": [d + "@matrix-coach-" + stamp for d in M.DATASETS],
                    }
                )
            )
        calls = []

        def run(*args, **kwargs):
            calls.append(args)
            return "1" if args[1] == "get" else ""

        report = {
            "backup": str(backups / "20261005T000000Z"),
            "stamp": "20261005T000000Z",
            "snapshots": [d + "@matrix-coach-20261005T000000Z" for d in M.DATASETS],
        }
        with patch.object(M, "BACKUPS", backups), patch.object(M, "run", side_effect=run):
            M.prune_recovery(report)
        self.assertTrue(failed.exists())
        self.assertFalse(any(c[1] == "destroy" for c in calls))
        self.assertEqual(len(list(backups.iterdir())), 4)


if __name__ == "__main__":
    unittest.main()
