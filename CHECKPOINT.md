# CHECKPOINT — 2026-07-26, start of v2 implementation

Restore point for the v2 work (agent protocol, POS ingestion, forecasting, WhatsApp
approvals). Read this first if you come back cold.

## Where the repo was when v2 work started

- **Commit:** `5d76f5a` — "scaffold: Pharma OS Pharmacy OS — WhatsApp-first operations layer"
  (the single commit; it contains the entire v1 system, ~5,085 lines)
- **Branch:** `main`, no remote pushes
- **Uncommitted v1 drift** (6 files, +206/−66) — this was already dirty *before* v2:

  | File | What changed, uncommitted |
  |---|---|
  | `api/llm.py` | +225/−66 — the Anthropic→Gemini dual-provider port |
  | `api/config.py` | `load_dotenv()`, `LLM_PROVIDER`, Anthropic key made optional |
  | `.env.example` | Anthropic block → `AI / LLM` block, defaults to `gemini-3.6-flash` |
  | `api/router.py` | one line: `"You are Pharma OS"` → `"You are Pharma OS"` |
  | `dashboard/app.py` | rebrand + **a password backdoor** (see below) |
  | `.gitignore` | added `.mcp.json`, `.agents/` |

To get back to exactly this point:

```bash
git stash list                 # v2 work is all new files + patches, not stashed
git diff                       # the v1 drift above, still uncommitted
git checkout 5d76f5a -- <file> # to reset any single file to the scaffold
```

## The review discrepancy — resolved, do not re-litigate

`ARCHITECTURE_V2.md` §0 claims the code review described code "not in the repo"
(no backdoor, no `pharma123`, no Gemini, `llm.py` is 187 lines and Anthropic-only).

**Both are correct about different files.** Verified:

```
working tree  dashboard/app.py:29  APP_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "pharma123")
working tree  dashboard/app.py:83  if pw == APP_PASSWORD or pw in ("pharma123", "pharmaos-admin"):
working tree  dashboard/app.py:87  st.error("Wrong password. Default password is 'pharma123'")
git HEAD      dashboard/app.py:26  APP_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

working tree  api/llm.py  310 lines, 14 gemini/google references
git HEAD      api/llm.py  187 lines,  0 gemini/google references
```

`ARCHITECTURE_V2.md` reviewed `git HEAD`. The code review reviewed the working tree.
**The working tree is what runs**, so the backdoor and the missing `google-genai`
dependency are real and are fixed in v2.

Consequence for implementation: several `patch_v2.py` anchor strings target HEAD text
that no longer exists in the working tree. Those anchors were retargeted rather than
left to fail silently — the patch scripts print `! label — anchor not found` and keep
going, so an unretargeted anchor is a silent no-op.

## v1 blockers that v2 fixes

1. **Dashboard password backdoor** — `pw in ("pharma123", "pharmaos-admin")` accepted
   regardless of configured password; error message printed the password.
2. **`google-genai` missing from `api/requirements.txt`** while `api/llm.py` imports it
   and `.env` sets `GEMINI_API_KEY`. The import sits in a `try/except` that only logs,
   so Docker builds green and then every LLM call raises
   `RuntimeError("No configured LLM client available.")`.
3. **`python-dotenv` missing from `dashboard/requirements.txt`** while `app.py:13`
   imports it.
4. **Pharmacist identity was a `selectbox`** — anyone past one shared password could
   sign PPB-attributable clinical approvals as someone else.
5. **`job_runs` / `wa_messages` queries had no `pharmacy_id` filter**; message bodies
   (customer content, DPA 2019) rendered to anyone with the shared password.
6. **Orders page issued raw `UPDATE`s** from the UI — no stock movement, no customer
   notification, no record of who dispatched.

## The correctness risk v2 exists to fix

Pharma OS does not replace phAMACore, so **stock has two writers that don't know about
each other**: Pharma OS receives (`+150`), phAMACore's till sells (`−30`), and Pharma OS
never hears about the sale. Within a day the stock number is fiction, and expiry
value-at-risk, reorder suggestions and FEFO allocation are all computed off it.

The bridge agent fixes this, and the leftover gap (received − sold vs what the till
says is on the shelf) becomes the `VARIANCE` feature.

Related: a CSV stock snapshot carries **no demand signal**, so `v_velocity_90d` returns
`avg_daily = 0` for every product until the system has run 90 days on its own — while
the pharmacy has 24 months of signal on that PC. `v_demand_baseline` +
`v_seasonality` + the history backfill are what fix the amnesia.

## Open questions for the on-site Monday visit

- Which DB engine is phAMACore, and does the vendor document an export or API?
- Do the export buttons produce CSV, XLS, or **PDF only**? (PDF-only changes the plan.)
- Is the PC left on overnight, and does it sleep? (Kills a 15-min poller.)
- Does the vendor contract prohibit third-party DB access? **Get the owner's written
  authorisation before the agent reads anything.**
- Will antivirus quarantine an unsigned executable?

Run `python agent/agent.py --probe-only` on their PC before writing any DB code.

## Status as of 2026-07-26 — v2 code + schema are DONE

