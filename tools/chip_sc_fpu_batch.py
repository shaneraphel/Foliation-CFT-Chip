#!/usr/bin/env python3
"""超导批量：killer runs → 14D FPU Metropolis → Θ* → physics 验算."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from tools.chip_physics_sim_gate import exit_if_not_simulated  # noqa: E402

PASS_V1 = Path.home() / "Desktop" / "foliation-pass-v1"
FPU_BATCH = ENGINE / "tools/chip_fpu_mcmc_anneal.py"
PHYSICS = ENGINE / "tools/physics_chip_verify.py"
RUNS = PASS_V1 / "artifacts/abinitio/runs"


def hubbard_for_run(run: Path) -> Path | None:
    p = run / "fpu_feed/hubbard_params.json"
    return p if p.is_file() else None


def discover_sc_runs(limit: int = 0) -> list[Path]:
    out: list[Path] = []
    if not RUNS.is_dir():
        return out
    for p in sorted(RUNS.glob("killer_*"), key=lambda x: x.stat().st_mtime, reverse=True):
        if hubbard_for_run(p):
            out.append(p)
        if limit and len(out) >= limit:
            break
    return out


def run_fpu_anneal(run: Path, steps: int, out_dir: Path, *, walkers: int = 1) -> dict:
    out = out_dir / f"{run.name}_fpu_mcmc.json"
    cmd = [
        sys.executable,
        str(FPU_BATCH),
        "--run",
        str(run),
        "--steps",
        str(steps),
        "--walkers",
        str(walkers),
        "--out",
        str(out),
    ]
    env = {**os.environ, "PASS_V1_ROOT": str(PASS_V1)}
    p = subprocess.run(cmd, cwd=ENGINE, capture_output=True, text=True, timeout=7200, env=env)
    rep: dict = {
        "run": str(run),
        "exit": p.returncode,
        "out": str(out),
        "stdout": (p.stdout or "")[-1500:],
        "stderr": (p.stderr or "")[-1500:],
    }
    if out.is_file():
        data = json.loads(out.read_text(encoding="utf-8"))
        rep["j_best"] = data.get("j_best")
        rep["j_init"] = data.get("j_init")
        rep["l_best"] = data.get("l_best")
        rep["sigma_at_best"] = data.get("sigma_at_best")
        rep["mass_production_ready"] = data.get("mass_production_ready")
        rep["theta_best"] = data.get("theta_best")
        rep["anneal_report"] = data
    star = run / "fpu_feed/fpu_mcmc_theta_star.json"
    rep["theta_star_path"] = str(star) if star.is_file() else None
    if star.is_file():
        rep["theta_star"] = json.loads(star.read_text(encoding="utf-8"))
    rep["sc_fpu_anneal_ok"] = p.returncode == 0 and rep.get("j_best") is not None
    rep["ok"] = rep["sc_fpu_anneal_ok"]
    return rep


def run_physics(run: Path) -> dict:
    cmd = [sys.executable, str(PHYSICS), "--run", str(run), "--skip-wannier"]
    p = subprocess.run(cmd, cwd=ENGINE, capture_output=True, text=True, timeout=7200)
    summary = {}
    report_path = PASS_V1 / "artifacts/physics_chip_verify_report.json"
    if report_path.is_file():
        summary = json.loads(report_path.read_text()).get("summary") or {}
    return {
        "exit": p.returncode,
        "summary": summary,
        "stdout": (p.stdout or "")[-800:],
        "ok": p.returncode == 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=Path, nargs="*", help="killer run 目录（默认自动发现）")
    ap.add_argument("--limit", type=int, default=3, help="最多处理 N 个 run")
    ap.add_argument("--steps", type=int, default=50, help="每 run FPU 退火步数")
    ap.add_argument("--walkers", type=int, default=1, help="MCMC 并行 walker 数")
    ap.add_argument("--skip-physics", action="store_true")
    ap.add_argument("--out", type=Path, default=ENGINE / "out/chip/sc_fpu_batch_report.json")
    args = ap.parse_args()
    exit_if_not_simulated(tool="chip_sc_fpu_batch", require=("chip_a", "chip_b"))

    runs = [r.resolve() for r in args.runs] if args.runs else discover_sc_runs(args.limit)
    if not runs:
        print("无可用 killer run（需 hubbard_params.json）", file=sys.stderr)
        return 2

    out_dir = args.out.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    batch: dict = {"ts": int(time.time()), "runs": [], "ok_count": 0}

    for run in runs:
        print(f">> SC FPU batch: {run.name}")
        item = {"run": str(run), "fpu_anneal": run_fpu_anneal(run, args.steps, out_dir, walkers=args.walkers)}
        item["anneal_report"] = item["fpu_anneal"].get("anneal_report")
        if not args.skip_physics:
            item["physics"] = run_physics(run)
        item["ok"] = item["fpu_anneal"].get("sc_fpu_anneal_ok", False) and (
            args.skip_physics or item.get("physics", {}).get("ok", False)
        )
        if item["ok"]:
            batch["ok_count"] += 1
        batch["runs"].append(item)

    batch["total"] = len(runs)
    args.out.write_text(json.dumps(batch, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"total": batch["total"], "ok": batch["ok_count"]}, indent=2))
    print(f"[*] → {args.out}")
    return 0 if batch["ok_count"] == batch["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
