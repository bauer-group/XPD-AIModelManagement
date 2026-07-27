"""Tests for the S3 destination against moto, plus the fault paths moto cannot produce.

moto gives a faithful, *working* S3. It cannot give a broken one, and three of this
module's rules only exist for the broken case: abort the multipart upload on every
exception path, verify `ContentLength` after every upload, and translate error codes into
typed exceptions. Those are driven through `SpyClient`, which delegates to moto and
injects the failure.

Where moto genuinely cannot reach — `PartsCount` and `GetObjectAttributes.ObjectParts`,
both of which it simply does not return — the assertion is written as an
`@pytest.mark.integration` test against the MinIO rig rather than faked here. Each such
test carries a comment saying exactly what moto lacks.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from bg_ai_model_management import shutdown
from bg_ai_model_management.config.models import S3Settings, Settings
from bg_ai_model_management.errors import (
    AuthError,
    BucketNotFoundError,
    ConfigError,
    DestinationError,
    ObjectNotFoundError,
    ObjectTooLargeError,
    OperationCancelledError,
    SizeMismatchError,
    UploadFailedError,
)
from bg_ai_model_management.integrity.hashing import sha256_bytes
from bg_ai_model_management.tools.hfbackup.destination import (
    DELETE_BATCH_SIZE,
    MAX_PARTS,
    MAX_SINGLE_PUT_SIZE,
    SHA256_METADATA_KEY,
    S3Destination,
    _default_capabilities,
    _parts_from_etag,
)

from .conftest import ChunkReader, SpyClient

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

MIB = 1024**2
PART = 5 * MIB  # the S3 minimum, which keeps these tests small but legal


def client_error(code: str, operation: str, *, status: int = 400) -> ClientError:
    """A non-retryable ClientError, so the retry layer fails fast instead of sleeping."""
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )


def raises(exc: BaseException, *, on_call: int = 0) -> Any:
    """A `SpyClient` fault that raises `exc` on one specific call index."""

    def fault(index: int, _kwargs: dict[str, Any]) -> None:
        if index == on_call:
            raise exc
        return None

    return fault


# ── multipart: dense consecutive part numbering ──────────────────────────────


def test_multipart_uses_dense_consecutive_part_numbers(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    """Non-consecutive part numbers make S3 answer HTTP 500, not a helpful 4xx."""
    destination, spy = spy_destination
    payload = b"x" * (PART * 3 + 17)

    result = destination.upload_multipart(
        "aimm/big.bin", ChunkReader(payload, max_chunk=64 * 1024), size=len(payload), part_size=PART
    )

    numbers = [call["PartNumber"] for call in spy.params("upload_part")]
    assert numbers == [1, 2, 3, 4], "part numbers must start at 1 and be strictly consecutive"
    assert result.parts == 4
    assert result.part_size == PART
    assert result.size == len(payload)


def test_every_part_but_the_last_is_exactly_part_size(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    """Equal-sized parts are what make the composite ETag reproducible."""
    destination, spy = spy_destination
    payload = b"y" * (PART * 2 + 100)

    destination.upload_multipart(
        "aimm/equal.bin", ChunkReader(payload, max_chunk=13), size=len(payload), part_size=PART
    )

    lengths = [call["ContentLength"] for call in spy.params("upload_part")]
    assert lengths == [PART, PART, 100]
    assert sum(lengths) == len(payload)


def test_a_short_reader_still_produces_dense_numbering(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    """`read()` may legally return fewer bytes than asked; the loop must absorb that."""
    destination, spy = spy_destination
    payload = b"z" * (PART + 1)

    destination.upload_multipart(
        "aimm/short.bin", ChunkReader(payload, max_chunk=7), size=len(payload), part_size=PART
    )

    assert [call["PartNumber"] for call in spy.params("upload_part")] == [1, 2]
    assert [call["ContentLength"] for call in spy.params("upload_part")] == [PART, 1]


def test_an_exactly_one_part_object_still_uses_multipart(
    destination: S3Destination, s3_client: S3Client, s3_bucket: str
) -> None:
    payload = b"a" * PART
    result = destination.upload_multipart(
        "aimm/one.bin", ChunkReader(payload), size=len(payload), part_size=PART
    )
    assert result.parts == 1
    assert s3_client.get_object(Bucket=s3_bucket, Key="aimm/one.bin")["Body"].read() == payload


def test_multipart_refuses_an_object_needing_more_than_max_parts(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    destination, spy = spy_destination
    with pytest.raises(ObjectTooLargeError):
        destination.upload_multipart(
            "aimm/huge.bin", ChunkReader(b""), size=PART * (MAX_PARTS + 1), part_size=PART
        )
    assert not spy.called("create_multipart_upload"), "no upload may be opened at all"


# ── multipart: abort on EVERY exception path ─────────────────────────────────


def assert_aborted(spy: SpyClient, key: str) -> None:
    aborts = spy.params("abort_multipart_upload")
    assert aborts, "the multipart upload was NOT aborted; it will occupy storage invisibly"
    assert aborts[-1]["Key"] == key
    created = spy.params("create_multipart_upload")
    assert aborts[-1]["UploadId"], "the abort must carry the real upload id"
    assert created, "an upload must have been opened for the abort to be meaningful"


def test_abort_on_upload_part_failure(spy_destination: tuple[S3Destination, SpyClient]) -> None:
    destination, spy = spy_destination
    spy.faults["upload_part"] = raises(client_error("InvalidRequest", "UploadPart"), on_call=1)
    payload = b"x" * (PART * 2)

    with pytest.raises(UploadFailedError):
        destination.upload_multipart(
            "aimm/f.bin", ChunkReader(payload), size=len(payload), part_size=PART
        )
    assert_aborted(spy, "aimm/f.bin")


def test_abort_on_complete_multipart_upload_failure(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    destination, spy = spy_destination
    spy.faults["complete_multipart_upload"] = raises(
        client_error("InvalidRequest", "CompleteMultipartUpload")
    )
    payload = b"x" * PART

    with pytest.raises(UploadFailedError):
        destination.upload_multipart(
            "aimm/f.bin", ChunkReader(payload), size=len(payload), part_size=PART
        )
    assert_aborted(spy, "aimm/f.bin")


def test_abort_when_shutdown_is_requested_mid_upload(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    """A SIGTERM during a multi-hour upload must not orphan the multipart upload.

    Orphaned parts keep occupying storage, are billed, and do not appear in an
    ordinary bucket listing — the failure mode this poll exists to prevent.
    """
    destination, spy = spy_destination
    payload = b"z" * (PART * 3)

    def request_shutdown_after_the_first_part(index: int, _kwargs: dict[str, Any]) -> None:
        if index == 0:
            shutdown.request()
        return None

    spy.faults["upload_part"] = request_shutdown_after_the_first_part
    try:
        with pytest.raises(OperationCancelledError):
            destination.upload_multipart(
                "aimm/big.bin", ChunkReader(payload), size=len(payload), part_size=PART
            )
    finally:
        shutdown.reset()

    assert_aborted(spy, "aimm/big.bin")
    assert len(spy.params("upload_part")) == 1, "no further part may start after the signal"


def test_a_shutdown_signal_stops_an_upload_before_it_opens(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    """Requested before the first part: nothing is created, so nothing needs aborting."""
    destination, spy = spy_destination
    payload = b"z" * PART

    shutdown.request()
    try:
        with pytest.raises(OperationCancelledError):
            destination.upload_multipart(
                "aimm/none.bin", ChunkReader(payload), size=len(payload), part_size=PART
            )
    finally:
        shutdown.reset()

    assert spy.params("upload_part") == []
    assert_aborted(spy, "aimm/none.bin")


def test_abort_when_the_reader_itself_fails(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    """A torn HTTP body from Hugging Face surfaces as a reader error, not an S3 error."""
    destination, spy = spy_destination

    class TornReader:
        def __init__(self) -> None:
            self.calls = 0

        def read(self, n: int, /) -> bytes:
            self.calls += 1
            if self.calls > 3:
                raise OSError("connection reset by peer")
            return b"q" * min(n, MIB)

    with pytest.raises(OSError, match="connection reset"):
        destination.upload_multipart(
            "aimm/torn.bin", TornReader(), size=PART * 2, part_size=PART
        )
    assert_aborted(spy, "aimm/torn.bin")


def test_abort_on_a_short_source_stream(spy_destination: tuple[S3Destination, SpyClient]) -> None:
    """Fewer bytes than promised means a truncated backup, so it must fail and abort."""
    destination, spy = spy_destination
    payload = b"x" * (PART + 10)

    with pytest.raises(SizeMismatchError) as excinfo:
        destination.upload_multipart(
            "aimm/short.bin", ChunkReader(payload), size=PART * 3, part_size=PART
        )
    assert "expected" in str(excinfo.value)
    assert_aborted(spy, "aimm/short.bin")


def test_abort_on_keyboard_interrupt_and_the_interrupt_still_propagates(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    """Ctrl-C must clean up too — and must NOT be swallowed into an UploadFailedError."""
    destination, spy = spy_destination
    spy.faults["upload_part"] = raises(KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        destination.upload_multipart(
            "aimm/interrupted.bin", ChunkReader(b"x" * PART), size=PART, part_size=PART
        )
    assert_aborted(spy, "aimm/interrupted.bin")


def test_a_failed_abort_does_not_mask_the_original_error(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    """Cleanup is best-effort; the caller must still see why the upload failed."""
    destination, spy = spy_destination
    spy.faults["upload_part"] = raises(client_error("InvalidRequest", "UploadPart"))
    spy.faults["abort_multipart_upload"] = raises(client_error("AccessDenied", "Abort"))

    with pytest.raises(UploadFailedError) as excinfo:
        destination.upload_multipart(
            "aimm/f.bin", ChunkReader(b"x" * PART), size=PART, part_size=PART
        )
    assert "InvalidRequest" in str(excinfo.value)


def test_a_failed_create_multipart_upload_needs_no_abort(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    destination, spy = spy_destination
    spy.faults["create_multipart_upload"] = raises(client_error("AccessDenied", "Create"))

    with pytest.raises(UploadFailedError):
        destination.upload_multipart(
            "aimm/f.bin", ChunkReader(b"x" * PART), size=PART, part_size=PART
        )
    assert not spy.called("abort_multipart_upload"), "there was no upload id to abort"


def test_no_multipart_upload_is_left_pending_after_a_failure(
    spy_destination: tuple[S3Destination, SpyClient], s3_client: S3Client, s3_bucket: str
) -> None:
    """The observable consequence: the server has no orphaned upload afterwards."""
    destination, spy = spy_destination
    spy.faults["upload_part"] = raises(client_error("InvalidRequest", "UploadPart"), on_call=1)

    with pytest.raises(UploadFailedError):
        destination.upload_multipart(
            "aimm/f.bin", ChunkReader(b"x" * PART * 2), size=PART * 2, part_size=PART
        )

    pending = s3_client.list_multipart_uploads(Bucket=s3_bucket).get("Uploads", [])
    assert pending == [], f"an orphaned multipart upload survived: {pending}"


# ── post-upload size verification ────────────────────────────────────────────


def head_returning_size(size: int) -> Any:
    """A `SpyClient` fault making head_object lie about ContentLength."""

    def fault(_index: int, kwargs: dict[str, Any]) -> dict[str, Any]:
        return {
            "ContentLength": size,
            "ETag": '"deadbeef"',
            "Metadata": {},
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    return fault


def test_multipart_verifies_the_stored_size_with_head_object(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    """A truncated upload must fail at backup time, not silently at restore time."""
    destination, spy = spy_destination
    spy.faults["head_object"] = head_returning_size(PART - 1)

    with pytest.raises(SizeMismatchError) as excinfo:
        destination.upload_multipart(
            "aimm/t.bin", ChunkReader(b"x" * PART), size=PART, part_size=PART
        )
    assert "stored" in str(excinfo.value)
    assert spy.called("head_object"), "the size check must actually issue a HEAD"


def test_put_small_verifies_the_stored_size_with_head_object(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    destination, spy = spy_destination
    spy.faults["head_object"] = head_returning_size(999)

    with pytest.raises(SizeMismatchError):
        destination.put_small("aimm/s.bin", b"hello", sha256=sha256_bytes(b"hello"))


def test_an_object_absent_immediately_after_upload_is_a_size_mismatch(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    destination, spy = spy_destination
    spy.faults["head_object"] = raises(client_error("404", "HeadObject", status=404))

    with pytest.raises(SizeMismatchError) as excinfo:
        destination.put_small("aimm/gone.bin", b"hello", sha256=sha256_bytes(b"hello"))
    assert "absent immediately after" in str(excinfo.value)


# ── the three write paths ────────────────────────────────────────────────────


def test_inline_path_round_trips_bytes_and_metadata(
    destination: S3Destination, s3_client: S3Client, s3_bucket: str
) -> None:
    payload = b'{"model_type": "llama"}'
    digest = sha256_bytes(payload)

    result = destination.put_small(
        "aimm/config.json",
        payload,
        sha256=digest,
        metadata={"Aimm-Repo-Id": "acme/model", "aimm-commit-sha": "f" * 40},
    )

    assert result.parts == 1
    assert result.part_size is None, "a single PutObject has no part size"
    assert result.size == len(payload)
    assert '"' not in result.etag, "the ETag must be unquoted"

    stored = s3_client.get_object(Bucket=s3_bucket, Key="aimm/config.json")
    assert stored["Body"].read() == payload
    assert stored["Metadata"] == {
        "aimm-repo-id": "acme/model",
        "aimm-commit-sha": "f" * 40,
        SHA256_METADATA_KEY: digest,
    }


def test_metadata_keys_come_back_lowercased(destination: S3Destination) -> None:
    """S3 lowercases metadata keys, so every round-trip comparison must expect that."""
    destination.put_small(
        "aimm/m.bin", b"x", sha256=sha256_bytes(b"x"), metadata={"AIMM-Repo-Type": "models"}
    )
    head = destination.head("aimm/m.bin")
    assert head is not None
    assert "aimm-repo-type" in head.metadata
    assert head.metadata["aimm-repo-type"] == "models"


def test_non_printable_metadata_values_are_reduced_to_ascii(
    destination: S3Destination,
) -> None:
    """Non-ASCII comes back RFC 2047 encoded and control characters make S3 drop the entry."""
    destination.put_small(
        "aimm/u.bin",
        b"x",
        sha256=sha256_bytes(b"x"),
        metadata={"aimm-repo-id": "ünïcode/mödel\x01"},
    )
    head = destination.head("aimm/u.bin")
    assert head is not None
    assert head.metadata["aimm-repo-id"] == "ncode/mdel"


def test_checksum_sha256_is_sent_only_when_the_backend_supports_it(
    settings: Settings, s3_client: S3Client
) -> None:
    """Sending a checksum a backend rejects breaks the upload outright, so it is gated."""
    payload = b"hello"
    digest = sha256_bytes(payload)

    unsupported = SpyClient(s3_client)
    S3Destination(settings.s3, unsupported, _default_capabilities(settings.s3)).put_small(
        "aimm/no-checksum", payload, sha256=digest
    )
    assert "ChecksumSHA256" not in unsupported.params("put_object")[0]

    supported = SpyClient(s3_client)
    caps = _default_capabilities(settings.s3)
    S3Destination(
        settings.s3,
        supported,
        type(caps)(
            request_checksum_calculation=caps.request_checksum_calculation,
            addressing_style=caps.addressing_style,
            supports_sha256_checksum=True,
            supports_get_object_attributes=False,
            probed=True,
        ),
    ).put_small("aimm/with-checksum", payload, sha256=digest)
    assert "ChecksumSHA256" in supported.params("put_object")[0]


def test_streaming_path_uploads_from_an_iterator_backed_reader(
    destination: S3Destination, s3_client: S3Client, s3_bucket: str
) -> None:
    """The STREAM path: bytes arrive from the network in awkward chunks, never on disk."""
    payload = bytes(range(256)) * (PART * 2 // 256 + 3)
    result = destination.upload_multipart(
        "aimm/stream.bin",
        ChunkReader(payload, max_chunk=997),
        size=len(payload),
        part_size=PART,
    )
    assert result.parts == 3
    assert s3_client.get_object(Bucket=s3_bucket, Key="aimm/stream.bin")["Body"].read() == payload


def test_file_path_uploads_from_an_open_binary_handle(
    destination: S3Destination, s3_client: S3Client, s3_bucket: str, tmp_path: Path
) -> None:
    """The DISK path hands `upload_multipart` a real file object, not a wrapper."""
    payload = b"d" * (PART + 4096)
    staged = tmp_path / "staged.bin"
    staged.write_bytes(payload)

    with staged.open("rb") as handle:
        result = destination.upload_multipart(
            "aimm/disk.bin", handle, size=len(payload), part_size=PART
        )

    assert result.parts == 2
    assert s3_client.get_object(Bucket=s3_bucket, Key="aimm/disk.bin")["Body"].read() == payload


def test_an_empty_object_uploads_as_a_single_empty_part(destination: S3Destination) -> None:
    result = destination.upload_multipart(
        "aimm/empty.bin", io.BytesIO(b""), size=0, part_size=PART
    )
    assert result.parts == 1
    assert result.size == 0
    head = destination.head("aimm/empty.bin")
    assert head is not None and head.size == 0


def test_storage_and_encryption_parameters_are_omitted_when_unconfigured(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    """Sending StorageClass unconditionally is what breaks MinIO; omission is the default."""
    destination, spy = spy_destination
    destination.put_small("aimm/plain", b"x", sha256=sha256_bytes(b"x"))
    params = spy.params("put_object")[0]
    assert "StorageClass" not in params
    assert "ServerSideEncryption" not in params


def test_configured_storage_and_encryption_parameters_are_sent(
    s3_client: S3Client, s3_bucket: str
) -> None:
    s3 = S3Settings(
        bucket=s3_bucket,
        prefix="aimm",
        probe=False,
        storage_class="STANDARD",
        server_side_encryption="AES256",
    )
    spy = SpyClient(s3_client)
    S3Destination(s3, spy, _default_capabilities(s3)).put_small(
        "aimm/sse", b"x", sha256=sha256_bytes(b"x")
    )
    params = spy.params("put_object")[0]
    assert params["StorageClass"] == "STANDARD"
    assert params["ServerSideEncryption"] == "AES256"


# ── reads, listing and deletion ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("etag", "expected"),
    [
        ("01c8fe3563ffe17995d53ff0d06d0f1b-2", 2),
        ("01c8fe3563ffe17995d53ff0d06d0f1b-10000", 10_000),
        # A single PUT has a bare MD5 and therefore no part count.
        ("d41d8cd98f00b204e9800998ecf8427e", None),
        # Degenerate shapes must not be mistaken for a count.
        ("", None),
        ("-2", None),
        ("abc-", None),
        ("abc-xyz", None),
        ("abc-0", None),
    ],
)
def test_parts_from_etag_reads_the_multipart_suffix(etag: str, expected: int | None) -> None:
    """The part count is derived from the ETag, never from a PartNumber-bearing HEAD.

    Asking S3 for `PartsCount` requires sending `PartNumber`, and that makes the same
    response report the size of that ONE PART in `ContentLength`. Since `head()` builds
    `ObjectHead.size` from `ContentLength`, taking that route would silently turn every
    size verification into a comparison against a single part. The suffix is free and safe.
    """
    assert _parts_from_etag(etag) == expected


def test_head_returns_none_for_a_missing_key(destination: S3Destination) -> None:
    assert destination.head("aimm/never-written") is None
    assert destination.exists("aimm/never-written") is False


def test_get_bytes_and_get_stream_round_trip(destination: S3Destination) -> None:
    payload = b"n" * (3 * MIB)
    destination.put_small("aimm/read.bin", payload, sha256=sha256_bytes(payload))

    assert destination.get_bytes("aimm/read.bin") == payload
    with destination.get_stream("aimm/read.bin") as chunks:
        assert b"".join(chunks) == payload


def test_get_bytes_on_a_missing_key_raises_object_not_found(destination: S3Destination) -> None:
    with pytest.raises(ObjectNotFoundError):
        destination.get_bytes("aimm/absent")


def test_list_keys_paginates_beyond_the_thousand_key_page(
    destination: S3Destination, s3_client: S3Client, s3_bucket: str
) -> None:
    """1000 is the server's MaxKeys ceiling; without a paginator the rest is invisible."""
    total = 1150  # one full page plus a remainder, which is all pagination needs to prove
    for index in range(total):
        s3_client.put_object(Bucket=s3_bucket, Key=f"aimm/page/{index:05d}", Body=b"x")
    s3_client.put_object(Bucket=s3_bucket, Key="aimm/elsewhere/nope", Body=b"x")

    found = list(destination.list_keys("aimm/page/"))
    assert len(found) == total
    assert {summary.key for summary in found} == {
        f"aimm/page/{index:05d}" for index in range(total)
    }
    assert all(summary.size == 1 for summary in found)
    assert all('"' not in summary.etag for summary in found), "ETags must be unquoted"


