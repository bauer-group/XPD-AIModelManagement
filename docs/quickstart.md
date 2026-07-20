# Quickstart

Goal: a verified backup of a real Hugging Face model repository in a local MinIO, and a
restore of it, in under five minutes.

Everything here uses `hf-internal-testing/tiny-random-gpt2`. It is a genuine public model
repository, it is a few hundred kilobytes, and it contains both LFS and non-LFS files — so
it exercises both halves of the integrity model without costing bandwidth.

## 1. Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install bg-ai-model-management
```

## 2. Start a MinIO

```bash
docker run -d --name minio -p 9000:9000 \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  minio/minio server /data
```

This is a scratch instance for a walkthrough. Do not reuse root credentials against
anything you care about — see [Backends](backends.md) for the least-privilege policy the
tool actually needs.

## 3. Configure

Credentials are **never** command-line flags. They would land in shell history and in `ps`
output. They come from the environment, from a secret file, or from a profile that
interpolates an environment variable.

```bash
export AIMM_S3__ENDPOINT_URL=http://localhost:9000
export AIMM_S3__BUCKET=hf-backup
export AIMM_S3__ACCESS_KEY_ID=minioadmin
export AIMM_S3__SECRET_ACCESS_KEY=minioadmin
```

Setting `AIMM_S3__ENDPOINT_URL` selects path-style addressing automatically, which is what
self-hosted MinIO and Ceph RGW want.

Optionally set a Hugging Face token. It is not needed for public repositories, but it
raises your rate limits considerably and is required for gated ones:

```bash
export HF_TOKEN=<your-hugging-face-token>
```

Note that `HF_TOKEN` has no `AIMM_` prefix — it is Hugging Face's own variable and `aimm`
reads it under its real name.

## 4. Check before you move bytes

```bash
aimm hf-backup doctor --preset minio --ensure-bucket
```

`doctor` probes the object store, checks Hugging Face authentication, checks that the
staging directory is writable and how much space it has, and prints the fully resolved
settings **with every secret masked**. This is the output to paste into a bug report.

`--ensure-bucket` creates the bucket if it is missing. It is off by default and should stay
off outside a scratch environment; a production object store is not a place for implicit
`CreateBucket` calls.

If a check fails, `doctor` still reports every other check and then exits `2`.

## 5. Back it up

```bash
aimm hf-backup sync hf-internal-testing/tiny-random-gpt2 --preset minio
```

What happened, in order:

1. `main` was resolved to an immutable 40-character commit SHA.
2. The file tree was enumerated **against that SHA**, not against `main`.
3. Each file was routed to `inline`, `stream` or `disk` by size and configuration.
4. Each upload was followed by a `head_object` that asserts the stored byte count.
5. `manifest.json` was written **only because every file succeeded**, then its digest file,
   then `refs/main.json`.

Try `--dry-run` first if you want the plan without moving anything.

To back up a dataset instead of a model, either prefix the specification or pass the type:

```bash
aimm hf-backup sync datasets/hf-internal-testing/fixtures_ade20k --preset minio
aimm hf-backup sync hf-internal-testing/fixtures_ade20k --repo-type datasets --preset minio
```

## 6. See what you have

```bash
aimm hf-backup catalog list --preset minio
aimm hf-backup catalog revisions hf-internal-testing/tiny-random-gpt2 --preset minio
aimm hf-backup catalog show hf-internal-testing/tiny-random-gpt2 --preset minio
```

`catalog show` verifies the manifest's own digest before printing it.

## 7. Prove it is intact

```bash
# cheap: HEAD per object, no data transfer at all
aimm hf-backup verify hf-internal-testing/tiny-random-gpt2 --preset minio

# expensive: read every object back and re-hash it
aimm hf-backup verify hf-internal-testing/tiny-random-gpt2 --preset minio --level deep
```

On a tiny repository `deep` is free. On a real estate it is a full-egress operation with a
real invoice attached — read the cost model in [Operations](operations.md) before you point
it at a hundred terabytes.

The exit code is the result:

| Exit | Meaning |
| --- | --- |
| `0` | clean |
| `20` | drift, or the revision is incomplete (no manifest) |
| `6` | corrupt — stored bytes do not match the manifest |

## 8. Restore

```bash
aimm hf-backup restore hf-internal-testing/tiny-random-gpt2 \
  --dest ./restored --preset minio
```

`restore` reads the manifest, verifies its digest, then for each file streams the object to
`./restored/<path>.aimm-part` while hashing it, checks the digest, `fsync`s and atomically
renames it into place. A digest mismatch removes the part file and fails with exit `6`
rather than leaving a plausible-looking bad file on disk.

**`restore` never contacts Hugging Face.** That is the whole point: the backup keeps working
after the upstream repository is deleted, renamed or gated.

Use `--verify-only` to run the same read-and-hash pass without writing anything.

## 9. Make it repeatable

Once it works, move the settings out of your shell and into a profile so a scheduled run
does not depend on someone's `.bashrc`:

```yaml
# aimm.yaml
default_backend: minio

backends:
  minio:
    preset: minio
    endpoint_url: http://localhost:9000
    region: us-east-1
    bucket: hf-backup
    prefix: aimm
    access_key_id: ${MINIO_ACCESS_KEY}
    secret_access_key: ${MINIO_SECRET_KEY}

transfer:
  mode: auto
  workers: 8
  part_size: 8MiB
```

`aimm` discovers `./aimm.yaml` automatically, so this now works:

```bash
aimm hf-backup sync hf-internal-testing/tiny-random-gpt2
```

A missing `${MINIO_ACCESS_KEY}` is a hard error, not an empty string — otherwise the tool
would quietly try to authenticate with a blank credential.

Next: [Configuration](configuration.md) for the full precedence chain and every variable,
and [Operations](operations.md) for scheduling and retention.
