## Nextcloud Fetcher

使用 Python，透過 Nextcloud **WebDAV** 與**公開分享 (public share)** 介面下載檔案或整個資料夾 (ZIP)，支援自動解壓縮與密碼分享。

- Python 3.10+
- 安裝後提供 `ncfetch` CLI（基於 [Typer](https://typer.tiangolo.com/)）

---

### 安裝

#### A. 作為套件安裝（推薦給使用者）

直接從 GitHub 安裝指定 tag（private repo 也適用，須有對應憑證）：

```bash
# uv
uv pip install "git+https://github.com/JohnsonWang1015/NextcloudFetcher.git@v0.1.1"

# 加進另一個 uv 專案的相依
uv add "git+https://github.com/JohnsonWang1015/NextcloudFetcher.git@v0.1.1"

# 純 pip
pip install "git+https://github.com/JohnsonWang1015/NextcloudFetcher.git@v0.1.1"

# 隔離安裝為全域 CLI（類似 pipx）
uv tool install "git+https://github.com/JohnsonWang1015/NextcloudFetcher.git@v0.1.1"
```

> Private repo 的話請改用 SSH (`git+ssh://git@github.com/...`) 或在 URL 帶入 PAT。

#### B. 從原始碼開發（給貢獻者）

```bash
git clone https://github.com/JohnsonWang1015/NextcloudFetcher.git
cd NextcloudFetcher
uv sync
cp .env.example .env
vim .env   # 編輯填入 Nextcloud 帳號密碼與網址
```

開發模式下用 `uv run ncfetch ...` 執行；套件模式下直接 `ncfetch ...`。本文後續範例使用裸 `ncfetch`，兩種模式皆通。

---

### 設定 `.env`

在你**執行 `ncfetch` 的工作目錄**（或其任一父層）放一個 `.env`：

```env
NEXTCLOUD_BASE_URL=https://your-cloud.example.com
NEXTCLOUD_USERNAME=alice
NEXTCLOUD_PASSWORD=app_password_or_token

# 選填 — 公開分享預設密碼（可改用 CLI -p 覆寫）
NEXTCLOUD_PUBLIC_PASSWORD=

# 選填 — 自訂 WebDAV 根路徑，預設 /remote.php/dav/files/<username>
NEXTCLOUD_WEBDAV_ROOT=

# 選填 — 是否驗證 TLS 憑證（true/false 或自訂 CA bundle 路徑）
NEXTCLOUD_VERIFY_SSL=true

# 選填 — HTTP 請求 timeout (秒)
REQUEST_TIMEOUT=60
```

`ncfetch` 會從 CWD 往上搜尋 `.env`，找到第一個就載入；找不到時你也可以改用 shell 環境變數（`export` 或 `NEXTCLOUD_BASE_URL=... ncfetch ...`）。

> **安全建議**：`NEXTCLOUD_PASSWORD` 不要填網頁登入密碼。到 Nextcloud → 設定 → 安全性 → **建立 App password**，這樣外洩時可單獨撤銷而不影響主帳號。也請把 `.env` 加入 `.gitignore`。

---

### 使用

下載單一檔案：
```bash
ncfetch file "Projects/report.pdf" -o ./downloads
```

下載整個資料夾（Nextcloud 會回 ZIP，可選自動解壓）：
```bash
ncfetch folder "Datasets/2025" --zip ./downloads/2025.zip --unzip-to ./downloads/2025
```

無密碼公開分享 — 下載單一檔案（token 或完整 URL 都可）：
```bash
ncfetch public-file "https://<host>/s/<token>" "<file_name>" -o ./downloads
# 或
ncfetch public-file "<token>" "<file_name>" -o ./downloads
```

無密碼公開分享 — 下載資料夾（含自動解壓）：
```bash
ncfetch public-folder "<token>" "<folder>" \
  --zip ./downloads/<zip_name>.zip \
  --unzip-to ./downloads/<zip_name>
```

有密碼公開分享 — 用 CLI 帶密碼：
```bash
ncfetch public-file "<token>" "<file_name>" -o ./downloads -p "<share_password>"
```

有密碼公開分享 — 用環境變數帶密碼：
```bash
# Linux / macOS
export NEXTCLOUD_PUBLIC_PASSWORD=YourSharePassword
# Windows (PowerShell)
$env:NEXTCLOUD_PUBLIC_PASSWORD = "YourSharePassword"

ncfetch public-folder "https://<host>/s/<token>" "<folder>" --zip ./downloads/<zip_name>.zip
```

完整指令列表：`ncfetch --help`、各子指令 `ncfetch <command> --help`。

---

### 升級

```bash
uv pip install --upgrade "git+https://github.com/JohnsonWang1015/NextcloudFetcher.git@v0.1.1"
```

或把上面 `@v0.1.1` 換成最新 tag。可用版本見 [Releases / tags](https://github.com/JohnsonWang1015/NextcloudFetcher/tags)。
