#!/usr/bin/env bash
# sky130 OpenROAD tapeout for physics chips: CFT / FPU / QE-accel (no GCU, no NLP).
set -euo pipefail

ENGINE="$(cd "$(dirname "$0")/.." && pwd)"
PASS="${PASS_V1_ROOT:-$HOME/Desktop/foliation-pass-v1}"
YOSYS="${YOSYS:-$PASS/.tools/oss-cad-suite/bin/yosys}"
YS_SCRIPT="${ENGINE}/tools/evaluate_sky130.ys"
ROUTE_TCL="${ENGINE}/tools/sky130_route_chip.tcl"
LIB="${PASS}/benchmarks/pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib"
OUT_ROOT="${PASS}/artifacts/physics_tapeout/sky130"
IMAGE="${FOLIATION_OPENROAD_IMAGE:-openroad/orfs:latest}"
PLATFORM="${FOLIATION_OPENROAD_PLATFORM:-linux/amd64}"
OPENROAD_PATH="${FOLIATION_OPENROAD_PATH:-/OpenROAD-flow-scripts/tools/install/OpenROAD/bin}"

mkdir -p "$OUT_ROOT" "${PASS}/artifacts/reports"
"$ENGINE/bin/chip-cft-materialize" >/dev/null

chip_aig() {
  local chip="$1" pass_rel cft_aig
  case "$chip" in
    cft) pass_rel="artifacts/gcu_test/extensions_collapsed_v2.aig" ;;
    fpu) pass_rel="artifacts/gcu_test/extensions_universal_v3.aig" ;;
    qe_accel) pass_rel="artifacts/gcu_missing/fpu_extensions_raw.aig" ;;
    *) return 1 ;;
  esac
  cft_aig="${FOLIATION_CFT_ROOT:-$HOME/Foliation-CFT-Chip}/data/chips/$chip/netlist.aig"
  if [[ -s "$PASS/$pass_rel" ]]; then
    echo "$PASS/$pass_rel"
  elif [[ -s "$cft_aig" ]]; then
    echo "$cft_aig"
  else
    echo "$PASS/$pass_rel"
  fi
}

ensure_qe_aig() {
  local aig="$PASS/artifacts/gcu_missing/fpu_extensions_raw.aig"
  local cft_aig="${FOLIATION_CFT_ROOT:-$HOME/Foliation-CFT-Chip}/data/chips/qe_accel/netlist.aig"
  if [[ -s "$aig" ]]; then return 0; fi
  if [[ -s "$cft_aig" ]]; then
    mkdir -p "$(dirname "$aig")"
    cp "$cft_aig" "$aig"
    return 0
  fi
  echo "[*] building QE-accel AIG via run_missing_cores_local.sh"
  bash "$PASS/scripts/run_missing_cores_local.sh"
}

map_chip() {
  local chip="$1" aig top outdir
  aig="$(chip_aig "$chip")"
  top="foliation_${chip}_mapped"
  outdir="$OUT_ROOT/$chip"
  mkdir -p "$outdir"
  echo "[*] yosys sky130 map: $chip"
  (
    cd "$PASS"
    FOLIATION_SKY130_INPUT="$aig" \
    FOLIATION_SKY130_OUTPUT="$outdir/mapped_sky130.v" \
    FOLIATION_SKY130_TOP="$top" \
    FOLIATION_SKY130_LIB="$LIB" \
    "$YOSYS" -c "$YS_SCRIPT"
  ) >"$outdir/yosys_map.log" 2>&1
  cp "$aig" "$outdir/netlist.aig"
  echo "$top" >"$outdir/top_module.txt"
  echo "[*] mapped top=$top"
}

