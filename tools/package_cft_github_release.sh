#!/usr/bin/env bash
# Foliation CFT GitHub release: SC/HEA computed data + pipeline scripts + CFT/FPU/QE chips.
# Excludes: GCU, NLP, EDA toolchain (yosys/openroad/foliation-eda), full Rust engine.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$HOME/Foliation-CFT-Chip}"
PASS="${PASS_V1_ROOT:-$HOME/Desktop/foliation-pass-v1}"
PRIOR="${FOLIATION_CFT_PRIOR:-$HOME/Foliation-CFT-Chip}"

echo "[*] Packaging CFT release (data + scripts + chips, no EDA) → $DEST"
rm -rf "$DEST"
mkdir -p "$DEST"/{scripts,tools,bin,data/sc,data/sc/runs,data/sc/oracle,data/hea,data/hea/run,data/forge,data/oracle,data/chips,boot/knowledge/sources}

# --- Pipeline scripts only (SC / HEA / FPU / forge; no EDA, no GCU, no agent infra) ---
TOOL_WHITELIST=(
  chip_sc_fpu_batch.py
  chip_fpu_mcmc_anneal.py
  chip_fpu_mcmc_walkers.py
  chip_dual_batch_sweeper.py
  chip_hea_thermo_anneal.py
  chip_hea_emergence_bootstrap.py
  chip_hea_cstar_closure.py
  crystal_forge_daemon.py
  crystal_evidence_ledger.py
  crystal_synthesis_gate.py
  chip_discovery_orchestrator.py
  physics_chip_verify.py
  chip_physics_sim_gate.py
  chip_data_harvest_pipeline.py
  foliation_artifact_paths.py
  foliation_icloud_mount.py
)
for name in "${TOOL_WHITELIST[@]}"; do
  [[ -f "$ROOT/tools/$name" ]] && cp "$ROOT/tools/$name" "$DEST/tools/"
done

BIN_WHITELIST=(
  chip-sc-fpu-batch
  chip-fpu-mcmc-anneal
  chip-hea-thermo-anneal
  chip-hea-emergence
  crystal-forge
  chip-dual-batch-sweeper
  chip-icloud-env.sh
  chip-icloud-mount
)
for name in "${BIN_WHITELIST[@]}"; do
  [[ -f "$ROOT/bin/$name" ]] && cp "$ROOT/bin/$name" "$DEST/bin/"
done
chmod +x "$DEST/bin/"* 2>/dev/null || true

# pass-v1 scripts used by SC decode / god-mode (not EDA)
PASS_SCRIPTS=(
  scripts/god_mode_mass_sweeper.py
  scripts/fpu_feed_wannier.py
  scripts/oracle/decode_theta_to_chemistry.py
  scripts/oracle/export_theta_blueprint.py
  scripts/oracle/phonopy_stability_gate.py
)
for rel in "${PASS_SCRIPTS[@]}"; do
  src="$PASS/$rel"
  [[ -f "$src" ]] || continue
  dst="$DEST/scripts/${rel#scripts/}"
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst" 2>/dev/null || true
done

# --- Chip artifacts: CFT / FPU / QE-accel (outputs only; no EDA runner) ---
if [[ -d "$PASS/artifacts/physics_tapeout/sky130" ]]; then
  for chip in cft fpu qe_accel; do
    src="$PASS/artifacts/physics_tapeout/sky130/$chip"
    dst="$DEST/data/chips/$chip"
    [[ -d "$src" ]] || continue
    mkdir -p "$dst"
    [[ -f "$src/netlist.aig" ]] && cp "$src/netlist.aig" "$dst/" 2>/dev/null || true
    [[ -f "$src/mapped_sky130.v" ]] && cp "$src/mapped_sky130.v" "$dst/" 2>/dev/null || true
    [[ -f "$src/final.def" ]] && cp "$src/final.def" "$dst/" 2>/dev/null || true
    [[ -f "$src/final.gds" ]] && cp "$src/final.gds" "$dst/" 2>/dev/null || true
    [[ -f "$src/top_module.txt" ]] && cp "$src/top_module.txt" "$dst/" 2>/dev/null || true
  done
  [[ -f "$PASS/artifacts/physics_tapeout/sky130/MANIFEST.json" ]] && \
    cp "$PASS/artifacts/physics_tapeout/sky130/MANIFEST.json" "$DEST/data/chips/MANIFEST.json" 2>/dev/null || true
