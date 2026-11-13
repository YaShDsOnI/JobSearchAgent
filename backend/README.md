# JobFinderAgent backend

Phase 5C scope (see `../AGENTS.md`). In addition to what is listed below, it provides the application review package API (`POST /jobs/{id}/applications`, `GET /applications`, `GET /applications/{id}`, `PUT /applications/{id}/answers/{key}`, `POST /applications/{id}/approve`, `POST /applications/{id}/approval/revoke`, `POST /applications/{id}/skip`). It prepares and records candidate approval only; nothing is ever sent (see `../docs/application-agent.md`). This package provides the authenticated profile/preferences API, deterministic explainable matching, encrypted local artifact storage, factual CV drafting with provenance validation, local DOCX/PDF rendering, human CV review, and a provider-agnostic job discovery pipeline (source registry, fail-closed authorization policy, validation, normalization, deduplication, versioned snapshots, provenance, lifecycle, retries/idempotency, source health, audit) with sanitized fixture providers, migrations, health checks, and test infrastructure. See `../docs/source-connectors.md`.

## Local setup

1. Copy `.env.example` to `.env`.
2. Generate secrets outside source control. For example, generate a 32-byte URL-safe base64 artifact key and a long session secret with a system secret generator.
3. Create a project virtual environment in `backend/.venv` (ignored by git) and install the package with development extras into it. Do not use a shared global interpreter: it may hold dependency versions below the floors in `pyproject.toml`.
   - `python -m venv .venv`
   - Windows: `.venv\Scripts\activate`; Linux/macOS: `source .venv/bin/activate`
   - `python -m pip install -e ".[dev]"`

   Run every command below from the activated environment.
4. Apply migrations: `alembic upgrade head`.
5. Optionally load the sanitized local fixture catalog through the discovery pipeline: `python -m app.seed_fixture`.
6. Start the API: `python -m uvicorn app.main:app --reload --port 8000`.
7. Process queued ingestion tasks (created by `POST /api/v1/connectors/{id}/ingest`): `python -m app.worker --once`.

The fixture loader performs no network access and creates records whose URLs use `example.invalid`. No live connector is registered by this repository, and `SOURCE_NETWORK_ACCESS_ENABLED` defaults to `false`. Shared-catalog sources can be triggered only by users with the `operator` role.

## Operations

- Designate an operator (the person must first register through the app):
  `python -m app.admin promote-operator --email <email> --reason "<ticket or justification>"`.
  Remove the role with `demote-operator` (the last active operator cannot be removed), and list operators with `list-operators`. Changes are audited; there is intentionally no HTTP endpoint for role changes.
- Reconcile encrypted artifacts left behind by interrupted work:
  `python -m app.admin reconcile-artifacts` (dry run) or `... --apply`. Exit code 2 means rows reference missing blobs, which needs investigation.

- Schedule a connector: `python -m app.admin set-schedule --connector <name> --interval-minutes 60` (or `--disable`). Run `python -m app.scheduler --once` from a timer (or `--loop`) to enqueue due runs, and `python -m app.worker` (or `--loop`) to execute them. Both loops stop cleanly on SIGTERM/SIGINT after the current task or tick.
- Register the Ashby public job board provider (read-only; starts disabled). This needs a recorded product-owner authorization for the specific board:
  `python -m app.admin register-ashby --name <n> --board <BoardName> --employer "<Employer>" --basis official_api --evidence-ref <url> --reviewed-by <role> --terms-version <v> [--approve]`.
  Then run `python -m app.admin set-connector-enabled --connector <n> --enable --reason "<why>"` (use `--disable` as the kill switch). Live fetching also requires `SOURCE_NETWORK_ACCESS_ENABLED=true` and `SOURCE_EGRESS_ALLOWLIST=api.ashbyhq.com`.
