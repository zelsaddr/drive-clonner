import html
import os
import re
import urllib.parse

import requests

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

NOT_DOWNLOADABLE_MARKERS = [
    "can't download",
    "cannot download",
    "can not download",
    "you can't view or download",
    "download is disabled",
    "downloads are disabled",
    "not available for download",
    "download not allowed",
    "file cannot be downloaded",
    "quota exceeded",
    "tidak dapat didownload",
    "tidak dapat di download",
    "tidak bisa didownload",
    "tidak bisa di download",
    "tidak dapat diunduh",
    "tidak bisa diunduh",
    "file tidak dapat didownload",
    "unduhan tidak diizinkan",
]

RESULT_DOWNLOADED = "downloaded"
RESULT_NOT_DOWNLOADABLE = "not_downloadable"
RESULT_ERROR = "error"


def sanitize_filename(name):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
    name = name.strip(" .")
    return name or "file"


def build_session(cookies):
    session = requests.Session()
    session.headers.update({"User-Agent": CHROME_UA})
    for c in cookies:
        try:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
        except Exception:
            continue
    return session


def download(session, item_id, name, dest_dir, progress_cb=None):
    os.makedirs(dest_dir, exist_ok=True)
    url = "https://drive.google.com/uc?export=download&id=%s" % item_id

    for _ in range(5):
        try:
            r = session.get(url, stream=True, allow_redirects=True, timeout=120)
        except Exception as exc:
            return {"status": RESULT_ERROR, "reason": str(exc)}

        content_type = (r.headers.get("Content-Type") or "").lower()
        if content_type and "text/html" not in content_type and "application/json" not in content_type:
            path = _write_stream(r, name, content_type, dest_dir, progress_cb)
            return {"status": RESULT_DOWNLOADED, "path": path}

        text = r.text if "text/html" in content_type else _read_limited(r)

        if r.status_code in (401, 403, 404):
            return _report_not_available(r.status_code, text)

        next_url = _parse_confirm_form(text)
        if next_url:
            url = next_url
            continue

        if _looks_like_error_html(text):
            return _report_not_available(r.status_code, text)

        return {"status": RESULT_ERROR, "reason": "unexpected html: %s" % text[:200]}

    return {"status": RESULT_ERROR, "reason": "download confirmation loop exceeded"}


def _parse_confirm_form(text):
    action_match = re.search(r"<form\b[^>]*action=[\"']([^\"']+)[\"']", text, re.IGNORECASE)
    if not action_match:
        return None
    if 'id="download-form"' not in text:
        return None
    action = html.unescape(action_match.group(1))
    if action.startswith("/"):
        action = "https://drive.google.com" + action

    params = {}
    for tag in re.finditer(r"<input\b[^>]*>", text, re.IGNORECASE):
        raw = tag.group(0)
        nm = re.search(r"name=[\"']([^\"']+)[\"']", raw, re.IGNORECASE)
        if not nm:
            continue
        vl = re.search(r"value=[\"']([^\"']*)[\"']", raw, re.IGNORECASE)
        params[nm.group(1)] = html.unescape(vl.group(1)) if vl else ""

    if "confirm" not in params:
        return None
    return action + "?" + urllib.parse.urlencode(params)


def _looks_like_error_html(text):
    low = text.lower()
    return any(m in low for m in NOT_DOWNLOADABLE_MARKERS)


def _report_not_available(status, text):
    title = ""
    m = re.search(r"<title>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
    if m:
        title = html.unescape(m.group(1)).strip()
    if status == 404:
        title = title or "file Google native / tidak tersedia"
    return {"status": RESULT_NOT_DOWNLOADABLE, "reason": title or "download disabled (status %s)" % status}


def _read_limited(resp):
    try:
        return resp.content[:200000].decode("utf-8", errors="replace")
    except Exception:
        return ""


def _write_stream(resp, name, content_type, dest_dir, progress_cb=None):
    filename = None
    cd = resp.headers.get("Content-Disposition") or ""
    if "filename=" in cd:
        parts = cd.split("filename=", 1)[1]
        filename = parts.split(";")[0].strip().strip('"')
    if not filename:
        ext = _ext_for_mime(content_type)
        filename = sanitize_filename(name)
        if ext and not filename.lower().endswith(ext):
            filename += ext
    else:
        filename = sanitize_filename(filename)

    path = os.path.join(dest_dir, filename)
    total = int(resp.headers.get("Content-Length") or 0)
    written = 0
    with open(path, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                fh.write(chunk)
                written += len(chunk)
                if progress_cb:
                    progress_cb(written, total or written)
    return path


def _ext_for_mime(content_type):
    mime_map = {
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/x-matroska": ".mkv",
        "video/webm": ".webm",
        "video/x-msvideo": ".avi",
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/x-rar-compressed": ".rar",
    }
    for mime, ext in mime_map.items():
        if mime in content_type:
            return ext
    return ""


def is_video_name(name):
    from config import VIDEO_EXTENSIONS
    ext = os.path.splitext(name)[1].lower()
    return ext in VIDEO_EXTENSIONS