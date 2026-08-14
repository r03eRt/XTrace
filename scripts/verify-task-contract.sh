#!/usr/bin/env bash
# verify-task-contract.sh — Comprueba que no queden tareas BLOCKED y que los
# contratos de tarea tengan los campos obligatorios.
set -euo pipefail

fail=0

if grep -rIlE --include='tasks.md' -e '—[[:space:]]*BLOCKED' -e 'status:[[:space:]]*BLOCKED' specs >/dev/null 2>&1; then
  echo "❌ Hay tareas en estado BLOCKED en tasks.md:"
  grep -rInE --include='tasks.md' -e '—[[:space:]]*BLOCKED' -e 'status:[[:space:]]*BLOCKED' specs || true
  fail=1
fi

# Contratos de tarea: specs/**/contracts/*.yml|*.yaml
required=(task_id spec_id status allowed_paths required_tests reviewer)
while IFS= read -r contract; do
  for field in "${required[@]}"; do
    grep -qE "^${field}\s*:" "$contract" || { echo "❌ $contract: falta campo '$field'"; fail=1; }
  done
done < <(find specs -path '*/contracts/*.y*ml' 2>/dev/null || true)

[ $fail -eq 0 ] && echo "✅ Contratos de tarea OK (sin BLOCKED)."
exit $fail