- **All v2 patches applied** to `api/`, `dashboard/`, `wa-gateway/`. Verified by
  grepping each anchor, not by trusting the patch scripts' exit code. The
  `patch_v2*.py` scripts are no longer on disk; the edits are in the working tree.
- **`db/schema_v2.sql` applied to the live Supabase.** Dry-run first inside a
  transaction that was rolled back, then committed. Verified present: all 8 tables,
  all 3 views (`v_demand_baseline`, `v_seasonality`, `v_stock_variance`), the
  `staff` PIN columns, the `payments` SMS columns, and the `stock_movements` CHECK
  now admitting `'pos_sale'`.
- **`tests/test_v2.py` added** — 45 pure-function tests over the three v2 paths that
  can silently corrupt stock or money: unit resolution, export-shape detection, SMS
  payment parsing, forecast pack rounding.
- **Suite: 73 passed, 0 failed, 0 skipped** (`set -a; . ./.env; set +a` first, or the
  6 DB invariant tests skip themselves).

### Bug found and fixed while finishing: pack-denominated POS sales were dropped

`_parse_qty` returns `(packs, loose, is_packs)` and a `'2W0P'` row therefore lands in
`pos_sales` with **`qty_pieces = 0`**, its real quantity in `raw.qty_packs` — because
only the cloud knows `pack_size`. But `apply_pos_sales()` selected
`where ... and qty_pieces > 0`, which excluded exactly those rows.

Effect: every pack-denominated till sale — the normal case, since phAMACore writes
`NWNP` — was never applied, never flagged with `apply_error`, and never decremented
stock. Silent, and it defeats the entire purpose of the agent. The unit-resolution
work inside the loop (`_resolve_pieces`, the `remaining <= 0` guard) was already
correct; only the row-selection filter was missed.

Fixed at `api/agent_api.py:242` — the filter now also admits
`coalesce((raw->>'qty_is_packs')::boolean, false)`. Cast semantics confirmed against
the live DB (pack row → selected, legacy `qty_pieces` row → falls back, null → false).
Locked in by `test_pack_rows_are_not_excluded_by_the_apply_filter`, which was
confirmed to fail against the pre-fix code rather than pass vacuously.

### Second bug, found by the end-to-end run: one day of live sales beat 24 months

`v_demand_baseline` blended the two signals with
`coalesce(live, history)` — so live won whenever it existed, even with a single day
behind it. The `method` CASE right below it has always applied the documented 21-day
threshold, so the two disagreed: the number came from live, the sentence printed to
the owner said "phAMACore history".

Caught because the e2e run reported **60.00/day with method "phAMACore history
(6 months)"** — and 6 months of history (1365 pcs / 180 days) is 7.58/day, while 60
was exactly the one POS sale just applied.

Effect: on the agent's first sync every product that sold that day gets
`avg_daily = today's qty / 1 day`. Here that was 8x; with a slower mover it is 30x.
`forecast_30d` was 1800 instead of 228 and cover 1.5 days instead of 11.9, so `ORDER`
would have asked the owner to buy roughly a year of stock on day one — which is how
a forecasting feature gets switched off in week three.

Fixed in `db/schema_v2.sql` (view replaced in the live DB too): the preference order
is now settled live (>= 21 days) > backfilled history > short live > nothing, and a
fourth `method` branch names the short-live case as provisional. Both directions are
locked in by `test_one_day_of_live_sales_does_not_outrank_months_of_history` and
`test_settled_live_sales_do_take_over_from_history`; the first was confirmed to fail
against the old view (`avg_daily=60.0`) rather than pass vacuously.

### Verified end to end against a running API

Drove the whole agent protocol against `uvicorn` on the live DB: enrolment (bogus
token → 403, missing token → 401, per-install token != enrolment token), heartbeat
state persistence, the command queue (queued → taken → done), POS ingest of a
pack-denominated `2W0P` row (**150 → 90 pieces: 2 x 30 = 60 deducted correctly**),
replay of the same export not double-deducting, history backfill driving the
forecast, and the snapshot producing the `VARIANCE` WhatsApp message. All test data
removed afterwards; the DB is back to 0 products / 0 movements.

Script kept at `scratchpad/e2e.py` — it is not in the repo because it writes to a
real database and is not safe to run against a loaded pilot.

### The venv was broken — repaired

`.venv/bin/python3` had been repointed to `/usr/bin/python3` (3.12) while
`pyvenv.cfg` declares 3.13 and every package sits in `lib/python3.13/site-packages`,
so nothing imported. It was also created `--without-pip`. Repaired by repointing the
symlink to `/home/rym/anaconda3/bin/python3.13` and installing via
`/home/rym/anaconda3/bin/python -m pip --python .venv/bin/python install -r api/requirements.txt`.
Note the `--python` flag must precede the subcommand.

### Still to do (operational, not code)

Steps 3–4 of the Saturday plan need the real world: Flow A end-to-end on the MedTrack
invoice, then set PINs and populate `duty_roster` from the dashboard. Nothing is
committed to git yet — the whole v2 body of work is still uncommitted.

## Session 2 — GOWA, onboarding, PO documents, physical count

### Reconciling four external design reviews on "add vision counting to Loop A"

