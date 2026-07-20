"""Behaviour that only a real MinIO can prove.

Everything here is deliberately narrow: a test belongs in this file ONLY if moto cannot
answer it honestly. Anything moto can fake is a unit test in `tests/hfbackup/` and must
stay there, because these tests need Docker, take seconds rather than milliseconds, and
are skipped on any machine without the rig.

The directory-level `conftest.py` marks every test here `integration` and skips the whole
module when `AIMM_IT_ENDPOINT` is unset, so no marker is needed below.

What this file does NOT prove, and no green run here should be read as proving: the rig
speaks plain HTTP, and botocore's aws-chunked checksum trailer only appears over HTTPS.
Trailer compatibility with a production TLS endpoint remains unverified. See README.md.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

    from bg_ai_model_management.tools.hfbackup.destination import S3Destination


def uploads_under(client: S3Client, bucket: str, prefix: str) -> list[str]:
    """Keys of the in-flight multipart uploads under `prefix`, filtered client-side.

    Deliberately does NOT pass `Prefix` to the server. MinIO honours it on
    ListMultipartUploads only when it equals a complete object key, so a directory prefix
    silently returns nothing — the very bug `abort_stale_uploads` was fixed for. A test
    that asked the server to filter would report "no uploads" for both a working and a
    broken implementation, and would therefore assert nothing at all.
    """
    paginator = client.get_paginator("list_multipart_uploads")
    return [
        upload["Key"]
        for page in paginator.paginate(Bucket=bucket)
        for upload in page.get("Uploads", [])
        if upload["Key"].startswith(prefix)
    ]

# 5 MiB is the S3 minimum part size; two parts is the cheapest genuine multipart upload.
PART = 5 * 1024 * 1024


class ChunkReader:
    """A `ByteReader` over an in-memory payload that returns short reads.

    Short reads are the point: a reader backed by a socket routinely returns fewer bytes
    than asked for, and an uploader that assumes `read(n)` yields exactly `n` bytes builds
    undersized parts. Returning at most 64 KiB per call keeps that path exercised.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, n: int, /) -> bytes:
        chunk = self._data[self._pos : self._pos + min(n, 64 * 1024)]
        self._pos += len(chunk)
        return chunk


# ------------------------------------------------------------------ least privilege


def test_the_probe_succeeds_under_the_scoped_service_account(
    rig_destination: S3Destination,
) -> None:
    """Constructing the destination runs the capability probe against a real policy.

    The rig authenticates with the service account `minio-init` provisions, not with the
    root credentials, and its policy grants exactly the actions the tool claims to need.
    If the tool ever starts calling something outside that grant, this fixture stops
    resolving — which is the whole reason the rig uses least privilege.
    """
    capabilities = rig_destination.capabilities
    assert capabilities is not None


# ------------------------------------------------------------------ multipart truth


