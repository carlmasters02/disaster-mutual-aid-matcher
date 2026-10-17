'use strict';
/* Tsunagu frontend — vanilla JS + vendored Leaflet/Chart.js. State arrives
   as a WS snapshot, then typed events (patch/wave/rematch/dispatch/escalation/report). */

const $ = (id) => document.getElementById(id);
const NEED_GLYPH = [['oxygen', 'O₂'], ['bedridden', '🛏'], ['wheelchair', '♿'], ['walker', '🚶'],
  ['infant', '👶'], ['dementia', '🧠'], ['visual', '🦯'], ['hearing', '🦻']];
const NEED_JA = { wheelchair: '車椅子', walker: '歩行器', bedridden: '寝たきり', dementia: '認知症', visual: '視覚',
  hearing: '聴覚', oxygen: '在宅酸素', dialysis: '透析', infant: '乳幼児', non_japanese_speaker: '日本語以外', pet: 'ペット' };
const FLAG = { ja: '🇯🇵', en: '🇬🇧', zh: '🇨🇳', vi: '🇻🇳', ko: '🇰🇷', ne: '🇳🇵', pt: '🇧🇷' };
const STATUS_JA = { covered: '確保 Covered', partial: '一部 Partial', uncovered: '未確保 Uncovered',
  escalated: '公的機関へ Escalated', unmatched: '—' };
const SLOT_LABEL = { weekday_night: '02:00', weekday_day: '14:00' };
const ROLE_JA = { neighbor: '近隣住民', student: '学生', shop_owner: '商店主', foreign_resident: '外国人住民' };

const S = {
  people: {}, helpers: {}, shelters: {}, ints: {}, roads: {}, statuses: {}, assignments: [], evacuations: [],
  slot: 'weekday_night', selected: null, arming: false, quake: null, autoTimer: null,
  series: { weekday_night: Array(6).fill(null), weekday_day: Array(6).fill(null) },
  gaps: null, gapTab: 'slot', docTab: 'ja',
};
const roadLines = {}, personMarkers = {}, helperMarkers = {}, shelterMarkers = {};
let map, L_roads, L_people, L_helpers, L_shelters, L_sel, L_crit, L_epi, chart, gapsTimer;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, opts = {}) {
  const o = { ...opts, headers: { 'Content-Type': 'application/json' } };
  if (o.body && typeof o.body !== 'string') o.body = JSON.stringify(o.body);
  const r = await fetch(path, o);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

/* ------------------------------------------------------------------ map */
function initMap(center) {
  map = L.map('map', { zoomControl: true }).setView(center, 16);
  const noTiles = new URLSearchParams(location.search).has('notiles');
  if (!noTiles) {
    const tiles = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
      { maxZoom: 19, opacity: 0.3, attribution: '© OpenStreetMap' });
    let errs = 0;
    tiles.on('tileerror', () => { if (++errs === 3) { map.removeLayer(tiles); $('map').classList.add('no-tiles'); } });
    tiles.addTo(map);
  } else $('map').classList.add('no-tiles');
  L_roads = L.layerGroup().addTo(map);
  L_crit = L.layerGroup().addTo(map);
  L_sel = L.layerGroup().addTo(map);
  L_helpers = L.layerGroup().addTo(map);
  L_shelters = L.layerGroup().addTo(map);
  L_people = L.layerGroup().addTo(map);
  L_epi = L.layerGroup().addTo(map);
  L.control.layers(null, { '道路 Roads': L_roads, '協力者 Helpers': L_helpers, '避難所 Shelters': L_shelters, '住民 People': L_people },
    { collapsed: true }).addTo(map);
  const legend = L.control({ position: 'bottomleft' });
  legend.onAdd = () => {
    const d = L.DomUtil.create('div', 'legend');
    d.innerHTML = '<span class="dot mk-covered"></span>確保 <span class="dot mk-partial"></span>一部/予備なし ' +
      '<span class="dot mk-uncovered"></span>未確保 <span class="dot mk-escalated"></span>公的機関へ<br>' +
      '<span class="ln" style="background:#6d0f0f"></span>閉塞 <span class="ln" style="background:#ff6a00"></span>火災 ' +
      '<span class="dot" style="background:#2255a4;width:8px;height:8px"></span>協力者';
    return d;
  };
  legend.addTo(map);
  map.on('click', onMapClick);
}

