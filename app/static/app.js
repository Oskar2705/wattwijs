"use strict";

/* ================= hulpjes ================= */
const $ = (s, el = document) => el.querySelector(s);
const nl1 = new Intl.NumberFormat("nl-NL", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const nl2 = new Intl.NumberFormat("nl-NL", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const nl0 = new Intl.NumberFormat("nl-NL", { maximumFractionDigits: 0 });
const SVGNS = "http://www.w3.org/2000/svg";

const fmtW = (w) => {
  if (w == null) return "–";
  const a = Math.abs(w);
  return a < 1000 ? `${nl0.format(a)} W` : `${nl1.format(a / 1000)} kW`;
};
const fmtKwh = (wh, unit = true) => {
  if (wh == null) return "–";
  const k = wh / 1000;
  const s = k < 10 ? nl2.format(k) : nl1.format(k);
  return unit ? `${s} kWh` : s;
};
const eurFmt = new Intl.NumberFormat("nl-NL", { style: "currency", currency: "EUR" });
const eur = (v) => (v == null ? "–" : eurFmt.format(v));
const cent = (v) => (v == null ? "–" : `${Math.abs(v) < 0.1 ? nl1.format(v * 100) : nl0.format(v * 100)} cent`);
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" });
const dayWord = (ts) => {
  const d = localDate(new Date(ts * 1000)), t = localDate();
  const tm = localDate(new Date(Date.now() + 86400000));
  return d === t ? "vandaag" : d === tm ? "morgen" : dayName(d, { weekday: "long" });
};
const pct = (v) => (v == null ? "–" : `${nl0.format(v)}%`);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

const COLORS = { sun: "var(--sun)", grid: "var(--grid)", batt: "var(--batt)", house: "var(--house)", hp: "var(--hp)" };

function el(tag, attrs = {}, parent) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}

function localDate(d = new Date()) {
  const z = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
  return z.toISOString().slice(0, 10);
}
const dayName = (iso, opts = { weekday: "long", day: "numeric", month: "long", year: "numeric" }) =>
  new Date(iso + "T12:00:00").toLocaleDateString("nl-NL", opts);

/* ================= tooltip ================= */
const tip = $("#tooltip");
let tipOwner = null;
function showTip(html, x, y) {
  tip.innerHTML = html;
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  let left = x + 16, top = y - r.height / 2;
  if (left + r.width > innerWidth - 8) left = x - r.width - 16;
  if (left < 8) {
    // Smal scherm: midden onder (of boven) het aangetikte punt, binnen het scherm.
    left = x - r.width / 2;
    top = y + 12 + r.height > innerHeight - 8 ? y - r.height - 12 : y + 12;
  }
  left = Math.max(8, Math.min(left, innerWidth - r.width - 8));
  top = Math.max(8, Math.min(top, innerHeight - r.height - 8));
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}
const hideTip = () => { tip.hidden = true; tipOwner = null; };
// Op een touchscreen: tik buiten een grafiek of paneel, of scrollen, sluit het infovenster.
document.addEventListener("pointerdown", (e) => {
  if (!e.target.closest(".pv, .chart")) hideTip();
}, { passive: true });
window.addEventListener("scroll", () => { if (!tip.hidden) hideTip(); }, { passive: true });
const tipRow = (color, label, value) => `<div class="row"><i style="--c:${color}"></i><span>${label}</span><span>${value}</span></div>`;

/* ================= grafieken ================= */
function niceScale(v) {
  // Rond stapgrootte af op 1, 2 of 5 x 10^n; geeft {max, step}.
  if (v <= 0) return { max: 1, step: 0.25 };
  const raw = v / 4;
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / p;
  const step = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  return { max: Math.ceil(v / step) * step, step };
}

function chartFrame(host) {
  host.innerHTML = "";
  const w = host.clientWidth || 600, h = host.clientHeight || 280;
  const m = { l: 44, r: 12, t: 12, b: 28 };
  const svg = el("svg", { viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: "none" }, host);
  return { svg, w, h, m, iw: w - m.l - m.r, ih: h - m.t - m.b };
}

function yAxis(f, scale, fmt) {
  const { max, step } = scale;
  const g = el("g", { class: "axis" }, f.svg);
  const n = Math.round(max / step);
  for (let i = 0; i <= n; i++) {
    const v = step * i, y = f.m.t + f.ih - (v / max) * f.ih;
    el("line", { x1: f.m.l, x2: f.m.l + f.iw, y1: y, y2: y, class: i === 0 ? "base" : "gridl" }, g);
    const t = el("text", { x: f.m.l - 8, y: y + 4, "text-anchor": "end" }, g);
    t.textContent = fmt(v);
  }
}

/* Lijngrafiek met kruisdraad. series: [{name,color,values:[number|null]}], x: [timestamps] */
function lineChart(host, { x, series, x0, x1, yFmt, xTickFmt, xTicks, tooltip }) {
  const f = chartFrame(host);
  const allY = series.flatMap((s) => s.values.filter((v) => v != null));
  if (!x.length || !allY.length) {
    const t = el("text", { x: f.w / 2, y: f.h / 2, "text-anchor": "middle", class: "empty" }, f.svg);
    t.textContent = "Nog geen gegevens voor deze dag";
    return;
  }
  const scale = niceScale(Math.max(...allY) * 1.02);
  const max = scale.max;
  yAxis(f, scale, yFmt);
  const sx = (t) => f.m.l + ((t - x0) / (x1 - x0)) * f.iw;
  const sy = (v) => f.m.t + f.ih - (v / max) * f.ih;
  const gx = el("g", { class: "axis" }, f.svg);
  for (const t of xTicks) {
    const tx = el("text", { x: sx(t), y: f.h - 6, "text-anchor": "middle" }, gx);
    tx.textContent = xTickFmt(t);
  }
  const labels = [];
  for (const s of series) {
    let d = "", pen = false, lastPt = null;
    s.values.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${sx(x[i]).toFixed(1)} ${sy(v).toFixed(1)}`;
      pen = true;
      lastPt = [sx(x[i]), sy(v)];
    });
    el("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "vector-effect": "non-scaling-stroke" }, f.svg);
    if (lastPt) labels.push({ y: lastPt[1], x: lastPt[0], s });
  }
  // directe labels aan het eind van elke lijn, botsingen voorkomen
  labels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++) if (labels[i].y - labels[i - 1].y < 15) labels[i].y = labels[i - 1].y + 15;
  for (const l of labels) {
    if (l.x > f.m.l + f.iw - 70) continue; // dag bijna vol: legenda volstaat
    const t = el("text", { x: l.x + 6, y: l.y + 4, class: "dlabel" }, f.svg);
    t.textContent = l.s.name;
  }

  const cross = el("line", { y1: f.m.t, y2: f.m.t + f.ih, class: "crosshair", visibility: "hidden" }, f.svg);
  const dots = series.map((s) => el("circle", { r: 4.5, fill: s.color, stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" }, f.svg));
  const hit = el("rect", { x: f.m.l, y: f.m.t, width: f.iw, height: f.ih, fill: "transparent" }, f.svg);
  const move = (ev) => {
    const r = f.svg.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * f.w;
    const t = x0 + ((px - f.m.l) / f.iw) * (x1 - x0);
    let best = 0;
    for (let i = 1; i < x.length; i++) if (Math.abs(x[i] - t) < Math.abs(x[best] - t)) best = i;
    const cx = sx(x[best]);
    cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
    series.forEach((s, k) => {
      const v = s.values[best];
      dots[k].setAttribute("visibility", v == null ? "hidden" : "visible");
      if (v != null) { dots[k].setAttribute("cx", cx); dots[k].setAttribute("cy", sy(v)); }
    });
    showTip(tooltip(best), ev.clientX, ev.clientY);
  };
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerdown", move);
  hit.addEventListener("pointerleave", (ev) => { if (ev.pointerType !== "mouse") return; hideTip(); cross.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden")); });
}

/* Gegroepeerde staafgrafiek. series: [{name,color,values}] */
function barChart(host, { n, series, tick, tickEvery = 1, yFmt, tooltip }) {
  const f = chartFrame(host);
  const all = series.flatMap((s) => s.values.filter((v) => v != null));
  if (!all.length || Math.max(...all) <= 0) {
    const t = el("text", { x: f.w / 2, y: f.h / 2, "text-anchor": "middle", class: "empty" }, f.svg);
    t.textContent = "Geen gegevens in deze periode";
    return;
  }
  const scale = niceScale(Math.max(...all) * 1.02);
  const max = scale.max;
  yAxis(f, scale, yFmt);
  const slot = f.iw / n;
  const groupW = Math.min(slot * 0.78, 64);
  const gap = 2;
  const bw = Math.max((groupW - gap * (series.length - 1)) / series.length, 1.5);
  const sy = (v) => f.m.t + f.ih - (v / max) * f.ih;
  const gx = el("g", { class: "axis" }, f.svg);
  const bars = el("g", {}, f.svg);
  for (let i = 0; i < n; i++) {
    const cx = f.m.l + slot * i + slot / 2;
    if (i % tickEvery === 0) {
      const t = el("text", { x: cx, y: f.h - 6, "text-anchor": "middle" }, gx);
      t.textContent = tick(i);
    }
    series.forEach((s, k) => {
      const v = s.values[i];
      if (!v || v <= 0) return;
      const x = cx - groupW / 2 + k * (bw + gap);
      const y = sy(v), h = f.m.t + f.ih - y;
      const r = Math.min(4, bw / 2, h);
      el("path", { d: `M${x} ${y + h}V${y + r}q0 -${r} ${r} -${r}h${bw - 2 * r}q${r} 0 ${r} ${r}V${y + h}Z`, fill: s.color }, bars);
    });
    const hit = el("rect", { x: f.m.l + slot * i, y: f.m.t, width: slot, height: f.ih, fill: "transparent" }, f.svg);
    const show = (ev) => { hit.setAttribute("fill", "var(--grid-line)"); hit.setAttribute("fill-opacity", ".6"); showTip(tooltip(i), ev.clientX, ev.clientY); };
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerdown", show);
    hit.addEventListener("pointerleave", (ev) => { hit.setAttribute("fill", "transparent"); if (ev.pointerType === "mouse") hideTip(); });
    f.svg.insertBefore(hit, bars);
  }
}

function legend(host, items, box = false) {
  host.innerHTML = items.map(([name, color]) => `<span><i class="${box ? "box" : ""}" style="--c:${color}"></i>${name}</span>`).join("");
}

/* ================= live ================= */
let live = null;

function setFlow(id, watts, reverse) {
  const p = $(`#f-${id}`);
  const on = watts != null && watts > 15;
  p.classList.toggle("on", on);
  p.classList.toggle("rev", !!reverse);
  if (on) {
    const kw = watts / 1000;
    p.style.animationDuration = `${Math.max(0.35, 2.4 - kw * 0.55).toFixed(2)}s`;
    p.style.strokeWidth = `${(4 + Math.min(kw, 3) * 1.6).toFixed(1)}`;
  }
}

function renderLive(d) {
  live = d;
  const n = d.now;
  $("#siteName").textContent = `Energie ${d.site_name}`;
  $("#demoNote").hidden = !d.demo;

  const anyOnline = Object.values(d.sources).some((s) => s.configured && s.online);
  const pulse = $("#pulse");
  pulse.className = "pulse " + (anyOnline ? "live" : "stale");
  $("#pulseText").textContent = anyOnline
    ? `Live, bijgewerkt om ${new Date().toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`
    : "Geen verbinding met de apparaten";

  // schema
  $("#v-sun").textContent = fmtW(n.solar_w);
  $("#v-grid").textContent = n.grid_w == null ? "–" : `${fmtW(n.grid_w)} ${n.grid_w < -15 ? "terug" : n.grid_w > 15 ? "af" : ""}`.trim();
  $("#v-house").textContent = fmtW(n.house_w);
  $("#v-batt").textContent = !n.battery_configured ? "uit" : n.soc == null ? "–" : `${nl0.format(n.soc)}%`;
  $("#v-hp").textContent = fmtW(n.heatpump_w);
  setFlow("sun", n.solar_w, false);
  setFlow("grid", n.grid_w == null ? null : Math.abs(n.grid_w), n.grid_w < 0);
  setFlow("batt", n.battery_w == null ? null : Math.abs(n.battery_w), n.battery_w < 0);
  setFlow("hp", n.heatpump_w, false);
  const fill = $("#battFill");
  const h = n.soc == null ? 0 : (Math.max(0, Math.min(100, n.soc)) / 100) * 21;
  fill.setAttribute("height", h.toFixed(1));
  fill.setAttribute("y", (10 - h).toFixed(1));

  // verhaal
  const s = [];
  const li = (c, html) => s.push(`<li style="--c:${c}"><span>${html}</span></li>`);
  if (n.solar_w == null) li(COLORS.sun, "Geen gegevens van de zonnepanelen.");
  else if (n.solar_w < 15) li(COLORS.sun, "De zonnepanelen leveren nu niets.");
  else li(COLORS.sun, `De zon levert <strong>${fmtW(n.solar_w)}</strong>.`);
  if (n.house_w == null) li(COLORS.house, "Het verbruik van het huis is nu onbekend.");
  else li(COLORS.house, `Het huis gebruikt <strong>${fmtW(n.house_w)}</strong>${n.heatpump_w > 30 ? `, waarvan de warmtepomp <strong>${fmtW(n.heatpump_w)}</strong>` : ""}.`);
  if (!n.battery_configured) { /* batterij niet in gebruik */ }
  else if (n.battery_w == null) li(COLORS.batt, "Geen gegevens van de thuisbatterij.");
  else if (n.battery_w > 25) li(COLORS.batt, `De batterij laadt op met <strong>${fmtW(n.battery_w)}</strong> en is <strong>${pct(n.soc)}</strong> vol.`);
  else if (n.battery_w < -25) li(COLORS.batt, `De batterij levert <strong>${fmtW(n.battery_w)}</strong> aan het huis en is nog <strong>${pct(n.soc)}</strong> vol.`);
  else li(COLORS.batt, `De batterij staat stil en is <strong>${pct(n.soc)}</strong> vol.`);
  if (n.grid_w == null) li(COLORS.grid, "Geen gegevens van de slimme meter.");
  else if (n.grid_w > 25) li(COLORS.grid, `Je neemt <strong>${fmtW(n.grid_w)}</strong> af van het net.`);
  else if (n.grid_w < -25) li(COLORS.grid, `Je levert <strong>${fmtW(n.grid_w)}</strong> terug aan het net.`);
  else li(COLORS.grid, "Er gaat bijna niets van of naar het net.");
  const pn = d.price && d.price.now;
  if (pn) {
    const lvl = pn.level ? `, ${pn.level} voor vandaag` : "";
    li("var(--ink)", `Stroom kost nu <strong>${cent(pn.all_in)}</strong> per kWh${lvl}. Terugleveren levert <strong>${cent(pn.export)}</strong> op.`);
  }
  $("#story").innerHTML = s.join("");
  renderPriceTips(d.price, d.price_error);

  const hp = n.heatpump || {};
  const parts = [];
  if (hp.supply_temperature != null) parts.push(`aanvoer ${nl1.format(hp.supply_temperature)} °C`);
  if (hp.outdoor_temperature != null) parts.push(`buiten ${nl1.format(hp.outdoor_temperature)} °C`);
  // Onder ~300 W draait alleen de circulatiepomp; de "warmte" is dan nawarmte uit de leidingen
  // en een rendement zegt niets. Boven 8× is de cloudwaarde van stroom of warmte niet van hetzelfde moment.
  const HP_COMPRESSOR_W = 300, COP_MAX = 8;
  if (n.heatpump_w != null && n.heatpump_w < HP_COMPRESSOR_W) {
    parts.push("compressor staat uit");
  } else if (hp.heat_output_w > 50 && n.heatpump_w >= HP_COMPRESSOR_W) {
    const cop = hp.heat_output_w / n.heatpump_w;
    parts.push(`maakt ${fmtW(hp.heat_output_w)} warmte` + (cop >= 1 && cop <= COP_MAX ? ` (rendement ${nl1.format(cop)}×)` : ""));
  }
  if (Number(hp.fault_code) > 0) parts.push(`storingscode ${esc(hp.fault_code)} (zie de MyDeWarmte-app)`);
  $("#hpDetail").textContent = parts.length ? `Warmtepomp: ${parts.join(", ")}.` : "";

  // vandaag
  const t = d.today;
  const tile = (k, c, v, sub = "", hero = false) =>
    `<div class="tile${hero ? " hero" : ""}"><div class="k">${c ? `<i style="--c:${c}"></i>` : ""}${k}</div><div class="v">${v}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
  const kwh = (wh) => `${fmtKwh(wh, false)}<small>kWh</small>`;
  $("#today").innerHTML = [
    tile("Zelf voorzien", "", t.self_sufficiency == null ? "–" : `${nl0.format(t.self_sufficiency)}<small>%</small>`, "van het verbruik kwam van eigen zon of batterij", true),
    tile("Opgewekt", COLORS.sun, kwh(t.solar_wh)),
    tile("Verbruikt", COLORS.house, kwh(t.house_wh)),
    tile("Warmtepomp", COLORS.hp, kwh(t.heatpump_wh)),
    tile("Van het net", COLORS.grid, kwh(t.import_wh)),
    tile("Teruggeleverd", COLORS.grid, kwh(t.export_wh)),
    n.battery_configured
      ? tile("Batterij geladen", COLORS.batt, kwh(t.charge_wh), `ontladen ${fmtKwh(t.discharge_wh)}`)
      : tile("Batterij", COLORS.batt, "uit", "niet aangesloten"),
  ].join("");

  // euro's vandaag
  const money = $("#todayMoney");
  money.hidden = t.net_cost == null;
  if (t.net_cost != null) {
    const before = localDate() < d.contract_start;
    money.innerHTML = [
      tile("Kosten vandaag", "", eur(t.net_cost), "afname min teruglevering"),
      tile("Stroom van het net", COLORS.grid, eur(t.cost_import)),
      tile("Teruglevering", COLORS.sun, eur(t.revenue_export)),
      tile("Gemiddeld betaald", COLORS.grid, t.import_wh > 100 ? cent(t.cost_import / (t.import_wh / 1000)) : "–",
        d.price && d.price.today_avg != null ? `per kWh van het net; daggemiddelde ${cent(d.price.today_avg)}` : "per kWh van het net"),
    ].join("") + (before ? `<div class="money-note">Indicatie met de prijzen van Frank Energie. Tot ${dayName(d.contract_start, { day: "numeric", month: "long", year: "numeric" })} geldt nog het oude contract met salderen.</div>` : `<div class="money-note">Zonder vaste kosten, netbeheerkosten en de jaarlijkse vermindering energiebelasting.</div>`);
  }

  // meldingen
  const alerts = d.open_alerts || [];
  $("#panelBadge").hidden = !alerts.length;
  $("#panelBadge").textContent = alerts.length;
  const strip = $("#alertStrip");
  strip.hidden = !alerts.length || location.hash === "#panelen";
  if (alerts.length) {
    strip.className = "alert-strip verdict bad";
    const one = alerts[0];
    const deadOne = alerts.length === 1 && one.deviation != null && one.deviation <= -75;
    strip.innerHTML = `${warnIcon()}<div><h2>${alerts.length === 1 ? `Zonnepaneel ${esc(one.name)} ${deadOne ? "levert (bijna) niets op" : "levert te weinig op"}` : `${alerts.length} zonnepanelen leveren te weinig op`}</h2><p><a href="#panelen">Bekijk de panelen</a></p></div>`;
  }

  // bronnen
  const names = { grid: "Slimme meter", battery: "Thuisbatterij", solar: "Zonnepanelen", heatpump: "Warmtepomp" };
  $("#sources").innerHTML = Object.entries(d.sources)
    .map(([k, src]) => {
      const cls = !src.configured ? "" : src.online ? "ok" : "down";
      const txt = !src.configured ? "niet ingesteld" : src.online ? (src.mode ? `verbonden via ${src.mode}` : "verbonden") : "geen verbinding";
      return `<li class="${cls}" title="${esc(src.error || "")}"><span class="st"></span>${names[k]}: ${txt}</li>`;
    })
    .join("");
}

async function pollLive() {
  try {
    renderLive(await getJSON("/api/live"));
  } catch (e) {
    $("#pulse").className = "pulse stale";
    $("#pulseText").textContent = "Geen verbinding met de energiemonitor";
  }
}

let curveData = null;
async function loadCurve() {
  curveData = await getJSON("/api/curve");
  drawCurve();
}
function drawCurve() {
  if (!curveData) return;
  const day = curveData.day;
  const x0 = new Date(day + "T00:00:00").getTime() / 1000;
  const x1 = x0 + 86400;
  const pts = curveData.points;
  const x = pts.map((p) => p.ts);
  const kw = (k) => pts.map((p) => (p[k] == null ? null : Math.max(p[k], 0) / 1000));
  const series = [
    { name: "Zon", color: COLORS.sun, values: kw("solar_w") },
    { name: "Verbruik huis", color: COLORS.house, values: kw("house_w") },
    { name: "Warmtepomp", color: COLORS.hp, values: kw("heatpump_w") },
  ];
  legend($("#curveLegend"), series.map((s) => [s.name, s.color]));
  lineChart($("#curveChart"), {
    x, series, x0, x1,
    yFmt: (v) => (Number.isInteger(v) ? nl0.format(v) : nl1.format(v)),
    xTicks: ($("#curveChart").clientWidth < 560 ? [0, 6, 12, 18, 24] : [0, 3, 6, 9, 12, 15, 18, 21, 24]).map((h) => x0 + h * 3600),
    xTickFmt: (t) => `${Math.round((t - x0) / 3600)}:00`,
    tooltip: (i) => {
      const p = pts[i];
      const tm = new Date(p.ts * 1000).toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" });
      return `<b>${tm}</b>` +
        tipRow(COLORS.sun, "Zon", fmtW(p.solar_w)) +
        tipRow(COLORS.house, "Verbruik huis", fmtW(p.house_w)) +
        tipRow(COLORS.hp, "Warmtepomp", fmtW(p.heatpump_w)) +
        `<div class="sep"></div>` +
        tipRow(COLORS.grid, p.grid_w < 0 ? "Terug naar net" : "Van het net", fmtW(p.grid_w)) +
        tipRow(COLORS.batt, p.battery_w < 0 ? "Batterij levert" : "Batterij laadt", fmtW(p.battery_w)) +
        tipRow(COLORS.batt, "Batterij vol", pct(p.soc));
    },
  });
}

/* ================= prijzen ================= */
const tipIcon = {
  clock: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`,
  up: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 17l6-6 4 4 6-7"/><path d="M15 8h5v5"/></svg>`,
};

function renderPriceTips(p, error) {
  const card = $("#priceCard");
  const wasHidden = card.hidden;
  card.hidden = !p && !error;
  if (!p) {
    if (error) $("#priceTips").innerHTML = `<li class="warn">${warnIcon()}<span>De stroomprijzen konden niet worden opgehaald: ${esc(error)}</span></li>`;
    return;
  }
  if (wasHidden) drawPrices();
  const tips = [];
  for (const w of p.negative || []) {
    tips.push(`<li class="warn">${warnIcon()}<span><strong>Negatieve prijs ${dayWord(w.start)} ${clock(w.start)} tot ${clock(w.end)}.</strong> Terugleveren kost dan geld (tot ${cent(-w.min_export)} per kWh); stroom gebruiken is dan juist gunstig.</span></li>`);
  }
  if (p.cheapest_2h) {
    const b = p.cheapest_2h;
    tips.push(`<li>${tipIcon.clock}<span>Goedkoopste 2 uur voor de wasmachine of vaatwasser: <strong>${dayWord(b.start)} ${clock(b.start)} tot ${clock(b.end)}</strong>, gemiddeld ${cent(b.avg)}.</span></li>`);
  }
  if (p.next_high) {
    tips.push(`<li>${tipIcon.up}<span>Duurste moment van de rest van vandaag: <strong>${clock(p.next_high.ts)}</strong>, ${cent(p.next_high.all_in)}. Dan liever geen grote verbruikers aan.</span></li>`);
  }
  if (!p.has_tomorrow) tips.push(`<li>${infoIcon()}<span>De prijzen voor morgen komen rond 13:00 binnen.</span></li>`);
  $("#priceTips").innerHTML = tips.join("");
}

let priceData = null;
async function loadPrices() {
  priceData = await getJSON("/api/prices");
  drawPrices();
}

function drawPrices() {
  const host = $("#priceChart");
  if (!priceData || $("#priceCard").hidden) return;
  const rows = priceData.prices;
  legend($("#priceLegend"), [["Afnameprijs", "var(--grid)"], ["Terugleverprijs", "var(--sun)"]]);
  const f = chartFrame(host);
  if (!rows.length) {
    const t = el("text", { x: f.w / 2, y: f.h / 2, "text-anchor": "middle", class: "empty" }, f.svg);
    t.textContent = priceData.error ? "Prijzen ophalen lukt niet" : "Nog geen prijzen";
    return;
  }
  const last = rows[rows.length - 1].ts + 900;
  const x0 = priceData.start, x1 = Math.max(last, x0 + 86400);
  const hi = Math.max(...rows.map((r) => r.all_in)) * 100;
  const lo = Math.min(0, ...rows.map((r) => r.export)) * 100;
  const sc = niceScale(hi - lo);
  let step = sc.step;
  const yMin = lo < 0 ? -Math.ceil(-lo / step) * step : 0;
  const yMax = Math.ceil(hi / step) * step;
  const sx = (t) => f.m.l + ((t - x0) / (x1 - x0)) * f.iw;
  const sy = (v) => f.m.t + f.ih - ((v - yMin) / (yMax - yMin)) * f.ih;
  const ga = el("g", { class: "axis" }, f.svg);
  for (let v = yMin; v <= yMax + 1e-9; v += step) {
    el("line", { x1: f.m.l, x2: f.m.l + f.iw, y1: sy(v), y2: sy(v), class: Math.abs(v) < 1e-9 ? "base" : "gridl" }, ga);
    const t = el("text", { x: f.m.l - 8, y: sy(v) + 4, "text-anchor": "end" }, ga);
    t.textContent = nl0.format(v);
  }
  const narrow = host.clientWidth < 560;
  for (let h = 0; h * 3600 <= x1 - x0; h += narrow ? 12 : 3) {
    const t = el("text", { x: sx(x0 + h * 3600), y: f.h - 6, "text-anchor": "middle" }, ga);
    t.textContent = `${h % 24}:00`;
  }
  // scheiding vandaag/morgen
  const mid = x0 + 86400;
  if (x1 > mid) {
    el("line", { x1: sx(mid), x2: sx(mid), y1: f.m.t, y2: f.m.t + f.ih, class: "daysep" }, f.svg);
    const t = el("text", { x: sx(mid) + 6, y: f.m.t + 12, class: "daylabel" }, f.svg);
    t.textContent = "morgen";
  }
  const now = Date.now() / 1000;
  const bw = Math.max(f.iw / ((x1 - x0) / 900) - 1, 1);
  const g = el("g", {}, f.svg);
  for (const r of rows) {
    const v = r.all_in * 100, y0 = sy(0), y = sy(v);
    el("rect", { x: sx(r.ts) + 0.5, y: Math.min(y, y0), width: bw, height: Math.max(Math.abs(y0 - y), 1), fill: "var(--grid)", "fill-opacity": r.ts + 900 < now ? 0.35 : 0.9 }, g);
  }
  let d = "";
  rows.forEach((r, i) => { d += `${i ? "L" : "M"}${(sx(r.ts) + bw / 2).toFixed(1)} ${sy(r.export * 100).toFixed(1)}`; });
  el("path", { d, fill: "none", stroke: "var(--sun)", "stroke-width": 2, "stroke-linejoin": "round" }, f.svg);
  if (now >= x0 && now <= x1) {
    el("line", { x1: sx(now), x2: sx(now), y1: f.m.t, y2: f.m.t + f.ih, class: "nowline" }, f.svg);
    const t = el("text", { x: sx(now), y: f.m.t - 2, "text-anchor": "middle", class: "nowlabel" }, f.svg);
    t.textContent = "nu";
  }
  const hl = el("rect", { y: f.m.t, height: f.ih, width: bw + 2, fill: "var(--ink)", "fill-opacity": 0.08, visibility: "hidden" }, f.svg);
  const hit = el("rect", { x: f.m.l, y: f.m.t, width: f.iw, height: f.ih, fill: "transparent" }, f.svg);
  const move = (ev) => {
    const r0 = f.svg.getBoundingClientRect();
    const t = x0 + (((ev.clientX - r0.left) / r0.width) * f.w - f.m.l) / f.iw * (x1 - x0);
    const r = rows.find((x) => t >= x.ts && t < x.ts + 900);
    if (!r) { hl.setAttribute("visibility", "hidden"); hideTip(); return; }
    hl.setAttribute("x", sx(r.ts) - 0.5); hl.setAttribute("visibility", "visible");
    showTip(`<b>${dayWord(r.ts)} ${clock(r.ts)} – ${clock(r.ts + 900)}</b>` +
      tipRow("var(--grid)", "Afname kost", cent(r.all_in)) +
      tipRow("var(--sun)", "Terugleveren levert", cent(r.export)), ev.clientX, ev.clientY);
  };
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerdown", move);
  hit.addEventListener("pointerleave", (ev) => { if (ev.pointerType !== "mouse") return; hl.setAttribute("visibility", "hidden"); hideTip(); });
}

/* ================= geschiedenis ================= */
const hist = { mode: "hour", key: localDate(), data: null, table: false };

function shiftKey(mode, key, dir) {
  if (mode === "hour") { const d = new Date(key + "T12:00:00"); d.setDate(d.getDate() + dir); return localDate(d); }
  if (mode === "day") { const [y, m] = key.split("-").map(Number); const d = new Date(y, m - 1 + dir, 1); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; }
  return String(Number(key) + dir);
}
function defaultKey(mode) {
  const t = localDate();
  return mode === "hour" ? t : mode === "day" ? t.slice(0, 7) : t.slice(0, 4);
}
function keyLabel(mode, key) {
  if (mode === "hour") return dayName(key);
  if (mode === "day") return new Date(key + "-15T12:00:00").toLocaleDateString("nl-NL", { month: "long", year: "numeric" });
  return key;
}

async function loadHistory() {
  hist.data = await getJSON(`/api/history?mode=${hist.mode}&key=${hist.key}`);
  drawHistory();
}

function bucketLabel(mode, b, long = false) {
  if (mode === "hour") return long ? `${b}:00 – ${b + 1}:00` : `${b}`;
  if (mode === "day") return long ? dayName(b, { weekday: "short", day: "numeric", month: "long" }) : String(Number(b.slice(8)));
  return new Date(b + "-15T12:00:00").toLocaleDateString("nl-NL", { month: long ? "long" : "short" });
}

function drawHistory() {
  const d = hist.data;
  if (!d) return;
  $("#histLabel").textContent = keyLabel(hist.mode, hist.key);
  const today = defaultKey(hist.mode);
  $("#histNext").disabled = hist.key >= today;
  const fd = d.first_day || localDate();
  const first = hist.mode === "hour" ? fd : hist.mode === "day" ? fd.slice(0, 7) : fd.slice(0, 4);
  $("#histPrev").disabled = hist.key <= first;

  const t = d.total;
  $("#histSum").innerHTML = [
    ["Opgewekt", fmtKwh(t.solar_wh)], ["Verbruikt", fmtKwh(t.house_wh)], ["Van het net", fmtKwh(t.import_wh)],
    ["Teruggeleverd", fmtKwh(t.export_wh)], ["Zelf voorzien", pct(t.self_sufficiency)],
    ...(t.net_cost != null ? [["Kosten stroom", eur(t.net_cost)]] : []),
  ].map(([k, v]) => `<span>${k} <strong>${v}</strong></span>`).join("");

  const rows = d.rows;
  const series = [
    { name: "Opgewekt", color: COLORS.sun, values: rows.map((r) => r.solar_wh) },
    { name: "Verbruikt", color: COLORS.house, values: rows.map((r) => r.house_wh) },
  ];
  $("#histTitle").textContent = hist.mode === "hour" ? "Opwek en verbruik per uur" : hist.mode === "day" ? "Opwek en verbruik per dag" : "Opwek en verbruik per maand";
  legend($("#histLegend"), series.map((s) => [s.name, s.color]), true);
  const small = $("#histChart").clientWidth < 560;
  barChart($("#histChart"), {
    n: rows.length, series,
    tick: (i) => bucketLabel(hist.mode, rows[i].bucket),
    tickEvery: hist.mode === "hour" ? (small ? 3 : 2) : hist.mode === "day" ? (small ? 5 : 2) : 1,
    yFmt: (v) => (Number.isInteger(v / 1000) ? nl0.format(v / 1000) : nl1.format(v / 1000)),
    tooltip: (i) => {
      const r = rows[i];
      if (r.solar_wh == null) return `<b>${bucketLabel(hist.mode, r.bucket, true)}</b>Geen gegevens`;
      return `<b>${bucketLabel(hist.mode, r.bucket, true)}</b>` +
        tipRow(COLORS.sun, "Opgewekt", fmtKwh(r.solar_wh)) +
        tipRow(COLORS.house, "Verbruikt", fmtKwh(r.house_wh)) +
        tipRow(COLORS.hp, "waarvan warmtepomp", fmtKwh(r.heatpump_wh)) +
        `<div class="sep"></div>` +
        tipRow(COLORS.grid, "Van het net", fmtKwh(r.import_wh)) +
        tipRow(COLORS.grid, "Teruggeleverd", fmtKwh(r.export_wh)) +
        tipRow(COLORS.batt, "Batterij geladen", fmtKwh(r.charge_wh)) +
        tipRow(COLORS.batt, "Batterij ontladen", fmtKwh(r.discharge_wh)) +
        (r.net_cost != null ? `<div class="sep"></div>` + tipRow("var(--ink)", "Kosten (netto)", eur(r.net_cost)) : "");
    },
  });

  const hasMoney = rows.some((r) => r.net_cost != null);
  $("#histMoneyCard").hidden = !hasMoney;
  if (hasMoney) {
    const ms = [
      { name: "Kosten stroom van het net", color: COLORS.grid, values: rows.map((r) => r.cost_import) },
      { name: "Opbrengst teruglevering", color: COLORS.sun, values: rows.map((r) => (r.revenue_export == null ? null : Math.max(r.revenue_export, 0))) },
    ];
    $("#histMoneyTitle").textContent = hist.mode === "hour" ? "Kosten en opbrengst per uur" : hist.mode === "day" ? "Kosten en opbrengst per dag" : "Kosten en opbrengst per maand";
    const periodStart = hist.mode === "hour" ? hist.key : hist.mode === "day" ? `${hist.key}-01` : `${hist.key}-01-01`;
    const startsBefore = periodStart < d.contract_start;
    $("#histMoneyHint").textContent = startsBefore ? "Indicatie: vóór de overstap gold nog het oude contract" : "Variabele kosten, zonder vaste kosten";
    legend($("#histMoneyLegend"), ms.map((x) => [x.name, x.color]), true);
    barChart($("#histMoneyChart"), {
      n: rows.length, series: ms,
      tick: (i) => bucketLabel(hist.mode, rows[i].bucket),
      tickEvery: hist.mode === "hour" ? (small ? 3 : 2) : hist.mode === "day" ? (small ? 5 : 2) : 1,
      yFmt: (v) => eurFmt.format(v).replace(",00", ""),
      tooltip: (i) => {
        const r = rows[i];
        if (r.net_cost == null) return `<b>${bucketLabel(hist.mode, r.bucket, true)}</b>Geen prijzen of gegevens`;
        return `<b>${bucketLabel(hist.mode, r.bucket, true)}</b>` +
          tipRow(COLORS.grid, "Stroom van het net", eur(r.cost_import)) +
          tipRow(COLORS.sun, "Teruglevering", eur(r.revenue_export)) +
          `<div class="sep"></div>` + tipRow("var(--ink)", "Netto kosten", eur(r.net_cost)) +
          tipRow(COLORS.batt, "Batterij leverde op", eur(r.battery_value));
      },
    });
  }

  const cols = [["solar_wh", "Opgewekt"], ["house_wh", "Verbruikt"], ["heatpump_wh", "Warmtepomp"], ["import_wh", "Van net"], ["export_wh", "Terug"], ["charge_wh", "Batterij in"], ["discharge_wh", "Batterij uit"]];
  $("#histTable").innerHTML = `<table><thead><tr><th>${hist.mode === "hour" ? "Uur" : hist.mode === "day" ? "Dag" : "Maand"}</th>${cols.map(([, l]) => `<th>${l}</th>`).join("")}</tr></thead><tbody>` +
    rows.filter((r) => r.solar_wh != null).map((r) => `<tr><td>${bucketLabel(hist.mode, r.bucket, true)}</td>${cols.map(([k]) => `<td>${fmtKwh(r[k], false)}</td>`).join("")}</tr>`).join("") +
    `</tbody><tfoot><tr><td>Totaal (kWh)</td>${cols.map(([k]) => `<td>${fmtKwh(t[k], false)}</td>`).join("")}</tr></tfoot></table>`;
  if (t.net_cost != null) {
    const ecols = [["cost_import", "Kosten net"], ["revenue_export", "Teruglevering"], ["net_cost", "Netto"], ["battery_value", "Batterij"]];
    $("#histTable").innerHTML += `<table style="margin-top:1rem"><thead><tr><th>In euro's</th>${ecols.map(([, l]) => `<th>${l}</th>`).join("")}</tr></thead><tbody>` +
      rows.filter((r) => r.net_cost != null).map((r) => `<tr><td>${bucketLabel(hist.mode, r.bucket, true)}</td>${ecols.map(([k]) => `<td>${eur(r[k])}</td>`).join("")}</tr>`).join("") +
      `</tbody><tfoot><tr><td>Totaal</td>${ecols.map(([k]) => `<td>${eur(t[k])}</td>`).join("")}</tr></tfoot></table>`;
  }
}

function initHistory() {
  document.querySelectorAll(".seg button").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll(".seg button").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
      hist.mode = b.dataset.mode;
      hist.key = defaultKey(hist.mode);
      loadHistory();
    })
  );
  $("#histPrev").addEventListener("click", () => { hist.key = shiftKey(hist.mode, hist.key, -1); loadHistory(); });
  $("#histNext").addEventListener("click", () => { hist.key = shiftKey(hist.mode, hist.key, 1); loadHistory(); });
  $("#tableToggle").addEventListener("click", (e) => {
    hist.table = !hist.table;
    $("#histTable").hidden = !hist.table;
    e.target.textContent = hist.table ? "Verberg tabel" : "Toon als tabel";
    e.target.setAttribute("aria-expanded", String(hist.table));
  });
  const f = $("#exportForm");
  const today = localDate();
  f.end.value = today;
  f.start.value = localDate(new Date(Date.now() - 30 * 86400000));
  f.addEventListener("submit", (e) => {
    e.preventDefault();
    location.href = `/api/export.csv?start=${f.start.value}&end=${f.end.value}`;
  });
}

