"""Serve the SIH26123 fleet dashboard for a real demo or a real URL.

Local judges, a projector, or a container behind $PORT — same server either way:

    python scripts/run_web.py                      # 0.0.0.0:8765, one fleet per visitor
    python scripts/run_web.py --port 8000 --open   # pick the port and open a browser
    python scripts/run_web.py --single             # one shared fleet for everyone
    PORT=8080 python scripts/run_web.py            # platform-provided port (Docker/Render/Fly)

Each visitor gets an independent simulation with its own cookie, so two people
clicking at the same time never fight over one floor. Sessions are capped by
count and idle time.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.dashboard import Dashboard, serve, serve_sessions  # noqa: E402
from simulation.maps import get_map  # noqa: E402


def lan_ip() -> str:
    """Best-effort LAN address so a phone on the same Wi-Fi can open the demo."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.3)
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def human_port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SIH26123 edge-fleet dashboard server")
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"),
                   help="interface to bind (default 0.0.0.0 so a phone or a container can reach it)")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")),
                   help="port to bind (default $PORT or 8765)")
    p.add_argument("--visitors", type=int, default=int(os.environ.get("MAX_SESSIONS", "32")),
                   help="maximum concurrent independent fleets (default 32)")
    p.add_argument("--ttl", type=float, default=float(os.environ.get("SESSION_TTL_S", "3600")),
                   help="idle seconds before a visitor's fleet is recycled (default 3600)")
    p.add_argument("--single", action="store_true",
                   help="one shared fleet instead of one per visitor (best for a projector)")
    p.add_argument("--open", action="store_true", help="open a browser once the server is up")
    return p.parse_args(argv)


def banner(host: str, port: int, args: argparse.Namespace, env_key: str) -> None:
    shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    spec = get_map(env_key)
    print()
    print("SIH26123 — Edge-AI Distributed Fleet Coordination")
    print(f"  floor      : {spec.label}")
    print(f"  coordination: every robot bids over the peer radio; no central dispatcher")
    print(f"  this machine: http://{shown}:{port}/")
    if host in ("0.0.0.0", "::", ""):
        ip = lan_ip()
        if ip != "127.0.0.1":
            print(f"  same Wi-Fi  : http://{ip}:{port}/")
    if args.single:
        print("  sessions    : ONE shared fleet (everyone watches the same run)")
    else:
        print(f"  sessions    : one independent fleet per visitor (max {args.visitors}, idle ttl {args.ttl:.0f}s)")
    print("  stop        : Ctrl+C")
    print()
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.port and not human_port_free("127.0.0.1", args.port):
        print(f"port {args.port} is already in use — try --port {args.port + 1} or stop the other server.")
        return 2

    dash = Dashboard(label="shared")
    if args.single:
        httpd = serve(dash, host=args.host, port=args.port)
    else:
        httpd = serve_sessions(
            lambda sid: Dashboard(label=sid[:6]),
            host=args.host,
            port=args.port,
            ttl_s=args.ttl,
            limit=args.visitors,
        )

    banner(args.host, httpd.server_address[1], args, dash.sim.env)

    if args.open:
        threading.Thread(
            target=lambda: (time.sleep(0.4), webbrowser.open(f"http://127.0.0.1:{httpd.server_address[1]}/")),
            daemon=True,
            name="open-browser",
        ).start()

    started = time.time()
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        httpd.shutdown()
        httpd.server_close()
        store = getattr(httpd, "session_store", None)
        count = len(getattr(store, "_sessions", {})) if store is not None else 1
        uptime = time.time() - started
        print(f"served {count} fleet session(s) over {uptime:.0f}s — dashboard stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