- Source health: `GET /api/v1/connectors/health` or `python -m app.admin health-report` (exit code 3 when a source is failing, blocked, suspended, or overdue). Suspended sources stay suspended until reset.
- Health alerts: set `ALERT_WEBHOOK_REF` (for example `file:alert_webhook`) and `ALERT_WEBHOOK_HOST`. The scheduler loop then sends `firing`/`resolved` notifications. `python -m app.admin send-health-alerts` sends them once (exit code 4 when a delivery failed).

### Live network settings (off by default)

`SOURCE_NETWORK_ACCESS_ENABLED=false` and an empty `SOURCE_EGRESS_ALLOWLIST` keep all live source traffic disabled. Do not enable them before the Phase 5 prerequisites in `../docs/roadmap.md` are met. Related limits:

- `SOURCE_HTTP_CONNECT_TIMEOUT_SECONDS`
- `SOURCE_HTTP_READ_TIMEOUT_SECONDS`
- `SOURCE_HTTP_TOTAL_TIMEOUT_SECONDS`
- `SOURCE_HTTP_MAX_RESPONSE_BYTES`
- `SOURCE_HTTP_MAX_REDIRECTS`
- `SOURCE_HTTP_MAX_REQUESTS_PER_RUN`

Connector credentials are never stored in the database. Configure a reference: `file:<name>` reads `<SECRETS_DIR>/<name>` (a secret-manager-projected file; required outside development and test), and `env:JFA_SOURCE_SECRET_<NAME>` reads the environment (development and test only).

Outside development and test, `SOURCE_NETWORK_ACCESS_ENABLED=true` also requires `SOURCE_EGRESS_PROXY_URL` (for example `http://egress-proxy:3128`). Every source request is then tunneled through that CONNECT-only proxy, and no other connection can be opened.

## Phase 6 sandbox adapter (local only)

The only destination is the local sandbox ATS, a dummy employer site in this repository.

1. `python -m playwright install chromium` (once).
2. `python -m sandbox_ats --port 8100`. It binds to loopback only.
3. `python -m app.admin register-sandbox` registers the sandbox jobs and the destination, which starts disabled.
4. `python -m app.admin set-destination-enabled --destination sandbox_ats --enable --reason "<why>"`. Use `--disable` as the kill switch.
5. Run the API with `APPLICATION_SUBMISSION_ENABLED=true` (and `SANDBOX_ATS_ORIGIN=http://127.0.0.1:8100`, the default).
6. Run the isolated runner: `python -m app.runner --loop`.

In the app, prepare and approve a sandbox job's package, choose "Fill the sandbox form for review", check what was filled, then confirm. Design: `../docs/application-agent.md`, "Phase 6: sandbox browser adapter".

## Reference deployment

`docker compose --profile app up --build` (from the repository root) runs migrations, the API (on `127.0.0.1:8000`), a continuous worker and scheduler, and the egress proxy. The worker and scheduler sit on an internal network whose only route out is the proxy. Put `POSTGRES_PASSWORD`, `SESSION_SECRET`, `ARTIFACT_ENCRYPTION_KEY`, and any `SOURCE_*` and `ALERT_*` settings in a root `.env` (never committed). Put secret files in `deploy/secrets/` (git-ignored), or point `SECRETS_HOST_DIR` elsewhere. Before enabling network access in any deployed environment, run `deploy/egress-proxy/verify.py` there (see `../docs/testing.md`). Design: `../docs/source-connectors.md`, "Deployment enforcement".

## Checks

Run everything with the local CI runner from the repository root: `python tools/ci.py` (or `--quick` for lint and SQLite tests). See `../docs/testing.md`, "Local CI". The individual commands are:

- `python -m pytest` (in-memory SQLite)
- `TEST_DATABASE_URL=postgresql+psycopg://<user>:<password>@127.0.0.1:<port>/<disposable_db> python -m pytest` (PostgreSQL). The target schema is dropped and rebuilt from the migrations, so use a throwaway database only.
- `python -m ruff check app migrations tests`
- `python -m mypy app`
- `python -m compileall -q app migrations tests`
- `alembic upgrade head`, `alembic downgrade base`, `alembic upgrade head`, then `alembic check` (expects no drift)
- `pip-audit` (installed with the `dev` extras)
