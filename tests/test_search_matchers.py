"""Tests for the pure matching layer in ncfetch.search."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ncfetch.search import (
    ByteLineSplitter,
    FindSpec,
    LineScanner,
    RemoteFile,
    compile_pattern,
    entry_matches,
    filter_entries,
    looks_binary,
    match_globs,
    path_allowed,
)


# ---------- compile_pattern ----------


def test_compile_pattern_is_regex_by_default():
    assert compile_pattern(r"a.c").search("abc")


def test_compile_pattern_fixed_string_escapes_metachars():
    """-F must treat 'a.c' literally, so 'abc' is NOT a match."""
    rx = compile_pattern("a.c", fixed_string=True)
    assert not rx.search("abc")
    assert rx.search("xa.cy")


def test_compile_pattern_ignore_case():
    assert compile_pattern("todo", ignore_case=True).search("TODO: fix")


# ---------- glob filters ----------


def test_match_globs_matches_basename():
    assert match_globs("Datasets/2025/a.csv", ["*.csv"])


def test_match_globs_matches_full_path():
    assert match_globs("Datasets/2025/a.csv", ["Datasets/*"])


def test_match_globs_is_case_sensitive_by_default():
    """fnmatchcase keeps behaviour identical on Linux and Windows."""
    assert not match_globs("a/README.MD", ["*.md"])
    assert match_globs("a/README.MD", ["*.md"], ignore_case=True)


def test_match_globs_empty_list_never_matches():
    assert not match_globs("a.csv", [])


def test_path_allowed_no_filters_passes():
    assert path_allowed("any/thing.bin")


def test_path_allowed_include_gate():
    assert path_allowed("a/b.md", include=["*.md"])
    assert not path_allowed("a/b.txt", include=["*.md"])


def test_path_allowed_exclude_beats_include():
    assert not path_allowed("node_modules/x.md", include=["*.md"], exclude=["node_modules/*"])


# ---------- binary sniffing ----------


def test_looks_binary_detects_nul():
    assert looks_binary(b"PK\x03\x04\x00\x00")


def test_looks_binary_false_for_utf8_text():
    assert not looks_binary("中文 text\nline2\n".encode())


def test_looks_binary_only_sniffs_first_8k():
    """A NUL past the sniff window must not flip the verdict (matches grep/git)."""
    assert not looks_binary(b"a" * 9000 + b"\x00")


# ---------- ByteLineSplitter ----------


def test_splitter_joins_line_across_chunk_boundary():
    sp = ByteLineSplitter()
    assert list(sp.feed(b"hel")) == []
    assert list(sp.feed(b"lo\nworld")) == ["hello"]
    assert list(sp.flush()) == ["world"]


def test_splitter_strips_crlf():
    sp = ByteLineSplitter()
    assert list(sp.feed(b"a\r\nb\r\n")) == ["a", "b"]


def test_splitter_flush_is_empty_when_file_ends_with_newline():
    sp = ByteLineSplitter()
    list(sp.feed(b"a\n"))
    assert list(sp.flush()) == []


def test_splitter_replaces_undecodable_bytes_instead_of_raising():
    sp = ByteLineSplitter()
    (line,) = list(sp.feed(b"\xff\xfe bad\n"))
    assert "bad" in line


def test_splitter_handles_multibyte_utf8():
    sp = ByteLineSplitter()
    assert list(sp.feed("水位資料\n".encode())) == ["水位資料"]


# ---------- LineScanner ----------


def _scan(lines, pattern="hit", **kw):
    sc = LineScanner("f.txt", compile_pattern(pattern), **kw)
    for ln in lines:
        sc.feed(ln)
    return sc.finish()


def test_scanner_reports_1_based_linenos():
    out = _scan(["a", "hit", "b"])
    assert [(m.lineno, m.line) for m in out] == [(2, "hit")]


def test_scanner_before_context():
    out = _scan(["l1", "l2", "l3", "hit"], before=2)
    assert out[0].before == ((2, "l2"), (3, "l3"))


def test_scanner_after_context():
    out = _scan(["hit", "l2", "l3", "l4"], after=2)
    assert out[0].after == ((2, "l2"), (3, "l3"))


def test_scanner_after_context_truncated_at_eof():
    """A match on the last line still gets emitted, just with a short tail."""
    out = _scan(["a", "hit"], after=3)
    assert len(out) == 1 and out[0].after == ()


def test_scanner_before_context_does_not_include_match_line():
    out = _scan(["hit"], before=2)
    assert out[0].before == ()


def test_scanner_max_count_stops_collecting():
    out = _scan(["hit", "hit", "hit"], max_count=2)
    assert len(out) == 2


def test_scanner_exhausted_flags_early_abort():
    sc = LineScanner("f", compile_pattern("hit"), max_count=1)
    sc.feed("nope")
    assert not sc.exhausted
    sc.feed("hit")
    assert sc.exhausted


def test_scanner_not_exhausted_while_after_context_outstanding():
    """Aborting the download too early would truncate the -A lines."""
    sc = LineScanner("f", compile_pattern("hit"), max_count=1, after=2)
    sc.feed("hit")
    assert not sc.exhausted
    sc.feed("x")
    sc.feed("y")
    assert sc.exhausted


def test_scanner_invert_match():
    out = _scan(["hit", "miss"], invert=True)
    assert [m.line for m in out] == ["miss"]


def test_scanner_overlapping_context_keeps_matches_in_line_order():
    out = _scan(["hit", "hit"], before=1, after=1)
    assert [m.lineno for m in out] == [1, 2]


# ---------- FindSpec filtering ----------


NOW = datetime.now(timezone.utc)


def _f(path, size=100, days_old=1, is_dir=False):
    return RemoteFile(path, size, NOW - timedelta(days=days_old), is_dir)


def test_entry_matches_type_filter():
    spec = FindSpec(kind="d")
    assert entry_matches(_f("dir", is_dir=True), spec)
    assert not entry_matches(_f("a.txt"), spec)


def test_entry_matches_name_glob():
    assert entry_matches(_f("a/b.csv"), FindSpec(name_globs=("*.csv",)))
    assert not entry_matches(_f("a/b.txt"), FindSpec(name_globs=("*.csv",)))


def test_entry_matches_regex_on_path():
    assert entry_matches(_f("2025/report.pdf"), FindSpec(regex=compile_pattern(r"20\d\d/")))


def test_entry_matches_size_bounds():
    assert entry_matches(_f("a", size=100), FindSpec(min_size=50, max_size=150))
    assert not entry_matches(_f("a", size=10), FindSpec(min_size=50))
    assert not entry_matches(_f("a", size=500), FindSpec(max_size=150))


def test_size_filter_excludes_directories():
    """Dirs have no size, so a size query must not silently return them."""
    assert not entry_matches(_f("d", is_dir=True), FindSpec(min_size=1))


def test_entry_matches_newer_than():
    cutoff = NOW - timedelta(days=7)
    assert entry_matches(_f("fresh", days_old=1), FindSpec(newer_than=cutoff))
    assert not entry_matches(_f("stale", days_old=30), FindSpec(newer_than=cutoff))


def test_entry_matches_older_than():
    cutoff = NOW - timedelta(days=7)
    assert entry_matches(_f("stale", days_old=30), FindSpec(older_than=cutoff))
    assert not entry_matches(_f("fresh", days_old=1), FindSpec(older_than=cutoff))


def test_unknown_mtime_is_excluded_by_time_filters():
    node = RemoteFile("a", 1, None, False)
    assert not entry_matches(node, FindSpec(newer_than=NOW - timedelta(days=1)))


def test_filter_entries_applies_all_conditions():
    nodes = [_f("a.csv", size=100), _f("b.csv", size=10), _f("c.txt", size=100)]
    out = filter_entries(nodes, FindSpec(name_globs=("*.csv",), min_size=50))
    assert [n.path for n in out] == ["a.csv"]
