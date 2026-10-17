# Gmail Ingestion Reliability — Audit & Verification Report

**Scope:** audit of the Gmail ingestion pipeline, database/migration readiness, and
reliability test coverage. No background worker was implemented, no schema was
redesigned, and no production database or Gmail data was modified.

**Date:** 2026-10-14
**Branch:** `self-write`

---

## 1. Executive summary

The Gmail ingestion pipeline is **structurally sound and ready for the background-worker phase**.
Incremental history sync, full re-sync, cursor persistence, MIME body extraction, and
Pub/Sub handling all work and are covered by tests. Raw emails are persisted as `PENDING`
and deduplicated by Gmail message id.

The one gap between the current code and the product's end goal is architectural and
intentional: nothing yet drains the `PENDING` emails through the LangGraph workflow, so
job applications are not created yet. The status-transition helpers for that future
worker already exist (`app/db/email_processing.py`) and are unit-tested but unused.

One minor migration inconsistency was fixed (see §3). No functional bugs were found in
the sync, cursor, or dedup logic. The full test suite passes: **39 passed, 5 skipped**.
The 5 skips are PostgreSQL-only tests gated behind `TEST_DATABASE_URL` (not set in this
environment); the commands to run them are in §4 and §5.

---

## 2. Code audit

### Files inspected

| File | Role | Notes |
|------|------|-------|
| `app/db/models.py` | SQLModel schema | `Email`, `SyncState`, `Application`, `ApplicationEvent`, `ProcessedEmail`, `EmailStatus` |
| `app/db/database.py` | Engine + table creation | `create_db_and_tables()` runs `create_all` then `apply_migrations` |
| `app/db/migrations.py` | Idempotent additive migrations | Postgres-only, no Alembic |
| `app/db/email_processing.py` | Status-transition helpers | `claim_pending_emails`, `mark_email_status` (unused today) |
| `app/gmail/sync.py` | Sync + cursor + MIME extraction | `sync_history`, `full_sync`, `_fetch_and_persist`, `_lock_cursor` |
| `app/gmail/service.py` | Gmail client + watch registration | `build_service`, `register_watch` |
| `app/gmail/oauth.py` | OAuth 2.0 (PKCE) | `get_credentials`, `exchange_code`, etc. |
| `app/main.py` | FastAPI app + Pub/Sub endpoint | `/pubsub/push`, `/gmail/watch`, OAuth routes |
| `app/graph/graph.py` | LangGraph workflow | classify → extract; compiled but not invoked |
| `tests/*` | Test suite | see §5 |

> Note: the task referenced `app/db/repository.py`, `app/gmail/client.py`, and
> `app/gmail/email_processing.py`. None of those files exist in this codebase. The
> audit was adapted to the actual structure above (`app/db/email_processing.py` and
> `app/gmail/sync.py` hold the equivalent functionality).

### Files changed

| File | Change | Reason |
|------|--------|--------|
| `app/db/migrations.py` | `status` column DDL `VARCHAR` → `VARCHAR(10)`; `apply_migrations` accepts an optional `schema` argument | (1) Match the length SQLModel derives from `EmailStatus`. (2) Allow tests to target a throwaway schema without mutating `search_path` (which leaks across Neon pooled connections — see §3). Additive: default behavior unchanged. |
| `tests/test_sync.py` | Added `test_sync_persists_full_email_fields`, `test_sync_commit_failure_does_not_advance_cursor` | Phase 3 coverage: field fidelity + atomic cursor/email commit. |
| `tests/test_handler.py` | Added `test_pubsub_push_duplicate_notification_no_duplicate_emails`, `test_pubsub_push_delayed_notification_uses_stored_cursor` | Phase 3 coverage: duplicate + delayed Pub/Sub notifications. |
| `tests/test_migrations.py` | New file (Postgres-gated) | Phase 2 coverage: migration idempotency, column/index addition, duplicate-message-id handling. |
| `tests/test_postgres_concurrency.py` | Fixture now calls `apply_migrations(engine)` after `create_all` | Mirror production startup (`create_db_and_tables`) so the test reconciles a legacy `emails` table that predates the status columns. |

No production code in `sync.py`, `models.py`, `database.py`, `main.py`, `service.py`,
or `oauth.py` was modified.

### Answers to the Phase 1 questions

1. **How Gmail sync persists raw email records.**
   `sync_history`/`full_sync` call `_fetch_and_persist` (`app/gmail/sync.py:350`), which
   checks `_email_exists`, fetches the full message, runs `extract_email_fields`, and
   `session.add(Email(...))`. The session is committed once at the end of the sync.

