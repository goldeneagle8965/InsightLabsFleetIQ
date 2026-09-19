"""Local space-time A*. Each robot runs this on its own map.

Peer planned_path cells are treated as reservations. After a peer's last
known cell, they are assumed to wait there for HOLD_HORIZON ticks.
This module never sees a global occupancy oracle — callers pass only
what the robot itself has heard.
"""

from __future__ import annotations

import heapq
from collections import deque
from typing import Iterable

from simulation.config import DIRS, HOLD_HORIZON, PATH_HORIZON


Pos = tuple[int, int]


def manhattan(a: Pos, b: Pos) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def spatial_path(
    start: Pos,
    goal: Pos,
    blocked: set[Pos],
    width: int,
    height: int,
    max_expand: int = 4000,
) -> list[Pos]:
    """4-connected BFS, no time. Returns start..goal or empty."""
    if start == goal:
        return [start]
    if not _free(goal, blocked, width, height) or not _free(start, blocked, width, height):
        return []
    q = deque([start])
    prev: dict[Pos, Pos | None] = {start: None}
    n = 0
    while q and n < max_expand:
        cur = q.popleft()
        n += 1
        for dx, dy in DIRS:
            nxt = (cur[0] + dx, cur[1] + dy)
            if nxt in prev or not _free(nxt, blocked, width, height):
                continue
            prev[nxt] = cur
            if nxt == goal:
                return _rewind(prev, nxt)
            q.append(nxt)
    return []


def nearest_cell(
    start: Pos,
    candidates: Iterable[Pos],
    blocked: set[Pos],
    width: int,
    height: int,
    forbidden: set[Pos] | None = None,
) -> Pos | None:
    cand = {p for p in candidates if p not in (forbidden or set())}
    if not cand:
        return None
    if start in cand:
        return start
    q = deque([start])
    seen = {start}
    while q:
        cur = q.popleft()
        for dx, dy in DIRS:
            nxt = (cur[0] + dx, cur[1] + dy)
            if nxt in seen or not _free(nxt, blocked, width, height):
                continue
            if nxt in cand:
                return nxt
            seen.add(nxt)
            q.append(nxt)
    return None


def reservations_from_peers(
    peers: dict[str, object],
    now: int,
    hold: int = HOLD_HORIZON,
    age_corrected: bool = True,
) -> tuple[set[tuple[int, int, int]], set[tuple[int, int, int, int, int]]]:
    """Vertex (x,y,t) and edge (x1,y1,x2,y2,t) reservations from peer paths.

    `peers` values expose `.planned_path` (list of positions, index 0 = now).

    A peer's `planned_path[0]` is its cell when the packet was *sent*, not when
    we received it. Age-correcting advances the series by the message age so we
    reserve the ticks of floor the peer will actually occupy. Without it, a
    3-tick radio delay makes every receiver plan into tiles the sender has
    already left (or not yet reached).
    """
    vertex: set[tuple[int, int, int]] = set()
    edge: set[tuple[int, int, int, int, int]] = set()
    for peer in peers.values():
        path = list(getattr(peer, "planned_path", None) or [])
        if not path:
            pos = getattr(peer, "position", None)
            if pos is not None:
                path = [tuple(pos)]
        path = [tuple(p) for p in path]
        if age_corrected:
            age = max(0, now - int(getattr(peer, "timestamp", now)))
            if age:
                path = path[age:] if age < len(path) else [path[-1]]
        last = path[-1]
        span = len(path) + hold
        series: list[Pos] = []
        for i in range(span):
            cell = path[i] if i < len(path) else last
            series.append(cell)
            vertex.add((cell[0], cell[1], now + i))
        for i in range(len(series) - 1):
            a, b = series[i], series[i + 1]
            t = now + i
            edge.add((a[0], a[1], b[0], b[1], t))
    return vertex, edge


