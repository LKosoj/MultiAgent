#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
PYTHON_BIN="${PYTHON:-python3}"

manual_scripts=(
  "test_pipeline_run.py"
  "test_enhanced_workflow.py"
  "test_memory_archivist_simple.py"
  "test_agent.py"
  "test_workflow.py"
  "test_brainstormer.py"
  "test_memory_archivist.py"
  "StoryBookManager/test_universal_editor.py"
  "StoryBookManager/test_schema_validation.py"
)

printf 'Manual smoke scripts excluded from default pytest:\n'
printf '  %s\n' "${manual_scripts[@]}"

"$PYTHON_BIN" -m py_compile "${manual_scripts[@]}"

if [[ "${RUN_MANUAL_SMOKE_TESTS:-0}" != "1" ]]; then
  printf '\nSyntax check passed. Set RUN_MANUAL_SMOKE_TESTS=1 to execute scripts with local services/fixtures.\n'
  exit 0
fi

for script in "${manual_scripts[@]}"; do
  printf '\n==> %s\n' "$script"
  "$PYTHON_BIN" "$script"
done
