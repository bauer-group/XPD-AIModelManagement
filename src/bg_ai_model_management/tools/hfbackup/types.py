"""Shared value types for the hf-backup tool. No behaviour, no I/O."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol


class RepoType(str, Enum):
    models = "models"
    datasets = "datasets"


#: aimm uses the plural (URL-shaped) form; huggingface_hub wants the singular.
HF_REPO_TYPE: dict[RepoType, str] = {
    RepoType.models: "model",
    RepoType.datasets: "dataset",
}


class TransferMode(str, Enum):
    auto = "auto"
    stream = "stream"
    disk = "disk"


class TransferPath(str, Enum):
    inline = "inline"
    stream = "stream"
    disk = "disk"


class VerifyLevel(str, Enum):
    quick = "quick"
    deep = "deep"
    upstream = "upstream"


class VerifyStatus(str, Enum):
    ok = "ok"
    incomplete = "incomplete"
    drift = "drift"
    corrupt = "corrupt"


class RecheckMode(str, Enum):
    none = "none"
    head = "head"
    deep = "deep"


class SourceKind(str, Enum):
    """Which upstream hub a repository is mirrored from."""

    huggingface = "huggingface"
    modelscope = "modelscope"


#: Label recorded in the manifest for a digest the hub itself attested, per hub. A
#: digest computed locally is always "computed" — see `Manifest.sha256_source`.
ATTESTED_SHA256_SOURCE: dict[SourceKind, str] = {
    SourceKind.huggingface: "hf-lfs",
    SourceKind.modelscope: "modelscope",
}


class ByteReader(Protocol):
    """Minimal binary reader. `read` may return fewer than `n` bytes; b"" means EOF."""

    def read(self, n: int, /) -> bytes: ...


@dataclass(frozen=True, slots=True)
class RepoRef:
    repo_id: str
    repo_type: RepoType
    revision: str


@dataclass(frozen=True, slots=True)
class PinnedRepo:
    repo_id: str
    repo_type: RepoType
    revision_requested: str
    commit_sha: str  # full 40-char hex


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: str  # POSIX, repo-relative
    size: int
    blob_id: str
    sha256: str | None  # HF LFS sha256; None for non-LFS files
    xet_hash: str | None
    is_lfs: bool


@dataclass(frozen=True, slots=True)
class UploadResult:
    key: str
    size: int
    etag: str  # normalised: surrounding double quotes stripped
    part_size: int | None  # None for single PutObject
    parts: int


@dataclass(frozen=True, slots=True)
class ObjectHead:
    key: str
    size: int
    etag: str
    metadata: dict[str, str]  # keys are lowercase, x-amz-meta- prefix stripped
    storage_class: str | None
    last_modified: datetime | None
    parts_count: int | None


@dataclass(frozen=True, slots=True)
class ObjectSummary:
    key: str
    size: int
    etag: str
    last_modified: datetime


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    request_checksum_calculation: str  # "when_supported" | "when_required"
    addressing_style: str  # "path" | "virtual"
    supports_sha256_checksum: bool
    supports_get_object_attributes: bool
    probed: bool


@dataclass(frozen=True, slots=True)
class FileResult:
    path: str
    key: str
    size: int
    sha256: str
    sha256_source: str  # "hf-lfs" | "computed"
    blob_id: str
    xet_hash: str | None
    is_lfs: bool
    etag: str
    part_size: int | None
    parts: int
    transfer_path: TransferPath
    skipped: bool
    uploaded_at: str  # RFC 3339 UTC, trailing Z


class Source(Protocol):
    """Read-only access to one upstream repository, pinned to an immutable commit.

    This is the entire contract the engine needs. Implemented by `HubSource`
    (huggingface.co) and `ModelScopeSource` (modelscope.cn); a third hub is a third
    implementation and nothing else.

    Two rules bind every implementation:

    **Pin before you enumerate.** `pin` resolves a moving ref to a commit SHA once, and
    every later call is made against that SHA. Listing against `main` and transferring
    against `main` in two separate calls lets a push in between produce a torn snapshot:
    file list from state A, bytes from state B, silently and with no error anywhere.

    **Report an anchor the engine can check.** For a file with `is_lfs` set, the engine
    verifies the stored bytes against `SourceFile.sha256`; otherwise against
    `SourceFile.blob_id` as a git blob id. A hub that attests a content sha256 for
    *every* file therefore reports `is_lfs=True` throughout — that selects the digest
    the hub actually vouches for, which is what the flag decides here.
    """

    #: Which hub this is. Selects the manifest's `sha256_source` label, so a stored
    #: revision records where its digests came from.
    kind: SourceKind

    def pin(self, ref: RepoRef) -> PinnedRepo:
        """Resolve a ref to an immutable commit SHA."""
        ...

    def list_files(self, pinned: PinnedRepo) -> list[SourceFile]:
        """Enumerate every file at the pinned commit, sorted by path."""
        ...

    def open_stream(
        self, pinned: PinnedRepo, file: SourceFile
    ) -> AbstractContextManager[Iterator[bytes]]:
        """Yield an iterator of byte chunks for one file, without touching disk."""
        ...

    def read_bytes(self, pinned: PinnedRepo, file: SourceFile) -> bytes:
        """Read a whole small file into memory."""
        ...

    def staged(
        self, pinned: PinnedRepo, file: SourceFile, staging_dir: Path
    ) -> AbstractContextManager[Path]:
        """Download one file into an isolated directory and yield the payload path."""
        ...

    def whoami(self) -> str | None:
        """The authenticated user name, or None when unauthenticated."""
        ...
