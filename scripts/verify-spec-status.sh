#!/usr/bin/env bash
# verify-spec-status.sh — Comprueba que las specs referenciadas por la rama/PR
# tengan un estado válido y, si van a implementarse, estén APPROVED.
set -euo pipefail

fail=0
specs_dir="specs"

if [ ! -d "$specs_dir" ]; then
  echo "No existe specs/ — nada que verificar."
  exit 0
fi

valid="DRAFT CLARIFICATION_REQUIRED READY_FOR_REVIEW APPROVED IMPLEMENTING IMPLEMENTED DEPRECATED"

while IFS= read -r spec; do
  status=$(grep -iE '^\*?\*?\s*(Status|Estado)\**\s*:' "$spec" | head -1 | sed -E 's/.*[:：]\s*//; s/[`*]//g' | tr -d '\r' | xargs || true)
  if [ -z "$status" ]; then
    echo "❌ $spec: falta el campo Estado/Status."
    fail=1
    continue
  fi
  if ! echo "$valid" | grep -qw "$status"; then
    echo "❌ $spec: estado inválido '$status'."
    fail=1
  else
    echo "✅ $spec: $status"
  fi
done < <(find "$specs_dir" -name 'spec.md')

exit $fail
