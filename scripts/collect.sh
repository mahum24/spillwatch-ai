#!/bin/bash
# Poll the live feeds and rebuild models. Schedule every 15 minutes (see README).
cd "$(dirname "$0")/.." || exit 1
mkdir -p data/logs
{
  .venv/bin/spillwatch collect && .venv/bin/spillwatch transform
} >> "data/logs/collect_$(date +%Y-%m-%d).log" 2>&1
