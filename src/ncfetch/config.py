from __future__ import annotations
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    base_url: str = os.getenv("NEXTCLOUD_BASE_URL", "").rstrip("/")
    username: str = os.getenv("NEXTCLOUD_USERNAME", "")
    password: str = os.getenv("NEXTCLOUD_PASSWORD", "")
    webdav_root: str | None = os.getenv("NEXTCLOUD_WEBDAV_ROOT") or None
    verify_ssl: str = os.getenv("NEXTCLOUD_VERIFY_SSL", "true")
    request_timeout: float = float(os.getenv("REQUEST_TIMEOUT", "60"))

    def webdav_base(self) -> str:
        # /remote.php/dav/files/<username>
        root = self.webdav_root
        if not root or root.strip() == "":
            root = f"/remote.php/dav/files/{self.username}"
        return f"{self.base_url}{root}".rstrip("/")

    def verify_arg(self):
        v = str(self.verify_ssl).lower().strip()
        if v in ("false", "0", "no", "off"):
            return False
        if v not in ("true", "1", "yes", "on"):
            return v
        return True

def get_settings() -> Settings:
    s = Settings()
    if not s.base_url or not s.username or not s.password:
        raise RuntimeError("環境變數未設定完整：請在 .env 設定 NEXTCLOUD_BASE_URL / NEXTCLOUD_USERNAME / NEXTCLOUD_PASSWORD")
    return s