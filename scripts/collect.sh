#!/bin/bash
# Poll the live feeds, rebuild models and run data-quality checks.
# Scheduled every 15 minutes by cron (see README).
cd "$(dirname "$0")/.." || exit 1
mkdir -p data/logs
SW=.venv/bin/spillwatch
{
  $SW collect && $SW transform && $SW check

  # Once a day, re-load the last 2 days of the rainfall archive. This fills any
  # gaps left when the laptop was asleep and picks up the EA's revised readings.
  if [ "$(date +%H)" = "06" ] && [ "$(date +%M)" -lt 15 ]; then
    $SW backfill-rain --days 2 --force && $SW transform
  fi
} >> "data/logs/collect_$(date +%Y-%m-%d).log" 2>&1
