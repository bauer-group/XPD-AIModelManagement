# Installation

## Requirements

| Requirement | Value |
| --- | --- |
| Python | 3.12, 3.13 or 3.14 |
| Operating system | Linux, macOS, Windows |
| Network | outbound HTTPS to the Hugging Face endpoint and to your object store |
| Disk | only for the `disk` transfer path; see [Transfer strategy](transfer-strategy.md) |

Python 3.12 is the floor and it is also what the project lints and type-checks against, so
the published wheel is honest about its own `requires-python`.

## Install from PyPI

```bash
pip install bg-ai-model-management
```

Remember the naming split: you install `bg-ai-model-management`, you import
`bg_ai_model_management`, and you run `aimm`.

## Install into a virtual environment

Recommended for anything other than a throwaway container, and required on distributions
that ship an externally managed Python:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install bg-ai-model-management
```

## Install with `pipx`

If you only ever want the command line and never the library, `pipx` keeps it isolated:

```bash
pipx install bg-ai-model-management
```

## Verify the install

```bash
aimm --version
aimm tools
```

`aimm tools` lists the mounted tools and should print at least:

```text
hf-backup  Back up Hugging Face repos to S3-compatible storage.
```

If `hf-backup` is missing, the entry point did not load. Run with
`--log-level DEBUG` — the loader logs a warning with the import traceback and skips the
broken tool rather than failing the whole CLI.

## Dependencies and version bounds

Every dependency carries **both** a lower and an upper bound. The lower bound is the
version actually verified during design; the upper bound is the next major, where the
vendor is allowed to break us. There is deliberately **no lockfile** — a published wheel
ships ranges, because a consumer's resolver has to be able to co-install it with everything
else in their environment. The reasoning, including why an earlier lockfile decision was
reversed, is recorded in
[ADR 0006](adr/0006-pypi-distribution.md).

Notable transitive dependency: `hf-xet` arrives with `huggingface_hub` on common
architectures and accelerates *file downloads to disk only*. It does nothing for the
streaming transfer path. See [Transfer strategy](transfer-strategy.md).

## Install from source

For development, or to run against an unreleased commit:

```bash
git clone https://github.com/bauer-group/XPD-AIModelManagement.git
cd XPD-AIModelManagement
pip install -e ".[dev]"
```

The development extra is `dev` (not `test`). Add `docs` to build the documentation site.
Full setup instructions are in [Development](development.md).

## Uninstall

```bash
pip uninstall bg-ai-model-management
```

Nothing is left behind in the object store — `aimm` never writes local state outside the
staging directory, which it cleans up after every file.
