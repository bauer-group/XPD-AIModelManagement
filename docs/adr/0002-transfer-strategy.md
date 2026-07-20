# ADR 0002 — Transfer strategy: auto-hybrid and hand-rolled multipart

- Status: accepted
- Date: 2026-07-20
- Affects: `bg_ai_model_management.tools.hfbackup.planner`, `.source`, `.destination`, `.engine`

## Context

The task is to back up Hugging Face repositories into a 120 TB MinIO, and also into AWS S3,
R2 and Wasabi. Individual shards are in the tens of gigabytes; the file count per repository
ranges from three to several thousand. The development host is Windows; production runs on
Linux with limited disk space.

Relevant verified constraints:

- `client.put_object(Body=<non-seekable>)` is unreliable: against a plain-HTTP endpoint and
  with `request_checksum_calculation="when_required"` it fails immediately with
  `io.UnsupportedOperation: seek`. Over HTTPS with `when_supported` the first attempt
  succeeds (aws-chunked), but a **retry** fails with `UnseekableStreamError`, because the
  stream would have to be rewound.
- `upload_fileobj` handles non-seekable streams, but at unknown size it picks 8 MiB parts and
  therefore caps the object at 10 000 × 8 MiB ≈ 78 GiB. The `ChunksizeAdjuster` only corrects
  when the size is **known**.
- Non-seekable uploads buffer in RAM: `requires_multipart_upload` reads up to
  `multipart_threshold` bytes purely in order to decide.
- S3 limits: at most 10 000 parts, part size 5 MiB to 5 GiB, last part arbitrarily short.
- `hf-xet` accelerates only **file downloads** to disk. No streaming path passes through Xet
  chunk deduplication.
- House ruling: hand-rolled multipart rather than the boto3 transfer manager, because MinIO
  and Ceph/RGW are strict about multipart semantics; equal-sized parts, `abort` in the
  `except`, and a `head_object` verification after every upload.

## Decision

**Three transfer paths, chosen automatically by rule, either extreme forceable by flag, and
a self-implemented multipart upload as the common foundation.**

| Path | When | Mechanism |
| --- | --- | --- |
| `INLINE` | `size <= inline_max` (default 8 MiB) | body in RAM, one `put_object` with `ContentLength` and `ChecksumSHA256` |
| `STREAM` | the default for anything larger | `get_session().stream("GET", hf_hub_url(...))` → `iter_bytes()` → our own multipart upload, sha256 computed in flight |
| `DISK` | fallback, see below | `hf_hub_download(local_dir=...)` → multipart from the file → `shutil.rmtree` of the file's directory |

`DISK` is chosen for: `--mode disk`, unknown file size, after two stream failures of the
same file, with `--prefer-xet` for Xet files when the budget allows, or when no admissible
part size exists.

The part size is derived deterministically from the known size (doubling until
`ceil(size/ps) <= 10000`, clamped to `[5 MiB, min(5 GiB, max_part_memory)]`) and recorded in
the manifest.

A `DiskBudget` reserves the file size before every `DISK` download and releases it after the
upload; the budget is `min(max_disk_bytes, free - reserve)`.

## Consequences

### Positive

- The normal case — large shards — never touches disk. No staging volume, no cleanup
  failures, no inode leak.
- Because we roll multipart ourselves and *always* know the size from `list_repo_tree`, the
  78 GiB trap is structurally excluded: the part size grows with the file.
- The sha256 is produced in the same pass as the upload — no second read, no extra egress
  cost, and for LFS files it is immediately checkable against Hugging Face's own
  `lfs.sha256`.
- Because we set and store the part size ourselves, the multipart ETag is recomputable and
  resume works without re-downloading.
- The RAM ceiling is exactly `workers × part_size` and therefore predictable (64 MiB by
  default), rather than depending on how the transfer manager behaves with non-seekable
  streams.
- Small files — the majority by count — cost one request instead of three, and are the only
  class that gets a real server-side whole-object sha256.
- The downgrade after two stream failures fixes a defect of the original design: a pure
  **upload** failure no longer triggers a re-download from Hugging Face.

### Negative

- **The streaming path gives up `hf-xet`.** Neither `get_session().stream(...)` nor
  `HfFileSystem.open()` goes through xet-core; both are plain HTTP GETs against the
  resolver/CDN. For repositories with high chunk redundancy the DISK path is measurably
  faster. That is why `--prefer-xet` exists — the throughput gain is paid for in disk space,
  and that trade belongs to the operator, not to the tool.
- **An aborted stream cannot be resumed.** An HTTP body cannot be picked up in the middle; an
  abort at 90% costs the full 90% again. The two-failure downgrade limits the damage but does
  not remove it.
- **We now carry multipart code ourselves.** Part numbering from 1 and without gaps
  (non-consecutive numbers are answered by S3 with HTTP 500, not 4xx), `abort` on every
  failure path, resumption, limits. That is real code with real bugs; it needs the MinIO
  integration tests, and moto has demonstrably been shown to be insufficient for it.
- Three paths mean three code paths in tests, logs and error messages. `transfer_path` is
  therefore recorded in the manifest and in every log line; otherwise an error message cannot
  be attributed reproducibly.
- The DISK path needs accounting (`DiskBudget`), otherwise `N` workers × a large shard fill
  the disk. That accounting is additional concurrent state.

### Neutral

- `--mode stream` is a request, not a contract: unknown size and oversized objects still lead
  to `DISK` or to a clear error. A loud abort is better than an upload that dies at part
  10 001.

## Alternatives rejected

**`upload_fileobj` only (the boto3 transfer manager).** Less code of our own, but: 8 MiB
parts at unknown size and therefore the 78 GiB ceiling; RAM buffering purely to decide; and
the house ruling exists precisely because MinIO and Ceph are strict about multipart
semantics. We would also have no control over the part size and could not recompute the
ETag.

**Disk buffering only (the original design).** Simple, and it keeps `hf-xet`. But: a staging
volume on the order of the largest repository, a cleanup obligation for every file, and the
original design itself demonstrates how easily `.cache/huggingface/` residue is left behind.

**Streaming only.** Elegant, but not safely partitionable at unknown size and with no
fallback if an endpoint turns out to be hostile to streaming.

**`HfFileSystem.open(block_size=0)` as the primary variant.** It works (verified), but
Hugging Face itself documents overhead in the fsspec layer and recommends `HfApi` methods for
"better performance and reliability". It remains the fallback.

**Parallel parts per file over HTTP range requests.** Higher single-file throughput, but it
destroys the single-pass sha256, multiplies requests against the scarce Hugging Face
resolver budget, and makes error handling considerably more complicated. With 8 files in
parallel the link is saturated anyway.

**`hf_transfer`.** Removed in `huggingface_hub` 1.x; `HF_HUB_ENABLE_HF_TRANSFER` is ignored
and only produces a `FutureWarning`. Do not add it to the dependencies.
