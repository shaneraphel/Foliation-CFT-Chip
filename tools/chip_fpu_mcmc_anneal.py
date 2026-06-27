#!/usr/bin/env python3
"""14 维 Θ Metropolis 退火 — forge-daemon FPU(Θ) · BKT |dΣ/dμ|."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.chip_physics_sim_gate import exit_if_not_simulated  # noqa: E402

PASS_V1 = Path(os.environ.get("PASS_V1_ROOT", Path.home() / "Desktop/foliation-pass-v1"))
DEFAULT_RUN = PASS_V1 / "artifacts/abinitio/runs/killer_20260615_075958"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="14D FPU Metropolis: max J(Theta)=|dSigma/dmu| via forge-daemon"
    )
    ap.add_argument("--run", type=Path, default=DEFAULT_RUN, help="abinitio run 目录")
    ap.add_argument("--steps", type=int, default=100, help="退火步数（每 proposal ≈2 forge）")
    ap.add_argument("--delta-mu", type=float, default=0.05)
    ap.add_argument("--v-max", type=float, default=1.5)
    ap.add_argument("--lambda-penalty", type=float, default=10.0)
    ap.add_argument("--gamma", type=float, default=0.08)
    ap.add_argument("--t-start", type=float, default=1.0)
    ap.add_argument("--t-end", type=float, default=0.001)
    ap.add_argument("--eval-only", action="store_true", help="仅评估初始 Θ 的 J/L")
    ap.add_argument("--walkers", type=int, default=1, help=">1 时走 chip_fpu_mcmc_walkers 多 walker 并行")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--forge-output-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=ROOT / "out/chip/fpu_mcmc_anneal_report.json")
    args = ap.parse_args()
    exit_if_not_simulated(tool="chip_fpu_mcmc_anneal", require=("chip_a", "chip_b"))

    if args.walkers > 1:
        walkers = ROOT / "tools/chip_fpu_mcmc_walkers.py"
        cmd = [
            sys.executable,
            str(walkers),
            "--run",
            str(args.run),
            "--steps",
            str(args.steps),
            "--walkers",
            str(args.walkers),
            "--seed-base",
            str(args.seed),
            "--out",
            str(args.out),
        ]
        return subprocess.call(cmd, cwd=ROOT, env={**os.environ, "PASS_V1_ROOT": str(PASS_V1)})

    hubbard = args.run / "fpu_feed/hubbard_params.json"
    if not hubbard.is_file():
        print(f"缺少 {hubbard} — 先跑 fpu_feed_wannier.py", file=sys.stderr)
        return 2

    cmd = [
        "cargo",
        "run",
        "--release",
        "--quiet",
        "--manifest-path",
        str(ROOT / "crates/foliation-engine/Cargo.toml"),
        "--bin",
        "fpu-mcmc-anneal",
        "--",
        f"--hubbard={hubbard}",
        f"--fpu-feed={hubbard.parent}",
        f"--steps={args.steps}",
        f"--delta-mu={args.delta_mu}",
        f"--v-max={args.v_max}",
        f"--lambda={args.lambda_penalty}",
        f"--gamma={args.gamma}",
        f"--t-start={args.t_start}",
        f"--t-end={args.t_end}",
        f"--out={args.out}",
        f"--seed={args.seed}",
    ]
    if args.forge_output_dir:
        cmd.append(f"--forge-output-dir={args.forge_output_dir}")
    if args.eval_only:
        cmd.append("--eval-only")

    env = {**os.environ, "PASS_V1_ROOT": str(PASS_V1)}
    print(">>", " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT, env=env)
    if r.returncode != 0:
        return r.returncode

    if args.out.is_file():
        rep = json.loads(args.out.read_text(encoding="utf-8"))
        labels = rep.get("theta_labels") or []
        star = rep.get("theta_best") or []
        print(
            json.dumps(
                {
                    "theta_star": dict(zip(labels, star)) if labels else star,
                    "J_init": rep.get("j_init"),
                    "J_best": rep.get("j_best"),
                    "L_best": rep.get("l_best"),
                    "sigma_at_best": rep.get("sigma_at_best"),
                    "forge_calls": rep.get("forge_calls"),
                    "accepts": rep.get("accepts"),
                    "mass_production_ready": rep.get("mass_production_ready"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
