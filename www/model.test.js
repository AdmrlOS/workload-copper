import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeFrame, connectionState, prepareReplay, replayIndexAt, seekIndex, formatBytes, safeRunId } from './model.js';

const frame = (overrides = {}) => ({
  tick: 12, time_s: 0.6,
  robot: { x: 2, y: 3, yaw: 0.5, radius: 0.35 },
  world: { width: 24, height: 16 },
  goal: { x: 20, y: 12, label: 'Loading bay', index: 1 },
  path: [{ x: 2, y: 3 }, { x: 20, y: 12 }],
  obstacles: [], ...overrides,
});

test('successful HTTP polls cannot label a frozen or missing simulation frame live', () => {
  const fresh = { status: 'live', frame: frame() };
  assert.equal(connectionState(fresh, { now: 1500, receivedAt: 1400, progressedAt: 1400 }).kind, 'live');
  assert.equal(connectionState(fresh, { now: 5000, receivedAt: 4900, progressedAt: 1000 }).kind, 'stale');
  assert.equal(connectionState(fresh, { now: 5000, receivedAt: 1000, progressedAt: 4900 }).kind, 'unreachable');
  assert.equal(connectionState(fresh, { now: 1500, receivedAt: 1400, progressedAt: 1400, error: 'Network failed' }).kind, 'unreachable');
  assert.equal(connectionState({ status: 'live', frame: null }, { now: 100, receivedAt: 100, progressedAt: 100 }).kind, 'waiting');
  assert.equal(connectionState({ status: 'error', error: 'Native engine exited' }, { now: 100, receivedAt: 100 }).kind, 'error');
  assert.equal(connectionState(null, { now: 0 }).kind, 'connecting');
});

test('native replay refuses unverified, mismatched, empty, and corrupt output', () => {
  const payload = { id: '20261027-001', verification: { verified: true, cycles: 3, mismatches: 0, engine: 'Copper', detail: 'Native verification' }, frames: [frame({ tick: 0, time_s: 0 }), frame({ tick: 1, time_s: 0.05 }), frame({ tick: 2, time_s: 0.2 })] };
  assert.throws(() => prepareReplay({ ...payload, verification: { ...payload.verification, verified: false } }), /verified/i);
  assert.throws(() => prepareReplay({ ...payload, verification: { ...payload.verification, mismatches: 1 } }), /mismatch/i);
  assert.throws(() => prepareReplay({ ...payload, frames: [] }), /frame/i);
  assert.throws(() => prepareReplay({ ...payload, frames: [payload.frames[1], payload.frames[0], payload.frames[2]] }), /order/i);
  assert.throws(() => prepareReplay({ ...payload, frames: payload.frames.slice(0, 2) }), /cycle|count/i);
  assert.throws(() => prepareReplay({ ...payload, frames: [null, null, null] }), /frame/i);
  const replay = prepareReplay(payload);
  assert.equal(replay.frames.length, 3);
  assert.equal(replay.verification.cycles, 3);
  assert.equal(replayIndexAt(replay.frames, 0), 0);
  assert.equal(replayIndexAt(replay.frames, 0.1), 1);
  assert.equal(replayIndexAt(replay.frames, 100), 2);
  assert.equal(seekIndex(-10, 3), 0);
  assert.equal(seekIndex(99, 3), 2);
  assert.equal(seekIndex(NaN, 3), 0);
  assert.equal(seekIndex(0, 0), 0);
});

test('recording links accept only safe backend tokens and missing sizes stay unknown', () => {
  assert.equal(safeRunId('20261027T030410-abc_1'), true);
  for (const id of ['../private', '', null, 'a/b', 'a%2fb', '<script>', 'a?b']) assert.equal(safeRunId(id), false);
  assert.equal(formatBytes(null), '—');
  assert.equal(formatBytes(0), '0 B');
  assert.equal(formatBytes(1024), '1.0 KiB');
  assert.equal(formatBytes(1048576), '1.0 MiB');
});

test('accepts actual rover output without inventing absent telemetry', () => {
  const result = normalizeFrame(frame());
  assert.equal(result.tick, 12);
  assert.equal(result.robot.x, 2);
  assert.equal(result.pipeline_us, null);
  assert.equal(result.command.v, null);
  assert.equal(result.collisions, null);
  assert.equal(result.safety_stop, null);
  assert.equal(normalizeFrame(frame({ path: undefined })).path_available, false);
  assert.equal(normalizeFrame(frame({ path: [] })).path_available, true);
});

test('rejects unusable frames and removes malformed geometric points', () => {
  assert.equal(normalizeFrame(null), null);
  assert.equal(normalizeFrame(frame({ world: { width: 0, height: 10 } })), null);
  assert.equal(normalizeFrame(frame({ robot: { x: NaN, y: 0, yaw: 0, radius: 0.3 } })), null);
  const result = normalizeFrame(frame({ path: [{ x: 1, y: 2 }, { x: '3', y: 4 }], obstacles: [{ x: 1, y: 1, w: -1, h: 1 }] }));
  assert.deepEqual(result.path, [{ x: 1, y: 2 }]);
  assert.deepEqual(result.obstacles, []);
});
