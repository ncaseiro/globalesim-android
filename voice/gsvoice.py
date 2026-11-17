#!/usr/bin/env python3
"""GlobaleSIM Voice — customer accounts, minutes, safety limits and provisioning API.

One file, Python standard library only. Used two ways:
  * AGI from Asterisk:   gs-agi.py out|in|charge ...   (decides if a call may happen, charges minutes)
  * HTTPS API (behind Caddy): python3 gsvoice.py api   (the website/app create customers, buy numbers, add minutes)

Data: SQLite at /var/lib/gsvoice/gsvoice.db.  Config: /etc/gsvoice/gsvoice.json.
Customer SIP logins are written to /etc/asterisk/pjsip_customers.conf (suspended customers are left out).
"""
import datetime as dt, hmac, json, math, os, re, secrets, sqlite3, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONF_PATH = os.environ.get("GSVOICE_CONF", "/etc/gsvoice/gsvoice.json")
DEFAULTS = {
    "db": "/var/lib/gsvoice/gsvoice.db",
    "pjsip_customers": "/etc/asterisk/pjsip_customers.conf",
    "api_listen": "127.0.0.1", "api_port": 8090, "api_token": "",
    "sip_domain": "voice.globalesim.tel",
    "company_did": "", "test_user": "",
    "telnyx_api_key": "", "telnyx_connection_id": "", "telnyx_api_base": "https://api.telnyx.com/v2",
    "plans": {"num300": {"name": "GlobaleSIM Number 300", "minutes": 300}, "num1000": {"name": "GlobaleSIM Number 1000", "minutes": 1000}},
    "topup_minutes": 300,
    "limits": {"new_calls_per_10min": 5, "calls_per_day": 100, "new_account_days": 7, "new_account_minutes": 100,
               "robocall_window_min": 30, "robocall_calls": 8, "robocall_short_sec": 15, "robocall_short_calls": 6},
    "reload_cmd": ["asterisk", "-rx", "module reload res_pjsip.so"],
    # receive-only texting: Telnyx → webhook /api/sms/in/<sms_hook_token> → Asterisk (AMI MessageSend) → the app
    "sms_hook_token": "", "messaging_profile_id": "",
    "ami_host": "127.0.0.1", "ami_port": 5038, "ami_user": "gsvoice", "ami_secret": "",
}

def conf():
    c = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONF_PATH) as f: user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(c.get(k), dict): c[k].update(v)
            else: c[k] = v
    except FileNotFoundError: pass
    return c

C = conf()
_lock = threading.Lock()

def now(): return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
def iso(t): return t.strftime("%Y-%m-%d %H:%M:%S")
def digits(s): return re.sub(r"\D", "", s or "")

def db():
    os.makedirs(os.path.dirname(C["db"]), exist_ok=True)
    con = sqlite3.connect(C["db"], timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS customers (
        sip_user TEXT PRIMARY KEY, sip_pass TEXT NOT NULL, email TEXT NOT NULL, did TEXT, number_id TEXT,
        plan TEXT NOT NULL, period_anchor TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL, note TEXT);
    CREATE TABLE IF NOT EXISTS topups (id INTEGER PRIMARY KEY, sip_user TEXT, minutes INTEGER, period_start TEXT, created_at TEXT, ref TEXT);
    CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, sip_user TEXT, direction TEXT, peer TEXT,
        started_at TEXT, billsec INTEGER, minutes INTEGER DEFAULT 0, result TEXT);
    CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, sip_user TEXT, ts TEXT, kind TEXT, detail TEXT);
    CREATE INDEX IF NOT EXISTS calls_user_time ON calls(sip_user, started_at);
    CREATE TABLE IF NOT EXISTS texts (id INTEGER PRIMARY KEY, sip_user TEXT, did TEXT, peer TEXT, body TEXT, media INTEGER DEFAULT 0,
        received_at TEXT, delivered INTEGER DEFAULT 0, tries INTEGER DEFAULT 0, tx_id TEXT UNIQUE);
    CREATE INDEX IF NOT EXISTS texts_user ON texts(sip_user, id);
    CREATE UNIQUE INDEX IF NOT EXISTS customers_did ON customers(did) WHERE did IS NOT NULL AND did <> '';
    """)
    cols = {r[1] for r in con.execute("PRAGMA table_info(customers)")}
    for col, typ in (("phone_id", "TEXT"), ("e911", "INTEGER DEFAULT 0"), ("e911_address_id", "TEXT"), ("e911_json", "TEXT"),
                     ("paused_at", "TEXT"), ("sub_ref", "TEXT")):
        if col not in cols: con.execute(f"ALTER TABLE customers ADD COLUMN {col} {typ}")
    return con

# ---------------------------------------------------------------- minutes & limits
def period_start(anchor_iso, t=None):
    """Monthly period that started on the anchor's day-of-month (clamped to month length)."""
    t = t or now()
    a = dt.date.fromisoformat(anchor_iso[:10])
    y, m = t.year, t.month
    def mk(y, m):
        import calendar
        return dt.date(y, m, min(a.day, calendar.monthrange(y, m)[1]))
    s = mk(y, m)
    if s > t.date():
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        s = mk(y, m)
    return max(s, a)

