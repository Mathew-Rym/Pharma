# Multi-tenant resolution — two pharmacies on one bot

**Written:** 28 July 2026 · **Demo:** Thursday 30 July 2026
**Scope:** make two pharmacies coexist on one codebase with visible isolation.
**Not in scope:** self-service registration, staff self-enrolment, RLS enforcement.

---

## 1. Why this is needed

The database is already multi-tenant. The application is not.

| Layer | State |
|---|---|
| Schema | `pharmacy_id` on every tenant table; `tenant_isolation` policy on all 31 public tables; isolation proven against two real tenants in `tests/test_rls.py` |
| Application | **Hard-bound to one pharmacy at import time** |

The binding is four module-level constants:

```python
PID = settings.PHARMACY_ID     # router.py:19, grn.py:18, reports.py:17, approvals.py:37
```

`settings.PHARMACY_ID` is read from `.env` once at process start. Every query below
these lines already accepts `pharmacy_id` as a SQL parameter — the value is merely
*sourced* from a boot-time constant. So a second pharmacy today means a second process
with a second `.env`, not a second row.

### The tenant key already arrives on every message

This is the part that makes the change small. GOWA's webhook body carries `device_id`,
and its value is the JID of the **receiving** account. Verified from a live inbound:

```text
gowa inbound from=254720521291 type=text device=254777602338@s.whatsapp.net
```

`main.py:199` logs that value and discards it. Nothing needs to be invented: the
resolver reads a field that is already present.

**Corollary — do not infer tenancy from the message sender.** On a message arriving at
Pharmacy A's number, the sender is the *customer*. A pharmacy's own number never appears
as the sender of a message to itself, so any `WHERE wa_number = from_phone` lookup can
never match.

---

## 2. Verified platform capabilities

Both tested against the running container on 28 July, not assumed.

| Capability | Result |
|---|---|
| Multiple concurrent GOWA sessions | **Yes.** Created slot `pharmacy-2`; `pharmacy-1` stayed `logged_in` with its `jid` intact. |
| Pair by code (no QR) | **Yes.** `GET /app/login-with-code` returns `400 VALIDATION_ERROR: phone_number(): cannot be blank` — route present, takes a number. |
| Per-slot identity | `GET /devices` returns `id`, `state`, `jid`, `display_name` per slot once logged in. |

Two consequences:

- Pairing a pharmacy handset uses an 8-character code typed on that handset. No QR image
  to relay to another phone, no ~60s expiry. This removes the entire class of failure
  that blocked pairing earlier today.
- **With more than one slot, `/app/devices` returns `DEVICE_ID_REQUIRED`** unless
  `X-Device-Id` is sent. It worked header-less with one slot. Any new code touching that
  endpoint must send the header. (`run.sh qr` survives this: it falls back to `/devices`,
  which now carries `jid` — re-verified after creating the second slot.)

---

## 3. Numbers and identities

| Number | Role | GOWA slot | State |
|---|---|---|---|
| `254777602338` | **Platform bot** | `pharmacy-1` | Paired, `display_name: Pharma OS`, logo pushed, live traffic proven |
| new SIM #1 | Pharmacy A — New Lemuma | `pharmacy-a` | To create and pair |
| new SIM #2 | Pharmacy B — Greenline | `pharmacy-b` | To create and pair |

**The platform bot keeps slot `pharmacy-1` despite the misleading name.** Reassigning that
slot to a pharmacy would require unpairing, which throws away the only working session,
its branding, and its warmed sending history — to gain nothing but a tidier identifier.
GOWA has no rename, so the wart stays. Slot ids are opaque keys stored in
`pharmacies.gowa_device_id`; nothing reads meaning from them.

Delete the exploratory `pharmacy-2` slot (`DELETE /devices/pharmacy-2`) so it cannot be
confused with `pharmacy-b`. It is empty and unpaired.

**Two different companies, deliberately.** Not two branches of one. Two branches
registering as separately-owned isolated tenants is incoherent and a pharmacy owner in
the room will say so. Flat `pharmacies` (no org→branch hierarchy) is honest for two
independent businesses and is what the schema already models.

**SIM warming.** Register both numbers and send several *human* messages from each before
the bot touches them. A number that begins automated sending hours after registration is
the classic pattern WhatsApp bans.

---

## 4. Resolution design

