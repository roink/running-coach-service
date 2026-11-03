#!/usr/bin/env python3
"""Credential-free CI release format and NAS download validation (standard library)."""

import hashlib
import json
import re
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

REPOSITORY = "roink/running-coach-service"
WORKFLOW = "coach-ci.yml"
ASSETS = ("coach-release.json", "coach-image.tar.gz", "coach-source.tar.gz")
SERVICE = "services/running-coach/"


def source_file(name):
    """An explicit allowlist; no recursive copying of a workstation or NAS tree."""
    if not name.startswith(SERVICE):
        return False
    relative = name.removeprefix(SERVICE)
    return relative in (
        "Containerfile",
        "requirements.in",
        "requirements.txt",
        ".dockerignore",
    ) or bool(re.fullmatch(r"coach/[a-z_]+\.py", relative))


def checksum(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def export_source(root, archive):
    root = Path(root)
    files = sorted(p for p in (root / SERVICE).rglob("*") if p.is_file())
    with tarfile.open(archive, "w:gz") as output:
        for path in files:
            name = path.relative_to(root).as_posix()
            if source_file(name):
                if path.is_symlink():
                    raise ValueError("Release source cannot contain symlinks")
                output.add(path, arcname=name, recursive=False)


def extract_source(archive, destination):
    with tarfile.open(archive) as source:
        members = source.getmembers()
        names = [m.name for m in members]
        required = {SERVICE + n for n in ("Containerfile", "requirements.txt", "coach/worker.py")}
        if (
            len(names) != len(set(names))
            or not required.issubset(names)
            or any(not m.isfile() or not source_file(m.name) for m in members)
            or sum(m.size for m in members) > 10 * 1024**2
        ):
            raise ValueError("Unsafe or incomplete release source archive")
        source.extractall(destination, members=members, filter="data")


def archive_identity(archive, revision, expected=None):
    """Use the config digest, which is stable across Docker and Podman stores."""
    with tarfile.open(archive) as source:

        def read(name):
            path = Path(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unsafe image metadata path")
            member = source.getmember(name)
            if not member.isfile() or member.size > 1024**2:
                raise ValueError("Invalid image metadata")
            return source.extractfile(member).read()

        manifests = json.loads(read("manifest.json"))
        if len(manifests) != 1:
            raise ValueError("Expected one exported image")
        raw = read(manifests[0]["Config"])
        identity = "sha256:" + hashlib.sha256(raw).hexdigest()
        config = json.loads(raw)
        labels = config.get("config", {}).get("Labels", {})
        if (
            config.get("os") != "linux"
            or config.get("architecture") != "amd64"
            or labels.get("org.opencontainers.image.revision") != revision
            or labels.get("org.opencontainers.image.source") != "https://github.com/" + REPOSITORY
            or (expected is not None and identity != expected)
        ):
            raise ValueError("Exported image platform, source, revision or identity mismatch")
        return identity


def validate_release(release, run):
    if (
        release.get("schema") != 1
        or release.get("repository") != REPOSITORY
        or not re.fullmatch(r"[0-9a-f]{40}", release.get("revision", ""))
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", release.get("image_id", ""))
        or not re.fullmatch(r"\d+\.\d+\.\d+", release.get("codex", ""))
        or release["revision"] != run["head_sha"]
        or release.get("ci_run_id") != run["id"]
        or release.get("ci_run_attempt") != run["run_attempt"]
        or release.get("ci_run_number") != run["run_number"]
        or run.get("head_repository", {}).get("full_name") != REPOSITORY
        or run.get("head_branch") != "main"
        or run.get("event") not in ("push", "schedule", "workflow_dispatch")
        or run.get("conclusion") != "success"
        or run.get("path") != ".github/workflows/" + WORKFLOW
    ):
        raise ValueError("Release does not match a successful trusted main-branch CI run")
    for key in ("archive_sha256", "source_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", release.get(key, "")):
            raise ValueError("Missing release asset checksum")


def api(path):
    request = urllib.request.Request(
        "https://api.github.com/repos/" + REPOSITORY + path,
        headers={"User-Agent": "nas-coach-updater", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def newest():
    runs = api("/actions/workflows/" + WORKFLOW + "/runs?branch=main&status=success&per_page=20")
    for run in sorted(
        runs["workflow_runs"], key=lambda r: (r["run_number"], r["run_attempt"]), reverse=True
    ):
        if run.get("event") not in ("push", "schedule", "workflow_dispatch"):
            continue
        tag = f"coach-build-{run['id']}-{run['run_attempt']}"
        try:
            release = api("/releases/tags/" + tag)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue
            raise
        if release.get("draft") or release.get("target_commitish") != run["head_sha"]:
            continue
        assets = {a["name"]: a["browser_download_url"] for a in release["assets"]}
        if set(ASSETS).issubset(assets):
            return run, assets
    return None


def download(url, destination, limit):
    if not url.startswith("https://github.com/" + REPOSITORY + "/releases/download/"):
        raise ValueError("Unexpected release asset location")
    request = urllib.request.Request(url, headers={"User-Agent": "nas-coach-updater"})
    size = 0
    with (
        urllib.request.urlopen(request, timeout=60) as response,
        Path(destination).open("wb") as out,
    ):
        while chunk := response.read(1024**2):
            size += len(chunk)
            if size > limit:
                raise ValueError("Release asset exceeds size limit")
            out.write(chunk)


def stage(state, run, assets):
    directory = Path(state) / f"ci-{run['id']}-{run['run_attempt']}"
    directory.mkdir(mode=0o700, exist_ok=True)
    download(assets[ASSETS[0]], directory / ASSETS[0], 64 * 1024)
    release = json.loads((directory / ASSETS[0]).read_text())
    validate_release(release, run)
    for name, key, limit in (
        (ASSETS[1], "archive_sha256", 2 * 1024**3),
        (ASSETS[2], "source_sha256", 10 * 1024**2),
    ):
        download(assets[name], directory / name, limit)
        if checksum(directory / name) != release[key]:
            raise ValueError("Downloaded release checksum mismatch")
    archive_identity(directory / ASSETS[1], release["revision"], release["image_id"])
    context = directory / "source"
    if context.exists():
        # Interrupted preflight must never retain unverified extra source files.
        import shutil

        shutil.rmtree(context)
    extract_source(directory / ASSETS[2], context)
    return release, directory
