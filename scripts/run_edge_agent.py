"""Run one edge agent: the unmodified ``Robot`` deciding on its own.

Two modes, both using the same ``EdgeAgent``/``Robot`` code:

``--offline``
    No host. The agent builds a ``LocalWorldView`` over a real ``Warehouse``; a
    small local harness announces the map's jobs on the peer medium and applies the
    robot's intentions through ``Warehouse.apply_moves``, feeding ``ACT_RESULT``
    back exactly where the host would. This is the "same robot logic runs locally"
    path - what you run on a Pi that has no network yet.
default (network)
    The agent binds UDP, sends ``HELLO``, adopts geometry from ``HELLO_WORLD``,
    then decides every tick and unicasts its ``ACT`` to the host. Peers talk to
    each other directly; nothing is relayed through a broker.

Known limitation (stated, not hidden): the shipped ``EdgeHost`` drives its own
in-process agents, so a host does not yet answer a *separate* agent process. The
network loop below is a complete protocol client awaiting that host capability;
until then use ``--offline`` (or ``scripts/run_edge_host.py``) for a working run.

Stdlib only.
"""

from __future__ import annotations

import argparse
import copy
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.config import MAX_TICKS_DEMO, TICK_HZ, CoordinationFlags  # noqa: E402
from simulation.edge import EdgeAgent, LocalWorldView, LoopbackRadio, SharedMedium  # noqa: E402
from simulation.edge.agent import HEARTBEAT_TICKS  # noqa: E402
from simulation.edge.radio import UdpRadio  # noqa: E402
from simulation.maps import get_map, map_keys  # noqa: E402
from simulation.peer import MSG_TASK_ANNOUNCE  # noqa: E402

HEARTBEAT_EVERY = HEARTBEAT_TICKS


def parse_pairs(raw: str) -> dict[str, tuple[str, int]]:
    peers: dict[str, tuple[str, int]] = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        rid, _, addr = item.partition("=")
        host, _, port = addr.partition(":")
        peers[rid.strip()] = (host.strip(), int(port or 9200))
    return peers


def run_offline(args: argparse.Namespace) -> int:
    """One robot against a real ``Warehouse``, with no host in the loop."""
    spec = get_map(args.env)
    world = spec.build()
    # ``build()`` returns geometry; the jobs are a separate piece of the map spec
    # (the engine attaches them the same way at reset).
    world.tasks = {t.task_id: copy.copy(t) for t in spec.tasks()}
    pos = tuple(spec.spawn()[0])

    medium = SharedMedium(delay=1, drop_prob=0.0, seed=args.seed)
    radio = LoopbackRadio(args.id, medium)
    registry = world.robots
    registry[args.id] = pos
    agent = EdgeAgent(
        args.id, pos, radio,
        priority=args.priority,
        policy=args.policy,
        flags=CoordinationFlags(),
        view=LocalWorldView(world),
    )
    robot = agent.install()
    if robot is None:  # pragma: no cover - the inputs above are always complete
        print("! could not build the robot", file=sys.stderr)
        return 2

    # The WMS bulletin is a broadcast, not an order. Announce the jobs once, then
    # let the robot choose exactly as it does in the full simulation.
    for task in world.tasks.values():
        medium.deliver(args.id, {
            "type": MSG_TASK_ANNOUNCE,
            "from_id": "WMS",
            "task_id": task.task_id,
            "pick": list(task.pick),
            "drop": list(task.drop),
            "urgency": task.urgency,
        }, recv_tick=0)
    print(f"offline agent {args.id}  pos={pos}  policy={args.policy}  env={args.env}  "
          f"jobs={len(world.tasks)}")

    tick = 0
    try:
        for tick in range(1, args.max_ticks + 1):
            medium.tick(tick)                       # bulletins sent last tick land now
            act = agent.tick_once(tick)             # the robot decides + publishes its ACT
            if act is None:
                continue
            dest = (int(act["dest"][0]), int(act["dest"][1]))
            moved = world.apply_moves({args.id: dest}, tick)
            robot.note_result(moved[args.id])       # ...and learns where it really is
            # The world's board follows the robot's own view, exactly as the host does.
            for tid, known in robot.known_tasks.items():
                task = world.tasks.get(tid)
                if task is not None and known.status == "DONE" and task.status != "DONE":
                    task.status = "DONE"
                    task.holder = known.holder or args.id
            if tick % HEARTBEAT_EVERY == 0:
                hb = agent.heartbeat(tick)
                print(f"tick {tick:4d} pos={tuple(robot.pos)} phase={robot.phase} "
                      f"task={robot.current_task} battery={robot.battery:.1f} "
                      f"step_ms_p95={hb['step_ms_p95']}")
            if world.tasks and all(t.status == "DONE" for t in world.tasks.values()):
                print("every job done")
                break
    except KeyboardInterrupt:
        print("\nstopping")

    print(f"done: tick={tick} distance={robot.distance:.1f} tasks_done={robot.tasks_done} "
          f"wait_ticks={robot.wait_ticks} deadlocks={robot.deadlock_events}")
    agent.close()
    return 0


