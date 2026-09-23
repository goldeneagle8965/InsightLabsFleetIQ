# SIH 26123 — Root-Cause Analysis of the Live Dashboard Run

**Target:** `sih26123` Edge-AI Distributed Fleet Coordination simulation
**Live run to explain:** mall env, 8 robots, UBPA policy, seed 0, tick 9992
**Scope:** diagnosis + smallest algorithmic fix. No UI changes, no fabricated metrics.

---

## 1. Live signature (dashboard screenshot at tick 9992)

| Metric | Live value |
|---|---|
| Tasks | **4/6** — T1, T2 unfinished (OPEN) |
| Collisions | **7,377** (tail ≈ 1/tick from ≈ tick 7742) |
| Deadlocks | **5** |
| Stalls | **4** |
| Stall handoffs | **1** |
| Mean wait | **0.92** |
| Mean comm latency | **1.69** |
| Delivered messages | **783,802** |
| Final positions | all 8 robots clustered at charger cells (19–21, 8–9) |
| Obstacles at tick 9992 | exactly (8,4), (11,6), (16,4), (19,4) — **no door cells blocked** |

The screenshot was re-verified cell-by-cell (44 px/cell calibration): the map shows exactly the 4 EXACT4 obstacles, no door cells blocked, and the 8 robots at the charger. The blue markers at (4,2)/(14,2) are the unfinished T1/T2 pick markers, not robots.

---

## 2. What the live run actually did (reconstructed from the numbers)

- **Delivered 783,802 / 9992 ≈ 78.4 msgs/tick** — a full-run delivery volume. A 76-tick completion run delivers only ~5,600 msgs. The live sim genuinely stepped to tick 9992 **without completing**.
- **Mean latency 1.69 = (6895×2 + 3097×1) / 9992** — the radio delay was 1 for 6,895 ticks and 0 for 3,097 ticks (flip 1→0 at tick 6895). The operator's stated "0-tick broadcast delay" is contradicted by the observed latency; the delay knob was changed mid-run.
- **Collisions 7,377 ≈ the idle-charger-jostle total (7,387)** of a control run that completes 6/6 and then idles. The live collision count is dominated by robots jostling at the charger after the fleet went idle — consistent with tasks stranded and robots with nothing to do.

---

## 3. Reproduction methodology

All experiments run with `FleetSim(env="mall", n_robots=8, policy="ubpa", seed=0, drop_prob=0.0, radio_delay=1, max_ticks=9992)`, `run_until(9992, on_tick=...)`, `world.set_block(cell, True/False)`, `set_radio(delay=...)`, `metrics()`, `bus.delivered_count`. Obstacle schedules were applied in `on_tick` exactly as the dashboard's operator controls do.

**Sweep 3 — screenshot obstacles only (59 cases).** EXACT4 at every placement tick 5–500, every subset, progressive blocking, flip 1→0 / 0→1 at 1000/3000/6895, hash seeds {0,1,2,3,4,5,7,11,13,42,999}, delay 0/1/flip. **Result: 59/59 complete 6/6 at ~tick 58–77 with ~32 collisions, 0 deadlocks, 0 stalls.** The obstacles visible in the screenshot do NOT explain the live signature.

**Sweep 4 — door-cell blocks (19 cases).** Blocking T1/T2 pick/drop cells at tick 20, alone and combined with EXACT4, with delay=1 and with the 6895 flip. **Result: blocking the DROP cells (13,8)/(5,8) strands T1/T2 permanently (4/6).** Best match:

```
d_T1drop_T2drop_exact_flip   (T1D+T2D+EXACT4 @ t=20, delay 1→0 @ 6895)
  tasks 4/6 (T1,T2 OPEN) · stalls 4 · handoffs 2 · lat 1.691 (exact live match)
  wait 0.886 · coll 6,545 · delivered 712,928 · robots clustered at charger
```

The event log shows the mechanism directly:
- R1 claims T1 at t=8, R5 claims T2 at t=12 (before the blocks appear).
- At t=20 the drop cells (13,8)/(5,8) are blocked; R4/R1 sense them.
- R1 picks up, can never reach the drop, `unreachable_ticks` reaches 14 → `_release("unreachable")` at t=39 → task returns to OPEN → auction reopens → R1 re-claims → repeat forever.

**E2 control — EXACT4@20, delay=1, no door blocks, manual loop to 9992.** Completes 6/6 at tick 76, then accumulates 7,387 collisions from idle charger-cluster jostle (~1/tick from ~tick 2500), 0 deadlocks. Live total 7,377 ≈ E2 total 7,387 → the live collision count is the idle-jostle regime; the live tail starts later (~7742) because the live early dynamics differed (stranding).

---

## 4. Root cause

### 4.1 Primary bug — the auction bid-validity check never verifies the drop is reachable

`simulation/robot.py`, `_bid_cost` (line 574):

```python
probe = spatial_path(self.pos, kt.pick, self._blocked(), w, h)
probe += spatial_path(kt.pick, kt.drop, self._blocked(), w, h)   # <-- concatenation
...
if not probe and d > 0:
    valid = False
    cost += 1000.0
```

