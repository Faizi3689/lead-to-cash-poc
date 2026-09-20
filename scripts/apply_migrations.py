"""Apply db/NNN_*.sql files in filename order, each exactly once.

Usage (from the project root, venv active):
    python -m scripts.apply_migrations                 # apply pending files
    python -m scripts.apply_migrations --status        # show what is applied / pending
    python -m scripts.apply_migrations --baseline 002  # mark 001..002 as applied WITHOUT running them
                                                       # (use when those tables were created manually,
                                                       #  e.g. via the Supabase SQL Editor)

Applied files are recorded in `schema_migrations` with a checksum, so re-running is safe.
If an already-applied file has been edited, the script stops: put schema changes in a new
numbered file (e.g. 004_...sql) instead of editing an old one.
"""
import argparse
import hashlib
import sys
from pathlib import Path

from sqlalchemy import text

from app.db import engine

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db"


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def _ensure_table() -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "create table if not exists schema_migrations ("
            " filename text primary key, checksum text not null,"
            " applied_at timestamptz not null default now())"
        ))
        conn.execute(text("alter table schema_migrations enable row level security"))


def _applied() -> dict[str, str]:
    with engine.connect() as conn:
        return dict(conn.execute(text("select filename, checksum from schema_migrations")).all())


def status() -> int:
    applied = _applied()
    for path in _files():
        print(f"{'applied' if path.name in applied else 'PENDING'}  {path.name}")
    return 0


def baseline(upto: str) -> int:
    applied = _applied()
    targets = [p for p in _files() if p.name[:3] <= upto.zfill(3)]
    if not targets:
        print(f"No files match --baseline {upto}")
        return 1
    with engine.begin() as conn:
        for path in targets:
            if path.name in applied:
                print(f"skip     {path.name} (already recorded)")
                continue
            conn.execute(
                text("insert into schema_migrations (filename, checksum) values (:f, :c)"),
                {"f": path.name, "c": _checksum(path.read_text(encoding="utf-8"))},
            )
            print(f"baseline {path.name} (marked as applied, not executed)")
    return 0


def apply() -> int:
    files = _files()
    if not files:
        print(f"No migration files found in {MIGRATIONS_DIR}")
        return 1
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply numbered SQL migrations in db/")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="show applied / pending files")
    group.add_argument("--baseline", metavar="NNN", help="mark files up to NNN as applied without running")
    args = parser.parse_args()

    _ensure_table()
    if args.status:
        return status()
    if args.baseline:
        return baseline(args.baseline)
    return apply()


if __name__ == "__main__":
    sys.exit(main())
