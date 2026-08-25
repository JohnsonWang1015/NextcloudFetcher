from __future__ import annotations
import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional

import typer
from tqdm import tqdm

from .config import get_settings
from .providers.webdav_provider import WebDAVProvider
from .providers.public_share_provider import PublicShareProvider
from .search import (
    FindSpec,
    GrepMatch,
    GrepSpec,
    GrepStats,
    RemoteFile,
    compile_pattern,
)
from .utils import unzip
from .webdav_dav import DAVEntry

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

app = typer.Typer(help="Nextcloud Fetcher (WebDAV) — 檔案/資料夾下載與上傳 (資料夾下載輸出為 ZIP，可選自動解壓)")

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


_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?:I?B)?\s*$", re.IGNORECASE)
_SIZE_MULT = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
_AGE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.IGNORECASE)
_AGE_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _parse_size(text: Optional[str]) -> Optional[int]:
    """'512' / '10K' / '5M' / '1.5G' → bytes. None passes through; 0 means 'no limit'."""
    if text is None:
        return None
    m = _SIZE_RE.match(text)
    if not m:
        raise typer.BadParameter(f"無法解析大小：{text!r}（可用 512、10K、5M、1.5G）")
    return int(float(m.group(1)) * _SIZE_MULT[m.group(2).upper()])


def _parse_age_cutoff(text: Optional[str]) -> Optional[datetime]:
    """'7d' / '12h' / '30m' → the UTC instant that far in the past."""
    if text is None:
        return None
    m = _AGE_RE.match(text)
    if not m:
        raise typer.BadParameter(f"無法解析時間長度：{text!r}（可用 30m、12h、7d、2w）")
    secs = float(m.group(1)) * _AGE_MULT[m.group(2).lower()]
    return datetime.now(timezone.utc) - timedelta(seconds=secs)


def _fmt_mtime(dt: Optional[datetime]) -> str:
    if dt is None:
        return " " * 16
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _print_listing(entries: Iterable[DAVEntry], long: bool = False) -> None:
    items = sorted(entries, key=lambda e: (not e.is_dir, e.rel_path.lower()))
    for e in items:
        kind = "d" if e.is_dir else "f"
        size = "        -" if e.is_dir else _humanize(e.size)
        when = f"  {_fmt_mtime(e.mtime)}" if long else ""
        typer.echo(f"{kind}  {size}{when}  {e.rel_path}")


def _print_found(nodes: Iterable[RemoteFile], long: bool = False) -> None:
    for n in nodes:
        if not long:
            typer.echo(n.path + ("/" if n.is_dir else ""))
            continue
        kind = "d" if n.is_dir else "f"
        size = "        -" if n.is_dir else _humanize(n.size)
        typer.echo(f"{kind}  {size}  {_fmt_mtime(n.mtime)}  {n.path}")


