"""Layered settings loader.

Precedence, highest wins:

1. CLI overrides (passed as ``overrides``, injected as init kwargs)
2. ``AIMM_*`` environment variables
3. ``HF_TOKEN`` / ``MODELSCOPE_API_TOKEN`` (the unprefixed hub variables we honour)
4. Docker secrets under ``/run/secrets``
5. the profile YAML file
6. model defaults

pydantic-settings orders its source tuple **highest priority first**; reversing
it inverts the whole chain with no error, so the order below is load-bearing.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    NestedSecretsSettingsSource,
    PydanticBaseSettingsSource,
    YamlConfigSettingsSource,
)

from ..errors import ConfigError
from .interpolation import interpolate
from .models import Settings

PROFILE_ENV: str = "AIMM_PROFILE"
PROFILE_FILENAMES: tuple[str, ...] = ("aimm.yaml", "aimm.yml")
BACKEND_ENV: str = "AIMM_BACKEND"
HF_TOKEN_ENV: str = "HF_TOKEN"
MODELSCOPE_TOKEN_ENV: str = "MODELSCOPE_API_TOKEN"

#: Unprefixed environment variables each hub's own ecosystem already defines,
#: mapped onto the settings section they belong to. A ``validation_alias`` cannot
#: do this: the env source resolves nested fields as ``AIMM_<SECTION>__<FIELD>``,
#: so an unprefixed alias is never even looked up — it just silently stays unset.
UNPREFIXED_TOKENS: dict[str, str] = {
    HF_TOKEN_ENV: "hub",
    MODELSCOPE_TOKEN_ENV: "modelscope",
}

_USER_CONFIG_RELPATH: tuple[str, str] = ("aimm", "config.yaml")


def find_profile(explicit: Path | None = None, *, cwd: Path | None = None) -> Path | None:
    """Resolve the profile path, or ``None`` when no profile exists.

    Order: ``explicit`` -> ``$AIMM_PROFILE`` -> ``./aimm.yaml`` -> ``./aimm.yml``
    -> ``$XDG_CONFIG_HOME/aimm/config.yaml`` (POSIX) or
    ``%APPDATA%/aimm/config.yaml`` (Windows).

    Raises:
        ConfigError: an explicitly requested profile does not exist.
    """
    if explicit is not None:
        if not Path(explicit).is_file():
            raise ConfigError(f"profile not found: {explicit}")
        return Path(explicit)

    from_env = os.environ.get(PROFILE_ENV)
    if from_env:
        if not Path(from_env).is_file():
            raise ConfigError(f"{PROFILE_ENV} points to a missing file: {from_env}")
        return Path(from_env)

    base = cwd if cwd is not None else Path.cwd()
    for name in PROFILE_FILENAMES:
        candidate = base / name
        if candidate.is_file():
            return candidate

    user = _user_config_path()
    return user if user is not None and user.is_file() else None


def _user_config_path() -> Path | None:
    """Per-user profile location, or None when the platform root is unset."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        root = Path(appdata) if appdata else None
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        root = Path(xdg) if xdg else Path.home() / ".config"
    return root.joinpath(*_USER_CONFIG_RELPATH) if root is not None else None


