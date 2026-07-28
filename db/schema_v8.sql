-- v8 — tenant resolution by WhatsApp identity
--
-- Until now the running process was bound to one pharmacy by nine module-level
-- constants reading settings.PHARMACY_ID at import. These columns are what let a single
-- process serve several pharmacies: the inbound webhook already carries the JID of the
-- receiving account, so tenancy becomes a lookup instead of a boot-time constant.
--
-- See docs/superpowers/specs/2026-07-28-multi-tenant-resolution-design.md

-- The JID exactly as GOWA reports it, e.g. 254777602338@s.whatsapp.net.
-- Stored in full rather than as a bare number: this value is compared against GOWA's
-- /devices output on every send, and normalising on each comparison is a bug surface.
alter table pharmacies add column if not exists wa_jid text;

-- The GOWA device slot label, e.g. pharmacy-a. Needed because outbound is addressed by
-- X-Device-Id -- unlike inbound, which binds on the JID above. Nullable: a pharmacy row
-- exists before its handset is paired.
alter table pharmacies add column if not exists gowa_device_id text;

-- 'tenant' = a real pharmacy. 'platform' = the onboarding/registration line, which has no
-- inventory and no customers. Modelled as a row rather than a hardcoded device id, so one
-- lookup yields three outcomes -- tenant, platform, or unknown -- with no second code path
-- and no fifth constant to replace the nine being removed.
alter table pharmacies add column if not exists kind text not null default 'tenant';

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'pharmacies_kind_chk') then
    alter table pharmacies
      add constraint pharmacies_kind_chk check (kind in ('tenant', 'platform'));
  end if;
end $$;

-- PARTIAL unique indexes, not UNIQUE NOT NULL columns. Registration inserts a pharmacy
-- before it is paired, so both columns must accept null while still refusing duplicates
-- among the rows that are bound. A plain UNIQUE NOT NULL here makes the first insert of a
-- new pharmacy fail outright.
create unique index if not exists pharmacies_wa_jid_uniq
  on pharmacies (wa_jid) where wa_jid is not null;

create unique index if not exists pharmacies_gowa_device_id_uniq
  on pharmacies (gowa_device_id) where gowa_device_id is not null;

-- Deliberately NOT backfilled here.
--
-- The obvious move is `set wa_jid = wa_number || '@s.whatsapp.net'`, and it would have
-- been wrong: wa_number on the existing row was the placeholder 254712345678 while the
-- paired handset is 254777602338. That backfill would have bound the only tenant to a
-- number that does not exist, so no inbound message could resolve and the working system
-- would go dark the moment the resolver shipped.
--
-- Binding values come from GOWA's own /devices output. See REVERT.md.
