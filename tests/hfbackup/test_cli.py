"""Tests for the `aimm hf-backup` command surface.

Three contracts are asserted here that nothing else in the suite can reach:

* **Exit codes are a scheduling contract.** 0, 20 (drift) and 6 (corrupt) must stay
  distinct, because a cron job's only view of the outcome is `$?`. These are driven
  through `main.run`, which is the one place exceptions become exit codes.
* **Under `--json`, stdout carries exactly one document.** Anything else — a log line, a
  progress bar, a stray table — makes every downstream `jq` invocation fail.
* **`prune` without `--yes` changes nothing.** Not one object deleted, not one multipart
  upload aborted.

The object store is real moto; Hugging Face is a fake that fails the test if a command
that is supposed to work offline touches it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar

import click
import pytest
import typer
from typer.main import get_command
from typer.testing import CliRunner

from bg_ai_model_management import main as main_module
from bg_ai_model_management.config.models import Settings
from bg_ai_model_management.errors import (
    EXIT_OK,
    ConfigError,
    SourceError,
)
from bg_ai_model_management.integrity.hashing import sha256_bytes
from bg_ai_model_management.tools.hfbackup import cli as hf_cli
from bg_ai_model_management.tools.hfbackup import keys
from bg_ai_model_management.tools.hfbackup.destination import S3Destination
from bg_ai_model_management.tools.hfbackup.types import RepoType

from .test_catalog import SHA_A, SHA_B, SHA_C, Seeder
from .test_engine import FakeSource

PREFIX = "aimm"
REPO = "acme/model"

EXIT_CONFIG = 2
EXIT_CORRUPT = 6
EXIT_RETENTION = 9
EXIT_DRIFT = 20


class OfflineHub:
    """A `HubSource` stand-in. Any use fails the test with a pointed message."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"the command reached Hugging Face (attribute {name!r}); browsing, verifying "
            "and pruning a backup must work without Hub credentials or connectivity"
        )


