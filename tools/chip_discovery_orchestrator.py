#!/usr/bin/env python3
"""HEA + SC chip-origin discovery orchestrator.

Own chips solve; external validators only audit. Outputs a unified candidate
ledger for room-temperature/room-pressure survivability.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PASS_V1 = Path(os.environ.get("PASS_V1_ROOT", Path.home() / "Desktop" / "foliation-pass-v1"))
sys.path.insert(0, str(ROOT))

from tools.crystal_synthesis_gate import synthesis_gate  # noqa: E402


def read_json(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def formula_from_decode(decode: dict[str, Any]) -> str:
    accepted = decode.get("accepted_recipe") or {}
    formula = accepted.get("formula") or {}
    if formula.get("formula_pretty"):
        return formula["formula_pretty"]
    raw = decode.get("accepted_formula") or decode.get("formula_input") or ""
    return str(raw).replace("_", "")


def sc_candidate(run_id: str) -> dict[str, Any]:
    decode = read_json(PASS_V1 / "artifacts" / "oracle" / f"inverse_decode_{run_id}.json") or {}
    gcu = read_json(PASS_V1 / "artifacts" / "oracle" / f"gcu_prescreen_{run_id}.json") or {}
    fast = gcu.get("gcu_phonopy_fastpath") if isinstance(gcu, dict) else {}
    stability = read_json(PASS_V1 / "artifacts" / "oracle" / f"stability_verdict_{run_id}.json") or {}
    formula = formula_from_decode(decode if isinstance(decode, dict) else {})
    gate = synthesis_gate(
        formula=formula,
        track="SC",
        external_verdict=stability if isinstance(stability, dict) else {},
        gcu_fastpath=fast if isinstance(fast, dict) else {},
    )
    return {
        "track": "SC",
        "run_id": run_id,
        "formula": formula,
        "solver_of_record": [
            "WANNIER_FPU_FEED_FRONTEND_CHIP",
            "FPU_DMFT_THETA14_CHIP",
            "HAMILTONIAN_GCU_FIVE_CORE_PIPELINE",
            "NLP_PHYSICS_CHIP",
        ],
        "external_role": "validation_only",
        "decode_verdict": (decode or {}).get("verdict") if isinstance(decode, dict) else None,
        "gcu_verdict": (gcu or {}).get("verdict") if isinstance(gcu, dict) else None,
        "fastpath_verdict": (fast or {}).get("verdict") if isinstance(fast, dict) else None,
        "external_verdict": (stability or {}).get("verdict") if isinstance(stability, dict) else None,
        "synthesis_gate": gate,
    }


def hea_candidates(limit: int = 5) -> list[dict[str, Any]]:
    latest = read_json(ROOT / "artifacts" / "oracle" / "hea_recipes" / "latest.json")
    if latest is None:
        latest = read_json(PASS_V1 / "artifacts" / "oracle" / "hea_recipes" / "latest.json")
    if not isinstance(latest, dict):
        return []
    rows = latest.get("top_subsets") or ([latest.get("best")] if latest.get("best") else [])
    out: list[dict[str, Any]] = []
    queue_dir = ROOT / "artifacts" / "crystal_forge" / "hea_validation_queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    for idx, row in enumerate(rows[:limit], start=1):
        if not isinstance(row, dict):
            continue
        c = row.get("c") or {}
        elems = row.get("elements") or list(c)
        formula = "_".join(f"{e}{float(c.get(e, 0.0)):.2f}" for e in elems)
        gate = synthesis_gate(formula="".join(elems), track="HEA", gcu_fastpath={"verdict": "PASS_HEA_MATRIX_FASTPATH"})
        queue = {
            "schema": "foliation.hea_validation_queue_item.v1",
            "rank": idx,
            "formula": formula,
            "elements": elems,
            "fractions": {e: float(c.get(e, 0.0)) for e in elems},
            "solver_of_record": [
                "NLP_PHYSICS_CHIP",
                "GCU_TENSOR_CONTRACTION_CORE_8BIT",
                "GCU_GEOMETRY_COPROCESSOR",
            ],
            "external_role": "validation_only",
            "gcu_hijack": {
                "sqs_correlation_solver": "GCU_TENSOR_CONTRACTION_CORE_8BIT",
                "force_constant_fastpath": "GCU_GEOMETRY_COPROCESSOR",
                "external_validator": "SQS/QE/Phonopy audit only",
            },
            "commands": {
                "write_c_star_report": "materialize this JSON as c_star input",
                "sqs_closure": "bin/chip-hea-cstar-closure --c-star <queue-json>",
                "external_audit": "gcu-parasitic-validator equivalent for HEA after POSCAR exists",
            },
        }
        queue_path = queue_dir / f"hea_rank_{idx:02d}.json"
        queue_path.write_text(json.dumps(queue, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        out.append(
            {
                "track": "HEA",
                "formula": formula,
                "solver_of_record": [
                    "NLP_PHYSICS_CHIP",
                    "GCU_TENSOR_CONTRACTION_CORE_8BIT",
                    "GCU_GEOMETRY_COPROCESSOR",
                ],
                "external_role": "validation_only",
                "validation_queue": str(queue_path),
                "hea_matrix": row,
                "synthesis_gate": gate,
            }
        )
    return out


def parasite_plan(run_id: str) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(ROOT / "tools/gcu_parasitic_validator.py"),
        "--run-id",
        run_id,
        "--no-external",
    ]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600)
    try:
        body = json.loads(p.stdout)
    except json.JSONDecodeError:
        body = {"error": p.stderr[-1000:], "exit": p.returncode}
    return body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="killer_20260615_122305")
    ap.add_argument("--hea-limit", type=int, default=5)
    ap.add_argument("--out", type=Path, default=ROOT / "artifacts/crystal_forge/discovery_candidates.json")
    args = ap.parse_args()

    sc = sc_candidate(args.run_id)
    candidates = [sc] + hea_candidates(args.hea_limit)
    body = {
        "schema": "foliation.chip_discovery_orchestrator.v1",
        "principle": "own chips solve; external algorithms validate only and must be parasitized by GCU evidence",
        "run_id": args.run_id,
        "gcu_parasitic_validator_plan": parasite_plan(args.run_id),
        "gcu_validator_hijack_map": str(ROOT / "artifacts/crystal_forge/gcu_validator_hijack_map.json"),
        "candidates": candidates,
        "next_actions": [
            "complete current QE/Phonopy audit for calibration",
            "calibrate GCU min_proxy_cm1 against external min_frequency_cm-1",
            "route HEA top candidates through SQS -> GCU fastpath -> external validator",
            "promote only PASS_STABLE candidates to hall_of_fame",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(args.out), "candidate_count": len(candidates)}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
