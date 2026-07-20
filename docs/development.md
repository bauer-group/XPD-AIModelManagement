# Development

## Setup

```bash
git clone https://github.com/bauer-group/XPD-AIModelManagement.git
cd XPD-AIModelManagement
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev,docs]"
pre-commit install
```

The development extra is **`dev`**, not `test`. There is no `uv` and no lockfile — plain
`pip` with both-bounded dependencies. See [ADR 0006](adr/0006-pypi-distribution.md) for why.

## Make targets

`Makefile` and `Make.cmd` are POSIX/Windows twins and are kept in sync; a target present in
only one is a bug.

| Target | Command |
| --- | --- |
| `install` | `pip install -e .` |
| `install-dev` | `pip install -e ".[dev,docs]"` and `pre-commit install` |
| `lint` | `ruff check src tests` |
| `format` | `ruff check src tests --fix` |
| `type-check` | `mypy src/bg_ai_model_management` |
| `test` | `pytest -q -m "not integration"` |
| `test-cov` | as `test`, with coverage and `term-missing` |
| `integration` | `pytest -q -m integration` |
| `minio-up` | start the integration rig |
| `minio-down` | stop the rig and remove its volumes |
| `docs` | `mkdocs build` |
| `build` | `python -m build` |
| `clean` | remove build and test artefacts |
| `pre-commit` | `pre-commit run --all-files` |
| `all-checks` | lint, type-check, test with coverage |

## Quality gates

Everything below must pass before a commit:

| Gate | Rule |
| --- | --- |
| `ruff check` | rule sets `E`, `F`, `W`, `I`, `B`, `UP`, `SIM`, `RUF`; line length 100 |
| `mypy --strict` | full annotations; `from __future__ import annotations` at the top of every module |
| `pytest` | coverage floor 80%, enforced by the coverage config itself |
| markdownlint | every shipped Markdown file, against `.markdownlint.jsonc` |

Lint and type-check run against **Python 3.12**, the floor, while releases run on 3.14.
Checking against the floor is what keeps the published wheel honest about its own
`requires-python`.

## Code rules that are not negotiable

- **Library code raises typed exceptions from `bg_ai_model_management.errors`.** It never
  calls `sys.exit()`, never raises `typer.Exit`, never calls `print()`. Only
  `main.py` maps exceptions to exit codes, and it is the only place that calls `sys.exit()`.
- **No `os.environ` mutation after startup.** `main.py` seeds the Hugging Face environment
  defaults once, before any `huggingface_hub` import, because that library reads its entire
  environment at import time. Nothing else writes to `os.environ`.
- **Typer parameters use `Annotated[...]` only.** Never `x: str = typer.Option(...)`.
- **Secrets are `SecretStr`.** Never log `.get_secret_value()`, never accept a secret as a
  CLI flag.
- **All timestamps are RFC 3339 UTC with a trailing `Z`.**
- **English everywhere** — identifiers, docstrings, log messages, CLI help, error strings
  and all prose.

`cli.py` must not import `huggingface_hub`, `boto3` or any tool module at module scope:
`aimm --help` has to stay fast, and the Hub reads its environment at import time. Tools are
mounted lazily.

## Tests

Three layers:

| Layer | Scope | Needs |
| --- | --- | --- |
| Unit | key layout, part-size arithmetic, path selection, manifest round-trip, retention, config precedence, redaction, retry classification, exit-code mapping, path safety | nothing |
| `moto` | S3 API behaviour: pagination, metadata round-trip, simple multipart flows | nothing |
| MinIO integration | what `moto` demonstrably cannot do: real multipart semantics, `PartsCount`, path-style addressing, storage-class rejection, orphaned multipart uploads, deleting a prefix with over 1000 keys | Docker |

```bash
pytest -q -m "not integration"     # the fast gate
pytest -q -m integration           # needs the rig
```

Integration tests **skip cleanly** when the rig is not running. A developer with no Docker
must see skips, never errors.

## The MinIO rig

```bash
make minio-up      # docker compose -f tests/integration/docker-compose.yml up -d --wait
make integration
make minio-down    # docker compose ... down -v
```

The rig runs the organisation's own MinIO images and provisions a bucket, an IAM policy and
a **policy-scoped, non-root** credential pair that the tests authenticate with.

`AIMM_IT_ENDPOINT` is the gate — unset means the integration tests skip.

| Variable | Rig default | Meaning |
| --- | --- | --- |
| `AIMM_IT_ENDPOINT` | `http://localhost:9800` | the gate; unset means skip |
| `AIMM_IT_BUCKET` | `aimm-it` | bucket created by the init container |
| `AIMM_IT_REGION` | `eu-north1` | matches the MinIO region label |
| `AIMM_IT_ACCESS_KEY` | rig-local | policy-scoped user, never root |
| `AIMM_IT_SECRET_KEY` | rig-local | as above |

