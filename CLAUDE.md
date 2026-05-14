# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`nextcloudfetch` is a small Python CLI (`ncfetch`) that downloads files and folders from a Nextcloud server over **WebDAV**. It supports both authenticated user accounts and anonymous/password-protected **public shares**. Folder downloads always come back as a ZIP, optionally auto-extracted.

Package layout: `src/ncfetch/` (hatchling build, package = `ncfetch`, distribution name = `nextcloudfetch`).

## Common commands

Environment setup uses **`uv`** (not pip/poetry). Dependencies are pinned in `uv.lock`.

```bash
uv sync                      # install/refresh deps into .venv
cp .env.example .env         # then edit with NEXTCLOUD_BASE_URL/USERNAME/PASSWORD
uv run ncfetch --help        # entry point (defined in pyproject [project.scripts])
```

Tests live under `tests/` and use **pytest**. Dev deps are declared in `[dependency-groups].dev` (PEP 735); `uv run pytest` picks them up automatically. No linter or CI is configured.

```bash
uv run pytest                          # all tests
uv run pytest tests/test_webdav_dav.py # one file
uv run pytest -k humanize              # by name
```

The current test suite covers **pure functions only** — XML parsing (`parse_propfind`), token extraction, ZIP-response sniffing, humanize formatting, password resolution, base-prefix stripping, and Zip Slip rejection in `unzip()`. HTTP-mocked integration tests for the three-tier fallback are not yet wired up — add `respx` to dev deps and mock `httpx` if you go there.

### Running the CLI subcommands

Authenticated user (`.env` credentials):

```bash
uv run ncfetch file    "Projects/report.pdf" -o ./downloads
uv run ncfetch folder  "Datasets/2025" --zip ./downloads/2025.zip --unzip-to ./downloads/2025
uv run ncfetch ls      "Datasets"                                  # list a remote dir
uv run ncfetch mirror  "Datasets/2025" -o ./downloads/2025 -w 16   # tree copy, N workers
```

Public share (`<token>` or full `https://host/s/<token>` URL):

```bash
uv run ncfetch public-file    "<token-or-url>" "<file>"           -o ./downloads [-p <pwd>]
uv run ncfetch public-folder  "<token-or-url>" "<folder>"  --zip ./out.zip [--unzip-to ./out] [-p <pwd>] [-w 8]
uv run ncfetch public-ls      "<token-or-url>" "<folder>"  [-p <pwd>]
uv run ncfetch public-mirror  "<token-or-url>" "<folder>"  -o ./out [-w 16] [-p <pwd>]
```

Public-share password resolution order (see `cli._public_pwd`): `--password` flag → `NEXTCLOUD_PUBLIC_PASSWORD` env var → empty string.

## Architecture

### Provider abstraction

`provider_base.StorageProvider` is an ABC with four async methods: `download_file`, `download_folder_zip`, `list_folder`, `download_folder_tree`. Two concrete providers implement it:

- **`providers/webdav_provider.WebDAVProvider`** — authenticated user. Files: `GET /remote.php/dav/files/<user>/<path>`. Folders: same URL with `Accept: application/zip` (Nextcloud responds with a server-built ZIP).
- **`providers/public_share_provider.PublicShareProvider`** — public share. Auth is `(token, share_password_or_empty)` via HTTP Basic. Base WebDAV path is `<base_url>/public.php/webdav`.

Shared WebDAV plumbing lives in **`webdav_dav.py`** (`propfind()`, `parse_propfind()`, `DAVEntry`) and the **`download_tree()`** free function in `webdav_provider.py` — both providers reuse the same walker by passing their own `_build_url` / `_build_webdav_url` as a callback. Keep WebDAV-protocol bits there, not in the providers themselves.

If you add a new transport (e.g., OCS Share API, S3-backed mirror), make it another `StorageProvider` and wire a new Typer command in `cli.py` — do **not** push transport-specific logic into the CLI module.

