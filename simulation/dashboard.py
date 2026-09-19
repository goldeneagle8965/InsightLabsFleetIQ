"""Live dashboard: spectator view plus operator controls. Still no dispatcher.

The dashboard is allowed to *edit the world* (walls, radio quality, jobs,
battery level, fleet size, tick speed) and to *publish measurements*. It is not
allowed to coordinate: it never names a task winner, never plans a path and
never grants right-of-way. Every coordination decision on screen was taken by a
robot and broadcast over the peer radio, which is what the auction feed and the
radio log exist to show.

Two servers come out of this module:

  * ``serve(dash)``           -- one fleet, every visitor watches it (local demo)
  * ``serve_sessions(factory)`` -- one fleet per browser session (deploy, and
    lets several judges click the map at the same time without stomping each
    other's run)
"""

from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from simulation.config import MAX_TICKS_DEMO, TICK_HZ
from simulation.engine import FleetSim
from simulation.maps import get_map, map_keys
from simulation.world import Task

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "frontend" / "static"
RESULTS = ROOT / "results"
MEASURE_SCRIPT = ROOT / "scripts" / "run_experiments.py"

# Shipped measurement artifacts. The page shows file name, size, mtime and a
# content hash next to every number, so a reviewer can diff the page against
# the file on disk instead of taking our word for it.
MEASURED_FILES: dict[str, Path] = {
    "metrics": RESULTS / "metrics.json",
    "degradation": RESULTS / "degradation.json",
    "scale_delay": RESULTS / "probe_26123_scale_delay.json",
    "ablation": RESULTS / "probe_26123_ablation.json",
}
# Re-measurement never overwrites the shipped artifact: it writes beside it, so
# the deck-cited numbers stay exactly as measured while the page proves they
# reproduce on this machine.
REMEASURE_OUT = RESULTS / "remeasure_metrics.json"

HEADLINE_KEYS = (
    "all_tasks_complete_tick",
    "deadlock_count",
    "stall_count",
    "stall_handoff_count",
    "collision_count",
    "mean_robot_step_ms",
)
# Wall-clock metrics cannot repeat exactly across machines or CPU load. They are
# still shown side by side after a re-measure, but they never count as a
# mismatch: everything else in the table is a tick count and must be identical.
TIMING_KEYS = ("mean_robot_step_ms",)

SPEED_STEPS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
SPEED_MIN, SPEED_MAX = 0.25, 16.0
MAX_BODY = 16 * 1024
# Never advance more than this many ticks in one call: after a stall or a tab
# switch the loop must return to the HTTP threads promptly.
MAX_TICKS_PER_CALL = 480
# Wall-clock period of a driving loop, and the sleepy period used when there is
# nothing to do (paused / run finished).
CATCHUP_S = 0.005
IDLE_S = 0.25

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
}


# --------------------------------------------------------------------------
# measured-artifact provenance
# --------------------------------------------------------------------------


def _source_mtime() -> float:
    """Newest mtime across the code the numbers were produced by."""
    newest = 0.0
    for folder in (ROOT / "simulation", ROOT / "scripts"):
        if not folder.is_dir():
            continue
        for path in folder.glob("*.py"):
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
    return newest


