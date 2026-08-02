# WhatsApp: which numbers, who pairs, and why nothing gets banned

The rules below have been re-derived several times from scratch, and one anti-pattern has
been proposed four separate times. Written down so it stops happening.

## The distinction everything rests on

| Capability | Requires |
| --- | --- |
| A number can **send** to the bot | Nothing. Any WhatsApp user can. |
| The system can **receive and reply** on a number | A GOWA session for that number, created by a Linked Devices action **on that handset**. |

**Anti-pattern, proposed four times:** a `CONNECT <code>` / `ACTIVATE <code>` message arriving
on the platform device **cannot** create a session for a different number. Only the
physical handset can pair itself. Chat can relay a link code; it cannot do the linking.

## Pairing vs linking

| | What it is | Who |
| --- | --- | --- |
| **Pairing** | Giving GOWA control of a WhatsApp account, via QR or an 8-character link code. | Bots only. One session per pharmacy shopfront. |
| **Linking** | Storing a phone number in the database so the system knows which pharmacy it belongs to. | Every human. |

**Never pair a personal phone.** GOWA would carry that person's private conversations, and
a ban would take their own communications down with it.

**Never pair a pharmacy's existing advertised number.** It is their most valuable asset and
an unofficial client can lose it. Issue a dedicated SIM and let them advertise it
gradually.

## Who sends where

| Person | Sends from | Sends to | Paired? |
| --- | --- | --- | --- |
| Customer | their own phone | pharmacy shopfront number | never |
| Staff / pharmacist / manager | their own phone | pharmacy shopfront number | never |
| Owner (reports, alerts) | their own phone | pharmacy shopfront number | never |
| Owner / pharmacy handset (registration) | the pharmacy SIM | platform bot | the SIM, yes |

A WhatsApp account cannot message itself, which is why the owner's phone must be separate
from the pharmacy number — otherwise the bot could never send them a report.

## Who a number acts for, and who may be replied to

Two different questions, two different answers, read off the same tables. Confusing them
cost a real bug.

| | `tenancy.resolve_by_sender()` | `safety.has_relationship()` (Gate 2) |
| --- | --- | --- |
| Asks | which pharmacy does this number **act for**? | may we **reply** to this number? |
| Reads | staff (active), suppliers | customers, staff, suppliers, own number, onboarding_contacts |
| Customers count? | **no** | **yes** |

**Customers are deliberately not an identity signal.** Shopping at a pharmacy is not acting
for it, and it is many-to-many by nature — someone who buys from three chemists is ordinary,
not ambiguous. Reading `customers` in the resolver was a category error with a concrete
cost: a stranger's *first* message auto-creates a customers row (before consent), so a
pharmacy owner who registered through a host line and later sent that line any ordinary
message resolved to **two** pharmacies and was answered *"you're registered at more than one
pharmacy, which one?"* from then on. Filtering on `consent_given` does not fix it — the
collision just moves from "texted once" to "consented at two", which is normal behaviour.

Customer traffic does not need the resolver: the device the message arrived on names the
tenant, and that is the stronger signal because the sender cannot spoof it.

**The consequence, stated rather than discovered later:** a customer-only number whose
`device_id` fails to resolve now falls to `_greet_unknown` and gets **no reply**. That is
fail-closed and intended. Today it affects nobody — every inbound arrives on a bound tenant
device.

**This is armed, not dormant.** While every device resolves to a tenant, `resolve_by_sender`
barely runs. The moment a dedicated platform SIM is paired and `./run.sh platform` applied,
that device resolves to `kind='platform'` — not a tenant — so `pharmacy_id` is left unset and
sender resolution becomes the **primary** path for every host-line message.

**`suppliers` has the same many-to-many shape and is knowingly left in.** One distributor
across twenty pharmacies is twenty rows, so a distributor texting an unbound line resolves to
twenty candidates and the caller asks. Asking is the correct degraded behaviour; guessing is
not. Not fixed — recorded so it is not rediscovered at scale.

## The single grey tick — SUSPECTED cause, not established

Senders see one grey tick and it never advances. The message does arrive: the bot receives
it over the multi-device protocol and replies within seconds, with the reply recorded
`status='sent'`.

**Hypothesis, untested:** `WHATSAPP_PRESENCE_ON_CONNECT=unavailable` (what the container
actually runs today) means the account presents as offline, and an offline account returns
no delivery receipt.

**The evidence is one correlation**, and it is thin:

```text
11:06:49  [PRESENCE_PULSE] marked pharmacy-1 as unavailable
11:07:13  a real inbound arrives, 24 seconds later — one tick
```

**What does not fit:** `WHATSAPP_AUTO_MARK_READ=true` is also set. If receipts were flowing
at all we would expect *blue* ticks, not double. A single grey tick means no receipt of any
kind is being sent — consistent with the presence theory, and equally consistent with
something else being wrong. Nobody has isolated it.

