#!/usr/bin/env python3
"""GlobaleSIM Voice — customer accounts, minutes, safety limits and provisioning API.

One file, Python standard library only. Used two ways:
  * AGI from Asterisk:   gs-agi.py out|in|charge ...   (decides if a call may happen, charges minutes)
  * HTTPS API (behind Caddy): python3 gsvoice.py api   (the website/app create customers, buy numbers, add minutes)

Data: SQLite at /var/lib/gsvoice/gsvoice.db.  Config: /etc/gsvoice/gsvoice.json.
Customer SIP logins are written to /etc/asterisk/pjsip_customers.conf (suspended customers are left out).
"""
import datetime as dt, hmac, json, math, os, random, re, secrets, socket, sqlite3, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request
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
    "limits_biz": {"new_calls_per_10min": 20, "calls_per_day": 300, "robocall_calls": 40, "robocall_short_calls": 30},
    # where voicemail / alerts are reported (app.php?api=voice_hook), with a shared secret
    "notify_url": "", "notify_token": "",
    "data_dir": "/var/lib/gsvoice", "tts_cmd": "espeak-ng",
    # natural voices (Piper, runs on this server); the installer downloads the models into piper_dir/voices
    "piper_dir": "/opt/piper",
    # Master shutoff for outgoing calls (all lines together). Tripping it pauses outgoing calls only and emails you.
    # Telnyx list prices (USD), used for cost estimates in the admin panel
    "rates": {"in": 0.0032, "out": 0.005, "number": 1.0, "e911": 1.5, "channel_tiers": [[10, 12.0], [40, 11.0], [200, 9.0], [1000000, 8.0]]},
    # a number taken off a line goes to our pool (no new Telnyx charge) and rests this many days before a new customer gets it
    "pool_rest_days": 30,
    "guard": {"out_minutes_hour": 900, "out_minutes_day": 6000, "out_calls_10min": 150, "concurrent": 40, "new_lines_day": 25},
    "low_balance": 20,                 # email when the Telnyx balance drops below this (USD)
    "alert_kinds": ["auto_suspend", "guard_trip", "e911_failed", "release_failed", "low_balance", "line_flood", "backup_failed"],
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
    CREATE TABLE IF NOT EXISTS departments (id INTEGER PRIMARY KEY, company_id INTEGER, name TEXT, exts TEXT DEFAULT '[]', created_at TEXT);
    CREATE TABLE IF NOT EXISTS stock (number TEXT PRIMARY KEY, phone_id TEXT, bought_at TEXT, note TEXT, available_at TEXT, prev_email TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS customers_did ON customers(did) WHERE did IS NOT NULL AND did <> '';
    """)
    cols = {r[1] for r in con.execute("PRAGMA table_info(customers)")}
    for col, typ in (("phone_id", "TEXT"), ("e911", "INTEGER DEFAULT 0"), ("e911_address_id", "TEXT"), ("e911_json", "TEXT"),
                     ("paused_at", "TEXT"), ("sub_ref", "TEXT"), ("company_id", "INTEGER"), ("ext", "TEXT"), ("display_name", "TEXT"),
                     ("vm_on", "INTEGER DEFAULT 1"), ("web_pass", "TEXT"), ("web_exp", "TEXT"),
                     ("fwd_mode", "TEXT DEFAULT ''"), ("fwd_to", "TEXT"), ("dnd", "INTEGER DEFAULT 0"),
                     ("desk_pass", "TEXT"), ("desk_brand", "TEXT"), ("desk_mac", "TEXT"), ("billing", "TEXT DEFAULT 'paypal'"), ("admin_note", "TEXT"),
                     ("backup_to", "TEXT")):
        if col not in cols: con.execute(f"ALTER TABLE customers ADD COLUMN {col} {typ}")
    kc = {r[1] for r in con.execute("PRAGMA table_info(calls)")}
    for col, typ in (("q_jitter", "REAL"), ("q_loss", "REAL"), ("q_rtt", "REAL"), ("q_mos", "REAL"), ("q_pkts", "INTEGER"), ("q_ip", "TEXT"), ("q_dev", "TEXT"),
                     ("answered_by", "TEXT"), ("dept", "TEXT"), ("cid_used", "TEXT"), ("push", "TEXT")):
        if col not in kc: con.execute(f"ALTER TABLE calls ADD COLUMN {col} {typ}")
    cc = {r[1] for r in con.execute("PRAGMA table_info(companies)")}
    for col, typ in (("comp", "INTEGER DEFAULT 0"), ("voice", "TEXT DEFAULT ''"), ("rec_main", "INTEGER DEFAULT 0"), ("rec_closed", "INTEGER DEFAULT 0"), ("prov_token", "TEXT"), ("billing", "TEXT DEFAULT 'paypal'"), ("admin_note", "TEXT")):
        if col not in cc: con.execute(f"ALTER TABLE companies ADD COLUMN {col} {typ}")
    cc = {r[1] for r in con.execute("PRAGMA table_info(customers)")}
    for col in ("push_token", "push_platform", "push_at"):
        if col not in cc: con.execute(f"ALTER TABLE customers ADD COLUMN {col} TEXT")
    dc = {r[1] for r in con.execute("PRAGMA table_info(departments)")}
    for col in ("did", "phone_id"):
        if col not in dc: con.execute(f"ALTER TABLE departments ADD COLUMN {col} TEXT")
    sc = {r[1] for r in con.execute("PRAGMA table_info(stock)")}
    for col in ("available_at", "prev_email"):
        if col not in sc: con.execute(f"ALTER TABLE stock ADD COLUMN {col} TEXT")
    con.execute("UPDATE companies SET billing='free' WHERE comp=1 AND (billing IS NULL OR billing='paypal')")
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

# ---- desk phone automatic setup: the phone downloads its settings from /prov/<company token>/<file> ----
def mac_norm(m): return re.sub(r"[^0-9a-f]", "", (m or "").lower())

def prov_config(brand, c, name):
    host = C["sip_domain"]; user = c["sip_user"] + "-desk"; pw = c["desk_pass"]; label = (name or f"Ext {c['ext'] or ''}")[:20]
    if brand == "yealink":
        return "\n".join(["#!version:1.0.0.1", "account.1.enable = 1", f"account.1.label = {label}", f"account.1.display_name = {label}",
            f"account.1.auth_name = {user}", f"account.1.user_name = {user}", f"account.1.password = {pw}",
            f"account.1.sip_server.1.address = {host}", "account.1.sip_server.1.port = 5061", "account.1.sip_server.1.transport_type = 2",
            "account.1.sip_server.1.expires = 300", "account.1.srtp_encryption = 1", "account.1.nat.udp_update_enable = 1",
            "account.1.dtmf.type = 1", "security.trust_certificates = 0", "local_time.time_zone = -6", "local_time.time_zone_name = United States-Central Time", ""]), "text/plain"
    if brand == "grandstream":
        x = {"P271": "1", "P270": label, "P47": f"{host}:5061", "P35": user, "P36": user, "P34": pw, "P3": label, "P130": "2", "P183": "1", "P52": "2", "P63": "1"}
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<gs_provision version="1">\n<config version="1">\n'
                + "".join(f"<{k}>{v}</{k}>\n" for k, v in x.items()) + "</config>\n</gs_provision>\n"), "application/xml"
    if brand == "mitel":
        return "\n".join([f"sip line1 auth name: {user}", f"sip line1 user name: {user}", f"sip line1 password: {pw}",
            f"sip line1 display name: {label}", f"sip line1 screen name: {label}", f"sip line1 proxy ip: {host}", "sip line1 proxy port: 5061",
            f"sip line1 registrar ip: {host}", "sip line1 registrar port: 5061", "sip line1 transport protocol: 4", "sip srtp mode: 1",
            "sip registration period: 300", "time zone name: US-Central", ""]), "text/plain"
    return "", "text/plain"

def prov_lookup(token, fname):
    """Which extension's phone is asking? Only phones whose MAC the owner entered get a configuration."""
    with db() as con:
        co = con.execute("SELECT * FROM companies WHERE prov_token=? AND status='active'", (token,)).fetchone()
        if not co: return None
        name = re.sub(r"\.(cfg|xml)$", "", fname.lower())
        if name.startswith("cfg") and len(name) == 15: name = name[3:]          # Grandstream asks for cfg<mac>.xml
        mac = mac_norm(name)
        if len(mac) != 12: return None
        c = con.execute("SELECT * FROM customers WHERE company_id=? AND desk_mac=? AND status='active' AND desk_pass IS NOT NULL", (co["id"], mac)).fetchone()
        if not c: return None
        event(con, c["sip_user"], "desk_prov", f"{c['desk_brand']} {mac}"); con.commit()
        return prov_config(c["desk_brand"] or "", c, c["display_name"])

def ast_rx(cmd):
    """Run one Asterisk console command (read-only status questions)."""
    try: return subprocess.run(list(C.get("asterisk_cmd") or ["asterisk"]) + ["-rx", cmd], capture_output=True, text=True, timeout=5).stdout
    except Exception: return ""

_ping = {"t": 0.0, "r": None}
def ping():
    """Public health check for the outside watchdog — no customer data, no secrets. Cached 15 s."""
    if time.time() - _ping["t"] > 15 or _ping["r"] is None:
        up = "System uptime" in ast_rx("core show uptime seconds")
        regs = ast_rx("pjsip show registrations") if up else ""
        trunk = None if "carrier-reg" not in regs else bool(re.search(r"carrier-reg\S*\s+\S+\s+Registered", regs))
        try:
            with db() as con: con.execute("SELECT 1").fetchone(); dbok = True
        except Exception: dbok = False
        _ping.update(t=time.time(), r={"ok": up and dbok and trunk is not False, "asterisk": up, "trunk": trunk, "db": dbok, "at": iso(now())})
    return _ping["r"]

_pres = {"t": 0.0, "on": set(), "busy": set()}
def presence():
    """Who is signed in (app, computer or desk phone) and who is on a call — cached a few seconds."""
    if time.time() - _pres["t"] > 4:
        on = set(base_user(a) for a in re.findall(r"Contact:\s+([A-Za-z0-9_-]+)/sip:", ast_rx("pjsip show contacts")))
        busy = set(base_user(c) for c in re.findall(r"^PJSIP/([A-Za-z0-9_-]+)-[0-9a-f]{8}!", ast_rx("core show channels concise"), re.M))
        _pres.update(t=time.time(), on=on, busy=busy)
    return _pres

def team_json(con, co, me=None):
    pr = presence(); out = []
    for m in con.execute("SELECT * FROM customers WHERE company_id=? AND status='active' ORDER BY ext", (co["id"],)):
        u = m["sip_user"]
        st = "busy" if u in pr["busy"] else ("dnd" if m["dnd"] else ("available" if u in pr["on"] else "offline"))
        out.append({"ext": m["ext"], "name": m["display_name"] or "", "state": st, "me": u == me, "desk": bool(m["desk_pass"])})
    return out

def qos_parse(rtcp):
    """Asterisk CHANNEL(rtcp,all) string -> (jitter ms, loss %, round-trip ms, packets, MOS 1-4.5) or None if no audio flowed.
    The leg measured is the customer's device <-> our server, so it shows their internet, not the carrier's."""
    kv = dict(x.split("=", 1) for x in (rtcp or "").split(";") if "=" in x)
    f = lambda k: float(kv.get(k) or 0)
    rx, tx, lp, rlp = f("rxcount"), f("txcount"), max(0.0, f("lp")), max(0.0, f("rlp"))
    if rx + tx < 50: return None                                  # under ~1 second of audio: nothing to judge
    loss = max(lp / (rx + lp) if rx + lp else 0, rlp / (tx + rlp) if tx + rlp else 0) * 100
    jit = max(f("rxjitter"), f("txjitter")) * 1000; rtt = f("rtt") * 1000
    eff = rtt / 2 + 2 * jit + 10                                  # simplified ITU E-model
    r = (93.2 - eff / 40 if eff < 160 else 93.2 - (eff - 120) / 10) - 95 * loss / (loss + 10)   # packet-loss impairment (G.711 with concealment)
    r = max(0, min(100, r)); mos = 1 + 0.035 * r + 7e-6 * r * (r - 60) * (100 - r)
    return round(jit, 1), round(loss, 2), round(rtt, 1), int(rx + tx), round(max(1.0, min(4.5, mos)), 2)

def qos_grade(mos):
    return "good" if mos >= 4.0 else "fair" if mos >= 3.6 else "poor"

def base_user(ep):
    for suf in ("-web", "-desk"):
        if ep and ep.endswith(suf): return ep[:-len(suf)]
    return ep

_DIALU = []     # the lines the last dial string rings (an AGI run passes them on as GS_DIALU for the iPhone wake-up)

def dials(con, users):
    """Dial string ringing each line's phone app and, if signed in, its computer browser too."""
    global _DIALU
    _DIALU = list(users)
    out = []
    for u in users:
        out.append(f"PJSIP/{u}")
        r = con.execute("SELECT web_exp, desk_pass FROM customers WHERE sip_user=?", (u,)).fetchone()
        if r and r["web_exp"] and r["web_exp"] > iso(now()): out.append(f"PJSIP/{u}-web")
        if r and r["desk_pass"]: out.append(f"PJSIP/{u}-desk")
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
    L = dict(C["limits"])
    if co_of(con, cust):   # business lines (brokers, dispatch) make many short calls on purpose: far higher bar before an auto-pause
        LB = C.get("limits_biz") or {}
        L.update(robocall_calls=LB.get("robocall_calls", 40), robocall_short_calls=LB.get("robocall_short_calls", 30))
    since = iso(now() - dt.timedelta(minutes=L["robocall_window_min"]))
    rows = con.execute("SELECT peer, billsec FROM calls WHERE sip_user=? AND direction='out' AND started_at>=?", (cust["sip_user"], since)).fetchall()
    short = [r for r in rows if (r["billsec"] or 0) < L["robocall_short_sec"]]
    if len(rows) >= L["robocall_calls"] and len(short) >= L["robocall_short_calls"] and len({r["peer"] for r in rows}) >= L["robocall_short_calls"]:
        con.execute("UPDATE customers SET status='suspended', note=? WHERE sip_user=?", ("auto: robocall pattern " + iso(now()), cust["sip_user"]))
        event(con, cust["sip_user"], "auto_suspend", f"{len(rows)} calls / {len(short)} short in {L['robocall_window_min']} min")
        return True
    return False

# ---------------------------------------------------------------- hold music (per company)
def moh_dir(cid): return os.path.join(C["data_dir"], "moh", f"co{int(cid)}")
def moh_files(cid):
    d = moh_dir(cid)
    try: return sorted(f for f in os.listdir(d) if f.endswith(".wav"))
    except OSError: return []
def moh_class(con, c):
    """Music the other person hears when this line puts a call on hold: the company's own songs, else the default."""
    cid = c["company_id"] if "company_id" in c.keys() else None
    return f"co{int(cid)}" if cid and moh_files(cid) else "default"
def write_moh(con):
    """/var/lib/gsvoice/moh.conf (included by musiconhold.conf): one class per company that uploaded songs."""
    out = ["; generated by gsvoice.py"]
    for co in con.execute("SELECT id FROM companies"):
        if moh_files(co["id"]):
            # radio style: one live stream per company, so callers join mid-song instead of starting at song 1
            if os.path.exists("/usr/local/bin/gs-moh-stream"):
                out.append(f"[co{co['id']}]\nmode=custom\napplication=/usr/local/bin/gs-moh-stream {moh_dir(co['id'])}\nformat=slin\n")
            else:
                out.append(f"[co{co['id']}]\nmode=files\ndirectory={moh_dir(co['id'])}\nsort=alpha\n")
    path = os.path.join(C["data_dir"], "moh.conf")
    tmp = path + ".tmp"; open(tmp, "w").write("\n".join(out) + "\n"); os.replace(tmp, path)
    ast_rx("moh reload")
    try: write_parking(con)
    except Exception as e: print(f"parking: {e}", flush=True)

# ---------------------------------------------------------------- call parking
# Each company has its own lot "co<id>" with slots 801-899. Park = transfer the caller to *8; pick up = dial *8xx.
PARK_SLOTS = range(801, 900)
def write_parking(con):
    out = ["; generated by gsvoice.py — one parking lot per company (slots 801-899, ring back the parker after 3 minutes)"]
    for co in con.execute("SELECT id FROM companies WHERE status != 'canceled' ORDER BY id"):
        cid = co["id"]
        out.append(f"[co{cid}]\nparkpos={PARK_SLOTS[0]}-{PARK_SLOTS[-1]}\ncontext=gs-parked-co{cid}\nparkingtime=180\ncomebacktoorigin=no\n"
                   f"comebackdialtime=30\ncomebackcontext=gs-park-timeout\nfindslot=next\nparkedmusicclass={('co%d' % cid) if moh_files(cid) else 'default'}\n")
    path = os.path.join(C["data_dir"], "parking.conf"); new = "\n".join(out) + "\n"
    try: old = open(path).read()
    except OSError: old = ""
    if new != old:
        tmp = path + ".tmp"; open(tmp, "w").write(new); os.replace(tmp, path)
        ast_rx("module reload res_parking.so")

def ami_events(action, done_event, timeout=4):
    """Run one AMI action and return the events it lists (until done_event)."""
    if not C["ami_secret"]: return []
    evs = []
    try:
        with socket.create_connection((C["ami_host"], int(C["ami_port"])), timeout=timeout) as sk:
            f = sk.makefile("rwb"); f.readline()
            def send(lines): f.write(("".join(f"{k}: {v}\r\n" for k, v in lines) + "\r\n").encode()); f.flush()
            def block():
                d = {}
                while True:
                    ln = f.readline().decode(errors="replace")
                    if not ln: return None
                    ln = ln.strip()
                    if not ln: return d
                    k, _, v = ln.partition(":"); d[k.strip()] = v.strip()
            send([("Action", "Login"), ("Username", C["ami_user"]), ("Secret", C["ami_secret"]), ("Events", "off")])
            if (block() or {}).get("Response") != "Success": return []
            send(action + [("ActionID", "gs1")])
            for _ in range(500):
                d = block()
                if d is None: break
                if d.get("Event") == done_event or d.get("Response") == "Error": break
                if d.get("Event"): evs.append(d)
            send([("Action", "Logoff")])
    except OSError:
        pass
    return evs

def parked_calls(con, cid):
    """Calls parked in a company's lot right now: slot, caller, who parked it, seconds parked."""
    out = []
    for e in ami_events([("Action", "ParkedCalls"), ("ParkingLot", f"co{cid}")], "ParkedCallsComplete"):
        if e.get("Event") != "ParkedCall": continue
        by = base_user(re.sub(r"^PJSIP/", "", e.get("ParkerDialString", "")))
        r = con.execute("SELECT display_name, ext FROM customers WHERE sip_user=?", (by,)).fetchone() if by else None
        out.append({"slot": e.get("ParkingSpace", ""), "caller": e.get("ParkeeCallerIDNum", ""), "caller_name": "" if e.get("ParkeeCallerIDName", "") in ("<unknown>", "") else e.get("ParkeeCallerIDName"),
                    "seconds": int(e.get("ParkingDuration", "0") or 0), "parked_by": (r["display_name"] if r else "") or "", "parked_by_ext": (r["ext"] if r else "") or ""})
    return sorted(out, key=lambda x: x["slot"])

# ---------------------------------------------------------------- departments
def dept_list(con, cid):
    return [{"id": d["id"], "name": d["name"], "exts": json.loads(d["exts"] or "[]"), "did": d["did"] or ""} for d in
            con.execute("SELECT * FROM departments WHERE company_id=? ORDER BY name", (cid,))]
def dept_tag(name):
    """Department name as sent to the phones (X-GS-Dept header): plain characters only."""
    return re.sub(r"[^A-Za-z0-9 &'-]", "", name or "")[:40]

def dept_members(con, co, dept_id, free_first=True):
    """Active lines in a department. Members already on a call are skipped (they can't take another), unless everyone is busy."""
    d = con.execute("SELECT * FROM departments WHERE id=? AND company_id=?", (int(dept_id), co["id"])).fetchone()
    if not d: return None, []
    ms = [con.execute("SELECT * FROM customers WHERE company_id=? AND ext=? AND status='active'", (co["id"], str(e))).fetchone() for e in json.loads(d["exts"] or "[]")]
    ms = [m for m in ms if m]
    if free_first:
        busy = presence()["busy"]; free = [m for m in ms if m["sip_user"] not in busy]
        if free: ms = free
    return d, ms

def dept_number(con, co, dept_id, number):
    """Give a department its own direct number from the pool (calls to it ring the department straight away), or
    take it off again (number="") — the number goes back to the pool, ready at once (nobody else ever had it)."""
    d = con.execute("SELECT * FROM departments WHERE id=? AND company_id=?", (int(dept_id), co["id"])).fetchone()
    if not d: raise ApiError(404, "no such department")
    want = digits(number or "")
    if want and len(want) == 10: want = "1" + want
    if want == (d["did"] or ""): return
    st = None
    if want:
        st = con.execute("SELECT * FROM stock WHERE number=? AND COALESCE(available_at,'')<=?", (want, iso(now()))).fetchone()
        if not st: raise ApiError(409, "that number is not ready in the pool")
    if d["did"]:
        con.execute("INSERT OR REPLACE INTO stock (number, phone_id, bought_at, note, available_at, prev_email) VALUES (?,?,?,?,?,?)",
                    (d["did"], d["phone_id"] or "", iso(now()), ("was " + d["name"] + " direct number")[:80], iso(now()), ""))
    if st: con.execute("DELETE FROM stock WHERE number=?", (want,))
    con.execute("UPDATE departments SET did=?, phone_id=? WHERE id=?", (want or None, (st["phone_id"] if st else None), d["id"]))
    event(con, co["main_user"] or "", "dept_number", f"{d['name']} {want or 'removed'}")

# ---------------------------------------------------------------- iPhone call notifications (Apple push, APNs)
# gsvoice.json "apns": {"key_file": "/etc/gsvoice/apns.p8", "key_id": "...", "team_id": "...", "topic": "tel.globalesim.app",
#                       "host": "api.push.apple.com"}.  No extra Python packages: openssl signs, curl speaks HTTP/2.
_APNS_JWT = [0, ""]

def _b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def apns_jwt():
    ap = C.get("apns") or {}
    if _APNS_JWT[1] and time.time() - _APNS_JWT[0] < 40 * 60: return _APNS_JWT[1]
    head = _b64u(json.dumps({"alg": "ES256", "kid": ap["key_id"]}).encode())
    body = _b64u(json.dumps({"iss": ap["team_id"], "iat": int(time.time())}).encode())
    der = subprocess.run(["openssl", "dgst", "-sha256", "-sign", ap["key_file"]], input=f"{head}.{body}".encode(),
                         capture_output=True, timeout=10, check=True).stdout
    # DER ECDSA signature → raw r||s (64 bytes) for JWT
    i = 2 + (1 if der[1] & 0x80 else 0)
    def intpart(j):
        n = der[j + 1]; v = der[j + 2:j + 2 + n]; return v[-32:].rjust(32, b"\0"), j + 2 + n
    r, i = intpart(i); s_, _ = intpart(i)
    tok = f"{head}.{body}.{_b64u(r + s_)}"
    _APNS_JWT[0] = time.time(); _APNS_JWT[1] = tok
    return tok

def apns_ready():
    ap = C.get("apns") or {}
    return bool(ap.get("key_file") and ap.get("key_id") and ap.get("team_id") and os.path.exists(ap["key_file"]))

def apns_send_many(items):
    """items: [(token, payload dict, collapse id)] — sent in parallel, waits at most ~5 s. Returns {token: http status}."""
    if not items or not apns_ready(): return {}
    ap = C["apns"]; jwt = apns_jwt(); host = ap.get("host", "api.push.apple.com"); procs = []
    for tok, payload, coll in items:
        cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--http2", "-m", "5",
               "-H", f"authorization: bearer {jwt}", "-H", f"apns-topic: {ap.get('topic', 'tel.globalesim.app')}",
               "-H", "apns-push-type: alert", "-H", "apns-priority: 10", "-H", f"apns-expiration: {int(time.time()) + 40}",
               "-H", f"apns-collapse-id: {coll}", "-d", json.dumps(payload), f"{ap.get('base', 'https://' + host)}/3/device/{tok}"]
        procs.append((tok, subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)))
    out = {}
    for tok, pr in procs:
        try: out[tok] = int((pr.communicate(timeout=7)[0] or b"0").decode() or 0)
        except Exception: pr.kill(); out[tok] = 0
    return out

