# SIH26123 — Edge-AI Distributed Fleet Coordination

Three warehouse AMRs. No central dispatcher. Each robot broadcasts pose and path, auctions work, and yields with an explicit UBPA rule.

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

## The 60-second walkthrough

Work down the list in order; each beat builds on the one before it. All of it is live interaction, not a recorded clip.

| # | Do this | What you are seeing |
|---|---|---|
| 1 | Watch the first 20 seconds | Three AMRs with independent local plans. The **Radio (peer packets)** panel is the only channel between them — there is no dispatcher anywhere in the process. |
| 2 | Click any corridor cell | A live obstacle appears. The nearest robot re-plans around it; if that made a goal unreachable, the task goes back on the board and is re-auctioned. |
| 3 | Click **Block aisle** | Head-on conflict in a narrow corridor: conflict detection → local negotiation → one robot yields into a turnout → both continue with `deadlock_count` at 0. UBPA reports `collision_count` 0 in E1 against 2 for stop-and-wait; E2 and E3 record none under either policy. The counter is printed from the run, never asserted. |
| 4 | Drag **Packet loss** to 40% | Peer broadcasts start dropping and the `delivered · dropped` counter moves. Robots that miss a pose update wait rather than guess. Measured over 5 seeds: the same 6/6 tasks still finish, just later — 137–196 ticks instead of 93, with 0 deadlocks. |
| 5 | Drag **Radio delay** to 3 ticks | Intent arrives stale, so yields happen later. Measured: 6/6 tasks, 0 deadlocks, 0 intention conflicts, finish tick 103 (93 at delay 1). That is what the age-corrected peer reservations bought: removing RC2 alone still finishes 6/6, but at 94 ticks with 3 intention conflicts, and with every resilience fix off the same slider stops at 3/6 tasks with 577 intention conflicts (`results/probe_26123_ablation.json`). |
| 6 | Click **Policy: stop-and-wait** | Resets the same floor under the baseline. Two robots meet in the aisle, neither yields, `deadlock_count` climbs and `tasks_done` stalls — exactly the failure the UBPA rule exists to prevent. |
| 7 | Click **Policy: UBPA**, then **Pause** / **Reset** | Back to the fix. Pause freezes the clock so you can talk over a moment; Reset returns to the same seed. |

Two panels worth pointing at while this runs: **Live run meters** (this run only) and **Measured vs stop-and-wait** (loaded from `results/metrics.json`, never invented).

This list runs in the same order as the spoken version, so the timings carry over: `docs/demo_script.md` (0:00 → 2:40) and slide 10 of the deck (0:00 / 0:40 / 1:10 / 1:50 / 2:10 / 2:25).

Useful options:

```bash
python scripts/run_demo.py --policy stop_wait   # start in the baseline instead
python scripts/run_demo.py --port 9000          # force a specific port
python scripts/run_demo.py --no-browser         # do not open a browser tab
python scripts/run_demo.py --seed 7 --robots 4  # different layout draw
```

`--host`, `--port`, `--policy`, `--seed`, `--robots`, `--no-browser` are the full set. Any free working directory is fine: the script resolves the repo root, `frontend/static/`, and `results/metrics.json` from its own file path, not from your current folder.

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

## Degrade the radio, then measure it

The two sliders in the Operator panel are not decoration: `scripts/run_degradation.py` sweeps packet loss and radio delay over 5 seeds each and writes `results/degradation.json`.

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

The 8-robot point is the honest weak spot and is not a rounding error: two robots used to livelock over the `y=1` highway, six more sat IDLE with tasks CLAIMED, and the fleet plateaued at 14/16 tasks forever (0/5 runs). RC5 fixed the livelock by counting stall episodes per task and handing the task back after 4 of them — which is why the row above now reads 16/16. It is still the slowest configuration in the table and still spends 14 deadlock timeouts to get there.

## Tests

```bash
python -m unittest tests.test_mandatory
```

9 tests covering peer-broadcast schema, auction-winner validity, the two forbidden patterns (no central scheduler, no hardcoded winner), UBPA yielding, baseline deadlock recording, live block → replan, and unreachable-pick release. Expected: `Ran 9 tests ... OK`.

## Layout

```
simulation/   engine, robot, peer radio bus, space-time planner, UBPA rule, metrics, scenarios
frontend/     dashboard UI served by simulation/dashboard.py (stdlib http.server)
scripts/      run_demo.py, run_experiments.py, run_degradation.py, build_pptx.py,
              probe_26123_stall_diag.py, probe_26123_scale_delay.py, probe_26123_ablation.py
results/      metrics.json, degradation.json — measured, regenerated by scripts/
              probe_26123_scale_delay.json, probe_26123_ablation.json — scale + fix attribution
tests/        test_mandatory.py
docs/         problem analysis, requirements, architecture, methodology,
              measured evaluation, demo script, slide outline, both decks
```

Windows note: if `git` is not on your PATH, it lives at `C:\Program Files\Git\cmd\git.exe`.

## Presentation

Two decks, same measured numbers, different jobs:

- `docs/SIH26123_EdgeFleet_FULL-12slide-Detail.pptx` (and `.pdf`) — 12 slides, 16:9, speaker notes on all of them. Rebuild with `python scripts/build_pptx.py`.
- `docs/SIH26123_EdgeFleet_OFFICIAL-6slide-Template.pptx` (and the ready-to-upload `.pdf`) — the 6-slide official SIH format. Built from the supplied template by `../sih26123_pptbuild/build_tpl_26123.py`; see `../SIH 226123 Ppts/SIH26123_DECKS_README.md` for the deck-by-deck comparison.

Neither builder contains a typed metric. Both read `results/metrics.json`, `results/degradation.json`, `results/probe_26123_scale_delay.json` and `results/probe_26123_ablation.json` at build time, and `python ../sih26123_pptbuild/verify_deck_numbers_26123.py` re-reads both decks and fails (exit 1) if any cited number is missing from the results files, if the deck copies drift apart, or if an unexplained percentage appears.
