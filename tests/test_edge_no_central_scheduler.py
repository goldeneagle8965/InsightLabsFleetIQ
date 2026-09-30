"""T7/T8 - neither the host nor the dashboard may make a fleet decision.

("directly, through the peer
broadcast", not "through a scheduler") is a static one: the host has no vocabulary
and no writable attributes with which to choose a winner, order a cost, or route a
robot. ``TestHostHasNoDecisionVocabulary`` proves that by reading the source of
``simulation/edge/host.py`` and ``simulation/dashboard.py`` as an AST and rejecting
the decision-making constructs. ``TestOperatorSurface`` then proves the same from
the outside: hostile actions are refused, spectator data has no effect, and a robot
whose ACT the host never received keeps deciding on its own.

Run: python -m unittest tests.test_edge_no_central_scheduler
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.config import MAX_TICKS_DEMO  # noqa: E402
from simulation.dashboard import Dashboard  # noqa: E402
from simulation.edge import EdgeHost  # noqa: E402
from simulation.edge.host import DEAD_TICKS  # noqa: E402
from simulation.engine import FleetSim  # noqa: E402

HOST_FILE = ROOT / "simulation" / "edge" / "host.py"
DASH_FILE = ROOT / "simulation" / "dashboard.py"

#: Identifiers that only exist to run a central decision. None may appear as a
#: name, attribute, function, class or parameter anywhere in the host or the web
#: layer. (Docstrings and comments are exempt: they are documentation, not code,
#: and host.py names these constructs only to say it does not have them.)
FORBIDDEN_IDENTIFIERS = frozenset(
    {
        "FleetScheduler",
        "Scheduler",
        "Orchestrator",
        "ubpa_score",
        "UBPA_U",
        "UBPA_B",
        "UBPA_P",
        "UBPA_A",
        "_winner",
        "choose_winner",
        "resolve_deadlock",
        "assign_task",
        "winner_for",
        "best_robot_for",
        # route planning: the host must never grow a pathfinder
        "planner",
        "_plan",
        "plan_route",
        "_choose_move",
        "_choose_goal",
        "best_bidder",
    }
)

#: Robot attributes that hold a decision. The host may read them into a
#: spectator view; it may never write them.
ROBOT_DECISION_ATTRS = frozenset(
    {
        "goal",
        "current_task",
        "phase",
        "planned_path",
        "policy",
        "resume_goal",
        "resume_phase",
        "yield_target",
        "neg",
        "next_step",
        "stall_task",
    }
)

#: Peer decision types. A host that originates any of these has become the broker.
PEER_DECISION_TYPES = frozenset(
    {"BID", "CLAIM", "RELEASE", "YIELD_QUERY", "YIELD_ACK", "YIELD_NACK"}
)

#: The only messages the host may put on the wire. HELLO / ACT / HEARTBEAT are
#: agent-originated; TASK_ANNOUNCE is the WMS bulletin (a broadcast, not an order).
HOST_EMISSION_TYPES = frozenset({"HELLO_WORLD", "OBS", "ACT_RESULT", "SHUTDOWN", "TASK_ANNOUNCE"})

#: Construction-time helpers that may seat robots at their spawn positions. No
#: other function may write ``world.robots``.
SPAWN_FUNCS = frozenset({"reset", "_build_loopback", "_build_udp"})


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def enclosing_functions(tree: ast.Module) -> dict[ast.AST, str]:
    """Map every node to the innermost ``def`` that contains it."""
    owner: dict[ast.AST, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            nxt = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else current
            owner[child] = nxt
            visit(child, nxt)

    owner[tree] = ""
    visit(tree, "")
    return owner


class TestHostHasNoDecisionVocabulary(unittest.TestCase):
    """T7 - a static reading of the host and the web layer."""

    def setUp(self) -> None:
        self.host_tree = parse(HOST_FILE)
        self.dash_tree = parse(DASH_FILE)

    def _trees(self):
        return {"host.py": self.host_tree, "dashboard.py": self.dash_tree}

    def test_no_decision_identifiers(self) -> None:
        for label, tree in self._trees().items():
            found: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = node.name
                elif isinstance(node, ast.arg):
                    name = node.arg
                else:
                    continue
                if name in FORBIDDEN_IDENTIFIERS:
                    found.append(f"{label}:{getattr(node, 'lineno', '?')} {name}")
            self.assertEqual(found, [], f"decision vocabulary present: {found}")

    def test_no_writes_to_robot_decision_attributes(self) -> None:
        """``robot.goal = ...`` (etc.) may never appear in either file.

        The base expression is inspected because ``sim.policy = "ubpa"`` in the
        dashboard is an operator knob that recreates the fleet, not a live steer.
        """
        violations: list[str] = []
        for label, tree in self._trees().items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                if not isinstance(node.ctx, ast.Store) or node.attr not in ROBOT_DECISION_ATTRS:
                    continue
                base = ast.unparse(node.value)
                if "robot" in base or "bot" in base or "agents[" in base:
                    violations.append(f"{label}:{node.lineno} {base}.{node.attr} = ...")
        self.assertEqual(violations, [], f"the host wrote a robot decision: {violations}")

    def test_apply_moves_only_inside_apply(self) -> None:
        """The one mutator of ``world.robots`` is reachable from one place."""
        owner = enclosing_functions(self.host_tree)
        calls: list[str] = []
        for node in ast.walk(self.host_tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "apply_moves":
                    calls.append(owner.get(node, ""))
        self.assertTrue(calls, "no apply_moves call found; the guard is stale")
        self.assertEqual(set(calls), {"apply"}, f"apply_moves called from {set(calls)}")

    def test_world_robots_written_only_at_spawn(self) -> None:
        owner = enclosing_functions(self.host_tree)
        writes: list[str] = []
        for node in ast.walk(self.host_tree):
            if not isinstance(node, ast.Subscript) or not isinstance(node.ctx, ast.Store):
                continue
            target = ast.unparse(node.value)
            if target.endswith("world.robots"):
                writes.append(f"{owner.get(node, '?')} -> {target}[...]")
        funcs = {w.split(" ")[0] for w in writes}
        self.assertTrue(writes, "no world.robots write found; the guard is stale")
        self.assertLessEqual(funcs, SPAWN_FUNCS, f"robots moved outside construction: {writes}")

    def test_no_cost_ordering(self) -> None:
        """No min/max/sorted over bids, costs or UBPA scores."""
        offenders: list[str] = []
        for label, tree in self._trees().items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not isinstance(node.func, ast.Name) or node.func.id not in {"min", "max", "sorted"}:
                    continue
                blob = " ".join(ast.unparse(a) for a in node.args)
                if any(word in blob for word in ("bid", "cost", "ubpa", "auction", "score")):
                    offenders.append(f"{label}:{node.lineno} {blob}")
        self.assertEqual(offenders, [], f"the host ordered robots by cost: {offenders}")

    def test_no_assignment_call(self) -> None:
        """There is no ``Warehouse.assign`` (or any ``.assign``) for a scheduler."""
        offenders: list[str] = []
        for label, tree in self._trees().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in {"assign", "assign_task", "dispatch"}:
                        offenders.append(f"{label}:{node.lineno} {ast.unparse(node.func)}")
        self.assertEqual(offenders, [], f"an assignment call exists: {offenders}")

    def test_host_head_args_are_not_peer_decisions(self) -> None:
        """The host builds no message whose type is a peer decision.

        The first argument to ``head`` is usually the imported constant
        ``CTRL_OBS`` rather than a literal, so the symbolic name is normalised
        (``CTRL_OBS`` -> ``OBS``) before it is compared against the allow-list.
        """
        raw: set[str] = set()
        for node in ast.walk(self.host_tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "head":
                if node.args:
                    raw.add(ast.unparse(node.args[0]).strip("'\""))
        self.assertTrue(raw, "no head() call found; the guard is stale")

        built = {token[5:] if token.startswith("CTRL_") else token for token in raw}
        self.assertFalse(built & PEER_DECISION_TYPES, f"host originates peer decisions: {built}")
        self.assertLessEqual(built, HOST_EMISSION_TYPES, f"unexpected host emission types: {built}")


class TestOperatorSurface(unittest.TestCase):
    """T8 - from the outside: the API cannot steer, only observe."""

    def _decision_state(self, sim: FleetSim) -> dict[str, tuple]:
        return {
            bot.id: (bot.goal, bot.current_task, bot.phase, tuple(bot.planned_path), bot.policy)
            for bot in sim.robots
        }

    def test_dashboard_refuses_decision_actions(self) -> None:
        dash = Dashboard(FleetSim(n_robots=3, seed=1))
        for _ in range(25):
            dash.sim.step()
        dash.sim.paused = True

        before = self._decision_state(dash.sim)
        for action in ("set_winner", "assign_task", "resolve_deadlock", "force_route",
                       "choose_bidder", "yield_now", "grant_task", "override_path"):
            out = dash.handle_action({"action": action})
            self.assertIsInstance(out, dict, f"{action} did not return a dict")
            self.assertFalse(out.get("ok"), f"dashboard accepted decision action {action!r}")
        after = self._decision_state(dash.sim)
        self.assertEqual(before, after, "a refused action still moved a robot's decision")

    def test_every_existing_operator_action_is_a_knob_not_a_decision(self) -> None:
        """Every legal action returns a verdict and never crashes the server."""
        dash = Dashboard(FleetSim(n_robots=3, seed=1))
        world = dash.sim.world
        cells = [(x, y) for y in range(world.height) for x in range(world.width) if world.is_free((x, y))]
        pick, drop = cells[0], cells[1]

        bodies = [
            {"action": "pause"},
            {"action": "resume"},
            {"action": "toggle_pause"},
            {"action": "step"},
            {"action": "block_aisle"},
            {"action": "toggle", "x": pick[0], "y": pick[1]},
            {"action": "clear_blocks"},
            {"action": "policy", "policy": "ubpa"},
            {"action": "radio", "drop_prob": 0.2, "delay": 1},
            {"action": "speed", "speed": 2.0},
            {"action": "env", "env": "warehouse"},
            {"action": "robots", "n": 3},
            {"action": "add_task", "pick": list(pick), "drop": list(drop)},
            {"action": "remove_task", "task_id": "NOPE"},
            {"action": "drain", "robot_id": "R1"},
            {"action": "forecast", "horizon": 50},
        ]
        for body in bodies:
            out = dash.handle_action(body)
            self.assertIsInstance(out, dict, f"{body['action']} returned {type(out).__name__}")
            self.assertIn("ok", out, f"{body['action']} returned no verdict")

    def test_spectator_view_is_inert(self) -> None:
        """Corrupting the read-only projections cannot change a decision (T8.2)."""
        a = EdgeHost(n_robots=3, seed=1)
        b = EdgeHost(n_robots=3, seed=1)
        for _ in range(40):
            a.step()
            b.step()

        snap = a.snapshot()
        snap["auctions"].insert(0, {
            "task_id": "JFAKE", "viewer": "R1", "opened_tick": 0, "closed": False,
            "bids": 9, "valid_bids": 9, "best_bidder": "R2", "best_cost": 0.0,
        })
        snap["robots"][0]["task"] = "JFAKE"
        snap["robots"][0]["pos"] = [0, 0]
        snap["metrics"]["tasks_done"] = 999
        snap["agents"][0]["state"] = "dead"

        a.step()
        b.step()

        def decisions(host: EdgeHost) -> dict[str, tuple]:
            out = {}
            for rid in host.order:
                robot = host.agents[rid].robot
                out[rid] = (robot.goal, robot.current_task, robot.phase, tuple(robot.planned_path))
            return out

        self.assertEqual(decisions(a), decisions(b), "tampered spectator data changed a decision")

    def test_host_never_originates_a_peer_decision(self) -> None:
        """Instrumented run: the host's own emissions are CTRL/bulletin only (T8.3)."""
        host = EdgeHost(n_robots=3, seed=1)
        emitted: list[str] = []

        real_send = host._send
        medium = host.medium
        real_broadcast = medium.broadcast

        def spy_send(rid: str, msg: dict) -> None:
            emitted.append(str(msg.get("type")))
            real_send(rid, msg)

        def spy_broadcast(sender: str, msg: dict, tick: int):
            if sender == "WMS":  # only host-originated bulletins
                emitted.append(str(msg.get("type")))
            return real_broadcast(sender, msg, tick)

        host._send = spy_send
        medium.broadcast = spy_broadcast
        host.run_until(MAX_TICKS_DEMO)
        host._send = real_send
        medium.broadcast = real_broadcast

        self.assertTrue(emitted, "the host emitted nothing; instrumentation failed")
        self.assertFalse(set(emitted) & PEER_DECISION_TYPES, f"host sent {set(emitted) & PEER_DECISION_TYPES}")
        self.assertLessEqual(set(emitted), HOST_EMISSION_TYPES, f"unexpected emissions: {set(emitted)}")

    def test_host_does_not_take_over_a_robot_whose_act_was_lost(self) -> None:
        """T8.4 - a lost ACT starves nothing: the robot keeps deciding, the host waits."""
        host = EdgeHost(n_robots=3, seed=1)
        host.run_until(30)
        agent = host.agents["R1"]
        frozen = tuple(agent.robot.pos)
        step_n_before = agent.robot.step_n

        def lost_tick_once(tick: int):
            agent.tick = max(agent.tick, int(tick))
            agent.pump()
            if agent.robot is None:
                agent.install()
            if agent.robot is not None:
                agent.robot.step(agent.tick)  # the robot still decides, on its own
                agent.acts_sent += 1
            return None  # ...but the host never receives the ACT

        real = agent.tick_once
        agent.tick_once = lost_tick_once
        host.run_until(30 + DEAD_TICKS + 5)
        agent.tick_once = real

        self.assertEqual(tuple(agent.robot.pos), frozen, "the host moved a robot that sent no ACT")
        self.assertGreater(agent.robot.step_n, step_n_before, "the robot stopped deciding on its own")
        self.assertEqual(host.agent_state("R1"), "dead")
        self.assertGreater(host.late_acts, 0, "a missing ACT was not counted")

        host.run_until(MAX_TICKS_DEMO)
        self.assertTrue(host.all_done(), "the fleet did not finish once the ACTs resumed")
        self.assertNotEqual(tuple(agent.robot.pos), frozen, "R1 never resumed after the link returned")


if __name__ == "__main__":
    unittest.main(verbosity=2)