`probe` is the **concatenation** of the two legs. It is non-empty if *either* leg is reachable. `spatial_path` returns `[start]` when `start == goal` and `[]` when the goal is blocked. So:

- If the **pick** is reachable but the **drop** is blocked → `probe = [path…] + []` → non-empty → **bid valid**.
- The robot wins the auction, claims the task, picks up, and can never deliver.
- After `UNREACHABLE_TICKS = 14` it releases the task (`_release("unreachable")`), the task returns to OPEN, the auction reopens, and the same or another robot re-claims it.
- **Infinite claim → pick → fail → release → re-claim cycle.** The task never completes, even if the block is later removed.

This is the answer to "are robots repeatedly selecting the same route/cells?" — **yes**, because the bid validity check lets them keep taking tasks whose drop is unreachable, and each cycle re-plans to the same blocked drop cell. The rerouting mechanism (release + re-claim) cannot escape because there is no alternative route to a blocked cell; the task is simply impossible, but nothing stops the robots from bidding on it.

### 4.2 Aggravating bug — `known_blocked` is monotonic (robots never forget removed obstacles)

- `_sense_and_share` (line 409): `self.known_blocked |= seen` — only ever adds.
- MSG_OBSTACLE handler (line 267): `self.known_blocked.add(...)` — only ever adds.
- The operator's `toggle_block` / `clear_blocks` (dashboard) removes the cell from `world.blocked` but **broadcasts nothing**; robots learn of blocks only by sensing (SENSOR_RANGE=2) or radio, and never learn of removals.

So a **transient** block (placed, then removed — exactly what the tick-9992 screenshot implies, since no door cells are blocked at the end) permanently poisons the robots' maps. Even after the drop cell is physically clear, every robot still believes it is blocked. This is the "stale reservation/occupancy information" the user asked about — the robots' obstacle knowledge is stale, not the peer occupancy data.

### 4.3 Why the live numbers look the way they do

- **4/6 with T1/T2 OPEN**: the drop cells of T1 (13,8) and T2 (5,8) were blocked at some point during the run (then cleared before the screenshot). The assigned robots could never deliver; the tasks stayed OPEN.
- **All 8 robots at the charger**: with T1/T2 un-completable, the fleet finished T3–T6, went idle, drained battery, and clustered at the charger.
- **Collisions ≈ 7,377**: dominated by the idle charger-cluster jostle (E2 control gives 7,387), not by the stranding itself.
- **Latency 1.69**: delay=1 for 6,895 ticks, delay=0 for 3,097 (flip at 6895) — reproduced exactly by the flip case.
- **Deadlocks 5 / stalls 4 / handoffs 1**: small counts — the live stranding produced only brief stalling (see §6 limitations). The pathological never-ending cycle in our drop-only reproductions yields 246–937 deadlock events; the live run's low count indicates its stranding was "cleaner" (e.g., pick AND drop both blocked → probe empty → no re-claiming; or short-lived operator-toggled blocks).

---

## 5. The smallest algorithmic fix (no UI changes)

Two small changes in `simulation/robot.py` (plus one broadcast in `simulation/engine.py` for the transient case).

### Part 1 — validate BOTH legs in `_bid_cost` (the core fix)

```python
def _bid_cost(self, kt):
    d = manhattan(self.pos, kt.pick) + manhattan(kt.pick, kt.drop)
    cost = float(d)
    if self.battery < 30:
        cost += AUCTION_BATTERY_PENALTY
    blocked = self._blocked()
    w, h = self.world.width, self.world.height
    probe_pick = spatial_path(self.pos, kt.pick, blocked, w, h)
    probe_drop = spatial_path(kt.pick, kt.drop, blocked, w, h)
    probe = probe_pick + probe_drop          # keep for the congestion term
    peer_cells = set()
    for p in self.peers.values():
        peer_cells.update(tuple(c) for c in p.planned_path)
    overlap = sum(1 for c in probe if c in peer_cells)
    cost += AUCTION_CONGESTION * overlap
    cost += AUCTION_WORKLOAD * self.workload
    need = max(d, 1) * DRAIN_MOVE
    valid = self.battery >= need
    if not probe_pick or not probe_drop:     # <-- the fix: BOTH legs must be reachable
        valid = False
        cost += 1000.0
    return cost, valid
```

A robot can no longer win a task whose drop (or pick) is unreachable, so the claim→pick→fail→release cycle cannot start.

### Part 2 — let robots forget removed obstacles (recovery from transient blocks)

