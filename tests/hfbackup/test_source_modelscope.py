"""Tests for the ModelScope source: wire format, pinning, and the anchor mapping.

Every fixture body here is shaped exactly like a live response captured from
modelscope.cn, including the three shapes that are easy to get wrong and that the
module exists to handle: refs served only over git's smart-HTTP endpoint, a failure
delivered as HTTP 200 with `Success: false`, and directory entries appearing in the
file list as `Type: "tree"`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from bg_ai_model_management import shutdown
from bg_ai_model_management.config.models import ModelScopeSettings
from bg_ai_model_management.errors import (
    AuthError,
    ConfigError,
    OperationCancelledError,
    RepoNotFoundError,
    RevisionNotFoundError,
    SourceError,
    UnsafePathError,
)
from bg_ai_model_management.tools.hfbackup.source_modelscope import (
    ModelScopeSource,
    _parse_pkt_refs,
)
from bg_ai_model_management.tools.hfbackup.types import (
    PinnedRepo,
    RepoRef,
    RepoType,
    SourceKind,
)

MASTER_SHA = "09b42cad3d112e832108974449ccb5e8e0f5b5d1"
TAG_SHA = "1" * 40


def pkt(payload: bytes) -> bytes:
    """Frame one git pkt-line: four hex digits of TOTAL length, then the payload."""
    return f"{len(payload) + 4:04x}".encode("ascii") + payload


REFS_BODY = (
    pkt(b"# service=git-upload-pack\n")
    + b"0000"
    + pkt(f"{MASTER_SHA} HEAD\x00multi_ack thin-pack side-band\n".encode())
    + pkt(f"{MASTER_SHA} refs/heads/master\n".encode())
    + pkt(f"{TAG_SHA} refs/tags/v1.0\n".encode())
    + b"0000"
)

FILES_BODY = {
    "Code": 200,
    "Data": {
        "Files": [
            {"Path": "assets", "Type": "tree", "Size": 0, "Sha256": "", "IsLFS": False},
            {
                "Path": "model.safetensors",
                "Type": "blob",
                "Size": 1503300328,
                "Sha256": "f4" * 32,
                "IsLFS": True,
            },
            {"Path": "config.json", "Type": "blob", "Size": 726, "Sha256": "66" * 32},
        ]
    },
    "Success": True,
}

NOT_FOUND_BODY = {
    "Code": 10010205001,
    # Verbatim from the live API — the fullwidth punctuation is real, not a typo.
    "Message": "获取模型目录树失败，信息：record not found",  # noqa: RUF001
    "Success": False,
}


def source_for(handler: object, *, token: str | None = None) -> ModelScopeSource:
    """A source whose transport is driven by `handler`."""
    settings = ModelScopeSettings(endpoint="https://modelscope.test", token=token)  # type: ignore[arg-type]
    client = httpx.Client(
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        follow_redirects=True,
    )
    return ModelScopeSource(settings, client=client)


def route(request: httpx.Request) -> httpx.Response:
    """Dispatch on the path, the way the live host does."""
    path = request.url.path
    if path.endswith("/info/refs"):
        return httpx.Response(200, content=REFS_BODY)
    if path.endswith("/repo/files"):
        return httpx.Response(200, json=FILES_BODY)
    if "/resolve/" in path:
        return httpx.Response(200, content=b"payload-bytes")
    return httpx.Response(404)


def ref(revision: str = "master", repo_type: RepoType = RepoType.models) -> RepoRef:
    return RepoRef("Qwen/Qwen3-0.6B", repo_type, revision)


def pinned() -> PinnedRepo:
    return PinnedRepo("Qwen/Qwen3-0.6B", RepoType.models, "master", MASTER_SHA)


@pytest.fixture(autouse=True)
def _no_leaked_shutdown() -> Iterator[None]:
    yield
    shutdown.reset()


# ── pin ──────────────────────────────────────────────────────────────────────
def test_the_source_declares_its_hub() -> None:
    """The manifest's digest provenance is selected from this."""
    assert ModelScopeSource.kind is SourceKind.modelscope


def test_pin_resolves_a_branch_to_its_head_sha() -> None:
    assert source_for(route).pin(ref("master")).commit_sha == MASTER_SHA


def test_pin_resolves_a_tag() -> None:
    assert source_for(route).pin(ref("v1.0")).commit_sha == TAG_SHA


