# Troubleshooting

## Start here

```bash
aimm hf-backup doctor
```

`doctor` reports every probe even after one fails, then prints the fully resolved settings
with every secret masked. It is the intended first step of any bug report, and it is safe to
paste.

For more detail on any command:

```bash
aimm --log-level DEBUG hf-backup sync owner/name
```

## Exit codes

| Code | Meaning | Typical cause |
| --- | --- | --- |
| `0` | success | |
| `1` | unexpected internal error | a bug — please report it with the `run_id` |
| `2` | usage or configuration error | bad flag, missing bucket, invalid profile, unknown key |
| `3` | authentication or authorisation | wrong or missing credentials, insufficient IAM policy |
| `4` | Hugging Face source error | repository missing, gated, revision not found |
| `5` | object store error | bucket missing, endpoint unreachable, TLS failure |
| `6` | **integrity failure** | stored bytes do not match the manifest |
| `7` | insufficient disk space | staging budget cannot hold the file |
| `8` | transfer failed after retries | network, endpoint instability |
| `9` | retention refused | policy missing, or the plan would leave nothing |
| `20` | **differences found** | `verify` found drift, or the revision is incomplete |
| `130` | interrupted | SIGINT |

### `20` is a finding, not a crash

This is the one that gets misread most often. A `verify` that exits `20` did its job
correctly: it compared what is stored against the manifest and found a difference. The tool
is working; the *data* needs attention.

`6` is different in kind. It means a stored digest did not match, or upstream disagrees
with the manifest. Treat it as data loss until you have proven otherwise.

`--no-strict` makes `verify` exit `0` on drift and incomplete so the finding lives only in
the report. It does **not** suppress `6`.

## By symptom

### `hf-backup` is missing from `aimm tools`

The entry point failed to import. Run `aimm --log-level DEBUG tools` — the loader logs a
warning with the full traceback and skips the tool rather than taking the CLI down. Usually
a broken or partial install; reinstall with `pip install --force-reinstall
bg-ai-model-management`.

### `ConfigError: ... required` — bucket not set

No bucket in the profile, no `--bucket`, no `AIMM_S3__BUCKET`. Exit `2`.

### An unknown key in the profile is rejected

Every settings model forbids extra keys, so `buckets:` instead of `bucket:` fails at load
time. This is deliberate: silently ignoring a typo means writing to the wrong place.

### A profile setting is ignored

Almost always precedence. An environment variable beats the profile, and a **flag you
actually typed** beats both. A flag left at its default does not reach the settings layer at
all, so that is not the cause. Confirm with `aimm hf-backup doctor`, which prints what was
actually resolved.

### `ConfigError` about an unset variable during interpolation

`${VAR}` in the profile with no `VAR` in the environment and no `${VAR:-default}`. This is
intentionally fatal — substituting an empty string would make the tool authenticate with a
blank credential and then report a confusing permission error instead.

### A Docker secret file is ignored

Nested fields need the delimiter and the prefix in the filename. The file must be
`/run/secrets/AIMM_s3__secret_access_key`; a file named `s3__secret_key` is silently
ignored and the field stays unset.

### Exit `3` — authentication

