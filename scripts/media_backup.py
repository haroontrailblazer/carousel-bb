"""Mirror the media bucket to local disk, keys and all.

    .venv/Scripts/python.exe scripts/media_backup.py [--out backups/media]

Every cover clip, poster, slide and CTA image the pipeline has ever rendered
lives in Supabase Storage, addressed by an object key that encodes the run it
belongs to. This copies them down preserving those keys as directory paths, so
the backup is not just "the files" - it is the bucket, and
:mod:`scripts.media_restore` can put it back exactly where it was.

    <out>/<bucket>/<key...>       the objects
    <out>/manifest.json           key, size, etag, last-modified, content-type

The manifest is what makes this verifiable: a restore can be checked against it
without re-downloading anything, and a second run can tell what changed.

Re-runnable. An object whose local copy already matches the recorded size and
ETag is skipped, so an interrupted download resumes instead of starting over.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402

from app.config import settings  # noqa: E402


def _client():
    """An S3 client pointed at Supabase Storage's S3-compatible endpoint."""
    if not settings.s3_endpoint:
        raise SystemExit(
            "SUPABASE_S3_ENDPOINT is not set - nothing to back up. Check .env."
        )
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        region_name=settings.s3_region or "us-east-1",
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        # Storage is a long way away and a cover clip is tens of megabytes;
        # the default 60s read timeout gives up part-way through one.
        config=Config(
            retries={"max_attempts": 5, "mode": "standard"},
            read_timeout=300,
            connect_timeout=30,
        ),
    )


def _local_matches(path: Path, size: int, etag: str) -> bool:
    """True when the file on disk is already this object.

    Size first because it is free. The ETag is an MD5 for a
    single-part upload and something else entirely for a multipart one, so a
    hash mismatch on a big file is not evidence of corruption - size is the
    check that means the same thing for both.
    """
    if not path.exists() or path.stat().st_size != size:
        return False
    clean = (etag or "").strip('"')
    if "-" in clean:  # multipart: the ETag is not a plain MD5
        return True
    digest = hashlib.md5()  # noqa: S324 - matching S3's ETag, not securing anything
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest() == clean


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(REPO / "backups" / "media"))
    parser.add_argument(
        "--bucket",
        default="",
        help=f"bucket to mirror (default: MEDIA_BUCKET, {settings.media_bucket!r})",
    )
    args = parser.parse_args()

    bucket = args.bucket or settings.media_bucket
    out = Path(args.out) / bucket
    out.mkdir(parents=True, exist_ok=True)

    client = _client()
    started = datetime.now()
    objects: list[dict[str, Any]] = []
    downloaded = skipped = failed = 0
    total_bytes = 0

    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            size = int(obj.get("Size", 0))
            etag = str(obj.get("ETag", "")).strip('"')
            # The key IS the path. Keeping the layout means a restore is a
            # straight upload of every file under <out>/<bucket>/.
            target = out / key
            target.parent.mkdir(parents=True, exist_ok=True)

            record = {
                "key": key,
                "size": size,
                "etag": etag,
                "last_modified": obj["LastModified"].isoformat()
                if obj.get("LastModified")
                else None,
            }

            if _local_matches(target, size, etag):
                skipped += 1
                record["status"] = "already-present"
            else:
                try:
                    client.download_file(bucket, key, str(target))
                    downloaded += 1
                    total_bytes += size
                    record["status"] = "downloaded"
                except Exception as exc:  # noqa: BLE001 - report and keep going
                    failed += 1
                    record["status"] = f"FAILED: {exc}"
                    print(f"  ! {key}: {exc}", file=sys.stderr)

            objects.append(record)
            if len(objects) % 25 == 0:
                print(f"  ...{len(objects)} objects")

    manifest = {
        "taken_at": started.isoformat(),
        "endpoint": settings.s3_endpoint,
        "bucket": bucket,
        "object_count": len(objects),
        "downloaded": downloaded,
        "already_present": skipped,
        "failed": failed,
        "bytes_downloaded": total_bytes,
        "took_seconds": round((datetime.now() - started).total_seconds(), 2),
        "objects": objects,
    }
    (Path(args.out) / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    print(
        f"\n{len(objects)} objects in {bucket}: {downloaded} downloaded, "
        f"{skipped} already present, {failed} failed "
        f"({total_bytes / 1e6:.1f} MB) -> {out}"
    )
    # A partial backup that exits 0 is a backup nobody checks.
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
