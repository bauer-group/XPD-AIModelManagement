"""S3-compatible destination for hf-backup.

One boto3 client is built on the main thread and shared by every worker: clients are
documented as thread-safe, Sessions and Resources are not, and the module-level
``boto3.client()`` alias is explicitly warned against in concurrent contexts.

The upload path is hand-rolled rather than delegated to ``upload_fileobj`` /
TransferManager. At an unknown stream size s3transfer keeps its 8 MiB default part size,
which caps an object near 78 GiB; it also buffers in RAM merely to decide whether to go
multipart, and silently doubles the chunk size for large files so the resulting ETag can no
longer be recomputed from the configured value. Owning the part size keeps the ETag
reproducible and the memory ceiling bounded by ``workers * part_size``.

Backend behaviour is discovered by a runtime probe rather than a hardcoded table: presets
supply the defaults, the probe may downgrade them, and an explicit user setting always wins.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import TYPE_CHECKING, Any, Final, Literal

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import SecretStr

from ... import __version__
from ...config.models import PORTABLE_STORAGE_CLASSES, BackendPreset, S3Settings
from ...errors import (
    AimmError,
    AuthError,
    BucketNotFoundError,
    ConfigError,
    DestinationError,
    ObjectNotFoundError,
    ObjectTooLargeError,
    SizeMismatchError,
    UploadFailedError,
)
from ...integrity.hashing import sha256_b64, sha256_bytes
from ...net.retry import call_with_retry, is_retryable
from ...shutdown import raise_if_requested
from . import keys
from .types import BackendCapabilities, ByteReader, ObjectHead, ObjectSummary, UploadResult

if TYPE_CHECKING:  # pragma: no cover - typing only; types-boto3 is a test-extra dependency
    from types_boto3_s3.client import S3Client
    from types_boto3_s3.type_defs import CompletedPartTypeDef, ObjectIdentifierTypeDef

#: The two values botocore accepts; anything else raises InvalidChecksumConfigError.
ChecksumCalculation = Literal["when_supported", "when_required"]

logger = logging.getLogger(__name__)

SHA256_METADATA_KEY: Final[str] = "aimm-sha256"  # S3 lowercases metadata keys
DELETE_BATCH_SIZE: Final[int] = 1000
LIST_PAGE_SIZE: Final[int] = 1000

#: S3 hard limit on part numbers; also the reason ObjectTooLargeError exists.
MAX_PARTS: Final[int] = 10_000
#: Read size for get_stream / get_bytes.
GET_CHUNK_SIZE: Final[int] = 1 << 20
#: Floor for max_pool_connections; botocore's default of 10 blocks threads invisibly.
MIN_POOL_CONNECTIONS: Final[int] = 32
#: S3's hard ceiling for a single PutObject. s3transfer.utils.MAX_SINGLE_UPLOAD_SIZE.
MAX_SINGLE_PUT_SIZE: Final[int] = 5 * 1024**3

_NOT_FOUND_CODES: Final[frozenset[str]] = frozenset({"404", "NoSuchKey", "NotFound"})
_NO_BUCKET_CODES: Final[frozenset[str]] = frozenset({"NoSuchBucket"})
_AUTH_CODES: Final[frozenset[str]] = frozenset(
    {
        "401",
        "403",
        "AccessDenied",
        "AccountProblem",
        "AllAccessDisabled",
        "ExpiredToken",
        "InvalidAccessKeyId",
        "InvalidToken",
        "TokenRefreshRequired",
        "UnauthorizedAccess",
    }
)
#: Codes that leave it UNKNOWN whether CompleteMultipartUpload actually committed.
#: A retry that arrives after the first attempt already committed finds the UploadId
#: consumed and answers NoSuchUpload — for an upload that in fact succeeded.
_AMBIGUOUS_COMPLETE_CODES: Final[frozenset[str]] = frozenset({"NoSuchUpload"})
#: Codes a backend returns when it dislikes botocore's checksum / aws-chunked trailer.
_CHECKSUM_CODES: Final[frozenset[str]] = frozenset(
    {
        "BadDigest",
        "IncompleteBody",
        "InvalidChunkSizeError",
        "InvalidDigest",
        "InvalidRequest",
        "MalformedTrailerError",
        "MissingContentLength",
        "NotImplemented",
        "SignatureDoesNotMatch",
        "XAmzContentSHA256Mismatch",
    }
)


def _error_code(exc: ClientError) -> str:
    """Return the S3 error code of a ClientError, or '' when absent."""
    error = exc.response.get("Error") or {}
    return str(error.get("Code") or "")


def _unquote(etag: str) -> str:
    """Strip the double quotes S3 wraps around every ETag."""
    return etag.strip('"')


def _parts_from_etag(etag: str) -> int | None:
    """Part count for a multipart object, or None when the object was a single PUT.

    A multipart ETag is ``<md5-of-md5s>-<N>`` where ``N`` is the number of parts, so the
    count is already in the HEAD response we have. The obvious alternative — asking S3 for
    ``PartsCount`` — is a trap twice over: S3 only returns that field when the request also
    carries a ``PartNumber``, and adding ``PartNumber`` makes ``ContentLength`` report the
    size of THAT PART instead of the whole object, silently corrupting every size check
    built on :attr:`ObjectHead.size`. Deriving it here costs no extra request.

    Note the suffix is a structural convention, not a checksum: it still holds under
    SSE-KMS/SSE-C where the leading digest is not an MD5 at all.
    """
    head, _, tail = etag.rpartition("-")
    if not head or not tail.isdigit():
        return None
    count = int(tail)
    return count if count > 0 else None


def _ascii(value: str) -> str:
    """Reduce a metadata value to printable ASCII.

    Non-ASCII values come back RFC 2047 encoded and unprintable characters make S3 drop the
    entry entirely and report it via MissingMeta, so both break round-trip comparison.
    """
    return "".join(c for c in value if " " <= c <= "~")


def _read_exactly(reader: ByteReader, n: int) -> bytes:
    """Read exactly n bytes, or fewer at EOF. Short reads are legal, so loop."""
    buf = bytearray()
    while len(buf) < n:
        chunk = reader.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


class S3Destination:
    """One S3 client, shared by all worker threads, plus a hand-rolled multipart upload."""

    def __init__(
        self,
        settings: S3Settings,
        client: S3Client,
        capabilities: BackendCapabilities,
        *,
        attempts: int = 5,
        max_wait: float = 60.0,
    ) -> None:
        self._settings = settings
        self._client = client
        self._capabilities = capabilities
        self._bucket = settings.bucket
        self._attempts = attempts
        self._max_wait = max_wait

    def _retry[T](self, fn: Callable[[], T]) -> T:
        """Run one S3 call under the CONFIGURED retry policy.

        Every call site goes through here. Calling ``call_with_retry`` bare left
        ``transfer.max_attempts`` and ``transfer.max_wait`` documented but dead, so an
        operator setting ``max_attempts: 1`` to fail fast during an incident got the
        hardcoded five attempts anyway.
        """
        return call_with_retry(fn, attempts=self._attempts, max_wait=self._max_wait)

    # ------------------------------------------------------------------ construction

    @classmethod
    def create(
        cls,
        settings: S3Settings,
        *,
        workers: int = 8,
        attempts: int = 5,
        max_wait: float = 60.0,
    ) -> S3Destination:
        """Build the shared client and probe the backend.

        Builds ONE ``boto3.session.Session()`` on the calling (main) thread and ONE client
        from it, sized for `workers` concurrent uploads. No botocore event hooks are
        registered anywhere in this module: doing so voids the thread-safety guarantee.

        Checksum behaviour is set through ``botocore.config.Config`` and never through
        ``os.environ``: botocore resolves these options once at client construction into
        ``client.meta.config``, so a later environment change has no effect, and writing
        ``os.environ`` from worker threads is a data race on top.

        Args:
            workers: Concurrency the connection pool is sized for. Passing fewer than the
                engine actually runs makes every request past the pool size pay a fresh
                TLS handshake for the whole run.
            attempts: Maximum attempts per S3 call, from ``transfer.max_attempts``.
            max_wait: Backoff ceiling in seconds, from ``transfer.max_wait``.

        Raises:
            ConfigError: a non-portable StorageClass was configured for a non-AWS backend.
            AuthError: the credentials were missing or rejected.
            BucketNotFoundError: the bucket does not exist or is not visible.
            DestinationError: any other failure reaching the endpoint.
        """
        cls._validate_storage_class(settings)
        pool_size = max(MIN_POOL_CONNECTIONS, 2 * workers)
        client = _build_client(settings, settings.resolved_checksum_calculation(), pool_size)
        dest = cls(
            settings,
            client,
            _default_capabilities(settings),
            attempts=attempts,
            max_wait=max_wait,
        )
        dest.ensure_bucket()
        dest._require_bucket()
        if settings.probe:
            dest.probe()
        return dest

    @staticmethod
    def _validate_storage_class(settings: S3Settings) -> None:
        """Reject a StorageClass the backend will not accept, before the first upload."""
        storage_class = settings.storage_class
        if storage_class is None or settings.preset is BackendPreset.aws:
            return
        if storage_class not in PORTABLE_STORAGE_CLASSES:
            raise ConfigError(
                f"storage class {storage_class!r} is not portable; preset "
                f"{settings.preset.value!r} accepts only "
                f"{sorted(PORTABLE_STORAGE_CLASSES)}. Set preset 'aws' to use it anyway."
            )

    @property
    def capabilities(self) -> BackendCapabilities:
        """What this endpoint was determined to actually support."""
        return self._capabilities

    @property
    def client(self) -> S3Client:
        """The shared, thread-safe client. Exposed for the catalog and doctor commands."""
        return self._client

    # ------------------------------------------------------------------ probing

    def probe(self) -> BackendCapabilities:
        """Determine what this endpoint actually accepts, using one tiny throwaway object.

        Whether MinIO, Ceph RGW, R2 or Wasabi accept botocore's aws-chunked CRC32 trailer is
        unverified, and the trailer is only emitted over HTTPS — a plain-HTTP development
        endpoint cannot reproduce a production TLS failure. That is why this probes instead
        of trusting a backend table. An explicit ``checksum_calculation`` setting is never
        overridden; only ``auto`` may be downgraded here.

        Raises:
            AuthError, BucketNotFoundError, DestinationError
        """
        key = keys.probe_key(self._settings.prefix)
        payload = b"aimm"
        checksum = sha256_b64(sha256_bytes(payload))
        effective: ChecksumCalculation = self._settings.resolved_checksum_calculation()
        may_downgrade = self._settings.checksum_calculation == "auto"
        supports_sha256 = False
        try:
            try:
                self._put_probe(key, payload, checksum=checksum)
                supports_sha256 = True
            except (ClientError, BotoCoreError) as exc:
                logger.debug("probe: ChecksumSHA256 rejected (%s)", exc.__class__.__name__)
                if may_downgrade and effective == "when_supported" and _is_checksum_error(exc):
                    self._rebuild_client("when_required")
                    effective = "when_required"
                    logger.info("probe: endpoint rejected checksum trailers, using when_required")
                try:
                    self._put_probe(key, payload, checksum=None)
                except (ClientError, BotoCoreError) as retry_exc:
                    raise self._translate(retry_exc, "put_object", key) from retry_exc
            supports_goa = self._probe_get_object_attributes(key)
        finally:
            self._delete_quietly(key)

        self._capabilities = BackendCapabilities(
            request_checksum_calculation=effective,
            addressing_style=self._settings.resolved_addressing_style(),
            supports_sha256_checksum=supports_sha256,
            supports_get_object_attributes=supports_goa,
            probed=True,
        )
        logger.debug("probed backend capabilities: %s", self._capabilities)
        return self._capabilities

    def _put_probe(self, key: str, payload: bytes, *, checksum: str | None) -> None:
        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": payload,
            "ContentLength": len(payload),
        }
        if checksum is not None:
            params["ChecksumSHA256"] = checksum
        self._client.put_object(**params)

    def _probe_get_object_attributes(self, key: str) -> bool:
        """Test whether this endpoint implements GetObjectAttributes.

        Support is unverified across MinIO versions, and head_object + ETag is the
        fallback, so a failure here is informational rather than fatal.
        """
        try:
            self._client.get_object_attributes(
                Bucket=self._bucket, Key=key, ObjectAttributes=["ETag", "ObjectSize"]
            )
        except (ClientError, BotoCoreError) as exc:
            logger.debug("probe: GetObjectAttributes unsupported (%s)", exc.__class__.__name__)
            return False
        return True

    def _rebuild_client(self, checksum_calculation: ChecksumCalculation) -> None:
        """Replace the shared client with one using a different checksum mode.

        Only ever called from probe(), on the main thread, before any worker exists.
        """
        # botocore sets Config attributes dynamically, so this is a getattr by necessity.
        pool_size: int = getattr(
            self._client.meta.config, "max_pool_connections", MIN_POOL_CONNECTIONS
        )
        old = self._client
        self._client = _build_client(self._settings, checksum_calculation, pool_size)
        try:
            old.close()
        except Exception:
            logger.debug("failed to close superseded S3 client", exc_info=True)

    # ------------------------------------------------------------------ reads

    def head(self, key: str) -> ObjectHead | None:
        """head_object with ChecksumMode='ENABLED'; None when the key does not exist.

        Without ChecksumMode='ENABLED' the Checksum* fields are silently absent, which a
        naive verifier reads as 'no checksum stored' and then re-uploads everything.
        """
        try:
            response = self._retry(
                lambda: self._client.head_object(
                    Bucket=self._bucket, Key=key, ChecksumMode="ENABLED"
                )
            )
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                return None
            raise self._translate(exc, "head_object", key) from exc
        except BotoCoreError as exc:
            raise self._translate(exc, "head_object", key) from exc
        metadata = {k.lower(): v for k, v in (response.get("Metadata") or {}).items()}
        etag = _unquote(str(response.get("ETag", "")))
        return ObjectHead(
            key=key,
            size=int(response["ContentLength"]),
            etag=etag,
            metadata=metadata,
            storage_class=response.get("StorageClass"),
            last_modified=response.get("LastModified"),
            # Prefer the server's own answer when it volunteers one, but this request
            # deliberately carries no PartNumber (see _parts_from_etag), so in practice
            # PartsCount is absent and the ETag suffix is what makes the field truthful.
            parts_count=response.get("PartsCount") or _parts_from_etag(etag),
        )

    def exists(self, key: str) -> bool:
        """True when the key resolves to an object."""
        return self.head(key) is not None

    @contextmanager
    def get_stream(self, key: str) -> Iterator[Iterator[bytes]]:
        """Yield an iterator of byte chunks for one object, without buffering it whole."""
        try:
            response = self._retry(lambda: self._client.get_object(Bucket=self._bucket, Key=key))
        except ClientError as exc:
            raise self._translate(exc, "get_object", key) from exc
        except BotoCoreError as exc:
            raise self._translate(exc, "get_object", key) from exc
        body = response["Body"]
        try:
            yield body.iter_chunks(GET_CHUNK_SIZE)
        finally:
            body.close()

    def get_bytes(self, key: str) -> bytes:
        """Read a whole object into memory. Only for manifests and other small objects."""
        with self.get_stream(key) as chunks:
            return b"".join(chunks)

    def list_keys(self, prefix: str) -> Iterator[ObjectSummary]:
        """Paginate ListObjectsV2 over a prefix.

        1000 is the server ceiling for MaxKeys; a larger PageSize buys nothing.
        """
        paginator = self._client.get_paginator("list_objects_v2")
        pages = paginator.paginate(
            Bucket=self._bucket,
            Prefix=prefix,
            PaginationConfig={"PageSize": LIST_PAGE_SIZE},
        )
        try:
            for page in pages:
                for obj in page.get("Contents", []):
                    yield ObjectSummary(
                        key=obj["Key"],
                        size=int(obj["Size"]),
                        etag=_unquote(str(obj.get("ETag", ""))),
                        last_modified=obj["LastModified"],
                    )
        except ClientError as exc:
            raise self._translate(exc, "list_objects_v2", prefix) from exc
        except BotoCoreError as exc:
            raise self._translate(exc, "list_objects_v2", prefix) from exc

    def list_prefixes(self, prefix: str) -> list[str]:
        """List immediate child prefixes via Delimiter='/' (CommonPrefixes)."""
        paginator = self._client.get_paginator("list_objects_v2")
        pages = paginator.paginate(
            Bucket=self._bucket,
            Prefix=prefix,
            Delimiter="/",
            PaginationConfig={"PageSize": LIST_PAGE_SIZE},
        )
        found: list[str] = []
        try:
            for page in pages:
                found.extend(cp["Prefix"] for cp in page.get("CommonPrefixes", []))
        except ClientError as exc:
            raise self._translate(exc, "list_objects_v2", prefix) from exc
        except BotoCoreError as exc:
            raise self._translate(exc, "list_objects_v2", prefix) from exc
        return found

    # ------------------------------------------------------------------ writes

    def put_small(
        self,
        key: str,
        data: bytes,
        *,
        sha256: str,
        metadata: Mapping[str, str] | None = None,
    ) -> UploadResult:
        """Single PutObject for a small object, followed by a size verification.

        ChecksumSHA256 is sent only when the probe found the backend accepts it; where it
        does, S3 itself rejects a corrupted body before it is ever stored.

        Raises:
            ObjectTooLargeError, UploadFailedError, SizeMismatchError, DestinationError
        """
        if len(data) > MAX_SINGLE_PUT_SIZE:
            # Guarded here as well as in the settings, so the library API is safe on its
            # own: S3 answers EntityTooLarge, and the caller has by then already paid for
            # the whole download.
            raise ObjectTooLargeError(
                f"{key}: {len(data)} bytes exceeds the {MAX_SINGLE_PUT_SIZE} byte limit "
                "for a single PutObject; use upload_multipart instead"
            )
        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": data,
            "ContentLength": len(data),
            "Metadata": self._metadata(metadata, sha256=sha256),
        }
        params.update(self._storage_params())
        if self._capabilities.supports_sha256_checksum:
            params["ChecksumSHA256"] = sha256_b64(sha256)
        try:
            self._retry(lambda: self._client.put_object(**params))
        except (ClientError, BotoCoreError) as exc:
            raise UploadFailedError(f"put_object failed for {key}: {exc}") from exc
        head = self._verify_size(key, len(data))
        return UploadResult(key=key, size=len(data), etag=head.etag, part_size=None, parts=1)

    def upload_multipart(
        self,
        key: str,
        reader: ByteReader,
        *,
        size: int,
        part_size: int,
        metadata: Mapping[str, str] | None = None,
    ) -> UploadResult:
        """Hand-rolled multipart upload from any ByteReader. Always uses MPU.

        Every part except the last is exactly `part_size` bytes; part numbers start at 1 and
        are strictly consecutive, because non-consecutive numbers make S3 answer HTTP 500
        rather than a 4xx. The upload is aborted on every exception path, including
        KeyboardInterrupt, so no orphaned upload is left occupying storage invisibly.

        Deliberately takes no sha256: for a streamed body the digest is only known once the
        stream has been consumed, and object metadata is immutable after upload. The
        manifest is the authority for multipart objects.

        Raises:
            UploadFailedError, SizeMismatchError, ObjectTooLargeError, DestinationError
        """
        expected_parts = max(1, ceil(size / part_size))
        if expected_parts > MAX_PARTS:
            raise ObjectTooLargeError(
                f"{key} needs {expected_parts} parts of {part_size} bytes, "
                f"which exceeds the S3 limit of {MAX_PARTS}"
            )

        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Metadata": self._metadata(metadata),
        }
        params.update(self._storage_params())
        try:
            created = self._retry(lambda: self._client.create_multipart_upload(**params))
        except (ClientError, BotoCoreError) as exc:
            raise UploadFailedError(f"create_multipart_upload failed for {key}: {exc}") from exc
        upload_id = str(created["UploadId"])

        parts: list[CompletedPartTypeDef] = []
        total = 0
        try:
            while len(parts) < expected_parts:
                # A part boundary is the only safe place to stop: the abort below then
                # unwinds a whole upload instead of a half-written body. Uploads run on
                # worker threads, which a signal never reaches, so this poll is the only
                # way a SIGTERM can end them — see bg_ai_model_management.shutdown.
                raise_if_requested(f"upload of {key}")
                chunk = _read_exactly(reader, part_size)
                if not chunk and parts:
                    break
                number = len(parts) + 1
                response = self._retry(
                    lambda c=chunk, n=number: self._client.upload_part(  # type: ignore[misc]
                        Bucket=self._bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=n,
                        Body=c,
                        ContentLength=len(c),
                    )
                )
                parts.append({"ETag": response["ETag"], "PartNumber": number})
                total += len(chunk)
            if total != size:
                raise SizeMismatchError(
                    f"{key}: read {total} bytes from the source but expected {size}"
                )
            self._complete_multipart(key, upload_id, parts, size=size)
        except BaseException as exc:
            self._abort_quietly(key, upload_id)
            if isinstance(exc, (ClientError, BotoCoreError)):
                raise UploadFailedError(f"multipart upload of {key} failed: {exc}") from exc
            raise

        head = self._verify_size(key, size)
        logger.debug("uploaded %s in %d parts of %d bytes", key, len(parts), part_size)
        return UploadResult(
            key=key, size=size, etag=head.etag, part_size=part_size, parts=len(parts)
        )

    def _complete_multipart(
        self, key: str, upload_id: str, parts: list[CompletedPartTypeDef], *, size: int
    ) -> None:
        """Commit the upload, settling an ambiguous failure with a head_object.

        CompleteMultipartUpload is NOT idempotent, and at this tool's scale that matters.
        A 400 GiB shard commits in ~6,400 parts and routinely takes longer than
        ``read_timeout``, so the request times out while the server is still committing.
        Both this module's retry layer and botocore's own standard-mode retry then
        re-issue it, the server has already committed and consumed the UploadId, and the
        retry answers NoSuchUpload. Reporting that as a failure discards a correct
        multi-hour transfer AND the manifest for the whole revision, forcing a complete
        re-download from Hugging Face.

        The key is pinned to a commit SHA, so an object of the expected length at that
        key is this file: a head_object settles what the protocol leaves ambiguous. A
        permanent failure (a rejected checksum, say) is not ambiguous and still aborts.
        """
        try:
            self._retry(
                lambda: self._client.complete_multipart_upload(
                    Bucket=self._bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
            )
        except (ClientError, BotoCoreError) as exc:
            if not _is_ambiguous_complete(exc) or not self._stored_with_size(key, size):
                raise
            logger.warning(
                "complete_multipart_upload for %s reported %s, but the object is stored "
                "at its full %d bytes; treating the upload as committed",
                key,
                type(exc).__name__,
                size,
            )

    def _stored_with_size(self, key: str, size: int) -> bool:
        """True when `key` already holds an object of exactly `size` bytes."""
        try:
            head = self.head(key)
        except AimmError:
            return False
        return head is not None and head.size == size

    def _verify_size(self, key: str, size: int) -> ObjectHead:
        """head_object and assert ContentLength.

        A truncated upload must fail here, at backup time, and not silently wait to be
        discovered at restore time years later.
        """
        head = self.head(key)
        if head is None:
            raise SizeMismatchError(f"{key} is absent immediately after a successful upload")
        if head.size != size:
            raise SizeMismatchError(f"{key}: stored {head.size} bytes but expected {size}")
        return head

    # ------------------------------------------------------------------ deletes

    def delete_keys(self, keys_to_delete: Iterable[str]) -> int:
        """Delete in batches of DELETE_BATCH_SIZE. Returns the number actually deleted.

        Raises:
            DestinationError: the backend reported per-key failures.
        """
        deleted = 0
        batch: list[ObjectIdentifierTypeDef] = []
        for key in keys_to_delete:
            batch.append({"Key": key})
            if len(batch) == DELETE_BATCH_SIZE:
                deleted += self._delete_batch(batch)
                batch = []
        if batch:
            deleted += self._delete_batch(batch)
        return deleted

    def _delete_batch(self, batch: list[ObjectIdentifierTypeDef]) -> int:
        try:
            response = self._retry(
                lambda: self._client.delete_objects(
                    Bucket=self._bucket, Delete={"Objects": batch, "Quiet": False}
                )
            )
        except (ClientError, BotoCoreError) as exc:
            raise self._translate(exc, "delete_objects", batch[0]["Key"]) from exc
        failures = response.get("Errors") or []
        if failures:
            detail = ", ".join(f"{e.get('Key')}: {e.get('Code')}" for e in failures[:10])
            raise DestinationError(f"{len(failures)} object(s) could not be deleted: {detail}")
        return len(response.get("Deleted") or [])

    def abort_stale_uploads(self, prefix: str, older_than: timedelta, *, now: datetime) -> int:
        """Abort multipart uploads under `prefix` initiated before ``now - older_than``.

        Orphaned uploads occupy storage on MinIO indefinitely and never show up in
        ListObjectsV2, so nothing else in this tool can see them. `now` is injected to keep
        this unit-testable.

        The server-side ``Prefix`` filter is deliberately NOT used. MinIO — the primary
        production backend — honours ``Prefix`` on ListMultipartUploads only when it equals
        a complete object key: verified against a live rig holding two in-flight uploads
        under ``it/``, an unfiltered call returned both while ``it``, ``it/``, ``it/dbg``
        and ``it/dbg/`` each returned zero, with and without ``Delimiter``. Passing the
        prefix to the server therefore made this method a silent no-op for every directory
        prefix, so orphans accumulated forever — exactly what it exists to prevent. We
        paginate unfiltered and filter on the key in Python instead.
        """
        cutoff = (now if now.tzinfo else now.replace(tzinfo=UTC)) - older_than
        paginator = self._client.get_paginator("list_multipart_uploads")
        aborted = 0
        try:
            for page in paginator.paginate(Bucket=self._bucket):
                for upload in page.get("Uploads", []):
                    if not upload["Key"].startswith(prefix):
                        continue
                    if upload["Initiated"] > cutoff:
                        continue
                    self._client.abort_multipart_upload(
                        Bucket=self._bucket, Key=upload["Key"], UploadId=upload["UploadId"]
                    )
                    aborted += 1
                    logger.info("aborted stale multipart upload for %s", upload["Key"])
        except ClientError as exc:
            raise self._translate(exc, "list_multipart_uploads", prefix) from exc
        except BotoCoreError as exc:
            raise self._translate(exc, "list_multipart_uploads", prefix) from exc
        return aborted

    def _abort_quietly(self, key: str, upload_id: str) -> None:
        """Abort an upload without masking the exception that triggered the abort."""
        try:
            self._client.abort_multipart_upload(Bucket=self._bucket, Key=key, UploadId=upload_id)
        except Exception:
            logger.warning("failed to abort multipart upload %s for %s", upload_id, key)

    def _delete_quietly(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception:
            logger.debug("failed to delete probe object %s", key, exc_info=True)

    # ------------------------------------------------------------------ bucket

    def ensure_bucket(self) -> None:
        """Create the bucket, but only when ``settings.ensure_bucket`` is True.

        Default is OFF: the reference target is a 120 TB shared production store whose
        buckets and policies are provisioned as IaC, so silently creating one would produce
        an unmanaged bucket with default policies.
        """
        if not self._settings.ensure_bucket:
            return
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return
        except ClientError as exc:
            if _error_code(exc) not in _NOT_FOUND_CODES | _NO_BUCKET_CODES:
                raise self._translate(exc, "head_bucket", self._bucket) from exc
        params: dict[str, Any] = {"Bucket": self._bucket}
        if self._settings.region and self._settings.region != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": self._settings.region}
        try:
            self._client.create_bucket(**params)
        except (ClientError, BotoCoreError) as exc:
            raise self._translate(exc, "create_bucket", self._bucket) from exc
        logger.info("created bucket %s", self._bucket)

    def _require_bucket(self) -> None:
        """Fail fast when the bucket is missing, rather than at the first upload."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            code = _error_code(exc)
            if code in _NOT_FOUND_CODES | _NO_BUCKET_CODES:
                raise BucketNotFoundError(
                    f"bucket {self._bucket!r} does not exist or is not visible"
                ) from exc
            raise self._translate(exc, "head_bucket", self._bucket) from exc
        except BotoCoreError as exc:
            raise self._translate(exc, "head_bucket", self._bucket) from exc

    def close(self) -> None:
        """Release the shared client's connection pool."""
        try:
            self._client.close()
        except Exception:
            logger.debug("failed to close the S3 client", exc_info=True)

    # ------------------------------------------------------------------ helpers

    def _metadata(
        self, metadata: Mapping[str, str] | None, *, sha256: str | None = None
    ) -> dict[str, str]:
        """Build the user-metadata dict.

        Keys are lowercased because S3 lowercases them anyway and every round-trip
        comparison would otherwise fail; values are reduced to printable ASCII.
        """
        result = {k.lower(): _ascii(v) for k, v in (metadata or {}).items()}
        if sha256 is not None:
            result[SHA256_METADATA_KEY] = sha256
        return result

    def _storage_params(self) -> dict[str, Any]:
        """StorageClass / SSE parameters, omitted entirely when not configured."""
        params: dict[str, Any] = {}
        if self._settings.storage_class:
            params["StorageClass"] = self._settings.storage_class
        if self._settings.server_side_encryption:
            params["ServerSideEncryption"] = self._settings.server_side_encryption
            if self._settings.sse_kms_key_id:
                params["SSEKMSKeyId"] = self._settings.sse_kms_key_id
        return params

    def _translate(self, exc: BaseException, operation: str, target: str) -> AimmError:
        """Map a botocore exception onto the typed error hierarchy."""
        if isinstance(exc, ClientError):
            code = _error_code(exc)
            if code in _AUTH_CODES:
                return AuthError(f"{operation} on {target!r} was refused: {code}")
            if code in _NO_BUCKET_CODES:
                return BucketNotFoundError(f"bucket {self._bucket!r} does not exist")
            if code in _NOT_FOUND_CODES:
                return ObjectNotFoundError(f"{target!r} does not exist")
            return DestinationError(f"{operation} on {target!r} failed: {code or exc}")
        return DestinationError(f"{operation} on {target!r} failed: {exc}")


