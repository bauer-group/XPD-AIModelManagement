"""Cooperative shutdown on SIGTERM/SIGINT, so terminated runs leave no orphans.

`docker stop`, systemd and Kubernetes all terminate with **SIGTERM**, whose default
disposition kills the interpreter outright: no unwinding, no ``finally``, and
therefore no ``abort_multipart_upload``. Every in-flight multipart upload is then
orphaned — it keeps occupying storage, is billed, and does not appear in an ordinary
bucket listing. SIGINT was already survivable because its default handler raises
``KeyboardInterrupt``, which the destination's abort-on-every-exception path catches;
SIGTERM had no such route.

A flag rather than an exception, because of where the work happens: signals are only
ever delivered to the **main thread**, while transfers run on a worker pool. Raising
in the main thread cannot reach a worker mid-upload, and ``ThreadPoolExecutor``
shutdown waits for running futures regardless. So the handler sets an event, workers
poll :func:`raise_if_requested` at part boundaries, and the existing abort path does
the rest.

A **second** signal restores the default handler and re-raises it, so an operator who
wants the process gone immediately is never trapped by the graceful path.

Handlers are installed by :mod:`bg_ai_model_management.main` only. Importing this
module changes nothing on its own.
"""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

from bg_ai_model_management.errors import OperationCancelledError

log = logging.getLogger(__name__)

#: Signals a long transfer should survive gracefully. SIGTERM is the one that matters
#: for containers; SIGINT is included so Ctrl-C takes the same orderly route.
DEFAULT_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGINT)

_requested = threading.Event()
#: Signal number -> the handler that was installed before we replaced it.
_previous: dict[int, signal.Handlers | object] = {}


def install_handlers(signals: tuple[signal.Signals, ...] = DEFAULT_SIGNALS) -> None:
    """Install the cooperative handlers. Idempotent, and main-thread only.

    Signal handlers can only be registered from the main thread; called from any
    other, or on a platform lacking one of the signals, the affected signal is left
    alone rather than raising — an inability to handle SIGTERM must never stop a
    backup from running.
    """
    if threading.current_thread() is not threading.main_thread():
        log.debug("not the main thread; leaving signal handlers untouched")
        return
    for signum in signals:
        try:
            replaced = signal.signal(signum, _handle)
        except (OSError, ValueError) as exc:  # unsupported on this platform
            log.debug("could not install a handler for %s: %s", signum.name, exc)
            continue
        # Only the FIRST install records what it replaced. A second call would
        # otherwise store our own handler as "the previous one", after which reset()
        # could never restore the real original.
        _previous.setdefault(int(signum), replaced)


def _handle(signum: int, _frame: FrameType | None) -> None:
    """Request shutdown; on a repeat signal, hand back to the default disposition."""
    name = signal.Signals(signum).name
    if _requested.is_set():
        log.warning("%s received again — terminating immediately", name)
        _restore(signum)
        signal.raise_signal(signum)
        return
    _requested.set()
    log.warning(
        "%s received — finishing the current parts and aborting in-flight uploads; "
        "send it again to terminate immediately",
        name,
    )


def _restore(signum: int) -> None:
    """Put back the handler that was in place before ``install_handlers``."""
    previous = _previous.pop(signum, signal.SIG_DFL)
    try:
        signal.signal(signal.Signals(signum), previous)  # type: ignore[arg-type]
    except (OSError, ValueError) as exc:  # pragma: no cover - platform edge
        log.debug("could not restore the handler for %d: %s", signum, exc)


def request() -> None:
    """Request shutdown programmatically. Idempotent."""
    _requested.set()


def is_requested() -> bool:
    """True once a shutdown signal has been received."""
    return _requested.is_set()


def raise_if_requested(context: str) -> None:
    """Abort the current operation if shutdown was requested.

    Call at points where stopping is cheap and safe — between multipart parts, before
    starting a file — never mid-part, where the abort would have to unwind a partially
    written body anyway.

    Args:
        context: What is being abandoned, for the message. Never a secret.

    Raises:
        OperationCancelledError: A shutdown signal has been received.
    """
    if _requested.is_set():
        raise OperationCancelledError(f"{context}: cancelled by a shutdown signal")


def reset() -> None:
    """Clear the request and restore every handler. For tests and embedders."""
    _requested.clear()
    for signum in list(_previous):
        _restore(signum)
