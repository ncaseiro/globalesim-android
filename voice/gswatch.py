#!/usr/bin/env python3
"""GlobaleSIM watchdog — runs on a DIFFERENT server than the voice server and pushes an alert to your phone
(ntfy app) when the phone service or the website stops answering, and again when it is back.

Checks every minute:
  voice   https://<voice>/api/ping says ok (Asterisk running, carrier connected, database readable)
          and the SIP TLS port the app uses (5061) accepts a secure connection
  site    the website answers
  backup  a nightly backup from the voice server arrived in the last 26 hours (if backups are set up)
Alerts after 2 failed checks in a row (no alarms for a one-second blip), a reminder every 30 minutes while down,
and a "back up" message with how long it was down.
"""
import json, os, socket, ssl, sys, time, urllib.request, glob

CONF = os.environ.get("GSWATCH_CONF", "/etc/gswatch.json")
STATE = "/var/lib/gswatch/state.json"

def load(p, d):
    try:
        with open(p) as f: return json.load(f)
    except Exception: return d

def save(p, d):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w") as f: json.dump(d, f)
    os.replace(p + ".tmp", p)

def push(c, title, msg, urgent=False, tags="warning"):
    """ntfy push notification (free app for Android/iPhone). Never contains customer data."""
    try:
        req = urllib.request.Request(c.get("ntfy_server", "https://ntfy.sh").rstrip("/") + "/" + c["topic"], data=msg.encode(), method="POST",
                                     headers={"Title": title, "Priority": "urgent" if urgent else "default", "Tags": tags})
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        sys.stderr.write("push failed %r\n" % e); return False

def check_voice(c):
    host = c["voice_host"]
    try:
        with urllib.request.urlopen("https://%s/api/ping" % host, timeout=12) as r: j = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try: j = json.loads(e.read() or b"{}")
        except Exception: j = {}
        if not j: return False, "the voice API answered with error %s" % e.code
    except Exception as e:
        return False, "the voice server is not answering (%s)" % (getattr(e, "reason", None) or e.__class__.__name__)
    if not j.get("ok"):
        why = []
        if not j.get("asterisk"): why.append("the phone system (Asterisk) is not running")
        if j.get("trunk") is False: why.append("the connection to Telnyx is down")
        if not j.get("db"): why.append("the customer database can't be read")
        return False, "; ".join(why) or "the voice server says it is not ok"
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, int(c.get("sip_tls_port", 5061))), timeout=10) as s:
            with ctx.wrap_socket(s, server_hostname=host): pass
    except ssl.SSLCertVerificationError:
        return False, "the SIP security certificate is invalid or expired — the app can't connect"
    except Exception as e:
        return False, "the app's SIP port %s is not accepting connections (%s)" % (c.get("sip_tls_port", 5061), e.__class__.__name__)
    return True, ""

def check_site(c):
    try:
        req = urllib.request.Request(c["site_url"], headers={"User-Agent": "GlobaleSIM-watchdog"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return (200 <= r.status < 400), "the website answered %s" % r.status
    except urllib.error.HTTPError as e: return False, "the website answered with error %s" % e.code
    except Exception as e: return False, "the website is not answering (%s)" % (getattr(e, "reason", None) or e.__class__.__name__)

def check_backup(c):
    d = c.get("backup_dir") or ""
    if not d or not os.path.isdir(d): return True, ""
    files = glob.glob(os.path.join(d, "gsvoice-*.tar.gz"))
    if not files: return True, ""                       # not set up yet / first night
    age = (time.time() - max(os.path.getmtime(f) for f in files)) / 3600
    return (age < 26), "the last backup from the voice server is %d hours old" % age

NAMES = {"voice": "Phone service", "site": "Website", "backup": "Nightly backup"}

def step(c, st, key, ok, why):
    s = st.setdefault(key, {"fails": 0, "down": False, "since": 0, "last_alert": 0})
    t = time.time()
    if ok:
        if s["down"]:
            mins = max(1, int((t - s["since"]) / 60))
            push(c, "OK: %s is back" % NAMES[key], "%s is working again after about %d minute(s)." % (NAMES[key], mins), tags="white_check_mark")
        s.update(fails=0, down=False, since=0)
        return
    s["fails"] += 1
    if s["fails"] == 1: s["since"] = t
    need = 1 if key == "backup" else int(c.get("fails_before_alert", 2))
    if not s["down"] and s["fails"] >= need:
        s["down"] = True; s["last_alert"] = t
        extra = {"voice": " Calls to numbers with a backup phone go to that phone. Check the server in DigitalOcean, or run: systemctl status asterisk gsvoice-api",
                 "site": " The phone service itself may still work.",
                 "backup": " Check the voice server: run gs-backup by hand to see the error."}[key]
        push(c, "DOWN: %s" % NAMES[key], why[:300] + "." + extra, urgent=(key == "voice"), tags="rotating_light" if key == "voice" else "warning")
    elif s["down"] and key != "backup" and t - s["last_alert"] > 1800:
        s["last_alert"] = t
        push(c, "STILL DOWN: %s" % NAMES[key], "Down for about %d minutes: %s." % ((t - s["since"]) / 60, why[:250]), urgent=(key == "voice"), tags="rotating_light")

def main():
    c = load(CONF, None)
    if not c or not c.get("topic"): sys.exit("no config at " + CONF)
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("voice:", check_voice(c)); print("site:", check_site(c)); print("backup:", check_backup(c))
        print("push:", push(c, "Test: GlobaleSIM watchdog", "If you can read this, alerts reach your phone.", tags="bell")); return
    last_backup_check = 0
    while True:
        st = load(STATE, {})
        try:
            step(c, st, "voice", *check_voice(c))
            if c.get("site_url"): step(c, st, "site", *check_site(c))
            if time.time() - last_backup_check > 900:
                last_backup_check = time.time(); step(c, st, "backup", *check_backup(c))
            save(STATE, st)
        except Exception as e:
            sys.stderr.write("watchdog error %r\n" % e)
        time.sleep(int(c.get("interval", 60)))

if __name__ == "__main__":
    main()
