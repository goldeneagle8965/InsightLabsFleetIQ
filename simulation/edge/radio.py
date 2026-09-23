"""Radio transports. A medium, never a dispatcher.

``SharedMedium``
    In-process broadcast fabric with the *same* delay/countdown algorithm as
    ``simulation/peer.py``: a packet queued at tick N with ``delay=1`` is handed
    over two ``tick()`` calls later, because the engine advances the bus before
    the robots step. Reimplemented rather than imported so the parity test
    (T1) proves the semantics were reproduced, not inherited.

``LoopbackRadio``
    One agent's handle on that medium. Used for the in-process demo, the parity
    test and the loss/degradation tests.

``UdpRadio``
    Real agent-to-agent unicast. Peers talk to each other directly; the host is
    never in the path of PEER traffic. CTRL traffic arriving on the same socket
    is diverted to a separate queue so ``Robot._ingest`` only ever sees peer
    messages.

Neither transport chooses a winner, a path or a yielder.
"""

from __future__ import annotations

import random
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from simulation.config import RADIO_DELAY
from simulation.edge.protocol import (
    CTRL_TYPES,
    MAX_DATAGRAM,
    PROTOCOL_VERSION,
    HOST_ID,
    MessageTooLarge,
    ProtocolError,
    encode,
    head,
    decode_safe,
)


def _stamp(packet: dict[str, Any], sender: str, tick: int) -> dict[str, Any]:
    """Mirror ``PeerBus.broadcast`` stamping (peer.py:94-97).

    The one addition is the protocol version tag. ``PeerBus`` never needs it
    because nothing on that path is ever encoded; a real datagram does, and
    ``decode`` rejects a frame without it. The robot ignores the key, so adding
    it cannot change a decision.
    """
    out = dict(packet)
    out.setdefault("from_id", sender)
    out.setdefault("v", PROTOCOL_VERSION)
    out["sent_tick"] = int(tick)
    return out


# --- in-process medium --------------------------------------------------


@dataclass
class _InFlight:
    remaining: int
    dest: str
    msg: dict[str, Any]


@dataclass
class SharedMedium:
    """Broadcast fabric for robots running inside one process."""

    delay: int = RADIO_DELAY
    drop_prob: float = 0.0
    seed: int = 0
    subscribers: list[str] = field(default_factory=list)
    in_flight: list[_InFlight] = field(default_factory=list)
    mailboxes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    log: deque = field(default_factory=lambda: deque(maxlen=250))
    delivered_count: int = 0
    dropped_count: int = 0
    oversize_count: int = 0
    latency_sum: float = 0.0
    latency_n: int = 0
    now: int = 0
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed + 17)

    # ----- membership ---------------------------------------------------

    def register(self, robot_id: str) -> None:
        if robot_id not in self.subscribers:
            self.subscribers.append(robot_id)
        self.mailboxes.setdefault(robot_id, [])

    # ----- traffic ------------------------------------------------------

    def broadcast(self, sender: str, msg: dict[str, Any], tick: int) -> None:
        packet = _stamp(msg, sender, tick)
        to_id = packet.get("to_id")
        self.log.appendleft(
            {
                "tick": int(tick),
                "from_id": sender,
                "type": packet.get("type"),
                "task_id": packet.get("task_id"),
                "status": packet.get("status"),
                "to": to_id,
            }
        )
        try:
            encode(packet)
        except MessageTooLarge:
            # A datagram the transport refuses to fragment is a dropped packet,
            # not a crash: UDP would have dropped it anyway.
            self.oversize_count += 1
            self.dropped_count += 1
            return
        targets = [s for s in self.subscribers if s != sender]
        if to_id:
            targets = [to_id] if to_id in self.subscribers and to_id != sender else []
        for dest in targets:
            if self.drop_prob > 0 and self._rng.random() < self.drop_prob:
                self.dropped_count += 1
                continue
            self.in_flight.append(_InFlight(int(self.delay), dest, dict(packet)))

    def deliver(self, dest: str, msg: dict[str, Any], recv_tick: int | None = None) -> None:
        """Hand a message straight to one mailbox, bypassing the medium.

        The host uses this for the WMS bulletin in network mode: the message
        left the host as a datagram, so the agent may not pretend it came from
        the shared fabric.
        """
        now = self.now if recv_tick is None else int(recv_tick)
        delivered = dict(msg)
        delivered["recv_tick"] = now
        sent = int(delivered.get("sent_tick", now))
        self.latency_sum += max(0, now - sent)
        self.latency_n += 1
        self.mailboxes.setdefault(dest, []).append(delivered)
        self.delivered_count += 1

    def tick(self, now: int) -> None:
        """Advance one tick, delivering everything whose countdown expired."""
        self.now = int(now)
        keep: list[_InFlight] = []
        for env in self.in_flight:
            if env.remaining <= 0:
                delivered = dict(env.msg)
                delivered["recv_tick"] = self.now
                sent = int(delivered.get("sent_tick", self.now))
                self.latency_sum += max(0, self.now - sent)
                self.latency_n += 1
                self.mailboxes.setdefault(env.dest, []).append(delivered)
                self.delivered_count += 1
            else:
                env.remaining -= 1
                keep.append(env)
        self.in_flight = keep

    def receive(self, robot_id: str) -> list[dict[str, Any]]:
        msgs = self.mailboxes.get(robot_id, [])
        self.mailboxes[robot_id] = []
        return msgs

    # ----- knobs and stats ----------------------------------------------

    def set_radio(self, drop_prob: float | None = None, delay: int | None = None) -> dict[str, Any]:
        if drop_prob is not None:
            self.drop_prob = max(0.0, min(0.9, float(drop_prob)))
        if delay is not None:
            self.delay = max(0, min(8, int(delay)))
        return {"drop_prob": self.drop_prob, "delay": self.delay}

    def mean_latency(self) -> float:
        if self.latency_n == 0:
            return 0.0
        return self.latency_sum / self.latency_n

    def stats(self) -> dict[str, Any]:
        return {
            "drop_prob": self.drop_prob,
            "delay": self.delay,
            "dropped": self.dropped_count,
            "delivered": self.delivered_count,
            "oversize": self.oversize_count,
            "mean_latency": self.mean_latency(),
            "in_flight": len(self.in_flight),
        }

    def snapshot_log(self, n: int = 36) -> list[dict[str, Any]]:
        return list(self.log)[: int(n)]


