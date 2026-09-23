# Optional features (after MVP)

- Charging policy when battery < 20%.
- More than three robots / larger map.
- Packet loss / radio delay slider — **in the live demo** (Operator panel). The scripted sweep is done: `scripts/run_degradation.py` writes `results/degradation.json` (clean, 40% loss, 70% loss, 3-tick delay, 40% loss + 3-tick delay; 5 seeds each). Note on reading those five seeds: in the **clean** scenario every seed coincides exactly (same outcome metrics; only the mean step time moves), so the five runs are a determinism check, not five independent trials. Under loss/delay the seeds do diverge, which is why the degraded rows are reported as ranges/averages (e.g. 40% loss averages 0.2 deadlock timeouts — one seed of five). See `docs/evaluation.md`, "Seeds: what the five seeds do and do not show".
- Export of judge-facing plots from `results/`.
- On-device (Raspberry Pi) tick loop — same robot code, real clock.
