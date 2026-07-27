# Auth — where it is, and what is left

## What shipped (Phase 1)

Per-user sign-in with a **6-digit code sent over WhatsApp**, backed by a permanent
Supabase Auth identity.

```
   phone number ──► staff row found ──► 6-digit code ──► WhatsApp
                                              │
                        typed on the desktop  ▼
                                        verify (hashed, single-use)
                                              │
                                              ▼
                              auth.users identity created/linked
                                              │
                                              ▼
                     session: this person, this pharmacy, this role
```

### Why a code and not a magic link, email, or Google

| Option | Why not (yet) |
|---|---|
| Magic **link** over WhatsApp | Opens on the *phone*. People sign in on a *desktop*. The link makes the cross-device problem worse, not better. |
| Email magic link | Needs SMTP configured in Supabase. Also: a pharmacy assistant may have no work email. |
| Google OAuth | Needs a Google Cloud project, consent screen and redirect URL. Many staff have no Google account. |
| **WhatsApp code** | Already installed on every staff phone. `staff.phone` is already the access list. Works today with zero external configuration. |

No email is ever sent by Supabase: `admin.generate_link` mints the identity token
without delivering it, and `verify_otp` redeems it server-side. That also sidesteps
the usual Streamlit problem where Supabase returns tokens in a URL *fragment* the
server cannot read.

**Google stays on the roadmap.** It is an addition, not a prerequisite.

## Rolling it out

`AUTH_MODE` in `.env`. Move one step at a time; each step leaves a working system.

| Value | WhatsApp sign-in | Shared password | Use when |
|---|---|---|---|
| `shared` *(default)* | – | yes | Today. Nothing changes. |
| `whatsapp` | yes | yes (break-glass) | Rollout. Staff sign in personally; the shared password still rescues you. |
| `strict` | yes | **no** | Once every active staff member has signed in at least once. |

Check who is ready before going `strict`:

```sql
select name, role, access_state, last_login_at from v_staff_access
 where is_active order by access_state, name;
```

`access_state` is `invited` until that person has signed in once, then `active`.
Anyone still `invited` when you switch to `strict` cannot get in.

**The default is `shared` on purpose.** An auth migration must never be able to lock a
pharmacy out of its own stock system mid-shift.

## What this changes in the dashboard

Signed in with the shared password:
- pharmacy chosen from a dropdown
- "Signed in as" chosen from a dropdown ← *this is the attribution theatre the review flagged*

Signed in personally:
- pharmacy comes from **your staff row**; no switcher exists
- the acting user is **you**; no dropdown
- a Sign out button
- every sign-in, success or failure, lands in `login_events`

## There is no separate invitation flow, deliberately

The schema has an `invitations` table, but the UI does not use it. Adding someone's
number under **Setup → Who the system talks to** already *is* the invitation: from that
moment they can request a code and sign in. A token-and-link flow on top would be a
second path to the same outcome, with an extra expiry to get wrong.

The table stays because a future email/Google flow needs it — those cannot rely on
possession of a WhatsApp number.

## Security properties, and the tests that hold them

| Property | Test |
|---|---|
| Codes stored SHA-256, never plaintext | `test_the_code_is_never_stored_in_plaintext` |
| A used code cannot be reused | `test_a_used_code_cannot_be_used_again` |
| Lockout after 5 failures, 15 min | `test_lockout_after_repeated_wrong_codes` |
| Lockout **cannot be reset by requesting a new code** — it lives on `staff`, not the code row | same test |
| Codes expire in 10 minutes | `test_expired_code_is_rejected` |
| Deactivating staff revokes dashboard access, not just WhatsApp | `test_deactivated_staff_cannot_sign_in` |
| An unknown number gets the **same answer** as a known one | `test_unknown_number_gets_the_same_answer_as_a_known_one` |
| No code is sent to a non-staff number | `test_no_code_is_sent_to_an_unknown_number` |
| Sign-in survives Supabase Auth being down | `test_sign_in_still_works_when_supabase_auth_is_down` |
| Default mode is the old behaviour | `test_default_auth_mode_is_the_old_behaviour` |