2. **Are records persisted as `PENDING`?**
   Yes. `Email.status` defaults to `EmailStatus.PENDING` (`app/db/models.py:181`) and
   sync never sets it explicitly, so every ingested email starts `PENDING`.

3. **Are duplicate Gmail message ids prevented?**
   Yes, in three layers: (a) `Email.message_id` is `unique=True, index=True`
   (`app/db/models.py:168`) producing a unique index `ix_emails_message_id`; (b) the
   `_email_exists` pre-check avoids re-fetching already-stored ids; (c) `_lock_cursor`
   serializes concurrent syncs for the same mailbox with `SELECT … FOR UPDATE`. The
   migration also (re)creates the unique index as a backstop (§4).

4. **How is the history cursor persisted and advanced?**
   Per-mailbox row in `sync_state` (`email_address` unique). `_lock_cursor` reads the
   row under `FOR UPDATE` (inserting a placeholder on first sync), `sync_history` pages
   through `history.list`, and only calls `set_cursor` + `session.commit()` after all
   messages are persisted — so a mid-sync failure leaves the cursor untouched.

5. **How is the LangGraph workflow invoked?**
   It is **not** invoked from any ingestion path today. `app/graph/graph.py` defines and
   compiles `workflow` (classify → extract) but no code calls `workflow.invoke`, and no
   code writes to `applications`/`application_events`/`processed_emails`. The pipeline
   currently stops at "raw email stored as `PENDING`."

6. **How is extracted application info saved and deduplicated?**
   Not implemented yet. The graph's `extract` node returns an `ApplicationExtraction`
   but there is no `persist` node and no repository layer. Dedup columns are designed
   (`ProcessedEmail.message_id` unique, `ApplicationEvent.source_email_id` unique) but
   unused.

7. **Are the status-transition helpers correct and used?**
   `claim_pending_emails` (`FOR UPDATE SKIP LOCKED`) and `mark_email_status` in
   `app/db/email_processing.py` are correct in isolation and unit-tested, but **not
   currently used** by any code path (the worker does not exist yet).

---

## 3. Issues found

### Fixed

- **Migration `status` column length inconsistency (cosmetic).**
  `SQLModel.metadata.create_all` creates `status` as `VARCHAR(10)` (the length SQLModel
  derives from the longest `EmailStatus` value), but `apply_migrations` created it as
  unbounded `VARCHAR`. Both store identical values, so this was not a functional bug,
  but the two code paths produced subtly different schemas. Changed to `VARCHAR(10)` in
  `app/db/migrations.py` for consistency.

- **`test_postgres_concurrency.py` did not reconcile a legacy `emails` table.**
  Run against the actual database, this surfaced as `UndefinedColumn: column "status"
  does not exist` during `SQLModel.metadata.create_all`. `create_all` is additive-only
  and never alters an existing table, so the fixture failed on a legacy `emails` table
  that predates the `status`/`error`/`updated_at` columns. Fixed by calling
  `apply_migrations(engine)` after `create_all` in the fixture, exactly mirroring
  production startup (`create_db_and_tables`).

