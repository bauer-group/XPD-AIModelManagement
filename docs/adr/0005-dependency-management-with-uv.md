# ADR 0005 — Dependency management with uv

- Status: **superseded by [ADR 0006](0006-pypi-distribution.md)**
- Date: 2026-07-20
- Superseded: 2026-07-20
- Affects: `pyproject.toml`, `uv.lock`, `.python-version`, `Dockerfile`, CI

> **This ADR no longer describes the project.** There is no `uv.lock`, no `.python-version`
> and no uv in this repository; the build backend is hatchling, not setuptools; and the
> development extra is `dev`, not `test`. The reasoning below is preserved unchanged because
> it was sound *for the deliverable it was written about*, and because the exact shape of its
> central argument is what makes the reversal comprehensible: the case for a lockfile rested
> entirely on a container image being rebuilt automatically on a base-image cron. When the
> deliverable became a PyPI wheel, that premise disappeared — a wheel ships version
> **ranges** and can never ship a lockfile. See [ADR 0006](0006-pypi-distribution.md).

## Context

The house standard for Python projects is `pip` with `>=` lower bounds and no lockfile. For
this repository the owner explicitly deviated from that ("use it if it makes sense; our
ecosystem is alive and the best option wins"). This deviation is recorded here so that it
stays comprehensible and bounded.

The decisive reason is repository-specific, not general: the base-image monitor rebuilds the
image **automatically every day** as soon as the base image digest moves. With `pip` and `>=`
lower bounds, such a *security patch* rebuild silently pulls new `boto3` and
`huggingface_hub` majors into an artefact whose entire purpose is to still be able to restore
a model in three years. That case is real: `huggingface_hub` 1.0 swapped `requests` for
`httpx`, removed `local_dir_use_symlinks`, `resume_download` and `configure_http_backend`,
and moved the error classes. An unattended rebuild across such a boundary produces an image
that no longer starts — and nobody looks, because it was "only a base-image update".

Speed is a pleasant side effect, but it is not the justification.

## Decision

**`uv` takes over locking, installation, virtualenv and Python version management.
`setuptools` remains the build backend. `uv.lock` is committed. The deviation is deliberately
minimal.**

- `[build-system] requires = ["setuptools>=68", "wheel"]`,
  `build-backend = "setuptools.build_meta"`. **No** hatchling, **no** poetry.
- Development and test dependencies live in `[project.optional-dependencies]` under `test` —
  **not** in PEP 735 `[dependency-groups]`. That keeps `pip install -e ".[test]"` working for
  anyone without uv; uv reads the same table via `uv sync --extra test`.
- `.python-version` pins `3.14`. `requires-python = ">=3.12"`; CI and the image run 3.14.
- CI uses `astral-sh/setup-uv` (pinned to a tag) plus `uv sync --locked`.
- The Dockerfile pulls `uv` via `COPY --from=ghcr.io/astral-sh/uv:<pinned tag>` into the
  builder and test stages and **never** ships it in the production image.
- Dependency lower bounds correspond to the versions actually verified against. Raising a
  lower bound is always safe; guessing a lower one is not.

## Consequences

### Positive

- An automatic rebuild caused by a base-image patch does not change the Python dependencies.
  That is the actual purpose.
- `uv sync --locked` aborts when `uv.lock` does not match `pyproject.toml`. Drift becomes
  loud rather than silent.
- `uv` resolves for foreign target platforms (`--python-platform`), so wheel availability for
  `linux/arm64` can be checked in CI without building arm64.
- Resolution and installation are orders of magnitude faster, which noticeably shortens the
  three-stage Docker build.

### Negative

- **One more tool in the chain** that has to be maintained and pinned. The local development
  host has 0.11.7, the uv documentation shows 0.11.29 — the version pinned in CI must be the
  one actually validated against, otherwise lockfile format and resolver drift apart between
  dev and CI.
- **`python-build.yml` from `automation-templates` does not know `uv`.** Its
  `package-manager` accepts only pip/poetry/pipenv/conda, and its cache key does not hash
  `uv.lock`. The Python CI job must therefore be written inline — a deviation from the house
  rule "prefer reusable workflows", confined to that one job. See ADR 0004.
- **Dependabot and `uv.lock`** do not mesh cleanly: a PR that raises `pyproject.toml` without
  regenerating the lockfile breaks the Docker build. That is the desired behaviour, but it
  creates manual work.
- A committed lockfile also means security updates to the Python dependencies must be applied
  **actively**. The convenience of automatic patches is traded for reproducibility — for an
  archival tool that is the right direction, but it is a trade, not a win.

### Neutral

- Anyone who does not want to install uv can still use `pip install -e ".[test]"`. They then
  do not get pinned versions — acceptable for local development and unacceptable for the
  image, which is exactly where `--locked` is enforced.

## Alternatives rejected

**Plain `pip` per the house standard.** The automatic rebuild problem would remain unsolved.
The alternative would be pinning every dependency hard to `==`, which means a hand-maintained
lockfile without tooling support, including no transitive resolution.

**Poetry.** Can do the same, but is slower, brings its own build backend (which would drop the
setuptools requirement) and is not established in house.

**PEP 735 `[dependency-groups]`.** The more modern place for development dependencies and
fully supported by uv. Rejected because `pip` does not understand them: a fallback to
`pip install -e ".[test]"` would silently install without the test tooling. The loss of
compatibility outweighs the elegance.

**`hatchling` as the build backend.** Cleaner for `src/` layouts, but the house convention is
setuptools, and for a package with a single `packages.find` entry there is no substantive
difference.

**Not committing `uv.lock`.** That would invalidate the entire rationale of this ADR.