@dataclass
class LoopbackRadio:
    """One agent's handle on a :class:`SharedMedium`.

    Exposes the same two methods ``Robot`` uses (``broadcast``/``receive``) plus
    ``register`` so the host can build the fleet without knowing the transport.
    """

    agent_id: str
    medium: SharedMedium = field(default_factory=SharedMedium)

    def __post_init__(self) -> None:
        self.medium.register(self.agent_id)

    # ----- Robot's view -------------------------------------------------

    def broadcast(self, sender: str, msg: dict[str, Any], tick: int) -> None:
        self.medium.broadcast(sender, msg, tick)

    def receive(self, robot_id: str) -> list[dict[str, Any]]:
        return self.medium.receive(robot_id)

    # ----- host/agent plumbing -----------------------------------------

    def register(self, robot_id: str | None = None) -> None:
        self.medium.register(robot_id or self.agent_id)

    def tick(self, now: int) -> None:
        self.medium.tick(now)

    def deliver(self, msg: dict[str, Any], recv_tick: int | None = None) -> None:
        self.medium.deliver(self.agent_id, msg, recv_tick)

    def mean_latency(self) -> float:
        return self.medium.mean_latency()

    def stats(self) -> dict[str, Any]:
        return self.medium.stats()


# --- real UDP -----------------------------------------------------------


class RadioPortInUse(OSError):
    """The listen port is already owned by another radio.

    On the LAN an agent's port *is* its identity: the host pins the address from
    that agent's own ``HELLO``, so two processes sharing one port would have the
    fleet's datagrams split between them. This is raised instead of quietly
    sharing the port (doc 03 §2).
    """