/* ================= panelen ================= */
const pan = { day: null, data: null };
const warnIcon = () => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 2 21h20Z"/><path d="M12 10v5M12 18v.01" stroke-linecap="round"/></svg>`;
const okIcon = () => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></svg>`;
const infoIcon = () => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.01"/></svg>`;

async function loadPanels() {
  pan.data = await getJSON(`/api/panels${pan.day ? `?day=${pan.day}` : ""}`);
  pan.day = pan.data.day;
  drawPanels();
}

function drawPanels() {
  const d = pan.data;
  const shortDate = (s) => dayName(s, { day: "numeric", month: "long" });
  $("#panLabel").textContent = dayName(d.day);
  const idx = d.days.indexOf(d.day);
  $("#panPrev").disabled = idx <= 0;
  $("#panNext").disabled = idx < 0 || idx >= d.days.length - 1;

  const verdict = (cls, icon, title, text) => `<div class="verdict ${cls}">${icon}<div><h2>${title}</h2><p>${text}</p></div></div>`;
  const low = d.panels.filter((p) => p.status === "low");
  const lowText = (p) => (p.reason === "dead" ? `Paneel ${esc(p.name)} leverde (bijna) niets` : `Paneel ${esc(p.name)} bleef achter`);
  const measurable = d.panels.filter((p) => p.status !== "skip");
  let v;
  if (!d.configured) {
    v = verdict("neutral", infoIcon(), "De paneelcontrole staat uit", "Vul de SolarEdge-inloggegevens in om de opbrengst per paneel te zien.");
  } else if (d.open_alerts.length) {
    const a = d.open_alerts;
    v = verdict("bad", warnIcon(),
      a.length === 1 ? `Zonnepaneel ${esc(a[0].name)} levert te weinig op` : `${a.length} zonnepanelen leveren te weinig op`,
      a.map((x) => {
        const p = d.panels.find((q) => q.serial === x.serial);
        return p && p.reason === "dead"
          ? `Paneel ${esc(x.name)} levert sinds ${shortDate(x.opened_day)} (bijna) niets meer op.`
          : `Paneel ${esc(x.name)} wekt sinds ${shortDate(x.opened_day)} ${x.deviation != null ? `${nl0.format(Math.abs(x.deviation))}% ` : ""}minder op dan normaal.`;
      }).join(" ") +
        " Als dit zo blijft, bel dan de installateur.");
  } else if (!d.panels.length) {
    v = verdict("neutral", infoIcon(), "Nog geen paneelgegevens",
      d.error ? `Ophalen bij SolarEdge lukt niet: ${esc(d.error)}` : "De gegevens per paneel worden binnen een uur opgehaald.");
  } else if (!measurable.length) {
    v = verdict("neutral", infoIcon(), "Te weinig zon om te vergelijken", "Op zo'n donkere dag zeggen kleine verschillen niets. Deze dag telt niet mee.");
  } else if (low.length) {
    v = verdict("neutral", infoIcon(), low.map(lowText).join(", "),
      `Blijft dit ${d.consecutive_days} zonnige dagen op rij zo, dan krijg je automatisch een mail.`);
  } else {
    v = verdict("good", okIcon(), `Alle ${d.panels.length} panelen werken normaal`, "Geen enkel paneel wijkt opvallend af van de andere.");
  }
  $("#panelVerdict").innerHTML = v;

  drawRoof(d);
  $("#panHint").innerHTML = d.panels.length
    ? `${d.final ? "" : `Dag nog bezig, controle om ${esc(d.check_time)}`}`
    : "";

  const max = Math.max(1, ...d.panels.map((p) => p.wh || 0));
  const groups = {};
  for (const p of d.panels) (groups[p.grp] ||= []).push(p);
  const max2 = Math.max(max, ...d.panels.map((p) => p.reference_wh || 0));
  $("#panelBars").innerHTML = `<p class="hint"><span class="medkey"><i></i> streepje = wat dit paneel normaal haalt</span></p>` + Object.entries(groups).map(([g, ps]) => {
    return `<div class="pgroup"><h4>${esc(g)}</h4>` + ps.map((p) => {
      const exp = p.reference_wh;
      const isLow = p.status === "low";
      const dev = p.reason === "dead" ? "levert niets" : p.deviation == null ? "" : `${p.deviation > 0 ? "+" : ""}${nl0.format(p.deviation)}%`;
      return `<div class="prow${isLow ? " low" : ""}">
        <span class="nm">${esc(p.name)}${isLow ? `<span class="warn" title="Wijkt te veel af">⚠</span>` : ""}</span>
        <span class="track" aria-hidden="true"><span class="bar" style="width:${((p.wh || 0) / max2) * 100}%"></span>${exp ? `<span class="med" style="left:${(exp / max2) * 100}%"></span>` : ""}</span>
        <span class="num">${fmtKwh(p.wh)}<small>${p.status === "skip" ? "telt niet mee" : dev}</small></span>
      </div>`;
    }).join("") + `</div>`;
  }).join("");

  $("#panExplain").textContent =
    (d.layout && d.layout.length
      ? `Elke avond om ${d.check_time} wordt elk paneel vergeleken met de panelen naast hem in dezelfde rij, die dezelfde ligging hebben. ` +
        `Vaste verschillen, zoals een paneel dat altijd wat schaduw heeft, leert de app vanzelf. Levert een paneel ` +
        `${d.consecutive_days} zonnige dagen op rij ${nl0.format(d.threshold_pct)}% of meer minder op dan op zijn gewone mindere dagen, dan gaat er een mail uit. `
      : `Elke avond om ${d.check_time} wordt gekeken of er een paneel is dat (bijna) niets oplevert. `) +
    `Een paneel dat minder dan ${nl0.format(d.dead_share * 100)}% doet van de andere panelen, wordt altijd als kapot gemeld. ` +
    `Donkere dagen tellen verder niet mee.`;
  $("#alertHistory").innerHTML = d.recent_alerts.length
    ? d.recent_alerts.map((a) => `<li>Paneel ${esc(a.name)}: gemeld op ${shortDate(a.opened_day)}${a.deviation != null ? ` (${nl0.format(a.deviation)}%)` : ""}${a.closed_day ? `, weer normaal sinds ${shortDate(a.closed_day)}` : ", nog niet opgelost"}</li>`).join("")
    : "<li>Nog geen meldingen.</li>";
}

