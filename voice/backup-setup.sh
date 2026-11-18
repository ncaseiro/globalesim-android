#!/bin/bash
# Nightly backup of the GlobaleSIM voice server to another server — run as root ON THE VOICE SERVER:
#   curl -fsSL https://raw.githubusercontent.com/ncaseiro/globalesim-android/<ref>/voice/backup-setup.sh -o backup-setup.sh && GS_REF=<ref> bash backup-setup.sh <backup-server-ip>
# Backs up: customer database (safe live copy), voice settings, Asterisk config, greetings, voicemail, web/SSH/fail2ban config.
# The key it makes can ONLY drop a backup file on the other server (nothing else).
set -e
REF=${GS_REF:-voice}
DEST=${1:-$GS_BACKUP_HOST}
[ "$(id -u)" = 0 ] || { echo "Run as root."; exit 1; }
[ -n "$DEST" ] || { echo "Usage: bash backup-setup.sh <backup-server-ip>"; exit 1; }

KEY=/root/.ssh/gsbackup_ed25519
mkdir -p /root/.ssh; chmod 700 /root/.ssh
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "gsvoice-backup@$(hostname)" -f "$KEY"
echo "$DEST" > /etc/gsvoice/backup-host

cat > /usr/local/bin/gs-backup <<'EOF'
#!/bin/bash
# Nightly: live-safe copy of the database + settings → the backup server. Logs to /var/log/gs-backup.log
set -o pipefail
DEST=$(cat /etc/gsvoice/backup-host 2>/dev/null); [ -n "$DEST" ] || { echo "no backup host"; exit 1; }
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
python3 - "$T/gsvoice.db" <<'PY'
import json, sqlite3, sys
db = json.load(open("/etc/gsvoice/gsvoice.json")).get("db") or "/var/lib/gsvoice/gsvoice.db"
src = sqlite3.connect(db, timeout=30); dst = sqlite3.connect(sys.argv[1]); src.backup(dst); dst.close()
ok = sqlite3.connect(sys.argv[1]).execute("PRAGMA integrity_check").fetchone()[0]
if ok != "ok": sys.exit("database copy failed its check: " + ok)
PY
[ $? -eq 0 ] || { logger -t gs-backup "FAILED: database copy"; echo "$(date -u) FAILED: database copy"; exit 1; }
cat > "$T/RESTORE.txt" <<TXT
GlobaleSIM voice server backup made $(date -u) on $(hostname).
To restore on a fresh server: run the normal voice installer first, then
  systemctl stop asterisk gsvoice-api
  tar xzf <this file> -C /restore && cp -a /restore/etc/gsvoice /restore/etc/asterisk /etc/ && cp -a /restore/var/lib/gsvoice/. /var/lib/gsvoice/
  cp /restore/gsvoice.db \$(python3 -c "import json;print(json.load(open('/etc/gsvoice/gsvoice.json')).get('db') or '/var/lib/gsvoice/gsvoice.db')")
  chown -R asterisk:asterisk /etc/gsvoice /var/lib/gsvoice && systemctl start asterisk gsvoice-api
TXT
P=(); for x in etc/gsvoice etc/asterisk etc/caddy/Caddyfile etc/ssh/sshd_config.d etc/fail2ban/jail.d var/lib/gsvoice/greet var/lib/gsvoice/vm; do [ -e "/$x" ] && P+=("$x"); done
OUT=$(tar czf - -C / "${P[@]}" -C "$T" gsvoice.db RESTORE.txt \
  | ssh -i /root/.ssh/gsbackup_ed25519 -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 "gsbackup@$DEST" 2>&1)
RC=$?
echo "$(date -u) rc=$RC $OUT"
[ $RC -eq 0 ] || logger -t gs-backup "FAILED: $OUT"
exit $RC
EOF
chmod 700 /usr/local/bin/gs-backup

# every night at 3:15 am Central (09:15 UTC)
echo "15 9 * * * root /usr/local/bin/gs-backup >> /var/log/gs-backup.log 2>&1" > /etc/cron.d/gs-backup
chmod 644 /etc/cron.d/gs-backup

PUB=$(cat "$KEY.pub")
cat <<EOF

================  STEP DONE ON THE VOICE SERVER  ================
Now log in to the backup server ($DEST) and paste this ONE command there:

curl -fsSL https://raw.githubusercontent.com/ncaseiro/globalesim-android/$REF/voice/watchdog-install.sh -o watchdog-install.sh && GS_REF=$REF BACKUP_KEY='$PUB' bash watchdog-install.sh

Then come back here and run a first backup:   gs-backup
(This key is not a password — it can only drop backup files there.)
=================================================================
EOF
