# Storage backends

MinIO, Ceph RGW, AWS S3, Cloudflare R2 and Wasabi are equal-ranked. A preset supplies
*defaults*; a runtime probe supplies the *truth*.

## Presets

| Preset | Addressing | Checksum calculation | Storage class |
| --- | --- | --- | --- |
| `minio` | `path` | `when_required` | only `STANDARD`, `REDUCED_REDUNDANCY` |
| `ceph-rgw` | `path` | `when_required` | leave unset |
| `aws` | `virtual` | `when_supported` | full AWS set |
| `r2` | `virtual` | `when_supported` | leave unset |
| `wasabi` | `virtual` | `when_required` | leave unset |
| `auto` (default) | `path` when `endpoint_url` is set, otherwise `virtual` | from the preset, then the probe | from the preset |

Set it with `--preset` or `AIMM_S3__PRESET`. Anything you set explicitly beats both the
preset and the probe.

## Addressing style

The `auto` rule is: **a custom endpoint means self-hosted, and self-hosted means
path-style.** Cloud providers get virtual-host addressing.

```text
https://endpoint/bucket/key      # path-style
https://bucket.endpoint/key      # virtual-host style
```

Override with `--addressing path|virtual` when your deployment does not match the rule —
for example a MinIO behind a wildcard-DNS reverse proxy that genuinely wants virtual-host
addressing.

## The capability probe

At startup (and in `doctor`) `aimm` writes a tiny object to `<prefix>/v1/_probe/<uuid>` and
deletes it immediately, to discover what the backend actually supports:

| Discovered | Used for |
| --- | --- |
| `addressing_style` | confirms the resolved addressing works |
| `request_checksum_calculation` | falls back from `when_supported` to `when_required` if the first fails |
| `supports_sha256_checksum` | whether `ChecksumSHA256` can be stored |
| `supports_get_object_attributes` | whether part-level attributes can be read, or `head_object` must be used |

Disable it with `--no-probe` or `AIMM_S3__PROBE=false` if your IAM policy forbids the write,
but then the preset table is the only information available and a mismatch surfaces as a
mid-run failure instead of a startup one.

### Why probe rather than tabulate

Whether MinIO, R2 or Wasabi accept botocore's `aws-chunked` checksum **trailer** is not
verified, and it cannot be settled by a table because it also depends on the transport:
over an `http://` endpoint botocore sends a pre-computed header, over `https://` it sends a
trailer. A bug that appears against production MinIO over TLS does **not** reproduce against
a plain-HTTP development MinIO. A green test against plain HTTP is therefore not evidence of
trailer compatibility.

## Storage class

By default no `StorageClass` is sent at all and the server decides. This is not laziness:
MinIO's validator accepts only `STANDARD` and `REDUCED_REDUNDANCY`, so a generic
`--storage-class STANDARD_IA` fails outright there.

Set `--storage-class` only when you know the target accepts the value.

## Server-side encryption

```bash
aimm hf-backup sync owner/name --sse AES256
aimm hf-backup sync owner/name --sse aws:kms --sse-kms-key-id <key-id>
```

`aimm` passes SSE settings through and does no client-side encryption. Note that under
SSE-KMS the ETag stops being an MD5 even for single-PUT objects — the manifest sha256
remains the authoritative check either way, so integrity verification is unaffected. See
[Integrity](integrity.md).

## Least-privilege IAM policy

These are the actions the tool actually uses. Nothing here is aspirational.

Object level, on `arn:aws:s3:::<bucket>/*`:

| Action | Used by |
| --- | --- |
| `s3:PutObject` | uploads, manifest and ref writes, the probe object |
| `s3:GetObject` | verify, restore, manifest reads |
| `s3:DeleteObject` | `prune`, probe cleanup, removing an object that failed its checksum |
| `s3:AbortMultipartUpload` | the mandatory abort on every multipart failure path |
| `s3:ListMultipartUploadParts` | `prune --abort-older-than` |

Bucket level, on `arn:aws:s3:::<bucket>`:

| Action | Used by |
| --- | --- |
| `s3:ListBucket` | catalog walking, key listing |
| `s3:GetBucketLocation` | client bootstrap and `doctor` |
| `s3:ListBucketMultipartUploads` | `prune --abort-older-than` |

Deliberately **not** required:

- `s3:CreateBucket` — `--ensure-bucket` is off by default. Withholding the permission means
  the default cannot quietly drift; if someone flips it, the failure is loud.
- `s3:PutBucketPolicy`, or anything else bucket-administrative.
- `s3:GetObjectAttributes` — support on MinIO is unverified, and the probe already treats
  it as a discovered capability. Absence means "probe says no", not "failure".

If you want a read-only auditing credential, `verify --level quick|deep`, `restore` and all
`catalog` commands need only `s3:GetObject`, `s3:ListBucket` and `s3:GetBucketLocation`.

## Per-backend notes

### MinIO

```yaml
backends:
  minio:
    preset: minio
    endpoint_url: https://s3.example.com
    region: eu-north1
    bucket: hf-backup
    access_key_id: ${MINIO_ACCESS_KEY}
    secret_access_key: ${MINIO_SECRET_KEY}
```

- Path-style addressing; `region` is a label MinIO echoes back, matching
  `MINIO_REGION_NAME`.
- Do not set `--storage-class` to anything other than `STANDARD` or `REDUCED_REDUNDANCY`.
- Use a policy-scoped service account, never the root credentials.
- Orphaned multipart uploads consume storage permanently and are invisible to
  `ListObjectsV2`. Schedule `prune --abort-older-than`.

### Ceph RGW

As MinIO: path-style, `when_required` checksums, and leave the storage class unset. If the
gateway sits behind a load balancer, raise `s3.read_timeout` before raising the retry count
— a timeout mid-multipart costs a whole part.

### AWS S3

```yaml
backends:
  aws:
    preset: aws
    region: eu-central-1
    bucket: bauer-hf-archive
```

- No `endpoint_url`, so virtual-host addressing is selected automatically.
- Credentials may come from the standard boto3 chain (instance profile, `AWS_*`,
  `~/.aws/credentials`) — leave them out of the profile entirely.
- Lifecycle policies interact with `prune`: if a lifecycle rule transitions objects to a
  class that needs restoration before reading, `verify --level deep` and `restore` will
  fail on them. Keep the `aimm` prefix on a directly readable class, or accept that deep
  verification of archived revisions is not possible.

### Cloudflare R2

Virtual-host addressing, `when_supported` checksums, leave the storage class unset. R2 has
no egress charge, which changes the `verify --level deep` cost calculation considerably —
see [Operations](operations.md#verification-strategy).

### Wasabi

Virtual-host addressing but `when_required` checksums. Note Wasabi's minimum retention
billing period: `prune` deletes objects, but deleting early may not reduce the bill. Size
`--keep-within` against the billing minimum rather than against pure storage cost.

## Switching backends

Define several backends in one profile and select at call time:

```bash
aimm hf-backup sync owner/name --backend minio
aimm hf-backup sync owner/name --backend aws
```

Copying an existing backup between backends is not a built-in operation. Restore to local
disk and re-sync, or use your object store's own replication.

## Checking a backend

```bash
aimm hf-backup doctor --backend minio
```

Reports bucket reachability, the probed addressing style, the resolved checksum mode,
whether sha256 checksums and `GetObjectAttributes` are supported, and whether the probe
actually ran.