- **Neon pooled connections reject `search_path` in the connection options.**
  The first migration-test attempt used `connect_args={"options": "-csearch_path=…"}`,
  which Neon's pooled endpoint rejects (`unsupported startup parameter in options:
  search_path`). A fallback that issued `SET search_path` on checkout then leaked that
  session state to *other* clients (observed as a fresh connection reporting a
  stale/dropped `search_path`). Fixed by refactoring `apply_migrations` to accept an
  explicit `schema` argument and having the test create/drop its own schema via fully
  qualified DDL — no `search_path` mutation at all.

### Observed but intentionally not changed (documented risks)

- **No worker drains `PENDING` emails.** Applications are not created until a worker is
  built (out of scope). The ingestion side is complete and correct.
- **README is stale.** It still references SQLite and a `/gmail/webhook` endpoint; the
  code uses PostgreSQL (Neon) and `POST /pubsub/push`. Documentation-only, out of scope.
- **Duplicate `ApplicationStatus` enum.** Defined identically in both `app/db/models.py`
  and `app/graph/graph.py`. Harmless today, but a future drift risk.
- **Migration server defaults vs. model defaults.** `apply_migrations` adds server-side
  defaults (`DEFAULT 'pending'`, `DEFAULT now()`) that the SQLModel fields do not
  declare (they use Python-side `default`/`default_factory`). Functionally equivalent,
  but a freshly `create_all`-ed table and a migrated table differ in whether those
  server defaults exist.
- **`claim_pending_emails` has no crash recovery.** If a future worker crashes after
  claiming, emails remain stuck in `PROCESSING`. The worker phase will need a
  reclaim/stale-timeout mechanism.
- **A single failed `messages.get` aborts the whole sync.** `_fetch_and_persist` does not
  catch per-message errors; one transient/poison message raises and rolls back the sync
  (cursor not advanced). This is the *safe* behavior (no data loss, retryable via
  Pub/Sub), but it means a permanently-failing message blocks progress for that mailbox.

---

## 4. Migration safety

`apply_migrations` (`app/db/migrations.py`) is additive and idempotent:

- Runs only when `engine.dialect.name == "postgresql"` (no-op on SQLite, where
  `create_all` builds fresh tables).
- Adds each missing column independently (`status`, `error`, `updated_at`), so a
  partially-migrated table is completed on the next run.
- Creates `ix_emails_message_id` (unique, the dedup backstop) only if absent **and**
  only when no duplicate `message_id` rows exist; otherwise it warns and skips.
- Creates `ix_emails_status` (non-unique) if absent.
- Re-running is safe: every branch is guarded by "already present?" checks.

Column/index compatibility with SQLModel (verified by reflecting a fresh `create_all`):

| Column | Model type | Migration DDL |
|--------|-----------|---------------|
| `status` | `VARCHAR(10) NOT NULL` | `VARCHAR(10) NOT NULL DEFAULT 'pending'` |
| `error` | `VARCHAR NULL` | `VARCHAR` |
| `updated_at` | `TIMESTAMPTZ NOT NULL` | `TIMESTAMPTZ NOT NULL DEFAULT now()` |
| index `ix_emails_message_id` | unique | `CREATE UNIQUE INDEX …` |
| index `ix_emails_status` | non-unique | `CREATE INDEX …` |

### Safe commands (run manually; do NOT echo the URL)

```bash
cd backend

# 1. Verify which database the configured URL points at (prints db + role, not the URL)
.venv/bin/python -c "import os, sqlalchemy; e=sqlalchemy.create_engine(os.environ['DATABASE_URL']); c=e.connect(); print(c.execute(sqlalchemy.text('SELECT current_database(), current_user')).fetchone())"

# 2. Back up before applying anything
pg_dump "$DATABASE_URL" > "backup_$(date +%F_%H%M%S).sql"

# 3. Apply the migration (idempotent; safe to re-run).
#    It also runs automatically on app startup via create_db_and_tables().
.venv/bin/python -c "from app.db.database import engine; from app.db.migrations import apply_migrations; apply_migrations(engine); print('migrations applied')"
```

> **Do not** run `apply_migrations` against production without first confirming the
> `DATABASE_URL` target with step 1 and obtaining explicit approval. The migration only
> performs additive `ALTER TABLE … ADD COLUMN` / `CREATE INDEX`; it never drops columns,
> tables, or data. It never removes duplicate rows — if duplicates exist it skips the
> unique index and prints a warning.

### Postgres test verification (throwaway database)

The Postgres-only tests (`test_migrations.py`, `test_postgres_concurrency.py`) are
skipped unless `TEST_DATABASE_URL` is set. Use a **throwaway** database/branch (not
production):

```bash
cd backend
TEST_DATABASE_URL=postgresql://… .venv/bin/python -m pytest tests/test_migrations.py tests/test_postgres_concurrency.py -v
```

The migration tests run inside an isolated, uniquely-named schema that is dropped on
teardown, so they do not touch existing tables.

---

## 5. Test results

Command executed (from `backend/`) without `TEST_DATABASE_URL` (SQLite only):

```bash
.venv/bin/python -m pytest tests/ -v
```

Result:

```
39 passed, 5 skipped, 1 warning in 0.48s
```

The 5 skipped tests are the PostgreSQL-only tests (`tests/test_migrations.py` and
`tests/test_postgres_concurrency.py`).

Command executed against the test database (the same URL as production, per
instruction — it is a testing database):

```bash
TEST_DATABASE_URL="$DATABASE_URL" .venv/bin/python -m pytest tests/ -v
```

Result:

```
44 passed, 1 warning in 18.66s
```

No pre-existing failures. The single warning is a third-party deprecation warning in
`starlette.testclient` (the `anyio.abc.BlockingPortal` alias), unrelated to this code.

New/updated tests (all passing):

- `test_sync_persists_full_email_fields` — message id, thread id, sender, recipient,
  subject, date, body, and `PENDING` status all persisted faithfully.
- `test_sync_commit_failure_does_not_advance_cursor` — a commit failure leaves neither
  the cursor nor the email persisted (single-transaction atomicity).
- `test_pubsub_push_duplicate_notification_no_duplicate_emails` — the same Pub/Sub
  notification delivered twice produces exactly one email row.
- `test_pubsub_push_delayed_notification_uses_stored_cursor` — a stale notification with
  an older `historyId` is ignored in favor of the stored cursor.
- `tests/test_migrations.py` — 3 Postgres-gated migration tests (skipped here).

Existing reliability coverage that already existed and still passes: cursor-not-advanced
on fetch failure, repeated-message-id dedup, already-persisted skip, expired-history
recovery, full-sync re-anchor ordering, MIME/body extraction edge cases, and
`FOR UPDATE SKIP LOCKED` claim behavior.

---

## 6. Manual verification guide (test email)

1. **Start the backend** (with a valid `.env`):
   ```bash
   cd backend && uvicorn app.main:app --reload --port 8000
   ```
   Startup runs `create_db_and_tables()`, which applies any pending migrations.

2. **Authorize Gmail** (one-time): open `http://localhost:8000/auth/google`, complete
   the consent flow, and confirm `{"status": "authorized"}`.

