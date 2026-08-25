"""End-to-end tests for the async walk/grep/find layer against a fake DAV server.

Uses httpx.MockTransport so no network or pytest-asyncio plugin is needed —
each test drives the coroutine with asyncio.run().
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from ncfetch.search import (
    FindSpec,
    GrepSpec,
    compile_pattern,
    find_entries,
    grep_tree,
    walk_propfind,
)

BASE = "https://nc.test/dav"

# path -> bytes (None marks a collection)
TREE: dict[str, bytes | None] = {
    "": None,
    "notes": None,
    "notes/a.md": b"alpha\nTODO: fix me\nomega\n",
    "notes/b.md": b"nothing here\n",
    "notes/big.md": b"TODO in a file the size filter should skip\n",
    "data.csv": b"id,name\n1,TODO\n",
    "blob.bin": b"\x00\x01TODO\x02",
}
BIG_SIZE = 50 * 1024 * 1024  # what the server advertises for notes/big.md


def _url(rel: str) -> str:
    return f"{BASE}/{rel}" if rel else BASE


def _href(rel: str) -> str:
    return f"/dav/{rel}" if rel else "/dav/"


def _propfind_body(target: str) -> bytes:
    def entry(rel: str) -> str:
        body = TREE[rel]
        if body is None:
            rtype = "<d:resourcetype><d:collection/></d:resourcetype>"
            size = ""
        else:
            n = BIG_SIZE if rel == "notes/big.md" else len(body)
            rtype = "<d:resourcetype/>"
            size = f"<d:getcontentlength>{n}</d:getcontentlength>"
        return (
            f"<d:response><d:href>{_href(rel)}</d:href><d:propstat><d:prop>"
            f"{rtype}{size}"
            f"<d:getlastmodified>Tue, 20 May 2025 08:00:00 GMT</d:getlastmodified>"
            f"</d:prop></d:propstat></d:response>"
        )

    prefix = f"{target}/" if target else ""
    children = [
        rel for rel in TREE
        if rel.startswith(prefix) and rel != target and "/" not in rel[len(prefix):]
    ]
    body = entry(target) + "".join(entry(c) for c in children)
    return f'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">{body}</d:multistatus>'.encode()


def _handler(request: httpx.Request) -> httpx.Response:
    rel = request.url.path[len("/dav"):].strip("/")
    if request.method == "PROPFIND":
        if rel not in TREE or TREE[rel] is not None:
            return httpx.Response(404)
        return httpx.Response(207, content=_propfind_body(rel))
    if request.method == "GET":
        body = TREE.get(rel)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body)
    return httpx.Response(405)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


def _run(coro_factory):
    async def main():
        async with _client() as client:
            return await coro_factory(client)

    return asyncio.run(main())


# ---------- walk ----------


def test_walk_finds_every_file_and_dir():
    files, dirs = _run(lambda c: walk_propfind(client=c, url_builder=_url, base_dir=""))
    assert sorted(f.path for f in files) == [
        "blob.bin", "data.csv", "notes/a.md", "notes/b.md", "notes/big.md",
    ]
    assert [d.path for d in dirs] == ["notes"]


def test_walk_parses_size_and_mtime():
    files, _ = _run(lambda c: walk_propfind(client=c, url_builder=_url, base_dir="notes"))
    a = next(f for f in files if f.path == "notes/a.md")
    assert a.size == len(TREE["notes/a.md"])
    assert a.mtime is not None and a.mtime.year == 2025


def test_walk_from_subfolder_returns_paths_from_share_root():
    files, _ = _run(lambda c: walk_propfind(client=c, url_builder=_url, base_dir="notes"))
    assert all(f.path.startswith("notes/") for f in files)


# ---------- find ----------


def test_find_by_glob():
    hits = _run(lambda c: find_entries(
        client=c, url_builder=_url, remote_folder="",
        spec=FindSpec(name_globs=("*.md",)),
    ))
    assert [h.path for h in hits] == ["notes/a.md", "notes/b.md", "notes/big.md"]


def test_find_dirs_only():
    hits = _run(lambda c: find_entries(
        client=c, url_builder=_url, remote_folder="", spec=FindSpec(kind="d"),
    ))
    assert [h.path for h in hits] == ["notes"]


# ---------- grep ----------


def _grep(spec: GrepSpec, folder: str = "", **kw):
    seen: dict[str, list] = {}
    stats = _run(lambda c: grep_tree(
        client=c, url_builder=_url, remote_folder=folder, spec=spec,
        on_file_result=lambda p, m: seen.__setitem__(p, m),
        progress=False, **kw,
    ))
    return seen, stats


def test_grep_finds_matches_across_the_tree():
    seen, stats = _grep(GrepSpec(pattern=compile_pattern("TODO")))
    assert set(seen) == {"notes/a.md", "data.csv"}
    assert stats.matches == 2


def test_grep_reports_correct_lineno_and_text():
    seen, _ = _grep(GrepSpec(pattern=compile_pattern("TODO")))
    (m,) = seen["notes/a.md"]
    assert (m.lineno, m.line) == (2, "TODO: fix me")


def test_grep_include_filter_limits_downloads():
    seen, stats = _grep(GrepSpec(pattern=compile_pattern("TODO"), include=("*.csv",)))
    assert set(seen) == {"data.csv"}
    assert stats.scanned == 1 and stats.skipped_filter == 4


def test_grep_exclude_filter():
    seen, _ = _grep(GrepSpec(pattern=compile_pattern("TODO"), exclude=("notes/*",)))
    assert set(seen) == {"data.csv"}


def test_grep_skips_binary_files():
    """blob.bin contains TODO but starts with NUL bytes — must be skipped, not matched."""
    seen, stats = _grep(GrepSpec(pattern=compile_pattern("TODO")))
    assert "blob.bin" not in seen
    assert stats.skipped_binary == 1


def test_grep_binary_opt_in_matches_it():
    seen, _ = _grep(GrepSpec(pattern=compile_pattern("TODO"), skip_binary=False))
    assert "blob.bin" in seen


def test_grep_skips_oversized_files_before_downloading():
    """notes/big.md advertises 50MB via PROPFIND, so it never gets fetched."""
    _, stats = _grep(GrepSpec(pattern=compile_pattern("TODO"), max_bytes=1024))
    assert stats.skipped_size == 1


def test_grep_max_bytes_zero_means_unlimited():
    seen, _ = _grep(GrepSpec(pattern=compile_pattern("TODO"), max_bytes=0))
    assert "notes/big.md" in seen


def test_grep_context_lines():
    seen, _ = _grep(GrepSpec(pattern=compile_pattern("TODO"), before=1, after=1, max_bytes=0))
    (m,) = seen["notes/a.md"]
    assert m.before == ((1, "alpha"),) and m.after == ((3, "omega"),)


def test_grep_ignore_case():
    seen, _ = _grep(GrepSpec(pattern=compile_pattern("todo", ignore_case=True)))
    assert "notes/a.md" in seen


def test_grep_subfolder_scope():
    seen, _ = _grep(GrepSpec(pattern=compile_pattern("TODO")), folder="notes")
    assert set(seen) == {"notes/a.md"}


def test_grep_no_matches_gives_empty_stats():
    seen, stats = _grep(GrepSpec(pattern=compile_pattern("zzz-nope")))
    assert seen == {} and stats.matched_files == 0 and stats.matches == 0


def test_grep_survives_a_failing_file():
    """One 404/500 must not abort the whole search — it is counted and skipped."""
    broken = dict(TREE)
    broken["notes/gone.md"] = b"TODO"

    def handler(request: httpx.Request) -> httpx.Response:
        rel = request.url.path[len("/dav"):].strip("/")
        if request.method == "GET" and rel == "notes/gone.md":
            return httpx.Response(500)
        return _handler(request)

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            with_gone = dict(TREE)
            return await grep_tree(
                client=c, url_builder=_url, remote_folder="notes",
                spec=GrepSpec(pattern=compile_pattern("TODO")), progress=False,
            )

    TREE["notes/gone.md"] = b"TODO"
    try:
        stats = asyncio.run(main())
    finally:
        TREE.pop("notes/gone.md")
    assert stats.errors == 1
    assert stats.matched_files == 1  # notes/a.md still reported


def test_grep_max_count_stops_after_n_hits_per_file():
    """Aborts the transfer early; the remaining matches in the file are not reported."""
    TREE["notes/many.md"] = b"TODO 1\nTODO 2\nTODO 3\n"
    try:
        seen, _ = _grep(GrepSpec(pattern=compile_pattern("TODO"), max_count=2))
    finally:
        TREE.pop("notes/many.md")
    assert [m.lineno for m in seen["notes/many.md"]] == [1, 2]
