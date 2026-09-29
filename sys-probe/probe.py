#!/usr/bin/env python3
"""Generic page-change probe. All stdlib.
Runtime secrets (env): PROBE_KEY, TG_TOKEN, TG_CHAT.
Repo contains no target URLs and no non-English text.
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

BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
HOT = []  # filled from encrypted blob at runtime
HOT_EXTRA = ["finalresult", "final", "meritlist", "recommend"]


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

        v = re.search(
            r'<a href="(' +
            re.escape(CFG.get("pat", "/")) +
            r'[^"]+)">',
            row
        )

        if not (t and v):
            continue

        title = re.sub(
            r"\s+",
            " ",
            H.unescape(
                re.sub(r"<[^>]+>", "", t.group(1))
            )
        ).strip()

        out.append({
            "h": hashlib.sha256(
                v.group(1).encode()
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
        })

    return out


# IMPORTANT:
# chat_id=None means automatic alerts go to TG_CHAT.
# When a user sends a command, answer_commands()
# passes that user's chat ID so the reply goes back to them.
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


def send_doc(url, caption):
    # Automatic PDF alerts remain restricted to TG_CHAT.
    if not (TG_TOKEN and TG_CHAT):
        if os.environ.get("PROBE_VERBOSE") == "1":
            print("[DRY-RUN] would attach: " + url[:80])
        else:
            print("[DRY-RUN] attachment suppressed")
        return False

    try:
        blob = fetch_bytes(url)

        if len(blob) > 45 * 1024 * 1024 or len(blob) < 1000:
            return False

        bnd = "----probe" + os.urandom(8).hex()
        cap = caption[:900]

        body = (
            f"--{bnd}\r\n"
            f"Content-Disposition: form-data; "
            f"name=\"chat_id\"\r\n\r\n"
            f"{TG_CHAT}\r\n"

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
            }
        )

        with urllib.request.urlopen(req2, timeout=120) as r2:
            return r2.status == 200

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

    # TG_CHAT is still required because it is the
    # destination for automatic monitoring alerts.
    # It does NOT restrict who can use commands.
    if not (TG_TOKEN and TG_CHAT):
        return

    for u in load_updates():

        ST["_upd"] = u.get("update_id", 0) + 1

        m = u.get("message", {})

        # The chat ID of whoever sent the command.
        chat = str(
            m.get("chat", {}).get("id", "")
        )

        raw_text = m.get("text") or ""

        text = (
            raw_text.strip().lower().split()[0]
            if raw_text.strip()
            else ""
        )

        # Anyone can issue commands.
        if not text.startswith("/"):
            continue

        cmd = text.split("@")[0]

        for full in (
            "/status",
            "/latest",
            "/check",
            "/test",
            "/help"
        ):
            if full.startswith(cmd) and len(cmd) >= 4:
                cmd = full
                break

        # IMPORTANT:
        # Every command response uses `chat`.
        # Therefore the response goes back to the
        # person who issued the command.

        if cmd == "/status":

            send(
                f"Probe OK. Tracking {ctx['tracked']} items. "
                f"Last check: {ctx['checked_at']}. "
                f"New this run: {ctx['new']}. "
                f"State: "
                f"{'BLIND' if ctx['blind'] else 'watching'}.",
                chat
            )

        elif cmd == "/latest":

            top = ctx.get("top", [])[:5]

            if not top:
                send(
                    "No items cached yet.",
                    chat
                )

            else:
                lines = [
                    f"{i+1}. "
                    f"{x['title'][:120]} "
                    f"({x['date']})"
                    for i, x in enumerate(top)
                ]

                send(
                    "Latest tracked items:\n\n"
                    + "\n\n".join(lines),
                    chat
                )

        elif cmd == "/check":

            send(
                f"Checked just now "
                f"({ctx['checked_at']}). "
                f"New this run: {ctx['new']}. "
                f"{'All quiet.' if not ctx['new'] else 'Alerts sent above.'}",
                chat
            )

        elif cmd == "/test":

            send(
                "Probe test: channel live.",
                chat
            )

        else:

            send(
                "Commands: /status /latest /check /test",
                chat
            )


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

        ok = send(
            "Probe test: monitoring channel is live. "
            "You will get an alert here on any new item "
            "or any failure."
        )

        print(
            "test sent"
            if ok
            else "test dry-run (secrets missing)"
        )

        return

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
            f"({e}). Check secrets. Logs: {run_url()}"
        )

        sys.exit(1)

    items, errors = [], []

    for u in urls:

        try:
            items += parse(get(u))

        except Exception as e:
            errors.append(f"{e}")

    hot_items = []

    for u in hurls:

        try:

            hot_items += [
                x
                for x in parse(get(u))
                if relevant(x["title"])
            ]

        except Exception as e:
            errors.append(f"extra: {e}")

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

    if not items:

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

            send(
                f"PROBE FAILURE: all sources unreachable "
                f"({'; '.join(errors)[:200]}). "
                f"You are currently BLIND - retrying every 5 min. "
                f"Logs: {run_url()}"
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

    new = [
        x
        for x in items
        if x["h"] not in st
    ]

    seen_ids = set(st)

    uniq = [
        x
        for x in new
        if x["h"] not in seen_ids
        and not seen_ids.add(x["h"])
    ]

    for x in items + hot_items:
        st[x["h"]] = x["date"]

    json.dump(
        st,
        open(STATE, "w")
    )

    hnew = [
        x
        for x in hot_items
        if x["h"] not in seen_ids
    ]

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

    if first:

        send(
            f"Probe started. Tracking "
            f"{len(items)} existing items. "
            f"Alerts will arrive here on any new item."
        )

        ST.update(st)

        answer_commands(ctx)

        json.dump(
            ST,
            open(STATE, "w")
        )

        return

    if was_failing:

        send(
            "Probe recovered: sources reachable again."
        )

    ctx.update({
        "tracked":
            len([
                k for k in st
                if not k.startswith("_")
            ]),

        "new": len(uniq),

        "blind": False,

        "top":
            sorted(
                items,
                key=lambda z:
                    z.get("date", ""),
                reverse=True
            )
    })

    ST = st

    for x in uniq:

        is_hot = hot_match(
            x["title"]
        )

        flag = (
            "\n*** FINAL-RESULT keywords matched "
            "- CHECK IMMEDIATELY ***"
            if is_hot
            else ""
        )

        msg = (
            f"NEW ITEM ALERT{flag}\n\n"
            f"Title: {x['title']}\n"
            f"Published: {x['date']}\n"
            f"Details: {x['link']}"
        )

        if x["pdf"]:
            msg += f"\nPDF: {x['pdf']}"

        # Automatic alert → TG_CHAT
        send(msg)

        if x["pdf"]:

            tag = (
                "FINAL notice"
                if is_hot
                else "Notice"
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

    # Commands → whoever sent the command
    answer_commands(ctx)

    json.dump(
        ST,
        open(STATE, "w")
    )


if __name__ == "__main__":
    main()
