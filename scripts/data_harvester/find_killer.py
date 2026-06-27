#!/usr/bin/env python3
"""Find smallest 2D CIF with killer TM (Cu/V/Nb/Ta/Ti/Mo/W) and no magnetic poisons."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ase.io import read

from common import CIF_READY_DIR, HEAVY_CIF_DIR

KILLER_TM = frozenset({"Cu", "V", "Nb", "Ta", "Ti", "Mo", "W"})
MAGNETIC_POISONS = frozenset({"Fe", "Co", "Ni", "Mn", "Cr"})
DOCKER_IMAGE = "starforge_physics:latest"
QE_PSEUDO = "/usr/share/espresso/pseudo"
LOCAL_PSEUDO = Path(__file__).resolve().parents[2] / "artifacts" / "abinitio" / "qe" / "pseudo"


def docker_pslib_elements(image: str) -> set[str]:
    if shutil.which("docker") is None:
        return set()
    try:
        probe = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                image,
                "bash",
                "-lc",
                f'ls -1 {QE_PSEUDO}/*.* 2>/dev/null | sed "s/.*\\///" | cut -d. -f1 | sort -u',
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return {line.strip() for line in probe.stdout.splitlines() if line.strip()}
    except (subprocess.SubprocessError, OSError):
        return set()


def score_cif(cif: Path) -> dict | None:
    formula = cif.stem.split("_")[0]
    try:
        atoms = read(str(cif), format="cif")
        elems = set(atoms.get_chemical_symbols())
        nat = len(atoms)
    except Exception:  # noqa: BLE001
        return None

    if not elems & KILLER_TM:
        return None
    if elems & MAGNETIC_POISONS:
        return None

    killer_hits = sorted(elems & KILLER_TM)
    return {
        "path": str(cif.resolve()),
        "filename": cif.name,
        "formula": formula,
        "elements": sorted(elems),
        "killer_tm": killer_hits,
        "n_atoms": nat,
    }


def pseudo_ready(row: dict, pslib: set[str]) -> bool:
    """True when every element has a staged {El}.upf (local cache or Docker PSLibrary)."""
    for el in row["elements"]:
        if (LOCAL_PSEUDO / f"{el}.upf").is_file():
            continue
        if el not in pslib:
            return False
    return True


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Rank killer-TM 2D CIFs for mass sweep")
    ap.add_argument("--top", type=int, default=30, help="sweep pool size written to killer_selection.json")
    args = ap.parse_args()
    top_n = max(1, args.top)

    pslib = docker_pslib_elements(DOCKER_IMAGE)
    roots = [CIF_READY_DIR, HEAVY_CIF_DIR]
    hits: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for cif in sorted(root.glob("*.cif")):
            key = cif.name
            if key in seen:
                continue
            seen.add(key)
            row = score_cif(cif)
            if row:
                row["pseudo_ready"] = pseudo_ready(row, pslib)
                hits.append(row)

    if not hits:
        print("ERROR: no killer TM structure in harvest cache")
        return 1

    hits.sort(key=lambda r: (r["n_atoms"], r["filename"]))
    chemistry_winner = hits[0]
    executable = [h for h in hits if h["pseudo_ready"]]
    executable.sort(key=lambda r: (r["n_atoms"], r["filename"]))
    pipeline_winner = executable[0] if executable else chemistry_winner

    payload = {
        "chemistry_winner": chemistry_winner,
        "pipeline_winner": pipeline_winner,
        "pool_size": len(hits),
        "pseudo_ready_count": len(executable),
        "pslibrary_elements": sorted(pslib),
        "top10": hits[:10],
        "sweep_pool": hits[:top_n],
        "sweep_pool_size": min(top_n, len(hits)),
    }
    out = Path(__file__).resolve().parents[2] / "artifacts" / "data_harvester" / "killer_selection.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"[KILLER] pool={len(hits)} pseudo_ready={len(executable)}")
    print(f"  chemistry winner: {chemistry_winner['formula']} ({chemistry_winner['filename']}, n={chemistry_winner['n_atoms']})")
    print(f"    TM: {', '.join(chemistry_winner['killer_tm'])}")
    if not chemistry_winner["pseudo_ready"]:
        missing = sorted(set(chemistry_winner["elements"]) - pslib)
        print(f"    missing PSLibrary UPF: {', '.join(missing)}")
    print(f"  pipeline winner: {pipeline_winner['formula']} ({pipeline_winner['filename']}, n={pipeline_winner['n_atoms']})")
    print(f"    TM: {', '.join(pipeline_winner['killer_tm'])}")
    print(f"    path: {pipeline_winner['path']}")
    print(f"[*] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
