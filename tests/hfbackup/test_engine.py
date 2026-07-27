"""Tests for the engine: selection, digest branching, resume, and the two hard invariants.

Two properties in this layer are worth more than the rest of the module combined, and
both get a dedicated section below:

* **`manifest.json` appears only after every file succeeded.** Its presence is the sole
  completeness marker — there is no state database — so a manifest written after a partial
  run would permanently mislabel a torn revision as good.
* **A failed *upload* never re-downloads from Hugging Face.** The retry unit is the S3
  part, not the file. A regression here turns one flaky part into a fresh multi-gigabyte
  Hub download, which is the defect this design exists to prevent.

The Hugging Face side is a counting fake; the S3 side is real moto, so what the engine
writes is inspected as actual objects in an actual bucket.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from bg_ai_model_management import shutdown
from bg_ai_model_management.config.models import Settings
from bg_ai_model_management.errors import (
    ChecksumMismatchError,
    ConfigError,
    ManifestError,
    OperationCancelledError,
    SourceError,
    TransferError,
)
from bg_ai_model_management.integrity.hashing import git_blob_id, sha256_bytes
from bg_ai_model_management.tools.hfbackup import keys
from bg_ai_model_management.tools.hfbackup.destination import S3Destination
from bg_ai_model_management.tools.hfbackup.engine import (
    Engine,
    RestoreRequest,
    SyncRequest,
    VerifyRequest,
    check_expected,
    selected,
)
from bg_ai_model_management.tools.hfbackup.manifest import Manifest
from bg_ai_model_management.tools.hfbackup.planner import DiskBudget, StreamFailureTracker
from bg_ai_model_management.tools.hfbackup.types import (
    PinnedRepo,
    RecheckMode,
    RepoRef,
    RepoType,
    SourceFile,
    SourceKind,
    TransferMode,
    VerifyLevel,
    VerifyStatus,
)

from .conftest import SpyClient

COMMIT = "1234567890abcdef" * 2 + "12345678"
REPO = "acme/model"
MIB = 1024**2


# ── the Hugging Face fake ────────────────────────────────────────────────────


class FakeSource:
    """A counting `HubSource` stand-in over an in-memory repository.

    Every Hub interaction is recorded, because most assertions in this module are about
    how *often* the Hub was touched rather than what it returned.
    """

    #: Part of the `Source` protocol: it selects the manifest's digest provenance.
    kind = SourceKind.huggingface

    def __init__(self, blobs: dict[str, bytes], *, lfs: set[str] | None = None) -> None:
        self.blobs = dict(blobs)
        self.lfs = lfs if lfs is not None else set(blobs)
        #: LFS paths the Hub reports WITHOUT an `lfs.sha256`, so there is no upstream
        #: value to confirm the stored digest against.
        self.no_upstream_sha256: set[str] = set()
        self.pin_calls = 0
        self.list_calls = 0
        self.stream_calls: list[str] = []
        self.read_calls: list[str] = []
        self.staged_calls: list[str] = []
        #: paths whose next transfer attempt should fail, and how many times.
        self.fail_stream: dict[str, int] = {}
        self.fail_all: dict[str, int] = {}
        self._lock = threading.Lock()

    # -- construction helpers

    def file(self, path: str) -> SourceFile:
        blob = self.blobs[path]
        is_lfs = path in self.lfs
        attested = is_lfs and path not in self.no_upstream_sha256
        return SourceFile(
            path=path,
            size=len(blob),
            blob_id=git_blob_id([blob], len(blob)),
            sha256=sha256_bytes(blob) if attested else None,
            xet_hash=None,
            is_lfs=is_lfs,
        )

    def _maybe_fail(self, table: dict[str, int], path: str) -> None:
        with self._lock:
            remaining = table.get(path, 0)
            if remaining > 0:
                table[path] = remaining - 1
                raise SourceError(f"simulated Hub failure for {path}")

    # -- the HubSource surface

    def pin(self, ref: RepoRef) -> PinnedRepo:
        self.pin_calls += 1
        return PinnedRepo(
            repo_id=ref.repo_id,
            repo_type=ref.repo_type,
            revision_requested=ref.revision,
            commit_sha=COMMIT,
        )

    def list_files(self, pinned: PinnedRepo) -> list[SourceFile]:
        self.list_calls += 1
        return [self.file(path) for path in sorted(self.blobs)]

    @contextmanager
    def open_stream(self, pinned: PinnedRepo, file: SourceFile) -> Iterator[Iterator[bytes]]:
        with self._lock:
            self.stream_calls.append(file.path)
        self._maybe_fail(self.fail_all, file.path)
        self._maybe_fail(self.fail_stream, file.path)
        blob = self.blobs[file.path]
        yield iter(blob[index : index + 8192] for index in range(0, len(blob), 8192))

    def read_bytes(self, pinned: PinnedRepo, file: SourceFile) -> bytes:
        with self._lock:
            self.read_calls.append(file.path)
        self._maybe_fail(self.fail_all, file.path)
        return self.blobs[file.path]

    @contextmanager
    def staged(self, pinned: PinnedRepo, file: SourceFile, staging_dir: Path) -> Iterator[Path]:
        with self._lock:
            self.staged_calls.append(file.path)
        self._maybe_fail(self.fail_all, file.path)
        target = staging_dir / f"stage-{len(self.staged_calls)}" / Path(file.path).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.blobs[file.path])
        try:
            yield target
        finally:
            target.unlink(missing_ok=True)

    @property
    def hub_fetches(self) -> int:
        """Every way this engine can pull bytes out of Hugging Face."""
        return len(self.stream_calls) + len(self.read_calls) + len(self.staged_calls)


@pytest.fixture
def blobs() -> dict[str, bytes]:
    return {
        "config.json": b'{"model_type": "llama"}',
        "tokenizer.json": b'{"version": "1.0"}' * 8,
        "model.safetensors": bytes(range(256)) * (6 * MIB // 256),
    }


@pytest.fixture
def source(blobs: dict[str, bytes]) -> FakeSource:
    return FakeSource(blobs)


@pytest.fixture
def engine(source: FakeSource, destination: S3Destination, settings: Settings) -> Engine:
    return Engine(
        source,  # type: ignore[arg-type]
        destination,
        settings,
        run_id="test-run",
        tool_version="0.1.0",
    )


def manifest_key(settings: Settings) -> str:
    return keys.manifest_key(settings.s3.prefix, RepoType.models, REPO, COMMIT)


def sync_request(**overrides: object) -> SyncRequest:
    base: dict[str, object] = {
        "repos": (RepoRef(repo_id=REPO, repo_type=RepoType.models, revision="main"),)
    }
    base.update(overrides)
    return SyncRequest(**base)  # type: ignore[arg-type]


# ── selection ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "include", "exclude", "expected"),
    [
        ("config.json", ["*"], [], True),
        ("config.json", [], [], True),
        ("config.json", ["*.json"], [], True),
        ("config.json", ["*.bin"], [], False),
        ("config.json", ["*.json", "*.bin"], [], True),
        ("model.bin", ["*.json", "*.bin"], [], True),
        # exclude always wins over include, however specific the include is.
        ("config.json", ["config.json"], ["*.json"], False),
        ("config.json", ["*"], ["config.json"], False),
        # fnmatch's '*' crosses '/', unlike a shell glob. Pinned deliberately: an operator
        # typing --include 'a/*' gets the whole subtree, which is surprising but is the
        # documented behaviour of the matcher this tool uses.
        ("a/b/c.bin", ["a/*"], [], True),
        ("a/b/c.bin", ["a/*/*"], [], True),
        ("a/b/c.bin", ["*"], ["*/b/*"], False),
        ("nested/deep/file.bin", ["*.bin"], [], True),
    ],
)
def test_selected_applies_multiple_globs_with_exclude_winning(
    path: str, include: list[str], exclude: list[str], expected: bool
) -> None:
    assert selected(path, include, exclude) is expected


def test_sync_honours_include_and_exclude(
    engine: Engine, source: FakeSource, destination: S3Destination, settings: Settings
) -> None:
    report = engine.sync(sync_request(include=("*.json",), exclude=("tokenizer*",)))

    assert report.repos[0].files_total == 1
    stored = {summary.key for summary in destination.list_keys(settings.s3.prefix)}
    assert any(key.endswith("files/config.json") for key in stored)
    assert not any(key.endswith("files/tokenizer.json") for key in stored)


# ── digest branching ─────────────────────────────────────────────────────────


def test_check_expected_uses_sha256_for_lfs_files() -> None:
    blob = b"weights"
    file = SourceFile(
        path="model.bin",
        size=len(blob),
        blob_id="does-not-matter",
        sha256=sha256_bytes(blob),
        xet_hash=None,
        is_lfs=True,
    )
    check_expected(file, sha256_bytes(blob), "an-unrelated-blob-id")

    with pytest.raises(ChecksumMismatchError, match="sha256 mismatch"):
        check_expected(file, sha256_bytes(b"tampered"), "an-unrelated-blob-id")


def test_check_expected_never_compares_the_blob_id_of_an_lfs_file() -> None:
    """For an LFS file `blob_id` is the sha1 of the *pointer*, not of the content.

    Checking it would fail on every large file in every repository — this is the single
    most damaging branch to get backwards, so it is asserted explicitly.
    """
    blob = b"weights"
    file = SourceFile(
        path="model.bin",
        size=len(blob),
        blob_id="0" * 40,
        sha256=sha256_bytes(blob),
        xet_hash=None,
        is_lfs=True,
    )
    check_expected(file, sha256_bytes(blob), "f" * 40)  # wildly wrong blob id, still fine


def test_check_expected_uses_the_blob_id_for_non_lfs_files() -> None:
    """A non-LFS file has no upstream sha256, so the git blob id is the only attestation."""
    blob = b'{"a": 1}'
    file = SourceFile(
        path="config.json",
        size=len(blob),
        blob_id=git_blob_id([blob], len(blob)),
        sha256=None,
        xet_hash=None,
        is_lfs=False,
    )
    check_expected(file, sha256_bytes(blob), git_blob_id([blob], len(blob)))

    with pytest.raises(ChecksumMismatchError, match="git blob id mismatch"):
        check_expected(file, sha256_bytes(blob), git_blob_id([b"other"], 5))


def test_check_expected_tolerates_an_lfs_file_without_an_upstream_sha256() -> None:
    file = SourceFile(
        path="model.bin", size=3, blob_id="0" * 40, sha256=None, xet_hash=None, is_lfs=True
    )
    check_expected(file, sha256_bytes(b"abc"), "irrelevant")


# ── invariant 1: the manifest marks completeness ─────────────────────────────


def test_a_clean_run_writes_the_manifest_its_digest_and_the_ref(
    engine: Engine, destination: S3Destination, settings: Settings
) -> None:
    report = engine.sync(sync_request())

    repo = report.repos[0]
    assert repo.errors == ()
    assert report.ok is True
    assert repo.manifest_key == manifest_key(settings)
    assert destination.exists(manifest_key(settings))
    assert destination.exists(
        keys.manifest_digest_key(settings.s3.prefix, RepoType.models, REPO, COMMIT)
    )
    assert destination.exists(keys.ref_key(settings.s3.prefix, RepoType.models, REPO, "main"))


def test_a_mid_run_failure_leaves_no_manifest_at_all(
    engine: Engine, source: FakeSource, destination: S3Destination, settings: Settings
) -> None:
    """The core invariant. A torn revision must be indistinguishable from an aborted one.

    If a manifest were written here, `verify` would report the revision as complete and a
    later `restore` would hand the operator a silently short model.
    """
    source.fail_all["tokenizer.json"] = 99

    report = engine.sync(sync_request())

    repo = report.repos[0]
    assert repo.errors, "the failing file must be reported"
    assert "tokenizer.json" in repo.errors[0]
    assert report.ok is False
    assert repo.manifest_key is None
    assert destination.head(manifest_key(settings)) is None, (
        "a manifest was written despite a failed file; the revision is now mislabelled as complete"
    )
    assert not destination.exists(
        keys.manifest_digest_key(settings.s3.prefix, RepoType.models, REPO, COMMIT)
    )
    assert not destination.exists(keys.ref_key(settings.s3.prefix, RepoType.models, REPO, "main")), (
        "the ref must not advance to an incomplete revision"
    )


def test_the_files_that_did_succeed_survive_a_mid_run_failure(
    engine: Engine, source: FakeSource, destination: S3Destination, settings: Settings
) -> None:
    """Partial progress is kept on purpose: it is what makes the next run cheaper."""
    source.fail_all["tokenizer.json"] = 99
    engine.sync(sync_request())

    stored = {summary.key for summary in destination.list_keys(settings.s3.prefix)}
    assert any(key.endswith("files/config.json") for key in stored)
    assert not any(key.endswith("files/tokenizer.json") for key in stored)


def test_a_resume_after_a_failure_redoes_only_what_is_missing_once_complete(
    engine: Engine, source: FakeSource, destination: S3Destination, settings: Settings
) -> None:
    """Run 1 fails, run 2 repairs, run 3 is a no-op. The full resume lifecycle.

    Run 2 must redo *everything*, not just the failed file: with no manifest there is no
    trustworthy record of what run 1 stored, and inventing one from the surviving keys is
    exactly the size-only comparison this design rejects.
    """
    source.fail_all["tokenizer.json"] = 1

    first = engine.sync(sync_request())
    assert first.repos[0].errors
    assert not destination.exists(manifest_key(settings))
    fetched_in_first = source.hub_fetches

    second = engine.sync(sync_request())
    assert second.repos[0].errors == ()
    assert second.repos[0].files_transferred == 3
    assert second.repos[0].files_skipped == 0, "no manifest means nothing may be trusted as done"
    assert destination.exists(manifest_key(settings))

    fetched_in_second = source.hub_fetches - fetched_in_first
    assert fetched_in_second == 3

    third = engine.sync(sync_request())
    assert third.repos[0].files_skipped == 3, "a complete revision must be fully skipped"
    assert third.repos[0].files_transferred == 0
    assert source.hub_fetches == fetched_in_first + fetched_in_second, (
        "a fully-satisfied resume must not fetch a single byte from Hugging Face"
    )


def test_fail_fast_stops_at_the_first_error_and_still_writes_no_manifest(
    source: FakeSource, destination: S3Destination, make_settings: object
) -> None:
    settings = make_settings(transfer={"workers": 1, "fail_fast": True})  # type: ignore[operator]
    engine = Engine(source, destination, settings, run_id="r", tool_version="0.1.0")  # type: ignore[arg-type]
    source.fail_all["config.json"] = 99

    report = engine.sync(sync_request())

    assert report.ok is False
    assert not destination.exists(manifest_key(settings))


# ── invariant 2: an upload failure never re-downloads from the Hub ───────────


def test_a_retried_s3_part_does_not_re_download_the_file(
    source: FakeSource,
    spy_destination: tuple[S3Destination, SpyClient],
    make_settings: object,
    staging_dir: Path,
    instant_retry: None,
) -> None:
    """A flaky part is retried against the staged bytes, not against Hugging Face.

    This is the defect the three-way transfer design exists to close: without it, one
    transient 503 on part 7 of 200 costs a fresh multi-gigabyte download.
    """
    destination, spy = spy_destination
    settings = make_settings(  # type: ignore[operator]
        transfer={"workers": 1, "mode": TransferMode.disk, "staging_dir": staging_dir}
    )
    engine = Engine(source, destination, settings, run_id="r", tool_version="0.1.0")  # type: ignore[arg-type]

    # A retryable S3 failure on the first part of the first multipart upload.
    from botocore.exceptions import ClientError

    def flaky(index: int, _kwargs: dict[str, object]) -> None:
        if index == 0:
            raise ClientError(
                {
                    "Error": {"Code": "SlowDown", "Message": "slow down"},
                    "ResponseMetadata": {"HTTPStatusCode": 503},
                },
                "UploadPart",
            )
        return None

    spy.faults["upload_part"] = flaky

    report = engine.sync(sync_request())

    assert report.ok is True, f"the retry should have absorbed the failure: {report.repos[0].errors}"
    per_file = {path: source.staged_calls.count(path) for path in set(source.staged_calls)}
    assert all(count == 1 for count in per_file.values()), (
        f"a file was downloaded from Hugging Face more than once: {per_file}"
    )
    assert spy.params("upload_part"), "the retry path must actually have been exercised"


def test_a_stream_failure_downgrades_to_disk_and_pays_the_hub_once_more_at_most(
    source: FakeSource, destination: S3Destination, make_settings: object, staging_dir: Path
) -> None:
    """STREAM cannot rewind a torn body, so the re-dispatch demotes the file to DISK."""
    settings = make_settings(  # type: ignore[operator]
        transfer={
            "workers": 1,
            "mode": TransferMode.auto,
            "inline_max": 1,
            "stream_failure_downgrade": 1,
            "staging_dir": staging_dir,
        }
    )
    engine = Engine(source, destination, settings, run_id="r", tool_version="0.1.0")  # type: ignore[arg-type]
    source.fail_stream["model.safetensors"] = 1

    report = engine.sync(sync_request())

    assert report.ok is True
    assert source.stream_calls.count("model.safetensors") == 1, "STREAM is attempted exactly once"
    assert source.staged_calls.count("model.safetensors") == 1, "then DISK, exactly once"
    assert report.repos[0].by_path.get("disk") == 1, "the downgraded file is reported as DISK"


def test_a_stream_checksum_mismatch_deletes_the_uploaded_object_before_raising(
    destination: S3Destination, make_settings: object, staging_dir: Path
) -> None:
    """Never leave a bad object behind for a later restore to trust.

    On the STREAM path the digest is only known after the upload completed, so the object
    already exists by the time the mismatch is detected. It must be removed.
    """
    blob = b"n" * (6 * MIB)
    source = FakeSource({"model.safetensors": blob})
    # Claim an upstream sha256 that the streamed bytes will never match.
    source.lfs = {"model.safetensors"}
    settings = make_settings(  # type: ignore[operator]
        transfer={"workers": 1, "mode": TransferMode.stream, "inline_max": 1,
                  "staging_dir": staging_dir}
    )
    engine = Engine(source, destination, settings, run_id="r", tool_version="0.1.0")  # type: ignore[arg-type]

    real_file = source.file
    source.file = lambda path: dataclasses.replace(  # type: ignore[method-assign]
        real_file(path), sha256="f" * 64
    )

    report = engine.sync(sync_request())

    assert report.ok is False
    assert "ChecksumMismatchError" in report.repos[0].errors[0]
    key = keys.file_key(settings.s3.prefix, RepoType.models, REPO, COMMIT, "model.safetensors")
    assert destination.head(key) is None, (
        "the corrupt object was left in the bucket; a later restore would trust it"
    )
    assert not destination.exists(manifest_key(settings))


def test_an_integrity_error_is_not_retried(
    destination: S3Destination, make_settings: object, staging_dir: Path
) -> None:
    """Bad bytes are not a transport problem, so re-dispatching cannot help."""
    blob = b"n" * (6 * MIB)
    source = FakeSource({"model.safetensors": blob})
    settings = make_settings(  # type: ignore[operator]
        transfer={"workers": 1, "mode": TransferMode.stream, "inline_max": 1,
                  "stream_failure_downgrade": 3, "staging_dir": staging_dir}
    )
    engine = Engine(source, destination, settings, run_id="r", tool_version="0.1.0")  # type: ignore[arg-type]
    real_file = source.file
    source.file = lambda path: dataclasses.replace(  # type: ignore[method-assign]
        real_file(path), sha256="f" * 64
    )

    engine.sync(sync_request())

    assert source.stream_calls.count("model.safetensors") == 1, (
        "a checksum failure must not be re-dispatched"
    )


# ── resume: never size-only ──────────────────────────────────────────────────


def test_a_skip_requires_the_digest_the_size_and_the_etag_to_agree(
    engine: Engine, source: FakeSource, destination: S3Destination, settings: Settings
) -> None:
    """Defect (e): comparing size alone silently accepts a corrupted stored object."""
    engine.sync(sync_request())
    baseline = source.hub_fetches

    # Replace one stored object with same-sized but different bytes: the etag changes.
    key = keys.file_key(settings.s3.prefix, RepoType.models, REPO, COMMIT, "config.json")
    original = destination.get_bytes(key)
    tampered = b"X" * len(original)
    destination.put_small(key, tampered, sha256=sha256_bytes(tampered))

    report = engine.sync(sync_request(recheck=RecheckMode.head))

    assert report.repos[0].files_transferred == 1, "the tampered object must be re-uploaded"
    assert report.repos[0].files_skipped == 2
    assert source.hub_fetches == baseline + 1
    assert destination.get_bytes(key) == original, "the good bytes must be restored"


def test_a_missing_object_is_re_uploaded_even_though_the_manifest_lists_it(
    engine: Engine, destination: S3Destination, settings: Settings
) -> None:
    engine.sync(sync_request())
    key = keys.file_key(settings.s3.prefix, RepoType.models, REPO, COMMIT, "config.json")
    destination.delete_keys([key])

    report = engine.sync(sync_request(recheck=RecheckMode.head))

    assert report.repos[0].files_transferred == 1
    assert destination.exists(key)


def test_recheck_none_never_skips(
    engine: Engine, source: FakeSource, destination: S3Destination
) -> None:
    engine.sync(sync_request())
    baseline = source.hub_fetches

    report = engine.sync(sync_request(recheck=RecheckMode.none))

    assert report.repos[0].files_skipped == 0
    assert report.repos[0].files_transferred == 3
    assert source.hub_fetches == baseline + 3


def test_recheck_deep_rehashes_the_stored_bytes(
    engine: Engine, destination: S3Destination, settings: Settings
) -> None:
    """`deep` catches a corruption that preserved both the size and the ETag headers."""
    engine.sync(sync_request())
    key = keys.file_key(settings.s3.prefix, RepoType.models, REPO, COMMIT, "config.json")
    original = destination.get_bytes(key)
    destination.put_small(key, b"Y" * len(original), sha256=sha256_bytes(b"Y"))

    report = engine.sync(sync_request(recheck=RecheckMode.deep))

    assert report.repos[0].files_transferred == 1
    assert destination.get_bytes(key) == original


def test_an_unusable_manifest_is_ignored_for_resume_rather_than_fatal(
    engine: Engine, destination: S3Destination, settings: Settings
) -> None:
    """A corrupt manifest must make the next sync re-upload, not refuse to run."""
    engine.sync(sync_request())
    destination.put_small(manifest_key(settings), b"{ this is not a manifest", sha256="0" * 64)

    report = engine.sync(sync_request())

    assert report.ok is True
    assert report.repos[0].files_transferred == 3


# ── dry run ──────────────────────────────────────────────────────────────────


def test_a_dry_run_transfers_nothing_and_writes_nothing(
    engine: Engine, source: FakeSource, destination: S3Destination, settings: Settings
) -> None:
    report = engine.sync(sync_request(dry_run=True))

    repo = report.repos[0]
    assert repo.files_total == 3
    assert repo.files_transferred == 0
    assert repo.manifest_key is None
    assert sum(repo.by_path.values()) == 3, "every file must still be assigned a path"
    assert source.hub_fetches == 0, "a dry run must not fetch a byte"
    assert list(destination.list_keys(settings.s3.prefix)) == []


# ── transfer path assignment ─────────────────────────────────────────────────


def test_small_files_take_the_inline_path_and_large_ones_stream(
    source: FakeSource, destination: S3Destination, make_settings: object, staging_dir: Path
) -> None:
    """The three-way split in practice: one PUT for the small files, MPU for the big one."""
    settings = make_settings(  # type: ignore[operator]
        transfer={"workers": 1, "inline_max": 1024, "staging_dir": staging_dir}
    )
    engine = Engine(source, destination, settings, run_id="r", tool_version="0.1.0")  # type: ignore[arg-type]

    report = engine.sync(sync_request())

    assert report.repos[0].by_path == {"inline": 2, "stream": 1}
    assert sorted(source.read_calls) == ["config.json", "tokenizer.json"]
    assert source.stream_calls == ["model.safetensors"]


def test_every_file_goes_inline_when_it_fits_under_inline_max(
    engine: Engine, source: FakeSource
) -> None:
    """With the 8 MiB default, a 6 MiB shard is a single PutObject, not a multipart."""
    report = engine.sync(sync_request())
    assert report.repos[0].by_path == {"inline": 3}
    assert source.stream_calls == []


def test_disk_mode_routes_every_file_through_staging(
    source: FakeSource, destination: S3Destination, make_settings: object, staging_dir: Path
) -> None:
    settings = make_settings(  # type: ignore[operator]
        transfer={"workers": 1, "mode": TransferMode.disk, "staging_dir": staging_dir}
    )
    engine = Engine(source, destination, settings, run_id="r", tool_version="0.1.0")  # type: ignore[arg-type]

    report = engine.sync(sync_request())

    assert report.repos[0].by_path == {"disk": 3}
    assert sorted(source.staged_calls) == sorted(source.blobs)
    assert source.stream_calls == [] and source.read_calls == []
    assert not any(staging_dir.rglob("*.bin")), "staging must be left clean"


def test_the_progress_hook_reports_each_file(engine: Engine) -> None:
    events: list[tuple[str, str, int]] = []
    engine.progress_hook = lambda event, path, size: events.append((event, path, size))

    engine.sync(sync_request())

    assert {event for event, _, _ in events} == {"start", "done"}
    assert {path for _, path, _ in events} == {"config.json", "tokenizer.json", "model.safetensors"}


# ── verify and restore round trip ────────────────────────────────────────────


def test_verify_reports_ok_for_a_clean_backup(engine: Engine) -> None:
    engine.sync(sync_request())

    report = engine.verify(
        VerifyRequest(repo=RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT))
    )
    assert report.status is VerifyStatus.ok
    assert report.checked == 3
    assert report.findings == ()


def test_verify_reports_incomplete_when_there_is_no_manifest(
    engine: Engine, source: FakeSource
) -> None:
    source.fail_all["config.json"] = 99
    engine.sync(sync_request())

    report = engine.verify(
        VerifyRequest(repo=RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT))
    )
    assert report.status is VerifyStatus.incomplete
    assert report.checked == 0


def test_verify_reports_drift_for_a_missing_object(
    engine: Engine, destination: S3Destination, settings: Settings
) -> None:
    engine.sync(sync_request())
    destination.delete_keys(
        [keys.file_key(settings.s3.prefix, RepoType.models, REPO, COMMIT, "config.json")]
    )

    report = engine.verify(
        VerifyRequest(repo=RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT))
    )
    assert report.status is VerifyStatus.drift
    assert [finding.kind for finding in report.findings] == ["missing"]


def test_verify_reports_corrupt_when_deep_hashing_disagrees(
    engine: Engine, destination: S3Destination, settings: Settings
) -> None:
    """A sha256 finding is corruption, not drift — and the two exit codes differ."""
    engine.sync(sync_request())
    key = keys.file_key(settings.s3.prefix, RepoType.models, REPO, COMMIT, "config.json")
    original = destination.get_bytes(key)
    destination.put_small(key, b"Z" * len(original), sha256=sha256_bytes(b"Z"))

    report = engine.verify(
        VerifyRequest(
            repo=RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT),
            level=VerifyLevel.deep,
        )
    )
    assert report.status is VerifyStatus.corrupt
    assert "sha256" in {finding.kind for finding in report.findings}


def test_verify_upstream_compares_the_manifest_against_the_hub_tree(
    engine: Engine, source: FakeSource
) -> None:
    """`--level upstream` re-fetches the Hub tree at the pinned SHA; values must match."""
    engine.sync(sync_request())

    report = engine.verify(
        VerifyRequest(
            repo=RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT),
            level=VerifyLevel.upstream,
        )
    )
    assert report.status is VerifyStatus.ok
    assert report.findings == ()


def test_verify_upstream_flags_a_file_that_changed_on_the_hub(
    engine: Engine, source: FakeSource
) -> None:
    """A pinned commit is immutable, so any upstream difference means real corruption."""
    engine.sync(sync_request())
    source.blobs["config.json"] = b'{"model_type": "mistral", "changed": true}'

    report = engine.verify(
        VerifyRequest(
            repo=RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT),
            level=VerifyLevel.upstream,
        )
    )
    assert report.status is VerifyStatus.corrupt
    upstream = [finding for finding in report.findings if finding.kind == "upstream"]
    assert [finding.path for finding in upstream] == ["config.json"]


def test_verify_upstream_flags_a_file_that_vanished_from_the_hub(
    engine: Engine, source: FakeSource
) -> None:
    engine.sync(sync_request())
    del source.blobs["tokenizer.json"]

    report = engine.verify(
        VerifyRequest(
            repo=RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT),
            level=VerifyLevel.upstream,
        )
    )
    assert report.status is VerifyStatus.corrupt
    assert any(
        finding.actual == "absent upstream" and finding.path == "tokenizer.json"
        for finding in report.findings
    )


def test_verify_sampling_is_deterministic(engine: Engine) -> None:
    """The commit SHA seeds the sample, so two verifies check the same files."""
    engine.sync(sync_request())
    request = VerifyRequest(
        repo=RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT),
        sample_percent=50.0,
    )
    first = engine.verify(request)
    second = engine.verify(request)
    assert first.checked == second.checked == 2
    assert first.findings == second.findings


def test_sync_then_restore_round_trips_every_byte(
    engine: Engine, blobs: dict[str, bytes], dest_dir: Path
) -> None:
    from bg_ai_model_management.tools.hfbackup.engine import RestoreRequest

    engine.sync(sync_request())
    report = engine.restore(
        RestoreRequest(
            repo=RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT),
            dest=dest_dir,
        )
    )

    assert report.files == 3
    for path, blob in blobs.items():
        assert (dest_dir / path).read_bytes() == blob


def test_restore_refuses_an_incomplete_revision(engine: Engine, source: FakeSource, dest_dir: Path) -> None:
    from bg_ai_model_management.tools.hfbackup.engine import RestoreRequest

    source.fail_all["config.json"] = 99
    engine.sync(sync_request())

    with pytest.raises(ManifestError, match="incomplete"):
        engine.restore(
            RestoreRequest(
                repo=RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT),
                dest=dest_dir,
            )
        )


# ── revision resolution ──────────────────────────────────────────────────────


def test_a_hex_revision_is_used_without_touching_the_hub(engine: Engine, source: FakeSource) -> None:
    resolved = engine.resolve_revision(
        RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT.upper())
    )
    assert resolved == COMMIT.lower()
    assert source.pin_calls == 0


def test_a_named_revision_is_resolved_from_the_stored_ref_not_the_hub(
    engine: Engine, source: FakeSource
) -> None:
    """`restore` and `verify` must work without Hub credentials or Hub connectivity."""
    engine.sync(sync_request())
    pins_after_sync = source.pin_calls

    resolved = engine.resolve_revision(
        RepoRef(repo_id=REPO, repo_type=RepoType.models, revision="main")
    )
    assert resolved == COMMIT
    assert source.pin_calls == pins_after_sync, "a stored ref must not trigger a Hub call"


def test_an_unknown_named_revision_never_falls_back_to_the_hub(
    engine: Engine, source: FakeSource
) -> None:
    """Regression: the Hub fallback defeated restore in the disaster it exists for.

    A sync pinned by SHA — or run with `--no-update-ref` — writes no ref object. Two
    years later the upstream repository is deleted and `restore org/model` pins against
    a Hub that answers 404, so the command dies without ever looking at the intact
    objects sitting in the bucket. Resolution must stay inside the object store.
    """
    before = source.pin_calls
    with pytest.raises(ConfigError, match="no stored ref"):
        engine.resolve_revision(
            RepoRef(repo_id=REPO, repo_type=RepoType.models, revision="never-synced")
        )
    assert source.pin_calls == before, "resolving a revision must never contact the Hub"


def test_restore_never_reaches_the_hub_when_no_ref_was_stored(
    engine: Engine, source: FakeSource, dest_dir: Path, blobs: dict[str, bytes]
) -> None:
    """The whole point of a backup: the bucket is intact, the Hub is not.

    A sync pinned by SHA — or, as here, `--no-update-ref` — writes no ref object. Two
    years later the upstream repository has been deleted and the operator runs restore.
    The old fallback pinned against the Hub, got RepoNotFoundError, and exited without
    ever looking at the perfectly intact objects in the bucket. Resolution must stay
    inside the store: the SHA restores, and the unresolvable name yields an actionable
    store-only error rather than a Hub call.
    """
    engine.sync(sync_request(update_ref=False))

    def gone(ref: RepoRef) -> PinnedRepo:
        raise SourceError("repository not found, or not visible to this token")

    source.pin = gone  # type: ignore[method-assign]

    report = engine.restore(
        RestoreRequest(
            repo=RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT),
            dest=dest_dir,
        )
    )
    assert report.files == len(blobs)
    for path, blob in blobs.items():
        assert (dest_dir / path).read_bytes() == blob

    with pytest.raises(ConfigError, match="no stored ref"):
        engine.restore(
            RestoreRequest(
                repo=RepoRef(repo_id=REPO, repo_type=RepoType.models, revision="main"),
                dest=dest_dir,
                overwrite=True,
            )
        )


def test_the_error_for_an_unresolvable_revision_lists_the_stored_shas(
    engine: Engine,
) -> None:
    engine.sync(sync_request())
    with pytest.raises(ConfigError, match=COMMIT):
        engine.resolve_revision(
            RepoRef(repo_id=REPO, repo_type=RepoType.models, revision="v9.9.9")
        )


def test_a_malformed_ref_object_is_a_manifest_error(
    engine: Engine, destination: S3Destination, settings: Settings
) -> None:
    key = keys.ref_key(settings.s3.prefix, RepoType.models, REPO, "main")
    destination.put_small(key, b'{"commit_sha": "not-a-sha"}', sha256="0" * 64)

    with pytest.raises(ManifestError, match="invalid commit sha"):
        engine.resolve_revision(RepoRef(repo_id=REPO, repo_type=RepoType.models, revision="main"))


def test_syncing_a_pinned_sha_does_not_write_a_ref(
    engine: Engine, destination: S3Destination, settings: Settings
) -> None:
    """A ref file named after a commit SHA would be meaningless, so it is skipped."""
    engine.sync(
        sync_request(repos=(RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT),))
    )
    assert not destination.exists(keys.ref_key(settings.s3.prefix, RepoType.models, REPO, COMMIT))


def test_update_ref_false_leaves_the_ref_alone(
    engine: Engine, destination: S3Destination, settings: Settings
) -> None:
    engine.sync(sync_request(update_ref=False))
    assert not destination.exists(keys.ref_key(settings.s3.prefix, RepoType.models, REPO, "main"))


# ── concurrency ──────────────────────────────────────────────────────────────


def test_sync_is_correct_under_a_real_worker_pool(
    destination: S3Destination, make_settings: object, staging_dir: Path
) -> None:
    """Eight workers over thirty files: every object stored, exactly one manifest."""
    payloads = {f"shard-{index:02d}.bin": bytes([index]) * (index + 1) for index in range(30)}
    source = FakeSource(payloads)
    settings = make_settings(transfer={"workers": 8, "staging_dir": staging_dir})  # type: ignore[operator]
    engine = Engine(source, destination, settings, run_id="r", tool_version="0.1.0")  # type: ignore[arg-type]

    report = engine.sync(sync_request())

    assert report.ok is True
    assert report.repos[0].files_transferred == 30
    document = destination.get_bytes(manifest_key(settings))
    assert len(document) > 0
    stored = [
        summary
        for summary in destination.list_keys(settings.s3.prefix)
        if "/files/" in summary.key
    ]
    assert len(stored) == 30


# ── regressions ──────────────────────────────────────────────────────────────


class DelegatingDestination:
    """Proxy over the real `S3Destination` that can observe or break `get_stream`.

    moto gives a faithful S3; what it cannot give is a torn download, nor a window in
    which to look at `--dest` while a body is still being written. Both are needed to
    pin the restore invariants below.
    """

    def __init__(self, inner: S3Destination) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class WatchingDestination(DelegatingDestination):
    """Snapshots the restore directory halfway through every body."""

    def __init__(self, inner: S3Destination, dest: Path) -> None:
        super().__init__(inner)
        self._dest = dest
        self.snapshots: list[list[str]] = []

    @contextmanager
    def get_stream(self, key: str) -> Iterator[Iterator[bytes]]:
        with self._inner.get_stream(key) as chunks:
            data = b"".join(chunks)
        half = len(data) // 2

        def body() -> Iterator[bytes]:
            yield data[:half]
            self.snapshots.append(sorted(path.name for path in self._dest.iterdir()))
            yield data[half:]

        yield body()


class TornDestination(DelegatingDestination):
    """Drops the connection partway through every body, as a real store does."""

    @contextmanager
    def get_stream(self, key: str) -> Iterator[Iterator[bytes]]:
        with self._inner.get_stream(key) as chunks:
            data = b"".join(chunks)

        def body() -> Iterator[bytes]:
            yield data[: len(data) // 2]
            raise ConnectionError("read timeout on endpoint URL")

        yield body()


def test_a_narrower_resync_at_the_same_commit_keeps_the_earlier_files(
    engine: Engine,
    destination: S3Destination,
    settings: Settings,
    blobs: dict[str, bytes],
    dest_dir: Path,
) -> None:
    """Regression: a narrower re-sync demoted a complete backup to a partial one.

    `sync org/model` then `sync org/model --include '*.json'` at the same commit used to
    replace the full manifest with a shorter one. The remaining objects stayed in the
    bucket referenced by nothing, so `restore` handed back an unusable model while
    `verify` and `catalog` both reported the revision as healthy — the cardinal failure
    mode for a backup tool. `commit_sha` pins an immutable file set, so anything the
    previous manifest recorded is still a genuine file of exactly this revision.
    """
    engine.sync(sync_request())
    engine.sync(sync_request(include=("*.json",), exclude=("tokenizer*",)))

    document = Manifest.from_json(destination.get_bytes(manifest_key(settings)))
    assert sorted(entry.path for entry in document.files) == sorted(blobs)
    assert document.totals.files == len(blobs)
    assert document.totals.bytes == sum(len(blob) for blob in blobs.values())
    # `transferred` and `skipped` describe THIS run, so they cover only config.json.
    assert document.totals.transferred + document.totals.skipped == 1
    # The recorded selection must span both runs, or the field is a lie.
    assert set(document.selection.include) == {"*", "*.json"}

    report = engine.restore(
        RestoreRequest(
            repo=RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT),
            dest=dest_dir,
        )
    )
    assert report.files == len(blobs)
    for path, blob in blobs.items():
        assert (dest_dir / path).read_bytes() == blob


def test_verify_still_covers_every_file_after_a_narrower_resync(
    engine: Engine, blobs: dict[str, bytes]
) -> None:
    engine.sync(sync_request())
    engine.sync(sync_request(include=("*.json",)))

    report = engine.verify(
        VerifyRequest(
            repo=RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT),
            level=VerifyLevel.deep,
        )
    )
    assert report.status is VerifyStatus.ok
    assert report.checked == len(blobs)


def test_a_stream_only_transfer_failure_keeps_the_original_cause(
    destination: S3Destination, make_settings: Callable[..., Settings], staging_dir: Path
) -> None:
    """Regression: the terminal raise blamed a setting and discarded the real error.

    Under `--mode stream` the STREAM -> DISK downgrade is deliberately disabled, so a
    persistently failing stream falls out of the dispatch loop. That must surface as a
    TransferError chained to the transport failure — not as a ConfigError pointing at
    `transfer.stream_failure_downgrade`, which cannot do anything in this mode.
    """
    source = FakeSource({"a.bin": b"x" * (2 * MIB)})
    source.fail_stream["a.bin"] = 99
    settings = make_settings(
        transfer={"mode": TransferMode.stream, "inline_max": 1, "staging_dir": staging_dir}
    )
    engine = Engine(source, destination, settings, run_id="r", tool_version="0.1.0")  # type: ignore[arg-type]
    pinned = source.pin(RepoRef(repo_id=REPO, repo_type=RepoType.models, revision="main"))

    with pytest.raises(TransferError) as caught:
        engine._transfer_one(
            pinned,
            source.file("a.bin"),
            {},
            sync_request(),
            DiskBudget(1 << 30),
            StreamFailureTracker(),
            staging_dir,
        )

    assert isinstance(caught.value.__cause__, SourceError)
    assert "stream_failure_downgrade" not in str(caught.value)


def test_the_restore_part_file_never_occupies_another_target_path(
    destination: S3Destination, make_settings: Callable[..., Settings], dest_dir: Path
) -> None:
    """Regression: `X` and `X.aimm-part` in one repository shared a part-file path.

    Two workers then wrote and renamed through the same file, so the restored `X` could
    silently receive the other file's bytes (POSIX) or the run could die mid-restore
    with WinError 5 (Windows) — in both cases after reporting success. The part name
    must therefore not be derivable from the target name alone.
    """
    payloads = {
        "model.safetensors": b"real-weights" * 512,
        "model.safetensors.aimm-part": b"decoy-bytes" * 512,
    }
    source = FakeSource(payloads)
    settings = make_settings(transfer={"workers": 1})
    watcher = WatchingDestination(destination, dest_dir)
    engine = Engine(source, destination, settings, run_id="r", tool_version="0.1.0")  # type: ignore[arg-type]
    engine.sync(sync_request())
    engine.destination = watcher  # type: ignore[assignment]

    engine.restore(
        RestoreRequest(
            repo=RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT),
            dest=dest_dir,
        )
    )

    in_flight = {
        name for snapshot in watcher.snapshots for name in snapshot if name.endswith(".aimm-part")
    }
    collisions = sorted(in_flight & set(payloads))
    assert in_flight, "the snapshots never observed a part file at all"
    assert not collisions, f"a part file occupied another entry's target path: {collisions}"
    for path, blob in payloads.items():
        assert (dest_dir / path).read_bytes() == blob


def test_a_torn_restore_leaves_no_part_file_behind(
    engine: Engine, destination: S3Destination, dest_dir: Path
) -> None:
    """Regression: only a digest mismatch unlinked the part file.

    A connection dropped mid-body left a `.aimm-part` of arbitrary size sitting in
    `--dest`, where it looks like real content and no later run removes it.
    """
    engine.sync(sync_request())
    engine.destination = TornDestination(destination)  # type: ignore[assignment]

    with pytest.raises(ConnectionError):
        engine.restore(
            RestoreRequest(
                repo=RepoRef(repo_id=REPO, repo_type=RepoType.models, revision=COMMIT),
                dest=dest_dir,
            )
        )

    assert list(dest_dir.rglob("*.aimm-part")) == []


def test_a_dry_run_does_not_create_the_staging_directory(
    destination: S3Destination,
    make_settings: Callable[..., Settings],
    source: FakeSource,
    tmp_path: Path,
) -> None:
    """`--dry-run` promises to transfer no bytes and write nothing."""
    staging = tmp_path / "mnt" / "nvme" / "aimm-staging"
    settings = make_settings(transfer={"staging_dir": staging})
    engine = Engine(source, destination, settings, run_id="r", tool_version="0.1.0")  # type: ignore[arg-type]

    report = engine.sync(sync_request(dry_run=True))

    assert report.ok is True
    assert not staging.exists()
    assert not staging.parent.exists()


def test_an_lfs_file_without_an_upstream_sha256_is_recorded_as_computed(
    engine: Engine, source: FakeSource, destination: S3Destination, settings: Settings
) -> None:
    """Regression: `sha256_source` claimed a provenance that was never established.

    `hf-lfs` means the digest was CONFIRMED against Hugging Face's own value. When the
    Hub reports an LFS file with no `lfs.sha256` there is nothing to confirm against, so
    the digest is local and must be labelled `computed` like any other.
    """
    source.no_upstream_sha256 = {"config.json"}
    engine.sync(sync_request())

    document = Manifest.from_json(destination.get_bytes(manifest_key(settings)))
    entry = document.index()["config.json"]
    assert entry.lfs is True
    assert entry.sha256_source == "computed"


# ── shutdown: a signalled stop is not a completed revision ───────────────────


def test_a_shutdown_signal_stops_the_sync_and_writes_no_manifest(
    engine: Engine, destination: S3Destination, settings: Settings
) -> None:
    """A container stopped mid-sync must leave the revision visibly incomplete.

    The manifest is the only completeness marker, so writing one for a run that was
    cut short would permanently label a torn revision as good.
    """
    shutdown.request()
    try:
        with pytest.raises(OperationCancelledError):
            engine.sync(sync_request())
    finally:
        shutdown.reset()

    stored = {summary.key for summary in destination.list_keys(settings.s3.prefix)}
    assert not any(key.endswith("manifest.json") for key in stored)


def test_a_shutdown_signal_stops_files_from_being_downloaded(
    engine: Engine, source: FakeSource
) -> None:
    """Queued work must not start a fresh Hub download after the signal arrived."""
    shutdown.request()
    try:
        with pytest.raises(OperationCancelledError):
            engine.sync(sync_request())
    finally:
        shutdown.reset()

    fetched = source.read_calls + source.stream_calls + source.staged_calls
    assert fetched == [], "no file may be fetched once shutdown was requested"
