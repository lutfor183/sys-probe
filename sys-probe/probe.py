#!/usr/bin/env python3
"""Generic page-change probe. All stdlib.
Runtime secrets (env): PROBE_KEY, TG_TOKEN, TG_CHAT, MAIL_USER, MAIL_APP.
MAIL_TO defaults to lotfor1515@gmail.com.
State file keeps only hashes + dates, never titles or links.
"""
import re, os, sys, json, hashlib, html as H
import urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
BLOB = os.path.join(HERE, "targets.enc.json")
STATE = os.path.join(HERE, "state.json")

KEY = os.environ.get("PROBE_KEY", "")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
TEST = os.environ.get("PROBE_TEST") == "1"
MAIL_USER = os.environ.get("MAIL_USER", "")
MAIL_APP = os.environ.get("MAIL_APP", "")
MAIL_TO = os.environ.get("MAIL_TO") or "lotfor1515@gmail.com"

# Hard fallback: BCS exam list on bpsc.gov.bd (public URL, user-confirmed).
# Fetched even when the encrypted target list works, so a stale/changed
# exam_type ID in the blob can never blind us. Page 2 covers overflow.
FALLBACK_BASE = "https://bpsc.gov.bd"
FALLBACK_PAT = "/pages/psc-exams/"
FALLBACK_URLS = [
    "https://bpsc.gov.bd/pages/psc-exams?page=1&page_size=10&filters=%7B%22exam_type%22%3A%22691995ee933eb65569ddc1ba%22%7D",
    "https://bpsc.gov.bd/pages/psc-exams?page=2&page_size=10&filters=%7B%22exam_type%22%3A%22691995ee933eb65569ddc1ba%22%7D",
    "https://bpsc.gov.bd/pages/psc-exams?page=1&page_size=20",
]

BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
HOT = []  # filled from encrypted blob at runtime
# BCS-only monitor. norm() lowercases, converts BN digits ০-৯ -> 0-9, strips spaces/dots.
# So matching is case-insensitive + BN/EN insensitive.
HOT_EXTRA = [
    "finalresult", "final", "result",
    "ফলাফল", "চূড়ান্ত", "চুড়ান্ত", "চূড়ান্তফলাফল",
    "merit", "meritlist", "মেধা",
    "recommend", "সুপারিশ", "মনোনয়ন", "মনোনयन".replace("यन","য়ন"),
    "viva", "মৌখিক",
    "bcs", "বিসিএস",
]


# Strict second net: NEVER sends standalone alerts (no false flags).
# Only used as extra digest lines inside the footer-stamp-changed message.
# Requires BOTH a BCS-series number (43-55) AND a commission/cadre word.
MAYBE_NUM_RE = r"(4[3-9]|5[0-5])"
MAYBE_AUTH_KEYS = [
    "cadre", "ক্যাডার", "কাডার",
    "bpsc", "psc", "পিএসসি",
    "civilservice",
]


def extract_sitestamp(page):
    """Footer stamp e.g. 'সাইটটি শেষ হাল-নাগাদ করা হয়েছে: মঙ্গলবার, ৬ অক্টোবর, ২০২৬ এ ১৫:১৯:৫৪'.
    Returns normalized display string (up to 200 chars) or '' if not found."""
    if not page:
        return ""
    i = page.find("হাল-নাগাদ")
    if i < 0:
        i = page.find("হালনাগাদ")
    if i < 0:
        return ""
    seg = page[max(0, i - 60):i + 220]
    seg = re.sub(r"<[^>]+>", " ", seg)
    seg = H.unescape(seg)
    seg = re.sub(r"\s+", " ", seg).strip()
    seg = re.sub(r"<.*$", "", seg).strip()  # dangling unclosed tag
    _tm = re.search(r"[\u09e6-\u09ef0-9]{1,2}:[\u09e6-\u09ef0-9]{2}(?::[\u09e6-\u09ef0-9]{2})?", seg)
    if _tm:
        seg = seg[:_tm.end()]  # drop trailing footer text after HH:MM:SS
    for _cut in ("সাইটটি", "সাইট", "শেষ"):
        _j = seg.find(_cut)
        if _j >= 0:
            seg = seg[_j:]
            break
    return seg[:200]


