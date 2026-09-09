"""Profile pictures, stored in Supabase alongside the carousel media.

Plain object keys (``avatars/<hash>.webp``) rather than the ADK artifact
layout, because an avatar has no session, no version history and no agent that
produced it - it is one small file per person, overwritten in place.

Two deliberate choices worth stating:

* The MEDIA BUCKET IS PRIVATE, so avatars are served back through this app
  (``GET /api/profile/avatar/{key}``) rather than by a presigned URL. A
  presigned URL expires - S3 signatures cap out around a week - and an avatar
  URL is stored in the user's profile and read for months, so it would rot.
  Serving it ourselves also keeps faces behind the login, which a public
  bucket would not.

* The EMAIL IS HASHED into the key. Object keys leak into logs, storage
  browsers and error messages; there is no reason for a listing of the bucket
  to be a list of everyone's email address.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Optional

from botocore.exceptions import ClientError

from app import runtime

logger = logging.getLogger(__name__)

#: Where avatars live inside the media bucket.
PREFIX = "avatars"

#: Refuse anything larger. The browser compresses to well under 100 KB; this
#: is a backstop against a client that does not, not a target.
MAX_BYTES = 2 * 1024 * 1024

#: What the browser is asked to produce, and the only thing accepted.
CONTENT_TYPE = "image/webp"


def key_for(email: str) -> str:
    """Stable, opaque object key for a person's avatar."""
    digest = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()
    return f"{PREFIX}/{digest}.webp"


def _service():
    """The shared artifact service, for its configured S3 client."""
    return runtime.artifact_service()


def _put(key: str, payload: bytes) -> None:
    service = _service()
    service._client.put_object(  # noqa: SLF001 - one client, deliberately shared
        Bucket=service.bucket_name,
        Key=key,
        Body=payload,
        ContentType=CONTENT_TYPE,
        CacheControl="private, max-age=60",
    )


def _get(key: str) -> Optional[bytes]:
    service = _service()
    try:
        response = service._client.get_object(  # noqa: SLF001
            Bucket=service.bucket_name, Key=key
        )
    except ClientError as exc:
        # Supabase's S3 endpoint answers a missing object with HTTP 404 and an
        # EMPTY error code - not the NoSuchKey that AWS sends - so matching on
        # the code alone turned "this person has no picture" into a 502.
        # Measured against the live endpoint: {'Message': '', 'Code': ''}, 404.
        error = exc.response.get("Error") or {}
        status = (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if status == 404 or error.get("Code") in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    return response["Body"].read()


async def save(email: str, payload: bytes) -> str:
    """Store one person's avatar. Returns the object key."""
    if not payload:
        raise ValueError("The uploaded image was empty.")
    if len(payload) > MAX_BYTES:
        raise ValueError(
            f"That image is {len(payload) // 1024} KB; the limit is "
            f"{MAX_BYTES // 1024} KB."
        )
    key = key_for(email)
    await asyncio.to_thread(_put, key, payload)
    logger.info("Stored avatar for %s (%d bytes).", email, len(payload))
    return key


async def load(key: str) -> Optional[bytes]:
    """Read an avatar back, or None when there is none stored."""
    return await asyncio.to_thread(_get, key)


async def delete(email: str) -> None:
    """Remove a stored avatar. Missing is not an error."""
    service = _service()
    key = key_for(email)

    def _remove() -> None:
        try:
            service._client.delete_object(  # noqa: SLF001
                Bucket=service.bucket_name, Key=key
            )
        except ClientError as exc:  # pragma: no cover - best effort
            logger.warning("Could not delete avatar %s: %s", key, exc)

    await asyncio.to_thread(_remove)


__all__ = ["CONTENT_TYPE", "MAX_BYTES", "PREFIX", "delete", "key_for", "load", "save"]
