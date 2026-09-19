"""Frontend/API contract checks for the live dashboard.

The browser client reads a precise set of snapshot fields. These tests drive real
simulations in-process and assert that every field the client touches is present and of
the type the renderer assumes, so a live page cannot hit "undefined is not a function"
in front of a judge.

They also replay every operator action the UI can POST, so an action that silently stops
working (or starts raising) fails the suite instead of the demo.

Run: python -m unittest tests.test_dashboard_contract
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.dashboard import Dashboard  # noqa: E402
from simulation.maps import map_keys  # noqa: E402

HEADLINE_KEYS = (
    "all_tasks_complete_tick",
    "deadlock_count",
    "stall_count",
    "stall_handoff_count",
    "collision_count",
    "mean_robot_step_ms",
)

FORECAST_KEYS = (
    "from_tick",
    "horizon_ticks",
    "finish_tick",
    "eta_ticks",
    "eta_seconds",
    "finished",
    "tasks_done",
    "tasks_total",
    "collisions",
    "deadlocks",
    "compute_ms",
)


def is_cell(v) -> bool:
    return isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(c, int) for c in v)


def num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def num_or_none(v) -> bool:
    return v is None or num(v)


def check_snapshot(s: dict, where: str, problems: list[str]) -> None:
    """Append a message per contract violation. Mirrors the client's assumptions."""

    def ok(cond: bool, msg: str) -> None:
        if not cond:
            problems.append(msg)

    for k in (
        "tick", "hz", "policy", "paused", "complete", "speed", "n_robots", "env",
        "map", "jobs", "auctions", "charger", "world", "robots", "radio", "events",
        "metrics", "radio_stats", "shadow", "speed_steps", "envs", "server",
        "measured", "remeasure",
    ):
        ok(k in s, f"{where}: snapshot missing '{k}'")

    ok(num(s["tick"]), f"{where}: tick not numeric")
    ok(isinstance(s["policy"], str), f"{where}: policy not str")
    ok(isinstance(s["paused"], bool) and isinstance(s["complete"], bool), f"{where}: paused/complete not bool")
    ok(num(s["speed"]) and num(s["hz"]), f"{where}: speed/hz not numeric")
    ok(isinstance(s["n_robots"], int), f"{where}: n_robots not int")
    ok(isinstance(s["map"], dict) and isinstance(s["map"].get("label"), str), f"{where}: map.label missing")

    w = s["world"]
    for k in ("width", "height", "walls", "blocked", "turnouts", "depot", "charger", "tasks"):
        ok(k in w, f"{where}: world.{k} missing")
    ok(isinstance(w["width"], int) and isinstance(w["height"], int), f"{where}: world size not int")
    for k in ("walls", "blocked", "turnouts"):
        ok(isinstance(w[k], list) and all(is_cell(c) for c in w[k]), f"{where}: world.{k} not list of cells")
    ok(is_cell(w["depot"]) and is_cell(w["charger"]), f"{where}: depot/charger not cells")

    ok(len(s["robots"]) == s["n_robots"], f"{where}: robots length != n_robots")
    for b in s["robots"]:
        ok(isinstance(b["id"], str), f"{where}: robot id not str")
        ok(is_cell(b["pos"]), f"{where}: robot {b['id']} pos not a cell")
        ok(num(b["battery"]), f"{where}: robot {b['id']} battery not numeric")
        ok(isinstance(b["status"], str), f"{where}: robot {b['id']} status not str")
        ok(b["task"] is None or isinstance(b["task"], str), f"{where}: robot {b['id']} task not str/None")
        ok(num_or_none(b["urgency"]) and num(b["priority"]) and num(b["workload"]),
           f"{where}: robot {b['id']} numbers bad")
        ok(isinstance(b["path"], list) and all(is_cell(p) for p in b["path"]),
           f"{where}: robot {b['id']} path not cells")
        ok(num(b["ubpa"]), f"{where}: robot {b['id']} ubpa not numeric")

    for j in s["jobs"]:
        ok(isinstance(j["task_id"], str), f"{where}: job id not str")
        ok(is_cell(j["pick"]) and is_cell(j["drop"]), f"{where}: job {j['task_id']} pick/drop bad")
        ok(num(j["urgency"]), f"{where}: job {j['task_id']} urgency not numeric")
        ok(isinstance(j["status"], str), f"{where}: job {j['task_id']} status not str (client lowercases it)")
        ok(j["holder"] is None or isinstance(j["holder"], str), f"{where}: job holder not str/None")
        ok(j["complete_tick"] is None or num(j["complete_tick"]), f"{where}: job complete_tick bad")

    ok(is_cell(s["charger"]["cell"]), f"{where}: charger.cell bad")
    ok(isinstance(s["charger"]["charging"], list), f"{where}: charger.charging not list")

    for a in s["auctions"]:
        for k in ("task_id", "viewer", "opened_tick", "closed", "bids", "valid_bids", "best_bidder", "best_cost"):
            ok(k in a, f"{where}: auction missing {k}")
        ok(isinstance(a["closed"], bool), f"{where}: auction.closed not bool")
        ok(num(a["bids"]) and num(a["valid_bids"]), f"{where}: auction bid counts not numeric")
        ok(a["best_bidder"] is None or isinstance(a["best_bidder"], str), f"{where}: best_bidder bad")
        ok(num_or_none(a["best_cost"]), f"{where}: best_cost bad")

    for m in s["radio"]:
        for k in ("tick", "from_id", "type", "task_id", "status"):
            ok(k in m, f"{where}: radio entry missing {k}")
        ok(num(m["tick"]), f"{where}: radio tick not numeric")
        ok(isinstance(m["from_id"], str), f"{where}: radio from_id not str")
        ok(m["type"] is None or isinstance(m["type"], str), f"{where}: radio type bad")

    for e in s["events"]:
        for k in ("tick", "robot", "kind", "detail"):
            ok(k in e, f"{where}: event missing {k}")
        ok(isinstance(e["kind"], str) and isinstance(e["robot"], str), f"{where}: event kind/robot not str")

    for k in (
        "collision_count", "deadlock_count", "stall_count", "stall_handoff_count",
        "all_tasks_complete_tick", "mean_wait_ticks", "total_distance",
        "mean_comm_latency_ticks", "mean_robot_step_ms", "tasks_done", "tasks_total",
        "complete", "ticks",
    ):
        ok(k in s["metrics"], f"{where}: metrics missing {k}")
    ok(num(s["metrics"]["tasks_done"]) and num(s["metrics"]["tasks_total"]), f"{where}: tasks counts bad")
    ok(num_or_none(s["metrics"]["all_tasks_complete_tick"]), f"{where}: finish tick bad")
    ok(num(s["metrics"]["mean_robot_step_ms"]), f"{where}: step ms bad")

    rs = s["radio_stats"]
    for k in ("drop_prob", "delay", "dropped", "delivered", "mean_latency"):
        ok(k in rs and num(rs[k]), f"{where}: radio_stats.{k} bad")

    sh = s["shadow"]
    ok(sh is None or isinstance(sh, dict), f"{where}: shadow not dict/None")
    if isinstance(sh, dict) and "error" not in sh:
        for k in ("policy", "tick", "lag_ticks", "tasks_done", "tasks_total", "complete",
                  "finish_tick", "collision_count", "deadlock_count", "stall_count", "mean_wait_ticks"):
            ok(k in sh, f"{where}: shadow missing {k}")
        ok(num(sh["lag_ticks"]), f"{where}: shadow lag not numeric")
        ok(num_or_none(sh["finish_tick"]), f"{where}: shadow finish bad")

    ok(isinstance(s["envs"], list) and s["envs"] and all(isinstance(e.get("key"), str) for e in s["envs"]),
       f"{where}: envs bad")
    ok(isinstance(s["speed_steps"], list) and all(num(v) for v in s["speed_steps"]), f"{where}: speed_steps bad")
    for k in ("session", "uptime_s", "ticks_served"):
        ok(k in s["server"], f"{where}: server missing {k}")

    md = s["measured"]
    ok(isinstance(md.get("provenance"), str), f"{where}: measured.provenance not str")
    ok(isinstance(md.get("source_mtime"), str), f"{where}: measured.source_mtime not str")
    ok(isinstance(md.get("files"), dict) and md["files"], f"{where}: measured.files empty")
    for name, fp in md["files"].items():
        for k in ("path", "exists", "bytes", "sha256_16"):
            ok(k in fp, f"{where}: measured.files[{name}] missing {k}")
        ok(isinstance(fp["exists"], bool), f"{where}: measured.files[{name}].exists not bool")
        if fp["exists"]:
            ok(isinstance(fp["sha256_16"], str) and len(fp["sha256_16"]) >= 8,
               f"{where}: measured.files[{name}] digest bad")
    ok(isinstance(md.get("experiments"), dict), f"{where}: measured.experiments not dict")
    for eid, exp in md["experiments"].items():
        ok(isinstance(exp.get("comparable"), bool), f"{where}: {eid} comparable not bool")
        for side in ("baseline", "ubpa", "improvement"):
            block = exp.get(side)
            ok(isinstance(block, dict), f"{where}: {eid}.{side} not dict")
            for k in HEADLINE_KEYS:
                ok(k in (block or {}), f"{where}: {eid}.{side} missing {k}")
    fresh = md.get("fresh")
    ok(isinstance(fresh, dict) and "file" in fresh, f"{where}: measured.fresh.file missing")
    ok(isinstance(fresh["file"].get("exists"), bool), f"{where}: measured.fresh.file.exists not bool")
    if fresh["file"]["exists"]:
        ok(isinstance(fresh.get("delta"), dict) and fresh["delta"], f"{where}: fresh.delta empty after re-measure")
        for eid, d in fresh["delta"].items():
            ok(isinstance(d.get("timing_keys"), list), f"{where}: {eid} timing_keys not a list")
            ok(d.get("match") in (True, False, None), f"{where}: {eid} overall match bad")
            for k, row in (d.get("metrics") or {}).items():
                for f in ("shipped", "remeasured", "match", "timing"):
                    ok(f in row, f"{where}: {eid}.{k} delta row missing {f}")
                ok(isinstance(row["timing"], bool), f"{where}: {eid}.{k} timing flag not bool")

    rm = s["remeasure"]
    for k in ("status", "exit_code", "started", "finished", "seconds", "log", "out"):
        ok(k in rm, f"{where}: remeasure missing {k}")
    ok(isinstance(rm["log"], list), f"{where}: remeasure.log not list")