function panelTip(p) {
  const st = p.reason === "dead" ? "Levert (bijna) niets" : p.status === "low" ? "Te laag" : p.status === "skip" ? "Telt niet mee (te weinig zon)" : "Normaal";
  return `<b>Paneel ${esc(p.name)}</b>` +
    tipRow("var(--grid)", "Opbrengst", fmtKwh(p.wh)) +
    (p.reference_wh ? tipRow("var(--ink)", "Normaal voor dit paneel", fmtKwh(p.reference_wh)) : "") +
    (p.deviation != null && p.reason !== "dead" ? tipRow("var(--ink)", "Verschil", `${p.deviation > 0 ? "+" : ""}${nl0.format(p.deviation)}%`) : "") +
    `<div class="sep"></div>${st}${p.compared_with ? `, vergeleken met ${esc(p.compared_with)}` : ""}`;
}

function drawRoof(d) {
  const roof = $("#roof");
  const wrap = $("#panelListWrap");
  const byName = Object.fromEntries(d.panels.map((p) => [p.name, p]));
  if (!d.layout || !d.layout.length || !d.panels.length) {
    roof.innerHTML = d.panels.length ? `<p class="hint">Stel <code>PANEL_LAYOUT</code> in om de panelen te tonen zoals ze op het dak liggen, en om ze eerlijk met hun buren te vergelijken.</p>` : "";
    wrap.open = true;
    return;
  }
  const placed = new Set(d.layout.flat());
  const rest = d.panels.filter((p) => !placed.has(p.name)).map((p) => p.name);
  const rows = [...d.layout, ...(rest.length ? [rest] : [])];
  const max = Math.max(1, ...d.panels.map((p) => p.wh || 0));
  roof.innerHTML = rows.map((row, i) => {
    const label = i >= d.layout.length ? `<p class="roof-label">Niet ingedeeld</p>` : "";
    return label + `<div class="roof-row">` + row.map((name) => {
      const p = byName[name];
      if (!p) return `<div class="pv skip" style="--lvl:.1"><span class="wh">?</span><span class="nm">${esc(name)}</span></div>`;
      const lvl = 0.22 + 0.78 * ((p.wh || 0) / max);
      const cls = p.reason === "dead" ? "dead" : p.status === "low" ? "low" : p.status === "skip" ? "skip" : "";
      const ink = cls === "dead" ? "" : lvl > 0.62 ? "--pv-ink:#fff;" : "";
      const sub = p.reason === "dead" ? "<small>⚠ levert niets</small>" : p.status === "low" ? `<small>⚠ ${nl0.format(p.deviation)}%</small>` : "";
      const wh = (p.wh || 0) >= 1000 ? `${nl2.format(p.wh / 1000)} kWh` : `${nl0.format(p.wh || 0)} Wh`;
      return `<button class="pv ${cls}" style="--lvl:${lvl.toFixed(3)};${ink}" data-serial="${esc(p.serial)}" aria-label="Paneel ${esc(p.name)}: ${wh}">
        <span class="wh"><span>${wh}${sub}</span></span><span class="nm">${esc(p.name)}</span></button>`;
    }).join("") + `</div>`;
  }).join("") + `<div class="roof-legend"><span>minder</span><span class="ramp"></span><span>meer opbrengst</span><span><i class="warnkey"></i> wijkt af</span></div>`;
  wrap.open = false;
  roof.querySelectorAll(".pv[data-serial]").forEach((b) => {
    const p = d.panels.find((x) => x.serial === b.dataset.serial);
    const show = (x, y) => { showTip(panelTip(p), x, y); tipOwner = b; };
    const anchor = () => { const r = b.getBoundingClientRect(); return [r.left + r.width / 2, r.bottom + 8]; };
    // Hoe begon de aanraking? Safari op iPhone geeft de klik daarna door alsof het een muis is.
    let startedWith = "";
    b.addEventListener("pointerdown", (ev) => { startedWith = ev.pointerType || ""; });
    b.addEventListener("pointermove", (ev) => { if (ev.pointerType === "mouse") show(ev.clientX, ev.clientY); });
    b.addEventListener("pointerleave", (ev) => { if (ev.pointerType === "mouse") hideTip(); });
    b.addEventListener("click", () => {
      const viaMouse = startedWith === "mouse" && matchMedia("(hover: hover)").matches;
      startedWith = "";
      if (viaMouse) return;  // muis: zweven regelt het al
      if (tipOwner === b && !tip.hidden) { hideTip(); return; }  // nog een tik = sluiten
      show(...anchor());
    });
    b.addEventListener("keydown", (ev) => { if (ev.key === "Escape") hideTip(); });
    b.addEventListener("focus", () => { if (b.matches(":focus-visible")) show(...anchor()); });
    b.addEventListener("blur", hideTip);
  });
}