function roadStyle(r) {
  if (r.on_fire) return { color: '#ff6a00', weight: 6, opacity: 0.95 };
  if (r.blocked) return { color: '#6d0f0f', weight: 6, opacity: 0.95 };
  return { color: '#8f8a80', weight: r.width_m >= 6 ? 4 : r.width_m >= 4 ? 3 : 2, opacity: 0.65 };
}

function setRoadState(rid, patch) {
  const id = rid.endsWith('r') ? rid.slice(0, -1) : rid;
  const r = S.roads[id];
  if (!r) return;
  Object.assign(r, patch);
  const line = roadLines[id];
  if (line) {
    line.setStyle(roadStyle(r));
    const el = line.getElement();
    if (el) el.classList.toggle('road-fire', !!r.on_fire);
    if (r.blocked || r.on_fire) line.bringToFront();
  }
}

function personIcon(p) {
  const st = S.statuses[p.id] || p.status || 'unmatched';
  const g = (NEED_GLYPH.find(([n]) => p.needs.includes(n)) || [null, ''])[1];
  const sel = S.selected === p.id ? ' sel' : '';
  return L.divIcon({ className: '', html: `<div class="mk mk-person mk-${st}${sel}">${g}</div>`, iconSize: [24, 24], iconAnchor: [12, 12] });
}

function inboundCounts() {
  const c = {};
  for (const e of S.evacuations) c[e.shelter_id] = (c[e.shelter_id] || 0) + 1;
  return c;
}

function shelterIcon(s, inbound) {
  const tot = s.occupancy + (inbound || 0);
  const pct = Math.min(100, Math.round((100 * tot) / s.capacity));
  const over = tot > s.capacity;
  return L.divIcon({
    className: '',
    html: `<div class="shelter-wrap"><div class="mk mk-shelter${s.damaged ? ' damaged' : ''}">${s.kind === '福祉避難所' ? '🏥' : '🏫'}</div>` +
      `<div class="shelter-bar${over ? ' over' : ''}"><i style="width:${pct}%"></i></div></div>`,
    iconSize: [46, 40], iconAnchor: [23, 16],
  });
}

function shelterTip(s, inbound) {
  return `<b>${esc(s.name)}</b> (${s.kind})<br>収容 ${s.occupancy + (inbound || 0)} / ${s.capacity}` +
    `${s.accessible ? ' ♿' : ''}${s.has_generator ? ' 🔋' : ''}${s.damaged ? '<br><b style="color:#c62828">被災・使用不可</b>' : ''}`;
}

function renderAll(snap) {
  S.people = {}; S.helpers = {}; S.shelters = {}; S.ints = {}; S.roads = {};
  snap.people.forEach((p) => (S.people[p.id] = p));
  snap.helpers.forEach((h) => (S.helpers[h.id] = h));
  snap.shelters.forEach((s) => (S.shelters[s.id] = s));
  snap.intersections.forEach((i) => (S.ints[i.id] = i));
  snap.roads.forEach((r) => (S.roads[r.id] = r));
  S.statuses = Object.fromEntries(snap.people.map((p) => [p.id, p.status]));
  S.assignments = snap.assignments; S.evacuations = snap.evacuations; S.slot = snap.slot;
  [L_roads, L_people, L_helpers, L_shelters, L_sel, L_crit, L_epi].forEach((l) => l.clearLayers());
  for (const k of [roadLines, personMarkers, helperMarkers, shelterMarkers]) for (const id in k) delete k[id];

  for (const r of Object.values(S.roads)) {
    const a = S.ints[r.from_id], b = S.ints[r.to_id];
    const line = L.polyline([[a.lat, a.lon], [b.lat, b.lon]], roadStyle(r)).addTo(L_roads);
    line.bindTooltip(`${r.id} 幅${r.width_m}m · ブロック塀 ${Math.round(r.block_wall_risk * 100)}% · 木密 ${Math.round(r.wooden_density * 100)}%`, { sticky: true });
    roadLines[r.id] = line;
    if (r.on_fire) setTimeout(() => setRoadState(r.id, {}), 0);
  }
  for (const h of Object.values(S.helpers)) {
    helperMarkers[h.id] = L.circleMarker([h.lat, h.lon], helperStyle(h)).addTo(L_helpers)
      .bindTooltip(`${esc(h.display_name)} · ${ROLE_JA[h.role] || h.role} · ${h.languages.map((l) => FLAG[l]).join('')}`);
  }
  const inb = inboundCounts();
  for (const s of Object.values(S.shelters)) {
    shelterMarkers[s.id] = L.marker([s.lat, s.lon], { icon: shelterIcon(s, inb[s.id]), zIndexOffset: 500 }).addTo(L_shelters)
      .bindTooltip(shelterTip(s, inb[s.id]));
  }
  for (const p of Object.values(S.people)) addPersonMarker(p);

  setMetrics(snap.coverage);
  setSlotButtons(snap.slot);
  $('dispatch-feed').innerHTML = '';
  (snap.dispatches || []).forEach(addDispatch);
  $('escalation-feed').innerHTML = '';
  (snap.escalations || []).forEach(addEscalation);
  if (!(snap.escalations || []).length) $('escalation-feed').innerHTML = '<span class="muted">なし / none</span>';
  setPower(snap.power_out);
  syncQuake(snap.quake, snap.coverage);
  if (S.selected && S.people[S.selected]) showPerson(S.selected); else clearSelection();
  scheduleGaps();
}

