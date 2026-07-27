-- ============================================================
-- Pharma OS schema v7 — REAL tenant isolation. Additive, safe on top of v1-v6.
--
-- PHASE 2. Until now every query filtered `pharmacy_id = %s` by hand. That is
-- discipline, not isolation: one forgotten filter leaks another pharmacy's patient
-- data, and two such queries had already shipped (job_runs, wa_messages).
--
-- THE PROBLEM WITH WHAT WAS ALREADY THERE
--
-- RLS was already ENABLED on most tables with ZERO policies, described as a
-- fail-closed posture. It was not protecting anything, because the application
-- connects as `postgres`, and:
--
--     select rolname, rolbypassrls from pg_roles where rolname='postgres';
--     -->  postgres | true
--
-- A role with BYPASSRLS ignores every policy. Enabling RLS and connecting as
-- postgres is security theatre -- it looks locked in the Supabase UI and enforces
-- nothing.
--
-- THE FIX
--
-- A second role that CANNOT bypass RLS, which the app switches into per transaction:
--
--     set local role pharmaos_app;
--     set local app.current_pharmacy = '<uuid>';
--     ... queries ...                      -- policies now apply, and cannot be
--     commit;                              -- escaped without ending the transaction
--
-- SET LOCAL rather than a separate connection or credential on purpose: it is scoped
-- to the transaction, so it cannot leak into the next request on a pooled connection,
-- and it needs no new secret in .env.
--
-- NOT SWITCHED ON BY DEFAULT. This file only creates the capability. db.tenant_scope()
-- opts in, and DB_ENFORCE_RLS gates it. Same reasoning as AUTH_MODE: a half-landed
-- isolation change must not be able to take a pharmacy's stock system down mid-shift.
-- ============================================================

-- ---------- the role that cannot bypass RLS ----------
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'pharmaos_app') then
    create role pharmaos_app nologin nobypassrls;
  end if;
end $$;

-- Explicitly, in case the role predates this file or someone flipped it.
alter role pharmaos_app nobypassrls;

grant usage on schema public to pharmaos_app;
grant select, insert, update, delete on all tables in schema public to pharmaos_app;
grant usage, select on all sequences in schema public to pharmaos_app;
grant execute on all functions in schema public to pharmaos_app;

-- Anything added later inherits the same grants, so a new table is not silently
-- unreachable the first time someone queries it through the app role.
alter default privileges in schema public
  grant select, insert, update, delete on tables to pharmaos_app;
alter default privileges in schema public
  grant usage, select on sequences to pharmaos_app;

-- The connection role must be allowed to SET ROLE into it.
do $$
begin
  execute format('grant pharmaos_app to %I', current_user);
exception when others then
  null;   -- already a member, or not permitted; harmless either way
end $$;

-- ---------- the tenant the current transaction may see ----------
-- `true` as the second argument means "return null if unset" rather than raising.
-- Unset therefore denies everything instead of erroring, which is the right failure
-- direction: a bug that forgets to set the tenant shows no rows, it does not show
-- somebody else's rows.
create or replace function current_pharmacy() returns uuid as $$
  select nullif(current_setting('app.current_pharmacy', true), '')::uuid;
$$ language sql stable;

comment on function current_pharmacy is
  'The pharmacy this transaction may touch. NULL when unset, which denies everything.';

-- ---------- policies ----------
do $$
declare
  t text;
  -- Tables that carry pharmacy_id directly.
  direct text[] := array[
    'agents','alerts','batches','customers','demand_forecast','duty_roster','grns',
    'job_runs','login_events','orders','payments','pos_sales','prescriptions',
    'products','purchase_orders','sales_history_monthly','staff','stock_movements',
    'stock_reconciliation','suppliers','wa_messages','wa_state'];
begin
  foreach t in array direct loop
    if to_regclass('public.' || t) is null then continue; end if;
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists tenant_isolation on %I', t);
    execute format($f$
      create policy tenant_isolation on %I
        using      (pharmacy_id = current_pharmacy())
        with check (pharmacy_id = current_pharmacy())
    $f$, t);
  end loop;
end $$;

-- Child tables have no pharmacy_id of their own. Scope them through their parent, so
-- there is still exactly one definition of which tenant a row belongs to rather than
-- a denormalised copy that can drift.
do $$
declare
  spec text[][] := array[
    ['grn_lines',    'grns',            'grn_id'],
    ['order_lines',  'orders',          'order_id'],
    ['po_lines',     'purchase_orders', 'po_id'],
    ['loyalty_ledger','customers',      'customer_id'],
    ['agent_commands','agents',         'agent_id'],
    ['sync_state',   'agents',          'agent_id'],
    ['invitations',  'staff',           'staff_id'],
    ['login_codes',  'staff',           'staff_id']];
  i int;
begin
  for i in 1 .. array_length(spec, 1) loop
    if to_regclass('public.' || spec[i][1]) is null then continue; end if;
    execute format('alter table %I enable row level security', spec[i][1]);
    execute format('drop policy if exists tenant_isolation on %I', spec[i][1]);
    execute format($f$
      create policy tenant_isolation on %I
        using (exists (select 1 from %I p
                        where p.id = %I.%I
                          and p.pharmacy_id = current_pharmacy()))
        with check (exists (select 1 from %I p
                             where p.id = %I.%I
                               and p.pharmacy_id = current_pharmacy()))
    $f$, spec[i][1], spec[i][2], spec[i][1], spec[i][3],
         spec[i][2], spec[i][1], spec[i][3]);
  end loop;
end $$;

-- `pharmacies` itself: you may see your own row, and nothing else.
alter table pharmacies enable row level security;
drop policy if exists tenant_isolation on pharmacies;
create policy tenant_isolation on pharmacies
  using      (id = current_pharmacy())
  with check (id = current_pharmacy());

-- ---------- proof, callable from a test ----------
-- Returns every public table that is NOT protected, so the test asserts on the
-- database's own account of itself rather than on a list someone maintained by hand
-- and forgot to update when adding a table.
create or replace view v_rls_coverage as
select c.relname as table_name,
       c.relrowsecurity as rls_enabled,
       (select count(*) from pg_policy p where p.polrelid = c.oid) as policies
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public'
   and c.relkind = 'r'
   and c.relname not in ('schema_migrations')
 order by c.relrowsecurity, c.relname;
