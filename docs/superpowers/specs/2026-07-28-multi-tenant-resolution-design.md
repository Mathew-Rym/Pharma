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

**Platform bot behaviour on Thursday.** The registration flow is deferred (§9), so the
platform handler is a stub that replies with a fixed message — *"This is the Pharma OS
platform line. Registration opens shortly."* It must be an explicit branch, not an
unresolved-tenant fall-through: `254777602338` has live customer history from testing, and
letting those messages hit the generic "unknown device" path would be indistinguishable
from a genuine routing failure while debugging on the day.

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
| 1 | Device-scoped outbound (`wa.py`) | Invisible with one number, catastrophic with three (§6) |
| 2 | `wa_jid` / `gowa_device_id` columns + partial unique indexes | Everything else needs them |
| 3 | Resolver: `device_id` → `pharmacy_id`, contextvar, replace 4 `PID` constants, platform-bot stub branch | The core change |
| 4 | `jobs.py` per-pharmacy loop | Otherwise cron writes to one tenant only |
| 5 | Seed two tenants with overlapping SKUs at different prices | What makes isolation *visible* |
| 6 | Pair both SIMs, brand each per device | Needs #1 to brand independently |
| 7 | Re-run the existing demo end to end on Pharmacy A | Regression: today's working flow must survive |
| 8 | *(after the demo)* registration + `CONNECT` activation | Narrate from a screenshot Thursday |

If time runs out, cut from the bottom. Never cut #1 or #7.

---

## 6. The landmine: outbound is single-device

`wa.py:36-37` stamps every outbound message with one hardcoded slot:

```python
if settings.GOWA_DEVICE_ID:
    kw["headers"] = {"X-Device-Id": settings.GOWA_DEVICE_ID}
```

With three sessions live, **Pharmacy B's customer receives their reply from Pharmacy A's
number.** Correct inbound resolution does not fix this, and nothing would look more
broken on stage. `send_text` must take the resolved pharmacy's `gowa_device_id`, and a
missing device must be a hard error rather than a silent fall back to the env default.

`run.sh brand` has the same shape — it brands whichever slot `GOWA_DEVICE_ID` names, so
it needs a device argument for each pharmacy to get its own name and logo.

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
2. **A reply arrives from the number it was sent to** (catches §6).
3. A's staff phone messaging B's number is not treated as staff.
4. No module-level `PID` remains anywhere (grep-style structural test).
5. A cron job writes rows for both tenants with the correct `pharmacy_id`.
6. Unknown `device_id` yields the generic message and no pharmacy name.

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
