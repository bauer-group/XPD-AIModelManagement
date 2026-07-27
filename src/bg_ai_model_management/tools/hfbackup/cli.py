"""`aimm hf-backup` — back up Hugging Face repositories to S3-compatible storage.

This module is the presentation layer and nothing else. It parses arguments, resolves
settings, drives the engine and renders reports. It never calls ``sys.exit()`` and never
swallows a typed error: ``aimm.main.run`` owns the exception-to-exit-code mapping.

Two invariants are load-bearing and must survive every future edit:

* **Credentials are never CLI flags.** They come from the environment, a Docker secret or
  the profile file, so they never land in shell history or in ``ps`` output.
* **With ``--json``, stdout carries exactly one result document and nothing else.** Every
  log line, progress bar and human-readable table goes to stderr via the shared console.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import uuid4

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from bg_ai_model_management import __version__
from bg_ai_model_management.cli import GlobalOptions, new_run_id
from bg_ai_model_management.config.loader import find_profile, load_settings
from bg_ai_model_management.config.models import BackendPreset, Settings
from bg_ai_model_management.errors import (
    AimmError,
    ConfigError,
    DriftDetectedError,
    IntegrityError,
    RetentionRefusedError,
    TransferError,
)
from bg_ai_model_management.logging_setup import get_console, redact
from bg_ai_model_management.tools.hfbackup import catalog, keys
from bg_ai_model_management.tools.hfbackup.destination import S3Destination
from bg_ai_model_management.tools.hfbackup.engine import (
    Engine,
    RestoreReport,
    RestoreRequest,
    SyncReport,
    SyncRequest,
    VerifyReport,
    VerifyRequest,
)
from bg_ai_model_management.tools.hfbackup.manifest import Manifest
from bg_ai_model_management.tools.hfbackup.retention import (
    RetentionPlan,
    RetentionPolicy,
    RevisionInfo,
    plan_retention,
)
from bg_ai_model_management.tools.hfbackup.source import HubSource
from bg_ai_model_management.tools.hfbackup.source_modelscope import ModelScopeSource
from bg_ai_model_management.tools.hfbackup.types import (
    RecheckMode,
    RepoRef,
    RepoType,
    Source,
    SourceKind,
    TransferMode,
    VerifyLevel,
    VerifyStatus,
)

app = typer.Typer(
    name="hf-backup",
    help="Back up Hugging Face or ModelScope repos to S3-compatible storage.",
    no_args_is_help=True,
    add_completion=False,
)
catalog_app = typer.Typer(
    help="Inspect what is already stored in the object store.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(catalog_app, name="catalog")


# ── CLI-only enums ───────────────────────────────────────────────────────────
# The settings models use Literal[...]; Typer needs an Enum to validate and to render a
# choice list, so these mirror the literals one-to-one.


class Addressing(str, Enum):
    """S3 addressing style."""

    auto = "auto"
    path = "path"
    virtual = "virtual"


class Checksum(str, Enum):
    """botocore request/response checksum mode."""

    auto = "auto"
    when_supported = "when_supported"
    when_required = "when_required"


class Sse(str, Enum):
    """Server-side encryption algorithm."""

    aes256 = "AES256"
    aws_kms = "aws:kms"


# ── shared backend options ───────────────────────────────────────────────────
# Declared once as Annotated aliases and reused by every command, so the surface stays
# identical across sync/verify/restore/prune/catalog/doctor.

BackendOpt = Annotated[
    str | None,
    typer.Option(
        "--backend",
        "-B",
        envvar="AIMM_BACKEND",
        help="Backend name from the profile's `backends` mapping.",
    ),
]
EndpointOpt = Annotated[
    str | None,
    typer.Option(
        "--endpoint-url",
        envvar="AIMM_S3__ENDPOINT_URL",
        help="S3 endpoint URL. Setting it selects path-style addressing.",
    ),
]
BucketOpt = Annotated[
    str | None,
    typer.Option(
        "--bucket",
        "-b",
        envvar="AIMM_S3__BUCKET",
        help="Target bucket. Required unless the profile supplies one.",
    ),
]
PrefixOpt = Annotated[
    str,
    typer.Option("--prefix", envvar="AIMM_S3__PREFIX", help="Key prefix inside the bucket."),
]
RegionOpt = Annotated[
    str,
    typer.Option("--region", envvar="AIMM_S3__REGION", help="Region label sent to the endpoint."),
]
PresetOpt = Annotated[
    BackendPreset,
    typer.Option(
        "--preset",
        envvar="AIMM_S3__PRESET",
        case_sensitive=False,
        help="Backend preset supplying addressing and checksum defaults.",
    ),
]
AddressingOpt = Annotated[
    Addressing,
    typer.Option(
        "--addressing",
        envvar="AIMM_S3__ADDRESSING_STYLE",
        case_sensitive=False,
        help="Addressing style. 'auto' means path-style when an endpoint is set.",
    ),
]
ChecksumOpt = Annotated[
    Checksum,
    typer.Option(
        "--checksum",
        envvar="AIMM_S3__CHECKSUM_CALCULATION",
        case_sensitive=False,
        help="Checksum calculation mode. 'auto' resolves from the preset.",
    ),
]
StorageClassOpt = Annotated[
    str | None,
    typer.Option(
        "--storage-class",
        envvar="AIMM_S3__STORAGE_CLASS",
        help="Storage class. Omitted by default; MinIO accepts only STANDARD.",
    ),
]
SseOpt = Annotated[
    Sse | None,
    typer.Option(
        "--sse",
        envvar="AIMM_S3__SERVER_SIDE_ENCRYPTION",
        case_sensitive=False,
        help="Server-side encryption algorithm.",
    ),
]
SseKeyOpt = Annotated[
    str | None,
    typer.Option(
        "--sse-kms-key-id",
        envvar="AIMM_S3__SSE_KMS_KEY_ID",
        help="KMS key id used when --sse aws:kms is selected.",
    ),
]
NoVerifyTlsOpt = Annotated[
    bool,
    typer.Option(
        "--no-verify-tls",
        help="Do not verify the endpoint's TLS certificate. "
        "The equivalent setting is AIMM_S3__VERIFY_TLS.",
    ),
]
EnsureBucketOpt = Annotated[
    bool,
    typer.Option(
        "--ensure-bucket",
        envvar="AIMM_S3__ENSURE_BUCKET",
        help="Create the bucket when it is missing. Off by default on purpose.",
    ),
]
NoProbeOpt = Annotated[
    bool,
    typer.Option(
        "--no-probe",
        help="Skip the backend capability probe. The equivalent setting is AIMM_S3__PROBE.",
    ),
]

# ── shared selection and transfer options ────────────────────────────────────

RepoTypeOpt = Annotated[
    RepoType,
    typer.Option(
        "--repo-type",
        envvar="AIMM_REPO_TYPE",
        case_sensitive=False,
        help="Repository type for specs that do not carry a prefix.",
    ),
]
AnyRepoTypeOpt = Annotated[
    RepoType | None,
    typer.Option(
        "--repo-type",
        envvar="AIMM_REPO_TYPE",
        case_sensitive=False,
        help="Restrict to one repository type. Both types are listed by default.",
    ),
]
SourceOpt = Annotated[
    SourceKind,
    typer.Option(
        "--source",
        envvar="AIMM_SOURCE",
        case_sensitive=False,
        help="Upstream hub to mirror from. ModelScope serves models only, and its "
        "default branch is 'master' rather than 'main'.",
    ),
]


RevisionOpt = Annotated[
    str,
    typer.Option("--revision", help="Branch, tag or 40-hex commit SHA."),
]
IncludeOpt = Annotated[
    list[str] | None,
    typer.Option("--include", help="Glob of repo-relative paths to include. Repeatable."),
]
ExcludeOpt = Annotated[
    list[str] | None,
    typer.Option("--exclude", help="Glob of repo-relative paths to exclude. Repeatable; wins."),
]
WorkersOpt = Annotated[
    int,
    typer.Option(
        "--workers",
        min=1,
        max=64,
        envvar="AIMM_TRANSFER__WORKERS",
        help="Number of concurrent worker threads.",
    ),
]

HEX40 = re.compile(r"^[0-9a-f]{40}$")
DURATION = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
DURATION_SECONDS: dict[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
REPO_TYPE_PREFIXES: dict[str, RepoType] = {
    "models/": RepoType.models,
    "datasets/": RepoType.datasets,
}

#: Option name -> dotted settings path. An option missing from this map never reaches the
#: settings layer; an option present here only reaches it when the user actually typed it.
OVERRIDE_KEYS: dict[str, str] = {
    "endpoint_url": "s3.endpoint_url",
    "bucket": "s3.bucket",
    "prefix": "s3.prefix",
    "region": "s3.region",
    "preset": "s3.preset",
    "addressing": "s3.addressing_style",
    "checksum": "s3.checksum_calculation",
    "storage_class": "s3.storage_class",
    "sse": "s3.server_side_encryption",
    "sse_kms_key_id": "s3.sse_kms_key_id",
    "no_verify_tls": "s3.verify_tls",
    "ensure_bucket": "s3.ensure_bucket",
    "no_probe": "s3.probe",
    "mode": "transfer.mode",
    "workers": "transfer.workers",
    "part_size": "transfer.part_size",
    "inline_max": "transfer.inline_max",
    "max_part_memory": "transfer.max_part_memory",
    "staging_dir": "transfer.staging_dir",
    "disk_reserve": "transfer.disk_reserve",
    "max_disk": "transfer.max_disk_bytes",
    "prefer_xet": "transfer.prefer_xet",
    "fail_fast": "transfer.fail_fast",
}

#: Options whose flag expresses the negation of the setting it drives.
NEGATED_OPTIONS: frozenset[str] = frozenset({"no_verify_tls", "no_probe"})


# ── plumbing ─────────────────────────────────────────────────────────────────
def globals_of(ctx: typer.Context) -> GlobalOptions:
    """Return the root callback's GlobalOptions, or a standalone default.

    The sub-app is also invocable on its own (tests, `python -m`), in which case the root
    callback never ran and `ctx.obj` is None.
    """
    obj = ctx.obj
    if isinstance(obj, GlobalOptions):
        return obj
    return GlobalOptions(
        profile=None,
        log_level="INFO",
        log_format="text",
        json_output=False,
        run_id=new_run_id(),
        console=get_console(),
    )


def collect_overrides(ctx: typer.Context) -> dict[str, Any]:
    """Return dotted settings overrides for the options the user actually provided.

    Click records where every parameter's value came from, so an option left at its
    default is dropped here. Passing a flag's default down to `load_settings` would make
    an untouched flag silently beat the profile file.
    """
    overrides: dict[str, Any] = {}
    for name, dotted in OVERRIDE_KEYS.items():
        if name not in ctx.params:
            continue
        source = ctx.get_parameter_source(name)
        # Compared by NAME, deliberately. typer vendors its own copy of click, so at
        # runtime this is a `typer._click.core.ParameterSource` member while a bare
        # `from click.core import ParameterSource` imports the REAL click's enum. The two
        # are distinct objects, so an identity check silently never fires and every
        # untouched flag's default would be passed down as an explicit override — which
        # makes an unset flag beat the profile file. `typer._click` is private, so
        # importing the vendored enum to compare by identity is not an option either.
        if source is None or source.name == "DEFAULT":
            continue
        value = ctx.params[name]
        if value is None:
            continue
        if isinstance(value, Enum):
            value = value.value
        if name in NEGATED_OPTIONS:
            value = not value
        overrides[dotted] = value
    return overrides


def open_settings(ctx: typer.Context, backend: str | None) -> tuple[GlobalOptions, Settings]:
    """Resolve global options and settings for one command invocation."""
    opts = globals_of(ctx)
    settings = load_settings(
        profile=opts.profile, backend=backend, overrides=collect_overrides(ctx)
    )
    return opts, settings


@contextmanager
def open_destination(settings: Settings) -> Iterator[S3Destination]:
    """Build the shared S3 client on this thread and close it afterwards — always.

    `workers` must be passed: the connection pool is sized from it, and leaving it at the
    default made `--workers 64` run 64 concurrent uploads against a 32-connection pool,
    where every request past the 32nd pays a fresh TLS handshake for the whole run.
    """
    destination = S3Destination.create(
        settings.s3,
        workers=settings.transfer.workers,
        attempts=settings.transfer.max_attempts,
        max_wait=settings.transfer.max_wait,
    )
    try:
        yield destination
    finally:
        destination.close()


@contextmanager
def open_source(settings: Settings, kind: SourceKind = SourceKind.huggingface) -> Iterator[Source]:
    """Open the upstream hub, releasing whatever it holds on exit.

    Mirrors `open_destination`: the ModelScope source owns an HTTP connection pool, and
    a long-lived process (a scheduler embedding this) must not leak one per run.
    """
    if kind is SourceKind.modelscope:
        modelscope = ModelScopeSource(settings.modelscope)
        try:
            yield modelscope
        finally:
            modelscope.close()
        return
    yield HubSource(settings.hub)


def build_engine(
    opts: GlobalOptions, settings: Settings, destination: S3Destination, source: Source
) -> Engine:
    """Assemble the engine with the console the log handler already owns."""
    return Engine(
        source,
        destination,
        settings,
        console=opts.console,
        run_id=opts.run_id,
        tool_version=__version__,
    )


def new_progress(console: Console) -> Progress:
    """Return a progress display bound to the shared console.

    `rich.Progress` guards its state with an RLock, so worker threads may call `advance`
    concurrently. On a non-TTY or in CI it is disabled outright: a degraded Progress emits
    one plain line per refresh, which for thousands of files means thousands of lines.
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
        disable=not console.is_terminal or bool(os.environ.get("CI")),
    )


