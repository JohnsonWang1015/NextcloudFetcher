"""Remote search primitives: name matching (`find`) and content grep (`grep`).

The heavy lifting is split into two layers so the interesting parts stay pure
and unit-testable:

* pure helpers — glob/regex matching, binary sniffing, byte→line splitting and
  the context-aware `LineScanner`;
* async orchestrators — `walk_propfind`, `find_entries` and `grep_tree` — which
  take a `url_builder` callback so both WebDAVProvider (user auth) and
  PublicShareProvider (token auth) can share them, exactly like
  `download_tree` / `build_folder_zip_via_propfind` already do.

Grep never buffers a whole file: bytes are streamed, split into lines as they
arrive, and the transfer is aborted as soon as a per-file match cap is hit.
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterator, List, NamedTuple, Optional, Sequence, Tuple

import httpx
from tqdm import tqdm

from .webdav_dav import propfind, parse_propfind

logger = logging.getLogger(__name__)

CHUNK = 1024 * 1024  # 1MB
_BINARY_SNIFF = 8192


class RemoteFile(NamedTuple):
    """One remote node discovered by a PROPFIND walk (path relative to the share root)."""
    path: str
    size: Optional[int] = None
    mtime: Optional[datetime] = None
    is_dir: bool = False


# ===================== 純函式：比對 / 過濾 =====================


def compile_pattern(
    pattern: str, *, ignore_case: bool = False, fixed_string: bool = False
) -> re.Pattern[str]:
    """Build the regex used by grep. `fixed_string` escapes the pattern (grep -F)."""
    if fixed_string:
        pattern = re.escape(pattern)
    return re.compile(pattern, re.IGNORECASE if ignore_case else 0)


def match_globs(rel_path: str, globs: Sequence[str], *, ignore_case: bool = False) -> bool:
    """True if rel_path matches any glob, tested against the full path AND the basename.

    So `--include '*.py'` matches `a/b/c.py` and `--include 'Datasets/*'` also
    works. Uses fnmatchcase so behaviour does not change between Linux/Windows.
    """
    if not globs:
        return False
    name = rel_path.rsplit("/", 1)[-1]
    if ignore_case:
        rel_path, name = rel_path.lower(), name.lower()
        globs = [g.lower() for g in globs]
    return any(
        fnmatch.fnmatchcase(rel_path, g) or fnmatch.fnmatchcase(name, g) for g in globs
    )


def path_allowed(
    rel_path: str,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    *,
    ignore_case: bool = False,
) -> bool:
    """Include-then-exclude gate; exclude wins over include."""
    if include and not match_globs(rel_path, include, ignore_case=ignore_case):
        return False
    if exclude and match_globs(rel_path, exclude, ignore_case=ignore_case):
        return False
    return True


def looks_binary(data: bytes) -> bool:
    """A NUL byte in the first 8KB is the same heuristic grep/git use."""
    return b"\x00" in data[:_BINARY_SNIFF]


class ByteLineSplitter:
    """Incremental bytes → decoded lines (universal newlines, newline stripped).

    Handles chunk boundaries: a line split across two network chunks is emitted
    once, whole. Undecodable bytes become U+FFFD instead of raising.
    """

    def __init__(self, encoding: str = "utf-8") -> None:
        self.encoding = encoding
        self._buf = b""

    def feed(self, data: bytes) -> Iterator[str]:
        self._buf += data
        if b"\n" not in self._buf:
            return
        parts = self._buf.split(b"\n")
        self._buf = parts.pop()
        for raw in parts:
            yield self._decode(raw)

    def flush(self) -> Iterator[str]:
        """Emit the trailing line of a file that does not end with a newline."""
        if self._buf:
            raw, self._buf = self._buf, b""
            yield self._decode(raw)

    def _decode(self, raw: bytes) -> str:
        return raw.rstrip(b"\r").decode(self.encoding, errors="replace")


@dataclass(frozen=True)
class GrepMatch:
    """One matching line, plus its -B/-A context as (lineno, text) pairs."""
    path: str
    lineno: int
    line: str
    before: Tuple[Tuple[int, str], ...] = ()
    after: Tuple[Tuple[int, str], ...] = ()


@dataclass
class _Pending:
    lineno: int
    line: str
    before: Tuple[Tuple[int, str], ...]
    after: List[Tuple[int, str]] = field(default_factory=list)

    def build(self, path: str) -> GrepMatch:
        return GrepMatch(
            path=path, lineno=self.lineno, line=self.line,
            before=self.before, after=tuple(self.after),
        )


class LineScanner:
    """Feed lines one at a time; collect matches with before/after context.

    Kept separate from any I/O so the context bookkeeping (the fiddly part) is
    directly testable. `exhausted` lets the caller abort the download early once
    `max_count` matches are complete.
    """

    def __init__(
        self,
        path: str,
        pattern: re.Pattern[str],
        *,
        before: int = 0,
        after: int = 0,
        max_count: int = 0,
        invert: bool = False,
    ) -> None:
        self.path = path
        self.pattern = pattern
        self.before = max(0, before)
        self.after = max(0, after)
        self.max_count = max(0, max_count)
        self.invert = invert
        self.count = 0
        self._lineno = 0
        self._history: deque[Tuple[int, str]] = deque(maxlen=self.before)
        self._pending: List[_Pending] = []
        self._done: List[GrepMatch] = []

    @property
    def exhausted(self) -> bool:
        """max_count reached and no match is still waiting for its after-context."""
        return bool(self.max_count) and self.count >= self.max_count and not self._pending

    def feed(self, line: str) -> None:
        self._lineno += 1
        n = self._lineno

        still: List[_Pending] = []
        for p in self._pending:
            p.after.append((n, line))
            if len(p.after) >= self.after:
                self._done.append(p.build(self.path))
            else:
                still.append(p)
        self._pending = still

        room = not self.max_count or self.count < self.max_count
        if room and (bool(self.pattern.search(line)) != self.invert):
            self.count += 1
            p = _Pending(n, line, tuple(self._history))
            if self.after:
                self._pending.append(p)
            else:
                self._done.append(p.build(self.path))

        self._history.append((n, line))

    def finish(self) -> List[GrepMatch]:
        """Flush matches whose after-context was cut short by EOF."""
        for p in self._pending:
            self._done.append(p.build(self.path))
        self._pending = []
        self._done.sort(key=lambda m: m.lineno)
        return self._done


# ===================== 遞迴走訪 =====================


async def walk_propfind(
    *,
    client: httpx.AsyncClient,
    url_builder: Callable[[str], str],
    base_dir: str,
) -> Tuple[List[RemoteFile], List[RemoteFile]]:
    """Breadth-first PROPFIND walk. Returns (files, dirs) relative to the share root."""
    files: List[RemoteFile] = []
    dirs: List[RemoteFile] = []
    queue: List[str] = [base_dir]
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
            node = RemoteFile(full, entry.size, entry.mtime, entry.is_dir)
            if entry.is_dir:
                queue.append(full)
                dirs.append(node)
            else:
                files.append(node)
    return files, dirs


# ===================== find：依名稱/大小/時間過濾 =====================


@dataclass(frozen=True)
class FindSpec:
    """Filters for `ncfetch find`. Empty/None fields are no-ops."""
    name_globs: Tuple[str, ...] = ()
    regex: Optional[re.Pattern[str]] = None
    kind: str = "any"                      # "any" | "f" | "d"
    min_size: Optional[int] = None
    max_size: Optional[int] = None
    newer_than: Optional[datetime] = None
    older_than: Optional[datetime] = None
    ignore_case: bool = False


def entry_matches(node: RemoteFile, spec: FindSpec) -> bool:
    if spec.kind == "f" and node.is_dir:
        return False
    if spec.kind == "d" and not node.is_dir:
        return False
    if spec.name_globs and not match_globs(
        node.path, spec.name_globs, ignore_case=spec.ignore_case
    ):
        return False
    if spec.regex is not None and not spec.regex.search(node.path):
        return False
    if not node.is_dir:
        if spec.min_size is not None and (node.size or 0) < spec.min_size:
            return False
        if spec.max_size is not None and (node.size or 0) > spec.max_size:
            return False
    elif spec.min_size is not None or spec.max_size is not None:
        return False  # 目錄沒有大小，套用大小條件時一律排除
    if spec.newer_than is not None and (node.mtime is None or node.mtime <= spec.newer_than):
        return False
    if spec.older_than is not None and (node.mtime is None or node.mtime >= spec.older_than):
        return False
    return True


def filter_entries(nodes: Sequence[RemoteFile], spec: FindSpec) -> List[RemoteFile]:
    return [n for n in nodes if entry_matches(n, spec)]


async def find_entries(
    *,
    client: httpx.AsyncClient,
    url_builder: Callable[[str], str],
    remote_folder: str,
    spec: FindSpec,
) -> List[RemoteFile]:
    """Walk the tree and return every node passing `spec`, sorted by path."""
    files, dirs = await walk_propfind(
        client=client, url_builder=url_builder, base_dir=remote_folder.strip("/")
    )
    hits = filter_entries(list(files) + list(dirs), spec)
    hits.sort(key=lambda n: (not n.is_dir, n.path.lower()))
    return hits


# ===================== grep：串流下載 + 逐行比對 =====================


@dataclass(frozen=True)
class GrepSpec:
    pattern: re.Pattern[str]
    include: Tuple[str, ...] = ()
    exclude: Tuple[str, ...] = ()
    max_bytes: int = 5 * 1024 * 1024      # 0 = 不限制
    before: int = 0
    after: int = 0
    max_count: int = 0                    # 每檔最多幾筆；0 = 不限
    skip_binary: bool = True
    invert: bool = False
    encoding: str = "utf-8"


@dataclass
class GrepStats:
    scanned: int = 0
    matched_files: int = 0
    matches: int = 0
    skipped_filter: int = 0
    skipped_size: int = 0
    skipped_binary: int = 0
    errors: int = 0


async def _grep_one(
    client: httpx.AsyncClient,
    url: str,
    rel_path: str,
    spec: GrepSpec,
) -> Tuple[List[GrepMatch], str]:
    """Stream one remote file and scan it. Returns (matches, status).

    status is one of "ok" / "binary" / "too-big"; too-big only happens for files
    whose size the server did not advertise, since known sizes are pre-filtered.
    """
    scanner = LineScanner(
        rel_path, spec.pattern,
        before=spec.before, after=spec.after,
        max_count=spec.max_count, invert=spec.invert,
    )
    splitter = ByteLineSplitter(spec.encoding)
    read = 0
    first = True

    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes(chunk_size=CHUNK):
            if first:
                first = False
                if spec.skip_binary and looks_binary(chunk):
                    return [], "binary"
            read += len(chunk)
            if spec.max_bytes and read > spec.max_bytes:
                return [], "too-big"
            for line in splitter.feed(chunk):
                scanner.feed(line)
            if scanner.exhausted:
                return scanner.finish(), "ok"

    for line in splitter.flush():
        scanner.feed(line)
    return scanner.finish(), "ok"


async def grep_tree(
    *,
    client: httpx.AsyncClient,
    url_builder: Callable[[str], str],
    remote_folder: str,
    spec: GrepSpec,
    workers: int = 8,
    on_file_result: Optional[Callable[[str, List[GrepMatch]], None]] = None,
    progress: bool = True,
) -> GrepStats:
    """PROPFIND walk → parallel streamed downloads → per-line regex scan.

    Files are filtered by include/exclude globs and by the size advertised in
    PROPFIND *before* anything is transferred, so a `--include '*.md'` search
    over a big tree only downloads the markdown.

    `on_file_result` is invoked once per file that has matches, in completion
    order (not path order) — matching how parallel greps behave. It is called
    from the event loop under a lock, so it may print directly.
    """
    files, _dirs = await walk_propfind(
        client=client, url_builder=url_builder, base_dir=remote_folder.strip("/")
    )
    stats = GrepStats()

    candidates: List[RemoteFile] = []
    for node in files:
        if not path_allowed(node.path, spec.include, spec.exclude):
            stats.skipped_filter += 1
            continue
        if spec.max_bytes and node.size is not None and node.size > spec.max_bytes:
            stats.skipped_size += 1
            continue
        candidates.append(node)

    sem = asyncio.Semaphore(max(1, workers))
    emit_lock = asyncio.Lock()
    bar = tqdm(
        total=len(candidates), unit="file", desc="Grep", dynamic_ncols=True,
        disable=not progress, leave=False,
    )

    async def one(node: RemoteFile) -> None:
        url = url_builder(node.path)
        try:
            async with sem:
                matches, status = await _grep_one(client, url, node.path, spec)
        except Exception as e:  # 單檔失敗不該中斷整趟搜尋
            stats.errors += 1
            logger.warning("讀取失敗，略過 %s：%s", node.path, e)
            bar.update(1)
            return

        if status == "binary":
            stats.skipped_binary += 1
        elif status == "too-big":
            stats.skipped_size += 1
        else:
            stats.scanned += 1
            if matches:
                stats.matched_files += 1
                stats.matches += len(matches)
                if on_file_result is not None:
                    async with emit_lock:
                        on_file_result(node.path, matches)
        bar.update(1)

    try:
        await asyncio.gather(*(one(n) for n in candidates))
    finally:
        bar.close()
    return stats
