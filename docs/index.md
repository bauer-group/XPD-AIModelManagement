# AI Model Management Toolkit

`aimm` is BAUER GROUP's toolkit for AI model development and operations. It is built as a
swiss-army knife: a tool-agnostic core plus tools that mount on top of it, so tool #2 costs
a subpackage and one line of packaging metadata rather than a new project.

Tool #1 is `aimm hf-backup` — Hugging Face **or ModelScope** repositories into
S3-compatible storage, with
verification, restore and retention.

## The three names

| Thing | Value |
| --- | --- |
| Distribution name | `bg-ai-model-management` |
| Import package | `bg_ai_model_management` |
| CLI command | `aimm` |

Four more identifiers are public contract and are keyed to the CLI name, not the import
package: the entry-point group `aimm.tools`, the environment prefix `AIMM_`, the profile
filenames `aimm.yaml` / `aimm.yml`, and the default S3 key prefix `aimm`.

## What the toolkit promises

- **A snapshot means something.** Backups are pinned to an immutable commit SHA before
  enumeration begins, so the file list and the bytes always describe the same commit.
- **Completeness is structural.** `manifest.json` is written only when every file
  succeeded. Its presence *is* the completeness marker; there is no status database to go
  stale.
- **Integrity is provable.** Digests are compared against the hub's own values where the
  hub publishes them, and the limits of that are stated plainly in
  [Integrity](integrity.md).
- **Restore is self-contained.** `restore` and `verify --level quick|deep` never contact
  Hugging Face.
- **Deleting is hard on purpose.** `prune` needs an explicit policy *and* `--yes`, and
  refuses plans that would leave nothing behind.
- **Exit codes are stable.** `0` clean, `20` drift, `6` corrupt. That is the scheduling
  interface.

## Where to go next

| If you want to | Read |
| --- | --- |
| Install the tool | [Installation](installation.md) |
| Get a first backup working | [Quickstart](quickstart.md) |
| Write a profile, set env vars | [Configuration](configuration.md) |
| Look up a flag | [CLI reference](cli.md) |
| Understand inline / stream / disk | [Transfer strategy](transfer-strategy.md) |
| Know exactly what is verified | [Integrity](integrity.md) |
| Configure MinIO, Ceph, AWS, R2, Wasabi | [Backends](backends.md) |
| Schedule, monitor, prune, recover | [Operations](operations.md) |
| Decode an exit code or an error | [Troubleshooting](troubleshooting.md) |
| Hack on the code or add tool #2 | [Development](development.md) |
| Understand why it is built this way | [Architecture](architecture.md) and the ADRs |

## Non-goals

Stated once, so nobody waits for them:

- **No built-in scheduler.** `aimm` is a process with deterministic exit codes and a
  machine-readable `--json` document. Cron, systemd timers and Kubernetes CronJobs call it.
- **No plugin framework.** The extension point is a setuptools entry point returning a
  `typer.Typer`.
- **No client-side encryption.** SSE-S3 and SSE-KMS are passed through; encrypting before
  upload is out of scope for v1.
- **No upload back to the Hub.** `aimm hf-backup` is one-way: Hub to S3 to local disk.
- **No bucket provisioning.** `--ensure-bucket` exists but is off by default.
