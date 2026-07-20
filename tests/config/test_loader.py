"""Profile discovery, backend selection and the failure messages.

The rule that shapes this file: every way a profile can be wrong must produce a
`ConfigError` with a message an operator can act on, and none of those messages may
contain a credential — pydantic's own `str(ValidationError)` embeds `input_value`,
which for a secret field would print it in plaintext.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bg_ai_model_management.config.loader import (
    BACKEND_ENV,
    PROFILE_ENV,
    PROFILE_FILENAMES,
    find_profile,
    load_settings,
)
from bg_ai_model_management.errors import ConfigError

pytestmark = pytest.mark.usefixtures("clean_aimm_env")

MULTI_BACKEND_PROFILE = """
default_backend: minio

backends:
  minio:
    preset: minio
    endpoint_url: https://eu-north1.s3.bauer-group.com
    region: eu-north1
    bucket: hf-backup
    access_key_id: ${MINIO_ACCESS_KEY}
    secret_access_key: ${MINIO_SECRET_KEY}
  aws:
    preset: aws
    region: eu-central-1
    bucket: bauer-hf-archive

transfer:
  workers: 4
  part_size: 16MiB

hub:
  endpoint: https://huggingface.co

log_level: DEBUG
"""


#: Named backends AND a plain `s3` block in one profile.
MIXED_PROFILE = """
backends:
  a:
    bucket: backend-a
  b:
    bucket: backend-b

s3:
  bucket: top-level-bucket