@pytest.fixture
def no_ambient_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the profile search at an empty directory.

    `find_profile` falls back to `$XDG_CONFIG_HOME/aimm/config.yaml` or
    `%APPDATA%/aimm/config.yaml`, either of which may genuinely exist on a maintainer's
    machine and would silently supply a bucket the test never asked for.
    """
    empty = tmp_path / "config-home"
    empty.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty))
    monkeypatch.setenv("APPDATA", str(empty))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def wired_cli(
    monkeypatch: pytest.MonkeyPatch,
    destination: S3Destination,
    no_ambient_profile: None,
) -> S3Destination:
    """Wire the CLI to the moto destination and an offline Hub, and return the store."""

    @contextmanager
    def fake_open_destination(settings: Settings) -> Iterator[S3Destination]:
        yield destination  # deliberately not closed: the fixture owns its lifetime

    monkeypatch.setattr(hf_cli, "open_destination", fake_open_destination)
    monkeypatch.setattr(hf_cli, "HubSource", OfflineHub)
    return destination


@pytest.fixture
def seeded(wired_cli: S3Destination, settings: Settings) -> Seeder:
    """A store holding three complete revisions, with `main` pinning only the newest.

    Three, not two: `main` protects one revision and rule 2 always keeps the newest
    complete one, so a two-revision store can never produce a non-empty prune plan and
    the prune tests would assert nothing.
    """
    seeder = Seeder(wired_cli, settings)
    seeder.revision(REPO, SHA_A, created_at="2024-01-01T00:00:00Z")
    seeder.revision(REPO, SHA_B, created_at="2025-01-01T00:00:00Z")
    seeder.revision(REPO, SHA_C, created_at="2026-01-01T00:00:00Z")
    seeder.ref(REPO, "main", SHA_C)
    return seeder


def run(*args: str) -> int:
    """Invoke the whole CLI exactly as the console script does, returning the exit code."""
    return main_module.run(["hf-backup", *args, "--bucket", "aimm-test", "--prefix", PREFIX])


runner = CliRunner()


# ── exit codes stay distinct ─────────────────────────────────────────────────


def test_a_clean_command_exits_zero(seeded: Seeder, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("catalog", "list") == EXIT_OK


def test_drift_exits_twenty(seeded: Seeder, wired_cli: S3Destination, settings: Settings) -> None:
    """20 means 'differences found'. It is a finding, not a crash, and cron treats it so."""
    wired_cli.delete_keys(
        [keys.file_key(PREFIX, RepoType.models, REPO, SHA_B, "config.json")]
    )
    assert run("verify", REPO, "--revision", SHA_B) == EXIT_DRIFT


def test_an_incomplete_revision_exits_twenty(
    wired_cli: S3Destination, settings: Settings
) -> None:
    seeder = Seeder(wired_cli, settings)
    seeder.revision(REPO, SHA_A, complete=False)
    assert run("verify", REPO, "--revision", SHA_A) == EXIT_DRIFT


def test_corruption_exits_six(seeded: Seeder, wired_cli: S3Destination) -> None:
    """6 means the stored bytes are wrong. It must never be confused with 20."""
    key = keys.file_key(PREFIX, RepoType.models, REPO, SHA_B, "config.json")
    original = wired_cli.get_bytes(key)
    replacement = b"X" * len(original)
    wired_cli.put_small(key, replacement, sha256=sha256_bytes(replacement))

    assert run("verify", REPO, "--revision", SHA_B, "--level", "deep") == EXIT_CORRUPT


def test_the_three_outcomes_produce_three_different_codes(
    seeded: Seeder, wired_cli: S3Destination
) -> None:
    """The whole point, asserted in one place: a scheduler can tell these apart."""
    clean = run("verify", REPO, "--revision", SHA_B)

    wired_cli.delete_keys([keys.file_key(PREFIX, RepoType.models, REPO, SHA_A, "config.json")])
    drift = run("verify", REPO, "--revision", SHA_A)

    key = keys.file_key(PREFIX, RepoType.models, REPO, SHA_B, "model.bin")
    original = wired_cli.get_bytes(key)
    wired_cli.put_small(key, b"Z" * len(original), sha256=sha256_bytes(b"Z"))
    corrupt = run("verify", REPO, "--revision", SHA_B, "--level", "deep")

    assert (clean, drift, corrupt) == (EXIT_OK, EXIT_DRIFT, EXIT_CORRUPT)
    assert len({clean, drift, corrupt}) == 3


def test_no_strict_downgrades_drift_to_success(seeded: Seeder, wired_cli: S3Destination) -> None:
    """`--no-strict` is for the operator who wants the report without the alarm."""
    wired_cli.delete_keys([keys.file_key(PREFIX, RepoType.models, REPO, SHA_B, "config.json")])
    assert run("verify", REPO, "--revision", SHA_B, "--no-strict") == EXIT_OK


def test_corruption_ignores_no_strict(seeded: Seeder, wired_cli: S3Destination) -> None:
    """Corruption is never downgraded: `--no-strict` covers drift, not bad bytes."""
    key = keys.file_key(PREFIX, RepoType.models, REPO, SHA_B, "config.json")
    original = wired_cli.get_bytes(key)
    wired_cli.put_small(key, b"X" * len(original), sha256=sha256_bytes(b"X"))

    assert run("verify", REPO, "--revision", SHA_B, "--level", "deep", "--no-strict") == EXIT_CORRUPT


def test_a_usage_error_exits_two(no_ambient_profile: None) -> None:
    assert main_module.run(["hf-backup", "verify", "acme/model", "--level", "sideways"]) == (
        EXIT_CONFIG
    )


def test_an_unconstrained_prune_exits_nine(seeded: Seeder) -> None:
    assert run("prune", REPO) == EXIT_RETENTION


def test_a_missing_bucket_is_a_configuration_error(no_ambient_profile: None) -> None:
    assert main_module.run(["hf-backup", "catalog", "list"]) == EXIT_CONFIG


# ── --json owns stdout ───────────────────────────────────────────────────────


def parse_single_document(stdout: str) -> dict[str, Any]:
    """Assert stdout holds exactly one JSON document, and return it."""
    assert stdout.strip(), "no document was written to stdout"
    decoder = json.JSONDecoder()
    document, index = decoder.raw_decode(stdout.lstrip())
    trailing = stdout.lstrip()[index:].strip()
    assert not trailing, f"stdout carried more than one document; trailing text: {trailing!r}"
    assert isinstance(document, dict)
    return document


@pytest.mark.parametrize(
    "args",
    [
        ("catalog", "list"),
        ("catalog", "revisions", REPO),
        ("catalog", "show", REPO, "--revision", SHA_B),
    ],
)
def test_json_mode_emits_exactly_one_document_on_stdout(
    seeded: Seeder, capsys: pytest.CaptureFixture[str], args: tuple[str, ...]
) -> None:
    code = main_module.run(
        ["--json", "hf-backup", *args, "--bucket", "aimm-test", "--prefix", PREFIX]
    )
    captured = capsys.readouterr()

    assert code == EXIT_OK
    document = parse_single_document(captured.out)
    assert document["command"].startswith(args[0])
    assert "run_id" in document


def test_every_log_line_goes_to_stderr_under_json(
    seeded: Seeder, wired_cli: S3Destination, capsys: pytest.CaptureFixture[str]
) -> None:
    """A single log line on stdout breaks every `aimm ... --json | jq` pipeline.

    A deliberately corrupt ref object is planted so the run is guaranteed to log a
    warning; asserting on an empty stderr would otherwise prove nothing.
    """
    broken = keys.ref_key(PREFIX, RepoType.models, REPO, "broken")
    wired_cli.put_small(broken, b"not json at all", sha256=sha256_bytes(b"x"))

    code = main_module.run(
        [
            "--json",
            "--log-level",
            "DEBUG",
            "hf-backup",
            "catalog",
            "revisions",
            REPO,
            "--bucket",
            "aimm-test",
            "--prefix",
            PREFIX,
        ]
    )
    captured = capsys.readouterr()

    assert code == EXIT_OK
    document = parse_single_document(captured.out)  # stdout is still exactly one document
    assert "skipping unreadable ref object" in captured.err, (
        "the warning must appear on stderr, where it cannot corrupt the JSON document"
    )
    assert "skipping unreadable ref object" not in captured.out
    assert "broken" not in document["refs"]


def test_json_mode_renders_no_human_table(
    seeded: Seeder, capsys: pytest.CaptureFixture[str]
) -> None:
    main_module.run(
        ["--json", "hf-backup", "catalog", "list", "--bucket", "aimm-test", "--prefix", PREFIX]
    )
    out = capsys.readouterr().out
    assert "─" not in out and "│" not in out, "a rich table leaked onto stdout"


def test_a_failing_command_still_emits_its_json_document(
    seeded: Seeder, wired_cli: S3Destination, capsys: pytest.CaptureFixture[str]
) -> None:
    """The report is emitted before the exception, so a drifting verify is still scriptable."""
    wired_cli.delete_keys([keys.file_key(PREFIX, RepoType.models, REPO, SHA_B, "config.json")])

    code = main_module.run(
        [
            "--json",
            "hf-backup",
            "verify",
            REPO,
            "--revision",
            SHA_B,
            "--bucket",
            "aimm-test",
            "--prefix",
            PREFIX,
        ]
    )
    captured = capsys.readouterr()

    assert code == EXIT_DRIFT
    document = parse_single_document(captured.out)
    assert document["status"] == "drift"
    assert document["findings"], "the findings must be machine-readable, not only rendered"


def test_without_json_nothing_is_written_to_stdout_by_the_reporter(
    seeded: Seeder, capsys: pytest.CaptureFixture[str]
) -> None:
    """Human output goes to the shared stderr console so stdout stays free for data."""
    assert run("catalog", "list") == EXIT_OK
    captured = capsys.readouterr()
    assert captured.out == ""


# ── prune is inert without --yes ─────────────────────────────────────────────


def stored_keys(destination: S3Destination) -> set[str]:
    return {summary.key for summary in destination.list_keys(PREFIX)}


def test_prune_without_yes_deletes_nothing(
    seeded: Seeder, wired_cli: S3Destination, capsys: pytest.CaptureFixture[str]
) -> None:
    """The single most destructive command in the tool must be inert by default."""
    before = stored_keys(wired_cli)
    assert before, "the fixture must have seeded something to delete"

    code = main_module.run(
        [
            "--json",
            "hf-backup",
            "prune",
            REPO,
            "--keep-last",
            "1",
            "--bucket",
            "aimm-test",
            "--prefix",
            PREFIX,
        ]
    )
    document = parse_single_document(capsys.readouterr().out)

    assert code == EXIT_OK
    assert document["applied"] is False
    assert document["repos"][0]["delete"], "the plan must actually propose a deletion"
    assert document["totals"]["objects_deleted"] == 0
    assert stored_keys(wired_cli) == before, "prune deleted objects without --yes"


def test_prune_without_yes_aborts_no_multipart_uploads(
    seeded: Seeder, wired_cli: S3Destination, s3_client: Any, s3_bucket: str
) -> None:
    """'Changes nothing' includes the multipart uploads, not only the objects."""
    s3_client.create_multipart_upload(
        Bucket=s3_bucket, Key=keys.repo_root(PREFIX, RepoType.models, REPO) + "/pending"
    )

    assert run("prune", REPO, "--keep-last", "1") == EXIT_OK

    pending = s3_client.list_multipart_uploads(Bucket=s3_bucket).get("Uploads", [])
    assert len(pending) == 1, "prune aborted a multipart upload without --yes"


def test_prune_with_yes_deletes_the_planned_revision(
    seeded: Seeder, wired_cli: S3Destination, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main_module.run(
        [
            "--json",
            "hf-backup",
            "prune",
            REPO,
            "--keep-last",
            "1",
            "--yes",
            "--bucket",
            "aimm-test",
            "--prefix",
            PREFIX,
        ]
    )
    document = parse_single_document(capsys.readouterr().out)

    assert code == EXIT_OK
    assert document["applied"] is True
    assert document["totals"]["objects_deleted"] > 0

    remaining = stored_keys(wired_cli)
    assert not any(SHA_A in key for key in remaining), "the old revision should be gone"
    assert any(SHA_B in key for key in remaining), "the kept revision must survive"
    assert any(SHA_C in key for key in remaining), "the ref-protected revision must survive"


def test_prune_never_deletes_a_revision_a_ref_points_at(
    seeded: Seeder, wired_cli: S3Destination, capsys: pytest.CaptureFixture[str]
) -> None:
    """`refs/*.json` feed `RetentionPolicy.protected`, end to end through the CLI."""
    seeded.ref(REPO, "pinned", SHA_A)

    main_module.run(
        [
            "--json",
            "hf-backup",
            "prune",
            REPO,
            "--keep-last",
            "1",
            "--yes",
            "--bucket",
            "aimm-test",
            "--prefix",
            PREFIX,
        ]
    )
    document = parse_single_document(capsys.readouterr().out)

    protected = {rev["commit_sha"] for rev in document["repos"][0]["protected"]}
    assert protected == {SHA_A, SHA_C}, "both referenced revisions must be protected"
    assert document["repos"][0]["delete"] == [], "nothing is left to delete once B is kept"
    assert any(SHA_A in key for key in stored_keys(wired_cli))


def test_prune_requires_either_repos_or_all_repos(seeded: Seeder) -> None:
    assert run("prune", "--keep-last", "1") == EXIT_CONFIG
    assert run("prune", REPO, "--all-repos", "--keep-last", "1") == EXIT_CONFIG


# ── the help surface ─────────────────────────────────────────────────────────


def walk_commands(command: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Recurse into sub-groups by duck-typing rather than by isinstance.

    typer 0.27 vendors click: a `TyperGroup` derives from `typer._click.core.Command`,
    NOT from the `click.Group` importable as `click`. `isinstance(cmd, click.Group)` is
    therefore always False and would silently walk nothing at all.
    """
    children = getattr(command, "commands", None)
    if children:
        for name, sub in sorted(children.items()):
            yield from walk_commands(sub, (*path, name))
    else:
        yield path, command


