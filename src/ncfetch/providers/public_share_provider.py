from __future__ import annotations
import asyncio
from pathlib import Path
from typing import AsyncIterator, Optional, List, Tuple
import httpx
from urllib.parse import quote
from zipfile import ZipFile, ZIP_DEFLATED
import xml.etree.ElementTree as ET
from tqdm import tqdm

from ..config import Settings
from ..provider_base import StorageProvider

CHUNK = 1024 * 1024  # 1MB
NS = {
    "d": "DAV:",
}

def _extract_token(token_or_url: str) -> str:
    t = token_or_url.strip().rstrip("/")
    if "/s/" in t:
        t = t.split("/s/", 1)[1]
    if "?" in t:
        t = t.split("?", 1)[0]
    return t


def _is_zip_response(resp: httpx.Response) -> bool:
    ct = (resp.headers.get("Content-Type") or "").lower()
    cd = (resp.headers.get("Content-Disposition") or "").lower()
    if "application/zip" in ct:
        return True
    if "attachment" in cd and ".zip" in cd:
        return True
    return False


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
        return f"{self.webdav_base}/{quote(remote_path, safe='/')}"

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
        async with httpx.AsyncClient(verify=self.verify, auth=self.auth, timeout=self.timeout) as client:
            f = await asyncio.to_thread(open, local_path, "wb")
            try:
                async for chunk in self._stream(client, url):
                    await asyncio.to_thread(f.write, chunk)
            finally:
                await asyncio.to_thread(f.close)

    # ---------- 資料夾 (帶三段式 fallback) ----------

    async def download_folder_zip(self, remote_folder: str, local_zip_path: Path) -> None:
        local_zip_path.parent.mkdir(parents=True, exist_ok=True)

        # 1) Web 端點 (最接近瀏覽器下載)
        try:
            await self._download_folder_zip_via_web(remote_folder, local_zip_path)
            return
        except Exception:
            # 若回到 HTML 或被拒絕，進入下一步
            pass

        # 2) Public WebDAV ZIP (部份版本支援)
        try:
            await self._download_folder_zip_via_webdav(remote_folder, local_zip_path)
            return
        except Exception:
            pass

        # 3) 遞迴列目錄＋逐檔下載 → 本機 ZIP
        await self._download_folder_zip_via_recursive_webdav(remote_folder, local_zip_path)

    async def _download_folder_zip_via_web(self, remote_folder: str, local_zip_path: Path) -> None:
        """走 /s/<token>/download?path=/folder，並確認回應真的是 zip。"""
        async with httpx.AsyncClient(verify=self.verify, timeout=self.timeout, follow_redirects=True) as client:
            await self._login_public_share(client)

            norm = "/" + remote_folder.lstrip("/")
            encoded_path = quote(norm, safe="/")
            download_url = f"{self.base_url}/s/{self.token}/download?path={encoded_path}"

            async with client.stream("GET", download_url) as resp:
                # 某些情況會回 HTML (WebDAV 介面說明)，必須檢查
                if not _is_zip_response(resp):
                    # 讀少量內容看是不是 HTML
                    head = await resp.aread(2048)
                    text_head = head.decode(errors="ignore").lower()
                    if "webdav" in text_head or "<html" in text_head:
                        raise RuntimeError("Web 端點未回 ZIP (疑似回到 WebDAV 說明頁或被拒絕)。")
                    # 若不是 HTML，但也不是 zip，就當成錯誤
                    raise RuntimeError(f"Web 端點回應非 ZIP (Content-Type={resp.headers.get('Content-Type')}).")

                f = await asyncio.to_thread(open, local_zip_path, "wb")
                try:
                    async for chunk in resp.aiter_bytes(chunk_size=CHUNK):
                        await asyncio.to_thread(f.write, chunk)
                finally:
                    await asyncio.to_thread(f.close)

    async def _download_folder_zip_via_webdav(self, remote_folder: str, local_zip_path: Path) -> None:
        global head
        """嘗試 Public WebDAV 對資料夾 GET + Accept: application/zip。"""
        url = self._build_webdav_url(remote_folder.rstrip("/"))
        headers = {"Accept": "application/zip"}
        async with httpx.AsyncClient(verify=self.verify, auth=self.auth, timeout=self.timeout) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if not _is_zip_response(resp):
                    # 讀一些看是不是 HTML
                    head = await resp.aread(2048)
                    text_head = head.decode(errors="ignore").lower()
                    if "webdav" in text_head or "<html" in text_head:
                        raise RuntimeError("Public WebDAV 未提供 ZIP 打包。")
                    raise RuntimeError(f"Public WebDAV 回應非 ZIP (Content-Type={resp.headers.get('Content-Type')}).")

                f = await asyncio.to_thread(open, local_zip_path, "wb")
                try:
                    await asyncio.to_thread(f.write, head)  # 已讀的前 2KB 也寫入
                    async for chunk in resp.aiter_bytes(chunk_size=CHUNK):
                        await asyncio.to_thread(f.write, chunk)
                finally:
                    await asyncio.to_thread(f.close)

    # ---------- 遞迴列目錄 + 打包 ----------

    async def _propfind(self, client: httpx.AsyncClient, href: str, depth: int = 1) -> httpx.Response:
        headers = {
            "Depth": str(depth),
            "Content-Type": "text/xml; charset=utf-8",
        }
        # PROPFIND body 最簡可為空；Nextcloud 接受空 body
        return await client.request("PROPFIND", href, headers=headers, content=b"")

    def _parse_propfind(self, base_href: str, content: bytes) -> List[Tuple[str, bool]]:
        """
        解析 Multi-Status，回傳 [(path, is_dir), ...]
        base_href: 呼叫 PROPFIND 時請求的 URL (用來還原相對路徑)
        """
        items: List[Tuple[str, bool]] = []
        root = ET.fromstring(content)
        for resp in root.findall("d:response", NS):
            href_el = resp.find("d:href", NS)
            if href_el is None or not href_el.text:
                continue
            href = href_el.text
            # Nextcloud 可能回 HTML-encoded / 或補尾斜線；這裡直接比對 string 前綴
            # 移除 base_href 前綴
            if href.startswith(base_href):
                rel = href[len(base_href):]
            else:
                # 嘗試寬鬆比對 (去掉尾斜線)
                bh = base_href.rstrip("/")
                if href.startswith(bh):
                    rel = href[len(bh):]
                else:
                    # 若無法對齊，跳過
                    continue
            rel = rel.lstrip("/")
            # 自己本身 (資料夾)會回一筆空 rel
            if rel == "":
                is_dir = True
            else:
                # is dir?
                is_dir = resp.find(".//d:collection", NS) is not None
            items.append((rel, is_dir))
        return items

    async def _download_folder_zip_via_recursive_webdav(self, remote_folder: str, local_zip_path: Path) -> None:
        """
        使用 Public WebDAV 遞迴列出資料夾內容，逐檔 GET，並在本機邊寫邊壓 ZIP。
        顯示 tqdm 進度：
        - 外層：已下載檔案數 (file)
        - 內層：單檔 bytes 進度 (如果 Content-Length 可得)
        """
        base_dir = remote_folder.strip("/")

        async with httpx.AsyncClient(
            verify=self.verify, auth=self.auth, timeout=self.timeout, follow_redirects=True
        ) as client:
            queue: List[str] = [base_dir]

            with ZipFile(local_zip_path, mode="w", compression=ZIP_DEFLATED) as zf:
                # 外層「檔案數」進度條 (未知總數，動態增加)
                with tqdm(total=0, unit="file", desc="Downloading (WebDAV→ZIP)", dynamic_ncols=True) as t_files:
                    while queue:
                        cur = queue.pop(0)  # 目前資料夾相對路徑 (可能為 "" 表根)
                        cur_url = self._build_webdav_url(cur) if cur else self.webdav_base

                        # PROPFIND depth=1：列出該層 (含自身)
                        resp = await self._propfind(client, cur_url, depth=1)
                        resp.raise_for_status()
                        items = self._parse_propfind(
                            cur_url if cur_url.endswith("/") else cur_url + "/", resp.content
                        )

                        for rel, is_dir in items:
                            # 跳過自身 (即 rel 為空)
                            if rel == "":
                                continue

                            zip_path = f"{cur}/{rel}".strip("/")
                            if is_dir:
                                # 目錄：加入 queue，並在 ZIP 中建空目錄 (可選)
                                queue.append(zip_path)
                                if not zip_path.endswith("/"):
                                    zf.writestr(zip_path + "/", b"")
                                else:
                                    zf.writestr(zip_path, b"")
                                continue

                            # 檔案：下載並寫入 ZIP
                            file_url = self._build_webdav_url(zip_path)
                            async with client.stream("GET", file_url) as fresp:
                                fresp.raise_for_status()

                                # 嘗試取得檔案大小以顯示 bytes 進度
                                total_bytes = None
                                cl = fresp.headers.get("Content-Length")
                                if cl and cl.isdigit():
                                    total_bytes = int(cl)

                                # 內層 bytes 進度條 (短暫顯示，完成後隱藏)
                                with tqdm(
                                    total=total_bytes if total_bytes is not None else 0,
                                    unit="B",
                                    unit_scale=True,
                                    desc=zip_path,
                                    dynamic_ncols=True,
                                    leave=False,
                                ) as t_bytes:
                                    with zf.open(zip_path, "w") as zf_entry:
                                        async for chunk in fresp.aiter_bytes(chunk_size=CHUNK):
                                            zf_entry.write(chunk)
                                            # 更新 bytes 進度 (若 total 不知道，tqdm 仍會顯示累積大小)
                                            t_bytes.update(len(chunk))

                            # 完成一個檔案
                            t_files.total += 1  # 動態增加 total
                            t_files.update(1)   # 已完成數 +1
                            # 顯示最後一個檔名在 postfix (截短避免過長)
                            t_files.set_postfix_str(zip_path[-60:])
                            t_files.refresh()