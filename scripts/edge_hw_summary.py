"""Collect a three-node Pi run into one machine-readable hardware summary.

One command, one file. On the Pi run, launch the host and each agent with
``--summary-json`` pointing into a shared directory; then, on the laptop:

    python scripts/edge_hw_summary.py --dir hw_runs --expect R1,R2,R3

It reads every ``*.json`` record in that directory (writing its own output as
``hardware_summary.json`` unless ``--out`` says otherwise), merges the per-node
records into ``hardware_summary.json`` and prints the fleet roll-up.

Honesty rules
-------------
* Every number is copied from a record a node actually wrote. Nothing is
  recomputed from what the run was *supposed* to do, and nothing is filled in.
* A node that produced no record is listed in ``missing`` and its fields stay
  ``null`` - an absent robot is reported as absent, not as a zero.
* CPU/RAM measurement needs ``resource`` and ``/proc/self/statm``. On a platform
  without them (e.g. a Windows rehearsal) the per-node block keeps
  ``available: false`` and the reason, and the fleet totals over those fields are
  ``null``, so a laptop rehearsal can never be read as Pi evidence.
* This collector runs on the *laptop*, not the Pi: the laptop cannot read a
  Pi's CPU time, which is exactly why each node measures itself.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "sih26123-hw-summary/1"
DEFAULT_OUT_NAME = "hardware_summary.json"


def _load_records(directory: Path, out_path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Read every JSON record in ``directory`` except our own output."""
    records: list[dict[str, Any]] = []
    unreadable: list[str] = []
    for path in sorted(directory.glob("*.json")):
        if out_path.exists() and path.resolve() == out_path.resolve():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            unreadable.append(f"{path.name}: {exc}")
            continue
        if isinstance(data, dict):
            data["_source_file"] = path.name
            records.append(data)
        else:
            unreadable.append(f"{path.name}: not a JSON object")
    return records, unreadable


