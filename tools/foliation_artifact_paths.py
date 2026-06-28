#!/usr/bin/env python3
"""Resolve artifact roots: local repo vs iCloud Foliation-Engine-Archive."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_ICLOUD = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Foliation-Engine-Archive"


def icloud_root() -> Path | None:
    if os.environ.get("FOLIATION_USE_ICLOUD", "0") in ("0", "false", "False"):
        return None
    explicit = os.environ.get("FOLIATION_ICLOUD_ROOT", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_dir() else None
    return DEFAULT_ICLOUD if DEFAULT_ICLOUD.is_dir() else None


def mass_parallel_root() -> Path:
    icloud = icloud_root()
    if icloud:
        p = icloud / "artifacts" / "er100_mass_parallel"
    else:
        p = ROOT / "artifacts" / "er100_mass_parallel"
    p.mkdir(parents=True, exist_ok=True)
    return p


def gcu_rom_root() -> Path:
    icloud = icloud_root()
    if icloud:
        p = icloud / "artifacts" / "gcu_rom"
    else:
        p = ROOT / "artifacts" / "gcu_rom"
    p.mkdir(parents=True, exist_ok=True)
    return p


def abinitio_run_root(run_id: str) -> Path:
    icloud = icloud_root()
    if icloud:
        p = icloud / "artifacts" / "abinitio" / "runs" / run_id
    else:
        p = ROOT / "artifacts" / "abinitio" / "runs" / run_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def de_novo_root(run_id: str) -> Path:
    icloud = icloud_root()
    if icloud:
        p = icloud / "data" / "gcu_topological_solution" / "de_novo_drugs" / run_id
    else:
        p = ROOT / "data" / "gcu_topological_solution" / "de_novo_drugs" / run_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def pocket_audit_pdb(run_id: str) -> Path:
    icloud = icloud_root()
    base = icloud / "data" / "gcu_topological_solution" if icloud else ROOT / "data" / "gcu_topological_solution"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"er100_cryptic_pocket_{run_id}.pdb"
