"use strict";

const API = window.API_BASE || "";
const app = document.getElementById("app");
const note = document.getElementById("topbar-note");

const STATUS_CLASS = { "Healthy": "ok", "Needs attention": "warn", "Underperforming": "bad", "No data": "none" };
const ISSUE_LABEL = { sudden_drop: "Sudden drop", gradual_decline: "Gradual decline", outage: "Outage" };
const COLORS = {
  generation: "#9db8c6", outage: "#c8372d", sudden_drop: "#e4811f",
  gradual_decline: "#d4a017", bad_data: "#8b98a1", irradiance: "#12303f",
};

let charts = [];
let navToken = 0; // ignore responses from a page the user already left

// ---------- helpers ----------
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 2) => (n === null || n === undefined ? "–" : Number(n).toFixed(d));
const day = (iso, withYear = false) =>
  new Date(iso + "T00:00:00Z").toLocaleDateString("en-GB", { day: "numeric", month: "short", ...(withYear ? { year: "numeric" } : {}), timeZone: "UTC" });
const range = (a, b) => (a === b ? day(a, true) : `${day(a)} to ${day(b, true)}`);

async function getJSON(path) {
  let res;
  try {
    res = await fetch(API + path);
  } catch {
    throw new Error("Cannot reach the server. Check your connection and try again.");
  }
  if (!res.ok) {
    let detail = `Server returned ${res.status}.`;
    try { const j = await res.json(); if (j.detail) detail = typeof j.detail === "string" ? j.detail : detail; } catch { /* keep default */ }
    throw new Error(detail);
  }
  return res.json();
}

function destroyCharts() { charts.forEach((c) => c.destroy()); charts = []; }

function showLoading() {
  destroyCharts();
  app.innerHTML = `<div class="grid" role="status" aria-label="Loading"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>`;
}

function showError(message, retry) {
  destroyCharts();
  app.innerHTML = `<div class="state error" role="alert"><h2>Something went wrong</h2><p>${esc(message)}</p><button class="primary" id="retry">Try again</button></div>`;
  document.getElementById("retry").addEventListener("click", retry);
}

// ---------- gauge: PR on a half dial with the status bands ----------
function gauge(pr) {
  const cx = 60, cy = 58, r = 46, lo = 0.4, hi = 0.9;
  const ang = (v) => Math.PI * (1 - (Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo));
  const pt = (v, rad = r) => [cx + rad * Math.cos(ang(v)), cy - rad * Math.sin(ang(v))];
  const arc = (a, b, color) => {
    const [x1, y1] = pt(a), [x2, y2] = pt(b);
    return `<path d="M${x1.toFixed(1)} ${y1.toFixed(1)} A${r} ${r} 0 0 1 ${x2.toFixed(1)} ${y2.toFixed(1)}" fill="none" stroke="${color}" stroke-width="9"/>`;
  };
  let needle = "";
  if (pr !== null && pr !== undefined) {
    const [nx, ny] = pt(pr, r - 8);
    needle = `<line x1="${cx}" y1="${cy}" x2="${nx.toFixed(1)}" y2="${ny.toFixed(1)}" stroke="#12303f" stroke-width="3" stroke-linecap="round"/><circle cx="${cx}" cy="${cy}" r="4.5" fill="#12303f"/>`;
  }
  return `<svg class="gauge" viewBox="0 0 120 66" role="img" aria-label="Performance ratio ${fmt(pr)} on a dial from 0.4 to 0.9">
    ${arc(0.4, 0.6, "#e9a49e")}${arc(0.6, 0.7, "#f2cf7a")}${arc(0.7, 0.9, "#8fd0b3")}${needle}</svg>`;
}

