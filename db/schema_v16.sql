-- v16 — tenant-aware conversation state + outbound message tracking + inbound_history
--
-- wa_state was keyed on phone alone: the same person messaging two pharmacies saw one
-- pharmacy's conversation state from the other's session. The composite (pharmacy_id, phone)
-- key guarantees isolation.
--
-- wa_messages gains gowa_device_id so outbound rate-limiting (safety.py Gate 4) can work
-- per-device rather than per-pharmacy. Also adds a `status` column to track delivery
-- outcomes (sent, refused, failed) instead of swallowing them.
--
-- inbound_history is the table that safety.py Gate 3 reads to decide whether a phone has
-- messaged this pharmacy before. Created here so gate 3 works on a fresh database.

-- ---- wa_state: phone-only → (pharmacy_id, phone) ----
-- Drop the old primary key (phone alone), then create the composite one.
-- Idempotent: the DO block checks which constraint exists before touching anything.
do $$
declare
  existing_pk text;
begin
  select conname into existing_pk
    from pg_constraint
   where conrelid = 'wa_state'::regclass
     and contype = 'p';

  -- If the PK is the old phone-only one, migrate.
  -- The new key is (pharmacy_id, phone).
  if existing_pk is not null then
    -- Check if it's the old single-column PK
    if (select count(*) from information_schema.key_column_usage
         where table_name = 'wa_state'
           and constraint_name = existing_pk) = 1 then
      -- Backfill: rows with null pharmacy_id cannot be part of a composite key.
      -- Delete them — they are expired state from before tenancy existed.
      delete from wa_state where pharmacy_id is null;

      -- Drop old PK and add composite one
      execute format('alter table wa_state drop constraint %I', existing_pk);
      alter table wa_state alter column pharmacy_id set not null;
      alter table wa_state add primary key (pharmacy_id, phone);
    end if;
  end if;
end $$;

-- ---- wa_messages: track outbound device and delivery status ----
alter table wa_messages add column if not exists gowa_device_id text;
alter table wa_messages add column if not exists status text;

-- Index for rate-limiting queries in safety.py
create index if not exists wa_messages_device_rate
  on wa_messages (gowa_device_id, direction, created_at desc)
  where direction = 'out';

-- ---- inbound_history: Gate 3 foundation ----
create table if not exists inbound_history (
  id            bigserial primary key,
  pharmacy_id   uuid not null references pharmacies(id) on delete cascade,
  phone         text not null,
  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),
  message_count int not null default 1,
  unique (pharmacy_id, phone)
);

create index if not exists inbound_history_phone
  on inbound_history (phone);

alter table inbound_history enable row level security;
do $$
begin
  if not exists (select 1 from pg_policies
                  where tablename = 'inbound_history' and policyname = 'tenant_isolation') then
    create policy tenant_isolation on inbound_history
      using (pharmacy_id = current_pharmacy());
  end if;
end $$;

-- ---- pharmacies: platform vs tenant distinction ----
alter table pharmacies add column if not exists kind text not null default 'tenant';
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'pharmacies_kind_chk') then
    alter table pharmacies add constraint pharmacies_kind_chk
      check (kind in ('platform', 'tenant'));
  end if;
end $$;

-- GOWA device fields for multi-device routing
alter table pharmacies add column if not exists wa_jid text;
alter table pharmacies add column if not exists gowa_device_id text;

create unique index if not exists pharmacies_wa_jid_uniq
  on pharmacies (wa_jid) where wa_jid is not null;
create unique index if not exists pharmacies_gowa_device_uniq
  on pharmacies (gowa_device_id) where gowa_device_id is not null;
