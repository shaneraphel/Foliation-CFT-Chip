#!/usr/bin/env python3
"""双轨批量：HEA(C*→SRO) + SC(14D FPU Θ*) · NLP 阵列辅助编排."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from tools.chip_physics_sim_gate import exit_if_not_simulated  # noqa: E402
from tools.chip_nlp_bridge import nlp_array_tick, physics_phase6_brief  # noqa: E402

PASS_V1 = Path.home() / "Desktop" / "foliation-pass-v1"
HEA_RUN = PASS_V1 / "artifacts/abinitio/runs/hea_20260615_052716"
HEA_THERMO = ENGINE / "out/chip/hea_thermo_full.json"
HEA_CLOSURE = ENGINE / "tools/chip_hea_cstar_closure.py"
HEA_ANNEAL = ENGINE / "tools/chip_hea_thermo_anneal.py"
SC_BATCH = ENGINE / "tools/chip_sc_fpu_batch.py"
NLP_SYNC = ENGINE / "tools/chip_nlp_sync.py"
GOD_MODE = PASS_V1 / "scripts/god_mode_mass_sweeper.py"
REPORT = ENGINE / "out/chip/dual_batch_sweeper_report.json"


def run_py(script: Path, args: list[str], *, timeout: int = 7200, cwd: Path | None = None) -> dict:
    cmd = [sys.executable, str(script), *args]
    print(">>", " ".join(cmd))
    p = subprocess.run(cmd, cwd=cwd or ENGINE, capture_output=True, text=True, timeout=timeout)
    return {
        "cmd": cmd,
        "exit": p.returncode,
        "stdout": (p.stdout or "")[-2000:],
        "stderr": (p.stderr or "")[-800:],
        "ok": p.returncode == 0,
    }


def hea_pipeline(*, anneal_steps: int, skip_sqs: bool) -> dict:
    exit_if_not_simulated(tool="chip_dual_batch_sweeper (hea)", require=("hea_engine",))
    steps: dict = {}
    if not HEA_THERMO.is_file() or anneal_steps > 0:
        steps["thermo_anneal"] = run_py(
            HEA_ANNEAL,
            ["--steps", str(anneal_steps or 200000), "--out", str(HEA_THERMO)],
            timeout=120,
        )
    c_star = HEA_THERMO if HEA_THERMO.is_file() else ENGINE / "out/chip/hea_thermo_anneal_report.json"
    args = ["--run", str(HEA_RUN), "--c-star", str(c_star)]
    if skip_sqs:
        args.append("--skip-sqs")
    steps["cstar_closure"] = run_py(HEA_CLOSURE, args, timeout=600)
    val_path = HEA_RUN / "hea_emergence/c_star_sro_validation.json"
    summary = json.loads(val_path.read_text())["summary"] if val_path.is_file() else {}
    return {"steps": steps, "summary": summary, "ok": steps["cstar_closure"]["ok"]}


def sc_pipeline(*, limit: int, fpu_steps: int, walkers: int = 1) -> dict:
    exit_if_not_simulated(tool="chip_dual_batch_sweeper (sc)", require=("chip_a", "chip_b"))
    out = ENGINE / "out/chip/sc_fpu_batch_report.json"
    rep = run_py(
        SC_BATCH,
        [
            "--limit",
            str(limit),
            "--steps",
            str(fpu_steps),
            "--walkers",
            str(walkers),
            "--skip-physics",
            "--out",
            str(out),
        ],
        timeout=7200,
    )
    batch = json.loads(out.read_text()) if out.is_file() else {}
    return {"batch_run": rep, "batch": batch, "ok": rep["ok"]}


def god_mode_sc_sweep(*, dry_run: bool, workers: int, walkers: int, steps: int) -> dict:
    if not GOD_MODE.is_file():
        return {"ok": False, "error": "missing god_mode_mass_sweeper.py"}
    if dry_run:
        return {"ok": True, "skipped": True, "note": "dry-run — 未启动 pass-v1 sweep"}
    env = {**os.environ, "FOLIATION_ENGINE_ROOT": str(ENGINE), "PASS_V1_ROOT": str(PASS_V1)}
    env.setdefault("FOLIATION_QE_OMP", os.environ.get("FOLIATION_QE_OMP", "6"))
    cmd = [
        sys.executable,
        "-u",
        str(GOD_MODE),
        "--steps",
        str(steps),
        "--workers",
        str(workers),
        "--walkers",
        str(walkers),
    ]
    print(">>", " ".join(cmd))
    p = subprocess.run(cmd, cwd=ENGINE, env=env, capture_output=True, text=True, timeout=86400)
    return {
        "cmd": cmd,
        "exit": p.returncode,
        "stdout": (p.stdout or "")[-2000:],
        "stderr": (p.stderr or "")[-800:],
        "ok": p.returncode == 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="HEA + SC dual batch sweeper")
    ap.add_argument("--regime", choices=("hea", "sc", "both"), default="sc")
    ap.add_argument("--hea-anneal-steps", type=int, default=50000, help="HEA 矩阵退火步数；0=用已有 C*")
    ap.add_argument("--sc-limit", type=int, default=2, help="SC 批量 run 数")
    ap.add_argument("--sc-fpu-steps", type=int, default=30, help="每 run 14D FPU 退火步数")
    ap.add_argument("--sc-walkers", type=int, default=3, help="SC 每 run MCMC walker 数")
    ap.add_argument("--god-mode", action="store_true", help="额外跑 pass-v1 god_mode sweep")
    ap.add_argument("--god-mode-dry", action="store_true", help="仅打印 god_mode 计划")
    ap.add_argument("--parallel-tracks", action="store_true", help="regime=both 时 HEA 与 SC 并行")
    ap.add_argument("--skip-sqs", action="store_true", help="Bypass SQS structure generation")
    ap.add_argument("--god-workers", type=int, default=2)
    ap.add_argument("--god-walkers", type=int, default=3)
    ap.add_argument("--nlp-sync", action="store_true", default=True)
    args = ap.parse_args()

    nlp_ctx = nlp_array_tick(
        f"dual batch regime={args.regime} HEA C* SRO SC FPU 14D parallel={args.parallel_tracks} {physics_phase6_brief()}",
        cores=64,
    )
    print(f"[NLP 64核预检]\n{nlp_ctx[:1200]}\n")

    report: dict = {
        "ts": int(time.time()),
        "regime": args.regime,
        "parallel_tracks": args.parallel_tracks,
        "nlp_preflight": nlp_ctx[:800],
    }

    if args.regime == "both" and args.parallel_tracks:
        print("\n=== HEA ∥ SC 双轨并行 ===")
        with ThreadPoolExecutor(max_workers=2) as pool:
            hea_f = pool.submit(hea_pipeline, anneal_steps=args.hea_anneal_steps, skip_sqs=args.skip_sqs)
            sc_f = pool.submit(
                sc_pipeline,
                limit=args.sc_limit,
                fpu_steps=args.sc_fpu_steps,
                walkers=args.sc_walkers,
            )
            report["hea"] = hea_f.result()
            report["sc"] = sc_f.result()
    else:
        if args.regime in ("hea", "both"):
            print("\n=== HEA 批量：C* → 结构 → Warren–Cowley ===")
            report["hea"] = hea_pipeline(anneal_steps=args.hea_anneal_steps, skip_sqs=args.skip_sqs)

        if args.regime in ("sc", "both"):
            print("\n=== SC 批量：14D FPU Metropolis → Θ* ===")
            report["sc"] = sc_pipeline(
                limit=args.sc_limit,
                fpu_steps=args.sc_fpu_steps,
                walkers=args.sc_walkers,
            )

    if args.god_mode:
        print("\n=== god_mode_mass_sweeper（并行 sweep · Top30 pool）===")
        report["god_mode"] = god_mode_sc_sweep(
            dry_run=args.god_mode_dry,
            workers=args.god_workers,
            walkers=args.god_walkers,
            steps=args.sc_fpu_steps,
        )

    if args.nlp_sync:
        report["nlp_sync"] = run_py(NLP_SYNC, [], timeout=120)

    report["summary"] = {
        "hea_ok": (report.get("hea") or {}).get("ok"),
        "sc_ok": (report.get("sc") or {}).get("ok"),
        "hea_c_star": ((report.get("hea") or {}).get("summary") or {}).get("c_star_ok"),
        "hea_sluggish": ((report.get("hea") or {}).get("summary") or {}).get("sluggish_status"),
        "hea_cocktail": ((report.get("hea") or {}).get("summary") or {}).get("cocktail_status"),
        "sc_batch_ok": (report.get("sc") or {}).get("ok"),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n" + json.dumps(report["summary"], indent=2, ensure_ascii=False))
    print(f"[*] → {REPORT}")
    ok = all(
        v
        for k, v in report["summary"].items()
        if k.endswith("_ok") and v is not None
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
