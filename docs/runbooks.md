# Operations runbooks

Status: Phase 7 (private beta hardening), first increment, 2026-11-14. Decision record: `docs/roadmap.md`, "Decision record: Phase 7 first increment (2026-11-14)".

These runbooks cover the reference deployment in `compose.yaml`. Every command below exists in this repository. Where a step needs something the repository does not provide yet, the runbook says so instead of describing a procedure that does not exist.

General rules for every incident:

- Contain first: use a kill switch, then investigate. Every switch here is read fresh, with no restart needed, unless the step says otherwise.
- Do not paste CVs, application answers, fill manifests, session cookies, or secrets into tickets, chat, or logs. Refer to IDs and hashes. Audit events already hold only IDs, codes, and counts.
- Record what you did, when, and why: commands that change state take `--reason` and write audit events.
- An incident that may have exposed candidate data can trigger legal notification duties. Escalate to the product owner the same day. Legal review of launch jurisdictions is a Phase 7 acceptance criterion that is not complete yet.

## Backup and restore

### What a backup contains

`python -m app.backup create` writes one directory, `jfa-backup-<UTC time>-<id>`:

- `database.enc`: every application table, compressed and encrypted with AES-GCM under a key derived from `BACKUP_ENCRYPTION_KEY`. Per-table row digests are inside the ciphertext.
- `artifacts/`: every blob referenced by `stored_artifacts`, copied byte for byte. Blobs are already encrypted under `ARTIFACT_ENCRYPTION_KEY`.
- `manifest.json`: format, backup ID, time, schema revision, and the SHA-256 of every file. It holds no personal data and is MAC'd, so any edit is detected.

The database is read in one `REPEATABLE READ` transaction, which gives one consistent snapshot. The directory is written as `.partial` and renamed only when complete. If a referenced blob is missing, the backup fails. Run `python -m app.admin reconcile-artifacts` and investigate before retrying.

### Keys

| Key | Used by | Needed to restore | Stored |
|---|---|---|---|
| `BACKUP_ENCRYPTION_KEY` | Backup CLI only. Never set on the API, worker, scheduler, or runner. | Yes | In the secret manager, never next to the backups |
| `ARTIFACT_ENCRYPTION_KEY` | App and backup CLI | Yes. Blobs stay encrypted under it. | In the secret manager, never next to the backups |

Generate a backup key with `python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`. It must decode to 32 bytes and differ from the artifact key; the CLI refuses otherwise. Losing either key makes every backup unrecoverable. Escrow both keys before the first backup.

### Schedule (reference deployment)

Set `BACKUP_ENCRYPTION_KEY` and optionally `BACKUP_HOST_DIR` (default `./backups`, git-ignored) in `.env`. The container runs as UID 10001, so on Linux create the host directory first, owned by that UID with mode `0700`. Then:

```sh
docker compose --profile ops run --rm backup create --out-root /backups --ledger /backups/deletion-ledger.json
docker compose --profile ops run --rm backup verify --backup /backups/<directory>
docker compose --profile ops run --rm backup prune --out-root /backups --keep-days 35
```

- Run `create` at least daily from the host scheduler (cron or a systemd timer). The repository does not ship a host scheduler.
- `--ledger` merges completed deletions into the deletion ledger in the same step (see "Deletion replay").
- Run `verify` after every `create`. A backup that was never verified does not count.
- Copy each verified directory and the ledger off the host. The repository does not ship off-site replication; choose a target the product owner approves for personal data.
- Retention is 35 days (`prune --keep-days 35`). `prune` deletes only directories this tool created, always keeps the newest complete backup, and removes abandoned `.partial` directories past the cutoff. Apply the same retention to off-site copies. A deleted candidate's data can survive in backups for at most this long, and a restore cannot bring it back because of deletion replay.

### Deletion replay

