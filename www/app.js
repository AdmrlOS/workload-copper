import { normalizeFrame, connectionState, prepareReplay, replayIndexAt, seekIndex, safeRunId, formatBytes } from './model.js';

// This client only observes native output. There is no physics, planner, or
// generated telemetry in the browser. Playback selects exact recorded frames.
const $ = (id) => document.getElementById(id);
const finite = (v) => typeof v === 'number' && Number.isFinite(v);
const fmt = (v, digits = 2) => finite(v) ? v.toFixed(digits) : '—';
const integer = (v) => Number.isInteger(v) && v >= 0 ? v.toLocaleString('en-US') : '—';
const set = (id, text) => { const element = $(id); if (element.textContent !== String(text)) element.textContent = text; };
const metric = (id, value, unit) => {
  const node = $(id);
  if (node.dataset.value === `${value}|${unit}`) return;
  node.dataset.value = `${value}|${unit}`;
  node.replaceChildren(document.createTextNode(value), Object.assign(document.createElement('small'), { textContent: unit }));
};
const clock = (seconds) => {
  if (!finite(seconds)) return '—';
  const total = Math.max(0, Math.round(seconds * 1000));
  return `${String(Math.floor(total / 60000)).padStart(2, '0')}:${String(Math.floor(total / 1000) % 60).padStart(2, '0')}.${String(total % 1000).padStart(3, '0')}`;
};
const store = {
  state: null, lastFrame: null, receivedAt: null, progressedAt: null, fetchError: null,
  mode: 'live', history: [], runId: null, recordings: [], recordingKey: '', selectedId: '',
  replay: null, replayIndex: 0, replayLoading: false, replayError: '', request: 0,
  playing: false, anchorMs: 0, anchorTime: 0, speed: 1,
};
const layers = { path: true, trace: true, lidar: true };
const field = $('field-canvas');
const fieldContext = field.getContext('2d');
const timeline = $('timeline-canvas');
const timelineContext = timeline.getContext('2d');
let dirty = true;
let fieldSize = { width: 0, height: 0 };
let timelineSize = { width: 0, height: 0 };

function currentFrame() {
  return store.mode === 'replay' ? store.replay?.frames[store.replayIndex] || null : store.lastFrame;
}
function health() {
  return connectionState(store.state, { now: performance.now(), receivedAt: store.receivedAt, progressedAt: store.progressedAt, error: store.fetchError });
}
async function getJSON(url, timeout = 5000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, { cache: 'no-store', signal: controller.signal, headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(`HTTP ${response.status} from ${url.split('/').slice(0, 3).join('/')}`);
    return await response.json();
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('The local backend did not respond in time.');
    throw error;
  } finally { clearTimeout(timer); }
}

async function poll() {
  const started = performance.now();
  try {
    const state = await getJSON('/api/state', 3500);
    if (!state || typeof state !== 'object' || typeof state.status !== 'string') throw new Error('The backend returned an invalid state response.');
    const frame = normalizeFrame(state.frame);
    const now = performance.now();
    const runId = state.run_id || null;
    const reset = runId !== store.runId || (frame && store.lastFrame && frame.tick < store.lastFrame.tick);
    if (reset) {
      store.history = [];
      store.lastFrame = null;
      store.progressedAt = null;
    }
    store.runId = runId;
    if (frame && (!store.lastFrame || frame.tick !== store.lastFrame.tick)) {
      store.progressedAt = now;
      store.history.push(frame);
      if (store.history.length > 900) store.history.splice(0, store.history.length - 900);
    }
    if (frame) store.lastFrame = frame;
    store.state = state;
    store.receivedAt = now;
    store.fetchError = null;
    syncRecordings();
  } catch (error) { store.fetchError = error.message || 'Could not reach the local backend.'; }
  update();
  // At most one in-flight poll, approximately 5 Hz including request latency.
  setTimeout(poll, Math.max(0, 200 - (performance.now() - started)));
}

