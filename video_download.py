import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time

from downloader import build_session, sanitize_filename

ORIGIN = "https://drive.google.com"
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

PROGRESSIVE_PREFERENCE = [22, 18]

DEFAULT_CONNECTIONS = 8

ABORT = threading.Event()

TRANSIENT = (429, 500, 502, 503, 504)

_notify = print
_last_retry_log = [0.0]


def set_notify(fn):
    global _notify
    if fn is not None:
        _notify = fn


def _retry_log(msg):
    now = time.time()
    if now - _last_retry_log[0] > 5.0:
        _last_retry_log[0] = now
        try:
            _notify(msg)
        except Exception:
            pass


class _Transient(Exception):
    pass

_state = {"endpoint": None, "api_key": None, "authorization": None}
_lock = threading.Lock()


def _sapisidhash(cookies, origin=ORIGIN):
    sapisid = next((c["value"] for c in cookies if c["name"] == "SAPISID"), "")
    t = str(int(time.time()))
    h = hashlib.sha1((t + " " + sapisid + " " + origin).encode("utf-8")).hexdigest()
    return "SAPISIDHASH %s_%s" % (t, h)


def _auth_headers(cookies, origin=ORIGIN, ref=None):
    # Prefer the Authorization header captured from the browser during discover()
    # (a valid OAuth/SAPISIDHASH token) over a locally-computed SAPISIDHASH.
    auth = _state.get("authorization") or _sapisidhash(cookies, origin)
    headers = {
        "Authorization": auth,
        "X-Goog-AuthUser": "0",
        "Origin": origin,
        "User-Agent": CHROME_UA,
    }
    if ref:
        headers["Referer"] = ref
    return headers


def _base_headers(ref):
    return {"Referer": ref, "Origin": ORIGIN, "X-Goog-AuthUser": "0", "User-Agent": CHROME_UA}


def _parse_formats(body):
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    data = json.loads(body)
    pr = json.loads(data["mediaStreamingData"]["serializedHouseBrandPlayerResponse"])
    sd = pr.get("streamingData", {})
    adaptive = sd.get("adaptiveFormats", [])
    return {
        "progressive": sd.get("formats", []),
        "video": [f for f in adaptive if (f.get("mimeType") or "").startswith("video/")],
        "audio": [f for f in adaptive if (f.get("mimeType") or "").startswith("audio/")],
    }


def _nudge_play(page):
    try:
        for fr in [page.main_frame] + list(page.frames):
            try:
                fr.evaluate(
                    "() => { const v=document.querySelector('video');"
                    " if(v){v.muted=false; const p=v.play(); if(p&&p.catch)p.catch(function(){});}"
                    " const b=document.querySelector('.ytp-large-play-button'); if(b) b.click(); }"
                )
            except Exception:
                pass
    except Exception:
        pass


def discover(browser, file_id, timeout=45, force=False):
    global _state
    with _lock:
        if not force and _state["endpoint"] and _state["api_key"]:
            return _state["endpoint"], _state["api_key"]

        page = browser.page
        captured = [None]

        def on_resp(resp):
            if captured[0] is None and "/v1/drive/media/" in resp.url and "/playback" in resp.url:
                captured[0] = resp

        page.on("response", on_resp)
        try:
            browser.open_file(file_id)
            deadline = time.time() + timeout
            while time.time() < deadline and captured[0] is None:
                _nudge_play(page)
                time.sleep(1)
        finally:
            try:
                page.remove_listener("response", on_resp)
            except Exception:
                pass

        resp = captured[0]
        if resp is None:
            raise RuntimeError("gagal menangkap endpoint playback")

        # Capture the exact Authorization header the browser used, so we can reuse
        # a valid token instead of only a locally-computed SAPISIDHASH.
        try:
            h = resp.request.headers
            _state["authorization"] = h.get("authorization") or h.get("Authorization")
        except Exception:
            pass

        u = resp.url
        m = re.search(r"https://([^/]+)(/v1/drive/media)", u)
        if not m:
            raise RuntimeError("format URL playback tak dikenal: %s" % u[:120])
        host = m.group(1)
        prefix = m.group(2)
        km = re.search(r"[?&]key=([^&]+)", u)
        key = km.group(1) if km else None

        _state["endpoint"] = "https://%s%s" % (host, prefix)
        _state["api_key"] = key
        return _state["endpoint"], _state["api_key"]


