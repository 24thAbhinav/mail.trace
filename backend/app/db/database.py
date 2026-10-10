"""Database engine, table creation, and session dependency."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

from dotenv import load_dotenv
from sqlmodel import Session, SQLModel, create_engine

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "")

# SQLAlchemy 2.0 requires postgresql:// instead of postgres://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, echo=False)


def create_db_and_tables() -> None:
    """Create all tables registered on SQLModel metadata (idempotent)."""
    # Import models so they are registered on SQLModel.metadata before create_all.
    from app.db import models  # noqa: F401

    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a database session."""
    with Session(engine) as session:
        yield session
