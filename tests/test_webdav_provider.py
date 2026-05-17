"""Tests for pure helpers in webdav_provider."""
from __future__ import annotations

import httpx

from ncfetch.providers.webdav_provider import (
    WebDAVProvider,
    _is_zip_response,
    _strip_base,
    build_folder_zip_via_propfind,
)


def test_strip_base_typical():
    assert _strip_base("Datasets/2025/a.csv", "Datasets/2025") == "a.csv"


def test_strip_base_nested():
    assert _strip_base("Datasets/2025/sub/b.csv", "Datasets/2025") == "sub/b.csv"


def test_strip_base_equal_to_base_returns_empty():
    assert _strip_base("Datasets/2025", "Datasets/2025") == ""


def test_strip_base_empty_base_passthrough():
    assert _strip_base("a/b", "") == "a/b"


def test_strip_base_no_prefix_match_keeps_full():
    """If full doesn't start with base, return as-is (caller's invariant violated)."""
    assert _strip_base("other/x", "Datasets") == "other/x"


# ---------- _is_zip_response (canonical location) ----------


def _make_response(headers: dict[str, str]) -> httpx.Response:
    return httpx.Response(200, headers=headers)


def test_is_zip_response_by_content_type():
    assert _is_zip_response(_make_response({"Content-Type": "application/zip"}))


def test_is_zip_response_by_disposition_when_octet_stream():
    """Some Nextcloud / reverse-proxy combos serve octet-stream + .zip filename."""
    assert _is_zip_response(
        _make_response(
            {"Content-Type": "application/octet-stream",
             "Content-Disposition": "attachment; filename=folder.zip"}
        )
    )


def test_is_zip_response_rejects_html():
    """Regression: original bug — server returned 200 + HTML and we wrote it as ZIP."""
    assert not _is_zip_response(_make_response({"Content-Type": "text/html; charset=utf-8"}))


def test_is_zip_response_rejects_webdav_multistatus():
    """User WebDAV GET on a folder often returns a multistatus XML — must not pass as ZIP."""
    assert not _is_zip_response(
        _make_response({"Content-Type": "application/xml; charset=utf-8"})
    )


# ---------- re-export sanity ----------


def test_is_zip_response_reexport_is_same_function():
    """public_share_provider must continue to expose _is_zip_response (used by tests/imports)."""
    from ncfetch.providers import public_share_provider as ps

    assert ps._is_zip_response is _is_zip_response


def test_build_folder_zip_via_propfind_importable():
    """Shared tier-2 helper must be reachable from the canonical module."""
    assert callable(build_folder_zip_via_propfind)


# ---------- WebDAVProvider._build_url URL-encoding ----------


class _DummySettings:
    """Minimal Settings-like stub for testing _build_url without touching .env."""
    base_url = "https://nc.example.com"
    username = "alice"
    password = "secret"
    request_timeout = 30.0

    def webdav_base(self) -> str:
        return f"{self.base_url}/remote.php/dav/files/{self.username}"

    def verify_arg(self):
        return True


def test_build_url_encodes_chinese_path():
    """The original bug context: 中文路徑 must percent-encode."""
    p = WebDAVProvider(_DummySettings())
    url = p._build_url("出差")
    assert url == "https://nc.example.com/remote.php/dav/files/alice/%E5%87%BA%E5%B7%AE"


def test_build_url_preserves_slashes():
    p = WebDAVProvider(_DummySettings())
    url = p._build_url("Datasets/2025/foo bar.csv")
    assert url == "https://nc.example.com/remote.php/dav/files/alice/Datasets/2025/foo%20bar.csv"


def test_build_url_empty_returns_base():
    p = WebDAVProvider(_DummySettings())
    assert p._build_url("") == "https://nc.example.com/remote.php/dav/files/alice"


def test_build_url_quoted_alias_matches_build_url():
    """Both names must produce identical URLs (kept as alias for back-compat)."""
    p = WebDAVProvider(_DummySettings())
    assert p._build_url("a/b c") == p._build_url_quoted("a/b c")
