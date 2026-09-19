# 02 — Requirement matrix: current vs required

Columns: **Status today** (as audited in document 00), **Evidence** (file:line in the current code),
**Edge design element** (what document 01–07 provides), **Test** (document 07 id).

Status key: `D` = demonstrated · `U` = implemented but unexercised · `S` = simulated-only ·
`O` = overclaimed wording · `M` = missing.

---

## A. Official problem-statement identity

> "Decentralized robots that negotiate routes — warehouse AMRs coordinate without a central
> controller, sense each other, negotiate priority, and re-route themselves."

| # | Requirement | Today | Evidence | Edge design element | Test |
| --- | --- | --- | --- | --- | --- |
| A1 | ≥3 robots, each with local planner and local state | **D** | `engine.py:104-112`; `robot.py:823 _plan`, `1041 _choose_move` | 3 `EdgeAgent` processes, one `Robot` each | T1, T11 |
| A2 | Coordination without a central controller | **D** (in-process) | no scheduler class exists; `engine.py:138-139` only iterates | host authority boundary (§4 of doc 01) | T7, T8 |
| A3 | Robots negotiate **directly** (peer-to-peer, not via a scheduler) | **S** — direct *logically*, same process physically | `peer.py:94 broadcast`, `robot.py:1057` | `RemoteRadio`: unicast UDP straight between agents, host not a broker | T3, T6, T7 |
| A4 | Each robot **senses** others | **D** | `robot.py:409 _sense_and_share`, `246 _ingest` (peer states) | sensing modelled at the host, shipped in `OBS` as `nearby_blocked`; peers sensed from their own broadcasts | T2 |
| A5 | Negotiate **priority** | **D** | `robot.py:892 ubpa_score` (U/B/P/A), `908 _resolve_conflict`, `347-408` yield handshake | unchanged; `YIELD_*` now crosses a real network | T1, T6 |
| A6 | Re-route themselves | **D** | `robot.py:823-877 _plan`, `751 _check_stall` recovery | unchanged, executed locally on each Pi | T1 |
| A7 | Chaos: operator blocks a cell/aisle live | **D** | `engine.py:163/190`; dashboard actions | operator edits world on the host; agents learn through `OBS`/`OBSTACLE` only | T4 |
| A8 | Robots run **independently** (independent processes/hardware) | **M / O** | single `for bot in self.robots` loop, one process, no threads (`grep` in doc 00 §1) | 3 separate processes on 3 Pi nodes, own tick loop and socket | T10, T11 |
| A9 | "Edge-AI" inference on the robot | **M** (heuristic) | `ubpa_score` is closed-form; no model, no runtime | on-device decision stack: policy + auction + UBPA all computed on the node, zero host input | T7 (host feeds no scores) |
| A10 | Fleet scaling | **D** | `engine.py:216 set_robots` 3–8 | agent table is configuration; N agents work with no code change | T5 |

---

## B. `docs/mandatory_requirements.md` (the 10 + forbidden list)

| # | Requirement | Today | Evidence | Edge design element | Test |
| --- | --- | --- | --- | --- | --- |
| B1 | No `FleetScheduler.assign(robot, task)` | **D** | absent from the tree | host has no task-assignment code path; WMS relays bulletins only | T7, T8 |
| B2 | Peer state broadcast at a fixed tick rate, full schema | **D** | `peer.py:28 PeerState`, `robot.py:228 peer_state`, `TICK_HZ = 8` | schema unchanged on the wire (doc 04 §2.1) | T3 |
| B3 | Auction: distance + battery + congestion + workload, lowest valid wins | **D** | `robot.py:574 _bid_cost`, `607 _winner` (per-robot local view) | bids travel agent→agent; host never sees a bid | T7 |
| B4 | Conflict detection from peer `planned_path`, not an occupancy oracle | **D** | `robot.py:878 _conflict_peer`, `1032 _peer_occupied` | `OBS` deliberately carries **no** other-robot positions; only peers report peers | T7 |
| B5 | Local negotiation `YIELD_QUERY` / `ACK` / `NACK` | **D** | `robot.py:347/390/398`; unicast via `to_id` | unicast datagrams, no relay | T3, T6 |
| B6 | Explainable deadlock rule (UBPA) | **D** | `robot.py:892/908`; E3 shows 0 vs 3 deadlocks | unchanged | T1 |
| B7 | Replan on sensed/broadcast obstacle | **D** | `robot.py:409-419`, `823` | `OBSTACLE` messages unchanged; sensing comes from `OBS.nearby_blocked` | T4 |
| B8 | Reassignment when the holder can't finish | **D** | `robot.py:664 _release`, `738 _expire_claim`, `751 _check_stall` | release/expiry logic reused, now with real loss | T6 |
| B9 | Chaos input live | **D** | `engine.py:163/190` | host-side operator edit; propagates through `OBS` and `OBSTACLE` | T4 |
| B10 | Metrics vs stop-and-wait baseline: collisions, deadlocks, completion, wait, distance, comm latency, CPU/tick | **D** | `results/metrics.json`; `metrics.py` | `EdgeHost` inherits the same `RunMetrics` fields; adds real wall-clock hop latency and per-agent `step_ms` | T1, T11 |
| B11 | Forbidden: central path coordinator reserving cells | **absent** | `world.apply_moves` only applies | host authority boundary; T7 static check | T7 |
| B12 | Forbidden: hardcoded winner | **absent** | no such branch | T7 static check over `dashboard.py` + `host.py` | T7 |
| B13 | Forbidden: hardcoded metric strings in UI | **absent** | all headline values come from the snapshot | UI untouched | T8 |
| B14 | Forbidden: placeholder `resolve_deadlock` | **absent** | real UBPA implementation | unchanged | — |

