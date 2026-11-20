#!/usr/bin/env python3
"""Daily HD e-paper -> single linked PDF for Prothom Alo + The Daily Star.

Source: epapers.vercel.app proxy endpoints (upstream APIs are not directly
reachable). Pages are selected by their stable NewsProPageTitle.

HD rules verified on the live service:
  Daily Star   .../<hash>_01_mr.jpg  ->  .../<hash>_01.jpg   (2424x3723)
  Prothom Alo  .../<8hex><8hex>_01_hr.jpg -> .../<8hex>_01.jpg (2569x4032)

Every page record also carries hotspot rectangles. Each rectangle is turned
into a PDF /Link over the HD page image, pointing at that story's cropped
article image (filepathstorypic) so a tap opens the readable crop in the
browser.

State file stores only the last fully-sent date.
Runtime secrets (env): TG_TOKEN, TG_CHAT.
"""
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

try:
    import epaper_bot as EB
except Exception:
    EB = None

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "epaper_state.json")

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
# Dedicated e-paper bot (@hawker0_bot). Falls back to the probe bot.
EPAPER_TOKEN = os.environ.get("EPAPER_TOKEN", "")
EPAPER_CHAT = os.environ.get("EPAPER_CHAT", "")

BASE = "https://epapers.vercel.app"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

PAPERS = [
    {
        "label": "Prothom Alo",
        "edition": 1,
        "pages": "/prothomalo/api/GetAllpages",
        "api": "/prothomalo/api",
        "slots": [
            ("front", ["প্রথম পাতা"], "first"),
            ("last", ["শেষ পাতা"], "last"),
            ("international", ["আন্তর্জাতিক"], None),
            ("editorial", ["অভিমত"], None),
            ("opinion", ["মতামত"], None),
        ],
    },
    {
        "label": "Daily Star",
        "edition": 1,
        "pages": "/dailystar/postapi/GetAllpagespost",
        "api": "/dailystar/api",
        "slots": [
            ("front", ["FRONT"], "first"),
            ("last", ["BACK"], "last"),
            ("international", ["INTERNATIONAL"], None),
            ("editorial", ["EDITORIAL"], None),
            ("opinion", ["OPINION"], None),
        ],
    },
    {
        "label": "Daily Star Business",
        "edition": 7,
        "pages": "/dailystar/postapi/GetAllpagespost",
        "api": "/dailystar/api",
        "slots": [
            ("front", ["BIZFRONT"], "first"),
            ("last", ["BIZBACK"], "last"),
        ],
    },
]


def get(url, timeout=60, tries=3):
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
    return json.loads(get(url, timeout).decode("utf-8", "replace"))


def norm(s):
    s = (s or "").upper().replace("–", "-").replace("—", "-")
    return " ".join(s.split())


def today_dhaka():
    g = time.gmtime(time.time() + 6 * 3600)
    return f"{g.tm_mday:02d}/{g.tm_mon:02d}/{g.tm_year}"


