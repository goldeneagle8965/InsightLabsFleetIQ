"""Run the edge host: one simulated world, robots that choose for themselves.

The host owns physics, the clock and the transport. It never receives bids,
never picks a winner and never plans a route - see
``tests/test_edge_no_central_scheduler.py``.

Transports
----------
``loopback`` (default)
    Robots share an in-process ``SharedMedium``. This is the parity mode: the run
    is bit-identical to ``FleetSim`` for the same seed (test T1).
``udp``
    The same messages travel over real UDP sockets on one machine
    (``edge-lan`` in doc 01 section 7). Useful for rehearsing loss/latency before
    touching hardware.
``lan``
    The robots are separate processes, one per machine or per terminal. The host
    binds exactly the port it was given, waits ``--join-timeout`` for each robot's
    ``HELLO``, and pins the source address of that datagram as the robot's control
    address. A robot that never checks in simply never moves; a robot that goes
    quiet is reported ``dead`` and may rejoin from the same address at any time.
    This is the ``edge-pi`` path of doc 01.

Run three robots against one host:

    python scripts/run_edge_host.py --transport lan --robots 3
    python scripts/run_edge_agent.py --id R1 --host 127.0.0.1:9100 --listen 127.0.0.1:9201
    python scripts/run_edge_agent.py --id R2 --host 127.0.0.1:9100 --listen 127.0.0.1:9202
    python scripts/run_edge_agent.py --id R3 --host 127.0.0.1:9100 --listen 127.0.0.1:9203

Stdlib only; safe to launch from any working directory.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.config import MAX_TICKS_DEMO, TICK_HZ  # noqa: E402
from simulation.dashboard import Dashboard, serve  # noqa: E402
from simulation.edge import EdgeHost  # noqa: E402
from simulation.edge.agent import process_resources  # noqa: E402
from simulation.engine import FleetSim  # noqa: E402
from simulation.maps import map_keys  # noqa: E402

#: Host process start, for the ``process_uptime_s`` field of the summary.
PROCESS_START = time.perf_counter()


class EdgeSession:
    """Presents an ``EdgeHost`` through the dashboard's client contract.

    The web layer owns the presentation-only blocks (``shadow``, ``envs``,
    ``server``, ``measured``, ``remeasure``); the host owns the run. This adapter
    copies the host's core snapshot over a ``Dashboard`` snapshot so the existing
    page renders without forking the UI, and it routes every operator action to
    the *host* - which is a world, not a dispatcher.
    """

    def __init__(self, host: EdgeHost, *, env: str, seed: int, n_robots: int, policy: str) -> None:
        self.host = host
        self._base = Dashboard(FleetSim(n_robots=n_robots, policy=policy, seed=seed, env=env))
        self.last_seen = time.time()
        self.speed = 1.0
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            base = self._base.snapshot()
            base.update(self.host.snapshot())
        self.last_seen = time.time()
        return base

    def handle_action(self, body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action") or "")
        host = self.host
        with self._lock:
            if action == "pause":
                host.paused = True
                return {"ok": True, "paused": True}
            if action == "resume":
                host.paused = False
                return {"ok": True, "paused": False}
            if action == "toggle_pause":
                host.paused = not host.paused
                return {"ok": True, "paused": host.paused}
            if action == "step":
                if not host.paused:
                    return {"ok": False, "error": "pause first, then step"}
                host.step()
                return {"ok": True, "tick": host.tick}
            if action == "reset":
                host.reset()
                return {"ok": True, "tick": host.tick}
            if action == "toggle":
                x, y = int(body.get("x", -1)), int(body.get("y", -1))
                if not host.world.in_bounds((x, y)):
                    return {"ok": False, "error": f"cell {x},{y} is outside the map"}
                return {"ok": True, "blocked": host.toggle_block(x, y), "cell": [x, y]}
            if action == "clear_blocks":
                removed = len(host.world.blocked)
                host.world.blocked.clear()
                return {"ok": True, "cleared": removed}
            if action == "radio":
                return {"ok": True, **host.set_radio(drop_prob=body.get("drop_prob"), delay=body.get("delay"))}
            if action == "speed":
                self.speed = max(0.25, min(8.0, float(body.get("speed", 1.0))))
                return {"ok": True, "speed": self.speed}
            if action == "env":
                key = str(body.get("env") or "")
                if key not in map_keys():
                    return {"ok": False, "error": f"unknown environment {key!r}"}
                host.set_env(key)
                return {"ok": True, "env": host.env}
            if action == "add_task":
                pick = self._cell(body, "pick")
                drop = self._cell(body, "drop")
                for label, cell in (("pick", pick), ("drop", drop)):
                    if not host.world.is_free(cell):
                        return {"ok": False, "error": f"{label} {cell} is inside a rack"}
                task = host.add_task(pick, drop, float(body.get("urgency", 0.6)))
                return {"ok": True, "task": {"task_id": task.task_id, "pick": list(task.pick),
                                             "drop": list(task.drop)}}
            if action == "remove_task":
                task_id = str(body.get("task_id") or "")
                gone = host.remove_task(task_id)
                return {"ok": gone, "task_id": task_id, "error": None if gone else "no such job"}
            if action in ("policy", "robots", "drain", "block_aisle", "forecast"):
                # These need a new fleet or a sim-only feature. Refusing beats
                # silently pretending the knob did something.
                return {"ok": False, "error": f"{action} is not offered in edge mode; restart the host"}
            return {"ok": False, "error": f"unknown action {action!r}"}

    def _cell(self, body: dict[str, Any], key: str) -> tuple[int, int]:
        raw = body.get(key) or []
        return (int(raw[0]), int(raw[1]))


def resolve_transport(args: argparse.Namespace) -> str:
    """``auto`` keeps the historical behaviour: agent addresses given => UDP."""
    if args.transport != "auto":
        return str(args.transport)
    return "udp" if args.agents else "loopback"


def build_host(args: argparse.Namespace) -> EdgeHost:
    transport = resolve_transport(args)
    host = EdgeHost(
        n_robots=max(3, args.robots),
        policy=args.policy,
        seed=args.seed,
        env=args.env,
        drop_prob=args.drop,
        radio_delay=args.delay,
        max_ticks=args.max_ticks,
    )
    if transport in ("udp", "lan"):
        udp_host, _, udp_port = args.udp.partition(":")
        host.transport = transport
        host.udp_host = udp_host or "0.0.0.0"
        host.udp_base_port = int(udp_port or 9100)
        host.advertise = args.advertise
        # LAN mode has no silent fallback: a host that quietly moved to another
        # port would look exactly like three robots failing to connect.
        host.reset()
        if host.transport != transport:
            print("! UDP bind failed; falling back to loop-back", file=sys.stderr)
    return host


def pump(host: EdgeHost, session: EdgeSession, stop: threading.Event, tick_hz: int) -> None:
    period = 1.0 / max(1, tick_hz)
    while not stop.is_set():
        started = time.perf_counter()
        if not host.paused:
            if host.tick >= host.max_ticks:
                # Past the horizon the clock is frozen, so there is nothing left to
                # do but report. (A paused host still waits: that is the operator's.)
                print(f"tick horizon reached ({host.max_ticks}); stopping")
                break
            host.step()
            if host.tick % 40 == 0:
                m = host.metrics()
                print(f"tick {host.tick:4d}  done {m.tasks_done}/{m.tasks_total}  "
                      f"collisions {m.collision_count}  deadlocks {m.deadlock_count}")
            if host.all_done():
                print(f"fleet complete at tick {host.tick}")
                break
        delay = period / max(0.25, session.speed)
        stop.wait(max(0.0, delay - (time.perf_counter() - started)))
    stop.set()


def main() -> int:
    p = argparse.ArgumentParser(description="SIH26123 edge host (world + spectator)")
    # --transport is documented in the module docstring; --lan is the shorthand
    # used by the deployment notes.
    p.add_argument("--agents", default="", help="R1=host:port,... enables UDP transport")
    p.add_argument("--transport", choices=("auto", "loopback", "udp", "lan"), default="auto",
                   help="auto = udp when --agents is given, else loopback")
    p.add_argument("--udp", default="0.0.0.0:9100", help="host bind address for UDP/LAN mode")
    p.add_argument("--advertise", default="127.0.0.1", help="address agents should send to")
    p.add_argument("--join-timeout", type=float, default=10.0,
                   help="LAN mode: seconds to wait for the robots' HELLOs before ticking")
    p.add_argument("--tick-hz", type=int, default=TICK_HZ)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--policy", choices=("ubpa", "stop_wait"), default="ubpa")
    p.add_argument("--robots", type=int, default=3)
    p.add_argument("--env", choices=tuple(map_keys()), default="warehouse")
    p.add_argument("--drop", type=float, default=0.0)
    p.add_argument("--delay", type=int, default=1)
    p.add_argument("--max-ticks", type=int, default=MAX_TICKS_DEMO)
    p.add_argument("--headless", action="store_true", help="run to completion, print metrics, exit")
    p.add_argument("--serve", default="", help="host:port to serve the dashboard, e.g. 127.0.0.1:8765")
    p.add_argument(
        "--summary-json",
        default=None,
        metavar="PATH",
        help="write the end-of-run host summary to PATH (feeds scripts/edge_hw_summary.py)",
    )
    args = p.parse_args()

    try:
        host = build_host(args)
    except OSError as exc:
        print(f"! cannot start the host: {exc}", file=sys.stderr)
        return 2
    session = EdgeSession(host, env=args.env, seed=args.seed, n_robots=max(3, args.robots), policy=args.policy)
    print(f"edge host: transport={host.transport} agents={','.join(host.order)} "
          f"seed={args.seed} env={args.env} policy={args.policy}")
    if host.transport == "lan":
        bound = host.sock.getsockname() if host.sock is not None else (args.udp, 0)
        print(f"waiting up to {args.join_timeout:g}s for HELLO on udp {bound[0]}:{bound[1]} "
              f"(robots: {', '.join(host.order)})")
        joined = host.await_agents(args.join_timeout)
        print(f"admitted {joined}/{len(host.order)} robots before the first tick")
        if joined < len(host.order):
            holding = [rid for rid in host.order if host.agent_state(rid) == "boot"]
            print(f"! no HELLO yet from {', '.join(holding)}; their cells stay put until they call in",
                  file=sys.stderr)

    stop = threading.Event()
    thread = threading.Thread(target=pump, args=(host, session, stop, args.tick_hz), daemon=True)

    if args.serve:
        srv_host, _, srv_port = args.serve.partition(":")
        try:
            httpd = serve(session, host=srv_host or "127.0.0.1", port=int(srv_port or 8765))
        except OSError as exc:
            # A busy dashboard port must not stop the fleet: the decisions do not
            # depend on the server existing.
            print(f"! cannot serve the dashboard: {exc}", file=sys.stderr)
            return 2
        print(f"dashboard: http://{srv_host or '127.0.0.1'}:{srv_port or 8765}/  (Ctrl-C to stop)")
        thread.start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopping")
        finally:
            stop.set()
            httpd.shutdown()
    else:
        thread.start()
        try:
            while thread.is_alive():
                thread.join(timeout=0.5)
        except KeyboardInterrupt:
            stop.set()
            print("\nstopping")

    stop.set()
    thread.join(timeout=2.0)
    m = host.metrics()
    print(f"finished: tick={host.tick} complete={m.complete} "
          f"complete_tick={m.all_tasks_complete_tick} tasks={m.tasks_done}/{m.tasks_total} "
          f"collisions={m.collision_count} deadlocks={m.deadlock_count}")
    print(f"link: {host.link()}")
    # Machine-readable end-of-run record. One line with its own ``host_summary ``
    # prefix, so a log that also carries agent ``summary `` lines stays parseable.
    host_record = _host_summary(host, args, m)
    print("host_summary " + json.dumps(host_record, sort_keys=True), flush=True)
    if args.summary_json:
        _write_summary(args.summary_json, host_record)
    # Say goodbye while the socket is still open: a robot whose host stops normally
    # should exit at once rather than wait out its silence window. A host that dies
    # without this line is covered by the agent's own --host-timeout, which is why
    # the two paths exist separately.
    host.stop()
    host.close()
    return 0


def _host_summary(host: EdgeHost, args: argparse.Namespace, m: Any) -> dict[str, Any]:
    """Fleet-wide end-of-run record: outcomes, transport health, and host CPU/RAM.

    Every number here is read off the live host: nothing is recomputed from
    expectations. ``resources`` is measured with stdlib facilities only and reports
    ``available: false`` with a reason on a platform that cannot supply them.
    """
    return {
        "role": "host",
        "transport": host.transport,
        "tick": host.tick,
        "max_ticks": host.max_ticks,
        "policy": args.policy,
        "seed": args.seed,
        "env": args.env,
        "robots": len(host.order),
        "expected_robots": len(host.order),
        "admitted": int(host.admissions),
        "dashboard": bool(args.serve),
        "full": bool(m.complete),
        "complete_tick": m.all_tasks_complete_tick,
        "tasks_done": m.tasks_done,
        "tasks_total": m.tasks_total,
        "collisions": m.collision_count,
        "deadlocks": m.deadlock_count,
        "stalls": m.stall_count,
        "metrics": m.to_dict(),
        "link": host.link(),
        "process_uptime_s": round(time.perf_counter() - PROCESS_START, 3),
        "resources": process_resources(time.perf_counter() - PROCESS_START),
    }


def _write_summary(path: str, record: dict[str, Any]) -> None:
    """Best-effort write of ``record``; never changes the run's exit status."""
    try:
        out = Path(path)
        if out.parent and not out.parent.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"could not write --summary-json {path}: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
