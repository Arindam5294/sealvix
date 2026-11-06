"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const stepLogs = {};
const stepExit = {};
let es = null;
let lastVerify = null;
let receiptHash = null;

const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const finePointer = window.matchMedia && window.matchMedia("(hover: hover) and (pointer: fine)").matches;

const LABELS = {
  runtime: "Runtime", network_during_tests: "Network (tests)", root_filesystem: "Root FS", user: "User",
  cpus: "CPUs", memory: "Memory", pids_limit: "PIDs", wall_clock_s: "Time limit (s)",
  capabilities: "Capabilities", secrets_inside: "Secrets inside", lifecycle: "Lifecycle",
};
const STAGES = ["intake", "install", "baseline", "fix", "report"];

function esc(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

/* ---------------- config ---------------- */
async function loadConfig() {
  try {
    const r = await fetch("/api/config");
    const c = await r.json();
    $("#profile").innerHTML = Object.entries(LABELS)
      .map(([k, l]) => `<li><span>${l}</span><span>${esc(c.containment[k])}</span></li>`).join("");
    const iso = $("#iso");
    if (c.containment.isolated) { iso.textContent = "isolated · " + c.containment.runtime; iso.className = "chip good"; }
    else { iso.textContent = "NOT isolated · dev mode"; iso.className = "chip warn"; }
    $("#demos").innerHTML = c.demos.map((d) => `<button type="button" data-d="${esc(d)}"><span class="spark" aria-hidden="true">◆</span>${esc(d)}</button>`).join("");
    $("#demos").querySelectorAll("button").forEach((b) => b.onclick = () => start("demo:" + b.dataset.d));
  } catch (e) {
    $("#err").textContent = "Could not load config: " + e.message;
  }
}

/* ---------------- pipeline stepper ---------------- */
function stageEl(s) { return document.querySelector(`#pipeline li[data-stage="${s}"]`); }
function setStage(s, state, note) {
  const el = stageEl(s); if (!el) return;
  el.classList.remove("active", "done", "fail", "skip");
  if (state) el.classList.add(state);
  if (note != null) el.querySelector(".note").textContent = note;
  updateProgress();
}
function activate(s, note) {
  const idx = STAGES.indexOf(s);
  STAGES.forEach((x, i) => {
    const el = stageEl(x);
    if (i < idx && !el.classList.contains("done") && !el.classList.contains("fail") && !el.classList.contains("skip")) setStage(x, "done");
  });
  setStage(s, "active", note);
}
function updateProgress() {
  let last = -1;
  STAGES.forEach((x, i) => { const el = stageEl(x); if (el.classList.contains("done") || el.classList.contains("skip") || el.classList.contains("fail") || el.classList.contains("active")) last = i; });
  const doneish = STAGES.filter((x) => stageEl(x).classList.contains("done") || stageEl(x).classList.contains("skip")).length;
  const p = last < 0 ? 0 : (stageEl(STAGES[last]).classList.contains("active") ? last + 0.5 : last) / (STAGES.length - 1);
  $("#pipeline").style.setProperty("--progress", Math.min(1, Math.max(0, doneish === STAGES.length ? 1 : p)));
}
const DEFAULT_NOTES = ["clone · detect", "sandboxed deps", "reproduce failure", "agent loop", "diff + receipt"];
function resetPipeline() { STAGES.forEach((s, i) => setStage(s, null, DEFAULT_NOTES[i])); }
function currentStage() { return STAGES.find((s) => stageEl(s).classList.contains("active")); }

/* ---------------- streaming text (typewriter) ---------------- */
const streams = new WeakMap();
function streamText(el, text) {
  const prev = streams.get(el); if (prev) cancelAnimationFrame(prev);
  text = text || "";
  if (reduceMotion || text.length < 2) { el.textContent = text; el.scrollTop = el.scrollHeight; return; }
  el.textContent = "";
  el.classList.add("typing");
  const per = Math.max(24, Math.ceil(text.length / 70)); // ~1.2s max regardless of size
  let i = 0;
  const tick = () => {
    i = Math.min(text.length, i + per);
    el.textContent = text.slice(0, i);
    el.scrollTop = el.scrollHeight;
    if (i < text.length) streams.set(el, requestAnimationFrame(tick));
    else { el.classList.remove("typing"); streams.delete(el); }
  };
  streams.set(el, requestAnimationFrame(tick));
}

/* ---------------- run state ---------------- */
function reset() {
  $("#timeline").innerHTML = ""; $("#log").textContent = ""; $("#logstep").textContent = ""; $("#logtabs").innerHTML = "";
  $("#diff").textContent = "No changes yet."; $("#diff").className = "diff muted"; $("#diffstat").textContent = "";
  $("#receipt").textContent = "…"; $("#receipt").className = "receipt muted"; $("#err").textContent = ""; $("#destroyed").textContent = "0";
  $("#receiptHash").textContent = "pending"; $("#copyHash").disabled = true; $("#seal").className = "seal"; receiptHash = null;
  $("#before").textContent = "Waiting for baseline…"; $("#before").className = "log muted"; $("#beforeExit").textContent = "–"; $("#beforeExit").className = "exit";
  $("#after").textContent = "Waiting for verification…"; $("#after").className = "log muted"; $("#afterExit").textContent = "–"; $("#afterExit").className = "exit";
  document.querySelectorAll(".badge").forEach((b) => { b.classList.remove("on", "fire"); const s = b.querySelector(".b-s"); if (s) s.textContent = "armed"; });
  for (const k in stepLogs) delete stepLogs[k];
  for (const k in stepExit) delete stepExit[k];
  lastVerify = null;
  resetPipeline();
}

function setStatus(s) {
  const el = $("#status"); el.textContent = String(s).replace(/_/g, " "); el.className = "status " + s;
  document.body.dataset.state = s;
}

function add(html, cls = "", step = null) {
  const tl = $("#timeline");
  const empty = tl.querySelector(".empty"); if (empty) empty.remove();
  const li = document.createElement("li");
  li.innerHTML = html; if (cls) li.className = cls;
  if (step) { li.classList.add("step"); li.tabIndex = 0; li.onclick = () => showLog(step); li.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); showLog(step); } }; }
  tl.appendChild(li);
  tl.scrollTop = tl.scrollHeight;
}

