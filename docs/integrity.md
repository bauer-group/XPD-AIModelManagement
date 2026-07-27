# Integrity

A backup whose correctness you cannot demonstrate is not a backup. This page states
exactly what is checked, against what authority, and — just as importantly — where that
authority stops.

## The split: sha256 for LFS, git blob id for the rest

This section describes **Hugging Face**, whose anchors are not uniform. ModelScope is
simpler and is covered in [On ModelScope](#on-modelscope) below.

Hugging Face does not publish one uniform content hash. What it publishes depends on how
git stores the file:

| File kind | Authoritative value | Where it comes from | Algorithm |
| --- | --- | --- | --- |
| LFS / Xet file | content `sha256` | `RepoFile.lfs.sha256` | SHA-256 |
| Non-LFS file | git blob id | `RepoFile.blob_id` | SHA-1 over `blob <len>\0` + content |

Both arrive free with the file listing — no extra request, no extra download.

For an **LFS file** this is an end-to-end proof. Hugging Face states the sha256; `aimm`
computes the sha256 of the bytes it actually received, in the same pass as the upload; the
two must agree. Neither the network nor the object store is trusted.

For a **non-LFS file** there is no content sha256 to compare against, because git never
computed one. What exists is the git blob id, and it is genuinely verifiable — `aimm`
recomputes `sha1(b"blob %d\0" % size + content)` in the same single pass and compares.

### Why the split is acceptable

SHA-1 is not collision-resistant against a determined attacker. That matters, and it is
the reason this is spelled out rather than buried:

- The files that anchor on SHA-1 are the small ones: `config.json`, `tokenizer.json`,
  `.gitattributes`, `README.md`. Kilobytes.
- Every heavy weight file — the ones where silent corruption would actually cost you a
  model — is LFS or Xet, and therefore anchored on SHA-256 from Hugging Face itself.
- SHA-1 remains perfectly sound against *accidental* corruption, which is the failure mode
  a backup tool is primarily defending against.

So the honest statement is: **weights are cryptographically anchored upstream; small
configuration files are anchored on git's own SHA-1.** That residual risk is accepted, and
is a deliberate consequence of what Hugging Face publishes rather than a shortcut.

### The trap this avoids

A naive implementation checks `blob_id` for every file. That fails on **every large file in
every repository**, because git does not store the LFS payload — it stores the LFS
*pointer* file, and `blob_id` is the SHA-1 of the pointer, not of the weights. Branching on
whether `lfs` is present is not cosmetic; it is required for correctness.

## What is recorded

Every file gets both digests plus a provenance marker, so a later reader can tell whether a
checksum was **confirmed by Hugging Face** or merely **observed by us**:

| Manifest field | Meaning |
| --- | --- |
| `sha256` | the digest `aimm` computed while transferring |
| `sha256_source` | which hub attested the digest — `hf-lfs`, `modelscope` — or `computed` when nothing upstream did |
| `blob_id` | the git blob id, recomputed and checked for non-LFS files |
| `xet_hash` | Hugging Face's Xet content hash, when present |
| `lfs` | whether the file is LFS-backed |
| `s3_etag` | the stored object's ETag, quotes stripped |
| `s3_part_size`, `s3_parts` | the multipart geometry, so the ETag stays recomputable |

Without `sha256_source` the two cases would look identical in the manifest, even though one
is an end-to-end proof and the other is not.

## The four checkpoints

| Checkpoint | Comparison | Authority |
| --- | --- | --- |
| Origin, LFS | computed sha256 == `lfs.sha256` | Hugging Face |
| Origin, non-LFS | recomputed blob id == `blob_id` | Hugging Face |
| Origin, any file | computed sha256 == the listed `Sha256` | ModelScope |
| Transport | `head_object` `ContentLength` == expected size, after **every** upload | S3 |
| At rest | `sha256(GET object)` == `manifest.sha256` | the manifest |

The transport check is what makes a truncated upload fail at backup time rather than years
later at restore time.

## On ModelScope

ModelScope publishes a **content sha256 for every file**, LFS or not, and publishes no git
blob id at all — the mirror image of Hugging Face. Every file is therefore anchored on
SHA-256 attested by the hub, and the SHA-1 caveat above simply does not arise.

Mechanically this is expressed by reporting every ModelScope file with `is_lfs=True`. That
flag does not describe a storage technology here; it selects which anchor the engine uses —
sha256 when set, git blob id otherwise. Reporting `is_lfs=False` would send verification to
a blob id that does not exist, and every plain text file in every repository would fail its
check.

The manifest records the difference honestly: `sha256_source` is `modelscope`, never
`hf-lfs`, so a later reader can tell which hub vouched for the digest. Manifests written
before ModelScope support keep their `hf-lfs` labels and remain valid.

## Why the ETag is not enough

The ETag is free from `head_object`, so the temptation is obvious. It does not carry the
weight:

1. **For a multipart object the ETag is not a content hash.** It is the MD5 of the
   concatenated part MD5s, with a `-N` suffix. It therefore depends on the *part size*, not
   only on the content. `aimm` rolls multipart itself and records `s3_part_size` and
   `s3_parts` precisely so that this value stays reproducible — boto3's transfer manager
   silently doubles the part size on large files, which destroys that property.
2. **Under SSE-KMS or SSE-C the ETag is not an MD5 at all**, even for a single PUT.
3. **MD5 is not an integrity proof** in any security-relevant sense.
4. **The ETag says nothing about Hugging Face.** It confirms that S3 stored what it
   received, not that what it received matches the repository.
5. **There is no whole-object sha256 for a multipart object.** S3 can only carry SHA-256 as
   a *composite* value over the part digests. True whole-object checksums exist only for CRC
   algorithms, because only those linearise. The sha256 `aimm` computes in flight is the
   only trustworthy whole-object value there is.
6. The ETag does not change when only metadata changes.

The ETag stays useful as a **cheap drift indicator** in `verify --level quick`, which is
exactly the job it is fit for. It is the first line of defence, not the last.

## The manifest

One JSON document per (repository, revision), stored beside the data:

```text
<prefix>/v1/<repo_type>/<owner>/<name>/revisions/<commit_sha>/manifest.json
<prefix>/v1/<repo_type>/<owner>/<name>/revisions/<commit_sha>/manifest.json.sha256
<prefix>/v1/<repo_type>/<owner>/<name>/revisions/<commit_sha>/files/<path/in/repo>
<prefix>/v1/<repo_type>/<owner>/<name>/refs/<ref>.json
```

It exists for five reasons that no other mechanism covers:

1. S3 cannot return a whole-object sha256 for a multipart object.
2. S3 user metadata is capped at 2 KB and is immutable after upload, so repository-level
   information does not fit there.
3. `restore` and `verify` must work without Hugging Face.
4. Retention needs a per-revision inventory to decide safely.
5. **Its presence is the completeness marker.**

### Completeness is structural

`manifest.json` is written only after *every* file in the selection has been transferred and
verified. There is no partial manifest and there is no status database.

The consequences follow directly:

- An aborted run leaves objects under `files/` but no manifest.
- `verify` reports that revision as `incomplete` and exits `20`.
- `sync` resumes it, transferring only what is missing.
- `prune` cleans it up, since incomplete revisions are deleted by default.

A crashed run is therefore *automatically* marked correctly, with no bookkeeping that can
itself go stale. The price is that there is no partial credit: if one file of five thousand
fails, there is no manifest and the next run must pick up the remainder. That is
deliberate — a manifest with holes would be worse than no manifest, because it would assert
something untrue.

### The manifest protects itself

`manifest.json.sha256` sits next to it. `catalog show`, `verify` and `restore` all check it
before trusting the manifest's contents.

## Immutability comes from the commit SHA

A revision is pinned to a full 40-character commit SHA **before** enumeration begins, and
that SHA is then used for every listing and every download URL.

```text
sha   = repo_info(repo_id, revision="main").sha      # 40 hex, immutable
files = list_repo_tree(repo_id, recursive=True, revision=sha)
url   = hf_hub_url(repo_id, path, revision=sha)
```

Enumerating against `main` and downloading against `main` separately allows a push in
between to produce a snapshot whose file list is from state A and whose bytes are from
state B, with nothing to detect it.

Because the SHA is part of the S3 key, three useful properties fall out for free:

- Re-syncing an unchanged revision is a no-op.
- A moved `main` produces a **new** revision prefix instead of overwriting the old one.
- Retention becomes prefix selection.

Short seven-character hashes are resolved server-side but are not used anywhere, because
they can become ambiguous as history grows, and a backup catalogue must not be ambiguous.

## Resume never compares size alone

A file already present is skipped only when the manifest entry agrees with upstream on
**digest and size**, and additionally — depending on `--recheck` — the object store confirms
it by `head_object` (size and ETag) or by reading and re-hashing the bytes. `--recheck none`
disables skipping entirely and re-transfers everything.

## Secondary safeguards

- The computed sha256 is also stored as the object metadata key `aimm-sha256`, alongside
  `aimm-repo-id`, `aimm-commit-sha` and `aimm-repo-type`. This is redundant to the manifest
  and exists so a third-party tool can check a single object without parsing anything of
  ours. The manifest remains the authority; metadata is capped at 2 KB and immutable.
- S3 lowercases metadata keys, so the schema is lowercase throughout and round-trip
  comparisons do not fail on capitalisation.

## Known limits, stated plainly

- **Non-LFS files have no upstream sha256.** Covered above.
- **`verify --level deep` costs full egress.** `quick` is the default for that reason and
  `--sample-percent` exists to make periodic deep checking affordable. See
  [Operations](operations.md#verification-strategy).
- **The manifest is a single point of truth.** Lose it and the data is still there but no
  longer verifiable. Mitigations: the digest file beside it, and
  `verify --level upstream`, which can re-derive the truth from Hugging Face for as long as
  the repository still exists there.