The enumeration one matters more than it looks: a sign-in box that says *"that number
is not a staff member"* is a tool for discovering who works at the pharmacy.

---

# Phase 2 — database-enforced isolation

## What was actually wrong before

RLS was already **enabled** on most tables with **zero policies**, and the checkpoint
described that as a fail-closed posture. It was not protecting anything:

```sql
select rolname, rolbypassrls from pg_roles where rolname = 'postgres';
-->  postgres | true
```

The application connects as `postgres`, and a role with `BYPASSRLS` ignores every
policy. Enabling RLS and connecting as `postgres` looks locked in the Supabase UI and
enforces nothing. That is worse than no RLS, because it reads as done.

## What shipped

`schema_v7.sql` adds a role that **cannot** bypass RLS, plus a `tenant_isolation`
policy on all 31 public tables. `db.tenant_scope()` switches into it per transaction:

```python
with tenant_scope(pharmacy_id) as cur:
    cur.execute("select * from products")     # no WHERE pharmacy_id needed
```

```
set local role pharmaos_app          -- policies now apply
set local app.current_pharmacy = ... -- which tenant they resolve to
```

`SET LOCAL`, not a separate connection: it dies with the transaction, so it cannot
leak onto whoever borrows that pooled connection next — asserted by
`test_scope_does_not_leak_to_the_next_transaction`.

Child tables (`grn_lines`, `order_lines`, `po_lines`, `login_codes`, …) have no
`pharmacy_id`; they are scoped through their parent, so there is still exactly one
definition of who owns a row rather than a denormalised copy that can drift.

### Proven, not asserted

`tests/test_rls.py` uses two real pharmacies and checks behaviour:

| | |
|---|---|
| A cannot read B's rows | `test_a_tenant_cannot_read_another_tenants_rows` |
| A cannot INSERT rows owned by B | `test_a_tenant_cannot_write_into_another_tenant` |
| A cannot UPDATE B's rows | `test_a_tenant_cannot_update_another_tenants_rows` |
| Unset tenant shows **nothing**, not everything | `test_unset_tenant_shows_nothing_rather_than_everything` |
| Child rows don't leak | `test_child_rows_are_scoped_through_their_parent` |
| `pharmaos_app` cannot bypass RLS | `test_the_app_role_cannot_bypass_rls` |
| Every table has a policy (queried from the DB, not a hand-kept list) | `test_policies_exist_on_every_tenant_table` |

## A bug this surfaced immediately

`jobs._run()` inserted into `job_runs` **without** `pharmacy_id` — for months. Nothing
complained, because the dashboard papered over it with `or pharmacy_id is null`. Under
RLS that same insert fails the `WITH CHECK` outright, so **every cron job would have
started failing the moment isolation was switched on.** Fixed, and
`test_no_insert_omits_pharmacy_id_on_a_tenant_table` now scans for the whole class.

## Where it actually stands — read this bit

`DB_ENFORCE_RLS` defaults to **false**, and even set to true it only affects code
inside `tenant_scope()`. The existing `q()` / `ex()` helpers still connect as
`postgres` and are still protected only by their hand-written `where pharmacy_id = %s`.

So, precisely:

- **The mechanism is built and proven.** Isolation works and is tested against a real
  second tenant.
- **The application does not route through it yet.** Migrating ~200 call sites is
  Phase 2b.

Two things block flipping it on globally rather than per-call:

1. **Onboarding creates a pharmacy.** `WITH CHECK` rejects inserting a pharmacy whose
   id is not already the current tenant, which is correct and also breaks first-run.
2. **The admin view lists all pharmacies.** That is a legitimate cross-tenant read and
   needs an explicit exemption, not a leak.

Both need an admin escape hatch — a `pharmaos_admin` role, or running those specific
operations outside `tenant_scope()`. Small, but it should be deliberate rather than
discovered when onboarding stops working.

**Honest status: one deployment is safe for pharmacies that all belong to you. The
database can now prove isolation, but the app is not yet asking it to.**
