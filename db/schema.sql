-- ============================================================
-- PHARMA OS PHARMACY OS — Supabase / Postgres schema (MVP)
-- Design rules:
--   1. Stock truth lives in BATCHES (qty in pieces) + an append-only
--      MOVEMENTS ledger. Never UPDATE a quantity without a movement row.
--   2. Every AI-extracted field carries a confidence score + a link to
--      the source image. Audit trail is the product, not a nice-to-have.
--   3. Quantities are stored in PIECES internally. phAMACore's "5W0P"
--      (5 whole packs, 0 pieces) is a display format only.
-- ============================================================

create extension if not exists "pgcrypto";
create extension if not exists "pg_trgm";   -- fuzzy match on drug names

-- ---------- tenancy & people ----------
create table pharmacies (
  id            uuid primary key default gen_random_uuid(),
  name          text not null,
  ppb_licence   text,
  mpesa_paybill text,
  wa_number     text,                        -- the pharmacy's Pharma OS WhatsApp line
  timezone      text default 'Africa/Nairobi',
  created_at    timestamptz default now()
);

-- Phone number IS the identity. No passwords for staff on WhatsApp.
create table staff (
  id           uuid primary key default gen_random_uuid(),
  pharmacy_id  uuid not null references pharmacies(id) on delete cascade,
  phone        text not null unique,         -- E.164, e.g. 254713755274
  name         text not null,
  role         text not null check (role in ('owner','manager','pharmacist','attendant')),
  ppb_reg_no   text,                         -- required for role='pharmacist'
  is_active    boolean default true,
  created_at   timestamptz default now()
);
create index on staff (pharmacy_id, is_active);

-- ---------- catalogue ----------
create table suppliers (
  id           uuid primary key default gen_random_uuid(),
  pharmacy_id  uuid not null references pharmacies(id) on delete cascade,
  code         text,                          -- e.g. SUP567
  name         text not null,
  phone        text,
  alt_phone    text,
  email        text,
  address      text,
  rep_name     text,                          -- "Vivian" — the knowledge that lives in a phone today
  mpesa_paybill text,
  lead_time_days int default 2,
  notes        text,
  created_at   timestamptz default now()
);
create index on suppliers (pharmacy_id);
create index on suppliers using gin (name gin_trgm_ops);

create table products (
  id             uuid primary key default gen_random_uuid(),
  pharmacy_id    uuid not null references pharmacies(id) on delete cascade,
  legacy_code    text,                         -- phAMACore code: TABS0292, A11675
  name           text not null,                -- "PRENOR 25/5MG TABS 30S"
  generic_name   text,
  form           text,                         -- tabs, syrup, inj, cream
  strength       text,
  pack_size      int not null default 1,       -- pieces per whole pack (30s -> 30)
  is_prescription_only boolean default false,  -- POM flag: gates auto-quoting
  reorder_level_pieces int default 0,
  cost_price     numeric(12,4),
  sell_price     numeric(12,4),
  vat_rate       numeric(5,2) default 0,
  preferred_supplier_id uuid references suppliers(id),
  created_at     timestamptz default now(),
  unique (pharmacy_id, legacy_code)
);
create index on products using gin (name gin_trgm_ops);
create index on products using gin (generic_name gin_trgm_ops);

-- ---------- stock ----------
create table batches (
  id             uuid primary key default gen_random_uuid(),
  pharmacy_id    uuid not null references pharmacies(id) on delete cascade,
  product_id     uuid not null references products(id) on delete cascade,
  batch_no       text,                         -- ST26-0439, K2671
  expiry_date    date,                         -- normalised from 01/2028 -> 2028-01-31
  qty_pieces     int not null default 0,
  cost_price     numeric(12,4),
  grn_id         uuid,                         -- provenance
  source_image   text,                         -- Supabase Storage path
  confidence     numeric(4,3),                 -- AI extraction confidence 0..1
  verified_by    uuid references staff(id),    -- human who signed off
  verified_at    timestamptz,
  created_at     timestamptz default now(),
  unique (pharmacy_id, product_id, batch_no, expiry_date)
);
create index on batches (pharmacy_id, expiry_date) where qty_pieces > 0;
create index on batches (product_id) where qty_pieces > 0;

-- Append-only ledger. Sum of movements per batch must equal batches.qty_pieces.
create table stock_movements (
  id           bigserial primary key,
  pharmacy_id  uuid not null references pharmacies(id) on delete cascade,
  batch_id     uuid not null references batches(id) on delete cascade,
  delta_pieces int not null,                   -- +receive, -sale, +/-adjust
  reason       text not null check (reason in
                 ('grn','sale','adjust','expiry_writeoff','return','transfer','opening')),
  ref_table    text,
  ref_id       uuid,
  actor_staff  uuid references staff(id),
  note         text,
  created_at   timestamptz default now()
);
create index on stock_movements (pharmacy_id, created_at desc);
create index on stock_movements (batch_id);

