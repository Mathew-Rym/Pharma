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

DB-backed tests need `.env` loaded; `run.sh` does that for you.

**The suite creates its own pharmacy.** `tests/conftest.py` inserts a `PYTEST-<mark>`
pharmacy at import time, exports its id as `PHARMACY_ID`, and deletes it (and everything
hanging off it, in FK order) at the end of the session. It used to bind
`settings.PHARMACY_ID` — a real pharmacy — so every run wrote into live data and cleanup was
manual. An explicit `PHARMACY_ID` in the environment still wins if you want to aim a run at
a specific pharmacy.

Five tests skip without a real Supabase project or a running GOWA, marked `needs_supabase`
and `needs_gowa`. They are skipped explicitly rather than left failing, because a suite that
always shows one red trains everyone to ignore red.

Against a throwaway local Postgres (`pharma-test-pg` on `127.0.0.1:55432`) the whole suite
runs in about 6 seconds with `PHARMACY_ID` unset:

```bash
env -u PHARMACY_ID DATABASE_URL=postgresql://test:test@127.0.0.1:55432/test \
  PHARMAOS_TESTING=1 GEMINI_API_KEY=test-only SHARED_SECRET=test-secret \
  SUPABASE_URL=https://test.supabase.co SUPABASE_SERVICE_KEY=test.test.test \
  .venv/bin/python -m pytest tests/ -q
```

---

## Backing up the WhatsApp sessions

Sessions live in the `gowa-storage` volume at `/app/storages/whatsapp.db` (plus `-wal` and
`-shm`). **`docker stop` / `docker start` cannot lose them** — the files never move. Only
deleting the volume can. A container recreate keeps them too: on 2 August GOWA logged
`auto-connected device pharmacy-1` after a full `docker rm` + `docker run`, which is the
session loading from disk.

Read-only, and it does not require GOWA to be running:

```bash
mkdir -p ~/gowa-backups
docker run --rm -v gowa-storage:/v:ro -v ~/gowa-backups:/out alpine \
  tar czf /out/gowa-sessions-$(date +%F-%H%M).tgz \
  -C /v whatsapp.db whatsapp.db-wal whatsapp.db-shm
```

Restore by stopping GOWA, extracting back into the volume, and starting it.

Three things to know before trusting a backup:

- **Check it is not empty.** A snapshot taken after a logout contains a `whatsmeow_device`
  table with zero rows and restores nothing. Both snapshots currently in `~/gowa-backups`
  are empty for exactly that reason — they were taken after WhatsApp invalidated the
  session. **Re-run the backup after you next pair.** To check:

  ```bash
  docker run --rm -v gowa-storage:/v:ro alpine sh -c \
    'apk add -q sqlite; cp /v/whatsapp.db /tmp/w.db; \
     sqlite3 /tmp/w.db "select count(*) from whatsmeow_device;"'
  ```

- **The volume is on ONE host.** A host failure loses every pharmacy's session at once, and
  each one needs a physical handset to re-pair. That is the single largest operational risk
  in the deployment, and copying the tarball off this machine is the whole mitigation.

- **Portability across GOWA versions is UNVERIFIED.** It is a SQLite schema owned by
  whatsmeow; a major version bump could change it. Nobody has tested a restore into a
  different version.

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