function helperStyle(h) {
  const off = !h.available || h.injured || !h.availability.includes(S.slot);
  return { radius: 4, color: '#fff', weight: 1, fillColor: h.injured ? '#c62828' : off ? '#b9b4a9' : '#2255a4', fillOpacity: off ? 0.5 : 0.9 };
}

function addPersonMarker(p) {
  const m = L.marker([p.lat, p.lon], { icon: personIcon(p), zIndexOffset: 1000 }).addTo(L_people);
  m.on('click', (e) => { L.DomEvent.stopPropagation(e); showPerson(p.id); });
  m.bindTooltip(`${p.id} · ${p.block} · ${p.needs.map((n) => NEED_JA[n] || n).join('・') || '歩行'}`);
  personMarkers[p.id] = m;
}

function refreshPeople() {
  for (const p of Object.values(S.people)) personMarkers[p.id]?.setIcon(personIcon(p));
}

function refreshShelters() {
  const inb = inboundCounts();
  for (const s of Object.values(S.shelters)) {
    shelterMarkers[s.id]?.setIcon(shelterIcon(s, inb[s.id]));
    shelterMarkers[s.id]?.setTooltipContent(shelterTip(s, inb[s.id]));
  }
}

function refreshHelpers() {
  for (const h of Object.values(S.helpers)) helperMarkers[h.id]?.setStyle(helperStyle(h));
}

/* ------------------------------------------------------------ top bar */
function setMetrics(c) {
  if (!c) return;
  $('m-cov').textContent = `${c.pct_covered}%`;
  $('m-unc').textContent = c.uncovered;
  $('m-eta').textContent = `${c.avg_eta_min}分`;
  $('m-cov').className = c.pct_covered >= 85 ? 'good' : c.pct_covered >= 65 ? 'mid' : 'bad';
}

function setSlotButtons(slot) {
  $('slot-night').classList.toggle('active', slot === 'weekday_night');
  $('slot-day').classList.toggle('active', slot === 'weekday_day');
}

function setPower(on) {
  $('power-banner').classList.toggle('hidden', !on);
}

function banner(text, ms = 3500) {
  const b = $('quake-banner');
  b.textContent = text;
  b.classList.remove('hidden');
  clearTimeout(banner._t);
  banner._t = setTimeout(() => b.classList.add('hidden'), ms);
}

async function setSlot(slot) {
  stopAuto();
  try { await api('/api/timeslot', { method: 'POST', body: { slot } }); } catch (e) { banner('⚠ ' + e.message); }
}

/* ----------------------------------------------------------- person card */
function clearSelection() {
  S.selected = null;
  L_sel.clearLayers();
  $('person-detail').innerHTML = '<span class="muted">地図上の住民をクリック / Click a person on the map</span>';
}

async function showPerson(pid, preloaded) {
  const prev = S.selected;
  S.selected = pid;
  if (prev && personMarkers[prev]) personMarkers[prev].setIcon(personIcon(S.people[prev]));
  if (personMarkers[pid]) personMarkers[pid].setIcon(personIcon(S.people[pid]));
  if (!preloaded) $('person-detail').innerHTML = '<span class="muted">個別避難計画を作成中… drafting plan</span>';
  let d = preloaded;
  try { d = d || await api(`/api/person/${pid}`); } catch (e) { $('person-detail').textContent = e.message; return; }
  if (S.selected !== pid) return;
  renderPersonCard(d);
  drawSelection(d);
}

