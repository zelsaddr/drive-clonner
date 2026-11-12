import json
import os
import queue
import threading
import time
import traceback

import webview

import config as cfg_mod
from browser import DriveBrowser
from lister import list_shared_items, scroll_shared_list, scrape_items
from downloader import build_session, download, sanitize_filename, is_video_name
from video_download import download_video, discover


def _render_html():
    path = os.path.join(cfg_mod.BASE_DIR, "static", "index.html")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class DriveApi:
    """Backend object exposed to JS as `pywebview.api`."""

    def __init__(self):
        self._window = None
        self._cfg = cfg_mod.load_config()
        self._browser = None
        self._items = []
        self._headless = bool(self._cfg.get("headless", False))
        self._cfg_lock = threading.Lock()

        self._cmd_queue = queue.Queue()
        self._worker_stop = False
        self._abort = threading.Event()

        try:
            import video_download as vd
            vd.set_notify(lambda m: self._emit("log", m))
        except Exception:
            pass

        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    # ---------------------------------------------------------------- API (JS -> Python)
    def get_state(self):
        return {
            "headless": self._headless,
            "recursive": bool(self._cfg.get("recursive", True)),
            "quality": self._cfg.get("quality", "720p"),
            "parallel": int(self._cfg.get("parallel_files", 4)),
            "skip_existing": bool(self._cfg.get("skip_existing", True)),
            "download_dir": self._cfg.get("download_dir") or cfg_mod.DOWNLOAD_DIR,
        }

    def login(self):
        self._cmd_queue.put(("start", None))

    def list_shared(self):
        self._cmd_queue.put(("list", None))

    def select_folder(self, folder_id):
        if not folder_id:
            return
        self._cmd_queue.put(("list_folder", folder_id))

    def preview(self, item):
        item = item or {}
        if not item.get("id"):
            self._emit("status", "Tidak ada item untuk di-preview.")
            return
        self._cmd_queue.put(("preview", item))

    def start_clone(self, request):
        request = request or {}
        items = request.get("items") or []
        if not isinstance(items, list):
            items = []
        try:
            parallel = int(request.get("parallel", 4))
        except (TypeError, ValueError):
            parallel = 4
        self._cmd_queue.put(("clone", {
            "items": items,
            "recursive": bool(request.get("recursive", True)),
            "quality": request.get("quality", "720p"),
            "parallel": parallel,
            "skip_existing": bool(request.get("skip_existing", True)),
        }))

    def set_option(self, key, value):
        with self._cfg_lock:
            if key == "headless":
                self._headless = bool(value)
                self._cfg["headless"] = bool(value)
            elif key == "recursive":
                self._cfg["recursive"] = bool(value)
            elif key == "quality":
                self._cfg["quality"] = str(value)
            elif key == "parallel":
                try:
                    self._cfg["parallel_files"] = int(value)
                except (TypeError, ValueError):
                    return
            elif key == "skip_existing":
                self._cfg["skip_existing"] = bool(value)
            else:
                return
            cfg_mod.save_config(self._cfg)

    def abort(self):
        self._abort.set()
        import video_download as vd
        vd.ABORT.set()

    # ---------------------------------------------------------------- events (Python -> JS)
    def _emit(self, event, data=None):
        if self._window is None:
            return
        try:
            payload = json.dumps({"event": event, "data": data}, ensure_ascii=True, default=str)
            self._window.evaluate_js("window.__driveHandleEvent && window.__driveHandleEvent(%s)" % payload)
        except Exception:
            pass

    # ---------------------------------------------------------------- worker
    def _worker_loop(self):
        while True:
            try:
                cmd = self._cmd_queue.get(timeout=0.5)
            except queue.Empty:
                if self._worker_stop:
                    break
                continue
            if cmd[0] == "exit":
                break
            try:
                self._run(cmd)
            except Exception:
                self._emit("error", traceback.format_exc())

    def _ensure_browser(self, force_headful=False):
        want_headless = self._headless and not force_headful
        if self._browser is not None and self._browser.headless == want_headless:
            return
        if self._browser is not None:
            self._browser.close()
            self._browser = None
            self._wait_cdp_free()
        self._emit("status", "Membuka browser (%s)..." % ("headless" if want_headless else "tampil"))
        try:
            browser = DriveBrowser(self._cfg.get("profile_dir") or cfg_mod.PROFILE_DIR,
                                   want_headless,
                                   launch_mode=self._cfg.get("launch_mode", "cdp"),
                                   cdp_port=int(self._cfg.get("cdp_port", 9222)))
            browser.start()
        except Exception:
            # Don't leave a half-started browser behind; allow a clean retry.
            self._browser = None
            raise
        self._browser = browser

    def _relaunch_headless(self):
        if self._headless:
            return
        with self._cfg_lock:
            self._headless = True
            self._cfg["headless"] = True
            cfg_mod.save_config(self._cfg)
        self._emit("headless", True)
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        self._wait_cdp_free()

    def _wait_cdp_free(self, timeout=15.0):
        import requests
        port = int(self._cfg.get("cdp_port", 9222))
        endpoint = "http://127.0.0.1:%d" % port
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                requests.get(endpoint + "/json/version", timeout=1)
            except Exception:
                return
            time.sleep(0.3)

    def _auto_headless_after_login(self):
        if self._headless or self._browser is None:
            return
        try:
            logged_in = self._browser.is_logged_in()
        except Exception:
            return
        if logged_in:
            self._relaunch_headless()
            self._ensure_browser()

    def _wait_until_logged_in(self, timeout=900.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._worker_stop:
                return False
            try:
                url = self._browser.page.evaluate("location.href") or ""
            except Exception:
                time.sleep(2)
                continue
            if "accounts.google.com" not in url:
                return True
            time.sleep(2)
        return False

    def _run(self, cmd):
        name, payload = cmd
        if name == "start":
            self._ensure_browser(force_headful=True)
            self._browser.open_shared_with_me()
            url = self._browser.page.url or ""
            if "accounts.google.com" in url:
                self._emit("status", "Login dulu di browser (jendela tampil). Menunggu login selesai...")
                if not self._wait_until_logged_in():
                    self._emit("status", "Login belum terdeteksi. Setelah login, klik 'Muat List Shared'.")
                    return
            self._relaunch_headless()
            self._ensure_browser()
            self._browser.open_shared_with_me()
            self._emit("status", "Login berhasil. Browser dialihkan ke mode headless (login tersimpan).")
        elif name == "list":
            self._ensure_browser()
            self._auto_headless_after_login()
            self._emit("status", "Memuat shared with me...")
            items = list_shared_items(self._browser)
            self._items = items
            self._emit("items", items)
        elif name == "preview":
            self._ensure_browser()
            self._emit("status", "Membuka preview...")
            self._browser.open_file(payload["id"])
            self._browser.bring_to_front()
            self._emit("status", "Preview terbuka.")
        elif name == "list_folder":
            self._ensure_browser()
            try:
                children = self._list_folder(payload)
            except Exception as exc:
                self._emit("folder_error", {"id": payload, "error": str(exc)})
            else:
                self._emit("folder_children", {"id": payload, "children": children})
        elif name == "clone":
            self._clone(payload)
        elif name == "close_browser":
            if self._browser is not None:
                self._browser.close()
                self._browser = None

    # ---------------------------------------------------------------- download
    def _make_file_progress(self, file_id):
        state = {"t": 0.0}

        def cb(done, total):
            now = time.time()
            if now - state["t"] < 0.3 and done != total:
                return
            state["t"] = now
            self._emit("file_progress", {"id": file_id, "done": done, "total": total})
        return cb

    def _final_name(self, name):
        n = sanitize_filename(name)
        if is_video_name(name):
            if not n.lower().endswith(".mp4"):
                root, ext = os.path.splitext(n)
                n = (root if ext else n) + ".mp4"
        return n

    def _target(self, f):
        return os.path.join(f["dest"], self._final_name(f["name"]))

    def _manifest_path(self):
        base = self._cfg.get("download_dir") or cfg_mod.DOWNLOAD_DIR
        return os.path.join(base, ".cloner_manifest.json")

    def _load_manifest(self):
        try:
            with open(self._manifest_path(), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save_manifest(self, manifest):
        try:
            with open(self._manifest_path(), "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _clone(self, payload):
        if self._browser is None:
            self._emit("status", "Buka browser & login dulu, lalu muat list shared.")
            return

        self._abort.clear()
        import video_download as vd
        vd.ABORT.clear()

        items = payload.get("items") or []
        recursive = payload.get("recursive", True)
        quality = payload.get("quality", "720p")
        try:
            parallel = int(payload.get("parallel", 4))
        except (TypeError, ValueError):
            parallel = 4
        parallel = max(1, min(parallel, 16))
        skip_existing = bool(payload.get("skip_existing", True))
        dest = self._cfg.get("download_dir") or cfg_mod.DOWNLOAD_DIR
        os.makedirs(dest, exist_ok=True)
        cookies = self._browser.cookies()
        connections = int(self._cfg.get("stream_connections", 8))
        manifest = self._load_manifest()

        self._emit("status", "Memindai struktur folder...")
        files = []
        visited = set()
        self._flatten(items, dest, recursive, files, visited)

        total = len(files)
        self._emit("queue", {"total": total, "parallel": parallel})
        if total == 0:
            self._emit("status", "Tidak ada file untuk diunduh.")
            self._emit("finished", {"ok": 0, "skip": 0, "fail": 0, "total": 0})
            return

        video_file = next((f for f in files if is_video_name(f["name"])), None)
        if video_file:
            try:
                if not vd._state["endpoint"]:
                    self._emit("status", "Menemukan endpoint stream...")
                    vd.discover(self._browser, video_file["id"])
            except Exception as exc:
                self._emit("status", "Discover stream gagal: %s" % str(exc)[:120])

        nxt = [0]
        okc = [0]
        skipc = [0]
        failc = [0]
        lock = threading.Lock()

        # Auth cookies/token can go stale during a long run. Share them via a
        # mutex so the worker thread can refresh them while downloads run.
        cookies_lock = threading.Lock()
        cookies_holder = {"cookies": cookies}

        def get_cookies():
            with cookies_lock:
                return list(cookies_holder["cookies"])

        def worker():
            while True:
                if self._abort.is_set():
                    return
                with lock:
                    i = nxt[0]
                    nxt[0] += 1
                if i >= total:
                    return
                f = files[i]
                target = self._target(f)
                if skip_existing and os.path.exists(target) and os.path.getsize(target) > 0:
                    with lock:
                        skipc[0] += 1
                    self._emit("file_done", {"id": f["id"], "name": f["name"],
                                             "status": "skip", "reason": "sudah ada"})
                    continue
                cks = get_cookies()
                sess = build_session(cks)
                t0 = time.time()
                self._emit("file_start", {"id": f["id"], "name": f["name"]})
                pcb = self._make_file_progress(f["id"])
                try:
                    out = self._download_file(f, sess, cks, quality, connections, pcb)
                except Exception as exc:
                    out = {"status": "error", "reason": str(exc)}
                info = {"id": f["id"], "name": f["name"],
                        "status": out["status"], "reason": out.get("reason", "")}
                if out["status"].startswith("ok"):
                    with lock:
                        okc[0] += 1
                    p = out.get("path")
                    if p and os.path.exists(p):
                        rel = os.path.relpath(p, dest)
                        manifest[rel] = os.path.getsize(p)
                        info["bytes"] = os.path.getsize(p)
                        info["elapsed"] = time.time() - t0
                else:
                    with lock:
                        failc[0] += 1
                self._emit("file_done", info)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(parallel)]
        for t in threads:
            t.start()

        # Keep the playback auth token + cookies fresh while downloads run.
        # discover(force=True) re-navigates the live browser to capture a fresh
        # Authorization header (and lets Google rotate session cookies).
        video_id = video_file["id"] if video_file else None
        last_refresh = time.time()
        while any(t.is_alive() for t in threads):
            if self._abort.is_set():
                break
            if video_id and time.time() - last_refresh > 240:
                try:
                    vd.discover(self._browser, video_id, force=True)
                except Exception as exc:
                    self._emit("log", "Refresh token gagal: %s" % str(exc)[:100])
                try:
                    fresh = self._browser.cookies()
                    if fresh:
                        with cookies_lock:
                            cookies_holder["cookies"] = fresh
                except Exception:
                    pass
                last_refresh = time.time()
            time.sleep(5)

        for t in threads:
            t.join()

        self._save_manifest(manifest)
        self._emit("log", "━━━━━━━━━━━━━━━━━━━━")
        self._emit("log", "Ringkasan: %d berhasil, %d dilewati, %d gagal (dari %d)." % (
            okc[0], skipc[0], failc[0], total))
        self._emit("status", "Selesai: %d berhasil, %d dilewati, %d gagal." % (okc[0], skipc[0], failc[0]))
        self._emit("finished", {"ok": okc[0], "skip": skipc[0], "fail": failc[0], "total": total})

    def _flatten(self, items, dest, recursive, files, visited):
        for item in items:
            iid = item["id"]
            if iid in visited:
                continue
            if item.get("is_folder"):
                if not recursive:
                    continue
                visited.add(iid)
                sub = os.path.join(dest, sanitize_filename(item["name"]))
                os.makedirs(sub, exist_ok=True)
                self._emit("log", "📁 " + item["name"] + " (memindai...)")
                try:
                    children = self._list_folder(iid)
                except Exception as exc:
                    self._emit("progress", {"id": iid, "name": item["name"],
                                            "status": "gagal buka folder", "reason": str(exc)})
                    continue
                self._flatten([c for c in children if c["id"] != iid], sub, recursive, files, visited)
            else:
                files.append({"id": iid, "name": item["name"], "dest": dest})

    def _download_file(self, f, session, cookies, quality, connections, cb):
        res = download(session, f["id"], f["name"], f["dest"], progress_cb=cb)
        if res["status"] == "downloaded":
            return {"status": "ok", "path": res.get("path")}

        reason = res.get("reason", "")
        if is_video_name(f["name"]):
            # Video files are usually only reachable via the stream endpoint. The
            # standard endpoint may return "unexpected html" (e.g. a sign-in page
            # after Google rate-limits it) even while the stream endpoint still works,
            # so always fall back to the stream for videos on any non-success.
            try:
                path = download_video(f["id"], f["name"], f["dest"], cookies,
                                      quality=quality, connections=connections, progress_cb=cb)
                return {"status": "ok (stream)", "path": path}
            except Exception as stream_exc:
                return {"status": "gagal", "reason": "%s | stream: %s" % (reason, str(stream_exc)[:140])}

        if res["status"] == "not_downloadable":
            return {"status": "tidak bisa", "reason": reason}
        return {"status": "error", "reason": reason}

    def _list_folder(self, folder_id):
        url = "https://drive.google.com/drive/folders/%s" % folder_id
        self._browser.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        self._browser.page.wait_for_timeout(2500)
        scroll_shared_list(self._browser)
        return scrape_items(self._browser)

    # ---------------------------------------------------------------- shutdown
    def _shutdown(self):
        # Playwright's sync API is single-threaded: the browser must be closed on
        # the same worker thread that created it, otherwise we hit a greenlet /
        # EPIPE teardown crash. So ask the worker to close the browser, then exit.
        self._worker_stop = True
        self._abort.set()
        import video_download as vd
        vd.ABORT.set()
        self._cmd_queue.put(("close_browser", None))
        self._cmd_queue.put(("exit", None))
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=10.0)
        self._cleanup_partials()
        # Last-resort: if the worker was stuck mid-navigation, hard-kill the Chrome
        # process tree directly (pure subprocess, no Playwright thread affinity).
        b = self._browser
        if b is not None:
            try:
                b._terminate_chrome(b._chrome_proc)
            except Exception:
                pass
            self._browser = None

    def _cleanup_partials(self):
        base = self._cfg.get("download_dir") or cfg_mod.DOWNLOAD_DIR
        removed = 0
        for root, _dirs, files in os.walk(base):
            for fn in files:
                low = fn.lower()
                if ".part" in low or low.endswith(".vid.mp4") or low.endswith(".aud.m4a"):
                    try:
                        os.remove(os.path.join(root, fn))
                        removed += 1
                    except Exception:
                        pass
        return removed


def main():
    api = DriveApi()
    html = _render_html()
    window = webview.create_window(
        "Gdrive Clonner - Shared with Me",
        html=html,
        js_api=api,
        width=1200,
        height=800,
        min_size=(900, 600),
        background_color="#0b1220",
    )
    api._window = window
    try:
        # Use a persistent WebView2 user-data folder (non-private) so pywebview
        # doesn't create/delete a temp dir on every run -- that temp-dir cleanup
        # fails with "[WinError 5] Access is denied" on Windows.
        webview.start(
            private_mode=False,
            storage_path=os.path.join(cfg_mod.BASE_DIR, "webview_data"),
        )
    finally:
        api._shutdown()


if __name__ == "__main__":
    main()