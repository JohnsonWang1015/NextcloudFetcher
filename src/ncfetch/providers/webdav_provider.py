from __future__ import annotations
import asyncio
from pathlib import Path
import httpx
from typing import AsyncIterator
from ..config import Settings
from ..provider_base import StorageProvider

CHUNK = 1024 * 1024  # 1MB

class WebDAVProvider(StorageProvider):
    """
    以 Nextcloud WebDAV 實作：
    - 檔案下載：直接 GET /remote.php/dav/files/<user>/<path>
    - 資料夾下載：對資料夾路徑送 GET，並加 Accept: application/zip (Nextcloud 會回 ZIP)
    """

    def __init__(self, settings: Settings):
        self.s = settings
        self.base = settings.webdav_base()
        self.auth = (settings.username, settings.password)
        self.verify = settings.verify_arg()
        self.timeout = httpx.Timeout(settings.request_timeout)

    def _build_url(self, remote_path: str) -> str:
        remote_path = remote_path.lstrip("/")
        return f"{self.base}/{remote_path}"

    async def _stream(self, client: httpx.AsyncClient, url: str, headers: dict | None = None) -> AsyncIterator[bytes]:
        async with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes(chunk_size=CHUNK):
                yield chunk

    async def download_file(self, remote_path: str, local_path: Path) -> None:
        url = self._build_url(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(verify=self.verify, auth=self.auth, timeout=self.timeout) as client:
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
        async with httpx.AsyncClient(verify=self.verify, auth=self.auth, timeout=self.timeout) as client:
            f = await asyncio.to_thread(open, local_zip_path, "wb")
            try:
                async for chunk in self._stream(client, url, headers=headers):
                    await asyncio.to_thread(f.write, chunk)
            finally:
                await asyncio.to_thread(f.close)