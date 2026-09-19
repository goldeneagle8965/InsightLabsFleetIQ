# 04 — Message schema

One JSON object per UDP datagram. Common header on **every** message:

| Field | Type | Req | Meaning |
| --- | --- | --- | --- |
| `v` | int | yes | protocol version (`1`) |
| `type` | str | yes | message type, see the tables below |
| `from_id` | str | yes | sender: `R1..Rn`, `WMS` (bulletin), `HOST` |
| `tick` | int | yes | sender's logical tick at send time |
| `to_id` | str | no | present ⇒ unicast destination (mirrors `peer.py:110`) |

Transport-added fields, stamped so the wire matches what `PeerBus` already delivers:

| Field | Where | Meaning |
| --- | --- | --- |
| `sent_tick` | sender transport | same value the bus stamps (`peer.py:97`) |
| `recv_tick` | receiver transport | tick at which the datagram was consumed (`peer.py:124`) |
| `sent_ms` | sender transport | wall-clock milliseconds at send, for RTT/latency stats only |

Field types follow the existing code exactly: coordinates are `[x, y]` arrays on the wire, converted to
tuples by `PeerState.from_msg` (`peer.py:48-66`) and by the `tuple(msg["pick"])` calls in `_note_task`.
The robot's handlers (`robot.py:_ingest`, `246-273`) accept these payloads **unchanged** — that is the
whole point of reusing the vocabulary.

---

## 1. CTRL messages (host ↔ agent)

### 1.1 `HELLO` — agent → host, once at boot and every 8 ticks until answered

```json
{"v":1,"type":"HELLO","from_id":"R2","tick":0,"agent_version":"edge-1","robot_id":"R2",
 "policy":"ubpa","listen_port":9202,"pid":4711}
```

| Field | Type | Notes |
| --- | --- | --- |
| `robot_id` | str | must match the id the host expects for this address |
| `policy` | str | `ubpa` \| `stop_wait` — the host does **not** choose it, it reports it |
| `listen_port` | int | where peers should send PEER traffic |

The host answers with `HELLO_WORLD`. The design also rejects a second live claim on the same `robot_id`
(`ID_TAKEN`, doc 06 F9); the shipped host has no such guard because it never accepts a remote agent in the
first place, so the duplicate-id case cannot yet arise (doc 06 §5 implementation status).

### 1.2 `HELLO_WORLD` (a.k.a. `WORLD`) — host → agent, once, plus the first `OBS`

Static geometry: everything the robot needs to plan but that never changes during a run.

```json
{"v":1,"type":"HELLO_WORLD","from_id":"HOST","tick":0,
 "world":{"width":25,"height":11,"walls":[[0,0],[1,0],[2,0]],
          "turnouts":[[3,5],[7,5]],"charger":[1,9],"sensor_range":2,"tick_hz":8,
          "part":1,"parts":1},
 "peers":{"R1":"192.168.50.11:9201","R2":"192.168.50.12:9202","R3":"192.168.50.13:9203"}}
```

| Field | Type | Maps to (robot.py) |
| --- | --- | --- |
| `world.width`, `world.height` | int | `self.world.width/height` (584-592, 835, 850-863, 1001-1011) |
| `world.walls` | `[[x,y]]` | `self.world.walls` (821, 1022) |
| `world.turnouts` | `[[x,y]]` | `self.world.turnouts` (999-1011) |
| `world.charger` | `[x,y]` | `self.world.charger` (213, 437, 441, 499) — repeated in every `OBS` so an env switch needs no re-handshake |
| `world.sensor_range` | int | default for `sense(pos, rng)` (410) |
| `peers` | dict | the agent's peer table (static config, host-announced) |

Chunking: if `width*height` ever makes the wall list exceed ~1100 B, split into `parts` chunks with the
same `part`/`parts` counters; the agent assembles then applies. Current maps (25×11) fit in one datagram.

### 1.3 `OBS` — host → agent, once per tick per agent

Everything the robot may look up during one `step()`. Deliberately contains **no other robot's
position**: peers are learned from peers, which is requirement B4.

