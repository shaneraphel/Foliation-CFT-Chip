#!/usr/bin/env bash
# Full heavyweight harvest + optional inverse pass from FPU feed
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT/scripts/data_harvester"
[ -f .env ] || echo "WARN: copy .env.example to .env and set MP_API_KEY"

python3 heavy_harvester.py "$@"
RC=$?

echo ">> Extended sources (OPTIMADE / COD / OQMD struct / NOMAD index)..."
python3 extended_sources.py --optimade-limit 25 --oqmd-struct-limit 20 || echo "WARN: extended_sources partial"

echo ">> Rank killer-TM pool..."
python3 find_killer.py --top 40 || true

if [ -f "$ROOT/artifacts/abinitio/latest/fpu_feed/hubbard_params.json" ]; then
  echo ">> Inverse harvest from FPU oracle..."
  python3 inverse_harvester.py --fpu-feed "$ROOT/artifacts/abinitio/latest/fpu_feed/hubbard_params.json" || true
fi
exit $RC
