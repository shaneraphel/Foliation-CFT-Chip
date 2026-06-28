#!/usr/bin/env python3
"""挂载 iCloud Archive — 零 pull，本地路径 symlink → iCloud 完整文件."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.foliation_artifact_paths import DEFAULT_ICLOUD, icloud_root  # noqa: E402

# (local_rel, icloud_rel) — 只挂载重资产，代码仍在 repo
MOUNT_PAIRS: tuple[tuple[str, str], ...] = (
    ("out/chip/crystal_forge", "out/chip/crystal_forge"),
    ("artifacts/crystal_forge", "artifacts/crystal_forge"),
    ("artifacts/gcu_rom", "artifacts/gcu_rom"),
    ("artifacts/er100_mass_parallel", "artifacts/er100_mass_parallel"),
    ("artifacts/abinitio/runs", "artifacts/abinitio/runs"),
    ("data/gcu_topological_solution", "data/gcu_topological_solution"),
    ("training_sandbox/data/akasha", "training_sandbox/data/akasha"),
    ("training_sandbox/checkpoints", "training_sandbox/checkpoints"),
)


def _link(local: Path, cloud: Path, *, dry_run: bool) -> dict:
    if local.resolve() == cloud.resolve():
        return {"local": str(local), "cloud": str(cloud), "action": "skip_same_path"}
    if not cloud.exists():
        return {"local": str(local), "cloud": str(cloud), "action": "skip_missing_cloud"}
    if local.is_symlink() and local.resolve() == cloud.resolve():
        return {"local": str(local), "cloud": str(cloud), "action": "ok_symlink"}
    if local.exists() and not local.is_symlink():
        bak = local.with_name(local.name + ".local.bak")
        if bak.exists():
            shutil.rmtree(bak) if bak.is_dir() else bak.unlink()
        if dry_run:
            return {"local": str(local), "cloud": str(cloud), "action": f"would_backup→{bak}"}
        local.rename(bak)
    if dry_run:
        return {"local": str(local), "cloud": str(cloud), "action": "would_symlink"}
    local.parent.mkdir(parents=True, exist_ok=True)
    if local.exists() or local.is_symlink():
        local.unlink()
    local.symlink_to(cloud)
    return {"local": str(local), "cloud": str(cloud), "action": "linked"}


def mount(*, icloud: Path | None = None, dry_run: bool = False) -> dict:
    cloud_root = icloud or icloud_root() or DEFAULT_ICLOUD
    if not cloud_root.is_dir():
        raise SystemExit(f"iCloud archive missing: {cloud_root}")
    results = []
    for local_rel, cloud_rel in MOUNT_PAIRS:
        local = ROOT / local_rel
        cloud = cloud_root / cloud_rel
        cloud.parent.mkdir(parents=True, exist_ok=True)
        results.append(_link(local, cloud, dry_run=dry_run))
    report = {
        "schema": "foliation.icloud_mount.v1",
        "ts": datetime.now(timezone.utc).isoformat(),
        "icloud_root": str(cloud_root),
        "dry_run": dry_run,
        "mounts": results,
    }
    log = ROOT / "out/chip/logs/icloud_mount.json"
    if not dry_run:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Symlink local heavy dirs → iCloud (no pull)")
    ap.add_argument("--icloud", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    os.environ.setdefault("FOLIATION_USE_ICLOUD", "1")
    rep = mount(icloud=args.icloud, dry_run=args.dry_run)
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
