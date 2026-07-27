# ADR 0007 — A Source protocol and a second hub (ModelScope)

- Status: accepted
- Date: 2026-07-27
- Affects: `bg_ai_model_management.tools.hfbackup.types`, `.source`, `.source_modelscope`, `.engine`, `.manifest`, `.cli`, `bg_ai_model_management.config.models`

## Context

`hf-backup` was written against one upstream. `Engine.__init__` named `HubSource`
concretely, and the manifest hard-coded `sha256_source` as `"hf-lfs" | "computed"`.

Some models are published only on modelscope.cn — Chinese-market releases, and
fine-tunes that never reach Hugging Face. A sovereign mirror that cannot hold them is
incomplete, and the alternative in practice was a private re-implementation of the whole
download path in every consumer of this library. One such island already existed
downstream, which is what prompted this.

Constraints, all verified against the live ModelScope API:

- **The REST API cannot resolve a branch head.** `/api/v1/models/<id>/revisions` returns
  branch *names*; the `Revision` field on a file entry is the last commit that touched
  *that file*. Only git's smart-HTTP endpoint (`/<repo>.git/info/refs`) returns a head,
  and only to a git-shaped `User-Agent` — anything else gets
  `{"message": "mirror self-forwarded loop detected"}`.
- **Errors arrive as HTTP 200** with `{"Success": false, "Code": 10010205001}` for a
  missing repository.
- **Every file carries a content `Sha256`, LFS or not, and no git blob id exists.** This
  is the inverse of Hugging Face, where a plain file has only `blob_id` and an LFS file
  has `lfs.sha256`.
- **Listing accepts a commit SHA** as `Revision`, so the pin-then-enumerate invariant
  holds: listing two different commits of the same repository returns different trees.
- **Datasets are served by a different API shape**; the models path answers HTTP 405
  under `/api/v1/datasets/…`.

## Decision

**1. Extract a `Source` protocol.** Six methods — `pin`, `list_files`, `open_stream`,
`read_bytes`, `staged`, `whoami` — plus a `kind`. `Engine` is typed against it. The
protocol is structural, so no implementation inherits anything, and `mypy --strict`
rejects a source that drifts from the contract.

**2. Report ModelScope files as `is_lfs=True`.** The engine chooses its integrity anchor
from that flag: sha256 when set, git blob id otherwise. ModelScope attests a content
sha256 for every file and publishes no blob id, so `is_lfs=False` would verify against an
anchor that does not exist and every plain file in every repository would fail its check.
The flag selects a digest, not a storage technology, and this is the reading that makes
verification correct.

**3. Widen `sha256_source` to `hf-lfs | modelscope | computed`.** The manifest is an audit
record; labelling a ModelScope digest `hf-lfs` would assert a provenance that was never
established. The value is derived from `Source.kind`, so a third hub cannot forget it.

**4. Keep credentials and endpoints per hub.** `ModelScopeSettings` sits beside
`HubSettings` rather than being folded into it, so an `HF_TOKEN` can never be sent to
modelscope.cn, nor the reverse.

**5. Select the hub per invocation, not per profile field.** `--source` /
`AIMM_SOURCE` on `sync`, `verify` and `doctor`. `restore` reads only S3 and never
contacts an upstream, so it takes no flag.

## Consequences

- A third hub is a new `Source` implementation, a new `SourceKind` member and nothing
  else. No dispatch table, no plugin framework.
- The tool is still called `hf-backup` while now mirroring two hubs. Renaming it would
  break every existing script and profile for a cosmetic gain; the name is treated as
  historical, and `--source` is the real selector.
- **Operators must give each hub its own `s3.prefix`.** The same `owner/name` exists on
  both hubs with different commit SHAs, and one prefix would interleave two upstreams in
  a single key namespace. This is documented in [Sources](../sources.md) but not
  enforceable here: a single run cannot see the other run's configuration.
- Datasets remain Hugging Face only, refused at pin time rather than mid-transfer.
- Manifests written before this change keep their `hf-lfs` labels and stay valid; the
  Literal was widened, not changed.

## Alternatives rejected

**A ModelScope-to-Hugging-Face shim.** Wrapping ModelScope in something that quacks like
`huggingface_hub` would have avoided the protocol, but every difference above would then
be hidden behind a fake `RepoFile`, and the blob-id gap cannot be faked at all.

**One merged `HubSettings` with a hub-selecting field.** Cheaper, and exactly how a token
ends up on the wrong host.

**Leaving it downstream.** The island already existed and worked, but it re-implemented
pinning, listing, streaming, staging and error translation against a private engine API —
which is a maintenance liability for both repositories and cannot be shared.