fi
# Preserve chip tapeout from prior release if pass-v1 iCloud placeholders time out
if [[ -d "$PRIOR/data/chips" ]]; then
  rsync -a "$PRIOR/data/chips/" "$DEST/data/chips/" 2>/dev/null || true
fi

# --- Engine-side SC/HEA reports ---
ICLOUD="${FOLIATION_ICLOUD_ROOT:-}"
USE_ICLOUD="${FOLIATION_USE_ICLOUD:-0}"
if [[ "$USE_ICLOUD" != "0" && -n "$ICLOUD" && -d "$ICLOUD" ]]; then
  FORGE_ROOT="$ICLOUD/out/chip/crystal_forge"
  CHIP_OUT="$ICLOUD/out/chip"
else
  FORGE_ROOT="$ROOT/out/chip/crystal_forge"
  CHIP_OUT="$ROOT/out/chip"
fi

cp "$CHIP_OUT/sc_fpu_batch_report.json" "$DEST/data/sc/" 2>/dev/null || true
cp "$CHIP_OUT/dual_batch_sweeper_report.json" "$DEST/data/sc/" 2>/dev/null || true
cp "$CHIP_OUT/PHYSICS_SIM_OK.json" "$DEST/data/sc/" 2>/dev/null || true
cp "$CHIP_OUT/"*_fpu_mcmc.json "$DEST/data/sc/" 2>/dev/null || true
cp "$CHIP_OUT/blueprint_"*.json "$DEST/data/sc/" 2>/dev/null || true
cp "$CHIP_OUT/hea_thermo_full.json" "$DEST/data/hea/" 2>/dev/null || true

if [[ -d "$FORGE_ROOT" ]]; then
  cp "$FORGE_ROOT/state.json" "$DEST/data/forge/" 2>/dev/null || true
  cp "$FORGE_ROOT/cycle_"*.json "$DEST/data/forge/" 2>/dev/null || true
  tail -n 200 "$FORGE_ROOT/forge.log" > "$DEST/data/forge/forge_tail.log" 2>/dev/null || true
fi

if [[ -d "$PASS/artifacts/oracle/theta_blueprint" ]]; then
  cp "$PASS/artifacts/oracle/theta_blueprint/blueprint_"*.json "$DEST/data/oracle/" 2>/dev/null || true
  cp "$PASS/artifacts/oracle/theta_blueprint/blueprint_manifest.json" "$DEST/data/oracle/" 2>/dev/null || true
fi

# --- SC computed oracle (no gcu_prescreen) ---
ORACLE="$PASS/artifacts/oracle"
if [[ -d "$ORACLE" ]]; then
  cp "$ORACLE"/physics_lookup_v1.json "$DEST/data/sc/oracle/" 2>/dev/null || true
  for f in "$ORACLE"/inverse_decode_killer_*.json "$ORACLE"/stability_verdict_killer_*.json; do
    [[ -f "$f" ]] && cp "$f" "$DEST/data/sc/oracle/" 2>/dev/null || true
  done
  if [[ -d "$ORACLE/decoded_structures" ]]; then
    rsync -a "$ORACLE/decoded_structures/" "$DEST/data/sc/oracle/decoded_structures/" 2>/dev/null || true
  fi
  if [[ -d "$ORACLE/fpu_recheck" ]]; then
    rsync -a "$ORACLE/fpu_recheck/" "$DEST/data/sc/oracle/fpu_recheck/" 2>/dev/null || true
  fi
