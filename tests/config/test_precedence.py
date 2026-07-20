"""THE CANARY. pydantic-settings orders its source tuple highest-priority FIRST.

Reversing that tuple inverts the entire configuration chain and raises nothing: the
tool simply starts ignoring `--bucket` in favour of whatever is in the profile file.
There is no type error, no warning, and no symptom until an operator writes a backup
into the wrong bucket. Every test below pins one rung of the ladder

    CLI  >  env  >  docker secret  >  profile YAML  >  model default

by setting the *same* field at two adjacent layers and asserting the higher one wins.
Adjacent pairs are used deliberately: asserting only "CLI beats default" would still
pass with a completely scrambled middle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bg_ai_model_management.config.loader import load_settings
from bg_ai_model_management.config.models import Settings

pytestmark = pytest.mark.usefixtures("clean_aimm_env")


def write_profile(path: Path, body: str) -> Path:
    profile = path / "aimm.yaml"
    profile.write_text(body, encoding="utf-8")
    return profile


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    """A profile that sets every field the precedence tests contend over."""
    return write_profile(
        tmp_path,
        """
        s3:
          bucket: yaml-bucket
          region: yaml-region
          prefix: yaml-prefix
        transfer:
          workers: 3
        log_level: WARNING
        """.replace("        ", ""),
    )


# ------------------------------------------------------------------ adjacent rungs


def test_cli_override_beats_the_environment(profile: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIMM_S3__BUCKET", "env-bucket")
    settings = load_settings(profile=profile, overrides={"s3.bucket": "cli-bucket"})
    assert settings.s3.bucket == "cli-bucket"


def test_environment_beats_the_docker_secret(
    profile: Path, secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (secrets_dir / "AIMM_s3__bucket").write_text("secret-bucket", encoding="utf-8")
    monkeypatch.setenv("AIMM_S3__BUCKET", "env-bucket")
    settings = load_settings(profile=profile)
    assert settings.s3.bucket == "env-bucket"


def test_docker_secret_beats_the_profile(profile: Path, secrets_dir: Path) -> None:
    (secrets_dir / "AIMM_s3__prefix").write_text("secret-prefix", encoding="utf-8")
    settings = load_settings(profile=profile)
    assert settings.s3.prefix == "secret-prefix"


def test_profile_beats_the_model_default(profile: Path) -> None:
    assert Settings.model_fields["transfer"].default.workers == 8
    settings = load_settings(profile=profile)
    assert settings.transfer.workers == 3


def test_the_model_default_applies_when_nothing_else_sets_the_field(profile: Path) -> None:
    settings = load_settings(profile=profile)
    assert settings.s3.max_attempts == 10
    assert settings.transfer.mode.value == "auto"
    assert settings.hub.endpoint == "https://huggingface.co"


# ------------------------------------------------------------- the whole ladder at once


def test_every_layer_wins_over_every_layer_below_it(
    profile: Path, secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One call, four contended fields, one per rung. If the source tuple is reversed
    this fails on the first assertion instead of quietly shipping."""
    (secrets_dir / "AIMM_s3__prefix").write_text("secret-prefix", encoding="utf-8")
    (secrets_dir / "AIMM_s3__region").write_text("secret-region", encoding="utf-8")
    (secrets_dir / "AIMM_s3__bucket").write_text("secret-bucket", encoding="utf-8")
    monkeypatch.setenv("AIMM_S3__REGION", "env-region")
    monkeypatch.setenv("AIMM_S3__BUCKET", "env-bucket")

    settings = load_settings(profile=profile, overrides={"s3.bucket": "cli-bucket"})

    assert settings.s3.bucket == "cli-bucket"  # CLI    over env, secret and yaml
    assert settings.s3.region == "env-region"  # env    over secret and yaml
    assert settings.s3.prefix == "secret-prefix"  # secret over yaml
    assert settings.transfer.workers == 3  # yaml   over default
    assert settings.s3.max_attempts == 10  # default when nobody sets it


