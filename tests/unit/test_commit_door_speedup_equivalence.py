"""The commit door speed-up answers exactly what the direct door answers.

Three changes sit behind these tests, each with its guard named:

- the import-scoped exact-input memo of ``commit_door_fields_verdict``: key is
  the call site plus every field in its exact order, value is the verdict,
  bounded by entries and characters, dropped at the end of the import;
- the per-call prep cache: each string's normalised form and its decoded and
  despaced variants are computed once per door call and reused between the
  floor half and the gate half of the same call;
- the content-hash lookup rewrite (tested in
  ``test_source_content_hash_lookup.py``).

Differential: every fixture's verdict through the memoised call sites is
compared with the verdict of a direct ``commit_door_fields_verdict`` call on
the same arguments, and a full import whose memo is forced to miss on every
call is compared, row by row, with the memoised import.
"""

from __future__ import annotations

import base64
from alicebot_api.legacy_credential_check import commit_door_fields_verdict
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_capture import VNextCaptureService, _DOOR_MEMO_MAX_ENTRIES, _door_fields_verdict_memoized

from tests.unit.test_markdown_import_equality import (
    USER_ID,
    _import,
    _vault,
    build_equality_folder,
    row_digest,
)

# (name, fields, expected verdict). Expected values measured on main before
# the change and pinned here.
FIXTURES = [
    (
        "clean note",
        ("note", None, None, "notes/a.md", "a.md", "# Plan\n\nordinary text about the archive.\n", {}),
        None,
    ),
    (
        "key split over two lines",
        ("# Split", None, None, "notes/s.md", "s.md", "# Split\n\napi key\n = hunter2value and the note continues.\n", {}),
        "credential",
    ),
    ("pem block", ("pem", "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA1234\n-----END RSA PRIVATE KEY-----"), "credential"),
    ("unmatched begin", ("begin", "-----BEGIN PRIVATE KEY-----\nbody continues"), "credential"),
    ("base64 wrapped key", ("value: " + base64.b64encode(b"api key = hunter2value").decode(),), "credential"),
    ("low entropy akia", ("Fact: the access id is AKIAAAAAAAAAAAAA",), "credential"),
    ("refused file name", ("notes", "password=hunter2value.md"), "credential"),
    ("expands under normalisation", ("note", "\uFDFA" * 120), "expansion"),
]


def _fresh_memo() -> dict[str, object]:
    return {"entries": {}, "chars": 0}


def test_memo_answer_matches_the_direct_door() -> None:
    """Guard: the memo key (dropping a field from it must fail this test)."""
    for name, fields, expected in FIXTURES:
        direct = commit_door_fields_verdict(*fields)
        assert direct == expected, name
        memo = _fresh_memo()
        site = "capture_source" if len(fields) > 2 else "unit_verdict"
        answers = [_door_fields_verdict_memoized(memo, site, *fields) for _ in range(3)]
        assert answers == [expected, expected, expected], name
    # Two calls that differ only in one field, with different verdicts: the
    # memo must not answer the second from the first.
    memo = _fresh_memo()
    first = _door_fields_verdict_memoized(memo, "capture_source", "note", None, None, "a.md", "a.md", "hello", {})
    second = _door_fields_verdict_memoized(
        memo, "capture_source", "password=hunter2value", None, None, "a.md", "a.md", "hello", {}
    )
    assert (first, second) == (None, "credential")


def test_repeated_lines_hit_the_memo_and_match_the_door(tmp_path) -> None:
    """Guard: the memo again, on the repeated-line fixture class."""
    line = "Fact: the access id is AKIAAAAAAAAAAAAA inline."
    memo = _fresh_memo()
    answers = [_door_fields_verdict_memoized(memo, "unit_verdict", line) for _ in range(5)]
    assert answers == ["credential"] * 5
    assert len(memo["entries"]) == 1
    assert commit_door_fields_verdict(line) == "credential"


def test_memo_is_dropped_at_the_end_of_the_import(tmp_path) -> None:
    """Guard: the memo lifetime (not dropping it shares state across imports)."""
    folder = tmp_path / "eq"
    build_equality_folder(folder)
    database = _vault(tmp_path / "v")
    with sqlite_user_connection(database, USER_ID) as conn:
        service = VNextCaptureService(SQLiteVNextStore(conn, USER_ID))
        service.import_markdown_folder(folder)
        assert service._door_memo is None
        assert service._source_lookup_cache is None
    # A second import on the same service starts from nothing.
    with sqlite_user_connection(database, USER_ID) as conn:
        service = VNextCaptureService(SQLiteVNextStore(conn, USER_ID))
        memo_object = {"entries": {"stale": "credential"}, "chars": 5}
        service._door_memo = memo_object
        service.import_markdown_folder(folder)
        assert service._door_memo is not memo_object
        assert service._door_memo is None


def test_memo_never_grows_past_its_bounds() -> None:
    """Guard: the bounds (removing them lets the memo hold whole file texts)."""
    memo = _fresh_memo()
    assert _door_fields_verdict_memoized(memo, "unit_verdict", "x" * 5_000) is None
    assert memo["entries"] == {}
    memo = {"entries": {}, "chars": 2_000_000}
    assert _door_fields_verdict_memoized(memo, "unit_verdict", "clean") is None
    assert memo["entries"] == {}
    memo = {"entries": {index: None for index in range(_DOOR_MEMO_MAX_ENTRIES)}, "chars": 0}
    assert _door_fields_verdict_memoized(memo, "unit_verdict", "clean") is None
    assert len(memo["entries"]) == _DOOR_MEMO_MAX_ENTRIES


def test_import_with_forced_memo_misses_stores_the_same_rows(tmp_path, monkeypatch) -> None:
    """Guard: the memo is an answer cache only (any reused state shows here)."""
    import alicebot_api.vnext_capture as vc

    real = vc.commit_door_fields_verdict

    def every_call_misses(*fields):
        return real(*fields)

    folder = tmp_path / "eq"
    build_equality_folder(folder)
    memoised = _vault(tmp_path / "v1")
    _import(memoised, folder)
    forced = _vault(tmp_path / "v2")
    monkeypatch.setattr(vc, "_door_fields_verdict_memoized", lambda memo, site, *fields: every_call_misses(*fields))
    _import(forced, folder)
    # Both vaults digested under the same procedure; equal means the memo
    # changed nothing about what is stored.
    assert row_digest(forced, {str(folder): "<corpus>"}) == row_digest(memoised, {str(folder): "<corpus>"})
