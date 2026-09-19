"""The host half of the split: physics, clock and transport. Never a dispatcher.

``EdgeHost`` owns three things and nothing else:

1. **Physics.** It owns the one ``Warehouse`` and is the only process allowed to
   run ``apply_moves``. A robot moves because its own ``ACT`` asked to move, or
   it does not move at all.
2. **The clock.** ``tick`` is published downward in every ``OBS`` so peers can
   compare timestamps. A clock is not a decision.
3. **Transport.** It relays the WMS bulletin and carries ``OBS``/``ACT_RESULT``.
   Real peer traffic never passes through here: on the loop-back medium the
   robots share a ``SharedMedium`` directly, and over UDP they unicast to each
   other's own port.

The step order is a copy of ``simulation/engine.py::FleetSim.step`` (lines
116-146) because ``tests/test_edge_agent.py::test_lockstep_parity_with_engine``
requires the edge path to reproduce the reference run, tick for tick:

    tick += 1
    forced block (if scheduled)
    announce / re-announce on the medium
    medium.tick(tick)             <- messages broadcast last tick land here
    publish OBS to every agent
    every agent steps, publishing its own ACT
    world.apply_moves(intentions) <- the only mutator of world.robots
    publish ACT_RESULT to every agent
    _sync_task_board(); _watch_progress()

What is deliberately absent: no ``_winner``, no route planning, no ``_plan``, no
per-robot cost ordering. Those live in ``simulation/robot.py`` and are proven to
stay there by ``tests/test_edge_no_central_scheduler.py``.
"""

from __future__ import annotations

import copy
import random
import selectors
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from simulation.config import (
    MAX_TICKS_DEMO,
    RADIO_DELAY,
    SENSOR_RANGE,
    STALL_WINDOW,
    TASK_REANNOUNCE_TICKS,
    TICK_HZ,
    CoordinationFlags,
)
from simulation.edge.agent import HEARTBEAT_TICKS, EdgeAgent
from simulation.edge.protocol import (
    CTRL_ACT_RESULT,
    CTRL_OBS,
    CTRL_HELLO_WORLD,
    CTRL_SHUTDOWN,
    PEER_TYPES,
    HOST_ID,
    WMS_ID,
    chunk_message,
    decode_safe,
    head,
    world_digest,
)
from simulation.edge.radio import LoopbackRadio, SharedMedium, UdpRadio, host_bulletin
from simulation.maps import get_map, map_keys
from simulation.metrics import RunMetrics
from simulation.peer import MSG_TASK_ANNOUNCE
from simulation.world import Task, Warehouse, default_tasks, hyderabad_layout, spawn_positions

Pos = tuple[int, int]

#: Ticks of ACT silence after which the spectator view stops calling an agent
#: "alive". The robot is not touched - it is only *reported* honestly.
DEAD_TICKS = 8

#: How long the host will wait for an in-flight CTRL datagram at the end of a
#: tick in UDP mode (seconds). The in-process agents answer within microseconds;
#: this is a ceiling, not a delay.
UDP_POLL_S = 0.05


@dataclass
class _Kt:
    """Read-only stand-in for ``robot.KnownTask`` when the robot is remote."""

    status: str = "OPEN"
    holder: str | None = None


def _cells(raw: Any) -> list[list[int]]:
    return [[int(p[0]), int(p[1])] for p in (raw or ())]


