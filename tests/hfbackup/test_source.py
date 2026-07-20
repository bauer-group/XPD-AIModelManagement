"""The Hugging Face source layer, with the Hub entirely mocked. No network.

Three behaviours here are the reason the module exists, and each has a failure mode
that is silent rather than loud:

* **`pin()` must yield an immutable commit SHA.** Listing against `main` and then
  transferring against `main` lets a push between the two calls produce a torn
  snapshot — file list from state A, bytes from state B, no error anywhere.
* **The integrity anchor differs per file.** `blob_id` for an LFS file is the SHA-1 of
  the *pointer*, not the payload, and will never match the content. `lfs.sha256` is the
  anchor there. Getting this backwards fails on every large file, i.e. every file that
  matters.
* **`GatedRepoError` is a SUBCLASS of `RepositoryNotFoundError`.** Catching the parent
  first reports "repository does not exist" for a repository that merely needs its
  licence accepted, sending the operator to look for a typo that is not there.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from huggingface_hub import RepoFile, RepoFolder
from huggingface_hub.errors import (
    GatedRepoError,
    HfHubHTTPError,
    HFValidationError,
    LocalEntryNotFoundError,
    RemoteEntryNotFoundError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
)
from pydantic import SecretStr

from bg_ai_model_management import errors
from bg_ai_model_management.config.models import HubSettings
from bg_ai_model_management.tools.hfbackup import source as source_module
from bg_ai_model_management.tools.hfbackup.source import HubSource
from bg_ai_model_management.tools.hfbackup.types import PinnedRepo, RepoRef, RepoType

COMMIT = "3f8a1c0d9e2b4a6c8d0f1e3a5b7c9d1e2f4a6b8c"
REQUEST = httpx.Request("GET", "https://huggingface.co/api/models/org/name")


def hub_http_error(exc_type: Any, status: int) -> Exception:
    """Build a huggingface_hub HTTP error the way the library itself does."""
    error: Exception = exc_type("hub said no", response=httpx.Response(status, request=REQUEST))
    return error


def repo_file(path: str, *, size: int = 10, oid: str = "b" * 40, sha256: str | None = None) -> Any:
    """A `RepoFile` as `list_repo_tree` yields it. LFS files carry an `lfs` block."""
    lfs = {"size": size, "oid": sha256, "pointerSize": 134} if sha256 else None
    return RepoFile(path=path, size=size, oid=oid, lfs=lfs)  # type: ignore[no-untyped-call]


def repo_folder(path: str, *, oid: str = "f" * 40) -> Any:
    """A `RepoFolder` as `list_repo_tree` interleaves them with files."""
    return RepoFolder(path=path, oid=oid)  # type: ignore[no-untyped-call]


class FakeInfo:
    def __init__(self, sha: str | None) -> None:
        self.sha = sha


class FakeApi:
    """Records calls and replays canned answers. Never touches the network."""

    def __init__(
        self,
        *,
        sha: str | None = COMMIT,
        tree: list[Any] | None = None,
        raises: Exception | None = None,
        whoami_result: dict[str, Any] | None = None,
    ) -> None:
        self._sha = sha
        self._tree = tree or []
        self._raises = raises
        self._whoami = {"name": "bauer-group"} if whoami_result is None else whoami_result
        self.repo_info_calls: list[dict[str, Any]] = []
        self.tree_calls: list[dict[str, Any]] = []

    def repo_info(self, repo_id: str, **kwargs: Any) -> FakeInfo:
        self.repo_info_calls.append({"repo_id": repo_id, **kwargs})
        if self._raises is not None:
            raise self._raises
        return FakeInfo(self._sha)

    def list_repo_tree(self, repo_id: str, **kwargs: Any) -> Iterator[Any]:
        self.tree_calls.append({"repo_id": repo_id, **kwargs})
        if self._raises is not None:
            raise self._raises
        yield from self._tree

    def whoami(self) -> dict[str, Any]:
        if self._raises is not None:
            raise self._raises
        return self._whoami


def build(api: FakeApi, *, token: str | None = "hf_test_token") -> HubSource:
    settings = HubSettings(token=SecretStr(token) if token else None, chunk_size=1024)
    return HubSource(settings, api=api)


@pytest.fixture
def pinned() -> PinnedRepo:
    return PinnedRepo(
        repo_id="org/name",
        repo_type=RepoType.models,
        revision_requested="main",
        commit_sha=COMMIT,
    )


# ------------------------------------------------------------------------ pin()


def test_pin_resolves_a_moving_ref_to_an_immutable_commit_sha() -> None:
    api = FakeApi()
    result = build(api).pin(RepoRef("org/name", RepoType.models, "main"))
    assert result.commit_sha == COMMIT
    assert len(result.commit_sha) == 40
    assert result.revision_requested == "main"
    assert result.repo_id == "org/name"
    assert result.repo_type is RepoType.models


def test_pin_asks_the_hub_for_the_requested_revision() -> None:
    api = FakeApi()
    build(api).pin(RepoRef("org/name", RepoType.models, "v2.0"))
    assert api.repo_info_calls[0]["revision"] == "v2.0"


@pytest.mark.parametrize(
    ("repo_type", "expected"), [(RepoType.models, "model"), (RepoType.datasets, "dataset")]
)
def test_pin_translates_the_plural_repo_type_to_the_hub_singular(
    repo_type: RepoType, expected: str
) -> None:
    """aimm uses the URL-shaped plural; `huggingface_hub` wants the singular."""
    api = FakeApi()
    build(api).pin(RepoRef("org/name", repo_type, "main"))
    assert api.repo_info_calls[0]["repo_type"] == expected


def test_pin_is_immutable() -> None:
    api = FakeApi()
    result = build(api).pin(RepoRef("org/name", RepoType.models, "main"))
    with pytest.raises(AttributeError):
        result.commit_sha = "x" * 40


def test_pin_rejects_a_hub_response_without_a_sha() -> None:
    """A silently missing SHA would pin the whole backup to `None`."""
    with pytest.raises(errors.SourceError):
        build(FakeApi(sha=None)).pin(RepoRef("org/name", RepoType.models, "main"))


def test_pin_rejects_an_empty_sha() -> None:
    with pytest.raises(errors.SourceError):
        build(FakeApi(sha="")).pin(RepoRef("org/name", RepoType.models, "main"))


def test_pin_validates_the_repository_id_before_calling_the_hub() -> None:
    api = FakeApi()
    with pytest.raises(errors.ConfigError):
        build(api).pin(RepoRef("not a valid repo id!!", RepoType.models, "main"))
    assert api.repo_info_calls == []


# ------------------------------------------------------ exception translation order


def test_a_gated_repository_is_reported_as_gated_not_as_missing() -> None:
    """`GatedRepoError` subclasses `RepositoryNotFoundError`. If the clause order is
    wrong this returns `RepoNotFoundError` and the operator hunts a nonexistent typo
    instead of accepting the licence."""
    assert issubclass(GatedRepoError, RepositoryNotFoundError)
    api = FakeApi(raises=hub_http_error(GatedRepoError, 403))
    with pytest.raises(errors.RepoGatedError) as caught:
        build(api).pin(RepoRef("meta-llama/Llama-3", RepoType.models, "main"))
    assert not isinstance(caught.value, errors.RepoNotFoundError)
    assert "licence" in str(caught.value).lower()


def test_a_missing_repository_is_reported_as_missing() -> None:
    api = FakeApi(raises=hub_http_error(RepositoryNotFoundError, 404))
    with pytest.raises(errors.RepoNotFoundError):
        build(api).pin(RepoRef("org/gone", RepoType.models, "main"))


def test_a_missing_revision_is_distinguished_from_a_missing_repository() -> None:
    api = FakeApi(raises=hub_http_error(RevisionNotFoundError, 404))
    with pytest.raises(errors.RevisionNotFoundError):
        build(api).pin(RepoRef("org/name", RepoType.models, "no-such-tag"))


def test_a_missing_file_is_reported_as_a_file_error(pinned: PinnedRepo) -> None:
    """`RemoteEntryNotFoundError` derives from `HfHubHTTPError` and must precede it."""
    assert issubclass(RemoteEntryNotFoundError, HfHubHTTPError)
    api = FakeApi(raises=hub_http_error(RemoteEntryNotFoundError, 404))
    with pytest.raises(errors.FileNotInRepoError):
        build(api).list_files(pinned)


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_credential_is_reported_as_an_auth_error(status: int) -> None:
    api = FakeApi(raises=hub_http_error(HfHubHTTPError, status))
    with pytest.raises(errors.AuthError):
        build(api).pin(RepoRef("org/name", RepoType.models, "main"))


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_server_error_is_a_source_error_not_an_auth_error(status: int) -> None:
    api = FakeApi(raises=hub_http_error(HfHubHTTPError, status))
    with pytest.raises(errors.SourceError) as caught:
        build(api).pin(RepoRef("org/name", RepoType.models, "main"))
    assert not isinstance(caught.value, errors.AuthError)


def test_a_malformed_repository_id_is_a_configuration_error() -> None:
    api = FakeApi(raises=HFValidationError("repo id is malformed"))
    with pytest.raises(errors.ConfigError):
        build(api).pin(RepoRef("org/name", RepoType.models, "main"))


def test_the_translated_error_never_carries_the_token() -> None:
    api = FakeApi(raises=hub_http_error(HfHubHTTPError, 401))
    with pytest.raises(errors.AuthError) as caught:
        build(api, token="hf_super_secret_value").pin(RepoRef("org/name", RepoType.models, "main"))
    assert "hf_super_secret_value" not in str(caught.value)


def test_every_translated_error_keeps_the_original_as_its_cause() -> None:
    """`raise ... from exc` keeps the Hub's own message reachable at DEBUG."""
    original = hub_http_error(GatedRepoError, 403)
    with pytest.raises(errors.RepoGatedError) as caught:
        build(FakeApi(raises=original)).pin(RepoRef("org/name", RepoType.models, "main"))
    assert caught.value.__cause__ is original


