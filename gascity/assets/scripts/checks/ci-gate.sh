#!/usr/bin/env bash
set -euo pipefail

# build-basic's CI gate: the exec check of the `ci-gate` step.
#
# All logic lives in ../ci_gate.py (`wait`). It reads $GC_BEAD_ID (the fix
# round iteration) and $GC_ITERATION, waits bounded for the PR's CI, records
# the verdict on the workflow root, and exits:
#   0  terminal verdict (the loop ends; ci-merge decides what to do)
#   1  red with fix rounds left (gc starts the next fix round)
#   75 gc bd failed at startup (gc re-runs this check without an attempt)
# Its stderr is one token-free summary line, which gc keeps in gc.attempt_log.
# This gate never prompts and never calls a model.

fail() {
  echo "ci-gate: $*" >&2
  exit 75
}

# gc runs gates with HOME=<city root> and a narrow PATH. ci_gate.py needs only
# the stdlib: python3 when present, else a uv-managed interpreter.
if command -v python3 >/dev/null 2>&1; then
  PY=(python3)
elif command -v uv >/dev/null 2>&1; then
  PY=(uv run --quiet --no-project python)
else
  fail "uv or python3 is required on PATH"
fi
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "${PY[@]}" "$SCRIPT_DIR/../ci_gate.py" wait
