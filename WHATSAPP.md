# WhatsApp: which numbers, who pairs, and why nothing gets banned

The rules below have been re-derived several times from scratch, and one anti-pattern has
been proposed four separate times. Written down so it stops happening.

## The distinction everything rests on

| Capability | Requires |
|---|---|
| A number can **send** to the bot | Nothing. Any WhatsApp user can. |
| The system can **receive and reply** on a number | A GOWA session for that number, created by a Linked Devices action **on that handset**. |

**Anti-pattern, proposed four times:** a `CONNECT <code>` / `ACTIVATE <code>` message arriving
on the platform device **cannot** create a session for a different number. Only the
physical handset can pair itself. Chat can relay a link code; it cannot do the linking.

## Pairing vs linking

| | What it is | Who |
|---|---|---|
| **Pairing** | Giving GOWA control of a WhatsApp account, via QR or an 8-character link code. | Bots only. One session per pharmacy shopfront. |
| **Linking** | Storing a phone number in the database so the system knows which pharmacy it belongs to. | Every human. |

**Never pair a personal phone.** GOWA would carry that person's private conversations, and
a ban would take their own communications down with it.

**Never pair a pharmacy's existing advertised number.** It is their most valuable asset and
an unofficial client can lose it. Issue a dedicated SIM and let them advertise it
gradually.

## Who sends where

| Person | Sends from | Sends to | Paired? |
|---|---|---|---|
| Customer | their own phone | pharmacy shopfront number | never |
| Staff / pharmacist / manager | their own phone | pharmacy shopfront number | never |
| Owner (reports, alerts) | their own phone | pharmacy shopfront number | never |
| Owner / pharmacy handset (registration) | the pharmacy SIM | platform bot | the SIM, yes |

A WhatsApp account cannot message itself, which is why the owner's phone must be separate
from the pharmacy number — otherwise the bot could never send them a report.

## The four gates, on every send

Implemented in `api/safety.py`, called from `wa.compose()` before the row is written.

| Gate | Rule | Where |
|---|---|---|
| 1 · Allowlist | when `WA_ALLOWLIST` is set, only those numbers can receive | dev, test, staging |
| 2 · Relationship | recipient must be a customer, staff or supplier **of the sending tenant** | everywhere |
| 3 · Chat established | an `inbound_history` row must exist for (recipient, tenant) | everywhere |
| 4 · Rate cap | per device: `WA_RATE_LIMIT_HOUR`, plus `WA_NEW_CHAT_LIMIT_HOUR` new chats | everywhere |

**Gate 3 is derived from data, never from a caller's claim.** An `is_reply` /
`is_proactive=False` parameter means every existing call site silently bypasses the check
— including the ones that caused the problem. If there is no inbound row it *is* a new
chat, whatever the caller says. Replies pass automatically, because a reply means they
messaged first. This was implemented as a flag once and removed; do not reintroduce it.

**Gate 3 is not satisfied by membership.** Being in `staff` or `customers` is Gate 2. The
first backfill seeded `inbound_history` from those tables and thereby marked staff who had
never texted the bot as reachable — recreating the exact risk the gate was added to
prevent. It backfills from `wa_messages where direction='in'` only.

Check the live posture at any time:

```bash
./run.sh safety
```

It prints which gates are on, who can currently be messaged, and — most usefully — who is
related but **not** reachable because they never messaged in.

## Onboarding sequence

1. Pharmacy handset (dedicated SIM) messages the platform bot: `REGISTER`.
2. Bot collects name/address/owner conversationally. `wa_jid` is taken **from the sender**,
   never typed.
3. Backend calls GOWA `/app/login-with-code` for that number → WhatsApp returns an
   8-character code.
4. Bot replies **into that same chat** with the code plus: *WhatsApp → Linked Devices →
   Link with phone number instead*.
5. Human enters it on that handset. GOWA reports the session up.
6. Backend matches the JID to the pending row, binds `gowa_device_id`, sets it active.
7. Owner messages the **pharmacy number** with their owner code → `staff` row, phone taken
   from the sender. This also establishes the chat, which is what makes later alerts legal.
8. Staff are given a join code **out of band** (spoken, SMS) and message the pharmacy
   number with `JOIN <code>`. The bot never messages staff first.

The QR flow works too but the code flow is better here: a WhatsApp QR expires in under a
minute and cannot survive being relayed to another phone as an image.

## Not a credential

A PPB number is a **public register entry**. It is recorded as a claim, never accepted as
proof of identity — otherwise anyone who can look up a pharmacist could award themselves
authority to approve prescription-only medicines, and every approval in the audit log
would prove only that somebody typed a public number. Authority comes from an invitation
issued by someone who already holds it.

## Note on the wider design doc

The architecture note this file summarises contains an illustrative schema (`SERIAL` ids,
`inbound_records`, an `inventory` table). That is **not** this codebase: ids are `uuid`, the
table is `inbound_history`, and stock lives in `products` + `batches`. Treat that section as
prose, not as DDL to apply.
