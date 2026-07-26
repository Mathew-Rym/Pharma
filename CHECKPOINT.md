# CHECKPOINT — 2026-07-26, start of v2 implementation

Restore point for the v2 work (agent protocol, POS ingestion, forecasting, WhatsApp
approvals). Read this first if you come back cold.

## Where the repo was when v2 work started

- **Commit:** `5d76f5a` — "scaffold: Dishii Pharmacy OS — WhatsApp-first operations layer"
  (the single commit; it contains the entire v1 system, ~5,085 lines)
- **Branch:** `main`, no remote pushes
- **Uncommitted v1 drift** (6 files, +206/−66) — this was already dirty *before* v2:

  | File | What changed, uncommitted |
  |---|---|
  | `api/llm.py` | +225/−66 — the Anthropic→Gemini dual-provider port |
  | `api/config.py` | `load_dotenv()`, `LLM_PROVIDER`, Anthropic key made optional |
  | `.env.example` | Anthropic block → `AI / LLM` block, defaults to `gemini-3.6-flash` |
  | `api/router.py` | one line: `"You are Dishii"` → `"You are Pharma OS"` |
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
working tree  dashboard/app.py:83  if pw == APP_PASSWORD or pw in ("pharma123", "dishii-admin"):
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

1. **Dashboard password backdoor** — `pw in ("pharma123", "dishii-admin")` accepted
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

Dishii does not replace phAMACore, so **stock has two writers that don't know about
each other**: Dishii receives (`+150`), phAMACore's till sells (`−30`), and Dishii
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

## Not done, deliberately

- `MPESA_CONSUMER_KEY` / `MPESA_CONSUMER_SECRET` are empty in `.env`, so STK push
  cannot run. The forwarded-SMS path (`api/payments_sms.py`) is the demo path.
- Dashboard still opens a fresh TCP+TLS connection per query and uses no
  `@st.cache_data`. Single-user internal tool on a pilot; not worth the hours yet.
- RLS policies are still absent (RLS is enabled with zero policies, which is the
  intended fail-closed posture while the backend uses the `service_role` key).