function renderPersonCard(d) {
  const p = d.person;
  const cur = d.plans[0] || {};
  const helpers = d.helpers.map((h) => `<li><b>${h.as === 'primary' ? '主' : '予備'}</b> ${esc(h.name)} <span class="muted">(${ROLE_JA[h.role] || h.role} · ${h.languages.map((l) => FLAG[l]).join('')} · ${h.eta_min}分)</span></li>`).join('')
    || '<li class="bad-text">協力者なし No helper</li>';
  const ev = d.evacuation ? `${esc(d.evacuation.shelter_name)} <span class="muted">(${d.evacuation.shelter_kind} · 徒歩${d.evacuation.eta_min}分)</span>` : '<span class="bad-text">到達不能 Unreachable</span>';
  const why = d.escalation ? `<div class="esc"><b>⚠ 公的機関へ</b> ${esc(d.escalation.explanation)}</div>`
    : d.uncovered ? `<div class="muted">理由: ${esc(d.uncovered.reason)}</div>` : '';
  const hist = d.plans.map((pl, i) => `<span class="ver ${pl.status}${i === 0 ? ' cur' : ''}" title="${pl.created_at}">v${pl.version} ${pl.status}</span>`).join(' ← ');
  const resLabel = `${FLAG[cur.doc_language] || ''} 本人向け${cur.doc_language === 'ja' ? '（やさしい日本語）' : ''}`;
  $('person-detail').innerHTML = `
    <div class="ph"><b>${esc(p.display_name)}</b> <span class="muted">${p.id} · ${p.block}</span>
      <span class="pill mk-${d.status}">${STATUS_JA[d.status] || d.status}</span></div>
    <div class="kv"><span>${p.age}歳 · ${p.floor}階${p.lives_alone ? ' · 一人暮らし' : ''}</span><span>${FLAG[p.language]} 優先度 <b>${p.priority}</b></span></div>
    <div>${p.needs.map((n) => `<span class="chip">${NEED_JA[n] || n}</span>`).join('') || '<span class="chip">歩行</span>'}
      <span class="chip warn">必要人数 ${p.helpers_required}</span></div>
    <ul class="hl">${helpers}</ul>
    <div class="kv"><span>避難先</span><span>${ev}</span></div>
    ${why}
    <div class="doc-tabs"><button class="doc-tab ${S.docTab === 'ja' ? 'active' : ''}" data-doc="ja">🇯🇵 公式計画</button>
      <button class="doc-tab ${S.docTab === 'res' ? 'active' : ''}" data-doc="res">${resLabel}</button>
      ${cur.fallback ? '<span class="fallback-badge" title="LLM unavailable — deterministic template">template</span>' : '<span class="ai-badge">ai&amp; LLM</span>'}</div>
    <div class="plan-doc">${esc(S.docTab === 'ja' ? cur.doc_ja : cur.doc_resident_lang) || '—'}</div>
    <div class="hist">版履歴: ${hist}</div>
    ${p.notes_freeform ? `<details><summary class="muted">申込メモ</summary><div class="muted">${esc(p.notes_freeform)}</div></details>` : ''}`;
  $('person-detail').querySelectorAll('.doc-tab').forEach((b) => b.addEventListener('click', () => {
    S.docTab = b.dataset.doc;
    renderPersonCard(d);
  }));
}

function drawSelection(d) {
  L_sel.clearLayers();
  const p = d.person;
  for (const h of d.helpers) {
    L.polyline([[h.lat, h.lon], [p.lat, p.lon]], { color: h.as === 'primary' ? '#2e7d32' : '#e65100', weight: 3, dashArray: '6 6' }).addTo(L_sel);
    L.circleMarker([h.lat, h.lon], { radius: 7, color: '#fff', weight: 2, fillColor: h.as === 'primary' ? '#2e7d32' : '#e65100', fillOpacity: 1 })
      .bindTooltip(`${esc(h.name)} (${h.as})`).addTo(L_sel);
  }
  if (d.evacuation) {
    const pts = [[p.lat, p.lon], ...d.evacuation.route_ids.map((id) => [S.ints[id].lat, S.ints[id].lon])];
    const sh = S.shelters[d.evacuation.shelter_id];
    if (sh) pts.push([sh.lat, sh.lon]);
    L.polyline(pts, { color: '#ffffff', weight: 9, opacity: 0.9 }).addTo(L_sel);
    L.polyline(pts, { color: '#2255a4', weight: 5 }).addTo(L_sel);
  }
}

