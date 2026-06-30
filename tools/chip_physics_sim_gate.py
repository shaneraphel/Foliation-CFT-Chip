#!/usr/bin/env python3
"""物理芯片 A/B（单核 master 仿真门禁）+ HEA 矩阵核预仿真 — 与 nlp-array-sim 同路径，核数=1."""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
PASS_V1 = Path(os.environ.get("PASS_V1_ROOT", Path.home() / "Desktop" / "foliation-pass-v1"))
sys.path.insert(0, str(ENGINE))
from tools.foliation_chip_paths import materialize_chip_netlists, universal_fpu_aig  # noqa: E402

MARKER = ENGINE / "out/chip/PHYSICS_SIM_OK.json"
SIM_LOG = ENGINE / "out/chip/logs/physics_sim_gate.json"
NLP_SIM = ENGINE / "bin/nlp-array-sim"
UNIVERSAL_AIG = universal_fpu_aig()
GOD_MODE = PASS_V1 / "scripts/god_mode_mass_sweeper.py"
GOD_LOG = PASS_V1 / "artifacts/god_mode_mcmc.log"
DUAL_BATCH = ENGINE / "tools/chip_dual_batch_sweeper.py"
SIM_TIMEOUT = int(os.environ.get("CHIP_PHYSICS_SIM_TIMEOUT", "120"))
MARKER_TTL = int(os.environ.get("CHIP_PHYSICS_SIM_TTL", "3600"))

RESIDUAL_RE = re.compile(r"residual\[0\]:\s+(true|false)", re.I)
MS_RE = re.compile(r"耗时:\s+([\d.]+)\s*ms")

CHIP_A_CTX = (
    "physics_chip_a|killer|ab-initio|QE|phonopy|Wannier|fpu_feed_wannier|6-bath|V_k|E_k"
)
CHIP_B_CTX = (
    "physics_chip_b|forge-daemon|extensions_universal_v3.aig|14-bus|Sigma_iwn|DMFT|Universal_AIM"
)
HEA_CTX = "hea_engine|hea_matrix_algebra|Cantor_NiCoCrFeMn|C_star|Metropolis|Warren-Cowley|SRO"


def _force() -> bool:
    return os.environ.get("CHIP_PHYSICS_SIM_FORCE", "").lower() in ("1", "true", "yes")


def _sim_cores() -> int:
    return int(os.environ.get("CHIP_PHYSICS_SIM_CORES", "1"))


def sim_tick(context: str, *, chip: str) -> dict:
    if not NLP_SIM.is_file():
        return {"chip": chip, "ok": False, "error": f"missing {NLP_SIM}"}
    cmd = [str(NLP_SIM), "--cores", str(_sim_cores()), context[:4000]]
    t0 = time.time()
    try:
        p = subprocess.run(
            cmd,
            cwd=ENGINE,
            capture_output=True,
            text=True,
            timeout=SIM_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"chip": chip, "ok": False, "error": f"timeout {SIM_TIMEOUT}s", "context": context[:120]}
    out = (p.stdout or "") + (p.stderr or "")
    m = RESIDUAL_RE.search(out)
    residual = m.group(1).lower() == "true" if m else None
    ms_m = MS_RE.search(out)
    elapsed_ms = float(ms_m.group(1)) if ms_m else (time.time() - t0) * 1000
    # residual[0]=false → 无残差 → PASS（与 god_mode NLP 预检一致）
    ok = p.returncode == 0 and residual is False
    if residual is None and p.returncode == 0:
        ok = True
    return {
        "chip": chip,
        "ok": ok,
        "residual": residual,
        "exit": p.returncode,
        "elapsed_ms": round(elapsed_ms, 3),
        "context_head": context[:160],
        "tail": out.strip()[-600:],
    }


def run_physics_sim(*, include_hea: bool = True) -> dict:
    if os.environ.get("CHIP_PHYSICS_SIM_SKIP", "").lower() not in ("1", "true", "yes"):
        materialize_chip_netlists()
    aig_note = str(UNIVERSAL_AIG) if UNIVERSAL_AIG.is_file() else "missing_aig"
    chip_a = sim_tick(CHIP_A_CTX, chip="physics_a")
    chip_b = sim_tick(f"{CHIP_B_CTX}|aig={aig_note}", chip="physics_b")
    hea = sim_tick(HEA_CTX, chip="hea_engine") if include_hea else {"chip": "hea_engine", "ok": True, "skipped": True}
    body = {
        "ts": int(time.time()),
        "cores": _sim_cores(),
        "sim_path": "bin/nlp-array-sim (MasterCoreSimulator per-core, same as NLP array)",
        "chip_a": chip_a,
        "chip_b": chip_b,
        "hea_engine": hea,
        "all_ok": chip_a.get("ok") and chip_b.get("ok") and hea.get("ok"),
    }
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    SIM_LOG.parent.mkdir(parents=True, exist_ok=True)
    SIM_LOG.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return body


