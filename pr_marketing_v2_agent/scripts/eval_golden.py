"""Score the catalog updater's understanding pipeline on golden cases (real LLM calls).

    uv run --no-sync python scripts/eval_golden.py                 # tests/golden/cases.json
    uv run --no-sync python scripts/eval_golden.py --case canvas-export-two-loops -v

Each case runs route -> extract -> verify -> compose -> check against a temporary copy of the
seeded catalog (nothing real is written) and is compared with hand-written expectations:

- operation precision: proposed (op, target[, field]) that a human expected (or allowed)
- operation recall:    expected operations that were proposed
- false writes:        proposals containing a forbidden phrase (claims the evidence lacks)
- routing:             product_change decided correctly

Go live with tier A only once precision >= 0.95 and false writes == 0 (see the design).
Needs AI_GATEWAY_URL / BIFROST_VIRTUAL_KEY like a normal run.
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
os.environ["LOCAL_OUTPUT"] = "true"

from tools import understand  # noqa: E402
from utils import catalog, events, storage  # noqa: E402

CASES = ROOT / "tests" / "golden" / "cases.json"
SEED = ROOT / "seed" / "catalog_seed.json"


def _fn(tool: Any) -> Any:
    return getattr(tool, "fn", tool)


def _key(op: dict[str, Any] | list[str]) -> tuple[str, ...]:
    if isinstance(op, list):
        return tuple(op)
    parts = [op["op"], op.get("target") or ""]
    if op["op"] == "edit_field":
        parts.append(op.get("field") or "")
    return tuple(parts)


async def run_case(case: dict[str, Any]) -> dict[str, Any]:
    key = events.save_event(case["event"])
    route = await _fn(understand.route_event_tool)(key)
    out: dict[str, Any] = {"route": route, "proposals": [], "dropped": []}
    if route["product_change"]:
        ex = await _fn(understand.extract_claims_tool)(key, route)
        ve = await _fn(understand.verify_claims_tool)(key, ex["claims"])
        out["dropped"] += ex["dropped"] + ve["dropped"]
        if ve["claims"]:
            co = await _fn(understand.compose_ops_tool)(key, route, ve["claims"])
            ch = await _fn(understand.check_ops_tool)(key, co["ops"], ve["claims"])
            out["proposals"], out["dropped"] = ch["proposals"], out["dropped"] + ch["dropped"]
    return out


def score(case: dict[str, Any], got: dict[str, Any]) -> dict[str, Any]:
    exp = case["expect"]
    expected = {_key(o) for o in exp.get("ops", [])}
    allowed = expected | {_key(o) for o in exp.get("optional_ops", [])}
    proposed = [_key(p["op"]) for p in got["proposals"]]
    texts = [json.dumps(p["op"], ensure_ascii=False).lower() for p in got["proposals"]]
    false_writes = [
        phrase for phrase in exp.get("forbidden", []) for t in texts if phrase.lower() in t
    ]
    return {
        "name": case["name"],
        "route_ok": got["route"]["product_change"] == exp["product_change"],
        "proposed": len(proposed),
        "correct": sum(k in allowed for k in proposed),
        "expected": len(expected),
        "found": len(expected & set(proposed)),
        "unexpected": [k for k in proposed if k not in allowed],
        "missed": sorted(expected - set(proposed)),
        "false_writes": false_writes,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--case", help="run only the case with this name")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    cases = json.loads(args.cases.read_text())["cases"]
    if args.case:
        cases = [c for c in cases if c["name"] == args.case]
    with tempfile.TemporaryDirectory() as tmp:
        storage._LOCAL_DIR = Path(tmp)  # a throwaway catalog, never the real one
        catalog.apply_seed(json.loads(SEED.read_text()))
        rows = []
        for case in cases:
            got = await run_case(case)
            row = score(case, got)
            rows.append(row)
            flag = (
                "ok " if row["route_ok"] and not row["unexpected"] and not row["missed"] else "!! "
            )
            print(
                f"{flag}{row['name']}: {row['correct']}/{row['proposed']} correct, "
                f"{row['found']}/{row['expected']} found"
            )
            if row["unexpected"] or row["missed"] or row["false_writes"] or not row["route_ok"]:
                print(
                    f"     unexpected={row['unexpected']} missed={row['missed']} "
                    f"false_writes={row['false_writes']} route_ok={row['route_ok']}"
                )
            if args.verbose:
                for p in got["proposals"]:
                    print(
                        f"     [{p['tier']}] {p['op']['op']} {p['op']['target']}: "
                        f"{p['preview']['after'][:160]}"
                    )
                for d in got["dropped"]:
                    print(f"     dropped ({d['stage']}): {d['item'][:80]} — {d['reason']}")

    proposed = sum(r["proposed"] for r in rows)
    expected = sum(r["expected"] for r in rows)
    precision = sum(r["correct"] for r in rows) / proposed if proposed else 1.0
    recall = sum(r["found"] for r in rows) / expected if expected else 1.0
    false_writes = sum(len(r["false_writes"]) for r in rows)
    routing = sum(r["route_ok"] for r in rows) / len(rows) if rows else 1.0
    print(
        f"\nprecision {precision:.2f}  recall {recall:.2f}  routing {routing:.2f}  "
        f"false writes {false_writes}  ({len(rows)} cases)"
    )
    ready = precision >= 0.95 and false_writes == 0
    print("Tier A can go live." if ready else "Not ready for live tier A yet.")
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