def call_push_payload(caller, callid, title="Incoming call", tail="tap to answer"):
    who = fmt_num(caller) if caller else "Unknown caller"
    return {"aps": {"alert": {"title": title, "body": f"{who} · {tail}" if tail else who}, "sound": "ring.wav",
                    "interruption-level": "time-sensitive", "thread-id": "calls"},
            "url": "globalesim://phone", "call": str(callid)}

def pushwait_main(a, args):
    """Before a ring: wake the iPhone apps of the lines about to ring, wait (≤ 14 s) until one of them has signed in again,
    then set the dial string again so it includes them.  Lines without an iPhone: nothing happens, no delay."""
    var = args[1] if len(args) > 1 and re.fullmatch(r"GS_[A-Z]+", args[1] or "") else "GS_DIALS"
    users = [u for u in a.cmd("GET VARIABLE GS_DIALU").partition("(")[2].rstrip(")").split(",") if u]
    caller = a.cmd("GET VARIABLE CALLERID(num)").partition("(")[2].rstrip(")")
    callid = a.cmd("GET VARIABLE GS_CALLID").partition("(")[2].rstrip(")") or "0"
    if not users or not apns_ready(): return
    with db() as con:
        rows = con.execute(f"SELECT sip_user, push_token FROM customers WHERE status='active' AND push_token IS NOT NULL AND push_token<>'' AND sip_user IN ({','.join('?' * len(users))})", users).fetchall()
    if not rows: return
    a.cmd("EXEC Ringing")
    t0 = time.time()
    res = apns_send_many([(r["push_token"], call_push_payload(caller, callid), f"call-{callid}") for r in rows])
    gone = [t for t, st in res.items() if st in (400, 410)]
    if gone:
        with db() as con: con.executemany("UPDATE customers SET push_token=NULL WHERE push_token=?", [(t,) for t in gone]); con.commit()
    woke = [r["sip_user"] for r in rows if res.get(r["push_token"]) == 200]
    if woke:
        while time.time() - t0 < 14:
            fresh = False
            for u in woke:
                cl = a.cmd(f"GET FULL VARIABLE ${{PJSIP_AOR({u}-web,contact)}}").partition("(")[2].rstrip(")")
                for c in [x for x in cl.split(",") if x]:
                    ex = a.cmd(f"GET FULL VARIABLE ${{PJSIP_CONTACT({c},expiration_time)}}").partition("(")[2].rstrip(")")
                    if ex.isdigit() and int(ex) - 300 >= t0 - 3: fresh = True      # registered after the push (expires 300 s)
            if fresh: break
            time.sleep(0.5)
    with db() as con: a.set(var, dials(con, users))
    with db() as con: con.execute("UPDATE calls SET push=? WHERE id=?", (",".join(woke), int(callid) if callid.isdigit() else 0)); con.commit()

