"""Tests for the transfer planner.

`choose_part_size` and `choose_path` are pure, so the whole auto-hybrid decision table is
driven from a literal table transcribed from the contract's normative pseudocode. The
stateful half — `DiskBudget` and `StreamFailureTracker` — is exercised with real threads,
because both exist solely to be correct under the engine's worker pool.
"""

from __future__ import annotations

import math
import threading
from pathlib import Path

import pytest

from bg_ai_model_management.config.models import TransferSettings
from bg_ai_model_management.errors import InsufficientDiskSpaceError, ObjectTooLargeError
from bg_ai_model_management.tools.hfbackup.planner import (
    MAX_PARTS,
    PART_MAX,
    PART_MIN,
    DiskBudget,
    StreamFailureTracker,
    choose_part_size,
    choose_path,
)
from bg_ai_model_management.tools.hfbackup.types import SourceFile, TransferMode, TransferPath

KIB = 1024
MIB = 1024**2
GIB = 1024**3

DEFAULT_PART = 8 * MIB
DEFAULT_MEMORY = 64 * MIB


def source_file(
    *,
    size: int,
    path: str = "model.safetensors",
    xet_hash: str | None = None,
    is_lfs: bool = True,
) -> SourceFile:
    return SourceFile(
        path=path,
        size=size,
        blob_id="0" * 40,
        sha256="a" * 64 if is_lfs else None,
        xet_hash=xet_hash,
        is_lfs=is_lfs,
    )


# ── choose_part_size ─────────────────────────────────────────────────────────


def test_constants_match_the_s3_limits() -> None:
    """The three limits are S3's, not ours; drifting from them corrupts every upload."""
    assert PART_MIN == 5 * MIB
    assert PART_MAX == 5 * GIB
    assert MAX_PARTS == 10_000


@pytest.mark.parametrize(
    ("size", "configured", "max_part_memory", "expected"),
    [
        # Small object: the configured size is used verbatim.
        (1 * KIB, DEFAULT_PART, DEFAULT_MEMORY, DEFAULT_PART),
        (0, DEFAULT_PART, DEFAULT_MEMORY, DEFAULT_PART),
        # A configured size below the S3 floor is raised to the floor.
        (1 * MIB, 1 * MIB, DEFAULT_MEMORY, PART_MIN),
        (1 * MIB, 0, DEFAULT_MEMORY, PART_MIN),
        # Exactly MAX_PARTS parts still fits, so no doubling happens.
        (DEFAULT_PART * MAX_PARTS, DEFAULT_PART, DEFAULT_MEMORY, DEFAULT_PART),
        # One byte more needs 10_001 parts, so the size doubles once.
        (DEFAULT_PART * MAX_PARTS + 1, DEFAULT_PART, DEFAULT_MEMORY, 2 * DEFAULT_PART),
        # Two doublings.
        (2 * DEFAULT_PART * MAX_PARTS + 1, DEFAULT_PART, DEFAULT_MEMORY, 4 * DEFAULT_PART),
        # The largest object admissible under the 64 MiB default: 625 GiB.
        (DEFAULT_MEMORY * MAX_PARTS, DEFAULT_PART, DEFAULT_MEMORY, DEFAULT_MEMORY),
        # A generous memory ceiling lets the part size grow well past the default.
        (DEFAULT_MEMORY * MAX_PARTS + 1, DEFAULT_PART, 1 * GIB, 2 * DEFAULT_MEMORY),
    ],
)
def test_choose_part_size_table(
    size: int, configured: int, max_part_memory: int, expected: int
) -> None:
    assert choose_part_size(size, configured, max_part_memory=max_part_memory) == expected


def test_choose_part_size_rejects_an_object_beyond_the_memory_ceiling() -> None:
    """625 GiB + 1 byte needs 128 MiB parts, which the 64 MiB default forbids."""
    with pytest.raises(ObjectTooLargeError) as excinfo:
        choose_part_size(DEFAULT_MEMORY * MAX_PARTS + 1, DEFAULT_PART, max_part_memory=DEFAULT_MEMORY)
    assert str(DEFAULT_MEMORY) in str(excinfo.value)


def test_choose_part_size_ceiling_is_the_min_of_part_max_and_memory() -> None:
    """`max_part_memory` is a real limit, not a formality: it can bind before PART_MAX."""
    huge = PART_MAX * MAX_PARTS
    assert choose_part_size(huge, PART_MAX, max_part_memory=PART_MAX) == PART_MAX
    with pytest.raises(ObjectTooLargeError) as excinfo:
        choose_part_size(huge, PART_MAX, max_part_memory=DEFAULT_MEMORY)
    assert str(DEFAULT_MEMORY) in str(excinfo.value), "the memory ceiling must be the one reported"


