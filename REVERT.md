# Revert — back to single-tenant

Tested on 28 July 2026, not assumed. The tagged code was checked out in a scratch
worktree and booted successfully against the live database.

## What the tag is

`pre-multitenant` → `b3578ce`

Nine `PID = settings.PHARMACY_ID` constants intact. `pharmacies` has no `wa_jid` /
`gowa_device_id` / `kind`. One GOWA session (`pharmacy-1` = `254777602338`), branded, with
live traffic proven end to end.

## Revert the code

```bash
git checkout pre-multitenant
./run.sh stop && ./run.sh all
```

Verified: the tagged tree boots and `/health` returns `{"ok":true}`.

**Code alone is not enough once the migration has run.** After item #3 deletes
`settings.PHARMACY_ID`, reverting only the schema leaves nine modules calling `pid()`
against columns that no longer exist. Revert code *then* schema, in that order.

## Revert the schema

`psql` is **not installed** on this machine, so the down-migration runs through the venv:

```bash
set -a && . ./.env && set +a
.venv/bin/python - <<'EOF'
import sys; sys.path.insert(0, "api")
from db import ex
ex("drop index if exists pharmacies_wa_jid_uniq")
ex("drop index if exists pharmacies_gowa_device_id_uniq")
ex("alter table pharmacies drop column if exists wa_jid")
ex("alter table pharmacies drop column if exists gowa_device_id")
ex("alter table pharmacies drop column if exists kind")
print("schema reverted")
EOF
```

All three columns are additive, so dropping them is safe and fast. The backfill is three
values (see below) — retype them if you roll forward again.

## Backups taken

| What | Where |
|---|---|
| 7 non-empty tables as CSV (52 rows total) | scratchpad `pre-multitenant-backup/` |
| Live schema introspection | `pre-multitenant-backup/live-schema.json` |
| GOWA device state | `pre-multitenant-backup/gowa-devices.json` |

**Move that directory somewhere permanent — it is in a session scratchpad.** It contains
customer and prescription rows, so do **not** commit it.

The committed `db/schema*.sql` files are the schema of record; `live-schema.json` is a
cross-check in case they have drifted from the running database.

**Note: the database is not on this laptop.** It is hosted Supabase in `eu-north-1`, so
hardware failure was only ever a risk to code — which is now pushed. These dumps protect
against a bad migration, which is a different risk and still worth covering.

## Backfill values (for rolling forward again)

```
pharmacies.id           = c1457e5e-9f62-468b-ab50-b41382e83610   (name: 'Pharma')
        wa_jid          = '254777602338@s.whatsapp.net'
        gowa_device_id  = 'pharmacy-1'
        kind            = 'tenant'
```

`pharmacy-1` stays mapped to the **real tenant** until cutover. Making it the platform bot
early would leave the existing pharmacy with no inbound path at all.

## GOWA state at tag time

```
pharmacy-1   logged_in      jid=254777602338@s.whatsapp.net   ← PAIRED, do not delete
pharmacy-2   disconnected   jid=(none)                        ← empty, safe to delete
```

Re-verify with `GET /devices` before deleting anything. Do not trust this snapshot; slots
change.
