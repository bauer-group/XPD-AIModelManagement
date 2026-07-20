"""Validated settings models.

Every credential is a :class:`~pydantic.SecretStr`, whose ``repr``/``str`` render
as ``**********``. Nothing in this module may render a secret in a log, an error
or a model dump; use ``model_dump(mode="json")`` when a dump leaves the process,
because plain ``model_dump()`` returns the live ``SecretStr`` objects.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..errors import ConfigError
from ..tools.hfbackup.types import TransferMode

MIN_PART_SIZE: int = 5 * 1024**2
MAX_PART_SIZE: int = 5 * 1024**3

#: Where Docker/Kubernetes mounts secrets. Resolved to ``None`` when it is absent, which
#: is the normal case off-container: pydantic-settings warns once per ``Settings``
#: construction for a missing ``secrets_dir``, and that buried real warnings under
#: hundreds of lines of noise in test and CI output. A directory that does not exist
#: contributes no values either way, so this is behaviour-preserving.
DOCKER_SECRETS_DIR: str = "/run/secrets"


def _docker_secrets_dir() -> str | None:
    return DOCKER_SECRETS_DIR if Path(DOCKER_SECRETS_DIR).is_dir() else None


class BackendPreset(str, Enum):
    auto = "auto"
    minio = "minio"
    ceph_rgw = "ceph-rgw"
    aws = "aws"
    r2 = "r2"
    wasabi = "wasabi"


#: Per-backend defaults for the two settings that differ between object stores.
PRESET_DEFAULTS: dict[BackendPreset, dict[str, str]] = {
    BackendPreset.minio: {"addressing_style": "path", "checksum_calculation": "when_required"},
    BackendPreset.ceph_rgw: {"addressing_style": "path", "checksum_calculation": "when_required"},
    BackendPreset.aws: {"addressing_style": "virtual", "checksum_calculation": "when_supported"},
    BackendPreset.r2: {"addressing_style": "virtual", "checksum_calculation": "when_supported"},
    BackendPreset.wasabi: {"addressing_style": "virtual", "checksum_calculation": "when_required"},
}

#: Storage classes that are safe on every supported backend. MinIO's IsValid()
#: accepts only these two, so anything else must be opted into per backend.
PORTABLE_STORAGE_CLASSES: frozenset[str] = frozenset({"STANDARD", "REDUCED_REDUNDANCY"})

#: Suffixes are binary multiples throughout: "MB" and "MiB" both mean 1024**2.
#: Operators sizing a transfer buffer mean powers of two, and a silent 4.8%
#: difference between the two spellings would be worse than the imprecision.
_SIZE_UNITS: dict[str, int] = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "KIB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "MIB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "GIB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
    "TIB": 1024**4,
}

_SIZE_PATTERN: re.Pattern[str] = re.compile(r"^(\d+(?:\.\d+)?)\s*([A-Za-z]*)$")


def parse_size(value: str | int) -> int:
    """Parse ``"8MiB"`` / ``"5GiB"`` / ``"1024"`` / ``"512K"`` into a byte count.

    Raises:
        ConfigError: the value is not a non-negative size.
    """
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly.
        raise ConfigError(f"invalid size: {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ConfigError(f"size must not be negative: {value}")
        return value
    match = _SIZE_PATTERN.match(value.strip())
    if match is None:
        raise ConfigError(f"invalid size: {value!r}")
    number, unit = match.group(1), match.group(2).upper()
    if unit not in _SIZE_UNITS:
        raise ConfigError(f"unknown size unit {match.group(2)!r} in {value!r}")
    return int(float(number) * _SIZE_UNITS[unit])


def _coerce_size(value: Any) -> Any:
    """``field_validator(mode="before")`` helper: expand size strings, keep None."""
    if value is None:
        return None
    if isinstance(value, (str, int)):
        return parse_size(value)
    return value


class S3Settings(BaseModel):
    """Connection and behaviour of the S3-compatible destination."""

    model_config = ConfigDict(extra="forbid")

    preset: BackendPreset = BackendPreset.auto
    endpoint_url: str | None = None
    region: str = "us-east-1"
    bucket: str
    prefix: str = "aimm"
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None
    session_token: SecretStr | None = None
    addressing_style: Literal["auto", "path", "virtual"] = "auto"
    checksum_calculation: Literal["auto", "when_supported", "when_required"] = "auto"
    storage_class: str | None = None
    server_side_encryption: Literal["AES256", "aws:kms"] | None = None
    sse_kms_key_id: str | None = None
    ensure_bucket: bool = False
    max_attempts: int = Field(default=10, ge=1, le=30)
    connect_timeout: float = Field(default=15.0, gt=0)
    read_timeout: float = Field(default=120.0, gt=0)
    verify_tls: bool = True
    ca_bundle: Path | None = None
    probe: bool = True

    @field_validator("prefix")
    @classmethod
    def _strip_prefix_slashes(cls, value: str) -> str:
        """Trim surrounding slashes so key building never produces '//'."""
        stripped = value.strip().strip("/")
        if not stripped:
            raise ValueError("prefix must not be empty")
        return stripped

    def resolved_addressing_style(self) -> Literal["path", "virtual"]:
        """Resolve ``auto``: explicit setting > preset > custom-endpoint heuristic.

        Self-hosted MinIO and Ceph RGW are configured with an explicit endpoint
        and the house ruling is path-style for self-hosting; AWS, R2 and Wasabi
        expect virtual-host addressing.
        """
        if self.addressing_style != "auto":
            return self.addressing_style
        preset = PRESET_DEFAULTS.get(self.preset)
        if preset is not None:
            style = preset["addressing_style"]
            return "path" if style == "path" else "virtual"
        return "path" if self.endpoint_url else "virtual"

    def resolved_checksum_calculation(self) -> Literal["when_supported", "when_required"]:
        """Resolve ``auto`` from the preset, else ``when_supported``.

        A runtime capability probe may still downgrade this to ``when_required``.
        """
        if self.checksum_calculation != "auto":
            return self.checksum_calculation
        preset = PRESET_DEFAULTS.get(self.preset)
        if preset is not None:
            value = preset["checksum_calculation"]
            return "when_required" if value == "when_required" else "when_supported"
        return "when_supported"


class TransferSettings(BaseModel):
    """Concurrency, buffering and staging behaviour of the transfer engine."""

    model_config = ConfigDict(extra="forbid")

    mode: TransferMode = TransferMode.auto
    workers: int = Field(default=8, ge=1, le=64)
    part_size: int = Field(default=8 * 1024**2, ge=MIN_PART_SIZE, le=MAX_PART_SIZE)
    #: Bounded by MAX_PART_SIZE because the INLINE path is a single PutObject and S3
    #: rejects a single PUT above 5 GiB with EntityTooLarge — after the whole Hub
    #: download has already been paid for. `0` disables the inline path entirely.
    inline_max: int = Field(default=8 * 1024**2, ge=0, le=MAX_PART_SIZE)
    max_part_memory: int = 64 * 1024**2
    staging_dir: Path | None = None
    max_disk_bytes: int | None = None
    disk_reserve: int = 5 * 1024**3
    prefer_xet: bool = False
    stream_failure_downgrade: int = Field(default=2, ge=1)
    max_attempts: int = Field(default=5, ge=1, le=20)
    max_wait: float = Field(default=60.0, gt=0)
    fail_fast: bool = False

    @field_validator(
        "part_size",
        "inline_max",
        "max_part_memory",
        "max_disk_bytes",
        "disk_reserve",
        mode="before",
    )
    @classmethod
    def _parse_sizes(cls, value: Any) -> Any:
        return _coerce_size(value)


class HubSettings(BaseModel):
    """Hugging Face Hub endpoint and credentials."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str = "https://huggingface.co"
    #: ``None`` means "fall back to huggingface_hub.get_token()" at the call site.
    token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("token", "HF_TOKEN"),
    )
    chunk_size: int = 1 << 20
    #: huggingface_hub 1.x builds its shared client with ``timeout=None``, so a stalled
    #: CDN connection would block a worker forever. These are passed explicitly on every
    #: raw streaming request; ``HF_HUB_DOWNLOAD_TIMEOUT`` only reaches the Hub's own
    #: downloader and never this tool's stream.
    connect_timeout: float = Field(default=15.0, gt=0)
    read_timeout: float = Field(default=120.0, gt=0)

    @field_validator("chunk_size", mode="before")
    @classmethod
    def _parse_chunk_size(cls, value: Any) -> Any:
        return _coerce_size(value)


class Settings(BaseSettings):
    """Root settings document. Built by ``bg_ai_model_management.config.loader.load_settings``."""

    model_config = SettingsConfigDict(
        env_prefix="AIMM_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="forbid",
        secrets_dir=_docker_secrets_dir(),
    )

    s3: S3Settings
    transfer: TransferSettings = TransferSettings()
    hub: HubSettings = HubSettings()
    log_level: str = "INFO"
    log_format: str = "text"
