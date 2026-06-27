#!/usr/bin/env python3
"""Crystal evidence ledger — classify forge candidates by falsifiable evidence.

This deliberately separates candidate evidence from hall-of-fame evidence:
only a real Phonopy PASS_STABLE verdict may graduate a formula.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PASS_V1 = Path(os.environ.get("PASS_V1_ROOT", Path.home() / "Desktop" / "foliation-pass-v1"))
OUT_DIR = ROOT / "artifacts" / "crystal_forge"


def _read_json(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _tail(path: Path, n: int = 24) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-n:]


def _docker_phonopy_progress() -> dict[str, Any]:
    try:
        ps = subprocess.run(
            ["docker", "ps", "--format", "{{.ID}} {{.Status}} {{.Names}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    containers = [line for line in ps.stdout.splitlines() if line.strip()]
    rows: list[dict[str, Any]] = []
    for line in containers:
        cid = line.split()[0]
        script = r"""
wd=$(ls -d /tmp/oracle_phonopy_* 2>/dev/null | head -1)
ok=0
for f in $wd/disp-*/pw.out; do
  [ -f "$f" ] && grep -q "JOB DONE" "$f" && ok=$((ok+1))
done
pw=$(pgrep -a pw.x 2>/dev/null | head -1)
echo "{\"workdir\":\"$wd\",\"job_done\":$ok,\"pw\":\"$pw\"}"
"""
        try:
            ex = subprocess.run(
                ["docker", "exec", cid, "bash", "-lc", script],
                capture_output=True,
                text=True,
                timeout=5,
            )
            payload = json.loads(ex.stdout.strip() or "{}")
        except Exception:
            payload = {}
        rows.append({"container": line, **payload})
    return {"ok": True, "containers": rows}


def _decode_summary(decode: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(decode, dict):
        return {"present": False}
    accepted = decode.get("accepted_recipe") or {}
    formula = accepted.get("formula") or {}
    fpu = accepted.get("fpu") or {}
    return {
        "present": True,
        "verdict": decode.get("verdict"),
        "formula": (
            formula.get("formula_pretty")
            or decode.get("accepted_formula")
            or decode.get("formula_input")
        ),
        "fpu_ok": fpu.get("ok"),
        "j_bkt": fpu.get("J_BKT") or decode.get("J_recheck"),
        "report_path": fpu.get("report_path"),
    }


def classify_evidence(
    *,
    decode: dict[str, Any],
    gcu: dict[str, Any],
    fast: dict[str, Any],
    stability: dict[str, Any],
    hall: list[Any],
    docker: dict[str, Any],
) -> tuple[str, list[str], list[str]]:
    missing: list[str] = []
    next_actions: list[str] = []

    decode_ok = decode.get("verdict") == "PASS_SYNTHESIS_RECIPE"
    gcu_ok = gcu.get("passed") is True and gcu.get("verdict") == "PASS_GCU_PRESCREEN"
    fast_ok = fast.get("verdict") == "PASS_GCU_PHONOPY_FASTPATH"
    stable = stability.get("verdict") == "PASS_STABLE" and stability.get("passed") is True
    hall_has_entry = bool(hall)

    if not decode_ok:
        missing.append("PASS_SYNTHESIS_RECIPE decode/FPU acceptance")
    if not gcu_ok:
        missing.append("PASS_GCU_PRESCREEN chip_a/chip_b + ROM + four-core evidence")
    if not fast_ok:
        missing.append("PASS_GCU_PHONOPY_FASTPATH FORCE_CONSTANTS_GCU_PROXY evidence")
    if not stable:
        missing.append("PASS_STABLE Phonopy band.yaml verdict")
    if stable and not hall_has_entry:
        missing.append("hall_of_fame.json graduation entry")

    if stable and hall_has_entry:
        tier = "T3_SYNTHESIZED_STABLE"
    elif decode_ok and gcu_ok and fast_ok:
        tier = "T2_CANDIDATE_GCU_FASTPATH_QE_AUDIT"
    elif decode_ok and gcu_ok:
        tier = "T1_CANDIDATE_GCU_PRESCREEN"
    else:
        tier = "T0_INCOMPLETE"

    if "PASS_STABLE Phonopy band.yaml verdict" in missing:
        progress = docker.get("containers") or []
        done = max((int(r.get("job_done") or 0) for r in progress), default=0)
        next_actions.append(f"keep QE/Phonopy final audit running ({done}/9 displacement SCFs done)")
    if fast_ok and not stable:
        next_actions.append("calibrate min_proxy_cm1 against final Phonopy min frequency when band.yaml lands")
    if fast.get("spotcheck_displacements", 0):
        next_actions.append("run 1-2 pw.x spot-check displacements before full Docker audit")
    if stable and not hall_has_entry:
        next_actions.append("append hall_of_fame + README synthesized table")
    if not next_actions:
        next_actions.append("archive evidence and keep hunting for next candidate")

    return tier, missing, next_actions


def build_ledger(run_id: str) -> dict[str, Any]:
    decode = _read_json(PASS_V1 / "artifacts" / "oracle" / f"inverse_decode_{run_id}.json")
    gcu = _read_json(PASS_V1 / "artifacts" / "oracle" / f"gcu_prescreen_{run_id}.json")
    fast = _read_json(
        PASS_V1
        / "artifacts"
        / "oracle"
        / "gcu_phonopy_fastpath"
        / run_id
        / "gcu_phonopy_fastpath.json"
    )
    stability = _read_json(PASS_V1 / "artifacts" / "oracle" / f"stability_verdict_{run_id}.json")
    hall_raw = _read_json(ROOT / "artifacts" / "crystal_forge" / "hall_of_fame.json")
    docker = _docker_phonopy_progress()

    decode_s = _decode_summary(decode if isinstance(decode, dict) else None)
    gcu_d = gcu if isinstance(gcu, dict) else {}
    fast_d = fast if isinstance(fast, dict) else {}
    stability_d = stability if isinstance(stability, dict) else {}
    hall = hall_raw if isinstance(hall_raw, list) else []
    tier, missing, next_actions = classify_evidence(
        decode=decode_s,
        gcu=gcu_d,
        fast=fast_d,
        stability=stability_d,
        hall=hall,
        docker=docker,
    )

    return {
        "schema": "foliation.crystal_evidence_ledger.v1",
        "ts": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "status_tier": tier,
        "is_toy": tier == "T0_INCOMPLETE",
        "can_claim_new_crystal": tier == "T3_SYNTHESIZED_STABLE",
        "candidate_formula": decode_s.get("formula"),
        "evidence": {
            "decode_fpu": decode_s,
            "gcu_prescreen": {
                "present": bool(gcu_d),
                "verdict": gcu_d.get("verdict"),
                "passed": gcu_d.get("passed"),
                "mapped_hops": (gcu_d.get("rom_burn") or {}).get("mapped_hops"),
                "lambda_max": (gcu_d.get("spectral_proxy") or {}).get("lambda_max"),
                "delta_alpha": (gcu_d.get("spectral_proxy") or {}).get("delta_alpha_log2"),
            },
            "gcu_fastpath": {
                "present": bool(fast_d),
                "verdict": fast_d.get("verdict"),
                "used_hops": fast_d.get("used_hops"),
                "fc2_norm": fast_d.get("fc2_norm"),
                "min_proxy_cm1": fast_d.get("min_proxy_cm1"),
                "force_constants_proxy": fast_d.get("force_constants_proxy"),
            },
            "phonopy_final": {
                "present": bool(stability_d),
                "verdict": stability_d.get("verdict"),
                "passed": stability_d.get("passed"),
                "min_frequency_cm-1": (
                    (stability_d.get("band_analysis") or {}).get("min_frequency_cm-1")
                    or stability_d.get("min_frequency_cm-1")
                ),
                "band_error": (stability_d.get("band_analysis") or {}).get("error"),
            },
            "docker_progress": docker,
            "hall_entries": hall,
            "forge_log_tail": _tail(ROOT / "out" / "chip" / "crystal_forge" / "forge.log", 16),
        },
        "missing_data": missing,
        "next_actions": next_actions,
        "evidence_paths": {
            "decode": str(PASS_V1 / "artifacts" / "oracle" / f"inverse_decode_{run_id}.json"),
            "gcu_prescreen": str(PASS_V1 / "artifacts" / "oracle" / f"gcu_prescreen_{run_id}.json"),
            "gcu_fastpath": str(
                PASS_V1
                / "artifacts"
                / "oracle"
                / "gcu_phonopy_fastpath"
                / run_id
                / "gcu_phonopy_fastpath.json"
            ),
            "force_constants_proxy": str(
                PASS_V1
                / "artifacts"
                / "oracle"
                / "gcu_phonopy_fastpath"
                / run_id
                / "FORCE_CONSTANTS_GCU_PROXY"
            ),
            "stability_verdict": str(
                PASS_V1 / "artifacts" / "oracle" / f"stability_verdict_{run_id}.json"
            ),
            "forge_log": str(ROOT / "out" / "chip" / "crystal_forge" / "forge.log"),
        },
    }


def write_markdown(ledger: dict[str, Any], path: Path) -> None:
    evidence = ledger["evidence"]
    lines = [
        f"# Crystal Evidence Ledger — {ledger['run_id']}",
        "",
        f"- Status tier: `{ledger['status_tier']}`",
        f"- Candidate formula: `{ledger.get('candidate_formula') or 'unknown'}`",
        f"- Can claim new crystal: `{ledger['can_claim_new_crystal']}`",
        f"- Is toy: `{ledger['is_toy']}`",
        "",
        "## Evidence",
        "",
        f"- Decode/FPU: `{evidence['decode_fpu'].get('verdict')}` J_BKT=`{evidence['decode_fpu'].get('j_bkt')}`",
        f"- GCU prescreen: `{evidence['gcu_prescreen'].get('verdict')}` mapped_hops=`{evidence['gcu_prescreen'].get('mapped_hops')}`",
        f"- GCU fastpath: `{evidence['gcu_fastpath'].get('verdict')}` min_proxy_cm1=`{evidence['gcu_fastpath'].get('min_proxy_cm1')}`",
        f"- Phonopy final: `{evidence['phonopy_final'].get('verdict')}` passed=`{evidence['phonopy_final'].get('passed')}`",
        "",
        "## Missing Data",
        "",
    ]
    lines.extend(f"- {item}" for item in ledger["missing_data"])
    lines.extend(["", "## Next Actions", ""])
    lines.extend(f"- {item}" for item in ledger["next_actions"])
    lines.extend(["", "## Evidence Paths", ""])
    lines.extend(f"- `{k}`: `{v}`" for k, v in ledger["evidence_paths"].items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build falsifiable crystal evidence ledger")
    ap.add_argument("--run-id", default="killer_20260615_122305")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    body = build_ledger(args.run_id)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / f"evidence_ledger_{args.run_id}.json"
    md_path = args.out_dir / f"evidence_ledger_{args.run_id}.md"
    json_path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_markdown(body, md_path)

    print(json.dumps({
        "run_id": body["run_id"],
        "status_tier": body["status_tier"],
        "can_claim_new_crystal": body["can_claim_new_crystal"],
        "is_toy": body["is_toy"],
        "missing_data": body["missing_data"],
        "next_actions": body["next_actions"],
        "json": str(json_path),
        "markdown": str(md_path),
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
