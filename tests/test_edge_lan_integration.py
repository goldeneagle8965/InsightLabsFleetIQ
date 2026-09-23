"""The real LAN split: three robots in three processes, over real sockets.

Nothing here is stubbed. Each robot is a separate Python interpreter started with
``scripts/run_edge_agent.py``, it binds its own UDP socket, and the host in this
process talks to it the way it would talk to a Raspberry Pi: ``HELLO_WORLD``,
``OBS`` and ``ACT_RESULT`` out, ``HELLO``/``HEARTBEAT``/``ACT`` back. Peer traffic
(``PEER_STATE``) never touches the host - each robot learns where its peers are
from the roster the host pinned and unicasts to them directly.

What this file proves, and how:

* ``test_three_robots_join_decide_and_see_each_other`` - the full path works:
  three admissions on three separate sockets, every robot deciding every tick,
  every robot hearing the other two *directly* (its own summary reports
  ``peers_observed``), and the host's link ledger reading the robots' own radios.
* ``test_a_lost_robot_is_reported_dead_and_can_rejoin`` - failure and recovery:
  a robot is killed mid-run, the host reports it ``dead`` and holds its cell
  (no ACT means no motion), and the same robot rejoining from the same address is
  neither a duplicate claim nor a rebind - it just starts deciding again.
* ``TestLanAdmissionRules`` - the same reconnect rules driven straight through
  ``_handle_ctrl``, so the refusals are checked deterministically: an impostor
  cannot steal a live id, a rebind needs real silence first, an ``ACT`` from the
  wrong address or for the wrong tick is dropped rather than reused.
* ``TestLanHostSocketRules`` - the rendezvous port is the host's alone (a second
  host must fail, not split the fleet's datagrams) and a host with no robots still
  runs its clock without moving anything.
* ``TestAgentListenPortIsOwned`` - an agent's listen port is its identity too, so
  a second radio on a busy port is refused rather than sharing the datagrams, and
  an adopted agent whose host disappears exits instead of looping forever holding
  the port it bound.
* ``TestAnUnadoptedAgentGivesUp`` - the other half of that exit rule: a robot that
  nobody ever adopts is invisible to the host, so ``--host-timeout`` cannot reach
  it. It gives up on its own deadline and says which address never answered, while
  ``--adopt-timeout 0`` keeps a robot waiting for a host that boots later.

Run: ``python -m unittest tests.test_edge_lan_integration``
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.edge.host import DEAD_TICKS, LAN_REBIND_TICKS, EdgeHost  # noqa: E402
from simulation.edge.protocol import (  # noqa: E402
    CTRL_ACT,
    CTRL_HEARTBEAT,
    CTRL_HELLO,
    head,
)
from simulation.edge.radio import RadioPortInUse, UdpRadio  # noqa: E402
from simulation.edge.session import RemoteAgentSession  # noqa: E402

AGENT_SCRIPT = ROOT / "scripts" / "run_edge_agent.py"
#: Generous: the whole point is that a slow start must not be mistaken for a fault.
JOIN_TIMEOUT_S = 25.0


def free_ports(count: int) -> list[int]:
    """``count`` distinct UDP ports that were free a moment ago.

    The sockets stay open until every port has been handed out, so the OS cannot
    hand the same number back twice inside one call.
    """
    held = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            held.append(sock)
        return [int(sock.getsockname()[1]) for sock in held]
    finally:
        for sock in held:
            sock.close()


class AgentProcess:
    """One robot, in its own interpreter, on its own real socket."""

    def __init__(
        self,
        rid: str,
        host_addr: tuple[str, int],
        listen_port: int,
        work_dir: Path,
        extra_args: list[str] | None = None,
    ) -> None:
        self.rid = rid
        self.host_addr = (str(host_addr[0]), int(host_addr[1]))
        self.listen_port = int(listen_port)
        self.extra_args = list(extra_args or [])
        self.log_path = work_dir / f"{rid}.log"
        self._log = None
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        # A file, not a pipe: an agent that prints more than the pipe buffer holds
        # would block forever if the test only read it at the end.
        self._log = self.log_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(AGENT_SCRIPT),
                "--id", self.rid,
                "--host", f"{self.host_addr[0]}:{self.host_addr[1]}",
                "--listen", f"127.0.0.1:{self.listen_port}",
                "--max-ticks", "4000",
                *self.extra_args,
            ],
            cwd=str(ROOT),
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

    @property
    def pid(self) -> int:
        return -1 if self.proc is None else int(self.proc.pid)

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        """Hard kill: the network does not get a goodbye message either."""
        if self.is_running():
            self.proc.kill()
        self.wait(timeout=10.0)

    def wait(self, timeout: float = 10.0) -> None:
        if self.proc is None:
            return
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:  # pragma: no cover - only on a hung agent
            self.proc.kill()
            self.proc.wait(timeout=5.0)
        if self._log is not None:
            self._log.flush()

    def stop(self) -> None:
        self.kill()
        if self._log is not None:
            self._log.close()
            self._log = None

    def lines(self) -> list[str]:
        if not self.log_path.exists():
            return []
        return self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()

    def summary(self) -> dict:
        """The ``summary {json}`` line the agent prints on exit."""
        for line in reversed(self.lines()):
            if line.startswith("summary "):
                return json.loads(line[len("summary "):])
        raise AssertionError(f"no summary line from {self.rid}; log:\n" + "\n".join(self.lines()[-20:]))


class FleetFixture(unittest.TestCase):
    """A LAN host in this process plus one real agent process per robot."""

    def make_fleet(self, n_robots: int = 3) -> EdgeHost:
        self.work_dir = Path(tempfile.mkdtemp(prefix="edge-lan-"))
        self.addCleanup(self._cleanup_work_dir)
        ports = free_ports(n_robots + 1)
        self.host_addr = ("127.0.0.1", ports[0])
        self.listen_ports = {f"R{i + 1}": ports[i + 1] for i in range(n_robots)}
        host = EdgeHost(
            n_robots=n_robots,
            seed=1,
            transport="lan",
            udp_host=self.host_addr[0],
            udp_base_port=self.host_addr[1],
        )
        self.addCleanup(host.close)
        self.agents: dict[str, AgentProcess] = {}
        return host

    def _cleanup_work_dir(self) -> None:
        for agent in getattr(self, "agents", {}).values():
            agent.stop()
        keep = getattr(self, "work_dir", None)
        if keep is not None:
            for path in keep.glob("*.log"):
                try:
                    path.unlink()
                except OSError:  # pragma: no cover - best effort
                    pass
            try:
                keep.rmdir()
            except OSError:  # pragma: no cover - best effort
                pass

    def launch(self, rid: str, extra_args: list[str] | None = None) -> AgentProcess:
        previous = self.agents.get(rid)
        if previous is not None:  # a relaunch replaces the dead process
            previous.stop()
        agent = AgentProcess(
            rid, self.host_addr, self.listen_ports[rid], self.work_dir, extra_args
        )
        agent.start()
        self.agents[rid] = agent
        return agent


class TestThreeRobotsOverRealSockets(FleetFixture):
    def test_three_robots_join_decide_and_see_each_other(self) -> None:
        host = self.make_fleet(3)
        self.assertEqual(host.link()["transport"], "lan")
        self.assertEqual(host.link()["admissions"], 0)
        for rid in host.order:
            self.assertEqual(host.agent_state(rid), "boot")

        for rid in host.order:
            self.launch(rid)

        # The join window is the only thing that waits for the fleet.
        joined = host.await_agents(JOIN_TIMEOUT_S)
        self.assertEqual(joined, 3, "every robot must check in over its own socket")
        self.assertEqual(host.admissions, 3)
        self.assertEqual(host.duplicate_claims, 0)
        self.assertEqual(host.rebinds, 0)
        for rid in host.order:
            session = host.agents[rid]
            self.assertIsInstance(session, RemoteAgentSession)
            self.assertIsNotNone(session.addr)
            self.assertEqual(session.listen_port, self.listen_ports[rid])
            self.assertEqual(host.agent_state(rid), "alive")

        for _ in range(60):
            host.step()

        link = host.link()
        self.assertGreater(link["ctrl_recv"], 0, "the host received real CTRL frames")
        self.assertGreater(link["mean_rtt_ms"], 0.0, "the OBS -> ACT round trip was measured")
        self.assertEqual(link["unaddressed"], 0, "every robot was addressable from tick 1")
        self.assertEqual(link["foreign"], 0, "no CTRL arrived from anyone but the robots")
        self.assertGreater(link["sent"], 0, "the robots reported their own radio ledgers")
        self.assertEqual(sorted(entry[0] for entry in link["roster"]), sorted(host.order))
        for entry in link["roster"]:
            self.assertEqual(entry[1], "127.0.0.1")
            self.assertEqual(entry[2], self.listen_ports[entry[0]])

        for rid in host.order:
            telemetry = host.agents[rid].telemetry()
            self.assertGreater(telemetry["acts_sent"], 0, f"{rid} decided for itself")
            self.assertGreater(telemetry["heartbeats"], 0, f"{rid} kept its own liveness up")
            self.assertGreaterEqual(telemetry["last_seen_tick"], 0)

        # Shutdown is a clock message, so the agents exit and write their summaries.
        host.stop()
        for agent in self.agents.values():
            agent.wait(timeout=15.0)
            self.assertFalse(agent.is_running(), f"{agent.rid} did not stop on SHUTDOWN")

        for rid in host.order:
            summary = self.agents[rid].summary()
            self.assertTrue(summary["adopted"], f"{rid} never adopted the geometry")
            self.assertGreater(summary["acts_sent"], 0)
            self.assertGreater(summary["control_frames"], 0)
            self.assertEqual(
                summary["peers_known"],
                sorted(other for other in host.order if other != rid),
                f"{rid} did not learn the full peer table",
            )
            # Heard directly, robot to robot - the one fact the host never relays.
            self.assertGreaterEqual(
                len(summary["peers_observed"]), 2,
                f"{rid} never heard its peers directly: {summary['peers_observed']}",
            )

    def test_a_lost_robot_is_reported_dead_and_can_rejoin(self) -> None:
        host = self.make_fleet(3)
        for rid in host.order:
            self.launch(rid)
        self.assertEqual(host.await_agents(JOIN_TIMEOUT_S), 3)
        for _ in range(20):
            host.step()

        victim = self.agents["R3"]
        acts_before = host.agents["R3"].telemetry()["acts_sent"]
        self.assertGreater(acts_before, 0)
        victim.kill()

        for _ in range(DEAD_TICKS + 3):
            host.step()
        self.assertEqual(host.agent_state("R3"), "dead")
        self.assertLess(host._last_act_tick["R3"], host.tick - DEAD_TICKS)

        # A robot with no ACT holds still: the host moves nothing on its behalf.
        held = tuple(host.world.robots["R3"])
        after_death = host.agents["R3"].telemetry()["acts_sent"]
        for _ in range(6):
            host.step()
        self.assertEqual(tuple(host.world.robots["R3"]), held)
        self.assertEqual(host.agents["R3"].telemetry()["acts_sent"], after_death)

        # The same robot comes back on the same address.
        self.launch("R3")
        deadline = time.time() + JOIN_TIMEOUT_S
        while time.time() < deadline and host.agents["R3"].telemetry()["acts_sent"] == after_death:
            host.step()
        self.assertGreater(
            host.agents["R3"].telemetry()["acts_sent"], after_death,
            "the rejoined robot never started deciding again",
        )
        self.assertEqual(host.agent_state("R3"), "alive")
        # Same address, same robot: this is a resume, not a new admission and not a
        # hijack attempt - and the counters have to say so.
        self.assertEqual(host.admissions, 3)
        self.assertEqual(host.rebinds, 0)
        self.assertEqual(host.duplicate_claims, 0)


class TestLanHostSocketRules(unittest.TestCase):
    """The rendezvous port belongs to one host, and to nothing else."""

    def test_the_lan_port_is_bound_exactly_and_never_shared(self) -> None:
        port = free_ports(1)[0]
        first = EdgeHost(
            n_robots=1, seed=1, transport="lan", udp_host="127.0.0.1", udp_base_port=port
        )
        self.addCleanup(first.close)
        self.assertIsNotNone(first.sock)
        self.assertEqual(int(first.sock.getsockname()[1]), port)
        # A second host on the same port must fail loudly. Silently sharing it would
        # split the fleet's datagrams between two worlds, which looks exactly like
        # three robots that cannot connect.
        with self.assertRaises(OSError):
            EdgeHost(n_robots=1, seed=1, transport="lan", udp_host="127.0.0.1", udp_base_port=port)

    def test_a_lan_host_with_no_robots_still_runs_its_clock(self) -> None:
        host = EdgeHost(
            n_robots=2, seed=1, transport="lan", udp_host="127.0.0.1", udp_base_port=free_ports(1)[0]
        )
        self.addCleanup(host.close)
        self.assertEqual(host.await_agents(0.2), 0, "the join wait is bounded")
        held = dict(host.world.robots)
        for _ in range(3):
            host.step()
        self.assertEqual(host.world.robots, held)
        self.assertEqual([host.agent_state(rid) for rid in host.order], ["boot", "boot"])


class TestLanAdmissionRules(unittest.TestCase):
    """Reconnect rules driven straight through ``_handle_ctrl``, deterministically."""

    def setUp(self) -> None:
        self.host = EdgeHost(
            n_robots=1,
            seed=1,
            transport="lan",
            udp_host="127.0.0.1",
            udp_base_port=free_ports(1)[0],
        )
        self.addCleanup(self.host.close)
        self.pinned = ("127.0.0.1", 9201)
        self.impostor = ("127.0.0.1", 9300)

    def hello(self, listen_port: int = 9201) -> dict:
        return head(
            CTRL_HELLO,
            "R1",
            self.host.tick,
            robot_id="R1",
            policy="ubpa",
            priority=1,
            agent_version="edge-1",
            listen_port=listen_port,
        )

    def act(self, tick: int) -> dict:
        return head(CTRL_ACT, "R1", tick, dest=[3, 3], phase="MOVING", task=None)

    def heartbeat(self) -> dict:
        return head(CTRL_HEARTBEAT, "R1", self.host.tick, battery=90.0, current_task=None)

    def admitted(self) -> RemoteAgentSession:
        self.assertTrue(self.host._handle_ctrl(self.hello(), self.pinned))
        self.assertEqual(self.host.admissions, 1)
        session = self.host.agents["R1"]
        self.assertEqual(session.addr, self.pinned)
        self.assertEqual(self.host._addr["R1"], self.pinned)
        return session

    def test_a_robot_is_admitted_by_the_source_address_of_its_own_hello(self) -> None:
        session = self.admitted()
        self.assertTrue(session.ready)
        self.assertEqual(session.listen_port, 9201)
        self.assertEqual(self.host.agent_state("R1"), "alive")
        self.assertEqual(self.host.link()["roster"], [["R1", "127.0.0.1", 9201]])

    def test_a_second_claim_on_a_live_robot_is_refused(self) -> None:
        session = self.admitted()
        self.assertFalse(self.host._handle_ctrl(self.hello(listen_port=9300), self.impostor))
        self.assertEqual(self.host.duplicate_claims, 1)
        self.assertEqual(self.host.rebinds, 0)
        self.assertEqual(session.addr, self.pinned, "the pinned address must survive the claim")
        self.assertEqual(session.listen_port, 9201)
        # A keepalive from the real robot is still accepted, and is not a new admission.
        self.assertTrue(self.host._handle_ctrl(self.hello(), self.pinned))
        self.assertEqual(self.host.admissions, 1)

    def test_a_rebind_needs_real_silence_first(self) -> None:
        session = self.admitted()
        self.host.tick += LAN_REBIND_TICKS
        self.assertFalse(self.host._handle_ctrl(self.hello(listen_port=9300), self.impostor))
        self.assertEqual(self.host.duplicate_claims, 1)
        self.assertEqual(session.addr, self.pinned)

        self.host.tick += 1  # now the id has been quiet for longer than the window
        self.assertTrue(self.host._handle_ctrl(self.hello(listen_port=9300), self.impostor))
        self.assertEqual(self.host.rebinds, 1)
        self.assertEqual(session.addr, self.impostor)
        self.assertEqual(self.host._addr["R1"], self.impostor)
        self.assertEqual(self.host.admissions, 1, "a rebind is not a new robot")
        self.assertEqual(
            self.host.link()["roster"], [["R1", "127.0.0.1", 9300]], "peers are told the new address"
        )
        self.assertEqual(self.host.agent_state("R1"), "alive")

    def test_an_act_from_another_address_is_refused(self) -> None:
        session = self.admitted()
        self.host.tick = 5
        self.assertFalse(self.host._handle_ctrl(self.act(5), self.impostor))
        self.assertEqual(self.host.foreign, 1)
        self.assertFalse(session.has_act(5))
        self.assertTrue(self.host._handle_ctrl(self.act(5), self.pinned))
        self.assertTrue(session.has_act(5))

    def test_an_act_for_another_tick_is_never_reused(self) -> None:
        session = self.admitted()
        self.host.tick = 5
        self.assertTrue(self.host._handle_ctrl(self.act(5), self.pinned))
        # The host advances; the stale ACT must not be applied to the new tick.
        self.host.tick = 6
        self.assertFalse(session.has_act(6))
        self.assertIsNone(session.tick_once(6))
        self.assertEqual(session.acts_missing, 1)
        self.assertIsNone(session.tick_once(5), "a dropped tick stays dropped")
        self.assertTrue(self.host._handle_ctrl(self.act(6), self.pinned))
        self.assertEqual(session.tick_once(6)["dest"], [3, 3])

    def test_a_heartbeat_keeps_a_holding_robot_alive(self) -> None:
        self.admitted()
        self.assertTrue(self.host._handle_ctrl(self.heartbeat(), self.pinned))
        self.assertEqual(self.host.agents["R1"].telemetry()["heartbeats"], 1)
        for _ in range(DEAD_TICKS):
            self.host.tick += 1
        self.assertEqual(self.host.agent_state("R1"), "alive", "a heartbeat is a sign of life")
        for _ in range(DEAD_TICKS + 1):
            self.host.tick += 1
        self.assertEqual(self.host.agent_state("R1"), "dead", "silence must expire")
        self.assertTrue(self.host._handle_ctrl(self.heartbeat(), self.pinned))
        self.assertEqual(self.host.agent_state("R1"), "alive")

    def test_an_unknown_robot_id_is_counted_not_seated(self) -> None:
        self.admitted()
        stranger = head(
            CTRL_HELLO, "R9", self.host.tick, robot_id="R9", policy="ubpa", priority=1,
            agent_version="edge-1", listen_port=9400,
        )
        self.assertFalse(self.host._handle_ctrl(stranger, self.impostor))
        self.assertEqual(self.host.foreign, 1)
        self.assertEqual(self.host.admissions, 1)
        self.assertEqual(list(self.host.order), ["R1"])

    def test_a_host_with_no_agents_holds_every_cell(self) -> None:
        positions = dict(self.host.world.robots)
        for _ in range(3):
            self.host.step()
        self.assertEqual(self.host.world.robots, positions)
        self.assertEqual(self.host.agent_state("R1"), "boot")
        self.assertGreater(self.host.link()["unaddressed"], 0)


class TestAgentListenPortIsOwned(FleetFixture):
    """One robot id, one port: the agent's side of the same rule.

    Refusing to share the host's rendezvous port is not enough. An agent's listen
    port decides which process the host answers, so two agents on one port means
    the host admits the id while the ``OBS`` frames are split between the two
    processes - which reads as "admitted, but never decides". Both halves fail
    loudly here.
    """

    #: Silence window used by the sub-process in the last test, in seconds.
    SILENCE_WINDOW_S = 2.0

    def test_a_radio_refuses_a_port_someone_else_already_listens_on(self) -> None:
        port = free_ports(1)[0]
        squatter = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(squatter.close)
        # Deliberately the pre-fix socket options: SO_REUSEADDR is what used to let
        # a second agent bind an address the first one already owned.
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(("127.0.0.1", port))
        with self.assertRaises(RadioPortInUse) as caught:
            UdpRadio("R1", listen=("127.0.0.1", port))
        self.assertIn("already in use", str(caught.exception))

    def test_a_second_agent_process_exits_with_a_usable_message(self) -> None:
        self.make_fleet(2)
        self._await_listening(self.launch("R1"))
        # Same port, different robot id: this process must die, not shadow R1.
        duplicate = AgentProcess(
            "R1copy", self.host_addr, self.listen_ports["R1"], self.work_dir
        )
        self.addCleanup(duplicate.stop)
        duplicate.start()
        duplicate.wait(timeout=20.0)
        self.assertEqual(duplicate.proc.returncode, 2, "\n".join(duplicate.lines()))
        log = "\n".join(duplicate.lines())
        self.assertIn("already in use", log, "the refusal has to say why")
        self.assertIn("--listen", log, "and how to fix it")

    def test_an_adopted_agent_exits_when_the_host_goes_silent(self) -> None:
        host = self.make_fleet(1)
        agent = self.launch("R1", ["--host-timeout", str(self.SILENCE_WINDOW_S)])
        self.assertEqual(host.await_agents(JOIN_TIMEOUT_S), 1)
        for _ in range(10):
            host.step()
            time.sleep(0.01)
        self.assertGreater(host.agents["R1"].telemetry()["acts_sent"], 0)
        self.assertTrue(agent.is_running(), "a robot with a live host stays up")

        host.close()  # the only thing that tells the robot the clock is gone
        agent.wait(timeout=30.0)
        self.assertFalse(agent.is_running(), "an adopted agent must not outlive its host")
        self.assertIn("host silent", "\n".join(agent.lines()))
        summary = agent.summary()
        self.assertTrue(summary["adopted"])
        self.assertGreater(summary["acts_sent"], 0)
        # ...and it says how long it waited, so a silent-host exit is not mistaken
        # for a crash when the log is read afterwards.
        self.assertGreaterEqual(summary["host_silence_s"], self.SILENCE_WINDOW_S)

    def _await_listening(self, agent: AgentProcess, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any("listening on" in line for line in agent.lines()):
                return
            time.sleep(0.05)
        raise AssertionError(f"{agent.rid} never bound its socket:\n" + "\n".join(agent.lines()))


class TestAnUnadoptedAgentGivesUp(FleetFixture):
    """A robot waiting for a host that is not there must not wait forever.

    The silence rule only covers a robot the host has already admitted - an
    unadopted one is invisible to the host, so nothing on the host side can end it.
    Without a deadline it spends the whole tick budget (eight minutes at the demo
    rate) holding its port, which is how stray agents pile up between runs.
    """

    #: How long the sub-process is told to wait for a host before giving up.
    ADOPT_WINDOW_S = 2.0

    def _prepare(self) -> None:
        """A work dir plus the two ports: one host that is not up, one robot."""
        self.work_dir = Path(tempfile.mkdtemp(prefix="edge-lan-"))
        self.addCleanup(self._cleanup_work_dir)
        self.agents = {}
        host_port, agent_port = free_ports(2)
        self.host_addr = ("127.0.0.1", host_port)
        self.listen_ports = {"R1": agent_port}

    def test_an_agent_nobody_adopts_gives_up_and_exits(self) -> None:
        self._prepare()
        agent = self.launch("R1", ["--adopt-timeout", str(self.ADOPT_WINDOW_S)])
        agent.wait(timeout=25.0)
        log = "\n".join(agent.lines())
        self.assertFalse(agent.is_running(), "a robot nobody adopts must not hold its port")
        self.assertEqual(agent.proc.returncode, 3, log)
        self.assertIn("no HELLO_WORLD", log, "it has to name the thing that never came")
        # The address it was aiming at, and the way out of the situation.
        self.assertIn(str(self.host_addr[1]), log)
        self.assertIn("--adopt-timeout", log)
        summary = agent.summary()
        self.assertFalse(summary["adopted"])
        self.assertGreater(summary["hello_sent"], 0, "it should have kept announcing itself")
        self.assertEqual(summary["acts_sent"], 0, "nobody asked it to decide anything")

    def test_adopt_timeout_zero_waits_for_a_host_that_is_late(self) -> None:
        self._prepare()
        # The deployment case the deadline is not allowed to break: the robot is
        # switched on first and the host follows.
        agent = self.launch("R1", ["--adopt-timeout", "0", "--host-timeout", "3"])
        time.sleep(3.0)
        self.assertTrue(agent.is_running(), "waiting for a late host is not a fault")
        self.assertNotIn("no HELLO_WORLD", "\n".join(agent.lines()))

        host = EdgeHost(
            n_robots=1,
            seed=1,
            transport="lan",
            udp_host=self.host_addr[0],
            udp_base_port=self.host_addr[1],
        )
        self.addCleanup(host.close)
        self.assertEqual(host.await_agents(JOIN_TIMEOUT_S), 1, "the late host adopts it")
        for _ in range(10):
            host.step()
            time.sleep(0.01)
        self.assertGreater(
            host.agents["R1"].telemetry()["acts_sent"],
            0,
            "and it starts deciding the moment the host arrives",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
