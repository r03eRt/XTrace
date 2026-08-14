#!/usr/bin/env bash
# verify-workflow.sh — Comprueba que cada feature con spec APPROVED/IMPLEMENTING
# disponga de plan.md y tasks.md, y que exista la plantilla de PR.
set -euo pipefail

fail=0

[ -f ".github/pull_request_template.md" ] || { echo "❌ Falta .github/pull_request_template.md"; fail=1; }
[ -f "AGENTS.md" ] || { echo "❌ Falta AGENTS.md"; fail=1; }
[ -f ".specify/memory/constitution.md" ] || { echo "❌ Falta la constitución"; fail=1; }

if [ -d specs ]; then
  while IFS= read -r spec; do
    dir=$(dirname "$spec")
    status=$(grep -iE '^\*?\*?\s*(Status|Estado)\**\s*:' "$spec" | head -1 | sed -E 's/.*[:：]\s*//; s/[`*]//g' | tr -d '\r' | xargs || true)
    case "$status" in
      APPROVED|IMPLEMENTING|IMPLEMENTED)
        [ -f "$dir/plan.md" ]  || { echo "❌ $dir: falta plan.md (estado $status)"; fail=1; }
        [ -f "$dir/tasks.md" ] || { echo "❌ $dir: falta tasks.md (estado $status)"; fail=1; }
        ;;
      *)
        echo "ℹ️  $dir: estado $status (no requiere plan/tasks aún)";;
    esac
  done < <(find specs -name 'spec.md')
fi

exit $fail
