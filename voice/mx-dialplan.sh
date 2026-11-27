#!/bin/bash
# Adds Mexico dialing (52 + 10 digits) to [from-customer] in /etc/asterisk/extensions.conf, then reloads the dial plan.
# Safe to run twice. Lines without a Mexican number are still refused by the voice server (intl_off).
set -e
F=/etc/asterisk/extensions.conf
grep -q '_52XXXXXXXXXX' "$F" && { echo "Mexico dialing is already there."; exit 0; }
cp -a "$F" "$F.bak-$(date +%Y%m%d%H%M)"
python3 - "$F" <<'PY'
import sys
f = sys.argv[1]; s = open(f).read()
i = s.index("[from-customer]"); j = s.index("exten => _X.,1,Playback(ss-noservice)", i)
blk = """; Mexico (lines with a Mexican number only; the voice server checks it and caps the minutes)
exten => _+52XXXXXXXXXX,1,Goto(from-customer,${EXTEN:1},1)
exten => _01152XXXXXXXXXX,1,Goto(from-customer,${EXTEN:3},1)
exten => _52XXXXXXXXXX,1,Set(__TRANSFER_CONTEXT=gs-transfer)
 same => n,Set(GS_EP=${CHANNEL(endpoint)})
 same => n,AGI(gs-agi.py,out,${GS_EP},${EXTEN},${PJSIP_HEADER(read,X-GS-CID)})
 same => n,GotoIf($["${GS_OK}" = "1"]?ok:blocked)
 same => n(ok),Set(GROUP(gsuser)=${GS_BASE})
 same => n,GotoIf($[${GROUP_COUNT(${GS_BASE}@gsuser)} > 2]?blocked)
 same => n,Set(GROUP(gsall)=out)
 same => n,GotoIf($[${GROUP_COUNT(out@gsall)} > ${GS_GMAX}]?blocked)
 same => n,GotoIf($["${GS_CO}" = ""]?go)
 same => n,Set(GROUP(gsco)=${GS_CO})
 same => n,GotoIf($[${GROUP_COUNT(${GS_CO}@gsco)} > ${GS_COMAX}]?blocked)
 same => n(go),Set(CALLERID(all)=<${GS_CID}>)
 same => n,Set(CHANNEL(hangup_handler_push)=gs-charge,s,1(${GS_CALLID}))
 same => n,Set(CHANNEL(hangup_handler_push)=gs-qos,s,1(${GS_CALLID}))
 same => n,Dial(PJSIP/${DIALPFX}${EXTEN}@carrier,60,L(${GS_MAXMS})U(gs-outans^${GS_CALLID}^${GS_MAXSEC}))
 same => n,Hangup()
 same => n(blocked),Playback(ss-noservice)
 same => n,Hangup()
"""
open(f, "w").write(s[:j] + blk + s[j:])
PY
asterisk -rx "dialplan reload" >/dev/null
asterisk -rx "dialplan show 523312345678@from-customer" | grep -q AGI && echo "OK — Mexico dialing added." || { echo "Something is off — restoring the old dial plan."; cp -a "$(ls -t $F.bak-* | head -1)" "$F"; asterisk -rx "dialplan reload"; exit 1; }
