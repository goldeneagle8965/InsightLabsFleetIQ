# 06 — Failure modes, detection and recovery

Rule that governs every entry below: **degrade locally, never centralize.** No failure may be "fixed"
by giving the host a decision it does not have (no winner selection, no conflict arbitration, no route
planning). Where a failure genuinely cannot be handled locally, the fleet gets slower or smaller — it
does not get a scheduler.

Severity: **S1** demo-stopping · **S2** degrades visibly but survives · **S3** cosmetic/telemetry.

---

## 1. Failure table

| ID | Failure | Sev | Detection | Behaviour | Recovery | Test |
| --- | --- | --- | --- | --- | --- | --- |
| F1 | Agent process crashes / is killed | S2 | 3 consecutive `HEARTBEAT` misses (> 24 ticks) | host holds that robot (hold-by-default); peers drop it after `PEER_STALE_TICKS = 6`, then plan around the frozen cell; its task frees after `CLAIM_SILENT_TICKS = 24` and is re-auctioned | `systemd Restart=always` respawns it; it re-`HELLO`s and rejoins with a fresh `OBS_RESET`; the run never stops | T5 |
| F2 | Node loses power / cable pulled | S2 | same as F1 | identical to F1 | reconnect + respawn | T5 |
| F3 | Agent alive but silent (wedged in a long step, GIL/GC pause) | S2 | `HEARTBEAT` still present but `ACT` missing for N ticks | that robot holds; it is not treated as gone while heartbeats continue | automatic when the step completes; `step_ms_p95` in the snapshot explains it | T4 |
| F4 | `ACT` arrives after the tick deadline | S3 | host counter `late_acts`; `ACT_RESULT.late = true` | the intention is discarded, the robot holds, and it is told the truth so `note_result` keeps its state consistent | self-correcting next tick | T3 |
| F5 | `OBS` lost for a tick | S3 | agent counter `obs_missed` | agent steps on the last view; obstacle positions may be one tick stale (a realistic sensor model), and it never invents geometry | next `OBS` | T6 |
| F6 | 40% / 70% packet loss on the peer channel | S2 | host `link.lost_pct`; agent `lost_est` | RC1 reannounce (`TASK_REANNOUNCE_TICKS = 20`) recovers orphaned bulletins; peer states expire; claims arbitrate or expire (`CLAIM_GRACE_TICKS = 4` / `CLAIM_SILENT_TICKS = 24`); yields retry every 2 ticks with an 8-tick id-order tie-break | throughput drops; completion still happens | T6 |
| F7 | Host crashes or is stopped | S1 | agents see no `OBS` for `HEARTBEAT_MISS_TICKS = 16` | agents go `DEGRADED`: they keep their state and their decision stack alive but do **not** move on an unverifiable world; the frozen world's last positions are all they know | restart the host; agents adopt the new `OBS`; a resumed host re-announces the map | T10 |
| F8 | Clock/tick skew between agents and host | S3 | `abs(agent_tick - obs_tick)` in `HEARTBEAT` | ticks are logical and adopted monotonically from `OBS`; wall-clock is used for statistics only, never for ordering | bounded by the LAN round trip | T3 |
| F9 | Duplicate `robot_id` (stale process on a laptop) | S2 | host sees two source addresses claiming one id | the later claim is refused while the first address is still talking (pinned-address wins, counted as `duplicate_claims`); the id only moves after `LAN_REBIND_TICKS = 16` ticks of total silence (`rebinds`), so a stray packet cannot take over a robot mid-task. The design note's explicit `ID_TAKEN` reply is not sent — the impostor is dropped and counted instead (§5) | kill the stale process | T12 |
| F10 | Malformed / truncated / spoofed datagram | S3 | decode exception counter | dropped, counted, logged; never raises out of the socket loop | — | T3 |
| F11 | Two robots claim the same cell in the same tick | S2 | `Warehouse.apply_moves` collision counter (`world.py:116-152`) | physics keeps both in place and counts it; each is told via `ACT_RESULT.collision = true`, and the robots' `note_result` path feeds the next decision | robots re-plan on their own; nothing central intervenes | T1, T4 |
| F12 | Two robots deadlock head-on | S2 | UBPA conflict detection (`robot.py:878`) | `YIELD_QUERY`/`ACK`/`NACK` exchange; `STALEMATE_TICKS = 8` id-order tie-break is the documented safety net | one yields into a turnout; if it truly cannot, stall hand-back (`STALL_HANDOFF_EPISODES = 4`) returns the task | T1, T6 |
| F13 | Task bulletin reaches nobody | S2 | host sees a task no agent ever acknowledged (`known_tasks` gap reported in `HEARTBEAT`) | WMS repeats the bulletin every 20 ticks | first successful delivery clears it | T6 |
| F14 | Battery drains with no reachable charger | S3 | agent phase `CHARGING` with zero movement; `unreachable_ticks` | it waits; charger cell comes from `OBS`/`HELLO_WORLD` and is never reserved by the host | operator moves the block, or the robot releases its task (existing path) | T4 |
| F15 | Agent thermal/CPU throttling | S3 | `HEARTBEAT.step_ms_p95 > 60 ms`, `vcgencmd get_throttled` ≠ 0 | decisions slow down; late `ACT`s are discarded and the robot holds instead of acting on a stale plan | cooling/PSU fix; no code change | T11 |
| F16 | Wi-Fi partition: Pis see the host but not each other | S2 | peers expire (`PEER_STALE_TICKS = 6`) while `OBS` keeps flowing | robots run on local information only: fewer successful yields, more stall hand-backs, longer makespan — measured, not hidden<br>**Never** mitigated by routing peer traffic through the host | reconvergence when the link returns | T6 |
| F17 | Wi-Fi partition: Pis see each other but not the host | S1 | no `OBS` | `DEGRADED`: no movement authority, so robots freeze safely rather than walking blind | host returns | T10 |
| F18 | Host applies a robot's intention for the wrong robot | — | impossible by construction: `ACT_RESULT.position` comes from `world.robots[rid]` after `apply_moves` | — | covered by T4 (host cannot move a robot with no `ACT` on file) | T4 |
| F19 | Config drift: Pi runs a different `--policy` or map than the host expects | S2 | `HELLO.policy` versus host expectation; `world_digest` mismatch | host logs a mismatch; the robot still decides for itself, so the fleet runs mixed-policy (UBPA alongside stop-and-wait) which is a legitimate, explainable configuration | redeploy from the manifest | T10 |
| F20 | Runaway message rate (a bug or a spoofed flood) | S2 | per-source rate counter | agent applies a per-source cap (e.g. 200 msg/s) and drops the excess from that source; the decision loop is never blocked | source removed | T3 |
| F21 | A second agent starts on an address a running one already holds | S2 | `RadioPortInUse` at startup (the bind is exclusive, no `SO_REUSEADDR`) | the newcomer refuses to start and exits **2** with the listen address and the hint that each robot needs its own `--listen` port. It does **not** silently share the socket: the two processes would have split each other's datagrams and the host would have seen one robot flapping | start it on its own port, or stop the stray | T12 |
| F22 | Nothing ever adopts the agent (`--host` points at nothing, or the host is not up yet) | S2 | no `HELLO_WORLD` within `--adopt-timeout` (default 60 s) | the agent exits **3** with `no HELLO_WORLD from <host> in Ns`, so it cannot sit there holding a port and confusing the next run. `--adopt-timeout 0` waits indefinitely for the boot-order case where the robot really does come up first. Note this window is the only one an unadopted agent has: `--host-timeout` (15 s of silence after adoption) cannot cover it, because the host never knew the robot existed | start the host, or raise the timeout; under `Restart=always` systemd retries the node | T12 |