# ------------------------------------------------------------------ list_files()


def test_list_files_splits_the_integrity_anchor_by_lfs_flag(pinned: PinnedRepo) -> None:
    """The single most important assertion in this file. `blob_id` of an LFS file is
    the SHA-1 of the pointer and will never equal the payload digest."""
    api = FakeApi(
        tree=[
            repo_file("config.json", size=42, oid="c" * 40),
            repo_file("model.safetensors", size=9_000_000, oid="p" * 40, sha256="d" * 64),
        ]
    )
    files = {f.path: f for f in build(api).list_files(pinned)}

    plain = files["config.json"]
    assert plain.is_lfs is False
    assert plain.sha256 is None
    assert plain.blob_id == "c" * 40

    lfs = files["model.safetensors"]
    assert lfs.is_lfs is True
    assert lfs.sha256 == "d" * 64
    assert lfs.blob_id == "p" * 40
    assert lfs.blob_id != lfs.sha256


def test_list_files_records_size_and_path(pinned: PinnedRepo) -> None:
    api = FakeApi(tree=[repo_file("a/b.bin", size=1234)])
    (file,) = build(api).list_files(pinned)
    assert file.path == "a/b.bin"
    assert file.size == 1234


def test_list_files_skips_folders(pinned: PinnedRepo) -> None:
    """`list_repo_tree` yields both `RepoFile` and `RepoFolder`; a folder is not a file
    and must not become a zero-byte object."""
    api = FakeApi(
        tree=[repo_folder("subdir"), repo_file("subdir/model.bin"), repo_folder("nested")]
    )
    assert [f.path for f in build(api).list_files(pinned)] == ["subdir/model.bin"]


