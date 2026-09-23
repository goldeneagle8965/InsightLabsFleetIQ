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

Network mode is the Pi path: run one of these per robot, on the robot's own
machine (or on three terminals of one laptop to rehearse it). ``HELLO`` is both
the handshake and the keepalive, the host answers it with ``HELLO_WORLD``
(geometry plus the peer addresses it has pinned), and from then on this process
decides once per ``OBS`` and unicasts its own ``ACT``. Peer traffic goes straight
between robots: each robot learns where its peers are from the roster the host
published and unicasts to them directly.

One ``summary {json}`` line is printed on exit, which is what the LAN integration
test reads back.

Stdlib only.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.config import MAX_TICKS_DEMO, TICK_HZ, CoordinationFlags  # noqa: E402
from simulation.edge import EdgeAgent, LocalWorldView, LoopbackRadio, SharedMedium  # noqa: E402
from simulation.edge.agent import HEARTBEAT_TICKS, process_resources  # noqa: E402
from simulation.edge.radio import RadioPortInUse, UdpRadio  # noqa: E402
from simulation.maps import get_map, map_keys  # noqa: E402
from simulation.peer import MSG_TASK_ANNOUNCE  # noqa: E402

HEARTBEAT_EVERY = HEARTBEAT_TICKS
#: Process start, used to report the run's own process uptime next to its CPU/RAM
#: snapshot. Module level so ``--summary-json`` reports the whole process lifetime,
#: not just the part spent inside ``run_network``.
PROCESS_START = time.perf_counter()
#: How often to retry ``HELLO`` while the host has not answered yet.
HELLO_RETRY_S = 0.5
#: Keepalive cadence once adopted (8 host ticks at the demo rate, doc 05 §4).
HELLO_REFRESH_S = 1.0
#: Socket poll period. The host's ``OBS`` sets the pace; this only decides how
#: quickly that ``OBS`` is noticed, so it stays small and cheap.
POLL_S = 0.002
#: How long an *adopted* agent waits in silence for the host before exiting. The
#: host answers every tick, so this only ever fires when the host process is gone;
#: an agent that kept looping would sit there holding its port and confuse the next
#: run (doc 04 §3.3).
HOST_TIMEOUT_S = 15.0
#: How long an *unadopted* agent keeps announcing itself before giving up. The host
#: answers ``HELLO`` with ``HELLO_WORLD`` in milliseconds when it is there at all, so
#: waiting longer than this only happens when ``--host`` points at nothing. Without a
#: bound the process would hold its port for the whole tick budget (8 minutes at the
#: demo rate), which is what leaves stray agents behind between runs. ``0`` waits
#: forever, for the deploy where the robot really does boot before its host.
ADOPT_TIMEOUT_S = 60.0


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
    """Talk to a LAN host: handshake, adopt geometry, decide, repeat.

    The host owns the clock and the physics; this process owns the robot. Exactly
    one decision is taken per ``OBS`` the host sent, and the ``ACT`` that comes out of
    it is the robot's own intention. Nothing here waits for an instruction.
    """
    try:
        radio = UdpRadio(
            args.id,
            peers=parse_pairs(args.peers),
            listen=args.listen,
            drop_prob=args.drop,
            seed=args.seed,
        )
    except RadioPortInUse as exc:
        # The port is this robot's identity on the LAN, so sharing it is never the
        # right answer: whichever process lost the race would silently miss frames.
        print(f"agent {args.id}: {exc}", file=sys.stderr, flush=True)
        print(
            f"hint: give each robot id its own --listen port "
            f"(this one wanted {args.listen[0]}:{args.listen[1]})",
            file=sys.stderr,
            flush=True,
        )
        return 2
    radio.set_host(args.host)
    agent = EdgeAgent(args.id, None, radio, priority=args.priority, policy=args.policy)
    print(
        f"agent {args.id} listening on {radio.bound_address()}  host={args.host[0]}:{args.host[1]}",
        flush=True,
    )

    tick = 0
    decided = -1
    next_hello = 0.0
    exit_code = 0
    # Bounded even if the host never answers: an unadopted robot is invisible to the
    # host, so ``--host-timeout`` cannot cover it and it would otherwise spin holding
    # its port for the whole tick budget. ``--adopt-timeout 0`` waits indefinitely,
    # for the deployment where a robot really does boot before its host.
    adopt_deadline = (
        None if args.adopt_timeout <= 0 else time.perf_counter() + args.adopt_timeout
    )
    try:
        while agent.running and tick < args.max_ticks:
            agent.pump()  # adopt HELLO_WORLD / OBS / ACT_RESULT
            now = time.perf_counter()
            if not agent.ready and adopt_deadline is not None and now > adopt_deadline:
                print(
                    f"no HELLO_WORLD from {args.host[0]}:{args.host[1]} in "
                    f"{args.adopt_timeout:g}s; exiting "
                    f"(check --host, or use --adopt-timeout 0 to wait for it)",
                    file=sys.stderr,
                    flush=True,
                )
                exit_code = 3
                break
            if agent.ready and agent.host_silence_s() > args.host_timeout:
                # The host owned the clock; with it gone there is no tick to act on.
                # Exit rather than loop forever holding a port nobody is using.
                print(
                    f"host silent for {agent.host_silence_s():.1f}s; exiting",
                    file=sys.stderr,
                    flush=True,
                )
                break
            if now >= next_hello:
                # HELLO is the handshake *and* the keepalive. The host answers the
                # first one with HELLO_WORLD and uses the later ones to keep this
                # robot's liveness current; it is never a request for orders.
                agent.send_to_host(agent.hello(agent.tick))
                next_hello = now + (HELLO_REFRESH_S if agent.ready else HELLO_RETRY_S)
            # No OBS, no decision: ``last_seen_tick`` stays -1 until the host has
            # actually asked this robot something, so adopting the geometry is not
            # by itself a reason to publish an intention for a tick nobody asked for.
            if agent.ready and agent.last_seen_tick >= 0 and agent.tick != decided:
                decided = agent.tick
                tick = max(tick, agent.tick)
                agent.tick_once(agent.tick)  # decide, then unicast the ACT
                if agent.tick % HEARTBEAT_EVERY == 0:
                    agent.send_heartbeat(agent.tick)
                    robot = agent.robot
                    print(
                        f"tick {tick:4d} pos={tuple(robot.pos)} phase={robot.phase} "
                        f"acts={agent.acts_sent} peers={sorted(agent.peer_addrs)}",
                        flush=True,
                    )
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        # Read the counters while the socket is still open: diagnostics must not be
        # lost just because the shutdown path got there first.
        summary = _summary(args, agent)
        agent.close()

    print("summary " + json.dumps(summary, sort_keys=True), flush=True)
    # Same dict, on disk: the one-command hardware summary (scripts/edge_hw_summary.py)
    # merges one file per node. Writing it here keeps the stdout contract untouched.
    if args.summary_json:
        _write_summary(args.summary_json, summary)
    return exit_code


