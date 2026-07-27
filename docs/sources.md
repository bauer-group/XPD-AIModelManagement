# Sources

`aimm hf-backup` mirrors from two upstream hubs. Everything after the download —
planning, integrity checking, the S3 key layout, the manifest, `verify`, `restore`,
`prune` — is identical; only where the bytes come from differs.

| Hub | `--source` | Default branch | Repo types | Credential |
| --- | --- | --- | --- | --- |
| huggingface.co | `huggingface` (default) | `main` | models, datasets | `HF_TOKEN` (gated repos, rate limits) |
| modelscope.cn | `modelscope` | `master` | models only | `MODELSCOPE_API_TOKEN` (private repos only) |

```bash
aimm hf-backup sync Qwen/Qwen3-0.6B                              # Hugging Face
aimm hf-backup sync --source modelscope iic/SenseVoiceSmall --revision master
aimm hf-backup doctor --source modelscope
```

`AIMM_SOURCE=modelscope` sets it for a whole shell or a container.

## Keep the hubs in separate prefixes

The key layout is `<bucket>/<prefix>/v1/models/<owner>/<name>/revisions/<sha>/`. The
same `owner/name` frequently exists on **both** hubs with **different commit SHAs**, so
mirroring both under one prefix interleaves two upstreams in a single namespace and lets
their `refs/<ref>.json` pointers overwrite each other.

Give each hub its own prefix — `s3.prefix: hf` and `s3.prefix: ms`, one profile each:

```yaml
# aimm.modelscope.yaml
s3:
  bucket: ai-models
  prefix: ms
modelscope:
  endpoint: https://modelscope.cn
```

```bash
aimm --profile aimm.modelscope.yaml hf-backup sync --source modelscope <repo>
```

Mirror any given model from exactly one hub. Where a model exists on both, prefer
Hugging Face and keep ModelScope for what is only published there.

## What differs on ModelScope

Three differences are load-bearing, and all three were verified against the live API.

**Refs come from git, not the REST API.** `/api/v1/models/<id>/revisions` returns branch
*names* only, and the `Revision` field on a file entry is the last commit that touched
*that file* — not the repository head. The branch head comes from git's smart-HTTP
endpoint, which serves real refs only to a git-shaped user agent. A revision that is
already a 40-character SHA is used as-is and needs no lookup.

**Failures arrive as HTTP 200.** A missing repository answers `200` with
`{"Success": false, "Code": 10010205001, …}`. The status line alone is not a success
signal, so the envelope is checked on every REST call.

**Every file carries a content sha256** — LFS or not — and no git blob id exists. That is
the mirror image of Hugging Face, where a plain file has only a blob id. Since the engine
picks its integrity anchor from `SourceFile.is_lfs` (sha256 when set, git blob id
otherwise), every ModelScope file is reported with `is_lfs=True`. That is not a claim
about git-lfs: it selects the digest ModelScope actually attests. Verification is
therefore at least as strong as on Hugging Face — stronger for plain text files, which
Hugging Face can only anchor with a git blob id.

The manifest records which hub vouched for each digest in `sha256_source`:

| Value | Meaning |
| --- | --- |
| `hf-lfs` | Hugging Face LFS metadata attested this sha256 |
| `modelscope` | ModelScope's per-file `Sha256` attested this sha256 |
| `computed` | Only this tool hashed the bytes; no upstream attestation |

## Limits

* **Datasets are Hugging Face only.** ModelScope serves datasets from a different API
  than the one this tool speaks, so `--source modelscope` with `--repo-type datasets` is
  refused at pin time rather than failing mid-transfer.
* **No identity probe.** `doctor --source modelscope` reports reachability and whether a
  token is configured; it never claims a user name, because ModelScope's identity
  endpoint is not part of the verified surface this implementation is built on.
* **`--revision main` will not resolve** on most ModelScope repositories. The error lists
  the refs that do exist.

## Adding a third hub

Implement `bg_ai_model_management.tools.hfbackup.types.Source` — six methods and a
`kind` — and add the enum member. The engine, planner, destination, manifest and every
command are already written against that protocol; nothing else has to change.
