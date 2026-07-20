"""Tests for the manifest: shape, deterministic bytes, digest, and self-sufficiency.

The manifest is the only durable record of what a backup contains. Two properties carry
that weight and are tested hardest here: serialisation is byte-for-byte deterministic
(because the sha256 sidecar covers exactly those bytes), and a manifest plus a bucket is
enough to drive a full restore with Hugging Face unreachable.
"""

from __future__ import annotations

import dataclasses
import json
import random
from typing import Any

import pytest

from bg_ai_model_management.config.models import Settings
from bg_ai_model_management.errors import ChecksumMismatchError, ManifestError
from bg_ai_model_management.integrity.hashing import sha256_bytes
from bg_ai_model_management.tools.hfbackup import keys
from bg_ai_model_management.tools.hfbackup.destination import S3Destination
from bg_ai_model_management.tools.hfbackup.engine import Engine, RestoreRequest
from bg_ai_model_management.tools.hfbackup.manifest import (
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    Manifest,
    ManifestSelection,
    build_manifest,
    digest_line,
    verify_digest,
)
from bg_ai_model_management.tools.hfbackup.types import (
    FileResult,
    PinnedRepo,
    RepoRef,
    RepoType,
    TransferPath,
)

COMMIT = "a" * 40


def file_result(path: str, *, size: int = 10, sha256: str | None = None) -> FileResult:
    return FileResult(
        path=path,
        key=f"aimm/v1/models/acme/model/revisions/{COMMIT}/files/{path}",
        size=size,
        sha256=sha256 or sha256_bytes(path.encode()),
        sha256_source="computed",
        blob_id="b" * 40,
        xet_hash=None,
        is_lfs=False,
        etag="e" * 32,
        part_size=None,
        parts=1,
        transfer_path=TransferPath.inline,
        skipped=False,
        uploaded_at="2026-07-20T12:00:00Z",
    )


@pytest.fixture
def pinned() -> PinnedRepo:
    return PinnedRepo(
        repo_id="acme/model",
        repo_type=RepoType.models,
        revision_requested="main",
        commit_sha=COMMIT,
    )


@pytest.fixture
def manifest(pinned: PinnedRepo, settings: Settings) -> Manifest:
    return build_manifest(
        pinned=pinned,
        results=[file_result("config.json"), file_result("model.bin", size=2048)],
        settings=settings,
        key_root=keys.revision_root("aimm", RepoType.models, "acme/model", COMMIT),
        selection=ManifestSelection(),
        tool_version="0.1.0",
        run_id="20260720T120000Z-abcdef",
    )


# ── shape ────────────────────────────────────────────────────────────────────


def test_build_manifest_records_source_destination_and_totals(
    manifest: Manifest, settings: Settings
) -> None:
    assert manifest.manifest_version == MANIFEST_VERSION
    assert manifest.tool == "aimm"
    assert manifest.source.repo_id == "acme/model"
    assert manifest.source.commit_sha == COMMIT
    assert manifest.source.repo_type == "models"
    assert manifest.destination.bucket == settings.s3.bucket
    assert manifest.totals.files == 2
    assert manifest.totals.bytes == 10 + 2048
    assert manifest.totals.transferred == 2
    assert manifest.totals.skipped == 0


def test_totals_split_transferred_from_skipped(pinned: PinnedRepo, settings: Settings) -> None:
    """A resumed run reports what it actually moved, not what the revision contains."""
    moved = file_result("new.bin")
    reused = dataclasses.replace(file_result("old.bin"), skipped=True)
    document = build_manifest(
        pinned=pinned,
        results=[moved, reused],
        settings=settings,
        key_root="root",
        selection=ManifestSelection(),
        tool_version="0.1.0",
        run_id="run",
    )
    assert document.totals.files == 2
    assert document.totals.transferred == 1
    assert document.totals.skipped == 1


def test_manifest_rejects_unknown_fields(manifest: Manifest) -> None:
    """`extra='forbid'`: a typo in a hand-edited manifest must fail loudly."""
    raw = json.loads(manifest.to_json())
    raw["surprise"] = True
    with pytest.raises(ManifestError):
        Manifest.from_json(json.dumps(raw).encode())


def test_manifest_rejects_a_future_version(manifest: Manifest) -> None:
    """A newer tool's manifest must not be half-understood by an older build."""
    raw = json.loads(manifest.to_json())
    raw["manifest_version"] = MANIFEST_VERSION + 1
    with pytest.raises(ManifestError) as excinfo:
        Manifest.from_json(json.dumps(raw).encode())
    assert "unsupported manifest_version" in str(excinfo.value)


@pytest.mark.parametrize(
    "payload",
    [b"", b"not json", b"[]", b'"a string"', b"null", b"\xff\xfe garbage"],
)
def test_manifest_rejects_malformed_input(payload: bytes) -> None:
    with pytest.raises(ManifestError):
        Manifest.from_json(payload)


