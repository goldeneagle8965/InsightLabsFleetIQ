"""One robot's decision stack, off the host machine.

``EdgeAgent`` is the whole of the edge abstraction: a stock
``simulation.robot.Robot`` plus the two things it no longer gets from its own
process - a world it may only *observe*, and a radio it shares with its peers.
It plans, bids, claims, yields and wins exactly as it does inside ``FleetSim``,
because it is the same object with a different ``world`` and ``bus``.

What the agent does **not** do
-----------------------------
It does not ask the host what to do. The only inbound CTRL messages it reacts to
are ``HELLO_WORLD`` (static geometry), ``OBS`` (this robot's sensor picture),
``ACT_RESULT`` (the authoritative position after physics) and ``SHUTDOWN``. Every
decision travels the other way, as an ``ACT`` describing the robot's own
intention, and peer traffic bypasses the host entirely.

The one authority the host does hold is the clock: ``OBS.tick`` is adopted
monotonically (``self.tick = max(self.tick, obs.tick)``) because ``robot.py``
compares a peer's ``timestamp`` against its own tick for staleness and claim
expiry, so the numbers must be comparable. A tick is a clock, not a decision.
"""

from __future__ import annotations

import os
import sys
import time
from collections import deque
from typing import Any, Iterable

try:  # POSIX only: Windows ships no ``resource`` module.
    import resource as _resource
except ImportError:  # pragma: no cover - platform dependent
    _resource = None  # type: ignore[assignment]

from simulation.config import CoordinationFlags
from simulation.edge.protocol import (
    CTRL_ACT,
    CTRL_ACT_RESULT,
    CTRL_HEARTBEAT,
    CTRL_HELLO,
    CTRL_HELLO_WORLD,
    CTRL_OBS,
    CTRL_SHUTDOWN,
    ChunkAssembler,
    MessageTooLarge,
    ProtocolError,
    chunk_message,
    decode_safe,
    encode,
    head,
)
from simulation.edge.worldview import RemoteWorldView
from simulation.robot import Robot

Pos = tuple[int, int]

#: Reported in ``HELLO`` so the host can tell which build answered.
AGENT_VERSION = "edge-1"
#: Heartbeat cadence, matching doc 04 §1.6 (8 ticks = 1 s at the demo rate).
HEARTBEAT_TICKS = 8
#: Ring size for the local step-time samples behind ``step_ms_p95``.
_STEP_SAMPLES = 64


def _split_addr(raw: Any) -> tuple[str, int] | None:
    """Accept ``[host, port]``, ``(host, port)`` or ``"host:port"``; ``None`` if unusable."""
    if raw is None:
        return None
    if isinstance(raw, str):
        host, _, port = raw.rpartition(":")
        raw = [host, port]
    try:
        host = str(raw[0]).strip()
        port = int(raw[1])
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    return None if not host else (host, port)


