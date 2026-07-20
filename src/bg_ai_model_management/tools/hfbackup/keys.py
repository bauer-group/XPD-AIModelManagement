"""Deterministic S3 key layout for hf-backup, plus the path-safety guards.

Every repository-supplied name is hostile input. The Hugging Face Hub is a public,
user-writable namespace, so a file called ``../../etc/cron.d/x`` must never become an
S3 key outside our prefix, and must never land outside ``--dest`` during a restore.
``assert_safe_relpath`` and ``safe_local_path`` are the only two places that decide
this; every key builder funnels through them.

Layout (``LAYOUT_VERSION`` = ``v1``)::

    <prefix>/v1/<repo_type>/<owner>/<name>/refs/<ref>.json
    <prefix>/v1/<repo_type>/<owner>/<name>/revisions/<sha>/manifest.json
    <prefix>/v1/<repo_type>/<owner>/<name>/revisions/<sha>/manifest.json.sha256
    <prefix>/v1/<repo_type>/<owner>/<name>/revisions/<sha>/files/<path>
    <prefix>/v1/_probe/<uuid4hex>

The revision segment is always a commit SHA, never a moving ref, so a key is immutable
once written and a restore three years from now resolves to the same bytes.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from bg_ai_model_management.errors import UnsafePathError

from .types import RepoType

LAYOUT_VERSION: str = "v1"
MAX_PATH_LENGTH: int = 1024

#: Segments that would re-root or self-reference a path.
_FORBIDDEN_SEGMENTS: frozenset[str] = frozenset({"", ".", ".."})

#: C0 control characters, NUL included. Illegal in both S3 keys and local filenames.
_CONTROL_CHARS: str = "".join(chr(c) for c in range(0x20))

#: A colon is rejected ANYWHERE in a segment, not only as a leading drive designator.
#: ``a/C:/b`` re-roots on Windows, and ``config.json:x`` is an NTFS alternate-data-stream
#: reference: opening it creates a real, empty ``config.json`` in ``--dest`` plus a hidden
#: stream, then the rename fails with WinError 87 — a file the repository never contained,
#: and a legitimate ``config.json`` that can no longer be restored.
_COLON: str = ":"

#: Characters git permits in a ref name that are also safe in an S3 key.
_REF_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9._/-]+$")


def assert_safe_relpath(path: str) -> str:
    """Validate a repository-relative path and return it unchanged.

    Rejects the empty string, a leading ``/``, any backslash, a colon anywhere (drive
    designators and NTFS alternate data streams alike), any segment equal to ``''``,
    ``'.'`` or ``'..'``, any C0 control character, and anything longer than
    ``MAX_PATH_LENGTH``.

    The path is returned UNCHANGED — never normalised. Rewriting an upstream path
    would silently change what we claim to have backed up.

    Args:
        path: A POSIX, repository-relative path as reported by the Hub.

    Returns:
        The path exactly as given.

    Raises:
        UnsafePathError: On any violation.
    """
    if not path:
        raise UnsafePathError("Path is empty.")
    if len(path) > MAX_PATH_LENGTH:
        raise UnsafePathError(f"Path exceeds {MAX_PATH_LENGTH} characters: {len(path)} given.")
    if path.startswith("/"):
        raise UnsafePathError(f"Path is absolute: {path!r}")
    if "\\" in path:
        raise UnsafePathError(f"Path contains a backslash: {path!r}")
    for char in path:
        if char in _CONTROL_CHARS:
            raise UnsafePathError(f"Path contains control character 0x{ord(char):02x}: {path!r}")
    if _COLON in path:
        raise UnsafePathError(f"Path contains a colon: {path!r}")
    for segment in path.split("/"):
        if segment in _FORBIDDEN_SEGMENTS:
            raise UnsafePathError(f"Path contains an unsafe segment {segment!r}: {path!r}")
    return path


def assert_safe_ref(ref: str) -> str:
    """Validate a git ref name for use in a key and return it unchanged.

    Allows ``[A-Za-z0-9._-]`` and ``/``, and applies every rule of
    ``assert_safe_relpath`` on top, so ``..`` cannot slip through as a ref either.

    Args:
        ref: A branch, tag or revision name.

    Returns:
        The ref exactly as given.

    Raises:
        UnsafePathError: On any violation.
    """
    assert_safe_relpath(ref)
    if not _REF_RE.match(ref):
        raise UnsafePathError(f"Ref contains characters that are illegal in a key: {ref!r}")
    return ref


def safe_local_path(dest: Path, relpath: str) -> Path:
    """Join ``dest`` and a repository-relative path, guaranteeing containment.

    This is the Zip-Slip guard for ``restore``. Without it a crafted repository can
    write outside ``--dest``. The syntactic check runs first, then both sides are
    resolved and containment is re-checked, which also defeats a symlinked parent.

    Args:
        dest: The restore root.
        relpath: A repository-relative path.

    Returns:
        The absolute path to write to, guaranteed to be inside ``dest``.

    Raises:
        UnsafePathError: If the path is unsafe or escapes ``dest``.
    """
    assert_safe_relpath(relpath)
    root = dest.resolve()
    candidate = (root / relpath).resolve()
    if candidate != root and not candidate.is_relative_to(root):
        raise UnsafePathError(f"Path escapes the destination directory: {relpath!r}")
    return candidate


def _normalise_prefix(prefix: str) -> str:
    """Strip surrounding slashes from a configured prefix and validate what remains."""
    trimmed = prefix.strip("/")
    if trimmed:
        assert_safe_relpath(trimmed)
    return trimmed


def _layout_root(prefix: str) -> str:
    """Return ``'<prefix>/v1'``, or just ``'v1'`` when the prefix is empty."""
    trimmed = _normalise_prefix(prefix)
    return f"{trimmed}/{LAYOUT_VERSION}" if trimmed else LAYOUT_VERSION


def repo_root(prefix: str, repo_type: RepoType, repo_id: str) -> str:
    """Return ``'<prefix>/v1/<repo_type>/<owner>/<name>'`` (no trailing slash).

    ``repo_id`` is not escaped: the Hub restricts it to ``[A-Za-z0-9._-]`` per segment
    and ``/`` is a legal S3 separator, so ``owner/name`` maps to a natural hierarchy.
    It is still validated here, independently of any file path, so that a forged id
    cannot reach outside the prefix.

    Raises:
        UnsafePathError: If ``prefix`` or ``repo_id`` is unsafe.
    """
    assert_safe_relpath(repo_id)
    return f"{_layout_root(prefix)}/{repo_type.value}/{repo_id}"


def revision_root(prefix: str, repo_type: RepoType, repo_id: str, commit_sha: str) -> str:
    """Return ``'<repo_root>/revisions/<commit_sha>'``.

    Raises:
        UnsafePathError: If any component is unsafe.
    """
    assert_safe_relpath(commit_sha)
    return f"{repo_root(prefix, repo_type, repo_id)}/revisions/{commit_sha}"


def file_key(prefix: str, repo_type: RepoType, repo_id: str, commit_sha: str, path: str) -> str:
    """Return ``'<revision_root>/files/<path>'``.

    Raises:
        UnsafePathError: If ``path`` or any other component is unsafe.
    """
    assert_safe_relpath(path)
    return f"{revision_root(prefix, repo_type, repo_id, commit_sha)}/files/{path}"


def manifest_key(prefix: str, repo_type: RepoType, repo_id: str, commit_sha: str) -> str:
    """Return ``'<revision_root>/manifest.json'``."""
    return f"{revision_root(prefix, repo_type, repo_id, commit_sha)}/manifest.json"


def manifest_digest_key(prefix: str, repo_type: RepoType, repo_id: str, commit_sha: str) -> str:
    """Return ``'<revision_root>/manifest.json.sha256'``."""
    return f"{revision_root(prefix, repo_type, repo_id, commit_sha)}/manifest.json.sha256"


def ref_key(prefix: str, repo_type: RepoType, repo_id: str, ref: str) -> str:
    """Return ``'<repo_root>/refs/<ref>.json'``.

    Raises:
        UnsafePathError: If ``ref`` or any other component is unsafe.
    """
    assert_safe_ref(ref)
    return f"{repo_root(prefix, repo_type, repo_id)}/refs/{ref}.json"


def refs_prefix(prefix: str, repo_type: RepoType, repo_id: str) -> str:
    """Return ``'<repo_root>/refs/'`` WITH a trailing slash, for ListObjectsV2."""
    return f"{repo_root(prefix, repo_type, repo_id)}/refs/"


def revisions_prefix(prefix: str, repo_type: RepoType, repo_id: str) -> str:
    """Return ``'<repo_root>/revisions/'`` WITH a trailing slash.

    The trailing slash is required for ``ListObjectsV2`` with ``Delimiter='/'`` to roll
    each revision up into exactly one ``CommonPrefixes`` entry.
    """
    return f"{repo_root(prefix, repo_type, repo_id)}/revisions/"


def probe_key(prefix: str) -> str:
    """Return ``'<prefix>/v1/_probe/<uuid4hex>'``.

    A fresh random key per call, so a capability probe never collides with a concurrent
    run and never overwrites real data.
    """
    return f"{_layout_root(prefix)}/_probe/{uuid.uuid4().hex}"


def parse_repo_prefix(prefix: str, key: str) -> tuple[RepoType, str] | None:
    """Invert ``repo_root`` for catalog listing.

    Args:
        prefix: The configured prefix the key was built with.
        key: An S3 key or common prefix under that prefix.

    Returns:
        ``(repo_type, repo_id)``, or ``None`` when the key does not belong to this
        layout — an unrecognised key is a normal occurrence in a shared bucket and
        must not raise.
    """
    root = f"{_layout_root(prefix)}/"
    if not key.startswith(root):
        return None
    segments = key[len(root) :].split("/")
    if len(segments) < 3:
        return None
    try:
        repo_type = RepoType(segments[0])
    except ValueError:
        return None
    if not segments[1] or not segments[2]:
        return None
    return repo_type, f"{segments[1]}/{segments[2]}"
