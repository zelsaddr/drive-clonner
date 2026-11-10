import os
import subprocess
import time

import requests
from playwright.sync_api import sync_playwright

SHARED_URL = "https://drive.google.com/drive/shared-with-me"

CHROME_CANDIDATES = [
    os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
]

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-infobars",
    "--no-default-browser-check",
]

REGION_JS = """() => {
  const dpr = window.devicePixelRatio || 1;
  const ow = window.outerWidth, oh = window.outerHeight;
  const iw = window.innerWidth, ih = window.innerHeight;
  let left = (window.screenX || 0) + (ow - iw) / 2;
  let top = (window.screenY || 0) + (oh - ih);
  return {
    left: Math.round(left * dpr),
    top: Math.round(top * dpr),
    width: Math.round(iw * dpr),
    height: Math.round(ih * dpr),
    dpr: dpr,
  };
}"""


def _find_chrome():
    for p in CHROME_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


def _stealth_script():
    from playwright_stealth import StealthConfig
    return "\n".join(StealthConfig().enabled_scripts)


class DriveBrowser:
    def __init__(self, profile_dir, headless=False, launch_mode="cdp", cdp_port=9222):
        self.profile_dir = profile_dir
        self.headless = headless
        self.launch_mode = launch_mode
        self.cdp_port = cdp_port
        self.pw = None
        self.context = None
        self.page = None
        self._browser = None
        self._chrome_proc = None
        self._port = None

    def start(self):
        self.pw = sync_playwright().start()
        if self.launch_mode == "cdp":
            self._start_cdp()
        else:
            self._start_stealth()
        return self.page

    def _start_cdp(self):
        chrome = _find_chrome()
        if not chrome:
            raise RuntimeError("Chrome tidak ditemukan. Install Chrome atau gunakan mode stealth.")

        self._port = self.cdp_port
        endpoint = "http://127.0.0.1:%d" % self._port
        if not self._cdp_alive(endpoint):
            os.makedirs(self.profile_dir, exist_ok=True)
            cmd = [
                chrome,
                "--remote-debugging-port=%d" % self._port,
                "--user-data-dir=%s" % self.profile_dir,
                "--start-maximized",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-notifications",
            ]
            if self.headless:
                cmd.append("--headless=new")
                cmd.append("--window-size=1920,1080")
            self._chrome_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._wait_for_cdp(endpoint)
        else:
            self._chrome_proc = None

        self._browser = self.pw.chromium.connect_over_cdp(endpoint)
        self.context = self._browser.contexts[0]
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def _cdp_alive(self, endpoint):
        try:
            resp = requests.get(endpoint + "/json/version", timeout=2)
            return resp.status_code == 200 and "Chrome" in (resp.text or "")
        except Exception:
            return False

    def _start_stealth(self):
        os.makedirs(self.profile_dir, exist_ok=True)
        self.context = self.pw.chromium.launch_persistent_context(
            self.profile_dir,
            channel="chrome",
            headless=self.headless,
            viewport=None,
            no_viewport=True,
            args=STEALTH_ARGS + ["--start-maximized"],
        )
        self.context.add_init_script(_stealth_script())
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

    def _wait_for_cdp(self, endpoint, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                requests.get(endpoint + "/json/version", timeout=2)
                return
            except Exception:
                time.sleep(0.3)
        raise RuntimeError("Gagal menyambung ke Chrome (CDP timeout).")

    def close(self):
        try:
            if self.context is not None and self.page is not None:
                try:
                    session = self.context.new_cdp_session(self.page)
                    session.send("Browser.close")
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(0.4)
        if self.pw is not None:
            try:
                self.pw.stop()
            except Exception:
                pass
        if self._chrome_proc is not None:
            try:
                self._chrome_proc.terminate()
            except Exception:
                pass
            self._chrome_proc = None
        self.context = None
        self.page = None
        self._browser = None
        self.pw = None

    def open_shared_with_me(self):
        self.page.goto(SHARED_URL, wait_until="domcontentloaded", timeout=90000)
        self.page.wait_for_timeout(3000)

    def cookies(self):
        if self.context is None:
            return []
        return self.context.cookies()

    def open_file(self, file_id):
        url = "https://drive.google.com/file/d/%s/preview" % file_id
        self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        return url

    def window_region(self):
        try:
            cdp = self.context.new_cdp_session(self.page)
            bounds = cdp.send("Browser.getWindowForTarget")["bounds"]
            dpr = self.page.evaluate("window.devicePixelRatio")
            vw = self.page.evaluate("window.innerWidth")
            vh = self.page.evaluate("window.innerHeight")
            left_dip = bounds["left"] + (bounds["width"] - vw) / 2.0
            top_dip = bounds["top"] + (bounds["height"] - vh)
            return {
                "left": int(round(left_dip * dpr)),
                "top": int(round(top_dip * dpr)),
                "width": int(round(vw * dpr)),
                "height": int(round(vh * dpr)),
                "dpr": dpr,
            }
        except Exception:
            return self.page.evaluate(REGION_JS)

    def bring_to_front(self):
        try:
            self.page.bring_to_front()
        except Exception:
            pass