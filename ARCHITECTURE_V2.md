# Dishii v2 — reconciled architecture

Reconciles: the v1 cloud build, the on-premises agent spec, the two open sync questions,
and the code review. One system, three tiers, one WhatsApp surface.

---

## 0. First, the review discrepancy — resolved

An earlier draft of this document claimed the code review described code that "is not in
the repo": no password backdoor, no `pharma123`, `llm.py` Anthropic-only at 187 lines.

**Both were right about different files.** The review read the *working tree*; this
document read *`git HEAD`*. Verified:

```
working tree  dashboard/app.py:29  APP_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "pharma123")
working tree  dashboard/app.py:83  if pw == APP_PASSWORD or pw in ("pharma123", "dishii-admin"):
working tree  dashboard/app.py:87  st.error("Wrong password. Default password is 'pharma123'")
git HEAD      dashboard/app.py:26  APP_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

working tree  api/llm.py  310 lines, 14 gemini/google references
git HEAD      api/llm.py  187 lines,  0 gemini/google references
```

`git status` at the time showed `M dashboard/app.py`, `M api/llm.py`, `M api/config.py`,
`M .env.example` — the Gemini port and the rebrand were staged in the working tree and
never committed. **The working tree is what runs**, so every blocker the review named is
real:

| Blocker | Status |
|---|---|
| `pw in ("pharma123", "dishii-admin")` backdoor — accepted regardless of configured password | Fixed |
| Error message printed the working password to unauthenticated visitors | Fixed |
| `DASHBOARD_PASSWORD` defaulted to `pharma123` | Fixed — no default, fails closed |
| Empty `DASHBOARD_PASSWORD` skipped the gate entirely (`if APP_PASSWORD:`) | Fixed — refuses to start |
| `google-genai` missing from `api/requirements.txt` while `llm.py` imports it | Fixed |
| `python-dotenv` missing from `dashboard/requirements.txt` while `app.py:13` imports it | Fixed |

That last pair is the one that would have killed the demo silently: the `google.genai`
import sits inside a `try/except` that only logs a warning, so the Docker image builds
green and then **every LLM call raises `RuntimeError("No configured LLM client
available.")`** at runtime, with no Anthropic key configured to fall back to.

One review point is **not** accepted: the sidebar has 8 pages, not 3. The docstring was
wrong, not the code — a pharmacy needs stock, orders and suppliers pages. Docstring
corrected instead.

---

## 1. The three tiers

```
┌──────────────── PHARMACY PREMISES ────────────────┐
│                                                   │
│  phAMACore (unchanged — still the till)           │
│      │ read-only                                  │
│      ▼                                            │
│  dishii-agent  (Python, their PC)                 │
│   • probes for the DB, downgrades to folder watch  │
│   • backfills 24 months of history ONCE           │
│   • ingests sales every 15 min                    │
│   • long-polls for commands  ── outbound only ──  │
└───────────────────────────────────────────────────┘
                                                  │
        ┌─────────────────────────────────────────▼───┐
        │  CLOUD  (Railway + Supabase)                │
        │   FastAPI · batches · expiry · forecast     │
        │   vision · reports · reconciliation         │
        └──────┬──────────────────────────┬───────────┘
               │                          │
        ┌──────▼───────┐          ┌───────▼──────────┐
        │  WhatsApp    │          │  Streamlit       │
        │  everyone    │          │  screen-only jobs│
        └──────────────┘          └──────────────────┘
```

**Why the agent polls us instead of us calling it:** no inbound ports, no router
config, no firewall exception, no remote-shell surface, works from any NAT'd machine on
any ISP. Asking a pharmacy to port-forward is how you lose the deal in the IT
conversation.

**"Call things from WhatsApp" is therefore a queue, not a call:**

```
"SYNC"  →  router.py  →  insert agent_commands  →  agent polls (≤60s)
        →  agent runs it locally  →  POSTs result  →  we WhatsApp the answer
```

Up to 60 seconds of latency. For "resync now" that's invisible.

---

## 2. The problem nobody had flagged yet

You are not replacing phAMACore. That means **stock has two writers who don't know
about each other**:

