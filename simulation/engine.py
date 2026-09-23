"""Tick loop. World is physics + radio. Robots decide everything else."""

from __future__ import annotations

import copy
import random
import time
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
from simulation.maps import get_map, map_keys
from simulation.metrics import RunMetrics
from simulation.peer import MSG_OBSTACLE, MSG_TASK_ANNOUNCE, PeerBus
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
    # Selectable floor plan (see simulation/maps.py). "warehouse" reproduces the
    # measured benchmark layout byte for byte, so results/ stays comparable.
    env: str = "warehouse"
    # Wall-clock speed multiplier, honoured by the runner loop, not by the sim:
    # the tick size never changes, only how fast ticks are consumed.
    speed: float = 1.0
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
    # Sequence for operator-posted jobs, so their ids never collide with the
    # announced T1..Tn set.
    _job_seq: int = 0

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._rng = random.Random(self.seed)
        if self.env in map_keys():
            spec = get_map(self.env)
            self.world = spec.build()
        else:
            spec = None
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
        self._job_seq = 0
        if self.tasks is not None:
            jobs = self.tasks
        elif spec is not None:
            jobs = spec.tasks()
        else:
            jobs = default_tasks()
        self.world.tasks = {t.task_id: copy.copy(t) for t in jobs}
        if self.spawn is not None:
            spawns = self.spawn
        elif spec is not None:
            spawns = spec.spawn()
        else:
            spawns = spawn_positions()
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
        if not on and self.world.in_bounds((x, y)) and (x, y) not in self.world.walls:
            # Operator cleared a block: tell the fleet so robots drop the cell
            # from known_blocked instead of treating it as blocked forever.
            self.bus.broadcast(
                "WMS",
                {"type": MSG_OBSTACLE, "cell": [x, y], "cleared": True},
                self.tick,
            )
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

    # ----- operator controls -------------------------------------------

    def set_env(self, env: str) -> str:
        """Switch floor plan. Rebuilds at tick 0; robots still auction the work."""
        if env in map_keys():
            self.env = env
        self.reset()
        self.events.append(
            {"tick": self.tick, "robot": "OP", "kind": "env", "detail": get_map(self.env).label}
        )
        return self.env

    def set_robots(self, n: int) -> int:
        """Fleet size, clamped to the 3..8 range the layout supports."""
        self.n_robots = max(3, min(8, int(n)))
        self.reset()
        self.events.append(
            {"tick": self.tick, "robot": "OP", "kind": "robots", "detail": f"{self.n_robots} robots"}
        )
        return self.n_robots

    def add_task(
        self, pick: tuple[int, int], drop: tuple[int, int], urgency: float = 0.6
    ) -> Task:
        """Post a job to the floor.

        This is the WMS half of the problem statement, not the coordinator half:
        the job is *broadcast* as a bulletin on the same radio the robots already
        use. No robot is named, no robot is told to take it. Whoever bids lowest
        wins it through the ordinary auction.
        """
        self._job_seq += 1
        tid = f"J{self._job_seq}"
        task = Task(
            task_id=tid,
            pick=(int(pick[0]), int(pick[1])),
            drop=(int(drop[0]), int(drop[1])),
            urgency=max(0.0, min(1.0, float(urgency))),
        )
        self.post_task(task)
        self.events.append(
            {
                "tick": self.tick,
                "robot": "WMS",
                "kind": "job",
                "detail": f"{tid} posted pick {task.pick} -> drop {task.drop}",
            }
        )
        return task

    def post_task(self, task: Task) -> Task:
        """Put a job on the floor and broadcast it over the peer radio.

        Shared by the operator's job board and the WMS bulletin so both travel
        the identical route: a broadcast every robot may hear, never an
        assignment to a named robot. The auction still decides who takes it.
        """
        task.status = "OPEN"
        task.holder = None
        task.complete_tick = None
        task.announced_tick = self.tick
        self.world.tasks[task.task_id] = task
        self._broadcast_task(task)
        return task

    def remove_task(self, task_id: str) -> bool:
        """Withdraw a posted job. A robot already carrying it finishes it."""
        task = self.world.tasks.pop(task_id, None)
        if task is None:
            return False
        self.events.append(
            {"tick": self.tick, "robot": "WMS", "kind": "job", "detail": f"{task_id} withdrawn"}
        )
        return True

    def drain_robot(self, robot_id: str, level: float = 15.0) -> float:
        """Force one robot's battery down so it has to decide to charge itself.

        The operator changes the world, not the plan: the robot notices its own
        low battery on its next tick and drives to the charger on its own.
        """
        for bot in self.robots:
            if bot.id == robot_id:
                bot.battery = max(0.0, min(100.0, float(level)))
                bot._event(self.tick, "battery", f"operator set {bot.battery:.0f}%")
                return bot.battery
        return -1.0

    def forecast(self, limit: int | None = None) -> dict[str, Any]:
        """Shadow-run a copy of the current state and report when it finishes.

        The clone keeps the seed, the RNG position, the radio bus and every
        blocked cell, so the answer is deterministic for the same question. The
        live sim is never stepped here -- it is a forecast of *this* fleet from
        *this* moment, which is why an obstacle changes the number.
        """
        clone = copy.deepcopy(self)
        clone.paused = False
        clone.events = []
        for bot in clone.robots:
            bot.events = []
        cap = int(limit) if limit else self.max_ticks
        t0 = time.perf_counter()
        while clone.tick < cap and not clone.all_done():
            clone.step()
        compute_ms = (time.perf_counter() - t0) * 1000.0
        m = clone.metrics()
        finish = m.all_tasks_complete_tick
        eta_ticks = None if finish is None else finish - self.tick
        return {
            "from_tick": self.tick,
            "horizon_ticks": cap,
            "finish_tick": finish,
            "eta_ticks": eta_ticks,
            "eta_seconds": (
                None
                if eta_ticks is None
                else round(eta_ticks / (TICK_HZ * max(0.1, self.speed)), 1)
            ),
            "finished": clone.all_done(),
            "tasks_done": m.tasks_done,
            "tasks_total": m.tasks_total,
            "collisions": m.collision_count,
            "deadlocks": m.deadlock_count,
            "compute_ms": round(compute_ms, 1),
        }

    def _broadcast_task(self, task: Task) -> None:
        self.bus.broadcast(
            "WMS",
            {
                "type": MSG_TASK_ANNOUNCE,
                "task_id": task.task_id,
                "pick": list(task.pick),
                "drop": list(task.drop),
                "urgency": task.urgency,
            },
            self.tick,
        )

    def _auction_view(self, limit: int = 4) -> list[dict[str, Any]]:
        """Read-only window onto the live auctions, for the dashboard.

        This is evidence, not control: it shows the bids robots have actually
        broadcast to each other. Nothing here can pick a winner.
        """
        out: list[dict[str, Any]] = []
        for bot in self.robots:
            for tid, auc in bot.auctions.items():
                valid = sum(1 for _, ok in auc.bids.values() if ok)
                if not auc.bids:
                    continue
                best_id, best = None, None
                for rid, (cost, ok) in auc.bids.items():
                    if not ok:
                        continue
                    if best is None or cost < best:
                        best, best_id = cost, rid
                out.append(
                    {
                        "task_id": tid,
                        "viewer": bot.id,
                        "opened_tick": auc.opened_tick,
                        "closed": auc.closed,
                        "bids": len(auc.bids),
                        "valid_bids": valid,
                        "best_bidder": best_id,
                        "best_cost": None if best is None else round(best, 2),
                    }
                )
            if len(out) >= limit:
                break
        return out[:limit]

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
        spec = get_map(self.env)
        return {
            "tick": self.tick,
            "hz": TICK_HZ,
            "policy": self.policy,
            "paused": self.paused,
            "complete": self.all_done(),
            "speed": self.speed,
            "n_robots": self.n_robots,
            "env": self.env,
            "map": {"key": spec.key, "label": spec.label, "blurb": spec.blurb},
            "jobs": [
                {
                    "task_id": t.task_id,
                    "pick": list(t.pick),
                    "drop": list(t.drop),
                    "urgency": t.urgency,
                    "status": t.status,
                    "holder": t.holder,
                    "complete_tick": t.complete_tick,
                }
                for t in self.world.tasks.values()
            ],
            "auctions": self._auction_view(),
            "charger": {
                "cell": list(self.world.charger),
                "charging": [b.id for b in self.robots if b.phase == "CHARGING"],
            },
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
            # Same route as an operator-posted job: broadcast, never assignment.
            self.post_task(t)
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