def test_list_prefixes_returns_immediate_children_only(
    destination: S3Destination, s3_client: S3Client, s3_bucket: str
) -> None:
    for key in ("aimm/v1/models/acme/a/x", "aimm/v1/models/acme/b/y", "aimm/v1/models/other/c/z"):
        s3_client.put_object(Bucket=s3_bucket, Key=key, Body=b"x")

    assert sorted(destination.list_prefixes("aimm/v1/models/")) == [
        "aimm/v1/models/acme/",
        "aimm/v1/models/other/",
    ]


def test_delete_keys_batches_at_the_thousand_key_limit(
    spy_destination: tuple[S3Destination, SpyClient], s3_client: S3Client, s3_bucket: str
) -> None:
    """DeleteObjects accepts at most 1000 keys per call, so batching is not optional."""
    total = DELETE_BATCH_SIZE * 2 + 7
    keys_to_delete = [f"aimm/bulk/{index:05d}" for index in range(total)]
    destination, spy = spy_destination
    for key in keys_to_delete:
        s3_client.put_object(Bucket=s3_bucket, Key=key, Body=b"x")

    deleted = destination.delete_keys(keys_to_delete)

    assert deleted == total
    batches = [len(call["Delete"]["Objects"]) for call in spy.params("delete_objects")]
    assert batches == [DELETE_BATCH_SIZE, DELETE_BATCH_SIZE, 7]
    assert all(size <= DELETE_BATCH_SIZE for size in batches)
    assert list(destination.list_keys("aimm/bulk/")) == []


