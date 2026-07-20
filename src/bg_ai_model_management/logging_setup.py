"""Logging setup: stdlib logging, a secret-redacting filter and text/JSON formatters.

All log output goes to stderr so that ``--json`` owns stdout. The rich ``Console``
created here is shared with every progress bar in the codebase; a second Console
would corrupt the bars.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any, TextIO

from rich.console import Console
from rich.logging import RichHandler

NOISY_LOGGERS: tuple[str, ...] = (
    "botocore",
    "boto3",
    "urllib3",
    "s3transfer",
    "httpx",
    "httpcore",
    "huggingface_hub",
    "filelock",
    "fsspec",
)

SECRET_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "secret_access_key",
        "access_key_id",
        "access_key",
        "token",
        "hf_token",
        "api_key",
        "apikey",
        "authorization",
        "session_token",
        "aws_secret_access_key",
        "aws_access_key_id",
        "sas_token",
        "private_key",
    }
)

MASK = "***"

# Longest first, so `secret_access_key` wins over `secret` and the whole name is masked.
_FIELDS = "|".join(sorted((re.escape(n) for n in SECRET_FIELD_NAMES), key=len, reverse=True))
# Quoted member: "api_key": "value" / 'api_key': 'value' -> keep the quotes, mask the
# value. Both quote styles matter: JSON uses double, a Python dict repr (how botocore and
# httpx render a header mapping) uses single.
_JSON_KV = re.compile(rf"""(?i)(["'])([a-z0-9_.\-]*(?:{_FIELDS}))\1(\s*:\s*)(["'])[^"']*\4""")
# `Authorization: Bearer <token>` must consume the value to END OF LINE. The generic _KV
# value group below stops at the first whitespace, so it masked only the scheme word and
# left the credential sitting in the log line — the single most common shape of a leaked
# credential. The scheme is masked along with the token so that the later _KV pass is a
# no-op on the result and redact() stays idempotent.
_AUTH_HEADER = re.compile(r"(?i)\b((?:proxy-)?authorization\s*[=:]\s*).+$", re.MULTILINE)
# key=value / key: value, with an optional vendor prefix (aws_access_key, db-password).
_KV = re.compile(rf"""(?i)\b([a-z0-9_.\-]*(?:{_FIELDS}))(\s*[=:]\s*)("[^"]*"|'[^']*'|\S+)""")
# scheme://user:PASSWORD@host -> mask the password segment only.
_URL_CRED = re.compile(r"(://[^:/@\s]+:)([^@/\s]+)(@)")

_console: Console | None = None
_STANDARD_RECORD_FIELDS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None)))


def redact(text: str) -> str:
    """Mask secret-looking values in a free-form string.

    Handles ``key=value`` / ``key: value`` pairs whose key ends in one of
    ``SECRET_FIELD_NAMES``, ``Authorization`` headers including their auth scheme, URLs
    with embedded credentials, and quoted members of JSON objects and dict reprs.
    Idempotent, and never raises.
    """
    if not isinstance(text, str):  # defensive: logging hands us whatever it was given
        return text
    try:
        text = _JSON_KV.sub(rf"\1\2\1\3\4{MASK}\4", text)
        text = _AUTH_HEADER.sub(rf"\1{MASK}", text)
        text = _KV.sub(rf"\1\2{MASK}", text)
        return _URL_CRED.sub(rf"\1{MASK}\3", text)
    except Exception:
        return text


class SecretRedactingFilter(logging.Filter):
    """Applies :func:`redact` to record.msg, record.args and formatted exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
            if record.exc_info is not None:
                # Render the traceback here so it can be redacted; handlers then use
                # exc_text verbatim. Rich tracebacks are given up on purpose: an
                # unredacted secret in a frame is worse than a plain traceback.
                record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
                record.exc_info = None
        except Exception:
            pass
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per record, plus whatever the caller passed via ``extra``."""

    def __init__(self, *, run_id: str | None = None) -> None:
        super().__init__()
        self._run_id = run_id

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "run_id": self._run_id,
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_RECORD_FIELDS and key not in payload:
                payload[key] = value
        if record.exc_text:
            payload["exception"] = record.exc_text
        return json.dumps(payload, default=str)


def configure_logging(
    *,
    level: str = "INFO",
    fmt: str = "text",
    console: Console | None = None,
    run_id: str | None = None,
    stream: TextIO | None = None,
) -> Console:
    """Configure root logging once and return the shared rich Console.

    ``fmt='text'`` installs a ``RichHandler`` bound to the returned Console;
    ``fmt='json'`` installs a ``StreamHandler`` emitting one JSON object per record.
    Both attach :class:`SecretRedactingFilter`, and ``NOISY_LOGGERS`` drop to WARNING.
    Idempotent: a second call replaces the handlers rather than stacking them.
    """
    global _console
    if console is None:
        console = Console(stderr=stream is None, file=stream)
    _console = console

    handler: logging.Handler
    if fmt == "json":
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(JsonFormatter(run_id=run_id))
    else:
        handler = RichHandler(console=console, show_path=False, rich_tracebacks=False)
        handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SecretRedactingFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    for noisy in NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return console


def get_console() -> Console:
    """Return the Console created by :func:`configure_logging`, or a default one.

    The SAME Console object must be handed to ``rich.progress.Progress``, otherwise
    progress bars and log lines corrupt each other.
    """
    global _console
    if _console is None:
        _console = Console(stderr=True)
    return _console
