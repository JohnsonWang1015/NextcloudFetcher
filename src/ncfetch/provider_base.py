from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List

from .webdav_dav import DAVEntry


class StorageProvider(ABC):
    @abstractmethod
    async def download_file(self, remote_path: str, local_path: Path) -> None:
        ...

    @abstractmethod
    async def download_folder_zip(self, remote_folder: str, local_zip_path: Path) -> None:
        ...

    @abstractmethod
    async def list_folder(self, remote_folder: str) -> List[DAVEntry]:
        ...

    @abstractmethod
    async def download_folder_tree(
        self, remote_folder: str, local_dir: Path, workers: int = 8
    ) -> None:
        ...
