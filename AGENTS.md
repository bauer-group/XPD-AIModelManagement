# AGENTS.md

Instructions for an AI agent — or a human in a hurry — working on this repo.

## Fast path

```bash
python -m venv .venv && .venv/Scripts/activate   # POSIX: .venv/bin/activate
pip install -e ".[dev,docs]"
make all-checks          # or: .\Make.cmd all-checks
aimm hf-backup doctor    # prints the resolved config, secrets masked
```

Python 3.12 is the floor and 3.14 is what the release runs on. `ruff` and `mypy`
are configured for **3.12** on purpose: `target-version` / `python_version` say
which version to *check against*, not which one you run, and at 3.12 they reject
syntax that would break a user on the floor of `requires-python`.

There is no `uv`, no lockfile, no Dockerfile and no container image in this repo.
If you are about to create one, you are working from an obsolete brief.

## What this is

`bg-ai-model-management` is a toolkit for AI model development and operations.
It is a **swiss-army knife**: one CLI, one config system, one logging and retry
story, and N independent tools mounted underneath it.

Tool #1 is `aimm hf-backup` — it backs up Hugging Face repos to S3-compatible
storage and can verify, restore and prune them.

## The naming split (three different names, all correct)

This trips up every newcomer and every agent. They are deliberately different:

| Thing | Value | Where it appears |
| --- | --- | --- |
| Distribution name | `bg-ai-model-management` | PyPI, `pip install`, `pyproject.toml` `name` |
| Import package | `bg_ai_model_management` | `import`, `src/` layout, `mypy` target |
| CLI command | `aimm` | what the operator actually types |

`aimm` is short because it is typed; the distribution name is long because PyPI
is a global namespace and BAUER GROUP packages are prefixed `bg-`.

## The five `aimm` identifiers that must never be renamed

These are user-facing. Renaming any of them breaks either a user's existing
config or the plugin contract, silently:

| Identifier | Value |
| --- | --- |
| CLI command | `aimm` |
| Entry-point group | `aimm.tools` |
| Environment prefix | `AIMM_*` |
| Profile filenames | `aimm.yaml` / `aimm.yml`, discovered in the CWD |
| Default S3 key prefix | `aimm` — already written into live buckets |

Unchanged for the same reason: the S3 user-metadata keys (`aimm-sha256`,
`aimm-repo-id`, `aimm-commit-sha`, `aimm-repo-type`), the restore part-file
suffix `.aimm-part`, the `user_agent_extra` value `aimm/<version>`, the user
config directory `$XDG_CONFIG_HOME/aimm/config.yaml`, and the manifest's
`"tool": "aimm"` field.

## Layout

```text
src/bg_ai_model_management/
├── __init__.py        __version__ ONLY, a plain literal (hatchling + semantic-release read it)
├── main.py            console entry point; the ONLY module that calls sys.exit()
├── cli.py             root Typer app + the aimm.tools entry-point loader
├── errors.py          the typed exception hierarchy and the exit codes
├── logging_setup.py   stdlib logging + the secret-redacting filter
├── net/retry.py       tenacity retry helper with an injectable sleep
├── integrity/hashing.py
├── config/            models.py (pydantic settings), loader.py, interpolation.py
└── tools/
    └── hfbackup/      types, keys, source, destination, manifest, planner,
                       retention, catalog, engine, cli
```

## How to add tool #2

The extension seam is a setuptools entry point. There is no plugin framework, no
registry and no DI, and none should be added.

1. Create `src/bg_ai_model_management/tools/<yourtool>/` with a `cli.py` that
   exports a module-level `app: typer.Typer`.
2. Register it in `pyproject.toml`:

   ```toml
   [project.entry-points."aimm.tools"]
   hf-backup = "bg_ai_model_management.tools.hfbackup.cli:app"
   your-tool = "bg_ai_model_management.tools.yourtool.cli:app"
   ```

3. Reinstall (`pip install -e ".[dev]"`) so the entry point is written into the
   installed metadata. It will not appear until you do.