fi
# Preserve oracle/runs from prior release when pass-v1 is iCloud-evicted
if [[ -d "$PRIOR/data/sc/oracle" ]]; then
  rsync -a "$PRIOR/data/sc/oracle/" "$DEST/data/sc/oracle/" 2>/dev/null || true
fi
if [[ -d "$PRIOR/data/sc/runs" ]]; then
  rsync -a "$PRIOR/data/sc/runs/" "$DEST/data/sc/runs/" 2>/dev/null || true
fi
if [[ -d "$PRIOR/scripts" ]]; then
  rsync -a "$PRIOR/scripts/" "$DEST/scripts/" 2>/dev/null || true
fi

# --- SC killer runs: fpu_feed + CIF ---
RUNS="$PASS/artifacts/abinitio/runs"
if [[ -d "$RUNS" ]]; then
  SC_RUNS=()
  while IFS= read -r rid; do
    [[ -n "$rid" ]] && SC_RUNS+=("$rid")
  done < <(ROOT="$ROOT" CHIP_OUT="$CHIP_OUT" PASS="$PASS" python3 - <<'PY'
import json
import os
from pathlib import Path
root = Path(os.environ["ROOT"])
chip = Path(os.environ.get("CHIP_OUT", root / "out/chip"))
pass_r = Path(os.environ["PASS"])
seen = set()
batch = chip / "sc_fpu_batch_report.json"
if batch.is_file():
    try:
        for row in json.loads(batch.read_text()).get("runs", []):
            run = row.get("run", "")
            if run:
                seen.add(Path(run).name)
    except (OSError, TimeoutError, json.JSONDecodeError):
        pass
bp_dir = pass_r / "artifacts/oracle/theta_blueprint"
if bp_dir.is_dir():
    for p in bp_dir.glob("blueprint_killer_*.json"):
        try:
            seen.add(json.loads(p.read_text()).get("run_id") or p.stem.replace("blueprint_", ""))
        except (OSError, TimeoutError, json.JSONDecodeError):
            pass
oracle = pass_r / "artifacts/oracle"
if oracle.is_dir():
    for p in oracle.glob("inverse_decode_killer_*.json"):
        seen.add(p.stem.replace("inverse_decode_", ""))
    for p in oracle.glob("stability_verdict_killer_*.json"):
        seen.add(p.stem.replace("stability_verdict_", ""))
# Forge checkpoint sc_done (iCloud-safe)
forge_state = chip / "crystal_forge" / "state.json"
if forge_state.is_file():
    try:
        for rid in json.loads(forge_state.read_text()).get("sc_done", []):
            if rid:
                seen.add(rid)
    except (OSError, TimeoutError, json.JSONDecodeError):
        pass
for rid in sorted(seen):
    if rid.startswith("killer_"):
        print(rid)
PY
)
  for rid in ${SC_RUNS[@]+"${SC_RUNS[@]}"}; do
    src="$RUNS/$rid"
    dst="$DEST/data/sc/runs/$rid"
    [[ -d "$src" ]] || continue
    mkdir -p "$dst"
    [[ -f "$src/candidate.cif" ]] && cp "$src/candidate.cif" "$dst/" 2>/dev/null || true
    if [[ -d "$src/fpu_feed" ]]; then
      mkdir -p "$dst/fpu_feed"
      cp "$src/fpu_feed/"*.json "$dst/fpu_feed/" 2>/dev/null || true
    fi
  done
fi

cp "$PASS/artifacts/mass_sweep_results.parquet" "$DEST/data/sc/" 2>/dev/null || true
cp "$PASS/artifacts/bkt_survivors.csv" "$DEST/data/sc/" 2>/dev/null || true
cp "$PASS/artifacts/data_harvester/sources_manifest.json" "$DEST/data/" 2>/dev/null || true
cp "$PASS/artifacts/data_harvester/killer_selection.json" "$DEST/data/sc/" 2>/dev/null || true

