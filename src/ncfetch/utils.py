from __future__ import annotations
from pathlib import Path
from zipfile import ZipFile, ZipInfo
from tqdm import tqdm


class ZipSlipError(ValueError):
    """Raised when a zip member would extract outside the target directory."""


def _safe_target(out_root: Path, member_name: str) -> Path:
    """Resolve where `member_name` would extract under `out_root`, refusing escapes.

    Defends against absolute paths, `..` traversal, and any combination that
    would land outside out_root after resolution.
    """
    target = (out_root / member_name).resolve()
    if not target.is_relative_to(out_root):
        raise ZipSlipError(f"拒絕解壓越界路徑：{member_name!r}")
    return target


def unzip(zip_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_root = out_dir.resolve()
    with ZipFile(zip_path, "r") as zf:
        items = zf.infolist()
        # 預先檢查所有路徑，任何一筆越界就整個 ZIP 拒絕（避免部分解壓出來才發現）
        for m in items:
            _safe_target(out_root, m.filename)
        for m in tqdm(items, desc="Unzip", unit="file"):
            zf.extract(m, path=out_root)
