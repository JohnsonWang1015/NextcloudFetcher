from __future__ import annotations
from pathlib import Path
from zipfile import ZipFile
from tqdm import tqdm

def unzip(zip_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path, "r") as zf:
        items = zf.infolist()
        for m in tqdm(items, desc="Unzip", unit="file"):
            zf.extract(m, path=out_dir)