def test_pin_sends_a_git_user_agent() -> None:
    """A generic agent gets "mirror self-forwarded loop detected" from the live host."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, content=REFS_BODY)

    source_for(handler).pin(ref())
    assert seen["user-agent"].startswith("git/")


def test_pin_passes_a_commit_sha_through_without_a_request() -> None:
    """A pinned SHA must not need the network — restoring an old revision relies on it."""

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("pin() must not call out for an explicit commit SHA")

    sha = "a" * 40
    assert source_for(refuse).pin(ref(sha)).commit_sha == sha


def test_pin_reports_an_unknown_revision_with_the_known_refs() -> None:
    with pytest.raises(RevisionNotFoundError) as excinfo:
        source_for(route).pin(ref("main"))  # ModelScope uses 'master'
    assert "refs/heads/master" in str(excinfo.value)


def test_pin_rejects_datasets_before_any_request() -> None:
    """ModelScope serves datasets from another API (live: HTTP 405), so fail early."""
    with pytest.raises(ConfigError):
        source_for(route).pin(ref(repo_type=RepoType.datasets))


def test_pin_reports_an_empty_ref_set_as_a_source_error() -> None:
    with pytest.raises(SourceError):
        source_for(lambda _r: httpx.Response(200, content=b"0000")).pin(ref())


# ── list_files ───────────────────────────────────────────────────────────────
def test_list_files_drops_trees_and_sorts_by_path() -> None:
    files = source_for(route).list_files(pinned())
    assert [file.path for file in files] == ["config.json", "model.safetensors"]


def test_every_file_is_presented_through_the_sha256_anchor() -> None:
    """The engine checks sha256 when is_lfs is set, and a git blob id otherwise.

    ModelScope attests a content sha256 for every file and publishes no blob id, so
    reporting is_lfs=False would verify against an anchor that does not exist.
    """
    files = source_for(route).list_files(pinned())
    assert all(file.is_lfs for file in files)
    assert all(file.blob_id == "" for file in files)
    assert files[0].sha256 == "66" * 32


def test_list_files_pins_the_requested_revision() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=FILES_BODY)

    source_for(handler).list_files(pinned())
    assert seen["Revision"] == MASTER_SHA
    assert seen["Recursive"] == "True"


def test_list_files_rejects_a_path_that_escapes_the_key_layout() -> None:
    body = {
        "Code": 200,
        "Success": True,
        "Data": {"Files": [{"Path": "../escape", "Type": "blob", "Size": 1, "Sha256": "ab"}]},
    }
    with pytest.raises(UnsafePathError):
        source_for(lambda _r: httpx.Response(200, json=body)).list_files(pinned())


# ── error translation ────────────────────────────────────────────────────────
def test_a_failure_envelope_on_http_200_becomes_repo_not_found() -> None:
    """The live API answers 200 for a missing repo; status alone is not success."""
    with pytest.raises(RepoNotFoundError):
        source_for(lambda _r: httpx.Response(200, json=NOT_FOUND_BODY)).list_files(pinned())


def test_an_unrecognised_envelope_code_becomes_a_source_error() -> None:
    body = {"Code": 500, "Message": "boom", "Success": False}
    with pytest.raises(SourceError):
        source_for(lambda _r: httpx.Response(200, json=body)).list_files(pinned())


def test_an_auth_status_becomes_an_auth_error() -> None:
    with pytest.raises(AuthError):
        source_for(lambda _r: httpx.Response(403)).list_files(pinned())


def test_a_transport_failure_becomes_a_source_error() -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(SourceError):
        source_for(boom).list_files(pinned())


# ── transfer ─────────────────────────────────────────────────────────────────
def test_read_bytes_reassembles_the_chunks() -> None:
    source = source_for(route)
    file = source.list_files(pinned())[0]
    assert source.read_bytes(pinned(), file) == b"payload-bytes"


def test_the_download_url_quotes_each_segment_but_keeps_slashes() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"x")

    source = source_for(handler)
    file = source._source_file(
        {"Path": "sub dir/we#ird.bin", "Size": 1, "Sha256": "ab"}, "test"
    )
    source.read_bytes(pinned(), file)
    assert seen[0].endswith(f"/models/Qwen/Qwen3-0.6B/resolve/{MASTER_SHA}/sub%20dir/we%23ird.bin")


def test_staged_writes_the_payload_and_always_cleans_up(tmp_path: Path) -> None:
    source = source_for(route)
    file = source.list_files(pinned())[0]
    with source.staged(pinned(), file, tmp_path) as path:
        assert path.read_bytes() == b"payload-bytes"
        staging_dir = path.parent
    assert not staging_dir.exists()


def test_staged_cleans_up_when_the_body_fails(tmp_path: Path) -> None:
    def fail_on_download(request: httpx.Request) -> httpx.Response:
        if "/resolve/" in request.url.path:
            return httpx.Response(500)
        return route(request)

    source = source_for(fail_on_download)
    file = source.list_files(pinned())[0]
    with pytest.raises(SourceError), source.staged(pinned(), file, tmp_path):
        pass  # pragma: no cover - the context manager raises before the body runs
    assert list(tmp_path.iterdir()) == []


def test_a_shutdown_signal_stops_a_download(tmp_path: Path) -> None:
    """A multi-gigabyte download must not outlive a SIGTERM by minutes."""
    source = source_for(route)
    file = source.list_files(pinned())[0]
    shutdown.request()
    with pytest.raises(OperationCancelledError), source.staged(pinned(), file, tmp_path):
        pass  # pragma: no cover - cancelled before the body runs
    assert list(tmp_path.iterdir()) == []


# ── identity and reachability ────────────────────────────────────────────────
def test_whoami_never_claims_an_identity() -> None:
    """doctor reports reachability instead; asserting an unverified user is worse."""
    assert source_for(route).whoami() is None


def test_ping_reports_the_credential_state_without_leaking_it() -> None:
    anonymous = source_for(lambda _r: httpx.Response(200, text="ok")).ping()
    assert "anonymous" in anonymous

    detail = source_for(lambda _r: httpx.Response(200, text="ok"), token="secret").ping()
    assert "token set" in detail
    assert "secret" not in detail


def test_ping_translates_an_unreachable_endpoint() -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(SourceError):
        source_for(boom).ping()


# ── pkt-line parsing ─────────────────────────────────────────────────────────
def test_the_pkt_line_parser_strips_the_capability_list() -> None:
    assert _parse_pkt_refs(REFS_BODY)["HEAD"] == MASTER_SHA


def test_the_pkt_line_parser_stops_on_a_malformed_header() -> None:
    assert _parse_pkt_refs(b"zzzz") == {}
    assert _parse_pkt_refs(b"0100short") == {}  # length points beyond the buffer
