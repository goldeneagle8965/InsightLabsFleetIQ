# Methodology

## Local planner

Space-time A* on a 4-connected grid. State = `(x, y, t)`. Actions: N/E/S/W/WAIT. Peer `planned_path[t]` cells are reserved. Unknown future: robots assumed to wait at last known cell for a short hold horizon.

## Auction cost

```
cost = dist(pos, pick) + dist(pick, drop)
     + 40  if battery < 30
     + 8 * overlapping_peer_path_cells
     + 6 * tasks_completed_this_run
```

Invalid if estimated remaining cells * drain > battery. Each robot computes this locally, broadcasts `BID`, waits `BID_WINDOW` ticks, then independently selects `argmin(valid bids)`. Equal cost: lexicographically smaller `robot_id` wins so every robot agrees without a referee.

## UBPA right-of-way (original contribution)

Higher score keeps the aisle. Lower score yields to the nearest turnout (intersection, degree ≥ 3).

```
score = 4.0 * task_urgency
      + 2.0 * (battery / 100)
      + 1.5 * priority
      + 1.0 * arrival_bonus
```

`arrival_bonus` is larger when the robot reaches the first conflicting cell sooner (already committed). Tie: smaller `robot_id` has right of way.

If scores would make both yield or neither (stale state), after `STALEMATE_TICKS` the smaller `robot_id` yields. That is the safety net, not the primary rule.

## Stop-and-wait baseline

Identical robots, auction, map, and tasks. Conflict policy: if any peer occupies a cell on my remaining path within `LOOKAHEAD`, stop. No yield, no reverse to turnout. Mutual waiting increments `deadlock_count` after the stalemate timeout and stays stuck until the other moves (which they may not).

## Robustness fixes (RC1–RC5)

Each of these is a local repair a robot or the WMS can make from what it already hears on the radio. They are switches in `CoordinationFlags`, not forks of the code, and `results/probe_26123_ablation.json` records what each one is worth.

| | mechanism | constant |
|---|---|---|
| RC1 | the WMS repeats a bulletin for any task at least one robot has never heard (`_reannounce_unheard`) | `TASK_REANNOUNCE_TICKS` |
| RC2 | `reservations_from_peers()` projects a peer's path from the message timestamp, not from `now`, so an old packet reserves the ticks it actually meant | — |
| RC3 | fleet-level stall detector: no completion and no movement for a window, counted for both policies | `STALL_WINDOW` |
| RC4 | claim arbitration (one holder per task, ties broken by the same cost rule as the auction) and claim expiry when a holder stops refreshing | `CLAIM_GRACE_TICKS`, `CLAIM_SILENT_TICKS`, `CLAIM_REASSERT_TICKS` |
| RC5 | a holder that stalls `STALL_HANDOFF_EPISODES` times on the same task releases it with a broadcast, peers mark it OPEN and run a fresh auction, and the ex-holder cannot bid for it again until `STALL_COOLDOWN_TICKS` pass | `STALL_HANDOFF_EPISODES`, `STALL_COOLDOWN_TICKS` |

RC5 exists because a re-plan proves nothing about reachability: `_plan` resets `unreachable_ticks` whenever it finds a path, so the older "release when unreachable" path never fires for a robot that can always find *a* path but never gets through the contested junction. Counting stall episodes per task is the measurable substitute.

## Sensing

A robot only *sees* a user-blocked cell within Manhattan distance `SENSOR_RANGE` (2). It then broadcasts `OBSTACLE`. Others do not magically know the block.