"""


def write(path: Path, body: str, name: str = "aimm.yaml") -> Path:
    target = path / name
    target.write_text(body, encoding="utf-8")
    return target


@pytest.fixture
def minio_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIO_ACCESS_KEY", "the-access-key")
    monkeypatch.setenv("MINIO_SECRET_KEY", "the-secret-key")


# ---------------------------------------------------------------- find_profile


def test_an_explicit_profile_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = write(tmp_path, "s3:\n  bucket: b\n", name="custom.yaml")
    monkeypatch.setenv(PROFILE_ENV, str(write(tmp_path, "s3:\n  bucket: c\n", name="env.yaml")))
    assert find_profile(explicit) == explicit


def test_a_missing_explicit_profile_raises(tmp_path: Path) -> None:
    """Silently falling back would back up to a bucket the operator did not name."""
    with pytest.raises(ConfigError) as caught:
        find_profile(tmp_path / "nope.yaml")
    assert "nope.yaml" in str(caught.value)


def test_the_profile_environment_variable_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = write(tmp_path, "s3:\n  bucket: b\n", name="env.yaml")
    monkeypatch.setenv(PROFILE_ENV, str(profile))
    assert find_profile(cwd=tmp_path) == profile


def test_a_dangling_profile_environment_variable_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROFILE_ENV, str(tmp_path / "missing.yaml"))
    with pytest.raises(ConfigError) as caught:
        find_profile(cwd=tmp_path)
    assert PROFILE_ENV in str(caught.value)


@pytest.mark.parametrize("name", PROFILE_FILENAMES)
def test_a_profile_in_the_working_directory_is_discovered(tmp_path: Path, name: str) -> None:
    profile = write(tmp_path, "s3:\n  bucket: b\n", name=name)
    assert find_profile(cwd=tmp_path) == profile


def test_yaml_is_preferred_over_yml(tmp_path: Path) -> None:
    yaml_file = write(tmp_path, "s3:\n  bucket: a\n", name="aimm.yaml")
    write(tmp_path, "s3:\n  bucket: b\n", name="aimm.yml")
    assert find_profile(cwd=tmp_path) == yaml_file


def test_no_profile_anywhere_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running with nothing but environment variables is a supported CI shape."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    assert find_profile(cwd=tmp_path) is None


def test_the_user_config_directory_is_the_last_resort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "userconfig"
    (root / "aimm").mkdir(parents=True)
    profile = root / "aimm" / "config.yaml"
    profile.write_text("s3:\n  bucket: b\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    monkeypatch.setenv("APPDATA", str(root))
    assert find_profile(cwd=tmp_path / "elsewhere") == profile


def test_a_directory_is_not_a_profile(tmp_path: Path) -> None:
    (tmp_path / "aimm.yaml").mkdir()
    with pytest.raises(ConfigError):
        find_profile(tmp_path / "aimm.yaml")


# ------------------------------------------------------------- backend selection


def test_default_backend_is_used_when_none_is_named(tmp_path: Path, minio_env: None) -> None:
    settings = load_settings(profile=write(tmp_path, MULTI_BACKEND_PROFILE))
    assert settings.s3.bucket == "hf-backup"
    assert settings.s3.region == "eu-north1"


def test_an_explicit_backend_beats_default_backend(tmp_path: Path, minio_env: None) -> None:
    settings = load_settings(profile=write(tmp_path, MULTI_BACKEND_PROFILE), backend="aws")
    assert settings.s3.bucket == "bauer-hf-archive"
    assert settings.s3.region == "eu-central-1"


def test_the_backend_environment_variable_is_honoured(
    tmp_path: Path, minio_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(BACKEND_ENV, "aws")
    settings = load_settings(profile=write(tmp_path, MULTI_BACKEND_PROFILE))
    assert settings.s3.bucket == "bauer-hf-archive"


def test_an_explicit_backend_beats_the_environment_variable(
    tmp_path: Path, minio_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(BACKEND_ENV, "aws")
    settings = load_settings(profile=write(tmp_path, MULTI_BACKEND_PROFILE), backend="minio")
    assert settings.s3.bucket == "hf-backup"


def test_an_unknown_backend_name_lists_the_known_ones(tmp_path: Path, minio_env: None) -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings(profile=write(tmp_path, MULTI_BACKEND_PROFILE), backend="ceph")
    message = str(caught.value)
    assert "ceph" in message
    assert "minio" in message and "aws" in message


def test_a_single_backend_needs_no_selection(tmp_path: Path) -> None:
    body = "backends:\n  only:\n    bucket: sole-bucket\n"
    assert load_settings(profile=write(tmp_path, body)).s3.bucket == "sole-bucket"


def test_several_backends_without_a_selection_is_refused(tmp_path: Path) -> None:
    """Guessing here would write a backup into an arbitrary bucket."""
    body = "backends:\n  a:\n    bucket: a\n  b:\n    bucket: b\n"
    with pytest.raises(ConfigError) as caught:
        load_settings(profile=write(tmp_path, body))
    assert BACKEND_ENV in str(caught.value)


def test_a_top_level_s3_block_wins_when_no_backend_is_selected(tmp_path: Path) -> None:
    """A profile can carry named backends *and* a plain `s3` block; with nothing
    selected the plain block is the unambiguous answer, so it is used rather than a
    backend being guessed."""
    assert load_settings(profile=write(tmp_path, MIXED_PROFILE)).s3.bucket == "top-level-bucket"


def test_a_named_backend_still_beats_a_top_level_s3_block(tmp_path: Path) -> None:
    settings = load_settings(profile=write(tmp_path, MIXED_PROFILE), backend="b")
    assert settings.s3.bucket == "backend-b"


def test_a_top_level_s3_block_needs_no_backends_key(tmp_path: Path) -> None:
    body = "s3:\n  bucket: plain-bucket\n  region: eu-west-1\n"
    assert load_settings(profile=write(tmp_path, body)).s3.bucket == "plain-bucket"


def test_an_empty_backends_mapping_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(profile=write(tmp_path, "backends: {}\ns3:\n  bucket: b\n"))


def test_the_backends_key_is_consumed_and_never_reaches_the_model(
    tmp_path: Path, minio_env: None
) -> None:
    """`Settings` has `extra="forbid"`; a leftover `backends` key would be a hard error."""
    settings = load_settings(profile=write(tmp_path, MULTI_BACKEND_PROFILE))
    assert not hasattr(settings, "backends")
    assert not hasattr(settings, "default_backend")


def test_non_backend_sections_survive_backend_selection(tmp_path: Path, minio_env: None) -> None:
    settings = load_settings(profile=write(tmp_path, MULTI_BACKEND_PROFILE))
    assert settings.transfer.workers == 4
    assert settings.transfer.part_size == 16 * 1024**2
    assert settings.log_level == "DEBUG"


# ------------------------------------------------------------------ interpolation


def test_profile_references_are_expanded(tmp_path: Path, minio_env: None) -> None:
    settings = load_settings(profile=write(tmp_path, MULTI_BACKEND_PROFILE))
    assert settings.s3.secret_access_key is not None
    assert settings.s3.secret_access_key.get_secret_value() == "the-secret-key"


def test_a_missing_variable_in_the_selected_backend_raises(tmp_path: Path) -> None:
    """Without this, `secret_access_key` becomes "" and every request goes anonymous."""
    with pytest.raises(ConfigError) as caught:
        load_settings(profile=write(tmp_path, MULTI_BACKEND_PROFILE))
    assert "MINIO_ACCESS_KEY" in str(caught.value)


def test_an_unselected_backend_may_reference_unset_variables(tmp_path: Path) -> None:
    """Selection happens before interpolation on purpose: a host that only uses AWS
    must not be blocked by the MinIO entry's unset variables."""
    settings = load_settings(profile=write(tmp_path, MULTI_BACKEND_PROFILE), backend="aws")
    assert settings.s3.bucket == "bauer-hf-archive"


