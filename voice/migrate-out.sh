#!/bin/bash
# Move the GlobaleSIM voice server to a new machine — run as root ON THE OLD SERVER:
#   curl -fsSL https://raw.githubusercontent.com/ncaseiro/globalesim-android/<ref>/voice/migrate-out.sh -o migrate-out.sh && bash migrate-out.sh <new-server-ip>
# 1st run: prints one line to paste on the new server (lets this server copy files there). 2nd run: the move:
#   stops the phone service here, copies settings + customer database + greetings + voicemail + hold music to the new server,
#   and turns the phone service off here (so both never answer at once). Undo line is printed at the end.
set -e
NEW=${1:-}
REF=${GS_REF:-voice}
[ "$(id -u)" = 0 ] || { echo "Run as root."; exit 1; }
[ -n "$NEW" ] || { echo "Usage: bash migrate-out.sh <new-server-ip>"; exit 1; }
KEY=/root/.ssh/gsmigrate_ed25519
mkdir -p /root/.ssh; chmod 700 /root/.ssh
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "gs-migrate@$(hostname)" -f "$KEY"
S="ssh -i $KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 root@$NEW"
if ! $S true 2>/dev/null; then
  cat <<EOF

================  ONE STEP ON THE NEW SERVER FIRST  ================
Open the NEW server ($NEW) — DigitalOcean → the droplet → Web Console — and paste this ONE line:

mkdir -p /root/.ssh && echo '$(cat "$KEY.pub")' >> /root/.ssh/authorized_keys && echo KEY-ADDED

Then come back here and run the same command again:  bash migrate-out.sh $NEW
=====================================================================
EOF
  exit 2
fi

DB=$(python3 -c "import json;print(json.load(open('/etc/gsvoice/gsvoice.json')).get('db') or '/var/lib/gsvoice/gsvoice.db')")
echo "Connected to $NEW. Stopping the phone service here — calls pause until the new server is up (about 10 minutes)…"
systemctl stop asterisk gsvoice-api
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/root"
for p in /etc/globalesim-voice.conf /etc/gsvoice /var/lib/gsvoice /etc/ssh/sshd_config.d/00-keys-only.conf /etc/fail2ban/jail.d/zz-globalesim.local /etc/fail2ban/jail.d/zz-home.local; do
  [ -e "$p" ] && { mkdir -p "$T/root$(dirname "$p")"; cp -a "$p" "$T/root$p"; }
done
rm -f "$T/root$DB" "$T/root$DB-wal" "$T/root$DB-shm"
python3 - "$DB" "$T/root$DB" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=30); dst = sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close()
ok = sqlite3.connect(sys.argv[2]).execute("PRAGMA integrity_check").fetchone()[0]
n = sqlite3.connect(sys.argv[2]).execute("SELECT count(*) FROM customers").fetchone()[0]
if ok != "ok": sys.exit("database copy failed its check: " + ok)
print(f"database copied: {n} lines/extensions, check ok")
PY
echo "Copying to $NEW…"
tar czf - -C "$T/root" . | $S "tar xzf - -C / && echo copied-ok"
$S "test -s '$DB' && echo database-in-place"
systemctl disable asterisk gsvoice-api >/dev/null 2>&1 || true
cat <<EOF

================  COPIED — NOW ON THE NEW SERVER  ================
1. On the NEW server ($NEW), run the normal installer and press Enter at every question
   (your settings were copied, so the answers are already filled in):

curl -fsSL https://raw.githubusercontent.com/ncaseiro/globalesim-android/$REF/voice/install.sh -o install.sh && GS_REF=$REF bash install.sh

2. Point voice.globalesim.tel to $NEW in your DNS.
3. Test: call your number, and make a call from the app.

The phone service on THIS server is now off (it won't start again by itself).
If you need to go back:   systemctl enable --now asterisk gsvoice-api
=================================================================
EOF
