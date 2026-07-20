# ADR 0006 — PyPI distribution and dependency bounds instead of a lockfile

- Status: accepted
- Date: 2026-07-20
- Supersedes: [ADR 0004](0004-container-and-distribution.md), [ADR 0005](0005-dependency-management-with-uv.md)
- Affects: `pyproject.toml`, `.github/workflows/`, the whole distribution story

## Context

ADRs 0004 and 0005 were written against a different deliverable. At that point `aimm` was to
ship as a multi-arch container image on GHCR, and the packaging decisions followed from that
premise:

- a multi-stage Alpine build with an artefact gate,
- `uv` with a committed `uv.lock`, enforced by `uv sync --locked`,
- setuptools as the build backend,
- development dependencies under a `test` extra.

**The owner then redirected the project from a container image to a PyPI package.** That
single change invalidates the load-bearing argument of both ADRs, and it is worth being
precise about *why*, because the earlier reasoning was not wrong.

ADR 0005's case for a lockfile was entirely specific: a base-image monitor rebuilt the image
automatically whenever the base digest moved, so an unattended *security patch* rebuild could
silently pull a new `huggingface_hub` or `boto3` major into an artefact whose purpose is to
restore a model in three years. `huggingface_hub` 1.0 swapping `requests` for `httpx` is
exactly that scenario. Against a container image that argument holds completely, and the
decision to commit a lockfile was correct for that deliverable.

It does not survive the change of deliverable, for a structural reason rather than a matter
of taste: **a wheel ships version ranges and can never ship a lockfile.** There is no
automatic rebuild to defend against, because there is no image to rebuild. There is no
artefact whose transitive closure we control, because the artefact is installed into someone
else's environment by their resolver, alongside their other packages. A lockfile in a
published library or application distribution is not merely unnecessary — it is inexpressible
in the packaging format.

Two further constraints came with the redirect: the house PyPI template
(`CoolifyMigration`) uses **hatchling**, and the house naming is a `bg-`-prefixed
distribution name with an underscored import package.

## Decision

**Ship a PyPI wheel built by hatchling. Reproducibility comes from both-bounded dependency
ranges in `pyproject.toml`. There is no container image, no lockfile and no uv.**

### Packaging

- Build backend **hatchling** (`requires = ["hatchling"]`,
  `build-backend = "hatchling.build"`). Not setuptools, not poetry.
- `name = "bg-ai-model-management"`, `dynamic = ["version"]`, with
  `[tool.hatch.version] path = "src/bg_ai_model_management/__init__.py"` reading
  `__version__`. There is no static `project.version`.
- Import package `bg_ai_model_management` under `src/`. The CLI command stays **`aimm`**, as
  do the entry-point group `aimm.tools`, the environment prefix `AIMM_`, the profile
  filenames `aimm.yaml` / `aimm.yml` and the default S3 key prefix `aimm`. Those are public
  contracts with users and third-party tools; the rename is module paths only.
- `requires-python = ">=3.12"`; lint and type-check run against 3.12, the floor, while
  releases run on 3.14.
- Extras: **`dev`** (pytest, coverage, moto, ruff, mypy, type stubs, build, twine,
  pre-commit, python-semantic-release) and **`docs`**. There is no `test` extra.

### Dependency bounds

Every runtime dependency carries **both** bounds:

| Bound | Purpose |
| --- | --- |
| Lower (`>=`) | the version actually **verified** during design. Raising it is always safe; guessing lower is not |
| Upper (`<`) | the next **major**, where the vendor is allowed to break us. This buys what a lockfile would have bought, without freezing our consumers |

Two upper bounds are tighter than "next major", both deliberately: `httpx` is capped below
1.0 because it is pre-1.0 and the *minor* is its breaking axis (and `huggingface_hub` 1.x
independently requires it), and `rich` is capped below 17 to admit the verified-adjacent 16.x
and no further.

Developer tools (`ruff`, `mypy`, `types-boto3`, `build`, `twine`, `pre-commit`,
`python-semantic-release`) carry **no** upper bound: they are never imported by shipped code,
and capping them only creates dependency-bot noise.

### Release automation

- python-semantic-release, configured in `pyproject.toml`, computes the version from
  Conventional Commits, rewrites `__version__`, regenerates `CHANGELOG.md`, tags, and creates
  the GitHub release.
- Publication to PyPI uses **trusted publishing** (OIDC), so no long-lived PyPI token exists
  anywhere.
- The broken `release.yml` identified in ADR 0004 is deleted. It validated a
  `docker-compose.yml` from an unrelated repository and gated the release job on its output,
  so releases were permanently skipped **and no run ever went red**. It is replaced by
  `python-automatic-release.yml`.
- `.github/config/release/semantic-release.json` is deleted: it configures the *JavaScript*
  semantic-release and conflicts with `[tool.semantic_release]`. Two competing release
  configurations is a trap.

## Consequences

### Positive