# ---------------------------------------------------------------- Asterisk config
def write_pjsip(con):
    out = ["; generated by gsvoice.py — do not edit by hand"]
    for c in con.execute("SELECT * FROM customers WHERE status='active' ORDER BY sip_user"):
        u = c["sip_user"]; moh = moh_class(con, c)
        cid_num = c["did"] or co_main_did(con, co_of(con, c)) or "0"
        out.append(f"""
[{u}]
type=endpoint
context=from-customer
message_context=gs-messages
allow_transfer=yes
moh_suggest={moh}
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
    # desk phones (Yealink, Grandstream, Mitel, Poly…): "<user>-desk", own strong password, works over UDP/TCP/TLS,
    # encrypted audio when the phone offers it, one phone per extension
    for c in con.execute("SELECT * FROM customers WHERE status='active' AND desk_pass IS NOT NULL AND desk_pass<>''"):
        moh = moh_class(con, c)
        u = c["sip_user"]; w = u + "-desk"
        cid_num = c["did"] or co_main_did(con, co_of(con, c)) or "0"
        out.append(f"""
[{w}]
type=endpoint
context=from-customer
message_context=gs-messages
allow_transfer=yes
moh_suggest={moh}
disallow=all
allow=g722,ulaw,alaw
auth={w}-auth
aors={w}
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
dtmf_mode=rfc4733
device_state_busy_at=1
media_encryption=sdes
media_encryption_optimistic=yes
callerid=<{cid_num}>

[{w}-auth]
type=auth
auth_type=userpass
username={w}
password={c['desk_pass']}

[{w}]
type=aor
max_contacts=1
remove_existing=yes
qualify_frequency=60""")
    # computer browser logins (WebRTC): "<user>-web", a separate password that expires (24 h, renewed while used)
    for c in con.execute("SELECT * FROM customers WHERE status='active' AND web_pass IS NOT NULL AND web_exp > ?", (iso(now()),)):
        moh = moh_class(con, c)
        u = c["sip_user"]; w = u + "-web"
        cid_num = c["did"] or co_main_did(con, co_of(con, c)) or "0"
        out.append(f"""
[{w}]
type=endpoint
context=from-customer
message_context=gs-messages
allow_transfer=yes
moh_suggest={moh}
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
    try: write_parking(con)            # a parking lot for every company
    except Exception as e: print(f"parking: {e}", flush=True)

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

def telnyx_available(params):
    """Numbers Telnyx can sell us. No exact match (error 10031): ask again for the closest ones (best_effort);
    still nothing: an empty list, not an error."""
    try: return telnyx("GET", "/available_phone_numbers?" + urllib.parse.urlencode(params))
    except ApiError as e:
        if "10031" not in e.msg: raise
    try: return telnyx("GET", "/available_phone_numbers?" + urllib.parse.urlencode({**params, "filter[best_effort]": "true"}))
    except ApiError as e:
        if "10031" in e.msg: return {"data": []}
        raise

def telnyx_search(area_code="", contains="", limit=10):
    params = {"filter[country_code]": "US", "filter[features][]": "voice", "filter[limit]": str(limit)}
    if area_code: params["filter[national_destination_code]"] = digits(area_code)[:3]
    if contains: params["filter[phone_number][contains]"] = digits(contains)
    r = telnyx_available(params)
    return [n.get("phone_number") for n in r.get("data", []) if n.get("phone_number")]

def telnyx_phone_id(did, tries=1):
    """Telnyx id of one of our numbers, looked up by the number itself ('' if not in the account).
    tries>1 right after buying: number orders take a few seconds to finish."""
    for i in range(tries):
        r = telnyx("GET", "/phone_numbers?filter[phone_number]=" + urllib.parse.quote("+" + digits(did)) + "&page[size]=1")
        for n in r.get("data") or []:
            if digits(n.get("phone_number")) == digits(did) and n.get("id"): return str(n.get("id"))
        if i < tries - 1: time.sleep(2)
    return ""

def telnyx_backup(phone_id, to):
    """Backup number: Telnyx sends calls there by itself when our voice server can't be reached (on-failure).
    to = 11 digits, or '' to turn it off."""
    cf = {"call_forwarding_enabled": True, "forwards_to": "+" + to, "forwarding_type": "on-failure"} if to else {"call_forwarding_enabled": False}
    telnyx("PATCH", f"/phone_numbers/{phone_id}/voice", {"call_forwarding": cf})

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
        pid = telnyx_phone_id(n, tries=12)
        if pid: return n, pid
    raise ApiError(409, "no number available in that area code — try another")

def get_number(con, want, area, taken):
    """A number for a new line: the picked one or one from the same area code — from our pool first (ready now, already paid).
    Pool numbers still resting after a previous owner are only used when picked by number."""
    ac = digits(area)[:3] or (want[1:4] if want else "")
    ready = "SELECT * FROM stock WHERE number LIKE ? AND COALESCE(available_at,'')<=? ORDER BY bought_at LIMIT 1"
    st = con.execute("SELECT * FROM stock WHERE number=?", (want,)).fetchone() if want else None
    if not st and not want and ac:
        st = con.execute(ready, ("1" + ac + "%", iso(now()))).fetchone()
    if not st:
        try: return telnyx_buy(want, area, taken)
        except ApiError:                              # picked number gone and none to buy: a pool number in the same area code
            st = con.execute(ready, ("1" + ac + "%", iso(now()))).fetchone() if ac else None
            if not st: raise
    con.execute("DELETE FROM stock WHERE number=?", (st["number"],))
    return st["number"], st["phone_id"]

def channel_cost(n):
    left, cost = int(n or 0), 0.0
    for size, price in C["rates"]["channel_tiers"]:
        k = min(left, size); cost += k * price; left -= k
        if left <= 0: break
    return round(cost, 2)

def telnyx_all(path):
    out, page = [], 1
    while page <= 20:
        r = telnyx("GET", path + ("&" if "?" in path else "?") + f"page[number]={page}&page[size]=250")
        out += r.get("data") or []
        tp = ((r.get("meta") or {}).get("total_pages")) or 1
        if page >= tp: break
        page += 1
    return out

def telnyx_channels(n=None):
    """Incoming channels (unlimited incoming minutes, shared by every number on channel billing). n=None reads, else sets."""
    for path in ("/inbound_channels", "/phone_numbers/inbound_channels"):
        try:
            r = telnyx("GET", path) if n is None else telnyx("PATCH", path, {"channels": int(n)})
            d = r.get("data") or {}
            if isinstance(d, list): d = d[0] if d else {}
            return int(d.get("channels") or 0)
        except ApiError as e:
            if "404" not in e.msg and "not found" not in e.msg.lower() and path == "/inbound_channels": raise
            last = e
    raise last

def peak_incoming(con, days=30):
    """Most incoming calls at the same time in the last N days (ringing counted as ~15 s) — what channels must cover."""
    since = iso(now() - dt.timedelta(days=days)); ev = []
    for r in con.execute("SELECT started_at, billsec FROM calls WHERE direction='in' AND started_at>=?", (since,)):
        try: t = dt.datetime.fromisoformat(str(r["started_at"]).replace("Z", "").replace("T", " ")).timestamp()
        except ValueError: continue
        ev += [(t, 1), (t + 15 + max(0, int(r["billsec"] or 0)), -1)]
    cur = peak = 0
    for _, d in sorted(ev, key=lambda x: (x[0], x[1])):
        cur += d; peak = max(peak, cur)
    return peak

def line_costs(con):
    """This period's Telnyx cost per line and per company (list prices): minutes in/out + number + 911."""
    R = C["rates"]; lines = []
    for c in con.execute("SELECT * FROM customers WHERE status<>'canceled' OR did IS NOT NULL"):
        co = co_of(con, c)
        ps = period_start((co["period_anchor"] if co else c["period_anchor"]) or c["created_at"]).isoformat()
        mi = {r["direction"]: r["m"] for r in con.execute("SELECT direction, COALESCE(SUM(minutes),0) m FROM calls WHERE sip_user=? AND started_at>=? GROUP BY direction", (c["sip_user"], ps))}
        cost = mi.get("in", 0) * R["in"] + mi.get("out", 0) * R["out"] + (R["number"] if c["did"] else 0) + (R["e911"] if c["e911"] else 0)
        lines.append({"sip_user": c["sip_user"], "email": c["email"], "did": c["did"] or "", "plan": c["plan"], "status": c["status"], "company_id": c["company_id"],
                      "ext": c["ext"] or "", "since": ps, "billing": c["billing"] or "paypal", "min_in": mi.get("in", 0), "min_out": mi.get("out", 0), "cost": round(cost, 2)})
    cos = {}
    for l in lines:
        if l["company_id"]:
            k = cos.setdefault(l["company_id"], {"company_id": l["company_id"], "min_in": 0, "min_out": 0, "cost": 0.0, "lines": 0})
            k["min_in"] += l["min_in"]; k["min_out"] += l["min_out"]; k["cost"] = round(k["cost"] + l["cost"], 2); k["lines"] += 1
    return lines, list(cos.values())

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

def e911_off(phone_id, address_id=""):
    """Turn 911 off on a number (it must never send help to the previous owner's address)."""
    try: telnyx("PATCH", f"/phone_numbers/{phone_id}/voice", {"emergency": {"emergency_enabled": False}}); return True
    except ApiError:
        if not address_id: raise
    telnyx("POST", f"/phone_numbers/{phone_id}/actions/enable_emergency", {"emergency_enabled": False, "emergency_address_id": str(address_id)})
    return True

def to_pool(con, c, why):
    """Take the number off a line and keep it in our pool: Telnyx bills by the month, so returning it saves nothing.
    911 is switched off; the number rests pool_rest_days before a new customer is offered it."""
    if not c["did"]: return True
    if c["phone_id"] and C["telnyx_api_key"] and c["e911"]:
        try: e911_off(c["phone_id"], c["e911_address_id"] or "")
        except ApiError as e: event(con, c["sip_user"], "e911_failed", "could not turn 911 off on " + c["did"] + ": " + e.msg[:150])
    if c["phone_id"] and C["telnyx_api_key"] and c["backup_to"]:     # the next owner's calls must never ring this person's cell
        try: telnyx_backup(c["phone_id"], "")
        except ApiError as e: event(con, c["sip_user"], "backup_failed", "could not turn the backup number off on " + c["did"] + ": " + e.msg[:150])
    rest = now() + dt.timedelta(days=int(C.get("pool_rest_days", 30)))
    con.execute("INSERT OR REPLACE INTO stock (number, phone_id, bought_at, note, available_at, prev_email) VALUES (?,?,?,?,?,?)",
                (c["did"], c["phone_id"] or "", iso(now()), why[:80], iso(rest), c["email"]))
    con.execute("UPDATE customers SET did=NULL, phone_id=NULL, number_id=NULL, e911=0, e911_address_id=NULL, backup_to=NULL WHERE sip_user=? AND did=?", (c["sip_user"], c["did"]))
    event(con, c["sip_user"], "pooled", f"{c['did']} {why}")
    return True

def release_number(con, c, why):
    """Line closes: its number goes to our pool (see to_pool), the line is canceled."""
    to_pool(con, c, why)
    con.execute("UPDATE customers SET status='canceled', note=? WHERE sip_user=?", (why, c["sip_user"]))
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

def to_8k(src, path_noext, max_secs=120):
    """Any audio (wav/mp3/webm/ogg/m4a) → 8 kHz mono 16-bit WAV for Asterisk, loudness evened out."""
    out = path_noext + ".wav"
    if subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ac", "1", "-ar", "8000", "-sample_fmt", "s16", "-af", "loudnorm=I=-18:TP=-2", "-t", str(int(max_secs)), out],
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

def notify_detached(kind, data):
    """Send a site notification from a child process so the caller (an AGI on an emergency call) never waits for it."""
    try:
        if os.fork() != 0: return
    except OSError:
        return notify_site(kind, data)
    try:
        fd = os.open(os.devnull, os.O_RDWR)
        for n in (0, 1, 2): os.dup2(fd, n)                 # let Asterisk see the AGI finish right away
        os.setsid(); notify_site(kind, data)
    finally:
        os._exit(0)

def member_json(con, m):
    d = {k: m[k] for k in ("sip_user", "email", "did", "ext", "display_name", "status", "note", "created_at", "plan")}
    d["desk_on"] = bool(m["desk_pass"]); d["desk_brand"] = m["desk_brand"] or ""
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
    d["departments"] = dept_list(con, co["id"])
    d["moh"] = [f[:-4] for f in moh_files(co["id"])]
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
    num, pid = get_number(con, want, digits(area), taken)
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
                "low_balance": "ALERT: Telnyx balance is low", "line_flood": "ALERT: unusual number of new lines",
                "backup_failed": "ALERT: a backup number could not be changed at Telnyx"}