def get_playback(file_id, cookies, endpoint=None, api_key=None):
    endpoint = endpoint or _state["endpoint"]
    api_key = api_key or _state["api_key"]
    if not endpoint:
        raise RuntimeError("endpoint belum ditemukan; panggil discover() dulu")

    url = "%s/%s/playback?auditContext=forDisplay" % (endpoint, file_id)
    if api_key:
        url += "&key=%s" % api_key
    ref = "%s/file/d/%s/preview" % (ORIGIN, file_id)
    sess = build_session(cookies)
    sess.headers.update(_auth_headers(cookies, ORIGIN, ref))

    last = None
    for attempt in range(6):
        try:
            r = sess.get(url, timeout=60)
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 200:
            return _parse_formats(r.text)
        if r.status_code in TRANSIENT:
            last = "http %s" % r.status_code
            _retry_log("playback http %s, retry %d/6..." % (r.status_code, attempt + 1))
            time.sleep(1.5 * (attempt + 1))
            continue
        raise RuntimeError("playback http %s: %s" % (r.status_code, r.text[:200]))
    raise RuntimeError("playback gagal setelah retry: %s" % (last or "?"))


def _ratebypass(url):
    if "ratebypass" in url:
        return url
    return url + ("&ratebypass=yes" if "?" in url else "?ratebypass=yes")


def _content_length(cookies, url, headers):
    for attempt in range(4):
        sess = build_session(cookies)
        sess.headers.update(headers)
        try:
            with sess.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=60) as r:
                if r.status_code in TRANSIENT:
                    raise _Transient(r.status_code)
                r.raise_for_status()
                cr = r.headers.get("Content-Range") or ""
                if cr and "/" in cr:
                    return int(cr.rsplit("/", 1)[1])
                return int(r.headers.get("Content-Length") or 0)
        except _Transient:
            time.sleep(1.0 * (attempt + 1))
        except Exception:
            return 0
    return 0


def _download_sequential(cookies, url, path, headers, progress_cb=None):
    last = None
    for attempt in range(5):
        try:
            sess = build_session(cookies)
            sess.headers.update(headers)
            with sess.get(url, stream=True, timeout=180) as r:
                if r.status_code in TRANSIENT:
                    raise _Transient(r.status_code)
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                with open(path, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            fh.write(chunk)
                            done += len(chunk)
                            if progress_cb:
                                progress_cb(done, total or done)
            return path
        except _Transient:
            last = "server 5xx"
        except Exception as exc:
            last = exc
        time.sleep(1.0 * (attempt + 1))
    raise RuntimeError("download gagal: %s" % (last or "?"))


def _download_to(cookies, url, path, headers, connections=DEFAULT_CONNECTIONS, progress_cb=None):
    url = _ratebypass(url)
    try:
        total = _content_length(cookies, url, headers)
    except Exception:
        total = 0

    if total <= 0 or total < 4 * 1024 * 1024:
        return _download_sequential(cookies, url, path, headers, progress_cb)

    CHUNK = 2 * 1024 * 1024
    n_chunks = (total + CHUNK - 1) // CHUNK
    nxt = [0]
    done = [0]
    errors = []
    lock = threading.Lock()
    parts = {}

    def worker():
        while True:
            if ABORT.is_set():
                return
            with lock:
                i = nxt[0]
                nxt[0] += 1
            if i >= n_chunks:
                return
            start = i * CHUNK
            end = min(start + CHUNK - 1, total - 1)
            part_path = "%s.part%04d" % (path, i)
            hdr = {"Range": "bytes=%d-%d" % (start, end)}
            ok = False
            last_err = None
            for attempt in range(6):
                try:
                    sess = build_session(cookies)
                    sess.headers.update(headers)
                    with sess.get(url, headers=hdr, stream=True, timeout=120) as r:
                        if r.status_code in TRANSIENT:
                            raise _Transient(r.status_code)
                        r.raise_for_status()
                        with open(part_path, "wb") as fh:
                            for c in r.iter_content(1024 * 1024):
                                if c:
                                    fh.write(c)
                    ok = True
                    break
                except Exception as exc:
                    # Read timeouts, broken pipes, truncated responses (IncompleteRead)
                    # and 5xx are all transient: retry the same chunk on a fresh
                    # connection instead of failing the whole file.
                    last_err = exc
                    _retry_log("chunk %d error, retry %d/6 (%s)" % (i, attempt + 1, type(exc).__name__))
                    time.sleep(1.0 * (attempt + 1))
            if not ok:
                with lock:
                    errors.append(last_err or Exception("chunk %d gagal" % i))
                return
            with lock:
                parts[i] = part_path
                done[0] += os.path.getsize(part_path)
            if progress_cb:
                progress_cb(done[0], total)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, connections))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        for p in parts.values():
            try:
                os.remove(p)
            except Exception:
                pass
        raise errors[0]

    with open(path, "wb") as out:
        for i in range(n_chunks):
            p = parts[i]
            with open(p, "rb") as fh:
                shutil.copyfileobj(fh, out)
            os.remove(p)
    return path


