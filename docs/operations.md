# Operations

Running `aimm hf-backup` as a scheduled job: what to schedule, what it costs, what to alert
on, and how to get the data back.

## The scheduling interface

`aimm` has no built-in scheduler. It is a process with three properties a scheduler can
build on:

1. **Stable exit codes.** In particular `20` (differences found) is distinct from `6`
   (corruption) and from `0` (clean).
2. **`--json` on stdout.** Exactly one document, nothing else; every log line goes to
   stderr.
3. **`--run-id`.** A correlation id that appears in every log record, in the manifest and
   in the JSON report.

That is the whole contract. Cron, systemd timers and Kubernetes CronJobs do the rest.

### Alerting rules

| Exit | Alert | Why |
| --- | --- | --- |
| `0` | none | |
| `20` | ticket, not a page | `verify` found drift or an incomplete revision. Usually resolved by re-running `sync` |
| `6` | **page** | stored bytes do not match the manifest. This is data loss until proven otherwise |
| `8` | ticket | transfer failed after retries — usually the network or the endpoint |
| `9` | ticket | a prune was refused by a safety guard. Investigate the policy, not the guard |
| `2`, `3` | ticket | configuration or credentials broke |
| `130` | none | someone interrupted it |

A monitor that collapses everything non-zero into one bucket will page at 03:00 for a `20`
that only needed a re-sync, and will under-react to the `6` that actually matters.

## Scheduled sync

```bash
# /etc/cron.d/aimm — daily at 02:00
0 2 * * * aimm  aimm --json --log-format json hf-backup sync \
  --from-file /etc/aimm/repos.txt >> /var/log/aimm/sync.jsonl 2>> /var/log/aimm/sync.log
```

`repos.txt` holds one specification per line; `#` starts a comment:

```text
# production models
meta-llama/Llama-3-8B
openai/whisper-large-v3@v3.0
datasets/my-org/eval-suite
```

Notes for unattended runs:

- Re-syncing an unchanged revision is a no-op — the commit SHA is part of the key.
- A moved `main` creates a **new** revision prefix. Old revisions are not overwritten, which
  is why retention exists.
- `--fail-fast` is usually wrong for a scheduled run: you want the other repositories
  attempted and one collected error report.
- Set `--log-format json` so the log is machine-parseable, and rely on the `run_id` to tie
  a log line back to a report.
- Progress bars disable themselves automatically without a TTY or when `CI` is set.

## Verification strategy

This is the section most operators skip, and the one that determines whether verification
ever actually happens.

### The cost model

| Level | Requests | Data transferred | Practical cost |
| --- | --- | --- | --- |
| `quick` | one `HEAD` per file | **none** | negligible; run it often |
| `deep` | one `HEAD` + one `GET` per file | **every stored byte** | full egress |
| `upstream` | as `deep`, plus the Hub file tree | every stored byte | full egress plus Hub calls |

> **`verify --level deep` across a 120 TB estate reads 120 TB back out of the object
> store.** On a cloud provider that is a per-gigabyte egress invoice. On self-hosted MinIO
> it is not billed, but it saturates the storage network for as long as it takes and
> competes with everything else on that array. Either way it is an operation to plan, not
> a button to press.

The two failure modes this causes in practice are equally bad: teams that discover the cost
and stop verifying at all, and teams that run one enormous deep verification, get the bill,
and never run another.

### `--sample-percent` is the affordable alternative

```bash
aimm hf-backup verify meta-llama/Llama-3-8B --level deep --sample-percent 2
```

This deep-checks a deterministic 2% of files. The sample is seeded on the commit SHA, so
the same revision always yields the same sample — repeated runs do not accumulate coverage
by themselves, which means you should vary *what* you verify rather than expecting
repetition to widen it.

Bit rot and silent storage faults are not targeted attacks; they are population-level
events. A small sample detects them with high probability at a fraction of the cost, and it
detects them *repeatedly* rather than once.

### A schedule that works

