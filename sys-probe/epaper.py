#!/usr/bin/env python3
"""Daily HD e-paper fetcher for Prothom Alo + The Daily Star.

Source: epapers.vercel.app proxies (the upstream APIs are not reachable
directly). Each page record exposes a stable human title
(NewsProPageTitle), so pages are selected by title, not by page number.

HD: Daily Star serves `..._01_mr.jpg`; dropping `_mr` gives the
2424x3723 master. Prothom Alo's `XHighResolution` is the largest file
published. Images are sent as documents so Telegram keeps the bytes.

State file (epaper_state.json) stores only the last sent date string.

Runtime secrets (env): TG_TOKEN, TG_CHAT.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "epaper_state.json")

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
VERBOSE = os.environ.get("PROBE_VERBOSE") == "1"

BASE = "https://epapers.vercel.app"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

# (paper label, edition id, api path, [(slot, [title candidates], page fallback)])
PAPERS = [
    (
        "Prothom Alo",
        1,
        "/prothomalo/api/GetAllpages",
        [
            ("front", ["প্রথম পাতা"], "first"),
            ("last", ["শেষ পাতা"], "last"),
            ("international", ["আন্তর্জাতিক"], None),
            ("editorial", ["অভিমত"], None),
            ("opinion", ["মতামত"], None),
        ],
    ),
    (
        "Daily Star",
        1,
        "/dailystar/postapi/GetAllpagespost",
        [
            ("front", ["FRONT"], "first"),
            ("last", ["BACK"], "last"),
            ("international", ["INTERNATIONAL"], None),
            ("editorial", ["EDITORIAL"], None),
            ("opinion", ["OPINION"], None),
        ],
    ),
    (
        "Daily Star Business",
        7,
        "/dailystar/postapi/GetAllpagespost",
        [
            ("front", ["BIZFRONT"], "first"),
            ("last", ["BIZBACK"], "last"),
        ],
    ),
]


def get(url, timeout=45, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(2 + 2 * i)
    raise last


def get_json(url, timeout=45):
    raw = get(url, timeout)
    return json.loads(raw.decode("utf-8", "replace"))


def norm(s):
    s = (s or "").upper()
    s = s.replace("–", "-").replace("—", "-")
    return " ".join(s.split())


def hd_url(paper, page):
    """Largest published image for one page record."""
    if paper == "Prothom Alo":
        return page.get("XHighResolution") or page.get("HighResolution")
    mr = page.get("MrImageUrl") or ""
    if mr.endswith("_mr.jpg"):
        return mr[: -len("_mr.jpg")] + ".jpg"
    return page.get("HrImageUrlJpg") or mr


def pages_for(paper, edition_id, api_path, date_str):
    url = f"{BASE}{api_path}?editionid={edition_id}&editiondate={date_str}"
    rows = get_json(url)
    return rows if isinstance(rows, list) else []


def pick(pages, candidates, fallback):
    titles = [norm(c) for c in candidates]
    for p in pages:
        n = norm(p.get("NewsProPageTitle"))
        for t in titles:
            if n and (n == t or n.startswith(t)):
                return p
    if not fallback:
        return None
    ordered = sorted(pages, key=lambda p: p.get("PageNo") or 0)
    if not ordered:
        return None
    return ordered[0] if fallback == "first" else ordered[-1]


def collect(date_str):
    """-> list of dicts: paper, slot, title, page_no, url"""
    out, missing = [], []
    for paper, edition_id, api_path, slots in PAPERS:
        try:
            pages = pages_for(paper, edition_id, api_path, date_str)
        except Exception as e:
            missing.append(f"{paper}: api {str(e)[:60]}")
            continue
        for slot, candidates, fallback in slots:
            p = pick(pages, candidates, fallback)
            url = hd_url(paper, p) if p else ""
            if not p or not url:
                missing.append(f"{paper}/{slot}")
                continue
            out.append(
                {
                    "paper": paper,
                    "slot": slot,
                    "title": p.get("NewsProPageTitle") or slot,
                    "page_no": p.get("PageNo"),
                    "url": url,
                }
            )
    return out, missing


def send_document(chat, blob, filename, caption):
    if not (TG_TOKEN and chat):
        print(f"[DRY-RUN] {filename} ({len(blob) // 1024} KB) {caption}")
        return True
    bnd = "----epaper" + os.urandom(8).hex()
    body = (
        f"--{bnd}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat}\r\n"
        f"--{bnd}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption[:900]}\r\n"
        f"--{bnd}\r\nContent-Disposition: form-data; name=\"document\"; "
        f"filename=\"{filename}\"\r\nContent-Type: image/jpeg\r\n\r\n"
    ).encode() + blob + f"\r\n--{bnd}--\r\n".encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={bnd}"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.status == 200


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception as e:
        print("state save failed:", str(e)[:80])


def run(date_str=None, chat=None, force=False, retries=1, wait=300):
    """Fetch and send today's pages. Returns (sent, total, missing)."""
    date_str = date_str or time.strftime("%d/%m/%Y")
    chat = chat or TG_CHAT
    st = load_state()

    if not force and st.get("last") == date_str and not os.environ.get("EPAPER_RESEND"):
        print(f"already sent for {date_str}, skipping")
        return 0, 0, []

    pages, missing = [], []
    for attempt in range(max(1, retries)):
        pages, missing = collect(date_str)
        expect = sum(len(s[3]) for s in PAPERS)
        if len(pages) >= expect:
            missing = []
            break
        if attempt + 1 < retries:
            print(f"attempt {attempt + 1}: {len(pages)}/{expect} pages, retrying in {wait}s")
            time.sleep(wait)

    sent = 0
    for p in pages:
        try:
            blob = get(p["url"], timeout=90, tries=2)
            name = (
                p["paper"].replace(" ", "_")
                + "_"
                + p["slot"].upper()
                + "_"
                + date_str.replace("/", "-")
                + ".jpg"
            )
            cap = f"{p['paper']} - {p['title']} (page {p['page_no']}) - {date_str}"
            if send_document(chat, blob, name, cap):
                sent += 1
            print(f"sent {name} ({len(blob) // 1024} KB)")
        except Exception as e:
            missing.append(f"{p['paper']}/{p['slot']} dl {str(e)[:60]}")

    dry = not (TG_TOKEN and chat)
    if sent and not missing and not dry:
        st["last"] = date_str
        save_state(st)
    elif sent and not dry:
        st["last_partial"] = date_str
        save_state(st)

    print(f"epaper: {sent}/{len(pages)} sent, missing={missing}")
    return sent, len(pages), missing


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else None
    retries = int(os.environ.get("EPAPER_RETRIES", "1"))
    sent, total, missing = run(date_str=date_str, retries=retries)
    if missing:
        print("missing:", "; ".join(missing))
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
