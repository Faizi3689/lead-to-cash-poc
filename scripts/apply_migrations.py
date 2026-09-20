"""Apply db/*.sql files in filename order, each exactly once.

Usage (from the project root, venv active):
    python -m scripts.apply_migrations

Applied files are recorded in `schema_migrations` with a checksum, so re-running is safe.
If an already-applied file has been edited, the script stops instead of guessing:
create a new numbered file (e.g. 003_...sql) for schema changes.
"""
import hashlib
import sys
from pathlib import Path

from sqlalchemy import text

from app.db import engine

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db"


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def main() -> int:
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    if not files:
        print(f"No migration files found in {MIGRATIONS_DIR}")
        return 1

    with engine.begin() as conn:
        conn.execute(text(
            "create table if not exists schema_migrations ("
            " filename text primary key, checksum text not null,"
            " applied_at timestamptz not null default now())"
        ))
        conn.execute(text("alter table schema_migrations enable row level security"))

    for path in files:
        sql = path.read_text(encoding="utf-8")
        checksum = _checksum(sql)
        with engine.begin() as conn:  # one transaction per file: all or nothing
            row = conn.execute(
                text("select checksum from schema_migrations where filename = :f"), {"f": path.name}
            ).first()
            if row:
                if row.checksum != checksum:
                    print(f"STOP  {path.name} was changed after being applied. "
                          f"Put schema changes in a new numbered file instead.")
                    return 1
                print(f"skip  {path.name} (already applied)")
                continue

            # Raw driver cursor with no parameters: runs the multi-statement file as-is.
            conn.connection.dbapi_connection.cursor().execute(sql)
            conn.execute(
                text("insert into schema_migrations (filename, checksum) values (:f, :c)"),
                {"f": path.name, "c": checksum},
            )
            print(f"apply {path.name}")

    print("Migrations complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
