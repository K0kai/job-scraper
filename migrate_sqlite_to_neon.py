"""Copy the existing SQLite panel data into an initialized Neon database."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parent
TABLE_ORDER = (
    "settings",
    "jobs",
    "runs",
    "ai_decisions",
    "cover_letters",
    "resumes",
    "form_field_rules",
    "applications",
    "form_answers",
    "ai_answer_cache",
    "copilot_asks",
    "queue_jobs",
    "event_logs",
)
REPLACED_TABLES = {"settings", "form_field_rules", "event_logs"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite",
        type=Path,
        default=ROOT / "jobs.db",
        help="SQLite database to copy (default: jobs.db beside this script)",
    )
    args = parser.parse_args()
    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        parser.error("Set DATABASE_URL to the Neon connection string before running this script.")
    source_path = args.sqlite.expanduser().resolve()
    if not source_path.is_file():
        parser.error(f"SQLite database not found: {source_path}")

    source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        source_tables = {
            row["name"]
            for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        tables = [name for name in TABLE_ORDER if name in source_tables]
        missing = set(TABLE_ORDER) - source_tables
        if missing:
            print("SQLite source is missing tables (skipping): " + ", ".join(sorted(missing)))

        with psycopg.connect(database_url, row_factory=dict_row) as target:
            with target.cursor() as cur:
                for table in tables:
                    if table in REPLACED_TABLES:
                        continue
                    cur.execute(f'SELECT COUNT(*) AS n FROM "{table}"')
                    if cur.fetchone()["n"]:
                        raise RuntimeError(
                            f'Neon table "{table}" is not empty. Migration stopped to avoid duplicate or mixed data.'
                        )

                # Replace SQL-installed starter rules and any first-boot logs with SQLite data.
                for table in ("form_field_rules", "event_logs"):
                    if table in tables:
                        cur.execute(f'TRUNCATE TABLE "{table}" RESTART IDENTITY')

                for table in tables:
                    source_columns = [
                        row["name"] for row in source.execute(f'PRAGMA table_info("{table}")')
                    ]
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = %s "
                        "ORDER BY ordinal_position",
                        (table,),
                    )
                    target_columns = {row["column_name"] for row in cur.fetchall()}
                    columns = [column for column in source_columns if column in target_columns]
                    if not columns:
                        continue

                    rows = source.execute(f'SELECT * FROM "{table}"').fetchall()
                    if not rows:
                        print(f"{table}: 0 rows")
                        continue

                    quoted_columns = ", ".join(f'"{column}"' for column in columns)
                    placeholders = ", ".join(["%s"] * len(columns))
                    if table == "settings":
                        conflict = " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
                    else:
                        conflict = " ON CONFLICT DO NOTHING"
                    statement = (
                        f'INSERT INTO "{table}" ({quoted_columns}) '
                        f"VALUES ({placeholders}){conflict}"
                    )
                    values = [tuple(row[column] for column in columns) for row in rows]
                    cur.executemany(statement, values)

                    if "id" in columns:
                        cur.execute(
                            f"SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                            f"GREATEST(COALESCE(MAX(id), 1), 1), MAX(id) IS NOT NULL) "
                            f'FROM "{table}"',
                            (table,),
                        )
                    print(f"{table}: copied {len(rows)} row(s)")

        print("SQLite data migration to Neon completed.")
        return 0
    finally:
        source.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