mkdir -p "$DEST/scripts/data_harvester"
for name in common.py extended_sources.py heavy_harvester.py find_killer.py run_harvest.sh; do
  [[ -f "$PASS/scripts/data_harvester/$name" ]] && cp "$PASS/scripts/data_harvester/$name" "$DEST/scripts/data_harvester/"
done
chmod +x "$DEST/scripts/data_harvester/run_harvest.sh" 2>/dev/null || true
printf '%s\n' '# Copy to .env locally. https://materialsproject.org/api' 'MP_API_KEY=your_mp_api_key_here' \
  > "$DEST/scripts/data_harvester/.env.example"

# --- HEA computed run ---
HEA_RUN="hea_20260615_052716"
if [[ -d "$RUNS/$HEA_RUN" ]]; then
  HEA_DST="$DEST/data/hea/run/$HEA_RUN"
  mkdir -p "$HEA_DST"
  [[ -f "$RUNS/$HEA_RUN/candidate.cif" ]] && cp "$RUNS/$HEA_RUN/candidate.cif" "$HEA_DST/"
  [[ -d "$RUNS/$HEA_RUN/hea_emergence" ]] && rsync -a "$RUNS/$HEA_RUN/hea_emergence/" "$HEA_DST/hea_emergence/"
  [[ -d "$RUNS/$HEA_RUN/sqs" ]] && rsync -a "$RUNS/$HEA_RUN/sqs/" "$HEA_DST/sqs/"
  [[ -d "$RUNS/$HEA_RUN/lammps" ]] && rsync -a "$RUNS/$HEA_RUN/lammps/" "$HEA_DST/lammps/"
  if [[ -d "$RUNS/$HEA_RUN/fpu_feed" ]]; then
    mkdir -p "$HEA_DST/fpu_feed"
    cp "$RUNS/$HEA_RUN/fpu_feed/"*.json "$HEA_DST/fpu_feed/" 2>/dev/null || true
  fi
fi

cp "$ROOT/tools/README_CFT_RELEASE.template.md" "$DEST/README.md" 2>/dev/null || true

cat > "$DEST/.gitignore" <<'EOF'
.DS_Store
*.pyc
__pycache__/
.env
EOF

python3 - <<PY
import json, time
from pathlib import Path
dest = Path("$DEST")
manifest = {
    "package": "foliation-cft-chip",
    "version": "0.3.0",
    "ts": int(time.time()),
    "excludes": [
        "GCU verilog/AIG/ROM",
        "nlp_array",
        "EDA toolchain (yosys/openroad/foliation-eda)",
        "crates/foliation-engine",
        "chip_physics_sky130_tapeout",
    ],
    "includes": {
        "tools": sorted(p.name for p in (dest/"tools").glob("*.py")),
        "bin": sorted(p.name for p in (dest/"bin").iterdir() if p.is_file()),
        "scripts": sorted(str(p.relative_to(dest/"scripts")) for p in (dest/"scripts").rglob("*.py")),
        "data_sc": sorted(str(p.relative_to(dest/"data/sc")) for p in (dest/"data/sc").rglob("*") if p.is_file()),
        "data_hea": sorted(str(p.relative_to(dest/"data/hea")) for p in (dest/"data/hea").rglob("*") if p.is_file()),
        "data_chips": sorted(str(p.relative_to(dest/"data/chips")) for p in (dest/"data/chips").rglob("*") if p.is_file()),
        "data_forge": sorted(p.name for p in (dest/"data/forge").glob("*") if p.is_file()),
        "data_oracle": sorted(p.name for p in (dest/"data/oracle").glob("*") if p.is_file()),
    },
}
(dest/"data/MANIFEST.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False)+"\n")
print(json.dumps(manifest["includes"], indent=2, ensure_ascii=False))
PY

echo "[*] Done: $DEST"
du -sh "$DEST"
