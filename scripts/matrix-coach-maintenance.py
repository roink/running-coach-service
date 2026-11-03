#!/usr/bin/env python3
"""Guarded Matrix/coach releases with verified encrypted off-host recovery bundles.

NAS only. Shares the general container-maintenance lock. --check is read-only.
No production data rollback is automated. Private reports never contain credentials.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coach_release

ROOT = Path("/tank/services/compose")
STATE = ROOT / "matrix-coach-updates"
BACKUPS = Path("/tank/backups/matrix-coach")
COACH = Path("/tank/services/appdata/running-coach")
ALERTS = Path("/var/lib/nas-matrix-alerts")
SOURCE = ROOT / "build/running-coach/current"
ORDER = (
    "matrix-db",
    "matrix",
    "matrix-tunnel",
    "coach-egress",
    "coach-data",
    "coach-worker",
    "matrix-alerts",
    "coach-gateway",
)
COACH_SERVICES = ("coach-egress", "coach-data", "coach-worker", "matrix-alerts", "coach-gateway")
REGISTRY = {
    "matrix-db": "docker.io/library/postgres:17-alpine",
    "matrix": "ghcr.io/element-hq/synapse:latest",
    "matrix-tunnel": "docker.io/cloudflare/cloudflared:latest",
}
DATASETS = ("tank/services/appdata", "tank/services/appdata/running-coach", "tank/services/compose")
TAG = "nas-matrix-coach"


class Deferred(RuntimeError):
    """No live upgrade has started; retry later."""


def write(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def run(*args, timeout=300, input=None):
    result = subprocess.run(args, input=input, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Full diagnostics stay root-private on the NAS. Never print arguments or stderr.
        (STATE / "last-command-error.log").write_text(result.stdout + result.stderr)
        raise RuntimeError(f"{args[0]} exited {result.returncode}; inspect private error log")
    return result.stdout


def active(name):
    return (
        subprocess.run(
            ["systemctl", "is-active", "--quiet", name + ".service"], capture_output=True
        ).returncode
        == 0
    )


def image(name):
    return run("podman", "inspect", "nas-" + name, "--format", "{{.Image}}").strip()


def guard():
    run("/usr/local/sbin/nas-storage-ready")
    pool = json.loads(run("zpool", "status", "-j", "--json-int", "tank"))["pools"]["tank"]
    if pool.get("error_count", 0):
        raise Deferred("Pool has data errors")
    if run("zpool", "list", "-H", "-o", "health", "tank").strip() != "ONLINE":
        raise Deferred("Pool must be ONLINE")
    for dataset in DATASETS:
        if run("zfs", "get", "-H", "-o", "value", "mounted", dataset).strip() != "yes":
            raise Deferred("Required dataset not mounted")
    if (
        int(run("zfs", "get", "-Hp", "-o", "value", "available", "tank/services/appdata"))
        < 20 * 1024**3
    ):
        raise Deferred("Less than 20 GiB free")
    if not all(active(n) for n in ORDER) or not active("backrest"):
        raise Deferred(
            "Stack and Backrest must already be active; inactive services are not enabled"
        )
    if (ROOT / "updates/blocked.json").exists():
        raise Deferred("General NAS updates require review")


def candidate_command(candidate, *command, volumes=(), timeout=300, user="65532:65532"):
    return run(
        "podman",
        "run",
        "--rm",
        "--network=none",
        "--user=" + user,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--security-opt=apparmor=nas-coach-worker",
        "--memory=1g",
        "--cpus=1",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=256m",
        "--env=CODEX_HOME=/tmp/codex",
        "--env=HOME=/tmp",
        "--entrypoint=python",
        *[arg for v in volumes for arg in ("--volume", v)],
        candidate,
        *command,
        timeout=timeout,
    )


def fetch_coach(report):
    try:
        newest = coach_release.newest()
    except (OSError, TimeoutError) as error:
        raise Deferred("CI release preflight unavailable; no live upgrade applied") from error
    if newest is None:
        raise Deferred("No successful downloadable coach CI build available")
    ci_run, assets = newest
    installed_path = STATE / "coach-installed.json"
    installed = json.loads(installed_path.read_text()) if installed_path.exists() else {}
    available = (ci_run["run_number"], ci_run["run_attempt"])
    previous = (installed.get("ci_run_number", 0), installed.get("ci_run_attempt", 0))
    if available <= previous:
        return {}
    try:
        release, directory = coach_release.stage(STATE, ci_run, assets)
    except (OSError, TimeoutError) as error:
        raise Deferred("CI download unavailable; no live upgrade applied") from error
    # All asset checks complete before importing the image, pausing or mounting history.
    run("podman", "load", "--input", str(directory / "coach-image.tar.gz"), timeout=900)
    identity = release["image_id"]
    info = json.loads(run("podman", "image", "inspect", identity))[0]
    labels = info.get("Labels") or info.get("Config", {}).get("Labels", {})
    if (
        info["Id"].removeprefix("sha256:") != identity.removeprefix("sha256:")
        or info.get("Os") != "linux"
        or info.get("Architecture") != "amd64"
        or labels.get("org.opencontainers.image.revision") != release["revision"]
    ):
        raise RuntimeError("Imported CI image identity mismatch")
    version = run(
        "podman",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--entrypoint=codex",
        identity,
        "--version",
    ).strip()
    if version != "codex-cli " + release["codex"]:
        raise RuntimeError("CI image Codex version mismatch")
    # Recheck this binary offline using the NAS's existing confinement profile.
    output = candidate_command(identity, "-I", "-m", "coach.maintenance", "candidate")
    (STATE / "coach-candidate-checks.log").write_text(output)
    report["candidate"] = {
        "codex": release["codex"],
        "image": identity,
        "context": str(directory / "source/services/running-coach"),
        "release": release,
    }
    return {n: identity for n in COACH_SERVICES}


def pull_matrix(report):
    images = {}
    for name, reference in REGISTRY.items():
        try:
            run("podman", "pull", reference, timeout=900)
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            raise Deferred("Registry preflight unavailable; no live upgrade applied") from error
        images[name] = run("podman", "image", "inspect", reference, "--format", "{{.Id}}").strip()
    postgres = run(
        "podman",
        "run",
        "--rm",
        "--network=none",
        "--entrypoint=postgres",
        images["matrix-db"],
        "--version",
    ).strip()
    if not re.search(r"PostgreSQL\) 17\.", postgres):
        raise RuntimeError("PostgreSQL major boundary violated")
    synapse = run(
        "podman",
        "run",
        "--rm",
        "--network=none",
        "--entrypoint=python",
        images["matrix"],
        "-c",
        "import synapse; print(synapse.__version__)",
    ).strip()
    if not re.fullmatch(r"1\.\d+\.\d+", synapse):
        raise RuntimeError("Synapse prerelease/major upgrade requires review")
    report["registry_versions"] = {"postgres": postgres, "synapse": synapse}
    return images


def manifest(directory):
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def job_counts():
    with sqlite3.connect("file:" + str(COACH / "worker/jobs.sqlite3") + "?mode=ro", uri=True) as db:
        return dict(db.execute("SELECT state,count(*) FROM jobs GROUP BY state"))


def quiesce(report, legacy=False):
    report["phase"] = "quiescing"
    write(STATE / "last-run.json", report)
    # Pause claims before draining; gateway is stopped first so no new jobs arrive.
    run("systemctl", "stop", "coach-gateway.service")
    acknowledged = COACH / "worker/maintenance.paused"
    acknowledged.unlink(missing_ok=True)
    (COACH / "worker/maintenance.pause").touch(mode=0o600)
    deadline = time.monotonic() + 660
    while True:
        counts = job_counts()
        idle = not counts.get("running", 0)
        # Acknowledgement comes from the idle worker loop, closing the race
        # between its last flag check and a new claim. Bootstrap requires an
        # empty queue because the old worker cannot acknowledge this protocol.
        if idle and (not counts.get("queued", 0) if legacy else acknowledged.exists()):
            break
        if time.monotonic() > deadline:
            raise Deferred("Coach turn did not drain; no upgrade applied")
        time.sleep(2)
    run("systemctl", "stop", "coach-worker.service")
    if job_counts().get("running", 0):
        raise RuntimeError("Coach claim raced maintenance pause; review interrupted job")
    # Freeze all application writers before dumping, then freeze PostgreSQL too.
    for name in reversed(ORDER[:-1]):
        if name not in ("coach-gateway", "coach-worker", "matrix-db"):
            run("systemctl", "stop", name + ".service")


def dump_database(directory):
    with (directory / "synapse.dump").open("wb") as output:
        result = subprocess.run(
            ["podman", "exec", "nas-matrix-db", "pg_dump", "-U", "synapse", "-d", "synapse", "-Fc"],
            stdout=output,
            stderr=subprocess.PIPE,
            timeout=300,
        )
    if result.returncode:
        raise RuntimeError("PostgreSQL dump failed")


def restic(*args):
    config = json.loads(Path("/tank/services/appdata/backrest/config.json").read_text())
    repos = [r for r in config["repos"] if r["id"] == "Sciebo"]
    if len(repos) != 1 or repos[0].get("commandPrefix"):
        raise RuntimeError("Review Backrest repository integration")
    repo = repos[0]
    if "\n" in repo["uri"] or "\n" in repo["password"]:
        raise RuntimeError("Unsupported repository credential encoding")
    # Secrets travel over stdin, never command arguments, logs, reports or workstation files.
    return run(
        "podman",
        "exec",
        "-i",
        "nas-backrest",
        "sh",
        "-ec",
        "read -r RESTIC_REPOSITORY; read -r RESTIC_PASSWORD; "
        'export RESTIC_REPOSITORY RESTIC_PASSWORD; exec restic --retry-lock=5m "$@"',
        "nas-stack-backup",
        *args,
        input=repo["uri"] + "\n" + repo["password"] + "\n",
        timeout=1800,
    )


def verify_sqlite(directory):
    count = 0
    for path in directory.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                continue
        with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Restored SQLite integrity check failed")
        count += 1
    if count < 4:
        raise RuntimeError("Missing restored bot/worker SQLite stores")
    return count


def verify_postgres(directory, old_image):
    name = "nas-matrix-restore-check"
    run(
        "podman",
        "run",
        "--detach",
        "--name",
        name,
        "--network=none",
        "--memory=512m",
        "--cpus=1",
        "--tmpfs=/var/lib/postgresql/data:rw,size=384m",
        "--tmpfs=/var/run/postgresql:rw,size=16m",
        "--env=POSTGRES_HOST_AUTH_METHOD=trust",
        "--volume",
        str(directory / "synapse.dump") + ":/restore.dump:ro",
        old_image,
    )
    try:
        for _ in range(60):
            ready = subprocess.run(
                ["podman", "exec", name, "pg_isready", "-U", "postgres"],
                capture_output=True,
                timeout=10,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Isolated PostgreSQL restore did not start")
        run("podman", "exec", name, "createdb", "-U", "postgres", "recovered")
        run(
            "podman",
            "exec",
            name,
            "pg_restore",
            "-U",
            "postgres",
            "--no-owner",
            "--no-acl",
            "--exit-on-error",
            "-d",
            "recovered",
            "/restore.dump",
        )
        counts = run(
            "podman",
            "exec",
            name,
            "psql",
            "-U",
            "postgres",
            "-d",
            "recovered",
            "-Atc",
            "SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM events), "
            "(SELECT count(*) FROM rooms);",
        ).strip()
        if counts != (directory / "database-counts.txt").read_text().strip():
            raise RuntimeError("Restored PostgreSQL row counts differ")
    finally:
        run("podman", "rm", "--force", name)


def backup(report):
    directory = BACKUPS / report["stamp"]
    directory.mkdir(parents=True, mode=0o700)
    report["backup"] = str(directory)
    write(STATE / "last-run.json", report)
    dump_database(directory)
    counts = run(
        "podman",
        "exec",
        "nas-matrix-db",
        "psql",
        "-U",
        "synapse",
        "-d",
        "synapse",
        "-Atc",
        "SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM events), "
        "(SELECT count(*) FROM rooms);",
    )
    (directory / "database-counts.txt").write_text(counts)
    run("systemctl", "stop", "matrix-db.service")
    snapshots = [d + "@matrix-coach-" + report["stamp"] for d in DATASETS]
    report["snapshots"] = snapshots
    write(STATE / "last-run.json", report)
    run("zfs", "snapshot", *snapshots)
    for snap in snapshots:
        run("zfs", "hold", "nas-matrix-coach", snap)
    # Explicit paths include the separate child dataset and eMMC bot encryption store.
    # Private deployment configuration and exact Quadlets stay inside the encrypted backup.
    private = [
        "matrix-homeserver.yaml",
        "matrix-postgres.env",
        "matrix-credentials.json",
        "matrix-init.sql",
        "matrix-tunnel-token",
        "coach-service-credentials.json",
        "coach-gateway.json",
        "matrix-alerts.json",
        "coach-data.json",
        "coach-mongodb.json",
        "coach-worker-token",
        "coach-data-token",
    ]
    run(
        "tar",
        "--acls",
        "--xattrs",
        "--numeric-owner",
        "-cpf",
        str(directory / "state.tar"),
        "-C",
        "/tank/services/appdata",
        "matrix",
        "running-coach",
        "-C",
        "/var/lib",
        "nas-matrix-alerts",
        "-C",
        str(ROOT),
        "quadlet",
        *["private/" + n for n in private],
    )
    write(directory / "images.json", report["previous_images"])
    write(directory / "sha256.json", manifest(directory))
    report["phase"] = "uploading-backup"
    write(STATE / "last-run.json", report)
    output = restic("backup", "--json", "--tag", TAG, "/backup/matrix-coach/" + report["stamp"])
    summaries = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    snapshot = next(s["snapshot_id"] for s in summaries if s.get("message_type") == "summary")
    report["restic_snapshot"] = snapshot
    report["phase"] = "restoring-backup"
    write(STATE / "last-run.json", report)
    verify_backup(report, directory, snapshot)


def verify_backup(report, directory, snapshot):
    verify = Path("/tank/services/appdata/backrest/nas-stack-restore") / report["stamp"]
    verify.mkdir(mode=0o700, parents=True, exist_ok=True)
    restic("restore", snapshot, "--target", "/config/nas-stack-restore/" + report["stamp"])
    restored = verify / "backup/matrix-coach" / report["stamp"]
    expected = json.loads((directory / "sha256.json").read_text())
    if any(
        hashlib.sha256((restored / p).read_bytes()).hexdigest() != h for p, h in expected.items()
    ):
        raise RuntimeError("Off-host restored checksums differ")
    extracted = directory / "restore-check"
    extracted.mkdir(mode=0o700)
    run(
        "tar",
        "--acls",
        "--xattrs",
        "--numeric-owner",
        "-xpf",
        str(restored / "state.tar"),
        "-C",
        str(extracted),
    )
    report["restored_sqlite_stores"] = verify_sqlite(extracted)
    verify_postgres(restored, report["previous_images"]["matrix-db"])
    # Prove critical non-database material exists in the restored archive as well.
    if not list((extracted / "matrix/synapse").glob("*.signing.key")):
        raise RuntimeError("Restored Synapse signing key missing")
    for relative in (
        "running-coach/codex/auth.json",
        "running-coach/workspace/MEMORY.md",
        "private/coach-gateway.json",
        "private/matrix-alerts.json",
    ):
        if not (extracted / relative).is_file():
            raise RuntimeError("Required recovery file missing")
    if "candidate" in report:
        # Resume an isolated copy restored from off-host storage, with no auth file.
        with sqlite3.connect(
            "file:" + str(extracted / "running-coach/worker/jobs.sqlite3") + "?mode=ro", uri=True
        ) as db:
            row = db.execute("SELECT value FROM metadata WHERE key='thread_id'").fetchone()
        if row:
            thread_file = directory / "thread.json"
            write(thread_file, {"thread_id": json.loads(row[0])})
            thread_file.chmod(0o644)
            owner = (COACH / "codex").stat()
            candidate_command(
                report["candidate"]["image"],
                "-I",
                "-m",
                "coach.maintenance",
                "resume",
                user=str(owner.st_uid) + ":" + str(owner.st_gid),
                volumes=(
                    str(extracted / "running-coach/codex/sessions") + ":/history:ro",
                    str(thread_file) + ":/thread.json:ro",
                ),
            )
            thread_file.unlink()
        report["restored_thread_resumed"] = bool(row)
    report["backup_verified"] = True
    write(
        directory / "verified.json",
        {
            "restic_snapshot": snapshot,
            "sqlite_stores": report["restored_sqlite_stores"],
            "postgres_restore": True,
        },
    )
    shutil.rmtree(verify)
    shutil.rmtree(extracted)


def rotate_database_password():
    """Rotate a disclosed application password; secret values never leave the NAS."""
    import secrets

    path = ROOT / "private/matrix-homeserver.yaml"
    config = json.loads(path.read_text())
    password = secrets.token_hex(32)
    # Use the existing trusted local DB connection and send SQL through stdin only.
    run(
        "podman",
        "exec",
        "-i",
        "nas-matrix-db",
        "psql",
        "-U",
        "synapse",
        "-d",
        "synapse",
        "-v",
        "ON_ERROR_STOP=1",
        input="ALTER ROLE synapse PASSWORD '" + password + "';\n",
    )
    old_password = config["database"]["args"]["password"]
    for name in ("matrix-credentials.json", "matrix-init.sql"):
        saved = ROOT / "private" / name
        if saved.exists():
            saved.write_text(saved.read_text().replace(old_password, password))
    config["database"]["args"]["password"] = password
    temporary = path.with_suffix(".rotate")
    stat = path.stat()
    temporary.write_text(json.dumps(config, indent=2) + "\n")
    os.chown(temporary, stat.st_uid, stat.st_gid)
    temporary.chmod(stat.st_mode & 0o777)
    temporary.replace(path)


def promote(images, report):
    report["phase"] = "promoting"
    write(STATE / "last-run.json", report)
    for name, value in images.items():
        path = ROOT / "quadlet" / (name + ".container.d") / "95-stack-release.conf"
        path.parent.mkdir(exist_ok=True)
        # No native AutoUpdate label: this controller owns the coordinated transaction.
        path.write_text("[Container]\nImage=" + value + "\nPull=never\nAutoUpdate=\n")
    run("systemctl", "daemon-reload")


def readiness(name):
    if name in ("coach-gateway", "matrix-alerts", "coach-worker", "coach-data"):
        mode = "bot" if name in ("coach-gateway", "matrix-alerts") else name.removeprefix("coach-")
        return run("podman", "exec", "nas-" + name, "python", "-I", "-m", "coach.maintenance", mode)
    if name == "matrix-db":
        return run(
            "podman",
            "exec",
            "nas-matrix-db",
            "psql",
            "-U",
            "synapse",
            "-d",
            "synapse",
            "-Atc",
            "SELECT 1;",
        )
    if name == "matrix":
        return run(
            "podman",
            "exec",
            "nas-matrix",
            "python",
            "-c",
            "import urllib.request; "
            'urllib.request.urlopen("http://127.0.0.1:8008/_matrix/client/versions",timeout=5).read()',
        )
    if name == "matrix-tunnel":
        info = json.loads(run("podman", "inspect", "nas-" + name))[0]
        since = info["State"]["StartedAt"]
        # Read current-container logs only; a prior registration cannot satisfy this probe.
        result = subprocess.run(
            ["podman", "logs", "--since", since, "nas-" + name],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        if "Registered tunnel connection" not in result.stdout + result.stderr:
            raise RuntimeError("Tunnel not registered")
    elif name == "coach-egress":
        result = run(
            "podman",
            "exec",
            "nas-coach-worker",
            "python",
            "-c",
            'import socket; s=socket.create_connection(("coach-egress",3128),5); '
            's.sendall(b"CONNECT 127.0.0.1:22 HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\n\\r\\n"); '
            'assert b"403" in s.recv(1024); s.close(); print("private proxy denied")',
        )
        return result


def start_stack(legacy=False):
    for name in ORDER:
        run("systemctl", "start", name + ".service")
        if legacy or name == "coach-egress":
            continue  # Probe from the confined worker once it is running.
        deadline = time.monotonic() + 180
        while True:
            try:
                readiness(name)
                break
            except (RuntimeError, subprocess.SubprocessError):
                if time.monotonic() > deadline:
                    raise RuntimeError("Readiness failed: " + name) from None
                time.sleep(3)
    if legacy:
        return
    readiness("coach-egress")
    server = json.loads((ROOT / "private/matrix-homeserver.yaml").read_text())["server_name"]
    config = json.loads((ROOT / "private/coach-gateway.json").read_text())
    request = urllib.request.Request(
        "https://" + server + "/_matrix/client/v3/account/whoami",
        headers={
            "Authorization": "Bearer " + config["access_token"],
            "User-Agent": "nas-matrix-maintenance/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        if json.load(response)["user_id"] != config["user_id"]:
            raise RuntimeError("Public authenticated Matrix check failed")
    run("/usr/local/sbin/nas-check-networks", timeout=180)
    # Additional sandbox checks cover mounts/capabilities, independent of TCP rules.
    info = json.loads(run("podman", "inspect", "nas-coach-worker"))[0]
    allowed = {
        "/workspace": (str(COACH / "workspace"), True),
        "/state": (str(COACH / "worker"), True),
        "/codex": (str(COACH / "codex"), True),
        "/incoming": (str(COACH / "incoming"), False),
        "/run/secrets/worker-token": (str(ROOT / "private/coach-worker-token"), False),
        "/run/secrets/data-token": (str(ROOT / "private/coach-data-token"), False),
    }
    actual = {
        m["Destination"]: (m["Source"], m["RW"]) for m in info["Mounts"] if m["Type"] == "bind"
    }
    if actual != allowed:
        raise RuntimeError("Worker bind mounts changed")
    if info["Config"]["User"].split(":")[0] in ("0", "root", ""):
        raise RuntimeError("Worker must be non-root")
    security = info["HostConfig"].get("SecurityOpt", [])
    if not info["HostConfig"].get("ReadonlyRootfs") or not {
        "no-new-privileges",
        "apparmor=nas-coach-worker",
    }.issubset(security):
        raise RuntimeError("Worker sandbox changed")
    run(
        "podman",
        "exec",
        "nas-coach-worker",
        "python",
        "-c",
        "from pathlib import Path; "
        's=dict(line.split(":",1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line); '
        'assert int(s["CapEff"].strip(),16)==0 and s["NoNewPrivs"].strip()=="1"',
    )


def resume():
    (COACH / "worker/maintenance.pause").unlink(missing_ok=True)
    (COACH / "worker/maintenance.paused").unlink(missing_ok=True)


def execute(args, report):
    guard()
    report["previous_images"] = {n: image(n) for n in ORDER}
    images = {}
    if args.mode == "matrix":
        images = pull_matrix(report)
    elif args.mode == "coach":
        images = fetch_coach(report)
    changes = {n: value for n, value in images.items() if value != report["previous_images"][n]}
    if not changes and args.mode != "backup":
        if "candidate" in report:
            write(STATE / "coach-installed.json", report["candidate"]["release"])
        report["status"] = "current"
        return
    for n, old in report["previous_images"].items():
        run(
            "podman",
            "tag",
            old,
            "localhost/nas-stack-rollback/" + n + ":" + report["stamp"].lower(),
        )
    report["changes"] = changes
    # Save exact release definitions, not just mutable registry tags.
    recovery = STATE / ("release-" + report["stamp"])
    shutil.copytree(ROOT / "quadlet", recovery)
    report["release_backup"] = str(recovery)
    quiesced = False
    promoted = False
    try:
        quiesced = True
        quiesce(report, legacy=args.bootstrap)
        if args.rotate_database_password:
            rotate_database_password()
        backup(report)
        if changes:
            promoted = True
            promote(changes, report)
        report["phase"] = "verifying"
        write(STATE / "last-run.json", report)
        start_stack()
        complete_release(report)
    except BaseException:
        if quiesced and not promoted:
            # No replacement has started; restore the prior running services.
            # Keep claims paused until startup checks have passed.
            start_stack(legacy=args.bootstrap)
            resume()
        elif promoted:
            (STATE / "live-blocked").touch(mode=0o600)
            # Image-only downgrade can corrupt migrated state. Keep affected writers stopped.
            for name in reversed(ORDER):
                subprocess.run(
                    ["systemctl", "stop", name + ".service"], capture_output=True, timeout=180
                )
        raise


def complete_release(report):
    if "candidate" in report:
        # Preserve the exact CI source/lock locally; building never runs on the NAS.
        context = Path(report["candidate"]["context"])
        shutil.copytree(context, ROOT / "build/running-coach/service", dirs_exist_ok=True)
        for name in ("requirements.txt", "Containerfile"):
            shutil.copy2(context / name, SOURCE / "services/running-coach" / name)
        run(
            "podman",
            "tag",
            report["candidate"]["image"],
            "localhost/nas-apps/running-coach:current",
        )
        write(STATE / "coach-installed.json", report["candidate"]["release"])
    resume()
    report["phase"] = "complete"
    report["status"] = "updated" if report["changes"] else "backup-verified"


def startup_guard():
    if (STATE / "live-blocked").exists():
        raise RuntimeError("Live upgrade failed; coordinated recovery is required")
    path = STATE / "last-run.json"
    if not path.exists():
        return
    report = json.loads(path.read_text())
    if report.get("status") != "running" or report.get("phase") == "preflight":
        return
    try:
        current = Path("/proc/" + str(report["pid"]) + "/stat").read_text().split()[21]
        if current == report["process_start"]:
            return
    except (OSError, KeyError):
        pass
    raise RuntimeError("Interrupted stack transaction requires review before startup")


def prune_recovery(report):
    # Only this controller's fully verified, completed recovery points are eligible.
    directory = Path(report["backup"])
    write(directory / "complete.json", {"stamp": report["stamp"], "snapshots": report["snapshots"]})
    completed = sorted(p for p in BACKUPS.iterdir() if (p / "complete.json").is_file())
    for old in completed[:-3]:
        record = json.loads((old / "complete.json").read_text())
        if not re.fullmatch(r"\d{8}T\d{6}Z", old.name) or record["stamp"] != old.name:
            raise RuntimeError("Unexpected recovery directory")
        for snap in record["snapshots"]:
            if snap not in [d + "@matrix-coach-" + old.name for d in DATASETS]:
                raise RuntimeError("Unexpected recovery snapshot")
            run("zfs", "release", "nas-matrix-coach", snap)
            if run("zfs", "get", "-H", "-o", "value", "userrefs", snap).strip() == "0":
                run("zfs", "destroy", snap)
        shutil.rmtree(old)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["check", "matrix", "coach", "backup", "startup-guard"])
    parser.add_argument("--bootstrap", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--rotate-database-password", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        sys.exit("Run as root on the NAS")
    os.umask(0o077)
    if args.bootstrap:
        parser.error("Legacy local-build bootstrap is retired; use qualified CI releases")
    if args.mode == "startup-guard":
        startup_guard()
        return
    if args.mode == "check":
        print(
            json.dumps(
                {
                    "installed_images": {n: image(n) for n in ORDER},
                    "available_ci": coach_release.newest(),
                    "installed_ci": json.loads((STATE / "coach-installed.json").read_text())
                    if (STATE / "coach-installed.json").exists()
                    else None,
                    "blocked": (STATE / "blocked.json").exists(),
                    "last_run": json.loads((STATE / "last-run.json").read_text())
                    if (STATE / "last-run.json").exists()
                    else None,
                },
                indent=2,
            )
        )
        return
    with Path("/run/nas-container-maintenance.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(75)
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (STATE / "blocked.json").exists():
            sys.exit("Stack updates blocked; inspect private blocked.json")
        previous = (
            json.loads((STATE / "last-run.json").read_text())
            if (STATE / "last-run.json").exists()
            else {}
        )
        if previous.get("status") == "running":
            write(STATE / "blocked.json", previous)
            sys.exit("Interrupted maintenance requires review; claims remain paused")
        report = {
            "stamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "mode": args.mode,
            "status": "running",
            "phase": "preflight",
            "pid": os.getpid(),
            "process_start": Path("/proc/self/stat").read_text().split()[21],
        }
        write(STATE / "last-run.json", report)
        try:
            execute(args, report)
            if report["status"] in ("updated", "backup-verified"):
                prune_recovery(report)
        except Deferred as error:
            report.update(status="deferred", error=str(error))
            sys.exit(75)
        except BaseException as error:
            report.update(status="failed", error=type(error).__name__ + ": " + str(error))
            write(STATE / "blocked.json", report)
            raise
        finally:
            write(STATE / "last-run.json", report)
            if report["status"] in ("current", "updated", "backup-verified"):
                write(STATE / (args.mode + "-last.json"), report)
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
