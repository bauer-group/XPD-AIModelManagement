"""Settings models: size parsing, bounds, backend resolution and `extra="forbid"`.

The bounds are not decoration. `part_size` below 5 MiB is rejected by S3 for every
part but the last, and a `workers` value of 0 would hang the engine on an empty pool —
both are far cheaper to catch here than at the third hour of a 400 GB transfer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from bg_ai_model_management.config.models import (
    MAX_PART_SIZE,
    MIN_PART_SIZE,
    PORTABLE_STORAGE_CLASSES,
    PRESET_DEFAULTS,
    BackendPreset,
    HubSettings,
    S3Settings,
    Settings,
    TransferSettings,
    parse_size,
)
from bg_ai_model_management.errors import ConfigError
from bg_ai_model_management.tools.hfbackup.types import TransferMode

#: Every preset except `auto`, which resolves from the endpoint heuristic instead.
NAMED_PRESETS: list[BackendPreset] = sorted(PRESET_DEFAULTS, key=lambda item: item.value)

# --------------------------------------------------------------------- parse_size


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1024, 1024),
        (0, 0),
        ("0", 0),
        ("1024", 1024),
        ("512K", 512 * 1024),
        ("512KB", 512 * 1024),
        ("512KiB", 512 * 1024),
        ("8M", 8 * 1024**2),
        ("8MB", 8 * 1024**2),
        ("8MiB", 8 * 1024**2),
        ("8mib", 8 * 1024**2),
        ("8 MiB", 8 * 1024**2),
        ("  8MiB  ", 8 * 1024**2),
        ("5G", 5 * 1024**3),
        ("5GiB", 5 * 1024**3),
        ("2TiB", 2 * 1024**4),
        ("1.5MiB", 1572864),
    ],
)
def test_parse_size_accepts_the_documented_forms(value: str | int, expected: int) -> None:
    assert parse_size(value) == expected


def test_metric_and_binary_spellings_are_deliberately_identical() -> None:
    """ "MB" and "MiB" both mean 1024**2 here. An operator sizing a transfer buffer
    means powers of two, and a silent 4.8% gap between the two spellings would be
    worse than the imprecision."""
    assert parse_size("8MB") == parse_size("8MiB")
    assert parse_size("5GB") == parse_size("5GiB")


@pytest.mark.parametrize(
    "value", ["abc", "", "   ", "-1", "8XB", "8.5.5M", "MiB", "8 Mi B", "0x10"]
)
def test_parse_size_rejects_garbage(value: str) -> None:
    with pytest.raises(ConfigError):
        parse_size(value)


def test_parse_size_rejects_a_negative_integer() -> None:
    with pytest.raises(ConfigError):
        parse_size(-1)


@pytest.mark.parametrize("value", [True, False])
def test_parse_size_rejects_a_bool(value: bool) -> None:
    """`bool` is an `int` subclass; `part_size: true` must not silently mean 1 byte."""
    with pytest.raises(ConfigError):
        parse_size(value)


def test_parse_size_error_does_not_echo_a_huge_input() -> None:
    with pytest.raises(ConfigError) as caught:
        parse_size("nonsense")
    assert "nonsense" in str(caught.value)


# -------------------------------------------------------------------- S3Settings


def test_bucket_is_required() -> None:
    with pytest.raises(ValidationError):
        S3Settings()


def test_unknown_keys_are_rejected() -> None:
    """`extra="forbid"` turns a profile typo into an error instead of a no-op."""
    with pytest.raises(ValidationError):
        S3Settings(bucket="b", buckets="typo")


def test_defaults_match_the_contract() -> None:
    settings = S3Settings(bucket="hf-backup")
    assert settings.preset is BackendPreset.auto
    assert settings.region == "us-east-1"
    assert settings.prefix == "aimm"
    assert settings.addressing_style == "auto"
    assert settings.checksum_calculation == "auto"
    assert settings.ensure_bucket is False
    assert settings.verify_tls is True
    assert settings.probe is True
    assert settings.max_attempts == 10
    assert settings.connect_timeout == 15.0
    assert settings.read_timeout == 120.0
    assert settings.storage_class is None
    assert settings.server_side_encryption is None


@pytest.mark.parametrize(
    ("given", "expected"), [("/aimm/", "aimm"), ("aimm/", "aimm"), ("/a/b", "a/b"), (" x ", "x")]
)
def test_prefix_slashes_are_trimmed(given: str, expected: str) -> None:
    """A stray slash would produce `//` in every key built from this prefix."""
    assert S3Settings(bucket="b", prefix=given).prefix == expected


@pytest.mark.parametrize("given", ["", "/", "///", "   "])
def test_an_empty_prefix_is_rejected(given: str) -> None:
    with pytest.raises(ValidationError):
        S3Settings(bucket="b", prefix=given)


@pytest.mark.parametrize("value", [0, 31, -1])
def test_max_attempts_bounds(value: int) -> None:
    with pytest.raises(ValidationError):
        S3Settings(bucket="b", max_attempts=value)


@pytest.mark.parametrize("field", ["connect_timeout", "read_timeout"])
def test_timeouts_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError):
        S3Settings(bucket="b", **{field: 0})


def test_server_side_encryption_is_a_closed_set() -> None:
    assert S3Settings(bucket="b", server_side_encryption="AES256").server_side_encryption
    assert S3Settings(bucket="b", server_side_encryption="aws:kms").server_side_encryption
    with pytest.raises(ValidationError):
        S3Settings(bucket="b", server_side_encryption="rot13")


def test_credentials_are_secret_strings() -> None:
    settings = S3Settings(
        bucket="b",
        access_key_id="AKIA-visible-id",
        secret_access_key="the-secret",
        session_token="the-session",
    )
    assert isinstance(settings.secret_access_key, SecretStr)
    for value in ("the-secret", "the-session", "AKIA-visible-id"):
        assert value not in repr(settings)


def test_ca_bundle_is_a_path() -> None:
    assert S3Settings(bucket="b", ca_bundle="/etc/ssl/ca.pem").ca_bundle == Path("/etc/ssl/ca.pem")


# ----------------------------------------------------------- backend resolution


def test_auto_addressing_uses_path_style_for_a_custom_endpoint() -> None:
    """House ruling: an explicit endpoint means self-hosted MinIO or Ceph RGW."""
    settings = S3Settings(bucket="b", endpoint_url="https://s3.bauer-group.com")
    assert settings.resolved_addressing_style() == "path"


def test_auto_addressing_uses_virtual_host_without_an_endpoint() -> None:
    assert S3Settings(bucket="b").resolved_addressing_style() == "virtual"


@pytest.mark.parametrize("preset", NAMED_PRESETS)
def test_each_preset_resolves_to_its_documented_defaults(preset: BackendPreset) -> None:
    settings = S3Settings(bucket="b", preset=preset)
    assert settings.resolved_addressing_style() == PRESET_DEFAULTS[preset]["addressing_style"]
    assert (
        settings.resolved_checksum_calculation() == PRESET_DEFAULTS[preset]["checksum_calculation"]
    )


def test_a_preset_beats_the_endpoint_heuristic() -> None:
    """`preset: aws` with an endpoint (a VPC endpoint, say) still means virtual-host."""
    settings = S3Settings(bucket="b", preset=BackendPreset.aws, endpoint_url="https://vpce")
    assert settings.resolved_addressing_style() == "virtual"


@pytest.mark.parametrize("style", ["path", "virtual"])
def test_an_explicit_addressing_style_beats_everything(style: str) -> None:
    settings = S3Settings(
        bucket="b",
        preset=BackendPreset.aws,
        endpoint_url="https://s3.internal",
        addressing_style=style,
    )
    assert settings.resolved_addressing_style() == style


@pytest.mark.parametrize("value", ["when_supported", "when_required"])
def test_an_explicit_checksum_calculation_beats_the_preset(value: str) -> None:
    settings = S3Settings(
        bucket="b",
        preset=BackendPreset.minio,
        checksum_calculation=value,
    )
    assert settings.resolved_checksum_calculation() == value


def test_auto_preset_falls_back_to_when_supported() -> None:
    assert S3Settings(bucket="b").resolved_checksum_calculation() == "when_supported"


def test_the_preset_table_covers_every_named_backend() -> None:
    """`auto` is the only preset without a row; a new backend must add one."""
    assert set(PRESET_DEFAULTS) == set(BackendPreset) - {BackendPreset.auto}


def test_portable_storage_classes_are_the_minio_accepted_pair() -> None:
    assert {"STANDARD", "REDUCED_REDUNDANCY"} == PORTABLE_STORAGE_CLASSES


# --------------------------------------------------------------- TransferSettings


def test_transfer_defaults_match_the_contract() -> None:
    settings = TransferSettings()
    assert settings.mode is TransferMode.auto
    assert settings.workers == 8
    assert settings.part_size == 8 * 1024**2
    assert settings.inline_max == 8 * 1024**2
    assert settings.max_part_memory == 64 * 1024**2
    assert settings.disk_reserve == 5 * 1024**3
    assert settings.staging_dir is None
    assert settings.max_disk_bytes is None
    assert settings.prefer_xet is False
    assert settings.fail_fast is False
    assert settings.stream_failure_downgrade == 2
    assert settings.max_attempts == 5
    assert settings.max_wait == 60.0


@pytest.mark.parametrize(
    "field", ["part_size", "inline_max", "max_part_memory", "disk_reserve", "max_disk_bytes"]
)
def test_every_size_field_accepts_a_human_string(field: str) -> None:
    settings = TransferSettings(**{field: "16MiB"})
    assert getattr(settings, field) == 16 * 1024**2


@pytest.mark.parametrize("field", ["max_disk_bytes", "staging_dir"])
def test_optional_fields_accept_none(field: str) -> None:
    assert getattr(TransferSettings(**{field: None}), field) is None


@pytest.mark.parametrize("value", [0, 65, -1])
def test_workers_bounds(value: int) -> None:
    with pytest.raises(ValidationError):
        TransferSettings(workers=value)


@pytest.mark.parametrize("value", [1, 8, 64])
def test_workers_accepts_the_permitted_range(value: int) -> None:
    assert TransferSettings(workers=value).workers == value


def test_part_size_below_the_s3_minimum_is_rejected() -> None:
    """S3 rejects any part but the last below 5 MiB; failing here saves an upload."""
    with pytest.raises(ValidationError):
        TransferSettings(part_size=MIN_PART_SIZE - 1)


def test_part_size_above_the_s3_maximum_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TransferSettings(part_size=MAX_PART_SIZE + 1)


@pytest.mark.parametrize("value", [MIN_PART_SIZE, 8 * 1024**2, MAX_PART_SIZE])
def test_part_size_accepts_the_permitted_range(value: int) -> None:
    assert TransferSettings(part_size=value).part_size == value


def test_part_size_bounds_are_the_s3_limits() -> None:
    assert MIN_PART_SIZE == 5 * 1024**2
    assert MAX_PART_SIZE == 5 * 1024**3


def test_a_malformed_size_string_raises_a_config_error() -> None:
    """`parse_size` raises `ConfigError`, which is already the CLI's exit code 2."""
    with pytest.raises(ConfigError):
        TransferSettings(part_size="not-a-size")


