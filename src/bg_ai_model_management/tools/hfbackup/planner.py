"""Transfer planning: part sizing, the auto-hybrid path rule, and disk accounting.

`choose_part_size` and `choose_path` are pure functions with no I/O, so the whole
auto-hybrid decision table is unit-testable without a network or a filesystem.
`DiskBudget` and `StreamFailureTracker` carry the only mutable state, and both are
thread-safe because the engine drives them from a worker pool.
"""

from __future__ import annotations

import math
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from bg_ai_model_management.errors import InsufficientDiskSpaceError, ObjectTooLargeError
from bg_ai_model_management.tools.hfbackup.types import SourceFile, TransferMode, TransferPath

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps planner free of config imports
    from bg_ai_model_management.config.models import TransferSettings

PART_MIN: int = 5 * 1024**2  # s3transfer.utils.MIN_UPLOAD_CHUNKSIZE
PART_MAX: int = 5 * 1024**3  # s3transfer.utils.MAX_SINGLE_UPLOAD_SIZE
MAX_PARTS: int = 10_000  # s3transfer.utils.MAX_PARTS


def choose_part_size(size: int, configured: int, *, max_part_memory: int) -> int:
    """Return the smallest admissible multipart part size for an object of `size`.

    Starts at `max(configured, PART_MIN)` and doubles until the object fits into
    `MAX_PARTS` parts. The ceiling is `min(PART_MAX, max_part_memory)`: `max_part_memory`
    bounds what a single worker may hold in RAM on the STREAM path, so it is a real
    limit, not a formality. With the 64 MiB default this admits objects up to ~625 GiB.

    Raises:
        ObjectTooLargeError: no admissible part size exists under the current limits.
    """
    ceiling = min(PART_MAX, max_part_memory)
    part_size = max(configured, PART_MIN)
    while math.ceil(size / part_size) > MAX_PARTS:
        part_size *= 2
    if part_size > ceiling:
        raise ObjectTooLargeError(
            f"object of {size} bytes needs a part size of {part_size} bytes, "
            f"which exceeds the limit of {ceiling} bytes "
            f"(min of PART_MAX and transfer.max_part_memory)"
        )
    return part_size


def choose_path(
    file: SourceFile,
    *,
    mode: TransferMode,
    inline_max: int,
    part_size: int,
    max_part_memory: int,
    prefer_xet: bool,
    stream_failures: int,
    stream_failure_downgrade: int,
    budget: DiskBudget,
) -> TransferPath:
    """Decide INLINE / STREAM / DISK for one file. Pure function — no I/O.

    Three paths, not two: INLINE is a single PutObject for small files, STREAM is the
    default (Hub -> memory -> multipart, never touching disk), and DISK is the fallback
    that stages the file locally. STREAM cannot resume a torn body, so after
    `stream_failure_downgrade` failures the file is demoted to DISK, where the Hub
    download is paid exactly once.
    """
    if mode is TransferMode.disk:
        return TransferPath.disk
    if file.size is None:  # defensive: an unsized file cannot be part-sized
        return TransferPath.disk
    if file.size <= inline_max:
        return TransferPath.inline
    if mode is TransferMode.stream:
        return TransferPath.stream
    # mode is AUTO from here on.
    if stream_failures >= stream_failure_downgrade:
        return TransferPath.disk
    if prefer_xet and file.xet_hash and budget.can_reserve(file.size):
        # hf-xet only accelerates downloads to disk; streaming is a plain HTTP GET.
        return TransferPath.disk
    try:
        choose_part_size(file.size, part_size, max_part_memory=max_part_memory)
    except ObjectTooLargeError:
        return TransferPath.disk
    return TransferPath.stream


class DiskBudget:
    """Thread-safe accounting for the DISK path's staging directory."""

    def __init__(self, total_bytes: int) -> None:
        self._total = max(0, total_bytes)
        self._available = self._total
        self._cond = threading.Condition()

    @classmethod
    def from_settings(cls, transfer: TransferSettings, staging_dir: Path) -> DiskBudget:
        """Derive the budget from free space, the reserve, and the optional hard cap."""
        free = shutil.disk_usage(staging_dir).free - transfer.disk_reserve
        if transfer.max_disk_bytes is not None:
            free = min(free, transfer.max_disk_bytes)
        return cls(free)

    @property
    def total(self) -> int:
        return self._total

    @property
    def available(self) -> int:
        with self._cond:
            return self._available

    def can_reserve(self, n: int) -> bool:
        """True if `n` fits the budget at all, whether or not it is free right now."""
        return n <= self._total

    @contextmanager
    def reserve(self, n: int, *, timeout: float | None = None) -> Iterator[None]:
        """Block until `n` bytes are free, hold them, and release on exit — always.

        Raises:
            InsufficientDiskSpaceError: `n` exceeds the total budget, or `timeout` elapsed
                before enough space became free.
        """
        if not self.can_reserve(n):
            raise InsufficientDiskSpaceError(
                f"staging budget is {self._total} bytes, which cannot hold a "
                f"{n} byte file; raise transfer.max_disk_bytes or lower transfer.disk_reserve"
            )
        with self._cond:
            if not self._cond.wait_for(lambda: self._available >= n, timeout):
                raise InsufficientDiskSpaceError(
                    f"timed out after {timeout}s waiting for {n} bytes of staging space "
                    f"({self._available} of {self._total} available)"
                )
            self._available -= n
        try:
            yield
        finally:
            with self._cond:
                self._available += n
                self._cond.notify_all()


class StreamFailureTracker:
    """Thread-safe per-path counter driving the STREAM -> DISK downgrade."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def record(self, path: str) -> int:
        """Count one STREAM failure for `path` and return the new total."""
        with self._lock:
            count = self._counts.get(path, 0) + 1
            self._counts[path] = count
            return count

    def count(self, path: str) -> int:
        with self._lock:
            return self._counts.get(path, 0)