A backup taken before a candidate deleted their profile still holds the deleted data. Every restore, whatever produced it (including a database provider's point-in-time recovery), must be followed by deletion replay before anyone uses the data.

- The ledger (`deletion-ledger.json`) lists completed `profile_delete` requests: data request ID, user ID, and completion time. It holds no other personal data. It is MAC'd with the backup key, so a forged entry cannot trigger a deletion.
- Entries are only ever added, never removed. Keep the ledger longer than the oldest backup, and copy it off-site with the backups.
- Replay is idempotent. A deletion counts as applied when the database already holds that request, or a later completed deletion for the same user. Data the candidate created after their deletion stays.
- Run `python -m app.backup update-ledger --ledger FILE` after any manual deletion so the ledger never lags the database by more than one backup.

### Restore

Restore only into a new, empty environment, never over a live one.

1. Contain the failed environment: stop its API, worker, scheduler, and runner so nothing writes to it.
2. Provision a new PostgreSQL database and an empty artifact volume. Use the release whose code matches the backup's `alembic_revision`, which `verify` prints.
3. Migrate the empty database to that revision: `python -m alembic upgrade <revision>`.
4. Verify the backup in the new environment: `python -m app.backup verify --backup DIR`. This decrypts every blob with the artifact key and checks its plaintext SHA-256 and size against its row.
5. Restore with the latest ledger: `python -m app.backup restore --backup DIR --ledger FILE`, with `DATABASE_URL` and `ARTIFACT_STORAGE_PATH` pointing at the new environment. Restore refuses a non-empty database, a non-empty artifact store, or a different schema revision. It reads every table back inside the transaction and commits only an exact copy. On any failure it removes the blobs it copied. Deletion replay runs right after the commit, and the result is recorded in a `backup.restored` audit event.
6. Run `python -m app.admin reconcile-artifacts` (dry run). It must report no missing files.
7. Keep the kill switches closed (`SOURCE_NETWORK_ACCESS_ENABLED=false`, `APPLICATION_SUBMISSION_ENABLED=false`, destination disabled) until a smoke check passes, then reopen them deliberately.
8. Rotate `SESSION_SECRET` if the old environment may have been compromised. Candidates then sign in again.

`--skip-deletion-replay --reason "..."` exists only for a restore drill into a scratch environment that will be destroyed. Never use it for an environment candidates will use.

### Restore drill (monthly, and before the pilot)

Restore the latest verified backup into a throwaway environment, following steps 2 to 6 above, then:

- check that `restore` printed the expected row and artifact counts and a deletion replay summary;
- sign in with a test account that exists in the backup and confirm a CV downloads;
- destroy the scratch database, artifact volume, and any copies.

Record the date, backup ID, duration, and result. A drill that fails is an incident.

## Leaked secret or token

| Leaked | Contain | Rotate | Notes |
|---|---|---|---|
| `SESSION_SECRET` | Rotate immediately. Sessions are stateless JWTs, so rotation revokes every session. | Set a new value of at least 32 characters, then restart the API. | There is no per-user session revocation. |
| A user's session cookie | Rotate `SESSION_SECRET`. That is the only revocation. | — | The session TTL is `SESSION_TTL_MINUTES` (default 60). |
| Connector credential (`file:<name>`) | `python -m app.admin set-connector-enabled --connector N --disable --reason R` | Replace the file in `SECRETS_DIR`, then revoke the old credential with the provider. | Phase 5B's Ashby provider uses no credential. |
| Alert webhook URL | Revoke it at the receiver. | Replace the `ALERT_WEBHOOK_REF` file. | — |
| `BACKUP_ENCRYPTION_KEY` | Treat every backup as exposed and restrict access to backup storage. | Generate a new key. Take and verify a new backup with a new ledger file (`create --ledger NEW`). The live database records every deletion, including replayed ones, so the new ledger is complete. Then delete the old backups and the old ledger. | — |
| `ARTIFACT_ENCRYPTION_KEY` | Restrict access to the artifact volume and backups. | **Gap:** there is no re-encryption tool yet. The `encryption_key_ref` column exists for it. Until one exists, escalate to the product owner. | Blobs in backups stay readable with the old key. |
| Database credentials | Change the PostgreSQL password and update `POSTGRES_PASSWORD`. | — | Review `audit_events` for unexpected actors. |

After any leak, review `audit_events` around the exposure window (role changes, connector and destination switches, deletions, backups) and record the findings.

## Unintended or suspicious submission

The only destination is the local sandbox ATS (Phase 6), so no real employer can receive anything. Still treat any submission the candidate did not confirm as a critical incident: it means a gate failed.

1. Stop all submissions: `python -m app.admin set-destination-enabled --destination sandbox_ats --disable --reason R`. This takes effect before the next click. Also set `APPLICATION_SUBMISSION_ENABLED=false` and restart the runner.
2. Preserve the evidence. The submission row holds the review hash, `fill_hash`, `submit_clicked_at`, and the confirmation ID. The fill manifest holds the values sent. The application timeline holds each step. Do not copy the values out; refer to the IDs.
3. Compare against the gates: an active approval matching the review hash, the candidate's confirmation of the `fill_hash`, and the switches at click time.
4. Tell the candidate what was sent, using the reconstructible record.
5. Keep the destination disabled until a regression test covers the cause.

## Source-policy violation

Examples: a provider's terms changed, a rate limit was exceeded, a block or challenge appeared, or a host outside the reviewed allowlist was contacted.

1. Disable the connector: `python -m app.admin set-connector-enabled --connector N --disable --reason R`. For anything broader, set `SOURCE_NETWORK_ACCESS_ENABLED=false` (restart the worker and scheduler).
2. Inspect `python -m app.admin health-report` and the connector's `source_runs` (error codes only, no payloads).
3. Never work around a block, challenge, or terms change. Re-enable only after a new policy review recorded with `register-ashby` (new `--terms-version` and `--evidence-ref`).
4. If postings from a source must be removed, the owner of a private source can delete it (`DELETE /connectors/{id}`), which removes derived data. Then run `reconcile-artifacts --apply`.

## Model provider incident

Not applicable yet: no model is called (see `AGENTS.md`). Before any model integration, this section must name the provider, region, retention, and the switch that stops calls.

## Data export failure

The candidate export is `GET /api/v1/profile/export`. It covers the profile, preferences, consents, applications and packages, submissions, tracker entries, and CV versions with their claims.

1. Reproduce with a test account in a scratch environment restored from a backup, not with the candidate's own session.
2. A 5xx response has a `request_id`. Find the error by that ID in logs; logs hold no field values.
3. Fix the defect, then add a regression test next to the export tests (`tests/test_application_tracker.py`, `tests/test_phase6_submission.py`, `tests/test_phase5c_applications.py`).
4. Track the candidate's request in the support ticket (there is no export-request endpoint yet) and tell them when the export will be available. Follow the response deadline legal review sets.

## Deletion request verification

`DELETE /api/v1/profile` deletes the candidate's data and records a completed `profile_delete` data request. To confirm a deletion:

- the export for that user shows no profile, CV versions, applications, or tracker entries (preferences appear emptied);
- `python -m app.admin reconcile-artifacts` reports no orphaned or missing blobs;
- run `python -m app.backup update-ledger` so a later restore replays the deletion;
- backups hold the data for at most the 35-day retention, and deletion replay prevents its return.

The deletion and replay behaviour is tested in `tests/test_phase7_backup.py` and the profile deletion tests.
