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

## What is NOT done — Phase 2

**RLS is not enabled, and tenant isolation is still application-level.** Every query
filters `pharmacy_id = %s` by hand. That is discipline, not isolation — one forgotten
filter leaks another pharmacy's data, and the reviewer already found two such queries
(`job_runs`, `wa_messages`).

Phase 2:

1. RLS policies on every tenant-scoped table, keyed on `auth.uid()`
2. A **non-`service_role`** connection for user-facing queries — `service_role`
   bypasses RLS entirely, so policies alone would do nothing
3. `set local app.current_pharmacy` per request
4. Pass the Supabase session JWT from the dashboard into the API

Until then the honest statement is: **one deployment is safe for pharmacies that all
belong to you. It is not yet safe for a second paying customer.**

Phase 1 was the prerequisite — RLS needs `auth.uid()`, and `auth.uid()` needs the
identity link that now exists.
