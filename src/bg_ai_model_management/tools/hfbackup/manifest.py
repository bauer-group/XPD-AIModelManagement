"""The backup manifest: pydantic models plus deterministic serialisation.

Pure module — it performs no S3 I/O. The engine owns reading and writing; this module
only knows how a manifest is shaped, how it turns into bytes, and how its digest is
formed. Serialisation is byte-for-byte deterministic because `manifest.json.sha256`
covers exactly these bytes.

**Completeness invariant.** `manifest.json` is written only after every selected file has
succeeded, so its mere existence marks the revision as complete. There is no state
database: an interrupted run leaves objects under `files/` and no manifest, which
`verify` reports as INCOMPLETE, `sync` resumes from, and `prune` can remove.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from bg_ai_model_management.errors import ManifestError
from bg_ai_model_management.integrity.hashing import sha256_bytes
from bg_ai_model_management.tools.hfbackup.types import FileResult, PinnedRepo

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module free of config imports
    from bg_ai_model_management.config.models import Settings

MANIFEST_VERSION: int = 1
MANIFEST_FILENAME: str = "manifest.json"


class ManifestFileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    key: str
    size: int
    sha256: str
    sha256_source: Literal["hf-lfs", "computed"]
    blob_id: str
    xet_hash: str | None = None
    lfs: bool
    s3_etag: str
    s3_part_size: int | None = None
    s3_parts: int
    transfer_path: Literal["inline", "stream", "disk"]
    uploaded_at: str


class ManifestSource(BaseModel):
    provider: Literal["huggingface"] = "huggingface"
    endpoint: str
    repo_type: Literal["models", "datasets"]
    repo_id: str
    revision_requested: str
    commit_sha: str


class ManifestDestination(BaseModel):
    backend: str
    endpoint_url: str | None = None
    region: str
    bucket: str
    prefix: str
    key_root: str


class ManifestSelection(BaseModel):
    include: list[str] = ["*"]
    exclude: list[str] = []


class ManifestTotals(BaseModel):
    files: int
    bytes: int
    transferred: int
    skipped: int


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_version: int = MANIFEST_VERSION
    tool: Literal["aimm"] = "aimm"
    tool_version: str
    created_at: str
    run_id: str
    source: ManifestSource
    destination: ManifestDestination
    selection: ManifestSelection
    totals: ManifestTotals
    files: list[ManifestFileEntry]

    def to_json(self) -> bytes:
        """Serialise to deterministic UTF-8 JSON with a trailing newline.

        Keys are sorted within every object and `files` is sorted by path, so two runs
        producing the same content produce the same bytes — which is what makes the
        sha256 sidecar meaningful.
        """
        data: dict[str, Any] = self.model_dump(mode="json")
        entries: list[dict[str, Any]] = data["files"]
        data["files"] = sorted(entries, key=lambda entry: entry["path"])
        text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
        return (text + "\n").encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> Manifest:
        """Parse and validate manifest bytes.

        Raises:
            ManifestError: malformed JSON, wrong shape, or a manifest_version this build
                does not understand.
        """
        try:
            raw = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestError(f"manifest is not valid UTF-8 JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ManifestError("manifest root is not a JSON object")
        version = raw.get("manifest_version")
        if not isinstance(version, int) or version > MANIFEST_VERSION:
            raise ManifestError(
                f"unsupported manifest_version {version!r}; "
                f"this build understands up to {MANIFEST_VERSION}"
            )
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise ManifestError(f"manifest failed validation: {exc}") from exc

    def index(self) -> dict[str, ManifestFileEntry]:
        """Return a path -> entry mapping, used for resume lookups."""
        return {entry.path: entry for entry in self.files}


def digest_line(data: bytes) -> str:
    """Return a sha256sum-compatible line covering `data`."""
    return f"{sha256_bytes(data)}  {MANIFEST_FILENAME}\n"


def verify_digest(data: bytes, line: str) -> None:
    """Check manifest bytes against a `digest_line`.

    Raises:
        ManifestError: the sidecar is empty or the digest does not match.
    """
    fields = line.strip().split(maxsplit=1)
    if not fields:
        raise ManifestError("manifest digest file is empty")
    expected = fields[0].lower()
    actual = sha256_bytes(data)
    if expected != actual:
        raise ManifestError(
            f"manifest digest mismatch: sidecar says {expected}, content hashes to {actual}"
        )


def build_manifest(
    *,
    pinned: PinnedRepo,
    results: Sequence[FileResult],
    settings: Settings,
    key_root: str,
    selection: ManifestSelection,
    tool_version: str,
    run_id: str,
    carried: Sequence[ManifestFileEntry] = (),
) -> Manifest:
    """Assemble a manifest from completed file results. Main thread only.

    Args:
        carried: Entries of a previous manifest for the same commit that this run did
            not select. They count towards `totals.files` and `totals.bytes` — which
            describe the manifest — but towards neither `transferred` nor `skipped`,
            which describe this run's work. `transferred + skipped < files` is therefore
            the signature of a narrower re-sync, not an inconsistency.
    """
    entries = [
        ManifestFileEntry(
            path=result.path,
            key=result.key,
            size=result.size,
            sha256=result.sha256,
            sha256_source="hf-lfs" if result.sha256_source == "hf-lfs" else "computed",
            blob_id=result.blob_id,
            xet_hash=result.xet_hash,
            lfs=result.is_lfs,
            s3_etag=result.etag,
            s3_part_size=result.part_size,
            s3_parts=result.parts,
            transfer_path=result.transfer_path.value,
            uploaded_at=result.uploaded_at,
        )
        for result in results
    ]
    entries.extend(carried)
    entries.sort(key=lambda entry: entry.path)
    totals = ManifestTotals(
        files=len(entries),
        bytes=sum(entry.size for entry in entries),
        transferred=sum(1 for result in results if not result.skipped),
        skipped=sum(1 for result in results if result.skipped),
    )
    return Manifest(
        tool_version=tool_version,
        created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        run_id=run_id,
        source=ManifestSource(
            endpoint=settings.hub.endpoint,
            repo_type=pinned.repo_type.value,
            repo_id=pinned.repo_id,
            revision_requested=pinned.revision_requested,
            commit_sha=pinned.commit_sha,
        ),
        destination=ManifestDestination(
            backend=settings.s3.preset.value,
            endpoint_url=settings.s3.endpoint_url,
            region=settings.s3.region,
            bucket=settings.s3.bucket,
            prefix=settings.s3.prefix,
            key_root=key_root,
        ),
        selection=selection,
        totals=totals,
        files=entries,
    )
