-- v10 — inbound history for WhatsApp anti-ban safety gates
--
-- Every outbound message now checks whether the recipient has ever messaged this
-- pharmacy. Without that proof of an existing conversation, WhatsApp considers
-- the message unsolicited, and enough of those get the number banned.
--
-- This table is the ground truth for "has this phone ever messaged us?". It is
-- updated on every inbound message (router.py) and checked at compose time (wa.py).

create table if not exists inbound_history (
    id             uuid primary key default gen_random_uuid(),
    pharmacy_id    uuid not null references pharmacies(id) on delete cascade,
    phone          text not null,
    first_seen_at  timestamptz default now(),
    last_seen_at   timestamptz default now(),
    message_count  integer default 1,
    unique(pharmacy_id, phone)
);

create index if not exists idx_inbound_history_pharmacy_phone
    on inbound_history(pharmacy_id, phone);

-- RLS *with* a policy. Enabling it and stopping there is the exact mistake AUTH.md
-- documents: it reads as locked in the Supabase UI and enforces nothing, because the app
-- connects as a role with BYPASSRLS. It would also fail
-- test_policies_exist_on_every_tenant_table, which queries v_rls_coverage rather than a
-- hand-maintained list precisely so a new table cannot be forgotten.
alter table inbound_history enable row level security;
drop policy if exists tenant_isolation on inbound_history;
create policy tenant_isolation on inbound_history
  using      (pharmacy_id = current_pharmacy())
  with check (pharmacy_id = current_pharmacy());

-- Backfill from EVIDENCE, not from membership.
--
-- The first version of this seeded from customers, staff and suppliers, reasoning that
-- those people are "known to the pharmacy". That conflates the two gates and fabricates
-- consent. Membership is Gate 2 (relationship). Gate 3 asks a different question -- did
-- this person open a conversation with us? -- and a manager typing a colleague's number
-- is not that colleague messaging in.
--
-- The effect was concrete: staff numbers that had never texted the bot were marked as
-- having inbound history, so the system would freely cold-message them. That is the
-- pattern that gets a WhatsApp number reported, i.e. the backfill re-created the exact
-- risk the gate was added to remove.
--
-- wa_messages is the only record of who actually spoke to us first.
insert into inbound_history (pharmacy_id, phone, first_seen_at, last_seen_at, message_count)
select pharmacy_id, from_phone, min(created_at), max(created_at), count(*)
  from wa_messages
 where direction = 'in'
   and from_phone is not null and from_phone <> ''
   and pharmacy_id is not null
 group by pharmacy_id, from_phone
on conflict (pharmacy_id, phone) do nothing;
