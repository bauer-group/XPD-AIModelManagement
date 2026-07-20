"""The single most important test file in the core suite.

Every assertion here is the same assertion stated five ways: **the plaintext secret
must not appear in the bytes that leave the process**. Testing that `***` is present is
not enough — a filter that emits `token=*** (was hunter2)` would pass that and still
leak. So each test searches the emitted text for the secret itself.

The five shapes a credential reaches a log line in: a `key=value` pair, a DSN with
embedded credentials, a JSON object member, a `SecretStr` interpolated into a message,
and — the one that is easiest to forget — a secret inside an exception traceback.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from bg_ai_model_management.logging_setup import (
    MASK,
    NOISY_LOGGERS,
    SECRET_FIELD_NAMES,
    SecretRedactingFilter,
    configure_logging,
    get_console,
    redact,
)

#: Distinctive enough that a substring search cannot match by accident.
SECRET = "hunter2-Zq7xKp0LmR4tWv91"


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """`configure_logging` reconfigures the root logger; put it back afterwards."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_noisy = {name: logging.getLogger(name).level for name in NOISY_LOGGERS}
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    for name, level in saved_noisy.items():
        logging.getLogger(name).setLevel(level)


# --------------------------------------------------------------------------- redact()


@pytest.mark.parametrize(
    "template",
    [
        "token={s}",
        "token = {s}",
        "token: {s}",
        "secret_access_key={s}",
        "aws_secret_access_key={s}",
        "access_key_id={s}",
        "session_token={s}",
        "hf_token={s}",
        "api_key={s}",
        "apikey={s}",
        "password={s}",
        "passwd={s}",
        "private_key={s}",
        "sas_token={s}",
        'password="{s}"',
        "password='{s}'",
        "db-password={s}",
        "AWS_SECRET_ACCESS_KEY={s}",
        "connecting with token={s} to bucket hf-backup",
    ],
)
def test_key_value_shapes_never_leak(template: str) -> None:
    line = template.format(s=SECRET)
    masked = redact(line)
    assert SECRET not in masked
    assert MASK in masked


@pytest.mark.parametrize("field", sorted(SECRET_FIELD_NAMES))
def test_every_declared_secret_field_is_masked(field: str) -> None:
    """The field list is data; if a name is added it must actually be honoured."""
    assert SECRET not in redact(f"{field}={SECRET}")


@pytest.mark.parametrize(
    "line",
    [
        "https://admin:{s}@minio.example.com/bucket",
        "s3://user:{s}@s3.eu-north1.bauer-group.com",
        "postgres://aimm:{s}@db:5432/aimm",
    ],
)
def test_dsn_credentials_never_leak(line: str) -> None:
    masked = redact(line.format(s=SECRET))
    assert SECRET not in masked
    assert MASK in masked
    # The host must survive: a redacted line still has to be diagnosable.
    assert "@" in masked


def test_dsn_keeps_the_username_and_scheme() -> None:
    masked = redact(f"https://admin:{SECRET}@minio.example.com/bucket")
    assert masked == f"https://admin:{MASK}@minio.example.com/bucket"


@pytest.mark.parametrize(
    "line",
    [
        '{{"api_key": "{s}"}}',
        '{{"secret_access_key":"{s}"}}',
        '{{"bucket": "hf-backup", "token": "{s}"}}',
        '{{"aws_secret_access_key" : "{s}"}}',
    ],
)
def test_json_member_values_never_leak(line: str) -> None:
    masked = redact(line.format(s=SECRET))
    assert SECRET not in masked
    assert MASK in masked


def test_json_redaction_leaves_parsable_json() -> None:
    masked = redact(json.dumps({"bucket": "hf-backup", "token": SECRET}))
    assert json.loads(masked) == {"bucket": "hf-backup", "token": MASK}


def test_redaction_is_idempotent() -> None:
    once = redact(f"token={SECRET}")
    assert redact(once) == once


def test_non_secret_text_is_untouched() -> None:
    line = "uploaded 42 parts of models/org/name to bucket=hf-backup region=eu-north1"
    assert redact(line) == line


def test_redact_never_raises_on_odd_input() -> None:
    for value in ("", "=", "token=", "://:@", "$" * 500, "token=" + "x" * 10_000):
        redact(value)


# ------------------------------------------------------------------ the logging filter


def test_lazy_percent_args_are_redacted_after_formatting() -> None:
    """`log.info("token=%s", secret)` is the idiomatic call and the easiest to leak."""
    buffer = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=buffer)
    logging.getLogger("aimm.test").info("connecting with token=%s", SECRET)
    out = buffer.getvalue()
    assert SECRET not in out
    assert MASK in json.loads(out)["message"]


def test_secret_inside_an_exception_traceback_never_reaches_the_stream() -> None:
    """The forgotten path: the message is clean but the traceback carries the secret."""
    buffer = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=buffer)
    try:
        raise RuntimeError(f"upload rejected for secret_access_key={SECRET}")
    except RuntimeError:
        logging.getLogger("aimm.test").error("upload failed", exc_info=True)
    out = buffer.getvalue()
    assert SECRET not in out
    payload = json.loads(out)
    assert "Traceback" in payload["exception"]
    assert MASK in payload["exception"]


