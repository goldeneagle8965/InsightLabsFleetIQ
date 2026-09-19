"""The slice of ``Warehouse`` a robot may look at when it is off the host.

``simulation/robot.py`` touches the world through exactly seven members
(measured, see ``docs/edge/00_AUDIT_SUMMARY.md``):

    world.charger        lines 213, 437, 441, 499
    world.sense(pos)     line 410
    world.width          lines 584-585, 591-592, 835, 850-851, 862-863, 1001-1002, 1010-1011
    world.height         same lines
    world.walls          lines 821, 1022
    world.turnouts       lines 999-1011
    world.in_bounds(p)   line 1022

``RemoteWorldView`` implements those and nothing else. In particular it has no
``blocked`` attribute: the live obstacle set is known to the agent only through
``sense()`` and through peer ``OBSTACLE`` messages, exactly as on the real
floor. ``tests/test_edge_agent.py`` asserts that absence, so a future edit that
quietly hands the robot a map-wide oracle fails the suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from simulation.config import SENSOR_RANGE

Pos = tuple[int, int]

#: The complete surface ``Robot`` reads. Kept explicit so the boundary is a
#: testable contract rather than a claim in a document.
ROBOT_WORLD_SURFACE: tuple[str, ...] = (
    "charger",
    "sense",
    "width",
    "height",
    "walls",
    "turnouts",
    "in_bounds",
)


def _cells(raw: Iterable[Any]) -> set[Pos]:
    out: set[Pos] = set()
    for item in raw or ():
        out.add((int(item[0]), int(item[1])))
    return out


@dataclass
class RemoteWorldView:
    """Static geometry plus the agent's own local obstacle picture."""

    width: int
    height: int
    walls: set[Pos] = field(default_factory=set)
    turnouts: set[Pos] = field(default_factory=set)
    charger: Pos = (0, 0)
    sensor_range: int = SENSOR_RANGE
    digest: str = ""
    #: Cells the host reported as blocked within ``sensor_range`` **and** cells
    #: peers shared over ``OBSTACLE``. Private on purpose.
    _blocked: set[Pos] = field(default_factory=set)
    #: Digest from the newest OBS; when it differs from ``digest`` the agent
    #: asks the host for a fresh HELLO_WORLD (env switch, doc 04 §1.3).
    obs_digest: str = ""

    # ----- construction -------------------------------------------------

    @classmethod
    def from_world_message(cls, msg: dict[str, Any]) -> "RemoteWorldView":
        """Build from a ``HELLO_WORLD`` payload (doc 04 §1.2)."""
        world = msg.get("world") or msg
        charger = world.get("charger") or (0, 0)
        return cls(
            width=int(world.get("width", 0)),
            height=int(world.get("height", 0)),
            walls=_cells(world.get("walls")),
            turnouts=_cells(world.get("turnouts")),
            charger=(int(charger[0]), int(charger[1])),
            sensor_range=int(world.get("sensor_range", SENSOR_RANGE)),
            digest=str(world.get("digest") or msg.get("digest") or ""),
        )

    def apply_world(self, msg: dict[str, Any]) -> None:
        """Adopt new static geometry in place (keeps the object the robot holds)."""
        fresh = RemoteWorldView.from_world_message(msg)
        self.width = fresh.width
        self.height = fresh.height
        self.walls = fresh.walls
        self.turnouts = fresh.turnouts
        self.charger = fresh.charger
        self.sensor_range = fresh.sensor_range
        self.digest = fresh.digest or self.digest

    # ----- per-tick observation -----------------------------------------

    def apply_obs(self, msg: dict[str, Any]) -> None:
        """Replace the local obstacle picture with the host's current one.

        Only cells within ``sensor_range`` of *this* robot are sent, so this is
        the network form of ``Warehouse.sense`` (world.py:71-77) and not a map
        dump. Cells a peer reported over ``OBSTACLE`` are preserved, because the
        robot learned those from the radio, not from the host.
        """
        charger = msg.get("charger")
        if charger:
            self.charger = (int(charger[0]), int(charger[1]))
        self.obs_digest = str(msg.get("world_digest") or "")
        self._blocked = _cells(msg.get("nearby_blocked"))

    @property
    def needs_world(self) -> bool:
        """True when the newest OBS describes geometry this agent has not loaded."""
        return bool(self.obs_digest) and bool(self.digest) and self.obs_digest != self.digest

    def note_peer_obstacle(self, cell: Pos) -> None:
        """Add a cell a peer shared over ``OBSTACLE``.

        The robot also stores it in ``known_blocked`` during ``_ingest``; this
        keeps ``sense()`` consistent for the same tick.
        """
        self._blocked.add((int(cell[0]), int(cell[1])))

    # ----- the seven members the robot uses -----------------------------

    def sense(self, pos: Pos, rng: int | None = None) -> set[Pos]:
        """Blocked cells within ``rng`` (default ``sensor_range``) of ``pos``.

        Matches ``Warehouse.sense`` when the host computed ``nearby_blocked``
        from the same position - which is what the tick loop guarantees.
        """
        radius = self.sensor_range if rng is None else int(rng)
        px, py = int(pos[0]), int(pos[1])
        return {b for b in self._blocked if abs(b[0] - px) + abs(b[1] - py) <= radius}

    def in_bounds(self, p: Pos) -> bool:
        return 0 <= int(p[0]) < self.width and 0 <= int(p[1]) < self.height

    # ----- diagnostics ---------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Telemetry only; the robot never reads this."""
        return {
            "width": self.width,
            "height": self.height,
            "walls": len(self.walls),
            "turnouts": len(self.turnouts),
            "charger": list(self.charger),
            "sensor_range": self.sensor_range,
            "digest": self.digest,
            "obs_digest": self.obs_digest,
            "locally_blocked": len(self._blocked),
            "has_blocked_attr": hasattr(self, "blocked"),
        }