/* ------------------------------------------------------------- intake */
async function doIntake() {
  const text = $('intake-text').value.trim();
  if (!text) return;
  $('intake-btn').disabled = true;
  $('intake-result').innerHTML = '<span class="muted">解析中… parsing (ai&)</span>';
  try {
    const r = await api('/api/intake', { method: 'POST', body: { text } });
    const pz = r.parsed;
    $('intake-result').innerHTML = `${r.parsed_fallback ? '<span class="fallback-badge">keyword rules</span>' : '<span class="ai-badge">ai&amp; LLM</span>'}
      ${pz.needs.map((n) => `<span class="chip">${NEED_JA[n] || n}</span>`).join('')}
      <span class="chip">移動: ${pz.mobility}</span><span class="chip">${pz.floor}階</span>
      ${pz.lives_alone ? '<span class="chip">一人暮らし</span>' : ''}<span class="chip">${FLAG[pz.language]} ${pz.language}</span>
      <span class="chip warn">必要人数 ${pz.helpers_required}</span> → <b>${r.person.id}</b>`;
    map.flyTo([r.person.lat, r.person.lon], 17, { duration: 0.8 });
    showPerson(r.person.id, r);
  } catch (e) { $('intake-result').textContent = '⚠ ' + e.message; }
  $('intake-btn').disabled = false;
}

/* ---------------------------------------------------------------- feeds */
function addDispatch(d) {
  const el = document.createElement('div');
  el.className = 'msg';
  el.innerHTML = `<div class="meta">${FLAG[d.lang] || ''} → ${esc(d.helper_name)} · ${esc(d.person_id)}${d.fallback ? '<span class="fallback-badge">template</span>' : ''}</div>${esc(d.text)}`;
  el.addEventListener('click', () => showPerson(d.person_id));
  const feed = $('dispatch-feed');
  feed.prepend(el);
  while (feed.children.length > 40) feed.lastChild.remove();
}

function addEscalation(e) {
  const feed = $('escalation-feed');
  if (feed.querySelector('.muted')) feed.innerHTML = '';
  let el = feed.querySelector(`[data-pid="${e.person_id}"]`);
  if (!el) {
    el = document.createElement('div');
    el.className = 'esc';
    el.dataset.pid = e.person_id;
    el.addEventListener('click', () => showPerson(e.person_id));
    feed.prepend(el);
  }
  el.innerHTML = `<b>🚨 ${esc(e.person_id)}</b> 優先度${e.priority} · ${esc(e.reason)}${e.fallback ? '<span class="fallback-badge">template</span>' : ''}<br>${esc(e.explanation)}`;
}

/* ---------------------------------------------------------------- chart */
function initChart() {
  chart = new Chart($('cov-chart'), {
    type: 'line',
    data: {
      labels: ['平常', '第1波', '第2波', '第3波', '第4波', '第5波'],
      datasets: [
        { label: '🌙 02:00', data: S.series.weekday_night, borderColor: '#2255a4', backgroundColor: '#2255a4', tension: 0.25, spanGaps: true, borderWidth: 3 },
        { label: '☀️ 14:00', data: S.series.weekday_day, borderColor: '#e65100', backgroundColor: '#e65100', tension: 0.25, spanGaps: true, borderWidth: 3 },
      ],
    },
    options: {
      animation: { duration: 400 }, maintainAspectRatio: false,
      scales: { y: { min: 0, max: 100, title: { display: true, text: 'カバー率 %' } } },
      plugins: { legend: { position: 'top', labels: { boxWidth: 14 } } },
    },
  });
}

function setSeries(slot, arr) {
  const target = S.series[slot];
  for (let i = 0; i < 6; i++) target[i] = arr[i] ?? null;
  chart.update();
}

/* ---------------------------------------------------------------- quake */
function syncQuake(q, cov) {
  S.quake = q;
  L_epi.clearLayers();
  if (!q) {
    setSeries(S.slot, [cov ? cov.pct_covered : null]);
    $('q-step').disabled = $('q-auto').disabled = true;
    $('q-compare').disabled = true;
    return;
  }
  const { epicenter_lat: lat, epicenter_lon: lon, magnitude } = q.params;
  L.circle([lat, lon], { radius: 600, color: '#c62828', weight: 1, fillOpacity: 0.06 }).addTo(L_epi);
  L.marker([lat, lon], { icon: L.divIcon({ className: '', html: '<div class="mk mk-epi">💥</div>', iconSize: [34, 34], iconAnchor: [17, 17] }) })
    .bindTooltip(`震央 M${magnitude}`).addTo(L_epi);
  setSeries(q.slot, q.series);
  if (q.compare_series) setSeries(q.slot === 'weekday_night' ? 'weekday_day' : 'weekday_night', q.compare_series);
  $('q-step').disabled = $('q-auto').disabled = q.done;
  $('q-compare').disabled = false;
  $('btn-report').disabled = false;
}