`cli.py::load_tools` mounts every entry point in that group at startup, sorted by
name. Two properties it guarantees, which your tool must not break:

- **A tool that fails to import is logged and skipped.** One broken third-party
  tool must never take `aimm --help` down with it.
- **`cli.py` imports no tool, no `huggingface_hub` and no `boto3` at module
  scope.** `aimm --help` has to stay fast, and `huggingface_hub` reads its
  environment at *import* time — `main.py::seed_hf_env` has to run first.

## Rules that are not style

1. **Library code raises; only `main.py` exits.** Every error is a typed
   exception from `errors.py` carrying an `exit_code`. Library code never calls
   `sys.exit()`, `typer.Exit` or `print()`.
2. **No `os.environ` mutation after startup.** `main.py::seed_hf_env` seeds the
   HF defaults once, before any `huggingface_hub` import. Nothing else writes.
3. **Secrets are `pydantic.SecretStr`,** never logged, never in a repr, a
   traceback or a CLI flag. Credentials come from the environment or a profile.
4. **Hand-rolled multipart, never `boto3.upload_file`/`TransferManager`.** MinIO
   and Ceph/RGW are strict about multipart semantics, and `s3transfer` at unknown
   stream size picks 8 MiB parts (capping objects near 78 GiB) and doubles the
   chunk size for large files so the ETag stops being reproducible. Equal-sized
   parts, dense consecutive part numbers, `abort_multipart_upload` on **every**
   exception path, and a `head_object` ContentLength check after every upload.
5. **`from __future__ import annotations` at the top of every module,** full
   annotations, `mypy --strict` green.
6. **English everywhere** — identifiers, docstrings, log messages, CLI help,
   error strings and all prose.
7. **`huggingface_hub` is 1.x.** `hf_transfer` does not exist there. Never add
   it.

## Exit codes

`errors.py` is the single source of truth. Exit code `20` (`DriftDetectedError`)
is **a finding, not a crash** — a `verify` that exits 20 did its job and found a
difference. Anyone scripting `aimm` depends on that distinction.

| Code | Meaning |
| --- | --- |
| 0 | success |
| 1 | unexpected internal error |
| 2 | `ConfigError` |
| 3 | `AuthError` |
| 4 | `SourceError` |
| 5 | `DestinationError` |
| 6 | `IntegrityError` |
| 7 | `InsufficientDiskSpaceError` |
| 8 | `TransferError` |
| 9 | `RetentionRefusedError` |
| 20 | `DriftDetectedError` — a finding |
| 130 | interrupted |

## Verify before you claim anything

```bash
make all-checks      # ruff + mypy strict + pytest with an 80% coverage gate
make minio-up        # local MinIO rig, for the integration suite
make integration     # skips cleanly if AIMM_IT_ENDPOINT is unset
make minio-down
```

Integration tests gate on `AIMM_IT_ENDPOINT`. A machine without Docker must see
**skips**, never errors.

## Do not commit

**Agents must not run `git commit`, `git push`, `git reset` or `git checkout`.**
Write files; committing is the human's decision. An earlier agent committed
unprompted and it had to be cleaned up.

When a human does commit: Conventional Commits, subject in **past tense**, max 50
characters, no trailing period (`added X`, not `add X`). **Never**
`Co-Authored-By:` or any AI attribution.

## Where to look

| Question | File |
| --- | --- |
| What exit code does this failure produce? | `errors.py` |
| How does config resolve — flag vs env vs profile vs default? | `config/loader.py` |
| Why was this file skipped or re-uploaded? | `tools/hfbackup/planner.py` |
| Inline, stream or disk — who decides? | `tools/hfbackup/engine.py` |
| What exactly is verified, and against what? | `tools/hfbackup/manifest.py`, `integrity/hashing.py` |
| What does an S3 key look like? | `tools/hfbackup/keys.py` |
| Why did `prune` refuse? | `tools/hfbackup/retention.py` |
| Is a secret at risk of being logged? | `logging_setup.py` |
