"""Ad-hoc probes for two judge-facing gaps. Not part of the deck pipeline.

A) Fleet-size sweep
   Everything published so far is 3 robots / 6 tasks. This asks the obvious
   follow-up question: does UBPA still finish when the fleet grows?

   Scale points use 2 jobs per robot and distinct spawn cells, because
   spawn_positions() only defines 4 cells -- reusing it would stack robots on
   top of each other and measure spawn collisions instead of coordination.

   n=3 is included as the E1 control (default_tasks + spawn_positions) so the
   row can be checked against the published 93-tick result.

   scale_8robots_16tasks used to livelock (14/16 tasks, no run ever finished).
   That is what RC5 (stall hand-back with cooldown) fixes; the summary now also
   carries stall_handoff_count_mean so the hand-backs are visible, not inferred.

B) Radio-delay root cause
   delay3 (3-tick radio delay) used to starve E1 to 3/6 tasks with ~577
   conflict events. Cause: reservations_from_peers() projected a peer's
   planned path forward starting at `now`, ignoring how old the message was.
   At delay 1 that error is one tick; at delay 3 the receiver reserved the
   wrong three ticks of floor, so it planned into cells the peer already
   occupied.

   The probe runs delay3 twice through the shipped code path: with the RC2
   fix on, and with the same code and RC2 switched off via CoordinationFlags.
   No monkeypatching -- the ablation switch is what a reviewer would flip.

Usage:
    python scripts/probe_26123_scale_delay.py
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

from simulation.config import MAX_TICKS_EXPERIMENT, CoordinationFlags  # noqa: E402
from simulation.engine import FleetSim  # noqa: E402
from simulation.world import Task, default_tasks, spawn_positions  # noqa: E402

RESULTS = ROOT / "results"
SEEDS = (1, 2, 3, 4, 5)

# Aisle rows (y even, y=1 and y=9 highways) -- all verified free by
# hyderabad_layout(): racks sit on y=3/5/7 except at the gap columns.
CANDIDATES = (
    (4, 2), (20, 2), (8, 4), (16, 4), (6, 8), (18, 8),
    (4, 6), (20, 6), (12, 2), (12, 8), (10, 4), (14, 4),
    (2, 4), (22, 4), (8, 2), (16, 8), (6, 4), (18, 4),
    (10, 6), (14, 6), (2, 6), (22, 6),
)
SPAWNS = (
    (2, 1), (6, 1), (12, 1), (18, 1), (22, 1),
    (2, 9), (6, 9), (12, 9), (18, 9), (22, 9),
)


# --------------------------------------------------------------------------
# A) fleet-size sweep
# --------------------------------------------------------------------------


def scale_tasks(n_robots: int) -> list[Task]:
    """Two jobs per robot, deterministic, spread over the aisle rows."""
    out: list[Task] = []
    for i in range(n_robots * 2):
        pick = CANDIDATES[i % len(CANDIDATES)]
        j = (i * 5 + 7) % len(CANDIDATES)
        if CANDIDATES[j] == pick:
            j = (j + 1) % len(CANDIDATES)
        drop = CANDIDATES[j]
        out.append(Task(f"T{i + 1}", pick=pick, drop=drop, urgency=round(0.9 - 0.02 * i, 3)))
    return out


def run_scale(policy: str, n_robots: int, seed: int, control: bool) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"n_robots": n_robots}
    if control:
        # E1 published configuration, so the n=3 row is directly comparable.
        kwargs["tasks"] = None
        kwargs["spawn"] = None
    else:
        kwargs["tasks"] = scale_tasks(n_robots)
        kwargs["spawn"] = list(SPAWNS)
    sim = FleetSim(policy=policy, seed=seed, max_ticks=MAX_TICKS_EXPERIMENT, **kwargs)
    row = sim.run_until(MAX_TICKS_EXPERIMENT).to_dict()
    row["seed"] = seed
    row["n_robots"] = n_robots
    row["policy"] = policy
    return row


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def avg(key: str) -> float:
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        return round(statistics.fmean(vals), 3) if vals else 0.0

    ticks = [r["all_tasks_complete_tick"] for r in rows if r["all_tasks_complete_tick"] is not None]
    return {
        "runs": len(rows),
        "finished_runs": sum(1 for r in rows if r["complete"]),
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
        "mean_wait_ticks_mean": avg("mean_wait_ticks"),
        "mean_robot_step_ms_mean": avg("mean_robot_step_ms"),
    }


# --------------------------------------------------------------------------
# B) radio-delay root cause
# --------------------------------------------------------------------------


def run_delay(seed: int, radio_delay: int, age_corrected: bool) -> dict[str, Any]:
    """Shipped code path, with only the RC2 ablation switch moved."""
    sim = FleetSim(
        policy="ubpa",
        seed=seed,
        max_ticks=MAX_TICKS_EXPERIMENT,
        radio_delay=radio_delay,
        flags=CoordinationFlags(age_corrected_peers=age_corrected),
    )
    row = sim.run_until(MAX_TICKS_EXPERIMENT).to_dict()
    row["seed"] = seed
    row["radio_delay"] = radio_delay
    row["age_corrected"] = age_corrected
    row["packets_dropped"] = sim.bus.dropped_count
    return row


# --------------------------------------------------------------------------


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "tick_limit": MAX_TICKS_EXPERIMENT,
        "seeds": list(SEEDS),
        "probe": "ad-hoc, not cited by the decks unless a human accepts it",
        "a_fleet_size": {},
        "b_radio_delay": {},
    }

    print("=== A) fleet-size sweep (UBPA, then stop-and-wait for contrast) ===")
    configs = (
        ("E1_control_3robots_6tasks", 3, True),
        ("scale_6robots_12tasks", 6, False),
        ("scale_8robots_16tasks", 8, False),
        ("scale_10robots_20tasks", 10, False),
    )
    for label, n_robots, control in configs:
        entry: dict[str, Any] = {"n_robots": n_robots, "policy": {}}
        for policy in ("ubpa", "stop_wait"):
            t0 = time.time()
            rows = [run_scale(policy, n_robots, s, control) for s in SEEDS]
            summary = summarize(rows)
            elapsed = round(time.time() - t0, 1)
            entry["policy"][policy] = {
                "runs": rows,
                "summary": summary,
                "wall_seconds": elapsed,
            }
            ticks = (
                f"{summary['complete_tick_min']}-{summary['complete_tick_max']} ticks"
                if summary["complete_tick_min"] is not None
                else "incomplete at tick limit"
            )
            print(
                f"  {label:<26} {policy:<10} "
                f"finished {summary['finished_runs']}/{summary['runs']} runs | "
                f"tasks {summary['tasks_done_min']}-{summary['tasks_done_max']}"
                f"/{summary['tasks_total']} | {ticks} | "
                f"deadlocks {summary['deadlock_count_mean']} | "
                f"handoffs {summary['stall_handoff_count_mean']} | "
                f"conflicts {summary['collision_count_mean']} | {elapsed}s",
                flush=True,
            )
        payload["a_fleet_size"][label] = entry

    print("\n=== B) radio delay: shipped RC2 vs RC2 switched off ===")
    delay_configs = (
        ("delay1_shipped", 1, True),
        ("delay1_rc2_off", 1, False),
        ("delay3_rc2_off", 3, False),
        ("delay3_shipped", 3, True),
    )
    for label, delay, aged in delay_configs:
        t0 = time.time()
        rows = [run_delay(s, delay, aged) for s in SEEDS]
        summary = summarize(rows)
        elapsed = round(time.time() - t0, 1)
        payload["b_radio_delay"][label] = {
            "radio_delay": delay,
            "age_corrected": aged,
            "runs": rows,
            "summary": summary,
            "wall_seconds": elapsed,
        }
        ticks = (
            f"{summary['complete_tick_min']}-{summary['complete_tick_max']} ticks"
            if summary["complete_tick_min"] is not None
            else "incomplete at tick limit"
        )
        print(
            f"  {label:<22} finished {summary['finished_runs']}/{summary['runs']} runs | "
            f"tasks {summary['tasks_done_min']}-{summary['tasks_done_max']}"
            f"/{summary['tasks_total']} | {ticks} | "
            f"deadlocks {summary['deadlock_count_mean']} | "
            f"conflicts {summary['collision_count_mean']} | {elapsed}s",
            flush=True,
        )

    out = RESULTS / "probe_26123_scale_delay.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
