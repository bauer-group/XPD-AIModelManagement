"""Shared value types for the hf-backup tool. No behaviour, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
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