def test_index_maps_path_to_entry(manifest: Manifest) -> None:
    index = manifest.index()
    assert set(index) == {"config.json", "model.bin"}
    assert index["model.bin"].size == 2048


# ── determinism ──────────────────────────────────────────────────────────────


def test_serialisation_is_byte_identical_across_calls(manifest: Manifest) -> None:
    assert manifest.to_json() == manifest.to_json()


def test_serialisation_is_independent_of_input_order(
    pinned: PinnedRepo, settings: Settings
) -> None:
    """Same content, shuffled: identical bytes. Otherwise the digest is meaningless.

    The engine collects results from a thread pool via `as_completed`, so the input order
    is genuinely non-deterministic in production. This is the test that makes the sidecar
    digest a real integrity check rather than a record of scheduling luck.
    """
    results = [file_result(f"file-{index:02d}.bin", size=index) for index in range(25)]
    kwargs: dict[str, Any] = {
        "pinned": pinned,
        "settings": settings,
        "key_root": "root",
        "selection": ManifestSelection(),
        "tool_version": "0.1.0",
        "run_id": "fixed-run-id",
    }
    first = build_manifest(results=list(results), **kwargs)
    baseline = first.to_json()

    rng = random.Random(1234)
    for _ in range(10):
        shuffled = list(results)
        rng.shuffle(shuffled)
        other = build_manifest(results=shuffled, **kwargs)
        # created_at is a timestamp, so compare everything else.
        assert other.model_copy(update={"created_at": first.created_at}).to_json() == baseline


def test_files_are_sorted_by_path(pinned: PinnedRepo, settings: Settings) -> None:
    results = [file_result(name) for name in ("z.bin", "a.bin", "m/nested.bin")]
    document = build_manifest(
        pinned=pinned,
        results=results,
        settings=settings,
        key_root="root",
        selection=ManifestSelection(),
        tool_version="0.1.0",
        run_id="run",
    )
    paths = [entry["path"] for entry in json.loads(document.to_json())["files"]]
    assert paths == sorted(paths)


def test_serialised_json_has_sorted_keys_and_a_trailing_newline(manifest: Manifest) -> None:
    data = manifest.to_json()
    assert data.endswith(b"\n")
    raw = json.loads(data)
    assert list(raw) == sorted(raw)


def test_round_trip_preserves_every_field(manifest: Manifest) -> None:
    assert Manifest.from_json(manifest.to_json()) == manifest
    assert Manifest.from_json(manifest.to_json()).to_json() == manifest.to_json()


def test_non_ascii_paths_survive_the_round_trip(pinned: PinnedRepo, settings: Settings) -> None:
    """`ensure_ascii=False` plus UTF-8: a repo with accented filenames must round-trip."""
    document = build_manifest(
        pinned=pinned,
        results=[file_result("modèle/données.bin")],
        settings=settings,
        key_root="root",
        selection=ManifestSelection(),
        tool_version="0.1.0",
        run_id="run",
    )
    data = document.to_json()
    assert "modèle/données.bin".encode() in data
    assert Manifest.from_json(data).files[0].path == "modèle/données.bin"


# ── the digest sidecar ───────────────────────────────────────────────────────


def test_digest_line_is_sha256sum_compatible(manifest: Manifest) -> None:
    data = manifest.to_json()
    line = digest_line(data)
    assert line == f"{sha256_bytes(data)}  {MANIFEST_FILENAME}\n"
    verify_digest(data, line)


def test_verify_digest_accepts_a_bare_digest_without_a_filename(manifest: Manifest) -> None:
    data = manifest.to_json()
    verify_digest(data, sha256_bytes(data))


def test_verify_digest_is_case_insensitive(manifest: Manifest) -> None:
    data = manifest.to_json()
    verify_digest(data, sha256_bytes(data).upper() + f"  {MANIFEST_FILENAME}\n")


def test_verify_digest_rejects_a_tampered_manifest(manifest: Manifest) -> None:
    """The whole point of the sidecar: a single flipped byte must be caught."""
    data = manifest.to_json()
    line = digest_line(data)
    tampered = data.replace(b'"size": 2048', b'"size": 2047')
    assert tampered != data, "the tamper must actually change the bytes"
    with pytest.raises(ManifestError) as excinfo:
        verify_digest(tampered, line)
    assert "digest mismatch" in str(excinfo.value)


@pytest.mark.parametrize("line", ["", "   ", "\n"])
def test_verify_digest_rejects_an_empty_sidecar(manifest: Manifest, line: str) -> None:
    with pytest.raises(ManifestError) as excinfo:
        verify_digest(manifest.to_json(), line)
    assert "empty" in str(excinfo.value)


# ── self-sufficiency: a manifest is enough to restore ────────────────────────


