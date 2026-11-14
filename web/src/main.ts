// Steady live dashboard. Reads only from steadyd (/api, /ws/live); never talks to the band.
import { drawSpectrum, drawTrend, type Series } from "./charts.js";
import type { BandEvent, BandStatus, ConversationTurn, Episode, LiveMessage, WindowRow } from "./types.js";

const HISTORY = 90; // 90 windows x 2 s = 3 minutes
const $ = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;

const state = {
  windows: [] as WindowRow[],
  status: null as BandStatus | null,
  events: [] as BandEvent[],
  episodes: [] as Episode[],
  logged: 0,
};

const FLAG_NAMES = ["rest_tremor", "action_tremor", "dyskinesia", "freeze", "fall_suspected", "hard_impact"];
const flagNames = (w: WindowRow): string[] =>
  w.detections ?? FLAG_NAMES.filter((_, bit) => (w.flags >> bit) & 1);

const fmt = (v: number | null | undefined, digits = 1): string =>
  v === null || v === undefined || Number.isNaN(v) ? "–" : v.toFixed(digits);
const clock = (t: number): string => new Date(t * 1000).toLocaleTimeString();

function pill(label: string, value: string, cls = ""): HTMLElement {
  const el = document.createElement("span");
  el.className = `pill ${cls}`;
  el.append(`${label} `);
  const b = document.createElement("b");
  b.textContent = value;
  el.append(b);
  return el;
}

function renderPills(): void {
  const s = state.status;
  const last = state.windows.at(-1);
  const pills: HTMLElement[] = [];
  if (s) {
    pills.push(pill("band", s.connected ? (s.name ?? "connected") : s.reconnecting ? "reconnecting" : "offline",
      s.connected ? "" : "bad"));
    pills.push(pill("link", s.transport ?? "–"));
    if (s.battery !== null) {
      pills.push(pill("battery", `${s.battery}%${s.charging ? " ⚡" : ""}`, s.battery < 20 && !s.charging ? "warn" : ""));
    }
    if (s.rssi !== null) pills.push(pill("signal", `${s.rssi} dBm`));
  }
  if (last) {
    pills.push(pill("data age", `${fmt(last.data_age_s, 2)} s`, (last.data_age_s ?? 9) > 1 ? "warn" : ""));
    pills.push(pill("lost", `${last.lost_samples}`, last.lost_samples > 0 ? "warn" : ""));
    pills.push(pill("compute", `${fmt(last.compute_ms)} ms`));
  }
  $("pills").replaceChildren(...pills);
  $("live-dot").classList.toggle("on", Boolean(s?.connected));
}

function renderCards(w: WindowRow): void {
  const ctx = $("ctx");
  ctx.textContent = w.context.replace("_", " ");
  ctx.className = `big ctx-${w.context}`;
  $("ctx-sub").textContent = w.valid
    ? `${w.activity_state ?? "–"} · cadence ${fmt(w.cadence, 0)} · coverage ${(w.valid_frac * 100).toFixed(0)}%`
    : "window not valid (no or partial data)";
  $("amp").textContent = fmt(w.tremor_amp, 2);
  $("freq").textContent = w.dom_freq ? `${w.dom_freq.toFixed(2)} Hz` : "–";
  $("freq-sub").textContent = `peak prominence ${fmt(w.peak_prom, 1)} · rest ratio ${fmt(w.rest_ratio, 2)}`;
  $("move").textContent = `${fmt(w.gyro_rms, 1)} · ${w.acc_dyn_rms != null ? (w.acc_dyn_rms * 1000).toFixed(0) : "–"}`;
}

const CHECK_LABELS: Record<string, string> = {
  context: "arm resting / in use", rhythmic: "one steady rhythm", strong_enough: "strong enough",
  not_violent: "not a vigorous action", rest_band: "3.5–7.5 Hz", action_band: "4–12 Hz",
};

function renderDetection(w: WindowRow): void {
  const found = w.detections ?? [];
  const badge = $("detect");
  badge.textContent = found.length ? found.join(", ").replaceAll("_", " ") : w.valid ? "nothing detected" : "no data";
  badge.className = `badge ${found.length ? "pos" : ""}`;
  $("checks").replaceChildren(...Object.entries(w.checks ?? {}).map(([k, ok]) => {
    const el = document.createElement("span");
    el.className = `check ${ok ? "ok" : ""}`;
    el.textContent = `${ok ? "✓" : "✗"} ${CHECK_LABELS[k] ?? k}`;
    return el;
  }));
}

function renderEpisodes(): void {
  const rows = state.episodes.slice(0, 15).map((e) => {
    const tr = document.createElement("tr");
    const m = e.metrics ?? {};
    const detail = e.kind.includes("tremor")
      ? `${m["freq_mean_hz"] ?? "–"} Hz · peak ${m["amp_peak_dps"] ?? "–"} °/s · ${m["grade"] ?? ""}`
      : `impact ${m["impact_g"] ?? "–"} g · turned ${m["orientation_change_deg"] ?? "–"}° · ${m["still"] ? "still after" : "moving after"}`;
    const lasted = e.ended_at === null ? "ongoing" : `${fmt(e.duration_s, 0)} s`;
    [clock(e.started_at), e.kind.replaceAll("_", " "), e.severity, lasted, detail].forEach((text, i) => {
      const td = document.createElement("td");
      td.textContent = text;
      if (i === 2) td.className = `sev-${e.severity}`;
      tr.append(td);
    });
    return tr;
  });
  $("episodes").replaceChildren(...rows);
  $("ep-count").textContent = state.episodes.length ? `${state.episodes.length} recent` : "none yet";
}

