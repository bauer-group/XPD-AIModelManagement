"""Path safety and the key layout. The security-critical file of the hf-backup tool.

Every filename here arrives from the Hugging Face Hub, which is a public,
user-writable namespace. A repository can legally contain a file called
`../../../etc/cron.d/x`, and two separate things must then be true:

* it can never become an S3 key outside the configured prefix, and
* it can never be written outside `--dest` during a restore (Zip-Slip).

The tests are grouped by those two guarantees. Hostile names are asserted against
both, because rejecting a name in one place and accepting it in the other is exactly
how this class of bug ships.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bg_ai_model_management.errors import UnsafePathError
from bg_ai_model_management.tools.hfbackup import keys
from bg_ai_model_management.tools.hfbackup.types import RepoType

SHA = "a" * 40
PREFIX = "aimm"

#: Names a hostile or merely careless repository can contain.
TRAVERSAL_NAMES = [
    "..",
    "../x",
    "../../etc/passwd",
    "a/../../etc/passwd",
    "a/..",
    "./x",
    "a/./b",
    "x/./../../y",
]

ABSOLUTE_NAMES = ["/etc/passwd", "/", "//server/share/x", "/a/b"]

WINDOWS_NAMES = [
    "a\\b",
    "..\\..\\windows\\system32",
    "C:/Windows/System32/x",
    "C:\\Windows",
    "c:/x",
    "a/C:/b",
    "a/D:x",
]

MALFORMED_NAMES = ["", "a//b", "a/", "//a", "a//", "///"]

CONTROL_NAMES = ["a\x00b", "a\nb", "a\tb", "\x01x", "x\x1f", "a\rb"]

#: Confusables that LOOK like traversal but are ordinary characters to every
#: filesystem: FULLWIDTH FULL STOP, FRACTION SLASH, FULLWIDTH REVERSE SOLIDUS. They
#: must be accepted as names and must still stay inside `--dest`. The visual ambiguity
#: is the entire point of the fixture, hence the suppression.
LOOK_ALIKE_NAMES = ["．．/x", "⁄etc", "a＼b"]  # noqa: RUF001


# ------------------------------------------------------- assert_safe_relpath: reject


@pytest.mark.parametrize("path", TRAVERSAL_NAMES)
def test_traversal_is_rejected(path: str) -> None:
    with pytest.raises(UnsafePathError):
        keys.assert_safe_relpath(path)


@pytest.mark.parametrize("path", ABSOLUTE_NAMES)
def test_absolute_paths_are_rejected(path: str) -> None:
    with pytest.raises(UnsafePathError):
        keys.assert_safe_relpath(path)


@pytest.mark.parametrize("path", WINDOWS_NAMES)
def test_backslashes_and_drive_letters_are_rejected(path: str) -> None:
    """A backslash is an ordinary character on POSIX and a separator on Windows, so a
    POSIX-only check would let `..\\..\\x` through to a Windows restore."""
    with pytest.raises(UnsafePathError):
        keys.assert_safe_relpath(path)


def test_a_drive_letter_is_rejected_in_any_segment() -> None:
    """`Path("dest") / "a/C:/b"` re-roots on Windows, so the first segment is not the
    only one that matters."""
    with pytest.raises(UnsafePathError):
        keys.assert_safe_relpath("harmless/dir/C:/evil")


@pytest.mark.parametrize("path", MALFORMED_NAMES)
def test_empty_segments_are_rejected(path: str) -> None:
    with pytest.raises(UnsafePathError):
        keys.assert_safe_relpath(path)


@pytest.mark.parametrize("path", CONTROL_NAMES)
def test_control_characters_are_rejected(path: str) -> None:
    """NUL truncates a C string; a newline forges a second line in any log or listing."""
    with pytest.raises(UnsafePathError):
        keys.assert_safe_relpath(path)


def test_an_over_long_path_is_rejected() -> None:
    with pytest.raises(UnsafePathError):
        keys.assert_safe_relpath("a" * (keys.MAX_PATH_LENGTH + 1))


def test_a_path_at_the_length_limit_is_accepted() -> None:
    assert keys.assert_safe_relpath("a" * keys.MAX_PATH_LENGTH)


def test_the_error_is_a_config_error_so_it_exits_two() -> None:
    """`UnsafePathError` derives from `ConfigError`; that is what maps it to exit 2."""
    from bg_ai_model_management.errors import ConfigError

    with pytest.raises(ConfigError):
        keys.assert_safe_relpath("../x")


# ------------------------------------------------------- assert_safe_relpath: accept


@pytest.mark.parametrize(
    "path",
    [
        "config.json",
        "model.safetensors",
        "a/b/c.bin",
        "subdir/model-00001-of-00002.safetensors",
        "a file with spaces.txt",
        "..hidden",
        "...leading-dots",
        "a..b/c",
        "dir.with.dots/f",
        "UPPER/Mixed_Case-1.2.3",
        "ünïcode/文件.bin",
        "emoji-🎉.txt",
    ],
)
def test_legitimate_paths_are_accepted(path: str) -> None:
    assert keys.assert_safe_relpath(path) == path


def test_the_path_is_returned_unchanged_and_never_normalised() -> None:
    """Rewriting an upstream path would silently change what we claim to have backed
    up: the manifest would no longer name the file the Hub named."""
    path = "a..b/c...d/e.bin"
    assert keys.assert_safe_relpath(path) is path


def test_a_unicode_look_alike_is_an_ordinary_name_not_a_traversal() -> None:
    """U+FF0E FULLWIDTH FULL STOP is not `.` to any filesystem, so it is a legal file
    name rather than an escape. It must be accepted, and must still stay contained —
    see `test_unicode_look_alikes_stay_inside_dest`."""
    for path in LOOK_ALIKE_NAMES:
        assert keys.assert_safe_relpath(path) == path


# ------------------------------------------------------------------ assert_safe_ref


@pytest.mark.parametrize("ref", ["main", "v1.0.0", "refs/pr/3", "release-2024.06", "a_b.c-d"])
def test_legitimate_refs_are_accepted(ref: str) -> None:
    assert keys.assert_safe_ref(ref) == ref


@pytest.mark.parametrize(
    "ref", ["..", "../main", "main/..", "/main", "main\\x", "C:/main", "", "a//b", "a\x00b"]
)
def test_unsafe_refs_are_rejected(ref: str) -> None:
    with pytest.raises(UnsafePathError):
        keys.assert_safe_ref(ref)


@pytest.mark.parametrize("ref", ["main branch", "main?x", "main*", "a:b", "a;b", "a#b", "a%2e"])
def test_refs_with_key_hostile_characters_are_rejected(ref: str) -> None:
    """A ref becomes a key segment; `%2e` in particular is `.` after URL decoding."""
    with pytest.raises(UnsafePathError):
        keys.assert_safe_ref(ref)


# ---------------------------------------------------------------- safe_local_path


@pytest.mark.parametrize("path", TRAVERSAL_NAMES + ABSOLUTE_NAMES + WINDOWS_NAMES)
def test_a_hostile_name_cannot_escape_the_restore_directory(dest_dir: Path, path: str) -> None:
    with pytest.raises(UnsafePathError):
        keys.safe_local_path(dest_dir, path)


def test_a_legitimate_name_resolves_inside_the_restore_directory(dest_dir: Path) -> None:
    resolved = keys.safe_local_path(dest_dir, "a/b/c.bin")
    assert resolved == dest_dir.resolve() / "a" / "b" / "c.bin"
    assert resolved.is_relative_to(dest_dir.resolve())


@pytest.mark.parametrize("path", [*LOOK_ALIKE_NAMES, "..hidden/f"])
def test_unicode_look_alikes_stay_inside_dest(dest_dir: Path, path: str) -> None:
    """They are accepted as names, so the containment check is what must hold."""
    assert keys.safe_local_path(dest_dir, path).is_relative_to(dest_dir.resolve())


def test_a_symlinked_parent_cannot_be_used_to_escape(dest_dir: Path, tmp_path: Path) -> None:
    """Both sides are resolved before containment is re-checked, which defeats a
    symlink planted inside the destination by an earlier file in the same restore."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = dest_dir / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - needs privileges on Windows
        pytest.skip("symlinks are not available to this user")
    with pytest.raises(UnsafePathError):
        keys.safe_local_path(dest_dir, "link/escaped.bin")