```text
GOWA (3 sessions) ──webhook──▶ POST /webhook/gowa
                                  │
      body["device_id"]  =  JID of the receiving account
                                  │
        ┌─────────────────────────┴──────────────────────────┐
        │ JID == platform bot JID → platform handler          │
        │ JID → pharmacies.wa_jid → pharmacy_id               │
        └─────────────────────────┬──────────────────────────┘
                                  │
                    ┌─────────────┴──────────────┐
                    │ staff(phone, pharmacy_id)?  │→ staff handler
                    │ else                        │→ customer handler
                    └─────────────────────────────┘
```

**The platform bot is a row, not a constant.** `if device_id == PLATFORM_BOT_DEVICE_ID`
would delete four hardcoded constants and add a fifth. Give `pharmacies` a `kind` column
(`'pharmacy' | 'platform'`) so a single lookup yields three outcomes: **tenant**,
**platform**, or **throw**. No second code path, no second source of truth.

**Platform bot behaviour on Thursday.** Registration is deferred (§9), so the platform
handler is a stub. It must be an explicit branch, not an unresolved-tenant fall-through:
`254777602338` has live customer history from today's testing, and letting those messages
hit the generic "unknown device" path would be indistinguishable from a genuine routing
failure while debugging on the day.

**Legacy customers get a useful sentence, not a generic greeting.** The line is
platform-only — it is not a shopfront, and dual-purposing it would reintroduce the
implicit tenant default this change exists to remove. But "contact your pharmacy
directly" is a redirect that withholds the destination: same message slot, strictly less
use. So the stub does two things:

- A phone with existing `customers` history → name the number to use. The destination is
  **derived, not hardcoded**: `customers.pharmacy_id → pharmacies.wa_number`. Those
  customers already belonged to exactly one pharmacy when the system was single-tenant,
  so this tells them where their pharmacy moved. It is a migration, not a reassignment —
  and deriving it means a real pharmacy's customers are never pointed at a demo fixture.
- Any other phone → the platform message plus `REGISTER`.

**Match explicit commands before identity.** Checking "is this phone a known owner?"
ahead of `REGISTER` routes a registered owner into owner-commands and they can never
register a second pharmacy — which breaks the two-pharmacy demo, since both are
registered from the same handset. Commands first, identity second (or owner-commands must
itself handle `REGISTER`).

**Cutover is gated.** Do not switch `254777602338` to platform-only until Pharmacy A is
paired *and* a real order has completed on it. Until then the working line stays working.

### Schema change

`pharmacies` currently has: `id, name, ppb_licence, mpesa_paybill, wa_number, timezone,
created_at`. Add:

- `wa_jid text` — the JID GOWA reports; the resolution key.
- `gowa_device_id text` — the slot id, used for **outbound** (§6).

Both nullable, each with a partial unique index (`where ... is not null`). A pharmacy
exists before it is paired, so `NOT NULL` would make the row uninsertable — the first
bug in the earlier draft plan.

### Carrying `pharmacy_id` to the queries

**Decision: a contextvar set once per inbound message.** `PID` becomes
`current_pharmacy()` in all four modules.

Rejected alternative — threading an explicit parameter through every signature. It is
cleaner in the abstract, but it is dozens of edits across four modules inside two days,
and each missed site is a chance of a silent cross-tenant read. With a contextvar the
correct tenant is the default everywhere, and a missed site behaves correctly rather than
leaking.

Accepted cost: it is implicit, with two sharp edges that get explicit guards.

1. **Background tasks.** `handle_inbound` runs through `BackgroundTasks`, so the var must
   be set *inside* `handle_inbound`, not in the webhook handler.
2. **Cron jobs.** `jobs.py` has no inbound message. It must loop over pharmacies and set
   the var per iteration. AUTH.md records that `job_runs` inserts silently omitted
   `pharmacy_id` for months; this is the same class of bug, so it gets a test.

---

## 5. Build order

Ordered so that stopping at any point leaves a working demo. **The resolver and the seed
are the demo. Registration is a bonus beat.**