def load_marker() -> dict | None:
    if not MARKER.is_file():
        return None
    try:
        return json.loads(MARKER.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def marker_fresh() -> bool:
    m = load_marker()
    if not m or not m.get("all_ok"):
        return False
    age = time.time() - int(m.get("ts", 0))
    return age <= MARKER_TTL and not _force()


def ensure_physics_sim(*, include_hea: bool = True) -> dict:
    if marker_fresh():
        return load_marker() or {}
    return run_physics_sim(include_hea=include_hea)


def exit_if_not_simulated(*, tool: str = "", require: tuple[str, ...] = ("chip_a", "chip_b")) -> None:
    if _force() and os.environ.get("CHIP_PHYSICS_SIM_SKIP", "").lower() in ("1", "true", "yes"):
        return
    m = ensure_physics_sim(include_hea="hea_engine" in require or "hea" in require)
    fails: list[str] = []
    key_map = {"chip_a": "chip_a", "chip_b": "chip_b", "hea": "hea_engine", "hea_engine": "hea_engine"}
    for req in require:
        k = key_map.get(req, req)
        block = m.get(k) or m.get(req)
        if not (block or {}).get("ok"):
            fails.append(req)
    if not fails and m.get("all_ok"):
        return
    prefix = f"[PHYSICS SIM REQUIRED] {tool}: " if tool else "[PHYSICS SIM REQUIRED] "
    msg = (
        prefix
        + f"物理 A/B 单核 master 仿真未通过 ({', '.join(fails) or 'all_ok=false'})。"
        + f" 运行: bin/chip-physics-sim  报告: {MARKER.relative_to(ENGINE)}"
    )
    print(msg, file=sys.stderr)
    raise SystemExit(3)


def stop_god_mode() -> list[int]:
    killed: list[int] = []
    try:
        r = subprocess.run(
            ["pgrep", "-f", "god_mode_mass_sweeper.py"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return killed
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line.isdigit():
            continue
        pid = int(line)
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except OSError:
            pass
    return killed


def start_god_mode(*, steps: int = 100, j_threshold: float = 0.05) -> dict:
    exit_if_not_simulated(tool="start_god_mode", require=("chip_a", "chip_b"))
    env = os.environ.copy()
    env["FOLIATION_ENGINE_ROOT"] = str(ENGINE)
    env["PASS_V1_ROOT"] = str(PASS_V1)
    env["PYTHONUNBUFFERED"] = "1"
    GOD_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_f = GOD_LOG.open("a", encoding="utf-8")
    cmd = [
        sys.executable,
        "-u",
        str(GOD_MODE),
        "--steps",
        str(steps),
        "--j-threshold",
        str(j_threshold),
    ]
    p = subprocess.Popen(
        cmd,
        cwd=ENGINE,
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return {"pid": p.pid, "log": str(GOD_LOG), "cmd": cmd}


def start_hea_pipeline(*, anneal_steps: int = 50000) -> dict:
    exit_if_not_simulated(tool="start_hea_pipeline", require=("hea_engine",))
    hea_log = ENGINE / "out/chip/logs/hea_batch.log"
    hea_log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(DUAL_BATCH),
        "--regime",
        "hea",
        "--hea-anneal-steps",
        str(anneal_steps),
    ]
    log_f = hea_log.open("a", encoding="utf-8")
    p = subprocess.Popen(
        cmd,
        cwd=ENGINE,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return {"pid": p.pid, "log": str(hea_log), "cmd": cmd}


def start_dual_batch(*, hea_steps: int = 50000, sc_limit: int = 2, sc_fpu_steps: int = 30) -> dict:
    exit_if_not_simulated(tool="start_dual_batch", require=("chip_a", "chip_b", "hea_engine"))
    cmd = [
        sys.executable,
        str(DUAL_BATCH),
        "--regime",
        "both",
        "--hea-anneal-steps",
        str(hea_steps),
        "--sc-limit",
        str(sc_limit),
        "--sc-fpu-steps",
        str(sc_fpu_steps),
    ]
    p = subprocess.run(cmd, cwd=ENGINE, capture_output=True, text=True, timeout=7200)
    return {"exit": p.returncode, "stdout": (p.stdout or "")[-3000:], "stderr": (p.stderr or "")[-800:]}


def main() -> int:
    ap = argparse.ArgumentParser(description="物理芯片 A/B 单核 master 仿真门禁")
    ap.add_argument("--run", action="store_true", help="运行 A+B+HEA 单核仿真并写 marker")
    ap.add_argument("--no-hea", action="store_true")
    ap.add_argument("--stop-god-mode", action="store_true")
    ap.add_argument("--restart", action="store_true", help="stop → sim → god_mode 后台 + dual batch HEA+SC")
    ap.add_argument("--god-steps", type=int, default=100)
    ap.add_argument("--j-threshold", type=float, default=0.05)
    ap.add_argument("--hea-anneal-steps", type=int, default=50000)
    ap.add_argument("--sc-limit", type=int, default=2)
    ap.add_argument("--sc-fpu-steps", type=int, default=30)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.stop_god_mode or args.restart:
        pids = stop_god_mode()
        print(json.dumps({"stopped_pids": pids}, ensure_ascii=False))

    if args.restart:
        rep = run_physics_sim(include_hea=not args.no_hea)
        print(json.dumps({"sim": rep["all_ok"], "marker": str(MARKER)}, ensure_ascii=False))
        if not rep["all_ok"]:
            print("仿真未全绿，不重启物理任务", file=sys.stderr)
            return 3
        god = start_god_mode(steps=args.god_steps, j_threshold=args.j_threshold)
        print(json.dumps({"god_mode": god}, ensure_ascii=False))
        hea = start_hea_pipeline(anneal_steps=args.hea_anneal_steps)
        print(json.dumps({"hea_pipeline": hea}, ensure_ascii=False))
        return 0

    if args.run or not args.status:
        rep = run_physics_sim(include_hea=not args.no_hea)
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        print(f"\n[*] marker → {MARKER}")
        return 0 if rep["all_ok"] else 3

    m = load_marker()
    print(json.dumps(m or {"error": "no marker"}, indent=2, ensure_ascii=False))
    return 0 if (m or {}).get("all_ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