---

## 2. Degradation ladder

The fleet is expected to walk down this ladder and back up, without ever climbing toward a central
coordinator:

| Level | Condition | Fleet behaviour | Expected measurable effect |
| --- | --- | --- | --- |
| L0 | all links healthy | full negotiation, direct peer messaging | matches the parity run |
| L1 | some peer messages lost (< 40%) | occasional re-plan, rare re-auction | completion tick rises a few percent |
| L2 | heavy peer loss (≥ 70%) | most negotiation fails; stall hand-backs carry the run | completion tick rises sharply, tasks still finish |
| L3 | agent(s) dead | surviving fleet avoids frozen cells, re-auctions their tasks | makespan rises, no collisions added |
| L4 | host silent | agents hold and log `DEGRADED` | no movement, no corruption, no guessing |
| L5 | host restarted | agents re-`HELLO`, adopt the new world view, resume | run continues from the current world state |

The important property is monotonic behaviour: **information loss only ever costs performance**, never
correctness of the world (positions are always what `apply_moves` says) and never authority (the host
still cannot pick a winner at any level).

---

## 3. Detection surfaces

| Surface | Where | Fields |
| --- | --- | --- |
| Host link telemetry | host snapshot `link` | `sent`, `recv`, `lost_pct`, `late_acts`, `malformed`, `dup`, `mean_rtt_ms` (T11) |
| LAN admission telemetry | host snapshot `link` | `admissions`, `duplicate_claims`, `rebinds`, `foreign`, `unaddressed`, `roster` — who joined, who tried to steal an id, who legitimately moved (T12) |
| Per-agent telemetry | host snapshot `agents` | `last_seen_tick`, `tick_lag`, `step_ms_p95`, `policy`, `peer_hello_ok` |
| Fleet telemetry | existing dashboard | tick, tasks done/total, collisions, deadlocks, stalls, `mean_comm_latency_ticks` — unchanged fields, so the UI needs no edit |
| Negotiation audit | existing event log + message log | `YIELD_QUERY`/`ACK`/`NACK` with both UBPA scores, `CLAIM`/`RELEASE` with reasons |

