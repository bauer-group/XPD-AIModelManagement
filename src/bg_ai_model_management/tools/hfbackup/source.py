"""Read-only access to a Hugging Face repository, pinned to an immutable commit.

Two invariants carry this module.

**Pin before you enumerate.** ``pin()`` resolves a moving ref such as ``main`` to a
40-character commit SHA once, and every later call is made against that SHA. Listing
against ``main`` and transferring against ``main`` in two separate calls lets a push
between them produce a torn snapshot: file list from state A, bytes from state B,
silently and with no error anywhere.

**The integrity anchor differs per file.** For a non-LFS file Hugging Face's
``RepoFile.blob_id`` is the git SHA-1 of the content and is a real upstream anchor.
For an LFS file ``blob_id`` is the SHA-1 of the *pointer file* and never matches the
payload; the anchor there is ``lfs.sha256``. Callers must branch on ``is_lfs``, so
``list_files`` records both plus the flag rather than guessing on their behalf.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from huggingface_hub import HfApi, RepoFile, hf_hub_download, hf_hub_url
from huggingface_hub.errors import (
    GatedRepoError,
    HfHubHTTPError,
    HFValidationError,
    LocalEntryNotFoundError,
    RemoteEntryNotFoundError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
)

# `huggingface_hub.utils` is the documented public path for all five of these (verified
# present on hub 1.24.0), but its __init__ declares no __all__ and re-exports them with a
# plain import. mypy --strict implies --no-implicit-reexport and therefore rejects them.
# The alternative is importing from huggingface_hub.utils._http / ._auth / ._headers /
# ._validators, i.e. binding this tool to private module paths that upstream is free to
# move. The narrow ignore is the safer of the two.
from huggingface_hub.utils import (  # type: ignore[attr-defined]
    build_hf_headers,
    get_session,
    get_token,
    hf_raise_for_status,
    validate_repo_id,
)

from bg_ai_model_management import errors
from bg_ai_model_management.net.retry import call_with_retry

from . import keys
from .types import HF_REPO_TYPE, PinnedRepo, RepoRef, SourceFile

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance, types only
    from bg_ai_model_management.config.models import HubSettings

#: HTTP statuses that mean "your credentials are missing or rejected", not "broken".
_AUTH_STATUS: frozenset[int] = frozenset({401, 403})

#: Length of the per-file staging directory name. 64 bits of SHA-1 over the repo path
#: is collision-free enough for a scratch directory and keeps the path short on
#: Windows, where the 260-character limit is still a real constraint.
_STAGE_DIR_CHARS: int = 16


@contextmanager
def _translated(context: str) -> Iterator[None]:
    """Translate huggingface_hub exceptions into aimm's typed errors.

    The clause order is load-bearing. ``GatedRepoError`` is a SUBCLASS of
    ``RepositoryNotFoundError``, so catching the parent first would report "repository
    does not exist" for a repository that merely needs its licence accepted — an
    actively misleading diagnostic for a backup tool. Likewise
    ``RemoteEntryNotFoundError`` derives from ``HfHubHTTPError`` and must precede it,
    and ``HfHubHTTPError`` itself derives from ``httpx.HTTPError`` so the bare transport
    clause must come LAST or it would swallow every Hub status error.

    ``LocalEntryNotFoundError`` (raised by ``hf_hub_download`` when a connection blip
    leaves nothing in the cache) subclasses ``FileNotFoundError``/``EntryNotFoundError``
    and none of the HTTP types, so it needs a clause of its own; without it the module's
    "library code raises typed errors" contract is violated and ``doctor`` dies with an
    untyped traceback in the exact situation it exists to diagnose.

    Args:
        context: Short human-readable description of the operation, for the message.
            Must never contain a token.

    Raises:
        RepoGatedError, RepoNotFoundError, RevisionNotFoundError, FileNotInRepoError,
        AuthError, SourceError, ConfigError: Depending on the underlying failure.
    """
    try:
        yield
    except GatedRepoError as exc:
        raise errors.RepoGatedError(
            f"{context}: the repository is gated. Accept its licence on the Hub first."
        ) from exc
    except RevisionNotFoundError as exc:
        raise errors.RevisionNotFoundError(f"{context}: revision not found.") from exc
    except RepositoryNotFoundError as exc:
        raise errors.RepoNotFoundError(
            f"{context}: repository not found, or not visible to this token."
        ) from exc
    except RemoteEntryNotFoundError as exc:
        raise errors.FileNotInRepoError(f"{context}: file not found in the repository.") from exc
    except LocalEntryNotFoundError as exc:
        raise errors.SourceError(
            f"{context}: the download produced no local file; the Hub was unreachable "
            "or the connection was interrupted."
        ) from exc
    except HfHubHTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in _AUTH_STATUS:
            raise errors.AuthError(
                f"{context}: the Hugging Face Hub rejected the credentials (HTTP {status})."
            ) from exc
        raise errors.SourceError(f"{context}: Hugging Face Hub error (HTTP {status}).") from exc
    except HFValidationError as exc:
        raise errors.ConfigError(f"{context}: {exc}") from exc
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise errors.SourceError(
            f"{context}: could not reach the Hugging Face Hub ({type(exc).__name__})."
        ) from exc


class HubSource:
    """Read-only access to a Hugging Face repository, pinned to a commit."""

    def __init__(self, settings: HubSettings, *, api: HfApi | None = None) -> None:
        """Build an ``HfApi`` bound to the configured endpoint and token.

        A ``None`` token falls back to ``get_token()``, which resolves the ambient
        credential (``HF_TOKEN`` or the stored token file). The resolved value is kept
        so the raw streaming path can authenticate too — Hugging Face names "not
        passing a token" as the number-one cause of throttling.

        Args:
            settings: Hub endpoint, token and streaming chunk size.
            api: Pre-built client, for tests.
        """
        self._settings = settings
        token = settings.token.get_secret_value() if settings.token is not None else get_token()
        self._token: str | None = token
        self._api = api if api is not None else HfApi(endpoint=settings.endpoint, token=token)

    def pin(self, ref: RepoRef) -> PinnedRepo:
        """Resolve a ref to an immutable commit SHA.

        MUST be called before ``list_files``, ``open_stream``, ``read_bytes`` or
        ``staged``.

        Args:
            ref: Repository id, type and the requested revision.

        Returns:
            The same repository pinned to a full commit SHA.

        Raises:
            RepoGatedError, RepoNotFoundError, RevisionNotFoundError, AuthError,
            SourceError, ConfigError: On any Hub failure or an invalid repository id.
        """
        with _translated(f"pin {ref.repo_id}@{ref.revision}"):
            validate_repo_id(ref.repo_id)
            # Retried INSIDE the translation, where the exception is still a raw
            # HfHubHTTPError that `is_retryable` can classify. Outside it every failure
            # is already an AimmError, which is never retryable by design — so a single
            # transient 503 would abort a whole multi-repository sync.
            info = call_with_retry(
                lambda: self._api.repo_info(
                    ref.repo_id,
                    repo_type=HF_REPO_TYPE[ref.repo_type],
                    revision=ref.revision,
                )
            )
        commit_sha = getattr(info, "sha", None)
        if not isinstance(commit_sha, str) or not commit_sha:
            raise errors.SourceError(
                f"pin {ref.repo_id}@{ref.revision}: the Hub returned no commit SHA "
                f"(got {type(info).__name__})."
            )
        return PinnedRepo(
            repo_id=ref.repo_id,
            repo_type=ref.repo_type,
            revision_requested=ref.revision,
            commit_sha=commit_sha,
        )

    def list_files(self, pinned: PinnedRepo) -> list[SourceFile]:
        """Enumerate every file at the pinned commit, sorted by path.

        Folders are skipped. Every path is validated before it can become an S3 key.

        Args:
            pinned: The repository pinned by ``pin()``.

        Returns:
            One ``SourceFile`` per file, ordered by path so that plans and manifests
            are byte-stable across runs.

        Raises:
            UnsafePathError: If the Hub reports a path that could escape the layout.
            RepoGatedError, RepoNotFoundError, RevisionNotFoundError, AuthError,
            SourceError: On any Hub failure.
        """
        context = f"list {pinned.repo_id}@{pinned.commit_sha}"
        files: list[SourceFile] = []
        with _translated(context):
            # `list_repo_tree` is a generator, so the HTTP call happens during
            # iteration: the retried unit has to be the materialised listing, not the
            # generator object. The Hub retries pages 2..n itself; page 1 is only
            # covered here.
            entries = call_with_retry(
                lambda: list(
                    self._api.list_repo_tree(
                        pinned.repo_id,
                        recursive=True,
                        expand=False,
                        revision=pinned.commit_sha,
                        repo_type=HF_REPO_TYPE[pinned.repo_type],
                    )
                )
            )
            for entry in entries:
                if not isinstance(entry, RepoFile):
                    continue
                keys.assert_safe_relpath(entry.path)
                files.append(
                    SourceFile(
                        path=entry.path,
                        size=entry.size,
                        blob_id=entry.blob_id,
                        sha256=entry.lfs.sha256 if entry.lfs is not None else None,
                        xet_hash=entry.xet_hash,
                        is_lfs=entry.lfs is not None,
                    )
                )
        files.sort(key=lambda f: f.path)
        return files

    @contextmanager
    def open_stream(self, pinned: PinnedRepo, file: SourceFile) -> Iterator[Iterator[bytes]]:
        """Yield an iterator of byte chunks for one file, without touching disk.

        This is a plain HTTP GET against the resolver/CDN, so it does NOT go through
        hf-xet and ``HF_XET_HIGH_PERFORMANCE`` has no effect on it.

        The response is closed when the context exits, so the caller must consume the
        iterator inside the ``with`` block.

        Opening the response is retried with backoff; the BODY is not, because a torn
        body cannot be rewound. That split matters: this call bypasses the Hub's own
        ``http_stream_backoff``, so without it a 429 on the resolver would fail every
        small file of a repository outright.

        Args:
            pinned: The repository pinned by ``pin()``.
            file: The file to read.

        Yields:
            An iterator of byte chunks of ``settings.chunk_size``.

        Raises:
            FileNotInRepoError, AuthError, SourceError: On an HTTP error response, or
                once the transport has failed on every attempt.
        """
        url = hf_hub_url(
            pinned.repo_id,
            file.path,
            repo_type=HF_REPO_TYPE[pinned.repo_type],
            revision=pinned.commit_sha,
            endpoint=self._settings.endpoint,
        )
        headers = build_hf_headers(token=self._token)
        with ExitStack() as stack, _translated(f"stream {pinned.repo_id}:{file.path}"):
            response = call_with_retry(lambda: self._open_response(stack, url, headers))
            yield response.iter_bytes(self._settings.chunk_size)

    def _open_response(self, stack: ExitStack, url: str, headers: dict[str, str]) -> httpx.Response:
        """Open one streaming GET and validate its status. Retried as a unit.

        On success the response's close callback is handed to ``stack``, which the
        caller's ``with`` block owns; on failure ``attempt`` unwinds and closes the
        response here, so a retry never leaks a half-open connection.

        An explicit timeout is mandatory: ``huggingface_hub``'s shared client is built
        with ``timeout=None``, so a stalled CDN connection would block a worker thread
        forever — no exception, no progress, nothing for the retry layer to catch.
        """
        timeout = httpx.Timeout(
            connect=self._settings.connect_timeout,
            read=self._settings.read_timeout,
            write=self._settings.read_timeout,
            pool=self._settings.connect_timeout,
        )
        with ExitStack() as attempt:
            response = attempt.enter_context(
                get_session().stream(
                    "GET", url, follow_redirects=True, headers=headers, timeout=timeout
                )
            )
            hf_raise_for_status(response)
            stack.enter_context(attempt.pop_all())
        return response

    def read_bytes(self, pinned: PinnedRepo, file: SourceFile) -> bytes:
        """Read a whole small file into memory.

        Only call this for files at or below ``transfer.inline_max``; there is no size
        guard here because the planner already made that decision.

        Raises:
            FileNotInRepoError, AuthError, SourceError: As ``open_stream``.
        """
        with self.open_stream(pinned, file) as chunks:
            return b"".join(chunks)

    @contextmanager
    def staged(self, pinned: PinnedRepo, file: SourceFile, staging_dir: Path) -> Iterator[Path]:
        """Download one file into an isolated directory and yield the payload path.

        The per-file directory is removed on exit, ALWAYS, including on exception.
        Removing only the payload would leak ``.cache/huggingface/download/<f>.metadata``,
        possibly a ``.lock``, plus ``.gitignore`` and ``CACHEDIR.TAG``; across millions
        of files that is an unbounded inode leak. Hugging Face documents
        ``.cache/huggingface/`` as safe to delete, so ``rmtree`` of the per-file root
        removes everything and needs no private ``_local_folder`` API.

        With ``local_dir`` set the yielded path is a real file, not a symlink.

        Args:
            pinned: The repository pinned by ``pin()``.
            file: The file to download.
            staging_dir: Root under which the isolated directory is created.

        Yields:
            The path of the downloaded payload.

        Raises:
            FileNotInRepoError, AuthError, SourceError: On any Hub failure.
        """
        digest = hashlib.sha1(file.path.encode("utf-8"), usedforsecurity=False).hexdigest()
        local_dir = staging_dir / digest[:_STAGE_DIR_CHARS]
        local_dir.mkdir(parents=True, exist_ok=True)
        try:
            with _translated(f"download {pinned.repo_id}:{file.path}"):
                downloaded = hf_hub_download(
                    pinned.repo_id,
                    file.path,
                    repo_type=HF_REPO_TYPE[pinned.repo_type],
                    revision=pinned.commit_sha,
                    local_dir=local_dir,
                    token=self._token,
                    endpoint=self._settings.endpoint,
                )
            if not isinstance(downloaded, str):
                raise errors.SourceError(
                    f"download {pinned.repo_id}:{file.path}: the Hub returned "
                    f"{type(downloaded).__name__} instead of a path."
                )
            yield Path(downloaded)
        finally:
            shutil.rmtree(local_dir, ignore_errors=True)

    def whoami(self) -> str | None:
        """Return the authenticated user name, or ``None`` when unauthenticated.

        Used by ``doctor``. A missing token is reported as ``None``; a token the Hub
        rejects raises, because silently reporting "unauthenticated" would hide a
        misconfigured credential.

        Raises:
            AuthError, SourceError: If a token is present but not accepted.
        """
        if self._token is None:
            return None
        with _translated("whoami"):
            info = self._api.whoami()
        name = info.get("name")
        return name if isinstance(name, str) else None