class EdgeAgent:
    """``Robot`` + a remote world view + a radio. No scheduler, no host orders."""

    def __init__(
        self,
        robot_id: str,
        pos: Pos | None = None,
        radio: Any = None,
        *,
        priority: int = 1,
        policy: str = "ubpa",
        flags: CoordinationFlags | None = None,
        view: Any = None,
        agent_version: str = AGENT_VERSION,
    ) -> None:
        self.id = str(robot_id)
        self.priority = int(priority)
        self.policy = str(policy)
        self.flags = flags or CoordinationFlags()
        self.agent_version = str(agent_version)
        self.radio = radio
        self.view = view
        self.pos: Pos | None = None if pos is None else (int(pos[0]), int(pos[1]))
        #: The unmodified robot. ``None`` until geometry *and* a spawn cell are known,
        #: which is the honest state of a Pi that has booted but not yet been answered.
        self.robot: Robot | None = None
        self.assembler = ChunkAssembler()
        self.world_epoch = 0
        self.tick = 0
        self.running = True
        self.started_ms = time.perf_counter()
        self.control_frames = 0
        self.malformed = 0
        self.unknown_frames = 0
        self.oversize = 0
        self.pending_result: dict[str, Any] | None = None
        self.last_seen_tick = -1
        self.hello_sent = 0
        self.hello_ok = False
        self.acts_sent = 0
        self.acts_missing = 0
        self.ctrl_sent = 0
        #: Robot id -> where that peer can be reached, as published by the host in
        #: ``HELLO_WORLD.peers``. This is the robot's own peer table: the host
        #: forwards an address, and the peer traffic still goes straight between
        #: robots (doc 03 §2).
        self.peer_addrs: dict[str, tuple[str, int]] = {}
        self.peers_learned = 0
        #: ``sent_ms`` of the newest OBS, echoed back in ACT so the host can measure
        #: a real round trip instead of estimating one (doc 04 §1.4).
        self.obs_sent_ms: float | None = None
        #: Wall clock of the newest CTRL frame the host sent. The clock the agent
        #: obeys is the host's, so a host that has gone away has to be noticed in
        #: wall-clock time: an adopted robot waiting for a tick that will never
        #: come would otherwise loop forever holding its port (doc 04 §3.3).
        self.last_host_ms = time.perf_counter()
        self.step_ms: deque[float] = deque(maxlen=_STEP_SAMPLES)

    # ----- construction -------------------------------------------------

    @property
    def ready(self) -> bool:
        return self.robot is not None

    def install(self, view: Any = None, pos: Pos | None = None) -> Robot | None:
        """Build the robot once the world view and the spawn cell are both known.

        The delayed construction is what lets the very same class run three ways:
        in-process (host supplies ``pos`` and a ``RemoteWorldView``), on a Pi
        (``pos`` arrives in ``HELLO_WORLD.spawn``) and locally with no host at all
        (host supplies a ``LocalWorldView`` over a real ``Warehouse``).
        """
        if view is not None:
            self.view = view
        if pos is not None:
            self.pos = (int(pos[0]), int(pos[1]))
        if self.robot is not None:
            return self.robot
        if self.view is None or self.pos is None or self.radio is None:
            return None
        # Positional on purpose: this is ``robot.py:100-109`` untouched.
        self.robot = Robot(
            self.id,
            self.pos,
            self.view,
            self.radio,
            priority=self.priority,
            policy=self.policy,
            flags=self.flags,
        )
        self.pos = self.robot.pos
        return self.robot

    # ----- inbound CTRL -------------------------------------------------

    def deliver_datagrams(self, payloads: Iterable[bytes | str]) -> int:
        """Decode, reassemble and apply a batch of raw datagrams.

        Malformed frames and half-assembled chunks are counted and dropped, never
        raised: a hostile or lossy link must not be able to stop the decision loop
        (doc 03 §7, doc 04 §3.2).
        """
        decoded: list[dict[str, Any]] = []
        for raw in payloads:
            msg = decode_safe(raw)
            if msg is None:
                self.malformed += 1
                continue
            decoded.append(msg)
        return self.apply_frames(decoded)

    def apply_frames(self, messages: Iterable[dict[str, Any]]) -> int:
        """Reassemble already-decoded frames and route each complete message.

        Every inbound path goes through here, because a ``HELLO_WORLD`` arrives as
        several ``CHUNK`` envelopes and a plain message passes through the assembler
        unchanged. Returns how many messages were consumed.
        """
        applied = 0
        for msg in messages:
            try:
                full = self.assembler.feed(msg)
            except ProtocolError:
                self.malformed += 1
                continue
            if full is None:  # a CHUNK envelope whose siblings are still missing
                continue
            if self.apply_control(full):
                applied += 1
            else:
                self.unknown_frames += 1
        return applied

    def apply_control(self, msg: dict[str, Any]) -> bool:
        """Route one decoded CTRL message. Returns ``True`` if it was consumed."""
        if not isinstance(msg, dict):
            return False
        kind = str(msg.get("type") or "")
        if kind == CTRL_HELLO_WORLD:
            self.on_world(msg)
        elif kind == CTRL_OBS:
            self.on_observation(msg)
        elif kind == CTRL_ACT_RESULT:
            self.on_result(msg)
        elif kind == CTRL_SHUTDOWN:
            self.running = False
        else:
            return False
        self.control_frames += 1
        self.last_host_ms = time.perf_counter()
        return True

    def on_world(self, msg: dict[str, Any]) -> bool:
        """Adopt static geometry from ``HELLO_WORLD`` and then build the robot."""
        world = msg.get("world") or msg
        if self.view is None:
            self.view = RemoteWorldView.from_world_message(msg)
        elif hasattr(self.view, "apply_world"):
            self.view.apply_world(msg)
        self.world_epoch += 1
        # The peer table travels with the geometry. Adopting it *before* the robot
        # is built means the very first broadcast already reaches everyone the host
        # has seen check in, whichever order the robots booted in.
        self.learn_peers(msg.get("peers"))
        if self.pos is None:
            spawn = msg.get("spawn") or world.get("spawn")
            if spawn:
                self.pos = (int(spawn[0]), int(spawn[1]))
        self.install()
        self.last_seen_tick = max(self.last_seen_tick, int(msg.get("tick", 0)))
        return True

    def learn_peers(self, roster: Any) -> int:
        """Adopt the peer addresses the host has pinned, and return how many are new.

        A robot can only unicast to peers it has been told about (``UdpRadio.peers``
        is its whole routing table) and the host is never the relay, so reaching a
        peer needs this one fact: *where* it is. The host knows it because it read the
        source address of that robot's own datagram. Accepts the wire form
        (``[[id, host, port], ...]``) or a ``{id: [host, port]}`` mapping.
        """
        if not roster:
            return 0
        if isinstance(roster, dict):
            entries: Iterable[Any] = [(rid, raw) for rid, raw in roster.items()]
        else:
            entries = [(entry[0], entry[1:]) for entry in roster if entry]
        learned = 0
        for rid, raw in entries:
            rid = str(rid)
            if rid == self.id:
                continue  # a robot is not its own peer
            addr = _split_addr(raw)
            if addr is None or addr[1] <= 0:
                # No reachable port means no peer channel. Better to know nothing
                # than to broadcast into a port nobody is listening on.
                continue
            previous = self.peer_addrs.get(rid)
            self.peer_addrs[rid] = addr
            if previous is None:
                self.peers_learned += 1
                learned += 1
            add_peer = getattr(self.radio, "add_peer", None)
            if add_peer is not None:
                add_peer(rid, addr)
        return learned

    def on_observation(self, msg: dict[str, Any]) -> bool:
        """Adopt the host's tick and refresh the local obstacle picture."""
        tick = int(msg.get("tick", self.tick))
        self.tick = max(self.tick, tick)
        self.last_seen_tick = max(self.last_seen_tick, tick)
        sent_ms = msg.get("sent_ms")
        if sent_ms is not None:
            self.obs_sent_ms = float(sent_ms)
        take_obs = getattr(self.view, "apply_obs", None)
        if take_obs is not None:
            take_obs(msg)
        return True

    def on_result(self, msg: dict[str, Any]) -> bool:
        """Apply the authoritative position through the robot's own ``note_result``.

        This is the only place the agent learns where it really is. It runs before
        the next ``step()``, which is exactly where ``FleetSim`` calls it.
        """
        pos = msg.get("position") or msg.get("pos")
        if pos is None:
            self.pending_result = dict(msg)
            return False
        self.pos = (int(pos[0]), int(pos[1]))
        if self.robot is not None:
            self.robot.note_result(self.pos)
        self.last_seen_tick = max(self.last_seen_tick, int(msg.get("tick", self.tick)))
        self.pending_result = None
        return True

    # ----- transport ----------------------------------------------------

    def pump(self) -> int:
        """Drain the real transport, if this agent has one.

        Duck-typed on ``take_control``: the in-process ``LoopbackRadio`` has no
        such method, because the host owns ``SharedMedium.tick`` and the two must
        not advance the same medium twice.
        """
        radio = self.radio
        take = getattr(radio, "take_control", None)
        if radio is None or take is None:
            return 0
        tick_method = getattr(radio, "tick", None)
        if tick_method is not None:
            tick_method(self.tick)
        # ``take_control`` hands back *decoded* datagrams, not raw bytes, so they go
        # through the same assembler the in-process path uses: a HELLO_WORLD is
        # several CHUNK envelopes and must be put back together before it can be
        # applied. Without this the robot would hear its geometry as garbage.
        return self.apply_frames(take())

    def host_silence_s(self) -> float:
        """Seconds since the host last said anything, or since this agent booted."""
        return max(0.0, time.perf_counter() - self.last_host_ms)

    def send_to_host(self, msg: dict[str, Any]) -> bool:
        """Send one CTRL message to the host. No-op on the in-process medium."""
        addr = getattr(self.radio, "host_addr", None)
        sock = getattr(self.radio, "sock", None)
        if addr is None or sock is None:
            return False
        self.ctrl_sent += 1
        try:
            for part in chunk_message(msg):
                sock.sendto(part, addr)
        except (MessageTooLarge, OSError):
            self.oversize += 1
            return False
        return True

    # ----- outbound CTRL ------------------------------------------------

    def hello(self, tick: int | None = None) -> dict[str, Any]:
        """The boot/keepalive handshake (doc 04 §1.1)."""
        self.hello_sent += 1
        port = getattr(self.radio, "port", None)
        return head(
            CTRL_HELLO,
            self.id,
            self.tick if tick is None else int(tick),
            robot_id=self.id,
            policy=self.policy,
            priority=self.priority,
            agent_version=self.agent_version,
            listen_port=None if port is None else int(port),
        )

    def act_message(self, tick: int, intention: Pos) -> dict[str, Any]:
        """``ACT`` — the robot's own intention, verbatim from ``_choose_move``."""
        robot = self.robot
        return head(
            CTRL_ACT,
            self.id,
            tick,
            dest=[int(intention[0]), int(intention[1])],
            phase=getattr(robot, "phase", "IDLE"),
            task=getattr(robot, "current_task", None),
            policy=self.policy,
            obs_ms=self.obs_sent_ms,
            world_epoch=self.world_epoch,
        )

    def tick_once(self, tick: int) -> dict[str, Any] | None:
        """One decision tick: drain control, step the robot, publish the intention.

        Returns the ``ACT`` payload, which the in-process host reads directly. Over
        UDP the same payload has already been transmitted to the host, so the
        return value is only a convenience for tests.
        """
        self.tick = max(self.tick, int(tick))
        self.pump()
        if self.robot is None:
            self.acts_missing += 1
            if not self.install():
                return None
        if self.pending_result is not None:  # an ACT_RESULT that arrived with no coords
            deferred = self.pending_result
            self.robot  # noqa: B018 - the robot exists by now; keep the read explicit
            self.on_result(deferred)
        t0 = time.perf_counter()
        intention = self.robot.step(self.tick)
        self.step_ms.append((time.perf_counter() - t0) * 1000.0)
        self.acts_sent += 1
        msg = self.act_message(self.tick, intention)
        self.send_to_host(msg)
        return msg

    def send_heartbeat(self, tick: int | None = None) -> dict[str, Any]:
        """Emit a heartbeat; the host records it, the wire carries it over UDP."""
        msg = self.heartbeat(self.tick if tick is None else int(tick))
        self.send_to_host(msg)
        return msg

    def heartbeat(self, tick: int | None = None) -> dict[str, Any]:
        """Liveness plus the counters the host cannot compute without the robot.

        ``known_tasks``/``task_holders``/``current_task`` are what let the host keep
        the spectator task board honest when the robots live on other machines
        (doc 06 F1-F3). Nothing in here is an instruction.
        """
        robot = self.robot
        counters: dict[str, Any] = {}
        known: dict[str, str] = {}
        holders: dict[str, str] = {}
        if robot is not None:
            counters = {
                "wait_ticks": int(robot.wait_ticks),
                "move_ticks": int(robot.move_ticks),
                "distance": float(robot.distance),
                "deadlock_events": int(robot.deadlock_events),
                "stall_handoffs": int(robot.stall_handoffs),
                "tasks_done": int(robot.tasks_done),
                "step_n": int(robot.step_n),
                "step_ms_sum": float(robot.step_ms_sum),
            }
            known = {tid: kt.status for tid, kt in robot.known_tasks.items()}
            holders = {tid: kt.holder for tid, kt in robot.known_tasks.items() if kt.holder}
        radio = self._radio_counters()
        return head(
            CTRL_HEARTBEAT,
            self.id,
            self.tick if tick is None else int(tick),
            uptime_s=round(time.perf_counter() - self.started_ms, 2),
            step_ms_p95=round(self.step_ms_p95, 4),
            sent=radio["sent"],
            recv=radio["recv"],
            lost_est=radio["lost_est"],
            policy=self.policy,
            phase=getattr(robot, "phase", "BOOT"),
            battery=None if robot is None else round(robot.battery, 2),
            current_task=getattr(robot, "current_task", None),
            known_tasks=known,
            task_holders=holders,
            counters=counters,
            world_epoch=self.world_epoch,
            peer_hello_ok=self.hello_ok,
        )

    # ----- telemetry ----------------------------------------------------

    @property
    def step_ms_p95(self) -> float:
        if not self.step_ms:
            return 0.0
        values = sorted(self.step_ms)
        index = min(len(values) - 1, int(round(0.95 * (len(values) - 1))))
        return float(values[index])

    def _radio_counters(self) -> dict[str, float]:
        radio = self.radio
        stats = radio.stats() if hasattr(radio, "stats") else {}
        delivered = int(stats.get("delivered", 0))
        dropped = int(stats.get("dropped", 0))
        return {
            "delivered": float(delivered),
            "dropped": float(dropped),
            # "sent" is the transport's own ledger: packets it accepted for
            # delivery plus the ones it threw away. The agent cannot count peer
            # broadcasts itself without wrapping Robot.bus, and this definition is
            # the one the loss statistic actually needs.
            "sent": float(delivered + dropped),
            "recv": float(delivered),
            "lost_est": float(dropped),
            "mean_latency": float(stats.get("mean_latency", 0.0)),
        }

    def telemetry(self) -> dict[str, Any]:
        """Diagnostics only. Nothing on this page is fed back into a decision."""
        robot = self.robot
        radio = self._radio_counters()
        return {
            "id": self.id,
            "policy": self.policy,
            "priority": self.priority,
            "agent_version": self.agent_version,
            "ready": self.ready,
            "world_epoch": self.world_epoch,
            "tick": self.tick,
            "last_seen_tick": self.last_seen_tick,
            "host_silence_s": round(self.host_silence_s(), 2),
            "uptime_s": round(time.perf_counter() - self.started_ms, 2),
            "step_ms_p95": round(self.step_ms_p95, 4),
            "step_samples": len(self.step_ms),
            "control_frames": self.control_frames,
            "malformed": self.malformed,
            "unknown_frames": self.unknown_frames,
            "oversize": self.oversize,
            "hello_sent": self.hello_sent,
            "hello_ok": self.hello_ok,
            "peers_learned": self.peers_learned,
            "peers_known": sorted(self.peer_addrs),
            "acts_sent": self.acts_sent,
            "acts_missing": self.acts_missing,
            "ctrl_sent": self.ctrl_sent,
            "radio": radio,
            "position": None if self.pos is None else list(self.pos),
            "robot": None
            if robot is None
            else {
                "pos": list(robot.pos),
                "phase": robot.phase,
                "current_task": robot.current_task,
                "battery": round(robot.battery, 2),
                "velocity": list(robot.velocity),
                "distance": round(robot.distance, 3),
                "wait_ticks": robot.wait_ticks,
                "move_ticks": robot.move_ticks,
                "deadlock_events": robot.deadlock_events,
                "stall_handoffs": robot.stall_handoffs,
                "tasks_done": robot.tasks_done,
                "known_tasks": {tid: kt.status for tid, kt in robot.known_tasks.items()},
                "peers": sorted(robot.peers),  # heard directly, peer-to-peer
                "auctions": sorted(robot.auctions),
                "step_n": robot.step_n,
            },
        }

    def close(self) -> None:
        closer = getattr(self.radio, "close", None)
        if closer is not None:
            closer()