```json
{"v":1,"type":"OBS","from_id":"HOST","tick":41,
 "nearby_blocked":[[4,5],[4,6]],"charger":[1,9],
 "active_blocks":6,"world_digest":"a1b2c3"}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `tick` | int | logical tick this observation is valid for; agent adopts it monotonically |
| `nearby_blocked` | `[[x,y]]` | cells with `Manhattan(cell, robot_pos) ≤ sensor_range` that are currently blocked — the network form of `Warehouse.sense` (`world.py:71-77`) |
| `charger` | `[x,y]` | charger cell (stable, but cheap to repeat) |
| `active_blocks` | int | total blocked cells, telemetry only |
| `world_digest` | str | short hash of `(width,height,walls,turnouts)`; a change triggers a fresh `HELLO_WORLD` (env switch) |

`RemoteWorldView.sense(pos, rng)` returns the subset of `nearby_blocked` within `rng` of `pos`; because
the host computes it from the same robot position the robot will use, the result equals
`Warehouse.sense` exactly (test `T2`).

### 1.4 `ACT` — agent → host, once per tick

The robot's own decision. The host applies it or rejects it; it never edits it.

```json
{"v":1,"type":"ACT","from_id":"R2","tick":41,"dest":[5,4],"phase":"TO_DROP",
 "task":"T3","step_ms":0.31}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `dest` | `[x,y]` | exactly what `Robot._choose_move` returned (`robot.py:1041`) — may equal the current cell (hold) |
