#!/usr/bin/env python3
"""Multi-walker 14D FPU Metropolis — N 个独立 seed 并行，取 J_best 最大者."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from tools.chip_physics_sim_gate import exit_if_not_simulated  # noqa: E402

PASS_V1 = Path(os.environ.get("PASS_V1_ROOT", Path.home() / "Desktop/foliation-pass-v1"))
ANNEAL = ENGINE / "tools/chip_fpu_mcmc_anneal.py"


def _run_one_walker(
    run: str,
    steps: int,
    seed: int,
    walker_id: int,
    out_json: str,
    forge_archive: str,
) -> dict:
    env = {
        **os.environ,
        "PASS_V1_ROOT": str(PASS_V1),
        "FORGE_OUTPUT_DIR": forge_archive,
    }
    cmd = [
        sys.executable,
        str(ANNEAL),
        "--run",
        run,
        "--steps",
        str(steps),
        "--seed",
        str(seed),
        "--forge-output-dir",
        forge_archive,
        "--out",
        out_json,
    ]
    p = subprocess.run(cmd, cwd=ENGINE, env=env, capture_output=True, text=True, timeout=7200)
    rep: dict = {
        "walker_id": walker_id,
        "seed": seed,
        "exit": p.returncode,
        "out": out_json,
        "forge_archive": forge_archive,
        "stdout": (p.stdout or "")[-800:],
        "stderr": (p.stderr or "")[-400:],
    }
    out_path = Path(out_json)
    if out_path.is_file():
        data = json.loads(out_path.read_text(encoding="utf-8"))
        rep.update(
            {
                "j_best": data.get("j_best"),
                "j_init": data.get("j_init"),
                "l_best": data.get("l_best"),
                "sigma_at_best": data.get("sigma_at_best"),
                "theta_best": data.get("theta_best"),
                "mass_production_ready": data.get("mass_production_ready"),
            }
        )
    rep["ok"] = p.returncode == 0 and rep.get("j_best") is not None
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description="Parallel MCMC walkers — max J_best wins")
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--steps", type=int, default=100)
    ap.add_argument("--walkers", type=int, default=3)
    ap.add_argument("--seed-base", type=int, default=42)
    ap.add_argument("--out", type=Path, default=ENGINE / "out/chip/fpu_mcmc_walkers_report.json")
    args = ap.parse_args()
    exit_if_not_simulated(tool="chip_fpu_mcmc_walkers", require=("chip_a", "chip_b"))

    run = args.run.resolve()
    hubbard = run / "fpu_feed/hubbard_params.json"
    if not hubbard.is_file():
        print(f"缺少 {hubbard}", file=sys.stderr)
        return 2

    n = max(1, args.walkers)
    work = ENGINE / "out/chip/walkers" / run.name
    work.mkdir(parents=True, exist_ok=True)

    tasks: list[tuple[int, int, Path, Path]] = []
    for i in range(n):
        seed = args.seed_base + i * 9973
        out_json = work / f"walker_{i}.json"
        forge_dir = work / f"forge_w{i}"
        forge_dir.mkdir(parents=True, exist_ok=True)
        tasks.append((i, seed, out_json, forge_dir))

    results: list[dict] = []
    with ProcessPoolExecutor(max_workers=n) as pool:
        futs = {
            pool.submit(
                _run_one_walker,
                str(run),
                args.steps,
                seed,
                wid,
                str(out_json),
                str(forge_dir),
            ): wid
            for wid, seed, out_json, forge_dir in tasks
        }
        for fut in as_completed(futs):
            wid = futs[fut]
            try:
                rep = fut.result()
            except Exception as exc:  # noqa: BLE001
                rep = {"walker_id": wid, "ok": False, "error": str(exc)}
            results.append(rep)
            print(f"[walker {wid}] ok={rep.get('ok')} J_best={rep.get('j_best')}")

    ok_runs = [r for r in results if r.get("ok")]
    if not ok_runs:
        print("所有 walker 失败", file=sys.stderr)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"walkers": results, "ok": False}, indent=2), encoding="utf-8")
        return 1

    best = max(ok_runs, key=lambda r: float(r.get("j_best") or 0))
    best_out = Path(best["out"])
    merged = json.loads(best_out.read_text(encoding="utf-8"))
    merged["walkers"] = results
    merged["winning_walker"] = best.get("walker_id")
    merged["winning_seed"] = best.get("seed")

    final_report = args.out
    final_report.parent.mkdir(parents=True, exist_ok=True)
    final_report.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    shutil.copy2(best_out, ENGINE / "out/chip" / f"{run.name}_fpu_mcmc.json")

    star_src = work / f"walker_{best['walker_id']}.json"
    feed = run / "fpu_feed"
    star = {
        "manifold": merged.get("manifold"),
        "theta_labels": merged.get("theta_labels"),
        "theta_star": merged.get("theta_best"),
        "U_eV": (merged.get("theta_best") or [None])[0],
        "Mu_eV": (merged.get("theta_best") or [None, None])[1],
        "V_k_eV": (merged.get("theta_best") or [0] * 14)[2:8],
        "E_k_eV": (merged.get("theta_best") or [0] * 14)[8:14],
        "sigma_iwn": merged.get("sigma_at_best"),
        "J_BKT": merged.get("j_best"),
        "L_best": merged.get("l_best"),
        "winning_walker": best.get("walker_id"),
        "winning_seed": best.get("seed"),
    }
    (feed / "fpu_mcmc_theta_star.json").write_text(json.dumps(star, indent=2), encoding="utf-8")

    summary = {
        "walkers": n,
        "winning_walker": best.get("walker_id"),
        "J_best": merged.get("j_best"),
        "sigma_at_best": merged.get("sigma_at_best"),
        "report": str(final_report),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
