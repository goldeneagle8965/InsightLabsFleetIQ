# 05 — Raspberry Pi deployment plan

Target: the existing robot decision stack running as **three independent processes on three Raspberry
Pi nodes**, coordinating directly over UDP, against a world simulated on the host PC.

---

## 1. Bill of materials

| Qty | Item | Notes |
| --- | --- | --- |
| 3 | Raspberry Pi 5 (4 GB) *or* Pi 4 Model B (4 GB) | a Pi Zero 2 W would also carry the compute (see §6) but has no wired NIC |
| 3 | microSD A2 32 GB | the agent is ~40 kB of Python; capacity is for the OS |
| 3 | Official PSU: 5 V/5 A USB-C (Pi 5) or 5 V/3 A (Pi 4) | under-voltage throttling shows up as `step_ms` spikes |
| 3 | Active cooler / heatsink case | the demo runs a closed loop for minutes; passive cooling is enough at 8 Hz but thermal margin is cheap |
| 1 | 8-port Gigabit switch | wired is preferred: it makes loss/jitter an honest measurement |
| 4 | Cat5e/6 patch cables | 3× Pi + host |
| 1 | Host PC | runs `run_edge_host.py` + the existing dashboard |
| 3 | micro-HDMI→HDMI (optional) | only for first boot / diagnostics |
| 3 | LED + 330 Ω resistor (optional) | per-node "alive / holding / negotiating" indicator |
| — | 5 GHz Wi-Fi AP (fallback) | only if wiring the room is impossible |

Cost envelope: Pi 5 4 GB ≈ ₹5,000-6,000 each in India, Pi 4 4 GB ≈ ₹4,500, so a 3-node wired demo lands
around ₹20,000-25,000 including PSUs, cards, switch and cables. No GPU, no accelerators, no sensors are
needed — the load is a 125 ms control loop, not inference.

---

## 2. Network plan

| Node | Hostname | IPv4 | Agent port |
| --- | --- | --- | --- |
| Host PC | `host` | 192.168.50.10 | host `9100/udp`, discovery `9101/udp` |
| Pi 1 | `pi-r1` | 192.168.50.11 | 9201/udp |
| Pi 2 | `pi-r2` | 192.168.50.12 | 9202/udp |
| Pi 3 | `pi-r3` | 192.168.50.13 | 9203/udp |

Static addressing on the demo LAN, no DHCP (a lease change mid-demo is the classic silent failure).
Raspberry Pi OS *Bookworm* uses NetworkManager:

```bash
# on each Pi, adjust the last octet; wlan0 if you are on Wi-Fi
sudo nmcli con mod "Wired connection 1" ipv4.method manual \
  ipv4.addresses 192.168.50.11/24 ipv4.gateway 192.168.50.10 ipv4.dns "1.1.1.1"
sudo nmcli con up "Wired connection 1"
```

Bookworm also supports the older file, if `dhcpcd` is present:

```
# /etc/dhcpcd.conf
interface eth0
static ip_address=192.168.50.11/24
static routers=192.168.50.10
```

Firewall (only needed if `ufw` is enabled; stock Raspberry Pi OS is open):

```bash
sudo ufw allow 9201:9210/udp     # agent sockets (peers + host control)
sudo ufw allow from 192.168.50.10 to any port 9201:9210 proto udp
```

Name resolution is optional — the peer table carries IP:port pairs explicitly, so the demo works even
with no mDNS/DNS.

---

## 3. Node provisioning

Raspberry Pi OS **Lite (64-bit, Bookworm)** on each card, then:

```bash
sudo hostnamectl set-hostname pi-r1
sudo apt update && sudo apt full-upgrade -y          # one-time; do not do this 5 minutes before judging
sudo apt install -y python3                     # already present; asserts version
python3 -V                                      # expect 3.11.x
sudo timedatectl set-ntp true                   # wall-clock only for logs; the protocol uses logical ticks
```

Notes that matter:

- **No pip installs.** The agent is stdlib-only (`socket`, `json`, `time`, `selectors`, `argparse`).
  Test `T10` fails the suite if a third-party import appears in `simulation/edge/`.
- **No ROS, no OpenCV, no GPIO library required.** GPIO LEDs are optional and imported lazily.

Copy the code (the agent needs `simulation/edge/` and the parts of `simulation/` it imports):

```bash
# from the host
scp -r sih26123/simulation pi@192.168.50.11:/home/pi/sih26123/
scp -r sih26123/scripts    pi@192.168.50.11:/home/pi/sih26123/
ssh pi@192.168.50.11 'cd /home/pi/sih26123 && python -c "import simulation.edge.agent"'
```