def test_admissible_part_sizes_are_doublings_of_the_configured_size() -> None:
    """The search doubles, so every result is `max(configured, PART_MIN) * 2**k`.

    This is why the largest admissible object depends on the configured part size and not
    only on `max_part_memory`: from an 8 MiB default the lattice lands exactly on 64 MiB,
    whereas from 5 MiB it steps 5 -> 10 -> 20 -> 40 -> 80 MiB and overshoots the same
    ceiling. Nothing is wrong with that, but a caller reasoning in round numbers will
    guess incorrectly, so it is pinned here.
    """
    base = max(DEFAULT_PART, PART_MIN)
    for size in (DEFAULT_PART, DEFAULT_PART * MAX_PARTS + 1, 400 * GIB):
        part_size = choose_part_size(size, DEFAULT_PART, max_part_memory=DEFAULT_MEMORY)
        assert part_size % base == 0
        assert (part_size // base) & (part_size // base - 1) == 0, "must be a power of two"

    # 625 GiB is admissible from an 8 MiB configured size, and not from 5 MiB.
    assert choose_part_size(DEFAULT_MEMORY * MAX_PARTS, DEFAULT_PART, max_part_memory=DEFAULT_MEMORY)
    with pytest.raises(ObjectTooLargeError):
        choose_part_size(DEFAULT_MEMORY * MAX_PARTS, PART_MIN, max_part_memory=DEFAULT_MEMORY)


@pytest.mark.parametrize(
    "size",
    [
        0,
        1,
        5 * MIB - 1,
        DEFAULT_PART,
        DEFAULT_PART * MAX_PARTS,
        DEFAULT_PART * MAX_PARTS + 1,
        100 * GIB,
        DEFAULT_MEMORY * MAX_PARTS,
        DEFAULT_MEMORY * MAX_PARTS + 1,
    ],
)
@pytest.mark.parametrize("configured", [1, PART_MIN, DEFAULT_PART, 32 * MIB])
def test_choose_part_size_invariants_hold_everywhere(size: int, configured: int) -> None:
    """The total contract: either a refusal, or a part size honouring both S3 limits.

    Asserted over the whole cross product rather than a hand-picked list, so a future
    change to the search cannot quietly return a 4 MiB part or a 10 001-part plan for
    some input nobody thought to enumerate.
    """
    try:
        part_size = choose_part_size(size, configured, max_part_memory=DEFAULT_MEMORY)
    except ObjectTooLargeError:
        return  # refusing is always an admissible answer
    assert part_size >= PART_MIN, "a part below 5 MiB is rejected by S3"
    assert part_size <= min(PART_MAX, DEFAULT_MEMORY), "a part may never exceed the ceiling"
    assert math.ceil(size / part_size) <= MAX_PARTS, "S3 refuses more than 10 000 parts"


def test_choose_part_size_never_returns_a_size_needing_more_than_max_parts() -> None:
    """Sweep the powers of two: the loop must terminate below the ceiling or refuse."""
    admitted = 0
    for exponent in range(20, 50):
        size = 1 << exponent
        try:
            part_size = choose_part_size(size, DEFAULT_PART, max_part_memory=DEFAULT_MEMORY)
        except ObjectTooLargeError:
            continue
        admitted += 1
        assert math.ceil(size / part_size) <= MAX_PARTS
    assert admitted > 0, "the sweep must actually exercise the success path"


# ── choose_path ──────────────────────────────────────────────────────────────


@pytest.fixture
def roomy_budget() -> DiskBudget:
    return DiskBudget(100 * GIB)


def decide(
    file: SourceFile,
    budget: DiskBudget,
    *,
    mode: TransferMode = TransferMode.auto,
    inline_max: int = DEFAULT_PART,
    part_size: int = DEFAULT_PART,
    max_part_memory: int = DEFAULT_MEMORY,
    prefer_xet: bool = False,
    stream_failures: int = 0,
    stream_failure_downgrade: int = 2,
) -> TransferPath:
    return choose_path(
        file,
        mode=mode,
        inline_max=inline_max,
        part_size=part_size,
        max_part_memory=max_part_memory,
        prefer_xet=prefer_xet,
        stream_failures=stream_failures,
        stream_failure_downgrade=stream_failure_downgrade,
        budget=budget,
    )


def test_mode_disk_forces_disk_even_for_a_tiny_file(roomy_budget: DiskBudget) -> None:
    """Rule 1 precedes the inline rule: an explicit DISK mode wins over everything."""
    assert decide(source_file(size=1), roomy_budget, mode=TransferMode.disk) is TransferPath.disk


def test_an_unsized_file_goes_to_disk(roomy_budget: DiskBudget) -> None:
    """Rule 2: without a size there is no part size, so streaming is impossible."""
    unsized = SourceFile(
        path="mystery.bin",
        size=None,  # type: ignore[arg-type]
        blob_id="0" * 40,
        sha256=None,
        xet_hash=None,
        is_lfs=False,
    )
    assert decide(unsized, roomy_budget) is TransferPath.disk


@pytest.mark.parametrize("mode", list(TransferMode))
def test_small_files_go_inline_in_every_mode_except_disk(
    mode: TransferMode, roomy_budget: DiskBudget
) -> None:
    """Rule 3 sits above the mode split for AUTO and STREAM, but below explicit DISK."""
    path = decide(source_file(size=DEFAULT_PART), roomy_budget, mode=mode)
    assert path is (TransferPath.disk if mode is TransferMode.disk else TransferPath.inline)


def test_inline_boundary_is_inclusive(roomy_budget: DiskBudget) -> None:
    assert decide(source_file(size=DEFAULT_PART), roomy_budget) is TransferPath.inline
    assert decide(source_file(size=DEFAULT_PART + 1), roomy_budget) is TransferPath.stream


def test_mode_stream_never_downgrades(roomy_budget: DiskBudget) -> None:
    """Rule 4: an explicit STREAM mode opts out of the failure downgrade entirely."""
    file = source_file(size=100 * MIB)
    assert (
        decide(file, roomy_budget, mode=TransferMode.stream, stream_failures=99)
        is TransferPath.stream
    )


@pytest.mark.parametrize(
    ("stream_failures", "downgrade", "expected"),
    [
        (0, 2, TransferPath.stream),
        (1, 2, TransferPath.stream),
        (2, 2, TransferPath.disk),
        (3, 2, TransferPath.disk),
        (0, 1, TransferPath.stream),
        (1, 1, TransferPath.disk),
    ],
)
def test_stream_downgrades_to_disk_after_repeated_failures(
    stream_failures: int,
    downgrade: int,
    expected: TransferPath,
    roomy_budget: DiskBudget,
) -> None:
    """Rule 5. STREAM cannot rewind a torn body, so persistent failure means staging."""
    file = source_file(size=100 * MIB)
    path = decide(
        file,
        roomy_budget,
        stream_failures=stream_failures,
        stream_failure_downgrade=downgrade,
    )
    assert path is expected


def test_prefer_xet_takes_the_disk_path_when_the_file_carries_a_xet_hash(
    roomy_budget: DiskBudget,
) -> None:
    """Rule 6: hf-xet accelerates downloads to disk only; streaming is a plain HTTP GET."""
    file = source_file(size=100 * MIB, xet_hash="x" * 64)
    assert decide(file, roomy_budget, prefer_xet=True) is TransferPath.disk


def test_prefer_xet_is_ignored_without_a_xet_hash(roomy_budget: DiskBudget) -> None:
    file = source_file(size=100 * MIB, xet_hash=None)
    assert decide(file, roomy_budget, prefer_xet=True) is TransferPath.stream


def test_prefer_xet_is_ignored_when_the_disk_budget_cannot_hold_the_file() -> None:
    """The budget is consulted before promising the DISK path, not after."""
    file = source_file(size=100 * MIB, xet_hash="x" * 64)
    tiny = DiskBudget(1 * MIB)
    assert decide(file, tiny, prefer_xet=True) is TransferPath.stream


def test_an_object_too_large_to_stream_falls_back_to_disk(roomy_budget: DiskBudget) -> None:
    """Rule 7: no admissible in-memory part size means the bytes must go via disk."""
    file = source_file(size=DEFAULT_MEMORY * MAX_PARTS + 1)
    assert decide(file, roomy_budget) is TransferPath.disk


def test_the_default_auto_decision_is_stream(roomy_budget: DiskBudget) -> None:
    """Rule 8: the fall-through. Streaming never touches disk, so it is the default."""
    assert decide(source_file(size=100 * MIB), roomy_budget) is TransferPath.stream


def test_choose_path_is_pure(roomy_budget: DiskBudget) -> None:
    """Repeated calls with identical inputs must not consume budget or shift state."""
    file = source_file(size=100 * MIB, xet_hash="x" * 64)
    before = roomy_budget.available
    decisions = {decide(file, roomy_budget, prefer_xet=True) for _ in range(10)}
    assert decisions == {TransferPath.disk}
    assert roomy_budget.available == before


# ── DiskBudget ───────────────────────────────────────────────────────────────


def test_disk_budget_reports_its_total_and_availability() -> None:
    budget = DiskBudget(10 * MIB)
    assert budget.total == 10 * MIB
    assert budget.available == 10 * MIB


def test_a_negative_budget_is_clamped_to_zero() -> None:
    """`free - disk_reserve` goes negative on a full disk; that must mean 'nothing', not debt."""
    budget = DiskBudget(-5 * GIB)
    assert budget.total == 0
    assert budget.available == 0
    assert not budget.can_reserve(1)


@pytest.mark.parametrize(
    ("total", "wanted", "expected"),
    [(10, 10, True), (10, 11, False), (10, 0, True), (0, 1, False)],
)
def test_can_reserve_answers_against_the_total_not_the_free_space(
    total: int, wanted: int, expected: bool
) -> None:
    budget = DiskBudget(total)
    assert budget.can_reserve(wanted) is expected


def test_reserve_refuses_to_overcommit() -> None:
    """A file larger than the whole budget can never be staged, so fail immediately."""
    budget = DiskBudget(10 * MIB)
    with pytest.raises(InsufficientDiskSpaceError) as excinfo, budget.reserve(10 * MIB + 1):
        pytest.fail("the budget must refuse a file it can never hold")
    assert "cannot hold" in str(excinfo.value)
    assert budget.available == 10 * MIB, "a refused reservation must consume nothing"


def test_reserve_releases_on_a_normal_exit() -> None:
    budget = DiskBudget(10 * MIB)
    with budget.reserve(4 * MIB):
        assert budget.available == 6 * MIB
    assert budget.available == 10 * MIB


def test_reserve_releases_on_an_exception() -> None:
    """A failed transfer must not leak its reservation, or the pool deadlocks."""
    budget = DiskBudget(10 * MIB)
    with pytest.raises(RuntimeError), budget.reserve(4 * MIB):
        raise RuntimeError("transfer blew up")
    assert budget.available == 10 * MIB


def test_reserve_times_out_rather_than_blocking_forever() -> None:
    """With 8 of 10 MiB held, a second 8 MiB request can only ever time out."""
    budget = DiskBudget(10 * MIB)
    with budget.reserve(8 * MIB):
        with pytest.raises(InsufficientDiskSpaceError) as excinfo:  # noqa: SIM117
            with budget.reserve(8 * MIB, timeout=0.05):
                pytest.fail("there was never room for a second 8 MiB reservation")
        assert "timed out" in str(excinfo.value)
    assert budget.available == 10 * MIB


def test_concurrent_reservations_never_exceed_the_budget() -> None:
    """The real contract: with N threads competing, the budget is never overdrawn."""
    budget = DiskBudget(100)
    lock = threading.Lock()
    in_flight = 0
    peak = 0
    errors: list[BaseException] = []

    def worker() -> None:
        nonlocal in_flight, peak
        try:
            for _ in range(20):
                with budget.reserve(40):
                    with lock:
                        in_flight += 40
                        peak = max(peak, in_flight)
                    with lock:
                        in_flight -= 40
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, f"a worker failed: {errors[0]!r}"
    assert not any(thread.is_alive() for thread in threads), "a worker deadlocked"
    assert peak <= 100, f"the budget was overdrawn: {peak} bytes in flight against 100"
    assert budget.available == 100, "every reservation must be released"


def test_a_waiting_reservation_is_woken_when_space_frees_up() -> None:
    budget = DiskBudget(10)
    acquired = threading.Event()

    def waiter() -> None:
        with budget.reserve(10, timeout=10):
            acquired.set()

    with budget.reserve(10):
        thread = threading.Thread(target=waiter)
        thread.start()
        assert not acquired.wait(timeout=0.2), "the waiter must block while the budget is full"
    thread.join(timeout=10)
    assert acquired.is_set(), "the waiter must be woken once the space is released"


def test_from_settings_subtracts_the_reserve(tmp_path: Path) -> None:
    transfer = TransferSettings(disk_reserve=0, max_disk_bytes=None)
    budget = DiskBudget.from_settings(transfer, tmp_path)
    assert budget.total > 0

    reserved = DiskBudget.from_settings(TransferSettings(disk_reserve=1 * MIB), tmp_path)
    assert reserved.total == budget.total - 1 * MIB


def test_from_settings_honours_the_hard_cap(tmp_path: Path) -> None:
    """`max_disk_bytes` is a ceiling, so it must win whenever the disk is roomier."""
    transfer = TransferSettings(disk_reserve=0, max_disk_bytes=7 * MIB)
    assert DiskBudget.from_settings(transfer, tmp_path).total == 7 * MIB


# ── StreamFailureTracker ─────────────────────────────────────────────────────


def test_tracker_counts_per_path() -> None:
    tracker = StreamFailureTracker()
    assert tracker.count("a.bin") == 0
    assert tracker.record("a.bin") == 1
    assert tracker.record("a.bin") == 2
    assert tracker.count("a.bin") == 2
    assert tracker.count("b.bin") == 0, "counters must not bleed between files"


def test_tracker_is_thread_safe() -> None:
    """The engine records failures from the worker pool, so a lost update is a real bug."""
    tracker = StreamFailureTracker()

    def worker() -> None:
        for _ in range(200):
            tracker.record("shared.bin")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert tracker.count("shared.bin") == 8 * 200
