# 07 — Test plan

Two new files covering T1-T11 as 42 `unittest` methods, plus a third covering T12 (the LAN admission path)
as 17 more — 59 edge tests in total, all runnable without hardware and without new dependencies:

```bash
python -m unittest tests.test_edge_agent tests.test_edge_no_central_scheduler
python -m unittest tests.test_edge_lan_integration
# full suite (91 tests; note tests/ has no __init__.py, so unittest discovery is not used)
for m in test_mandatory test_dashboard_contract test_dashboard_is_not_required_for_decision \
         test_web_entry test_edge_agent test_edge_no_central_scheduler test_edge_lan_integration; \
  do python -m unittest tests.$m; done
```

`T12` is the one that needs real sockets: three separate interpreters, three real UDP listen ports, and a
long-running host process — the same code path `edge-pi` will take, minus the second machine.

Design of the plan: **T1 is an equality test, not a smoke test.** The edge path must reproduce the
existing simulation bit-for-bit in lock-step loopback mode, otherwise "same robot logic runs locally or
connects to the simulator" is an unproven claim. Everything after T1 tests the things the distribution
*adds*: loss, death, staleness, and the absence of a coordinator.

---

## 1. Test inventory

| ID | Name | File | Purpose |
| --- | --- | --- | --- |
| T1 | `test_lockstep_parity_with_engine` | `test_edge_agent.py` | edge loop-back reproduces `FleetSim` exactly |
| T2 | `test_worldview_equivalent_to_warehouse` | `test_edge_agent.py` | the 7-member world adapter is faithful |
| T3 | `test_codec_roundtrip_and_malformed` | `test_edge_agent.py` | wire encoding, chunking, hostile input |
| T4 | `test_host_cannot_move_without_act` | `test_edge_agent.py` | host is physics, not a controller |
| T5 | `test_agent_death_is_survivable` | `test_edge_agent.py` | fleet continues when a node dies |
| T6 | `test_degraded_links_40_and_70_pct` | `test_edge_agent.py` | loss degrades, never deadlocks |
| T7 | `test_no_decision_identifiers`, `test_no_writes_to_robot_decision_attributes`, `test_apply_moves_only_inside_apply`, `test_world_robots_written_only_at_spawn`, `test_no_cost_ordering`, `test_no_assignment_call`, `test_host_head_args_are_not_peer_decisions` | `test_edge_no_central_scheduler.py` | static AST guard on `host.py` + `dashboard.py` |
| T8 | `test_dashboard_refuses_decision_actions`, `test_every_existing_operator_action_is_a_knob_not_a_decision`, `test_spectator_view_is_inert`, `test_host_never_originates_a_peer_decision`, `test_host_does_not_take_over_a_robot_whose_act_was_lost` | `test_edge_no_central_scheduler.py` | behavioural guard on the API surface |
| T9 | `test_benchmark_digest_unchanged` | `test_edge_agent.py` | shipped benchmark preserved |
| T10 | `test_offline_fallback_intact` | `test_edge_agent.py` | deterministic sim still works; agent is stdlib-only |
| T11 | `test_telemetry_exposes_link_health` | `test_edge_agent.py` | degradation is measured, not asserted |
| T12 | `TestThreeAgentsOverRealSockets`, `TestAnUnadoptedAgentGivesUp` | `test_edge_lan_integration.py` | the whole thing runs as separate processes over real UDP: bind, adopt, decide, die, rejoin, give up |

---

## 2. T1 — lock-step parity (the load-bearing test)

**Setup.** `EdgeHost(n_robots=3, policy="ubpa", seed=1, env="warehouse", radio_delay=1)` driving three
in-process `EdgeAgent`s with `priority=1` and default `CoordinationFlags`. Reference:
`FleetSim(n_robots=3, policy="ubpa", seed=1, radio_delay=1)`, stepped the same number of ticks. (The host
has no separate `lockstep` switch: lock-step loop-back *is* the host's default, and the parity test drives
both one tick at a time.)

**Assertions.**

1. Per-tick, per-robot position sequences are identical for the whole run (not just at the end).
2. `world.robots` final state is identical; `collision_events` identical. (Both counters mean *conflicting claims, deduplicated per robot pair per tick* — see T4 step 4 — so "identical" compares conflict counts, not physical near-misses.)
3. `RunMetrics.to_dict()` matches on `collision_count`, `deadlock_count`, `stall_count`,
   `stall_handoff_count`, `all_tasks_complete_tick`, `total_distance`, `tasks_done`, `tasks_total`
   (`mean_robot_step_ms` is timing-only and excluded).
