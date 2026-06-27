#!/usr/bin/env python3
"""Phase 3b — Phonopy stability gate: QE forces → band.yaml → imaginary mode verdict.

Gate (default): min frequency must be > -20 cm^-1 across BZ (phonopy band.yaml, THz→cm^-1).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_PASS_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DECODE_DIR = DEFAULT_PASS_ROOT / "artifacts" / "oracle"
THZ_TO_CM1 = 33.3564085199
DEFAULT_IMAGINARY_CM1 = -20.0


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_band_yaml_min_cm1(band_yaml: Path) -> dict[str, Any]:
    freqs_thz: list[float] = []
    if not band_yaml.is_file():
        return {"ok": False, "error": f"missing {band_yaml}"}

    try:
        import yaml

        data = yaml.safe_load(band_yaml.read_text(encoding="utf-8"))
        for ph in data.get("phonon", []):
            for band in ph.get("band", []):
                freqs_thz.extend(float(x) for x in band.get("frequency", []))
    except Exception:
        for line in band_yaml.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.search(r"frequency:\s*([-+0-9.eE]+)", line)
            if m:
                freqs_thz.append(float(m.group(1)))

    if not freqs_thz:
        return {"ok": False, "error": "no frequencies parsed from band.yaml"}

    min_thz = min(freqs_thz)
    max_thz = max(freqs_thz)
    min_cm1 = min_thz * THZ_TO_CM1
    max_cm1 = max_thz * THZ_TO_CM1
    imaginary = sum(1 for f in freqs_thz if f < -1e-6)
    return {
        "ok": True,
        "min_frequency_THz": min_thz,
        "max_frequency_THz": max_thz,
        "min_frequency_cm-1": min_cm1,
        "max_frequency_cm-1": max_cm1,
        "imaginary_mode_points": imaginary,
        "n_frequencies": len(freqs_thz),
        "band_yaml": str(band_yaml.resolve()),
    }


def gate_verdict(
    band_stats: dict[str, Any],
    *,
    threshold_cm1: float,
    docker_log: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not band_stats.get("ok"):
        err = band_stats.get("error", "unknown")
        if "missing" in str(err) and docker_log and docker_log.get("returncode") not in (None, 0):
            return {
                "passed": False,
                "verdict": "FAIL_QE_PHONOPY_INCOMPLETE",
                "threshold_cm-1": threshold_cm1,
                "reason": docker_log.get("stderr_tail", err)[:500],
            }
        return {
            "passed": False,
            "verdict": "FAIL_NO_PHONON_DATA",
            "threshold_cm-1": threshold_cm1,
            "reason": band_stats.get("error", "unknown"),
        }
    min_cm1 = float(band_stats["min_frequency_cm-1"])
    passed = min_cm1 > threshold_cm1
    verdict = "PASS_STABLE_LATTICE" if passed else "FAIL_IMAGINARY_PHONON"
    return {
        "passed": passed,
        "verdict": verdict,
        "threshold_cm-1": threshold_cm1,
        "min_frequency_cm-1": min_cm1,
        "imaginary_mode_points": band_stats.get("imaginary_mode_points", 0),
        "reason": None if passed else f"min {min_cm1:.2f} cm-1 <= {threshold_cm1} cm-1",
    }


def setup_phonopy_run(
    *,
    pass_root: Path,
    run_dir: Path,
    poscar: Path,
    formula: str,
) -> None:
    sqs = run_dir / "sqs"
    ph = run_dir / "phonopy"
    qe = run_dir / "qe" / "pseudo"
    sqs.mkdir(parents=True, exist_ok=True)
    ph.mkdir(parents=True, exist_ok=True)
    qe.mkdir(parents=True, exist_ok=True)

    poscar_text = poscar.read_text(encoding="utf-8")
    (sqs / "POSCAR").write_text(poscar_text, encoding="utf-8")
    ph.mkdir(parents=True, exist_ok=True)
    for stale in ph.glob("POSCAR-*"):
        stale.unlink(missing_ok=True)
    for stale in ph.glob("disp-*"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
    for name in ("phonopy_disp.yaml", "SPOSCAR", "band.yaml", "total_dos.dat"):
        p = ph / name
        if p.is_file():
            p.unlink()
    (ph / "POSCAR-unitcell").write_text(poscar_text, encoding="utf-8")

    band_conf_src = (
        pass_root / "docker/foliation-abinitio/templates/killer/phonopy/band.conf"
    )
    if band_conf_src.is_file():
        (ph / "band.conf").write_text(band_conf_src.read_text(encoding="utf-8"), encoding="utf-8")

    fetch = pass_root / "scripts/abinitio_pipeline/fetch_pseudopotentials.py"
    elems = sorted(set(re.findall(r"[A-Z][a-z]?", formula)))
    have = sum(1 for e in elems if (qe / f"{e}.upf").is_file())
    if fetch.is_file() and have < len(elems):
        subprocess.run(
            [sys.executable, str(fetch), "--elements", *elems, "--out", str(qe)],
            cwd=pass_root,
            check=False,
        )

    prep = pass_root / "scripts/abinitio_pipeline/prepare_qe_from_cif.py"
    if prep.is_file():
        subprocess.run(
            [sys.executable, str(prep), "--run", str(run_dir), "--poscar", str(poscar)],
            cwd=pass_root,
            check=False,
        )


def run_phonopy_docker(*, pass_root: Path, run_container: str) -> dict[str, Any]:
    image = os.environ.get("FOLIATION_ABINITIO_IMAGE", "foliation-abinitio:latest")
    proxy = os.environ.get("FOLIATION_PROXY", "http://127.0.0.1:7892")
    omp = os.environ.get("FOLIATION_QE_OMP", "4")
    gcu_mode = os.environ.get("FOLIATION_QE_GCU_MODE", "")
    gcu_sidecar = os.environ.get("FOLIATION_QE_GCU_SIDECAR", "")
    gcu_wrapper_dir = os.environ.get("FOLIATION_QE_GCU_WRAPPER_DIR", "")
    gcu_interposer_src = os.environ.get("FOLIATION_QE_GCU_INTERPOSER_SRC", "")
    gcu_interposer_so = os.environ.get("FOLIATION_QE_GCU_INTERPOSER_SO", "")
    gcu_force_interpose = os.environ.get("FOLIATION_GCU_FORCE_INTERPOSE", "")
    gcu_interpose_max_ops = os.environ.get("FOLIATION_GCU_INTERPOSE_MAX_OPS", "")
    gcu_force_large_zgemm = os.environ.get("FOLIATION_GCU_FORCE_LARGE_ZGEMM", "")
    gcu_large_zgemm_k = os.environ.get("FOLIATION_GCU_LARGE_ZGEMM_K", "")
    gcu_tile_m = os.environ.get("FOLIATION_GCU_TILE_M", "")
    gcu_tile_n = os.environ.get("FOLIATION_GCU_TILE_N", "")
    gcu_tile_k = os.environ.get("FOLIATION_GCU_TILE_K", "")
    check = subprocess.run(["docker", "images", "-q", image], capture_output=True, text=True)
    if not check.stdout.strip():
        return {"ok": False, "error": f"docker image missing: {image}"}

    script_container = "/foliation_project/scripts/oracle/run_phonopy_oracle.sh"
    cmd = [
        "docker",
        "run",
        "--rm",
        "-e",
        f"HTTP_PROXY={proxy}",
        "-e",
        f"HTTPS_PROXY={proxy}",
        "-e",
        "FOLIATION_PROJECT=/foliation_project",
        "-e",
        f"OMP_NUM_THREADS={omp}",
        "-e",
        f"FOLIATION_QE_GCU_MODE={gcu_mode}",
        "-e",
        f"FOLIATION_QE_GCU_SIDECAR={gcu_sidecar}",
        "-e",
        f"FOLIATION_QE_GCU_WRAPPER_DIR={gcu_wrapper_dir}",
        "-e",
        f"FOLIATION_QE_GCU_INTERPOSER_SRC={gcu_interposer_src}",
        "-e",
        f"FOLIATION_QE_GCU_INTERPOSER_SO={gcu_interposer_so}",
        "-e",
        f"FOLIATION_GCU_FORCE_INTERPOSE={gcu_force_interpose}",
        "-e",
        f"FOLIATION_GCU_INTERPOSE_MAX_OPS={gcu_interpose_max_ops}",
        "-e",
        f"FOLIATION_GCU_FORCE_LARGE_ZGEMM={gcu_force_large_zgemm}",
        "-e",
        f"FOLIATION_GCU_LARGE_ZGEMM_K={gcu_large_zgemm_k}",
        "-e",
        f"FOLIATION_GCU_TILE_M={gcu_tile_m}",
        "-e",
        f"FOLIATION_GCU_TILE_N={gcu_tile_n}",
        "-e",
        f"FOLIATION_GCU_TILE_K={gcu_tile_k}",
        "-v",
        f"{pass_root}:/foliation_project:rw",
        "-w",
        "/foliation_project",
        image,
        "bash",
        "-lc",
        f"bash {script_container} {run_container}",
    ]
    proc = subprocess.run(cmd, cwd=pass_root, capture_output=True, text=True)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-2000:] if proc.stdout else "",
        "stderr_tail": proc.stderr[-2000:] if proc.stderr else "",
        "gcu_sidecar": {
            "mode": gcu_mode or None,
            "sidecar": gcu_sidecar or None,
            "wrapper_dir": gcu_wrapper_dir or None,
            "interposer_src": gcu_interposer_src or None,
            "interposer_so": gcu_interposer_so or None,
            "force_interpose": gcu_force_interpose or None,
            "force_large_zgemm": gcu_force_large_zgemm or None,
            "large_zgemm_k": gcu_large_zgemm_k or None,
        },
    }


def run_pipeline(
    *,
    decode_path: Path | None,
    run_id: str | None,
    pass_root: Path,
    poscar: Path | None,
    threshold_cm1: float,
    skip_phonopy: bool,
) -> dict[str, Any]:
    if decode_path is None and run_id:
        decode_path = DEFAULT_DECODE_DIR / f"inverse_decode_{run_id}.json"
    decode: dict[str, Any] | None = None
    if decode_path and decode_path.is_file():
        decode = _read_json(decode_path)
        run_id = decode.get("run_id", run_id or "unknown")

    build_script = pass_root / "scripts/oracle/build_decoded_structure.py"
    struct_dir = DEFAULT_DECODE_DIR / "decoded_structures" / str(run_id)
    if decode and build_script.is_file():
        subprocess.run(
            [sys.executable, str(build_script), "--decode", str(decode_path)],
            cwd=pass_root,
            check=False,
        )
        poscar = struct_dir / "POSCAR"
        manifest_path = struct_dir / "structure_manifest.json"
    elif poscar is None:
        raise FileNotFoundError("no POSCAR and no decode JSON to build structure")

    if poscar is None or not poscar.is_file():
        raise FileNotFoundError(f"missing POSCAR at {poscar}")

    manifest = _read_json(struct_dir / "structure_manifest.json") if (struct_dir / "structure_manifest.json").is_file() else {}
    formula = manifest.get("formula_pretty") or manifest.get("target_formula") or "Mo3Ta1S8"

    phonopy_run_id = f"oracle_phonopy_{run_id}"
    run_dir = pass_root / "artifacts" / "oracle" / "phonopy_runs" / phonopy_run_id
    run_container = f"/foliation_project/artifacts/oracle/phonopy_runs/{phonopy_run_id}"

    docker_log: dict[str, Any] = {"skipped": True}
    band_yaml = run_dir / "phonopy" / "band.yaml"
    if not skip_phonopy:
        setup_phonopy_run(pass_root=pass_root, run_dir=run_dir, poscar=poscar, formula=formula)
        docker_log = run_phonopy_docker(pass_root=pass_root, run_container=run_container)

    band_stats = parse_band_yaml_min_cm1(band_yaml)
    verdict = gate_verdict(band_stats, threshold_cm1=threshold_cm1, docker_log=docker_log)

    payload = {
        "schema": "foliation.phonopy_stability_verdict.v1",
        "run_id": run_id,
        "formula_proposed": manifest.get("target_formula", formula),
        "poscar": str(poscar.resolve()),
        "phonopy_run_dir": str(run_dir.resolve()),
        "decode_ref": str(decode_path.resolve()) if decode_path else None,
        "threshold_cm-1": threshold_cm1,
        "band_analysis": band_stats,
        "docker": docker_log,
        **verdict,
    }
    out_path = DEFAULT_DECODE_DIR / f"stability_verdict_{run_id}.json"
    _write_json(out_path, payload)
    payload["out"] = str(out_path)
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="Phonopy stability gate (Phase 3)")
    ap.add_argument("--decode", type=Path, help="inverse_decode JSON")
    ap.add_argument("--run-id", help="killer / decode run id")
    ap.add_argument("--poscar", type=Path, help="explicit POSCAR (skip decode build)")
    ap.add_argument("--pass-root", type=Path, default=DEFAULT_PASS_ROOT)
    ap.add_argument("--threshold-cm1", type=float, default=DEFAULT_IMAGINARY_CM1)
    ap.add_argument("--skip-phonopy", action="store_true", help="structure setup + parse only")
    args = ap.parse_args()

    try:
        payload = run_pipeline(
            decode_path=args.decode.expanduser().resolve() if args.decode else None,
            run_id=args.run_id,
            pass_root=args.pass_root.resolve(),
            poscar=args.poscar.expanduser().resolve() if args.poscar else None,
            threshold_cm1=args.threshold_cm1,
            skip_phonopy=args.skip_phonopy,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1

    passed = payload.get("passed", False)
    min_cm1 = payload.get("min_frequency_cm-1")
    min_str = f"{min_cm1:.1f}" if isinstance(min_cm1, (int, float)) else "na"
    print(
        f"phonopy={payload.get('verdict')} min_cm1={min_str} passed={passed}",
        file=sys.stderr,
    )
    summary = {
        "ok": passed,
        "verdict": payload.get("verdict"),
        "passed": passed,
        "min_frequency_cm-1": min_cm1,
        "out": payload.get("out"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
