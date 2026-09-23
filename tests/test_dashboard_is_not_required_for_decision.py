"""The dashboard watches the fleet. The fleet does not need the dashboard.

Three claims, each checked against the real code rather than the docstrings:

1. ``TestTheWebLayerIsOutsideTheDecisionStack`` - no module that makes a decision
   imports the HTTP layer, so a decision cannot reach it even by accident. An AST
   scan, plus a positive control proving the scan would have caught it.
2. ``TestTheDashboardCannotSteerTheFleet`` - a host that is snapshotted, paused and
   resumed, and fed every decision-flavoured action on every tick runs *identically*
   to a host nobody ever touched. The refusals are explicit, not silent.
3. ``TestARealServerIsNotRequired`` - an actual ``serve()`` HTTP server is started,
   read over real HTTP, then torn down mid-run. The robots keep deciding while it
   is gone and finish in exactly the state of a control host that never had one.

The distinction being protected here is the one the judges ask about: the edge
robots decide among themselves, so the removal of any single observer must be
invisible to the outcome. If a run needs the server, that is a central scheduler
with extra steps.

Run: ``python -m unittest tests.test_dashboard_is_not_required_for_decision``
"""

from __future__ import annotations

import ast
import http.client
import json
import socket
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from run_edge_host import EdgeSession  # noqa: E402  (the dashboard adapter)

from simulation.edge import EdgeHost  # noqa: E402
from simulation.dashboard import serve  # noqa: E402

#: Everything that participates in choosing what a robot does.
DECISION_MODULES = (
    "simulation/planner.py",
    "simulation/robot.py",
    "simulation/peer.py",
    "simulation/world.py",
    "simulation/metrics.py",
    "simulation/engine.py",
    "simulation/edge/__init__.py",
    "simulation/edge/agent.py",
    "simulation/edge/host.py",
    "simulation/edge/protocol.py",
    "simulation/edge/radio.py",
    "simulation/edge/session.py",
    "simulation/edge/worldview.py",
)

#: Anything that would let code on the decision path be *driven* from a web layer.
WEB_ROOTS = (
    "http",
    "socketserver",
    "flask",
    "websockets",
    "urllib",
    "requests",
    "simulation.dashboard",
    "simulation.web",
)

#: Actions that would move decision authority into the dashboard if they worked.
DECISION_ACTIONS = (
    "set_winner",
    "pick_winner",
    "choose_bidder",
    "grant_task",
    "assign_task",
    "resolve_deadlock",
    "plan_route",
    "force_route",
    "override_path",
    "yield_now",
    "dispatch",
    "schedule",
)

#: Fleet-level knobs the edge host refuses by design (they need a new fleet).
FLEET_ACTIONS = ("policy", "robots", "drain", "block_aisle", "forecast")