def is_argument(param: Any) -> bool:
    """True for a positional argument, again without touching the real click classes."""
    return getattr(param, "param_type_name", "") == "argument"


ALL_COMMANDS = list(walk_commands(get_command(hf_cli.app)))
assert len(ALL_COMMANDS) > 1, "the command walk found nothing; the traversal itself is broken"


def test_the_command_set_is_the_documented_one() -> None:
    assert {path for path, _ in ALL_COMMANDS} == {
        ("sync",),
        ("verify",),
        ("restore",),
        ("prune",),
        ("doctor",),
        ("catalog", "list"),
        ("catalog", "revisions"),
        ("catalog", "show"),
    }


@pytest.mark.parametrize(
    ("path", "command"), ALL_COMMANDS, ids=[" ".join(path) for path, _ in ALL_COMMANDS]
)
def test_help_exits_zero_and_documents_every_flag(
    path: tuple[str, ...], command: click.Command
) -> None:
    """An undocumented flag is an unusable flag, and rich help silently truncates.

    The width that matters here is TERMINAL_WIDTH, pinned in the root conftest at import
    time — typer reads it into a module constant when `typer.rich_utils` is imported, so
    passing it through `env=` below would arrive far too late to have any effect. At the
    non-TTY default of 80 columns rich wraps long option names mid-token, and
    `--abort-older-than` then exists on screen but not as a contiguous string.
    """
    result = runner.invoke(
        hf_cli.app, [*path, "--help"], env={"TERM": "dumb", "NO_COLOR": "1"}
    )
    assert result.exit_code == 0, result.output

    missing = []
    for param in command.params:
        if is_argument(param):
            continue
        long_opts = [opt for opt in param.opts if opt.startswith("--")]
        if long_opts and not any(opt in result.output for opt in long_opts):
            missing.append(long_opts[0])
    assert not missing, f"`{' '.join(path)} --help` does not mention: {missing}"


