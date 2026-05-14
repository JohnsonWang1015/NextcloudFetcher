from __future__ import annotations
import asyncio
import logging
import os
from pathlib import Path
from typing import Iterable, Optional

import typer

from .config import get_settings
from .providers.webdav_provider import WebDAVProvider
from .providers.public_share_provider import PublicShareProvider
from .utils import unzip
from .webdav_dav import DAVEntry

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

app = typer.Typer(help="Nextcloud Downloader (WebDAV) — 檔案/資料夾下載 (資料夾輸出為 ZIP，可選自動解壓)")

# 解析 public share 密碼：優先 CLI -> 環境變數 -> 空字串
def _public_pwd(cli_pwd: Optional[str]) -> str:
    if cli_pwd is not None:
        return cli_pwd
    env_pwd = os.getenv("NEXTCLOUD_PUBLIC_PASSWORD")
    return env_pwd if env_pwd is not None else ""


def _humanize(n: Optional[int]) -> str:
    if n is None:
        return "        -"
    units = ["B", "K", "M", "G", "T"]
    f = float(n)
    for u in units:
        if f < 1024.0 or u == "T":
            return f"{f:7.1f}{u}"
        f /= 1024.0
    return f"{f:7.1f}T"


def _print_listing(entries: Iterable[DAVEntry]) -> None:
    items = sorted(entries, key=lambda e: (not e.is_dir, e.rel_path.lower()))
    for e in items:
        kind = "d" if e.is_dir else "f"
        size = "        -" if e.is_dir else _humanize(e.size)
        typer.echo(f"{kind}  {size}  {e.rel_path}")


@app.command("file")
def download_file(
    remote_path: str = typer.Argument(..., help="遠端檔案相對路徑，例如: 'Projects/report.pdf'"),
    out: Path = typer.Option(Path("./downloads"), "--out", "-o", help="本機輸出資料夾"),
):
    """下載單一檔案"""
    s = get_settings()
    provider = WebDAVProvider(s)
    out.mkdir(parents=True, exist_ok=True)
    local_path = out / Path(remote_path).name

    async def run():
        await provider.download_file(remote_path, local_path)
        typer.echo(f"✅ 已下載：{local_path}")

    asyncio.run(run())


@app.command("folder")
def download_folder(
    remote_folder: str = typer.Argument(..., help="遠端資料夾相對路徑，例如: 'Datasets/flood-tiles'"),
    out_zip: Path = typer.Option(Path("./downloads/folder.zip"), "--zip", help="ZIP 存檔路徑"),
    unzip_to: Path | None = typer.Option(None, "--unzip-to", help="自動解壓縮到此資料夾（可選）"),
):
    """下載整個資料夾 (Nextcloud 會回 ZIP 檔)，可選擇自動解壓。"""
    s = get_settings()
    provider = WebDAVProvider(s)

    async def run():
        await provider.download_folder_zip(remote_folder, out_zip)
        typer.echo(f"✅ 資料夾 ZIP 已下載：{out_zip}")
        if unzip_to:
            unzip(out_zip, unzip_to)
            typer.echo(f"📦 已解壓縮到：{unzip_to}")

    asyncio.run(run())


@app.command("ls")
def list_folder(
    remote_folder: str = typer.Argument("", help="遠端資料夾相對路徑；留空為根目錄"),
):
    """列出遠端資料夾內容 (使用者帳號)"""
    s = get_settings()
    provider = WebDAVProvider(s)

    async def run():
        entries = await provider.list_folder(remote_folder)
        _print_listing(entries)

    asyncio.run(run())


@app.command("mirror")
def mirror_folder(
    remote_folder: str = typer.Argument(..., help="遠端資料夾相對路徑"),
    out_dir: Path = typer.Option(Path("./downloads/mirror"), "--out", "-o", help="本機鏡像根目錄"),
    workers: int = typer.Option(8, "--workers", "-w", min=1, max=64, help="並行下載數"),
):
    """鏡像下載：把遠端資料夾照原樹狀結構同步到本機 (不打 ZIP)。"""
    s = get_settings()
    provider = WebDAVProvider(s)

    async def run():
        await provider.download_folder_tree(remote_folder, out_dir, workers=workers)
        typer.echo(f"✅ 鏡像完成：{out_dir}")

    asyncio.run(run())


