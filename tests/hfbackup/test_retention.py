"""Tests for retention planning.

`plan_retention` is the one pure function in this codebase whose bugs destroy data, so it
is tested twice over: once rule by rule against the contract, and once as a property —
across a large space of generated inputs, the newest complete revision is never selected
for deletion. `now` is injected everywhere; no test reads the clock.
"""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime, timedelta

import pytest

from bg_ai_model_management.errors import RetentionRefusedError
from bg_ai_model_management.tools.hfbackup.retention import (
    RetentionPolicy,
    RevisionInfo,
    plan_retention,
)

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)


def revision(
    sha: str,
    *,
    age: timedelta | None = None,
    complete: bool = True,
    created_at: datetime | None = None,
    total_bytes: int = 1024,
    file_count: int = 3,
) -> RevisionInfo:
    """Build a RevisionInfo positioned relative to the fixed NOW."""
    if created_at is None and age is not None:
        created_at = NOW - age
    return RevisionInfo(
        commit_sha=sha,
        complete=complete,
        created_at=created_at,
        total_bytes=total_bytes,
        file_count=file_count,
    )


def shas(revisions: tuple[RevisionInfo, ...]) -> set[str]:
    return {rev.commit_sha for rev in revisions}


# ── the refusal guards ───────────────────────────────────────────────────────


def test_an_unconstrained_prune_is_refused() -> None:
    """Rule: no policy at all is always a mistake, never 'delete everything'."""
    revisions = [revision("a", age=timedelta(days=1)), revision("b", age=timedelta(days=900))]
    with pytest.raises(RetentionRefusedError) as excinfo:
        plan_retention(revisions, RetentionPolicy(), now=NOW)
    assert "without a retention policy" in str(excinfo.value)


def test_an_unconstrained_prune_is_refused_even_with_no_revisions() -> None:
    with pytest.raises(RetentionRefusedError):
        plan_retention([], RetentionPolicy(keep_incomplete=True), now=NOW)


def test_an_empty_repository_yields_an_empty_plan() -> None:
    plan = plan_retention([], RetentionPolicy(keep_last=1), now=NOW)
    assert plan.keep == () and plan.delete == () and plan.protected == ()


def test_a_plan_that_would_leave_nothing_behind_is_refused() -> None:
    """The structural backstop: incomplete-only debris still may not be wiped blind."""
    revisions = [
        revision("a", age=timedelta(days=900), complete=False),
        revision("b", age=timedelta(days=800), complete=False),
    ]
    with pytest.raises(RetentionRefusedError) as excinfo:
        plan_retention(revisions, RetentionPolicy(keep_last=2), now=NOW)
    assert "no complete revision would survive" in str(excinfo.value)


# ── rule 1: protected refs ───────────────────────────────────────────────────


def test_a_sha_referenced_by_a_ref_is_protected_and_never_deleted() -> None:
    revisions = [
        revision("new", age=timedelta(days=1)),
        revision("old", age=timedelta(days=900)),
    ]
    plan = plan_retention(
        revisions,
        RetentionPolicy(keep_last=1, protected=frozenset({"old"})),
        now=NOW,
    )
    assert shas(plan.protected) == {"old"}
    assert shas(plan.delete) == set()
    assert shas(plan.keep) == {"new"}


def test_a_protected_revision_appears_only_in_protected() -> None:
    """The three buckets partition the input; a revision in two of them is a UI lie."""
    revisions = [revision("a", age=timedelta(days=1)), revision("b", age=timedelta(days=2))]
    plan = plan_retention(
        revisions, RetentionPolicy(keep_last=5, protected=frozenset({"a"})), now=NOW
    )
    assert shas(plan.protected) == {"a"}
    assert "a" not in shas(plan.keep)
    assert "a" not in shas(plan.delete)
    assert shas(plan.keep) | shas(plan.delete) | shas(plan.protected) == {"a", "b"}


def test_an_incomplete_revision_can_still_be_protected_by_a_ref() -> None:
    """Protection is checked before completeness, so a ref pins even a torn revision."""
    revisions = [
        revision("good", age=timedelta(days=1)),
        revision("torn", age=timedelta(days=900), complete=False),
    ]
    plan = plan_retention(
        revisions,
        RetentionPolicy(keep_last=1, protected=frozenset({"torn"})),
        now=NOW,
    )
    assert shas(plan.protected) == {"torn"}
    assert shas(plan.delete) == set()


# ── rule 2: the newest complete revision is untouchable ──────────────────────