def _number(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _node_rollup(record: dict[str, Any]) -> dict[str, Any]:
    """One agent record, flattened to the fields the summary reports."""
    robot_id = record.get("id") or record.get("_source_file", "?")
    resources = record.get("resources") if isinstance(record.get("resources"), dict) else {}
    peers_known = record.get("peers_known") or []
    peers_observed = record.get("peers_observed") or []
    others = [p for p in peers_observed if p != robot_id]
    return {
        "id": robot_id,
        "source_file": record.get("_source_file"),
        "adopted": record.get("adopted"),
        "tasks_done": record.get("tasks_done"),
        "acts_missing": record.get("acts_missing"),
        "malformed": record.get("malformed"),
        "step_ms_p95": record.get("step_ms_p95"),
        "peers_known": peers_known,
        # Heard directly on the peer channel, robot to robot: the evidence that
        # coordination is peer-to-peer and not relayed through the host.
        "peers_observed": peers_observed,
        "peer_observation": bool(others),
        "process_uptime_s": record.get("process_uptime_s"),
        "resources_available": bool(resources.get("available")),
        "resources_reason": resources.get("reason"),
        "cpu_total_s": resources.get("cpu_total_s"),
        "cpu_pct_of_core": resources.get("cpu_pct_of_core"),
        "rss_mb": resources.get("rss_mb"),
        "load_avg_1m": resources.get("load_avg_1m"),
    }


def _pick_host(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The most recently written host record, if any."""
    hosts = [r for r in records if r.get("role") == "host"]
    if not hosts:
        return None
    return max(hosts, key=lambda r: str(r.get("_source_file", "")))


def _sum_or_none(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return round(sum(present), 3)


def _mean_or_none(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return round(sum(present) / len(present), 4)


def build_summary(
    directory: Path,
    records: list[dict[str, Any]],
    unreadable: list[str],
    expect: list[str],
) -> dict[str, Any]:
    host = _pick_host(records)
    agents = [r for r in records if r.get("role") == "robot" or "adopted" in r]
    nodes = {r["id"]: _node_rollup(r) for r in agents if r.get("id")}

    seen = sorted(nodes)
    missing = [rid for rid in expect if rid not in nodes]

    resources_available = [n["resources_available"] for n in nodes.values()]
    gaps = sorted({n["resources_reason"] for n in nodes.values() if n["resources_reason"]})

    host_link = (host or {}).get("link") or {}
    host_resources = (host or {}).get("resources") or {}

    fleet: dict[str, Any] = {
        "host_present": host is not None,
        "expected_nodes": expect,
        "nodes_observed": seen,
        "missing": missing,
        "admitted": (host or {}).get("admitted"),
        "robots_expected": (host or {}).get("robots"),
        "all_admitted": bool(host) and (host or {}).get("admitted") == (host or {}).get("robots"),
        "tasks_done": (host or {}).get("tasks_done"),
        "tasks_total": (host or {}).get("tasks_total"),
        "complete": (host or {}).get("full"),
        "complete_tick": (host or {}).get("complete_tick"),
        "collisions": (host or {}).get("collisions"),
        "deadlocks": (host or {}).get("deadlocks"),
        "stalls": (host or {}).get("stalls"),
        "all_nodes_adopted": bool(nodes) and all(n.get("adopted") for n in nodes.values()),
        "all_nodes_observed_peers": bool(nodes) and all(n.get("peer_observation") for n in nodes.values()),
        "peer_links": {rid: n["peers_observed"] for rid, n in sorted(nodes.items())},
        "agent_tasks_done_sum": _sum_or_none([n.get("tasks_done") for n in nodes.values()]),
        "step_ms_p95_max": max(
            [v for v in (n.get("step_ms_p95") for n in nodes.values()) if v is not None],
            default=None,
        ),
        "step_ms_p95_mean": _mean_or_none([n.get("step_ms_p95") for n in nodes.values()]),
        "mean_rtt_ms": host_link.get("mean_rtt_ms"),
        "rtt_samples": host_link.get("rtt_samples"),
        "link_sent": host_link.get("sent"),
        "link_recv": host_link.get("recv"),
        "link_dropped": host_link.get("dropped"),
        "link_lost_pct": host_link.get("lost_pct"),
        "link": host_link or None,
        "node_cpu_total_s_sum": _sum_or_none([n.get("cpu_total_s") for n in nodes.values()]),
        "node_rss_mb_sum": _sum_or_none([n.get("rss_mb") for n in nodes.values()]),
        "host_cpu_total_s": host_resources.get("cpu_total_s"),
        "host_rss_mb": host_resources.get("rss_mb"),
        "host_dashboard": (host or {}).get("dashboard"),
        "host_transport": (host or {}).get("transport"),
        "host_policy": (host or {}).get("policy"),
        "host_seed": (host or {}).get("seed"),
    }

    return {
        "schema": SCHEMA,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_dir": str(directory),
        "records_read": len(records),
        "unreadable_files": unreadable,
        "resources_available_any": any(resources_available),
        "resource_gaps": gaps,
        "nodes": {rid: nodes[rid] for rid in seen},
        "fleet": fleet,
        "notes": [
            "Every value is copied from a record a node wrote; nothing is inferred.",
            "A missing node is listed in fleet.missing; its fields are absent, not zero.",
            "CPU/RAM needs `resource` and /proc/self/statm; if unavailable, "
            "resources_available is false and the totals are null on purpose.",
            "This collector runs on the laptop and cannot measure a Pi's CPU/RAM; "
            "each node measures itself.",
        ],
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Merge per-node edge run records into one hardware summary")
    p.add_argument("--dir", default="hw_runs", help="directory holding the per-node *.json records")
    p.add_argument("--out", default=None, help=f"output path (default: DIR/{DEFAULT_OUT_NAME})")
    p.add_argument("--expect", default="R1,R2,R3", help="comma-separated node ids expected to report")
    args = p.parse_args()

    directory = Path(args.dir)
    out_path = Path(args.out) if args.out else directory / DEFAULT_OUT_NAME
    if not directory.is_dir():
        print(f"no such directory: {directory}", file=sys.stderr)
        return 2

    expect = [rid.strip() for rid in args.expect.split(",") if rid.strip()]
    records, unreadable = _load_records(directory, out_path)
    if not records:
        print(f"no JSON records in {directory}", file=sys.stderr)
        return 3

    summary = build_summary(directory, records, unreadable, expect)
    try:
        if out_path.parent and not out_path.parent.exists():
            out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"could not write {out_path}: {exc}", file=sys.stderr)
        return 4

    f = summary["fleet"]
    print(f"hardware summary -> {out_path}")
    print(f"  nodes observed : {', '.join(f['nodes_observed']) or '(none)'}")
    print(f"  missing        : {', '.join(f['missing']) or '(none)'}")
    print(f"  admitted       : {f['admitted']}/{f['robots_expected']}  all_admitted={f['all_admitted']}")
    print(f"  tasks          : {f['tasks_done']}/{f['tasks_total']}  complete={f['complete']} "
          f"complete_tick={f['complete_tick']}")
    print(f"  collisions     : {f['collisions']}  deadlocks: {f['deadlocks']}  stalls: {f['stalls']}")
    print(f"  peer links     : {f['peer_links']}")
    print(f"  step_ms_p95    : max={f['step_ms_p95_max']} mean={f['step_ms_p95_mean']}")
    print(f"  link           : sent={f['link_sent']} recv={f['link_recv']} dropped={f['link_dropped']} "
          f"mean_rtt_ms={f['mean_rtt_ms']} samples={f['rtt_samples']}")
    if summary["resources_available_any"]:
        print(f"  cpu (nodes)    : {f['node_cpu_total_s_sum']}s total  rss_mb_sum={f['node_rss_mb_sum']}  "
              f"host_cpu={f['host_cpu_total_s']}s host_rss_mb={f['host_rss_mb']}")
    else:
        reason = "; ".join(summary["resource_gaps"]) or "platform cannot measure it"
        print(f"  cpu/ram        : NOT MEASURED here ({reason})")
    if unreadable:
        print(f"  unreadable     : {len(unreadable)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())