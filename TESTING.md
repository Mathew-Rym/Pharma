# Pharma OS — setup and testing, start to finish

Part 1 is setup (once). Part 2 is testing the three loops.

Each step says **what you should see**, so you can tell "working" from "looks like it
worked". If a step doesn't match, stop there — the next step depends on it.

---

# PART 1 — SETUP

## 0. What you need

| | Check with | If missing |
|---|---|---|
| Python 3.11+ | `python3 --version` | `sudo apt install python3 python3-venv` |
| Docker | `docker ps` | [docs.docker.com/engine/install](https://docs.docker.com/engine/install/) — Compose is *not* required |
| A Supabase project | — | [supabase.com](https://supabase.com), free tier is fine |
| A Gemini or Anthropic API key | — | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| A spare SIM for WhatsApp | — | Any number **not** your personal one — see §5 |

## 1. Get the code and a virtualenv

```bash
cd dishii-repo
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r api/requirements.txt -r dashboard/requirements.txt
.venv/bin/python -m pip install pytest pypdf pillow
```

**Expect:** no errors. This takes a few minutes.

## 2. Fill in `.env`

```bash
cp .env.example .env
```

Then edit it. These five are required — the API refuses to start without them:

| Variable | Where to get it |
|---|---|
| `DATABASE_URL` | Supabase → Project Settings → Database → Connection string → **URI**. Prefer the session pooler (port 5432). |
| `SUPABASE_URL` | Supabase → Project Settings → API → Project URL |
| `SUPABASE_SERVICE_KEY` | same page → **service_role** key. Never the anon key. |
| `SHARED_SECRET` | `openssl rand -hex 32` |
| `GEMINI_API_KEY` | aistudio.google.com/apikey (or set `ANTHROPIC_API_KEY` instead) |

And these:

```bash
DASHBOARD_PASSWORD=$(openssl rand -hex 8)       # the dashboard refuses to start unset
GOWA_PASS=$(openssl rand -hex 16)
GOWA_WEBHOOK_SECRET=$(openssl rand -hex 32)
```

Leave `PHARMACY_ID=` **empty** for now. You don't have a pharmacy yet — step 6 creates
it and prints the id to paste back.

> `.env` is gitignored. Never commit it.

## 3. Create the database

```bash
./run.sh migrate
```

Applies `db/schema.sql` then `schema_v2 … v6` in order. Each is additive and safe to
re-run.

**Expect:** `database is current: 25 tables, 8 views, all columns present`

`schema.sql` may report "skipped … already exists" on a database that has been set up
before. That is correct, not a failure.

Verify any time with:

```bash
./run.sh check
```

## 4. Start the API and dashboard

```bash
./run.sh all
```

**Expect:**
```
  API        http://localhost:8000/docs
  Dashboard  http://localhost:8501
```

Storage buckets (`invoices`, `prescriptions`, `docs`) are created automatically on
first API start.

## 5. Pair WhatsApp

```bash
./run.sh whatsapp     # starts GOWA; pulls the image the first time
./run.sh qr           # prints a QR in the terminal AND saves .run/whatsapp-qr.png
```

Scan it: **WhatsApp → Settings → Linked devices → Link a device**.

**The QR expires in about 30 seconds.** Have the phone on that screen *before* you run
`./run.sh qr`. If it expires, just run it again.

**Expect:** `PAIRED.` then:

```bash
./run.sh brand        # pushes the logo and display name onto the account
```

> **Use a dedicated SIM.** Not your personal number, not the pharmacy's main line.
> This drives a WhatsApp Web session and Meta bans numbers for it — the ban hits the
> number, not the code.

`http://localhost:3001` will look blank. That is expected: GOWA v9 downloads its web
dashboard from GitHub at startup and that request is often blocked. The REST API is
fine, which is why pairing goes through the terminal.

## 6. Create your pharmacy

Open **http://localhost:8501** and sign in with `DASHBOARD_PASSWORD`.

On a fresh database you land on **Set up your pharmacy**. Fill in:
- Pharmacy name, M-Pesa Paybill, **PPB licence** (prints on the PO a distributor gets)
- The pharmacy's WhatsApp line (the SIM from §5)
- **Owner's name and WhatsApp number — use your own real number**

**Expect:** a green confirmation and `PHARMACY_ID=<uuid>`.

**Copy that uuid into `.env`** as `PHARMACY_ID=`, then restart:

```bash
./run.sh stop && ./run.sh all
```

## 7. Add yourself properly — do not skip

**Setup → Who the system talks to → Add someone**, if you are not already there from
step 6. Then **Set PIN → `4417` → Save PIN**.

**Expect:** your row shows `🔑 PIN set`.

> **This is the single most common reason "nothing works".** WhatsApp answers numbers
> in `staff` and ignores every other number *silently* — no error, no log, nothing
> looks broken. Skip this and every later step fails quietly.

## 8. Confirm the install

```bash
./run.sh check                 # database
./run.sh say "HELP"            # message path, no phone needed
./run.sh test                  # 157 tests, ~10 min
```

**Expect:** `bot <- ` with the command list, and `157 passed`.

## 9. Optional — sign in as yourself instead of a shared password

```bash
# .env
AUTH_MODE=whatsapp
```

Restart the dashboard. The sign-in page now offers **Send me a code on WhatsApp**;
the shared password still works alongside it.

**Expect:** after entering the 6-digit code, the sidebar shows *your* name and role
with a Sign out button, and no "Signed in as" dropdown.

Needs §5 done first. See [AUTH.md](AUTH.md) for the rollout path.

---

# PART 2 — TESTING THE LOOPS

## Stage 1 — the message path (2 min)

```bash
./run.sh say "HELP"
./run.sh say "PC"        # -> "No agent is installed on the pharmacy PC yet..."
./run.sh say "VARIANCE"  # -> "No stock variances open."
./run.sh say "LOW"       # -> "Nothing is below its reorder level."
./run.sh say "ORDER"     # -> "Nothing needs reordering right now."
```

**Expect:** a sensible sentence each time. Empty answers are *correct* — there is no
stock yet.

**If you see** `no reply in 45s` → §7.

Then message `HELP` from your own phone to the pharmacy SIM. Same list, on your phone.

## Stage 2 — LOOP A: receiving ⭐ (15 min)

Use a real supplier invoice. Any printed invoice works for a first pass.

### 2.1 Send it
Photograph the invoice → send to the pharmacy line.
**Expect:** `Page 1 received. Send more pages, or reply DONE to process.`

### 2.2 Extract
Reply **`DONE`**. **Expect** (~30s):
```
Read 18 line(s) from invoice APL12000627.

📦 Now photograph the goods.
Lay the packs out so they are all visible...
Reply SKIP to receive on the invoice quantities without counting.
```

### 2.3 Count
Photograph the boxes laid out. Send more than one photo if they don't fit.
**Expect:** `Goods photo 1 received. Send more, or reply COUNT to count them.`

Reply **`COUNT`**. **Expect** some mix of:
```
✅ 16 line(s) match the invoice.
⚠️ Counts that do not match: 5. PRENOR — invoice 10W0P, I count 9W0P
🔍 Could not count confidently: 7. AMOXIL — back of the pile is not visible
```

### 2.4 Correct
Reply **`5:9W`** — line 5, you counted 9 packs. (`5:2W5P` = 2 packs + 5 loose.)
**Expect:** `Line 5 counted as 9W. More corrections, or reply OK.`

### 2.5 Receive
Reply **`OK`**.
**Expect:** `✅ Received 18 line(s) into stock.` plus any discrepancies.

**Verify:** dashboard → Stock shows the products; Receiving shows the GRN approved.

### 2.6 Three things worth testing deliberately

| Try | Should happen | Why it matters |
|---|---|---|
| **`SKIP`** at 2.3 | Straight to review on invoice quantities | A 40-line delivery at closing time must never be blocked |
| **`OK`** at 2.5 *without* fixing a flagged count | Receives, and says *"count never confirmed"* | The disagreement stays claimable instead of vanishing |
| Photo with boxes **cut off at the edge** | Asks for one more photo | A false shortage teaches staff to ignore every warning |

For the middle one, check `v_open_receiving_discrepancies` in Supabase.

### 2.7 No-WhatsApp fallback
**Dashboard → Manual upload → Supplier invoice.** Same pipeline, not a second one.

## Stage 3 — LOOP B: the till and the variance (10 min)

### 3.1 Feed it sales
**Dashboard → Manual upload → phAMACore export.** Shape detected from headers:

```csv
Sale Date,SaleID,ItemCode,ItemName,Qty,UnitPrice,Total
26/07/2026 10:14,INV001,PRN255,PRENOR 25/5MG TABS 30S,2W0P,450,900
```
```csv
StockPeriod,ItemCode,ItemName,Qty,Value
2026-06,PRN255,PRENOR 25/5MG TABS 30S,290,130500
```
```csv
ItemCode,Description,Qty
PRN255,PRENOR 25/5MG TABS 30S,3W0P
```

**Expect:** `1 sales row(s) landed, 1 applied to the ledger.`

**Verify the important bit:** `2W0P` is 2 **packs**. At pack size 30 the stock must
drop by **60, not 2**. Check the Stock page.

### 3.2 With the pharmacy PC
Run this **first**, before any DB config:
```bash
python agent/agent.py --probe-only
```
Prints open DB ports, services, ODBC DSNs, every `.fdb/.mdb/.mdf`, and flags which
file changed in the last two hours — that one is the live database. Paste it back.

Then fill `agent/config.ini` (from `config.ini.example`) with `api_url`,
`pharmacy_id`, and an enrolment token from **Dashboard → System**, and run
`python agent/agent.py --config config.ini`.

From WhatsApp: `PC` (is it online), `SYNC` (pull now).

### 3.3 The variance
Upload a stock snapshot whose numbers differ from your stock, then:

```
VARIANCE
```
**Expect:**
```
⚖️ Stock variance — 1 item(s), KES 450 unexplained
• PRENOR 25/5MG TABS 30S — till says 60, we calculate 90 (-30 pcs, KES 450)
```

**This is the number that sells the product.** Nobody at that pharmacy can see it today.

## Stage 4 — LOOP C: forecast → order → distributor (10 min)

### 4.1 Give it history
Upload a monthly-totals CSV (3.1, second shape) with 6–24 months. Without history
there is no forecast, and the system correctly says *"no demand signal yet"* rather
than forecasting zero.

### 4.2 Forecast
```
ORDER
WHY prenor
```
**Expect:** suggestions grouped by supplier, each line with its reason; `WHY` gives
on-hand, rate/day, seasonal multiplier, cover, and the basis in plain English.

**"Nothing needs reordering" is usually correct** — it only suggests items below
`supplier lead time + 10 days` of cover. Upload more sales to force one.

### 4.3 Order it
```
PO
OKPO 4417
```
**Expect:** `✅ Sent to MEDTRACK on 254711000111.`

The supplier gets a WhatsApp text **and a PDF on your letterhead**. Open the PDF:

- ✅ pharmacy name, PPB licence, callback number
- ✅ order reference, addressed to the rep
- ✅ packs, pieces, unit cost, line total
- ✅ *"BATCH NUMBER and EXPIRY DATE must appear against every line"*
- ❌ **no** internal rationale, **no** sell prices — your negotiating position

Try `OKPO 9999` → `Wrong PIN. 3 attempt(s) left.` Four failures locks for 15 min.

### 4.4 Close the loop
Delivery arrives → photograph the invoice → **back to Stage 2**.

## Stage 5 — reports (5 min)

```
EXPIRY · LOW · TODAY · REPORT · report for july · who supplies prenor
```

Check `REPORT` specifically — it was broken until recently (a typographic dash crashed
the PDF renderer) and is exactly the kind of thing that only fails when you demo it.

## Stage 6 — confirm nothing is lying to you

```bash
./run.sh test
```

**Expect:** `157 passed`.

The one that matters most is `test_ledger_matches_batch_quantities`: every batch's
quantity must equal the sum of its movements. If that ever fails, stop and fix it
first — it means the system is misreporting stock to a pharmacist.

---

# Reference

```bash
./run.sh all | api | dashboard | stop
./run.sh whatsapp | qr | brand | unpair
./run.sh check | migrate | test
./run.sh say "ORDER"
tail -f .run/api.log
docker logs -f pharmaos-gowa
```

### WhatsApp commands

| | |
|---|---|
| `HELP` | command list |
| `DONE` `COUNT` `SKIP` `OK` `CANCEL` | receiving flow |
| `5:2W` · `5:2W5P` | correct a count — line 5, 2 packs (+5 loose) |
| `7 EXP 06/2028` · `7 BATCH ST26-0439` · `7 NEW` | fix a line |
| `EXPIRY` `LOW` `TODAY` `REPORT` | reports |
| `ORDER` `PO` `OKPO <pin>` `WHY <product>` | reordering |
| `VARIANCE` `SYNC` `PC` `PROBE` | pharmacy PC |
| `APPROVE <pin>` · `REJECT <reason>` | prescriptions |

### When it doesn't work

| Symptom | Cause |
|---|---|
| No WhatsApp reply at all | Number not in `staff`, or inactive → Setup |
| "Refusing to start without a gate" | `DASHBOARD_PASSWORD` unset |
| Dashboard `KeyError: DATABASE_URL` | `.env` not filled in, or you started it without `run.sh` |
| localhost:3001 blank | Normal — GOWA's web UI is not bundled. Use `./run.sh qr`. |
| `unknown shorthand flag: 'f'` | Docker Compose not installed. `run.sh` handles it; don't call compose directly. |
| Messages arrive, nothing happens | API unreachable from the container → `GOWA_WEBHOOK_URL` |
| `401` on `/webhook/gowa` | `GOWA_WEBHOOK_SECRET` ≠ the container's `WHATSAPP_WEBHOOK_SECRET` |
| `PO` creates nothing | Nothing below reorder point, or the product has no supplier — a supplier is learned the first time you *receive* that product from them |
| Invoice not read | Check `GEMINI_API_KEY`; failures reply with the error type |
| Stock moved by the wrong amount | Check pack size — `2W0P` at pack 30 is 60 pieces |
| `server closed the connection unexpectedly` | Should be fixed (pool health checks). If it recurs, restart the API and tell me. |

### What is deliberately not done yet

**RLS.** Tenant isolation is application-level (`where pharmacy_id = %s` on every
query). One deployment is safe for pharmacies that all belong to you; not yet for a
second paying customer. See [AUTH.md](AUTH.md).
