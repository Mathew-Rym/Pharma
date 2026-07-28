-- v9 — the outbound record carries its own device
--
-- wa_messages was a log written AFTER the fact. It becomes the record the sender reads
-- FROM, because the device a message must leave by cannot be recovered later.
--
-- Sending happens off the request path in at least four places (jobs.py reorder alerts,
-- the Rx approval SLA escalation, the wa.py retry path, the daily report push). Resolving
-- the device from request or contextvar state works for webhook-driven replies and
-- silently sends from the wrong pharmacy's number on the first cron run. So the device is
-- decided at compose time and written here.

-- The GOWA slot this message must leave by.
alter table wa_messages add column if not exists gowa_device_id text;

-- The JID that slot is expected to be logged in as. Outbound has to be addressed by slot
-- label, so a slot deleted and recreated under the same name would point at a different
-- handset with every log line still looking correct. Recording the expected JID lets the
-- sender refuse rather than misdeliver.
alter table wa_messages add column if not exists expected_wa_jid text;

-- queued -> sent | failed. Distinct from `handled`, which tracks inbound processing.
alter table wa_messages add column if not exists status text;
alter table wa_messages add column if not exists attempts integer not null default 0;
alter table wa_messages add column if not exists last_error text;

-- Find work to retry, and the unsent backlog per tenant, without a full scan.
create index if not exists wa_messages_outbound_status
  on wa_messages (status, created_at) where direction = 'out';
