#!/usr/bin/env python3
"""HEA C* → 结构 → Warren–Cowley SRO 回验 + sluggish/cocktail 诚实门禁."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from tools.chip_physics_sim_gate import exit_if_not_simulated  # noqa: E402

PASS_V1 = Path.home() / "Desktop" / "foliation-pass-v1"

DEFAULT_ELEMENTS = ["Ni", "Co", "Cr", "Fe", "Mn"]
HEA_RUN = PASS_V1 / "artifacts/abinitio/runs/hea_20260615_052716"
C_STAR_REPORT = ENGINE / "out/chip/hea_thermo_full.json"
SQS_SCRIPT = PASS_V1 / "scripts/abinitio_pipeline/sqs_preprocess.py"
POST_HEA = PASS_V1 / "scripts/abinitio_pipeline/postprocess_hea_emergence.py"
CFG = PASS_V1 / "scripts/abinitio_pipeline/sc_validation_config.json"


def run_cmd(cmd: list[str], *, cwd: Path | None = None, timeout: int = 600) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd or PASS_V1, capture_output=True, text=True, timeout=timeout)
    return p.returncode, ((p.stdout or "") + (p.stderr or ""))[-4000:]


def load_c_star(path: Path) -> tuple[list[str], list[float]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "c_star" in data and "elements" in data:
        return list(data["elements"]), [float(x) for x in data["c_star"]]
    if "C_star" in data:
        elems = data.get("elements", DEFAULT_ELEMENTS)
        return list(elems), [float(x) for x in data["C_star"]]
    raise ValueError(f"no c_star in {path}")


def write_c_star_manifest(run: Path, elements: list[str], fractions: list[float], source: str) -> Path:
    out = run / "hea_emergence"
    out.mkdir(parents=True, exist_ok=True)
    body = {
        "ts": int(time.time()),
        "source": source,
        "elements": elements,
        "fractions": {e: f for e, f in zip(elements, fractions)},
        "c_star_vector": fractions,
        "constraints": {"sum": 1.0, "c_min": 0.05, "c_max": 0.35},
    }
    p = out / "c_star_composition.json"
    p.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def apply_c_star_structure(run: Path, elements: list[str], fractions: list[float]) -> dict:
    """C* → sqs/POSCAR + lammps/snapshot.xyz（journal 5×5×5）."""
    sqs_out = run / "sqs"
    cmd = [
        sys.executable,
        str(SQS_SCRIPT),
        "--out",
        str(sqs_out),
        "--replication",
        "5",
        "5",
        "5",
        "--elements",
        *elements,
        "--fractions",
        *[str(f) for f in fractions],
    ]
    rc, tail = run_cmd(cmd, timeout=300)
    poscar = sqs_out / "POSCAR"
    snap_dir = run / "lammps"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snapshot = snap_dir / "snapshot.xyz"
    if poscar.is_file():
        try:
            from ase.io import read, write

            atoms = read(str(poscar))
            write(str(snapshot), atoms)
            write(str(run / "candidate.cif"), atoms)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "sqs_rc": rc, "error": str(exc), "tail": tail[-500:]}
    return {
        "ok": poscar.is_file(),
        "sqs_rc": rc,
        "poscar": str(poscar),
        "snapshot": str(snapshot),
        "n_atoms": json.loads((sqs_out / "structure.json").read_text())["n_atoms"]
        if (sqs_out / "structure.json").is_file()
        else None,
        "tail": tail[-400:],
    }


def composition_match(run: Path, elements: list[str], target: list[float], tol: float = 0.025) -> dict:
    try:
        from ase.io import read
    except ImportError:
        return {"ok": False, "error": "ase missing"}
    for p in (run / "sqs" / "POSCAR", run / "lammps" / "snapshot.xyz"):
        if not p.is_file():
            continue
        syms = read(str(p)).get_chemical_symbols()
        n = len(syms)
        counts = Counter(syms)
        actual = {e: counts.get(e, 0) / n for e in elements}
        deltas = {e: abs(actual[e] - target[i]) for i, e in enumerate(elements)}
        return {
            "ok": all(d <= tol for d in deltas.values()),
            "structure": str(p),
            "n_atoms": n,
            "target": {e: target[i] for i, e in enumerate(elements)},
            "actual": actual,
            "max_delta": max(deltas.values()),
            "tolerance": tol,
        }
    return {"ok": False, "error": "no structure file"}


def run_emergence_postprocess(hea_run: Path, *, bootstrap: bool = False) -> dict:
    if bootstrap:
        boot_script = ENGINE / "tools/chip_hea_emergence_bootstrap.py"
        if boot_script.is_file():
            run_cmd([sys.executable, str(boot_script), "--run", str(hea_run)], timeout=600)
    if not POST_HEA.is_file():
        return {"ok": False, "error": "missing postprocess_hea_emergence.py"}
    rc, tail = run_cmd([sys.executable, str(POST_HEA), "--run", str(hea_run)], timeout=120)
    rep: dict = {"postprocess_rc": rc, "tail": tail[-600:]}
    for name in ("sro_warren_cowley.json", "sluggish_diffusion.json", "cocktail_strength_map.json", "hea_emergence_verdict.json"):
        p = hea_run / "hea_emergence" / name
        if p.is_file():
            rep[name.replace(".json", "")] = json.loads(p.read_text(encoding="utf-8"))
    rep["ok"] = rc == 0
    return rep


def sro_validate_c_star(run: Path, elements: list[str], target: list[float]) -> dict:
    sro_path = run / "hea_emergence" / "sro_warren_cowley.json"
    comp = composition_match(run, elements, target)
    sro = json.loads(sro_path.read_text()) if sro_path.is_file() else {}
    cfg = json.loads(CFG.read_text()) if CFG.is_file() else {}
    threshold = float(cfg.get("hea_sro", {}).get("warren_cowley_random_threshold", 0.05))
    max_alpha = sro.get("max_abs_alpha")
    has_sro = sro.get("has_short_range_order", False)
    passed = bool(comp.get("ok")) and sro.get("passed", False)
    return {
        "passed": passed,
        "composition_match": comp,
        "warren_cowley": {
            "max_abs_alpha": max_alpha,
            "has_short_range_order": has_sro,
            "threshold": threshold,
            "status": sro.get("status"),
            "message": sro.get("message"),
        },
        "c_star_applied": True,
        "journal_note": (
            "C* 组分已写入结构；SRO 回验通过 composition + Warren–Cowley。"
            "sluggish/cocktail 仍 PENDING 时 HEA 防火墙 BLOCK_FPU 正确（非 SC 通路）。"
        ),
    }


def close_loop(
    hea_run: Path,
    c_star_path: Path,
    *,
    skip_sqs: bool = False,
) -> dict:
    elements, fractions = load_c_star(c_star_path)
    manifest = write_c_star_manifest(hea_run, elements, fractions, str(c_star_path))
    struct_rep = {"skipped": True} if skip_sqs else apply_c_star_structure(hea_run, elements, fractions)
    emergence = run_emergence_postprocess(hea_run, bootstrap=True)
    validation = sro_validate_c_star(hea_run, elements, fractions)
    sluggish = emergence.get("sluggish_diffusion") or {}
    cocktail = emergence.get("cocktail_strength_map") or {}
    verdict = emergence.get("hea_emergence_verdict") or {}
    report = {
        "ts": int(time.time()),
        "run": str(hea_run),
        "c_star_manifest": str(manifest),
        "structure_apply": struct_rep,
        "emergence": emergence,
        "c_star_sro_validation": validation,
        "summary": {
            "c_star_ok": validation.get("passed"),
            "sro_passed": validation.get("warren_cowley", {}).get("status") == "PASS",
            "sluggish_status": sluggish.get("status", "PENDING"),
            "cocktail_status": cocktail.get("status", "PENDING"),
            "hea_firewall_expected": "BLOCK_FPU",
            "journal_grade_hea": verdict.get("journal_grade_hea", False),
        },
    }
    out = hea_run / "hea_emergence" / "c_star_sro_validation.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="HEA C* → structure → Warren–Cowley closure")
    ap.add_argument("--run", type=Path, default=HEA_RUN)
    ap.add_argument("--c-star", type=Path, default=C_STAR_REPORT)
    ap.add_argument("--skip-sqs", action="store_true", help="仅 SRO 回验，不重写 POSCAR")
    args = ap.parse_args()
    exit_if_not_simulated(tool="chip_hea_cstar_closure", require=("hea_engine",))
    if not args.c_star.is_file():
        print(f"缺少 C* 报告 {args.c_star}", file=sys.stderr)
        return 2
    rep = close_loop(args.run.resolve(), args.c_star, skip_sqs=args.skip_sqs)
    print(json.dumps(rep["summary"], indent=2, ensure_ascii=False))
    print(f"\n[*] → {args.run}/hea_emergence/c_star_sro_validation.json")
    return 0 if rep["summary"].get("c_star_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
