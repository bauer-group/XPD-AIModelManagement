# ADR 0004 — Container and distribution

- Status: **superseded by [ADR 0006](0006-pypi-distribution.md)**
- Date: 2026-07-20
- Superseded: 2026-07-20
- Affects: `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `.github/workflows/`

> **This ADR no longer describes the project.** The owner redirected the deliverable from a
> container image to a PyPI wheel. There is no `Dockerfile`, no GHCR image and no
> `docker-compose.yml` at the repository root; the only compose file is the integration test
> rig. The reasoning below is preserved unchanged — including the parts that were correct
> and are now simply irrelevant — because an ADR that quietly rewrites its own history is
> worthless. See [ADR 0006](0006-pypi-distribution.md) for what replaced it and why.
>
> One decision from this ADR did survive the reversal: the replacement of the broken
> `release.yml`. It is now carried by `python-automatic-release.yml` rather than by a Docker
> release workflow.

## Context

`aimm` is published as a multi-arch image (`linux/amd64`, `linux/arm64`) to GHCR. The house
convention is a multi-stage build `builder → test → prod` on `python:3.14-alpine`, non-root
uid/gid 1000, `tini` as PID 1, a full OCI label block, and a *functional* healthcheck.

The critical technical question was whether the compiled dependencies ship musl wheels for
both architectures — otherwise a Rust source build under QEMU would be forced and the base
image would have to move to `-slim`.

Verified:

- `pydantic-core` and `PyYAML` ship `musllinux` wheels for x86_64 and aarch64;
  `pydantic-core` even ships explicit `cp314` wheels.
- `hf-xet` 1.5.2 ships `musllinux_1_2` wheels for **both** architectures.
- `huggingface_hub`, `boto3`, `typer` and `rich` are pure Python (`py3-none-any`).
- **Trap on glibc bases:** `hf-xet`'s aarch64 wheel is `manylinux_2_28` (glibc ≥ 2.28), while
  its x86_64 wheel is `manylinux_2_17`. An arm64 base image that is too old demonstrably
  downgrades **silently** to `hf-xet==0.1.2` instead of failing.
- The existing `release.yml` is non-functional: it validates
  `services/dozzle/docker-compose.yml`, a leftover from a foreign repository. The file does
  not exist, `docker compose config` fails, the step ends with exit 1, and the `release` job
  is permanently **skipped** through `needs`. Releases have therefore never happened since
  the repository began, without a single red run ever appearing.

## Decision

**Keep `python:3.14-alpine` as the base for all three stages. Multi-arch build without QEMU
source builds. `uv` only in `builder`/`test`, never in the production image. The existing
`release.yml` is replaced by `docker-release.yml`.**

- `builder`: `build-base`/`libffi-dev` permitted, installs into `/install`.
- `test`: installs the test extras and runs `pytest`.
- `prod`: `COPY --from=builder /install /usr/local`, plus an artefact gate copying from the
  `test` stage — the production image cannot be built without green tests.
- `uv` arrives via `COPY --from=ghcr.io/astral-sh/uv:<pinned tag>` and stays in the build
  stages. `uv sync --locked` (not `--frozen`), so that a stale lockfile **breaks** the build
  instead of being silently re-resolved.
- Non-root uid/gid 1000; cache and staging directories are created and chowned **before** the
  `USER` instruction.
- `ENTRYPOINT ["/sbin/tini", "--", "aimm"]`, with `ca-certificates` and `tzdata` installed.
- `HF_HUB_DOWNLOAD_TIMEOUT` and `HF_HUB_ETAG_TIMEOUT` are set as `ENV` in the Dockerfile, not
  from Python.
- The build **asserts** the resolved `hf-xet` version rather than trusting it.
- CI: the in-house `docker-build.yml` with `platforms: linux/amd64,linux/arm64`,
  `generate-sbom: true`, SHA-tagged third-party actions, `bauer-group/*` modules on `@main`,
  an explicit `permissions:` block and `timeout-minutes` on every inline job.
- The Python lint/typecheck/test job is written **inline** with `astral-sh/setup-uv`, not via
  `python-build.yml`.

## Consequences

### Positive

- No compiler and no `uv` in the production image: smaller attack surface, smaller image.
- No QEMU in the build. All dependencies are pre-built, so both architectures build natively
  or without emulation. QEMU is particularly expensive for Python builds when bytecode
  compilation runs.
- Alpine/musl avoids the `manylinux_2_28` trap entirely: for musl there are
  `musllinux_1_2` wheels for both architectures, so the asymmetric glibc requirement does not
  exist at all.
- The artefact gate in the production stage makes "image built" and "tests green" the same
  statement.
- `uv sync --locked` plus a committed lockfile means: when the daily base-image monitor
  rebuilds the image because of a security patch, the Python dependencies do **not** change.
  For a tool whose purpose is to still be able to restore a model in three years, that is the
  decisive point.
- Replacing `release.yml` fixes a state in which releases silently never happened.

### Negative

- **musl is slower than glibc under heavily concurrent I/O.** musl's `malloc` is measurably
  weaker under many threads, and DNS resolution behaves differently. For a tool running 8
  workers against one S3 endpoint that is a real, if probably network-dominated,
  disadvantage. **If throughput becomes the problem, `python:3.14-slim-trixie` is the first
  lever** — but then with an explicit check of the `hf-xet` version on arm64.
- **Any future dependency without a musl wheel becomes a source build.** That is a latent
  trap: it does not bite today, it bites when package number 12 is added. Mitigation: a CI
  check repeating the cross-resolution for both architectures binary-only.
- **`uv sync --locked` breaks the build** when a Dependabot PR raises `pyproject.toml`
  without regenerating the lockfile. That is intended, but it creates friction and must be
  accounted for in the Dependabot configuration.
- **The Python CI job is hand-built**, because `python-build.yml` does not support `uv`: its
  `package-manager` knows only pip/poetry/pipenv/conda, its cache key does not hash
  `uv.lock`, and its ruff invocation ends in `|| true` and therefore never finds an error.
  This one job thus deviates from the house rule "prefer reusable workflows". Everything else
  — Docker build, hadolint, security scan, code quality, semantic release — continues to run
  through the in-house modules.
- Non-root plus bind mounts means UID conflicts on native Linux. Docker Desktop on Windows
  and macOS papers over this, so the bug appears only for Linux users. `docker-compose.yml`
  therefore uses a **named volume** for the cache and documents `user: "${UID}:${GID}"` as
  the escape route for bind mounts.

### Neutral

- No `version:` key in `docker-compose.yml` (obsolete, produces only a warning).
- Interactive invocation is `docker compose run --rm aimm <subcommand>`.

## Alternatives rejected

**`python:3.14-slim-trixie` (Debian/glibc).** Better thread and allocator performance and
broader wheel coverage. Rejected because the house convention is Alpine, musl wheel
availability was verified for all current dependencies, and `hf-xet`'s asymmetric
`manylinux_2_28` requirement on arm64 is a real, silently failing trap that does not exist
with musl. It is the first documented lever if throughput becomes a problem.

**Distroless (`gcr.io/distroless/python3-debian13`).** Smallest attack surface, but the
bundled CPython minor version is not controllable and not documented as stable — for a tool
targeting 3.14 that is disqualifying.

**QEMU emulation for arm64.** Not needed, since all wheels are pre-built, and catastrophically
slow for bytecode compilation.

**Cross-install with `uv --python-platform aarch64-unknown-linux-musl` from an amd64
builder.** Works today (verified), but uv cannot execute the target interpreter — any future
dependency with a build hook or without a wheel fails. Native runners are simpler and
Docker's own recommendation.

**`python-build.yml` for the Python CI job.** Unusable: no `uv` branch in the install
dispatch, `coverage-fail-under` defaulting to `false` despite a configured threshold,
`run-type-check` off by default, `type-check-args` defaulting to `--ignore-missing-imports`
— and the ruff step cannot fail because of `|| true`. A CI job that cannot fail is not a CI
job.

**Bundling `hf_transfer` into the image.** Removed in `huggingface_hub` 1.x; the corresponding
environment variable is ignored. The accelerator is `hf-xet` and it is installed
automatically.

**Cosign instead of GitHub attestations.** Both would work.
`actions/attest-build-provenance` needs only `id-token: write` and `attestations: write`, is
checkable with `gh attestation verify`, and is the route taken by the in-house
`docker-build.yml`. Keyless cosign remains possible as a third layer if consumers want to
verify through Rekor/Fulcio.
