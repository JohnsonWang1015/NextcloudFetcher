"""Tests for MKCOL batching in WebDAVProvider.ensure_remote_dirs.

The regression these guard: ensure_remote_dir used to be called once per
subdirectory, and each call re-issued MKCOL for every ancestor — so a wide tree
cost O(dirs x depth) round-trips instead of O(dirs).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from ncfetch.providers.webdav_provider import WebDAVProvider


class _DummySettings:
    base_url = "https://nc.example.com"
    username = "alice"
    password = "secret"
    request_timeout = 30.0

    def webdav_base(self) -> str:
        return f"{self.base_url}/remote.php/dav/files/{self.username}"

    def verify_arg(self):
        return True


ROOT = "/remote.php/dav/files/alice/"


def _run_ensure(dirs, *, concurrency=4, existing=()):
    """Drive ensure_remote_dirs against a mock server; return MKCOL paths in order."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "MKCOL"
        rel = request.url.path[len(ROOT):]
        calls.append(rel)
        # Nextcloud answers 405 for a collection that already exists.
        return httpx.Response(405 if rel in existing else 201)

    provider = WebDAVProvider(_DummySettings())

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await provider.ensure_remote_dirs(client, dirs, concurrency=concurrency)

    asyncio.run(main())
    return calls


def test_single_path_creates_each_level_once():
    assert _run_ensure(["a/b/c"]) == ["a", "a/b", "a/b/c"]


def test_shared_ancestors_are_not_recreated():
    """The whole point: a/b is created once, not once per leaf."""
    calls = _run_ensure(["a/b/c", "a/b/d", "a/b/e"])
    assert calls.count("a") == 1
    assert calls.count("a/b") == 1
    assert sorted(calls) == ["a", "a/b", "a/b/c", "a/b/d", "a/b/e"]


def test_wide_tree_issues_exactly_one_mkcol_per_distinct_dir():
    dirs = [f"root/proj{i}/src/utils" for i in range(10)]
    calls = _run_ensure(dirs)
    expected = {"root"}
    for i in range(10):
        expected |= {f"root/proj{i}", f"root/proj{i}/src", f"root/proj{i}/src/utils"}
    assert len(calls) == len(expected) == 31
    assert set(calls) == expected


def test_parents_are_created_before_children():
    """MKCOL 409s if the parent is missing, so depth order is load-bearing."""
    calls = _run_ensure(["x/y/z", "x/q"])
    for path in calls:
        parent = path.rsplit("/", 1)[0]
        if parent != path:
            assert calls.index(parent) < calls.index(path)


def test_duplicate_inputs_collapse():
    assert _run_ensure(["a/b", "a/b", "a/b/"]) == ["a", "a/b"]


def test_empty_input_is_a_noop():
    assert _run_ensure([]) == []


def test_empty_and_slash_only_paths_are_skipped():
    assert _run_ensure(["", "/", "//"]) == []


def test_leading_and_trailing_slashes_are_normalized():
    assert _run_ensure(["/a/b/"]) == ["a", "a/b"]


def test_existing_dirs_405_is_not_an_error():
    calls = _run_ensure(["a/b"], existing=("a",))
    assert calls == ["a", "a/b"]


def test_mkcol_error_status_still_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    provider = WebDAVProvider(_DummySettings())

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await provider.ensure_remote_dirs(client, ["a"])

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(main())


def test_ensure_remote_dir_single_still_walks_every_level():
    """Back-compat: the old single-dir entry point keeps its behaviour."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path[len(ROOT):])
        return httpx.Response(201)

    provider = WebDAVProvider(_DummySettings())

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await provider.ensure_remote_dir(client, "p/q/r")

    asyncio.run(main())
    assert calls == ["p", "p/q", "p/q/r"]


def test_concurrency_one_still_correct():
    assert _run_ensure(["a/b/c", "a/b/d"], concurrency=1) == ["a", "a/b", "a/b/c", "a/b/d"]
