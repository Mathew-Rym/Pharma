#!/usr/bin/env bash
# Pharma OS launcher. One entry point so nobody has to remember which directory each
# service must be started from, or to source .env first (forgetting that is the single
# most common "it doesn't work").
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
PY="$ROOT/.venv/bin/python"

[ -f .env ] || { echo "no .env — copy .env.example and fill it in"; exit 1; }
set -a; . ./.env; set +a
[ -x "$PY" ] || { echo "no venv at .venv — see RUNBOOK.md"; exit 1; }

api_up()  { curl -sf --max-time 2 http://127.0.0.1:8000/health  >/dev/null 2>&1; }
dash_up() { curl -sf --max-time 2 http://127.0.0.1:8501/_stcore/health >/dev/null 2>&1; }

case "${1:-help}" in

api)
  cd "$ROOT/api"
  exec "$PY" -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
  ;;

dashboard|dash)
  cd "$ROOT/dashboard"
  exec "$PY" -m streamlit run app.py --server.port 8501 \
       --server.headless true --browser.gatherUsageStats false
  ;;

whatsapp|wa)
  # `docker compose` is a CLI PLUGIN, not part of docker itself, and it is missing on
  # plenty of Ubuntu installs -- including this one. When it is absent the compose
  # command fails with "unknown shorthand flag: 'f'", which reads like a typo rather
  # than a missing dependency, and localhost:3001 just never comes up. So: use compose
  # when it exists, otherwise run the same container directly. Same result either way,
  # no extra install.
  : "${GOWA_PASS:?set GOWA_PASS in .env}"
  : "${GOWA_WEBHOOK_SECRET:?set GOWA_WEBHOOK_SECRET in .env}"

  if docker compose version >/dev/null 2>&1; then
    docker compose -f wa-gowa/docker-compose.yml up -d
    echo "Logs: docker compose -f wa-gowa/docker-compose.yml logs -f"
  else
    echo "docker compose plugin not installed - running the container directly"
    docker rm -f pharmaos-gowa >/dev/null 2>&1 || true
    docker volume create gowa-storage >/dev/null
    docker run -d --name pharmaos-gowa --restart unless-stopped \
      -p 3001:3000 \
      --add-host host.docker.internal:host-gateway \
      -v gowa-storage:/app/storages \
      -e APP_PORT=3000 \
      -e APP_DEBUG=false \
      -e APP_OS="Pharma OS" \
      -e APP_BASIC_AUTH="${GOWA_USER:-pharmaos}:${GOWA_PASS}" \
      -e WHATSAPP_WEBHOOK="${GOWA_WEBHOOK_URL:-http://host.docker.internal:8000/webhook/gowa}" \
      -e WHATSAPP_WEBHOOK_SECRET="${GOWA_WEBHOOK_SECRET}" \
      -e WHATSAPP_WEBHOOK_EVENTS=message \
      -e WHATSAPP_WEBHOOK_IGNORE_JIDS=@g.us \
      -e WHATSAPP_AUTO_DOWNLOAD_MEDIA=true \
      -e WHATSAPP_PRESENCE_ON_CONNECT=unavailable \
      -e WHATSAPP_AUTO_MARK_READ=true \
      -e WHATSAPP_AUTO_REJECT_CALL=true \
      -e WHATSAPP_ACCOUNT_VALIDATION=true \
      aldinokemal2104/go-whatsapp-web-multidevice:latest rest
    echo "Logs: docker logs -f pharmaos-gowa"
  fi

  echo -n "waiting for GOWA "
  for _ in $(seq 1 60); do
    curl -sf --max-time 2 -u "${GOWA_USER:-pharmaos}:${GOWA_PASS}" \
         http://127.0.0.1:3001/app/info >/dev/null 2>&1 && break
    echo -n "."; sleep 2
  done
  echo
  if curl -sf --max-time 3 -u "${GOWA_USER:-pharmaos}:${GOWA_PASS}" \
       http://127.0.0.1:3001/app/info >/dev/null 2>&1; then
    echo "  GOWA is up:  http://localhost:3001"
    echo "  Login:       ${GOWA_USER:-pharmaos} / (GOWA_PASS from .env)"
    echo
    echo "  Next: ./run.sh qr    then scan with the pharmacy SIM"
  else
    echo "  GOWA did not come up. Check: docker logs pharmaos-gowa"
  fi
  ;;