@contextmanager
def file_progress(console: Console, engine: Engine, description: str) -> Iterator[None]:
    """Wire `engine.progress_hook` to a per-file progress bar for the duration.

    Without this the engine's hook stays `None` and a multi-hour, multi-terabyte run
    prints nothing at all between the first log line and the final table, so an operator
    cannot tell a working run from a hung one. The console is the shared stderr console,
    so `--json` still emits exactly one document on stdout.

    The total is deliberately unknown: the file list is only enumerated inside the
    engine, so this renders an indeterminate bar with a live count rather than lying
    about the denominator.
    """
    with new_progress(console) as progress:
        task = progress.add_task(description, total=None)

        def advance(event: str, path: str, size: int) -> None:
            # Exactly one advance per file: sync emits either `skip` alone or
            # `start` + `done`, while verify and restore emit only `done`.
            if event in ("done", "skip"):
                progress.advance(task)

        engine.progress_hook = advance
        try:
            yield
        finally:
            engine.progress_hook = None


def json_default(value: Any) -> Any:
    """Serialise the value types the reports are built from."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, timedelta):
        return int(value.total_seconds())
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__} as JSON")


def emit(opts: GlobalOptions, document: Mapping[str, Any], render: Callable[[], None]) -> None:
    """Emit the result: one JSON document on stdout, or human output on the console.

    Under --json stdout receives exactly one document and nothing else, which is what
    makes every command scriptable. Logs and progress stay on stderr either way.
    """
    if opts.json_output:
        sys.stdout.write(json.dumps(document, indent=2, sort_keys=True, default=json_default))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return
    render()


def now_utc() -> datetime:
    """Current UTC time; a single seam so reports and retention agree."""
    return datetime.now(UTC)


def timestamp(moment: datetime) -> str:
    """RFC 3339 UTC with a trailing Z."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def human_bytes(count: int) -> str:
    """Format a byte count for display."""
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PiB"