def _use_color(no_color: bool) -> bool:
    """Colorize only for a real terminal, and never when NO_COLOR is set."""
    if no_color or os.getenv("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _highlight(line: str, pattern: re.Pattern[str], color: bool) -> str:
    if not color:
        return line
    return pattern.sub(lambda m: typer.style(m.group(0), fg="red", bold=True) or m.group(0), line)


def _fmt_grep_line(path: str, lineno: int, text: str, sep: str, color: bool) -> str:
    """grep layout: 'path:lineno:text' for hits, 'path-lineno-text' for context."""
    return f"{_fmt_path(path, color)}{sep}{_fmt_lineno(lineno, color)}{sep}{text}"


def _fmt_path(path: str, color: bool) -> str:
    return typer.style(path, fg="magenta") if color else path


def _fmt_lineno(lineno: int, color: bool) -> str:
    return typer.style(str(lineno), fg="green") if color else str(lineno)


def _emit_matches(
    path: str,
    matches: List[GrepMatch],
    *,
    pattern: re.Pattern[str],
    color: bool,
    with_context: bool,
) -> None:
    """Print one file's matches. Uses tqdm.write so the progress bar stays intact."""
    out: List[str] = []
    prev_end: Optional[int] = None
    for m in matches:
        if with_context and prev_end is not None and m.lineno > prev_end + 1:
            out.append("--")
        for n, text in m.before:
            if prev_end is None or n > prev_end:
                out.append(_fmt_grep_line(path, n, text, "-", color))
        out.append(_fmt_grep_line(path, m.lineno, _highlight(m.line, pattern, color), ":", color))
        for n, text in m.after:
            out.append(_fmt_grep_line(path, n, text, "-", color))
        prev_end = max([m.lineno] + [n for n, _ in m.after])
    tqdm.write("\n".join(out))


def _echo_grep_summary(stats: GrepStats) -> None:
    """Skip counts go to stderr so stdout stays pipe-friendly."""
    sys.stdout.flush()  # keep the summary after the results when stdout is a pipe
    bits = [f"掃描 {stats.scanned} 檔", f"命中 {stats.matched_files} 檔 / {stats.matches} 行"]
    if stats.skipped_filter:
        bits.append(f"名稱過濾略過 {stats.skipped_filter}")
    if stats.skipped_size:
        bits.append(f"過大略過 {stats.skipped_size}")
    if stats.skipped_binary:
        bits.append(f"二進位略過 {stats.skipped_binary}")
    if stats.errors:
        bits.append(f"讀取失敗 {stats.errors}")
    typer.secho("🔎 " + "，".join(bits), err=True, fg="cyan")


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
    workers: int = typer.Option(8, "--workers", "-w", min=1, max=64, help="tier-2 並行下載數"),
):
    """下載整個資料夾。優先讓伺服器回 ZIP，若伺服器不支援就 PROPFIND 遞迴下載並在本機打包。"""
    s = get_settings()
    provider = WebDAVProvider(s)

    async def run():
        await provider.download_folder_zip(remote_folder, out_zip, workers=workers)
        typer.echo(f"✅ 資料夾 ZIP 已下載：{out_zip}")
        if unzip_to:
            unzip(out_zip, unzip_to)
            typer.echo(f"📦 已解壓縮到：{unzip_to}")

    asyncio.run(run())


@app.command("ls")
def list_folder(
    remote_folder: str = typer.Argument("", help="遠端資料夾相對路徑；留空為根目錄"),
    long: bool = typer.Option(False, "--long", "-l", help="顯示最後修改時間"),
):
    """列出遠端資料夾內容 (使用者帳號)"""
    s = get_settings()
    provider = WebDAVProvider(s)

    async def run():
        entries = await provider.list_folder(remote_folder)
        _print_listing(entries, long=long)

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
    long: bool = typer.Option(False, "--long", "-l", help="顯示最後修改時間"),
):
    """列出公開分享內容"""
    s = get_settings()
    provider = PublicShareProvider(s, token_or_url, _public_pwd(password))

    async def run():
        entries = await provider.list_folder(remote_folder)
        _print_listing(entries, long=long)

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


# ===================== 上傳 =====================

@app.command("upload")
def upload_file_cmd(
    local_path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, help="本機檔案路徑"),
    remote_path: Optional[str] = typer.Argument(None, help="遠端目標路徑 (含檔名)，例如 'Backups/photo.jpg'；省略則放到根目錄並沿用原檔名"),
    no_overwrite: bool = typer.Option(False, "--no-overwrite", help="若遠端已存在則中止 (預設覆蓋)"),
):
    """上傳單一檔案到 Nextcloud。"""
    s = get_settings()
    provider = WebDAVProvider(s)

    target = remote_path or local_path.name

    async def run():
        await provider.upload_file(
            local_path, target, overwrite=not no_overwrite
        )
        typer.echo(f"✅ 已上傳：{local_path} → /{target.lstrip('/')}")

    asyncio.run(run())


