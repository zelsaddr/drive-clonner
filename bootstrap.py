import importlib
import shutil
import subprocess
import sys

PACKAGES = [
    ("playwright", "playwright"),
    ("requests", "requests"),
    ("webview", "pywebview"),
]


def ensure_packages():
    missing = []
    for imp_name, pip_name in PACKAGES:
        try:
            importlib.import_module(imp_name)
        except Exception:
            missing.append(pip_name)

    if missing:
        print("[bootstrap] Dependency belum ada: %s" % ", ".join(missing))
        print("[bootstrap] Menginstal via pip...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade"] + missing)
        except Exception as exc:
            print("[bootstrap] Gagal install otomatis: %s" % exc)
            print("[bootstrap] Install manual:  %s -m pip install %s" % (sys.executable, " ".join(missing)))
            raise SystemExit(1)

    for imp_name, _pip_name in PACKAGES:
        importlib.import_module(imp_name)


def ensure_browser_runtime():
    """Auto-install Playwright Chromium if neither Chrome nor a cached Chromium exists."""
    from browser import _find_chrome
    if _find_chrome():
        return
    try:
        from playwright.sync_api import sync_playwright
        p = sync_playwright().start()
        try:
            p.chromium.executable_path
            return
        finally:
            p.stop()
    except Exception:
        pass
    print("[bootstrap] Mengunduh Chromium Playwright (sekali saja)...")
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])


def check_tools():
    problems = []
    if not (shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")):
        problems.append("ffmpeg tidak ditemukan (hanya perlu untuk kualitas 1080p; 360p/720p tetap jalan)")
    return problems


def main():
    print("[bootstrap] Memeriksa dependency...")
    ensure_packages()
    print("[bootstrap] Dependency Python siap.")
    ensure_browser_runtime()
    problems = check_tools()
    for p in problems:
        print("[bootstrap] PERINGATAN: %s" % p)

    import app
    app.main()


if __name__ == "__main__":
    main()