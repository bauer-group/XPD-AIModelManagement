"""Shared pytest fixtures.

Only fixtures used by more than one test module belong here: temporary paths, a moto
S3 backend, and an instant-sleep helper so retry tests do not actually wait. Module-local
fixtures stay in their own test module.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
import pytest
from moto import mock_aws

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

TEST_REGION = "us-east-1"
TEST_BUCKET = "aimm-test"


@pytest.fixture(autouse=True)
def _utf8_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force UTF-8 for CliRunner output.

    Rich box-drawing characters raise UnicodeEncodeError under the Windows cp1252
    console encoding, which would fail CLI tests on the maintainer's machine only.
    """
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")


@pytest.fixture
def clean_aimm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every variable the settings layer reads from the ambient environment.

    A maintainer who exports `AIMM_S3__BUCKET` in their shell would otherwise see
    precedence tests fail for reasons that have nothing to do with the code.
    """
    for name in list(os.environ):
        if name.startswith(("AIMM_", "AWS_")):
            monkeypatch.delenv(name, raising=False)
    for name in ("HF_TOKEN", "HF_ENDPOINT", "HUGGING_FACE_HUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def secrets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for `/run/secrets`, wired into `Settings.model_config`.

    `secrets_dir` is class-level pydantic configuration, so it cannot be passed per
    call. `load_settings` derives a fresh subclass on every call, which picks the
    patched value up; monkeypatch restores the original afterwards.
    """
    from bg_ai_model_management.config.models import Settings

    path = tmp_path / "run-secrets"
    path.mkdir()
    monkeypatch.setitem(Settings.model_config, "secrets_dir", str(path))
    return path


@pytest.fixture
def staging_dir(tmp_path: Path) -> Path:
    """An empty directory usable as `transfer.staging_dir`."""
    path = tmp_path / "staging"
    path.mkdir()
    return path


@pytest.fixture
def dest_dir(tmp_path: Path) -> Path:
    """An empty directory usable as a `restore --dest` target."""
    path = tmp_path / "dest"
    path.mkdir()
    return path


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install fake AWS credentials so moto can never reach a real account."""
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", TEST_REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)


@pytest.fixture
def s3_client(aws_credentials: None) -> Iterator[S3Client]:
    """A moto-backed S3 client built from an explicit Session, as production does."""
    with mock_aws():
        session = boto3.session.Session(region_name=TEST_REGION)
        yield session.client("s3", region_name=TEST_REGION)


@pytest.fixture
def s3_bucket(s3_client: S3Client) -> str:
    """Create and return the name of an empty test bucket."""
    s3_client.create_bucket(Bucket=TEST_BUCKET)
    return TEST_BUCKET


class FakeSleep:
    """Records requested sleep durations instead of sleeping.

    Injected as the `sleep=` argument of `call_with_retry`, so retry tests exercise the
    full backoff schedule in microseconds and can assert on the waits.
    """

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)

    @property
    def total(self) -> float:
        """Sum of every requested wait, in seconds."""
        return sum(self.calls)


@pytest.fixture
def fake_sleep() -> FakeSleep:
    """An instant, recording stand-in for `time.sleep`."""
    return FakeSleep()


@pytest.fixture
def minio_endpoint() -> str:
    """Endpoint of a live MinIO for integration tests; skips when unset."""
    endpoint = os.environ.get("AIMM_IT_ENDPOINT")
    if not endpoint:
        pytest.skip("AIMM_IT_ENDPOINT is not set; skipping integration test")
    return endpoint
