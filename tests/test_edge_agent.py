"""Edge-agent checks. T1 is an equality test against the reference sim, not a smoke test.

Implements T1-T6 and T9-T11 of ``docs/edge/07_test_plan.md``. The "no central
scheduler" guards live in ``tests/test_edge_no_central_scheduler.py``.

Run: python -m unittest tests.test_edge_agent
"""

from __future__ import annotations

import ast
import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.config import MAX_TICKS_DEMO, SENSOR_RANGE  # noqa: E402
from simulation.dashboard import Dashboard  # noqa: E402
from simulation.edge import (  # noqa: E402
    EdgeHost,
    LocalWorldView,
    LoopbackRadio,
    RemoteWorldView,
    SharedMedium,
)
from simulation.edge.agent import EdgeAgent  # noqa: E402
from simulation.edge.protocol import (  # noqa: E402
    CTRL_ACT,
    CTRL_HELLO_WORLD,
    CTRL_OBS,
    HOST_ID,
    PEER_STATE,
    PROTOCOL_VERSION,
    ChunkAssembler,
    ProtocolError,
    chunk_message,
    decode,
    decode_safe,
    encode,
    head,
    world_digest,
)
from simulation.engine import FleetSim  # noqa: E402
from simulation.maps import get_map  # noqa: E402
from simulation.peer import PeerState  # noqa: E402

import tests.test_dashboard_contract as dashboard_contract  # noqa: E402

EDGE_DIR = ROOT / "simulation" / "edge"
RESULTS = ROOT / "results"

#: T9.1 - the shipped benchmark digest, recorded in docs/edge/07_test_plan.md.
METRICS_SHA256 = "6d7d2a9600cd99dbb0e7419a7554747c0610119c078b010ce8f07806e1cea4f8"

#: T9.3 - digests of the reference implementation captured while the working tree
#: was clean against HEAD (``git status --porcelain`` empty for these paths). The
#: edge package re-hosts these files and must never edit them.
SIM_CORE_FILES = (
    "simulation/engine.py",
    "simulation/robot.py",
    "simulation/world.py",
    "simulation/peer.py",
    "simulation/maps.py",
)

