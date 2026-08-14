#!/usr/bin/env bash
# verify-migrations.sh — Comprobaciones básicas de migraciones Supabase:
# nombres ordenables, no vacías y aviso ante sentencias destructivas sin marcar.
set -euo pipefail

fail=0
dir="supabase/migrations"

if [ ! -d "$dir" ] || [ -z "$(ls -A "$dir" 2>/dev/null | grep -v '.gitkeep' || true)" ]; then
  echo "ℹ️  Sin migraciones todavía."
  exit 0
fi

for f in "$dir"/*.sql; do
  [ -e "$f" ] || continue
  [ -s "$f" ] || { echo "❌ Migración vacía: $f"; fail=1; }
  if grep -iqE '\b(drop\s+table|drop\s+column|truncate)\b' "$f"; then
    if ! grep -iq 'RECOVERY-PLAN' "$f"; then
      echo "❌ $f: sentencia destructiva sin comentario 'RECOVERY-PLAN'."
      fail=1
    fi
  fi
done

[ $fail -eq 0 ] && echo "✅ Migraciones OK."
exit $fail
