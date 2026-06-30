#!/usr/bin/env python3
"""Crystal Forge Daemon — SC + HEA 双轨逆向造物 · Phonopy 门控 · README 封神榜.

严格确定性：无 LLM、无插值、无概率化学式。
接管 god_mode / sc_fpu_batch 产出，循环 decode → POSCAR → phonopy → hall update.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from tools.foliation_artifact_paths import icloud_root  # noqa: E402

PASS_V1 = Path(os.environ.get("PASS_V1_ROOT", Path.home() / "Desktop" / "foliation-pass-v1"))
README = ENGINE / "README.md"
_ICLOUD = icloud_root()
_CF_BASE = (_ICLOUD / "out/chip/crystal_forge") if _ICLOUD else (ENGINE / "out/chip/crystal_forge")
_CHIP_OUT = (_ICLOUD / "out/chip") if _ICLOUD else (ENGINE / "out/chip")
_ART_BASE = (_ICLOUD / "artifacts") if _ICLOUD else (ENGINE / "artifacts")
STATE_PATH = _CF_BASE / "state.json"
HALL_PATH = _ART_BASE / "crystal_forge" / "hall_of_fame.json"
LOG_PATH = _CF_BASE / "forge.log"
_STAGE = ENGINE / "out/chip/_forge_stage"

DECODE = PASS_V1 / "scripts/oracle/decode_theta_to_chemistry.py"
BUILD_POSCAR = PASS_V1 / "scripts/oracle/build_decoded_structure.py"
PHONOPY_GATE = PASS_V1 / "scripts/oracle/phonopy_stability_gate.py"
GCU_PRESCREEN = ENGINE / "tools/chip_gcu_phonopy_prescreen.py"
GCU_PARASITIC_VALIDATOR = ENGINE / "tools/gcu_parasitic_validator.py"
GOD_MODE = PASS_V1 / "scripts/god_mode_mass_sweeper.py"
HEA_ANNEAL = ENGINE / "tools/chip_hea_thermo_anneal.py"
HEA_CLOSURE = ENGINE / "tools/chip_hea_cstar_closure.py"
HEA_BOOTSTRAP = ENGINE / "tools/chip_hea_emergence_bootstrap.py"
SC_BATCH = ENGINE / "tools/chip_sc_fpu_batch.py"
BLUEPRINT_DIR = PASS_V1 / "artifacts/oracle/theta_blueprint"
DECODE_DIR = PASS_V1 / "artifacts/oracle"
HEA_RUN = PASS_V1 / "artifacts/abinitio/runs/hea_20260615_052716"
HEA_RUN_ICLOUD = (_ICLOUD / "pass-v1-artifacts/abinitio/runs/hea_20260615_052716") if _ICLOUD else None
HEA_VAL_CACHE = ENGINE / "out/chip/hea_c_star_sro_validation.json"
PASS_PYTHON = Path(
    os.environ.get(
        "FOLIATION_PASS_PYTHON",
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
    )
)

CRYSTAL_MARKER_BEGIN = "<!-- SYNTHESIZED_CRYSTALS:BEGIN -->"
CRYSTAL_MARKER_END = "<!-- SYNTHESIZED_CRYSTALS:END -->"

try:
    from tools.chip_physics_sim_gate import exit_if_not_simulated  # noqa: E402
except ImportError:
    exit_if_not_simulated = None  # type: ignore


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 7200) -> dict[str, Any]:
    _log(">> " + " ".join(cmd))
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd or ENGINE,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PASS_V1_ROOT": str(PASS_V1), "FOLIATION_ENGINE_ROOT": str(ENGINE)},
        )
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "exit": -1, "error": f"timeout {timeout}s", "stdout": str(exc)[:500]}
    return {
        "ok": p.returncode == 0,
        "exit": p.returncode,
        "stdout": (p.stdout or "")[-3000:],
        "stderr": (p.stderr or "")[-800:],
    }


def _stage_file(src: Path, name: str) -> Path | None:
    if not src.is_file():
        return None
    dst = _STAGE / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        dst.write_bytes(src.read_bytes())
        return dst
    except OSError:
        return None


def _safe_read_json(path: Path) -> dict[str, Any]:
    for candidate in (path, HEA_VAL_CACHE):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if candidate == path and path != HEA_VAL_CACHE:
                HEA_VAL_CACHE.parent.mkdir(parents=True, exist_ok=True)
                HEA_VAL_CACHE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            return data if isinstance(data, dict) else {}
        except (OSError, TimeoutError, json.JSONDecodeError):
            continue
    return {}


def _resolve_hea_run() -> Path:
    if HEA_RUN.is_dir():
        return HEA_RUN
    if HEA_RUN_ICLOUD and HEA_RUN_ICLOUD.is_dir():
        return HEA_RUN_ICLOUD
    return HEA_RUN


def load_state() -> dict[str, Any]:
    if STATE_PATH.is_file():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"sc_done": [], "hea_done": [], "cycles": 0}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_hall() -> list[dict[str, Any]]:
    if HALL_PATH.is_file():
        return json.loads(HALL_PATH.read_text(encoding="utf-8"))
    return []


def save_hall(entries: list[dict[str, Any]]) -> None:
    HALL_PATH.parent.mkdir(parents=True, exist_ok=True)
    HALL_PATH.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def discover_sc_run_ids(*, limit: int = 10) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    rid_re = re.compile(r"^killer_\d{8}_\d{6}$")

    def _has_fpu_feed(run_id: str) -> bool:
        hubbard = PASS_V1 / "artifacts/abinitio/runs" / run_id / "fpu_feed/hubbard_params.json"
        try:
            return hubbard.is_file()
        except OSError:
            return False

    batch = _CHIP_OUT / "sc_fpu_batch_report.json"
    if not batch.is_file():
        batch = ENGINE / "out/chip/sc_fpu_batch_report.json"
    if batch.is_file():
        try:
            rep = json.loads(batch.read_text(encoding="utf-8"))
            for row in rep.get("runs") or rep.get("results") or []:
                rid = row.get("run_id") or row.get("name")
                if not rid and isinstance(row.get("run"), str):
                    rid = Path(row["run"]).name
                if (
                    isinstance(rid, str)
                    and rid_re.match(rid)
                    and rid not in seen
                    and _has_fpu_feed(rid)
                ):
                    seen.add(rid)
                    ids.append(rid)
        except (json.JSONDecodeError, OSError, TimeoutError):
            pass
    for bp_dir in (_CHIP_OUT, BLUEPRINT_DIR):
        if not bp_dir.is_dir():
            continue
        try:
            bps = sorted(bp_dir.glob("blueprint_killer_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue
        for bp in bps:
            try:
                rid = json.loads(bp.read_text(encoding="utf-8")).get("run_id") or bp.stem.replace("blueprint_", "")
            except (json.JSONDecodeError, OSError, TimeoutError):
                rid = bp.stem.replace("blueprint_", "")
            if rid and rid_re.match(rid) and rid not in seen:
                if _has_fpu_feed(rid) or bp_dir == _CHIP_OUT:
                    seen.add(rid)
                    ids.append(rid)
    if _ICLOUD:
        return ids[:limit]
    runs_root = PASS_V1 / "artifacts/abinitio/runs"
    if runs_root.is_dir():
        for run_dir in sorted(runs_root.glob("killer_*"), key=lambda p: p.name, reverse=True):
            rid = run_dir.name
            if rid_re.match(rid) and rid not in seen and _has_fpu_feed(rid):
                seen.add(rid)
                ids.append(rid)
    return ids[:limit]


def _formula_from_run(run_id: str) -> str | None:
    cif = PASS_V1 / "artifacts/abinitio/runs" / run_id / "candidate.cif"
    if not cif.is_file():
        return None
    for line in cif.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*data_(\S+)", line)
        if m:
            return m.group(1)
    try:
        from pymatgen.core import Structure

        return Structure.from_file(str(cif)).composition.reduced_formula
    except Exception:
        return None


def _should_mark_sc_done(row: dict[str, Any]) -> bool:
    """Only terminal outcomes enter sc_done — infra failures remain retryable."""
    if row.get("ok"):
        return True
    stage = row.get("stage") or ""
    if stage in ("phonopy", "gcu_fastpath_only"):
        return True
    if stage == "gcu_prescreen":
        pre = row.get("gcu_prescreen_body") or {}
        if pre.get("verdict") == "FAIL_GCU_SPECTRAL_STRICT":
            return True
    return False


def _resolve_blueprint_path(run_id: str) -> Path | None:
    for path in (
        _CHIP_OUT / f"blueprint_{run_id}.json",
        ENGINE / f"out/chip/blueprint_{run_id}.json",
    ):
        if path.is_file():
            return path
    bp_pass = BLUEPRINT_DIR / f"blueprint_{run_id}.json"
    if not bp_pass.is_file():
        return None
    try:
        json.loads(bp_pass.read_text(encoding="utf-8"))
        return bp_pass
    except (OSError, TimeoutError, json.JSONDecodeError):
        return None


def sc_track_one(
    run_id: str,
    *,
    j_threshold: float,
    skip_phonopy: bool,
    skip_gcu_prescreen: bool = False,
    strict_spectral: bool = False,
) -> dict[str, Any]:
    rep: dict[str, Any] = {"run_id": run_id, "track": "SC"}
    if not DECODE.is_file():
        return {**rep, "ok": False, "error": "missing decode_theta_to_chemistry.py"}

    bp_path = BLUEPRINT_DIR / f"blueprint_{run_id}.json"
    local_bp = _resolve_blueprint_path(run_id)
    decode_cmd = [sys.executable, str(DECODE), "--j-threshold", str(j_threshold)]
    
    if local_bp is None:
        batch = _CHIP_OUT / "sc_fpu_batch_report.json"
        if not batch.is_file():
            batch = ENGINE / "out/chip/sc_fpu_batch_report.json"
        if batch.is_file():
            try:
                rep_data = json.loads(batch.read_text(encoding="utf-8"))
                for row in rep_data.get("runs") or rep_data.get("results") or []:
                    fpu = row.get("fpu_anneal", {})
                    rid = row.get("run_id") or row.get("name")
                    if not rid and isinstance(row.get("run"), str):
                        rid = Path(row["run"]).name
                    if rid == run_id and fpu and "theta_best" in fpu:
                        run_dir = PASS_V1 / "artifacts/abinitio/runs" / run_id
                        bp = {
                            "schema": "foliation.theta_blueprint.v1",
                            "run_id": run_id,
                            "run_dir": str(run_dir),
                            "formula": _formula_from_run(run_id),
                            "theta_labels": fpu.get("anneal_report", {}).get("theta_labels", []),
                            "theta_star": fpu["theta_best"],
                            "sigma_iwn": fpu.get("sigma_at_best"),
                            "J_BKT": fpu.get("j_best"),
                        }
                        local_bp = _CHIP_OUT / f"blueprint_{run_id}.json"
                        local_bp.parent.mkdir(parents=True, exist_ok=True)
                        local_bp.write_text(json.dumps(bp), encoding="utf-8")
                        break
            except (OSError, TimeoutError, json.JSONDecodeError):
                pass

    if local_bp is not None:
        staged_bp = _stage_file(local_bp, f"blueprint_{run_id}.json") or local_bp
        decode_cmd.extend(["--blueprint", str(staged_bp)])
        lookup = PASS_V1 / "artifacts/oracle/physics_lookup_v1.json"
        staged_lookup = _stage_file(lookup, "physics_lookup_v1.json")
        if staged_lookup:
            decode_cmd.extend(["--lookup", str(staged_lookup)])
    else:
        return {**rep, "ok": False, "stage": "decode", "error": f"missing blueprint for {run_id}"}

    dec = _run(
        [str(PASS_PYTHON), *decode_cmd[1:]],
        cwd=PASS_V1,
        timeout=600,
    )
    rep["decode"] = dec
    decode_json = DECODE_DIR / f"inverse_decode_{run_id}.json"
    if not dec["ok"] or not decode_json.is_file():
        return {**rep, "ok": False, "stage": "decode"}

    payload = json.loads(decode_json.read_text(encoding="utf-8"))
    rep["decode_verdict"] = payload.get("verdict")
    if payload.get("verdict") != "PASS_SYNTHESIS_RECIPE":
        return {**rep, "ok": False, "stage": "decode_verdict"}

    # Phase 2c 架构补丁：超胞离散化与 FPU 强制回验
    accepted = payload.get("accepted_recipe", {})
    fpu_data = accepted.get("fpu", {})
    j_bkt = fpu_data.get("J_BKT")
    if j_bkt is not None and j_bkt < j_threshold:
        _log(f"PHASE 2c BLOCK [{run_id}]: J_BKT={j_bkt} < {j_threshold}. FAIL_FRAGILE_PHASE (failed lattice quantization re-verification)")
        return {**rep, "ok": False, "stage": "phase2c_quantization", "verdict": "FAIL_FRAGILE_PHASE"}

    if not BUILD_POSCAR.is_file():
        return {**rep, "ok": False, "error": "missing build_decoded_structure.py"}
    pos = _run([str(PASS_PYTHON), str(BUILD_POSCAR), "--run-id", run_id], cwd=PASS_V1, timeout=300)
    rep["poscar"] = pos
    poscar = PASS_V1 / "artifacts/oracle/decoded_structures" / run_id / "POSCAR"
    rep["poscar_path"] = str(poscar)
    if not pos["ok"] or not poscar.is_file():
        return {**rep, "ok": False, "stage": "poscar"}

    if not skip_gcu_prescreen and GCU_PRESCREEN.is_file():
        gcu_cmd = [sys.executable, str(GCU_PRESCREEN), "--run-id", run_id]
        if strict_spectral:
            gcu_cmd.append("--strict-spectral")
        gcu = _run(gcu_cmd, cwd=ENGINE, timeout=600)
        rep["gcu_prescreen"] = gcu
        prescreen_path = PASS_V1 / "artifacts" / "oracle" / f"gcu_prescreen_{run_id}.json"
        if prescreen_path.is_file():
            rep["gcu_prescreen_body"] = json.loads(prescreen_path.read_text(encoding="utf-8"))
        pre = rep.get("gcu_prescreen_body") or {}
        if not pre.get("passed"):
            rep["ok"] = False
            rep["stage"] = "gcu_prescreen"
            _log(
                f"GCU PRESCREEN FAIL [{run_id}] verdict={pre.get('verdict', '—')} "
                f"λ={((pre.get('spectral_proxy') or {}).get('lambda_max'))} "
                f"phonopy=BLOCKED"
            )
            return rep
        fast = pre.get("gcu_phonopy_fastpath") or {}
        if fast:
            _log(
                f"GCU PHONOPY FASTPATH [{run_id}] verdict={fast.get('verdict', '—')} "
                f"min_proxy_cm1={fast.get('min_proxy_cm1')} "
                f"fc2_norm={fast.get('fc2_norm')} "
                f"spotcheck={fast.get('spotcheck_displacements', 0)}"
            )
        _log(
            f"GCU PRESCREEN PASS [{run_id}] λ={((pre.get('spectral_proxy') or {}).get('lambda_max'))} "
            f"Δα={((pre.get('spectral_proxy') or {}).get('delta_alpha_log2'))} → Phonopy final audit"
        )

    if skip_phonopy:
        rep["phonopy"] = {"skipped": True, "reason": "gcu_fastpath_only"}
        return {**rep, "ok": True, "stage": "gcu_fastpath_only"}

    validator = GCU_PARASITIC_VALIDATOR if GCU_PARASITIC_VALIDATOR.is_file() else PHONOPY_GATE
    if not validator.is_file():
        return {**rep, "ok": False, "error": "missing phonopy_stability_gate.py"}
    if validator == GCU_PARASITIC_VALIDATOR:
        _log(f"GCU PARASITE VALIDATOR [{run_id}] external=phonopy role=validation_only")
        ph = _run(
            [sys.executable, str(validator), "--run-id", run_id, "--validator", "phonopy"],
            cwd=ENGINE,
            timeout=7200,
        )
    else:
        ph = _run([sys.executable, str(validator), "--run-id", run_id], cwd=PASS_V1, timeout=7200)
    rep["phonopy"] = ph
    verdict_path = PASS_V1 / "artifacts/oracle" / f"stability_verdict_{run_id}.json"
    if verdict_path.is_file():
        rep["stability"] = json.loads(verdict_path.read_text(encoding="utf-8"))
    passed = (rep.get("stability") or {}).get("verdict") == "PASS_STABLE"
    rep["ok"] = passed
    rep["stage"] = "phonopy"
    if not passed:
        stab = rep.get("stability") or {}
        docker = stab.get("docker") or {}
        band_err = (stab.get("band_analysis") or {}).get("error")
        root_cause = (
            stab.get("root_cause")
            or stab.get("error")
            or stab.get("reason")
            or band_err
            or docker.get("stderr_tail")
            or docker.get("stdout_tail")
            or ph.get("stderr")
            or ph.get("stdout")
            or "unknown"
        )
        _log(f"PHONOPY FAIL [{run_id}] verdict={stab.get('verdict', '—')}")
        _log(f"PHONOPY ROOT [{run_id}]: {str(root_cause)[:2000]}")
        if docker.get("stdout_tail") and str(root_cause) != str(docker.get("stdout_tail")):
            _log(f"PHONOPY DOCKER [{run_id}]: {str(docker.get('stdout_tail'))[-2500:]}")
        if ph.get("stderr"):
            _log(f"PHONOPY STDERR [{run_id}]: {ph['stderr'][:1500]}")
    if passed:
        rep["hall_entry"] = _hall_entry_from_sc(run_id, payload, rep)
    return rep


def _hall_entry_from_sc(run_id: str, decode: dict, rep: dict) -> dict[str, Any]:
    accepted = decode.get("accepted_recipe") or {}
    fpu = accepted.get("fpu") or {}
    formula_obj = accepted.get("formula") or {}
    j = fpu.get("J_BKT") or decode.get("J_BKT_blueprint")
    formula = formula_obj.get("formula_pretty") or decode.get("formula_input") or run_id
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "formula": formula,
        "track": "SC",
        "metric": f"J_BKT={j}" if j is not None else "J_BKT=—",
        "run_id": run_id,
        "evidence": f"artifacts/oracle/stability_verdict_{run_id}.json",
        "poscar": f"artifacts/oracle/decoded_structures/{run_id}/POSCAR",
        "decode": f"artifacts/oracle/inverse_decode_{run_id}.json",
    }


def hea_track_one(*, anneal_steps: int, skip_phonopy: bool) -> dict[str, Any]:
    hea_run = _resolve_hea_run()
    rep: dict[str, Any] = {"track": "HEA", "hea_run": str(hea_run)}
    if not hea_run.is_dir():
        return {**rep, "ok": False, "error": f"missing HEA run {hea_run}"}

    thermo_out = ENGINE / "out/chip/hea_thermo_full.json"
    ann = _run(
        [sys.executable, str(HEA_ANNEAL), "--steps", str(anneal_steps), "--out", str(thermo_out)],
        timeout=3600,
    )
    rep["anneal"] = ann
    if not ann["ok"]:
        return {**rep, "ok": False, "stage": "anneal"}

    c_star = thermo_out if thermo_out.is_file() else ENGINE / "out/chip/hea_thermo_anneal_report.json"
    clo = _run(
        [
            sys.executable,
            str(HEA_CLOSURE),
            "--run",
            str(hea_run),
            "--c-star",
            str(c_star),
        ],
        timeout=600,
    )
    rep["closure"] = clo
    val = hea_run / "hea_emergence/c_star_sro_validation.json"
    summary = _safe_read_json(val).get("summary", {})
    rep["c_star_summary"] = summary
    if not clo["ok"] or not summary.get("c_star_ok"):
        if clo["ok"] and not summary:
            rep["closure_read_warn"] = "validation_json_unreadable"
            return {**rep, "ok": False, "stage": "closure", "retryable": True}
        return {**rep, "ok": False, "stage": "closure"}

    if HEA_BOOTSTRAP.is_file():
        boot = _run([sys.executable, str(HEA_BOOTSTRAP), "--run", str(hea_run)], timeout=600)
        rep["hea_bootstrap"] = boot
        post = _run(
            [sys.executable, str(PASS_V1 / "scripts/abinitio_pipeline/postprocess_hea_emergence.py"), "--run", str(hea_run)],
            cwd=PASS_V1,
            timeout=300,
        )
        rep["hea_emergence_refresh"] = post

    poscar = hea_run / "sqs/POSCAR"
    rep["poscar_path"] = str(poscar)
    if skip_phonopy or not poscar.is_file():
        rep["phonopy"] = {"skipped": True}
        rep["ok"] = summary.get("c_star_ok", False)
        rep["stage"] = "hea_closure"
        return rep

    # HEA phonopy via pass-v1 firewall path if available — lightweight gate on SQS POSCAR TBD
    rep["ok"] = True
    rep["stage"] = "hea_ready"
    return rep


def update_readme_table(hall: list[dict[str, Any]]) -> bool:
    if not README.is_file():
        return False
    text = README.read_text(encoding="utf-8")
    if CRYSTAL_MARKER_BEGIN not in text or CRYSTAL_MARKER_END not in text:
        return False

    rows = [
        "| Status | Formula | Track | \\(J_\\mathrm{BKT}\\) / Thermo | Evidence |",
        "|:------:|:--------|:-----:|:---------------------------:|:---------|",
    ]
    if not hall:
        rows.append("| 🎯 **Hunting** | — | SC / HEA | — | Furnace warming — no stable synthesis logged yet |")
    else:
        for e in sorted(hall, key=lambda x: x.get("ts", ""), reverse=True):
            formula = e.get("formula", "—")
            track = e.get("track", "—")
            metric = e.get("metric", "—")
            ev = e.get("evidence", "—")
            rows.append(f"| ✅ **Synthesized** | {formula} | {track} | {metric} | `{ev}` |")

    block = CRYSTAL_MARKER_BEGIN + "\n\n" + "\n".join(rows) + "\n\n" + CRYSTAL_MARKER_END
    pre, _, post = text.partition(CRYSTAL_MARKER_BEGIN)
    if not post:
        return False
    _, _, tail = post.partition(CRYSTAL_MARKER_END)
    new_text = pre + block + tail
    README.write_text(new_text, encoding="utf-8")
    return True


def append_hall(entry: dict[str, Any]) -> None:
    hall = load_hall()
    key = (entry.get("track"), entry.get("run_id"), entry.get("formula"))
    if any((h.get("track"), h.get("run_id"), h.get("formula")) == key for h in hall):
        return
    hall.append(entry)
    save_hall(hall)
    update_readme_table(hall)
    _log(f"HALL: {entry.get('formula')} ({entry.get('track')}) → README updated")


def resume_god_mode(*, steps: int = 100, dry: bool = False, force: bool = False) -> dict[str, Any]:
    if not GOD_MODE.is_file():
        return {"ok": False, "error": "missing god_mode_mass_sweeper.py"}
    cmd = [sys.executable, str(GOD_MODE), "--steps", str(steps)]
    if dry:
        cmd.append("--dry-run")
    if force:
        cmd.append("--force")
    return _run(cmd, cwd=PASS_V1, timeout=7200)


def run_cycle(
    *,
    sc_limit: int,
    j_threshold: float,
    hea_steps: int,
    skip_phonopy: bool,
    skip_gcu_prescreen: bool,
    strict_spectral: bool,
    god_mode: bool,
    god_steps: int,
    god_force: bool,
    sc_only: bool,
) -> dict[str, Any]:
    if exit_if_not_simulated:
        req = ("chip_a", "chip_b") if sc_only else ("chip_a", "chip_b", "hea_engine")
        exit_if_not_simulated(tool="crystal_forge_daemon", require=req)

    state = load_state()
    state["cycles"] = int(state.get("cycles", 0)) + 1
    report: dict[str, Any] = {"cycle": state["cycles"], "sc": [], "hea": None, "god_mode": None}

    if god_mode:
        _log("WARN: --god-mode blocks cycle; run god_mode in separate process")
        report["god_mode"] = resume_god_mode(steps=god_steps, force=god_force)

    sc_done = set(state.get("sc_done") or [])
    pending = discover_sc_run_ids(limit=max(sc_limit * 8, 32))
    run_ids = [r for r in pending if r not in sc_done and _resolve_blueprint_path(r)][:sc_limit]

    if run_ids:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(2, len(run_ids))) as executor:
            futures = {}
            for run_id in run_ids:
                _log(f"SC forge (parallel): {run_id}")
                futures[executor.submit(
                    sc_track_one,
                    run_id,
                    j_threshold=j_threshold,
                    skip_phonopy=skip_phonopy,
                    skip_gcu_prescreen=skip_gcu_prescreen,
                    strict_spectral=strict_spectral,
                )] = run_id

            for future in concurrent.futures.as_completed(futures):
                run_id = futures[future]
                try:
                    row = future.result()
                    report["sc"].append(row)
                    if row.get("hall_entry"):
                        append_hall(row["hall_entry"])
                    if _should_mark_sc_done(row):
                        sc_done.add(run_id)
                    else:
                        _log(
                            f"SC forge RETRYABLE [{run_id}] stage={row.get('stage')} "
                            f"ok={row.get('ok')} — not marking sc_done"
                        )
                except Exception as exc:
                    _log(f"SC forge ERROR [{run_id}]: {exc} — not marking sc_done")
        state["sc_done"] = sorted(sc_done)

    if not sc_only and (not state.get("hea_done") or state["cycles"] % 3 == 0):
        _log("HEA forge cycle")
        hea = hea_track_one(anneal_steps=hea_steps, skip_phonopy=skip_phonopy)
        report["hea"] = hea
        if hea.get("hall_entry") and hea.get("ok") and not skip_phonopy:
            append_hall(hea["hall_entry"])
            state["hea_done"] = True
        elif hea.get("ok"):
            state["hea_last_closure"] = datetime.now(timezone.utc).isoformat()

    save_state(state)
    report_path = _CF_BASE / f"cycle_{state['cycles']}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _log(f"Cycle {state['cycles']} complete → {report_path}")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Crystal Forge — SC + HEA dual-track daemon")
    ap.add_argument("--loop", action="store_true", help="infinite furnace loop")
    ap.add_argument("--interval", type=int, default=3600, help="seconds between cycles")
    ap.add_argument("--sc-limit", type=int, default=10)
    ap.add_argument("--j-threshold", type=float, default=0.05)
    ap.add_argument("--hea-steps", type=int, default=50000)
    ap.add_argument("--skip-phonopy", action="store_true")
    ap.add_argument("--skip-gcu-prescreen", action="store_true", help="bypass Wannier→ROM GCU prescreen")
    ap.add_argument(
        "--strict-spectral",
        action="store_true",
        help="GCU prescreen blocks soft_mode_risk before Phonopy Docker",
    )
    ap.add_argument("--god-mode", action="store_true", help="resume god_mode_mass_sweeper each cycle")
    ap.add_argument("--god-steps", type=int, default=100)
    ap.add_argument("--god-force", action="store_true", help="pass --force to god_mode_mass_sweeper")
    ap.add_argument("--sc-only", action="store_true", help="SC decode+phonopy only; no HEA track")
    ap.add_argument("--once", action="store_true", help="single cycle then exit")
    args = ap.parse_args()

    if args.once:
        args.loop = False

    def one() -> int:
        try:
            rep = run_cycle(
                sc_limit=args.sc_limit,
                j_threshold=args.j_threshold,
                hea_steps=args.hea_steps,
                skip_phonopy=args.skip_phonopy,
                skip_gcu_prescreen=args.skip_gcu_prescreen,
                strict_spectral=args.strict_spectral,
                god_mode=args.god_mode,
                god_steps=args.god_steps,
                god_force=args.god_force,
                sc_only=args.sc_only,
            )
        except SystemExit as exc:
            _log(f"ABORT: physics sim gate ({exc})")
            return int(exc.code) if isinstance(exc.code, int) else 3
        ok = any(r.get("ok") for r in rep.get("sc") or []) or (rep.get("hea") or {}).get("ok")
        return 0 if ok or rep.get("sc") else 1

    if not args.loop:
        return one()

    _log("Crystal Forge daemon — dual track infinite loop")
    while True:
        rc = one()
        _log(f"cycle exit={rc}; sleep {args.interval}s")
        time.sleep(max(60, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
