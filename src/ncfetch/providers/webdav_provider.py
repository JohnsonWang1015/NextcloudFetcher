from __future__ import annotations
import asyncio
import logging
import tempfile
from pathlib import Path
from typing import AsyncIterator, Callable, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote
from zipfile import ZipFile, ZIP_DEFLATED

import httpx
from tqdm import tqdm

from ..config import Settings
from ..provider_base import StorageProvider
from ..search import (
    FindSpec,
    GrepMatch,
    GrepSpec,
    GrepStats,
    RemoteFile,
    find_entries,
    grep_tree,
    walk_propfind,
)
from ..webdav_dav import DAVEntry, propfind, parse_propfind

logger = logging.getLogger(__name__)

CHUNK = 1024 * 1024  # 1MB


def _is_zip_response(resp: httpx.Response) -> bool:
    """Sniff whether an HTTP response is actually a ZIP payload.

    Some Nextcloud builds/proxies return application/octet-stream with a .zip
    Content-Disposition filename instead of application/zip, so check both.
    """
    ct = (resp.headers.get("Content-Type") or "").lower()
    cd = (resp.headers.get("Content-Disposition") or "").lower()
    if "application/zip" in ct:
        return True
    if "attachment" in cd and ".zip" in cd:
        return True
    return False


class WebDAVProvider(StorageProvider):
    """
    以 Nextcloud WebDAV 實作：
    - 檔案下載：直接 GET /remote.php/dav/files/<user>/<path>
    - 資料夾下載：對資料夾路徑送 GET，並加 Accept: application/zip (Nextcloud 會回 ZIP)
    - 鏡像下載 / 列目錄 / find / grep：用 PROPFIND 走遠端樹
    - 檔案上傳：PUT /remote.php/dav/files/<user>/<path>
    - 資料夾上傳：MKCOL 建立目錄樹 + 對每個檔案 PUT (可並行)
    """

    def __init__(self, settings: Settings):
        self.s = settings
        self.base = settings.webdav_base()
        self.auth = (settings.username, settings.password)
        self.verify = settings.verify_arg()
        self.timeout = httpx.Timeout(settings.request_timeout)

    def _build_url(self, remote_path: str) -> str:
        """URL-encode path segments; safe='/' preserves separators."""
        remote_path = remote_path.lstrip("/")
        if not remote_path:
            return self.base
        return f"{self.base}/{quote(remote_path, safe='/')}"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(verify=self.verify, auth=self.auth, timeout=self.timeout)

    # Kept as an alias for callers that historically used the "quoted" name. Both
    # forms now percent-encode — _build_url and _build_url_quoted are equivalent.
    _build_url_quoted = _build_url

    async def _stream(self, client: httpx.AsyncClient, url: str, headers: dict | None = None) -> AsyncIterator[bytes]:
        async with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes(chunk_size=CHUNK):
                yield chunk

    async def download_file(self, remote_path: str, local_path: Path) -> None:
        url = self._build_url(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._client() as client:
            f = await asyncio.to_thread(open, local_path, "wb")
            try:
                async for chunk in self._stream(client, url):
                    await asyncio.to_thread(f.write, chunk)
            finally:
                await asyncio.to_thread(f.close)

    async def download_folder_zip(
        self, remote_folder: str, local_zip_path: Path, workers: int = 8
    ) -> None:
        """Two-tier folder ZIP download.

        Tier 1: GET <user-webdav>/<folder> with Accept: application/zip and
                validate the response is actually a ZIP. Some Nextcloud builds
                honor this and serve a server-built ZIP; others return an HTML
                or WebDAV directory listing — those are sniffed and rejected.

        Tier 2: recursive PROPFIND + parallel file downloads + local ZIP
                packing (the always-works fallback).
        """
        local_zip_path.parent.mkdir(parents=True, exist_ok=True)

        tiers = [
            ("user WebDAV zip", lambda: self._download_folder_zip_via_webdav(remote_folder, local_zip_path)),
            ("recursive PROPFIND", lambda: self._download_folder_zip_via_recursive(remote_folder, local_zip_path, workers)),
        ]
        last_exc: Optional[BaseException] = None
        for i, (name, fn) in enumerate(tiers):
            try:
                await fn()
                return
            except Exception as e:
                local_zip_path.unlink(missing_ok=True)
                last_exc = e
                if i < len(tiers) - 1:
                    logger.warning("Tier %d (%s) 失敗，改試下一個策略: %s", i + 1, name, e)
                else:
                    logger.error("所有 fallback 策略皆失敗，最後一個錯誤：%s", e)
        assert last_exc is not None
        raise last_exc

    async def _download_folder_zip_via_webdav(
        self, remote_folder: str, local_zip_path: Path
    ) -> None:
        """Tier 1: user WebDAV GET + Accept: application/zip, sniff before writing."""
        url = self._build_url(remote_folder.rstrip("/"))
        headers = {"Accept": "application/zip"}
        async with self._client() as client:
            async with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                if not _is_zip_response(resp):
                    head = await resp.aread(2048)
                    text_head = head.decode(errors="ignore").lower()
                    if "<html" in text_head or "<?xml" in text_head or "multistatus" in text_head:
                        raise RuntimeError("使用者 WebDAV 未回 ZIP (疑似 HTML/XML 目錄列表)。")
                    raise RuntimeError(
                        f"WebDAV 回應非 ZIP (Content-Type={resp.headers.get('Content-Type')})."
                    )

                f = await asyncio.to_thread(open, local_zip_path, "wb")
                try:
                    async for chunk in resp.aiter_bytes(chunk_size=CHUNK):
                        await asyncio.to_thread(f.write, chunk)
                finally:
                    await asyncio.to_thread(f.close)

    async def _download_folder_zip_via_recursive(
        self, remote_folder: str, local_zip_path: Path, workers: int
    ) -> None:
        """Tier 2: PROPFIND walk + parallel temp downloads + serial ZIP write."""
        async with self._client() as client:
            await build_folder_zip_via_propfind(
                client=client,
                url_builder=self._build_url,
                remote_folder=remote_folder,
                local_zip_path=local_zip_path,
                workers=workers,
                desc="Downloading (WebDAV→ZIP)",
            )

    async def list_folder(self, remote_folder: str) -> List[DAVEntry]:
        url = self._build_url(remote_folder.strip("/"))
        async with self._client() as client:
            resp = await propfind(client, url, depth=1)
            resp.raise_for_status()
            return parse_propfind(url if url.endswith("/") else url + "/", resp.content)

    async def download_folder_tree(
        self, remote_folder: str, local_dir: Path, workers: int = 8
    ) -> None:
        async with self._client() as client:
            await download_tree(
                client=client,
                url_builder=self._build_url,
                remote_folder=remote_folder,
                local_dir=local_dir,
                workers=workers,
            )

    # ---------- 搜尋：檔名 (find) 與內容 (grep) ----------

    async def stream_file(self, remote_path: str) -> AsyncIterator[bytes]:
        """Yield a remote file's bytes without touching disk (backs `ncfetch cat`)."""
        url = self._build_url(remote_path)
        async with self._client() as client:
            async for chunk in self._stream(client, url):
                yield chunk

    async def walk(self, remote_folder: str = "") -> Tuple[List[RemoteFile], List[RemoteFile]]:
        async with self._client() as client:
            return await walk_propfind(
                client=client, url_builder=self._build_url,
                base_dir=remote_folder.strip("/"),
            )

    async def find(self, remote_folder: str, spec: FindSpec) -> List[RemoteFile]:
        async with self._client() as client:
            return await find_entries(
                client=client, url_builder=self._build_url,
                remote_folder=remote_folder, spec=spec,
            )

    async def grep(
        self,
        remote_folder: str,
        spec: GrepSpec,
        *,
        workers: int = 8,
        on_file_result: Optional[Callable[[str, List[GrepMatch]], None]] = None,
        progress: bool = True,
    ) -> GrepStats:
        async with self._client() as client:
            return await grep_tree(
                client=client, url_builder=self._build_url,
                remote_folder=remote_folder, spec=spec, workers=workers,
                on_file_result=on_file_result, progress=progress,
            )

    # ---------- 上傳：目錄與檔案 ----------

    async def _mkcol_one(self, client: httpx.AsyncClient, remote_dir: str) -> None:
        """對單一層級執行 MKCOL；視 201/405 皆為 OK (已存在)。"""
        url = self._build_url_quoted(remote_dir)
        resp = await client.request("MKCOL", url)
        # 201 Created；405 表示已存在 (Nextcloud 對已存在的 collection 回 405)
        if resp.status_code in (201, 405):
            return
        resp.raise_for_status()

    async def ensure_remote_dir(self, client: httpx.AsyncClient, remote_dir: str) -> None:
        """逐層 MKCOL 建立單一資料夾 (idempotent)。空字串視為根，直接略過。"""
        await self.ensure_remote_dirs(client, [remote_dir], concurrency=1)

    async def ensure_remote_dirs(
        self,
        client: httpx.AsyncClient,
        dirs: Iterable[str],
        *,
        concurrency: int = 4,
    ) -> None:
        """MKCOL a whole set of directories at once — deduped, shallowest depth first.

        Every path implies its ancestors, and callers hand us overlapping trees
        (`a/b/c` and `a/b/d` share `a` and `a/b`), so expanding them into one set
        collapses what used to be one MKCOL per level *per directory* into one
        MKCOL per distinct directory. On a wide tree that is the difference
        between O(dirs x depth) round-trips and O(dirs).

        Depth levels stay serialized because MKCOL requires the parent to exist,
        but directories at the same depth are independent and go out in parallel.
        """
        needed: set[str] = set()
        for d in dirs:
            cur = ""
            for part in (d or "").strip("/").split("/"):
                if not part:
                    continue
                cur = f"{cur}/{part}" if cur else part
                needed.add(cur)
        if not needed:
            return

        by_depth: dict[int, List[str]] = {}
        for d in needed:
            by_depth.setdefault(d.count("/"), []).append(d)

        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(remote_dir: str) -> None:
            async with sem:
                await self._mkcol_one(client, remote_dir)

        for depth in sorted(by_depth):
            await asyncio.gather(*(one(d) for d in sorted(by_depth[depth])))

    async def _put_file(
        self,
        client: httpx.AsyncClient,
        local_path: Path,
        remote_path: str,
        *,
        overwrite: bool,
        progress_desc: str | None = None,
    ) -> None:
        url = self._build_url_quoted(remote_path)

        if not overwrite:
            head = await client.head(url)
            if head.status_code == 200:
                raise FileExistsError(f"遠端檔案已存在：{remote_path}")

        size = local_path.stat().st_size
        desc = progress_desc or local_path.name

        with tqdm(
            total=size, unit="B", unit_scale=True, desc=desc, dynamic_ncols=True, leave=False
        ) as bar:
            async def body() -> AsyncIterator[bytes]:
                f = await asyncio.to_thread(open, local_path, "rb")
                try:
                    while True:
                        chunk = await asyncio.to_thread(f.read, CHUNK)
                        if not chunk:
                            break
                        bar.update(len(chunk))
                        yield chunk
                finally:
                    await asyncio.to_thread(f.close)

            # 帶 Content-Length，部分伺服器與反向代理會更穩
            headers = {"Content-Length": str(size)}
            resp = await client.put(url, content=body(), headers=headers)
            resp.raise_for_status()

    async def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        *,
        overwrite: bool = True,
        ensure_parent: bool = True,
    ) -> None:
        if not local_path.exists() or not local_path.is_file():
            raise FileNotFoundError(f"找不到本機檔案：{local_path}")

        async with self._client() as client:
            if ensure_parent:
                parent = "/".join(remote_path.lstrip("/").split("/")[:-1])
                if parent:
                    await self.ensure_remote_dir(client, parent)
            await self._put_file(client, local_path, remote_path, overwrite=overwrite)

    async def upload_folder(
        self,
        local_folder: Path,
        remote_folder: str,
        *,
        concurrency: int = 4,
        overwrite: bool = True,
    ) -> None:
        if not local_folder.exists() or not local_folder.is_dir():
            raise NotADirectoryError(f"找不到本機資料夾：{local_folder}")

        files: List[Path] = sorted(p for p in local_folder.rglob("*") if p.is_file())
        subdirs: List[Path] = sorted(
            (p for p in local_folder.rglob("*") if p.is_dir()),
            key=lambda p: len(p.relative_to(local_folder).parts),
        )
        remote_root = remote_folder.strip("/")

        targets = [remote_root] if remote_root else []
        for d in subdirs:
            rel = d.relative_to(local_folder).as_posix()
            targets.append(f"{remote_root}/{rel}" if remote_root else rel)

        async with self._client() as client:
            await self.ensure_remote_dirs(client, targets, concurrency=concurrency)

            sem = asyncio.Semaphore(max(1, concurrency))
            outer = tqdm(
                total=len(files),
                unit="file",
                desc=f"Uploading → /{remote_root}" if remote_root else "Uploading → /",
                dynamic_ncols=True,
            )

            async def one(local_file: Path) -> None:
                rel = local_file.relative_to(local_folder).as_posix()
                target = f"{remote_root}/{rel}" if remote_root else rel
                async with sem:
                    await self._put_file(
                        client, local_file, target, overwrite=overwrite, progress_desc=rel
                    )
                outer.update(1)
                outer.set_postfix_str(rel[-60:])

            try:
                await asyncio.gather(*(one(p) for p in files))
            finally:
                outer.close()

    async def upload_many(
        self,
        sources: Sequence[Path],
        remote_folder: str,
        *,
        concurrency: int = 4,
        overwrite: bool = True,
    ) -> None:
        """
        批次上傳一組來源 (檔案或資料夾) 到指定遠端資料夾下。
        - 檔案：上傳到 <remote_folder>/<檔名>
        - 資料夾：以該資料夾名作為子目錄，遞迴上傳其下所有檔案
        """
        plan: List[Tuple[Path, str]] = []
        dirs_to_make: List[str] = []
        remote_root = remote_folder.strip("/")
        if remote_root:
            dirs_to_make.append(remote_root)

        for src in sources:
            if not src.exists():
                raise FileNotFoundError(f"找不到來源：{src}")
            if src.is_file():
                target = f"{remote_root}/{src.name}" if remote_root else src.name
                plan.append((src, target))
            elif src.is_dir():
                top = src.name
                top_remote = f"{remote_root}/{top}" if remote_root else top
                dirs_to_make.append(top_remote)
                for sub in sorted(p for p in src.rglob("*") if p.is_dir()):
                    rel = sub.relative_to(src).as_posix()
                    dirs_to_make.append(f"{top_remote}/{rel}")
                for f in sorted(p for p in src.rglob("*") if p.is_file()):
                    rel = f.relative_to(src).as_posix()
                    plan.append((f, f"{top_remote}/{rel}"))
            else:
                raise ValueError(f"不支援的來源類型：{src}")

        async with self._client() as client:
            await self.ensure_remote_dirs(client, dirs_to_make, concurrency=concurrency)

            sem = asyncio.Semaphore(max(1, concurrency))
            outer = tqdm(
                total=len(plan),
                unit="file",
                desc=f"Batch uploading → /{remote_root}" if remote_root else "Batch uploading → /",
                dynamic_ncols=True,
            )

            async def one(item: Tuple[Path, str]) -> None:
                local_file, target = item
                async with sem:
                    await self._put_file(
                        client, local_file, target, overwrite=overwrite, progress_desc=target
                    )
                outer.update(1)
                outer.set_postfix_str(target[-60:])

            try:
                await asyncio.gather(*(one(item) for item in plan))
            finally:
                outer.close()