@app.command("upload-folder")
def upload_folder_cmd(
    local_folder: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True, help="本機資料夾路徑"),
    remote_folder: str = typer.Argument(..., help="遠端目標資料夾，例如 'Backups/2026'"),
    concurrency: int = typer.Option(4, "--concurrency", "-c", min=1, max=32, help="同時上傳檔案數 (預設 4)"),
    no_overwrite: bool = typer.Option(False, "--no-overwrite", help="若遠端檔案已存在則中止 (預設覆蓋)"),
):
    """遞迴上傳整個資料夾到 Nextcloud (本機資料夾的內容會放在 remote_folder 之下)。"""
    s = get_settings()
    provider = WebDAVProvider(s)

    async def run():
        await provider.upload_folder(
            local_folder, remote_folder,
            concurrency=concurrency, overwrite=not no_overwrite,
        )
        typer.echo(f"✅ 資料夾已上傳：{local_folder} → /{remote_folder.strip('/')}")

    asyncio.run(run())


@app.command("upload-batch")
def upload_batch_cmd(
    sources: List[Path] = typer.Argument(..., exists=True, help="多個本機檔案或資料夾路徑 (可混合)"),
    to: str = typer.Option(..., "--to", "-t", help="遠端目標資料夾，所有來源放在此資料夾之下"),
    concurrency: int = typer.Option(4, "--concurrency", "-c", min=1, max=32, help="同時上傳檔案數 (預設 4)"),
    no_overwrite: bool = typer.Option(False, "--no-overwrite", help="若遠端檔案已存在則中止 (預設覆蓋)"),
):
    """批次上傳多個檔案/資料夾到指定遠端資料夾。資料夾會保留結構放到 <to>/<資料夾名>/。"""
    s = get_settings()
    provider = WebDAVProvider(s)

    async def run():
        await provider.upload_many(
            sources, to, concurrency=concurrency, overwrite=not no_overwrite
        )
        typer.echo(f"✅ 批次上傳完成 ({len(sources)} 個來源) → /{to.strip('/')}")

    asyncio.run(run())


# ===================== 搜尋：find / grep / cat =====================


def _build_find_spec(
    *, name: Optional[List[str]], regex: Optional[str], kind: str,
    min_size: Optional[str], max_size: Optional[str],
    newer: Optional[str], older: Optional[str], ignore_case: bool,
) -> FindSpec:
    if kind not in ("any", "f", "d"):
        raise typer.BadParameter("--type 只能是 any / f / d")
    try:
        rx = re.compile(regex, re.IGNORECASE if ignore_case else 0) if regex else None
    except re.error as e:
        raise typer.BadParameter(f"--regex 不是合法的正規表示式：{e}")
    return FindSpec(
        name_globs=tuple(name or ()),
        regex=rx,
        kind=kind,
        min_size=_parse_size(min_size),
        max_size=_parse_size(max_size),
        newer_than=_parse_age_cutoff(newer),
        older_than=_parse_age_cutoff(older),
        ignore_case=ignore_case,
    )


def _run_find(provider, remote_folder: str, spec: FindSpec, long: bool) -> None:
    async def run():
        hits = await provider.find(remote_folder, spec)
        _print_found(hits, long=long)
        sys.stdout.flush()
        typer.secho(f"🔎 找到 {len(hits)} 筆", err=True, fg="cyan")
        if not hits:
            raise typer.Exit(code=1)

    asyncio.run(run())


def _build_grep_spec(
    *, pattern: str, ignore_case: bool, fixed_string: bool, invert: bool,
    include: Optional[List[str]], exclude: Optional[List[str]],
    max_size: str, before: int, after: int, context: int,
    max_count: int, binary: bool, encoding: str,
) -> GrepSpec:
    try:
        rx = compile_pattern(pattern, ignore_case=ignore_case, fixed_string=fixed_string)
    except re.error as e:
        raise typer.BadParameter(f"PATTERN 不是合法的正規表示式：{e}")
    if context:
        before = after = context
    return GrepSpec(
        pattern=rx,
        include=tuple(include or ()),
        exclude=tuple(exclude or ()),
        max_bytes=_parse_size(max_size) or 0,
        before=before,
        after=after,
        max_count=max_count,
        skip_binary=not binary,
        invert=invert,
        encoding=encoding,
    )


