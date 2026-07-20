"""Fixtures wiring the integration tests to the live MinIO rig.

Only rig-scoped fixtures belong here: the credential resolution, the raw boto3 client,
an isolated per-test key prefix, and a fully constructed `S3Destination`. Everything
that does not need a real server lives in `tests/conftest.py` against moto.

Start the rig with:

    docker compose -f tests/integration/docker-compose.yml up -d --wait
    export AIMM_IT_ENDPOINT=http://localhost:9800

`AIMM_IT_ENDPOINT` is the gate. When it is unset this whole directory skips at
collection time, so `make test` on a laptop without Docker reports skips and never
errors. See `docker-compose.yml` and `README.md` for what the rig does and does not
prove — in particular, it speaks plain HTTP and therefore cannot exercise the
aws-chunked checksum trailer.
"""

from __future__ import annotations

import importlib.util
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import pytest

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client
    from types_boto3_s3.type_defs import ObjectIdentifierTypeDef

    from bg_ai_model_management.config.models import S3Settings
    from bg_ai_model_management.tools.hfbackup.destination import S3Destination

# --------------------------------------------------------------------------- the gate

_ENDPOINT = os.environ.get("AIMM_IT_ENDPOINT")
_BUCKET = os.environ.get("AIMM_IT_BUCKET", "aimm-it")
_REGION = os.environ.get("AIMM_IT_REGION", "eu-north1")


def _skip_reason() -> str | None:
    """Why this directory cannot run, or None when the rig is reachable."""
    if importlib.util.find_spec("boto3") is None:
        return "boto3 is not installed"
    if not _ENDPOINT:
        return "MinIO rig not running: set AIMM_IT_ENDPOINT (make minio-up)"
    return None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test under this directory `integration`, and skip it when the rig is down.

    Both marks are applied here rather than in the test modules, for two reasons.

    CI selects with `-m integration` over the whole tree (`pytest tests -m integration`,
    see `.github/workflows/integration.yml`). A module under this directory that forgot
    the marker would be silently deselected and the workflow would go green having run
    nothing — worse than a failure. Marking centrally makes that impossible.

    And the gate cannot be a module-level `pytest.skip(..., allow_module_level=True)`
    in this file. Verified: that works for `pytest tests`, but when the directory is
    named directly — `pytest tests/integration -m integration` — this conftest is loaded
    as an *initial* conftest during argument parsing, the `Skipped` exception escapes
    before any collection report exists, and pytest dies with a raw traceback and exit
    code 1 instead of skipping. The marking below must therefore keep working under BOTH
    invocations; neither one may be treated as the only shape CI uses.
    """
    reason = _skip_reason()
    here = Path(__file__).parent
    for item in items:
        if here not in item.path.parents:
            continue
        item.add_marker(pytest.mark.integration)
        if reason is not None:
            item.add_marker(pytest.mark.skip(reason=reason))


# --------------------------------------------------------------------- credentials


class RigCredentials(NamedTuple):
    """A non-root credential pair for the rig. Never `repr`ed with the secret."""

    access_key: str
    secret_key: str
    source: str

    def __repr__(self) -> str:
        """Redact the secret so a fixture dump or assertion diff cannot leak it."""
        return f"RigCredentials(access_key={self.access_key!r}, secret_key=***, source={self.source!r})"


def _load_service_account(path: Path) -> RigCredentials:
    """Read a `minio-init`-generated service-account credentials JSON."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        pytest.fail(f"AIMM_IT_CREDENTIALS_FILE={path} is not readable JSON: {exc}")
    access_key = payload.get("accessKey")
    secret_key = payload.get("secretKey")
    if not access_key or not secret_key:
        pytest.fail(f"AIMM_IT_CREDENTIALS_FILE={path} has no accessKey/secretKey")
    return RigCredentials(str(access_key), str(secret_key), source="service-account")


@pytest.fixture(scope="session")
def rig_credentials() -> RigCredentials:
    """The credential the tests authenticate with — always policy-scoped, never root.

    Prefers the service account provisioned by `minio-init`, whose credentials MinIO
    generates at bootstrap and which must therefore be exported out of the rig first
    (see README.md). Falls back to the deterministic policy-scoped user so the rig is
    usable with one command and no export step.

    The rig's root credentials are deliberately not reachable from here. Running the
    suite under least privilege is the point: a missing IAM action must fail in the
    rig, not in production.
    """
    creds_file = os.environ.get("AIMM_IT_CREDENTIALS_FILE")
    if creds_file:
        return _load_service_account(Path(creds_file))
    access_key = os.environ.get("AIMM_IT_ACCESS_KEY", "aimm-it-user")
    secret_key = os.environ.get("AIMM_IT_SECRET_KEY", "aimm-it-user-secret")
    return RigCredentials(access_key, secret_key, source="scoped-user")


