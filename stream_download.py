import argparse
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.join(BASE_DIR, "downloads")
DEFAULT_PORT = 8666
FFMPEG = "ffmpeg"
REFERER = "https://drive.google.com/"


def sanitize(name):
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (name or "video"))
    name = name.strip(" .")
    return name or "video"


def unique_path(out_dir, name, ext=".mp4"):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name + ext)
    if not os.path.exists(path):
        return path
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(out_dir, name + "_" + stamp + ext)


def _headers():
    return "Referer: %s\r\nUser-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36\r\n" % REFERER


def mux(video_url, audio_url, title, out_dir):
    out = unique_path(out_dir, sanitize(title), ".mp4")
    inputs = []
    if video_url:
        inputs += ["-headers", _headers(), "-i", video_url]
    if audio_url:
        inputs += ["-headers", _headers(), "-i", audio_url]
    if not inputs:
        return False, None, "tidak ada URL"

    base = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"] + inputs

    cmd = base + ["-c", "copy", "-movflags", "+faststart", out]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return True, out, ""

    cmd2 = base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                   "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    proc2 = subprocess.run(cmd2, capture_output=True, text=True)
    if proc2.returncode == 0:
        return True, out, ""
    return False, None, (proc2.stderr or proc.stderr or "")[-800:]


class Handler(BaseHTTPRequestHandler):
    server_version = "GDriveStreamBridge/1.0"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.rstrip("/") in ("/ping", "/health"):
            self._json(200, {"ok": True})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/stream":
            self._json(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            data = json.loads(raw or "{}")
        except Exception:
            self._json(400, {"ok": False, "error": "bad json"})
            return
        video = data.get("video_url")
        audio = data.get("audio_url")
        title = data.get("title", "video")
        out_dir = self.server.out_dir
        if not video and not audio:
            self._json(400, {"ok": False, "error": "video_url/audio_url kosong"})
            return
        self._json(200, {"ok": True, "status": "muxing", "title": title})
        t = threading.Thread(target=self._run, args=(video, audio, title, out_dir), daemon=True)
        t.start()

    def _run(self, video, audio, title, out_dir):
        ok, path, err = mux(video, audio, title, out_dir)
        if ok:
            print("[bridge] OK -> %s" % path, flush=True)
        else:
            print("[bridge] GAGAL: %s" % err, flush=True)

    def log_message(self, *args):
        pass


def serve(port, out_dir):
    httpd = HTTPServer(("127.0.0.1", port), Handler)
    httpd.out_dir = out_dir
    print("[bridge] siap di http://127.0.0.1:%d  (out: %s)" % (port, out_dir), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description="Download & mux stream GDrive DASH via ffmpeg")
    ap.add_argument("--video", help="URL stream video (videoplayback itag video)")
    ap.add_argument("--audio", help="URL stream audio (videoplayback itag audio)")
    ap.add_argument("--title", default="video", help="Nama file output")
    ap.add_argument("--outdir", default=DEFAULT_OUT_DIR, help="Folder output")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port bridge HTTP")
    ap.add_argument("--bridge", action="store_true", help="Jalankan sebagai HTTP bridge (mode default tanpa --video)")
    args = ap.parse_args()

    outdir = os.path.abspath(args.outdir or DEFAULT_OUT_DIR)
    os.makedirs(outdir, exist_ok=True)

    if args.video or args.audio:
        ok, path, err = mux(args.video, args.audio, args.title, outdir)
        if ok:
            print("OK -> %s" % path)
        else:
            print("GAGAL: %s" % err)
            raise SystemExit(1)
        return

    serve(args.port, outdir)


if __name__ == "__main__":
    main()