import re

NAME_LOOKUP_JS = r"""
(arg) => {
  const selector = arg[0];
  const nameClasses = arg[1];
  const out = [];
  const seen = new Set();
  const nodes = document.querySelectorAll(selector);
  for (const el of nodes) {
    const id = (el.getAttribute('data-id') || '').trim();
    if (!id || seen.has(id)) continue;
    seen.add(id);

    let name = '';
    const aria = (el.getAttribute('aria-label') || '').trim();
    const tooltip = (el.getAttribute('data-tooltip') || '').trim();
    if (aria) name = aria;
    else if (tooltip) name = tooltip;
    else {
      for (const cls of nameClasses) {
        const cand = el.querySelector('[class*="' + cls + '"]');
        if (cand && cand.innerText && cand.innerText.trim()) {
          name = cand.innerText.trim();
          break;
        }
      }
    }
    if (!name) {
      const clone = el.cloneNode(true);
      for (const img of clone.querySelectorAll('img,svg')) img.remove();
      const text = (clone.innerText || '').trim();
      const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
      if (lines.length) name = lines[0];
    }

    const low = name.toLowerCase();
    const isFolder = low.includes('folder') || low.endsWith(' map') || low.endsWith(' folder')
      || /, ?folder$/.test(low);

    out.push({ id, name, isFolder });
  }
  return out;
}
"""


def scrape_items(browser):
    page = browser.page
    selector = "[data-id]"
    name_classes = ["Q5txwe", "s2Wjec", "KL4NAf"]
    items = page.evaluate(NAME_LOOKUP_JS, [selector, name_classes])
    cleaned = []
    for it in items:
        name = _clean_name(it["name"])
        if not name or name == "(unnamed)":
            continue
        if "WIZ_global_data" in name or name.startswith("window."):
            continue
        if len(name) > 200:
            continue
        cleaned.append({
            "id": it["id"],
            "name": name,
            "is_folder": it["isFolder"],
        })
    return cleaned


TYPE_LABELS = [
    "folder bersama", "microsoft excel", "google spreadsheet", "google dokumen",
    "google formulir", "google slide", "arsip terkompresi", "tidak diketahui",
    "spreadsheet", "dokumen", "formulir", "excel", "pdf", "gambar", "video",
    "audio", "foto", "music", "arsip", "folders", "folder",
]


def _clean_name(raw):
    name = (raw or "").strip()
    name = re.sub(r"\s*Info\s+selengkapnya.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*(\(Alt\s*\+.*?\))\s*$", "", name, flags=re.IGNORECASE)
    for label in sorted(TYPE_LABELS, key=len, reverse=True):
        name = re.sub(r"\s+" + re.escape(label) + r"\s*$", "", name, flags=re.IGNORECASE)
    name = name.replace("\n", " ").strip().strip(" \t-—–·")
    return name or "(unnamed)"


def wait_for_list(page, timeout=3000):
    """Wait until the Drive item list has rendered (returns early once items appear)."""
    try:
        page.wait_for_selector("[data-id]", timeout=timeout)
    except Exception:
        pass
    page.wait_for_timeout(200)


def scroll_shared_list(browser, rounds=15):
    page = browser.page
    last = None
    for _ in range(rounds):
        before = page.evaluate("document.querySelectorAll('[data-id]').length")
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(400)
        after = page.evaluate("document.querySelectorAll('[data-id]').length")
        if after == before and last == after:
            break
        last = after
    page.evaluate("window.scrollTo(0, 0)")


def list_shared_items(browser):
    browser.open_shared_with_me()
    scroll_shared_list(browser)
    items = scrape_items(browser)
    return items