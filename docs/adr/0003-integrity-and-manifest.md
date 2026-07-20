# ADR 0003 — Integrity model and manifest

- Status: accepted
- Date: 2026-07-20
- Affects: `bg_ai_model_management.tools.hfbackup.manifest`, `.keys`, `.engine`, `bg_ai_model_management.integrity.hashing`

## Context

A backup whose correctness cannot be demonstrated is not a backup. The original design
compared only file sizes on resume and did not pin the revision to a commit — two holes
through which silent corruption fits.

The hard constraints, all verified:

- **There is no whole-object sha256 for multipart objects.** For multipart, S3 carries
  SHA-256 only as a *composite* value (sha256 over the concatenated part digests). True
  whole-object checksums exist only for CRC algorithms, because only those linearise.
- **The multipart ETag is MD5-over-MD5s with a `-N` suffix** and therefore depends on the
  part size. Under SSE-KMS or SSE-C the ETag is not an MD5 even for a single PUT.
- **`ChecksumSHA256` is absent from `head_object`** unless `ChecksumMode="ENABLED"` is
  passed — otherwise it silently returns `None`, which a naive verifier reads as "no checksum
  stored" and re-uploads everything.
- **`list_repo_tree(recursive=True)` supplies `size`, `blob_id`, `lfs.sha256` and
  `xet_hash`** without an extra flag, and it is paginated.
- **`lfs` is `None` for small non-LFS files.** There is no content sha256 from Hugging Face
  there, only the git blob id.
- **User metadata is capped at 2 KB, is lowercased, and is immutable after upload.**
- `repo_info(revision="main").sha` returns the immutable 40-character SHA.

## Decision

**One JSON manifest per (repository, revision) is the integrity authority. It sits beside the
data in S3, is protected by its own digest file, and its mere existence marks the snapshot as
complete.**

Four levels of checking, each with its own authority:

| Level | Comparison | Source of truth |
| --- | --- | --- |
| Origin (LFS) | computed sha256 == `RepoFile.lfs.sha256` | Hugging Face |
| Origin (non-LFS) | `sha1("blob <len>\0" + content)` == `RepoFile.blob_id` | Hugging Face |
| Transport | `head_object.ContentLength` == expected size, after **every** upload | S3 |
| At rest | `sha256(GET object)` == `manifest.files[].sha256` | the manifest |

**Pin before enumerating.** `repo_info(...).sha` is resolved once and then passed into
*every* `list_repo_tree` and `hf_hub_url` call. The full 40-character SHA is part of the S3
key.

**Resume compares hash, size and ETag** — never size alone.

**Key layout:**

```text
<prefix>/v1/<repo_type>/<owner>/<name>/revisions/<sha>/manifest.json
<prefix>/v1/<repo_type>/<owner>/<name>/revisions/<sha>/manifest.json.sha256
<prefix>/v1/<repo_type>/<owner>/<name>/revisions/<sha>/files/<path/in/repo>
<prefix>/v1/<repo_type>/<owner>/<name>/refs/<ref>.json
```

**Manifest completeness invariant:** `manifest.json` is written only when *every* file has
been transferred and verified successfully. There is no intermediate state and no status
database.

## Consequences

### Positive

- **`restore` and `verify` work without Hugging Face.** The backup survives deletion,
  renaming or gating of the source repository.
- The SHA in the key makes snapshots immutable: re-syncing the same revision is a no-op, and
  a moved `main` produces a new prefix rather than overwriting the old one. Retention
  therefore becomes prefix selection.
- No more torn snapshots: enumeration and transfer are guaranteed to see the same commit.
- Because `s3_part_size` and `s3_parts` are in the manifest, the multipart ETag is
  recomputable — drift is detectable without data transfer.
- `sha256_source` distinguishes "confirmed by Hugging Face" (`hf-lfs`) from "only observed by
  us" (`computed`). Without that field the two cases would look identical, even though one is
  an end-to-end proof and the other is not.
- The completeness invariant replaces a state database with the existence of an object. A
  crashed run is automatically marked correctly as incomplete.

### Negative

- **Non-LFS files have no content sha256 originating upstream.** The git blob id is SHA-1,
  not SHA-256, and SHA-1 is no longer dependable against deliberate collisions. For
  kilobyte-scale configuration files this is an accepted residual risk; the heavy weight
  files are without exception LFS or Xet based and therefore sha256-protected.
- **`verify --level deep` costs full egress.** Deep-verifying a 120 TB estate is an invoice,
  not a button press. That is why `quick` is the default and `deep` must be requested
  explicitly, sensibly as a sample via `--sample-percent`.
- **The manifest is a single point of truth.** If it is lost, the data is still there but no
  longer verifiable. Mitigation: `manifest.json.sha256` beside it, and
  `verify --level upstream`, which can re-check the manifest against Hugging Face for as long
  as the repository exists.
- **The completeness invariant means there is no partial success.** If one file of 5 000
  fails there is no manifest, and the next run must pick up the remaining file. That is
  intentional — a manifest with gaps would be worse than none.
- One additional object per revision (`manifest.json.sha256`) doubles the number of small
  objects at revision level. At two objects per revision that is irrelevant.

### Neutral

- The sha256 is additionally stored as the object metadata key `aimm-sha256`. Redundant to
  the manifest, but useful for third-party tools and for the case where someone checks
  individual objects outside `aimm`. At 64 hex characters the 2 KB limit is not a concern.
- Metadata keys are lowercased by S3. The schema therefore uses lowercase throughout, so that
  round-trip comparisons do not fail.

## Alternatives rejected

**Compare ETags only.** Tempting, because it is free from `head_object`. But the multipart
ETag depends on the part size, is not an MD5 at all under SSE-KMS or SSE-C, MD5 is not an
integrity proof, and above all: the ETag confirms only that S3 stored what it received — not
that what it received corresponds to the repository. The ETag is retained as a cheap drift
indicator in `verify --level quick`.

**S3 object metadata only, no manifest.** Fails on three counts: the 2 KB limit, immutability
after upload, and the absence of any place for repository-level information (commit SHA,
selection filters, totals). `restore` would also have to call `head_object` for every object
individually instead of reading one object.

**Server-side CRC64NVME as a whole-object checksum.** Technically the only real whole-object
option for multipart. Rejected because Hugging Face supplies sha256 and not CRC — a CRC
comparison could never check against the origin, only against ourselves. Support across
MinIO, Ceph, R2 and Wasabi is also unverified.

**Composite ETag as the primary check.** Requires exact knowledge of the part boundaries.
Since we roll multipart ourselves we would even know them — but the check would be against
our own partitioning, not against the content. It remains a secondary indicator.

**A state database (SQLite) for progress and resume.** One more piece of state that can
become corrupt, go stale, or be lost between runs. The manifest in S3 does the same job, sits
with the data, and outlives the process.

**Short seven-character commit hashes in the key.** They are resolved server-side by the Hub
(verified), but they are unsuitable as a persistent identifier: they can become ambiguous as
history grows, and a backup catalogue must not be ambiguous.
