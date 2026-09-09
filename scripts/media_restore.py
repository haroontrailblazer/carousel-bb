"""Upload a mirror made by :mod:`scripts.media_backup` into a bucket.

    .venv/Scripts/python.exe scripts/media_restore.py backups/media \
        --bucket carousel-media

Reads the target's credentials from the environment, exactly as the app does,
so restoring into a NEW Supabase project is a matter of pointing .env at it
first. Object keys are taken from the directory layout, so what goes up is
addressed the same way it was addressed before - which is what makes the
signed URLs in existing bundles keep working.

Verifies against the manifest rather than trusting the upload: every object is
listed back and its size compared. Silence is not evidence.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402

from app.config import settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mirror", help="directory produced by media_backup.py")
    parser.add_argument("--bucket", default="", help="target bucket")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    mirror = Path(args.mirror)
    manifest = json.loads((mirror / "manifest.json").read_text(encoding="utf-8"))
    bucket = args.bucket or settings.media_bucket
    root = mirror / manifest["bucket"]

    if not root.is_dir():
        print(f"No mirror at {root}", file=sys.stderr)
        return 2

    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        region_name=settings.s3_region or "us-east-1",
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(
            retries={"max_attempts": 5, "mode": "standard"},
            read_timeout=300,
            connect_timeout=30,
        ),
    )

    files = sorted(p for p in root.rglob("*") if p.is_file())
    print(f"{len(files)} files -> {bucket} at {settings.s3_endpoint}")
    if args.dry_run:
        for path in files[:20]:
            print("  would upload", path.relative_to(root).as_posix())
        if len(files) > 20:
            print(f"  ... and {len(files) - 20} more")
        return 0

    uploaded = failed = 0
    for path in files:
        key = path.relative_to(root).as_posix()
        # Content type matters here: a cover clip served as
        # application/octet-stream is not something Instagram will fetch.
        guessed = mimetypes.guess_type(key)[0] or "application/octet-stream"
        try:
            client.upload_file(
                str(path), bucket, key, ExtraArgs={"ContentType": guessed}
            )
            uploaded += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ! {key}: {exc}", file=sys.stderr)

    print(f"\nuploaded {uploaded}, failed {failed}. Verifying...")

    # Verify by listing, not by hoping.
    remote: dict[str, int] = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            remote[obj["Key"]] = int(obj.get("Size", 0))

    missing = []
    wrong = []
    for entry in manifest["objects"]:
        key, size = entry["key"], entry["size"]
        if key not in remote:
            missing.append(key)
        elif remote[key] != size:
            wrong.append((key, size, remote[key]))

    if missing:
        print(f"MISSING on the target ({len(missing)}):", file=sys.stderr)
        for key in missing[:10]:
            print("   ", key, file=sys.stderr)
    if wrong:
        print(f"WRONG SIZE ({len(wrong)}):", file=sys.stderr)
        for key, want, got in wrong[:10]:
            print(f"    {key}: expected {want}, found {got}", file=sys.stderr)
    if not missing and not wrong:
        print(f"All {len(manifest['objects'])} objects present and the right size.")

    return 1 if (failed or missing or wrong) else 0


if __name__ == "__main__":
    raise SystemExit(main())
