"""Check that the baseline migration covers everything the database has.

    .venv/Scripts/python.exe scripts/db_verify_baseline.py [--dsn ...]

Read-only. Compares ``db/migrations/005_transfer_baseline.sql`` against a live
database and reports anything the live one has that the migration does not
declare - which is the dangerous direction, because that is what a transfer
would silently leave behind.

Reports the other direction too (declared but absent), which is how the missing
``runs.title_locked`` column was found: the code writes it, the migration
declares it, and the database has never had it.

Deliberately a crude text scan rather than a SQL parser. The point is to catch
a table or column that was forgotten, and for that a regex over CREATE TABLE
and ADD COLUMN is enough - a parser would be more code and no more truthful.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402

BASELINE = REPO / "db" / "migrations" / "005_transfer_baseline.sql"


def _declared(sql: str) -> dict[str, set[str]]:
    """Tables and columns the migration declares."""
    tables: dict[str, set[str]] = {}

    for match in re.finditer(
        r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);", sql, re.DOTALL
    ):
        name, body = match.group(1), match.group(2)
        cols: set[str] = set()
        for raw in body.split("\n"):
            line = raw.strip()
            if not line or line.startswith("--"):
                continue
            first = line.split()[0].strip('",')
            # Table constraints, and the continuation lines they wrap onto -
            # a multi-line FOREIGN KEY puts REFERENCES at the start of the
            # next line, which reads exactly like a column name.
            if first.upper() in {
                "PRIMARY",
                "FOREIGN",
                "UNIQUE",
                "CHECK",
                "CONSTRAINT",
                "REFERENCES",
            }:
                continue
            cols.add(first)
        tables[name] = cols

    for match in re.finditer(
        r"ALTER TABLE\s+(\w+)\s+ADD COLUMN IF NOT EXISTS\s+(\w+)", sql
    ):
        tables.setdefault(match.group(1), set()).add(match.group(2))

    return tables


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default="")
    args = parser.parse_args()

    dsn = (args.dsn or settings.database_url or "").replace("+asyncpg", "", 1)
    declared = _declared(BASELINE.read_text(encoding="utf-8"))

    conn = await asyncpg.connect(dsn)
    try:
        live: dict[str, set[str]] = {}
        for row in await conn.fetch(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position"
        ):
            live.setdefault(row["table_name"], set()).add(row["column_name"])
    finally:
        await conn.close()

    problems = 0

    missing_tables = sorted(set(live) - set(declared))
    if missing_tables:
        problems += len(missing_tables)
        print("TABLES THE MIGRATION DOES NOT DECLARE (a transfer would lose these):")
        for name in missing_tables:
            print(f"    {name}  ({len(live[name])} columns)")
    else:
        print(f"All {len(live)} live tables are declared.")

    for name in sorted(set(live) & set(declared)):
        gap = sorted(live[name] - declared[name])
        if gap:
            problems += len(gap)
            print(f"COLUMNS MISSING FROM THE MIGRATION - {name}: {gap}")

    print()
    extra_tables = sorted(set(declared) - set(live))
    if extra_tables:
        print(f"Declared but not on this database: {extra_tables}")
    for name in sorted(set(live) & set(declared)):
        ahead = sorted(declared[name] - live[name])
        if ahead:
            # Not a failure. The migration is allowed to be ahead - that is
            # what applying it fixes.
            print(f"Declared but not on this database - {name}: {ahead}")

    print()
    if problems:
        print(f"{problems} gap(s). Fix the migration before transferring.")
        return 1
    print("The migration covers every table and column this database has.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
