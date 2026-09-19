/* SIH26123 — Edge-AI Distributed Fleet Coordination.
   Reads /state, drives /action. Nothing on this page is a hardcoded number:
   every figure comes from the live simulation or from results/*.json. */

"use strict";

const $ = (id) => document.getElementById(id);
const canvas = $("grid");
const ctx = canvas.getContext("2d");
const confetti = $("confetti");
const cctx = confetti.getContext("2d");

const ROBOT_COLORS = [
  "#ffd166", "#46d5c8", "#ff8fa3", "#8fbcff",
  "#c4a2ff", "#9ee493", "#f4a261", "#7ae0f0",
];

const SPEED_PRESETS = [0.5, 1, 2, 4, 8, 16];

const HEADLINE_LABELS = {
  all_tasks_complete_tick: "finish tick",
  deadlock_count: "deadlocks",
  stall_count: "stalls",
  stall_handoff_count: "handoffs",
  collision_count: "collisions",
  mean_robot_step_ms: "step ms",
};

const METRIC_LABELS = {
  collision_count: "collisions",
  deadlock_count: "deadlocks",
  stall_count: "stalls",
  stall_handoff_count: "handoffs",
  all_tasks_complete_tick: "finish tick",
  mean_wait_ticks: "mean wait",
  total_distance: "total distance",
  mean_comm_latency_ticks: "comm latency",
  mean_robot_step_ms: "step ms",
};

const S = {
  snap: null,
  inflight: false,
  timer: null,
  hover: null,
  pickMode: null,
  celebrated: false,
  bannerTick: null,
  history: { tasks: [], bad: [], step: [] },
  lastRadioTop: null,
  sparkOffset: 0,
  pollMs: 180,
};

/* ------------------------------------------------------------------ utils */

const esc = (v) => String(v == null ? "" : v)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

function fmt(v, digits = 3) {
  if (v == null || v === "" || Number.isNaN(v)) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return Number.isInteger(n) ? String(n) : n.toFixed(digits);
}

function int(v) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  return String(Math.round(Number(v)));
}

function policyName(p) { return p === "ubpa" ? "UBPA" : "stop-and-wait"; }