qr)
  # Pair a WhatsApp number, entirely from the terminal.
  #
  # GOWA v9 no longer bundles its web dashboard -- it downloads it from GitHub at
  # startup, and that download fails with 403 behind many networks, which is why
  # http://localhost:3001 can look dead while the API is perfectly healthy. So we do
  # not depend on the browser UI at all.
  #
  # From v8 it is multi-device: you must CREATE a device slot first, then request the
  # QR scoped to it. `GET /app/login` with no device returns DEVICE_ID_REQUIRED.
  : "${GOWA_PASS:?set GOWA_PASS in .env}"
  "$PY" - <<'PYEOF'
import sys, time
from pathlib import Path
sys.path.insert(0, "api")
import httpx
from config import settings

base = settings.GOWA_URL.rstrip("/")
auth = (settings.GOWA_USER or "pharmaos", settings.GOWA_PASS)


def devices():
    # Pairing state is split across TWO endpoints in GOWA v9 and neither alone is
    # enough. /devices lists the slots with {"id", "state"}; /app/devices reports the
    # WhatsApp account with {"device", "jid", "name"}. Nothing anywhere returns the
    # `connected` / `logged_in` / `device_id` keys this script used to look for, so the
    # scan-detection loop below could never fire -- a successful pairing still printed
    # "Not paired yet", and an already-paired number was offered a pointless new QR.
    # Merge both, keyed by slot id, and normalise to the fields the rest of this uses.
    slots = {}
    r = httpx.get(f"{base}/devices", auth=auth, timeout=15)
    for d in (r.json() or {}).get("results") or []:
        did = d.get("id") or d.get("device_id")
        slots[did] = {"device_id": did, "state": d.get("state") or ""}
    try:
        r = httpx.get(f"{base}/app/devices", auth=auth, timeout=15)
        for d in (r.json() or {}).get("results") or []:
            did = d.get("device") or d.get("device_id")
            slots.setdefault(did, {"device_id": did, "state": ""})
            slots[did]["jid"] = d.get("jid") or ""
            slots[did]["push_name"] = d.get("name") or ""
    except Exception:
        pass  # slot list alone is still usable
    return list(slots.values())


def paired(d):
    # jid is the ONLY trustworthy signal: WhatsApp only hands back an account id once
    # the handshake completed, and a paired-but-offline device keeps it across restarts.
    #
    # Do NOT trust state=="connected" from /devices. That is transport-level -- it flips
    # to connected the moment GOWA opens its socket to WhatsApp to *show you a QR*, with
    # nobody logged in and jid still empty. Treating it as proof reports "PAIRED." for an
    # unscanned QR, which is worse than the original bug: you'd chase silent messaging
    # failures instead of just scanning again.
    # state=="logged_in" is the other authoritative value; observed alongside a jid the
    # moment pairing completes.
    return bool(d.get("jid") or d.get("state") == "logged_in"
                or d.get("connected") or d.get("logged_in"))


try:
    devs = devices()
except Exception as e:
    print(f"GOWA not reachable at {base}: {e}\nStart it with: ./run.sh whatsapp")
    sys.exit(1)

connected = [d for d in devs if paired(d)]
if connected:
    print("Already paired:")
    for d in connected:
        print(f"   {d.get('device_id') or d.get('jid')}  {d.get('push_name') or ''}")
    print("\nSet GOWA_DEVICE_ID in .env to that id if it is not already.")
    print("To pair a different number: ./run.sh unpair, then ./run.sh qr")
    sys.exit(0)

dev_id = settings.GOWA_DEVICE_ID or (devs[0].get("device_id") if devs else None)
if dev_id and not any(d.get("device_id") == dev_id for d in devs):
    # GOWA_DEVICE_ID names a slot that no longer exists (a `docker rm` without the
    # volume, or an unpair that dropped it). Requesting a QR against it just returns
    # DEVICE_ID_REQUIRED, so recreate the slot under the same name first.
    r = httpx.post(f"{base}/devices", auth=auth, timeout=20, json={"device_id": dev_id})
    print(f"recreated missing device slot: {dev_id}"
          if r.status_code in (200, 201) else
          f"could not recreate slot {dev_id}: {r.status_code} {r.text[:200]}")
if not dev_id:
    r = httpx.post(f"{base}/devices", auth=auth, timeout=20,
                   json={"device_id": "pharmacy-1"})
    if r.status_code not in (200, 201):
        print(f"could not create a device slot: {r.status_code} {r.text[:200]}")
        sys.exit(1)
    dev_id = ((r.json() or {}).get("results") or {}).get("device_id") or "pharmacy-1"
    print(f"created device slot: {dev_id}")

