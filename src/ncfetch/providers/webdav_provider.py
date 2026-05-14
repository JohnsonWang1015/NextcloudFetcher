from __future__ import annotations
import asyncio
from pathlib import Path
from typing import AsyncIterator, Callable, List, Optional, Tuple

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