route_chip() {
  local chip="$1" top outdir netlist def log
  top="$(cat "$OUT_ROOT/$chip/top_module.txt")"
  outdir="$OUT_ROOT/$chip"
  netlist="$outdir/mapped_sky130.v"
  def="$outdir/final.def"
  log="${PASS}/artifacts/reports/openroad_route_${chip}.log"
  local route_rc=0
  if command -v openroad >/dev/null 2>&1; then
    echo "[*] host openroad route: $chip"
    local util="${FOLIATION_ROUTE_UTIL:-}"
    if [[ "$chip" == "qe_accel" && -z "$util" ]]; then util=12; fi
    FOLIATION_PASS_ROOT="$PASS" \
    FOLIATION_ROUTE_TOP="$top" \
    FOLIATION_ROUTE_NETLIST="$netlist" \
    FOLIATION_ROUTE_DEF="$def" \
    FOLIATION_ROUTE_UTIL="$util" \
    openroad -no_init "$ROUTE_TCL" | tee "$log" || route_rc=$?
  else
  echo "[*] docker openroad route: $chip"
  local util="${FOLIATION_ROUTE_UTIL:-}"
  if [[ "$chip" == "qe_accel" && -z "$util" ]]; then util=12; fi
  docker run --rm --platform "$PLATFORM" \
      -v "$PASS:/work" \
      -v "$ENGINE/tools:/tapeout_tools:ro" \
      -w /work \
      -e FOLIATION_PASS_ROOT=/work \
      -e FOLIATION_ROUTE_TOP="$top" \
      -e FOLIATION_ROUTE_NETLIST="/work/${netlist#$PASS/}" \
    -e FOLIATION_ROUTE_DEF="/work/${def#$PASS/}" \
    -e FOLIATION_ROUTE_UTIL="${util}" \
    -e PATH="${OPENROAD_PATH}:/usr/local/bin:/usr/bin:/bin" \
      "$IMAGE" \
      bash -lc "openroad -no_init /tapeout_tools/sky130_route_chip.tcl" \
      | tee "$log" || route_rc=$?
  fi
  if [[ ! -s "$def" ]]; then
    echo "ERROR: OpenROAD did not produce $def — see $log" >&2
    return 1
  fi
  if grep -q 'Wrote DEF (detailed)' "$log" 2>/dev/null; then
    echo "[*] $chip: detailed route OK"
  elif grep -q 'Wrote DEF (global)' "$log" 2>/dev/null; then
    echo "[*] $chip: global route only (DRT skipped)"
  else
    echo "[*] $chip: placement-only DEF"
  fi
  return 0
}

gds_from_def() {
  local chip="$1" def gds klayout
  def="$OUT_ROOT/$chip/final.def"
  gds="$OUT_ROOT/$chip/final.gds"
  klayout="$PASS/.tools/klayout/klayout.app/Contents/MacOS/klayout"
  [[ -s "$def" ]] || return 0
  if [[ ! -x "$klayout" ]]; then
    echo "[warn] KLayout missing — DEF only for $chip"
    return 0
  fi
  echo "[*] KLayout DEF->GDS: $chip"
  "$klayout" -b -rd "input=$def" -rd "output=$gds" -rd "root=$PASS" \
    -r "$PASS/scripts/klayout_def_to_gds.rb" \
    >>"${PASS}/artifacts/reports/klayout_${chip}.log" 2>&1 || true
}

write_manifest() {
  python3 - <<PY
import json, time
from pathlib import Path
root = Path("$OUT_ROOT")
chips = {}
for chip in ("cft", "fpu", "qe_accel"):
    d = root / chip
    if not d.is_dir():
        continue
    route_mode = None
    log = Path("$PASS") / "artifacts/reports" / f"openroad_route_{chip}.log"
    if log.is_file():
        txt = log.read_text(errors="replace")
        for mode in ("detailed", "global", "placement"):
            if f"Wrote DEF ({mode})" in txt:
                route_mode = mode
                break
    chips[chip] = {
        "top": (d / "top_module.txt").read_text().strip() if (d / "top_module.txt").is_file() else None,
        "route_mode": route_mode,
        "aig": str((d / "netlist.aig").relative_to(root)) if (d / "netlist.aig").is_file() else None,
        "mapped_v": str((d / "mapped_sky130.v").relative_to(root)) if (d / "mapped_sky130.v").is_file() else None,
        "def": str((d / "final.def").relative_to(root)) if (d / "final.def").is_file() else None,
        "gds": str((d / "final.gds").relative_to(root)) if (d / "final.gds").is_file() else None,
        "yosys_log": str((d / "yosys_map.log").relative_to(root)) if (d / "yosys_map.log").is_file() else None,
    }
manifest = {
    "schema": "foliation.physics_tapeout.sky130.v1",
    "pdk": "sky130_fd_sc_hd",
    "excludes": ["gcu_hamiltonian", "nlp_array"],
    "chips": chips,
    "ts": int(time.time()),
}
out = root / "MANIFEST.json"
out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
print(json.dumps(manifest, indent=2, ensure_ascii=False))
PY
}

CHIPS=(cft fpu qe_accel)
if [[ $# -gt 0 ]]; then CHIPS=("$@"); fi

ensure_qe_aig
for chip in "${CHIPS[@]}"; do
  map_chip "$chip"
  route_chip "$chip"
  gds_from_def "$chip"
done
write_manifest
echo "[*] tapeout artifacts: $OUT_ROOT"
du -sh "$OUT_ROOT"/* 2>/dev/null || true