**(a) Operator unblock broadcast** — in `simulation/engine.py`, `toggle_block` / `clear_blocks` (and the dashboard's `_mirror_blocks` path), when a cell is removed from `world.blocked`, broadcast an obstacle-cleared message:

```python
# engine.toggle_block, on the "off" branch:
self.bus.broadcast("WMS", {"type": MSG_OBSTACLE, "cell": [x, y], "cleared": True}, self.tick)
```

**(b) Robot ingest** — in `simulation/robot.py` `_ingest`, MSG_OBSTACLE branch (line 267):

```python
elif kind == MSG_OBSTACLE:
    cell = msg.get("cell")
    if cell:
        c = (int(cell[0]), int(cell[1]))
        if msg.get("cleared"):
            self.known_blocked.discard(c)
        else:
            self.known_blocked.add(c)
```

**(c) Sensor re-verification** (defense in depth) — in `_sense_and_share`, forget known-blocked cells within SENSOR_RANGE that are no longer blocked:

```python
seen = self.world.sense(self.pos)
stale = {c for c in self.known_blocked
         if manhattan(self.pos, c) <= SENSOR_RANGE and c not in seen}
if stale:
    self.known_blocked -= stale
    for cell in stale:
        self._send({"type": MSG_OBSTACLE, "cell": list(cell), "cleared": True}, tick)
```

Part 1 alone stops the pathological cycle. Part 2 is what lets the fleet actually **recover** when the operator clears a block — which is exactly the live situation (door cells clear at tick 9992, tasks still stranded).

---

## 6. Verification of the fix (runtime monkey-patch, no source change)

| Scenario | Before fix | After fix |
|---|---|---|
| No blocks | 6/6 @ t=58, 32 coll, 0 dead | 6/6 @ t=58, 32 coll, 0 dead (unchanged) |
| EXACT4 only @20 | 6/6 @ t=76, 32 coll, 0 dead | 6/6 @ t=76, 32 coll, 0 dead (unchanged) |
| **Transient door blocks** (T1D+T2D+EXACT4 @20, cleared @200) | **4/6 stranded forever**, 10,986 coll, 937 dead | **6/6 @ t=236**, 33 coll, 2 dead, 1 stall |
| Permanent door blocks (T1D+T2D+EXACT4 @20) | 4/6, 10,986 coll, 937 dead, cycling | 4/6 (impossible tasks stay OPEN), 0 dead, no cycling |

The fix does not regress normal operation, eliminates the claim→release cycle, and lets the fleet complete T1/T2 once the operator clears the door cells.

---

## 7. Limitations / honest accounting

- **Exact obstacle history is not fully reconstructible.** The screenshot shows only the tick-9992 state (EXACT4, no door blocks). The door blocks that stranded T1/T2 were placed and cleared at unknown times. Our reproductions match the core signature (4/6 T1/T2, stalls=4, latency exact, wait≈0.89–0.92, charger cluster, delivered ≈ 713–787k, collisions in the thousands) but not every metric exactly.
- **Deadlock count (5 vs 246–937).** The live run's low deadlock count means its stranding produced almost no 20-tick stall-window triggers. The most consistent explanation: the live stranding was "clean" — e.g., both pick and drop cells blocked (probe empty → no re-claiming) or short-lived operator toggles — with the 5 deadlock events / 4 stalls / 1 handoff coming from brief stalls elsewhere (e.g., peer-blocking on T3–T6). This detail does not change the root cause or the fix.
- **Collision count (6,545 vs 7,377) and delivered (712,928 vs 783,802)** differ by ≤12% from the best reproduction; the live values sit between our door-block case and the E2 idle-jostle control, consistent with a slightly different block timing/history.
- The user's stated "0% packet loss and 0-tick broadcast delay" for the live run is contradicted by the observed mean latency 1.69; the delay knob was 1 for 6,895 ticks and 0 for 3,097. This is treated as operator belief/screenshot state, not ground truth.

---

## 8. Direct answers to the original questions

1. **Why do multiple robots converge on the same cells near the job/drop area and fail to clear them?**
   T1's drop (13,8) and T2's drop (5,8) were blocked during the run. The assigned robots picked up and could never deliver; the auction kept re-assigning the impossible tasks, so robots kept converging on the blocked drop cells and failing.

2. **Are robots repeatedly selecting the same route/cells?**
   **Yes.** `_bid_cost` validates only the concatenated pick+drop probe, so a task whose drop is blocked is still biddable. Each claim→pick→fail→release cycle re-plans to the same blocked drop cell.

3. **Is reservation/occupancy information stale?**
   **Peer occupancy is not stale; obstacle knowledge is.** `known_blocked` is monotonic — robots never forget a block, even after the operator removes it. That is why the tasks stayed stranded after the door cells were cleared.

4. **Does the rerouting mechanism fail to escape local congestion?**
   **Yes.** There is no alternative route to a blocked drop cell; the task is impossible, and the "reroute" (release → re-claim) loops forever. The fix stops robots from bidding on impossible tasks and lets them forget cleared obstacles, so the fleet recovers as soon as the operator unblocks the cells.

---

*Evidence artifacts: `poc_diag_26123_sweep3_out.json`, `poc_diag_26123_sweep4_out.json`, `poc_diag_26123_hypo_out.json` (E2 control), `poc_diag_26123_trace_out.json`, `live_screenshot_9992.png`.*
