# migrations.py - minimal, idempotent schema migrations (no Alembic yet).
#
# The project creates tables with ``SQLModel.metadata.create_all``, which is
# additive only: it creates missing tables but never alters existing ones.
# ``apply_migrations`` reconciles pre-existing tables (e.g. the ``emails``
# table created before the processing-status column existed).

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def apply_migrations(engine: Engine, schema: str | None = None) -> None:
    """Apply additive migrations idempotently.

    Only meaningful on PostgreSQL; SQLite is used in tests where tables are
    created fresh by ``create_all``.

    ``schema`` optionally targets a specific schema (used by the integration
    tests to isolate against a throwaway schema without mutating ``search_path``,
    which leaks across Neon's pooled connections). When omitted, the default
    ``search_path`` applies, matching production startup.
    """
    if engine.dialect.name != "postgresql":
        return

    qualified = f'"{schema}".' if schema else ""
    table_ref = f'{qualified}"emails"'

    with engine.begin() as conn:
        inspector = inspect(conn)
        if "emails" not in inspector.get_table_names(schema=schema):
            return

        columns = {
            column["name"]
            for column in inspector.get_columns("emails", schema=schema)
        }

        additions = [
            # VARCHAR(10) matches the length SQLModel derives from the
            # ``EmailStatus`` enum (longest value is "processing").
            ("status", "VARCHAR(10) NOT NULL DEFAULT 'pending'"),
            ("error", "VARCHAR"),
            ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT now()"),
        ]
        for name, ddl in additions:
            if name not in columns:
                conn.execute(text(f'ALTER TABLE {table_ref} ADD COLUMN "{name}" {ddl}'))

        indexes = {
            index["name"]
            for index in inspector.get_indexes("emails", schema=schema)
        }

        # Unique index on message_id is the dedup backstop. Only create it when
        # there are no duplicate message_ids, otherwise it would fail.
        if "ix_emails_message_id" not in indexes:
            duplicates = conn.execute(
                text(
                    "SELECT count(*) FROM ("
                    f"SELECT message_id FROM {table_ref} "
                    "GROUP BY message_id HAVING count(*) > 1"
                    ") AS dupes"
                )
            ).scalar()
            if duplicates:
                print(
                    "WARNING: emails.message_id has duplicates; "
                    "skipping unique index creation."
                )
            else:
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX ix_emails_message_id "
                        f"ON {table_ref} (message_id)"
                    )
                )

        if "ix_emails_status" not in indexes:
            conn.execute(
                text(f"CREATE INDEX ix_emails_status ON {table_ref} (status)")
            )

    # ── gmail_tokens ─────────────────────────────────────────────────────────
    # Create the table if it was not caught by create_all (i.e. on an existing
    # deployment that pre-dates this model).
    if "gmail_tokens" not in inspector.get_table_names(schema=schema):
        qualified_gt = f'{qualified}"gmail_tokens"'
        conn.execute(
            text(
                f"CREATE TABLE {qualified_gt} ("
                "id SERIAL PRIMARY KEY, "
                "email_address VARCHAR NOT NULL UNIQUE, "
                "credentials_json TEXT NOT NULL, "
                "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            )
        )
        conn.execute(
            text(
                f"CREATE UNIQUE INDEX ix_gmail_tokens_email_address "
                f"ON {qualified_gt} (email_address)"
            )
        )

