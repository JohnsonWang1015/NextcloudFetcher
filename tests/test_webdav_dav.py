"""Tests for ncfetch.webdav_dav.parse_propfind — pure XML parsing."""
from __future__ import annotations

import pytest

from ncfetch.webdav_dav import parse_propfind


def _xml(*responses: str) -> bytes:
    body = "\n".join(responses)
    return (
        f'<?xml version="1.0"?>'
        f'<d:multistatus xmlns:d="DAV:">{body}</d:multistatus>'
    ).encode()


def _resp(href: str, is_dir: bool = False, size: int | None = None) -> str:
    rtype = "<d:resourcetype><d:collection/></d:resourcetype>" if is_dir else "<d:resourcetype/>"
    cl = f"<d:getcontentlength>{size}</d:getcontentlength>" if size is not None else ""
    return f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{rtype}{cl}</d:prop></d:propstat></d:response>"


def test_self_target_is_excluded():
    """PROPFIND on a folder returns the folder itself as the first entry — must be filtered."""
    xml = _xml(
        _resp("/dav/MyFolder/", is_dir=True),
        _resp("/dav/MyFolder/inner.txt", size=42),
    )
    entries = parse_propfind("https://h/dav/MyFolder/", xml)
    assert len(entries) == 1
    assert entries[0].rel_path == "inner.txt"
    assert entries[0].size == 42


def test_percent_encoded_href_decodes():
    """Nextcloud returns hrefs URL-encoded; rel_path must come back decoded."""
    xml = _xml(
        _resp("/dav/Folder/Sub%20Dir/", is_dir=True),
        _resp("/dav/Folder/Caf%C3%A9.pdf", size=99),
    )
    entries = parse_propfind("https://h/dav/Folder/", xml)
    rels = sorted(e.rel_path for e in entries)
    assert rels == ["Café.pdf", "Sub Dir"]


def test_dir_vs_file_distinction():
    xml = _xml(
        _resp("/dav/F/", is_dir=True),
        _resp("/dav/F/sub/", is_dir=True),
        _resp("/dav/F/file.bin", size=10),
    )
    entries = parse_propfind("https://h/dav/F/", xml)
    by_name = {e.rel_path: e for e in entries}
    assert by_name["sub"].is_dir is True
    assert by_name["sub"].size is None
    assert by_name["file.bin"].is_dir is False
    assert by_name["file.bin"].size == 10


def test_non_matching_href_is_skipped():
    """If a response href doesn't share the PROPFIND base path, skip it (don't crash)."""
    xml = _xml(
        _resp("/dav/F/keep.txt", size=1),
        _resp("/some/other/path/oops.txt", size=2),
    )
    entries = parse_propfind("https://h/dav/F/", xml)
    rels = [e.rel_path for e in entries]
    assert rels == ["keep.txt"]


def test_missing_contentlength_yields_none_size():
    xml = _xml(_resp("/dav/F/x.bin"))  # no <getcontentlength>
    entries = parse_propfind("https://h/dav/F/", xml)
    assert entries[0].size is None


def test_base_url_without_trailing_slash_still_matches():
    """Tolerate callers passing base without trailing slash."""
    xml = _xml(
        _resp("/dav/F/", is_dir=True),
        _resp("/dav/F/a.txt", size=1),
    )
    entries = parse_propfind("https://h/dav/F", xml)
    assert [e.rel_path for e in entries] == ["a.txt"]
