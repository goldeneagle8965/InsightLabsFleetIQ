# 03 — Proposed network protocol

Transport: **UDP + JSON (UTF-8), one message per datagram**. Rationale: the existing message vocabulary
is already small JSON dicts (`peer.py:16-24`), the robot tolerates loss and staleness by construction,
and UDP is what makes loss/jitter *real* instead of a `drop_prob` knob. No TCP: a retransmitting link
would hide exactly the failures the demo exists to show, and head-of-line blocking on one socket would
couple robots that are supposed to be independent.

---

## 1. Roles and channels

Three logical channels share the medium; they never share decision authority.

| Channel | Direction | Who speaks | What |
| --- | --- | --- | --- |
| **CTRL** (host ↔ agent) | bidirectional | host, agent | `HELLO`, `HELLO_WORLD`, `OBS`, `ACT`, `ACT_RESULT`, `HEARTBEAT`, `SHUTDOWN` |
| **PEER** (agent ↔ agent) | mesh, direct unicast | agents | `PEER_STATE`, `TASK_ANNOUNCE`, `BID`, `CLAIM`, `RELEASE`, `OBSTACLE`, `YIELD_QUERY`, `YIELD_ACK`, `YIELD_NACK` |
| **BULLETIN** (WMS → all) | one-way fan-out | host only | `TASK_ANNOUNCE` with `from_id = "WMS"` |

The WMS bulletin rides the PEER port numbers but is *not* peer traffic: it is the same bulletin the WMS
already broadcasts today (`engine.py:331 _broadcast_task`), and it names a task without addressing any
robot. The host never sends `BID`, `CLAIM` or `YIELD_*`.

---

## 2. Ports and addressing

| Node | Address | Port | Socket use |
| --- | --- | --- | --- |
| Host | `192.168.50.10` | `9100/udp` | receives `ACT`, `HELLO`, `HEARTBEAT`; sends `OBS`, `ACT_RESULT`, `TASK_ANNOUNCE`, `SHUTDOWN` |
| Host (broadcast) | `255.255.255.255:9101` or multicast `239.26.123.10:9101` | `9101/udp` | optional agent discovery announcements |
| Agent *i* | `192.168.50.1(0+i)` | `9200+i` | receives PEER + BULLETIN + `OBS`/`ACT_RESULT`; sends `ACT`/`HEARTBEAT` to host, PEER to peers |

Bundle the two directions into **one socket per node** where possible:

