"""Tests for CLI-local pure helpers: _humanize, _public_pwd."""
from __future__ import annotations

import pytest

from ncfetch.cli import _humanize, _public_pwd


def test_humanize_none():
    assert _humanize(None).strip() == "-"


def test_humanize_bytes_kb_mb():
    assert _humanize(0).strip().endswith("B")
    assert "K" in _humanize(2048)
    assert "M" in _humanize(5 * 1024 * 1024)
    assert "G" in _humanize(3 * 1024 * 1024 * 1024)


def test_humanize_huge_falls_back_to_tera():
    assert "T" in _humanize(10 * 1024 ** 4)


def test_public_pwd_cli_wins(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_PUBLIC_PASSWORD", "from-env")
    assert _public_pwd("from-cli") == "from-cli"


def test_public_pwd_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_PUBLIC_PASSWORD", "from-env")
    assert _public_pwd(None) == "from-env"


def test_public_pwd_empty_when_neither(monkeypatch):
    monkeypatch.delenv("NEXTCLOUD_PUBLIC_PASSWORD", raising=False)
    assert _public_pwd(None) == ""


def test_public_pwd_empty_cli_overrides_env(monkeypatch):
    """An explicit empty `--password ''` must override the env (not fall through)."""
    monkeypatch.setenv("NEXTCLOUD_PUBLIC_PASSWORD", "from-env")
    assert _public_pwd("") == ""
