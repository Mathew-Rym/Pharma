-- v15 — hash the approval PINs that are sitting in plaintext
--
-- staff.approval_pin held the PIN as typed, and api/approvals.check_pin compared it with
-- `str(pin).strip() == str(staff["approval_pin"]).strip()`. Anyone with read access to the
-- staff table -- a backup, a support query, a screenshot of a psql session -- could approve
-- a prescription-only medicine as a named pharmacist, against that pharmacist's PPB
-- registration number. The POM approval log would record it as valid. An audit trail that
-- can be forged is worse than one that is missing, because it is trusted.
--
-- A 4-6 digit PIN is trivially brute-forced from a hash, so this is not confidentiality
-- against someone who has the table and time; the lockout counter in check_pin is what
-- limits online guessing. What it removes is the ability to read a WORKING PIN straight out
-- of stored data, which is the exposure that actually happens.

-- IDEMPOTENT. A sha-256 hex digest is exactly 64 characters of [0-9a-f]; a PIN is 4-6
-- digits. So "already hashed" is decidable from the value itself, and re-running this is a
-- no-op rather than double-hashing every PIN and locking every pharmacist out of approvals.
--
-- sha256() is built in from Postgres 11 -- no pgcrypto dependency, which matters because a
-- migration that needs an extension fails differently on a managed instance.
update staff
   set approval_pin = encode(sha256(approval_pin::bytea), 'hex')
 where approval_pin is not null
   and approval_pin <> ''
   and approval_pin !~ '^[0-9a-f]{64}$';

-- Anything left that is neither empty nor a 64-char digest means a PIN was written in a
-- format nobody expected. Surfaced rather than silently migrated.
do $$
declare n int;
begin
  select count(*) into n from staff
   where approval_pin is not null and approval_pin <> ''
     and approval_pin !~ '^[0-9a-f]{64}$';
  if n > 0 then
    raise warning 'v15: % approval_pin value(s) are still not sha-256 hex', n;
  end if;
end $$;