def imported_modules(path: Path) -> set[str]:
    """Every module name ``path`` imports, from the AST (no execution)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                found.add(node.module)
            elif node.module:  # pragma: no cover - relative imports are not used here
                found.add(node.module)
    return found


def web_imports(names: set[str]) -> set[str]:
    """The subset of ``names`` that pulls in a web/HTTP layer at any depth."""
    return {
        name
        for name in names
        if any(name == root or name.startswith(root + ".") for root in WEB_ROOTS)
    }


#: Metric fields that measure *wall time* rather than a decision. Two identical
#: runs may legitimately differ in these, so they are excluded from an equality
#: check - and the fact that only this one field moves is itself the evidence that
#: nothing the dashboard did changed a single choice.
WALL_CLOCK_KEYS = ("mean_robot_step_ms",)


def deterministic_metrics(source) -> dict:
    """Metrics minus the wall-clock fields, from a ``RunMetrics`` or a payload."""
    raw = source.metrics().to_dict() if hasattr(source, "metrics") else dict(source)
    return {key: value for key, value in raw.items() if key not in WALL_CLOCK_KEYS}


def decisions(host: EdgeHost) -> dict[str, dict]:
    """What each robot actually decided: goal, task, phase, path, and the books."""
    out: dict[str, dict] = {}
    for rid in host.order:
        robot = host.agents[rid].robot
        if robot is None:  # pragma: no cover - loop-back agents always exist
            out[rid] = {"robot": None}
            continue
        out[rid] = {
            "pos": tuple(robot.pos),
            "phase": robot.phase,
            "current_task": robot.current_task,
            "planned_path": [tuple(p) for p in robot.planned_path],
            "ubpa": round(float(robot.last_ubpa), 9),
            "velocity": tuple(robot.velocity),
            "battery": round(float(robot.battery), 6),
            "wait_ticks": int(robot.wait_ticks),
            "tasks_done": int(robot.tasks_done),
            "step_n": int(robot.step_n),
        }
    return out


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_json(port: int, path: str, body: dict | None = None) -> dict:
    """One real HTTP request, so the server is exercised as a browser would."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        if body is None:
            conn.request("GET", path)
        else:
            payload = json.dumps(body)
            conn.request("POST", path, payload, {"Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read()
        return json.loads(raw.decode("utf-8"))
    finally:
        conn.close()


class TestTheWebLayerIsOutsideTheDecisionStack(unittest.TestCase):
    def test_no_decision_module_imports_the_web_layer(self) -> None:
        offenders: dict[str, set[str]] = {}
        for rel in DECISION_MODULES:
            path = ROOT / rel
            self.assertTrue(path.is_file(), f"{rel} is missing; the list is stale")
            found = web_imports(imported_modules(path))
            if found:
                offenders[rel] = found
        self.assertEqual(offenders, {}, f"the web layer is reachable from a decision: {offenders}")

    def test_the_scan_would_catch_a_web_import(self) -> None:
        """A guard nobody can trip is not a guard: the detector must see a real one."""
        dashboard = ROOT / "simulation/dashboard.py"
        self.assertIn("http.server", imported_modules(dashboard))

    def test_the_only_bridge_is_the_script_that_owns_the_page(self) -> None:
        """``run_edge_host`` presents the host; it is not part of the decision path."""
        bridge = imported_modules(SCRIPTS / "run_edge_host.py")
        self.assertIn("simulation.dashboard", bridge)
        for rel in DECISION_MODULES:
            self.assertNotIn("run_edge_host", imported_modules(ROOT / rel))


class TestTheDashboardCannotSteerTheFleet(unittest.TestCase):
    """A watched run and an untouched run must be the same run."""

    TICKS = 60

    def _pair(self) -> tuple[EdgeHost, EdgeHost, EdgeSession]:
        control = EdgeHost(n_robots=3, seed=1)
        viewed = EdgeHost(n_robots=3, seed=1)
        session = EdgeSession(viewed, env="warehouse", seed=1, n_robots=3, policy="ubpa")
        return control, viewed, session

    def test_polling_and_refusals_change_nothing(self) -> None:
        control, viewed, session = self._pair()
        for tick in range(self.TICKS):
            control.step()
            session.snapshot()  # what the page does every frame
            # Presentation knobs round-trip, so the adapter is genuinely reached.
            self.assertIs(session.handle_action({"action": "toggle_pause"})["ok"], True)
            self.assertIs(session.handle_action({"action": "toggle_pause"})["ok"], True)
            self.assertIs(session.handle_action({"action": "speed", "speed": 4.0})["ok"], True)
            self.assertFalse(viewed.paused, f"tick {tick}: pause must round-trip")
            # Every action that would be a fleet decision is refused, loudly.
            for name in DECISION_ACTIONS:
                out = session.handle_action({"action": name})
                self.assertIs(out["ok"], False, f"{name} was accepted: {out}")
                self.assertIn("unknown action", out["error"])
            for name in FLEET_ACTIONS:
                out = session.handle_action({"action": name})
                self.assertIs(out["ok"], False, f"{name} was accepted: {out}")
                self.assertIn("not offered in edge mode", out["error"])
            viewed.step()

        self.assertEqual(viewed.tick, control.tick, "the clock is the host's, not the page's")
        self.assertGreater(control.tick, 0)
        self.assertEqual(decisions(viewed), decisions(control))
        self.assertEqual(deterministic_metrics(viewed), deterministic_metrics(control))

    def test_a_snapshot_is_a_copy_and_never_a_handle(self) -> None:
        control, viewed, session = self._pair()
        for _ in range(10):
            control.step()
            viewed.step()

        page = session.snapshot()
        # Vandalise the payload a browser was handed. If any of this were a live
        # reference, the robots would feel it on the next tick.
        page["tick"] = -1
        page["metrics"]["tasks_done"] = 999
        page["metrics"]["collision_count"] = 999
        page["robots"][0]["pos"] = [0, 0]
        page["robots"][0]["path"] = [[0, 0]]
        if page["jobs"]:
            page["jobs"][0]["status"] = "DONE"
            page["jobs"][0]["holder"] = "R3"

        for _ in range(10):
            control.step()
            viewed.step()

        self.assertEqual(decisions(viewed), decisions(control))
        self.assertEqual(deterministic_metrics(viewed), deterministic_metrics(control))
        fresh = session.snapshot()
        self.assertNotEqual(fresh["tick"], -1)
        self.assertNotEqual(fresh["metrics"]["tasks_done"], 999)
        self.assertEqual(
            deterministic_metrics(fresh["metrics"]), deterministic_metrics(viewed)
        )


class TestARealServerIsNotRequired(unittest.TestCase):
    """Start the real HTTP server, kill it mid-run, and keep deciding."""

    TICKS_WITH_SERVER = 30
    TICKS_WITHOUT = 30

    def test_the_fleet_survives_the_server_being_pulled(self) -> None:
        control = EdgeHost(n_robots=3, seed=1)
        viewed = EdgeHost(n_robots=3, seed=1)
        session = EdgeSession(viewed, env="warehouse", seed=1, n_robots=3, policy="ubpa")

        port = free_port()
        httpd = serve(session, host="127.0.0.1", port=port)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        try:
            self.assertIs(http_json(port, "/health")["ok"], True)

            for _ in range(self.TICKS_WITH_SERVER):
                control.step()
                viewed.step()

            state = http_json(port, "/state")
            self.assertEqual(state["tick"], self.TICKS_WITH_SERVER)
            self.assertEqual(
                deterministic_metrics(state["metrics"]), deterministic_metrics(control),
                "the page shows the host's own numbers",
            )
            self.assertEqual(state["edge"]["transport"], "loopback")
            refused = http_json(port, "/action", {"action": "set_winner"})
            self.assertIs(refused["ok"], False)
            self.assertIn("unknown action", refused["error"])
            self.assertEqual(viewed.tick, control.tick)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=10.0)
        self.assertFalse(thread.is_alive(), "the server really is gone")

        # With nothing serving the page, the fleet still decides.
        with self.assertRaises(OSError):
            http_json(port, "/state")

        for _ in range(self.TICKS_WITHOUT):
            control.step()
            viewed.step()

        self.assertEqual(viewed.tick, self.TICKS_WITH_SERVER + self.TICKS_WITHOUT)
        self.assertEqual(
            decisions(viewed), decisions(control),
            "a run that needs the dashboard is a central scheduler in disguise",
        )
        self.assertEqual(deterministic_metrics(viewed), deterministic_metrics(control))
        self.assertGreater(control.metrics().tasks_done, 0)

    def test_a_snapshot_request_does_not_advance_the_clock(self) -> None:
        """Reading the page must not be a step. Otherwise the view *is* the driver."""
        host = EdgeHost(n_robots=3, seed=1)
        session = EdgeSession(host, env="warehouse", seed=1, n_robots=3, policy="ubpa")
        before = host.tick
        for _ in range(5):
            session.snapshot()
        self.assertEqual(host.tick, before)
        self.assertEqual(host.metrics().tasks_done, 0)
        for rid in host.order:
            self.assertEqual(host.agents[rid].robot.step_n, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
