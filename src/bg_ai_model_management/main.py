"""Console entry point and the exception-to-exit-code translation.

This is the ONLY module in the codebase that calls ``sys.exit()``. Library code raises
typed exceptions from :mod:`bg_ai_model_management.errors`; they are mapped to exit codes here and nowhere
else.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import MutableMapping, Sequence

import typer

from bg_ai_model_management import shutdown
from bg_ai_model_management.cli import build_app, load_tools
from bg_ai_model_management.errors import (
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_UNEXPECTED,
    AimmError,
    OperationCancelledError,
)
from bg_ai_model_management.logging_setup import configure_logging

HF_ENV_DEFAULTS: dict[str, str] = {
    "HF_HUB_DOWNLOAD_TIMEOUT": "60",
    "HF_HUB_ETAG_TIMEOUT": "30",
}

log = logging.getLogger(__name__)


def seed_hf_env(env: MutableMapping[str, str] | None = None) -> None:
    """Apply ``HF_ENV_DEFAULTS`` with setdefault semantics.

    ``huggingface_hub`` reads every environment variable at IMPORT time, so this must run
    before any ``huggingface_hub`` import. Called first thing in :func:`run`. Never
    overrides a value the operator already set.
    """
    target = os.environ if env is None else env
    for key, value in HF_ENV_DEFAULTS.items():
        target.setdefault(key, value)


def _exit_code(code: int | str | None) -> int:
    """Normalise a ``SystemExit.code`` into a process exit code."""
    if code is None:
        return EXIT_OK
    if isinstance(code, int):
        return code
    return EXIT_UNEXPECTED


def run(argv: Sequence[str] | None = None) -> int:
    """Execute the CLI and return a process exit code.

    Seeds the HF environment, builds the root app, mounts tools, invokes the app, and
    translates every exception into an exit code. Never raises ``AimmError`` or
    ``SystemExit`` to its caller.
    """
    seed_hf_env()
    configure_logging()  # bootstrap; the root callback reconfigures from the parsed flags
    # SIGTERM's default disposition would kill the interpreter without unwinding, so
    # in-flight multipart uploads would never be aborted. See the module docstring.
    shutdown.install_handlers()

    app = build_app()
    load_tools(app)
    try:
        app(args=None if argv is None else list(argv), prog_name="aimm")
    except SystemExit as exc:  # Typer/click exit in standalone mode, incl. usage errors
        return _exit_code(exc.code)
    except (KeyboardInterrupt, typer.Abort):
        log.error("interrupted")
        return EXIT_INTERRUPTED
    except OperationCancelledError as exc:
        # Same exit code as Ctrl-C, and deliberately not the AimmError branch below:
        # a signalled stop is an operator decision, not a failure of the work.
        log.error("interrupted: %s", exc)
        return exc.exit_code
    except typer.Exit as exc:
        # typer.Exit/Abort are the classes typer actually raises; on a typer build that
        # vendors click they are NOT click.exceptions.Exit/Abort, so catch these.
        return _exit_code(exc.exit_code)
    except AimmError as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        log.debug("traceback for %s", type(exc).__name__, exc_info=True)
        return exc.exit_code
    except Exception:
        log.error("unexpected internal error", exc_info=True)
        return EXIT_UNEXPECTED
    return EXIT_OK


def main() -> None:
    """Console entry point. The ONLY place in this codebase that calls ``sys.exit()``."""
    sys.exit(run())


if __name__ == "__main__":
    main()
