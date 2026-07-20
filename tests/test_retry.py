"""Retry policy: transient failures only, and the original exception on the way out.

Two failure modes this file exists to catch. Retrying a *permanent* error turns a
one-second "access denied" into a minute of pointless backoff before the same message.
And losing the exception type — tenacity's default wraps it in `RetryError` — makes
every `except DestinationError` upstream silently stop matching.

Every test injects `sleep`, so the whole file runs in microseconds.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from botocore.exceptions import ConnectionError as BotoConnectionError
from huggingface_hub.errors import (
    GatedRepoError,
    HFValidationError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
)

from bg_ai_model_management import errors
from bg_ai_model_management.net.retry import (
    RETRYABLE_HTTP_STATUS,
    RETRYABLE_S3_ERROR_CODES,
    call_with_retry,
    is_retryable,
    retryable,
)

from .conftest import FakeSleep

_REQUEST = httpx.Request("GET", "https://huggingface.co/api/models/org/name")


def client_error(code: str = "", *, status: int | None = None) -> ClientError:
    """A botocore ClientError shaped like a real S3 error response."""
    response: Any = {"Error": {"Code": code, "Message": code}}
    if status is not None:
        response["ResponseMetadata"] = {"HTTPStatusCode": status}
    return ClientError(response, "PutObject")


def hub_error(exc_type: Any, status: int) -> Exception:
    error: Exception = exc_type("hub said no", response=httpx.Response(status, request=_REQUEST))
    return error


# ------------------------------------------------------------------- classification


@pytest.mark.parametrize("code", sorted(RETRYABLE_S3_ERROR_CODES))
def test_retryable_s3_error_codes(code: str) -> None:
    assert is_retryable(client_error(code)) is True


@pytest.mark.parametrize("status", sorted(RETRYABLE_HTTP_STATUS))
def test_retryable_by_s3_response_status(status: int) -> None:
    assert is_retryable(client_error("Unmapped", status=status)) is True


@pytest.mark.parametrize("status", sorted(RETRYABLE_HTTP_STATUS))
def test_retryable_by_hub_response_status(status: int) -> None:
    assert (
        is_retryable(
            httpx.HTTPStatusError(
                "x", request=_REQUEST, response=httpx.Response(status, request=_REQUEST)
            )
        )
        is True
    )


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("timed out"),
        httpx.WriteTimeout("timed out"),
        httpx.PoolTimeout("timed out"),
        httpx.ConnectError("refused"),
        httpx.ReadError("reset"),
        httpx.RemoteProtocolError("truncated"),
        BotoConnectionError(error=OSError("down")),
        EndpointConnectionError(endpoint_url="https://s3.invalid"),
        ConnectTimeoutError(endpoint_url="https://s3.invalid"),
        ReadTimeoutError(endpoint_url="https://s3.invalid"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_transport_failures_are_retryable(exc: BaseException) -> None:
    assert is_retryable(exc) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 409, 412, 416, 501])
def test_permanent_http_statuses_are_not_retryable(status: int) -> None:
    """403 and 404 are the ones that matter: retrying them wastes a minute per file."""
    response = httpx.Response(status, request=_REQUEST)
    assert is_retryable(httpx.HTTPStatusError("x", request=_REQUEST, response=response)) is False
    assert is_retryable(client_error("Whatever", status=status)) is False


@pytest.mark.parametrize(
    "exc",
    [
        client_error("AccessDenied"),
        client_error("NoSuchBucket"),
        client_error("NoSuchKey"),
        client_error("InvalidAccessKeyId"),
        client_error("SignatureDoesNotMatch"),
        client_error("EntityTooLarge"),
    ],
    ids=lambda e: str(e.response["Error"]["Code"]),
)
def test_permanent_s3_error_codes_are_not_retryable(exc: ClientError) -> None:
    assert is_retryable(exc) is False


@pytest.mark.parametrize(
    "exc",
    [
        hub_error(GatedRepoError, 403),
        hub_error(RepositoryNotFoundError, 404),
        hub_error(RevisionNotFoundError, 404),
        HFValidationError("repo id is malformed"),
        ValueError("bad value"),
        TypeError("bad type"),
        KeyboardInterrupt(),
        KeyError("missing"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_permanent_failures_are_not_retryable(exc: BaseException) -> None:
    assert is_retryable(exc) is False


@pytest.mark.parametrize(
    "exc_type",
    [
        errors.AimmError,
        errors.ConfigError,
        errors.UnsafePathError,
        errors.AuthError,
        errors.SourceError,
        errors.RepoGatedError,
        errors.DestinationError,
        errors.UploadFailedError,
        errors.IntegrityError,
        errors.ChecksumMismatchError,
        errors.TransferError,
        errors.DriftDetectedError,
    ],
    ids=lambda t: t.__name__,
)
def test_no_aimm_error_is_ever_retryable(exc_type: type[errors.AimmError]) -> None:
    """Our own errors are already-diagnosed decisions; retrying them is always wrong."""
    assert is_retryable(exc_type("decided")) is False


# ----------------------------------------------------------------- call_with_retry


def test_succeeds_without_sleeping_when_the_call_works(fake_sleep: FakeSleep) -> None:
    assert call_with_retry(lambda: "ok", sleep=fake_sleep) == "ok"
    assert fake_sleep.calls == []


def test_retries_a_transient_failure_then_succeeds(fake_sleep: FakeSleep) -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise client_error("SlowDown")
        return "ok"

    assert call_with_retry(flaky, attempts=5, sleep=fake_sleep) == "ok"
    assert calls["n"] == 3
    assert len(fake_sleep.calls) == 2


def test_a_non_retryable_error_is_not_retried(fake_sleep: FakeSleep) -> None:
    calls = {"n": 0}

    def denied() -> str:
        calls["n"] += 1
        raise client_error("AccessDenied")

    with pytest.raises(ClientError):
        call_with_retry(denied, attempts=5, sleep=fake_sleep)
    assert calls["n"] == 1
    assert fake_sleep.calls == []


def test_an_aimm_error_is_not_retried(fake_sleep: FakeSleep) -> None:
    calls = {"n": 0}

    def refuse() -> str:
        calls["n"] += 1
        raise errors.ChecksumMismatchError("digest mismatch")

    with pytest.raises(errors.ChecksumMismatchError):
        call_with_retry(refuse, attempts=5, sleep=fake_sleep)
    assert calls["n"] == 1


def test_exhausted_attempts_reraise_the_original_exception(fake_sleep: FakeSleep) -> None:
    """`reraise=True`: a RetryError here would break every `except` clause upstream."""
    calls = {"n": 0}

    def always() -> str:
        calls["n"] += 1
        raise client_error("ServiceUnavailable")

    with pytest.raises(ClientError) as caught:
        call_with_retry(always, attempts=4, sleep=fake_sleep)
    assert calls["n"] == 4
    assert len(fake_sleep.calls) == 3
    assert caught.value.response["Error"]["Code"] == "ServiceUnavailable"


def test_exhausted_attempts_preserve_a_narrow_exception_type(fake_sleep: FakeSleep) -> None:
    def always() -> str:
        raise httpx.ConnectTimeout("nope")

    with pytest.raises(httpx.ConnectTimeout):
        call_with_retry(always, attempts=2, sleep=fake_sleep)


def test_single_attempt_means_no_retry(fake_sleep: FakeSleep) -> None:
    calls = {"n": 0}

    def always() -> str:
        calls["n"] += 1
        raise client_error("SlowDown")

    with pytest.raises(ClientError):
        call_with_retry(always, attempts=1, sleep=fake_sleep)
    assert calls["n"] == 1
    assert fake_sleep.calls == []


def test_backoff_never_exceeds_max_wait(fake_sleep: FakeSleep) -> None:
    def always() -> str:
        raise client_error("SlowDown")

    with pytest.raises(ClientError):
        call_with_retry(always, attempts=8, max_wait=3.0, multiplier=1000.0, sleep=fake_sleep)
    assert fake_sleep.calls
    assert max(fake_sleep.calls) <= 3.0
    assert min(fake_sleep.calls) >= 0.0


def test_on_retry_callback_sees_every_retry(fake_sleep: FakeSleep) -> None:
    seen: list[tuple[str, int]] = []

    def always() -> str:
        raise client_error("InternalError")

    with pytest.raises(ClientError):
        call_with_retry(
            always,
            attempts=3,
            sleep=fake_sleep,
            on_retry=lambda exc, n: seen.append((type(exc).__name__, n)),
        )
    assert seen == [("ClientError", 1), ("ClientError", 2)]


def test_on_retry_is_not_called_on_success(fake_sleep: FakeSleep) -> None:
    seen: list[int] = []
    call_with_retry(lambda: 1, sleep=fake_sleep, on_retry=lambda exc, n: seen.append(n))
    assert seen == []


# ---------------------------------------------------------------------- decorator


def test_decorator_retries_and_passes_arguments_through(fake_sleep: FakeSleep) -> None:
    calls = {"n": 0}

    def upload(key: str, *, size: int) -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise client_error("Throttling")
        return f"{key}:{size}"

    wrapped: Callable[..., str] = retryable(attempts=3, sleep=fake_sleep)(upload)
    assert wrapped("a/b.bin", size=7) == "a/b.bin:7"
    assert calls["n"] == 2


def test_decorator_does_not_retry_a_permanent_failure(fake_sleep: FakeSleep) -> None:
    calls = {"n": 0}

    def denied() -> str:
        calls["n"] += 1
        raise client_error("AccessDenied")

    wrapped: Callable[..., str] = retryable(attempts=5, sleep=fake_sleep)(denied)
    with pytest.raises(ClientError):
        wrapped()
    assert calls["n"] == 1


def test_decorator_preserves_the_wrapped_function_identity() -> None:
    def named() -> None:
        """Docstring kept."""

    wrapped: Callable[..., None] = retryable()(named)
    assert wrapped.__name__ == "named"
    assert wrapped.__doc__ == "Docstring kept."
