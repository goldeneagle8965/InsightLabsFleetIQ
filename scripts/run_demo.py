"""Live spectator dashboard. Does not assign tasks or right-of-way.

Safe to launch from any working directory: the repo root is derived from this
file's own path (so imports and results/metrics.json always resolve), and if
the requested port is already serving an older dashboard the script moves to
the next free port instead of silently double-binding it.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.config import TICK_HZ  # noqa: E402
from simulation.dashboard import Dashboard, serve  # noqa: E402
from simulation.engine import FleetSim  # noqa: E402


def _port_busy(host: str, port: int, timeout: float = 0.35) -> bool:
    """True when something already answers on host:port.

    A plain connect probe is used instead of a bind probe because the HTTP
    server enables SO_REUSEADDR, which on Windows lets a second instance bind
    the same port and serve a stale page without any error.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        try:
            return probe.connect_ex((host, port)) == 0
        except OSError:
            return False


def _pick_port(host: str, port: int, tries: int = 40) -> int:
    for candidate in range(port, port + tries + 1):
        if not _port_busy(host, candidate):
            return candidate
    return port


def main() -> None:
    p = argparse.ArgumentParser(description="SIH26123 live warehouse demo")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--policy", choices=("ubpa", "stop_wait"), default="ubpa")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--robots", type=int, default=3)
    p.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    args = p.parse_args()

    sim = FleetSim(n_robots=max(3, args.robots), policy=args.policy, seed=args.seed)
    dash = Dashboard(sim)

    port = _pick_port(args.host, args.port)
    if port != args.port:
        print(f"Port {args.port} is already serving a dashboard; using {port} instead.")

    httpd = None
    for _ in range(10):
        try:
            httpd = serve(dash, args.host, port)
            break
        except OSError as exc:
            print(f"Port {port} unavailable ({exc}); trying {port + 1}.")
            port += 1
    if httpd is None:
        raise SystemExit("Could not bind a local port; pass --port <n> to choose one.")

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://{args.host}:{port}/"
    print(f"SIH26123 dashboard: {url}")
    print(f"repo root: {ROOT}")
    print(f"fleet: {sim.n_robots} robots, policy={sim.policy}, seed={sim.seed}, {TICK_HZ} ticks/s")
    print("Click a cell to block it. Block aisle for the chaos beat.")
    print("Ctrl+C to stop.")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # no browser on this host; the URL above still works
            pass

    # The dashboard owns the clock: it consumes ticks at the requested speed and
    # steps the shadow twin in lock-step, so the local demo shows the same
    # comparison the deployed page does.
    stop = threading.Event()
    driver = threading.Thread(target=dash.run_forever, args=(stop,), daemon=True, name="fleet-driver")
    driver.start()
    try:
        while driver.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        stop.set()
        driver.join(timeout=2.0)
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