Because the dashboard renders its existing fields, the same browser view works for the edge run: the
`server` block reports the host, `robots` reports what the agents broadcast, and nothing in the UI had to
learn a new concept.

---

## 4. What is explicitly *not* recovered automatically

Honesty items, so nobody is surprised on stage:

| Situation | Why it is not auto-recovered | Presentation |
| --- | --- | --- |
| A robot physically stuck against a non-blocked-but-permanently-occupied route (e.g. the only corridor held by a dead robot) | recovering would need an authority to move another robot's state — that is a decision the fleet does not delegate | the stall hand-back returns the task to the pool; the aisle stays blocked until an operator clears it |
| Host crash with no restart | there is no leader election and no state replication; the host is the world | the demo falls back to the offline in-process simulation |
| Full battery cycle in the shipped benchmark | the benchmark's route length (E1 ≈ 151 moves) never crosses `CHARGE_THRESHOLD = 20` | documented as unexercised-by-benchmark (finding A2); demonstrate with the existing `drain` operator action, in a separate run |
| Wire-level authenticity | no key management on a demo LAN | doc 03 §7 lists the risk, doc 04 §4 designs the optional mitigation |
---

## 5. Implementation status of the mechanisms above

This table was written before the package was built. The failure table in §1 describes the *design*; this
section says which parts of it the shipped `simulation/edge/` code actually implements, so nothing in the
demo is claimed on the strength of a design note. Nothing here weakens the decision argument - the parts
that are missing are all measurement or hardening features, never a decision.