function showLog(step) {
  streamText($("#log"), stepLogs[step] || "");
  $("#logstep").textContent = "· " + step;
  $$("#logtabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.step === step ? "true" : "false"));
}

function addTab(step, ok) {
  let b = document.querySelector(`#logtabs button[data-step="${CSS.escape(step)}"]`);
  if (!b) {
    b = document.createElement("button"); b.type = "button"; b.dataset.step = step; b.setAttribute("role", "tab");
    b.onclick = () => showLog(step);
    $("#logtabs").appendChild(b);
  }
  b.className = ok ? "ok" : "bad";
  b.innerHTML = `<i aria-hidden="true"></i>${esc(step)}`;
}

function setPane(which, step) {
  const pre = $("#" + which), ex = $("#" + which + "Exit");
  const code = stepExit[step];
  pre.className = "log";
  streamText(pre, stepLogs[step] || "");
  ex.textContent = `${step} · exit ${code}`;
  ex.className = "exit " + (code === 0 ? "ok" : "bad");
}

function renderDiff(d) {
  if (!d) return;
  $("#diff").className = "diff";
  let a = 0, r = 0, files = 0;
  $("#diff").innerHTML = d.split("\n").map((l, i) => {
    const c = l.startsWith("+++") || l.startsWith("---") || l.startsWith("@@") || l.startsWith("diff") ? "hdr"
      : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : "";
    if (l.startsWith("diff")) files++; else if (c === "add") a++; else if (c === "del") r++;
    return `<span class="${c}" style="--i:${Math.min(i, 60)}">${esc(l)}</span>`;
  }).join("\n");
  $("#diffstat").innerHTML = `${files} file${files === 1 ? "" : "s"} · <span class="ok">+${a}</span> <span class="bad">−${r}</span>`;
}

function fireBadge(control) {
  const b = document.querySelector(`.badge[data-k="${control}"]`); if (!b) return;
  b.classList.add("on");
  const s = b.querySelector(".b-s"); if (s) s.textContent = "fired";
  b.classList.remove("fire"); void b.offsetWidth; b.classList.add("fire");
}

function bumpDestroyed() {
  const el = $("#destroyed");
  el.textContent = +el.textContent + 1;
  el.classList.remove("bump"); void el.offsetWidth; el.classList.add("bump");
}

function showReceipt(rc) {
  $("#receipt").className = "receipt";
  $("#receipt").textContent = JSON.stringify(rc, null, 2);
  receiptHash = rc && rc.receipt_sha256;
  if (receiptHash) {
    $("#copyHash").disabled = false;
    $("#seal").className = "seal sealed";
    const el = $("#receiptHash");
    if (reduceMotion) { el.textContent = receiptHash; return; }
    // hex scramble resolving into the real hash
    const hex = "0123456789abcdef"; let n = 0;
    const iv = setInterval(() => {
      n += 2;
      el.textContent = receiptHash.split("").map((ch, i) => i < n ? ch : hex[(Math.random() * 16) | 0]).join("");
      if (n >= receiptHash.length) { clearInterval(iv); el.textContent = receiptHash; }
    }, 16);
  }
}

function onEvent(ev) {
  switch (ev.type) {
    case "status": setStatus(ev.status); if (ev.status === "running") activate("intake"); break;
    case "phase": {
      add(`<b>${esc(ev.phase)}</b> <span class="mut">${esc(ev.detail || (ev.stack ? ev.stack + " · " + ev.test : ""))}</span>`, "phase");
      if (ev.phase === "clone") activate("intake", "cloning");
      else if (ev.phase === "detect") activate("intake", "stack: " + (ev.stack || "?"));
      else if (ev.phase === "install") activate("install", "network on · no secrets");
      else if (ev.phase === "baseline") activate("baseline", "network none");
      else if (ev.phase === "agent") activate("fix", "diagnosing…");
      break;
    }
    case "step": {
      stepLogs[ev.step] = ev.output; stepExit[ev.step] = ev.exit_code; showLog(ev.step);
      const ok = ev.exit_code === 0;
      addTab(ev.step, ok);
      add(`<span class="${ok ? "ok" : "bad"}">${ok ? "✔" : "✖"}</span> ${esc(ev.step)} · exit ${ev.exit_code} · ${ev.duration_s}s · net ${esc(ev.network)}`, "", ev.step);
      if (ev.step === "install") setStage("install", ok ? "done" : "fail", ok ? `ok · ${ev.duration_s}s` : `exit ${ev.exit_code}`);
      else if (ev.step === "baseline") {
        setStage("baseline", "done", ok ? "already green" : `red · exit ${ev.exit_code}`);
        setPane("before", "baseline");
        if (ok) { lastVerify = "baseline"; setPane("after", "baseline"); }
      } else if (/^verify-/.test(ev.step)) {
        lastVerify = ev.step; setPane("after", ev.step);
        if (ok) setStage("fix", "done", `${ev.step} green`);
        else activate("fix", `${ev.step} still red · patching`);
      }
      break;
    }
    case "containment": {
      fireBadge(ev.control);
      add(`<span class="ok">🛡</span> contained in ${esc(ev.step)}: ${esc(ev.control.replace(/_/g, " "))}`, "contain"); break;
    }
    case "destroyed": bumpDestroyed(); break;
    case "edit": add(`✎ edited <code>${esc(ev.path)}</code>`, "edit"); if (currentStage() === "fix") setStage("fix", "active", "patched " + ev.path.split("/").pop()); break;
    case "agent": add(`<span class="mut">agent: ${esc(ev.message)}</span>`, "agent"); break;
    case "error": {
      $("#err").textContent = ev.message;
      const cur = currentStage() || "intake"; setStage(cur, "fail", "error");
      add(`<span class="bad">✖</span> ${esc(ev.message)}`, "error");
      break;
    }
    case "receipt": showReceipt(ev.receipt); activate("report", "sealing receipt"); break;
    case "done": {
      setStatus(ev.status); renderDiff(ev.diff); $("#f button").disabled = false;
      if ($("#receipt").textContent === "…") $("#receipt").textContent = "No receipt (run did not reach the baseline).";
      finishPipeline(ev.status);
      if (es) es.close();
      break;
    }
  }
}

function finishPipeline(status) {
  if (status === "green" || status === "already_green") {
    STAGES.forEach((s) => { const el = stageEl(s); if (!el.classList.contains("done")) setStage(s, s === "fix" && status === "already_green" ? "skip" : "done", s === "fix" && status === "already_green" ? "nothing to fix" : null); });
    setStage("report", "done", "receipt sealed");
    celebrate();
  } else if (status === "failed" || status === "awaiting_agent") {
    setStage("fix", "fail", status === "failed" ? "could not verify a fix" : "agent unavailable");
    if (receiptHash) setStage("report", "done", "receipt sealed");
  } else if (status === "error") {
    const cur = currentStage(); if (cur) setStage(cur, "fail", "error");
    if (receiptHash) setStage("report", "done", "receipt sealed");
  }
}

/* ---------------- job start / resume ---------------- */
function watch(id, fallback) {
  if (es) es.close();
  let got = false;
  es = new EventSource(`/api/jobs/${id}/events`);
  es.onmessage = (m) => { got = true; onEvent(JSON.parse(m.data)); };
  es.onerror = () => {
    if (!got && fallback) { es.close(); fallback(); }
  };
}

async function start(source) {
  reset(); setStatus("queued"); $("#f button").disabled = true;
  document.querySelector(".pipeline-card").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
  let r, j;
  try {
    r = await fetch("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, branch: $("#branch").value.trim() || null }) });
    j = await r.json().catch(() => ({}));
  } catch (e) { r = { ok: false }; j = { detail: "network error: " + e.message }; }
  if (!r.ok) { $("#err").textContent = (typeof j.detail === "string" ? j.detail : "request failed"); setStatus("error"); $("#f button").disabled = false; return; }
  history.replaceState(null, "", "#" + j.id);
  watch(j.id);
}

// Opening a link with #<job id> replays a live job, or shows a saved receipt.
async function resume() {
  const id = location.hash.slice(1);
  if (!/^[a-f0-9]{6,32}$/.test(id)) return;
  const r = await fetch(`/api/jobs/${id}`).catch(() => null);
  if (!r || !r.ok) return;
  const job = await r.json();
  reset(); setStatus(job.status || "queued");
  watch(id, () => {
    // Not in memory any more: render the saved record.
    add(`<span class="mut">saved record for ${esc(job.source || id)}</span>`);
    if (job.receipt) showReceipt(job.receipt);
    (job.steps || []).forEach((s) => add(`<span class="${s.exit_code === 0 ? "ok" : "bad"}">${s.exit_code === 0 ? "✔" : "✖"}</span> ${esc(s.step)} · exit ${s.exit_code}`));
    if (job.error) $("#err").textContent = job.error;
    renderDiff(job.diff); setStatus(job.status); finishPipeline(job.status);
  });
}

$("#f").onsubmit = (e) => { e.preventDefault(); const s = $("#src").value.trim(); if (s) start(s); };
$("#copyHash").onclick = async () => {
  if (!receiptHash) return;
  try { await navigator.clipboard.writeText(receiptHash); $("#copyHash").textContent = "Copied ✓"; }
  catch { $("#copyHash").textContent = "Select & copy"; }
  setTimeout(() => { $("#copyHash").textContent = "Copy SHA-256"; }, 1600);
};

/* ---------------- effects ---------------- */
function celebrate() {
  document.body.classList.remove("win"); void document.body.offsetWidth; document.body.classList.add("win");
  if (reduceMotion) return;
  const c = $("#fx"), ctx = c.getContext("2d");
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  c.width = innerWidth * dpr; c.height = innerHeight * dpr; ctx.scale(dpr, dpr);
  const colors = ["#3ddc84", "#4cc9f0", "#8b5cf6", "#f0f4ff", "#ffd166"];
  const parts = Array.from({ length: 160 }, () => ({
    x: innerWidth / 2 + (Math.random() - 0.5) * 200, y: innerHeight * 0.35,
    vx: (Math.random() - 0.5) * 14, vy: -Math.random() * 14 - 4, s: 4 + Math.random() * 6,
    r: Math.random() * 6, vr: (Math.random() - 0.5) * 0.3, col: colors[(Math.random() * colors.length) | 0],
  }));
  const t0 = performance.now();
  const frame = (t) => {
    const el = t - t0;
    ctx.clearRect(0, 0, innerWidth, innerHeight);
    ctx.globalAlpha = Math.max(0, 1 - el / 2800);
    for (const p of parts) {
      p.vy += 0.32; p.vx *= 0.99; p.x += p.vx; p.y += p.vy; p.r += p.vr;
      ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.r); ctx.fillStyle = p.col;
      ctx.fillRect(-p.s / 2, -p.s / 4, p.s, p.s / 2); ctx.restore();
    }
    if (el < 2800) requestAnimationFrame(frame); else ctx.clearRect(0, 0, innerWidth, innerHeight);
  };
  requestAnimationFrame(frame);
}

function initTilt() {
  if (reduceMotion || !finePointer) return;
  $$(".tilt").forEach((el) => {
    el.addEventListener("pointermove", (e) => {
      const r = el.getBoundingClientRect();
      const x = (e.clientX - r.left) / r.width - 0.5, y = (e.clientY - r.top) / r.height - 0.5;
      el.style.setProperty("--rx", (-y * 5).toFixed(2) + "deg");
      el.style.setProperty("--ry", (x * 6).toFixed(2) + "deg");
      el.style.setProperty("--mx", ((x + 0.5) * 100).toFixed(1) + "%");
      el.style.setProperty("--my", ((y + 0.5) * 100).toFixed(1) + "%");
    });
    el.addEventListener("pointerleave", () => { el.style.setProperty("--rx", "0deg"); el.style.setProperty("--ry", "0deg"); });
  });
}

function initReveal() {
  const els = $$(".reveal");
  if (reduceMotion || !("IntersectionObserver" in window)) { els.forEach((e) => e.classList.add("in")); return; }
  const io = new IntersectionObserver((ents) => ents.forEach((en) => { if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); } }), { threshold: 0.08 });
  els.forEach((e) => io.observe(e));
}

