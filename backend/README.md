# JobFinderAgent backend

Phase 4 local/fixture scope. This package provides the authenticated profile/preferences API, deterministic explainable matching, encrypted local artifact storage, factual CV drafting with provenance validation, local DOCX/PDF rendering, human CV review, and a provider-agnostic job discovery pipeline (source registry, fail-closed authorization policy, validation, normalization, deduplication, versioned snapshots, provenance, lifecycle, retries/idempotency, source health, audit) with sanitized fixture providers, migrations, health checks, and test infrastructure. See `../docs/source-connectors.md`.

## Local setup

1. Copy `.env.example` to `.env`.
2. Generate secrets outside source control. For example, generate a 32-byte URL-safe base64 artifact key and a long session secret with a system secret generator.
3. Install the package and development extras: `python -m pip install -e ".[dev]"`.
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

- Schedule a connector: `python -m app.admin set-schedule --connector <name> --interval-minutes 60` (or `--disable`). Run `python -m app.scheduler --once` from a timer (or `--loop`) to enqueue due runs, and `python -m app.worker` to execute them.
- Register the Ashby public job board provider (read-only; starts disabled). This needs a recorded product-owner authorization for the specific board:
  `python -m app.admin register-ashby --name <n> --board <BoardName> --employer "<Employer>" --basis official_api --evidence-ref <url> --reviewed-by <role> --terms-version <v> [--approve]`.
  Then run `python -m app.admin set-connector-enabled --connector <n> --enable --reason "<why>"` (use `--disable` as the kill switch). Live fetching also requires `SOURCE_NETWORK_ACCESS_ENABLED=true` and `SOURCE_EGRESS_ALLOWLIST=api.ashbyhq.com`.
- Source health: `GET /api/v1/connectors/health` or `python -m app.admin health-report` (exit code 3 when a source is failing, blocked, suspended, or overdue). Suspended sources stay suspended until reset.

### Live network settings (off by default)

`SOURCE_NETWORK_ACCESS_ENABLED=false` and an empty `SOURCE_EGRESS_ALLOWLIST` keep all live source traffic disabled. Do not enable them before the Phase 5 prerequisites in `../docs/roadmap.md` are met. Related limits:

- `SOURCE_HTTP_CONNECT_TIMEOUT_SECONDS`
- `SOURCE_HTTP_READ_TIMEOUT_SECONDS`
- `SOURCE_HTTP_TOTAL_TIMEOUT_SECONDS`
- `SOURCE_HTTP_MAX_RESPONSE_BYTES`
- `SOURCE_HTTP_MAX_REDIRECTS`
- `SOURCE_HTTP_MAX_REQUESTS_PER_RUN`

Connector credentials are never stored in the database. Configure a reference such as `env:JFA_SOURCE_SECRET_<NAME>`, and provide the value through the environment or a secret manager.

## Checks

- `python -m pytest` (in-memory SQLite)
- `TEST_DATABASE_URL=postgresql+psycopg://<user>:<password>@127.0.0.1:<port>/<disposable_db> python -m pytest` (PostgreSQL). The target schema is dropped and rebuilt from the migrations, so use a throwaway database only.
- `python -m ruff check app migrations tests`
- `python -m mypy app`
- `python -m compileall -q app migrations tests`
- `alembic upgrade head`, `alembic downgrade base`, `alembic upgrade head`, then `alembic check` (expects no drift)
- `pip-audit` (installed with the `dev` extras)
