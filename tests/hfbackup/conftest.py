"""Fixtures shared by the H2 test modules (destination, engine layer, CLI).

Everything here is deliberately narrow: a settings factory, a moto-backed
``S3Destination`` and a seam that makes the destination's retry layer instant. Fixtures
used by only one module stay in that module.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, TypeVar

import pytest

from bg_ai_model_management.config.models import (
    HubSettings,
    S3Settings,
    Settings,
    TransferSettings,
)
from bg_ai_model_management.tools.hfbackup import destination as destination_module
from bg_ai_model_management.tools.hfbackup.destination import S3Destination, _default_capabilities

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

T = TypeVar("T")

#: Every AIMM_* name that could leak from the developer's shell into a Settings build.
_LEAKY_ENV_PREFIXES = ("AIMM_", "AWS_ENDPOINT")

#: EXEMPT from the scrub. ``AIMM_IT_*`` addresses the MinIO integration rig and is read
#: by the ``minio_endpoint`` fixture, never by ``Settings`` — probed directly:
#: constructing ``Settings`` with ``AIMM_IT_ENDPOINT`` present raises nothing despite
#: ``env_prefix="AIMM_"`` and ``extra="forbid"``. Scrubbing it made the two
#: ``@pytest.mark.integration`` tests in this package skip unconditionally, so the whole
#: MinIO gate passed vacuously in CI with the rig booted and zero assertions run.
_RIG_ENV_PREFIX = "AIMM_IT_"


def scrub_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop ambient configuration so a developer's shell cannot change a test's outcome.

    ``Settings`` is a ``BaseSettings``: a stray ``AIMM_S3__BUCKET`` in the environment
    would silently override what a test passed in.
    """
    for name in list(os.environ):
        if name.startswith(_LEAKY_ENV_PREFIXES) and not name.startswith(_RIG_ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse wrapper around `scrub_ambient_env` for every test in this package."""
    scrub_ambient_env(monkeypatch)


@pytest.fixture
def make_settings(s3_bucket: str) -> Callable[..., Settings]:
    """Return a factory building `Settings` around the moto test bucket."""

    def factory(**overrides: Any) -> Settings:
        s3_kwargs: dict[str, Any] = {"bucket": s3_bucket, "prefix": "aimm", "probe": False}
        s3_kwargs.update(overrides.pop("s3", {}))
        transfer_kwargs: dict[str, Any] = {"workers": 2}
        transfer_kwargs.update(overrides.pop("transfer", {}))
        hub_kwargs: dict[str, Any] = {}
        hub_kwargs.update(overrides.pop("hub", {}))
        return Settings(
            s3=S3Settings(**s3_kwargs),
            transfer=TransferSettings(**transfer_kwargs),
            hub=HubSettings(**hub_kwargs),
            **overrides,
        )

    return factory


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    """Default settings pointing at the moto test bucket."""
    return make_settings()


@pytest.fixture
def destination(settings: Settings, s3_client: S3Client) -> S3Destination:
    """An `S3Destination` wrapping the moto client, with unprobed default capabilities.

    Built by calling the constructor rather than `create()` so a test can decide for
    itself whether the backend claims sha256 checksum support.
    """
    return S3Destination(settings.s3, s3_client, _default_capabilities(settings.s3))


@pytest.fixture
def instant_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `destination`'s retry layer sleep for zero seconds.

    `call_with_retry` takes `sleep` as a keyword-only default bound at definition time,
    so patching `time.sleep` has no effect. Replacing the module-level name the
    destination actually calls is the honest seam.
    """
    real = destination_module.call_with_retry

    def instant(fn: Callable[[], T], **kwargs: Any) -> T:
        kwargs.setdefault("sleep", lambda _seconds: None)
        return real(fn, **kwargs)

    monkeypatch.setattr(destination_module, "call_with_retry", instant)


class ChunkReader:
    """A `ByteReader` that hands out at most `max_chunk` bytes per call.

    Short reads are legal under the protocol, so every consumer must loop. Defaulting to
    a deliberately awkward chunk size keeps that requirement under test.
    """

    def __init__(self, data: bytes, *, max_chunk: int = 7) -> None:
        self._data = data
        self._pos = 0
        self._max_chunk = max_chunk

    def read(self, n: int, /) -> bytes:
        take = min(n, self._max_chunk, len(self._data) - self._pos)
        chunk = self._data[self._pos : self._pos + take]
        self._pos += take
        return chunk


class SpyClient:
    """Delegating S3 client proxy that records calls and can inject faults.

    moto gives a faithful S3; what it cannot give is a *failing* S3. This proxy supplies
    the failure paths — the abort-on-every-exception rule and the post-upload size check
    are otherwise untestable without a real broken server.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: operation name -> callable(call_index, kwargs) -> None to raise, or a response.
        self.faults: dict[str, Callable[[int, dict[str, Any]], Any]] = {}

    def _count(self, operation: str) -> int:
        return sum(1 for name, _ in self.calls if name == operation)

    def __getattr__(self, name: str) -> Any:
        inner_attr = getattr(self.inner, name)
        if not callable(inner_attr):
            return inner_attr

        def wrapper(**kwargs: Any) -> Any:
            index = self._count(name)
            self.calls.append((name, kwargs))
            fault = self.faults.get(name)
            if fault is not None:
                result = fault(index, kwargs)
                if result is not None:
                    return result
            return inner_attr(**kwargs)

        return wrapper

    def get_paginator(self, operation: str) -> Any:
        self.calls.append((f"paginate:{operation}", {}))
        return self.inner.get_paginator(operation)

    def params(self, operation: str) -> list[dict[str, Any]]:
        """Every keyword payload recorded for one operation, in call order."""
        return [kwargs for name, kwargs in self.calls if name == operation]

    def called(self, operation: str) -> bool:
        return any(name == operation for name, _ in self.calls)


@pytest.fixture
def spy_destination(
    settings: Settings, s3_client: S3Client
) -> Iterator[tuple[S3Destination, SpyClient]]:
    """An `S3Destination` over a recording, fault-injectable client proxy."""
    spy = SpyClient(s3_client)
    yield S3Destination(settings.s3, spy, _default_capabilities(settings.s3)), spy