| `phase`, `task` | str | telemetry for the spectator view (the robot's own phase/task, not a host assignment) |
| `step_ms` | float | local compute time for this decision, feeding `mean_robot_step_ms` |

### 1.5 `ACT_RESULT` — host → agent, once per tick per robot

Ground truth after physics. Consumed through `Robot.note_result` **before** the next `step()`.

```json
{"v":1,"type":"ACT_RESULT","from_id":"HOST","tick":41,"position":[5,4],
 "accepted":true,"blocked_by":"collision","collision":false,"late":false}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `position` | `[x,y]` | authoritative position after `apply_moves` |
| `accepted` | bool | `false` when the intention was not granted (out-of-bounds, occupied, collision, or late) |
| `blocked_by` | str | `reason` tag: `bounds` \| `wall` \| `blocked` \| `occupied` \| `collision` \| `late` \| null |
| `collision` | bool | whether this robot was part of a counted collision pair on this tick |
| `late` | bool | `ACT` arrived after the deadline and was not applied |

### 1.6 `HEARTBEAT` — agent → host, every 8 ticks

```json
{"v":1,"type":"HEARTBEAT","from_id":"R1","tick":20,"uptime_s":0.02,"step_ms_p95":0.36,
 "sent":243,"recv":243,"lost_est":0.0,"policy":"ubpa","phase":"TO_DROP","current_task":"T1",
 "battery":97.9,"world_epoch":1,"peer_hello_ok":false,
 "known_tasks":{"T1":"PICKED","T2":"CLAIMED","T3":"CLAIMED","T4":"OPEN","T5":"OPEN","T6":"OPEN"},
 "task_holders":{"T1":"R1","T2":"R3","T3":"R2"},
 "counters":{"tasks_done":0,"wait_ticks":10,"move_ticks":10,"distance":10.0,
             "deadlock_events":0,"stall_handoffs":0,"step_ms_sum":4.8,"step_n":20}}
```

| Field | Meaning |
| --- | --- |
| `tick`, `uptime_s` | the agent's own logical tick and wall-clock age |
| `sent`, `recv`, `lost_est` | counters the agent observed about itself; `lost_est` is the agent's estimate, not a host measurement (the host's own numbers are `link.lost_pct`) |
| `step_ms_p95` | decision cost. This is telemetry: the host records the number (doc 05 §6), it does not raise an `agent_pressure` flag. There is no such field in the shipped snapshot |
| `phase`, `current_task`, `battery`, `policy` | the robot's own state, read-only, for the spectator view |
| `known_tasks` | task id → the agent's own status for it. This is what makes doc 06 F13 (a robot that never heard a bulletin) detectable **without** the host tracking per-agent task state itself |
| `task_holders` | task id → the agent's belief about the holder, learned from peer traffic. Display and gap-detection only |
| `counters` | the agent's own tallies, plus the `step_ms_sum`/`step_n` pair behind `step_ms_p95` |
| `world_epoch`, `peer_hello_ok` | which map generation the agent is on, and whether it has heard any peer yet |

The heartbeat is the only thing the host learns about a remote agent. It carries no instruction in either
direction, and the host never writes a field back into a robot.

### 1.7 `SHUTDOWN` — host → agent

```json
{"v":1,"type":"SHUTDOWN","from_id":"HOST","tick":812,"reason":"run complete"}
```

The agent finishes the current tick, flushes its `ACT`, closes the socket. A clean stop, not a kill.

---

## 2. PEER messages (agent ↔ agent, direct)

Payload shapes are copied from the code that emits them, so no handler changes are required.

### 2.1 `PEER_STATE` — every tick, broadcast to all peers

| Field | Type | Source |
| --- | --- | --- |
| `robot_id` | str | `PeerState.robot_id` (`peer.py:29`) |
| `position` | `[x,y]` | `robot.py:228-244 peer_state()` |
| `velocity` | `[vx,vy]` | — |
| `current_task` | str \| null | — |
| `planned_path` | `[[x,y],...]` | truncated to `PATH_HORIZON = 12` cells |
| `battery` | float | — |
| `priority` | int | — |
| `timestamp` | int | sender's tick; **the staleness clock** (`robot.py:424`) |
| `status` | str | `IDLE`/`MOVING`/`PICKING`/… |
| `task_urgency` | float | auction input |
| `workload` | int | auction input |
| `ubpa_score` | float \| null | the sender's own UBPA score for its contested cell, if any |

```json
{"v":1,"type":"PEER_STATE","from_id":"R2","robot_id":"R2","tick":41,"timestamp":41,
 "position":[5,4],"velocity":[0,1],"current_task":"T3","planned_path":[[5,4],[5,5],[5,6]],
 "battery":87.4,"priority":1,"status":"MOVING","task_urgency":0.7,"workload":2,
 "ubpa_score":3.812}
```

### 2.2 `TASK_ANNOUNCE` — WMS bulletin, fan-out to all agents

```json
{"v":1,"type":"TASK_ANNOUNCE","from_id":"WMS","tick":4,
 "task_id":"T1","pick":[3,2],"drop":[1,8],"urgency":0.6}
```

Identical in shape to `engine.py:331-343`. **Bulletin, not assignment:** no `to_id`, no robot named.
Repeats every `TASK_REANNOUNCE_TICKS = 20` for any task at least one agent has never heard
(`engine.py:470` behaviour, host-side).

### 2.3 `BID` — agent → all peers (or to the auction opener)

```json
{"v":1,"type":"BID","from_id":"R3","tick":41,"task_id":"T1","pick":[3,2],"drop":[1,8],
 "urgency":0.6,"cost":18.44,"valid":true}
```

`valid = false` means the bidder cannot finish the round trip on its battery (`robot.py:574 _bid_cost`).
The host never receives this message; there is no tally anywhere except in each robot's own `auctions`
dict (`robot.py:286 _note_bid`).

### 2.4 `CLAIM` — agent → all peers (`robot.py:641 _send_claim`)

```json
{"v":1,"type":"CLAIM","from_id":"R1","tick":43,"task_id":"T1","pick":[3,2],"drop":[1,8],
 "urgency":0.6,"cost":17.9,"reassert":false}
```

Completion variant (`robot.py:466-481`):

```json
{"v":1,"type":"CLAIM","from_id":"R1","tick":61,"task_id":"T1","pick":[3,2],"drop":[1,8],
 "urgency":0.6,"done":true}
```

`reassert: true` marks a repeat during a contested race (`CLAIM_REASSERT_TICKS = 8`).

### 2.5 `RELEASE` — agent → all peers (`robot.py:664 _release`)

```json
{"v":1,"type":"RELEASE","from_id":"R2","tick":77,"task_id":"T1","pick":[3,2],"drop":[1,8],
 "urgency":0.6,"reason":"stall_handoff"}
```

`reason` is free text from the robot (`unreachable`, `stall_handoff`, `yield`, …) and is what the
spectator view shows a judge asking "why did that robot give the task back?".

### 2.6 `OBSTACLE` — agent → all peers (`robot.py:409-419`)

```json
{"v":1,"type":"OBSTACLE","from_id":"R2","tick":35,"cell":[6,5]}
```

Emitted once per newly sensed cell. This is how one robot's locally sensed blockage becomes fleet
knowledge without any central map service.

### 2.7 `YIELD_QUERY` — agent → one peer (`robot.py:948-957`)

```json
{"v":1,"type":"YIELD_QUERY","from_id":"R1","to_id":"R2","tick":50,
 "score":4.12,"cell":[6,5],"their_arrival":3}
```

| Field | Meaning |
| --- | --- |
| `score` | sender's UBPA score for the contested cell |
| `cell` | the contested cell |
| `their_arrival` | how many steps ahead the sender believes the peer will arrive (LOOKAHEAD-based) |

### 2.8 `YIELD_ACK` / `YIELD_NACK` — peer → querying agent (`robot.py:390/398`)

```json
{"v":1,"type":"YIELD_NACK","from_id":"R2","to_id":"R1","tick":50,"score":5.41,"cell":[6,5]}
```

`ACK` = "I yield" (sender keeps the aisle), `NACK` = "I keep it" (sender yields). Both carry the
responder's own `score`, which is what makes the exchange auditable: a judge can recompute the winner
from the two numbers in the log, with no hidden tie-break.

---

## 3. Encoding and validation rules

1. `json.dumps(msg, separators=(",", ":"), sort_keys=False)` — compact, ~30% smaller than defaults.
2. Decode strictly: non-object JSON, missing `type`, or a `v` other than `1` ⇒ count `malformed`, drop.
3. Numeric coercion mirrors the code (`int`, `float` casts in `from_msg`) so a hostile payload cannot
   inject a string where the robot does arithmetic.
4. `planned_path` is truncated sender-side to `PATH_HORIZON`; the receiver does not trust length.
5. Coordinates are validated against `0 ≤ x < width`, `0 ≤ y < height` before use in `OBSTACLE`
   handling, so a spoofed cell cannot poison `known_blocked` with an out-of-map value.
6. Payloads are never executed, `eval`-ed or used as filenames. No path or format-string interpolation
   from the wire into anything but dictionary lookups.

---

## 4. Optional integrity field (designed, disabled by default)

```json
{"v":1,"type":"ACT","from_id":"R2","tick":41,"dest":[5,4],
 "sig":"b7c1e0…"}            // hex HMAC-SHA256 over the canonical JSON minus "sig", shared key
```

Documented so the gap is explicit: with `--shared-key`, agents sign CTRL+PEER messages and receivers
drop unsigned/invalid ones (stdlib `hmac` + `hashlib`, no new dependency). Off by default because the
demo network is closed and a shared key adds a provisioning step; the source-address allow-lists in doc
03 §7 are the default protection. Test `T8` asserts the *decision* invariant either way — integrity does
not change who decides.

---

## 5. Size reference

| Type | Typical bytes | Frequency |
| --- | --- | --- |
| `PEER_STATE` | 300-340 | 8 Hz per agent |
| `OBS` | 140-220 (map excluded after hello) | 8 Hz per agent |
| `HELLO_WORLD` | 600-900 (map) | once per run |
| `ACT` / `ACT_RESULT` | 60-90 | 8 Hz per agent |
| `BID` / `CLAIM` / `RELEASE` | 110-160 | bursty (auction opens, claims, hand-backs) |
| `YIELD_*` | 90-120 | bursty (conflicts only) |
| `HEARTBEAT` | 110 | 1 Hz per agent |

All ≤ 1200 B, so no datagram ever depends on IP fragmentation - the failure mode that turns a
3-robot demo into a random 3-robot demo on a busy Wi-Fi channel.