- Dishii receives goods → `+150 pieces`
- phAMACore's till sells them → `-30 pieces`, and Dishii never hears about it

Within one day Dishii's stock number is fiction. Every downstream feature — expiry
value at risk, reorder suggestions, FEFO allocation for a patient order — is computed
off a number that's drifting. **This is the single biggest correctness risk in the
whole "make it smarter, don't replace it" strategy**, and it's why the agent isn't
optional garnish.

`agent_api.apply_pos_sales()` fixes it: POS sales land in `pos_sales`, then get applied
to the ledger FEFO. phAMACore doesn't record which batch was sold, so we assume
oldest-first — that's what good practice says they do, and we say the assumption out
loud rather than hiding it.

**Then the leftover gap becomes your best feature.** Received minus sold, versus what
phAMACore says is on the shelf. That difference is miscounts, unrecorded sales and
shrinkage. Nobody at that pharmacy can currently see it. `VARIANCE` on WhatsApp:

> ⚖️ **Stock variance** — 7 items, KES 18,400 unexplained
> • Prenor 25/5mg 30s — till says 22, we calculate 30 (−8 pcs, KES 3,637)

That's the number an owner cares about more than any report. Show it on Friday.

**Unit caveat, flagged not hidden:** phAMACore writes quantities as `5W0P`. The agent
cannot convert that to pieces because it does not know `pack_size` — only the cloud
does. `_parse_qty` therefore returns the whole-pack count for W/P notation and ships the
original string as `qty_raw`, and `/agent/snapshot` multiplies by `pack_size`. **Confirm
on-site which unit each export actually uses before trusting a variance figure**, because
a pack/piece mix-up would make every variance wrong by a factor of the pack size.

---

## 3. Answering the two sync questions without answering them

The confirmed answers were "scheduled file export only" and "mixed / not sure" about
history shape. Neither changes the architecture — `agent.py` is layered and
self-downgrading, so **the answer changes when automation switches on, not what we
build**:

| If phAMACore turns out to be… | Agent does | Human effort |
|---|---|---|
| Readable DB (Firebird/MSSQL/Access) | `db_poll`, 15-min read-only poll with high-water mark | none, ever |
| Export buttons, locked DB | `folder_watch` — sweep + debounce 5s | someone clicks export, or a Scheduled Task does |
| No machine access | `manual` — WhatsApp/email the CSV | human forever |

Run this on their PC Monday, before writing a line of DB code:

```
python agent/agent.py --probe-only
```

It prints open DB ports, Windows services, ODBC DSNs, every `.fdb/.mdb/.mdf` on disk,
and **which file was modified in the last two hours** — that one is the live database.
Then WhatsApp `PROBE` and the same scan comes back to your phone.

Same story for history shape. `classify_and_parse()` sniffs headers and routes to one of
three shapes:

