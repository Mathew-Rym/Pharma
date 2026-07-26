# Pharma OS — Pharmacy OS

WhatsApp-first operations layer for Kenyan retail pharmacies. Sits **on top of** an
existing POS (phAMACore), it does not replace it.

Four flows:
- **A. Goods receiving** — photograph a supplier invoice → every line received with batch
  number and expiry date, no typing
- **B. Ask anything** — owner texts "what's expiring in 90 days" or "report for july" and
  gets an answer or a PDF, from anywhere
- **C. Prescription → delivery** — customer sends a photo → pharmacist verifies → M-Pesa
  STK → receipt with a traceability QR
- **D. Automation** — daily expiry sweep, draft purchase orders, digests, reconciliation

```
WhatsApp ──▶ wa-gateway (Node/Baileys, zero logic) ──▶ api (FastAPI, all logic)
                                                          ├─▶ Supabase (Postgres + Storage)
                                                          ├─▶ Claude (vision + tool routing)
                                                          └─▶ Daraja (M-Pesa)
                                    Streamlit dashboard ──┘
                              GitHub Actions cron ──▶ POST /jobs/{name}
```

---

## Repository layout — what goes where

```
pharmaos/
├── db/schema.sql                 ← run this in Supabase FIRST
├── api/                          ← ALL business logic lives here
│   ├── config.py                 env vars, business thresholds
│   ├── db.py                     connection pool, apply_movement(), storage
│   ├── utils.py                  phone / W-P units / expiry parsing  ← pure, tested
│   ├── llm.py                    Claude calls + the extraction prompts
│   ├── state.py                  conversation state (wa_state table)
│   ├── router.py                 staff vs customer, keyword shortcuts, tool loop
│   ├── grn.py                    FLOW A  goods receiving
│   ├── rx.py                     FLOW C  prescriptions, payment, fulfilment
│   ├── reports.py                FLOW B  SQL tools + report/receipt PDFs
│   ├── jobs.py                   FLOW D  cron jobs
│   ├── mpesa.py                  Daraja STK push + idempotent callback
│   ├── pdfgen.py                 fpdf2 + matplotlib + QR
│   └── main.py                   FastAPI routes
├── wa-gateway/index.js           ← Baileys. NO business logic, ever.
├── dashboard/app.py              ← Streamlit: verification queue, GRN review, expiry
├── seed/import_phamacore.py      ← CSV/XLSX export → products + opening batches
├── tests/test_invariants.py      ← pure tests + the ledger invariant
└── .github/workflows/cron.yml    ← schedules
```

**Where to add things later**
| You want to… | Edit |
|---|---|
| add a new WhatsApp command | `router.py` (shortcut) or `reports.py` `TOOLS` (model-picked) |
| change what the AI reads off an invoice | `llm.py` → `INVOICE_SYSTEM` |
| add a report section | `reports.py` → `build_report_pdf` |
| add a scheduled job | `jobs.py` → new function + `JOBS` dict + `cron.yml` |
| change reorder maths | `jobs.py` → `_suggest_qty` |
| swap Baileys for Meta Cloud API | `wa-gateway/index.js` + `api/wa.py`. Nothing else. |

**Never** put an `if (text === 'OK')` in `wa-gateway/index.js`. That single decision is
what keeps the Meta Cloud API migration a two-file change instead of a rewrite.

---

# STEP-BY-STEP SETUP

Total time if nothing fights you: about 90 minutes to a working WhatsApp bot.

## Step 0 — Before you touch the laptop (do this now)

1. **Text Vivian** and ask for the phAMACore product/stock export as CSV or Excel.
   phAMACore has an `export` button on the Purchase Order screen. **Every other step
   depends on this file.** Also ask permission to photograph ~20 past supplier invoices —
   that is your test set.
2. **Register for M-Pesa Daraja sandbox** at `developer.safaricom.co.ke`. Create an app,
   note the Consumer Key and Secret. Approval can be slow; start it now.
3. **Buy a dedicated SIM** for the WhatsApp number. Never use the pharmacy's main
   business line — Baileys is against Meta's ToS and the number can be banned. Send a few
   normal messages from it today so it isn't brand new on demo day.

## Step 1 — Create the repo (5 min)