function initPanels() {
  $("#panPrev").addEventListener("click", () => { const i = pan.data.days.indexOf(pan.day); if (i > 0) { pan.day = pan.data.days[i - 1]; loadPanels(); } });
  $("#panNext").addEventListener("click", () => { const i = pan.data.days.indexOf(pan.day); if (i < pan.data.days.length - 1) { pan.day = pan.data.days[i + 1]; loadPanels(); } });
}

/* ================= meldingen ================= */
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const isStandalone = () => window.navigator.standalone === true || matchMedia("(display-mode: standalone)").matches;
const pushSupported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
let swReg = null;
let pushCfg = null;

async function registerSW() {
  if (!("serviceWorker" in navigator)) return null;
  try { swReg = await navigator.serviceWorker.register("/sw.js", { scope: "/" }); } catch (e) { swReg = null; }
  return swReg;
}

function b64ToBytes(s) {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

const postJSON = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
  .then(async (r) => { const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.detail || r.status); return d; });

function pushMsg(text, err = false) {
  const m = $("#pushMsg");
  if (m) { m.textContent = text; m.className = "push-msg" + (err ? " err" : ""); }
}

async function renderPush() {
  const box = $("#pushBox");
  if (!pushCfg) pushCfg = await getJSON("/api/push/config").catch(() => null);
  if (!pushCfg || !pushCfg.enabled) {
    box.innerHTML = `<p>Pushmeldingen staan uit op de server (<code>PUSH_ENABLED</code>).</p>`;
    return;
  }
  if (isIOS && !isStandalone()) {
    box.innerHTML = `<h3>Eerst de app op je beginscherm zetten</h3>
      <p>Op een iPhone kunnen meldingen alleen binnenkomen als Energie Thuis als app op het beginscherm staat.</p>
      <ol>
        <li>Tik in Safari op de <strong>drie puntjes (•••)</strong> naast de adresbalk en kies <strong>Deel</strong>. Op een oudere iPhone tik je direct op de deelknop (het vierkantje met het pijltje omhoog).</li>
        <li>Scroll omlaag en kies <strong>Zet op beginscherm</strong>. Laat <strong>Open als webapp</strong> aan staan en tik op <strong>Voeg toe</strong>.</li>
        <li>Open Energie Thuis via het nieuwe icoon en kom terug naar dit scherm (het belletje rechtsboven).</li>
      </ol>
      <p>Zie je "Zet op beginscherm" niet? Scroll in het deelmenu helemaal naar beneden, tik op <strong>Wijzig taken</strong> en voeg het toe. Gebruik Safari zelf, niet de ingebouwde browser van WhatsApp of Mail.</p>`;
    return;
  }
  if (!pushSupported()) {
    box.innerHTML = `<p>Deze browser ondersteunt geen pushmeldingen. Gebruik op een iPhone Safari (iOS 16.4 of nieuwer) en zet de app op het beginscherm.</p>`;
    return;
  }
  const reg = swReg || (await registerSW()) || (await navigator.serviceWorker.ready);
  const sub = await reg.pushManager.getSubscription();
  let prefs = null;
  if (sub) prefs = (await postJSON("/api/push/status", { endpoint: sub.endpoint }).catch(() => ({}))).prefs;

  if (!sub || !prefs) {
    const denied = Notification.permission === "denied";
    box.innerHTML = `<h3>Meldingen ontvangen</h3>
      <p>Krijg elke ochtend het goedkoopste moment voor de vaatwasser en wasmachine, een seintje als er een zonnepaneel kapot is, en af en toe een leuk record.</p>
      ${denied ? `<p class="push-msg err">Meldingen zijn geblokkeerd voor deze app. Zet ze aan via Instellingen → Meldingen → Energie, en probeer het opnieuw.</p>` : ""}
      <div class="btns"><button class="btn" id="pushOn" ${denied ? "disabled" : ""}>Meldingen aanzetten</button></div>
      <div class="push-msg" id="pushMsg"></div>`;
    $("#pushOn").addEventListener("click", async (e) => {
      e.target.disabled = true;
      pushMsg("Even geduld…");
      try {
        const perm = await Notification.requestPermission();
        if (perm !== "granted") throw new Error("Je hebt geen toestemming gegeven voor meldingen.");
        const s = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(pushCfg.public_key) });
        const label = isIOS ? "iPhone" : /Android/.test(navigator.userAgent) ? "Android" : "Computer";
        await postJSON("/api/push/subscribe", { subscription: s.toJSON(), label });
        await postJSON("/api/push/test", { endpoint: s.endpoint });
        renderPush();
      } catch (err) {
        e.target.disabled = false;
        pushMsg(`Aanzetten lukte niet: ${err.message || err}`, true);
      }
    });
    return;
  }

  box.innerHTML = `<h3>Meldingen staan aan</h3>
    <p>Kies welke meldingen je op deze telefoon wilt krijgen.</p>
    <ul class="switches">${pushCfg.kinds.map((k) => `<li><span>${esc(k.label)}</span>
      <label class="switch"><input type="checkbox" data-kind="${k.key}" ${prefs[k.key] ? "checked" : ""} aria-label="${esc(k.label)}"><span></span></label></li>`).join("")}</ul>
    <div class="btns">
      <button class="btn ghost" id="pushTest">Stuur een testmelding</button>
      <button class="btn ghost" id="pushMorning">Stuur de ochtendtip nu</button>
      <button class="btn ghost" id="pushOff">Meldingen uitzetten</button>
    </div>
    <div class="push-msg" id="pushMsg"></div>`;
  box.querySelectorAll("input[data-kind]").forEach((i) => i.addEventListener("change", async () => {
    try {
      await postJSON("/api/push/prefs", { endpoint: sub.endpoint, prefs: { [i.dataset.kind]: i.checked } });
      pushMsg("Opgeslagen.");
    } catch (err) { i.checked = !i.checked; pushMsg(`Opslaan lukte niet: ${err.message}`, true); }
  }));
  $("#pushTest").addEventListener("click", () => postJSON("/api/push/test", { endpoint: sub.endpoint })
    .then(() => pushMsg("Testmelding verstuurd.")).catch((err) => pushMsg(`Versturen lukte niet: ${err.message}`, true)));
  $("#pushMorning").addEventListener("click", () => postJSON("/api/push/test", { endpoint: sub.endpoint, kind: "morning" })
    .then(() => pushMsg("Ochtendtip verstuurd.")).catch((err) => pushMsg(`Versturen lukte niet: ${err.message}`, true)));
  $("#pushOff").addEventListener("click", async () => {
    await postJSON("/api/push/unsubscribe", { endpoint: sub.endpoint }).catch(() => {});
    await sub.unsubscribe().catch(() => {});
    renderPush();
  });
}