Verify the deployment is reproducible by diffing a manifest, not by eyeballing directories:

```bash
# host - write the manifest once, then verify every copy against it
python scripts/check_edge_deploy.py --manifest edge_manifest.sha256 --roots simulation scripts
# on the Pi (same format as sha256sum, so no Python needed)
sha256sum -c edge_manifest.sha256
```

The manifest is a build artefact: regenerate it with `--update` after any code change rather than
committing a stale copy.

---

## 4. Running the fleet

**Host PC (world + spectator + existing dashboard):**

```bash
python scripts/run_edge_host.py \
  --agents R1=192.168.50.11:9201,R2=192.168.50.12:9202,R3=192.168.50.13:9203 \
  --tick-hz 8 --seed 1 --env warehouse \
  --udp 0.0.0.0:9100 --advertise 192.168.50.10 --serve 127.0.0.1:8765
```

**Each Pi (same command, different id):**

```bash
python scripts/run_edge_agent.py --id R1 --host 192.168.50.10:9100 --listen 0.0.0.0:9201 \
  --peers R2=192.168.50.12:9202,R3=192.168.50.13:9203 --policy ubpa
```

As a service on each Pi, `/etc/systemd/system/edge-agent@.service`:

```ini
[Unit]
Description=SIH26123 edge agent %i
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/sih26123
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/etc/edge-agent.env
ExecStart=/usr/bin/python3 scripts/run_edge_agent.py \
  --id %i --host ${HOST_ADDR} --listen 0.0.0.0:${LISTEN_PORT} \
  --peers ${PEER_TABLE} --policy ubpa
Restart=always
RestartSec=2
WatchdogSec=10
Nice=-5
LimitNOFILE=4096
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
# /etc/edge-agent.env, per node
HOST_ADDR=192.168.50.10:9100
LISTEN_PORT=9201
PEER_TABLE=R2=192.168.50.12:9202,R3=192.168.50.13:9203

sudo systemctl daemon-reload
sudo systemctl enable --now edge-agent@R1
systemctl status edge-agent@R1 --no-pager
journalctl -u edge-agent@R1 -f
```

Start order: host first (it can run with zero agents connected — every robot just holds), then the Pis in
any order. Agents retry `HELLO` every 8 ticks until answered, so order never matters.

---

## 5. Pre-demo checklist (5 minutes, run it every time)

| # | Check | Command | Expected |
| --- | --- | --- | --- |
| 1 | Reachability | `ping -c1 192.168.50.1{1,2,3}` | 3 replies, < 2 ms |
| 2 | Ports listening | `ss -lunp \| grep 920` on each Pi | one socket per node |
| 3 | Host sees all three | host log | `3/3 agents HELLO` |
| 4 | Ticks advancing | dashboard or host log | tick climbs at ~8/s |
| 5 | No under-voltage | `vcgencmd get_throttled` on each Pi | `throttled=0x0` |
| 6 | Loss is low | host snapshot `link.lost_pct` | < 1% wired |
| 7 | Agents are deciding | host snapshot `peer_last` | `BID`/`CLAIM`/`YIELD_*` present, `best_bidder` shown **only** as display |
| 8 | Offline fallback ready | `RUN_DEMO.bat` | the in-process demo still starts if the LAN misbehaves |

---

## 6. Resource budget

| Resource | Per agent | Basis |
| --- | --- | --- |
| CPU | ~1.5 ms per 125 ms tick (≈1.2% of one core), worst observed step 2-4 ms | the same `Robot.step` measures ~0.15 ms mean on the PC (`mean_robot_step_ms` in `results/metrics.json`); a Pi 5 core is roughly an order of magnitude slower |
| RAM | ~35-45 MB RSS (interpreter + dicts) | 3 agents ≈ 130 MB total, trivial on 4 GB |
| Disk | ~50 MB for the code tree | plus OS |
| Network | ~15 kB/s fleet-wide | doc 03 §9 |
| Power | ~3-5 W idle-ish per Pi 5, ~7 W peak | official PSU headroom |

The tick budget is 125 ms and the decision costs ~1.5 ms, so there is roughly **80× headroom**: the edge
node could run a learned policy or a full 3-D planner later without changing the harness. That headroom
is the honest answer to "why is this Edge-AI?" — the architecture is sized for real per-robot inference,
and today it runs the existing heuristic policy.

Thermal behaviour: at 8 Hz the Pis idle; `HEARTBEAT.step_ms_p95` rising above 60 ms, or
`vcgencmd get_throttled` non-zero, means the node is throttling (usually a weak PSU or a closed case).

---