def _find(lst, itag):
    for f in lst:
        if f["itag"] == itag:
            return f
    return None


def _choose(pb, quality):
    prog = pb["progressive"]
    av = pb["video"]
    aa = pb["audio"]

    def prog_best(*tags):
        for t in tags:
            f = _find(prog, t)
            if f:
                return f
        return None

    if quality == "360p":
        f = prog_best(18, 22)
        if f:
            return ("prog", f)
    elif quality == "720p":
        f = prog_best(22, 18)
        if f:
            return ("prog", f)
    elif quality == "1080p":
        v = _find(av, 137) or _find(av, 136)
        a = _find(aa, 140) or _find(aa, 141) or (aa[0] if aa else None)
        if v and a:
            return ("adaptive", v, a)
        f = prog_best(22, 18)
        if f:
            return ("prog", f)

    f = prog_best(22, 18)
    if f:
        return ("prog", f)
    v = _find(av, 137) or _find(av, 136) or (av[0] if av else None)
    a = _find(aa, 140) or (aa[0] if aa else None)
    if v and a:
        return ("adaptive", v, a)
    return None


def _mp4_name(name):
    n = sanitize_filename(name)
    if not n.lower().endswith(".mp4"):
        root, ext = os.path.splitext(n)
        n = (root if ext else n) + ".mp4"
    return n


def download_video(file_id, name, dest_dir, cookies, quality="720p",
                   connections=DEFAULT_CONNECTIONS, progress_cb=None):
    os.makedirs(dest_dir, exist_ok=True)

    if not _state["endpoint"]:
        raise RuntimeError("stream endpoint belum ditemukan; panggil discover() dulu")

    pb = get_playback(file_id, cookies)

    ref = "%s/file/d/%s/preview" % (ORIGIN, file_id)
    headers = _base_headers(ref)

    choice = _choose(pb, quality)
    if choice is None:
        raise RuntimeError("tidak ada format untuk kualitas %s" % quality)

    if choice[0] == "prog":
        f = choice[1]
        path = _unique(os.path.join(dest_dir, _mp4_name(name)))
        _download_to(cookies, f["url"], path, headers, connections, progress_cb)
        return path

    v, a = choice[1], choice[2]
    stem = sanitize_filename(name)
    vpath = os.path.join(dest_dir, stem + ".vid.mp4")
    apath = os.path.join(dest_dir, stem + ".aud.m4a")
    _download_to(cookies, v["url"], vpath, headers, connections, progress_cb)
    _download_to(cookies, a["url"], apath, headers, connections, progress_cb)

    if not (shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")):
        os.remove(vpath)
        os.remove(apath)
        raise RuntimeError("ffmpeg tidak ditemukan (perlu untuk menggabungkan 1080p)")

    out = _unique(os.path.join(dest_dir, _mp4_name(name)))
    muxcmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
              "-i", vpath, "-i", apath, "-c", "copy", "-movflags", "+faststart", out]
    proc = subprocess.run(muxcmd, capture_output=True, text=True)
    os.remove(vpath)
    os.remove(apath)
    if proc.returncode != 0:
        raise RuntimeError("mux gagal: %s" % proc.stderr[-400:])
    return out


def _unique(path):
    if not os.path.exists(path):
        return path
    stamp = time.strftime("%Y%m%d_%H%M%S")
    root, ext = os.path.splitext(path)
    return "%s_%s%s" % (root, stamp, ext)