def norm(s):
    s = H.unescape(s or "")
    s = re.sub(r"<[^>]+>", "", s)
    s = s.translate(BN_DIGITS)
    s = s.replace("।", "").replace(".", "")
    return re.sub(r"\s+", "", s)


CFG = {}


def hot_match(title):
    n = norm(title).lower()
    return any(k in n for k in HOT) or any(k in n for k in HOT_EXTRA)


def datekey(x):
    # DD-MM-YYYY (BN digits already -> EN) as (YYYY,MM,DD).
    # Plain string sort puts 30-07 above 01-10 and hides the newest items.
    try:
        d, m, y = x.get("date", "").strip().split("-")[:3]
        return (int(y), int(m), int(d))
    except Exception:
        return (0, 0, 0)


def is_50_final(title):
    # Emergency: 50th BCS + result words, BN/EN insensitive
    n = norm(title).lower()
    has50 = "50" in n  # ৫০ already -> 50 via BN_DIGITS
    result_words = ["final", "result", "ফলাফল", "চূড়ান্ত", "চুড়ান্ত", "merit", "সুপারিশ", "মনোনয়ন", "recommend"]
    return has50 and any(k in n for k in result_words)


def is_final_result(title):
    # Emergency: ANY BCS series 43-55 + result words, BN/EN insensitive.
    # (51st BCS-special is the live series as of Oct 2026; 50-only check
    # would miss its final result.)
    n = norm(title).lower()
    if not re.search(r"(4[3-9]|5[0-5])", n):
        return False
    result_words = ["final", "result", "ফলাফল", "চূড়ান্ত", "চুড়ান্ত", "merit", "সুপারিশ", "মনোনয়ন", "recommend"]
    return any(k in n for k in result_words)


def decrypt_targets():
    import base64
    from Crypto.Cipher import AES
    from Crypto.Protocol.KDF import PBKDF2
    from Crypto.Hash import SHA256

    j = json.load(open(BLOB, encoding="utf-8"))

    salt = base64.b64decode(j["salt"])
    iv = base64.b64decode(j["iv"])
    data = base64.b64decode(j["data"])

    k = PBKDF2(
        KEY,
        salt,
        dkLen=32,
        count=j.get("iter", 100000),
        hmac_hash_module=SHA256
    )

    c = AES.new(k, AES.MODE_GCM, nonce=iv)
    pt = c.decrypt_and_verify(data[:-16], data[-16:])

    return json.loads(pt.decode("utf-8"))


def get(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="ignore")


def parse(page):
    out = []

    for m in re.finditer(
        r'<tr class="table-tr">(.*?)</tr>',
        page,
        re.S
    ):
        row = m.group(1)

        t = re.search(
            r'data-column="title">\s*(.*?)\s*</td>',
            row,
            re.S
        )

        p = re.search(
            r'data-column="pdf">.*?href="([^"]+\.pdf)"',
            row,
            re.S
        )

        d = re.search(
            r'data-column="publish_date">.*?<span>(.*?)</span>',
            row,
            re.S
        )

        e = re.search(
            r'data-column="exam_type">.*?<span>(.*?)</span>',
            row,
            re.S
        )

        v = re.search(
            r'<a href="(' +
            re.escape(CFG.get("pat", "/")) +
            r'[^"]+)">',
            row
        )

        if not (t and v):
            continue

        exam_type = re.sub(
            r"\s+",
            " ",
            H.unescape(
                re.sub(r"<[^>]+>", "", e.group(1))
                if e else ""
            )
        ).strip()

        title = re.sub(
            r"\s+",
            " ",
            H.unescape(
                re.sub(r"<[^>]+>", "", t.group(1))
            )
        ).strip()

        _link = v.group(1)
        out.append({
            "h": hashlib.sha256(
                _link.encode()
            ).hexdigest()[:32],

            "h2": hashlib.sha256(
                (_link + "|" + title + "|" + (
                    H.unescape(d.group(1)).strip().translate(BN_DIGITS)
                    if d else ""
                )).encode()
            ).hexdigest()[:32],

            "title": title,

            "pdf": p.group(1) if p else "",

            "date": (
                H.unescape(d.group(1))
                .strip()
                .translate(BN_DIGITS)
                if d else ""
            ),

            "link": CFG["base"] + v.group(1),

            "exam_type": exam_type,
        })

    return out