hdrs = {"X-Device-Id": dev_id}
out = Path(".run"); out.mkdir(exist_ok=True)
png = out / "whatsapp-qr.png"


def draw(im_bytes):
    # Draw into the terminal too, so a headless box can pair without opening a file.
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(im_bytes)).convert("L")
        w, h = im.size
        # QR images are a fixed module grid; sample it back down to that grid.
        n = 45
        step_x, step_y = w / n, h / n
        for gy in range(n):
            row = ""
            for gx in range(n):
                px = im.getpixel((min(int(gx * step_x + step_x / 2), w - 1),
                                  min(int(gy * step_y + step_y / 2), h - 1)))
                row += "  " if px > 128 else "██"
            print(row)
    except Exception:
        pass


def fetch_qr():
    """Ask GOWA for a pairing QR, save it to .run/whatsapp-qr.png, draw it. False on
    failure."""
    try:
        r = httpx.get(f"{base}/app/login", auth=auth, headers=hdrs, timeout=90)
        res = (r.json() or {}).get("results") or {}
    except Exception as e:
        print(f"login request failed: {e}")
        return False

    code = res.get("qr_code") or res.get("code")
    link = res.get("qr_link")

    if code:
        # Raw payload: render it straight into the terminal, no browser needed.
        try:
            import qrcode
            q = qrcode.QRCode(border=1)
            q.add_data(code)
            q.make()
            q.print_ascii(invert=True)
        except Exception:
            print("QR payload (paste into any QR generator):\n", code)
        return True
    if link:
        # GOWA returns an ABSOLUTE url built from its own container hostname, e.g.
        # http://localhost/statics/qrcode/scan-qr-xxx.png -- port 80 inside the
        # container, which is not reachable from here. Keep only the path and re-join
        # to GOWA_URL.
        from urllib.parse import urlparse
        path = urlparse(str(link)).path or str(link)
        url = f"{base}/{path.lstrip('/')}"
        try:
            img = httpx.get(url, auth=auth, timeout=30)
            img.raise_for_status()
            png.write_bytes(img.content)
            print(f"QR saved to: {png.resolve()}   (refreshes here automatically)")
            draw(img.content)
            return True
        except Exception as e:
            print(f"could not download the QR image ({e})")
            print(f"Try opening: {url}")
            return False
    print("No QR returned:", res)
    return False


print(f"On the pharmacy phone: WhatsApp -> Settings -> Linked devices -> Link a device")
print(f"Device id: {dev_id}   (GOWA_DEVICE_ID in .env)\n")

# A WhatsApp QR dies after ~60s, and GOWA rotates to a new one roughly every 90s --
# but /app/login returns only the FIRST one and then logs "QR context canceled while
# sending QR path" as it tries to push the rest into a request we already closed. So a
# single fetch gives you one ~60s window: open the png, walk to the phone, and it is
# already dead. That is the whole reason pairing "never works". Re-fetch on a loop so
# the file on disk is always a live code and you get several attempts.
ROUNDS, POLL_SECS = 6, 50
for rnd in range(ROUNDS):
    if rnd:
        print(f"\nThat code expired. Fresh QR ({rnd + 1}/{ROUNDS})...\n")
    if not fetch_qr():
        sys.exit(1)
    print("\nWaiting for the scan", end="", flush=True)
    for _ in range(POLL_SECS // 5):
        time.sleep(5)
        try:
            if [d for d in devices() if paired(d)]:
                print("\n\n  PAIRED.")
                print("  Next: ./run.sh brand   (pushes the logo + display name)")
                sys.exit(0)
        except Exception:
            pass
        print(".", end="", flush=True)

print(f"\n\nStill not paired after {ROUNDS} codes. The phone must be on the "
      "'Link a device' screen BEFORE the QR appears. Rerun: ./run.sh qr")
PYEOF
  ;;

unpair)
  : "${GOWA_PASS:?set GOWA_PASS in .env}"
  D="${GOWA_DEVICE_ID:-pharmacy-1}"
  curl -s -u "${GOWA_USER:-pharmaos}:${GOWA_PASS}" -H "X-Device-Id: $D" \
       "http://127.0.0.1:3001/app/logout" | head -c 300
  echo
  echo "Logged out. Run ./run.sh qr to pair a different number."
  ;;

