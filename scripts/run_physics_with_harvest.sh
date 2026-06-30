#!/usr/bin/env bash
# Harvest extended sources → HEA + SC physics → FPU batch → provenance
# Author: Shan Yu
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PROXY="${FOLIATION_PROXY:-http://127.0.0.1:7892}"
export FOLIATION_PROXY="$PROXY" HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY"
export PASS_V1_ROOT="$ROOT"
LOG="$ROOT/artifacts/physics_with_harvest.log"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG"; }

ENGINE="${FOLIATION_ENGINE_ROOT:-$HOME/Foliation-Engine}"
if [[ -x "$ENGINE/bin/chip-cft-materialize" && -x "$ENGINE/bin/chip-physics-sim" ]]; then
  export FOLIATION_CFT_ROOT="${FOLIATION_CFT_ROOT:-$HOME/Foliation-CFT-Chip}"
  log "=== [0/4] CFT chip materialize + physics sim gate ==="
  (cd "$ENGINE" && bin/chip-cft-materialize && bin/chip-physics-sim) 2>&1 | tee -a "$LOG"
fi

log "=== [1/4] Data harvest (MP/JARVIS/AFLOW/OQMD + OPTIMADE/COD/NOMAD) ==="
bash "$ROOT/scripts/data_harvester/run_harvest.sh" --limit "${HARVEST_LIMIT:-600}" --max-ehull 0.08 2>&1 | tee -a "$LOG" || log "WARN: harvest partial"

log "=== [2/4] Physics full cycle (HEA SQS + SC killer) ==="
bash "$ROOT/scripts/run_physics_full_cycle.sh" 2>&1 | tee -a "$LOG" || log "WARN: physics cycle partial"

CFT="${FOLIATION_CFT_ROOT:-$HOME/Foliation-CFT-Chip}"
if [[ -f "$ENGINE/tools/chip_sc_fpu_batch.py" ]]; then
  log "=== [3/4] SC FPU batch via Foliation-Engine (limit=${SC_BATCH_LIMIT:-30}) ==="
  (
    cd "$ENGINE"
    export PASS_V1_ROOT="$ROOT"
    bin/chip-physics-sim >/dev/null
    python3 tools/chip_sc_fpu_batch.py --limit "${SC_BATCH_LIMIT:-30}" --steps "${SC_FPU_STEPS:-100}" --walkers "${SC_WALKERS:-4}" --skip-physics 2>&1 | tee -a "$LOG"
  ) || log "WARN: sc fpu batch partial"
elif [[ -d "$CFT/tools" ]]; then
  log "=== [3/4] CFT SC FPU batch (limit=3) ==="
  (
    cd "$CFT"
    export PASS_V1_ROOT="$ROOT"
    python3 tools/chip_sc_fpu_batch.py --limit 3 --steps 50 2>&1 | tee -a "$LOG"
  ) || log "WARN: sc fpu batch partial"
fi

log "=== [4/4] Provenance dry-run on latest SC run ==="
SC_RUN="$(readlink -f "$ROOT/artifacts/abinitio/latest" 2>/dev/null || echo "$ROOT/artifacts/abinitio/latest")"
if [[ -d "$SC_RUN" ]]; then
  bash "$ROOT/scripts/provenance/run_provenance_deposit.sh" "$SC_RUN" 2>&1 | tee -a "$LOG" || true
  python3 "$ROOT/scripts/provenance/upload_deposits_api.py" --root "$ROOT" --run "$SC_RUN" --dry-run 2>&1 | tee -a "$LOG" || true
fi

log "=== Done ==="
log "sources: $ROOT/artifacts/data_harvester/sources_manifest.json"
log "killer:  $ROOT/artifacts/data_harvester/killer_selection.json"
log "SC run:  $SC_RUN"
