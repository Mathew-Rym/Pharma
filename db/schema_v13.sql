-- v13 — onboarding: pharmacy lifecycle and the codes that link humans to it
--
-- REGISTER creates a pharmacy that does not yet have a paired handset, so it needs a
-- lifecycle: registered but not yet reachable, versus live. Without it, an unpaired
-- pharmacy is indistinguishable from a broken one.

alter table pharmacies add column if not exists status text not null default 'active';
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'pharmacies_status_chk') then
    alter table pharmacies add constraint pharmacies_status_chk
      check (status in ('pending_activation', 'active', 'suspended'));
  end if;
end $$;

alter table pharmacies add column if not exists address text;

-- The phone that registered this pharmacy. Recorded so a half-finished registration can be
-- resumed and so we know who to tell when pairing completes. NOT the same as wa_jid: the
-- registering phone is a HUMAN's personal number, while wa_jid is the pharmacy handset's.
-- Conflating the two would make GOWA carry the owner's private conversations, and a ban
-- would take down their own messaging.
alter table pharmacies add column if not exists owner_phone text;

-- Handed out by the owner, out of band, for staff to redeem with `JOIN <code>`. The bot
-- never messages staff first, so this code is what lets them start the conversation
-- themselves -- which is also what opens the anti-ban chat-established gate.
alter table pharmacies add column if not exists join_code text;
create unique index if not exists pharmacies_join_code_uniq
  on pharmacies (join_code) where join_code is not null;

-- Two codes, not one, because the roles they grant are not interchangeable. JOIN makes an
-- attendant; OWNER makes a manager, who can approve purchase orders and see the money.
-- One code plus "tell me your role" would let whoever holds it promote themselves.
alter table pharmacies add column if not exists owner_code text;
create unique index if not exists pharmacies_owner_code_uniq
  on pharmacies (owner_code) where owner_code is not null;

-- Existing rows predate the lifecycle: a pharmacy with a paired device is live, one
-- without has never been reachable.
update pharmacies
   set status = case when gowa_device_id is not null then 'active'
                     else 'pending_activation' end
 where status = 'active' and gowa_device_id is null;

-- Find pharmacies waiting for their handset to finish pairing, without a full scan.
create index if not exists pharmacies_pending_activation
  on pharmacies (status) where status = 'pending_activation';


-- Someone mid-registration is not a customer, and must not become one.
--
-- Replying to a stranger needs a Gate 2 relationship, and the obvious shortcut is to give
-- them a `customers` row on the line that is answering. That is wrong in two ways, both
-- found by driving the real flow:
--
--   * tenancy.resolve_by_sender() reads customers, so the row is permanent. Every message
--     the owner ever sends afterwards resolves to two pharmacies and gets answered with
--     "which one?" instead of an answer.
--   * When the answering line is a tenant rather than a dedicated platform number, that
--     pharmacy's customer list silently fills with people who were registering a
--     different pharmacy entirely.
--
-- So onboarding contacts live in their own table. Gate 2 consults it; nothing else does.
-- It grants permission to reply, not membership of anything.
create table if not exists onboarding_contacts (
  pharmacy_id  uuid not null references pharmacies(id) on delete cascade,
  phone        text not null,
  created_at   timestamptz not null default now(),
  primary key (pharmacy_id, phone)
);

-- Time-bounded on purpose. An abandoned registration from months ago should not leave a
-- number permanently messageable; the queries that read this apply a window, and the index
-- is what keeps that cheap.
create index if not exists onboarding_contacts_recent
  on onboarding_contacts (phone, created_at desc);

alter table onboarding_contacts enable row level security;
do $$
begin
  if not exists (select 1 from pg_policies
                  where tablename = 'onboarding_contacts' and policyname = 'tenant_isolation') then
    create policy tenant_isolation on onboarding_contacts
      using (pharmacy_id = current_pharmacy());
  end if;
end $$;
