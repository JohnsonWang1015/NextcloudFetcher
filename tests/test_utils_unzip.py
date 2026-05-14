"""Tests for utils.unzip — happy path + Zip Slip rejection."""
from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest

from ncfetch.utils import ZipSlipError, unzip


def _build_zip(zip_path: Path, members: dict[str, bytes]) -> None:
    with ZipFile(zip_path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def test_unzip_happy_path(tmp_path: Path):
    z = tmp_path / "ok.zip"
    _build_zip(z, {"a.txt": b"hello", "sub/b.txt": b"world"})
    out = tmp_path / "out"
    unzip(z, out)
    assert (out / "a.txt").read_bytes() == b"hello"
    assert (out / "sub" / "b.txt").read_bytes() == b"world"


def test_unzip_rejects_parent_traversal(tmp_path: Path):
    z = tmp_path / "evil.zip"
    _build_zip(z, {"../escape.txt": b"pwned"})
    out = tmp_path / "out"
    with pytest.raises(ZipSlipError):
        unzip(z, out)
    # Nothing should have been written (we check before extracting)
    assert not (tmp_path / "escape.txt").exists()


def test_unzip_rejects_absolute_path(tmp_path: Path):
    z = tmp_path / "evil.zip"
    _build_zip(z, {"/tmp/escape.txt": b"pwned"})
    out = tmp_path / "out"
    with pytest.raises(ZipSlipError):
        unzip(z, out)


def test_unzip_rejects_mixed_innocent_and_evil(tmp_path: Path):
    """If any member escapes, the whole archive is refused — atomic check."""
    z = tmp_path / "mixed.zip"
    _build_zip(z, {"safe.txt": b"ok", "../escape.txt": b"pwned"})
    out = tmp_path / "out"
    with pytest.raises(ZipSlipError):
        unzip(z, out)
    # The "safe" file must not have leaked into out either
    assert not (out / "safe.txt").exists()
