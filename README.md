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

## Requirements

- Python 3.8+
- Google Chrome
- ffmpeg (in `PATH`, or set `ffmpeg_path` in `config.json`)

## Installation

```bash
pip install -r requirements.txt
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

## Project structure

| File | Purpose |
| --- | --- |
| `app.py` | CustomTkinter GUI and download orchestration |
| `browser.py` | Playwright browser control (CDP / stealth launch) |
| `lister.py` | Scrapes shared items from the Drive UI |
| `downloader.py` | Standard (non-video) file downloader |
| `video_download.py` | Video stream discovery and DASH download |
| `stream_download.py` | Standalone ffmpeg mux bridge for stream URLs |
| `config.py` | Config loading/defaults and shared constants |