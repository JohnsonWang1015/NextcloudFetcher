"""Tests for the _strip_base helper in webdav_provider."""
from __future__ import annotations

from ncfetch.providers.webdav_provider import _strip_base


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
