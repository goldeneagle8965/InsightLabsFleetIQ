"""Tick loop. World is physics + radio. Robots decide everything else."""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from typing import Any, Callable

from simulation.config import (
    MAX_TICKS_DEMO,
    RADIO_DELAY,
    STALL_WINDOW,
    TASK_REANNOUNCE_TICKS,
    TICK_HZ,
    CoordinationFlags,
)
from simulation.metrics import RunMetrics
from simulation.peer import MSG_TASK_ANNOUNCE, PeerBus
from simulation.robot import Robot
from simulation.world import Task, Warehouse, default_tasks, hyderabad_layout, spawn_positions


Policy = str  # "ubpa" | "stop_wait"


@dataclass
class FleetSim:
    n_robots: int = 3
    policy: str = "ubpa"
    seed: int = 0
    drop_prob: float = 0.0
    radio_delay: int = RADIO_DELAY
    max_ticks: int = MAX_TICKS_DEMO
    tasks: list[Task] | None = None
    spawn: list[tuple[int, int]] | None = None
    announce_at: int = 4
    forced_block: tuple[int, int] | None = None
    forced_block_tick: int | None = None
    # Ablation switches (see CoordinationFlags). Defaults are the shipped fixes;
    # every one can be turned off and the same scenarios re-measured.
    flags: CoordinationFlags = field(default_factory=CoordinationFlags)
    world: Warehouse = field(init=False)
    bus: PeerBus = field(init=False)
    robots: list[Robot] = field(init=False)
    tick: int = 0
    paused: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    _announced: bool = False
    _rng: random.Random = field(init=False)
    _last_announce_tick: int = 0
    _last_progress_tick: int = 0
    _done_mark: int = 0
    _stall_marked: bool = False
    _stall_episodes: int = 0

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._rng = random.Random(self.seed)
        self.world = hyderabad_layout()
        self.bus = PeerBus(delay=self.radio_delay, drop_prob=self.drop_prob, rng=random.Random(self.seed + 17))
        self.tick = 0
        self.paused = False
        self.events = []
        self._announced = False
        self._last_announce_tick = 0
        self._last_progress_tick = 0
        self._done_mark = 0
        self._stall_marked = False
        self._stall_episodes = 0
        jobs = self.tasks if self.tasks is not None else default_tasks()
        self.world.tasks = {t.task_id: copy.copy(t) for t in jobs}
        spawns = self.spawn if self.spawn is not None else spawn_positions()
        self.robots = []
        for i in range(self.n_robots):
            rid = f"R{i + 1}"
            pos = spawns[i % len(spawns)]
            self.world.robots[rid] = pos
            bot = Robot(
                rid, pos, self.world, self.bus, priority=1, policy=self.policy, flags=self.flags
            )
            self.bus.register(rid)
            self.robots.append(bot)
        # WMS bulletin is not a robot; register a sender id so broadcasts work
        self.bus.register("WMS")

    def step(self) -> None:
        if self.paused:
            return
        self.tick += 1
        if self.forced_block is not None and self.forced_block_tick == self.tick:
            self.world.set_block(self.forced_block, True)
            self.events.append(
                {
                    "tick": self.tick,
                    "robot": "OP",
                    "kind": "block",
                    "detail": f"aisle cell {self.forced_block}",
                }
            )
        if not self._announced and self.tick >= self.announce_at:
            self._announce_tasks()
            self._announced = True
        else:
            self._reannounce_unheard()

        self.bus.tick(self.tick)
        intentions: dict[str, tuple[int, int]] = {}
        for bot in self.robots:
            intentions[bot.id] = bot.step(self.tick)
        self.world.apply_moves(intentions, self.tick)
        for bot in self.robots:
            bot.note_result(self.world.robots[bot.id])
            self.events.extend(bot.events)
            bot.events = []
        self._sync_task_board()
        self._watch_progress()

    def run_until(
        self,
        max_ticks: int | None = None,
        on_tick: Callable[["FleetSim"], None] | None = None,
    ) -> RunMetrics:
        limit = max_ticks if max_ticks is not None else self.max_ticks
        while self.tick < limit and not self.all_done():
            self.step()
            if on_tick:
                on_tick(self)
        return self.metrics()

    def all_done(self) -> bool:
        return all(t.status == "DONE" for t in self.world.tasks.values()) and bool(self.world.tasks)

    def toggle_block(self, x: int, y: int) -> bool:
        on = self.world.toggle_block((x, y))
        self.events.append(
            {
                "tick": self.tick,
                "robot": "OP",
                "kind": "block" if on else "unblock",
                "detail": f"{(x, y)}",
            }
        )
        return on

    def set_radio(self, drop_prob: float | None = None, delay: int | None = None) -> dict[str, Any]:
        """Live radio knobs. The bus is a medium, not a dispatcher."""
        if drop_prob is not None:
            self.drop_prob = max(0.0, min(0.9, float(drop_prob)))
            self.bus.drop_prob = self.drop_prob
        if delay is not None:
            self.radio_delay = max(0, min(8, int(delay)))
            self.bus.delay = self.radio_delay
        return {
            "drop_prob": self.drop_prob,
            "delay": self.radio_delay,
            "dropped": self.bus.dropped_count,
            "delivered": self.bus.delivered_count,
        }

    def block_aisle(self) -> tuple[int, int]:
        """Block a 1-wide aisle cell on y=4 that a robot is heading toward."""
        cell = (8, 4)
        for bot in self.robots:
            for p in bot.planned_path[1:8]:
                if p[1] in (2, 4, 6, 8) and p not in self.world.walls:
                    cell = p
                    break
        self.world.set_block(cell, True)
        self.events.append(
            {"tick": self.tick, "robot": "OP", "kind": "block", "detail": f"BLOCK AISLE {cell}"}
        )
        return cell

    def snapshot(self) -> dict[str, Any]:
        robots = []
        for bot in self.robots:
            robots.append(
                {
                    "id": bot.id,
                    "pos": list(bot.pos),
                    "battery": round(bot.battery, 2),
                    "status": bot.phase,
                    "task": bot.current_task,
                    "urgency": bot.task_urgency,
                    "priority": bot.priority,
                    "workload": bot.workload,
                    "path": [list(p) for p in bot.planned_path],
                    "ubpa": round(bot.last_ubpa, 3),
                    "velocity": list(bot.velocity),
                }
            )
        m = self.metrics()
        return {
            "tick": self.tick,
            "hz": TICK_HZ,
            "policy": self.policy,
            "paused": self.paused,
            "complete": self.all_done(),
            "world": self.world.snapshot(),
            "robots": robots,
            "radio": self.bus.snapshot_log(36),
            "events": self.events[-40:],
            "metrics": m.to_dict(),
            "radio_stats": {
                "drop_prob": self.drop_prob,
                "delay": self.radio_delay,
                "dropped": self.bus.dropped_count,
                "delivered": self.bus.delivered_count,
                "mean_latency": self.bus.mean_latency(),
            },
        }

    def metrics(self) -> RunMetrics:
        deadlocks = sum(b.deadlock_events for b in self.robots)
        waits = sum(b.wait_ticks for b in self.robots)
        moves = sum(b.move_ticks for b in self.robots)
        dist = sum(b.distance for b in self.robots)
        step_ms = 0.0
        n = sum(b.step_n for b in self.robots)
        if n:
            step_ms = sum(b.step_ms_sum for b in self.robots) / n
        done = sum(1 for t in self.world.tasks.values() if t.status == "DONE")
        complete = done == len(self.world.tasks) and bool(self.world.tasks)
        complete_tick = self.tick if complete else None
        mean_wait = (waits / (waits + moves)) if (waits + moves) else 0.0
        return RunMetrics(
            collision_count=self.world.collision_events,
            deadlock_count=deadlocks,
            stall_count=self._stall_episodes,
            stall_handoff_count=sum(b.stall_handoffs for b in self.robots),
            all_tasks_complete_tick=complete_tick,
            mean_wait_ticks=mean_wait,
            total_distance=dist,
            mean_comm_latency_ticks=self.bus.mean_latency(),
            mean_robot_step_ms=step_ms,
            tasks_done=done,
            tasks_total=len(self.world.tasks),
            complete=complete,
            ticks=self.tick,
            policy=self.policy,
            seed=self.seed,
        )

    def _reannounce_unheard(self) -> None:
        """WMS repeats a bulletin that at least one robot has never heard (RC1).

        One announcement plus heavy loss means every robot can miss the same
        task, and nothing ever repeats it: the auction then has nothing to bid
        on and the task is dead for the whole run. This is idempotent -- robots
        that already know the task keep their holder and status.
        """
        if not self.flags.task_reannounce or not self._announced:
            return
        if self.tick - self._last_announce_tick < TASK_REANNOUNCE_TICKS:
            return
        unheard = [
            t
            for t in self.world.tasks.values()
            if t.status != "DONE"
            and any(t.task_id not in bot.known_tasks for bot in self.robots)
        ]
        if not unheard:
            return
        self._last_announce_tick = self.tick
        for t in unheard:
            self.bus.broadcast(
                "WMS",
                {
                    "type": MSG_TASK_ANNOUNCE,
                    "task_id": t.task_id,
                    "pick": list(t.pick),
                    "drop": list(t.drop),
                    "urgency": t.urgency,
                },
                self.tick,
            )
        self.events.append(
            {
                "tick": self.tick,
                "robot": "WMS",
                "kind": "reannounce",
                "detail": f"{len(unheard)} tasks unheard",
            }
        )

    def _watch_progress(self) -> None:
        """Fleet-level stall episodes, counted for both policies.

        Liveness used to be inferred from each robot's conflict counter, which
        UBPA barely touched, so a frozen fleet reported zero deadlocks. Here the
        yardstick is completion or movement: no task finished and no robot moved
        for STALL_WINDOW ticks while work is outstanding.
        """
        if not self._announced:
            return
        done = sum(1 for t in self.world.tasks.values() if t.status == "DONE")
        moved = any(b.velocity != (0, 0) for b in self.robots)
        if done > self._done_mark or moved or done == len(self.world.tasks):
            self._done_mark = done
            self._last_progress_tick = self.tick
            self._stall_marked = False
            return
        if self._stall_marked:
            return
        if self.tick - self._last_progress_tick >= STALL_WINDOW:
            self._stall_marked = True
            self._stall_episodes += 1
            self.events.append(
                {
                    "tick": self.tick,
                    "robot": "FLEET",
                    "kind": "stall",
                    "detail": f"no progress for {STALL_WINDOW} ticks ({done}/{len(self.world.tasks)} done)",
                }
            )

    def _announce_tasks(self) -> None:
        self._last_announce_tick = self.tick
        self._last_progress_tick = self.tick
        for t in self.world.tasks.values():
            t.announced_tick = self.tick
            t.status = "OPEN"
            t.holder = None
            self.bus.broadcast(
                "WMS",
                {
                    "type": MSG_TASK_ANNOUNCE,
                    "task_id": t.task_id,
                    "pick": list(t.pick),
                    "drop": list(t.drop),
                    "urgency": t.urgency,
                },
                self.tick,
            )
        self.events.append(
            {
                "tick": self.tick,
                "robot": "WMS",
                "kind": "announce",
                "detail": f"{len(self.world.tasks)} tasks",
            }
        )

    def _sync_task_board(self) -> None:
        """Spectator view of holders. World does not assign — robots do."""
        holders: dict[str, str] = {}
        for bot in self.robots:
            if bot.current_task:
                holders[bot.current_task] = bot.id
            for tid, kt in bot.known_tasks.items():
                if kt.status == "DONE":
                    t = self.world.tasks.get(tid)
                    if t and t.status != "DONE":
                        t.status = "DONE"
                        t.holder = bot.id
                        t.complete_tick = self.tick
        for tid, t in self.world.tasks.items():
            if t.status == "DONE":
                continue
            if tid in holders:
                t.holder = holders[tid]
                if t.status == "OPEN":
                    t.status = "CLAIMED"
            else:
                # released or never claimed
                claimed_elsewhere = False
                for bot in self.robots:
                    kt = bot.known_tasks.get(tid)
                    if kt and kt.holder and kt.status not in ("OPEN", "DONE"):
                        t.holder = kt.holder
                        t.status = kt.status
                        claimed_elsewhere = True
                        break
                if not claimed_elsewhere and t.status != "DONE":
                    t.holder = None
                    if t.status not in ("OPEN",):
                        t.status = "OPEN"
