-- v12 — staff.phone is unique PER PHARMACY, not globally
--
-- staff carried `UNIQUE (phone)` across the whole table, so one person could be staff at
-- exactly one pharmacy for the lifetime of the deployment. That is the same class of bug
-- the build spec flagged for ppb_number, and it has three consequences:
--
--   * A pharmacist who genuinely works at two pharmacies cannot be inserted the second
--     time. Their employer is decided by whoever registered first.
--   * tenancy.resolve_by_sender() returns a LIST specifically so a person known at several
--     pharmacies is reported as ambiguous rather than silently pinned to one. For staff
--     that branch was unreachable, because the database made the situation impossible.
--   * Onboarding a second pharmacy whose owner already helps at the first fails with a
--     constraint violation that reads like a duplicate-entry bug rather than a policy.
--
-- customers already has UNIQUE (pharmacy_id, phone). This brings staff in line.
--
-- Safe to apply: nothing in api/ or dashboard/ uses ON CONFLICT on staff.phone.

do $$
begin
  if exists (select 1 from pg_constraint
              where conrelid = 'staff'::regclass and conname = 'staff_phone_key') then
    alter table staff drop constraint staff_phone_key;
  end if;

  if not exists (select 1 from pg_constraint
                  where conrelid = 'staff'::regclass
                    and conname = 'staff_pharmacy_id_phone_key') then
    alter table staff add constraint staff_pharmacy_id_phone_key unique (pharmacy_id, phone);
  end if;
end $$;

-- Same reasoning for the PPB number: a pharmacist registered at two pharmacies needs the
-- same registration number recorded at both. Only added if the column is not already
-- constrained per-pharmacy.
do $$
begin
  if exists (select 1 from pg_constraint
              where conrelid = 'staff'::regclass and conname = 'staff_ppb_reg_no_key') then
    alter table staff drop constraint staff_ppb_reg_no_key;
    alter table staff add constraint staff_pharmacy_id_ppb_reg_no_key
      unique (pharmacy_id, ppb_reg_no);
  end if;
end $$;