function syncRecordings() {
  const rows = Array.isArray(store.state?.recordings) ? store.state.recordings.filter((r) => r && safeRunId(r.id)) : [];
  const latest = store.state?.latest_replay;
  if (latest && safeRunId(latest.id) && !rows.some((r) => r.id === latest.id)) rows.unshift({ ...latest, bytes: null, created_at: null });
  // Retention may remove a server-side run while its verified frames are loaded.
  // Keep the selected metadata paired with those frames, not a newer recording.
  if (store.mode === 'replay' && store.replay && !rows.some((r) => r.id === store.replay.id)) {
    const loaded = store.recordings.find((r) => r.id === store.replay.id);
    if (loaded) rows.unshift({ ...loaded, retained_locally: true });
  }
  store.recordings = rows;
  const key = JSON.stringify(rows.map((r) => [r.id, r.verified, r.cycles, r.mismatches, r.bytes]));
  if (key === store.recordingKey) return;
  store.recordingKey = key;
  const select = $('recording-select');
  select.replaceChildren();
  if (!rows.length) {
    select.append(new Option('No recordings yet', ''));
    store.selectedId = '';
  } else {
    for (const record of rows) {
      const verified = record.verified === true && record.mismatches === 0;
      select.append(new Option(`${record.id} · ${verified ? 'verified' : 'not verified'}`, record.id));
    }
    if (!rows.some((r) => r.id === store.selectedId)) store.selectedId = rows.find((r) => r.verified === true && r.mismatches === 0)?.id || rows[0].id;
    select.value = store.selectedId;
  }
}

function stop() { store.playing = false; }
function switchMode(mode) {
  if (mode === store.mode) return;
  stop();
  store.request += 1;
  store.replayLoading = false;
  store.mode = mode;
  store.replayError = '';
  update();
  if (mode === 'replay') loadReplay(store.selectedId);
}
async function loadReplay(id) {
  stop();
  const request = ++store.request;
  store.replay = null;
  store.replayIndex = 0;
  store.replayError = '';
  store.replayLoading = false;
  const record = store.recordings.find((r) => r.id === id);
  if (!id) store.replayError = 'No native recording is available yet. Live control can continue while the backend records and verifies a run.';
  else if (!safeRunId(id)) store.replayError = 'The backend supplied an invalid recording identifier.';
  else if (record?.verified !== true || record?.mismatches !== 0) store.replayError = 'This recording is not verified. Playback is blocked until native Copper verification succeeds with zero mismatches.';
  if (store.replayError) { update(); return; }
  store.replayLoading = true;
  update();
  try {
    const payload = await getJSON(`/api/replay/${encodeURIComponent(id)}`, 20000);
    if (request !== store.request || store.mode !== 'replay') return;
    if (payload.id !== id) throw new Error('The replay response does not match the selected recording.');
    store.replay = prepareReplay(payload);
  } catch (error) {
    if (request === store.request) store.replayError = error.message || 'The native replay could not be loaded.';
  } finally {
    if (request === store.request) { store.replayLoading = false; update(); }
  }
}
function seek(value) {
  if (!store.replay || store.mode !== 'replay') return;
  stop();
  store.replayIndex = seekIndex(value, store.replay.frames.length);
  update();
}
function togglePlay() {
  if (!store.replay || store.mode !== 'replay') return;
  if (store.playing) stop();
  else {
    if (store.replayIndex >= store.replay.frames.length - 1) store.replayIndex = 0;
    store.anchorMs = performance.now();
    store.anchorTime = store.replay.frames[store.replayIndex].time_s;
    store.playing = store.replay.frames.length > 1;
  }
  update();
}

