"""Measure UBPA against stop-and-wait. Writes results/metrics.json.

Pass ``--out <path>`` to write somewhere else. The dashboard's "re-measure on
this machine" button uses that so a fresh run can be compared against the
shipped numbers instead of overwriting the artifact the deck cites.
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
from simulation.metrics import RunMetrics, summarize_pair  # noqa: E402
from simulation.scenarios import blocked_aisle, head_on  # noqa: E402

RESULTS = ROOT / "results"
SEEDS = (1, 2, 3, 4, 5)


def run_one(policy: str, seed: int, experiment: str, **kwargs) -> RunMetrics:
    sim = FleetSim(
        policy=policy,
        seed=seed,
        max_ticks=MAX_TICKS_EXPERIMENT,
        **kwargs,
    )
    m = sim.run_until(MAX_TICKS_EXPERIMENT)
    m.experiment = experiment
    return m


def mean_run(rows: list[RunMetrics], policy: str, experiment: str) -> RunMetrics:
    def avg(key: str) -> float:
        vals = [getattr(r, key) for r in rows]
        nums = []
        for v in vals:
            if v is None:
                continue
            nums.append(float(v))
        if not nums:
            return 0.0
        return statistics.fmean(nums)

    complete = all(r.complete for r in rows)
    complete_tick = None
    ticks = [r.all_tasks_complete_tick for r in rows if r.all_tasks_complete_tick is not None]
    if ticks and complete:
        complete_tick = int(round(statistics.fmean(ticks)))
    return RunMetrics(
        collision_count=int(round(avg("collision_count"))),
        deadlock_count=int(round(avg("deadlock_count"))),
        stall_count=int(round(avg("stall_count"))),
        stall_handoff_count=int(round(avg("stall_handoff_count"))),
        all_tasks_complete_tick=complete_tick,
        mean_wait_ticks=avg("mean_wait_ticks"),
        total_distance=avg("total_distance"),
        mean_comm_latency_ticks=avg("mean_comm_latency_ticks"),
        mean_robot_step_ms=avg("mean_robot_step_ms"),
        tasks_done=int(round(avg("tasks_done"))),
        tasks_total=int(round(avg("tasks_total"))),
        complete=complete,
        ticks=int(round(avg("ticks"))),
        policy=policy,
        seed=-1,
        experiment=experiment,
    )


def pair_experiment(name: str, seeds: tuple[int, ...], **kwargs) -> dict:
    base_rows = [run_one("stop_wait", s, name, **kwargs) for s in seeds]
    ours_rows = [run_one("ubpa", s, name, **kwargs) for s in seeds]
    base = mean_run(base_rows, "stop_wait", name)
    ours = mean_run(ours_rows, "ubpa", name)
    summary = summarize_pair(base, ours)
    return {
        "seeds": list(seeds),
        "baseline_runs": [r.to_dict() for r in base_rows],
        "ubpa_runs": [r.to_dict() for r in ours_rows],
        "summary": summary,
    }


def _parse_out(argv: list[str]) -> Path:
    """Destination for the metrics payload; defaults to the shipped artifact."""
    out = RESULTS / "metrics.json"
    if "--out" in argv:
        index = argv.index("--out")
        if index + 1 >= len(argv):
            raise SystemExit("--out needs a path")
        out = Path(argv[index + 1]).expanduser()
        if not out.is_absolute():
            out = ROOT / out
    return out


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    out = _parse_out(argv)
    RESULTS.mkdir(parents=True, exist_ok=True)
    print("E1 throughput (3 robots, 6 tasks, 5 seeds)...")
    e1 = pair_experiment("E1", SEEDS)
    print("E2 head-on aisle...")
    e2 = pair_experiment("E2", (1, 2, 3), **head_on())
    print("E3 mid-run aisle block...")
    e3 = pair_experiment("E3", (1, 2, 3), **blocked_aisle())
    payload = {
        "tick_limit": MAX_TICKS_EXPERIMENT,
        "experiments": {"E1": e1, "E2": e2, "E3": e3},
        "note": "Improvement is (baseline-ours)/baseline only when both policies finished.",
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    _print_table(payload)


def _print_table(payload: dict) -> None:
    keys = (
        "collision_count",
        "deadlock_count",
        "stall_count",
        "stall_handoff_count",
        "all_tasks_complete_tick",
        "mean_wait_ticks",
        "total_distance",
        "mean_comm_latency_ticks",
        "mean_robot_step_ms",
        "complete",
    )
    for name, exp in payload["experiments"].items():
        summary = exp["summary"]
        b = summary["baseline"]
        u = summary["ubpa"]
        print(f"\n=== {name} comparable={summary['comparable']} ===")
        print(f"{'metric':28} {'stop-wait':>14} {'ubpa':>14} {'improv':>10}")
        for k in keys:
            bv = b.get(k)
            uv = u.get(k)
            imp = summary["improvement"].get(k)
            imp_s = "n/a" if imp is None else f"{imp * 100:.1f}%"
            print(f"{k:28} {str(bv):>14} {str(uv):>14} {imp_s:>10}")


if __name__ == "__main__":
    main()
