"""SQLAlchemy engine/session setup for the cloud control plane."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from .config import get_settings

settings = get_settings()

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

Base = declarative_base()


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  ensure models are registered

    Base.metadata.create_all(bind=engine)
    _apply_additive_migrations()


def _apply_additive_migrations() -> None:
    """Add new columns to pre-existing tables (prototype-grade, additive only).

    ``create_all`` never alters existing tables, so newly-introduced columns are
    applied here. Each statement runs in its own transaction and is ignored if it
    has already been applied."""
    from sqlalchemy import text

    statements = [
        "ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT false",
        "ALTER TABLE search_documents ADD COLUMN category VARCHAR",
        "ALTER TABLE linking_codes ADD COLUMN kind VARCHAR DEFAULT 'appliance'",
        "ALTER TABLE desktop_agents ADD COLUMN agent_token_hash VARCHAR",
        "ALTER TABLE appliances ADD COLUMN agent_token_hash VARCHAR",
    ]
    for statement in statements:
        try:
            with engine.begin() as conn:
                conn.execute(text(statement))
        except Exception:
            pass  # column already exists or dialect variance — safe to skip