def _is_checksum_error(exc: BaseException) -> bool:
    """True when a failure plausibly stems from checksum / aws-chunked trailer handling."""
    return isinstance(exc, ClientError) and _error_code(exc) in _CHECKSUM_CODES


def _is_ambiguous_complete(exc: BaseException) -> bool:
    """True when a CompleteMultipartUpload failure leaves the outcome UNKNOWN.

    Either the transport failed (the server may have committed before the response was
    lost), or a retry found the UploadId already consumed by a successful first attempt.
    """
    return is_retryable(exc) or (
        isinstance(exc, ClientError) and _error_code(exc) in _AMBIGUOUS_COMPLETE_CODES
    )


def _default_capabilities(settings: S3Settings) -> BackendCapabilities:
    """Preset-derived defaults, used until (or instead of) a probe.

    Both optional features default to unsupported: skipping them costs only a redundant
    server-side guard, whereas assuming them breaks the upload outright.
    """
    return BackendCapabilities(
        request_checksum_calculation=settings.resolved_checksum_calculation(),
        addressing_style=settings.resolved_addressing_style(),
        supports_sha256_checksum=False,
        supports_get_object_attributes=False,
        probed=False,
    )


def _build_client(
    settings: S3Settings, checksum_calculation: ChecksumCalculation, pool_size: int
) -> S3Client:
    """Build the single shared S3 client.

    Uses an explicit Session rather than the ``boto3.client()`` module-level alias, which
    the docs warn "may result in response ordering issues or interpreter failures from
    underlying SSL modules" when invoked in a concurrent context.
    """
    config = Config(
        signature_version="s3v4",
        s3={"addressing_style": settings.resolved_addressing_style()},
        request_checksum_calculation=checksum_calculation,
        response_checksum_validation=checksum_calculation,
        retries={"mode": "standard", "max_attempts": settings.max_attempts},
        max_pool_connections=pool_size,
        connect_timeout=settings.connect_timeout,
        read_timeout=settings.read_timeout,
        user_agent_extra=f"aimm/{__version__}",
    )
    verify: bool | str = settings.verify_tls
    if settings.verify_tls and settings.ca_bundle is not None:
        verify = str(settings.ca_bundle)
    if not settings.verify_tls:
        # An operator who set this for a staging box must not be able to carry the same
        # profile to production unnoticed: SigV4 Authorization headers and every model
        # byte then travel over a channel any on-path party can transparently proxy.
        logger.warning(
            "TLS certificate verification is DISABLED for %s; credentials and object "
            "bytes are exposed to any on-path party",
            settings.endpoint_url or settings.region,
        )
    session = boto3.session.Session()
    return session.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        region_name=settings.region,
        aws_access_key_id=_secret(settings.access_key_id),
        aws_secret_access_key=_secret(settings.secret_access_key),
        aws_session_token=_secret(settings.session_token),
        verify=verify,
        config=config,
    )


def _secret(value: SecretStr | None) -> str | None:
    """Unwrap a SecretStr, or None so boto3 falls back to its credential chain.

    The plaintext is handed straight to boto3 and never logged.
    """
    return None if value is None else value.get_secret_value()
