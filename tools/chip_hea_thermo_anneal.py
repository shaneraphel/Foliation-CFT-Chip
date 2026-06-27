#!/usr/bin/env python3
"""Cantor HEA 热力学退火 — Rust Metropolis 或 Python silent matrix sweep."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.chip_physics_sim_gate import exit_if_not_simulated  # noqa: E402
from tools.hea_silent_matrix_anneal import run_silent_matrix_anneal  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="HEA composition Metropolis / silent matrix annealing")
    ap.add_argument("--steps", type=int, default=200_000)
    ap.add_argument("--gamma", type=float, default=0.03)
    ap.add_argument("--temperature-k", type=float, default=298.15)
    ap.add_argument("--out", type=Path, default=ROOT / "out/chip/hea_thermo_anneal_report.json")
    ap.add_argument(
        "--element-pool",
        type=str,
        default="",
        help="Comma-separated transition-metal pool (enables silent matrix mode)",
    )
    ap.add_argument("--max-elements", type=int, default=5)
    ap.add_argument(
        "--target-config-entropy",
        choices=("max", "balanced"),
        default="max",
    )
    ap.add_argument(
        "--enthalpy-penalty",
        choices=("strict", "soft"),
        default="strict",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts/oracle/hea_recipes",
        help="Output directory for silent matrix recipes",
    )
    args = ap.parse_args()
    exit_if_not_simulated(tool="chip_hea_thermo_anneal", require=("hea_engine",))

    if args.element_pool.strip():
        report = run_silent_matrix_anneal(
            element_pool=[x.strip() for x in args.element_pool.split(",") if x.strip()],
            max_elements=args.max_elements,
            steps=args.steps,
            target_config_entropy=args.target_config_entropy,
            enthalpy_penalty=args.enthalpy_penalty,
            output_dir=args.output,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report.get("best"):
            c = report["best"]["c"]
            print(
                "C* = "
                + " ".join(f"{e}:{c[e]:.4f}" for e in report["best"]["elements"]),
                flush=True,
            )
        print(f"report → {report['out']}", flush=True)
        return 0

    cmd = [
        "cargo",
        "run",
        "--release",
        "--quiet",
        "--manifest-path",
        str(ROOT / "crates/foliation-engine/Cargo.toml"),
        "--bin",
        "hea-thermo-anneal",
        "--",
        f"--steps={args.steps}",
        f"--gamma={args.gamma}",
        f"--temperature-k={args.temperature_k}",
        f"--out={args.out}",
    ]
    print(">>", " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        return r.returncode
    if args.out.is_file():
        rep = json.loads(args.out.read_text(encoding="utf-8"))
        print(
            json.dumps(
                {
                    "c_star": dict(zip(rep.get("elements", []), rep.get("c_star", []))),
                    "l_best": rep.get("l_best"),
                    "delta_mismatch": rep.get("delta_mismatch"),
                    "steps_per_sec": rep.get("steps_per_sec"),
                    "mass_production_ready": rep.get("mass_production_ready"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
