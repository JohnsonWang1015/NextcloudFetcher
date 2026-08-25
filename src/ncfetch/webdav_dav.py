from __future__ import annotations
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional
from urllib.parse import unquote, urlsplit

import httpx

NS = {"d": "DAV:"}


@dataclass(frozen=True)
class DAVEntry:
    """One entry from a PROPFIND multistatus response.

    rel_path is relative to the PROPFIND target (no leading slash). The target
    itself is filtered out by parse_propfind.
    """
    rel_path: str
    is_dir: bool
    size: Optional[int] = None
    mtime: Optional[datetime] = None


def parse_http_date(value: str | None) -> Optional[datetime]:
    """Parse a DAV getlastmodified (RFC 1123) value; None on anything unparseable.

    Always returns a tz-aware datetime — a date without a zone is read as UTC —
    so callers can compare mtimes without hitting naive/aware TypeErrors.
    """
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def propfind(client: httpx.AsyncClient, url: str, depth: int = 1) -> httpx.Response:
    headers = {
        "Depth": str(depth),
        "Content-Type": "text/xml; charset=utf-8",
    }
    return await client.request("PROPFIND", url, headers=headers, content=b"")


def parse_propfind(base_url: str, content: bytes) -> List[DAVEntry]:
    """Parse a DAV multistatus body and return non-self entries.

    base_url is the URL passed to PROPFIND; rel_path values are relative to it.
    Both the base and the response hrefs are URL-decoded and normalized before
    comparison so percent-encoded paths line up correctly.
    """
    base_path = urlsplit(base_url).path
    base_norm = unquote(base_path).rstrip("/") + "/"

    entries: List[DAVEntry] = []
    root = ET.fromstring(content)
    for resp in root.findall("d:response", NS):
        href_el = resp.find("d:href", NS)
        if href_el is None or not href_el.text:
            continue
        href_norm = unquote(urlsplit(href_el.text).path)

        if href_norm.rstrip("/") == base_norm.rstrip("/"):
            continue
        if not href_norm.startswith(base_norm):
            continue
        rel = href_norm[len(base_norm):].strip("/")
        if not rel:
            continue

        is_dir = resp.find(".//d:collection", NS) is not None
        size: Optional[int] = None
        cl_el = resp.find(".//d:getcontentlength", NS)
        if cl_el is not None and cl_el.text and cl_el.text.isdigit():
            size = int(cl_el.text)

        mt_el = resp.find(".//d:getlastmodified", NS)
        mtime = parse_http_date(mt_el.text if mt_el is not None else None)

        entries.append(DAVEntry(rel_path=rel, is_dir=is_dir, size=size, mtime=mtime))
    return entries
