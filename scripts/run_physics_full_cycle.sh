#!/usr/bin/env bash
# Dual-regime physics: HEA (Cantor alloy SQS) + SC (killer ab-initio → FPU)
# Author: Shan Yu — verify before provenance upload
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PROXY="${FOLIATION_PROXY:-http://127.0.0.1:7892}"
export FOLIATION_PROXY="$PROXY" HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY"
LOG="$ROOT/artifacts/physics_full_cycle.log"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG"; }

log "=== Physics full cycle start ==="
ENGINE="${FOLIATION_ENGINE_ROOT:-$HOME/Foliation-Engine}"
if [[ -x "$ENGINE/bin/chip-cft-materialize" && -x "$ENGINE/bin/chip-physics-sim" ]]; then
  export PASS_V1_ROOT="$ROOT"
  export FOLIATION_CFT_ROOT="${FOLIATION_CFT_ROOT:-$HOME/Foliation-CFT-Chip}"
  log ">> [gate] CFT chip materialize + physics sim"
  (cd "$ENGINE" && bin/chip-cft-materialize && bin/chip-physics-sim) 2>&1 | tee -a "$LOG"
fi

# --- HEA: NiCoCrFeMn 5×5×5 SQS + emergence postprocess ---
HEA_ID="hea_$(date +%Y%m%d_%H%M%S)"
HEA_RUN="$ROOT/artifacts/abinitio/runs/${HEA_ID}"
mkdir -p "$HEA_RUN"/{sqs,lammps,qe,phonopy,wannier,fpu_feed,figures,hea_emergence}
log ">> [HEA] SQS supercell → $HEA_RUN"
python3 "$ROOT/scripts/abinitio_pipeline/sqs_preprocess.py" \
  --out "$HEA_RUN/sqs" --run-mcsqs --mc-anneal --replication 5 5 5
python3 "$ROOT/scripts/abinitio_pipeline/extract_wannier_metrics.py" --run "$HEA_RUN" || true
FOLIATION_REGIME=hea python3 "$ROOT/scripts/abinitio_pipeline/physical_firewalls.py" --run "$HEA_RUN" --regime hea || true
python3 "$ROOT/scripts/abinitio_pipeline/postprocess_hea_emergence.py" --run "$HEA_RUN" || true
bash "$ROOT/scripts/abinitio_pipeline/run_hea_emergence.sh" "$HEA_RUN" || true

# --- SC: use harvest killer_selection winner, else NbSe2 fallback ---
KILLER_JSON="$ROOT/artifacts/data_harvester/killer_selection.json"
if [[ -f "$KILLER_JSON" ]]; then
  SC_CIF="$(python3 -c "import json; d=json.load(open('$KILLER_JSON')); print(d['pipeline_winner']['path'])")"
  log ">> [SC] killer from harvest: $SC_CIF"
elif [[ -n "${SC_CIF:-}" && -f "${SC_CIF}" ]]; then
  log ">> [SC] SC_CIF env: $SC_CIF"
else
  SC_CIF="${SC_CIF:-/var/tmp/foliation-cif-cache/NbSe2_JVASP-655.cif}"
  if [[ ! -f "$SC_CIF" ]]; then
    SC_CIF="$(find "$ROOT/artifacts/data_harvester" -name 'MoS2*.cif' -o -name 'NbSe2*.cif' 2>/dev/null | head -1)"
  fi
  log ">> [SC] killer fallback CIF=$SC_CIF"
fi
export KILLER_CIF="$SC_CIF"
export FOLIATION_REGIME=hydrogen
bash "$ROOT/scripts/run_killer_pipeline.sh" 2>&1 | tee -a "$LOG" || log "WARN: killer pipeline partial"
SC_RUN="$(readlink -f "$ROOT/artifacts/abinitio/latest" 2>/dev/null || echo "$ROOT/artifacts/abinitio/latest")"

# --- FPU forge (chip B) if firewall allows ---
FW_ACTION="$(python3 -c "import json; print(json.load(open('$SC_RUN/firewall_report.json')).get('final_action',''))" 2>/dev/null || echo BLOCK)"
if [[ "$FW_ACTION" == "ALLOW" ]]; then
  log ">> [SC] forge-daemon Universal v3"
  HR="$SC_RUN/wannier/wannier90_hr.dat"
  if [[ -f "$HR" ]]; then
    python3 "$ROOT/scripts/fpu_feed_wannier.py" --hr "$HR" --out "$SC_RUN/fpu_feed" || true
    "$ROOT/foliation-opt-v2/target/release/forge-daemon" \
      --netlist artifacts/gcu_test/extensions_universal_v3.aig \
      --interaction-u 2.5 --mu 0.0 --once 2>&1 | tee -a "$LOG" || true
  fi
  bash "$ROOT/scripts/abinitio_pipeline/run_sc_validation.sh" "$SC_RUN" || true
else
  log ">> [SC] FPU blocked: final_action=$FW_ACTION — missing bader/phonon until full killer stages pass"
fi

# --- Provenance + upload ---
for RUN in "$HEA_RUN" "$SC_RUN"; do
  log ">> Provenance deposit: $RUN"
  bash "$ROOT/scripts/provenance/run_provenance_deposit.sh" "$RUN" || true
  python3 "$ROOT/scripts/provenance/upload_deposits_api.py" --root "$ROOT" --run "$RUN" --dry-run || true
done

log "=== Physics full cycle done ==="
log "HEA run: $HEA_RUN"
log "SC  run: $SC_RUN"
log "Review: hea_emergence/hea_emergence_verdict.json sc_validation/sc_macro_verdict.json"
