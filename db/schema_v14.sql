-- v14 — staff.role gets a history, because it is a regulatory record
--
-- staff.role decides who may approve a prescription-only medicine, and that approval is
-- logged against a PPB registration number. So role IS part of the regulatory trail. As a
-- bare mutable column with no history, the answer to "who authorised this dispensing, and
-- were they entitled to?" rests on a value that anyone holding a JOIN code could rewrite --
-- and did: on the night of the first live registration, one phone sent OWNER (becoming a
-- manager) and three minutes later JOIN, and `do update set role = excluded.role` silently
-- demoted them to attendant. Nothing recorded that it happened.
--
-- Append-only. No updates, no deletes: a history that can be edited is not a history.

create table if not exists staff_role_changes (
  id           uuid primary key default gen_random_uuid(),
  pharmacy_id  uuid not null references pharmacies(id) on delete cascade,
  phone        text not null,
  old_role     text,                       -- null when the person is newly added
  new_role     text not null,
  -- How the change was made. 'join_code'/'owner_code' are self-service over WhatsApp;
  -- 'register' is the owner created by REGISTER; 'dashboard' is a human with a login.
  -- Knowing the mechanism is the point: a role granted by a forwarded code carries less
  -- assurance than one granted by a named person in the dashboard.
  mechanism    text not null check (mechanism in
                 ('join_code','owner_code','register','dashboard','seed')),
  -- Who caused it. The redeeming phone for a code, a staff id or name for the dashboard.
  -- Deliberately free text: the actor is not always a row in our tables.
  actor        text,
  created_at   timestamptz not null default now()
);

create index if not exists staff_role_changes_pharmacy
  on staff_role_changes (pharmacy_id, created_at desc);
create index if not exists staff_role_changes_phone
  on staff_role_changes (phone, created_at desc);

alter table staff_role_changes enable row level security;
do $$
begin
  if not exists (select 1 from pg_policies
                  where tablename = 'staff_role_changes' and policyname = 'tenant_isolation') then
    create policy tenant_isolation on staff_role_changes
      using (pharmacy_id = current_pharmacy());
  end if;
end $$;

-- Backfill what can be established from the current state. Every existing staff row got its
-- role somehow; we cannot know how, so it is recorded as an unknown-mechanism starting
-- point rather than left absent. 'seed' is used for exactly this: it means "this is where
-- the history begins", not "a script did it".
insert into staff_role_changes (pharmacy_id, phone, old_role, new_role, mechanism, actor,
                                created_at)
select s.pharmacy_id, s.phone, null, s.role, 'seed', 'v14 backfill', s.created_at
  from staff s
 where not exists (select 1 from staff_role_changes c
                    where c.pharmacy_id = s.pharmacy_id and c.phone = s.phone);
