# SIH26123 — Edge-AI Distributed Fleet Coordination

Three to eight AMRs. Four indoor floors. No central dispatcher. Each robot broadcasts pose and path, auctions work, yields with an explicit UBPA rule, and decides on its own when to break off and charge.

Python 3.10+, standard library only — no install step, no build step, no service to configure.

## Run it in one minute

```bash
git clone https://github.com/goldeneagle8965/sih26123-edge-fleet.git
cd sih26123-edge-fleet
python scripts/run_demo.py
```

On Windows, **double-click `RUN_DEMO.bat`** in the repo folder instead — it changes into the right directory for you and starts the same command.

Your browser opens on the URL it prints (default <http://127.0.0.1:8765/>). That terminal *is* the simulation clock — 8 ticks/second. Leave it running and drive everything from the browser. `Ctrl+C` stops it.

No git? Use **Code → Download ZIP** on the repository page. The demo needs nothing but Python.

If you ever see a stale page, or the server prints a different port than you expected, that is the automatic port fallback working: a previous run is still holding 8765, so this run moved to the next free port. `Ctrl+C` the old terminal (or just use the new URL) — the script never double-binds a busy port.

**Running it from the wrong folder** is the usual reason nothing happens. `python scripts/run_demo.py` is a relative path, so your shell has to already be in the repo root. Symptom and fix:

```text
can't open file '...\scripts\run_demo.py': [Errno 2] No such file or directory   <- wrong folder
```

```bash
# Windows PowerShell / cmd
cd "C:\path\to\sih26123"
python scripts\run_demo.py
```

```bash
# macOS / Linux
cd /path/to/sih26123
python3 scripts/run_demo.py
```

`run_demo.py` opens on the warehouse floor and serves to `127.0.0.1` only — switch floors from the UI once it is running. `--host --port --policy --seed --robots --no-browser` are its full set of options.

## Serve it to a room, a phone, or a URL

`scripts/run_web.py` is the hosting entry point — same dashboard, but built for a real deployment: it binds `0.0.0.0`, honours `$PORT`/`$HOST`, and gives **every visitor their own independent fleet** (cookie-scoped session, capped by count and idle TTL) so two judges clicking at once never fight over one floor.

```bash
python scripts/run_web.py                      # 0.0.0.0:8765, one fleet per visitor
python scripts/run_web.py --port 8000 --open   # pick the port, open a browser tab
python scripts/run_web.py --single             # ONE shared fleet — best on a projector
```

On Windows, **double-click `RUN_WEB.bat`** (it starts the same command with `--open` and keeps the window open). The banner prints both URLs it is reachable on:

```text
SIH26123 — Edge-AI Distributed Fleet Coordination
  floor      : Warehouse (3PL)
  coordination: every robot bids over the peer radio; no central dispatcher
  this machine: http://127.0.0.1:8765/
  same Wi-Fi  : http://192.168.x.x:8765/
  sessions    : one independent fleet per visitor (max 32, idle ttl 3600s)
  stop        : Ctrl+C
```

That `same Wi-Fi` line is the one to show judges: they open it on their own phone and drive their own fleet while your laptop keeps running the projector view. `--visitors`, `--ttl` and `--single` tune the sharing model; `HOST`, `PORT`, `MAX_SESSIONS` and `SESSION_TTL_S` are the env-var equivalents, so the same file works unchanged on a PaaS.

Deployment artifacts are in the repo root and all point at the same entry point:

| file | what it is |
|---|---|
| `Dockerfile` + `.dockerignore` | `python:3.12-slim`, non-root user, `PYTHONUNBUFFERED=1`, `HEALTHCHECK` on `/health`, binds `$PORT` |
| `Procfile` | `web: python scripts/run_web.py --host 0.0.0.0` (Heroku-style) |
| `render.yaml` | Render blueprint: Docker service, health check path `/health`, auto-deploy off |
| `RUN_WEB.bat` | double-click local server on Windows |

```bash
docker build -t sih26123-edge-fleet .
docker run --rm -p 8765:8765 -e PORT=8765 sih26123-edge-fleet
# then: curl -s localhost:8765/health
```

Everything the page does is decided server-side in `simulation/`; the browser only sends actions and renders snapshots. Nothing in the container needs GPU, network access or a build toolchain, which is why the image is ~150 MB and starts in about a second.

## The 2-minute walkthrough

Work down the list in order; each beat builds on the one before it. All of it is live interaction, not a recorded clip.

| # | Do this | What you are seeing |
|---|---|---|
| 1 | Watch the first 20 seconds | Three AMRs, each with its own local plan. The header chips show `tick`, `tick rate`, `policy`, `fleet` and `session`. The **Radio — peer packets** panel is the only channel between the robots — there is no dispatcher anywhere in the process, and the panel header reads `peer packets last 60 ticks: N · messages from a central scheduler: 0`. |
| 2 | Hover a robot | A tooltip with the state *that robot* holds locally: its plan, its claim, what it believes the others are doing. |
| 3 | Click any free cell, then **Clear obstacles** | A live obstacle appears and the nearest robot re-plans on the next tick; if that made a goal unreachable, the job goes back on the board and is re-auctioned. Clearing puts the floor back. |
| 4 | Click **Block aisle** (key `b`) | Head-on conflict in a narrow corridor: conflict detection → local negotiation → one robot yields into a turnout → both continue, with the bad-event counter at 0. |
| 5 | Slide **Robots** from 3 to 8 | The fleet resizes live — extra robots spawn at their own cells and start bidding. This is the same fleet size that gets slowest in the scale table below, and the meters show it. |
| 6 | Press a **Floor** button: Warehouse / Shopping mall / Hospital / Restaurant | The whole simulation is rebuilt on that layout — its own racks, counters, wards and pillars, its own job mix (hospital picks are urgent), its own spawn cells, depot and charging station. The panel title and blurb follow the floor. |
| 7 | Open **Job board → Inject a job** | Add work at runtime instead of editing a queue: type pick/drop/urgency, or press **Set pick from map** / **Set drop from map** and click the floor. The job is announced over the radio and robot bids appear in **Being decided right now**. Invalid drops (inside a rack) are rejected with the cell named — that validation is deliberate, not a silent no-op. |
| 8 | Drag a robot's battery in **Charging station** | Drain a robot below its reserve: it abandons the queue, drives to the charger, charges, and resumes — all decided on the robot, not by the UI. |
| 9 | Slide **Simulation speed** (0.25× → 16×), then **Pause** / **Step 1 tick** / **Reset** (keys `space` / `s` / `r`) | Pause freezes the clock so you can talk over a moment; step advances exactly one tick; reset returns to the same seed. |
| 10 | Drag **Packet loss** to 40% | Peer broadcasts start dropping and `delivered · dropped` moves. Robots that miss a pose update wait rather than guess: measured over 5 seeds, the same 6/6 tasks still finish, just later — 137–196 ticks instead of 93, with 0 deadlocks. |
| 11 | Drag **Broadcast delay** to 3 ticks | Intent arrives stale, so yields happen later. Measured: 6/6 tasks, 0 deadlocks, 0 intention conflicts, finish tick 103 (93 at delay 1). Removing the age-corrected peer reservations alone still finishes 6/6, but at 94 ticks with 3 intention conflicts; with every resilience fix off, the same slider stops at 3/6 tasks with 577 intention conflicts (`results/probe_26123_ablation.json`). |
| 12 | Press **Forecast ETA** | Runs the current fleet forward and reports the finish estimate for this floor, fleet size and job set — a prediction from the live run, not a fixed number. |
| 13 | Click **Stop-and-wait** under *Coordination policy* | Resets the same floor under the baseline. Two robots meet in the aisle, neither yields, and `tasks_done` stalls — exactly the failure UBPA exists to prevent. Switch back to **UBPA**. |
| 14 | Point at **Shadow twin** and **Measured on this build** | The shadow twin runs the *other* policy on the same seed and map in a second live simulation, side by side. The measured panel's header names the file it is reading (`results/metrics.json`). **Re-measure on this machine** re-runs the whole suite on this CPU and prints its numbers beside the shipped ones, flagging any tick-count metric that failed to reproduce (wall-clock `mean_robot_step_ms` is shown but never counted as a mismatch). The fresh run is written to `results/remeasure_metrics.json`; the shipped `metrics.json` is never overwritten. |

Panels that exist purely as evidence: **Being decided right now** (each row is a task one robot is brokering to the others — read-only proof the choice is made on the floor), **Event feed** (operator-relevant changes, including re-auctions and charger trips) and **The pitch in 30 seconds** at the bottom, which is the spoken summary in four bullets.

Shortcuts: `space` pause · `s` step · `b` block aisle · `r` reset · `f` forecast.

This list runs in the same order as the spoken version, so the timings carry over: `docs/demo_script.md` (0:00 → 2:40) and slide 10 of the deck (0:00 / 0:40 / 1:10 / 1:50 / 2:10 / 2:25).

## Where each requested feature lives

| requested | where it is | what makes it more than a mock-up |
|---|---|---|
| functional site, not read-only | `frontend/static/` ↔ `simulation/dashboard.py` | Actions are POSTs that mutate the running simulation (`policy`, `env`, `robots`, `speed`, `radio`, `add_task`, `remove_task`, `drain`, `block_aisle`, `toggle`, `clear_blocks`, `pause`/`resume`, `step`, `reset`, `forecast`, `remeasure`); the page polls snapshots. The browser holds no state the server does not own. |
| brief project info | **The pitch in 30 seconds** card, `docs/problem_analysis.md` | Four bullets: problem, what was built, how it holds up, where the evidence comes from. |
| 3+ robots on the map | **Operator console → Robots** slider, 3–8 | Live mid-run fleet resize: new robots spawn at free cells and join the auctions; the snapshot schema and meters follow. |
| maps beyond warehouse | **Floor** buttons — Warehouse (3PL), Shopping mall, Hospital, Restaurant | Each is a real layout in `simulation/maps.py` with its own obstacles, job set, spawn cells, depot and charger — not a colour swap. |
| custom robot count, max 8 | same slider, plus `--robots` on `run_demo.py` | The UI clamps at 8 by design; the scale probe goes to 10 on purpose, to show where the coordination starts to cost. |
| task dashboard + charging station | **Job board** (live jobs, ages, assignee) and **Charging station** (per-robot battery, charger occupancy, forced-low-charge drill) | Battery drain, reserve break-off, charging and resume are all simulated per tick, and the drill works on any robot, any time. |
| wow factor | **Shadow twin**, **Being decided right now**, **Re-measure on this machine**, live obstacle painting, live fleet resize, 4 floors, phone-playable sessions | Every one of them is a live simulation read-out, not an animation: two policies run side by side on the same seed, peer auctions are visible as they resolve, and the headline metrics can be rebuilt in front of the judges. |

## Measure it yourself (about 4 seconds)

```bash
python scripts/run_experiments.py
```

Writes `results/metrics.json` and prints the comparison table. Fixed seeds mean re-running reproduces every decision metric exactly — tasks, ticks, deadlocks, collisions, distance, wait, comm latency. The only field that moves is `mean_robot_step_ms`, because that one is wall-clock on your machine.

Last measured run (tick limit 800; means across seeds):

| experiment | stop-and-wait | UBPA |
|---|---|---|
| E1 throughput | incomplete (0/6 tasks, 2 deadlocks) | **93 ticks, 6/6, 0 deadlocks** |
| E2 head-on | incomplete (1/3 tasks, 2 deadlocks) | **50 ticks, 3/3, 0 deadlocks** |
| E3 aisle block at t=16 | incomplete (3/6 tasks, 3 deadlocks) | **99 ticks, 6/6, 0 deadlocks** |

`improvement` is `null` throughout the JSON on purpose: the baseline never finished inside the tick limit, so every percentage would be meaningless (`comparable=false`). Raw values only. The full table, per-seed runs and the reasoning: `docs/evaluation.md`.

The re-measure button was verified end to end on the machine this was written on: job `exit_code: 0` in 4.4 s, `results/remeasure_metrics.json` rewritten, the shipped `results/metrics.json` byte-identical afterwards, and all three experiments reporting `match: true` — every tick-count headline metric reproduced exactly, with only `mean_robot_step_ms` differing (`0.1558` shipped vs `0.1147` now) and correctly excluded from the match.

## Degrade the radio, then measure it

The two sliders in the Operator console are not decoration: `scripts/run_degradation.py` sweeps packet loss and radio delay over 5 seeds each and writes `results/degradation.json`.

| scenario | UBPA outcome (means over 5 seeds) |
|---|---|
| clean (0% loss, 1 tick delay) | 6/6 tasks, 0 deadlocks, finishes tick 93 |
| 40% packet loss | **still 6/6, 0 deadlocks**, finishes in 137–196 ticks |
| 70% packet loss | still 6/6, 0.6 deadlocks on average, finishes in 141–213 ticks |
| 3 tick radio delay | 6/6 tasks, 0 deadlocks, 0 intention conflicts, finishes tick 103 |
| 40% loss + 3 tick delay | still 6/6, finishes in 99–177 ticks |

`packets_dropped` rises with the loss knob but is not a cost metric on its own: longer runs mean more broadcasts, so a run that finishes under 70% loss drops more packets in total than a run that used to die early. 40% loss is the number quoted in the decks and in the demo script, and it is read from this file at build time — never typed into a slide by hand.

## Robustness fixes and how they were attributed

Five fixes landed in `simulation/` after the first honest measurement round, each behind its own `CoordinationFlags` switch:

| fix | flag | what it stops |
|---|---|---|
| RC1 task re-announce | `task_reannounce` | a task nobody heard is dead for the whole run |
| RC2 age-corrected peer reservations | `age_corrected_peers` | reserving the wrong three ticks of floor when intent arrives stale |
| RC3 stall detector | `stall_detector` | a frozen fleet reporting zero deadlocks |
| RC4 claim arbitration + expiry | `claim_arbitration`, `claim_expiry` | two robots holding the same task, and stale claims |
| RC5 stall hand-back with cooldown | `stall_handoff` | a holder that cannot reach a task parking on it forever |

`python scripts/probe_26123_ablation.py` re-runs every scenario with exactly one flag off and writes `results/probe_26123_ablation.json`. That is where the claim "the fixes work" is checked instead of asserted:

| scenario | shipped | all fixes off | which single fix, when removed, breaks it |
|---|---|---|---|
| E1 clean | 5/5, 93 ticks, 0 conflicts | 5/5, 72 ticks, 2 conflicts | none — but removing RC2 alone gives 72/2, i.e. RC2 costs 21 ticks and buys 2 conflict-free runs |
| delay 3 | 5/5, 103 ticks, 0 conflicts | 0/5, 3/6 tasks, 577 conflicts | RC4 (0/5, 5/6) |
| 70% loss | 5/5, 188 ticks | 0/5, 1–5/6 tasks | RC1 (0/5, 2–5/6) |
| 8 robots | 5/5, 327 ticks, 16/16 tasks | 0/5, 3/16 tasks | RC5 (0/5, 14/16), RC2 (0/5, 12/16), RC3 (0/5, 10/16) |

Two things that table says out loud rather than hides: RC2 makes the clean case **slower** (72 → 93 ticks), and the fixes are not interchangeable — RC1 and RC4 do nothing at 8 robots, and RC5 does nothing at 3. Deadlock counts are also detector-dependent: a row with `stall_detector` off reports 0 by construction.

## Scale, and where it stops being pretty

`python scripts/probe_26123_scale_delay.py` grows the fleet (2 jobs per robot, distinct spawn cells) and writes `results/probe_26123_scale_delay.json`:

| fleet | UBPA | stop-and-wait |
|---|---|---|
| 3 robots / 6 tasks | 5/5 runs, 93 ticks, 0 conflicts | 0/5 runs, 0/6 tasks |
| 6 robots / 12 tasks | 5/5 runs, 99 ticks, 0 conflicts | 0/5 runs, 0/12 tasks |
| 8 robots / 16 tasks | 5/5 runs, 327 ticks, 13 conflicts, 14 deadlock timeouts, 2 task hand-backs | 0/5 runs, 1/16 tasks |
| 10 robots / 20 tasks | 5/5 runs, 180 ticks, 15 conflicts | 0/5 runs, 1/20 tasks |

The 8-robot point is the honest weak spot and is not a rounding error: two robots used to livelock over the `y=1` highway, six more sat IDLE with tasks CLAIMED, and the fleet plateaued at 14/16 tasks forever (0/5 runs). RC5 fixed the livelock by counting stall episodes per task and handing the task back after 4 of them — which is why the row above now reads 16/16. It is still the slowest configuration in the table and still spends 14 deadlock timeouts to get there. The 10-robot row finishes faster in ticks than 8 but carries more intention conflicts; both are in the JSON and neither is hidden. What that means for real hardware — two clocks, compute budget, radio budget, ISO 3691-4 safety rules, a parts list and four milestones with exit criteria — is in `docs/hardware_feasibility.md`.

## Tests

```bash
python -m unittest tests.test_mandatory tests.test_dashboard_contract tests.test_web_entry
```

25 tests, `Ran 25 tests ... OK`.

| file | tests | what it locks down |
|---|---|---|
| `tests/test_mandatory.py` | 9 | the problem statement's hard requirements: peer-broadcast schema, auction-winner validity, the two forbidden patterns (no central scheduler, no hardcoded winner), UBPA yielding, baseline deadlock recording, live block → replan, unreachable-pick release |
| `tests/test_dashboard_contract.py` | 6 | the dashboard API contract, looped over all four floors and fleets of 3/5/8: snapshot schema, every action (`policy`, `env`, `robots`, `speed`, `radio`, `add_task`, `remove_task`, `drain`, `block_aisle`, `toggle`, `clear_blocks`, `pause`, `step`, `reset`, `forecast`), shadow-twin output, and that a rejected job is rejected with a named reason |
| `tests/test_web_entry.py` | 10 | the hosting path: env/arg parsing, loopback round trip through `serve_sessions`, per-visitor cookie isolation (two visitors never share a fleet), `/health` and 404 behaviour |

They run in under two seconds and need no network beyond loopback.

## Layout

```
simulation/   engine, robot, peer radio bus, space-time planner, UBPA rule, maps, metrics, scenarios, dashboard
frontend/     dashboard UI served by simulation/dashboard.py (stdlib http.server)
scripts/      run_demo.py, run_web.py, run_experiments.py, run_degradation.py, build_pptx.py,
              probe_26123_stall_diag.py, probe_26123_scale_delay.py, probe_26123_ablation.py
results/      metrics.json, degradation.json — measured, regenerated by scripts/
              probe_26123_scale_delay.json, probe_26123_ablation.json — scale + fix attribution
tests/        test_mandatory.py, test_dashboard_contract.py, test_web_entry.py
docs/         problem analysis, requirements, architecture, methodology,
              measured evaluation, demo script, slide outline, hardware feasibility, both decks
```

Deployment files live in the repo root: `Dockerfile`, `.dockerignore`, `Procfile`, `render.yaml`, `RUN_WEB.bat`, `RUN_DEMO.bat`.

Windows note: if `git` is not on your PATH, it lives at `C:\Program Files\Git\cmd\git.exe`.

## Docs worth reading before you present

- `docs/hardware_feasibility.md` — what this would take on real AMRs: two-clock framing (8 Hz sim vs 0.5–1.0 s per cell of real motion), compute and radio budgets against sourced specs (Jetson Orin Nano, TurtleBot 4, MiR250, OTTO 100), radio model vs ETSI TR 102 638 and ROS 2 DDS latency figures, ISO 3691-4 safety obligations, and four milestones with concrete exit criteria. Prices are marked *quote required* rather than invented.
- `docs/evaluation.md` — the measured tables with per-seed runs and the reasoning behind `comparable=false`.
- `docs/demo_script.md` — the spoken 0:00 → 2:40 version of the walkthrough above.

## Presentation

Two decks, same measured numbers, different jobs:

- `docs/SIH26123_EdgeFleet_FULL-12slide-Detail.pptx` (and `.pdf`) — 12 slides, 16:9, speaker notes on all of them. Rebuild with `python scripts/build_pptx.py`.
- `docs/SIH26123_EdgeFleet_OFFICIAL-6slide-Template.pptx` (and the ready-to-upload `.pdf`) — the 6-slide official SIH format. Built from the supplied template by `../sih26123_pptbuild/build_tpl_26123.py`; see `../SIH 226123 Ppts/SIH26123_DECKS_README.md` for the deck-by-deck comparison.

Neither builder contains a typed metric. Both read `results/metrics.json`, `results/degradation.json`, `results/probe_26123_scale_delay.json` and `results/probe_26123_ablation.json` at build time, and `python ../sih26123_pptbuild/verify_deck_numbers_26123.py` re-reads both decks and fails (exit 1) if any cited number is missing from the results files, if the deck copies drift apart, or if an unexplained percentage appears.