def run_network(args: argparse.Namespace) -> int:
    radio = UdpRadio(
        args.id,
        peers=parse_pairs(args.peers),
        listen=args.listen,
        drop_prob=args.drop,
        seed=args.seed,
    )
    radio.set_host(args.host)
    agent = EdgeAgent(args.id, None, radio, priority=args.priority, policy=args.policy)
    print(f"agent {args.id} listening on {radio.bound_address()}  host={args.host[0]}:{args.host[1]}")

    period = 1.0 / max(1, args.tick_hz)
    hello_every = max(1, args.tick_hz)  # keep handshaking once a second until adopted
    tick = 0
    try:
        while tick < args.max_ticks and agent.running:
            started = time.perf_counter()
            agent.pump()
            if agent.robot is None:
                if tick % hello_every == 0:
                    agent.send_to_host(agent.hello(tick))
                if tick % HEARTBEAT_EVERY == 0:
                    print(f"tick {tick:4d} awaiting HELLO_WORLD from the host")
            else:
                tick = max(tick, agent.tick)
                agent.tick_once(tick)  # decide, then unicast ACT to the host
                if tick % HEARTBEAT_EVERY == 0:
                    agent.send_heartbeat(tick)
                    robot = agent.robot
                    print(f"tick {tick:4d} pos={tuple(robot.pos)} phase={robot.phase} "
                          f"acts={agent.acts_sent} ctrl={agent.control_frames}")
            tick += 1
            time.sleep(max(0.0, period - (time.perf_counter() - started)))
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        agent.close()
    print(f"done: tick={tick} acts_sent={agent.acts_sent} control_frames={agent.control_frames} "
          f"malformed={agent.malformed} unknown={agent.unknown_frames}")
    return 0


def _addr(raw: str, default_port: int) -> tuple[str, int]:
    host, _, port = raw.partition(":")
    return (host or "0.0.0.0", int(port or default_port))


def main() -> int:
    p = argparse.ArgumentParser(description="SIH26123 edge agent (one robot)")
    p.add_argument("--id", default="R1")
    p.add_argument("--offline", action="store_true", help="run with a local Warehouse and no host")
    p.add_argument("--host", default="127.0.0.1:9100", help="host address host:port")
    p.add_argument("--listen", default="0.0.0.0:9201", help="agent bind address host:port")
    p.add_argument("--peers", default="", help="R2=host:port,R3=host:port (direct peer table)")
    p.add_argument("--policy", choices=("ubpa", "stop_wait"), default="ubpa")
    p.add_argument("--priority", type=int, default=1)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--env", choices=tuple(map_keys()), default="warehouse")
    p.add_argument("--drop", type=float, default=0.0)
    p.add_argument("--tick-hz", type=int, default=TICK_HZ)
    p.add_argument("--max-ticks", type=int, default=MAX_TICKS_DEMO)
    args = p.parse_args()

    args.host = _addr(args.host, 9100)
    args.listen = _addr(args.listen, 9201)
    if args.offline:
        return run_offline(args)
    return run_network(args)


if __name__ == "__main__":
    raise SystemExit(main())