all)
  mkdir -p .run
  # setsid, not just nohup+&. A backgrounded child stays in this script's process
  # group, so whatever reaps the launcher -- a terminal closing, a CI step finishing,
  # an agent harness cleaning up -- takes the services down with it. setsid puts them
  # in their own session so they outlive the thing that started them.
  DETACH="setsid"; command -v setsid >/dev/null || DETACH="nohup"
  api_up  || { (cd api && $DETACH "$PY" -m uvicorn main:app --host 0.0.0.0 --port 8000 \
                 >"$ROOT/.run/api.log" 2>&1 < /dev/null &) ; }
  dash_up || { (cd dashboard && $DETACH "$PY" -m streamlit run app.py --server.port 8501 \
                 --server.headless true --browser.gatherUsageStats false \
                 >"$ROOT/.run/dashboard.log" 2>&1 < /dev/null &) ; }
  for _ in $(seq 1 30); do api_up && dash_up && break; sleep 1; done
  echo
  api_up  && echo "  API        http://localhost:8000/docs" || echo "  API        FAILED — see .run/api.log"
  dash_up && echo "  Dashboard  http://localhost:8501"      || echo "  Dashboard  FAILED — see .run/dashboard.log"
  echo "  WhatsApp   ./run.sh whatsapp   (optional; ./run.sh say works without it)"
  echo
  echo "Next: open the dashboard -> Setup -> add your WhatsApp number as owner + a PIN."
  echo "WhatsApp ignores any number that is not in staff, silently."
  ;;

stop)
  pkill -f "uvicorn main:app" 2>/dev/null || true
  pkill -f "streamlit run app.py" 2>/dev/null || true
  # Stop whichever way GOWA was started. NOT `docker rm` -- that would delete the
  # container but the WhatsApp session lives in the named volume, so pairing survives
  # a stop/start. Only ./run.sh unpair should end a session.
  docker compose -f wa-gowa/docker-compose.yml down 2>/dev/null || true
  docker stop pharmaos-gowa >/dev/null 2>&1 || true
  echo "stopped (WhatsApp pairing preserved in the gowa-storage volume)"
  ;;