ALERT_HELP = {"auto_suspend": "The line can't call out until you activate it in /shop-admin → Phone lines. Check its recent calls first.",
              "guard_trip": "Incoming calls still work. If this was real traffic, raise the limits; if not, find the line in /shop-admin and pause it. Turn outgoing calls back on in /shop-admin → Phone lines.",
              "e911_failed": "Fix the customer's 911 address (contact them), then retry.",
              "low_balance": "Add funds at Telnyx, or calls and number purchases will stop.",
              "line_flood": "Many new lines in 24 hours. Check that they are real customers.",
              "backup_failed": "Open the number in the Telnyx portal → Voice → Call forwarding and set it by hand. A pooled number must have forwarding OFF."}

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
    if mode == "pushwait":
        try: pushwait_main(a, args)
        except Exception as e: sys.stderr.write(f"pushwait: {e}\n")
        return
    try: _agi_modes(a, mode, args)
    finally:
        if _DIALU and mode not in ("ans", "qos", "charge", "vmsave"): a.set("GS_DIALU", ",".join(_DIALU))

def _agi_modes(a, mode, args):
    with _lock, db() as con:
        if mode == "out":                 # out <endpoint> <dest> [chosen caller ID]
            user, dest = base_user(args[1]), digits(args[2]); want_cid = digits(args[3]) if len(args) > 3 else ""
            a.set("GS_BASE", user)
            cust = con.execute("SELECT * FROM customers WHERE sip_user=?", (user,)).fetchone()
            ok, reason, maxsec = check_outbound(con, cust, dest)
            a.set("GS_OK", "1" if ok else "0"); a.set("GS_REASON", reason)
            if ok:
                co = co_of(con, cust)
                cur = con.execute("INSERT INTO calls (sip_user, direction, peer, started_at, billsec, result) VALUES (?,?,?,?,0,'dialing')", (user, "out", dest, iso(now())))
                cid_num = cust["did"] or co_main_did(con, co)
                if want_cid and want_cid in (cust["did"] or "", co_main_did(con, co) if co else ""): cid_num = want_cid   # only numbers this line may use
                con.execute("UPDATE calls SET cid_used=? WHERE id=?", (cid_num, cur.lastrowid))
                a.set("GS_CALLID", str(cur.lastrowid)); a.set("GS_CID", "+" + cid_num); a.set("GS_MAXMS", str(int(maxsec * 1000)))
                a.set("GS_CO", f"co{co['id']}" if co else ""); a.set("GS_COMAX", str(max(2, len(co_members(con, co["id"], True)))) if co else "0")
                a.set("GS_GMAX", str(C["guard"]["concurrent"])); a.set("GS_MAXSEC", str(max(60, int(maxsec))))
            else:
                event(con, user, "blocked_out", f"{reason} {dest}")
        elif mode in ("e911", "e933"):    # e911|e933 <endpoint>  — 911 call, or the 933 address read-back test
            orig = con.execute("SELECT * FROM customers WHERE sip_user=?", (base_user(args[1]),)).fetchone()
            cust, co = orig, co_of(con, orig)
            if cust and not cust["did"] and co:          # extension without its own number: the company's number and address
                cust = con.execute("SELECT * FROM customers WHERE sip_user=?", (co["main_user"],)).fetchone()
            ok = bool(cust and cust["e911"] and cust["did"])
            a.set("GS_OK", "1" if ok else "0")
            if ok: a.set("GS_CID", "+" + cust["did"])
            test = mode == "e933"
            event(con, args[1], "e933_test" if test else "e911_call", "routed" if ok else "no 911 address")
            if not test and orig is not None:            # Kari's Law: tell the company (and us) at once — never delays the call
                addr = {}
                try: addr = json.loads((cust["e911_json"] if cust else None) or "{}")
                except Exception: pass
                info = {"sip_user": orig["sip_user"], "email": orig["email"], "ext": orig["ext"] or "", "name": orig["display_name"] or "",
                        "did": (cust["did"] if cust else "") or "", "routed": ok, "at": dt.datetime.now().isoformat(timespec="seconds"),
                        "address": ", ".join(x for x in (addr.get("street"), addr.get("unit"), addr.get("city"), addr.get("state"), addr.get("zip")) if x),
                        "company": co["name"] if co else "", "owner_email": co["owner_email"] if co else ""}
                notify_detached("e911_call", info)
        elif mode == "in":                # in <exten> <to-header> <caller>
            # the dialed number = user part of the To URI (never the host/port digits), else the extension
            def num(x):
                m = re.search(r"sip:\+?(\d{7,15})@", x or ""); d = m.group(1) if m else (digits(x) if len(digits(x or "")) <= 15 else "")
                return ("1" + d) if len(d) == 10 else d
            to_raw = args[2] if len(args) > 2 else ""; caller = digits(args[3]) if len(args) > 3 else ""
            did = num(to_raw) or num(args[1]) or num("sip:" + args[1] + "@")
            did = (C.get("did_alias") or {}).get(did, did)   # a backup-carrier (Sinch) number that rings like one of our numbers
            cust = con.execute("SELECT * FROM customers WHERE did=? AND status='active'", (did,)).fetchone()
            dnum = None
            if not cust and did:                  # a department's own direct number: billed to the company's main line
                dnum = con.execute("SELECT * FROM departments WHERE did=?", (did,)).fetchone()
                dco = company(con, dnum["company_id"]) if dnum else None
                if dco and dco["status"] == "active":
                    cust = con.execute("SELECT * FROM customers WHERE sip_user=? AND status='active'", (dco["main_user"],)).fetchone()
                if not cust: dnum = None
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
                    a.set("GS_DEPT", ""); a.set("GS_DEPTNAME", "")
                    if dnum and co:
                        a.set("GS_CO", str(co["id"]))
                        if biz_closed(co):
                            mode = "closed"
                            g = greet_file(co["id"], "closed"); a.set("GS_GREET", g if os.path.exists(g + ".wav") else "")
                        else:
                            mode = "dept"
                            d, ms = dept_members(con, co, dnum["id"])
                            if d and ms:
                                a.set("GS_DEPT", str(d["id"])); a.set("GS_DEPTNAME", dept_tag(d["name"])); a.set("GS_DIAL", dials(con, [m["sip_user"] for m in ms]))
                            else: a.set("GS_DIAL", dials(con, [cust["sip_user"]]))   # nobody in the department: the main line
                            con.execute("UPDATE calls SET dept=? WHERE id=?", (dept_tag(dnum["name"]), cur.lastrowid))
                    elif co and co["main_user"] == cust["sip_user"] and co["status"] == "active":
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
        elif mode == "ext" and len(args) > 2 and re.fullmatch(r"D\d+", args[2] or ""):   # ext <endpoint> D<id> — call a department
            me = con.execute("SELECT * FROM customers WHERE sip_user=?", (base_user(args[1]),)).fetchone()
            co = co_of(con, me); d, ms = dept_members(con, co, args[2][1:]) if co else (None, [])
            ms = [m for m in ms if m["sip_user"] != (me["sip_user"] if me else "")]
            ok = bool(me and me["status"] == "active" and ms)
            a.set("GS_OK", "1" if ok else "0"); a.set("GS_DND", ""); a.set("GS_FWD", "")
            if ok:
                a.set("GS_USER", ms[0]["sip_user"]); a.set("GS_DIALS", dials(con, [m["sip_user"] for m in ms])); a.set("GS_VMUSER", "")
                a.set("GS_CNAME", ((me["display_name"] or f"Ext {me['ext']}") + " (" + d["name"] + ")").replace('"', "")); a.set("GS_CNUM", me["ext"] or "")
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
            a.set("GS_DEPT", ""); a.set("GS_DEPTNAME", "")
            def resolve(opt):
                typ = (opt or {}).get("type"); tg = (opt or {}).get("target")
                if typ == "dept":                         # a department: everyone free in it rings at once
                    d, ms = dept_members(con, co, tg or 0)
                    if d and ms:
                        a.set("GS_DEPT", str(d["id"])); a.set("GS_DEPTNAME", dept_tag(d["name"]))
                        return "dial", dials(con, [m["sip_user"] for m in ms]), ms[0]["sip_user"]
                    typ, tg = "ext", None                 # empty department: the main line
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
            if cust and co and re.fullmatch(r"D\d+", target or ""):              # to a department: everyone free rings
                d, ms = dept_members(con, co, target[1:])
                if ms:
                    a.set("GS_OK", "1"); a.set("GS_DIALS", dials(con, [m["sip_user"] for m in ms])); a.set("GS_VMUSER", ms[0]["sip_user"] if ms[0]["vm_on"] else "")
                    a.set("GS_XEXT" if mode == "xfer" else "GS_FWDEXT", "1"); a.set("GS_DEPT", str(d["id"])); a.set("GS_DEPTNAME", dept_tag(d["name"]))
            elif cust and co and re.fullmatch(r"[1-8]\d\d", t):          # to an extension of the same company: free
                tgt = con.execute("SELECT * FROM customers WHERE company_id=? AND ext=? AND status='active'", (co["id"], t)).fetchone()
                if tgt:
                    a.set("GS_OK", "1"); a.set("GS_DIALS", dials(con, [tgt["sip_user"]])); a.set("GS_VMUSER", tgt["sip_user"] if tgt["vm_on"] else "")
                    a.set("GS_XEXT" if mode == "xfer" else "GS_FWDEXT", "1")
            elif cust and parse_nanp(t) and con.execute("SELECT 1 FROM customers WHERE did=?", (parse_nanp(t),)).fetchone() and mode == "fwd":
                event(con, cust["sip_user"], "blocked_out", f"fwd loop to own number {t}")   # never loop through our own numbers
            elif cust and parse_nanp(t):                                   # to an outside number: same checks + billing as any call
                num = parse_nanp(t)
                ok, reason, maxsec = check_outbound(con, cust, num)
                if ok:
                    cur = con.execute("INSERT INTO calls (sip_user, direction, peer, started_at, billsec, result) VALUES (?,?,?,?,0,?)", (cust["sip_user"], "out", num, iso(now()), mode))
                    a.set("GS_OK", "1"); a.set("GS_NUM", num); a.set("GS_XCALL", str(cur.lastrowid)); a.set("GS_XMAX", str(max(60, int(maxsec))))
                    # the carrier only accepts our own numbers as caller ID (the original caller's number is refused)
                    a.set("GS_CID", "+" + ((co_main_did(con, co) if co else "") or cust["did"] or ""))
                else:
                    event(con, cust["sip_user"], "blocked_out", f"{mode} {num} {reason}")
            if cust: event(con, cust["sip_user"], mode, f"to {t or '?'}")
        elif mode == "park":              # park <BLINDTRANSFER channel> — the caller was transferred to *8: pick a free slot in the company's lot
            ch = re.sub(r"^PJSIP/", "", (args[1] if len(args) > 1 else "") or "")
            who = base_user(ch.rsplit("-", 1)[0]) if "-" in ch else ""
            cust = con.execute("SELECT * FROM customers WHERE sip_user=? AND status='active'", (who,)).fetchone() if who else None
            co = co_of(con, cust) if cust else None
            if co:
                busy = {int(p["slot"]) for p in parked_calls(con, co["id"]) if str(p["slot"]).isdigit()}
                free = [n for n in PARK_SLOTS if n not in busy]
                if free:
                    slot = random.choice(free)
                    a.set("GS_OK", "1"); a.set("GS_LOT", f"co{co['id']}"); a.set("GS_SLOT", str(slot))
                    event(con, cust["sip_user"], "park", f"slot {slot}")
        elif mode == "unpark":            # unpark <endpoint> <slot> — *8xx from a phone of the same company
            cust = con.execute("SELECT * FROM customers WHERE sip_user=? AND status='active'", (base_user(args[1]),)).fetchone() if len(args) > 1 else None
            co = co_of(con, cust) if cust else None
            slot = args[2] if len(args) > 2 else ""
            if co and slot.isdigit() and int(slot) in PARK_SLOTS:
                a.set("GS_OK", "1"); a.set("GS_LOT", f"co{co['id']}")
                event(con, cust["sip_user"], "unpark", f"slot {slot}")
        elif mode == "parkback":          # parkback <PARKER dial string> — nobody took a parked call back: the parker's voicemail (else the company's)
            who = base_user(re.sub(r"^PJSIP/", "", (args[1] if len(args) > 1 else "") or ""))
            cust = con.execute("SELECT * FROM customers WHERE sip_user=?", (who,)).fetchone() if who else None
            co = co_of(con, cust) if cust else None
            if cust and cust["status"] == "active": a.set("GS_DIALS", dials(con, [cust["sip_user"]]))   # ring all of the parker's phones first
            vm = cust["sip_user"] if cust and cust["vm_on"] else ""
            if not vm and co:
                m = con.execute("SELECT sip_user, vm_on FROM customers WHERE sip_user=?", (co["main_user"],)).fetchone()
                vm = m["sip_user"] if m and m["vm_on"] else ""
            a.set("GS_VMUSER", vm)
        elif mode == "ans":               # ans <callid> <endpoint that answered> [dept id] — Dial U() on the answering leg
            cid = int(args[1]) if len(args) > 1 and (args[1] or "").isdigit() else 0
            who = base_user(args[2]) if len(args) > 2 else ""
            dname = ""
            if len(args) > 3 and (args[3] or "").isdigit():
                d = con.execute("SELECT name FROM departments WHERE id=?", (int(args[3]),)).fetchone(); dname = d["name"] if d else ""
            if cid and who:
                con.execute("UPDATE calls SET answered_by=?, dept=COALESCE(NULLIF(?,''), dept) WHERE id=?", (who, dname, cid))
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
        elif mode == "qos":               # qos <callid> <CHANNEL(rtcp,all)> <remote addr> <endpoint>  (hangup handler on the customer's leg)
            cid = int(args[1] or 0) if len(args) > 1 and (args[1] or "").isdigit() else 0
            q = qos_parse(args[2] if len(args) > 2 else "")
            if cid and q:
                ip = re.sub(r":\d+$", "", (args[3] if len(args) > 3 else "").strip("[] ")).strip("[]")[:45]
                ep = args[4] if len(args) > 4 else ""
                dev = "computer" if ep.endswith("-web") else "desk phone" if ep.endswith("-desk") else "app"
                # several devices may ring: keep the leg that carried the most audio
                con.execute("UPDATE calls SET q_jitter=?, q_loss=?, q_rtt=?, q_pkts=?, q_mos=?, q_ip=?, q_dev=? WHERE id=? AND COALESCE(q_pkts,0) < ?",
                            (q[0], q[1], q[2], q[3], q[4], ip, dev, cid, q[3]))
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
    d = {k: c[k] for k in c.keys() if k not in ("sip_pass", "web_pass", "push_token")}
    d["push"] = bool(c["push_token"]) if "push_token" in c.keys() else False
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
            # our own stock first: ready at once and already paid for
            ac, has = digits(q.get("area_code"))[:3], digits(q.get("contains"))
            mine = [r["number"] for r in con.execute("SELECT number FROM stock WHERE COALESCE(available_at,'')<=? ORDER BY number", (iso(now()),)) if (not ac or r["number"][1:4] == ac) and (not has or has in r["number"][1:])]
            mine = [n for n in mine if not con.execute("SELECT 1 FROM customers WHERE did=?", (n,)).fetchone()]
            try: r = telnyx_available(params)
            except ApiError:
                if not mine: raise
                r = {}
            out = [{"phone_number": "+" + n, "region": "In stock", "monthly": None, "stock": True} for n in mine[:5]]
            return {"numbers": out + [{"phone_number": n.get("phone_number"), "region": ", ".join(x.get("region_name", "") for x in n.get("region_information", []) if x.get("region_type") in ("rate_center", "state")),
                                 "monthly": (n.get("cost_information") or {}).get("monthly_cost")} for n in r.get("data", []) if digits(n.get("phone_number")) not in mine]}
        # ---- Telnyx tools for the admin panel ----
        if p == ["telnyx", "overview"] and method == "GET":
            R = C["rates"]; out = {"rates": R, "errors": []}
            try: b = telnyx("GET", "/balance").get("data") or {}; out["balance"] = float(b.get("available_credit") or b.get("balance") or 0)
            except ApiError as e: out["balance"] = None; out["errors"].append(e.msg[:200])
            try: out["channels"] = telnyx_channels()
            except ApiError as e: out["channels"] = None; out["errors"].append(e.msg[:200])
            ps = now().replace(day=1).strftime("%Y-%m-%d")
            m = {r["direction"]: r["m"] for r in con.execute("SELECT direction, COALESCE(SUM(minutes),0) m FROM calls WHERE started_at>=? GROUP BY direction", (ps,))}
            nums = con.execute("SELECT COUNT(*) FROM customers WHERE did IS NOT NULL AND did<>''").fetchone()[0]
            stock = con.execute("SELECT COUNT(*) FROM stock").fetchone()[0]
            e911 = con.execute("SELECT COUNT(*) FROM customers WHERE e911=1 AND did IS NOT NULL").fetchone()[0]
            ch = out["channels"] or 0
            est = {"numbers": round((nums + stock) * R["number"], 2), "e911": round(e911 * R["e911"], 2), "channels": channel_cost(ch),
                   "calls_in": round(m.get("in", 0) * R["in"], 2), "calls_out": round(m.get("out", 0) * R["out"], 2)}
            est["total"] = round(sum(est.values()), 2)
            out.update({"numbers_assigned": nums, "numbers_stock": stock, "e911_numbers": e911, "minutes_in": m.get("in", 0), "minutes_out": m.get("out", 0),
                        "month_start": ps, "estimate": est, "peak_incoming_30d": peak_incoming(con, 30)})
            return out
        if p == ["telnyx", "numbers"] and method == "GET":
            nums = telnyx_all("/phone_numbers")
            try: billing = {v.get("id"): v.get("usage_payment_method") for v in telnyx_all("/phone_numbers/voice")}
            except ApiError: billing = {}
            stockrows = {r["number"]: r for r in con.execute("SELECT * FROM stock")}; stock = set(stockrows)
            deptrows = {r["did"]: r for r in con.execute("SELECT d.did, d.name, c.name AS co FROM departments d LEFT JOIN companies c ON c.id=d.company_id WHERE d.did IS NOT NULL")}
            out = []
            for n in nums:
                d = digits(n.get("phone_number")); c = con.execute("SELECT * FROM customers WHERE did=?", (d,)).fetchone()
                co = co_of(con, c) if c else None
                out.append({"id": n.get("id"), "number": d, "status": n.get("status"), "connection": n.get("connection_name") or "",
                            "billing": billing.get(n.get("id")) or "pay-per-minute", "e911": bool(n.get("emergency_enabled")),
                            "purchased_at": (n.get("purchased_at") or n.get("created_at") or "")[:10],
                            "use": "customer" if c else ("stock" if d in stock else ("department" if d in deptrows else "other")),
                            "dept": deptrows[d]["name"] if d in deptrows else "",
                            "email": c["email"] if c else "", "sip_user": c["sip_user"] if c else "", "line_status": c["status"] if c else "",
                            "company": co["name"] if co else ((deptrows[d]["co"] or "") if d in deptrows else ""), "ext": (c["ext"] or "") if c else "",
                            "available_at": (stockrows[d]["available_at"] or "") if d in stockrows else "", "prev_email": (stockrows[d]["prev_email"] or "") if d in stockrows else "",
                            "pool_note": (stockrows[d]["note"] or "") if d in stockrows else ""})
            return {"numbers": out}
        if len(p) == 4 and p[:2] == ["telnyx", "numbers"] and p[3] == "billing" and method == "POST":
            m = str(body.get("method") or "")
            if m not in ("pay-per-minute", "channel"): raise ApiError(400, "method must be pay-per-minute or channel")
            if m == "channel" and telnyx_channels() < 1: raise ApiError(400, "buy at least 1 channel first — otherwise every call to this number gets a busy signal")
            telnyx("PATCH", f"/phone_numbers/{p[2]}/voice", {"usage_payment_method": m})
            event(con, "admin", "billing_set", f"{p[2]} {m}"); con.commit()
            return {"ok": True, "id": p[2], "billing": m}
        if p == ["telnyx", "channels"]:
            if method == "POST":
                n = int(body.get("channels", -1))
                if not 0 <= n <= 500: raise ApiError(400, "channels must be 0-500")
                got = telnyx_channels(n); event(con, "admin", "channels_set", str(got)); con.commit()
                return {"channels": got, "monthly": channel_cost(got)}
            got = telnyx_channels(); return {"channels": got, "monthly": channel_cost(got)}
        if p == ["telnyx", "stock"] and method == "POST":           # buy one number into stock
            want = digits(body.get("phone_number")); want = ("1" + want) if len(want) == 10 else want
            if not re.fullmatch(r"1\d{10}", want): raise ApiError(400, "phone_number required")
            if con.execute("SELECT 1 FROM customers WHERE did=? UNION SELECT 1 FROM stock WHERE number=? UNION SELECT 1 FROM departments WHERE did=?", (want, want, want)).fetchone(): raise ApiError(409, "we already own that number")
            num, pid = telnyx_buy(want, "", ())
            if num != want: raise ApiError(409, "that number was just taken")
            con.execute("INSERT INTO stock (number, phone_id, bought_at, note) VALUES (?,?,?,?)", (num, pid, iso(now()), (body.get("note") or "")[:80]))
            event(con, "admin", "stock_buy", num); con.commit()
            return {"ok": True, "number": num}
        if p == ["stock"] and method == "GET":                       # the pool, quick (no Telnyx call): ready numbers first
            return {"stock": [dict(r) for r in con.execute("SELECT number, available_at, note FROM stock ORDER BY COALESCE(available_at,'') > ?, number", (iso(now()),))],
                    "now": iso(now())}
        if len(p) == 4 and p[:2] == ["telnyx", "stock"] and p[3] == "adopt" and method == "POST":   # a number bought in the Telnyx portal -> our pool
            num = digits(p[2]); num = ("1" + num) if len(num) == 10 else num
            if not re.fullmatch(r"1\d{10}", num): raise ApiError(400, "bad number")
            if con.execute("SELECT 1 FROM customers WHERE did=? UNION SELECT 1 FROM stock WHERE number=? UNION SELECT 1 FROM departments WHERE did=?", (num, num, num)).fetchone(): raise ApiError(409, "that number is already in use or in the pool")
            pid = telnyx_phone_id(num)
            if not pid: raise ApiError(404, f"{num} was not found in your Telnyx account")
            if C["telnyx_connection_id"]:                    # make sure its calls come to this server
                telnyx("PATCH", f"/phone_numbers/{pid}", {"connection_id": C["telnyx_connection_id"]})
            try: sms_attach(pid)
            except Exception: pass
            con.execute("INSERT INTO stock (number, phone_id, bought_at, note, available_at) VALUES (?,?,?,?,?)", (num, pid, iso(now()), "added from Telnyx portal", iso(now())))
            event(con, "admin", "stock_adopt", num); con.commit()
            return {"ok": True, "number": num}
        if len(p) == 4 and p[:2] == ["telnyx", "stock"] and p[3] == "ready" and method == "POST":
            con.execute("UPDATE stock SET available_at=? WHERE number=?", (iso(now()), digits(p[2]))); event(con, "admin", "stock_ready", digits(p[2])); con.commit()
            return {"ok": True}
        if len(p) == 4 and p[:2] == ["telnyx", "stock"] and p[3] == "release" and method == "POST":
            st = con.execute("SELECT * FROM stock WHERE number=?", (digits(p[2]),)).fetchone()
            if not st: raise ApiError(404, "not in stock")
            if st["phone_id"]: telnyx("DELETE", f"/phone_numbers/{st['phone_id']}")
            con.execute("DELETE FROM stock WHERE number=?", (st["number"],)); event(con, "admin", "stock_release", st["number"]); con.commit()
            return {"ok": True}
        if p == ["quality"] and method == "GET":                    # admin: call quality of the last N days
            days = max(1, min(90, int(q.get("days") or 7))); since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
            rows = con.execute("""SELECT k.id, k.sip_user, k.direction, k.peer, k.started_at, k.billsec, k.q_jitter, k.q_loss, k.q_rtt, k.q_mos, k.q_ip, k.q_dev,
                                  c.email, c.ext, c.display_name, c.company_id FROM calls k LEFT JOIN customers c ON c.sip_user=k.sip_user
                                  WHERE k.started_at>=? AND k.q_mos IS NOT NULL ORDER BY k.id DESC""", (since,)).fetchall()
            calls = [dict(r, grade=qos_grade(r["q_mos"])) for r in rows]
            def summ(lst):
                n = len(lst)
                if not n: return {"calls": 0}
                return {"calls": n, "mos": round(sum(x["q_mos"] for x in lst) / n, 2), "jitter": round(sum(x["q_jitter"] for x in lst) / n, 1),
                        "loss": round(sum(x["q_loss"] for x in lst) / n, 2), "rtt": round(sum(x["q_rtt"] for x in lst) / n, 1),
                        "good": sum(1 for x in lst if x["grade"] == "good"), "fair": sum(1 for x in lst if x["grade"] == "fair"), "poor": sum(1 for x in lst if x["grade"] == "poor")}
            by_line, by_dev = {}, {}
            for x in calls:
                by_line.setdefault(x["sip_user"], []).append(x); by_dev.setdefault(x["q_dev"] or "app", []).append(x)
            lines = []
            for u, lst in by_line.items():
                d = summ(lst); d.update({"sip_user": u, "email": lst[0]["email"], "ext": lst[0]["ext"], "name": lst[0]["display_name"], "last_ip": lst[0]["q_ip"]}); lines.append(d)
            lines.sort(key=lambda d: d["mos"])
            measured = con.execute("SELECT COUNT(*) FROM calls WHERE started_at>=? AND billsec>0", (since,)).fetchone()[0]
            return {"days": days, "overall": summ(calls), "answered_calls": measured, "lines": lines,
                    "devices": {k: summ(v) for k, v in by_dev.items()}, "worst": sorted(calls, key=lambda x: x["q_mos"])[:25]}
        if p == ["telnyx", "costs"] and method == "GET":
            lines, cos = line_costs(con)
            return {"lines": lines, "companies": cos, "rates": C["rates"]}
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
            if body.get("billing") in ("paypal", "invoice", "free"): con.execute("UPDATE customers SET billing=? WHERE sip_user=?", (body["billing"], u))
            con.commit()
            want = digits(body.get("phone_number")); want = ("1" + want) if len(want) == 10 else want
            taken = {r[0] for r in con.execute("SELECT did FROM customers WHERE did IS NOT NULL")}
            num, pid = get_number(con, want, digits(body.get("area_code")), taken)
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
                bm = body.get("billing") if body.get("billing") in ("paypal", "invoice", "free") else "paypal"
                cur = con.execute("INSERT INTO companies (name, owner_email, period_anchor, created_at, sub_ref, ivr_json, hours_json, billing, comp) VALUES (?,?,?,?,?,?,?,?,?)",
                                  (name, email, now().date().isoformat(), iso(now()), ref or None, "{}", "{}", bm, 1 if bm == "free" else 0))
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
            if sub == "departments" and len(p) == 5 and p[4] == "number" and p[3].isdigit() and method == "POST":   # admin: a department's direct number
                dept_number(con, co, int(p[3]), str(body.get("number") or "")); con.commit()
                return company_json(con, company(con, cid))
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
                    if re.fullmatch(r"[0-9]", str(k)) and isinstance(v, dict) and v.get("type") in ("ext", "group", "vm", "repeat", "dept"):
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
            if sub == "billing" and method == "POST":      # admin: paypal | invoice | free (free = complimentary)
                bm = str(body.get("billing") or "")
                if bm not in ("paypal", "invoice", "free"): raise ApiError(400, "billing must be paypal, invoice or free")
                con.execute("UPDATE companies SET billing=?, comp=? WHERE id=?", (bm, 1 if bm == "free" else 0, cid))
                event(con, co["main_user"] or "", "billing", f"company {cid} {bm}"); con.commit()
                return company_json(con, company(con, cid))
            if sub == "note" and method == "POST":
                con.execute("UPDATE companies SET admin_note=? WHERE id=?", (str(body.get("note") or "")[:1000], cid)); con.commit()
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
            if sub == "parked" and method == "GET": return {"parked": parked_calls(con, cid)}
            if sub == "departments" and method == "GET": return {"departments": dept_list(con, cid)}
            if sub == "departments" and method == "POST":  # create / rename / set members / delete a department
                did_ = int(body.get("id") or 0)
                if body.get("delete") and did_:
                    if con.execute("SELECT 1 FROM departments WHERE id=? AND company_id=? AND did IS NOT NULL", (did_, cid)).fetchone():
                        dept_number(con, co, did_, "")              # its direct number goes back to the pool
                    con.execute("DELETE FROM departments WHERE id=? AND company_id=?", (did_, cid))
                else:
                    name = re.sub(r"\s+", " ", str(body.get("name") or "")).strip()[:40]
                    if not name: raise ApiError(400, "department name required")
                    valid = {m["ext"] for m in co_members(con, cid)}
                    exts = [str(e) for e in (body.get("exts") or []) if str(e) in valid]
                    if con.execute("SELECT 1 FROM departments WHERE company_id=? AND lower(name)=lower(?) AND id<>?", (cid, name, did_)).fetchone():
                        raise ApiError(409, "there is already a department with that name")
                    if did_:
                        if not con.execute("SELECT 1 FROM departments WHERE id=? AND company_id=?", (did_, cid)).fetchone(): raise ApiError(404, "no such department")
                        con.execute("UPDATE departments SET name=?, exts=? WHERE id=?", (name, json.dumps(exts), did_))
                    else:
                        con.execute("INSERT INTO departments (company_id, name, exts, created_at) VALUES (?,?,?,?)", (cid, name, json.dumps(exts), iso(now())))
                event(con, co["main_user"] or "", "dept_set", str(body.get("name") or did_)[:60]); con.commit()
                return {"departments": dept_list(con, cid)}
            if sub == "board" and method == "GET":         # the office app's live board: everyone's state + departments
                team = team_json(con, co)
                for d in (dl := dept_list(con, cid)):
                    st = {t["ext"]: t["state"] for t in team}
                    d["free"] = sum(1 for e in d["exts"] if st.get(e) == "available"); d["busy"] = sum(1 for e in d["exts"] if st.get(e) == "busy")
                return {"team": team, "departments": dl, "at": iso(now())}
            if sub == "calls" and method == "GET":         # company call history (who answered, for which department)
                ms = co_members(con, cid); names = {m["sip_user"]: {"name": m["display_name"] or "", "ext": m["ext"] or ""} for m in ms}
                users = list(names) or [""]
                lim = max(1, min(500, int(q.get("limit") or 100)))
                where = "(sip_user IN (%s) OR answered_by IN (%s))" % (",".join("?" * len(users)), ",".join("?" * len(users)))
                args_ = users + users
                if q.get("user"): where += " AND (sip_user=? OR answered_by=?)"; args_ += [q["user"], q["user"]]
                if q.get("before"): where += " AND id<?"; args_.append(int(q["before"]))
                rows = con.execute(f"SELECT id, sip_user, direction, peer, started_at, billsec, result, answered_by, dept, cid_used FROM calls WHERE {where} ORDER BY id DESC LIMIT {lim}", args_).fetchall()
                out = []
                for r in rows:
                    d = dict(r); d["line"] = names.get(r["sip_user"], {}); d["answered"] = names.get(r["answered_by"] or "", {}) if r["answered_by"] else None
                    d.pop("sip_user", None); d.pop("answered_by", None); out.append(d)
                return {"calls": out}
            if sub == "moh":                               # the company's own hold music
                if method == "GET": return {"moh": [f[:-4] for f in moh_files(cid)]}
                if method == "POST" and body.get("delete"):
                    nm = re.sub(r"[^A-Za-z0-9 _.-]", "", str(body["delete"]))[:60]
                    try: os.remove(os.path.join(moh_dir(cid), nm + ".wav"))
                    except OSError: pass
                    write_moh(con); write_pjsip(con)
                    return {"moh": [f[:-4] for f in moh_files(cid)]}
                if method == "POST" and body.get("audio_b64"):
                    import base64 as b64
                    if len(moh_files(cid)) >= 20: raise ApiError(400, "up to 20 songs — delete one first")
                    data = b64.b64decode(body["audio_b64"])[:25_000_000]
                    if len(data) < 5000: raise ApiError(400, "that file is too small to be a song")
                    nm = re.sub(r"[^A-Za-z0-9 _.-]", "", os.path.splitext(str(body.get("name") or "song"))[0]).strip()[:60] or "song"
                    os.makedirs(moh_dir(cid), exist_ok=True)
                    src = os.path.join(moh_dir(cid), ".upload"); open(src, "wb").write(data)
                    try:
                        if not to_8k(src, os.path.join(moh_dir(cid), nm), max_secs=900): raise ApiError(400, "could not read that audio file (use MP3, WAV or M4A)")
                    finally:
                        try: os.remove(src)
                        except OSError: pass
                    write_moh(con); write_pjsip(con)
                    return {"moh": [f[:-4] for f in moh_files(cid)]}
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
                d["recent_calls"] = [dict(r) for r in con.execute("SELECT direction, peer, started_at, billsec, minutes, result, q_mos, q_jitter, q_loss, q_rtt, q_dev FROM calls WHERE sip_user=? ORDER BY id DESC LIMIT 50", (u,))]
                d["texts"] = [dict(r) for r in con.execute("SELECT id, peer, body, received_at FROM texts WHERE sip_user=? ORDER BY id DESC LIMIT 50", (u,))]
                d["voicemails"] = [dict(r) for r in con.execute("SELECT id, caller, secs, created_at, heard FROM voicemails WHERE sip_user=? ORDER BY id DESC LIMIT 50", (u,))]
                co = co_of(con, c)
                if co: d["company"] = {"id": co["id"], "name": co["name"], "owner": co["owner_email"] == c["email"], "ext": c["ext"], "main_did": co_main_did(con, co)}
                return d
            if sub == "desk":                                    # desk phone login + automatic setup
                if method == "POST":
                    on = bool(body.get("on", True))
                    brand = str(body.get("brand") or c["desk_brand"] or "")
                    if brand not in ("", "yealink", "grandstream", "mitel", "other"): raise ApiError(400, "unknown brand")
                    mac = mac_norm(body.get("mac")) if "mac" in body else (c["desk_mac"] or "")
                    if mac and len(mac) != 12: raise ApiError(400, "the MAC address has 12 letters/digits, e.g. 80:5E:C0:12:34:56")
                    if mac and con.execute("SELECT 1 FROM customers WHERE desk_mac=? AND sip_user<>?", (mac, u)).fetchone(): raise ApiError(409, "that phone is already set up on another extension")
                    pw = (c["desk_pass"] if c["desk_pass"] and not body.get("new_password") else secrets.token_urlsafe(15).replace("-", "x").replace("_", "y")) if on else None
                    con.execute("UPDATE customers SET desk_pass=?, desk_brand=?, desk_mac=? WHERE sip_user=?", (pw, brand if on else None, mac if on else None, u))
                    co = co_of(con, c)
                    if co and not co["prov_token"]: con.execute("UPDATE companies SET prov_token=? WHERE id=?", (secrets.token_urlsafe(12), co["id"]))
                    event(con, u, "desk", "on" if on else "off"); con.commit(); write_pjsip(con)
                    c = con.execute("SELECT * FROM customers WHERE sip_user=?", (u,)).fetchone()
                co = co_of(con, c)
                ext_file = {"yealink": "{mac}.cfg", "grandstream": "cfg{mac}.xml", "mitel": "{mac}.cfg"}.get(c["desk_brand"] or "", "")
                return {"on": bool(c["desk_pass"]), "server": C["sip_domain"], "username": u + "-desk", "password": c["desk_pass"] or "",
                        "ports": {"udp": 5060, "tcp": 5060, "tls": 5061}, "brand": c["desk_brand"] or "", "mac": c["desk_mac"] or "",
                        "prov_url": f"https://{C['sip_domain']}/prov/{co['prov_token']}/" if co and co["prov_token"] else "", "prov_file": ext_file,
                        "registered": bool(ast_rx(f"pjsip show aor {u}-desk").count("Contact:  " + u + "-desk/")) if c["desk_pass"] else False}
            if sub == "team" and method == "GET":              # the company's extensions and who is free right now
                co = co_of(con, c)
                return {"team": team_json(con, co, u) if co else []}
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
                        if con.execute("SELECT 1 FROM customers WHERE did=?", (to,)).fetchone() or to == digits(C["company_did"] or ""):
                            raise ApiError(400, "that's a GlobaleSIM number — forward to its extension instead")
                con.execute("UPDATE customers SET fwd_mode=?, fwd_to=?, dnd=? WHERE sip_user=?", (mode, to if mode else (c["fwd_to"] or ""), 1 if body.get("dnd") else 0, u))
                event(con, u, "forward_set", f"{mode or 'off'} {to} dnd={1 if body.get('dnd') else 0}"); con.commit()
                c = con.execute("SELECT * FROM customers WHERE sip_user=?", (u,)).fetchone()
                return cust_json(con, c)
            if sub == "push" and method == "POST":             # iPhone app: where to send "incoming call" notifications
                tk = str(body.get("token") or "").lower()
                if not re.fullmatch(r"[0-9a-f]{64,200}", tk): raise ApiError(400, "bad token")
                con.execute("UPDATE customers SET push_token=NULL WHERE push_token=? AND sip_user<>?", (tk, u))   # phone moved to another line
                con.execute("UPDATE customers SET push_token=?, push_platform=?, push_at=? WHERE sip_user=?",
                            (tk, str(body.get("platform") or "ios")[:10], iso(now()), u))
                con.commit(); return {"ok": True}
            if sub == "push" and method == "DELETE":
                con.execute("UPDATE customers SET push_token=NULL WHERE sip_user=?", (u,)); con.commit(); return {"ok": True}
            if sub == "webcreds" and method == "GET":         # computer browser login (website checked the owner)
                if c["status"] != "active": raise ApiError(403, "line not active")
                if not c["web_pass"] or not c["web_exp"] or c["web_exp"] < iso(now() + dt.timedelta(hours=2)):
                    con.execute("UPDATE customers SET web_pass=?, web_exp=? WHERE sip_user=?", (secrets.token_urlsafe(18), iso(now() + dt.timedelta(hours=24)), u))
                    con.commit(); write_pjsip(con)
                    c = con.execute("SELECT * FROM customers WHERE sip_user=?", (u,)).fetchone()
                return {"sip_user": u + "-web", "sip_pass": c["web_pass"], "domain": C["sip_domain"], "wss": f"wss://{C['sip_domain']}/ws", "expires": c["web_exp"]}
            if sub == "credentials" and method == "GET":     # for the app, after the website verified the signed-in owner
                return {"sip_user": u, "sip_pass": c["sip_pass"], "sip_server": C["sip_domain"], "did": c["did"]}
            if sub == "number" and method == "POST":           # admin: give this line a number (pool first, else buy) + 911; change=true swaps it
                old = dict(c) if c["did"] else None
                if old and not body.get("change"): raise ApiError(409, "this line already has a number")
                a911 = body.get("e911") or {}
                if not a911.get("street") and c["e911_json"]: a911 = json.loads(c["e911_json"])     # keep the address on file
                if not a911.get("street") and c["company_id"]:          # an extension's direct number: the company's 911 address
                    cm = con.execute("SELECT e911_json FROM customers WHERE sip_user=(SELECT main_user FROM companies WHERE id=?)", (c["company_id"],)).fetchone()
                    if cm and cm["e911_json"]: a911 = json.loads(cm["e911_json"])
                if old:
                    want = digits(body.get("phone_number")); want = ("1" + want) if len(want) == 10 else want
                    if want and want == old["did"]: raise ApiError(400, "that is already this line's number")
                    con.execute("UPDATE customers SET did=NULL, e911=0, e911_address_id=NULL WHERE sip_user=?", (u,))
                try:
                    buy_for(con, u, body.get("phone_number"), body.get("area_code"), a911)
                except ApiError:
                    if old: con.execute("UPDATE customers SET did=?, e911=?, e911_address_id=? WHERE sip_user=?", (old["did"], old["e911"], old["e911_address_id"], u))
                    raise
                if old: to_pool(con, old, "number changed")        # old number (and its 911) off the line, into the pool
                if old and old.get("backup_to"):                    # the backup number follows the line to its new number
                    n = con.execute("SELECT * FROM customers WHERE sip_user=?", (u,)).fetchone()
                    try:
                        if n["phone_id"]: telnyx_backup(n["phone_id"], old["backup_to"]); con.execute("UPDATE customers SET backup_to=? WHERE sip_user=?", (old["backup_to"], u))
                    except ApiError as e: event(con, u, "backup_failed", e.msg[:150])
                con.commit(); write_pjsip(con)
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
            elif sub == "billing" and method == "POST":        # admin: how this line pays — paypal | invoice | free
                bm = str(body.get("billing") or "")
                if bm not in ("paypal", "invoice", "free"): raise ApiError(400, "billing must be paypal, invoice or free")
                con.execute("UPDATE customers SET billing=? WHERE sip_user=?", (bm, u)); event(con, u, "billing", bm)
            elif sub == "backup" and method == "POST":         # backup number: where calls go if our voice server is down
                to = digits(str(body.get("to") or ""))
                if to:
                    to = parse_nanp(to)
                    if not to: raise ApiError(400, "enter a US or Canada number")
                    if to == (c["did"] or ""): raise ApiError(400, "the backup number must be a different phone")
                    if con.execute("SELECT 1 FROM customers WHERE did=? AND status<>'canceled'", (to,)).fetchone() or to == digits(C["company_did"] or ""):
                        raise ApiError(400, "use a phone outside GlobaleSIM, like a cell phone (a GlobaleSIM number would be down too)")
                if not c["did"]: raise ApiError(400, "this line has no phone number yet")
                if not c["phone_id"]:
                    pid = telnyx_phone_id(c["did"])
                    if not pid: raise ApiError(404, f"number {c['did']} was not found in your Telnyx account")
                    con.execute("UPDATE customers SET phone_id=?, number_id=COALESCE(number_id, ?) WHERE sip_user=?", (pid, pid, u))
                    c = con.execute("SELECT * FROM customers WHERE sip_user=?", (u,)).fetchone()
                telnyx_backup(c["phone_id"], to)
                con.execute("UPDATE customers SET backup_to=? WHERE sip_user=?", (to or None, u)); event(con, u, "backup_set", to or "off")
            elif sub == "e911" and method == "POST":           # admin / customer: (re)register the 911 address of this line's number
                a = body.get("e911") or {}
                if not a.get("street") or not a.get("zip") or not a.get("city"): raise ApiError(400, "needs a full address (street, city, state, ZIP)")
                if not c["did"]: raise ApiError(400, "this line has no phone number yet")
                if not c["phone_id"]:                          # number added by hand / adopted: find its Telnyx id from the number
                    pid = telnyx_phone_id(c["did"])
                    if not pid: raise ApiError(404, f"number {c['did']} was not found in your Telnyx account")
                    con.execute("UPDATE customers SET phone_id=?, number_id=COALESCE(number_id, ?) WHERE sip_user=?", (pid, pid, u))
                    c = con.execute("SELECT * FROM customers WHERE sip_user=?", (u,)).fetchone()
                aid = telnyx_e911(c["phone_id"], a)
                con.execute("UPDATE customers SET e911=1, e911_address_id=?, e911_json=? WHERE sip_user=?", (aid, json.dumps(a), u)); event(con, u, "e911", "address set by admin")
            elif sub == "email" and method == "POST":          # admin: change the sign-in email of a line (owner email too, if it runs a company)
                em = str(body.get("email") or "").strip().lower()
                if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", em): raise ApiError(400, "enter a valid email")
                if em != c["email"] and con.execute("SELECT 1 FROM customers WHERE email=? AND status<>'canceled' AND sip_user<>?", (em, u)).fetchone():
                    raise ApiError(409, "another active line already uses that email")
                con.execute("UPDATE customers SET email=? WHERE sip_user=?", (em, u))
                con.execute("UPDATE companies SET owner_email=? WHERE main_user=?", (em, u))
                event(con, u, "email", f"{c['email']} -> {em}")
            elif sub == "note" and method == "POST":
                con.execute("UPDATE customers SET admin_note=? WHERE sip_user=?", (str(body.get("note") or "")[:1000], u))
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
            if method == "GET" and self.path.startswith("/prov/"):
                parts = self.path.split("?")[0].split("/")
                res = prov_lookup(parts[2], parts[3]) if len(parts) == 4 else None
                body_b = (res[0] if res and res[0] else "not found\n").encode()
                self.send_response(200 if res and res[0] else 404); self.send_header("Content-Type", res[1] if res else "text/plain")
                self.send_header("Content-Length", str(len(body_b))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body_b)
                return
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
            if method == "GET" and self.path.split("?")[0] == "/api/ping":   # outside watchdog (no secret needed)
                res = ping(); code = 200 if res["ok"] else 503
                b = json.dumps(res).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
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
    with db() as con:
        write_pjsip(con)
        try: write_moh(con)            # picks up the radio-style hold music after an update
        except Exception as e: print(f"moh: {e}", flush=True)
    threading.Thread(target=sms_retry_loop, daemon=True).start()
    threading.Thread(target=alert_loop, daemon=True).start()
    ThreadingHTTPServer((C["api_listen"], int(C["api_port"])), Handler).serve_forever()

