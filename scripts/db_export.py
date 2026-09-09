"""Dump every table in ``public`` to newline-delimited JSON.

    .venv/Scripts/python.exe scripts/db_export.py [--out backups/db-YYYYMMDD]

Written rather than shelling out to ``pg_dump`` because pg_dump is not
installed here and its output is only restorable by ``pg_restore`` of a
compatible version. JSONL is restorable by :mod:`scripts.db_import`, readable
by eye, and diffable - which matters when the point of the exercise is
confirming that nothing changed.

WHAT IT WRITES

    <out>/manifest.json     table list, row counts, sequence values, timing
    <out>/<table>.jsonl     one JSON object per row, in primary-key order

Every value is stored in a form that survives the round trip: timestamps as
ISO-8601 strings, jsonb as real JSON (not a string), bytes as base64. The
import side reverses each of those using the column types it reads from the
TARGET database, so a column that is jsonb here is loaded as jsonb there.

THIS FILE CONTAINS SECRETS. ``app_config`` holds the encrypted Telegram
credentials and ``sessions`` holds whatever the pipeline put in session state.
The output directory is gitignored; keep it that way.

Read-only: the only statements issued are SELECTs.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402

#: Load order for the import side. Parents first: `events` has a foreign key
#: into `sessions`, so restoring events first fails every row. Everything else
#: is independent, but a fixed order keeps two dumps of the same database
#: byte-comparable.
TABLE_ORDER = [
    "sessions",
    "events",
    "app_states",
    "user_states",
    "adk_internal_metadata",
    "app_users",
    "app_config",
    "news_queue",
    "runs",
    "run_events",
    "feedback",
    "pending_reviews",
    "memory_entries",
]


def _encode(value: Any) -> Any:
    """Render one column value as something JSON can hold, losslessly."""
    if isinstance(value, (datetime, date)):
        return {"__type__": "datetime", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__type__": "decimal", "value": str(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "__type__": "bytes",
            "value": base64.b64encode(bytes(value)).decode("ascii"),
        }
    return value


async def _order_by(conn: asyncpg.Connection, table: str) -> str:
    """Primary-key ordering, so two dumps of one database match line for line."""
    cols = await conn.fetch(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = $1::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """,
        f"public.{table}",
    )
    if not cols:
        return ""
    return " ORDER BY " + ", ".join(f'"{c["attname"]}"' for c in cols)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(REPO / "backups" / f"db-{datetime.now():%Y%m%d-%H%M%S}"),
        help="directory to write into (created if missing)",
    )
    parser.add_argument(
        "--dsn",
        default="",
        help="source database; defaults to DATABASE_URL from the environment",
    )
    args = parser.parse_args()

    dsn = (args.dsn or settings.database_url or "").replace("+asyncpg", "", 1)
    if not dsn:
        print("No DATABASE_URL set and no --dsn given.", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # ONE connection, not a pool. The Supabase pooler on port 5432 runs in
    # session mode and caps this project at 15 clients; a export that opens
    # three of them while the service is running can tip it over, and a
    # refused connection there is how a run got stranded once already.
    conn = await asyncpg.connect(dsn)
    started = datetime.now()
    manifest: dict[str, Any] = {
        "taken_at": started.isoformat(),
        "source_host": dsn.split("@")[-1].split("/")[0],
        "tables": {},
        "sequences": {},
        "table_order": TABLE_ORDER,
    }

    try:
        live = {
            r["tablename"]
            for r in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
        # Anything on the server that this script does not know about still
        # gets dumped - silently skipping a table is how a transfer loses data
        # nobody notices for a month.
        tables = [t for t in TABLE_ORDER if t in live] + sorted(live - set(TABLE_ORDER))
        for extra in sorted(live - set(TABLE_ORDER)):
            print(f"  ! {extra}: not in TABLE_ORDER - dumped, but check its "
                  f"restore order by hand")

        for table in tables:
            rows = await conn.fetch(
                f'SELECT * FROM public."{table}"{await _order_by(conn, table)}'
            )
            path = out / f"{table}.jsonl"
            with path.open("w", encoding="utf-8", newline="\n") as handle:
                for row in rows:
                    handle.write(
                        json.dumps(
                            {k: _encode(v) for k, v in dict(row).items()},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            manifest["tables"][table] = {
                "rows": len(rows),
                "bytes": path.stat().st_size,
                "file": path.name,
            }
            print(f"  {table:24} {len(rows):>6} rows  {path.stat().st_size:>10,} bytes")

        # Sequences carry the next id. Restore the rows without them and the
        # first insert on the new database collides with row 1.
        for seq in await conn.fetch(
            "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname='public'"
        ):
            manifest["sequences"][seq["sequencename"]] = seq["last_value"]

        manifest["took_seconds"] = round(
            (datetime.now() - started).total_seconds(), 2
        )
        (out / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
    finally:
        await conn.close()

    total = sum(t["rows"] for t in manifest["tables"].values())
    print(f"\n{total} rows from {len(manifest['tables'])} tables -> {out}")
    print(f"sequences: {manifest['sequences']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
