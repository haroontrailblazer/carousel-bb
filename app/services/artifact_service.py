"""Supabase Storage artifact service for the Carousel Factory.

``SupabaseArtifactService`` implements the full ``BaseArtifactService`` ABC of
the installed google-adk (2.7.0) on top of Supabase Storage's S3-compatible
API via boto3.

Object key layout mirrors the shipped ``GcsArtifactService`` exactly:

- user-namespaced files (filename starts with ``user:``):
  ``{app_name}/{user_id}/user/{filename}/{version}``
- session-scoped files:
  ``{app_name}/{user_id}/{session_id}/{filename}/{version}``

Versions are monotonically increasing integers starting at 0; every save
creates a new object keyed by the next version (``max(existing) + 1``).

boto3 is synchronous, so every network call runs inside ``asyncio.to_thread``
from the async interface methods. Connection credentials come exclusively from
``app.config.settings`` (``s3_endpoint`` / ``s3_region`` / ``s3_access_key`` /
``s3_secret_key`` / ``media_bucket``) unless explicitly overridden.

Unlike GCS (which stores each ADK marker as its own metadata entry), all
per-object ADK metadata is packed into a single JSON document under the
``adk-meta`` metadata key. S3 lowercases metadata keys on the wire and
requires ASCII values; a single ``ensure_ascii`` JSON document sidesteps both
restrictions while preserving key casing and unicode content.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional, Union

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from google.adk.artifacts import artifact_util
from google.adk.artifacts.base_artifact_service import (
    ArtifactVersion,
    BaseArtifactService,
    ensure_part,
)
from google.adk.errors.input_validation_error import InputValidationError
from google.genai import types
from typing_extensions import override

from app.config import settings

logger = logging.getLogger(__name__)

# Single S3 metadata key under which all ADK bookkeeping is stored as JSON.
_ADK_META_KEY = "adk-meta"

# Field names inside the JSON metadata document.
_META_CUSTOM = "customMetadata"
_META_DISPLAY_NAME = "displayName"
_META_IS_TEXT = "isText"
_META_FILE_URI = "fileUri"
_META_FILE_MIME_TYPE = "fileMimeType"

# Keys used when surfacing metadata through ArtifactVersion.custom_metadata,
# matching the names GcsArtifactService uses for the same fields.
_OUT_DISPLAY_NAME = "adkDisplayName"
_OUT_IS_TEXT = "adkIsText"
_OUT_FILE_URI = "adkFileUri"
_OUT_FILE_MIME_TYPE = "adkFileMimeType"

_DELETE_BATCH_SIZE = 1000  # S3 DeleteObjects hard limit per request.


def _parse_version(object_key: str, prefix: str) -> Optional[int]:
    """Extract an artifact version number from an S3 object key.

    S3 (like GCS) has a flat namespace, so a prefix listing for artifact
    ``a/`` also returns objects of any artifact nested under it (``a/b/3``).
    An object holds a version of the artifact denoted by ``prefix`` only when
    its key is exactly ``{prefix}{version}`` - anything with a further ``/``
    belongs to a different artifact and must be skipped.

    Args:
        object_key: Full object key; must start with ``prefix``.
        prefix: Key prefix of the artifact, including the trailing ``/``.

    Returns:
        The version number, or None if the object is not a version of this
        artifact.
    """
    suffix = object_key[len(prefix):]
    if "/" in suffix:
        return None
    # int() also accepts whitespace, underscores and non-ASCII digits, none of
    # which _object_key() can produce - reject them explicitly.
    if not (suffix.isascii() and suffix.isdigit()):
        logger.warning(
            "Skipping object %s: key does not end with a version number.",
            object_key,
        )
        return None
    return int(suffix)


def _encode_meta(doc: dict[str, Any]) -> dict[str, str]:
    """Serialize the ADK metadata document into S3 object metadata."""
    if not doc:
        return {}
    return {_ADK_META_KEY: json.dumps(doc, ensure_ascii=True)}


def _decode_meta(s3_metadata: Optional[dict[str, str]]) -> dict[str, Any]:
    """Deserialize the ADK metadata document from S3 object metadata."""
    if not s3_metadata:
        return {}
    raw = s3_metadata.get(_ADK_META_KEY)
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Could not decode %s object metadata: %r", _ADK_META_KEY, raw)
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _public_metadata(doc: dict[str, Any]) -> dict[str, Any]:
    """Flatten the internal metadata doc into GCS-style custom_metadata."""
    out: dict[str, Any] = dict(doc.get(_META_CUSTOM) or {})
    if doc.get(_META_DISPLAY_NAME):
        out[_OUT_DISPLAY_NAME] = doc[_META_DISPLAY_NAME]
    if doc.get(_META_IS_TEXT):
        out[_OUT_IS_TEXT] = "true"
    if doc.get(_META_FILE_URI):
        out[_OUT_FILE_URI] = doc[_META_FILE_URI]
    if doc.get(_META_FILE_MIME_TYPE):
        out[_OUT_FILE_MIME_TYPE] = doc[_META_FILE_MIME_TYPE]
    return out


class SupabaseArtifactService(BaseArtifactService):
    """ADK artifact service backed by Supabase Storage's S3-compatible API.

    Implements the exact google-adk 2.7.0 ``BaseArtifactService`` surface
    (``save_artifact`` / ``load_artifact`` / ``list_artifact_keys`` /
    ``delete_artifact`` / ``list_versions`` / ``list_artifact_versions`` /
    ``get_artifact_version``) plus a ``public_url`` helper that returns a
    presigned GET URL (24 h default) for the publisher and review mail.
    """

    def __init__(
        self,
        *,
        bucket_name: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        region_name: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 120.0,
    ) -> None:
        """Initialize the service and its boto3 S3 client.

        All arguments default to values from ``app.config.settings``.

        Args:
            bucket_name: Storage bucket (default ``settings.media_bucket``).
            endpoint_url: S3-compatible endpoint
                (default ``settings.s3_endpoint``, e.g.
                ``https://<project>.storage.supabase.co/storage/v1/s3``).
            region_name: Region (default ``settings.s3_region``).
            access_key: S3 access key id (default ``settings.s3_access_key``).
            secret_key: S3 secret key (default ``settings.s3_secret_key``).
            connect_timeout: TCP connect timeout in seconds.
            read_timeout: Socket read timeout in seconds (uploads of cover
                videos can be several MB, so this is generous).

        Raises:
            ValueError: If the endpoint or credentials are not configured -
                callers (app/agent.py) catch this and fall back to the
                in-memory artifact service for local `adk web` runs.
        """
        self.bucket_name: str = bucket_name or settings.media_bucket
        self._endpoint_url: str = endpoint_url or settings.s3_endpoint
        self._region_name: str = region_name or settings.s3_region
        resolved_access_key = access_key or settings.s3_access_key
        resolved_secret_key = secret_key or settings.s3_secret_key

        missing = [
            name
            for name, value in (
                ("SUPABASE_S3_ENDPOINT", self._endpoint_url),
                ("SUPABASE_S3_ACCESS_KEY", resolved_access_key),
                ("SUPABASE_S3_SECRET_KEY", resolved_secret_key),
                ("MEDIA_BUCKET", self.bucket_name),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "SupabaseArtifactService is not configured; missing env: "
                + ", ".join(missing)
            )

        # Supabase's S3 gateway requires SigV4 and path-style addressing.
        boto_config = BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            retries={"max_attempts": 3, "mode": "standard"},
        )
        # boto3 clients are thread-safe, so sharing one across the
        # asyncio.to_thread worker threads is fine.
        self._client = boto3.client(
            "s3",
            endpoint_url=self._endpoint_url,
            region_name=self._region_name,
            aws_access_key_id=resolved_access_key,
            aws_secret_access_key=resolved_secret_key,
            config=boto_config,
        )

    # ------------------------------------------------------------------
    # Async interface (exact google-adk 2.7.0 ABC surface)
    # ------------------------------------------------------------------

    @override
    async def save_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        artifact: Union[types.Part, dict[str, Any]],
        session_id: Optional[str] = None,
        custom_metadata: Optional[dict[str, Any]] = None,
    ) -> int:
        """Save an artifact; returns the new revision id (0 for the first)."""
        return await asyncio.to_thread(
            self._save_artifact,
            app_name,
            user_id,
            session_id,
            filename,
            artifact,
            custom_metadata,
        )

    @override
    async def load_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
        version: Optional[int] = None,
    ) -> Optional[types.Part]:
        """Load an artifact version (latest when ``version`` is None)."""
        return await asyncio.to_thread(
            self._load_artifact,
            app_name,
            user_id,
            session_id,
            filename,
            version,
        )

    @override
    async def list_artifact_keys(
        self, *, app_name: str, user_id: str, session_id: Optional[str] = None
    ) -> list[str]:
        """List artifact filenames (session-scoped plus user-scoped)."""
        return await asyncio.to_thread(
            self._list_artifact_keys,
            app_name,
            user_id,
            session_id,
        )

    @override
    async def delete_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
    ) -> None:
        """Delete every version of an artifact."""
        return await asyncio.to_thread(
            self._delete_artifact,
            app_name,
            user_id,
            session_id,
            filename,
        )

    @override
    async def list_versions(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
    ) -> list[int]:
        """List all version numbers of an artifact, ascending."""
        return await asyncio.to_thread(
            self._list_versions,
            app_name,
            user_id,
            session_id,
            filename,
        )

    @override
    async def list_artifact_versions(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
    ) -> list[ArtifactVersion]:
        """List all versions of an artifact with their metadata, ascending."""
        return await asyncio.to_thread(
            self._list_artifact_versions_sync,
            app_name,
            user_id,
            session_id,
            filename,
        )

    @override
    async def get_artifact_version(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
        version: Optional[int] = None,
    ) -> Optional[ArtifactVersion]:
        """Get metadata for one artifact version (latest when None)."""
        return await asyncio.to_thread(
            self._get_artifact_version_sync,
            app_name,
            user_id,
            session_id,
            filename,
            version,
        )

    # ------------------------------------------------------------------
    # Public URL helper (used by publisher + review mail)
    # ------------------------------------------------------------------

    def public_url(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
        version: Optional[int] = None,
        expires_in: int = 86_400,
    ) -> str:
        """Return a presigned GET URL for an artifact version.

        Instagram's Graph API and mail clients both need plain HTTPS URLs to
        fetch media, so this signs a time-limited GET for the stored object.

        NOTE: when ``version`` is None this performs a synchronous network
        round-trip (a prefix listing) to find the latest version; from async
        code prefer :meth:`public_url_async`.

        Args:
            app_name: The app name.
            user_id: The user ID.
            filename: The artifact filename (may be ``user:``-namespaced).
            session_id: The session ID; None for user-scoped artifacts.
            version: Version to sign; None signs the latest version.
            expires_in: URL lifetime in seconds (default 24 h).

        Returns:
            A presigned HTTPS GET URL.

        Raises:
            FileNotFoundError: If the artifact has no stored versions.
        """
        if version is None:
            versions = self._list_versions(app_name, user_id, session_id, filename)
            if not versions:
                raise FileNotFoundError(
                    f"No versions found for artifact {filename!r} "
                    f"(app={app_name!r}, user={user_id!r}, session={session_id!r})"
                )
            version = max(versions)
        key = self._object_key(app_name, user_id, filename, version, session_id)
        # Signing itself is pure local computation (no network call).
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket_name, "Key": key},
            ExpiresIn=expires_in,
        )

    async def public_url_async(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
        version: Optional[int] = None,
        expires_in: int = 86_400,
    ) -> str:
        """Async wrapper around :meth:`public_url` (runs in a worker thread)."""
        return await asyncio.to_thread(
            lambda: self.public_url(
                app_name=app_name,
                user_id=user_id,
                filename=filename,
                session_id=session_id,
                version=version,
                expires_in=expires_in,
            )
        )

    # ------------------------------------------------------------------
    # Key construction (mirrors GcsArtifactService exactly)
    # ------------------------------------------------------------------

    @staticmethod
    def _file_has_user_namespace(filename: str) -> bool:
        """True if the filename is user-scoped (starts with ``user:``)."""
        return filename.startswith("user:")

    def _object_prefix(
        self,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
    ) -> str:
        """Construct the object-key prefix for a given artifact (no version)."""
        artifact_util.validate_path_segment(app_name, "app_name")
        artifact_util.validate_path_segment(user_id, "user_id")
        if self._file_has_user_namespace(filename):
            return f"{app_name}/{user_id}/user/{filename}"
        if session_id is None:
            raise InputValidationError(
                "Session ID must be provided for session-scoped artifacts."
            )
        artifact_util.validate_path_segment(session_id, "session_id")
        return f"{app_name}/{user_id}/{session_id}/{filename}"

    def _object_key(
        self,
        app_name: str,
        user_id: str,
        filename: str,
        version: int,
        session_id: Optional[str] = None,
    ) -> str:
        """Construct the full versioned object key for an artifact."""
        prefix = self._object_prefix(app_name, user_id, filename, session_id)
        return f"{prefix}/{version}"

    # ------------------------------------------------------------------
    # S3 primitives
    # ------------------------------------------------------------------

    def _iter_keys(self, prefix: str) -> list[dict[str, Any]]:
        """List all objects under a prefix (paginated).

        Returns:
            A list of dicts with at least ``Key`` and ``LastModified``.
        """
        results: list[dict[str, Any]] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
            results.extend(page.get("Contents") or [])
        return results

    def _head_object(self, key: str) -> Optional[dict[str, Any]]:
        """HEAD an object; returns None when it does not exist."""
        try:
            return self._client.head_object(Bucket=self.bucket_name, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    # ------------------------------------------------------------------
    # Sync implementations (run inside asyncio.to_thread)
    # ------------------------------------------------------------------

    def _save_artifact(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
        artifact: Union[types.Part, dict[str, Any]],
        custom_metadata: Optional[dict[str, Any]] = None,
    ) -> int:
        """Sync body of save_artifact; mirrors GcsArtifactService semantics."""
        artifact = ensure_part(artifact)
        versions = self._list_versions(app_name, user_id, session_id, filename)
        version = 0 if not versions else max(versions) + 1
        key = self._object_key(app_name, user_id, filename, version, session_id)

        meta_doc: dict[str, Any] = {}
        if custom_metadata:
            # GCS coerces every metadata value to str - match that.
            meta_doc[_META_CUSTOM] = {
                k: str(v) for k, v in custom_metadata.items()
            }
        if artifact.inline_data and artifact.inline_data.display_name:
            meta_doc[_META_DISPLAY_NAME] = artifact.inline_data.display_name
        elif artifact.inline_data is None and artifact.text is not None:
            # Flag text artifacts so load reconstructs Part(text=...) instead
            # of Part.from_bytes().
            meta_doc[_META_IS_TEXT] = True

        put_kwargs: dict[str, Any] = {
            "Bucket": self.bucket_name,
            "Key": key,
        }

        if artifact.inline_data:
            data = artifact.inline_data.data
            if data is None:
                raise InputValidationError("Artifact inline_data must contain data.")
            put_kwargs["Body"] = data
            if artifact.inline_data.mime_type:
                put_kwargs["ContentType"] = artifact.inline_data.mime_type
        elif artifact.text is not None:
            put_kwargs["Body"] = artifact.text.encode("utf-8")
            put_kwargs["ContentType"] = "text/plain"
        elif artifact.file_data:
            file_data = artifact.file_data
            file_uri = file_data.file_uri
            if not file_uri:
                raise InputValidationError("Artifact file_data must have a file_uri.")
            if artifact_util.is_artifact_ref(artifact):
                parsed_uri = artifact_util.parse_artifact_uri(file_uri)
                if not parsed_uri:
                    raise InputValidationError(
                        f"Invalid artifact reference URI: {file_uri}"
                    )
                artifact_util.validate_artifact_reference_scope(
                    app_name=app_name,
                    user_id=user_id,
                    session_id=session_id,
                    parsed_uri=parsed_uri,
                )
            # Content lives elsewhere: store the URI reference with an empty
            # body, exactly like the GCS implementation.
            meta_doc[_META_FILE_URI] = file_uri
            if file_data.mime_type:
                meta_doc[_META_FILE_MIME_TYPE] = file_data.mime_type
                put_kwargs["ContentType"] = file_data.mime_type
            put_kwargs["Body"] = b""
        else:
            raise InputValidationError(
                "Artifact must have either inline_data or text."
            )

        metadata = _encode_meta(meta_doc)
        if metadata:
            put_kwargs["Metadata"] = metadata

        self._client.put_object(**put_kwargs)
        return version

    def _load_artifact(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
        version: Optional[int] = None,
    ) -> Optional[types.Part]:
        """Sync body of load_artifact; mirrors GcsArtifactService semantics."""
        if version is None:
            versions = self._list_versions(app_name, user_id, session_id, filename)
            if not versions:
                return None
            version = max(versions)

        key = self._object_key(app_name, user_id, filename, version, session_id)
        try:
            response = self._client.get_object(Bucket=self.bucket_name, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

        meta_doc = _decode_meta(response.get("Metadata"))
        content_type: Optional[str] = response.get("ContentType") or None

        file_uri = meta_doc.get(_META_FILE_URI)
        if file_uri:
            if file_uri.startswith("artifact://"):
                parsed_uri = artifact_util.parse_artifact_uri(file_uri)
                if not parsed_uri:
                    raise InputValidationError(
                        f"Invalid artifact reference URI: {file_uri}"
                    )
                artifact_util.validate_artifact_reference_scope(
                    app_name=app_name,
                    user_id=user_id,
                    session_id=session_id,
                    parsed_uri=parsed_uri,
                )
                return self._load_artifact(
                    app_name=parsed_uri.app_name,
                    user_id=parsed_uri.user_id,
                    session_id=parsed_uri.session_id,
                    filename=parsed_uri.filename,
                    version=parsed_uri.version,
                )
            mime_type = meta_doc.get(_META_FILE_MIME_TYPE) or content_type
            return types.Part(
                file_data=types.FileData(file_uri=file_uri, mime_type=mime_type)
            )

        artifact_bytes: bytes = response["Body"].read()
        if meta_doc.get(_META_IS_TEXT):
            return types.Part(text=artifact_bytes.decode("utf-8"))
        display_name = meta_doc.get(_META_DISPLAY_NAME)
        if display_name:
            return types.Part(
                inline_data=types.Blob(
                    mime_type=content_type,
                    data=artifact_bytes,
                    display_name=display_name,
                )
            )
        return types.Part.from_bytes(data=artifact_bytes, mime_type=content_type)

    def _list_artifact_keys(
        self, app_name: str, user_id: str, session_id: Optional[str]
    ) -> list[str]:
        """Sync body of list_artifact_keys (session + user namespaces)."""
        artifact_util.validate_path_segment(app_name, "app_name")
        artifact_util.validate_path_segment(user_id, "user_id")
        if session_id is not None:
            artifact_util.validate_path_segment(session_id, "session_id")
        filenames: set[str] = set()

        if session_id:
            session_prefix = f"{app_name}/{user_id}/{session_id}/"
            for obj in self._iter_keys(session_prefix):
                # Key is like {session_prefix}{filename}/{version}; the
                # filename itself may contain "/", so drop only the last
                # segment (the version).
                fn_and_version = obj["Key"][len(session_prefix):]
                filename = "/".join(fn_and_version.split("/")[:-1])
                if filename:
                    filenames.add(filename)

        user_namespace_prefix = f"{app_name}/{user_id}/user/"
        for obj in self._iter_keys(user_namespace_prefix):
            fn_and_version = obj["Key"][len(user_namespace_prefix):]
            filename = "/".join(fn_and_version.split("/")[:-1])
            if filename:
                filenames.add(filename)

        return sorted(filenames)

    def _delete_keys(self, keys: list[str]) -> int:
        """Delete objects, batching where the backend actually supports it.

        Supabase's S3-compatible gateway does NOT implement the DeleteObjects
        batch operation - it answers with an empty ClientError, which is as
        unhelpful as it sounds. Real S3 does implement it and batching there is
        worth having, so this attempts the batch and falls back to one call per
        key rather than assuming either behaviour.

        A key that is already gone counts as deleted: the caller only cares
        that nothing is left.

        Returns:
            How many keys were removed.
        """
        if not keys:
            return 0

        for start in range(0, len(keys), _DELETE_BATCH_SIZE):
            batch = keys[start:start + _DELETE_BATCH_SIZE]
            try:
                self._client.delete_objects(
                    Bucket=self.bucket_name,
                    Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
                )
            except Exception:
                for key in batch:
                    try:
                        self._client.delete_object(Bucket=self.bucket_name, Key=key)
                    except Exception:
                        logger.debug("Could not delete %s (already gone?).", key)
        return len(keys)

    def _delete_artifact(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
    ) -> None:
        """Sync body of delete_artifact: removes every stored version."""
        versions = self._list_versions(app_name, user_id, session_id, filename)
        keys = [
            self._object_key(app_name, user_id, filename, v, session_id)
            for v in versions
        ]
        self._delete_keys(keys)


    def delete_session_artifacts(
        self,
        app_name: str,
        user_id: str,
        session_id: str,
    ) -> int:
        """Delete EVERY stored object for a session, in batches.

        Deleting a task has to take its media with it. Without this the rows
        vanish while the cover video and every slide stay in the bucket -
        unreferenced, unreachable through the console, and still billed for.

        Deliberately prefix-based rather than looping over the bundle's
        filenames: a run that failed midway, or one reworked several times,
        leaves objects the final bundle never mentioned. The prefix is the only
        description of "everything this session wrote".

        Returns:
            How many objects were removed.
        """
        prefix = f"{app_name}/{user_id}/{session_id}/"
        keys = [obj["Key"] for obj in self._iter_keys(prefix) if obj.get("Key")]
        return self._delete_keys(keys)

    async def delete_session_artifacts_async(
        self,
        app_name: str,
        user_id: str,
        session_id: str,
    ) -> int:
        """Async wrapper around :meth:`delete_session_artifacts`."""
        return await asyncio.to_thread(
            lambda: self.delete_session_artifacts(app_name, user_id, session_id)
        )

    def latest_versions(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> dict[str, int]:
        """Newest version of every artifact in a session, in ONE listing.

        ``public_url(version=None)`` has to discover the newest version, and it
        does that with its own listing per file. Signing a whole carousel -
        cover video, poster, five slides, CTA - therefore made eight separate
        round trips to object storage, each around two and a half seconds from
        here. Listing the session prefix once and signing from the result turns
        that into one.

        Returns:
            ``{filename: newest_version}``; empty when nothing is stored yet.
        """
        prefix = f"{app_name}/{user_id}/{session_id or 'user'}/"
        newest: dict[str, int] = {}
        for obj in self._iter_keys(prefix):
            key = obj.get("Key", "")
            rest = key[len(prefix):]
            # Keys look like "<filename>/<version>".
            filename, _, version_text = rest.rpartition("/")
            if not filename or not version_text.isdigit():
                continue
            version = int(version_text)
            if version > newest.get(filename, -1):
                newest[filename] = version
        return newest

    async def latest_versions_async(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> dict[str, int]:
        """Async wrapper around :meth:`latest_versions`."""
        return await asyncio.to_thread(
            lambda: self.latest_versions(app_name, user_id, session_id)
        )

    def _list_versions(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
    ) -> list[int]:
        """List all version numbers of an artifact, ascending."""
        prefix = (
            self._object_prefix(app_name, user_id, filename, session_id) + "/"
        )
        versions: list[int] = []
        for obj in self._iter_keys(prefix):
            version = _parse_version(obj["Key"], prefix)
            if version is None:
                continue
            versions.append(version)
        versions.sort()
        return versions

    def _get_artifact_version_sync(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
        version: Optional[int] = None,
    ) -> Optional[ArtifactVersion]:
        """Sync body of get_artifact_version."""
        if version is None:
            versions = self._list_versions(app_name, user_id, session_id, filename)
            if not versions:
                return None
            version = max(versions)

        key = self._object_key(app_name, user_id, filename, version, session_id)
        head = self._head_object(key)
        if head is None:
            return None

        meta_doc = _decode_meta(head.get("Metadata"))
        return ArtifactVersion(
            version=version,
            canonical_uri=f"s3://{self.bucket_name}/{key}",
            create_time=head["LastModified"].timestamp(),
            mime_type=head.get("ContentType") or None,
            custom_metadata=_public_metadata(meta_doc),
        )

    def _list_artifact_versions_sync(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
    ) -> list[ArtifactVersion]:
        """Sync body of list_artifact_versions.

        S3 listings do not carry object metadata or content-type (unlike GCS
        blob listings), so each version costs one extra HEAD request. Version
        counts per artifact are small in this pipeline, so this stays cheap.
        """
        prefix = (
            self._object_prefix(app_name, user_id, filename, session_id) + "/"
        )
        artifact_versions: list[ArtifactVersion] = []
        for obj in self._iter_keys(prefix):
            version = _parse_version(obj["Key"], prefix)
            if version is None:
                continue
            head = self._head_object(obj["Key"])
            meta_doc = _decode_meta(head.get("Metadata")) if head else {}
            mime_type = (head.get("ContentType") or None) if head else None
            artifact_versions.append(
                ArtifactVersion(
                    version=version,
                    canonical_uri=f"s3://{self.bucket_name}/{obj['Key']}",
                    create_time=obj["LastModified"].timestamp(),
                    mime_type=mime_type,
                    custom_metadata=_public_metadata(meta_doc),
                )
            )
        artifact_versions.sort(key=lambda av: av.version)
        return artifact_versions
