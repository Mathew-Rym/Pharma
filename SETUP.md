# Setup and run — start to finish

Every command runs from `dishii-repo/`. Nothing here assumes a step you have not already
done, and each step says how to tell it worked.

---

## 0. One-time: environment

```bash
cp .env.example .env      # then fill it in
```

You need, at minimum: `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`,
`SHARED_SECRET`, either `GEMINI_API_KEY` or `ANTHROPIC_API_KEY`, `GOWA_USER`, `GOWA_PASS`,
`GOWA_WEBHOOK_SECRET`, `PHARMACY_ID`.

`PHARMACY_ID` is now only a bootstrap hint — tenancy is resolved per message from the
database. It still has to name a **row that exists**, or anything importing `config` fails.
Check:

```bash
set -a && . ./.env && set +a
.venv/bin/python -c "
import sys; sys.path.insert(0,'api')
from config import settings; from db import q1
print('ok' if q1('select 1 from pharmacies where id=%s',(settings.PHARMACY_ID,)) else 'MISSING ROW')"
```

Apply the schema (idempotent, safe to re-run):

```bash
./run.sh migrate      # ends with: database is current: N tables, M views
```

---

## 1. Start WhatsApp (GOWA)

```bash
./run.sh whatsapp     # starts the container, with or without docker compose
```

Confirm it is up:

```bash
curl -s -u "$GOWA_USER:$GOWA_PASS" http://localhost:3001/devices | python3 -m json.tool
```

A slot with `"state": "logged_in"` and a `"jid"` is paired. `"disconnected"` with no jid is
an empty slot.

---

## 2. Pair a number

Two ways. **Which one you want depends on whether you are standing at the machine.**

### 2a. You are at the machine → QR

```bash
./run.sh qr
```

Prints the QR in the terminal *and* writes `.run/whatsapp-qr.png`. Then on the handset:
**WhatsApp → Settings → Linked devices → Link a device** and scan.

It refreshes the code automatically every ~50 seconds for six rounds, because a WhatsApp
QR dies in under a minute — the older single-shot version was dead by the time anyone
walked to the phone. If it says **"Already paired"** there is nothing to do.

### 2b. The pharmacy is somewhere else → 8-character code

```bash
./run.sh pair 254712345678               # slot name is derived
./run.sh pair 254712345678 pharmacy-b    # or name it yourself
```

WhatsApp returns a code like `ABCD-EFGH`. Read it out, SMS it, put it in a WhatsApp
message — it is text, so unlike a QR it survives being relayed. The pharmacy enters it on
**their own** handset:

**WhatsApp → Settings → Linked devices → Link a device → "Link with phone number
instead" → enter the code**

The command then waits and prints `PAIRED` when the session comes up.

> Only the physical handset can pair itself. No message sent *to* your bot can create a
> session for a different number — see [WHATSAPP.md](WHATSAPP.md).

---

## 3. Bind the paired slot to a pharmacy

Pairing gives GOWA a session. Binding tells the app whose it is.

```bash
./run.sh bind pharmacy-b                        # interactive picker
./run.sh bind pharmacy-b "Greenline Pharmacy"   # or name it; created if missing
```

It reads the JID from GOWA rather than letting you type it, because the JID is the tenant
key and a typo binds the wrong handset. Confirm:

```bash
./run.sh safety
```

---

## 4. Give each pharmacy its own identity

```bash
GOWA_DEVICE_ID=pharmacy-b ./run.sh brand
```

Sets the display name and profile photo **for that slot only**. Run it once per pharmacy,
or they all appear identically to customers.

---

## 5. Seed demo data

```bash
.venv/bin/python seed/demo_two_tenants.py
```

Idempotent — re-run it to reset to a known state before each rehearsal. It prints the
isolation moment so you can see it worked:

```
nebivolol     A: Nebilong 5mg KES 780   B: Nebilet 5mg KES 890
atorvastatin  A: Atorvachol KES 950     B: not stocked
```

---

## 6. Run the app

```bash
./run.sh all        # api on :8000, dashboard on :8501
./run.sh stop       # stops both; the WhatsApp pairing survives
```

Verify:

```bash
curl -s http://127.0.0.1:8000/health     # {"ok":true,"products":N,"tenants":N}
```

The health check takes ~2s on a cold pooled connection. A timeout under 5s will look like
a dead server when it is not.

---

## 7. Before you let anyone message it

```bash
./run.sh safety
```

This is the step that decides whether the demo works. It prints:

* which gates are on
* **the allowlist, number by number** — nothing outside it can receive anything
* who can be messaged (they have texted the bot before)
* who is related but **not reachable** because they never texted in

Two things silence a participant, and both look identical to a broken bot:

1. **Not on `WA_ALLOWLIST`.** Add them to `.env`. Takes effect immediately, no restart.
2. **Never messaged the bot.** They must send one message first. The system deliberately
   refuses to message anyone who has not — that is what stops the number being banned.

So for every demo participant: **add their number to the allowlist, and have them text the
bot once.**

---

## 8. Cron jobs

```bash
curl -s -X POST -H "x-pharmaos-secret: $SHARED_SECRET" \
  http://127.0.0.1:8000/jobs/low_stock_check | python3 -m json.tool
```

Each job now runs **once per paired tenant**, each with its own tenant bound, and returns
a per-pharmacy result. An unpaired pharmacy is skipped at selection: it cannot receive the
alert, so running it would only produce failures that look like bugs.

Available: `expiry_sweep`, `forecast_refresh`, `variance_report`, `low_stock_check`,
`daily_digest`, `weekly_report`, `refill_reminders`, `reconcile`.

---

## 9. Tests

```bash
set -a && . ./.env && set +a
.venv/bin/python -m pytest tests/ -q
```

**Budget ten minutes.** Almost every test hits the real Supabase pooler over the network,
so it is slow, not hung — a short `timeout` will cut it off mid-run and look like a hang.

Iterate on one file instead:

```bash
.venv/bin/python -m pytest tests/test_tenancy.py -q
```

---

## Recovery

Something broken and you want yesterday back? See [REVERT.md](REVERT.md) — the tag is
`pre-multitenant` and the revert has been tested, not assumed.

If inbound stops resolving, the usual cause is a pharmacy row whose `wa_jid` /
`gowa_device_id` no longer matches GOWA. Compare the two:

```bash
./run.sh safety
curl -s -u "$GOWA_USER:$GOWA_PASS" http://localhost:3001/devices
```

and re-run `./run.sh bind <slot>`.

---

## Command reference

| Command | Does |
|---|---|
| `./run.sh migrate` | Apply schema, in numeric order |
| `./run.sh whatsapp` | Start the GOWA container |
| `./run.sh qr` | Pair by QR, self-refreshing |
| `./run.sh pair <phone> [slot]` | Pair by 8-character code — for remote pharmacies |
| `./run.sh bind <slot> [name]` | Attach a paired slot to a pharmacy |
| `./run.sh unpair` | Log a slot out |
| `./run.sh brand` | Push display name + photo for `GOWA_DEVICE_ID` |
| `./run.sh safety` | **Anti-ban posture and who is reachable** |
| `./run.sh all` / `stop` | Start / stop api + dashboard |
| `./run.sh check` | Verify the schema matches the code |
| `./run.sh test` | Run the suite |
| `./run.sh say` | Send a test message |
