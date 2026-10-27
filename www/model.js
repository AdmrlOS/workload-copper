const finite = (value) => typeof value === 'number' && Number.isFinite(value);
const number = (value) => finite(value) ? value : null;
const point = (value) => value && finite(value.x) && finite(value.y);
const count = (value) => Number.isInteger(value) && value >= 0 ? value : null;

/** Freshness is based on received AND progressing native output. */
export function connectionState(state, { now, receivedAt = null, progressedAt = null, error = null } = {}) {
  if (error || (receivedAt !== null && now - receivedAt > 2000)) return { kind: 'unreachable', label: 'Backend unreachable', detail: error || 'No response for more than 2 seconds.' };
  if (!state) return { kind: 'connecting', label: 'Connecting', detail: 'Waiting for the local Copper runtime.' };
  if (state.status === 'error') return { kind: 'error', label: 'Runtime error', detail: String(state.error || 'The native runtime reported an error.') };
  if (state.status === 'starting') return { kind: 'starting', label: 'Runtime starting', detail: 'Waiting for the first native control cycle.' };
  if (state.status === 'verifying') return { kind: 'verifying', label: 'Verifying recording', detail: 'The backend is checking a native Copper replay.' };
  if (!normalizeFrame(state.frame)) return { kind: 'waiting', label: 'Awaiting telemetry', detail: 'The backend has not supplied a valid simulation frame.' };
  if (state.status !== 'live') return { kind: 'waiting', label: 'Unknown runtime state', detail: 'The backend has not reported a live runtime.' };
  if (progressedAt === null || now - progressedAt > 2000) return { kind: 'stale', label: 'Telemetry stale', detail: 'The native tick has not advanced for more than 2 seconds.' };
  return { kind: 'live', label: 'Runtime live', detail: 'Receiving native simulation output.' };
}


export const safeRunId = (id) => typeof id === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(id);

export function formatBytes(value) {
  if (!finite(value) || value < 0) return '—';
  if (value < 1024) return `${Math.floor(value)} B`;
  const unit = value < 1024 ** 2 ? 1 : value < 1024 ** 3 ? 2 : 3;
  return `${(value / (1024 ** unit)).toFixed(1)} ${['B', 'KiB', 'MiB', 'GiB'][unit]}`;
}

/** A verification flag comes from the backend, never the animation. */
export function prepareReplay(payload) {
  const verification = payload?.verification;
  if (verification?.verified !== true) throw new Error('This recording has not been verified by the native backend.');
  if (verification.mismatches !== 0) throw new Error('Native replay reported mismatches. Playback is blocked.');
  if (!Number.isInteger(verification.cycles) || verification.cycles <= 0) throw new Error('Verified replay has no reported native cycles.');
  if (!Array.isArray(payload.frames) || !payload.frames.length) throw new Error('Verified replay contains no frames.');
  if (payload.frames.length !== verification.cycles) throw new Error('Replay frame count does not match the verified native cycles.');
  const frames = payload.frames.map(normalizeFrame);
  if (frames.some((f) => !f)) throw new Error('Replay contains an invalid simulation frame.');
  if (frames.some((f, i) => i > 0 && (f.time_s < frames[i - 1].time_s || f.tick <= frames[i - 1].tick))) throw new Error('Replay frames are not in native cycle order.');
  return { id: payload.id, verification: { ...verification }, frames };
}

export function seekIndex(value, length) {
  return Math.max(0, Math.min(Math.max(0, length - 1), Number.isFinite(Number(value)) ? Math.round(Number(value)) : 0));
}

/** Select a recorded frame at native simulation time; never synthesize movement. */
export function replayIndexAt(frames, time) {
  if (!frames.length) return 0;
  let low = 0;
  let high = frames.length - 1;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (frames[middle].time_s <= time) low = middle;
    else high = middle - 1;
  }
  return low;
}

/** Validate actual backend geometry. Missing measurements stay unknown, never zero. */
export function normalizeFrame(raw) {
  if (!raw || !Number.isInteger(raw.tick) || raw.tick < 0 || !finite(raw.time_s) ||
      !point(raw.robot) || !finite(raw.robot.yaw) || !finite(raw.robot.radius) || raw.robot.radius <= 0 ||
      !raw.world || !finite(raw.world.width) || !finite(raw.world.height) ||
      raw.world.width <= 0 || raw.world.height <= 0) return null;
  return {
    tick: raw.tick,
    time_s: raw.time_s,
    robot: { x: raw.robot.x, y: raw.robot.y, yaw: raw.robot.yaw, radius: raw.robot.radius },
    world: { width: raw.world.width, height: raw.world.height },
    goal: point(raw.goal) ? { ...raw.goal, label: String(raw.goal.label || 'Mission goal') } : null,
    path_available: Array.isArray(raw.path),
    path: Array.isArray(raw.path) ? raw.path.filter(point).map(({ x, y }) => ({ x, y })) : [],
    obstacles: Array.isArray(raw.obstacles) ? raw.obstacles.filter((o) => point(o) && finite(o.w) && finite(o.h) && o.w > 0 && o.h > 0).map((o) => ({ x: o.x, y: o.y, w: o.w, h: o.h, dynamic: o.dynamic === true })) : [],
    lidar: Array.isArray(raw.lidar) ? raw.lidar.filter(point).map(({ x, y }) => ({ x, y })) : [],
    command: { v: number(raw.command?.v), omega: number(raw.command?.omega) },
    safety_stop: typeof raw.safety_stop === 'boolean' ? raw.safety_stop : null,
    nearest_obstacle_m: number(raw.nearest_obstacle_m),
    planner_status: typeof raw.planner_status === 'string' ? raw.planner_status : null,
    goals_completed: count(raw.goals_completed),
    pipeline_us: number(raw.pipeline_us),
    collisions: count(raw.collisions),
  };
}