4. Task board state (`status`, `holder`, `complete_tick` per task) identical.
5. The robots' `known_tasks`/`auctions` keys are identical (proves peer traffic, including bids and
   claims, was delivered with the same timing).

**Why it can be exact.** The engine's per-tick order (`bus.tick` → robot steps → `apply_moves` →
`note_result`, `engine.py:136-146`) is reproduced by the host (document 01 §5), and `LoopbackRadio`
delivers with `PeerBus`'s delay-1 semantics. Any deviation means the adapter changed the robot's
information — the exact class of bug this test exists to catch.

**Failure interpretation.** A mismatch in step 1 only ⇒ observation/shape bug. A mismatch that appears
first at the first `BID`/`CLAIM` ⇒ peer-delivery ordering bug. A mismatch in metrics only ⇒ host
bookkeeping (`_sync_task_board`/`_watch_progress` equivalents) diverged.

---

## 3. T2 — world adapter equivalence

For each of several ticks and every robot position, compare `RemoteWorldView` to the host `Warehouse`:

| Access | Assertion |
| --- | --- |
| `width`, `height` | equal |
| `walls` | equal as sets (order-independent) |
| `turnouts` | equal as sets |
| `charger` | equal |
| `in_bounds(p)` | equal for a grid sweep including all four borders and out-of-range cells |
| `sense(pos, rng)` for `rng ∈ {0,1,2,5}` | equal as sets, before and **after** an operator block, and after an `env` switch (map reload) |

This is the test that proves "no edit to `robot.py`" is safe: every world attribute the robot reads is
answered identically by the network view.

---

## 4. T3 — codec and hostile input

1. Round-trip every message type through `encode()` → bytes → `decode()` ⇒ dict equal (tuples become
   lists on the wire and are re-tupled by the receiver).
2. `PEER_STATE.from_msg(decoded)` returns a `PeerState` equal to the original.
3. A `HELLO_WORLD` larger than 1200 B is chunked into `parts` and reassembled byte-identically.
4. Malformed input is rejected without an exception escaping: truncated JSON, non-object JSON (`[1,2,3]`),
   missing `type`, empty `type`, wrong `v`. The shipped test asserts `decode_safe(...) is None` and
   `decode(...)` raises `ProtocolError` for each. Note the boundary honestly: a payload that is *valid JSON
   with a valid type* but a missing `from_id` is **not** rejected at the codec - it is a higher layer's job,
   and the host/agent counters (`malformed`, `unknown_frames`, `foreign`) record it there.
5. Unknown `type` is counted and dropped, never dispatched.
6. Host control is directly addressed: every `OBS`/`ACT_RESULT` the host sends carries `to_id == <that rid>`
   and is delivered to that agent only - the assertion the shipped test makes in place of a broker check.
7. The host is not a peer broker: a `PEER_STATE` frame handed to the host is refused and counted
   (`peer_leaked`). Transport-level source allow-listing lives in `UdpRadio` and is exercised separately.
8. A 200-datagram junk flood from one source is bounded and does not stop the decision loop (T20 in
   doc 06). The shipped host counts-and-drops rather than rate-limiting - see doc 06 §5.

---

## 5. T4 — the host cannot move a robot

1. Withhold all `ACT`s for one tick: every robot stays exactly where it was, and `ACT_RESULT.accepted` is
   `false` with `blocked_by = "late"` when that agent finally sends.
2. Send an `ACT` for a robot the host does not know ⇒ ignored, counted, no world mutation.
3. Send an out-of-bounds `ACT` and an `ACT` into a wall ⇒ robot stays; `blocked_by` says `bounds`/`wall`.
4. Two agents intentionally send the same empty cell ⇒ `apply_moves` counts one collision, both stay, and
   both are told `collision = true`. No arbitration happens anywhere. Note the units: one event per
   unordered robot pair per tick (`collision_pairs` dedupe key is `(a, b, tick)`). The same pair conflicting
   on consecutive ticks is one event per tick, a three-way claim for one cell is two events, and a step into
   a cell whose occupant is *staying* is a blocked step, not a collision at all.