# ── argument parsing ─────────────────────────────────────────────────────────
def parse_repo_spec(spec: str, *, default_type: RepoType, default_revision: str) -> RepoRef:
    """Parse `owner/name`, `datasets/owner/name` or `owner/name@revision`.

    Raises:
        ConfigError: the specification is empty or malformed.
    """
    text = spec.strip()
    if not text:
        raise ConfigError("empty repository specification")
    repo_type = default_type
    for prefix, value in REPO_TYPE_PREFIXES.items():
        if text.startswith(prefix):
            repo_type, text = value, text[len(prefix) :]
            break
    repo_id, _, revision = text.partition("@")
    if not repo_id or repo_id.startswith("/") or repo_id.endswith("/") or "//" in repo_id:
        raise ConfigError(
            f"invalid repository specification {spec!r}; "
            "expected owner/name, datasets/owner/name or owner/name@revision"
        )
    return RepoRef(repo_id=repo_id, repo_type=repo_type, revision=revision or default_revision)


def read_specs(path: Path | None) -> list[str]:
    """Read one repository specification per line; '#' starts a comment.

    Raises:
        ConfigError: the file cannot be read.
    """
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read --from-file {path}: {exc}") from exc
    specs = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            specs.append(stripped)
    return specs


def parse_duration(text: str) -> timedelta:
    """Parse a retention duration such as '30m', '12h', '90d' or '2w'.

    Raises:
        ConfigError: the value is not a recognised duration.
    """
    match = DURATION.match(text.strip())
    if match is None:
        raise ConfigError(f"invalid duration {text!r}; expected a form like 30m, 12h, 90d or 2w")
    return timedelta(seconds=int(match.group(1)) * DURATION_SECONDS[match.group(2).lower()])


