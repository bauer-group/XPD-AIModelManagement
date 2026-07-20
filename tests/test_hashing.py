"""Digests. Every value here is checked against an independent computation.

The hashing readers are the only place a 50 GB stream is measured, and they get one
pass at it. Two properties therefore matter more than the rest: a short `read()` must
never lose bytes (S3 part loops read in fixed sizes that will not divide the source
chunking), and the sha256 and git blob id must both come out of that single pass.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from bg_ai_model_management.integrity.hashing import (
    CHUNK_SIZE,
    HashingFileReader,
    HashingReader,
    composite_etag,
    git_blob_id,
    hash_file,
    sha256_b64,
    sha256_bytes,
    sha256_file,
)

PAYLOAD = b"the quick brown fox jumps over the lazy dog\n" * 37


def expected_blob_id(data: bytes) -> str:
    """Independent git blob id: `sha1(b"blob <len>\\0" + content)`."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def chunked(data: bytes, size: int) -> Iterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


def drain(reader: HashingReader | HashingFileReader, n: int) -> bytes:
    out = bytearray()
    while True:
        block = reader.read(n)
        if not block:
            return bytes(out)
        out.extend(block)


# ------------------------------------------------------------------------ one-shot


def test_sha256_bytes_matches_hashlib() -> None:
    assert sha256_bytes(PAYLOAD) == hashlib.sha256(PAYLOAD).hexdigest()


def test_sha256_bytes_of_empty_input() -> None:
    assert sha256_bytes(b"") == hashlib.sha256(b"").hexdigest()


def test_sha256_bytes_is_lowercase_hex() -> None:
    digest = sha256_bytes(PAYLOAD)
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(c in "0123456789abcdef" for c in digest)


