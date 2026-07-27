-- ============================================================
-- Pharma OS schema v5 — per-user identity. Additive, safe on top of v1-v4.
--
-- PHASE 1A of the auth work. This file only adds structure; nothing changes
-- behaviour yet. The dashboard keeps working exactly as it does today, which is the
-- point: each phase has to leave a working system behind it.
--
-- THE MODEL
--
--   auth.users        WHO someone is. One row per human, forever. Supabase owns it.
--        |
--   staff             WHAT they can do HERE. One row per human per pharmacy.
--        |
--   pharmacies        the tenant
--
-- Identity is permanent and belongs to the person. Everything else -- phone number,
-- email, WhatsApp -- is a communication channel that can change without the person
-- changing. That is why `supabase_user_id` is the link and `phone` is not.
--
-- WHAT THIS DELIBERATELY DOES NOT ADD
--
-- No `status` column. The reviews asked for pending/active/suspended, but those three
-- states are already fully expressible:
--
--     invited      supabase_user_id IS NULL      (nobody has claimed the row yet)
--     active       supabase_user_id IS NOT NULL AND is_active
--     deactivated  NOT is_active
--
-- Adding a status column would create a second source of truth for the same fact, and
-- the first time someone updates one without the other you get a staff member who is
-- 'active' but cannot log in, with no way to tell which column is lying. Same reason
-- v3 reused qty_counted_pieces instead of adding staff_confirmed_count.
-- ============================================================

-- ---------- identity link ----------
-- No foreign key to auth.users on purpose. Supabase manages that table, a FK across
-- into it complicates restores and project migrations, and the uniqueness constraint
-- is what actually matters here: one identity cannot be two staff rows in the same
-- pharmacy.
alter table staff add column if not exists supabase_user_id uuid;

do $$
begin
  if not exists (select 1 from pg_indexes
                  where indexname = 'staff_supabase_user_uidx') then
    create unique index staff_supabase_user_uidx
        on staff (supabase_user_id) where supabase_user_id is not null;
  end if;
end $$;

-- Business contact address. NOT identity -- the identity email lives in auth.users and
-- may legitimately differ (personal Gmail for login, work address on the PO).
alter table staff add column if not exists display_email text;

alter table staff add column if not exists invited_by uuid references staff(id);
alter table staff add column if not exists invited_at timestamptz;
alter table staff add column if not exists accepted_at timestamptz;
alter table staff add column if not exists last_login_at timestamptz;

comment on column staff.supabase_user_id is
  'auth.users.id once the invitation is accepted. NULL means invited but not yet '
  'claimed. This is the identity link; staff.phone is a communication channel.';

-- ---------- invitations ----------
-- Separate from authentication on purpose: who invited you is a different question
-- from who you are. The token proves "this pharmacy asked for you"; Supabase then
-- proves "you are you".
create table if not exists invitations (
  id          uuid primary key default gen_random_uuid(),
  staff_id    uuid not null references staff(id) on delete cascade,
  token       text unique not null,
  -- 72h: long enough to survive a weekend, short enough that a screenshot of a
  -- WhatsApp invite left on a shared phone stops being useful.
  expires_at  timestamptz not null default now() + interval '72 hours',
  used_at     timestamptz,
  revoked     boolean default false,
  created_by  uuid references staff(id),
  created_at  timestamptz default now()
);
create index if not exists invitations_staff_idx on invitations (staff_id);
create index if not exists invitations_live_idx on invitations (token)
    where used_at is null and not revoked;

-- ---------- login audit ----------
-- Medical software needs to answer "who was in the system, when, and how". Separate
-- from the general audit trail because logins are high-volume and queried differently.
create table if not exists login_events (
  id           bigserial primary key,
  staff_id     uuid references staff(id) on delete set null,
  pharmacy_id  uuid references pharmacies(id) on delete cascade,
  email        text,
  method       text check (method in ('google','magic_link','shared_password','whatsapp')),
  success      boolean default true,
  failure_reason text,
  ip_address   inet,
  user_agent   text,
  created_at   timestamptz default now()
);
create index if not exists login_events_staff_idx on login_events (staff_id, created_at desc);
create index if not exists login_events_failed_idx on login_events (created_at desc)
    where not success;

-- ---------- a view the dashboard can read directly ----------
create or replace view v_staff_access as
select s.id,
       s.pharmacy_id,
       p.name              as pharmacy,
       s.name,
       s.role,
       s.phone,
       s.display_email,
       s.ppb_reg_no,
       s.is_active,
       s.supabase_user_id,
       s.accepted_at,
       s.last_login_at,
       (s.approval_pin is not null) as has_pin,
       case
         when not s.is_active                 then 'deactivated'
         when s.supabase_user_id is not null  then 'active'
         else 'invited'
       end                 as access_state,
       (select count(*) from invitations i
         where i.staff_id = s.id and i.used_at is null and not i.revoked
           and i.expires_at > now())          as live_invites
  from staff s
  join pharmacies p on p.id = s.pharmacy_id;
