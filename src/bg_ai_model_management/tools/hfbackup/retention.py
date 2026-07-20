"""Retention planning for stored revisions. Pure, injectable `now`, safe by construction.

`plan_retention` is a pure function: it decides, it never deletes. The caller performs
the deletion, which keeps the dangerous half trivially testable. Two structural guards
make it incapable of destroying the last good copy: the newest COMPLETE revision is
always kept regardless of policy, and a plan with no survivors at all is refused.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bg_ai_model_management.errors import RetentionRefusedError

_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class RevisionInfo:
    commit_sha: str
    complete: bool  # manifest.json present
    created_at: datetime | None
    total_bytes: int
    file_count: int


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    keep_last: int | None = None
    keep_within: timedelta | None = None
    keep_incomplete: bool = False
    protected: frozenset[str] = frozenset()  # SHAs referenced by refs/*.json
    #: Minimum age before an incomplete revision may be deleted. `None` means no grace,
    #: which is only safe when no sync can possibly be running. Callers must set this to
    #: something longer than a plausible sync: `created_at` for an incomplete revision is
    #: the mtime of its newest stored object, so a run that is still uploading looks
    #: seconds old, and deleting it destroys objects a live worker pool is about to
    #: record in a manifest.
    incomplete_grace: timedelta | None = None


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    keep: tuple[RevisionInfo, ...]
    delete: tuple[RevisionInfo, ...]
    protected: tuple[RevisionInfo, ...]


def plan_retention(
    revisions: Sequence[RevisionInfo],
    policy: RetentionPolicy,
    *,
    now: datetime,
) -> RetentionPlan:
    """Classify revisions into keep / delete / protected. Pure; `now` is injected.

    Rules:
        1. Any SHA in `policy.protected` is PROTECTED and never deleted.
        2. The newest COMPLETE revision is always kept, whatever the policy says.
        3. `keep_last=N` keeps the N newest complete revisions.
        4. `keep_within=D` keeps everything created within D of `now`.
        5. Incomplete revisions are deleted unless `keep_incomplete` is True. This is a
           subtraction applied after rules 3 and 4, so recency alone never preserves the
           debris of a crashed run — but rule 2 still protects the newest complete copy.
           An incomplete revision younger than `incomplete_grace` is exempt, because at
           that age it is indistinguishable from a sync that is still running: deleting
           the objects of a live run leaves its workers to finish and write a manifest
           describing thousands of files that no longer exist, which marks the revision
           COMPLETE and ref-protected while it is in fact unrestorable.
        6. Everything else is DELETE.

    Raises:
        RetentionRefusedError: no policy was given at all (an unconstrained prune is
            always a mistake), or the plan would leave nothing behind.
    """
    if policy.keep_last is None and policy.keep_within is None:
        raise RetentionRefusedError(
            "refusing to prune without a retention policy: set keep_last and/or keep_within"
        )

    ordered = sorted(revisions, key=lambda rev: rev.created_at or _EPOCH, reverse=True)
    protected_shas = {rev.commit_sha for rev in ordered if rev.commit_sha in policy.protected}
    protected = tuple(rev for rev in ordered if rev.commit_sha in protected_shas)
    candidates = [rev for rev in ordered if rev.commit_sha not in protected_shas]

    keep_shas: set[str] = set()

    # Rule 2 — evaluated over ALL revisions, so a protected newest copy needs no duplicate.
    newest_complete = next((rev for rev in ordered if rev.complete), None)
    if newest_complete is not None and newest_complete.commit_sha not in protected_shas:
        keep_shas.add(newest_complete.commit_sha)

    complete = [rev for rev in candidates if rev.complete]
    if policy.keep_last is not None:
        keep_shas.update(rev.commit_sha for rev in complete[: policy.keep_last])
    if policy.keep_within is not None:
        cutoff = now - policy.keep_within
        keep_shas.update(
            rev.commit_sha
            for rev in candidates
            if rev.created_at is not None and rev.created_at >= cutoff
        )
    if not policy.keep_incomplete:
        # Safe against rule 2: `newest_complete` is complete, so it is never in this set.
        incomplete = [rev for rev in candidates if not rev.complete]
        keep_shas -= {rev.commit_sha for rev in incomplete if _is_settled(rev, policy, now)}
        # A revision that may still be uploading survives whatever rules 3 and 4 decided.
        keep_shas |= {rev.commit_sha for rev in incomplete if not _is_settled(rev, policy, now)}

    keep = tuple(rev for rev in candidates if rev.commit_sha in keep_shas)
    delete = tuple(rev for rev in candidates if rev.commit_sha not in keep_shas)

    if delete and not keep and not protected:
        raise RetentionRefusedError(
            f"refusing a plan that would delete all {len(delete)} stored revision(s); "
            "no complete revision would survive"
        )
    return RetentionPlan(keep=keep, delete=delete, protected=protected)


def _is_settled(rev: RevisionInfo, policy: RetentionPolicy, now: datetime) -> bool:
    """True when an incomplete revision is old enough to be certainly abandoned.

    `created_at` for an incomplete revision is the mtime of its newest stored object, so
    a sync that is still uploading reads as a few seconds old. Anything inside the grace
    window may be a live run and is left alone; an unknown `created_at` means no object
    was found at all, which cannot be an in-flight upload.
    """
    if policy.incomplete_grace is None:
        return True
    if rev.created_at is None:
        return True
    return rev.created_at <= now - policy.incomplete_grace