```bash
mkdir pharmaos && cd pharmaos
git init
# copy the contents of this repository in, then:
cp .env.example .env
git add -A && git commit -m "scaffold"
gh repo create pharmaos --private --source=. --push
```

`.env` is gitignored. Confirm with `git status` that it is **not** staged before pushing.

## Step 2 — Supabase project + schema (10 min)

1. `supabase.com` → New project. Region: **eu-central-1** or **eu-west-2** (lowest
   latency to Nairobi of the common options). Save the database password.
2. **SQL Editor** → paste all of `db/schema.sql` → Run. It creates `pg_trgm` and
   `pgcrypto`, 18 tables, 3 views, and enables RLS.
3. Verify: `select count(*) from information_schema.tables where table_schema='public';`
   → should be 18 or more.
4. **Project Settings → API** → copy the Project URL and the **`service_role`** key.
   That key bypasses RLS. It belongs on your server only — never in the Streamlit
   frontend of a public app, never in git, never in a browser.
5. **Project Settings → Database → Connection string → URI** → copy it. Replace
   `[YOUR-PASSWORD]` with the real password. This is `DATABASE_URL`.

Paste all four values into `.env`.

## Step 3 — Create the pharmacy and staff rows (5 min)

Supabase SQL Editor. **Use real phone numbers in `254...` format** — the phone number is
the identity in this system, there are no passwords for staff.

```sql
insert into pharmacies (name, mpesa_paybill, timezone)
values ('New Lemuma Pharmacy Co. Ltd', '4166919', 'Africa/Nairobi')
returning id;
-- copy the returned uuid into PHARMACY_ID in .env
```

```sql
insert into staff (pharmacy_id, phone, name, role, ppb_reg_no) values
  ('<PHARMACY_ID>', '254700000001', 'You (dev)',   'owner',      null),
  ('<PHARMACY_ID>', '254713755274', 'Vivian',      'attendant',  null),
  ('<PHARMACY_ID>', '254712345678', 'Pharmacist',  'pharmacist', 'PPB-11908');
```

Also seed the supplier contacts trapped on Vivian's phone — this is the fastest
demo win in the whole product:

```sql
insert into suppliers (pharmacy_id, code, name, phone, rep_name, mpesa_paybill) values
  ('<PHARMACY_ID>', 'SUP567', 'MEDTRACK ETHICALS LTD', '254790279735', 'Vivian', '4166919'),
  ('<PHARMACY_ID>', null,     'NORTHERN PHARMACY LIMITED', '254716217217', 'Lilian', null);
```

## Step 4 — Seed the catalogue from phAMACore (10 min)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r api/requirements.txt openpyxl
set -a && source .env && set +a          # load env into the shell

python seed/import_phamacore.py ~/Downloads/stock_export.csv --dry-run
```

Read the dry-run output carefully. It prints the headers it detected and five sample
rows. If `pack_size` or quantities look wrong, add your column name to `HEADER_MAP` in
`seed/import_phamacore.py` and re-run. Then:

```bash
python seed/import_phamacore.py ~/Downloads/stock_export.csv
```

Opening batches are created with **`expiry_date = NULL` on purpose**. The legacy export
has no trustworthy expiry data and inventing dates would poison the expiry engine
permanently. Real expiries arrive with the first invoice you photograph.

No export yet? Insert 20 products by hand and keep building — do not let this block you.

## Step 5 — Run the API locally (10 min)

```bash
cd api
uvicorn main:app --reload --port 8000
```

```bash
curl localhost:8000/health
# {"ok":true,"products":487,"time":"..."}
```

Now test the whole brain **without WhatsApp at all** — this is the single most useful
habit for the next four days:

```bash
S=$(grep ^SHARED_SECRET .env | cut -d= -f2)

curl -X POST localhost:8000/dev/simulate -H "x-pharmaos-secret: $S" \
  -H 'content-type: application/json' \
  -d '{"from":"254700000001","text":"EXPIRY"}'

curl -X POST localhost:8000/dev/simulate -H "x-pharmaos-secret: $S" \
  -H 'content-type: application/json' \
  -d '{"from":"254700000001","text":"who supplies prenor"}'