class ExplodingSource:
    """A `HubSource` stand-in that fails the test if anything touches Hugging Face."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"restore contacted the Hugging Face source (attribute {name!r}); "
            "a stored manifest must be sufficient on its own"
        )


def test_a_manifest_alone_drives_a_restore_without_contacting_hugging_face(
    destination: S3Destination,
    settings: Settings,
    pinned: PinnedRepo,
    dest_dir: Any,
) -> None:
    """Seed a bucket by hand, then restore from it with the Hub wired to explode.

    This is the disaster-recovery contract: if Hugging Face is gone, deleted the repo, or
    is simply unreachable, the backup must still be restorable from the object store and
    the manifest alone.
    """
    prefix = settings.s3.prefix
    payloads = {"config.json": b'{"hidden_size": 4096}', "weights/model.bin": b"\x00\x01" * 512}

    results = []
    for path, blob in payloads.items():
        key = keys.file_key(prefix, RepoType.models, pinned.repo_id, COMMIT, path)
        upload = destination.put_small(key, blob, sha256=sha256_bytes(blob))
        results.append(
            FileResult(
                path=path,
                key=key,
                size=len(blob),
                sha256=sha256_bytes(blob),
                sha256_source="computed",
                blob_id="b" * 40,
                xet_hash=None,
                is_lfs=False,
                etag=upload.etag,
                part_size=None,
                parts=1,
                transfer_path=TransferPath.inline,
                skipped=False,
                uploaded_at="2026-07-20T12:00:00Z",
            )
        )

    document = build_manifest(
        pinned=pinned,
        results=results,
        settings=settings,
        key_root=keys.revision_root(prefix, RepoType.models, pinned.repo_id, COMMIT),
        selection=ManifestSelection(),
        tool_version="0.1.0",
        run_id="run",
    )
    data = document.to_json()
    destination.put_small(
        keys.manifest_key(prefix, RepoType.models, pinned.repo_id, COMMIT),
        data,
        sha256=sha256_bytes(data),
    )
    line = digest_line(data).encode()
    destination.put_small(
        keys.manifest_digest_key(prefix, RepoType.models, pinned.repo_id, COMMIT),
        line,
        sha256=sha256_bytes(line),
    )

    engine = Engine(ExplodingSource(), destination, settings, run_id="restore-run")  # type: ignore[arg-type]
    report = engine.restore(
        RestoreRequest(
            repo=RepoRef(repo_id=pinned.repo_id, repo_type=RepoType.models, revision=COMMIT),
            dest=dest_dir,
        )
    )

    assert report.files == 2
    assert report.bytes == sum(len(blob) for blob in payloads.values())
    for path, blob in payloads.items():
        assert (dest_dir / path).read_bytes() == blob
    assert not list(dest_dir.rglob("*.aimm-part")), "no partial files may be left behind"


def test_a_restore_refuses_bytes_that_disagree_with_the_manifest(
    destination: S3Destination,
    settings: Settings,
    pinned: PinnedRepo,
    dest_dir: Any,
) -> None:
    """The manifest is the authority: corrupted stored bytes must never reach disk."""
    prefix = settings.s3.prefix
    path = "config.json"
    key = keys.file_key(prefix, RepoType.models, pinned.repo_id, COMMIT, path)
    destination.put_small(key, b"CORRUPTED", sha256=sha256_bytes(b"CORRUPTED"))

    honest = b'{"hidden_size": 4096}'
    document = build_manifest(
        pinned=pinned,
        results=[
            FileResult(
                path=path,
                key=key,
                size=len(honest),
                sha256=sha256_bytes(honest),
                sha256_source="computed",
                blob_id="b" * 40,
                xet_hash=None,
                is_lfs=False,
                etag="e" * 32,
                part_size=None,
                parts=1,
                transfer_path=TransferPath.inline,
                skipped=False,
                uploaded_at="2026-07-20T12:00:00Z",
            )
        ],
        settings=settings,
        key_root="root",
        selection=ManifestSelection(),
        tool_version="0.1.0",
        run_id="run",
    )
    data = document.to_json()
    destination.put_small(
        keys.manifest_key(prefix, RepoType.models, pinned.repo_id, COMMIT),
        data,
        sha256=sha256_bytes(data),
    )
    line = digest_line(data).encode()
    destination.put_small(
        keys.manifest_digest_key(prefix, RepoType.models, pinned.repo_id, COMMIT),
        line,
        sha256=sha256_bytes(line),
    )

    engine = Engine(ExplodingSource(), destination, settings)  # type: ignore[arg-type]
    with pytest.raises(ChecksumMismatchError):
        engine.restore(
            RestoreRequest(
                repo=RepoRef(repo_id=pinned.repo_id, repo_type=RepoType.models, revision=COMMIT),
                dest=dest_dir,
            )
        )
    assert not (dest_dir / path).exists(), "a corrupt file must not be left on disk"
    assert not list(dest_dir.rglob("*.aimm-part")), "the partial file must be cleaned up"
