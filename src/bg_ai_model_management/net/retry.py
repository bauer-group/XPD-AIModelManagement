"""Narrow retry policy: transient failures only, never permanent ones.

Deliberately does **not** add extra backoff around HTTP 429 from the Hugging Face Hub.
``huggingface_hub`` >= 1.2.0 parses the ``RateLimit`` header and sleeps exactly until
reset; this layer sits outside and only engages once the Hub client itself gives up.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar, cast

import httpx
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from botocore.exceptions import ConnectionError as BotoConnectionError
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from bg_ai_model_management.errors import AimmError

P = ParamSpec("P")
T = TypeVar("T")

log = logging.getLogger(__name__)

RETRYABLE_S3_ERROR_CODES: frozenset[str] = frozenset(
    {
        "RequestTimeout",
        "RequestTimeoutException",
        "SlowDown",
        "ThrottlingException",
        "Throttling",
        "InternalError",
        "InternalServerError",
        "ServiceUnavailable",
        "RequestLimitExceeded",
        "BandwidthLimitExceeded",
        "TooManyRequests",
    }
)
RETRYABLE_HTTP_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

_RETRYABLE_BOTO_TRANSPORT = (
    BotoConnectionError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)


def _status_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status of an httpx-shaped exception, else None."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def is_retryable(exc: BaseException) -> bool:
    """True only for transient failures.

    Retryable: httpx transport and timeout errors; any httpx-shaped HTTP error whose
    status is in ``RETRYABLE_HTTP_STATUS`` (this covers ``HfHubHTTPError``, which derives
    from ``httpx.HTTPError`` and carries a ``response``); botocore ``ClientError`` whose
    ``Error.Code`` is in ``RETRYABLE_S3_ERROR_CODES`` or whose HTTP status is retryable;
    botocore connection and timeout errors.

    Never retryable: every :class:`~bg_ai_model_management.errors.AimmError`, the Hub's permanent 401/403/404
    failures (``GatedRepoError``, ``RepositoryNotFoundError``, ``RevisionNotFoundError``),
    ``LocalEntryNotFoundError``, ``HFValidationError``, ``ValueError``, ``TypeError`` and
    ``KeyboardInterrupt`` — none of which present a retryable status or type below.
    """
    if isinstance(exc, AimmError):
        return False
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in RETRYABLE_S3_ERROR_CODES:
            return True
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return isinstance(status, int) and status in RETRYABLE_HTTP_STATUS
    if isinstance(exc, _RETRYABLE_BOTO_TRANSPORT):
        return True
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPError):
        status = _status_of(exc)
        return status is not None and status in RETRYABLE_HTTP_STATUS
    return False


def call_with_retry[T](
    fn: Callable[[], T],
    *,
    attempts: int = 5,
    max_wait: float = 60.0,
    multiplier: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[BaseException, int], None] | None = None,
) -> T:
    """Call ``fn`` with full-jitter exponential backoff on transient failures only.

    ``reraise=True`` is mandatory: tenacity's default wraps the real exception in
    ``RetryError``, after which every ``except DestinationError`` upstream silently stops
    matching. ``sleep`` is injectable so tests run instantly.

    Raises:
        Whatever ``fn`` raises, once the attempts are exhausted.
    """

    def before_sleep(state: RetryCallState) -> None:
        outcome = state.outcome
        exc = outcome.exception() if outcome is not None else None
        if exc is None:
            return
        log.warning(
            "attempt %d failed with %s; retrying", state.attempt_number, type(exc).__name__
        )
        if on_retry is not None:
            on_retry(exc, state.attempt_number)

    retrying = Retrying(
        retry=retry_if_exception(is_retryable),
        wait=wait_random_exponential(multiplier=multiplier, max=max_wait),
        stop=stop_after_attempt(attempts),
        reraise=True,
        sleep=cast(Callable[[Any], None], sleep),
        before_sleep=before_sleep,
    )
    return retrying(fn)


def retryable(
    *,
    attempts: int = 5,
    max_wait: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Decorator form of :func:`call_with_retry`."""

    def decorator(fn: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            return call_with_retry(
                lambda: fn(*args, **kwargs),
                attempts=attempts,
                max_wait=max_wait,
                sleep=sleep,
            )

        return wrapper

    return decorator
