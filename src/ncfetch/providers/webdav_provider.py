from __future__ import annotations
import asyncio
from pathlib import Path
from typing import AsyncIterator, Callable, List, Optional, Sequence, Tuple
from urllib.parse import quote

import httpx
from tqdm import tqdm

from ..config import Settings
from ..provider_base import StorageProvider
from ..webdav_dav import DAVEntry, propfind, parse_propfind

CHUNK = 1024 * 1024  # 1MB


class WebDAVProvider(StorageProvider):
    """
    以 Nextcloud WebDAV 實作：
    - 檔案下載：直接 GET /remote.php/dav/files/<user>/<path>
    - 資料夾下載：對資料夾路徑送 GET，並加 Accept: application/zip (Nextcloud 會回 ZIP)
    - 鏡像下載 / 列目錄：用 PROPFIND 走遠端樹
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
        remote_path = remote_path.lstrip("/")
        return f"{self.base}/{remote_path}" if remote_path else self.base

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(verify=self.verify, auth=self.auth, timeout=self.timeout)

    def _build_url_quoted(self, remote_path: str) -> str:
        """URL-encode remote path segments (上傳時用，避免空白/中文導致 400)."""
        return f"{self.base}/{quote(remote_path.lstrip('/'), safe='/')}"

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

    async def download_folder_zip(self, remote_folder: str, local_zip_path: Path) -> None:
        # Nextcloud 對資料夾做 GET + Accept: application/zip 會回 zip 檔
        url = self._build_url(remote_folder.rstrip("/"))
        headers = {"Accept": "application/zip"}
        local_zip_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._client() as client:
            f = await asyncio.to_thread(open, local_zip_path, "wb")
            try:
                async for chunk in self._stream(client, url, headers=headers):
                    await asyncio.to_thread(f.write, chunk)
            finally:
                await asyncio.to_thread(f.close)

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
        """逐層 MKCOL 建立資料夾 (idempotent)。空字串視為根，直接略過。"""
        parts = [p for p in remote_dir.strip("/").split("/") if p]
        cur = ""
        for p in parts:
            cur = f"{cur}/{p}" if cur else p
            await self._mkcol_one(client, cur)

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

        async with self._client() as client:
            if remote_root:
                await self.ensure_remote_dir(client, remote_root)
            for d in subdirs:
                rel = d.relative_to(local_folder).as_posix()
                target_dir = f"{remote_root}/{rel}" if remote_root else rel
                await self.ensure_remote_dir(client, target_dir)

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
            # 依深度建立目錄 (淺者優先)，去重避免重複呼叫
            seen: set[str] = set()
            for d in sorted(dirs_to_make, key=lambda s: len(s.split("/"))):
                if d and d not in seen:
                    await self.ensure_remote_dir(client, d)
                    seen.add(d)

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

    files: List[Tuple[str, Optional[int]]] = []
    queue: List[str] = [base_rel]
    while queue:
        cur = queue.pop(0)
        cur_url = url_builder(cur)
        resp = await propfind(client, cur_url, depth=1)
        resp.raise_for_status()
        entries = parse_propfind(
            cur_url if cur_url.endswith("/") else cur_url + "/", resp.content
        )
        for entry in entries:
            full = f"{cur}/{entry.rel_path}".strip("/") if cur else entry.rel_path
            if entry.is_dir:
                queue.append(full)
                (local_dir / _strip_base(full, base_rel)).mkdir(parents=True, exist_ok=True)
            else:
                files.append((full, entry.size))

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

        await asyncio.gather(*(fetch_one(p) for p, _ in files))


def _strip_base(full: str, base: str) -> str:
    if not base:
        return full
    if full == base:
        return ""
    if full.startswith(base + "/"):
        return full[len(base) + 1:]
    return full