def test_delete_keys_on_an_empty_iterable_issues_no_request(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    destination, spy = spy_destination
    assert destination.delete_keys([]) == 0
    assert not spy.called("delete_objects")


def test_delete_keys_raises_when_the_backend_reports_per_key_failures(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    """A partial delete that reports success would silently strand objects forever."""
    destination, spy = spy_destination

    def fault(_index: int, _kwargs: dict[str, Any]) -> dict[str, Any]:
        return {
            "Deleted": [{"Key": "aimm/ok"}],
            "Errors": [{"Key": "aimm/locked", "Code": "AccessDenied"}],
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    spy.faults["delete_objects"] = fault
    with pytest.raises(DestinationError) as excinfo:
        destination.delete_keys(["aimm/ok", "aimm/locked"])
    assert "aimm/locked" in str(excinfo.value)


# ── stale multipart uploads ──────────────────────────────────────────────────


def test_abort_stale_uploads_uses_the_injected_now(
    destination: S3Destination, s3_client: S3Client, s3_bucket: str
) -> None:
    """Orphaned uploads never appear in ListObjectsV2, so nothing else can see them."""
    s3_client.create_multipart_upload(Bucket=s3_bucket, Key="aimm/stale/a")
    s3_client.create_multipart_upload(Bucket=s3_bucket, Key="aimm/stale/b")
    initiated = s3_client.list_multipart_uploads(Bucket=s3_bucket)["Uploads"][0]["Initiated"]

    # `now` sits only an hour past the upload, so a 24 h threshold spares them.
    spared = destination.abort_stale_uploads(
        "aimm/", timedelta(hours=24), now=initiated + timedelta(hours=1)
    )
    assert spared == 0
    assert len(s3_client.list_multipart_uploads(Bucket=s3_bucket).get("Uploads", [])) == 2

    aborted = destination.abort_stale_uploads(
        "aimm/", timedelta(hours=24), now=initiated + timedelta(days=3)
    )
    assert aborted == 2
    assert s3_client.list_multipart_uploads(Bucket=s3_bucket).get("Uploads", []) == []


def test_abort_stale_uploads_accepts_a_naive_now(
    destination: S3Destination, s3_client: S3Client, s3_bucket: str
) -> None:
    """A naive datetime is treated as UTC rather than raising a comparison TypeError."""
    s3_client.create_multipart_upload(Bucket=s3_bucket, Key="aimm/stale/a")
    naive = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=3650)
    assert destination.abort_stale_uploads("aimm/", timedelta(hours=1), now=naive) == 1


def test_abort_stale_uploads_respects_the_prefix(
    destination: S3Destination, s3_client: S3Client, s3_bucket: str
) -> None:
    s3_client.create_multipart_upload(Bucket=s3_bucket, Key="aimm/wanted/a")
    s3_client.create_multipart_upload(Bucket=s3_bucket, Key="other/unwanted/b")
    future = datetime.now(UTC) + timedelta(days=3650)

    assert destination.abort_stale_uploads("aimm/", timedelta(hours=1), now=future) == 1
    remaining = {
        upload["Key"] for upload in s3_client.list_multipart_uploads(Bucket=s3_bucket)["Uploads"]
    }
    assert remaining == {"other/unwanted/b"}


# ── the capability probe ─────────────────────────────────────────────────────


def test_probe_discovers_capabilities_and_cleans_up_after_itself(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    """The probe writes one tiny throwaway object and must always remove it."""
    destination, spy = spy_destination

    capabilities = destination.probe()

    assert capabilities.probed is True
    assert capabilities.supports_sha256_checksum is True, "moto accepts ChecksumSHA256"
    assert spy.called("delete_object"), "the probe object must be deleted"
    probe_keys = {call["Key"] for call in spy.params("put_object")}
    assert all("_probe/" in key for key in probe_keys)
    assert list(destination.list_keys("aimm/v1/_probe/")) == []


def test_probe_downgrades_to_when_required_if_the_trailer_is_rejected(
    s3_client: S3Client, s3_bucket: str
) -> None:
    """A backend that rejects botocore's checksum trailer must be detected, not assumed.

    This is the branch that only exists because trailer support across MinIO, Ceph RGW,
    R2 and Wasabi is unverified — and because the trailer is emitted over HTTPS only, so
    no plain-HTTP rig can provoke it. Here it is provoked directly.
    """
    s3 = S3Settings(bucket=s3_bucket, prefix="aimm", probe=True, checksum_calculation="auto")
    spy = SpyClient(s3_client)

    def reject_checksummed_put(_index: int, kwargs: dict[str, Any]) -> None:
        if "ChecksumSHA256" in kwargs:
            raise client_error("BadDigest", "PutObject")
        return None

    spy.faults["put_object"] = reject_checksummed_put
    destination = S3Destination(s3, spy, _default_capabilities(s3))

    capabilities = destination.probe()

    assert capabilities.request_checksum_calculation == "when_required"
    assert capabilities.supports_sha256_checksum is False
    assert capabilities.probed is True


def test_an_explicit_checksum_setting_is_never_downgraded_by_the_probe(
    s3_client: S3Client, s3_bucket: str
) -> None:
    """An operator's explicit choice outranks the probe, always."""
    s3 = S3Settings(
        bucket=s3_bucket, prefix="aimm", probe=True, checksum_calculation="when_supported"
    )
    spy = SpyClient(s3_client)
    spy.faults["put_object"] = lambda _index, kwargs: (
        None if "ChecksumSHA256" not in kwargs else _raise(client_error("BadDigest", "PutObject"))
    )
    destination = S3Destination(s3, spy, _default_capabilities(s3))

    capabilities = destination.probe()

    assert capabilities.request_checksum_calculation == "when_supported"


def _raise(exc: BaseException) -> None:
    raise exc


def test_probe_reports_missing_get_object_attributes_without_failing(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    """head_object + ETag is the fallback, so an unsupported call is informational."""
    destination, spy = spy_destination
    spy.faults["get_object_attributes"] = raises(client_error("NotImplemented", "GOA"))

    capabilities = destination.probe()

    assert capabilities.supports_get_object_attributes is False
    assert capabilities.probed is True


def test_a_probe_that_cannot_write_at_all_raises(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    """When even the unchecksummed retry fails, the endpoint is genuinely unusable."""
    destination, spy = spy_destination
    spy.faults["put_object"] = lambda _index, _kwargs: _raise(
        client_error("AccessDenied", "PutObject", status=403)
    )

    with pytest.raises(AuthError):
        destination.probe()
    assert spy.called("delete_object"), "cleanup must run even when the probe fails"


def test_create_with_probe_enabled_returns_probed_capabilities(
    aws_credentials: None, s3_bucket: str
) -> None:
    s3 = S3Settings(bucket=s3_bucket, prefix="aimm", probe=True)
    destination = S3Destination.create(s3, workers=4)
    try:
        assert destination.capabilities.probed is True
    finally:
        destination.close()


# ── bucket handling and error translation ────────────────────────────────────


def test_ensure_bucket_is_off_by_default(s3_client: S3Client) -> None:
    """The reference target is a shared production store provisioned as IaC."""
    s3 = S3Settings(bucket="never-created", prefix="aimm", probe=False)
    spy = SpyClient(s3_client)
    S3Destination(s3, spy, _default_capabilities(s3)).ensure_bucket()
    assert not spy.called("create_bucket")
    assert not spy.called("head_bucket")


def test_ensure_bucket_creates_the_bucket_when_asked(s3_client: S3Client) -> None:
    s3 = S3Settings(bucket="opt-in-bucket", prefix="aimm", probe=False, ensure_bucket=True)
    S3Destination(s3, s3_client, _default_capabilities(s3)).ensure_bucket()
    assert s3_client.head_bucket(Bucket="opt-in-bucket")["ResponseMetadata"]["HTTPStatusCode"] == 200


def test_ensure_bucket_is_idempotent(s3_client: S3Client, s3_bucket: str) -> None:
    s3 = S3Settings(bucket=s3_bucket, prefix="aimm", probe=False, ensure_bucket=True)
    spy = SpyClient(s3_client)
    S3Destination(s3, spy, _default_capabilities(s3)).ensure_bucket()
    assert not spy.called("create_bucket"), "an existing bucket must not be recreated"


def test_create_fails_fast_when_the_bucket_is_missing(
    aws_credentials: None, s3_client: S3Client
) -> None:
    """Failing at construction beats failing halfway through the first upload."""
    s3 = S3Settings(bucket="absent-bucket", prefix="aimm", probe=False)
    with pytest.raises(BucketNotFoundError):
        S3Destination.create(s3, workers=2)


def test_create_rejects_a_non_portable_storage_class_before_any_upload(s3_bucket: str) -> None:
    """MinIO's IsValid() accepts only STANDARD and REDUCED_REDUNDANCY."""
    s3 = S3Settings(
        bucket=s3_bucket, prefix="aimm", probe=False, preset="minio", storage_class="STANDARD_IA"
    )
    with pytest.raises(ConfigError) as excinfo:
        S3Destination.create(s3, workers=2)
    assert "not portable" in str(excinfo.value)


def test_the_aws_preset_permits_any_storage_class(aws_credentials: None, s3_bucket: str) -> None:
    s3 = S3Settings(
        bucket=s3_bucket, prefix="aimm", probe=False, preset="aws", storage_class="STANDARD_IA"
    )
    destination = S3Destination.create(s3, workers=2)
    try:
        assert destination.capabilities.probed is False
    finally:
        destination.close()


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        ("AccessDenied", 403, AuthError),
        ("InvalidAccessKeyId", 403, AuthError),
        ("ExpiredToken", 403, AuthError),
        ("NoSuchBucket", 404, BucketNotFoundError),
        ("NoSuchKey", 404, ObjectNotFoundError),
        ("InternalErrorNotRetryableHere", 400, DestinationError),
    ],
)
def test_error_codes_translate_to_typed_exceptions(
    spy_destination: tuple[S3Destination, SpyClient],
    code: str,
    status: int,
    expected: type[Exception],
) -> None:
    """The exit-code contract depends entirely on this mapping being right."""
    destination, spy = spy_destination
    spy.faults["get_object"] = raises(client_error(code, "GetObject", status=status))
    with pytest.raises(expected):
        destination.get_bytes("aimm/whatever")


def test_close_never_raises(destination: S3Destination) -> None:
    destination.close()
    destination.close()


# ── what moto cannot do: deferred to the MinIO rig ───────────────────────────
# Probed against moto 5.2.2: head_object returns PartsCount=None for every multipart
# object, and GetObjectAttributes answers without an ObjectParts member. Asserting either
# here would mean stubbing the very response under test, which manufactures a green tick
# for behaviour that was never exercised. These run against the real server instead, and
# skip when the rig is down.


@pytest.fixture
def minio_destination(minio_endpoint: str) -> Iterator[S3Destination]:
    """A destination pointed at the live MinIO rig, per the AIMM_IT_* contract."""
    import os
    import uuid

    from pydantic import SecretStr

    s3 = S3Settings(
        endpoint_url=minio_endpoint,
        bucket=os.environ.get("AIMM_IT_BUCKET", "aimm-it"),
        region=os.environ.get("AIMM_IT_REGION", "eu-north1"),
        prefix=f"aimm-parts-{uuid.uuid4().hex[:8]}",
        access_key_id=SecretStr(os.environ["AIMM_IT_ACCESS_KEY"]),
        secret_access_key=SecretStr(os.environ["AIMM_IT_SECRET_KEY"]),
        probe=True,
    )
    destination = S3Destination.create(s3, workers=2)
    try:
        yield destination
    finally:
        destination.delete_keys(
            summary.key for summary in destination.list_keys(s3.prefix)
        )
        destination.close()


@pytest.mark.integration
def test_head_object_reports_parts_count_on_a_real_server(
    minio_destination: S3Destination,
) -> None:
    """moto returns PartsCount=None for every multipart object, so this needs MinIO.

    `ObjectHead.parts_count` is how a verifier distinguishes a multipart object from a
    single PUT without re-deriving it from the ETag suffix. If the field is never
    populated by any server we talk to, the field is decoration.
    """
    key = f"{minio_destination._settings.prefix}/parts.bin"
    payload = b"p" * (PART * 2)
    result = minio_destination.upload_multipart(
        key, ChunkReader(payload), size=len(payload), part_size=PART
    )

    assert result.parts == 2
    head = minio_destination.head(key)
    assert head is not None
    assert head.parts_count == 2
    assert head.etag.endswith("-2"), "a multipart ETag carries the -N part-count suffix"


@pytest.mark.integration
def test_get_object_attributes_returns_object_parts_on_a_real_server(
    minio_destination: S3Destination,
) -> None:
    """moto implements GetObjectAttributes but never returns the ObjectParts member.

    ObjectParts is the only way to read back the server's own view of the part sizes,
    which is what proves our part-size arithmetic matched what was actually stored.
    """
    key = f"{minio_destination._settings.prefix}/attrs.bin"
    payload = b"p" * (PART * 3)
    minio_destination.upload_multipart(
        key, ChunkReader(payload), size=len(payload), part_size=PART
    )

    if not minio_destination.capabilities.supports_get_object_attributes:
        pytest.skip("this MinIO build does not implement GetObjectAttributes")

    response = minio_destination.client.get_object_attributes(
        Bucket=minio_destination._settings.bucket,
        Key=key,
        ObjectAttributes=["ETag", "ObjectSize", "ObjectParts"],
    )
    assert response["ObjectParts"]["TotalPartsCount"] == 3
    assert response["ObjectSize"] == len(payload)


# ── regressions ──────────────────────────────────────────────────────────────


def commit_then_lose_the_upload_id(spy: SpyClient) -> Any:
    """Model real S3: the server commits, the client times out, the retry gets 404.

    moto does NOT reproduce this — it tolerates a repeated complete and answers success —
    which is a moto artifact rather than S3 behaviour, and is exactly why the suite
    missed this. So the fault drives moto's real commit itself, then raises the timeout
    the client would have seen, and answers NoSuchUpload from then on the way a real
    server does once the UploadId has been spent.
    """
    state = {"committed": False}

    def fault(_index: int, kwargs: dict[str, Any]) -> None:
        if state["committed"]:
            raise client_error("NoSuchUpload", "CompleteMultipartUpload", status=404)
        spy.inner.complete_multipart_upload(**kwargs)
        state["committed"] = True
        raise ReadTimeoutError(endpoint_url="https://minio.example")

    return fault


def test_a_complete_that_times_out_after_committing_is_not_reported_as_a_failure(
    spy_destination: tuple[S3Destination, SpyClient], instant_retry: None
) -> None:
    """Regression: a successful multi-hour upload was turned into a hard failure.

    A 400 GiB shard commits in ~6,400 parts and routinely takes longer than
    `read_timeout`. botocore raises ReadTimeoutError, which `is_retryable` accepts, so
    complete is re-issued — but the server had already committed, so the UploadId is
    gone and the retry answers NoSuchUpload. That is not in RETRYABLE_S3_ERROR_CODES, so
    it used to propagate as UploadFailedError for a file that was correctly stored, and
    `_sync_repo` then withheld the manifest for the WHOLE revision.
    """
    destination, spy = spy_destination
    spy.faults["complete_multipart_upload"] = commit_then_lose_the_upload_id(spy)
    payload = b"x" * PART

    result = destination.upload_multipart(
        "aimm/big.bin", ChunkReader(payload), size=len(payload), part_size=PART
    )

    assert result.size == len(payload)
    assert result.etag
    assert len(spy.params("complete_multipart_upload")) > 1, "the retry did not happen"
    assert destination.head("aimm/big.bin") is not None


def test_a_permanent_complete_failure_still_aborts_and_raises(
    spy_destination: tuple[S3Destination, SpyClient],
) -> None:
    """The disambiguation must not swallow an unambiguous, permanent rejection."""
    destination, spy = spy_destination
    spy.faults["complete_multipart_upload"] = raises(
        client_error("InvalidRequest", "CompleteMultipartUpload")
    )
    payload = b"x" * PART

    with pytest.raises(UploadFailedError):
        destination.upload_multipart(
            "aimm/f.bin", ChunkReader(payload), size=len(payload), part_size=PART
        )
    assert_aborted(spy, "aimm/f.bin")


def test_a_lost_upload_id_with_no_stored_object_is_still_a_failure(
    spy_destination: tuple[S3Destination, SpyClient], instant_retry: None
) -> None:
    """NoSuchUpload with nothing in the bucket means the upload really did fail."""
    destination, spy = spy_destination
    spy.faults["complete_multipart_upload"] = raises(
        client_error("NoSuchUpload", "CompleteMultipartUpload", status=404)
    )
    payload = b"x" * PART

    with pytest.raises(UploadFailedError):
        destination.upload_multipart(
            "aimm/never.bin", ChunkReader(payload), size=len(payload), part_size=PART
        )
    assert_aborted(spy, "aimm/never.bin")


def test_put_small_refuses_an_object_above_the_single_put_limit(
    destination: S3Destination,
) -> None:
    """Regression: nothing between the config and the wire guarded the 5 GiB limit.

    `transfer.inline_max` used to be unbounded, so `inline_max: 6GiB` routed 6 GiB files
    to a single PutObject; S3 answers EntityTooLarge only AFTER the whole Hub download
    has been paid for. The library API guards itself rather than trusting the settings.
    """

    class Huge(bytes):
        def __len__(self) -> int:
            return MAX_SINGLE_PUT_SIZE + 1

    with pytest.raises(ObjectTooLargeError) as excinfo:
        destination.put_small("aimm/huge.bin", Huge(), sha256="0" * 64)
    assert "single PutObject" in str(excinfo.value)


def test_the_configured_retry_budget_reaches_every_s3_call(
    settings: Settings, s3_client: S3Client
) -> None:
    """Regression: `transfer.max_attempts` and `transfer.max_wait` were dead settings.

    Every call site called `call_with_retry` bare, so the hardcoded five attempts always
    applied; an operator setting `max_attempts: 1` to fail fast during a MinIO incident
    got no effect at all, and a single throttled part could occupy a worker for minutes.
    """
    spy = SpyClient(s3_client)
    destination = S3Destination(
        settings.s3, spy, _default_capabilities(settings.s3), attempts=2, max_wait=0.001
    )

    def always_throttled(_index: int, _kwargs: dict[str, Any]) -> None:
        raise client_error("SlowDown", "PutObject", status=503)

    spy.faults["put_object"] = always_throttled

    with pytest.raises(UploadFailedError):
        destination.put_small("aimm/throttled.bin", b"payload", sha256="0" * 64)

    assert len(spy.params("put_object")) == 2, "attempts= must reach the retry layer"


def test_create_sizes_the_connection_pool_for_the_requested_workers(
    aws_credentials: None, s3_bucket: str
) -> None:
    """Regression: the CLI never passed `workers`, so the pool was stuck at 32.

    With `--workers 64` the engine ran 64 concurrent uploads against a 32-connection
    urllib3 pool built with `block=False`, so every request past the 32nd created and
    immediately discarded a connection — a fresh TLS handshake each time, for the whole
    run.
    """
    s3 = S3Settings(bucket=s3_bucket, prefix="aimm", probe=False)
    destination = S3Destination.create(s3, workers=64)
    try:
        assert destination.client.meta.config.max_pool_connections == 128
    finally:
        destination.close()


def test_disabling_tls_verification_is_logged_as_a_warning(
    aws_credentials: None, s3_bucket: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: `--no-verify-tls` produced a completely silent insecure client.

    A profile written for a self-signed staging MinIO carried to production sent SigV4
    Authorization headers and every model byte over a channel any on-path party can
    proxy, with no WARNING, no `--json` field and no marker in any table.
    """
    s3 = S3Settings(bucket=s3_bucket, prefix="aimm", probe=False, verify_tls=False)
    with caplog.at_level("WARNING"):
        destination = S3Destination.create(s3, workers=2)
    destination.close()

    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert any("verification is DISABLED" in message for message in warnings), warnings
