"""Measure UBPA under a degraded radio. Writes results/degradation.json.

The live demo lets an operator drag packet loss and radio delay. This script
measures what those sliders actually do, on the same E1 fleet configuration
used by run_experiments.py, so the degradation claims in the deck and README
are regenerated rather than typed.

Usage:
    python scripts/run_degradation.py
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.config import MAX_TICKS_EXPERIMENT  # noqa: E402
from simulation.engine import FleetSim  # noqa: E402

RESULTS = ROOT / "results"
SEEDS = (1, 2, 3, 4, 5)

# (label, drop_prob, radio_delay) - "clean" is the E1 control point.
CONFIGS = (
    ("clean", 0.0, 1),
    ("loss40", 0.4, 1),
    ("delay3", 0.0, 3),
    ("loss40_delay3", 0.4, 3),
    ("loss70", 0.7, 1),
)


def run_one(drop_prob: float, radio_delay: int, seed: int) -> dict:
    sim = FleetSim(
        policy="ubpa",
        seed=seed,
        max_ticks=MAX_TICKS_EXPERIMENT,
        drop_prob=drop_prob,
        radio_delay=radio_delay,
    )
    m = sim.run_until(MAX_TICKS_EXPERIMENT)
    row = m.to_dict()
    row["seed"] = seed
    row["packet_loss"] = drop_prob
    row["radio_delay"] = radio_delay
    row["packets_dropped"] = sim.bus.dropped_count
    return row


def summarize(rows: list[dict]) -> dict:
    def avg(key: str) -> float:
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        return round(statistics.fmean(vals), 3) if vals else 0.0

    ticks = [r["all_tasks_complete_tick"] for r in rows if r["all_tasks_complete_tick"] is not None]
    return {
        "runs": len(rows),
        "all_complete": all(r["complete"] for r in rows),
        "tasks_done_min": min(r["tasks_done"] for r in rows),
        "tasks_done_max": max(r["tasks_done"] for r in rows),
        "tasks_total": rows[0]["tasks_total"],
        "complete_tick_min": min(ticks) if ticks else None,
        "complete_tick_max": max(ticks) if ticks else None,
        "complete_tick_mean": int(round(statistics.fmean(ticks))) if ticks else None,
        "deadlock_count_mean": avg("deadlock_count"),
        "stall_count_mean": avg("stall_count"),
        "stall_handoff_count_mean": avg("stall_handoff_count"),
        "collision_count_mean": avg("collision_count"),
        "packets_dropped_mean": avg("packets_dropped"),
        "mean_comm_latency_ticks_mean": avg("mean_comm_latency_ticks"),
        "mean_robot_step_ms_mean": avg("mean_robot_step_ms"),
    }


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    experiments: dict[str, dict] = {}
    for label, drop, delay in CONFIGS:
        print(f"{label}: packet_loss={drop} radio_delay={delay} ...")
        rows = [run_one(drop, delay, s) for s in SEEDS]
        experiments[label] = {
            "packet_loss": drop,
            "radio_delay": delay,
            "seeds": list(SEEDS),
            "runs": rows,
            "summary": summarize(rows),
        }
    payload = {
        "tick_limit": MAX_TICKS_EXPERIMENT,
        "policy": "ubpa",
        "fleet": "E1 default (3 robots, 6 tasks)",
        "experiments": experiments,
        "note": (
            "collision_count counts resolved intention conflicts (swap or same-cell "
            "claims), not physical crashes. It is a contention meter, not a damage meter."
        ),
    }
    out = RESULTS / "degradation.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    for label, exp in experiments.items():
        s = exp["summary"]
        ticks = (
            f"{s['complete_tick_min']}-{s['complete_tick_max']}"
            if s["complete_tick_min"] is not None
            else "incomplete at tick limit"
        )
        print(
            f"  {label:<15} tasks {s['tasks_done_min']}-{s['tasks_done_max']}/{s['tasks_total']} "
            f"| finish {ticks} | deadlocks {s['deadlock_count_mean']} "
            f"| stalls {s['stall_count_mean']} | handoffs {s['stall_handoff_count_mean']} "
            f"| coll_events {s['collision_count_mean']} | dropped {s['packets_dropped_mean']}"
        )


if __name__ == "__main__":
    main()