def test_containment_holds_for_a_relative_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "restore").mkdir()
    resolved = keys.safe_local_path(Path("restore"), "a/b.bin")
    assert resolved.is_relative_to((tmp_path / "restore").resolve())


# -------------------------------------------------------------------- key builders


def test_repo_root_shape() -> None:
    assert keys.repo_root(PREFIX, RepoType.models, "org/name") == "aimm/v1/models/org/name"


def test_repo_root_for_datasets() -> None:
    assert keys.repo_root(PREFIX, RepoType.datasets, "org/ds") == "aimm/v1/datasets/org/ds"


def test_revision_root_shape() -> None:
    assert (
        keys.revision_root(PREFIX, RepoType.models, "org/name", SHA)
        == f"aimm/v1/models/org/name/revisions/{SHA}"
    )


def test_file_key_shape() -> None:
    assert (
        keys.file_key(PREFIX, RepoType.models, "org/name", SHA, "a/b.bin")
        == f"aimm/v1/models/org/name/revisions/{SHA}/files/a/b.bin"
    )


def test_manifest_keys_sit_beside_each_other() -> None:
    manifest = keys.manifest_key(PREFIX, RepoType.models, "org/name", SHA)
    digest = keys.manifest_digest_key(PREFIX, RepoType.models, "org/name", SHA)
    assert manifest.endswith("/manifest.json")
    assert digest == f"{manifest}.sha256"


