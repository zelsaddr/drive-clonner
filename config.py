import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "browser_profile")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".ogv",
}

DEFAULT_CONFIG = {
    "stream_connections": 8,
    "parallel_files": 4,
    "skip_existing": True,
    "download_dir": DOWNLOAD_DIR,
    "ffmpeg_path": "ffmpeg",
    "headless": False,
    "launch_mode": "cdp",
    "cdp_port": 9222,
    "name_classes": ["Q5txwe", "s2Wjec", "KL4NAf"],
    "item_selector": "[data-id]",
}

COUNTRIES = None


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)