def usage(con, cust, t=None):
    t = t or now()
    ps = period_start(cust["period_anchor"], t).isoformat()
    used = con.execute("SELECT COALESCE(SUM(minutes),0) FROM calls WHERE sip_user=? AND started_at>=?", (cust["sip_user"], ps)).fetchone()[0]
    extra = con.execute("SELECT COALESCE(SUM(minutes),0) FROM topups WHERE sip_user=? AND period_start=?", (cust["sip_user"], ps)).fetchone()[0]
    plan = C["plans"].get(cust["plan"], {"minutes": 0})
    included = plan["minutes"]
    L = C["limits"]
    created = dt.datetime.fromisoformat(cust["created_at"]).replace(tzinfo=dt.timezone.utc)
    new_account = (t - created).days < L["new_account_days"]
    if new_account: included = min(included, L["new_account_minutes"])
    return {"period_start": ps, "included": included, "extra": extra, "used": used,
            "remaining": max(0, included + extra - used), "new_account_limit": new_account}

def event(con, user, kind, detail=""):
    con.execute("INSERT INTO events (sip_user, ts, kind, detail) VALUES (?,?,?,?)", (user, iso(now()), kind, detail))

def check_outbound(con, cust, dest):
    """Returns (ok, reason, max_seconds)."""
    if cust is None: return False, "unknown", 0
    if cust["status"] != "active": return False, "suspended", 0
    if not cust["did"]: return False, "no_number", 0
    u = usage(con, cust)
    if u["remaining"] <= 0: return False, "no_minutes", 0
    L = C["limits"]; t = now()
    n10 = con.execute("SELECT COUNT(*) FROM calls WHERE sip_user=? AND direction='out' AND started_at>=?", (cust["sip_user"], iso(t - dt.timedelta(minutes=10)))).fetchone()[0]
    if n10 >= L["new_calls_per_10min"]: return False, "rate_10min", 0
    nday = con.execute("SELECT COUNT(*) FROM calls WHERE sip_user=? AND direction='out' AND started_at>=?", (cust["sip_user"], iso(t - dt.timedelta(days=1)))).fetchone()[0]
    if nday >= L["calls_per_day"]: return False, "rate_day", 0
    return True, "ok", u["remaining"] * 60

def robocall_check(con, cust):
    L = C["limits"]
    since = iso(now() - dt.timedelta(minutes=L["robocall_window_min"]))
    rows = con.execute("SELECT peer, billsec FROM calls WHERE sip_user=? AND direction='out' AND started_at>=?", (cust["sip_user"], since)).fetchall()
    short = [r for r in rows if (r["billsec"] or 0) < L["robocall_short_sec"]]
    if len(rows) >= L["robocall_calls"] and len(short) >= L["robocall_short_calls"] and len({r["peer"] for r in rows}) >= L["robocall_short_calls"]:
        con.execute("UPDATE customers SET status='suspended', note=? WHERE sip_user=?", ("auto: robocall pattern " + iso(now()), cust["sip_user"]))
        event(con, cust["sip_user"], "auto_suspend", f"{len(rows)} calls / {len(short)} short in {L['robocall_window_min']} min")
        return True
    return False

# ---------------------------------------------------------------- Asterisk config
def write_pjsip(con):
    out = ["; generated by gsvoice.py — do not edit by hand"]
    for c in con.execute("SELECT * FROM customers WHERE status='active' ORDER BY sip_user"):
        u = c["sip_user"]
        out.append(f"""
[{u}]
type=endpoint
context=from-customer
message_context=gs-messages
disallow=all
allow=ulaw,alaw,g722
auth={u}-auth
aors={u}
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
dtmf_mode=rfc4733
device_state_busy_at=1
media_encryption=sdes
media_encryption_optimistic=no
callerid=<{c['did'] or '0'}>

[{u}-auth]
type=auth
auth_type=userpass
username={u}
password={c['sip_pass']}

[{u}]
type=aor
max_contacts=2
remove_existing=yes
qualify_frequency=60""")
    path = C["pjsip_customers"]
    tmp = path + ".tmp"
    with open(tmp, "w") as f: f.write("\n".join(out) + "\n")
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)
    try: subprocess.run(C["reload_cmd"], timeout=15, capture_output=True)
    except Exception: pass

