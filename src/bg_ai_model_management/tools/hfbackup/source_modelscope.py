"""Read-only access to a ModelScope (modelscope.cn) repository, pinned to a commit.

The second implementation of :class:`~bg_ai_model_management.tools.hfbackup.types.Source`.
Everything downstream — planner, engine, manifest, destination, verify, restore — is
unchanged: only where the bytes come from differs.

ModelScope is not Hugging Face wearing a different hostname, and three differences are
load-bearing. All three were verified against the live API.

**Refs come from git, not from the REST API.** ``/api/v1/models/<id>/revisions``
returns branch *names* only, and the ``Revision`` field on a file entry is the last
commit that touched *that file*, not the repository head. The only source of a branch
head is git's smart-HTTP endpoint, which serves real refs solely to a git-shaped user
agent — hence :data:`_GIT_USER_AGENT`.

**Failures arrive as HTTP 200.** A missing repository answers ``200`` with
``{"Success": false, "Code": 10010205001, …}``. Status alone is not a success signal;
the envelope has to be read.

**Every file carries a content sha256**, LFS or not, and no git blob id exists. That
inverts Hugging Face's situation, where a plain file has only a blob id. Since the
engine picks its integrity anchor from ``SourceFile.is_lfs`` — sha256 when set, git
blob id otherwise — every file here is reported with ``is_lfs=True``. That is not a
claim about git-lfs; it selects the digest ModelScope actually attests. Reporting
``is_lfs=False`` would send verification to a blob id that does not exist, and every
plain text file in every repository would fail its check.

Datasets are not served by this API shape (the models path answers HTTP 405 under
``/api/v1/datasets/…``), so a dataset repo is refused at ``pin`` rather than failing
mid-transfer.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from ... import errors
from ...net.retry import call_with_retry
from ...shutdown import raise_if_requested
from . import keys
from .types import PinnedRepo, RepoRef, RepoType, SourceFile, SourceKind

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance, types only
    from ...config.models import ModelScopeSettings

log = logging.getLogger(__name__)

#: ModelScope's git endpoint answers `{"message": "mirror self-forwarded loop detected"}`
#: to a generic agent and serves real refs only to a git-shaped one. Verified: curl with
#: this UA returns the same SHA as `git ls-remote`.
_GIT_USER_AGENT = "git/2.43.0"

#: Envelope code observed live for "repository or revision does not exist".
_CODE_RECORD_NOT_FOUND = 10010205001

#: HTTP statuses that mean "credentials missing or rejected", not "broken".
_AUTH_STATUS: frozenset[int] = frozenset({401, 403})

#: Per-file staging directory name length — 64 bits of SHA-1 over the repo path. Short
#: on purpose: Windows' 260-character path limit is a real constraint here.
_STAGE_DIR_CHARS = 16

_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")


def is_commit_sha(value: str) -> bool:
    """True when `value` is already a full 40-character lowercase commit SHA."""
    return _SHA_RE.match(value) is not None


@contextmanager
def _translated(
    context: str, *, missing: type[errors.AimmError] = errors.FileNotInRepoError
) -> Iterator[None]:
    """Translate ModelScope and httpx failures into aimm's typed errors.

    Clause order matters: `httpx.HTTPStatusError` derives from `httpx.HTTPError`, so the
    status-bearing clause must precede the bare transport clause, or every 404 would be
    reported as "could not reach".

    Args:
        context: Short description of the operation. Must never contain a token.
        missing: What a 404 means here. Resolving a ref or listing a tree hits the
            repository itself, so a 404 there is a missing REPOSITORY; during a
            transfer the repository is known to exist and a 404 is a missing file.
            Getting this wrong tells an operator a file vanished when in truth the
            whole model was deleted upstream.

    Raises:
        RepoNotFoundError, AuthError, FileNotInRepoError, SourceError: depending on the
            underlying failure.
    """
    try:
        yield
    except _EnvelopeError as exc:
        if exc.code == _CODE_RECORD_NOT_FOUND:
            raise errors.RepoNotFoundError(
                f"{context}: repository or revision not found on ModelScope."
            ) from exc
        raise errors.SourceError(f"{context}: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in _AUTH_STATUS:
            raise errors.AuthError(
                f"{context}: ModelScope rejected the credentials (HTTP {status})."
            ) from exc
        if status == 404:
            raise missing(f"{context}: not found (HTTP 404).") from exc
        raise errors.SourceError(f"{context}: ModelScope error (HTTP {status}).") from exc
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise errors.SourceError(
            f"{context}: could not reach ModelScope ({type(exc).__name__})."
        ) from exc


class _EnvelopeError(Exception):
    """ModelScope answered with a failure envelope. Never retryable by construction.

    Deliberately not an `AimmError`: it is raised *inside* the retried call so that
    `is_retryable` classifies it (as permanent) before `_translated` turns it into the
    typed error the rest of the codebase sees.
    """

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class ModelScopeSource:
    """Read-only access to a ModelScope repository, pinned to a commit."""

    kind = SourceKind.modelscope

    def __init__(
        self, settings: ModelScopeSettings, *, client: httpx.Client | None = None
    ) -> None:
        """Bind an HTTP client to the configured endpoint and credential.

        Args:
            settings: Endpoint, token and streaming/timeout tuning.
            client: Pre-built client, for tests.
        """
        self._settings = settings
        self._endpoint = settings.endpoint.rstrip("/")
        self._token = settings.token.get_secret_value() if settings.token is not None else None
        self._timeout = httpx.Timeout(
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.read_timeout,
            pool=settings.connect_timeout,
        )
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(follow_redirects=True)

    def close(self) -> None:
        """Release the connection pool, but only if this instance created it."""
        if self._owns_client:
            self._client.close()

    # ── the Source contract ──────────────────────────────────────────────────
    def pin(self, ref: RepoRef) -> PinnedRepo:
        """Resolve a ref to an immutable commit SHA.

        A revision that is already a 40-character SHA is trusted as-is; anything else is
        looked up among the remote's branches and tags. Note that ModelScope repositories
        conventionally use `master`, not `main`, as the default branch.

        Raises:
            ConfigError: the repository is a dataset, which this API does not serve.
            RepoNotFoundError, RevisionNotFoundError, AuthError, SourceError: on any
                ModelScope failure.
        """
        context = f"pin {ref.repo_id}@{ref.revision}"
        self._require_model(ref.repo_type, context)
        if is_commit_sha(ref.revision):
            return self._pinned(ref, ref.revision)

        with _translated(context, missing=errors.RepoNotFoundError):
            refs = call_with_retry(lambda: self._remote_refs(ref.repo_id))
        for candidate in (
            f"refs/heads/{ref.revision}",
            f"refs/tags/{ref.revision}",
            ref.revision,
        ):
            sha = refs.get(candidate)
            if sha is not None:
                return self._pinned(ref, sha)

        known = ", ".join(sorted(name for name in refs if name.startswith("refs/"))) or "none"
        raise errors.RevisionNotFoundError(
            f"{context}: no such branch or tag on ModelScope. Known refs: {known}"
        )

    def list_files(self, pinned: PinnedRepo) -> list[SourceFile]:
        """Enumerate every file at the pinned commit, sorted by path.

        Tree entries are dropped: ModelScope lists directories alongside files, with
        `Type: "tree"`, size 0 and an empty `Sha256`.

        Raises:
            UnsafePathError: a reported path could escape the key layout.
            RepoNotFoundError, AuthError, SourceError: on any ModelScope failure.
        """
        context = f"list {pinned.repo_id}@{pinned.commit_sha}"
        with _translated(context, missing=errors.RepoNotFoundError):
            entries = call_with_retry(
                lambda: self._get_json(
                    f"/api/v1/models/{pinned.repo_id}/repo/files",
                    params={"Revision": pinned.commit_sha, "Recursive": "True"},
                    context=context,
                )
            )
        raw = entries.get("Files")
        if not isinstance(raw, list):
            raise errors.SourceError(f"{context}: the response contained no file list.")

        files: list[SourceFile] = []
        for item in raw:
            if not isinstance(item, dict) or item.get("Type") != "blob":
                continue
            files.append(self._source_file(item, context))
        files.sort(key=lambda file: file.path)
        return files

    @contextmanager
    def open_stream(self, pinned: PinnedRepo, file: SourceFile) -> Iterator[Iterator[bytes]]:
        """Yield an iterator of byte chunks for one file, without touching disk.

        Opening the response is retried with backoff; the BODY is not, because a torn
        body cannot be rewound. The response is closed when the context exits, so the
        caller must consume the iterator inside the `with` block.

        Raises:
            FileNotInRepoError, AuthError, SourceError: on an HTTP error response, or
                once the transport has failed on every attempt.
        """
        url = self._resolve_url(pinned.repo_id, pinned.commit_sha, file.path)
        with _translated(f"stream {pinned.repo_id}:{file.path}"):
            response = call_with_retry(lambda: self._open_response(url))
            try:
                yield response.iter_bytes(self._settings.chunk_size)
            finally:
                response.close()

    def read_bytes(self, pinned: PinnedRepo, file: SourceFile) -> bytes:
        """Read a whole small file into memory.

        Only for files at or below `transfer.inline_max`; the planner already made that
        decision, so there is no size guard here.
        """
        with self.open_stream(pinned, file) as chunks:
            return b"".join(chunks)

    @contextmanager
    def staged(self, pinned: PinnedRepo, file: SourceFile, staging_dir: Path) -> Iterator[Path]:
        """Download one file into an isolated directory and yield the payload path.

        The per-file directory is removed on exit, ALWAYS, including on exception: across
        a large repository a leaked staging directory per file is an unbounded inode leak.
        """
        digest = hashlib.sha1(file.path.encode("utf-8"), usedforsecurity=False).hexdigest()
        local_dir = staging_dir / digest[:_STAGE_DIR_CHARS]
        local_dir.mkdir(parents=True, exist_ok=True)
        # The path passed `assert_safe_relpath` during listing, so its last segment is a
        # safe file name; the fallback only guards a pathological empty basename.
        payload = local_dir / (PurePosixPath(file.path).name or "payload")
        try:
            with self.open_stream(pinned, file) as chunks, payload.open("wb") as handle:
                for chunk in chunks:
                    # A multi-gigabyte download must not outlive a SIGTERM by minutes;
                    # the staging directory is removed by the `finally` either way.
                    raise_if_requested(f"download of {pinned.repo_id}:{file.path}")
                    handle.write(chunk)
            yield payload
        finally:
            shutil.rmtree(local_dir, ignore_errors=True)

    def whoami(self) -> str | None:
        """Always None: this tool never establishes a ModelScope identity.

        Mirroring public repositories needs no credential, and ModelScope's identity
        endpoint is not part of the verified surface this module is built on. `doctor`
        reports reachability and whether a token is configured via :meth:`ping` instead
        of claiming a user name it did not confirm.
        """
        return None

    def ping(self) -> str:
        """Confirm the endpoint answers; returns a human-readable detail line.

        Raises:
            SourceError: ModelScope is unreachable.
        """
        with _translated("ping ModelScope"):
            call_with_retry(lambda: self._request("GET", self._endpoint).raise_for_status())
        credential = "token set" if self._token else "anonymous — fine for public repos"
        return f"{self._endpoint} reachable; {credential}"

    # ── internals ────────────────────────────────────────────────────────────
    def _source_file(self, item: Mapping[str, Any], context: str) -> SourceFile:
        path = item.get("Path")
        if not isinstance(path, str) or not path:
            raise errors.SourceError(f"{context}: a file entry carried no usable path.")
        size = item.get("Size")
        if not isinstance(size, int):
            raise errors.SourceError(f"{context}: {path} carried no integer size.")
        keys.assert_safe_relpath(path)
        sha256 = item.get("Sha256")
        return SourceFile(
            path=path,
            size=size,
            # No git blob id exists upstream; never read, because is_lfs is True below.
            blob_id="",
            sha256=sha256 if isinstance(sha256, str) and sha256 else None,
            xet_hash=None,
            is_lfs=True,
        )

    def _remote_refs(self, repo_id: str) -> dict[str, str]:
        """Map every remote ref (`refs/heads/…`, `refs/tags/…`, `HEAD`) to its SHA."""
        response = self._request(
            "GET",
            f"{self._endpoint}/{repo_id}.git/info/refs",
            params={"service": "git-upload-pack"},
            git=True,
        )
        response.raise_for_status()
        refs = _parse_pkt_refs(response.content)
        if not refs:
            raise _EnvelopeError(
                f"{repo_id}: the git endpoint returned no refs; the repository may not exist"
            )
        return refs

    def _get_json(
        self, path: str, *, params: Mapping[str, str], context: str
    ) -> dict[str, Any]:
        """GET a REST endpoint and unwrap its `Data` envelope."""
        response = self._request("GET", f"{self._endpoint}{path}", params=params)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise _EnvelopeError(f"{context}: the response was not JSON") from exc
        if not isinstance(payload, dict):
            raise _EnvelopeError(f"{context}: the response was not a JSON object")
        # A missing repo answers 200 + Success=false, so this is the real error path;
        # raise_for_status above almost never fires for it.
        if payload.get("Success") is False:
            code = payload.get("Code")
            raise _EnvelopeError(
                f"{context}: {payload.get('Message') or 'unknown error'}",
                code=code if isinstance(code, int) else None,
            )
        data = payload.get("Data")
        if not isinstance(data, dict):
            raise _EnvelopeError(f"{context}: the response carried no Data object")
        return data

    def _resolve_url(self, repo_id: str, revision: str, path: str) -> str:
        """The direct download URL for one file at one revision.

        Mirrors Hugging Face's `/resolve/` layout. Each path segment is quoted
        separately so `/` keeps its meaning while spaces and `#` do not.
        """
        quoted = "/".join(quote(segment, safe="") for segment in path.split("/"))
        return f"{self._endpoint}/models/{repo_id}/resolve/{revision}/{quoted}"

    def _open_response(self, url: str) -> httpx.Response:
        """Open one streaming GET and validate its status. Retried as a unit."""
        request = self._client.build_request(
            "GET", url, headers=self._headers(), timeout=self._timeout
        )
        response = self._client.send(request, stream=True)
        try:
            response.raise_for_status()
        except BaseException:
            response.close()
            raise
        return response

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        git: bool = False,
    ) -> httpx.Response:
        return self._client.request(
            method,
            url,
            params=dict(params) if params else None,
            headers=self._headers(git=git),
            timeout=self._timeout,
        )

    def _headers(self, *, git: bool = False) -> dict[str, str]:
        headers: dict[str, str] = {}
        if git:
            headers["User-Agent"] = _GIT_USER_AGENT
        if self._token:
            # Public repositories need no credential; this is for private ones and is
            # never required by the mirroring path.
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    @staticmethod
    def _require_model(repo_type: RepoType, context: str) -> None:
        """Reject dataset entries: ModelScope serves datasets from a different API.

        Verified: the models file-listing path answers HTTP 405 under
        `/api/v1/datasets/…`, so reusing it would fail mid-run rather than up front.
        """
        if repo_type is not RepoType.models:
            raise errors.ConfigError(
                f"{context}: the ModelScope source supports repo type 'models' only "
                f"(got {repo_type.value})."
            )

    @staticmethod
    def _pinned(ref: RepoRef, commit_sha: str) -> PinnedRepo:
        return PinnedRepo(
            repo_id=ref.repo_id,
            repo_type=ref.repo_type,
            revision_requested=ref.revision,
            commit_sha=commit_sha,
        )


def _parse_pkt_refs(payload: bytes) -> dict[str, str]:
    """Parse a git smart-HTTP `info/refs` body into `ref name -> commit SHA`.

    The body is a sequence of pkt-lines: four hex digits giving the total line length
    (`0000` being a flush packet) followed by the payload. The first ref line carries the
    capability list after a NUL byte, which is stripped here.
    """
    refs: dict[str, str] = {}
    for line in _pkt_lines(payload):
        text = line.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()
        if not text or text.startswith("#"):
            continue
        sha, _, name = text.partition(" ")
        if name and is_commit_sha(sha):
            refs[name] = sha
    return refs


def _pkt_lines(payload: bytes) -> Iterator[bytes]:
    """Yield each non-flush pkt-line payload, stopping at the first malformed header."""
    position = 0
    while position + 4 <= len(payload):
        try:
            length = int(payload[position : position + 4], 16)
        except ValueError:
            return
        if length == 0:  # flush packet
            position += 4
            continue
        if length < 4 or position + length > len(payload):
            return
        yield payload[position + 4 : position + length]
        position += length