def is_bcs(x_or_title):
    # ID-proof: does NOT rely on exam_type=...c1ba URL param.
    # Uses exam_type column text + title keywords, BN/EN + case insensitive.
    if isinstance(x_or_title, dict):
        t = x_or_title.get("title", "")
        e = x_or_title.get("exam_type", "")
        n = norm(t + " " + e).lower()
        if "বিসিএস" in (t + " " + e):
            return True
        return "bcs" in n
    n = norm(x_or_title).lower()
    if "বিসিএস" in x_or_title:
        return True
    return "bcs" in n


def is_maybe_bcs(x_or_title):
    """Strict second net: number 43-55 AND commission/cadre word, and NOT is_bcs().
    Generic words (seat/routine/circular/...) deliberately excluded to avoid spam."""
    if isinstance(x_or_title, dict):
        t = x_or_title.get("title", "")
        e = x_or_title.get("exam_type", "")
        raw = (t or "") + " " + (e or "")
    else:
        raw = x_or_title or ""
    if is_bcs(raw):
        return False
    n = norm(raw).lower()
    if not re.search(MAYBE_NUM_RE, n):
        return False
    return any(k in n for k in MAYBE_AUTH_KEYS)


# Broadcast model:
# - TG_CHAT is the owner channel (always notified).
# - ST["_subs"] is auto-built list of every chat that ever messaged the bot.
# - Passive BCS alerts go to owner + all subs. Active /all /bcs replies go to sender.
def send(text, chat_id=None):
    destination = chat_id or TG_CHAT

    if not (TG_TOKEN and destination):
        if os.environ.get("PROBE_VERBOSE") == "1":
            print("[DRY-RUN]\n" + text + "\n")
        else:
            print(
                f"[DRY-RUN] message suppressed ({len(text)} chars)"
            )
        return False

    data = urllib.parse.urlencode({
        "chat_id": destination,
        "text": text,
        "parse_mode": "HTML"
    }).encode()

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data=data
    )

    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status == 200


def subs():
    s = set(ST.get("_subs", []))
    if TG_CHAT:
        s.add(str(TG_CHAT))
    return sorted(s)


def add_sub(chat_id):
    if not chat_id:
        return
    c = str(chat_id)
    cur = set(str(x) for x in ST.get("_subs", []))
    if c not in cur:
        cur.add(c)
        ST["_subs"] = sorted(cur)


def broadcast(text):
    ok = False
    for dest in subs():
        try:
            if send(text, dest):
                ok = True
        except Exception as e:
            print(f"broadcast to {dest} failed: {str(e)[:60]}")
    # fallback: if no subs yet, send() already covers TG_CHAT via subs()
    return ok


def send_mail(subject, body):
    # Gmail SMTP over SSL (stdlib only). Needs MAIL_USER + MAIL_APP
    # (Gmail App Password) + MAIL_TO. Skips silently if unconfigured.
    if not (MAIL_USER and MAIL_APP and MAIL_TO):
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject[:120]
        msg["From"] = MAIL_USER
        msg["To"] = MAIL_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(MAIL_USER, MAIL_APP)
            s.sendmail(MAIL_USER, [MAIL_TO], msg.as_string())
        print("mailed:", subject[:60])
        return True
    except Exception as e:
        print("mail failed:", str(e)[:100])
        return False


