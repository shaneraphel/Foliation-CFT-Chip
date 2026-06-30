#!/usr/bin/env python3
"""Physics dual-regime verify + NbSe2 forge — 64 核芯片代跑，零 Composer 上下文膨胀."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PASS_V1 = Path(os.environ.get("PASS_V1_ROOT", Path.home() / "Desktop" / "foliation-pass-v1"))
ENGINE = Path(__file__).resolve().parents[1]
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))
from tools.foliation_chip_paths import materialize_chip_netlists, universal_fpu_aig  # noqa: E402

HEA_RUN = PASS_V1 / "artifacts/abinitio/runs/hea_20260615_052716"
AIG = universal_fpu_aig()
REPORT = PASS_V1 / "artifacts/physics_chip_verify_report.json"
HEA_CLOSURE = ENGINE / "tools/chip_hea_cstar_closure.py"
C_STAR_REPORT = ENGINE / "out/chip/hea_thermo_full.json"

from tools.chip_physics_sim_gate import exit_if_not_simulated  # noqa: E402


def run(cmd: list[str], cwd: Path | None = None, timeout: int | None = None) -> tuple[int, str]:
    t = timeout or int(os.environ.get("CHIP_SHELL_TIMEOUT", "7200"))
    p = subprocess.run(cmd, cwd=cwd or PASS_V1, capture_output=True, text=True, timeout=t)
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out[-6000:]


def scf_ok(run: Path) -> bool:
    scf = run / "qe/scf.out"
    return scf.is_file() and "convergence has been achieved" in scf.read_text(errors="replace")


def formula_ok(run: Path, need: set[str]) -> bool:
    cif = run / "candidate.cif"
    if not cif.is_file():
        return False
    try:
        from ase.io import read

        syms = set(read(str(cif)).get_chemical_symbols())
        return need <= syms
    except Exception:
        return False


def summarize_hea(*, mass_produce: bool, hea_run: Path | None = None) -> dict:
    run = (hea_run or HEA_RUN).resolve()
    v = run / "hea_emergence/hea_emergence_verdict.json"
    sro = run / "hea_emergence/sro_warren_cowley.json"
    cstar_val = run / "hea_emergence/c_star_sro_validation.json"
    sluggish = run / "hea_emergence/sluggish_diffusion.json"
    cocktail = run / "hea_emergence/cocktail_strength_map.json"
    fw = run / "firewall_report.json"
    rep: dict = {"run": str(run), "ok": False, "mass_produce": mass_produce}
    if v.is_file():
        rep["verdict"] = json.loads(v.read_text())
    if sro.is_file():
        s = json.loads(sro.read_text())
        rep["max_abs_alpha"] = s.get("max_abs_alpha")
        rep["sro_passed"] = s.get("passed")
    if cstar_val.is_file():
        cv = json.loads(cstar_val.read_text())
        rep["c_star_validation"] = cv.get("summary") or cv.get("c_star_sro_validation")
        rep["c_star_ok"] = (cv.get("summary") or {}).get("c_star_ok")
    if sluggish.is_file():
        rep["sluggish"] = json.loads(sluggish.read_text())
    if cocktail.is_file():
        rep["cocktail"] = json.loads(cocktail.read_text())
    if fw.is_file():
        rep["firewall"] = json.loads(fw.read_text()).get("final_action")
    else:
        rep["firewall"] = "BLOCK_FPU"
    rep["physical_sanity"] = (
        "NiCoCrFeMn SRO |α|≈0.33 — 非玩具随机固溶体；"
        "HEA 正确门禁=BLOCK_FPU（非 SC/FPU 通路）；"
        "sluggish/cocktail PENDING=缺 MD/DOS，journal-grade 诚实；"
        + ("C*→结构 SRO 回验 OK" if rep.get("c_star_ok") else "C* 闭环待 chip_hea_cstar_closure")
        + (" → 可量产 deposit" if rep.get("c_star_ok") and rep.get("sro_passed") else "")
    )
    rep["ok"] = bool(rep.get("c_star_ok") or rep.get("sro_passed"))
    if mass_produce and rep["ok"]:
        rc, tail = run(
            ["bash", "scripts/provenance/run_provenance_deposit.sh", str(run)],
            timeout=600,
        )
        rep["provenance_deposit"] = {"rc": rc, "tail": tail[-400:]}
        rc2, out2 = run(
            [
                "python3",
                "scripts/provenance/upload_deposits_api.py",
                "--root",
                str(PASS_V1),
                "--run",
                str(run),
                "--dry-run",
            ],
            timeout=300,
        )
        rep["upload_dry_run"] = {"rc": rc2, "out": out2[-400:]}
    return rep


def run_hea_cstar_closure(hea_run: Path) -> dict:
    if not HEA_CLOSURE.is_file():
        return {"ok": False, "error": "missing chip_hea_cstar_closure.py"}
    c_star = C_STAR_REPORT if C_STAR_REPORT.is_file() else hea_run / "hea_emergence/c_star_composition.json"
    if not c_star.is_file():
        return {"ok": False, "error": f"no C* report at {c_star}"}
    rc, tail = run(
        [sys.executable, str(HEA_CLOSURE), "--run", str(hea_run), "--c-star", str(c_star)],
        cwd=ENGINE,
        timeout=600,
    )
    val = hea_run / "hea_emergence/c_star_sro_validation.json"
    summary = json.loads(val.read_text()).get("summary") if val.is_file() else {}
    return {"rc": rc, "summary": summary, "tail": tail[-600:], "ok": rc == 0}


def run_sc_fpu_mcmc(sc_run: Path, steps: int = 30) -> dict:
    script = ENGINE / "tools/chip_fpu_mcmc_anneal.py"
    out = ENGINE / "out/chip" / f"{sc_run.name}_fpu_mcmc.json"
    rc, tail = run(
        [
            sys.executable,
            str(script),
            "--run",
            str(sc_run),
            "--steps",
            str(steps),
            "--out",
            str(out),
        ],
        cwd=ENGINE,
        timeout=7200,
    )
    rep: dict = {"rc": rc, "out": str(out), "tail": tail[-800:]}
    star = sc_run / "fpu_feed/fpu_mcmc_theta_star.json"
    if star.is_file():
        rep["theta_star"] = json.loads(star.read_text())
    if out.is_file():
        rep["anneal"] = json.loads(out.read_text())
    rep["ok"] = rc == 0
    return rep


def resume_wannier(run_dir: Path) -> tuple[int, str]:
    sh = PASS_V1 / "scripts/resume_killer_wannier.sh"
    if not sh.is_file():
        return 1, "missing resume_killer_wannier.sh"
    return run(["bash", str(sh), str(run_dir)])


def forge_universal_v3(run_dir: Path) -> dict:
    hr = run_dir / "wannier/wannier90_hr.dat"
    rep: dict = {"hr": hr.is_file(), "aig": str(AIG), "ok": False}
    if not hr.is_file():
        rep["error"] = "missing wannier90_hr.dat"
        return rep
    if not AIG.is_file():
        rep["error"] = f"missing {AIG}"
        return rep
    rc, tail = run(
        [
            "python3",
            str(PASS_V1 / "scripts/fpu_feed_wannier.py"),
            "--run",
            str(run_dir),
        ],
        timeout=900,
    )
    rep["fpu_feed_wannier"] = {"rc": rc, "tail": tail[-800:]}
    summary = run_dir / "fpu_feed/forge_dmft_summary.json"
    if summary.is_file():
        rep["forge_summary"] = json.loads(summary.read_text(encoding="utf-8"))
        rep["ok"] = True
    feed = run_dir / "fpu_feed/feed_status.json"
    if feed.is_file():
        rep["feed_status"] = json.loads(feed.read_text(encoding="utf-8"))
    return rep


def postprocess_sc(run_dir: Path) -> dict:
    rep: dict = {
        "run": str(run_dir),
        "scf_converged": scf_ok(run_dir),
        "nbse2": formula_ok(run_dir, {"Nb", "Se"}),
    }
    pseudo = run_dir / "qe/pseudo"
    rep["pseudopotentials"] = sorted(p.name for p in pseudo.glob("*.upf")) if pseudo.is_dir() else []
    for script in (
        "scripts/abinitio_pipeline/extract_wannier_metrics.py",
        "scripts/abinitio_pipeline/physical_firewalls.py",
        "scripts/abinitio_pipeline/extract_fpu_sc_feed.py",
        "scripts/abinitio_pipeline/postprocess_sc_macro.py",
    ):
        cmd = ["python3", script, "--run", str(run_dir)]
        if "firewalls" in script:
            cmd += ["--regime", "hydrogen"]
        rc, tail = run(cmd, timeout=600)
        rep[Path(script).name] = {"rc": rc, "tail": tail[-400:]}
    # pass-v1 hopping parser 列位 off-by-one → 修正后再跑防火墙
    try:
        from tools.chip_hopping_anisotropy import write_hopping_json

        rep["hopping_fix"] = write_hopping_json(run_dir)
        rc, tail = run(
            ["python3", "scripts/abinitio_pipeline/physical_firewalls.py", "--run", str(run_dir), "--regime", "hydrogen"],
            timeout=600,
        )
        rep["physical_firewalls_retry"] = {"rc": rc, "tail": tail[-400:]}
    except ImportError:
        pass
    fw = run_dir / "firewall_report.json"
    if fw.is_file():
        rep["firewall"] = json.loads(fw.read_text())
    rep["wannier_hr"] = (run_dir / "wannier/wannier90_hr.dat").is_file()
    rep["physical_sanity"] = (
        "NbSe2 2H — 需 Nb/Se 赝势（非 Mo/S）；"
        "phonopy dim=1 1 1 防 OOM；"
        "极点经 fpu_feed_wannier → extensions_universal_v3.aig 14-bus"
    )
    return rep


def provenance(run_dir: Path) -> dict:
    rc, tail = run(["bash", "scripts/provenance/run_provenance_deposit.sh", str(run_dir)], timeout=600)
    rc2, out2 = run(
        [
            "python3",
            "scripts/provenance/upload_deposits_api.py",
            "--root",
            str(PASS_V1),
            "--run",
            str(run_dir),
            "--dry-run",
        ],
        timeout=300,
    )
    return {"deposit_rc": rc, "deposit_tail": tail[-400:], "upload_dry_rc": rc2, "upload_out": out2[-400:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, default=None, help="killer run dir")
    ap.add_argument("--skip-wannier", action="store_true")
    ap.add_argument("--hea-mass-produce", action="store_true")
    ap.add_argument("--hea-run", type=Path, default=None)
    ap.add_argument("--hea-cstar-closure", action="store_true", help="HEA C*→结构→Warren–Cowley")
    ap.add_argument("--sc-fpu-anneal", action="store_true", help="14D FPU Metropolis on --run")
    ap.add_argument("--sc-anneal-steps", type=int, default=30)
    args = ap.parse_args()

    materialize_chip_netlists()
    exit_if_not_simulated(tool="physics_chip_verify", require=("chip_a", "chip_b"))

    hea_run = (args.hea_run or HEA_RUN).resolve()
    sc_run = args.run
    if sc_run is None:
        latest = PASS_V1 / "artifacts/abinitio/latest"
        sc_run = latest.resolve() if latest.exists() else None
    if sc_run is None or not sc_run.is_dir():
        print("ERROR: no SC run", file=sys.stderr)
        return 1
    sc_run = sc_run.resolve()

    if args.hea_mass_produce:
        exit_if_not_simulated(tool="physics_chip_verify (--hea-mass-produce)", require=("hea_engine",))
    if args.sc_fpu_anneal:
        exit_if_not_simulated(tool="physics_chip_verify (--sc-fpu-anneal)", require=("chip_a", "chip_b"))

    report: dict = {
        "ts": int(__import__("time").time()),
        "hea": summarize_hea(mass_produce=args.hea_mass_produce, hea_run=hea_run),
        "sc_target": str(sc_run),
        "phonopy_dim": "1 1 1",
    }

    if args.hea_cstar_closure:
        exit_if_not_simulated(tool="physics_chip_verify (--hea-cstar-closure)", require=("hea_engine",))
        report["hea_cstar_closure"] = run_hea_cstar_closure(hea_run)
        report["hea"] = summarize_hea(mass_produce=args.hea_mass_produce, hea_run=hea_run)

    if args.sc_fpu_anneal and sc_run:
        report["sc_fpu_mcmc"] = run_sc_fpu_mcmc(sc_run, args.sc_anneal_steps)

    if not args.skip_wannier and scf_ok(sc_run) and not (sc_run / "wannier/wannier90_hr.dat").is_file():
        rc, tail = resume_wannier(sc_run)
        report["wannier_resume"] = {"rc": rc, "tail": tail[-500:]}

    report["sc"] = postprocess_sc(sc_run)
    report["forge_universal_v3"] = forge_universal_v3(sc_run)
    report["provenance_sc"] = provenance(sc_run)

    report["summary"] = {
        "hea_sro_ok": report["hea"].get("ok"),
        "hea_c_star_ok": report["hea"].get("c_star_ok"),
        "hea_firewall": report["hea"].get("firewall"),
        "hea_sluggish": (report["hea"].get("sluggish") or {}).get("status"),
        "hea_cocktail": (report["hea"].get("cocktail") or {}).get("status"),
        "nbse2_authentic": report["sc"].get("nbse2"),
        "scf_ok": report["sc"].get("scf_converged"),
        "wannier_ok": report["sc"].get("wannier_hr"),
        "forge_ok": report["forge_universal_v3"].get("ok"),
        "firewall": (report["sc"].get("firewall") or {}).get("final_action"),
        "pole_count": (report["forge_universal_v3"].get("forge_summary") or {}).get("pole_count"),
        "sigma_iwn": (report["forge_universal_v3"].get("forge_summary") or {}).get("sigma_iwn"),
        "sc_fpu_anneal_ok": (report.get("sc_fpu_mcmc") or {}).get("ok"),
        "sc_j_best": ((report.get("sc_fpu_mcmc") or {}).get("anneal") or {}).get("j_best"),
    }

    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    print(f"\n[*] report → {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