function onMapClick(e) {
  if (!S.arming) { clearSelection(); refreshPeople(); return; }
  S.arming = false;
  $('q-arm').classList.remove('armed');
  $('q-arm').textContent = '地震 Epicenter';
  $('map').classList.remove('crosshair');
  startQuake(e.latlng.lat, e.latlng.lng);
}

async function startQuake(lat, lon) {
  const magnitude = parseFloat($('q-mag').value);
  try {
    await api('/api/quake', { method: 'POST', body: { magnitude, epicenter_lat: lat, epicenter_lon: lon } });
    banner(`💥 M${magnitude} 地震発生 — ▶ Step で第1波 / press Step`, 4000);
  } catch (e) { banner('⚠ ' + e.message); }
}

async function stepQuake() {
  if (!S.quake || S.quake.done) { stopAuto(); return; }
  $('q-step').disabled = true;
  try {
    const r = await api('/api/quake/step', { method: 'POST' });
    if (r.done) {
      stopAuto();
      $('q-step').disabled = $('q-auto').disabled = true;
      S.quake.done = true;
      setTimeout(() => banner('第5波 完了 — 比較 Compare / 報告 Report', 4000), 1800);
    } else $('q-step').disabled = false;
  } catch (e) { banner('⚠ ' + e.message); stopAuto(); $('q-step').disabled = false; }
}

function toggleAuto() {
  if (S.autoTimer) { stopAuto(); return; }
  $('q-auto').classList.add('armed');
  $('q-auto').textContent = '⏸ Pause';
  stepQuake();
  S.autoTimer = setInterval(stepQuake, 4000);
}

function stopAuto() {
  if (S.autoTimer) clearInterval(S.autoTimer);
  S.autoTimer = null;
  $('q-auto').classList.remove('armed');
  $('q-auto').textContent = '⏩ Auto';
}

async function compare() {
  $('q-compare').disabled = true;
  try {
    const r = await api('/api/quake/compare', { method: 'POST' });
    setSeries(r.slot, r.series);
    const own = r.own.series, oth = r.series;
    banner(`同じ地震: ${SLOT_LABEL[r.own.slot]} → ${own[own.length - 1]}% ／ ${SLOT_LABEL[r.slot]} → ${oth[oth.length - 1]}%`, 6000);
  } catch (e) { banner('⚠ ' + e.message); }
  $('q-compare').disabled = false;
}

/* ----------------------------------------------------------------- gaps */
function scheduleGaps() {
  clearTimeout(gapsTimer);
  gapsTimer = setTimeout(loadGaps, 900);
}

async function loadGaps() {
  try { S.gaps = await api('/api/gaps'); renderGaps(); } catch (e) { $('gap-content').textContent = e.message; }
}

function table(head, rows) {
  return `<table><tr>${head.map((h) => `<th>${h}</th>`).join('')}</tr>${rows.join('')}</table>`;
}

