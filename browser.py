import os
import subprocess
import sys
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

    def _ensure_playwright_chromium(self):
        """Return Playwright's bundled Chromium path, auto-installing it first if needed."""
        try:
            return self.pw.chromium.executable_path
        except Exception:
            subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
            return self.pw.chromium.executable_path

    def _runtime_binary(self):
        """Resolve a Chromium/Chrome executable, preferring system Chrome."""
        chrome = _find_chrome()
        if chrome:
            return chrome
        try:
            return self._ensure_playwright_chromium()
        except Exception as exc:
            raise RuntimeError(
                "Chrome tidak ditemukan dan Chromium Playwright gagal diinstal otomatis.\n"
                "Jalankan manual: %s -m playwright install chromium\nDetail: %s"
                % (sys.executable, exc)
            )

    def _start_cdp(self):
        chrome = self._runtime_binary()

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
        launch_kwargs = {
            "headless": self.headless,
            "viewport": None,
            "no_viewport": True,
            "args": STEALTH_ARGS + ["--start-maximized"],
        }
        if _find_chrome():
            launch_kwargs["channel"] = "chrome"
        else:
            self._ensure_playwright_chromium()
        self.context = self.pw.chromium.launch_persistent_context(
            self.profile_dir,
            **launch_kwargs,
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
        # 1) Graceful close via CDP (also lets Chrome flush its profile/cookies).
        try:
            if self.context is not None and self.page is not None:
                try:
                    session = self.context.new_cdp_session(self.page)
                    session.send("Browser.close")
                except Exception:
                    pass
        except Exception:
            pass

        # 2) Wait for Chrome to actually exit so the Playwright driver finishes
        #    dispatching close events before we tear it down (avoids an EPIPE
        #    crash in the Node driver on Windows).
        self._wait_cdp_dead("http://127.0.0.1:%d" % self.cdp_port, timeout=10.0)
        time.sleep(0.3)

        # 3) Stop the Playwright driver.
        if self.pw is not None:
            try:
                self.pw.stop()
            except Exception:
                pass
            self.pw = None

        # 4) Make sure the Chrome process tree is gone.
        self._terminate_chrome(self._chrome_proc)
        self._chrome_proc = None

        self.context = None
        self.page = None
        self._browser = None

    def _wait_cdp_dead(self, endpoint, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                requests.get(endpoint + "/json/version", timeout=1)
            except Exception:
                return
            time.sleep(0.2)

    def _terminate_chrome(self, proc):
        if proc is None:
            return
        # Give graceful shutdown a moment to flush the profile before hard-kill.
        try:
            proc.wait(timeout=2.0)
            return
        except Exception:
            pass
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def open_shared_with_me(self):
        self.page.goto(SHARED_URL, wait_until="domcontentloaded", timeout=90000)
        try:
            self.page.wait_for_selector("[data-id]", timeout=5000)
        except Exception:
            pass
        self.page.wait_for_timeout(300)

    def is_logged_in(self, timeout=90000):
        """Navigate to Shared-with-me and report whether we are past the Google login page."""
        self.page.goto(SHARED_URL, wait_until="domcontentloaded", timeout=timeout)
        return "accounts.google.com" not in (self.page.url or "")

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