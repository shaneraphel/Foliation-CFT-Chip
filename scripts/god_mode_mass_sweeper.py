#!/usr/bin/env python3
"""Top-10 killer CIF → ab-initio → Wannier → 14D FPU MCMC 退火 → BKT 幸存者名单.

已废弃：一维 μ 穷举 (sweep_mu_and_record)。
核心武器：Foliation-Engine tools/chip_sc_fpu_batch.py + fpu_mcmc_annealing (forge-daemon).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import polars as pl

sys.path.append(os.path.join(os.path.dirname(__file__)))
from fpu_feed_wannier import extract_6bath_poles, parse_wannier_hr  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ENGINE = Path(os.environ.get("FOLIATION_ENGINE_ROOT", Path.home() / "Foliation-Engine"))
sys.path.insert(0, str(ENGINE))

from tools.chip_physics_sim_gate import exit_if_not_simulated  # noqa: E402

SC_FPU_BATCH = ENGINE / "tools" / "chip_sc_fpu_batch.py"
FPU_FEED = ROOT / "scripts" / "fpu_feed_wannier.py"
UNIVERSAL_V3_NETLIST = "artifacts/gcu_test/extensions_universal_v3.aig"
RESULTS_FILE = ROOT / "artifacts" / "mass_sweep_results.parquet"
BKT_SURVIVORS_CSV = ROOT / "artifacts" / "bkt_survivors.csv"
KILLER_JSON = ROOT / "artifacts" / "data_harvester" / "killer_selection.json"
PIPELINE_SH = ROOT / "scripts" / "run_killer_pipeline.sh"
BATCH_REPORT = ENGINE / "out" / "chip" / "sc_fpu_batch_report.json"
EXPORT_BLUEPRINT = ROOT / "scripts" / "oracle" / "export_theta_blueprint.py"
DEFAULT_J_BKT = 0.05
RUN_HOST_RE = re.compile(r">>\s*KILLER_RUN_HOST=(\S+)")

SURVIVOR_FIELDS = [
    "cif_name",
    "formula",
    "cif_path",
    "run_dir",
    "hr_path",
    "j_best",
    "l_best",
    "sigma_at_best",
    "j_init",
    "theta_U",
    "theta_mu",
    "theta_V",
    "theta_E",
    "pole_count",
    "bkt_survivor",
    "sc_fpu_anneal_ok",
    "status",
    "abinitio_status",
]


def resolve_cif_path(path: str) -> str:
    heavy = path.replace("/cif_ready/", "/heavy_cif_ready/")
    if os.path.isfile(heavy) and os.path.getsize(heavy) > 0:
        return heavy
    return path


def load_targets(limit: int = 0) -> list[dict]:
    with open(KILLER_JSON, encoding="utf-8") as f:
        data = json.load(f)
    seen: set[str] = set()
    targets: list[dict] = []
    pool = data.get("sweep_pool") or data.get("top10") or []
    for row in [data.get("pipeline_winner")] + pool:
        if not row:
            continue
        key = row.get("filename") or os.path.basename(row.get("path", ""))
        if key in seen:
            continue
        seen.add(key)
        item = dict(row)
        item["path"] = resolve_cif_path(item["path"])
        targets.append(item)
    if limit:
        targets = targets[:limit]
    return targets


def run_abinitio(cif_path: str) -> tuple[bool, str | None, Path | None]:
    env = os.environ.copy()
    env["KILLER_CIF"] = cif_path
    env.setdefault("FOLIATION_QE_OMP", os.environ.get("FOLIATION_QE_OMP", "4"))
    try:
        proc = subprocess.run(
            ["bash", str(PIPELINE_SH)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        m = RUN_HOST_RE.search(combined)
        run_dir = Path(m.group(1)) if m else None
        if proc.returncode != 0:
            return False, f"FAIL_ABINITIO:{combined[-300:]}", run_dir
        return True, None, run_dir
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as exc:
        return False, f"FAIL_ABINITIO:{exc}", None


def resolve_run_dir() -> Path | None:
    latest = ROOT / "artifacts" / "abinitio" / "latest"
    if latest.is_symlink() or latest.is_dir():
        return Path(os.path.realpath(latest))
    return None


def assert_universal_v3_netlist() -> None:
    netlist = ROOT / UNIVERSAL_V3_NETLIST
    if not netlist.is_file():
        raise FileNotFoundError(f"Universal v3 netlist missing: {UNIVERSAL_V3_NETLIST}")


def run_fpu_feed(run_dir: Path) -> tuple[bool, str]:
    hubbard = run_dir / "fpu_feed" / "hubbard_params.json"
    if hubbard.is_file():
        return True, "SKIP_EXISTING"
    hr = run_dir / "wannier" / "wannier90_hr.dat"
    if not hr.is_file():
        return False, "FAIL_NO_HR"
    try:
        proc = subprocess.run(
            [sys.executable, str(FPU_FEED), "--run", str(run_dir)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=900,
        )
        ok = proc.returncode == 0 and hubbard.is_file()
        return ok, (proc.stderr or proc.stdout or "")[-400:]
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as exc:
        return False, str(exc)


def run_fpu_mcmc_anneal(
    cif_name: str,
    run_dir: Path,
    hr_path: Path,
    *,
    steps: int,
    j_threshold: float,
    walkers: int = 1,
) -> dict:
    """Wannier 骨架 → 14D MCMC 退火 → 截获 Θ* / J_best / Σ."""
    assert_universal_v3_netlist()
    feed_ok, feed_tail = run_fpu_feed(run_dir)
    if not feed_ok:
        return {
            "cif_name": cif_name,
            "run_dir": str(run_dir),
            "hr_path": str(hr_path),
            "status": "FAIL_FPU_FEED",
            "abinitio_status": "OK",
            "sc_fpu_anneal_ok": False,
            "bkt_survivor": False,
            "feed_tail": feed_tail,
        }

    H_local, _ = parse_wannier_hr(hr_path)
    E_bath, V_hybrid = extract_6bath_poles(H_local, impurity_index=0)
    v_str = ",".join(map(str, V_hybrid))
    e_str = ",".join(map(str, E_bath))

    if not SC_FPU_BATCH.is_file():
        return {
            "cif_name": cif_name,
            "status": "FAIL_NO_CHIP_BATCH",
            "sc_fpu_anneal_ok": False,
            "bkt_survivor": False,
        }

    out_json = ENGINE / "out" / "chip" / f"{run_dir.name}_fpu_mcmc.json"
    env = {**os.environ, "PASS_V1_ROOT": str(ROOT), "FOLIATION_ENGINE_ROOT": str(ENGINE)}
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(SC_FPU_BATCH),
                "--runs",
                str(run_dir),
                "--steps",
                str(steps),
                "--walkers",
                str(walkers),
                "--skip-physics",
                "--out",
                str(BATCH_REPORT),
            ],
            cwd=ENGINE,
            env=env,
            capture_output=True,
            text=True,
            timeout=7200,
        )
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as exc:
        return {
            "cif_name": cif_name,
            "run_dir": str(run_dir),
            "status": f"FAIL_MCMC:{exc}",
            "sc_fpu_anneal_ok": False,
            "bkt_survivor": False,
            "v_str": v_str,
            "e_str": e_str,
        }

    anneal: dict = {}
    if out_json.is_file():
        anneal = json.loads(out_json.read_text(encoding="utf-8"))
    elif BATCH_REPORT.is_file():
        batch = json.loads(BATCH_REPORT.read_text(encoding="utf-8"))
        runs = batch.get("runs") or []
        if runs:
            anneal = (runs[0].get("fpu_anneal") or {}) | (runs[0].get("anneal_report") or {})

    star_path = run_dir / "fpu_feed" / "fpu_mcmc_theta_star.json"
    star: dict = {}
    if star_path.is_file():
        star = json.loads(star_path.read_text(encoding="utf-8"))

    j_best = anneal.get("j_best") or star.get("J_BKT")
    l_best = anneal.get("l_best") or star.get("L_best")
    j_init = anneal.get("j_init")
    sigma = anneal.get("sigma_at_best") or star.get("sigma_iwn")
    theta = star.get("theta_star") or anneal.get("theta_best") or []

    has_report = out_json.is_file() or star_path.is_file() or BATCH_REPORT.is_file()
    sc_ok = has_report and j_best is not None
    try:
        bkt = bool(sc_ok and float(j_best) > j_threshold)
    except (TypeError, ValueError):
        bkt = False

    if bkt and EXPORT_BLUEPRINT.is_file():
        try:
            subprocess.run(
                [sys.executable, str(EXPORT_BLUEPRINT), "--run", str(run_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
            pass

    row = {
        "cif_name": cif_name,
        "run_dir": str(run_dir),
        "hr_path": str(hr_path),
        "j_best": j_best,
        "l_best": l_best,
        "j_init": j_init,
        "sigma_at_best": sigma,
        "theta_U": theta[0] if len(theta) > 0 else star.get("U_eV"),
        "theta_mu": theta[1] if len(theta) > 1 else star.get("Mu_eV"),
        "theta_V": star.get("V_k_eV"),
        "theta_E": star.get("E_k_eV"),
        "pole_count": star.get("pole_count"),
        "v_str": v_str,
        "e_str": e_str,
        "sc_fpu_anneal_ok": sc_ok,
        "bkt_survivor": bkt,
        "status": "BKT_SURVIVOR" if bkt else ("OK" if sc_ok else "FAIL_MCMC"),
        "abinitio_status": "OK",
        "mcmc_exit": proc.returncode,
        "mcmc_stdout": (proc.stdout or "")[-600:],
    }
    if bkt:
        print(f"[BKT] {cif_name} J_best={j_best} > {j_threshold} Σ={sigma}")
    return row


def flush_results(
    existing_df: pl.DataFrame | None,
    new_rows: list[dict],
    *,
    replace_cif_names: set[str] | None = None,
) -> pl.DataFrame | None:
    if existing_df is not None and replace_cif_names and "cif_name" in existing_df.columns:
        existing_df = existing_df.filter(~pl.col("cif_name").is_in(list(replace_cif_names)))
    new_df = pl.DataFrame(new_rows) if new_rows else pl.DataFrame()
    if existing_df is not None and existing_df.height > 0:
        combined = pl.concat([existing_df, new_df], how="diagonal_relaxed") if new_df.height > 0 else existing_df
    else:
        combined = new_df
    if combined.height > 0:
        RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        combined.write_parquet(RESULTS_FILE)
    return combined


def append_survivor(row: dict, *, formula: str = "", cif_path: str = "") -> None:
    if not row.get("bkt_survivor"):
        return
    BKT_SURVIVORS_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not BKT_SURVIVORS_CSV.is_file()
    out_row = {k: row.get(k) for k in SURVIVOR_FIELDS}
    out_row["formula"] = formula
    out_row["cif_path"] = cif_path
    if isinstance(out_row.get("theta_V"), list):
        out_row["theta_V"] = json.dumps(out_row["theta_V"])
    if isinstance(out_row.get("theta_E"), list):
        out_row["theta_E"] = json.dumps(out_row["theta_E"])
    with BKT_SURVIVORS_CSV.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SURVIVOR_FIELDS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(out_row)
    print(f"[BKT SURVIVOR] {row.get('cif_name')} J_best={row.get('j_best')} Σ={row.get('sigma_at_best')}")


def nlp_preflight_one_line() -> str:
    """64 核 tick 单行摘要 — 零 cloud token."""
    sim = ENGINE / "bin" / "nlp-array-sim"
    if not sim.is_file():
        return "[NLP] skip — nlp-array-sim missing"
    try:
        p = subprocess.run(
            [str(sim), "--cores", "64", "god_mode 14D MCMC BKT top10"],
            cwd=ENGINE,
            capture_output=True,
            text=True,
            timeout=30,
        )
        line = (p.stdout or "").strip().splitlines()
        return line[-1][:200] if line else f"exit={p.returncode}"
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        return "[NLP] tick timeout"


def process_target(
    target: dict,
    *,
    steps: int,
    j_threshold: float,
    walkers: int,
) -> dict:
    cif_path = target["path"]
    cif_name = target.get("filename") or os.path.basename(cif_path)
    formula = target.get("formula", "")

    print(f"\n[RUN] abinitio → {cif_name}")
    try:
        ok, fail_status, run_dir = run_abinitio(cif_path)
    except Exception as exc:  # noqa: BLE001
        ok, fail_status, run_dir = False, f"FAIL_ABINITIO:{exc}", None

    if not ok:
        return {
            "cif_name": cif_name,
            "cif_path": cif_path,
            "formula": formula,
            "run_dir": str(run_dir) if run_dir else None,
            "status": fail_status or "FAIL_ABINITIO",
            "abinitio_status": fail_status or "FAIL_ABINITIO",
            "sc_fpu_anneal_ok": False,
            "bkt_survivor": False,
        }

    if run_dir is None:
        run_dir = resolve_run_dir()
    if not run_dir:
        return {
            "cif_name": cif_name,
            "cif_path": cif_path,
            "formula": formula,
            "status": "FAIL_NO_RUN",
            "abinitio_status": "OK",
            "sc_fpu_anneal_ok": False,
            "bkt_survivor": False,
        }

    hr_path = run_dir / "wannier" / "wannier90_hr.dat"
    if not hr_path.is_file():
        return {
            "cif_name": cif_name,
            "cif_path": cif_path,
            "formula": formula,
            "run_dir": str(run_dir),
            "status": "FAIL_NO_HR",
            "abinitio_status": "OK",
            "sc_fpu_anneal_ok": False,
            "bkt_survivor": False,
        }

    print(f"[MCMC] 14D anneal → {run_dir.name} (steps={steps}, walkers={walkers})")
    try:
        row = run_fpu_mcmc_anneal(
            cif_name,
            run_dir,
            hr_path,
            steps=steps,
            j_threshold=j_threshold,
            walkers=walkers,
        )
    except Exception as exc:  # noqa: BLE001
        row = {
            "cif_name": cif_name,
            "run_dir": str(run_dir),
            "status": f"FAIL_MCMC:{exc}",
            "abinitio_status": "OK",
            "sc_fpu_anneal_ok": False,
            "bkt_survivor": False,
        }

    row["cif_path"] = cif_path
    row["formula"] = formula
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="Top-10 SC god mode — 14D FPU MCMC (no 1D μ sweep)")
    ap.add_argument("--steps", type=int, default=100, help="每材料 MCMC 退火步数")
    ap.add_argument("--j-threshold", type=float, default=DEFAULT_J_BKT, help="BKT 临界 J_best 阈值")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 个 target（0=全部 sweep_pool）")
    ap.add_argument("--workers", type=int, default=1, help="材料级并行 pipeline 数（2 推荐于 10 核 Mac）")
    ap.add_argument("--walkers", type=int, default=1, help="每材料 MCMC 并行 walker 数")
    ap.add_argument(
        "--force",
        action="store_true",
        help="re-sweep targets even if already present in mass_sweep_results.parquet",
    )
    args = ap.parse_args()

    exit_if_not_simulated(tool="god_mode_mass_sweeper", require=("chip_a", "chip_b"))
    print(f"[NLP] {nlp_preflight_one_line()}")
    print(f"[GOD MODE] 14D MCMC engine → {SC_FPU_BATCH}")
    print(f"[GOD MODE] BKT threshold J_best > {args.j_threshold}")
    print(
        f"[GOD MODE] workers={args.workers} walkers={args.walkers} "
        f"QE_OMP={os.environ.get('FOLIATION_QE_OMP', '4')}"
    )

    existing_df: pl.DataFrame | None = None
    done: set[str] = set()
    if RESULTS_FILE.is_file():
        existing_df = pl.read_parquet(RESULTS_FILE)
        if "cif_name" in existing_df.columns and not args.force:
            done = set(existing_df["cif_name"].unique().to_list())

    targets = load_targets(args.limit)
    pending = []
    for target in targets:
        cif_name = target.get("filename") or os.path.basename(target["path"])
        if not args.force and cif_name in done:
            print(f"[SKIP] {cif_name} already in {RESULTS_FILE}")
            continue
        if args.force and cif_name in done:
            print(f"[FORCE] re-sweep {cif_name} (replacing parquet row on commit)")
        pending.append(target)

    parquet_lock = threading.Lock()

    def commit_row(row: dict) -> None:
        nonlocal existing_df
        with parquet_lock:
            append_survivor(row, formula=row.get("formula", ""), cif_path=row.get("cif_path", ""))
            replace = {row["cif_name"]} if args.force else None
            existing_df = flush_results(existing_df, [row], replace_cif_names=replace)
            done.add(row["cif_name"])
            print(f"[DONE] {row.get('cif_name')} status={row.get('status')} → {RESULTS_FILE}")

    if args.workers <= 1:
        for target in pending:
            row = process_target(
                target,
                steps=args.steps,
                j_threshold=args.j_threshold,
                walkers=args.walkers,
            )
            commit_row(row)
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futs = {
                pool.submit(
                    process_target,
                    target,
                    steps=args.steps,
                    j_threshold=args.j_threshold,
                    walkers=args.walkers,
                ): target
                for target in pending
            }
            for fut in as_completed(futs):
                row = fut.result()
                commit_row(row)

    print(f"\n[*] parquet → {RESULTS_FILE}")
    print(f"[*] survivors → {BKT_SURVIVORS_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