// ---------- overview ----------
async function renderOverview() {
  const token = ++navToken;
  note.textContent = "";
  showLoading();
  let plants, quality = null;
  try { plants = await getJSON("/plants"); } catch (e) { if (token === navToken) showError(e.message, renderOverview); return; }
  // Quality reporting is informative; a failure here must not hide the dashboard.
  try { quality = await getJSON("/data-quality"); } catch { /* dashboard remains usable */ }
  if (token !== navToken) return;

  if (!plants.length) {
    app.innerHTML = `<div class="state"><h2>No plants found</h2><p>The data file loaded but contains no usable rows. Check the CSV and restart the server.</p><button id="retry" class="primary">Reload</button></div>`;
    document.getElementById("retry").addEventListener("click", renderOverview);
    return;
  }

  const asOf = plants[0].as_of;
  note.textContent = `Data to ${day(asOf, true)}`;
  const count = (s) => plants.filter((p) => p.status === s).length;
  const tally = [["Underperforming", "bad"], ["Needs attention", "warn"], ["Healthy", "ok"], ["No data", "none"]]
    .filter(([s]) => count(s) > 0)
    .map(([s, c]) => `<li><span class="badge ${c}">${count(s)} ${esc(s.toLowerCase())}</span></li>`).join("");

  const cards = plants.map((p) => {
    const cls = STATUS_CLASS[p.status] || "none";
    const chips = Object.entries(p.issue_counts).filter(([, n]) => n > 0)
      .map(([t, n]) => `<li class="chip t-${t}">${esc(ISSUE_LABEL[t])}${n > 1 ? ` x${n}` : ""}</li>`).join("");
    return `<a class="plant ${cls}" href="#/plant/${encodeURIComponent(p.id)}" aria-label="Plant ${esc(p.id)}, ${esc(p.status)}">
      <div class="plant-top"><h2>Plant ${esc(p.id)}</h2><span class="badge ${cls}">${esc(p.status)}</span></div>
      ${gauge(p.pr_7d)}
      <div class="pr-value"><strong>${fmt(p.pr_7d)}</strong><span>7-day average PR</span></div>
      <p class="meta"><span>${fmt(p.capacity_kw, 0)} kW capacity</span><span>${p.issue_total} issue${p.issue_total === 1 ? "" : "s"}</span></p>
      ${chips ? `<ul class="chips">${chips}</ul>` : `<p class="clean">No issues detected</p>`}
    </a>`;
  }).join("");

  const qualityBanner = quality && (quality.rows_with_quality_flags > 0 || quality.duplicate_rows_removed > 0 || quality.rows_dropped_bad_site_or_date > 0)
    ? `<aside class="quality-note" role="status"><strong>Data quality:</strong> ${quality.rows_with_quality_flags} row(s) have flagged readings; ${quality.duplicate_rows_removed} duplicate row(s) removed and ${quality.rows_dropped_bad_site_or_date} row(s) dropped for invalid date/site. Review <code>/data-quality</code> for details.</aside>`
    : "";

  app.innerHTML = `<h1>Plant health</h1>
    <p class="lede">Status is based on each plant's 7-day average performance ratio (PR), so cloudy days don't count against a plant. Select a plant to see its history.</p>
    ${qualityBanner}
    <ul class="tally">${tally}</ul>
    <div class="grid">${cards}</div>
    <p class="legend-note">Dial bands: below 0.60 underperforming, 0.60 to 0.70 needs attention, 0.70 and above healthy.</p>`;
}

// ---------- detail ----------
async function renderPlant(id, query = {}) {
  const token = ++navToken;
  note.textContent = "";
  if (!document.getElementById("chart-gen")) showLoading();
  const qs = new URLSearchParams(Object.entries(query).filter(([, v]) => v)).toString();
  let p;
  try { p = await getJSON(`/plants/${encodeURIComponent(id)}${qs ? "?" + qs : ""}`); }
  catch (e) { if (token === navToken) showError(e.message, () => renderPlant(id, query)); return; }
  if (token !== navToken) return;

  destroyCharts();
  const cls = STATUS_CLASS[p.status] || "none";
  note.textContent = `Data to ${day(p.as_of, true)}`;

  const issues = p.issues.length
    ? `<ul class="issues">${p.issues.map((i) => `<li class="issue t-${i.type}">
        <div class="issue-top">${esc(ISSUE_LABEL[i.type])}<span>${esc(range(i.start_date, i.end_date))}</span></div>
        <p>${esc(i.reason)}</p></li>`).join("")}</ul>`
    : `<p class="empty">No issues detected${query.start || query.end ? " in this date range" : ""}.</p>`;

  app.innerHTML = `<a class="back" href="#/">Back to all plants</a>
    <div class="detail-head"><h1>Plant ${esc(p.id)}</h1><span class="badge ${cls}">${esc(p.status)}</span></div>
    <p class="stats"><span>7-day average PR <b>${fmt(p.pr_7d)}</b></span><span>Capacity <b>${fmt(p.capacity_kw, 0)} kW</b></span><span>Latest reading <b>${day(p.as_of, true)}</b></span></p>

    <section class="panel" aria-labelledby="h-gen">
      <div class="panel-head">
        <h2 id="h-gen">Daily generation and irradiance</h2>
        <form class="range" id="range-form">
          <label>From<input type="date" id="r-start" value="${esc(query.start || "")}" min="${esc(p.range.min_date)}" max="${esc(p.range.max_date)}"></label>
          <label>To<input type="date" id="r-end" value="${esc(query.end || "")}" min="${esc(p.range.min_date)}" max="${esc(p.range.max_date)}"></label>
          <button class="primary" type="submit">Apply</button><button type="button" id="r-reset">Reset</button>
        </form>
      </div>
      <div id="range-error" role="alert" class="empty" hidden></div>
      ${p.daily.length ? `<div class="chart-box"><canvas id="chart-gen" aria-label="Daily generation bars with irradiance line"></canvas></div>
        <ul class="key">
          <li><i style="background:${COLORS.generation}"></i>Generation (kWh)</li>
          <li><i class="line" style="background:${COLORS.irradiance}"></i>Irradiance (kWh/m2)</li>
          <li><i style="background:${COLORS.outage}"></i>Outage day</li>
          <li><i style="background:${COLORS.sudden_drop}"></i>Sudden drop period</li>
          <li><i style="background:${COLORS.gradual_decline}"></i>Gradual decline period</li>
          <li><i style="background:${COLORS.bad_data}"></i>Bad or missing data</li>
        </ul>` : `<p class="empty">No readings in this date range. Reset the range to see all 90 days.</p>`}
    </section>

    ${p.daily.length ? `<section class="panel" aria-labelledby="h-pr">
      <h2 id="h-pr">Performance ratio</h2>
      <p class="lede" style="margin:4px 0 12px">Daily PR and its 7-day average. Days with irradiance below 1.0 and outage days are left out.</p>
      <div class="chart-box short"><canvas id="chart-pr" aria-label="Daily and 7-day average performance ratio"></canvas></div>
    </section>` : ""}

    <section class="panel" aria-labelledby="h-issues"><h2 id="h-issues" style="margin-bottom:12px">Issues</h2>${issues}</section>`;

  document.getElementById("range-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const start = document.getElementById("r-start").value, end = document.getElementById("r-end").value;
    if (start && end && start > end) {
      const box = document.getElementById("range-error");
      box.textContent = "The start date must be on or before the end date."; box.hidden = false; return;
    }
    renderPlant(id, { start, end });
  });
  document.getElementById("r-reset").addEventListener("click", () => renderPlant(id));

  if (p.daily.length) drawCharts(p);
}