def test_ref_key_shape() -> None:
    assert (
        keys.ref_key(PREFIX, RepoType.models, "org/name", "main")
        == "aimm/v1/models/org/name/refs/main.json"
    )


def test_a_slashed_ref_nests_under_refs() -> None:
    assert keys.ref_key(PREFIX, RepoType.models, "o/n", "refs/pr/3").endswith("refs/refs/pr/3.json")


def test_listing_prefixes_carry_a_trailing_slash() -> None:
    """`ListObjectsV2` with `Delimiter='/'` needs it to roll each revision up into
    exactly one `CommonPrefixes` entry."""
    assert keys.refs_prefix(PREFIX, RepoType.models, "o/n").endswith("/refs/")
    assert keys.revisions_prefix(PREFIX, RepoType.models, "o/n").endswith("/revisions/")


def test_every_key_starts_at_the_configured_prefix() -> None:
    builders = [
        keys.repo_root(PREFIX, RepoType.models, "o/n"),
        keys.revision_root(PREFIX, RepoType.models, "o/n", SHA),
        keys.file_key(PREFIX, RepoType.models, "o/n", SHA, "a.bin"),
        keys.manifest_key(PREFIX, RepoType.models, "o/n", SHA),
        keys.manifest_digest_key(PREFIX, RepoType.models, "o/n", SHA),
        keys.ref_key(PREFIX, RepoType.models, "o/n", "main"),
        keys.refs_prefix(PREFIX, RepoType.models, "o/n"),
        keys.revisions_prefix(PREFIX, RepoType.models, "o/n"),
        keys.probe_key(PREFIX),
    ]
    for key in builders:
        assert key.startswith(f"{PREFIX}/{keys.LAYOUT_VERSION}/")
        assert "//" not in key
        assert ".." not in key.split("/")


def test_a_prefix_with_stray_slashes_produces_the_same_keys() -> None:
    for prefix in ("aimm", "/aimm", "aimm/", "/aimm/"):
        assert keys.repo_root(prefix, RepoType.models, "o/n") == "aimm/v1/models/o/n"


def test_an_empty_prefix_puts_the_layout_at_the_bucket_root() -> None:
    assert keys.repo_root("", RepoType.models, "o/n") == "v1/models/o/n"
    assert keys.probe_key("").startswith("v1/_probe/")


def test_a_nested_prefix_is_preserved() -> None:
    assert keys.repo_root("team/aimm", RepoType.models, "o/n") == "team/aimm/v1/models/o/n"


# ------------------------------------------------- key builders reject hostile input


@pytest.mark.parametrize("path", TRAVERSAL_NAMES + ABSOLUTE_NAMES + WINDOWS_NAMES + CONTROL_NAMES)
def test_a_hostile_file_name_cannot_forge_a_key(path: str) -> None:
    with pytest.raises(UnsafePathError):
        keys.file_key(PREFIX, RepoType.models, "org/name", SHA, path)


@pytest.mark.parametrize("repo_id", ["../evil", "/abs", "a\\b", "C:/x", "", "a//b", "o/n/.."])
def test_a_forged_repo_id_cannot_reach_outside_the_prefix(repo_id: str) -> None:
    with pytest.raises(UnsafePathError):
        keys.repo_root(PREFIX, RepoType.models, repo_id)


@pytest.mark.parametrize("sha", ["../..", "a/../b", "/abs", "x\\y"])
def test_a_forged_commit_sha_cannot_reach_outside_the_prefix(sha: str) -> None:
    with pytest.raises(UnsafePathError):
        keys.revision_root(PREFIX, RepoType.models, "o/n", sha)