function update() {
  const state = store.state;
  const frame = currentFrame();
  const status = health();
  const replay = store.mode === 'replay';
  const available = replay && Boolean(store.replay);
  const live = !replay && status.kind === 'live';
  $('connection').dataset.state = status.kind;
  set('connection-label', status.label);
  $('connection').title = status.detail;
  $('notice').hidden = status.kind === 'live';
  $('notice').dataset.state = status.kind;
  set('notice-title', `${status.label}.`);
  set('notice-detail', replay && available ? `${status.detail} Loaded verified replay remains available offline.` : `${status.detail}${store.lastFrame ? ' The last received field is retained, not extrapolated.' : ' No browser simulation is running.'}`);
  $('live-mode').classList.toggle('active', !replay);
  $('replay-mode').classList.toggle('active', replay);
  $('live-mode').setAttribute('aria-pressed', String(!replay));
  $('replay-mode').setAttribute('aria-pressed', String(replay));
  set('target-hz', finite(state?.target_hz) ? state.target_hz : '—');
  const source = available ? 'VERIFIED REPLAY' : replay ? (store.replayLoading ? 'LOADING REPLAY' : 'NO REPLAY LOADED') : live ? 'LIVE SIMULATION' : frame ? 'LAST RECEIVED / NOT LIVE' : 'AWAITING DATA';
  set('field-source', source);
  $('field-source').dataset.source = available ? 'replay' : live ? 'live' : frame ? 'stale' : '';
  set('telemetry-source', replay ? 'RECORDED' : live ? 'LIVE' : frame ? 'LAST FRAME' : 'NO FRAME');
  set('world-size', frame ? `FIELD ${fmt(frame.world.width, 1)} × ${fmt(frame.world.height, 1)} m` : 'FIELD — × — m');
  set('field-tick', `TICK ${integer(frame?.tick)}`);
  set('map-stamp', replay ? 'NATIVE VERIFIED REPLAY / EXACT FRAME' : live ? 'NATIVE SENSOR / PLANNER OUTPUT' : frame ? 'LAST NATIVE FRAME / NOT LIVE' : 'NATIVE SENSOR / PLANNER OUTPUT');
  $('map-empty').hidden = Boolean(frame);
  if (!frame) {
    $('map-empty').querySelector('strong').textContent = replay ? store.replayLoading ? 'Loading native replay' : 'No verified replay loaded' : 'Waiting for native telemetry';
    $('map-empty').querySelector('span').textContent = replay ? 'Only recordings verified by the native Copper backend can be played.' : 'No sensor frame has been received. The browser does not generate a simulation.';
  }
  metric('pose-position', frame ? `${fmt(frame.robot.x)} / ${fmt(frame.robot.y)}` : '— / —', 'm');
  metric('pose-heading', fmt(frame?.robot.yaw), 'rad');
  set('goal-label', frame?.goal?.label || 'Awaiting mission');
  $('goal-label').title = frame?.goal ? `${frame.goal.label} · ${fmt(frame.goal.x)}, ${fmt(frame.goal.y)} m` : '';
  metric('goals-completed', integer(frame?.goals_completed), 'goals');
  metric('command-v', fmt(frame?.command.v), 'm/s');
  metric('command-omega', fmt(frame?.command.omega), 'rad/s');
  metric('clearance', fmt(frame?.nearest_obstacle_m), 'm');
  metric('path-count', frame?.path_available ? integer(frame.path.length) : '—', 'points');
  set('pipeline-us', fmt(frame?.pipeline_us, 1));
  set('pipeline-state', frame?.planner_status ? frame.planner_status.replaceAll('_', ' ') : 'Awaiting cycles');
  set('node-sensors', frame ? `#${frame.tick}` : '—');
  set('sensor-fields', frame?.lidar.length ? 'Pose · obstacles · LiDAR' : 'Pose · obstacles');
  $('layer-lidar').disabled = !frame?.lidar.length;
  $('layer-lidar').closest('label').title = frame?.lidar.length ? `${frame.lidar.length} native LiDAR points` : 'No LiDAR points supplied in this native frame';
  $('layer-lidar').closest('label').style.opacity = frame?.lidar.length ? '1' : '.45';
  set('node-planner', frame?.path_available ? `${frame.path.length} pts` : '—');
  set('node-controller', frame?.safety_stop === true ? 'STOP' : frame?.safety_stop === false ? 'TRACK' : '—');
  set('node-telemetry', frame ? replay ? 'REPLAY' : live ? 'LIVE' : 'STALE' : '—');
  document.querySelector('.pipeline-panel').dataset.active = String(Boolean(frame) && (live || available));
  if (frame?.safety_stop === true) $('node-controller').style.color = 'var(--danger)';
  else $('node-controller').style.removeProperty('color');
  set('engine-version', `${state?.engine || 'Copper'} / ${state?.copper_version || '—'}`);
  set('architecture', state?.architecture || '—');
  set('safety-state', frame?.safety_stop === true ? 'Safety stop active' : frame?.safety_stop === false ? 'Safety clear' : 'Safety unknown');
  $('safety-dot').style.background = frame?.safety_stop === true ? 'var(--danger)' : frame?.safety_stop === false ? 'var(--green)' : 'var(--dim)';
  set('collision-count', `${integer(frame?.collisions)} collisions`);
  $('collision-count').style.color = frame?.collisions > 0 ? 'var(--danger)' : '';
  set('runtime-footer', state?.revision ? `REV ${String(state.revision).slice(0, 12)} · LOCAL SIMULATION` : 'LOCAL SIMULATION · NO HARDWARE CONNECTED');
  field.setAttribute('aria-label', frame ? `${source}. Rover at ${fmt(frame.robot.x)}, ${fmt(frame.robot.y)} meters; heading ${fmt(frame.robot.yaw)} radians. Goal ${frame.goal?.label || 'unknown'}. Tick ${frame.tick}. ${frame.safety_stop === true ? 'Safety stop active.' : ''}` : 'Warehouse simulation. Awaiting native telemetry.');
  updateReplay(frame, available);
  dirty = true;
}

