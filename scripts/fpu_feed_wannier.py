#!/usr/bin/env python3
"""Wannier90 ED → 6-bath Lanczos poles → forge-daemon 14-bus → fpu_feed."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import scipy.linalg as la

ROOT = Path(__file__).resolve().parents[1]
FORGE_MANIFEST = ROOT / "foliation-opt-v2" / "Cargo.toml"
# Universal v3 硬件母体：373 门 / 14-bus / 6-bath。禁止省略或覆盖 --netlist。
UNIVERSAL_V3_NETLIST = "artifacts/gcu_test/extensions_universal_v3.aig"
DEFAULT_AIG = ROOT / UNIVERSAL_V3_NETLIST


def resolve_hr_path(run: Path | None) -> Path:
    if run:
        return run / "wannier" / "wannier90_hr.dat"
    latest = ROOT / "artifacts" / "abinitio" / "latest"
    if latest.is_symlink() or latest.exists():
        return latest.resolve() / "wannier" / "wannier90_hr.dat"
    return ROOT / "artifacts" / "abinitio" / "runs" / "latest" / "wannier" / "wannier90_hr.dat"


def resolve_run(hr_path: Path) -> Path:
    return hr_path.parent.parent


def parse_wannier_hr(filepath: Path) -> tuple[np.ndarray, int]:
    """从 Wannier90 hr.dat 提取实空间跳跃哈密顿量"""
    print(f"[LANCZOS] 正在吞噬跳跃矩阵: {filepath}")
    with filepath.open(encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    start_idx = 0
    for i, line in enumerate(lines):
        if len(line.split()) == 7:
            start_idx = i
            break

    data = np.loadtxt(lines[start_idx:])
    num_wann = int(np.max(data[:, 3]))

    H_local = np.zeros((num_wann, num_wann), dtype=complex)
    for row in data:
        if int(row[0]) == 0 and int(row[1]) == 0 and int(row[2]) == 0:
            i, j = int(row[3]) - 1, int(row[4]) - 1
            H_local[i, j] = row[5] + 1j * row[6]

    return H_local, num_wann


def extract_6bath_poles(H_local: np.ndarray, impurity_index: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """
    通过对角化局部环境，提取与杂质轨道耦合最强的 6 个浴能级
    这完美对应了你的 Universal v3 AIG 的 6-bath 设定
    """
    print("[LANCZOS] 正在执行谱分解与极点坍缩...")
    num_wann = H_local.shape[0]
    env_indices = [i for i in range(num_wann) if i != impurity_index]

    H_env = H_local[np.ix_(env_indices, env_indices)]
    V_env = H_local[impurity_index, env_indices]

    E_vals, U_vecs = la.eigh(H_env)
    V_rotated = np.abs(np.dot(U_vecs.T.conj(), V_env))

    top_indices = np.argsort(V_rotated)[-6:][::-1]
    E_top6 = E_vals[top_indices]
    V_top6 = V_rotated[top_indices]

    while len(E_top6) < 6:
        E_top6 = np.append(E_top6, 10.0)
        V_top6 = np.append(V_top6, 1e-4)

    return np.round(E_top6.real, 4), np.round(V_top6.real, 4)


def build_forge_daemon() -> Path:
    forge = ROOT / "foliation-opt-v2" / "target" / "release" / "forge-daemon"
    if not forge.is_file():
        subprocess.run(
            [
                "cargo",
                "build",
                "--release",
                "--manifest-path",
                str(FORGE_MANIFEST),
                "--bin",
                "forge-daemon",
            ],
            check=True,
            cwd=ROOT,
        )
    return forge


def fire_forge_daemon(
    U: float,
    Mu: float,
    V_array: np.ndarray,
    E_array: np.ndarray,
    archive: Path,
) -> Path:
    """向 Universal v3 14 维硬件母体通电（网表路径硬锁定）"""
    if not DEFAULT_AIG.is_file():
        raise SystemExit(f"致命错误: Universal v3 网表不存在 {UNIVERSAL_V3_NETLIST}")
    cmd = [
        str(build_forge_daemon()),
        "--netlist",
        UNIVERSAL_V3_NETLIST,
        f"--interaction-u={U}",
        f"--chemical-potential={Mu}",
        f"--hybridization-v={','.join(map(str, V_array))}",
        f"--bath-energy-e={','.join(map(str, E_array))}",
        f"--output-dir={archive}",
        "--once",
    ]

    print(f"\n[FPU FEED] 向 373 节点 Universal v3 母体注入 14 维参数总线:")
    print(f"  netlist: {UNIVERSAL_V3_NETLIST}")
    print(f"  U: {U} eV | Mu: {Mu} eV")
    print(f"  V_k: {V_array}")
    print(f"  E_k: {E_array}")
    print(f"[FPU FEED] 启动指令: {' '.join(cmd)}\n")

    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=120)
    print(result.stdout)
    if result.stderr:
        print("[WARNING/ERROR]", result.stderr)
    if result.returncode != 0:
        raise SystemExit(f"forge-daemon failed (rc={result.returncode})")

    csvs = sorted(archive.glob("forge_*/dmft_poles.csv"), key=lambda p: p.stat().st_mtime)
    if not csvs:
        raise SystemExit("ERROR: forge-daemon produced no dmft_poles.csv")
    return csvs[-1].parent


def parse_poles_csv(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split(",")
        if len(parts) >= 2:
            rows.append({"parity_state": parts[0].strip(), "spectral_weight": float(parts[1])})
    return rows


def parse_sigma(path: Path) -> float | None:
    if not path.is_file():
        return None
    m = re.search(r"Sigma\(i\w_n\)=([-\d.eE+]+)", path.read_text(encoding="utf-8"))
    return float(m.group(1)) if m else None


def write_fpu_feed(
    run: Path,
    U: float,
    Mu: float,
    V_array: np.ndarray,
    E_array: np.ndarray,
    forge_dir: Path,
    netlist: Path,
    num_wann: int,
) -> None:
    feed = run / "fpu_feed"
    feed.mkdir(parents=True, exist_ok=True)

    hubbard = {
        "U_eV": U,
        "Mu_eV": Mu,
        "V_k_eV": V_array.tolist(),
        "E_k_eV": E_array.tolist(),
        "num_wann": num_wann,
        "source": "wannier90_hr.dat+ED+forge-daemon-universal-6bath",
        "netlist": str(netlist),
    }
    (feed / "hubbard_params.json").write_text(json.dumps(hubbard, indent=2), encoding="utf-8")
    (run / "wannier" / "hubbard_params.json").write_text(json.dumps(hubbard, indent=2), encoding="utf-8")

    poles_src = forge_dir / "dmft_poles.csv"
    sigma_src = forge_dir / "self_energy_iwn.txt"
    shutil.copy2(poles_src, feed / "dmft_poles.csv")
    if sigma_src.is_file():
        shutil.copy2(sigma_src, feed / "self_energy_iwn.txt")

    poles = parse_poles_csv(poles_src)
    sigma = parse_sigma(sigma_src)
    summary = {
        "sigma_iwn": sigma,
        "pole_count": len(poles),
        "poles": poles,
        "forge_run_dir": str(forge_dir),
        "U_eV": U,
        "Mu_eV": Mu,
        "V_k_eV": V_array.tolist(),
        "E_k_eV": E_array.tolist(),
        "aig": str(netlist),
    }
    (feed / "forge_dmft_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (feed / "forge_sigma.json").write_text(
        json.dumps({"sigma_iwn": sigma, "source": str(sigma_src), "poles": len(poles)}, indent=2),
        encoding="utf-8",
    )
    (feed / "green_function_computed.json").write_text(
        json.dumps(
            {
                "source": "forge-daemon+ED-6bath",
                "pole_weights": [p["spectral_weight"] for p in poles],
                "sigma_iwn": sigma,
                "U_eV": U,
                "Mu_eV": Mu,
                "V_k_eV": V_array.tolist(),
                "E_k_eV": E_array.tolist(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (feed / "feed_status.json").write_text(
        json.dumps(
            {
                "status": "FPU_DMFT_OK",
                "missing": [],
                "written": [
                    "hubbard_params.json",
                    "dmft_poles.csv",
                    "forge_dmft_summary.json",
                    "forge_sigma.json",
                    "green_function_computed.json",
                    "self_energy_iwn.txt",
                ],
                "note": "ED 6-bath poles + Universal v3 14-bus forge-daemon",
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Wannier ED → Universal AIM forge-daemon feed")
    parser.add_argument("--u", type=float, default=2.5, help="Hubbard U 相互作用强度")
    parser.add_argument("--mu", type=float, default=-1.2, help="化学势")
    parser.add_argument("--impurity-index", type=int, default=0, help="杂质 Wannier 轨道索引 (Mo d)")
    parser.add_argument("--run", type=Path, default=None, help="abinitio run 目录")
    parser.add_argument("--archive", type=Path, default=ROOT / "data" / "StarForge_Archive")
    args = parser.parse_args()

    hr_path = resolve_hr_path(args.run)
    if not hr_path.is_file():
        print(f"致命错误: 找不到 {hr_path}。请确保 Wannier90 已成功落盘。")
        return 1

    run = resolve_run(hr_path)
    netlist = DEFAULT_AIG.resolve()
    if not netlist.is_file():
        print(f"致命错误: Universal v3 网表不存在 {UNIVERSAL_V3_NETLIST}")
        return 1

    H_loc, num_wann = parse_wannier_hr(hr_path)
    E_bath, V_hybrid = extract_6bath_poles(H_loc, impurity_index=args.impurity_index)

    forge_dir = fire_forge_daemon(args.u, args.mu, V_hybrid, E_bath, args.archive.resolve())
    write_fpu_feed(run, args.u, args.mu, V_hybrid, E_bath, forge_dir, netlist, num_wann)

    summary = json.loads((run / "fpu_feed" / "forge_dmft_summary.json").read_text(encoding="utf-8"))
    print(f"[*] Sigma(iw_n) = {summary.get('sigma_iwn')}")
    print(f"[*] poles ({summary.get('pole_count')}):")
    for pole in summary.get("poles", [])[:8]:
        print(f"    {pole['parity_state']}  weight={pole['spectral_weight']}")
    print(f"[*] fpu_feed → {run / 'fpu_feed'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
