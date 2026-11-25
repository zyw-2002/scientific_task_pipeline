#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
SOURCE=${1:?source bundle path required}
OUT=${2:-"$ROOT/work/source_case"}
python3 - "$OUT" "$SOURCE" <<'PY'
import json, sys
from pipeline.orchestration.fixture_run import run_source_case
result=run_source_case(root=sys.argv[1], source_bundle=sys.argv[2])
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result['status']=='pass' else 1)
PY