# ---------------------------------------------------------------- Telnyx
def telnyx(method, path, body=None):
    if not C["telnyx_api_key"]: raise ApiError(503, "telnyx_api_key not configured")
    req = urllib.request.Request(C["telnyx_api_base"].rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + C["telnyx_api_key"], "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise ApiError(502, "telnyx: " + e.read().decode(errors="replace")[:400])

def telnyx_search(area_code="", contains="", limit=10):
    params = {"filter[country_code]": "US", "filter[features][]": "voice", "filter[limit]": str(limit)}
    if area_code: params["filter[national_destination_code]"] = digits(area_code)[:3]
    if contains: params["filter[phone_number][contains]"] = digits(contains)
    r = telnyx("GET", "/available_phone_numbers?" + urllib.parse.urlencode(params))
    return [n.get("phone_number") for n in r.get("data", []) if n.get("phone_number")]

def telnyx_phone_id(num):
    for _ in range(12):                       # number orders finish in a few seconds
        r = telnyx("GET", "/phone_numbers?" + urllib.parse.urlencode({"filter[phone_number]": "+" + num}))
        d = r.get("data") or []
        if d and d[0].get("id"): return d[0]["id"]
        time.sleep(2)
    return ""

def telnyx_buy(num, area_code="", taken=()):
    """Buy num (11 digits); if it's gone, take another from the same area code. Returns (num, phone_id)."""
    tried, candidates = set(), [num] if num else []
    if not candidates or area_code:
        try: candidates += [digits(x) for x in telnyx_search(area_code or num[1:4], limit=5)]
        except ApiError: pass
    for n in candidates:
        if not n or n in tried or n in taken: continue
        tried.add(n)
        try:
            r = telnyx("POST", "/number_orders", {"phone_numbers": [{"phone_number": "+" + n}], "connection_id": C["telnyx_connection_id"]})
        except ApiError:
            continue
        if ((r.get("data") or {}).get("status") or "") in ("failure", "failed"): continue
        pid = telnyx_phone_id(n)
        if pid: return n, pid
    raise ApiError(409, "no number available in that area code — try another")

def telnyx_e911(phone_id, a):
    """a = {name, street, unit, city, state, zip}. Returns address id (911 enabled) or raises ApiError."""
    first, _, last = (a.get("name") or "").strip().partition(" ")
    r = telnyx("POST", "/addresses", {"first_name": first or "Customer", "last_name": last or first or "Customer",
                                       "business_name": a.get("name") or "GlobaleSIM customer", "street_address": a.get("street", ""),
                                       "extended_address": a.get("unit", ""), "locality": a.get("city", ""), "administrative_area": a.get("state", ""),
                                       "postal_code": a.get("zip", ""), "country_code": "US"})
    aid = (r.get("data") or {}).get("id")
    if not aid: raise ApiError(502, "telnyx: address not created")
    telnyx("POST", f"/phone_numbers/{phone_id}/actions/enable_emergency", {"emergency_enabled": True, "emergency_address_id": str(aid)})
    return str(aid)

def release_number(con, c, why):
    if c["phone_id"] and C["telnyx_api_key"]:
        try: telnyx("DELETE", f"/phone_numbers/{c['phone_id']}")
        except ApiError as e: event(con, c["sip_user"], "release_failed", e.msg[:200]); return False
    con.execute("UPDATE customers SET did=NULL, phone_id=NULL, e911=0, status='canceled', note=? WHERE sip_user=?", (why, c["sip_user"]))
    event(con, c["sip_user"], "released", f"{c['did']} {why}")
    return True

def daily_main():
    """Cron: release numbers of lines unpaid for more than 30 days."""
    with _lock, db() as con:
        cutoff = iso(now() - dt.timedelta(days=30))
        for c in con.execute("SELECT * FROM customers WHERE status='suspended' AND note LIKE 'unpaid%' AND paused_at IS NOT NULL AND paused_at < ?", (cutoff,)).fetchall():
            release_number(con, c, "released after 30 days unpaid")
        con.commit(); write_pjsip(con)

# ---------------------------------------------------------------- texts (receive only)
import base64, socket

def ami_message(sip_user, peer, body):
    """Hand a text to the customer's app through Asterisk (SIP MESSAGE). True if Asterisk accepted it."""
    if not C["ami_secret"]: return False
    try:
        with socket.create_connection((C["ami_host"], int(C["ami_port"])), timeout=5) as sk:
            f = sk.makefile("rwb")
            f.readline()                                           # banner
            def act(lines):
                f.write(("".join(f"{k}: {v}\r\n" for k, v in lines) + "\r\n").encode()); f.flush()
                resp = {}
                while True:
                    ln = f.readline().decode(errors="replace").strip()
                    if not ln: break
                    k, _, v = ln.partition(":"); resp[k.strip()] = v.strip()
                return resp
            if act([("Action", "Login"), ("Username", C["ami_user"]), ("Secret", C["ami_secret"]), ("Events", "off")]).get("Response") != "Success": return False
            frm = f'"{fmt_num(peer)}" <sip:+{peer}@{C["sip_domain"]}>' if peer else f'<sip:unknown@{C["sip_domain"]}>'
            r = act([("Action", "MessageSend"), ("To", f"pjsip:{sip_user}"), ("From", frm),
                     ("Base64Body", base64.b64encode(body.encode()).decode())])
            act([("Action", "Logoff")])
            return r.get("Response") == "Success"
    except OSError:
        return False

def fmt_num(d):
    d = digits(d)
    if len(d) == 11 and d[0] == "1": d = d[1:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else d

def sms_in(payload):
    """Telnyx 'message.received' → store, then push to the app if it's online."""
    to = digits(((payload.get("to") or [{}])[0] or {}).get("phone_number", ""))
    frm = digits((payload.get("from") or {}).get("phone_number", ""))
    text = (payload.get("text") or "").strip()[:1600]
    media = len(payload.get("media") or [])
    if media and not text: text = "[photo or file — not shown]"
    elif media: text += "\n[photo or file — not shown]"
    with _lock, db() as con:
        cust = con.execute("SELECT * FROM customers WHERE did=? AND status<>'canceled'", (to,)).fetchone()
        user = cust["sip_user"] if cust else (C["test_user"] if C["company_did"] and to == digits(C["company_did"]) else "")
        if not user: event(con, "", "text_unknown", f"to={to} from={frm}"); con.commit(); return
        if con.execute("SELECT COUNT(*) FROM texts WHERE sip_user=? AND received_at > ?", (user, iso(now() - dt.timedelta(days=1)))).fetchone()[0] >= 300:
            return                                                # flood guard
        try:
            cur = con.execute("INSERT INTO texts (sip_user, did, peer, body, media, received_at, tx_id) VALUES (?,?,?,?,?,?,?)",
                              (user, to, frm, text, media, iso(now()), str(payload.get("id") or secrets.token_hex(8))))
        except sqlite3.IntegrityError:
            return                                                # Telnyx retry of a text we already have
        con.commit(); tid = cur.lastrowid
        active = (cust is None) or cust["status"] == "active"
    if active and ami_message(user, frm, text):
        with _lock, db() as con: con.execute("UPDATE texts SET delivered=1 WHERE id=?", (tid,)); con.commit()

def sms_retry_loop():
    """Every minute: re-send texts the app missed while offline (last 24 h, up to 30 tries)."""
    while True:
        time.sleep(60)
        try:
            with _lock, db() as con:
                rows = con.execute("SELECT t.* FROM texts t JOIN customers c ON c.sip_user=t.sip_user WHERE t.delivered=0 AND t.tries<30 AND c.status='active' AND t.received_at > ? ORDER BY t.id LIMIT 50",
                                   (iso(now() - dt.timedelta(days=1)),)).fetchall()
                if C["test_user"]:
                    rows += con.execute("SELECT * FROM texts WHERE sip_user=? AND delivered=0 AND tries<30 AND received_at > ? LIMIT 20", (C["test_user"], iso(now() - dt.timedelta(days=1)))).fetchall()
            for t in rows:
                ok = ami_message(t["sip_user"], t["peer"], t["body"])
                with _lock, db() as con: con.execute("UPDATE texts SET delivered=?, tries=tries+1 WHERE id=?", (1 if ok else 0, t["id"])); con.commit()
        except Exception as e:
            sys.stderr.write("sms retry %r\n" % e)

def sms_attach(phone_id):
    if C["messaging_profile_id"] and phone_id:
        try: telnyx("PATCH", f"/phone_numbers/{phone_id}/messaging", {"messaging_profile_id": C["messaging_profile_id"]}); return True
        except ApiError as e: sys.stderr.write("sms attach %s: %s\n" % (phone_id, e.msg))
    return False

def sms_setup(conf_path):
    """Installer step: create/refresh the Telnyx messaging profile (receive only) and attach every number."""
    c = json.load(open(conf_path))
    if not c.get("sms_hook_token"): c["sms_hook_token"] = secrets.token_urlsafe(24)
    hook = f"https://{C['sip_domain']}/api/sms/in/{c['sms_hook_token']}"
    C["sms_hook_token"] = c["sms_hook_token"]
    body = {"webhook_url": hook, "webhook_api_version": "2", "enabled": True}
    pid = c.get("messaging_profile_id", "")
    try:
        if pid: telnyx("PATCH", f"/messaging_profiles/{pid}", body)
        else:
            r = telnyx("POST", "/messaging_profiles", dict(body, name="GlobaleSIM Phone (receive only)", whitelisted_destinations=["US"]))
            pid = (r.get("data") or {}).get("id", "")
    except ApiError as e:
        print("  texting: Telnyx messaging profile failed:", e.msg); json.dump(c, open(conf_path, "w"), indent=2); return
    c["messaging_profile_id"] = pid; C["messaging_profile_id"] = pid
    json.dump(c, open(conf_path, "w"), indent=2)
    n = 0
    with db() as con:
        nums = [(r["phone_id"], r["did"]) for r in con.execute("SELECT phone_id, did FROM customers WHERE did IS NOT NULL AND did<>''")]
    if C["company_did"]: nums.append(("", digits(C["company_did"])))
    for pidn, did in nums:
        if not pidn:
            try: pidn = ((telnyx("GET", "/phone_numbers?" + urllib.parse.urlencode({"filter[phone_number]": "+" + did})).get("data") or [{}])[0] or {}).get("id", "")
            except ApiError: pidn = ""
        n += sms_attach(pidn)
    print(f"  texting: receive-only profile {pid}, {n} number(s) attached")

# ---------------------------------------------------------------- AGI
class Agi:
    def __init__(self):
        self.env = {}
        for line in sys.stdin:
            line = line.rstrip("\n")
            if not line: break
            k, _, v = line.partition(":"); self.env[k.strip()] = v.strip()
    def cmd(self, c):
        sys.stdout.write(c + "\n"); sys.stdout.flush()
        return sys.stdin.readline().strip()
    def set(self, k, v): self.cmd(f'SET VARIABLE {k} "{v}"')

def agi_main(args):
    a = Agi()
    mode = args[0] if args else ""
    with _lock, db() as con:
        if mode == "out":                 # out <endpoint> <dest>
            user, dest = args[1], digits(args[2])
            cust = con.execute("SELECT * FROM customers WHERE sip_user=?", (user,)).fetchone()
            ok, reason, maxsec = check_outbound(con, cust, dest)
            a.set("GS_OK", "1" if ok else "0"); a.set("GS_REASON", reason)
            if ok:
                cur = con.execute("INSERT INTO calls (sip_user, direction, peer, started_at, billsec, result) VALUES (?,?,?,?,0,'dialing')", (user, "out", dest, iso(now())))
                a.set("GS_CALLID", str(cur.lastrowid)); a.set("GS_CID", "+" + cust["did"]); a.set("GS_MAXMS", str(int(maxsec * 1000)))
            else:
                event(con, user, "blocked_out", f"{reason} {dest}")
        elif mode == "e911":              # e911 <endpoint>
            cust = con.execute("SELECT * FROM customers WHERE sip_user=?", (args[1],)).fetchone()
            ok = bool(cust and cust["e911"] and cust["did"])
            a.set("GS_OK", "1" if ok else "0")
            if ok: a.set("GS_CID", "+" + cust["did"])
            event(con, args[1], "e911_call", "routed" if ok else "no 911 address")
        elif mode == "in":                # in <exten> <to-header> <caller>
            # the dialed number = user part of the To URI (never the host/port digits), else the extension
            def num(x):
                m = re.search(r"sip:\+?(\d{7,15})@", x or ""); d = m.group(1) if m else (digits(x) if len(digits(x or "")) <= 15 else "")
                return ("1" + d) if len(d) == 10 else d
            to_raw = args[2] if len(args) > 2 else ""; caller = digits(args[3]) if len(args) > 3 else ""
            did = num(to_raw) or num(args[1]) or num("sip:" + args[1] + "@")
            cust = con.execute("SELECT * FROM customers WHERE did=? AND status='active'", (did,)).fetchone()
            if not cust and did:
                other = con.execute("SELECT * FROM customers WHERE did=?", (did,)).fetchone()
                if other: event(con, other["sip_user"], "blocked_in", f"line {other['status']} " + caller)
            if cust:
                u = usage(con, cust)
                if u["remaining"] <= 0:
                    a.set("GS_OK", "0"); a.set("GS_REASON", "no_minutes"); event(con, cust["sip_user"], "blocked_in", "no_minutes " + caller)
                else:
                    cur = con.execute("INSERT INTO calls (sip_user, direction, peer, started_at, billsec, result) VALUES (?,?,?,?,0,'ringing')", (cust["sip_user"], "in", caller, iso(now())))
                    a.set("GS_OK", "1"); a.set("GS_USER", cust["sip_user"]); a.set("GS_CALLID", str(cur.lastrowid)); a.set("GS_MAXMS", str(u["remaining"] * 60000))
            elif C["test_user"] and (not C["company_did"] or did == digits(C["company_did"]) or not did):
                a.set("GS_OK", "1"); a.set("GS_USER", C["test_user"]); a.set("GS_CALLID", "0"); a.set("GS_MAXMS", str(4 * 3600 * 1000))
            else:
                a.set("GS_OK", "0"); a.set("GS_REASON", "unknown_number")
                if not con.execute("SELECT 1 FROM customers WHERE did=?", (did,)).fetchone():
                    event(con, "", "unknown_in", f"to={to_raw[:120]} exten={args[1]} caller={caller}")
        elif mode == "charge":            # charge <callid> <answered-seconds>
            cid = int(args[1] or 0); secs = int(float(args[2] or 0)) if len(args) > 2 and args[2] else 0
            if cid:
                mins = math.ceil(secs / 60) if secs > 0 else 0
                con.execute("UPDATE calls SET billsec=?, minutes=?, result=? WHERE id=?", (secs, mins, "answered" if secs > 0 else "no_answer", cid))
                row = con.execute("SELECT c.* FROM customers c JOIN calls k ON k.sip_user=c.sip_user WHERE k.id=? AND k.direction='out'", (cid,)).fetchone()
                if row and row["status"] == "active" and robocall_check(con, row):
                    con.commit(); write_pjsip(con)
        con.commit()

# ---------------------------------------------------------------- API
class ApiError(Exception):
    def __init__(self, code, msg): super().__init__(msg); self.code = code; self.msg = msg

def cust_json(con, c):
    d = {k: c[k] for k in c.keys() if k != "sip_pass"}
    d["usage"] = usage(con, c); d["plan_name"] = C["plans"].get(c["plan"], {}).get("name", c["plan"])
    return d

def api_route(method, path, q, body):
    p = [x for x in path.split("/") if x]
    if p[:1] != ["api"]: raise ApiError(404, "not found")
    p = p[1:]
    with _lock, db() as con:
        if p == ["health"]:
            return {"ok": True, "customers": con.execute("SELECT COUNT(*) FROM customers").fetchone()[0]}
        if p == ["numbers", "search"] and method == "GET":
            params = {"filter[country_code]": "US", "filter[features][]": "voice", "filter[limit]": q.get("limit", "10")}
            if q.get("area_code"): params["filter[national_destination_code]"] = digits(q["area_code"])[:3]
            if q.get("contains"): params["filter[phone_number][contains]"] = digits(q["contains"])
            if q.get("ends_with"): params["filter[phone_number][ends_with]"] = digits(q["ends_with"])
            r = telnyx("GET", "/available_phone_numbers?" + urllib.parse.urlencode(params))
            return {"numbers": [{"phone_number": n.get("phone_number"), "region": ", ".join(x.get("region_name", "") for x in n.get("region_information", []) if x.get("region_type") in ("rate_center", "state")),
                                 "monthly": (n.get("cost_information") or {}).get("monthly_cost")} for n in r.get("data", [])]}
        if p == ["customers"] and method == "GET":
            where, args = [], []
            if q.get("status"): where.append("status=?"); args.append(q["status"])
            if q.get("email"): where.append("email=?"); args.append(q["email"].strip().lower())
            rows = con.execute("SELECT * FROM customers" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT 500", args).fetchall()
            return {"customers": [cust_json(con, c) for c in rows]}
        if p == ["customers"] and method == "POST":
            email = (body.get("email") or "").strip().lower(); plan = body.get("plan", "num300")
            if "@" not in email: raise ApiError(400, "email required")
            if plan not in C["plans"]: raise ApiError(400, "unknown plan")
            user = "c" + secrets.token_hex(5); pw = secrets.token_urlsafe(18)
            con.execute("INSERT INTO customers (sip_user, sip_pass, email, plan, period_anchor, created_at) VALUES (?,?,?,?,?,?)",
                        (user, pw, email, plan, now().date().isoformat(), iso(now())))
            event(con, user, "created", plan); con.commit(); write_pjsip(con)
            c = con.execute("SELECT * FROM customers WHERE sip_user=?", (user,)).fetchone()
            return dict(cust_json(con, c), sip_pass=pw, sip_server=C["sip_domain"])
        if p == ["provision"] and method == "POST":
            # one step after payment: customer (by email) + number (chosen, or same area code) + plan + 911 address
            email = (body.get("email") or "").strip().lower(); plan = body.get("plan", "num300"); ref = str(body.get("ref", ""))[:80]
            if "@" not in email or plan not in C["plans"]: raise ApiError(400, "email and plan required")
            if ref:
                done = con.execute("SELECT * FROM customers WHERE sub_ref=?", (ref,)).fetchone()
                if done and done["did"]: return dict(cust_json(con, done), already=True)
            c = con.execute("SELECT * FROM customers WHERE email=? AND status<>'canceled' ORDER BY created_at DESC LIMIT 1", (email,)).fetchone()
            if c and c["did"]:
                if not (c["status"] == "suspended" and (c["note"] or "").startswith("unpaid")): raise ApiError(409, "this account already has a line")
                # restarted subscription on a paused line: same number, fresh month
                con.execute("UPDATE customers SET plan=?, period_anchor=?, sub_ref=?, status='active', note=NULL, paused_at=NULL WHERE sip_user=?",
                            (plan, now().date().isoformat(), ref or None, c["sip_user"]))
                event(con, c["sip_user"], "restarted", plan); con.commit(); write_pjsip(con)
                c = con.execute("SELECT * FROM customers WHERE sip_user=?", (c["sip_user"],)).fetchone()
                return dict(cust_json(con, c), picked=True)
            if not c:
                user = "c" + secrets.token_hex(5)
                con.execute("INSERT INTO customers (sip_user, sip_pass, email, plan, period_anchor, created_at) VALUES (?,?,?,?,?,?)",
                            (user, secrets.token_urlsafe(18), email, plan, now().date().isoformat(), iso(now())))
                event(con, user, "created", plan)
                c = con.execute("SELECT * FROM customers WHERE sip_user=?", (user,)).fetchone()
            u = c["sip_user"]
            con.execute("UPDATE customers SET plan=?, period_anchor=?, sub_ref=?, status='active', note=NULL, paused_at=NULL WHERE sip_user=?",
                        (plan, now().date().isoformat(), ref or None, u))
            con.commit()
            want = digits(body.get("phone_number")); want = ("1" + want) if len(want) == 10 else want
            taken = {r[0] for r in con.execute("SELECT did FROM customers WHERE did IS NOT NULL")}
            num, pid = telnyx_buy(want, digits(body.get("area_code")), taken)
            con.execute("UPDATE customers SET did=?, phone_id=?, number_id=? WHERE sip_user=?", (num, pid, pid, u))
            sms_attach(pid)                                   # receive texts on the new number
            event(con, u, "number", num + (" (picked)" if num == want else f" (wanted {want or 'any'})"))
            a911 = body.get("e911") or {}
            if a911.get("street") and a911.get("zip"):
                con.execute("UPDATE customers SET e911_json=? WHERE sip_user=?", (json.dumps(a911), u))
                try:
                    aid = telnyx_e911(pid, a911)
                    con.execute("UPDATE customers SET e911=1, e911_address_id=? WHERE sip_user=?", (aid, u))
                except ApiError as e:
                    event(con, u, "e911_failed", e.msg[:200])
            con.commit(); write_pjsip(con)
            c = con.execute("SELECT * FROM customers WHERE sip_user=?", (u,)).fetchone()
            return dict(cust_json(con, c), picked=(num == want))
        if p == ["alerts"]:
            return {"events": [dict(r) for r in con.execute("SELECT * FROM events WHERE kind IN ('auto_suspend','blocked_out','blocked_in','unknown_in','text_unknown','e911_failed','release_failed','released') ORDER BY id DESC LIMIT 200")]}
        if len(p) >= 2 and p[0] == "customers":
            c = con.execute("SELECT * FROM customers WHERE sip_user=?", (p[1],)).fetchone()
            if not c: raise ApiError(404, "no such customer")
            u = c["sip_user"]; sub = p[2] if len(p) > 2 else ""
            if sub == "" and method == "GET":
                d = cust_json(con, c)
                d["recent_calls"] = [dict(r) for r in con.execute("SELECT direction, peer, started_at, billsec, minutes, result FROM calls WHERE sip_user=? ORDER BY id DESC LIMIT 50", (u,))]
                d["texts"] = [dict(r) for r in con.execute("SELECT id, peer, body, received_at FROM texts WHERE sip_user=? ORDER BY id DESC LIMIT 50", (u,))]
                return d
            if sub == "credentials" and method == "GET":     # for the app, after the website verified the signed-in owner
                return {"sip_user": u, "sip_pass": c["sip_pass"], "sip_server": C["sip_domain"], "did": c["did"]}
            if sub == "number" and method == "POST":
                if c["did"]: raise ApiError(409, "customer already has a number")
                num = digits(body.get("phone_number"))
                if len(num) == 10: num = "1" + num
                if not re.fullmatch(r"1\d{10}", num): raise ApiError(400, "phone_number must be a US number")
                nid = ""
                if body.get("buy", True):
                    r = telnyx("POST", "/number_orders", {"phone_numbers": [{"phone_number": "+" + num}], "connection_id": C["telnyx_connection_id"]})
                    nid = ((r.get("data") or {}).get("id")) or ""
                con.execute("UPDATE customers SET did=?, number_id=? WHERE sip_user=?", (num, nid, u))
                event(con, u, "number", num); con.commit(); write_pjsip(con)
            elif sub == "plan" and method == "POST":           # new/renewed paid month
                plan = body.get("plan", c["plan"])
                if plan not in C["plans"]: raise ApiError(400, "unknown plan")
                # renewal paid: fresh minutes; a line paused for non-payment comes back on (admin/robocall pauses stay)
                con.execute("UPDATE customers SET plan=?, period_anchor=?, status=CASE WHEN status='suspended' AND note LIKE 'unpaid%' THEN 'active' ELSE status END, "
                            "paused_at=CASE WHEN status='suspended' AND note LIKE 'unpaid%' THEN NULL ELSE paused_at END, "
                            "note=CASE WHEN status='suspended' AND note LIKE 'unpaid%' THEN NULL ELSE note END WHERE sip_user=?", (plan, now().date().isoformat(), u))
                event(con, u, "plan", plan); con.commit(); write_pjsip(con)
            elif sub == "topup" and method == "POST":
                mins = int(body.get("minutes", C["topup_minutes"])); tref = str(body.get("ref", ""))[:80]
                if tref and con.execute("SELECT 1 FROM topups WHERE ref=?", (tref,)).fetchone(): return cust_json(con, c)
                if not 0 < mins <= 5000: raise ApiError(400, "bad minutes")
                con.execute("INSERT INTO topups (sip_user, minutes, period_start, created_at, ref) VALUES (?,?,?,?,?)",
                            (u, mins, usage(con, c)["period_start"], iso(now()), str(body.get("ref", ""))[:80]))
                event(con, u, "topup", str(mins))
            elif sub == "release" and method == "POST":
                release_number(con, c, str(body.get("note", "released by admin"))[:200]); con.commit(); write_pjsip(con)
            elif sub == "status" and method == "POST":
                st = body.get("status")
                if st not in ("active", "suspended", "canceled"): raise ApiError(400, "bad status")
                con.execute("UPDATE customers SET status=?, note=?, paused_at=? WHERE sip_user=?",
                            (st, str(body.get("note", ""))[:200], iso(now()) if st == "suspended" else None, u))
                event(con, u, "status", st); con.commit(); write_pjsip(con)
            else:
                raise ApiError(404, "not found")
            con.commit()
            return cust_json(con, con.execute("SELECT * FROM customers WHERE sip_user=?", (u,)).fetchone())
    raise ApiError(404, "not found")

class Handler(BaseHTTPRequestHandler):
    server_version = "gsvoice"
    def log_message(self, fmt, *a): sys.stderr.write("api %s %s\n" % (self.address_string(), fmt % a))
    def _go(self, method):
        try:
            if method == "POST" and self.path.startswith("/api/sms/in/"):   # Telnyx webhook (secret in the URL)
                n = int(self.headers.get("Content-Length") or 0); raw = self.rfile.read(min(n, 200000)) if n else b"{}"
                tokp = self.path.rsplit("/", 1)[-1].split("?")[0]
                if C["sms_hook_token"] and hmac.compare_digest(tokp, C["sms_hook_token"]):
                    try:
                        d = (json.loads(raw or b"{}").get("data") or {})
                        if d.get("event_type") == "message.received": sms_in(d.get("payload") or {})
                    except Exception as e: sys.stderr.write("sms in %r\n" % e)
                    res, code = {"ok": True}, 200
                else: res, code = {"error": "not found"}, 404
                b = json.dumps(res).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
                return
            tok = self.headers.get("Authorization", "")
            if not C["api_token"] or not hmac.compare_digest(tok, "Bearer " + C["api_token"]): raise ApiError(401, "unauthorized")
            u = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            res, code = api_route(method, u.path, q, body), 200
        except ApiError as e: res, code = {"error": e.msg}, e.code
        except (ValueError, KeyError) as e: res, code = {"error": "bad request: " + str(e)}, 400
        except Exception as e: res, code = {"error": "server error"}, 500; sys.stderr.write("api error %r\n" % e)
        b = json.dumps(res, default=str).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self): self._go("GET")
    def do_POST(self): self._go("POST")

def api_main():
    with db() as con: write_pjsip(con)
    threading.Thread(target=sms_retry_loop, daemon=True).start()
    ThreadingHTTPServer((C["api_listen"], int(C["api_port"])), Handler).serve_forever()

if __name__ == "__main__":
    if sys.argv[1:2] == ["api"]: api_main()
    elif sys.argv[1:2] == ["agi"]: agi_main(sys.argv[2:])
    elif sys.argv[1:2] == ["daily"]: daily_main()
    elif sys.argv[1:2] == ["sms-setup"]: sms_setup(os.environ.get("GSVOICE_CONF", "/etc/gsvoice/gsvoice.json"))
    elif sys.argv[1:2] == ["regen"]:
        with db() as con: write_pjsip(con)
    else: print(__doc__)
