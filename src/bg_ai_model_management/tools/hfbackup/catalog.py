"""Inventory of what is actually stored in the bucket.

The key layout is self-describing, so the catalog is derived from the key space itself
rather than from a side index that could drift out of sync with reality. `list_repos`
walks with `Delimiter='/'` and never downloads a manifest; `list_revisions` reads them
because it reports per-revision sizes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bg_ai_model_management.errors import ManifestError, ObjectNotFoundError
from bg_ai_model_management.tools.hfbackup import keys
from bg_ai_model_management.tools.hfbackup.manifest import (
    MANIFEST_FILENAME,
    Manifest,
    verify_digest,
)
from bg_ai_model_management.tools.hfbackup.retention import RevisionInfo
from bg_ai_model_management.tools.hfbackup.types import RepoType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bg_ai_model_management.tools.hfbackup.destination import S3Destination

log = logging.getLogger(__name__)

_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    repo_type: RepoType
    repo_id: str
    revisions: int
    complete_revisions: int
    total_bytes: int
    latest_sha: str | None
    refs: dict[str, str]  # ref name -> sha


def list_repos(
    dest: S3Destination,
    prefix: str,
    *,
    repo_type: RepoType | None = None,
    owner: str | None = None,
) -> list[CatalogEntry]:
    """Summarise every stored repository. Never downloads manifests."""
    wanted = [repo_type] if repo_type is not None else list(RepoType)
    entries: list[CatalogEntry] = []
    for rtype in wanted:
        type_root = f"{prefix}/{keys.LAYOUT_VERSION}/{rtype.value}/"
        owners = [owner] if owner is not None else _children(dest, type_root)
        for owner_name in owners:
            for name in _children(dest, f"{type_root}{owner_name}/"):
                repo_id = f"{owner_name}/{name}"
                entries.append(_summarise(dest, prefix, rtype, repo_id))
    return sorted(entries, key=lambda entry: (entry.repo_type.value, entry.repo_id))


def list_revisions(
    dest: S3Destination,
    prefix: str,
    repo_type: RepoType,
    repo_id: str,
) -> list[RevisionInfo]:
    """List revisions with completeness, size and file count, newest first.

    Reads each `manifest.json` for authoritative size and count; falls back to summing
    the `files/` keys when the manifest is absent, which is exactly the signature of an
    interrupted run.
    """
    revisions: list[RevisionInfo] = []
    root = keys.revisions_prefix(prefix, repo_type, repo_id)
    for sha in _children(dest, root):
        revisions.append(_revision_info(dest, prefix, repo_type, repo_id, sha))
    return sorted(
        revisions,
        key=lambda rev: (rev.created_at or _EPOCH, rev.commit_sha),
        reverse=True,
    )


def read_refs(
    dest: S3Destination,
    prefix: str,
    repo_type: RepoType,
    repo_id: str,
) -> dict[str, str]:
    """Return a ref name -> commit sha mapping read from `refs/*.json`."""
    root = keys.refs_prefix(prefix, repo_type, repo_id)
    refs: dict[str, str] = {}
    for summary in dest.list_keys(root.rstrip("/") + "/"):
        if not summary.key.endswith(".json"):
            continue
        name = summary.key[len(root.rstrip("/")) + 1 : -len(".json")]
        try:
            payload = json.loads(dest.get_bytes(summary.key).decode("utf-8"))
            sha = payload["commit_sha"]
        # TypeError belongs here: a refs/*.json holding a JSON array or a bare string
        # makes payload["commit_sha"] raise TypeError, not KeyError. Without it a single
        # malformed ref object escapes the loop and takes down `catalog list` and
        # `prune --all-repos` entirely — precisely when an operator needs them most.
        except (
            ObjectNotFoundError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ) as exc:
            log.warning("skipping unreadable ref object %s: %s", summary.key, exc)
            continue
        if isinstance(sha, str):
            refs[name] = sha
    return refs


def show(
    dest: S3Destination,
    prefix: str,
    repo_type: RepoType,
    repo_id: str,
    commit_sha: str,
) -> Manifest:
    """Fetch one manifest and verify it against its digest sidecar.

    Raises:
        ObjectNotFoundError: the revision has no manifest (it is incomplete).
        ManifestError: the manifest is malformed or fails its digest check.
    """
    manifest_key = keys.manifest_key(prefix, repo_type, repo_id, commit_sha)
    digest_key = keys.manifest_digest_key(prefix, repo_type, repo_id, commit_sha)
    data = dest.get_bytes(manifest_key)
    try:
        line = dest.get_bytes(digest_key).decode("utf-8")
    except ObjectNotFoundError as exc:
        raise ManifestError(f"manifest at {manifest_key} has no digest sidecar") from exc
    verify_digest(data, line)
    return Manifest.from_json(data)


def _children(dest: S3Destination, parent: str) -> list[str]:
    """Return the immediate child names under a prefix, without the parent or slashes."""
    names: list[str] = []
    for child in dest.list_prefixes(parent):
        tail = child[len(parent) :] if child.startswith(parent) else child
        tail = tail.strip("/")
        if tail:
            names.append(tail)
    return sorted(names)


def _summarise(
    dest: S3Destination,
    prefix: str,
    repo_type: RepoType,
    repo_id: str,
) -> CatalogEntry:
    """Build one CatalogEntry from a single flat listing of the repo's revisions."""
    root = keys.revisions_prefix(prefix, repo_type, repo_id)
    total_bytes = 0
    shas: set[str] = set()
    complete: dict[str, datetime] = {}
    for summary in dest.list_keys(root):
        tail = summary.key[len(root) :]
        sha, _, rest = tail.partition("/")
        if not sha or not rest:
            continue
        shas.add(sha)
        total_bytes += summary.size
        if rest == MANIFEST_FILENAME:
            complete[sha] = summary.last_modified
    latest_sha = max(complete, key=lambda sha: complete[sha]) if complete else None
    return CatalogEntry(
        repo_type=repo_type,
        repo_id=repo_id,
        revisions=len(shas),
        complete_revisions=len(complete),
        total_bytes=total_bytes,
        latest_sha=latest_sha,
        refs=read_refs(dest, prefix, repo_type, repo_id),
    )


