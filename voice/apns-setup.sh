#!/bin/bash
# GlobaleSIM voice server: add the Apple Push Notifications key, so iPhones get "Incoming call" alerts
# when the app is closed.  Run on the voice server as root:
#   bash apns-setup.sh
# It asks for the Key ID and the key file's text (paste it — it stays on this server, never shown again).
set -euo pipefail
GSJ=/etc/gsvoice/gsvoice.json; KEYF=/etc/gsvoice/apns.p8
[ -f "$GSJ" ] || { echo "Run this on the voice server (no $GSJ here)."; exit 1; }

echo "== Apple push key for iPhone call alerts =="
read -rp "Key ID (10 letters/numbers, from Apple → Keys): " KID
KID=$(echo "$KID" | tr -d '[:space:]')
[[ "$KID" =~ ^[A-Z0-9]{10}$ ]] || { echo "That doesn't look like a Key ID."; exit 1; }
read -rp "Team ID [CCV35YQVGU]: " TEAM; TEAM=${TEAM:-CCV35YQVGU}
echo
echo "Now paste the WHOLE text of the AuthKey_${KID}.p8 file (open it in Notepad, Ctrl+A, Ctrl+C),"
echo "then press Enter. It ends with the line -----END PRIVATE KEY-----"
TMP=$(mktemp); chmod 600 "$TMP"
while IFS= read -r line; do
  echo "$line" >> "$TMP"
  [[ "$line" == *"END PRIVATE KEY"* ]] && break
done
openssl pkey -in "$TMP" -noout 2>/dev/null || { rm -f "$TMP"; echo "That isn't a valid key — nothing changed. Try again."; exit 1; }
install -m 600 -o asterisk -g asterisk "$TMP" "$KEYF"; rm -f "$TMP"

python3 - "$GSJ" "$KID" "$TEAM" "$KEYF" <<'PY'
import json, sys
p, kid, team, kf = sys.argv[1:]
c = json.load(open(p))
c["apns"] = {"key_file": kf, "key_id": kid, "team_id": team, "topic": "tel.globalesim.app", "host": "api.push.apple.com"}
json.dump(c, open(p + ".tmp", "w"), indent=2); import os; os.replace(p + ".tmp", p)
PY
chown asterisk:asterisk "$GSJ"; chmod 640 "$GSJ"
systemctl restart gsvoice-api
sudo -u asterisk GSVOICE_CONF="$GSJ" python3 /opt/gsvoice/gsvoice.py pushtest || true
echo
echo "Done. Test it on your iPhone line:  sudo -u asterisk python3 /opt/gsvoice/gsvoice.py pushtest <your phone number>"