def test_the_newest_complete_revision_survives_keep_last_zero_semantics() -> None:
    """Even the strictest policy expressible must not empty the repository."""
    revisions = [
        revision("newest", age=timedelta(days=500)),
        revision("older", age=timedelta(days=600)),
    ]
    plan = plan_retention(revisions, RetentionPolicy(keep_within=timedelta(days=1)), now=NOW)
    assert shas(plan.keep) == {"newest"}, "the last good copy is never a deletion candidate"
    assert shas(plan.delete) == {"older"}


def test_the_newest_complete_revision_survives_even_when_newer_debris_exists() -> None:
    """'Newest complete' must skip past incomplete revisions, not stop at the newest key."""
    revisions = [
        revision("debris", age=timedelta(hours=1), complete=False),
        revision("newest-good", age=timedelta(days=900)),
        revision("older-good", age=timedelta(days=901)),
    ]
    plan = plan_retention(revisions, RetentionPolicy(keep_within=timedelta(days=2)), now=NOW)
    assert "newest-good" in shas(plan.keep)
    assert shas(plan.delete) == {"debris", "older-good"}


def test_rule_two_does_not_duplicate_a_protected_newest_copy() -> None:
    """When the newest complete revision is already protected, it stays in `protected`."""
    revisions = [revision("only", age=timedelta(days=900))]
    plan = plan_retention(
        revisions,
        RetentionPolicy(keep_last=1, protected=frozenset({"only"})),
        now=NOW,
    )
    assert shas(plan.protected) == {"only"}
    assert plan.keep == ()
    assert plan.delete == ()


# ── rule 3: keep_last ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("keep_last", "expected_keep"),
    [
        (1, {"r0"}),
        (2, {"r0", "r1"}),
        (3, {"r0", "r1", "r2"}),
        (10, {"r0", "r1", "r2", "r3"}),
    ],
)
def test_keep_last_keeps_the_n_newest_complete_revisions(
    keep_last: int, expected_keep: set[str]
) -> None:
    revisions = [revision(f"r{index}", age=timedelta(days=index + 1)) for index in range(4)]
    plan = plan_retention(revisions, RetentionPolicy(keep_last=keep_last), now=NOW)
    assert shas(plan.keep) == expected_keep
    assert shas(plan.delete) == {"r0", "r1", "r2", "r3"} - expected_keep


def test_keep_last_counts_only_complete_revisions() -> None:
    """Incomplete debris must not consume a retention slot from a good copy."""
    revisions = [
        revision("torn", age=timedelta(days=1), complete=False),
        revision("good-1", age=timedelta(days=2)),
        revision("good-2", age=timedelta(days=3)),
    ]
    plan = plan_retention(revisions, RetentionPolicy(keep_last=2), now=NOW)
    assert shas(plan.keep) == {"good-1", "good-2"}
    assert shas(plan.delete) == {"torn"}


# ── rule 4: keep_within, evaluated against the injected now ──────────────────


def test_keep_within_is_measured_from_the_injected_now() -> None:
    revisions = [
        revision("fresh", age=timedelta(days=1)),
        revision("edge", age=timedelta(days=30)),
        revision("stale", age=timedelta(days=31)),
    ]
    plan = plan_retention(revisions, RetentionPolicy(keep_within=timedelta(days=30)), now=NOW)
    assert shas(plan.keep) == {"fresh", "edge"}, "the cutoff is inclusive"
    assert shas(plan.delete) == {"stale"}


def test_moving_now_forward_moves_the_cutoff() -> None:
    """The proof that `now` really is injected: the same input, two different verdicts."""
    revisions = [revision("a", age=timedelta(days=10)), revision("b", age=timedelta(days=20))]
    policy = RetentionPolicy(keep_within=timedelta(days=15))

    assert shas(plan_retention(revisions, policy, now=NOW).keep) == {"a"}
    later = plan_retention(revisions, policy, now=NOW + timedelta(days=100))
    assert shas(later.keep) == {"a"}, "rule 2 still saves the newest complete copy"
    assert shas(later.delete) == {"b"}


def test_a_revision_without_a_timestamp_is_not_covered_by_keep_within() -> None:
    """An undated revision cannot be proved recent, so recency must not save it."""
    revisions = [
        revision("dated", age=timedelta(hours=1)),
        revision("undated", created_at=None),
    ]
    plan = plan_retention(revisions, RetentionPolicy(keep_within=timedelta(days=30)), now=NOW)
    assert shas(plan.keep) == {"dated"}
    assert shas(plan.delete) == {"undated"}