| Cadence | Command | Cost |
| --- | --- | --- |
| After every sync | `verify` (quick, implicit default) | none |
| Nightly | `verify --level quick` across everything | none |
| Weekly | `verify --level deep --sample-percent 1` across everything | 1% of the estate |
| Monthly | `verify --level deep` on the newest revision of your top-priority repositories | bounded by choice of repositories |
| On demand | `verify --level deep` (full) before decommissioning the upstream source | one full pass, deliberately |

`--level upstream` is the odd one out: it also re-fetches the Hub tree at the pinned SHA and
compares the manifest against it, which catches corruption of the *manifest itself*. Because
the SHA is immutable, the values must be identical. Run it when you suspect the manifest,
and while the upstream repository still exists.

```bash
# nightly quick sweep, alerting on drift
for repo in $(aimm --json hf-backup catalog list | jq -r '.repos[] | .repo_id'); do
  aimm --json hf-backup verify "$repo" > "/var/log/aimm/verify-$repo.json"
done
```

## Retention

`prune` deletes revisions no longer covered by the policy. It is the only command that can
delete anything.

```bash
# plan — changes nothing, exits 0
aimm hf-backup prune --all-repos --keep-last 3 --keep-within 90d

# apply
aimm hf-backup prune --all-repos --keep-last 3 --keep-within 90d --yes
```

### The rules

1. Any SHA a `refs/*.json` points at is **protected** and never deleted.
2. The **newest complete revision is always kept**, whatever the policy says.
3. `--keep-last N` keeps the N newest complete revisions.
4. `--keep-within D` keeps everything created within D of now.
5. Incomplete revisions (no manifest) are deleted unless `--keep-incomplete` is passed.
   This subtraction runs after rules 3 and 4, so recency alone never preserves the debris of
   a crashed run — but rule 2 still protects the newest complete copy.
   **Exception:** an incomplete revision younger than `--abort-older-than` (default `24h`)
   is kept regardless. At that age it cannot be told apart from a sync that is still
   uploading, and deleting a live run's objects leaves its workers to finish and write a
   manifest describing files that no longer exist — a revision marked complete that
   `restore` cannot satisfy. Raise `--abort-older-than` above your longest sync.
6. Everything else is deleted.

Both `--keep-last` and `--keep-within` may be given; the union is kept.

### The guards

- **A policy is mandatory.** Neither `--keep-last` nor `--keep-within` exits `9` without
  touching anything. An unconstrained prune is always a mistake.
- **`--yes` is mandatory to delete.** Without it, the plan is printed, nothing is deleted,
  no multipart upload is aborted, exit `0`.
- **A plan with no survivors is refused**, exit `9`.
- **Repositories or `--all-repos`, not both, not neither.** Exit `2`.

### Orphaned multipart uploads

```bash
aimm hf-backup prune --all-repos --keep-last 5 --abort-older-than 24h --yes
```

Multipart uploads abandoned by a crash consume storage indefinitely and appear in no object
listing, so they are invisible until the storage bill or the capacity alarm arrives. Every
normal failure path aborts its own upload; this sweeps up what a hard kill left behind.

**A container stop is no longer a hard kill.** `docker stop`, systemd and Kubernetes all
terminate with SIGTERM, whose default disposition kills the interpreter without unwinding —
no `finally`, and therefore no abort. `aimm` installs a cooperative handler instead: the
first signal asks every worker to stop at its next part boundary, each in-flight upload is
aborted properly, no manifest is written, and the process exits `130`. A **second** signal
hands back to the default disposition and terminates immediately.

Two operational consequences:

* Give the container a grace period long enough to finish the current parts —
  `stop_grace_period: 30s` is usually plenty, since only the parts in flight have to end,
  not the file.
* A cancelled repository stays incomplete on purpose. The manifest is the completeness
  marker, so the next run re-plans that repository from scratch rather than trusting a
  half-written record. Already-uploaded objects are re-uploaded; nothing is corrupted.

