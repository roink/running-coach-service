"""Synthetic archives and mocked GitHub/Podman; never reads runtime NAS state."""

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import coach_release as R

SPEC = importlib.util.spec_from_file_location(
    "ci_maintenance", Path(__file__).resolve().parents[1] / "scripts/matrix-coach-maintenance.py"
)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class Releases(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.revision = "a" * 40
        self.run = {
            "id": 42,
            "run_number": 7,
            "run_attempt": 1,
            "head_sha": self.revision,
            "head_branch": "main",
            "event": "push",
            "conclusion": "success",
            "path": ".github/workflows/coach-ci.yml",
            "head_repository": {"full_name": R.REPOSITORY},
        }
        self.release = {
            "schema": 1,
            "repository": R.REPOSITORY,
            "revision": self.revision,
            "image_id": "sha256:" + "b" * 64,
            "codex": "0.166.0",
            "ci_run_id": 42,
            "ci_run_number": 7,
            "ci_run_attempt": 1,
            "archive_sha256": "c" * 64,
            "source_sha256": "d" * 64,
        }

    def tar(self, path, entries):
        with tarfile.open(path, "w:gz") as archive:
            for name, value in entries.items():
                info = tarfile.TarInfo(name)
                if value is None:
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/etc/passwd"
                    archive.addfile(info)
                else:
                    info.size = len(value)
                    archive.addfile(info, io.BytesIO(value))

    def image_archive(self, platform="amd64", revision=None, oci=False):
        raw = json.dumps(
            {
                "os": "linux",
                "architecture": platform,
                "config": {
                    "Labels": {
                        "org.opencontainers.image.revision": revision or self.revision,
                        "org.opencontainers.image.source": "https://github.com/" + R.REPOSITORY,
                    }
                },
            }
        ).encode()
        digest = hashlib.sha256(raw).hexdigest()
        config = "blobs/sha256/" + digest if oci else digest + ".json"
        entries = {config: raw, "manifest.json": json.dumps([{"Config": config}]).encode()}
        if oci:
            entries["oci-layout"] = b'{"imageLayoutVersion":"1.0.0"}'
        archive = self.root / "image.tar.gz"
        self.tar(archive, entries)
        return archive, "sha256:" + digest

    def source_archive(self, extras=None):
        entries = {
            R.SERVICE + name: b"fixture"
            for name in ("Containerfile", "requirements.txt", "coach/worker.py")
        }
        entries.update(extras or {})
        archive = self.root / "source.tar.gz"
        self.tar(archive, entries)
        return archive

    def test_release_requires_exact_successful_main_workflow_and_identity(self):
        R.validate_release(self.release, self.run)
        for field, value in {
            "head_sha": "e" * 40,
            "head_branch": "feature",
            "event": "pull_request",
            "conclusion": "failure",
            "path": ".github/workflows/other.yml",
            "run_attempt": 2,
            "id": 43,
            "run_number": 8,
            "head_repository": {"full_name": "other/repository"},
        }.items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                R.validate_release(self.release, {**self.run, field: value})
        for field, value in {
            "image_id": "latest",
            "source_sha256": "",
            "codex": "1.0.0-beta",
            "repository": "other/repository",
            "ci_run_id": 43,
        }.items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                R.validate_release({**self.release, field: value}, self.run)

    def test_config_digest_is_identical_for_legacy_and_oci_exports(self):
        for oci in (False, True):
            archive, identity = self.image_archive(oci=oci)
            self.assertEqual(R.archive_identity(archive, self.revision, identity), identity)
            with self.assertRaises(ValueError):
                R.archive_identity(archive, self.revision, "sha256:" + "f" * 64)
        archive, _ = self.image_archive(platform="arm64")
        with self.assertRaises(ValueError):
            R.archive_identity(archive, self.revision)
        archive, _ = self.image_archive(revision="e" * 40)
        with self.assertRaises(ValueError):
            R.archive_identity(archive, self.revision)

    def test_source_export_excludes_history_credentials_and_databases(self):
        service = self.root / R.SERVICE
        (service / "coach").mkdir(parents=True)
        for name in (
            "Containerfile",
            "requirements.txt",
            "coach/worker.py",
            "auth.json",
            "coach/history.json",
            "coach/auth.py.bak",
            "jobs.sqlite3",
        ):
            (service / name).write_text("fixture")
        archive = self.root / "export.tar.gz"
        R.export_source(self.root, archive)
        with tarfile.open(archive) as source:
            self.assertEqual(
                set(source.getnames()),
                {
                    R.SERVICE + "Containerfile",
                    R.SERVICE + "requirements.txt",
                    R.SERVICE + "coach/worker.py",
                },
            )

    def test_source_extraction_refuses_traversal_secrets_and_links(self):
        for name, value in {
            "../outside": b"fixture",
            R.SERVICE + "coach/../../auth.json": b"fixture",
            R.SERVICE + "auth.json": b"fixture",
            R.SERVICE + "coach/link.py": None,
            "/absolute.py": b"fixture",
        }.items():
            archive = self.source_archive({name: value})
            with self.subTest(name=name), self.assertRaises(ValueError):
                R.extract_source(archive, self.root / "extracted")
        self.assertFalse((self.root / "extracted").exists())

    def test_checksum_failure_happens_before_image_load_or_service_pause(self):
        assets = {
            name: "https://github.com/" + R.REPOSITORY + "/releases/download/test/" + name
            for name in R.ASSETS
        }

        def download(url, destination, limit):
            destination.write_text(
                json.dumps(self.release) if destination.name == R.ASSETS[0] else "corrupted asset"
            )

        with (
            patch.object(M, "STATE", self.root),
            patch.object(R, "newest", return_value=(self.run, assets)),
            patch.object(R, "download", side_effect=download),
            patch.object(M, "run") as run,
        ):
            with self.assertRaisesRegex(ValueError, "checksum"):
                M.fetch_coach({})
        run.assert_not_called()

    def test_invalid_provenance_never_loads_image(self):
        with (
            patch.object(M, "STATE", self.root),
            patch.object(R, "newest", return_value=(self.run, {})),
            patch.object(R, "stage", side_effect=ValueError("provenance")),
            patch.object(M, "run") as run,
        ):
            with self.assertRaises(ValueError):
                M.fetch_coach({})
        run.assert_not_called()

    def test_qualified_image_only_runs_offline_checks_before_private_backup(self):
        report = {}
        info = {
            "Id": self.release["image_id"],
            "Os": "linux",
            "Architecture": "amd64",
            "Labels": {"org.opencontainers.image.revision": self.revision},
        }

        def command(*args, **kwargs):
            if "inspect" in args:
                return json.dumps([info])
            if "--version" in args:
                return "codex-cli 0.166.0\n"
            return ""

        with (
            patch.object(M, "STATE", self.root),
            patch.object(R, "newest", return_value=(self.run, {})),
            patch.object(R, "stage", return_value=(self.release, self.root)),
            patch.object(M, "run", side_effect=command) as run,
            patch.object(M, "candidate_command", return_value="ready") as candidate,
        ):
            images = M.fetch_coach(report)
        self.assertEqual(set(images), set(M.COACH_SERVICES))
        self.assertTrue(all(v == self.release["image_id"] for v in images.values()))
        candidate.assert_called_once_with(
            self.release["image_id"], "-I", "-m", "coach.maintenance", "candidate"
        )
        self.assertFalse(any(c.args[0] == "systemctl" for c in run.call_args_list))
        self.assertEqual(report["candidate"]["release"], self.release)

    def test_repeated_or_older_ci_run_never_loads_or_restarts(self):
        (self.root / "coach-installed.json").write_text(json.dumps(self.release))
        with (
            patch.object(M, "STATE", self.root),
            patch.object(R, "newest", return_value=(self.run, {})),
            patch.object(R, "stage") as stage,
            patch.object(M, "run") as run,
        ):
            self.assertEqual(M.fetch_coach({}), {})
        stage.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
