#!/bin/bash
# GlobaleSIM watchdog — run as root on a server OTHER than the voice server (e.g. the San Francisco droplet).
#   curl -fsSL https://raw.githubusercontent.com/ncaseiro/globalesim-android/<ref>/voice/watchdog-install.sh -o watchdog-install.sh && GS_REF=<ref> bash watchdog-install.sh
# Safe to run again (keeps your alert channel). With BACKUP_KEY='ssh-ed25519 …' it also sets up a place for the
# voice server's nightly backups: a user that can ONLY drop a backup file, nothing else.
set -e
umask 022
REF=${GS_REF:-voice}
RAW="https://raw.githubusercontent.com/ncaseiro/globalesim-android/$REF/voice"
[ "$(id -u)" = 0 ] || { echo "Run as root."; exit 1; }
command -v python3 >/dev/null || { apt-get update -qq; apt-get install -y -qq python3 >/dev/null; }

mkdir -p /opt/gswatch /var/lib/gswatch
curl -fsSL "$RAW/gswatch.py" -o /opt/gswatch/gswatch.py.new && mv /opt/gswatch/gswatch.py.new /opt/gswatch/gswatch.py
python3 -m py_compile /opt/gswatch/gswatch.py

CONF=/etc/gswatch.json
if [ -f "$CONF" ]; then
  echo "Keeping your settings in $CONF"
else
  TOPIC="gs-alerts-$(head -c 9 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  read -rp "Voice server name [voice.globalesim.tel]: " VH; VH=${VH:-voice.globalesim.tel}
  read -rp "Website to watch [https://globalesim.tel/]: " SU; SU=${SU:-https://globalesim.tel/}
  cat > "$CONF" <<EOF
{"topic": "$TOPIC", "ntfy_server": "https://ntfy.sh", "voice_host": "$VH", "sip_tls_port": 5061,
 "site_url": "$SU", "interval": 60, "fails_before_alert": 2, "backup_dir": ""}
EOF
  chmod 600 "$CONF"
fi

# ---- optional: receive the voice server's nightly backups ----
if [ -n "$BACKUP_KEY" ]; then
  case "$BACKUP_KEY" in ssh-ed25519\ *) ;; *) echo "BACKUP_KEY must be the ssh-ed25519 line the voice server printed."; exit 1;; esac
  id gsbackup >/dev/null 2>&1 || useradd -m -s /bin/sh gsbackup
  install -d -m 700 -o gsbackup -g gsbackup /home/gsbackup/backups /home/gsbackup/.ssh
  cat > /usr/local/bin/gs-backup-receive <<'EOF'
#!/bin/sh
# Only thing the voice server's backup key can do: save one backup from stdin, keep the newest 14.
umask 077
d=/home/gsbackup/backups; f="$d/gsvoice-$(date -u +%Y%m%d-%H%M%S).tar.gz"
head -c 3000000000 > "$f.part" || { rm -f "$f.part"; echo "receive failed"; exit 1; }
gzip -t "$f.part" 2>/dev/null || { rm -f "$f.part"; echo "backup was damaged in transit — not kept"; exit 1; }
mv "$f.part" "$f"
ls -1t "$d"/gsvoice-*.tar.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
echo "saved $(basename "$f") ($(du -h "$f" | cut -f1)) on $(hostname)"
EOF
  chmod 755 /usr/local/bin/gs-backup-receive
  echo "restrict,command=\"/usr/local/bin/gs-backup-receive\" $BACKUP_KEY" > /home/gsbackup/.ssh/authorized_keys
  chown gsbackup:gsbackup /home/gsbackup/.ssh/authorized_keys; chmod 600 /home/gsbackup/.ssh/authorized_keys
  python3 - "$CONF" <<'EOF'
import json, sys
p = sys.argv[1]; c = json.load(open(p)); c["backup_dir"] = "/home/gsbackup/backups"; json.dump(c, open(p, "w"))
EOF
  # key-only SSH policies elsewhere on this server still apply; this user has no password at all
  passwd -l gsbackup >/dev/null 2>&1 || true
  echo "Backup storage ready: /home/gsbackup/backups (newest 14 kept)"
fi

cat > /etc/systemd/system/gswatch.service <<'EOF'
[Unit]
Description=GlobaleSIM watchdog (alerts when the phone service or website is down)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/gswatch/gswatch.py
Restart=always
RestartSec=10
ProtectSystem=strict
ReadWritePaths=/var/lib/gswatch
PrivateTmp=yes
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable gswatch >/dev/null 2>&1
systemctl restart gswatch

echo
echo "Checking now and sending a test alert…"
python3 /opt/gswatch/gswatch.py test || true
TOPIC=$(python3 -c "import json;print(json.load(open('$CONF'))['topic'])")
cat <<EOF

================  WATCHDOG IS RUNNING  ================
To get the alerts on your phone:
  1. Install the free app "ntfy" (Play Store or App Store).
  2. Tap + (Subscribe to topic) and type exactly:

        $TOPIC

  3. You should then see the test message. Done.

Keep that topic name private — anyone who knows it can read the alerts.
Status any time:   systemctl status gswatch
Check by hand:     python3 /opt/gswatch/gswatch.py test
=======================================================
EOF