#: RunMetrics fields that must be identical between the edge path and the engine.
#: ``mean_robot_step_ms`` is wall-clock timing and deliberately excluded.
PARITY_METRIC_KEYS = (
    "collision_count",
    "deadlock_count",
    "stall_count",
    "stall_handoff_count",
    "all_tasks_complete_tick",
    "total_distance",
    "tasks_done",
    "tasks_total",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wire_world(world, digest: str | None = None) -> dict:
    """A ``HELLO_WORLD`` payload for a real warehouse, as the host builds it."""
    digest = digest or world_digest(world.width, world.height, world.walls, world.turnouts)
    return head(
        CTRL_HELLO_WORLD,
        HOST_ID,
        0,
        digest=digest,
        world={
            "width": int(world.width),
            "height": int(world.height),
            "walls": [list(p) for p in sorted(world.walls)],
            "turnouts": [list(p) for p in sorted(world.turnouts)],
            "charger": [int(world.charger[0]), int(world.charger[1])],
            "sensor_range": int(SENSOR_RANGE),
            "digest": digest,
        },
    )


def free_cell(world) -> tuple[int, int]:
    for y in range(world.height):
        for x in range(world.width):
            if world.is_free((x, y)) and (x, y) not in world.robots.values():
                return (x, y)
    raise AssertionError("no free cell on this map")


def edge_snapshot_for_contract() -> dict:
    """An edge snapshot with the dashboard-owned blocks attached.

    The edge host deliberately does not own ``shadow``/``envs``/``server``/
    ``measured``/``remeasure`` - those are the web layer's. Attaching them here
    proves the host's *own* fields satisfy the same client contract.
    """
    base = Dashboard(FleetSim(n_robots=3, policy="ubpa", seed=1, env="warehouse")).snapshot()
    host = EdgeHost(n_robots=3, policy="ubpa", seed=1, env="warehouse")
    host.run_until(MAX_TICKS_DEMO)
    base.update(host.snapshot())
    return base


class TestLockstepParity(unittest.TestCase):
    """T1 - edge loop-back reproduces FleetSim, tick for tick."""

    def test_lockstep_parity_with_engine(self) -> None:
        ref = FleetSim(n_robots=3, policy="ubpa", seed=1, radio_delay=1, env="warehouse")
        edge = EdgeHost(n_robots=3, policy="ubpa", seed=1, radio_delay=1, env="warehouse")

        steps = 0
        for _ in range(MAX_TICKS_DEMO):
            ref.step()
            edge.step()
            steps += 1
            ref_pos = {b.id: tuple(b.pos) for b in ref.robots}
            edge_pos = {rid: tuple(edge.agents[rid].robot.pos) for rid in edge.order}
            # 1. per-tick, per-robot positions
            self.assertEqual(ref_pos, edge_pos, f"position divergence at tick {ref.tick}")
            if ref.all_done() and edge.all_done():
                break

        self.assertTrue(ref.all_done(), "reference run did not finish")
        self.assertTrue(edge.all_done(), "edge run did not finish")
        self.assertEqual(ref.tick, edge.tick, "completion tick differs")

        # 2. final world state and collision bookkeeping
        self.assertEqual(ref.world.robots, edge.world.robots)
        self.assertEqual(ref.world.collision_events, edge.world.collision_events)

        # 3. headline metrics
        rm, em = ref.metrics().to_dict(), edge.metrics().to_dict()
        for key in PARITY_METRIC_KEYS:
            self.assertEqual(rm[key], em[key], f"metric {key} diverged")

        # 4. task board
        ref_board = {t.task_id: (t.status, t.holder, t.complete_tick) for t in ref.world.tasks.values()}
        edge_board = {t.task_id: (t.status, t.holder, t.complete_tick) for t in edge.world.tasks.values()}
        self.assertEqual(ref_board, edge_board)

        # 5. both robots knew the same tasks and ran the same auctions -> peer
        # traffic (bids, claims) was delivered with identical timing.
        for bot in ref.robots:
            twin = edge.agents[bot.id].robot
            self.assertEqual(set(bot.known_tasks), set(twin.known_tasks), f"{bot.id} known_tasks")
            self.assertEqual(set(bot.auctions), set(twin.auctions), f"{bot.id} auctions")

        self.assertGreater(steps, 50, "parity run finished suspiciously early")

    def test_parity_after_forced_block(self) -> None:
        """The same equality, with an operator block mid-run (T1, second phase)."""
        ref = FleetSim(n_robots=3, seed=3, radio_delay=1, forced_block=(6, 3), forced_block_tick=25)
        edge = EdgeHost(n_robots=3, seed=3, radio_delay=1, forced_block=(6, 3), forced_block_tick=25)
        for _ in range(MAX_TICKS_DEMO):
            ref.step()
            edge.step()
            self.assertEqual(
                {b.id: tuple(b.pos) for b in ref.robots},
                {rid: tuple(edge.agents[rid].robot.pos) for rid in edge.order},
                f"divergence at tick {ref.tick} after forced block",
            )
            if ref.all_done() and edge.all_done():
                break
        self.assertTrue(edge.all_done())

    def test_parity_on_alternate_map(self) -> None:
        """The adapter is map-agnostic: parity holds after an env switch (T1/T2)."""
        for key in ("hospital", "mall"):
            ref = FleetSim(n_robots=3, seed=1, radio_delay=1, env=key)
            edge = EdgeHost(n_robots=3, seed=1, radio_delay=1, env=key)
            for _ in range(MAX_TICKS_DEMO):
                ref.step()
                edge.step()
                self.assertEqual(
                    {b.id: tuple(b.pos) for b in ref.robots},
                    {rid: tuple(edge.agents[rid].robot.pos) for rid in edge.order},
                    f"divergence on {key} at tick {ref.tick}",
                )
                if ref.all_done() and edge.all_done():
                    break
            self.assertTrue(edge.all_done(), f"{key} did not finish")


class TestWorldViewEquivalence(unittest.TestCase):
    """T2 - every world member the robot reads is answered identically."""

    def test_worldview_equivalent_to_warehouse(self) -> None:
        for key in ("warehouse", "hospital"):
            spec = get_map(key)
            world = spec.build()
            block = free_cell(world)
            world.set_block(block, True)

            view = RemoteWorldView.from_world_message(wire_world(world))

            self.assertEqual(view.width, world.width)
            self.assertEqual(view.height, world.height)
            self.assertEqual(set(view.walls), set(world.walls))
            self.assertEqual(set(view.turnouts), set(world.turnouts))
            self.assertEqual((int(view.charger[0]), int(view.charger[1])), tuple(world.charger))

            # in_bounds over the whole grid plus one ring outside it
            for x in range(-1, world.width + 1):
                for y in range(-1, world.height + 1):
                    self.assertEqual(
                        view.in_bounds((x, y)), world.in_bounds((x, y)), f"{key} in_bounds {(x, y)}"
                    )

            # sense() for every robot spawn, before/after the block, for several radii
            positions = [tuple(p) for p in spec.spawn()] + [tuple(world.charger)]
            for pos in positions:
                view.apply_obs({"nearby_blocked": [list(p) for p in sorted(world.sense(pos))],
                                "charger": list(world.charger)})
                for rng in (0, 1, 2, 5, SENSOR_RANGE):
                    self.assertEqual(
                        view.sense(pos, rng), world.sense(pos, rng), f"{key} sense {pos} rng={rng}"
                    )

            # the robot must never be handed a map-wide obstacle oracle
            self.assertFalse(hasattr(view, "blocked"), "RemoteWorldView leaked a 'blocked' oracle")

            # LocalWorldView answers the same questions over a live Warehouse
            local = LocalWorldView(world)
            for pos in positions:
                self.assertEqual(local.sense(pos), world.sense(pos), f"{key} local sense {pos}")
            self.assertEqual(local.width, world.width)
            self.assertEqual(set(local.walls), set(world.walls))

    def test_worldview_reload_on_env_switch(self) -> None:
        """An env switch changes geometry, and ``apply_world`` adopts it in place."""
        a = get_map("warehouse").build()
        view = RemoteWorldView.from_world_message(wire_world(a))
        self.assertEqual(set(view.walls), set(a.walls))

        b = get_map("mall").build()
        view.apply_world(wire_world(b))
        self.assertEqual((view.width, view.height), (b.width, b.height))
        self.assertEqual(set(view.walls), set(b.walls))
        self.assertNotEqual(set(view.walls), set(a.walls), "maps unexpectedly identical")


class TestCodecAndHostileInput(unittest.TestCase):
    """T3 - wire encoding, chunking and hostile input."""

    def test_codec_roundtrip_and_malformed(self) -> None:
        # 1. every CTRL type survives encode -> decode unchanged (lists, not tuples)
        samples = [
            head(CTRL_OBS, HOST_ID, 3, to_id="R1", nearby_blocked=[[1, 2]], charger=[1, 9]),
            head(CTRL_ACT, "R1", 3, dest=[2, 3], phase="TO_PICK", task=None),
            head("HELLO", "R1", 0, policy="ubpa", priority=1),
            head("ACT_RESULT", HOST_ID, 3, to_id="R1", position=[2, 3], accepted=True),
            head(PEER_STATE, "R1", 5, robot_id="R1", position=[1, 2], velocity=[0, 0],
                 current_task=None, planned_path=[[1, 2]], battery=90.0, priority=1,
                 timestamp=5, status="IDLE", task_urgency=0.0, workload=0),
        ]
        for msg in samples:
            self.assertEqual(decode(encode(msg)), msg, f"round-trip changed {msg['type']}")

        # 2. PeerState survives the wire
        state = PeerState(
            robot_id="R1", position=(1, 2), velocity=(0, 0), current_task=None,
            planned_path=[(1, 2), (2, 2)], battery=88.0, priority=1, timestamp=7,
            status="TO_PICK", task_urgency=0.5, workload=1,
        )
        wire = dict(state.to_msg(), v=PROTOCOL_VERSION)
        back = PeerState.from_msg(decode(encode(wire)))
        self.assertEqual(back, state)

        # 4. malformed input is rejected, never raised at the call site
        hostile = (
            b"",
            b"{bad",
            b"[1,2,3]",
            b'{"v":1}',       # missing type
            b'{"type":"OBS"}',  # missing version
            b'{"v":99,"type":"OBS"}',
            b'{"v":1,"type":""}',
            "\u00ff\xfe not utf8".encode("utf-8", "replace"),
        )
        for bad in hostile:
            self.assertIsNone(decode_safe(bad), f"decode_safe accepted {bad!r}")
            with self.assertRaises(ProtocolError):
                decode(bad)

    def test_large_world_is_chunked_and_reassembled(self) -> None:
        world = get_map("warehouse").build()
        big = head(
            CTRL_HELLO_WORLD,
            HOST_ID,
            0,
            digest="deadbeefcafe",
            world={
                "width": world.width,
                "height": world.height,
                "walls": [[x, y] for x in range(world.width) for y in range(world.height)],
                "turnouts": [],
                "charger": [1, 9],
                "sensor_range": SENSOR_RANGE,
                "digest": "deadbeefcafe",
            },
        )
        parts = chunk_message(big)
        self.assertGreater(len(parts), 1, "oversize HELLO_WORLD was not chunked")
        for part in parts:
            self.assertLessEqual(len(part), 1280)

        asm = ChunkAssembler()
        out = None
        for part in parts:
            out = asm.feed(decode(part))
        self.assertIsNotNone(out, "reassembly produced nothing")
        self.assertEqual(out, big)

    def test_unknown_type_is_counted_not_dispatched(self) -> None:
        medium = SharedMedium()
        agent = EdgeAgent("R1", (1, 9), LoopbackRadio("R1", medium))
        applied = agent.deliver_datagrams([encode(head("NOT_A_TYPE", "R9", 1))])
        self.assertEqual(applied, 0)
        self.assertEqual(agent.unknown_frames, 1)

    def test_host_is_not_a_peer_broker(self) -> None:
        """Peer frames arriving at the host are refused, not relayed (T3.6/T8.3)."""
        host = EdgeHost(n_robots=3, seed=1)
        before = host.peer_leaked
        consumed = host._handle_ctrl(head(PEER_STATE, "R1", 1, robot_id="R1"))
        self.assertFalse(consumed, "host consumed a peer frame")
        self.assertEqual(host.peer_leaked, before + 1)

    def test_control_is_directly_addressed(self) -> None:
        """T3.6 - CTRL goes point-to-point, never as a fleet broadcast."""
        host = EdgeHost(n_robots=3, seed=1)
        seen: list[tuple[str, str | None]] = []
        real_send = host._send

        def spy(rid: str, msg: dict) -> None:
            seen.append((rid, msg.get("to_id")))
            real_send(rid, msg)

        host._send = spy
        host.run_until(20)
        host._send = real_send

        self.assertTrue(seen)
        for rid, to_id in seen:
            self.assertEqual(to_id, rid, f"CTRL for {rid} was addressed to {to_id!r}")

    def test_flood_does_not_stop_the_loop(self) -> None:
        """T3.8 - a burst of junk is bounded and cannot kill a decision tick."""
        host = EdgeHost(n_robots=3, seed=1)
        host.run_until(20)
        agent = host.agents["R1"]
        junk = [b"{bad"] * 100 + [encode(head("NOPE", "R9", 1))] * 100
        applied = agent.deliver_datagrams(junk)
        self.assertEqual(applied, 0)
        self.assertGreaterEqual(agent.malformed, 100)
        self.assertGreaterEqual(agent.unknown_frames, 100)
        tick_before = host.tick
        host.step()
        self.assertEqual(host.tick, tick_before + 1, "the loop stalled after the flood")


class TestHostIsPhysicsNotController(unittest.TestCase):
    """T4 - the host may only move a robot that asked to move."""

    def test_host_cannot_move_without_act(self) -> None:
        host = EdgeHost(n_robots=3, seed=1)
        host.run_until(20)
        before = {rid: tuple(host.agents[rid].robot.pos) for rid in host.order}

        real = host.collect_actions
        host.collect_actions = lambda: {}  # every ACT withheld for one tick
        host.step()
        host.collect_actions = real

        after = {rid: tuple(host.agents[rid].robot.pos) for rid in host.order}
        self.assertEqual(before, after, "a robot moved without an ACT")
        self.assertEqual(host.late_acts, len(host.order))

    def test_unknown_robot_act_is_ignored(self) -> None:
        host = EdgeHost(n_robots=3, seed=1)
        host.run_until(15)
        real = host.collect_actions

        def extra() -> dict:
            out = dict(real())
            out["R99"] = head(CTRL_ACT, "R99", host.tick, dest=[free_cell(host.world)])
            return out

        host.collect_actions = extra
        host.step()
        host.collect_actions = real
        self.assertNotIn("R99", host.world.robots)
        self.assertNotIn("R99", host.agents)

    def test_out_of_bounds_and_wall_acts_are_blocked(self) -> None:
        host = EdgeHost(n_robots=3, seed=1)
        host.run_until(15)
        stay = tuple(host.agents["R1"].robot.pos)
        wall = sorted(host.world.walls)[0]
        real = host.collect_actions
        results: dict[str, dict] = {}
        real_send = host._send

        def spy(rid: str, msg: dict) -> None:
            if msg.get("type") == "ACT_RESULT":
                results[rid] = msg
            real_send(rid, msg)

        for dest, reason in (((-1, -1), "bounds"), (wall, "wall")):
            host.collect_actions = lambda d=dest: {"R1": head(CTRL_ACT, "R1", host.tick, dest=[d[0], d[1]])}
            host._send = spy
            host.step()
            host._send = real_send
            host.collect_actions = real
            self.assertEqual(tuple(host.agents["R1"].robot.pos), stay, f"R1 moved into {dest}")
            self.assertEqual(results["R1"]["accepted"], False)
            self.assertEqual(results["R1"]["blocked_by"], reason)

    def test_collision_is_measured_never_arbitrated(self) -> None:
        host = EdgeHost(n_robots=3, seed=1)
        host.run_until(20)
        cell = free_cell(host.world)
        before = host.world.collision_events
        results: dict[str, dict] = {}
        real_send = host._send

        def spy(rid: str, msg: dict) -> None:
            if msg.get("type") == "ACT_RESULT":
                results[rid] = msg
            real_send(rid, msg)

        host.collect_actions = lambda: {
            rid: head(CTRL_ACT, rid, host.tick, dest=[cell[0], cell[1]]) for rid in ("R1", "R2")
        }
        host._send = spy
        host.step()
        host._send = real_send

        self.assertEqual(host.world.collision_events, before + 1, "collision not counted once")
        for rid in ("R1", "R2"):
            self.assertTrue(results[rid]["collision"], f"{rid} was not told about the collision")
            self.assertEqual(results[rid]["blocked_by"], "collision")
        # both stayed put - nobody resolved the tie for them
        self.assertNotEqual(tuple(host.agents["R1"].robot.pos), cell)
        self.assertNotEqual(tuple(host.agents["R2"].robot.pos), cell)

    def test_apply_only_ever_receives_act_destinations(self) -> None:
        """T4.5 - the behavioural twin of the static guard."""
        host = EdgeHost(n_robots=3, seed=1)
        acts: dict[str, tuple] = {}
        applied: dict[str, tuple] = {}
        real_collect = host.collect_actions
        real_apply = host.apply

        def collect() -> dict:
            out = real_collect()
            for rid, msg in out.items():
                if msg is not None and msg.get("dest") is not None:
                    acts[rid] = (int(msg["dest"][0]), int(msg["dest"][1]))
            return out

        def apply(intentions: dict) -> dict:
            applied.update({rid: tuple(dest) for rid, dest in intentions.items()})
            return real_apply(intentions)

        host.collect_actions = collect
        host.apply = apply
        host.run_until(60)
        host.collect_actions = real_collect
        host.apply = real_apply

        self.assertTrue(applied, "no moves were applied")
        for rid, dest in applied.items():
            self.assertIn(rid, host.order)
            self.assertEqual(dest, acts[rid], f"{rid} was moved somewhere it never asked for")


class TestAgentDeath(unittest.TestCase):
    """T5 - the fleet survives losing a node."""

    def test_agent_death_is_survivable(self) -> None:
        host = EdgeHost(n_robots=3, seed=1)
        host.run_until(30)
        silence_tick = host.tick
        frozen = tuple(host.agents["R3"].robot.pos)
        outstanding = {t.task_id for t in host.world.tasks.values() if t.status != "DONE"}

        host.silent.add("R3")
        host.run_until(MAX_TICKS_DEMO)

        # 1. the run still completes
        self.assertTrue(host.all_done(), "fleet did not finish after R3 went silent")
        metrics = host.metrics()
        self.assertIsNotNone(metrics.all_tasks_complete_tick)
        self.assertEqual(metrics.tasks_done, metrics.tasks_total)

        # 2. the dead robot never moves again
        self.assertEqual(tuple(host.agents["R3"].robot.pos), frozen)

        # 3. no collision is attributed to it after it went silent
        for a, b, when in host.world.collision_pairs:
            if "R3" in (a, b):
                self.assertLessEqual(when, silence_tick, "R3 collided after going silent")

        # 4. work R3 still held was completed by someone else
        for tid in outstanding:
            task = host.world.tasks[tid]
            self.assertEqual(task.status, "DONE", f"{tid} was abandoned")
            self.assertNotEqual(task.holder, "R3", f"{tid} is still held by the dead node")

        # 5. the snapshot is still renderable by the existing dashboard contract
        base = Dashboard(FleetSim(n_robots=3, seed=1)).snapshot()
        base.update(host.snapshot())
        problems: list[str] = []
        dashboard_contract.check_snapshot(base, "edge-dead-R3", problems)
        self.assertEqual(problems, [], f"contract violations: {problems[:5]}")
        self.assertEqual(host.agent_state("R3"), "dead")


class TestDegradedLinks(unittest.TestCase):
    """T6 - loss degrades the run, it never deadlocks it."""

    def _stop_wait_baseline(self, seed: int, drop_prob: float) -> int:
        sim = FleetSim(n_robots=3, policy="stop_wait", seed=seed, drop_prob=drop_prob)
        sim.run_until(MAX_TICKS_DEMO)
        return sim.metrics().deadlock_count

    def test_degraded_links_40_and_70_pct(self) -> None:
        baseline_tick = {}
        for drop in (0.0, 0.4, 0.7):
            for seed in (1, 2, 3):
                host = EdgeHost(n_robots=3, seed=seed, drop_prob=drop)
                host.run_until(MAX_TICKS_DEMO)
                metrics = host.metrics()
                link = host.link()
                where = f"drop={drop} seed={seed}"

                # 1. every task finishes; the run is bounded
                self.assertTrue(metrics.complete, f"{where}: not complete")
                self.assertLessEqual(host.tick, MAX_TICKS_DEMO)

                # 2. deadlocks never exceed the stop-and-wait baseline
                self.assertLessEqual(
                    metrics.deadlock_count,
                    self._stop_wait_baseline(seed, drop),
                    f"{where}: UBPA deadlocks worse than stop-and-wait",
                )

                # 3. loss is measured, not asserted away
                if drop > 0:
                    self.assertLess(abs(link["lost_pct"] - drop * 100.0), 12.0,
                                    f"{where}: measured loss {link['lost_pct']} far from {drop}")
                    self.assertGreater(link["dropped"], 0)

                if seed == 1:
                    baseline_tick[drop] = metrics.all_tasks_complete_tick

        # 4. the ladder is monotonic in the one direction that matters
        self.assertGreaterEqual(baseline_tick[0.7], baseline_tick[0.0])

    def test_peer_partition_still_completes(self) -> None:
        """Peer channel dead, host alive: work still finishes, penalty recorded.

        The loop-back medium carries both peer broadcasts and the WMS bulletin, so
        a peer-only partition is modelled by dropping every non-WMS broadcast.
        The makespan cost is recorded, not asserted away - and it is real: losing
        PEER_STATE means robots cannot see each other and collisions rise.
        """
        host = EdgeHost(n_robots=3, seed=1)
        medium = host.medium
        real_broadcast = medium.broadcast

        def peers_blacked_out(sender: str, msg: dict, tick: int):
            if sender != "WMS":
                return None
            return real_broadcast(sender, msg, tick)

        medium.broadcast = peers_blacked_out
        host.run_until(MAX_TICKS_DEMO)

        clean = EdgeHost(n_robots=3, seed=1)
        clean.run_until(MAX_TICKS_DEMO)

        self.assertTrue(host.metrics().complete, "fleet deadlocked with the peer channel down")
        self.assertGreaterEqual(
            host.metrics().all_tasks_complete_tick,
            clean.metrics().all_tasks_complete_tick,
            "a partition cannot make the fleet faster",
        )
        self.assertGreater(host.metrics().collision_count, clean.metrics().collision_count,
                           "blind robots should collide more, not less")


class TestBenchmarkPreserved(unittest.TestCase):
    """T9 - the shipped measurement is byte-identical and untouched."""

    def test_benchmark_digest_unchanged(self) -> None:
        metrics = RESULTS / "metrics.json"
        self.assertTrue(metrics.is_file(), "results/metrics.json is missing")

        before = sha256_file(metrics)
        self.assertEqual(before, METRICS_SHA256, "the shipped benchmark digest changed")

        degradation = RESULTS / "degradation.json"
        deg_before = sha256_file(degradation) if degradation.is_file() else None

        # Exercise every path that could conceivably write into results/
        EdgeHost(n_robots=3, seed=1).run_until(MAX_TICKS_DEMO)
        FleetSim(n_robots=3, seed=1).run_until(MAX_TICKS_DEMO)

        self.assertEqual(sha256_file(metrics), before, "an edge run mutated results/metrics.json")
        if deg_before is not None:
            self.assertEqual(sha256_file(degradation), deg_before)

    def test_results_dir_untouched_by_edge_run(self) -> None:
        before = {p.name: sha256_file(p) for p in sorted(RESULTS.glob("*")) if p.is_file()}
        EdgeHost(n_robots=3, seed=1).run_until(MAX_TICKS_DEMO)
        after = {p.name: sha256_file(p) for p in sorted(RESULTS.glob("*")) if p.is_file()}
        self.assertEqual(before, after, "an edge run wrote into results/")

    def test_sim_core_files_unmodified(self) -> None:
        baseline = {
            "simulation/engine.py": "3cf6abb1b9e2dc089482a95450ab9057cc8bb91460ec85f1d3bda7c538131293",
            "simulation/robot.py": "c8bac6fba2f5df1a781cae80eaae237277e371f0905c33e651deed4ec2431040",
            "simulation/world.py": "38466afeaa4ca6e491ed38ddcb6f3630c1ee52fd20be0436d27ea9f57cf85943",
            "simulation/peer.py": "87080732a87f4c5687d51828fc30f61cc979c9be3fb3c6a15326172705fb383f",
            "simulation/maps.py": "731faa8e187c8a663f2782592dc2f84488ae13f7e0865f79a6094baa68228564",
        }
        self.assertEqual(set(baseline), set(SIM_CORE_FILES))
        for rel, digest in baseline.items():
            path = ROOT / rel
            self.assertTrue(path.is_file(), f"{rel} is missing")
            self.assertEqual(sha256_file(path), digest, f"{rel} was modified by the edge work")


class TestOfflineFallback(unittest.TestCase):
    """T10 - the deterministic sim still works and the edge stack is stdlib-only."""

    def test_reference_sim_still_completes(self) -> None:
        sim = FleetSim(n_robots=3, policy="ubpa", seed=1)
        sim.run_until(MAX_TICKS_DEMO)
        self.assertTrue(sim.all_done(), "the offline reference run did not finish")
        self.assertIsNotNone(sim.metrics().all_tasks_complete_tick)

    def test_edge_modules_import_no_third_party(self) -> None:
        allowed = set(sys.stdlib_module_names) | {"__future__", "simulation"}
        offenders: list[str] = []
        for path in sorted(EDGE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    roots = [(node.module or "").split(".")[0]]
                else:
                    continue
                for root in roots:
                    if root and root not in allowed:
                        offenders.append(f"{path.name}: {root}")
        self.assertEqual(offenders, [], f"edge package imports non-stdlib modules: {offenders}")

    def test_core_does_not_import_edge(self) -> None:
        for rel in ("simulation/engine.py", "simulation/robot.py", "simulation/dashboard.py",
                    "simulation/world.py", "scripts/run_demo.py"):
            path = ROOT / rel
            if not path.is_file():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    name = getattr(node, "module", None) or ""
                    roots = [a.name for a in node.names] if isinstance(node, ast.Import) else [name]
                    self.assertFalse(
                        any(r.startswith("simulation.edge") for r in roots),
                        f"{rel} imports the edge package; the fallback must stand alone",
                    )

    def test_edge_agent_builds_a_local_robot(self) -> None:
        """T10.4 - the same agent class runs against a live Warehouse."""
        world = get_map("warehouse").build()
        medium = SharedMedium()
        radio = LoopbackRadio("R1", medium)
        agent = EdgeAgent("R1", tuple(world.charger), radio, view=LocalWorldView(world))
        robot = agent.install()
        self.assertIsNotNone(robot, "EdgeAgent failed to build a robot over LocalWorldView")
        self.assertIs(robot.world, agent.view)
        before = tuple(robot.pos)
        intention = robot.step(1)
        self.assertIsInstance(intention, tuple)
        self.assertEqual(intention, before, "a robot at the charger should hold position")


class TestTelemetry(unittest.TestCase):
    """T11 - degradation is measured and exposed."""

    def test_telemetry_exposes_link_health(self) -> None:
        host = EdgeHost(n_robots=3, seed=1)
        host.run_until(MAX_TICKS_DEMO)
        snap = host.snapshot()
        link = snap["link"]

        # link counters are populated and RTT is transport-measured
        self.assertGreater(link["sent"], 0)
        self.assertGreater(link["recv"], 0)
        self.assertGreaterEqual(link["lost_pct"], 0.0)
        self.assertEqual(link["lost_pct"], 0.0, "a clean loop-back link should lose nothing")
        self.assertGreater(link["rtt_samples"], 0)
        self.assertGreater(link["mean_rtt_ms"], 0.0, "RTT must be measured, not estimated")

        # per-agent liveness and step timing are present
        self.assertEqual(len(snap["agents"]), 3)
        for entry in snap["agents"]:
            self.assertIn(entry["id"], host.order)
            self.assertIn(entry["state"], ("alive", "dead", "boot"))
            self.assertIn("last_seen_tick", entry)
            self.assertIn("tick_lag", entry)
            self.assertIsInstance(entry["step_ms_p95"], float)
            self.assertGreaterEqual(entry["step_ms_p95"], 0.0)
            self.assertEqual(entry["state"], "alive")

        # the host's own fields satisfy the existing dashboard contract
        base = Dashboard(FleetSim(n_robots=3, seed=1)).snapshot()
        base.update(snap)
        problems: list[str] = []
        dashboard_contract.check_snapshot(base, "edge-telemetry", problems)
        self.assertEqual(problems, [], f"contract violations: {problems[:5]}")

    def test_late_act_and_malformed_counters(self) -> None:
        host = EdgeHost(n_robots=3, seed=1)
        host.run_until(20)
        real = host.collect_actions
        host.collect_actions = lambda: {}
        host.step()
        host.collect_actions = real
        self.assertGreater(host.link()["late_acts"], 0)

        agent = host.agents["R1"]
        agent.deliver_datagrams([b"{not json"])
        self.assertGreaterEqual(agent.malformed, 1)

    def test_radio_degradation_is_visible(self) -> None:
        host = EdgeHost(n_robots=3, seed=1, drop_prob=0.5)
        host.run_until(MAX_TICKS_DEMO)
        link = host.link()
        self.assertGreater(link["dropped"], 0, "a lossy link reported no drops")
        self.assertGreater(link["lost_pct"], 0.0)
        self.assertGreaterEqual(link["lost_pct"], 25.0)
        self.assertLessEqual(link["lost_pct"], 75.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
