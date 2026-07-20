"""Root Typer application and the entry-point-based tool loader.

This module must never import ``huggingface_hub``, ``boto3`` or any tool module at module
scope: ``aimm --help`` has to stay fast, and the Hub reads its environment at import time.
Tools are mounted lazily from ``run()``.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import entry_points
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from bg_ai_model_management import __version__
from bg_ai_model_management.errors import ConfigError
from bg_ai_model_management.logging_setup import configure_logging

TOOL_ENTRY_POINT_GROUP: str = "aimm.tools"

LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})
LOG_FORMATS: frozenset[str] = frozenset({"text", "json"})

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GlobalOptions:
    """Everything the root callback resolved, handed to sub-apps via ``ctx.obj``."""

    profile: Path | None
    log_level: str
    log_format: str
    json_output: bool
    run_id: str
    console: Console


def new_run_id() -> str:
    """Return ``'<YYYYMMDDTHHMMSSZ>-<6 hex>'``."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def load_tools(app: typer.Typer) -> list[str]:
    """Mount every entry point in group ``aimm.tools`` as a sub-app.

    Returns the mounted names, sorted. A tool that fails to import is logged as a warning
    and skipped: one broken third-party tool must not break ``aimm --help``. Never raises.
    """
    mounted: list[str] = []
    for ep in sorted(entry_points(group=TOOL_ENTRY_POINT_GROUP), key=lambda e: e.name):
        try:
            tool = ep.load()
            # Checked here, not by add_typer: mounting a non-Typer succeeds and only
            # explodes later while building the command tree, taking `aimm --help` down.
            if not isinstance(tool, typer.Typer):
                raise TypeError(f"expected a typer.Typer, got {type(tool).__name__}")
            app.add_typer(tool, name=ep.name)
        except Exception:
            log.warning("tool %r failed to load and was skipped", ep.name, exc_info=True)
        else:
            mounted.append(ep.name)
    return mounted


def _summary(group: typer.models.TyperInfo) -> str:
    instance = group.typer_instance
    help_text = instance.info.help if instance is not None else None
    return help_text.strip().splitlines()[0] if isinstance(help_text, str) and help_text else ""


def build_app() -> typer.Typer:
    """Create the root Typer app with the global callback and the ``tools`` command."""
    app = typer.Typer(
        name="aimm",
        help="BAUER GROUP AI model management toolkit.",
        add_completion=False,
        no_args_is_help=True,
        # The callback must run even without a subcommand, otherwise `--version` never
        # reaches it and click fails with "Missing command." instead.
        invoke_without_command=True,
        pretty_exceptions_enable=False,
    )

    @app.callback()
    def root(
        ctx: typer.Context,
        profile: Annotated[
            Path | None,
            typer.Option(
                "--profile",
                envvar="AIMM_PROFILE",
                exists=False,
                dir_okay=False,
                help="Path to the aimm profile YAML.",
            ),
        ] = None,
        log_level: Annotated[
            str,
            typer.Option(
                "--log-level", envvar="AIMM_LOG_LEVEL", help="DEBUG, INFO, WARNING or ERROR."
            ),
        ] = "INFO",
        log_format: Annotated[
            str,
            typer.Option("--log-format", envvar="AIMM_LOG_FORMAT", help="text or json."),
        ] = "text",
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit a machine-readable result document on stdout."),
        ] = False,
        run_id: Annotated[
            str | None,
            typer.Option(
                "--run-id",
                envvar="AIMM_RUN_ID",
                help="Correlation id for this run. Generated when omitted.",
            ),
        ] = None,
        no_color: Annotated[
            bool, typer.Option("--no-color", envvar="NO_COLOR", help="Disable coloured output.")
        ] = False,
        version: Annotated[
            bool, typer.Option("--version", "-V", help="Show version and exit.")
        ] = False,
    ) -> None:
        """Back up, verify and restore AI models and datasets."""
        if version:
            typer.echo(__version__)
            raise typer.Exit()
        if ctx.invoked_subcommand is None:
            # ctx.fail (not a raw click.UsageError): typer vendors click, so the real
            # click's exception classes are not the ones typer's parser catches.
            ctx.fail("Missing command.")

        level = log_level.upper()
        if level not in LOG_LEVELS:
            raise ConfigError(
                f"invalid --log-level {log_level!r}; expected one of {sorted(LOG_LEVELS)}"
            )
        if log_format not in LOG_FORMATS:
            raise ConfigError(f"invalid --log-format {log_format!r}; expected text or json")

        resolved_run_id = run_id or new_run_id()
        console = configure_logging(
            level=level,
            fmt=log_format,
            console=Console(stderr=True, no_color=True) if no_color else None,
            run_id=resolved_run_id,
        )
        ctx.obj = GlobalOptions(
            profile=profile,
            log_level=level,
            log_format=log_format,
            json_output=json_output,
            run_id=resolved_run_id,
            console=console,
        )

    @app.command("tools")
    def list_tools() -> None:
        """List mounted tools."""
        for group in sorted(app.registered_groups, key=lambda g: g.name or ""):
            typer.echo(f"{group.name}  {_summary(group)}".rstrip())

    return app
