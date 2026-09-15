-- ============================================================================
-- schema_v18: price management + owner briefings
--
-- Two tables:
--
-- price_history: every selling-price change, who made it and when. Price changes
--   are money actions, so they get the same auditability as stock movements and
--   pharmacist approvals: "who set this price, when, from what, to what".
--
-- pharmacy_settings: per-tenant operational preferences. The briefing jobs read
--   their enable flags and delivery hours from here, so a pharmacy can turn the
--   midday check-in off without a redeploy. One row per pharmacy, created lazily
--   by the API (and by the defaults below) -- absent row means "all defaults on",
--   which is why every read coalesces.
-- ============================================================================

create table if not exists price_history (
  id            uuid primary key default gen_random_uuid(),
  pharmacy_id   uuid not null references pharmacies(id) on delete cascade,
  product_id    uuid not null references products(id),
  old_price     numeric(12,4),
  new_price     numeric(12,4) not null,
  actor_staff   uuid references staff(id),
  actor_phone   text,
  source        text not null default 'whatsapp',   -- whatsapp | dashboard | grn | agent
  created_at    timestamptz not null default now()
);
create index if not exists price_history_product_idx
  on price_history (pharmacy_id, product_id, created_at desc);
create index if not exists price_history_day_idx
  on price_history (pharmacy_id, created_at desc);

create table if not exists pharmacy_settings (
  pharmacy_id           uuid primary key references pharmacies(id) on delete cascade,
  -- Briefing switches. Default ON for morning/evening; the afternoon pulse is a
  -- check-in, so it is ON too but the job itself stays quiet when nothing changed.
  briefing_morning      boolean not null default true,
  briefing_afternoon    boolean not null default true,
  briefing_evening      boolean not null default true,
  -- Delivery hours in the pharmacy's LOCAL time (EAT unless the row says
  -- otherwise). The jobs translate local -> UTC when deciding to run.
  briefing_hour_morning   int not null default 7,
  briefing_hour_afternoon int not null default 13,
  briefing_hour_evening   int not null default 20,
  updated_at timestamptz not null default now()
);

-- RLS, consistent with v7: same current_pharmacy() helper (null when unset, which
-- denies everything -- the right failure direction), same tenant_isolation shape.
alter table price_history     enable row level security;
alter table pharmacy_settings enable row level security;

drop policy if exists tenant_isolation on price_history;
create policy tenant_isolation on price_history
  using      (pharmacy_id = current_pharmacy())
  with check (pharmacy_id = current_pharmacy());

drop policy if exists tenant_isolation on pharmacy_settings;
create policy tenant_isolation on pharmacy_settings
  using      (pharmacy_id = current_pharmacy())
  with check (pharmacy_id = current_pharmacy());