- **Host:** one socket bound to `0.0.0.0:9100` sends and receives everything host-side (replies go to the
  source address of the agent's last message, so agents need no fixed source port).
- **Agent:** one socket bound to `0.0.0.0:9200+i` receives everything; it sends `OBS`-derived acks and
  PEER traffic from that same port so peers can reply `YIELD_ACK`/`YIELD_NACK` back to it.

Peer table is **static configuration** (`--peers R1=192.168.50.11:9201,...`), with an optional
discovery fallback: the host announces the roster in `HELLO_WORLD`, and a joining agent may also send a
`HELLO` probe to the broadcast address. Static-by-default keeps the demo deterministic and avoids a
"who is in the fleet" question that only a directory service could answer (a directory is fine — it
would still hold no decision authority — but it is unnecessary for three robots).

---

## 3. Wire format

```
J = UTF-8 JSON object, no trailing bytes, one datagram per message

{
  "v": 1,                 # protocol version, integer, required
  "type": "PEER_STATE",   # message type, required
  "from_id": "R2",        # sender identity, required (host uses "HOST"; bulletins use "WMS")
  "tick": 41,             # logical tick at send time, required for all control+peer traffic
  ...type-specific fields...
}
```

Rules that keep the robot code unmodified:

1. `from_id` is injected by the sender, mirroring `peer.py:96 packet.setdefault("from_id", sender)`.
2. `sent_tick` / `recv_tick` mirror what `PeerBus` stamps (`peer.py:97`, `124-125`) so latency
   statistics and any future age-based logic behave the same on the wire.
3. `to_id`, when present, means unicast and is honoured by the transport (`peer.py:110-112` semantics).
4. Unknown fields are ignored, unknown `type` values are logged and dropped — forward compatible.
5. Coordinates are 2-element arrays `[x, y]` on the wire and converted to tuples on receipt
   (`PeerState.from_msg` already does this).
6. Maximum datagram: **1200 bytes**. The world map is chunked (`WORLD` part 1..k) rather than sent in one
   oversized datagram, so nothing relies on IP fragmentation.

---

## 4. Timing semantics

| Concept | Definition |
| --- | --- |
| Tick | logical time unit, `1/8 s` nominal (`TICK_HZ = 8`), owned by the world because physics needs one simultaneity authority |
| `OBS(t)` | the state of the world as seen by one agent, valid for tick `t` |
| `ACT(t, dest)` | the intention of that agent for tick `t`, sent as soon as its `step(t)` returns |
| Deadline | the host collects `ACT(t)` until its own tick-`t` cut-off, then applies. Late arrivals are discarded and answered with the real outcome |
| `ACT_RESULT(t, pos, accepted)` | authoritative post-move position for tick `t`; `accepted=false` means the move was blocked by physics/collision and the robot stayed |
| Hold-by-default | a robot with no `ACT` on file for tick `t` contributes `dest = current position` |
| Clock adoption | agent sets `self.tick = max(self.tick, obs.tick)`; it never rewinds, so `p.timestamp` comparisons stay monotonic across loss |

**Why the host declares the tick but is not a coordinator.** `apply_moves` needs an atomic
"simultaneous" moment; the tick supplies it. The host never uses the tick to give one robot a head
start, never reorders intentions (they are applied in sorted-id order), and never decides who wins a
conflict — conflicts are resolved by the robots' own `YIELD_*` exchange before the tick is applied.

**Free-running in normal mode, lock-step in tests.** Normal operation lets each agent keep its own
cadence, so a slow node produces late `ACT`s and observable degradation. `lockstep=True` exists only to
make the parity test (`T1`) an equality assertion.

---

## 5. Sequence of a normal tick (edge-lan / edge-pi)

```
Host tick 40 ends, applies moves
  │
  ├─► OBS(41) ─────────────► R1, R2, R3          (map diff, nearby_blocked, charger, tick=41)
  │
 agents step 41 independently:
  │      R1: robot.step(41) ──► intends (5,4)
  │           peer broadcasts: PEER_STATE(41), maybe BID/CLAIM/YIELD_QUERY
  ├◄─ ACT(41,(5,4)) ────────── R1
  ├◄─ ACT(41,(1,7)) ────────── R2
  ├◄─ ACT(41,(3,2)) ────────── R3
  │
  host: apply_moves({R1:(5,4),R2:(1,7),R3:(3,2)}, 41)   ← physics, collisions counted here
  │
  ├─► ACT_RESULT(41,(5,4),accepted=true) ─► R1     (R2 blocked → accepted=false)
  ├─► ACT_RESULT(41,pos,accepted=false)  ─► R2
  │
  each agent: note_result(pos) before its next step(42)
```

Aggregated over a run, this is the same loop the simulation already performs
(`engine.py:136-146`), with the middle split across the network.

---

## 6. Reliability and loss model

UDP loses, duplicates and reorders. The robot already survives all three, so the protocol keeps the
robot's assumptions instead of adding a reliability layer:

| Hazard | Handling | Where |
| --- | --- | --- |
| Peer state lost | state expires after `PEER_STALE_TICKS = 6`; the next broadcast replaces it | `robot.py:420-427` |
| Task bulletin lost for every robot | WMS repeats any task at least one robot has never heard, every `TASK_REANNOUNCE_TICKS = 20` | `engine.py:470` semantics reused by the host |
| Bid / claim lost | auction re-opens for that task; claims expire (`CLAIM_SILENT_TICKS = 24`) or are contradicted (`CLAIM_GRACE_TICKS = 4`) | `robot.py:701-750` |
| Yield handshake lost | exchange retries every 2 ticks; `STALEMATE_TICKS = 8` id-order tie-break ends it | `robot.py:908-978` |
| `ACT` lost / late | hold-by-default; the robot learns the truth from `ACT_RESULT` | host + `note_result` |
| `OBS` lost | agent keeps its last view and its own tick; it may step with slightly stale geometry, which is a realistic sensor model | agent |
| Duplicated message | idempotent handlers (`_note_task` overwrites the same task, `_note_bid` overwrites the same bidder's cost, claims re-assert harmlessly) | `robot.py:275-300` |
| Reordered message | `timestamp`/`tick` ordering and "newest wins" updates | `robot.py:255` |

Deliberate omission: **no sequence numbers, acks or retransmits for peer traffic.** Adding them would
turn lossy UDP into something like TCP and destroy the phenomenon being demonstrated. The host
nevertheless keeps counters (`sent`, `recv`, `lost`, `dup`, `mean_rtt_ms`) so degradation can be quoted
with numbers rather than adjectives.

---

## 7. Trust and safety boundary

The demo LAN is trusted-by-assumption, and this is stated rather than implied:

| Concern | Status | Mitigation available |
| --- | --- | --- |
| Anyone on the LAN can forge `ACT` for another robot | **open** | host accepts `ACT` only from the configured address of that robot id |
| Anyone can forge `from_id` on PEER traffic | **open** | optional `sig` field (document 04 §4) — designed, off by default because it is not needed for a closed demo network |
| Anyone can send `OBS`/`ACT_RESULT` to an agent | **open** | agents accept control traffic only from the configured host address |
| Rogue agent floods the medium | **open** | rate limit per source address in the agent socket loop |
| Passive eavesdropping | **open** | not defended; the payloads are warehouse coordinates |
| DoS by malformed JSON | handled | decode errors are caught, counted and dropped; no exception escapes the socket loop |

Source-address filtering costs three lines and removes the most embarrassing demo failure (a stale
process on a laptop stealing robot ids). Cryptographic authentication would be more correct but adds a
dependency to an otherwise stdlib-only agent, so it is documented as an explicit, testable gap instead
of being quietly skipped.

---

## 8. Protocol states

**Agent:** `BOOT → HELLO → (HELLO_WORLD) → RUNNING → SHUTDOWN`

`DEGRADED` was the design's name for "host silent for > `HEARTBEAT_MISS_TICKS` (16 ticks = 2 s)". It is
**not a label the shipped agent carries**: there is no `DEGRADED` state and no `obs_missed` counter (doc 06
§5). The guarantee it stood for is implemented from the host side instead - `EdgeHost.agent_state(rid)`
reports `boot` → `alive` → `dead` from the age of the last `ACT`, a silent agent is simply not stepped, and
an agent that hears no `OBS` has no movement authority, so it cannot walk blind. Same behaviour, different
vocabulary: the state-machine diagram is design, the `boot/alive/dead` triple is code.

**Host:** `CONFIGURING → RUNNING → (DRAINING) → STOPPED`. `DRAINING` waits for in-flight `ACT`s and
flushes final `ACT_RESULT`s so a stopped run still reports consistent metrics. The shipped host implements
`RUNNING` and a best-effort `STOPPED` (`EdgeHost.stop()` sends `SHUTDOWN`); an explicit `DRAINING` phase is
design - `step()` already applies only the `ACT`s that arrived by the deadline.

---

## 9. Bandwidth budget (3 agents)

| Message | Approx. size | Rate | Bytes/s |
| --- | --- | --- | --- |
| `PEER_STATE` (full path truncated to `PATH_HORIZON = 12` cells) | ~320 B | 1/tick/agent × 2 peers | ~5.1 kB/s |
| `OBS` | ~200 B (map only on first message) | 1/tick/agent | ~4.8 kB/s |
| `ACT` | ~60 B | 1/tick/agent | ~1.4 kB/s |
| `ACT_RESULT` | ~70 B | 1/tick/agent | ~1.7 kB/s |
| bids/claims/yields (bursty) | ~120 B | « 1/tick/agent | < 1 kB/s |
| **Total** | | | **≈ 15 kB/s for the whole fleet at 8 Hz** |

Three orders of magnitude below a 100 Mbit switch port; comfortable on Wi-Fi, and the reason a Pi Zero
would also work as an agent node.