3. **Register the push watch**: `curl -X POST http://localhost:8000/gmail/watch`.
   This stores the initial history cursor if none exists.

4. **Send yourself a test email** from another account (e.g. with a distinct subject
   line). This triggers a Pub/Sub push to `/pubsub/push`.

5. **Verify ingestion** by querying the database (using a tool like `psql` or Neon's
   SQL editor — do not print the connection string):

   ```sql
   SELECT message_id, subject, body, status, received_at
   FROM emails
   ORDER BY received_at DESC
   LIMIT 5;
   ```

   Expect a row for the test email with `status = 'pending'` and the subject/body you
   sent.

   ```sql
   SELECT email_address, history_id, updated_at FROM sync_state;
   ```

   Expect one row for your mailbox with an advancing `history_id`.

6. **Re-trigger the same notification** (or re-send) and confirm the message id is not
   duplicated (the `emails` count for that `message_id` stays 1).

7. **Confirm no processing has occurred yet** (expected at this stage): there should be
   no new rows in `applications` — a background worker is not yet wired up.

---

## 7. Known limitations

- **No worker**: `PENDING` emails accumulate but are never processed into applications.
- **Stale README**: references SQLite and an outdated webhook path (`/gmail/webhook`
  vs. actual `/pubsub/push`).
- **No per-message failure isolation**: a single failing `messages.get` aborts the sync
  (safe, but blocks progress on a poison message).
- **No stale `PROCESSING` reclaim**: `claim_pending_emails` offers no crash recovery;
  the worker phase must handle it.
- **Duplicate `ApplicationStatus` enum** across `models.py` and `graph.py`.
- **Migration vs. `create_all` server-default asymmetry** (documented in §3).
- **Postgres tests require `TEST_DATABASE_URL`**; they are skipped otherwise but were
  run and pass against the test database (see §5).
- **Single mailbox**: the whole pipeline assumes one Gmail account (single-user).

---

## 8. Next steps (before the background worker)

1. Implement the worker that drains `emails` via `claim_pending_emails` →
   LangGraph `workflow.invoke` → `mark_email_status`.
2. Implement the persistence layer for `Application` / `ApplicationEvent` /
   `ProcessedEmail` (dedup on `ProcessedEmail.message_id`), and reconcile the duplicate
   `ApplicationStatus` enums.
3. Add a stale-`PROCESSING` reclaim mechanism (timeout-based).
4. Consider per-message error capture (mark individual emails `FAILED` with the error
   instead of aborting the whole sync).
5. Update the README to reflect Postgres/Neon and the `/pubsub/push` endpoint.
6. Decide whether to fold the hand-written `apply_migrations` into Alembic once the
   schema stabilizes.

---

*This report is based on actual code inspection and test runs. The PostgreSQL-only
tests (migration behavior, unique-constraint enforcement, concurrent full-sync
serialization) were executed against the configured testing database (same URL as
production, used as a testing DB per instruction) and passed. No production data was
modified and no credentials were printed.*