@pytest.mark.parametrize("value", [8.5, [8], {"size": 8}])
def test_a_size_field_rejects_a_type_it_cannot_interpret(value: Any) -> None:
    """Non-string, non-integer values pass through to pydantic, which rejects them."""
    with pytest.raises((ValidationError, ConfigError)):
        TransferSettings(part_size=value)


def test_transfer_forbids_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        TransferSettings(worker=4)


def test_stream_failure_downgrade_must_be_at_least_one() -> None:
    with pytest.raises(ValidationError):
        TransferSettings(stream_failure_downgrade=0)


# -------------------------------------------------------------------- HubSettings


def test_hub_defaults() -> None:
    settings = HubSettings()
    assert settings.endpoint == "https://huggingface.co"
    assert settings.token is None
    assert settings.chunk_size == 1 << 20


def test_hub_token_accepts_the_hf_token_alias() -> None:
    """`HF_TOKEN` is the ecosystem's own name and carries no `AIMM_` prefix."""
    settings = HubSettings(HF_TOKEN="hf_abc")
    assert settings.token is not None
    assert settings.token.get_secret_value() == "hf_abc"
    assert "hf_abc" not in repr(settings)


def test_hub_chunk_size_accepts_a_human_string() -> None:
    assert HubSettings(chunk_size="2MiB").chunk_size == 2 * 1024**2


