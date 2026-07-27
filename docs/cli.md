# CLI reference

Every flag on this page exists in the shipped command line. When this page and `--help`
disagree, `--help` is right and this page is the bug — the CLI is executable and prose is
not.

## Root

```text
aimm [OPTIONS] COMMAND [ARGS]...
```

| Option | Type | Default | Environment |
| --- | --- | --- | --- |
| `--profile` | path | discovered | `AIMM_PROFILE` |
| `--log-level` | `DEBUG`\|`INFO`\|`WARNING`\|`ERROR` | `INFO` | `AIMM_LOG_LEVEL` |
| `--log-format` | `text`\|`json` | `text` | `AIMM_LOG_FORMAT` |
| `--json` | flag | off | — |
| `--run-id` | text | generated | `AIMM_RUN_ID` |
| `--no-color` | flag | off | `NO_COLOR` |
| `--version`, `-V` | flag | — | — |

Global options go **before** the tool name:

```bash
aimm --json --log-format json hf-backup sync owner/name
```

### `aimm tools`

Lists the mounted tools, one per line, as `name  summary`. A tool that fails to import is
logged as a warning at startup and omitted here rather than breaking the CLI.

## Shared backend options

These appear on `sync`, `verify`, `restore`, `prune`, every `catalog` subcommand and
`doctor`. All enum options are case-insensitive.

| Option | Type | Default | Environment |
| --- | --- | --- | --- |
| `--backend`, `-B` | text | profile `default_backend` | `AIMM_BACKEND` |
| `--endpoint-url` | text | — | `AIMM_S3__ENDPOINT_URL` |
| `--bucket`, `-b` | text | from profile (required if absent) | `AIMM_S3__BUCKET` |
| `--prefix` | text | `aimm` | `AIMM_S3__PREFIX` |
| `--region` | text | `us-east-1` | `AIMM_S3__REGION` |
| `--preset` | `auto`\|`minio`\|`ceph-rgw`\|`aws`\|`r2`\|`wasabi` | `auto` | `AIMM_S3__PRESET` |
| `--addressing` | `auto`\|`path`\|`virtual` | `auto` | `AIMM_S3__ADDRESSING_STYLE` |
| `--checksum` | `auto`\|`when_supported`\|`when_required` | `auto` | `AIMM_S3__CHECKSUM_CALCULATION` |
| `--storage-class` | text | unset | `AIMM_S3__STORAGE_CLASS` |
| `--sse` | `AES256`\|`aws:kms` | unset | `AIMM_S3__SERVER_SIDE_ENCRYPTION` |
| `--sse-kms-key-id` | text | unset | `AIMM_S3__SSE_KMS_KEY_ID` |
| `--no-verify-tls` | flag | off | — (set `AIMM_S3__VERIFY_TLS=false`) |
| `--ensure-bucket` | flag | **off** | `AIMM_S3__ENSURE_BUCKET` |
| `--no-probe` | flag | off | — (set `AIMM_S3__PROBE=false`) |

`--no-verify-tls` and `--no-probe` are negations of a setting, so they carry no `envvar` of
their own; use the setting variable instead.