function renderGaps() {
  const g = S.gaps;
  if (!g) return;
  const c = $('gap-content');
  L_crit.clearLayers();
  if (S.gapTab === 'slot') {
    const sg = g.slot_gap;
    c.innerHTML = `<div class="gap-head">平常時カバー率 🌙 02:00 <b>${sg.night_pct}%</b> → ☀️ 14:00 <b class="bad-text">${sg.day_pct}%</b>（昼は通勤で協力者が不在）</div>` +
      table(['地区 Block', '昼に不足する人', '不足枠', '募集目安 Recruit'],
        sg.blocks.map((b) => `<tr><td>${b.block}</td><td>${b.people}</td><td>${b.missing_slots}</td><td><b>昼間協力者 ${b.recruit}名</b></td></tr>`));
  } else if (S.gapTab === 'spof') {
    c.innerHTML = '<div class="gap-head">この協力者が欠けると予備のない住民が孤立 / Helpers whose loss strands someone</div>' +
      table(['協力者', '役割', '予備なしの担当', '高優先'],
        g.spof_helpers.map((h) => `<tr class="clickable" data-pid="${h.stranded_if_lost[0] || ''}"><td>${esc(h.name)} <span class="muted">${h.helper_id}</span></td><td>${ROLE_JA[h.role] || h.role}</td><td>${h.stranded_if_lost.join(', ') || '—'}</td><td>${h.high_priority}</td></tr>`));
  } else if (S.gapTab === 'roads') {
    c.innerHTML = '<div class="gap-head">閉塞すると住民が孤立・大幅遅延する道路（クリックで地図表示）/ Critical roads</div>' +
      table(['道路', '幅', 'ブロック塀', '利用者', '孤立', '遅延'],
        g.critical_roads.map((r) => `<tr class="clickable" data-road="${r.road_id}"><td>${r.road_id}</td><td>${r.width_m}m</td><td>${Math.round(r.block_wall_risk * 100)}%</td><td>${r.users}</td><td class="${r.strands.length ? 'bad-text' : ''}">${r.strands.length}</td><td>${r.delays.length}</td></tr>`));
    for (const r of g.critical_roads) {
      const a = S.ints[r.from], b = S.ints[r.to];
      if (a && b) L.polyline([[a.lat, a.lon], [b.lat, b.lon]], { color: '#000', weight: 7, dashArray: '4 8', opacity: 0.8 }).addTo(L_crit);
    }
  } else {
    c.innerHTML = '<div class="gap-head">避難所の収容予測 / Shelter load forecast</div>' +
      table(['避難所', '種別', '定員', '現在', '割当', '超過'],
        g.shelter_overflow.map((s) => `<tr><td>${esc(s.name)}${s.damaged ? ' <b class="bad-text">被災</b>' : ''}</td><td>${s.kind}</td><td>${s.capacity}</td><td>${s.occupancy}</td><td>${s.inbound}</td><td class="${s.overflow > 0 ? 'bad-text' : ''}">${s.overflow > 0 ? '+' + s.overflow : s.overflow}</td></tr>`));
  }
  c.querySelectorAll('tr[data-road]').forEach((tr) => tr.addEventListener('click', () => {
    const r = g.critical_roads.find((x) => x.road_id === tr.dataset.road);
    const a = S.ints[r.from], b = S.ints[r.to];
    map.flyTo([(a.lat + b.lat) / 2, (a.lon + b.lon) / 2], 18, { duration: 0.6 });
  }));
  c.querySelectorAll('tr[data-pid]').forEach((tr) => tr.addEventListener('click', () => tr.dataset.pid && showPerson(tr.dataset.pid)));
}

/* --------------------------------------------------------------- report */
async function requestReport() {
  $('btn-report').disabled = true;
  $('btn-report').textContent = '作成中…';
  try { await api('/api/report', { method: 'POST' }); } catch (e) { banner('⚠ ' + e.message); }
  $('btn-report').disabled = false;
  $('btn-report').textContent = '報告 Report';
}

function showReport(payload, fallback) {
  const recs = (payload.recommendations || []).map((r) => `<li>${esc(r)}</li>`).join('');
  const secs = (payload.sections || []).map((s) => `<h2>${esc(s.heading)}</h2><p>${esc(s.body)}</p>`).join('');
  $('report-content').innerHTML = `<h1>${esc(payload.title)} ${fallback ? '<span class="fallback-badge">template</span>' : '<span class="ai-badge">ai&amp; LLM</span>'}</h1>
    ${secs}<h2>提言 Recommendations</h2><ol>${recs}</ol>
    <p class="muted">要配慮個人情報は国内GPU（ai&）でのみ処理 · 関係データはNeo4jグラフに保存 · 全データ架空</p>`;
  $('report-modal').classList.remove('hidden');
}