def test_a_lower_layer_still_supplies_fields_the_higher_one_omits(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Layers merge per field; a single `AIMM_S3__REGION` must not wipe the profile's
    bucket by replacing the whole `s3` object."""
    monkeypatch.setenv("AIMM_S3__REGION", "env-region")
    settings = load_settings(profile=profile, overrides={"s3.prefix": "cli-prefix"})
    assert settings.s3.region == "env-region"
    assert settings.s3.prefix == "cli-prefix"
    assert settings.s3.bucket == "yaml-bucket"
    assert settings.log_level == "WARNING"


def test_an_override_that_is_absent_does_not_shadow_the_profile(profile: Path) -> None:
    """`overrides` must carry only what the user actually typed. This is the caller's
    contract, asserted here because violating it silently disables the profile."""
    settings = load_settings(profile=profile, overrides={})
    assert settings.s3.bucket == "yaml-bucket"


def test_nested_dotted_overrides_reach_nested_models(profile: Path) -> None:
    settings = load_settings(
        profile=profile,
        overrides={"transfer.workers": 16, "hub.endpoint": "https://hub.internal"},
    )
    assert settings.transfer.workers == 16
    assert settings.hub.endpoint == "https://hub.internal"


# --------------------------------------------------------------- the nested-secret trap


def test_a_nested_docker_secret_reaches_a_nested_model(profile: Path, secrets_dir: Path) -> None:
    """Plain `secrets_dir` silently ignores `s3__secret_access_key` and leaves the
    field `None`, which then becomes an anonymous S3 request. This asserts that
    `NestedSecretsSettingsSource` is actually wired in."""
    (secrets_dir / "AIMM_s3__secret_access_key").write_text("s3cr3t-from-file", encoding="utf-8")
    settings = load_settings(profile=profile)
    assert settings.s3.secret_access_key is not None
    assert settings.s3.secret_access_key.get_secret_value() == "s3cr3t-from-file"


def test_the_secret_filename_carries_the_env_prefix(profile: Path, secrets_dir: Path) -> None:
    """`env_prefix` applies to secret filenames too: without `AIMM_` it is ignored."""
    (secrets_dir / "s3__secret_access_key").write_text("wrong-name", encoding="utf-8")
    settings = load_settings(profile=profile)
    assert settings.s3.secret_access_key is None


def test_a_missing_secrets_directory_is_not_an_error(profile: Path) -> None:
    """`/run/secrets` does not exist outside a container; that is the normal case."""
    settings = load_settings(profile=profile)
    assert settings.s3.bucket == "yaml-bucket"


# ------------------------------------------------------------------- the HF_TOKEN rung


def test_hf_token_is_read_without_the_aimm_prefix(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_from_ecosystem")
    settings = load_settings(profile=profile)
    assert settings.hub.token is not None
    assert settings.hub.token.get_secret_value() == "hf_from_ecosystem"


def test_an_explicit_aimm_hub_token_beats_hf_token(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_from_ecosystem")
    monkeypatch.setenv("AIMM_HUB__TOKEN", "hf_from_aimm")
    settings = load_settings(profile=profile)
    assert settings.hub.token is not None
    assert settings.hub.token.get_secret_value() == "hf_from_aimm"


def test_hub_token_is_none_when_nothing_supplies_one(profile: Path) -> None:
    """`None` is meaningful: it tells `HubSource` to fall back to `get_token()`."""
    settings = load_settings(profile=profile)
    assert settings.hub.token is None


# --------------------------------------------------------------------- secret hygiene


def test_a_credential_never_appears_in_a_repr(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIMM_S3__SECRET_ACCESS_KEY", "leak-me-if-you-can")
    settings = load_settings(profile=profile)
    assert "leak-me-if-you-can" not in repr(settings)
    assert "leak-me-if-you-can" not in repr(settings.s3)
    assert "leak-me-if-you-can" not in str(settings.s3.secret_access_key)


def test_a_credential_never_appears_in_a_json_dump(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--json` output and any diagnostic dump go through this path."""
    monkeypatch.setenv("AIMM_S3__SECRET_ACCESS_KEY", "leak-me-if-you-can")
    monkeypatch.setenv("AIMM_S3__SESSION_TOKEN", "session-leak")
    monkeypatch.setenv("HF_TOKEN", "hub-leak")
    settings = load_settings(profile=profile)
    dumped = settings.model_dump_json()
    for secret in ("leak-me-if-you-can", "session-leak", "hub-leak"):
        assert secret not in dumped


def test_a_credential_survives_the_round_trip_it_is_meant_to_make(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redaction must not be achieved by losing the value."""
    monkeypatch.setenv("AIMM_S3__SECRET_ACCESS_KEY", "real-value")
    settings = load_settings(profile=profile)
    assert settings.s3.secret_access_key is not None
    assert settings.s3.secret_access_key.get_secret_value() == "real-value"


# ------------------------------------------------------------------ environment shapes


def test_the_nested_delimiter_is_a_double_underscore(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIMM_TRANSFER__WORKERS", "12")
    assert load_settings(profile=profile).transfer.workers == 12


def test_environment_names_are_case_insensitive(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("aimm_s3__region", "lowercase-region")
    assert load_settings(profile=profile).s3.region == "lowercase-region"


def test_a_size_string_from_the_environment_is_parsed(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIMM_TRANSFER__PART_SIZE", "16MiB")
    assert load_settings(profile=profile).transfer.part_size == 16 * 1024**2


def test_a_boolean_from_the_environment_is_parsed(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIMM_S3__PROBE", "false")
    monkeypatch.setenv("AIMM_S3__ENSURE_BUCKET", "true")
    settings = load_settings(profile=profile)
    assert settings.s3.probe is False
    assert settings.s3.ensure_bucket is True


def test_an_unrelated_aimm_it_variable_does_not_trip_extra_forbid(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The integration rig uses the `AIMM_IT_*` namespace. The env source resolves
    declared fields only, so these must be ignored rather than rejected."""
    monkeypatch.setenv("AIMM_IT_ENDPOINT", "http://localhost:9000")
    monkeypatch.setenv("AIMM_IT_BUCKET", "rig-bucket")
    assert load_settings(profile=profile).s3.bucket == "yaml-bucket"
