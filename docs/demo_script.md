# 3-minute demo script

**Setup:** `python scripts/run_demo.py` then open the printed URL.

0:00 — “Three warehouse AMRs. There is no central dispatcher. Each robot broadcasts pose, path, battery, task.”

0:20 — Point at the radio feed. “Robot A knows Robot B directly from this packet, not from a scheduler.”

0:40 — Tasks appear. “Auction: cost is distance + battery penalty + congestion + workload. Lowest valid bid wins. That is why R2 took task T3.”

1:10 — Head-on in the narrow aisle. “Conflict on planned paths. UBPA scores. The lower score yields into the turnout. Both continue. No one called a central lock.”

1:50 — Press **BLOCK AISLE** (or click a cell in front of a robot). “Local sensor range 2. Obstacle broadcast. Replan. If the pick is unreachable, the holder RELEASEs and peers re-auction.”

2:10 — Drag **packet loss** to 40%. “Indoor radio is lossy, and the dropped-packet counter is real. We measured the cost over 5 seeds: the same 6/6 tasks still finish, just later — 137–196 ticks instead of 93 — with 0.2 deadlock timeouts on average, one seed of five. Stale peers do not freeze the aisle: stalemate timeout, lower id yields. At 70% loss it still finishes 6/6, in 141–213 ticks.”

2:20 — Metrics panel. “Same seeds, same map. Stop-and-wait never finishes (hits 800). UBPA finishes E1 in 93 ticks, E2 head-on in 50, E3 blocked aisle in 99, and 8 robots on 16 tasks in 327. No improvement percentage — the baseline has no finish time.” Flip to **stop-and-wait** if there are 15 seconds left.

2:40 — Limitations: simulated radio, grid kinematics, laptop clock as edge CPU proxy. Volunteer the two weak numbers before a judge finds them: (1) the clean run is 21 ticks slower than the pre-fix 72, because age-correcting peer reservations costs ticks to remove conflicts; (2) the 8-robot point finishes 16/16 but needs 327 ticks and 14 deadlock timeouts, and without the stall hand-back it livelocks at 14/16. Both are in `results/probe_26123_ablation.json` and `results/probe_26123_scale_delay.json`. Volunteer two more if a judge asks how the numbers were produced: (3) the five seeds are a determinism check, not five independent trials — E1 returns identical outcome metrics on all five; (4) UBPA is not universally better — on a low-contention aisle layout with three short jobs, stop-and-wait finishes in 47 ticks with 1 conflict and UBPA in 58 with 4 (`docs/evaluation.md` has the snippet).

## Judge questions

- “Who assigns tasks?” — Nobody. Each robot bids and independently checks who won.
- “Who has right of way?” — UBPA, then robot_id. Show the live scores.
- “What if both have stale data?” — Stalemate timeout, lower id yields. Measured at 3 ticks of delay: 6/6 tasks, 103 ticks, 0 deadlocks, 0 intention conflicts. Removing RC2 alone still finishes 6/6, but at 94 ticks with 3 conflicts; with every resilience fix off, the same slider stops at 3/6 tasks with 577 conflicts (`results/probe_26123_ablation.json`).
- “What does 40% packet loss cost you?” — 137–196 ticks instead of 93 over 5 seeds, still 6/6 tasks, with 0.2 deadlock timeouts on average (one seed of five).
- “Does it still work at fleet scale?” — 6 robots 99 ticks, 8 robots 327 ticks (16/16, 14 deadlock timeouts), 10 robots 180 ticks. Stop-and-wait finishes 0/5 runs at every one of those sizes.
- “Which of your fixes actually matters?” — One at a time, with the switch off: RC1 under 70% loss, RC4 at 3 ticks of delay, RC5 at 8 robots (14/16 without it). RC2 is the one that costs the clean case 21 ticks.