def _revision_info(
    dest: S3Destination,
    prefix: str,
    repo_type: RepoType,
    repo_id: str,
    commit_sha: str,
) -> RevisionInfo:
    """Describe one revision, preferring the manifest and falling back to raw keys."""
    try:
        manifest = show(dest, prefix, repo_type, repo_id, commit_sha)
    except (ObjectNotFoundError, ManifestError) as exc:
        log.debug("revision %s has no usable manifest (%s); summing keys instead", commit_sha, exc)
        return _incomplete_revision(dest, prefix, repo_type, repo_id, commit_sha)
    return RevisionInfo(
        commit_sha=commit_sha,
        complete=True,
        created_at=_parse_timestamp(manifest.created_at),
        total_bytes=manifest.totals.bytes,
        file_count=manifest.totals.files,
    )


def _incomplete_revision(
    dest: S3Destination,
    prefix: str,
    repo_type: RepoType,
    repo_id: str,
    commit_sha: str,
) -> RevisionInfo:
    root = keys.revision_root(prefix, repo_type, repo_id, commit_sha) + "/files/"
    total_bytes = 0
    file_count = 0
    newest: datetime | None = None
    for summary in dest.list_keys(root):
        total_bytes += summary.size
        file_count += 1
        if newest is None or summary.last_modified > newest:
            newest = summary.last_modified
    return RevisionInfo(
        commit_sha=commit_sha,
        complete=False,
        created_at=newest,
        total_bytes=total_bytes,
        file_count=file_count,
    )


def _parse_timestamp(value: str) -> datetime | None:
    """Parse an RFC 3339 UTC timestamp, tolerating a manifest written by a newer tool.

    The result is ALWAYS timezone-aware. `_incomplete_revision` derives `created_at` from
    boto3's aware `last_modified`, so a naive value here would poison every comparison
    they share: sorting `list_revisions` and evaluating `keep_within` both raise
    "can't compare offset-naive and offset-aware datetimes", and one hand-edited manifest
    would take down `catalog revisions` and `prune --all-repos` for the whole bucket.
    """
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        log.warning("manifest carries an unparsable created_at %r", value)
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
