"""The host-side stand-in for an ``EdgeAgent`` that lives on another machine.

``RemoteAgentSession`` is what the host holds for a robot whose decision stack is
on a Pi: an address, a mailbox and the last thing that robot said. It is *not* a
remote control and it is *not* a shadow robot. It owns no ``Robot``, so it has
nothing with which to plan a path, order a cost or pick an auction winner - the
whole class is ``dict`` bookkeeping plus one behaviour rule:

    an ``ACT`` is an intention for exactly the tick it names. An ``ACT`` for any
    other tick is dropped and counted, never re-used.

That rule is what keeps the host honest in LAN mode. If a robot's packet is lost
the host holds the robot still and reports it (doc 06 F4/F5); it never invents a
destination and never decides for the robot.

Its whole job is to answer the three questions ``EdgeHost`` asks every member of
``self.agents``:

* ``tick_once(tick)`` -> the ``ACT`` the robot published for this tick, or ``None``.
* ``send_heartbeat(tick)`` -> the newest ``HEARTBEAT`` the robot sent.
* ``telemetry()`` / ``ready`` / ``step_ms_p95`` -> a read-only spectator view.

Everything the host knows about a remote robot therefore arrives as a message the
robot itself chose to send, which is exactly the property doc 06 F1-F4 describe.
"""

from __future__ import annotations

from typing import Any

Pos = tuple[int, int]

#: Reported in the spectator view for a robot that is not in this process.
REMOTE_VERSION = "remote-1"


def _int_or_none(raw: Any) -> int | None:
    try:
        return None if raw is None else int(raw)
    except (TypeError, ValueError):
        return None


class RemoteAgentSession:
    """Address book + ACT mailbox for one robot running elsewhere on the LAN."""

    def __init__(
        self,
        robot_id: str,
        pos: Pos,
        *,
        policy: str = "ubpa",
        priority: int = 1,
    ) -> None:
        self.id = str(robot_id)
        #: The spawn cell the host seated for this robot. The robot learns its own
        #: position from ``HELLO_WORLD.spawn`` and its own ``ACT_RESULT``.
        self.pos: Pos = (int(pos[0]), int(pos[1]))
        self.policy = str(policy)
        self.priority = int(priority)
        self.agent_version = REMOTE_VERSION
        #: No local decision stack. Every spectator projection in ``host.py``
        #: branches on this being ``None`` and falls back to heartbeat fields.
        self.robot: Any = None
        #: The remote robot's radio is on its own machine, so there is none here.
        #: Present (as ``None``) so ``EdgeHost.set_radio`` can ask every session the
        #: same question and get an honest "not mine to tune" answer.
        self.radio: Any = None
        #: True once ``HELLO_WORLD`` has been sent to the admitted address.
        self.ready = False
        #: Where the host last heard from this robot. The source address of the
        #: datagram, not a configured value (doc 03 §2: "the host pins it").
        self.addr: tuple[str, int] | None = None
        #: Where *peers* should reach this robot, as the robot reported it.
        self.listen_port: int | None = None
        self.world_epoch = 0
        self.tick = 0
        self.hello_received = 0
        self.hello_ok = False
        self.duplicate_claims = 0
        self.rebinds = 0
        self.acts_sent = 0
        self.acts_missing = 0
        self.heartbeats = 0
        self.last_heartbeat: dict[str, Any] = {}
        self.last_seen_tick = -1
        self.step_ms_p95 = 0.0
        self._act: dict[str, Any] | None = None

    # ----- admission ----------------------------------------------------

    def claim(self, addr: Any, hello: dict[str, Any]) -> None:
        """Pin the address this robot claimed and adopt what it reported.

        ``policy``/``priority`` are *reported*, never chosen here: the host has no
        say in how a robot decides (doc 04 §1.1).
        """
        self.addr = None if not addr else (str(addr[0]), int(addr[1]))
        self.listen_port = _int_or_none(hello.get("listen_port"))
        self.agent_version = str(hello.get("agent_version") or REMOTE_VERSION)
        self.policy = str(hello.get("policy") or self.policy)
        self.priority = int(hello.get("priority") or self.priority)
        self.hello_received += 1
        self.hello_ok = True
        self.world_epoch += 1
        self.ready = True

    def rebind(self, addr: Any, hello: dict[str, Any]) -> None:
        """The same robot id answered from a new address after going quiet."""
        self.rebinds += 1
        self.claim(addr, hello)

    # ----- the host's three questions -----------------------------------

    def queue_act(self, msg: dict[str, Any]) -> None:
        """Hold the ACT the robot published, unedited."""
        self.acts_sent += 1
        self._act = dict(msg)

    def has_act(self, tick: int) -> bool:
        act = self._act
        return act is not None and int(act.get("tick", -1)) == int(tick)

    def tick_once(self, tick: int) -> dict[str, Any] | None:
        """This tick's ACT, or ``None``. A stale ACT becomes a late ACT."""
        self.tick = max(self.tick, int(tick))
        act = self._act
        self._act = None
        if act is None or int(act.get("tick", -1)) != int(tick):
            self.acts_missing += 1
            return None
        self.last_seen_tick = int(tick)
        return act

    def note_heartbeat(self, msg: dict[str, Any], tick: int) -> None:
        """Record liveness plus the counters only the robot can compute."""
        self.last_heartbeat = dict(msg)
        self.heartbeats += 1
        self.last_seen_tick = max(self.last_seen_tick, int(tick))
        raw = msg.get("step_ms_p95")
        if raw is not None:
            self.step_ms_p95 = float(raw)

    def send_heartbeat(self, tick: int) -> dict[str, Any]:
        """Return the robot's own heartbeat; the host never authors one for it."""
        return dict(self.last_heartbeat)

    # ----- telemetry ----------------------------------------------------

    def telemetry(self) -> dict[str, Any]:
        """Diagnostics for this edge session. Nothing here feeds a decision."""
        hb = self.last_heartbeat
        radio = hb.get("radio") or {}
        return {
            "id": self.id,
            "policy": self.policy,
            "priority": self.priority,
            "agent_version": self.agent_version,
            "ready": self.ready,
            "world_epoch": self.world_epoch,
            "tick": self.tick,
            "last_seen_tick": self.last_seen_tick,
            "step_ms_p95": round(self.step_ms_p95, 4),
            "hello_received": self.hello_received,
            "hello_ok": self.hello_ok,
            "duplicate_claims": self.duplicate_claims,
            "rebinds": self.rebinds,
            "acts_sent": self.acts_sent,
            "acts_missing": self.acts_missing,
            "heartbeats": self.heartbeats,
            "addr": None if self.addr is None else [self.addr[0], self.addr[1]],
            "listen_port": self.listen_port,
            # The radio counters the robot reported; the host cannot see the wire.
            "radio": {
                "delivered": float(hb.get("recv", 0) or 0),
                "dropped": float(hb.get("lost_est", 0) or 0),
                "sent": float(hb.get("sent", 0) or 0),
                "recv": float(hb.get("recv", 0) or 0),
                "lost_est": float(hb.get("lost_est", 0) or 0),
                "mean_latency": float(radio.get("mean_latency", 0.0) or 0.0),
                "port": self.listen_port,
            },
        }

    def close(self) -> None:
        """Nothing to release: the socket belongs to the host, not the session."""
        return None


__all__ = ["REMOTE_VERSION", "RemoteAgentSession"]
