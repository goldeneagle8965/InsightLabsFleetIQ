"""Which fix is load-bearing in which scenario? One flag off at a time.

Read-only attribution probe. Nothing here is a new behaviour: every row is the
shipped code with one CoordinationFlags switch moved, so a reviewer can flip the
same switch by hand and get the same number.

Rows per scenario:
    shipped   every fix on (what the decks quote)
    all_off   every fix off (what the code did before RC1-RC5)
    no_rc1..5 shipped minus exactly one fix

Columns: runs that finished, task range, mean finish tick, conflict events,
deadlocks, stall episodes, stall hand-backs. A scenario where removing one flag
drops finished runs to 0/5/blocks the tick limit is a scenario that flag is
load-bearing for; a scenario where every removal still finishes 5/5 means those
fixes are redundancy there, which is worth saying out loud rather than implying
every fix rescues every case.

Usage:
    python scripts/probe_26123_ablation.py
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from simulation.config import MAX_TICKS_EXPERIMENT, CoordinationFlags  # noqa: E402
from simulation.engine import FleetSim  # noqa: E402
from simulation.metrics import RunMetrics  # noqa: E402
import probe_26123_scale_delay as scale_probe  # noqa: E402

RESULTS = ROOT / "results"
SEEDS = (1, 2, 3, 4, 5)

ALL_OFF = CoordinationFlags(
    stall_detector=False,
    task_reannounce=False,
    age_corrected_peers=False,
    claim_arbitration=False,
    claim_expiry=False,
    stall_handoff=False,
)

VARIANTS: dict[str, CoordinationFlags] = {
    "shipped": CoordinationFlags(),
    "all_off": ALL_OFF,
    "no_rc1_reannounce": CoordinationFlags(task_reannounce=False),
    "no_rc2_age_corrected": CoordinationFlags(age_corrected_peers=False),
    "no_rc3_stall_detector": CoordinationFlags(stall_detector=False),
    "no_rc4_claims": CoordinationFlags(claim_arbitration=False, claim_expiry=False),
    "no_rc5_stall_handoff": CoordinationFlags(stall_handoff=False),
}


def scenario_kwargs(name: str) -> dict[str, Any]:
    if name == "e1_clean":
        return {}
    if name == "delay3":
        return {"radio_delay": 3}
    if name == "loss70":
        return {"drop_prob": 0.7}
    if name == "scale8":
        return {
            "n_robots": 8,
            "tasks": scale_probe.scale_tasks(8),
            "spawn": list(scale_probe.SPAWNS),
        }
    raise SystemExit(f"unknown scenario {name!r}")


def run_one(scenario: str, flags: CoordinationFlags, seed: int) -> tuple[RunMetrics, int]:
    sim = FleetSim(
        policy="ubpa",
        seed=seed,
        max_ticks=MAX_TICKS_EXPERIMENT,
        flags=flags,
        **scenario_kwargs(scenario),
    )
    metrics = sim.run_until(MAX_TICKS_EXPERIMENT)
    # Dropped-packet count is only meaningful next to the tick the run reached:
    # a run that dies early broadcasts less than one that keeps working.
    return metrics, sim.bus.dropped_count


def summarize(rows: list[RunMetrics], dropped: list[int]) -> dict[str, Any]:
    ticks = [r.all_tasks_complete_tick for r in rows if r.all_tasks_complete_tick is not None]

    def mean_of(key: str) -> float:
        return round(statistics.fmean(float(getattr(r, key)) for r in rows), 2)

    return {
        "runs": len(rows),
        "finished_runs": sum(1 for r in rows if r.complete),
        "tasks_done_min": min(r.tasks_done for r in rows),
        "tasks_done_max": max(r.tasks_done for r in rows),
        "tasks_total": rows[0].tasks_total,
        "complete_tick_min": min(ticks) if ticks else None,
        "complete_tick_max": max(ticks) if ticks else None,
        "complete_tick_mean": int(round(statistics.fmean(ticks))) if ticks else None,
        "collision_count_mean": mean_of("collision_count"),
        "deadlock_count_mean": mean_of("deadlock_count"),
        "stall_count_mean": mean_of("stall_count"),
        "stall_handoff_count_mean": mean_of("stall_handoff_count"),
        "mean_wait_ticks_mean": mean_of("mean_wait_ticks"),
        "packets_dropped_mean": round(statistics.fmean(dropped), 1),
        "ticks_mean": mean_of("ticks"),
    }


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "tick_limit": MAX_TICKS_EXPERIMENT,
        "seeds": list(SEEDS),
        "policy": "ubpa",
        "note": (
            "Each row is the shipped code with exactly one CoordinationFlags switch moved. "
            "read-only attribution; no monkeypatching."
        ),
        "scenarios": {},
    }
    for scenario in ("e1_clean", "delay3", "loss70", "scale8"):
        print(f"\n=== {scenario} (UBPA, seeds {list(SEEDS)}) ===")
        print(
            f"{'variant':22} {'finished':>9} {'tasks':>7} {'finish':>8} {'ticks':>7} "
            f"{'conflicts':>9} {'deadlocks':>9} {'stalls':>7} {'handoffs':>8} {'dropped':>9}"
        )
        entry: dict[str, Any] = {}
        for variant, flags in VARIANTS.items():
            t0 = time.time()
            pairs = [run_one(scenario, flags, s) for s in SEEDS]
            rows = [m for m, _ in pairs]
            s = summarize(rows, [d for _, d in pairs])
            entry[variant] = {"summary": s, "wall_seconds": round(time.time() - t0, 1)}
            finish = s["complete_tick_mean"] if s["complete_tick_mean"] is not None else "-"
            print(
                f"{variant:22} {s['finished_runs']}/{s['runs']:>7} "
                f"{s['tasks_done_min']}-{s['tasks_done_max']}/{s['tasks_total']:<3} "
                f"{str(finish):>8} {s['ticks_mean']:>7.0f} {s['collision_count_mean']:>9} "
                f"{s['deadlock_count_mean']:>9} {s['stall_count_mean']:>7} "
                f"{s['stall_handoff_count_mean']:>8} {s['packets_dropped_mean']:>9}",
                flush=True,
            )
        payload["scenarios"][scenario] = entry

    out = RESULTS / "probe_26123_ablation.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
