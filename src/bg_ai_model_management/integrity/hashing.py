"""Streamed digests: sha256, git blob ids and S3 composite ETags.

Everything here is constant-memory. The hashing readers compute sha256 **and** the git
blob id in a single pass, because the two verification branches (LFS files carry an
authoritative ``lfs.sha256``, non-LFS files carry an authoritative ``blob_id``) need
different digests and a 50 GB stream can only be read once.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import BinaryIO

CHUNK_SIZE: int = 1 << 20  # 1 MiB


def _blob_hasher(size: int) -> hashlib._Hash:
    """A sha1 primed with the git blob header. Not a security digest."""
    hasher = hashlib.sha1(usedforsecurity=False)
    hasher.update(b"blob %d\0" % size)
    return hasher


def sha256_bytes(data: bytes) -> str:
    """Return the lowercase hex sha256 of an in-memory buffer."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = CHUNK_SIZE) -> str:
    """Streamed sha256 of a file. Constant memory. Returns lowercase hex."""
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def hash_file(path: Path, *, size: int, chunk_size: int = CHUNK_SIZE) -> tuple[str, str]:
    """Return ``(sha256_hex, git_blob_id_hex)`` computed in ONE pass over the file."""
    with open(path, "rb") as fh:
        reader = HashingFileReader(fh, size=size)
        while reader.read(chunk_size):
            pass
    return reader.hexdigest, reader.blob_id


def git_blob_id(chunks: Iterable[bytes], size: int) -> str:
    """Git blob object id: ``sha1(b'blob %d\\0' % size + content)``. Lowercase hex.

    This is the only content-derived identifier Hugging Face exposes for non-LFS files.
    """
    hasher = _blob_hasher(size)
    for chunk in chunks:
        hasher.update(chunk)
    return hasher.hexdigest()


def composite_etag(part_md5_digests: Sequence[bytes], *, quoted: bool = False) -> str:
    """Recompute an S3 multipart ETag: ``md5(concat(part digests)).hex + '-' + N``."""
    hasher = hashlib.md5(usedforsecurity=False)
    for digest in part_md5_digests:
        hasher.update(digest)
    etag = f"{hasher.hexdigest()}-{len(part_md5_digests)}"
    return f'"{etag}"' if quoted else etag


def sha256_b64(hex_digest: str) -> str:
    """Convert a hex sha256 to the base64 form S3's ``ChecksumSHA256`` header expects."""
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


class HashingReader:
    """Adapts an iterator of byte chunks to the ByteReader protocol while hashing.

    Computes sha256 AND the git blob id in the SAME pass. The git-blob hasher is primed
    with ``b'blob %d\\0' % size`` at construction. Buffers at most one source chunk beyond
    what the caller asked for.
    """

    def __init__(self, chunks: Iterator[bytes], *, size: int) -> None:
        self._chunks = chunks
        self._buffer = bytearray()
        self._sha256 = hashlib.sha256()
        self._blob = _blob_hasher(size)
        self._bytes_read = 0

    def read(self, n: int, /) -> bytes:
        """Return up to ``n`` bytes; ``b''`` at EOF. Short reads are legal; callers loop."""
        if n <= 0:
            return b""
        while not self._buffer:
            chunk = next(self._chunks, None)
            if chunk is None:
                return b""
            if not chunk:
                continue  # an empty intermediate chunk is not EOF
            self._sha256.update(chunk)
            self._blob.update(chunk)
            self._bytes_read += len(chunk)
            self._buffer.extend(chunk)
        out = bytes(self._buffer[:n])
        del self._buffer[:n]
        return out

    @property
    def hexdigest(self) -> str:
        """sha256 of everything consumed so far. Valid at any time; final at EOF."""
        return self._sha256.hexdigest()

    @property
    def blob_id(self) -> str:
        """Git blob id of everything consumed so far. Only meaningful once EOF is reached."""
        return self._blob.hexdigest()

    @property
    def bytes_read(self) -> int:
        return self._bytes_read


class HashingFileReader:
    """Same protocol over an open binary file, so the DISK path shares one code path."""

    def __init__(self, fh: BinaryIO, *, size: int) -> None:
        self._fh = fh
        self._sha256 = hashlib.sha256()
        self._blob = _blob_hasher(size)
        self._bytes_read = 0

    def read(self, n: int, /) -> bytes:
        if n <= 0:
            return b""
        data = self._fh.read(n)
        if data:
            self._sha256.update(data)
            self._blob.update(data)
            self._bytes_read += len(data)
        return data

    @property
    def hexdigest(self) -> str:
        return self._sha256.hexdigest()

    @property
    def blob_id(self) -> str:
        return self._blob.hexdigest()

    @property
    def bytes_read(self) -> int:
        return self._bytes_read