There is no credential flag anywhere in this CLI. See
[Configuration](configuration.md#credentials).

## Repository specifications

Wherever a repository is accepted:

```text
owner/name                     # type from --repo-type, revision from --revision
datasets/owner/name            # explicit type prefix, overrides --repo-type
models/owner/name              # explicit type prefix
owner/name@main                # explicit revision
owner/name@a1b2c3d4...         # explicit 40-hex commit SHA
```

## `aimm hf-backup sync`

```text
aimm hf-backup sync [OPTIONS] [REPOS]...
```

Back up one or more repositories at a pinned commit.

| Option | Type | Default | Environment |
| --- | --- | --- | --- |
| `--source` | `huggingface`\|`modelscope` | `huggingface` | `AIMM_SOURCE` |
| `--repo-type` | `models`\|`datasets` | `models` | `AIMM_REPO_TYPE` |
| `--revision` | text | `main` | — |
| `--from-file` | path | — | — |
| `--include` | text, repeatable | `*` | — |
| `--exclude` | text, repeatable | — | — |
| `--mode` | `auto`\|`stream`\|`disk` | `auto` | `AIMM_TRANSFER__MODE` |
| `--workers` | int 1..64 | `8` | `AIMM_TRANSFER__WORKERS` |
| `--part-size` | size | `8MiB` | `AIMM_TRANSFER__PART_SIZE` |
| `--inline-max` | size | `8MiB` | — |
| `--max-part-memory` | size | `64MiB` | — |
| `--staging-dir` | path | system temp | — |
| `--disk-reserve` | size | `5GiB` | — |
| `--max-disk` | size | derived from free space | — |
| `--prefer-xet` / `--no-prefer-xet` | flag | off | — |
| `--recheck` | `none`\|`head`\|`deep` | `head` | — |
| `--update-ref` / `--no-update-ref` | flag | on | — |
| `--dry-run` | flag | off | — |
| `--fail-fast` | flag | off | — |

Plus every [shared backend option](#shared-backend-options).

`--from-file` reads one specification per line; `#` starts a comment. It combines with
positional arguments rather than replacing them. Passing neither is an error.

`--exclude` wins over `--include`.

`--recheck` controls how hard an already-stored file is re-checked before it is skipped.
It is never size-only: the manifest entry must agree with upstream on digest *and* size
first, and then:

| Value | Additional check | Cost |
| --- | --- | --- |
| `none` | nothing is skipped; everything is re-transferred | full ingress |
| `head` | `head_object` must match size and ETag | one HEAD per file |
| `deep` | the stored object is read back and re-hashed | full egress |

`--dry-run` plans and reports without transferring a byte or writing anything.

Exits `8` if any file failed. Without a complete run there is no manifest, so a partially
failed sync is never recorded as a success.

```bash
aimm hf-backup sync meta-llama/Llama-3-8B openai/whisper-large-v3
aimm hf-backup sync --from-file repos.txt --workers 16 --mode stream
aimm hf-backup sync owner/name --include '*.safetensors' --exclude '*.onnx'
aimm hf-backup sync owner/name@v1.0 --dry-run
```

## `aimm hf-backup verify`

```text
aimm hf-backup verify [OPTIONS] REPO
```

Check a stored revision against its manifest.

| Option | Type | Default |
| --- | --- | --- |
| `--source` | `huggingface`\|`modelscope` | `huggingface` |
| `--repo-type` | `models`\|`datasets` | `models` |
| `--revision` | text — ref name or 40-hex SHA | `main` |
| `--level` | `quick`\|`deep`\|`upstream` | `quick` |
| `--sample-percent` | float, greater than 0, at most 100 | `100.0` |
| `--workers` | int 1..64 | `8` |
| `--strict` / `--no-strict` | flag | strict |

Plus every [shared backend option](#shared-backend-options).

| Level | What it does | Transfer cost |
| --- | --- | --- |
| `quick` | `head_object` per file; compares size and ETag against the manifest | none |
| `deep` | additionally reads every object back and re-computes sha256 | **full egress** |
| `upstream` | `deep`, plus re-fetches the Hub tree at the pinned SHA and compares | full egress, plus Hub calls |

> **`--level deep` on a large estate is an invoice, not a button.** It reads every stored
> byte back out of the object store. On a 120 TB estate that is 120 TB of egress, billed
> per gigabyte on a cloud provider and saturating the link on a self-hosted one.
> `--sample-percent` is the affordable alternative: `--sample-percent 2` deep-checks a
> deterministic 2% sample. The sample is seeded on the commit SHA, so the same revision
> always yields the same sample — repeated runs do not creep towards full coverage on
> their own. See [Operations](operations.md#verification-strategy) for a schedule that
> gets real coverage without a real invoice.

The exit code carries the verdict:

| Status | Exit | Meaning |
| --- | --- | --- |
| `ok` | `0` | no findings |
| `drift` | `20` | size or ETag mismatch, or an object is missing |
| `incomplete` | `20` | there is no manifest — the revision was never completed |
| `corrupt` | `6` | a stored digest mismatched, or upstream disagrees with the manifest |

`--no-strict` suppresses the `20` for drift and incomplete, so the command exits `0` and
the finding is only in the report. `corrupt` still exits `6` regardless — that one is not
suppressible.

```bash
aimm hf-backup verify meta-llama/Llama-3-8B
aimm hf-backup verify meta-llama/Llama-3-8B --level deep --sample-percent 2
aimm hf-backup verify meta-llama/Llama-3-8B --level upstream --json
```

## `aimm hf-backup restore`

```text
aimm hf-backup restore [OPTIONS] REPO --dest DIRECTORY
```

Materialise a stored revision on local disk. **Never contacts Hugging Face** — including
when resolving `--revision`. A name is looked up in `refs/<ref>.json` and nothing else; when
no such ref was stored (a sync pinned by SHA, or one run with `--no-update-ref`) the command
fails with exit `2` and lists the commit SHAs that *are* stored, rather than pinning against
an upstream repository that may no longer exist.

| Option | Type | Default |
| --- | --- | --- |
| `--dest` | path, directory | **required** |
| `--repo-type` | `models`\|`datasets` | `models` |
| `--revision` | text — ref name or 40-hex SHA | `main` |
| `--include` | text, repeatable | `*` |
| `--exclude` | text, repeatable | — |
| `--workers` | int 1..64 | `8` |
| `--overwrite` / `--no-overwrite` | flag | off — an existing file is an error |
| `--verify-only` | flag | off |

Plus every [shared backend option](#shared-backend-options).

Each file is streamed to `<path>.<token>.aimm-part` while being hashed, checked against the
manifest digest, `fsync`ed and atomically renamed. A mismatch — or any failure mid-stream —
removes the part file and fails; a half-written file is never left looking finished, and no
part file is left behind in `--dest`. The random token matters: a repository may legitimately
contain both `X` and `X.aimm-part`, and a name derived from the target alone would put two
workers on the same path.

Every path from the manifest is re-validated before it is joined to `--dest`, so a
maliciously crafted repository cannot write outside the destination directory.

```bash
aimm hf-backup restore meta-llama/Llama-3-8B --dest /srv/models/llama3
aimm hf-backup restore owner/name --dest ./out --include '*.json' --overwrite
aimm hf-backup restore owner/name --dest ./out --verify-only
```

## `aimm hf-backup prune`

```text
aimm hf-backup prune [OPTIONS] [REPOS]...
```

Delete revisions the retention policy no longer covers.

| Option | Type | Default |
| --- | --- | --- |
| `--repo-type` | `models`\|`datasets` | `models` |
| `--all-repos` | flag | off |
| `--keep-last` | int, at least 1 | — |
| `--keep-within` | duration — `30m`, `12h`, `90d`, `2w` | — |
| `--keep-incomplete` / `--no-keep-incomplete` | flag | off |
| `--abort-older-than` | duration | `24h` |
| `--yes`, `-y` | flag | off |

Plus every [shared backend option](#shared-backend-options).

Three safety properties, all enforced in code rather than by convention:

1. **At least one of `--keep-last` / `--keep-within` is required.** An unconstrained prune
   exits `9` without touching anything.
2. **Without `--yes` nothing changes.** The plan is printed, no object is deleted and no
   multipart upload is aborted. The command still exits `0`.
3. **Pass repositories or `--all-repos`, not both and not neither.** Either is a usage
   error, exit `2`.

Beyond that, the planner keeps the newest complete revision whatever the policy says,
protects any revision a ref points at, and refuses a plan that would leave nothing behind.
See [Operations](operations.md#retention).

`--abort-older-than` cleans up multipart uploads orphaned by earlier crashes. Those consume
storage on MinIO indefinitely and appear in no object listing.

```bash
aimm hf-backup prune meta-llama/Llama-3-8B --keep-last 3
aimm hf-backup prune --all-repos --keep-last 2 --keep-within 90d          # plan only
aimm hf-backup prune --all-repos --keep-last 2 --keep-within 90d --yes    # apply
```

## `aimm hf-backup catalog`

Read-only inventory. These commands talk only to the object store; a ref name is resolved
from `refs/<ref>.json`, never from the Hub, so browsing a backup needs no Hub credentials
and no network access to Hugging Face.

### `catalog list`

```text
aimm hf-backup catalog list [OPTIONS]
```

| Option | Type | Default |
| --- | --- | --- |
| `--repo-type` | `models`\|`datasets` | both |
| `--owner` | text | all owners |

Plus every [shared backend option](#shared-backend-options).

### `catalog revisions`

```text
aimm hf-backup catalog revisions [OPTIONS] REPO
```

| Option | Type | Default |
| --- | --- | --- |
| `--repo-type` | `models`\|`datasets` | `models` |

Plus every [shared backend option](#shared-backend-options). Marks each revision complete
or incomplete and shows which refs point at it.

### `catalog show`

```text
aimm hf-backup catalog show [OPTIONS] REPO
```

| Option | Type | Default |
| --- | --- | --- |
| `--repo-type` | `models`\|`datasets` | `models` |
| `--revision` | text — ref name or 40-hex SHA | `main` |

Plus every [shared backend option](#shared-backend-options). Verifies the manifest's own
digest before printing it; a manifest that fails its digest check exits `6`.

```bash
aimm hf-backup catalog list --owner meta-llama
aimm hf-backup catalog revisions meta-llama/Llama-3-8B
aimm hf-backup catalog show meta-llama/Llama-3-8B --revision v1.0 --json
```

## `aimm hf-backup doctor`

```text
aimm hf-backup doctor [OPTIONS]
```

Takes the [shared backend options](#shared-backend-options) plus `--source`
(`huggingface` by default, `AIMM_SOURCE`). Runs four checks and prints the resolved
settings with every secret masked:

| Check | What it reports |
| --- | --- |
| settings | which profile was used, if any |
| object store | bucket reachable; probed addressing, checksum mode, sha256 and `GetObjectAttributes` support |
| hugging face *or* modelscope | Hugging Face: the authenticated user, or that the session is anonymous. ModelScope: that the endpoint answers and whether a token is configured — it never claims an identity |
| staging dir | writable, and how much free space is available |

Every check is reported even when an earlier one failed; the command then exits `2` if any
check failed. This is the output to attach to a bug report.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | success |
| `1` | unexpected internal error |
| `2` | usage or configuration error |
| `3` | authentication or authorisation failure |
| `4` | upstream source error (Hugging Face or ModelScope) |
| `5` | object store error |
| `6` | **integrity failure** |
| `7` | insufficient disk space for staging |
| `8` | transfer failed after retries |
| `9` | retention refused by a safety guard |
| `20` | **differences found — a finding, not a crash** |
| `130` | interrupted — Ctrl-C, or SIGTERM from a container stop; in-flight uploads are aborted first |

Full diagnosis per code: [Troubleshooting](troubleshooting.md).

## JSON output

`--json` writes exactly one document to stdout and nothing else. Every logline, table and
progress bar goes to stderr, so the stream is always safe to pipe.

```bash
aimm --json hf-backup verify owner/name | jq -r '.status'
aimm --json hf-backup catalog list | jq -r '.repos[] | "\(.repo_id) \(.revisions)"'
aimm --json hf-backup prune --all-repos --keep-last 3 | jq '.totals'
```

Each document carries `command` and `run_id`; the rest is command-specific.