say)
  # Fake an inbound WhatsApp message. Works with no phone and no GOWA.
  api_up || { echo "API is not running — ./run.sh api"; exit 1; }
  TEXT="${2:?usage: ./run.sh say \"EXPIRY\" [phone]}"
  FROM="${3:-}"
  if [ -z "$FROM" ]; then
    FROM=$("$PY" -c "
import sys; sys.path.insert(0,'api')
from db import q1
from config import settings
r=q1(\"select phone from staff where pharmacy_id=%s and is_active order by case role when 'owner' then 0 else 1 end limit 1\",(settings.PHARMACY_ID,))
print(r['phone'] if r else '')" 2>/dev/null | tail -1)
  fi
  [ -n "$FROM" ] || { echo "no active staff — add your number in the dashboard first"; exit 1; }
  echo "you -> $TEXT   (as $FROM)"
  # Show only replies produced AFTER this message, and WAIT for them. Reading a fixed
  # number of recent rows after a fixed sleep prints stale replies from an earlier
  # message whenever the model takes an extra second, which looks exactly like a bug.
  "$PY" - "$FROM" "$TEXT" <<'PYEOF'
import json, sys, time
sys.path.insert(0, "api")
import httpx
from config import settings
from db import q

phone, text = sys.argv[1], sys.argv[2]
since = q("select now() as t")[0]["t"]
r = httpx.post("http://127.0.0.1:8000/dev/simulate",
               headers={"x-pharmaos-secret": settings.SHARED_SECRET},
               json={"from": phone, "text": text}, timeout=30)
if r.status_code != 200:
    print(f"   simulate failed: {r.status_code} {r.text[:120]}")
    sys.exit(1)

deadline = time.time() + 45          # vision/LLM replies can take ~30s
seen = 0
print("   ...", end="", flush=True)
while time.time() < deadline:
    rows = q("""select body, error, created_at from wa_messages
                 where direction='out' and created_at > %s
                 order by created_at""", (since,))
    if len(rows) > seen:
        for row in rows[seen:]:
            print("\r" + " " * 20)
            print("bot <- " + (row["body"] or "")[:900])
            if row["error"]:
                print("       [logged only - no WhatsApp gateway running]")
        seen = len(rows)
        deadline = time.time() + 4   # brief grace for a follow-up message
    time.sleep(1)
if not seen:
    print("\r   no reply in 45s. Is this number in `staff` and active?")
PYEOF
  ;;

brand)
  # Push the Pharma OS mark and display name onto the paired WhatsApp account, so the
  # pharmacy's customers see the logo rather than a grey silhouette. Run once after
  # pairing, and again whenever the logo changes.
  "$PY" - <<'PYEOF'
import sys
sys.path.insert(0, "api")
from pathlib import Path
import httpx
from config import settings

if settings.WA_BACKEND != "gowa":
    print("WA_BACKEND is not 'gowa' — nothing to brand."); sys.exit(0)

base = settings.GOWA_URL.rstrip("/")
auth = (settings.GOWA_USER, settings.GOWA_PASS) if settings.GOWA_USER else None
hdrs = {"X-Device-Id": settings.GOWA_DEVICE_ID} if settings.GOWA_DEVICE_ID else {}
img = Path("brand/whatsapp-profile-640.png")

if not img.exists():
    print(f"missing {img} — run: .venv/bin/python brand/make_assets.py"); sys.exit(1)

try:
    r = httpx.get(f"{base}/app/devices", auth=auth, timeout=15)
    if r.status_code == 401:
        print("GOWA rejected the credentials. Check GOWA_USER / GOWA_PASS."); sys.exit(1)
except Exception as e:
    print(f"GOWA not reachable at {base}: {e}\nStart it with ./run.sh whatsapp")
    sys.exit(1)

# Display name first: it succeeds even when no avatar can be set, so a failure here
# tells you the session is not actually paired.
try:
    r = httpx.post(f"{base}/user/pushname", json={"push_name": "Pharma OS"},
                   auth=auth, headers=hdrs, timeout=30)
    print(("  display name -> 'Pharma OS'" if r.status_code == 200
           else f"  display name failed: {r.status_code} {r.text[:120]}"))
except Exception as e:
    print(f"  display name failed: {e}")

try:
    with open(img, "rb") as fh:
        r = httpx.post(f"{base}/user/avatar",
                       files={"avatar": ("logo.png", fh, "image/png")},
                       auth=auth, headers=hdrs, timeout=60)
    print(("  profile picture -> brand/whatsapp-profile-640.png"
           if r.status_code == 200
           else f"  profile picture failed: {r.status_code} {r.text[:160]}"))
except Exception as e:
    print(f"  profile picture failed: {e}")
PYEOF
  ;;

pair)
  # Pair a pharmacy handset by CODE rather than QR.
  #
  # Use this for onboarding a real pharmacy: WhatsApp generates an 8-character code, the
  # human types it on THAT handset, and no image has to be relayed anywhere. A QR expires
  # in under a minute and cannot survive being sent to another phone as a picture, which
  # is why ./run.sh qr only works when you are standing at the machine.
  #
  # Usage: ./run.sh pair 254712345678 [slot-name]
  : "${GOWA_PASS:?set GOWA_PASS in .env}"
  PHONE="${2:?usage: ./run.sh pair <phone-with-country-code> [slot-name]}"
  SLOT="${3:-pharmacy-$(echo "$PHONE" | tail -c 5)}"
  PHONE="$PHONE" SLOT="$SLOT" "$PY" - <<'PYEOF'
import os, sys, time
sys.path.insert(0, "api")
import httpx
from config import settings

base = settings.GOWA_URL.rstrip("/")
auth = (settings.GOWA_USER or "pharmaos", settings.GOWA_PASS)
phone, slot = os.environ["PHONE"], os.environ["SLOT"]


def slots():
    r = httpx.get(f"{base}/devices", auth=auth, timeout=15)
    return {s["id"]: s for s in ((r.json() or {}).get("results") or [])}


live = slots()
if slot not in live:
    r = httpx.post(f"{base}/devices", auth=auth, timeout=20, json={"device_id": slot})
    if r.status_code not in (200, 201):
        print(f"could not create slot {slot}: {r.status_code} {r.text[:200]}")
        sys.exit(1)
    print(f"created slot {slot}")
elif live[slot].get("jid"):
    print(f"slot {slot} is ALREADY paired to {live[slot]['jid']}")
    print("To pair a different number use a new slot name, or ./run.sh unpair first.")
    sys.exit(0)

print(f"\nAsking WhatsApp for a link code for +{phone} ...\n")
# The query parameter is `phone`, NOT `phone_number` -- even though the validation error
# GOWA returns when it is missing says "phone_number(): cannot be blank", which is the
# internal field name and sends you straight to the wrong parameter. Verified against the
# running container: ?phone=2547... returns {"pair_code": "XXXX-XXXX"}.
r = httpx.get(f"{base}/app/login-with-code", auth=auth,
              headers={"X-Device-Id": slot}, params={"phone": phone}, timeout=90)