class InterpolatingYamlSource(YamlConfigSettingsSource):
    """YAML source that selects a backend and then expands ``${ENV}``.

    Backend selection happens *before* interpolation on purpose: a profile that
    defines several backends must not fail because an unrelated backend
    references a variable that is unset on this host.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        *,
        yaml_file: Path,
        backend: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._backend = backend
        self._env = env if env is not None else os.environ
        super().__init__(settings_cls, yaml_file=yaml_file)

    def __call__(self) -> dict[str, Any]:
        raw = super().__call__()
        if not raw:
            return {}
        selected = _select_backend(dict(raw), self._backend, self._env)
        result: dict[str, Any] = interpolate(selected, env=self._env, strict=True)
        return result


def _select_backend(
    data: dict[str, Any],
    backend: str | None,
    env: Mapping[str, str],
) -> dict[str, Any]:
    """Flatten the chosen entry of ``backends`` into the ``s3`` key."""
    backends = data.pop("backends", None)
    default_backend = data.pop("default_backend", None)
    if backends is None:
        return data
    if not isinstance(backends, dict) or not backends:
        raise ConfigError("profile key 'backends' must be a non-empty mapping")

    name = backend or env.get(BACKEND_ENV) or default_backend
    if name is None:
        if len(backends) == 1:
            name = next(iter(backends))
        elif "s3" in data:
            return data
        else:
            known = ", ".join(sorted(backends))
            raise ConfigError(
                f"profile defines several backends ({known}) but none was selected; "
                f"use --backend, {BACKEND_ENV} or 'default_backend'"
            )
    if name not in backends:
        known = ", ".join(sorted(backends))
        raise ConfigError(f"unknown backend {name!r}; profile defines: {known}")

    data["s3"] = backends[name]
    return data


class _UnprefixedTokenSource(PydanticBaseSettingsSource):
    """Maps unprefixed hub tokens onto their settings section.

    ``HF_TOKEN`` and ``MODELSCOPE_API_TOKEN`` are each ecosystem's own variable
    name, so they carry no ``AIMM_`` prefix and the ordinary env source cannot see
    them. This sits below ``AIMM_HUB__TOKEN`` / ``AIMM_MODELSCOPE__TOKEN`` so an
    explicit aimm setting always wins.
    """

    def __init__(self, settings_cls: type[BaseSettings], tokens: dict[str, str]) -> None:
        super().__init__(settings_cls)
        #: section name -> token value
        self._tokens = tokens

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # Not used: this source yields its whole document from __call__.
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return {section: {"token": token} for section, token in self._tokens.items()}


def load_settings(
    *,
    profile: Path | None = None,
    backend: str | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Settings:
    """Build :class:`Settings` from every configured layer.

    Args:
        profile: Explicit profile path; otherwise discovered by ``find_profile``.
        backend: Name of an entry in the profile's ``backends`` mapping.
        overrides: ONLY the values the user explicitly provided on the command
            line, keyed by dotted path (``{"s3.bucket": "x"}``). Passing a
            flag's default here would silently beat the profile file.

    Raises:
        ConfigError: profile unreadable, unknown backend, interpolation failure,
            unknown key, or a validation failure.
    """
    profile_path = find_profile(profile)
    init_kwargs = _expand_overrides(overrides or {})
    unprefixed = {
        section: value
        for variable, section in UNPREFIXED_TOKENS.items()
        if (value := os.environ.get(variable))
    }

    class _Settings(Settings):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            # HIGHEST PRIORITY FIRST. Do not reorder.
            sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
            if unprefixed:
                sources.append(_UnprefixedTokenSource(settings_cls, unprefixed))
            # Plain secrets_dir never reaches nested models: a file named
            # s3__secret_access_key is silently ignored and the field stays None.
            sources.append(
                NestedSecretsSettingsSource(
                    file_secret_settings,
                    secrets_nested_delimiter="__",
                    secrets_dir_missing="ok",
                )
            )
            if profile_path is not None:
                sources.append(
                    InterpolatingYamlSource(
                        settings_cls,
                        yaml_file=profile_path,
                        backend=backend,
                    )
                )
            return tuple(sources)

    try:
        return _Settings(**init_kwargs)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration:\n{_format_errors(exc)}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"profile is not valid YAML: {profile_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"profile is unreadable: {profile_path}: {exc}") from exc


def _format_errors(exc: ValidationError) -> str:
    """Render a ValidationError without ever echoing the offending input value.

    pydantic's own ``str(exc)`` embeds ``input_value``, which for a credential
    field would print the secret in plaintext.
    """
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)


def _expand_overrides(overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Turn ``{"s3.bucket": "x"}`` into ``{"s3": {"bucket": "x"}}``."""
    result: dict[str, Any] = {}
    for dotted, value in overrides.items():
        parts = dotted.split(".")
        node = result
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ConfigError(f"conflicting override paths around {dotted!r}")
            node = child
        node[parts[-1]] = value
    return result