- **Consumption is trivial.** `pip install bg-ai-model-management` or
  `pipx install bg-ai-model-management`, on any platform with Python 3.12 or newer. No
  registry authentication, no image pull, no container runtime.
- **No base-image CVE treadmill.** The largest maintenance cost of the container path was
  keeping a base image patched and rebuilt. That cost is gone entirely, along with the daily
  rebuild that motivated the lockfile in the first place.
- **The musl versus glibc trade-off disappears.** ADR 0004 accepted measurably weaker
  allocator behaviour under concurrency in exchange for avoiding `hf-xet`'s asymmetric
  `manylinux_2_28` requirement on arm64. On PyPI the user's own interpreter and platform
  select the correct wheel, so neither problem exists.
- **Version is single-sourced.** `dynamic = ["version"]` plus `version_variables` means
  semantic-release rewrites exactly one variable and there is nothing to keep in sync.
- **The extension seam is stronger, not weaker.** Entry points work across distributions, so
  a third party can publish their own `aimm.tools` tool to PyPI and it mounts on install.
  With an image, a third-party tool would have required rebuilding our image.
- **CI is simpler.** The in-house reusable workflows support pip directly, so the hand-built
  inline CI job that ADR 0005 was forced into — the one that deviated from the house rule
  because `python-build.yml` did not understand uv — is no longer necessary.

### Negative

- **We give up the pinned transitive closure.** This is the real cost, and it is the exact
  thing ADR 0005 was protecting. A user installing today and a user installing in eighteen
  months get different transitive versions, and we cannot make that not be true. What
  mitigates it:
  - upper bounds at the next major, so a breaking release cannot be pulled in silently;
  - lower bounds at versions actually verified rather than guessed;
  - a CI matrix across 3.12, 3.13 and 3.14 that resolves fresh on every run, so an
    incompatible release inside our ranges surfaces as a red build rather than as a user's
    bug report.

  It does **not** give the guarantee an image digest or a lockfile gave. Anyone who needs
  that guarantee for an archival deployment should pin `bg-ai-model-management==<version>`
  together with a `pip freeze` of their own environment, or install into a container of their
  own making. That is now the consumer's decision, which is the honest place for it.
- **We no longer control the runtime environment.** The image guaranteed a known interpreter,
  a known OpenSSL, known CA certificates and known locale settings. Now the tool must be
  robust across whatever Python the user brings — hence the 3.12/3.13/3.14 test matrix and
  keeping Windows a first-class development target.
- **The artefact gate is gone.** ADR 0004's production stage could not be built without green
  tests, which made "image built" and "tests green" the same statement. A wheel has no such
  structural gate; it is enforced by the release workflow instead, which is a weaker
  guarantee because a workflow can be edited.
- **Two superseded ADRs remain in the tree.** Deliberately. Deleting them would hide a
  reversal, and a decision record that hides its reversals is worse than none — the next
  person to propose a container image needs to be able to read why one was chosen, and why
  it stopped applying.

### Neutral

- The only compose file left in the repository is the integration test rig under
  `tests/integration/`. It is a test fixture, not a distribution mechanism.
- `HF_HUB_DOWNLOAD_TIMEOUT` and `HF_HUB_ETAG_TIMEOUT` were set as Dockerfile `ENV` entries
  under ADR 0004. They are now seeded in `main.py` before any `huggingface_hub` import, with
  `setdefault` semantics so an operator's own value is never overwritten. The constraint
  behind them is unchanged: the Hub library reads its entire environment at import time.

## Alternatives rejected

**Publish both a wheel and an image.** Twice the release surface, twice the security
scanning, and two artefacts that can disagree about their dependency versions. If a container
is genuinely wanted later, the correct shape is a thin image that simply
`pip install`s the published wheel — which anyone can build in five lines without us
maintaining it.

**Keep uv for development, without a lockfile.** Faster resolution locally, but it adds a
tool to the chain in exchange for convenience alone, once its actual justification has
evaporated. Plain `pip install -e ".[dev]"` works everywhere and matches the house PyPI
template.

**Keep the lockfile for CI reproducibility only.** Tempting, and it would make CI runs
deterministic. Rejected because it makes CI test a dependency set that **no user will ever
have**. The value of the matrix is precisely that it resolves fresh and therefore discovers
an incompatible upstream release before a user does. Determinism here would be the enemy of
the thing we actually want to detect.

**Pin exact versions (`==`) in `[project.dependencies]`.** Technically expressible in a
wheel, and it would give reproducibility. Rejected because it makes the package
uninstallable alongside almost anything else: any consumer with their own `boto3` requirement
gets an unresolvable conflict. A published distribution that cannot be co-installed is not a
published distribution.

**setuptools instead of hatchling.** ADR 0005 preferred setuptools on house-convention
grounds. The house PyPI template uses hatchling, `dynamic = ["version"]` reading
`__version__` is cleaner under hatch than under setuptools for a `src/` layout, and the
template is the binding reference for a PyPI tool. The convention that applied was the
container convention, and it no longer applies.