class UdpRadio:
    """Real peer-to-peer UDP for edge agents.

    Outbound PEER messages go to the configured address of each peer, directly.
    Inbound datagrams are split: CTRL frames are queued for the agent's control
    loop, everything else is queued for ``Robot._ingest``. The host is never the
    relay for agent-to-agent traffic.
    """

    def __init__(
        self,
        agent_id: str,
        peers: dict[str, tuple[str, int]] | None = None,
        listen: tuple[str, int] = ("0.0.0.0", 0),
        drop_prob: float = 0.0,
        seed: int = 0,
        sock: socket.socket | None = None,
    ) -> None:
        self.agent_id = str(agent_id)
        self.peers: dict[str, tuple[str, int]] = dict(peers or {})
        self.drop_prob = float(drop_prob)
        self._rng = random.Random(seed + 17)
        self._inbox: list[dict[str, Any]] = []
        self._ctrl: list[dict[str, Any]] = []
        self.delivered_count = 0
        self.dropped_count = 0
        self.oversize_count = 0
        self.foreign_count = 0
        self.latency_sum = 0.0
        self.latency_n = 0
        self._now = 0
        self._closed = False
        self.sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        # Bound exclusively: under Windows (and BSD sockets in general) a UDP
        # socket created with SO_REUSEADDR can bind an address another socket
        # already owns, and the kernel then hands datagrams to whichever
        # process it likes. A second agent started on a busy port must fail
        # loudly, not steal half the traffic.
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            self.sock.bind(listen)
        except OSError as exc:  # pragma: no cover - platform wording differs
            try:
                self.sock.close()
            except OSError:
                pass
            raise RadioPortInUse(
                f"UDP port {listen[0]}:{listen[1]} is already in use by another "
                f"socket; a second agent cannot share a listen port"
            ) from exc
        self.host_addr: tuple[str, int] | None = None

    # ----- discovery ----------------------------------------------------

    @property
    def port(self) -> int:
        return int(self.sock.getsockname()[1])

    def bound_address(self) -> str:
        return f"{self.sock.getsockname()[0]}:{self.port}"

    def set_host(self, addr: tuple[str, int] | None) -> None:
        """Record the host address; CTRL is only accepted from here."""
        self.host_addr = addr

    def add_peer(self, robot_id: str, addr: tuple[str, int]) -> None:
        self.peers[str(robot_id)] = addr

    def register(self, robot_id: str | None = None) -> None:
        """Peer tables are static config; registration is a no-op that keeps
        the transport interface identical to ``LoopbackRadio``."""
        return None

    # ----- Robot's view -------------------------------------------------

    def broadcast(self, sender: str, msg: dict[str, Any], tick: int) -> None:
        if self._closed:
            return
        packet = _stamp(msg, sender, tick)
        packet["sent_ms"] = round(time.perf_counter() * 1000.0, 3)
        to_id = packet.get("to_id")
        if to_id:
            targets = [to_id] if to_id in self.peers else []
        else:
            targets = [rid for rid in self.peers if rid != sender]
        try:
            payload = encode(packet)
        except MessageTooLarge:
            self.oversize_count += 1
            return
        for rid in targets:
            if self.drop_prob > 0 and self._rng.random() < self.drop_prob:
                self.dropped_count += 1
                continue
            try:
                self.sock.sendto(payload, self.peers[rid])
            except OSError:
                self.dropped_count += 1

    def receive(self, robot_id: str) -> list[dict[str, Any]]:
        """Drain the socket and return the messages ``Robot`` should ingest."""
        self._pump()
        msgs = self._inbox
        self._inbox = []
        return msgs

    # ----- agent control loop ------------------------------------------

    def take_control(self) -> list[dict[str, Any]]:
        """Drain CTRL frames (HELLO_WORLD, OBS, ACT_RESULT, SHUTDOWN)."""
        self._pump()
        msgs = self._ctrl
        self._ctrl = []
        return msgs

    def inject(self, msg: dict[str, Any]) -> None:
        """Queue an already-received frame, e.g. from a test or a replay."""
        self._route(msg)

    def tick(self, now: int) -> None:
        self._now = int(now)

    def mean_latency(self) -> float:
        if self.latency_n == 0:
            return 0.0
        return self.latency_sum / self.latency_n

    def stats(self) -> dict[str, Any]:
        return {
            "drop_prob": self.drop_prob,
            "delay": None,
            "dropped": self.dropped_count,
            "delivered": self.delivered_count,
            "oversize": self.oversize_count,
            "foreign": self.foreign_count,
            "mean_latency": self.mean_latency(),
            "in_flight": 0,
            "port": self.port,
        }

    def log(self, n: int = 36) -> list[dict[str, Any]]:
        return []  # the UDP transport keeps counters, not a packet log

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.sock.close()
        except OSError:  # pragma: no cover - best effort
            pass

    # ----- internals ----------------------------------------------------

    def _pump(self) -> None:
        if self._closed:
            return
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            msg = decode_safe(data)
            if msg is None:
                self.dropped_count += 1
                continue
            if self.host_addr is not None and msg.get("type") in CTRL_TYPES and addr != self.host_addr:
                self.foreign_count += 1
                continue
            self._route(msg)

    def _route(self, msg: dict[str, Any]) -> None:
        delivered = dict(msg)
        delivered.setdefault("recv_tick", self._now)
        sent = int(delivered.get("sent_tick", self._now))
        self.latency_sum += max(0, self._now - sent)
        self.latency_n += 1
        self.delivered_count += 1
        if str(msg.get("type")) in CTRL_TYPES:
            self._ctrl.append(delivered)
        else:
            self._inbox.append(delivered)


def attach_loopback(robots: Iterable[str], *, delay: int = RADIO_DELAY, drop_prob: float = 0.0, seed: int = 0):
    """Build one medium plus one radio per robot id. Convenience for tests/tools."""
    medium = SharedMedium(delay=delay, drop_prob=drop_prob, seed=seed)
    radios = {}
    for rid in robots:
        medium.register(rid)
        radios[rid] = LoopbackRadio(rid, medium)
    return medium, radios


def host_bulletin(task_msg: dict[str, Any], tick: int) -> dict[str, Any]:
    """Wrap a task row as a WMS bulletin (broadcast, never an assignment)."""
    return head(
        str(task_msg.get("type", "TASK_ANNOUNCE")),
        HOST_ID if task_msg.get("from_id") is None else str(task_msg.get("from_id")),
        tick,
        **{k: v for k, v in task_msg.items() if k not in ("v", "type", "from_id", "tick")},
    )


__all__ = [
    "LoopbackRadio",
    "RadioPortInUse",
    "SharedMedium",
    "UdpRadio",
    "attach_loopback",
    "host_bulletin",
    "ProtocolError",
    "MAX_DATAGRAM",
]