def test_sha256_file_matches_the_in_memory_digest(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(PAYLOAD)
    assert sha256_file(path) == sha256_bytes(PAYLOAD)


@pytest.mark.parametrize("chunk_size", [1, 7, 64, 4096, CHUNK_SIZE])
def test_sha256_file_is_independent_of_chunk_size(tmp_path: Path, chunk_size: int) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(PAYLOAD)
    assert sha256_file(path, chunk_size=chunk_size) == sha256_bytes(PAYLOAD)


def test_sha256_file_of_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    assert sha256_file(path) == hashlib.sha256(b"").hexdigest()


# ----------------------------------------------------------------------- blob ids


def test_git_blob_id_matches_an_independent_computation() -> None:
    assert git_blob_id([PAYLOAD], len(PAYLOAD)) == expected_blob_id(PAYLOAD)


def test_git_blob_id_of_a_known_value() -> None:
    """`git hash-object` of a file containing exactly "hello\\n"."""
    assert git_blob_id([b"hello\n"], 6) == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_git_blob_id_of_empty_content() -> None:
    """The well-known empty blob id."""
    assert git_blob_id([], 0) == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def test_git_blob_id_is_independent_of_chunking() -> None:
    baseline = git_blob_id([PAYLOAD], len(PAYLOAD))
    for size in (1, 3, 512, len(PAYLOAD) * 2):
        assert git_blob_id(chunked(PAYLOAD, size), len(PAYLOAD)) == baseline


def test_git_blob_id_depends_on_the_declared_size() -> None:
    """The size goes into the header, so a wrong size must not silently agree."""
    assert git_blob_id([PAYLOAD], len(PAYLOAD)) != git_blob_id([PAYLOAD], len(PAYLOAD) + 1)


def test_hash_file_returns_both_digests_in_one_pass(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(PAYLOAD)
    digest, blob = hash_file(path, size=len(PAYLOAD))
    assert digest == sha256_bytes(PAYLOAD)
    assert blob == expected_blob_id(PAYLOAD)


def test_hash_file_on_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    digest, blob = hash_file(path, size=0)
    assert digest == hashlib.sha256(b"").hexdigest()
    assert blob == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


# ------------------------------------------------------------------------- ETags


def test_composite_etag_matches_the_s3_rule() -> None:
    digests = [hashlib.md5(b"part-a", usedforsecurity=False).digest()]
    digests.append(hashlib.md5(b"part-b", usedforsecurity=False).digest())
    expected = hashlib.md5(b"".join(digests), usedforsecurity=False).hexdigest()
    assert composite_etag(digests) == f"{expected}-2"


def test_composite_etag_can_be_quoted() -> None:
    digests = [hashlib.md5(b"x", usedforsecurity=False).digest()]
    assert composite_etag(digests, quoted=True) == f'"{composite_etag(digests)}"'


def test_composite_etag_part_count_is_the_suffix() -> None:
    digests = [hashlib.md5(bytes([i]), usedforsecurity=False).digest() for i in range(7)]
    assert composite_etag(digests).endswith("-7")


def test_composite_etag_is_order_sensitive() -> None:
    a = hashlib.md5(b"a", usedforsecurity=False).digest()
    b = hashlib.md5(b"b", usedforsecurity=False).digest()
    assert composite_etag([a, b]) != composite_etag([b, a])


def test_sha256_b64_matches_the_header_encoding() -> None:
    import base64

    hex_digest = sha256_bytes(PAYLOAD)
    assert sha256_b64(hex_digest) == base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")
    assert len(sha256_b64(hex_digest)) == 44


# ------------------------------------------------------------------ HashingReader


def test_hashing_reader_yields_the_whole_stream() -> None:
    reader = HashingReader(chunked(PAYLOAD, 100), size=len(PAYLOAD))
    assert drain(reader, 1024) == PAYLOAD


@pytest.mark.parametrize("read_size", [1, 3, 99, 100, 101, 4096, len(PAYLOAD), len(PAYLOAD) * 3])
def test_hashing_reader_survives_any_read_size(read_size: int) -> None:
    """Short reads are legal and the caller loops; no byte may be dropped or repeated."""
    reader = HashingReader(chunked(PAYLOAD, 97), size=len(PAYLOAD))
    assert drain(reader, read_size) == PAYLOAD
    assert reader.hexdigest == sha256_bytes(PAYLOAD)
    assert reader.blob_id == expected_blob_id(PAYLOAD)
    assert reader.bytes_read == len(PAYLOAD)


def test_hashing_reader_computes_both_digests_in_one_pass() -> None:
    reader = HashingReader(chunked(PAYLOAD, 64), size=len(PAYLOAD))
    drain(reader, 128)
    assert reader.hexdigest == sha256_bytes(PAYLOAD)
    assert reader.blob_id == expected_blob_id(PAYLOAD)


def test_hashing_reader_tolerates_empty_intermediate_chunks() -> None:
    """An empty chunk mid-stream is not EOF; treating it as EOF truncates the upload."""
    chunks = iter([b"abc", b"", b"def", b"", b"ghi"])
    reader = HashingReader(chunks, size=9)
    assert drain(reader, 4) == b"abcdefghi"
    assert reader.hexdigest == sha256_bytes(b"abcdefghi")


def test_hashing_reader_returns_empty_bytes_at_eof_and_stays_there() -> None:
    reader = HashingReader(iter([b"abc"]), size=3)
    assert reader.read(10) == b"abc"
    assert reader.read(10) == b""
    assert reader.read(10) == b""


def test_hashing_reader_rejects_a_non_positive_read() -> None:
    reader = HashingReader(chunked(PAYLOAD, 16), size=len(PAYLOAD))
    assert reader.read(0) == b""
    assert reader.read(-1) == b""
    assert reader.bytes_read == 0


def test_hashing_reader_on_an_empty_stream() -> None:
    reader = HashingReader(iter([]), size=0)
    assert reader.read(16) == b""
    assert reader.bytes_read == 0
    assert reader.hexdigest == hashlib.sha256(b"").hexdigest()
    assert reader.blob_id == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def test_hashing_reader_digest_is_valid_mid_stream() -> None:
    reader = HashingReader(chunked(PAYLOAD, 50), size=len(PAYLOAD))
    reader.read(10)
    consumed = reader.bytes_read
    assert reader.hexdigest == sha256_bytes(PAYLOAD[:consumed])


def test_hashing_reader_buffers_at_most_one_chunk_ahead() -> None:
    """Constant memory is the whole point; a reader that drained the iterator on the
    first `read()` would hold a 50 GB file in RAM."""
    source = chunked(PAYLOAD, 64)
    reader = HashingReader(source, size=len(PAYLOAD))
    reader.read(1)
    assert reader.bytes_read == 64


# -------------------------------------------------------------- HashingFileReader


def test_hashing_file_reader_matches_the_stream_reader(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(PAYLOAD)
    with path.open("rb") as handle:
        reader = HashingFileReader(handle, size=len(PAYLOAD))
        assert drain(reader, 333) == PAYLOAD
    assert reader.hexdigest == sha256_bytes(PAYLOAD)
    assert reader.blob_id == expected_blob_id(PAYLOAD)
    assert reader.bytes_read == len(PAYLOAD)


def test_hashing_file_reader_on_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    with path.open("rb") as handle:
        reader = HashingFileReader(handle, size=0)
        assert reader.read(16) == b""
    assert reader.bytes_read == 0
    assert reader.hexdigest == hashlib.sha256(b"").hexdigest()


def test_hashing_file_reader_rejects_a_non_positive_read(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(PAYLOAD)
    with path.open("rb") as handle:
        reader = HashingFileReader(handle, size=len(PAYLOAD))
        assert reader.read(0) == b""
        assert reader.read(-5) == b""
        assert reader.bytes_read == 0


def test_both_readers_agree_on_the_same_payload(tmp_path: Path) -> None:
    """The stream path and the disk path must produce identical anchors, or a file
    would verify differently depending on which transfer path the planner chose."""
    path = tmp_path / "payload.bin"
    path.write_bytes(PAYLOAD)
    stream_reader = HashingReader(chunked(PAYLOAD, 71), size=len(PAYLOAD))
    drain(stream_reader, 512)
    with path.open("rb") as handle:
        file_reader = HashingFileReader(handle, size=len(PAYLOAD))
        drain(file_reader, 512)
    assert stream_reader.hexdigest == file_reader.hexdigest
    assert stream_reader.blob_id == file_reader.blob_id