def notify(text, mail_subject="BCS probe"):
    # Every passive alert goes to Telegram AND email (when configured).
    ok = broadcast(text)
    send_mail(mail_subject, text)
    return ok


def relevant(title):
    n = norm(title)
    return ("50" in n or "2025" in n) and hot_match(title)


def fetch_bytes(url):
    import shutil
    import subprocess
    import tempfile

    aria = shutil.which("aria2c")

    if aria:
        try:
            d = tempfile.mkdtemp(prefix="dl")

            r = subprocess.run(
                [
                    aria,
                    "-x", "8",
                    "-s", "8",
                    "-k", "1M",
                    "--max-tries=2",
                    "--connect-timeout=20",
                    "--timeout=60",
                    "--quiet=true",
                    "-d", d,
                    "-o", "f.pdf",
                    url
                ],
                capture_output=True,
                timeout=100
            )

            p = os.path.join(d, "f.pdf")

            if r.returncode == 0 and os.path.exists(p):
                with open(p, "rb") as f:
                    return f.read()

        except Exception as e:
            print(
                "fast-dl failed, fallback:",
                str(e)[:80]
            )

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def send_doc(url, caption, chat_id=None):
    # PDF broadcast: if chat_id given send there, else to all subs
    targets = [str(chat_id)] if chat_id else subs()
    if not (TG_TOKEN and targets):
        if os.environ.get("PROBE_VERBOSE") == "1":
            print("[DRY-RUN] would attach: " + url[:80])
        else:
            print("[DRY-RUN] attachment suppressed")
        return False

    try:
        blob = fetch_bytes(url)

        if len(blob) > 45 * 1024 * 1024 or len(blob) < 1000:
            return False

        ok_any = False
        for dest in targets:
            try:
                bnd = "----probe" + os.urandom(8).hex()
                cap = caption[:900]

                body = (
                    f"--{bnd}\r\n"
                    f"Content-Disposition: form-data; "
                    f"name=\"chat_id\"\r\n\r\n"
                    f"{dest}\r\n"

                    f"--{bnd}\r\n"
                    f"Content-Disposition: form-data; "
                    f"name=\"caption\"\r\n\r\n"
                    f"{cap}\r\n"

                    f"--{bnd}\r\n"
                    f"Content-Disposition: form-data; "
                    f"name=\"document\"; "
                    f"filename=\"notice.pdf\"\r\n"
                    f"Content-Type: application/pdf\r\n\r\n"
                ).encode() + blob + f"\r\n--{bnd}--\r\n".encode()

                req2 = urllib.request.Request(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                    data=body,
                    headers={
                        "Content-Type":
                            f"multipart/form-data; boundary={bnd}"
                    },
                )

                with urllib.request.urlopen(req2, timeout=120) as r2:
                    if r2.status == 200:
                        ok_any = True
            except Exception as e:
                print(f"attach to {dest} failed:", str(e)[:80])
                continue
        return ok_any

    except Exception as e:
        print(
            "attach failed:",
            str(e)[:100]
        )
        return False


def api(method, payload=None):
    data = (
        urllib.parse.urlencode(payload).encode()
        if payload else None
    )

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
        data=data
    )

    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(
            r.read().decode()
        )


def load_updates():
    off = ST.get("_upd", 0)

    try:
        d = api(
            "getUpdates",
            {
                "offset": off,
                "limit": 20,
                "timeout": 0
            }
        )

        return d.get("result", [])

    except Exception as e:
        print(
            "updates fetch failed:",
            str(e)[:80]
        )
        return []