def drive(dash: Dashboard, ticks: int) -> None:
    for _ in range(ticks):
        with dash.lock:
            dash._step_pair()


class TestSnapshotContract(unittest.TestCase):
    def _no_problems(self, problems: list[str]) -> None:
        if problems:
            shown = "\n".join(f" - {p}" for p in problems[:25])
            self.fail(f"{len(problems)} contract violation(s):\n{shown}")

    def test_running_session(self) -> None:
        d = Dashboard(label="contract")
        drive(d, 40)
        problems: list[str] = []
        check_snapshot(d.snapshot(), "after 40 ticks", problems)
        self._no_problems(problems)

    def test_every_map_and_fleet_size(self) -> None:
        """Every selectable floor, at the fleet sizes the UI allows."""
        for key in map_keys():
            for n in (3, 5, 8):
                with self.subTest(env=key, robots=n):
                    d = Dashboard(label=f"{key}-{n}")
                    d.handle_action({"action": "env", "env": key})
                    d.handle_action({"action": "robots", "n": n})
                    drive(d, 25)
                    problems = []
                    check_snapshot(d.snapshot(), f"{key}/n={n}", problems)
                    self._no_problems(problems)

    def test_finished_run_and_shadow(self) -> None:
        d = Dashboard(label="finished")
        d.handle_action({"action": "speed", "speed": 16.0})
        with d.lock:
            guard = 0
            while not d.sim.all_done() and guard < 4000:
                d._step_pair()
                guard += 1
        snap = d.snapshot()
        problems: list[str] = []
        check_snapshot(snap, "finished run", problems)
        self._no_problems(problems)
        self.assertTrue(snap["complete"], "finished run reports complete=False")
        self.assertIsNotNone(snap["metrics"]["all_tasks_complete_tick"], "finished run has no finish tick")
        self.assertIsNotNone(snap["shadow"], "shadow twin missing on a finished run")

    def test_operator_actions(self) -> None:
        """Replay every action the UI can POST, including an unknown one."""
        d = Dashboard(label="actions")
        problems: list[str] = []
        for body in (
            {"action": "block_aisle"},
            {"action": "toggle", "x": 1, "y": 1},
            {"action": "clear_blocks"},
            {"action": "radio", "drop_prob": 0.4, "delay": 3},
            {"action": "definitely_not_an_action"},  # must not raise
            {"action": "policy", "policy": "stop_wait"},
            {"action": "policy", "policy": "ubpa"},
            {"action": "speed", "speed": 4.0},
            {"action": "pause"},
            {"action": "step"},
            {"action": "resume"},
            {"action": "robots", "n": 8},
            {"action": "drain", "robot_id": "A", "level": 14},
            {"action": "forecast"},
        ):
            res = d.handle_action(body)
            if "ok" not in res:
                problems.append(f"action {body['action']} returned no ok flag: {res}")
        snap = d.snapshot()
        check_snapshot(snap, "after action sweep", problems)
        if snap["radio_stats"]["drop_prob"] != 0.4:
            problems.append("radio action did not apply")
        if snap["n_robots"] != 8:
            problems.append("robots action did not apply")
        drained = [b for b in snap["robots"] if b["id"] == "A"]
        if drained and not 0 <= drained[0]["battery"] <= 20:
            problems.append(f"drain action did not lower battery (A={drained[0]['battery']})")
        self._no_problems(problems)

    def test_forecast_payload(self) -> None:
        d = Dashboard(label="forecast")
        res = d.handle_action({"action": "forecast", "horizon": 40})
        forecast = res.get("forecast")
        self.assertIsInstance(forecast, dict, f"forecast missing from response: {res}")
        missing = [k for k in FORECAST_KEYS if k not in forecast]
        self.assertEqual([], missing, f"forecast missing keys: {missing}")

    def test_add_and_remove_task_round_trip(self) -> None:
        d = Dashboard(label="jobs")
        snap = d.snapshot()
        w = snap["world"]
        walls = {tuple(c) for c in w["walls"]}
        free = [c for c in ([1, 1], [2, 2], [3, 3], [1, 3], [3, 1]) if tuple(c) not in walls]
        self.assertGreaterEqual(len(free), 2, "no free cells to post a job between")
        added = d.handle_action({"action": "add_task", "pick": free[0], "drop": free[-1], "urgency": 0.9})
        self.assertTrue(added.get("ok"), f"add_task failed: {added}")
        tid = added["task"]["task_id"]
        snap = d.snapshot()
        problems: list[str] = []
        check_snapshot(snap, "after add_task", problems)
        self._no_problems(problems)
        self.assertTrue(any(j["task_id"] == tid for j in snap["jobs"]),
                        "posted job absent from the job board payload")
        removed = d.handle_action({"action": "remove_task", "task_id": tid})
        self.assertTrue(removed.get("ok"), f"remove_task failed: {removed}")
        self.assertFalse(any(j["task_id"] == tid for j in d.snapshot()["jobs"]),
                         "removed job still on the board")


if __name__ == "__main__":
    unittest.main(verbosity=2)