# ---------------------------------------------------------------- failure surface


def test_a_malformed_profile_raises_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(profile=write(tmp_path, "s3: [this is: not: a mapping\n"))


def test_an_unknown_profile_key_is_rejected(tmp_path: Path) -> None:
    """`extra="forbid"` turns `prefx:` into an error instead of a silent default."""
    with pytest.raises(ConfigError) as caught:
        load_settings(profile=write(tmp_path, "s3:\n  bucket: b\n  prefx: typo\n"))
    assert "prefx" in str(caught.value)


def test_a_missing_required_field_names_the_location(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings(profile=write(tmp_path, "s3:\n  region: eu-north1\n"))
    assert "s3.bucket" in str(caught.value)


def test_a_missing_s3_section_names_the_section(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings(profile=write(tmp_path, "transfer:\n  workers: 4\n"))
    assert "s3" in str(caught.value)


def test_a_validation_error_never_echoes_the_offending_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pydantic's own `str(ValidationError)` embeds `input_value`. For a credential
    field that would print the secret into the terminal and into CI logs."""
    monkeypatch.setenv("AIMM_S3__SECRET_ACCESS_KEY", "a-real-looking-secret")
    with pytest.raises(ConfigError) as caught:
        load_settings(profile=write(tmp_path, "s3:\n  bucket: b\n  probe: not-a-bool\n"))
    message = str(caught.value)
    assert "a-real-looking-secret" not in message
    assert "s3.probe" in message


def test_loading_without_any_profile_uses_the_environment_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("AIMM_S3__BUCKET", "env-only-bucket")
    settings = load_settings()
    assert settings.s3.bucket == "env-only-bucket"


def test_no_configuration_at_all_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    with pytest.raises(ConfigError):
        load_settings()


def test_conflicting_override_paths_are_refused(tmp_path: Path) -> None:
    profile = write(tmp_path, "s3:\n  bucket: b\n")
    with pytest.raises(ConfigError):
        load_settings(profile=profile, overrides={"s3": "scalar", "s3.bucket": "x"})


def test_an_empty_profile_is_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIMM_S3__BUCKET", "env-bucket")
    assert load_settings(profile=write(tmp_path, "")).s3.bucket == "env-bucket"