def test_secret_in_a_chained_traceback_never_reaches_the_stream() -> None:
    """`raise ... from exc` renders both frames; both must be scrubbed."""
    buffer = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=buffer)
    try:
        try:
            raise ValueError(f"token={SECRET}")
        except ValueError as exc:
            raise RuntimeError("wrapped") from exc
    except RuntimeError:
        logging.getLogger("aimm.test").error("failed", exc_info=True)
    assert SECRET not in buffer.getvalue()


def test_secretstr_repr_does_not_render_the_value() -> None:
    """pydantic masks it, but the tool must never rely on that without checking."""
    secret = SecretStr(SECRET)
    assert SECRET not in repr(secret)
    assert SECRET not in str(secret)
    assert SECRET not in f"{secret}"
    assert secret.get_secret_value() == SECRET


def test_secretstr_logged_directly_does_not_leak() -> None:
    buffer = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=buffer)
    logging.getLogger("aimm.test").info("credential is %s", SecretStr(SECRET))
    assert SECRET not in buffer.getvalue()


def test_filter_returns_true_so_the_record_is_still_emitted() -> None:
    """A filter that swallowed records would hide the leak by hiding the log."""
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token=%s", (SECRET,), None)
    assert SecretRedactingFilter().filter(record) is True
    assert SECRET not in record.getMessage()


def test_filter_clears_args_after_folding_them_in() -> None:
    """Leaving `args` populated would let a handler re-expand the raw secret."""
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token=%s", (SECRET,), None)
    SecretRedactingFilter().filter(record)
    assert not record.args


# ------------------------------------------------------------------ configure_logging


def test_text_format_output_is_redacted_too() -> None:
    """The RichHandler path is a different formatter and needs its own proof."""
    buffer = io.StringIO()
    configure_logging(level="DEBUG", fmt="text", stream=buffer)
    logging.getLogger("aimm.test").warning("token=%s", SECRET)
    assert SECRET not in buffer.getvalue()
    assert MASK in buffer.getvalue()


def test_configure_logging_is_idempotent() -> None:
    """A second call must replace handlers, not stack them and duplicate every line."""
    buffer = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=buffer)
    configure_logging(level="DEBUG", fmt="json", stream=buffer)
    assert len(logging.getLogger().handlers) == 1
    logging.getLogger("aimm.test").info("once")
    assert buffer.getvalue().count('"message": "once"') == 1


def test_noisy_loggers_are_quietened() -> None:
    configure_logging(level="DEBUG", fmt="json", stream=io.StringIO())
    for name in NOISY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


def test_json_record_carries_the_contract_fields() -> None:
    buffer = io.StringIO()
    configure_logging(level="INFO", fmt="json", stream=buffer, run_id="run-42")
    logging.getLogger("aimm.test").info("hello", extra={"repo_id": "org/name"})
    payload = json.loads(buffer.getvalue())
    assert payload["level"] == "INFO"
    assert payload["logger"] == "aimm.test"
    assert payload["message"] == "hello"
    assert payload["run_id"] == "run-42"
    assert payload["repo_id"] == "org/name"
    assert payload["timestamp"].endswith("Z")


def test_configure_logging_returns_the_shared_console() -> None:
    """Progress bars must use this exact object or they corrupt the log stream."""
    console = configure_logging(level="INFO", fmt="text", stream=io.StringIO())
    assert get_console() is console


def test_level_is_honoured() -> None:
    buffer = io.StringIO()
    configure_logging(level="WARNING", fmt="json", stream=buffer)
    logging.getLogger("aimm.test").info("suppressed")
    logging.getLogger("aimm.test").warning("kept")
    assert "suppressed" not in buffer.getvalue()
    assert "kept" in buffer.getvalue()


@pytest.mark.parametrize(
    "line",
    [
        "Authorization: Bearer {s}",
        "Authorization: Basic {s}",
        "Proxy-Authorization: Bearer {s}",
        "authorization={s}",
        '{{"authorization": "Bearer {s}"}}',
        # A Python dict repr — how botocore and httpx render a header mapping.
        "{{'Authorization': 'Bearer {s}'}}",
    ],
)
def test_authorization_header_value_never_leaks(line: str) -> None:
    """The generic key=value rule stops at the first space, which masked only the scheme
    word (`Authorization: *** hunter2`) and left the credential in the log line. The
    authorization value must be consumed to end of line."""
    masked = redact(line.format(s=SECRET))
    assert SECRET not in masked
    assert MASK in masked


def test_authorization_redaction_stops_at_the_end_of_the_line() -> None:
    """Masking to end of LINE, not end of string: the next log line must survive."""
    masked = redact(f"Authorization: Bearer {SECRET}\nuploaded 42 parts to bucket=hf-backup")
    assert SECRET not in masked
    assert "uploaded 42 parts to bucket=hf-backup" in masked