**Every sync also sweeps before it transfers.** `sync` aborts abandoned uploads of the
repository it is about to write that are older than `--abort-stale` (default `24h`), so a
run clears the debris of the previous one without anyone remembering to. `--abort-stale
off` disables it. The threshold is what makes this safe next to a concurrent run: an
upload lives only as long as ONE file transfer, so anything older by hours is provably
abandoned. The sweep is scoped to that repository's key root, so two catalogs under
different prefixes never touch each other's uploads, and a failure to list uploads is a
warning rather than a failed backup — listing is a separate S3 permission.

What it cannot cover is a repository that is no longer in the run at all: nothing sweeps a
model you removed from the catalog. A bucket lifecycle rule
(`AbortIncompleteMultipartUpload`, e.g. 7 days) is therefore still worth having, and it is
also the only backstop that works when the tool never runs again — after a SIGKILL, an OOM
kill, or a power cut.

## Disaster recovery

### Restore a specific revision

```bash
aimm hf-backup catalog revisions meta-llama/Llama-3-8B
aimm hf-backup restore meta-llama/Llama-3-8B \
  --revision a1b2c3d4e5f6... --dest /srv/models/llama3
```

**`restore` never contacts Hugging Face.** This is what makes the backup meaningful: it
works after the upstream repository has been deleted, renamed, gated, or after Hugging Face
is simply unreachable.

### Restore only part of a repository

```bash
aimm hf-backup restore owner/name --dest ./out --include '*.safetensors' --include '*.json'
```

### Confirm a restore would succeed, without writing

```bash
aimm hf-backup restore owner/name --dest ./out --verify-only
```

This performs the same read-and-hash pass as a real restore and writes nothing — the
cheapest honest rehearsal available.

### Recover from an incomplete revision

`verify` reporting `incomplete` (exit `20`) means objects exist under `files/` but no
manifest was ever written, so a run was interrupted. Re-run `sync` for that revision; only
the missing files are transferred, and the manifest is written when the set is complete.

## Monitoring

Useful fields from `--json`:

| Command | Field | Use |
| --- | --- | --- |
| `sync` | `.ok` | boolean, run-level success |
| `sync` | `.repos[].files_transferred`, `.bytes_transferred` | throughput trend |
| `sync` | `.repos[].errors` | per-repository error messages |
| `verify` | `.status` | `ok`, `drift`, `incomplete`, `corrupt` |
| `verify` | `.findings[]` | path, kind, expected, actual |
| `prune` | `.totals.bytes_deleted`, `.totals.revisions_deleted` | reclaimed capacity |
| `catalog.list` | `.repos[].complete_revisions` | coverage |
| `doctor` | `.ok`, `.checks[]` | health check |

Ship `--log-format json` to Loki or ELK and key dashboards on `run_id`.

A serviceable health check for a scheduled deployment is `aimm hf-backup doctor --json`
plus the age of the newest complete revision from `catalog revisions` — process liveness
alone tells you nothing about whether backups are still happening.

## Capacity planning

- Stored size is approximately the sum of repository sizes across retained revisions.
  Revisions do **not** share objects: a changed `main` stores the full new revision, since
  keys include the commit SHA.
- `catalog list` reports `total_bytes` per repository, and `catalog revisions` reports it
  per revision.
- Local disk is only needed for the `disk` transfer path and is bounded by
  `--max-disk` / `--disk-reserve`. Streaming needs none.
- Peak RAM is roughly `workers × max(part_size, inline_max)` — 64 MiB at the defaults.

## Security in operation

- Credentials are never flags. Environment, `/run/secrets` file, or interpolated into a
  profile.
- Every log line passes a redaction filter before reaching a handler; secrets are held as
  `SecretStr` and render masked in `--json` and in `doctor` output.
- Give the backup credential only the actions listed in [Backends](backends.md#least-privilege-iam-policy).
  `prune` is the only command that can delete, and it needs `--yes`.
- Presigned Hugging Face URLs expire and are never persisted into the catalogue or the
  manifest; they are re-resolved at transfer time.