function updateReplay(frame, available) {
  const replay = store.mode === 'replay';
  const record = store.recordings.find((r) => r.id === store.selectedId);
  const verified = available && store.replay.verification.verified === true;
  const verification = store.replay?.verification;
  const frames = available ? store.replay.frames : [];
  for (const id of ['replay-start', 'replay-end', 'replay-play', 'replay-scrub', 'replay-speed']) $(id).disabled = !available;
  $('recording-select').disabled = !store.recordings.length;
  $('replay-play').setAttribute('aria-label', store.playing ? 'Pause verified replay' : 'Play verified replay');
  $('play-icon').replaceChildren();
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', store.playing ? 'M4 3h4v12H4ZM11 3h4v12h-4Z' : 'm6 3 9 6-9 6Z');
  $('play-icon').append(path);
  $('replay-scrub').max = String(available ? frames.length - 1 : 0);
  $('replay-scrub').value = String(available ? store.replayIndex : 0);
  $('replay-scrub').setAttribute('aria-valuetext', available ? `Frame ${store.replayIndex + 1} of ${frames.length}, native tick ${frame.tick}, ${clock(frame.time_s)}` : 'No verified recording loaded');
  set('verification-badge', verified ? `✓ VERIFIED · ${integer(verification.cycles)} CYCLES · ${integer(verification.mismatches)} MISMATCHES` : 'NATIVE COPPER LOG');
  $('verification-badge').dataset.verified = String(verified);
  set('timeline-label', replay ? store.replayLoading ? 'LOADING NATIVE REPLAY' : available ? `${store.playing ? 'PLAYING' : 'PAUSED'} / VERIFIED REPLAY` : 'REPLAY UNAVAILABLE' : 'OBSERVED LIVE TICKS');
  set('timeline-position', frame ? replay && available ? `${store.replayIndex + 1} / ${frames.length} FRAMES · TICK ${frame.tick}` : `TICK ${integer(frame.tick)} · ${clock(frame.time_s)}` : 'Awaiting native cycles');
  set('timeline-start', available ? clock(frames[0].time_s) : store.history.length && !replay ? clock(store.history[0].time_s) : '00:00.000');
  set('timeline-end', available ? clock(frames.at(-1).time_s) : !replay && frame ? clock(frame.time_s) : '—');
  set('replay-detail', available ? `${verification.engine || 'Copper'} native verification · exact recorded frames` : replay ? 'No unverified playback.' : 'Live trail samples received at 5 Hz · native control runs independently.');
  const id = record?.id;
  const download = $('download-log');
  const downloadable = safeRunId(id) && record?.verified === true && record?.mismatches === 0 && !record?.retained_locally;
  download.classList.toggle('disabled', !downloadable);
  download.setAttribute('aria-disabled', String(!downloadable));
  download.tabIndex = downloadable ? 0 : -1;
  if (downloadable) download.href = `/api/recordings/${encodeURIComponent(id)}/download`;
  else download.removeAttribute('href');
  let meta = 'No native recording selected.';
  if (record) {
    meta = `${record.id} · ${formatBytes(record.bytes)} · ${integer(record.cycles)} native cycles · ${record.verified === true && record.mismatches === 0 ? 'verified / 0 mismatches' : 'not verified'}`;
    if (record.created_at) {
      const created = new Date(record.created_at);
      if (Number.isFinite(created.getTime())) meta += ` · ${created.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}`;
    }
  }
  set('recording-meta', meta);
  $('replay-error').hidden = !store.replayError;
  set('replay-error', store.replayError);
}