def patterns(values: Sequence[str] | None, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Normalise a repeatable glob option."""
    return tuple(values) if values else default


def resolve_commit(
    destination: S3Destination, prefix: str, repo_type: RepoType, repo_id: str, revision: str
) -> str:
    """Resolve a revision to a commit SHA using only the object store.

    A 40-hex revision is used as-is; anything else is looked up in `refs/<ref>.json`.
    Deliberately never contacts Hugging Face: browsing and pruning a backup must work
    without Hub credentials and without network access to the Hub.

    Raises:
        ConfigError: the ref has never been backed up.
    """
    candidate = revision.strip().lower()
    if HEX40.match(candidate):
        return candidate
    refs = catalog.read_refs(destination, prefix, repo_type, repo_id)
    sha = refs.get(revision)
    if sha is None:
        known = ", ".join(sorted(refs)) or "none"
        raise ConfigError(
            f"revision {revision!r} is not backed up for {repo_id} (known refs: {known})"
        )
    return sha


def masked_settings(settings: Settings) -> dict[str, Any]:
    """Serialise settings for display with every secret masked.

    `SecretStr` already renders as '**********' in JSON mode; `redact` additionally masks
    credentials embedded in free-form strings such as an endpoint URL. This is the output
    people paste into bug reports, so both layers matter.
    """

    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return cast("dict[str, Any]", scrub(settings.model_dump(mode="json")))


# ── sync ─────────────────────────────────────────────────────────────────────
@app.command()
def sync(
    ctx: typer.Context,
    source: SourceOpt = SourceKind.huggingface,
    repos: Annotated[
        list[str] | None,
        typer.Argument(help="owner/name | datasets/owner/name | owner/name@revision"),
    ] = None,
    repo_type: RepoTypeOpt = RepoType.models,
    revision: RevisionOpt = "main",
    from_file: Annotated[
        Path | None,
        typer.Option(
            "--from-file",
            exists=True,
            dir_okay=False,
            help="File with one repository specification per line.",
        ),
    ] = None,
    include: IncludeOpt = None,
    exclude: ExcludeOpt = None,
    mode: Annotated[
        TransferMode,
        typer.Option(
            "--mode",
            envvar="AIMM_TRANSFER__MODE",
            case_sensitive=False,
            help="Transfer strategy: auto picks streaming or staging per file.",
        ),
    ] = TransferMode.auto,
    workers: WorkersOpt = 8,
    part_size: Annotated[
        str,
        typer.Option(
            "--part-size",
            envvar="AIMM_TRANSFER__PART_SIZE",
            help="Multipart part size, e.g. 8MiB. Minimum 5MiB, maximum 5GiB.",
        ),
    ] = "8MiB",
    inline_max: Annotated[
        str,
        typer.Option("--inline-max", help="Files at or below this size go in a single PUT."),
    ] = "8MiB",
    max_part_memory: Annotated[
        str,
        typer.Option("--max-part-memory", help="Upper bound on a single buffered part."),
    ] = "64MiB",
    staging_dir: Annotated[
        Path | None,
        typer.Option(
            "--staging-dir",
            file_okay=False,
            help="Directory for the disk path. Defaults to the system temp dir.",
        ),
    ] = None,
    disk_reserve: Annotated[
        str,
        typer.Option("--disk-reserve", help="Free space never consumed by staging."),
    ] = "5GiB",
    max_disk: Annotated[
        str | None,
        typer.Option("--max-disk", help="Cap on staging bytes. Derived from free space."),
    ] = None,
    prefer_xet: Annotated[
        bool,
        typer.Option(
            "--prefer-xet/--no-prefer-xet",
            help="Prefer the xet-accelerated disk path for files that carry a xet hash.",
        ),
    ] = False,
    abort_stale: Annotated[
        str,
        typer.Option(
            "--abort-stale",
            help="Abort abandoned multipart uploads of each repository older than this "
            "before transferring it. 'off' disables the sweep.",
        ),
    ] = "24h",
    recheck: Annotated[
        RecheckMode,
        typer.Option(
            "--recheck",
            case_sensitive=False,
            help="How hard to re-check an already-stored file before skipping.",
        ),
    ] = RecheckMode.head,
    update_ref: Annotated[
        bool,
        typer.Option(
            "--update-ref/--no-update-ref", help="Point refs/<revision>.json at the synced commit."
        ),
    ] = True,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Plan only; transfer no bytes and write nothing."),
    ] = False,
    fail_fast: Annotated[
        bool,
        typer.Option("--fail-fast", help="Stop at the first file error."),
    ] = False,
    backend: BackendOpt = None,
    endpoint_url: EndpointOpt = None,
    bucket: BucketOpt = None,
    prefix: PrefixOpt = "aimm",
    region: RegionOpt = "us-east-1",
    preset: PresetOpt = BackendPreset.auto,
    addressing: AddressingOpt = Addressing.auto,
    checksum: ChecksumOpt = Checksum.auto,
    storage_class: StorageClassOpt = None,
    sse: SseOpt = None,
    sse_kms_key_id: SseKeyOpt = None,
    no_verify_tls: NoVerifyTlsOpt = False,
    ensure_bucket: EnsureBucketOpt = False,
    no_probe: NoProbeOpt = False,
) -> None:
    """Back up one or more Hugging Face repositories to the object store."""
    opts, settings = open_settings(ctx, backend)
    specs = list(repos or []) + read_specs(from_file)
    if not specs:
        raise ConfigError("no repositories given; pass REPOS or --from-file")
    request = SyncRequest(
        repos=tuple(
            parse_repo_spec(spec, default_type=repo_type, default_revision=revision)
            for spec in specs
        ),
        include=patterns(include, default=("*",)),
        exclude=patterns(exclude),
        recheck=recheck,
        update_ref=update_ref,
        dry_run=dry_run,
        abort_stale_after=(
            None if abort_stale.strip().lower() in {"off", "no", "none"}
            else parse_duration(abort_stale)
        ),
    )
    with open_destination(settings) as destination, open_source(settings, source) as upstream:
        engine = build_engine(opts, settings, destination, upstream)
        with file_progress(opts.console, engine, "syncing files"):
            report = engine.sync(request)

    document = {
        "command": "sync",
        "run_id": report.run_id,
        "dry_run": dry_run,
        "ok": report.ok,
        "repos": [dataclasses.asdict(repo) for repo in report.repos],
    }
    emit(opts, document, lambda: render_sync(opts.console, report, dry_run=dry_run))
    if not report.ok:
        failed = ", ".join(repo.repo_id for repo in report.repos if repo.errors)
        raise TransferError(f"sync completed with errors in: {failed or 'unknown repositories'}")


def render_sync(console: Console, report: SyncReport, *, dry_run: bool) -> None:
    """Render a sync report as a table."""
    table = Table(title=f"sync {report.run_id}{' (dry run)' if dry_run else ''}")
    table.add_column("repository")
    table.add_column("commit", no_wrap=True)
    table.add_column("files", justify="right")
    table.add_column("sent", justify="right")
    table.add_column("skipped", justify="right")
    table.add_column("bytes", justify="right")
    table.add_column("paths")
    table.add_column("errors", justify="right")
    for repo in report.repos:
        paths = ", ".join(f"{name}={count}" for name, count in sorted(repo.by_path.items()))
        table.add_row(
            f"{repo.repo_type.value}/{repo.repo_id}",
            repo.commit_sha[:12],
            str(repo.files_total),
            str(repo.files_transferred),
            str(repo.files_skipped),
            human_bytes(repo.bytes_transferred),
            paths or "-",
            str(len(repo.errors)),
        )
    console.print(table)
    for repo in report.repos:
        for message in repo.errors:
            console.print(f"[red]{repo.repo_id}[/red]: {message}")


# ── verify ───────────────────────────────────────────────────────────────────
@app.command()
def verify(
    ctx: typer.Context,
    repo: Annotated[str, typer.Argument(help="owner/name | datasets/owner/name")],
    source: SourceOpt = SourceKind.huggingface,
    repo_type: RepoTypeOpt = RepoType.models,
    revision: RevisionOpt = "main",
    level: Annotated[
        VerifyLevel,
        typer.Option(
            "--level",
            case_sensitive=False,
            help="quick: manifest vs HEAD. deep: re-hash stored bytes. "
            "upstream: also compare against the Hub.",
        ),
    ] = VerifyLevel.quick,
    sample_percent: Annotated[
        float,
        typer.Option(
            "--sample-percent",
            min=0.0,
            max=100.0,
            help="Percentage of files to check. Must be greater than zero.",
        ),
    ] = 100.0,
    workers: WorkersOpt = 8,
    strict: Annotated[
        bool,
        typer.Option("--strict/--no-strict", help="Exit 20 when drift is found."),
    ] = True,
    backend: BackendOpt = None,
    endpoint_url: EndpointOpt = None,
    bucket: BucketOpt = None,
    prefix: PrefixOpt = "aimm",
    region: RegionOpt = "us-east-1",
    preset: PresetOpt = BackendPreset.auto,
    addressing: AddressingOpt = Addressing.auto,
    checksum: ChecksumOpt = Checksum.auto,
    storage_class: StorageClassOpt = None,
    sse: SseOpt = None,
    sse_kms_key_id: SseKeyOpt = None,
    no_verify_tls: NoVerifyTlsOpt = False,
    ensure_bucket: EnsureBucketOpt = False,
    no_probe: NoProbeOpt = False,
) -> None:
    """Check a stored revision against its manifest.

    Exit codes are a scheduling contract: 0 clean, 20 drift or incomplete, 6 corrupt.
    """
    if sample_percent <= 0.0:
        raise ConfigError("--sample-percent must be greater than 0")
    opts, settings = open_settings(ctx, backend)
    request = VerifyRequest(
        repo=parse_repo_spec(repo, default_type=repo_type, default_revision=revision),
        level=level,
        sample_percent=sample_percent,
        strict=strict,
    )
    with open_destination(settings) as destination, open_source(settings, source) as upstream:
        engine = build_engine(opts, settings, destination, upstream)
        with file_progress(opts.console, engine, "verifying files"):
            report = engine.verify(request)

    document = {
        "command": "verify",
        "run_id": opts.run_id,
        "repo_id": report.repo_id,
        "commit_sha": report.commit_sha,
        "status": report.status.value,
        "level": level.value,
        "checked": report.checked,
        "findings": [dataclasses.asdict(finding) for finding in report.findings],
    }
    emit(opts, document, lambda: render_verify(opts.console, report))

    if report.status is VerifyStatus.corrupt:
        raise IntegrityError(
            f"{report.repo_id}@{report.commit_sha[:12]} is corrupt: "
            f"{len(report.findings)} finding(s)"
        )
    if strict and report.status in (VerifyStatus.drift, VerifyStatus.incomplete):
        raise DriftDetectedError(
            f"{report.repo_id}@{report.commit_sha[:12]} is {report.status.value}: "
            f"{len(report.findings)} finding(s)"
        )


def render_verify(console: Console, report: VerifyReport) -> None:
    """Render a verify report as a table."""
    colour = {"ok": "green", "incomplete": "yellow", "drift": "yellow", "corrupt": "red"}
    console.print(
        f"{report.repo_id}@{report.commit_sha[:12]} "
        f"[{colour[report.status.value]}]{report.status.value.upper()}[/] "
        f"({report.checked} file(s) checked)"
    )
    if not report.findings:
        return
    table = Table(title="findings")
    table.add_column("path")
    table.add_column("kind")
    table.add_column("expected")
    table.add_column("actual")
    for finding in report.findings:
        table.add_row(finding.path, finding.kind, finding.expected, finding.actual)
    console.print(table)


# ── restore ──────────────────────────────────────────────────────────────────
@app.command()
def restore(
    ctx: typer.Context,
    repo: Annotated[str, typer.Argument(help="owner/name | datasets/owner/name")],
    dest_dir: Annotated[
        Path,
        typer.Option(
            "--dest",
            file_okay=False,
            writable=True,
            help="Target directory. Files are written atomically inside it.",
        ),
    ],
    repo_type: RepoTypeOpt = RepoType.models,
    revision: RevisionOpt = "main",
    include: IncludeOpt = None,
    exclude: ExcludeOpt = None,
    workers: WorkersOpt = 8,
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite/--no-overwrite", help="Replace existing files instead of failing."
        ),
    ] = False,
    verify_only: Annotated[
        bool,
        typer.Option("--verify-only", help="Check the stored bytes without writing."),
    ] = False,
    backend: BackendOpt = None,
    endpoint_url: EndpointOpt = None,
    bucket: BucketOpt = None,
    prefix: PrefixOpt = "aimm",
    region: RegionOpt = "us-east-1",
    preset: PresetOpt = BackendPreset.auto,
    addressing: AddressingOpt = Addressing.auto,
    checksum: ChecksumOpt = Checksum.auto,
    storage_class: StorageClassOpt = None,
    sse: SseOpt = None,
    sse_kms_key_id: SseKeyOpt = None,
    no_verify_tls: NoVerifyTlsOpt = False,
    ensure_bucket: EnsureBucketOpt = False,
    no_probe: NoProbeOpt = False,
) -> None:
    """Restore a stored revision to a local directory. Never contacts Hugging Face."""
    opts, settings = open_settings(ctx, backend)
    request = RestoreRequest(
        repo=parse_repo_spec(repo, default_type=repo_type, default_revision=revision),
        dest=dest_dir,
        include=patterns(include, default=("*",)),
        exclude=patterns(exclude),
        overwrite=overwrite,
        verify_only=verify_only,
    )
    # restore reads only from S3, so the upstream is never contacted; the default
    # source keeps `build_engine` honest without adding a flag that does nothing.
    with open_destination(settings) as destination, open_source(settings) as upstream:
        engine = build_engine(opts, settings, destination, upstream)
        with file_progress(opts.console, engine, "restoring files"):
            report = engine.restore(request)

    document = {
        "command": "restore",
        "run_id": opts.run_id,
        "dest": str(dest_dir),
        "verify_only": verify_only,
        **dataclasses.asdict(report),
    }
    emit(opts, document, lambda: render_restore(opts.console, report, dest_dir))


def render_restore(console: Console, report: RestoreReport, dest_dir: Path) -> None:
    """Render a restore report as a single line."""
    console.print(
        f"{report.repo_id}@{report.commit_sha[:12]} -> {dest_dir}: "
        f"{report.files} file(s), {human_bytes(report.bytes)}, "
        f"{report.skipped} skipped, {report.duration_seconds:.1f}s"
    )


# ── prune ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RepoScan:
    """One repository's stored revisions and the refs protecting them."""

    repo_type: RepoType
    repo_id: str
    revisions: tuple[RevisionInfo, ...]
    refs: dict[str, str]


def scan_repo(
    destination: S3Destination, prefix: str, repo_type: RepoType, repo_id: str
) -> RepoScan:
    """Read one repository's revisions and refs. Safe to call from a worker thread."""
    return RepoScan(
        repo_type=repo_type,
        repo_id=repo_id,
        revisions=tuple(catalog.list_revisions(destination, prefix, repo_type, repo_id)),
        refs=catalog.read_refs(destination, prefix, repo_type, repo_id),
    )


def scan_all(
    destination: S3Destination,
    prefix: str,
    targets: Sequence[tuple[RepoType, str]],
    console: Console,
) -> list[RepoScan]:
    """Scan every target repository in parallel, showing progress on the shared console."""
    scans: list[RepoScan] = []
    with new_progress(console) as progress:
        task = progress.add_task("scanning revisions", total=len(targets))
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(targets)))) as pool:
            futures = [
                pool.submit(scan_repo, destination, prefix, repo_type, repo_id)
                for repo_type, repo_id in targets
            ]
            for future in as_completed(futures):
                scans.append(future.result())
                progress.advance(task)
    return sorted(scans, key=lambda scan: (scan.repo_type.value, scan.repo_id))


