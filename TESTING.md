# Testing Pharma OS — step by step

Follow this in order. Each step builds on the one before, and each says **what you
should see**, so you can tell "working" from "looks like it worked".

Two ways to drive WhatsApp:

- **Simulated** — `./run.sh say "..."`. No phone, no QR, works immediately. Use this
  for everything except photo flows.
- **Real** — pair a SIM with GOWA. Needed for Loop A, because you have to send photos.

Start simulated. Move to real once Stage 3 passes.

---

# Stage 0 — Start everything (2 min)

```bash
cd dishii-repo
./run.sh check        # is the database current?
./run.sh all          # API :8000 + dashboard :8501
```

**Expect:**
```
database is current: 25 tables, 8 views, all columns present
  API        http://localhost:8000/docs
  Dashboard  http://localhost:8501
```

If `check` reports anything missing, run `./run.sh migrate` and check again.

---

# Stage 1 — Dashboard and your first pharmacy (5 min)

### 1.1 Log in
Open **http://localhost:8501**. Password is `DASHBOARD_PASSWORD` from `.env`.

**Expect:** the sidebar with 10 pages.
**If it refuses to start:** `DASHBOARD_PASSWORD` is unset. That is deliberate — this
surface holds prescription images, so an unconfigured deploy fails closed.

### 1.2 Add yourself — do not skip this
**Setup → Who the system talks to → Add someone**

| Field | Value |
|---|---|
| Name | your name |
| WhatsApp number | **0720521291**, e.g. `0713 755 274` |
| Role | `owner` |

Then **Set PIN** → `4417` → Save PIN.

**Expect:** your row appears with `🔑 PIN set`.

> **This is the single most common reason "nothing works".** WhatsApp answers numbers
> in `staff` and ignores everything else *silently* — no error, no log, nothing looks
> broken. If you skip this, every later stage fails quietly.

### 1.2b Try per-user sign-in (optional, new)

Everything above uses one shared password. To sign in **as yourself** instead:

```bash
# .env
AUTH_MODE=whatsapp        # shared password still works alongside it
```

Restart the dashboard, then on the sign-in page enter your WhatsApp number →
**Send me a code on WhatsApp** → type the 6 digits.

**Expect:** the sidebar shows *your* name and role with a Sign out button, and there
is no "Signed in as" dropdown — the acting user is you, because you authenticated.

**Needs WhatsApp paired first** (Stage 3), or the code has nowhere to go. Until then
stay on `AUTH_MODE=shared`.

See [AUTH.md](AUTH.md) for the rollout path and what is still missing (RLS).

### 1.3 Fill in the pharmacy
**Setup → Pharmacy** — set the name, M-Pesa Paybill, and **PPB licence** (the licence
prints on the purchase-order PDF a distributor receives).

---

# Stage 2 — Prove the message path works (2 min)

```bash
./run.sh say "HELP"
```

**Expect:** `bot <- ` followed by the staff command list.
**If you see** `no reply in 45s. Is this number in staff and active?` → Stage 1.2.

Then try the read-only commands. On an empty database these correctly report nothing:

```bash
./run.sh say "PC"        # -> "No agent is installed on the pharmacy PC yet..."
./run.sh say "VARIANCE"  # -> "No stock variances open."
./run.sh say "LOW"       # -> "Nothing is below its reorder level."
./run.sh say "ORDER"     # -> "Nothing needs reordering right now."
```

**Expect:** a sensible sentence each time, not an error. Empty answers are correct —
there is no stock yet.

---

# Stage 3 — Connect real WhatsApp (10 min)

Needed from here on, because Loop A requires sending photos.

```bash
./run.sh whatsapp
docker compose -f wa-gowa/docker-compose.yml logs -f     # watch for the QR
```

Open **http://localhost:3001** and scan the QR.

> Use a **dedicated SIM**. Not your personal number, not the pharmacy's main line.
> This drives a WhatsApp Web session and Meta bans numbers for it — the ban hits the
> number, not the code.

**Verify:** message `HELP` from your own phone to that SIM.
**Expect:** the same command list, now on your actual phone.

**If nothing arrives:**
```bash
docker compose -f wa-gowa/docker-compose.yml logs --tail 30
tail -20 .run/api.log
```
- `401` on `/webhook/gowa` → `GOWA_WEBHOOK_SECRET` in `.env` and
  `WHATSAPP_WEBHOOK_SECRET` on the container disagree.
- No webhook attempt at all → the container cannot reach the API. Check
  `GOWA_WEBHOOK_URL`.

---

# Stage 4 — LOOP A: receiving (15 min) ⭐ the wedge

Use a real supplier invoice if you have one. Otherwise any printed invoice works.