res = (r.json() or {}).get("results") or {}
code = res.get("pair_code") or res.get("code") or res.get("pairing_code")
if not code:
    print(f"no code returned: {r.status_code} {str(res)[:300]}")
    sys.exit(1)

print("=" * 46)
print(f"   CODE:  {code}")
print("=" * 46)
print(f"\nOn the handset for +{phone}:")
print("  WhatsApp -> Settings -> Linked devices")
print("  -> Link a device -> 'Link with phone number instead'")
print(f"  -> enter {code}\n")
print("Relay that code to the pharmacy however you like -- it is text, so WhatsApp,")
print("SMS or a phone call all work. Unlike a QR it does not have to be a picture.\n")

print("Waiting for the pairing", end="", flush=True)
for _ in range(40):
    time.sleep(3)
    s = slots().get(slot) or {}
    if s.get("jid"):
        print(f"\n\n  PAIRED: {slot} -> {s['jid']}")
        print("\n  Now bind it to a pharmacy:")
        print(f"    ./run.sh bind {slot}\n")
        sys.exit(0)
    print(".", end="", flush=True)
print("\n\nNot paired yet. The code expires; rerun ./run.sh pair to get a fresh one.")
PYEOF
  ;;

bind)
  # Attach a paired GOWA slot to a pharmacy row, so inbound resolves and outbound has a
  # device. Reads the JID from GOWA rather than accepting it as an argument: the JID is
  # the tenant key, and typing it by hand is how you bind the wrong handset.
  SLOT="${2:?usage: ./run.sh bind <slot-name> [pharmacy-name]}"
  SLOT="$SLOT" PHARM="${3:-}" "$PY" - <<'PYEOF'
import os, sys
sys.path.insert(0, "api")
import httpx
from config import settings
from db import ex, q, q1

base = settings.GOWA_URL.rstrip("/")
auth = (settings.GOWA_USER or "pharmaos", settings.GOWA_PASS)
slot, want = os.environ["SLOT"], os.environ.get("PHARM") or ""

r = httpx.get(f"{base}/devices", auth=auth, timeout=15)
s = {d["id"]: d for d in ((r.json() or {}).get("results") or [])}.get(slot)
if not s:
    print(f"no such slot: {slot}"); sys.exit(1)
jid = s.get("jid")
if not jid:
    print(f"slot {slot} is not paired yet -- run ./run.sh pair first"); sys.exit(1)

taken = q1("select name from pharmacies where wa_jid=%s and gowa_device_id<>%s",
           (jid, slot))
if taken:
    print(f"{jid} is already bound to {taken['name']}"); sys.exit(1)

if want:
    row = q1("select id, name from pharmacies where name = %s", (want,))
    if not row:
        row = q1("""insert into pharmacies (name, kind, timezone)
                    values (%s,'tenant','Africa/Nairobi') returning id, name""", (want,))
        print(f"created pharmacy {row['name']}")
else:
    rows = q("""select id, name, wa_jid, gowa_device_id from pharmacies
                 where kind='tenant' order by name""")
    print("\nWhich pharmacy?\n")
    for i, x in enumerate(rows, 1):
        bound = f"  (bound to {x['gowa_device_id']})" if x["gowa_device_id"] else ""
        print(f"  {i}. {x['name']}{bound}")
    pick = input("\nNumber: ").strip()
    row = rows[int(pick) - 1]

ex("update pharmacies set wa_jid=%s, gowa_device_id=%s, wa_number=%s where id=%s",
   (jid, slot, jid.split("@")[0], row["id"]))
print(f"\n  {row['name']}  <-  {slot}  ({jid})\n")
print("  Verify: ./run.sh safety\n")
PYEOF
  ;;

platform)
  # Designate a paired slot as the PLATFORM line -- the number strangers text REGISTER to.
  #
  # Until this exists, register.py answers onboarding from whichever tenant owns the
  # inbound device. That works, but it puts the ban risk of cold onboarding traffic on a
  # real pharmacy's number. One dedicated line moves it off them permanently.
  SLOT="${2:?usage: ./run.sh platform <slot-name> [display-name]}"
  SLOT="$SLOT" PNAME="${3:-Pharma OS}" "$PY" - <<'PYEOF'
import os, sys
sys.path.insert(0, "api")
import httpx
from config import settings
from db import ex, q1

slot, pname = os.environ["SLOT"], os.environ["PNAME"]
base = settings.GOWA_URL.rstrip("/")
auth = (settings.GOWA_USER or "pharmaos", settings.GOWA_PASS)

