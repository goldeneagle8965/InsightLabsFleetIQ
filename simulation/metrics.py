"""Measured metrics. Numbers come from a run, never from a hardcoded string."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


METRIC_KEYS = (
    "collision_count",
    "deadlock_count",
    "stall_count",
    "stall_handoff_count",
    "all_tasks_complete_tick",
    "mean_wait_ticks",
    "total_distance",
    "mean_comm_latency_ticks",
    "mean_robot_step_ms",
    "tasks_done",
    "tasks_total",
    "complete",
)


@dataclass
class RunMetrics:
    collision_count: int = 0
    deadlock_count: int = 0
    # Fleet-wide stall episodes: zero task completions and zero robot movement
    # for STALL_WINDOW ticks while work is still outstanding. Measured for both
    # policies; before this field, UBPA liveness was simply unobserved.
    stall_count: int = 0
    # Tasks a holder gave back after repeatedly stalling on them (RC5). This is
    # the count that separates "the fleet retried the work elsewhere" from
    # "the work was abandoned".
    stall_handoff_count: int = 0
    all_tasks_complete_tick: int | None = None
    mean_wait_ticks: float = 0.0
    total_distance: float = 0.0
    mean_comm_latency_ticks: float = 0.0
    mean_robot_step_ms: float = 0.0
    tasks_done: int = 0
    tasks_total: int = 0
    complete: bool = False
    ticks: int = 0
    policy: str = ""
    seed: int = 0
    experiment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def improvement(baseline: RunMetrics, ours: RunMetrics, key: str) -> float | None:
    """(baseline - ours) / baseline. None if incomparable or baseline is 0."""
    if not baseline.complete or not ours.complete:
        return None
    b = getattr(baseline, key)
    o = getattr(ours, key)
    if b is None or o is None:
        return None
    try:
        bf = float(b)
        of = float(o)
    except (TypeError, ValueError):
        return None
    if bf == 0:
        return None
    return (bf - of) / bf


def summarize_pair(baseline: RunMetrics, ours: RunMetrics) -> dict[str, Any]:
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
    )
    out: dict[str, Any] = {
        "baseline": baseline.to_dict(),
        "ubpa": ours.to_dict(),
        "improvement": {},
        "comparable": bool(baseline.complete and ours.complete),
    }
    for k in keys:
        out["improvement"][k] = improvement(baseline, ours, k)
    return out


@dataclass
class Accumulator:
    wait_ticks: int = 0
    move_ticks: int = 0
    distance: float = 0.0
    step_ms_sum: float = 0.0
    step_n: int = 0
    deadlock_ids: set[tuple[str, str, int]] = field(default_factory=set)

    def note_step(self, moved: bool, dist: float, step_ms: float) -> None:
        if moved:
            self.move_ticks += 1
            self.distance += dist
        else:
            self.wait_ticks += 1
        self.step_ms_sum += step_ms
        self.step_n += 1

    def mean_wait(self) -> float:
        n = self.wait_ticks + self.move_ticks
        if n == 0:
            return 0.0
        return self.wait_ticks / n

    def mean_step_ms(self) -> float:
        if self.step_n == 0:
            return 0.0
        return self.step_ms_sum / self.step_n