function toast(msg, kind = "ok", ms = 3200) {
  const el = document.createElement("div");
  el.className = "toast" + (kind === "bad" ? " bad" : kind === "warn" ? " warn" : "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, ms - 320);
  setTimeout(() => el.remove(), ms);
}

/* ------------------------------------------------------------- networking */

async function getState() {
  if (S.inflight) return;
  S.inflight = true;
  try {
    const res = await fetch("/state", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const snap = await res.json();
    if (!snap || typeof snap !== "object" || !snap.world) throw new Error("bad snapshot");
    S.snap = snap;
    renderAll(snap);
    setStatus(snap);
  } catch (err) {
    setStatus(null, err.message);
  } finally {
    S.inflight = false;
  }
}

async function act(body, quiet) {
  try {
    const res = await fetch("/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({ ok: false, error: "unreadable reply" }));
    if (!data.ok) toast(data.error || "action rejected", "bad");
    else if (!quiet) toast(describeAction(body, data));
    await getState();
    return data;
  } catch (err) {
    toast("request failed: " + err.message, "bad");
    return { ok: false, error: String(err) };
  }
}

function describeAction(body, data) {
  switch (body.action) {
    case "block_aisle": return "aisle blocked at " + (data.cell || []).join(",");
    case "clear_blocks": return "cleared " + data.cleared + " obstacle cell(s)";
    case "toggle": return data.blocked ? "cell " + (data.cell || []).join(",") + " blocked" : "cell " + (data.cell || []).join(",") + " cleared";
    case "pause": return "fleet paused";
    case "resume": return "fleet running";
    case "step": return "advanced to tick " + data.tick;
    case "reset": return "run reset";
    case "policy": return "policy: " + policyName(data.policy);
    case "env": return "floor: " + (data.label || data.env);
    case "robots": return "fleet size: " + data.n_robots + " robots";
    case "speed": return "speed: " + data.speed + "×";
    case "add_task": return "job posted: " + ((data.task && data.task.task_id) || "");
    case "remove_task": return "job " + data.task_id + " removed";
    case "drain": return data.robot_id + " battery → " + fmt(data.battery, 1) + "%";
    case "radio": return "radio: loss " + Math.round((data.drop_prob || 0) * 100) + "% · delay " + data.delay;
    default: return body.action + " ok";
  }
}

function setStatus(snap, error) {
  const el = $("chip-status");
  const b = el.querySelector("b");
  el.classList.remove("live", "paused", "done", "error");
  if (error) { el.classList.add("error"); b.textContent = "offline"; return; }
  if (!snap) { b.textContent = "connecting…"; return; }
  if (snap.complete) { el.classList.add("done"); b.textContent = "complete"; }
  else if (snap.paused) { el.classList.add("paused"); b.textContent = "paused"; }
  else { el.classList.add("live"); b.textContent = "running"; }
  $("btn-pause").textContent = snap.paused ? "Resume" : "Pause";
}

/* ------------------------------------------------------------ poll driver */

function schedulePoll() {
  S.pollMs = !S.snap ? 400 : (S.snap.complete ? 900 : S.snap.paused ? 500 : 180);
  clearTimeout(S.timer);
  S.timer = setTimeout(async () => { await getState(); schedulePoll(); }, S.pollMs);
}

/* ------------------------------------------------------------------ floor */

function geometry(snap) {
  const w = snap.world.width, h = snap.world.height;
  const cell = Math.max(12, Math.min(Math.floor(1180 / w), Math.floor(600 / h)));
  return { w, h, cell };
}

function cellCenter(cell, x, y) {
  return [x * cell + cell / 2, y * cell + cell / 2];
}

function drawFloor(snap) {
  const { w, h, cell } = geometry(snap);
  if (canvas.width !== cell * w) canvas.width = cell * w;
  if (canvas.height !== cell * h) canvas.height = cell * h;

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#080d16";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  const walls = new Set((snap.world.walls || []).map((p) => p.join(",")));
  const blocked = new Set((snap.world.blocked || []).map((p) => p.join(",")));
  const turnouts = new Set((snap.world.turnouts || []).map((p) => p.join(",")));

  // floor tiles
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const key = x + "," + y;
      const px = x * cell, py = y * cell;
      if (walls.has(key)) {
        ctx.fillStyle = "#243049";
        ctx.fillRect(px + 0.5, py + 0.5, cell - 1, cell - 1);
        ctx.fillStyle = "rgba(255,255,255,0.055)";
        ctx.fillRect(px + 0.5, py + 0.5, cell - 1, 2);
      } else if (blocked.has(key)) {
        ctx.fillStyle = "#3a1b23";
        ctx.fillRect(px + 0.5, py + 0.5, cell - 1, cell - 1);
        ctx.strokeStyle = "#ff6b81";
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        for (let d = -cell; d < cell; d += 7) {
          ctx.moveTo(px + Math.max(0, d), py + Math.max(0, -d));
          ctx.lineTo(px + Math.min(cell, cell + d), py + Math.min(cell, cell - d));
        }
        ctx.save();
        ctx.beginPath();
        ctx.rect(px + 0.5, py + 0.5, cell - 1, cell - 1);
        ctx.clip();
        ctx.stroke();
        ctx.restore();
        ctx.strokeStyle = "#ff6b81";
        ctx.strokeRect(px + 1, py + 1, cell - 2, cell - 2);
      } else if (turnouts.has(key)) {
        ctx.fillStyle = "#111d31";
        ctx.fillRect(px + 0.5, py + 0.5, cell - 1, cell - 1);
        ctx.fillStyle = "rgba(106,168,255,0.16)";
        ctx.fillRect(px + cell * 0.3, py + cell * 0.3, cell * 0.4, cell * 0.4);
      } else {
        ctx.fillStyle = "#0c1524";
        ctx.fillRect(px + 0.5, py + 0.5, cell - 1, cell - 1);
      }
    }
  }

  // depot + charger pads
  const pad = (cellXY, fill, ring, glyph) => {
    if (!cellXY) return;
    const [x, y] = cellXY;
    ctx.fillStyle = fill;
    ctx.fillRect(x * cell + 2, y * cell + 2, cell - 4, cell - 4);
    ctx.strokeStyle = ring;
    ctx.lineWidth = 1.6;
    ctx.strokeRect(x * cell + 2, y * cell + 2, cell - 4, cell - 4);
    if (glyph && cell >= 20) {
      ctx.fillStyle = ring;
      ctx.font = `600 ${Math.round(cell * 0.44)}px system-ui, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(glyph, x * cell + cell / 2, y * cell + cell / 2 + 1);
    }
  };
  pad(snap.world.depot, "rgba(74,222,128,0.16)", "#4ade80", "⌂");
  pad(snap.world.charger, "rgba(255,179,71,0.18)", "#ffb347", "⚡");

  // jobs
  for (const t of snap.jobs || []) {
    const done = t.status === "DONE";
    const alpha = done ? 0.26 : 1;
    ctx.globalAlpha = alpha;
    const [px, py] = t.pick;
    const [dx, dy] = t.drop;
    ctx.strokeStyle = "#6aa8ff";
    ctx.lineWidth = 1.8;
    ctx.strokeRect(px * cell + cell * 0.18, py * cell + cell * 0.18, cell * 0.64, cell * 0.64);
    ctx.fillStyle = "#6aa8ff";
    ctx.beginPath();
    ctx.arc(px * cell + cell / 2, py * cell + cell / 2, Math.max(2.2, cell * 0.14), 0, Math.PI * 2);
    ctx.fill();

    ctx.strokeStyle = done ? "#4ade80" : "#ff8fa3";
    ctx.setLineDash(done ? [] : [4, 3]);
    ctx.strokeRect(dx * cell + cell * 0.18, dy * cell + cell * 0.18, cell * 0.64, cell * 0.64);
    ctx.setLineDash([]);
    ctx.fillStyle = done ? "#4ade80" : "#ff8fa3";
    ctx.beginPath();
    ctx.arc(dx * cell + cell / 2, dy * cell + cell / 2, Math.max(2.2, cell * 0.14), 0, Math.PI * 2);
    ctx.fill();
    if (cell >= 26) {
      ctx.fillStyle = "rgba(231,238,252,0.82)";
      ctx.font = `600 ${Math.round(cell * 0.3)}px system-ui, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      if (!done) ctx.fillText(t.task_id, dx * cell + cell / 2, dy * cell + cell / 2 - cell * 0.42);
    }
    ctx.globalAlpha = 1;
  }

  // peer links: a broadcast observed in the last few ticks draws an arc
  const robots = snap.robots || [];
  const posOf = (id) => {
    const r = robots.find((b) => b.id === id);
    return r ? r.pos : null;
  };
  const nowTick = snap.tick;
  const fresh = (snap.radio || []).filter((m) => nowTick - (m.tick || 0) <= 6);
  ctx.save();
  ctx.setLineDash([5, 5]);
  ctx.lineDashOffset = -(S.sparkOffset % 10);
  for (const m of fresh) {
    const from = posOf(m.from_id);
    if (!from) continue;
    for (const other of robots) {
      if (other.id === m.from_id) continue;
      ctx.strokeStyle = "rgba(106,168,255,0.20)";
      ctx.lineWidth = 1.1;
      const [ax, ay] = cellCenter(cell, from[0], from[1]);
      const [bx, by] = cellCenter(cell, other.pos[0], other.pos[1]);
      ctx.beginPath();
      ctx.moveTo(ax, ay);
      ctx.lineTo(bx, by);
      ctx.stroke();
    }
  }
  ctx.restore();

  // robots
  robots.forEach((bot, i) => {
    const color = ROBOT_COLORS[i % ROBOT_COLORS.length];
    const [x, y] = bot.pos;
    const cx = x * cell + cell / 2;
    const cy = y * cell + cell / 2;

    if (Array.isArray(bot.path) && bot.path.length > 1) {
      ctx.strokeStyle = color;
      ctx.globalAlpha = 0.38;
      ctx.lineWidth = Math.max(1.6, cell * 0.09);
      ctx.beginPath();
      (bot.path || []).forEach((p, idx) => {
        const [px, py] = cellCenter(cell, p[0], p[1]);
        if (idx === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      });
      ctx.stroke();
      ctx.globalAlpha = 1;
      const goal = bot.path[bot.path.length - 1];
      const [gx, gy] = cellCenter(cell, goal[0], goal[1]);
      ctx.fillStyle = color;
      ctx.globalAlpha = 0.5;
      ctx.beginPath();
      ctx.arc(gx, gy, Math.max(2, cell * 0.11), 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
    }

    const charging = bot.status === "CHARGING";
    const low = Number(bot.battery) < 25;

    if (low || charging) {
      ctx.strokeStyle = charging ? "rgba(180,140,255,0.85)" : "rgba(255,107,129,0.8)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(cx, cy, cell * 0.46, 0, Math.PI * 2);
      ctx.stroke();
    }

    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(cx, cy, cell * 0.33, 0, Math.PI * 2);
    ctx.fill();

    // battery ring: full circle = 100%
    const pct = Math.max(0, Math.min(100, Number(bot.battery) || 0)) / 100;
    ctx.strokeStyle = "rgba(231,238,252,0.85)";
    ctx.lineWidth = Math.max(1.6, cell * 0.07);
    ctx.beginPath();
    ctx.arc(cx, cy, cell * 0.44, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * pct);
    ctx.stroke();

    if (charging) {
      ctx.fillStyle = "#0a1120";
      ctx.font = `700 ${Math.round(cell * 0.36)}px system-ui, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("⚡", cx, cy + 1);
    } else {
      ctx.fillStyle = "#0a1120";
      ctx.font = `700 ${Math.max(9, Math.round(cell * 0.34))}px system-ui, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(String(bot.id).replace(/^R/, ""), cx, cy + 1);
    }
  });

  S.sparkOffset += 0.6;
}

/* -------------------------------------------------------------- hover tip */

function canvasCell(ev) {
  const s = S.snap;
  if (!s) return null;
  const { w, h, cell } = geometry(s);
  const rect = canvas.getBoundingClientRect();
  const sx = (ev.clientX - rect.left) * (canvas.width / rect.width);
  const sy = (ev.clientY - rect.top) * (canvas.height / rect.height);
  const x = Math.floor(sx / cell), y = Math.floor(sy / cell);
  if (x < 0 || y < 0 || x >= w || y >= h) return null;
  return { x, y, sx, sy };
}

function onMove(ev) {
  const s = S.snap;
  const tip = $("hover-tip");
  const c = canvasCell(ev);
  if (!s || !c) { tip.style.display = "none"; return; }
  const bot = (s.robots || []).find((b) => b.pos[0] === c.x && b.pos[1] === c.y);
  const wrapRect = canvas.parentElement.getBoundingClientRect();
  const left = ev.clientX - wrapRect.left + 14;
  const top = ev.clientY - wrapRect.top + 14;
  tip.style.left = Math.max(0, Math.min(left, wrapRect.width - 250)) + "px";
  tip.style.top = Math.max(0, Math.min(top, wrapRect.height - 80)) + "px";
  if (bot) {
    tip.innerHTML =
      `<b style="color:${ROBOT_COLORS[(s.robots || []).indexOf(bot) % ROBOT_COLORS.length]}">${esc(bot.id)}</b> ` +
      `local state<br>status ${esc(bot.status)} · task ${esc(bot.task || "—")}<br>` +
      `battery ${fmt(bot.battery, 1)}% · UBPA ${fmt(bot.ubpa)}<br>` +
      `priority ${fmt(bot.priority, 2)} · workload ${fmt(bot.workload, 2)}<br>` +
      `plan ${(bot.path || []).length} cells · cell ${c.x},${c.y}`;
  } else {
    const walls = new Set((s.world.walls || []).map((p) => p.join(",")));
    const blocked = new Set((s.world.blocked || []).map((p) => p.join(",")));
    const turnouts = new Set((s.world.turnouts || []).map((p) => p.join(",")));
    const key = c.x + "," + c.y;
    const what = walls.has(key) ? "rack (not walkable)"
      : blocked.has(key) ? "operator obstacle"
      : turnouts.has(key) ? "passing bay"
      : "free floor";
    tip.innerHTML = `cell ${c.x},${c.y} · ${what}<br>click to ${
      blocked.has(key) ? "clear" : walls.has(key) ? "—" : "block"}`;
  }
  tip.style.display = "block";
}

function onClick(ev) {
  const c = canvasCell(ev);
  if (!c) return;
  if (S.pickMode) {
    const mode = S.pickMode;
    S.pickMode = null;
    $(mode === "pick" ? "pick-x" : "drop-x").value = String(c.x);
    $(mode === "pick" ? "pick-y" : "drop-y").value = String(c.y);
    $("pick-hint").innerHTML = `Set <b>${mode}</b> to cell ${c.x},${c.y}. Now add the job.`;
    toast(mode + " set to " + c.x + "," + c.y);
    return;
  }
  const walls = new Set((S.snap.world.walls || []).map((p) => p.join(",")));
  if (walls.has(c.x + "," + c.y)) { toast("that cell is a rack", "warn"); return; }
  act({ action: "toggle", x: c.x, y: c.y }, true);
}

/* ---------------------------------------------------------------- renders */

function renderAll(snap) {
  renderChrome(snap);
  drawFloor(snap);
  renderMeters(snap);
  renderShadow(snap);
  renderMeasured(snap);
  renderControls(snap);
  renderJobs(snap);
  renderCharger(snap);
  renderAuctions(snap);
  renderRadio(snap);
  renderEvents(snap);
  pushHistory(snap);
  drawSparks();
  maybeCelebrate(snap);
}

function renderChrome(snap) {
  $("chip-tick").textContent = "t=" + snap.tick;
  $("chip-hz").textContent = snap.hz + " Hz";
  $("chip-policy").textContent = policyName(snap.policy);
  $("chip-fleet").textContent = snap.n_robots + " robots";
  const srv = snap.server || {};
  $("chip-server").textContent = (srv.session || "s1") + " · up " + fmt(srv.uptime_s, 0) + "s";
  $("map-label").textContent = (snap.map && snap.map.label) || snap.env;
  $("map-blurb").textContent = (snap.map && snap.map.blurb) || "";
  $("footer-left").textContent =
    `tick ${snap.tick} · ${snap.n_robots} robots · policy ${policyName(snap.policy)} · speed ${snap.speed}× · session ${srv.session || "—"}`;
  const rs = snap.radio_stats || {};
  $("footer-right").textContent =
    `peer packets ${rs.delivered || 0} delivered / ${rs.dropped || 0} dropped · ticks served ${srv.ticks_served || 0}`;
  $("legend").innerHTML = [
    ["#243049", "rack"],
    ["#ff6b81", "operator obstacle"],
    ["#111d31", "passing bay"],
    ["#4ade80", "depot"],
    ["#ffb347", "charger"],
    ["#6aa8ff", "job pick"],
    ["#ff8fa3", "job drop"],
    ["#ffd166", "robot (" + (snap.robots || []).length + ")"],
  ].map(([c, t]) => `<span><i style="background:${c}"></i>${t}</span>`).join("");
}

function renderMeters(snap) {
  const m = snap.metrics || {};
  const cards = [
    ["tick", int(snap.tick), ""],
    ["jobs done", `${m.tasks_done}/${m.tasks_total}`, m.complete ? "good" : ""],
    ["collisions", int(m.collision_count), m.collision_count > 0 ? "bad" : "good"],
    ["deadlocks", int(m.deadlock_count), m.deadlock_count > 0 ? "bad" : "good"],
    ["stalls", int(m.stall_count), m.stall_count > 0 ? "warn" : "good"],
    ["stall handoffs", int(m.stall_handoff_count), ""],
    ["mean wait", fmt(m.mean_wait_ticks, 2), "warn"],
    ["total distance", fmt(m.total_distance, 1), ""],
    ["comm latency", fmt(m.mean_comm_latency_ticks, 2) + " t", ""],
    ["step ms", fmt(m.mean_robot_step_ms, 2), ""],
    ["finish tick", m.all_tasks_complete_tick == null ? "running" : String(m.all_tasks_complete_tick), ""],
    ["ticks elapsed", int(m.ticks), ""],
  ];
  $("live-metrics").innerHTML = cards.map(([k, v, cls]) =>
    `<div class="meter ${cls}"><span class="k">${esc(k)}</span><b>${esc(v)}</b></div>`).join("");
}

function pushHistory(snap) {
  const m = snap.metrics || {};
  const push = (arr, v) => { arr.push(Number(v) || 0); if (arr.length > 180) arr.shift(); };
  push(S.history.tasks, m.tasks_done);
  push(S.history.bad, (m.collision_count || 0) + (m.deadlock_count || 0) + (m.stall_count || 0));
  push(S.history.step, m.mean_robot_step_ms);
}

function drawSpark(id, series, color) {
  const cv = $(id);
  const c = cv.getContext("2d");
  const w = cv.width, h = cv.height;
  c.clearRect(0, 0, w, h);
  const data = series.slice(-w);
  if (data.length < 2) return;
  const max = Math.max(...data, 1e-6);
  const min = Math.min(...data, 0);
  const span = Math.max(max - min, 1e-6);
  const px = (i) => (i / (data.length - 1)) * (w - 2) + 1;
  const py = (v) => h - 3 - ((v - min) / span) * (h - 8);
  c.beginPath();
  data.forEach((v, i) => { i ? c.lineTo(px(i), py(v)) : c.moveTo(px(i), py(v)); });
  c.strokeStyle = color;
  c.lineWidth = 1.6;
  c.stroke();
  c.lineTo(px(data.length - 1), h - 1);
  c.lineTo(px(0), h - 1);
  c.closePath();
  const grad = c.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, color + "44");
  grad.addColorStop(1, color + "00");
  c.fillStyle = grad;
  c.fill();
  c.fillStyle = "rgba(231,238,252,0.75)";
  c.font = "10px ui-monospace, monospace";
  c.textAlign = "right";
  c.fillText(String(max.toFixed(Number.isInteger(max) ? 0 : 1)), w - 4, 11);
}

function drawSparks() {
  drawSpark("spark-tasks", S.history.tasks, "#46d5c8");
  drawSpark("spark-bad", S.history.bad, "#ff6b81");
  drawSpark("spark-step", S.history.step, "#6aa8ff");
}

function renderShadow(snap) {
  const shadow = snap.shadow;
  const m = snap.metrics || {};
  const body = $("shadow-body");
  if (!shadow) {
    $("shadow-lag").textContent = "twin unavailable";
    body.innerHTML = `<p class="sub">No shadow twin reported by the server for this session.</p>`;
    return;
  }
  if (shadow.error) {
    $("shadow-lag").textContent = "twin error";
    body.innerHTML = `<p class="sub">Shadow twin stopped: ${esc(shadow.error)}</p>`;
    return;
  }
  $("shadow-lag").textContent = "lag " + shadow.lag_ticks + " ticks";
  const row = (label, a, b, lowerWins) => {
    let cls = "";
    if (typeof a === "number" && typeof b === "number" && a !== b) {
      const aWins = lowerWins ? a < b : a > b;
      cls = aWins ? "lead" : "trail";
    }
    return `<div class="row"><span>${esc(label)}</span><span class="${cls}">${esc(fmt(a, 2))} vs ${esc(fmt(b, 2))}</span></div>`;
  };
  const liveDone = m.tasks_done || 0, shadowDone = shadow.tasks_done || 0;
  const pct = (a, b) => b ? Math.round((a / b) * 100) : 0;
  body.innerHTML = `
    <div class="twin live">
      <h3>live · ${esc(policyName(snap.policy))}</h3>
      <div class="big">${liveDone}/${m.tasks_total} jobs</div>
      <div class="row"><span>tick</span><span>${snap.tick}</span></div>
      <div class="row"><span>finish tick</span><span>${m.all_tasks_complete_tick == null ? "running" : m.all_tasks_complete_tick}</span></div>
      <div class="row"><span>collisions</span><span>${m.collision_count}</span></div>
      <div class="row"><span>deadlocks</span><span>${m.deadlock_count}</span></div>
      <div class="row"><span>stalls</span><span>${m.stall_count}</span></div>
      <div class="job-bar" style="width:${pct(liveDone, m.tasks_total)}%"></div>
    </div>
    <div class="twin shadow">
      <h3>twin · ${esc(policyName(shadow.policy))}</h3>
      <div class="big">${shadowDone}/${shadow.tasks_total} jobs</div>
      <div class="row"><span>tick</span><span>${shadow.tick}</span></div>
      <div class="row"><span>finish tick</span><span>${shadow.finish_tick == null ? "running" : shadow.finish_tick}</span></div>
      <div class="row"><span>collisions</span><span>${shadow.collision_count}</span></div>
      <div class="row"><span>deadlocks</span><span>${shadow.deadlock_count}</span></div>
      <div class="row"><span>stalls</span><span>${shadow.stall_count}</span></div>
      <div class="job-bar" style="width:${pct(shadowDone, shadow.tasks_total)}%"></div>
    </div>
    <div style="grid-column:1/-1">
      ${row("jobs done (higher is ahead)", liveDone, shadowDone, false)}
      ${row("collisions (lower is better)", m.collision_count || 0, shadow.collision_count || 0, true)}
      ${row("deadlocks (lower is better)", m.deadlock_count || 0, shadow.deadlock_count || 0, true)}
      ${row("stalls (lower is better)", m.stall_count || 0, shadow.stall_count || 0, true)}
      ${row("mean wait ticks (lower is better)", m.mean_wait_ticks || 0, shadow.mean_wait_ticks || 0, true)}
    </div>`;
}

function fingerprintChip(name, fp) {
  if (!fp) return "";
  const ok = fp.exists;
  return `<span class="pill ${ok ? "" : "open"}" title="${esc(fp.path || "")}">${esc(name)} ${
    ok ? esc(fp.sha256_16 || "") + " · " + (fp.bytes || 0) + "B" : "missing"}</span>`;
}

function renderMeasured(snap) {
  const md = snap.measured;
  const box = $("measured-body");
  if (!md) {
    box.innerHTML = `<p class="sub">No measured artifacts reported.</p>`;
    return;
  }
  $("measured-src").textContent = (md.files && md.files.metrics && md.files.metrics.path) || "results/*.json";
  const files = Object.entries(md.files || {}).map(([n, fp]) => fingerprintChip(n, fp)).join(" ");
  const exps = md.experiments || {};
  const ids = Object.keys(exps);

  const head = `<p class="sub">${esc(md.provenance || "")} <span class="src">source mtime ${esc(md.source_mtime || "—")}</span></p>
    <div class="btn-row small">${files}</div>`;

  if (!ids.length) {
    box.innerHTML = head + `<p class="sub">metrics.json has no experiment summaries yet. Press Re-measure to build them here.</p>`;
  } else {
    const table = ids.map((id) => {
      const exp = exps[id] || {};
      const rows = Object.keys(HEADLINE_LABELS).map((k) => {
        const b = (exp.baseline || {})[k];
        const u = (exp.ubpa || {})[k];
        const imp = (exp.improvement || {})[k];
        const pct = imp == null ? "—" : (imp * 100 >= 0 ? "+" : "") + (imp * 100).toFixed(1) + "%";
        const cls = imp == null ? "" : imp > 0 ? "win" : imp < 0 ? "lose" : "";
        return `<tr><td>${esc(HEADLINE_LABELS[k])}</td><td class="num">${esc(fmt(b, 2))}</td><td class="num">${esc(fmt(u, 2))}</td><td class="num ${cls}">${esc(pct)}</td></tr>`;
      }).join("");
      return `<div style="margin-bottom:10px">
        <p class="field-label">${esc(id)} · seeds ${esc(exp.seeds == null ? "—" : exp.seeds)} · ${
          exp.comparable ? "comparable (both finished)" : "baseline incomplete → % withheld"}</p>
        <table><thead><tr><th>metric</th><th class="num">stop-and-wait</th><th class="num">UBPA</th><th class="num">Δ</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
    }).join("");
    box.innerHTML = head + table;
  }

  const fresh = md.fresh || {};
  const freshFile = fresh.file || {};
  const log = $("remeasure-log");
  const delta = fresh.delta || {};
  const dIds = Object.keys(delta);
  if (!freshFile.exists || !dIds.length) {
    log.innerHTML = `<div class="t">no re-measurement on this machine yet — press the button to rebuild E1/E2/E3 locally</div>`;
  } else {
    log.innerHTML = dIds.map((id) => {
      const d = delta[id];
      const tk = (d.timing_keys || []).map((k) => HEADLINE_LABELS[k] || k);
      const head2 = `<div><span class="ok">${esc(id)}</span> <span class="t">overall ${
        d.match === true ? "match" : d.match === false ? "MISMATCH" : "n/a"}</span>${
        tk.length ? ' <span class="warn">(wall-clock excluded: ' + esc(tk.join(", ")) + ")</span>" : ""}</div>`;
      const rows = Object.entries(d.metrics || {}).map(([k, v]) => {
        const tag = v.timing ? '<span class="warn">timing</span>'
          : v.match === true ? '<span class="ok">identical</span>'
          : v.match === false ? '<span class="bad">DIFFERS</span>' : '<span class="t">n/a</span>';
        return `<div>&nbsp;&nbsp;${esc(HEADLINE_LABELS[k] || k)}: shipped ${esc(fmt(v.shipped, 2))} · now ${esc(fmt(v.remeasured, 2))} ${tag}</div>`;
      }).join("");
      return head2 + rows;
    }).join("");
  }

  const rm = snap.remeasure || {};
  const st = $("remeasure-status");
  const running = rm.status === "running";
  $("btn-remeasure").disabled = running;
  $("btn-remeasure").textContent = running ? "Re-measuring…" : "Re-measure on this machine";
  if (running) {
    st.textContent = `running since ${rm.started || "?"} · writing ${rm.out || ""}`;
  } else if (rm.status === "done" || rm.status === "error") {
    st.textContent = `${rm.status} · exit ${rm.exit_code} · ${rm.seconds == null ? "?" : fmt(rm.seconds, 1)}s · out ${rm.out || ""}`;
  } else {
    st.textContent = "idle";
  }
  if (Array.isArray(rm.log) && rm.log.length) {
    const tail = rm.log.slice(-6).map((l) =>
      `<div>${esc(String(l).replace(/^\[stdout\]|\[stderr\]/, (m) => m))}</div>`).join("");
    $("remeasure-log").innerHTML += `<div style="margin-top:6px" class="t">— re-measure log —</div>${tail}`;
  }
}

function button(label, on, handler, cls) {
  const b = document.createElement("button");
  b.textContent = label;
  b.className = (cls || "") + (on ? " on" : "");
  b.onclick = handler;
  return b;
}

function renderControls(snap) {
  const envs = $("env-buttons");
  if (envs.dataset.sig !== String((snap.envs || []).length)) {
    envs.innerHTML = "";
    (snap.envs || []).forEach((e) => {
      const b = button(e.label, false, () => act({ action: "env", env: e.key }), "ghost");
      b.title = e.blurb || "";
      b.dataset.key = e.key;
      envs.appendChild(b);
    });
    envs.dataset.sig = String((snap.envs || []).length);
  }
  [...envs.children].forEach((b) => b.classList.toggle("on", b.dataset.key === snap.env));

  const robots = $("robot-buttons");
  if (robots.dataset.sig !== "n") {
    robots.innerHTML = "";
    [3, 4, 5, 6, 7, 8].forEach((n) => robots.appendChild(button(String(n) + " robots", false, () => act({ action: "robots", n }), "ghost")));
    robots.dataset.sig = "n";
  }
  [...robots.children].forEach((b, i) => b.classList.toggle("on", i + 3 === snap.n_robots));
  $("robot-count-label").textContent = snap.n_robots;
  const rr = $("robot-range");
  if (document.activeElement !== rr) rr.value = String(snap.n_robots);
  rr.oninput = () => { $("robot-count-label").textContent = rr.value; };
  rr.onchange = () => act({ action: "robots", n: Number(rr.value) });

  const speeds = $("speed-buttons");
  const computed = (snap.speed_steps && snap.speed_steps.length ? snap.speed_steps : SPEED_PRESETS);
  const sig = computed.join(",");
  if (speeds.dataset.sig !== sig) {
    speeds.innerHTML = "";
    computed.forEach((v) => speeds.appendChild(button(v + "×", false, () => act({ action: "speed", speed: v }), "ghost")));
    speeds.dataset.sig = sig;
  }
  [...speeds.children].forEach((b) => b.classList.toggle("on", Number(b.textContent.replace("×", "")) === Number(snap.speed)));
  $("speed-label").textContent = snap.speed + "×";
  const sr = $("speed-range");
  if (document.activeElement !== sr) sr.value = String(snap.speed);
  sr.oninput = () => { $("speed-label").textContent = sr.value + "×"; };
  sr.onchange = () => act({ action: "speed", speed: Number(sr.value) });

  const pol = $("policy-buttons");
  pol.innerHTML = "";
  pol.appendChild(button("UBPA (ours)", snap.policy === "ubpa", () => act({ action: "policy", policy: "ubpa" }), "ghost"));
  pol.appendChild(button("stop-and-wait", snap.policy === "stop_wait", () => act({ action: "policy", policy: "stop_wait" }), "ghost"));

  const rs = snap.radio_stats || {};
  const dropPct = Math.round((rs.drop_prob || 0) * 100);
  const delay = rs.delay == null ? 1 : rs.delay;
  const ds = $("drop-slider"), dl = $("delay-slider");
  if (document.activeElement !== ds) ds.value = String(dropPct);
  if (document.activeElement !== dl) dl.value = String(delay);
  $("drop-label").textContent = dropPct + "%";
  $("delay-label").textContent = delay + (delay === 1 ? " tick" : " ticks");
  $("radio-stats").textContent =
    `delivered ${rs.delivered || 0} · dropped ${rs.dropped || 0} · mean latency ${fmt(rs.mean_latency, 2)} ticks`;

  // default new-job cells from the current map
  const px = $("pick-x"), py = $("pick-y"), dx = $("drop-x"), dy = $("drop-y");
  if (px.value === "" && snap.world && snap.world.depot) {
    px.value = snap.world.depot[0]; py.value = snap.world.depot[1];
    const firstDrop = (snap.jobs || [])[0];
    if (firstDrop) { dx.value = firstDrop.drop[0]; dy.value = firstDrop.drop[1]; }
    else { dx.value = snap.world.charger[0]; dy.value = snap.world.charger[1]; }
  }
}

function renderJobs(snap) {
  const jobs = snap.jobs || [];
  const done = jobs.filter((j) => j.status === "DONE").length;
  $("job-count").textContent = `${done}/${jobs.length} complete`;
  const rows = jobs.map((j) => {
    const cls = j.status === "DONE" ? "done" : j.holder ? "active" : "open";
    const urgent = Math.max(0, Math.min(1, Number(j.urgency) || 0));
    return `<tr>
      <td class="mono">${esc(j.task_id)}</td>
      <td class="mono">${esc(j.pick.join(","))} → ${esc(j.drop.join(","))}</td>
      <td><span class="job-bar" style="width:${(urgent * 44).toFixed(0)}px"></span> <span class="src">${fmt(j.urgency, 2)}</span></td>
      <td><span class="pill ${cls}">${esc(j.status.toLowerCase())}</span></td>
      <td class="mono">${esc(j.holder || "—")}</td>
      <td class="num">${j.complete_tick == null ? "—" : j.complete_tick}</td>
      <td><button class="tiny danger" data-remove="${esc(j.task_id)}">✕</button></td>
    </tr>`;
  }).join("");
  $("jobs").innerHTML = `<table>
    <thead><tr><th>job</th><th>pick → drop</th><th>urgency</th><th>state</th><th>holder</th><th class="num">done t</th><th></th></tr></thead>
    <tbody>${rows || `<tr><td colspan="7" class="sub">no jobs</td></tr>`}</tbody></table>`;
  $("jobs").querySelectorAll("[data-remove]").forEach((b) => {
    b.onclick = () => act({ action: "remove_task", task_id: b.dataset.remove });
  });
}

function renderCharger(snap) {
  const ch = snap.charger || {};
  const charging = ch.charging || [];
  const cellTxt = (ch.cell || []).join(",");
  $("charger").innerHTML =
    `<div class="charge-slot"><span>charger cell</span><span class="mono">${esc(cellTxt)}</span></div>
     <div class="charge-slot"><span>docked now</span><span class="mono">${charging.length ? esc(charging.join(", ")) : "none"}</span></div>
     <div class="charge-slot"><span>reserve policy</span><span class="mono">robot-local</span></div>`;

  const robots = snap.robots || [];
  $("batteries").innerHTML = robots.map((b, i) => {
    const bat = Math.max(0, Math.min(100, Number(b.battery) || 0));
    const cls = bat < 25 ? "low" : bat < 55 ? "mid" : "";
    return `<div class="battery">
      <span class="id" style="color:${ROBOT_COLORS[i % ROBOT_COLORS.length]}">${esc(b.id)}</span>
      <span class="track"><span class="fill ${cls}" style="width:${bat.toFixed(1)}%"></span></span>
      <span class="pct">${bat.toFixed(1)}%</span>
      <button class="tiny ghost" data-drain="${esc(b.id)}" title="Force this robot below reserve">drain</button>
    </div>
    <div class="src" style="margin:-2px 0 4px 38px">${esc(b.status)} · ${esc(b.task || "no job")} · UBPA ${fmt(b.ubpa)}</div>`;
  }).join("");
  $("batteries").querySelectorAll("[data-drain]").forEach((b) => {
    b.onclick = () => act({ action: "drain", robot_id: b.dataset.drain, level: 14 });
  });
}

function renderAuctions(snap) {
  const auc = snap.auctions || [];
  if (!auc.length) {
    $("auctions").innerHTML = `<p class="sub">No auction open at tick ${snap.tick} — every robot is currently holding or moving on its own plan.</p>`;
    return;
  }
  const rows = auc.map((a) => `<tr>
      <td class="mono">${esc(a.task_id)}</td>
      <td class="mono">${esc(a.viewer)}</td>
      <td class="num">${a.opened_tick}</td>
      <td class="num">${a.valid_bids}/${a.bids}</td>
      <td class="mono">${esc(a.best_bidder || "—")}</td>
      <td class="num">${fmt(a.best_cost, 2)}</td>
      <td><span class="pill ${a.closed ? "done" : "open"}">${a.closed ? "closed" : "open"}</span></td>
    </tr>`).join("");
  $("auctions").innerHTML = `<table>
    <thead><tr><th>job</th><th>broker</th><th class="num">t0</th><th class="num">bids</th><th>best</th><th class="num">cost</th><th>state</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function renderRadio(snap) {
  const log = snap.radio || [];
  const now = snap.tick;
  const recent = log.filter((m) => now - (m.tick || 0) <= 60).length;
  const head = `<div class="src" style="margin-bottom:5px">peer packets last 60 ticks: <b>${recent}</b> · messages from a central scheduler: <b>0</b></div>`;
  const rows = log.slice(0, 60).map((m) => {
    const kind = (m.type || "").replace(/^MSG_/, "").toLowerCase();
    const cls = m.type === "MSG_BID" || m.type === "MSG_AWARD" ? "ok" : m.type === "MSG_INTENT" ? "warn" : "";
    return `<div><span class="t">t${esc(m.tick)}</span> <span class="from">${esc(m.from_id)}</span> ` +
      `<span class="kind ${cls}">${esc(kind)}</span> ${esc(m.task_id || "")} ${esc(m.status || "")}</div>`;
  }).join("");
  $("radio").innerHTML = head + (rows || `<div class="t">no packets yet</div>`);
}

function renderEvents(snap) {
  const kindClass = { collide: "bad", deadlock: "bad", stall: "warn", handoff: "warn", unblock: "ok", block: "warn", charge: "warn", task: "ok", done: "ok" };
  const rows = (snap.events || []).slice().reverse().map((e) =>
    `<div><span class="t">t${esc(e.tick)}</span> <span class="from">${esc(e.robot)}</span> ` +
    `<span class="kind ${kindClass[e.kind] || ""}">${esc(e.kind)}</span> ${esc(e.detail || "")}</div>`).join("");
  $("events").innerHTML = rows || `<div class="t">no events yet</div>`;
}

/* ------------------------------------------------------------ celebration */

function maybeCelebrate(snap) {
  const banner = $("complete-banner");
  if (snap.complete && !S.celebrated) {
    S.celebrated = true;
    const m = snap.metrics || {};
    banner.innerHTML = `All <b>${esc(m.tasks_total)}</b> jobs done at tick <b>${esc(m.all_tasks_complete_tick)}</b> — ` +
      `${esc(m.collision_count)} collisions, ${esc(m.deadlock_count)} deadlocks, ${esc(m.stall_count)} stalls. ` +
      `The twin is still working.`;
    banner.classList.remove("hidden");
    burstConfetti(2600);
    toast("fleet complete — the shadow twin is still going", "ok", 4200);
  }
  if (!snap.complete && S.celebrated) {
    S.celebrated = false;
    banner.classList.add("hidden");
  }
  if (!snap.complete) banner.classList.add("hidden");
}

function burstConfetti(ms) {
  confetti.classList.add("on");
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  confetti.width = window.innerWidth * dpr;
  confetti.height = window.innerHeight * dpr;
  cctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const parts = Array.from({ length: 150 }, () => ({
    x: Math.random() * window.innerWidth,
    y: -Math.random() * window.innerHeight * 0.5,
    vx: (Math.random() - 0.5) * 1.6,
    vy: 1.6 + Math.random() * 2.6,
    s: 3 + Math.random() * 5,
    c: ROBOT_COLORS[Math.floor(Math.random() * ROBOT_COLORS.length)],
    r: Math.random() * Math.PI,
  }));
  const t0 = performance.now();
  (function frame(t) {
    const elapsed = t - t0;
    cctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
    for (const p of parts) {
      p.x += p.vx; p.y += p.vy; p.r += 0.05;
      cctx.save();
      cctx.translate(p.x, p.y);
      cctx.rotate(p.r);
      cctx.fillStyle = p.c;
      cctx.globalAlpha = Math.max(0, 1 - elapsed / ms);
      cctx.fillRect(-p.s / 2, -p.s / 2, p.s, p.s * 0.6);
      cctx.restore();
    }
    if (elapsed < ms) requestAnimationFrame(frame);
    else { cctx.clearRect(0, 0, window.innerWidth, window.innerHeight); confetti.classList.remove("on"); }
  })(t0);
}

/* --------------------------------------------------------------- controls */

$("btn-pause").onclick = () => act({ action: S.snap && S.snap.paused ? "resume" : "pause" });
$("btn-step").onclick = () => act({ action: "step" });
$("btn-reset").onclick = () => { S.celebrated = false; act({ action: "reset" }); };
$("btn-block").onclick = () => act({ action: "block_aisle" });
$("btn-clear").onclick = () => act({ action: "clear_blocks" });
$("btn-remeasure").onclick = async () => {
  const r = await act({ action: "remeasure" }, true);
  toast(r.message || "re-measure requested", r.ok ? "ok" : "bad");
};
$("btn-forecast").onclick = async () => {
  const btn = $("btn-forecast");
  btn.disabled = true;
  btn.textContent = "Forecasting…";
  const r = await act({ action: "forecast" }, true);
  btn.disabled = false;
  btn.textContent = "Forecast ETA";
  const f = r && r.forecast;
  if (!f) { $("forecast-body").innerHTML = `<span class="bad">forecast unavailable</span>`; return; }
  $("forecast-body").innerHTML =
    `from t${f.from_tick} → finish <b>${f.finish_tick == null ? "not within horizon" : f.finish_tick}</b><br>` +
    `eta ${f.eta_ticks == null ? "—" : f.eta_ticks + " ticks"} (${f.eta_seconds == null ? "—" : f.eta_seconds + "s at " + (S.snap ? S.snap.speed : 1) + "×"})<br>` +
    `jobs ${f.tasks_done}/${f.tasks_total} · collisions ${f.collisions} · deadlocks ${f.deadlocks}<br>` +
    `<span class="src">clone-and-run took ${f.compute_ms} ms · horizon ${f.horizon_ticks} ticks · live sim untouched</span>`;
};
$("drop-slider").onchange = () => act({ action: "radio", drop_prob: Number($("drop-slider").value) / 100, delay: Number($("delay-slider").value) });
$("delay-slider").onchange = () => act({ action: "radio", drop_prob: Number($("drop-slider").value) / 100, delay: Number($("delay-slider").value) });
$("btn-add-task").onclick = () => act({
  action: "add_task",
  pick: [Number($("pick-x").value), Number($("pick-y").value)],
  drop: [Number($("drop-x").value), Number($("drop-y").value)],
  urgency: Number($("urgency").value),
});
$("btn-pick-map").onclick = () => { S.pickMode = "pick"; $("pick-hint").innerHTML = "Click a free cell on the floor to set the <b>pick</b> point."; };
$("btn-drop-map").onclick = () => { S.pickMode = "drop"; $("pick-hint").innerHTML = "Click a free cell on the floor to set the <b>drop</b> point."; };

canvas.addEventListener("mousemove", onMove);
canvas.addEventListener("mouseleave", () => { $("hover-tip").style.display = "none"; });
canvas.addEventListener("click", onClick);
window.addEventListener("resize", () => { if (S.snap) drawFloor(S.snap); });

document.addEventListener("keydown", (ev) => {
  const tag = (ev.target && ev.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  const key = ev.key.toLowerCase();
  if (key === " " || ev.code === "Space") { ev.preventDefault(); $("btn-pause").click(); }
  else if (key === "s") $("btn-step").click();
  else if (key === "b") $("btn-block").click();
  else if (key === "r") $("btn-reset").click();
  else if (key === "f") $("btn-forecast").click();
});

getState().then(schedulePoll);