def _write_summary(path: str, summary: dict[str, Any]) -> None:
    """Dump ``summary`` to ``path``, creating parent directories as needed.

    A failure here must never change the run's exit status: the JSON on stdout is
    the contract, the file is a convenience for the hardware benchmark collector.
    """
    try:
        out = Path(path)
        if out.parent and not out.parent.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"could not write --summary-json {path}: {exc}", file=sys.stderr, flush=True)


def _summary(args: argparse.Namespace, agent: EdgeAgent) -> dict[str, Any]:
    """Everything the run should leave behind about this robot, as JSON."""
    robot = agent.robot
    return {
        "id": args.id,
        "role": "robot",
        "agent_version": agent.agent_version,
        "adopted": agent.ready,
        "tick": agent.tick,
        "acts_sent": agent.acts_sent,
        "acts_missing": agent.acts_missing,
        "control_frames": agent.control_frames,
        "malformed": agent.malformed,
        "unknown_frames": agent.unknown_frames,
        "hello_sent": agent.hello_sent,
        "peers_known": sorted(agent.peer_addrs),
        # Heard directly, robot to robot: proof the peer channel is not the host.
        "peers_observed": sorted(robot.peers) if robot is not None else [],
        "tasks_done": int(robot.tasks_done) if robot is not None else 0,
        "distance": round(float(robot.distance), 2) if robot is not None else 0.0,
        "step_ms_p95": round(agent.step_ms_p95, 4),
        # How long the host had been quiet when this robot stopped. Zero-ish for a
        # normal SHUTDOWN; it is what tells a silent-host exit apart from a crash.
        "host_silence_s": round(agent.host_silence_s(), 2),
        "radio": agent.telemetry()["radio"],
        # CPU/RAM of *this process*, measured with stdlib facilities only. On a host
        # without ``resource`` or ``/proc/self`` (e.g. Windows) this reports
        # ``available: false`` and a reason - never fabricated zeros.
        "process_uptime_s": round(time.perf_counter() - PROCESS_START, 3),
        "resources": process_resources(time.perf_counter() - PROCESS_START),
    }


def _addr(raw: str, default_port: int) -> tuple[str, int]:
    host, _, port = raw.partition(":")
    return (host or "0.0.0.0", int(port or default_port))


def main() -> int:
    p = argparse.ArgumentParser(description="SIH26123 edge agent (one robot)")
    p.add_argument("--id", default="R1")
    p.add_argument("--offline", action="store_true", help="run with a local Warehouse and no host")
    p.add_argument("--host", default="127.0.0.1:9100", help="host address host:port")
    p.add_argument("--listen", default="0.0.0.0:9201", help="agent bind address host:port")
    p.add_argument(
        "--peers",
        default="",
        help="R2=host:port,R3=host:port (optional: a LAN host also publishes its roster)",
    )
    p.add_argument("--policy", choices=("ubpa", "stop_wait"), default="ubpa")
    p.add_argument("--priority", type=int, default=1)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--env", choices=tuple(map_keys()), default="warehouse")
    p.add_argument("--drop", type=float, default=0.0)
    p.add_argument("--tick-hz", type=int, default=TICK_HZ)
    p.add_argument("--max-ticks", type=int, default=MAX_TICKS_DEMO)
    p.add_argument(
        "--summary-json",
        default=None,
        metavar="PATH",
        help="also write the exit summary to PATH (feeds scripts/edge_hw_summary.py)",
    )
    p.add_argument(
        "--host-timeout",
        type=float,
        default=HOST_TIMEOUT_S,
        help="seconds of host silence before an adopted agent exits",
    )
    p.add_argument(
        "--adopt-timeout",
        type=float,
        default=ADOPT_TIMEOUT_S,
        help="seconds to wait for HELLO_WORLD before giving up (0 waits forever)",
    )
    args = p.parse_args()

    args.host = _addr(args.host, 9100)
    args.listen = _addr(args.listen, 9201)
    if args.offline:
        return run_offline(args)
    return run_network(args)


if __name__ == "__main__":
    raise SystemExit(main())
