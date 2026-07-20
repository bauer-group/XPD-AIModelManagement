# Transfer strategy

Three paths, chosen per file. Both extremes can be forced with `--mode`.

| Path | Chosen when | Mechanism | RAM | Disk |
| --- | --- | --- | --- | --- |
| `inline` | `size <= inline_max` (default 8 MiB) | body in memory, one `PutObject` | `workers × inline_max` | none |
| `stream` | the default for anything larger | Hugging Face HTTP stream straight into a hand-rolled multipart upload | `workers × part_size` | none |
| `disk` | fallback, see below | staged download, then multipart from the file | `workers × part_size` | bounded by a budget |

There are three rather than two because the third would otherwise be missed: by file
*count*, small files are the majority of almost every Hugging Face repository.

## How the path is chosen

```text
choose_path(f, cfg, budget, state):
    if cfg.mode == DISK:                            return DISK
    if f.size is None:                              return DISK
    if f.size <= cfg.inline_max:                    return INLINE
    if cfg.mode == STREAM:                          return STREAM
    # cfg.mode == AUTO from here
    if state.stream_failures(f.path) >= cfg.stream_failure_downgrade:  return DISK
    if cfg.prefer_xet and f.xet_hash and budget.can_reserve(f.size):   return DISK
    try:    choose_part_size(f.size, cfg.part_size)
    except ObjectTooLargeError:                     return DISK
    return STREAM
```

### Why each branch is there

- **Unknown size goes to disk.** Without a size the part size cannot be chosen. boto3's
  chunk-size adjuster only corrects when the size is known; at unknown size it stays at
  8 MiB parts and silently caps the object at 10 000 × 8 MiB ≈ 78 GiB. That is a silent
  truncation in the middle of a large run. In practice the file listing always supplies the
  size; this branch is the guard for the day it does not.
- **Small files go inline.** A multipart upload for an 800-byte `config.json` is three
  round trips instead of one. A single `PutObject` also makes the ETag the MD5 of the data
  and lets S3 store a real whole-object `ChecksumSHA256` — the only file class for which
  that is possible at all.
- **Streaming is the default for shards.** No disk, no cleanup, no inode leak, and the
  sha256 is computed in the same pass as the upload.
- **Two stream failures downgrade the file to disk.** An HTTP body cannot be resumed
  mid-stream, so a failure at 90% costs the whole 90% again. After the second failure the
  file is fetched to disk once; every further upload attempt then costs no Hugging Face
  bandwidth at all.
- **`--mode stream` is a request, not a contract.** Unknown size and objects too large for
  any admissible part size still fall back or fail loudly. A clear failure beats an upload
  that dies at part 10 001.
- **`--mode stream` never demotes to disk.** The downgrade above is scoped to `auto` on
  purpose: `stream` is the mode you pick when there is no staging volume, so staging
  gigabytes behind your back would be the wrong kind of helpful. A file whose stream keeps
  failing is retried `stream_failure_downgrade + 1` times and then reported as a transfer
  error carrying the underlying transport failure as its cause.

### Part size

```text
PART_MIN  = 5 MiB      # S3 hard floor
PART_MAX  = 5 GiB      # S3 hard ceiling
MAX_PARTS = 10 000     # S3 hard ceiling

choose_part_size(size, configured):
    ps = max(configured, PART_MIN)
    while ceil(size / ps) > MAX_PARTS:
        ps *= 2
    if ps > min(PART_MAX, max_part_memory):
        raise ObjectTooLargeError(size, ps)
    return ps
```

Because the size is always known and the part size grows with the file, the 78 GiB ceiling
is structurally excluded. The chosen part size is recorded in the manifest, which keeps the
multipart ETag reproducible.

## The Xet trade-off

`hf-xet` accelerates **file downloads to disk** through chunk-level deduplication. It
accelerates nothing else.

The streaming path is a plain HTTP GET against the resolver/CDN — it does not pass through
xet-core, and `HF_XET_HIGH_PERFORMANCE` has no effect on it. That is a real cost of choosing
streaming, not a footnote.

`--prefer-xet` is the lever: for files that carry a Xet hash it forces the disk path, so you
buy the acceleration with disk space and cleanup work. Which side of that trade is right
depends on your repositories and your hardware, so the tool does not decide it for you.

> `hf_transfer` is not an option. It was removed in `huggingface_hub` 1.x;
> `HF_HUB_ENABLE_HF_TRANSFER` is ignored there and only emits a warning. It is not a
> dependency of this project and must not be added.