---

## C. Constraints set for this piece of work

| # | Constraint | How it is honoured | Test |
| --- | --- | --- | --- |
| C1 | Do not rewrite the existing simulation engine | `engine.py`, `robot.py`, `world.py`, `peer.py`, `maps.py` are not edited at all; edge code is additive | T9 (hash of the modules + benchmark) |
| C2 | Do not move planning decisions into the dashboard | no UI change; dashboard action list unchanged; no decision endpoint added | T8 |
| C3 | No central scheduler | host authority boundary; static + behavioural guards | T7, T8 |
| C4 | Preserve the measured benchmark exactly | `results/metrics.json` untouched by the edge path; digest asserted | T9 |
| C5 | Preserve existing experiment JSON files | `results/` is read-only for this work; `remeasure_metrics.json` and the shipped metrics stay as they are | T9 |
| C6 | Keep the deterministic simulation as an offline fallback | `FleetSim`, `Dashboard`, `run_demo.py`, `RUN_DEMO.bat` still work; edge mode is a separate entry point | T10 |
| C7 | `edge_agent` abstraction: same robot logic local or remote | `EdgeAgent` wraps an unmodified `Robot`; `LoopbackRadio`/`RemoteRadio` and `LocalWorldView`/`RemoteWorldView` are interchangeable | T1, T2 |
| C8 | Host: give observations, receive actions; no winner/conflict/route authority | host code contains no bid, score, UBPA, winner or planner reference | T7 |
| C9 | Explicit peer-to-peer messaging between agents | UDP unicast agent↔agent; broker would be a single point of control | T3, T6 |
| C10 | Tests proving the dashboard/server cannot decide for the fleet | `tests/test_edge_no_central_scheduler.py` (static AST + behavioural) | T7, T8 |

---

## D. Edge-design-specific requirements (self-imposed, for the demo)

| # | Requirement | Design element | Test |
| --- | --- | --- | --- |
| D1 | One agent crash must not stop the fleet or cause a collision | hold-by-default + claim expiry (`CLAIM_SILENT_TICKS = 24`) | T5 |
| D2 | 40% and 70% packet loss must degrade, not deadlock | reannounce (RC1), peer expiry (6 ticks), claim arbitration | T6 |
| D3 | The host must apply only what arrived, and must report the outcome | `ACT_RESULT` with the authoritative position, consumed via `note_result` before the next step | T3, T4 |
| D4 | Map and static geometry must arrive once, not per tick | `HELLO_WORLD`/`WORLD` message with walls/turnouts/size | T2 |
| D5 | Bandwidth must stay inside a demo LAN budget | documented message sizes, ≤ 3 agents × ~6 msg/tick × ~300 B | doc 04 §5 |
| D6 | Agent must be dependency-free on Raspberry Pi OS | stdlib-only (`socket`, `json`, `time`) | T10 (import guard) |
| D7 | Loss/latency must be measurable, not asserted | rolling counters (`sent`, `recv`, `lost`, `rtt_ms`) exposed in the host snapshot | T11 |
