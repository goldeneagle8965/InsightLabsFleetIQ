"""Why does a configuration stall? End-state dump + progress curve.

Read-only diagnostic. Runs one configuration to the tick limit without
stopping early, then prints where each robot and task ended up, plus a
tasks-completed curve so a plateau (livelock / parked) is distinguishable
from slow-but-steady throughput.

Each kind is the shipped code with one configuration knob changed, so the
comparison is reproducible with an argument instead of an in-memory patch.

Usage:
    python scripts/probe_26123_stall_diag.py e1
    python scripts/probe_26123_stall_diag.py pre_delay3
    python scripts/probe_26123_stall_diag.py delay3
    python scripts/probe_26123_stall_diag.py delay3_no_rc2
    python scripts/probe_26123_stall_diag.py loss70
    python scripts/probe_26123_stall_diag.py loss70_no_rc1
    python scripts/probe_26123_stall_diag.py scale6
    python scripts/probe_26123_stall_diag.py scale10
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from simulation.config import MAX_TICKS_EXPERIMENT, CoordinationFlags  # noqa: E402
from simulation.engine import FleetSim  # noqa: E402
import probe_26123_scale_delay as probe  # noqa: E402

SEED = 1

# What the code did before the four fixes: every coordination repair disabled.
PREFIX_FLAGS = CoordinationFlags(
    stall_detector=False,
    task_reannounce=False,
    age_corrected_peers=False,
    claim_arbitration=False,
    claim_expiry=False,
    stall_handoff=False,
)


def flags_for(kind: str) -> CoordinationFlags:
    if kind.startswith("pre"):
        return PREFIX_FLAGS
    if kind.endswith("_no_rc1"):
        return CoordinationFlags(task_reannounce=False)
    if kind.endswith("_no_rc2"):
        return CoordinationFlags(age_corrected_peers=False)
    if kind.endswith("_no_rc3"):
        return CoordinationFlags(stall_detector=False)
    if kind.endswith("_no_rc4"):
        return CoordinationFlags(claim_arbitration=False, claim_expiry=False)
    if kind.endswith("_no_rc5"):
        return CoordinationFlags(stall_handoff=False)
    return CoordinationFlags()


def build(kind: str) -> FleetSim:
    base: dict = {"policy": "ubpa", "seed": SEED, "max_ticks": MAX_TICKS_EXPERIMENT}
    flags = flags_for(kind)
    core = kind.replace("pre_", "").replace("_no_rc1", "").replace("_no_rc2", "")
    core = core.replace("_no_rc3", "").replace("_no_rc4", "").replace("_no_rc5", "")
    if core == "e1":
        return FleetSim(**base, flags=flags)
    if core == "delay3":
        return FleetSim(**base, radio_delay=3, flags=flags)
    if core == "loss70":
        return FleetSim(**base, drop_prob=0.7, flags=flags)
    if core.startswith("scale"):
        n = int(core.replace("scale", ""))
        return FleetSim(
            **base,
            n_robots=n,
            tasks=probe.scale_tasks(n),
            spawn=list(probe.SPAWNS),
            flags=flags,
        )
    raise SystemExit(f"unknown config {kind!r}")


def main() -> None:
    kind = sys.argv[1] if len(sys.argv) > 1 else "e1"
    sim = build(kind)

    curve: list[tuple[int, int]] = []

    def on_tick(s: FleetSim) -> None:
        if s.tick % 25 == 0:
            done = sum(1 for t in s.world.tasks.values() if t.status == "DONE")
            curve.append((s.tick, done))

    sim.run_until(MAX_TICKS_EXPERIMENT, on_tick=on_tick)

    m = sim.metrics()
    f = sim.flags
    print(f"=== {kind} (seed {SEED}, limit {MAX_TICKS_EXPERIMENT}) ===")
    print(
        "flags: "
        + ", ".join(
            f"{name.replace('_', '-')}={'on' if getattr(f, name) else 'off'}"
            for name in (
                "stall_detector",
                "task_reannounce",
                "age_corrected_peers",
                "claim_arbitration",
                "claim_expiry",
                "stall_handoff",
            )
        )
    )
    print(
        f"ticks {m.ticks} | tasks {m.tasks_done}/{m.tasks_total} | complete {m.complete} "
        f"| deadlocks {m.deadlock_count} | stalls {m.stall_count} "
        f"| handoffs {m.stall_handoff_count} "
        f"| conflict events {m.collision_count} "
        f"| mean wait {m.mean_wait_ticks:.1f} | dropped {sim.bus.dropped_count}"
    )

    print("\n-- tasks-completed curve (every 25 ticks) --")
    print("  " + "  ".join(f"{t}:{d}" for t, d in curve))

    print("\n-- task end state --")
    for t in sim.world.tasks.values():
        print(
            f"  {t.task_id:<4} {t.status:<9} holder={str(t.holder):<5} "
            f"pick={t.pick} drop={t.drop} done_tick={t.complete_tick}"
        )

    print("\n-- robot end state --")
    for bot in sim.robots:
        print(
            f"  {bot.id:<4} pos={str(bot.pos):<8} phase={bot.phase:<9} "
            f"task={str(bot.current_task):<5} goal={str(bot.goal):<8} "
            f"stuck={bot.stuck_ticks:<4} stalled={bot.stalled_ticks:<4} "
            f"deadlocks={bot.deadlock_events:<3} recoveries={bot.stall_recoveries:<3} "
            f"handoffs={bot.stall_handoffs:<3} "
            f"unreach={bot.unreachable_ticks:<3} "
            f"yield_left={bot.yield_wait_left:<3} wait={bot.wait_ticks:<5} "
            f"move={bot.move_ticks:<5} done={bot.tasks_done:<3} "
            f"path_len={len(bot.planned_path):<3} batt={bot.battery:.0f}"
        )

    print("\n-- claim bookkeeping --")
    for bot in sim.robots:
        print(
            f"  {bot.id:<4} conflicts={bot.claim_conflicts:<3} lost={bot.claim_reasserts:<3} "
            f"stale={bot.claim_stale:<3} silent={bot.claim_silent:<3} "
            f"holding={str(bot.current_task):<5} cost={bot.claim_cost:.1f}"
        )

    print("\n-- who knows each unfinished task --")
    unfinished = [t for t in sim.world.tasks.values() if t.status != "DONE"]
    if not unfinished:
        print("  (none -- run finished)")
    for t in unfinished:
        seen = [
            f"{bot.id}:{bot.known_tasks[t.task_id].status}/{bot.known_tasks[t.task_id].holder}"
            for bot in sim.robots
            if t.task_id in bot.known_tasks
        ]
        blind = [bot.id for bot in sim.robots if t.task_id not in bot.known_tasks]
        print(
            f"  {t.task_id:<4} world={t.status:<8} known_by=[{', '.join(seen) or '-'}] "
            f"never_heard=[{', '.join(blind) or '-'}] open_auctions="
            f"{sum(1 for b in sim.robots if t.task_id in b.auctions and not b.auctions[t.task_id].closed)}"
        )

    kinds = collections.Counter(e["kind"] for e in sim.events)
    print("\n-- event kinds --")
    print("  " + ", ".join(f"{k}={v}" for k, v in kinds.most_common()))

    print("\n-- last 15 events --")
    for e in sim.events[-15:]:
        print(f"  t={e.get('tick'):<5} {e.get('robot'):<5} {e.get('kind'):<14} {e.get('detail')}")


if __name__ == "__main__":
    main()
