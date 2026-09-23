# 00 — Audit summary: what the current code really does

**Scope of this audit.** The repo as shipped (`sih26123/`, commit state at time of writing), audited
against the official problem-statement identity for SIH26123 ("Decentralized robots that negotiate
routes") and against `docs/mandatory_requirements.md`. This is an engineering audit, not a
penetration test: no target other than this repository was touched.

**Method.** Static reading of every module in `simulation/`, plus execution evidence:

| Evidence | Command | Result |
| --- | --- | --- |
| Existing suite | `python -m unittest tests.test_mandatory tests.test_dashboard_contract tests.test_web_entry` | 25 tests, all green (~1.8 s) |
| Edge suite (added by this pack) | `python -m unittest tests.test_edge_agent tests.test_edge_no_central_scheduler` | 42 tests, all green; includes the T1 lock-step parity equality and the T7/T8 "no central scheduler" guards |
| Edge LAN suite (real sockets) | `python -m unittest tests.test_edge_lan_integration` | 17 tests, all green (~10 s); three separate interpreters over real UDP, host admission by source address, agent death and rejoin, duplicate-claim and rebind rules, exclusive-bind refusal (exit 2), unadopted give-up (exit 3) |
| Dashboard independence | `python -m unittest tests.test_dashboard_is_not_required_for_decision` | 7 tests, all green |
| Whole tree | the seven modules above, run one at a time (`tests/` has no `__init__.py`, so discovery is not used) | **91 tests, OK** |
| Shipped benchmark | `python scripts/run_experiments.py` (fresh output dir) | reproduces `results/metrics.json`; sha256 `6d7d2a9600cd99dbb0e7419a7554747c0610119c078b010ce8f07806e1cea4f8` |
| Process model | `grep -rnE "multiprocessing\|asyncio\|socket\|subprocess" simulation/` | no hit in the simulation path; only `threading` in `dashboard.py` |

---

## 1. The one-line verdict

The **decision stack is genuinely decentralized and genuinely robot-local**. The **execution model is
not**: all robots are Python objects stepped in one loop inside one OS process, and the "radio" is an
in-process mailbox. The gap between the two is exactly what the edge design in documents 01–07 closes,
and it can be closed without touching `robot.py` or `engine.py` because the robot is duck-typed against
seven world members and two bus methods.

---

## 2. Requirement-by-requirement status

Status vocabulary used throughout this pack:

- **DEMONSTRATED** — implemented, exercised by the shipped benchmark and/or the test suite.
- **IMPLEMENTED / UNEXERCISED** — code exists and is correct, but the benchmark never triggers it.
- **SIMULATED-ONLY** — true inside one process, false as a physical/network claim.
- **OVERCLAIMED** — documentation asserts something the code does not do.
- **MISSING** — not in the repo at all; targeted by the edge design.

| # | Requirement (official PS / `mandatory_requirements.md`) | Status | Code evidence |
| --- | --- | --- | --- |
| 1 | ≥3 robots, each with local planner + local state, no `FleetScheduler.assign` | **DEMONSTRATED** | `robot.py:_plan/_choose_move/_bid_cost`; no scheduler class exists anywhere |
| 2 | Peer-state broadcast at a fixed tick rate, full schema | **DEMONSTRATED (in-process)** | `robot.py:1056 _broadcast_state`, `peer.py:PeerState.to_msg`, `TICK_HZ = 8` |
| 3 | Allocation by auction, cost = distance + battery + congestion + workload, lowest valid wins | **DEMONSTRATED** | `robot.py:517 _auction`, `574 _bid_cost`, `607 _winner` (local, per-robot comparison) |
| 4 | Conflict detection from peer `planned_path`, not a central occupancy oracle | **DEMONSTRATED** | `robot.py:878 _conflict_peer`, `1032 _peer_occupied` |
| 5 | Local negotiation `YIELD_QUERY` / `YIELD_ACK` / `YIELD_NACK` | **DEMONSTRATED** | `robot.py:347 _on_yield_query`, `390 _on_yield_ack`, `398 _on_yield_nack`, unicast via `to_id` |
| 6 | Explicit, explainable deadlock rule (UBPA) | **DEMONSTRATED** | `robot.py:892 ubpa_score` (U/B/P/A weights in `config.py:33-36`), `908 _resolve_conflict`; measured 0 deadlocks vs 3 for stop-and-wait (E3) |
| 7 | Replan when a sensed/broadcast obstacle blocks the path | **DEMONSTRATED** | `robot.py:409 _sense_and_share` → `MSG_OBSTACLE`, `823 _plan`; operator aisle block is the chaos input |
| 8 | Task reassignment when the holder cannot finish (release + re-auction) | **DEMONSTRATED** | `robot.py:664 _release`, `738 _expire_claim`, `751 _check_stall` + stall hand-back (RC5) |
| 9 | Chaos input: operator blocks a cell/aisle live | **DEMONSTRATED** | `engine.py:163 toggle_block`, `190 block_aisle`; dashboard actions `toggle`/`block_aisle`/`clear_blocks` |
| 10 | Measured metrics vs stop-and-wait baseline | **DEMONSTRATED** | `results/metrics.json` (numbers below), `metrics.py:summarize_pair` |
| 11 | Forbidden: central path coordinator reserving cells | **NOT PRESENT** | `world.apply_moves` only *records* the robots' own intentions and counts collisions; it never picks a move |
| 12 | Forbidden: hardcoded winner / placeholder `resolve_deadlock` | **NOT PRESENT** | `_resolve_conflict` is a full UBPA comparison; no `if robot_id == "R1"` anywhere |
| 13 | Forbidden: hardcoded metric strings in the UI | **NOT PRESENT** | every headline number in `frontend/static/app.js` comes from a snapshot field; no literal metric values in the JS |
| 14 | Robots run as **independent processes / on separate hardware** | **MISSING in the simulation path; built by the edge pack** | `engine.py:138-139` — one `for bot in self.robots: bot.step(tick)` loop, single process, no threads. The edge path runs each robot as its own OS process over real UDP (`simulation/edge/`, `scripts/run_edge_host.py` + `run_edge_agent.py`), proven by `tests/test_edge_lan_integration.py` (three interpreters, `3/3` admitted). Two physical machines remain unexercised — doc 05 §10 |
| 15 | Peer messaging over a **real network** between agents | **SIMULATED-ONLY in the benchmark path; real UDP on the edge path** | `peer.py:PeerBus` — `deque` + `defaultdict(list)` mailboxes, no sockets; `FleetSim` keeps using it. `simulation/edge/radio.py:UdpRadio` sends real datagrams (peer unicast between agents, the host address allow-listed for control frames), and the host never relays peer traffic (`peer_leaked` counter) |
| 16 | On-**device** inference at the edge | **MISSING** | UBPA is closed-form arithmetic on locally held state; no model, no accelerator, no ML runtime. Calling it "Edge-AI" is only defensible as "on-device decision stack" |
| 17 | Battery-aware behaviour (bidding penalty, drive-to-charger) | **IMPLEMENTED / UNEXERCISED** | `config.py:22-27`, `robot.py:429 _maybe_charge`; E1 UBPA travels ~151 moves ≈ 27 battery units, so `CHARGE_THRESHOLD = 20` is never crossed. Only the dashboard `drain` action forces a charge cycle |
| 18 | Fleet scaling beyond the demo 3 robots | **DEMONSTRATED** | `engine.py:216 set_robots` (3–8), exercised by the dashboard contract test across every map |

---

## 3. Overclaims to fix in the documentation (honesty items)

These are wording problems, not code problems, but a domain-aware judge will ask about them. Each is
fixed by an explicit correction in this pack rather than by deleting the original claim.

| Claim | Where | Reality | Correction |
| --- | --- | --- | --- |
| "independent robot processes" | `docs/problem_analysis.md` | robots are objects in one process (`engine.py:138`) | state it as *independent robot decision stacks*; document 01 shows the hardware path that makes "process" literally true |
| "Four indoor floors" | `README.md` | `simulation/maps.py` holds four **single-floor** 25×11 layout variants (warehouse, mall, hospital, restaurant) | say "four selectable floor plans (single level each)" |
| "Edge-AI" as a modelling claim | `README.md`, decks | UBPA is a heuristic, no learned model | "on-device coordination policy"; keep the AI framing for the negotiation/auction layer, which is real |
| Dashboard "runs" the fleet | reader's likely assumption | the dashboard *drives* a `FleetSim` at a wall-clock multiplier and edits operator parameters; it holds no decision authority (`grep` for winner/assign in `dashboard.py` returns only a docstring) | documents 01/03 + test `T8` freeze this as a contract |
| Shadow twin "compares UBPA vs stop-and-wait live" | `dashboard.py:_shadow_view` | true, and it is measurement only — a second `FleetSim` with the same seed, driven for the side panel | keep, but label as *measurement*, and assert by test that the shadow never writes into the live sim |
| "5 seeds", read as five independent trials | `README.md`, `docs/evaluation.md`, the decks | re-measured directly, seeds 1–5 return **identical** outcome metrics on E1 for both policies (same completion, tick, collisions, deadlocks, distance); only wall-clock `mean_robot_step_ms` moves | call them a **determinism check, not n = 5**: see the new *Seeds* section of `docs/evaluation.md`. Real variation is shown by the degraded-radio and scale sweeps, where the scenario changes |
| `collision_count` read as robot-on-robot contact | `docs/evaluation.md` metric list, decks | `world.apply_moves` counts **conflicting claims deduplicated per pair per tick**: a conflict over consecutive ticks counts once per tick, a three-way claim for one cell counts as two pairs, and a blocked step into an occupied cell is not a collision at all | documented in the metric list of `docs/evaluation.md` and in doc 07 §4/T4; both policies are scored by the same rule |
| "UBPA = 0 collisions" without a qualifier | `README.md`, decks, `docs/hardware_feasibility.md` | true on the E1/E2/E3 layouts; on a low-contention aisle layout with three short jobs UBPA records **4 conflicts and finishes 11 ticks later than stop-and-wait** (47 → 58) | spawn-specific claim: the counterexample and its reproduction snippet are published in `docs/evaluation.md` ("Where UBPA is not the better policy") and `docs/demo_script.md` |
| An implied "percentage target" for the benchmark | repo-wide search, including `SIH_2026_Problem_Statements.pdf` line 216 | there is **no percentage target in any source**: the problem statement is one line, and the prototype guide's instruction is to "compare against a stop-and-wait baseline and report only measured improvement" | state the E1 result as **completion and robustness**, never as a conversion of the 800-tick horizon against the 93-tick finish |

---

## 4. The coupling surface that makes the edge port cheap

Two interfaces, and nothing else, connect a robot to the outside world.

**World (7 members, all read-only for the robot):**

| Member | Used at (robot.py) | Edge plan |
| --- | --- | --- |
| `width`, `height` | 584-592, 835, 850-863, 1001-1011 | static, sent once in `HELLO_WORLD` |
| `walls` | 821, 1022 | static, sent once in `HELLO_WORLD` |
| `turnouts` | 999-1011 | static, sent once in `HELLO_WORLD` |
| `charger` | 213, 437, 441, 499 | in every `OBS` (cheap, tolerates map switch) |
| `sense(pos, rng)` | 410 | host computes `nearby_blocked` (Manhattan ≤ `SENSOR_RANGE = 2`) against the authoritative grid and ships it in `OBS` |
| `in_bounds(p)` | 1022 | pure function of `width`/`height`, derived locally |

**Bus (2 methods):**

| Member | Used at | Edge plan |
| --- | --- | --- |
| `broadcast(sender, msg, tick)` | 1057, 1060 | `RemoteRadio.broadcast` → UDP datagram to the peer table (honours `to_id` unicast) |
| `receive(robot_id)` | 247 | `RemoteRadio.receive` → drain the socket into a list, same shape as the mailbox |

Nothing in `robot.py` touches `world.blocked`, `world.robots` or `world.tasks`. That is why an adapter
with those seven attributes plus a two-method radio can host an unmodified `Robot` — verified by test
`T1`/`T2` in document 07.

---

## 5. Measured baseline that must not move

`results/metrics.json`, `tick_limit = 800`, seeds E1 = 1-5, E2/E3 = 1-3. Averages over seeds:

| Exp | Policy | Collisions | Deadlocks | Stalls | Tasks | Finish tick | Distance | Mean wait |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| E1 | stop-and-wait | 2.0 | 2.0 | 1.0 | 0/6 | — | 34.0 | 0.986 |
| E1 | UBPA | 0.0 | 0.0 | 0.0 | 6/6 | 93 (all seeds) | 151.0 | 0.459 |
| E2 | stop-and-wait | 0.0 | 2.0 | 1.0 | 1/3 | — | 19.0 | 0.992 |
| E2 | UBPA | 0.0 | 0.0 | 0.0 | 3/3 | 50 (all seeds) | 50.0 | 0.667 |
| E3 | stop-and-wait | 0.0 | 3.0 | 1.0 | 3/6 | — | 104.0 | 0.957 |
| E3 | UBPA | 0.0 | 0.0 | 0.0 | 6/6 | 99 (all seeds) | 171.0 | 0.424 |

sha256 of the file: `6d7d2a9600cd99dbb0e7419a7554747c0610119c078b010ce8f07806e1cea4f8`.

Two footnotes on that table. `Collisions` is *conflicting claims deduplicated per robot pair per tick* (see §3), not robot-on-robot contact, and a blocking step into an occupied cell is not counted at all. And "93 (all seeds)" is literal: the five seeds return the same finish tick and the same counts, which is a determinism property, not a mean — so the seed axis is n = 1 in outcome terms.

**Constraint accepted by this pack:** the edge work must not perturb this file. The edge code lives
under `simulation/edge/` and `scripts/run_edge_*.py` and is *additive*; `engine.py`, `robot.py`,
`world.py`, `peer.py` and `maps.py` are **not edited**. Test `T9` re-hashes the file after the whole
suite has run and fails if the digest moves.

---

## 6. What this pack deliberately does not do

Stated up front so the design is not read as more than it is:

1. **No rewriting of the simulation engine, and no new central component.** The host is physics, the
   sensing model, the WMS bulletin relay and a spectator — never a chooser (document 01 §4).
2. **No dashboard decision-making.** No new UI feature is added; the operator surfaces stay exactly
   what they are (`policy`, `env`, `robots`, `speed`, `radio`, `add_task`, `remove_task`, `drain`,
   `block_aisle`, `toggle`, `clear_blocks`, `pause`, `step`, `reset`, `forecast`) and none of them can
   pick a winner.
3. **No real robotics.** No SLAM, no motor control, no ROS. The Pis execute the existing decision stack
   against a simulated world; actuators are optional LEDs/buzzer for the demo only.
4. **No determinism claim across the network.** The in-process simulation stays the reproducible
   artefact. The distributed path is bit-identical only in lock-step loopback mode (test `T1`); on real
   hardware it is intentionally timing-dependent, which is the point of the exercise.
5. **No new dependency.** The agent is stdlib-only (`socket`, `json`, `time`, `selectors`), so it runs
   on a stock Raspberry Pi OS image.

---

## 7. Findings that need a decision from the team

| ID | Finding | Recommendation |
| --- | --- | --- |
| A1 | `docs/problem_analysis.md` says "independent robot processes"; the code is one process | reword now (cheap), and let the edge deployment make the stronger claim true |
| A2 | Battery/charging is never exercised by the shipped benchmark (E1 ≈ 151 moves vs ~444 needed to drain) | add a battery scenario to the docs as *unexercised-by-benchmark*; do not silently imply it is measured. A separate small run can demonstrate it without touching `metrics.json` |
| A3 | The dashboard's `_auction_view` computes a `best_bidder` for display | it is read-only aggregation of bids robots already broadcast. Document 01 classifies it as a spectator view, and test `T8` asserts the dashboard/host cannot set a winner |
| A4 | No authentication or integrity on the wire | acceptable for a demo LAN; document 03 §7 records the risk and the designed-but-optional `sig` field, instead of pretending the link is secure |
| A5 | `engine._sync_task_board` writes task holders back into the world | spectator bookkeeping derived from the robots' own broadcasts (they claim; the board only reflects it). Reproduced verbatim in the host for parity, and named as such |
