# Pharma OS — how to run and test everything

Four processes. Start them in this order; each one checks the thing before it.

| # | Service | Port | Start with |
|---|---------|------|-----------|
| 1 | Supabase (Postgres + storage) | — | already live, nothing to start |
| 2 | API (FastAPI) | 8000 | `./run.sh api` |
| 3 | Dashboard (Streamlit) | 8501 | `./run.sh dashboard` |
| 4 | GOWA (WhatsApp) | 3001 | `./run.sh whatsapp` |

`./run.sh all` starts 2 and 3 in the background and tells you what to do next.

---

## 0. One-time: is the database current?

```bash
./run.sh check
```

Confirms every table, view and column the code expects actually exists. Run it after
`git pull`. If it reports anything missing, apply the migrations in order — they are
additive and each is safe to re-run:

```bash
./run.sh migrate          # applies db/schema.sql .. schema_v4.sql as needed
```

---

## 1. Dashboard

```bash
./run.sh dashboard        # http://localhost:8501
```

Log in with `DASHBOARD_PASSWORD` from `.env`. It refuses to start if that is unset —
this surface holds prescription images, so an unconfigured deploy must fail closed
rather than open.

**First thing to do: Setup → add your own WhatsApp number as `owner`, and set a PIN.**
WhatsApp answers numbers in `staff` and ignores every other number silently. If you
skip this, nothing you send will get a reply and nothing will look broken.

Pages: Verification queue · Receiving · Stock · Expiry · Purchase orders · Orders ·
Suppliers · **Manual upload** · **Setup** · System.

---

## 2. API

```bash
./run.sh api              # http://localhost:8000  · /docs for the OpenAPI page
curl localhost:8000/health
```

---

## 3. WhatsApp

```bash
./run.sh whatsapp         # starts GOWA on 3001
docker compose -f wa-gowa/docker-compose.yml logs -f     # watch for the QR
```

Then open **http://localhost:3001** and scan the QR with the pharmacy's WhatsApp SIM.

Use a **dedicated SIM**, not a personal number and not the pharmacy's main line. This
drives a WhatsApp Web session; Meta bans numbers for it, and the ban hits the number,
not the code.

The API must be reachable from the container. Locally that is
`host.docker.internal:8000`, which is already set. Deployed, set `GOWA_WEBHOOK_URL` to
the public API URL.

### Testing WhatsApp without a phone

Every flow below works without pairing anything, using `/dev/simulate`:

```bash
./run.sh say "EXPIRY"                 # as the first owner in staff
./run.sh say "VARIANCE" 254712345678  # as a specific number
```

---

## Walking all three loops

### Loop A — receiving

Photograph a supplier invoice and send it to the pharmacy WhatsApp line.

```
you  → [photo of invoice]
bot  ← Page 1 received. Send more pages, or reply DONE to process.
you  → DONE
bot  ← Read 18 line(s) from invoice APL12000627.
        Now photograph the goods. Lay the packs out so they are all visible…
        Reply SKIP to receive on the invoice quantities without counting.
you  → [photo of the delivery on the counter]
bot  ← Goods photo 1 received. Send more, or reply COUNT to count them.
you  → COUNT
bot  ← 16 line(s) match the invoice.
        Counts that do not match: 5. PRENOR — invoice 10W0P, I count 9W0P
        Correct a count with line:packs — e.g. 5:2W
you  → 5:9W            (confirm you physically counted 9 packs)
you  → OK
bot  ← Received 18 line(s) into stock. Approved by <you>.
        Discrepancies recorded (claim within 48 hours): …
```

Things worth trying deliberately:
- **`SKIP`** — always works, receives on invoice quantities. A 40-line delivery at
  closing time must never be blocked.
- **Reply `OK` without confirming a flagged count** — it still receives, but the
  unanswered disagreement is kept on the GRN and shown in
  `v_open_receiving_discrepancies`, so it can still be claimed from the supplier.
- **A photo with boxes cut off at the edge** — it asks for one more photo rather than
  declaring a shortage.

### Loop B — the till, and the variance

Needs the agent on the pharmacy PC, or use the dashboard fallback:

**Dashboard → Manual upload → phAMACore export.** Drop in a CSV of sales, monthly
totals, or a stock snapshot. The shape is detected from the headers.

On the pharmacy PC:
```bash
python agent/agent.py --probe-only     # run this FIRST, paste the output back
python agent/agent.py --config config.ini
```

Then from WhatsApp:
```
PC          → is the pharmacy PC online
SYNC        → pull fresh data now
VARIANCE    → where the till and our stock disagree   ← the number owners care about
```

### Loop C — forecast → order → distributor

```
ORDER       → reorder suggestions, grouped by supplier, each line with its reason
PO          → creates draft purchase orders
OKPO 4417   → approves with your PIN; sends the order to the supplier's WhatsApp
               AND a purchase-order PDF on your letterhead
WHY prenor  → why the system suggested it
```

A reorder only appears when cover falls below `supplier lead time + 10 days`. With
plenty of stock the correct behaviour is to suggest nothing.

### Everything else

```
HELP · EXPIRY · LOW · TODAY · REPORT · report for july · who supplies prenor
```

---

## Running the tests

```bash
./run.sh test              # full suite, ~4 min (DB tests included)
./run.sh test -k gowa      # a subset
```

DB-backed tests need `.env` loaded; `run.sh` does that for you. They create and delete
their own rows and leave the database as they found it.

---

## When something does not work

| Symptom | Cause |
|---|---|
| WhatsApp never replies | The number is not in `staff`, or is `is_active=false`. Dashboard → Setup. |
| "Refusing to start without a gate" | `DASHBOARD_PASSWORD` unset in `.env`. |
| Messages arrive but nothing happens | API not reachable from the GOWA container — check `GOWA_WEBHOOK_URL`. |
| `401` on `/webhook/gowa` | `GOWA_WEBHOOK_SECRET` in `.env` and `WHATSAPP_WEBHOOK_SECRET` on the container disagree. |
| `PO` creates nothing | Nothing is below its reorder point, or the product has no supplier yet — a supplier is learned the first time you receive that product from them. |
| Invoice photo not read | Check `GEMINI_API_KEY`. Extraction failures reply with the error type. |