| Mechanism (from §1/§3) | Shipped? | Where / what instead |
| --- | --- | --- |
| Link counters: `sent`, `recv`, `lost_pct`, `late_acts`, `malformed` | **yes** | `EdgeHost.link()` and the `link` block in `snapshot()`; asserted by `T11` |
| Per-agent liveness: `last_seen_tick`, `tick_lag`, state | **yes** | `EdgeHost.agent_state(rid)` → `boot`/`alive`/`dead`; the `agents` block in `snapshot()`; asserted by `T5`/`T11` |
| Decision cost telemetry: `step_ms_p95` | **yes** | per-agent p95 from the agent's own step samples; in the `agents` block, and quoted in `HEARTBEAT` |
| Host-originated traffic is CTRL + bulletin only (F4, F18) | **yes** | `EdgeHost` builds only `OBS`/`ACT_RESULT`/`HELLO_WORLD`/`SHUTDOWN`; the WMS bulletin is the only broadcast. `T8.3` instruments the run and rejects any peer decision type |
| Host refuses to relay peer traffic (F8) | **yes** | `_handle_ctrl` counts a `PEER_STATE` frame as `peer_leaked` and drops it; asserted by `T3` |
| `TASK_ANNOUNCE` reannounce for a robot that never heard a bulletin (F13) | **yes** | host `_reannounce_unheard` mirrors `engine.py` and uses each robot's `known_tasks`; over UDP the same signal rides `HEARTBEAT.known_tasks` |
| Stall detection and hand-back (F10-F12) | **yes** | unchanged `Robot` logic (`STALL_WINDOW`, `stall_handoff`) plus the host's raw-velocity progress watch |
| Per-agent host credentials / duplicate-id guard (F9) | **host side yes, without the `ID_TAKEN` reply** | `EdgeHost._admit_remote` pins the source address that first claimed an id and refuses a second address while the first is still talking, counting it as `duplicate_claims`; the id only moves after `LAN_REBIND_TICKS = 2 × DEAD_TICKS = 16` ticks of total silence, counted as `rebinds` (a restarted process or a DHCP move). What the design note adds and the code does not is the explicit `ID_TAKEN` message back to the impostor — it is dropped silently and counted instead (doc 04 §1.1). Tested by T12 (a live id cannot be stolen; a rebind needs real silence) |
| Exclusive UDP bind on both ends (F21) | **yes** | `UdpRadio` does not set `SO_REUSEADDR`, so a second process on the same address raises `RadioPortInUse`; the agent exits **2** with the address and a hint instead of sharing the socket (doc 05 §4). On Linux the second bind is refused by the kernel; on Windows the same bind also fails, which is why the check is exercised by a real duplicate process in T12 |
| Unadopted-agent give-up (`--adopt-timeout`, F22) | **yes** | agent-side deadline: with no `HELLO_WORLD` the process exits **3** rather than holding its port for the whole tick budget (previously an implicit 500 s at the demo rate). `--adopt-timeout 0` waits forever for a late host. Asserted by T12 |
| `DEGRADED` state and `obs_missed` counter (F5, F7, F17) | **no** | replaced by the host-side `boot/alive/dead` liveness above (doc 03 §8). The safety property - a silent or hostless agent does not move - holds because movement requires an `ACT` the host accepts |
| `agent_pressure: high` flag (F15) | **no** | `step_ms_p95` is published as a number; a reader draws the conclusion. No derived flag exists in the snapshot (doc 04 §1.6) |
| Per-source rate cap (~200 msg/s, F20) | **no** | malformed/oversized/flood input is counted and dropped rather than rate-limited; `T3` shows 200 junk datagrams cannot stop a tick |
| Source-address filtering on PEER traffic (doc 03 §7) | **agent-side only** | `UdpRadio` drops control frames from anything but the configured host; peer allow-listing is not enforced |
| Optional HMAC `sig` (doc 04 §4) | **implemented, off by default** | `protocol.sign`/`verify` exist; disabled because the demo LAN is closed |

Read this as: the *coordination* claims are fully backed by code and tests; the *hardening* claims marked
**no** are design, and the pack now says so rather than implying otherwise. Of the two a judge was most likely
to poke at, the duplicate-id guard is now implemented on the host side (address pinning + `rebinds`, with the
`ID_TAKEN` reply still absent); the per-source rate cap is still design — input is counted and dropped rather
than throttled, which T3 shows is enough to keep a tick alive.
