"""Floor-plan catalogue.

Same physics and same coordination rules on every map: each layout only
decides where the walls are, where the depot/charger sit, and what the
default jobs look like. Nothing here assigns work or picks a right-of-way.

Every layout is built on the same 25x11 grid so the dashboard camera,
turnout detection, and the existing measurements stay comparable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from simulation.config import MAP_H, MAP_W
from simulation.world import Task, Warehouse

W, H = MAP_W, MAP_H


@dataclass(frozen=True)
class MapSpec:
    """One selectable environment."""

    key: str
    label: str
    blurb: str
    build: Callable[[], Warehouse]
    tasks: Callable[[], list[Task]]
    spawn: Callable[[], list[tuple[int, int]]]
    depot: tuple[int, int]
    charger: tuple[int, int]


def _border(walls: set[tuple[int, int]]) -> None:
    for x in range(W):
        walls.add((x, 0))
        walls.add((x, H - 1))
    for y in range(H):
        walls.add((0, y))
        walls.add((W - 1, y))


def _finish(walls: set[tuple[int, int]], depot: tuple[int, int], charger: tuple[int, int]) -> Warehouse:
    """Wrap a wall set into a Warehouse and derive the turnout cells."""
    for p in (depot, charger):
        walls.discard(p)
    wh = Warehouse(width=W, height=H, walls=walls, depot=depot, charger=charger)
    for y in range(1, H - 1):
        for x in range(1, W - 1):
            p = (x, y)
            if p in walls:
                continue
            if wh.degree(p) >= 3:
                wh.turnouts.add(p)
    return wh


def _pick_tasks(candidates: list[tuple[tuple[int, int], tuple[int, int], float]]) -> list[Task]:
    return [Task(f"T{i + 1}", pick=p, drop=d, urgency=u) for i, (p, d, u) in enumerate(candidates)]


# --- warehouse -------------------------------------------------------------


def warehouse_map() -> Warehouse:
    """3PL floor: rack rows 3/5/7 with aisle mouths, two highways."""
    walls: set[tuple[int, int]] = set()
    _border(walls)
    gap_xs = {1, 2, 12, 13, 22, 23}
    for y in (3, 5, 7):
        for x in range(1, W - 1):
            if x not in gap_xs:
                walls.add((x, y))
    for x in (1, 2):
        walls.discard((x, 3))
        walls.discard((x, 5))
        walls.discard((x, 7))
    wh = _finish(walls, depot=(1, 1), charger=(1, 9))
    for x in (1, 2, 12, 13, 22, 23):
        for y in (2, 4, 6, 8):
            p = (x, y)
            if p not in walls:
                wh.turnouts.add(p)
    return wh


def warehouse_tasks() -> list[Task]:
    return [
        Task("T1", pick=(4, 2), drop=(20, 8), urgency=0.9),
        Task("T2", pick=(20, 2), drop=(4, 8), urgency=0.85),
        Task("T3", pick=(8, 4), drop=(16, 6), urgency=0.7),
        Task("T4", pick=(16, 4), drop=(8, 6), urgency=0.65),
        Task("T5", pick=(6, 8), drop=(18, 2), urgency=0.55),
        Task("T6", pick=(18, 8), drop=(6, 2), urgency=0.5),
    ]


# --- shopping mall ---------------------------------------------------------


def mall_map() -> Warehouse:
    """Central atrium, shop units along the top and bottom, one ring corridor."""
    walls: set[tuple[int, int]] = set()
    _border(walls)
    # Shop units: solid blocks with a single door each, leaving a ring corridor.
    for x in range(2, 8):
        for y in (1, 2):
            walls.add((x, y))
    for x in range(11, 17):
        for y in (1, 2):
            walls.add((x, y))
    for x in range(2, 8):
        for y in (8, 9):
            walls.add((x, y))
    for x in range(11, 17):
        for y in (8, 9):
            walls.add((x, y))
    # Atrium pillars in the middle band
    for x, y in ((7, 5), (12, 5), (17, 5), (9, 4), (14, 6)):
        walls.add((x, y))
    # Shop doors
    for p in ((4, 2), (14, 2), (5, 8), (13, 8)):
        walls.discard(p)
    return _finish(walls, depot=(21, 1), charger=(21, 9))


def mall_tasks() -> list[Task]:
    return [
        Task("T1", pick=(4, 2), drop=(13, 8), urgency=0.9),
        Task("T2", pick=(14, 2), drop=(5, 8), urgency=0.85),
        Task("T3", pick=(21, 1), drop=(9, 9), urgency=0.7),
        Task("T4", pick=(21, 9), drop=(9, 1), urgency=0.65),
        Task("T5", pick=(2, 5), drop=(19, 5), urgency=0.55),
        Task("T6", pick=(19, 5), drop=(2, 5), urgency=0.5),
    ]


# --- hospital --------------------------------------------------------------


def hospital_map() -> Warehouse:
    """Wards on the left, a long central spine, theatres and pharmacy right."""
    walls: set[tuple[int, int]] = set()
    _border(walls)
    # Ward partitions on the left (vertical, with door gaps)
    for y in range(1, H - 1):
        if y not in (4, 8):
            walls.add((6, y))
    # Theatre block on the right with two doors
    for x in range(13, 20):
        for y in (3, 7):
            walls.add((x, y))
    for p in ((13, 3), (19, 3), (13, 7), (19, 7)):
        walls.discard(p)
    # Lab island in the middle
    for x in (9, 10):
        for y in (2, 3):
            walls.add((x, y))
    for x in (9, 10):
        for y in (7, 8):
            walls.add((x, y))
    return _finish(walls, depot=(1, 1), charger=(1, 9))


def hospital_tasks() -> list[Task]:
    return [
        Task("T1", pick=(3, 1), drop=(16, 5), urgency=0.95),
        Task("T2", pick=(16, 5), drop=(3, 9), urgency=0.9),
        Task("T3", pick=(1, 5), drop=(21, 5), urgency=0.75),
        Task("T4", pick=(21, 1), drop=(9, 9), urgency=0.7),
        Task("T5", pick=(9, 1), drop=(21, 9), urgency=0.6),
        Task("T6", pick=(3, 5), drop=(16, 1), urgency=0.55),
    ]


# --- restaurant ------------------------------------------------------------


def restaurant_map() -> Warehouse:
    """Open dining room with table blocks, pass at the top, wash-up at the back."""
    walls: set[tuple[int, int]] = set()
    _border(walls)
    # Table clusters (2x2) with walking space between them
    for bx in (4, 9, 14, 19):
        for by in (3, 6, 9):
            for dx in (0, 1):
                for dy in (0, 1):
                    walls.add((bx + dx, by + dy))
    # Kitchen pass counters
    for x in range(2, 6):
        walls.add((x, 1))
    for x in range(20, 24):
        walls.add((x, 1))
    return _finish(walls, depot=(1, 1), charger=(1, 9))


def restaurant_tasks() -> list[Task]:
    return [
        Task("T1", pick=(6, 1), drop=(20, 8), urgency=0.9),
        Task("T2", pick=(18, 1), drop=(6, 8), urgency=0.85),
        Task("T3", pick=(2, 3), drop=(22, 5), urgency=0.7),
        Task("T4", pick=(7, 5), drop=(17, 8), urgency=0.65),
        Task("T5", pick=(12, 2), drop=(7, 8), urgency=0.55),
        Task("T6", pick=(17, 5), drop=(12, 9), urgency=0.5),
    ]


# --- catalogue -------------------------------------------------------------

MAP_SPECS: dict[str, MapSpec] = {
    "warehouse": MapSpec(
        key="warehouse",
        label="Warehouse (3PL)",
        blurb="Narrow rack aisles, two highways. The measured benchmark layout.",
        build=warehouse_map,
        tasks=warehouse_tasks,
        spawn=lambda: [(2, 1), (12, 1), (22, 1), (2, 9)],
        depot=(1, 1),
        charger=(1, 9),
    ),
    "mall": MapSpec(
        key="mall",
        label="Shopping mall",
        blurb="Central atrium ring corridor with shop units and pillars.",
        build=mall_map,
        tasks=mall_tasks,
        spawn=lambda: [(9, 1), (9, 9), (19, 1), (19, 9)],
        depot=(21, 1),
        charger=(21, 9),
    ),
    "hospital": MapSpec(
        key="hospital",
        label="Hospital",
        blurb="Wards left, long spine, theatres and pharmacy right. Urgent jobs.",
        build=hospital_map,
        tasks=hospital_tasks,
        spawn=lambda: [(2, 1), (2, 9), (22, 1), (22, 9)],
        depot=(1, 1),
        charger=(1, 9),
    ),
    "restaurant": MapSpec(
        key="restaurant",
        label="Restaurant",
        blurb="Open dining room, table clusters, pass counter to wash-up.",
        build=restaurant_map,
        tasks=restaurant_tasks,
        spawn=lambda: [(6, 2), (18, 2), (2, 5), (22, 5)],
        depot=(1, 1),
        charger=(1, 9),
    ),
}

DEFAULT_MAP = "warehouse"


def map_keys() -> list[str]:
    return list(MAP_SPECS.keys())


def get_map(key: str | None) -> MapSpec:
    return MAP_SPECS.get(key or DEFAULT_MAP, MAP_SPECS[DEFAULT_MAP])


def build_environment(key: str | None) -> Warehouse:
    return get_map(key).build()
