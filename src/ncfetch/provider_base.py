from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path
from typing import AsyncIterator, Callable, List, Optional, Tuple

from .search import FindSpec, GrepMatch, GrepSpec, GrepStats, RemoteFile
from .webdav_dav import DAVEntry


class StorageProvider(ABC):
    @abstractmethod
    async def download_file(self, remote_path: str, local_path: Path) -> None:
        ...

    @abstractmethod
    async def download_folder_zip(
        self, remote_folder: str, local_zip_path: Path, workers: int = 8
    ) -> None:
        ...

    @abstractmethod
    async def list_folder(self, remote_folder: str) -> List[DAVEntry]:
        ...

    @abstractmethod
    async def download_folder_tree(
        self, remote_folder: str, local_dir: Path, workers: int = 8
    ) -> None:
        ...

    @abstractmethod
    def stream_file(self, remote_path: str) -> AsyncIterator[bytes]:
        """Async-generator of the file's bytes (no local file involved)."""
        ...

    @abstractmethod
    async def walk(self, remote_folder: str = "") -> Tuple[List[RemoteFile], List[RemoteFile]]:
        """Recursive PROPFIND walk; returns (files, dirs)."""
        ...

    @abstractmethod
    async def find(self, remote_folder: str, spec: FindSpec) -> List[RemoteFile]:
        """Filter the remote tree by name/size/mtime."""
        ...

    @abstractmethod
    async def grep(
        self,
        remote_folder: str,
        spec: GrepSpec,
        *,
        workers: int = 8,
        on_file_result: Optional[Callable[[str, List[GrepMatch]], None]] = None,
        progress: bool = True,
    ) -> GrepStats:
        """Full-text search across the remote tree."""
        ...
