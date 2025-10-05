## Nextcloud Fetcher

使用 Python + uv，透過 Nextcloud **WebDAV** 下載檔案或整個資料夾 (ZIP)。

### 安裝

```bash
uv sync
cp .env.example .env
vim .env  # 編輯 .env 檔案，填入你的 Nextcloud 帳號密碼與網址
```

### 使用

- 下載單一檔案
```bash
uv run ncfetch file "Projects/report.pdf" -o ./downloads 
```
- 下載整個資料夾 (會自動打包成 ZIP)
```bash
uv run ncfetch folder "Datasets/2025" --zip ./downloads/2025.zip --unzip-to ./downloads/2025
```
- 無密碼公開分享 - 下載檔案
```bash
uv run ncfetch public-file "https://<host>:<port>/s/<token>" "<file_name>" -o ./downloads
```
- 無密碼公開分享 - 下載資料夾 (會自動打包成 ZIP，並解壓縮)
```bash
uv run ncfetch public-folder "<token>" "<folder>" --zip ./downloads/<zip_name>.zip --unzip-to ./downloads/<zip_name>
```
- 有密碼公開分享 - 下載檔案 (CLI 帶密碼)
```bash
uv run ncfetch public-file "<token>" "<file_name>" -o ./downloads -p "<your_shared_password>"
```
- 有密碼公開分享 - 下載資料夾 (使用環境變數帶密碼)
```bash
export NEXTCLOUD_PUBLIC_PASSWORD=YourSharePassword # Linux / macOS
set NEXTCLOUD_PUBLIC_PASSWORD=YourSharePassword  # Windows

uv run ncfetch public-folder "https://<host>:<port>/s/<token>" "<folder>" --zip ./downloads/<zip_name>.zip
```