| # | Task | Why this position |
|---|---|---|
| 0 | **Tag the last known-good single-tenant state; verify one-command revert** | This tag is what makes Wednesday-night breakage survivable rather than terminal |
| 1 | Device-scoped outbound, persisted on the record (`wa.py`) | Invisible with one number, catastrophic with two (§6) |
| 2 | `wa_jid`, `gowa_device_id`, `kind` columns + partial unique indexes | Everything else needs them |
| 3 | Resolver: `device_id` → `pharmacy_id`, contextvar, replace 4 `PID` constants, platform/tenant/throw branch | The core change |
| 4 | `jobs.py` per-pharmacy loop | Otherwise cron writes to one tenant only — and it is the path that catches context-based device resolution |
| 5 | Seed two tenants, visibly distinct: different companies, different stock, **different price for the same molecule** | Without this the isolation beat has nothing to show |
| 6 | Pair both SIMs, brand each per device | Needs #1 to brand independently |
| 7 | Re-run the existing demo end to end on Pharmacy A | Regression: today's working flow must survive |
| 8 | Cut `254777602338` over to platform-only | **Gated:** only after A is paired and a real order completes on it |
| 9 | *(after the demo)* registration + pair-by-code activation | Narrate from a screenshot Thursday |

If time runs out, cut from the bottom. **Never cut #0, #1, #5 or #7.** Registration stays
off the critical path: resolver + two seeded tenants *is* the demo.

---

## 6. The landmine: outbound is single-device

`wa.py:36-37` stamps every outbound message with one hardcoded slot:

```python
if settings.GOWA_DEVICE_ID:
    kw["headers"] = {"X-Device-Id": settings.GOWA_DEVICE_ID}
```

Once a second number is paired, **Pharmacy B's customer receives their reply from
Pharmacy A's number.** Correct inbound resolution does not fix this, and nothing would
look more broken on stage.

### The device must be persisted, not read from context

This is the part that is easy to get wrong in a way that passes tests.

**Do not resolve the outbound device from request or contextvar state.** Sending happens
off the request path in at least four places today — `jobs.py` reorder alerts, the Rx
approval SLA escalation, the retry path in `wa.py`, and the daily report push. By the time
those execute there is no inbound message and no contextvar. A synchronous implementation
that reads ambient context will pass every test written against the webhook path and
still cross wires the first time a scheduled job fires.

So: **the resolved `gowa_device_id` is written onto the outbound record** (the
`wa_messages` row, or the queue row for anything deferred) at the moment the message is
composed, and the sender reads it from there. A missing device is a hard error, never a
fall back to `settings.GOWA_DEVICE_ID`.

### Validate the slot against the JID before sending

GOWA addresses outbound by `X-Device-Id`, which is the slot *label* — so unlike inbound,
the label cannot be avoided. That creates one specific hole: delete a slot and later
recreate one reusing the name, and it now points at a different handset. Messages would
go to the wrong pharmacy's customers while every log line looks correct.

Guard: immediately before sending, confirm the slot's reported `jid` equals the
pharmacy's `wa_jid`, and refuse if it does not. Fails loudly instead of silently
misrouting.

`run.sh brand` has the same single-device shape — it brands whichever slot
`GOWA_DEVICE_ID` names, so it needs a device argument for each pharmacy to get its own
name and logo.

---

## 7. Error handling

| Case | Behaviour |
|---|---|
| Unknown `device_id` | Log, generic reply. **Never** a hardcoded pharmacy name. |
| Tenant unresolved | Refuse the write. Show **nothing**, not everything. |
| Staff of A commands B's number | Refused and logged; not treated as staff. |
| Outbound with no resolved device | Hard error. No default. |
| Same phone is staff at A and customer at B | Resolved per tenant; the JID decides which context applies. |

---

## 8. Tests

These decide whether the change shipped. `tests/test_rls.py` already builds two real
pharmacies, so the fixtures exist.

1. Same product name at A and B with different price/stock → each number returns its own.
2. **Message both numbers within a few seconds; each reply arrives from the number it was
   sent to.** Interleaved, not sequential — sequential passes even with a shared global.
3. **Trigger a reorder alert on B; the manager's message arrives from B, not A.** This is
   the test that distinguishes a real fix from a context-based one, because the job has no
   request scope. If §6 was implemented from ambient context, this is where it fails.
4. A product carried only by A, requested from B → not found (the demo beat, as a test).
5. A's staff phone messaging B's number is not treated as staff.
6. No module-level `PID` and no hardcoded device id remains anywhere (structural test).
7. A cron job writes rows for both tenants with the correct `pharmacy_id`.
8. Unknown `device_id` yields the generic message and no pharmacy name.
9. A legacy customer phone on the platform line is named their own pharmacy's number,
   derived from `customers.pharmacy_id`.

---

## 9. Out of scope, and why