def answer_commands(ctx):
    global ST

    if not TG_TOKEN:
        return

    for u in load_updates():

        ST["_upd"] = u.get("update_id", 0) + 1

        m = u.get("message", {})

        chat = str(
            m.get("chat", {}).get("id", "")
        )
        if not chat:
            continue

        # broadcast: anyone who talks to bot gets future passive alerts
        add_sub(chat)

        raw_text = (m.get("text") or "").strip()
        if not raw_text:
            continue
        # auto-subscribe even on plain hi, reply once
        if not raw_text.startswith("/"):
            send(
                "Subscribed to BCS alerts. Commands: /bcs /all /latest /status /check /test",
                chat,
            )
            continue

        cmd = raw_text.strip().lower().split()[0].split("@")[0]

        for full in (
            "/start",
            "/status",
            "/latest",
            "/bcs",
            "/all",
            "/check",
            "/test",
            "/help",
        ):
            if full.startswith(cmd) and len(cmd) >= 3:
                cmd = full
                break

        if cmd == "/start":
            send(
                "Subscribed to BCS alerts.\nCommands:\n/bcs - latest BCS\n/all - latest ALL PSC\n/latest - latest BCS\n/status /check /test",
                chat,
            )

        elif cmd == "/status":
            stamp = ctx.get("stamp", "")
            send(
                f"Probe OK. Tracking {ctx['tracked']} BCS items. "
                f"Last check: {ctx['checked_at']}. "
                f"New this run: {ctx['new']} "
                f"(maybe={ctx.get('maybe', 0)} other={ctx.get('other', 0)}). "
                f"Subs: {len(subs())}. "
                f"State: "
                f"{'BLIND' if ctx['blind'] else 'watching'}. "
                f"Site stamp: {stamp or 'n/a'}. "
                f"Mail: {'on' if ctx.get('mail') else 'OFF - set MAIL_USER/MAIL_APP secrets'}."
                f"{' DEGRADED (fallback URLs only).' if ctx.get('degraded') else ''}",
                chat,
            )

        elif cmd == "/bcs":
            top = ctx.get("top_bcs", [])[:5]
            if not top:
                send("No BCS items cached yet.", chat)
            else:
                lines = [
                    f"{i+1}. "
                    f"{x['title'][:120]} "
                    f"({x['date']})"
                    for i, x in enumerate(top)
                ]
                send("Latest BCS (top 5):\n\n" + "\n\n".join(lines), chat)

        elif cmd in ("/all", "/latest"):
            top = ctx.get("top_all", [])[:5]
            if not top:
                send("No items cached yet.", chat)
            else:
                lines = [
                    f"{i+1}. "
                    f"{x['title'][:120]} "
                    f"({x['date']})"
                    for i, x in enumerate(top)
                ]
                send("Latest ALL (top 5):\n\n" + "\n\n".join(lines), chat)

        elif cmd == "/check":
            send(
                f"Checked just now "
                f"({ctx['checked_at']}). "
                f"New BCS this run: {ctx['new']}. "
                f"{'All quiet.' if not ctx['new'] else 'Alerts sent above.'}",
                chat,
            )

        elif cmd == "/test":
            send("Probe test: channel live.", chat)

        else:
            send("Commands: /bcs /all /latest /status /check /test", chat)


def run_url():
    base = os.environ.get(
        "GITHUB_SERVER_URL",
        "https://github.com"
    )

    repo = os.environ.get(
        "GITHUB_REPOSITORY",
        ""
    )

    rid = os.environ.get(
        "GITHUB_RUN_ID",
        ""
    )

    if repo and rid:
        return f"{base}/{repo}/actions/runs/{rid}"

    return "(local run)"


ST = {}