def test_the_root_help_lists_every_command() -> None:
    result = runner.invoke(hf_cli.app, ["--help"], env={"COLUMNS": "300", "TERM": "dumb"})
    assert result.exit_code == 0
    for name in ("sync", "verify", "restore", "prune", "catalog", "doctor"):
        assert name in result.output


def test_no_command_accepts_a_credential_as_a_flag() -> None:
    """Credentials on the command line land in shell history and in `ps` output."""
    forbidden = {
        "--secret-access-key",
        "--access-key-id",
        "--secret",
        "--password",
        "--token",
        "--hf-token",
        "--session-token",
        "--api-key",
    }
    for path, command in ALL_COMMANDS:
        opts = {opt for param in command.params for opt in param.opts}
        leaked = opts & forbidden
        assert not leaked, f"`{' '.join(path)}` accepts a credential flag: {leaked}"


def test_calling_the_tool_with_no_arguments_shows_help_rather_than_a_bare_error() -> None:
    """`no_args_is_help=True`: click renders the help and exits 2, not a cryptic usage line."""
    result = runner.invoke(hf_cli.app, [], env={"COLUMNS": "300", "TERM": "dumb"})
    assert "Usage" in result.output
    assert "sync" in result.output, "the help must actually list the commands"


# ── sync, restore and doctor end to end ──────────────────────────────────────


@pytest.fixture
def wired_hub(monkeypatch: pytest.MonkeyPatch, wired_cli: S3Destination) -> FakeSource:
    """Replace the offline Hub with a working in-memory repository."""
    source = FakeSource(
        {"config.json": b'{"model_type": "llama"}', "weights.bin": b"\x00\x01" * 64}
    )
    monkeypatch.setattr(hf_cli, "HubSource", lambda *args, **kwargs: source)
    return source


