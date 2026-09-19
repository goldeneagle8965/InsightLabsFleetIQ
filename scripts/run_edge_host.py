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

Known limitation (stated, not hidden): the host drives its *own* in-process
agents and uses UDP as the transport between them. It does not yet accept agent
processes running on separate machines, so the ``edge-pi`` variant in doc 01 is
a design target rather than something this script performs.

Stdlib only; safe to launch from any working directory.
"""

from __future__ import annotations

import argparse
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
from simulation.engine import FleetSim  # noqa: E402
from simulation.maps import map_keys  # noqa: E402


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


def build_host(args: argparse.Namespace) -> EdgeHost:
    host = EdgeHost(
        n_robots=max(3, args.robots),
        policy=args.policy,
        seed=args.seed,
        env=args.env,
        drop_prob=args.drop,
        radio_delay=args.delay,
        max_ticks=args.max_ticks,
    )
    if args.agents:
        udp_host, _, udp_port = args.udp.partition(":")
        host.transport = "udp"
        host.udp_host = udp_host or "0.0.0.0"
        host.udp_base_port = int(udp_port or 9100)
        host.advertise = args.advertise
        host.reset()
        if host.transport != "udp":
            print("! UDP bind failed; falling back to loop-back", file=sys.stderr)
    return host


def pump(host: EdgeHost, session: EdgeSession, stop: threading.Event, tick_hz: int) -> None:
    period = 1.0 / max(1, tick_hz)
    while not stop.is_set():
        started = time.perf_counter()
        if not host.paused and host.tick < host.max_ticks:
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
    p.add_argument("--agents", default="", help="R1=host:port,... enables UDP transport")
    p.add_argument("--udp", default="0.0.0.0:9100", help="host bind address for UDP mode")
    p.add_argument("--advertise", default="127.0.0.1", help="address agents should send to")
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
    args = p.parse_args()

    host = build_host(args)
    session = EdgeSession(host, env=args.env, seed=args.seed, n_robots=max(3, args.robots), policy=args.policy)
    print(f"edge host: transport={host.transport} agents={','.join(host.order)} "
          f"seed={args.seed} env={args.env} policy={args.policy}")

    stop = threading.Event()
    thread = threading.Thread(target=pump, args=(host, session, stop, args.tick_hz), daemon=True)

    if args.serve:
        srv_host, _, srv_port = args.serve.partition(":")
        httpd = serve(session, host=srv_host or "127.0.0.1", port=int(srv_port or 8765))
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
    host.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
