"""Typed exceptions and the single authoritative exit-code table."""

from __future__ import annotations


class AimmError(Exception):
    """Base class for every error raised by aimm library code."""

    exit_code: int = 1


class ConfigError(AimmError):
    """Invalid, missing or contradictory configuration."""

    exit_code = 2


class UnsafePathError(ConfigError):
    """A repository-supplied path failed the traversal safety check."""


class AuthError(AimmError):
    """Missing or rejected credentials for Hugging Face or the object store."""

    exit_code = 3


class SourceError(AimmError):
    """Generic failure talking to the Hugging Face Hub."""

    exit_code = 4


class RepoNotFoundError(SourceError):
    """The repository does not exist or is not visible to this token."""


class RepoGatedError(SourceError):
    """The repository exists but requires accepting its licence first."""


class RevisionNotFoundError(SourceError):
    """The requested revision or ref does not exist in the repository."""


class FileNotInRepoError(SourceError):
    """A file listed in the tree could not be resolved at transfer time."""


class DestinationError(AimmError):
    """Generic failure talking to the S3-compatible object store."""

    exit_code = 5


class BucketNotFoundError(DestinationError):
    """The configured bucket does not exist or is not accessible."""


class ObjectNotFoundError(DestinationError):
    """A key expected to exist was absent."""


class UploadFailedError(DestinationError):
    """An upload could not be completed; any multipart upload was aborted."""


class IntegrityError(AimmError):
    """Stored bytes do not match what they are supposed to be."""

    exit_code = 6


class ChecksumMismatchError(IntegrityError):
    """A computed digest did not match the expected digest."""


class SizeMismatchError(IntegrityError):
    """An observed byte count did not match the expected size."""


class ManifestError(IntegrityError):
    """A manifest is missing, malformed, or fails its own digest check."""


class InsufficientDiskSpaceError(AimmError):
    """The staging directory cannot hold the requested file."""

    exit_code = 7


class TransferError(AimmError):
    """A transfer failed after exhausting retries."""

    exit_code = 8


class ObjectTooLargeError(TransferError):
    """No admissible part size exists for this object under current limits."""


class RetentionRefusedError(AimmError):
    """A prune plan tripped a safety guard and was refused."""

    exit_code = 9


class DriftDetectedError(AimmError):
    """verify found differences. Not a crash: a finding."""

    exit_code = 20


EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_INTERRUPTED = 130
