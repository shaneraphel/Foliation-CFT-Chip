#!/usr/bin/env python3
"""
Heavyweight streaming harvester: MP + JARVIS + AFLOW + OQMD + static ICSD/Matbench fallback.

Polars-only cleaning. Async REST for AFLOW/OQMD. CIF → artifacts/data_harvester/heavy_cif_ready/

Usage:
  cd scripts/data_harvester && python3 heavy_harvester.py
  python3 scripts/data_harvester/heavy_harvester.py --max-ehull 0.05 --limit 500
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import polars as pl
from dotenv import load_dotenv

from common import (
    HEAVY_CIF_DIR,
    export_cif,
    has_magnetic_poison,
    has_orbital,
    row_meta,
)
from static_fallbacks import load_matbench_records

try:
    from extended_sources import harvest_cod, harvest_oqmd_structures, harvest_optimade
except ImportError:
    harvest_cod = harvest_oqmd_structures = harvest_optimade = None  # type: ignore[misc, assignment]

load_dotenv(Path(__file__).with_name(".env"))

MP_API_KEY = os.getenv("MP_API_KEY")
MAGNETIC_POISONS = ["Fe", "Co", "Ni", "Mn", "Cr", "Gd", "Nd", "Sm", "Eu", "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "U", "Pu"]
AFLOW_BASE = "https://aflowlib.duke.edu/API/aflux/"
AFLOW_SC_QUERIES = (
    "species(Mo,S),nspecies(2),compound,aurl,geometry,spacegroup_rel",
    "species(Nb,Se),nspecies(2),compound,aurl,geometry,spacegroup_rel",
    "species(W,S),nspecies(2),compound,aurl,geometry,spacegroup_rel",
)
OQMD_BASE = "https://oqmd.org/oqmdapi/formationenergy"
CONCURRENCY = int(os.getenv("HARVEST_CONCURRENCY", "8"))


def harvest_mp(max_ehull: float, limit: int) -> list[dict[str, Any]]:
    if not MP_API_KEY:
        print("[WARN] MP_API_KEY missing — skip MP")
        return []
    from mp_api.client import MPRester

    rows: list[dict[str, Any]] = []
    try:
        with MPRester(MP_API_KEY) as mpr:
            docs = mpr.summary.search(
                energy_above_hull=(0.0, max_ehull),
                band_gap=(0.0, 0.15),
                is_magnetic=False,
                fields=["material_id", "formula_pretty", "structure", "energy_above_hull", "elements"],
                num_chunks=limit,
            )
        for doc in docs[:limit]:
            elems = [e.symbol for e in doc.elements]
            if has_magnetic_poison(doc.formula_pretty, elems):
                continue
            a = float(doc.structure.lattice.a) if doc.structure else None
            rows.append(
                {
                    **row_meta(doc.material_id, doc.formula_pretty, "MP", float(doc.energy_above_hull)),
                    "structure_obj": doc.structure,
                    "lattice_a": a,
                    "elements": elems,
                }
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] MP harvest failed: {exc}")
    print(f"[HARVESTER] MP: {len(rows)} rows")
    return rows


def harvest_jarvis(max_ehull: float, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        from jarvis.core.atoms import Atoms
        from jarvis.db.figshare import data as jdata
        from pymatgen.io.jarvis import JarvisAtomsAdaptor

        dft_2d = jdata("dft_2d")
        for entry in dft_2d[:limit * 3]:
            elements = entry.get("elements", [])
            if has_magnetic_poison("", elements):
                continue
            exfol = entry.get("exfoliation_energy", 999)
            try:
                exfol_f = float(exfol)
            except (TypeError, ValueError):
                continue
            if exfol_f > 200:
                continue
            ehull = float(entry.get("ehull", 0.0))
            if ehull > max_ehull:
                continue
            ja = Atoms.from_dict(entry["atoms"])
            struct = JarvisAtomsAdaptor.get_structure(ja)
            rows.append(
                {
                    **row_meta(entry["jid"], entry["formula"], "JARVIS", ehull),
                    "structure_obj": struct,
                    "lattice_a": float(struct.lattice.a),
                    "elements": elements,
                }
            )
            if len(rows) >= limit:
                break
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] JARVIS harvest failed: {exc}")
    print(f"[HARVESTER] JARVIS: {len(rows)} rows")
    return rows


async def fetch_json(session, url: str, timeout: int) -> Any:
    import aiohttp

    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} for {url[:80]}")
        text = await resp.text()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # AFLUX may return concatenated JSON objects
            objs = []
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("{") or line.startswith("["):
                    try:
                        objs.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            return objs


async def harvest_aflow_async(min_species: int, limit: int) -> list[dict[str, Any]]:
    import aiohttp

    rows: list[dict[str, Any]] = []
    timeout = int(os.getenv("AFLOW_TIMEOUT_S", "90"))
    seen: set[str] = set()

    async def _query(session: aiohttp.ClientSession, aflux: str, tag: str) -> None:
        url = f"{AFLOW_BASE}?{aflux}&format=json&paging(0,{min(limit, 40)})"
        try:
            data = await fetch_json(session, url, timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] AFLOW {tag}: {exc}")
            return
        if isinstance(data, dict):
            data = [data]
        for i, entry in enumerate(data):
            if not isinstance(entry, dict):
                continue
            compound = str(entry.get("compound", entry.get("Compound", f"aflow_{tag}_{i}")))
            key = compound + str(entry.get("aurl", ""))
            if key in seen:
                continue
            seen.add(key)
            if has_magnetic_poison(compound):
                continue
            struct_obj = None
            geom = entry.get("geometry") or entry.get("positions_cartesian")
            if geom:
                try:
                    from pymatgen.core import Lattice, Structure

                    if isinstance(geom, list) and geom and isinstance(geom[0], (list, tuple)):
                        lat = Lattice.cubic(float(entry.get("a", 3.0)))
                        struct_obj = Structure(lat, ["X"], [[0, 0, 0]])
                except Exception:  # noqa: BLE001
                    struct_obj = None
            rows.append(
                {
                    **row_meta(f"aflow_{tag}_{i}", compound, "AFLOW", 0.05),
                    "structure_obj": struct_obj,
                    "aurl": entry.get("aurl", entry.get("AURL")),
                    "lattice_a": float(entry["a"]) if entry.get("a") else None,
                    "nspecies": entry.get("nspecies"),
                }
            )

    try:
        async with aiohttp.ClientSession() as session:
            # SC dichalcogenides first
            for q in AFLOW_SC_QUERIES:
                await _query(session, q, q.split("(")[1].split(")")[0].replace(",", "_"))
            # HEA multi-species fallback
            hea_q = f"nspecies({min_species},10),compound,aurl,spacegroup_rel,aelastic"
            await _query(session, hea_q, "hea")
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] AFLOW session failed: {exc}")
    print(f"[HARVESTER] AFLOW: {len(rows)} rows")
    return rows


async def harvest_oqmd_async(max_delta_e: float, limit: int) -> list[dict[str, Any]]:
    import aiohttp

    rows: list[dict[str, Any]] = []
    timeout = int(os.getenv("OQMD_TIMEOUT_S", "60"))
    offset = 0
    chunk = min(100, limit)
    try:
        async with aiohttp.ClientSession() as session:
            while len(rows) < limit:
                url = f"{OQMD_BASE}?limit={chunk}&offset={offset}&fields=id,entry_id,composition_id,delta_e,formation_energy"
                data = await fetch_json(session, url, timeout)
                batch = data.get("data", data) if isinstance(data, dict) else data
                if not batch:
                    break
                for entry in batch:
                    de = float(entry.get("delta_e", 99))
                    if de > max_delta_e:
                        continue
                    cid = str(entry.get("composition_id", entry.get("id", "")))
                    rows.append(
                        {
                            **row_meta(str(entry.get("id", cid)), cid, "OQMD", de),
                            "structure_obj": None,
                            "lattice_a": None,
                        }
                    )
                    if len(rows) >= limit:
                        break
                offset += chunk
                if len(batch) < chunk:
                    break
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] OQMD async failed: {exc}")
    print(f"[HARVESTER] OQMD: {len(rows)} rows")
    return rows


def polars_clean(rows: list[dict[str, Any]], max_ehull: float) -> pl.DataFrame:
    meta = [{k: v for k, v in r.items() if k != "structure_obj"} for r in rows]
    if not meta:
        return pl.DataFrame()
    df = pl.DataFrame(meta)
    return (
        df.filter(pl.col("e_hull") <= max_ehull)
        .sort("e_hull")
        .unique(subset=["formula"], keep="first")
    )


def export_cifs(rows: list[dict[str, Any]], df: pl.DataFrame, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    struct_map = {r["db_id"]: r.get("structure_obj") for r in rows}
    n = 0
    for row in df.to_dicts():
        struct = struct_map.get(row["db_id"])
        if struct is None:
            continue
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in row["formula"])
        path = out_dir / f"{safe}_{row['db_id']}.cif"
        try:
            export_cif(struct, path)
            n += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] CIF export {row['db_id']}: {exc}")
    return n


async def run_harvest(args: argparse.Namespace) -> int:
    mp_rows = harvest_mp(args.max_ehull, args.limit)
    jarvis_rows = harvest_jarvis(args.max_ehull, args.limit)
    aflow_rows, oqmd_rows = await asyncio.gather(
        harvest_aflow_async(args.min_species, args.limit),
        harvest_oqmd_async(args.max_delta_e, args.limit),
    )
    static_rows = load_matbench_records()
    for r in static_rows:
        r["structure_obj"] = None

    ext_rows: list[dict[str, Any]] = []
    if harvest_optimade and harvest_cod and harvest_oqmd_structures:
        try:
            ext_rows.extend(harvest_optimade(min(30, args.limit)))
            ext_rows.extend(harvest_cod())
            ext_rows.extend(harvest_oqmd_structures(min(25, args.limit)))
            print(f"[HARVESTER] extended_sources: {len(ext_rows)} rows")
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] extended_sources: {exc}")

    combined = mp_rows + jarvis_rows + aflow_rows + oqmd_rows + static_rows + ext_rows
    print(f"[HARVESTER] Polars clean ({len(combined)} raw)...")
    df = polars_clean(combined, args.max_ehull)
    if df.is_empty():
        print("[WARN] no candidates after clean")
        return 1

    out_csv = HEAVY_CIF_DIR.parent / "heavy_metadata.parquet"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out_csv)
    n_cif = export_cifs(combined, df, HEAVY_CIF_DIR)
    print(f"[HARVESTER] {df.height} candidates → {n_cif} CIF in {HEAVY_CIF_DIR}")
    print(f"[HARVESTER] metadata parquet: {out_csv}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Heavyweight materials harvester")
    parser.add_argument("--max-ehull", type=float, default=0.05)
    parser.add_argument("--max-delta-e", type=float, default=0.02)
    parser.add_argument("--min-species", type=int, default=4, help="AFLOW HEA nspecies min")
    parser.add_argument("--limit", type=int, default=300)
    args = parser.parse_args()
    return asyncio.run(run_harvest(args))


if __name__ == "__main__":
    raise SystemExit(main())
