from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path

class StorageProvider(ABC):
    @abstractmethod
    async def download_file(self, remote_path: str, local_path: Path) -> None:
        ...

    @abstractmethod
    async def download_folder_zip(self, remote_folder: str, local_zip_path: Path) -> None:
        ...