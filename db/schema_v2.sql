-- ============================================================
-- DISHII schema v2 — additive migration. Safe to run on top of v1.
--
-- What this adds and why:
--   1. AGENT PROTOCOL. The pharmacy PC runs an agent. It polls us; we never
--      connect to it. No inbound ports on their router, works behind NAT.
--   2. POS INGESTION. phAMACore stays the till. It sells stock we received.
--      Without ingesting its sales, our stock number drifts from reality
--      within a day. This is the single biggest correctness risk in the
--      whole "don't replace their system" strategy.
--   3. HISTORY BACKFILL. A CSV stock snapshot gives zero demand signal, so
--      avg_daily = 0 for every product and the system starts from amnesia
--      while the pharmacy has 24 months of signal sitting on that PC.
--   4. RECONCILIATION. Received minus sold vs what phAMACore says is on the
--      shelf. The gap is shrinkage, miscounts and theft. That gap is the
--      most valuable number this product will ever show an owner.
-- ============================================================

-- ---------- the agent on the pharmacy PC ----------
create table if not exists agents (
  id              uuid primary key default gen_random_uuid(),
  pharmacy_id     uuid not null references pharmacies(id) on delete cascade,
  enrolment_token text unique,               -- typed once at install
  agent_token     text unique,               -- issued at enrolment, used thereafter
  machine_name    text,
  agent_version   text,
  ingest_mode     text default 'unknown'
                  check (ingest_mode in ('unknown','db_poll','folder_watch','manual')),
  db_engine       text,                      -- firebird | mssql | access | sqlanywhere
  db_detail       jsonb,                     -- what the probe found
  last_seen_at    timestamptz,
  suspended       boolean default false,     -- kill switch
  created_at      timestamptz default now()
);
create index if not exists agents_pharmacy_idx on agents (pharmacy_id);

-- Command queue. WhatsApp -> cloud -> row here -> agent long-poll -> result.
-- This is how "ask the agent something from WhatsApp" works without an inbound port.
create table if not exists agent_commands (
  id           uuid primary key default gen_random_uuid(),
  agent_id     uuid not null references agents(id) on delete cascade,
  command      text not null,                -- probe | export_now | resync | full_backfill | ping
  args         jsonb default '{}',
  status       text not null default 'queued'
               check (status in ('queued','taken','done','error','expired')),
  result       jsonb,
  requested_by uuid references staff(id),
  reply_to     text,                          -- phone to WhatsApp the result to
  taken_at     timestamptz,
  done_at      timestamptz,
  created_at   timestamptz default now()
);
create index if not exists agent_commands_pending_idx
  on agent_commands (agent_id, status) where status = 'queued';

-- ---------- POS sales ingested from phAMACore ----------
-- Raw landing table. We keep the source row verbatim so a parsing bug is
-- always recoverable without going back to the pharmacy PC.
create table if not exists pos_sales (
  id            bigserial primary key,
  pharmacy_id   uuid not null references pharmacies(id) on delete cascade,
  source        text not null default 'phamacore',
  external_id   text,                         -- phAMACore sale/line id
  sold_at       timestamptz not null,
  legacy_code   text,
  description   text,
  qty_pieces    int not null,
  unit_price    numeric(12,4),
  line_total    numeric(14,2),
  payment_method text,
  product_id    uuid references products(id),
  batch_id      uuid references batches(id),  -- FEFO-guessed; POS has no batch
  applied       boolean default false,        -- has it hit stock_movements yet
  apply_error   text,
  raw           jsonb,
  created_at    timestamptz default now(),
  unique (pharmacy_id, source, external_id)   -- idempotency across retries
);
create index if not exists pos_sales_unapplied_idx
  on pos_sales (pharmacy_id, applied) where applied = false;
create index if not exists pos_sales_sold_at_idx on pos_sales (pharmacy_id, sold_at);

-- High-water marks, per agent per stream, so each poll reads only new rows.
create table if not exists sync_state (
  agent_id    uuid not null references agents(id) on delete cascade,
  stream      text not null,                  -- 'sales' | 'stock_snapshot' | 'history'
  last_id     text,
  last_ts     timestamptz,
  rows_seen   bigint default 0,
  updated_at  timestamptz default now(),
  primary key (agent_id, stream)
);

-- ---------- history backfill (kills the amnesia problem) ----------
-- Monthly totals per product, from phAMACore's own 12/24-month screens.
-- Deliberately separate from stock_movements: this is HISTORICAL AGGREGATE,
-- not ledger truth, and must never be mistaken for it.
create table if not exists sales_history_monthly (
  id           bigserial primary key,
  pharmacy_id  uuid not null references pharmacies(id) on delete cascade,
  product_id   uuid references products(id) on delete cascade,
  legacy_code  text,
  period       date not null,                 -- first day of the month
  qty_pieces   int not null default 0,
  value        numeric(14,2),
  source       text default 'phamacore_backfill',
  created_at   timestamptz default now(),
  unique (pharmacy_id, legacy_code, period)
);
create index if not exists shm_product_idx on sales_history_monthly (product_id, period);

-- ---------- reconciliation: the owner's favourite number ----------
create table if not exists stock_reconciliation (
  id             uuid primary key default gen_random_uuid(),
  pharmacy_id    uuid not null references pharmacies(id) on delete cascade,
  ran_at         timestamptz default now(),
  product_id     uuid references products(id),
  legacy_code    text,
  dishii_pieces  int,                          -- received - sold, per our ledger
  pos_pieces     int,                          -- what phAMACore says is on the shelf
  variance       int,                          -- pos - dishii
  variance_value numeric(14,2),
  status         text default 'open'
                 check (status in ('open','explained','written_off','ignored')),
  note           text,
  resolved_by    uuid references staff(id),
  resolved_at    timestamptz
);
create index if not exists recon_open_idx
  on stock_reconciliation (pharmacy_id, ran_at desc) where status = 'open';

