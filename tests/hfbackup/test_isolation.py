"""The environment scrubber that guards every test in this package.

`_isolated_env` exists so a maintainer who exports `AIMM_S3__BUCKET` in their shell
cannot change a test's outcome. It scrubs by prefix, and `AIMM_IT_ENDPOINT` — the
address of the MinIO integration rig — matches that prefix by accident. Scrubbing it
made every `@pytest.mark.integration` test skip unconditionally: CI booted the rig, the
`minio_endpoint` fixture read an already-emptied environment, both tests skipped, and
the job exited 0 having asserted nothing. A permanently, vacuously green gate is worse
than no gate, so the exemption gets a test of its own.
"""

from __future__ import annotations

import os

import pytest

from .conftest import scrub_ambient_env


def test_the_scrubber_drops_settings_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIMM_S3__BUCKET", "leaked-from-the-developers-shell")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://leaked")

    scrub_ambient_env(monkeypatch)

    assert "AIMM_S3__BUCKET" not in os.environ
    assert "AWS_ENDPOINT_URL" not in os.environ


def test_the_scrubber_keeps_the_integration_rig_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: scrubbing these made the whole MinIO gate pass vacuously."""
    monkeypatch.setenv("AIMM_IT_ENDPOINT", "http://localhost:9800")
    monkeypatch.setenv("AIMM_IT_ACCESS_KEY", "aimm-it-user")
    monkeypatch.setenv("AIMM_IT_SECRET_KEY", "aimm-it-user-secret")

    scrub_ambient_env(monkeypatch)

    assert os.environ["AIMM_IT_ENDPOINT"] == "http://localhost:9800"
    assert os.environ["AIMM_IT_ACCESS_KEY"] == "aimm-it-user"
    assert os.environ["AIMM_IT_SECRET_KEY"] == "aimm-it-user-secret"


def test_settings_tolerate_the_rig_variables_being_present() -> None:
    """The exemption is only safe because `Settings` ignores `AIMM_IT_*` entirely.

    `Settings` uses `env_prefix="AIMM_"` with `extra="forbid"`, so the obvious worry is
    that a surviving `AIMM_IT_ENDPOINT` maps to a forbidden extra field. It does not:
    nested delimiter parsing only claims names that match a declared field.
    """
    from bg_ai_model_management.config.models import S3Settings, Settings

    os.environ["AIMM_IT_ENDPOINT"] = "http://localhost:9800"
    try:
        settings = Settings(s3=S3Settings(bucket="aimm-test"))
    finally:
        os.environ.pop("AIMM_IT_ENDPOINT", None)
    assert settings.s3.bucket == "aimm-test"
