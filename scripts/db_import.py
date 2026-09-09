"""Load a dump from :mod:`scripts.db_export` into a target database.

    .venv/Scripts/python.exe scripts/db_import.py backups/db-20260827-140000 \
        --dsn postgresql://user:pass@new-host:5432/postgres

Apply ``db/migrations/005_transfer_baseline.sql`` to the target FIRST. This
script loads rows; it does not create tables, deliberately - a restore that
invents a schema on the fly is how two databases end up subtly different.

Refuses to touch a table that already has rows unless ``--truncate`` is given,
because the failure mode this protects against - running it twice and doubling
every row - is silent and tedious to unpick.

Everything happens inside ONE transaction. A restore that fails halfway is a
database in a state nobody designed, so either all of it lands or none of it
does.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import asyncpg  # noqa: E402


def _decode(value: Any) -> Any:
    """Reverse ``db_export._encode``."""
    if isinstance(value, dict) and "__type__" in value:
        kind = value["__type__"]
        if kind == "datetime":
            return datetime.fromisoformat(value["value"])
        if kind == "decimal":
            return Decimal(value["value"])
        if kind == "bytes":
            return base64.b64decode(value["value"])
    return value


async def _json_columns(conn: asyncpg.Connection, table: str) -> set[str]:
    """Columns the TARGET stores as json/jsonb.

    asyncpg wants a *string* for a json column, and this dump holds real
    parsed JSON - so those columns are re-serialised on the way in. Asking the
    target rather than assuming means a column someone widened from text to
    jsonb still loads.
    """
    rows = await conn.fetch(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1
          AND data_type IN ('json', 'jsonb')
        """,
        table,
    )
    return {r["column_name"] for r in rows}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", help="directory produced by db_export.py")
    parser.add_argument("--dsn", required=True, help="TARGET database URL")
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="empty each table before loading it (required if any has rows)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be loaded and change nothing",
    )
    args = parser.parse_args()

    dump = Path(args.dump)
    manifest = json.loads((dump / "manifest.json").read_text(encoding="utf-8"))
    order = manifest.get("table_order") or list(manifest["tables"])
    tables = [t for t in order if t in manifest["tables"]]
    tables += [t for t in manifest["tables"] if t not in tables]

    dsn = args.dsn.replace("+asyncpg", "", 1)
    conn = await asyncpg.connect(dsn)
    try:
        missing = []
        occupied = []
        for table in tables:
            exists = await conn.fetchval("SELECT to_regclass($1)", f"public.{table}")
            if not exists:
                missing.append(table)
                continue
            if await conn.fetchval(f'SELECT count(*) FROM public."{table}"'):
                occupied.append(table)

        if missing:
            print(
                "These tables do not exist on the target: "
                f"{missing}\nApply db/migrations/005_transfer_baseline.sql first.",
                file=sys.stderr,
            )
            return 2
        if occupied and not args.truncate:
            print(
                f"These tables already have rows: {occupied}\n"
                "Re-run with --truncate to replace them, or point at an empty "
                "database. Loading on top would duplicate every row.",
                file=sys.stderr,
            )
            return 2

        if args.dry_run:
            for table in tables:
                print(f"  would load {manifest['tables'][table]['rows']:>6} -> {table}")
            print(f"  would set sequences: {manifest.get('sequences', {})}")
            return 0

        # One transaction for the whole restore: all of it, or none of it.
        async with conn.transaction():
            if args.truncate:
                # CASCADE because `events` references `sessions`; RESTART
                # IDENTITY so the sequence values set below are the only ones
                # that matter.
                await conn.execute(
                    "TRUNCATE "
                    + ", ".join(f'public."{t}"' for t in tables)
                    + " RESTART IDENTITY CASCADE"
                )

            for table in tables:
                path = dump / manifest["tables"][table]["file"]
                lines = [
                    line for line in path.read_text(encoding="utf-8").splitlines() if line
                ]
                if not lines:
                    print(f"  {table:24} empty")
                    continue

                as_json = await _json_columns(conn, table)
                records = []
                columns: list[str] = []
                for line in lines:
                    row = json.loads(line)
                    if not columns:
                        columns = list(row)
                    records.append(
                        tuple(
                            json.dumps(_decode(row[c]))
                            if c in as_json and row[c] is not None
                            else _decode(row[c])
                            for c in columns
                        )
                    )
                await conn.copy_records_to_table(
                    table, records=records, columns=columns, schema_name="public"
                )
                print(f"  {table:24} {len(records):>6} rows")

            # Sequences last: setval has to come after the rows it must clear.
            for name, last in (manifest.get("sequences") or {}).items():
                if last is None:
                    continue
                await conn.execute(
                    "SELECT setval($1, $2, true)", f"public.{name}", int(last)
                )
                print(f"  sequence {name} -> {last}")
    finally:
        await conn.close()

    print("\nRestore complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
