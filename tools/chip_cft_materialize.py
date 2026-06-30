#!/usr/bin/env python3
"""Materialize GitHub Foliation-CFT-Chip netlists into pass-v1 (restore original physics pipeline)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from tools.foliation_chip_paths import (  # noqa: E402
    materialize_chip_netlists,
    materialize_tapeout_from_cft,
    resolve_aig,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="CFT GitHub chips → pass-v1 canonical AIG paths")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tapeout", action="store_true", help="also link sky130 tapeout from CFT")
    args = ap.parse_args()

    rep: dict[str, object] = {
        "netlists": materialize_chip_netlists(dry_run=args.dry_run),
        "resolved": {c: str(resolve_aig(c) or "") for c in ("cft", "fpu", "qe_accel")},
    }
    if args.tapeout:
        rep["tapeout"] = materialize_tapeout_from_cft(dry_run=args.dry_run)

    out = ENGINE / "out/chip/cft_materialize.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        out.write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(json.dumps(rep, indent=2, ensure_ascii=False))
    missing = [c for c, p in rep["resolved"].items() if not p]  # type: ignore[union-attr]
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