// Lightweight 3D perspective grid + drifting particles (2D canvas, pauses when hidden).
function initBackground() {
  const c = $("#bg"); if (!c || !c.getContext) return;
  const ctx = c.getContext("2d");
  let w, h, dpr;
  const dots = Array.from({ length: 70 }, () => ({ x: Math.random(), y: Math.random(), z: Math.random(), v: 0.0002 + Math.random() * 0.0006 }));
  const resize = () => { dpr = Math.min(2, window.devicePixelRatio || 1); w = innerWidth; h = innerHeight; c.width = w * dpr; c.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); };
  resize(); addEventListener("resize", resize);
  let off = 0, running = true;
  const draw = () => {
    ctx.clearRect(0, 0, w, h);
    const st = document.body.dataset.state;
    const speed = st === "running" || st === "queued" ? 0.012 : 0.003;
    const hue = st === "green" || st === "already_green" ? "61,220,132" : st === "failed" || st === "error" ? "255,92,122" : "76,201,240";
    off = (off + speed) % 1;
    const horizon = h * 0.62, cx = w / 2;
    // receding horizontal lines
    for (let i = 0; i < 18; i++) {
      const t = (i + off) / 18, y = horizon + Math.pow(t, 2.2) * (h - horizon + 40);
      ctx.strokeStyle = `rgba(${hue},${0.03 + t * 0.14})`; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }
    // converging verticals
    for (let i = -16; i <= 16; i++) {
      ctx.strokeStyle = `rgba(${hue},0.07)`;
      ctx.beginPath(); ctx.moveTo(cx + i * 14, horizon); ctx.lineTo(cx + i * w * 0.14, h + 40); ctx.stroke();
    }
    const g = ctx.createLinearGradient(0, horizon - 60, 0, horizon + 60);
    g.addColorStop(0, "rgba(5,6,11,0)"); g.addColorStop(0.5, `rgba(${hue},0.10)`); g.addColorStop(1, "rgba(5,6,11,0)");
    ctx.fillStyle = g; ctx.fillRect(0, horizon - 60, w, 120);
    for (const d of dots) {
      d.y -= d.v * (1 + speed * 60); if (d.y < 0) { d.y = 1; d.x = Math.random(); }
      ctx.fillStyle = `rgba(${hue},${0.15 + d.z * 0.5})`;
      ctx.beginPath(); ctx.arc(d.x * w, d.y * h, 0.6 + d.z * 1.6, 0, 6.283); ctx.fill();
    }
    if (running && !reduceMotion) requestAnimationFrame(draw);
  };
  document.addEventListener("visibilitychange", () => { const was = running; running = !document.hidden; if (running && !was) requestAnimationFrame(draw); });
  draw();
}

initBackground();
initTilt();
initReveal();
loadConfig().then(resume);