@dataclass
class EdgeHost:
    """One simulated world driven by robots that could be on other machines."""

    n_robots: int = 3
    policy: str = "ubpa"
    seed: int = 0
    drop_prob: float = 0.0
    radio_delay: int = RADIO_DELAY
    max_ticks: int = MAX_TICKS_DEMO
    tasks: list[Task] | None = None
    spawn: list[Pos] | None = None
    env: str = "warehouse"
    announce_at: int = 4
    forced_block: Pos | None = None
    forced_block_tick: int | None = None
    flags: CoordinationFlags = field(default_factory=CoordinationFlags)
    #: "loopback" shares an in-process medium; "udp" carries the same messages
    #: over real sockets on the host's own loopback interface.
    transport: str = "loopback"
    udp_host: str = "127.0.0.1"
    udp_base_port: int = 47100
    advertise: str = "127.0.0.1"

    # ----- built by reset() ---------------------------------------------
    world: Warehouse = field(init=False)
    agents: dict[str, EdgeAgent] = field(init=False, default_factory=dict)
    order: list[str] = field(init=False, default_factory=list)
    medium: SharedMedium | None = field(init=False, default=None)
    sock: socket.socket | None = field(init=False, default=None)
    _addr: dict[str, tuple[str, int]] = field(init=False, default_factory=dict)
    _heartbeats: dict[str, dict[str, Any]] = field(init=False, default_factory=dict)
    _last_act_tick: dict[str, int] = field(init=False, default_factory=dict)
    _prev_pos: dict[str, Pos] = field(init=False, default_factory=dict)
    #: Robot ids the operator has taken off the network (T5). They are not
    #: stepped and not observed; the world still remembers where they stopped.
    silent: set[str] = field(init=False, default_factory=set)
    events: list[dict[str, Any]] = field(init=False, default_factory=list)
    tick: int = 0
    paused: bool = False
    _announced: bool = field(init=False, default=False)
    _last_announce_tick: int = field(init=False, default=0)
    _last_progress_tick: int = field(init=False, default=0)
    _done_mark: int = field(init=False, default=0)
    _stall_marked: bool = field(init=False, default=False)
    _stall_episodes: int = field(init=False, default=0)
    _job_seq: int = field(init=False, default=0)
    #: Tick at which every task first reached DONE (first, not latest).
    _complete_tick: int | None = field(init=False, default=None)
    _digest: str = field(init=False, default="")
    # telemetry
    ctrl_sent: int = field(init=False, default=0)
    ctrl_recv: int = field(init=False, default=0)
    malformed: int = field(init=False, default=0)
    unknown: int = field(init=False, default=0)
    foreign: int = field(init=False, default=0)
    peer_leaked: int = field(init=False, default=0)
    late_acts: int = field(init=False, default=0)
    _rtt_sum: float = field(init=False, default=0.0)
    _rtt_n: int = field(init=False, default=0)
    _prev_ack: dict[str, bool] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.reset()

    # ----- construction -------------------------------------------------

    def reset(self) -> None:
        """Build the world, the radios and the agents. Mirrors ``FleetSim.reset``."""
        self._rng = random.Random(self.seed)
        if self.env in map_keys():
            spec = get_map(self.env)
            self.world = spec.build()
        else:
            spec = None
            self.world = hyderabad_layout()

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
        self._complete_tick = None
        self.ctrl_sent = 0
        self.ctrl_recv = 0
        self.malformed = 0
        self.unknown = 0
        self.foreign = 0
        self.peer_leaked = 0
        self.late_acts = 0
        self._rtt_sum = 0.0
        self._rtt_n = 0
        self.silent = set()
        self._heartbeats = {}
        self._last_act_tick = {}
        self._prev_pos = {}
        self._prev_ack = {}
        self._addr = {}

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

        self._digest = world_digest(
            self.world.width, self.world.height, self.world.walls, self.world.turnouts
        )
        self.medium = None
        self.sock = None
        self.close()  # release any previous transport before rebuilding

        if self.transport == "loopback":
            self._build_loopback(spawns)
        else:
            self._build_udp(spawns)

        # WMS bulletin is not a robot; the sender id only needs to exist.
        if self.medium is not None:
            self.medium.register(WMS_ID)
        for rid in self.order:
            self._prev_pos[rid] = self.world.robots[rid]
            self._last_act_tick[rid] = -1
            self._heartbeats[rid] = {}
            self._send_world(rid)

    @property
    def robots(self) -> list[EdgeAgent]:
        """The fleet in registration order, so ``_sync_task_board`` matches the engine."""
        return [self.agents[rid] for rid in self.order]

    def _ids(self) -> list[str]:
        return [f"R{i + 1}" for i in range(self.n_robots)]

    def _spawn_of(self, spawns: list[Pos], index: int) -> Pos:
        pos = spawns[index % len(spawns)]
        return (int(pos[0]), int(pos[1]))

    def _build_loopback(self, spawns: list[Pos]) -> None:
        self.medium = SharedMedium(delay=self.radio_delay, drop_prob=self.drop_prob, seed=self.seed)
        for i, rid in enumerate(self._ids()):
            pos = self._spawn_of(spawns, i)
            self.world.robots[rid] = pos
            radio = LoopbackRadio(rid, self.medium)  # registers rid in the medium
            self.agents[rid] = EdgeAgent(
                rid, pos, radio, priority=1, policy=self.policy, flags=self.flags
            )
        self.order = list(self.agents.keys())

    def _build_udp(self, spawns: list[Pos]) -> None:
        host = (self.udp_host, int(self.udp_base_port))
        radio_socks = self._make_host_socket(host)
        if radio_socks is None:  # pragma: no cover - a busy port falls back to loop-back
            self.transport = "loopback"
            self._build_loopback(spawns)
            return
        self.sock = radio_socks

        ids = self._ids()
        ports = {rid: int(self.udp_base_port) + 1 + i for i, rid in enumerate(ids)}
        peers_universe = {rid: (self.advertise, ports[rid]) for rid in ids}
        for i, rid in enumerate(ids):
            pos = self._spawn_of(spawns, i)
            self.world.robots[rid] = pos
            radio = UdpRadio(
                rid,
                peers={k: v for k, v in peers_universe.items() if k != rid},
                listen=(self.advertise, ports[rid]),
                drop_prob=self.drop_prob,
                seed=self.seed,
            )
            radio.set_host(host)
            self.agents[rid] = EdgeAgent(
                rid, pos, radio, priority=1, policy=self.policy, flags=self.flags
            )
            self._addr[rid] = (self.advertise, radio.port)
        self.order = list(self.agents.keys())

    def _make_host_socket(self, bind: tuple[str, int]) -> socket.socket | None:
        for attempt in range(4):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((bind[0], bind[1] + attempt * 100))
                sock.setblocking(False)
                return sock
            except OSError:  # pragma: no cover - only when the port is taken
                continue
        return None

    # ----- transport ----------------------------------------------------

    def _send(self, rid: str, msg: dict[str, Any]) -> None:
        """Encode and transmit one CTRL message to one agent."""
        parts = chunk_message(msg)
        self.ctrl_sent += len(parts)
        if self.transport == "loopback":
            self.agents[rid].deliver_datagrams(parts)
            self.ctrl_recv += len(parts)
            return
        if self.sock is None:  # pragma: no cover - defensive
            return
        addr = self._addr.get(rid)
        if addr is None:  # pragma: no cover - defensive
            return
        for part in parts:
            try:
                self.sock.sendto(part, addr)
            except OSError:  # pragma: no cover - host link down
                self.late_acts += 1

    def _world_message(self, rid: str) -> dict[str, Any]:
        pos = self.world.robots.get(rid) or (1, 1)
        return head(
            CTRL_HELLO_WORLD,
            HOST_ID,
            self.tick,
            to_id=rid,
            digest=self._digest,
            spawn=[int(pos[0]), int(pos[1])],
            world={
                "width": int(self.world.width),
                "height": int(self.world.height),
                "walls": _cells(sorted(self.world.walls)),
                "turnouts": _cells(sorted(self.world.turnouts)),
                "charger": [int(self.world.charger[0]), int(self.world.charger[1])],
                "sensor_range": int(SENSOR_RANGE),
                "digest": self._digest,
            },
        )

    def _obs_message(self, rid: str) -> dict[str, Any]:
        """``Warehouse.sense`` for one robot, in a datagram (doc 04 §1.3)."""
        pos = self.world.robots.get(rid) or (0, 0)
        return head(
            CTRL_OBS,
            HOST_ID,
            self.tick,
            to_id=rid,
            world_digest=self._digest,
            charger=[int(self.world.charger[0]), int(self.world.charger[1])],
            nearby_blocked=_cells(sorted(self.world.sense(pos))),
            sent_ms=round(time.perf_counter() * 1000.0, 3),
        )

    def _result_message(
        self,
        rid: str,
        requested: Pos | None,
        *,
        accepted: bool,
        blocked_by: str,
        collision: bool,
    ) -> dict[str, Any]:
        pos = self.world.robots.get(rid) or (0, 0)
        return head(
            CTRL_ACT_RESULT,
            HOST_ID,
            self.tick,
            to_id=rid,
            position=[int(pos[0]), int(pos[1])],
            requested=None if requested is None else [int(requested[0]), int(requested[1])],
            accepted=bool(accepted),
            moved=bool(self._prev_ack.get(rid, False)),
            blocked_by=str(blocked_by),
            collision=bool(collision),
        )

    def _send_world(self, rid: str) -> None:
        self._send(rid, self._world_message(rid))

    def _broadcast_wms(self, row: dict[str, Any], tick: int) -> None:
        """Publish a WMS bulletin. A broadcast, never an assignment."""
        if self.medium is not None:
            # Loop-back: the exact dict the engine hands to ``PeerBus.broadcast``.
            self.medium.broadcast(WMS_ID, row, tick)
            return
        if self.sock is None:  # pragma: no cover - defensive
            return
        msg = host_bulletin(row, tick)
        payloads = chunk_message(msg)
        for rid in self.order:
            addr = self._addr.get(rid)
            if addr is None:  # pragma: no cover - defensive
                continue
            self.ctrl_sent += len(payloads)
            for part in payloads:
                try:
                    self.sock.sendto(part, addr)
                except OSError:  # pragma: no cover - host link down
                    self.late_acts += 1

    # ----- intra-tick relays -------------------------------------------

    def _handle_ctrl(self, msg: dict[str, Any], addr: Any = None) -> bool:
        """Route one inbound CTRL frame. Never a decision, only bookkeeping."""
        kind = str(msg.get("type") or "")
        if kind in PEER_TYPES:
            # Peer traffic must travel between robots, not through the host.
            self.peer_leaked += 1
            return False
        rid = str(msg.get("from_id") or "")
        if kind == "HEARTBEAT":
            self._heartbeats[rid] = dict(msg)
            self.ctrl_recv += 1
            return True
        if kind == "HELLO":
            self.ctrl_recv += 1
            return True
        self.unknown += 1
        return False

    def _drain_host_socket(self, budget_s: float = 0.0) -> None:
        """Read CTRL frames sent by agents over UDP (and count stray peer frames)."""
        if self.sock is None:
            return
        deadline = time.perf_counter() + max(0.0, budget_s)
        sel = selectors.DefaultSelector()
        try:
            sel.register(self.sock, selectors.EVENT_READ)
            while True:
                if not sel.select(timeout=0.0 if budget_s <= 0 else max(0.0, deadline - time.perf_counter())):
                    if budget_s <= 0 or time.perf_counter() >= deadline:
                        return
                    continue
                try:
                    data, addr = self.sock.recvfrom(65535)
                except (BlockingIOError, InterruptedError):
                    return
                except OSError:  # pragma: no cover - socket closed under us
                    return
                msg = decode_safe(data)
                if msg is None:
                    self.malformed += 1
                    continue
                self._handle_ctrl(msg, addr)
                if budget_s <= 0:
                    return
        finally:
            sel.close()

    # ----- the tick -----------------------------------------------------

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

        if self.medium is not None:
            self.medium.tick(self.tick)

        for rid in self.order:
            if rid in self.silent:
                continue
            self._send(rid, self._obs_message(rid))

        actions = self.collect_actions()

        intentions: dict[str, Pos] = {}
        requested: dict[str, Pos] = {}
        for rid in self.order:
            msg = actions.get(rid)
            if msg is None:
                self.late_acts += 1
                continue
            dest = msg.get("dest")
            if dest is None:
                continue
            requested[rid] = (int(dest[0]), int(dest[1]))
            intentions[rid] = requested[rid]

        collisions_before = self.world.collision_events
        self.apply(intentions)
        collisions_after = self.world.collision_events
        touched = {rid for rid, pos in self.world.robots.items() if self._prev_pos.get(rid) != pos}
        # Every robot named in a collision recorded this tick is told so, whether
        # it was displaced or merely blocked. The world counted the event; the
        # host only reports it back. No arbitration happens here.
        collided = {
            rid
            for (a, b, at_tick) in self.world.collision_pairs
            if at_tick == self.tick
            for rid in (a, b)
        }

        for rid in self.order:
            old = self._prev_pos.get(rid)
            new = self.world.robots.get(rid)
            moved = old != new
            self._prev_ack[rid] = moved
            self._prev_pos[rid] = new
            want = requested.get(rid)
            if rid in self.silent:
                accepted, reason = False, "silent"
            elif want is None:
                accepted, reason = False, "late"
            elif new == want:
                accepted, reason = True, ""
            else:
                accepted, reason = False, self._reject_reason(want, rid)
            collision = rid in collided or bool(want is not None and new != want and rid in touched)
            self._send(rid, self._result_message(rid, want, accepted=accepted, blocked_by=reason, collision=collision))

        del touched, collisions_before, collisions_after
        if self.transport == "udp":
            self._drain_host_socket(UDP_POLL_S)

        if self.tick % HEARTBEAT_TICKS == 0:
            for rid in self.order:
                if rid in self.silent:
                    continue
                hb = self.agents[rid].send_heartbeat(self.tick)
                self._heartbeats[rid] = dict(hb)
                self._last_act_tick.setdefault(rid, self.tick)

        self._sync_task_board()
        self._watch_progress()
        if self._complete_tick is None and self.world.tasks and self.all_done():
            # Record the FIRST tick the fleet finished, like the engine's accumulator,
            # so a host that keeps stepping past completion still reports the truth.
            self._complete_tick = self.tick

    def _reject_reason(self, dest: Pos, rid: str) -> str:
        """Why a move did not happen. Physics vocabulary only - never a decision."""
        if not self.world.in_bounds(dest):
            return "bounds"
        if dest in self.world.walls:
            return "wall"
        if dest in self.world.blocked:
            return "blocked"
        if self.world.robots.get(rid) == dest:
            return "blocked"
        return "collision"

    def collect_actions(self) -> dict[str, Any]:
        """Step every live agent once and return ``{rid: ACT payload or None}``.

        This is the single place a robot's decision enters the host, and the
        value is whatever the robot's own ``_choose_move`` produced. The host
        never edits it.
        """
        actions: dict[str, Any] = {}
        for rid in self.order:
            if rid in self.silent:
                actions[rid] = None
                continue
            msg = self.agents[rid].tick_once(self.tick)
            if msg is None:
                actions[rid] = None
                continue
            self._last_act_tick[rid] = self.tick
            self.ctrl_recv += 1
            sent_ms = msg.get("obs_ms")
            if sent_ms is not None:
                rtt = time.perf_counter() * 1000.0 - float(sent_ms)
                if rtt >= 0:
                    self._rtt_sum += rtt
                    self._rtt_n += 1
            actions[rid] = msg
        return actions

    def apply(self, intentions: dict[str, Pos]) -> dict[str, Pos]:
        """The only mutator of ``world.robots``. Destinations come from ACTs."""
        return self.world.apply_moves(intentions, self.tick)

    def run_until(
        self, max_ticks: int | None = None, on_tick: Callable[["EdgeHost"], None] | None = None
    ) -> RunMetrics:
        limit = max_ticks if max_ticks is not None else self.max_ticks
        while self.tick < limit and not self.all_done():
            self.step()
            if on_tick:
                on_tick(self)
        return self.metrics()

    #: Alias kept for symmetry with ``FleetSim.run_until`` readers.
    run = run_until

    def all_done(self) -> bool:
        return all(t.status == "DONE" for t in self.world.tasks.values()) and bool(self.world.tasks)

    # ----- operator controls (world state only) -------------------------

    def toggle_block(self, x: int, y: int) -> bool:
        on = self.world.toggle_block((int(x), int(y)))
        self.events.append(
            {
                "tick": self.tick,
                "robot": "OP",
                "kind": "block" if on else "unblock",
                "detail": f"{(int(x), int(y))}",
            }
        )
        return on

    def set_radio(self, drop_prob: float | None = None, delay: int | None = None) -> dict[str, Any]:
        if drop_prob is not None:
            self.drop_prob = max(0.0, min(0.9, float(drop_prob)))
            if self.medium is not None:
                self.medium.drop_prob = self.drop_prob
            for agent in self.agents.values():
                radio = agent.radio
                if hasattr(radio, "drop_prob"):
                    radio.drop_prob = self.drop_prob
        if delay is not None:
            self.radio_delay = max(0, min(8, int(delay)))
            if self.medium is not None:
                self.medium.delay = self.radio_delay
        return {
            "drop_prob": self.drop_prob,
            "delay": self.radio_delay,
            "dropped": 0 if self.medium is None else self.medium.dropped_count,
            "delivered": 0 if self.medium is None else self.medium.delivered_count,
        }

    def set_env(self, env: str) -> str:
        if env in map_keys():
            self.env = env
        self.reset()
        self.events.append(
            {"tick": self.tick, "robot": "OP", "kind": "env", "detail": get_map(self.env).label}
        )
        return self.env

    def add_task(self, pick: Pos, drop: Pos, urgency: float = 0.6) -> Task:
        """Post a job: broadcast on the same radio the robots already use."""
        self._job_seq += 1
        task = Task(
            task_id=f"J{self._job_seq}",
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
                "detail": f"{task.task_id} posted pick {task.pick} -> drop {task.drop}",
            }
        )
        return task

    def post_task(self, task: Task) -> Task:
        task.status = "OPEN"
        task.holder = None
        task.complete_tick = None
        task.announced_tick = self.tick
        self.world.tasks[task.task_id] = task
        self._broadcast_task(task)
        return task

    def remove_task(self, task_id: str) -> bool:
        task = self.world.tasks.pop(str(task_id), None)
        if task is None:
            return False
        self.events.append(
            {"tick": self.tick, "robot": "WMS", "kind": "job", "detail": f"{task_id} withdrawn"}
        )
        return True

    # ----- WMS bulletin helpers (shared with the engine's payload shape) --

    def _bulletin_row(self, task: Task) -> dict[str, Any]:
        return {
            "type": MSG_TASK_ANNOUNCE,
            "task_id": task.task_id,
            "pick": list(task.pick),
            "drop": list(task.drop),
            "urgency": task.urgency,
        }

    def _broadcast_task(self, task: Task) -> None:
        self._broadcast_wms(self._bulletin_row(task), self.tick)

    def _announce_tasks(self) -> None:
        self._last_announce_tick = self.tick
        self._last_progress_tick = self.tick
        for t in self.world.tasks.values():
            self.post_task(t)
        self.events.append(
            {
                "tick": self.tick,
                "robot": "WMS",
                "kind": "announce",
                "detail": f"{len(self.world.tasks)} tasks",
            }
        )

    def _reannounce_unheard(self) -> None:
        if not self.flags.task_reannounce or not self._announced:
            return
        if self.tick - self._last_announce_tick < TASK_REANNOUNCE_TICKS:
            return
        board = self._board()
        unheard = [
            t
            for t in self.world.tasks.values()
            if t.status != "DONE"
            and any(t.task_id not in board[rid]["known_tasks"] for rid in self.order)
        ]
        if not unheard:
            return
        self._last_announce_tick = self.tick
        for t in unheard:
            self._broadcast_wms(self._bulletin_row(t), self.tick)
        self.events.append(
            {
                "tick": self.tick,
                "robot": "WMS",
                "kind": "reannounce",
                "detail": f"{len(unheard)} tasks unheard",
            }
        )

    # ----- spectator board (read-only) ---------------------------------

    def _board(self) -> dict[str, dict[str, Any]]:
        """Read-only view of what each robot believes. Never an instruction."""
        out: dict[str, dict[str, Any]] = {}
        for rid in self.order:
            robot = getattr(self.agents[rid], "robot", None)
            if robot is not None:
                out[rid] = {"current_task": robot.current_task, "known_tasks": robot.known_tasks}
                continue
            hb = self._heartbeats.get(rid) or {}
            statuses = hb.get("known_tasks") or {}
            holders = hb.get("task_holders") or {}
            known: dict[str, _Kt] = {}
            for tid, status in statuses.items():
                known[str(tid)] = _Kt(status=str(status), holder=holders.get(tid))
            out[rid] = {"current_task": hb.get("current_task"), "known_tasks": known}
        return out

    def _sync_task_board(self) -> None:
        """Spectator view of holders. The world does not assign - robots do."""
        board = self._board()
        holders: dict[str, str] = {}
        for rid in self.order:
            entry = board[rid]
            if entry["current_task"]:
                holders[entry["current_task"]] = rid
            for tid, kt in entry["known_tasks"].items():
                if kt.status == "DONE":
                    t = self.world.tasks.get(tid)
                    if t and t.status != "DONE":
                        t.status = "DONE"
                        t.holder = rid
                        t.complete_tick = self.tick
        for tid, t in self.world.tasks.items():
            if t.status == "DONE":
                continue
            if tid in holders:
                t.holder = holders[tid]
                if t.status == "OPEN":
                    t.status = "CLAIMED"
            else:
                claimed_elsewhere = False
                for rid in self.order:
                    kt = board[rid]["known_tasks"].get(tid)
                    if kt and kt.holder and kt.status not in ("OPEN", "DONE"):
                        t.holder = kt.holder
                        t.status = kt.status
                        claimed_elsewhere = True
                        break
                if not claimed_elsewhere and t.status != "DONE":
                    t.holder = None
                    if t.status not in ("OPEN",):
                        t.status = "OPEN"

    def _watch_progress(self) -> None:
        """Fleet-level stall episodes, inferred from completion or movement."""
        if not self._announced:
            return
        done = sum(1 for t in self.world.tasks.values() if t.status == "DONE")
        moved = any(self._prev_ack.get(rid, False) for rid in self.order)
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
                    "detail": f"no progress for {STALL_WINDOW} ticks "
                    f"({done}/{len(self.world.tasks)} done)",
                }
            )

    # ----- measurement --------------------------------------------------

    def _counters(self, rid: str) -> dict[str, float]:
        robot = getattr(self.agents[rid], "robot", None)
        if robot is not None:
            return {
                "wait_ticks": float(robot.wait_ticks),
                "move_ticks": float(robot.move_ticks),
                "distance": float(robot.distance),
                "deadlock_events": float(robot.deadlock_events),
                "stall_handoffs": float(robot.stall_handoffs),
                "tasks_done": float(robot.tasks_done),
                "step_ms_sum": float(robot.step_ms_sum),
                "step_n": float(robot.step_n),
            }
        hb = self._heartbeats.get(rid) or {}
        raw = hb.get("counters") or {}
        return {
            "wait_ticks": float(raw.get("wait_ticks", 0)),
            "move_ticks": float(raw.get("move_ticks", 0)),
            "distance": float(raw.get("distance", 0.0)),
            "deadlock_events": float(raw.get("deadlock_events", 0)),
            "stall_handoffs": float(raw.get("stall_handoffs", 0)),
            "tasks_done": float(raw.get("tasks_done", 0)),
            "step_ms_sum": float(raw.get("step_ms_sum", 0.0)),
            "step_n": float(raw.get("step_n", 0)),
        }

    def metrics(self) -> RunMetrics:
        counters = [self._counters(rid) for rid in self.order]
        waits = sum(c["wait_ticks"] for c in counters)
        moves = sum(c["move_ticks"] for c in counters)
        dist = sum(c["distance"] for c in counters)
        n = sum(c["step_n"] for c in counters)
        step_ms = (sum(c["step_ms_sum"] for c in counters) / n) if n else 0.0
        done = sum(1 for t in self.world.tasks.values() if t.status == "DONE")
        complete = done == len(self.world.tasks) and bool(self.world.tasks)
        mean_wait = (waits / (waits + moves)) if (waits + moves) else 0.0
        return RunMetrics(
            collision_count=self.world.collision_events,
            deadlock_count=int(sum(c["deadlock_events"] for c in counters)),
            stall_count=self._stall_episodes,
            stall_handoff_count=int(sum(c["stall_handoffs"] for c in counters)),
            all_tasks_complete_tick=self._complete_tick if complete else None,
            mean_wait_ticks=mean_wait,
            total_distance=dist,
            mean_comm_latency_ticks=self.link()["mean_latency_ticks"],
            mean_robot_step_ms=step_ms,
            tasks_done=done,
            tasks_total=len(self.world.tasks),
            complete=complete,
            ticks=self.tick,
            policy=self.policy,
            seed=self.seed,
        )

    def mean_rtt_ms(self) -> float:
        if self._rtt_n == 0:
            return 0.0
        return self._rtt_sum / self._rtt_n

    def link(self) -> dict[str, Any]:
        """Transport health. Measured by the transports, never asserted."""
        medium = self.medium
        delivered = 0 if medium is None else int(medium.delivered_count)
        dropped = 0 if medium is None else int(medium.dropped_count)
        latency = 0.0 if medium is None else float(medium.mean_latency())
        sent = delivered + dropped
        return {
            "transport": self.transport,
            "drop_prob": self.drop_prob,
            "delay": self.radio_delay,
            "sent": sent,
            "recv": delivered,
            "dropped": dropped,
            "lost_pct": round(100.0 * dropped / sent, 3) if sent else 0.0,
            "mean_latency_ticks": latency,
            "ctrl_sent": self.ctrl_sent,
            "ctrl_recv": self.ctrl_recv,
            "late_acts": self.late_acts,
            "malformed": self.malformed,
            "unknown": self.unknown,
            "foreign": self.foreign,
            "peer_leaked": self.peer_leaked,
            "mean_rtt_ms": round(self.mean_rtt_ms(), 4),
            "rtt_samples": self._rtt_n,
        }

    def agent_state(self, rid: str) -> str:
        last = self._last_act_tick.get(rid, -1)
        if rid in self.silent:
            return "dead"
        if last < 0:
            return "boot"
        return "dead" if self.tick - last > DEAD_TICKS else "alive"

    def agent_view(self, rid: str) -> dict[str, Any]:
        agent = self.agents[rid]
        robot = getattr(agent, "robot", None)
        hb = self._heartbeats.get(rid) or {}
        last = self._last_act_tick.get(rid, -1)
        return {
            "id": rid,
            "state": self.agent_state(rid),
            "ready": bool(agent.ready),
            "policy": agent.policy,
            "priority": agent.priority,
            "agent_version": agent.agent_version,
            "last_seen_tick": last,
            "tick_lag": None if last < 0 else self.tick - last,
            "silent_ticks": None if last < 0 else self.tick - last,
            "step_ms_p95": round(
                agent.step_ms_p95 if robot is not None else float(hb.get("step_ms_p95", 0.0)), 4
            ),
            "heartbeat_tick": hb.get("tick"),
            "radio": agent.telemetry()["radio"],
            "position": None
            if robot is None
            else [int(robot.pos[0]), int(robot.pos[1])],
        }

    def _auction_view(self, limit: int = 4) -> list[dict[str, Any]]:
        """Read-only projection of the auctions robots actually ran. Evidence only."""
        out: list[dict[str, Any]] = []
        for rid in self.order:
            robot = getattr(self.agents[rid], "robot", None)
            if robot is None:
                continue
            for tid, auc in robot.auctions.items():
                if not auc.bids:
                    continue
                valid = sum(1 for _, ok in auc.bids.values() if ok)
                best_id, best = None, None
                for bidder, (cost, ok) in auc.bids.items():
                    if not ok:
                        continue
                    if best is None or cost < best:
                        best, best_id = cost, bidder
                out.append(
                    {
                        "task_id": tid,
                        "viewer": rid,
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

    def _robot_view(self) -> list[dict[str, Any]]:
        robots: list[dict[str, Any]] = []
        for rid in self.order:
            robot = getattr(self.agents[rid], "robot", None)
            hb = self._heartbeats.get(rid) or {}
            if robot is not None:
                robots.append(
                    {
                        "id": rid,
                        "pos": list(robot.pos),
                        "battery": round(robot.battery, 2),
                        "status": robot.phase,
                        "task": robot.current_task,
                        "urgency": robot.task_urgency,
                        "priority": robot.priority,
                        "workload": robot.workload,
                        "path": [list(p) for p in robot.planned_path],
                        "ubpa": round(robot.last_ubpa, 3),
                        "velocity": list(robot.velocity),
                    }
                )
                continue
            robots.append(
                {
                    "id": rid,
                    "pos": list(self.world.robots.get(rid, (0, 0))),
                    "battery": float(hb.get("battery") or 0.0),
                    "status": str(hb.get("phase") or "UNKNOWN"),
                    "task": hb.get("current_task"),
                    "urgency": 0.0,
                    "priority": int(hb.get("priority", 1) or 1),
                    "workload": robot_workload(hb),
                    "path": [list(self.world.robots.get(rid, (0, 0)))],
                    "ubpa": 0.0,
                    "velocity": [0, 0],
                }
            )
        return robots

    def snapshot(self) -> dict[str, Any]:
        """The same core shape ``FleetSim.snapshot`` produces, plus edge telemetry."""
        spec = get_map(self.env)
        medium = self.medium
        m = self.metrics().to_dict()
        return {
            "tick": self.tick,
            "hz": TICK_HZ,
            "policy": self.policy,
            "paused": self.paused,
            "complete": self.all_done(),
            "speed": 1.0,
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
                "charging": [
                    rid
                    for rid in self.order
                    if getattr(self.agents[rid], "robot", None) is not None
                    and self.agents[rid].robot.phase == "CHARGING"
                ],
            },
            "world": self.world.snapshot(),
            "robots": self._robot_view(),
            "radio": [] if medium is None else medium.snapshot_log(36),
            "events": self.events[-40:],
            "metrics": m,
            "radio_stats": {
                "drop_prob": self.drop_prob,
                "delay": self.radio_delay,
                "dropped": 0 if medium is None else medium.dropped_count,
                "delivered": 0 if medium is None else medium.delivered_count,
                "mean_latency": 0.0 if medium is None else medium.mean_latency(),
            },
            # Edge-only blocks. A dashboard that ignores them still renders.
            "agents": [self.agent_view(rid) for rid in self.order],
            "link": self.link(),
            "edge": {"transport": self.transport, "digest": self._digest, "agents": len(self.order)},
        }

    # ----- shutdown -----------------------------------------------------

    def stop(self) -> None:
        """Tell the agents to stop deciding (a clock message, not an order)."""
        for rid in self.order:
            try:
                self._send(rid, head(CTRL_SHUTDOWN, HOST_ID, self.tick, to_id=rid))
            except Exception:  # pragma: no cover - best effort at teardown
                pass

    def close(self) -> None:
        for agent in list(self.agents.values()):
            try:
                agent.close()
            except Exception:  # pragma: no cover - best effort at teardown
                pass
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:  # pragma: no cover - best effort
                pass
            self.sock = None

    def __del__(self) -> None:  # pragma: no cover - interpreter teardown
        try:
            self.close()
        except Exception:
            pass


def robot_workload(hb: dict[str, Any]) -> int:
    """Tasks a remote robot has finished, as reported in its heartbeat."""
    counters = hb.get("counters") or {}
    return int(counters.get("tasks_done", 0) or 0)


__all__ = ["DEAD_TICKS", "EdgeHost"]
