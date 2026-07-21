# Security Policy

> **{{COMPANY_NAME}}** | {{REPO_FULL_NAME}}

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
{{SUPPORTED_VERSIONS_TABLE}}

Only the latest minor receives fixes. Upgrade before reporting a bug against an
older one. This table is rendered from `__version__` — see
`scripts/generate-docs.py` — because a hand-maintained one goes stale the moment
semantic-release bumps the version, and a policy naming the wrong supported
version is worse than none.

_Last updated: {{LAST_UPDATED}}._

## Reporting a vulnerability

Report security issues **privately** to **<security@bauer-group.com>** or via
[GitHub Security Advisories]({{REPO_URL}}/security/advisories/new).

Please do not open a public issue for a vulnerability. We aim to acknowledge
within 2 business days and to ship a fix or mitigation within 30 days.

## Threat model

`aimm` is a command-line tool run by an operator against infrastructure they
already control. It is not a service, has no network listener and has no
privilege boundary of its own. What it does have is **custody of two kinds of
credential**, and that is where the risk lives:

| Credential | What it grants |
| --- | --- |
| S3 access key + secret | read/write/delete on the backup bucket — that is, the ability to destroy the backups the tool exists to create |
| Hugging Face token | read access to every private and gated repo the account can see; a write-scoped token can modify them |

Both are supplied by the operator, held only in memory for the duration of a
run, and never persisted by the tool.

## Never paste `doctor` output that contains a secret

`aimm hf-backup doctor` is the command we ask for in bug reports. It is designed
for that: every credential is a `pydantic.SecretStr`, which renders as
`**********`, and `masked_settings()` additionally masks credential-shaped values
before anything is printed.

**Read it before you paste it anyway.**

Masking protects the fields the tool knows are secrets. It cannot protect a
secret you put somewhere it did not expect — an access key interpolated into a
custom `endpoint_url`, a presigned URL in an error message, a token embedded in
a bucket name or a profile path. Those are your values in your config, and the
tool has no way to recognise them.

The same applies to logs. `--log-level DEBUG` is far more verbose than the
default and includes request-level detail.

## Credential handling — what the tool promises

- Secrets are read from the **environment** or a profile file. Never hardcoded,
  never written to disk by the tool, never embedded in the manifest.
- **No secret is ever accepted as a CLI flag.** Flags land in shell history and
  in the process table, where every other user on the machine can read them. Use
  `AIMM_S3__ACCESS_KEY_ID` / `AIMM_S3__SECRET_ACCESS_KEY` and `HF_TOKEN`.
- Every credential is a `pydantic.SecretStr`, whose `str` and `repr` are the
  mask. This is what keeps a secret out of a **traceback**, which is the leak
  path people forget.
- Logging runs through a redacting filter (`logging_setup.py`) that masks
  key/value pairs, DSNs and JSON values that look like credentials — a defence in
  depth behind `SecretStr`, not a replacement for it.
- The tool does not phone home. The only network destinations are the Hugging
  Face Hub and the S3 endpoint you configure.

## Operational guidance

- **Scope the S3 credential to the backup bucket.** The tool needs
  `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`, `s3:AbortMultipartUpload`,
  `s3:ListMultipartUploadParts` on `arn:aws:s3:::<bucket>/*`, plus
  `s3:ListBucket`, `s3:GetBucketLocation`, `s3:ListBucketMultipartUploads` on
  `arn:aws:s3:::<bucket>`. Nothing else.
- **Do not grant `s3:CreateBucket`.** `ensure_bucket` defaults to off. Without
  the permission, a misconfigured endpoint fails loudly instead of quietly
  creating a bucket somewhere unexpected.
- **Use a read-scoped Hugging Face token.** Backup never needs write access to
  the Hub.
- **Do not disable TLS verification.** `--no-verify-tls` exists for a self-signed
  lab endpoint and turns the transport into plaintext-equivalent for an attacker
  on the path. It is not a fix for a certificate problem.
- Prefer per-host credentials over a shared one, so a rotation has a blast
  radius you can reason about.

## Reporting scope

In scope: anything that leaks a credential (into logs, output, the manifest, an
object, a traceback), that writes or deletes outside the configured prefix, that
lets remote repository content escape the intended destination path, or that
silently reports a corrupt backup as good.

Out of scope: the operator's own misconfiguration, the security of the S3
backend itself, and anything requiring an attacker who already controls the
machine `aimm` runs on.

## Scanning

Secret scanning (gitleaks + GitGuardian) and dependency scanning run on every
push via the shared `bauer-group/automation-templates` security workflow.
Dependencies are additionally monitored by Dependabot.

`.gitleaks.toml` allowlists documentation placeholders and the local MinIO test
rig's throwaway credentials — never `src/`. A finding under `src/` is always
worth a human look.