if __name__ == "__main__":
    if sys.argv[1:2] == ["api"]: api_main()
    elif sys.argv[1:2] == ["agi"]: agi_main(sys.argv[2:])
    elif sys.argv[1:2] == ["daily"]: daily_main()
    elif sys.argv[1:2] == ["report"]: report_main()
    elif sys.argv[1:2] == ["sms-setup"]: sms_setup(os.environ.get("GSVOICE_CONF", "/etc/gsvoice/gsvoice.json"))
    elif sys.argv[1:2] == ["pushtest"]:        # pushtest [line] — check the Apple push key; with a line: send it a test alert
        if not apns_ready(): print("Apple push is not set up yet (run apns-setup.sh)"); sys.exit(1)
        apns_jwt(); print("Apple push key OK")
        if len(sys.argv) > 2:
            with db() as con: r = con.execute("SELECT push_token FROM customers WHERE sip_user=? OR did=? OR email=?", (sys.argv[2],) * 3).fetchone()
            if not r or not r["push_token"]: print("that line has no iPhone signed in yet (open the Phone tab in the iPhone app)"); sys.exit(1)
            st = apns_send_many([(r["push_token"], call_push_payload("", "test", "GlobaleSIM test", "call alerts work ✓"), "test")])
            print("Apple answered:", list(st.values())[0], "(200 = delivered)")
    elif sys.argv[1:2] == ["regen"]:
        with db() as con: write_pjsip(con)
    else: print(__doc__)