### 4.1 Send the invoice
Photograph it and send to the pharmacy line.

**Expect:** `Page 1 received. Send more pages, or reply DONE to process.`

### 4.2 Process
Reply **`DONE`**.

**Expect** (~30s):
```
Read 18 line(s) from invoice APL12000627.

📦 Now photograph the goods.
Lay the packs out so they are all visible — flat on the counter beats a stack,
because I can only count what I can see. Send more than one photo if it does not
fit in the frame.

Reply SKIP to receive on the invoice quantities without counting.
```

### 4.3 Count the goods
Lay the boxes out and photograph them. Send more than one photo if needed.

**Expect:** `Goods photo 1 received. Send more, or reply COUNT to count them.`

Reply **`COUNT`**.

**Expect** one of three outcomes, all correct:
```
✅ 16 line(s) match the invoice.

⚠️ Counts that do not match the invoice:
5. PRENOR 25/5MG TABS — invoice 10W0P, I count 9W0P

🔍 Could not count confidently (check these by hand):
7. AMOXIL 500MG — back of the pile is not visible
```

### 4.4 Correct a count
Reply **`5:9W`** — line 5, you physically counted 9 packs.
(`5:2W5P` = 2 packs and 5 loose pieces.)

**Expect:** `Line 5 counted as 9W. More corrections, or reply OK.`

### 4.5 Receive
Reply **`OK`**.

**Expect:**
```
✅ Received 18 line(s) into stock. Approved by <your name>.
📋 Discrepancies recorded (claim within 48 hours): …
```

**Verify in the dashboard:** Stock page shows the products; Receiving page shows the
GRN as approved.

### 4.6 The three things worth testing deliberately

| Try this | Should happen | Why it matters |
|---|---|---|
| Reply **`SKIP`** at 4.3 | Goes straight to review on invoice quantities | A 40-line delivery at closing time must never be blocked |
| Reply **`OK`** at 4.5 *without* fixing a flagged count | Still receives, and the message says *"Received on invoice quantities, count never confirmed"* | The disagreement stays claimable instead of vanishing |
| Photograph with boxes **cut off at the edge** | Asks for one more photo instead of declaring a shortage | A false shortage teaches staff to ignore every warning |

For the second one, check **`v_open_receiving_discrepancies`** in Supabase — the
unanswered count is recorded there.

### 4.7 Manual fallback (no WhatsApp)
**Dashboard → Manual upload → Supplier invoice.** Upload the same pages.
**Expect:** identical extraction — it is the same pipeline, not a second one.

---

# Stage 5 — LOOP B: the till, and the variance (10 min)

Loop B needs sales data. Two ways in.

### 5.1 Without the pharmacy PC — manual upload
**Dashboard → Manual upload → phAMACore export.** Upload a CSV. Three shapes are
detected automatically from the headers:

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

**Expect:** `Detected sale · 1 rows` then `1 sales row(s) landed, 1 applied to the ledger.`

**Verify the important bit:** `2W0P` is 2 *packs*. With a pack size of 30 the stock
must drop by **60**, not 2. Check the Stock page.

### 5.2 With the pharmacy PC
Run this **first**, before writing any DB config:
```bash
python agent/agent.py --probe-only
```
It prints open DB ports, Windows services, ODBC DSNs, every `.fdb/.mdb/.mdf` on disk,
and flags which file changed in the last two hours — that one is the live database.
Paste the output back and the `[database]` section can be filled in.

Then copy `agent/config.ini.example` → `config.ini`, fill in `api_url`,
`pharmacy_id`, and an enrolment token from **Dashboard → System → Generate enrolment
token**, and run:
```bash
python agent/agent.py --config config.ini
```

From WhatsApp:
```
PC        → is the pharmacy PC online
SYNC      → pull fresh data now
```

### 5.3 The variance — the number owners care about
Upload a **stock snapshot** whose quantities differ from your stock, then:

```
VARIANCE
```

**Expect:**
```
⚖️ Stock variance — 1 item(s), KES 450 unexplained

• PRENOR 25/5MG TABS 30S — till says 60, we calculate 90 (-30 pcs, KES 450)

A negative figure means fewer on the shelf than received-minus-sold accounts for:
miscount at receiving, unrecorded sale, or shrinkage.
```

**This is the moment that sells the product.** Nobody at that pharmacy can currently
see this number.

---

# Stage 6 — LOOP C: forecast → order → distributor (10 min)

### 6.1 Give it demand signal
Upload a **monthly totals** CSV (Stage 5.1, second shape) with 6–24 months of history.
Without history there is no forecast, and that is correct — the system says
*"no demand signal yet"* rather than forecasting zero.

