#!/usr/bin/env python3
"""Thin wrapper: run pass-v1 harvest + physics, sync manifest into CFT release data/."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASS = Path(os.environ.get("PASS_V1_ROOT", Path.home() / "Desktop" / "foliation-pass-v1"))
HARVEST = PASS / "scripts/data_harvester/run_harvest.sh"
PIPELINE = PASS / "scripts/run_physics_with_harvest.sh"


def main() -> int:
    if not PASS.is_dir():
        print(f"ERROR: PASS_V1_ROOT missing: {PASS}", file=sys.stderr)
        return 1
    env = {**os.environ, "PASS_V1_ROOT": str(PASS), "FOLIATION_CFT_ROOT": str(ROOT)}
    steps: list[list[str]] = []
    if HARVEST.is_file():
        steps.append(["bash", str(HARVEST), "--limit", "150"])
    if PIPELINE.is_file():
        steps.append(["bash", str(PIPELINE)])
    for cmd in steps:
        print(">>", " ".join(cmd))
        p = subprocess.run(cmd, cwd=PASS, env=env)
        if p.returncode != 0:
            print(f"WARN: exit {p.returncode} for {' '.join(cmd)}")
    manifest = PASS / "artifacts/data_harvester/sources_manifest.json"
    if manifest.is_file():
        dest = ROOT / "data" / "sources_manifest.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(manifest, dest)
        print(f"[*] synced manifest → {dest}")
    killer = PASS / "artifacts/data_harvester/killer_selection.json"
    if killer.is_file():
        dest = ROOT / "data" / "sc" / "killer_selection.json"
        shutil.copy2(killer, dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