async def download_tree(
    *,
    client: httpx.AsyncClient,
    url_builder: Callable[[str], str],
    remote_folder: str,
    local_dir: Path,
    workers: int,
) -> None:
    """Walk remote_folder via PROPFIND (sequential), download files in parallel.

    Shared helper used by both WebDAVProvider and PublicShareProvider — they only
    differ in url_builder, so the walker takes it as a callback.
    """
    base_rel = remote_folder.strip("/")
    local_dir.mkdir(parents=True, exist_ok=True)

    files, dirs = await walk_propfind(
        client=client, url_builder=url_builder, base_dir=base_rel
    )
    for d in dirs:
        (local_dir / _strip_base(d.path, base_rel)).mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(workers)

    with tqdm(total=len(files), unit="file", desc=f"Mirror {base_rel or '/'}", dynamic_ncols=True) as bar:
        async def fetch_one(full_path: str) -> None:
            url = url_builder(full_path)
            rel = _strip_base(full_path, base_rel)
            out = local_dir / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            async with sem:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    f = await asyncio.to_thread(open, out, "wb")
                    try:
                        async for chunk in resp.aiter_bytes(chunk_size=CHUNK):
                            await asyncio.to_thread(f.write, chunk)
                    finally:
                        await asyncio.to_thread(f.close)
            bar.update(1)

        await asyncio.gather(*(fetch_one(f.path) for f in files))


