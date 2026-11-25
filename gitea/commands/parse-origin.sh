#!/bin/sh
set -eu

if [ -z "${GC_PACK_DIR:-}" ]; then
  echo "gc gitea parse-origin: missing Gas City pack context" >&2
  exit 1
fi

exec python3 "$GC_PACK_DIR/assets/scripts/gitea_pr.py" parse-origin "$@"