`run.sh` and `docker-compose.yml` have been changed to `available`, but **that change is not
in effect** — it needs the container recreated, and recreating it is currently the last
thing worth doing (see below). Do not read this section as describing running behaviour.

Cheapest next test, if GOWA exposes presence at runtime rather than only at connect:

```bash
curl -s -u "$GOWA_USER:$GOWA_PASS" -H "X-Device-Id: pharmacy-1" \
  -X POST http://localhost:3001/send/presence -d '{"type":"available"}'
```

Ten seconds, no restart, no re-pairing. If that endpoint does not exist, the question waits
until the container is restarted for some other reason.

## Re-pairing is not free

WhatsApp error **463, "reach-out timelock"** — its own server-side restriction on starting
new chats — says in its own text that *"newly-linked or low-activity numbers are affected
most"*, and advises *"ask the recipient to message you first."* That is Gate 3, arrived at
independently by WhatsApp.

On 2 August 254777602338 was paired, remote-logged-out, and re-paired within one hour. Then
an agent test-send to a **fabricated** number triggered 463, and the device was logged out
one second later:

```text
11:07:54  WhatsApp rejected this send with error 463 (reach-out timelock)
11:07:55  [REMOTE_LOGOUT] Received LoggedOut event for device pharmacy-1
```

The account was then blocked for five hours.

Two rules follow, and they are not stylistic:

* **Never send to a number that has not messaged first.** Not a real one, and especially not
  an invented one — messaging numbers that do not exist is the clearest spam signal there
  is. The gates enforce this; do not hand-craft webhooks that route around them.
* **Treat every link event as expensive.** Each pair/unpair cycle raises the profile of a
  number that WhatsApp already considers new. Do not recreate the container to change a
  setting unless the setting is worth the pairing.

`WA_ALLOWLIST` is the mechanical backstop for the first rule: with it set, Gate 1 refuses
every recipient not named, before anything reaches GOWA. Keep it set during development and
clear it only when serving real customers.

## The four gates, on every send

Implemented in `api/safety.py`, called from `wa.compose()` before the row is written.

| Gate | Rule | Where |
| --- | --- | --- |
| 1 · Allowlist | when `WA_ALLOWLIST` is set, only those numbers can receive | dev, test, staging |
| 2 · Relationship | recipient must be a customer, staff or supplier **of the sending tenant**, or be mid-registration with it | everywhere |
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

Implemented in `api/register.py`, hooked into the router **before** tenant resolution —
everything below comes from someone the resolver cannot place.

1. The owner, from **any** phone, messages the platform line: `REGISTER`.
2. Five questions, asked deterministically: their name, the pharmacy name, the PPB premises
   licence, the town, and **which handset the shop will use**. Then a summary and `YES`.
3. `YES` writes the pharmacy (`status='pending_activation'`, `kind='tenant'`) and the
   owner's `staff` row. `wa_jid` is left **null**.
4. Backend calls GOWA `/app/login-with-code?phone=<the shop handset>` → WhatsApp returns an
   8-character code. The bot relays it with: *WhatsApp → Linked Devices → Link with phone
   number instead*. `CODE` reissues it; they expire in minutes.
5. Someone types it on the shop handset. GOWA reports the session up.
6. `activation_sweep` (`./run.sh activate`, or cron) reads the JID back from GOWA, binds
   it, sets the pharmacy `active`, and tells the owner. It **refuses** a slot that linked
   to a different number than the one registered.
7. Managers text `OWNER <code>`, attendants text `JOIN <code>`, to the pharmacy's number.
   Each creates a `staff` row with the phone taken from the sender, and establishes the
   chat — which is what makes later alerts legal. The bot never messages staff first.

**Why the handset is asked for rather than inferred.** The tempting shortcut is to treat
whoever sent `REGISTER` as the handset, which makes `wa_jid` free and correct by
construction. It is only correct if the shop phone is the one texting — and when it is not,
the owner's personal WhatsApp silently becomes the pharmacy bot: every customer message
lands in their private chats, and a ban takes out their own messaging along with the shop's.
So it is a question, validated as a Kenyan mobile, and the JID is still read back from GOWA
at step 6 rather than trusted from step 2. A typo'd handset number is caught there, because
the slot links to a number that does not match what was registered.

**Why registration never calls the model.** The conversation is a pure function
(`register._step`) with no LLM, no network and no database. Slot-filling with Gemini reads
better in a demo and fails badly in production: a pharmacy that cannot sign up because a
model is rate-limited is revenue that never arrives, and a model that helpfully infers a
licence number puts fiction in a regulated field.

**Replying to a stranger.** This is the one flow where a phone with no relationship gets an
answer, so Gate 2 needs an exception. It is a row in `onboarding_contacts` — scoped to the
one pharmacy that is answering, and expiring after 24 hours. Not a `customers` row: that
table is read by `tenancy.resolve_by_sender()`, so the owner would stay pinned to the
answering pharmacy forever and every later message of theirs would come back "you're
registered at more than one pharmacy, which one?".

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
