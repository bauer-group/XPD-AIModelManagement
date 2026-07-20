# Configuration

## Precedence

Highest wins:

```text
CLI flag  >  environment variable  >  secret file  >  profile YAML  >  built-in default
```

The rule that makes this work is narrow and worth stating: a command sends **only the
options the user actually typed** down to the settings layer. An untouched flag never
reaches it, so an unset `--workers` cannot silently beat `transfer.workers` in your
profile. Click records where every parameter's value came from, and options sourced from
the default are dropped.

## The profile file

`aimm` looks for a profile in this order and stops at the first hit:

1. `--profile <path>`
2. `$AIMM_PROFILE`
3. `./aimm.yaml`
4. `./aimm.yml`
5. `$XDG_CONFIG_HOME/aimm/config.yaml` on POSIX, `%APPDATA%/aimm/config.yaml` on Windows

Nothing found is not an error — environment variables and defaults alone are a valid
configuration. A `--profile` that does not exist **is** an error.

```yaml
# aimm.yaml
default_backend: minio

backends:
  minio:
    preset: minio
    endpoint_url: https://eu-north1.s3.bauer-group.com
    region: eu-north1
    bucket: hf-backup
    prefix: aimm
    access_key_id: ${MINIO_ACCESS_KEY}
    secret_access_key: ${MINIO_SECRET_KEY}
  aws:
    preset: aws
    region: eu-central-1
    bucket: bauer-hf-archive

transfer:
  mode: auto
  workers: 8
  part_size: 8MiB
  inline_max: 8MiB
  staging_dir: /var/lib/aimm/staging
  disk_reserve: 5GiB

hub:
  endpoint: https://huggingface.co

log_level: INFO
log_format: text
```

Unknown keys are rejected. Every settings model is `extra="forbid"`, so a typo such as
`buckets:` fails loudly at load time instead of being silently ignored while the tool
writes to the wrong place.

### Selecting a backend

The `backends` mapping lets one profile describe several object stores. Which one is used
is resolved in this order:

1. `--backend <name>` / `-B <name>`
2. `$AIMM_BACKEND`
3. the profile's `default_backend`
4. the single backend, if exactly one is defined
5. the top-level `s3:` block, if there is no `backends` mapping at all

The selected backend is flattened into `s3`, so `--bucket` and `AIMM_S3__BUCKET` still
override it.

### Environment interpolation

`${VAR}` and `${VAR:-default}` are expanded in strings anywhere in the profile. `$$` is a
literal `$`.

**A referenced variable that is unset and has no default is a hard error.** Substituting an
empty string would make the tool authenticate with a blank credential and then report a
confusing permission failure; failing at load time is the honest behaviour.

### Size values

`part_size`, `inline_max`, `max_part_memory`, `disk_reserve` and `max_disk_bytes` accept
either a plain integer number of bytes or a human string: `8MiB`, `5GiB`, `512K`, `1024`.

`part_size` is validated to be at least 5 MiB and at most 5 GiB — those are S3's own
limits, not ours. `inline_max` is validated to 0..5 GiB for the same reason: the inline path
is a single `PutObject`, which S3 rejects above 5 GiB, and `0` disables it outright.
`workers` is validated to 1..64.

## Credentials

**Credentials are never CLI flags.** There is no `--access-key`, and there will not be one:
a flag lands in shell history and is visible in `ps` output to every user on the box.

Three supported sources, in precedence order:

| Source | How |
| --- | --- |
| Environment | `AIMM_S3__ACCESS_KEY_ID`, `AIMM_S3__SECRET_ACCESS_KEY`, `AIMM_S3__SESSION_TOKEN`, `HF_TOKEN` |
| Secret file | a file under `/run/secrets` named `AIMM_s3__secret_access_key` |
| Profile | `access_key_id: ${MINIO_ACCESS_KEY}` — interpolated from the environment |

If no S3 credentials are configured at all, boto3's own credential chain applies
(`AWS_ACCESS_KEY_ID`, instance metadata, `~/.aws/credentials`, and so on). If no `HF_TOKEN`
is set, `huggingface_hub`'s stored login token is used, and failing that the Hub is accessed
anonymously.

The secret-file name looks odd and is deliberate: the environment prefix applies to secret
filenames too, and nested fields need the `__` delimiter. A file named `s3__secret_key` is
**silently ignored** and the field stays unset.

Secrets are held as `SecretStr`, so they render as `**********` in reprs and in
`--json` output, and a redaction filter additionally masks credential-shaped text in every
log line before it reaches a handler.

## Environment variables

Two different mechanisms populate this table, which is why it is longer than the list of
flags. Some names are read by the CLI parser as a flag's `envvar`; the rest are resolved
by the settings layer from the prefix `AIMM_` plus the nested delimiter `__`, with no flag
involved at all.

### Global

| Variable | Effect | Default |
| --- | --- | --- |
| `AIMM_PROFILE` | profile path | — |
| `AIMM_BACKEND` | backend name within the profile | — |
| `AIMM_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` |
| `AIMM_LOG_FORMAT` | `text` or `json` | `text` |
| `AIMM_RUN_ID` | correlation id for the run | generated |
| `AIMM_REPO_TYPE` | default `--repo-type` | `models` |
| `NO_COLOR` | disable coloured output | — |

### Object store

