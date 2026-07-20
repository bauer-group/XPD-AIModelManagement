# Contributing

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate          # POSIX: source .venv/bin/activate
pip install -e ".[dev,docs]"
pre-commit install
```

Or, equivalently:

```bash
make install-dev                # or: .\Make.cmd install-dev
```

Requires Python 3.12, 3.13 or 3.14. **Develop on 3.14**; the CI matrix runs all
three.

There is no lockfile and no `uv` here. Reproducibility for a published wheel
comes from **both-bounded dependencies** in `pyproject.toml` (`"typer>=0.27.0,<1.0.0"`),
because a wheel ships ranges, not a lock. A lockfile nothing consumes claims a
reproducibility that does not exist and becomes a second source of truth that
drifts in silence.

ruff and mypy are configured for **3.12** on purpose, and raising them would
quietly end backward compatibility rather than modernise anything:
`target-version` and `python_version` tell those tools which version to *check
against*, not which one you run. At 3.12 they reject 3.13+ syntax before it
reaches a user on the floor of `requires-python`.

## Quality gates

```bash
make all-checks                 # or: .\Make.cmd all-checks
```

Runs `ruff check`, `mypy --strict`, and pytest with an **80% coverage gate**. All
three must pass before a PR. Individually:

| Command | What it does |
| --- | --- |
| `make lint` | `ruff check src tests` |
| `make format` | `ruff check src tests --fix` |
| `make type-check` | `mypy src/bg_ai_model_management` |
| `make test` | pytest, integration deselected |
| `make test-cov` | pytest with coverage and the 80% gate |
| `make docs` | build the MkDocs site |
| `make build` | sdist + wheel via hatchling |
| `make pre-commit` | every hook over the whole tree |

`Makefile` and `Make.cmd` are **twins**. If you add a target, add it to both — a
target that exists in only one is a trap for whoever is on the other platform.
The command strings in both are identical to what CI runs; if they ever diverge,
CI is right and the Make target is the bug.

## Integration tests

Integration tests need Docker and are deselected by default. They run against a
local MinIO rig built from the organisation's own images:

```bash
make minio-up                   # docker compose up -d --wait
make integration                # pytest -q -m integration
make minio-down                 # down -v, volumes included
```

The suite gates on the `AIMM_IT_ENDPOINT` environment variable and **skips
cleanly** when the rig is not running. A laptop without Docker must see skips,
never errors — if you write an integration test that errors instead of skipping,
that is a bug in the test.

The rig earns its keep because MinIO and Ceph/RGW are strict about multipart
semantics in ways moto does not model. What it **cannot** prove: it runs over
plain HTTP, and the `aws-chunked` checksum trailer only appears over HTTPS. A
green integration run is not evidence about a production TLS endpoint.

## Adding a tool

`aimm` is a swiss-army knife: tools are mounted through the `aimm.tools`
entry-point group. See `AGENTS.md` for the three-step recipe. There is no plugin
framework and none should be added.

## The rules that are not style

Full list in `AGENTS.md`. The four that get violated most often:

1. **Library code raises typed exceptions; only `main.py` maps them to exit codes
   and calls `sys.exit()`.** If you find yourself importing `typer` to exit from
   a module under `config/`, `net/` or `integrity/`, the error belongs in
   `errors.py` instead.
2. **No secret in a log, a repr, a traceback or a CLI flag.** Credentials are
   `pydantic.SecretStr` and come from the environment or a profile.
3. **Hand-rolled multipart, never `boto3.upload_file` or `TransferManager`.**
   Every rule around it (equal-sized parts, dense consecutive part numbers,
   `abort_multipart_upload` on every exception path, a `head_object` length check
   after every upload) exists because a real backend broke without it.
4. **`huggingface_hub` is 1.x.** `hf_transfer` does not exist there. Never add
   it.

## Testing expectations

- New features get tests; bug fixes get a **regression** test that fails before
  the fix.
- Prefer a test that encodes *why* over one that restates the code. A test that
  names the failure mode it prevents is worth three that assert the obvious.
- Anything touching credentials gets a redaction test. `test_logging_redaction.py`
  is the pattern.
- Retry logic uses the **injectable sleep** in `net/retry.py`, so tests run
  instantly. A test that actually waits is a test nobody will run.
- Unit tests must not need Docker, the network, an S3 backend or a Hugging Face
  token. If yours does, it is an integration test and needs `-m integration`.

## Documentation

Docs live in `docs/` and are built with MkDocs Material. Every command, flag,
environment variable and exit code in the docs must exist in the CLI, and every
one in the CLI must appear in the docs.

**When the docs and the CLI disagree, the CLI wins and the doc is the bug** — the
CLI is executable and the prose is not. Write `docs/cli.md` by reading the actual
`--help` output, never from memory.

All Markdown must pass markdownlint with the repo's `.markdownlint.jsonc`. Every
code block needs a language tag. Never print a credential, not even a
fake-looking one, outside an obvious placeholder such as `<your-access-key>`.

## Commits

Conventional Commits, subject in **past tense**, max 50 characters, no trailing
period:

```text
feat(hfbackup): added stream transfer for large LFS files
fix(destination): aborted multipart upload on checksum failure
docs(security): documented the doctor-output masking limits
```

Types: `feat` `fix` `docs` `style` `refactor` `perf` `test` `chore` `ci` `build`
`revert`. Breaking: `type!` or a `BREAKING CHANGE:` footer.

**Never include `Co-Authored-By:` or any AI attribution.**

One commit = one logical change. Each must work standalone. No WIP on `main`.
Never commit while CI is red.

## Releases

Automated by python-semantic-release on push to `main`. `feat` → minor,
`fix`/`perf` → patch; `major_on_zero = false`, so while the package is `0.y.z` a
breaking change bumps the minor and going 1.0 stays a human decision.

The version lives in `src/bg_ai_model_management/__init__.py` as a plain literal
`__version__` and is the single source of truth — hatchling reads it at build
time and semantic-release rewrites it. **Do not compute it** (for example with
`importlib.metadata.version`); that breaks both.

Publication to PyPI uses **trusted publishing** (OIDC). There is no API token to
manage and none should be added.

## Reporting a vulnerability

See [SECURITY.MD](SECURITY.MD). Do not open a public issue.