def _slots_from_env():
    """EPAPER_SLOTS='{"Prothom Alo": ["প্রথম পাতা", "শেষ পাতা"]}'"""
    raw = os.environ.get("EPAPER_SLOTS", "").strip()
    if not raw:
        return
    try:
        want = json.loads(raw)
    except Exception as e:
        print(f"EPAPER_SLOTS ignored: {e}")
        return
    for cfg in PAPERS:
        titles = want.get(cfg["label"]) or want.get(cfg["label"].split()[0])
        if not titles:
            continue
        cfg["slots"] = [
            (re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-") or f"p{i}", [t], None)
            for i, t in enumerate(titles)
        ]
        print(f"{cfg['label']}: {len(titles)} slots from EPAPER_SLOTS")


_slots_from_env()


def dump_pages(date_str):
    """EPAPER_DISCOVER=1 prints every page the API offers, for slot tuning."""
    if os.environ.get("EPAPER_DISCOVER") != "1":
        return
    for cfg in PAPERS:
        try:
            rows = get_json(
                f"{BASE}{cfg['pages']}?editionid={cfg['edition']}&editiondate={date_str}"
            )
        except Exception as e:
            print(f"{cfg['label']}: {e}")
            continue
        print(f"[{cfg['label']}] {len(rows)} pages")
        for p in rows:
            print(
                f"   ed{p.get('EditionID')} p{p.get('PageNo')} id{p.get('PageId')} "
                f"{p.get('NewsProPageTitle')!r}"
            )


def hd_urls(paper, page):
    """Best-first HD candidates for one page record."""
    if paper == "Prothom Alo":
        hr = page.get("XHighResolution") or ""
        out = []
        m = re.match(r"^(.*/)([0-9a-f]{8})[0-9a-f]{8}_(\d+)_hr\.jpg$", hr)
        if m:
            out.append(f"{m.group(1)}{m.group(2)}_{m.group(3)}.jpg")
        out += [hr, page.get("HighResolution") or ""]
        return [u for u in out if u]
    mr = page.get("MrImageUrl") or ""
    out = []
    if mr.endswith("_mr.jpg"):
        out.append(mr[: -len("_mr.jpg")] + ".jpg")
    out.append(mr)
    return [u for u in out if u]


def fetch_best(urls):
    err = None
    for u in urls:
        try:
            blob = get(u, timeout=120, tries=2)
            if len(blob) > 50000:
                return blob, u
        except Exception as e:
            err = e
    if err:
        raise err
    raise IOError("no candidate")


def as_int(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def page_no(p):
    try:
        return int(p.get("PageNo") or 0)
    except Exception:
        return 0


def pick(pages, candidates, edition, fallback, used=()):
    """Match a section by title, preferring this paper's main edition.

    Titles are the only cross-day key the API offers (PageId changes daily),
    so nothing is pinned to a page number. Ties between two pages carrying the
    same section name (e.g. an edition-specific duplicate) are settled by
    EditionID, then by the earlier candidate, then by page order.
    """
    best = None
    for p in pages:
        if p.get("PageId") in used:
            continue
        t = norm(p.get("NewsProPageTitle"))
        if not t:
            continue
        for i, c in enumerate(candidates):
            n = norm(c)
            if t == n:
                rank = 3
            elif t.startswith(n):
                rank = 2
            elif n in t:
                rank = 1
            else:
                continue
            key = (rank, 1 if as_int(p.get("EditionID"), -1) == as_int(edition, -2) else 0, -i, -page_no(p))
            if best is None or key > best[0]:
                best = (key, p)
            break
    if best is not None:
        return best[1], False
    if not fallback:
        return None, False
    fresh = sorted(
        (p for p in pages if p.get("PageId") not in used),
        key=page_no,
    )
    if not fresh:
        return None, False
    return (fresh[0] if fallback == "first" else fresh[-1]), True


def story_pic(api, sid, depth=3):
    seen = set()
    while sid and sid not in seen and depth > 0:
        seen.add(sid)
        depth -= 1
        try:
            d = get_json(f"{BASE}{api}/getstorydetail?Storyid={sid}")
        except Exception:
            return None
        p = (d.get("filepathstorypic") or "").replace("\\", "/")
        if p.startswith("http"):
            return p
        sid = d.get("LinkedStoryId") or 0
    return None


def page_links(cfg, page, w, h, tries=2):
    """Hotspot rects -> (x0, y0, x1, y1, url). Retries an empty page."""
    links = []
    for attempt in range(tries):
        links = _page_links_once(cfg, page, w, h)
        if links:
            break
        time.sleep(3)
    return links


def _page_links_once(cfg, page, w, h):
    api = cfg["api"]
    pw = float(page.get("PageWidth") or page.get("pageImageWidth") or w)
    ph = float(page.get("PageHeight") or page.get("pageImageHeight") or h)
    sx, sy = w / pw, h / ph
    try:
        hs = get_json(f"{BASE}{api}/getingRectangleObject?pageid={page['PageId']}")
    except Exception as e:
        print(f"    hotspots failed: {str(e)[:60]}")
        return []
    hs = hs if isinstance(hs, list) else []
    out, skipped = [], 0
    for x in hs:
        url = None
        link = x.get("Url") or ""
        if x.get("HaveLink") and link.startswith("http"):
            url = link
        elif x.get("ObjectId"):
            if x.get("ObjectType") == 2:
                url = story_pic(api, x["ObjectId"])
            else:
                try:
                    d = get_json(f"{BASE}{api}/getpicturedetail?Pic_id={x['ObjectId']}")
                    p = (d.get("ImagePath") or "").replace("\\", "/")
                    url = p if p.startswith("http") else None
                except Exception:
                    url = None
        if not url:
            skipped += 1
            continue
        try:
            x0 = float(x["Rect_Left"]) * sx
            x1 = float(x["Rect_Right"]) * sx
            y0 = float(x["Rect_Top"]) * sy
            y1 = float(x["Rect_Bottom"]) * sy
        except Exception:
            skipped += 1
            continue
        x0, x1 = max(0.0, min(x0, x1)), min(w, max(x0, x1))
        y0, y1 = max(0.0, min(y0, y1)), min(h, max(y0, y1))
        if x1 - x0 < 10 or y1 - y0 < 10:
            skipped += 1
            continue
        out.append((x0, y0, x1, y1, url))
    print(
        f"    hotspots {len(hs)} -> links {len(out)} "
        f"(story/pic resolved, skipped {skipped})"
    )
    return out


LADDER = [
    (1600, 85),
    (1500, 84),
    (1400, 84),
    (1350, 83),
    (1300, 82),
    (1250, 82),
    (1200, 83),
    (1100, 85),
    (1000, 88),
]


def encode_page(it, width, quality):
    """Downscale (halftone noise is thrown away), sharpen, baseline JPEG."""
    from PIL import Image, ImageFilter

    im = Image.open(io.BytesIO(it["blob"]))
    if im.mode != "RGB":
        im = im.convert("RGB")
    w0, h0 = im.size
    if width and w0 > width:
        im = im.resize((width, max(1, round(h0 * width / w0))), Image.LANCZOS)
        im = im.filter(ImageFilter.UnsharpMask(radius=1.2, percent=85, threshold=2))
    buf = io.BytesIO()
    im.save(
        buf, "JPEG", quality=quality, optimize=True, subsampling=2, progressive=False
    )
    return buf.getvalue()


def build_pdf(items, path, target_w=595.0):
    from reportlab import rl_config

    # Binary DCTDecode streams: ASCII85 wrapping inflated every page by 25%.
    rl_config.useA85 = 0
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(path)
    c.setTitle("E-papers")
    for it in items:
        w, h = it["size"]
        s = target_w / float(w)
        c.setPageSize((w * s, h * s))
        blob = it.get("enc") or it["blob"]
        c.drawImage(ImageReader(io.BytesIO(blob)), 0, 0, w * s, h * s)
        for x0, y0, x1, y1, url in it["links"]:
            c.linkURL(
                url,
                (x0 * s, (h - y1) * s, x1 * s, (h - y0) * s),
                relative=0,
                thickness=0,
            )
        key = "p%d" % it["n"]
        c.bookmarkPage(key)
        c.addOutlineEntry(it["label"], key, level=0)
        c.showPage()
    c.save()


def discover_chat(token):
    """Last chat that talked to this bot, so /start alone binds delivery."""
    if not token:
        return ""
    try:
        d = get_json(f"https://api.telegram.org/bot{token}/getUpdates?offset=-1&limit=1", 20)
    except Exception:
        return ""
    for u in reversed(d.get("result") or []):
        for k in ("message", "edited_message", "channel_post"):
            c = (u.get(k) or {}).get("chat") or {}
            if c.get("id"):
                return str(c["id"])
    return ""


def send_document(chat, blob, filename, caption, mime="application/pdf", token=None):
    token = token or TG_TOKEN
    if not (token and chat):
        print(f"[DRY-RUN] {filename} ({len(blob) // 1024} KB) {caption}")
        return True
    bnd = "----epaper" + os.urandom(8).hex()
    body = (
        f"--{bnd}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat}\r\n"
        f"--{bnd}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption[:900]}\r\n"
        f"--{bnd}\r\nContent-Disposition: form-data; name=\"document\"; "
        f"filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n"
    ).encode() + blob + f"\r\n--{bnd}--\r\n".encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={bnd}"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
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


def collect(date_str):
    dump_pages(date_str)
    items, missing = [], []
    for cfg in PAPERS:
        try:
            rows = get_json(
                f"{BASE}{cfg['pages']}?editionid={cfg['edition']}&editiondate={date_str}"
            )
        except Exception as e:
            missing.append(f"{cfg['label']}: api {str(e)[:60]}")
            continue
        rows = rows if isinstance(rows, list) else []
        used = set()
        for slot, cands, fallback in cfg["slots"]:
            p, inferred = pick(rows, cands, cfg["edition"], fallback, used)
            if not p or not hd_urls(cfg["label"], p):
                missing.append(f"{cfg['label']}/{slot}")
                continue
            used.add(p.get("PageId"))
            note = " (inferred: no title match)" if inferred else ""
            print(
                f"  {cfg['label']} {slot}: ed{p.get('EditionID')} "
                f"p{p.get('PageNo')} id{p.get('PageId')} "
                f"{p.get('NewsProPageTitle')!r}{note}"
            )
            items.append(
                {
                    "cfg": cfg,
                    "slot": slot,
                    "page": p,
                    "title": p.get("NewsProPageTitle") or slot,
                }
            )
    return items, missing


def _pending(db, date_str, cap):
    """Subscribers still owed today's PDF, in join order."""
    out = []
    if not (db and EB):
        return out
    for cid in EB.subscribers(db):
        u = (db.get("users") or {}).get(cid) or {}
        if u.get("last_sent") == date_str:
            continue
        if u.get("last_fail") == date_str and int(u.get("fails") or 0) >= 3:
            continue
        out.append(cid)
        if len(out) >= cap:
            break
    return out


def run(date_str=None, chat=None, force=False, retries=1, wait=300, token=None):
    from concurrent.futures import ThreadPoolExecutor
    from PIL import Image

    date_str = date_str or today_dhaka()
    st = load_state()

    if not force and st.get("last") == date_str:
        print(f"already sent for {date_str}, skipping")
        return -1, 0, []

    # An explicit chat means the sys-probe /epaper command: reply with that bot.
    # Otherwise deliver through the dedicated e-paper bot.
    explicit = chat is not None
    dry = os.environ.get("EPAPER_DRY") == "1"
    if dry:
        token, chat = "", "dry"
    elif chat:
        token = token or TG_TOKEN
    else:
        token = token or EPAPER_TOKEN or TG_TOKEN
    db = None
    if not dry and chat is None and EB:
        db = EB.load()
        EB.publish_commands(token)
        if not EB.subscribers(db) and EPAPER_CHAT:
            chat = EPAPER_CHAT
    if not dry and chat is None and not (db and EB.subscribers(db)):
        chat = EPAPER_CHAT or discover_chat(token)
        if not chat:
            print("e-paper: no subscribers yet, send /start to the e-paper bot")
            return 0, 0, ["chat"]
        if db is not None:
            u = db["users"].setdefault(
                str(chat),
                {"sub": True, "joined": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            )
            u["sub"] = True

    expect = sum(len(c["slots"]) for c in PAPERS)
    need = int(os.environ.get("EPAPER_MIN_PAGES", "0") or 0) or expect
    items, missing = [], []
    for attempt in range(max(1, retries)):
        items, missing = collect(date_str)
        if len(items) >= need:
            missing = []
            break
        if attempt + 1 < retries:
            print(f"attempt {attempt + 1}: {len(items)}/{expect} pages, wait {wait}s")
            time.sleep(wait)

    if len(items) < need:
        print(f"incomplete ({len(items)}/{need} needed), not sending. missing={missing}")
        return 0, len(items), missing

    def fetch(it):
        try:
            return it, fetch_best(hd_urls(it["cfg"]["label"], it["page"]))
        except Exception as e:
            return it, (None, f"{it['cfg']['label']}/{it['slot']} {str(e)[:50]}")

    built = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for it, (blob, used) in ex.map(fetch, items):
            if blob is None:
                missing.append(used)
                continue
            built.append(
                {
                    "n": len(built) + 1,
                    "size": Image.open(io.BytesIO(blob)).size,
                    "blob": blob,
                    "used": used,
                    "page": it["page"],
                    "cfg": it["cfg"],
                    "slot": it["slot"],
                    "title": it["title"],
                    "label": f"{it['cfg']['label']} - {it['title']}",
                }
            )

    if len(built) < need:
        print(f"downloads incomplete ({len(built)}/{need} needed), not sending")
        return 0, len(built), missing

    def links(it):
        from PIL import Image

        w, h = Image.open(io.BytesIO(it["blob"])).size
        return page_links(it["cfg"], it["page"], w, h)

    with ThreadPoolExecutor(max_workers=6) as ex:
        for it, lk in zip(built, ex.map(links, built)):
            it["links"] = lk
    nolink = [f"{it['cfg']['label']}/{it['slot']}" for it in built if not it["links"]]
    if nolink:
        print("pages without links:", ", ".join(nolink))

    path = os.path.join(HERE, "epapers-" + date_str.replace("/", "-") + ".pdf")
    budget = int(float(os.environ.get("EPAPER_MAX_MB", "10")) * 1024 * 1024)
    cache = {}

    def enc_at(i):
        if i not in cache:
            w, q = LADDER[i]
            cache[i] = [(it, encode_page(it, w, q)) for it in built]
        return cache[i]

    try:
        i = 4  # 1300px is the usual landing spot; probe one step up/down
        while True:
            width, q = LADDER[i]
            enc = enc_at(i)
            total = sum(len(b) for _, b in enc)
            print(f"  {width}px q{q}: {total // 1024} KB")
            if total > budget:
                if i + 1 >= len(LADDER):
                    break
                i += 1
                continue
            if i > 0:
                up = enc_at(i - 1)
                if sum(len(b) for _, b in up) <= budget:
                    i -= 1
                    enc = up
                    width, q = LADDER[i]
            break
        for it, b in enc:
            it["enc"] = b
        pick = (width, q)
        build_pdf(built, path)
        size = os.path.getsize(path)
        blob = open(path, "rb").read()
        os.unlink(path)
    except ImportError as e:
        print("pdf libs missing:", str(e)[:120])
        return 0, len(built), missing + ["reportlab/pillow missing"]
    except Exception as e:
        print("pdf build failed:", str(e)[:160])
        return 0, len(built), missing + ["pdf build failed"]

    nlinks = sum(len(x["links"]) for x in built)
    cap = (
        f"E-papers {date_str} - {len(built)} pages at {pick[0]}px, "
        f"{size // (1024 * 1024)} MB, {nlinks} tappable article links.\n"
        + ", ".join(f"{c['label']} x{len(c['slots'])}" for c in PAPERS)
        + (f"\nNo links on: {', '.join(nolink)}" if nolink else "")
    )
    if dry:
        print(f"DRY RUN: built {os.path.basename(path)}, {size // 1024} KB, {nlinks} links")
        print(cap)
        return 1, len(built), missing

    sent_ok, failed = 0, 0
    if chat:
        try:
            sent_ok += 1 if send_document(chat, blob, os.path.basename(path), cap, token=token) else 0
        except Exception as e:
            print("send failed:", str(e)[:140])
            failed += 1
        if db is not None and sent_ok:
            (db["users"].setdefault(str(chat), {}))["last_sent"] = date_str
            try:
                EB.save(db)
            except Exception:
                pass
    else:
        batch = int(os.environ.get("EPAPER_MAX_SEND", "15") or 15)
        gap = float(os.environ.get("EPAPER_SEND_GAP", "2.5"))
        targets = _pending(db, date_str, batch)
        print(f"broadcast: {len(EB.subscribers(db))} subscribers, {len(targets)} this run")
        for cid in targets:
            u = db["users"].setdefault(cid, {})
            try:
                ok = send_document(cid, blob, os.path.basename(path), cap, token=token)
            except Exception as e:
                ok = False
                print(f"  {cid}: {str(e)[:90]}")
            if ok:
                u["last_sent"] = date_str
                sent_ok += 1
                EB.bump(db, "pdf_sent")
            else:
                u["last_fail"] = date_str
                u["fails"] = int(u.get("fails") or 0) + 1
                failed += 1
            EB.save(db)
            time.sleep(gap)
        db.setdefault("totals", {})["last_broadcast"] = {
            "date": date_str,
            "sent": sent_ok,
            "failed": failed,
        }
        EB.save(db)
    print(f"pdf {size // 1024} KB, links={nlinks}, sent={sent_ok}, failed={failed}")

    if sent_ok and not explicit and not _pending(db, date_str, 1):
        st["last"] = date_str
        st["sent_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        st["pages"] = len(built)
        save_state(st)
    return (1 if sent_ok else 0), len(built), missing


def status_text():
    st = load_state()
    try:
        db = EB.load() if EB else {}
    except Exception:
        db = {}
    subs = len(EB.subscribers(db)) if (EB and db) else 0
    return (
        f"last send: {st.get('last') or 'never'}"
        + (f" at {st.get('sent_at')}" if st.get("sent_at") else "")
        + f"\npages {st.get('pages') or '?'}, subscribers {subs}"
    )


def tick():
    """Answer bot commands; safe to call on every probe run."""
    if not EB:
        return
    token = EPAPER_TOKEN or TG_TOKEN
    if not token:
        return
    db = EB.load()
    ctx = {
        "status": status_text(),
        "epaper_now": lambda cid: run(date_str=today_dhaka(), chat=cid, force=True, token=token),
    }
    try:
        EB.poll(token, db, ctx)
    finally:
        EB.save(db)


def maybe_daily():
    """Called from the 10-minute probe: first run after 06:00 Dhaka wins."""
    try:
        tick()
    except Exception as e:
        print("epaper bot tick failed:", str(e)[:120])

    g = time.gmtime(time.time() + 6 * 3600)
    if g.tm_hour < 6 or g.tm_hour >= 13:
        return

    date_str = today_dhaka()
    if load_state().get("last") == date_str:
        if not (EB and _pending(EB.load(), date_str, 1)):
            return
        if g.tm_min % 10 > 1:
            return
    print("daily epaper trigger")
    run(date_str=date_str, retries=1)


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else None
    retries = int(os.environ.get("EPAPER_RETRIES", "1"))
    force = os.environ.get("EPAPER_FORCE") == "1"
    sent, total, missing = run(date_str=date_str, retries=retries, force=force)
    if missing:
        print("missing:", "; ".join(missing))
    if sent == -1:
        return 0
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
