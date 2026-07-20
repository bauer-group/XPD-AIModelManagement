"""`${VAR}` expansion — and the one rule that matters: a missing variable must RAISE.

Substituting an empty string for an unset `${MINIO_SECRET_KEY}` produces an empty
access key, which S3 treats as an *anonymous* request. The failure then surfaces
hundreds of files later as an opaque 403 from the object store rather than as
"you forgot to export MINIO_SECRET_KEY", which is what actually happened.
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_ai_model_management.config.interpolation import ENV_PATTERN, interpolate
from bg_ai_model_management.errors import ConfigError

ENV = {"BUCKET": "hf-backup", "REGION": "eu-north1", "EMPTY": ""}


# ------------------------------------------------------------------ the strict rule


def test_missing_variable_raises_rather_than_yielding_an_empty_string() -> None:
    with pytest.raises(ConfigError):
        interpolate("${MINIO_SECRET_KEY}", env={})


def test_missing_variable_raises_when_nested_in_a_document() -> None:
    document = {"s3": {"bucket": "b", "secret_access_key": "${MINIO_SECRET_KEY}"}}
    with pytest.raises(ConfigError):
        interpolate(document, env=ENV)


def test_missing_variable_raises_when_nested_in_a_list() -> None:
    with pytest.raises(ConfigError):
        interpolate({"include": ["a", "${NOPE}"]}, env=ENV)


def test_the_error_names_the_variable_but_never_echoes_the_surrounding_text() -> None:
    """The message must be actionable without printing the line, which may hold a
    second, resolved credential."""
    with pytest.raises(ConfigError) as caught:
        interpolate("secret_access_key=${MINIO_SECRET_KEY} id=AKIAREALKEY", env=ENV)
    message = str(caught.value)
    assert "MINIO_SECRET_KEY" in message
    assert "AKIAREALKEY" not in message


def test_non_strict_mode_substitutes_an_empty_string() -> None:
    """Only ever used for diagnostics; never on the credential path."""
    assert interpolate("${NOPE}", env={}, strict=False) == ""


def test_an_explicitly_empty_variable_is_a_value_not_a_miss() -> None:
    """`EMPTY=""` was set on purpose; that is different from never being set."""
    assert interpolate("${EMPTY}", env=ENV) == ""


# --------------------------------------------------------------------- substitution


def test_simple_substitution() -> None:
    assert interpolate("${BUCKET}", env=ENV) == "hf-backup"


def test_substitution_inside_surrounding_text() -> None:
    assert interpolate("s3://${BUCKET}/${REGION}/x", env=ENV) == "s3://hf-backup/eu-north1/x"


def test_repeated_references_to_the_same_variable() -> None:
    assert interpolate("${BUCKET}-${BUCKET}", env=ENV) == "hf-backup-hf-backup"


def test_default_is_used_when_the_variable_is_unset() -> None:
    assert interpolate("${NOPE:-fallback}", env=ENV) == "fallback"


def test_default_is_ignored_when_the_variable_is_set() -> None:
    assert interpolate("${BUCKET:-fallback}", env=ENV) == "hf-backup"


def test_an_empty_default_is_allowed() -> None:
    assert interpolate("${NOPE:-}", env=ENV) == ""


def test_a_default_may_contain_punctuation() -> None:
    assert interpolate("${NOPE:-https://example.com:9000}", env=ENV) == "https://example.com:9000"


def test_an_empty_variable_does_not_fall_back_to_its_default() -> None:
    """`EMPTY` is set, so the default must not fire — shell `:-` semantics differ here
    on purpose: an operator who exported an empty value meant it."""
    assert interpolate("${EMPTY:-fallback}", env=ENV) == ""


# ------------------------------------------------------------------------- escaping


def test_double_dollar_escapes_a_literal_dollar() -> None:
    assert interpolate("$$", env=ENV) == "$"


def test_double_dollar_prevents_expansion() -> None:
    assert interpolate("$${BUCKET}", env=ENV) == "${BUCKET}"


def test_escaped_reference_does_not_raise_for_an_unset_variable() -> None:
    assert interpolate("$${NOPE}", env={}) == "${NOPE}"


def test_a_lone_dollar_is_literal() -> None:
    assert interpolate("cost is $5", env=ENV) == "cost is $5"


def test_shell_style_bare_reference_is_not_expanded() -> None:
    """Only the braced form is a reference; `$HOME` in a path stays literal."""
    assert interpolate("$BUCKET", env=ENV) == "$BUCKET"


# ------------------------------------------------------------------------ recursion


def test_nested_mappings_and_lists_are_expanded() -> None:
    document: dict[str, Any] = {
        "backends": {
            "minio": {
                "bucket": "${BUCKET}",
                "region": "${REGION}",
                "tags": ["${BUCKET}", "static"],
            }
        }
    }
    assert interpolate(document, env=ENV) == {
        "backends": {
            "minio": {"bucket": "hf-backup", "region": "eu-north1", "tags": ["hf-backup", "static"]}
        }
    }


@pytest.mark.parametrize("value", [None, True, False, 0, 42, 3.5, 8 * 1024**2])
def test_non_string_leaves_pass_through_unchanged(value: Any) -> None:
    assert interpolate(value, env=ENV) is value


def test_the_input_document_is_not_mutated() -> None:
    document = {"bucket": "${BUCKET}"}
    interpolate(document, env=ENV)
    assert document == {"bucket": "${BUCKET}"}


def test_keys_are_left_alone() -> None:
    """Only values are expanded; a key is a schema name, not operator data."""
    assert interpolate({"${BUCKET}": "x"}, env=ENV) == {"${BUCKET}": "x"}


def test_expansion_result_is_not_re_expanded() -> None:
    """A value that happens to look like a reference must not resolve recursively —
    that would let an environment variable's *content* pull in another variable."""
    assert interpolate("${INDIRECT}", env={"INDIRECT": "${BUCKET}", **ENV}) == "${BUCKET}"


def test_env_defaults_to_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIMM_TEST_INTERPOLATION", "from-os-environ")
    assert interpolate("${AIMM_TEST_INTERPOLATION}") == "from-os-environ"


# -------------------------------------------------------------------------- pattern


@pytest.mark.parametrize("text", ["${A}", "${A_B}", "${_A}", "${A1}", "${A:-d}", "${A:-}"])
def test_pattern_matches_valid_references(text: str) -> None:
    assert ENV_PATTERN.fullmatch(text) is not None


@pytest.mark.parametrize("text", ["${1A}", "${A-B}", "${}", "${A B}", "$A", "{A}"])
def test_pattern_rejects_malformed_references(text: str) -> None:
    assert ENV_PATTERN.fullmatch(text) is None


@pytest.mark.parametrize("text", ["${1A}", "${}", "${A B}", "not a reference at all"])
def test_malformed_references_are_left_verbatim(text: str) -> None:
    """Silently deleting an unparsable reference would hide a profile typo."""
    assert interpolate(text, env=ENV) == text
