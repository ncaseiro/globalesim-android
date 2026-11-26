#!/usr/bin/env bash
# GlobaleSIM Voice — call server installer (Asterisk + Telnyx or DIDLogic) for Ubuntu 24.04
# Safe to run again: it rewrites the config with the answers you give.
# Usage:  bash install.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
say(){ printf '\n\033[1;33m== %s ==\033[0m\n' "$*"; }
CONF=/etc/globalesim-voice.conf
PROVIDER=""; DL_SERVER=""; DL_USER=""; DL_PASS=""; DID=""; E911=""; TEST_PASS=""; TEST_USER=""
[ -f "$CONF" ] && . "$CONF"

# ---- Backups: gs-backup (every 30 min + before every install) and gs-restore (roll back) ----
mkdir -p /var/backups/gsvoice; chmod 700 /var/backups/gsvoice
cat > /usr/local/sbin/gs-backup <<'GSB'
#!/usr/bin/env bash
# gs-backup [label] — snapshot of the voice server settings + database. Keeps 48 recent (24 h) + 14 daily.
set -eu
B=/var/backups/gsvoice; L=${1:-auto}; T=$(date +%Y%m%d-%H%M%S); D="$B/$T-$L"; mkdir -p "$D"; chmod 700 "$D"
tar czf "$D/settings.tgz" --ignore-failed-read -C / etc/globalesim-voice.conf etc/gsvoice etc/asterisk opt/gsvoice usr/share/asterisk/agi-bin/gs-agi.py \
    etc/cron.d/gsvoice-adopt etc/cron.d/gsvoice-watch etc/cron.d/gsvoice-daily 2>/dev/null || true
if [ -f /var/lib/gsvoice/gsvoice.db ]; then
  python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" /var/lib/gsvoice/gsvoice.db "$D/gsvoice.db" && gzip -f "$D/gsvoice.db"
fi
tar czf "$D/files.tgz" --ignore-failed-read -C / --exclude=var/lib/gsvoice/gsvoice.db* --exclude=var/lib/gsvoice/moh var/lib/gsvoice 2>/dev/null || true
grep -h "carrier-reg" <(asterisk -rx "pjsip show registrations" 2>/dev/null) > "$D/registration.txt" 2>/dev/null || true
# retention: newest 48 'auto', 14 'daily', 20 of anything else (before-install, manual)
for k in auto:48 daily:14; do ls -1d "$B"/*-"${k%%:*}" 2>/dev/null | sort | head -n -"${k##*:}" | xargs -r rm -rf; done
ls -1d "$B"/* 2>/dev/null | grep -vE -- "-(auto|daily)$" | sort | head -n -20 | xargs -r rm -rf
echo "$D"
GSB
cat > /usr/local/sbin/gs-restore <<'GSB'
#!/usr/bin/env bash
# gs-restore — roll the voice server back to an earlier backup.
#   gs-restore            list backups and choose one (settings only — keeps today's customers, calls, voicemail)
#   gs-restore --with-db  also put the database back to that moment (loses anything that happened since)
set -eu
B=/var/backups/gsvoice; WITHDB=0; [ "${1:-}" = "--with-db" ] && WITHDB=1
mapfile -t L < <(ls -1d "$B"/* 2>/dev/null | sort -r | head -40)
[ ${#L[@]} -gt 0 ] || { echo "No backups yet."; exit 1; }
echo "Backups (newest first) — Telnyx sign-in at that moment shown on the right:"
for i in "${!L[@]}"; do n=$(basename "${L[$i]}"); r=$(grep -o "Registered\|Unregistered\|Rejected" "${L[$i]}/registration.txt" 2>/dev/null | head -1 || true)
  printf "  %2d) %s-%s-%s %s:%s  %-14s %s\n" $((i+1)) "${n:0:4}" "${n:4:2}" "${n:6:2}" "${n:9:2}" "${n:11:2}" "${n#*-*-}" "${r:-?}"; done
read -rp "Number to restore (Enter = cancel): " k; [ -n "$k" ] || exit 0
D=${L[$((k-1))]}; [ -d "$D" ] || { echo "No such backup"; exit 1; }
echo "Saving the current state first (label: before-restore)…"; /usr/local/sbin/gs-backup before-restore >/dev/null
systemctl stop gsvoice-api || true
tar xzf "$D/settings.tgz" -C /
[ -f "$D/files.tgz" ] && tar xzf "$D/files.tgz" -C / || true
if [ $WITHDB = 1 ] && [ -f "$D/gsvoice.db.gz" ]; then gunzip -c "$D/gsvoice.db.gz" > /var/lib/gsvoice/gsvoice.db; chown asterisk:asterisk /var/lib/gsvoice/gsvoice.db; fi
chown -R asterisk:asterisk /etc/asterisk /var/lib/gsvoice 2>/dev/null || true
systemctl restart asterisk; systemctl start gsvoice-api
sleep 8; asterisk -rx "pjsip show registrations" | grep carrier-reg || echo "(no Telnyx sign-in configured in that backup)"
echo "Restored $(basename "$D")."
GSB
chmod 700 /usr/local/sbin/gs-backup /usr/local/sbin/gs-restore
echo "*/30 * * * * root /usr/local/sbin/gs-backup auto >/dev/null 2>&1
17 3 * * * root /usr/local/sbin/gs-backup daily >/dev/null 2>&1" > /etc/cron.d/gsvoice-backup
[ -f "$CONF" ] && PRE_BACKUP=$(/usr/local/sbin/gs-backup before-install) && echo "Saved the current setup: $PRE_BACKUP" || PRE_BACKUP=""
PRE_REG=$(asterisk -rx "pjsip show registrations" 2>/dev/null | grep carrier-reg | grep -c " Registered" || true)

say "1/6 Installing Asterisk, firewall and fail2ban (takes 2-4 minutes)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq asterisk asterisk-core-sounds-en-gsm fail2ban ufw curl openssl logrotate cron espeak-ng sox ffmpeg >/dev/null
PUBIP=${PUBIP:-$(curl -fsS4 ifconfig.me || curl -fsS4 api.ipify.org || true)}
[ -n "$PUBIP" ] || { echo "Could not detect the public IP. Run: PUBIP=x.x.x.x bash install.sh"; exit 1; }
echo "Public IP: $PUBIP"

