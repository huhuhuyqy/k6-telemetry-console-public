export const COMMAND = Object.freeze({ READ_RAM: 0xa3, WRITE_RAM: 0xa4 });
export const LED_RAM_ID = 0x04;
export const RAM_READ_CHUNK_SIZE = 16;
export const MAX_LIGHTING_RAM_SIZE = 4096;
export const LIGHT_MODE_SOLID = 1;

export function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

export function lerp(start, end, amount) {
  return start + (end - start) * amount;
}

export function smoothstep(amount) {
  return amount * amount * (3 - 2 * amount);
}

export function u16(value) {
  return [value & 0xff, (value >>> 8) & 0xff];
}

export function u32(value) {
  return [value & 0xff, (value >>> 8) & 0xff, (value >>> 16) & 0xff, (value >>> 24) & 0xff];
}

function readU16(bytes, offset) {
  return (bytes[offset] ?? 0) | ((bytes[offset + 1] ?? 0) << 8);
}

function readU32(bytes, offset) {
  return (
    (bytes[offset] ?? 0)
    | ((bytes[offset + 1] ?? 0) << 8)
    | ((bytes[offset + 2] ?? 0) << 16)
    | ((bytes[offset + 3] ?? 0) << 24)
  ) >>> 0;
}

export function sum16(bytes, start = 0, end = bytes.length) {
  let result = 0;
  for (let index = start; index < end; index += 1) result = (result + bytes[index]) & 0xffff;
  return result;
}

export function crc16Ccitt(bytes) {
  let crc = 0xffff;
  for (const byte of bytes) {
    crc ^= byte << 8;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = crc & 0x8000 ? ((crc << 1) ^ 0x1021) & 0xffff : (crc << 1) & 0xffff;
    }
  }
  return crc;
}

const crc32Table = (() => {
  const table = new Uint32Array(256);
  for (let index = 0; index < 256; index += 1) {
    let value = index;
    for (let bit = 0; bit < 8; bit += 1) {
      value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    }
    table[index] = value >>> 0;
  }
  return table;
})();