function drawCharts(p) {
  if (typeof Chart === "undefined") {
    document.querySelectorAll(".chart-box").forEach((b) => (b.innerHTML = `<p class="empty">The chart library could not be loaded. Check your connection and reload.</p>`));
    return;
  }
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.color = "#4b6575";
  const d = p.daily;
  const labels = d.map((x) => day(x.date));
  const barColor = d.map((x) => COLORS[x.highlight] || COLORS.generation);
  const tooltipExtra = (items) => {
    const row = d[items[0].dataIndex];
    const lines = [`PR ${fmt(row.pr)}`, `Cloud cover ${fmt(row.cloud_cover, 0)}%`];
    if (row.flags.includes("outage")) lines.push("Outage: 0 kWh with usable sunlight");
    if (row.flags.includes("low_irradiance")) lines.push("Low sunlight: skipped for PR");
    if (row.data_quality.length) lines.push("Data issue: " + row.data_quality.join(", ").replaceAll("_", " "));
    return lines;
  };

  charts.push(new Chart(document.getElementById("chart-gen"), {
    data: {
      labels,
      datasets: [
        { type: "bar", label: "Generation (kWh)", data: d.map((x) => x.generation_kwh), backgroundColor: barColor, yAxisID: "y", order: 2, borderRadius: 2 },
        { type: "line", label: "Irradiance (kWh/m2)", data: d.map((x) => x.irradiance), borderColor: COLORS.irradiance, backgroundColor: COLORS.irradiance, yAxisID: "y1", order: 1, pointRadius: 0, pointHitRadius: 8, borderWidth: 2, tension: 0.25, spanGaps: false },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: false }, tooltip: { callbacks: { afterBody: tooltipExtra } } },
      scales: {
        x: { ticks: { maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false } },
        y: { beginAtZero: true, title: { display: true, text: "Generation (kWh)" } },
        y1: { position: "right", beginAtZero: true, grid: { drawOnChartArea: false }, title: { display: true, text: "Irradiance (kWh/m2)" } },
      },
    },
  }));

  const flat = (v) => d.map(() => v);
  charts.push(new Chart(document.getElementById("chart-pr"), {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "Daily PR", data: d.map((x) => x.pr), borderColor: "transparent", backgroundColor: "#7fa3b5", pointRadius: 2.5, showLine: false },
        { label: "7-day average", data: d.map((x) => x.pr_7d), borderColor: COLORS.irradiance, borderWidth: 2.5, pointRadius: 0, tension: 0.2 },
        { label: "Healthy from 0.70", data: flat(p.thresholds.healthy_min), borderColor: "#17895c", borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0 },
        { label: "Underperforming below 0.60", data: flat(p.thresholds.attention_min), borderColor: "#bf2f26", borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { position: "bottom", labels: { boxWidth: 14, usePointStyle: true } } },
      scales: { x: { ticks: { maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false } }, y: { suggestedMin: 0.3, suggestedMax: 0.95, title: { display: true, text: "PR" } } },
    },
  }));
}

// ---------- router ----------
function route() {
  window.scrollTo(0, 0);
  const m = location.hash.match(/^#\/plant\/([^/?]+)/);
  if (m) renderPlant(decodeURIComponent(m[1]));
  else renderOverview();
}
window.addEventListener("hashchange", route);
route();