def test_list_files_is_sorted_by_path(pinned: PinnedRepo) -> None:
    """Manifests and plans must be byte-stable across runs, so ordering is fixed here
    rather than left to whatever order the Hub happened to return."""
    api = FakeApi(tree=[repo_file(name) for name in ("z.bin", "a.bin", "m/x.bin", "b/a.bin")])
    paths = [f.path for f in build(api).list_files(pinned)]
    assert paths == sorted(paths)
    assert paths == ["a.bin", "b/a.bin", "m/x.bin", "z.bin"]


def test_list_files_enumerates_against_the_pinned_sha_not_the_ref(pinned: PinnedRepo) -> None:
    """Enumerating against `main` after pinning would reintroduce the torn snapshot."""
    api = FakeApi(tree=[repo_file("a.bin")])
    build(api).list_files(pinned)
    call = api.tree_calls[0]
    assert call["revision"] == COMMIT
    assert call["revision"] != pinned.revision_requested
    assert call["recursive"] is True
    assert call["expand"] is False


def test_list_files_rejects_a_hostile_path_from_the_hub(pinned: PinnedRepo) -> None:
    """The Hub is a public, user-writable namespace; its paths are hostile input."""
    api = FakeApi(tree=[repo_file("a.bin"), repo_file("../../etc/cron.d/x")])
    with pytest.raises(errors.UnsafePathError):
        build(api).list_files(pinned)