### Public-share folder download: three-tier fallback (important)

`PublicShareProvider.download_folder_zip` tries three strategies in order, catching exceptions and falling through:

1. **Web endpoint** — `GET /s/<token>/download?path=/<folder>` (closest to browser behavior). Validates that the response is actually a ZIP via `_is_zip_response`; if Nextcloud returns the WebDAV HTML page instead, this tier "fails" and the next is tried.
2. **Public WebDAV ZIP** — `GET public.php/webdav/<folder>` with `Accept: application/zip`. Only some Nextcloud versions/configs support this; same ZIP-vs-HTML sniff.
3. **Recursive PROPFIND + parallel client-side ZIP** — walks the share with `PROPFIND` (depth=1) per directory (sequential), enumerates every file, then downloads **N at a time** (`--workers`, default 8) into per-file `tempfile.NamedTemporaryFile`s. A single `asyncio.Lock` serializes `ZipFile.write(tmp_path, arcname=...)` since `zipfile.ZipFile` is **not thread-safe** — the lock is mandatory, do not remove it.

Each tier's failures are logged at `WARNING` and the half-written ZIP is `unlink`'d before falling through, so a partial tier-1 ZIP can't be mistaken for a successful tier-3 output. Preserve this chain when modifying — tiers 1 and 2 are fast/cheap but unreliable across versions; tier 3 is the always-works fallback. For password-protected shares, `_login_public_share` performs the `POST /s/<token>` cookie dance before tier 1 since the web endpoint requires session auth, not just Basic.

### Config

`config.Settings` is a frozen dataclass populated from `.env` via `python-dotenv`. `webdav_base()` derives `<base_url>/remote.php/dav/files/<username>` when `NEXTCLOUD_WEBDAV_ROOT` is unset. `verify_arg()` interprets `NEXTCLOUD_VERIFY_SSL` as bool-ish, but also passes through a CA-path string when given one — httpx accepts both forms.

`get_settings()` raises if `BASE_URL`/`USERNAME`/`PASSWORD` are missing. Public-share commands still need a populated `.env` because they reuse `Settings.base_url` and the TLS/timeout settings — they just don't use the user credentials.

### Async pattern

All providers expose `async` methods; the CLI wraps each command body in `asyncio.run(run())`. Streaming downloads use `httpx.AsyncClient.stream` with 1 MiB chunks, and file I/O is offloaded to threads via `asyncio.to_thread` so the event loop isn't blocked on `write()`.

## Knowledge graph

This repo has a graphify knowledge graph at `graphify-out/` (per the user's global CLAUDE.md). When answering architecture/codebase questions, prefer `graphify-out/GRAPH_REPORT.md` and `graphify-out/wiki/index.md` over re-reading raw files. After code edits, refresh with:

```bash
python3 -c "from graphify.watch import _rebuild_code; from pathlib import Path; _rebuild_code(Path('.'))"
```

## Gotchas

- The tier-3 `asyncio.Lock` around `zf.write(...)` is load-bearing — `zipfile.ZipFile` is not thread/coroutine-safe. Removing the lock will silently corrupt the central directory under any parallelism.
- `unzip()` in `utils.py` does a **pre-flight Zip Slip check** on every member before extracting anything, and raises `ZipSlipError` if any path would land outside `out_dir.resolve()`. The check is intentionally atomic (all-or-nothing) so a malicious archive cannot partially extract before being rejected — don't "optimize" by interleaving check + extract.
- PROPFIND walking is sequential by design (it's cheap; the GET parallelism is where the win is). Don't try to parallelize PROPFIND without measuring — most folder trees are wide-but-shallow and PROPFIND isn't the bottleneck.
- The package name (`nextcloudfetch`) and the import name (`ncfetch`) differ. PyPI/wheel = `nextcloudfetch`; `import ncfetch`; CLI = `ncfetch`.