def _run_grep(
    provider, remote_folder: str, spec: GrepSpec, *,
    workers: int, files_only: bool, count_only: bool, no_color: bool, quiet: bool,
) -> None:
    color = _use_color(no_color)
    with_context = bool(spec.before or spec.after)

    def on_result(path: str, matches: List[GrepMatch]) -> None:
        if files_only:
            tqdm.write(_fmt_path(path, color))
        elif count_only:
            tqdm.write(f"{_fmt_path(path, color)}:{_fmt_lineno(len(matches), color)}")
        else:
            _emit_matches(path, matches, pattern=spec.pattern, color=color,
                          with_context=with_context)

    async def run():
        stats = await provider.grep(
            remote_folder, spec, workers=workers,
            on_file_result=on_result, progress=not quiet,
        )
        if not quiet:
            _echo_grep_summary(stats)
        if not stats.matched_files:
            raise typer.Exit(code=1)

    asyncio.run(run())


@app.command("find")
def find_cmd(
    remote_folder: str = typer.Argument("", help="搜尋起點資料夾；留空為帳號根目錄"),
    name: Optional[List[str]] = typer.Option(None, "--name", "-n", help="檔名 glob，可重複；如 '*.csv'（比對完整路徑與檔名）"),
    regex: Optional[str] = typer.Option(None, "--regex", "-e", help="以正規表示式比對相對路徑"),
    kind: str = typer.Option("any", "--type", "-t", help="any / f (檔案) / d (資料夾)"),
    min_size: Optional[str] = typer.Option(None, "--min-size", help="最小檔案大小，如 10K、5M"),
    max_size: Optional[str] = typer.Option(None, "--max-size", help="最大檔案大小，如 100M"),
    newer: Optional[str] = typer.Option(None, "--newer", help="只列出比這段時間更新的項目，如 7d、12h"),
    older: Optional[str] = typer.Option(None, "--older", help="只列出比這段時間更舊的項目，如 30d"),
    ignore_case: bool = typer.Option(False, "--ignore-case", "-i", help="名稱比對忽略大小寫"),
    long: bool = typer.Option(False, "--long", "-l", help="長格式輸出 (型態/大小/時間)"),
):
    """遞迴搜尋遠端檔名／資料夾名稱（依 glob、regex、大小、修改時間過濾）。"""
    spec = _build_find_spec(
        name=name, regex=regex, kind=kind, min_size=min_size, max_size=max_size,
        newer=newer, older=older, ignore_case=ignore_case,
    )
    _run_find(WebDAVProvider(get_settings()), remote_folder, spec, long)