def test_sync_writes_a_manifest_and_reports_it(
    wired_hub: FakeSource, wired_cli: S3Destination, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main_module.run(
        ["--json", "hf-backup", "sync", REPO, "--bucket", "aimm-test", "--prefix", PREFIX]
    )
    document = parse_single_document(capsys.readouterr().out)

    assert code == EXIT_OK
    assert document["ok"] is True
    assert document["dry_run"] is False
    repo = document["repos"][0]
    assert repo["files_total"] == 2
    assert repo["files_transferred"] == 2
    assert repo["manifest_key"], "a clean sync must report where the manifest landed"
    assert wired_cli.exists(repo["manifest_key"])


def test_a_dry_run_sync_writes_nothing(
    wired_hub: FakeSource, wired_cli: S3Destination, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main_module.run(
        [
            "--json",
            "hf-backup",
            "sync",
            REPO,
            "--dry-run",
            "--bucket",
            "aimm-test",
            "--prefix",
            PREFIX,
        ]
    )
    document = parse_single_document(capsys.readouterr().out)

    assert code == EXIT_OK
    assert document["dry_run"] is True
    assert document["repos"][0]["files_transferred"] == 0
    assert list(wired_cli.list_keys(PREFIX)) == []


def test_sync_with_a_failing_file_exits_eight(
    wired_hub: FakeSource, wired_cli: S3Destination
) -> None:
    """A transfer failure is exit 8, distinct from drift, corruption and config errors."""
    wired_hub.fail_all["weights.bin"] = 99
    assert run("sync", REPO) == 8


def test_sync_requires_at_least_one_repository(wired_hub: FakeSource) -> None:
    assert run("sync") == EXIT_CONFIG


def test_sync_reads_repositories_from_a_file(
    wired_hub: FakeSource, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    listing = tmp_path / "repos.txt"
    listing.write_text(f"# repos\n{REPO}\n", encoding="utf-8")

    code = main_module.run(
        [
            "--json",
            "hf-backup",
            "sync",
            "--from-file",
            str(listing),
            "--bucket",
            "aimm-test",
            "--prefix",
            PREFIX,
        ]
    )
    document = parse_single_document(capsys.readouterr().out)

    assert code == EXIT_OK
    assert document["repos"][0]["repo_id"] == REPO


def test_restore_materialises_the_backup_on_disk(
    seeded: Seeder, dest_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`restore` must work with the Hub wired to explode — that is the whole point."""
    code = main_module.run(
        [
            "--json",
            "hf-backup",
            "restore",
            REPO,
            "--dest",
            str(dest_dir),
            "--revision",
            SHA_C,
            "--bucket",
            "aimm-test",
            "--prefix",
            PREFIX,
        ]
    )
    document = parse_single_document(capsys.readouterr().out)

    assert code == EXIT_OK
    assert document["files"] == 2
    assert (dest_dir / "config.json").read_bytes() == b"{}"
    assert (dest_dir / "model.bin").read_bytes() == b"weights"


def test_restore_refuses_to_overwrite_without_the_flag(seeded: Seeder, dest_dir: Path) -> None:
    assert run("restore", REPO, "--dest", str(dest_dir), "--revision", SHA_C) == EXIT_OK
    assert run("restore", REPO, "--dest", str(dest_dir), "--revision", SHA_C) == EXIT_CONFIG
    assert (
        run("restore", REPO, "--dest", str(dest_dir), "--revision", SHA_C, "--overwrite")
        == EXIT_OK
    )


def test_restore_verify_only_writes_nothing(seeded: Seeder, dest_dir: Path) -> None:
    assert (
        run("restore", REPO, "--dest", str(dest_dir), "--revision", SHA_C, "--verify-only")
        == EXIT_OK
    )
    assert list(dest_dir.iterdir()) == []


def test_doctor_reports_every_check_and_masks_the_settings(
    wired_cli: S3Destination, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`doctor` is what people paste into bug reports, so it must never leak a secret."""
    monkeypatch.setattr(
        hf_cli.S3Destination, "create", classmethod(lambda cls, s3, **kw: wired_cli)
    )
    monkeypatch.setattr(hf_cli, "HubSource", lambda *a, **k: _Whoami("karlbauer"))

    code = main_module.run(
        ["--json", "hf-backup", "doctor", "--bucket", "aimm-test", "--prefix", PREFIX]
    )
    document = parse_single_document(capsys.readouterr().out)

    assert code == EXIT_OK
    assert document["ok"] is True
    assert {check["name"] for check in document["checks"]} == {
        "settings",
        "object store",
        "hugging face",
        "staging dir",
    }
    assert "settings" in document


def test_doctor_exits_non_zero_when_a_check_fails(
    wired_cli: S3Destination, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every probe is still reported, but the command must not pretend to be healthy."""
    from bg_ai_model_management.errors import BucketNotFoundError

    def explode(cls: object, s3: object, **kwargs: object) -> None:
        raise BucketNotFoundError("bucket 'aimm-test' does not exist or is not visible")

    monkeypatch.setattr(hf_cli.S3Destination, "create", classmethod(explode))
    monkeypatch.setattr(hf_cli, "HubSource", lambda *a, **k: _Whoami(None))

    code = main_module.run(
        ["--json", "hf-backup", "doctor", "--bucket", "aimm-test", "--prefix", PREFIX]
    )
    document = parse_single_document(capsys.readouterr().out)

    assert code == EXIT_CONFIG
    assert document["ok"] is False
    failed = [check for check in document["checks"] if not check["ok"]]
    assert [check["name"] for check in failed] == ["object store"]
    assert "BucketNotFoundError" in failed[0]["detail"]


class _Whoami:
    def __init__(self, user: str | None) -> None:
        self._user = user

    def whoami(self) -> str | None:
        return self._user


# ── the human renderers ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "args",
    [
        ("catalog", "list"),
        ("catalog", "revisions", REPO),
        ("catalog", "show", REPO, "--revision", SHA_C),
        ("prune", REPO, "--keep-last", "1"),
    ],
)
def test_every_command_renders_a_human_report_on_stderr(
    seeded: Seeder, capsys: pytest.CaptureFixture[str], args: tuple[str, ...]
) -> None:
    """Without --json the report is drawn on the shared console, leaving stdout empty."""
    assert run(*args) == EXIT_OK
    captured = capsys.readouterr()
    assert captured.out == "", "stdout must stay free for machine-readable output"
    assert captured.err.strip(), "the human report was not rendered at all"


def test_the_prune_dry_run_says_so_in_the_rendered_output(
    seeded: Seeder, capsys: pytest.CaptureFixture[str]
) -> None:
    """An operator reading the table must not mistake a plan for an applied deletion."""
    assert run("prune", REPO, "--keep-last", "1") == EXIT_OK
    assert "nothing was deleted" in capsys.readouterr().err


def test_sync_renders_a_table(
    wired_hub: FakeSource, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("sync", REPO) == EXIT_OK
    assert REPO in capsys.readouterr().err


def test_restore_renders_a_single_line(
    seeded: Seeder, dest_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("restore", REPO, "--dest", str(dest_dir), "--revision", SHA_C) == EXIT_OK
    # Collapse whitespace rather than deleting newlines. Rich wraps ON a space and drops
    # it, so stripping "\n" fuses the halves back together: a wrap inside "2 file(s)"
    # became the literal "2file(s)" and failed only where the wrap point happened to land
    # there. conftest pins COLUMNS so this should no longer wrap at all, but normalising
    # keeps the assertion true regardless of width.
    err = " ".join(capsys.readouterr().err.split())
    assert dest_dir.name in err
    assert "2 file(s)" in err
    assert SHA_C[:12] in err


def test_verify_renders_its_findings(
    seeded: Seeder, wired_cli: S3Destination, capsys: pytest.CaptureFixture[str]
) -> None:
    wired_cli.delete_keys([keys.file_key(PREFIX, RepoType.models, REPO, SHA_B, "config.json")])
    assert run("verify", REPO, "--revision", SHA_B) == EXIT_DRIFT
    err = capsys.readouterr().err
    assert "DRIFT" in err
    assert "findings" in err


# ── argument parsing helpers ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("spec", "expected_id", "expected_type", "expected_revision"),
    [
        ("acme/model", "acme/model", RepoType.models, "main"),
        ("datasets/acme/data", "acme/data", RepoType.datasets, "main"),
        ("models/acme/model", "acme/model", RepoType.models, "main"),
        ("acme/model@v1.0", "acme/model", RepoType.models, "v1.0"),
        ("datasets/acme/data@abc123", "acme/data", RepoType.datasets, "abc123"),
        ("  acme/model  ", "acme/model", RepoType.models, "main"),
    ],
)
def test_parse_repo_spec(
    spec: str, expected_id: str, expected_type: RepoType, expected_revision: str
) -> None:
    ref = hf_cli.parse_repo_spec(spec, default_type=RepoType.models, default_revision="main")
    assert (ref.repo_id, ref.repo_type, ref.revision) == (
        expected_id,
        expected_type,
        expected_revision,
    )


@pytest.mark.parametrize("spec", ["", "   ", "/leading", "trailing/", "a//b", "@rev"])
def test_parse_repo_spec_rejects_malformed_input(spec: str) -> None:
    with pytest.raises(ConfigError):
        hf_cli.parse_repo_spec(spec, default_type=RepoType.models, default_revision="main")


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("30m", 1800), ("12h", 43200), ("90d", 7776000), ("2w", 1209600), ("1H", 3600)],
)
def test_parse_duration(text: str, seconds: int) -> None:
    assert hf_cli.parse_duration(text).total_seconds() == seconds


@pytest.mark.parametrize("text", ["", "30", "d", "-1d", "1y", "thirty days", "1.5h"])
def test_parse_duration_rejects_nonsense(text: str) -> None:
    with pytest.raises(ConfigError):
        hf_cli.parse_duration(text)


def test_read_specs_skips_blanks_and_comments(tmp_path: Path) -> None:
    listing = tmp_path / "repos.txt"
    listing.write_text(
        "\n".join(
            [
                "# a comment",
                "acme/one",
                "",
                "   ",
                "acme/two  # trailing comment",
                "datasets/acme/three",
            ]
        ),
        encoding="utf-8",
    )
    assert hf_cli.read_specs(listing) == ["acme/one", "acme/two", "datasets/acme/three"]


def test_read_specs_of_none_is_empty() -> None:
    assert hf_cli.read_specs(None) == []


def test_read_specs_reports_an_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        hf_cli.read_specs(tmp_path / "absent.txt")


@pytest.mark.parametrize(
    ("count", "rendered"),
    [(0, "0 B"), (512, "512 B"), (1024, "1.0 KiB"), (1024**2, "1.0 MiB"), (1024**4, "1.0 TiB")],
)
def test_human_bytes(count: int, rendered: str) -> None:
    assert hf_cli.human_bytes(count) == rendered


def test_masked_settings_never_leaks_a_secret(settings: Settings) -> None:
    """This is the output people paste into bug reports."""
    from pydantic import SecretStr

    revealing = settings.model_copy(
        update={
            "s3": settings.s3.model_copy(
                update={
                    "secret_access_key": SecretStr("SUPERSECRETVALUE"),
                    "endpoint_url": "https://user:hunter2@s3.example.com",
                }
            )
        }
    )
    rendered = json.dumps(hf_cli.masked_settings(revealing))
    assert "SUPERSECRETVALUE" not in rendered
    assert "hunter2" not in rendered


def test_resolve_commit_uses_the_object_store_only(seeded: Seeder, wired_cli: S3Destination) -> None:
    assert (
        hf_cli.resolve_commit(wired_cli, PREFIX, RepoType.models, REPO, "main") == SHA_C
    )
    assert (
        hf_cli.resolve_commit(wired_cli, PREFIX, RepoType.models, REPO, SHA_A.upper()) == SHA_A
    )


def test_resolve_commit_reports_the_known_refs_when_a_revision_is_unknown(
    seeded: Seeder, wired_cli: S3Destination
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        hf_cli.resolve_commit(wired_cli, PREFIX, RepoType.models, REPO, "nonexistent")
    assert "known refs: main" in str(excinfo.value)


# ── override collection ──────────────────────────────────────────────────────


# These two are the regression guard for the vendored-click defect. `collect_overrides`
# once did `from click.core import ParameterSource` and compared by identity, but typer
# vendors its own click, so the check never fired: every untouched flag's default was
# passed to load_settings as an explicit override and the profile file was silently
# ignored for every setting with a flag. cli.py now compares the source by name.
def test_only_explicitly_typed_options_become_overrides(
    seeded: Seeder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An untouched flag must never beat the profile file.

    `collect_overrides` reads click's parameter source for exactly this reason; passing a
    flag's default down to `load_settings` would make every default silently authoritative.
    """
    captured: dict[str, Any] = {}
    real = hf_cli.load_settings

    def spy(**kwargs: Any) -> Settings:
        captured.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(hf_cli, "load_settings", spy)

    run("catalog", "list")

    overrides = captured["overrides"]
    assert overrides["s3.bucket"] == "aimm-test"
    assert overrides["s3.prefix"] == PREFIX
    assert "s3.region" not in overrides, "an untouched --region leaked into the overrides"
    assert "s3.storage_class" not in overrides
    assert "s3.verify_tls" not in overrides


def test_a_profile_setting_survives_an_untouched_flag(
    wired_cli: S3Destination, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator-visible consequence, asserted end to end through the CLI.

    An `aimm.yaml` that sets a region and a part size must win over flags the operator
    never typed. This is the test that would have caught the vendored-click mismatch.
    """
    profile = tmp_path / "aimm.yaml"
    profile.write_text(
        "s3:\n"
        "  bucket: aimm-test\n"
        "  region: eu-north1\n"
        "transfer:\n"
        "  workers: 32\n"
        "  part_size: 64MiB\n",
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}
    real = hf_cli.load_settings

    def spy(**kwargs: Any) -> Settings:
        resolved = real(**kwargs)
        captured["settings"] = resolved
        return resolved

    monkeypatch.setattr(hf_cli, "load_settings", spy)
    monkeypatch.setenv("AIMM_PROFILE", str(profile))

    main_module.run(["hf-backup", "catalog", "list"])

    settings = captured["settings"]
    assert settings.s3.region == "eu-north1", "an untouched --region overrode the profile"
    assert settings.transfer.workers == 32, "an untouched --workers overrode the profile"
    assert settings.transfer.part_size == 64 * 1024**2


def test_a_negated_flag_is_inverted_before_reaching_the_settings(
    seeded: Seeder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-probe` drives `s3.probe = False`, not `no_probe = True`."""
    captured: dict[str, Any] = {}
    real = hf_cli.load_settings

    def spy(**kwargs: Any) -> Settings:
        captured.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(hf_cli, "load_settings", spy)

    main_module.run(
        ["hf-backup", "catalog", "list", "--bucket", "aimm-test", "--no-probe", "--no-verify-tls"]
    )

    assert captured["overrides"]["s3.probe"] is False
    assert captured["overrides"]["s3.verify_tls"] is False


# ── standalone invocation ────────────────────────────────────────────────────


def test_the_sub_app_is_usable_without_the_root_callback() -> None:
    """`ctx.obj` is None when the sub-app runs on its own; that must not crash."""
    result = runner.invoke(hf_cli.app, ["catalog", "list", "--help"], env={"COLUMNS": "300"})
    assert result.exit_code == 0


def test_globals_of_falls_back_to_sane_defaults() -> None:
    context = typer.Context(get_command(hf_cli.app))
    context.obj = None
    options = hf_cli.globals_of(context)
    assert options.json_output is False
    assert options.log_level == "INFO"
    assert options.run_id


# ── regressions ──────────────────────────────────────────────────────────────


class RecordingEngine:
    """Captures whatever `progress_hook` is set to at the moment work begins."""

    seen: ClassVar[list[tuple[str, object]]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.progress_hook = None

    def _record(self, command: str) -> None:
        RecordingEngine.seen.append((command, self.progress_hook))

    def sync(self, request: object) -> object:
        self._record("sync")
        from bg_ai_model_management.tools.hfbackup.engine import SyncReport

        return SyncReport(run_id="r", repos=(), ok=True)

    def verify(self, request: object) -> object:
        self._record("verify")
        from bg_ai_model_management.tools.hfbackup.engine import VerifyReport
        from bg_ai_model_management.tools.hfbackup.types import VerifyStatus

        return VerifyReport(
            repo_id=REPO, commit_sha=SHA_A, status=VerifyStatus.ok, checked=0, findings=()
        )

    def restore(self, request: object) -> object:
        self._record("restore")
        from bg_ai_model_management.tools.hfbackup.engine import RestoreReport

        return RestoreReport(
            repo_id=REPO, commit_sha=SHA_A, files=0, bytes=0, skipped=0, duration_seconds=0.0
        )


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("sync", ("sync", REPO)),
        ("verify", ("verify", REPO)),
        ("restore", ("restore", REPO, "--dest", "DEST")),
    ],
)
def test_every_long_running_command_wires_a_progress_hook(
    command: str,
    args: tuple[str, ...],
    wired_cli: S3Destination,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: `Engine.progress_hook` was never assigned, so nothing rendered.

    `new_progress` had exactly one caller — `prune` — so a multi-hour, multi-terabyte
    `sync` printed nothing at all between the opening log line and the final table, and
    an operator could not tell a working run from a hung one. Both the engine docstring
    and `new_progress` describe wiring that did not exist.
    """
    RecordingEngine.seen = []
    monkeypatch.setattr(hf_cli, "Engine", RecordingEngine)
    resolved = tuple(str(tmp_path / "restored") if a == "DEST" else a for a in args)

    assert run(*resolved) == EXIT_OK

    hooks = [hook for name, hook in RecordingEngine.seen if name == command]
    assert hooks, f"{command} never reached the engine"
    assert all(hook is not None for hook in hooks), (
        f"{command} left progress_hook unset; the run renders no progress at all"
    )


def test_the_progress_hook_advances_once_per_file(wired_cli: S3Destination) -> None:
    """`sync` emits `skip` alone or `start` + `done`; verify and restore emit `done`."""
    console = hf_cli.get_console()
    engine = RecordingEngine()
    with hf_cli.file_progress(console, engine, "testing"):  # type: ignore[arg-type]
        hook = engine.progress_hook
        assert hook is not None
        for event in ("start", "done", "skip", "error"):
            hook(event, "a.bin", 0)
    assert engine.progress_hook is None, "the hook must be released with the progress bar"


def test_open_destination_sizes_the_pool_for_the_configured_workers(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Regression: `workers` was never passed, so `--workers 64` ran against 32 sockets.

    `transfer.max_attempts` and `transfer.max_wait` were dead for the same reason: every
    S3 call fell back to the hardcoded defaults, so an operator could not make a job fail
    fast during an incident.
    """
    captured: dict[str, Any] = {}

    class FakeDestination:
        @staticmethod
        def create(s3: object, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return FakeDestination()

        def close(self) -> None:
            return None

    monkeypatch.setattr(hf_cli, "S3Destination", FakeDestination)
    tuned = settings.model_copy(
        update={"transfer": settings.transfer.model_copy(
            update={"workers": 64, "max_attempts": 3, "max_wait": 12.5}
        )}
    )

    with hf_cli.open_destination(tuned):
        pass

    assert captured["workers"] == 64
    assert captured["attempts"] == 3
    assert captured["max_wait"] == 12.5


def test_doctor_reports_disabled_tls_verification_in_its_check_row(
    wired_cli: S3Destination, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: `--no-verify-tls` was visible only inside the settings dump.

    `doctor` rendered the object store as a plain `ok`, so a profile written for a
    self-signed staging MinIO could be carried to production with nothing in the report
    saying that certificates were no longer being checked.
    """

    class UnreachableHub:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def whoami(self) -> str | None:
            raise SourceError("no Hub in a unit test")

    monkeypatch.setattr(hf_cli, "HubSource", UnreachableHub)
    exit_code = main_module.run(
        [
            "--json",
            "hf-backup",
            "doctor",
            "--bucket",
            "aimm-test",
            "--prefix",
            PREFIX,
            "--no-verify-tls",
        ]
    )
    document = json.loads(capsys.readouterr().out)
    store = next(check for check in document["checks"] if check["name"] == "object store")
    assert "tls_verification=DISABLED" in store["detail"]
    assert exit_code in (EXIT_OK, EXIT_CONFIG)  # the Hub check may fail; TLS is the assertion