@app.command()
def prune(
    ctx: typer.Context,
    repos: Annotated[
        list[str] | None,
        typer.Argument(help="owner/name | datasets/owner/name"),
    ] = None,
    repo_type: RepoTypeOpt = RepoType.models,
    all_repos: Annotated[
        bool,
        typer.Option("--all-repos", help="Operate on every repository under the prefix."),
    ] = False,
    keep_last: Annotated[
        int | None,
        typer.Option("--keep-last", min=1, help="Keep the N newest complete revisions."),
    ] = None,
    keep_within: Annotated[
        str | None,
        typer.Option("--keep-within", help="Keep everything newer than e.g. 30m, 12h, 90d, 2w."),
    ] = None,
    keep_incomplete: Annotated[
        bool,
        typer.Option(
            "--keep-incomplete/--no-keep-incomplete", help="Keep revisions that have no manifest."
        ),
    ] = False,
    abort_older_than: Annotated[
        str,
        typer.Option("--abort-older-than", help="Abort multipart uploads started before this age."),
    ] = "24h",
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Actually delete. Without it nothing is changed."),
    ] = False,
    backend: BackendOpt = None,
    endpoint_url: EndpointOpt = None,
    bucket: BucketOpt = None,
    prefix: PrefixOpt = "aimm",
    region: RegionOpt = "us-east-1",
    preset: PresetOpt = BackendPreset.auto,
    addressing: AddressingOpt = Addressing.auto,
    checksum: ChecksumOpt = Checksum.auto,
    storage_class: StorageClassOpt = None,
    sse: SseOpt = None,
    sse_kms_key_id: SseKeyOpt = None,
    no_verify_tls: NoVerifyTlsOpt = False,
    ensure_bucket: EnsureBucketOpt = False,
    no_probe: NoProbeOpt = False,
) -> None:
    """Delete revisions the retention policy no longer covers.

    Without --yes this prints the plan and changes nothing at all: no objects are deleted
    and no multipart upload is aborted.
    """
    if keep_last is None and keep_within is None:
        raise RetentionRefusedError(
            "prune requires --keep-last and/or --keep-within; "
            "an unconstrained prune is always a mistake"
        )
    if bool(repos) == all_repos:
        raise ConfigError("pass either repository arguments or --all-repos, not both or neither")

    opts, settings = open_settings(ctx, backend)
    within = parse_duration(keep_within) if keep_within is not None else None
    stale_after = parse_duration(abort_older_than)
    key_prefix = settings.s3.prefix
    moment = now_utc()

    with open_destination(settings) as destination:
        if all_repos:
            targets = [
                (entry.repo_type, entry.repo_id)
                for entry in catalog.list_repos(destination, key_prefix)
            ]
        else:
            targets = [
                (ref.repo_type, ref.repo_id)
                for ref in (
                    parse_repo_spec(spec, default_type=repo_type, default_revision="main")
                    for spec in repos or []
                )
            ]
        scans = scan_all(destination, key_prefix, targets, opts.console)

        results: list[dict[str, Any]] = []
        for scan in scans:
            policy = RetentionPolicy(
                keep_last=keep_last,
                keep_within=within,
                keep_incomplete=keep_incomplete,
                protected=frozenset(scan.refs.values()),
                # A revision with no manifest that is still being written looks
                # identical to the debris of a crashed run. `--abort-older-than` is
                # already the operator's stated "no upload of mine is older than this",
                # so it is reused as the grace period rather than inventing a knob.
                incomplete_grace=stale_after,
            )
            plan = plan_retention(scan.revisions, policy, now=moment)
            deleted_objects = 0
            aborted_uploads = 0
            if yes:
                deleted_objects = delete_revisions(destination, key_prefix, scan, plan)
                aborted_uploads = destination.abort_stale_uploads(
                    keys.repo_root(key_prefix, scan.repo_type, scan.repo_id) + "/",
                    stale_after,
                    now=moment,
                )
            results.append(
                {
                    "repo_type": scan.repo_type.value,
                    "repo_id": scan.repo_id,
                    "keep": [dataclasses.asdict(rev) for rev in plan.keep],
                    "protected": [dataclasses.asdict(rev) for rev in plan.protected],
                    "delete": [dataclasses.asdict(rev) for rev in plan.delete],
                    "deleted_objects": deleted_objects,
                    "aborted_uploads": aborted_uploads,
                }
            )

    document = {
        "command": "prune",
        "run_id": opts.run_id,
        "applied": yes,
        "now": timestamp(moment),
        "repos": results,
        "totals": {
            "revisions_deleted": sum(len(row["delete"]) for row in results),
            "bytes_deleted": sum(rev["total_bytes"] for row in results for rev in row["delete"]),
            "objects_deleted": sum(int(row["deleted_objects"]) for row in results),
            "uploads_aborted": sum(int(row["aborted_uploads"]) for row in results),
        },
    }
    emit(opts, document, lambda: render_prune(opts.console, results, applied=yes))