def test_keep_last_and_keep_within_are_a_union_not_an_intersection() -> None:
    """Both policies are protective, so satisfying either is enough to survive."""
    revisions = [
        revision("recent", age=timedelta(days=1)),
        revision("also-recent", age=timedelta(days=2)),
        revision("old-but-in-last-3", age=timedelta(days=400)),
        revision("doomed", age=timedelta(days=500)),
    ]
    plan = plan_retention(
        revisions,
        RetentionPolicy(keep_last=3, keep_within=timedelta(days=7)),
        now=NOW,
    )
    assert shas(plan.keep) == {"recent", "also-recent", "old-but-in-last-3"}
    assert shas(plan.delete) == {"doomed"}


# ── rule 5: incomplete revisions ─────────────────────────────────────────────


def test_incomplete_revisions_are_deleted_even_when_recent() -> None:
    """Recency alone never preserves the debris of a crashed run."""
    revisions = [
        revision("good", age=timedelta(days=20)),
        revision("torn", age=timedelta(minutes=5), complete=False),
    ]
    plan = plan_retention(revisions, RetentionPolicy(keep_within=timedelta(days=30)), now=NOW)
    assert shas(plan.keep) == {"good"}
    assert shas(plan.delete) == {"torn"}


def test_keep_incomplete_preserves_recent_debris() -> None:
    revisions = [
        revision("good", age=timedelta(days=20)),
        revision("torn", age=timedelta(minutes=5), complete=False),
    ]
    plan = plan_retention(
        revisions,
        RetentionPolicy(keep_within=timedelta(days=30), keep_incomplete=True),
        now=NOW,
    )
    assert shas(plan.keep) == {"good", "torn"}
    assert plan.delete == ()


def test_keep_incomplete_does_not_rescue_debris_outside_every_policy() -> None:
    """`keep_incomplete` disables a subtraction; it is not a blanket amnesty."""
    revisions = [
        revision("good", age=timedelta(days=1)),
        revision("ancient-torn", age=timedelta(days=900), complete=False),
    ]
    plan = plan_retention(
        revisions,
        RetentionPolicy(keep_within=timedelta(days=30), keep_incomplete=True),
        now=NOW,
    )
    assert shas(plan.keep) == {"good"}
    assert shas(plan.delete) == {"ancient-torn"}


# ── the safety property, over a generated input space ────────────────────────


def all_revisions(plan_keep: tuple[RevisionInfo, ...], *rest: tuple[RevisionInfo, ...]) -> set[str]:
    return shas(plan_keep).union(*(shas(bucket) for bucket in rest))


@pytest.mark.parametrize("seed", range(60))
def test_the_newest_complete_revision_is_never_selected_for_deletion(seed: int) -> None:
    """The one property that matters: a prune can never destroy the last good copy.

    Random policies are thrown at random repositories. Whatever the policy says, if a
    complete revision exists at all, the newest one must survive — in `keep` or in
    `protected`, but never in `delete`.
    """
    rng = random.Random(seed)
    count = rng.randint(1, 8)
    revisions = [
        revision(
            f"sha{index}",
            age=timedelta(hours=rng.randint(0, 24 * 800)),
            complete=rng.random() < 0.6,
        )
        for index in range(count)
    ]
    policy = RetentionPolicy(
        keep_last=rng.choice([None, 1, 2, 3]),
        keep_within=rng.choice([None, timedelta(days=1), timedelta(days=30)]),
        keep_incomplete=rng.random() < 0.5,
        protected=frozenset(
            rev.commit_sha for rev in revisions if rng.random() < 0.2
        ),
    )
    if policy.keep_last is None and policy.keep_within is None:
        pytest.skip("an unconstrained policy is refused by design and tested separately")

    try:
        plan = plan_retention(revisions, policy, now=NOW)
    except RetentionRefusedError:
        return  # refusing is always safe

    complete = [rev for rev in revisions if rev.complete]
    if not complete:
        return
    newest = max(complete, key=lambda rev: rev.created_at or datetime.min.replace(tzinfo=UTC))
    assert newest.commit_sha not in shas(plan.delete), (
        f"policy {policy} selected the newest complete revision {newest.commit_sha} for deletion"
    )
    # And the three buckets must always partition the input exactly.
    assert all_revisions(plan.keep, plan.delete, plan.protected) == {
        rev.commit_sha for rev in revisions
    }
    assert len(plan.keep) + len(plan.delete) + len(plan.protected) == len(revisions)


