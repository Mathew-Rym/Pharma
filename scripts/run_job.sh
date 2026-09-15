#!/usr/bin/env bash
# Run one Pharma OS scheduled job against the local API.
#
# WHY THIS EXISTS: .github/workflows/cron.yml schedules the daily jobs via GitHub
# Actions, but Actions runners cannot reach a VM whose API port is firewalled
# (and the API_URL secret is unset). Without this, expiry alerts, low-stock POs,
# digests and reconciliation silently never run. The same schedules therefore run
# ON the VM via crontab -- localhost only, no firewall involved.
#
# The secret comes from .env at runtime -- it is never embedded in the crontab.
#
# Install (once, on the VM -- UTC times mirror .github/workflows/cron.yml):
#   0 4 * * *  /path/to/Pharma/scripts/run_job.sh expiry_sweep       >> .run/cron.log 2>&1
#   3 4 * * *  /path/to/Pharma/scripts/run_job.sh forecast_refresh   >> .run/cron.log 2>&1
#   5 4 * * *  /path/to/Pharma/scripts/run_job.sh low_stock_check    >> .run/cron.log 2>&1
#   7 4 * * *  /path/to/Pharma/scripts/run_job.sh morning_briefing   >> .run/cron.log 2>&1
#   10 4 * * * /path/to/Pharma/scripts/run_job.sh variance_report    >> .run/cron.log 2>&1
#   0 10 * * * /path/to/Pharma/scripts/run_job.sh afternoon_briefing >> .run/cron.log 2>&1
#   0 17 * * * /path/to/Pharma/scripts/run_job.sh daily_digest       >> .run/cron.log 2>&1
#   0 5 * * 1  /path/to/Pharma/scripts/run_job.sh weekly_report      >> .run/cron.log 2>&1
#   30 5 * * * /path/to/Pharma/scripts/run_job.sh refill_reminders   >> .run/cron.log 2>&1
#   */15 * * * * /path/to/Pharma/scripts/run_job.sh reconcile        >> .run/cron.log 2>&1
set -euo pipefail
JOB=${1:?usage: run_job.sh <job-name>}
cd "$(dirname "$0")/.."
SECRET=$(grep '^SHARED_SECRET=' .env | cut -d= -f2-)
[ -n "$SECRET" ] || { echo 'SHARED_SECRET missing in .env' >&2; exit 1; }
curl -s -m 120 -X POST -H "x-pharmaos-secret: $SECRET" "http://localhost:8000/jobs/$JOB"
echo