-- ---------- goods receiving (the wedge) ----------
create table grns (                            -- Goods Received Note
  id            uuid primary key default gen_random_uuid(),
  pharmacy_id   uuid not null references pharmacies(id) on delete cascade,
  supplier_id   uuid references suppliers(id),
  invoice_no    text,                          -- APL12000627
  invoice_date  date,
  po_ref        text,                          -- POD00014766
  subtotal      numeric(14,2),
  vat_total     numeric(14,2),
  net_total     numeric(14,2),
  parsed_total  numeric(14,2),                 -- sum of our parsed lines
  status        text not null default 'parsing'
                check (status in ('parsing','needs_review','approved','rejected')),
  images        jsonb default '[]',            -- ["grn/uuid/p1.jpg", ...]
  raw_extract   jsonb,                         -- full model output, kept forever
  model         text,
  discrepancy_note text,
  approved_by   uuid references staff(id),
  approved_at   timestamptz,
  created_at    timestamptz default now()
);
create index on grns (pharmacy_id, status);

create table grn_lines (
  id            uuid primary key default gen_random_uuid(),
  grn_id        uuid not null references grns(id) on delete cascade,
  line_no       int,
  raw_code      text,
  raw_description text,
  product_id    uuid references products(id),  -- null = unmatched, needs human
  match_score   numeric(4,3),
  batch_no      text,
  expiry_date   date,
  qty_invoiced_pieces int,
  qty_counted_pieces  int,                     -- what staff physically counted
  unit_price    numeric(12,4),
  line_total    numeric(14,2),
  confidence    numeric(4,3),
  flags         text[] default '{}',            -- {missing_expiry,total_mismatch,unmatched_product,short_delivery}
  created_at    timestamptz default now()
);
create index on grn_lines (grn_id);

-- ---------- purchase orders (auto-generated, human-approved) ----------
create table purchase_orders (
  id           uuid primary key default gen_random_uuid(),
  pharmacy_id  uuid not null references pharmacies(id) on delete cascade,
  supplier_id  uuid not null references suppliers(id),
  po_no        text,
  status       text not null default 'draft'
               check (status in ('draft','awaiting_approval','sent','partially_received','received','cancelled')),
  reason       jsonb,                          -- {trigger:'reorder_level', forecast:{...}}
  total_estimate numeric(14,2),
  pdf_path     text,
  approved_by  uuid references staff(id),
  approved_at  timestamptz,
  sent_at      timestamptz,
  created_at   timestamptz default now()
);

create table po_lines (
  id          uuid primary key default gen_random_uuid(),
  po_id       uuid not null references purchase_orders(id) on delete cascade,
  product_id  uuid not null references products(id),
  qty_pieces  int not null,
  unit_cost   numeric(12,4),
  rationale   text                             -- "sold 2,840 in Jul; 769 left; 8d cover"
);

-- ---------- customers, prescriptions, orders ----------
create table customers (
  id            uuid primary key default gen_random_uuid(),
  pharmacy_id   uuid not null references pharmacies(id) on delete cascade,
  phone         text not null,
  name          text,
  consent_given boolean default false,          -- Kenya DPA 2019 — no marketing without this
  consent_at    timestamptz,
  marketing_opt_in boolean default false,
  loyalty_points int default 0,
  created_at    timestamptz default now(),
  unique (pharmacy_id, phone)
);

create table prescriptions (
  id             uuid primary key default gen_random_uuid(),
  pharmacy_id    uuid not null references pharmacies(id) on delete cascade,
  customer_id    uuid not null references customers(id) on delete cascade,
  image_path     text not null,
  patient_name   text,
  prescriber_name text,
  prescriber_reg text,
  issued_date    date,
  extracted      jsonb,                         -- [{drug,strength,form,qty,dosage,duration_days}]
  confidence     numeric(4,3),
  flags          text[] default '{}',           -- {illegible,expired_script,no_prescriber_reg,controlled_drug}
  status         text not null default 'pending_verification'
                 check (status in ('pending_verification','verified','rejected')),
  verified_by    uuid references staff(id),     -- MUST be role='pharmacist'
  verified_at    timestamptz,
  rejection_reason text,
  created_at     timestamptz default now()
);
create index on prescriptions (pharmacy_id, status);

create table orders (
  id             uuid primary key default gen_random_uuid(),
  pharmacy_id    uuid not null references pharmacies(id) on delete cascade,
  customer_id    uuid not null references customers(id),
  prescription_id uuid references prescriptions(id),
  channel        text default 'whatsapp',
  status         text not null default 'quoted'
                 check (status in ('quoted','awaiting_pharmacist','awaiting_payment',
                                   'paid','packed','dispatched','delivered','cancelled')),
  subtotal       numeric(14,2),
  delivery_fee   numeric(14,2) default 0,
  points_redeemed int default 0,
  total          numeric(14,2),
  delivery_address text,
  rider_name     text,
  rider_phone    text,
  delivery_code  text,
  receipt_pdf    text,
  qr_token       text unique,                   -- signed token behind the invoice QR
  created_at     timestamptz default now()
);
create index on orders (pharmacy_id, status);

