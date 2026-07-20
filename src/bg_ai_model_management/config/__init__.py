"""Configuration: validated models, layered loading, ``${ENV}`` interpolation."""

from __future__ import annotations

from .interpolation import ENV_PATTERN, interpolate
from .loader import (
    BACKEND_ENV,
    PROFILE_ENV,
    PROFILE_FILENAMES,
    InterpolatingYamlSource,
    find_profile,
    load_settings,
)
from .models import (
    PORTABLE_STORAGE_CLASSES,
    PRESET_DEFAULTS,
    BackendPreset,
    HubSettings,
    S3Settings,
    Settings,
    TransferSettings,
    parse_size,
)

__all__ = [
    "BACKEND_ENV",
    "ENV_PATTERN",
    "PORTABLE_STORAGE_CLASSES",
    "PRESET_DEFAULTS",
    "PROFILE_ENV",
    "PROFILE_FILENAMES",
    "BackendPreset",
    "HubSettings",
    "InterpolatingYamlSource",
    "S3Settings",
    "Settings",
    "TransferSettings",
    "find_profile",
    "interpolate",
    "load_settings",
    "parse_size",
]
