"""Orchestration of sync, verify and restore across a worker pool.

This layer is deliberately UI-free: it imports no `rich` at runtime and prints nothing,
so tests and a future scheduler can drive it exactly as the CLI does. Progress is
reported through `Engine.progress_hook`; rendering is the caller's business.

Two invariants shape the whole module:

* `manifest.json` is written **only** after every selected file succeeded, so its
  existence is the completeness marker and no state database is needed.
* A failed upload never re-downloads from Hugging Face. On the DISK path the bytes are
  already local, so the destination retries parts against the staged file. On the STREAM
  path a torn body cannot be rewound, so the file is re-dispatched — and the re-dispatch
  demotes it to DISK, after which the Hub download is paid exactly once.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import random
import tempfile
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from bg_ai_model_management import shutdown
from bg_ai_model_management.errors import (
    AimmError,
    ChecksumMismatchError,
    ConfigError,
    IntegrityError,
    ManifestError,
    ObjectNotFoundError,
    TransferError,
)
from bg_ai_model_management.integrity.hashing import (
    HashingReader,
    git_blob_id,
    hash_file,
    sha256_bytes,
)
from bg_ai_model_management.tools.hfbackup import keys
from bg_ai_model_management.tools.hfbackup.manifest import (
    Manifest,
    ManifestFileEntry,
    ManifestSelection,
    build_manifest,
    digest_line,
    verify_digest,
)
from bg_ai_model_management.tools.hfbackup.planner import (
    DiskBudget,
    StreamFailureTracker,
    choose_part_size,
    choose_path,
)
from bg_ai_model_management.tools.hfbackup.types import (
    ATTESTED_SHA256_SOURCE,
    FileResult,
    PinnedRepo,
    RecheckMode,
    RepoRef,
    RepoType,
    Source,
    SourceFile,
    TransferPath,
    UploadResult,
    VerifyLevel,
    VerifyStatus,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; engine must not import rich at runtime
    from rich.console import Console

    from bg_ai_model_management.config.models import Settings
    from bg_ai_model_management.tools.hfbackup.destination import S3Destination

log = logging.getLogger(__name__)

#: Called as hook(event, path, bytes_done). Events: "start", "done", "skip", "error".
ProgressHook = Callable[[str, str, int], None]

_SHA_LENGTH = 40
_PART_SUFFIX = ".aimm-part"


@dataclass(frozen=True, slots=True)
class SyncRequest:
    repos: tuple[RepoRef, ...]
    include: tuple[str, ...] = ("*",)
    exclude: tuple[str, ...] = ()
    recheck: RecheckMode = RecheckMode.head
    update_ref: bool = True
    dry_run: bool = False
    #: Abort multipart uploads under this repository older than this before
    #: transferring. None disables the sweep. See `Engine._sweep_stale_uploads`.
    abort_stale_after: timedelta | None = None


@dataclass(frozen=True, slots=True)
class RepoSyncReport:
    repo_id: str
    repo_type: RepoType
    commit_sha: str
    files_total: int
    files_transferred: int
    files_skipped: int
    bytes_transferred: int
    by_path: dict[str, int]  # TransferPath.value -> count
    errors: tuple[str, ...]
    manifest_key: str | None
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class SyncReport:
    run_id: str
    repos: tuple[RepoSyncReport, ...]
    ok: bool


@dataclass(frozen=True, slots=True)
class VerifyRequest:
    repo: RepoRef
    level: VerifyLevel = VerifyLevel.quick
    sample_percent: float = 100.0
    strict: bool = True


@dataclass(frozen=True, slots=True)
class VerifyFinding:
    path: str
    kind: str  # "missing" | "size" | "etag" | "sha256" | "upstream"
    expected: str
    actual: str


@dataclass(frozen=True, slots=True)
class VerifyReport:
    repo_id: str
    commit_sha: str
    status: VerifyStatus
    checked: int
    findings: tuple[VerifyFinding, ...]


@dataclass(frozen=True, slots=True)
class RestoreRequest:
    repo: RepoRef
    dest: Path
    include: tuple[str, ...] = ("*",)
    exclude: tuple[str, ...] = ()
    overwrite: bool = False
    verify_only: bool = False


@dataclass(frozen=True, slots=True)
class RestoreReport:
    repo_id: str
    commit_sha: str
    files: int
    bytes: int
    skipped: int
    duration_seconds: float


def selected(path: str, include: Sequence[str], exclude: Sequence[str]) -> bool:
    """Match a POSIX repo-relative path against include/exclude globs.

    An empty `include` means everything. Exclude always wins over include.
    """
    if any(fnmatch.fnmatch(path, pattern) for pattern in exclude):
        return False
    if not include:
        return True
    return any(fnmatch.fnmatch(path, pattern) for pattern in include)


def check_expected(file: SourceFile, sha256: str, blob_id: str) -> None:
    """Compare observed digests against Hugging Face's own values.

    The branch is load-bearing and live-verified. For an LFS file, `lfs.sha256` is the
    sha256 of the CONTENT. For a non-LFS file there is no upstream sha256, but `blob_id`
    is `sha1(b"blob %d\\0" % size + content)` and matches exactly. For an LFS file
    `blob_id` is the sha1 of the LFS *pointer*, so checking it would fail on every large
    file in every repository.

    Raises:
        ChecksumMismatchError: the stored bytes are not what Hugging Face attests.
    """
    if file.is_lfs:
        if file.sha256 is None:
            # Nothing upstream to compare against, so the stored digest is purely local.
            # `_file_result` records `sha256_source="computed"` for exactly this case;
            # claiming "hf-lfs" would assert a provenance that was never established.
            log.warning(
                "%s: the Hub reported an LFS file with no sha256; the stored digest is "
                "computed locally and is not confirmed against Hugging Face",
                file.path,
            )
        elif sha256 != file.sha256:
            raise ChecksumMismatchError(
                f"{file.path}: sha256 mismatch against Hugging Face LFS metadata "
                f"(expected {file.sha256}, computed {sha256})"
            )
    elif blob_id != file.blob_id:
        raise ChecksumMismatchError(
            f"{file.path}: git blob id mismatch against Hugging Face "
            f"(expected {file.blob_id}, computed {blob_id})"
        )


class Engine:
    """Drives sync / verify / restore. Owns no UI and no process-level state."""

    def __init__(
        self,
        source: Source,
        destination: S3Destination,
        settings: Settings,
        *,
        console: Console | None = None,
        run_id: str = "",
        tool_version: str = "",
    ) -> None:
        self.source = source
        self.destination = destination
        self.settings = settings
        #: Kept for callers that render progress; this layer never draws on it.
        self.console = console
        self.run_id = run_id
        self.tool_version = tool_version
        #: Optional UI seam. The CLI wires a rich Progress to it; tests wire a list.
        self.progress_hook: ProgressHook | None = None

    # ---------------------------------------------------------------- sync

    def sync(self, req: SyncRequest) -> SyncReport:
        """Back up every requested repository at its pinned commit."""
        reports = tuple(self._sync_repo(ref, req) for ref in req.repos)
        return SyncReport(
            run_id=self.run_id,
            repos=reports,
            ok=all(not report.errors for report in reports),
        )

    def _sync_repo(self, ref: RepoRef, req: SyncRequest) -> RepoSyncReport:
        started = time.monotonic()
        transfer = self.settings.transfer
        pinned = self.source.pin(ref)
        files = [
            file
            for file in self.source.list_files(pinned)
            if selected(file.path, req.include, req.exclude)
        ]
        if req.dry_run:
            # "Transfer no bytes and write nothing" includes the staging directory, so
            # the budget is read against the nearest EXISTING ancestor instead. Creating
            # it here would also turn a mistyped --staging-dir into an OSError abort
            # where the operator asked for a plan.
            budget = DiskBudget.from_settings(transfer, _nearest_existing(self._staging_path()))
            return self._plan_report(pinned, files, budget, started)

        self._sweep_stale_uploads(ref, req)
        staging_root = self._staging_root()
        budget = DiskBudget.from_settings(transfer, staging_root)
        tracker = StreamFailureTracker()
        existing = self._read_manifest(ref.repo_type, ref.repo_id, pinned.commit_sha, strict=False)
        index = existing.index() if existing is not None else {}
        results: list[FileResult] = []
        errors: list[str] = []

        log.info(
            "syncing %s/%s at %s: %d file(s) selected",
            ref.repo_type.value,
            ref.repo_id,
            pinned.commit_sha,
            len(files),
        )
        with ThreadPoolExecutor(max_workers=transfer.workers) as pool:
            futures: dict[Future[FileResult], SourceFile] = {
                pool.submit(
                    self._transfer_one, pinned, file, index, req, budget, tracker, staging_root
                ): file
                for file in files
            }
            for future in as_completed(futures):
                file = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    errors.append(f"{file.path}: {type(exc).__name__}: {exc}")
                    log.error("transfer failed for %s: %s", file.path, exc)
                    self._emit("error", file.path, 0)
                    if transfer.fail_fast:
                        for pending in futures:
                            pending.cancel()
                        break
                if shutdown.is_requested():
                    # Stop handing out new work; the pool still drains what is running,
                    # and those parts abort themselves at the next part boundary.
                    for pending in futures:
                        pending.cancel()
                    break

        # A cancelled run is not a failed one, and it must not look like a complete
        # revision: raising here skips the manifest, so the next run re-plans this
        # repository from scratch rather than trusting a half-written record.
        shutdown.raise_if_requested(f"sync of {ref.repo_id}")

        manifest_key: str | None = None
        if errors:
            log.error(
                "%s: %d file(s) failed; no manifest written, the revision stays incomplete",
                ref.repo_id,
                len(errors),
            )
        else:
            manifest_key = self._commit_manifest(pinned, results, req, existing)

        by_path: dict[str, int] = {}
        for result in results:
            by_path[result.transfer_path.value] = by_path.get(result.transfer_path.value, 0) + 1
        return RepoSyncReport(
            repo_id=ref.repo_id,
            repo_type=ref.repo_type,
            commit_sha=pinned.commit_sha,
            files_total=len(files),
            files_transferred=sum(1 for result in results if not result.skipped),
            files_skipped=sum(1 for result in results if result.skipped),
            bytes_transferred=sum(result.size for result in results if not result.skipped),
            by_path=by_path,
            errors=tuple(errors),
            manifest_key=manifest_key,
            duration_seconds=time.monotonic() - started,
        )

    def _plan_report(
        self,
        pinned: PinnedRepo,
        files: Sequence[SourceFile],
        budget: DiskBudget,
        started: float,
    ) -> RepoSyncReport:
        """Describe what a real run would do, without transferring a byte."""
        by_path: dict[str, int] = {}
        for file in files:
            path = self._choose_path(file, budget, stream_failures=0)
            by_path[path.value] = by_path.get(path.value, 0) + 1
        return RepoSyncReport(
            repo_id=pinned.repo_id,
            repo_type=pinned.repo_type,
            commit_sha=pinned.commit_sha,
            files_total=len(files),
            files_transferred=0,
            files_skipped=0,
            bytes_transferred=sum(file.size for file in files),
            by_path=by_path,
            errors=(),
            manifest_key=None,
            duration_seconds=time.monotonic() - started,
        )

    def _sweep_stale_uploads(self, ref: RepoRef, req: SyncRequest) -> None:
        """Abort this repository's abandoned multipart uploads before transferring.

        An upload orphaned by a hard kill occupies storage indefinitely and appears
        in no object listing, so nothing else notices it. Every run therefore clears
        the debris of the previous one, which is the only cleanup that happens on
        its own — `prune --abort-older-than` needs an operator, and a bucket
        lifecycle rule needs a bucket owner.

        The age threshold is what makes this safe next to a concurrent run: an
        upload lives only as long as ONE file transfer, so anything older by hours
        is provably abandoned. It is scoped to this repository's key root, so two
        catalogs under different prefixes never touch each other's uploads.

        Never fails the backup. Listing multipart uploads is a separate S3
        permission, and housekeeping that cannot run is a warning, not a reason to
        stop mirroring.
        """
        if req.abort_stale_after is None:
            return
        prefix = keys.repo_root(self.settings.s3.prefix, ref.repo_type, ref.repo_id) + "/"
        try:
            aborted = self.destination.abort_stale_uploads(
                prefix, req.abort_stale_after, now=datetime.now(UTC)
            )
        except AimmError as exc:
            log.warning("could not sweep stale uploads under %s: %s", prefix, exc)
            return
        if aborted:
            log.info("aborted %d stale multipart upload(s) under %s", aborted, prefix)

    def _transfer_one(
        self,
        pinned: PinnedRepo,
        file: SourceFile,
        index: dict[str, ManifestFileEntry],
        req: SyncRequest,
        budget: DiskBudget,
        tracker: StreamFailureTracker,
        staging_root: Path,
    ) -> FileResult:
        """Transfer one file, choosing and if necessary downgrading its path."""
        # Queued work must not start a fresh download once shutdown was requested;
        # the pool holds one future per file and drains them all otherwise.
        shutdown.raise_if_requested(f"transfer of {file.path}")
        key = keys.file_key(
            self.settings.s3.prefix,
            pinned.repo_type,
            pinned.repo_id,
            pinned.commit_sha,
            file.path,
        )
        skipped = self._maybe_skip(file, key, index, req.recheck)
        if skipped is not None:
            self._emit("skip", file.path, file.size)
            return skipped

        metadata = self._base_metadata(pinned)
        self._emit("start", file.path, 0)
        attempts = self.settings.transfer.stream_failure_downgrade + 1
        last_exc: BaseException | None = None
        for _ in range(attempts):
            path = self._choose_path(file, budget, stream_failures=tracker.count(file.path))
            try:
                return self._run_path(path, pinned, file, key, metadata, budget, staging_root)
            except IntegrityError:
                # Bad bytes are not a transport problem; a retry cannot fix them.
                raise
            except Exception as exc:
                if path is not TransferPath.stream:
                    raise
                last_exc = exc
                count = tracker.record(file.path)
                log.warning(
                    "stream transfer of %s failed (%s: %s); re-dispatching, failure %d",
                    file.path,
                    type(exc).__name__,
                    exc,
                    count,
                )
        # Reachable only under `--mode stream`, where the STREAM -> DISK downgrade is
        # deliberately disabled, so the loop above is a plain bounded retry. The cause
        # MUST be chained: the raise sits outside the `except` block, so without
        # `from last_exc` the real RepoGatedError / ConnectError is gone from both the
        # traceback and RepoSyncReport.errors, and the operator is left with a message
        # about a setting that is not the problem.
        raise TransferError(f"{file.path}: all {attempts} stream attempts failed") from last_exc

    def _run_path(
        self,
        path: TransferPath,
        pinned: PinnedRepo,
        file: SourceFile,
        key: str,
        metadata: dict[str, str],
        budget: DiskBudget,
        staging_root: Path,
    ) -> FileResult:
        transfer = self.settings.transfer
        if path is TransferPath.inline:
            # The bytes are already in hand, so verify BEFORE uploading.
            data = self.source.read_bytes(pinned, file)
            digest = sha256_bytes(data)
            check_expected(file, digest, git_blob_id([data], file.size))
            upload = self.destination.put_small(key, data, sha256=digest, metadata=metadata)

        elif path is TransferPath.stream:
            # Single pass over the body: verification is only possible after the upload.
            part_size = choose_part_size(
                file.size, transfer.part_size, max_part_memory=transfer.max_part_memory
            )
            with self.source.open_stream(pinned, file) as chunks:
                reader = HashingReader(chunks, size=file.size)
                upload = self.destination.upload_multipart(
                    key, reader, size=file.size, part_size=part_size, metadata=metadata
                )
            try:
                check_expected(file, reader.hexdigest, reader.blob_id)
            except ChecksumMismatchError:
                # Never leave a bad object behind for a later restore to trust.
                self.destination.delete_keys([key])
                raise
            digest = reader.hexdigest

        else:
            with budget.reserve(file.size), self.source.staged(pinned, file, staging_root) as local:
                # The bytes are on local disk, so a bad file costs no upload bandwidth.
                digest, blob = hash_file(local, size=file.size)
                check_expected(file, digest, blob)
                part_size = choose_part_size(
                    file.size, transfer.part_size, max_part_memory=transfer.max_part_memory
                )
                with local.open("rb") as handle:
                    upload = self.destination.upload_multipart(
                        key,
                        handle,
                        size=file.size,
                        part_size=part_size,
                        metadata=metadata,
                    )

        self._emit("done", file.path, file.size)
        return self._file_result(file, key, digest, upload, path)

    def _file_result(
        self,
        file: SourceFile,
        key: str,
        digest: str,
        upload: UploadResult,
        path: TransferPath,
    ) -> FileResult:
        return FileResult(
            path=file.path,
            key=key,
            size=file.size,
            sha256=digest,
            # "hf-lfs" asserts the digest was CONFIRMED against Hugging Face's own
            # value. An LFS file whose `lfs.sha256` is absent was compared to nothing,
            # so it is "computed" like any non-LFS file.
            # Which hub vouched for this digest, or "computed" when only this tool
            # hashed the bytes — the manifest must not claim a provenance it lacks.
            sha256_source=(
                ATTESTED_SHA256_SOURCE[self.source.kind]
                if file.is_lfs and file.sha256 is not None
                else "computed"
            ),
            blob_id=file.blob_id,
            xet_hash=file.xet_hash,
            is_lfs=file.is_lfs,
            etag=upload.etag,
            part_size=upload.part_size,
            parts=upload.parts,
            transfer_path=path,
            skipped=False,
            uploaded_at=_now(),
        )

    def _maybe_skip(
        self,
        file: SourceFile,
        key: str,
        index: dict[str, ManifestFileEntry],
        recheck: RecheckMode,
    ) -> FileResult | None:
        """Decide whether a previously stored file may be skipped.

        Never size-only: the manifest entry must agree with upstream on the digest and
        the size, and the destination must additionally confirm the object (`head`) or
        its content (`deep`).
        """
        if recheck is RecheckMode.none:
            return None
        entry = index.get(file.path)
        if entry is None:
            return None
        if entry.sha256 != (file.sha256 or entry.sha256) or entry.size != file.size:
            return None

        if recheck is RecheckMode.head:
            head = self.destination.head(key)
            if head is None or head.size != file.size or head.etag != entry.s3_etag:
                return None
        else:
            digest = hashlib.sha256()
            with self.destination.get_stream(key) as chunks:
                for chunk in chunks:
                    digest.update(chunk)
            if digest.hexdigest() != entry.sha256:
                return None

        return FileResult(
            path=entry.path,
            key=entry.key,
            size=entry.size,
            sha256=entry.sha256,
            sha256_source=entry.sha256_source,
            blob_id=entry.blob_id,
            xet_hash=entry.xet_hash,
            is_lfs=entry.lfs,
            etag=entry.s3_etag,
            part_size=entry.s3_part_size,
            parts=entry.s3_parts,
            transfer_path=TransferPath(entry.transfer_path),
            skipped=True,
            uploaded_at=entry.uploaded_at,
        )

    def _commit_manifest(
        self,
        pinned: PinnedRepo,
        results: Sequence[FileResult],
        req: SyncRequest,
        existing: Manifest | None = None,
    ) -> str:
        """Write manifest.json, then its digest, then the ref. Order is normative.

        Entries of `existing` that this run did not select are carried forward, so a
        narrower re-sync at an already-stored commit can never shrink the recorded file
        set. Without that, `sync org/model --include '*.json'` after a full backup
        replaces a 340-entry manifest with a 4-entry one: the other 336 objects stay in
        the bucket referenced by nothing, `restore` hands back an unusable model, and
        `verify` and `catalog` both report the revision as healthy.
        """
        prefix = self.settings.s3.prefix
        key_root = keys.revision_root(prefix, pinned.repo_type, pinned.repo_id, pinned.commit_sha)
        carried = _carry_forward(existing, results)
        if carried:
            log.info(
                "%s at %s: carrying %d previously stored file(s) outside this run's "
                "selection into the manifest",
                pinned.repo_id,
                pinned.commit_sha,
                len(carried),
            )
        document = build_manifest(
            pinned=pinned,
            results=results,
            settings=self.settings,
            key_root=key_root,
            selection=_merged_selection(req, existing, carried),
            tool_version=self.tool_version,
            run_id=self.run_id,
            carried=carried,
        )
        data = document.to_json()
        manifest_key = keys.manifest_key(prefix, pinned.repo_type, pinned.repo_id, pinned.commit_sha)
        digest_key = keys.manifest_digest_key(
            prefix, pinned.repo_type, pinned.repo_id, pinned.commit_sha
        )
        self.destination.put_small(manifest_key, data, sha256=sha256_bytes(data))
        line = digest_line(data).encode("utf-8")
        self.destination.put_small(digest_key, line, sha256=sha256_bytes(line))
        if req.update_ref and not _is_commit_sha(pinned.revision_requested):
            self._write_ref(pinned)
        log.info("wrote manifest %s (%d files)", manifest_key, len(results))
        return manifest_key

    def _write_ref(self, pinned: PinnedRepo) -> None:
        payload = (
            json.dumps(
                {
                    "ref": pinned.revision_requested,
                    "repo_id": pinned.repo_id,
                    "repo_type": pinned.repo_type.value,
                    "commit_sha": pinned.commit_sha,
                    "updated_at": _now(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        key = keys.ref_key(
            self.settings.s3.prefix, pinned.repo_type, pinned.repo_id, pinned.revision_requested
        )
        self.destination.put_small(key, payload, sha256=sha256_bytes(payload))

    # -------------------------------------------------------------- verify

    def verify(self, req: VerifyRequest) -> VerifyReport:
        """Check what is stored against the manifest, and optionally against upstream."""
        commit_sha = self.resolve_revision(req.repo)
        document = self._read_manifest(
            req.repo.repo_type, req.repo.repo_id, commit_sha, strict=True
        )
        if document is None:
            log.warning("no manifest for %s at %s: the revision is incomplete", req.repo.repo_id, commit_sha)
            return VerifyReport(
                repo_id=req.repo.repo_id,
                commit_sha=commit_sha,
                status=VerifyStatus.incomplete,
                checked=0,
                findings=(),
            )

        entries = _sample(document.files, req.sample_percent, seed=commit_sha)
        findings: list[VerifyFinding] = []
        deep = req.level in (VerifyLevel.deep, VerifyLevel.upstream)
        if req.level is VerifyLevel.upstream:
            findings.extend(self._verify_upstream(req.repo, commit_sha, document))
        with ThreadPoolExecutor(max_workers=self.settings.transfer.workers) as pool:
            for batch in pool.map(lambda entry: self._verify_one(entry, deep=deep), entries):
                findings.extend(batch)

        kinds = {finding.kind for finding in findings}
        if kinds & {"sha256", "upstream"}:
            status = VerifyStatus.corrupt
        elif kinds:
            status = VerifyStatus.drift
        else:
            status = VerifyStatus.ok
        return VerifyReport(
            repo_id=req.repo.repo_id,
            commit_sha=commit_sha,
            status=status,
            checked=len(entries),
            findings=tuple(findings),
        )

    def _verify_one(self, entry: ManifestFileEntry, *, deep: bool) -> list[VerifyFinding]:
        head = self.destination.head(entry.key)
        if head is None:
            return [VerifyFinding(entry.path, "missing", entry.key, "absent")]
        findings: list[VerifyFinding] = []
        if head.size != entry.size:
            findings.append(VerifyFinding(entry.path, "size", str(entry.size), str(head.size)))
        if head.etag != entry.s3_etag:
            findings.append(VerifyFinding(entry.path, "etag", entry.s3_etag, head.etag))
        if deep:
            digest = hashlib.sha256()
            with self.destination.get_stream(entry.key) as chunks:
                for chunk in chunks:
                    digest.update(chunk)
            actual = digest.hexdigest()
            if actual != entry.sha256:
                findings.append(VerifyFinding(entry.path, "sha256", entry.sha256, actual))
        self._emit("done", entry.path, entry.size)
        return findings

    def _verify_upstream(
        self, repo: RepoRef, commit_sha: str, document: Manifest
    ) -> list[VerifyFinding]:
        """Re-fetch the Hub tree at the pinned SHA; the values MUST be identical."""
        pinned = PinnedRepo(
            repo_id=repo.repo_id,
            repo_type=repo.repo_type,
            revision_requested=repo.revision,
            commit_sha=commit_sha,
        )
        upstream = {file.path: file for file in self.source.list_files(pinned)}
        findings: list[VerifyFinding] = []
        for entry in document.files:
            file = upstream.get(entry.path)
            if file is None:
                findings.append(VerifyFinding(entry.path, "upstream", entry.path, "absent upstream"))
            elif file.size != entry.size:
                findings.append(
                    VerifyFinding(entry.path, "upstream", str(entry.size), str(file.size))
                )
            elif file.is_lfs and file.sha256 is not None and file.sha256 != entry.sha256:
                findings.append(VerifyFinding(entry.path, "upstream", entry.sha256, file.sha256))
            elif not file.is_lfs and file.blob_id != entry.blob_id:
                findings.append(VerifyFinding(entry.path, "upstream", entry.blob_id, file.blob_id))
        return findings

    # ------------------------------------------------------------- restore

    def restore(self, req: RestoreRequest) -> RestoreReport:
        """Materialise a stored revision on local disk. Never contacts the object's source."""
        started = time.monotonic()
        commit_sha = self.resolve_revision(req.repo)
        document = self._read_manifest(
            req.repo.repo_type, req.repo.repo_id, commit_sha, strict=True
        )
        if document is None:
            raise ManifestError(
                f"{req.repo.repo_id} at {commit_sha} has no manifest; the backup is "
                "incomplete and cannot be restored"
            )
        entries = [
            entry for entry in document.files if selected(entry.path, req.include, req.exclude)
        ]
        written = 0
        with ThreadPoolExecutor(max_workers=self.settings.transfer.workers) as pool:
            for size in pool.map(lambda entry: self._restore_one(entry, req), entries):
                written += size
        return RestoreReport(
            repo_id=req.repo.repo_id,
            commit_sha=commit_sha,
            files=len(entries),
            bytes=written,
            skipped=len(document.files) - len(entries),
            duration_seconds=time.monotonic() - started,
        )

    def _restore_one(self, entry: ManifestFileEntry, req: RestoreRequest) -> int:
        target = keys.safe_local_path(req.dest, entry.path)
        digest = hashlib.sha256()

        if req.verify_only:
            with self.destination.get_stream(entry.key) as chunks:
                for chunk in chunks:
                    digest.update(chunk)
            self._assert_restored_digest(entry, digest.hexdigest(), None)
            self._emit("done", entry.path, entry.size)
            return entry.size

        if target.exists() and not req.overwrite:
            raise ConfigError(
                f"{target} already exists; pass --overwrite to replace existing files"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        # The token is load-bearing, not decoration. A repository may legitimately
        # contain both `X` and `X.aimm-part`; with a name derived purely from the target
        # the two workers restoring them write and rename through the SAME path, and one
        # file ends up holding the other's bytes while restore still reports success.
        partial = target.with_name(f"{target.name}.{uuid4().hex[:8]}{_PART_SUFFIX}")
        written = 0
        try:
            with partial.open("wb") as handle:
                with self.destination.get_stream(entry.key) as chunks:
                    for chunk in chunks:
                        digest.update(chunk)
                        handle.write(chunk)
                        written += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_restored_digest(entry, digest.hexdigest(), partial)
            partial.replace(target)
        except BaseException:
            # A dropped connection mid-stream would otherwise leave a part file of
            # arbitrary size in --dest that no later run ever cleans up. BaseException
            # because Ctrl-C leaks it identically.
            partial.unlink(missing_ok=True)
            raise
        self._emit("done", entry.path, written)
        return written

    def _assert_restored_digest(
        self, entry: ManifestFileEntry, actual: str, partial: Path | None
    ) -> None:
        if actual == entry.sha256:
            return
        if partial is not None:
            partial.unlink(missing_ok=True)
        raise ChecksumMismatchError(
            f"{entry.path}: restored bytes hash to {actual}, manifest says {entry.sha256}"
        )

    # --------------------------------------------------------------- misc

    def resolve_revision(self, repo: RepoRef) -> str:
        """Resolve a revision to a commit SHA using ONLY the object store.

        A 40-hex revision is used as is; anything else is read from
        `refs/<revision>.json`. Hugging Face is deliberately never consulted, not even
        as a fallback: `restore` and `verify` exist for the day the upstream repository
        is gone, and pinning against the Hub there fails with RepoNotFoundError while a
        perfectly intact copy sits in the bucket. A sync pinned by SHA (or run with
        `--no-update-ref`) writes no ref object, so this path is ordinary, not exotic.
        """
        if _is_commit_sha(repo.revision):
            return repo.revision.lower()
        key = keys.ref_key(self.settings.s3.prefix, repo.repo_type, repo.repo_id, repo.revision)
        try:
            raw = self.destination.get_bytes(key)
        except ObjectNotFoundError as exc:
            raise ConfigError(
                f"{repo.repo_id}: revision {repo.revision!r} has no stored ref, so it "
                "cannot be resolved without contacting Hugging Face. Pass a 40-hex "
                f"commit sha instead. Stored revisions: {self._stored_revisions(repo)}"
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
            commit_sha = payload["commit_sha"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ManifestError(f"ref object {key} is malformed: {exc}") from exc
        if not isinstance(commit_sha, str) or not _is_commit_sha(commit_sha):
            raise ManifestError(f"ref object {key} holds an invalid commit sha {commit_sha!r}")
        return commit_sha.lower()

    def _stored_revisions(self, repo: RepoRef, *, limit: int = 10) -> str:
        """Commit SHAs stored for this repository, for an actionable error message."""
        root = keys.revisions_prefix(self.settings.s3.prefix, repo.repo_type, repo.repo_id)
        shas = sorted(
            child[len(root) :].strip("/") for child in self.destination.list_prefixes(root)
        )
        if not shas:
            return "none"
        extra = len(shas) - limit
        listed = ", ".join(shas[:limit])
        return f"{listed}, ... (+{extra} more)" if extra > 0 else listed

    def _read_manifest(
        self, repo_type: RepoType, repo_id: str, commit_sha: str, *, strict: bool
    ) -> Manifest | None:
        """Read and digest-verify a manifest, or return None when it is absent.

        With `strict=False` (resume) an unreadable manifest is downgraded to "absent" and
        the run simply re-uploads; with `strict=True` (verify/restore) it is an error.
        """
        prefix = self.settings.s3.prefix
        manifest_key = keys.manifest_key(prefix, repo_type, repo_id, commit_sha)
        digest_key = keys.manifest_digest_key(prefix, repo_type, repo_id, commit_sha)
        try:
            data = self.destination.get_bytes(manifest_key)
        except ObjectNotFoundError:
            return None
        try:
            line = self.destination.get_bytes(digest_key).decode("utf-8")
            verify_digest(data, line)
            return Manifest.from_json(data)
        except (ObjectNotFoundError, ManifestError, UnicodeDecodeError) as exc:
            if strict:
                raise ManifestError(f"manifest {manifest_key} is unusable: {exc}") from exc
            log.warning("ignoring unusable manifest %s for resume: %s", manifest_key, exc)
            return None

    def _choose_path(
        self, file: SourceFile, budget: DiskBudget, *, stream_failures: int
    ) -> TransferPath:
        transfer = self.settings.transfer
        return choose_path(
            file,
            mode=transfer.mode,
            inline_max=transfer.inline_max,
            part_size=transfer.part_size,
            max_part_memory=transfer.max_part_memory,
            prefer_xet=transfer.prefer_xet,
            stream_failures=stream_failures,
            stream_failure_downgrade=transfer.stream_failure_downgrade,
            budget=budget,
        )

    def _staging_path(self) -> Path:
        """Where the DISK path would stage files. Creates nothing."""
        return self.settings.transfer.staging_dir or Path(tempfile.gettempdir()) / "aimm-staging"

    def _staging_root(self) -> Path:
        """The staging directory, created on demand. Never called on the dry-run path."""
        root = self._staging_path()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _base_metadata(self, pinned: PinnedRepo) -> dict[str, str]:
        """Object metadata. Keys are lowercase and values ASCII, or they do not round-trip."""
        return {
            "aimm-repo-id": _ascii(pinned.repo_id),
            "aimm-commit-sha": _ascii(pinned.commit_sha),
            "aimm-repo-type": pinned.repo_type.value,
        }

    def _emit(self, event: str, path: str, size: int) -> None:
        hook = self.progress_hook
        if hook is not None:
            hook(event, path, size)


def _carry_forward(
    existing: Manifest | None, results: Sequence[FileResult]
) -> list[ManifestFileEntry]:
    """Entries of a previous manifest for the SAME commit that this run did not cover.

    Safe by construction: `commit_sha` pins an immutable upstream file set, so a path
    outside this run's selection is still a genuine file of exactly this revision and
    its stored object cannot be a stale ghost.
    """
    if existing is None:
        return []
    covered = {result.path for result in results}
    return [entry for entry in existing.files if entry.path not in covered]


def _merged_selection(
    req: SyncRequest, existing: Manifest | None, carried: Sequence[ManifestFileEntry]
) -> ManifestSelection:
    """Record the selection the resulting manifest actually covers.

    When entries are carried forward the manifest spans both runs, so recording only
    this run's globs would make the field a lie. A union of the includes minus an
    intersection of the excludes is the tightest expressible superset of that union.
    """
    if existing is None or not carried:
        return ManifestSelection(include=list(req.include), exclude=list(req.exclude))
    return ManifestSelection(
        include=sorted(set(req.include) | set(existing.selection.include)),
        exclude=sorted(set(req.exclude) & set(existing.selection.exclude)),
    )


def _nearest_existing(path: Path) -> Path:
    """Closest existing ancestor of `path`; used for readings that must create nothing."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path(tempfile.gettempdir())


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_commit_sha(value: str) -> bool:
    return len(value) == _SHA_LENGTH and all(char in "0123456789abcdefABCDEF" for char in value)


def _ascii(value: str) -> str:
    return value.encode("ascii", "replace").decode("ascii")


def _sample(
    entries: Sequence[ManifestFileEntry], percent: float, *, seed: str
) -> list[ManifestFileEntry]:
    """Pick a deterministic subset of entries; the commit SHA seeds the choice."""
    if percent >= 100.0 or not entries:
        return list(entries)
    count = max(1, round(len(entries) * percent / 100.0))
    picked = random.Random(seed).sample(list(entries), count)
    return sorted(picked, key=lambda entry: entry.path)


__all__ = [
    "Engine",
    "ProgressHook",
    "RepoSyncReport",
    "RestoreReport",
    "RestoreRequest",
    "SyncReport",
    "SyncRequest",
    "VerifyFinding",
    "VerifyReport",
    "VerifyRequest",
    "check_expected",
    "selected",
]