def delete_revisions(
    destination: S3Destination, prefix: str, scan: RepoScan, plan: RetentionPlan
) -> int:
    """Delete every object belonging to the planned revisions. Returns the object count."""
    deleted = 0
    for revision in plan.delete:
        root = keys.revision_root(prefix, scan.repo_type, scan.repo_id, revision.commit_sha) + "/"
        deleted += destination.delete_keys(summary.key for summary in destination.list_keys(root))
    return deleted


def render_prune(console: Console, results: Sequence[Mapping[str, Any]], *, applied: bool) -> None:
    """Render the retention plan as a table."""
    table = Table(title="prune plan" if not applied else "pruned")
    table.add_column("repository")
    table.add_column("keep", justify="right")
    table.add_column("protected", justify="right")
    table.add_column("delete", justify="right")
    table.add_column("bytes freed", justify="right")
    table.add_column("objects", justify="right")
    table.add_column("uploads aborted", justify="right")
    for row in results:
        table.add_row(
            f"{row['repo_type']}/{row['repo_id']}",
            str(len(row["keep"])),
            str(len(row["protected"])),
            str(len(row["delete"])),
            human_bytes(sum(rev["total_bytes"] for rev in row["delete"])),
            str(row["deleted_objects"]),
            str(row["aborted_uploads"]),
        )
    console.print(table)
    for row in results:
        for revision in row["delete"]:
            console.print(f"  delete {row['repo_id']}@{revision['commit_sha'][:12]}")
    if not applied:
        console.print("[yellow]dry run: nothing was deleted. Pass --yes to apply.[/yellow]")


