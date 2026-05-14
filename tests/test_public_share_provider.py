"""Tests for token extraction and ZIP-response sniffing (pure helpers)."""
from __future__ import annotations

import httpx
import pytest

from ncfetch.providers.public_share_provider import _extract_token, _is_zip_response


def test_extract_token_from_bare_token():
    assert _extract_token("TDYLSwBgEkQgb") == "TDYLSwBgEkQgb"


def test_extract_token_from_full_url():
    assert _extract_token("https://cloud.example.com/s/TDYLSwBgEkQgb") == "TDYLSwBgEkQgb"


def test_extract_token_with_trailing_slash():
    assert _extract_token("https://cloud.example.com/s/TDYLSwBgEkQgb/") == "TDYLSwBgEkQgb"


def test_extract_token_strips_query_string():
    assert _extract_token("https://h/s/abc?download=1") == "abc"


def _make_response(headers: dict[str, str]) -> httpx.Response:
    return httpx.Response(200, headers=headers)


def test_is_zip_response_by_content_type():
    assert _is_zip_response(_make_response({"Content-Type": "application/zip"}))


def test_is_zip_response_by_content_disposition():
    assert _is_zip_response(
        _make_response({"Content-Type": "application/octet-stream", "Content-Disposition": "attachment; filename=foo.zip"})
    )


def test_is_zip_response_html_rejected():
    assert not _is_zip_response(_make_response({"Content-Type": "text/html"}))


def test_is_zip_response_no_headers():
    assert not _is_zip_response(_make_response({}))