def spacetime_astar(
    start: Pos,
    goal: Pos,
    blocked: set[Pos],
    width: int,
    height: int,
    vertex_res: set[tuple[int, int, int]],
    edge_res: set[tuple[int, int, int, int, int]],
    now: int,
    horizon: int = PATH_HORIZON,
    max_time: int = 36,
) -> list[Pos]:
    """Plan a space-time path. Returns positions from start (t=now) inclusive.

    Actions: N/E/S/W/WAIT. Vertex and edge conflicts against reservations
    are forbidden. If the goal is reached earlier than `horizon`, the path
    stays at the goal (WAIT) so peers can see the hold.
    """
    if start == goal:
        path = [start]
        for i in range(1, min(3, horizon)):
            t = now + i
            if (start[0], start[1], t) in vertex_res:
                break
            path.append(start)
        return path
    if not _free(goal, blocked, width, height):
        return []

    max_t = max(max_time, horizon + manhattan(start, goal) + 8)
    start_state = (start[0], start[1], now)
    heap: list[tuple[int, int, tuple[int, int, int]]] = []
    h0 = manhattan(start, goal)
    heapq.heappush(heap, (h0, 0, start_state))
    came: dict[tuple[int, int, int], tuple[int, int, int] | None] = {start_state: None}
    gscore = {start_state: 0}
    goal_state: tuple[int, int, int] | None = None

    while heap:
        _f, g, (x, y, t) = heapq.heappop(heap)
        if (x, y) == goal and t > now:
            goal_state = (x, y, t)
            break
        if g >= max_t or t - now >= max_t:
            continue
        for dx, dy in ((0, 0),) + DIRS:
            nx, ny = x + dx, y + dy
            nt = t + 1
            nxt = (nx, ny, nt)
            if nxt in came:
                continue
            if not _free((nx, ny), blocked, width, height):
                continue
            if (nx, ny, nt) in vertex_res:
                continue
            if (x, y, nx, ny, t) in edge_res or (nx, ny, x, y, t) in edge_res:
                continue
            ng = g + 1
            came[nxt] = (x, y, t)
            gscore[nxt] = ng
            f = ng + manhattan((nx, ny), goal)
            heapq.heappush(heap, (f, ng, nxt))

    if goal_state is None:
        return []
    return _rewind_st(came, goal_state)


def first_conflict(
    my_path: list[Pos],
    peer_path: list[Pos],
    lookahead: int,
) -> tuple[int, Pos] | None:
    """Return (index, cell) of the first shared cell within lookahead.

    Index is relative to path[0] == now. Used for conflict detection from
    peer planned_path, not from a central occupancy map.
    """
    n = min(lookahead, len(my_path), len(peer_path) if peer_path else 0)
    if not peer_path:
        return None
    peer_set_time = {i: peer_path[i] for i in range(min(lookahead, len(peer_path)))}
    last_peer = peer_path[-1]
    for i in range(min(lookahead, len(my_path))):
        cell = my_path[i]
        theirs = peer_set_time.get(i, last_peer if i >= len(peer_path) else None)
        if theirs is not None and cell == theirs and i > 0:
            return i, cell
        if i + 1 < len(my_path) and i + 1 < len(peer_path):
            if my_path[i] == peer_path[i + 1] and my_path[i + 1] == peer_path[i]:
                return i, cell
    return None


def _free(p: Pos, blocked: set[Pos], width: int, height: int) -> bool:
    x, y = p
    return 0 <= x < width and 0 <= y < height and p not in blocked


def _rewind(prev: dict[Pos, Pos | None], end: Pos) -> list[Pos]:
    out = [end]
    cur: Pos | None = end
    while cur is not None:
        cur = prev[cur]
        if cur is not None:
            out.append(cur)
    out.reverse()
    return out


def _rewind_st(
    came: dict[tuple[int, int, int], tuple[int, int, int] | None],
    end: tuple[int, int, int],
) -> list[Pos]:
    out: list[Pos] = []
    cur: tuple[int, int, int] | None = end
    while cur is not None:
        out.append((cur[0], cur[1]))
        cur = came[cur]
    out.reverse()
    return out