say "2/6 Phone carrier details (Telnyx: SIP Connection → Authentication; DIDLogic: SIP Trunks)"
echo "Press Enter to keep the value in [brackets]. Leave the username empty to set the carrier up later."
read -rp "Carrier — telnyx or didlogic [${PROVIDER:-telnyx}]: " x; x=${x:-${PROVIDER:-telnyx}}; x=$(echo "$x" | tr 'A-Z' 'a-z')
case "$x" in telnyx|didlogic) ;; *) echo "Type telnyx or didlogic"; exit 1;; esac
if [ "$x" != "${PROVIDER:-}" ]; then DL_SERVER=""; DL_USER=""; DL_PASS=""; fi   # switching carrier: ask fresh
PROVIDER=$x
DEF_SERVER=$([ "$PROVIDER" = telnyx ] && echo sip.telnyx.com || echo sip.nyc.didlogic.net)
read -rp "SIP server [${DL_SERVER:-$DEF_SERVER}]: " x; DL_SERVER=${x:-${DL_SERVER:-$DEF_SERVER}}
case "${DL_USER:-}" in *[[:space:]/:\'\"]*) DL_USER="";; esac      # a pasted command is never a SIP username
read -rp "SIP username [${DL_USER:-}]: " x
case "$x" in *[[:space:]/:\'\"]*) echo "That doesn't look like a SIP username (it has spaces or / : quotes) — run the installer again and type only the username."; exit 1;; esac
DL_USER=${x:-${DL_USER:-}}
if [ -n "$DL_USER" ]; then
  read -rsp "SIP password (hidden, Enter = keep current): " x; echo; DL_PASS=${x:-${DL_PASS:-}}
  [ -n "$DL_PASS" ] || { echo "A password is required"; exit 1; }
fi
read -rp "Your phone number from the carrier, digits only with country code, e.g. 12145550123 [${DID:-}]: " x; DID=${x:-${DID:-}}
DID=$(echo "$DID" | tr -cd '0-9')
read -rp "Is 911 (E911 with your address) enabled on that number at the carrier? y/N [${E911:-n}]: " x; E911=${x:-${E911:-n}}
SINCH_HOST=""; SINCH_MAP=""        # Sinch was removed (Nov 2026) — VoIP.ms is the backup carrier
echo "Backup carrier VoIP.ms (sub account from voip.ms -> Sub Accounts) — takes over outgoing calls when Telnyx can't, and rings its own number. Type - to remove it."
case "${BK_USER:-}" in *[[:space:]/:\'\"]*) BK_USER="";; esac
read -rp "VoIP.ms sub account username, e.g. 585855_gsfo (Enter = keep [${BK_USER:-none}]): " x
case "$x" in *[[:space:]/:\'\"]*) echo "That doesn't look like a VoIP.ms username (it has spaces or / : quotes) — run the installer again and type only the username."; exit 1;; esac
BK_USER=${x:-${BK_USER:-}}; [ "$BK_USER" = "-" ] && BK_USER=""
if [ -n "$BK_USER" ]; then
  read -rsp "VoIP.ms sub account password (hidden, Enter = keep current): " x; echo; BK_PASS=${x:-${BK_PASS:-}}
  case "$BK_PASS" in *[[:space:]\'\"]*) echo "The password has spaces or quotes — set a letters-and-numbers password in VoIP.ms and run the installer again."; exit 1;; esac
  [ -n "$BK_PASS" ] || { echo "A password is required"; exit 1; }
  read -rp "VoIP.ms server [${BK_SERVER:-sanjose1.voip.ms}]: " x; BK_SERVER=${x:-${BK_SERVER:-sanjose1.voip.ms}}; BK_SERVER=$(echo "$BK_SERVER" | tr -cd 'A-Za-z0-9.-')
  read -rp "Your VoIP.ms backup number, digits only, e.g. 19453778722 [${BK_DID:-none}]: " x; BK_DID=$(echo "${x:-${BK_DID:-}}" | tr -cd '0-9'); [ ${#BK_DID} = 10 ] && BK_DID=1$BK_DID
  if [ -n "$BK_DID" ]; then
    read -rp "When someone calls $BK_DID, ring it like which of your numbers? [${BK_RING:-${DID:-}}]: " x; BK_RING=$(echo "${x:-${BK_RING:-${DID:-}}}" | tr -cd '0-9'); [ ${#BK_RING} = 10 ] && BK_RING=1$BK_RING
  else BK_RING=""; fi
else BK_PASS=""; BK_SERVER=""; BK_DID=""; BK_RING=""; fi

# Customer service (accounts, minutes, number buying) — settings live in /etc/gsvoice/gsvoice.json
GSJ=/etc/gsvoice/gsvoice.json; mkdir -p /etc/gsvoice
jget(){ python3 -c "import json,sys;print(json.load(open('$GSJ')).get(sys.argv[1],''))" "$1" 2>/dev/null || true; }
API_DOMAIN=$(jget sip_domain); TX_KEY=$(jget telnyx_api_key); TX_CONN=$(jget telnyx_connection_id); API_TOKEN=$(jget api_token)
read -rp "Domain for the customer API and app [${API_DOMAIN:-voice.globalesim.tel}]: " x; API_DOMAIN=${x:-${API_DOMAIN:-voice.globalesim.tel}}
read -rsp "Telnyx API key (hidden, Enter = keep current / skip): " x; echo; TX_KEY=${x:-$TX_KEY}
read -rp "Telnyx SIP connection ID [${TX_CONN:-}]: " x; TX_CONN=${x:-$TX_CONN}
GTTS_KEY=$(jget google_tts_key)
read -rsp "Google Text-to-Speech API key for premium voices (hidden, Enter = keep current, - = remove): " x; echo
GTTS_KEY=${x:-$GTTS_KEY}; [ "$GTTS_KEY" = "-" ] && GTTS_KEY=""; GTTS_KEY=$(echo "$GTTS_KEY" | tr -cd 'A-Za-z0-9_-')
VMS_USER=$(jget voipms_api_user); VMS_PASS=$(jget voipms_api_pass); VMS_SELL=$(jget voipms_sell)
if [ -n "$BK_USER" ]; then
  echo "VoIP.ms API (lets customers pick VoIP.ms numbers). voip.ms -> Main Menu -> SOAP and REST/JSON API. Type - to remove."
  read -rp "VoIP.ms login email for the API [${VMS_USER:-none}]: " x; VMS_USER=${x:-$VMS_USER}; [ "$VMS_USER" = "-" ] && VMS_USER=""
  case "$VMS_USER" in *[[:space:]\'\"]*) echo "That doesn't look like an email address — run the installer again."; exit 1;; esac
  if [ -n "$VMS_USER" ]; then
    read -rsp "VoIP.ms API password (hidden, Enter = keep current): " x; echo; VMS_PASS=${x:-$VMS_PASS}
    case "$VMS_PASS" in *[[:space:]\'\"]*) echo "The API password has spaces or quotes — set a letters-and-numbers one in VoIP.ms and run the installer again."; exit 1;; esac
    case "$VMS_SELL" in True|true|1|y) d=y;; *) d=n;; esac
    read -rp "Offer VoIP.ms numbers to customers when they pick a number? y/N [$d]: " x; x=${x:-$d}
    case "$x" in y|Y|yes|YES) VMS_SELL=1;; *) VMS_SELL=0;; esac
  else VMS_PASS=""; VMS_SELL=0; fi
else VMS_USER=""; VMS_PASS=""; VMS_SELL=0; fi
SN_PROJ=$(jget sinch_project_id); SN_KID=$(jget sinch_key_id); SN_SEC=$(jget sinch_key_secret)
if [ -n "$SINCH_HOST" ]; then
  echo "Sinch API access (lets customers pick Sinch numbers too). Sinch dashboard -> Settings -> Access Keys. Type - to remove."
  read -rp "Sinch Project ID [${SN_PROJ:-none}]: " x; SN_PROJ=${x:-$SN_PROJ}; [ "$SN_PROJ" = "-" ] && SN_PROJ=""
  read -rp "Sinch Access Key ID [${SN_KID:-none}]: " x; SN_KID=${x:-$SN_KID}; [ "$SN_KID" = "-" ] && SN_KID=""
  read -rsp "Sinch Key Secret (hidden, Enter = keep current): " x; echo; SN_SEC=${x:-$SN_SEC}; [ -z "$SN_KID" ] && SN_SEC=""
  SN_PROJ=$(echo "$SN_PROJ" | tr -cd 'A-Za-z0-9-'); SN_KID=$(echo "$SN_KID" | tr -cd 'A-Za-z0-9-'); SN_SEC=$(echo "$SN_SEC" | tr -d '[:space:]')
else SN_PROJ=""; SN_KID=""; SN_SEC=""; fi
API_TOKEN=${API_TOKEN:-$(openssl rand -hex 24)}
NEW_PASS=0; [ -z "$TEST_PASS" ] && NEW_PASS=1
TEST_PASS=${TEST_PASS:-$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20)}
TEST_USER=${TEST_USER:-gs$(shuf -i 100000-999999 -n 1)}   # not "1001": bots guess short numbers

umask 077
cat > "$CONF" <<EOF
PROVIDER='$PROVIDER'
DL_SERVER='$DL_SERVER'
DL_USER='$DL_USER'
DL_PASS='$DL_PASS'
DID='$DID'
E911='$E911'
SINCH_HOST='$SINCH_HOST'
SINCH_MAP='$SINCH_MAP'
BK_USER='$BK_USER'
BK_PASS='$BK_PASS'
BK_SERVER='$BK_SERVER'
BK_DID='$BK_DID'
BK_RING='$BK_RING'
TEST_PASS='$TEST_PASS'
TEST_USER='$TEST_USER'
EOF
chmod 600 "$CONF"

# certificate for the app's encrypted connection (Caddy gets it from Let's Encrypt)
cat > /usr/local/bin/gsvoice-cert <<EOF
#!/bin/sh
# copy Caddy's certificate for $API_DOMAIN to Asterisk (TLS for the app); reload when it changed
SRC=\$(ls -d /var/lib/caddy/.local/share/caddy/certificates/*/$API_DOMAIN 2>/dev/null | head -1)
[ -n "\$SRC" ] || exit 0
mkdir -p /etc/asterisk/keys
if ! cmp -s "\$SRC/$API_DOMAIN.crt" /etc/asterisk/keys/voice.crt; then
  cp "\$SRC/$API_DOMAIN.crt" /etc/asterisk/keys/voice.crt; cp "\$SRC/$API_DOMAIN.key" /etc/asterisk/keys/voice.key
  chown asterisk:asterisk /etc/asterisk/keys/voice.*; chmod 600 /etc/asterisk/keys/voice.key
  asterisk -rx "module reload res_pjsip.so" >/dev/null 2>&1 || true
fi
EOF
chmod 755 /usr/local/bin/gsvoice-cert
echo '17 4 * * * root /usr/local/bin/gsvoice-cert' > /etc/cron.d/gsvoice-cert
/usr/local/bin/gsvoice-cert || true
ufw allow 5061/tcp >/dev/null

say "3/6 Writing Asterisk configuration"
cd /etc/asterisk
[ -d /etc/asterisk/original ] || { mkdir original; cp -a pjsip.conf extensions.conf rtp.conf logger.conf original/ 2>/dev/null || true; }

cat > rtp.conf <<EOF
[general]
rtpstart=10000
rtpend=20000
EOF

cat > logger.conf <<EOF
[general]
dateformat=%F %T
[logfiles]
console => notice,warning,error
messages => warning,error
fail2ban => notice
EOF

{
cat <<EOF
; ===== GlobaleSIM Voice — generated by install.sh =====
[global]
type=global
user_agent=GlobaleSIM Voice
endpoint_identifier_order=ip,username

[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060
external_media_address=$PUBIP
external_signaling_address=$PUBIP
local_net=10.0.0.0/8
local_net=172.16.0.0/12
local_net=192.168.0.0/16

[transport-tcp]
type=transport
protocol=tcp
bind=0.0.0.0:5060
external_media_address=$PUBIP
external_signaling_address=$PUBIP
local_net=10.0.0.0/8
local_net=172.16.0.0/12
local_net=192.168.0.0/16

; Calls from a computer browser (WebRTC): wss://$API_DOMAIN/ws → Caddy → Asterisk's local web server
[transport-ws]
type=transport
protocol=ws
bind=0.0.0.0

EOF
if [ -s /etc/asterisk/keys/voice.crt ] && [ -s /etc/asterisk/keys/voice.key ]; then cat <<EOF
; Encrypted SIP for the GlobaleSIM app (certificate from Caddy / Let's Encrypt)
[transport-tls]
type=transport
protocol=tls
bind=0.0.0.0:5061
cert_file=/etc/asterisk/keys/voice.crt
priv_key_file=/etc/asterisk/keys/voice.key
method=tlsv1_2
external_media_address=$PUBIP
external_signaling_address=$PUBIP
local_net=10.0.0.0/8
local_net=172.16.0.0/12
local_net=192.168.0.0/16

EOF
fi
cat <<EOF
; ---- Test phone (Zoiper/Linphone) — later each app customer gets one of these ----
[$TEST_USER]
type=endpoint
context=from-app
disallow=all
allow=ulaw,alaw,g722,gsm
auth=$TEST_USER-auth
aors=$TEST_USER
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
dtmf_mode=rfc4733
callerid=GlobaleSIM <${DID:-0}>

[$TEST_USER-auth]
type=auth
auth_type=userpass
username=$TEST_USER
password=$TEST_PASS

[$TEST_USER]
type=aor
max_contacts=3
remove_existing=yes
qualify_frequency=30
EOF
if [ -n "$DL_USER" ]; then cat <<EOF

; ---- Phone carrier trunk ($PROVIDER) ----
[carrier-auth]
type=auth
auth_type=userpass
username=$DL_USER
password=$DL_PASS

[carrier-reg]
type=registration
transport=transport-udp
outbound_auth=carrier-auth
server_uri=sip:$DL_SERVER
client_uri=sip:$DL_USER@$DL_SERVER
contact_user=$DL_USER
retry_interval=60
forbidden_retry_interval=600
expiration=300
line=yes
endpoint=carrier

[carrier]
type=endpoint
transport=transport-udp
context=from-carrier
allow_transfer=no
disallow=all
allow=ulaw,alaw
outbound_auth=carrier-auth
aors=carrier
$( [ "$PROVIDER" = didlogic ] && echo "from_user=$DL_USER" )
contact_user=$DL_USER
from_domain=$DL_SERVER
send_pai=yes
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
dtmf_mode=rfc4733

; the same carrier, used only for 911/933 calls that know the phone's GPS position: a PIDF-LO
; (geolocation.conf, filled in per call by [gs-geo]) goes in the INVITE, as Telnyx asks
[carrier911]
type=endpoint
transport=transport-udp
context=from-carrier
allow_transfer=no
disallow=all
allow=ulaw,alaw
outbound_auth=carrier-auth
aors=carrier
$( [ "$PROVIDER" = didlogic ] && echo "from_user=$DL_USER" )
from_domain=$DL_SERVER
send_pai=yes
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
dtmf_mode=rfc4733
geoloc_outgoing_call_profile=gs_e911

[carrier]
type=aor
contact=sip:$DL_SERVER

[carrier-identify]
type=identify
endpoint=carrier
EOF
for h in $( [ "$PROVIDER" = didlogic ] && echo sip.nyc.didlogic.net sip.lax.didlogic.net ) "$DL_SERVER"; do echo "match=$h"; done | sort -u
fi
if [ -n "$SINCH_HOST" ]; then cat <<EOF

; ---- Backup carrier: Sinch Elastic SIP Trunk (no password: Sinch allows this server's IP) ----
[sinch]
type=endpoint
transport=transport-udp
context=from-carrier
allow_transfer=no
disallow=all
allow=ulaw,alaw
aors=sinch
from_domain=$SINCH_HOST
send_pai=yes
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
dtmf_mode=rfc4733

[sinch]
type=aor
contact=sip:$SINCH_HOST
qualify_frequency=60

[sinch-identify]
type=identify
endpoint=sinch
EOF
for n in 131 133 134 136 137 138 139 141; do echo "match=206.146.$n.0/28"; done
fi
if [ -n "$BK_USER" ]; then cat <<EOF

; ---- Backup carrier: VoIP.ms (signs in like Telnyx; the sub account only accepts calls from this server's IP) ----
[voipms-auth]
type=auth
auth_type=userpass
username=$BK_USER
password=$BK_PASS

[voipms-reg]
type=registration
transport=transport-udp
outbound_auth=voipms-auth
server_uri=sip:$BK_SERVER
client_uri=sip:$BK_USER@$BK_SERVER
contact_user=$BK_USER
retry_interval=60
forbidden_retry_interval=600
expiration=300
line=yes
endpoint=voipms

[voipms]
type=endpoint
transport=transport-udp
context=from-carrier
allow_transfer=no
disallow=all
allow=ulaw
outbound_auth=voipms-auth
aors=voipms
from_user=$BK_USER
from_domain=$BK_SERVER
send_pai=yes
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
dtmf_mode=rfc4733

[voipms]
type=aor
contact=sip:$BK_SERVER

[voipms-identify]
type=identify
endpoint=voipms
match=208.100.60.0/24
EOF
fi
echo
echo "#include pjsip_customers.conf"
} > pjsip.conf

# 911 location (PIDF-LO). The point here is only a placeholder: [gs-geo] puts the phone's real position in on every call,
# and calls without a position never use this profile (they go out on [carrier], where Telnyx uses the 911 address on file).
cat > geolocation.conf <<'EOF'
[gs_gps]
type=location
format=GML
location_info=shape=Point,pos="0.0 0.0"
method=LIS

[gs_e911]
type=profile
location_reference=gs_gps
pidf_element=tuple
usage_rules=retransmission-allowed=yes
allow_routing_use=yes
profile_precedence=prefer_incoming
EOF
[ -f pjsip_customers.conf ] || echo "; customers are added by gsvoice" > pjsip_customers.conf

E911_ON=0; case "$E911" in y|Y|yes|YES) E911_ON=1;; esac
cat > extensions.conf <<EOF
; ===== GlobaleSIM Voice dial plan — generated by install.sh =====
[general]
static=yes
writeprotect=yes

[globals]
DID=$DID
DIALPFX=$( [ "$PROVIDER" = telnyx ] && echo "+" )
E911_ON=$E911_ON
SINCH_ON=$( [ -n "$SINCH_HOST" ] && echo 1 || echo 0 )
BK_ON=$( [ -n "$BK_USER$SINCH_HOST" ] && echo 1 || echo 0 )
BK_EP=$( [ -n "$BK_USER" ] && echo voipms || echo sinch )
BK_PFX=$( [ -n "$BK_USER" ] || echo "+" )

; Calls coming in from the carrier to your number -> ring the test phone
[from-carrier]
exten => h,1,NoOp(caller hung up)
exten => _.,1,Set(GS_TO=\${PJSIP_HEADER(read,To)})
 same => n,AGI(gs-agi.py,in,\${EXTEN},"\${GS_TO}",\${CALLERID(num)})
 same => n,GotoIf(\$["\${GS_PROBE}" = "1"]?probe)
 same => n,GotoIf(\$["\${GS_OK}" = "1"]?ok)
 same => n,Playback(ss-noservice)
 same => n,Hangup()
 same => n(probe),Hangup(17)
 same => n(ok),Set(CHANNEL(hangup_handler_push)=gs-charge,s,1(\${GS_CALLID}))
 same => n,Set(__TRANSFER_CONTEXT=gs-transfer)
 same => n,GotoIf(\$["\${GS_MODE}" = "ivr"]?gs-ivr,s,1)
 same => n,GotoIf(\$["\${GS_MODE}" = "closed"]?gs-closed,s,1)
 same => n,GotoIf(\$["\${GS_MODE}" = "dept"]?gs-dept,s,1)
 same => n,GotoIf(\$["\${GS_DND}" = "1"]?gs-vm,s,1)
 same => n,GotoIf(\$["\${GS_FWD}" = "always"]?gs-fwd,s,1)
 same => n,Gosub(gs-pushwait,s,1(GS_DIALS))
 same => n,Dial(\${GS_DIALS},25,L(\${GS_MAXMS})b(gs-qospre^s^1(\${GS_CALLID}))U(gs-ans^\${GS_CALLID}^))
 same => n,GotoIf(\$[\${REGEX("^(\${GS_FWDON})\$" \${DIALSTATUS})}]?gs-fwd,s,1)
 same => n,Goto(gs-vm,s,1)

; ===== Business phone system =====
; Main menu: "Press 1 for sales…" (greeting made from text, or the company's own); any 3-digit extension works too
[gs-ivr]
exten => s,1,Answer()
 same => n,ExecIf(\$["\${GS_ANS}" = ""]?Set(GS_ANS=\${EPOCH}))
 same => n,Set(GS_TRIES=0)
 same => n(menu),Set(TIMEOUT(digit)=2)
 same => n,Set(TIMEOUT(response)=6)
 same => n,Background(\${GS_GREET})
 same => n,WaitExten(6)
exten => _X,1,AGI(gs-agi.py,ivr,\${GS_CO},\${EXTEN})
 same => n,Goto(gs-ivr-act,s,1)
exten => _XXX,1,AGI(gs-agi.py,ivr,\${GS_CO},\${EXTEN})
 same => n,Goto(gs-ivr-act,s,1)
exten => t,1,Set(GS_TRIES=\$[\${GS_TRIES} + 1])
 same => n,GotoIf(\$[\${GS_TRIES} < 2]?s,menu)
 same => n,AGI(gs-agi.py,ivr,\${GS_CO},t)
 same => n,Goto(gs-ivr-act,s,1)
exten => i,1,Playback(pbx-invalid)
 same => n,Goto(s,menu)

[gs-ivr-act]
exten => s,1,GotoIf(\$["\${GS_ACT}" = "repeat"]?gs-ivr,s,menu)
 same => n,GotoIf(\$["\${GS_ACT}" = "vm"]?gs-vm,s,1)
 same => n,GotoIf(\$["\${GS_ACT}" = "dial"]?dial)
 same => n,Playback(pbx-invalid)
 same => n,Goto(gs-ivr,s,menu)
 same => n(dial),Gosub(gs-pushwait,s,1(GS_DIAL))
 same => n,Dial(\${GS_DIAL},25,L(\${GS_MAXMS})b(gs-qospre^s^1(\${GS_CALLID}^\${GS_DEPTNAME}))U(gs-ans^\${GS_CALLID}^\${GS_DEPT}))
 same => n,Goto(gs-vm,s,1)

; A department's own direct number (e.g. the AI receptionist transfers here): everyone free in the department rings at once
[gs-dept]
exten => s,1,Gosub(gs-pushwait,s,1(GS_DIAL))
 same => n,Dial(\${GS_DIAL},25,L(\${GS_MAXMS})b(gs-qospre^s^1(\${GS_CALLID}^\${GS_DEPTNAME}))U(gs-ans^\${GS_CALLID}^\${GS_DEPT}))
 same => n,Goto(gs-vm,s,1)

; Outside business hours: closed greeting, then voicemail
[gs-closed]
exten => s,1,Answer()
 same => n,ExecIf(\$["\${GS_ANS}" = ""]?Set(GS_ANS=\${EPOCH}))
 same => n,GotoIf(\$["\${GS_GREET}" = ""]?vm)
 same => n,Playback(\${GS_GREET})
 same => n(vm),Set(GS_SKIPINTRO=1)
 same => n,Goto(gs-vm,s,1)

; Voicemail: recorded, then the website emails the customer (and it shows in the app)
; A parked call nobody picked up in 3 minutes: ring all phones of the person who parked it for 30 s, then their voicemail
[gs-park-timeout]
exten => _[A-Za-z0-9].,1,Goto(s,1)
exten => s,1,AGI(gs-agi.py,parkback,\${PARKER})
 same => n,GotoIf(\$["\${GS_DIALS}" = ""]?vm)
 same => n,Gosub(gs-pushwait,s,1(GS_DIALS))
 same => n,Dial(\${GS_DIALS},30)
 same => n(vm),Goto(gs-vm,s,1)

[gs-vm]
exten => s,1,GotoIf(\$["\${GS_VMUSER}" = ""]?none)
 same => n,Answer()
 same => n,ExecIf(\$["\${GS_ANS}" = ""]?Set(GS_ANS=\${EPOCH}))
 same => n,Set(GS_VMFILE=/var/lib/gsvoice/vm/\${GS_VMUSER}-\${EPOCH}-\${RAND(1000,9999)})
 same => n,Set(CHANNEL(hangup_handler_push)=gs-vmsave,s,1(\${GS_VMUSER},\${GS_VMFILE},\${CALLERID(num)}))
 same => n,ExecIf(\$["\${GS_SKIPINTRO}" != "1"]?Playback(vm-intro))
 same => n,Record(\${GS_VMFILE}.wav,3,180,k)
 same => n,Hangup()
 same => n(none),Playback(vm-nobodyavail)
 same => n,Hangup()

[gs-vmsave]
exten => s,1,AGI(gs-agi.py,vmsave,\${ARG1},\${ARG2},\${ARG3})
 same => n,Return()

; Call quality: on the customer's own leg (their device <-> this server) — jitter, packet loss and delay, saved with the call
[gs-qos]
exten => s,1,AGI(gs-agi.py,qos,\${ARG1},\${CHANNEL(rtcp,all)},\${CHANNEL(pjsip,remote_addr)},\${CHANNEL(endpoint)})
 same => n,Return()
; incoming calls: each device that rings gets the same handler (b() runs on the dialed channel)
[gs-qospre]
exten => s,1,Set(CHANNEL(hangup_handler_push)=gs-qos,s,1(\${ARG1}))
 same => n,ExecIf(\$["\${ARG2}" != ""]?Set(PJSIP_HEADER(add,X-GS-Dept)=\${ARG2}))
 same => n,Return()

; After every customer call: charge the minutes (rounded up) and run the robocall check
; 911/933 from the apps: the phone's GPS position goes to Telnyx as a PIDF-LO (RFC 4119 / 5491) in the INVITE,
; on the [carrier911] endpoint only. Without a position the call uses [carrier] and Telnyx uses the 911 address on file.
[gs-geo]
exten => s,1,GotoIf(\$["\${GS_LAT}" = "" | "\${GS_LON}" = ""]?done)
 same => n,Set(GEOLOC_PROFILE(format)=GML)
 same => n,Set(GEOLOC_PROFILE(pidf_element)=tuple)
 same => n,Set(GEOLOC_PROFILE(location_info)=shape=Point,pos="\${GS_LAT} \${GS_LON}")
 same => n,Set(GEOLOC_PROFILE(usage_rules)=retransmission-allowed=yes)
 same => n,Set(GEOLOC_PROFILE(method)=LIS)
 same => n(done),Return()

[gs-charge]
; minutes = the longer of: time talking to a person (ANSWEREDTIME) or time since the server answered (menu / voicemail, GS_ANS)
exten => s,1,AGI(gs-agi.py,charge,\${ARG1},\${ANSWEREDTIME},\${GS_ANS},\${EPOCH},\${DIALEDTIME},\${GS_START})
 same => n,Return()

; Outside call answered: bill on the carrier leg itself (keeps counting after a transfer) and enforce the time limit there
[gs-outans]
exten => s,1,Set(GS_ANS=\${EPOCH})
 same => n,Set(TIMEOUT(absolute)=\${ARG2})
 same => n,Set(CHANNEL(hangup_handler_push)=gs-charge,s,1(\${ARG1}))
 same => n,Return()

; "Transfer now" (blind transfer) from the app, computer or desk phone: same checks + billing as a normal call
[gs-transfer]
; Park: transfer the caller to *8 -> a free slot 801-899 in the company's lot, with the company's hold music
exten => *8,1,AGI(gs-agi.py,park,\${BLINDTRANSFER})
 same => n,GotoIf(\$["\${GS_OK}" = "1"]?ok)
 same => n,Playback(pbx-invalid)
 same => n,Hangup()
 same => n(ok),Set(PARKINGEXTEN=\${GS_SLOT})
 same => n,ExecIf(\$["\${GS_DESKEP}" != ""]?Gosub(gs-parkmsg,s,1(\${GS_DESKEP},\${GS_SLOT})))
 same => n,Park(\${GS_LOT},s)
 same => n,Hangup()
; Conference: transfer a caller to *9 -> the user's own 3-way room (the user then dials *9 to join)
exten => *9,1,AGI(gs-agi.py,conf,ch,\${BLINDTRANSFER})
 same => n,GotoIf(\$["\${GS_OK}" = "1"]?ok)
 same => n,Playback(pbx-invalid)
 same => n,Hangup()
 same => n(ok),Set(CONFBRIDGE(user,template)=gs_guest)
 same => n,Set(CONFBRIDGE(user,music_on_hold_class)=\${GS_MOH})
 same => n,ConfBridge(\${GS_ROOM},gs_bridge)
 same => n,Hangup()
exten => _+X.,1,Goto(gs-transfer,\${EXTEN:1},1)
exten => _X.,1,Goto(gs-xfer-go,\${EXTEN},1)
exten => _D.,1,Goto(gs-xfer-go,\${EXTEN},1)
[gs-parkmsg]
; desk phones: show the parking slot on the screen (text message)
exten => s,1,Set(MESSAGE(body)=Call parked on \${ARG2}. Pick up with *\${ARG2})
 same => n,MessageSend(pjsip:\${ARG1},"Parked" <sip:park@$API_DOMAIN>)
 same => n,Return()
[gs-xfer-go]
exten => h,1,NoOp(transfer leg ended)
exten => _.,1,AGI(gs-agi.py,xfer,\${BLINDTRANSFER},\${EXTEN})
 same => n,GotoIf(\$["\${GS_OK}" = "1"]?ok)
 same => n,Playback(pbx-invalid)
 same => n,Hangup()
 same => n(ok),GotoIf(\$["\${GS_XEXT}" = "1"]?ext)
 same => n,Set(CALLERID(num)=\${GS_CID})
 same => n,GotoIf(\$["\${GS_VIA}" != ""]?viasinch)
 same => n,Dial(PJSIP/\${DIALPFX}\${GS_NUM}@carrier,60,U(gs-outans^\${GS_XCALL}^\${GS_XMAX}))
 same => n,ExecIf(\$[\${BK_ON} = 1 & ("\${DIALSTATUS}" = "CHANUNAVAIL" | "\${DIALSTATUS}" = "CONGESTION")]?Dial(PJSIP/\${BK_PFX}\${GS_NUM}@\${BK_EP},60,U(gs-outans^\${GS_XCALL}^\${GS_XMAX})))
 same => n,Hangup()
 same => n(viasinch),Dial(PJSIP/\${IF(\$["\${GS_VIA}" = "voipms"]?:+)}\${GS_NUM}@\${GS_VIA},60,U(gs-outans^\${GS_XCALL}^\${GS_XMAX}))
 same => n,ExecIf(\$["\${DIALSTATUS}" = "CHANUNAVAIL" | "\${DIALSTATUS}" = "CONGESTION"]?Dial(PJSIP/\${DIALPFX}\${GS_NUM}@carrier,60,U(gs-outans^\${GS_XCALL}^\${GS_XMAX})))
 same => n,Hangup()
 same => n(ext),Gosub(gs-pushwait,s,1(GS_DIALS))
 same => n,Dial(\${GS_DIALS},25,b(gs-qospre^s^1(\${GS_CALLID}^\${GS_DEPTNAME}))U(gs-ans^\${GS_CALLID}^\${GS_DEPT}))
 same => n,Goto(gs-vm,s,1)

;  iPhone apps can't listen in the background: before ringing, wake them with a push and wait (<= 14 s) until they sign in.
; Lines without an iPhone go straight through. ARG1 = the variable holding the dial string, rebuilt afterwards.
[gs-pushwait]
exten => s,1,GotoIf(\$["\${GS_DIALU}" = ""]?done)
 same => n,AGI(gs-agi.py,pushwait,\${ARG1})
 same => n(done),Return()

; Who answered an incoming call (and for which department) — shown in the office app's history
[gs-ans]
exten => s,1,AGI(gs-agi.py,ans,\${ARG1},\${CHANNEL(endpoint)},\${ARG2})
 same => n,Return()

; Call forwarding (always / busy / no answer) to an extension or an outside number
[gs-fwd]
exten => s,1,AGI(gs-agi.py,fwd,\${GS_FWDUSER},\${CALLERID(num)})
 same => n,GotoIf(\$["\${GS_OK}" = "1"]?ok)
 same => n,Goto(gs-vm,s,1)
 same => n(ok),GotoIf(\$["\${GS_FWDEXT}" = "1"]?ext)
 same => n,Set(CALLERID(num)=\${GS_CID})
 same => n,GotoIf(\$["\${GS_VIA}" != ""]?viasinch)
 same => n,Dial(PJSIP/\${DIALPFX}\${GS_NUM}@carrier,30,U(gs-outans^\${GS_XCALL}^\${GS_XMAX}))
 same => n,ExecIf(\$[\${BK_ON} = 1 & ("\${DIALSTATUS}" = "CHANUNAVAIL" | "\${DIALSTATUS}" = "CONGESTION")]?Dial(PJSIP/\${BK_PFX}\${GS_NUM}@\${BK_EP},30,U(gs-outans^\${GS_XCALL}^\${GS_XMAX})))
 same => n,Goto(gs-vm,s,1)
 same => n(viasinch),Dial(PJSIP/\${IF(\$["\${GS_VIA}" = "voipms"]?:+)}\${GS_NUM}@\${GS_VIA},30,U(gs-outans^\${GS_XCALL}^\${GS_XMAX}))
 same => n,ExecIf(\$["\${DIALSTATUS}" = "CHANUNAVAIL" | "\${DIALSTATUS}" = "CONGESTION"]?Dial(PJSIP/\${DIALPFX}\${GS_NUM}@carrier,30,U(gs-outans^\${GS_XCALL}^\${GS_XMAX})))
 same => n,Goto(gs-vm,s,1)
 same => n(ext),Gosub(gs-pushwait,s,1(GS_DIALS))
 same => n,Dial(\${GS_DIALS},25)
 same => n,Goto(gs-vm,s,1)

; Text messages sent FROM the app: not offered (receive-only line) — drop them
[gs-messages]
exten => _.,1,Hangup()

; Calls made by paying customers from the GlobaleSIM app
; Checked by gsvoice before every call: active account, minutes left, max 5 calls / 10 min,
; 100 / day, one call at a time; the call is cut off when the minutes run out.
[from-customer]
; Park (some phones look up the transfer target here instead of in gs-transfer)
exten => *8,1,Goto(gs-transfer,*8,1)
; Desk phone Retrieve key: *80 = the only parked call, or say the slot
exten => *80,1,AGI(gs-agi.py,unpark,\${CHANNEL(endpoint)},0)
 same => n,GotoIf(\$["\${GS_OK}" = "1"]?ok)
 same => n,GotoIf(\$["\${GS_COUNT}" = "0"]?none)
 same => n,Read(GS_IN,vm-enter-num-to-call,2,,1,8)
 same => n,Goto(from-customer,*8\${GS_IN},1)
 same => n(none),Playback(pbx-invalidpark)
 same => n,Hangup()
 same => n(ok),Set(__TRANSFER_CONTEXT=gs-transfer)
 same => n,ParkedCall(\${GS_LOT},\${GS_SLOT})
 same => n,Hangup()
; Join your own conference room (after transferring the callers to *9)
exten => *9,1,AGI(gs-agi.py,conf,ep,\${CHANNEL(endpoint)})
 same => n,GotoIf(\$["\${GS_OK}" = "1"]?ok)
 same => n,Playback(pbx-invalid)
 same => n,Hangup()
 same => n(ok),Set(__TRANSFER_CONTEXT=gs-transfer)
 same => n,Set(CONFBRIDGE(user,template)=gs_host)
 same => n,ConfBridge(\${GS_ROOM},gs_bridge)
 same => n,Hangup()
; Pick up a parked call: *8 + slot (e.g. *848), any phone of the same company
exten => _*8XX,1,AGI(gs-agi.py,unpark,\${CHANNEL(endpoint)},\${EXTEN:1})
 same => n,GotoIf(\$["\${GS_OK}" = "1"]?ok)
 same => n,Playback(pbx-invalid)
 same => n,Hangup()
 same => n(ok),Set(__TRANSFER_CONTEXT=gs-transfer)
 same => n,ParkedCall(\${GS_LOT},\${EXTEN:1})
 same => n,Hangup()
; Calls between extensions of the same company (101-899): free, never leave the server
exten => _[1-8]XX,1,Set(__TRANSFER_CONTEXT=gs-transfer)
 same => n,AGI(gs-agi.py,ext,\${CHANNEL(endpoint)},\${EXTEN})
 same => n,GotoIf(\$["\${GS_OK}" = "1"]?ok)
 same => n,Playback(pbx-invalid)
 same => n,Hangup()
 same => n(ok),Set(CALLERID(name)=\${GS_CNAME})
 same => n,Set(CALLERID(num)=\${GS_CNUM})
 same => n,Set(GS_SKIPINTRO=0)
 same => n,GotoIf(\$["\${GS_DND}" = "1"]?gs-vm,s,1)
 same => n,GotoIf(\$["\${GS_FWD}" = "always"]?gs-fwd,s,1)
 same => n,Gosub(gs-pushwait,s,1(GS_DIALS))
 same => n,Dial(\${GS_DIALS},25)
 same => n,GotoIf(\$[\${REGEX("^(\${GS_FWDON})\$" \${DIALSTATUS})}]?gs-fwd,s,1)
 same => n,Goto(gs-vm,s,1)
; Call a whole department (D<id>, from the office app): everyone free in it rings
exten => _D.,1,Set(__TRANSFER_CONTEXT=gs-transfer)
 same => n,AGI(gs-agi.py,ext,\${CHANNEL(endpoint)},\${EXTEN})
 same => n,GotoIf(\$["\${GS_OK}" = "1"]?ok)
 same => n,Playback(pbx-invalid)
 same => n,Hangup()
 same => n(ok),Set(CALLERID(name)=\${GS_CNAME})
 same => n,Set(CALLERID(num)=\${GS_CNUM})
 same => n,Gosub(gs-pushwait,s,1(GS_DIALS))
 same => n,Dial(\${GS_DIALS},30)
 same => n,Playback(vm-nobodyavail)
 same => n,Hangup()
; 911: only lines whose 911 address is registered at Telnyx; caller ID = their own number (so the address shows)
exten => 911,1,AGI(gs-agi.py,e911,\${CHANNEL(endpoint)})
 same => n,GotoIf(\$["\${GS_OK}" = "1"]?go)
 same => n,Playback(ss-noservice)
 same => n,Hangup()
 same => n(go),Set(CALLERID(all)=<\${GS_CID}>)
 same => n,GotoIf(\$["\${GS_VIA}" != ""]?viasinch)
 same => n,Dial(PJSIP/911@\${IF(\$["\${GS_LAT}" != ""]?carrier911:carrier)},120,b(gs-geo^s^1))
 same => n,Hangup()
 same => n(viasinch),Dial(PJSIP/911@\${GS_VIA},120)
 same => n,ExecIf(\$["\${DIALSTATUS}" = "CHANUNAVAIL" | "\${DIALSTATUS}" = "CONGESTION"]?Dial(PJSIP/911@carrier,120))
 same => n,Hangup()
; Kari's Law: 911 works with or without a leading 9
exten => 9911,1,Goto(from-customer,911,1)
; 933 = free test call: Telnyx reads back the 911 address and number on file (no emergency dispatch)
exten => 933,1,AGI(gs-agi.py,e933,\${CHANNEL(endpoint)})
 same => n,GotoIf(\$["\${GS_OK}" = "1"]?go)
 same => n,Playback(ss-noservice)
 same => n,Hangup()
 same => n(go),Set(CALLERID(all)=<\${GS_CID}>)
 same => n,GotoIf(\$["\${GS_VIA}" = "sinch"]?viasinch)
 same => n,Dial(PJSIP/933@\${IF(\$["\${GS_LAT}" != ""]?carrier911:carrier)},60,b(gs-geo^s^1))
 same => n,Hangup()
 same => n(viasinch),Dial(PJSIP/933@sinch,60)
 same => n,Hangup()
exten => _NXXNXXXXXX,1,Goto(from-customer,1\${EXTEN},1)
exten => _+1NXXNXXXXXX,1,Goto(from-customer,\${EXTEN:1},1)
exten => _1NXXNXXXXXX,1,Set(__TRANSFER_CONTEXT=gs-transfer)
 same => n,GotoIf(\$[\${REGEX("^(900|976)" \${EXTEN:1:3})}]?blocked)
 same => n,Set(GS_EP=\${CHANNEL(endpoint)})
 same => n,AGI(gs-agi.py,out,\${GS_EP},\${EXTEN},\${PJSIP_HEADER(read,X-GS-CID)})
 same => n,GotoIf(\$["\${GS_OK}" = "1"]?ok:blocked)
 same => n(ok),Set(GROUP(gsuser)=\${GS_BASE})
 same => n,GotoIf(\$[\${GROUP_COUNT(\${GS_BASE}@gsuser)} > 2]?blocked)
 same => n,Set(GROUP(gsall)=out)
 same => n,GotoIf(\$[\${GROUP_COUNT(out@gsall)} > \${GS_GMAX}]?blocked)
 same => n,GotoIf(\$["\${GS_CO}" = ""]?go)
 same => n,Set(GROUP(gsco)=\${GS_CO})
 same => n,GotoIf(\$[\${GROUP_COUNT(\${GS_CO}@gsco)} > \${GS_COMAX}]?blocked)
 same => n(go),Set(CALLERID(all)=<\${GS_CID}>)
 same => n,Set(CHANNEL(hangup_handler_push)=gs-charge,s,1(\${GS_CALLID}))
 same => n,Set(CHANNEL(hangup_handler_push)=gs-qos,s,1(\${GS_CALLID}))
 same => n,GotoIf(\$["\${GS_VIA}" != ""]?viasinch)
 same => n,Dial(PJSIP/\${DIALPFX}\${EXTEN}@carrier,60,L(\${GS_MAXMS})U(gs-outans^\${GS_CALLID}^\${GS_MAXSEC}))
 same => n,ExecIf(\$[\${BK_ON} = 1 & ("\${DIALSTATUS}" = "CHANUNAVAIL" | "\${DIALSTATUS}" = "CONGESTION")]?Dial(PJSIP/\${BK_PFX}\${EXTEN}@\${BK_EP},60,L(\${GS_MAXMS})U(gs-outans^\${GS_CALLID}^\${GS_MAXSEC})))
 same => n,Hangup()
 same => n(viasinch),Dial(PJSIP/\${IF(\$["\${GS_VIA}" = "voipms"]?:+)}\${EXTEN}@\${GS_VIA},60,L(\${GS_MAXMS})U(gs-outans^\${GS_CALLID}^\${GS_MAXSEC}))
 same => n,ExecIf(\$["\${DIALSTATUS}" = "CHANUNAVAIL" | "\${DIALSTATUS}" = "CONGESTION"]?Dial(PJSIP/\${DIALPFX}\${EXTEN}@carrier,60,L(\${GS_MAXMS})U(gs-outans^\${GS_CALLID}^\${GS_MAXSEC})))
 same => n,Hangup()
 same => n(blocked),Playback(ss-noservice)
 same => n,Hangup()
exten => _X.,1,Playback(ss-noservice)
 same => n,Hangup()

; Calls made from the app/test phone
; For now: USA + Canada only. International stays OFF until we add per-customer credit,
; because open international dialing is how phone servers get robbed (toll fraud).
[from-app]
exten => 911,1,GotoIf(\$[\${E911_ON} = 1]?go)
 same => n,Playback(ss-noservice)
 same => n,Hangup()
 same => n(go),Set(CALLERID(num)=\${DID})
 same => n,Dial(PJSIP/911@carrier,120)
 same => n,Hangup()
exten => 9911,1,Goto(from-app,911,1)
exten => 933,1,GotoIf(\$[\${E911_ON} = 1]?go)
 same => n,Playback(ss-noservice)
 same => n,Hangup()
 same => n(go),Set(CALLERID(num)=\${DID})
 same => n,Dial(PJSIP/933@carrier,60)
 same => n,Hangup()
exten => _NXXNXXXXXX,1,Goto(from-app,1\${EXTEN},1)
exten => _+1NXXNXXXXXX,1,Goto(from-app,\${EXTEN:1},1)
exten => _1NXXNXXXXXX,1,Set(__TRANSFER_CONTEXT=gs-transfer)
 same => n,GotoIf(\$[\${REGEX("^(900|976)" \${EXTEN:1:3})}]?blocked)
 same => n,Set(CALLERID(num)=\${DID})
 same => n,Set(TIMEOUT(absolute)=7200)
 same => n,Dial(PJSIP/\${DIALPFX}\${EXTEN}@carrier,60)
 same => n,ExecIf(\$[\${BK_ON} = 1 & ("\${DIALSTATUS}" = "CHANUNAVAIL" | "\${DIALSTATUS}" = "CONGESTION")]?Dial(PJSIP/\${BK_PFX}\${EXTEN}@\${BK_EP},60))
 same => n,Hangup()
 same => n(blocked),Playback(ss-noservice)
 same => n,Hangup()
exten => _X.,1,Playback(ss-noservice)
 same => n,Hangup()
EOF
# switch off modules we don't use (and that log errors on Ubuntu)
# chan_sip (old SIP driver) must stay off: it grabs browser connections on /ws and rejects them
sed -i "/^noload => res_geolocation\\.so\$/d; /^noload => res_pjsip_geolocation\\.so\$/d" modules.conf   # 911 location (PIDF-LO) needs these
for m in chan_sip.so res_config_ldap.so res_phoneprov.so res_pjsip_phoneprov_provider.so chan_iax2.so chan_skinny.so chan_mgcp.so chan_unistim.so; do
  sed -i "/^noload => ${m//./\\.}\$/d" modules.conf                       # old copies (some landed outside [modules])
  sed -i "/^\[modules\]/a noload => $m" modules.conf                     # must be inside [modules] to count
done
chown -R asterisk:asterisk /etc/asterisk; chmod 640 /etc/asterisk/pjsip.conf

say "4/6 Firewall: SSH, SIP 5060 and call audio 10000-20000"
ufw allow OpenSSH >/dev/null
ufw allow 5060/udp >/dev/null
ufw allow 5060/tcp >/dev/null
ufw allow 10000:20000/udp >/dev/null
ufw --force enable >/dev/null
ufw reload >/dev/null 2>&1 || true
ufw status | head -12

say "5/6 fail2ban: block anyone guessing passwords"
mkdir -p /var/log/asterisk; touch /var/log/asterisk/messages /var/log/asterisk/fail2ban; chown asterisk:asterisk /var/log/asterisk/*
rm -f /var/log/asterisk/security
# 4) logs can never fill the disk again: rotate at 50 MB, checked every 10 minutes
cat > /etc/logrotate.d/globalesim-asterisk <<EOF
/var/log/asterisk/messages /var/log/asterisk/fail2ban /var/log/asterisk/full {
    size 50M
    rotate 3
    compress
    missingok
    notifempty
    copytruncate
}
EOF
echo '*/10 * * * * root /usr/sbin/logrotate /etc/logrotate.d/globalesim-asterisk >/dev/null 2>&1' > /etc/cron.d/globalesim-logrotate
# 5) drop well-known SIP scanner tools before they even reach Asterisk (kept in ufw's own rules)
if [ -f /etc/ufw/before.rules ] && ! grep -q "globalesim-scanners" /etc/ufw/before.rules; then
  RULES="# globalesim-scanners: drop SIP scanner tools"
  for ua in friendly-scanner sipvicious sipcli sip-scan VaxSIPUserAgent pplsip sundayddr SIPScan; do
    for proto in udp tcp; do RULES="$RULES
-A ufw-before-input -p $proto --dport 5060 -m string --string \"$ua\" --algo bm -j DROP"; done
  done
  awk -v r="$RULES" '/^# End required lines/ && !d {print; print r; d=1; next} {print}' /etc/ufw/before.rules > /tmp/before.rules && cat /tmp/before.rules > /etc/ufw/before.rules
fi
ufw reload >/dev/null 2>&1 || true
KEEP_IPS=$(grep -oP '^ignoreip\s*=\s*\K.*' /etc/fail2ban/jail.d/asterisk.local 2>/dev/null | tr ' ' '\n' | grep -v '^127.0.0.1/8$' | tr '\n' ' ' || true)
ADMIN_IP=$(echo "${SSH_CLIENT:-}" | awk '{print $1}' || true)
IGNORE_IPS=$(echo "127.0.0.1/8 $KEEP_IPS $ADMIN_IP" | tr ' ' '\n' | grep -v '^$' | sort -u | tr '\n' ' ')
# bans: 1 hour the first time, doubling for every repeat (2 h, 4 h, … up to 4 weeks) — a customer or employee who types
# a wrong password, or an old login that keeps retrying, is back in an hour; real attackers stay out. The ban database is
# kept for 10 years so repeat offenders are remembered; ipset keeps thousands of bans fast
cat > /etc/fail2ban/fail2ban.local <<EOF
[Definition]
dbpurgeage = 3650d
EOF
apt-get install -y -qq ipset >/dev/null 2>&1 || true
cat > /etc/fail2ban/jail.d/asterisk.local <<EOF
[asterisk]
enabled  = true
backend  = polling
filter   = asterisk
logpath  = /var/log/asterisk/fail2ban
banaction = iptables-ipset-proto6-allports
maxretry = 5
findtime = 600
bantime  = 3600
bantime.increment = true
bantime.maxtime = 4w
ignoreip = $IGNORE_IPS
EOF
systemctl enable --now fail2ban >/dev/null 2>&1; systemctl restart fail2ban

say "5b Customer service: accounts, minutes, safety limits, HTTPS API ($API_DOMAIN)"
GS_REF=${GS_REF:-voice}
AGIDIR=$(grep -oP '^\s*astagidir\s*=>\s*\K\S+' /etc/asterisk/asterisk.conf 2>/dev/null || true); AGIDIR=${AGIDIR:-/usr/share/asterisk/agi-bin}
mkdir -p /opt/gsvoice /var/lib/gsvoice/vm /var/lib/gsvoice/greet "$AGIDIR"
curl -fsSL "https://raw.githubusercontent.com/ncaseiro/globalesim-android/$GS_REF/voice/gsvoice.py" -o /opt/gsvoice/gsvoice.py.new && python3 -m py_compile /opt/gsvoice/gsvoice.py.new && mv /opt/gsvoice/gsvoice.py.new /opt/gsvoice/gsvoice.py
cat > "$AGIDIR/gs-agi.py" <<'EOF'
#!/usr/bin/env python3
import sys; sys.path.insert(0, "/opt/gsvoice"); import gsvoice; gsvoice.agi_main(sys.argv[1:])
EOF
chmod 755 "$AGIDIR/gs-agi.py"
# Natural voices for phone-menu greetings (Piper, runs locally — free). Skipped quietly if a download fails.
PIPER=/opt/piper; mkdir -p "$PIPER/voices"
if [ ! -x "$PIPER/piper" ]; then
  curl -fsSL https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz -o /tmp/piper.tgz \
    && tar xzf /tmp/piper.tgz -C /opt && rm -f /tmp/piper.tgz || echo "  (natural voices not installed — greetings use the basic voice)"
fi
for v in en/en_US/lessac/medium/en_US-lessac-medium en/en_US/amy/medium/en_US-amy-medium en/en_US/ryan/high/en_US-ryan-high en/en_US/joe/medium/en_US-joe-medium \
         es/es_MX/claude/high/es_MX-claude-high es/es_ES/davefx/medium/es_ES-davefx-medium pt/pt_BR/faber/medium/pt_BR-faber-medium fr/fr_FR/siwis/medium/fr_FR-siwis-medium; do
  f="$PIPER/voices/$(basename $v)"
  [ -s "$f.onnx" ] || { curl -fsSL "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/$v.onnx" -o "$f.onnx" \
      && curl -fsSL "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/$v.onnx.json" -o "$f.onnx.json" || rm -f "$f.onnx" "$f.onnx.json"; }
done
echo "  natural voices: $(ls $PIPER/voices/*.onnx 2>/dev/null | wc -l) installed"

# Music on hold (callers hear it when put on hold)
apt-get install -y -qq asterisk-moh-opsound-gsm >/dev/null 2>&1 || true
cat > /etc/asterisk/musiconhold.conf <<EOF
[general]
[default]
mode=files
directory=moh
random=yes
#include "/var/lib/gsvoice/moh.conf"
EOF
[ -f /var/lib/gsvoice/moh.conf ] || echo "; company hold music — written by gsvoice" > /var/lib/gsvoice/moh.conf
# Hold music plays like a radio: one never-ending stream per company, callers join wherever it is (mid-song or the next song)
cat > /usr/local/bin/gs-moh-stream <<'EOF'
#!/bin/bash
# Usage: gs-moh-stream <folder> — plays every .wav in the folder in order, forever, as 8 kHz raw audio for Asterisk.
# Asterisk reads it in real time; if Asterisk stops reading, sox fails and we exit (Asterisk starts us again when needed).
D="$1"
while true; do
  shopt -s nullglob; F=("$D"/*.wav); shopt -u nullglob
  [ ${#F[@]} -eq 0 ] && { sleep 2; continue; }
  for f in "${F[@]}"; do
    sox -q "$f" -t raw -r 8000 -c 1 -e signed-integer -b 16 - 2>/dev/null || { [ -f "$f" ] && exit 0; }
  done
done
EOF
chmod 755 /usr/local/bin/gs-moh-stream
mkdir -p /var/lib/gsvoice/moh
chown asterisk:asterisk /etc/asterisk/musiconhold.conf /var/lib/gsvoice/moh.conf /var/lib/gsvoice/moh

# Call parking: one lot per company, written by gsvoice (slots 801-899)
cat > /etc/asterisk/res_parking.conf <<EOF
[general]
#include /var/lib/gsvoice/parking.conf
EOF
[ -f /var/lib/gsvoice/parking.conf ] || echo "; company parking lots — written by gsvoice" > /var/lib/gsvoice/parking.conf
chown asterisk:asterisk /etc/asterisk/res_parking.conf /var/lib/gsvoice/parking.conf

# Conference (merge calls): callers wait in the user's room, the user joins; when the user hangs up the conference ends
cat > /etc/asterisk/confbridge.conf <<EOF
[gs_bridge]
type=bridge
max_members=10
[gs_guest]
type=user
quiet=yes
music_on_hold_when_empty=yes
end_marked=yes
dsp_drop_silence=yes
[gs_host]
type=user
marked=yes
quiet=yes
dsp_drop_silence=yes
EOF
chown asterisk:asterisk /etc/asterisk/confbridge.conf

# Asterisk's built-in web server, local only — Caddy passes browser calls (wss://…/ws) to it
cat > /etc/asterisk/http.conf <<EOF
[general]
enabled=yes
bindaddr=127.0.0.1
bindport=8088
EOF
chown asterisk:asterisk /etc/asterisk/http.conf; chmod 640 /etc/asterisk/http.conf
# Asterisk manager (local only): lets gsvoice hand incoming texts to the app
AMI_SECRET=$(jget ami_secret); [ -n "$AMI_SECRET" ] || AMI_SECRET=$(openssl rand -hex 16)
cat > /etc/asterisk/manager.conf <<EOF
[general]
enabled = yes
bindaddr = 127.0.0.1
port = 5038

[gsvoice]
secret = $AMI_SECRET
deny = 0.0.0.0/0.0.0.0
permit = 127.0.0.1/255.255.255.255
read = system,call
write = message,call
EOF
chown asterisk:asterisk /etc/asterisk/manager.conf; chmod 640 /etc/asterisk/manager.conf
SN_SEC="$SN_SEC" GTTS_KEY="$GTTS_KEY" BK_EP="$( [ -n "$BK_USER" ] && echo voipms )" BK_DID="${BK_DID:-}" BK_RING="${BK_RING:-}" VMS_USER="$VMS_USER" VMS_PASS="$VMS_PASS" VMS_SELL="$VMS_SELL" VMS_ACCT="${BK_USER:-}" VMS_SERVER="${BK_SERVER:-}" python3 - "$GSJ" "$API_DOMAIN" "$TX_KEY" "$TX_CONN" "$API_TOKEN" "${DID:-}" "$TEST_USER" "$AMI_SECRET" "${SINCH_MAP:-}" "${SINCH_HOST:-}" "${SN_PROJ:-}" "${SN_KID:-}" <<'EOF'
import json, os, sys
p, dom, key, conn, tok, did, test, ami, smap, shost, sproj, skid = sys.argv[1:13]
try: c = json.load(open(p))
except Exception: c = {}
c.update({"sip_domain": dom, "telnyx_api_key": key, "telnyx_connection_id": conn, "api_token": tok, "company_did": did, "test_user": test, "ami_secret": ami,
          "did_alias": dict(x.split("=", 1) for x in smap.split(",") if x.count("=") == 1 and all(x.split("="))),
          "sinch_host": shost, "sinch_project_id": sproj, "sinch_key_id": skid, "sinch_key_secret": os.environ.get("SN_SEC", ""),
          "google_tts_key": os.environ.get("GTTS_KEY", "")})
bd, br = os.environ.get("BK_DID", ""), os.environ.get("BK_RING", "")
if bd and br and bd != br: c["did_alias"][bd] = br          # the VoIP.ms backup number rings like one of our numbers
c["backup_ep"] = os.environ.get("BK_EP", ""); c["backup_did"] = bd
c.update({"voipms_api_user": os.environ.get("VMS_USER", ""), "voipms_api_pass": os.environ.get("VMS_PASS", ""),
          "voipms_account": os.environ.get("VMS_ACCT", ""), "voipms_server": os.environ.get("VMS_SERVER", "") or "sanjose1.voip.ms",
          "voipms_sell": os.environ.get("VMS_SELL", "0") == "1"})
c.setdefault("notify_url", "https://globalesim.tel/app.php?api=voice_hook")   # voicemail emails go out from the website
json.dump(c, open(p, "w"), indent=2)
EOF
chown -R asterisk:asterisk /opt/gsvoice /var/lib/gsvoice /etc/gsvoice; chmod 640 "$GSJ"; chmod 750 /etc/gsvoice
echo '41 5 * * * asterisk /usr/bin/python3 /opt/gsvoice/gsvoice.py daily >/dev/null 2>&1' > /etc/cron.d/gsvoice-daily   # releases numbers unpaid 30+ days
rm -f /etc/cron.d/gsvoice-sinch
echo '*/5 * * * * asterisk /usr/bin/python3 /opt/gsvoice/gsvoice.py auto-adopt --quiet >/dev/null 2>&1' > /etc/cron.d/gsvoice-adopt
echo '2-59/5 * * * * asterisk /usr/bin/python3 /opt/gsvoice/gsvoice.py carrier-watch >/dev/null 2>&1' > /etc/cron.d/gsvoice-watch   # outage alarm: test incoming calls on both carriers   # numbers bought in the Telnyx portal / Sinch dashboard join the pool by themselves
# Security report: root collects fail2ban numbers (only root can), the report itself runs as asterisk and is emailed ~6 AM Central
cat > /usr/local/bin/gsvoice-f2b <<'EOF'
#!/bin/bash
B=$(fail2ban-client status asterisk 2>/dev/null | awk -F: '/Currently banned/{gsub(/ /,"",$2);print $2}')
N=$(grep -cE "\] Ban " /var/log/fail2ban.log 2>/dev/null)     # new bans only (not "Restore Ban" after a restart); F=$(grep -c "failed for" /var/log/asterisk/fail2ban 2>/dev/null)
P=/var/lib/gsvoice/f2b.json; O=$(cat $P.prev 2>/dev/null || echo "0 0")
read ON OF <<< "$O"
printf '{"banned": %s, "new_24h": %s, "failed_24h": %s}\n' "${B:-0}" "$(( ${N:-0} >= ON ? ${N:-0} - ON : ${N:-0} ))" "$(( ${F:-0} >= OF ? ${F:-0} - OF : ${F:-0} ))" > $P
echo "${N:-0} ${F:-0}" > $P.prev; chown asterisk:asterisk $P $P.prev
EOF
chmod 755 /usr/local/bin/gsvoice-f2b
cat > /etc/cron.d/gsvoice-report <<'EOF'
55 11 * * * root /usr/local/bin/gsvoice-f2b
5 12 * * * asterisk /usr/bin/python3 /opt/gsvoice/gsvoice.py report >/dev/null 2>&1
EOF
cat > /etc/systemd/system/gsvoice-api.service <<EOF
[Unit]
Description=GlobaleSIM Voice customer API
After=network.target asterisk.service
[Service]
User=asterisk
Group=asterisk
ExecStart=/usr/bin/python3 /opt/gsvoice/gsvoice.py api
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
# HTTPS in front of the API (automatic Let's Encrypt certificate once DNS points here)
if ! command -v caddy >/dev/null; then
  rm -f /etc/apt/sources.list.d/caddy-stable.list   # Ubuntu 24.04 ships caddy itself; no third-party repo needed
  apt-get install -y -qq caddy >/dev/null
fi
mkdir -p /etc/caddy && chmod 755 /etc/caddy
cat > /etc/caddy/Caddyfile <<EOF
$API_DOMAIN {
    handle /api/* {
        reverse_proxy 127.0.0.1:8090
    }
    handle /ws {
        reverse_proxy 127.0.0.1:8088
    }
    handle /prov/* {
        reverse_proxy 127.0.0.1:8090
    }
    respond "GlobaleSIM Voice" 200
}
EOF
chmod 644 /etc/caddy/Caddyfile /etc/systemd/system/gsvoice-api.service   # readable by the caddy user (script runs with umask 077)
ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
systemctl daemon-reload; systemctl enable gsvoice-api caddy >/dev/null 2>&1
systemctl restart gsvoice-api caddy


say "6/6 Starting Asterisk"
systemctl enable asterisk >/dev/null 2>&1
systemctl restart asterisk
# Receive-only texting: Telnyx messaging profile → https://$API_DOMAIN/api/sms/in/… and every number attached
if [ -n "$TX_KEY" ]; then
  GSVOICE_CONF="$GSJ" python3 /opt/gsvoice/gsvoice.py sms-setup || true
  chown asterisk:asterisk "$GSJ"; chmod 640 "$GSJ"
  systemctl restart gsvoice-api
fi
sleep 6
asterisk -rx "logger reload" >/dev/null || true
if [ -n "$DL_USER" ]; then
  for i in 1 2 3 4 5 6; do asterisk -rx "pjsip show registrations" | grep carrier-reg | grep -q " Registered" && break; sleep 5; done
  asterisk -rx "pjsip show registrations" | grep -E "carrier-reg" || true
  if ! asterisk -rx "pjsip show registrations" | grep carrier-reg | grep -q " Registered"; then
    printf '\n\033[1;31m!! The server could NOT sign in to Telnyx — calls will fail.\033[0m\n'
    echo "   Usually the SIP username or password was typed wrong."
    if [ -n "$PRE_BACKUP" ] && [ "${PRE_REG:-0}" -ge 1 ]; then
      read -rp "   Put back the setup from before this install (it was signed in)? [Y/n] " x
      if [ "${x:-y}" != "n" ] && [ "${x:-y}" != "N" ]; then
        systemctl stop gsvoice-api || true
        tar xzf "$PRE_BACKUP/settings.tgz" -C /; chown -R asterisk:asterisk /etc/asterisk 2>/dev/null || true
        systemctl restart asterisk; systemctl start gsvoice-api; sleep 8
        asterisk -rx "pjsip show registrations" | grep carrier-reg || true
        echo "   Rolled back. Run the installer again and re-enter the Telnyx SIP username/password carefully."
        exit 1
      fi
    fi
  fi
fi
if [ -n "$BK_USER" ]; then
  for i in 1 2 3 4 5 6; do asterisk -rx "pjsip show registrations" | grep voipms-reg | grep -q " Registered" && break; sleep 5; done
  asterisk -rx "pjsip show registrations" | grep voipms-reg || true
  if asterisk -rx "pjsip show registrations" | grep voipms-reg | grep -q " Registered"; then
    printf '\033[1;32mBackup carrier VoIP.ms: signed in.\033[0m\n'
  else
    printf '\n\033[1;33m!! Not signed in to VoIP.ms yet (Telnyx calls are not affected).\033[0m\n'
    echo "   Check the VoIP.ms sub account username/password, then run the installer again."
  fi
fi

cat <<EOF

================  DONE  ================
Test phone login (for Zoiper or Linphone on your phone):
   Server / domain : $PUBIP
   Username        : $TEST_USER
   Password        : $( [ "$NEW_PASS" = 1 ] && echo "$TEST_PASS   <-- NEW, copy it into Zoiper now; it will not be shown again" || echo "(unchanged — see /etc/globalesim-voice.conf)" )
   Transport       : UDP, port 5060
Your number        : ${DID:-not set yet}
Carrier            : $( [ -n "$DL_USER" ] && echo "$PROVIDER ($DL_USER @ $DL_SERVER) — 'Registered' above means it's connected" || echo "NOT set yet — run 'bash install.sh' again after creating the SIP account")
911                : $( [ "$E911_ON" = 1 ] && echo "ON" || echo "OFF (plays 'no service' — turn on E911 at your carrier, then rerun)")

Keep this password private. Settings are saved in $CONF (root only).
Customer API       : https://$API_DOMAIN/api/health  (key: see /etc/gsvoice/gsvoice.json — never share it)
Telnyx number API  : $( [ -n "$TX_KEY" ] && echo "configured" || echo "not set (number buying from the store disabled)" )
Check status any time:  asterisk -rx "pjsip show registrations"
=======================================
EOF
