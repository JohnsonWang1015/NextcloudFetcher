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


# ---------- _parse_size ----------


def test_parse_size_plain_bytes():
    from ncfetch.cli import _parse_size

    assert _parse_size("512") == 512


def test_parse_size_units():
    from ncfetch.cli import _parse_size

    assert _parse_size("10K") == 10 * 1024
    assert _parse_size("5M") == 5 * 1024 ** 2
    assert _parse_size("1.5G") == int(1.5 * 1024 ** 3)


def test_parse_size_accepts_b_and_ib_suffixes():
    from ncfetch.cli import _parse_size

    assert _parse_size("2MB") == _parse_size("2MiB") == 2 * 1024 ** 2


def test_parse_size_is_case_insensitive():
    from ncfetch.cli import _parse_size

    assert _parse_size("3m") == 3 * 1024 ** 2


def test_parse_size_none_passes_through():
    from ncfetch.cli import _parse_size

    assert _parse_size(None) is None


def test_parse_size_zero_means_unlimited():
    from ncfetch.cli import _parse_size

    assert _parse_size("0") == 0


def test_parse_size_rejects_garbage():
    import typer
    from ncfetch.cli import _parse_size

    with pytest.raises(typer.BadParameter):
        _parse_size("huge")


# ---------- _parse_age_cutoff ----------


def test_parse_age_cutoff_returns_tz_aware_past_instant():
    from datetime import datetime, timezone

    from ncfetch.cli import _parse_age_cutoff

    cutoff = _parse_age_cutoff("7d")
    assert cutoff.tzinfo is not None
    delta = datetime.now(timezone.utc) - cutoff
    assert 6.9 * 86400 < delta.total_seconds() < 7.1 * 86400


def test_parse_age_cutoff_units():
    from ncfetch.cli import _parse_age_cutoff

    assert (_parse_age_cutoff("30m") - _parse_age_cutoff("60m")).total_seconds() == pytest.approx(1800, abs=2)


def test_parse_age_cutoff_none_passes_through():
    from ncfetch.cli import _parse_age_cutoff

    assert _parse_age_cutoff(None) is None


def test_parse_age_cutoff_rejects_garbage():
    import typer
    from ncfetch.cli import _parse_age_cutoff

    with pytest.raises(typer.BadParameter):
        _parse_age_cutoff("yesterday")


# ---------- grep output formatting ----------


def test_fmt_grep_line_uses_colon_for_hits_and_dash_for_context():
    from ncfetch.cli import _fmt_grep_line

    assert _fmt_grep_line("a.md", 3, "text", ":", color=False) == "a.md:3:text"
    assert _fmt_grep_line("a.md", 3, "text", "-", color=False) == "a.md-3-text"


def test_highlight_is_a_noop_without_color():
    from ncfetch.cli import _highlight
    from ncfetch.search import compile_pattern

    assert _highlight("a TODO b", compile_pattern("TODO"), color=False) == "a TODO b"


def test_highlight_wraps_the_match_when_colored():
    from ncfetch.cli import _highlight
    from ncfetch.search import compile_pattern

    out = _highlight("a TODO b", compile_pattern("TODO"), color=True)
    assert "TODO" in out and out != "a TODO b"


def test_use_color_off_when_no_color_env_set(monkeypatch):
    from ncfetch.cli import _use_color

    monkeypatch.setenv("NO_COLOR", "1")
    assert not _use_color(False)


def test_use_color_off_when_flag_passed(monkeypatch):
    from ncfetch.cli import _use_color

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert not _use_color(True)