-- ---------- forecasting output (cached, explainable, not a black box) ----------
create table if not exists demand_forecast (
  product_id      uuid primary key references products(id) on delete cascade,
  pharmacy_id     uuid not null references pharmacies(id) on delete cascade,
  avg_daily       numeric(10,3),               -- blended baseline
  season_index    numeric(6,3) default 1.0,    -- this month vs annual average
  forecast_30d    int,
  days_of_cover   numeric(8,1),
  confidence      text default 'low'           -- low | medium | high
                  check (confidence in ('low','medium','high')),
  method          text,                        -- human-readable: how we got the number
  computed_at     timestamptz default now()
);

-- ---------- duty roster: "manager of the day" ----------
create table if not exists duty_roster (
  id          uuid primary key default gen_random_uuid(),
  pharmacy_id uuid not null references pharmacies(id) on delete cascade,
  staff_id    uuid not null references staff(id) on delete cascade,
  weekday     int check (weekday between 0 and 6),   -- 0 = Monday
  on_date     date,                                   -- overrides weekday
  shift       text default 'day',
  unique nulls not distinct (pharmacy_id, staff_id, weekday, on_date, shift)
);

-- ---------- staff approval PIN ----------
-- WhatsApp approval is only attributable if a secret is required. A 4-digit PIN
-- is weak but it is not nothing, and it beats a dropdown.
alter table staff add column if not exists approval_pin text;
alter table staff add column if not exists pin_failed_count int default 0;
alter table staff add column if not exists pin_locked_until timestamptz;

-- ---------- payment via forwarded M-Pesa SMS (demo path) ----------
alter table payments add column if not exists source text default 'stk';
alter table payments add column if not exists sms_text text;
alter table payments add column if not exists verified_by uuid references staff(id);

-- ---------- multi-tenant hygiene on job_runs ----------
alter table job_runs add column if not exists pharmacy_id uuid references pharmacies(id);

-- ---------- allow POS-ingested sales in the ledger ----------
-- v1's CHECK constraint rejects 'pos_sale', so every agent ingest would fail.
-- Kept as a distinct reason from 'sale' on purpose: 'sale' means Dishii took the
-- order, 'pos_sale' means phAMACore's till did. You need to tell them apart to
-- know which channel is growing.
alter table stock_movements drop constraint if exists stock_movements_reason_check;
alter table stock_movements add constraint stock_movements_reason_check
  check (reason in ('grn','sale','pos_sale','adjust','expiry_writeoff',
                    'return','transfer','opening','recon_adjust'));

-- ---------- views ----------
-- Blended demand baseline: recent live ledger sales where we have them,
-- backfilled monthly history where we don't. This is what makes forecasting
-- work on day one instead of day ninety.
create or replace view v_demand_baseline as
with live as (
  select p.id as product_id,
         -sum(m.delta_pieces)::numeric as pieces,
         greatest(extract(day from (now() - min(m.created_at)))::numeric, 1) as days
    from stock_movements m
    join batches b on b.id = m.batch_id
    join products p on p.id = b.product_id
   where m.reason in ('sale','pos_sale')
     and m.created_at > now() - interval '120 days'
   group by p.id
),
hist as (
  select product_id,
         sum(qty_pieces)::numeric as pieces,
         (count(*) * 30.0) as days
    from sales_history_monthly
   where period > current_date - interval '12 months'
     and product_id is not null
   group by product_id
)
select p.id as product_id,
       p.pharmacy_id,
       p.name,
       coalesce(l.pieces / nullif(l.days,0), h.pieces / nullif(h.days,0), 0) as avg_daily,
       case
         when l.days >= 60 then 'high'
         when l.days >= 21 or h.days >= 180 then 'medium'
         else 'low'
       end as confidence,
       case
         when l.days >= 21 then 'live sales (' || round(l.days) || ' days observed)'
         when h.days > 0   then 'phAMACore history (' || round(h.days/30) || ' months)'
         else 'no demand signal yet'
       end as method
  from products p
  left join live l on l.product_id = p.id
  left join hist h on h.product_id = p.id;

-- Month-of-year seasonality from backfilled history. This is how you catch
-- "docs prescribe X every August and then nobody wants it".
create or replace view v_seasonality as
with monthly as (
  select product_id,
         extract(month from period)::int as mo,
         avg(qty_pieces)::numeric as avg_qty
    from sales_history_monthly
   where product_id is not null
   group by product_id, extract(month from period)
),
annual as (
  select product_id, avg(avg_qty) as overall
    from monthly group by product_id
)
select m.product_id, m.mo,
       round(m.avg_qty / nullif(a.overall, 0), 3) as season_index,
       m.avg_qty
  from monthly m join annual a on a.product_id = m.product_id;

-- The reconciliation the owner actually wants to see
create or replace view v_stock_variance as
select r.pharmacy_id, p.name, r.legacy_code, r.dishii_pieces, r.pos_pieces,
       r.variance, r.variance_value, r.status, r.ran_at
  from stock_reconciliation r
  left join products p on p.id = r.product_id
 where r.status = 'open' and r.variance <> 0
 order by abs(r.variance_value) desc nulls last;

alter table agents               enable row level security;
alter table agent_commands       enable row level security;
alter table pos_sales            enable row level security;
alter table sales_history_monthly enable row level security;
alter table stock_reconciliation enable row level security;
alter table demand_forecast      enable row level security;
alter table duty_roster          enable row level security;