def _strip_base(full: str, base: str) -> str:
    if not base:
        return full
    if full == base:
        return ""
    if full.startswith(base + "/"):
        return full[len(base) + 1:]
    return full


# Canonical implementation now lives in ncfetch.search (shared with find/grep).
# Kept under the old private name for callers that imported it from here.
_walk_propfind = walk_propfind


async def build_folder_zip_via_propfind(
    *,
    client: httpx.AsyncClient,
    url_builder: Callable[[str], str],
    remote_folder: str,
    local_zip_path: Path,
    workers: int,
    desc: str = "Downloading (WebDAV→ZIP)",
) -> None:
    """PROPFIND walk → parallel temp downloads → serial ZipFile write.

    Shared between WebDAVProvider (user auth) and PublicShareProvider (token
    auth). The url_builder callback hides the auth/path-shape difference.

    The asyncio.Lock around zf.write(...) is load-bearing: zipfile.ZipFile is
    not thread/coroutine-safe. Removing the lock will silently corrupt the
    central directory under any parallelism.
    """
    base_dir = remote_folder.strip("/")
    files, dirs = await _walk_propfind(
        client=client, url_builder=url_builder, base_dir=base_dir,
    )

    sem = asyncio.Semaphore(workers)
    write_lock = asyncio.Lock()

    with ZipFile(local_zip_path, mode="w", compression=ZIP_DEFLATED) as zf:
        for d in dirs:
            zf.writestr(d.path.rstrip("/") + "/", b"")

        with tqdm(total=len(files), unit="file", desc=desc, dynamic_ncols=True) as bar:
            async def fetch_and_pack(full_path: str) -> None:
                url = url_builder(full_path)
                async with sem:
                    tmp = tempfile.NamedTemporaryFile(delete=False)
                    tmp_path = Path(tmp.name)
                    try:
                        async with client.stream("GET", url) as fresp:
                            fresp.raise_for_status()
                            async for chunk in fresp.aiter_bytes(chunk_size=CHUNK):
                                await asyncio.to_thread(tmp.write, chunk)
                        await asyncio.to_thread(tmp.close)
                        async with write_lock:
                            await asyncio.to_thread(
                                zf.write, str(tmp_path), arcname=full_path
                            )
                    finally:
                        if not tmp.closed:
                            await asyncio.to_thread(tmp.close)
                        tmp_path.unlink(missing_ok=True)
                bar.update(1)
                bar.set_postfix_str(full_path[-60:])

            await asyncio.gather(*(fetch_and_pack(f.path) for f in files))