export function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) crc = crc32Table[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

export function buildFrame(command, payload) {
  const frame = new Uint8Array(payload.length + 5);
  frame.set([0x5a, 0xa5, command, payload.length + 2], 0);
  frame.set(payload, 4);
  frame[frame.length - 1] = frame.slice(2, -1).reduce((sum, byte) => (sum + byte) & 0xff, 0);
  return frame;
}

export function parseResponse(bytes, command) {
  let offset = 0;
  while (offset < bytes.length - 1 && !(bytes[offset] === 0x5a && bytes[offset + 1] === 0xa5)) offset += 1;
  if (offset + 5 > bytes.length || bytes[offset + 2] !== command) return null;
  const declaredLength = bytes[offset + 3];
  const end = offset + 3 + declaredLength;
  const hasChecksumAt = (candidateEnd) => {
    let checksum = 0;
    for (let index = offset + 2; index < candidateEnd - 1; index += 1) {
      checksum = (checksum + bytes[index]) & 0xff;
    }
    return checksum === bytes[candidateEnd - 1];
  };
  if (declaredLength >= 2 && end <= bytes.length && hasChecksumAt(end)) {
    return bytes.slice(offset + 3, end - 1);
  }

  // K6 receiver firmware can use a command-specific length value in replies
  // while the physical input report is fixed-size and zero padded. Accept a
  // fallback boundary only when its checksum is valid and every following
  // byte is padding, so arbitrary bytes cannot be mistaken for a short frame.
  for (let candidateEnd = offset + 5; candidateEnd <= bytes.length; candidateEnd += 1) {
    let zeroPadded = true;
    for (let index = candidateEnd; index < bytes.length; index += 1) {
      if (bytes[index] !== 0) {
        zeroPadded = false;
        break;
      }
    }
    if (zeroPadded && hasChecksumAt(candidateEnd)) {
      return bytes.slice(offset + 3, candidateEnd - 1);
    }
  }
  return null;
}

export function parseReadRamChunk(payload, expectedChunkIndex) {
  const status = payload[1] ?? 255;
  const ramId = payload[2] ?? 255;
  const activeSlot = payload[3] ?? 0;
  const totalLength = readU16(payload, 4);
  const chunkIndex = payload[6] ?? 255;
  const chunkSize = payload[7] ?? 0;
  if (status !== 0) throw new Error(`灯光读取失败，chunk ${expectedChunkIndex}，状态 ${status}`);
  if (ramId !== LED_RAM_ID) throw new Error(`灯光读取 RAM 不一致：0x${ramId.toString(16)}`);
  if (chunkIndex !== expectedChunkIndex) throw new Error(`灯光读取分块不一致：${chunkIndex}/${expectedChunkIndex}`);
  if (totalLength <= 0 || totalLength > MAX_LIGHTING_RAM_SIZE) throw new Error(`灯光 RAM 长度异常：${totalLength}B`);
  const available = Math.max(0, payload.length - 8);
  if (chunkSize <= 0 || chunkSize > available) {
    throw new Error(`灯光读取分块 ${chunkIndex} 长度异常：声明 ${chunkSize}B，实际 ${available}B`);
  }
  return { activeSlot, totalLength, chunkIndex, data: payload.slice(8, 8 + chunkSize) };
}

export function encodeLightingPayload(payload) {
  const header = new Uint8Array(12);
  header.set([0x4c, 0x47, 0x48, 0x54], 0);
  header.set(u16(2), 4);
  header.set(u32(payload.length), 6);
  header.set(u16(sum16(header, 0, 10)), 10);
  const result = new Uint8Array(header.length + payload.length + 2);
  result.set(header, 0);
  result.set(payload, header.length);
  result.set(u16(sum16(payload)), header.length + payload.length);
  return result;
}

export function encodeLightingBin({ r, g, b, brightness = 100 }, vibration = false) {
  return encodeLightingPayload(Uint8Array.from([
    LIGHT_MODE_SOLID, r, g, b, clamp(brightness, 0, 100), vibration ? 1 : 0,
  ]));
}

export function wrapLightingBin(bin) {
  const wrapped = new Uint8Array(bin.length + 4);
  wrapped.set(u16(bin.length + 4), 0);
  wrapped.set(u16(crc16Ccitt(bin)), 2);
  wrapped.set(bin, 4);
  return wrapped;
}

function validateLightingBin(bin) {
  if (!(bin instanceof Uint8Array) || bin.length < 14) throw new Error('灯光 RAM 内容过短。');
  if (String.fromCharCode(...bin.slice(0, 4)) !== 'LGHT') throw new Error('灯光 RAM 缺少 LGHT 标识。');
  if (readU16(bin, 10) !== sum16(bin, 0, 10)) throw new Error('灯光 RAM 头校验失败。');
  const payloadLength = readU32(bin, 6);
  const expectedBinLength = 12 + payloadLength + 2;
  if (expectedBinLength !== bin.length) {
    throw new Error(`灯光 RAM 内容长度不一致：${expectedBinLength}/${bin.length}`);
  }
  const payload = bin.slice(12, 12 + payloadLength);
  if (readU16(bin, 12 + payloadLength) !== sum16(payload)) throw new Error('灯光 RAM 内容校验失败。');
  return { version: readU16(bin, 4), payloadLength };
}

export function validateLightingRam(wrapped) {
  if (!(wrapped instanceof Uint8Array) || wrapped.length < 18) throw new Error('灯光 RAM 数据过短。');
  const declaredWrappedLength = readU16(wrapped, 0);
  if (declaredWrappedLength !== wrapped.length) {
    throw new Error(`灯光 RAM 包装长度不一致：${declaredWrappedLength}/${wrapped.length}`);
  }
  const bin = wrapped.slice(4);
  const declaredCrc = readU16(wrapped, 2);
  const actualCrc = crc16Ccitt(bin);
  if (declaredCrc !== actualCrc) throw new Error('灯光 RAM CRC16 校验失败。');
  return validateLightingBin(bin);
}

// A3 returns the RAM field exactly as stored by the device. Depending on the
// firmware and how the effect was created, that field can be a wrapped A4
// blob, a direct LGHT blob, or a firmware-owned opaque preset. Completeness is
// established by the A3 total length and exact chunk checks in app.js; this
// function must not require the A4 transport wrapper for an exact-byte backup.
export function validateRestorableLightingSnapshot(snapshot) {
  if (!(snapshot instanceof Uint8Array) || snapshot.length === 0 || snapshot.length > MAX_LIGHTING_RAM_SIZE) {
    throw new Error(`灯光快照长度异常：${snapshot?.length ?? 0}B`);
  }
  const magicAtStart = String.fromCharCode(...snapshot.slice(0, 4)) === 'LGHT';
  if (magicAtStart) return { format: 'lght', ...validateLightingBin(snapshot) };
  const magicAfterWrapper = snapshot.length >= 18
    && String.fromCharCode(...snapshot.slice(4, 8)) === 'LGHT';
  if (magicAfterWrapper) return { format: 'wrapped-lght', ...validateLightingRam(snapshot) };
  const allZero = snapshot.every((byte) => byte === 0);
  const allErased = snapshot.every((byte) => byte === 0xff);
  if (allZero || allErased) throw new Error('灯光快照是空白数据，不能用于恢复。');
  return { format: 'opaque', length: snapshot.length };
}

export class SerialTaskQueue {
  constructor() {
    this.tail = Promise.resolve();
  }

  run(operation) {
    const pending = this.tail.then(operation, operation);
    this.tail = pending.catch(() => {});
    return pending;
  }

  idle() {
    return this.tail;
  }
}

export function selectCoreCue(normalized, data, brake, shifting) {
  if (data?.absActive) return 'abs';
  if (brake > 2) return 'brake';
  if (shifting || data?.shiftUpHint) return 'shift-up';
  if (data?.shiftDownHint) return 'shift-down';
  if (normalized > 0) return 'rpm';
  if (data?.tcActive) return 'tc';
  if (data?.wrongWay) return 'wrong-way';
  if (data?.drsActive) return 'drs-active';
  if (data?.drsAvailable) return 'drs-available';
  return null;
}

// Vehicle instrumentation has to be visible while the engine is running.
// Keep it below urgent driving cues (ABS, brake and shift) but above the
// ordinary RPM background. Otherwise every signal is hidden as soon as RPM
// becomes non-zero, which defeats the road-car/showroom mappings.
export function selectDisplayCue(normalized, data, brake, shifting) {
  const coreCue = selectCoreCue(normalized, data, brake, shifting);
  if (['abs', 'brake', 'shift-up', 'shift-down'].includes(coreCue)) return coreCue;
  if (data?.warningLights) return 'warning-lights';
  // ACC/AC EVO normally provide hazardLights, but deriving it here also keeps
  // the display correct for sources that expose only the two direction flags.
  if (data?.hazardLights || (data?.indicatorLeft && data?.indicatorRight)) return 'hazards';
  if (data?.indicatorLeft) return 'indicator-left';
  if (data?.indicatorRight) return 'indicator-right';
  if (data?.flashingLights) return 'flashing-lights';
  if (Number(data?.wiperStage) > 0) return 'wiper';
  if (data?.rainLights) return 'rain-lights';
  if (data?.headlights || Number(data?.mainLightStage) > 0) return 'headlights';
  if (Number(data?.specialLightStage) > 0) return 'special-lights';
  if (Number(data?.cockpitLightStage) > 0) return 'cockpit-lights';
  return coreCue;
}
