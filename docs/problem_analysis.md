# SIH26123 — Problem Analysis

**PS:** Edge-AI Distributed Fleet Coordination  
**Prototype type:** Multi-robot warehouse simulation + edge-style local planners  
**One-line identity:** Decentralized robots that negotiate routes instead of waiting for a central controller.

## What the judges are being shown

Three or more warehouse robots that do **not** wait for a central brain. Each robot:

1. Broadcasts its own state to peers
2. Detects conflicts from those broadcasts
3. Negotiates locally who goes first
4. Yields or replans in real time
5. Reassigns a task when a corridor is blocked

A global scheduler that *looks* decentralized on a dashboard is the failure mode of this PS. If a judge asks “does Robot A know what Robot B is doing directly, or through something else?”, the answer must be: **directly, through the peer broadcast**.

## Mandatory vs original work

| Layer | What it is |
|---|---|
| Mandatory | ≥3 robots, peer state broadcast, local planning, conflict detection, deadlock handling, reroute, task reassignment |
| India-specific | Hyderabad 3PL warehouse layout (narrow rack aisles, two highways, depot/drop) |
| Original algorithm | **UBPA right-of-way** (Urgency, Battery, Priority, Arrival) with deterministic tie-break |
| Evaluation | Same scenario vs stop-and-wait baseline, measured not asserted |
| Killer demo | Head-on aisle deadlock + live “Block aisle” chaos button |

## What this prototype is / is not

**Implemented:** grid warehouse, independent **robot decision stacks** (one OS process in the benchmark run; one process per robot over real UDP on the edge path — `simulation/edge/`, `tests/test_edge_lan_integration.py`), simulated radio, auction, space-time local planner, UBPA yield, obstacle broadcast, live dashboard, experiments, tests.

**Simulated:** radio in the benchmark path (the edge path sends real datagrams), lidar-range obstacle sensing, battery drain. Edge CPU time is **measured**, not simulated: each edge node times its own tick loop and reads its own process CPU/RSS via `process_resources` (`simulation/edge/agent.py`), emitted with `--summary-json` and rolled up by `scripts/edge_hw_summary.py`. On platforms without the `resource` module or `/proc`, those fields are reported as **not measured** rather than estimated.

**Not claimed:** real AMRs, real 5G/Wi-Fi mesh, on-robot GPU inference, warehouse production deployment.

## Risks

- Accidentally introducing a central dispatcher while wiring the dashboard.
- Both robots yielding (or neither) because of stale peer state — must have a deterministic timeout tie-break.
- Fabricating “40% faster” without a baseline run.
- Demo that only works as a recording.