def _file_fingerprint(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError:
        return {"name": path.name, "exists": False}
    st = path.stat()
    return {
        "name": path.name,
        "path": path.relative_to(ROOT).as_posix(),
        "exists": True,
        "bytes": len(raw),
        "sha256_16": hashlib.sha256(raw).hexdigest()[:16],
        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        "mtime_epoch": round(st.st_mtime, 3),
        "stale": st.st_mtime < _source_mtime(),
    }


def _experiment_view(payload: Any) -> dict[str, Any]:
    """Headline comparison out of a metrics.json-shaped artifact."""
    out: dict[str, Any] = {}
    experiments = payload.get("experiments") if isinstance(payload, dict) else None
    if not isinstance(experiments, dict):
        return out
    for name, exp in experiments.items():
        if not isinstance(exp, dict):
            continue
        summary = exp.get("summary") if isinstance(exp.get("summary"), dict) else {}
        base = summary.get("baseline") if isinstance(summary.get("baseline"), dict) else {}
        ours = summary.get("ubpa") if isinstance(summary.get("ubpa"), dict) else {}
        improvements = summary.get("improvement") if isinstance(summary.get("improvement"), dict) else {}
        out[name] = {
            "comparable": bool(summary.get("comparable")),
            "seeds": exp.get("seeds"),
            "baseline": {k: base.get(k) for k in HEADLINE_KEYS},
            "ubpa": {k: ours.get(k) for k in HEADLINE_KEYS},
            "improvement": {k: improvements.get(k) for k in HEADLINE_KEYS},
        }
    return out


def _reproduce_delta(shipped: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    """Per-experiment, per-metric: shipped value vs just-measured value."""
    delta: dict[str, Any] = {}
    for name, exp in fresh.items():
        old = shipped.get(name)
        if not isinstance(old, dict):
            continue
        rows: dict[str, Any] = {}
        for key in HEADLINE_KEYS:
            ship = (old.get("ubpa") or {}).get(key)
            now = (exp.get("ubpa") or {}).get(key)
            timing = key in TIMING_KEYS
            rows[key] = {
                "shipped": ship,
                "remeasured": now,
                "match": None if timing else ship == now,
                "timing": timing,
            }
        delta[name] = {
            "metrics": rows,
            "match": all(r["match"] for r in rows.values() if r["match"] is not None),
            "timing_keys": [k for k in HEADLINE_KEYS if k in TIMING_KEYS],
        }
    return delta


_MEASURED_LOCK = threading.Lock()
_MEASURED_CACHE: dict[str, Any] = {"key": None, "payload": None}


def _measured_cache_key() -> tuple:
    key: list[Any] = [round(_source_mtime(), 3)]
    for path in list(MEASURED_FILES.values()) + [REMEASURE_OUT]:
        try:
            st = path.stat()
            key.append((path.name, round(st.st_mtime, 3), st.st_size))
        except OSError:
            key.append((path.name, None, None))
    return tuple(key)


def measured_payload() -> dict[str, Any]:
    """Fingerprint + headline numbers for the artifacts, re-read when they move."""
    key = _measured_cache_key()
    with _MEASURED_LOCK:
        if _MEASURED_CACHE["key"] == key:
            return _MEASURED_CACHE["payload"]

    files: dict[str, Any] = {}
    artifacts: dict[str, Any] = {}
    for name, path in MEASURED_FILES.items():
        files[name] = _file_fingerprint(path)
        if files[name].get("exists"):
            try:
                artifacts[name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                artifacts[name] = None
    metrics = artifacts.get("metrics") or {}
    experiments = _experiment_view(metrics)

    payload: dict[str, Any] = {
        "files": files,
        "experiments": experiments,
        "tick_limit": metrics.get("tick_limit") if isinstance(metrics, dict) else None,
        "note": metrics.get("note") if isinstance(metrics, dict) else None,
        "source_mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(key[0])),
        "provenance": (
            "Read from results/*.json at request time; no number on this page is typed in by hand. "
            "Press Re-measure to rebuild the E1/E2/E3 comparison on this machine."
        ),
    }

    fresh_info = _file_fingerprint(REMEASURE_OUT)
    payload["fresh"] = {"file": fresh_info, "experiments": {}, "delta": {}}
    if fresh_info.get("exists"):
        try:
            fresh = json.loads(REMEASURE_OUT.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fresh = None
        if isinstance(fresh, dict):
            fresh_view = _experiment_view(fresh)
            payload["fresh"]["experiments"] = fresh_view
            payload["fresh"]["delta"] = _reproduce_delta(experiments, fresh_view)

    with _MEASURED_LOCK:
        _MEASURED_CACHE["key"] = key
        _MEASURED_CACHE["payload"] = payload
    return payload


class RemeasureJob:
    """Re-runs the experiment suite, non-destructively, on this machine.

    The suite is fast (E1/E2/E3 at 800 ticks, tens of seconds at worst), so this
    runs in a thread and the page polls its status. Output goes to
    ``results/remeasure_metrics.json``; the shipped ``metrics.json`` is never
    touched.
    """

    def __init__(self, script: Path = MEASURE_SCRIPT, out: Path = REMEASURE_OUT) -> None:
        self.script = script
        self.out = out
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "status": "idle",
            "exit_code": None,
            "started": None,
            "finished": None,
            "seconds": None,
            "log": [],
            "out": out.name,
        }

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self._state["status"] == "running":
                return False, "already running"
            if not self.script.exists():
                return False, f"missing {self.script.name}"
            self._state = {
                "status": "running",
                "exit_code": None,
                "started": time.strftime("%H:%M:%S"),
                "finished": None,
                "seconds": None,
                "log": [],
                "out": self.out.name,
            }
        threading.Thread(target=self._run, daemon=True, name="remeasure-26123").start()
        return True, "started"

    def _run(self) -> None:
        t0 = time.perf_counter()
        cmd = [sys.executable, str(self.script), "--out", str(self.out)]
        log: deque[str] = deque(maxlen=30)
        exit_code: int | None = None
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if proc.stdout is not None:
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        log.append(line)
            exit_code = proc.wait()
        except OSError as exc:
            log.append(f"{type(exc).__name__}: {exc}")
            exit_code = -1
        with self._lock:
            self._state.update(
                {
                    "status": "done" if exit_code == 0 else "failed",
                    "exit_code": exit_code,
                    "finished": time.strftime("%H:%M:%S"),
                    "seconds": round(time.perf_counter() - t0, 1),
                    "log": list(log),
                }
            )

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)


REMEASURE = RemeasureJob()


# --------------------------------------------------------------------------
# one fleet (+ its shadow twin)
# --------------------------------------------------------------------------


class Dashboard:
    """One fleet under observation, plus the other policy on the same map.

    The shadow twin is a second ``FleetSim`` with the same seed, map, radio
    quality and fleet size, driven tick-for-tick with the opposite policy. It is
    what makes the comparison visible *during* the demo rather than only in the
    results file: the panel shows what stop-and-wait did at the same tick.
    Operator edits are mirrored, so both fleets face the same obstacle.
    """

    def __init__(self, sim: FleetSim | None = None, *, label: str = "local") -> None:
        self.lock = threading.RLock()
        self.label = label
        self.created = time.time()
        self.last_seen = self.created
        self.sim = sim if sim is not None else FleetSim()
        self.shadow_error: str | None = None
        self.shadow: FleetSim | None = None
        self._carry = 0.0
        self._ticks_served = 0
        self.httpd: ThreadingHTTPServer | None = None
        self.rebuild_shadow()

    # ----- shadow twin --------------------------------------------------

    def _peer_policy(self) -> str:
        return "stop_wait" if self.sim.policy == "ubpa" else "ubpa"

    def rebuild_shadow(self) -> None:
        """Fresh twin of the current fleet under the other policy."""
        self.shadow_error = None
        try:
            live = self.sim
            jobs = [Task(t.task_id, t.pick, t.drop, t.urgency) for t in live.world.tasks.values()]
            self.shadow = FleetSim(
                n_robots=live.n_robots,
                policy=self._peer_policy(),
                seed=live.seed,
                drop_prob=live.drop_prob,
                radio_delay=live.radio_delay,
                max_ticks=live.max_ticks,
                tasks=jobs,
                spawn=live.spawn,
                env=live.env,
            )
            self._mirror_blocks()
        except Exception as exc:  # a comparison panel must never break the demo
            self.shadow = None
            self.shadow_error = f"{type(exc).__name__}: {exc}"

    def _mirror_blocks(self) -> None:
        shadow = self.shadow
        if shadow is None:
            return
        try:
            live_cells = set(self.sim.world.blocked)
            mine = set(shadow.world.blocked)
            for cell in live_cells - mine:
                shadow.world.set_block(cell, True)
            for cell in mine - live_cells:
                shadow.world.set_block(cell, False)
        except Exception as exc:
            self.shadow_error = f"{type(exc).__name__}: {exc}"

    def _mirror_radio(self) -> None:
        shadow = self.shadow
        if shadow is None:
            return
        shadow.drop_prob = self.sim.drop_prob
        shadow.bus.drop_prob = self.sim.drop_prob
        shadow.radio_delay = self.sim.radio_delay
        shadow.bus.delay = self.sim.radio_delay

    def _step_pair(self) -> None:
        self.sim.step()
        if self.shadow is not None:
            try:
                self.shadow.step()
            except Exception as exc:
                self.shadow_error = f"{type(exc).__name__}: {exc}"
                self.shadow = None

    # ----- clock --------------------------------------------------------

    @property
    def speed(self) -> float:
        return float(self.sim.speed)

    def advance(self, dt_s: float) -> int:
        """Consume the ticks that ``dt_s`` wall-clock seconds are worth.

        Speed multiplies tick consumption only; the sim's tick size never
        changes, so every number in the measured tables stays comparable.
        """
        if self.sim.paused or self.sim.all_done():
            self._carry = 0.0
            return 0
        self._carry = min(self._carry + dt_s * TICK_HZ * self.speed, MAX_TICKS_PER_CALL)
        ticks = int(self._carry)
        if ticks <= 0:
            return 0
        self._carry -= ticks
        ran = 0
        with self.lock:
            for _ in range(ticks):
                if self.sim.paused or self.sim.all_done():
                    break
                self._step_pair()
                ran += 1
        self._ticks_served += ran
        return ran

    def run_forever(self, stop: threading.Event | None = None) -> None:
        """Drive this fleet from a dedicated thread (deployed/multi-session mode)."""
        last = time.perf_counter()
        while stop is None or not stop.is_set():
            now = time.perf_counter()
            dt, last = now - last, now
            self.advance(dt)
            idle = self.sim.paused or self.sim.all_done()
            time.sleep(IDLE_S if idle else CATCHUP_S)

    # ----- views --------------------------------------------------------

    def _shadow_view(self) -> dict[str, Any] | None:
        shadow = self.shadow
        if shadow is None:
            return {"error": self.shadow_error} if self.shadow_error else None
        m = shadow.metrics()
        return {
            "policy": shadow.policy,
            "tick": shadow.tick,
            "lag_ticks": self.sim.tick - shadow.tick,
            "tasks_done": m.tasks_done,
            "tasks_total": m.tasks_total,
            "complete": m.complete,
            "finish_tick": m.all_tasks_complete_tick,
            "collision_count": m.collision_count,
            "deadlock_count": m.deadlock_count,
            "stall_count": m.stall_count,
            "mean_wait_ticks": round(m.mean_wait_ticks, 3),
        }

    def snapshot(self) -> dict[str, Any]:
        self.last_seen = time.time()
        with self.lock:
            snap = self.sim.snapshot()
            snap["shadow"] = self._shadow_view()
            snap["speed"] = round(self.speed, 2)
            snap["speed_steps"] = list(SPEED_STEPS)
            snap["envs"] = [
                {"key": key, "label": get_map(key).label, "blurb": get_map(key).blurb}
                for key in map_keys()
            ]
            snap["server"] = {
                "session": self.label,
                "uptime_s": round(time.time() - self.created, 1),
                "ticks_served": self._ticks_served,
            }
        snap["measured"] = measured_payload()
        snap["remeasure"] = REMEASURE.status()
        return snap

    # ----- operator actions ---------------------------------------------

    def handle_action(self, body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action") or "")
        if action == "remeasure":
            ok, message = REMEASURE.start()
            return {"ok": ok, "message": message, "remeasure": REMEASURE.status()}
        with self.lock:
            try:
                return self._apply(action, body)
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                return {"ok": False, "error": f"{action}: {exc}"}

    def _cell(self, body: dict[str, Any], key: str) -> tuple[int, int]:
        raw = body.get(key)
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise ValueError(f"{key} must be [x, y]")
        x, y = int(raw[0]), int(raw[1])
        if not self.sim.world.in_bounds((x, y)):
            raise ValueError(f"{key} {x},{y} is outside the map")
        return x, y

    def _apply(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        sim = self.sim

        if action == "pause":
            sim.paused = True
            self._carry = 0.0
            return {"ok": True, "paused": True}

        if action in ("resume", "play"):
            sim.paused = False
            self._carry = 0.0
            return {"ok": True, "paused": False}

        if action == "toggle_pause":
            sim.paused = not sim.paused
            self._carry = 0.0
            return {"ok": True, "paused": sim.paused}

        if action == "step":
            if sim.all_done():
                return {"ok": False, "error": "run already complete"}
            if not sim.paused:
                return {"ok": False, "error": "pause the fleet before single-stepping"}
            # FleetSim.step() is a no-op while paused, so the single-step has to
            # lift the flag for exactly one tick and put it straight back.
            sim.paused = False
            try:
                self._step_pair()
            finally:
                sim.paused = True
            self._ticks_served += 1
            return {"ok": True, "tick": sim.tick}

        if action == "reset":
            sim.reset()
            self.rebuild_shadow()
            self._carry = 0.0
            return {"ok": True, "tick": sim.tick}

        if action == "block_aisle":
            cell = sim.block_aisle()
            self._mirror_blocks()
            return {"ok": True, "cell": list(cell)}

        if action == "toggle":
            x, y = self._xy(body)
            on = bool(sim.toggle_block(x, y))
            self._mirror_blocks()
            return {"ok": True, "blocked": on, "cell": [x, y]}

        if action == "clear_blocks":
            removed = len(sim.world.blocked)
            sim.world.blocked.clear()
            self._mirror_blocks()
            sim.events.append(
                {"tick": sim.tick, "robot": "OP", "kind": "unblock", "detail": f"cleared {removed} cells"}
            )
            return {"ok": True, "cleared": removed}

        if action == "policy":
            policy = str(body.get("policy", "ubpa"))
            if policy not in ("ubpa", "stop_wait"):
                return {"ok": False, "error": f"unknown policy {policy!r}"}
            sim.policy = policy
            sim.reset()
            self.rebuild_shadow()
            return {"ok": True, "policy": policy}

        if action == "radio":
            out = sim.set_radio(drop_prob=body.get("drop_prob"), delay=body.get("delay"))
            self._mirror_radio()
            return {"ok": True, **out}

        if action == "speed":
            speed = float(body.get("speed", 1.0))
            sim.speed = max(SPEED_MIN, min(SPEED_MAX, speed))
            self._carry = 0.0
            return {"ok": True, "speed": sim.speed}

        if action == "env":
            key = str(body.get("env") or "")
            if key not in map_keys():
                return {"ok": False, "error": f"unknown environment {key!r}"}
            sim.set_env(key)
            self.rebuild_shadow()
            return {"ok": True, "env": sim.env, "label": get_map(sim.env).label}

        if action == "robots":
            n = int(body.get("n", 3))
            sim.set_robots(n)
            self.rebuild_shadow()
            return {"ok": True, "n_robots": sim.n_robots}

        if action == "add_task":
            pick = self._cell(body, "pick")
            drop = self._cell(body, "drop")
            for label, cell in (("pick", pick), ("drop", drop)):
                if not sim.world.is_free(cell):
                    return {"ok": False, "error": f"{label} {cell} is inside a rack"}
            urgency = float(body.get("urgency", 0.6))
            task = sim.add_task(pick, drop, urgency)
            if self.shadow is not None:
                self.shadow.post_task(Task(task.task_id, task.pick, task.drop, task.urgency))
            return {
                "ok": True,
                "task": {"task_id": task.task_id, "pick": list(task.pick), "drop": list(task.drop)},
            }

        if action == "remove_task":
            task_id = str(body.get("task_id") or "")
            gone = sim.remove_task(task_id)
            if self.shadow is not None:
                self.shadow.remove_task(task_id)
            return {"ok": gone, "task_id": task_id, "error": None if gone else "no such job"}

        if action == "drain":
            robot_id = str(body.get("robot_id") or body.get("robot") or "")
            level = float(body.get("level", 15.0))
            left = sim.drain_robot(robot_id, level)
            if left < 0:
                return {"ok": False, "error": f"unknown robot {robot_id!r}"}
            if self.shadow is not None:
                self.shadow.drain_robot(robot_id, level)
            return {"ok": True, "robot_id": robot_id, "battery": left}

        if action == "forecast":
            horizon = int(body.get("horizon") or MAX_TICKS_DEMO)
            horizon = max(1, min(MAX_TICKS_DEMO, horizon))
            return {"ok": True, "forecast": self.sim.forecast(horizon)}

        return {"ok": False, "error": f"unknown action {action!r}"}

    def _xy(self, body: dict[str, Any]) -> tuple[int, int]:
        x, y = int(body.get("x", -1)), int(body.get("y", -1))
        if not self.sim.world.in_bounds((x, y)):
            raise ValueError(f"cell {x},{y} is outside the map")
        if (x, y) in self.sim.world.walls:
            raise ValueError(f"cell {x},{y} is inside a rack")
        return x, y


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------


class SessionStore:
    """One independent fleet per browser session.

    A judge opening the URL on a second device gets a fleet of their own: their
    clicks do not disturb anyone else's demo. Sessions are capped by count and
    idle time so an open port cannot be turned into unbounded memory.
    """

    def __init__(
        self,
        factory: Callable[[str], Dashboard],
        *,
        ttl_s: float = 3600.0,
        limit: int = 32,
        autostart: bool = True,
    ) -> None:
        self._factory = factory
        self._ttl_s = ttl_s
        self._limit = max(1, limit)
        self._autostart = autostart
        self._lock = threading.Lock()
        self._sessions: dict[str, Dashboard] = {}
        self._stops: dict[str, threading.Event] = {}

    def resolve(self, sid: str | None) -> tuple[str, Dashboard]:
        with self._lock:
            self._prune_locked()
            if sid and sid in self._sessions:
                return sid, self._sessions[sid]
            new_sid = secrets.token_urlsafe(9)
            dash = self._factory(new_sid)
            self._sessions[new_sid] = dash
            if self._autostart:
                stop = threading.Event()
                self._stops[new_sid] = stop
                threading.Thread(
                    target=dash.run_forever,
                    args=(stop,),
                    daemon=True,
                    name=f"fleet-{new_sid}",
                ).start()
            return new_sid, dash

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _prune_locked(self) -> None:
        now = time.time()
        for sid, dash in list(self._sessions.items()):
            if now - dash.last_seen > self._ttl_s:
                self._drop_locked(sid)
        while len(self._sessions) >= self._limit:
            oldest = min(self._sessions.items(), key=lambda kv: kv[1].last_seen)[0]
            self._drop_locked(oldest)

    def _drop_locked(self, sid: str) -> None:
        self._sessions.pop(sid, None)
        stop = self._stops.pop(sid, None)
        if stop is not None:
            stop.set()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _cookie_sid(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "sid" and value:
            return value
    return None


def make_handler(resolve: Callable[[str | None], tuple[str, Dashboard]]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SIH26123"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send(self, code: int, body: bytes, ctype: str, cookie: str | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            if cookie:
                self.send_header("Set-Cookie", f"sid={cookie}; Path=/; SameSite=Lax")
            if self.close_connection:
                # Tell the client before dropping the socket, otherwise it tries
                # to reuse a connection we have already hung up.
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, payload: dict[str, Any], cookie: str | None = None) -> None:
            self._send(code, json.dumps(payload).encode("utf-8"), "application/json", cookie)

        def _session(self) -> tuple[str, Dashboard]:
            sid = _cookie_sid(self.headers.get("Cookie"))
            try:
                return resolve(sid)
            except Exception as exc:  # never let a bad session 500 the page
                return "", _EmergencyDashboard(f"{type(exc).__name__}: {exc}")

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/health":
                self._json(200, {"ok": True, "service": "sih26123", "time": time.strftime("%H:%M:%S")})
                return
            if path in ("/", "/index.html"):
                self._static("index.html")
                return
            if path == "/state":
                sid, dash = self._session()
                try:
                    payload = dash.snapshot()
                except Exception as exc:
                    self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                    return
                self._json(200, payload, cookie=sid or None)
                return
            if path.startswith("/static/"):
                self._static(path[len("/static/") :])
                return
            self._send(404, b"not found", "text/plain")

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def _static(self, rel: str) -> None:
            target = (STATIC / rel).resolve()
            root = STATIC.resolve()
            if target != root and root not in target.parents:
                self._send(403, b"forbidden", "text/plain")
                return
            if not target.is_file():
                self._send(404, b"missing", "text/plain")
                return
            ctype = CONTENT_TYPES.get(target.suffix, "text/plain")
            try:
                body = target.read_bytes()
            except OSError:
                self._send(500, b"unreadable", "text/plain")
                return
            self._send(200, body, ctype)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            # Any early return that leaves the request body unread must close the
            # connection, otherwise the leftover bytes are parsed as the next
            # request line on a keep-alive socket and the client sees a garbage
            # 400 instead of the answer it asked for.
            if length > MAX_BODY:
                self.close_connection = True
                self._send(413, b'{"ok":false,"error":"body too large"}', "application/json")
                return
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                self._send(411, b'{"ok":false,"error":"length required"}', "application/json")
                return
            if path not in ("/action", "/"):
                self.close_connection = True
                self._send(404, b"not found", "text/plain")
                return
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be an object")
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                self._json(400, {"ok": False, "error": "invalid JSON body"})
                return
            sid, dash = self._session()
            try:
                out = dash.handle_action(body)
            except Exception as exc:  # a bad action must not take the server down
                out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._json(200, out, cookie=sid or None)

    return Handler


class _EmergencyDashboard:
    """Fallback so a session failure still yields a readable page."""

    def __init__(self, message: str) -> None:
        self.last_seen = time.time()
        self.message = message

    def snapshot(self) -> dict[str, Any]:
        return {"ok": False, "error": self.message}

    def handle_action(self, body: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "error": self.message}


def _serve(resolve: Callable[[str | None], tuple[str, Dashboard]], host: str, port: int) -> ThreadingHTTPServer:
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return Server((host, port), make_handler(resolve))


def serve(dash: Dashboard, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Single-fleet server: every visitor watches the one demo fleet."""

    def resolve(sid: str | None) -> tuple[str, Dashboard]:
        return "local", dash

    httpd = _serve(resolve, host, port)
    dash.httpd = httpd
    return httpd


def serve_sessions(
    factory: Callable[[str], Dashboard],
    host: str = "0.0.0.0",
    port: int = 8765,
    *,
    ttl_s: float = 3600.0,
    limit: int = 32,
) -> ThreadingHTTPServer:
    """Multi-session server used by scripts/run_web.py."""
    store = SessionStore(factory, ttl_s=ttl_s, limit=limit)

    def resolve(sid: str | None) -> tuple[str, Dashboard]:
        return store.resolve(sid)

    httpd = _serve(resolve, host, port)
    httpd.session_store = store  # type: ignore[attr-defined]
    return httpd