### 6.2 Forecast
```
ORDER
```

**Expect** — only for items whose cover is below `supplier lead time + 10 days`:
```
📋 Reorder suggestions

*MEDTRACK DISTRIBUTORS*
• PRENOR 25/5MG TABS 30S — order 7W0P
  11 days cover left · phAMACore history (6 months)

Estimated total: KES 3,150
Reply PO to create draft orders, or PO <supplier> for just one.
```

**If it says "Nothing needs reordering"** that is usually correct — you have plenty of
cover. To force a suggestion, upload a sales CSV that consumes most of the stock.

```
WHY prenor
```
**Expect:** on-hand, rate per day, seasonal multiplier, 30-day forecast, days of
cover, and the basis in plain English. Every number is interrogable — that is the
point of not using a black-box model.

### 6.3 Raise and approve the order
```
PO
```
**Expect:** a purchase order per supplier, each line carrying its own rationale, ending
`Reply OKPO <your PIN> to send this to MEDTRACK on WhatsApp.`

```
OKPO 4417
```
**Expect:**
```
✅ Sent to MEDTRACK DISTRIBUTORS on 254711000111. Recorded against <your name>.
```

The supplier receives **two things**: a readable WhatsApp text, and a **PDF purchase
order on your letterhead**.

**Open that PDF and check:**
- ✅ Pharmacy name, PPB licence, callback number
- ✅ Order reference, addressed to the rep by name
- ✅ Per line: packs, pieces, unit cost, line total
- ✅ *"BATCH NUMBER and EXPIRY DATE must appear against every line"* — Loop A cannot
  recover these later
- ✅ Authorised by you, with timestamp
- ❌ **No** internal rationale, **no** sell prices — that is your negotiating position

### 6.4 Test the PIN
Try `OKPO 9999`.
**Expect:** `Wrong PIN. 3 attempt(s) left.` Four failures locks for 15 minutes.

### 6.5 Close the loop
When the delivery arrives, photograph the invoice → **back to Stage 4**. That is the
full cycle: receive → sell → reconcile → forecast → order → receive.

---

# Stage 7 — Reports and everything else (5 min)

```
EXPIRY              → what is expiring, valued
LOW                 → below reorder level
TODAY               → today's sales
REPORT              → monthly PDF with charts
report for july     → same, in plain language
who supplies prenor → supplier contact
```

**`REPORT` is worth checking specifically** — it was broken until recently (a
typographic dash crashed the PDF renderer) and is exactly the kind of thing that only
fails when you demo it.

---

# Stage 8 — Confirm nothing is lying to you (2 min)

```bash
./run.sh test
```

**Expect:** `144 passed`.

The one that matters most is `test_ledger_matches_batch_quantities`: every batch's
quantity must equal the sum of its movements. If that ever fails, stop and fix it
before anything else — it means the system is misreporting stock to a pharmacist,
which is the one failure that ends a pilot.

---

# Quick reference

```bash
./run.sh all | api | dashboard | whatsapp | stop
./run.sh check                 # DB matches code?
./run.sh migrate               # apply missing migrations
./run.sh test                  # 144 tests
./run.sh say "ORDER"           # fake an inbound message
tail -f .run/api.log
docker compose -f wa-gowa/docker-compose.yml logs -f
```

### WhatsApp commands

| Staff | |
|---|---|
| `HELP` | command list |
| `DONE` / `COUNT` / `SKIP` / `OK` / `CANCEL` | receiving flow |
| `5:2W` · `5:2W5P` | correct a count — line 5, 2 packs (and 5 loose) |
| `7 EXP 06/2028` · `7 BATCH ST26-0439` · `7 NEW` | fix a line |
| `EXPIRY` `LOW` `TODAY` `REPORT` | reports |
| `ORDER` `PO` `OKPO <pin>` `WHY <product>` | reordering |
| `VARIANCE` `SYNC` `PC` `PROBE` | pharmacy PC |
| `APPROVE <pin>` · `REJECT <reason>` | prescriptions |

### When it does not work

| Symptom | Cause |
|---|---|
| No reply at all | Number not in `staff`, or inactive → Setup |
| "Refusing to start without a gate" | `DASHBOARD_PASSWORD` unset |
| Messages arrive, nothing happens | API unreachable from container → `GOWA_WEBHOOK_URL` |
| `401` on `/webhook/gowa` | Webhook secrets disagree |
| `PO` creates nothing | Nothing below reorder point, or the product has no supplier — a supplier is learned the first time you receive that product from them |
| Invoice not read | Check `GEMINI_API_KEY`; failures reply with the error type |
| Stock moved by the wrong amount | Check pack size — `2W0P` at pack 30 is 60 pieces |
