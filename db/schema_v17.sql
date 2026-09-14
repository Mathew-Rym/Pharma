-- v17 — stockout_log: demand signals from patient inquiries
--
-- The restock loop had a blind spot: a customer asking for a medicine the pharmacy
-- does not stock produced a polite "not found" and nothing else. Fifty customers can
-- ask for the same thing over a month and the reorder engine never hears about it,
-- because it reads SALES (things that moved), not DEMAND (things people wanted).
--
-- stockout_log records every out-of-stock inquiry as a demand signal. Aggregated
-- counts (7-day window) drive staff alerts and small-batch draft POs — never an
-- automatic order: the existing OKPO PIN approval stays the only path money moves.
--
-- phone is stored raw (not hashed): notify-on-restock needs to message the customer
-- back, and the number already lives in inbound_history/wa_messages for this tenant
-- under the same DPA consent. Hashing here would create a promise we cannot keep.

create table if not exists stockout_log (
    id            bigserial primary key,
    pharmacy_id   uuid not null references pharmacies(id) on delete cascade,
    product_id    uuid references products(id) on delete set null,
    product_query text not null,              -- what the customer actually typed
    phone         text not null,
    language      text not null default 'en',
    wants_notify  boolean not null default false,
    notified_at   timestamptz,
    triggered_at  timestamptz,                -- set on rows when a staff alert/PO fired
    created_at    timestamptz not null default now()
);

create index if not exists stockout_log_product
    on stockout_log (pharmacy_id, product_id, created_at desc);
create index if not exists stockout_log_pending_notify
    on stockout_log (pharmacy_id, product_id)
    where wants_notify and notified_at is null;

alter table stockout_log enable row level security;
do $$
begin
  if not exists (select 1 from pg_policies
                  where tablename = 'stockout_log' and policyname = 'tenant_isolation') then
    create policy tenant_isolation on stockout_log
      using (pharmacy_id = current_pharmacy())
      with check (pharmacy_id = current_pharmacy());
  end if;
end $$;