@pytest.mark.parametrize("prefix", ["../elsewhere", "a/../b", "a\\b", "a\x00b", "a//b"])
def test_a_hostile_configured_prefix_is_rejected(prefix: str) -> None:
    with pytest.raises(UnsafePathError):
        keys.repo_root(prefix, RepoType.models, "o/n")


def test_a_leading_slash_in_the_prefix_is_normalised_not_rejected() -> None:
    """`prefix: /aimm` is a plausible thing to type and means the same bucket root."""
    assert keys.repo_root("/aimm", RepoType.models, "o/n") == "aimm/v1/models/o/n"


# --------------------------------------------------------------------- probe_key


def test_probe_key_is_fresh_on_every_call() -> None:
    """A shared probe key would let two concurrent runs overwrite each other's probe."""
    assert len({keys.probe_key(PREFIX) for _ in range(64)}) == 64


def test_probe_key_lives_under_a_reserved_segment() -> None:
    key = keys.probe_key(PREFIX)
    assert key.startswith(f"{PREFIX}/v1/_probe/")
    assert keys.parse_repo_prefix(PREFIX, key) is None


# --------------------------------------------------------------- parse_repo_prefix


@pytest.mark.parametrize("repo_type", list(RepoType))
@pytest.mark.parametrize("repo_id", ["org/name", "user/model-1", "o/n.v2"])
def test_parse_repo_prefix_inverts_repo_root(repo_type: RepoType, repo_id: str) -> None:
    key = keys.repo_root(PREFIX, repo_type, repo_id)
    assert keys.parse_repo_prefix(PREFIX, key) == (repo_type, repo_id)


def test_parse_repo_prefix_works_on_a_deeper_key() -> None:
    key = keys.file_key(PREFIX, RepoType.datasets, "org/ds", SHA, "a/b.bin")
    assert keys.parse_repo_prefix(PREFIX, key) == (RepoType.datasets, "org/ds")


def test_parse_repo_prefix_round_trips_with_an_empty_prefix() -> None:
    key = keys.repo_root("", RepoType.models, "org/name")
    assert keys.parse_repo_prefix("", key) == (RepoType.models, "org/name")


@pytest.mark.parametrize(
    "key",
    [
        "other-tool/v1/models/o/n",
        "aimm/v2/models/o/n",
        "aimm/v1/spaces/o/n",
        "aimm/v1/models/o",
        "aimm/v1/models//name",
        "aimm/v1/models/owner/",
        "aimm/v1/models",
        "aimm/v1/",
        "",
        "totally unrelated object",
    ],
)
def test_parse_repo_prefix_returns_none_for_a_foreign_key(key: str) -> None:
    """An unrecognised key is normal in a shared bucket and must not raise: `catalog`
    walks whatever the bucket contains."""
    assert keys.parse_repo_prefix(PREFIX, key) is None


# ── regressions ──────────────────────────────────────────────────────────────

#: NTFS alternate-data-stream references. The colon is NOT at the start of the segment,
#: so the old drive-designator regex (anchored `^[A-Za-z]:`) let every one of these
#: through. On Windows, opening `<dest>/config.json:x.aimm-part` creates a REAL, empty
#: `config.json` in the restore directory plus a hidden stream on it, and the rename then
#: fails with WinError 87 — an untyped OSError mapped to exit 1 instead of exit 2, a file
#: the repository never contained, and a legitimate `config.json` that can no longer be
#: restored because `target.exists()` now fires for it.
ALTERNATE_DATA_STREAM_NAMES = [
    "config.json:x",
    "model.safetensors:Zone.Identifier",
    "a/config.json:stream",
    "weights:$DATA",
    "x:",
]


@pytest.mark.parametrize("name", ALTERNATE_DATA_STREAM_NAMES)
def test_a_colon_anywhere_in_a_segment_is_rejected(name: str) -> None:
    with pytest.raises(UnsafePathError):
        keys.assert_safe_relpath(name)


@pytest.mark.parametrize("name", ALTERNATE_DATA_STREAM_NAMES)
def test_an_alternate_data_stream_never_reaches_the_filesystem(name: str, tmp_path: Path) -> None:
    """The restore path must refuse it, not create a stray file while failing."""
    with pytest.raises(UnsafePathError):
        keys.safe_local_path(tmp_path, name)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ALTERNATE_DATA_STREAM_NAMES)
def test_an_alternate_data_stream_never_becomes_a_key(name: str) -> None:
    with pytest.raises(UnsafePathError):
        keys.file_key("aimm", RepoType.models, "acme/model", "a" * 40, name)