create table order_lines (
  id          uuid primary key default gen_random_uuid(),
  order_id    uuid not null references orders(id) on delete cascade,
  product_id  uuid not null references products(id),
  batch_id    uuid references batches(id),      -- FEFO allocation
  qty_pieces  int not null,
  unit_price  numeric(12,4),
  line_total  numeric(14,2),
  substituted_from text
);

create table payments (
  id            uuid primary key default gen_random_uuid(),
  pharmacy_id   uuid not null references pharmacies(id) on delete cascade,
  order_id      uuid references orders(id),
  method        text default 'mpesa_stk',
  amount        numeric(14,2) not null,
  mpesa_receipt text unique,                    -- idempotency key from Daraja callback
  checkout_request_id text,
  phone         text,
  status        text not null default 'pending'
                check (status in ('pending','success','failed','timeout')),
  raw_callback  jsonb,
  created_at    timestamptz default now()
);

create table loyalty_ledger (
  id          bigserial primary key,
  customer_id uuid not null references customers(id) on delete cascade,
  delta       int not null,
  reason      text not null,                    -- 'purchase','redeem','bonus','expiry_clearance'
  order_id    uuid references orders(id),
  created_at  timestamptz default now()
);

-- ---------- messaging & jobs ----------
create table wa_messages (
  id           bigserial primary key,
  pharmacy_id  uuid references pharmacies(id) on delete cascade,
  wa_id        text,                            -- provider message id, for idempotency
  direction    text check (direction in ('in','out')),
  from_phone   text,
  to_phone     text,
  msg_type     text,                            -- text,image,document
  body         text,
  media_path   text,
  intent       text,                            -- classified intent
  handled      boolean default false,
  error        text,
  created_at   timestamptz default now(),
  unique (wa_id)
);
create index on wa_messages (from_phone, created_at desc);

-- Short-lived conversation state. Keeps the bot from re-asking things.
create table wa_state (
  phone       text primary key,
  pharmacy_id uuid references pharmacies(id) on delete cascade,
  flow        text,                             -- 'grn','prescription','report','idle'
  context     jsonb default '{}',
  expires_at  timestamptz,
  updated_at  timestamptz default now()
);

create table alerts (
  id           uuid primary key default gen_random_uuid(),
  pharmacy_id  uuid not null references pharmacies(id) on delete cascade,
  kind         text not null,                   -- 'expiry_90','low_stock','grn_discrepancy','payment_failed'
  severity     text default 'info',
  payload      jsonb,
  sent_to      text[],
  sent_at      timestamptz,
  created_at   timestamptz default now()
);

create table job_runs (                          -- so a failed cron is visible, not silent
  id         bigserial primary key,
  job        text not null,
  status     text not null,
  detail     jsonb,
  started_at timestamptz default now(),
  ended_at   timestamptz
);

-- ---------- helper views ----------
create view v_stock_on_hand as
select p.id as product_id, p.pharmacy_id, p.legacy_code, p.name, p.pack_size,
       p.reorder_level_pieces, p.sell_price,
       coalesce(sum(b.qty_pieces),0) as qty_pieces,
       coalesce(sum(b.qty_pieces),0) / greatest(p.pack_size,1) as whole_packs,
       min(b.expiry_date) filter (where b.qty_pieces > 0) as earliest_expiry
from products p
left join batches b on b.product_id = p.id and b.qty_pieces > 0
group by p.id;

create view v_expiry_risk as
select b.pharmacy_id, p.name, p.legacy_code, b.batch_no, b.expiry_date, b.qty_pieces,
       round(b.qty_pieces * coalesce(p.cost_price,0), 2) as value_at_risk,
       (b.expiry_date - current_date) as days_left
from batches b
join products p on p.id = b.product_id
where b.qty_pieces > 0 and b.expiry_date is not null
order by b.expiry_date;

-- 90-day velocity, used for reorder suggestions and season detection
create view v_velocity_90d as
select p.id as product_id, p.pharmacy_id, p.name,
       -sum(m.delta_pieces) as sold_90d,
       round(-sum(m.delta_pieces)::numeric / 90, 2) as avg_daily
from stock_movements m
join batches b on b.id = m.batch_id
join products p on p.id = b.product_id
where m.reason = 'sale' and m.created_at > now() - interval '90 days'
group by p.id;

-- ---------- RLS ----------
-- The FastAPI backend uses the service_role key and bypasses RLS.
-- Enable RLS anyway so a leaked anon key can't drain the tables.
alter table pharmacies       enable row level security;
alter table staff            enable row level security;
alter table suppliers        enable row level security;
alter table products         enable row level security;
alter table batches          enable row level security;
alter table stock_movements  enable row level security;
alter table grns             enable row level security;
alter table grn_lines        enable row level security;
alter table purchase_orders  enable row level security;
alter table po_lines         enable row level security;
alter table customers        enable row level security;
alter table prescriptions    enable row level security;
alter table orders           enable row level security;
alter table order_lines      enable row level security;
alter table payments         enable row level security;
alter table loyalty_ledger   enable row level security;
alter table wa_messages      enable row level security;
alter table wa_state         enable row level security;
alter table alerts           enable row level security;
