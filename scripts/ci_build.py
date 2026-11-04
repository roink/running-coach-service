#!/usr/bin/env python3
"""Build with generic source only; no NAS connectivity, credentials or private fixtures."""

import gzip
import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

import coach_release as release

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services/running-coach"
IMAGE = "running-coach-ci:build"


def run(*command):
    subprocess.run(command, check=True)


def main():
    revision = os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Expected immutable source revision")
    with urllib.request.urlopen(
        "https://registry.npmjs.org/@openai%2Fcodex/latest", timeout=30
    ) as r:
        version = json.load(r)["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Stable Codex version required")
    run(
        "docker",
        "run",
        "--rm",
        "--volume",
        str(SERVICE) + ":/build:rw",
        "--workdir=/build",
        "python:3.12-slim-bookworm",
        "sh",
        "-ec",
        "pip install --no-cache-dir uv==0.12.12 && "
        "uv pip compile --python-version 3.12 --upgrade --generate-hashes "
        "--no-emit-index-url requirements.in -o requirements.txt",
    )
    # The release preserves exactly the Containerfile and lock used for this build.
    path = SERVICE / "Containerfile"
    path.write_text(
        re.sub(r"ARG CODEX_VERSION=.*", "ARG CODEX_VERSION=" + version, path.read_text())
    )
    run(
        "docker",
        "build",
        "--pull",
        "--platform=linux/amd64",
        "--file",
        str(path),
        "--label",
        "org.opencontainers.image.revision=" + revision,
        "--label",
        "org.opencontainers.image.source=https://github.com/" + release.REPOSITORY,
        "--tag",
        IMAGE,
        str(SERVICE),
    )
    base = (
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--memory=2g",
        "--cpus=2",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=512m,mode=1777",
        "--env=HOME=/tmp",
        "--env=CODEX_HOME=/tmp/codex",
        "--entrypoint=python",
    )
    run(
        *base,
        IMAGE,
        "-I",
        "-c",
        "import subprocess; subprocess.run("
        "['file', '--mime-type', '/usr/local/bin/python3.12'], check=True)",
    )
    run(*base, IMAGE, "-I", "-m", "coach.maintenance", "candidate")
    for pattern in ("test_coach*.py", "test_matrix_coach_maintenance.py"):
        run(
            *base,
            "--volume",
            str(ROOT / "tests") + ":/checks:ro",
            "--volume",
            str(ROOT / "scripts") + ":/scripts:ro",
            IMAGE,
            "-m",
            "unittest",
            "discover",
            "-s",
            "/checks",
            "-p",
            pattern,
            "-v",
        )
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    # docker save may export OCI indexes on newer Docker stores. The config hash
    # read from the archive is the identity Podman uses, independent of store mode.
    temporary = output / "image.tar"
    run("docker", "save", "--output", str(temporary), IMAGE)
    image_id = release.archive_identity(temporary, revision)
    with (
        temporary.open("rb") as source,
        gzip.open(output / release.ASSETS[1], "wb", compresslevel=1) as target,
    ):
        shutil.copyfileobj(source, target)
    temporary.unlink()
    release.export_source(ROOT, output / release.ASSETS[2])
    manifest = {
        "schema": 1,
        "repository": release.REPOSITORY,
        "revision": revision,
        "codex": version,
        "image_id": image_id,
        "ci_run_id": int(os.environ["GITHUB_RUN_ID"]),
        "ci_run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "ci_run_number": int(os.environ["GITHUB_RUN_NUMBER"]),
        "archive_sha256": release.checksum(output / release.ASSETS[1]),
        "source_sha256": release.checksum(output / release.ASSETS[2]),
    }
    (output / release.ASSETS[0]).write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
