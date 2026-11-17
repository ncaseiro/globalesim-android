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
    "plans": {"num300": {"name": "GlobaleSIM Number 300", "minutes": 300}, "num1000": {"name": "GlobaleSIM Number 1000", "minutes": 1000},
              "biz_ext": {"name": "GlobaleSIM Business extension", "minutes": 1000}},
    # business lines: busier than personal lines, still capped (per extension)
    "limits_biz": {"new_calls_per_10min": 20, "calls_per_day": 300},
    # where voicemail / alerts are reported (app.php?api=voice_hook), with a shared secret
    "notify_url": "", "notify_token": "",
    "data_dir": "/var/lib/gsvoice", "tts_cmd": "espeak-ng",
    # natural voices (Piper, runs on this server); the installer downloads the models into piper_dir/voices
    "piper_dir": "/opt/piper",
    # Master shutoff for outgoing calls (all lines together). Tripping it pauses outgoing calls only and emails you.
    "guard": {"out_minutes_hour": 900, "out_minutes_day": 6000, "out_calls_10min": 150, "concurrent": 40, "new_lines_day": 25},
    "low_balance": 20,                 # email when the Telnyx balance drops below this (USD)
    "alert_kinds": ["auto_suspend", "guard_trip", "e911_failed", "release_failed", "low_balance", "line_flood"],
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
    CREATE TABLE IF NOT EXISTS companies (id INTEGER PRIMARY KEY, name TEXT, owner_email TEXT, main_user TEXT, period_anchor TEXT,
        status TEXT DEFAULT 'active', note TEXT, created_at TEXT, ivr_json TEXT, hours_json TEXT, greeting TEXT DEFAULT '', sub_ref TEXT, paused_at TEXT,
        comp INTEGER DEFAULT 0, voice TEXT DEFAULT '', rec_main INTEGER DEFAULT 0, rec_closed INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS voicemails (id INTEGER PRIMARY KEY, sip_user TEXT, company_id INTEGER, caller TEXT, file TEXT, secs INTEGER,
        created_at TEXT, heard INTEGER DEFAULT 0);
    CREATE INDEX IF NOT EXISTS vm_user ON voicemails(sip_user, id);
    CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS customers_did ON customers(did) WHERE did IS NOT NULL AND did <> '';
    """)
    cols = {r[1] for r in con.execute("PRAGMA table_info(customers)")}
    for col, typ in (("phone_id", "TEXT"), ("e911", "INTEGER DEFAULT 0"), ("e911_address_id", "TEXT"), ("e911_json", "TEXT"),
                     ("paused_at", "TEXT"), ("sub_ref", "TEXT"), ("company_id", "INTEGER"), ("ext", "TEXT"), ("display_name", "TEXT"),
                     ("vm_on", "INTEGER DEFAULT 1"), ("web_pass", "TEXT"), ("web_exp", "TEXT"),
                     ("fwd_mode", "TEXT DEFAULT ''"), ("fwd_to", "TEXT"), ("dnd", "INTEGER DEFAULT 0")):
        if col not in cols: con.execute(f"ALTER TABLE customers ADD COLUMN {col} {typ}")
    cc = {r[1] for r in con.execute("PRAGMA table_info(companies)")}
    for col, typ in (("comp", "INTEGER DEFAULT 0"), ("voice", "TEXT DEFAULT ''"), ("rec_main", "INTEGER DEFAULT 0"), ("rec_closed", "INTEGER DEFAULT 0")):
        if col not in cc: con.execute(f"ALTER TABLE companies ADD COLUMN {col} {typ}")
    if "company_id" not in {r[1] for r in con.execute("PRAGMA table_info(topups)")}:
        con.execute("ALTER TABLE topups ADD COLUMN company_id INTEGER")
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

def company(con, cid):
    return con.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone() if cid else None

def co_of(con, cust):
    return company(con, cust["company_id"]) if cust is not None and "company_id" in cust.keys() and cust["company_id"] else None

def co_members(con, cid, active_only=False):
    return con.execute("SELECT * FROM customers WHERE company_id=? AND status " + ("='active'" if active_only else "<>'canceled'") + " ORDER BY ext", (cid,)).fetchall()

def co_main_did(con, co):
    r = con.execute("SELECT did FROM customers WHERE sip_user=?", (co["main_user"],)).fetchone() if co else None
    return r["did"] if r and r["did"] else ""

FWD_ON = {"always": "", "busy": "BUSY|CHANUNAVAIL|CONGESTION", "noanswer": "NOANSWER", "busy_noanswer": "BUSY|CHANUNAVAIL|CONGESTION|NOANSWER"}

def set_forward_vars(a, cust):
    """Do-not-disturb / call-forwarding settings of the line being called → dialplan variables."""
    a.set("GS_DND", "1" if cust["dnd"] else "0")
    mode = cust["fwd_mode"] or ""
    a.set("GS_FWD", "always" if mode == "always" and cust["fwd_to"] else "")
    a.set("GS_FWDON", FWD_ON.get(mode, "^NEVER$") if mode != "always" and cust["fwd_to"] else "^NEVER$")
    a.set("GS_FWDUSER", cust["sip_user"])

def parse_nanp(x):
    d = digits(x)
    if len(d) == 10: d = "1" + d
    return d if re.fullmatch(r"1[2-9]\d{2}[2-9]\d{6}", d) and not re.match(r"1(900|976)", d) else ""

def base_user(ep):
    return ep[:-4] if ep and ep.endswith("-web") else ep

def dials(con, users):
    """Dial string ringing each line's phone app and, if signed in, its computer browser too."""
    out = []
    for u in users:
        out.append(f"PJSIP/{u}")
        r = con.execute("SELECT web_exp FROM customers WHERE sip_user=?", (u,)).fetchone()
        if r and r["web_exp"] and r["web_exp"] > iso(now()): out.append(f"PJSIP/{u}-web")
    return "&".join(out)

def company_usage(con, co, t=None):
    """One shared pool: every extension's included minutes + the company's packs, used by everyone."""
    t = t or now()
    ps = period_start(co["period_anchor"], t).isoformat()
    members = co_members(con, co["id"])
    users = [m["sip_user"] for m in members]
    used = con.execute(f"SELECT COALESCE(SUM(minutes),0) FROM calls WHERE started_at>=? AND sip_user IN ({','.join('?' * len(users))})",
                       (ps, *users)).fetchone()[0] if users else 0
    extra = con.execute("SELECT COALESCE(SUM(minutes),0) FROM topups WHERE company_id=? AND period_start=?", (co["id"], ps)).fetchone()[0]
    included = sum(C["plans"].get(m["plan"], {"minutes": 0})["minutes"] for m in members)
    L = C["limits"]
    created = dt.datetime.fromisoformat(co["created_at"]).replace(tzinfo=dt.timezone.utc)
    new_account = (t - created).days < L["new_account_days"] and not co["comp"]     # complimentary (own/partner) accounts: no new-account cap
    if new_account: included = min(included, L["new_account_minutes"] * max(1, len(members)))
    return {"period_start": ps, "included": included, "extra": extra, "used": used, "remaining": max(0, included + extra - used),
            "new_account_limit": new_account, "pool": True, "extensions": len(members)}

def usage(con, cust, t=None):
    co = co_of(con, cust)
    if co: return company_usage(con, co, t)
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

def setting(con, k, default=""):
    r = con.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default

def set_setting(con, k, v):
    con.execute("INSERT INTO settings (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

def guard_metrics(con, t=None):
    t = t or now()
    one = lambda q, *a: con.execute(q, a).fetchone()[0] or 0
    return {"out_minutes_hour": one("SELECT SUM(minutes) FROM calls WHERE direction='out' AND started_at>=?", iso(t - dt.timedelta(hours=1))),
            "out_minutes_day": one("SELECT SUM(minutes) FROM calls WHERE direction='out' AND started_at>=?", iso(t - dt.timedelta(days=1))),
            "out_calls_10min": one("SELECT COUNT(*) FROM calls WHERE direction='out' AND started_at>=?", iso(t - dt.timedelta(minutes=10))),
            "new_lines_day": one("SELECT COUNT(*) FROM customers WHERE created_at>=?", iso(t - dt.timedelta(days=1)))}

def guard_check(con):
    """Master shutoff: True if outgoing calls may proceed. Trips (and stays tripped) when any limit is passed."""
    if setting(con, "out_paused") == "1": return False
    m, G = guard_metrics(con), C["guard"]
    over = [f"{k} {m[k]} > {G[k]}" for k in ("out_minutes_hour", "out_minutes_day", "out_calls_10min") if m[k] > G[k]]
    if over:
        set_setting(con, "out_paused", "1"); set_setting(con, "out_paused_reason", "; ".join(over)); set_setting(con, "out_paused_at", iso(now()))
        event(con, "", "guard_trip", "Outgoing calls paused: " + "; ".join(over))
        con.commit()
        return False
    return True

def check_outbound(con, cust, dest):
    """Returns (ok, reason, max_seconds)."""
    if cust is None: return False, "unknown", 0
    if not guard_check(con): return False, "paused_all", 0
    if cust["status"] != "active": return False, "suspended", 0
    co = co_of(con, cust)
    if not cust["did"] and not (co and co_main_did(con, co)): return False, "no_number", 0
    if co and co["status"] != "active": return False, "suspended", 0
    u = usage(con, cust)
    if u["remaining"] <= 0: return False, "no_minutes", 0
    L = dict(C["limits"], **(C["limits_biz"] if co else {})); t = now()
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
        cid_num = c["did"] or co_main_did(con, co_of(con, c)) or "0"
        out.append(f"""
[{u}]
type=endpoint
context=from-customer
message_context=gs-messages
transfer_context=gs-transfer
allow_transfer=yes
moh_suggest=default
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
callerid=<{cid_num}>

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
    # computer browser logins (WebRTC): "<user>-web", a separate password that expires (24 h, renewed while used)
    for c in con.execute("SELECT * FROM customers WHERE status='active' AND web_pass IS NOT NULL AND web_exp > ?", (iso(now()),)):
        u = c["sip_user"]; w = u + "-web"
        cid_num = c["did"] or co_main_did(con, co_of(con, c)) or "0"
        out.append(f"""
[{w}]
type=endpoint
context=from-customer
message_context=gs-messages
transfer_context=gs-transfer
allow_transfer=yes
moh_suggest=default
disallow=all
allow=ulaw,alaw
webrtc=yes
auth={w}-auth
aors={w}
direct_media=no
force_rport=yes
rewrite_contact=yes
device_state_busy_at=1
callerid=<{cid_num}>

[{w}-auth]
type=auth
auth_type=userpass
username={w}
password={c['web_pass']}

[{w}]
type=aor
max_contacts=3
remove_existing=no
qualify_frequency=30""")
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
        for co in con.execute("SELECT * FROM companies WHERE status='suspended' AND note LIKE 'unpaid%' AND paused_at IS NOT NULL AND paused_at < ?", (cutoff,)).fetchall():
            for m in co_members(con, co["id"]):
                if m["did"]: release_number(con, m, "company released after 30 days unpaid")
                con.execute("UPDATE customers SET status='canceled' WHERE sip_user=?", (m["sip_user"],))
            con.execute("UPDATE companies SET status='canceled' WHERE id=?", (co["id"],))
        for v in con.execute("SELECT * FROM voicemails WHERE created_at < ?", (iso(now() - dt.timedelta(days=60)),)).fetchall():
            try: os.remove(v["file"])
            except OSError: pass
            con.execute("DELETE FROM voicemails WHERE id=?", (v["id"],))
        con.execute("UPDATE customers SET web_pass=NULL, web_exp=NULL WHERE web_exp IS NOT NULL AND web_exp < ?", (iso(now()),))
        con.commit(); write_pjsip(con)

# ---------------------------------------------------------------- business phone system
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

def biz_closed(co, t=None):
    """hours_json = {"tz":"America/Chicago","days":{"mon":["09:00","17:00"],...}} — a day missing = closed all day."""
    h = json.loads(co["hours_json"] or "{}")
    if not h.get("enabled"): return False
    try:
        from zoneinfo import ZoneInfo
        lt = (t or now()).astimezone(ZoneInfo(h.get("tz") or "America/Chicago"))
    except Exception:
        lt = (t or now())
    span = (h.get("days") or {}).get(DAYS[lt.weekday()])
    if not span or len(span) != 2: return True
    hm = lt.strftime("%H:%M")
    return not (span[0] <= hm < span[1])

def greet_file(co_id, kind):
    return os.path.join(C["data_dir"], "greet", f"co{co_id}-{kind}")

VOICES = [  # id, label, language — only those whose model is installed are offered
    ("en_US-lessac-medium", "Lessac — female, US English", "en"), ("en_US-amy-medium", "Amy — female, US English", "en"),
    ("en_US-ryan-high", "Ryan — male, US English", "en"), ("en_US-joe-medium", "Joe — male, US English", "en"),
    ("es_MX-claude-high", "Claude — Spanish (Mexico)", "es"), ("es_ES-davefx-medium", "Dave — male, Spanish (Spain)", "es"),
    ("pt_BR-faber-medium", "Faber — male, Portuguese (Brazil)", "pt"), ("fr_FR-siwis-medium", "Siwis — female, French", "fr"),
]

def voices():
    return [{"id": v, "label": lab, "lang": lg} for v, lab, lg in VOICES
            if os.path.exists(os.path.join(C["piper_dir"], "voices", v + ".onnx")) and os.path.exists(os.path.join(C["piper_dir"], "piper"))]

def default_voice(lang):
    for v in voices():
        if v["lang"] == lang: return v["id"]
    return ""

def to_8k(src, path_noext):
    """Any audio (wav/mp3/webm/ogg/m4a) → 8 kHz mono 16-bit WAV for Asterisk, loudness evened out."""
    out = path_noext + ".wav"
    if subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ac", "1", "-ar", "8000", "-sample_fmt", "s16", "-af", "loudnorm=I=-18:TP=-2", "-t", "120", out],
                      timeout=60, capture_output=True).returncode == 0: return True
    return subprocess.run(["sox", src, "-r", "8000", "-c", "1", "-b", "16", out], timeout=60, capture_output=True).returncode == 0

def tts(text, path_noext, lang="en", voice=""):
    """Text → 8 kHz mono WAV that Asterisk plays: a natural Piper voice when installed, espeak-ng otherwise."""
    voice = voice if any(v["id"] == voice for v in voices()) else default_voice(lang)
    if voice:
        os.makedirs(os.path.dirname(path_noext), exist_ok=True)
        raw = path_noext + ".piper.wav"
        try:
            subprocess.run([os.path.join(C["piper_dir"], "piper"), "-m", os.path.join(C["piper_dir"], "voices", voice + ".onnx"), "-f", raw],
                           input=text[:1500].encode(), check=True, timeout=60, capture_output=True)
            if to_8k(raw, path_noext): return True
        except Exception as e:
            sys.stderr.write("piper %r\n" % e)
        finally:
            try: os.remove(raw)
            except OSError: pass
    voice = {"en": "en-us+f3", "pt": "pt-br+f3", "es": "es-419+f3", "fr": "fr-fr+f3"}.get(lang, "en-us+f3")
    os.makedirs(os.path.dirname(path_noext), exist_ok=True)
    raw = path_noext + ".tts.wav"
    try:
        subprocess.run([C["tts_cmd"], "-v", voice, "-s", "150", "-w", raw, text[:1500]], check=True, timeout=30, capture_output=True)
        subprocess.run(["sox", raw, "-r", "8000", "-c", "1", "-b", "16", path_noext + ".wav"], check=True, timeout=30, capture_output=True)
        return True
    except Exception as e:
        sys.stderr.write("tts %r\n" % e); return False
    finally:
        try: os.remove(raw)
        except OSError: pass

def default_greeting(co, ivr):
    parts = [f"Thank you for calling {co['name']}."]
    for d in sorted((ivr.get("options") or {}).keys()):
        lab = (ivr["options"][d].get("label") or "").strip()
        if lab: parts.append(f"For {lab}, press {d}.")
    parts.append("If you know your party's extension, you may dial it at any time.")
    return " ".join(parts)

def notify_site(kind, data):
    if not C["notify_url"]: return
    try:
        req = urllib.request.Request(C["notify_url"], data=json.dumps(dict(data, kind=kind)).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-GS-Token": C["notify_token"] or C["api_token"]})
        urllib.request.urlopen(req, timeout=6).read()
    except Exception as e:
        sys.stderr.write("notify %s %r\n" % (kind, e))

def member_json(con, m):
    d = {k: m[k] for k in ("sip_user", "email", "did", "ext", "display_name", "status", "note", "created_at", "plan")}
    d["vm_new"] = con.execute("SELECT COUNT(*) FROM voicemails WHERE sip_user=? AND heard=0", (m["sip_user"],)).fetchone()[0]
    ps = period_start(company(con, m["company_id"])["period_anchor"]).isoformat()
    d["used"] = con.execute("SELECT COALESCE(SUM(minutes),0) FROM calls WHERE sip_user=? AND started_at>=?", (m["sip_user"], ps)).fetchone()[0]
    return d

def company_json(con, co):
    d = {k: co[k] for k in co.keys()}
    d["ivr"] = json.loads(co["ivr_json"] or "{}"); d["hours"] = json.loads(co["hours_json"] or "{}")
    d.pop("ivr_json", None); d.pop("hours_json", None)
    d["main_did"] = co_main_did(con, co)
    d["members"] = [member_json(con, m) for m in co_members(con, co["id"])]
    d["usage"] = company_usage(con, co)
    d["closed_now"] = biz_closed(co)
    d["voices"] = voices()
    d["greeting_main"] = os.path.exists(greet_file(co["id"], "main") + ".wav")
    d["greeting_closed"] = os.path.exists(greet_file(co["id"], "closed") + ".wav")
    return d

def next_ext(con, cid):
    used = {m["ext"] for m in con.execute("SELECT ext FROM customers WHERE company_id=?", (cid,))}
    for n in range(101, 900):
        if str(n) not in used: return str(n)
    raise ApiError(409, "no free extension numbers")

def new_member(con, cid, email, name, ext, plan="biz_ext"):
    user = "c" + secrets.token_hex(5)
    con.execute("INSERT INTO customers (sip_user, sip_pass, email, plan, period_anchor, created_at, company_id, ext, display_name) VALUES (?,?,?,?,?,?,?,?,?)",
                (user, secrets.token_urlsafe(18), email.strip().lower(), plan, now().date().isoformat(), iso(now()), cid, ext, (name or "").strip()[:60]))
    event(con, user, "created", f"company {cid} ext {ext}")
    return user

def buy_for(con, user, want, area, e911):
    """Buy a number for a line (fallback in the same area code), receive texts, 911 address."""
    want = digits(want); want = ("1" + want) if len(want) == 10 else want
    taken = {r[0] for r in con.execute("SELECT did FROM customers WHERE did IS NOT NULL")}
    num, pid = telnyx_buy(want, digits(area), taken)
    con.execute("UPDATE customers SET did=?, phone_id=?, number_id=? WHERE sip_user=?", (num, pid, pid, user))
    event(con, user, "number", num + (" (picked)" if num == want else f" (wanted {want or 'any'})"))
    sms_attach(pid)
    if e911 and e911.get("street") and e911.get("zip"):
        con.execute("UPDATE customers SET e911_json=? WHERE sip_user=?", (json.dumps(e911), user))
        try:
            aid = telnyx_e911(pid, e911)
            con.execute("UPDATE customers SET e911=1, e911_address_id=? WHERE sip_user=?", (aid, user))
        except ApiError as e:
            event(con, user, "e911_failed", e.msg[:200])
    return num

def save_greeting(con, co, kind, text, lang, voice=""):
    if co["rec_" + kind]: return            # the owner's own recording stays until they switch back to a voice
    ok = tts(text, greet_file(co["id"], kind), lang, voice or co["voice"])
    if not ok: raise ApiError(500, "could not create the greeting audio")

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

# ---------------------------------------------------------------- alerts + daily security report
def alert_loop():
    """Every minute: email new alert events to the owner (via the website), at most one per kind per 30 min; check the Telnyx balance."""
    last_bal = 0
    try:
        with _lock, db() as con:     # very first start: begin after the existing history (don't email old events)
            if not setting(con, "alert_last_id"):
                set_setting(con, "alert_last_id", con.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]); con.commit()
    except Exception: pass
    while True:
        time.sleep(60)
        try:
            with _lock, db() as con:
                last = int(setting(con, "alert_last_id", "0") or 0)
                kinds = C["alert_kinds"]
                rows = con.execute(f"SELECT * FROM events WHERE id>? AND kind IN ({','.join('?' * len(kinds))}) ORDER BY id LIMIT 100", (last, *kinds)).fetchall()
                top = con.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
                set_setting(con, "alert_last_id", top); con.commit()
                send = {}
                for r in rows:
                    if setting(con, "alert_ts_" + r["kind"]) > iso(now() - dt.timedelta(minutes=30)): continue
                    send.setdefault(r["kind"], []).append(r)
                for k, rs in send.items():
                    set_setting(con, "alert_ts_" + k, iso(now()))
                con.commit()
            for k, rs in send.items():
                lines = [f"{r['ts']} UTC  {r['sip_user'] or '-'}  {r['detail']}" for r in rs[:20]]
                notify_site("alert", {"subject": ALERT_TITLES.get(k, k), "text": "\n".join(lines) + "\n\n" + ALERT_HELP.get(k, "")})
            if time.time() - last_bal > 1800 and C["telnyx_api_key"]:
                last_bal = time.time()
                try:
                    b = (telnyx("GET", "/balance").get("data") or {})
                    bal = float(b.get("available_credit") or b.get("balance") or 0)
                    if bal < float(C["low_balance"]):
                        with _lock, db() as con:
                            if setting(con, "alert_ts_low_balance") < iso(now() - dt.timedelta(hours=12)):
                                event(con, "", "low_balance", f"Telnyx balance ${bal:.2f}"); con.commit()
                except ApiError: pass
        except Exception as e:
            sys.stderr.write("alert loop %r\n" % e)

ALERT_TITLES = {"auto_suspend": "ALERT: a line was paused for robocall-like calling", "guard_trip": "ALERT: all outgoing calls paused (master shutoff)",
                "e911_failed": "ALERT: a 911 address was not accepted", "release_failed": "ALERT: a number could not be released",
                "low_balance": "ALERT: Telnyx balance is low", "line_flood": "ALERT: unusual number of new lines"}
ALERT_HELP = {"auto_suspend": "The line can't call out until you activate it in /shop-admin → Phone lines. Check its recent calls first.",
              "guard_trip": "Incoming calls still work. If this was real traffic, raise the limits; if not, find the line in /shop-admin and pause it. Turn outgoing calls back on in /shop-admin → Phone lines.",
              "e911_failed": "Fix the customer's 911 address (contact them), then retry.",
              "low_balance": "Add funds at Telnyx, or calls and number purchases will stop.",
              "line_flood": "Many new lines in 24 hours. Check that they are real customers."}

def report_main():
    """Daily security report → emailed to the owner by the website."""
    with _lock, db() as con:
        t = now(); day = iso(t - dt.timedelta(days=1)); one = lambda q, *a: con.execute(q, a).fetchone()[0] or 0
        outc, outm = one("SELECT COUNT(*) FROM calls WHERE direction='out' AND started_at>=?", day), one("SELECT SUM(minutes) FROM calls WHERE direction='out' AND started_at>=?", day)
        inc, inm = one("SELECT COUNT(*) FROM calls WHERE direction='in' AND started_at>=?", day), one("SELECT SUM(minutes) FROM calls WHERE direction='in' AND started_at>=?", day)
        top = con.execute("SELECT c.sip_user, c.email, c.did, SUM(k.minutes) m, COUNT(*) n FROM calls k JOIN customers c ON c.sip_user=k.sip_user WHERE k.started_at>=? GROUP BY c.sip_user ORDER BY m DESC LIMIT 5", (day,)).fetchall()
        newl = one("SELECT COUNT(*) FROM customers WHERE created_at>=?", day)
        alerts = con.execute(f"SELECT kind, COUNT(*) n FROM events WHERE ts>=? AND kind IN ({','.join('?' * len(C['alert_kinds']))}) GROUP BY kind", (day, *C["alert_kinds"])).fetchall()
        blocked = one("SELECT COUNT(*) FROM events WHERE ts>=? AND kind IN ('blocked_out','blocked_in')", day)
        lines = one("SELECT COUNT(*) FROM customers WHERE status='active' AND did IS NOT NULL")
        paused = setting(con, "out_paused") == "1"; reason = setting(con, "out_paused_reason")
    f2b = {}
    try: f2b = json.load(open(os.path.join(C["data_dir"], "f2b.json")))
    except Exception: pass
    bal = "?"
    try:
        b = (telnyx("GET", "/balance").get("data") or {}); bal = f"${float(b.get('available_credit') or b.get('balance') or 0):.2f}"
    except Exception: pass
    flags = []
    if paused: flags.append("OUTGOING CALLS ARE PAUSED (master shutoff): " + reason)
    for a in alerts: flags.append(f"{a['n']} × {ALERT_TITLES.get(a['kind'], a['kind'])}")
    if f2b.get("failed_24h", 0) > 2000: flags.append(f"Heavy login attacks: {f2b['failed_24h']} failed attempts in 24 h (all blocked)")
    status = "ALL NORMAL" if not flags else "NEEDS A LOOK"
    txt = [f"GlobaleSIM Phone — daily security report ({t.strftime('%Y-%m-%d')})", f"Status: {status}", ""]
    if flags: txt += ["Attention:"] + [" • " + x for x in flags] + [""]
    txt += [f"Attackers blocked (permanent): {f2b.get('banned', '?')} total, {f2b.get('new_24h', '?')} new in 24 h; failed login attempts 24 h: {f2b.get('failed_24h', '?')}",
            f"Calls 24 h: {outc} out ({outm} min), {inc} in ({inm} min); calls refused by limits: {blocked}",
            f"Active lines: {lines}; new lines 24 h: {newl}",
            f"Telnyx balance: {bal}", "", "Top lines (24 h):"]
    txt += [f"  {r['did'] or r['sip_user']}  {r['email']}  {r['m']} min / {r['n']} calls" for r in top] or ["  (no calls)"]
    if newl > C["guard"]["new_lines_day"]:
        with _lock, db() as con: event(con, "", "line_flood", f"{newl} new lines in 24 h"); con.commit()
    notify_site("report", {"subject": f"GlobaleSIM security report — {status}", "text": "\n".join(txt)})
    print("\n".join(txt))

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
            user, dest = base_user(args[1]), digits(args[2])
            a.set("GS_BASE", user)
            cust = con.execute("SELECT * FROM customers WHERE sip_user=?", (user,)).fetchone()
            ok, reason, maxsec = check_outbound(con, cust, dest)
            a.set("GS_OK", "1" if ok else "0"); a.set("GS_REASON", reason)
            if ok:
                co = co_of(con, cust)
                cur = con.execute("INSERT INTO calls (sip_user, direction, peer, started_at, billsec, result) VALUES (?,?,?,?,0,'dialing')", (user, "out", dest, iso(now())))
                a.set("GS_CALLID", str(cur.lastrowid)); a.set("GS_CID", "+" + (cust["did"] or co_main_did(con, co))); a.set("GS_MAXMS", str(int(maxsec * 1000)))
                a.set("GS_CO", f"co{co['id']}" if co else ""); a.set("GS_COMAX", str(max(2, len(co_members(con, co["id"], True)))) if co else "0")
                a.set("GS_GMAX", str(C["guard"]["concurrent"])); a.set("GS_MAXSEC", str(max(60, int(maxsec))))
            else:
                event(con, user, "blocked_out", f"{reason} {dest}")
        elif mode == "e911":              # e911 <endpoint>
            cust = con.execute("SELECT * FROM customers WHERE sip_user=?", (base_user(args[1]),)).fetchone()
            if cust and not cust["did"] and co_of(con, cust):
                cust = con.execute("SELECT * FROM customers WHERE sip_user=?", (co_of(con, cust)["main_user"],)).fetchone()
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
                    a.set("GS_DIALS", dials(con, [cust["sip_user"]])); a.set("GS_START", str(int(time.time())))
                    set_forward_vars(a, cust)
                    a.set("GS_VMUSER", cust["sip_user"] if (cust["vm_on"] if "vm_on" in cust.keys() else 1) else "")
                    co = co_of(con, cust); mode = ""
                    if co and co["main_user"] == cust["sip_user"] and co["status"] == "active":
                        if biz_closed(co):
                            mode = "closed"
                            g = greet_file(co["id"], "closed"); a.set("GS_GREET", g if os.path.exists(g + ".wav") else "")
                        elif (json.loads(co["ivr_json"] or "{}")).get("enabled"):
                            mode = "ivr"
                            g = greet_file(co["id"], "main"); a.set("GS_GREET", g if os.path.exists(g + ".wav") else "beep")
                        a.set("GS_CO", str(co["id"]))
                    a.set("GS_MODE", mode)
            elif C["test_user"] and (not C["company_did"] or did == digits(C["company_did"]) or not did):
                a.set("GS_OK", "1"); a.set("GS_USER", C["test_user"]); a.set("GS_DIALS", f"PJSIP/{C['test_user']}"); a.set("GS_CALLID", "0"); a.set("GS_MAXMS", str(4 * 3600 * 1000))
            else:
                a.set("GS_OK", "0"); a.set("GS_REASON", "unknown_number")
                if not con.execute("SELECT 1 FROM customers WHERE did=?", (did,)).fetchone():
                    event(con, "", "unknown_in", f"to={to_raw[:120]} exten={args[1]} caller={caller}")
        elif mode == "ext":               # ext <endpoint> <3-digit extension> — free calls inside a company
            me = con.execute("SELECT * FROM customers WHERE sip_user=?", (base_user(args[1]),)).fetchone()
            co = co_of(con, me)
            tgt = con.execute("SELECT * FROM customers WHERE company_id=? AND ext=? AND status='active'", (co["id"], args[2])).fetchone() if co else None
            ok = bool(tgt and me["status"] == "active" and tgt["sip_user"] != me["sip_user"])
            a.set("GS_OK", "1" if ok else "0")
            if ok:
                a.set("GS_USER", tgt["sip_user"]); a.set("GS_DIALS", dials(con, [tgt["sip_user"]])); a.set("GS_VMUSER", tgt["sip_user"] if tgt["vm_on"] else "")
                set_forward_vars(a, tgt)
                a.set("GS_CNAME", (me["display_name"] or f"Ext {me['ext']}").replace('"', "")); a.set("GS_CNUM", me["ext"] or "")
        elif mode == "ivr":               # ivr <company id> <digit | 3-digit ext | t>
            co = company(con, int(args[1] or 0)); key = args[2] if len(args) > 2 else ""
            ivr = json.loads(co["ivr_json"] or "{}") if co else {}
            def resolve(opt):
                typ = (opt or {}).get("type"); tg = (opt or {}).get("target")
                exts = tg if isinstance(tg, list) else ([tg] if tg else [])
                ms = [con.execute("SELECT * FROM customers WHERE company_id=? AND ext=? AND status='active'", (co["id"], str(e))).fetchone() for e in exts]
                ms = [m for m in ms if m]
                if typ == "repeat": return "repeat", "", ""
                if not ms:
                    main = con.execute("SELECT * FROM customers WHERE sip_user=?", (co["main_user"],)).fetchone()
                    ms = [main] if main else []
                if not ms: return "invalid", "", ""
                if typ == "vm": return "vm", "", ms[0]["sip_user"]
                return "dial", dials(con, [m["sip_user"] for m in ms]), ms[0]["sip_user"]
            if not co: act = ("invalid", "", "")
            elif key == "t": act = resolve(ivr.get("timeout") or {"type": "ext", "target": None})
            elif len(key) == 3 and key.isdigit():
                act = resolve({"type": "ext", "target": key}) if con.execute("SELECT 1 FROM customers WHERE company_id=? AND ext=? AND status='active'", (co["id"], key)).fetchone() else ("invalid", "", "")
            elif key in (ivr.get("options") or {}): act = resolve(ivr["options"][key])
            else: act = ("invalid", "", "")
            a.set("GS_ACT", act[0]); a.set("GS_DIAL", act[1]); a.set("GS_VMUSER", act[2])
        elif mode in ("fwd", "xfer"):
            # fwd <line being forwarded> <caller>  — call forwarding (always / busy / no answer)
            # xfer <BLINDTRANSFER channel> <target>  — "transfer now" from the app / computer / desk phone
            a.set("GS_OK", "0"); a.set("GS_XEXT", ""); a.set("GS_FWDEXT", "")
            if mode == "fwd":
                cust = con.execute("SELECT * FROM customers WHERE sip_user=?", (args[1],)).fetchone(); target = cust["fwd_to"] if cust else ""
            else:
                m = re.match(r"^PJSIP/(.+)-[0-9a-f]{8}$", args[1] or "")
                cust = con.execute("SELECT * FROM customers WHERE sip_user=?", (base_user(m.group(1)),)).fetchone() if m else None
                target = args[2] if len(args) > 2 else ""
            co = co_of(con, cust) if cust else None
            t = digits(target)
            if cust and co and re.fullmatch(r"[1-8]\d\d", t):          # to an extension of the same company: free
                tgt = con.execute("SELECT * FROM customers WHERE company_id=? AND ext=? AND status='active'", (co["id"], t)).fetchone()
                if tgt:
                    a.set("GS_OK", "1"); a.set("GS_DIALS", dials(con, [tgt["sip_user"]])); a.set("GS_VMUSER", tgt["sip_user"] if tgt["vm_on"] else "")
                    a.set("GS_XEXT" if mode == "xfer" else "GS_FWDEXT", "1")
            elif cust and parse_nanp(t):                                   # to an outside number: same checks + billing as any call
                num = parse_nanp(t)
                ok, reason, maxsec = check_outbound(con, cust, num)
                if ok:
                    cur = con.execute("INSERT INTO calls (sip_user, direction, peer, started_at, billsec, result) VALUES (?,?,?,?,0,?)", (cust["sip_user"], "out", num, iso(now()), mode))
                    a.set("GS_OK", "1"); a.set("GS_NUM", num); a.set("GS_XCALL", str(cur.lastrowid)); a.set("GS_XMAX", str(max(60, int(maxsec))))
                else:
                    event(con, cust["sip_user"], "blocked_out", f"{mode} {num} {reason}")
            if cust: event(con, cust["sip_user"], mode, f"to {t or '?'}")
        elif mode == "vmsave":            # vmsave <user> <file without .wav> <caller>
            user, f = args[1], args[2]; caller = digits(args[3]) if len(args) > 3 else ""
            path = f + ".wav"
            size = os.path.getsize(path) if os.path.exists(path) else 0
            secs = max(0, (size - 44) // 16000)
            if secs < 2:
                try: os.remove(path)
                except OSError: pass
            else:
                cust = con.execute("SELECT * FROM customers WHERE sip_user=?", (user,)).fetchone()
                cur = con.execute("INSERT INTO voicemails (sip_user, company_id, caller, file, secs, created_at) VALUES (?,?,?,?,?,?)",
                                  (user, cust["company_id"] if cust and "company_id" in cust.keys() else None, caller, path, secs, iso(now())))
                con.commit()
                if cust: notify_site("voicemail", {"id": cur.lastrowid, "email": cust["email"], "did": cust["did"] or "", "ext": cust["ext"] or "",
                                                   "caller": caller, "secs": secs, "created_at": iso(now())})
        elif mode == "charge":            # charge <callid> <answered-seconds>
            cid = int(args[1] or 0); secs = int(float(args[2] or 0)) if len(args) > 2 and args[2] else 0
            if len(args) > 4 and args[3].isdigit() and args[4].isdigit():      # answered by the menu / voicemail (or the carrier leg)
                secs = max(secs, int(args[4]) - int(args[3]))
            if len(args) > 6 and secs > 0 and args[5].isdigit() and args[6].isdigit() and len(args) > 4 and args[4].isdigit():
                # incoming call that kept going after a transfer: whole call minus the ringing time
                ring = max(0, int(float(args[5])) - int(float(args[2] or 0)))
                secs = max(secs, int(args[4]) - int(args[6]) - ring)
            if cid:
                mins = math.ceil(secs / 60) if secs > 0 else 0
                # several legs may report (caller side + carrier side after a transfer): keep the longest
                con.execute("UPDATE calls SET billsec=MAX(billsec, ?), minutes=MAX(COALESCE(minutes,0), ?), result=CASE WHEN ? > 0 OR billsec > 0 THEN 'answered' ELSE 'no_answer' END WHERE id=?",
                            (secs, mins, secs, cid))
                row = con.execute("SELECT c.* FROM customers c JOIN calls k ON k.sip_user=c.sip_user WHERE k.id=? AND k.direction='out'", (cid,)).fetchone()
                if row and row["status"] == "active" and robocall_check(con, row):
                    con.commit(); write_pjsip(con)
        con.commit()

# ---------------------------------------------------------------- API
class ApiError(Exception):
    def __init__(self, code, msg): super().__init__(msg); self.code = code; self.msg = msg

def cust_json(con, c):
    d = {k: c[k] for k in c.keys() if k not in ("sip_pass", "web_pass")}
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
        # ---------------- business accounts ----------------
        if p == ["companies"] and method == "GET":
            rows = con.execute("SELECT * FROM companies WHERE owner_email=? ORDER BY id DESC" if q.get("owner_email") else "SELECT * FROM companies ORDER BY id DESC LIMIT 500",
                               (q["owner_email"].strip().lower(),) if q.get("owner_email") else ()).fetchall()
            return {"companies": [company_json(con, c) for c in rows]}
        if p == ["guard"]:
            if method == "POST" and "paused" in body:
                set_setting(con, "out_paused", "1" if body["paused"] else "0")
                if not body["paused"]: set_setting(con, "out_paused_reason", "")
                event(con, "", "guard_set", "paused by admin" if body["paused"] else "resumed by admin"); con.commit()
            return {"paused": setting(con, "out_paused") == "1", "reason": setting(con, "out_paused_reason"), "since": setting(con, "out_paused_at"),
                    "metrics": guard_metrics(con), "limits": C["guard"]}
        if p == ["voices"] and method == "GET": return {"voices": voices()}
        if p == ["tts", "preview"] and method == "POST":
            tmp = os.path.join(C["data_dir"], "greet", "preview-" + secrets.token_hex(6))
            if not tts(str(body.get("text") or "Thank you for calling. For sales, press 1.")[:400], tmp, body.get("lang", "en"), str(body.get("voice") or "")):
                raise ApiError(500, "could not create audio")
            try: return ("raw", open(tmp + ".wav", "rb").read(), "audio/wav")
            finally:
                try: os.remove(tmp + ".wav")
                except OSError: pass
        if p == ["companies", "adopt"] and method == "POST":
            # admin: turn an existing line (with its number) into a complimentary business line — owner = extension 101
            c = con.execute("SELECT * FROM customers WHERE sip_user=?", (str(body.get("sip_user", "")),)).fetchone()
            name = (body.get("name") or "").strip()[:80]
            if not c or not c["did"] or c["status"] == "canceled": raise ApiError(404, "no active line with a number")
            if c["company_id"]: raise ApiError(409, "this line is already part of a company")
            if not name: raise ApiError(400, "company name required")
            cur = con.execute("INSERT INTO companies (name, owner_email, main_user, period_anchor, created_at, sub_ref, ivr_json, hours_json, comp) VALUES (?,?,?,?,?,?,?,?,1)",
                              (name, c["email"], c["sip_user"], c["period_anchor"], iso(now()), None, "{}", "{}"))
            cid = cur.lastrowid
            con.execute("UPDATE customers SET company_id=?, ext='101', display_name=COALESCE(NULLIF(display_name,''), 'Main') WHERE sip_user=?", (cid, c["sip_user"]))
            event(con, c["sip_user"], "company", f"made business line {cid} ({name})")
            con.commit(); write_pjsip(con)
            return company_json(con, company(con, cid))
        if p == ["companies", "provision"] and method == "POST":
            # after payment: company + owner as extension 101 + main number (chosen, or same area code) + 911 address
            email = (body.get("owner_email") or "").strip().lower(); name = (body.get("name") or "").strip()[:80]; ref = str(body.get("ref", ""))[:80]
            if "@" not in email or not name: raise ApiError(400, "owner_email and name required")
            if ref:
                done = con.execute("SELECT * FROM companies WHERE sub_ref=?", (ref,)).fetchone()
                if done and co_main_did(con, done): return dict(company_json(con, done), already=True)
            co = con.execute("SELECT * FROM companies WHERE sub_ref=?", (ref,)).fetchone() if ref else None
            if not co:
                cur = con.execute("INSERT INTO companies (name, owner_email, period_anchor, created_at, sub_ref, ivr_json, hours_json) VALUES (?,?,?,?,?,?,?)",
                                  (name, email, now().date().isoformat(), iso(now()), ref or None, "{}", "{}"))
                cid = cur.lastrowid
                owner = new_member(con, cid, email, body.get("owner_name") or "Main", "101")
                con.execute("UPDATE companies SET main_user=? WHERE id=?", (owner, cid))
                for x in (body.get("extensions") or [])[:50]:
                    if "@" in (x.get("email") or ""): new_member(con, cid, x["email"], x.get("name"), next_ext(con, cid))
                con.commit()
                co = company(con, cid)
            buy_for(con, co["main_user"], body.get("phone_number"), body.get("area_code"), body.get("e911") or {})
            con.commit(); write_pjsip(con)
            return company_json(con, company(con, co["id"]))
        if p and p[0] == "companies" and len(p) >= 2 and p[1].isdigit():
            co = company(con, int(p[1]))
            if not co: raise ApiError(404, "no such company")
            cid = co["id"]; sub = p[2] if len(p) > 2 else ""
            if sub == "" and method == "GET": return company_json(con, co)
            if sub == "extensions" and method == "POST" and len(p) == 3:
                email = (body.get("email") or "").strip().lower()
                if "@" not in email: raise ApiError(400, "email required")
                if con.execute("SELECT 1 FROM customers WHERE company_id=? AND email=? AND status<>'canceled'", (cid, email)).fetchone(): raise ApiError(409, "that email already has an extension")
                ext = str(body.get("ext") or "") or next_ext(con, cid)
                if not re.fullmatch(r"[1-8]\d\d", ext) or con.execute("SELECT 1 FROM customers WHERE company_id=? AND ext=? AND status<>'canceled'", (cid, ext)).fetchone():
                    raise ApiError(409, "extension number taken or invalid (101-899)")
                user = new_member(con, cid, email, body.get("name"), ext)
                con.commit()
                if body.get("buy_number"):
                    main = con.execute("SELECT * FROM customers WHERE sip_user=?", (co["main_user"],)).fetchone()
                    buy_for(con, user, body.get("phone_number"), body.get("area_code") or (main["did"] or "")[1:4], json.loads(main["e911_json"] or "{}") if main else {})
                con.commit(); write_pjsip(con)
                return company_json(con, company(con, cid))
            if sub == "extensions" and len(p) == 5 and method == "POST":
                m = con.execute("SELECT * FROM customers WHERE company_id=? AND sip_user=?", (cid, p[3])).fetchone()
                if not m: raise ApiError(404, "no such extension")
                if p[4] == "remove":
                    if m["sip_user"] == co["main_user"]: raise ApiError(409, "the main line can't be removed")
                    if m["did"]: release_number(con, m, "extension removed")
                    con.execute("UPDATE customers SET status='canceled', note='extension removed' WHERE sip_user=?", (m["sip_user"],))
                elif p[4] == "number":                      # give an extension its own direct number
                    if m["did"]: raise ApiError(409, "this extension already has a number")
                    main = con.execute("SELECT * FROM customers WHERE sip_user=?", (co["main_user"],)).fetchone()
                    con.commit()
                    buy_for(con, m["sip_user"], body.get("phone_number"), body.get("area_code") or ((main["did"] or "")[1:4] if main else ""), json.loads((main["e911_json"] if main else None) or "{}"))
                elif p[4] == "update":
                    if "name" in body: con.execute("UPDATE customers SET display_name=? WHERE sip_user=?", (str(body["name"])[:60], m["sip_user"]))
                    if "vm_on" in body: con.execute("UPDATE customers SET vm_on=? WHERE sip_user=?", (1 if body["vm_on"] else 0, m["sip_user"]))
                    if body.get("ext") and body["ext"] != m["ext"]:
                        if not re.fullmatch(r"[1-8]\d\d", str(body["ext"])) or con.execute("SELECT 1 FROM customers WHERE company_id=? AND ext=? AND status<>'canceled'", (cid, str(body["ext"]))).fetchone():
                            raise ApiError(409, "extension number taken or invalid (101-899)")
                        con.execute("UPDATE customers SET ext=? WHERE sip_user=?", (str(body["ext"]), m["sip_user"]))
                else: raise ApiError(404, "not found")
                con.commit(); write_pjsip(con)
                return company_json(con, company(con, cid))
            if sub == "ivr" and method == "POST":
                ivr = {"enabled": bool(body.get("enabled")), "options": {}, "timeout": body.get("timeout") or None}
                for k, v in (body.get("options") or {}).items():
                    if re.fullmatch(r"[0-9]", str(k)) and isinstance(v, dict) and v.get("type") in ("ext", "group", "vm", "repeat"):
                        ivr["options"][str(k)] = {"label": str(v.get("label", ""))[:40], "type": v["type"], "target": v.get("target")}
                con.execute("UPDATE companies SET ivr_json=? WHERE id=?", (json.dumps(ivr), cid))
                custom = (body.get("greeting_text") or "").strip()
                if body.get("voice") is not None: con.execute("UPDATE companies SET voice=? WHERE id=?", (str(body["voice"])[:60], cid)); co = company(con, cid)
                if ivr["enabled"]: save_greeting(con, co, "main", custom or default_greeting(co, ivr), body.get("lang", "en"))
                con.execute("UPDATE companies SET greeting=? WHERE id=?", (custom[:1500], cid))   # empty = built from the menu each time
                con.commit()
                return company_json(con, company(con, cid))
            if sub == "hours" and method == "POST":
                h = {"enabled": bool(body.get("enabled")), "tz": str(body.get("tz") or "America/Chicago")[:40], "days": {}}
                for d in DAYS:
                    v = (body.get("days") or {}).get(d)
                    if isinstance(v, list) and len(v) == 2 and all(re.fullmatch(r"\d\d:\d\d", str(x)) for x in v): h["days"][d] = [str(v[0]), str(v[1])]
                con.execute("UPDATE companies SET hours_json=? WHERE id=?", (json.dumps(h), cid))
                if h["enabled"]:
                    txt = (body.get("closed_text") or "").strip() or f"Thank you for calling {co['name']}. We are closed right now. Please leave a message after the tone and we will call you back."
                    if body.get("voice") is not None: con.execute("UPDATE companies SET voice=? WHERE id=?", (str(body["voice"])[:60], cid)); co = company(con, cid)
                    save_greeting(con, co, "closed", txt, body.get("lang", "en"))
                con.commit()
                return company_json(con, company(con, cid))
            if sub == "topup" and method == "POST":
                mins = int(body.get("minutes", 0)); tref = str(body.get("ref", ""))[:80]
                if not 0 < mins <= 100000: raise ApiError(400, "bad minutes")
                if not (tref and con.execute("SELECT 1 FROM topups WHERE ref=?", (tref,)).fetchone()):
                    con.execute("INSERT INTO topups (sip_user, company_id, minutes, period_start, created_at, ref) VALUES (?,?,?,?,?,?)",
                                (co["main_user"], cid, mins, company_usage(con, co)["period_start"], iso(now()), tref))
                    event(con, co["main_user"], "topup", f"company {cid}: {mins}")
                con.commit()
                return company_json(con, company(con, cid))
            if sub == "status" and method == "POST":       # pause / unpause the whole company (e.g. unpaid)
                st = body.get("status"); note = str(body.get("note", ""))[:200]
                if st not in ("active", "suspended", "canceled"): raise ApiError(400, "bad status")
                con.execute("UPDATE companies SET status=?, note=?, paused_at=? WHERE id=?", (st, note, iso(now()) if st == "suspended" else None, cid))
                for m in co_members(con, cid):
                    if st == "active" and m["status"] == "suspended" and not (m["note"] or "").startswith(("unpaid", "company")): continue   # keep robocall/admin pauses
                    con.execute("UPDATE customers SET status=?, note=? WHERE sip_user=?", (st, ("company " + note) if st != "active" else None, m["sip_user"]))
                con.commit(); write_pjsip(con)
                return company_json(con, company(con, cid))
            if sub == "greeting" and len(p) == 4 and p[3] in ("main", "closed"):
                kind = p[3]
                if method == "GET":
                    try: return ("raw", open(greet_file(cid, kind) + ".wav", "rb").read(), "audio/wav")
                    except OSError: raise ApiError(404, "no greeting yet")
                if method == "POST" and body.get("audio_b64"):          # the owner's own recording / file
                    import base64 as b64
                    data = b64.b64decode(body["audio_b64"])[:8_000_000]
                    if len(data) < 2000: raise ApiError(400, "recording too short")
                    src = greet_file(cid, kind) + ".upload"
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    open(src, "wb").write(data)
                    try:
                        if not to_8k(src, greet_file(cid, kind)): raise ApiError(400, "could not read that audio file")
                    finally:
                        try: os.remove(src)
                        except OSError: pass
                    con.execute(f"UPDATE companies SET rec_{kind}=1 WHERE id=?", (cid,)); con.commit()
                    return company_json(con, company(con, cid))
                if method == "POST" and body.get("use_voice"):          # back to a spoken (Piper) greeting
                    con.execute(f"UPDATE companies SET rec_{kind}=0 WHERE id=?", (cid,)); co = company(con, cid)
                    if kind == "main":
                        ivr = json.loads(co["ivr_json"] or "{}"); save_greeting(con, co, "main", co["greeting"] or default_greeting(co, ivr), body.get("lang", "en"))
                    else:
                        save_greeting(con, co, "closed", f"Thank you for calling {co['name']}. We are closed right now. Please leave a message after the tone and we will call you back.", body.get("lang", "en"))
                    con.commit()
                    return company_json(con, company(con, cid))
                raise ApiError(400, "bad request")
            if sub == "renew" and method == "POST":        # monthly payment received: fresh pool
                con.execute("UPDATE companies SET period_anchor=? WHERE id=?", (now().date().isoformat(), cid)); con.commit()
                return company_json(con, company(con, cid))
            raise ApiError(404, "not found")
        # ---------------- voicemail ----------------
        if p and p[0] == "voicemails" and len(p) >= 2 and p[1].isdigit():
            v = con.execute("SELECT * FROM voicemails WHERE id=?", (int(p[1]),)).fetchone()
            if not v: raise ApiError(404, "no such voicemail")
            if len(p) == 3 and p[2] == "audio" and method == "GET":
                try: return ("raw", open(v["file"], "rb").read(), "audio/wav")
                except OSError: raise ApiError(404, "audio gone")
            if len(p) == 3 and p[2] == "heard" and method == "POST":
                con.execute("UPDATE voicemails SET heard=1 WHERE id=?", (v["id"],)); con.commit(); return {"ok": True}
            if len(p) == 3 and p[2] == "delete" and method == "POST":
                try: os.remove(v["file"])
                except OSError: pass
                con.execute("DELETE FROM voicemails WHERE id=?", (v["id"],)); con.commit(); return {"ok": True}
            return {k: v[k] for k in v.keys() if k != "file"}
        if p == ["alerts"]:
            return {"events": [dict(r) for r in con.execute("SELECT * FROM events WHERE kind IN ('auto_suspend','blocked_out','blocked_in','unknown_in','text_unknown','e911_failed','release_failed','released','guard_trip','guard_set','low_balance','line_flood') ORDER BY id DESC LIMIT 200")]}
        if len(p) >= 2 and p[0] == "customers":
            c = con.execute("SELECT * FROM customers WHERE sip_user=?", (p[1],)).fetchone()
            if not c: raise ApiError(404, "no such customer")
            u = c["sip_user"]; sub = p[2] if len(p) > 2 else ""
            if sub == "" and method == "GET":
                d = cust_json(con, c)
                d["recent_calls"] = [dict(r) for r in con.execute("SELECT direction, peer, started_at, billsec, minutes, result FROM calls WHERE sip_user=? ORDER BY id DESC LIMIT 50", (u,))]
                d["texts"] = [dict(r) for r in con.execute("SELECT id, peer, body, received_at FROM texts WHERE sip_user=? ORDER BY id DESC LIMIT 50", (u,))]
                d["voicemails"] = [dict(r) for r in con.execute("SELECT id, caller, secs, created_at, heard FROM voicemails WHERE sip_user=? ORDER BY id DESC LIMIT 50", (u,))]
                co = co_of(con, c)
                if co: d["company"] = {"id": co["id"], "name": co["name"], "owner": co["owner_email"] == c["email"], "ext": c["ext"], "main_did": co_main_did(con, co)}
                return d
            if sub == "forward" and method == "POST":         # call forwarding + do not disturb
                mode = str(body.get("mode") or ""); to = digits(str(body.get("to") or ""))
                if mode not in ("", "always", "busy", "noanswer", "busy_noanswer"): raise ApiError(400, "bad mode")
                co = co_of(con, c)
                if mode:
                    if co and re.fullmatch(r"[1-8]\d\d", to):
                        if to == (c["ext"] or ""): raise ApiError(400, "can't forward to yourself")
                        if not con.execute("SELECT 1 FROM customers WHERE company_id=? AND ext=? AND status='active'", (co["id"], to)).fetchone(): raise ApiError(400, "no such extension")
                    else:
                        to = parse_nanp(to)
                        if not to: raise ApiError(400, "enter a US or Canada number")
                        if to == (c["did"] or ""): raise ApiError(400, "can't forward to the same number")
                con.execute("UPDATE customers SET fwd_mode=?, fwd_to=?, dnd=? WHERE sip_user=?", (mode, to if mode else (c["fwd_to"] or ""), 1 if body.get("dnd") else 0, u))
                event(con, u, "forward_set", f"{mode or 'off'} {to} dnd={1 if body.get('dnd') else 0}"); con.commit()
                c = con.execute("SELECT * FROM customers WHERE sip_user=?", (u,)).fetchone()
                return cust_json(con, c)
            if sub == "webcreds" and method == "GET":         # computer browser login (website checked the owner)
                if c["status"] != "active": raise ApiError(403, "line not active")
                if not c["web_pass"] or not c["web_exp"] or c["web_exp"] < iso(now() + dt.timedelta(hours=2)):
                    con.execute("UPDATE customers SET web_pass=?, web_exp=? WHERE sip_user=?", (secrets.token_urlsafe(18), iso(now() + dt.timedelta(hours=24)), u))
                    con.commit(); write_pjsip(con)
                    c = con.execute("SELECT * FROM customers WHERE sip_user=?", (u,)).fetchone()
                return {"sip_user": u + "-web", "sip_pass": c["web_pass"], "domain": C["sip_domain"], "wss": f"wss://{C['sip_domain']}/ws", "expires": c["web_exp"]}
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
            if isinstance(res, tuple) and res and res[0] == "raw":
                self.send_response(200); self.send_header("Content-Type", res[2]); self.send_header("Content-Length", str(len(res[1]))); self.end_headers(); self.wfile.write(res[1])
                return
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
    threading.Thread(target=alert_loop, daemon=True).start()
    ThreadingHTTPServer((C["api_listen"], int(C["api_port"])), Handler).serve_forever()

if __name__ == "__main__":
    if sys.argv[1:2] == ["api"]: api_main()
    elif sys.argv[1:2] == ["agi"]: agi_main(sys.argv[2:])
    elif sys.argv[1:2] == ["daily"]: daily_main()
    elif sys.argv[1:2] == ["report"]: report_main()
    elif sys.argv[1:2] == ["sms-setup"]: sms_setup(os.environ.get("GSVOICE_CONF", "/etc/gsvoice/gsvoice.json"))
    elif sys.argv[1:2] == ["regen"]:
        with db() as con: write_pjsip(con)
    else: print(__doc__)