| Variable | Maps to | Default |
| --- | --- | --- |
| `AIMM_S3__PRESET` | `s3.preset` | `auto` |
| `AIMM_S3__ENDPOINT_URL` | `s3.endpoint_url` | — |
| `AIMM_S3__REGION` | `s3.region` | `us-east-1` |
| `AIMM_S3__BUCKET` | `s3.bucket` | required |
| `AIMM_S3__PREFIX` | `s3.prefix` | `aimm` |
| `AIMM_S3__ACCESS_KEY_ID` | `s3.access_key_id` | boto3 chain |
| `AIMM_S3__SECRET_ACCESS_KEY` | `s3.secret_access_key` | boto3 chain |
| `AIMM_S3__SESSION_TOKEN` | `s3.session_token` | — |
| `AIMM_S3__ADDRESSING_STYLE` | `s3.addressing_style` | `auto` |
| `AIMM_S3__CHECKSUM_CALCULATION` | `s3.checksum_calculation` | `auto` |
| `AIMM_S3__STORAGE_CLASS` | `s3.storage_class` | unset — the server decides |
| `AIMM_S3__SERVER_SIDE_ENCRYPTION` | `s3.server_side_encryption` | — |
| `AIMM_S3__SSE_KMS_KEY_ID` | `s3.sse_kms_key_id` | — |
| `AIMM_S3__ENSURE_BUCKET` | `s3.ensure_bucket` | `false` |
| `AIMM_S3__VERIFY_TLS` | `s3.verify_tls` | `true` |
| `AIMM_S3__PROBE` | `s3.probe` | `true` |

`s3.max_attempts`, `s3.connect_timeout`, `s3.read_timeout` and `s3.ca_bundle` follow the
same `AIMM_S3__*` pattern and default to `10`, `15.0`, `120.0` and unset.

### Transfer

| Variable | Maps to | Default |
| --- | --- | --- |
| `AIMM_TRANSFER__MODE` | `transfer.mode` | `auto` |
| `AIMM_TRANSFER__WORKERS` | `transfer.workers` | `8` |
| `AIMM_TRANSFER__PART_SIZE` | `transfer.part_size` | `8MiB` |
| `AIMM_TRANSFER__INLINE_MAX` | `transfer.inline_max` | `8MiB` |
| `AIMM_TRANSFER__MAX_PART_MEMORY` | `transfer.max_part_memory` | `64MiB` |
| `AIMM_TRANSFER__STAGING_DIR` | `transfer.staging_dir` | system temp |
| `AIMM_TRANSFER__MAX_DISK_BYTES` | `transfer.max_disk_bytes` | derived from free space |
| `AIMM_TRANSFER__DISK_RESERVE` | `transfer.disk_reserve` | `5GiB` |
| `AIMM_TRANSFER__PREFER_XET` | `transfer.prefer_xet` | `false` |

`transfer.stream_failure_downgrade`, `transfer.max_attempts`, `transfer.max_wait` and
`transfer.fail_fast` follow the same pattern and default to `2`, `5`, `60.0` and `false`.

### Hugging Face

| Variable | Maps to | Default |
| --- | --- | --- |
| `AIMM_HUB__ENDPOINT` | `hub.endpoint` | `https://huggingface.co` |
| `AIMM_HUB__CHUNK_SIZE` | `hub.chunk_size` | `1MiB` |
| `AIMM_HUB__CONNECT_TIMEOUT` | `hub.connect_timeout` | `15.0` seconds |
| `AIMM_HUB__READ_TIMEOUT` | `hub.read_timeout` | `120.0` seconds |
| `HF_TOKEN` | `hub.token` — **no `AIMM_` prefix** | stored login token |

`hub.connect_timeout` and `hub.read_timeout` are passed explicitly on every streaming
request. They are not decoration: `huggingface_hub` builds its shared client with
`timeout=None`, so without them a stalled CDN connection blocks a worker thread forever —
no bytes, no exception, no way for the run to fail. `HF_HUB_DOWNLOAD_TIMEOUT` below does
**not** cover this; it only reaches the Hub library's own downloader.

Two Hugging Face timeouts are seeded before the Hub library is imported, because it reads
its entire environment at import time and setting them later has no effect:

| Variable | Seeded to | Overridable |
| --- | --- | --- |
| `HF_HUB_DOWNLOAD_TIMEOUT` | `60` | yes — a value you set is never overwritten |
| `HF_HUB_ETAG_TIMEOUT` | `30` | yes |

### Integration test rig

`AIMM_IT_*` is a third, unrelated namespace used only by the integration test suite. It is
not part of the settings model. See [Development](development.md).

## Output and logging

| Flag | Effect |
| --- | --- |
| `--log-format text` | human output through a shared `rich` console (default) |
| `--log-format json` | one JSON object per log record, for Loki or ELK |
| `--json` | one machine-readable result document on **stdout**, nothing else |
| `--run-id` | correlation id that appears in every log record, the manifest and the JSON report |

All log output goes to **stderr**, which is what makes `--json` safe to pipe:

```bash
aimm hf-backup verify meta-llama/Llama-3-8B --json | jq -r .status
```

Third-party loggers (`botocore`, `boto3`, `urllib3`, `s3transfer`, `httpx`, `httpcore`,
`huggingface_hub`, `filelock`, `fsspec`) are damped to `WARNING` so `--log-level DEBUG`
stays readable.

## Inspecting the resolved configuration

```bash
aimm hf-backup doctor
```

`doctor` prints the fully resolved settings with every secret masked, plus the outcome of
each probe. It is the intended first step of any bug report.
