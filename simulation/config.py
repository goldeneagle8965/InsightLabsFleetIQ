"""SIH26123 simulation constants. Tuned for a live 8 Hz demo, not a paper-perfect AMR."""

from __future__ import annotations

from dataclasses import dataclass

TICK_HZ = 8

BID_WINDOW = 3
SENSOR_RANGE = 2
STALEMATE_TICKS = 8
LOOKAHEAD = 6
HOLD_HORIZON = 4
PATH_HORIZON = 12
UNREACHABLE_TICKS = 14
PICK_TICKS = 2
DROP_TICKS = 2
YIELD_WAIT = 6
RADIO_DELAY = 1
PEER_STALE_TICKS = 6

BATTERY_START = 100.0
DRAIN_MOVE = 0.18
DRAIN_WAIT = 0.03
CHARGE_RATE = 1.6
CHARGE_THRESHOLD = 20.0
CHARGE_RESUME = 80.0

AUCTION_BATTERY_PENALTY = 40.0
AUCTION_CONGESTION = 8.0
AUCTION_WORKLOAD = 6.0

UBPA_U = 4.0
UBPA_B = 2.0
UBPA_P = 1.5
UBPA_A = 1.0

MAX_TICKS_DEMO = 4000
MAX_TICKS_EXPERIMENT = 800

# --- coordination health -------------------------------------------------
# STALL_WINDOW: ticks of zero movement, while the robot still holds an
# unfinished task, that count as a stalled robot. 20 ticks = 2.5 s at 8 Hz,
# well above queueing behind a peer that is picking or dropping (<= 2 ticks).
STALL_WINDOW = 20
# WMS bulletin repeats unheard tasks this often. A one-shot announce plus 70%
# loss means a task can be dropped for every robot and orphaned forever.
TASK_REANNOUNCE_TICKS = 20
# A claim the named holder's own broadcast contradicts survives this long
# (covers the claim-vs-state race on a delayed radio) before going back OPEN.
CLAIM_GRACE_TICKS = 4
# A claim whose holder has not been heard from at all expires after this long.
CLAIM_SILENT_TICKS = 24
# Minimum gap between re-assertions of a claim we won in a contested race.
CLAIM_REASSERT_TICKS = 8
# Consecutive stall episodes on the same task before the holder hands it back.
# Re-planning is not enough when the contested junction is the only route: two
# holders can re-plan into each other forever. Four episodes = 80 ticks.
STALL_HANDOFF_EPISODES = 4
# After a stall hand-back the ex-holder stops bidding on that task for a while,
# so the fleet retries the work from a different position instead of the same
# robot walking straight back into the same junction.
STALL_COOLDOWN_TICKS = 60


@dataclass(frozen=True)
class CoordinationFlags:
    """Ablation switches. Defaults are the shipped behaviour.

    Every flag exists so a reviewer can turn one fix off and re-measure the
    same scenarios, rather than trusting a before/after claim.
    """

    stall_detector: bool = True        # RC3 movement-stall counter + recovery
    task_reannounce: bool = True       # RC1 WMS repeats unheard bulletins
    age_corrected_peers: bool = True   # RC2 project peer paths by message age
    claim_arbitration: bool = True     # RC4a deterministic claim tie-break
    claim_expiry: bool = True          # RC4b drop claims the holder contradicts
    stall_handoff: bool = True         # RC5 hand a task back after repeated stalls

MAP_W = 25
MAP_H = 11

DIRS = ((0, -1), (1, 0), (0, 1), (-1, 0))
