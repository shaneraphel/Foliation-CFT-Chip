#!/usr/bin/env python3
"""Extended authoritative data sources: OPTIMADE, COD, OQMD structures, NOMAD index."""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from common import CIF_READY_DIR, CACHE_DIR, HEAVY_CIF_DIR, export_cif, has_magnetic_poison, parse_formula_elements, row_meta

load_dotenv(Path(__file__).with_name(".env"))

OPTIMADE_PROVIDERS = (
    ("MP", "https://optimade.materialsproject.org"),
    ("OQMD", "https://oqmd.org/optimade"),
)
COD_API = "https://www.crystallography.net/cod/result"
OQMD_STRUCT = "https://oqmd.org/oqmdapi/structure"
NOMAD_QUERY = "https://nomad-lab.eu/prod/v1/entries/query"
KILLER_TM = frozenset({"Cu", "V", "Nb", "Ta", "Ti", "Mo", "W"})
# Target 2D dichalcogenide stoichiometry → COD element search (formula= fails on COD API)
SC_TARGETS: dict[str, tuple[str, ...]] = {
    "MoS2": ("Mo", "S"),
    "NbSe2": ("Nb", "Se"),
    "WS2": ("W", "S"),
    "TaS2": ("Ta", "S"),
    "VSe2": ("V", "Se"),
}


