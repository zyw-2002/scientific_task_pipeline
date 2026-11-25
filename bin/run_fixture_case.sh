#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
OUT=${1:-"$ROOT/work/fixture_case"}
python3 - "$OUT" <<'PY'
import json
import sys
from pipeline.orchestration.fixture_run import run_fixture_case
result=run_fixture_case(sys.argv[1])
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result['status']=='pass' else 1)
PY
