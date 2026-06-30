#!/usr/bin/env python3
"""Resolve physics chip netlists: pass-v1 canonical paths ← Foliation-CFT-Chip (GitHub)."""
from __future__ import annotations

import os
import shutil
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASS_V1 = Path(os.environ.get("PASS_V1_ROOT", Path.home() / "Desktop" / "foliation-pass-v1"))
DEFAULT_CFT = Path.home() / "Foliation-CFT-Chip"

# pass-v1 relative path → CFT release data/chips/*/netlist.aig
CHIP_MAP: dict[str, tuple[str, str]] = {
    "cft": (
        "artifacts/gcu_test/extensions_collapsed_v2.aig",
        "data/chips/cft/netlist.aig",
    ),
    "fpu": (
        "artifacts/gcu_test/extensions_universal_v3.aig",
        "data/chips/fpu/netlist.aig",
    ),
    "qe_accel": (
        "artifacts/gcu_missing/fpu_extensions_raw.aig",
        "data/chips/qe_accel/netlist.aig",
    ),
}

TAPEOUT_CHIPS = ("cft", "fpu", "qe_accel")


def cft_root() -> Path:
    explicit = os.environ.get("FOLIATION_CFT_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return DEFAULT_CFT


def pass_aig(chip: str) -> Path:
    rel, _ = CHIP_MAP[chip]
    return PASS_V1 / rel


def cft_aig(chip: str) -> Path:
    _, rel = CHIP_MAP[chip]
    return cft_root() / rel


def resolve_aig(chip: str) -> Path | None:
    """Resolve an AIG according to FOLIATION_NETLIST_SOURCE=cft|pass-v1|auto."""
    source = os.environ.get("FOLIATION_NETLIST_SOURCE", "auto").strip().lower()
    if source == "cft":
        order = (cft_aig(chip), pass_aig(chip))
    elif source in ("pass", "pass-v1", "pass_v1"):
        order = (pass_aig(chip), cft_aig(chip))
    else:
        order = (pass_aig(chip), cft_aig(chip))
    for path in order:
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > 64:
                return path
        except OSError:
            continue
    return None


def sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def universal_fpu_aig() -> Path:
    p = resolve_aig("fpu")
    return p if p else pass_aig("fpu")


def materialize_chip_netlists(*, dry_run: bool = False) -> dict[str, object]:
    """Copy CFT data/chips/*.aig → pass-v1 canonical paths (original pipeline)."""
    cft = cft_root()
    actions: list[dict[str, str]] = []
    for chip, (pass_rel, _) in CHIP_MAP.items():
        dst = PASS_V1 / pass_rel
        src = cft_aig(chip)
        action = "skip"
        if src.is_file():
            src_sha = sha256(src)
            dst_sha = sha256(dst) if dst.is_file() else None
            need = bool(src_sha and src_sha != dst_sha)
            if need:
                action = "copy" if not dry_run else "would_copy"
                if not dry_run:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
        actions.append(
            {
                "chip": chip,
                "src": str(src),
                "dst": str(dst),
                "action": action,
                "src_sha256": sha256(src) or "",
                "dst_sha256": sha256(dst) or "",
            }
        )
    return {"cft_root": str(cft), "actions": actions}


def materialize_tapeout_from_cft(*, dry_run: bool = False) -> dict[str, object]:
    """Symlink sky130 tapeout dirs from CFT when pass-v1 copy missing."""
    cft = cft_root()
    out: list[dict[str, str]] = []
    pass_root = PASS_V1 / "artifacts/physics_tapeout/sky130"
    for chip in TAPEOUT_CHIPS:
        src = cft / "data/chips" / chip
        dst = pass_root / chip
        action = "skip"
        if src.is_dir() and not dst.is_dir():
            action = "link" if not dry_run else "would_link"
            if not dry_run:
                pass_root.mkdir(parents=True, exist_ok=True)
                dst.symlink_to(src)
        elif src.is_dir() and dst.is_dir() and not any(dst.iterdir()):
            action = "link" if not dry_run else "would_link"
            if not dry_run:
                shutil.rmtree(dst)
                dst.symlink_to(src)
        out.append({"chip": chip, "src": str(src), "dst": str(dst), "action": action})
    manifest_src = cft / "data/chips/MANIFEST.json"
    manifest_dst = pass_root / "MANIFEST.json"
    if manifest_src.is_file() and not manifest_dst.is_file() and not dry_run:
        pass_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(manifest_src, manifest_dst)
    return {"actions": out}