def _get_json(url: str, *, timeout: int = 90, data: bytes | None = None, headers: dict | None = None) -> Any:
    req = urllib.request.Request(url, data=data, headers=headers or {"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _get_text(url: str, *, timeout: int = 90) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _norm_formula(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s or "")


def _stoich_match(target: str, entry: dict[str, Any]) -> bool:
    """True when COD entry is binary AB2 dichalcogenide matching target elements."""
    want = parse_formula_elements(target)
    if len(want) != 2:
        return False
    for key in ("calcformula", "formula", "cellformula"):
        raw = str(entry.get(key) or "")
        elems = parse_formula_elements(raw)
        if elems and elems == want:
            return True
    return _norm_formula(target) in _norm_formula(str(entry.get("calcformula", "")))


def optimade_filter_sc() -> str:
    tm = ",".join(f'"{e}"' for e in sorted(KILLER_TM))
    return f"elements HAS ANY {tm} AND nsites < 20"


def harvest_optimade(limit_per_provider: int = 40) -> list[dict[str, Any]]:
    from pymatgen.io.ase import AseAtomsAdaptor

    rows: list[dict[str, Any]] = []
    filt = urllib.parse.quote(optimade_filter_sc())
    for label, base in OPTIMADE_PROVIDERS:
        url = f"{base}/v1/structures?filter={filt}&page_limit={limit_per_provider}"
        headers: dict[str, str] = {"Accept": "application/vnd.api+json"}
        if label == "MP":
            key = os.getenv("MP_API_KEY")
            if not key:
                print(f"[EXT] {label} OPTIMADE skip (no MP_API_KEY)")
                continue
            headers["X-API-KEY"] = key
        try:
            data = _get_json(url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] OPTIMADE {label}: {exc}")
            continue
        n_before = len(rows)
        for entry in data.get("data", [])[:limit_per_provider]:
            attrs = entry.get("attributes", {})
            formula = str(
                attrs.get("chemical_formula_reduced")
                or attrs.get("chemical_formula_descriptive")
                or ""
            )
            if has_magnetic_poison(formula):
                continue
            lattice = attrs.get("lattice_vectors")
            positions = attrs.get("cartesian_site_positions") or attrs.get("positions")
            species = attrs.get("species_at_sites") or attrs.get("species")
            if not (lattice and positions and species):
                continue
            try:
                from ase import Atoms

                atoms = Atoms(symbols=species, positions=positions, cell=lattice, pbc=True)
                struct = AseAtomsAdaptor.get_structure(atoms)
            except Exception:  # noqa: BLE001
                continue
            sid = str(entry.get("id", f"{label}_{len(rows)}"))
            rows.append(
                {
                    **row_meta(sid, _norm_formula(formula), f"OPTIMADE_{label}", 0.03),
                    "structure_obj": struct,
                    "lattice_a": float(struct.lattice.a),
                }
            )
        print(f"[EXT] OPTIMADE {label}: {len(rows) - n_before}")
        time.sleep(0.5)
    return rows


def _parse_cod_cif(cif_text: str):
    from pymatgen.io.cif import CifParser

    parser = CifParser.from_string(cif_text)
    structs = parser.parse_structures(primitive=False)
    if not structs:
        structs = parser.parse_structures(primitive=True)
    return structs[0]


def _cod_prefilter(target: str, entry: dict[str, Any]) -> bool:
    if not _stoich_match(target, entry):
        return False
    blob = " ".join(str(entry.get(k, "")) for k in ("mineral", "commonname", "chemname", "title")).lower()
    hints = {
        "MoS2": ("molybdenite", "molybdenum disulfide", "mos2", "mo s2"),
        "NbSe2": ("niobium selenide", "nbse2", "2h-nbse2"),
        "WS2": ("tungsten disulfide", "ws2"),
        "TaS2": ("tantalum disulfide", "tas2"),
        "VSe2": ("vanadium selenide", "vse2"),
    }
    keys = hints.get(target, ())
    if any(h in blob for h in keys):
        return True
    try:
        z = int(float(entry.get("Z") or 99))
        nat = int(float(entry.get("nel") or 99))
        return z <= 6 and nat <= 6
    except (TypeError, ValueError):
        return True


def harvest_cod(limit_each: int = 5) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for target, (el1, el2) in SC_TARGETS.items():
        q = urllib.parse.urlencode(
            {"format": "json", "el1": el1, "el2": el2, "nel": "2", "count": str(limit_each * 6)}
        )
        try:
            data = _get_json(f"{COD_API}?{q}", timeout=45)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] COD {target}: {exc}", flush=True)
            continue
        entries = data if isinstance(data, list) else data.get("cod", data.get("data", []))
        candidates = [e for e in entries if isinstance(e, dict) and _cod_prefilter(target, e)]
        matched = 0
        for entry in candidates:
            code = str(entry.get("file") or entry.get("id") or entry.get("codid", "")).strip()
            if not code or not code.isdigit():
                continue
            try:
                cif_text = _get_text(f"https://www.crystallography.net/cod/{code}.cif", timeout=45)
                struct = _parse_cod_cif(cif_text)
            except Exception:  # noqa: BLE001
                continue
            f = _norm_formula(target)
            if has_magnetic_poison(f):
                continue
            rows.append(
                {
                    **row_meta(f"COD_{code}", f, "COD", 0.02),
                    "structure_obj": struct,
                    "lattice_a": float(struct.lattice.a),
                    "cod_id": code,
                    "cod_target": target,
                }
            )
            matched += 1
            if matched >= limit_each:
                break
        print(f"[EXT] COD {target}: {matched} structures (candidates={len(candidates)})", flush=True)
        time.sleep(0.15)
    print(f"[EXT] COD total rows: {len(rows)}", flush=True)
    return rows


def harvest_oqmd_structures(limit: int = 30, max_delta_e: float = 0.08) -> list[dict[str, Any]]:
    from pymatgen.core import Lattice, Structure

    rows: list[dict[str, Any]] = []
    url = (
        f"https://oqmd.org/oqmdapi/formationenergy?limit={limit}"
        f"&fields=id,entry_id,composition_id,delta_e,spacegroup,volume,natoms"
        f"&delta_e__lt={max_delta_e}"
    )
    try:
        data = _get_json(url)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] OQMD list: {exc}")
        return rows
    batch = data.get("data", []) if isinstance(data, dict) else data
    for entry in batch[:limit]:
        eid = entry.get("entry_id") if entry.get("entry_id") is not None else entry.get("id")
        if eid is None:
            continue
        cid = str(entry.get("composition_id", eid))
        if has_magnetic_poison(cid):
            continue
        try:
            sdata = _get_json(f"{OQMD_STRUCT}/{eid}?fields=cell,positions,species,composition")
            struct_blob = sdata.get("data", sdata)
            if isinstance(struct_blob, list):
                struct_blob = struct_blob[0] if struct_blob else {}
            cell = struct_blob.get("cell") or struct_blob.get("lattice")
            positions = struct_blob.get("positions") or struct_blob.get("cart_coords")
            species = struct_blob.get("species") or struct_blob.get("elements")
            if not (cell and positions and species):
                continue
            lat = Lattice(cell)
            struct = Structure(lat, species, positions, coords_are_cartesian=bool(struct_blob.get("cart_coords")))
        except Exception:  # noqa: BLE001
            continue
        rows.append(
            {
                **row_meta(str(eid), cid, "OQMD_STRUCT", float(entry.get("delta_e", 0.05))),
                "structure_obj": struct,
                "lattice_a": float(struct.lattice.a),
            }
        )
    print(f"[EXT] OQMD structures: {len(rows)}")
    return rows


