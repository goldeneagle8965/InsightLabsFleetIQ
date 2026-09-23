# Mandatory requirements — SIH26123

These must work before any extra feature is added.

1. **At least three robots**, each with its own local planner and local state. No `FleetScheduler.assign(robot, task)` function exists.
2. **Peer state broadcast** at a fixed tick rate. Schema:
   `robot_id, position, velocity, current_task, planned_path, battery, priority, timestamp` plus `status, task_urgency, workload` used by auction/yield.
3. **Task allocation via auction.** Cost = distance + battery penalty + congestion + workload. Lowest *valid* cost wins. Validity: enough battery to finish pick+drop.
4. **Conflict detection** from peer `planned_path`, not from a central occupancy oracle.
5. **Local negotiation** messages (`YIELD_QUERY`, `YIELD_ACK`, `YIELD_NACK`).
6. **Deadlock resolution** with an explicit, explainable rule (UBPA).
7. **Replanning** when a sensed/broadcast obstacle blocks the path.
8. **Task reassignment** when the current holder cannot reach the pick/drop (release + re-auction).
9. **Chaos input:** operator can block a cell/aisle live.
10. **Measured metrics vs stop-and-wait baseline:** collisions, deadlocks, task completion time, waiting time, distance, communication latency, CPU time per robot tick (measured by each edge node from its own process counters via `process_resources`, reported in the `summary`/`host_summary` JSON written by `--summary-json`; on the Windows dev host this reads **not measured**, since the platform exposes neither `resource` nor `/proc`).

## Explicitly forbidden

- A central path coordinator that reserves cells for all robots.
- Hardcoded winner of the head-on demo (`if robot_id == "R1": pass`).
- Hardcoded metric strings in the UI.
- Placeholder `def resolve_deadlock(): return None`.