/* ------------------------------------------------------------------- ws */
const handlers = {
  snapshot: (ev) => renderAll(ev.payload),
  rematch: (ev) => {
    S.statuses = ev.statuses; S.assignments = ev.assignments; S.evacuations = ev.evacuations; S.slot = ev.slot;
    setMetrics(ev.coverage);
    setSlotButtons(ev.slot);
    refreshPeople(); refreshShelters(); refreshHelpers();
    if (ev.wave != null && S.quake) {
      S.series[S.quake.slot][ev.wave] = ev.coverage.pct_covered;
      chart.update();
    } else if (!S.quake) setSeries(ev.slot, [ev.coverage.pct_covered]);
    if (S.selected) showPerson(S.selected);
    scheduleGaps();
  },
  wave: (ev) => {
    ev.blocked_roads.forEach((id) => setRoadState(id, { blocked: true }));
    ev.fires.forEach((id) => setRoadState(id, { on_fire: true }));
    ev.injured_helpers.forEach((id) => { if (S.helpers[id]) Object.assign(S.helpers[id], { injured: true, available: false }); });
    ev.damaged_shelters.forEach((id) => { if (S.shelters[id]) S.shelters[id].damaged = true; });
    if (S.quake) S.quake.idx = ev.n;
    banner(`第${ev.n}波 / Wave ${ev.n}/${ev.total_waves} — 道路閉塞 ${ev.blocked_roads.length / 2} · 火災 ${ev.fires.length / 2} · 負傷 ${ev.injured_helpers.length}` +
      (ev.damaged_shelters.length ? ` · 避難所被災 ${ev.damaged_shelters.length}` : ''));
  },
  patch: (ev) => {
    const p = ev.payload;
    if (p.kind === 'person_added') {
      S.people[p.person.id] = { ...p.person, status: 'unmatched' };
      addPersonMarker(S.people[p.person.id]);
    } else if (p.kind === 'wave_state') {
      p.fires_out.forEach((id) => setRoadState(id, { on_fire: false, blocked: true }));
      p.shelters.forEach((s) => (S.shelters[s.id] = s));
      refreshShelters();
      setPower(p.power_out);
    } else if (p.kind === 'compare') setSeries(p.slot, p.series);
  },
  dispatch: addDispatch,
  escalation: (ev) => { addEscalation(ev); },
  report: (ev) => showReport(ev.payload, ev.fallback),
};

function connectWS() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    try { handlers[ev.type]?.(ev); } catch (err) { console.error(ev.type, err); }
  };
  ws.onopen = () => { $('conn').className = 'conn ok'; ws._ping = setInterval(() => ws.readyState === 1 && ws.send('ping'), 20000); };
  ws.onclose = () => { $('conn').className = 'conn'; clearInterval(ws._ping); setTimeout(connectWS, 1000); };
}

/* ----------------------------------------------------------------- boot */
function wire() {
  $('slot-night').onclick = () => setSlot('weekday_night');
  $('slot-day').onclick = () => setSlot('weekday_day');
  $('q-mag').oninput = () => ($('q-mag-v').textContent = parseFloat($('q-mag').value).toFixed(1));
  $('q-arm').onclick = () => {
    S.arming = !S.arming;
    $('q-arm').classList.toggle('armed', S.arming);
    $('q-arm').textContent = S.arming ? '地図をクリック Click map' : '地震 Epicenter';
    $('map').classList.toggle('crosshair', S.arming);
  };
  $('q-golden').onclick = () => { const c = map.getCenter(); startQuake(35.701, 139.806); void c; };
  $('q-step').onclick = stepQuake;
  $('q-auto').onclick = toggleAuto;
  $('q-compare').onclick = compare;
  $('btn-reset').onclick = async () => { stopAuto(); S.series.weekday_day.fill(null); S.series.weekday_night.fill(null); await api('/api/reset', { method: 'POST', body: {} }); };
  $('btn-report').onclick = requestReport;
  $('report-close').onclick = () => $('report-modal').classList.add('hidden');
  $('report-modal').onclick = (e) => { if (e.target.id === 'report-modal') $('report-modal').classList.add('hidden'); };
  $('intake-btn').onclick = doIntake;
  $('intake-sample').onclick = () => { $('intake-text').value = '母は82歳、歩行器を使用、3階に一人暮らし、エレベーターなし。ベトナム語しか話せません。'; };
  document.querySelectorAll('.gap-tab').forEach((b) => b.addEventListener('click', () => {
    document.querySelectorAll('.gap-tab').forEach((x) => x.classList.toggle('active', x === b));
    S.gapTab = b.dataset.tab;
    renderGaps();
  }));
  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'TEXTAREA') return;
    if (e.key === ' ' || e.key === 'ArrowRight') { e.preventDefault(); stepQuake(); }
    if (e.key === 'Escape') $('report-modal').classList.add('hidden');
  });
}

window.addEventListener('error', (e) => banner(`⚠ JS: ${e.message} @${e.lineno}`, 15000));

(async function boot() {
  initMap([35.701, 139.806]);
  initChart();
  wire();
  // ?sync=1 loads the snapshot synchronously (headless screenshots / slow venues)
  const sync = new URLSearchParams(location.search).has('sync') && (() => {
    const x = new XMLHttpRequest(); x.open('GET', '/api/snapshot', false); x.send(); return JSON.parse(x.responseText);
  })();
  try { renderAll(sync || await api('/api/snapshot')); } catch (e) { console.error(e); banner('⚠ ' + e.message + ' ' + (e.stack || '').split('\n')[1], 15000); }
  connectWS();
})();
