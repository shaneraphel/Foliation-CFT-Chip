"""Shared harvester filters, paths, and CIF export."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
HEAVY_CIF_DIR = ROOT / "artifacts" / "data_harvester" / "heavy_cif_ready"
CIF_READY_DIR = ROOT / "artifacts" / "data_harvester" / "cif_ready"
INVERSE_CIF_DIR = ROOT / "artifacts" / "data_harvester" / "inverse_matched"
CACHE_DIR = ROOT / "artifacts" / "data_harvester" / "cache"

MAGNETIC_POISONS = frozenset(
    {
        "Fe", "Co", "Ni", "Mn", "Cr", "Gd", "Nd", "Sm", "Eu", "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "U", "Pu",
    }
)

ORBITAL_ELEMENTS = {
    "d": frozenset({"Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn", "Y", "Zr", "Nb", "Mo", "W"}),
    "f": frozenset({"La", "Ce", "Pr", "Nd", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "Lu", "U"}),
}


def parse_formula_elements(formula: str) -> set[str]:
    return set(re.findall(r"[A-Z][a-z]?", formula or ""))


def has_magnetic_poison(formula: str, elements: list[str] | None = None) -> bool:
    elems = set(elements or []) or parse_formula_elements(formula)
    return bool(elems & MAGNETIC_POISONS)


def has_orbital(formula: str, orbitals: list[str]) -> bool:
    elems = parse_formula_elements(formula)
    for orb in orbitals:
        pool = ORBITAL_ELEMENTS.get(orb.lower())
        if pool and elems & pool:
            return True
    return not orbitals


def lattice_in_band(a: float | None, target: float, tolerance_frac: float) -> bool:
    if a is None or a <= 0:
        return False
    lo = target * (1.0 - tolerance_frac)
    hi = target * (1.0 + tolerance_frac)
    return lo <= a <= hi


def export_cif(structure: Any, path: Path) -> None:
    from pymatgen.io.cif import CifWriter

    path.parent.mkdir(parents=True, exist_ok=True)
    CifWriter(structure, symprec=0.01).write_file(str(path))


def row_meta(db_id: str, formula: str, source: str, e_hull: float, **extra: Any) -> dict[str, Any]:
    return {"db_id": db_id, "formula": formula, "source": source, "e_hull": e_hull, **extra}