@app.command("grep")
def grep_cmd(
    pattern: str = typer.Argument(..., help="要搜尋的正規表示式 (加 -F 則視為純文字)"),
    remote_folder: str = typer.Argument("", help="搜尋起點資料夾；留空為帳號根目錄"),
    ignore_case: bool = typer.Option(False, "--ignore-case", "-i", help="忽略大小寫"),
    fixed_string: bool = typer.Option(False, "--fixed-string", "-F", help="PATTERN 視為純文字，不當 regex"),
    invert: bool = typer.Option(False, "--invert-match", "-v", help="列出「不」符合的行"),
    include: Optional[List[str]] = typer.Option(None, "--include", help="只搜尋符合此 glob 的檔案，可重複；如 '*.md'"),
    exclude: Optional[List[str]] = typer.Option(None, "--exclude", help="排除符合此 glob 的檔案，可重複"),
    max_size: str = typer.Option("5M", "--max-size", help="超過此大小的檔案直接略過 (0 = 不限)"),
    context: int = typer.Option(0, "--context", "-C", min=0, help="同時顯示前後 N 行"),
    before: int = typer.Option(0, "--before-context", "-B", min=0, help="顯示命中行前 N 行"),
    after: int = typer.Option(0, "--after-context", "-A", min=0, help="顯示命中行後 N 行"),
    max_count: int = typer.Option(0, "--max-count", "-m", min=0, help="每個檔案最多幾筆命中就停止下載 (0 = 不限)"),
    files_only: bool = typer.Option(False, "--files-with-matches", "-l", help="只列出有命中的檔案路徑"),
    count_only: bool = typer.Option(False, "--count", "-c", help="每個檔案只印命中行數"),
    workers: int = typer.Option(8, "--workers", "-w", min=1, max=64, help="並行讀取檔案數"),
    binary: bool = typer.Option(False, "--binary", help="連二進位檔也一起搜尋 (預設略過)"),
    encoding: str = typer.Option("utf-8", "--encoding", help="解碼用編碼，如 utf-8、big5"),
    no_color: bool = typer.Option(False, "--no-color", help="停用顏色輸出"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="不顯示進度列與統計"),
):
    """全文搜尋：串流讀取遠端檔案內容並逐行比對（不落地、不需先下載）。

    只會下載通過 --include/--exclude 與 --max-size 篩選的檔案；命中 -m 上限後
    會立刻中斷該檔傳輸。找不到任何命中時 exit code 為 1（與 grep 相同）。
    """
    spec = _build_grep_spec(
        pattern=pattern, ignore_case=ignore_case, fixed_string=fixed_string, invert=invert,
        include=include, exclude=exclude, max_size=max_size, before=before, after=after,
        context=context, max_count=max_count, binary=binary, encoding=encoding,
    )
    _run_grep(
        WebDAVProvider(get_settings()), remote_folder, spec,
        workers=workers, files_only=files_only, count_only=count_only,
        no_color=no_color, quiet=quiet,
    )


@app.command("cat")
def cat_cmd(
    remote_path: str = typer.Argument(..., help="遠端檔案相對路徑"),
):
    """把遠端檔案內容直接印到 stdout（可接管線，如 | jq 或 | less）。"""
    provider = WebDAVProvider(get_settings())

    async def run():
        async for chunk in provider.stream_file(remote_path):
            await asyncio.to_thread(sys.stdout.buffer.write, chunk)
        await asyncio.to_thread(sys.stdout.buffer.flush)

    asyncio.run(run())


@app.command("public-find")
def public_find_cmd(
    token_or_url: str = typer.Argument(..., help="公開分享 token 或完整 URL"),
    remote_folder: str = typer.Argument("", help="搜尋起點；留空為分享根目錄"),
    password: Optional[str] = typer.Option(None, "--password", "-p", help="分享密碼 (若有設定)"),
    name: Optional[List[str]] = typer.Option(None, "--name", "-n", help="檔名 glob，可重複"),
    regex: Optional[str] = typer.Option(None, "--regex", "-e", help="以正規表示式比對相對路徑"),
    kind: str = typer.Option("any", "--type", "-t", help="any / f (檔案) / d (資料夾)"),
    min_size: Optional[str] = typer.Option(None, "--min-size", help="最小檔案大小，如 10K、5M"),
    max_size: Optional[str] = typer.Option(None, "--max-size", help="最大檔案大小，如 100M"),
    newer: Optional[str] = typer.Option(None, "--newer", help="只列出比這段時間更新的項目，如 7d"),
    older: Optional[str] = typer.Option(None, "--older", help="只列出比這段時間更舊的項目，如 30d"),
    ignore_case: bool = typer.Option(False, "--ignore-case", "-i", help="名稱比對忽略大小寫"),
    long: bool = typer.Option(False, "--long", "-l", help="長格式輸出"),
):
    """在公開分享中遞迴搜尋檔名／資料夾名稱。"""
    spec = _build_find_spec(
        name=name, regex=regex, kind=kind, min_size=min_size, max_size=max_size,
        newer=newer, older=older, ignore_case=ignore_case,
    )
    provider = PublicShareProvider(get_settings(), token_or_url, _public_pwd(password))
    _run_find(provider, remote_folder, spec, long)