## 7. Optional physical layer (demo only, still no motor control on the world)

| Option | Wiring | Effect |
| --- | --- | --- |
| Status LED | GPIO 17 → 330 Ω → LED → GND | solid = deciding, blink = holding, fast blink = degraded (host silent) |
| Negotiation LED | GPIO 27 | blinks on `YIELD_QUERY`/`ACK`/`NACK` — makes peer-to-peer traffic visible from across the room |
| Buzzer | GPIO 22 | short beep on task claim, long on completion |
| E-stop button | GPIO 23 → GND (pull-up) | sends `SHUTDOWN` to the agent process; the world simply holds that robot |

The GPIO layer is deliberately outside the decision loop: it reads robot state and never writes it, so
it cannot influence a route or a winner. No servos, no drive motors, no battery packs — this deployment
demonstrates *distributed coordination*, not locomotion, and the pack says so rather than implying
otherwise.

---

## 8. Rollback and safety

- **Instant rollback:** `RUN_DEMO.bat` / `python scripts/run_demo.py` still runs the complete in-process
  simulation offline; if the Wi-Fi acts up on stage, the demo continues without the Pis.
- **Nothing on the Pis writes to the repo.** The agent opens no files for writing; `results/metrics.json`
  cannot be touched from a node.
- **Kill switch:** `systemctl stop edge-agent@R1` (or the E-stop button) removes a robot from the world
  without stopping the run — the host holds its last position and the fleet re-plans around it.
- **No irreversible host operations** are required by this plan: no `apt upgrade` is mandatory, no
  firewall rule is mandatory, and no data outside `/home/pi/sih26123` is modified.

---

## 9. Demo-day runbook (3 minutes)

| t | Action | What the audience sees |
| --- | --- | --- |
| 0:00 | Start host, show dashboard; start the three Pis | tick advancing, three agents `HELLO` in the host log; `peer_last` filling with `PEER_STATE` |
| 0:30 | Point at the message log | `BID`/`CLAIM` appearing with no dispatcher in the path — direct `R2→R1` unicast |
| 0:50 | Head-on conflict on the main aisle | `YIELD_QUERY` → `NACK` → one robot pulls into a turnout; both UBPA scores visible in the log |
| 1:20 | Operator blocks an aisle (existing `block_aisle` control) | one robot senses the cell, broadcasts `OBSTACLE`, both re-plan; the dashboard shows the same thing it always did |
| 1:50 | Set radio loss to 40%, then 70% (existing `radio` control) | claims expire and re-auction; completion still happens, just later — counter to the stop-and-wait alternative on the same map |
| 2:20 | `systemctl stop edge-agent@R3` | fleet keeps working, task hand-back after repeated stalls, no central recovery anywhere |
| 2:45 | Compare the numbers | edge run vs `results/metrics.json` baseline; state clearly that the shipped benchmark file is unchanged and that the edge path is measured separately |

---

## 10. Implementation status (read before promising anything on stage)

This plan was written before the package was built. What the shipped code actually does:

| Step above | Status |
| --- | --- |
| 1. `scp` the tree to a Pi, import the agent | **works** — the edge package is stdlib-only and imports cleanly on a fresh interpreter |
| 2. Run the agent process on its own Pi against the host on the PC | **design target, not wired** — `scripts/run_edge_agent.py` implements the full client side (bind, `HELLO`, adopt `HELLO_WORLD`, unicast `ACT`), but the shipped `EdgeHost` drives its own in-process agents and has no path that accepts a *remote* agent. Run `--offline` on a Pi to prove the decision stack runs there with no host at all, or `run_edge_host.py` on the PC for a working run |
| 3. Drive the fleet `edge-lan` (host + agents over UDP on one PC) | **works** — `python scripts/run_edge_host.py --agents R1=127.0.0.1:9201,...` |
| 4. Dashboard sees the edge run | **works** — `--serve` attaches the existing HTTP handler to the host snapshot (the dashboard-owned blocks are filled by a presentation-only adapter) |
| 5. `systemctl stop edge-agent@R3` removes a robot mid-run | **proven in-process** — `EdgeHost.silent` plus the death test (`T5`) show the fleet finishing with a node gone. Neither the shutdown path nor `edge_agent.py` is packaged here; neither is a decision, so neither changes the safety argument |

What this means for the pitch: the *claim* is "robots decide for themselves, directly, over a peer medium". That
claim is fully demonstrated by `edge-loopback`/`edge-lan` and the test suite. The literal three-Pi wiring is the
remaining engineering step, and it is honest to say so: an agent that runs on a Pi and a host that can admit it
are both required, and only the first exists.