function upsertEpisode(e: Episode): void {
  const i = state.episodes.findIndex((x) => x.id === e.id);
  if (i >= 0) state.episodes[i] = e;
  else state.episodes.unshift(e);
  renderEpisodes();
}

function renderCharts(): void {
  const last = state.windows.at(-1);
  if (last?.spectrum) drawSpectrum($("spectrum"), last.spectrum.psd, last.spectrum.df, last.dom_freq ?? null);
  const pick = (k: keyof WindowRow): (number | null)[] =>
    state.windows.map((w) => (w.valid ? ((w[k] as number | null | undefined) ?? null) : null));
  const series: Series[] = [
    { label: "tremor amplitude °/s", colour: "#d29922", values: pick("tremor_amp") },
    { label: "gyro RMS °/s", colour: "#58a6ff", values: pick("gyro_rms") },
    { label: "voluntary (0.5–3 Hz) °/s", colour: "#bc8cff", values: pick("vol_rms") },
  ];
  drawTrend($("trend"), series);
  $("legend").replaceChildren(...series.map((s) => {
    const el = document.createElement("span");
    const i = document.createElement("i");
    i.style.background = s.colour;
    el.append(i, s.label);
    return el;
  }));
}

function renderTable(): void {
  const rows = state.windows.slice(-12).reverse().map((w) => {
    const tr = document.createElement("tr");
    const cells = [clock(w.t_end), w.context, w.valid ? "yes" : "no", fmt(w.dom_freq, 2), fmt(w.tremor_amp, 2),
      flagNames(w).join(", ").replaceAll("_", " ") || "–"];
    if (w.flags) tr.className = "pos";
    cells.forEach((text, i) => {
      const td = document.createElement("td");
      td.textContent = text;
      if (i === 1) td.className = `ctx-${w.context}`;
      tr.append(td);
    });
    return tr;
  });
  $("rows").replaceChildren(...rows);
  $("logged").textContent = `${state.logged} this session`;
}

function renderEvents(): void {
  const items = state.events.slice(0, 30).map((e) => {
    const li = document.createElement("li");
    const b = document.createElement("b");
    b.textContent = e.kind;
    li.append(b, ` · ${clock(e.time)}`);
    return li;
  });
  $("events").replaceChildren(...items);
}

let interimLine: HTMLElement | null = null;

function renderTurn(t: ConversationTurn): void {
  const box = $("transcript");
  if (!t.final) {                       // a guess at what is being said: one line, updated in place
    if (!interimLine) {
      interimLine = document.createElement("div");
      interimLine.className = "line user interim";
      box.append(interimLine);
    }
    interimLine.textContent = t.text;
  } else {
    interimLine?.remove();
    interimLine = null;
    const line = document.createElement("div");
    line.className = `line ${t.role}`;
    line.textContent = t.text;
    if (t.latency_ms != null || t.interrupted) {
      const small = document.createElement("small");
      small.textContent = `${t.latency_ms != null ? `${t.latency_ms.toFixed(0)} ms` : ""}${t.interrupted ? " · interrupted" : ""}`;
      line.append(small);
    }
    box.append(line);
  }
  box.scrollTop = box.scrollHeight;
}

function onMessage(msg: LiveMessage): void {
  if (msg.topic === "conversation.state") {
    $("convo-state").textContent = msg.data.state;
    if (msg.data.state === "opening") $("transcript").replaceChildren();
    return;
  }
  if (msg.topic === "conversation.turn") {
    renderTurn(msg.data);
    return;
  }
  if (msg.topic === "episode.started" || msg.topic === "episode.escalated" || msg.topic === "episode.ended") {
    upsertEpisode(msg.data);
    return;
  }
  if (msg.topic === "window") {
    renderDetection(msg.data);
    state.windows.push(msg.data);
    if (state.windows.length > HISTORY) state.windows.shift();
    state.logged += 1;
    renderCards(msg.data);
    renderCharts();
    renderTable();
  } else if (msg.topic === "band.status") {
    state.status = msg.data;
  } else if (msg.topic === "band.event") {
    state.events.unshift(msg.data);
    renderEvents();
  }
  renderPills();
}

async function backfill(): Promise<void> {
  try {
    const since = Date.now() / 1000 - HISTORY * 2;
    const res = await fetch(`/api/windows?since=${since}&limit=${HISTORY}`);
    if (res.ok) state.windows = ((await res.json()) as WindowRow[]).slice(-HISTORY);
    const ev = await fetch("/api/events?limit=30");
    if (ev.ok) state.events = ((await ev.json()) as { t: number; kind: string; arg: number | null }[])
      .map((e) => ({ kind: e.kind, time: e.t, arg: e.arg }));
    const ep = await fetch("/api/episodes?limit=15");
    if (ep.ok) state.episodes = (await ep.json()) as Episode[];
    renderEvents();
    renderEpisodes();
    renderCharts();
  } catch {
    /* live data will fill in */
  }
}

function connect(): void {
  const ws = new WebSocket(`ws://${location.host}/ws/live`);
  ws.onopen = () => ($("foot").textContent = "Live from steadyd · updates every 2 s");
  ws.onmessage = (ev) => onMessage(JSON.parse(String(ev.data)) as LiveMessage);
  ws.onclose = () => {
    $("foot").textContent = "steadyd is not reachable — retrying…";
    $("live-dot").classList.remove("on");
    setTimeout(connect, 2000);
  };
}

window.addEventListener("resize", renderCharts);
void backfill().then(connect);