@app.command("public-file")
def public_download_file(
    token_or_url: str = typer.Argument(..., help="公開分享 token 或完整 URL，例如：TDYLSwBgEkQgb 或 https://host/s/TDYLSwBgEkQgb"),
    remote_path: str = typer.Argument(..., help="分享中的相對路徑，如 'Slides.pptx'"),
    out: Path = typer.Option(Path("./downloads"), "--out", "-o", help="本機輸出資料夾"),
    password: Optional[str] = typer.Option(None, "--password", "-p", help="分享密碼 (若有設定)"),
):
    """從公開分享下載單一檔案"""
    s = get_settings()
    provider = PublicShareProvider(s, token_or_url, _public_pwd(password))
    out.mkdir(parents=True, exist_ok=True)
    local_path = out / Path(remote_path).name

    async def run():
        await provider.download_file(remote_path, local_path)
        typer.echo(f"✅ 已下載：{local_path}")

    asyncio.run(run())


@app.command("public-folder")
def public_download_folder(
    token_or_url: str = typer.Argument(..., help="公開分享 token 或完整 URL"),
    remote_folder: str = typer.Argument(..., help="分享中的資料夾相對路徑 (例如 'ParentFolder/SubFolder')"),
    out_zip: Path = typer.Option(Path("./downloads/share.zip"), "--zip", help="ZIP 存檔路徑"),
    unzip_to: Path | None = typer.Option(None, "--unzip-to", help="自動解壓縮到此資料夾 (可選)"),
    password: Optional[str] = typer.Option(None, "--password", "-p", help="分享密碼 (若有設定)"),
    workers: int = typer.Option(8, "--workers", "-w", min=1, max=64, help="tier-3 並行下載數"),
):
    """從公開分享下載整個資料夾 (ZIP)，可選擇自動解壓"""
    s = get_settings()
    provider = PublicShareProvider(s, token_or_url, _public_pwd(password))

    async def run():
        await provider.download_folder_zip(remote_folder, out_zip, workers=workers)
        typer.echo(f"✅ 公開分享資料夾 ZIP 已下載：{out_zip}")
        if unzip_to:
            unzip(out_zip, unzip_to)
            typer.echo(f"📦 已解壓縮到：{unzip_to}")

    asyncio.run(run())


@app.command("public-ls")
def public_list_folder(
    token_or_url: str = typer.Argument(..., help="公開分享 token 或完整 URL"),
    remote_folder: str = typer.Argument("", help="分享中的相對路徑；留空為分享根目錄"),
    password: Optional[str] = typer.Option(None, "--password", "-p", help="分享密碼 (若有設定)"),
):
    """列出公開分享內容"""
    s = get_settings()
    provider = PublicShareProvider(s, token_or_url, _public_pwd(password))

    async def run():
        entries = await provider.list_folder(remote_folder)
        _print_listing(entries)

    asyncio.run(run())


@app.command("public-mirror")
def public_mirror_folder(
    token_or_url: str = typer.Argument(..., help="公開分享 token 或完整 URL"),
    remote_folder: str = typer.Argument(..., help="分享中的資料夾相對路徑"),
    out_dir: Path = typer.Option(Path("./downloads/mirror"), "--out", "-o", help="本機鏡像根目錄"),
    workers: int = typer.Option(8, "--workers", "-w", min=1, max=64, help="並行下載數"),
    password: Optional[str] = typer.Option(None, "--password", "-p", help="分享密碼 (若有設定)"),
):
    """鏡像下載公開分享：依原樹狀結構同步到本機 (不打 ZIP)。"""
    s = get_settings()
    provider = PublicShareProvider(s, token_or_url, _public_pwd(password))

    async def run():
        await provider.download_folder_tree(remote_folder, out_dir, workers=workers)
        typer.echo(f"✅ 鏡像完成：{out_dir}")

    asyncio.run(run())