def main():
    global ST

    if TEST:

        ok = notify(
            "Probe test: monitoring channel is live. "
            "You will get an alert here on any new item "
            "or any failure.",
            "Probe test",
        )

        print(
            "test sent"
            if ok
            else "test dry-run (secrets missing)"
        )

        return

    degraded = False

    try:

        global CFG, HOT

        CFG = decrypt_targets()

        HOT = CFG.get("hot", [])

        urls = [
            CFG["base"] + u
            for u in CFG["urls"]
        ]

        hurls = [
            CFG["base"] + u
            for u in CFG.get("hot_urls", [])
        ]

    except Exception as e:

        print(
            "FATAL: cannot unlock target list:",
            e
        )

        send(
            f"PROBE FAILURE: cannot unlock target list "
            f"({e}). Running DEGRADED on fallback BCS URLs. "
            f"Logs: {run_url()}"
        )
        send_mail(
            "PROBE DEGRADED: target list locked",
            f"Cannot unlock target list ({e}). Watching fallback BCS URLs only. Logs: {run_url()}"
        )

        CFG = {"base": FALLBACK_BASE, "pat": FALLBACK_PAT,
               "urls": [], "hot": [], "hot_urls": []}
        HOT = []
        urls, hurls = [], []
        degraded = True

    items_all, errors = [], []
    stamps = []

    # Parallel fetch (stdlib threads): cuts per-pass time from serial
    # sum(urls) to roughly max(urls). Same parsing/stamp logic as before.
    def _fetch(u):
        try:
            return (u, get(u), "")
        except Exception as e:
            return (u, "", f"{e}"[:120])

    _seen_u = set()
    _all_urls = []
    for _u in [CFG["base"]] + list(urls) + list(FALLBACK_URLS):
        if _u and _u not in _seen_u:
            _seen_u.add(_u)
            _all_urls.append(_u)
    _pages = {}
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as _ex:
            for _u, _body, _err in _ex.map(_fetch, _all_urls):
                if _err:
                    errors.append(f"{_u[-40:]}: {_err}" if _u != CFG["base"] else f"home: {_err}")
                elif _body:
                    _pages[_u] = _body
    except Exception:
        _pages = {}
        for _u in _all_urls:
            try:
                _pages[_u] = get(_u)
            except Exception as e:
                errors.append(f"home: {e}" if _u == CFG["base"] else f"{e}")

    if CFG["base"] in _pages:
        _s = extract_sitestamp(_pages[CFG["base"]])
        if _s:
            stamps.append(_s)

    for u in [x for x in _all_urls if x != CFG["base"]]:
        _page = _pages.get(u, "")
        if not _page:
            continue
        try:
            items_all += parse(_page)
            _s = extract_sitestamp(_page)
            if _s:
                stamps.append(_s)
        except Exception as e:
            errors.append(f"{e}")

    # dedupe by hash, keep first
    seen_h = set()
    dedup_all = []
    for x in items_all:
        if x["h"] not in seen_h:
            seen_h.add(x["h"])
            dedup_all.append(x)
    items_all = dedup_all

    # ID-proof BCS filter: exam_type column OR title keywords.
    # Even if exam_type=...c1ba ID changes, BCS still caught from /all page.
    items = [x for x in items_all if is_bcs(x)]

    hot_items = []

    if hurls:
        def _fetch_hot(u):
            try:
                return (u, get(u), "")
            except Exception as e:
                return (u, "", f"{e}"[:120])
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=4) as _hex:
                _hot_pages = list(_hex.map(_fetch_hot, hurls))
        except Exception:
            _hot_pages = [_fetch_hot(u) for u in hurls]
        for _u, _hpage, _herr in _hot_pages:
            if _herr:
                errors.append(f"extra: {_herr}")
                continue
            try:
                hot_items += [
                    x
                    for x in parse(_hpage)
                    if relevant(x["title"])
                ]
                _s = extract_sitestamp(_hpage)
                if _s:
                    stamps.append(_s)
            except Exception as e:
                errors.append(f"extra: {e}")
    # also include BCS matches from hot urls even if ID changed
    hot_items = [x for x in hot_items if is_bcs(x)] if hot_items else []

    ctx = {
        "tracked": 0,
        "new": 0,
        "blind": True,
        "checked_at":
            __import__("datetime")
            .datetime
            .now(
                __import__("datetime").timezone.utc
            )
            .strftime("%Y-%m-%d %H:%M UTC"),
        "top": []
    }

    if not items_all:

        # Total blind: both sources failed.
        st = {}

        if os.path.exists(STATE):

            try:
                st = json.load(
                    open(
                        STATE,
                        encoding="utf-8"
                    )
                )

            except Exception:
                pass

        if not st.get("_failing"):

            st["_failing"] = True

            json.dump(
                st,
                open(STATE, "w")
            )

            notify(
                f"PROBE FAILURE: all sources unreachable "
                f"({'; '.join(errors)[:200]}). "
                f"You are currently BLIND - retrying every few min. "
                f"Logs: {run_url()}",
                "PROBE FAILURE: blind",
            )

            print("failure alert sent")

        else:

            print(
                "still failing, alert already sent"
            )

        ST.update(st)

        answer_commands(ctx)

        json.dump(
            ST,
            open(STATE, "w")
        )

        return

    st = {}

    first = not os.path.exists(STATE)

    if not first:

        try:

            st = json.load(
                open(
                    STATE,
                    encoding="utf-8"
                )
            )

        except Exception:

            first = True

    was_failing = st.pop(
        "_failing",
        False
    )

    seen_ids = set(st)
    hashv2 = st.get("_hashv2", False)

    def _fresh(x):
        # Link unseen -> check content hash too (catches in-place edits).
        # First run with h2 just seeds silently (old link-only behavior).
        if x["h"] in seen_ids:
            return False
        if not hashv2:
            return True
        return x.get("h2", "") not in seen_ids

    new = [x for x in items if _fresh(x)]

    uniq = [
        x
        for x in new
        if x["h"] not in seen_ids
        and not seen_ids.add(x["h"])
    ]

    for x in items + hot_items:
        st[x["h"]] = x["date"]
        if x.get("h2"):
            st[x["h2"]] = x["date"]
    if not hashv2:
        st["_hashv2"] = True
        print("hashv2 seeded (content hashes stored, no flood)")

    json.dump(
        st,
        open(STATE, "w")
    )

    hnew = [x for x in hot_items if _fresh(x)]

    print(
        f"total={len(items)} "
        f"new={len(uniq)} "
        f"hot_extra={len(hnew)}"
    )

    uniq += [
        x
        for x in hnew
        if x not in uniq
    ]

    # ---- site footer stamp: most common value seen this run ----
    cur_stamp = ""
    if stamps:
        from collections import Counter
        cur_stamp = Counter(stamps).most_common(1)[0][0]
    old_stamp = st.get("_sitestamp", "")
    if cur_stamp and old_stamp.startswith(cur_stamp):
        stamp_changed = False  # format cleanup only, adopt silently
    else:
        stamp_changed = bool(cur_stamp and old_stamp and cur_stamp != old_stamp)
    if cur_stamp:
        st["_sitestamp"] = cur_stamp

    # ---- anti-miss second net (computed from pre-run seen_ids) ----
    maybe_new = [
        x for x in items_all
        if _fresh(x) and not is_bcs(x) and is_maybe_bcs(x)
    ]
    # dedupe maybe by hash
    _mh = set()
    maybe_new = [x for x in maybe_new if x["h"] not in _mh and not _mh.add(x["h"])]
    other_new = [
        x for x in items_all
        if _fresh(x) and not is_bcs(x) and not is_maybe_bcs(x)
    ]
    _oh = set()
    other_new = [x for x in other_new if x["h"] not in _oh and not _oh.add(x["h"])]
    for x in maybe_new + other_new:
        st[x["h"]] = x["date"]

    print(f"stamp_changed={stamp_changed} maybe={len(maybe_new)} other={len(other_new)}")

    if first:
        _smsg = f" Site stamp: {cur_stamp}" if cur_stamp else ""
        notify(
            f"Probe started. Tracking "
            f"{len(items)} BCS items ({len(items_all)} total). "
            f"Alerts will arrive here on any new BCS item.{_smsg}",
            "Probe started",
        )

        ST.update(st)

        answer_commands(ctx)

        json.dump(
            ST,
            open(STATE, "w")
        )

        return

    if was_failing:

        notify(
            "Probe recovered: sources reachable again.",
            "Probe recovered",
        )

    top_bcs_sorted = sorted(items, key=datekey, reverse=True)
    top_all_sorted = sorted(items_all, key=datekey, reverse=True)
    ctx.update({
        "tracked": len([k for k in st if not k.startswith("_")]),

        "new": len(uniq),

        "blind": False,

        "top": top_bcs_sorted,

        "top_bcs": top_bcs_sorted,

        "top_all": top_all_sorted,

        "stamp": cur_stamp,

        "stamp_changed": stamp_changed,

        "maybe": len(maybe_new),

        "other": len(other_new),

        "mail": bool(MAIL_USER and MAIL_APP),

        "degraded": degraded,
    })

    ST = st

    for x in uniq:

        is_hot = hot_match(
            x["title"]
        )
        is_emg = is_final_result(x["title"])

        if is_emg:
            flag = "\n🚨🚨 EMERGENCY: BCS FINAL RESULT - CHECK IMMEDIATELY 🚨🚨"
            prefix = "🚨🚨 EMERGENCY BCS FINAL RESULT 🚨🚨\n\n"
        elif is_hot:
            flag = "\n*** BCS keywords matched - CHECK ***"
            prefix = "NEW BCS ITEM ALERT\n\n"
        else:
            flag = ""
            prefix = "NEW BCS ITEM ALERT\n\n"

        msg = (
            f"{prefix.strip()}{flag}\n\n"
            f"Title: {x['title']}\n"
            f"Published: {x['date']}\n"
            f"Details: {x['link']}"
        )

        if x["pdf"]:
            msg += f"\nPDF: {x['pdf']}"

        # Passive BCS alert → owner + all subs + email
        notify(
            msg,
            f"{'EMERGENCY BCS FINAL RESULT' if is_emg else 'New BCS notice'}: {x['title'][:80]}",
        )

        if x["pdf"]:

            tag = (
                "🚨 EMERGENCY BCS FINAL notice"
                if is_emg
                else ("BCS Notice" if is_hot else "Notice")
            )

            if send_doc(
                x["pdf"],
                f"{tag}: {x['title'][:800]}"
            ):

                print(
                    "attached pdf for:",
                    x["h"]
                )

        print(
            "alerted:",
            x["h"]
        )

    # Baseline once: historic non-BCS hashes were never stored before this
    # feature, so the very first run after deploy must NOT alert on them.
    sig_init = st.get("_sig_init", False)
    if not sig_init:
        st["_sig_init"] = True
        print(f"signal baseline stored (maybe={len(maybe_new)} other={len(other_new)}), no digest sent")

    if stamp_changed and sig_init:
        lines = []
        if other_new:
            for i, x in enumerate(other_new[:5]):
                lines.append(f"{i+1}. {x['title'][:110]} ({x['date']})")
        extra = ""
        if maybe_new or other_new:
            extra = f" New non-BCS this run: maybe={len(maybe_new)} other={len(other_new)}."
            if lines:
                extra += "\n\nNew other items (check manually):\n" + "\n".join(lines)
        notify(
            f"SITE UPDATED (footer timestamp changed)\n\nOld: {old_stamp}\nNew: {cur_stamp}.{extra}\nChecked: {ctx['checked_at']}",
            f"Site updated: {cur_stamp[:80]}",
        )
        print("stamp-alerted")

    # Commands → whoever sent the command
    answer_commands(ctx)

    json.dump(
        ST,
        open(STATE, "w")
    )


if __name__ == "__main__":
    main()
