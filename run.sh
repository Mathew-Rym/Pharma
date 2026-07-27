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
  docker compose -f wa-gowa/docker-compose.yml up -d
  echo "GOWA starting. Scan the QR at http://localhost:3001"
  echo "Logs: docker compose -f wa-gowa/docker-compose.yml logs -f"
  ;;

all)
  mkdir -p .run
  api_up  || { (cd api && nohup "$PY" -m uvicorn main:app --host 0.0.0.0 --port 8000 \
                 >"$ROOT/.run/api.log" 2>&1 &) ; }
  dash_up || { (cd dashboard && nohup "$PY" -m streamlit run app.py --server.port 8501 \
                 --server.headless true --browser.gatherUsageStats false \
                 >"$ROOT/.run/dashboard.log" 2>&1 &) ; }
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
  docker compose -f wa-gowa/docker-compose.yml down 2>/dev/null || true
  echo "stopped"
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
import os, glob, psycopg
files = ["db/schema.sql"] + sorted(glob.glob("db/schema_v*.sql"))
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
  ./run.sh whatsapp    GOWA + QR         http://localhost:3001
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