// Canvas backing stores follow their actual CSS size on every breakpoint.
function resizeCanvas(canvas, size, box) {
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  size.width = box.width;
  size.height = box.height;
  canvas.width = Math.max(1, Math.round(box.width * ratio));
  canvas.height = Math.max(1, Math.round(box.height * ratio));
  canvas.getContext('2d').setTransform(ratio, 0, 0, ratio, 0, 0);
  dirty = true;
}
new ResizeObserver((entries) => { for (const entry of entries) resizeCanvas(field, fieldSize, entry.contentRect); }).observe($('map-wrap'));
new ResizeObserver((entries) => { for (const entry of entries) resizeCanvas(timeline, timelineSize, entry.contentRect); }).observe($('replay-scrub').parentElement);

function drawField() {
  const ctx = fieldContext;
  const { width, height } = fieldSize;
  ctx.clearRect(0, 0, width, height);
  if (!width || !height) return;
  const frame = currentFrame();
  if (!frame) {
    // Neutral crosshair texture, not an invented warehouse or sensor frame.
    ctx.fillStyle = '#2a3535';
    for (let x = 20; x < width; x += 28) for (let y = 16; y < height; y += 28) ctx.fillRect(x, y, 1, 1);
    return;
  }
  const mobile = width < 500;
  const marginX = mobile ? 31 : 43;
  const marginY = mobile ? 30 : 34;
  const scale = Math.min((width - marginX * 2) / frame.world.width, (height - marginY * 2) / frame.world.height);
  const w = frame.world.width * scale;
  const h = frame.world.height * scale;
  const left = (width - w) / 2;
  const top = (height - h) / 2;
  const X = (x) => left + x * scale;
  const Y = (y) => top + h - y * scale;
  const font = 'Consolas, "Liberation Mono", monospace';
  ctx.fillStyle = '#182021';
  ctx.fillRect(left, top, w, h);
  const grid = frame.world.width > 50 || frame.world.height > 50 ? 5 : 1;
  ctx.lineWidth = 1;
  ctx.strokeStyle = '#283334';
  ctx.beginPath();
  for (let x = 0; x <= frame.world.width; x += grid) { ctx.moveTo(X(x), top); ctx.lineTo(X(x), top + h); }
  for (let y = 0; y <= frame.world.height; y += grid) { ctx.moveTo(left, Y(y)); ctx.lineTo(left + w, Y(y)); }
  ctx.stroke();
  ctx.strokeStyle = '#53615c';
  ctx.strokeRect(left, top, w, h);
  ctx.font = `8px ${font}`;
  ctx.fillStyle = '#6f827e';
  ctx.textAlign = 'center';
  const labelStep = frame.world.width > 20 ? 4 : 2;
  for (let x = 0; x <= frame.world.width; x += labelStep) ctx.fillText(String(x).padStart(2, '0'), X(x), top + h + 15);
  ctx.textAlign = 'right';
  for (let y = 0; y <= frame.world.height; y += labelStep) ctx.fillText(String(y).padStart(2, '0'), left - 9, Y(y) + 3);
  ctx.save();
  ctx.beginPath(); ctx.rect(left, top, w, h); ctx.clip();
  // Trace consists only of received poses; the 5 Hz live trail is not a full log.
  if (layers.trace) {
    const frames = store.mode === 'replay' ? store.replay.frames : store.history;
    const end = store.mode === 'replay' ? store.replayIndex : frames.length - 1;
    const stride = Math.max(1, Math.floor(end / 1200));
    ctx.beginPath(); ctx.strokeStyle = '#73988b'; ctx.globalAlpha = .65; ctx.lineWidth = 1.3; ctx.setLineDash([3, 4]);
    for (let i = 0; i <= end; i += stride) { const pose = frames[i].robot; if (!i) ctx.moveTo(X(pose.x), Y(pose.y)); else ctx.lineTo(X(pose.x), Y(pose.y)); }
    ctx.lineTo(X(frame.robot.x), Y(frame.robot.y)); ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha = 1;
  }
  if (layers.lidar && frame.lidar.length) {
    ctx.fillStyle = '#84a8a0'; ctx.globalAlpha = .55;
    for (const point of frame.lidar) ctx.fillRect(X(point.x) - 1, Y(point.y) - 1, 2, 2);
    ctx.globalAlpha = 1;
  }
  frame.obstacles.forEach((obstacle, i) => {
    const x = X(obstacle.x), y = Y(obstacle.y + obstacle.h), ow = obstacle.w * scale, oh = obstacle.h * scale;
    ctx.fillStyle = obstacle.dynamic ? '#443d2b' : '#2e3b3a';
    ctx.strokeStyle = obstacle.dynamic ? '#c8a76b' : '#5b6b66';
    ctx.lineWidth = 1;
    ctx.fillRect(x, y, ow, oh); ctx.strokeRect(x + .5, y + .5, ow - 1, oh - 1);
    if (!obstacle.dynamic) {
      ctx.strokeStyle = '#41534d'; ctx.beginPath();
      const horizontal = ow > oh;
      const extent = horizontal ? ow : oh;
      for (let offset = 10; offset < extent; offset += 12) {
        if (horizontal) { ctx.moveTo(x + offset, y + 3); ctx.lineTo(x + offset, y + oh - 3); }
        else { ctx.moveTo(x + 3, y + offset); ctx.lineTo(x + ow - 3, y + offset); }
      }
      ctx.stroke();
    } else {
      ctx.strokeStyle = '#b99c69'; ctx.beginPath();
      ctx.moveTo(x + ow * .3, y + oh); ctx.lineTo(x + ow * .3, y + oh + 5);
      ctx.moveTo(x + ow * .7, y + oh); ctx.lineTo(x + ow * .7, y + oh + 5); ctx.stroke();
    }
    if (!mobile || obstacle.dynamic) {
      ctx.font = `7px ${font}`; ctx.textAlign = 'center';
      ctx.fillStyle = obstacle.dynamic ? '#ddbd86' : '#a2b0a8';
      ctx.fillText(obstacle.dynamic ? 'FORKLIFT' : `SHELF ${String(i + 1).padStart(2, '0')}`, x + ow / 2, y - 7);
    }
  });
  if (layers.path && frame.path.length) {
    ctx.beginPath(); ctx.strokeStyle = '#e7a16d'; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    frame.path.forEach((p, i) => { if (!i) ctx.moveTo(X(p.x), Y(p.y)); else ctx.lineTo(X(p.x), Y(p.y)); });
    ctx.stroke();
    ctx.fillStyle = '#f2bf94';
    for (const p of frame.path) { ctx.beginPath(); ctx.arc(X(p.x), Y(p.y), 2.4, 0, Math.PI * 2); ctx.fill(); }
  }
  if (frame.goal) {
    const gx = X(frame.goal.x), gy = Y(frame.goal.y);
    ctx.strokeStyle = '#dca878'; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.arc(gx, gy, 11, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = '#e9b685'; ctx.beginPath(); ctx.moveTo(gx, gy - 4); ctx.lineTo(gx + 4, gy); ctx.lineTo(gx, gy + 4); ctx.lineTo(gx - 4, gy); ctx.closePath(); ctx.fill();
    ctx.font = `${mobile ? 7 : 8}px ${font}`; ctx.fillStyle = '#dfb389';
    const alignLeft = gx < left + w * .7;
    ctx.textAlign = alignLeft ? 'left' : 'right';
    ctx.fillText(`GOAL / ${frame.goal.label.toUpperCase()}`, gx + (alignLeft ? 16 : -16), gy - 12);
  }
  const rx = X(frame.robot.x), ry = Y(frame.robot.y), radius = frame.robot.radius * scale;
  ctx.strokeStyle = frame.safety_stop === true ? '#e9978a' : '#e9a778';
  ctx.fillStyle = '#bd8054';
  ctx.beginPath(); ctx.arc(rx, ry, radius, 0, Math.PI * 2); ctx.fill();
  ctx.lineWidth = 1.5; ctx.stroke();
  // The radius and yaw are native measurements; the small heading line is a glyph.
  ctx.beginPath(); ctx.moveTo(rx, ry); ctx.lineTo(rx + Math.cos(frame.robot.yaw) * (radius + 11), ry - Math.sin(frame.robot.yaw) * (radius + 11)); ctx.stroke();
  ctx.fillStyle = '#f4d0ad'; ctx.beginPath(); ctx.arc(rx, ry, Math.min(2, radius / 3), 0, Math.PI * 2); ctx.fill();
  ctx.font = `7px ${font}`; ctx.textAlign = 'left'; ctx.fillStyle = '#e7b28c';
  const labelX = Math.min(left + w - 50, rx + radius + 9);
  ctx.fillText('ROVER', Math.max(left + 3, labelX), ry + radius + 12);
  ctx.restore();
}

function drawTimeline() {
  const ctx = timelineContext, { width, height } = timelineSize;
  ctx.clearRect(0, 0, width, height);
  const frames = store.mode === 'replay' ? store.replay?.frames || [] : store.history;
  const y = height / 2;
  ctx.fillStyle = '#37423f'; ctx.fillRect(0, y - 1, width, 2);
  if (!frames.length) return;
  const start = frames[0].time_s, end = frames.at(-1).time_s, duration = end - start;
  ctx.fillStyle = store.mode === 'replay' ? '#7d6048' : '#506b60';
  ctx.fillRect(0, y - 2, width, 4);
  const stride = Math.max(1, Math.floor(frames.length / Math.max(1, width / 3)));
  for (let i = 0; i < frames.length; i += stride) {
    const f = frames[i];
    const x = duration > 0 ? (f.time_s - start) / duration * (width - 8) + 4 : 4;
    ctx.fillStyle = f.safety_stop === true ? '#e9978a' : '#778b80';
    ctx.fillRect(x, y - (f.safety_stop === true ? 7 : 4), 1, f.safety_stop === true ? 14 : 8);
    if (i > 0 && f.goals_completed !== null && f.goals_completed > frames[i - 1].goals_completed) {
      ctx.fillStyle = '#e6ad7b'; ctx.fillRect(x - 1, y - 8, 3, 16);
    }
  }
}

$('live-mode').addEventListener('click', () => switchMode('live'));
$('replay-mode').addEventListener('click', () => {
  if (store.mode === 'replay' && !store.replay && !store.replayLoading) loadReplay(store.selectedId);
  else switchMode('replay');
});
$('recording-select').addEventListener('change', (event) => {
  store.selectedId = event.target.value;
  if (store.mode === 'replay') loadReplay(store.selectedId);
  else update();
});
$('replay-play').addEventListener('click', togglePlay);
$('replay-start').addEventListener('click', () => seek(0));
$('replay-end').addEventListener('click', () => seek(store.replay?.frames.length - 1));
$('replay-scrub').addEventListener('input', (event) => seek(Number(event.target.value)));
$('replay-speed').addEventListener('change', (event) => {
  store.speed = Number(event.target.value);
  if (store.playing && store.replay) { store.anchorMs = performance.now(); store.anchorTime = store.replay.frames[store.replayIndex].time_s; }
});
for (const name of ['path', 'trace', 'lidar']) $(`layer-${name}`).addEventListener('change', (event) => { layers[name] = event.target.checked; dirty = true; });
document.addEventListener('keydown', (event) => {
  if (store.mode !== 'replay' || !store.replay || event.altKey || event.ctrlKey || event.metaKey) return;
  if (event.target.closest('select, textarea, [contenteditable="true"]')) return;
  // Native range keyboard behavior already emits an input event.
  if (event.target === $('replay-scrub') && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  if (event.key === ' ' && event.target.closest('button, a, input')) return;
  if ([' ', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) event.preventDefault();
  if (event.key === ' ') togglePlay();
  else if (event.key === 'ArrowLeft') seek(store.replayIndex - (event.shiftKey ? 10 : 1));
  else if (event.key === 'ArrowRight') seek(store.replayIndex + (event.shiftKey ? 10 : 1));
  else if (event.key === 'Home') seek(0);
  else if (event.key === 'End') seek(store.replay.frames.length - 1);
});
document.addEventListener('visibilitychange', () => { if (document.hidden) { stop(); update(); } });

// No interpolation or extrapolation: every rendered pose is a received frame.
setInterval(() => {
  if (store.playing && store.replay && store.mode === 'replay') {
    const frames = store.replay.frames;
    const target = store.anchorTime + (performance.now() - store.anchorMs) / 1000 * store.speed;
    const next = replayIndexAt(frames, target);
    if (target >= frames.at(-1).time_s) stop();
    if (next !== store.replayIndex || !store.playing) { store.replayIndex = next; update(); }
  }
  if (dirty) { drawField(); drawTimeline(); dirty = false; }
}, 50);
// Detect a silent/frozen runtime even if no fetch completes.
setInterval(update, 500);
update();
poll();
