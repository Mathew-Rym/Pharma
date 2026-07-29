-- v11 — pin search_path on our own functions
--
-- Supabase's linter flags "function_search_path_mutable". A SECURITY DEFINER or widely
-- called function with an unpinned search_path can be made to resolve a table name to
-- something the caller controls, if they can create objects in a schema that appears
-- earlier on the path.
--
-- Scope note: the linter lists 33 functions, but 31 of them belong to the pg_trgm
-- extension. Those are not ours to rewrite -- the fix for those is the SEPARATE
-- "extension in public" warning, i.e. relocating pg_trgm, which would break every
-- similarity() call in reports.py and grn.py unless the path is adjusted with it. Not
-- worth doing the night before a demo. These two are ours.

-- current_pharmacy() is the function every RLS policy calls, so it matters most.
--
-- Body deliberately UNCHANGED. It reads app.current_pharmacy, which db.tenant_scope()
-- sets with SET LOCAL per transaction. A proposed "fix" would have replaced this with a
-- lookup on app.user_phone -- a setting nothing in this codebase sets -- which returns
-- NULL, making every policy evaluate false and every RLS-scoped query return zero rows
-- the moment DB_ENFORCE_RLS is switched on. Pin the path; leave the logic alone.
create or replace function current_pharmacy()
returns uuid
language sql
stable
set search_path = public, pg_temp
as $$
  select nullif(current_setting('app.current_pharmacy', true), '')::uuid;
$$;

-- Recreated with its real body rather than the guessed one. It deletes expired sign-in
-- codes from login_codes; a version that deleted from `staff` would remove the staff row
-- itself, taking away access instead of expiring a code.
create or replace function purge_expired_login_codes()
returns void
language sql
security definer
set search_path = public, pg_temp
as $$
  delete from login_codes where expires_at < now();
$$;