@pytest.mark.parametrize(
    ("keep_last", "keep_within_days", "keep_incomplete"),
    list(itertools.product([None, 1, 2], [None, 1, 365], [False, True])),
)
def test_no_policy_combination_deletes_the_only_complete_revision(
    keep_last: int | None, keep_within_days: int | None, keep_incomplete: bool
) -> None:
    """Exhaustive over the policy surface, against a repo holding exactly one good copy."""
    revisions = [
        revision("lonely-good", age=timedelta(days=900)),
        revision("torn-a", age=timedelta(days=1), complete=False),
        revision("torn-b", age=timedelta(days=2), complete=False),
    ]
    policy = RetentionPolicy(
        keep_last=keep_last,
        keep_within=timedelta(days=keep_within_days) if keep_within_days else None,
        keep_incomplete=keep_incomplete,
    )
    if keep_last is None and keep_within_days is None:
        with pytest.raises(RetentionRefusedError):
            plan_retention(revisions, policy, now=NOW)
        return

    plan = plan_retention(revisions, policy, now=NOW)
    assert "lonely-good" in shas(plan.keep)
    assert "lonely-good" not in shas(plan.delete)


def test_the_plan_never_invents_or_loses_a_revision() -> None:
    revisions = [
        revision(f"sha{index}", age=timedelta(days=index), complete=index % 3 != 0)
        for index in range(12)
    ]
    plan = plan_retention(
        revisions,
        RetentionPolicy(keep_last=2, keep_within=timedelta(days=4), protected=frozenset({"sha7"})),
        now=NOW,
    )
    assert len(plan.keep) + len(plan.delete) + len(plan.protected) == len(revisions)
    assert all_revisions(plan.keep, plan.delete, plan.protected) == {
        rev.commit_sha for rev in revisions
    }


# ── rule 5, grace period: never delete a sync that is still running ──────────


def test_an_in_flight_incomplete_revision_is_not_deleted() -> None:
    """Regression: prune destroyed the objects of a sync that was still uploading.

    A 700 GB sync starting at 22:00 and still running at 03:00 has no manifest yet, so
    it classifies as incomplete; `created_at` is the mtime of its newest stored object,
    which is seconds old. The nightly `prune --all-repos --keep-last 3 --yes` used to
    delete it unconditionally. The sync's workers then finished with no errors, so a
    manifest was written describing thousands of files that no longer existed — a
    revision marked COMPLETE and ref-protected that restore cannot satisfy.
    """
    revisions = [
        revision("good", age=timedelta(days=20)),
        revision("in-flight", age=timedelta(minutes=5), complete=False),
    ]
    plan = plan_retention(
        revisions,
        RetentionPolicy(keep_last=3, incomplete_grace=timedelta(hours=24)),
        now=NOW,
    )
    assert shas(plan.keep) == {"good", "in-flight"}
    assert plan.delete == ()


def test_an_incomplete_revision_older_than_the_grace_is_still_deleted() -> None:
    """The grace protects live runs, not the debris of a crash three days ago."""
    revisions = [
        revision("good", age=timedelta(days=20)),
        revision("abandoned", age=timedelta(days=3), complete=False),
    ]
    plan = plan_retention(
        revisions,
        RetentionPolicy(keep_last=3, incomplete_grace=timedelta(hours=24)),
        now=NOW,
    )
    assert shas(plan.keep) == {"good"}
    assert shas(plan.delete) == {"abandoned"}


def test_an_incomplete_revision_with_no_objects_at_all_is_deleted() -> None:
    """`created_at is None` means nothing was found under files/, so nothing is in flight."""
    revisions = [
        revision("good", age=timedelta(days=20)),
        revision("empty", created_at=None, complete=False),
    ]
    plan = plan_retention(
        revisions,
        RetentionPolicy(keep_last=3, incomplete_grace=timedelta(hours=24)),
        now=NOW,
    )
    assert shas(plan.delete) == {"empty"}


def test_the_grace_period_does_not_rescue_a_complete_revision() -> None:
    """It is a guard against a live writer, not a second recency rule."""
    revisions = [
        revision("newest", age=timedelta(minutes=1)),
        revision("older", age=timedelta(days=400)),
    ]
    plan = plan_retention(
        revisions,
        RetentionPolicy(keep_last=1, incomplete_grace=timedelta(days=999)),
        now=NOW,
    )
    assert shas(plan.keep) == {"newest"}
    assert shas(plan.delete) == {"older"}
