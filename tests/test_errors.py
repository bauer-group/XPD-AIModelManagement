"""The exit-code table is the CLI's public contract; nothing else may define it.

Table-driven over every class in `errors`, plus a reflective sweep that fails when a
new exception is added without deciding its exit code.
"""

from __future__ import annotations

import pytest

from bg_ai_model_management import errors
from bg_ai_model_management.errors import AimmError

#: The normative table from the contract, section 2.1.
EXIT_CODES: list[tuple[type[AimmError], int]] = [
    (errors.AimmError, 1),
    (errors.ConfigError, 2),
    (errors.UnsafePathError, 2),
    (errors.AuthError, 3),
    (errors.SourceError, 4),
    (errors.RepoNotFoundError, 4),
    (errors.RepoGatedError, 4),
    (errors.RevisionNotFoundError, 4),
    (errors.FileNotInRepoError, 4),
    (errors.DestinationError, 5),
    (errors.BucketNotFoundError, 5),
    (errors.ObjectNotFoundError, 5),
    (errors.UploadFailedError, 5),
    (errors.IntegrityError, 6),
    (errors.ChecksumMismatchError, 6),
    (errors.SizeMismatchError, 6),
    (errors.ManifestError, 6),
    (errors.InsufficientDiskSpaceError, 7),
    (errors.TransferError, 8),
    (errors.ObjectTooLargeError, 8),
    (errors.RetentionRefusedError, 9),
    (errors.DriftDetectedError, 20),
]


def _all_subclasses(root: type[AimmError]) -> set[type[AimmError]]:
    found = {root}
    for child in root.__subclasses__():
        found |= _all_subclasses(child)
    return found


@pytest.mark.parametrize(("exc_type", "code"), EXIT_CODES, ids=lambda v: getattr(v, "__name__", v))
def test_exit_code_on_class(exc_type: type[AimmError], code: int) -> None:
    assert exc_type.exit_code == code


@pytest.mark.parametrize(("exc_type", "code"), EXIT_CODES, ids=lambda v: getattr(v, "__name__", v))
def test_exit_code_on_instance(exc_type: type[AimmError], code: int) -> None:
    """`main.py` reads `exc.exit_code` off the instance, so that is what must be right."""
    assert exc_type("boom").exit_code == code


def test_every_error_is_an_aimm_error() -> None:
    for exc_type, _ in EXIT_CODES:
        assert issubclass(exc_type, AimmError)
        assert issubclass(exc_type, Exception)


def test_table_covers_every_declared_exception() -> None:
    """A new exception without a row here would silently inherit an exit code."""
    declared = {
        obj for obj in vars(errors).values() if isinstance(obj, type) and issubclass(obj, AimmError)
    }
    assert declared == {exc_type for exc_type, _ in EXIT_CODES}


def test_no_stray_subclasses_outside_the_module() -> None:
    """Guards against a tool defining its own error outside the single table."""
    assert _all_subclasses(AimmError) == {exc_type for exc_type, _ in EXIT_CODES}


def test_specific_errors_inherit_their_family_code() -> None:
    """Subclasses must inherit, not redeclare: a second table would drift."""
    assert "exit_code" not in vars(errors.UnsafePathError)
    assert "exit_code" not in vars(errors.RepoGatedError)
    assert "exit_code" not in vars(errors.ChecksumMismatchError)
    assert "exit_code" not in vars(errors.ObjectTooLargeError)


def test_process_level_constants() -> None:
    assert errors.EXIT_OK == 0
    assert errors.EXIT_UNEXPECTED == 1
    assert errors.EXIT_INTERRUPTED == 130


def test_message_survives_round_trip() -> None:
    exc = errors.ChecksumMismatchError("digest mismatch for a/b.bin")
    assert str(exc) == "digest mismatch for a/b.bin"
