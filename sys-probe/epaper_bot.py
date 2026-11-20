"""Subscribers, telemetry and commands for the e-paper bot.

The subscriber file is encrypted with PROBE_KEY because this repository is
public; without the key the file is written plaintext and the workflows do
not commit it.
"""
import base64
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
USERS = os.path.join(HERE, "epaper_users.enc.json")
API = "https://api.telegram.org/bot"

HELP = (
    "/epaper - today's PDF now\n"
    "/subscribe - daily 6:00 AM Dhaka\n"
    "/unsubscribe - stop the daily PDF\n"
    "/status - what was sent last\n"
    "/stats - usage counters\n"
    "/help - this list"
)


def api(token, method, timeout=40, **params):
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(
        f"{API}{token}/{method}", data=data, headers={"User-Agent": "sys-probe"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    if not d.get("ok"):
        raise IOError(str(d.get("description"))[:120])
    return d.get("result")


def _key():
    k = os.environ.get("PROBE_KEY", "").strip()
    return hashlib.sha256(k.encode()).digest() if k else None


def load():
    try:
        raw = json.load(open(USERS, encoding="utf-8"))
    except Exception:
        return {"offset": 0, "users": {}, "totals": {}}
    if raw.get("plain"):
        db = raw.get("db") or {}
    else:
        key = _key()
        if not key:
            print("users file is encrypted but PROBE_KEY is missing")
            return {"offset": 0, "users": {}, "totals": {}}
        try:
            from Crypto.Cipher import AES

            cip = AES.new(key, AES.MODE_GCM, nonce=base64.b64decode(raw["n"]))
            db = json.loads(cip.decrypt_and_verify(base64.b64decode(raw["c"]), base64.b64decode(raw["t"])))
        except Exception as e:
            print("users load failed:", str(e)[:90])
            return {"offset": 0, "users": {}, "totals": {}}
    db.setdefault("offset", 0)
    db.setdefault("users", {})
    db.setdefault("totals", {})
    return db


def save(db):
    key = _key()
    if key:
        from Crypto.Cipher import AES

        nonce = os.urandom(12)
        cip = AES.new(key, AES.MODE_GCM, nonce=nonce)
        c, t = cip.encrypt_and_digest(json.dumps(db, ensure_ascii=False).encode())
        out = {
            "v": 1,
            "n": base64.b64encode(nonce).decode(),
            "c": base64.b64encode(c).decode(),
            "t": base64.b64encode(t).decode(),
        }
    else:
        out = {"v": 1, "plain": True, "db": db}
    tmp = USERS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, USERS)


def bump(db, key, n=1):
    t = db.setdefault("totals", {})
    t[key] = int(t.get(key) or 0) + n


def subscribers(db):
    return [
        cid
        for cid, u in (db.get("users") or {}).items()
        if u.get("sub", True) and not u.get("blocked")
    ]


def register(db, msg):
    chat = msg.get("chat") or {}
    cid = str(chat.get("id") or "")
    if not cid:
        return None, cid
    frm = msg.get("from") or {}
    users = db.setdefault("users", {})
    u = users.get(cid)
    new = u is None
    if new:
        u = {"joined": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sub": True, "cmds": {}, "seen": 0}
        users[cid] = u
        bump(db, "users")
    u["id"] = cid
    u["type"] = chat.get("type") or "private"
    for k, src in (("username", "username"), ("first_name", "first_name"), ("last_name", "last_name")):
        if frm.get(src):
            u[k] = frm[src]
    u["lang"] = frm.get("language_code") or u.get("lang") or ""
    u["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    u["seen"] = int(u.get("seen") or 0) + 1
    return u, cid


def _send(token, cid, text, send=None):
    (send or api)(token, "sendMessage", chat_id=cid, text=text[:3800], disable_web_page_preview="true")


def handle(db, token, msg, ctx, send=None):
    u, cid = register(db, msg)
    if not u:
        return
    bump(db, "messages")
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    cmd = text.split()[0].split("@")[0].lower()
    u.setdefault("cmds", {})
    u["cmds"][cmd] = int(u["cmds"].get(cmd) or 0) + 1
    bump(db, "commands")
    if cmd == "/start":
        u["sub"] = True
        bump(db, "starts")
        _send(token, cid, "Subscribed. Today's paper lands at 6:00 AM Dhaka.\n\n" + HELP, send)
    elif cmd in ("/stop", "/unsubscribe"):
        u["sub"] = False
        bump(db, "stops")
        _send(token, cid, "Unsubscribed. /subscribe any time.", send)
    elif cmd in ("/subscribe",):
        u["sub"] = True
        _send(token, cid, "Subscribed for 6:00 AM Dhaka.", send)
    elif cmd == "/epaper":
        u["sub"] = True
        bump(db, "epaper")
        _send(token, cid, "Building today's PDF, give it a minute...", send)
        if ctx.get("epaper_now"):
            ctx["epaper_now"](cid)
    elif cmd == "/status":
        _send(token, cid, ctx.get("status") or "no status", send)
    elif cmd == "/stats":
        t = db.get("totals") or {}
        lines = [
            f"subscribers: {len(subscribers(db))} of {len(db.get('users') or {})} known",
            f"starts {t.get('starts', 0)} / stops {t.get('stops', 0)}",
            f"commands {t.get('commands', 0)} / messages {t.get('messages', 0)}",
            f"pdf requests {t.get('epaper', 0)}",
        ]
        last = t.get("last_broadcast") or {}
        if last:
            lines.append(
                f"last broadcast {last.get('date')}: {last.get('sent', 0)} ok, {last.get('failed', 0)} failed"
            )
        _send(token, cid, "\n".join(lines), send)
    else:
        _send(token, cid, HELP, send)


def poll(token, db, ctx):
    """Drain getUpdates, handle commands, keep the offset in the db."""
    if not token:
        return 0
    try:
        ups = api(token, "getUpdates", offset=int(db.get("offset") or 0), timeout=2, limit=50)
    except Exception as e:
        print("poll failed:", str(e)[:80])
        return 0
    n = 0
    for upd in ups or []:
        db["offset"] = int(upd.get("update_id") or 0) + 1
        for k in ("message", "edited_message", "channel_post"):
            if upd.get(k):
                try:
                    handle(db, token, upd[k], ctx)
                    n += 1
                except Exception as e:
                    print("command failed:", str(e)[:90])
    return n


COMMANDS = [
    {"command": "epaper", "description": "Today's paper as a linked PDF"},
    {"command": "subscribe", "description": "Daily 6:00 AM Dhaka"},
    {"command": "unsubscribe", "description": "Stop the daily PDF"},
    {"command": "status", "description": "Last send and waitlist"},
    {"command": "stats", "description": "Usage counters"},
    {"command": "help", "description": "Command list"},
]


def publish_commands(token):
    if not token:
        return
    try:
        api(token, "setMyCommands", commands=json.dumps(COMMANDS))
    except Exception as e:
        print("setMyCommands failed:", str(e)[:80])
