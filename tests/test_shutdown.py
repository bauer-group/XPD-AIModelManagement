"""Tests for the cooperative shutdown flag and its signal handlers.

Signals are raised for real (``signal.raise_signal``) rather than by calling the
handler directly: the point of this module is what happens when the OS delivers
SIGTERM, and a handler invoked as a plain function proves nothing about that.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator

import pytest

from bg_ai_model_management import shutdown
from bg_ai_model_management.errors import EXIT_INTERRUPTED, OperationCancelledError


@pytest.fixture(autouse=True)
def clean_shutdown_state() -> Iterator[None]:
    """No test may leak a set flag or an installed handler into the next one."""
    shutdown.reset()
    yield
    shutdown.reset()


def test_nothing_is_requested_by_default() -> None:
    assert shutdown.is_requested() is False
    shutdown.raise_if_requested("noop")  # must not raise


def test_request_sets_the_flag_and_raise_if_requested_fires() -> None:
    shutdown.request()
    assert shutdown.is_requested() is True
    with pytest.raises(OperationCancelledError, match="upload of a/b: cancelled"):
        shutdown.raise_if_requested("upload of a/b")


def test_cancellation_carries_the_interrupted_exit_code() -> None:
    """Scripts distinguish "the operator stopped this" from "this broke"."""
    assert OperationCancelledError("x").exit_code == EXIT_INTERRUPTED


def test_sigterm_requests_shutdown_instead_of_killing_the_process() -> None:
    """The whole point: SIGTERM's default disposition would skip every finally block."""
    shutdown.install_handlers()
    signal.raise_signal(signal.SIGTERM)
    assert shutdown.is_requested() is True


def test_sigint_takes_the_same_orderly_route() -> None:
    shutdown.install_handlers()
    signal.raise_signal(signal.SIGINT)
    assert shutdown.is_requested() is True


def test_a_second_signal_hands_back_to_the_previous_handler() -> None:
    """An operator who signals twice must not be trapped by the graceful path.

    Asserted through a sentinel handler rather than the default disposition: the
    default would raise KeyboardInterrupt at whatever bytecode runs next, which is a
    race against the assertion and would abort the whole pytest session when it lost.
    """
    calls: list[str] = []
    original = signal.signal(signal.SIGTERM, lambda *_: calls.append("previous"))
    try:
        shutdown.install_handlers((signal.SIGTERM,))

        signal.raise_signal(signal.SIGTERM)
        assert shutdown.is_requested() is True
        assert calls == [], "the first signal must be handled cooperatively"

        signal.raise_signal(signal.SIGTERM)
        assert calls == ["previous"], "the second signal must reach the original handler"
    finally:
        signal.signal(signal.SIGTERM, original)


def test_reset_restores_the_previous_handler() -> None:
    original = signal.getsignal(signal.SIGTERM)
    shutdown.install_handlers((signal.SIGTERM,))
    assert signal.getsignal(signal.SIGTERM) is not original

    shutdown.reset()
    assert signal.getsignal(signal.SIGTERM) is original


def test_install_is_a_no_op_off_the_main_thread() -> None:
    """Registering a handler off the main thread raises; a backup must not die for it."""
    before = signal.getsignal(signal.SIGTERM)
    failures: list[BaseException] = []

    def install() -> None:
        try:
            shutdown.install_handlers((signal.SIGTERM,))
        except BaseException as exc:  # pragma: no cover - the assertion below reports it
            failures.append(exc)

    worker = threading.Thread(target=install)
    worker.start()
    worker.join()

    assert failures == []
    assert signal.getsignal(signal.SIGTERM) is before


def test_the_flag_is_visible_from_a_worker_thread() -> None:
    """Signals only reach the main thread; the flag is what crosses the boundary."""
    shutdown.request()
    seen: list[bool] = []

    worker = threading.Thread(target=lambda: seen.append(shutdown.is_requested()))
    worker.start()
    worker.join()

    assert seen == [True]