def _proc_rss_kb() -> int | None:
    """Resident set size in KiB, from ``/proc/self/statm``. ``None`` where absent.

    ``/proc`` is Linux-only, which is exactly the platform this measurement
    exists for (Raspberry Pi OS). Anywhere else the honest answer is ``None``.
    """
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
    except (OSError, IndexError, ValueError):
        return None
    page_size = 4096
    sysconf = getattr(os, "sysconf", None)
    if sysconf is not None:
        try:
            page_size = int(sysconf("SC_PAGE_SIZE"))
        except (ValueError, OSError):  # pragma: no cover - exotic libc
            page_size = 4096
    return (pages * page_size) // 1024


def process_resources(wall_s: float | None = None) -> dict[str, Any]:
    """This process's own CPU and resident memory, as far as the platform allows.

    Diagnostics only: like :meth:`EdgeAgent.telemetry`, nothing here is fed back
    into a decision, so measuring it cannot change a route or a winner.

    It exists because requirement 10 asks for *CPU time per robot tick* against
    the baseline, and on the Raspberry Pi that number has to come from the node
    itself - the host cannot read another machine's RSS.

    Stdlib only, in the order the platforms allow:

    * CPU - ``resource.getrusage(RUSAGE_SELF)``: user + system seconds, plus a
      percentage of one core when the caller passes the process wall time.
    * RAM - ``/proc/self/statm`` resident pages (Linux, hence Pi OS).

    A measurement the platform cannot make is returned as ``None`` with
    ``available: false`` and a ``reason``. A missing number is reported as
    missing, never as zero, so a Windows rehearsal can never be mistaken for Pi
    evidence.
    """
    out: dict[str, Any] = {
        "platform": sys.platform,
        "pid": os.getpid(),
        "cpu_count": os.cpu_count(),
        "available": False,
        "reason": None,
        "cpu_user_s": None,
        "cpu_sys_s": None,
        "cpu_total_s": None,
        "cpu_pct_of_core": None,
        "rss_kb": None,
        "rss_mb": None,
        "load_avg_1m": None,
    }
    missing: list[str] = []

    if _resource is not None:
        usage = _resource.getrusage(_resource.RUSAGE_SELF)
        user, system = float(usage.ru_utime), float(usage.ru_stime)
        total = user + system
        out["cpu_user_s"] = round(user, 6)
        out["cpu_sys_s"] = round(system, 6)
        out["cpu_total_s"] = round(total, 6)
        if wall_s and wall_s > 0:
            out["cpu_pct_of_core"] = round(100.0 * total / float(wall_s), 4)
    else:
        missing.append("cpu (no `resource` module on this platform)")

    rss_kb = _proc_rss_kb()
    if rss_kb is None:
        missing.append("rss (no /proc/self/statm on this platform)")
    else:
        out["rss_kb"] = rss_kb
        out["rss_mb"] = round(rss_kb / 1024.0, 3)

    getloadavg = getattr(os, "getloadavg", None)
    if getloadavg is not None:
        try:
            out["load_avg_1m"] = round(float(getloadavg()[0]), 3)
        except OSError:  # pragma: no cover - and not worth failing a run over
            pass

    out["available"] = not missing
    out["reason"] = None if not missing else "unavailable: " + "; ".join(missing)
    return out


__all__ = ["AGENT_VERSION", "HEARTBEAT_TICKS", "EdgeAgent", "process_resources"]