```

The reply attempt will fail at the gateway (nothing running on :3000 yet) but the API log
shows the tool that was chosen and the text it tried to send. That is enough to develop
every flow before WhatsApp exists.

## Step 6 — Baileys gateway + pair the number (15 min)

```bash
cd wa-gateway
npm install
AUTH_DIR=./auth API_URL=http://localhost:8000 SHARED_SECRET=<same-secret> node index.js
```

A QR code prints in the terminal. On the **dedicated SIM's** phone: WhatsApp → Settings →
Linked Devices → Link a Device → scan. You should see `whatsapp connected`.

Now message that number from your own phone. Because your number is in `staff` as
`owner`, you get the staff branch:

- send `HELP` → command list
- send `EXPIRY` → expiry list (empty until you receive stock — expected)
- **send a photo of the MedTrack invoice** → "Page 1 received..." → reply `DONE`

That last one is the entire product. If it works, you have a demo.

`./auth/` must persist. Delete it and you re-scan the QR. It is gitignored.

## Step 7 — Streamlit dashboard (5 min)

```bash
cd dashboard
pip install -r requirements.txt
set -a && source ../.env && set +a
streamlit run app.py
```

Open the **Verification queue** and **Receiving** pages. Check **System** last — it
shows ledger integrity, cron history and unhandled messages. If "Ledger integrity" is
ever red, stop building features and fix it.

## Step 8 — Deploy (20 min)

Railway, two services in one project.

**Service 1 — api**
- New Service → GitHub repo → **Root directory: `/api`**
- It will detect the Dockerfile
- Variables: every line from `.env` except `WA_GATEWAY_URL`
- Generate a domain → note it, e.g. `https://pharmaos-api.up.railway.app`
- Set `PUBLIC_BASE_URL` and `MPESA_CALLBACK_URL=<domain>/mpesa/callback`

**Service 2 — wa-gateway**
- New Service → same repo → **Root directory: `/wa-gateway`**
- Variables: `API_URL=<api domain>`, `SHARED_SECRET=<same>`, `AUTH_DIR=/data/auth`
- **Add a Volume mounted at `/data`.** Skip this and you re-scan the QR on every single
  deploy, including the one you do an hour before the demo.
- Generate a domain, open `<gateway-domain>/qr` in a browser, scan once.
- Then go back to the **api** service and set `WA_GATEWAY_URL=<gateway domain>`.

**Dashboard** — Streamlit Community Cloud, point at `dashboard/app.py`, paste the same
env vars into Secrets, set `DASHBOARD_PASSWORD`.

**Cron** — repo Settings → Secrets and variables → Actions → add `API_URL` and
`SHARED_SECRET`. Then Actions → pharmaos-cron → Run workflow → pick `expiry_sweep` to
verify it fires before you trust the schedule.

Pay the $5/month. A free tier that sleeps drops the WhatsApp socket and you will spend
hours debugging ghosts instead of building.

## Step 9 — M-Pesa (15 min)

Sandbox first. Daraja test credentials go in `.env`; the sandbox passkey in
`.env.example` is Safaricom's public test value.

```bash
curl -X POST localhost:8000/dev/simulate -H "x-pharmaos-secret: $S" \
  -H 'content-type: application/json' \
  -d '{"from":"254799999999","text":"YES"}'      # consent as a customer
```

Then send a prescription photo from a non-staff number, approve it in the dashboard as
the pharmacist, reply `CONFIRM` from the customer number. In sandbox use Safaricom's test
MSISDN `254708374149`.

Production needs a real shortcode and Safaricom's Go-Live process, which takes days to
weeks. **Do not put that on the critical path for Friday.** Demo on sandbox and show the
Paybill fallback, which works today with their existing Paybill 4166919.

---

# TESTING

```bash
pytest tests/ -v                    # pure tests always run; DB tests skip without env
set -a && source .env && set +a
pytest tests/ -v                    # now the invariant tests run too
```

The tests that matter:

| Test | Why it exists |
|---|---|
| `test_ledger_matches_batch_quantities` | **The** test. If it fails, you are lying to a pharmacist about their inventory. That ends a pilot on the spot. |
| `test_expiry_is_end_of_month_not_start` | `01/2028` means good through 31 Jan. Using the 1st writes off a month of saleable stock on every batch. |
| `test_phone_formats_all_resolve_to_one_person` | `0713…`, `+254713…`, `254713…` are one customer, not three. |
| `test_no_order_left_the_pharmacist_gate_unverified` | PPB compliance, as a query. Run it before every demo. |
| `test_no_duplicate_mpesa_receipts` | Safaricom retries callbacks. A duplicate double-credits an order. |

