#!/bin/bash
# Poll the live feeds, rebuild models and run data-quality checks.
# Scheduled by cron at :07/:22/:37/:52 (see README).
cd "$(dirname "$0")/.." || exit 1
mkdir -p data/logs
SW=.venv/bin/spillwatch
{
  # Once an hour, re-fetch yesterday's and today's rainfall. This repairs gaps
  # left while the laptop was asleep and picks up revised readings.
  if [ "$(date +%M)" -lt 15 ]; then
    $SW backfill-rain --days 1
  fi

  $SW collect && $SW transform && $SW check

} >> "data/logs/collect_$(date +%Y-%m-%d).log" 2>&1
