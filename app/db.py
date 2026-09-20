"""Database engine/session. Supabase is used as plain Postgres so we get real transactions."""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings

_settings = get_settings()
engine = create_engine(_settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def check_db() -> bool:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return True
