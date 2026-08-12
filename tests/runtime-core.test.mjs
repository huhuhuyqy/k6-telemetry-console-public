import assert from 'node:assert/strict';
import test from 'node:test';

import {
  SerialTaskQueue,
  buildFrame,
  encodeLightingBin,
  parseReadRamChunk,
  parseResponse,
  selectCoreCue,
  selectDisplayCue,
  validateLightingRam,
  validateRestorableLightingSnapshot,
  wrapLightingBin,
} from '../runtime-core.mjs';

test('HID response parsing follows the declared frame length', () => {
  const frame = buildFrame(0xa3, Uint8Array.from([0, 4, 1]));
  const padded = new Uint8Array(63);
  padded.set(frame);
  assert.deepEqual([...parseResponse(padded, 0xa3)], [5, 0, 4, 1]);
  assert.equal(parseResponse(frame.slice(0, -1), 0xa3), null);
});

test('HID response parsing accepts a checksummed K6 frame with zero report padding', () => {
  // Some K6 receiver revisions return a command-specific length byte rather
  // than the physical response payload length. The frame boundary is still
  // unambiguous because its checksum is followed only by HID zero padding.
  const bytes = [0x5a, 0xa5, 0xa4, 0x02, 0x00];
  bytes.push(bytes.slice(2).reduce((sum, byte) => (sum + byte) & 0xff, 0));
  const report = new Uint8Array(32);
  report.set(bytes);
  assert.deepEqual([...parseResponse(report, 0xa4)], [0x02, 0x00]);
});

test('A3 rejects a chunk whose declared bytes are missing', () => {
  const truncated = Uint8Array.from([12, 0, 4, 1, 24, 0, 0, 16, 1, 2, 3, 4]);
  assert.throws(() => parseReadRamChunk(truncated, 0), /长度异常/);
});

test('lighting snapshots validate all protocol checks', () => {
  const wrapped = wrapLightingBin(encodeLightingBin({ r: 12, g: 34, b: 56, brightness: 78 }));
  assert.deepEqual(validateLightingRam(wrapped), { version: 2, payloadLength: 6 });
  const corrupted = new Uint8Array(wrapped);
  corrupted[corrupted.length - 1] ^= 0xff;
  assert.throws(() => validateLightingRam(corrupted), /CRC16/);
});

test('an A3 direct LGHT snapshot remains eligible for exact restoration', () => {
  const directRam = encodeLightingBin({ r: 12, g: 34, b: 56, brightness: 78 });
  assert.deepEqual(
    validateRestorableLightingSnapshot(directRam),
    { format: 'lght', version: 2, payloadLength: 6 },
  );
});

test('a non-empty firmware preset can be backed up as opaque exact bytes', () => {
  const preset = Uint8Array.from([0x21, 0x05, 0x91, 0x44, 0x02, 0x7f]);
  assert.deepEqual(validateRestorableLightingSnapshot(preset), { format: 'opaque', length: 6 });
  assert.throws(() => validateRestorableLightingSnapshot(new Uint8Array(16)), /空白数据/);
});

test('lighting transactions remain serialized for their complete lifetime', async () => {
  const queue = new SerialTaskQueue();
  const events = [];
  let releaseFirst;
  const firstGate = new Promise((resolve) => { releaseFirst = resolve; });
  const first = queue.run(async () => {
    events.push('first-begin');
    await firstGate;
    events.push('first-commit');
  });
  const second = queue.run(async () => {
    events.push('second-begin');
    events.push('second-commit');
  });
  await Promise.resolve();
  assert.deepEqual(events, ['first-begin']);
  releaseFirst();
  await Promise.all([first, second]);
  assert.deepEqual(events, ['first-begin', 'first-commit', 'second-begin', 'second-commit']);
});

test('core cue order keeps RPM above TC, wrong-way and DRS', () => {
  const lowerCues = { tcActive: true, wrongWay: true, drsActive: true };
  assert.equal(selectCoreCue(0.5, lowerCues, 0, false), 'rpm');
  assert.equal(selectCoreCue(0.5, { ...lowerCues, absActive: true }, 80, true), 'abs');
  assert.equal(selectCoreCue(0.5, lowerCues, 80, true), 'brake');
  assert.equal(selectCoreCue(0.5, lowerCues, 0, true), 'shift-up');
});

test('vehicle instrumentation remains visible above the ordinary RPM background', () => {
  assert.equal(
    selectDisplayCue(0.5, { indicatorLeft: true }, 0, false),
    'indicator-left',
  );
  assert.equal(
    selectDisplayCue(0.5, { wiperStage: 2 }, 0, false),
    'wiper',
  );
  assert.equal(
    selectDisplayCue(0.5, { indicatorLeft: true, absActive: true }, 0, false),
    'abs',
  );
  assert.equal(
    selectDisplayCue(0.5, { indicatorLeft: true, indicatorRight: true }, 0, false),
    'hazards',
  );
  assert.equal(
    selectDisplayCue(0.5, { headlights: true }, 0, false),
    'headlights',
  );
});