@app.command("public-grep")
def public_grep_cmd(
    token_or_url: str = typer.Argument(..., help="公開分享 token 或完整 URL"),
    pattern: str = typer.Argument(..., help="要搜尋的正規表示式 (加 -F 則視為純文字)"),
    remote_folder: str = typer.Argument("", help="搜尋起點；留空為分享根目錄"),
    password: Optional[str] = typer.Option(None, "--password", "-p", help="分享密碼 (若有設定)"),
    ignore_case: bool = typer.Option(False, "--ignore-case", "-i", help="忽略大小寫"),
    fixed_string: bool = typer.Option(False, "--fixed-string", "-F", help="PATTERN 視為純文字"),
    invert: bool = typer.Option(False, "--invert-match", "-v", help="列出「不」符合的行"),
    include: Optional[List[str]] = typer.Option(None, "--include", help="只搜尋符合此 glob 的檔案，可重複"),
    exclude: Optional[List[str]] = typer.Option(None, "--exclude", help="排除符合此 glob 的檔案，可重複"),
    max_size: str = typer.Option("5M", "--max-size", help="超過此大小的檔案直接略過 (0 = 不限)"),
    context: int = typer.Option(0, "--context", "-C", min=0, help="同時顯示前後 N 行"),
    before: int = typer.Option(0, "--before-context", "-B", min=0, help="顯示命中行前 N 行"),
    after: int = typer.Option(0, "--after-context", "-A", min=0, help="顯示命中行後 N 行"),
    max_count: int = typer.Option(0, "--max-count", "-m", min=0, help="每檔命中上限 (0 = 不限)"),
    files_only: bool = typer.Option(False, "--files-with-matches", "-l", help="只列出有命中的檔案路徑"),
    count_only: bool = typer.Option(False, "--count", "-c", help="每個檔案只印命中行數"),
    workers: int = typer.Option(8, "--workers", "-w", min=1, max=64, help="並行讀取檔案數"),
    binary: bool = typer.Option(False, "--binary", help="連二進位檔也一起搜尋"),
    encoding: str = typer.Option("utf-8", "--encoding", help="解碼用編碼"),
    no_color: bool = typer.Option(False, "--no-color", help="停用顏色輸出"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="不顯示進度列與統計"),
):
    """在公開分享中做全文搜尋（串流讀取，不需先下載）。"""
    spec = _build_grep_spec(
        pattern=pattern, ignore_case=ignore_case, fixed_string=fixed_string, invert=invert,
        include=include, exclude=exclude, max_size=max_size, before=before, after=after,
        context=context, max_count=max_count, binary=binary, encoding=encoding,
    )
    provider = PublicShareProvider(get_settings(), token_or_url, _public_pwd(password))
    _run_grep(
        provider, remote_folder, spec,
        workers=workers, files_only=files_only, count_only=count_only,
        no_color=no_color, quiet=quiet,
    )


@app.command("public-cat")
def public_cat_cmd(
    token_or_url: str = typer.Argument(..., help="公開分享 token 或完整 URL"),
    remote_path: str = typer.Argument(..., help="分享中的檔案相對路徑"),
    password: Optional[str] = typer.Option(None, "--password", "-p", help="分享密碼 (若有設定)"),
):
    """把公開分享中的檔案內容直接印到 stdout。"""
    provider = PublicShareProvider(get_settings(), token_or_url, _public_pwd(password))

    async def run():
        async for chunk in provider.stream_file(remote_path):
            await asyncio.to_thread(sys.stdout.buffer.write, chunk)
        await asyncio.to_thread(sys.stdout.buffer.flush)

    asyncio.run(run())