def test_list_files_on_an_empty_repository(pinned: PinnedRepo) -> None:
    assert build(FakeApi(tree=[])).list_files(pinned) == []


def test_list_files_translates_a_gated_repository(pinned: PinnedRepo) -> None:
    api = FakeApi(raises=hub_http_error(GatedRepoError, 403))
    with pytest.raises(errors.RepoGatedError):
        build(api).list_files(pinned)


# ------------------------------------------------------------------- open_stream()


class FakeResponse:
    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.closed = False

    def iter_bytes(self, size: int) -> Iterator[bytes]:
        for start in range(0, len(self._payload), size):
            yield self._payload[start : start + size]


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    @contextmanager
    def stream(self, method: str, url: str, **kwargs: Any) -> Iterator[FakeResponse]:
        self.calls.append({"method": method, "url": url, **kwargs})
        try:
            yield self._response
        finally:
            self._response.closed = True


@pytest.fixture
def fake_session(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    session = FakeSession(FakeResponse(b"payload-bytes-" * 100))
    monkeypatch.setattr(source_module, "get_session", lambda: session)
    monkeypatch.setattr(source_module, "hf_raise_for_status", lambda response: None)
    return session


def test_open_stream_yields_the_payload_in_chunks(
    pinned: PinnedRepo, fake_session: FakeSession
) -> None:
    with build(FakeApi()).open_stream(pinned, repo_file("a.bin")) as chunks:
        collected = list(chunks)
    assert b"".join(collected) == b"payload-bytes-" * 100
    assert all(len(chunk) <= 1024 for chunk in collected)


def test_open_stream_requests_the_pinned_commit(
    pinned: PinnedRepo, fake_session: FakeSession
) -> None:
    with build(FakeApi()).open_stream(pinned, repo_file("a/b.bin")) as chunks:
        list(chunks)
    call = fake_session.calls[0]
    assert call["method"] == "GET"
    assert COMMIT in call["url"]
    assert call["follow_redirects"] is True


def test_open_stream_closes_the_response_on_the_way_out(
    pinned: PinnedRepo, fake_session: FakeSession
) -> None:
    with build(FakeApi()).open_stream(pinned, repo_file("a.bin")) as chunks:
        list(chunks)
    assert fake_session._response.closed is True


def test_open_stream_closes_the_response_even_on_an_exception(
    pinned: PinnedRepo, fake_session: FakeSession
) -> None:
    with pytest.raises(RuntimeError), build(FakeApi()).open_stream(pinned, repo_file("a.bin")):
        raise RuntimeError("caller blew up mid-transfer")
    assert fake_session._response.closed is True


def test_open_stream_translates_an_http_error(
    pinned: PinnedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession(FakeResponse(b"", status=404))
    monkeypatch.setattr(source_module, "get_session", lambda: session)

    def raise_missing(response: Any) -> None:
        raise hub_http_error(RemoteEntryNotFoundError, 404)

    monkeypatch.setattr(source_module, "hf_raise_for_status", raise_missing)
    with (
        pytest.raises(errors.FileNotInRepoError),
        build(FakeApi()).open_stream(pinned, repo_file("gone.bin")) as chunks,
    ):
        list(chunks)


def test_read_bytes_returns_the_whole_payload(
    pinned: PinnedRepo, fake_session: FakeSession
) -> None:
    assert build(FakeApi()).read_bytes(pinned, repo_file("a.bin")) == b"payload-bytes-" * 100


# ------------------------------------------------------------------------ staged()


def test_staged_yields_the_downloaded_payload(
    pinned: PinnedRepo, staging_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_download(repo_id: str, filename: str, **kwargs: Any) -> str:
        target = Path(kwargs["local_dir"]) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"downloaded")
        return str(target)

    monkeypatch.setattr(source_module, "hf_hub_download", fake_download)
    with build(FakeApi()).staged(pinned, repo_file("a/b.bin"), staging_dir) as path:
        assert path.read_bytes() == b"downloaded"


def test_staged_downloads_the_pinned_commit(
    pinned: PinnedRepo, staging_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_download(repo_id: str, filename: str, **kwargs: Any) -> str:
        seen.update({"repo_id": repo_id, "filename": filename, **kwargs})
        target = Path(kwargs["local_dir"]) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        return str(target)

    monkeypatch.setattr(source_module, "hf_hub_download", fake_download)
    with build(FakeApi()).staged(pinned, repo_file("a/b.bin"), staging_dir):
        pass
    assert seen["revision"] == COMMIT
    assert seen["repo_type"] == "model"
    assert seen["filename"] == "a/b.bin"
    # Removed in huggingface_hub 1.0; passing them would raise.
    assert "local_dir_use_symlinks" not in seen
    assert "resume_download" not in seen
    assert "force_filename" not in seen


def test_staged_removes_the_whole_directory_not_just_the_payload(
    pinned: PinnedRepo, staging_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing only the payload leaks `.cache/huggingface/download/<f>.metadata`, a
    possible `.lock`, plus `.gitignore` and `CACHEDIR.TAG`. Across millions of files
    that is an unbounded inode leak."""

    def fake_download(repo_id: str, filename: str, **kwargs: Any) -> str:
        local_dir = Path(kwargs["local_dir"])
        cache = local_dir / ".cache" / "huggingface" / "download"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / f"{filename}.metadata").write_text("meta", encoding="utf-8")
        (local_dir / ".gitignore").write_text("*", encoding="utf-8")
        (local_dir / "CACHEDIR.TAG").write_text("tag", encoding="utf-8")
        target = local_dir / filename
        target.write_bytes(b"payload")
        return str(target)

    monkeypatch.setattr(source_module, "hf_hub_download", fake_download)
    with build(FakeApi()).staged(pinned, repo_file("model.bin"), staging_dir) as path:
        assert path.exists()
        local_dir = path.parent
        assert (local_dir / ".gitignore").exists()
    assert not local_dir.exists()
    assert list(staging_dir.iterdir()) == []


def test_staged_cleans_up_when_the_caller_raises(
    pinned: PinnedRepo, staging_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_download(repo_id: str, filename: str, **kwargs: Any) -> str:
        target = Path(kwargs["local_dir"]) / filename
        target.write_bytes(b"payload")
        return str(target)

    monkeypatch.setattr(source_module, "hf_hub_download", fake_download)
    with (
        pytest.raises(RuntimeError),
        build(FakeApi()).staged(pinned, repo_file("model.bin"), staging_dir),
    ):
        raise RuntimeError("upload failed halfway")
    assert list(staging_dir.iterdir()) == []


def test_staged_cleans_up_when_the_download_itself_fails(
    pinned: PinnedRepo, staging_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_download(repo_id: str, filename: str, **kwargs: Any) -> str:
        Path(kwargs["local_dir"]).mkdir(parents=True, exist_ok=True)
        raise hub_http_error(RemoteEntryNotFoundError, 404)

    monkeypatch.setattr(source_module, "hf_hub_download", fake_download)
    with (
        pytest.raises(errors.FileNotInRepoError),
        build(FakeApi()).staged(pinned, repo_file("gone.bin"), staging_dir),
    ):
        pass
    assert list(staging_dir.iterdir()) == []


def test_staged_isolates_each_file_in_its_own_directory(
    pinned: PinnedRepo, staging_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two files with the same basename in different folders must not collide."""
    seen: list[Path] = []

    def fake_download(repo_id: str, filename: str, **kwargs: Any) -> str:
        local_dir = Path(kwargs["local_dir"])
        seen.append(local_dir)
        target = local_dir / Path(filename).name
        target.write_bytes(b"x")
        return str(target)

    monkeypatch.setattr(source_module, "hf_hub_download", fake_download)
    hub = build(FakeApi())
    with hub.staged(pinned, repo_file("a/model.bin"), staging_dir):
        pass
    with hub.staged(pinned, repo_file("b/model.bin"), staging_dir):
        pass
    assert seen[0] != seen[1]
    assert all(directory.parent == staging_dir for directory in seen)


def test_staged_directory_name_is_short_enough_for_windows(
    pinned: PinnedRepo, staging_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows' 260-character path limit is still real; a deep repo path must not be
    reproduced verbatim under the staging root."""
    seen: list[Path] = []

    def fake_download(repo_id: str, filename: str, **kwargs: Any) -> str:
        local_dir = Path(kwargs["local_dir"])
        seen.append(local_dir)
        target = local_dir / Path(filename).name
        target.write_bytes(b"x")
        return str(target)

    monkeypatch.setattr(source_module, "hf_hub_download", fake_download)
    deep = "/".join(f"segment-{i:03d}" for i in range(20)) + "/model.bin"
    with build(FakeApi()).staged(pinned, repo_file(deep), staging_dir):
        pass
    assert len(seen[0].name) == 16


# ------------------------------------------------------------------------ whoami()


def test_whoami_returns_the_authenticated_user() -> None:
    assert build(FakeApi(whoami_result={"name": "bauer-group"})).whoami() == "bauer-group"


def test_whoami_returns_none_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """`None` means "unauthenticated", which `doctor` reports as a finding."""
    monkeypatch.setattr(source_module, "get_token", lambda: None)
    assert build(FakeApi(), token=None).whoami() is None


def test_whoami_raises_when_a_present_token_is_rejected() -> None:
    """Reporting "unauthenticated" here would hide a misconfigured credential."""
    api = FakeApi(raises=hub_http_error(HfHubHTTPError, 401))
    with pytest.raises(errors.AuthError):
        build(api).whoami()


def test_whoami_tolerates_a_response_without_a_name() -> None:
    assert build(FakeApi(whoami_result={})).whoami() is None


# ---------------------------------------------------------------------- the client


def test_a_missing_token_falls_back_to_the_ambient_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(source_module, "get_token", lambda: "hf_ambient")
    hub = build(FakeApi(), token=None)
    assert hub.whoami() == "bauer-group"


def test_an_explicit_token_means_the_ambient_credential_is_never_consulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured token must win outright: silently preferring the machine's stored
    credential would back up under the wrong identity."""
    consulted: list[bool] = []

    def spy() -> str:
        consulted.append(True)
        return "hf_ambient"

    monkeypatch.setattr(source_module, "get_token", spy)
    build(FakeApi(), token="hf_explicit")
    assert consulted == []


def test_the_configured_token_reaches_the_streaming_request(
    pinned: PinnedRepo, fake_session: FakeSession
) -> None:
    """The raw streaming path bypasses `HfApi`, so it has to carry the token itself;
    Hugging Face names an unauthenticated request as the top cause of throttling."""
    with build(FakeApi(), token="hf_explicit").open_stream(pinned, repo_file("a.bin")) as chunks:
        list(chunks)
    headers = fake_session.calls[0]["headers"]
    assert headers["authorization"] == "Bearer hf_explicit"


# ── regressions: timeouts, retries and exception typing ──────────────────────


@pytest.fixture
def instant_hub_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the source layer's retry sleep for zero seconds.

    `call_with_retry` binds `sleep` as a keyword-only default at definition time, so
    patching `time.sleep` has no effect. Replacing the module-level name the source
    actually calls is the honest seam, exactly as `instant_retry` does for the
    destination.
    """
    real = source_module.call_with_retry

    def instant(fn: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("sleep", lambda _seconds: None)
        return real(fn, **kwargs)

    monkeypatch.setattr(source_module, "call_with_retry", instant)


class FlakySession:
    """Hands out a fresh response per attempt and records how many were opened."""

    def __init__(self, payload: bytes = b"payload") -> None:
        self.payload = payload
        self.responses: list[FakeResponse] = []
        self.calls: list[dict[str, Any]] = []

    @contextmanager
    def stream(self, method: str, url: str, **kwargs: Any) -> Iterator[FakeResponse]:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = FakeResponse(self.payload)
        self.responses.append(response)
        try:
            yield response
        finally:
            response.closed = True


class FlakyApi(FakeApi):
    """Fails the first `failures` calls with `error`, then behaves normally."""

    def __init__(self, error: Exception, failures: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._error = error
        self._failures = failures
        self.attempts = 0

    def _tick(self) -> None:
        self.attempts += 1
        if self.attempts <= self._failures:
            raise self._error

    def repo_info(self, repo_id: str, **kwargs: Any) -> FakeInfo:
        self._tick()
        return super().repo_info(repo_id, **kwargs)

    def list_repo_tree(self, repo_id: str, **kwargs: Any) -> Iterator[Any]:
        self._tick()
        yield from super().list_repo_tree(repo_id, **kwargs)


def test_open_stream_passes_an_explicit_timeout(
    pinned: PinnedRepo, fake_session: FakeSession
) -> None:
    """Regression: a stalled CDN connection blocked a worker thread forever.

    huggingface_hub 1.x builds its shared client with `timeout=None`, and the Hub's own
    downloader only escapes that by passing a per-request timeout. This call site passed
    none, so a blackholed TCP connection produced no bytes, no exception and no progress
    — nothing for the retry layer to catch and no way for the run to fail.
    """
    with build(FakeApi()).open_stream(pinned, repo_file("a.bin")) as chunks:
        list(chunks)

    timeout = fake_session.calls[0]["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 120.0
    assert timeout.connect == 15.0


def test_open_stream_honours_the_configured_timeouts(
    pinned: PinnedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession(FakeResponse(b"x"))
    monkeypatch.setattr(source_module, "get_session", lambda: session)
    monkeypatch.setattr(source_module, "hf_raise_for_status", lambda response: None)
    settings = HubSettings(chunk_size=1024, connect_timeout=3.0, read_timeout=7.0)

    with HubSource(settings, api=FakeApi()).open_stream(pinned, repo_file("a.bin")) as chunks:
        list(chunks)

    timeout = session.calls[0]["timeout"]
    assert timeout.connect == 3.0
    assert timeout.read == 7.0


def test_open_stream_retries_a_rate_limited_response(
    pinned: PinnedRepo, monkeypatch: pytest.MonkeyPatch, instant_hub_retry: None
) -> None:
    """Regression: the INLINE path had no 429 handling whatsoever.

    `net/retry.py` justifies adding no backoff of its own by pointing at hub's
    RateLimit-aware sleep — but that lives inside `http_stream_backoff`, which this raw
    call bypasses. So a throttled resolver failed every small file of a repository at
    once, `_sync_repo` withheld the manifest, and the next run re-transferred everything
    and re-triggered the throttling.
    """
    session = FlakySession()
    monkeypatch.setattr(source_module, "get_session", lambda: session)
    raised = {"count": 0}

    def rate_limited(response: Any) -> None:
        if raised["count"] < 2:
            raised["count"] += 1
            raise hub_http_error(HfHubHTTPError, 429)

    monkeypatch.setattr(source_module, "hf_raise_for_status", rate_limited)

    with build(FakeApi()).open_stream(pinned, repo_file("a.bin")) as chunks:
        assert b"".join(chunks) == b"payload"

    assert len(session.calls) == 3, "the rate-limited attempts were not retried"
    assert all(response.closed for response in session.responses[:-1]), (
        "a retried attempt must close its response or it leaks a connection"
    )


def test_open_stream_does_not_retry_a_permanent_failure(
    pinned: PinnedRepo, monkeypatch: pytest.MonkeyPatch, instant_hub_retry: None
) -> None:
    """A missing file is not transient; retrying it just delays the diagnosis."""
    session = FlakySession()
    monkeypatch.setattr(source_module, "get_session", lambda: session)

    def missing(response: Any) -> None:
        raise hub_http_error(RemoteEntryNotFoundError, 404)

    monkeypatch.setattr(source_module, "hf_raise_for_status", missing)

    with (
        pytest.raises(errors.FileNotInRepoError),
        build(FakeApi()).open_stream(pinned, repo_file("gone.bin")),
    ):
        pass

    assert len(session.calls) == 1


def test_a_transport_failure_is_translated_into_a_typed_error(
    pinned: PinnedRepo, monkeypatch: pytest.MonkeyPatch, instant_hub_retry: None
) -> None:
    """Regression: httpx transport errors escaped `_translated` unwrapped.

    `httpx.ConnectError` is neither an `HfHubHTTPError` nor an `HFValidationError`, so it
    passed straight through the translation and `aimm.main.run` reported "unexpected
    internal error" with a raw traceback and exit code 1 — including from `doctor`, the
    command that exists to diagnose exactly this.
    """

    class DeadSession:
        def stream(self, *args: Any, **kwargs: Any) -> Any:
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(source_module, "get_session", lambda: DeadSession())

    with (
        pytest.raises(errors.SourceError) as caught,
        build(FakeApi()).open_stream(pinned, repo_file("a.bin")),
    ):
        pass
    assert isinstance(caught.value.__cause__, httpx.ConnectError)


def test_whoami_translates_a_transport_failure() -> None:
    """`doctor` behind a proxy that blocks huggingface.co must render a FAILED row."""

    class DeadApi(FakeApi):
        def whoami(self) -> dict[str, Any]:
            raise httpx.ConnectTimeout("timed out")

    with pytest.raises(errors.SourceError):
        build(DeadApi()).whoami()


def test_a_download_that_produced_no_local_file_is_typed(
    pinned: PinnedRepo, staging_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LocalEntryNotFoundError` subclasses FileNotFoundError, not HfHubHTTPError."""

    def fake_download(repo_id: str, filename: str, **kwargs: Any) -> str:
        raise LocalEntryNotFoundError("cannot reach the Hub and nothing is cached")

    monkeypatch.setattr(source_module, "hf_hub_download", fake_download)
    with (
        pytest.raises(errors.SourceError),
        build(FakeApi()).staged(pinned, repo_file("a.bin"), staging_dir),
    ):
        pass


@pytest.mark.parametrize("status", [500, 502, 503])
def test_pin_retries_a_transient_hub_failure(status: int, instant_hub_retry: None) -> None:
    """Regression: one 503 on `pin` aborted an entire multi-repository sync.

    `Engine.sync` has no per-repo guard, so the exception escaped the generator and
    discarded the reports of every repository that had already succeeded.
    """
    api = FlakyApi(hub_http_error(HfHubHTTPError, status), failures=2)
    result = build(api).pin(RepoRef("org/name", RepoType.models, "main"))
    assert result.commit_sha == COMMIT
    assert api.attempts == 3


def test_pin_gives_up_on_a_permanent_failure(instant_hub_retry: None) -> None:
    api = FlakyApi(hub_http_error(RepositoryNotFoundError, 404), failures=99)
    with pytest.raises(errors.RepoNotFoundError):
        build(api).pin(RepoRef("org/gone", RepoType.models, "main"))
    assert api.attempts == 1, "a 404 is permanent and must not be retried"


def test_list_files_retries_a_transient_hub_failure(
    pinned: PinnedRepo, instant_hub_retry: None
) -> None:
    api = FlakyApi(hub_http_error(HfHubHTTPError, 503), failures=1, tree=[repo_file("a.bin")])
    files = build(api).list_files(pinned)
    assert [f.path for f in files] == ["a.bin"]
    assert api.attempts == 2