All four (Gemini, Kimi, ChatGPT, DeepSeek) independently reached the same correct
conclusion: **Loop A trusted the invoice for quantity and never verified what
physically arrived.** That was right and is now fixed.

Where they were wrong, and it matters:

1. **They proposed columns and tables that already existed.** Suggestions included
   `invoice_qty`, `actual_received_qty`, `staff_confirmed_count`, `physical_count`,
   plus new tables (`Delivery`, `DeliveryImages`, `VisionDetection`,
   `VisionVerification`, `ReceivingSession`). But v1 already shipped
   `grn_lines.qty_invoiced_pieces`, `grn_lines.qty_counted_pieces` ("what staff
   physically counted"), a `short_delivery` flag, `grns.discrepancy_note`, and an
   `approve()` that already prefers counted over invoiced and records the difference.
   Staff could already correct a count by replying `5:2W`. `grns` already IS the
   receiving session — it holds images, raw_extract, approved_by, approved_at.
   Adding parallel columns would have created two sources of truth for the same
   number. The real gap was far smaller: **nobody was asked to count, and nothing
   pre-filled it.**

2. **All four proposed per-line photos.** An 18-line invoice would mean 18 photos.
   Vivian will not do that, and a feature nobody uses is worse than none. Built as
   one photo (or several) of the whole delivery, with the invoice lines passed into
   the model as a reference manifest — which is also more accurate, because "how many
   AMOXIL 500MG 21S can you see" is a far easier question than "count the boxes" when
   every box is small white cardboard.

3. **None of them separated packs from pieces.** Vision cannot see 100 tablets inside
   a sealed carton; it counts cartons. Storing a piece count from vision would have
   repeated the exact 30x understatement that `2W0P` read as 2 pieces already caused
   in Loop B. `vision_packs`/`vision_loose` are stored, and pieces are derived with
   `pack_size` in the one place that conversion already lives.

4. **DeepSeek's `staff_confirmed_count int not null`** would fail on an existing table
   with rows, and would force a human count on every line before anything could be
   received.

The one addition worth keeping from them was ChatGPT's insistence on confidence and
never silently trusting an uncertain output. That is implemented, and testing improved
it: confidence alone was not enough. A model can be 100% confident it sees 3 packs
while 3 more sit out of frame. `fully_visible` is now a separate flag, and anything
not fully visible is asked about rather than declared short — a few false shortages
would teach staff to ignore every count warning, which is worse than not counting.

### The ledger boundary, which is the whole design

`apply_vision_count()` writes `vision_*` columns ONLY. It never writes
`qty_counted_pieces`, because that column means *a human stands behind this number*.
So `approve()` still receives the invoice quantity until a person confirms otherwise
with the existing `5:2W` reply. The machine can flag a discrepancy; it can never
silently change what enters stock. Locked in by
`test_machine_count_never_becomes_ledger_truth`.

`SKIP` always works. A 40-line delivery at closing time, a flat battery or a model
outage must never stop stock being received.

### Verified against a real model

Rendered a synthetic delivery photo with known ground truth (5 AMOXIL packs, 3
PANADOL) against an invoice claiming 5 and 6. Result: AMOXIL 5 packs confidence 1.0
variance 0; PANADOL 3 packs, −72 pieces (3 packs × 24) flagged, `pieces_to_receive`
still the invoice figure until `2:3W` confirmed it, after which the ledger used 72.

The first run counted PANADOL as 2 and said *"second pack is partially cropped at the
right edge"* — the model was right and the test image was wrong (8 boxes at 150px on a
1000px canvas). That is what prompted the `fully_visible` change.

### Also this session

- **GOWA** replaces Baileys as the transport, chosen for multi-device (one server, one
  WhatsApp account per pharmacy). `wa-gowa/docker-compose.yml`. `WA_BACKEND` selects.
- **Onboarding** (`dashboard/onboarding.py`): the dashboard used to `st.stop()` with
  "add staff rows first", which was unenterable. No per-user passwords by request —
  `staff.phone` is the WhatsApp whitelist, so the number IS the credential.
- **PO PDF on letterhead**, sent to the distributor on approval. Withholds
  `po_lines.rationale` and sell prices — that is the pharmacy's negotiating position.
- **`build_report_pdf` was broken and nobody knew.** fpdf2's core Helvetica is
  latin-1 only and the title had an en-dash, so `report for july` raised instead of
  returning a PDF. That is Friday demo step 5. Fixed at the layer with a
  `normalize_text` override so no future string can abort a document.
- **Manual upload** for invoice and phAMACore export, reusing the same pipelines.

## Not done, deliberately

- `MPESA_CONSUMER_KEY` / `MPESA_CONSUMER_SECRET` are empty in `.env`, so STK push
  cannot run. The forwarded-SMS path (`api/payments_sms.py`) is the demo path.
- Dashboard still opens a fresh TCP+TLS connection per query and uses no
  `@st.cache_data`. Single-user internal tool on a pilot; not worth the hours yet.
- RLS policies are still absent (RLS is enabled with zero policies, which is the
  intended fail-closed posture while the backend uses the `service_role` key).