@dataclass
class LocalWorldView:
    """A live ``Warehouse`` wearing the same seven-member surface as the wire view.

    This is the *"run locally"* half of the edge abstraction: the very same
    ``EdgeAgent`` / ``Robot`` can be handed either this wrapper or a
    ``RemoteWorldView`` and it cannot tell the difference. It exists so the
    decision stack has no hard dependency on the host process - the offline,
    hardware-free path (doc 05) and T10.4 exercise exactly this pairing.

    Unlike ``RemoteWorldView`` there is no ``apply_obs`` (the obstacle picture is
    already live, not pushed) and no digest bookkeeping; ``needs_world`` is
    therefore always ``False``.
    """

    world: Any
    sensor_range: int = SENSOR_RANGE

    # ----- the seven members the robot uses -----------------------------
    @property
    def width(self) -> int:
        return int(self.world.width)

    @property
    def height(self) -> int:
        return int(self.world.height)

    @property
    def walls(self) -> set[Pos]:
        return self.world.walls

    @property
    def turnouts(self) -> set[Pos]:
        return self.world.turnouts

    @property
    def charger(self) -> Pos:
        return (int(self.world.charger[0]), int(self.world.charger[1]))

    def sense(self, pos: Pos, rng: int | None = None) -> set[Pos]:
        """Delegate to ``Warehouse.sense`` with identical radius semantics."""
        radius = self.sensor_range if rng is None else int(rng)
        return self.world.sense((int(pos[0]), int(pos[1])), radius)

    def in_bounds(self, p: Pos) -> bool:
        return bool(self.world.in_bounds((int(p[0]), int(p[1]))))

    @property
    def needs_world(self) -> bool:
        """A locally-held world is never stale; the host cannot be out of date."""
        return False

    # ----- diagnostics ---------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Telemetry only; the robot never reads this."""
        return {
            "kind": "local",
            "width": self.width,
            "height": self.height,
            "walls": len(self.walls),
            "turnouts": len(self.turnouts),
            "charger": list(self.charger),
            "sensor_range": self.sensor_range,
            "has_blocked_attr": hasattr(self.world, "blocked"),
        }
