#!/usr/bin/env python3
"""Phase 1 — export Θ* engineering blueprint (deterministic, zero-probability).

Reads fpu_feed/fpu_mcmc_theta_star.json (preferred) or hubbard_params + forge_dmft_summary,
writes blueprint_<run_id>.csv|.json and updates blueprint_manifest.json.

CSV column order matches Rust THETA_LABELS + sigma_iwn + J_BKT + L_best + provenance.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Rust: crates/foliation-engine/src/experimental_v2/fpu_mcmc_annealing.rs
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

CSV_COLUMNS: tuple[str, ...] = (
    *THETA_LABELS,
    "sigma_iwn",
    "J_BKT",
    "L_best",
    "run_id",
    "formula",
    "cif_path",
    "provenance",
)

DEFAULT_PASS_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = DEFAULT_PASS_ROOT / "artifacts" / "oracle" / "theta_blueprint"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _formula_from_cif(cif_path: Path) -> str | None:
    if not cif_path.is_file():
        return None
    for line in cif_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*data_(\S+)", line)
        if m:
            return m.group(1)
    return None


def _resolve_run_dir(run_arg: str, pass_root: Path) -> Path:
    p = Path(run_arg).expanduser()
    if p.is_dir():
        return p.resolve()
    candidate = pass_root / "artifacts" / "abinitio" / "runs" / run_arg
    if candidate.is_dir():
        return candidate.resolve()
    raise FileNotFoundError(f"run directory not found: {run_arg}")


def _load_theta_from_feed(feed_dir: Path) -> tuple[list[float], dict[str, Any]]:
    theta_path = feed_dir / "fpu_mcmc_theta_star.json"
    meta: dict[str, Any] = {"source_files": []}

    if theta_path.is_file():
        data = _read_json(theta_path)
        meta["source_files"].append(str(theta_path))
        if "theta_star" in data and len(data["theta_star"]) == len(THETA_LABELS):
            theta = [float(x) for x in data["theta_star"]]
        else:
            u = float(data["U_eV"])
            mu = float(data["Mu_eV"])
            v = [float(x) for x in data["V_k_eV"]]
            e = [float(x) for x in data["E_k_eV"]]
            if len(v) != 6 or len(e) != 6:
                raise ValueError("V_k_eV / E_k_eV must each have length 6")
            theta = [u, mu, *v, *e]
        meta.update(
            {
                "sigma_iwn": data.get("sigma_iwn"),
                "J_BKT": data.get("J_BKT"),
                "L_best": data.get("L_best"),
                "forge_calls": data.get("forge_calls"),
                "manifold": data.get("manifold"),
            }
        )
        return theta, meta

    hubbard = feed_dir / "hubbard_params.json"
    forge = feed_dir / "forge_dmft_summary.json"
    if not hubbard.is_file():
        raise FileNotFoundError(f"missing {theta_path.name} and {hubbard.name} under {feed_dir}")

    h = _read_json(hubbard)
    meta["source_files"].append(str(hubbard))
    u = float(h["U_eV"])
    mu = float(h["Mu_eV"])
    v = [float(x) for x in h["V_k_eV"]]
    e = [float(x) for x in h["E_k_eV"]]
    sigma_iwn = h.get("sigma_iwn")
    j_bkt = None
    l_best = None

    if forge.is_file():
        f = _read_json(forge)
        meta["source_files"].append(str(forge))
        sigma_iwn = f.get("sigma_iwn", sigma_iwn)
        j_bkt = f.get("J_BKT")
        l_best = f.get("L_best")

    theta = [u, mu, *v, *e]
    meta.update(
        {
            "sigma_iwn": sigma_iwn,
            "J_BKT": j_bkt,
            "L_best": l_best,
            "fallback": "hubbard_params+forge_dmft_summary",
        }
    )
    return theta, meta


def build_blueprint_record(
    run_dir: Path,
    pass_root: Path,
) -> dict[str, Any]:
    feed_dir = run_dir / "fpu_feed"
    if not feed_dir.is_dir():
        raise FileNotFoundError(f"missing fpu_feed under {run_dir}")

    theta, meta = _load_theta_from_feed(feed_dir)
    if len(theta) != len(THETA_LABELS):
        raise ValueError(f"theta dim {len(theta)} != {len(THETA_LABELS)}")

    run_id = run_dir.name
    cif_path = run_dir / "candidate.cif"
    formula = _formula_from_cif(cif_path) or run_id

    sigma_iwn = meta.get("sigma_iwn")
    j_bkt = meta.get("J_BKT")
    l_best = meta.get("L_best")

    row: dict[str, Any] = {label: theta[i] for i, label in enumerate(THETA_LABELS)}
    row["sigma_iwn"] = float(sigma_iwn) if sigma_iwn is not None else ""
    row["J_BKT"] = float(j_bkt) if j_bkt is not None else ""
    row["L_best"] = float(l_best) if l_best is not None else ""
    row["run_id"] = run_id
    row["formula"] = formula
    row["cif_path"] = str(cif_path.resolve()) if cif_path.is_file() else ""
    row["provenance"] = "+".join(Path(p).name for p in meta.get("source_files", []))

    record = {
        "schema": "foliation.theta_blueprint.v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "run_dir": str(run_dir.resolve()),
        "formula": formula,
        "theta_labels": list(THETA_LABELS),
        "theta_star": theta,
        "sigma_iwn": sigma_iwn,
        "J_BKT": j_bkt,
        "L_best": l_best,
        "provenance": meta,
        "csv_row": row,
    }
    return record


def write_blueprint(record: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = record["run_id"]
    csv_path = out_dir / f"blueprint_{run_id}.csv"
    json_path = out_dir / f"blueprint_{run_id}.json"

    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        writer.writerow(record["csv_row"])

    json_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return csv_path, json_path


def update_manifest(out_dir: Path, record: dict[str, Any]) -> Path:
    manifest_path = out_dir / "blueprint_manifest.json"
    manifest: dict[str, Any] = {"schema": "foliation.theta_blueprint_manifest.v1", "blueprints": []}
    if manifest_path.is_file():
        manifest = _read_json(manifest_path)

    entry = {
        "run_id": record["run_id"],
        "formula": record["formula"],
        "J_BKT": record.get("J_BKT"),
        "L_best": record.get("L_best"),
        "csv": str((out_dir / f"blueprint_{record['run_id']}.csv").resolve()),
        "json": str((out_dir / f"blueprint_{record['run_id']}.json").resolve()),
        "exported_at": record["exported_at"],
    }
    blueprints: list[dict[str, Any]] = manifest.setdefault("blueprints", [])
    blueprints = [b for b in blueprints if b.get("run_id") != record["run_id"]]
    blueprints.append(entry)
    manifest["blueprints"] = sorted(blueprints, key=lambda b: b["run_id"])
    j_vals = [b["J_BKT"] for b in manifest["blueprints"] if b.get("J_BKT") is not None]
    manifest["summary"] = {
        "count": len(manifest["blueprints"]),
        "J_max": max(j_vals) if j_vals else None,
        "formulas": sorted({b["formula"] for b in manifest["blueprints"]}),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


def export_run(run_dir: Path, pass_root: Path, out_dir: Path) -> dict[str, Any]:
    record = build_blueprint_record(run_dir, pass_root)
    csv_path, json_path = write_blueprint(record, out_dir)
    manifest_path = update_manifest(out_dir, record)
    summary = {
        "ok": True,
        "run_id": record["run_id"],
        "formula": record["formula"],
        "J_BKT": record.get("J_BKT"),
        "L_best": record.get("L_best"),
        "theta_dim": len(record["theta_star"]),
        "csv": str(csv_path),
        "json": str(json_path),
        "manifest": str(manifest_path),
    }
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description="Export Θ* engineering blueprint (Phase 1)")
    p.add_argument("--run", required=True, help="run_id or absolute path to killer run dir")
    p.add_argument("--pass-root", type=Path, default=DEFAULT_PASS_ROOT)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = p.parse_args()

    try:
        run_dir = _resolve_run_dir(args.run, args.pass_root.resolve())
        summary = export_run(run_dir, args.pass_root.resolve(), args.out_dir.resolve())
    except (FileNotFoundError, ValueError, KeyError) as exc:
        err = {"ok": False, "error": str(exc)}
        print(json.dumps(err, ensure_ascii=False))
        return 1

    manifest = _read_json(Path(summary["manifest"]))
    ms = manifest.get("summary", {})
    j_max = ms.get("J_max")
    formulas = ",".join(ms.get("formulas", []))
    j_str = f"{j_max:.3f}" if isinstance(j_max, (int, float)) else "na"
    print(
        f"blueprint={ms.get('count', 1)} rows J_max={j_str} formula={formulas}",
        file=sys.stderr,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
