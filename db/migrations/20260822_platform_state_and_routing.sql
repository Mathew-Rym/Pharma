begin;

-- A phone may legitimately have simultaneous conversations with different pharmacies.
-- Replace the legacy phone-only key with tenant-scoped identity.
alter table public.wa_state drop constraint if exists wa_state_pkey;
alter table public.wa_state alter column pharmacy_id set not null;
alter table public.wa_state add primary key (pharmacy_id, phone);

-- Make inbound/outbound routing diagnosable without changing existing application reads.
alter table public.wa_messages
    add column if not exists routing_kind text,
    add column if not exists processing_stage text;

create index if not exists wa_messages_device_created_idx
    on public.wa_messages (gowa_device_id, created_at desc);

create index if not exists wa_messages_stage_created_idx
    on public.wa_messages (processing_stage, created_at desc);

commit;
