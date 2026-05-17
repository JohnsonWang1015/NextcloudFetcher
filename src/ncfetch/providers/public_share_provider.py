from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from typing import AsyncIterator, List, Optional
import httpx
from urllib.parse import quote

from ..config import Settings
from ..provider_base import StorageProvider
from ..webdav_dav import DAVEntry, propfind, parse_propfind
from .webdav_provider import (
    _is_zip_response,
    build_folder_zip_via_propfind,
    download_tree,
)

# Re-exported so existing imports keep working:
#   from ncfetch.providers.public_share_provider import _is_zip_response
__all__ = ["PublicShareProvider", "_extract_token", "_is_zip_response"]

logger = logging.getLogger(__name__)

CHUNK = 1024 * 1024  # 1MB


def _extract_token(token_or_url: str) -> str:
    t = token_or_url.strip().rstrip("/")
    if "/s/" in t:
        t = t.split("/s/", 1)[1]
    if "?" in t:
        t = t.split("?", 1)[0]
    return t


class PublicShareProvider(StorageProvider):
    """
    使用 Nextcloud 的 Public Web + Public WebDAV。
    Fallback 順序：
    1) Web 端點：GET /s/<token>/download?path=/資料夾
    2) Public WebDAV：GET public.php/webdav/<folder> + Accept: application/zip
    3) Public WebDAV 遞迴下載 → 本機即時壓成 ZIP
    Basic Auth: username=<token>, password=<share_password or ''>
    """

    def __init__(self, settings: Settings, token_or_url: str, password: Optional[str] = None):
        self.s = settings
        self.token = _extract_token(token_or_url)
        self.base_url = settings.base_url.rstrip("/")
        self.webdav_base = f"{self.base_url}/public.php/webdav"
        self.auth = (self.token, "" if password is None else password)
        self.share_password = password or ""
        self.verify = settings.verify_arg()
        self.timeout = httpx.Timeout(settings.request_timeout)

    # ---------- 基本 ----------

    def _build_webdav_url(self, remote_path: str) -> str:
        remote_path = remote_path.lstrip("/")
        if not remote_path:
            return self.webdav_base
        return f"{self.webdav_base}/{quote(remote_path, safe='/')}"

    def _client(self, follow_redirects: bool = False) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=self.verify,
            auth=self.auth,
            timeout=self.timeout,
            follow_redirects=follow_redirects,
        )

    async def _stream(self, client: httpx.AsyncClient, url: str, headers: dict | None = None) -> AsyncIterator[bytes]:
        async with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes(chunk_size=CHUNK):
                yield chunk

    async def _login_public_share(self, client: httpx.AsyncClient) -> None:
        """若分享有密碼，先 POST 建立 session cookie。"""
        if not self.share_password:
            return
        share_page = f"{self.base_url}/s/{self.token}"
        await client.get(share_page)
        r = await client.post(share_page, data={"password": self.share_password})
        r.raise_for_status()

    # ---------- 檔案 ----------

    async def download_file(self, remote_path: str, local_path: Path) -> None:
        url = self._build_webdav_url(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._client() as client:
            f = await asyncio.to_thread(open, local_path, "wb")
            try:
                async for chunk in self._stream(client, url):
                    await asyncio.to_thread(f.write, chunk)
            finally:
                await asyncio.to_thread(f.close)

    # ---------- 列目錄 / 鏡像 ----------

    async def list_folder(self, remote_folder: str) -> List[DAVEntry]:
        url = self._build_webdav_url(remote_folder.strip("/"))
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
                url_builder=self._build_webdav_url,
                remote_folder=remote_folder,
                local_dir=local_dir,
                workers=workers,
            )

    # ---------- 資料夾 (帶三段式 fallback) ----------

    async def download_folder_zip(
        self, remote_folder: str, local_zip_path: Path, workers: int = 8
    ) -> None:
        local_zip_path.parent.mkdir(parents=True, exist_ok=True)

        tiers = [
            ("web endpoint", lambda: self._download_folder_zip_via_web(remote_folder, local_zip_path)),
            ("public WebDAV zip", lambda: self._download_folder_zip_via_webdav(remote_folder, local_zip_path)),
            ("recursive PROPFIND", lambda: self._download_folder_zip_via_recursive_webdav(remote_folder, local_zip_path, workers)),
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

    async def _download_folder_zip_via_web(self, remote_folder: str, local_zip_path: Path) -> None:
        """走 /s/<token>/download?path=/folder，並確認回應真的是 zip。"""
        async with httpx.AsyncClient(verify=self.verify, timeout=self.timeout, follow_redirects=True) as client:
            await self._login_public_share(client)

            norm = "/" + remote_folder.lstrip("/")
            encoded_path = quote(norm, safe="/")
            download_url = f"{self.base_url}/s/{self.token}/download?path={encoded_path}"

            async with client.stream("GET", download_url) as resp:
                if not _is_zip_response(resp):
                    head = await resp.aread(2048)
                    text_head = head.decode(errors="ignore").lower()
                    if "webdav" in text_head or "<html" in text_head:
                        raise RuntimeError("Web 端點未回 ZIP (疑似回到 WebDAV 說明頁或被拒絕)。")
                    raise RuntimeError(
                        f"Web 端點回應非 ZIP (Content-Type={resp.headers.get('Content-Type')})."
                    )

                f = await asyncio.to_thread(open, local_zip_path, "wb")
                try:
                    async for chunk in resp.aiter_bytes(chunk_size=CHUNK):
                        await asyncio.to_thread(f.write, chunk)
                finally:
                    await asyncio.to_thread(f.close)

    async def _download_folder_zip_via_webdav(self, remote_folder: str, local_zip_path: Path) -> None:
        """嘗試 Public WebDAV 對資料夾 GET + Accept: application/zip。"""
        url = self._build_webdav_url(remote_folder.rstrip("/"))
        headers = {"Accept": "application/zip"}
        async with self._client() as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if not _is_zip_response(resp):
                    head = await resp.aread(2048)
                    text_head = head.decode(errors="ignore").lower()
                    if "webdav" in text_head or "<html" in text_head:
                        raise RuntimeError("Public WebDAV 未提供 ZIP 打包。")
                    raise RuntimeError(
                        f"Public WebDAV 回應非 ZIP (Content-Type={resp.headers.get('Content-Type')})."
                    )

                f = await asyncio.to_thread(open, local_zip_path, "wb")
                try:
                    async for chunk in resp.aiter_bytes(chunk_size=CHUNK):
                        await asyncio.to_thread(f.write, chunk)
                finally:
                    await asyncio.to_thread(f.close)

    # ---------- Tier 3：遞迴列目錄 + 並行下載 + 序列寫 ZIP ----------

    async def _download_folder_zip_via_recursive_webdav(
        self, remote_folder: str, local_zip_path: Path, workers: int = 8
    ) -> None:
        """Public WebDAV 遞迴 PROPFIND + 並行下載 + 序列寫 ZIP。

        Delegates to the shared build_folder_zip_via_propfind helper — same
        logic is used by WebDAVProvider tier-2; only the url_builder differs.
        """
        async with self._client(follow_redirects=True) as client:
            await build_folder_zip_via_propfind(
                client=client,
                url_builder=self._build_webdav_url,
                remote_folder=remote_folder,
                local_zip_path=local_zip_path,
                workers=workers,
                desc="Downloading (Public WebDAV→ZIP)",
            )
