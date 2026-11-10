import importlib
import shutil
import subprocess
import sys

PACKAGES = [
    ("customtkinter", "customtkinter"),
    ("playwright", "playwright"),
    ("requests", "requests"),
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


def check_tools():
    problems = []
    if not (shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")):
        problems.append("ffmpeg tidak ditemukan (hanya perlu untuk kualitas 1080p; 360p/720p tetap jalan)")
    from browser import _find_chrome
    if not _find_chrome():
        problems.append("Google Chrome tidak ditemukan (wajib untuk akses Google Drive)")
    return problems


def main():
    print("[bootstrap] Memeriksa dependency...")
    ensure_packages()
    problems = check_tools()
    print("[bootstrap] Dependency Python siap.")
    for p in problems:
        print("[bootstrap] PERINGATAN: %s" % p)

    import app
    app.main()


if __name__ == "__main__":
    main()