# ── catalog ──────────────────────────────────────────────────────────────────
@catalog_app.command("list")
def catalog_list(
    ctx: typer.Context,
    repo_type: AnyRepoTypeOpt = None,
    owner: Annotated[
        str | None,
        typer.Option("--owner", help="Restrict to one repository owner."),
    ] = None,
    backend: BackendOpt = None,
    endpoint_url: EndpointOpt = None,
    bucket: BucketOpt = None,
    prefix: PrefixOpt = "aimm",
    region: RegionOpt = "us-east-1",
    preset: PresetOpt = BackendPreset.auto,
    addressing: AddressingOpt = Addressing.auto,
    checksum: ChecksumOpt = Checksum.auto,
    storage_class: StorageClassOpt = None,
    sse: SseOpt = None,
    sse_kms_key_id: SseKeyOpt = None,
    no_verify_tls: NoVerifyTlsOpt = False,
    ensure_bucket: EnsureBucketOpt = False,
    no_probe: NoProbeOpt = False,
) -> None:
    """List the repositories stored under the prefix."""
    opts, settings = open_settings(ctx, backend)
    with open_destination(settings) as destination:
        entries = catalog.list_repos(
            destination, settings.s3.prefix, repo_type=repo_type, owner=owner
        )

    document = {
        "command": "catalog.list",
        "run_id": opts.run_id,
        "repos": [dataclasses.asdict(entry) for entry in entries],
    }

    def render() -> None:
        table = Table(title="catalog")
        table.add_column("repository")
        table.add_column("revisions", justify="right")
        table.add_column("complete", justify="right")
        table.add_column("bytes", justify="right")
        table.add_column("latest", no_wrap=True)
        table.add_column("refs")
        for entry in entries:
            refs = ", ".join(f"{name}->{sha[:12]}" for name, sha in sorted(entry.refs.items()))
            table.add_row(
                f"{entry.repo_type.value}/{entry.repo_id}",
                str(entry.revisions),
                str(entry.complete_revisions),
                human_bytes(entry.total_bytes),
                entry.latest_sha[:12] if entry.latest_sha else "-",
                refs or "-",
            )
        opts.console.print(table)

    emit(opts, document, render)


@catalog_app.command("revisions")
def catalog_revisions(
    ctx: typer.Context,
    repo: Annotated[str, typer.Argument(help="owner/name | datasets/owner/name")],
    repo_type: RepoTypeOpt = RepoType.models,
    backend: BackendOpt = None,
    endpoint_url: EndpointOpt = None,
    bucket: BucketOpt = None,
    prefix: PrefixOpt = "aimm",
    region: RegionOpt = "us-east-1",
    preset: PresetOpt = BackendPreset.auto,
    addressing: AddressingOpt = Addressing.auto,
    checksum: ChecksumOpt = Checksum.auto,
    storage_class: StorageClassOpt = None,
    sse: SseOpt = None,
    sse_kms_key_id: SseKeyOpt = None,
    no_verify_tls: NoVerifyTlsOpt = False,
    ensure_bucket: EnsureBucketOpt = False,
    no_probe: NoProbeOpt = False,
) -> None:
    """List the stored revisions of one repository."""
    opts, settings = open_settings(ctx, backend)
    ref = parse_repo_spec(repo, default_type=repo_type, default_revision="main")
    with open_destination(settings) as destination:
        revisions = catalog.list_revisions(
            destination, settings.s3.prefix, ref.repo_type, ref.repo_id
        )
        refs = catalog.read_refs(destination, settings.s3.prefix, ref.repo_type, ref.repo_id)

    document = {
        "command": "catalog.revisions",
        "run_id": opts.run_id,
        "repo_type": ref.repo_type.value,
        "repo_id": ref.repo_id,
        "refs": refs,
        "revisions": [dataclasses.asdict(revision) for revision in revisions],
    }

    def render() -> None:
        pointing = {sha: name for name, sha in refs.items()}
        table = Table(title=f"{ref.repo_type.value}/{ref.repo_id}")
        table.add_column("commit", no_wrap=True)
        table.add_column("state")
        table.add_column("created")
        table.add_column("files", justify="right")
        table.add_column("bytes", justify="right")
        table.add_column("refs")
        for revision in revisions:
            table.add_row(
                revision.commit_sha[:12],
                "complete" if revision.complete else "[yellow]incomplete[/yellow]",
                timestamp(revision.created_at) if revision.created_at else "-",
                str(revision.file_count),
                human_bytes(revision.total_bytes),
                pointing.get(revision.commit_sha, "-"),
            )
        opts.console.print(table)

    emit(opts, document, render)


@catalog_app.command("show")
def catalog_show(
    ctx: typer.Context,
    repo: Annotated[str, typer.Argument(help="owner/name | datasets/owner/name")],
    repo_type: RepoTypeOpt = RepoType.models,
    revision: RevisionOpt = "main",
    backend: BackendOpt = None,
    endpoint_url: EndpointOpt = None,
    bucket: BucketOpt = None,
    prefix: PrefixOpt = "aimm",
    region: RegionOpt = "us-east-1",
    preset: PresetOpt = BackendPreset.auto,
    addressing: AddressingOpt = Addressing.auto,
    checksum: ChecksumOpt = Checksum.auto,
    storage_class: StorageClassOpt = None,
    sse: SseOpt = None,
    sse_kms_key_id: SseKeyOpt = None,
    no_verify_tls: NoVerifyTlsOpt = False,
    ensure_bucket: EnsureBucketOpt = False,
    no_probe: NoProbeOpt = False,
) -> None:
    """Show one stored manifest, after verifying its digest."""
    opts, settings = open_settings(ctx, backend)
    ref = parse_repo_spec(repo, default_type=repo_type, default_revision=revision)
    with open_destination(settings) as destination:
        commit_sha = resolve_commit(
            destination, settings.s3.prefix, ref.repo_type, ref.repo_id, ref.revision
        )
        manifest = catalog.show(
            destination, settings.s3.prefix, ref.repo_type, ref.repo_id, commit_sha
        )

    document = {
        "command": "catalog.show",
        "run_id": opts.run_id,
        "manifest": manifest.model_dump(mode="json"),
    }
    emit(opts, document, lambda: render_manifest(opts.console, manifest))


