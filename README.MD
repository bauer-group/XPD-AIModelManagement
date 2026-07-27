<!-- AUTO-GENERATED FILE. DO NOT EDIT. Edit docs/README.template.MD instead. -->
<!-- Generated on 2026-07-27 12:07:12 UTC -->

# BAUER GROUP - AI Model Management Toolkit

[![PyPI](https://img.shields.io/pypi/v/bg-ai-model-management.svg)](https://pypi.org/project/bg-ai-model-management/)
[![Python](https://img.shields.io/pypi/pyversions/bg-ai-model-management.svg)](https://pypi.org/project/bg-ai-model-management/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://github.com/bauer-group/XPD-AIModelManagement/actions/workflows/tests.yml/badge.svg)](https://github.com/bauer-group/XPD-AIModelManagement/actions/workflows/tests.yml)

> **Today, Tomorrow, Together** | Building Better Software Together

A swiss-army knife for AI model development and operations. `aimm` is a **toolkit**, not
a single script: one core provides configuration precedence, secret redaction, retry
classification, hashing and a stable exit-code contract, and each tool mounts on top of it
as a Typer sub-app.

Tool #1 is **`aimm hf-backup`**: it backs up repositories from **Hugging Face or
ModelScope** to S3-compatible storage and can prove afterwards that the bytes are still
the right bytes. Pick the hub with `--source`; everything after the download — pinning,
integrity, the key layout, `verify`, `restore`, `prune` — is identical for both.

## The three names

This trips people up, so it is stated up front. They are deliberately different and none
of them is a typo:

| Thing | Value |
| --- | --- |
| Distribution name (what you `pip install`) | `bg-ai-model-management` |
| Import package (what you `import`) | `bg_ai_model_management` |
| CLI command (what you type) | `aimm` |

The CLI name is also the plugin contract: tools mount through the setuptools entry-point
group **`aimm.tools`**, every environment variable is prefixed **`AIMM_`**, and the profile
file is **`aimm.yaml`**. Those five identifiers are public API and do not change.

## What `hf-backup` does

- **Pins before it reads.** A revision is resolved to an immutable 40-character commit SHA
  *once*, and every subsequent enumeration and transfer runs against that SHA. A push to
  `main` mid-run cannot produce a torn snapshot.
- **Three transfer paths, chosen per file.** `inline` (a single `PutObject` from RAM),
  `stream` (Hugging Face straight into a hand-rolled multipart upload, no disk at all) and
  `disk` (staged download, for the cases where streaming is not safe). `--mode` forces
  either extreme.
- **Integrity you can check, not assume.** LFS files are anchored on Hugging Face's own
  `sha256`; non-LFS files on the git blob id. Both go into a per-revision manifest that is
  itself digest-protected.
- **Restore never contacts Hugging Face.** The manifest is the only authority, so the
  backup survives the upstream repo being deleted, renamed or gated.
- **Retention that refuses to do the stupid thing.** The newest complete revision is always
  kept, a revision a ref points at is protected, and a plan with no survivors is refused
  outright.
- **Exit codes are a scheduling interface.** `0` clean, `20` drift found, `6` corrupt. See
  below — this is the part that matters for cron.
- **Backend-agnostic.** MinIO, Ceph RGW, AWS S3, Cloudflare R2 and Wasabi are equal-ranked
  presets, and a runtime capability probe beats the preset table.

## Install

```bash
pip install bg-ai-model-management
```

Python 3.12 or newer. See [`docs/installation.md`](docs/installation.md).

## 60-second quickstart against MinIO

Start a MinIO, point `aimm` at it, back a real model up, verify it, restore it.

```bash
# 1. A throwaway MinIO on localhost:9000
docker run -d --name minio -p 9000:9000 \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  minio/minio server /data

# 2. Credentials come from the environment, never from a flag
export AIMM_S3__ACCESS_KEY_ID=minioadmin
export AIMM_S3__SECRET_ACCESS_KEY=minioadmin
export AIMM_S3__ENDPOINT_URL=http://localhost:9000
export AIMM_S3__BUCKET=hf-backup

# 3. Check that everything is reachable before moving bytes
aimm hf-backup doctor --preset minio --ensure-bucket

# 4. Back up a real (tiny) model repository
aimm hf-backup sync hf-internal-testing/tiny-random-gpt2 --preset minio

# 5. Prove it landed intact
aimm hf-backup verify hf-internal-testing/tiny-random-gpt2 --preset minio --level deep

# 6. Bring it back to local disk — this step never talks to Hugging Face
aimm hf-backup restore hf-internal-testing/tiny-random-gpt2 \
  --dest ./restored --preset minio
```

`--ensure-bucket` is opt-in on purpose and is only appropriate for a scratch bucket. In
production, buckets are provisioned as infrastructure; the tool should not be able to
create them.

Longer walkthrough: [`docs/quickstart.md`](docs/quickstart.md).

## Commands

Global options apply to every command: `--profile`, `--log-level`, `--log-format`,
`--json`, `--run-id`, `--no-color`, `--version`.

| Command | What it does |
| --- | --- |
| `aimm tools` | List the mounted tools |
| `aimm hf-backup sync` | Back up one or more repositories at a pinned commit |
| `aimm hf-backup verify` | Check a stored revision against its manifest (`quick`, `deep`, `upstream`) |
| `aimm hf-backup restore` | Materialise a stored revision on local disk |
| `aimm hf-backup prune` | Delete revisions the retention policy no longer covers |
| `aimm hf-backup catalog list` | List repositories stored under the prefix |
| `aimm hf-backup catalog revisions` | List the stored revisions of one repository |
| `aimm hf-backup catalog show` | Show one manifest, after verifying its digest |
| `aimm hf-backup doctor` | Probe the environment; print resolved settings with secrets masked |

Every flag is documented in [`docs/cli.md`](docs/cli.md).

## Exit codes

Every command returns a stable exit code. Three of them carry the operational meaning:

| Code | Meaning |
| --- | --- |
| `0` | success — nothing to report |
| `20` | **differences found.** `verify` did its job and found drift or an incomplete revision. This is a finding, not a crash |
| `6` | **integrity failure.** Stored bytes do not hash to what the manifest says. Escalate |

A monitoring job that treats "non-zero" as one bucket will page someone at 03:00 for a
`20` that only needed a re-sync, and will under-react to a `6` that means data loss. The
full table is in [`docs/troubleshooting.md`](docs/troubleshooting.md).

## Adding tool #2

The extension seam is a setuptools entry point that yields a `typer.Typer`. That is the
whole mechanism — no registry, no base class, no plugin framework:

```toml
[project.entry-points."aimm.tools"]
hf-backup = "bg_ai_model_management.tools.hfbackup.cli:app"
my-tool   = "my_package.cli:app"
```

The loader mounts every registered entry point at startup, and a tool that fails to import
is logged and skipped rather than taking `aimm --help` down with it. A tool can live in a
**different distribution package** entirely and still mount. See
[`docs/development.md`](docs/development.md).

## Documentation

| Document | Contents |
| --- | --- |
| [Installation](docs/installation.md) | supported Pythons, venv advice, verifying the install |
| [Quickstart](docs/quickstart.md) | first successful `sync` in under five minutes |
| [Configuration](docs/configuration.md) | profile YAML, backends, every environment variable, the precedence chain |
| [CLI reference](docs/cli.md) | every command, every flag, every exit code |
| [Transfer strategy](docs/transfer-strategy.md) | inline / stream / disk, how the path is chosen, tuning |
| [Integrity](docs/integrity.md) | what is verified against what, and the honest limits |
| [Backends](docs/backends.md) | MinIO, Ceph RGW, AWS, R2, Wasabi — presets and probes |
| [Operations](docs/operations.md) | scheduling, retention, disaster recovery, monitoring |
| [Troubleshooting](docs/troubleshooting.md) | symptom to cause to fix, keyed on exit codes |
| [Development](docs/development.md) | dev setup, the MinIO rig, how to add tool #2 |
| [Architecture](docs/architecture.md) | module map, data flows, concurrency, security model |
| [Decision records](docs/adr/0001-cli-and-package-architecture.md) | ADRs 0001-0006, including the reversals |

## License

MIT © BAUER GROUP