r = httpx.get(f"{base}/devices", auth=auth, timeout=15)
live = {s["id"]: s for s in ((r.json() or {}).get("results") or [])}
jid = (live.get(slot) or {}).get("jid")
if not jid:
    print(f"slot {slot} is not linked to a WhatsApp account.")
    print(f"Known slots: {', '.join(live) or '(none)'}")
    print("Pair it first: ./run.sh pair <phone> " + slot)
    sys.exit(1)

# The JID is unique across pharmacies, so a slot already bound to a tenant has to be
# released rather than duplicated -- otherwise the insert fails on the partial index and
# the message tells you nothing about why.
held = q1("select id, name, kind from pharmacies where wa_jid = %s", (jid,))
if held and held["kind"] != "platform":
    print(f"{jid} is currently bound to {held['name']} (a tenant).")
    print("Converting it to the platform line would leave that pharmacy with no device.")
    print("Pair a separate number for the platform, or unbind that pharmacy first.")
    sys.exit(1)

if held:
    ex("update pharmacies set name=%s, gowa_device_id=%s, wa_number=%s, status='active' "
       "where id=%s", (pname, slot, jid.split("@")[0], held["id"]))
    print(f"\n  updated platform line: {pname}  <-  {slot}  ({jid})\n")
else:
    row = q1("""insert into pharmacies (name, kind, status, wa_jid, gowa_device_id,
                                        wa_number, timezone)
                values (%s,'platform','active',%s,%s,%s,'Africa/Nairobi') returning id""",
             (pname, jid, slot, jid.split("@")[0]))
    print(f"\n  platform line created: {pname}  <-  {slot}  ({jid})\n")

print("  Strangers can now text REGISTER to this number.")
print("  Verify: ./run.sh safety\n")
PYEOF
  ;;

activate)
  # Bind the JID of every pharmacy whose handset has finished linking.
  #
  # Pairing is asynchronous -- we hand over a code and someone walks to another room --
  # so something has to notice when the handset actually links. Until it does, the
  # pharmacy is registered and completely mute. Cron calls the same job.
  "$PY" - <<'PYEOF'
import sys, json
sys.path.insert(0, "api")
from register import activation_sweep
print(json.dumps(activation_sweep(), indent=2))
PYEOF
  ;;

safety)
  # A gate that is off looks exactly like a gate that is on until the number is banned.
  # Print the posture so it is checkable in one command rather than inferred from .env.
  "$PY" - <<'PYEOF'
import sys; sys.path.insert(0, "api")
from config import settings
from db import q

print("\nWhatsApp safety posture\n")
allow = [p.strip() for p in settings.WA_ALLOWLIST.split(",") if p.strip()]
if allow:
    print(f"  Gate 1 allowlist        ON — ONLY these {len(allow)} number(s) can receive:")
    for a in allow:
        print(f"                            {a}")
    print("                          ** add every demo participant here or they get silence **")
else:
    print("  Gate 1 allowlist        OFF (production) — any related number can receive")
print(f"  Gate 2 relationship     ON — recipient must be a customer/staff/supplier")
print(f"  Gate 3 chat established ON — recipient must have messaged us first")
print(f"  Gate 4 rate limit       {settings.WA_RATE_LIMIT_HOUR}/hour, {settings.WA_NEW_CHAT_LIMIT_HOUR} new chats/hour, per device")
print(f"  Broadcast cap           {settings.WA_BROADCAST_MAX} recipients, {settings.WA_PACE_MIN_SECS}-{settings.WA_PACE_MAX_SECS}s randomised gap")

print("\nWho can currently be messaged (Gate 3 open)\n")
rows = q("""select p.name, ih.phone, ih.message_count
              from inbound_history ih join pharmacies p on p.id = ih.pharmacy_id
             order by p.name, ih.phone""")
for r in rows:
    print(f"  {r['name']:22} {r['phone']:14} {r['message_count']} inbound")
if not rows:
    print("  (nobody — no outbound message can be sent until someone messages in)")

print("\nRelated but NOT reachable — they have never messaged us\n")
gaps = q("""select p.name, s.phone, 'staff' as kind from staff s
              join pharmacies p on p.id = s.pharmacy_id
             where not exists (select 1 from inbound_history ih
                               where ih.pharmacy_id = s.pharmacy_id and ih.phone = s.phone)
            union all
            select p.name, c.phone, 'customer' from customers c
              join pharmacies p on p.id = c.pharmacy_id
             where not exists (select 1 from inbound_history ih
                               where ih.pharmacy_id = c.pharmacy_id and ih.phone = c.phone)""")
