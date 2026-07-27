-- ============================================================
-- Pharma OS schema v6 — WhatsApp login codes. Additive, safe on top of v1-v5.
--
-- PHASE 1B. Why a 6-digit code over WhatsApp rather than a magic link or email:
--
--   * A magic LINK sent to WhatsApp opens on the PHONE. The person is signing in on a
--     desktop. Cross-device login is the whole problem, and a link makes it worse.
--     A code they read on the phone and type on the desktop crosses that gap.
--   * Email needs SMTP configured in Supabase. Nobody has done that, and a pharmacy
--     assistant may not have a work email at all.
--   * Google OAuth needs a Google Cloud project, an OAuth consent screen, and a
--     redirect URL. Also not done, and many staff will not have Google accounts.
--   * WhatsApp is already the channel this product runs on, already installed on
--     every staff phone, and staff.phone is already the access list.
--
-- The code proves possession of the WhatsApp number. Supabase then supplies the
-- durable identity (auth.users) that survives a phone change, and the session that
-- Phase 2's RLS policies will key on. The code is authentication; auth.users is
-- identity. Keeping those separate is the point.
-- ============================================================

create table if not exists login_codes (
  id          bigserial primary key,
  staff_id    uuid not null references staff(id) on delete cascade,
  -- SHA-256, never the code itself. A leaked database backup must not hand someone a
  -- working login for every staff member who signed in that hour.
  code_hash   text not null,
  -- 10 minutes: long enough to walk to the desk, short enough that a code left on a
  -- screen is dead by the time anyone else reads it.
  expires_at  timestamptz not null default now() + interval '10 minutes',
  attempts    int default 0,
  used_at     timestamptz,
  sent_to     text,                     -- the phone it went to, for the audit trail
  created_at  timestamptz default now()
);
create index if not exists login_codes_live_idx
    on login_codes (staff_id, created_at desc) where used_at is null;

comment on table login_codes is
  'Short-lived WhatsApp sign-in codes. Hashed, single-use, rate-limited. Proves '
  'possession of staff.phone; auth.users is what proves identity.';

-- Rate limiting lives on staff so it survives the codes being deleted, and so an
-- attacker cannot reset it by triggering a new code.
alter table staff add column if not exists login_locked_until timestamptz;
alter table staff add column if not exists login_failed_count int default 0;

-- ---------- housekeeping ----------
-- Expired codes are useless and are an unnecessary thing to keep. Called by the
-- login path itself, so there is no cron dependency.
create or replace function purge_expired_login_codes() returns void as $$
  delete from login_codes
   where expires_at < now() - interval '1 day';
$$ language sql;