## Stress test list — Monday to Thursday

Ask for 20 real invoices off the filing spike and run every one.

**Vision:** glare, shadow across the page, 45° angle, page 2 sent before page 1, pen
annotations over printed lines, the same invoice sent twice, a photo of something that
isn't an invoice, a two-page invoice with page 2 missing.

**WhatsApp:** double-send the same message, send while an extraction is still running,
send `hi` mid-GRN-review, all three Kenyan phone formats, an unknown number, kill the
gateway mid-flow and restart it, send a voice note.

**Payments:** STK timeout, customer cancels, duplicate callback, callback 10 minutes late.

**Concurrency:** two staff photograph the same delivery from two phones. The duplicate
guard in `grn.py` should reply *"already received by Jasmin at 10:23"* — using their own
rubber-stamp language from the invoice.

**After every session:** `pytest tests/ -v`. Ledger drift found late is a nightmare;
found the same day it is a ten-minute fix.

**Rehearse the demo six times on the pharmacy's own WiFi.** Live Baileys demos fail on
bad connectivity and River Road is not your laptop. Record a screen capture as backup
and keep a dashboard-only path that needs no WhatsApp at all.

---

# COST TO RUN A PILOT

| Item | Monthly |
|---|---|
| Railway (api + gateway + volume) | ~$5–10 |
| Supabase free tier | $0 |
| Streamlit Community Cloud | $0 |
| GitHub Actions | $0 |
| Claude — invoice extraction | ~$0.05–0.15 per invoice; 200 invoices ≈ $10–30 |
| Claude — chat routing | a few dollars |
| **Total** | **~$25–50 per pharmacy** |

At KES 10,000/month (~$77) the margin is fine. Watch the vision spend — it is the only
line that scales with usage. `MODEL_VISION` is an env var precisely so you can move
routine invoices to Sonnet once you have measured the accuracy difference on your own
20-invoice test set.

---

# THINGS THAT WILL BITE YOU

1. **Baileys auth volume.** No persistent volume → QR rescan every deploy. Set it up in
   Step 8, not on demo morning.
2. **Supabase pgbouncer.** If you use the transaction pooler (port 6543), prepared
   statements break. `db.py` already sets `prepare_threshold=None`. Don't remove it.
3. **`service_role` key.** Bypasses RLS entirely. Server-side only.
4. **Expiry end-of-month.** Covered by a test. Never "simplify" it.
5. **Never auto-approve a GRN.** No matter how good accuracy gets. The moment a
   pharmacist finds a quantity they didn't approve, the pilot is over.
6. **Never let the model emit SQL.** It picks a tool and arguments; `reports.py` owns
   every query. There is no path from a WhatsApp message to arbitrary SQL.
7. **Pharmacist gate is not optional.** Kenya's PPB requires a licensed pharmacist to
   verify before dispensing. Removing it for a smoother demo is not a trade you can make.
8. **DPA 2019 consent** before storing any customer. `rx.py` asks first; keep it.
9. **Don't fan out messages.** `wa.broadcast` is sequential with a 1.5s gap on purpose.
   Blasting gets the number banned.
10. **GitHub Actions cron drifts** 5–15 minutes. Fine for a morning digest, not for
    anything time-critical.

---

# THE 2-DAY ORDER OF WORK

**Day 1** — Steps 0–6, then Flow A end to end. Photograph the real MedTrack invoice and
watch 18 lines land in stock with batch numbers and expiries. **Tag this in git.** If
Day 2 goes badly you still have a demo worth showing.

**Day 2** — Step 7 (dashboard + verification queue), Step 8 (deploy), Step 9 (sandbox
M-Pesa), then reports.

**Cut in this order if you run out of time:** loyalty redemption → rider dispatch →
demand forecasting → barcode fallback → multi-branch. Show the Calcigard numbers from
their own phAMACore screen (3,697 / 3,099 / 2,463 / 2,840 units Apr–Jul, 769 left ≈ 8
days of cover) on a **slide** instead of building the forecast model.

**If by Sunday night the only working thing is invoice photo → stock received with
batches and expiries, you still have a demo worth KES 10,000** — because that is the
thing costing them money every Thursday.