5. Patch `host.apply` to record calls: assert the only destination ever passed to `apply_moves` is one
   that arrived in an `ACT` (or the robot's own current position). This is the behavioural twin of T7.

---

## 6. T5 — agent death is survivable

Run 3 agents; stop feeding agent `R3` at tick ~30 (no `ACT`, no `HEARTBEAT`). Assert:

1. The run still completes all tasks (`all_tasks_complete_tick` is not `None`) within the tick limit.
2. `R3`'s position never changes after its last accepted `ACT`.
3. No new collision is attributed to `R3` after it went silent.
4. `R3`'s in-flight task is released/expired and completed by another robot (`tasks_done` still equals
   `tasks_total`).
5. The host reports `agents[R3].state = "dead"` and the snapshot is still renderable by the existing
   dashboard contract (re-run the existing `check_snapshot` field assertions against the edge snapshot).

---

## 7. T6 — degraded links

`LoopbackRadio(drop_prob=0.4)` and `drop_prob=0.7`, seeds 1-3, same map as E3:

1. UBPA still completes every task in both conditions (no infinite run).
2. `deadlock_count` does not exceed the stop-and-wait baseline for the same seed. The shipped assertion is
   `<=` the baseline, not `== 0`: measured on the warehouse map, 0.4 loss reaches 0 deadlocks but 0.7 loss
   reaches 1 for some seeds, still at or under the stop-and-wait baseline. Claiming a flat zero would be
   nicer and wrong.
3. Loss is measurable: the host's `link.lost_pct` is within a tolerance band of the configured value.
4. The degradation ladder from doc 06 §2 is monotonic in the one direction that matters: at 70% loss the
   completion tick is ≥ the 0%-loss completion tick.
5. A partition variant (peer channel dead, host alive) still completes, with the makespan penalty
   recorded rather than asserted away.

---

## 8. T7 — static guard: no decision code outside the agents

Parse `simulation/edge/host.py` and `simulation/dashboard.py` with `ast` and assert:

| Forbidden in host/dashboard | Why |
| --- | --- |
| any reference to `ubpa_score`, `UBPA_U/B/P/A`, `_winner`, `best_bidder =` | choosing an aisle/priority winner |
| any import or call of `planner`, `_plan`, `_choose_move`, `_choose_goal` | route planning |
| any assignment to `Robot.goal`, `Robot.current_task`, `Robot.phase`, `Robot.planned_path`, `Robot.policy` | steering a robot's decision |
| any call to `broadcast` with `BID`/`CLAIM`/`RELEASE`/`YIELD_*` from the host | speaking on a robot's behalf |
| a `sort`/`min`/`max` over per-robot costs or bids | implicit auction by proxy |
| `world.robots[rid] = ...` outside construction (`reset`/`_build_loopback`/`_build_udp`) | teleporting a robot mid-run; seating it at its spawn is not a decision |
| `Warehouse.assign` / `FleetScheduler` anywhere in the tree | the forbidden central component |

Implementation notes to avoid false positives: the guard inspects attribute *writes* (Store nodes) and
call targets rather than raw text, and explicitly allows `_auction_view`-style read-only projections
(reading `bot.auctions` to display a bid count is not choosing).

---

## 9. T8 — behavioural guard: the server cannot decide

1. POST every existing operator action, plus a set of adversarial ones
   (`set_winner`, `assign_task`, `force_route`, `yield_now`, `resolve_deadlock`) ⇒ the existing action
   list behaves as before and the unknown ones return no-op/ignored, never a decision.
2. Snapshot the fleet, then mutate the host's spectator view (e.g. reorder `auctions`, tamper with
   `best_bidder`) ⇒ step the run and assert the robots' `goal`, `current_task` and `planned_path` are
   unchanged by the tampering. Display data is provably inert.
3. Assert the dashboard never sends a decision message: instrument the host's send path and its WMS
   broadcast and assert the only messages the host originates are `OBS`, `ACT_RESULT`, `HELLO_WORLD`,
   `SHUTDOWN` and the `TASK_ANNOUNCE` bulletin. `HEARTBEAT` is agent-originated and is *not* on the host's
   emission list (doc 04 §1.6).
4. Assert a robot whose `ACT` the host never received still decides on its own once the link returns
   (i.e. the host cannot "take over" an unresponsive robot).

---

## 10. T9 — the shipped benchmark is untouched

