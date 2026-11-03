# Service repository

Only generic coach/bot source, synthetic tests and release tooling belong here.
Never add athlete notes, conversation history, Codex homes, credentials, private
configuration, runtime databases, deployment exports or NAS logs, even temporarily.
Use the explicit exporter in `scripts/coach_release.py` for release source assets.
Keep the image build context allowlisted by `services/running-coach/.dockerignore`.

GitHub-hosted CI builds and tests the shared image. Its build job has read-only
permissions; only a separate publication job can create releases. No NAS access
or application secrets are needed. The NAS performs private history compatibility,
backups, drains and guarded promotion locally; never move these checks into CI.

Run `uvx ruff==0.17.3 check`, `uvx ruff==0.17.3 format --check` and the fixture
tests described in README.md. CI also exercises the real Codex binary offline,
without login or inference. Do not run NAS operational commands as tests.
