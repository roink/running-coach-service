# Running coach and Matrix bots

Generic Python services and a shared container image for a running coach worker,
Matrix gateway, notification sender, bounded training-data reader and egress proxy.
Each entry point runs as a separate confined container on the NAS.

## Build and deployment boundary

GitHub-hosted CI resolves an exact stable Codex npm release and hash-locked Python
dependencies, builds Linux/amd64, runs synthetic coach/Matrix and release-safety
tests, and tests the real Codex app-server protocol offline without login or
inference. Builds run on pushes to `main`, pull requests, manual dispatch and
monthly on the first at 00:25 UTC. Actions are pinned to commit IDs.

The build job has read-only repository permissions and no NAS access or application
secrets. A separate fresh job publishes the tested image archive, allowlisted
source and checksummed release manifest. It never executes the build artifacts.
No repository secrets or external credentials are configured.

The NAS downloads public release assets without a GitHub token. It checks the
successful main-branch workflow, commit, run/attempt, SHA256s, platform, image
config ID and revision before importing. Newer Docker OCI exports and legacy
Docker archives both use the config digest recognized by Podman.

Private athlete notes, training data, history, bot encryption stores, runtime
databases, private configuration and authentication remain on the NAS. They are
never part of this repository, image context, source assets, CI fixtures or logs.
`.dockerignore` and the source exporter both allow only the service code and
dependency manifests. Never upload a NAS directory or private recovery bundle.

NAS-only `scripts/matrix-coach-maintenance.py` retains the existing shared lock,
storage and readiness gates, turn drain, encrypted off-host backup verification,
isolated restored-history check, held recovery snapshots and guarded promotion.
It polls for successful CI builds every 15 minutes and leaves unchanged services
running. The five coach/bot containers share the image; Synapse, PostgreSQL and
the Matrix tunnel keep their existing independent registry maintenance.

The NAS helper and its `coach_release.py` sibling are installed deliberately;
downloading an image never installs or executes host maintenance scripts.
Do not invoke the operational controller on a workstation or use it as a test.

## Local validation

Requires Python 3.12 and `uv`:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --require-hashes -r services/running-coach/requirements.txt
.venv/bin/python -m unittest discover -s tests -v
uvx ruff==0.17.3 check
uvx ruff==0.17.3 format --check
```

Tests use only synthetic fixtures, temporary directories and mocked external
commands. CI additionally needs Docker to run `scripts/ci_build.py`; that entry
point expects the normal `GITHUB_SHA`, `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT` and
`GITHUB_RUN_NUMBER` values supplied by Actions. No application credentials are
needed or accepted. Runtime service configuration is provided only as protected
NAS bind mounts and environment files, outside this repository.
