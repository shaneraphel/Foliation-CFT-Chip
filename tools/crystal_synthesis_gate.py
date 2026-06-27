#!/usr/bin/env python3
"""Synthesis plausibility gate for chip-origin crystal candidates."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.element_universe import parse_formula_elements, screen_elements  # noqa: E402


def synthesis_gate(
    *,
    formula: str,
    track: str,
    external_verdict: dict[str, Any] | None = None,
    gcu_fastpath: dict[str, Any] | None = None,
) -> dict[str, Any]:
    elements = parse_formula_elements(formula)
    elem = screen_elements(elements)
    ext = external_verdict or {}
    fast = gcu_fastpath or {}

    stable = ext.get("verdict") == "PASS_STABLE" and ext.get("passed") is True
    proxy_ok = fast.get("verdict") in {"PASS_GCU_PHONOPY_FASTPATH", "PASS_GCU_PRESCREEN"}
    min_proxy = fast.get("min_proxy_cm1")
    proxy_soft = isinstance(min_proxy, (int, float)) and min_proxy < -20.0

    route_notes: list[str] = []
    if elem["volatile_elements"]:
        route_notes.append("sealed ampoule / controlled vapor pressure required")
    if elem["refractory_elements"]:
        route_notes.append("high-temperature arc/solid-state route likely")
    if track.upper() == "HEA":
        route_notes.append("arc-melt + homogenization + quench candidate")
    if track.upper() == "SC":
        route_notes.append("solid-state/chalcogen sealed-tube route candidate")

    chip_predicted_room_pressure_survivable = elem["all_allowed"] and not proxy_soft
    chip_predicted_room_temperature_survivable = chip_predicted_room_pressure_survivable and proxy_ok
    externally_confirmed_room_temperature_survivable = chip_predicted_room_temperature_survivable and stable
    synthesis_plausible = elem["all_allowed"] and bool(route_notes)

    missing: list[str] = []
    if not elem["all_allowed"]:
        missing.append("allowed natural element set")
    if not stable:
        missing.append("external PASS_STABLE validator verdict")
    if not proxy_ok:
        missing.append("GCU fastpath/prescreen pass")
    if not synthesis_plausible:
        missing.append("synthesis route evidence")

    return {
        "schema": "foliation.synthesis_gate.v1",
        "formula": formula,
        "track": track,
        "elements": elem,
        "gcu_fastpath_verdict": fast.get("verdict"),
        "external_verdict": ext.get("verdict"),
        "chip_predicted_room_pressure_survivable": chip_predicted_room_pressure_survivable,
        "chip_predicted_room_temperature_survivable": chip_predicted_room_temperature_survivable,
        "externally_confirmed_room_temperature_survivable": externally_confirmed_room_temperature_survivable,
        "synthesis_plausible": synthesis_plausible,
        "route_notes": route_notes,
        "missing_data": missing,
        "verdict": "PASS_SYNTHESIS_GATE" if not missing else "PENDING_SYNTHESIS_EVIDENCE",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--formula", required=True)
    ap.add_argument("--track", default="SC")
    ap.add_argument("--external-json", type=Path, default=None)
    ap.add_argument("--gcu-json", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    ext = json.loads(args.external_json.read_text(encoding="utf-8")) if args.external_json and args.external_json.is_file() else {}
    gcu = json.loads(args.gcu_json.read_text(encoding="utf-8")) if args.gcu_json and args.gcu_json.is_file() else {}
    if "gcu_phonopy_fastpath" in gcu:
        gcu = gcu["gcu_phonopy_fastpath"]
    body = synthesis_gate(formula=args.formula, track=args.track, external_verdict=ext, gcu_fastpath=gcu)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