1. `sha256(results/metrics.json) == 6d7d2a9600cd99dbb0e7419a7554747c0610119c078b010ce8f07806e1cea4f8`.
2. Same digest for `results/degradation.json` if present, recomputed before and after the suite.
3. No modification to `simulation/engine.py`, `simulation/robot.py`, `simulation/world.py`,
   `simulation/peer.py`, `simulation/maps.py`: the shipped test embeds their `sha256` values (recorded while
   the tree was clean against HEAD) and re-hashes the working tree, so it needs no git and works on a bare
   copy. Regenerating those constants is a deliberate act, not an accident.
4. The edge path writes nothing into `results/`: assert no new files appear there after an edge run.

---

## 11. T10 — offline fallback and dependency hygiene

1. `FleetSim` still runs a complete experiment in-process (`all_done()` within the tick limit) — the
   deterministic artefact the benchmark depends on.
2. Importing every module in `simulation/edge/` requires no third-party package: the shipped test parses
   each module's AST and asserts every import root is in `sys.stdlib_module_names` (or is `simulation`
   itself). Using the interpreter's own list beats a hand-maintained allow-list that goes stale.
3. The fallback has no hard edge dependency: the shipped test parses `engine.py`, `robot.py`, `world.py`,
   `dashboard.py` and `scripts/run_demo.py` and fails if any of them imports `simulation.edge`.
4. `EdgeAgent` constructs a working `Robot` with `LocalWorldView` + `LoopbackRadio` (the "run locally"
   half of the abstraction requirement).

---

## 12. T11 — telemetry

Assert the host snapshot exposes, and correctly counts:

| Field | Assertion |
| --- | --- |
| `link.sent`, `link.recv`, `link.lost_pct` | non-zero after a run; `lost_pct` ≈ configured loss for T6 |
| `link.late_acts` | > 0 when a tick deadline is deliberately missed |
| `link.malformed` | increments on the T3 hostile inputs |
| `link.mean_rtt_ms` | finite and `> 0`: a real round trip is measured, not estimated (~0.3 ms on the loop-back) |
| `agents[*].last_seen_tick`, `tick_lag` | present for all live agents, lag bounded by the LAN |
| `agents[*].step_ms_p95` | present and `>= 0`; it is the number behind the throttling discussion in doc 05 §6 - the host publishes it, it does not derive a warning flag (doc 06 §5) |
| existing headline metrics | all fields required by `tests/test_dashboard_contract.py::check_snapshot` still present |

The last row is the important one, with one honest caveat: the host's *own* fields (robots, tasks, metrics,
`radio`, `agents`, `link`) satisfy `check_snapshot` unchanged, but the host does **not** own the
dashboard-only blocks (`shadow`, `speed_steps`, `envs`, `server`, `measured`, `remeasure`). The test attaches
those from the existing `Dashboard` helpers before calling `check_snapshot`, and `scripts/run_edge_host.py`
does the same in a small presentation adapter. So the UI does not fork and the client contract is met - but
"drop-in" would overstate it: the edge host is a **contract-compatible core**, not a whole `Dashboard`.

---

## 13. Acceptance criteria

| Criterion | Evidence required |
| --- | --- |
| Same robot logic, two homes | T1 (equality), T2 (adapter fidelity), T10.4 (local construction) |
| No central scheduler | T7 (static), T8 (behavioural), T4 (host cannot move a robot) |
| Explicit peer-to-peer | T3.6 (direct unicast, no broker), T8.3 (host emits no peer decisions) |
| Dashboard cannot decide | T8.1-3 |
| Lossy reality handled | T6, T11 |
| Benchmark preserved | T9 |
| Offline fallback intact | T10 |
| Nothing regressed | the pre-existing 25 tests still green |
| Runs as separate processes | T12 (three interpreters, real sockets, host admits by source address, a real agent dies and rejoins, an unadopted agent exits rather than squatting a port) |

**Pass condition for the whole pack:** the 32 pre-existing tests (25 simulation + 7 dashboard-independence)
plus T1-T12 green, `results/metrics.json` and `results/degradation.json` digests unchanged, and the edge
loop-back run reporting the same completion tick as `FleetSim` for the same seed. As shipped: 91 tests, OK,
with `metrics.json = 6d7d2a96…4f8` unchanged. Anything less is reported as a limitation rather than presented
as a result.