## The disk budget

The disk path is bounded, not hopeful:

```text
budget = min(max_disk_bytes, free_space(staging_dir) - disk_reserve)
```

A worker reserves the file's size **before** downloading and releases it after the upload.
If a single file does not fit the budget, the run raises `InsufficientDiskSpaceError` and
exits `7` instead of filling the host's disk.

Layout: one staging directory per run, one subdirectory per file, removed entirely with
`rmtree` after the upload — including on failure. Deleting only the payload would leave
behind `.cache/huggingface/download/<name>.metadata`, possibly a lock file, plus
`.gitignore` and `CACHEDIR.TAG`. Across millions of files that is an unbounded inode leak.

## Concurrency

- **One S3 client, created on the main thread, shared by every worker.** Built from an
  explicit session, because the module-level `boto3.client()` alias is not documented as
  thread-safe.
- `max_pool_connections` scales with the worker count. The default of 10 is too small and
  causes threads to block invisibly inside the connection pool.
- **Parallelism is per file, not per part.** Within one file, parts upload sequentially: the
  streaming path has exactly one HTTP body, and parallel parts would require range requests
  and would destroy the single-pass sha256.
- **Default 8 workers.** That is the value Hugging Face itself ships for
  `snapshot_download`, and it is the only defensible evidence available. Higher values run
  into Hub rate limits.
- **One `rich` console** is shared between the progress display and the log handler. Two
  consoles corrupt each other's output. Without a TTY, or when `CI` is set, the progress
  display is disabled outright — a degraded progress bar emits one line per refresh, which
  for thousands of files means thousands of lines.

## Tuning

| Symptom | Knob | Notes |
| --- | --- | --- |
| Throughput is low, link is not saturated | `--workers` | 1..64; watch for Hub rate limiting above ~8 |
| Very large shards fail to size parts | `--max-part-memory` | RAM ceiling is `workers × part_size` |
| Many tiny files, too many requests | `--inline-max` | raising it moves more files to a single PUT |
| Repeated stream failures on one endpoint | `--mode disk` | trades RAM-only operation for disk |
| Repositories with high chunk redundancy | `--prefer-xet` | needs disk budget |
| Host disk keeps filling | `--disk-reserve`, `--max-disk` | reserve is never consumed by staging |
| Staging on the wrong volume | `--staging-dir` | defaults to the system temp directory |

Rules of thumb:

- Peak RAM is approximately `workers × max(part_size, inline_max)`. With the defaults that
  is 8 × 8 MiB = 64 MiB.
- Raising `--workers` without raising the connection pool is pointless; the pool is sized
  from the worker count automatically.
- Raising `--part-size` reduces request count but raises the RAM ceiling proportionally.

## Retry behaviour

Only transient failures are retried, with full-jitter exponential backoff:

| Retried | Never retried |
| --- | --- |
| `httpx` transport and timeout errors | HTTP 401 / 403 / 404 |
| Hub HTTP errors with 408, 425, 429, 500, 502, 503, 504 | gated repository, repository not found, revision not found |
| S3 `RequestTimeout`, `SlowDown`, `Throttling`, `InternalError`, `ServiceUnavailable`, and similar | `NoSuchBucket`, `AccessDenied`, invalid storage class |
| botocore connection and read/connect timeout errors | any integrity error, any `ValueError`, `KeyboardInterrupt` |

Retrying everything would mean retrying programming errors and permanent 403s. At the
bandwidth involved that is expensive, so the classification is explicit and unit-tested.

On HTTP 429 from the Hub there is deliberately **no** extra backoff: `huggingface_hub`
already parses the rate-limit header and sleeps exactly until reset. This retry layer sits
outside it and only engages once the Hub client itself gives up.

## Cleanup guarantees

- Every multipart upload runs inside a context manager that calls
  `abort_multipart_upload` on **every** exception path. An abandoned multipart upload
  occupies storage on MinIO permanently and appears in no object listing.
- `prune --abort-older-than 24h` sweeps up multipart uploads orphaned by earlier crashes.
- `SIGINT` shuts down in order: parts in flight finish, no new files start, multipart
  uploads are aborted, exit code `130`. No manifest is written, so the run stays correctly
  marked incomplete.
- A single file error does not end the run by default. Errors are collected and reported;
  `--fail-fast` opts into stopping at the first one.