- **transaction-level** (`date, product, qty`) → `pos_sales`, real day-of-week signal
- **monthly totals** (phAMACore's Monthly Stock Activity screen) → `sales_history_monthly`
- **stock snapshot** → reconciliation only, no demand signal

Unrecognised files land in `rejected\` with a `.reason.txt` instead of vanishing.

---

## 4. The amnesia problem — the most important thing in the review

The review's sharpest point, and it reframes what the agent is *for*:

> `v_velocity_90d` divides by a hardcoded 90 and reads only `stock_movements`. The CSV
> seeder loads a stock snapshot. So `avg_daily = 0` for every product until the system
> has run 90 days on its own — while the pharmacy has 24 months of demand signal
> sitting on that PC.

Correct. A forecasting system that needs 90 days before it says anything useful will be
switched off in week three.

**Fixed by `v_demand_baseline`**, which blends:

```
live ledger sales     if >=21 days observed   -> confidence high/medium
phAMACore history     otherwise               -> confidence medium
nothing               -> say "no demand signal yet", never forecast zero as if it were fact
```

### Why historical sales are NOT written to `stock_movements`

This is the trap worth stating explicitly. `tests/test_invariants.py` asserts
`batches.qty_pieces == sum(stock_movements.delta_pieces)`, and the README calls that the
one failure that "ends a pilot on the spot." Backfilling 24 months of past sales as
`reason='sale'` ledger rows would break it instantly — current batch quantities do not
reflect two years of history, and historical sales have no real `batch_id` to attach to.

So historical aggregates live in **`sales_history_monthly`**: product-level, no
`batch_id`, explicitly marked `source='phamacore_backfill'`. It feeds forecasting and
nothing else. `v_demand_baseline` prefers live ledger data and only falls back to
history, so there is no double-count once real sales accumulate.

`stock_movements` gains only `pos_sale` — sales happening *now* through their till,
which are genuine ledger events and must move batch quantities.

**`v_seasonality`** gives the thing the pharmacist actually told you about:

> "some meds are a hit at different seasons where docs prescribe them to many patients
> and then they just aren't any more"

A trailing 90-day average cannot see that. A month-of-year index over 24 months can.
`season_index` is clamped to 0.4–2.5 so one freak month can't triple an order.

**No ML, deliberately.** An owner won't act on a number he can't interrogate. Every
forecast carries a plain-English `method`, so `WHY prenor` answers:

> **Prenor 25/5mg Tabs 30s**
> On hand: 3W0P · Selling: 8.2 pcs/day (x1.35 this month)
> Next 30 days: about 332 pcs · Cover: 11 days
> Basis: phAMACore history (18 months); July runs 1.4x higher than average
> Recent months: Feb 180, Mar 210, Apr 265, May 240, Jun 290, Jul 340

That's defensible in front of a sceptical owner. A gradient-boosted model isn't.

**What it does not do**, so nobody oversells it: no day-of-week effects unless
transaction-level history arrives, no trend extrapolation, no promotion or epidemic
awareness, no supplier-stockout modelling, and no confidence intervals — `confidence` is
a coarse high/medium/low based on how much data exists, not a statistical bound.

---

## 5. The flows, as built

### A. Receiving (unchanged, still the wedge)
Photo of supplier invoice → `DONE` → 18 lines with batch + expiry → `OK` → stock.

### B. Patient order — now numbered
```
Patient sends prescription photo
  → consent (first time only)
  → vision extraction
  → numbered list, priced, with out-of-stock shown:
       1. Amoxil 500mg 21s — KES 450
       2. Zinnat 250mg 10s — out of stock
       3. Panadol 500mg 24s — KES 180
    "Reply with the numbers you need — e.g. 1,3 — or ALL"
  → patient: "1,3"
  → order built, FEFO, excludes batches expiring <60 days
  → PHARMACIST GATE (below)
  → payment
  → e-receipt PDF with traceability QR
  → pharmacist packs
```

### The pharmacist gate moved to WhatsApp

The review was right: a self-selected dropdown behind one shared password is not
attribution, it's theatre. PPB attribution is the legal core of this product.

Now: the **original prescription image** is forwarded to the pharmacist's own phone
alongside the extraction, and they reply `APPROVE 4417`.

Two reasons this is better, not just cheaper:

1. **Zoom.** The review nailed it — zoom is the single most important control on a
   handwritten Kenyan script. WhatsApp's native image viewer has pinch-zoom, rotate and
   fullscreen, tuned by Meta for exactly this hardware. Anything we build in Streamlit
   is worse. And the pharmacist verifies from the bench instead of walking to the office.
2. **Attribution.** A PIN only they know, from a phone number already whitelisted in
   `staff`. Two weak factors are meaningfully better than a dropdown. Still not real
   auth — it's the cheapest thing that isn't a lie. Lockout after 4 failures.

Dashboard queue kept as the fallback, now with a real zoom/pan/pinch viewer
(`zoomable()` — 30 lines of HTML, works on desktop and phone).

`duty_roster` means a 2am prescription pings the pharmacist on shift, not five phones.

### C. Payment — the demo unlock

| Path | Works Friday? | Notes |
|---|---|---|
| **Forwarded M-Pesa SMS** | yes, today | Customer pays to their real Paybill 4166919, forwards the confirmation SMS into the chat. We parse code + amount + account ref, match the order, confirm. **Zero Safaricom onboarding.** |
| STK push (sandbox) | partly | Only against Safaricom's test MSISDN `254708374149`, and `MPESA_CONSUMER_KEY`/`SECRET` are currently empty in `.env` |
| STK/C2B (production) | no | Go-live takes days to weeks. Don't put it on the critical path. |

**Say this out loud in the pitch, don't hide it:** a forwarded SMS is text, and text can
be faked. It's marked `source='sms_forward'`, the receipt code is unique-indexed so the
same SMS can't be replayed, and staff get told *"check it appears in the M-Pesa
statement before handing over goods."* The owner checks his statement anyway. Production
replaces this with the C2B callback and demotes it to a late-callback fallback.

### D. Reorder — the loop
```
07:00  agent has already ingested overnight POS sales
07:05  forecast_refresh -> seasonal baseline per product
07:06  reorder_message -> WhatsApp to owner, grouped by supplier, each line
       carrying its own reason
       "Reply PO to create draft orders"
owner: PO
       -> draft POs created, one WhatsApp per supplier with quantities + rationale
owner: OKPO 4417
       -> PO sent to the supplier's WhatsApp, recorded against the owner
delivery arrives -> photograph invoice -> Flow A -> loop closes
```

That's stocktake → sales → forecast → order → confirm → receive, with a human tap at
the two points money moves.

---

## 6. Streamlit or PWA?

**Streamlit, for now.** Not because it's better — because a PWA costs you three days
you don't have, and the strategy says the dashboard is secondary. Every screen-only job
is a data grid, and Streamlit does data grids in ten lines.

The honest counter: Streamlit is genuinely bad at exactly one screen that matters — the
pharmacist queue on a phone. That's now solved twice over: the WhatsApp approval path
(primary) and the `zoomable()` component (fallback).

**Build the PWA when** you have 5+ pharmacies, or a pharmacist asks to verify from
their phone at 10pm, or you need offline. Not before. Keep every mutation inside `api/`
and the PWA becomes a new frontend rather than a rewrite — which is why the review's
"Orders page writes UPDATE straight from the UI" complaint mattered and is now fixed.

---

## 7. What was fixed from the review

| Issue | Fix |
|---|---|
| Password backdoor + `pharma123` default + password printed pre-auth | Removed; no default; fails closed and refuses to start |
| `google-genai` / `python-dotenv` missing from requirements | Added — this one silently killed every LLM call |
| Pharmacist identity = dropdown | WhatsApp approval + PIN, lockout after 4 fails |
| Verification image has no zoom | `zoomable()` — scroll/pinch zoom, drag pan, dbl-click reset |
| Orders page bypasses `api/` | `_set_order_status()` — notifies customer, records actor, spinner + error handling |
| `job_runs` / `wa_messages` unscoped | `pharmacy_id` filter added; **message bodies no longer rendered at all** |
| PO quantities not editable | `EDITPO` from WhatsApp routes to the dashboard PO page |
| No forecasting, amnesia after CSV seed | `v_demand_baseline` + `v_seasonality` + `forecast.py` |
| No rota, morning digest spams everyone | `duty_roster` + `_on_duty_pharmacists()` |
| No confirmation/spinner on mutations | Spinners + try/except on dashboard mutations |
| Docstring says 3 pages, ships 8 | Docstring corrected |
| No phAMACore connector | `agent/agent.py` — probe, DB poll, folder watch, backfill |
| Agent outbox stalled forever after 4 failures (`age = 0` hardcoded) | Real `last_attempt_at` backoff; batches park at 12 attempts and are logged, not lost |
| Agent recursed `return main()` on enrolment failure | Retry loop |
| `interval '%s minutes'` — placeholder inside a SQL string literal | `make_interval(mins => %s)` |

Still open, deliberately: per-query TCP connections and no `@st.cache_data` in the
dashboard. Single-user internal tool on a pilot. Not worth build-weekend hours.

---

## 8. On "don't focus on security for the demo"

Genuinely fine to defer: RLS policies, SQLCipher, code signing, cert pinning, DPIA,
the agent spec's field allow-list strictness, hashing `item_code`, multi-tenant
isolation, audit-log rotation. That's weeks of work protecting a system with one
customer.

**Build the boundary now (free), harden it later (expensive).** The agent already
speaks outbound-only over a per-install token with a local queue. Keeping that shape
costs nothing today and means the hardening is later a config change rather than a
rewrite.

Three things worth keeping, and the argument is commercial, not moral:

**1. Don't name the medicine in outbound messages.** One line of code. v1 sent *"your
Xarelto should be running low"* — Xarelto implies AF or a clot history, and family
phones get read by families. Changed to *"one of your regular items is due for a
refill."* If that message lands wrong with one patient during your pilot, you don't get
a second pharmacy.

**2. Use consenting test patients on Friday, not walk-ins.** Your own team, a friend, a
staff member. You get an identical demo without a real stranger's prescription in a
German S3 bucket before anyone has signed anything.

**3. Keep the pharmacist PIN.** Not privacy — liability. If a wrong dose goes out and
the record says "someone picked Pharmacist from a dropdown," the pharmacy carries that,
and they will ask who's responsible.

---

## 9. Plan to Friday

**Saturday**
1. `db/schema_v2.sql` in Supabase (additive, safe on top of v1)
2. Flow A end to end on the real MedTrack invoice — **git tag this**
3. Set PINs on the dashboard, add yourself to `duty_roster`

**Sunday**
4. Numbered-list patient flow, staff phone as the "patient"
5. Forwarded-SMS payment path (use a real M-Pesa SMS you already have)
6. Deploy both services to Railway, **volume mounted at `/data`**
7. Generate an agent enrolment token

**Monday — on-site, highest value day of the week**
8. `python agent/agent.py --probe-only` on their PC — paste output back
9. Whatever export buttons exist — run them — drop into `C:\Dishii\exports`
10. Watch the history land, then `WHY <product>` on WhatsApp
11. **Confirm whether export quantities are packs or pieces** (see §2 caveat)
12. Ask about vendor contract terms on third-party DB access, in writing

**Tuesday–Thursday** — 20 real invoices through Flow A, `pytest tests/ -v` after every
session, rehearse six times on their WiFi.

**Friday demo order** — this sequence, nothing else:
1. Vivian photographs today's invoice — 40 seconds — stock received with batches
2. Owner texts `EXPIRY` from his own phone
3. Owner texts `VARIANCE` — the unexplained-stock number — **the moment you win**
4. Owner texts `ORDER` — forecast with reasons — `PO` — `OKPO 4417` — sent to MedTrack
5. Owner texts `report for july` — PDF with charts
6. One slide: Vivian's phone as a single point of failure

**Cut Flow C from the demo if time is short.** It's the weakest part and the only one
touching patient data. Flow A + variance + reorder is a complete, defensible story.

---

## 10. Answer these on-site Monday

- Which DB engine, and does the vendor document an export or API?
- **Are export quantities packs or pieces?** (Gets every variance wrong if assumed.)
- Does a sale link to a customer record? Is a phone number stored anywhere?
- Do the export buttons produce CSV, XLS, or PDF only? (PDF-only changes the plan
  materially.)
- Is the PC left on overnight, and does it sleep? (Kills a 15-min poller.)
- Who has admin rights, and does a domain policy block service installs?
- Will antivirus quarantine an unsigned executable? (Budget for a code-signing cert.)
- Does the vendor contract prohibit third-party DB access? **Get the owner's written
  authorisation before the agent reads anything.**

On the agent language: for a shipped product .NET 8 is right — no runtime install,
first-class Windows Service hosting, fewer antivirus arguments. For Friday, Python is
right, because the toolchain already exists and `python agent.py` runs today. Migrate
when you have a second pharmacy; the protocol and the schema don't change.

**One tension in the agent spec worth raising:** it says the local SQLite is "backed up
nowhere," framed as a privacy virtue. But that recreates the exact failure this project
started from — Vivian's phone dying and taking the supplier numbers with it. If that PC's
disk fails, every consent record and pharmacist eligibility decision is gone, and
re-consenting hundreds of patients at the counter is brutal. An encrypted local backup
to a second on-premises location keeps the boundary intact and fixes it.