def test_hub_forbids_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        HubSettings(endpoints="typo")


# ----------------------------------------------------------------------- Settings


def test_settings_env_configuration_matches_the_contract() -> None:
    config: Any = Settings.model_config
    assert config["env_prefix"] == "AIMM_"
    assert config["env_nested_delimiter"] == "__"
    assert config["case_sensitive"] is False
    assert config["extra"] == "forbid"


def test_settings_composes_the_sub_models(tmp_path: Path) -> None:
    settings = Settings(s3={"bucket": "hf-backup"}, _secrets_dir=tmp_path)
    assert settings.s3.bucket == "hf-backup"
    assert isinstance(settings.transfer, TransferSettings)
    assert isinstance(settings.hub, HubSettings)
    assert settings.log_level == "INFO"
    assert settings.log_format == "text"


# ── regressions ──────────────────────────────────────────────────────────────


def test_inline_max_above_the_single_put_limit_is_rejected() -> None:
    """Regression: `inline_max` was unbounded while `part_size` beside it was not.

    `transfer.inline_max: 6GiB` routed every file up to 6 GiB through the INLINE path:
    `read_bytes` buffered the whole body in RAM per worker and `put_small` sent it as one
    PutObject, which S3 rejects with EntityTooLarge — after the entire Hub download had
    already been paid for. Nothing between the config and the wire caught it.
    """
    with pytest.raises(ValidationError):
        TransferSettings(inline_max=MAX_PART_SIZE + 1)
    with pytest.raises(ValidationError):
        TransferSettings(inline_max="6GiB")


@pytest.mark.parametrize("value", [0, 1024, 8 * 1024**2, MAX_PART_SIZE])
def test_inline_max_accepts_the_permitted_range(value: int) -> None:
    """0 disables the inline path; MAX_PART_SIZE is S3's single-PUT ceiling."""
    assert TransferSettings(inline_max=value).inline_max == value


def test_inline_max_rejects_a_negative_value() -> None:
    """The `before` size coercion gets there first, raising ConfigError not ValidationError."""
    with pytest.raises(ConfigError):
        TransferSettings(inline_max=-1)


def test_hub_settings_carry_explicit_timeouts() -> None:
    """Regression: hub 1.x builds its client with `timeout=None`, so a stall hung forever."""
    settings = HubSettings()
    assert settings.connect_timeout > 0
    assert settings.read_timeout > 0
    tuned = HubSettings(connect_timeout=5.0, read_timeout=30.0)
    assert (tuned.connect_timeout, tuned.read_timeout) == (5.0, 30.0)


@pytest.mark.parametrize("field", ["connect_timeout", "read_timeout"])
def test_hub_timeouts_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError):
        HubSettings(**{field: 0})