def build_nomad_index(formulas: tuple[str, ...] = tuple(SC_TARGETS)) -> dict[str, Any]:
    index: dict[str, Any] = {"queried_at": int(time.time()), "hits": {}}
    for formula in formulas:
        body = json.dumps(
            {
                "query": {"formula": formula},
                "pagination": {"page_size": 5},
                "required": {"include": ["entry_id", "results.material", "upload_create_time"]},
            }
        ).encode()
        try:
            data = _get_json(
                NOMAD_QUERY,
                data=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            hits = []
            for ent in (data.get("data", {}) or {}).get("entries", [])[:5]:
                mat = (ent.get("results") or {}).get("material") or {}
                hits.append(
                    {
                        "entry_id": ent.get("entry_id"),
                        "formula": (mat.get("chemical_formula_hill") or formula),
                        "upload_time": ent.get("upload_create_time"),
                    }
                )
            index["hits"][formula] = hits
        except Exception as exc:  # noqa: BLE001
            index["hits"][formula] = {"error": str(exc)}
        time.sleep(0.4)
    return index


def export_rows(rows: list[dict[str, Any]]) -> int:
    n = 0
    for out_dir in (HEAVY_CIF_DIR, CIF_READY_DIR):
        out_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        struct = row.get("structure_obj")
        if struct is None:
            continue
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in row["formula"])
        name = f"{safe}_{row['db_id']}.cif"
        for out_dir in (HEAVY_CIF_DIR, CIF_READY_DIR):
            path = out_dir / name
            if path.is_file():
                continue
            try:
                export_cif(struct, path)
                n += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] export {name}: {exc}")
    return n


def write_manifest(rows: list[dict[str, Any]], nomad_index: dict[str, Any]) -> Path:
    manifest = {
        "ts": int(time.time()),
        "author": "Shan Yu",
        "release": "0.2.0",
        "sources": {
            "MP": bool(os.getenv("MP_API_KEY")),
            "JARVIS": True,
            "AFLOW": True,
            "OQMD": True,
            "OPTIMADE": [p[0] for p in OPTIMADE_PROVIDERS],
            "COD": list(SC_TARGETS),
            "OQMD_STRUCT": True,
            "NOMAD_INDEX": True,
            "MATBENCH_STATIC": True,
        },
        "extended_rows": len(rows),
        "by_source": {},
        "nomad_crossref": nomad_index,
    }
    for r in rows:
        src = r.get("source", "UNKNOWN")
        manifest["by_source"][src] = manifest["by_source"].get(src, 0) + 1
    out = HEAVY_CIF_DIR.parent / "sources_manifest.json"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "nomad_index.json").write_text(json.dumps(nomad_index, indent=2), encoding="utf-8")
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Extended materials data sources")
    ap.add_argument("--optimade-limit", type=int, default=30)
    ap.add_argument("--oqmd-struct-limit", type=int, default=25)
    ap.add_argument("--skip-nomad", action="store_true")
    args = ap.parse_args()

    rows: list[dict[str, Any]] = []
    rows.extend(harvest_optimade(args.optimade_limit))
    rows.extend(harvest_cod())
    rows.extend(harvest_oqmd_structures(args.oqmd_struct_limit))
    nomad = {} if args.skip_nomad else build_nomad_index()
    n = export_rows(rows)
    manifest = write_manifest(rows, nomad)
    print(f"[EXT] exported {n} new CIF paths; manifest → {manifest}")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