# ------------------------------------------------------------------------ the rig


@pytest.fixture(scope="session")
def rig_endpoint() -> str:
    """The rig's S3 API URL. Non-None: an unset value skipped every item at collection."""
    assert _ENDPOINT is not None
    return _ENDPOINT


@pytest.fixture(scope="session")
def rig_bucket() -> str:
    """The bucket `minio-init` created. The credential can reach no other bucket."""
    return _BUCKET


@pytest.fixture(scope="session")
def rig_region() -> str:
    """Must match the server's `MINIO_REGION_NAME`, or every signed request redirects."""
    return _REGION


@pytest.fixture(scope="session")
def rig_client(
    rig_endpoint: str, rig_region: str, rig_credentials: RigCredentials
) -> Iterator[S3Client]:
    """A raw boto3 client for arranging and asserting around the code under test.

    Path-style and s3v4 to match how `S3Destination` talks to a self-hosted endpoint,
    so a test's setup cannot succeed via a route the tool itself would not take.
    """
    import boto3
    from botocore.config import Config

    client = boto3.session.Session().client(
        "s3",
        endpoint_url=rig_endpoint,
        region_name=rig_region,
        aws_access_key_id=rig_credentials.access_key,
        aws_secret_access_key=rig_credentials.secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )
    try:
        yield client
    finally:
        client.close()


def _purge(client: S3Client, bucket: str, prefix: str) -> None:
    """Remove every object and every in-flight multipart upload under `prefix`.

    Orphaned multipart uploads never appear in ListObjectsV2, so deleting objects
    alone would leave storage allocated and let one test's leftovers be counted by
    the next test's `abort_stale_uploads`.

    The `Prefix` filter is applied here in Python, not by the server. Verified against
    this rig: MinIO's ListMultipartUploads honours `Prefix` only when it equals a full
    object key — `it/`, `it/abc` and `it/abc/` all return zero uploads while the
    unfiltered call returns them all. Asking the server to filter would make this
    purge silently clean nothing. `MaxUploads` defaults to 10000, which the rig's key
    space cannot approach, and the paginator covers it if it ever did.
    """
    for upload_page in client.get_paginator("list_multipart_uploads").paginate(Bucket=bucket):
        for upload in upload_page.get("Uploads", []):
            if not upload["Key"].startswith(prefix):
                continue
            client.abort_multipart_upload(
                Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"]
            )
    for object_page in client.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix
    ):
        keys: list[ObjectIdentifierTypeDef] = [
            {"Key": obj["Key"]} for obj in object_page.get("Contents", [])
        ]
        if keys:
            client.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})


@pytest.fixture
def rig_prefix(rig_client: S3Client, rig_bucket: str) -> Iterator[str]:
    """A key prefix unique to this test, purged afterwards.

    The rig's bucket is long-lived and shared between tests and between runs, so
    isolation has to come from the key space. A random segment also keeps two
    developers pointed at the same rig from colliding.
    """
    prefix = f"it/{uuid.uuid4().hex[:12]}"
    try:
        yield prefix
    finally:
        _purge(rig_client, rig_bucket, prefix)


@pytest.fixture
def rig_settings(
    rig_endpoint: str,
    rig_bucket: str,
    rig_region: str,
    rig_prefix: str,
    rig_credentials: RigCredentials,
) -> S3Settings:
    """`S3Settings` pointed at the rig, scoped to this test's prefix.

    `preset` stays `auto` on purpose: resolving to path-style addressing from the
    presence of a custom endpoint is itself behaviour worth exercising against a
    real server. Override in a test that needs a specific preset.
    """
    from pydantic import SecretStr

    from bg_ai_model_management.config.models import S3Settings

    return S3Settings(
        endpoint_url=rig_endpoint,
        region=rig_region,
        bucket=rig_bucket,
        prefix=rig_prefix,
        access_key_id=SecretStr(rig_credentials.access_key),
        secret_access_key=SecretStr(rig_credentials.secret_key),
    )


@pytest.fixture
def rig_destination(rig_settings: S3Settings) -> Iterator[S3Destination]:
    """A probed `S3Destination` against the rig, closed afterwards.

    `S3Destination.create` runs the capability probe, so simply requesting this
    fixture already asserts that the least-privilege policy is sufficient to reach
    the bucket and write, read and delete the probe object.
    """
    from bg_ai_model_management.tools.hfbackup.destination import S3Destination

    destination = S3Destination.create(rig_settings)
    try:
        yield destination
    finally:
        destination.close()