| Deferred | Reason |
|---|---|
| Registration / `CONNECT` activation | Not a dependency of the isolation demo. Half-built on Thursday morning = nothing to show. |
| Staff self-enrolment | Needs the invitation-code flow (§10) done properly. Seeded staff is enough. |
| `DB_ENFORCE_RLS = true` | Migrating ~200 call sites is Phase 2b, and onboarding inserts break under `WITH CHECK` without an admin escape hatch. |
| org → branch hierarchy | Two independent companies do not need it. |
| PPB register verification | Needs an external data source. |

---

## 10. A constraint on any future WhatsApp login

Recorded here because it will come up again the moment self-enrolment is built.

**A PPB number must never be the credential that grants pharmacist rights.** Kenya's
Pharmacy and Poisons Board register is public and searchable. If typing `PPB-12345`
confers authority to approve prescription-only medicines, anyone who can look up a real
pharmacist's number can award themselves that authority — and every approval in the audit
log then proves only that somebody typed a public number. That destroys the one property
the POM gate exists to provide: *nothing prescription-only moves without a named
pharmacist and a timestamp.*

The PPB number is a **claim to record**, not a secret to authenticate. Authority must flow
from someone who already holds it: an owner or manager issues a one-time invitation code,
the joiner replies `JOIN <code>` from the invited handset, and the PPB number is captured
during that exchange for later verification against the register. Possession of the
invited handset supplies the proof; the invitation supplies the authority.

This matches `AUTH.md` ("adding someone's number *is* the invitation") and needs no
dashboard — the owner issues it from WhatsApp.

---

## 11. Current state, for whoever picks this up

- One pharmacy row: `Pharma` — `c1457e5e-9f62-468b-ab50-b41382e83610`, the `PHARMACY_ID`
  in `.env`.
- One staff row: `Admin / owner / 254700000001` — **not a real handset**, which is why
  live inbound traffic currently routes down the customer path.
- `AUTH_MODE=shared`, `DB_ENFORCE_RLS=false`.
- `products` is empty; schema is complete (`./run.sh check` reports 25 tables, 8 views,
  all columns present).
- Staff vs customer already works and already drives stock intake: a known staff phone
  sends media to `BUCKET_INVOICES` and the GRN flow, an unknown phone to `BUCKET_RX`
  (`main.py:83`, `router.py:73-81`). Only the tenant is hardcoded, not the role logic.

---

## 12. Rejected approaches — do not reintroduce

Each of these has been proposed at least once during design. They are recorded with the
reason so the next draft does not rediscover them.

| Proposal | Why it fails |
|---|---|
| **`CONNECT <code>` on the platform line pairs the pharmacy** | Proposed three times. A message arriving on the platform device cannot create a GOWA session for a different number. Linking requires an action **on the pharmacy handset**: backend opens the session → GOWA returns a link code → the code is typed on that phone → GOWA reports the device_id and its number → bind. Chat can relay the code; it cannot do the linking. |
| **`DEVICE_MAPPING` dict / `PLATFORM_BOT_DEVICE_ID` constant** | Replaces one hardcoded tenant with a hardcoded tenant list. A dict cannot contain a pharmacy that registered at runtime, so every self-registered tenant raises. DB lookup only. |
| **Resolve the outbound device from request context** | Background jobs, retries and scheduled alerts have no request context (§6). Passes webhook tests, crosses wires on the first cron run. |
| **Bind tenancy to the message sender** (`WHERE wa_number = from_phone`) | On a message to Pharmacy A's number the sender is the *customer*. A pharmacy's own number never appears as the sender of a message to itself, so the lookup can never match. |
| **Dual-purpose the platform line as a shopfront** | Reintroduces an implicit tenant default, which is the entire thing being removed. |
| **PPB number as the login credential** | The register is public (§10). |
| **Identity checked before explicit commands** | A known owner can then never reach `REGISTER`, breaking two-pharmacy registration from one handset. |
| **`device_id TEXT UNIQUE NOT NULL` on `pharmacies`** | A pharmacy exists before it is paired; the first insert would violate the constraint. Nullable + partial unique index. |

---

## 13. Demo ordering

Lead with **isolation on two already-seeded tenants**, not registration. Registration is
the least-finished, highest-risk component; opening with it turns a likely failure into
the first thing the room sees, rather than a footnote.

The strongest thirty seconds available, and it costs nothing to prepare: **from Pharmacy
B, ask for a product only Pharmacy A stocks. It comes back not found.** That is isolation
demonstrated rather than asserted — and it is the one beat that cannot be faked by a
seeded screenshot.
