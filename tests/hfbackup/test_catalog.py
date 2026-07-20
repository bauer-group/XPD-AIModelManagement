"""Tests for the catalog, against a seeded moto bucket.

The catalog derives everything from the key space itself rather than from a side index,
so these tests seed real objects and assert that what comes back describes them. The
interesting cases are the degraded ones: a revision with no manifest, a manifest whose
digest does not match, and an unreadable ref object — none of which may take the listing
down, because the catalog is what an operator reaches for when something is already wrong.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from bg_ai_model_management.config.models import Settings
from bg_ai_model_management.errors import ManifestError, ObjectNotFoundError
from bg_ai_model_management.integrity.hashing import sha256_bytes
from bg_ai_model_management.tools.hfbackup import catalog, keys
from bg_ai_model_management.tools.hfbackup.destination import S3Destination
from bg_ai_model_management.tools.hfbackup.manifest import (
    Manifest,
    ManifestSelection,
    build_manifest,
    digest_line,
)
from bg_ai_model_management.tools.hfbackup.retention import (
    RetentionPolicy,
    plan_retention,
)
from bg_ai_model_management.tools.hfbackup.types import (
    FileResult,
    PinnedRepo,
    RepoType,
    TransferPath,
)

PREFIX = "aimm"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def entry(path: str, key: str, blob: bytes) -> FileResult:
    return FileResult(
        path=path,
        key=key,
        size=len(blob),
        sha256=sha256_bytes(blob),
        sha256_source="computed",
        blob_id="0" * 40,
        xet_hash=None,
        is_lfs=False,
        etag="e" * 32,
        part_size=None,
        parts=1,
        transfer_path=TransferPath.inline,
        skipped=False,
        uploaded_at="2026-07-20T12:00:00Z",
    )


class Seeder:
    """Writes revisions, manifests and refs into the bucket the way the engine would."""

    def __init__(self, destination: S3Destination, settings: Settings) -> None:
        self.destination = destination
        self.settings = settings

    def revision(
        self,
        repo_id: str,
        commit_sha: str,
        *,
        repo_type: RepoType = RepoType.models,
        files: dict[str, bytes] | None = None,
        complete: bool = True,
        created_at: str | None = None,
    ) -> Manifest | None:
        payloads = files if files is not None else {"config.json": b"{}", "model.bin": b"weights"}
        results = []
        for path, blob in payloads.items():
            key = keys.file_key(PREFIX, repo_type, repo_id, commit_sha, path)
            upload = self.destination.put_small(key, blob, sha256=sha256_bytes(blob))
            # The real ETag, not a placeholder: `verify` compares the manifest against
            # head_object, so a seeded manifest with a fake ETag would report drift on a
            # perfectly healthy backup and make every exit-code test meaningless.
            results.append(dataclasses.replace(entry(path, key, blob), etag=upload.etag))
        if not complete:
            return None

        document = build_manifest(
            pinned=PinnedRepo(
                repo_id=repo_id,
                repo_type=repo_type,
                revision_requested="main",
                commit_sha=commit_sha,
            ),
            results=results,
            settings=self.settings,
            key_root=keys.revision_root(PREFIX, repo_type, repo_id, commit_sha),
            selection=ManifestSelection(),
            tool_version="0.1.0",
            run_id="seed",
        )
        if created_at is not None:
            document = document.model_copy(update={"created_at": created_at})
        data = document.to_json()
        self.destination.put_small(
            keys.manifest_key(PREFIX, repo_type, repo_id, commit_sha),
            data,
            sha256=sha256_bytes(data),
        )
        line = digest_line(data).encode()
        self.destination.put_small(
            keys.manifest_digest_key(PREFIX, repo_type, repo_id, commit_sha),
            line,
            sha256=sha256_bytes(line),
        )
        return document

    def ref(
        self,
        repo_id: str,
        name: str,
        commit_sha: str,
        *,
        repo_type: RepoType = RepoType.models,
        payload: bytes | None = None,
    ) -> None:
        body = payload if payload is not None else json.dumps({"commit_sha": commit_sha}).encode()
        self.destination.put_small(
            keys.ref_key(PREFIX, repo_type, repo_id, name), body, sha256=sha256_bytes(body)
        )


@pytest.fixture
def seeder(destination: S3Destination, settings: Settings) -> Seeder:
    return Seeder(destination, settings)


# ── list_repos ───────────────────────────────────────────────────────────────


def test_list_repos_on_an_empty_bucket(destination: S3Destination) -> None:
    assert catalog.list_repos(destination, PREFIX) == []


def test_list_repos_summarises_every_stored_repository(
    destination: S3Destination, seeder: Seeder
) -> None:
    seeder.revision("acme/small", SHA_A)
    seeder.revision("acme/small", SHA_B)
    seeder.revision("other/big", SHA_A, repo_type=RepoType.datasets)

    entries = catalog.list_repos(destination, PREFIX)

    assert [(item.repo_type, item.repo_id) for item in entries] == [
        (RepoType.datasets, "other/big"),
        (RepoType.models, "acme/small"),
    ], "results are sorted by (repo_type, repo_id)"
    models = entries[1]
    assert models.revisions == 2
    assert models.complete_revisions == 2
    assert models.total_bytes > 0
    assert models.latest_sha in {SHA_A, SHA_B}


def test_list_repos_counts_an_incomplete_revision_but_not_as_complete(
    destination: S3Destination, seeder: Seeder
) -> None:
    """An interrupted run leaves files with no manifest; the catalog must say so."""
    seeder.revision("acme/model", SHA_A)
    seeder.revision("acme/model", SHA_B, complete=False)

    [item] = catalog.list_repos(destination, PREFIX, repo_type=RepoType.models)
    assert item.revisions == 2
    assert item.complete_revisions == 1
    assert item.latest_sha == SHA_A, "only a complete revision can be the latest"


def test_list_repos_filters_by_type_and_owner(
    destination: S3Destination, seeder: Seeder
) -> None:
    seeder.revision("acme/one", SHA_A)
    seeder.revision("zeta/two", SHA_A)
    seeder.revision("acme/three", SHA_A, repo_type=RepoType.datasets)

    by_type = catalog.list_repos(destination, PREFIX, repo_type=RepoType.datasets)
    assert [item.repo_id for item in by_type] == ["acme/three"]

    by_owner = catalog.list_repos(destination, PREFIX, repo_type=RepoType.models, owner="acme")
    assert [item.repo_id for item in by_owner] == ["acme/one"]


def test_list_repos_reports_the_refs_pointing_at_each_repository(
    destination: S3Destination, seeder: Seeder
) -> None:
    seeder.revision("acme/model", SHA_A)
    seeder.ref("acme/model", "main", SHA_A)
    seeder.ref("acme/model", "v1.0", SHA_A)

    [item] = catalog.list_repos(destination, PREFIX, repo_type=RepoType.models)
    assert item.refs == {"main": SHA_A, "v1.0": SHA_A}


def test_list_repos_does_not_download_manifests(
    spy_destination: tuple[S3Destination, object], settings: Settings
) -> None:
    """Listing a 120 TB store must not pull one manifest per revision over the wire."""
    destination, spy = spy_destination
    Seeder(destination, settings).revision("acme/model", SHA_A)
    spy.calls.clear()  # type: ignore[attr-defined]

    catalog.list_repos(destination, PREFIX, repo_type=RepoType.models)

    fetched = [call["Key"] for call in spy.params("get_object")]  # type: ignore[attr-defined]
    assert not any(key.endswith("manifest.json") for key in fetched), (
        f"list_repos downloaded a manifest: {fetched}"
    )


# ── list_revisions ───────────────────────────────────────────────────────────


def test_list_revisions_prefers_the_manifest_totals(
    destination: S3Destination, seeder: Seeder
) -> None:
    document = seeder.revision(
        "acme/model", SHA_A, files={"a.bin": b"12345", "b.bin": b"678901234"}
    )
    assert document is not None

    [revision] = catalog.list_revisions(destination, PREFIX, RepoType.models, "acme/model")
    assert revision.commit_sha == SHA_A
    assert revision.complete is True
    assert revision.total_bytes == document.totals.bytes == 14
    assert revision.file_count == 2
    assert revision.created_at is not None


def test_list_revisions_falls_back_to_summing_keys_for_an_incomplete_revision(
    destination: S3Destination, seeder: Seeder
) -> None:
    """No manifest is exactly the signature of an interrupted run, not an error."""
    seeder.revision("acme/model", SHA_A, files={"x.bin": b"0123456789"}, complete=False)

    [revision] = catalog.list_revisions(destination, PREFIX, RepoType.models, "acme/model")
    assert revision.complete is False
    assert revision.file_count == 1
    assert revision.total_bytes == 10
    assert revision.created_at is not None, "the newest file's mtime stands in for a date"


def test_list_revisions_is_newest_first(destination: S3Destination, seeder: Seeder) -> None:
    seeder.revision("acme/model", SHA_A, created_at="2024-01-01T00:00:00Z")
    seeder.revision("acme/model", SHA_B, created_at="2026-01-01T00:00:00Z")
    seeder.revision("acme/model", SHA_C, created_at="2025-01-01T00:00:00Z")

    revisions = catalog.list_revisions(destination, PREFIX, RepoType.models, "acme/model")
    assert [rev.commit_sha for rev in revisions] == [SHA_B, SHA_C, SHA_A]


def test_a_revision_with_a_tampered_manifest_is_reported_as_incomplete(
    destination: S3Destination, seeder: Seeder
) -> None:
    """A manifest failing its own digest must not be trusted for size or count."""
    seeder.revision("acme/model", SHA_A, files={"x.bin": b"0123456789"})
    manifest_key = keys.manifest_key(PREFIX, RepoType.models, "acme/model", SHA_A)
    destination.put_small(manifest_key, b'{"manifest_version": 1}', sha256=sha256_bytes(b"x"))

    [revision] = catalog.list_revisions(destination, PREFIX, RepoType.models, "acme/model")
    assert revision.complete is False, "an unverifiable manifest is not a completeness marker"
    assert revision.total_bytes == 10, "the size falls back to the files/ keys"


def test_an_unparsable_created_at_does_not_break_the_listing(
    destination: S3Destination, seeder: Seeder
) -> None:
    seeder.revision("acme/model", SHA_A, created_at="not-a-timestamp")
    [revision] = catalog.list_revisions(destination, PREFIX, RepoType.models, "acme/model")
    assert revision.complete is True
    assert revision.created_at is None


def test_list_revisions_on_an_unknown_repository_is_empty(destination: S3Destination) -> None:
    assert catalog.list_revisions(destination, PREFIX, RepoType.models, "nobody/nothing") == []


# ── read_refs ────────────────────────────────────────────────────────────────


def test_read_refs_maps_names_to_shas(destination: S3Destination, seeder: Seeder) -> None:
    seeder.ref("acme/model", "main", SHA_A)
    seeder.ref("acme/model", "refs/pr/7", SHA_B)

    assert catalog.read_refs(destination, PREFIX, RepoType.models, "acme/model") == {
        "main": SHA_A,
        "refs/pr/7": SHA_B,
    }


def test_read_refs_is_empty_for_a_repository_with_none(destination: S3Destination) -> None:
    assert catalog.read_refs(destination, PREFIX, RepoType.models, "acme/model") == {}


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"{}",
        b'{"commit_sha": 42}',
        b"\xff\xfe",
        # A JSON array and a bare string both make payload["commit_sha"] raise TypeError
        # rather than KeyError. read_refs used to omit TypeError from its except clause,
        # so one malformed ref object took down `catalog list` and `prune --all-repos`
        # entirely. Fixed in catalog.py; these two stay as the regression guard.
        b"[]",
        b'"just-a-string"',
    ],
)
def test_an_unreadable_ref_object_is_skipped_not_fatal(
    destination: S3Destination, seeder: Seeder, payload: bytes
) -> None:
    """The catalog is what an operator uses when things are already broken.

    Its documented behaviour is to warn and continue past any ref object it cannot read,
    so no single corrupt sidecar may make the whole repository unlistable.
    """
    seeder.ref("acme/model", "good", SHA_A)
    seeder.ref("acme/model", "broken", SHA_B, payload=payload)

    refs = catalog.read_refs(destination, PREFIX, RepoType.models, "acme/model")
    assert refs == {"good": SHA_A}


def test_non_json_objects_under_refs_are_ignored(
    destination: S3Destination, seeder: Seeder
) -> None:
    seeder.ref("acme/model", "main", SHA_A)
    stray = keys.refs_prefix(PREFIX, RepoType.models, "acme/model").rstrip("/") + "/README.txt"
    destination.put_small(stray, b"hello", sha256=sha256_bytes(b"hello"))

    assert catalog.read_refs(destination, PREFIX, RepoType.models, "acme/model") == {"main": SHA_A}


# ── show ─────────────────────────────────────────────────────────────────────


def test_show_returns_a_digest_verified_manifest(
    destination: S3Destination, seeder: Seeder
) -> None:
    expected = seeder.revision("acme/model", SHA_A)
    assert expected is not None

    document = catalog.show(destination, PREFIX, RepoType.models, "acme/model", SHA_A)
    assert document == expected
    assert document.source.commit_sha == SHA_A


def test_show_raises_for_a_revision_with_no_manifest(
    destination: S3Destination, seeder: Seeder
) -> None:
    seeder.revision("acme/model", SHA_A, complete=False)
    with pytest.raises(ObjectNotFoundError):
        catalog.show(destination, PREFIX, RepoType.models, "acme/model", SHA_A)


def test_show_raises_when_the_digest_sidecar_is_missing(
    destination: S3Destination, seeder: Seeder
) -> None:
    seeder.revision("acme/model", SHA_A)
    destination.delete_keys([keys.manifest_digest_key(PREFIX, RepoType.models, "acme/model", SHA_A)])

    with pytest.raises(ManifestError) as excinfo:
        catalog.show(destination, PREFIX, RepoType.models, "acme/model", SHA_A)
    assert "no digest sidecar" in str(excinfo.value)


def test_show_rejects_a_manifest_that_fails_its_digest(
    destination: S3Destination, seeder: Seeder
) -> None:
    """The tamper-evidence contract: edited bytes must not be served as authoritative."""
    document = seeder.revision("acme/model", SHA_A)
    assert document is not None
    tampered = document.to_json().replace(b'"size": 7', b'"size": 6')
    destination.put_small(
        keys.manifest_key(PREFIX, RepoType.models, "acme/model", SHA_A),
        tampered,
        sha256=sha256_bytes(tampered),
    )

    with pytest.raises(ManifestError) as excinfo:
        catalog.show(destination, PREFIX, RepoType.models, "acme/model", SHA_A)
    assert "digest mismatch" in str(excinfo.value)


# ── timestamp normalisation ──────────────────────────────────────────────────


def test_a_naive_created_at_does_not_break_listing_or_pruning(
    destination: S3Destination, settings: Settings
) -> None:
    """Regression: one manifest without a trailing Z took down the whole bucket.

    `_parse_timestamp` returned a naive datetime, while `_incomplete_revision` derives
    `created_at` from boto3's aware `last_modified`. Sorting the two together raised
    `TypeError: can't compare offset-naive and offset-aware datetimes`, and because
    `scan_all` calls `future.result()` with no per-repo guard, a single hand-edited
    manifest anywhere under the prefix aborted `catalog revisions` and
    `prune --all-repos` for every repository — exactly when an operator needs them.
    """
    seeder = Seeder(destination, settings)
    seeder.revision("acme/model", SHA_A, created_at="2026-01-01T00:00:00Z")
    seeder.revision("acme/model", SHA_B, created_at="2025-01-01T00:00:00")  # no trailing Z
    seeder.revision("acme/model", SHA_C, complete=False)

    revisions = catalog.list_revisions(destination, PREFIX, RepoType.models, "acme/model")

    assert {rev.commit_sha for rev in revisions} == {SHA_A, SHA_B, SHA_C}
    moments = [rev.created_at for rev in revisions if rev.created_at is not None]
    assert moments, "no revision reported a created_at at all"
    assert all(moment.tzinfo is not None for moment in moments), (
        "every created_at must be timezone-aware or the retention comparisons explode"
    )
    # The retention planner sorts and compares these; it must not raise.
    plan = plan_retention(
        revisions,
        RetentionPolicy(keep_last=1, keep_within=timedelta(days=30)),
        now=datetime.now(UTC),
    )
    assert len(plan.keep) + len(plan.delete) == 3