/* ================= navigatie ================= */
function route() {
  const view = (location.hash || "#nu").slice(1);
  const views = ["nu", "geschiedenis", "panelen", "meldingen"];
  const v = views.includes(view) ? view : "nu";
  for (const id of views) $(`#view-${id}`).hidden = id !== v;
  document.querySelectorAll("a[data-view]").forEach((a) => (a.dataset.view === v ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  hideTip();
  if (live) renderLive(live);
  if (v === "nu") { loadCurve().catch(() => {}); loadPrices().catch(() => {}); }
  if (v === "geschiedenis") loadHistory().catch(() => {});
  if (v === "panelen") loadPanels().catch(() => {});
  if (v === "meldingen") renderPush().catch((e) => { $("#pushBox").textContent = `Laden lukte niet: ${e.message || e}`; });
}

window.addEventListener("hashchange", route);
let resizeT;
window.addEventListener("resize", () => {
  clearTimeout(resizeT);
  resizeT = setTimeout(() => { drawCurve(); drawPrices(); if (!$("#view-geschiedenis").hidden) drawHistory(); }, 150);
});

registerSW();
initHistory();
initPanels();
route();
pollLive();
setInterval(() => { if (!document.hidden) pollLive(); }, 5000);
setInterval(() => { if (!document.hidden && !$("#view-nu").hidden) { loadCurve().catch(() => {}); loadPrices().catch(() => {}); } }, 60000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollLive(); });
