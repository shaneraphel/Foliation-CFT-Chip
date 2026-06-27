#!/usr/bin/env python3
"""Phase 2c — deterministic Θ* → chemistry decode with supercell snap + FPU J_BKT gate.

Pipeline (zero-probability):
  1. Lookup decode (TM, ligand, dopant hint) from blueprint + physics_lookup_v1.json
  2. Continuous doping fraction x_theory from μ
  3. Snap x to legal supercell rationals (denom 3/4/9/16)
  4. Rigid μ_discrete reverse map from x_discrete
  5. FPU eval-only (forge-daemon via Foliation-Engine fpu-mcmc-anneal --eval-only)
  6. Accept integer formula if J_BKT ≥ threshold; else FAIL_FRAGILE_PHASE → next fraction
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

THETA_LABELS: tuple[str, ...] = (
    "U",
    "mu",
    "V0",
    "V1",
    "V2",
    "V3",
    "V4",
    "V5",
    "E0",
    "E1",
    "E2",
    "E3",
    "E4",
    "E5",
)

DEFAULT_PASS_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOOKUP = DEFAULT_PASS_ROOT / "artifacts" / "oracle" / "physics_lookup_v1.json"
DEFAULT_BLUEPRINT_DIR = DEFAULT_PASS_ROOT / "artifacts" / "oracle" / "theta_blueprint"
DEFAULT_OUT_DIR = DEFAULT_PASS_ROOT / "artifacts" / "oracle"
FOLIATION_ENGINE = Path(
    os.environ.get("FOLIATION_ENGINE_ROOT", Path.home() / "Foliation-Engine")
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _tm_and_ligand_elements(lookup: dict[str, Any]) -> tuple[list[str], list[str]]:
    tms: list[str] = []
    ligs: list[str] = []
    for sym, rec in lookup.get("elements", {}).items():
        roles = rec.get("role", [])
        if "transition_metal" in roles or "host" in roles:
            tms.append(sym)
        if "ligand" in roles:
            ligs.append(sym)
    return sorted(set(tms)), sorted(set(ligs))


def _predict_e_k(chi_tm: float, chi_l: float, rules: dict[str, Any]) -> float:
    alpha = float(rules["alpha_eV_per_delta_chi"])
    beta = float(rules["beta"])
    shift = float(rules["E_shift_eV"])
    return alpha * abs(chi_tm - chi_l) + beta * shift


def _predict_v_k(r_tm_pm: float, r_l_pm: float, rules: dict[str, Any]) -> float:
    gamma = float(rules["gamma"])
    a0 = float(rules["a0_A"])
    d = float(rules["d_bond_default_A"])
    overlap = 1.0 - (r_tm_pm + r_l_pm) / (2000.0 * 2.0 * d)
    overlap = max(0.05, min(1.0, overlap))
    return gamma * math.exp(-d / a0) * overlap


def parse_formula_elements(formula: str) -> list[str]:
    """Extract element symbols from pretty formula (e.g. MoS2, Nb1-xTaxS2 → Mo,S,Nb,Ta)."""
    return re.findall(r"[A-Z][a-z]?", formula or "")


def formula_from_cif(cif_path: Path) -> str | None:
    """Read reduced formula from candidate.cif (data_ line or pymatgen)."""
    if not cif_path.is_file():
        return None
    for line in cif_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*data_(\S+)", line)
        if m:
            return m.group(1)
    try:
        from pymatgen.core import Structure

        return Structure.from_file(str(cif_path)).composition.reduced_formula
    except Exception:
        return None


def resolve_parent_formula(blueprint: dict[str, Any], pass_root: Path) -> str | None:
    """Parent abinitio formula — blueprint field or candidate.cif under run_dir."""
    formula = blueprint.get("formula")
    if formula:
        return str(formula)
    run_dir = blueprint.get("run_dir")
    run_id = blueprint.get("run_id")
    if run_dir:
        rd = Path(run_dir)
    elif run_id:
        rd = pass_root / "artifacts" / "abinitio" / "runs" / str(run_id)
    else:
        return None
    return formula_from_cif(rd / "candidate.cif")


def formula_anchor(lookup: dict[str, Any], formula: str | None) -> tuple[str | None, str | None]:
    if not formula:
        return None, None
    elems = parse_formula_elements(formula)
    known = lookup.get("elements", {})
    tms = [e for e in elems if e in known and "transition_metal" in known[e].get("role", [])]
    ligs = [e for e in elems if e in known and "ligand" in known[e].get("role", [])]
    host = tms[0] if tms else None
    lig = ligs[0] if ligs else None
    return host, lig


def _pair_score(
    tm: str,
    lig: str,
    theta: list[float],
    lookup: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    elems = lookup["elements"]
    er = lookup["decode_rules"]["E_k_eV"]
    vr = lookup["decode_rules"]["V_k_eV"]
    ur = lookup["decode_rules"]["U_eV"]
    chi_tm = float(elems[tm]["pauling_en"])
    chi_l = float(elems[lig]["pauling_en"])
    r_tm = float(elems[tm]["shannon_radius_pm"])
    r_l = float(elems[lig]["shannon_radius_pm"])
    e_pred = _predict_e_k(chi_tm, chi_l, er)
    v_pred = _predict_v_k(r_tm, r_l, vr)
    e_tol = float(er["tolerance_eV"])
    v_tol = float(vr["tolerance_eV"])
    u_tol = float(ur["tolerance_eV"])

    u = theta[0]
    family = elems[tm].get("tm_family", "")
    u_band = lookup.get("tm_d_u_eV", {}).get(family, {})
    u_min = float(u_band.get("U_min", 0.0))
    u_max = float(u_band.get("U_max", 99.0))
    u_pen = 0.0 if u_min - u_tol <= u <= u_max + u_tol else min(abs(u - u_min), abs(u - u_max))

    e_vals = theta[8:14]
    v_vals = theta[2:8]
    e_hits = sum(1 for e in e_vals if abs(e - e_pred) <= e_tol)
    v_hits = sum(1 for v in v_vals if abs(v - v_pred) <= v_tol)
    e_err = sum(min(abs(e - e_pred), e_tol) for e in e_vals)
    v_err = sum(min(abs(v - v_pred), v_tol) for v in v_vals)
    miss = (6 - e_hits) + (6 - v_hits)
    score = miss * 10.0 + e_err + v_err + u_pen * 5.0
    detail = {
        "tm": tm,
        "ligand": lig,
        "E_pred_eV": e_pred,
        "V_pred_eV": v_pred,
        "E_bath_hits": e_hits,
        "V_bath_hits": v_hits,
        "E_band_err_eV": e_err,
        "V_band_err_eV": v_err,
        "U_penalty": u_pen,
        "total_L1": score,
    }
    return score, detail


def decode_host_ligand(theta: list[float], lookup: dict[str, Any], formula: str | None = None) -> dict[str, Any]:
    anchor_tm, anchor_lig = formula_anchor(lookup, formula)
    if anchor_tm and anchor_lig:
        score, detail = _pair_score(anchor_tm, anchor_lig, theta, lookup)
        return {
            "best_pair": detail,
            "candidates": [detail],
            "confidence": "formula_anchor",
            "formula_anchor": {"host_tm": anchor_tm, "ligand": anchor_lig},
        }
    tms, ligs = _tm_and_ligand_elements(lookup)
    ranked: list[dict[str, Any]] = []
    for tm in tms:
        for lig in ligs:
            score, detail = _pair_score(tm, lig, theta, lookup)
            ranked.append(detail)
    ranked.sort(key=lambda d: d["total_L1"])
    best = ranked[0]
    return {"best_pair": best, "candidates": ranked[:8], "confidence": "lookup_only"}


def select_dopant(host_tm: str, lookup: dict[str, Any]) -> str | None:
    for sym, rec in lookup.get("elements", {}).items():
        if rec.get("role") and "dopant_donor" in rec.get("role", []):
            if host_tm in rec.get("dopant_on", []):
                return sym
    return None


def legal_fractions(lookup: dict[str, Any]) -> list[dict[str, Any]]:
    grids = lookup.get("supercell_grids", [])
    seen: set[tuple[int, int]] = set()
    out: list[dict[str, Any]] = []
    for grid in grids:
        denom = int(grid["denominator"])
        name = grid["name"]
        for num in range(0, denom + 1):
            key = (num, denom)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "numerator": num,
                    "denominator": denom,
                    "x": num / denom,
                    "supercell": name,
                }
            )
    return sorted(out, key=lambda f: f["x"])


def x_theory_from_mu(mu_theta: float, lookup: dict[str, Any]) -> float:
    rules = lookup["decode_rules"].get("mu_discrete_reverse") or lookup["decode_rules"]["Mu_eV"]
    mu_ref = float(rules.get("mu_ref_eV", 0.0))
    slope = float(rules.get("mu_per_dopant_fraction_eV", 0.35))
    if slope <= 0:
        return 0.0
    # Continuous optimum for snap ordering (always defined).
    x = (mu_theta - mu_ref) / slope
    return max(0.0, min(1.0, x))


def snap_fractions_sorted(x_theory: float, lookup: dict[str, Any]) -> list[dict[str, Any]]:
    rules = lookup["decode_rules"].get("mu_discrete_reverse") or {}
    max_x = float(rules.get("max_dopant_fraction", 1.0))
    fracs = legal_fractions(lookup)
    uniq: dict[float, dict[str, Any]] = {}
    for f in fracs:
        if f["x"] == 0.0 or f["x"] <= max_x + 1e-12:
            uniq.setdefault(f["x"], f)
    ordered = sorted(uniq.values(), key=lambda f: (abs(f["x"] - x_theory), f["denominator"], f["numerator"]))
    unit = {"numerator": 0, "denominator": 1, "x": 0.0, "supercell": "1x1"}
    if 0.0 not in uniq:
        ordered.insert(0, unit)
    else:
        # Always FPU-gate pristine x=0 first, then nearest dopant fractions.
        nonzero = [f for f in ordered if f["x"] > 0]
        ordered = [uniq.get(0.0, unit), *nonzero]
    return ordered


def mu_discrete_from_x(x: float, lookup: dict[str, Any]) -> float:
    rules = lookup["decode_rules"].get("mu_discrete_reverse") or lookup["decode_rules"]["Mu_eV"]
    mu_ref = float(rules.get("mu_ref_eV", 0.0))
    slope = float(rules.get("mu_per_dopant_fraction_eV", 0.35))
    return mu_ref + slope * x


def rigid_backcalculate_mu(
    *,
    x_discrete: float,
    x_opt: float,
    mu_opt: float,
    lookup: dict[str, Any],
) -> float:
    """Pure algebraic chemical-potential snap.

    Phase 2c must not smooth the lattice fraction.  It snaps the atom-count
    topology first, then back-calculates the chemical potential with the
    phase-rigidity tensor supplied by the physics lookup.
    """
    rules = lookup["decode_rules"].get("mu_discrete_reverse") or lookup["decode_rules"]["Mu_eV"]
    rigidity = float(rules.get("mu_n_rigidity", rules.get("mu_per_dopant_fraction_eV", 0.35)))
    return mu_opt + rigidity * (x_discrete - x_opt)


def integer_formula(
    host: str,
    ligand: str,
    dopant: str | None,
    frac: dict[str, Any],
) -> dict[str, Any]:
    num = int(frac["numerator"])
    denom = int(frac["denominator"])
    if num == 0:
        # Undoped TMD: 2x2 ab supercell (build_decoded_structure default) for phonopy stability
        tmd = ligand in ("S", "Se", "Te")
        if tmd:
            return {
                "formula_pretty": f"{host}{ligand}2",
                "formula_subscript": f"{host}{ligand}2",
                "supercell": "2x2",
                "denominator": 1,
                "numerator": 0,
                "x_discrete": 0.0,
                "tm_sites": 4,
                "dopant_atoms": 0,
                "host_atoms": 4,
                "ligand_atoms": 8,
            }
        return {
            "formula_pretty": f"{host}{ligand}2",
            "formula_subscript": f"{host}{ligand}2",
            "supercell": "1x1",
            "denominator": 1,
            "numerator": 0,
            "x_discrete": 0.0,
            "tm_sites": 1,
            "dopant_atoms": 0,
            "host_atoms": 1,
            "ligand_atoms": 2,
        }
    n_tm = denom
    n_dop = num
    n_host = n_tm - n_dop
    n_lig = 2 * n_tm
    if n_dop == 0:
        pretty = f"{host}{n_tm}{ligand}{n_lig}"
        sub = f"{host}{n_tm}{ligand}{n_lig}"
    else:
        dop = dopant or "X"
        pretty = f"{host}{n_host}{dop}{n_dop}{ligand}{n_lig}"
        sub = f"{host}_{n_host}{dop}_{n_dop}{ligand}_{n_lig}"
    return {
        "formula_pretty": pretty,
        "formula_subscript": sub,
        "supercell": frac["supercell"],
        "denominator": denom,
        "numerator": num,
        "x_discrete": frac["x"],
        "tm_sites": n_tm,
        "dopant_atoms": n_dop,
        "host_atoms": n_host,
        "ligand_atoms": n_lig,
    }


def theta_with_mu(theta: list[float], mu: float) -> list[float]:
    out = list(theta)
    out[1] = mu
    return out


def write_hubbard_snapshot(theta: list[float], path: Path) -> None:
    body = {
        "U_eV": theta[0],
        "Mu_eV": theta[1],
        "V_k_eV": theta[2:8],
        "E_k_eV": theta[8:14],
        "source": "decode_theta_to_chemistry.fpu_recheck",
    }
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")


def fpu_eval_j_bkt(
    theta: list[float],
    *,
    pass_root: Path,
    delta_mu: float,
    forge_output_dir: Path | None,
    report_path: Path,
) -> dict[str, Any]:
    cargo_manifest = FOLIATION_ENGINE / "crates/foliation-engine/Cargo.toml"
    if not cargo_manifest.is_file():
        return {"ok": False, "error": f"missing {cargo_manifest}"}

    with tempfile.TemporaryDirectory(prefix="decode_theta_fpu_") as tmp:
        hubbard = Path(tmp) / "hubbard_recheck.json"
        write_hubbard_snapshot(theta, hubbard)
        cmd = [
            "cargo",
            "run",
            "--release",
            "--quiet",
            "--manifest-path",
            str(cargo_manifest),
            "--bin",
            "fpu-mcmc-anneal",
            "--",
            f"--hubbard={hubbard}",
            f"--delta-mu={delta_mu}",
            f"--out={report_path}",
            "--eval-only",
            "--steps=0",
        ]
        if forge_output_dir:
            cmd.append(f"--forge-output-dir={forge_output_dir}")
        env = {**os.environ, "PASS_V1_ROOT": str(pass_root)}
        proc = subprocess.run(cmd, cwd=FOLIATION_ENGINE, env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            return {
                "ok": False,
                "error": proc.stderr.strip() or proc.stdout.strip() or f"rc={proc.returncode}",
            }
        if not report_path.is_file():
            return {"ok": False, "error": f"missing report {report_path}"}
        rep = _read_json(report_path)
        return {
            "ok": True,
            "J_BKT": rep.get("j_best"),
            "L": rep.get("l_best"),
            "sigma_iwn": rep.get("sigma_at_best"),
            "forge_calls": rep.get("forge_calls"),
            "theta_evaluated": rep.get("theta_best"),
            "report_path": str(report_path),
        }


@dataclass
class DecodeResult:
    verdict: str
    payload: dict[str, Any]


def decode_blueprint(
    blueprint: dict[str, Any],
    lookup: dict[str, Any],
    *,
    j_threshold: float,
    delta_mu: float,
    pass_root: Path,
    skip_fpu: bool,
    max_fraction_tries: int,
) -> DecodeResult:
    theta = [float(x) for x in blueprint["theta_star"]]
    mu_theta = theta[1]
    parent_formula = resolve_parent_formula(blueprint, pass_root)
    if parent_formula and not blueprint.get("formula"):
        blueprint = {**blueprint, "formula": parent_formula}
    pair = decode_host_ligand(theta, lookup, blueprint.get("formula"))
    host = pair["best_pair"]["tm"]
    ligand = pair["best_pair"]["ligand"]
    dopant = select_dopant(host, lookup)

    x_theory = x_theory_from_mu(mu_theta, lookup)
    fracs = snap_fractions_sorted(x_theory, lookup)

    attempts: list[dict[str, Any]] = []
    accepted: dict[str, Any] | None = None
    report_dir = pass_root / "artifacts" / "oracle" / "fpu_recheck"
    report_dir.mkdir(parents=True, exist_ok=True)
    run_id = blueprint.get("run_id", "unknown")

    for i, frac in enumerate(fracs[:max_fraction_tries]):
        x_disc = float(frac["x"])
        if x_disc > 0 and not dopant:
            continue
        mu_disc = rigid_backcalculate_mu(
            x_discrete=x_disc,
            x_opt=x_theory,
            mu_opt=mu_theta,
            lookup=lookup,
        )
        theta_check = theta_with_mu(theta, mu_disc)
        formula = integer_formula(host, ligand, dopant if x_disc > 0 else None, frac)
        attempt: dict[str, Any] = {
            "try_index": len(attempts),
            "x_theory": x_theory,
            "x_discrete": x_disc,
            "fraction": f"{frac['numerator']}/{frac['denominator']}",
            "supercell": frac["supercell"],
            "mu_theta_eV": mu_theta,
            "mu_discrete_eV": mu_disc,
            "mu_backcalc_mode": "rigid_tensor_phase_2c",
            "theta_14d_snapped": theta_check,
            "formula": formula,
        }
        if skip_fpu:
            fpu = {"ok": True, "skipped": True, "J_BKT": blueprint.get("J_BKT")}
            attempt["fpu"] = fpu
            j_val = float(blueprint.get("J_BKT") or 0.0)
        else:
            rep_path = report_dir / f"recheck_{run_id}_{frac['numerator']}over{frac['denominator']}.json"
            fpu = fpu_eval_j_bkt(
                theta_check,
                pass_root=pass_root,
                delta_mu=delta_mu,
                forge_output_dir=report_dir / f"forge_{run_id}_{i}",
                report_path=rep_path,
            )
            attempt["fpu"] = fpu
            j_val = float(fpu.get("J_BKT") or 0.0) if fpu.get("ok") else 0.0

        if fpu.get("ok"):
            if j_val >= j_threshold:
                attempt["verdict"] = "PASS_SYNTHESIS_RECIPE"
                attempts.append(attempt)
                accepted = attempt
                break
            attempt["verdict"] = "FAIL_FRAGILE_PHASE"
            attempt["reason"] = "Topological collapse under strict lattice quantization. BKT gap closed."
        else:
            attempt["verdict"] = "FAIL_FPU_HARDWARE"
        attempts.append(attempt)

    if accepted:
        verdict = "PASS_SYNTHESIS_RECIPE"
    elif attempts:
        verdict = "FAIL_ALL_DISCRETE_FRACTIONS"
    else:
        verdict = "FAIL_NO_FRACTIONS"

    payload = {
        "schema": "foliation.inverse_decode.v1",
        "deterministic": True,
        "run_id": blueprint.get("run_id"),
        "formula_input": blueprint.get("formula"),
        "theta_star_ref": blueprint.get("run_dir"),
        "J_BKT_blueprint": blueprint.get("J_BKT"),
        "j_threshold": j_threshold,
        "decoded": {
            "host_tm": host,
            "ligand": ligand,
            "dopant_hint": {"element": dopant, "role": "raise_mu"} if dopant else None,
            "confidence": pair["confidence"],
            "constraints_satisfied": ["E_k band", "V_k overlap", "U window"],
            "pair_ranking": pair,
        },
        "doping": {
            "x_theory": x_theory,
            "mu_theta_eV": mu_theta,
            "mu_discrete_accepted_eV": accepted["mu_discrete_eV"] if accepted else None,
        },
        "accepted_recipe": accepted,
        "attempts": attempts,
        "verdict": verdict,
        "candidate_formulas": [
            a["formula"]["formula_subscript"]
            for a in attempts
            if a.get("verdict") == "PASS_SYNTHESIS_RECIPE"
        ]
        or ([accepted["formula"]["formula_subscript"]] if accepted else []),
    }
    return DecodeResult(verdict=verdict, payload=payload)


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 2c: Θ* → chemistry + discrete supercell + FPU gate")
    ap.add_argument("--blueprint", type=Path, help="blueprint_<run>.json path")
    ap.add_argument("--run-id", help="run_id under theta_blueprint/ (alternative to --blueprint)")
    ap.add_argument("--lookup", type=Path, default=DEFAULT_LOOKUP)
    ap.add_argument("--blueprint-dir", type=Path, default=DEFAULT_BLUEPRINT_DIR)
    ap.add_argument("--pass-root", type=Path, default=DEFAULT_PASS_ROOT)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--j-threshold", type=float, default=0.05)
    ap.add_argument("--delta-mu", type=float, default=0.05)
    ap.add_argument("--max-fraction-tries", type=int, default=12)
    ap.add_argument("--skip-fpu", action="store_true", help="decode + snap only; skip forge recheck")
    args = ap.parse_args()

    if args.blueprint:
        bp_path = args.blueprint.expanduser().resolve()
    elif args.run_id:
        bp_path = args.blueprint_dir / f"blueprint_{args.run_id}.json"
    else:
        print(json.dumps({"ok": False, "error": "need --blueprint or --run-id"}))
        return 2

    if not bp_path.is_file():
        print(json.dumps({"ok": False, "error": f"missing blueprint {bp_path}"}))
        return 2
    if not args.lookup.is_file():
        print(json.dumps({"ok": False, "error": f"missing lookup {args.lookup}"}))
        return 2

    blueprint = _read_json(bp_path)
    lookup = _read_json(args.lookup)
    parent_formula = resolve_parent_formula(blueprint, args.pass_root.resolve())
    if parent_formula and not blueprint.get("formula"):
        blueprint["formula"] = parent_formula
    result = decode_blueprint(
        blueprint,
        lookup,
        j_threshold=args.j_threshold,
        delta_mu=args.delta_mu,
        pass_root=args.pass_root.resolve(),
        skip_fpu=args.skip_fpu,
        max_fraction_tries=args.max_fraction_tries,
    )

    run_id = blueprint.get("run_id", "unknown")
    out_path = args.out_dir / f"inverse_decode_{run_id}.json"
    _write_json(out_path, result.payload)

    accepted = result.payload.get("accepted_recipe")
    formula = accepted["formula"]["formula_subscript"] if accepted else "none"
    j_acc = accepted.get("fpu", {}).get("J_BKT") if accepted else None
    j_str = f"{j_acc:.3f}" if isinstance(j_acc, (int, float)) else "na"
    print(
        f"decode={result.verdict} formula={formula} J_recheck={j_str} tries={len(result.payload.get('attempts', []))}",
        file=sys.stderr,
    )
    summary = {
        "ok": result.verdict == "PASS_SYNTHESIS_RECIPE",
        "verdict": result.verdict,
        "out": str(out_path),
        "accepted_formula": formula,
        "J_recheck": j_acc,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