For the object store, check that credentials are set (`AIMM_S3__ACCESS_KEY_ID` /
`AIMM_S3__SECRET_ACCESS_KEY`, a secret file, a profile, or the boto3 chain) and that the
policy grants the actions in [Backends](backends.md#least-privilege-iam-policy).

For Hugging Face, `doctor` reports whether the session is authenticated. Public repositories
work anonymously but at much lower rate limits; set `HF_TOKEN` (no `AIMM_` prefix).

### Exit `4` — repository is gated

The repository exists but requires accepting its licence first. Accept it on the Hugging
Face website with the same account as your token. This is reported distinctly from "not
found" on purpose — the two need completely different fixes, and conflating them sends
people looking for a typo that does not exist.

### Exit `4` — revision not found

`--revision` names a branch, tag or 40-hex SHA that does not exist. Check the spelling; note
that `catalog` commands resolve refs from `refs/<ref>.json` in the **backup**, so a ref that
exists upstream may simply never have been backed up.

### Exit `5` — bucket not found or endpoint unreachable

Check `--endpoint-url` and network reachability, then addressing style. A MinIO configured
for path-style but addressed virtual-host produces a confusing DNS or 404 error; try
`--addressing path`. `doctor` reports the probed addressing style.

For a TLS failure, set `s3.ca_bundle` to your internal CA rather than reaching for
`--no-verify-tls`, which disables verification entirely.

### Exit `6` — integrity failure

Something concrete is wrong:

| Finding kind | Meaning | Action |
| --- | --- | --- |
| `sha256` | a stored object does not hash to the manifest value | re-run `sync` for that revision; the object will be replaced |
| `upstream` | the manifest disagrees with Hugging Face at the pinned SHA | the manifest may be corrupt; re-sync the revision |
| manifest digest | `manifest.json` fails `manifest.json.sha256` | the manifest is corrupt; re-sync the revision |

Since keys include the commit SHA, re-syncing writes the same keys and repairs in place.
Use `--recheck none` to force re-transfer rather than allowing a skip.

### Exit `20` — revision is incomplete

There is no `manifest.json`, so a run was interrupted before the set was complete. Objects
under `files/` may exist. Re-run `sync` for that revision; only the missing files transfer
and the manifest is written once the set is complete.

If you want the debris gone instead, `prune` deletes incomplete revisions by default.

### Exit `20` — drift on size or ETag

A `head_object` disagreed with the manifest. Common causes: someone modified the object
outside `aimm`; a lifecycle policy transitioned it; or an SSE setting changed so the ETag is
no longer an MD5. Confirm with `--level deep`, which compares content rather than metadata —
if `deep` is clean, the content is fine and only the ETag expectation moved.

### Exit `7` — insufficient disk space

The `disk` path could not reserve space for a file. Either point `--staging-dir` at a larger
volume, lower `--disk-reserve`, raise `--max-disk`, or use `--mode stream` to avoid disk
entirely for files that can be streamed.

### Exit `8` — transfer failed after retries

The retry layer only retries transient failures, so this means repeated genuine failures.
Check endpoint stability and `s3.read_timeout` first; a timeout in the middle of a multipart
part costs the whole part. Lowering `--workers` helps when one endpoint is the bottleneck.

If it is one specific large file failing on the stream path, it will downgrade itself to
disk after two failures. `--mode disk` forces that immediately.

### `ObjectTooLargeError`

No admissible part size exists under the current limits. Raise `--max-part-memory`, or let
the file take the `disk` path. The part-size arithmetic is explained in
[Transfer strategy](transfer-strategy.md#part-size).

### Exit `9` — retention refused

Either no policy was given (pass `--keep-last` and/or `--keep-within`) or the plan would
have left nothing behind. The guard is not the problem; the policy is.

### Storage keeps growing after `prune`

Two likely reasons:

- **Orphaned multipart uploads.** They consume storage indefinitely and appear in no object
  listing. Run `prune --abort-older-than 24h --yes`.
- **Refs protect revisions.** Any revision a `refs/*.json` points at is never deleted. Check
  `catalog revisions` — the refs column shows which are pinned.

### Progress bars are missing or the output is noisy

The progress display is disabled without a TTY and when `CI` is set. That is intentional: a
degraded progress bar prints one line per refresh, which for thousands of files means
thousands of lines. Use `--log-format json` in automation.

### `--json` output has log lines mixed into it

It should not — logs go to stderr and stdout carries exactly one document. If you are seeing
them mixed, you are redirecting `2>&1` somewhere. Redirect the two streams separately.

### Unicode errors on Windows

Set `PYTHONIOENCODING=utf-8`. `rich` box-drawing characters do not survive cp1252.

## Reporting a bug

Include:

1. `aimm --version`
2. `aimm hf-backup doctor --json` (secrets are already masked)
3. The `run_id` of the failing run
4. The command as typed, with credentials removed
5. `aimm --log-level DEBUG ...` output for the failing command

Open it at
<https://github.com/bauer-group/XPD-AIModelManagement/issues>.