def test_multipart_round_trip_is_byte_exact(rig_destination: S3Destination, rig_prefix: str) -> None:
    """Upload a genuine two-part object and read every byte back.

    moto stores parts in process and hands them back, so it cannot fail the way a real
    server fails: a wrong part size, a short read or an off-by-one in the final part all
    still round-trip under moto and only surface against MinIO.
    """
    payload = bytes(range(256)) * (PART * 2 // 256)
    key = f"{rig_prefix}/round-trip.bin"

    result = rig_destination.upload_multipart(
        key, ChunkReader(payload), size=len(payload), part_size=PART
    )

    assert result.parts == 2
    returned = rig_destination.get_bytes(key)
    assert len(returned) == len(payload)
    assert hashlib.sha256(returned).hexdigest() == hashlib.sha256(payload).hexdigest()


def test_head_reports_the_part_count_of_a_multipart_object(
    rig_destination: S3Destination, rig_prefix: str
) -> None:
    """`ObjectHead.parts_count` must be populated, and must not lie.

    It is derived from the ETag's `-N` suffix rather than from S3's `PartsCount`, because
    S3 only returns `PartsCount` when the request carries a `PartNumber` — and adding a
    `PartNumber` makes `ContentLength` report that part's size instead of the object's,
    which would silently corrupt every size check built on `ObjectHead.size`. This test
    pins both halves of that: the count is right AND the size is still the whole object.
    """
    payload = b"p" * (PART * 2)
    key = f"{rig_prefix}/parts.bin"
    rig_destination.upload_multipart(key, ChunkReader(payload), size=len(payload), part_size=PART)

    head = rig_destination.head(key)

    assert head is not None
    assert head.parts_count == 2
    assert head.etag.endswith("-2"), "a multipart ETag carries the -N part-count suffix"
    assert head.size == len(payload), "size must be the whole object, not one part"


def test_single_put_object_reports_no_part_count(
    rig_destination: S3Destination, rig_prefix: str
) -> None:
    """A single-PUT object has no `-N` suffix, so `parts_count` must stay None.

    The counterpart to the test above: if the derivation were sloppy it would happily read
    a part count out of an ordinary MD5 ETag and report a single-PUT object as multipart.
    """
    payload = b"small"
    key = f"{rig_prefix}/single.bin"
    rig_destination.put_small(key, payload, sha256=hashlib.sha256(payload).hexdigest())

    head = rig_destination.head(key)

    assert head is not None
    assert head.parts_count is None
    assert head.size == len(payload)


# ------------------------------------------------------------------ metadata


def test_user_metadata_survives_a_real_server(
    rig_destination: S3Destination, rig_prefix: str
) -> None:
    """Metadata keys come back lower-cased and values intact through a real HTTP round trip.

    Header casing is normalised somewhere between botocore and the server, and the exact
    place differs per backend — which is precisely why asserting it against an in-process
    fake proves nothing.
    """
    payload = b"metadata"
    key = f"{rig_prefix}/meta.bin"
    rig_destination.put_small(
        key,
        payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        metadata={"Hf-Sha256": "abc123", "Hf-Commit": "deadbeef"},
    )

    head = rig_destination.head(key)

    assert head is not None
    assert head.metadata["hf-sha256"] == "abc123"
    assert head.metadata["hf-commit"] == "deadbeef"


# ------------------------------------------------------------------ orphaned uploads


def test_abort_stale_uploads_reaps_a_directory_prefix(
    rig_destination: S3Destination, rig_client: S3Client, rig_bucket: str, rig_prefix: str
) -> None:
    """Regression: MinIO honours ListMultipartUploads' `Prefix` only for a complete key.

    Filtering server-side by a directory prefix made this method a silent no-op on MinIO —
    it returned zero uploads, reported success, and orphaned multipart uploads accumulated
    forever, occupying storage that no `list_objects_v2` will ever show you. moto honours
    the prefix faithfully and therefore cannot catch this at all.

    An unfinished upload is created directly through the raw client so nothing depends on
    the abort path in `upload_multipart` that this method exists to clean up after.
    """
    key = f"{rig_prefix}/orphan.bin"
    created = rig_client.create_multipart_upload(Bucket=rig_bucket, Key=key)
    upload_id = created["UploadId"]

    try:
        aborted = rig_destination.abort_stale_uploads(
            rig_prefix,
            older_than=timedelta(seconds=0),
            now=datetime.now(UTC) + timedelta(hours=1),
        )

        assert aborted == 1, "a directory prefix must still find the orphaned upload"
        assert key not in uploads_under(rig_client, rig_bucket, rig_prefix)
    except BaseException:
        rig_client.abort_multipart_upload(Bucket=rig_bucket, Key=key, UploadId=upload_id)
        raise


def test_abort_stale_uploads_spares_an_upload_that_is_too_young(
    rig_destination: S3Destination, rig_client: S3Client, rig_bucket: str, rig_prefix: str
) -> None:
    """The age cutoff must be honoured, or a concurrent sync gets its upload killed.

    This is the failure the previous test's fix could easily introduce: once the prefix
    filter moved into Python it would be trivial to abort everything under the prefix
    regardless of age, destroying an in-flight upload from another process.
    """
    key = f"{rig_prefix}/fresh.bin"
    created = rig_client.create_multipart_upload(Bucket=rig_bucket, Key=key)
    upload_id = created["UploadId"]

    try:
        aborted = rig_destination.abort_stale_uploads(
            rig_prefix, older_than=timedelta(hours=24), now=datetime.now(UTC)
        )

        assert aborted == 0
        assert key in uploads_under(rig_client, rig_bucket, rig_prefix)
    finally:
        rig_client.abort_multipart_upload(Bucket=rig_bucket, Key=key, UploadId=upload_id)


# ------------------------------------------------------------------ listing & delete


def test_list_and_delete_round_trip_through_a_real_server(
    rig_destination: S3Destination, rig_prefix: str
) -> None:
    """`list_keys` sees what was written, and `delete_keys` removes exactly that set.

    Batch delete is one of the few operations where a backend can plausibly succeed for
    some keys and fail for others, and the count is what `prune` reports to the operator.
    """
    payload = b"x"
    keys = [f"{rig_prefix}/list-{index}.bin" for index in range(5)]
    for key in keys:
        rig_destination.put_small(key, payload, sha256=hashlib.sha256(payload).hexdigest())

    listed = sorted(summary.key for summary in rig_destination.list_keys(rig_prefix))
    assert listed == sorted(keys)

    deleted = rig_destination.delete_keys(keys)

    assert deleted == len(keys)
    assert list(rig_destination.list_keys(rig_prefix)) == []
