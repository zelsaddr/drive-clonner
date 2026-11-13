# Gdrive Clonner

Desktop GUI tool to clone files and folders from Google Drive **"Shared with me"** to your local disk.

It lists shared items in a tree view and downloads them in parallel. Ordinary files are fetched through the standard Drive download endpoint, while videos are downloaded from their stream (`videoplayback`) endpoints (with optional DASH muxing via ffmpeg).

## Features

- List everything in "Shared with me" with lazy folder browsing.
- Parallel downloads with per-file and overall progress bars.
- Video download from stream endpoints (360p / 720p / 1080p).
- Adaptive (DASH) video + audio muxing using ffmpeg.
- Resume-friendly: skips files that already exist.
- A `.cloner_manifest.json` record of what has been downloaded.
- Login opens a visible (headful) window and switches to headless automatically once logged in.
- Stream auth is captured from the browser and refreshed during long runs (avoids 401 partway).
- Chunk downloads retry transient network errors (read timeout / broken connection) automatically.

## Extensions downloaded by stream

Files whose extension is in `VIDEO_EXTENSIONS` (`config.py`) are treated as videos and downloaded from their stream (`videoplayback`) endpoint instead of the standard download endpoint:

`.mp4` `.m4v` `.mov` `.mkv` `.webm` `.avi` `.wmv` `.flv` `.mpg` `.mpeg` `.ts` `.m2ts` `.3gp` `.ogv`

Any other extension is fetched through the ordinary Drive download endpoint. Stream-downloaded videos are always saved with an `.mp4` extension (360p/720p progressive, or DASH video + audio muxed with ffmpeg for 1080p).

## Requirements

- Python 3.8+
- Google Chrome **atau** Chromium Playwright (otomatis diunduh jika Chrome tidak ada)
- Microsoft Edge WebView2 Runtime (biasanya sudah ada di Windows 10/11; dipakai untuk UI webview)
- ffmpeg (in `PATH`, or set `ffmpeg_path` in `config.json`) — opsional, hanya untuk 1080p
- Internet (untuk akses Google Drive; unduhan Chromium pertama kali juga butuh internet)

## Installation

```bash
pip install -r requirements.txt
```

Browser diunduh otomatis saat pertama kali dibutuhkan: jika Google Chrome terpasang di lokasi standar, Chrome dipakai; jika tidak, aplikasi memakai Chromium Playwright dan mengunduhnya sendiri via `playwright install chromium`. Untuk memaksa Chromium Playwright sejak awal:

```bash
playwright install chromium
```

If you use the `stealth` launch mode, also install the stealth plugin:

```bash
pip install playwright-stealth
```

## Configuration

Settings live in `config.json`. Defaults are defined in `config.py`.

| Key | Description |
| --- | --- |
| `stream_connections` | Number of chunk connections per video stream |
| `parallel_files` | Files downloaded at the same time |
| `skip_existing` | Skip files that already exist |
| `download_dir` | Destination folder |
| `ffmpeg_path` | Path to the ffmpeg binary |
| `headless` | Run the browser without a window |
| `launch_mode` | `cdp` (Chrome DevTools Protocol) or `stealth` |
| `cdp_port` | Port used for CDP launch |
| `name_classes` | CSS classes used to read file names from the Drive DOM |
| `item_selector` | Selector that matches each Drive item |

## Usage

```bash
python app.py
```

Workflow:

1. **Buka Browser & Login** — opens Chrome, log in to your Google account.
2. **Muat List Shared** — loads the "Shared with me" list.
3. Select items and folders in the tree (expand folders to load their contents).
4. **Mulai Download** — downloads the selected items.

UI memakai **pywebview + HTML/CSS/JS** (self-contained dark theme, dirender di WebView2) — tidak ada runtime Tailwind/CDN. Backend Python tetap sama. Kalau ingin menjalankan UI webview langsung:

```bash
python webui.py
```

## Project structure

| File | Purpose |
| --- | --- |
| `webui.py` | pywebview backend (`DriveApi`) + download orchestration |
| `static/index.html` | Frontend HTML/CSS/JS (toolbar, tree, progress, log) — self-contained, no CDN |
| `app.py` | Entry point — memanggil `webui.main()` |
| `browser.py` | Playwright browser control (CDP / stealth launch) |
| `lister.py` | Scrapes shared items from the Drive UI |
| `downloader.py` | Standard (non-video) file downloader |
| `video_download.py` | Video stream discovery and DASH download |
| `stream_download.py` | Standalone ffmpeg mux bridge for stream URLs |
| `config.py` | Config loading/defaults and shared constants |