for r in gaps:
    print(f"  {r['name']:22} {r['phone']:14} {r['kind']} — must message the bot first")
if not gaps:
    print("  (none)")
print()
PYEOF
  ;;

check)
  "$PY" - <<'PYEOF'
import sys; sys.path.insert(0, "api")
from db import q
TABLES = ["pharmacies","staff","products","batches","stock_movements","orders",
          "order_lines","payments","prescriptions","suppliers","purchase_orders",
          "po_lines","grns","grn_lines","customers","job_runs","wa_messages",
          "agents","agent_commands","pos_sales","sync_state","sales_history_monthly",
          "stock_reconciliation","demand_forecast","duty_roster"]
VIEWS = ["v_stock_on_hand","v_expiry_risk","v_velocity_90d","v_demand_baseline",
         "v_seasonality","v_stock_variance","v_grn_verification",
         "v_open_receiving_discrepancies"]
COLS = {"staff": ["approval_pin","pin_failed_count","pin_locked_until"],
        "payments": ["source","sms_text","verified_by"],
        "products": ["preferred_supplier_id"],
        "grns": ["goods_images","unresolved_count_note"],
        "grn_lines": ["vision_packs","vision_loose","vision_confidence","vision_note",
                      "qty_counted_pieces"],
        "stock_reconciliation": ["ledger_pieces"],
        "job_runs": ["pharmacy_id"]}
have_t = {r["table_name"] for r in q("select table_name from information_schema.tables where table_schema='public'")}
have_v = {r["table_name"] for r in q("select table_name from information_schema.views where table_schema='public'")}
bad = []
for t in TABLES:
    if t not in have_t: bad.append(f"table {t}")
for v in VIEWS:
    if v not in have_v: bad.append(f"view {v}")
for t, cs in COLS.items():
    have_c = {r["column_name"] for r in q("select column_name from information_schema.columns where table_name=%s", (t,))}
    for c in cs:
        if c not in have_c: bad.append(f"{t}.{c}")
chk = q("select 1 from pg_constraint where conname='stock_movements_reason_check' and pg_get_constraintdef(oid) like '%%pos_sale%%'")
if not chk: bad.append("stock_movements CHECK missing 'pos_sale'")
if bad:
    print("DATABASE OUT OF DATE — missing:")
    for b in bad: print("   -", b)
    print("\nRun: ./run.sh migrate")
    sys.exit(1)
print(f"database is current: {len(TABLES)} tables, {len(VIEWS)} views, all columns present")
PYEOF
  ;;

migrate)
  "$PY" - <<'PYEOF'
import os, glob, re, psycopg
# Sort by the NUMBER, not the string. Lexicographically "schema_v10.sql" sorts before
# "schema_v2.sql", so plain sorted() applied v10 second -- before the eight migrations it
# builds on. That is silent on an already-migrated database and breaks a fresh install.
def _ver(path):
    m = re.search(r"schema_v(\d+)\.sql$", path)
    return int(m.group(1)) if m else 0
files = ["db/schema.sql"] + sorted(glob.glob("db/schema_v*.sql"), key=_ver)
conn = psycopg.connect(os.environ["DATABASE_URL"])
for f in files:
    try:
        with conn.cursor() as cur:
            cur.execute(open(f).read())
        conn.commit()
        print(f"  applied {f}")
    except Exception as e:
        conn.rollback()
        # schema.sql is not idempotent (plain CREATE TABLE); skipping it on an
        # existing database is correct, not a failure.
        print(f"  skipped {f}: {str(e).splitlines()[0][:90]}")
conn.close()
PYEOF
  "$0" check
  ;;

test)
  shift || true
  exec "$PY" -m pytest tests/ -q "$@"
  ;;

*)
  cat <<'EOF'
Pharma OS

  ./run.sh all         start API + dashboard, print the URLs
  ./run.sh api         API only          http://localhost:8000/docs
  ./run.sh dashboard   dashboard only    http://localhost:8501
  ./run.sh whatsapp    start GOWA        http://localhost:3001
  ./run.sh qr          print the pairing QR in the terminal
  ./run.sh unpair      log out the paired number
  ./run.sh stop        stop everything

  ./run.sh check       is the database up to date with the code
  ./run.sh migrate     apply any missing migrations, then check
  ./run.sh test        run the test suite
  ./run.sh brand       push the logo + name onto the paired WhatsApp account
  ./run.sh say "ORDER" send a fake WhatsApp message and show the reply

See RUNBOOK.md for walking Loop A -> B -> C.
EOF
  ;;
esac
