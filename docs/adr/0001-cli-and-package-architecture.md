# ADR 0001 — CLI and package architecture

- Status: accepted
- Date: 2026-07-20
- Affects: the `aimm` core, all future tools

## Context

`AIModelManagement` is designed as a long-lived "swiss army knife" for AI model
development. Tool #1 (`hf-backup`) is the first of many building blocks. The starting point
was a single script, `hf2s3.py`, that mixed library and CLI concerns — among other things, a
helper called `sys.exit()`.

We need a structure that achieves three things at once:

1. Tools #2..#N can be added without touching core code.
2. The logic is testable and embeddable without the CLI.
3. No framework emerges that costs more effort than it saves.

The house conventions are binding: a `src/` layout, `cli.py` plus `main.py`, domain
packages, `tests/` mirroring `src/`, and extension through setuptools entry points (as in the
sibling repository `BackupHelper`).

## Decision

**One distribution package with a `src/` layout, a tool-agnostic core, and tools as
subpackages under `bg_ai_model_management.tools.*`, mounted as `typer.Typer` sub-apps
through the entry-point group `aimm.tools`.**

```toml
[project.entry-points."aimm.tools"]
hf-backup = "bg_ai_model_management.tools.hfbackup.cli:app"
```

```python
for ep in sorted(entry_points(group="aimm.tools"), key=lambda e: e.name):
    app.add_typer(ep.load(), name=ep.name)
```

That is the entire extension mechanism. No protocol, no base class, no registry, no
dependency injection container.

Note the deliberate asymmetry in names: the distribution is `bg-ai-model-management`, the
import package is `bg_ai_model_management`, but the CLI command, the entry-point group, the
environment prefix and the profile filename are all keyed to **`aimm`**. Those latter names
are a public contract with users and with third-party tools; renaming any of them breaks
either a user's configuration or the plugin contract.

The core supplies what every tool needs anyway and what nobody should write twice:
`errors.py` (the exception tree plus the exit-code table), `logging_setup.py` (including
secret redaction), `config/` (precedence and `${ENV}` interpolation), `net/retry.py`,
`integrity/hashing.py`.

**Error handling is strictly layered.** Library code raises only typed exceptions from
`bg_ai_model_management.errors`. Translation into process exit codes happens in exactly
**one** place: `bg_ai_model_management/main.py`. No other module calls `sys.exit()` or
raises `typer.Exit`.

**CLI style:** `Annotated[...]` parameters throughout. The older positional form
(`x: str = typer.Option(...)`) is deprecated according to Typer 0.27's own runtime docstring
but still works — a linter will not catch it, so the decision is made once and held.

**Configuration precedence:** CLI flag → environment → secret file → profile YAML → default.
Typer parses `argv` and hands only *explicitly set* values to pydantic-settings as
`init_settings`. The two systems therefore do not parse against each other.

## Consequences

### Positive

- Tool #2 costs: one subpackage, one `app = typer.Typer()`, one line in `pyproject.toml`.
  The core is not touched, so it cannot break.
- A tool can live in a *foreign* distribution package and still mount — entry points are not
  limited to this repository.
- The engine is importable without the CLI and therefore directly unit-testable. The
  `sys.exit()` defect of the original script is structurally excluded, not merely patched
  over.
- A single exit-code mapping means a single place where monitoring contracts can break, and
  a table that can be tested.

### Negative

- Entry points are resolved at process start. That costs milliseconds and, more seriously, a
  broken third-party tool could crash the entire CLI's `--help`, because `ep.load()` imports
  it. Mitigation: the loader catches `Exception` per entry point, logs a warning and skips
  the tool rather than killing the process.
- The core is a shared coupling point. A change to `errors.py` affects every tool. That is
  intended — consistency of exit codes matters more than isolation — but it does mean
  `errors.py` must be treated conservatively.
- One distribution package for all tools means a shared dependency list. If tool #4 needs a
  heavy dependency, everyone who only uses `hf-backup` pays for it too. The escape route,
  when the time comes: `[project.optional-dependencies]` per tool plus an import guard.
  Today that would be building for a future that may not arrive.

### Neutral

- `aimm tools` lists the registered tools. Five lines of code, but the only way to make the
  seam operable rather than merely asserted.

## Alternatives rejected

**One repository per tool.** Maximum isolation, but core code (redaction, retry, exit codes,
configuration precedence) would have to be duplicated or versioned as a separate package.
For a solo-developer toolkit that is more release overhead than benefit.

**A bespoke plugin protocol** (`class Tool(ABC)` with `register()`, capability discovery,
hook points). Without a second real consumer, every signature is speculation. A `typer.Typer`
object is already the interface — it has commands, help and arguments.

**Namespace packages with a directory scan.** Only works when everything lives in the same
tree, breaks under zip imports, and offers no advantage over entry points.

**`argparse` instead of Typer.** Fewer dependencies, but sub-app composition, `envvar=`
support, enum metavars and the help panels would all have to be hand-built. Typer is already
in house.

**Everything in `cli.py`, logic included.** The state of the original script. Not testable,
not embeddable, and every error path ends in `sys.exit()`.

**pydantic-settings `CliSettingsSource` instead of Typer overrides.** Both want to own
`argv`. The combination is unverified; instead Typer parses and pydantic-settings validates
and layers — each does what it is good at.