`AIMM_IT_*` is a separate namespace from the `AIMM_*` settings variables and does not
collide with them, even though the settings models forbid extra keys: the settings layer
resolves declared fields only and does not sweep every prefixed variable.

### What the rig does not cover

**The rig speaks plain HTTP.** The botocore `aws-chunked` checksum *trailer* only appears
over HTTPS, so a plain-HTTP rig cannot reproduce a production TLS trailer rejection. A green
run here is not evidence of trailer compatibility. This is why the capability probe exists
at runtime instead of a hard-coded table — see [Backends](backends.md#the-capability-probe).

Rig credentials are throwaway and local-only. They are allowlisted in the secret-scanner
configuration, because a scanner that cries wolf on the test rig trains everyone to ignore
it.

## Adding tool #2

The extension seam is one entry point that yields a `typer.Typer`. That is the whole
mechanism — no registry, no base class, no dependency injection.

### 1. Create the subpackage

```text
src/bg_ai_model_management/tools/mytool/
├── __init__.py
├── cli.py          # exports `app: typer.Typer`
└── ...             # your domain modules
```

### 2. Export a Typer app

```python
"""`aimm mytool` — one line describing what it does."""

from __future__ import annotations

from typing import Annotated

import typer

from bg_ai_model_management.cli import GlobalOptions, new_run_id
from bg_ai_model_management.errors import ConfigError
from bg_ai_model_management.logging_setup import get_console

app = typer.Typer(
    name="mytool",
    help="One line describing what it does.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def run(
    ctx: typer.Context,
    target: Annotated[str, typer.Argument(help="What to operate on.")],
) -> None:
    """Do the thing."""
    opts = globals_of(ctx)
    ...
```

The first line of the app's `help` is what `aimm tools` prints as the summary.

### 3. Register the entry point

```toml
[project.entry-points."aimm.tools"]
hf-backup = "bg_ai_model_management.tools.hfbackup.cli:app"
mytool    = "bg_ai_model_management.tools.mytool.cli:app"
```

Reinstall (`pip install -e ".[dev]"`) so the entry point is picked up, then `aimm tools`
lists it.

### What the core gives you for free

| Module | What it provides |
| --- | --- |
| `errors` | the exception tree and the single authoritative exit-code table |
| `logging_setup` | stdlib logging, the secret redaction filter, text and JSON formats, the shared console |
| `config` | the precedence chain, `${ENV}` interpolation, size parsing |
| `net.retry` | tenacity with a narrow retryability classification and an injectable sleep |
| `integrity.hashing` | streamed sha256, git blob id, composite ETag, single-pass hashing readers |

### What you must uphold

- Raise typed exceptions; never call `sys.exit()`.
- Read `ctx.obj` for the resolved `GlobalOptions`, and handle it being `None` when the
  sub-app is invoked standalone in a test.
- Use `get_console()` — the same console the log handler owns. A second console corrupts
  progress output.
- Honour `--json`: exactly one document on stdout, everything else on stderr.
- Add exit codes to `errors.py` only if genuinely new. Consistency across tools is worth
  more than a bespoke code.

### A tool in a different package

Entry points are not limited to this repository. A separate distribution can declare
`[project.entry-points."aimm.tools"]` and it will mount into `aimm` on install. It should
depend on `bg-ai-model-management` for the core. A tool that fails to import is logged and
skipped, so a broken third-party tool cannot break `aimm --help`.

## Documentation

```bash
mkdocs serve             # live preview at http://127.0.0.1:8000
mkdocs build --strict    # what `make docs` runs; a warning fails the build
```

Rules for the docs:

- Every command, flag, environment variable and exit code mentioned anywhere in `docs/` or
  `README.MD` **must exist**. When the docs and the CLI disagree, the CLI wins and the doc
  is the bug.
- Every fenced code block gets a language tag.
- Prefer a table or a runnable command over a paragraph.
- Never print a credential, not even a fake-looking one, outside an obvious placeholder such
  as `<your-access-key>`.
- All Markdown must pass markdownlint with `.markdownlint.jsonc`.

Do not hand-edit `CHANGELOG.md`; semantic-release generates it.

## Commits and releases

Conventional Commits with the subject in **past tense**, at most 50 characters, no full
stop:

```text
feat(hfbackup): added upstream verification level

Compares the manifest against the Hub tree at the pinned SHA, which
catches corruption of the manifest itself rather than only of the data.
```

Never include `Co-Authored-By:` or any AI attribution.

Releases are cut by python-semantic-release from the commit history: it computes the
version, rewrites `__version__`, regenerates the changelog, tags, and publishes to PyPI via
trusted publishing. `feat` produces a minor, `fix` and `perf` a patch, and a
`BREAKING CHANGE:` footer a major.
