"""Environment-variable interpolation for configuration trees.

Secrets never appear literally in a profile file; they are referenced as
``${VAR}`` and resolved after parsing against an *injected* mapping, so tests
never have to mutate ``os.environ``.

A referenced variable that is unset and carries no default is a hard error.
Substituting an empty string would hand an empty access key to the object store,
which S3 treats as an anonymous request — a silent, hard-to-diagnose failure.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from ..errors import ConfigError

#: Matches ``${NAME}`` and ``${NAME:-default}``.
ENV_PATTERN: re.Pattern[str] = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}",
)

#: ``$$`` escapes a literal ``$``; scanned in the same pass so that ``$${VAR}``
#: yields the literal text ``${VAR}`` instead of expanding it.
_TOKEN_PATTERN: re.Pattern[str] = re.compile(r"\$\$|" + ENV_PATTERN.pattern)


def interpolate(
    value: Any,
    *,
    env: Mapping[str, str] | None = None,
    strict: bool = True,
) -> Any:
    """Recursively expand ``${VAR}`` / ``${VAR:-default}`` inside dicts and lists.

    Args:
        value: Any config node. Non-``str`` leaves pass through unchanged.
        env: Variable source; defaults to ``os.environ``.
        strict: When True, an unset variable without a default raises.

    Returns:
        A new tree with every string leaf expanded.

    Raises:
        ConfigError: ``strict`` is True and a referenced variable is unset and
            has no default.
    """
    if env is None:
        env = os.environ
    if isinstance(value, str):
        return _expand(value, env, strict)
    if isinstance(value, Mapping):
        return {k: interpolate(v, env=env, strict=strict) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(item, env=env, strict=strict) for item in value]
    return value


def _expand(text: str, env: Mapping[str, str], strict: bool) -> str:
    def replace(match: re.Match[str]) -> str:
        if match.group(0) == "$$":
            return "$"
        name, default = match.group(1), match.group(2)
        found = env.get(name)
        if found is not None:
            return found
        if default is not None:
            return default
        if strict:
            # Never echo the value or the surrounding text: only the name.
            raise ConfigError(f"configuration references undefined environment variable: {name}")
        return ""

    return _TOKEN_PATTERN.sub(replace, text)
