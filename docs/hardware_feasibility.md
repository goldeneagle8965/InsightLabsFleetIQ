# Hardware feasibility — from simulated fleet to three real robots

Problem statement **SIH26123 — Edge-AI Distributed Fleet Coordination**.
This document answers the question a domain judge will ask after seeing the demo:
*"the coordination works in software — does it survive real robots, a real radio and a real floor?"*

It is written as an engineering feasibility argument, not marketing. Where a number is an
assumption it is labelled **assumption**; where it comes from a datasheet or standard it is
cited in [Sources](#sources). Nothing here was measured on physical hardware — the fleet has
not been built. Everything marked *measured in the sim* comes from this repository's own runs
(`results/metrics.json`, `results/probe_26123_scale_delay.json`, re-runnable with
`python scripts/run_experiments.py`).

---

## 1. What the prototype already proves, and what hardware must still prove

| Already demonstrated in the prototype | Still to be proven on hardware |
|---|---|
| Peer-to-peer task bidding with no dispatcher; every award derivable from the radio log | That a real 802.11 link delivers peer state often enough for the same decisions |
| A configurable, lossy, delayed peer channel (`drop_prob`, `delay` ticks) and recovery when it degrades | Real per-robot link latency and loss on a warehouse floor with metal racking |
| Right-of-way arbitration at narrow junctions using only local sensing + peer state | That a physical turnout bay has the geometry the protocol assumes |
| Opportunity charging with a queue at one charger | Dock alignment repeatability and charge power at real battery temperatures |
| Age-corrected peer prediction surviving a 3-tick stale link with **0 collisions** (measured in the sim, on the default 3-robot/6-task scenario only) | That the same result holds with a real, jittery link |
| Per-robot decision cost of **0.11 ms/step** (measured in the sim, dev laptop) | That an embedded computer keeps the decision loop inside the cell-time budget |

The honest summary: the prototype proves the **protocol and the recovery mechanisms**. Hardware
feasibility rests on three budgets — **time**, **compute**, **link** — argued below.

---

## 2. The two clocks: how simulation ticks map onto real robot time

The simulation runs at `TICK_HZ = 8` on a 25 × 11 cell grid (`simulation/config.py`), and one
robot step moves exactly one cell. That gives two clocks that must not be confused:

| | Simulation | Real hardware (**assumption**: 1 cell ≈ 1 m) |
|---|---|---|
| Steps per second | 8 Hz wall-clock | 1–2 Hz (1 m cell at 1.0–2.0 m/s) |
| Cell traverse time | 125 ms | 500–1000 ms |
| Peer awareness rate | 8 Hz (one broadcast per tick) | 10 Hz (ETSI CAM maximum rate) |
| Peer messages per cell travelled | 1 | 5–10 |
| Link latency admitted | 1 tick = 125 ms default, up to 3 ticks = 375 ms | ≤ 100 ms required for cooperative awareness |
| Decision compute allowed | 125 ms | 125–1000 ms |

Two consequences worth stating to a judge:

1. **The sim is pessimistic about the radio, not optimistic.** The default 1-tick peer delay
   (125 ms) and the stress case the ablation uses (3 ticks, 375 ms) both sit *at or beyond* the
   ≤ 100 ms latency that ETSI requires of cooperative awareness messages, and beyond the 10–80 ms
   typical of ROS 2 over Wi-Fi in published multi-robot experiments. A compliant V2X or DDS link
   would hand the protocol a **fresher** picture than the demo does.
2. **The sim is optimistic about nothing in the coordination layer, but it does compress the
   energy timescale.** See §7 — this is deliberate and disclosed.

### The invariants that actually have to hold

Dimensionless, so they survive the clock change:

- **peer broadcasts per decision** — sim 1, hardware 5–10 → more margin.
- **staleness as a fraction of cell-time** — sim 1.0, hardware ≤ 0.2 → more margin.
- **decision compute as a fraction of the budget** — sim 0.11 ms / 125 ms = **0.09 %** → more margin.
- **bidders per auction** — sim 3–8, hardware 3–8 → identical by construction.

Everything the protocol depends on scales *in our favour* on real hardware. The risk is not the
coordination policy; it is the physical subsystem it sits on — perception, safety geometry, the
access point, and the dock.

---

## 3. Reference platforms (sourced specs)

Two commercially available AMR classes are used as the hardware envelope. Either can host the
protocol; the numbers bound the geometry, speed and energy model.

| Spec | OTTO 100 | MiR250 |
|---|---|---|
| Payload | 150 kg | 250 kg |
| Max speed | 2.0 m/s (docking 0.3 m/s) | 2.0 m/s |
| Footprint / mass | 740 × 550 × 308 mm, 107 kg | 580 × 800 × 300 mm, 94 kg |
| Battery | 35 Ah at 26.4 V (≈0.92 kWh) | 1.63 kWh (34.2 Ah at 47.7 V), Li-NMC |
| Runtime | 6 h (90% → 10%) | up to 13 h with max payload, 17.4 h no load |
| Charge | 10% → 90% in **18 min** (autonomous, 98 A supercharger) | 10% → 90% in **52 min** (35 A) |
| Charging policy | autonomous **opportunity charging** by default | opportunity charging, ratio up to 1:16 |
| Aisle / corridor | min aisle 1099 mm one-way, 1975 mm two-way | 1450 mm default, 1300 mm dynamic |
| Detection fitted | Hokuyo safety laser scanner + 3D camera + ToF rear | safety laser scanners |
| Accel limit (loaded) | — | 0.3 m/s²; 9.5 m × 2 m needed to reach top speed |

**What this tells us about the turnout abstraction.** The prototype's `turnouts` are passing bays
at junctions where `degree >= 3` (`simulation/maps.py`). On a real floor a turnout is not an
abstraction — it is either a physically wider bay or one-way traffic management. The datasheets
show why the protocol needs it: a 740 mm-wide robot needs **1099 mm** of aisle to pass in one
direction, but **1975 mm** for two-way traffic. On a 1100 mm aisle, two robots *cannot* pass
without a bay. That is exactly the case the decentralised right-of-way arbitration exists for, and
it is the strongest argument in the deck: the standard industrial answer (install a fleet manager)
is what the PS forbids.

### Bill of materials — 3-robot proof of concept

Prices are deliberately omitted: unit list prices for AMRs and safety scanners are quote-based and
change with configuration. The table lists what has a *sourced spec* and what must be quoted.

| Item | Qty | Basis / note |
|---|---|---|
| AMR chassis with traction + lift | 3 | Class of OTTO 100 / MiR250 (§3) |
| Safety laser scanner, PL d | 3 | e.g. SICK microScan3 (§4) |
| Embedded computer for the coordination node | 3 | NVIDIA Jetson Orin Nano 8 GB — 67 TOPS, 7–25 W (§5) |
| 2D or 3D perception sensor | 3 | included on both reference platforms; needed for localisation + obstacle detection |
| Autonomous charger (opportunity charging) | 1 | shared single-dock, matching the sim's single charger with a queue |
| Wi-Fi 6 access point, dedicated SSID/VLAN | 1 | see §6 — an isolated AP avoids the congestion failure mode |
| Optional: UWB anchors for dock-approach proving | 4 | fallback if Wi-Fi localisation is insufficient at the dock |
| Optional: fleet-status display | 1 | this dashboard, run with `scripts/run_web.py` |

**Scale-up to 8 robots** is the same list with 8 chassis/radios/computers and 2 chargers, but it is
*not* purely additive — see the scale caveat below. The protocol itself does not change: the
dashboard already drives 3–8 robots and the scale probe has already run 6, 8 and 10.

### Scale caveat (measured, disclosed honestly)

`results/probe_26123_scale_delay.json` (ad-hoc probe, not part of the headline experiments):

| Fleet / tasks | Complete tick | Tasks done | Collisions (mean) | Stalls | Mean step ms |
|---|---|---|---|---|---|
| 3 / 6 | 93 | 6/6 | **0.0** | 0.0 | 0.107 |
| 6 / 12 | 99 | 12/12 | **0.0** | 0.0 | 0.133 |
| 8 / 16 | 327 | 16/16 | **13.0** | 1.0 | 0.131 |
| 10 / 20 | 180 | 20/20 | **15.0** | 0.0 | 0.159 |

Every task always completes and no run deadlocks, but **collision counts rise sharply at 8 and 10
robots**. Reading: the 25 × 11 layout was designed for a 3-robot demo, and at 8 robots the fleet
saturates the fixed set of turnout bays. The hardware consequence is concrete and useful —
**scaling beyond ~6 robots on one floor requires more junction capacity (more/ larger turnout bays
or one-way aisle management), not more coordination logic.** That is a floor-design task (§3: a
740 mm robot needs 1099 mm one-way but 1975 mm two-way), and it is the honest answer to "is 8 robots
feasible?": the algorithm is, the *layout* must be re-drawn and re-measured first. Stage S4 in §9
is written accordingly.

---

## 4. Safety and compliance envelope

A fleet that coordinates but cannot stop is not deployable. The governing standard is
**ISO 3691-4**, the international safety standard for driverless industrial trucks, and the
numbers it imposes line up with the simulation's own behaviour:

| ISO 3691-4 requirement | Where the prototype already implies it |
|---|---|
| Personnel-detection system (safety laser scanner) and braking circuit rated **PL d** | — (hardware only; not modelled) |
| Speed ≤ **0.3 m/s** where personnel detection is muted, e.g. docking, with compensating measures | The sim's charger/depot approach is the analogue of the docking step; the reference platforms dock at exactly 0.3 m/s |
| Detection verified with a 200 mm dia × 600 mm vertical cylinder **and** a 70 mm dia × 400 mm horizontal cylinder (person lying on the floor) | Prototype occupancy grid is cell-based; a real field must clear both test pieces — a hardware acceptance test, not a software one |
| Protective field grows with speed; stop must be achievable within the field | The protocol's `SENSOR_RANGE = 2` cells and `YIELD_WAIT` are the software mirror of this |
| Overspeed monitoring → stop if rated speed is exceeded | Robot velocity is exposed per robot in the dashboard snapshot (`robots[].velocity`) |

**Sensor for the safety layer:** SICK microScan3 — 4–9 m protective field, 275° scan angle,
**response time ≥ 70 ms**, protective-field supplement 65 mm. The 70 ms matters: it is *less* than
one 125 ms simulation tick, i.e. the real safety loop is faster than the coordination loop. Safety
therefore never depends on the peer radio. That separation is worth saying out loud to a judge:
**if the radio goes silent, the robot still stops for people.**

---

## 5. Compute budget: the coordination layer is not the bottleneck

Measured in the sim (`mean_robot_step_ms`, dev laptop, `results/metrics.json` family):

| Scenario (measured in sim) | Mean step time |
|---|---|
| 3 robots / 6 tasks, baseline | **≈ 0.107 ms** |
| 6 robots / 12 tasks (scale probe) | **≈ 0.133 ms** |
| 8 robots / 16 tasks (scale probe) | **≈ 0.131 ms** |
| 10 robots / 20 tasks (scale probe) | **≈ 0.159 ms** |
| 3 robots, radio delay 3 ticks, age correction on | **≈ 0.119 ms** |

Decision cost grows slowly with fleet size (0.107 → 0.159 ms from 3 to 10 robots, i.e. a factor of
1.5 for a factor of 3.3 in robots): the auction is linear in bidders and nothing in it is quadratic.

Budget comparison per cell-time:

| Platform | Coordination compute allowed | Sim decision cost | Headroom |
|---|---|---|---|
| Laptop (demo) | 125 ms at 8 Hz | 0.11 ms | ~1100× |
| Jetson Orin Nano 8 GB (67 TOPS, 7–25 W) | 500–1000 ms at 1–2 Hz | 0.11 ms × a generous embedded penalty | still > 200× |

A bidding round is a handful of float comparisons over ≤ 7 peer state messages of a few dozen
bytes. That is not an AI workload — it is arithmetic. **Conclusion: the coordination algorithms
are feasible on a Jetson-class module at a small fraction of one core, and would even fit on a
microcontroller-class node if perception were moved off it.**

Where the compute *is* actually spent: perception and localisation. That is why the reference
platforms carry laser scanners and cameras, and why the Jetson (67 TOPS, 7–25 W) is chosen — it
has room for a CNN-based obstacle layer *alongside* the coordination node. The `Edge-AI` label in
the PS is satisfied by the local perception stack; the coordination layer is deliberately cheap so
that it never competes with safety for cycles.

---

## 6. The peer link: from `drop_prob` + `delay` to a real radio

The prototype models the peer channel in three knobs (`simulation/peer.py`, dashboard controls):
`drop_prob` (0–100 %), `delay` (ticks) and the resulting `mean_comm_latency_ticks`, with every
packet visible in the radio log including **"peer packets last 60 ticks: N · central scheduler
messages: 0"**.

The real-world equivalents, with published numbers:

| Prototype knob | Real equivalent | Published figure |
|---|---|---|
| Peer broadcast, direct robot-to-robot | ETSI Cooperative Awareness Message over ITS-G5 / 802.11p or C-V2X; or ROS 2 DDS multicast over Wi-Fi | CAM generation rate min interval **100 ms (10 Hz)**, max interval 1 s; assumed max latency **100 ms**; CAM construction ≤ 50 ms and transmit ≤ 50 ms |
| `delay = 1 tick` (125 ms) | Wi-Fi 5 GHz, ROS 2 | 5 nodes ≈ **10 ms**, 10 nodes **15 ± 20 ms** (published swarm measurement); CycloneDDS over Wi-Fi ≈ **51 ms** mean for a 1 kB array vs 1.29 ms on Ethernet |
| `delay = 3 ticks` (375 ms) | congested or roaming Wi-Fi, retransmission storms | the same study finds latency degrades sharply beyond ~10 nodes and "default DDS retransmission is inappropriate for high-frequency streams" |
| `drop_prob` | packet loss with retries | cooperative-awareness use cases are specified at **90–95 % reliability**, i.e. 5–10 % loss is designed for, not an anomaly |

**Design consequences for the pilot:**

1. Give the fleet a **dedicated SSID/VLAN on one AP**, 5 GHz/6 GHz, and keep peer payloads small —
   the published failure mode is congestion, not distance.
2. Keep the **age-correction** mechanism (`age_corrected_peers`, RC2) enabled. In the sim,
   disabling it at 3 ticks of delay produced **2–3 collisions per run**; with it enabled the same
   scenario produced **0 collisions** at 103 ticks vs 94. It costs ~9 % make-span and buys
   collision-freedom on a stale link. That is a real, measurable trade to show a judge.
3. **Never put safety on the radio.** See §4. The radio carries *work*, not *stopping*.
4. The sim's `TASK_REANNOUNCE_TICKS` and claim-expiry logic exist because a one-shot announce plus
   70 % loss can orphan a task permanently. Both must ship on hardware exactly as simulated.

---

## 7. Energy and charging: the one deliberately compressed timescale

This is the most important disclosure in this document.

| Quantity | Simulation | Real hardware (MiR250 / OTTO 100) | Compression |
|---|---|---|---|
| Drain per cell travelled | 0.18 % of pack (`DRAIN_MOVE`) | 151 m at ~125 W avg ≈ 5 Wh ≈ **0.3 %** of a 1.63 kWh pack | ~**85×** |
| Charge rate | 1.6 %/tick at 8 Hz → full in ~8 s | 10–90 % in 18 min (OTTO) / 52 min (MiR250) | ~**135×** |
| Charge window | start at 20 %, resume at 80 % (`CHARGE_THRESHOLD` / `CHARGE_RESUME`) | opportunistic charging, 10–90 % windows, ~85 % availability claimed for 24/7 operation | same policy, different units |

The simulation compresses the energy/charging timescale by roughly **two orders of magnitude** so
that a 12-second demo shows a robot driving, draining, queueing at the charger, charging and
returning to work. Without that compression the demo would show nothing at all.

**What transfers and what does not:**

- **Transfers:** the *policy* — a single shared charger, a queue under decentralised bidding,
  a threshold to leave work and charge, a resume level to return. Compare the sim's CHARGE_THRESHOLD
  20 % / CHARGE_RESUME 80 % to the datasheet's 10 % → 90 % opportunity-charging window: same
  strategy, 8-hour shift in reality instead of 8 seconds.
- **Does not transfer:** the wall-clock and the percentages-per-second. On hardware, the ratio is
  the datasheet's **1:16 charging ratio** — 10 minutes of charging buys 2 h 40 min of runtime at
  max payload. Any judge asking "will the fleet really spend that much time charging?" should be
  answered with that ratio, not with the demo's numbers.
- **Availability math for the pilot:** with opportunity charging, a fleet of *n* robots and *c*
  chargers needs `n·t_charge ≤ c·(shift + t_charge)`. With a 1:16 ratio and 18–52 min charges,
  a single dock supports a 3-robot fleet across a shift; the 8-robot pilot should budget **2 docks**
  and treat the second as availability margin, not throughput.

---

## 8. What breaks first, and what we do about it

| Rank | Failure mode on real hardware | Why (sourced) | Mitigation already in the prototype |
|---|---|---|---|
| 1 | Wi-Fi congestion as the fleet grows | published ROS 2 swarm results: fine at 5 nodes (~10 ms), usable at 10 (15 ± 20 ms), "unusable" beyond | small payloads, dedicated AP, configurable `drop_prob`/`delay` stress-tested in the UI, age correction on |
| 2 | Junction geometry vs turnout assumption | real aisles: 1099 mm one-way vs 1975 mm two-way vs ~800 mm robot width | turnout bays are explicit in every map; one-way management as fallback; arbitration is local, so bays can be added without changing policy |
| 3 | Dock alignment and charge power | reference platforms dock at 0.3 m/s with dedicated docking accuracy; charge current ramps down with battery temperature | single-charger queue is modelled; UWB anchors listed as an optional aid in the BOM |
| 4 | Localisation drift in feature-poor aisles | laser-SLAM in long empty corridors is the classic weak case; both reference platforms use laser SLAM + 3D cameras | the sim's occupancy grid is exact, so this is a **pure hardware risk** — mitigate with fiducials or UWB (in BOM) and treat as a bench milestone |
| 5 | Safety acceptance (PL d, test pieces) | ISO 3691-4 verification uses defined test pieces and speed-dependent protective fields | safety loop is independent of, and faster than, the coordination loop (70 ms scanner response vs 125 ms tick) |

---

## 9. Staged plan to a credible 3-robot demo

| Stage | Deliverable | Exit criterion |
|---|---|---|
| S0 (done) | Software prototype, lossy/delayed radio model, measured ablations | This repository; `results/metrics.json`, tests green |
| S1 | Bench: one chassis, safety scanner, Jetson node, one AP | Coordination node holds its decision loop on the real link with the rover on blocks; measured step latency logged next to the scanner's 70 ms response |
| S2 | Two robots, one narrow aisle, one turnout | Zero collisions in 20 consecutive contested-junction runs with the radio stressed to 3-tick-equivalent staleness |
| S3 | Three robots + single charger, 4-hour shift | ≥ 85 % robot availability with opportunity charging; make-span within ±25 % of the sim's prediction for the same layout |
| S4 | Eight robots, 2 docks | **Layout re-drawn first** (more junction capacity — see the scale caveat in §3), then: zero collisions under the modelled 90–95 % link reliability envelope and the 3-tick-equivalent staleness stress case |

**Judge-facing one-liner:** *the coordination layer costs 0.11 ms of compute per robot step and
needs one peer broadcast per decision; the real robot gives it 500–1000 ms and 5–10 broadcasts per
cell. The interesting engineering is not the AI — it is the turnout geometry, the access point and
the dock, and all three are modelled or costed above.*

---

## Sources

All URLs retrieved 19 September 2026.

**Reference platforms**

- OTTO 100 datasheet — 150 kg payload, 2.0 m/s, 740 × 550 × 308 mm, 107 kg, 35 Ah at 26.4 V,
  6 h runtime, 10–90 % in 18 min, 98 A supercharger, Hokuyo safety scanner, 1099 mm one-way /
  1975 mm two-way aisle: <https://www.hannovermesse.de/apollo/hannover_messe_2025/obs/Binary/A1433447/OTTO%20100%20DATA%20SHEET%20-%20EN-EU%20(MAR2025).pdf>
  and <https://www.guidanceautomation.com/wp-content/uploads/2023/02/OTTO-100-Data-Sheet.pdf>
- MiR250 specifications — 250 kg payload, 2.0 m/s, 1.63 kWh Li-NMC, up to 13 h with payload,
  10–90 % in 52 min at 35 A, 1450/1300 mm corridor, 0.3 m/s² accel:
  <https://ai.motion.com/wp-content/uploads/2025/10/MiR250-Specifications-2.99.pdf>
  and <https://www.stima.it/files/31126/mir250productpresentation.pdf>

**Safety**

- ISO 3691-4:2023 — driverless industrial trucks, scope and overspeed/speed-control requirements:
  <https://www.iso.org/standard/83545.html> and
  <https://blog.ansi.org/ansi/iso-3691-4-2023-driverless-industrial-trucks/>
- ISO 3691-4 explained — 0.3 m/s limit where personnel detection is muted, PL d for personnel
  detection and braking, 200 mm × 600 mm and 70 mm × 400 mm test pieces:
  <https://www.fabrico.io/sr/blog/iso-3691-4-driverless-industrial-trucks/>
- TÜV Rheinland AGV whitepaper — protective-field/speed tables and PL d requirement:
  <https://www.tuv.com/content-media-files/master-content/services/industrial-services/pdf/tuv-rheinland-automatic-guided-vehicles-whitepaper-en_neu.pdf>
- SICK microScan3 — 4–9 m protective field, 275°, response time ≥ 70 ms, 65 mm protective-field
  supplement: <https://www.motionworld.com/assets/SICK-microScan3Series-Datasheet.pdf> and
  <https://www.sick.com/media/pdf/6/46/946/dataSheet_MICS3-AAAZ55AZ1P01_1075843_en.pdf>

**Compute**

- NVIDIA Jetson Orin Nano — 67 TOPS (8 GB, MAXN_SUPER), 7–25 W, 6-core Cortex-A78AE:
  <https://www.macnica.co.jp/en/business/semiconductor/manufacturers/nvidia/products/141900/>

**Radios and latency**

- ETSI EN 302 637-2 — CAM generation interval ≥ 100 ms (10 Hz), ≤ 1 s:
  <https://www.etsi.org/deliver/etsi_en/302600_302699/30263702/01.03.01_30/en_30263702v010301v.pdf>
- ETSI TR 102 638 — cooperative awareness latency requirement < 100 ms, 10 Hz:
  <https://www.etsi.org/deliver/etsi_tr/102600_102699/102638/01.01.01_60/tr_102638v010101p.pdf>
- Swarm robot communications in ROS 2, experimental study (5 nodes ≈ 10 ms, 10 nodes 15 ± 20 ms,
  congestion beyond): <https://www.riuma.uma.es/xmlui/bitstream/handle/10630/38979/Swarm_Robot_Communications_in_ROS_2_An_Experimental_Study.pdf?sequence=1&isAllowed=y>
- Optimising ROS 2 communication for wireless robots — default DDS retransmission unsuitable for
  high-frequency streams: <https://arxiv.org/html/2508.11366v1>
- ROS 2 middleware latency by network — CycloneDDS Wi-Fi ≈ 51 ms vs Ethernet 1.29 ms:
  <https://arxiv.org/pdf/2309.07496>

**Background (named, not relied on for numbers):** market-based multi-robot task allocation and the
Consensus-Based Bundle Algorithm (CBBA) are the literature analogues of the prototype's
utility-based priority auction; they are cited here as context for judges who ask where the
mechanism comes from, not as a source of measurements.