def render_manifest(console: Console, manifest: Manifest) -> None:
    """Render a manifest summary plus its file table."""
    console.print(
        f"{manifest.source.repo_type}/{manifest.source.repo_id}"
        f"@{manifest.source.commit_sha[:12]} "
        f"created {manifest.created_at} by aimm {manifest.tool_version} "
        f"(run {manifest.run_id})"
    )
    console.print(
        f"{manifest.totals.files} file(s), {human_bytes(manifest.totals.bytes)}, "
        f"{manifest.totals.transferred} transferred, {manifest.totals.skipped} skipped"
    )
    table = Table(title="files")
    table.add_column("path")
    table.add_column("size", justify="right")
    table.add_column("sha256", no_wrap=True)
    table.add_column("source")
    table.add_column("path kind")
    table.add_column("parts", justify="right")
    for entry in manifest.files:
        table.add_row(
            entry.path,
            human_bytes(entry.size),
            entry.sha256[:16],
            entry.sha256_source,
            entry.transfer_path,
            str(entry.s3_parts),
        )
    console.print(table)


# ── doctor ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Check:
    """One diagnostic result."""

    name: str
    ok: bool
    detail: str


@app.command()
def doctor(
    ctx: typer.Context,
    source: SourceOpt = SourceKind.huggingface,
    backend: BackendOpt = None,
    endpoint_url: EndpointOpt = None,
    bucket: BucketOpt = None,
    prefix: PrefixOpt = "aimm",
    region: RegionOpt = "us-east-1",
    preset: PresetOpt = BackendPreset.auto,
    addressing: AddressingOpt = Addressing.auto,
    checksum: ChecksumOpt = Checksum.auto,
    storage_class: StorageClassOpt = None,
    sse: SseOpt = None,
    sse_kms_key_id: SseKeyOpt = None,
    no_verify_tls: NoVerifyTlsOpt = False,
    ensure_bucket: EnsureBucketOpt = False,
    no_probe: NoProbeOpt = False,
) -> None:
    """Probe the environment and print the resolved settings with secrets masked.

    This is the command to paste into a bug report. Every probe is reported; the command
    still exits non-zero when something is broken.
    """
    opts = globals_of(ctx)
    checks: list[Check] = []
    profile_path = find_profile(opts.profile)
    settings = load_settings(
        profile=opts.profile, backend=backend, overrides=collect_overrides(ctx)
    )
    checks.append(Check("settings", True, f"profile: {profile_path or 'none (env + defaults)'}"))

    destination: S3Destination | None = None
    try:
        destination = S3Destination.create(
            settings.s3,
            workers=settings.transfer.workers,
            attempts=settings.transfer.max_attempts,
            max_wait=settings.transfer.max_wait,
        )
        caps = destination.capabilities
        checks.append(
            Check(
                "object store",
                True,
                f"bucket {settings.s3.bucket} reachable; "
                f"addressing={caps.addressing_style}, "
                f"checksums={caps.request_checksum_calculation}, "
                f"sha256={caps.supports_sha256_checksum}, "
                f"get_object_attributes={caps.supports_get_object_attributes}, "
                f"probed={caps.probed}, "
                # Surfaced in the table AND in --json, not only buried in the settings
                # dump: a profile carried from staging to production must be visible.
                f"tls_verification={'on' if settings.s3.verify_tls else 'DISABLED'}",
            )
        )
    except AimmError as exc:
        checks.append(Check("object store", False, describe(exc)))
    finally:
        if destination is not None:
            destination.close()

    if source is SourceKind.modelscope:
        # No identity probe: mirroring public ModelScope repos needs no credential, and
        # claiming a user name this tool never confirmed would be worse than saying so.
        modelscope = ModelScopeSource(settings.modelscope)
        try:
            checks.append(Check("modelscope", True, modelscope.ping()))
        except AimmError as exc:
            checks.append(Check("modelscope", False, describe(exc)))
        finally:
            modelscope.close()
    else:
        try:
            user = HubSource(settings.hub).whoami()
            checks.append(
                Check(
                    "hugging face",
                    True,
                    f"authenticated as {user}"
                    if user
                    else "unauthenticated - set HF_TOKEN for higher rate limits",
                )
            )
        except AimmError as exc:
            checks.append(Check("hugging face", False, describe(exc)))

    staging = settings.transfer.staging_dir or Path(tempfile.gettempdir())
    try:
        staging.mkdir(parents=True, exist_ok=True)
        probe = staging / f".aimm-doctor-{uuid4().hex}"
        probe.write_bytes(b"aimm")
        probe.unlink()
        free = shutil.disk_usage(staging).free
        checks.append(Check("staging dir", True, f"{staging} writable, {human_bytes(free)} free"))
    except OSError as exc:
        checks.append(Check("staging dir", False, describe(exc)))

    resolved = masked_settings(settings)
    failed = [check for check in checks if not check.ok]
    document = {
        "command": "doctor",
        "run_id": opts.run_id,
        "version": __version__,
        "ok": not failed,
        "checks": [dataclasses.asdict(check) for check in checks],
        "settings": resolved,
    }

    def render() -> None:
        table = Table(title=f"aimm {__version__} doctor")
        table.add_column("check")
        table.add_column("status")
        table.add_column("detail")
        for check in checks:
            status = "[green]ok[/green]" if check.ok else "[red]failed[/red]"
            table.add_row(check.name, status, check.detail)
        opts.console.print(table)
        opts.console.print("resolved settings (secrets masked):")
        opts.console.print_json(json.dumps(resolved, sort_keys=True))

    emit(opts, document, render)
    if failed:
        raise ConfigError(
            f"doctor found {len(failed)} problem(s): {', '.join(check.name for check in failed)}"
        )


def describe(exc: BaseException) -> str:
    """Render an exception for a report, with any credential-shaped text masked."""
    return redact(f"{type(exc).__name__}: {exc}")
